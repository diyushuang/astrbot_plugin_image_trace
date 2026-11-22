"""机器人已发图片的消息账本：``message_id → 本条消息里发过的原图``。

为什么需要它：``/原图`` 的引用路径以前只能靠文件名或图片内容哈希猜「用户引
用的到底是哪张」。但发图时平台往往已经回执了消息 ID，引用消息又带着被引用
消息的 ID（``Comp.Reply.id``）。把这两个 ID 对上，就能直接查回发送记录，
既不用下载引用图，也不会把 ``/原图`` 变成第二个 ``/溯源``。

记账只在**平台回执了 message_id** 的通道发生（OneBot 直发、QQ 官方
``post_media``）。标准消息链由 AstrBot respond stage 代发，插件拿不到回执，
这类消息用「配文 → 原图」的次级记录兜底（引用消息的 ``Reply.message_str``
仍带着发送时的配文）。

持久化：传了 ``path`` 时，账本会原子写入 JSON（临时文件 + ``os.replace``），
启动时自动读回；写入默认延迟 1.5 秒合并，卸载时 ``close()`` 强制落盘。这样
机器人重启后，旧消息的引用仍能按记录回查，不必退回哈希。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from collections import OrderedDict

from astrbot.api import logger

from .random_media import clean_display_name, is_thumbnail_url

_DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024
_FORMAT_VERSION = 1


def _normalize_caption(caption) -> str:
    """把配文归一成可比较的键：统一换行、折叠空白、去首尾。"""
    text = str(caption or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return " ".join(text.split())


class SentMessageStore:
    """``(会话, message_id) → [图片条目]`` 的定长 LRU，可选 JSON 持久化。

    另维护一份 ``(会话, 配文) → [图片条目]``：标准消息链由 AstrBot respond
    stage 代发时拿不到 message_id（签到卡片走 native 通道就是这种），引用
    消息的 ``Reply.message_str`` 仍带着当时发送的配文。配文精确/唯一子串
    命中同样属于「按发送记录查回」，不需要下载与哈希。
    """

    def __init__(
        self,
        max_messages: int = 2000,
        *,
        path: str | os.PathLike | None = None,
        save_delay: float = 1.5,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self._max = max(1, int(max_messages))
        self._messages: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
        self._captions: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
        self._path = os.fspath(path) if path else ""
        self._save_delay = max(0.0, float(save_delay))
        self._max_file_bytes = max(1024, int(max_file_bytes))
        self._dirty = False
        self._timer: threading.Timer | None = None
        self._closed = False
        # 发送完成发生在事件协程里，读取发生在另一条命令协程里；CPython 的
        # dict 操作本身原子，但「取列表 + 刷新 LRU」是两步，加锁避免交错淘汰。
        self._lock = threading.Lock()
        # 写盘与定时器单独一把锁，避免持账本锁做 IO。
        self._save_lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    # 写入 / 读取
    # ------------------------------------------------------------------

    @staticmethod
    def _entries(blocks) -> list[dict]:
        """把发送块过滤成可回查的原图条目；缩略图/纯 path 一律不入账。"""
        if not isinstance(blocks, (list, tuple)):
            return []
        entries: list[dict] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            url = str(block.get("url") or "").strip()
            if not url or is_thumbnail_url(url):
                continue
            entries.append(
                {
                    "url": url,
                    "file_name": clean_display_name(block.get("file_name"))
                    or clean_display_name(url)
                    or "",
                }
            )
        return entries

    def _trim(self, store: OrderedDict) -> None:
        while len(store) > self._max:
            store.popitem(last=False)

    def remember(self, session: str, message_id: str, blocks: list[dict]) -> None:
        """登记一条已发出消息里的原图直链；没有可用原图时整条跳过。"""
        session = str(session or "").strip()
        message_id = str(message_id or "").strip()
        if not session or not message_id:
            return
        entries = self._entries(blocks)
        if not entries:
            return
        key = (session, message_id)
        with self._lock:
            self._messages[key] = entries
            self._messages.move_to_end(key)
            self._trim(self._messages)
            self._dirty = True
        self._schedule_save()

    def remember_caption(self, session: str, caption: str, blocks: list[dict]) -> None:
        """登记「配文 → 原图」，供拿不到 message_id 的标准消息链回查。"""
        session = str(session or "").strip()
        caption_key = _normalize_caption(caption)
        if not session or not caption_key:
            return
        entries = self._entries(blocks)
        if not entries:
            return
        key = (session, caption_key)
        with self._lock:
            self._captions[key] = entries
            self._captions.move_to_end(key)
            self._trim(self._captions)
            self._dirty = True
        self._schedule_save()

    def find(self, session: str, message_id: str) -> dict | None:
        """按被引用消息 ID 取回本条消息的第一张原图。"""
        session = str(session or "").strip()
        message_id = str(message_id or "").strip()
        if not session or not message_id:
            return None
        key = (session, message_id)
        with self._lock:
            entries = self._messages.get(key)
            if not entries:
                return None
            self._messages.move_to_end(key)
            return dict(entries[0])

    def find_caption(self, session: str, caption: str) -> dict | None:
        """按被引用消息的配文查原图；精确命中优先，其次唯一子串命中。

        子串只用于容忍平台在引用文本前后加 ``[图片]``/空白等包装；出现多条
        候选时不猜，交给按名/内容兜底，避免把另一条签到/随机图的原图发出去。
        """
        session = str(session or "").strip()
        caption_key = _normalize_caption(caption)
        if not session or not caption_key:
            return None
        with self._lock:
            entries = self._captions.get((session, caption_key))
            if entries:
                self._captions.move_to_end((session, caption_key))
                return dict(entries[0])
            matches = [
                (key, value)
                for key, value in self._captions.items()
                if key[0] == session
                and (key[1] in caption_key or caption_key in key[1])
            ]
            if not matches:
                return None
            longest = max(len(key[1]) for key, _value in matches)
            best = [(key, value) for key, value in matches if len(key[1]) == longest]
            if len(best) != 1:
                return None
            key, value = best[0]
            self._captions.move_to_end(key)
            return dict(value[0])

    def count(self) -> int:
        with self._lock:
            return len(self._messages)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _schedule_save(self) -> None:
        """首次变更后启动一个延迟写盘；已有定时器就复用它。

        不因每次发图而重置定时器：持续发图时也能约每 ``save_delay`` 秒落一次
        盘，避免高频发送把写盘无限推迟。
        """
        if not self._path or self._closed:
            return
        with self._save_lock:
            if self._closed or self._timer is not None:
                return
            timer = threading.Timer(self._save_delay, self._on_save_timer)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _on_save_timer(self) -> None:
        with self._save_lock:
            self._timer = None
        self._write_snapshot()

    def _snapshot(self) -> dict:
        with self._lock:
            if not self._dirty:
                return {}
            self._dirty = False
            return {
                "version": _FORMAT_VERSION,
                "messages": [
                    {
                        "session": session,
                        "message_id": message_id,
                        "entries": [dict(entry) for entry in entries],
                    }
                    for (session, message_id), entries in self._messages.items()
                ],
                "captions": [
                    {
                        "session": session,
                        "caption": caption,
                        "entries": [dict(entry) for entry in entries],
                    }
                    for (session, caption), entries in self._captions.items()
                ],
            }

    def _write_snapshot(self) -> None:
        """把当前快照原子写盘；失败只告警，不阻断发送/查询。"""
        if not self._path:
            return
        # 写盘串行化：定时写与 close/flush 可能同时触发，避免旧快照覆盖新快照。
        with self._save_lock:
            data = self._snapshot()
            if not data:
                return
            path = self._path
            parent = os.path.dirname(path) or "."
            temp_path = ""
            try:
                os.makedirs(parent, exist_ok=True)
                fd, temp_path = tempfile.mkstemp(
                    prefix=".sent_messages.", suffix=".tmp", dir=parent
                )
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, path)
                temp_path = ""
            except Exception as exc:
                with self._lock:
                    self._dirty = True
                logger.warning(f"发送记录写盘失败（下次变更会重试）: {exc}")
            finally:
                if temp_path:
                    with contextlib.suppress(OSError):
                        os.unlink(temp_path)

    def flush(self) -> None:
        """立即落盘（卸载/测试用）；没有变更时不产生写 IO。"""
        if not self._path:
            return
        with self._save_lock:
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()
        self._write_snapshot()

    def close(self) -> None:
        """停止定时器并强制落盘；可重复调用。"""
        self._closed = True
        self.flush()

    def _load(self) -> None:
        if not self._path or not os.path.isfile(self._path):
            return
        try:
            size = os.path.getsize(self._path)
            if size > self._max_file_bytes:
                logger.warning(
                    f"发送记录文件过大（{size} 字节），跳过加载: {self._path}"
                )
                return
            with open(self._path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception as exc:
            logger.warning(f"发送记录读取失败，按空账本启动: {exc}")
            return
        if not isinstance(raw, dict):
            logger.warning(f"发送记录格式不合法，按空账本启动: {self._path}")
            return

        messages = raw.get("messages")
        if isinstance(messages, list):
            for item in messages:
                if not isinstance(item, dict):
                    continue
                session = str(item.get("session") or "").strip()
                message_id = str(item.get("message_id") or "").strip()
                entries = self._entries(item.get("entries") or [])
                if not session or not message_id or not entries:
                    continue
                key = (session, message_id)
                self._messages[key] = entries
                self._messages.move_to_end(key)
        self._trim(self._messages)

        captions = raw.get("captions")
        if isinstance(captions, list):
            for item in captions:
                if not isinstance(item, dict):
                    continue
                session = str(item.get("session") or "").strip()
                caption = _normalize_caption(item.get("caption"))
                entries = self._entries(item.get("entries") or [])
                if not session or not caption or not entries:
                    continue
                key = (session, caption)
                self._captions[key] = entries
                self._captions.move_to_end(key)
        self._trim(self._captions)
        if self._messages or self._captions:
            logger.info(
                "发送记录已加载: "
                f"{len(self._messages)} 条消息 / {len(self._captions)} 条配文"
            )

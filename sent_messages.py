"""机器人已发图片的消息账本：``message_id → 本条消息里发过的原图``。

为什么需要它：``/原图`` 的引用路径以前只能靠文件名或图片内容哈希猜「用户引
用的到底是哪张」。但发图时平台往往已经回执了消息 ID，引用消息又带着被引用
消息的 ID（``Comp.Reply.id``）。把这两个 ID 对上，就能直接查回发送记录，
既不用下载引用图，也不会把 ``/原图`` 变成第二个 ``/溯源``。

记账只在**平台回执了 message_id** 的通道发生（OneBot 直发、QQ 官方
``post_media``）。标准消息链由 AstrBot respond stage 代发，插件拿不到回执，
这类消息仍走按名/按内容的旧兜底。账本与 ``MediaHistory`` 一样是进程内 LRU：
重启后旧消息 ID 失效，属于可接受的降级，不写盘避免每次发图都产生 IO。
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from .random_media import clean_display_name, is_thumbnail_url


class SentMessageStore:
    """``(会话, message_id) → [图片条目]`` 的定长 LRU。"""

    def __init__(self, max_messages: int = 2000) -> None:
        self._max = max(1, int(max_messages))
        self._messages: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
        # 发送完成发生在事件协程里，读取发生在另一条命令协程里；CPython 的
        # dict 操作本身原子，但「取列表 + 刷新 LRU」是两步，加锁避免交错淘汰。
        self._lock = threading.Lock()

    def remember(self, session: str, message_id: str, blocks: list[dict]) -> None:
        """登记一条已发出消息里的原图直链；没有可用原图时整条跳过。"""
        session = str(session or "").strip()
        message_id = str(message_id or "").strip()
        if not session or not message_id:
            return
        entries: list[dict] = []
        for block in blocks or []:
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
        if not entries:
            return
        key = (session, message_id)
        with self._lock:
            self._messages[key] = entries
            self._messages.move_to_end(key)
            while len(self._messages) > self._max:
                self._messages.popitem(last=False)

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

    def count(self) -> int:
        with self._lock:
            return len(self._messages)

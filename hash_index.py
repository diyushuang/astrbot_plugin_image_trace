"""图床感知哈希索引：把服务端（img-indexer）预置的 pHash 拉到本地做汉明检索。

**为什么需要它**：图床里的图不在机器人本机磁盘上，插件无法直接扫描（`scan_dirs`
只能扫本地目录）。此前的哈希引擎因此对图床图完全无能——只能靠向量引擎。而向量
引擎要跑一次多模态 embedding 推理，慢且必须联网。img-indexer 入库时顺带算好的
pHash 正好补上这个空档：拉一份全量哈希（2.4 万张图 × 64 hex ≈ 1.5MB），本地做
汉明显式比对是纯 numpy 位运算，毫秒级、零网络、零推理。

**两侧哈希必须逐位一致**：本模块**只比对**，不计算——查询哈希由 features.py 现算，
库侧哈希由 img-indexer 的 src/lib/phash.mjs 预置。两者必须给出同一个码，否则相似
度会集体塌到 0.5 附近、检索静默全空。一致性靠 tools/verify_phash.mjs 双向门禁守着。

**复用位矩阵内核**：汉明距离沿用 library.py 的 `_POPCOUNT[xor].sum(axis=1)`
（一次向量化算出与全库的距离），不另起一套算法——两套算法早晚会在边界位上分叉。

**缓存策略**：索引落盘为 JSON，进程内保留矩阵。命中缓存的条件是「点数没变」：
Qdrant 的 count 便宜（一次请求），比逐页取版本号划算；点数变了就整体重建，
不做增量——增量对不上位矩阵的构建方式，容易留下位置错乱的幽灵行。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import numpy as np
from astrbot.api import logger

from .features import phash_hex_len
from .library import _POPCOUNT

# 服务端为「中位数落在浮点噪声里」的退化图（纯色/纯渐变/大面积平坦图）写的哨兵
# 值。必须与 img-indexer 的 tools/gen_phash.mjs::DEGENERATE_SENTINEL 一致。
#
# 为什么是 '-' 而不是空串：Qdrant 的 is_empty 把「键缺失」「空串」「null」视为
# 同一类，用空串做哨兵会让这些点继续命中服务端的工作清单、回填永不收敛。
DEGENERATE_SENTINEL = "-"

# 翻页时顺带取回的展示字段。取值要克制：全量 2.4 万点，多取一个字段就多几百 KB。
# 公开为常量供 main.py 传给 scroll_payloads：scroll 默认只取 key 一个字段，
# 不显式带上 image_url 等字段的话，build() 会把所有点判成「无直链」整批跳过
# （1.7.0 生产路径就因漏传该参数导致索引恒为 0 条可用）。
REMOTE_HASH_PAYLOAD_KEYS = ("phash", "src", "image_url", "file_name", "mime", "created_at", "thumb_url")

# 单页点数。512 是 Qdrant 的常用档位：再大对服务端内存压力上升，再小则请求数翻倍。
_PAGE_SIZE = 512


@dataclass
class RemoteMatch:
    """一条远端（图床）哈希命中。

    字段名与 library.MatchResult 对齐，好让 main.py 的两条哈希腿共用同一套
    展示与回传代码——调用方只需按 `.origin` 区分来源。
    """

    id: str  # Qdrant point id（UUID5），仅用于日志与去重
    similarity: float
    image_url: str
    file_name: str
    file_path: str = ""  # 远端图没有本地路径，恒为空；保留字段以对齐 MatchResult
    note: str = ""
    width: int | None = None
    height: int | None = None
    created_at: str = ""
    file_size: int | None = None
    thumb_url: str = ""
    origin: str = "remote"


class HashIndex:
    """图床 pHash 的本地镜像索引（构建一次、多次查询）。

    线程安全按「读侧取快照」实现：`_cache` 是单个 tuple，一次赋值即换一代，
    查询方拿到的一定是完整一致的一代，不需要读锁；构建方在锁内换掉引用。
    这与 library.ImageLibrary 的做法一致。
    """

    def __init__(self, cache_path: str, *, expected_phash_hex_len: int = 0):
        self.cache_path = cache_path
        self.expected_hex_len = (
            int(expected_phash_hex_len) if expected_phash_hex_len else phash_hex_len(16)
        )
        # (items, matrix, bits, skipped) —— 空代用零宽矩阵，查询方无需特判
        self._cache: tuple = ([], np.zeros((0, 0), dtype=np.uint8), 0, 0)
        self.built_at: float = 0.0
        self.remote_count: int = 0
        # 仅用于保护缓存的读-改-写（滚动刷新）；查询侧不加锁（取引用即快照）
        self._lock = threading.Lock()
        self._hex_re = re.compile(rf"^[0-9a-f]{{{self.expected_hex_len}}}$")

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------

    def load_cache(self) -> bool:
        """尝试从磁盘读回上一次的索引。返回是否读到可用的一代。

        缓存文件损坏（写一半断电、手工改坏）一律按「没有缓存」处理并删掉——
        带着半截索引跑检索会给出「未命中」的确定结论，比没有索引危险得多。
        """
        if not self.cache_path or not os.path.isfile(self.cache_path):
            return False
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items")
            if not isinstance(items, list) or not items:
                return False
            created_at = float(data.get("created_at") or 0)
            count = int(data.get("remote_count") or 0)
        except Exception as exc:
            logger.warning(f"图床哈希缓存读取失败，将重建: {exc}")
            self._discard_cache_file()
            return False
        self._build(items, remote_count=count, built_at=created_at)
        logger.info(
            f"图床哈希缓存已载入：{len(self._cache[0])} 条（远端点位 {count}，"
            f"建于 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(created_at))}）"
        )
        return True

    def build(self, points: list, *, remote_count: int = 0, save: bool = True) -> int:
        """用 scroll 回来的点集重建索引，返回实际入索引条数。

        逐点校验，任何一条不合规都只跳过它、不影响其余点：线上的 hash 由另一个
        进程写入，格式错误（长度不对、非十六进制、哨兵值）只能跳过，不能整批放弃。
        """
        items: list = []
        skipped = 0
        for point in points:
            if not isinstance(point, dict):
                skipped += 1
                continue
            payload = point.get("payload")
            if not isinstance(payload, dict):
                skipped += 1
                continue
            phash = payload.get("phash")
            if not isinstance(phash, str):
                skipped += 1
                continue
            phash = phash.strip().lower()
            # 退化哨兵：这类图本就不适合做感知哈希检索，留在索引里只会制造噪声
            if phash == DEGENERATE_SENTINEL:
                skipped += 1
                continue
            if not self._hex_re.match(phash):
                skipped += 1
                continue
            image_url = _first_text(payload, "image_url", "src")
            if not image_url:
                # 没有直链就没法回传，索引里留着也只会命中后失败
                skipped += 1
                continue
            items.append(
                {
                    "p": phash,
                    "u": image_url,
                    "n": _first_text(payload, "file_name"),
                    "t": _first_text(payload, "thumb_url"),
                    "c": _first_text(payload, "created_at"),
                    "m": _first_text(payload, "mime"),
                    "i": str(point.get("id") or ""),
                }
            )
        if skipped:
            logger.info(f"图床哈希索引：跳过 {skipped} 条不合规记录（格式错误/退化/无直链）")
        # 把 skipped 传进 _build：那一步还要再剔一次「hex 解不开」的漏网条目，
        # 两处计数必须相加，否则 stats() 会恒为 0（看起来一切正常，实际瞒报）
        self._build(items, remote_count=int(remote_count or len(points)), skipped=skipped)
        if save:
            self._save()
        return len(items)

    def _build(
        self, items: list, *, remote_count: int, built_at: float = 0.0, skipped: int = 0
    ) -> None:
        """把条目列表编译成位矩阵并整体换代。"""
        ids: list = []
        raws: list = []
        for pos, item in enumerate(items):
            try:
                raw = bytes.fromhex(str(item.get("p") or ""))
            except (TypeError, ValueError):
                continue
            if len(raw) * 2 != self.expected_hex_len:
                continue
            raws.append(raw)
            ids.append(pos)
        if raws:
            matrix = np.frombuffer(b"".join(raws), dtype=np.uint8).reshape(len(ids), -1)
        else:
            matrix = np.zeros((0, 0), dtype=np.uint8)
        bits = matrix.shape[1] * 8
        kept = [items[i] for i in ids]
        # 单个 tuple 一次赋值：读侧拿到的总是完整一致的一代
        self._cache = (kept, matrix, bits, int(skipped) + (len(items) - len(kept)))
        self.remote_count = int(remote_count)
        self.built_at = float(built_at or time.time())

    def _save(self) -> None:
        if not self.cache_path:
            return
        with self._lock:
            items = self._cache[0]
            payload = {
                "version": 1,
                "created_at": self.built_at,
                "remote_count": self.remote_count,
                "hex_len": self.expected_hex_len,
                "items": items,
            }
            tmp = f"{self.cache_path}.tmp"
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.cache_path)), exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp, self.cache_path)  # 原子替换：不留半截文件
            except Exception as exc:
                logger.warning(f"图床哈希缓存写入失败（不影响本次检索）: {exc}")
                self._discard_cache_file(tmp)

    def _discard_cache_file(self, path: str = "") -> None:
        target = path or self.cache_path
        try:
            if target and os.path.isfile(target):
                os.remove(target)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def count(self) -> int:
        return len(self._cache[0])

    @property
    def is_empty(self) -> bool:
        return not self._cache[0]

    def stats(self) -> dict:
        items, matrix, bits, skipped = self._cache
        return {
            "count": len(items),
            "bits": bits,
            "skipped": skipped,
            "remote_count": self.remote_count,
            "built_at": self.built_at,
        }

    def search(self, phash: str, limit: int = 5) -> list:
        """按 pHash 汉明距离检索，返回按相似度降序的 RemoteMatch 列表。

        与 library.ImageLibrary.search 同构：同一个 `_POPCOUNT[xor].sum(axis=1)`
        内核、同一个 `1 - dist / bits` 相似度口径。两处口径必须一致，否则
        同一个阈值在两条腿上含义不同。
        """
        cache = self._cache  # 快照
        items, matrix, bits, _skipped = cache
        if not items or not phash:
            return []
        try:
            query = np.frombuffer(bytes.fromhex(phash), dtype=np.uint8)
        except ValueError:
            return []
        if matrix.size == 0 or query.size != matrix.shape[1]:
            return []
        xor = np.bitwise_xor(matrix, query[None, :])
        dists = _POPCOUNT[xor].sum(axis=1)
        order = np.argsort(dists, kind="stable")[: max(1, limit)]
        results: list = []
        for idx in order:
            item = items[int(idx)]
            results.append(
                RemoteMatch(
                    id=str(item.get("i") or ""),
                    similarity=1.0 - float(dists[int(idx)]) / max(1, bits),
                    image_url=str(item.get("u") or ""),
                    file_name=str(item.get("n") or ""),
                    created_at=str(item.get("c") or ""),
                    thumb_url=str(item.get("t") or ""),
                )
            )
        return results

    def display_name(self, match: RemoteMatch) -> str:
        """展示名：优先 payload 里的原始文件名，缺失时退回 URL 末段。

        **不能**从 `image_url` 反推 UUID5——那条链是死的（id 与 URL 无函数关系）。
        """
        name = (match.file_name or "").strip()
        if name:
            return name
        path = urlparse(match.image_url).path
        return os.path.basename(path) or match.id


def _first_text(payload: dict, *keys: str) -> str:
    """按序取第一个非空字符串字段（服务端 payload 的字段约定并不统一）。"""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""

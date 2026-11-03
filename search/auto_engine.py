"""auto 引擎：向量优先，未命中或引擎不可用时回退哈希。

回退判定集中在这里：调用方只拿最终结果与 stage（vector/hash），不再自己写
「kind == hit / miss / error 该怎么走」的分支。图片只解析一次的责任仍在调用
方——本引擎接收已带 phash/mime 的 QueryImage，不重复下载或解码。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from .base import QueryImage, SearchHit, SearchOutcome
from .hash_engine import HashSearchEngine, HashSearchResult
from .vector_engine import VectorSearchEngine


@dataclass
class AutoSearchResult:
    """auto 一次检索的完整上下文。

    vector 永远有值（向量腿至少会给出 hit/miss/error/nofile）；
    hash 仅在确实执行了回退时才非 None；hash_empty 用于区分「回退后未命中」
    与「压根没有可回退的哈希库」。
    """

    vector: SearchOutcome
    hash: HashSearchResult | None = None
    hash_empty: bool = False


class AutoSearchEngine:
    """向量优先、哈希兜底的组合引擎。"""

    def __init__(
        self,
        vector_engine: VectorSearchEngine,
        hash_engine: HashSearchEngine,
        *,
        hash_pool_size: Callable[[], int],
        want: int,
    ) -> None:
        self._vector = vector_engine
        self._hash = hash_engine
        self._hash_pool_size = hash_pool_size
        self._want = want

    async def search_outcome(self, image: QueryImage) -> AutoSearchResult:
        vector = await self._vector.search_outcome(image)
        if vector.kind in ("hit", "nofile"):
            return AutoSearchResult(vector=vector)
        if self._hash_pool_size() == 0:
            return AutoSearchResult(vector=vector, hash_empty=True)
        hash_result = await asyncio.to_thread(
            self._hash.merged_candidates, image.phash, self._want
        )
        return AutoSearchResult(vector=vector, hash=hash_result)

    async def search(self, image: QueryImage) -> list[SearchHit]:
        """SearchEngine 协议入口：向量命中直接返回，否则返回哈希候选。"""
        result = await self.search_outcome(image)
        if result.vector.kind == "hit":
            return result.vector.hits
        return result.hash.hits if result.hash is not None else []

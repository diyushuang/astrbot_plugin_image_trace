"""哈希检索：本地图库与图床镜像两条腿的合并排序。

两条腿各有偏科：本地图库能给出本地路径参与后续压缩/回传，图床镜像覆盖面大
但只有直链。这里按相似度合并后统一排序（同分本地优先），而不是「本地先查、
没中再查图床」的顺序短路——否则本地一条 0.86 的勉强命中会挡住图床 0.99 的
完美命中。

相似度口径完全复用各自索引的既有实现（`library._POPCOUNT[xor]` 与
`1 - dist / bits`），本模块只做转换、合并与阈值过滤，不另写一套距离计算。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("search.hash_engine")

from .base import QueryImage, SearchHit, SearchOutcome


@dataclass
class HashSearchResult:
    """一次哈希合并检索的结果：达阈值命中 + 未命中时的候选行。"""

    hits: list[SearchHit]
    candidate_lines: list[str]


def _to_hit(match: Any, source: str) -> SearchHit:
    """把 library.MatchResult / hash_index.RemoteMatch 收敛成 SearchHit。"""
    return SearchHit(
        id=match.id,
        score=float(match.similarity),
        url=getattr(match, "image_url", None) or None,
        path=getattr(match, "file_path", None) or None,
        file_name=str(getattr(match, "file_name", "") or ""),
        width=getattr(match, "width", None),
        height=getattr(match, "height", None),
        size_bytes=getattr(match, "file_size", None),
        source=source,
        created_at=str(getattr(match, "created_at", "") or ""),
        note=str(getattr(match, "note", "") or ""),
        raw=match,
    )


class HashSearchEngine:
    """本地图库 + 图床镜像两条腿的哈希检索。"""

    def __init__(
        self,
        library,
        remote_index,
        *,
        threshold: float,
        top_n: int,
        remote_enabled: Callable[[], bool],
    ) -> None:
        self._library = library
        self._remote_index = remote_index
        self._threshold = threshold
        self._top_n = top_n
        self._remote_enabled = remote_enabled

    def merged_candidates(self, phash: str, want: int) -> HashSearchResult:
        """合并两条腿的候选并返回（达阈值命中, 候选行）。

        两条腿各自独立 try：图床那条是纯本地矩阵运算、本地那条要碰 SQLite，
        任一腿出错都不该让整条检索失败。
        """
        merged: list[tuple[float, str, SearchHit]] = []
        try:
            for match in self._library.search(phash, want):
                merged.append((float(match.similarity), "local", _to_hit(match, "local")))
        except Exception as exc:
            logger.warning(f"本地图库哈希检索失败（继续用图床索引）: {exc}")
        if self._remote_enabled() and not self._remote_index.is_empty:
            try:
                for match in self._remote_index.search(phash, want):
                    merged.append((float(match.similarity), "remote", _to_hit(match, "remote")))
            except Exception as exc:
                logger.warning(f"图床哈希索引检索失败（继续用本地图库）: {exc}")

        # 相似度降序；同分时本地靠前（本地能给出路径，回传链路更短）
        merged.sort(key=lambda item: (-item[0], 0 if item[1] == "local" else 1))
        hits = [hit for score, _source, hit in merged if score >= self._threshold]
        candidate_lines = []
        if self._top_n > 0:
            for score, _source, hit in merged[: self._top_n]:
                note = self.candidate_note(hit)
                candidate_lines.append(
                    f"· #{hit.id} 相似度 {score * 100:.1f}%{f'（{note}）' if note else ''}"
                )
        return HashSearchResult(hits=hits, candidate_lines=candidate_lines)

    @staticmethod
    def candidate_note(hit: SearchHit) -> str:
        """候选行的括注：本地腿用 note，图床腿用文件名（note 恒为空）。"""
        note = str(getattr(hit, "note", "") or "").strip()
        if note:
            return note
        return str(getattr(hit, "file_name", "") or "").strip()

    async def search(self, image: QueryImage) -> list[SearchHit]:
        """SearchEngine 协议入口：只返回达阈值命中。"""
        return self.merged_candidates(image.phash, max(1, self._top_n or 1)).hits

    def search_outcome(self, image: QueryImage, want: int | None = None) -> SearchOutcome:
        result = self.merged_candidates(
            image.phash, max(1, want if want is not None else (self._top_n or 1))
        )
        return SearchOutcome(
            kind="hit" if result.hits else "miss",
            hits=result.hits,
            candidate_lines=result.candidate_lines,
        )

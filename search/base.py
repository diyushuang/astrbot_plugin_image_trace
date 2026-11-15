"""检索层公共模型：统一的命中结构、查询载体与引擎协议。

本模块只依赖标准库，不引用 AstrBot / main，便于后续把检索层整体抽成独立库。
哈希腿与向量腿的原始结果（library.MatchResult / hash_index.RemoteMatch /
Qdrant payload dict）在这里收敛成同一种 `SearchHit`，渲染与回传层只认这一种
字段；原始对象保留在 `raw` 里，供少数仍需要额外字段的路径（如向量去重日志）
继续按旧口径读取。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class SearchHit:
    """一条统一后的检索命中。

    字段是两腿的并集：本地腿有 path/note，图床腿有 file_name，向量腿有 url。
    为兼容既有渲染代码，另提供 similarity / image_url / file_path / file_size
    四个只读别名——它们分别指向 score / url / path / size_bytes。
    """

    id: int | str
    score: float
    url: str | None = None
    path: str | None = None
    file_name: str = ""
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    source: str = ""
    created_at: str = ""
    note: str = ""
    raw: Any = None

    @property
    def similarity(self) -> float:
        """旧字段名：相似度分数。"""
        return self.score

    @property
    def image_url(self) -> str | None:
        """旧字段名：公开直链。"""
        return self.url

    @property
    def file_path(self) -> str | None:
        """旧字段名：本地文件路径。"""
        return self.path

    @property
    def file_size(self) -> int | None:
        """旧字段名：入库时记录的原图字节数。"""
        return self.size_bytes


@dataclass
class QueryImage:
    """一次检索的查询图。

    path/mime 供向量腿使用；phash 供哈希腿使用（由同一次解码算出，两腿复用）。
    """

    path: str = ""
    mime: str = ""
    phash: str = ""


@dataclass
class SearchOutcome:
    """带上下文的检索结果（引擎内部与 auto 组合使用）。

    kind: hit / miss / error / nofile。
    """

    kind: str
    hits: list[SearchHit] = field(default_factory=list)
    candidate_lines: list[str] = field(default_factory=list)
    miss_text: str = ""
    error: str = ""


@runtime_checkable
class SearchEngine(Protocol):
    """检索引擎的最小契约：给定查询图，返回统一命中列表。"""

    async def search(self, image: QueryImage) -> list[SearchHit]: ...


def miss_message(fail_line: str, candidates: list, empty_note: str = "") -> str:
    """未命中回复的统一拼装：阈值提示 + 可选的“最接近候选”或空库说明。"""
    if candidates:
        return "\n".join([fail_line, "最接近的候选："] + candidates)
    if empty_note:
        return f"{fail_line}\n{empty_note}"
    return fail_line

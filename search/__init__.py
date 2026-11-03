"""检索层：统一的命中模型与 hash / vector / auto 三个引擎。

main 只保留同名薄委托，不再直接拼两条哈希腿或写 auto 的回退分支。
"""

from __future__ import annotations

from .auto_engine import AutoSearchEngine, AutoSearchResult
from .base import QueryImage, SearchEngine, SearchHit, SearchOutcome, miss_message
from .hash_engine import HashSearchEngine, HashSearchResult
from .vector_engine import VectorSearchEngine

__all__ = [
    "AutoSearchEngine",
    "AutoSearchResult",
    "HashSearchEngine",
    "HashSearchResult",
    "QueryImage",
    "SearchEngine",
    "SearchHit",
    "SearchOutcome",
    "VectorSearchEngine",
    "miss_message",
]

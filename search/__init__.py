"""检索层：统一的命中模型与 hash / vector / auto 三个引擎。

main 只保留同名薄委托，不再直接拼两条哈希腿或写 auto 的回退分支。

这里的导出是**惰性**的：`vector_engine` 会从 `..vector_search` 取
`VectorEngineError`，而 `vector_search` 又要 import 本包下的
`embedding_client` / `qdrant_client`。若在包初始化时立即 import 三个引擎，
就会形成「vector_search → search/__init__ → vector_engine → vector_search」
的循环。惰性导出让 `from .search import X` 仍按需解析，语义不变。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

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

_LAZY_EXPORTS = {
    "AutoSearchEngine": ".auto_engine",
    "AutoSearchResult": ".auto_engine",
    "HashSearchEngine": ".hash_engine",
    "HashSearchResult": ".hash_engine",
    "QueryImage": ".base",
    "SearchEngine": ".base",
    "SearchHit": ".base",
    "SearchOutcome": ".base",
    "VectorSearchEngine": ".vector_engine",
    "miss_message": ".base",
}


def __getattr__(name: str) -> Any:
    """按需从子模块解析导出名（PEP 562），避免包初始化期的循环导入。"""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

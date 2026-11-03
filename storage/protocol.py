"""图片存储接口：登记/删除/直链反解，与具体图床实现解耦。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class StoreResult:
    """一次存储的结果：成功标志、公开直链、本地副本路径与给用户的说明。"""

    ok: bool
    url: str | None = None  # 图床直链（若有）
    file_path: str | None = None  # 本地文件路径（若有）
    message: str = ""


@runtime_checkable
class ImageStore(Protocol):
    """存储实现需要提供的最小能力。

    - store：把本地文件存入当前存储，返回直链/本地路径；
    - delete：按公开直链删除远端对象，URL 不属于当前存储时返回 False；
    - src_path：从公开直链反解床内路径（仅用于向量 payload 的 src 字段）。
    """

    async def store(self, src_path: str) -> StoreResult: ...

    async def delete(self, url: str) -> bool: ...

    def src_path(self, url: str) -> str: ...

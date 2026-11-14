"""存储注册表与兼容适配器。

ImageBedClient 仍是唯一的存储实现（mode 映射保留在 image_bed 内）；本模块
只负责“按配置构造当前生效的存储”并把它适配成 ImageStore 协议，后续新增
存储实现时在这里注册、不必改发送链路。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .protocol import ImageStore, StoreResult

if TYPE_CHECKING:  # 仅类型检查期引用，运行期延迟导入避免与 image_bed 成环
    from ..http_client import GuardedHttpClient
    from ..image_bed import ImageBedClient


class ImageBedStoreAdapter:
    """把 ImageBedClient（store / delete_remote / src_path_for）适配为 ImageStore。"""

    def __init__(self, client: ImageBedClient) -> None:
        self._client = client

    async def store(self, src_path: str) -> StoreResult:
        return await self._client.store(src_path)

    async def delete(self, url: str) -> bool:
        return await self._client.delete_remote(url)

    def src_path(self, url: str) -> str:
        return self._client.src_path_for(url)

    @property
    def local_dir(self) -> str:
        """本地副本目录（仅用于判断「文件是否由插件自己生成」）。"""
        return self._client.local_dir

    @property
    def mode(self) -> str:
        return self._client.mode

    async def close(self) -> None:
        await self._client.close()


class StorageRegistry:
    """持有当前生效的图片存储。

    目前只有图床适配器一种实现；构造参数与旧 `ImageBedClient` 完全一致，
    因此切换过程对调用方零行为差异。
    """

    def __init__(
        self,
        data_dir: str,
        config,
        http_getter: Callable[[], Awaitable[GuardedHttpClient]],
    ) -> None:
        from ..image_bed import ImageBedClient

        self._client = ImageBedClient(data_dir, config, http_getter)
        self.active: ImageStore = ImageBedStoreAdapter(self._client)

    @property
    def local_dir(self) -> str:
        return self._client.local_dir

    async def close(self) -> None:
        await self._client.close()

"""图片存储层：统一的存储接口、按后端拆分的实现与存储注册表。

`storage/` 下的后端只关心「怎么存」：本地副本、通用 HTTP 图床、
CloudFlare-ImgBed、R2/OCI 对象存储；`image_bed.ImageBedClient` 作为薄门面
按 mode 选择后端，并经 ImageBedStoreAdapter 适配到 ImageStore 协议。
"""

from __future__ import annotations

from .cloudflare_backend import CloudflareImgBedBackend
from .http_backend import GenericHttpBackend
from .local_backend import LocalBackend
from .modes import MODE_CFB, MODE_HTTP, MODE_LOCAL, MODE_OCI, MODE_R2
from .object_backend import ObjectBackend
from .protocol import ImageStore, StoreResult
from .registry import ImageBedStoreAdapter, StorageRegistry

__all__ = [
    "ImageStore",
    "StoreResult",
    "ImageBedStoreAdapter",
    "StorageRegistry",
    "LocalBackend",
    "GenericHttpBackend",
    "CloudflareImgBedBackend",
    "ObjectBackend",
    "MODE_LOCAL",
    "MODE_HTTP",
    "MODE_R2",
    "MODE_OCI",
    "MODE_CFB",
]

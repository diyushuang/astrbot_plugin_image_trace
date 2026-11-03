"""图片存储层：统一的存储接口与存储实现注册表。

本批只做接口收口：图床/储存桶的具体上传逻辑仍由 image_bed.ImageBedClient
提供，经 ImageBedStoreAdapter 适配到 ImageStore 协议，后续批次再按存储
实现拆分。
"""

from __future__ import annotations

from .protocol import ImageStore, StoreResult
from .registry import ImageBedStoreAdapter, StorageRegistry

__all__ = ["ImageStore", "StoreResult", "ImageBedStoreAdapter", "StorageRegistry"]

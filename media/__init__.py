"""图片获取层：图片段 → 本地路径/字节、URL 探测，与 AstrBot 事件解耦。"""

from __future__ import annotations

from .image_source import (
    ImageSource,
    ResolvedImage,
    decode_fail_message,
    describe_bad_image,
    download_fail_message,
    image_download_headers,
    range_probe_headers,
)

__all__ = [
    "ImageSource",
    "ResolvedImage",
    "decode_fail_message",
    "describe_bad_image",
    "download_fail_message",
    "image_download_headers",
    "range_probe_headers",
]

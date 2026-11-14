"""存储后端共用的小工具：超时、待上传文件的命名与读取、日志入口。"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import secrets
import time

from ..common import as_int, read_bytes

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("image_bed")


def timeout_seconds(config: dict) -> int:
    """图床 HTTP / 对象存储请求的统一超时秒数（配置缺失或非法时取 30 秒）。"""
    return max(5, as_int(config.get("timeout"), 30))


def new_filename(src_path: str) -> str:
    """按源文件扩展名生成带日期与随机串的上传文件名。"""
    ext = os.path.splitext(src_path)[1].lower() or ".jpg"
    return f"trace_{time.strftime('%Y%m%d')}_{secrets.token_hex(8)}{ext}"


async def read_upload_file(src_path: str) -> tuple[bytes, str, str]:
    """读取待上传文件并生成 (字节, 文件名, Content-Type)，供各上传后端共用。"""
    filename = new_filename(src_path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    file_bytes = await asyncio.to_thread(read_bytes, src_path)
    return file_bytes, filename, content_type

"""第三方转存平台的图片格式体检：嗅探、假格式转码与缩略图替身回退。

格式体检的实现主体；`main` 保留同名薄委托，`DeliveryService` 通过 host 门面
调用，因此既有 monkeypatch 点不变。本模块不反向 import main，random_media
配置由调用方注入。
"""

from __future__ import annotations

import asyncio
import io
import os
import secrets

from astrbot.api import logger

from ..image_delivery import qq_upload_safe_image
from ..random_media import swap_url_origin
from ..url_guard import read_limited_bytes
from .image_source import ImageSource

# 假格式转码前的像素上限：与 features / 签到卡片同一量级，防解压炸弹
QQ_TRANSCODE_MAX_PIXELS = 50_000_000


async def rewrite_blocks(
    host, blocks: list[dict], random_settings: dict
) -> list[dict]:
    """对 blocks 里每个待发 URL 做官方富媒体格式体检，按需改写发送形态。

    体检点放在 _yield_delivery（三条回传路径的汇合处）：标准格式原样
    保留（只花一次 16 字节嗅探）；假格式可解码 → 换成本地 JPEG path；
    不可解码 → 换成缩略图替身 URL。全部体检失败按原样保留——体检是
    增强，不能反过来挡住发送。
    """
    rewritten: list[dict] = []
    for block in blocks:
        url = str(block.get("url") or "")
        if not url or block.get("path"):
            rewritten.append(block)
            continue
        try:
            outcome = await host._offbox_image_delivery(url, random_settings)
        except Exception as exc:
            logger.warning(f"qq_official 图片格式体检异常，按原样交付: {exc}")
            outcome = None
        if outcome is None:
            rewritten.append(block)
            continue
        kind, value = outcome
        new_block = dict(block)
        if kind == "path":
            new_block.pop("url", None)
            new_block["path"] = value
            # 转码产物属于本次投递：由 _yield_delivery 在发送完成后清理
            new_block["_tmp_owned"] = True
        else:
            new_block["url"] = value
        rewritten.append(new_block)
    return rewritten


async def inspect_image_delivery(
    host, url: str, settings: dict
) -> tuple[str, str] | None:
    """qq_official 图片交付体检：返回 (方式, 值)；None 表示按原 URL 交付。

    官方富媒体上传只收标准 JPEG/PNG/GIF；图床里存在扩展名 .jpg、实为
    HEIF/AVIF/WEBP（微博 live 图等）的文件，原样交付必被打回「富媒体文件
    格式不支持」。处置分两级：
    - "path"：PIL 可解码的假格式 → 转码为 JPEG 落盘，本地字节交付；
    - "url"：PIL 解不了的（如未装 pillow-heif 的 HEIC）→ 换发同一点的
      缩略图替身直链（img-indexer 预生成的真 JPEG）。
    文件头能放行、或两级兜底都失败时返回 None（按原样交付——体检是增强，
    不能反过来挡住发送）。
    """
    try:
        client = await host._get_http()
        limit = host._download_limit_bytes()
        async with await client.request(
            "GET", url, timeout=ImageSource._timeout()
        ) as resp:
            if not 200 <= resp.status < 300:
                return None
            head = await resp.content.read(16)
            if qq_upload_safe_image(head):
                return None
            # 嗅探已消费前 16 字节：转码必须用完整字节，把 head 拼回去，
            # 否则 PIL 拿到的是缺了文件头的残缺流，解码必败。
            # 上限按完整响应体算：read_limited_bytes 会拿 Content-Length 与
            # limit 比较，若传 limit-len(head) 会把恰好等于上限的文件误拒。
            rest = await read_limited_bytes(resp, limit)
            data = head + rest
            if len(data) > limit:
                logger.warning(f"qq_official 图片超过体检上限，按原样交付: {len(data)} 字节")
                return None
    except Exception as exc:
        logger.warning(f"qq_official 图片格式体检失败，按原样交付: {exc}")
        return None
    path = await asyncio.to_thread(host._write_transcoded_jpeg, data)
    if path:
        return ("path", path)
    file_id = host._bed_file_id(url)
    if file_id and host.vector.qdrant_ready:
        try:
            thumb = await host.vector.thumb_url_for(file_id)
        except Exception:
            thumb = None
        if thumb:
            lan_hosts = host._lan_url_hosts(settings)
            if lan_hosts:
                # 图片由适配器本机下载，替身直链同样收进内网源
                thumb = swap_url_origin(
                    thumb, str(settings.get("lan_base_url") or ""), lan_hosts
                )
            logger.info(f"qq_official 假格式且不可转码，改发缩略图替身: {file_id}")
            return ("url", thumb)
    return None


def write_transcoded_jpeg(host, data: bytes) -> str | None:
    """把任意可解码图片字节转存为 JPEG 临时文件，返回路径；失败返回 None。"""
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()  # 幂等：让 Image.open 直解 HEIC
    except ImportError:
        pass
    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.warning("PIL 不可用，无法转码非标准格式图片，按原样交付")
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > QQ_TRANSCODE_MAX_PIXELS:
                logger.warning(
                    f"qq_official 待转码图片像素过大，跳过转码: {image.size}"
                )
                return None
            fixed = ImageOps.exif_transpose(image)
            if fixed.mode not in ("RGB", "L"):
                fixed = fixed.convert("RGB")
            path = os.path.join(host.tmp_dir, f"qqsafe_{secrets.token_hex(8)}.jpg")
            fixed.save(path, format="JPEG", quality=92)
        logger.info(f"qq_official 图片格式体检：已转码为 JPEG 交付 {path}")
        return path
    except Exception as exc:
        logger.warning(f"图片转码失败，按原样交付: {exc}")
        return None

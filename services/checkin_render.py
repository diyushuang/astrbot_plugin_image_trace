"""签到卡片渲染：下载图源、合成海报、落临时文件与缩略图替身选择。

渲染流程的实现主体；`main` 保留同名薄委托，`CheckinService` 通过 host 门面
调用，因此既有 monkeypatch 点不变。本模块不反向 import main，random_media
配置由调用方注入。
"""

from __future__ import annotations

import asyncio
import os
import secrets

from astrbot.api import logger

from ..checkin_card import compose_poster
from ..common import read_bytes
from ..random_media import is_thumbnail_url


class CheckinRenderService:
    """签到卡片的合成与图源选择。"""

    def __init__(self, *, host) -> None:
        self.host = host

    @staticmethod
    def write_bytes(path: str, data: bytes) -> None:
        """写临时文件；同步 IO，调用方走 asyncio.to_thread。"""
        with open(path, "wb") as handle:
            handle.write(data)

    async def card_path(
        self,
        image_url: str,
        quote: str,
        footer: str,
        settings: dict,
        *,
        quote_max_lines: int = 3,
    ) -> str | None:
        """下载图片 → 合成卡片 → 落临时文件，返回路径；任一步失败返回 None。

        合成放进 to_thread：Pillow 解码/绘制/编码是 CPU 活，放事件循环里会
        阻塞其它消息处理；实测 720px 卡片整段约 10ms、1440px 约 25ms。
        """
        host = self.host
        try:
            if os.path.isfile(image_url):
                data = await asyncio.to_thread(read_bytes, image_url)
            else:
                data = await host._download_bytes(image_url)
        except Exception as exc:
            logger.warning(f"签到卡片下载图片失败，改用轻量模式: {exc}")
            return None
        image_bytes = await asyncio.to_thread(
            compose_poster,
            data,
            quote=quote,
            footer=footer,
            font_path=settings["font_path"],
            border_px=settings["border_px"],
            quote_max_lines=quote_max_lines,
            show_quote=bool(settings.get("show_quote", True)),
        )
        if not image_bytes:
            return None
        path = os.path.join(host.tmp_dir, f"c_{secrets.token_hex(8)}.jpg")
        try:
            await asyncio.to_thread(self.write_bytes, path, image_bytes)
        except OSError as exc:
            logger.warning(f"签到卡片写入临时文件失败: {exc}")
            return None
        return path

    async def card_source(
        self,
        send_url: str,
        original_url: str,
        settings: dict,
        *,
        random_settings: dict,
    ) -> str:
        """卡片合成专用图源：遵循 checkin.image_mode，与「回传的那张」解耦。

        ``checkin.image_mode`` 决定合成用哪张：
        - ``original``：直接用原图。合成端会先做 JPEG 的 DCT 缩放再降到最长边
          1440px，所以原图不会撑爆内存，只是多花下载流量；
        - ``thumbnail``（默认）：优先缩略图替身。卡片成品只有 ≤1440px，用原图
          合成没有收益——2026-11-02 线上实录：一张 8736×11648 的手机长图被拿来
          合成，既白花几十 MB 流量与上亿像素解码，又被 compose_poster 的防炸
          上限挡回去，整张卡片降级成没有边框的轻量模式。

        注意这里**不看** ``random_media.send_thumbnail``：那个开关管 /随机图、
        /随机视频 发哪张，与签到各管各的，只是共用同一段缩略图反查链路。

        settings 是**签到组**的配置（只取 image_mode）；缩略图替身回来的是公网
        直链，必须再按 random_media 的内网配置换源，所以调用方另注入
        ``random_settings``，不能拿签到配置去算 lan_base_url。
        """
        host = self.host
        source = original_url or send_url
        if settings.get("image_mode") == "original":
            return source
        if is_thumbnail_url(send_url):
            # 上游已经给的就是缩略图直链（图床随机目录指到了 thumbnails/）
            return send_url
        thumb = await host._random_send_url(source, random_settings, thumbnail=True)
        if thumb and thumb != source:
            logger.debug(f"签到卡片改用缩略图合成: {source} -> {thumb}")
        return thumb or source

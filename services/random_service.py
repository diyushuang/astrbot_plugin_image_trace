"""R1 服务层：随机图流程：目录选择、图床直链归一与发送。

服务通过构造注入的 host 门面访问 library / vector / storage / 图床等
依赖，禁止反向 import main；main 中的同名方法只做薄委托。
"""

from __future__ import annotations

from urllib.parse import unquote

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..common import sanitize_error_text
from ..image_delivery import build_original_url, upgrade_to_https
from ..random_media import (
    RandomMediaError,
    clean_display_name,
    is_thumbnail_url,
    media_kind,
    pick_default_directory,
    swap_url_origin,
)


class RandomService:
    def __init__(self, *, host) -> None:
        self.host = host

    async def _random_media_delivery(
        self, event: AstrMessageEvent, directory, content_type: str, settings: dict
    ):
        """取一条随机媒体并回传。

        图片走 _yield_delivery（复用全局通道：auto / bytes / url / url-scaled /
        native），视频走标准消息链 Video.fromURL；无法识别类型时回退纯文本
        直链。同域 http 直链先升级为 https，减少协议端一次跳转。
        """
        # 未指定目录时回退到配置的 default_dir 池：图床若设置了
        # randomImageAPI.allowedDir，不带 dir 的裸 /random 会被判为「目录不允许」
        # 而 403，故由客户端补一个目录；池有多项时随机取其一，以尽量覆盖整库。
        target_dir = str(directory or "").strip() or pick_default_directory(settings)
        try:
            media_url = await self.host._random_client().fetch(target_dir, content_type)
        except RandomMediaError as exc:
            logger.warning(f"获取随机媒体失败: {exc}")
            yield event.plain_result(
                f"⚠️ 获取随机媒体失败：{sanitize_error_text(exc)}"
            )
            return
        lan_hosts = self.host._lan_url_hosts(settings)
        if lan_hosts:
            # 内网直连：床内直链统一改写为内网源（/random 直链可能已是内网源，
            # 改写幂等；公网域名形式的直链也一并收进来），后续取字节不出公网
            media_url = swap_url_origin(media_url, settings["lan_base_url"], lan_hosts)
        media_url = upgrade_to_https(media_url, settings["base_url"])
        target = self.host._delivery_target(event)
        kind = media_kind(media_url, content_type)
        if kind == "video":
            # 官方机器人等平台把视频 URL 交给腾讯服务器拉流，内网地址它访问
            # 不到，须换回公网直链；换不出来（第三方内网地址等）就保持原样，
            # 与图片 URL 上传共用同一套换源判定（_public_media_url）
            send_url = media_url
            if target.offbox_upload:
                send_url = self.host._public_media_url(media_url, settings) or media_url
            caption = self.host._random_caption(media_url, "video", settings)
            yield event.chain_result([Comp.Plain(caption), Comp.Video.fromURL(send_url)])
            return
        if kind == "image":
            send_url, original_url, original_name = await self.host._random_image_from_media(
                media_url, settings
            )
            # 配文一律用**原图**名并清洗掉图床改名的前缀（时间戳 + `@` 换 `_`），
            # 显示缩略图名对用户没有意义
            caption = self.host._random_caption(
                original_url, "image", settings, display_name=original_name
            )
            # qq_official 等平台的假格式体检已下沉到 _yield_delivery 统一处理
            blocks = [
                {
                    "url": send_url,
                    "file_name": original_name or clean_display_name(original_url) or "",
                }
            ]
            async for result in self.host._yield_delivery(
                event,
                caption,
                blocks,
                remember_blocks=(
                    blocks if send_url == original_url else [{"url": original_url}]
                ),
            ):
                yield result
            return
        yield event.plain_result(f"随机媒体获取成功：{media_url}")

    async def _random_image_candidate(
        self, directory, settings: dict, *, thumbnail: bool | None = None
    ) -> tuple[str, str, str]:
        """取一张随机图并归一为 (发送直链, 原图直链, 原图权威名)。

        /签到 与 /随机图 共用这段「取图 → 内网改写/HTTPS 升级 → 缩略图反查 →
        选发送态」链路。抽出来是为了让「发缩略图、历史留原图」的口径只存在
        一份：任何一处改动都不会让两条命令各自漂移。

        thumbnail 只覆盖「这次发哪张」，默认 None 表示沿用
        random_media.send_thumbnail；/签到 传 checkin.image_mode 换算的布尔值，
        因此两个功能同链路、异配置，互不影响。

        失败抛 RandomMediaError，由调用方给出可读提示。
        """
        # 未指定目录时回退到配置的 default_dir 池：图床若设置了
        # randomImageAPI.allowedDir，不带 dir 的裸 /random 会被判为「目录不允许」
        # 而 403，故由客户端补一个目录；池有多项时随机取其一，以尽量覆盖整库。
        target_dir = str(directory or "").strip() or pick_default_directory(settings)
        media_url = await self.host._random_client().fetch(target_dir, "image")
        lan_hosts = self.host._lan_url_hosts(settings)
        if lan_hosts:
            # 内网直连：床内直链统一改写为内网源（/random 直链可能已是内网源，
            # 改写幂等；公网域名形式的直链也一并收进来），后续取字节不出公网
            media_url = swap_url_origin(media_url, settings["lan_base_url"], lan_hosts)
        media_url = upgrade_to_https(media_url, settings["base_url"])
        return await self.host._random_image_from_media(media_url, settings, thumbnail=thumbnail)

    async def _random_image_from_media(
        self, media_url: str, settings: dict, *, thumbnail: bool | None = None
    ) -> tuple[str, str, str]:
        """把随机到的图片直链归一为 (发送直链, 原图直链, 原图权威名)。

        发送用缩略图替身、历史留原图：QQ 侧下载小图更快，sendMsg 回执超时
        概率显著下降；而 /原图 仍能凭历史里的原图直链取回全尺寸文件。

        thumbnail 覆盖「这次发哪张」：None=沿用 random_media.send_thumbnail；
        False 时直接把原图当发送态（/签到 的 original 模式），历史仍记原图。

        边界：图床的 /random 若被指到 thumbnails/ 目录（default_dir 配错或
        用户显式传了该目录），media_url 本身就是缩略图。此时别再查替身
        （缩略图没有替身），并把「原图」口径定为反查结果，避免历史里记进
        缩略图直链、导致 /原图 之后只能拿回小图。
        """
        lan_hosts = self.host._lan_url_hosts(settings)
        original_url = media_url
        original_name = ""
        if is_thumbnail_url(media_url):
            info = None
            try:
                normalized = unquote(build_original_url(media_url))
                info = await self.host.vector.original_info_for_thumb(normalized)
            except Exception as exc:  # 反查失败不影响发送，按原样处理
                logger.debug(f"随机图反查原图失败（按原样处理）: {exc}")
            if info and info.get("url"):
                original_url = info["url"]
                if lan_hosts:
                    # Qdrant 反查回的是公网直链，内网直连模式下同样改写
                    original_url = swap_url_origin(
                        original_url, settings["lan_base_url"], lan_hosts
                    )
                # 真名取自 payload.file_name（图床没改过名的原始名）
                original_name = str(info.get("file_name") or "").strip()
            else:
                # 2026-10-20 排查实录：反查失败的原因有多种——引擎未配置、
                # Qdrant 请求失败、数据未命中——后两者各有 DEBUG 记录（线上
                # 默认 INFO 看不到），而「引擎未配置」历史上完全静默，导致
                # warning 里看不出任何线索、跨实例排查只能逐台实测。把引擎
                # 状态直接写进 warning：未配置 → 给配置指引；缺 embed（此时
                # 反查仍可用）→ 说明只影响以图搜图；配置齐全 → 指向 DEBUG。
                if not self.host.vector.qdrant_ready:
                    hint = "向量引擎未配置，请在插件配置 vector_search 一节补齐"
                elif not self.host.vector.enabled:
                    hint = "向量引擎缺 embed 配置（反查仍可用，仅以图搜图不可用）"
                else:
                    hint = "原因见 DEBUG 日志"
                logger.warning(f"随机图命中的是缩略图且反查不到原图（{hint}）: {media_url}")
        send_url = await self.host._random_send_url(original_url, settings, thumbnail=thumbnail)
        return send_url, original_url, original_name

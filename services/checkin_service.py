"""R1 服务层：每日签到流程：签到记录、取图、文案与卡片合成失败回退。

服务通过构造注入的 host 门面访问 library / vector / storage / 图床等
依赖，禁止反向 import main；main 中的同名方法只做薄委托。
"""

from __future__ import annotations

import asyncio
import random

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..checkin_card import DEFAULT_QUOTES
from ..common import sanitize_error_text
from ..random_media import clean_display_name


class CheckinService:
    def __init__(self, *, host, checkin_settings, random_settings) -> None:
        self.host = host
        # 配置门面注入：读取器由 main 提供，保持既有 monkeypatch 点的语义。
        self.checkin_settings = checkin_settings
        self.random_settings = random_settings

    async def _deliver_checkin(
        self,
        event: AstrMessageEvent,
        header: str,
        quote: str,
        footer: str,
        send_url: str,
        original_url: str,
        block_name: str,
        settings: dict,
        *,
        quote_max_lines: int = 3,
        simple_text: str = "",
        card_source: str = "",
    ):
        """签到统一交付：card 合成失败自动退回 simple；两类签到共用。

        card_source 供微博模式传本地预缓存缩略图路径；它只在
        ``checkin.image_mode=thumbnail`` 时可当合成源（原图模式要求用原图），
        否则按 ``_checkin_card_source`` 现查——合成用小图，不能拿 send_url 兜底
        （它可能是原图）。

        ``checkin.show_quote`` 关掉时（只要美图 + 签到信息）：卡片不画语句区，
        轻量模式的正文也只留 header / simple_text，不再拼 💬 那一行。
        """
        show_quote = bool(settings.get("show_quote", True))
        remember = (
            [{"url": original_url, "file_name": block_name}] if original_url else []
        )
        if settings["render_mode"] == "card":
            source = card_source if settings["image_mode"] == "thumbnail" else ""
            if not source:
                source = await self.host._checkin_card_source(
                    send_url, original_url, settings
                )
            path = await self.host._checkin_card_path(
                source,
                quote if show_quote else "",
                footer,
                settings,
                quote_max_lines=quote_max_lines,
            )
            if path:
                try:
                    async for result in self.host._yield_delivery(
                        event,
                        header,
                        [{"path": path, "file_name": block_name}],
                        # 卡片已是成品 JPEG：交给适配器原样发送，避免二次压缩
                        mode_override="native",
                        allow_local_fallback=False,
                        remember_blocks=remember,
                    ):
                        yield result
                finally:
                    self.host._remove_quiet(path)
                return
            logger.warning("签到卡片合成失败，改用轻量模式")
        text = simple_text or header
        if show_quote and quote:
            text = f"{text}\n💬 {quote}"
        async for result in self.host._yield_delivery(
            event,
            text,
            [{"url": send_url, "file_name": block_name}],
            remember_blocks=remember,
        ):
            yield result

    async def _checkin_flow(self, event: AstrMessageEvent):
        """签到主流程：判定 → 取图 → 取句 → 卡片/轻量交付。

        签到记录先落库、再取图：取图失败不回收当天资格，避免接口抖动时用户
        反复重试刷图；提示文案会明确说明「已签到但取图失败」。同一天重复签到
        不再累加天数，但仍重新取图取句，让每次都有新内容。
        """
        settings = self.checkin_settings()
        if not settings["enabled"]:
            yield event.plain_result("每日签到功能已在插件配置中关闭。")
            return
        if self.host.checkin_store is None:
            yield event.plain_result("签到存储不可用（数据库初始化失败），详情请查看机器人日志。")
            return
        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("无法识别发送者，签到失败。")
            return

        today = self.host._checkin_today(settings["utc_offset_hours"])
        try:
            outcome = await asyncio.to_thread(self.host.checkin_store.check_in, user_id, today)
        except Exception as exc:
            logger.error(f"签到记录写入失败: {exc}")
            yield event.plain_result("签到记录写入失败，请稍后再试。")
            return
        # 昵称（checkin.show_sender）同时进消息文字与卡片页脚；取不到就整行省略。
        name = self.host._checkin_sender_name(event) if settings["show_sender"] else ""
        who = f"{name} " if name else ""
        if outcome.already:
            header = (
                f"📅 {who}今日已签到 · 连续 {outcome.streak} 天"
                f"（累计 {outcome.total} 天）"
            )
        else:
            header = (
                f"✅ {who}签到成功 · 连续 {outcome.streak} 天"
                f"（累计 {outcome.total} 天）"
            )

        random_settings = self.random_settings()

        # 微博模式：映射表里已有「文案 → 图库图片」，签到只读本地 JSON + 内网缩略图。
        # 任何一步失败都不把异常抛回事件管线，直接继续走下面的随机语句回退。
        if settings["mode"] == "weibo":
            try:
                candidate = await self.host._weibo_candidate(
                    random_settings, image_mode=settings["image_mode"]
                )
            except Exception as exc:
                logger.warning(f"微博签到取图失败，回退随机语句: {exc}")
                candidate = None
            if candidate is not None:
                post, send_url, original_url, image_name, local_thumb = candidate
                # show_quote=False 时整段文案都不进卡片/消息，只留「美图 + 签到信息」
                quote = post.text if settings["show_quote"] else ""
                footer = (
                    f"{post.account_display} · {post.date_display}\n"
                    f"{self.host._checkin_line(name, outcome)}"
                )
                block_name = (
                    clean_display_name(image_name)
                    or image_name
                    or clean_display_name(original_url)
                    or "今日签到"
                )
                simple_text = (
                    f"{header}\n📝 {post.account_display} · {post.date_display}"
                )
                async for result in self.host._deliver_checkin(
                    event,
                    header,
                    quote,
                    footer,
                    send_url,
                    original_url,
                    block_name,
                    settings,
                    quote_max_lines=5,
                    simple_text=simple_text,
                    card_source=local_thumb,
                ):
                    yield result
                return
            logger.debug("微博文案暂无可配图片，回退随机图 + 随机语句")

        try:
            # 签到的取图口径来自 checkin.image_mode，与 random_media.send_thumbnail
            # 各管各的：这里显式传布尔值，/随机图 那条仍走配置默认。
            send_url, original_url, original_name = await self.host._random_image_candidate(
                "", random_settings, thumbnail=settings["image_mode"] != "original"
            )
        except Exception as exc:
            # RandomMediaError 之外还可能有网络/解析等未预期异常：签到不能因此
            # 把异常抛回 AstrBot 事件管线，统一降级为可读提示（URL/内网地址脱敏）
            logger.warning(f"签到取图失败: {exc}")
            safe_exc = sanitize_error_text(exc)
            if outcome.already:
                yield event.plain_result(
                    f"📅 今天已经签到过（连续 {outcome.streak} 天），但取图失败：{safe_exc}"
                )
            else:
                yield event.plain_result(
                    f"✅ 签到成功（连续 {outcome.streak} 天），但取图失败：{safe_exc}"
                )
            return

        if settings["show_quote"]:
            try:
                quote = await self.host._quote_library().pick()
            except Exception as exc:
                # 语句文件损坏/非 UTF-8/接口构造失败都不该让签到整体失败
                logger.warning(f"签到语句获取失败，使用内置语句: {exc}")
                quote = random.choice(DEFAULT_QUOTES)
        else:
            # 关掉文字时不取句：省掉一次在线接口请求，卡片也没有文字区可画
            quote = ""
        footer = self.host._checkin_line(name, outcome)
        block_name = original_name or clean_display_name(original_url) or "今日签到"
        async for result in self.host._deliver_checkin(
            event,
            header,
            quote,
            footer,
            send_url,
            original_url,
            block_name,
            settings,
            quote_max_lines=3,
        ):
            yield result

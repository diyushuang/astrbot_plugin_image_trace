"""发送通道实现：OneBot、QQ 官方上传、标准消息链。

通道只拿 `DeliveryContext`（请求 + 计划 + 已算好的 URL 计划），负责「走哪条
通道、按什么顺序、结果怎么记账」；字节级发送原语仍由插件实例提供
（_send_via_onebot / _local_compress_delivery / _prepare_delivery_blocks /
_qq_upload_takeover），本批不搬这些底层实现。

已真正搬到本模块的实现：`_qq_post_media` → `QQOfficialChannel.post_media`，
`_offbox_pipeline` 的编排 → `QQOfficialChannel.run_offbox`；两个旧方法在
main 里保留同名薄委托，签名不变。
"""

from __future__ import annotations

import random

import astrbot.api.message_components as Comp

try:
    from ..image_delivery import CompressionEvidence, SendOutcome
except ImportError:  # 兼容插件以独立模块方式加载
    from image_delivery import CompressionEvidence, SendOutcome  # type: ignore[no-redef]

from .types import DeliveryContext, DeliveryResult


def _outcome_name(outcome, *, unknown: str = "unknown") -> str:
    if outcome is SendOutcome.FAILED:
        return "failed"
    if outcome is SendOutcome.UNKNOWN:
        return unknown
    return "sent"


class OneBotChannel:
    """OneBot 原生直发：URL 或压缩字节，均绕开协议端二次下载。"""

    name = "onebot"

    def __init__(self, host) -> None:
        self._host = host

    def can_handle(self, plan) -> bool:
        return bool(plan.direct_ok or plan.local_compress)

    async def deliver_url(self, ctx: DeliveryContext) -> DeliveryResult:
        host = self._host
        urls, evidences = ctx.url_plan
        if ctx.request.mark_compressed:
            host._mark_compressed_captions(ctx.request.blocks, evidences)
        outcome = await host._send_via_onebot(
            ctx.event, ctx.request.header, ctx.request.blocks, urls
        )
        name = _outcome_name(outcome)
        return DeliveryResult(
            outcome=name,
            sent_count=1 if name != "failed" else 0,
            evidence=list(evidences),
            fallback_reason="" if name != "failed" else "onebot_url_send_failed",
        )

    async def deliver_compressed(self, ctx: DeliveryContext) -> DeliveryResult:
        host = self._host
        outcome = await host._local_compress_delivery(
            ctx.event,
            ctx.request.header,
            ctx.request.blocks,
            ctx.image_blocks,
            mark_compressed=ctx.request.mark_compressed,
        )
        if outcome is None:
            return DeliveryResult(
                outcome="failed", fallback_reason="onebot_prepare_failed"
            )
        name = _outcome_name(outcome)
        return DeliveryResult(
            outcome=name,
            sent_count=1 if name != "failed" else 0,
            fallback_reason="" if name != "failed" else "onebot_bytes_send_failed",
        )


class QQOfficialChannel:
    """QQ 官方富媒体通道：格式体检 → 上传接管 → 自建 media 消息。"""

    name = "qq_official"

    def __init__(self, host) -> None:
        self._host = host

    def can_handle(self, plan) -> bool:
        return bool(plan.run_offbox)

    async def run_offbox(self, ctx: DeliveryContext) -> tuple[list[dict], bool]:
        """先改写信封（可能换成本地转码 path），再由上传通道决定 URL/分片。"""
        host = self._host
        blocks = await host._offbox_rewrite_blocks(ctx.request.blocks)
        handled = await host._qq_upload_takeover(
            ctx.event,
            ctx.request.header,
            blocks,
            mark_compressed=ctx.request.mark_compressed,
        )
        return blocks, handled

    async def post_media(self, event, target, content: str, media) -> None:
        """发送一条 msg_type=7 的 media 消息（实现自 main._qq_post_media）。"""
        api = event.bot.api
        message_id = str(
            getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        ).strip()
        payload = {
            "msg_type": 7,
            "media": media,
            "content": content or None,
            "msg_id": message_id or None,
            "msg_seq": random.randint(1, 10000),
        }
        kind, session_id = target
        if kind == "group":
            await api.post_group_message(group_openid=session_id, **payload)
        else:
            await api.post_c2c_message(openid=session_id, **payload)


class ChainChannel:
    """标准消息链：本地字节链（可压缩）或 URL 链兜底。"""

    name = "chain"

    def __init__(self, host) -> None:
        self._host = host

    def can_handle(self, plan) -> bool:
        return bool(plan.local_bytes_chain or plan.stages)

    async def deliver_bytes(self, ctx: DeliveryContext) -> DeliveryResult:
        host = self._host
        prepared = await host._prepare_delivery_blocks(
            ctx.image_blocks, compress=ctx.plan.prepare_compress
        )
        if prepared is None:
            return DeliveryResult(
                outcome="failed", fallback_reason="local_prepare_failed"
            )
        evidences = [
            CompressionEvidence.COMPRESSED if compressed else CompressionEvidence.ORIGINAL
            for _, compressed in prepared
        ]
        if ctx.request.mark_compressed:
            host._mark_compressed_captions(ctx.request.blocks, evidences)
        fallback_header = ctx.request.header
        if any(item is CompressionEvidence.COMPRESSED for item in evidences):
            fallback_header += "（已压缩）"
        chain = host._mention_prefix(ctx.event)
        if fallback_header:
            chain.append(Comp.Plain((" " if chain else "") + fallback_header))
        image_index = 0
        for block in ctx.request.blocks:
            if block.get("text"):
                chain.append(Comp.Plain("\n" + str(block["text"])))
            if block.get("url") or block.get("path"):
                data, _ = prepared[image_index]
                chain.append(Comp.Image.fromBytes(data))
                image_index += 1
        return DeliveryResult(
            outcome="sent", sent_count=1, evidence=evidences, chain=chain
        )

    async def deliver_url(self, ctx: DeliveryContext) -> DeliveryResult:
        host = self._host
        urls, evidences = ctx.url_plan
        if ctx.request.mark_compressed:
            host._mark_compressed_captions(ctx.request.blocks, evidences)
        chain = host._mention_prefix(ctx.event)
        if ctx.request.header:
            chain.append(
                Comp.Plain((" " if chain else "") + ctx.request.header)
            )
        # urls / evidences 与 image_blocks 同序，而本循环遍历全部 blocks
        # （含纯文本 block）：计数只按「有图 block」推进，避免 path 图让 URL 错位。
        image_index = 0
        for block in ctx.request.blocks:
            if block.get("text"):
                chain.append(Comp.Plain("\n" + str(block["text"])))
            path = str(block.get("path") or "")
            if block.get("url"):
                chain.append(Comp.Image.fromURL(urls[image_index]))
            elif path:
                chain.append(Comp.Image.fromFileSystem(path))
            if block.get("url") or path:
                image_index += 1
        return DeliveryResult(
            outcome="sent", sent_count=1, evidence=evidences, chain=chain
        )

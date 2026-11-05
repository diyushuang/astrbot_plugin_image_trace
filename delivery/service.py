"""投递编排服务：去重 → 建请求 → offbox 前置 → 计划 → 选通道 → 执行 → 记账。

第三轮从 ``main._yield_delivery`` 整段下沉。服务只接收 host 门面（插件实例），
通过同名方法访问既有的发送原语；这些方法在 main 里仍是薄委托，因此测试对
``plugin._offbox_pipeline`` / ``_delivery_config`` / ``_url_delivery_plan`` 等
的 monkeypatch 仍然生效。

对外暴露 `async generator`：需要交给 respond stage 的消息链由 ``deliver`` 逐个
yield；直发 / 上传接管成功时没有可 yield 的内容。

五条发送不变量在此集中体现：

1. 同一事件只投递一次（``EventGuard`` 入口去重）；
2. ``UNKNOWN`` / 超时按已送达，绝不重发；
3. 只有通道明确返回 ``failed`` 才换通道回退；
4. 「已压缩」配文只按通道实测证据写入（本服务不自行改写）；
5. QQ 官方上传接管已发过图片时不再交回（由接管自身保证）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .channels import ChainChannel, OneBotChannel
from .planner import plan_delivery
from .types import (
    DeliveryConfig,
    DeliveryContext,
    DeliveryProfile,
    DeliveryRequest,
)


@dataclass(frozen=True)
class DeliveryHooks:
    """入口一次性收集的 host 门面调用点。

    原先编织逻辑在分支里零散回取 ``host._xxx``；集中成显式钩子后，服务对 main
    的依赖只有这一张清单。钩子在 ``deliver`` 入口读取（晚于测试的 monkeypatch），
    因此 patch 点全部继续生效。
    """

    target: object
    remember_blocks: object
    offbox_pipeline: object
    url_delivery_plan: object
    remove_quiet: object
    already_sent: object
    mark_sent: object
    defer_stop: object


class EventGuard:
    """事件级记账：去重、标记已发送、按需 stop_event。

    原先这些判断散落在 ``_yield_delivery`` 的各个分支里；集中到 guard 后，
    「哪些分支算送达」一眼可见。``defer_stop`` 由调用方（多图溯源等）提前设置，
    此时只记账不 stop，避免管道在首图后中断当前处理器。
    """

    def __init__(self, event: AstrMessageEvent, hooks: DeliveryHooks) -> None:
        self._event = event
        self._hooks = hooks

    def already_sent(self) -> bool:
        return bool(self._hooks.already_sent(self._event))

    def mark_sent(self) -> None:
        self._hooks.mark_sent(self._event)

    def finish_direct(self) -> None:
        """直发 / 上传接管已送达：记账并按需 stop_event。"""
        self.mark_sent()
        if not self._hooks.defer_stop(self._event):
            self._event.stop_event()


class DeliveryService:
    """统一回传编排；host 为插件实例门面。"""

    def __init__(self, host, delivery_config) -> None:
        self.host = host
        # 配置门面注入：main 传入的读取器仍指向 main 薄委托，因此既有
        # monkeypatch(plugin._delivery_config) 的用例继续生效。
        self.delivery_config = delivery_config

    def _hooks(self) -> DeliveryHooks:
        """在服务入口一次性读取 host 上的全部依赖点。"""
        host = self.host
        return DeliveryHooks(
            target=host._delivery_target,
            remember_blocks=host._remember_blocks,
            offbox_pipeline=host._offbox_pipeline,
            url_delivery_plan=host._url_delivery_plan,
            remove_quiet=host._remove_quiet,
            already_sent=host._event_already_sent,
            mark_sent=host._mark_event_sent,
            defer_stop=host._defer_event_stop,
        )

    async def deliver(
        self,
        event: AstrMessageEvent,
        header: str,
        blocks: list[dict],
        *,
        mode_override: str | None = None,
        allow_local_fallback: bool = True,
        mark_compressed: bool = False,
        remember_blocks: list[dict] | None = None,
    ) -> AsyncGenerator:
        """按通道选主路径，失败后逐级回退；语义与旧 ``_yield_delivery`` 一致。"""
        host = self.host
        hooks = self._hooks()
        guard = EventGuard(event, hooks)
        if guard.already_sent():
            logger.warning("本次事件已投递过图片，跳过重复投递")
            return

        target = hooks.target(event)
        hooks.remember_blocks(
            event, blocks if remember_blocks is None else remember_blocks
        )
        request = DeliveryRequest(
            header=header,
            blocks=blocks,
            mode_override=mode_override,
            allow_local_fallback=allow_local_fallback,
            mark_compressed=mark_compressed,
            remember_blocks=remember_blocks,
        )
        # 格式体检可能把假格式转成本地 JPEG（block["_tmp_owned"]）；这些临时文件
        # 只服务于本次投递，发送完成后必须删除，否则会随每次 /随机图、/原图 累积。
        owned_paths: list[str] = []
        try:
            if target.offbox_upload:
                # 第三方转存平台的两个前置动作（格式体检 → 上传通道接管）收在一个
                # 入口里：前者可能把假格式换成 path/缩略图 URL，后者必须拿着改写后
                # 的 block 才能判断该走 URL 还是分片，顺序不可交换
                blocks, handled = await hooks.offbox_pipeline(
                    event, header, blocks, mark_compressed=mark_compressed
                )
                request.blocks = blocks
                owned_paths = [
                    str(block.get("path") or "")
                    for block in blocks
                    if block.get("_tmp_owned") and block.get("path")
                ]
                if handled:
                    # 通道接管已自行发完消息：必须标记已发送并 stop_event，否则
                    # respond stage 会照原路（base64 直传）再发一遍
                    guard.finish_direct()
                    return

            mode, max_side, quality, scaled_url_style = self.delivery_config()
            if mode_override is not None:
                mode = mode_override
            # 决策收口到纯计划器：输入是请求/平台画像/配置，输出是候选步骤。
            plan = plan_delivery(
                request,
                DeliveryProfile.from_target(target),
                DeliveryConfig(
                    mode=mode,
                    max_side=max_side,
                    quality=quality,
                    scaled_url_style=scaled_url_style,
                ),
            )
            mode, max_side = plan.mode, plan.max_side
            quality, scaled_url_style = plan.quality, plan.scaled_url_style
            image_blocks = [
                block for block in blocks if block.get("url") or block.get("path")
            ]

            # 回传计划最多只算一次：回退路径此前会重跑一遍规划，把同样的探测白做
            # 第二次（图床还会因此多做一次缩放处理）。
            url_plan: tuple[list[str], list] | None = None
            ctx = DeliveryContext(
                event=event, request=request, plan=plan, image_blocks=image_blocks
            )
            if plan.direct_ok:
                url_plan = await hooks.url_delivery_plan(
                    image_blocks,
                    max_side,
                    mode,
                    quality=quality,
                    scaled_url_style=scaled_url_style,
                )
                ctx.url_plan = url_plan
                result = await OneBotChannel(host).deliver_url(ctx)
                if result.outcome != "failed":
                    # SENT 已送达；UNKNOWN 可能已送达——两者都不能再发第二次
                    guard.finish_direct()
                    return

            if plan.local_compress:
                # OneBot 下 auto/bytes 直发压缩字节（混合载荷）：压缩结果
                # 经 base64 段直达 QQ，不经协议端二次下载，压缩 100% 生效
                result = await OneBotChannel(host).deliver_compressed(ctx)
                if result.outcome != "failed":
                    # SENT 已送达；UNKNOWN 可能已送达——两者都不能再发第二次
                    guard.finish_direct()
                    return
                # 准备失败 或 直发失败：字节路径已试过，整批改走 URL 链
                logger.warning("OneBot 压缩直发未成功，改用标准消息链 URL 发送")
            elif plan.local_bytes_chain:
                result = await ChainChannel(host).deliver_bytes(ctx)
                if result.outcome != "failed":
                    guard.mark_sent()
                    yield event.chain_result(result.chain)
                    return
                logger.warning("本地图片回退失败，改用标准消息链 URL 发送")

            # 走到这里说明没有任何通道明确送达（UNKNOWN 已在上面按已送达拦截）。
            if guard.already_sent():
                return
            if url_plan is None:
                url_plan = await hooks.url_delivery_plan(
                    image_blocks,
                    max_side,
                    mode,
                    quality=quality,
                    scaled_url_style=scaled_url_style,
                )
            ctx.url_plan = url_plan
            result = await ChainChannel(host).deliver_url(ctx)
            if result.outcome == "failed":
                return
            guard.mark_sent()
            yield event.chain_result(result.chain)
        finally:
            # 生成器被消费完/关闭时执行：此时消息已由 OneBot 直发或由 respond
            # stage 发完，删除转码产物是安全的
            for owned in owned_paths:
                hooks.remove_quiet(owned)

"""发送域公共类型：请求、结果、通道协议与平台画像。

这一层只描述「要发什么、发到哪、结果如何」，不碰 event / bot / 网络；
main 负责把 AstrBot 事件适配成 `DeliveryRequest`，通道实现负责真正出网。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeliveryRequest:
    """一次回传请求（同一次 _yield_delivery 调用）。"""

    header: str
    blocks: list[dict]
    mode_override: str | None = None
    allow_local_fallback: bool = True
    mark_compressed: bool = False


@dataclass
class DeliveryResult:
    """通道执行结果。

    outcome 沿用三态词表：sent / unknown / failed；额外 skipped 表示本批
    无需发送（例如没有图片且链里也没有文字）。
    fallback_reason 仅当 outcome == failed 且调用方允许换通道时填写。

    sent_count 是「批量投递次数」而非图片张数：OneBot 一条消息可以携带多图，
    通道无法在发送前逐图确认到达，故送达时记为 1、失败为 0。现有调用方都未
    读取该值；保持批量语义，避免为语义漂亮引入不可验证的逐图计数。
    """

    outcome: str = "failed"
    sent_count: int = 0
    fallback_reason: str = ""
    chain: list[Any] | None = None


@dataclass(frozen=True)
class DeliveryProfile:
    """平台能力画像，由 image_delivery.DeliveryTarget 转换而来。"""

    name: str = ""
    direct_send: bool = False
    offbox_upload: bool = False

    @classmethod
    def from_target(cls, target) -> DeliveryProfile:
        return cls(
            name=str(getattr(target, "name", "") or ""),
            direct_send=bool(getattr(target, "direct_send", False)),
            offbox_upload=bool(getattr(target, "offbox_upload", False)),
        )


@dataclass(frozen=True)
class DeliveryConfig:
    """一次发送实际采用的通道配置（已包含 mode_override）。"""

    mode: str
    max_side: int
    quality: int
    scaled_url_style: str


@dataclass
class DeliveryContext:
    """通道执行上下文；event 保持 Any，避免发送域依赖 AstrBot 类型。"""

    event: Any
    request: DeliveryRequest
    plan: Any
    image_blocks: list[dict] = field(default_factory=list)
    url_plan: Any = None

    async def deliver(self, context: DeliveryContext) -> DeliveryResult: ...

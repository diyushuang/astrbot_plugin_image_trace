"""发送域公共类型：请求、结果、通道协议与平台画像。

这一层只描述「要发什么、发到哪、结果如何」，不碰 event / bot / 网络；
main 负责把 AstrBot 事件适配成 `DeliveryRequest`，通道实现负责真正出网。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class DeliveryRequest:
    """一次回传请求（同一次 _yield_delivery 调用）。"""

    header: str
    blocks: list[dict]
    mode_override: str | None = None
    allow_local_fallback: bool = True
    mark_compressed: bool = False
    remember_blocks: list[dict] | None = None


@dataclass
class DeliveryResult:
    """通道执行结果。

    outcome 沿用三态词表：sent / unknown / failed；额外 skipped 表示本批
    无需发送（例如没有图片且链里也没有文字）。
    fallback_reason 仅当 outcome == failed 且调用方允许换通道时填写。
    """

    outcome: str = "failed"
    sent_count: int = 0
    evidence: list[Any] = field(default_factory=list)
    fallback_reason: str = ""


@dataclass(frozen=True)
class DeliveryProfile:
    """平台能力画像，由 image_delivery.DeliveryTarget 转换而来。"""

    name: str = ""
    direct_send: bool = False
    offbox_upload: bool = False
    native_mention: bool = False

    @classmethod
    def from_target(cls, target) -> DeliveryProfile:
        return cls(
            name=str(getattr(target, "name", "") or ""),
            direct_send=bool(getattr(target, "direct_send", False)),
            offbox_upload=bool(getattr(target, "offbox_upload", False)),
            native_mention=bool(getattr(target, "native_mention", False)),
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


@runtime_checkable
class DeliveryChannel(Protocol):
    """发送通道：先判定能否处理某份计划，再执行。"""

    name: str

    def can_handle(self, plan) -> bool: ...

    async def deliver(self, context: DeliveryContext) -> DeliveryResult: ...

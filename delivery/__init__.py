"""发送域：请求/结果类型、纯计划器与（后续批次）通道实现。"""

from __future__ import annotations

from .planner import DeliveryPlan, plan_delivery
from .types import (
    DeliveryChannel,
    DeliveryConfig,
    DeliveryContext,
    DeliveryProfile,
    DeliveryRequest,
    DeliveryResult,
)

__all__ = [
    "DeliveryChannel",
    "DeliveryConfig",
    "DeliveryContext",
    "DeliveryPlan",
    "DeliveryProfile",
    "DeliveryRequest",
    "DeliveryResult",
    "plan_delivery",
]

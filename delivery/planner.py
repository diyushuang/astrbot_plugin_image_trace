"""发送计划器：纯函数，把请求 + 平台画像 + 配置折算成回传步骤。

计划器不读配置单例、不碰 event、不出网；它只回答「这单该按什么顺序尝试
哪些通道」。执行与失败后的实际回退仍由调用方按计划逐段跑，因此 Step 4a
可以先把判定收口，而完全不改发送行为。

计划只描述**候选步骤**，不预判运行结果：
- onebot_url：OneBot 原生 URL 直发；
- onebot_bytes：OneBot 原生压缩字节直发（混合载荷）；
- chain_bytes：本地准备字节后走标准消息链；
- url_chain：最后的标准消息链 URL 兜底；
- offbox：第三方转存平台的前置体检 + 上传接管。
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    from ..image_delivery import is_napcat_parseable_url
except ImportError:  # 兼容独立模块加载
    from image_delivery import is_napcat_parseable_url  # type: ignore[no-redef]

from .types import DeliveryConfig, DeliveryProfile, DeliveryRequest

DIRECT_MODES = frozenset({"url", "url-scaled"})


@dataclass(frozen=True)
class DeliveryPlan:
    """一次回传的纯判定结果。"""

    mode: str
    max_side: int
    quality: int
    scaled_url_style: str
    run_offbox: bool = False
    direct_ok: bool = False
    allow_local_fallback: bool = True
    local_compress: bool = False
    local_bytes_chain: bool = False
    prepare_compress: bool = True
    needs_url_plan: bool = True
    stages: tuple[str, ...] = ()

    @property
    def direct_mode(self) -> bool:
        return self.mode in DIRECT_MODES


def plan_delivery(
    request: DeliveryRequest,
    profile: DeliveryProfile,
    config: DeliveryConfig,
) -> DeliveryPlan:
    """纯计划器：所有判定都来自入参，不产生副作用。"""
    mode = config.mode
    image_blocks = [b for b in request.blocks if b.get("url") or b.get("path")]
    raw_urls = [str(b["url"]) for b in image_blocks if b.get("url")]
    all_urls = bool(image_blocks) and len(raw_urls) == len(image_blocks)
    direct_mode = mode in DIRECT_MODES

    # URL 直发条件：模式允许 + 全是 URL + 能走 OneBot + 扩展名能被协议端解析
    direct_ok = (
        direct_mode
        and all_urls
        and profile.direct_send
        and (mode == "url" or all(is_napcat_parseable_url(u) for u in raw_urls))
    )

    should_compress = mode in {"auto", "bytes"} or (
        mode == "url-scaled" and any(b.get("path") for b in image_blocks)
    )
    # 走 URL 直传但没能直发成功：既包括 FAILED 后的回退，也包括扩展名让协议端
    # 解析不了宽高、压根没尝试直发的情形
    onebot_failed = profile.direct_send and direct_mode and all_urls
    local_attempt = request.allow_local_fallback and (should_compress or onebot_failed)
    local_compress = local_attempt and mode in {"auto", "bytes"} and profile.direct_send
    local_bytes_chain = local_attempt and not (mode in {"auto", "bytes"} and profile.direct_send)

    stages: list[str] = []
    if profile.offbox_upload:
        stages.append("offbox")
    if direct_ok:
        stages.append("onebot_url")
    if local_compress:
        stages.append("onebot_bytes")
    if local_bytes_chain:
        stages.append("chain_bytes")
    stages.append("url_chain")

    return DeliveryPlan(
        mode=mode,
        max_side=config.max_side,
        quality=config.quality,
        scaled_url_style=config.scaled_url_style,
        run_offbox=profile.offbox_upload,
        direct_ok=direct_ok,
        allow_local_fallback=request.allow_local_fallback,
        local_compress=local_compress,
        local_bytes_chain=local_bytes_chain,
        prepare_compress=mode != "url",
        needs_url_plan=True,
        stages=tuple(stages),
    )

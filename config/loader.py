"""配置读取门面：归一化、回落与默认值。

main.py 只保留同名薄委托，真正的读取规则集中在这里。两条硬约束：
- 新布局（basic / limits）优先，旧顶层键回落——迁移被绕过时旧值仍生效；
- 默认值全部取自 config/typed.py，调用点不再各写一份。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ..common import as_float, as_int, bool_or, is_blank, truthy
from ..image_delivery import bounded_int, delivery_settings, qq_upload_settings
from ..random_media import RANDOM_ENDPOINT_DEFAULT
from .migrations import (
    BUCKET_MODES,
    BUCKET_REQUIRED,
    NAPCAT_KEY_MAP,
    QQ_KEY_MAP,
    SCALAR_PATHS,
    ensure_group,
)
from .typed import (
    DEFAULT_BASIC,
    DEFAULT_CHECKIN,
    DEFAULT_DELIVERY,
    DEFAULT_RANDOM,
    DEFAULT_VECTOR,
)


def logger_warning(message: str) -> None:
    """延迟导入 AstrBot logger，避免本模块在插件加载前就硬依赖 astrbot。"""
    try:
        from astrbot.api import logger
    except Exception:  # 独立加载/单元测试环境
        import logging

        logger = logging.getLogger("astrbot")
    logger.warning(message)


def dict_group(config: Any, key: str) -> dict:
    """取对象型配置；手工改坏配置（非 dict）时退化为空 dict 而不是抛错。"""
    value = config.get(key) if hasattr(config, "get") else None
    return value if isinstance(value, dict) else {}


def ensure_group_cfg(config: Any, name: str) -> dict:
    return ensure_group(config, name)


def scalar_cfg(config: Any, key: str) -> Any:
    """读标量配置：新布局（basic / limits）优先，回落旧顶层键。"""
    group, field = SCALAR_PATHS.get(key, ("", key))
    if group:
        value = dict_group(config, group).get(field)
        if not is_blank(value):
            return value
    return config.get(key)


def int_cfg(config: Any, key: str, default: int) -> int:
    return as_int(scalar_cfg(config, key), default)


def float_cfg(config: Any, key: str, default: float) -> float:
    return as_float(scalar_cfg(config, key), default)


def bool_cfg(config: Any, key: str, default: bool) -> bool:
    return bool_or(scalar_cfg(config, key), default)


def first_value(*values: Any) -> Any:
    """取第一个非空值（0 / False 是合法配置，不能按假值跳过）。"""
    for value in values:
        if not is_blank(value):
            return value
    return None


def napcat_delivery_cfg(config: Any) -> dict:
    """推送通道（NapCat）配置，键名沿用 delivery_settings 的入参约定。"""
    new = dict_group(config, "delivery")
    old = dict_group(config, "image_delivery")
    return {
        "mode": first_value(new.get("napcat_mode"), old.get("mode")),
        "max_side": first_value(new.get("napcat_max_side"), old.get("max_side")),
        "quality": first_value(new.get("napcat_quality"), old.get("quality")),
        "scaled_url_style": first_value(
            new.get("napcat_scaled_url_style"), old.get("scaled_url_style")
        ),
        "verify_scaled": first_value(
            new.get("napcat_verify_scaled"), old.get("verify_scaled")
        ),
        "target_kb": first_value(new.get("napcat_target_kb"), old.get("target_kb")),
        "webp": first_value(new.get("napcat_webp"), old.get("webp")),
        "onebot_image_timeout": first_value(
            new.get("napcat_timeout"), old.get("onebot_image_timeout")
        ),
    }


def qq_delivery_cfg(config: Any) -> dict:
    """推送通道（QQ 官方）配置，键名沿用 qq_upload_settings 的入参约定。"""
    new = dict_group(config, "delivery")
    old = dict_group(config, "qq_upload")
    return {
        "mode": first_value(new.get("qq_mode"), old.get("mode")),
        "direct_max_mb": first_value(new.get("qq_native_max_mb"), old.get("direct_max_mb")),
        "auto_prefer": first_value(new.get("qq_auto_prefer"), old.get("auto_prefer")),
        "url_max_mb": first_value(new.get("qq_url_max_mb"), old.get("url_max_mb")),
        "max_upload_mb": first_value(new.get("qq_max_upload_mb"), old.get("max_upload_mb")),
        "on_failure": first_value(new.get("qq_on_failure"), old.get("on_failure")),
    }


def delivery_config(config: Any) -> tuple[str, int, int, str]:
    return delivery_settings(napcat_delivery_cfg(config))


def qq_upload_config(config: Any) -> tuple[str, int, int, str, int, str]:
    return qq_upload_settings(qq_delivery_cfg(config))


def verify_scaled(config: Any) -> bool:
    raw = napcat_delivery_cfg(config).get("verify_scaled")
    return DEFAULT_DELIVERY.napcat_verify_scaled if is_blank(raw) else truthy(raw)


def onebot_image_timeout(config: Any, fallback: int = DEFAULT_DELIVERY.napcat_timeout) -> int:
    raw = napcat_delivery_cfg(config).get("onebot_image_timeout")
    return bounded_int(raw, fallback, 10, 300)


def encode_options(config: Any) -> tuple[int, bool]:
    """本地压缩进阶选项：目标体积上限（字节，0 表示不限制）与 WebP 输出。"""
    raw = napcat_delivery_cfg(config)
    target_kb = bounded_int(raw.get("target_kb"), DEFAULT_DELIVERY.napcat_target_kb, 0, 10240)
    return target_kb * 1024, truthy(raw.get("webp"))


def bucket_config(config: Any) -> dict | None:
    """储存桶配置完整时返回可并入图床配置的 dict（含 mode），否则 None。"""
    raw = dict_group(config, "storage_bucket")
    mode = str(raw.get("mode") or "").strip()
    if mode not in BUCKET_MODES:
        return None
    missing = [f"storage_bucket.{k}" for k in BUCKET_REQUIRED[mode] if is_blank(raw.get(k))]
    if (
        mode == "oracle_oci"
        and not truthy(raw.get("oci_public_bucket"))
        and is_blank(raw.get("oci_public_base_url"))
    ):
        missing.append("storage_bucket.oci_public_bucket / oci_public_base_url（公开直链二选一）")
    if missing:
        logger_warning(
            f"储存桶 {mode} 配置不完整（缺 {'、'.join(missing)}），登记原图将改用图床设置。"
        )
        return None
    cfg = dict(raw)
    cfg["mode"] = mode
    return cfg


def effective_bed_cfg(config: Any) -> dict:
    """合并储存桶与图床两组配置：储存桶配置完整时优先。"""
    bed = dict(dict_group(config, "image_bed"))
    bucket = bucket_config(config)
    if bucket is not None:
        bed.update(bucket)
    return bed


def engine_choice(config: Any) -> str:
    choice = str(scalar_cfg(config, "search_engine") or DEFAULT_BASIC.search_engine).strip().lower()
    return choice if choice in ("hash", "vector", "auto") else DEFAULT_BASIC.search_engine


def vector_index_on_register(config: Any) -> bool:
    value = dict_group(config, "vector_search").get("vector_index_on_register")
    return DEFAULT_VECTOR.vector_index_on_register if is_blank(value) else truthy(value)


def scan_dirs(config: Any) -> list[str]:
    raw = scalar_cfg(config, "scan_dirs")
    if isinstance(raw, str):  # 手工配置成单个路径字符串时按一个目录处理
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [d.strip() for d in raw if isinstance(d, str) and d.strip()]


def random_settings(config: Any, allowed_private_hosts: set[str]) -> dict:
    """归一化随机图配置组（random_media），并重写本实例的内网白名单。"""
    raw = dict_group(config, "random_media")
    d = DEFAULT_RANDOM
    lan_base = str(raw.get("lan_base_url") or "").strip()
    lan_host = ""
    if lan_base:
        parsed_lan = urlsplit(lan_base)
        if parsed_lan.scheme in ("http", "https") and parsed_lan.netloc:
            lan_host = (parsed_lan.hostname or "").lower()
        else:
            logger_warning(
                f"random_media.lan_base_url 不是有效的 http(s) 地址，已忽略: {lan_base}"
            )
            lan_base = ""
    allowed_private_hosts.clear()
    if lan_host:
        allowed_private_hosts.add(lan_host)
    timeout = as_float(raw.get("timeout"), d.timeout)
    if timeout <= 0:
        # 0/负数会让请求立即超时，回退默认值
        timeout = d.timeout
    return {
        "base_url": str(raw.get("base_url") or "").strip(),
        "lan_base_url": lan_base,
        "api_endpoint": str(raw.get("api_endpoint") or RANDOM_ENDPOINT_DEFAULT).strip()
        or RANDOM_ENDPOINT_DEFAULT,
        "api_token": str(raw.get("api_token") or "").strip(),
        "default_dir": str(raw.get("default_dir") or "").strip(),
        "timeout": timeout,
        "retry_count": bounded_int(raw.get("retry_count"), d.retry_count, 0, 10),
        "show_file_info": bool_or(raw.get("show_file_info"), d.show_file_info),
        "enable_llm": bool_or(raw.get("enable_llm"), d.enable_llm),
        "send_thumbnail": bool_or(raw.get("send_thumbnail"), d.send_thumbnail),
    }


def checkin_settings(config: Any) -> dict:
    """归一化每日签到配置（checkin 组）：所有取值都做范围收敛。"""
    raw = dict_group(config, "checkin")
    d = DEFAULT_CHECKIN
    headers = raw.get("quote_api_headers")
    source = str(raw.get("quote_source") or "").strip().lower()
    mode = str(raw.get("mode") or "").strip().lower()
    render_mode = str(raw.get("render_mode") or "").strip().lower()
    image_mode = str(raw.get("image_mode") or "").strip().lower()
    return {
        "enabled": bool_or(raw.get("enabled"), d.enabled),
        "mode": "weibo" if mode == "weibo" else "quote",
        "render_mode": "simple" if render_mode == "simple" else "card",
        # 签到自己的取图口径，与 random_media.send_thumbnail 独立。
        "image_mode": "original" if image_mode == "original" else "thumbnail",
        # 签到文字两个开关，互相独立：
        # show_quote=卡片/消息里要不要那句随机语句（微博模式下是文案），
        #   关掉就是「只有美图 + 签到信息」；
        # show_sender=签到文字里要不要带上签到者群昵称（OneBot 群名片优先，
        #   接口失败才回退事件昵称）。
        "show_quote": bool_or(raw.get("show_quote"), d.show_quote),
        "show_sender": bool_or(raw.get("show_sender"), d.show_sender),
        "border_px": max(0, min(200, as_int(raw.get("border_px"), d.border_px))),
        "utc_offset_hours": max(-12, min(14, as_int(raw.get("utc_offset_hours"), d.utc_offset_hours))),
        "quote_source": source if source in {"api", "mixed"} else "builtin",
        "quote_local_text": str(raw.get("quote_local_text") or ""),
        "quote_api_url": str(raw.get("quote_api_url") or "").strip(),
        "quote_json_path": str(raw.get("quote_json_path") or "").strip(),
        "quote_api_headers": headers if isinstance(headers, dict) else {},
        "quote_timeout": max(1, min(30, as_int(raw.get("quote_timeout"), d.quote_timeout))),
        "font_path": str(raw.get("font_path") or "").strip(),
    }


__all__ = [
    "NAPCAT_KEY_MAP",
    "QQ_KEY_MAP",
    "SCALAR_PATHS",
    "bucket_config",
    "checkin_settings",
    "delivery_config",
    "dict_group",
    "effective_bed_cfg",
    "encode_options",
    "engine_choice",
    "ensure_group_cfg",
    "napcat_delivery_cfg",
    "onebot_image_timeout",
    "qq_delivery_cfg",
    "qq_upload_config",
    "random_settings",
    "scan_dirs",
    "vector_index_on_register",
    "verify_scaled",
]

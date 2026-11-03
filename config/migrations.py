"""配置布局迁移（1.7.14 起的版本化搬移）。

迁移只做「搬移 + 打标记」，不删旧键：AstrBot 加载插件配置时会按 schema 丢弃
未声明的键，旧键必须以隐藏镜像的形式留在 schema 里，否则用户旧值在插件初始化
之前就被清成默认值。这里集中两段历史迁移，main 只保留同名薄委托。
"""

from __future__ import annotations

import logging
from typing import Any

try:  # 包模式：astrbot_plugin_image_trace.config.migrations
    from ..common import is_blank
except ImportError:  # 兼容插件以独立模块方式加载（仓库目录在 sys.path 上）
    from common import is_blank  # type: ignore[no-redef]

logger = logging.getLogger("astrbot")

LAYOUT_VERSION = "3"

# 旧顶层键 → (新组, 新字段)
SCALAR_PATHS: dict[str, tuple[str, str]] = {
    "search_engine": ("basic", "search_engine"),
    "scan_dirs": ("basic", "scan_dirs"),
    "similarity_threshold": ("basic", "hash_similarity_threshold"),
    "hash_size": ("basic", "hash_size"),
    "top_n": ("basic", "top_n"),
    "max_images_per_query": ("basic", "max_images_per_query"),
    "ai_verify": ("basic", "ai_verify"),
    "register_admin_only": ("basic", "register_admin_only"),
    "max_download_mb": ("limits", "max_download_mb"),
    "remote_hash_enabled": ("limits", "remote_hash_enabled"),
    "remote_hash_ttl": ("limits", "remote_hash_ttl"),
    "remote_hash_max_points": ("limits", "remote_hash_max_points"),
}

# 旧 image_delivery 字段 → delivery 组里的 NapCat 字段
NAPCAT_KEY_MAP: dict[str, str] = {
    "mode": "napcat_mode",
    "max_side": "napcat_max_side",
    "quality": "napcat_quality",
    "verify_scaled": "napcat_verify_scaled",
    "scaled_url_style": "napcat_scaled_url_style",
    "target_kb": "napcat_target_kb",
    "webp": "napcat_webp",
    "onebot_image_timeout": "napcat_timeout",
}

# 旧 qq_upload 字段 → delivery 组里的 QQ 官方字段
QQ_KEY_MAP: dict[str, str] = {
    "mode": "qq_mode",
    "direct_max_mb": "qq_native_max_mb",
    "auto_prefer": "qq_auto_prefer",
    "url_max_mb": "qq_url_max_mb",
    "max_upload_mb": "qq_max_upload_mb",
    "on_failure": "qq_on_failure",
}

# 储存桶（对象存储）独立配置组；v1.2.x 曾作为 image_bed 的模式
BUCKET_MODES = ("cloudflare_r2", "oracle_oci")
BUCKET_REQUIRED = {
    "cloudflare_r2": (
        "r2_account_id",
        "r2_access_key_id",
        "r2_secret_access_key",
        "r2_bucket",
        "r2_public_base_url",
    ),
    "oracle_oci": (
        "oci_namespace",
        "oci_region",
        "oci_access_key_id",
        "oci_secret_access_key",
        "oci_bucket",
    ),
}
# v1.2.x image_bed 组里可能遗留的储存桶字段（迁移时搬走并清理）
BUCKET_LEGACY_FIELDS = (
    "r2_account_id",
    "r2_access_key_id",
    "r2_secret_access_key",
    "r2_bucket",
    "r2_endpoint",
    "r2_public_base_url",
    "oci_namespace",
    "oci_region",
    "oci_access_key_id",
    "oci_secret_access_key",
    "oci_bucket",
    "oci_endpoint",
    "oci_public_bucket",
    "oci_public_base_url",
)


def ensure_group(config: Any, name: str) -> dict:
    """取（必要时创建）对象型配置组，迁移与新键写入共用。"""
    group = config.get(name)
    if not isinstance(group, dict):
        group = {}
        config[name] = group
    return group


def _save(config: Any) -> None:
    saver = getattr(config, "save_config", None)
    if callable(saver):
        saver()


def migrate_config_layout(config: Any) -> None:
    """把旧布局搬进 basic / delivery / limits（一次性，带版本标记）。"""
    if str(config.get("config_layout") or "") == LAYOUT_VERSION:
        return
    moved = False
    for legacy, (group, field) in SCALAR_PATHS.items():
        value = config.get(legacy)
        if is_blank(value):
            continue
        ensure_group(config, group)[field] = value
        moved = True
    for legacy_group, key_map in (
        ("image_delivery", NAPCAT_KEY_MAP),
        ("qq_upload", QQ_KEY_MAP),
    ):
        legacy_items = config.get(legacy_group)
        if not isinstance(legacy_items, dict):
            continue
        target = ensure_group(config, "delivery")
        for old_key, new_key in key_map.items():
            value = legacy_items.get(old_key)
            if not is_blank(value):
                target[new_key] = value
                moved = True
    config["config_layout"] = LAYOUT_VERSION
    try:
        _save(config)
    except Exception as exc:  # 保存失败不影响本次会话
        logger.warning(f"配置布局迁移写入失败（重启后会自动重试）: {exc}")
    if moved:
        logger.info("配置已迁移到新布局：basic / delivery / limits（旧键保留为隐藏镜像）")


def migrate_bucket_config(config: Any) -> None:
    """v1.2.x 升级迁移（幂等）：image_bed 组里的储存桶模式与字段搬到
    独立的 storage_bucket 组。
    """
    bed = config.get("image_bed")
    if not isinstance(bed, dict):
        return
    legacy_mode = str(bed.get("mode") or "").strip()
    if legacy_mode not in BUCKET_MODES:
        return
    bucket = config.get("storage_bucket")
    if not isinstance(bucket, dict):
        bucket = {}
        config["storage_bucket"] = bucket
    if not bucket.get("mode"):
        bucket["mode"] = legacy_mode
        for key in BUCKET_LEGACY_FIELDS:
            value = bed.get(key)
            if value not in (None, "", False):
                bucket[key] = value
    # 旧下拉里已没有储存桶选项，归位 local；r2_*/oci_* 字段也不在
    # image_bed 组的 schema 里了，一并清掉避免残留
    bed["mode"] = "local"
    for key in BUCKET_LEGACY_FIELDS:
        bed.pop(key, None)
    try:
        _save(config)
    except Exception as exc:
        # 保存失败只影响持久化：本次会话已用迁移后的内存配置，
        # 重启后同一条迁移会再次执行（幂等）
        logger.warning(f"储存桶配置迁移写入失败（重启后会自动重试）: {exc}")
    logger.info(
        f"已把 v1.2.x 图床配置中的储存桶设置（{legacy_mode}）迁移到 "
        "storage_bucket 组，image_bed 组回归图床模式 local。"
    )

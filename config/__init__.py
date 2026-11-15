"""配置子包：类型化默认值、读取门面与布局迁移。"""

from .loader import (
    bucket_config,
    checkin_settings,
    delivery_config,
    dict_group,
    effective_bed_cfg,
    encode_options,
    engine_choice,
    ensure_group_cfg,
    load_plugin_config,
    napcat_delivery_cfg,
    onebot_image_timeout,
    qq_delivery_cfg,
    qq_upload_config,
    random_settings,
    scan_dirs,
    vector_index_on_register,
    verify_scaled,
)
from .migrations import migrate_bucket_config, migrate_config_layout
from .typed import PluginConfig

__all__ = [
    "PluginConfig",
    "bucket_config",
    "checkin_settings",
    "delivery_config",
    "dict_group",
    "effective_bed_cfg",
    "encode_options",
    "engine_choice",
    "ensure_group_cfg",
    "load_plugin_config",
    "migrate_bucket_config",
    "migrate_config_layout",
    "napcat_delivery_cfg",
    "onebot_image_timeout",
    "qq_delivery_cfg",
    "qq_upload_config",
    "random_settings",
    "scan_dirs",
    "vector_index_on_register",
    "verify_scaled",
]

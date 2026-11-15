"""配置数据类：默认值的唯一来源。

字段名与 `_conf_schema.json` 一一对应。重构后的读取路径（config/loader.py）
与测试都以这里的默认值为准，避免「面板默认值、代码兜底值、文档默认值」三份，
改一处漏两处。数据类只描述结构与默认值，不承担迁移或 IO。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class BasicConfig:
    search_engine: Literal["auto", "vector", "hash"] = "auto"
    scan_dirs: tuple[str, ...] = ()
    hash_similarity_threshold: float = 0.85
    ai_verify: bool = False
    register_admin_only: bool = True
    max_images_per_query: int = 3
    hash_size: int = 16
    top_n: int = 3


@dataclass(frozen=True)
class DeliveryConfig:
    napcat_mode: str = "auto"
    napcat_max_side: int = 1920
    napcat_quality: int = 85
    napcat_verify_scaled: bool = True
    napcat_scaled_url_style: Literal["query", "cf-path"] = "query"
    napcat_target_kb: int = 0
    napcat_webp: bool = False
    napcat_timeout: int = 60
    qq_mode: str = "auto"
    qq_native_max_mb: int = 3
    qq_auto_prefer: Literal["url", "bytes"] = "url"
    qq_url_max_mb: int = 20
    qq_max_upload_mb: int = 100
    qq_on_failure: Literal["fallback", "notify"] = "fallback"


@dataclass(frozen=True)
class ImageBedConfig:
    mode: str = "local"
    api_url: str = ""
    token: str = ""
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    file_field: str = "file"
    extra_fields: dict = field(default_factory=dict)
    url_path: str = "data.url"
    timeout: int = 30
    cfi_base_url: str = ""
    cfi_token: str = ""
    cfi_auth_code: str = ""
    cfi_upload_channel: str = ""
    cfi_channel_name: str = ""
    cfi_upload_folder: str = ""


@dataclass(frozen=True)
class StorageBucketConfig:
    mode: Literal["none", "cloudflare_r2", "oracle_oci"] = "none"
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    r2_endpoint: str = ""
    r2_public_base_url: str = ""
    oci_namespace: str = ""
    oci_region: str = ""
    oci_access_key_id: str = ""
    oci_secret_access_key: str = ""
    oci_bucket: str = ""
    oci_endpoint: str = ""
    oci_public_bucket: bool = False
    oci_public_base_url: str = ""


@dataclass(frozen=True)
class RandomMediaConfig:
    base_url: str = ""
    lan_base_url: str = ""
    api_endpoint: str = "/random"
    api_token: str = ""
    default_dir: str = ""
    timeout: float = 10.0
    retry_count: int = 3
    show_file_info: bool = True
    enable_llm: bool = True
    send_thumbnail: bool = True


@dataclass(frozen=True)
class CheckinConfig:
    enabled: bool = True
    mode: Literal["quote", "weibo"] = "quote"
    render_mode: Literal["card", "simple"] = "card"
    image_mode: Literal["thumbnail", "original"] = "thumbnail"
    show_quote: bool = True
    show_sender: bool = True
    border_px: int = 0
    utc_offset_hours: int = 8
    quote_source: Literal["builtin", "api", "mixed"] = "builtin"
    quote_local_text: str = ""
    quote_api_url: str = "https://v1.hitokoto.cn/?encode=json&c=d&c=i&c=k"
    quote_json_path: str = "hitokoto"
    quote_api_headers: dict = field(default_factory=dict)
    quote_timeout: int = 5
    font_path: str = ""


@dataclass(frozen=True)
class VectorConfig:
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    collection_name: str = "imgbed_images"
    embed_base_url: str = ""
    embed_api_key: str = ""
    embed_model: str = ""
    similarity_threshold: float = 0.8
    duplicate_vector_threshold: float = 0.995
    vector_index_on_register: bool = True
    embed_image_input: str = "nemotron-vl"
    embed_input_type: str = "passage"
    top_k: int = 5
    request_timeout: int = 30


@dataclass(frozen=True)
class LimitsConfig:
    max_download_mb: int = 20
    remote_hash_enabled: bool = True
    remote_hash_ttl: int = 21600
    remote_hash_max_points: int = 0


@dataclass(frozen=True)
class PluginConfig:
    basic: BasicConfig = field(default_factory=BasicConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    image_bed: ImageBedConfig = field(default_factory=ImageBedConfig)
    storage_bucket: StorageBucketConfig = field(default_factory=StorageBucketConfig)
    random_media: RandomMediaConfig = field(default_factory=RandomMediaConfig)
    checkin: CheckinConfig = field(default_factory=CheckinConfig)
    vector_search: VectorConfig = field(default_factory=VectorConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)


# 共享的默认单例：调用点用它兜底，任何默认值只在这里定义一次。
DEFAULT_BASIC = BasicConfig()
DEFAULT_DELIVERY = DeliveryConfig()
DEFAULT_BED = ImageBedConfig()
DEFAULT_BUCKET = StorageBucketConfig()
DEFAULT_RANDOM = RandomMediaConfig()
DEFAULT_CHECKIN = CheckinConfig()
DEFAULT_VECTOR = VectorConfig()
DEFAULT_LIMITS = LimitsConfig()

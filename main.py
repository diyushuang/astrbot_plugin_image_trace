"""AstrBot 图片溯源插件。

群内发送或引用图片 + 触发指令 -> 提取图片 -> 按当前引擎检索原图并回传：

- 哈希引擎：本地计算感知哈希特征（pHash/dHash/aHash），与本地图库比对
  汉明距离（检索仅用 pHash，dHash/aHash 预留作二级确认字段）；
- 向量引擎：调用多模态向量 AI 生成向量，到 Qdrant 向量库检索图床图片；
- auto 引擎：向量优先，未命中或不可用时回退哈希。

- QQ 消息的接收与发送完全通过 AstrBot 事件与消息组件 API 完成；
- 所有出网请求（下载兜底/图床/向量服务）统一经 url_guard 做 SSRF 校验；
- 可选使用当前会话的视觉大模型对哈希命中结果做二次复核（ai_verify 开关）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import os
import random
import secrets
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse, urlsplit

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# 说明：获取 AstrBot data 目录目前官方仅有 astrbot.core.utils.astrbot_path 这一条途径
# （astrbot.api / Context 均未暴露等价 API）。StarTools.get_data_dir() 路径等价
# （data/plugin_data/<name>），但要求较新的 AstrBot 版本，与 metadata.yaml 声明的
# astrbot_version ">=4.0.0" 兼容范围冲突，故保留此导入（AstrBot 官方插件生态通用做法）。
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from .common import (
        AI_VERIFY_MAX_CANDIDATES,
        COMPRESS_CONCURRENCY,
        DOWNLOAD_CHUNK_SIZE,
        DOWNLOAD_TIMEOUT,
        MEMORY_DOWNLOAD_MAX_BYTES,
        ONEBOT_IMAGE_TIMEOUT,
        PLAN_TOTAL_TIMEOUT,
        PROBE_CONCURRENCY,
        PROBE_TIMEOUT,
        QQ_URL_COOLDOWN_SECONDS,
        RESCAN_BATCH_SIZE,
        RESCAN_PROGRESS_EVERY,
        SCALED_VERIFY_COOLDOWN_SECONDS,
        TMP_MAX_AGE_SECONDS,
        as_float,
        as_int,
        bool_or,
        is_blank,
        is_qq_image_bed_host,
        read_bytes,
        truthy,
    )
    from .features import (
        ImageFeatures,
        compute_features,
        heif_available,
        image_bytes_ok,
        image_file_ok,
        phash_hex_len,
    )
    from .hash_index import REMOTE_HASH_PAYLOAD_KEYS, HashIndex
    from .http_client import GuardedHttpClient
    from .image_bed import MODE_CFB, ImageBedClient
    from .image_delivery import (
        CompressionEvidence,
        DeliveryTarget,
        SendOutcome,
        bounded_int,
        build_imgbed_file_url,
        build_original_url,
        build_scaled_url,
        choose_qq_upload_channel,
        classify_probe_status,
        classify_send_error,
        compression_evidence,
        content_length_from_headers,
        deduplicate_vector_hits,
        delivery_settings,
        delivery_target,
        is_napcat_parseable_url,
        is_public_http_url,
        is_scaling_capability_rejection,
        is_scaling_needed,
        prefer_inline_bytes,
        prefer_url_direct,
        prepare_image_bytes,
        qq_upload_safe_image,
        qq_upload_settings,
        send_diagnosis,
        sniff_image_format,
        upgrade_to_https,
    )
    from .library import ImageLibrary
    from .media_history import MediaHistory
    from .random_media import (
        RANDOM_ENDPOINT_DEFAULT,
        RandomMediaClient,
        RandomMediaError,
        clean_display_name,
        extract_directory,
        is_thumbnail_url,
        media_filename,
        media_kind,
        pick_default_directory,
        strip_leading_decoration,
        swap_url_origin,
    )
    from .url_guard import read_limited_bytes, set_allowed_private_hosts
    from .vector_search import VectorEngine, VectorEngineError
except ImportError:  # 兼容插件以独立模块方式加载
    from common import (  # type: ignore[no-redef]
        AI_VERIFY_MAX_CANDIDATES,
        COMPRESS_CONCURRENCY,
        DOWNLOAD_CHUNK_SIZE,
        DOWNLOAD_TIMEOUT,
        MEMORY_DOWNLOAD_MAX_BYTES,
        ONEBOT_IMAGE_TIMEOUT,
        PLAN_TOTAL_TIMEOUT,
        PROBE_CONCURRENCY,
        PROBE_TIMEOUT,
        QQ_URL_COOLDOWN_SECONDS,
        RESCAN_BATCH_SIZE,
        RESCAN_PROGRESS_EVERY,
        SCALED_VERIFY_COOLDOWN_SECONDS,
        TMP_MAX_AGE_SECONDS,
        as_float,
        as_int,
        bool_or,
        is_blank,
        is_qq_image_bed_host,
        read_bytes,
        truthy,
    )
    from features import (  # type: ignore[no-redef]
        ImageFeatures,
        compute_features,
        heif_available,
        image_bytes_ok,
        image_file_ok,
        phash_hex_len,
    )
    from hash_index import REMOTE_HASH_PAYLOAD_KEYS, HashIndex  # type: ignore[no-redef]
    from http_client import GuardedHttpClient  # type: ignore[no-redef]
    from image_bed import MODE_CFB, ImageBedClient  # type: ignore[no-redef]
    from image_delivery import (  # type: ignore[no-redef]
        CompressionEvidence,
        DeliveryTarget,
        SendOutcome,
        bounded_int,
        build_imgbed_file_url,
        build_original_url,
        build_scaled_url,
        choose_qq_upload_channel,
        classify_probe_status,
        classify_send_error,
        compression_evidence,
        content_length_from_headers,
        deduplicate_vector_hits,
        delivery_settings,
        delivery_target,
        is_napcat_parseable_url,
        is_public_http_url,
        is_scaling_capability_rejection,
        is_scaling_needed,
        prefer_inline_bytes,
        prefer_url_direct,
        prepare_image_bytes,
        qq_upload_safe_image,
        qq_upload_settings,
        send_diagnosis,
        sniff_image_format,
        upgrade_to_https,
    )
    from library import ImageLibrary  # type: ignore[no-redef]
    from media_history import MediaHistory  # type: ignore[no-redef]
    from random_media import (  # type: ignore[no-redef]
        RANDOM_ENDPOINT_DEFAULT,
        RandomMediaClient,
        RandomMediaError,
        clean_display_name,
        extract_directory,
        is_thumbnail_url,
        media_filename,
        media_kind,
        pick_default_directory,
        strip_leading_decoration,
        swap_url_origin,
    )
    from url_guard import (  # type: ignore[no-redef]
        read_limited_bytes,
        set_allowed_private_hosts,
    )
    from vector_search import VectorEngine, VectorEngineError  # type: ignore[no-redef]

PLUGIN_NAME = "astrbot_plugin_image_trace"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".jfif", ".heic", ".heif"}
# QQ 开放平台富媒体 file_type：1=图片、2=视频、3=语音、4=文件
QQ_IMAGE_FILE_TYPE = 1

# ----------------------------------------------------------------------
# 配置布局（1.7.14）：同类项合并进 basic / delivery / limits 三个组。
# 旧布局（顶层散项 + image_delivery / qq_upload 两组）继续可读——新键优先、
# 旧键回落，启动时由 _migrate_config_layout 落盘迁移，用户无需手动改配置。
# ----------------------------------------------------------------------

# 旧顶层键 → (新组, 新字段)
_SCALAR_PATHS: dict[str, tuple[str, str]] = {
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

# 当前配置布局版本（写在隐藏项 config_layout 里，避免重复迁移覆盖用户新值）
_LAYOUT_VERSION = "3"

# 旧 image_delivery 字段 → delivery 组里的 NapCat 字段
_NAPCAT_KEY_MAP: dict[str, str] = {
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
_QQ_KEY_MAP: dict[str, str] = {
    "mode": "qq_mode",
    "direct_max_mb": "qq_native_max_mb",
    "auto_prefer": "qq_auto_prefer",
    "url_max_mb": "qq_url_max_mb",
    "max_upload_mb": "qq_max_upload_mb",
    "on_failure": "qq_on_failure",
}



def _describe_bad_image(path: str) -> tuple[str, str | None]:
    """坏产物摘要 + 按文件头识别的容器格式，供日志与失败分诊使用。

    第二项为 None 表示「连图片容器都认不出」（错误体、HTML、空文件），非
    None 表示「容器认得出但解不开」（缺解码器、截断）——两者的处置完全不同：
    前者才值得重下，后者重下也是同一份内容。函数为同步 IO，调用方应走
    asyncio.to_thread。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(64)
        size = os.path.getsize(path)
    except OSError:
        return "无法读取", None
    text = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
    return f"{size} 字节, 头部 {text!r}", sniff_image_format(head)


# 储存桶（对象存储）独立配置组；v1.2.x 曾作为 image_bed 的模式，迁移见
# _migrate_bucket_config。完整判定与 image_bed.py 的 _require / 公开直链
# 校验口径一致：缺任何一项上传都必然回退本地副本，不满足就不优先于图床
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


class ImageTracePlugin(Star):
    """图片溯源插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME)
        self.tmp_dir = os.path.join(self.data_dir, "tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)

        # 旧布局先搬进 basic / delivery / limits，后续读取一律走新路径
        self._migrate_config_layout()
        # pHash 十六进制长度 = phash_hex_len(hash_size)（packbits 补齐到字节
        # 边界）；hash_size 必须为 4 的倍数，否则长度公式与旧数据对不上，
        # 全库记录都会被判为不匹配、索引静默清空
        hash_size = as_int(self._cfg("hash_size"), 16)
        if not 4 <= hash_size <= 32 or hash_size % 4 != 0:
            logger.warning(
                f"hash_size 配置值 {hash_size} 无效（须为 4~32 之间 4 的倍数，"
                "如 8/16/24/32），已回退为默认 16。"
            )
            hash_size = 16
        self.hash_size = hash_size
        self.library = ImageLibrary(
            os.path.join(self.data_dir, "library.db"),
            expected_phash_hex_len=phash_hex_len(hash_size),
        )
        self.hash_threshold = max(0.0, min(1.0, self._float_cfg("similarity_threshold", 0.85)))
        self.top_n = max(0, self._int_cfg("top_n", 3))
        self.max_images_per_query = max(1, self._int_cfg("max_images_per_query", 3))
        # v1.2.x 的储存桶配置先迁移，再合并（储存桶完整时优先于图床）
        self._migrate_bucket_config()
        self._bed_cfg = self._effective_bed_cfg()
        self._http: GuardedHttpClient | None = None
        self.bed = ImageBedClient(self.data_dir, self._bed_cfg, self._get_http)
        self.vector = VectorEngine(config, self._get_http)
        # 图床感知哈希索引：把 img-indexer 入库时预置的 pHash 拉到本地，
        # 让哈希引擎也能反查图床里的图（scan_dirs 只能扫本地目录，覆盖不到）
        self.remote_hash = HashIndex(
            os.path.join(self.data_dir, "remote_phash.json"),
            expected_phash_hex_len=phash_hex_len(hash_size),
        )
        self._remote_hash_task: asyncio.Task | None = None
        # 回传过的原图直链的历史（供 /原图 找回）；随机图客户端惰性构造
        self.history = MediaHistory()
        self._random_media_client: RandomMediaClient | None = None
        # 图床缩放能力冷却截止时刻（time.monotonic 口径，0 表示未观测到拒绝）。
        # 探测到 API 级拒绝（405/501 等）后的一段时间内不再探测缩放版。
        self._scaled_verify_unsupported_until = 0.0
        # QQ URL 上传通道冷却截止时刻：图床公网域名存在但腾讯侧拉不到时，auto
        # 模式在一段时间内直接分片，避免每次都先白撞一次必败的 URL 上传
        self._qq_url_unsupported_until = 0.0
        self._cleanup_tmp()
        # 先吃磁盘缓存（同步、瞬时），网络刷新交给生命周期钩子里的后台任务——
        # __init__ 里不能建 Task（此时还没有运行中的事件循环）
        cache_note = ""
        if self.remote_hash_on() and self.remote_hash.load_cache():
            cache_note = f"（图床哈希缓存 {self.remote_hash.count} 条）"
        engine = self._engine_choice()
        vector_note = "（向量引擎已启用）" if self.vector.enabled and engine != "hash" else ""
        bucket_note = (
            f"（储存桶 {self._bed_mode()} 优先）" if self._bed_mode() in BUCKET_MODES else ""
        )
        logger.info(
            f"图片溯源插件已加载，当前图库共 {self.library.count()} 条，"
            f"引擎={engine} {vector_note}{bucket_note}{cache_note}"
        )

    # ------------------------------------------------------------------
    # 图床哈希索引（后台刷新）
    # ------------------------------------------------------------------

    def remote_hash_on(self) -> bool:
        """图床哈希索引开关（默认开；需向量引擎已配置才能拉到数据）。"""
        return self._bool_cfg("remote_hash_enabled", True) and self.vector.enabled

    def _remote_hash_ttl(self) -> int:
        """缓存最长有效期（秒）。超过后启动时无条件重建，避免长期吃旧快照。"""
        return max(300, self._int_cfg("remote_hash_ttl", 21600))

    def _remote_hash_max_points(self) -> int:
        """拉取上限（0 = 不限）。用于先小样本验证链路，再放开全量。"""
        return max(0, self._int_cfg("remote_hash_max_points", 0))

    async def _refresh_remote_hash(self) -> None:
        """从 Qdrant 拉全量 pHash 重建本地索引。异常不出后台任务。"""
        try:
            points = await self.vector.scroll_payloads(
                key="phash",
                # 必须显式带上展示字段：scroll 默认只取 key 一个字段，缺 image_url
                # 会让 HashIndex.build 把全部点判成「无直链」跳过（索引恒为 0 条）
                with_payload_keys=list(REMOTE_HASH_PAYLOAD_KEYS),
                page_size=512,
                max_points=self._remote_hash_max_points(),
            )
            count = await asyncio.to_thread(self.remote_hash.build, points)
            logger.info(
                f"图床哈希索引已刷新：{count} 条可用"
                f"（远端点位 {len(points) if isinstance(points, list) else 0}），"
                f"缓存于 {self.remote_hash.cache_path}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"图床哈希索引刷新失败（沿用已有缓存）: {exc}")

    def start_remote_hash_refresh(self) -> None:
        """启动后台刷新任务（幂等）：已有一个在跑就不再起第二个。

        启动即刷新 vs 按需刷新：刷新要翻 ~47 页（2.4 万点），耗时数秒到数十秒。
        放在启动后台跑，首次 /溯源 就能命中；若拖到首次查询再拉，用户会先吃到
        一次「未命中」——而「未命中」是个确定结论，比查不到更误导人。
        """
        if not self.remote_hash_on():
            return
        if self._remote_hash_task is not None and not self._remote_hash_task.done():
            return
        self._remote_hash_task = asyncio.create_task(self._refresh_remote_hash())

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _dict_cfg(self, key: str) -> dict:
        """取对象型配置；手工改坏配置（非 dict）时退化为空 dict 而不是抛错。"""
        value = self.config.get(key)
        return value if isinstance(value, dict) else {}

    def _ensure_group(self, name: str) -> dict:
        """取（必要时创建）对象型配置组，迁移与新键写入共用。"""
        group = self.config.get(name)
        if not isinstance(group, dict):
            group = {}
            self.config[name] = group
        return group

    def _cfg(self, key: str):
        """读标量配置：新布局（basic / limits）优先，回落旧顶层键。

        迁移未执行（配置被手改、或用户回滚过版本）时旧键仍能生效；两处都有值时
        以新键为准，避免"面板改了不生效"。
        """
        group, field = _SCALAR_PATHS.get(key, ("", key))
        if group:
            value = self._dict_cfg(group).get(field)
            if not is_blank(value):
                return value
        return self.config.get(key)

    def _int_cfg(self, key: str, default: int) -> int:
        return as_int(self._cfg(key), default)

    def _float_cfg(self, key: str, default: float) -> float:
        return as_float(self._cfg(key), default)

    def _bool_cfg(self, key: str, default: bool) -> bool:
        """布尔配置：未配置（缺键/空串/null）时取默认值，与数值配置口径一致。"""
        return bool_or(self._cfg(key), default)

    def _migrate_config_layout(self) -> None:
        """把旧布局搬进 basic / delivery / limits（一次性，带版本标记）。

        为什么不「删旧键」：AstrBot 加载插件配置时会按 schema 删除已不存在的键
        （AstrBotConfig.check_config_integrity 会丢掉 refer_conf 之外的键），键名
        一改，用户的旧值在插件初始化**之前**就已经被清成默认值了。所以旧键继续以
        「隐藏镜像」的形式留在 schema 里（condition 恒假，面板不显示），迁移只做
        搬移 + 打标记，之后不再覆盖——用户此后在面板改的新值不会被旧值盖回去。

        旧值优先于新键：新键在首次加载时已被 schema 默认值填满，此时「新键非空」
        不代表用户改过，只有旧键才是用户真实设置。
        """
        if str(self.config.get("config_layout") or "") == _LAYOUT_VERSION:
            return
        moved = False
        for legacy, (group, field) in _SCALAR_PATHS.items():
            value = self.config.get(legacy)
            if is_blank(value):
                continue
            self._ensure_group(group)[field] = value
            moved = True
        for legacy_group, key_map in (
            ("image_delivery", _NAPCAT_KEY_MAP),
            ("qq_upload", _QQ_KEY_MAP),
        ):
            legacy_items = self.config.get(legacy_group)
            if not isinstance(legacy_items, dict):
                continue
            target = self._ensure_group("delivery")
            for old_key, new_key in key_map.items():
                value = legacy_items.get(old_key)
                if not is_blank(value):
                    target[new_key] = value
                    moved = True
        self.config["config_layout"] = _LAYOUT_VERSION
        try:
            self.config.save_config()
        except Exception as exc:  # 保存失败不影响本次会话
            logger.warning(f"配置布局迁移写入失败（重启后会自动重试）: {exc}")
        if moved:
            logger.info("配置已迁移到新布局：basic / delivery / limits（旧键保留为隐藏镜像）")

    def _napcat_delivery_cfg(self) -> dict:
        """推送通道（NapCat）配置，键名沿用 delivery_settings 的入参约定。

        delivery.napcat_* 优先，逐项回落旧 image_delivery.*（迁移未执行时也能用）。
        """
        new = self._dict_cfg("delivery")
        old = self._dict_cfg("image_delivery")
        return {
            "mode": self._first_value(new.get("napcat_mode"), old.get("mode")),
            "max_side": self._first_value(new.get("napcat_max_side"), old.get("max_side")),
            "quality": self._first_value(new.get("napcat_quality"), old.get("quality")),
            "scaled_url_style": self._first_value(
                new.get("napcat_scaled_url_style"), old.get("scaled_url_style")
            ),
            "verify_scaled": self._first_value(
                new.get("napcat_verify_scaled"), old.get("verify_scaled")
            ),
            "target_kb": self._first_value(new.get("napcat_target_kb"), old.get("target_kb")),
            "webp": self._first_value(new.get("napcat_webp"), old.get("webp")),
            "onebot_image_timeout": self._first_value(
                new.get("napcat_timeout"), old.get("onebot_image_timeout")
            ),
        }

    def _qq_delivery_cfg(self) -> dict:
        """推送通道（QQ 官方）配置，键名沿用 qq_upload_settings 的入参约定。"""
        new = self._dict_cfg("delivery")
        old = self._dict_cfg("qq_upload")
        return {
            "mode": self._first_value(new.get("qq_mode"), old.get("mode")),
            "direct_max_mb": self._first_value(
                new.get("qq_native_max_mb"), old.get("direct_max_mb")
            ),
            "auto_prefer": self._first_value(
                new.get("qq_auto_prefer"), old.get("auto_prefer")
            ),
            "url_max_mb": self._first_value(new.get("qq_url_max_mb"), old.get("url_max_mb")),
            "max_upload_mb": self._first_value(
                new.get("qq_max_upload_mb"), old.get("max_upload_mb")
            ),
            "on_failure": self._first_value(
                new.get("qq_on_failure"), old.get("on_failure")
            ),
        }

    @staticmethod
    def _first_value(*values):
        """取第一个非空值（0 / False 是合法配置，不能按假值跳过）。"""
        for value in values:
            if not is_blank(value):
                return value
        return None

    def _migrate_bucket_config(self) -> None:
        """v1.2.x 升级迁移（幂等）：image_bed 组里的储存桶模式与字段搬到
        独立的 storage_bucket 组。

        v1.3.0 起 image_bed.mode 不再含 cloudflare_r2 / oracle_oci（二者
        移入 storage_bucket 组），老配置只在 image_bed 组里存在，启动时
        搬一次；storage_bucket 已有 mode 时只清理残留不覆盖用户新配置。
        """
        bed = self.config.get("image_bed")
        if not isinstance(bed, dict):
            return
        legacy_mode = str(bed.get("mode") or "").strip()
        if legacy_mode not in BUCKET_MODES:
            return
        bucket = self.config.get("storage_bucket")
        if not isinstance(bucket, dict):
            bucket = {}
            self.config["storage_bucket"] = bucket
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
            self.config.save_config()
        except Exception as e:
            # 保存失败只影响持久化：本次会话已用迁移后的内存配置，
            # 重启后同一条迁移会再次执行（幂等）
            logger.warning(f"储存桶配置迁移写入失败（重启后会自动重试）: {e}")
        logger.info(
            f"已把 v1.2.x 图床配置中的储存桶设置（{legacy_mode}）迁移到 "
            "storage_bucket 组，image_bed 组回归图床模式 local。"
        )

    def _bucket_config(self) -> dict | None:
        """储存桶配置完整时返回可并入图床配置的 dict（含 mode），否则 None。

        完整 = mode 已选 + 必填项齐全 + 公开直链条件满足（R2 需
        r2_public_base_url，OCI 需公共桶或自定义公开地址二选一）：
        缺任一项上传都必然回退本地副本，优先于图床只会白费一次必败上传。
        """
        raw = self._dict_cfg("storage_bucket")
        mode = str(raw.get("mode") or "").strip()
        if mode not in BUCKET_MODES:
            return None
        missing = [
            f"storage_bucket.{k}" for k in BUCKET_REQUIRED[mode] if is_blank(raw.get(k))
        ]
        if (
            mode == "oracle_oci"
            and not truthy(raw.get("oci_public_bucket"))
            and is_blank(raw.get("oci_public_base_url"))
        ):
            missing.append(
                "storage_bucket.oci_public_bucket / oci_public_base_url（公开直链二选一）"
            )
        if missing:
            logger.warning(
                f"储存桶 {mode} 配置不完整（缺 {'、'.join(missing)}），登记原图将改用图床设置。"
            )
            return None
        cfg = dict(raw)
        cfg["mode"] = mode
        return cfg

    def _effective_bed_cfg(self) -> dict:
        """合并储存桶与图床两组配置：储存桶配置完整时优先（覆盖 mode 与字段）。"""
        bed = dict(self._dict_cfg("image_bed"))
        bucket = self._bucket_config()
        if bucket is not None:
            bed.update(bucket)
        return bed

    def _bed_mode(self) -> str:
        """生效的存储模式（storage_bucket 优先合并后的图床配置）。"""
        return str(self._bed_cfg.get("mode") or "local")

    def _storage_line(self) -> str:
        """/溯源状态 的存储方式一行：标明生效来源（储存桶优先于图床）。"""
        bucket_mode = str(self._dict_cfg("storage_bucket").get("mode") or "").strip()
        bed_mode = str(self._dict_cfg("image_bed").get("mode") or "local")
        active = self._bed_mode()
        if bucket_mode in BUCKET_MODES:
            if active == bucket_mode:
                return f"· 存储方式：储存桶 {bucket_mode}（优先于图床 {bed_mode}）"
            return f"· 存储方式：图床 {active}（储存桶 {bucket_mode} 配置不完整，未生效）"
        return f"· 存储方式：图床 {active}"

    def _engine_choice(self) -> str:
        """当前检索引擎：hash | vector | auto（vector 优先，失败/未命中回退 hash）。"""
        choice = str(self._cfg("search_engine") or "auto").strip().lower()
        return choice if choice in ("hash", "vector", "auto") else "auto"

    def _vector_index_on_register(self) -> bool:
        value = self._dict_cfg("vector_search").get("vector_index_on_register")
        if is_blank(value):
            return True
        return truthy(value)

    def _scan_dirs(self) -> list:
        raw = self._cfg("scan_dirs")
        if isinstance(raw, str):  # 手工配置成单个路径字符串时按一个目录处理
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [d.strip() for d in raw if isinstance(d, str) and d.strip()]

    # ------------------------------------------------------------------
    # 图片提取与解析
    # ------------------------------------------------------------------

    @staticmethod
    def _image_identity(seg) -> set:
        """图片段可用于去重的标识集合（url / file / path 全部收集）。

        同一张图在消息链与被引用消息里往往只带其中一部分字段（链里是 url、
        Reply.chain 里只有 file），只取其一就会把同一张图判成两张：/溯源 于是
        对同一张图跑两遍、回传两遍，表现为「图片被重复发送」。
        """
        keys = set()
        for name in ("url", "file", "path"):
            value = getattr(seg, name, None)
            if value:
                keys.add(str(value))
        return keys

    @classmethod
    def _extract_images(cls, event: AstrMessageEvent) -> list:
        """提取消息链与被引用消息中的图片段（按链接/文件名去重）。"""
        found: list = []
        seen: set = set()

        def push(seg) -> None:
            keys = cls._image_identity(seg) or {f"id:{id(seg)}"}
            if keys & seen:
                return
            seen.update(keys)
            found.append(seg)

        for seg in event.message_obj.message or []:
            if isinstance(seg, Comp.Image):
                push(seg)
            elif isinstance(seg, Comp.Reply):
                for sub in getattr(seg, "chain", None) or []:
                    if isinstance(sub, Comp.Image):
                        push(sub)
        return found

    async def _resolve_local_file(self, seg) -> tuple[str, bool, str]:
        """把图片段解析为本地文件。返回 (路径, 是否临时文件, 失败提示)。

        优先走 AstrBot 内置媒体解析 convert_to_file_path()，产物先经
        image_file_ok 校验。校验失败时分两种情况：

        - 认不出任何图片容器（多半是图床返回的几十~几百字节错误体、或
          rkey 过期/防盗链拦下的响应）→ 才值得走自带下载兜底；
        - 已经认得出容器却解不开（典型是缺解码器的 HEIC、被截断的文件）
          → 内容本身有问题，链接指向同一张图，重下不会有不同结果，直接带
          具体原因返回，省掉一次注定失败的请求与一对误导性日志。

        失败提示为空串表示解析成功。
        """
        try:
            path = await seg.convert_to_file_path()
            if path and os.path.isfile(path):
                if await asyncio.to_thread(image_file_ok, path):
                    return path, False, ""
                summary, sniffed = await asyncio.to_thread(_describe_bad_image, path)
                if sniffed is not None:
                    logger.warning(
                        f"内置解析产物无法解码（{summary}，容器格式 {sniffed}），"
                        "链接指向同一内容，跳过兜底下载"
                    )
                    return "", False, self._decode_fail_message(sniffed)
                logger.warning(f"内置解析产物不是有效图片（{summary}），改走兜底下载")
        except Exception as e:
            logger.debug(f"convert_to_file_path 失败，尝试兜底下载: {e}")

        url = getattr(seg, "url", None)
        if url and str(url).startswith(("http://", "https://")):
            try:
                path = await self._download(str(url))
            except Exception as e:
                logger.warning(f"图片兜底下载失败: {e}")
                return "", False, self._download_fail_message()
            if await asyncio.to_thread(image_file_ok, path):
                return path, True, ""
            summary, _ = await asyncio.to_thread(_describe_bad_image, path)
            logger.warning(
                f"兜底下载内容不是有效图片（{summary}），链接可能已过期或被防盗链拦截"
            )
            self._remove_quiet(path)
        return "", False, self._download_fail_message()

    @staticmethod
    def _download_fail_message() -> str:
        return (
            "⚠️ 未能获取图片内容：图片链接可能已过期（QQ 图床签名失效）或被防盗链拦截，"
            "请重新发送、或引用该图片后再试。"
        )

    @staticmethod
    def _decode_fail_message(sniffed: str) -> str:
        """认得出容器却解不开时的提示；HEIC 缺解码器是最常见的一种。"""
        if sniffed == "HEIF" and not heif_available():
            return (
                "⚠️ 检测到 HEIC/HEIF 图片（iPhone 默认格式），但服务端未安装 pillow-heif "
                "解码支持，无法溯源。安装该依赖后重启机器人即可。"
            )
        return (
            f"⚠️ 未能解析图片内容：识别为 {sniffed} 但解码失败，"
            "文件可能已损坏或格式不受当前服务端支持，详情见机器人日志。"
        )

    def _download_limit_bytes(self) -> int:
        """回传兜底下载的大小上限（字节）：limits.max_download_mb。"""
        return max(1, self._int_cfg("max_download_mb", 20)) * 1024 * 1024

    @staticmethod
    def _download_timeout() -> aiohttp.ClientTimeout:
        """兜底下载/格式体检统一的出网超时。"""
        return aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)

    async def _stream_response_to_temp(self, resp, limit: int) -> str:
        """把已打开的响应流式写入临时文件，返回路径；超限或失败即删文件。

        临时文件名完全不受外部输入影响（固定扩展名），图片解码由 Pillow
        按内容判断，与扩展名无关。
        """
        path = os.path.join(self.tmp_dir, f"q_{secrets.token_hex(8)}.jpg")
        try:
            received = 0
            with open(path, "wb") as file:
                async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                    received += len(chunk)
                    if received > limit:
                        raise ValueError("图片超过大小上限")
                    file.write(chunk)
        except Exception:
            self._remove_quiet(path)
            raise
        return path

    async def _download(self, url: str, *, size_limit: int | None = None) -> str:
        """下载到临时文件，返回路径。

        size_limit 供「分片上传」这类明确知道上限的调用方覆盖默认值：QQ 分片
        通道允许 200MB，而 max_download_mb（默认 20MB）是给回传兜底用的，按它
        截断会把本可以发出去的大图判成「超过大小上限」。缺省 None 时仍按配置。
        """
        client = await self._get_http()
        limit = (
            size_limit
            if size_limit is not None and size_limit > 0
            else self._download_limit_bytes()
        )
        async with await client.request(
            "GET",
            url,
            # prepare 契约为三参（url, method, cross_origin），签名不符
            # 会在请求发出前就 TypeError
            prepare=self._image_download_headers,
            timeout=self._download_timeout(),
        ) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > limit:
                raise ValueError("图片超过大小上限")
            return await self._stream_response_to_temp(resp, limit)

    async def _download_bytes(self, url: str) -> bytes:
        """下载到内存；声明体积超过内存阈值时退回落盘读回。

        回退交付只需要字节（压缩、发送都在内存里完成）。内存占用由两道闸门
        控制：声明长度超过 MEMORY_DOWNLOAD_MAX_BYTES 时改走落盘；声明长度未知
        （分块传输）时仍读内存，但受 max_download_mb 上限约束——阈值取 8MB 而非
        上限值，正是为了让「未知长度」这种情况也留在可控范围内（并发上限 3，
        最坏约 24MB）。大图分支直接复用当前已打开的响应落盘，不再发第二次 GET。
        """
        client = await self._get_http()
        size_limit = self._download_limit_bytes()
        async with await client.request(
            "GET", url, prepare=self._image_download_headers, timeout=self._download_timeout()
        ) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > size_limit:
                raise ValueError("图片超过大小上限")
            if declared.isdigit() and int(declared) > MEMORY_DOWNLOAD_MAX_BYTES:
                # 大图不冒险占内存：复用当前响应落盘后读回
                path = await self._stream_response_to_temp(resp, size_limit)
                try:
                    return await asyncio.to_thread(read_bytes, path)
                finally:
                    self._remove_quiet(path)
            buffer = io.BytesIO()
            received = 0
            async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                received += len(chunk)
                if received > size_limit:
                    raise ValueError("图片超过大小上限")
                buffer.write(chunk)
            return buffer.getvalue()

    @staticmethod
    def _image_download_headers(u: str, _m: str, _c: bool) -> dict:
        """图片兜底下载的请求头。

        NTQQ（multimedia.nt.qq.com.cn）与旧版 gchat.qpic.cn 图床均启用了
        防盗链：缺 Referer 时会返回 403 或只有几十~几百字节的错误体。两者的
        防盗链规则按各自站点校验，因此 Referer 必须与图片同族——拿旧图床的
        Referer 去请求 NTQQ，反而会被判成跨站盗用。Referer 不是凭据，跨域
        重定向是否剥离交给 url_guard；重定向离开该域名后因 host 不匹配自然
        不再携带。
        """
        host = (urlparse(u).hostname or "").lower()
        if not is_qq_image_bed_host(host):
            return {}
        if host == "multimedia.nt.qq.com.cn" or host.endswith(".nt.qq.com.cn"):
            return {"headers": {"Referer": "https://multimedia.nt.qq.com.cn/"}}
        return {"headers": {"Referer": "https://gchat.qpic.cn/"}}

    @classmethod
    def _range_probe_headers(cls, u: str, m: str, c: bool) -> dict:
        """在下载请求头基础上加 Range: bytes=0-0，用于退化探测响应体长度。"""
        base = dict(cls._image_download_headers(u, m, c))
        headers = dict(base.get("headers") or {})
        headers["Range"] = "bytes=0-0"
        base["headers"] = headers
        return base

    async def _probe_url(
        self, url: str, cache: dict | None = None, *, allow_range: bool = True
    ) -> tuple[bool | None, int | None]:
        """轻量探测 URL 是否存在及其响应体长度，返回 (是否存在, 字节数)。

        两个用途：判定图床缩放 URL 是否真的比原图小（压缩标识的唯一一手
        证据），以及 /原图 按文件名拼出的直链是否真的存在于图床。HEAD 优先，
        不支持时退化为只取 0-0 一个字节的 Range GET（仅限不带缩放参数的 URL：
        ImgBed 明确拒绝「Range + 缩放」的组合，退化为 Range 只会白花一次请求）。
        任何网络异常都返回 (None, None)——调用方必须把「探测不出来」与「确定
        不存在」分开处理，网络抖动不该被当成「图床没有这张图」。
        """
        if cache is not None and url in cache:
            return cache[url]
        result = await self._probe_url_uncached(url, allow_range=allow_range)
        if cache is not None:
            cache[url] = result
        return result

    async def _probe_url_uncached(
        self, url: str, *, allow_range: bool = True
    ) -> tuple[bool | None, int | None]:
        """探测实现。探测用 PROBE_TIMEOUT 而非下载超时：探测只读响应头，
        此前共用 30s 会让一次卡住的探测把交付拖到分钟级。

        缩放 URL（allow_range=False）的 HEAD 405 特殊处理：CloudFlare-ImgBed
        的缩放 API 只接受 GET 不接受 HEAD，405 不等于「缩放不支持」。
        遇到 405 时回退到 GET（不带 Range）再探一次，确认缩放功能是否真的不可用。
        """
        client = await self._get_http()
        timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT)

        # ---------- 第一步：HEAD 探测 ----------
        head_status: int | None = None
        try:
            async with await client.request(
                "HEAD", url, prepare=self._image_download_headers, timeout=timeout
            ) as resp:
                head_status = resp.status
                # 405 先不记冷却：它可能只是 HEAD 方法不被允许，而不是缩放
                # 功能本身不可用，下面回退 GET 再确认。
                if (
                    not allow_range
                    and resp.status != 405
                    and is_scaling_capability_rejection(resp.status)
                ):
                    self._note_scaled_unsupported(resp.status)
                exists = classify_probe_status(resp.status)
                if exists is False:
                    return False, None
                length = content_length_from_headers(resp.headers)
                if exists is True and length is not None:
                    return True, length
                if not allow_range and resp.status != 405:
                    # 非 405 的缩放探测失败（400/501/网络问题等）：不回退，
                    # 400/501 已在上文记冷却，其余按「无法判定」处理。
                    return None, None
        except Exception as exc:
            logger.debug(f"HEAD 探测失败 {url}: {exc}")
            if not allow_range:
                return None, None

        # ---------- 缩放 URL 的 405 回退：GET（不带 Range） ----------
        if not allow_range and head_status == 405:
            try:
                async with await client.request(
                    "GET", url, prepare=self._image_download_headers, timeout=timeout
                ) as resp:
                    # 只读响应头，不读响应体
                    if is_scaling_capability_rejection(resp.status):
                        # GET 也被拒绝：这才是真的缩放能力缺失
                        self._note_scaled_unsupported(resp.status)
                        return None, None
                    exists = classify_probe_status(resp.status)
                    length = content_length_from_headers(resp.headers)
                    if exists is True and length is not None:
                        return True, length
                    return None, None
            except Exception as exc:
                logger.debug(f"缩放 URL GET 回退探测失败 {url}: {exc}")
                return None, None

        # ---------- 普通 URL 的 Range GET 回退 ----------
        try:
            async with await client.request(
                "GET", url, prepare=self._range_probe_headers, timeout=timeout
            ) as resp:
                # 只读响应头，绝不读响应体：Range 被忽略而返回 200 时，
                # 完整读下来就等于把整张图下载了一遍，探测也就失去了意义
                return classify_probe_status(resp.status), content_length_from_headers(
                    resp.headers
                )
        except Exception as exc:
            logger.debug(f"URL 探测失败（无法判定存在性）{url}: {exc}")
        return None, None

    def _note_scaled_unsupported(self, status: int) -> None:
        """记录「图床处理不了缩放请求」，随后一段时间跳过缩放探测。"""
        self._scaled_verify_unsupported_until = (
            time.monotonic() + SCALED_VERIFY_COOLDOWN_SECONDS
        )
        logger.info(
            f"图床拒绝了缩放请求（HTTP {status}）：{SCALED_VERIFY_COOLDOWN_SECONDS} 秒内"
            "不再探测缩放版（配文相应不再标注「已压缩」）"
        )

    def _scaled_verify_cooling(self) -> bool:
        """是否处在「图床不支持缩放」的冷却期内。"""
        return time.monotonic() < self._scaled_verify_unsupported_until

    def _note_qq_url_unsupported(self, exc: BaseException) -> None:
        """记录「QQ 拉不到图床公网直链」，随后一段时间 auto 模式改走分片。

        与「图床拒绝缩放请求」同一套思路：公网域名存在不代表腾讯侧拉得到
        （域名停用、防盗链、防火墙、跨境链路），而这类失败每次都要白等一轮
        往返。失败一次就记冷却，冷却期内 auto 直接分片；固定 `url` 通道不受
        影响——用户显式钉死一条通道时就该照做，失败由 on_failure 决定。
        """
        self._qq_url_unsupported_until = time.monotonic() + QQ_URL_COOLDOWN_SECONDS
        logger.warning(
            f"QQ URL 上传失败：{QQ_URL_COOLDOWN_SECONDS} 秒内 auto 模式改走分片"
            f"（固定 url 通道不受影响）; {type(exc).__name__}: {exc}"
        )

    def _qq_url_cooling(self) -> bool:
        """是否处在「URL 上传通道不可用」的冷却期内。"""
        return time.monotonic() < self._qq_url_unsupported_until

    def _verify_scaled(self) -> bool:
        """delivery.napcat_verify_scaled：图床缩放是否实测校验，未配置时默认开启。"""
        raw = self._napcat_delivery_cfg().get("verify_scaled")
        return True if is_blank(raw) else truthy(raw)

    def _onebot_image_timeout(self) -> int:
        """delivery.napcat_timeout：OneBot image 段的 timeout 字段（秒）。

        该字段是协议端（NapCat 等）下载网络图片的窗口，不是插件的出网超时。
        未配置或填了非数字时用 common.ONEBOT_IMAGE_TIMEOUT；其余值收敛到
        [10, 300]——填 0 会落到 10 而不是原样写 0，否则等于告诉协议端「立刻
        放弃下载」，图片必然拉不到。
        """
        raw = self._napcat_delivery_cfg().get("onebot_image_timeout")
        return bounded_int(raw, ONEBOT_IMAGE_TIMEOUT, 10, 300)

    async def _get_http(self) -> GuardedHttpClient:
        if self._http is None:
            # 连接器启用 IP 钉扎：出网连接只允许落在 url_guard 校验过的地址上
            self._http = GuardedHttpClient()
        return self._http

    def _cleanup_tmp(self) -> None:
        """清理超过保留时限的临时文件（插件加载与卸载时各执行一次）。"""
        try:
            now = time.time()
            for name in os.listdir(self.tmp_dir):
                path = os.path.join(self.tmp_dir, name)
                if os.path.isfile(path) and now - os.path.getmtime(path) > TMP_MAX_AGE_SECONDS:
                    os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _remove_quiet(path: str) -> None:
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    def _delivery_config(self) -> tuple[str, int, int, str]:
        return delivery_settings(self._napcat_delivery_cfg())

    def _random_settings(self) -> dict:
        """归一化随机图配置组（random_media）。

        与其它配置组同源：数值用 common 的容错解析、布尔用真值表、空串按未
        配置处理。缺 base_url 时不在这里拦截，交由 RandomMediaClient 抛
        RandomMediaError，使“未配置”提示集中在一处。

        default_dir 支持单个目录或用 `,` 分隔的目录池：图床的 /random 只收单一
        dir，池用于「不指定目录时尽量覆盖整库」——见 pick_default_directory。

        lan_base_url（内网直连）非空时：随机接口改走内网源，其主机注册进
        url_guard 的私网白名单（仅豁免该主机的私网地址拒绝，其它 SSRF 校验
        照旧）；每次归一化都重注册，配置改动后热重载即可生效。
        """
        raw = self._dict_cfg("random_media")
        lan_base = str(raw.get("lan_base_url") or "").strip()
        lan_host = ""
        if lan_base:
            parsed_lan = urlsplit(lan_base)
            if parsed_lan.scheme in ("http", "https") and parsed_lan.netloc:
                lan_host = (parsed_lan.hostname or "").lower()
            else:
                logger.warning(f"random_media.lan_base_url 不是有效的 http(s) 地址，已忽略: {lan_base}")
                lan_base = ""
        set_allowed_private_hosts({lan_host} if lan_host else ())
        timeout = as_float(raw.get("timeout"), 10.0)
        if timeout <= 0:
            # 0/负数会让请求立即超时，回退默认值
            timeout = 10.0
        return {
            "base_url": str(raw.get("base_url") or "").strip(),
            "lan_base_url": lan_base,
            "api_endpoint": str(raw.get("api_endpoint") or RANDOM_ENDPOINT_DEFAULT).strip()
            or RANDOM_ENDPOINT_DEFAULT,
            "api_token": str(raw.get("api_token") or "").strip(),
            "default_dir": str(raw.get("default_dir") or "").strip(),
            "timeout": timeout,
            "retry_count": bounded_int(raw.get("retry_count"), 3, 0, 10),
            "show_file_info": bool_or(raw.get("show_file_info"), True),
            "enable_llm": bool_or(raw.get("enable_llm"), True),
            "send_thumbnail": bool_or(raw.get("send_thumbnail"), True),
        }

    @staticmethod
    def _lan_url_hosts(settings: dict) -> tuple[str, ...]:
        """内网直连模式下允许改写源的主机集合：内网 base 与公网 base 的主机名。

        图床自己产出的直链只可能落在这两个主机上（/random 按请求 Host 生成、
        Qdrant 登记的是公网域名），命中才改写成内网源，第三方链接不受影响。
        """
        lan = str((settings or {}).get("lan_base_url") or "").strip()
        if not lan:
            return ()
        hosts = [urlsplit(lan).hostname or ""]
        base_host = urlsplit(str((settings or {}).get("base_url") or "")).hostname
        if base_host:
            hosts.append(base_host)
        return tuple(host for host in hosts if host)

    @staticmethod
    def _delivery_target(event: AstrMessageEvent) -> DeliveryTarget:
        """当前事件的推送目标画像——平台判定的唯一入口。

        此前「能不能 OneBot 直发」「媒体是否经第三方转存」「能不能渲染原生 At」
        是三个各自去问 get_platform_name() 的静态方法，同一次推送里被反复问、
        也没有任何地方能一眼看出这些能力之间的约束关系。现在统一由这里构造，
        各分支只读字段（见 image_delivery.DeliveryTarget）。
        """
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:
            platform = ""
        return delivery_target(
            platform, has_bot=getattr(event, "bot", None) is not None
        )

    def _random_client(self) -> RandomMediaClient:
        """惰性构造随机图客户端；配置在插件加载时读入，改动后重载生效。"""
        if self._random_media_client is None:
            self._random_media_client = RandomMediaClient(self._random_settings(), self._get_http)
        return self._random_media_client

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        """会话唯一标识，用于隔离 /原图 的图片历史。"""
        return str(getattr(event, "unified_msg_origin", None) or "default")

    def _remember_blocks(self, event: AstrMessageEvent, blocks: list[dict]) -> None:
        """把本次回传的原图直链记入会话历史，供 /原图 找回。

        /溯源 的哈希与向量两条路径、以及 /随机图 现在都经 _yield_delivery
        回传（向量命中额外带 mark_compressed=True 生成逐图配文），因此只需在
        这一个入口收口，避免在各命令里分别维护历史而漏记。

        指向缩略图的直链**一律不入历史**：历史是 /原图 的取值来源，一旦记进
        缩略图，此后 /原图 只能拿回那张 720px 小图。/随机图 的发送路径本就
        通过 remember_blocks 传入原图（见 _random_media_delivery），这里是
        第二道闸，保证任何调用点都不会把替身写进历史。
        """
        session = self._session_key(event)
        for block in blocks:
            url = str(block.get("url") or "").strip()
            if not url:
                continue
            if is_thumbnail_url(url):
                logger.debug(f"跳过缩略图直链，不计入 /原图 历史: {url}")
                continue
            self.history.remember(
                session, url, display_name=clean_display_name(block.get("file_name"))
            )

    def _can_send_via_onebot(self, event: AstrMessageEvent) -> bool:
        """能否用 OneBot 原生接口直发（图片字节/URL 段、纯文本）。

        保留为薄封装：判定已收口到 _delivery_target（aiocqhttp 且 bot 就绪）。
        """
        return self._delivery_target(event).direct_send

    def _mention_prefix(self, event: AstrMessageEvent) -> list:
        """回传链开头的「@发送者」段；仅能渲染原生 At 的平台启用。

        qq_official 等平台没有原生 At 段：适配器会把 At 转成平台标记原文
        （`<qqbot-at-user id="..."/>`）拼进纯文本，而官方群消息的纯文本并不
        渲染该标记（仅 markdown 消息类型生效），用户看到的就是一串“代码”
        糊在文案前。故 @ 前缀只给能原生渲染它的 aiocqhttp，其余平台不加。
        """
        if not self._delivery_target(event).native_mention:
            return []
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            return []
        return [Comp.At(qq=sender)] if sender else []

    @staticmethod
    def _event_already_sent(event: AstrMessageEvent) -> bool:
        """本次事件是否已经投递过图片（事件级去重盾）。"""
        return bool(getattr(event, "_image_trace_sent", False))

    @staticmethod
    def _mark_event_sent(event: AstrMessageEvent) -> None:
        """标记本次事件已投递图片，禁止后续任何回退路径再发一次。"""
        try:
            event._image_trace_sent = True
        except AttributeError:  # 少数事件实现带 __slots__，标记失败不影响发送
            logger.debug("事件对象不支持动态属性，跳过已发送标记")

    @staticmethod
    def _vector_caption(block: dict, evidence: CompressionEvidence) -> str:
        """按压缩证据拼配文。

        只有拿到证据才写「已压缩」：此前按「URL 被改写」推断压缩，而图床在
        图片不超 max_side、格式不可处理或图像处理不可用时会按 fallback=original
        原样返回，于是出现「文字说已压缩、用户收到的却是原图」。UNKNOWN 只给
        一句 /原图 用法提示，ORIGINAL 连提示都不给——发的本来就是原图。
        """
        fields = []
        if evidence is CompressionEvidence.COMPRESSED:
            fields.append("已压缩，可发送 /原图 获取原图")
        elif evidence is CompressionEvidence.UNKNOWN:
            fields.append("可发送 /原图 获取原图")
        file_name = clean_display_name(block.get("file_name")) or str(
            block.get("file_name") or ""
        ).strip()
        if file_name:
            fields.append(file_name)
        fields.extend(str(field) for field in block.get("fields", []) if field)
        prefix = str(block.get("prefix") or "").strip()
        caption = " · ".join(fields)
        return f"{prefix} {caption}".strip() if prefix else caption

    async def _send_via_onebot(
        self, event: AstrMessageEvent, header: str, blocks: list[dict], payloads: list[str | bytes]
    ) -> SendOutcome:
        """通过 OneBot 原生接口直发消息，消息段按「文字集中 → 图片集中」排列。

        文字在前、图片在后是**必须**的，不是排版偏好：QQ 客户端会把
        「text 段 + 紧邻其后的 image 段」合并渲染成一个图文混排卡片，卡片内的
        图片按行内富文本插图处理，**一律裁成正方形缩略图**（无论原图多高）。
        此前按「标题 →（配文 → 图）…」图文交替排列，多图时每一张图都恰好紧跟在
        一段文字后面，于是几张图全是正方形缩略图；只有排在最后、后面没有更多
        文字重新分组的那张偶尔能拿到正确尺寸——线上报的「前三张方、第四张正常」
        就是这个渲染分组的结果。把全部 text 段提到最前、image 段连续排在末尾后，
        QQ 会把连续图片段按普通大图渲染，宽高比得以保留。

        配文因此不再紧贴各自的图片，改为在配文里前置序号（block["prefix"]，如
        「1.」「2.」），与图片的先后顺序一一对应，用户仍能分辨哪句配文属于哪张图。

        payloads 与 blocks 中的有图 block 同序一一对应，元素为 URL 字符串（原样
        作 data.file）或图片字节（编码为 base64:// 段，压缩结果直达 QQ、不经
        协议端二次下载）。调用方（_yield_delivery 的两个直发分支、
        _send_prompt）已保证数量相等，故这里按遍历到的有图 block 依次取用，
        无需再做长度校验。

        image 段带 OneBot 标准的 `timeout` 字段（秒）：URL 段由协议端自行下载，
        大图 + 多图时下载耗时会让 NapCat 的 sendMsg 回调窗口吃紧（实测超时形态
        见 classify_send_error 注释）。显式放宽该窗口可减少「结果未知」。base64
        段不经下载，带该字段无副作用。

        返回三态而非布尔：只有协议端明确回报失败（FAILED）才允许换通道重发。
        超时等 UNKNOWN 情形请求可能已经送达，调用方必须先看返回值再决定是否
        回退，否则同一张图会被发两遍。
        """
        message: list[dict] = []
        sender_id = event.get_sender_id()
        if sender_id:
            message.append({"type": "at", "data": {"qq": str(sender_id)}})

        # 1) 全部文字段集中在最前：标题一段，随后每张图各一段配文。
        #    连续 text 段之间不会再插入 image 段，QQ 不会把它们拆成图文卡片。
        text_lines: list[str] = []
        if header:
            text_lines.append(header)
        for block in blocks:
            text = str(block.get("text") or "").strip()
            if text:
                text_lines.append(text)
        if text_lines:
            # 单段承载全部文字：段数越少，QQ 越不容易按「文字+图」重新分组
            message.append({"type": "text", "data": {"text": "\n".join(text_lines)}})

        # 2) 全部图片段连续排在末尾：连续 image 段按普通大图渲染，保留宽高比。
        image_timeout = self._onebot_image_timeout()
        pending_payloads = iter(payloads)
        for block in blocks:
            if not (block.get("url") or block.get("path")):
                continue
            payload = next(pending_payloads)
            if isinstance(payload, bytes):
                payload = "base64://" + base64.b64encode(payload).decode()
            message.append(
                {
                    "type": "image",
                    "data": {"file": payload, "timeout": image_timeout},
                }
            )

        params: dict = {"message": message}
        group_id = event.get_group_id()
        if group_id:
            action = "send_group_msg"
            params["group_id"] = int(group_id) if str(group_id).isdigit() else group_id
        else:
            action = "send_private_msg"
            user_id = sender_id
            params["user_id"] = int(user_id) if str(user_id).isdigit() else user_id
        self_id = getattr(event.message_obj, "self_id", None)
        if self_id:
            params["self_id"] = self_id

        image_count = sum(1 for seg in message if seg["type"] == "image")
        try:
            await event.bot.call_action(action, **params)
        except Exception as exc:
            outcome = classify_send_error(exc)
            # 日志按成因给结论：不再只写「结果未知」，而是讲清「请求已发出、
            # 未拿到回执、因此按不重发处理」以及成因与排查方向。三态判定与
            # 回退决策一字未改，这里只换表达。
            logger.warning(
                f"OneBot 图片直发（{action}，{image_count} 张）：{send_diagnosis(exc)}"
            )
            logger.debug(
                f"OneBot 直发异常原始信息: {type(exc).__name__} -> {outcome.value}; "
                f"retcode={getattr(exc, 'retcode', None)}; echo={getattr(exc, 'echo', None)}; {exc}"
            )
            return outcome
        logger.info(f"OneBot 图片直发成功: {action}（{image_count} 张）")
        return SendOutcome.SENT

    async def _send_prompt(self, event: AstrMessageEvent, text: str) -> bool:
        """发送一条纯文本提示，返回是否已由 OneBot 原生处理。

        OneBot（aiocqhttp）下走原生 call_action 单发文本，命中提示就不会跟
        图片挤在同一条消息里；其余平台返回 False，由调用方 yield plain_result
        兜底。结果未知（超时）也按「已处理」返回：提示重复一遍无伤大雅，但
        让它再走一次消息链毫无意义。
        """
        if self._can_send_via_onebot(event):
            return await self._send_via_onebot(event, text, [], []) is not SendOutcome.FAILED
        return False

    def _mark_compressed_captions(
        self, blocks: list[dict], evidences: list[CompressionEvidence]
    ) -> None:
        """为 mark_compressed 路径写入逐图配文。

        evidences 与 blocks 中的有图 block 一一对应（同序于 URL 直传计划或
        _prepare_delivery_blocks 的输出）；无图 block（例如没有可用直链的条目）
        按 UNKNOWN 处理，但它也要拿到配文，才不会在合并消息里消失。仅在
        mark_compressed 为真时调用，默认路径不触碰 block["text"]。
        """
        index = 0
        for block in blocks:
            if block.get("url") or block.get("path"):
                evidence = evidences[index]
                index += 1
            else:
                evidence = CompressionEvidence.UNKNOWN
            block["text"] = self._vector_caption(block, evidence)

    async def _load_delivery_bytes(self, block: dict, *, validate: bool = True) -> bytes | None:
        """取回传要用的图片字节。

        validate=False 供本地压缩路径使用：压缩本身就要完整解码一次，再提前
        校验等于同一次解码做两遍（实测 4000x3000 JPEG 白花 78ms、外加一次
        全尺寸位图内存）。压缩失败时调用方再用 image_bytes_ok 判定「图本身
        坏」还是「这个格式不适合重编码」，坏图仍会被拦下。
        """
        url = str(block.get("url") or "")
        path = str(block.get("path") or "")
        try:
            if url:
                data = await self._download_bytes(url)
                if validate and not await asyncio.to_thread(image_bytes_ok, data):
                    return None
                return data
            if not path:
                return None
            if validate and not await asyncio.to_thread(image_file_ok, path):
                return None
            return await asyncio.to_thread(read_bytes, path)
        except Exception as exc:
            logger.warning(f"读取回传图片失败: {exc}")
            return None

    async def _prepare_delivery_blocks(
        self, blocks: list[dict], *, compress: bool
    ) -> list[tuple[bytes, bool]] | None:
        """准备要直发的字节，逐图压缩在并发上限内并行执行。

        顺序必须与传入的 image_blocks 一致（调用方按下标取用），因此用
        gather 收集结果而不是 as_completed。任一张失败即整体返回 None，
        调用方据此改走 URL 链——与逐张串行时的语义一致。
        """
        mode, max_side, quality, _ = self._delivery_config()
        target_bytes, use_webp = self._encode_options()
        semaphore = asyncio.Semaphore(COMPRESS_CONCURRENCY)

        async def prepare_one(block: dict) -> tuple[bytes, bool] | None:
            async with semaphore:
                data = await self._load_delivery_bytes(block, validate=not compress)
                if data is None:
                    return None
                if not compress or mode == "url":
                    return data, False
                result = await asyncio.to_thread(
                    prepare_image_bytes,
                    data,
                    max_side,
                    quality,
                    target_bytes=target_bytes,
                    use_webp=use_webp,
                )
                if result is not None:
                    return result
                if await asyncio.to_thread(image_bytes_ok, data):
                    # 压缩失败但图片本身可用（格式不支持重编码等）：回退原字节
                    logger.warning(
                        f"图片本地压缩失败，回退原字节: 格式={sniff_image_format(data) or '未知'}, "
                        f"大小={len(data) / 1024:.0f}KB"
                    )
                    return data, False
                if sniff_image_format(data) == "AVIF":
                    # Pillow<11.3 无 AVIF 解码器：探测、下载后既压不了也验不了，
                    # 只能让协议端自己拉 URL。给出可操作的提示而不是笼统的失败。
                    logger.warning(
                        f"AVIF 图片当前环境无法解码（Pillow 版本过低），改用 URL 发送: "
                        f"大小={len(data) / 1024:.0f}KB；升级 Pillow>=11.3 可启用 AVIF 本地压缩"
                    )
                    return None
                logger.warning(
                    f"回退图片无法解码，改用 URL 发送: 格式={sniff_image_format(data) or '未知'}, "
                    f"大小={len(data) / 1024:.0f}KB"
                )
                return None

        prepared = await asyncio.gather(
            *(prepare_one(block) for block in blocks if block.get("url") or block.get("path"))
        )
        if any(item is None for item in prepared):
            return None
        return list(prepared)

    async def _local_compress_delivery(
        self,
        event: AstrMessageEvent,
        header: str,
        blocks: list[dict],
        image_blocks: list[dict],
        *,
        mark_compressed: bool,
    ) -> SendOutcome | None:
        """auto / bytes 模式下 OneBot 的混合载荷直发。

        逐图决定 URL 段还是 base64 段：已知小图（≤COMPRESS_MIN_BYTES 且协议端
        能解析宽高）跳过下载、直发原图 URL——本地压缩本就会跳过这类图，下载再
        base64 回发纯属浪费一次往返；其余并发下载+压缩后内联 base64，压缩结果
        不经协议端二次下载，100% 到达 QQ（QQ 协议端会剥离 URL 查询参数，缩放
        URL 直传拿回的常是原图）。未压缩且超内联上限的巨型图（动图等）退回
        URL 段，避免单条消息过大。

        返回 None 表示准备失败（任一张下载/解码失败），调用方整批改走 URL 链；
        否则返回 _send_via_onebot 的三态结果。
        """
        prepare_blocks = [block for block in image_blocks if not prefer_url_direct(block)]
        prepared = await self._prepare_delivery_blocks(prepare_blocks, compress=True)
        if prepared is None:
            return None

        payloads: list[str | bytes] = []
        evidences: list[CompressionEvidence] = []
        pending = iter(prepared)
        for block in image_blocks:
            url = str(block.get("url") or "")
            if prefer_url_direct(block):
                payloads.append(build_original_url(url) or url)
                evidences.append(CompressionEvidence.ORIGINAL)
                continue
            data, compressed = next(pending)
            if prefer_inline_bytes(len(data), compressed, url):
                payloads.append(data)
            else:
                payloads.append(build_original_url(url) or url)
            evidences.append(
                CompressionEvidence.COMPRESSED if compressed else CompressionEvidence.ORIGINAL
            )

        if mark_compressed:
            self._mark_compressed_captions(blocks, evidences)
        elif any(evidence is CompressionEvidence.COMPRESSED for evidence in evidences):
            header += "（已压缩）"
        return await self._send_via_onebot(event, header, blocks, payloads)

    def _encode_options(self) -> tuple[int, bool]:
        """本地压缩进阶选项：目标体积上限（字节，0 表示不限制）与 WebP 输出。"""
        raw = self._napcat_delivery_cfg()
        target_kb = bounded_int(raw.get("target_kb"), 0, 0, 10240)
        return target_kb * 1024, truthy(raw.get("webp"))

    @staticmethod
    def _known_bytes(block: dict) -> int:
        """命中条目里记录的原图体积（字节）；未知或非法一律返回 0。"""
        try:
            value = int(block.get("size_bytes") or 0)
        except (TypeError, ValueError):
            return 0
        return value if value > 0 else 0

    async def _url_delivery_plan(
        self, image_blocks: list[dict], max_side: int, mode: str,
        quality: int = 85, scaled_url_style: str = "query",
    ) -> tuple[list[str], list[CompressionEvidence]]:
        """决定每张图 URL 直传实际发哪个 URL，并给出压缩证据，两者同序对应。

        不再无条件改写 URL：只有最长边确实超过 max_side（或尺寸未知）时才追加
        图床缩放参数——不超过 max_side 的图，图床按 fallback=original 原样返回，
        追加参数只是白白触发一次处理。开启 verify_scaled 时进一步实测缩放版是否
        真的更小，这也是「已压缩」文案唯一的一手依据；实测不出来就按 UNKNOWN
        处理，配文宁可不写，也不能出现「文字说压缩、收到的是原图」。

        探测成本被压到最低：所有探测并发执行且有总预算，命中条目自带原图体积
        （size_bytes / file_size）时不必再探原图；图床明确拒绝缩放请求后进入
        冷却期，冷却期内直接跳过探测。
        """
        urls: list[str] = []
        sizes: list[tuple] = []
        jobs: list[tuple[str, str, int] | None] = []
        verify = (
            mode == "url-scaled" and self._verify_scaled() and not self._scaled_verify_cooling()
        )
        for block in image_blocks:
            original = build_original_url(str(block.get("url") or ""))
            size = (block.get("width"), block.get("height"))
            sizes.append(size)
            if mode != "url-scaled" or not original:
                urls.append(original or str(block.get("url") or ""))
                jobs.append(None)
                continue
            scaled = build_scaled_url(original, max_side, quality=quality, style=scaled_url_style)
            if is_scaling_needed(size, max_side) is False or scaled == original:
                # 尺寸证明不会被缩放，或这条直链本来就带不了缩放参数
                urls.append(original)
                jobs.append(None)
                continue
            urls.append(scaled)
            jobs.append((original, scaled, self._known_bytes(block)) if verify else None)

        verified = await self._verify_scaled_batch(jobs) if any(jobs) else {}
        evidences = [
            compression_evidence(sizes[index], max_side, verified_smaller=verified.get(index))
            for index in range(len(image_blocks))
        ]
        return urls, evidences

    async def _verify_scaled_batch(
        self, jobs: list[tuple[str, str, int] | None]
    ) -> dict[int, bool | None]:
        """并行实测「缩放版是否真的更小」，返回 {下标: 是否更小或 None}。

        已知原图体积（命中条目里的 size_bytes / file_size）时只探缩放版——
        少一次请求也少一次图床处理；未知时才把原图与缩放版一起并发探。
        超出总预算仍未返回的探测一律不判定（配文按「说不准」处理），
        绝不让探测无限期拖住发送。
        """
        semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)
        cache: dict = {}

        async def run(index: int, job: tuple[str, str, int]) -> tuple[int, bool | None]:
            original, scaled, known = job
            async with semaphore:
                if known:
                    _, scaled_size = await self._probe_url(scaled, cache, allow_range=False)
                    return index, (
                        None if scaled_size is None else scaled_size < known
                    )
                (_, original_size), (_, scaled_size) = await asyncio.gather(
                    self._probe_url(original, cache),
                    self._probe_url(scaled, cache, allow_range=False),
                )
                if original_size is None or scaled_size is None:
                    return index, None
                return index, scaled_size < original_size

        tasks = [
            asyncio.create_task(run(index, job))
            for index, job in enumerate(jobs)
            if job is not None
        ]
        if not tasks:
            return {}
        done, pending = await asyncio.wait(tasks, timeout=PLAN_TOTAL_TIMEOUT)
        for task in pending:
            task.cancel()
        if pending:
            # 取消后回收，避免留下「已取消但仍挂着」的任务对象
            await asyncio.gather(*pending, return_exceptions=True)
            logger.warning(
                f"探测超出 {PLAN_TOTAL_TIMEOUT}s 预算，{len(pending)} 张图的压缩标识按"
                "「无法判定」处理（不阻塞图片发送）"
            )
        result: dict[int, bool | None] = {}
        for task in done:
            try:
                index, value = task.result()
            except Exception as exc:  # 单张探测失败不影响其余判定
                logger.debug(f"缩放实测失败: {exc}")
                continue
            result[index] = value
        return result

    async def _yield_delivery(
        self,
        event: AstrMessageEvent,
        header: str,
        blocks: list[dict],
        *,
        mode_override: str | None = None,
        allow_local_fallback: bool = True,
        mark_compressed: bool = False,
        remember_blocks: list[dict] | None = None,
    ):
        """统一回传入口：按通道选主路径，失败后逐级回退。

        mode 取统一词表（auto / native / bytes / url / url-scaled，两平台同名
        同义，见 image_delivery.normalize_delivery_mode）：
        - auto（默认）：交给插件自动决定。OneBot 下压缩字节经 base64 段直发
          （混合载荷，已知小图跳过下载直发 URL），失败改走标准消息链 URL；
          qq_official 下按体积分流到 native/url/bytes。
        - bytes：一律本机准备好字节再上传（OneBot 压缩后直发；qq_official 分片）。
        - url / url-scaled：URL 直传优先（原图 / 图床缩放版），本地压缩只作为
          失败回退。
        - native：不加工，直接交给适配器原样发送（排障对照用）。

        OneBot 侧仍默认压缩：QQ 协议端（NapCat 等）会剥离 URL 查询参数，缩放
        URL 直传拿回的常是原图，本地压缩是唯一能保证压缩 100% 生效的路径。

        mode_override 供 /原图 强制走 url、无视全局通道；
        allow_local_fallback=False 时彻底不进入下载字节/本地压缩分支。注意
        qq_official 等第三方转存平台例外：入口处的格式体检（_offbox_rewrite_blocks）
        可能把假格式图片改写为本地转码 JPEG 交付——官方富媒体上传拒收假格式，
        正确送达优先于「只发 URL」的提速承诺。

        mark_compressed 供 /溯源 向量命中启用：为真时按各回传路径实际的压缩
        证据写入 block["text"]（含文件名与「已压缩」用法说明），使合并消息里
        每张图各带一句配文。为假时一个字节都不碰 block——哈希 /随机图 /原图
        三处既有调用点行为与改造前一致（offbox 格式体检除外，见上）。

        qq_official 例外：格式体检之后还会按 `qq_upload` 组做上传通道分流，
        命中 url / url-scaled / bytes 时由 _qq_upload_takeover 自己上传并发消息
        （绕开适配器「图片恒 base64 直传、5MB 上限」）。该分支返回即视为已投递。

        单次发送保证：同一批图片在一次调用里只发起一次发送。只有 OneBot 直发
        明确失败（FAILED）才允许换通道重发；结果未知（超时）一律就此打住——
        请求可能已经送达，再发一次用户就会收到两张一模一样的图。

        remember_blocks 供「发缩略图、记原图」使用：/随机图 发出去的是替身
        （thumb_url），但会话历史必须留下原图直链，否则 /原图 只能找回那张 720px
        的缩略图。缺省 None 表示记的就是本次发出的 blocks（其余调用点行为不变）。
        """
        if self._event_already_sent(event):
            logger.warning("本次事件已投递过图片，跳过重复投递")
            return
        target = self._delivery_target(event)
        self._remember_blocks(event, blocks if remember_blocks is None else remember_blocks)
        if target.offbox_upload:
            # 第三方转存平台的两个前置动作（格式体检 → 上传通道接管）收在一个
            # 入口里：前者可能把假格式换成 path/缩略图 URL，后者必须拿着改写后
            # 的 block 才能判断该走 URL 还是分片，顺序不可交换
            blocks, handled = await self._offbox_pipeline(
                event, header, blocks, mark_compressed=mark_compressed
            )
            if handled:
                # 通道接管已自行发完消息：必须标记已发送并 stop_event，否则
                # respond stage 会照原路（base64 直传）再发一遍
                self._mark_event_sent(event)
                event.stop_event()
                return
        mode, max_side, quality, scaled_url_style = self._delivery_config()
        if mode_override is not None:
            mode = mode_override
        image_blocks = [block for block in blocks if block.get("url") or block.get("path")]
        raw_urls = [str(block["url"]) for block in image_blocks if block.get("url")]
        all_urls = bool(image_blocks) and len(raw_urls) == len(image_blocks)
        direct_mode = mode in {"url", "url-scaled"}
        # URL 直发条件：模式允许 + 全是 URL + 能走 OneBot + 所有 URL 扩展名
        # 能被协议端解析出宽高（否则 QQ 侧拿不到尺寸，体验差）。
        # url-scaled 模式下默认走 URL 直传；若 QQ 协议端会剥离查询参数导致
        # 图床返回原图，可把 scaled_url_style 改为 "cf-path"（路径式，参数
        # 嵌在路径里不会被剥离，但需图床域名开启 Cloudflare Image Resizing）。
        direct_ok = (
            direct_mode
            and all_urls
            and target.direct_send
            and (mode == "url" or all(is_napcat_parseable_url(u) for u in raw_urls))
        )

        # 回传计划最多只算一次：回退路径此前会重跑一遍规划，把同样的探测白做
        # 第二次（图床还会因此多做一次缩放处理）。
        plan: tuple[list[str], list[CompressionEvidence]] | None = None
        if direct_ok:
            plan = await self._url_delivery_plan(image_blocks, max_side, mode, quality=quality, scaled_url_style=scaled_url_style)
            urls, evidences = plan
            if mark_compressed:
                self._mark_compressed_captions(blocks, evidences)
            outcome = await self._send_via_onebot(event, header, blocks, urls)
            if outcome is not SendOutcome.FAILED:
                # SENT 已送达；UNKNOWN 可能已送达——两者都不能再发第二次
                self._mark_event_sent(event)
                event.stop_event()
                return

        if allow_local_fallback and not self._event_already_sent(event):
            should_compress = mode in {"auto", "bytes"} or (
                mode == "url-scaled"
                and any(block.get("path") for block in image_blocks)
            )
            # 走 URL 直传但没能直发成功：既包括 FAILED 后的回退，也包括扩展名
            # 让协议端解析不了宽高、压根没尝试直发的情形（改本地压缩成 JPEG 再发）
            onebot_failed = target.direct_send and direct_mode and all_urls
            if should_compress or onebot_failed:
                if mode in {"auto", "bytes"} and target.direct_send:
                    # OneBot 下 auto/bytes 直发压缩字节（混合载荷）：压缩结果
                    # 经 base64 段直达 QQ，不经协议端二次下载，压缩 100% 生效
                    outcome = await self._local_compress_delivery(
                        event, header, blocks, image_blocks, mark_compressed=mark_compressed
                    )
                    if outcome is not None and outcome is not SendOutcome.FAILED:
                        # SENT 已送达；UNKNOWN 可能已送达——两者都不能再发第二次
                        self._mark_event_sent(event)
                        event.stop_event()
                        return
                    # None=准备失败 或 FAILED=直发失败：字节路径已试过，整批改走 URL 链
                    logger.warning("OneBot 压缩直发未成功，改用标准消息链 URL 发送")
                else:
                    prepared = await self._prepare_delivery_blocks(
                        image_blocks, compress=mode != "url"
                    )
                    if prepared is not None:
                        evidences = [
                            CompressionEvidence.COMPRESSED if compressed else CompressionEvidence.ORIGINAL
                            for _, compressed in prepared
                        ]
                        if mark_compressed:
                            self._mark_compressed_captions(blocks, evidences)
                        fallback_header = header
                        if any(
                            item is CompressionEvidence.COMPRESSED for item in evidences
                        ):
                            fallback_header += "（已压缩）"
                        chain = self._mention_prefix(event)
                        if fallback_header:
                            chain.append(Comp.Plain((" " if chain else "") + fallback_header))
                        image_index = 0
                        for block in blocks:
                            if block.get("text"):
                                chain.append(Comp.Plain("\n" + str(block["text"])))
                            if block.get("url") or block.get("path"):
                                data, _ = prepared[image_index]
                                chain.append(Comp.Image.fromBytes(data))
                                image_index += 1
                        self._mark_event_sent(event)
                        yield event.chain_result(chain)
                        return
                    logger.warning("本地图片回退失败，改用标准消息链 URL 发送")

        if self._event_already_sent(event):
            return
        if plan is None:
            plan = await self._url_delivery_plan(image_blocks, max_side, mode, quality=quality, scaled_url_style=scaled_url_style)
        urls, evidences = plan
        if mark_compressed:
            self._mark_compressed_captions(blocks, evidences)
        chain = self._mention_prefix(event)
        if header:
            chain.append(Comp.Plain((" " if chain else "") + header))
        image_index = 0
        for block in blocks:
            if block.get("text"):
                chain.append(Comp.Plain("\n" + str(block["text"])))
            path = str(block.get("path") or "")
            if block.get("url"):
                chain.append(Comp.Image.fromURL(urls[image_index]))
                image_index += 1
            elif path:
                chain.append(Comp.Image.fromFileSystem(path))
        self._mark_event_sent(event)
        yield event.chain_result(chain)

    # ------------------------------------------------------------------
    # 指令：溯源
    # ------------------------------------------------------------------

    @filter.command("溯源", alias={"找原图"})
    async def trace(self, event: AstrMessageEvent):
        """发送或引用一张图片，按当前引擎（hash/vector/auto）找出相似的原图并回传"""
        self.start_remote_hash_refresh()  # 兜底周期刷新（幂等）
        images = self._extract_images(event)
        if not images:
            yield event.plain_result(self._usage())
            return
        engine = self._engine_choice()
        budget = self.max_images_per_query
        if engine == "vector":
            if not self.vector.enabled:
                yield event.plain_result(self.vector.config_hint())
                return
            for seg in images[:budget]:
                async for item in self._trace_vector(event, seg):
                    yield item
            return
        if engine == "auto" and self.vector.enabled:
            for seg in images[:budget]:
                async for item in self._trace_auto(event, seg):
                    yield item
            return
        # hash（或 auto 但向量引擎未配置）
        if self._hash_pool_size() == 0:
            empty_note = (
                "（向量引擎未启用：若图床侧已部署向量库，请检查 vector_search 配置）"
                if not self.vector.enabled
                else ""
            )
            yield event.plain_result(
                "图库还是空的：可发送 /登记原图 登记原图，"
                "或在插件配置 scan_dirs 填写图床目录后执行 /溯源重扫。" + empty_note
            )
            return
        for seg in images[:budget]:
            async for item in self._trace_hash(event, seg):
                yield item

    # ------------------------------------------------------------------
    # 哈希引擎（原有 pHash 比对逻辑，行为保持不变）
    # ------------------------------------------------------------------

    def _hash_pool_size(self) -> int:
        """两条哈希腿的可用条数之和：本地图库 + 图床镜像索引。

        空库提示必须按这个和判定。只看本地图库会在「本地空、图床有」时误报
        「图库还是空的」，而实际上哈希腿完全可用——用户会被误导去配 scan_dirs。
        """
        return self.library.count() + self.remote_hash.count

    @staticmethod
    def _miss_message(fail_line: str, candidates: list, empty_note: str = "") -> str:
        """未命中回复的统一拼装：阈值提示 + 可选的“最接近候选”或空库说明。"""
        if candidates:
            return "\n".join([fail_line, "最接近的候选："] + candidates)
        if empty_note:
            return f"{fail_line}\n{empty_note}"
        return fail_line

    def _merged_hash_candidates(self, phash: str, want: int) -> tuple:
        """把本地图库与图床镜像两条腿的候选**按相似度合并**后返回。

        为什么要合并而不是「本地先查、没中再查图床」：两条腿各有偏科。本地图库
        是扫描来的、可能只有几百张，但能拿到本地路径参与后续压缩/回传；图床镜像
        有两万多张、覆盖面大，却只有直链。若按顺序短路，会出现「本地有一条 0.86
        的低质命中、图床有一条 0.99 的完美命中，却只报前者」——用户看到的是
        「溯源命中了」但拿到的不是最像的那张。

        返回 (hits, cand_lines)：hits 是达阈值且已按相似度降序的候选（本地优先
        同分），cand_lines 是未命中时展示的「最接近候选」文本行。

        两条腿各自独立 try：图床那条是纯本地矩阵运算、本地那条要碰 SQLite，
        任一腿出错都不该让整条检索失败。
        """
        merged: list = []
        try:
            for m in self.library.search(phash, want):
                merged.append((float(m.similarity), "local", m))
        except Exception as exc:
            logger.warning(f"本地图库哈希检索失败（继续用图床索引）: {exc}")
        if self.remote_hash_on() and not self.remote_hash.is_empty:
            try:
                for m in self.remote_hash.search(phash, want):
                    merged.append((float(m.similarity), "remote", m))
            except Exception as exc:
                logger.warning(f"图床哈希索引检索失败（继续用本地图库）: {exc}")
        # 相似度降序；同分时本地靠前（本地能给出路径，回传链路更短）
        merged.sort(key=lambda item: (-item[0], 0 if item[1] == "local" else 1))
        hits = [m for sim, _src, m in merged if sim >= self.hash_threshold]
        cand_lines = []
        if self.top_n > 0:
            for sim, _src, m in merged[: self.top_n]:
                note = self._candidate_note(m)
                cand_lines.append(
                    f"· #{m.id} 相似度 {sim * 100:.1f}%{f'（{note}）' if note else ''}"
                )
        return hits, cand_lines

    def _candidate_note(self, match) -> str:
        """候选行的括注：本地腿用 note，图床腿用文件名（note 恒为空）。"""
        note = (getattr(match, "note", "") or "").strip()
        if note:
            return note
        return (getattr(match, "file_name", "") or "").strip()

    async def _trace_hash(self, event: AstrMessageEvent, seg, resolved=None) -> AsyncGenerator:
        """哈希检索；resolved 供 auto 引擎复用已解析的本地文件（含清理责任）。

        resolved 为 (路径, 是否临时文件, 失败提示)；传了它表示调用方已解析过
        这张图，本方法不负责删除临时文件（由调用方统一清理）。
        """
        threshold = self.hash_threshold
        top_n = self.top_n
        local_path = ""
        is_tmp = False
        owns_temp = resolved is None
        try:
            if resolved is None:
                local_path, is_tmp, fail_message = await self._resolve_local_file(seg)
            else:
                local_path, is_tmp, fail_message = resolved
            if not local_path:
                yield event.plain_result(fail_message)
                return

            feats: ImageFeatures = await asyncio.to_thread(
                compute_features, local_path, self.hash_size
            )
            # 至少取回 5 条候选：即便 top_n 配置为 0/较小值，AI 复核也需要
            # 足够的候选池才能剔除误报
            hits, candidate_lines = await asyncio.to_thread(
                self._merged_hash_candidates, feats.phash, max(5, top_n)
            )
            if hits and self._bool_cfg("ai_verify", False):
                hits = await self._ai_verify(local_path, hits)

            if hits:
                async for result in self._hash_hit_result(event, hits[0]):
                    yield result
                return

            yield event.plain_result(
                self._miss_message(
                    f"❌ 图库中未找到相似度达标的原图（阈值 {threshold * 100:.0f}%）。",
                    candidate_lines,
                )
            )
        except Exception as e:
            logger.error(f"图片溯源处理失败: {e}", exc_info=True)
            yield event.plain_result("⚠️ 处理图片时出错，详情请查看机器人日志。")
        finally:
            if owns_temp and is_tmp:
                self._remove_quiet(local_path)

    async def _hash_hit_result(self, event: AstrMessageEvent, best) -> AsyncGenerator:
        """把一条哈希命中渲染成回复（本地腿与图床腿共用）。

        两腿的差异只有两处：展示名（本地用备注、图床用文件名）与路径来源
        （本地文件 vs 远端直链）。其余展示与回传逻辑完全一致，故合并到一处，
        免得日后改一处漏一处。
        """
        title = f"✅ 溯源命中（相似度 {best.similarity * 100:.1f}%）#{best.id}"
        label = self._candidate_note(best)
        if label:
            title += f" · {label}"
        lines = []
        if best.width and best.height:
            lines.append(f"尺寸：{best.width}x{best.height}")
        if best.created_at:
            lines.append(f"入库时间：{best.created_at}")
        block = {"text": " · ".join(lines)}
        if best.width and best.height:
            # 供回传计划判断是否需要图床缩放，以及压缩标识是否成立
            block["width"] = best.width
            block["height"] = best.height
        if best.file_size and best.file_size > 0:
            # 入库时记录的原图体积：回传计划据此免探测原图体积
            block["size_bytes"] = int(best.file_size)
        if best.image_url:
            block["url"] = best.image_url
        elif best.file_path and os.path.isfile(best.file_path):
            block["path"] = best.file_path
        if "url" in block or "path" in block:
            async for result in self._yield_delivery(event, title, [block]):
                yield result
        else:
            yield event.plain_result(
                f"{title}，但原图文件已不存在："
                f"{best.file_path or best.image_url or '未知路径'}"
            )

    # ------------------------------------------------------------------
    # 向量引擎
    # ------------------------------------------------------------------

    async def _vector_search_one(self, event, seg, resolved=None):
        """单图向量检索。返回 (kind, item)：hit / miss / nofile / error。

        hit/miss/nofile 时 item 为可直接 yield 的结果对象；error 时 item 为错误说明字符串。

        resolved 供 auto 引擎复用已解析的本地文件：传了它（(路径, 是否临时文件,
        失败提示)）就不再重复下载/解码，且临时文件由调用方统一清理。
        """
        top_n = self.top_n
        local_path = ""
        is_tmp = False
        owns_temp = resolved is None
        try:
            if resolved is None:
                local_path, is_tmp, fail_message = await self._resolve_local_file(seg)
            else:
                local_path, is_tmp, fail_message = resolved
            if not local_path:
                return "nofile", event.plain_result(fail_message)
            vector = await self.vector.embed_file(local_path)
            hits = await self.vector.search(
                vector, limit=self.vector.top_k, with_vector=True
            )
            # 取回的全部达标命中（不止最高分那张），按相似度降序输出；
            # 数量上限即 top_k，需要更多就调大该配置
            good = sorted(
                (h for h in hits if h["score"] >= self.vector.threshold),
                key=lambda h: h["score"],
                reverse=True,
            )
            if good:
                good = deduplicate_vector_hits(good, self.vector.duplicate_threshold)
                return "hit", self._vector_hits_delivery(event, good)
            candidate_lines = [
                f"· {h['payload'].get('file_name') or str(h['id']).rsplit('/', 1)[-1]}"
                f" 相似度 {h['score'] * 100:.1f}%"
                for h in hits[:top_n]
            ]
            return "miss", event.plain_result(
                self._miss_message(
                    f"❌ 向量库中未找到相似度达标的原图"
                    f"（阈值 {self.vector.threshold * 100:.0f}%）。",
                    candidate_lines,
                    empty_note="（向量库中暂无图床图片，等待图床上传侧钩子入库）",
                )
            )
        except VectorEngineError as e:
            logger.warning(f"向量检索出错: {getattr(e, 'detail', e)}")
            return "error", str(e)
        except Exception:
            logger.error("向量检索异常", exc_info=True)
            return "error", "内部错误（详见机器人日志）"
        finally:
            if owns_temp and is_tmp:
                self._remove_quiet(local_path)

    async def _trace_vector(self, event: AstrMessageEvent, seg) -> AsyncGenerator:
        """严格向量引擎：只用向量库，出错直接提示。"""
        kind, item = await self._vector_search_one(event, seg)
        if kind == "error":
            yield event.plain_result(f"⚠️ 向量检索引擎错误：{item}")
        elif kind == "hit":
            async for result in item:
                yield result
        else:
            yield item

    async def _trace_auto(self, event: AstrMessageEvent, seg) -> AsyncGenerator:
        """auto 引擎：向量优先，未命中或引擎不可用时回退哈希。

        图片只解析一次：向量检索与哈希回退共用同一份本地文件，避免在“向量未
        命中”这条最常见的路径上重复下载与完整解码。临时文件由这里的 finally
        统一清理（两个下游方法在收到 resolved 时都不再负责删除）。
        """
        local_path, is_tmp, fail_message = await self._resolve_local_file(seg)
        if not local_path:
            yield event.plain_result(fail_message)
            return
        resolved = (local_path, is_tmp, "")
        try:
            kind, item = await self._vector_search_one(event, seg, resolved=resolved)
            if kind == "hit":
                async for result in item:
                    yield result
                return
            if kind == "nofile":
                yield item
                return
            hash_empty = self._hash_pool_size() == 0
            if kind == "miss":
                yield item
                if hash_empty:
                    yield event.plain_result("（哈希图库为空，无兜底可执行）")
                    return
                yield event.plain_result("尝试哈希比对：")
            else:  # error
                logger.info(f"auto 引擎回退哈希：{item}")
                if hash_empty:
                    yield event.plain_result(
                        "⚠️ 向量引擎不可用，且哈希图库为空，无兜底可执行。"
                        "可发送 /登记原图 或执行 /溯源重扫 建库。"
                    )
                    return
                yield event.plain_result("⚠️ 向量引擎不可用，改用哈希比对：")
            async for it in self._trace_hash(event, seg, resolved=resolved):
                yield it
        finally:
            if is_tmp:
                self._remove_quiet(local_path)

    async def _vector_hits_delivery(
        self, event: AstrMessageEvent, hits: list
    ) -> AsyncGenerator:
        """构造向量命中的去重回传请求。

        先发一条纯文本命中提示，再把全部相似图合并进单独一条消息：第二条走
        _yield_delivery 的统一入口（一条消息含多图），逐图配文由 mark_compressed
        按各回传路径的实际压缩情形写入，因此不再重复命中提示标题。
        """
        total_hits = sum(int(hit.get("duplicate_count") or 1) for hit in hits)
        duplicate_count = total_hits - len(hits)
        multi = len(hits) > 1
        if duplicate_count:
            logger.info(
                "向量命中去重：原始 %d 张，合并重复 %d 张，回传 %d 张",
                total_hits,
                duplicate_count,
                len(hits),
            )
        for hit in hits:
            duplicates = hit.get("duplicates") or []
            if not duplicates:
                continue
            payload = hit.get("payload") or {}
            display_name = payload.get("file_name") or str(hit.get("id"))
            duplicate_ids = "、".join(str(item.get("id")) for item in duplicates)
            logger.info(
                "向量命中去重：保留 %s（%s），合并重复 %d 张（%s）",
                display_name,
                hit.get("id"),
                len(duplicates),
                duplicate_ids,
            )

        if multi:
            header = f"✅ 向量检索命中 {len(hits)} 张（按相似度降序），图片发送可能有延迟"
        else:
            header = (
                f"✅ 向量检索命中（相似度 {hits[0]['score'] * 100:.1f}%），"
                "图片发送可能有延迟"
            )

        blocks = []
        for idx, hit in enumerate(hits, 1):
            payload = hit.get("payload") or {}
            file_name = payload.get("file_name") or str(hit.get("id")).rsplit("/", 1)[-1]
            fields = []
            if multi:
                fields.append(f"相似度 {hit['score'] * 100:.1f}%")
            width, height = payload.get("width"), payload.get("height")
            if width and height:
                fields.append(f"{width}x{height}")
            if payload.get("created_at"):
                fields.append(str(payload["created_at"]))
            prefix = f"{idx}." if multi else ""
            block = {
                "prefix": prefix,
                "file_name": str(file_name or ""),
                "fields": fields,
            }
            if width and height:
                # 供回传计划判断是否需要图床缩放，以及压缩标识是否成立
                block["width"] = width
                block["height"] = height
            size_bytes = self._known_bytes(payload)
            if size_bytes:
                # 图床侧钩子（img-indexer）入库时已记录原图体积，直接用：
                # 回传计划因此不必再探测原图尺寸，省一次往返与一次图床处理
                block["size_bytes"] = size_bytes
            image_url = str(payload.get("image_url") or "")
            if image_url:
                block["url"] = image_url
            else:
                block["fields"].append("该条目没有可用直链，可能已被删除")
            blocks.append(block)
        prompt_via_onebot = False
        prompt_task = None
        if self._can_send_via_onebot(event):
            # 提示与图片并行发出：提示只走一次 call_action，串行 await 会让图片
            # 晚一个完整往返才起步。两条消息都在同一条 OneBot 连接上按顺序下发，
            # 提示先入队，正常情况下仍先到达。
            prompt_task = asyncio.create_task(self._send_prompt(event, header))
        else:
            yield event.plain_result(header)
        try:
            async for result in self._yield_delivery(event, "", blocks, mark_compressed=True):
                yield result
            if prompt_task is not None:
                # 只为 stop_event 决策等待提示结果；图片可能已经交付，不再回滚。
                prompt_via_onebot = await prompt_task
                prompt_task = None
        finally:
            # 投递层异常或消费者提前退出时回收提示任务，避免留下悬挂任务。
            if prompt_task is not None:
                prompt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await prompt_task
        # 提示用 OneBot 原生发出（图片那条未必）时，仍按旧语义停事件；若图片
        # 那条也走了 OneBot，_yield_delivery 已停过，这里重复调用无副作用。
        # 放在 yield 之后：第二条消息已经交付，停事件不会把它吞掉。
        if prompt_via_onebot:
            event.stop_event()

    @staticmethod
    def _is_negative_answer(text: str) -> bool:
        """判断视觉模型的回答是否定（仅"否"开头不足以覆盖常见措辞）。"""
        answer = (text or "").strip().lower()
        if not answer:
            return False
        for prefix in ("否", "不是", "不 是", "不同", "不一致", "no", "not", "nope", "different"):
            if answer.startswith(prefix):
                return True
        return False

    async def _ai_verify(self, query_path: str, hits: list) -> list:
        """用当前会话的视觉大模型复核候选图是否为同一张图。

        任一环节失败都静默降级为纯哈希结果，不影响正常使用。候选之间彼此独立，
        因此并发送审——此前串行等待，多候选时复核耗时是单次推理的 N 倍。
        """
        provider = None
        try:
            # get_using_provider 已被官方弃用，优先使用异步版；旧版本回退同步 API。
            get_provider = getattr(self.context, "get_using_provider_async", None)
            if get_provider is not None:
                provider = await get_provider()
            else:
                provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None or not hasattr(provider, "text_chat"):
            logger.info("未找到可用的 LLM 提供商，跳过 AI 复核。")
            return hits

        async def verify(cand):
            """返回保留的候选；被否决时返回 None。"""
            cand_path = ""
            if cand.file_path and os.path.isfile(cand.file_path):
                cand_path = cand.file_path
            if not cand_path:
                return cand  # 候选无本地文件，无法复核，保留哈希结论
            try:
                resp = await provider.text_chat(
                    prompt=(
                        "请判断这两张图片是否为同一张图片（允许压缩、缩放、裁剪、"
                        "加水印等差异）。只回答：是、否、不确定。"
                    ),
                    image_urls=[cand_path, query_path],
                )
                answer = (getattr(resp, "completion_text", "") or "").strip()
                logger.debug(f"AI 复核 #{cand.id} -> {answer}")
                return None if self._is_negative_answer(answer) else cand
            except Exception as e:
                logger.warning(f"AI 复核失败，保留候选 #{cand.id}: {e}")
                return cand

        results = await asyncio.gather(
            *(verify(cand) for cand in hits[:AI_VERIFY_MAX_CANDIDATES])
        )
        # 不能回退为未复核的 hits：候选被全部否决时应当报告未命中
        return [cand for cand in results if cand is not None]

    # ------------------------------------------------------------------
    # 指令：登记原图
    # ------------------------------------------------------------------

    @filter.command("登记原图", alias={"原图登记"})
    async def register_image(self, event: AstrMessageEvent):
        """将消息或引用中的图片作为原图登记入库（默认仅管理员）"""
        if self._bool_cfg("register_admin_only", True) and not event.is_admin():
            yield event.plain_result("⛔ 仅管理员可登记原图（可在插件配置中修改）。")
            return

        images = self._extract_images(event)
        if not images:
            yield event.plain_result(
                "请与图片同条消息发送 /登记原图 [备注]，或引用一张图片发送 /登记原图。"
            )
            return

        note = self._strip_command(event.message_str, ("登记原图", "原图登记"))
        seg = images[0]
        local_path = ""
        is_tmp = False
        stored = None
        db_saved = False
        try:
            local_path, is_tmp, fail_message = await self._resolve_local_file(seg)
            if not local_path:
                yield event.plain_result(fail_message)
                return
            size_bytes = os.path.getsize(local_path)
            if size_bytes > self._download_limit_bytes():
                yield event.plain_result("⚠️ 图片超过大小上限，已取消登记。")
                return

            feats = await asyncio.to_thread(compute_features, local_path, self.hash_size)
            dup = self.library.find_by_phash(feats.phash)
            if dup is not None:
                yield event.plain_result(self._dup_message(dup))
                return

            stored = await self.bed.store(local_path)
            row = {
                "phash": feats.phash,
                "dhash": feats.dhash,
                "ahash": feats.ahash,
                "width": feats.width,
                "height": feats.height,
                "file_size": size_bytes,
                "image_url": stored.url or "",
                "file_path": stored.file_path or "",
                "source": "register",
                "note": note,
                "group_id": event.get_group_id() or "",
                "sender_id": event.get_sender_id() or "",
                "sender_name": event.get_sender_name() or "",
            }
            # 查重与插入在库方法的同一个锁区间内完成：上传期间的并发同图
            # 登记不会产生重复行（不再依赖"检查与插入之间无 await"的约定）
            entry_id, dup = await asyncio.to_thread(self.library.add_if_absent, row)
            if dup is not None:
                await self._cleanup_stored(stored)
                yield event.plain_result(self._dup_message(dup))
                return
            db_saved = True
            lines = [
                f"✅ 已登记原图 #{entry_id}（{stored.message}）",
                f"尺寸：{feats.width}x{feats.height}",
            ]
            if note:
                lines.append(f"备注：{note}")
            if stored.url:
                lines.append(f"直链：{stored.url}")
            if self.vector.enabled and self._vector_index_on_register():
                if self._bed_mode() == MODE_CFB:
                    # 图床侧钩子会把上传自动同步进向量库，无需插件再写一次
                    logger.debug(
                        "cloudflare_imgbed 模式：上传由图床侧钩子自动入库，跳过插件侧向量同步"
                    )
                elif not stored.url:
                    # 本地副本模式没有可供回图的公开直链，向量命中后也无法回传
                    logger.debug("当前图床模式没有公开直链，跳过向量同步")
                else:
                    try:
                        await self.vector.upsert_file(
                            local_path,
                            # 内容指纹作为 point id：同图同 ID，与图床 URL 规则无关
                            f"phash:{feats.phash}",
                            stored.url,
                            size_bytes,
                            feats.width,
                            feats.height,
                            src=self.bed.src_path_for(stored.url),
                        )
                        lines.append("已同步至 Qdrant 向量库")
                    except Exception as e:
                        # 图片已上传、记录已入库，向量同步失败只是增量失败，
                        # 不能让用户误以为整次登记失败
                        logger.warning(f"登记原图向量同步失败: {e}")
                        lines.append("（向量同步失败，可在图床侧触发钩子或对账补齐）")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            if stored is not None and not db_saved:
                await self._cleanup_stored(stored)
            logger.error(f"登记原图失败: {e}", exc_info=True)
            yield event.plain_result("⚠️ 登记失败，详情请查看机器人日志。")
        finally:
            if is_tmp:
                self._remove_quiet(local_path)

    @staticmethod
    def _dup_message(dup) -> str:
        note = f"，备注：{dup['note']}" if dup["note"] else ""
        return f"该图片已登记过（#{dup['id']}{note}）。"

    @staticmethod
    def _strip_command(message_str: str, keywords: tuple[str, ...]) -> str:
        """取指令关键词之后的内容作为备注。"""
        text = (message_str or "").strip()
        for kw in keywords:
            idx = text.find(kw)
            if idx != -1:
                return text[idx + len(kw) :].strip()
        return text

    # ------------------------------------------------------------------
    # 指令：状态 / 重扫 / 删除 / 帮助
    # ------------------------------------------------------------------

    @filter.command("溯源状态")
    async def status(self, event: AstrMessageEvent):
        """查看图库统计与插件配置摘要"""
        stats = self.library.stats()
        scan_dirs = self._scan_dirs()
        lines = [f"📚 图片溯源图库状态：共 {stats['total']} 条（索引 {stats['indexed']}）"]
        if stats["skipped"]:
            lines.append(
                f"⚠️ 有 {stats['skipped']} 条因哈希精度不匹配未参与检索，"
                "建议执行 /溯源重扫 force 重建。"
            )
        if stats["by_source"]:
            lines.append(
                "· 来源：" + "，".join(f"{k or '未知'} {v}" for k, v in stats["by_source"].items())
            )
        lines.append(
            f"· pHash 精度：{self.hash_size * self.hash_size}bit（hash_size={self.hash_size}）"
        )
        lines.append(f"· 相似度阈值：{self.hash_threshold * 100:.0f}%")
        lines.append(self._storage_line())
        engine = self._engine_choice()
        lines.append(f"· 检索引擎：{engine}（向量{'已启用' if self.vector.enabled else '未启用'}）")
        if self.vector.enabled:
            try:
                vec_count = await self.vector.count()
                count_text = f"{vec_count} 条"
            except Exception as e:
                # 原始错误可能包含内网地址/Key，只进日志，不进群聊
                logger.warning(f"查询向量库点数失败: {e}")
                count_text = "不可用（连接或鉴权异常，详情见机器人日志）"
            lines.append(f"· 向量库：{self.vector.collection}，{count_text}")
            lines.append(f"· Embed：{self.vector.embed_model} / {self.vector.image_input}")
        lines.append(f"· 扫描目录：{len(scan_dirs)} 个")
        lines.append(self._remote_hash_line())
        lines.append(f"· AI 复核：{'开' if self._bool_cfg('ai_verify', False) else '关'}")
        yield event.plain_result("\n".join(lines))

    def _remote_hash_line(self) -> str:
        """/溯源状态 的图床哈希索引一行。

        排查「为什么图床的图哈希查不到」时，这一行就能区分三种成因：
        开关没开 / 拉取失败（条数 0）/ 拉到了但被大量跳过（skipped 偏高）。
        """
        if not self.remote_hash_on():
            reason = "未配置向量引擎" if not self.vector.enabled else "已关闭"
            return f"· 图床哈希索引：{reason}"
        info = self.remote_hash.stats()
        if not info["count"]:
            return "· 图床哈希索引：空（拉取中或拉取失败，详情见机器人日志）"
        text = f"· 图床哈希索引：{info['count']} 条可用"
        if info["skipped"]:
            text += f"，跳过 {info['skipped']} 条（格式错误/退化图/无直链）"
        if info["built_at"]:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(info["built_at"]))
            text += f"（建于 {stamp}）"
        return text

    @staticmethod
    def _collect_image_files(dirs: list) -> tuple:
        """递归收集目录下的图片文件（同步、耗时，调用方需放到线程里执行）。

        返回 (文件列表, 成功遍历的目录列表)：遍历报错的目录（如网络盘
        掉线）会被跳过并记日志，不进入成功集合，从而不影响 prune 的
        清理范围判定。
        """
        files: list = []
        ok_dirs: list = []
        for d in dirs:
            errors: list = []

            def _onerror(err, errors=errors):
                errors.append(err)

            for root, _sub, names in os.walk(d, onerror=_onerror):
                for name in names:
                    if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                        files.append(os.path.abspath(os.path.join(root, name)))
            if errors:
                logger.warning(f"扫描目录 {d} 时出错（已跳过该目录的失效清理）: {errors[0]}")
            else:
                ok_dirs.append(d)
        return files, ok_dirs

    @filter.command("溯源重扫")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def rescan(self, event: AstrMessageEvent):
        """扫描配置的本地图库目录，建立/更新索引（管理员）"""
        try:
            force = "force" in (event.message_str or "").lower()
            valid_dirs = [os.path.abspath(d) for d in self._scan_dirs() if os.path.isdir(d)]
            invalid = len(self._scan_dirs()) - len(valid_dirs)
            if not valid_dirs:
                yield event.plain_result(
                    "未配置有效的扫描目录：请在插件配置 scan_dirs 中填写"
                    "图片库/图床存储目录的绝对路径。"
                )
                return

            # 目录遍历可能耗时很久，放到线程里避免阻塞事件循环
            files, scanned_dirs = await asyncio.to_thread(self._collect_image_files, valid_dirs)
            if force:
                await asyncio.to_thread(self.library.delete_by_source, "scan")
            known_paths = {
                os.path.normcase(os.path.abspath(p))
                for p in self.library.get_paths_for_source("scan")
            }
            # 一次性取回全部 pHash，在内存里去重，避免逐张查库；
            # 即便跨任务并发重扫漏判，phash 唯一索引 + INSERT OR IGNORE 也能兜底
            known_phashes = self.library.get_all_phash_hashes()
            pending: list = []
            added = skipped = failed = 0
            total = len(files)
            await event.send(
                event.plain_result(f"开始扫描 {len(valid_dirs)} 个目录，共 {total} 张图片…")
            )
            for i, path in enumerate(files, 1):
                if os.path.normcase(path) in known_paths:
                    skipped += 1
                else:
                    try:
                        feats = await asyncio.to_thread(compute_features, path, self.hash_size)
                        if feats.phash in known_phashes:
                            skipped += 1
                        else:
                            known_phashes.add(feats.phash)
                            pending.append(
                                {
                                    "phash": feats.phash,
                                    "dhash": feats.dhash,
                                    "ahash": feats.ahash,
                                    "width": feats.width,
                                    "height": feats.height,
                                    "file_size": os.path.getsize(path),
                                    "file_path": path,
                                    "source": "scan",
                                    "note": os.path.basename(path),
                                }
                            )
                            # 批量写入（单事务），避免逐条提交拖慢扫描
                            if len(pending) >= RESCAN_BATCH_SIZE:
                                batch = list(pending)
                                pending.clear()
                                added += await asyncio.to_thread(
                                    self.library.add_many, batch, reload_cache=False
                                )
                    except Exception as e:
                        failed += 1
                        logger.warning(f"扫描图片失败 {path}: {e}")
                if i % RESCAN_PROGRESS_EVERY == 0:
                    await event.send(
                        event.plain_result(
                            f"进度：{i}/{total}，新增 {added + len(pending)}，"
                            f"跳过 {skipped}，失败 {failed}"
                        )
                    )
            if pending:
                batch = list(pending)
                pending.clear()
                added += await asyncio.to_thread(self.library.add_many, batch, reload_cache=False)
            if added:
                await asyncio.to_thread(self.library.reload_cache)
            # 只清理"本次成功遍历的目录"下的失效条目，目录临时掉线不清索引
            removed = await asyncio.to_thread(
                self.library.prune_scan_missing,
                set(files),
                set(scanned_dirs),
            )
            result = f"✅ 重扫完成：共 {total} 张，新增 {added}，跳过 {skipped}，失败 {failed}"
            if removed:
                result += f"，清理失效条目 {removed}"
            failed_dirs = len(valid_dirs) - len(scanned_dirs)
            if failed_dirs:
                result += f"（注意：{failed_dirs} 个目录扫描失败已跳过）"
            elif invalid > 0:
                result += f"（注意：{invalid} 个配置目录无效已忽略）"
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"重扫失败: {e}", exc_info=True)
            yield event.plain_result("⚠️ 重扫失败，详情请查看机器人日志。")

    @filter.command("溯源删除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def delete_entry(self, event: AstrMessageEvent, entry_id: int):
        """按编号删除图库条目（管理员）"""
        try:
            row = self.library.get(entry_id)
            if row is None:
                yield event.plain_result(f"未找到编号 #{entry_id} 的条目。")
                return
            await asyncio.to_thread(self.library.delete, entry_id)
        except Exception as e:
            logger.error(f"删除条目 #{entry_id} 失败: {e}", exc_info=True)
            yield event.plain_result("⚠️ 删除失败，详情请查看机器人日志。")
            return
        notes: list = []
        # best-effort 清理插件侧写入的向量点（同图同 ID，按内容指纹派生）；
        # 图床侧钩子写入的点使用图床自身的 key 约定，其清理需在图床侧完成
        if self.vector.enabled and row["phash"]:
            try:
                await self.vector.delete_point(f"phash:{row['phash']}")
                notes.append("向量点已清除")
            except Exception as e:
                logger.warning(f"删除向量点失败 #{entry_id}: {e}")
                notes.append("向量点清除失败（详情见日志）")
        if row["image_url"]:
            try:
                if await self.bed.delete_remote(row["image_url"]):
                    notes.append("远端对象已删除")
            except Exception as e:
                logger.warning(f"删除远端对象失败 #{entry_id}: {e}")
        self._remove_local_copy(row)
        desc = row["note"] or row["file_path"] or row["image_url"] or "无备注"
        suffix = ("，" + "、".join(notes)) if notes else ""
        yield event.plain_result(f"✅ 已删除 #{entry_id}（{desc}）{suffix}")

    def _remove_local_copy(self, row) -> None:
        """删除登记流程产生的本地副本（仅限插件 images/ 目录内，绝不碰扫描目录的原图）。"""
        if row["source"] != "register":
            return
        self._remove_local_path_if_owned(row["file_path"] or "")

    def _remove_local_path_if_owned(self, path: str) -> None:
        """仅删除插件 images 目录内的本地副本。"""
        if not path:
            return
        real = os.path.abspath(path)
        images_root = os.path.abspath(self.bed.local_dir)
        try:
            inside = os.path.commonpath([real, images_root]) == images_root
        except ValueError:
            inside = False
        if inside and os.path.isfile(real):
            self._remove_quiet(real)

    async def _cleanup_stored(self, stored) -> None:
        """补偿清理本次上传但未落入图库的远端对象/本地副本。"""
        if stored is None:
            return
        if stored.url:
            try:
                await self.bed.delete_remote(stored.url)
            except Exception as e:
                logger.warning(f"补偿删除远端对象失败: {e}")
        if stored.file_path:
            try:
                self._remove_local_path_if_owned(stored.file_path)
            except Exception as e:
                logger.warning(f"补偿删除本地副本失败: {e}")

    @filter.command("溯源帮助")
    async def trace_help(self, event: AstrMessageEvent):
        """图片溯源插件使用帮助"""
        yield event.plain_result(self._usage())

    def _usage(self) -> str:
        return (
            "🔍 图片溯源使用说明\n"
            "· /溯源 —— 与图片同条消息发送，或引用一张图片发送，自动在图库中寻找原图\n"
            "· /登记原图 [备注] —— 将图片作为原图登记入库（默认仅管理员）\n"
            "· /溯源状态 —— 查看图库统计\n"
            "· /溯源重扫 [force] —— 扫描本地图库目录建立索引（管理员）\n"
            "· /溯源删除 <编号> —— 删除图库条目（管理员）\n"
            "· /随机图 [目录] —— 从图床随机取一张图片并回传\n"
            "· /随机视频 [目录] —— 从图床随机取一个视频并回传\n"
            "· /原图 [文件名] —— 取原图：带文件名时直接到图床按名取（无需先回传），"
            "不带文件名取本会话最近回传的原图\n"
            "· /原图修复 —— 清理会话历史里带配文前缀（🖼️ / 🎬 / 序号）的旧记录\n"
            "· /溯源帮助 —— 显示本说明\n"
            "检索引擎：auto=向量优先（未命中/不可用自动回退哈希）；hash/vector 可在插件配置 search_engine 切换。\n"
            "提示：哈希阈值、向量阈值与模型、图床接口、随机图接口等均可在 WebUI 插件配置中调整。"
        )

    # ------------------------------------------------------------------
    # 指令：随机图 / 随机视频 / 原图（CloudFlare-ImgBed 随机图接口）
    # ------------------------------------------------------------------

    @filter.command("随机图", alias={"随机图片"})
    async def random_image(self, event: AstrMessageEvent):
        """从图床随机取一张图片并回传（可带目录，如 /随机图 风景）"""
        settings = self._random_settings()
        directory, parsed_type = extract_directory(event.message_str or "")
        async for result in self._random_media_delivery(
            event, directory, parsed_type or "image", settings
        ):
            yield result

    @filter.command("随机视频")
    async def random_video(self, event: AstrMessageEvent):
        """从图床随机取一个视频并回传（可带目录，如 /随机视频 风景）"""
        settings = self._random_settings()
        directory, parsed_type = extract_directory(event.message_str or "")
        async for result in self._random_media_delivery(
            event, directory, parsed_type or "video", settings
        ):
            yield result

    def _imgbed_base(self) -> str:
        """/原图 直查用的图床站点地址（取第一个配置完整的）。

        CloudFlare-ImgBed 图床配置与本插件登记原图同站点，最权威；其次是
        随机图接口用的站点地址（同一台图床的另一处配置）。都没有时返回空串，
        由调用方给出「未配置图床站点」的提示，而不是拼一条必然 404 的链接。
        """
        bed = self._dict_cfg("image_bed")
        if str(bed.get("mode") or "").strip() == MODE_CFB:
            base = str(bed.get("cfi_base_url") or "").strip().rstrip("/")
            if base.lower().startswith(("http://", "https://")):
                return base
        base = str(self._dict_cfg("random_media").get("base_url") or "").strip().rstrip("/")
        return base if base.lower().startswith(("http://", "https://")) else ""

    @classmethod
    def _first_image_candidate(cls, event: AstrMessageEvent) -> str:
        """从本条消息/被引用消息的图片段里取一个**原始**文件名或直链。

        这里**不做任何清洗**，原样返回。这一点是必须的——调用方要先用
        is_thumbnail_url 判定它是否指向缩略图，而那个判据依赖路径里的
        `thumbnails/` 段；若先清洗成"裸文件名"，路径信息就丢了，判定会失效
        （实测：清洗后的名字 is_thumbnail_url 返回 False，于是缩略图检查形同
        虚设）。需要干净展示名时由调用方再走 clean_display_name。

        取不到返回空串。
        """
        for seg in cls._extract_images(event):
            for candidate in (getattr(seg, "url", None), getattr(seg, "file", None)):
                raw = str(candidate or "").strip()
                if raw and media_filename(raw):
                    return raw
        return ""

    @staticmethod
    def _original_usage() -> str:
        return (
            "本会话还没有回传过图片。/原图 也可直接按文件名到图床取原图：\n"
            "· /原图 文件名 —— 直接把图床里的原图取回来（无需先回传）\n"
            "· /原图 —— 取本会话最近回传过的图片的原图\n"
            "· 引用一张图发送 /原图 —— 按该图文件名到图床取原图"
        )

    async def _reverse_original(self, fetch, *, action: str, level: str = "warning") -> tuple[str, str]:
        """执行一次原图反查，返回 (payload 原名, 还原后的原图直链)。

        三个反查入口（缩略图直链 / 缩略图名 / 裸文件名）共用这段“不可用判定 →
        调用 → 异常只记日志 → 取 url → 按原图口径剥离处理参数”的骨架；各自的
        展示名回退顺序不同，留在调用点处理，避免把三种文案揉进一个函数。
        查不到/不可用一律返回 ("", "")，永不抛错。
        """
        if not self.vector.qdrant_ready:
            return "", ""
        try:
            info = await fetch()
        except Exception as exc:  # 反查方法自身已兜错，这里再保一层
            getattr(logger, level)(f"{action}失败: {exc}")
            return "", ""
        if not isinstance(info, dict):
            return "", ""
        original = str(info.get("url") or "").strip()
        if not original:
            return "", ""
        return str(info.get("file_name") or "").strip(), build_original_url(original)

    async def _original_from_thumbnail(self, thumb_ref: str) -> tuple[str, str]:
        """把「指向缩略图的直链或名字」换成它对应的原图 (名称, 直链)。

        为什么必须在这里拦：`/原图` 的直查分支只会拿一个名字去拼
        `{base}/file/{name}`。当这个名字来自「用户引用了上一条 /随机图 发出的
        缩略图」时，它指向 `thumbnails/`，拼出来必然命中缩略图，于是
        `/原图` 忠实地把 720px 小图又发了一遍——线上报的故障就是这个。

        反查走 Qdrant：缩略图与原图落在**同一个点**上（payload 里
        `thumb_url` 与 `image_url` 并存），按 thumb_url 即可取回原图。
        这里特意**不按名字倒推点 id**——缩略图被图床改过名（前置毫秒时间戳、
        `@` 换 `_`），倒推做不到。

        **host 差异由反查侧吸收，这里不强行归一化**。实测该部署的 payload：
            thumb_url = http://192.9.240.227:7658/file/thumbnails/178965…_【微博_…】.jpg
            image_url = http://192.9.240.227:7658/file/12%E3%80%81…%E5%89%A7%E7%85%A7.jpg
        两者都记**内网 IP**；而插件运行期手上的直链可能来自公网反代
        `img.dixc.de`（OpenResty → `172.19.0.2:8080`，同一图床）。故这里只把
        编码形态还原成原始形态（`unquote`，缩略图那侧存的是**未编码**串），
        实际匹配交给 `original_info_for_thumb`：它先试整串、再退回按 `/file/`
        之后的**对象键**匹配，从而与 host 无关。

        查不到时返回 ("", "")，调用方据此给出可读提示；宁可说「找不到」，也
        绝不把缩略图当原图发出去。
        """
        if not self.vector.qdrant_ready:
            return "", ""
        reference = str(thumb_ref or "").strip()
        if not reference:
            return "", ""
        base = self._imgbed_base()
        # 统一成「与 payload 同形」的完整缩略图直链：
        # 1) 有站点前缀就直接用；2) 只有路径段则补上前缀；3) 两者都无则作罢
        if reference.lower().startswith(("http://", "https://")):
            full = reference
        elif base:
            full = f"{base}/file/{reference.lstrip('/')}"
        else:
            return "", ""
        # 关键：还原到**原始未编码**形态（payload.thumb_url 就是这么存的），
        # 同时去掉可能残留的处理参数（width/height/fit/fallback）
        thumb_url = unquote(build_original_url(full))
        display, url = await self._reverse_original(
            lambda: self.vector.original_info_for_thumb(thumb_url),
            action="缩略图反查原图",
        )
        if not url:
            return "", ""
        # 展示名**必须**优先取 payload 里的 file_name：缩略图名的 `@`→`_` 不可逆，
        # 光清洗缩略图名还原不出真名（实测 payload.file_name 才是原始名）
        return display or clean_display_name(url) or clean_display_name(reference) or "", url

    async def _original_from_thumb_name(self, name: str) -> tuple[str, str]:
        """按「缩略图改名后的文件名」反查原图 (名称, 直链)；查不到返回 ("", "")。

        与 `_original_from_thumbnail` 的分工：那个认**带 `thumbnails/` 段**的直链，
        这个只认**文件名**，用于目录段已经在流转中丢掉的场景（QQ 把机器人发的图
        转存到自己的 CDN 后，插件拿到的只有裸文件名）。两者都是反查，返回的都
        是同一个 Qdrant 点上的原图直链。

        展示名取 payload.file_name（图床登记真名，`@` 完好）；反查失败返回空，
        由调用方继续走裸名探测与可读提示——**绝不把缩略图当原图发出去**。
        """
        if not self.vector.qdrant_ready:
            return "", ""
        query = str(name or "").strip()
        if not query:
            return "", ""
        display, url = await self._reverse_original(
            lambda: self.vector.original_info_for_thumb_name(query),
            action="按缩略图名反查原图",
            level="debug",
        )
        if not url:
            return "", ""
        return display or clean_display_name(url) or query, url

    async def _lookup_imgbed_file(self, name: str, *, quoted: str = "") -> tuple[str, str, str]:
        """按文件名到图床直查原图，返回 (名称, 直链, 失败提示)。

        **查询顺序：先向量库按名反查，再退回裸名拼 URL + 探测。** 反查在前是
        必须的，不是优化：图床的 `/file/{path}` 只认**完整对象键**（含各级目录），
        裸文件名拼上去**必然 404**。线上报的

            图床里没有找到「【微博@潮流合伙人】20200207-04：赵今麦海报.jpg」

        就是这个：原图实际在 `/file/7、综艺节目/爱奇艺《潮流合伙人》/《潮流合伙人》
        图集/` 下面，而插件拼的是 `/file/【微博@潮流合伙人】…jpg`。实测 A/B：
        裸名 404 / 带目录 200（内网与公网域名结果一致）。Qdrant 的 payload
        `image_url` 正是入库时登记的**带完整目录**的权威直链，拿名字反查它即可。

        反查非「恰好 1 命中」时（同名文件在多目录下有副本、或该图未进索引）不硬猜，
        退回原有的裸名拼 URL + 探测逻辑：那一步虽然对多级目录无能为力，但对
        「图床根目录下的文件」以及**用户直接给完整相对路径**的情形仍然有效，
        且探测结果能给出可读提示。

        探测为「确定不存在」（4xx）时返回空直链并给出提示，同时附上尝试过的
        完整直链，便于用户核对图床里的目录层级；探测结果未知（网络异常）不拦，
        仍把直链交给回传——网络抖动不该被当成「图床没有这张图」。

        name 指向缩略图时（`thumbnails/...`，典型来自引用上一条随机图消息）先
        反查真正的原图直链再返回，绝不把缩略图当原图交付。quoted 是引用图里的
        **原始直链**（保留 `thumbnails/` 段），有它时优先用它反查——name 可能
        已被清洗掉时间戳前缀，而 strip 之后的形态与 Qdrant 里存的并不完全一致。
        """
        base = self._imgbed_base()
        if not base:
            return (
                "",
                "",
                "未配置图床站点地址：请在「图床设置」（cloudflare_imgbed）或「随机图」里"
                "填写站点地址后重试，或先 /溯源 命中再发 /原图。",
            )
        # 判定要用「原始引用直链」与「名字」两者：任一指明 thumbnails/ 都算命中
        if is_thumbnail_url(quoted) or is_thumbnail_url(name):
            original_name, original_url = await self._original_from_thumbnail(quoted or name)
            if original_url:
                logger.info(
                    f"/原图 直查命中缩略图，已反查回原图: {quoted or name} -> {original_url}"
                )
                return original_name, original_url, ""
            return (
                "",
                "",
                f"「{name}」是图床里的缩略图，且查不到它对应的原图；"
                "请改用原图文件名，或先 /溯源 命中该图后再发 /原图。",
            )
        # ① 先按文件名反查：拿回入库时登记的、**带完整目录**的权威直链。
        #    这一步解决「裸名拼 URL 必然 404」——图床 /file/ 只认完整对象键。
        #    注意判据是 `found[1]` 而**不是** `if found:`：`_original_by_name`
        #    以 `("", "")` 表示落空，而**非空元组恒为真**——写成 `if found:`
        #    会在落空时也走进来，直接返回空直链（调用方拿到空 url 会当成
        #    「查不到」），恰好把兜底路径②整个跳过。这是个静默错误：
        #    没有异常、没有日志，只是所有直查都失效。
        found = await self._original_by_name(name)
        if found[1]:
            original_name, original_url = found
            logger.info(f"/原图 按名反查命中原图: {name} -> {original_url}")
            return original_name, original_url, ""
        # ①.5 名字本身是「缩略图被改名后的文件名」时，按缩略图名反查原图。
        #     这一段要放在 ② 之前：这类名字（`1789…_【微博_…】.jpg`）在图床里
        #     只存在于 `thumbnails/` 下，裸名拼 URL 必然 404，探测只会给出
        #     「图床里没有找到」这种让人白核对的提示。
        #
        #     为什么 ① 拦不住它：① 按 payload.file_name 精确匹配，而缩略图名
        #     被图床改过（前置毫秒时间戳、`@` 换成 `_`，且 `_` 不可逆）。
        #     为什么 quoted 也可能拦不住：用户引用机器人消息时，QQ 会把图转存到
        #     自己的 CDN，插件手上的直链里**没有 `thumbnails/` 段**，
        #     `is_thumbnail_url` 只能返回 False，于是走到这里。
        thumb_hit = await self._original_from_thumb_name(name)
        if thumb_hit[1]:
            original_name, original_url = thumb_hit
            logger.info(f"/原图 按缩略图名反查回原图: {name} -> {original_url}")
            return original_name, original_url, ""
        # ② 反查未命中（未进索引 / 同名多份 / 向量检索关闭）→ 退回裸名拼 URL + 探测，
        #    对根目录文件与「用户直接给完整相对路径」两种情形仍然管用
        candidate = build_imgbed_file_url(base, name)
        if not candidate:
            return (
                "",
                "",
                f"「{name}」不像图床里的文件名，无法直接取原图；"
                "可先用 /溯源 命中该图后再发 /原图。",
            )
        exists, _length = await self._probe_url(candidate)
        if exists is False:
            # 落空原因不同，提示必须不同：库里压根没这个名字（未进索引）时让用户
            # 去 /溯源；而同名多份且内容不同时图是**确实存在**的，报「图床里没有
            # 找到」只会让人白核对一遍。命中数只在失败路径上多查一次。
            matches = await self._file_name_matches(name)
            if matches and matches > 1:
                return (
                    "",
                    "",
                    f"「{name}」在图库里有 {matches} 份同名文件且内容不同，"
                    "无法确定你要哪一张。请带上目录再试（如："
                    f"/原图 目录名/{name}）。",
                )
            return "", "", f"图床里没有找到「{name}」，已尝试：{candidate}"
        return name, candidate, ""

    async def _file_name_matches(self, name: str) -> int | None:
        """问向量库「这个名字有几个点」；用于失败文案的诊断，查不到返回 None。

        对齐库内 `VectorEngine.file_name_matches`——本方法存在的意义只是把
        「引擎未启用 / 异常」收敛成 None，让调用方不必到处判 self.vector。
        """
        if not self.vector.qdrant_ready:
            return None
        try:
            return await self.vector.file_name_matches(name)
        except Exception as exc:  # 诊断失败不能影响原本的失败提示
            logger.debug(f"同名命中数诊断失败: {exc}")
            return None

    async def _original_by_name(self, name: str) -> tuple[str, str]:
        """按裸文件名到向量库反查原图 (名称, 直链)；查不到返回 ("", "")。

        与 `_original_from_thumbnail` 的区别：那个按**缩略图直链**反查（用于拦
        「引用随机图发出来的图」），这个按**文件名**反查（用于 `/原图 文件名`
        这条「没有历史、只有名字」的路径）。

        名字里可能只有裸文件名，而图床里它在多级目录下——payload.image_url 存的
        是入库时的完整直链，故必须由库来告诉我们目录，不能靠猜。

        跳过 name 自带目录（含 `/`）的情形：那说明调用方已经拿到了相对路径，
        直接交给图床按路径取更准，不必多此一举反查（反查只匹配裸 file_name，
        带目录的名字在这一步本来就命中不了）。

        展示名用 payload.file_name（图床登记真名，`@` 完好），比入参更可信。
        本方法**永不抛错**：反查只是获取目录层级的捷径，失败由调用方走兜底。
        """
        query = str(name or "").strip()
        if not query or "/" in query or not self.vector.qdrant_ready:
            return "", ""
        display, url = await self._reverse_original(
            lambda: self.vector.original_by_file_name(query),
            action="按文件名反查原图",
        )
        if not url:
            return "", ""
        return display or query, url

    @filter.command("原图修复")
    async def original_repair(self, event: AstrMessageEvent):
        """清理会话历史里带配文前缀/装饰的脏键（1.6.1 前的旧数据）。

        为什么需要这个命令：1.6.1 之前 `_remember_blocks` 会把配文前缀
        （`🖼️ `、`🎬 `、多图的 `1. `）连同文件名一起登记进历史。于是

            引用那条带前缀的消息发 /原图 → 历史/引用图里取到的名字自带前缀
            → 拼成 /file/🖼️%20【…】 → 必然 404
            → 提示文案里再复述一遍带前缀的名字，用户完全看不出问题在哪儿

        清洗逻辑现在已修（见 random_media.clean_display_name），**新写入的键是
        干净的**；但旧键是纯内存 LRU，不会自己消失——只在进程重启或该会话累积
        到上限被淘汰时才可能挤掉。故给一个显式入口。

        只管历史，**不动图床**：前缀从来不是图床里的真实文件，脏的只是本地键。
        """
        cleaned = self._repair_history_keys()
        if not cleaned:
            yield event.plain_result(
                "✅ 会话历史里没有带前缀的脏记录，无需修复。\n"
                "（前台提示文案本插件从不写入文件，无需清理）"
            )
            return
        logger.info(f"/原图修复 清理了 {cleaned} 条带前缀的会话历史键")
        yield event.plain_result(
            f"✅ 已清理 {cleaned} 条带前缀的会话历史记录。\n"
            "现在可以重新「引用那张图 + /原图」取原图了；"
            "若仍取不到，再引用一次 /随机图 刚发出的图重试。"
        )

    def _repair_history_keys(self) -> int:
        """把会话历史里带装饰前缀的键改名成干净形态；返回处理条数。

        做的是**改名而非删除**：URL 是好的，脏的只是键。直接删会让用户白白
        丢掉一条本来可用的历史，改名则让它立刻变成可命中的干净键。改名与
        LRU 归位由 MediaHistory.repair_keys 统一处理（不再访问其私有结构）。
        """
        return self.history.repair_keys(clean_display_name)

    @filter.command("原图")
    async def original_image(self, event: AstrMessageEvent):
        """重发最近回传过的图片的原图；也可直接按文件名到图床取（/原图 风景.jpg）"""
        session = self._session_key(event)
        query = self._strip_command(event.message_str or "", ("原图",)).strip(" \t:：,，")
        name = ""
        url = ""
        # 引用图里的**原始**直链/名字：保留 `thumbnails/` 路径段，供缩略图判定与
        # 反查使用（反查要拼出与 Qdrant payload 同形的完整缩略图直链）
        quoted = ""
        # 取值来源，只用于日志：历史命中 / 按名直查 / 缩略图反查。
        # 线上排查时「/原图 到底走了哪条路」是最关键的一条信息，而历史命中
        # 这条最常见、却原本一行日志都不打，导致日志里完全看不到命令执行过。
        source = "history"
        if query:
            status, payload = self.history.find(session, query)
            if status == "ambiguous":
                candidates = "、".join(payload[:5])
                yield event.plain_result(f"匹配到多张图片，请写出更完整的文件名：{candidates}")
                return
            if status == "found":
                name, url = payload
            else:
                # 历史没命中时 query 会退化成**直查图床的文件名/相对路径**，此时它
                # 可能自带装饰前缀——最常见的是用户从配文里复制粘贴（配文是
                # `🖼️ 名字`，复制过来就带了 `🖼️ `）。不清洗就会拼出
                # `/file/🖼️%20名字` 而必然 404。命中历史的分支无需清洗：
                # 历史键本就是干净文件名（1.6.1 起写入时即已清洗）。
                #
                # 这里用 strip_leading_decoration 而**不是** clean_display_name：
                # 后者靠 media_filename 取末段，会把用户手写的多级相对路径
                # （`7、综艺节目/…/海报.jpg`）削成裸文件名——图床按裸名取不到，
                # 于是「用户给足了信息反而查不到」。前者原地剥前缀、保住目录。
                query = strip_leading_decoration(query) or query
        else:
            latest = self.history.latest(session)
            if latest is not None:
                name, url = latest
            else:
                # 会话里没有回传记录：退化为「按消息/引用图里的文件名直查图床」
                quoted = self._first_image_candidate(event)
                query = clean_display_name(quoted) or ""

        if not url:
            if not query:
                yield event.plain_result(self._original_usage())
                return
            source = "lookup"
            name, url, message = await self._lookup_imgbed_file(query, quoted=quoted)
            if not url:
                yield event.plain_result(message)
                return
        elif is_thumbnail_url(url):
            # 兜底：历史里若因任何原因（旧版本写入、外部注入）存进了缩略图直链，
            # 也要在发出前反查回原图。正常路径下历史记的是原图（见 _yield_delivery
            # 的 remember_blocks），这里是防御，不依赖上游一定正确。
            original_name, original_url = await self._original_from_thumbnail(
                self._bed_file_id(url) or name
            )
            if original_url:
                logger.info(f"/原图 历史命中缩略图，已反查回原图: {url} -> {original_url}")
                name, url = original_name, original_url
                source = "history-thumb-reverse"
            else:
                yield event.plain_result(
                    f"本会话记录的「{name}」是缩略图，且查不到它对应的原图；"
                    "可先用 /溯源 命中该图后再发 /原图。"
                )
                return
        # R6：/原图 只发 URL、不落地字节——剥离 ImgBed 处理参数还原未处理原文件，
        # 并强制 url 通道 + 禁用本地回退
        final_url = build_original_url(url)
        # 最后一次校验：走到这里说明上游每一条路径都「自认为」拿到的是原图，
        # 但只要最终直链仍指向缩略图目录，就绝不能发出去——静默发出一张 720px
        # 小图，用户只会觉得「/原图 又没生效」，比明确报错难排查得多。
        if is_thumbnail_url(final_url):
            logger.warning(f"/原图 最终直链仍是缩略图，已拒绝发送: {final_url}")
            yield event.plain_result(
                f"取到的「{name}」仍是图床里的缩略图直链，已中止发送；"
                "请改用原图文件名，或先 /溯源 命中该图后再发 /原图。"
            )
            return
        logger.info(f"/原图 发出（来源={source}）: {name} <- {final_url}")
        block = {"url": final_url, "file_name": name}
        async for result in self._yield_delivery(
            event,
            f"🖼️ {name}（原图）",
            [block],
            mode_override="url",
            allow_local_fallback=False,
        ):
            yield result

    async def _random_media_delivery(
        self, event: AstrMessageEvent, directory, content_type: str, settings: dict
    ):
        """取一条随机媒体并回传。

        图片走 _yield_delivery（复用全局通道：auto / bytes / url / url-scaled /
        native），视频走标准消息链 Video.fromURL；无法识别类型时回退纯文本
        直链。同域 http 直链先升级为 https，减少协议端一次跳转。
        """
        # 未指定目录时回退到配置的 default_dir 池：图床若设置了
        # randomImageAPI.allowedDir，不带 dir 的裸 /random 会被判为「目录不允许」
        # 而 403，故由客户端补一个目录；池有多项时随机取其一，以尽量覆盖整库。
        target_dir = str(directory or "").strip() or pick_default_directory(settings)
        try:
            media_url = await self._random_client().fetch(target_dir, content_type)
        except RandomMediaError as exc:
            logger.warning(f"获取随机媒体失败: {exc}")
            yield event.plain_result(f"⚠️ 获取随机媒体失败：{exc}")
            return
        lan_hosts = self._lan_url_hosts(settings)
        if lan_hosts:
            # 内网直连：床内直链统一改写为内网源（/random 直链可能已是内网源，
            # 改写幂等；公网域名形式的直链也一并收进来），后续取字节不出公网
            media_url = swap_url_origin(media_url, settings["lan_base_url"], lan_hosts)
        media_url = upgrade_to_https(media_url, settings["base_url"])
        target = self._delivery_target(event)
        kind = media_kind(media_url, content_type)
        if kind == "video":
            # 官方机器人等平台把视频 URL 交给腾讯服务器拉流，内网地址它访问
            # 不到，须换回公网直链；换不出来（第三方内网地址等）就保持原样，
            # 与图片 URL 上传共用同一套换源判定（_public_media_url）
            send_url = media_url
            if target.offbox_upload:
                send_url = self._public_media_url(media_url, settings) or media_url
            caption = self._random_caption(media_url, "video", settings)
            yield event.chain_result([Comp.Plain(caption), Comp.Video.fromURL(send_url)])
            return
        if kind == "image":
            # 发送用缩略图替身、历史留原图：QQ 侧下载小图更快，sendMsg 回执超时
            # 概率显著下降；而 /原图 仍能凭历史里的原图直链取回全尺寸文件。
            #
            # 边界：图床的 /random 若被指到 thumbnails/ 目录（default_dir 配错或
            # 用户显式传了该目录），media_url 本身就是缩略图。此时别再查替身
            # （缩略图没有替身），并把「原图」口径定为反查结果，避免历史里记进
            # 缩略图直链、导致 /原图 之后只能拿回小图。
            original_url = media_url
            original_name = ""
            if is_thumbnail_url(media_url):
                info = None
                try:
                    normalized = unquote(build_original_url(media_url))
                    info = await self.vector.original_info_for_thumb(normalized)
                except Exception as exc:  # 反查失败不影响发送，按原样处理
                    logger.debug(f"随机图反查原图失败（按原样处理）: {exc}")
                if info and info.get("url"):
                    original_url = info["url"]
                    if lan_hosts:
                        # Qdrant 反查回的是公网直链，内网直连模式下同样改写
                        original_url = swap_url_origin(
                            original_url, settings["lan_base_url"], lan_hosts
                        )
                    # 真名取自 payload.file_name（图床没改过名的原始名）
                    original_name = str(info.get("file_name") or "").strip()
                else:
                    # 2026-10-20 排查实录：反查失败的原因有多种——引擎未配置、
                    # Qdrant 请求失败、数据未命中——后两者各有 DEBUG 记录（线上
                    # 默认 INFO 看不到），而「引擎未配置」历史上完全静默，导致
                    # warning 里看不出任何线索、跨实例排查只能逐台实测。把引擎
                    # 状态直接写进 warning：未配置 → 给配置指引；缺 embed（此时
                    # 反查仍可用）→ 说明只影响以图搜图；配置齐全 → 指向 DEBUG。
                    if not self.vector.qdrant_ready:
                        hint = "向量引擎未配置，请在插件配置 vector_search 一节补齐"
                    elif not self.vector.enabled:
                        hint = "向量引擎缺 embed 配置（反查仍可用，仅以图搜图不可用）"
                    else:
                        hint = "原因见 DEBUG 日志"
                    logger.warning(
                        f"随机图命中的是缩略图且反查不到原图（{hint}）: {media_url}"
                    )
            send_url = await self._random_send_url(original_url, settings)
            # 配文一律用**原图**名并清洗掉图床改名的前缀（时间戳 + `@` 换 `_`），
            # 显示缩略图名对用户没有意义
            caption = self._random_caption(
                original_url, "image", settings, display_name=original_name
            )
            # qq_official 等平台的假格式体检已下沉到 _yield_delivery 统一处理
            blocks = [
                {
                    "url": send_url,
                    "file_name": original_name or clean_display_name(original_url) or "",
                }
            ]
            async for result in self._yield_delivery(
                event,
                caption,
                blocks,
                remember_blocks=(
                    blocks if send_url == original_url else [{"url": original_url}]
                ),
            ):
                yield result
            return
        yield event.plain_result(f"随机媒体获取成功：{media_url}")

    @staticmethod
    def _bed_file_id(url: str) -> str:
        """从图床直链里取出文件 id（即 S3 对象键）。

        图床直链形如 `{base}/file/{id}`，id 可能含中文与 `@`/`#`（故 URL 里是
        百分号编码的），必须 unquote 还原成原始键才能算出同一个点 id。取不到
        返回空串，由调用方按「无替身」处理。
        """
        path = urlparse(str(url or "")).path
        marker = "/file/"
        index = path.find(marker)
        if index < 0:
            return ""
        return unquote(path[index + len(marker) :]).strip()

    async def _offbox_pipeline(
        self,
        event: AstrMessageEvent,
        header: str,
        blocks: list[dict],
        *,
        mark_compressed: bool = False,
    ) -> tuple[list[dict], bool]:
        """第三方转存平台的两个前置动作合一：格式体检 → 上传通道接管。

        顺序不可交换：体检可能把「假 jpg」换成本地转码 JPEG（path）或缩略图
        替身 URL，上传通道必须拿着**改写后**的 block 才能决定发 URL 还是发
        分片（path 只能分片）。合并前这是 _yield_delivery 里两段独立 hook，
        很容易出现「改写了 blocks 却忘了把它交给下一步」这类顺序错误。

        返回 (最终 blocks, 是否已投递完毕)：handled 为真时调用方只需标记已
        发送并 stop_event，不要再走任何原生回传分支（否则会重复投递）。
        """
        blocks = await self._offbox_rewrite_blocks(blocks)
        handled = await self._qq_upload_takeover(
            event, header, blocks, mark_compressed=mark_compressed
        )
        return blocks, handled

    async def _offbox_rewrite_blocks(self, blocks: list[dict]) -> list[dict]:
        """对 blocks 里每个待发 URL 做官方富媒体格式体检，按需改写发送形态。

        体检点放在 _yield_delivery（三条回传路径的汇合处）：标准格式原样
        保留（只花一次 16 字节嗅探）；假格式可解码 → 换成本地 JPEG path；
        不可解码 → 换成缩略图替身 URL。全部体检失败按原样保留——体检是
        增强，不能反过来挡住发送。
        """
        settings = self._random_settings()
        rewritten: list[dict] = []
        for block in blocks:
            url = str(block.get("url") or "")
            if not url or block.get("path"):
                rewritten.append(block)
                continue
            try:
                outcome = await self._offbox_image_delivery(url, settings)
            except Exception as exc:
                logger.warning(f"qq_official 图片格式体检异常，按原样交付: {exc}")
                outcome = None
            if outcome is None:
                rewritten.append(block)
                continue
            kind, value = outcome
            new_block = dict(block)
            if kind == "path":
                new_block.pop("url", None)
                new_block["path"] = value
            else:
                new_block["url"] = value
            rewritten.append(new_block)
        return rewritten

    async def _offbox_image_delivery(
        self, url: str, settings: dict
    ) -> tuple[str, str] | None:
        """qq_official 图片交付体检：返回 (方式, 值)；None 表示按原 URL 交付。

        官方富媒体上传只收标准 JPEG/PNG/GIF；图床里存在扩展名 .jpg、实为
        HEIF/AVIF/WEBP（微博 live 图等）的文件，原样交付必被打回「富媒体文件
        格式不支持」。处置分两级：
        - "path"：PIL 可解码的假格式 → 转码为 JPEG 落盘，本地字节交付；
        - "url"：PIL 解不了的（如未装 pillow-heif 的 HEIC）→ 换发同一点的
          缩略图替身直链（img-indexer 预生成的真 JPEG）。
        文件头能放行、或两级兜底都失败时返回 None（按原样交付——体检是增强，
        不能反过来挡住发送）。
        """
        try:
            client = await self._get_http()
            limit = self._download_limit_bytes()
            async with await client.request(
                "GET", url, timeout=self._download_timeout()
            ) as resp:
                if not 200 <= resp.status < 300:
                    return None
                head = await resp.content.read(16)
                if qq_upload_safe_image(head):
                    return None
                # 嗅探已消费前 16 字节：转码必须用完整字节，把 head 拼回去，
                # 否则 PIL 拿到的是缺了文件头的残缺流，解码必败
                rest = await read_limited_bytes(resp, limit - len(head))
                data = head + rest
        except Exception as exc:
            logger.warning(f"qq_official 图片格式体检失败，按原样交付: {exc}")
            return None
        path = await asyncio.to_thread(self._write_transcoded_jpeg, bytes(data))
        if path:
            return ("path", path)
        file_id = self._bed_file_id(url)
        if file_id and self.vector.qdrant_ready:
            try:
                thumb = await self.vector.thumb_url_for(file_id)
            except Exception:
                thumb = None
            if thumb:
                lan_hosts = self._lan_url_hosts(settings)
                if lan_hosts:
                    # 图片由适配器本机下载，替身直链同样收进内网源
                    thumb = swap_url_origin(
                        thumb, str(settings.get("lan_base_url") or ""), lan_hosts
                    )
                logger.info(f"qq_official 假格式且不可转码，改发缩略图替身: {file_id}")
                return ("url", thumb)
        return None

    def _write_transcoded_jpeg(self, data: bytes) -> str | None:
        """把任意可解码图片字节转存为 JPEG 临时文件，返回路径；失败返回 None。"""
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()  # 幂等：让 Image.open 直解 HEIC
        except ImportError:
            pass
        try:
            from PIL import Image, ImageOps
        except ImportError:
            logger.warning("PIL 不可用，无法转码非标准格式图片，按原样交付")
            return None
        try:
            with Image.open(io.BytesIO(data)) as image:
                fixed = ImageOps.exif_transpose(image)
                if fixed.mode not in ("RGB", "L"):
                    fixed = fixed.convert("RGB")
                path = os.path.join(self.tmp_dir, f"qqsafe_{secrets.token_hex(8)}.jpg")
                fixed.save(path, format="JPEG", quality=92)
            logger.info(f"qq_official 图片格式体检：已转码为 JPEG 交付 {path}")
            return path
        except Exception as exc:
            logger.warning(f"图片转码失败，按原样交付: {exc}")
            return None

    # ------------------------------------------------------------------
    # QQ 官方机器人上传通道（url / chunked，与缩略图发送无关）
    # ------------------------------------------------------------------

    def _qq_upload_config(self) -> tuple[str, int, int, str, int, str]:
        """推送通道（QQ 官方）配置归一化，语义见 image_delivery.qq_upload_settings。"""
        return qq_upload_settings(self._qq_delivery_cfg())

    @staticmethod
    def _qq_send_target(event: AstrMessageEvent) -> tuple[str, str] | None:
        """取 (会话类型, 目标 openid)：group→group_openid、c2c→user_openid。

        botpy 的群消息自带 group_openid、C2C 消息在 author.user_openid 上。
        都拿不到时退化为「session_id + 消息类型」；连类型都判不出就返回 None，
        让调用方交回原回传逻辑——猜错会话类型会把图发到错误的聊天里，比不发
        严重得多，所以这里宁可放弃接管。
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        group_openid = str(getattr(raw, "group_openid", "") or "").strip()
        if group_openid:
            return ("group", group_openid)
        author = getattr(raw, "author", None)
        user_openid = str(getattr(author, "user_openid", "") or "").strip()
        if not user_openid:
            user_openid = str(getattr(raw, "user_openid", "") or "").strip()
        if user_openid:
            return ("c2c", user_openid)
        try:
            session_id = str(event.get_session_id() or "").strip()
        except Exception:
            session_id = ""
        if not session_id:
            return None
        try:
            message_type = str(event.get_message_type() or "").upper()
        except Exception:
            message_type = ""
        if "GROUP" in message_type:
            return ("group", session_id)
        if any(hint in message_type for hint in ("FRIEND", "C2C", "PRIVATE")):
            return ("c2c", session_id)
        return None

    @staticmethod
    def _qq_target_kwargs(target: tuple[str, str]) -> dict:
        """把 (类型, openid) 转成适配器上传方法的收件人关键字参数。"""
        kind, session_id = target
        return {"group_openid": session_id} if kind == "group" else {"openid": session_id}

    def _public_media_url(self, url: str, settings: dict) -> str | None:
        """把图床直链换成「外部服务器下载得到」的公网地址；换不出来返回 None。

        两处共用：qq_official 的视频要交给腾讯拉流、图片要交给腾讯按 URL 转存。
        内网直连（lan_base_url）只是图床与 AstrBot 之间的便利通道，对外正源是
        base_url，故命中内网主机时按 base_url 换源（只换协议+主机端口，路径与
        查询串原样保留）。

        判定不能只看 IP 段：本部署的内网直连地址是 `192.9.240.227`，它属于
        **公网段**（RFC1918 私有段只有 10/8、172.16/12、192.168/16），句法检查
        会把它当成公网地址放行。因此以配置为准：

        1. 命中内网直连主机（lan_base_url）→ 按 base_url 换回公网源；
        2. 换完仍落在内网主机上（说明 base_url 本身就配成了内网）→ 不可用；
        3. 与 base_url 同主机 → 视为公网直链（配置已声明它是公网站点）；
        4. 其余第三方主机 → 退化为句法判断（公网域名/公网 IP 放行）。
        """
        if not url:
            return None
        base = str(settings.get("base_url") or "").strip()
        lan_hosts = self._lan_url_hosts(settings)
        candidate = swap_url_origin(url, base, lan_hosts) if lan_hosts else url
        try:
            host = (urlsplit(candidate).hostname or "").lower()
        except ValueError:
            return None
        # 只与「内网地址的主机」比，不能与整个 _lan_url_hosts 比——后者里头还
        # 包含公网 base 的主机（换源后正是要落到它上面）
        lan_host = ""
        lan_base = str(settings.get("lan_base_url") or "").strip()
        if lan_base:
            try:
                lan_host = (urlsplit(lan_base).hostname or "").lower()
            except ValueError:
                lan_host = ""
        if lan_host and host == lan_host:
            # 换源后仍是内网主机：腾讯服务器访问不到，别发出一次必败的上传
            return None
        base_host = ""
        if base:
            try:
                base_host = (urlsplit(base).hostname or "").lower()
            except ValueError:
                base_host = ""
        if base_host and host == base_host:
            return candidate
        return candidate if is_public_http_url(candidate) else None

    async def _qq_block_size(self, block: dict) -> int | None:
        """该图体积：优先用命中条目自带的大小，未知时 HEAD 探一次。

        探不出来返回 None（=未知），调用方据此避开「未知却当成小图直传」这个
        5MB 陷阱；探测只读响应头，不产生下载流量。
        """
        known = self._known_bytes(block)
        if known:
            return known
        url = str(block.get("url") or "")
        if not url:
            return None
        exists, length = await self._probe_url(url)
        return length if exists is not False else None

    async def _qq_upload_takeover(
        self,
        event: AstrMessageEvent,
        header: str,
        blocks: list[dict],
        *,
        mark_compressed: bool = False,
    ) -> bool:
        """qqofficial 上传通道接管：返回 True 表示本批已由本方法处理完毕。

        只在通道不是 direct 时才真正接管；一旦接管，图片由插件自己上传并发送
        （post_group_message / post_c2c_message + msg_type=7），所以成功返回后
        调用方必须标记已发送并 stop_event，否则 respond stage 会照原路（base64
        直传）再发一次，那正是「上传文件大小超过限制」的来源。

        失败语义两档（delivery.qq_on_failure）：
        - fallback（默认）：一张都还没发出去时返回 False，交回原有的本地压缩 /
          缩略图降级链；只要已经发过就绝不再交回，避免同一张图发两遍。
        - notify：不交回，改回一条文字说明失败原因。
        """
        mode, direct_max, url_max, auto_prefer, max_upload, on_failure = (
            self._qq_upload_config()
        )
        if mode == "native":
            # 通道关闭：交给 image_delivery 的原生回传逻辑（含压缩）
            return False
        target = self._qq_send_target(event)
        bot = getattr(event, "bot", None)
        if target is None or getattr(bot, "api", None) is None:
            logger.debug("qq_official 上传通道：会话标识或 bot 未就绪，交回原回传逻辑")
            return False
        images = [block for block in blocks if block.get("url") or block.get("path")]
        if not images:
            return False

        settings = self._random_settings()
        plan: list[tuple[dict, str, str | None]] = []
        for block in images:
            # 固定通道（native/bytes/url/url-scaled）不依赖体积，省掉一次 HEAD 探测
            size = None if mode != "auto" else await self._qq_block_size(block)
            url = str(block.get("url") or "")
            public_url = self._public_media_url(url, settings) if url else None
            url_usable = public_url is not None
            if mode == "auto" and url_usable and self._qq_url_cooling():
                # 刚撞过「腾讯拉不到公网直链」：冷却期内 auto 直接分片，别再白等
                url_usable = False
            channel = choose_qq_upload_channel(
                mode=mode,
                size_bytes=size,
                direct_max_bytes=direct_max,
                url_max_bytes=url_max,
                url_usable=url_usable,
                auto_prefer=auto_prefer,
            )
            if channel == "native":
                # 混批里只要有一张判定该直传，就整批交回原逻辑：拆成两次投递会
                # 绕开「一次事件只投递一批图」的去重盾，用户可能收到重复图片
                logger.debug("qq_official 上传通道：该批含直传图，交回原回传逻辑")
                return False
            if channel in {"url", "url-scaled"} and public_url is None:
                logger.warning(
                    f"qq_official 固定直链通道但拿不到公网直链（图床地址疑似内网）: {url}"
                )
            plan.append((block, channel, public_url))

        if mark_compressed:
            # 本通道发的是原图（URL 原样 / 分片原始字节），配文按「原图」口径写，
            # 不能沿用压缩证据——文字说已压缩而用户收到原图是这个仓库的老毛病
            self._mark_compressed_captions(
                blocks, [CompressionEvidence.ORIGINAL] * len(images)
            )

        prefix = [header] if header else []
        sent_any = False
        for index, (block, channel, public_url) in enumerate(plan):
            own_text = str(block.get("text") or "").strip()
            parts = (*prefix, own_text) if index == 0 else (own_text,)
            content = "\n".join(part for part in parts if part)
            try:
                media = await self._qq_upload_block(
                    event, block, channel, public_url, target, max_upload
                )
                await self._qq_post_media(event, target, content, media)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    f"qq_official {channel} 通道上传/发送失败"
                    f"（第 {index + 1}/{len(plan)} 张）: {detail}"
                )
                if channel in {"url", "url-scaled"}:
                    # 公网直链拿得到、腾讯却拉不到：记冷却，auto 后续直接走 bytes
                    self._note_qq_url_unsupported(exc)
                if not sent_any and on_failure == "fallback":
                    logger.warning(
                        "qq_official 上传通道不可用，交回原回传逻辑（本地压缩/缩略图）"
                    )
                    return False
                await self._qq_notify(event, target, f"⚠️ 图片发送失败：{detail}")
                return True
            sent_any = True
        return True

    async def _qq_upload_block(
        self,
        event: AstrMessageEvent,
        block: dict,
        channel: str,
        public_url: str | None,
        target: tuple[str, str],
        max_upload: int,
    ):
        """按通道上传一张图，返回 QQ 侧 Media。

        url / url-scaled 共用同一条 URL 上传实现，区别只在交给腾讯的是原图
        直链还是图床缩放直链；bytes 即分片上传（官方术语 chunked upload），
        需要先把图落到本地。
        """
        if channel in {"url", "url-scaled"}:
            if not public_url:
                raise RuntimeError("没有公网直链，QQ 无法按 URL 拉取该图")
            upload_url = public_url
            if channel == "url-scaled":
                _mode, max_side, quality, style = self._delivery_config()
                upload_url = (
                    build_scaled_url(public_url, max_side, quality=quality, style=style)
                    or public_url
                )
            return await self._qq_upload_via_url(event, upload_url, target)
        path = str(block.get("path") or "")
        temp = ""
        try:
            if not path:
                temp = await self._download(
                    str(block.get("url") or ""), size_limit=max_upload
                )
                path = temp
            name = (
                str(block.get("file_name") or "").strip()
                or os.path.basename(path)
                or "image.jpg"
            )
            return await self._qq_upload_via_chunked(event, path, name, target)
        finally:
            # 分片通道只为这一次上传落盘，发完即删；临时目录另有 TTL 清理兜底
            self._remove_quiet(temp)

    async def _qq_upload_via_url(
        self, event: AstrMessageEvent, url: str, target: tuple[str, str]
    ):
        """URL 上传：只传 `url` 字段，由腾讯侧下载转存（图片软限 20MB）。

        借适配器自身的通用媒体上传方法（它对「非本地路径」正是走 url 分支），
        避免在插件里复刻 QQ 接口字段与错误码；AstrBot 发主动消息时也是用同样的
        伪事件（只带 bot 属性）调用它。
        """
        try:
            from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
                QQOfficialMessageEvent,
            )
        except ImportError as exc:  # 版本差异：模块或方法被改名
            raise RuntimeError(
                f"当前 AstrBot 版本缺少 qqofficial 媒体上传方法: {exc}"
            ) from exc
        helper = SimpleNamespace(bot=getattr(event, "bot", None))
        return await QQOfficialMessageEvent.upload_group_and_c2c_media(
            helper,
            url,
            QQ_IMAGE_FILE_TYPE,
            srv_send_msg=False,
            **self._qq_target_kwargs(target),
        )

    async def _qq_upload_via_chunked(
        self, event: AstrMessageEvent, path: str, name: str, target: tuple[str, str]
    ):
        """分片上传：prepare → 并发 PUT 分片 → 逐片确认 → 合并（硬限 200MB）。

        直接实例化适配器的分片器，而不是走 upload_group_and_c2c_media：后者只在
        「本地文件 > 10MB」时才分片，5~10MB 仍会落回 base64 分支（照样撞 5MB）。
        """
        try:
            from astrbot.core.platform.sources.qqofficial.qqofficial_chunked_upload import (
                QQOfficialChunkedUploader,
            )
        except ImportError as exc:
            raise RuntimeError(
                f"当前 AstrBot 版本缺少 qqofficial 分片上传器: {exc}"
            ) from exc
        uploader = QQOfficialChunkedUploader(event.bot.api._http)
        kind, session_id = target
        if kind == "group":
            return await uploader.upload_group(
                file_path=Path(path),
                file_type=QQ_IMAGE_FILE_TYPE,
                file_name=name,
                group_openid=session_id,
                srv_send_msg=False,
            )
        return await uploader.upload_c2c(
            file_path=Path(path),
            file_type=QQ_IMAGE_FILE_TYPE,
            file_name=name,
            user_openid=session_id,
            srv_send_msg=False,
        )

    async def _qq_post_media(
        self, event: AstrMessageEvent, target: tuple[str, str], content: str, media
    ) -> None:
        """自己发一条 media 消息（msg_type=7）。

        不走 event.send：那条路会把图片重新解析成 base64 交给 file_data 直传，
        正是本次要绕开的 5MB 限制。msg_seq 每条随机——被动回复中 (msg_id,
        msg_seq) 组合重复会被平台判失败。
        """
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

    async def _qq_notify(
        self, event: AstrMessageEvent, target: tuple[str, str], text: str
    ) -> None:
        """失败说明用纯文本回一条；这条发不出去也不影响主流程。"""
        try:
            api = event.bot.api
            message_id = str(
                getattr(getattr(event, "message_obj", None), "message_id", "") or ""
            ).strip()
            payload = {
                "msg_type": 0,
                "content": text,
                "msg_id": message_id or None,
                "msg_seq": random.randint(1, 10000),
            }
            kind, session_id = target
            if kind == "group":
                await api.post_group_message(group_openid=session_id, **payload)
            else:
                await api.post_c2c_message(openid=session_id, **payload)
        except Exception as exc:
            logger.debug(f"qq_official 失败说明发送失败（忽略）: {exc}")

    async def _random_send_url(self, media_url: str, settings: dict) -> str:
        """把随机到的原图换成它的缩略图直链；取不到替身就原样返回原图。

        缩略图与原图落在同一个 Qdrant 点上（点 id = UUID5(原图 id)），所以只要
        随机到的是内容目录里的原图，就能按 id 查出它的 thumb_url——不需要映射表，
        也不能靠文件名（图床给缩略图改过名）。整条链路都失败时静默退回原图：
        替身只是提速手段，绝不能因为它缺席而让 /随机图 发不出图。
        """
        if not settings.get("send_thumbnail", True):
            return media_url
        file_id = self._bed_file_id(media_url)
        if not file_id:
            return media_url
        if not self.vector.qdrant_ready:
            # 向量库没配好就无从查替身；这不是错误，直接发原图
            return media_url
        try:
            thumb = await self.vector.thumb_url_for(file_id)
        except Exception as exc:  # thumb_url_for 已兜错，这里再保一层
            logger.warning(f"查询缩略图失败，改发原图: {exc}")
            return media_url
        if not thumb:
            logger.debug(f"该图无缩略图替身，按原图发送: {file_id}")
            return media_url
        logger.debug(f"随机图改发缩略图替身: {file_id} -> {thumb}")
        lan_hosts = self._lan_url_hosts(settings)
        if lan_hosts:
            # Qdrant 登记的 thumb_url 是公网直链，内网直连模式下改写为内网源
            thumb = swap_url_origin(thumb, str(settings.get("lan_base_url") or ""), lan_hosts)
        return thumb

    @staticmethod
    def _random_caption(
        media_url: str, kind: str, settings: dict, *, display_name: str = ""
    ) -> str:
        """随机媒体回传文案；show_file_info 关闭或取不到文件名时用固定文案。

        文件名一律经 clean_display_name 清洗：图床给缩略图改名时会前置毫秒
        时间戳（`1789658616018_【微博_…】.jpg`），这个前缀对用户毫无意义，
        纯属噪声，绝不能出现在配文里。清洗只做「去时间戳前缀」这一步确定的
        处理，不猜测、不改写其余内容。

        display_name 供调用方传入**更权威**的名字（如反查回原图时取自 Qdrant
        payload.file_name 的原始名）。它的价值在于缩略图名的 `@`→`_` 是**不可逆**
        的，光靠清洗缩略图名永远还原不出真名；有 payload 真名时优先用它。
        """
        label = "图片" if kind == "image" else "视频"
        fallback = f"随机{label}发送成功"
        if not settings.get("show_file_info", True):
            return fallback
        filename = str(display_name or "").strip() or clean_display_name(media_url)
        if not filename:
            return fallback
        icon = "🖼️" if kind == "image" else "🎬"
        return f"{icon} {filename}"

    @filter.llm_tool(name="sendRandomMedia")
    async def send_random_media(
        self,
        event: AstrMessageEvent,
        directory: str | None = None,
        content_type: str | None = None,
    ):
        """发送随机图片或视频。

        当用户请求随机图片或视频时使用此工具。

        Args:
            directory(string): 目录路径，指定从哪个目录获取随机媒体，可选。
            content_type(string): 内容类型，可选值为 image 或 video，可选。
        """
        settings = self._random_settings()
        if not settings["enable_llm"]:
            yield event.plain_result("LLM 调用随机媒体已被禁用，请使用 /随机图 或 /随机视频 命令")
            return
        parsed_directory, parsed_type = extract_directory(event.message_str or "")
        if directory is None and parsed_directory:
            directory = parsed_directory
        content_type = (parsed_type or content_type or "image").strip().lower()
        async for result in self._random_media_delivery(event, directory, content_type, settings):
            yield result

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def _stop_remote_hash_refresh(self, errors: list) -> None:
        """停掉后台刷新任务，避免卸载后任务还在写缓存文件。

        取消防抖：后台任务可能正卡在 47 页翻页中间，直接 await 会拖慢卸载；
        给 5 秒宽限，超时就放弃等待（任务自身会在下一轮 await 点收到取消）。
        """
        task = self._remote_hash_task
        self._remote_hash_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.CancelledError, TimeoutError):
            pass
        except Exception as e:
            errors.append(f"图床哈希刷新任务: {e}")

    async def initialize(self) -> None:
        """AstrBot 在事件循环起来后调用；此时才可以安全创建后台任务。

        图床哈希索引的刷新必须在这里启动，不能在 __init__：那会儿还没有运行中
        的事件循环，asyncio.create_task 会直接抛错。缓存为空时也照常刷新——
        首次启动后第一次 /溯源 不该因为「索引还没拉」而报未命中。
        """
        # 提前注册内网直连主机白名单：/原图 可能先于任何随机图命令运行，
        # 而它要取的直链可能是历史里记下的内网地址
        self._random_settings()
        if self.remote_hash_on():
            self.start_remote_hash_refresh()

    async def terminate(self):
        errors: list[str] = []
        # 内网直连白名单随插件卸载清理，避免残留到同进程的其它插件实例
        set_allowed_private_hosts(())
        await self._stop_remote_hash_refresh(errors)
        if self._http is not None:
            try:
                await self._http.close()
            except Exception as e:
                errors.append(f"HTTP 会话: {e}")
        try:
            await self.bed.close()
        except Exception as e:
            errors.append(f"图床客户端: {e}")
        try:
            self._cleanup_tmp()
        except Exception as e:
            errors.append(f"临时文件: {e}")
        try:
            self.library.close()
        except Exception as e:
            errors.append(f"图库数据库: {e}")
        if errors:
            logger.error("图片溯源插件卸载时部分资源清理失败: " + "；".join(errors))
        logger.info("图片溯源插件已卸载。")

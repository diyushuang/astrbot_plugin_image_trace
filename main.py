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

# E402：下面的合成包引导必须先于包内相对导入执行（仅此一处例外）。
# ruff: noqa: E402

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import random
import secrets
import time
from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta, timezone
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

# 只保留一份导入清单：统一按包内相对导入。若插件被作为独立模块加载
# （__package__ 为空），先向 sys.modules 注册一个指向本目录的合成包，
# 使相对导入成立，从而不必再维护「包加载 / 平铺加载」两套清单。
if __package__ in (None, ""):
    import sys as _sys
    import types as _types

    _pkg_name = "astrbot_plugin_image_trace"
    if _pkg_name not in _sys.modules:
        _pkg = _types.ModuleType(_pkg_name)
        _pkg.__path__ = [str(Path(__file__).resolve().parent)]
        _sys.modules[_pkg_name] = _pkg
    __package__ = _pkg_name

from .checkin_card import QuoteLibrary, compose_poster, strip_emoji
from .checkin_store import CheckinStore
from .common import (
    AI_VERIFY_MAX_CANDIDATES,
    MEMORY_DOWNLOAD_MAX_BYTES,
    PLAN_TOTAL_TIMEOUT,
    PROBE_CONCURRENCY,
    QQ_URL_COOLDOWN_SECONDS,
    SCALED_VERIFY_COOLDOWN_SECONDS,
    TMP_MAX_AGE_SECONDS,
    as_int,
    read_bytes,
    sanitize_error_text,
)
from .config.loader import (
    bool_cfg,
    bucket_config,
    checkin_settings,
    delivery_config,
    dict_group,
    effective_bed_cfg,
    encode_options,
    engine_choice,
    ensure_group_cfg,
    first_value,
    float_cfg,
    int_cfg,
    napcat_delivery_cfg,
    onebot_image_timeout,
    qq_delivery_cfg,
    qq_upload_config,
    random_settings,
    scalar_cfg,
    scan_dirs,
    vector_index_on_register,
    verify_scaled,
)
from .config.migrations import (
    BUCKET_MODES,
    LAYOUT_VERSION,
    SCALAR_PATHS,
    migrate_bucket_config,
    migrate_config_layout,
)
from .config.typed import DEFAULT_BASIC, DEFAULT_LIMITS
from .controllers import (
    AdminController,
    CheckinController,
    OriginalController,
    RandomController,
    RegisterController,
    TraceController,
)
from .delivery import (
    ChainChannel,
    DeliveryConfig,
    DeliveryContext,
    DeliveryProfile,
    DeliveryRequest,
    OneBotChannel,
    QQOfficialChannel,
    plan_delivery,
)
from .delivery.primitives import (
    local_compress_delivery,
    mark_compressed_captions,
    mention_prefix,
    prepare_delivery_blocks,
    send_via_onebot,
    url_delivery_plan,
)
from .features import (
    avif_available,
    heif_available,
    image_bytes_ok,
    image_file_ok,
    image_mime,
    phash_hex_len,
)
from .hash_index import REMOTE_HASH_PAYLOAD_KEYS, HashIndex
from .http_client import GuardedHttpClient
from .image_bed import MODE_CFB
from .image_delivery import (
    CompressionEvidence,
    DeliveryTarget,
    SendOutcome,
    build_original_url,
    build_scaled_url,
    choose_qq_upload_channel,
    delivery_target,
    is_public_http_url,
    qq_upload_safe_image,
    upgrade_to_https,
)
from .library import ImageLibrary
from .media.image_source import (
    ImageSource,
    ResolvedImage,
    decode_fail_message,
    download_fail_message,
    image_download_headers,
    range_probe_headers,
)
from .media_history import MediaHistory
from .random_media import (
    RandomMediaClient,
    clean_display_name,
    is_thumbnail_url,
    media_filename,
    swap_url_origin,
)
from .search import (
    AutoSearchEngine,
    HashSearchEngine,
    VectorSearchEngine,
    miss_message,
)
from .services import (
    CheckinService,
    OriginalService,
    RandomService,
    TraceService,
)
from .storage import StorageRegistry
from .url_guard import read_limited_bytes
from .vector_search import VectorEngine
from .weibo_posts import (
    WeiboPost,
    load_post_map,
    load_posts_from_dir,
    map_is_stale,
)

# 兼容旧测试/外部引用：这些常量已迁到 config.migrations，main 只保留别名。
_LAYOUT_VERSION = LAYOUT_VERSION
_SCALAR_PATHS = SCALAR_PATHS

PLUGIN_NAME = "astrbot_plugin_image_trace"
# 扫描目录认可的图片扩展名。TIFF 由 Pillow 原生支持；AVIF 只有 Pillow 11.3+
# 能解码，旧环境收进来只会让每张 AVIF 都记一条「扫描失败」，故按能力开关。
# HEIC/HEIF 同理：没有 pillow-heif 时每张都会解码失败，不应纳入扫描。
IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".jfif",
    ".tif",
    ".tiff",
} | ({".heic", ".heif"} if heif_available() else set()) | (
    {".avif"} if avif_available() else set()
)
# QQ 开放平台富媒体 file_type：1=图片、2=视频、3=语音、4=文件
QQ_IMAGE_FILE_TYPE = 1
# 假格式转码前的像素上限：与 features / 签到卡片同一量级，防解压炸弹
QQ_TRANSCODE_MAX_PIXELS = 50_000_000

# ----------------------------------------------------------------------
# 配置布局（1.7.14）：同类项合并进 basic / delivery / limits 三个组。
# 旧布局（顶层散项 + image_delivery / qq_upload 两组）继续可读——新键优先、
# 旧键回落。映射表、版本号与迁移实现已收口到 config/migrations.py，
# main 只保留同名薄委托。
# ----------------------------------------------------------------------



# 坏图摘要 / ResolvedImage 已迁到 media/image_source.py，这里通过导入复用；
# 储存桶（对象存储）的常量与完整判定在 config/migrations.py、config/loader.py。


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
        hash_size = as_int(self._cfg("hash_size"), DEFAULT_BASIC.hash_size)
        if not 4 <= hash_size <= 32 or hash_size % 4 != 0:
            logger.warning(
                f"hash_size 配置值 {hash_size} 无效（须为 4~32 之间 4 的倍数，"
                "如 8/16/24/32），已回退为默认 16。"
            )
            hash_size = DEFAULT_BASIC.hash_size
        self.hash_size = hash_size
        self.library = ImageLibrary(
            os.path.join(self.data_dir, "library.db"),
            expected_phash_hex_len=phash_hex_len(hash_size),
        )
        self.hash_threshold = max(
            0.0,
            min(1.0, self._float_cfg("similarity_threshold", DEFAULT_BASIC.hash_similarity_threshold)),
        )
        # 配置上限：这两个值直接放大网络请求量与消息长度，手改离谱值时必须收敛
        self.top_n = max(0, min(50, self._int_cfg("top_n", DEFAULT_BASIC.top_n)))
        self.max_images_per_query = max(
            1,
            min(10, self._int_cfg("max_images_per_query", DEFAULT_BASIC.max_images_per_query)),
        )
        # v1.2.x 的储存桶配置先迁移，再合并（储存桶完整时优先于图床）
        self._migrate_bucket_config()
        self._bed_cfg = self._effective_bed_cfg()
        self._http: GuardedHttpClient | None = None
        # 内网直连白名单按实例持有：交给本实例的 GuardedHttpClient，避免多个
        # 插件实例通过模块级全局集合互相覆盖（旧实现 terminate 会清掉别人的白名单）
        self._allowed_private_hosts: set[str] = set()
        self.storage = StorageRegistry(self.data_dir, self._bed_cfg, self._get_http)
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
        # 每日签到：独立 SQLite 记录（按用户全局一天一次）与语句库（首次取句时构造）。
        # 签到是附属功能：DB 打不开时降级为「签到不可用」，不能拖垮整个插件加载。
        self.checkin_store: CheckinStore | None = None
        try:
            self.checkin_store = CheckinStore(os.path.join(self.data_dir, "checkin.db"))
        except Exception as exc:
            logger.error(f"签到数据库初始化失败，/签到 将不可用: {exc}")
        self._quote_library_cache: QuoteLibrary | None = None
        # 微博签到：文案源目录 + 预生成映射表。映射表由 scripts/build_weibo_map.py
        # 离线生成，签到只读本地 JSON，不请求微博、不算 pHash。
        self.weibo_dir = os.path.join(self.data_dir, "weibo")
        self.weibo_thumb_dir = os.path.join(self.weibo_dir, "thumbs")
        try:
            os.makedirs(self.weibo_thumb_dir, exist_ok=True)
        except OSError as exc:
            logger.warning(f"微博文案目录创建失败，微博签到将回退随机语句: {exc}")
        self._weibo_posts_cache: list[WeiboPost] | None = None
        self._weibo_meta_cache: dict = {}
        self._weibo_signature: tuple | None = None
        self._weibo_empty_warned = False
        # 图床缩放能力冷却截止时刻（time.monotonic 口径，0 表示未观测到拒绝）。
        # 探测到 API 级拒绝（405/501 等）后的一段时间内不再探测缩放版。
        self._scaled_verify_unsupported_until = 0.0
        # QQ URL 上传通道冷却截止时刻：图床公网域名存在但腾讯侧拉不到时，auto
        # 模式在一段时间内直接分片，避免每次都先白撞一次必败的 URL 上传
        self._qq_url_unsupported_until = 0.0
        # 图片获取（解析/兜底下载/URL 探测）：事件与消息组件的适配留在本类，
        # 内部只依赖注入的 HTTP 客户端、临时目录与大小上限
        self.image_source = ImageSource(
            http_getter=self._get_http,
            tmp_dir=self.tmp_dir,
            remove_file=self._remove_quiet,
            size_limit_bytes=self._download_limit_bytes,
            on_scaled_unsupported=self._note_scaled_unsupported,
            heif_ready=lambda: heif_available(),
            memory_limit_bytes=lambda: MEMORY_DOWNLOAD_MAX_BYTES,
        )
        self._cleanup_tmp()
        # 命令处理器实现在 controllers/；这里组装一次，main 只保留命令注册与
        # 同名薄委托（测试对 plugin._xxx 的 monkeypatch 仍按实例属性生效）
        self.controllers = SimpleNamespace(
            trace=TraceController(self, self.config),
            register=RegisterController(self, self.config),
            original=OriginalController(self, self.config),
            random=RandomController(self, self.config),
            checkin=CheckinController(self, self.config),
            admin=AdminController(self, self.config),
        )
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

        # R1：业务流程服务。服务只接收 host 门面，不反向 import main；
        # 原有 self._xxx 委托仍保留，测试的 monkeypatch 点不变。
        self.services = SimpleNamespace(
            trace=TraceService(
                host=self,
                bool_cfg=lambda key, default=None: self._bool_cfg(key, default),
            ),
            checkin=CheckinService(
                host=self,
                checkin_settings=lambda: self._checkin_settings(),
                random_settings=lambda: self._random_settings(),
            ),
            random=RandomService(host=self),
            original=OriginalService(host=self),
        )

    # ------------------------------------------------------------------
    # 图床哈希索引（后台刷新）
    # ------------------------------------------------------------------

    def remote_hash_on(self) -> bool:
        """图床哈希索引开关（默认开；只需 Qdrant 可达，不依赖 embedding）。

        拉取只调用 Qdrant scroll，不碰向量化接口；因此启用判定用 qdrant_ready
        而不是 vector.enabled——只配 Qdrant、不配 embedding 的纯哈希部署同样
        应该能用图床哈希索引。
        """
        return self._bool_cfg("remote_hash_enabled", DEFAULT_LIMITS.remote_hash_enabled) and bool(
            getattr(self.vector, "qdrant_ready", False)
        )

    def _remote_hash_ttl(self) -> int:
        """缓存最长有效期（秒）。缓存新鲜时启动/溯源都不重新拉取。"""
        return max(300, self._int_cfg("remote_hash_ttl", DEFAULT_LIMITS.remote_hash_ttl))

    def _remote_hash_max_points(self) -> int:
        """拉取上限（0 = 不限）。用于先小样本验证链路，再放开全量。"""
        return max(
            0, self._int_cfg("remote_hash_max_points", DEFAULT_LIMITS.remote_hash_max_points)
        )

    def _remote_hash_cache_fresh(self) -> bool:
        """本地缓存是否仍在 remote_hash_ttl 有效期内且确实有可用条目。

        仅有 built_at 不够：切换 hash_size 后旧缓存全部长度不符，load_cache
        仍会返回成功但 count 为 0；此时若按 TTL 判「新鲜」，图床哈希腿会静默
        空转直到 TTL 到期。要求 count > 0 才能在空缓存时触发重建。
        """
        built_at = float(getattr(self.remote_hash, "built_at", 0.0) or 0.0)
        return (
            built_at > 0
            and self.remote_hash.count > 0
            and (time.time() - built_at) < self._remote_hash_ttl()
        )

    def _maybe_start_remote_hash_refresh(self) -> None:
        """缓存过期或缺失时才启动刷新。

        此前 /溯源 每次都调 start_remote_hash_refresh，而任务完成后 done() 为真，
        于是刷新完的下一次溯源又会翻一遍全量 Qdrant（2.4 万点/47 页）。用 TTL
        收口后：缓存新鲜就不刷；过期后由重启或下一次 /溯源 触发一次重建。
        """
        if not self.remote_hash_on() or self._remote_hash_cache_fresh():
            return
        self.start_remote_hash_refresh()

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

        直接调用表示“此刻就刷”；常规入口应走 _maybe_start_remote_hash_refresh，
        由 remote_hash_ttl 决定要不要刷。

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
        """取对象型配置；实现见 config.loader.dict_group。"""
        return dict_group(self.config, key)

    def _ensure_group(self, name: str) -> dict:
        """取（必要时创建）对象型配置组，实现见 config.migrations。"""
        return ensure_group_cfg(self.config, name)

    def _cfg(self, key: str):
        """读标量配置：新布局（basic / limits）优先，回落旧顶层键。"""
        return scalar_cfg(self.config, key)

    def _int_cfg(self, key: str, default: int) -> int:
        return int_cfg(self.config, key, default)

    def _float_cfg(self, key: str, default: float) -> float:
        return float_cfg(self.config, key, default)

    def _bool_cfg(self, key: str, default: bool) -> bool:
        """布尔配置：未配置（缺键/空串/null）时取默认值，与数值配置口径一致。"""
        return bool_cfg(self.config, key, default)

    def _migrate_config_layout(self) -> None:
        """把旧布局搬进 basic / delivery / limits；实现见 config.migrations。"""
        migrate_config_layout(self.config)

    def _napcat_delivery_cfg(self) -> dict:
        """推送通道（NapCat）配置，实现见 config.loader.napcat_delivery_cfg。"""
        return napcat_delivery_cfg(self.config)

    def _qq_delivery_cfg(self) -> dict:
        """推送通道（QQ 官方）配置，实现见 config.loader.qq_delivery_cfg。"""
        return qq_delivery_cfg(self.config)

    @staticmethod
    def _first_value(*values):
        """取第一个非空值（0 / False 是合法配置，不能按假值跳过）。"""
        return first_value(*values)

    def _migrate_bucket_config(self) -> None:
        """v1.2.x 储存桶配置迁移；实现见 config.migrations。"""
        migrate_bucket_config(self.config)

    def _bucket_config(self) -> dict | None:
        """储存桶配置完整时返回可并入图床配置的 dict（含 mode），否则 None。"""
        return bucket_config(self.config)

    def _effective_bed_cfg(self) -> dict:
        """合并储存桶与图床两组配置：储存桶配置完整时优先（覆盖 mode 与字段）。"""
        return effective_bed_cfg(self.config)

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
        return engine_choice(self.config)

    def _vector_index_on_register(self) -> bool:
        return vector_index_on_register(self.config)

    def _scan_dirs(self) -> list:
        return scan_dirs(self.config)

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

    @staticmethod
    def _probe_mime(path: str):
        """解析探测：返回 (None, mime) 表示有效，None 表示无效；不抛错。"""
        mime = image_mime(path)
        return (None, mime) if mime else None

    async def _resolve_local(self, seg, *, probe) -> ResolvedImage:
        """把图片段解析为本地文件（解析/兜底下载/失败分诊见 media.image_source）。"""
        return await self.image_source.resolve(seg, probe=probe)

    @staticmethod
    def _download_fail_message() -> str:
        return download_fail_message()

    @staticmethod
    def _decode_fail_message(sniffed: str) -> str:
        """认得出容器却解不开时的提示；HEIC 缺解码器是最常见的一种。"""
        return decode_fail_message(sniffed, heif_ready=heif_available())

    def _download_limit_bytes(self) -> int:
        """回传兜底下载的大小上限（字节）：limits.max_download_mb。"""
        return max(1, self._int_cfg("max_download_mb", DEFAULT_LIMITS.max_download_mb)) * 1024 * 1024

    @staticmethod
    def _download_timeout() -> aiohttp.ClientTimeout:
        """兜底下载/格式体检统一的出网超时（实现见 media.image_source）。"""
        return ImageSource._timeout()

    async def _stream_response_to_temp(self, resp, limit: int) -> str:
        """把已打开的响应流式写入临时文件（实现见 media.image_source）。"""
        return await self.image_source.stream_response_to_temp(resp, limit)

    async def _download(self, url: str, *, size_limit: int | None = None) -> str:
        """下载到临时文件，返回路径（实现见 media.image_source）。"""
        return await self.image_source.download(url, size_limit=size_limit)

    async def _download_bytes(self, url: str) -> bytes:
        """下载到内存（实现见 media.image_source）。"""
        return await self.image_source.download_bytes(url)

    @staticmethod
    def _image_download_headers(u: str, _m: str, _c: bool) -> dict:
        """图片兜底下载的请求头（实现见 media.image_source）。"""
        return image_download_headers(u, _m, _c)

    @classmethod
    def _range_probe_headers(cls, u: str, m: str, c: bool) -> dict:
        """在下载请求头基础上加 Range: bytes=0-0（实现见 media.image_source）。"""
        return range_probe_headers(u, m, c)

    async def _probe_url(
        self, url: str, cache: dict | None = None, *, allow_range: bool = True
    ) -> tuple[bool | None, int | None]:
        """轻量探测 URL 存在性与响应体长度（实现见 media.image_source）。"""
        return await self.image_source.probe(url, cache, allow_range=allow_range)

    async def _probe_url_uncached(
        self, url: str, *, allow_range: bool = True
    ) -> tuple[bool | None, int | None]:
        """探测实现（实现见 media.image_source）。"""
        return await self.image_source.probe_uncached(url, allow_range=allow_range)

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
        return verify_scaled(self.config)

    def _onebot_image_timeout(self) -> int:
        """delivery.napcat_timeout：OneBot image 段的 timeout 字段（秒）。

        实现与默认值见 config.loader.onebot_image_timeout；填 0 会落到 10，
        不会原样写 0（否则等于告诉协议端「立刻放弃下载」）。
        """
        return onebot_image_timeout(self.config)

    async def _get_http(self) -> GuardedHttpClient:
        if self._http is None:
            # 连接器启用 IP 钉扎：出网连接只允许落在 url_guard 校验过的地址上。
            # 内网直连白名单按实例传入，避免多实例共享模块级全局集合。
            self._http = GuardedHttpClient(self._allowed_private_hosts)
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
        return delivery_config(self.config)

    def _random_settings(self) -> dict:
        """归一化随机图配置组（random_media）；实现见 config.loader。"""
        return random_settings(self.config, self._allowed_private_hosts)

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

    def _checkin_settings(self) -> dict:
        """归一化每日签到配置（checkin 组）；实现见 config.loader。"""
        return checkin_settings(self.config)

    @staticmethod
    def _checkin_today(offset_hours: int) -> date:
        """按配置的 UTC 偏移算出「今天」，与服务器本地时区无关。"""
        return datetime.now(timezone(timedelta(hours=offset_hours))).date()

    @staticmethod
    def _checkin_sender_name(event: AstrMessageEvent, limit: int = 16) -> str:
        """签到者昵称：取不到就返回空串，任何异常都不该打断签到。

        昵称由协议端（QQ / OneBot）给出，长度与内容都不可控：这里只做空白
        折叠与限长，emoji 留给 ``compose_poster`` 在绘制前剔除——聊天消息里
        的昵称可以带 emoji，卡片上不行（中文字体没有彩色 emoji 字形）。
        """
        try:
            raw = str(event.get_sender_name() or "")
        except Exception:
            return ""
        name = " ".join(raw.split())
        if len(name) > limit:
            name = name[:limit].rstrip() + "…"
        return name

    def _checkin_line(self, name: str, outcome) -> str:
        """签到信息行：带昵称时「小双 · 连续签到 3 天 · 累计 10 天」。

        卡片页脚专用，所以昵称先过一遍 ``strip_emoji``：``compose_poster`` 本来
        也会剔除，但那是**整段页脚**统一处理——昵称全是 emoji 时（「🍂🍂」）
        拼好的 ``· 连续签到 3 天`` 会剩下一个孤零零的分隔符。这里先判定「昵称
        剥完还有没有可见字」，没有就整段省略；消息文字里的昵称不受影响。
        """
        detail = f"连续签到 {outcome.streak} 天 · 累计 {outcome.total} 天"
        visible = strip_emoji(name).strip(" ·…")
        return f"{visible} · {detail}" if visible else detail

    def _quote_library(self) -> QuoteLibrary:
        """惰性构造语句库；文本框留空时读取 quotes.txt，缺失则用内置语句初始化。"""
        if self._quote_library_cache is None:
            settings = self._checkin_settings()
            self._quote_library_cache = QuoteLibrary(
                os.path.join(self.data_dir, "quotes.txt"),
                local_text=settings["quote_local_text"],
                source=settings["quote_source"],
                api_url=settings["quote_api_url"],
                json_path=settings["quote_json_path"],
                headers=settings["quote_api_headers"],
                timeout=settings["quote_timeout"],
                http_getter=self._get_http,
            )
        return self._quote_library_cache

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
        # R2：实现已下沉到 delivery/primitives.py，签名不变。
        return mention_prefix(self, event)

    @staticmethod
    def _event_already_sent(event: AstrMessageEvent) -> bool:
        """本次事件是否已经投递过图片（事件级去重盾）。"""
        return bool(getattr(event, "_image_trace_sent", False))

    @staticmethod
    def _reset_event_sent(event: AstrMessageEvent) -> None:
        """复位投递标记，供多图查询在两轮不同图片之间使用。

        去重盾的职责是阻止**同一批图片**在回退路径上重复发送，而不是限制
        整个事件只能发一次；/溯源 一条消息可带多张图（max_images_per_query），
        每张图都是独立查询，必须在下一轮开始前复位。
        """
        try:
            event._image_trace_sent = False
        except AttributeError:  # 少数事件实现带 __slots__，标记失败不影响发送
            logger.debug("事件对象不支持动态属性，跳过已发送标记复位")

    @staticmethod
    def _mark_event_sent(event: AstrMessageEvent) -> None:
        """标记本次事件已投递图片，禁止后续任何回退路径再发一次。"""
        try:
            event._image_trace_sent = True
        except AttributeError:  # 少数事件实现带 __slots__，标记失败不影响发送
            logger.debug("事件对象不支持动态属性，跳过已发送标记")

    @staticmethod
    def _defer_event_stop(event: AstrMessageEvent, defer: bool | None = None) -> bool:
        """多图溯源期间延迟 stop_event，避免管道在首图后中断生成器。

        AstrBot 的调度器一旦发现 event 已 stopped，会停止继续消费当前处理器
        的异步生成器；而 /溯源 需要按 max_images_per_query 依次处理多张图。
        故在整轮循环内置位，循环结束由 trace 统一 stop。
        """
        if defer is not None:
            try:
                event._image_trace_defer_stop = bool(defer)
            except AttributeError:
                logger.debug("事件对象不支持动态属性，跳过延迟 stop 标记")
            return bool(defer)
        return bool(getattr(event, "_image_trace_defer_stop", False))

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
        # R2：实现已下沉到 delivery/primitives.py，签名不变。
        return await send_via_onebot(
            self, event, header, blocks, payloads,
            image_timeout=self._onebot_image_timeout(),
        )

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
        # R2：实现已下沉到 delivery/primitives.py，签名不变。
        mark_compressed_captions(self, blocks, evidences)

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
        # R2：实现已下沉到 delivery/primitives.py；配置仍在 main 读取后注入。
        mode, max_side, quality, _ = self._delivery_config()
        return await prepare_delivery_blocks(
            self, blocks, compress=compress,
            delivery_config=(mode, max_side, quality),
            encode_options=self._encode_options(),
        )

    async def _local_compress_delivery(
        self,
        event: AstrMessageEvent,
        header: str,
        blocks: list[dict],
        image_blocks: list[dict],
        *,
        mark_compressed: bool,
    ) -> SendOutcome | None:
        # R2：实现已下沉到 delivery/primitives.py，签名不变。
        return await local_compress_delivery(
            self, event, header, blocks, image_blocks,
            mark_compressed=mark_compressed,
        )

    def _encode_options(self) -> tuple[int, bool]:
        """本地压缩进阶选项：目标体积上限（字节，0 表示不限制）与 WebP 输出。"""
        return encode_options(self.config)

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
        # R2：实现已下沉到 delivery/primitives.py；缩放实测开关由 main 注入。
        return await url_delivery_plan(
            self, image_blocks, max_side, mode,
            quality=quality, scaled_url_style=scaled_url_style,
            verify_scaled=self._verify_scaled(),
        )

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

        投递成功后默认 stop_event 防 respond stage 重发；多图 /溯源 通过
        `_defer_event_stop` 把 stop 推迟到整轮结束，否则管道会在首图后中断
        当前处理器，后续图片不再处理。

        remember_blocks 供「发缩略图、记原图」使用：/随机图 发出去的是替身
        （thumb_url），但会话历史必须留下原图直链，否则 /原图 只能找回那张 720px
        的缩略图。缺省 None 表示记的就是本次发出的 blocks（其余调用点行为不变）。
        """
        if self._event_already_sent(event):
            logger.warning("本次事件已投递过图片，跳过重复投递")
            return
        target = self._delivery_target(event)
        self._remember_blocks(event, blocks if remember_blocks is None else remember_blocks)
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
                blocks, handled = await self._offbox_pipeline(
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
                    self._mark_event_sent(event)
                    if not self._defer_event_stop(event):
                        event.stop_event()
                    return
            mode, max_side, quality, scaled_url_style = self._delivery_config()
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
            image_blocks = [block for block in blocks if block.get("url") or block.get("path")]

            # 回传计划最多只算一次：回退路径此前会重跑一遍规划，把同样的探测白做
            # 第二次（图床还会因此多做一次缩放处理）。
            url_plan: tuple[list[str], list[CompressionEvidence]] | None = None
            ctx = DeliveryContext(
                event=event, request=request, plan=plan, image_blocks=image_blocks
            )
            if plan.direct_ok:
                url_plan = await self._url_delivery_plan(image_blocks, max_side, mode, quality=quality, scaled_url_style=scaled_url_style)
                ctx.url_plan = url_plan
                result = await OneBotChannel(self).deliver_url(ctx)
                if result.outcome != "failed":
                    # SENT 已送达；UNKNOWN 可能已送达——两者都不能再发第二次
                    self._mark_event_sent(event)
                    if not self._defer_event_stop(event):
                        event.stop_event()
                    return

            if plan.local_compress:
                # OneBot 下 auto/bytes 直发压缩字节（混合载荷）：压缩结果
                # 经 base64 段直达 QQ，不经协议端二次下载，压缩 100% 生效
                result = await OneBotChannel(self).deliver_compressed(ctx)
                if result.outcome != "failed":
                    # SENT 已送达；UNKNOWN 可能已送达——两者都不能再发第二次
                    self._mark_event_sent(event)
                    if not self._defer_event_stop(event):
                        event.stop_event()
                    return
                # 准备失败 或 直发失败：字节路径已试过，整批改走 URL 链
                logger.warning("OneBot 压缩直发未成功，改用标准消息链 URL 发送")
            elif plan.local_bytes_chain:
                result = await ChainChannel(self).deliver_bytes(ctx)
                if result.outcome != "failed":
                    self._mark_event_sent(event)
                    yield event.chain_result(result.chain)
                    return
                logger.warning("本地图片回退失败，改用标准消息链 URL 发送")

            # 走到这里说明没有任何通道明确送达（UNKNOWN 已在上面按已送达拦截）。
            if self._event_already_sent(event):
                return
            if url_plan is None:
                url_plan = await self._url_delivery_plan(image_blocks, max_side, mode, quality=quality, scaled_url_style=scaled_url_style)
            ctx.url_plan = url_plan
            result = await ChainChannel(self).deliver_url(ctx)
            if result.outcome == "failed":
                return
            self._mark_event_sent(event)
            yield event.chain_result(result.chain)
        finally:
            # 生成器被消费完/关闭时执行：此时消息已由 OneBot 直发或由 respond
            # stage 发完，删除转码产物是安全的
            for owned in owned_paths:
                self._remove_quiet(owned)

    # ------------------------------------------------------------------
    # 指令：溯源
    # ------------------------------------------------------------------

    @filter.command("溯源", alias={"找原图"})
    async def trace(self, event: AstrMessageEvent):
        """发送或引用一张图片，按当前引擎（hash/vector/auto）找出相似的原图并回传"""
        async for result in self.controllers.trace.trace(event):
            yield result

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
        """未命中回复的统一拼装（实现已迁到 search.base，保留同名薄委托）。"""
        return miss_message(fail_line, candidates, empty_note)

    def _hash_engine(self) -> HashSearchEngine:
        """按当前实例状态构造哈希合并引擎（属性动态读取，兼容测试替换）。"""
        return HashSearchEngine(
            self.library,
            self.remote_hash,
            threshold=self.hash_threshold,
            top_n=self.top_n,
            remote_enabled=self.remote_hash_on,
        )

    def _vector_engine(self) -> VectorSearchEngine:
        return VectorSearchEngine(self.vector, top_n=self.top_n)

    def _auto_engine(self) -> AutoSearchEngine:
        return AutoSearchEngine(
            self._vector_engine(),
            self._hash_engine(),
            hash_pool_size=self._hash_pool_size,
            want=max(5, self.top_n),
        )

    def _merged_hash_candidates(self, phash: str, want: int) -> tuple:
        """两条腿合并检索（实现已迁到 search.hash_engine，签名与返回不变）。"""
        result = self._hash_engine().merged_candidates(phash, want)
        return result.hits, result.candidate_lines

    def _candidate_note(self, match) -> str:
        """候选行括注（实现已迁到 search.hash_engine，保留同名薄委托）。"""
        return HashSearchEngine.candidate_note(match)

    async def _trace_hash(self, event: AstrMessageEvent, seg, resolved: ResolvedImage | None=None) -> AsyncGenerator:
        # R1：流程主体在 services/trace.py，main 只留同名薄委托。
        async for result in self.services.trace._trace_hash(event, seg, resolved):
            yield result

    async def _hash_outcome_delivery(self, event: AstrMessageEvent, query_path: str, hits: list, candidate_lines: list) -> AsyncGenerator:
        # R1：流程主体在 services/trace.py，main 只留同名薄委托。
        async for result in self.services.trace._hash_outcome_delivery(event, query_path, hits, candidate_lines):
            yield result

    async def _hash_hit_result(self, event: AstrMessageEvent, best) -> AsyncGenerator:
        # R1：流程主体在 services/trace.py，main 只留同名薄委托。
        async for result in self.services.trace._hash_hit_result(event, best):
            yield result

    # ------------------------------------------------------------------
    # 向量引擎
    # ------------------------------------------------------------------

    async def _vector_search_one(self, event, seg, resolved: ResolvedImage | None=None):
        # R1：流程主体在 services/trace.py，main 只留同名薄委托。
        return await self.services.trace._vector_search_one(event, seg, resolved)

    async def _trace_vector(self, event: AstrMessageEvent, seg) -> AsyncGenerator:
        # R1：流程主体在 services/trace.py，main 只留同名薄委托。
        async for result in self.services.trace._trace_vector(event, seg):
            yield result

    async def _trace_auto(self, event: AstrMessageEvent, seg) -> AsyncGenerator:
        # R1：流程主体在 services/trace.py，main 只留同名薄委托。
        async for result in self.services.trace._trace_auto(event, seg):
            yield result

    async def _vector_hits_delivery(self, event: AstrMessageEvent, hits: list) -> AsyncGenerator:
        # R1：流程主体在 services/trace.py，main 只留同名薄委托。
        async for result in self.services.trace._vector_hits_delivery(event, hits):
            yield result

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
        async for result in self.controllers.register.register_image(event):
            yield result

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
        async for result in self.controllers.trace.status(event):
            yield result

    def _remote_hash_line(self) -> str:
        """/溯源状态 的图床哈希索引一行。

        排查「为什么图床的图哈希查不到」时，这一行就能区分三种成因：
        开关没开 / 拉取失败（条数 0）/ 拉到了但被大量跳过（skipped 偏高）。
        """
        if not self.remote_hash_on():
            reason = (
                "未配置 Qdrant（vector_search.qdrant_url）"
                if not getattr(self.vector, "qdrant_ready", False)
                else "已关闭"
            )
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
        async for result in self.controllers.admin.rescan(event):
            yield result

    @filter.command("溯源删除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def delete_entry(self, event: AstrMessageEvent, entry_id: int):
        """按编号删除图库条目（管理员）"""
        async for result in self.controllers.admin.delete_entry(event, entry_id):
            yield result

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
        images_root = os.path.abspath(self.storage.local_dir)
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
                await self.storage.active.delete(stored.url)
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
        async for result in self.controllers.trace.trace_help(event):
            yield result

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
            "· /签到 —— 每日签到领取一张带边框的卡片（每天首次计入天数，同日重复只换图不涨天数，可 /打卡）；"
            "checkin.mode=weibo 时改为“微博文案 + 对应图库图片”，卡片显示博主与发博日期；"
            "checkin.show_quote=false 时只发美图 + 签到信息，checkin.show_sender 控制是否带签到者昵称\n"
            "· /签到文案导入 —— 管理员：把微博文案 CSV/MD 作为附件发送，导入插件数据目录\n"
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
        async for result in self.controllers.random.random_image(event):
            yield result

    @filter.command("随机视频")
    async def random_video(self, event: AstrMessageEvent):
        """从图床随机取一个视频并回传（可带目录，如 /随机视频 风景）"""
        async for result in self.controllers.random.random_video(event):
            yield result

    @filter.command("签到", alias={"打卡"})
    async def checkin(self, event: AstrMessageEvent):
        """每日签到，领取一张带边框与随机语句的随机缩略图"""
        async for result in self.controllers.checkin.checkin(event):
            yield result

    @staticmethod
    def _first_file_segment(event: AstrMessageEvent):
        """取本条消息或引用消息里的第一个文件段；旧版本没有 File 时返回 None。"""
        file_type = getattr(Comp, "File", None)
        if not isinstance(file_type, type):
            return None
        reply_type = getattr(Comp, "Reply", None)
        for seg in getattr(event.message_obj, "message", None) or []:
            if isinstance(seg, file_type):
                return seg
            if reply_type is not None and isinstance(seg, reply_type):
                for sub in getattr(seg, "chain", None) or []:
                    if isinstance(sub, file_type):
                        return sub
        return None

    @filter.command("签到文案导入", alias={"签到文案上传"})
    async def checkin_import(self, event: AstrMessageEvent):
        """管理员：导入微博文案 CSV/MD 到插件数据目录。"""
        async for result in self.controllers.checkin.checkin_import(event):
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

    async def _reverse_original(self, fetch, *, action: str, level: str='warning') -> tuple[str, str]:
        # R1：流程主体在 services/original.py，main 只留同名薄委托。
        return await self.services.original._reverse_original(fetch, action=action, level=level)

    async def _original_from_thumbnail(self, thumb_ref: str) -> tuple[str, str]:
        # R1：流程主体在 services/original.py，main 只留同名薄委托。
        return await self.services.original._original_from_thumbnail(thumb_ref)

    async def _original_from_thumb_name(self, name: str) -> tuple[str, str]:
        # R1：流程主体在 services/original.py，main 只留同名薄委托。
        return await self.services.original._original_from_thumb_name(name)

    async def _lookup_imgbed_file(self, name: str, *, quoted: str='') -> tuple[str, str, str]:
        # R1：流程主体在 services/original.py，main 只留同名薄委托。
        return await self.services.original._lookup_imgbed_file(name, quoted=quoted)

    async def _file_name_matches(self, name: str) -> int | None:
        # R1：流程主体在 services/original.py，main 只留同名薄委托。
        return await self.services.original._file_name_matches(name)

    async def _original_by_name(self, name: str) -> tuple[str, str]:
        # R1：流程主体在 services/original.py，main 只留同名薄委托。
        return await self.services.original._original_by_name(name)

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
        async for result in self.controllers.original.original_repair(event):
            yield result

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
        async for result in self.controllers.original.original_image(event):
            yield result

    async def _random_media_delivery(self, event: AstrMessageEvent, directory, content_type: str, settings: dict):
        # R1：流程主体在 services/random.py，main 只留同名薄委托。
        async for result in self.services.random._random_media_delivery(event, directory, content_type, settings):
            yield result

    async def _random_image_candidate(self, directory, settings: dict, *, thumbnail: bool | None=None) -> tuple[str, str, str]:
        # R1：流程主体在 services/random.py，main 只留同名薄委托。
        return await self.services.random._random_image_candidate(directory, settings, thumbnail=thumbnail)

    async def _random_image_from_media(self, media_url: str, settings: dict, *, thumbnail: bool | None=None) -> tuple[str, str, str]:
        # R1：流程主体在 services/random.py，main 只留同名薄委托。
        return await self.services.random._random_image_from_media(media_url, settings, thumbnail=thumbnail)

    # ------------------------------------------------------------------
    # 指令：每日签到（随机图 + 边框语句卡片，取图/呈现各有一档开关）
    # ------------------------------------------------------------------

    @staticmethod
    def _write_bytes(path: str, data: bytes) -> None:
        """写临时文件；同步 IO，调用方走 asyncio.to_thread。"""
        with open(path, "wb") as handle:
            handle.write(data)

    async def _checkin_card_path(
        self,
        image_url: str,
        quote: str,
        footer: str,
        settings: dict,
        *,
        quote_max_lines: int = 3,
    ) -> str | None:
        """下载图片 → 合成卡片 → 落临时文件，返回路径；任一步失败返回 None。

        合成放进 to_thread：Pillow 解码/绘制/编码是 CPU 活，放事件循环里会
        阻塞其它消息处理；实测 720px 卡片整段约 10ms、1440px 约 25ms。
        """
        try:
            if os.path.isfile(image_url):
                data = await asyncio.to_thread(read_bytes, image_url)
            else:
                data = await self._download_bytes(image_url)
        except Exception as exc:
            logger.warning(f"签到卡片下载图片失败，改用轻量模式: {exc}")
            return None
        image_bytes = await asyncio.to_thread(
            compose_poster,
            data,
            quote=quote,
            footer=footer,
            font_path=settings["font_path"],
            border_px=settings["border_px"],
            quote_max_lines=quote_max_lines,
            show_quote=bool(settings.get("show_quote", True)),
        )
        if not image_bytes:
            return None
        path = os.path.join(self.tmp_dir, f"c_{secrets.token_hex(8)}.jpg")
        try:
            await asyncio.to_thread(self._write_bytes, path, image_bytes)
        except OSError as exc:
            logger.warning(f"签到卡片写入临时文件失败: {exc}")
            return None
        return path

    async def _checkin_card_source(
        self, send_url: str, original_url: str, settings: dict
    ) -> str:
        """卡片合成专用图源：遵循 checkin.image_mode，与「回传的那张」解耦。

        ``checkin.image_mode`` 决定合成用哪张：
        - ``original``：直接用原图。合成端会先做 JPEG 的 DCT 缩放再降到最长边
          1440px，所以原图不会撑爆内存，只是多花下载流量；
        - ``thumbnail``（默认）：优先缩略图替身。卡片成品只有 ≤1440px，用原图
          合成没有收益——2026-11-02 线上实录：一张 8736×11648 的手机长图被拿来
          合成，既白花几十 MB 流量与上亿像素解码，又被 compose_poster 的防炸
          上限挡回去，整张卡片降级成没有边框的轻量模式。

        注意这里**不看** ``random_media.send_thumbnail``：那个开关管 /随机图、
        /随机视频 发哪张，与签到各管各的，只是共用同一段缩略图反查链路。

        settings 是**签到组**的配置（只取 image_mode）；缩略图替身回来的是公网
        直链，必须再按 random_media 的内网配置换源，所以内部另取
        ``_random_settings()``，不能拿签到配置去算 lan_base_url。
        """
        source = original_url or send_url
        if settings.get("image_mode") == "original":
            return source
        if is_thumbnail_url(send_url):
            # 上游已经给的就是缩略图直链（图床随机目录指到了 thumbnails/）
            return send_url
        thumb = await self._random_send_url(
            source, self._random_settings(), thumbnail=True
        )
        if thumb and thumb != source:
            logger.debug(f"签到卡片改用缩略图合成: {source} -> {thumb}")
        return thumb or source

    # ------------------------------------------------------------------
    # 微博签到：离线映射表（签到只读本地 JSON，不请求微博、不算 pHash）
    # ------------------------------------------------------------------

    def _weibo_map_path(self) -> str:
        return os.path.join(self.weibo_dir, "weibo_post_map.json")

    def _weibo_signature_sync(self) -> tuple:
        """映射表 + 源文件 mtime 组合；任一方变化即触发重新加载。"""
        try:
            map_mtime = os.path.getmtime(self._weibo_map_path())
        except OSError:
            map_mtime = 0.0
        source_mtime = 0.0
        try:
            for name in os.listdir(self.weibo_dir):
                if not name.lower().endswith((".csv", ".md", ".markdown")):
                    continue
                try:
                    source_mtime = max(
                        source_mtime,
                        os.path.getmtime(os.path.join(self.weibo_dir, name)),
                    )
                except OSError:
                    continue
        except OSError:
            pass
        return (round(map_mtime, 3), round(source_mtime, 3))

    def _load_weibo_pool_sync(self) -> tuple[list[WeiboPost], dict]:
        """读映射表；映射缺失/过期时才解析 CSV/MD，避免签到热路径重复解析。

        映射表优先（带图），源文件只用来补新帖子：映射过期时把源文件里有、
        映射里没有的帖子追加进来，这些帖子的 image 为 None，签到走回退。
        """
        map_path = self._weibo_map_path()
        posts, generated_at, map_meta = load_post_map(map_path)
        stale = map_is_stale(map_path, self.weibo_dir)
        source_posts: list[WeiboPost] = []
        source_files: list[str] = []
        errors: list[str] = []
        if not posts or stale:
            source_posts, source_files, errors = load_posts_from_dir(self.weibo_dir)
        merged = posts
        if not posts:
            merged = source_posts
        elif stale and source_posts:
            by_key = {(post.account.casefold(), post.published_at): post for post in posts}
            merged = [
                by_key.get((post.account.casefold(), post.published_at), post)
                for post in source_posts
            ]
            # 源文件被删掉时仍保留映射里的旧条目，避免已有配图丢失
            source_keys = {
                (post.account.casefold(), post.published_at) for post in source_posts
            }
            merged.extend(
                post
                for post in posts
                if (post.account.casefold(), post.published_at) not in source_keys
            )
        meta = {
            "count": len(merged),
            "matched_count": sum(1 for post in merged if post.image is not None),
            "map_count": len(posts),
            "map_generated_at": generated_at,
            "map_stale": stale,
            "source_files": [os.path.basename(path) for path in source_files],
            "errors": errors,
            "map_meta": map_meta,
        }
        return merged, meta

    async def _weibo_posts(self) -> tuple[list[WeiboPost], dict]:
        """签到热路径：mtime 未变时直接返回内存池，不解析任何文件。"""
        signature = await asyncio.to_thread(self._weibo_signature_sync)
        if self._weibo_posts_cache is not None and signature == self._weibo_signature:
            return self._weibo_posts_cache, self._weibo_meta_cache
        posts, meta = await asyncio.to_thread(self._load_weibo_pool_sync)
        self._weibo_posts_cache = posts
        self._weibo_meta_cache = meta
        self._weibo_signature = signature
        if posts:
            self._weibo_empty_warned = False
        elif not self._weibo_empty_warned:
            logger.warning(
                "微博签到模式已开启，但 %s 下没有文案文件或映射表；/签到 将回退随机语句。",
                self.weibo_dir,
            )
            self._weibo_empty_warned = True
        if meta.get("errors"):
            logger.warning("微博文案解析失败: %s", "；".join(meta["errors"]))
        return posts, meta

    async def _weibo_status_line(self) -> str:
        settings = self._checkin_settings()
        # 两个维度各自独立，状态行里都报出来，省得用户改完不知道生效在哪一层：
        # render_mode=合不合成，image_mode=用缩略图还是原图。文字/昵称两个
        # 开关同样常被改错（以为关掉文字会把签到信息也关掉），一并报出。
        shape = (
            f"{'合成卡片' if settings['render_mode'] == 'card' else '图文直发'}"
            f"，取图：{'原图' if settings['image_mode'] == 'original' else '缩略图'}"
            f"，文字：{'显示' if settings['show_quote'] else '不显示'}"
            f"，昵称：{'显示' if settings['show_sender'] else '不显示'}"
        )
        if settings["mode"] != "weibo":
            return f"· 签到模式：quote（随机图 + 随机语句；{shape}）"
        posts, meta = await self._weibo_posts()
        if not posts:
            return f"· 签到模式：weibo（未找到文案/映射，/签到 将回退随机语句；{shape}）"
        matched = int(meta.get("matched_count") or 0)
        suffix = "，源文件比映射新" if meta.get("map_stale") else ""
        if not meta.get("map_count"):
            suffix += "，映射表未生成"
        return (
            f"· 签到模式：weibo（文案 {len(posts)} 条，可配对 {matched} 条"
            f"{suffix}；{shape}）"
        )

    def _weibo_thumb_path(self, file_name: str) -> str:
        """本地预缓存缩略图路径；文件名用 sha1，避免中文/特殊字符进路径。"""
        key = hashlib.sha1(str(file_name or "").encode("utf-8")).hexdigest()
        path = os.path.join(self.weibo_thumb_dir, f"{key}.jpg")
        return path if os.path.isfile(path) else ""

    async def _weibo_candidate(self, random_settings: dict, *, image_mode: str = "thumbnail"):
        """取一条微博及其图库图片；返回 (post, 发送直链, 原图直链, 文件名, 本地缩略图)。

        映射表里有图就直接用；没有图时退一步取同账号同日的任意已匹配图片，
        仍没有则返回 None，由调用方回退现有「随机图 + 随机语句」。

        ``本地缩略图`` 由映射脚本的 ``--prefetch-thumbs`` 预生成；只有
        ``image_mode=thumbnail`` 才会返回它（card 模式零网络合成），原图模式
        必须发/用原图，不能被本地缩略图顶替。

        ``image_mode`` 来自 ``checkin.image_mode``，只影响签到：thumbnail 取
        ``thumb_url``，original 取 ``image_url``。
        """
        posts, _meta = await self._weibo_posts()
        if not posts:
            return None
        post = random.choice(posts)
        image = post.image
        if image is None:
            pool = [
                item.image
                for item in posts
                if item.image is not None
                and item.account.casefold() == post.account.casefold()
                and item.date == post.date
            ]
            if pool:
                image = random.choice(pool)
        if image is None:
            return None
        use_thumb = image_mode != "original"
        local_thumb = self._weibo_thumb_path(image.file_name) if use_thumb else ""
        original_url = image.image_url or image.thumb_url
        if original_url:
            original_url = build_original_url(unquote(original_url))
        # 原图模式下优先真原图直链，映射里缺 image_url 时才退 thumb_url
        preferred, fallback = (
            (image.thumb_url, original_url)
            if use_thumb
            else (original_url, image.thumb_url)
        )
        send_url = preferred or fallback
        lan_hosts = self._lan_url_hosts(random_settings)
        lan_base = str((random_settings or {}).get("lan_base_url") or "")
        if lan_hosts:
            send_url = swap_url_origin(send_url, lan_base, lan_hosts)
            original_url = swap_url_origin(original_url, lan_base, lan_hosts)
        base_url = str((random_settings or {}).get("base_url") or "")
        send_url = upgrade_to_https(send_url, base_url)
        original_url = upgrade_to_https(original_url, base_url)
        if not send_url and not local_thumb:
            return None
        return post, send_url, original_url, image.file_name, local_thumb

    async def _deliver_checkin(self, event: AstrMessageEvent, header: str, quote: str, footer: str, send_url: str, original_url: str, block_name: str, settings: dict, *, quote_max_lines: int=3, simple_text: str='', card_source: str=''):
        # R1：流程主体在 services/checkin.py，main 只留同名薄委托。
        async for result in self.services.checkin._deliver_checkin(event, header, quote, footer, send_url, original_url, block_name, settings, quote_max_lines=quote_max_lines, simple_text=simple_text, card_source=card_source):
            yield result

    async def _checkin_flow(self, event: AstrMessageEvent):
        # R1：流程主体在 services/checkin.py，main 只留同名薄委托。
        async for result in self.services.checkin._checkin_flow(event):
            yield result

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

        实现已迁到 delivery.channels.QQOfficialChannel，保留同名薄委托。
        """
        return await QQOfficialChannel(self).run_offbox(
            DeliveryContext(
                event=event,
                request=DeliveryRequest(
                    header=header, blocks=blocks, mark_compressed=mark_compressed
                ),
                plan=None,
            )
        )

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
                # 转码产物属于本次投递：由 _yield_delivery 在发送完成后清理
                new_block["_tmp_owned"] = True
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
                # 否则 PIL 拿到的是缺了文件头的残缺流，解码必败。
                # 上限按完整响应体算：read_limited_bytes 会拿 Content-Length 与
                # limit 比较，若传 limit-len(head) 会把恰好等于上限的文件误拒。
                rest = await read_limited_bytes(resp, limit)
                data = head + rest
                if len(data) > limit:
                    logger.warning(f"qq_official 图片超过体检上限，按原样交付: {len(data)} 字节")
                    return None
        except Exception as exc:
            logger.warning(f"qq_official 图片格式体检失败，按原样交付: {exc}")
            return None
        path = await asyncio.to_thread(self._write_transcoded_jpeg, data)
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
                if image.width * image.height > QQ_TRANSCODE_MAX_PIXELS:
                    logger.warning(
                        f"qq_official 待转码图片像素过大，跳过转码: {image.size}"
                    )
                    return None
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
        """推送通道（QQ 官方）配置归一化，实现见 config.loader.qq_upload_config。"""
        return qq_upload_config(self.config)

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

        判定不能只看 IP 段：曾遇到把云厂商分配的公网段 IP 当作内网直连地址的
        部署，句法检查会把它当公网地址放行。因此以配置为准：

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

        固定 url / url-scaled 通道是例外：某张图没有可交给腾讯的公网直链
        （只剩本地转码 path，或图床只有内网源）时，该图降级为分片上传——
        URL 上传没有入参，照原通道发出去必然失败，白记一轮 WARN。

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
        degraded = 0
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
                # 钉死直链通道，但这张图没有可交给腾讯的公网直链：只剩本地
                # 转码 path（格式体检把假格式换成 JPEG 时会丢掉 url），或图床
                # 只有内网源（base_url 未配/本身是内网）。URL 上传没有入参，
                # 发出去必然失败——降级到分片，图源本机就有，不依赖图床公网可达
                degraded += 1
                channel = "bytes"
            plan.append((block, channel, public_url))

        if degraded:
            logger.info(
                f"qq_official 直链通道没有可用的公网直链（本地转码图或内网图床），"
                f"本批 {degraded} 张改走分片上传"
            )

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
                await self._qq_notify(
                    event, target, f"⚠️ 图片发送失败：{sanitize_error_text(detail)}"
                )
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

        实现已迁到 delivery.channels.QQOfficialChannel，保留同名薄委托。
        """
        await QQOfficialChannel(self).post_media(event, target, content, media)

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

    async def _random_send_url(
        self, media_url: str, settings: dict, *, thumbnail: bool | None = None
    ) -> str:
        """把随机到的原图换成它的缩略图直链；取不到替身就原样返回原图。

        缩略图与原图落在同一个 Qdrant 点上（点 id = UUID5(原图 id)），所以只要
        随机到的是内容目录里的原图，就能按 id 查出它的 thumb_url——不需要映射表，
        也不能靠文件名（图床给缩略图改过名）。整条链路都失败时静默退回原图：
        替身只是提速手段，绝不能因为它缺席而让 /随机图 发不出图。

        thumbnail 是「这次要发哪一张」的显式口径：
        - None（默认）= 按 random_media.send_thumbnail，/随机图、/随机视频 走这条；
        - True/False = 调用方自带口径，/签到 用 checkin.image_mode 传进来。
        两条链路共用同一段反查，但判定来源各自独立，互不覆盖。
        """
        if thumbnail is None:
            thumbnail = bool(settings.get("send_thumbnail", True))
        if not thumbnail:
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
        async for result in self.controllers.random.send_random_media(
            event, directory, content_type
        ):
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
        的事件循环，asyncio.create_task 会直接抛错。缓存缺失或超过
        remote_hash_ttl 时才刷新；缓存新鲜则跳过，避免每次重启都翻一遍全量
        Qdrant。
        """
        # 提前注册内网直连主机白名单：/原图 可能先于任何随机图命令运行，
        # 而它要取的直链可能是历史里记下的内网地址
        self._random_settings()
        self._maybe_start_remote_hash_refresh()

    async def terminate(self):
        errors: list[str] = []
        # 本实例的内网直连白名单随插件卸载清空（模块级全局已不再被本实例使用）
        self._allowed_private_hosts.clear()
        await self._stop_remote_hash_refresh(errors)
        if self._http is not None:
            try:
                await self._http.close()
            except Exception as e:
                errors.append(f"HTTP 会话: {e}")
        try:
            await self.storage.close()
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
        if self.checkin_store is not None:
            try:
                self.checkin_store.close()
            except Exception as e:
                errors.append(f"签到数据库: {e}")
        if errors:
            logger.error("图片溯源插件卸载时部分资源清理失败: " + "；".join(errors))
        logger.info("图片溯源插件已卸载。")

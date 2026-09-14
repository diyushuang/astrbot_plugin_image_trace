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
import os
import secrets
import time
from collections.abc import AsyncGenerator
from urllib.parse import urlparse

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from .common import (
        AI_VERIFY_MAX_CANDIDATES,
        DOWNLOAD_CHUNK_SIZE,
        DOWNLOAD_TIMEOUT,
        RESCAN_BATCH_SIZE,
        RESCAN_PROGRESS_EVERY,
        TMP_MAX_AGE_SECONDS,
        as_float,
        as_int,
        is_blank,
        is_qq_image_bed_host,
        truthy,
    )
    from .features import ImageFeatures, compute_features, image_file_ok, phash_hex_len
    from .http_client import GuardedHttpClient
    from .image_bed import MODE_CFB, ImageBedClient
    from .image_delivery import (
        build_scaled_url,
        deduplicate_vector_hits,
        delivery_settings,
        is_napcat_parseable_url,
        prepare_image_bytes,
        sniff_image_format,
    )
    from .library import ImageLibrary
    from .vector_search import VectorEngine, VectorEngineError
except ImportError:  # 兼容插件以独立模块方式加载
    from common import (  # type: ignore[no-redef]
        AI_VERIFY_MAX_CANDIDATES,
        DOWNLOAD_CHUNK_SIZE,
        DOWNLOAD_TIMEOUT,
        RESCAN_BATCH_SIZE,
        RESCAN_PROGRESS_EVERY,
        TMP_MAX_AGE_SECONDS,
        as_float,
        as_int,
        is_blank,
        is_qq_image_bed_host,
        truthy,
    )
    from features import (  # type: ignore[no-redef]
        ImageFeatures,
        compute_features,
        image_file_ok,
        phash_hex_len,
    )
    from http_client import GuardedHttpClient  # type: ignore[no-redef]
    from image_bed import MODE_CFB, ImageBedClient  # type: ignore[no-redef]
    from image_delivery import (  # type: ignore[no-redef]
        build_scaled_url,
        deduplicate_vector_hits,
        delivery_settings,
        is_napcat_parseable_url,
        prepare_image_bytes,
        sniff_image_format,
    )
    from library import ImageLibrary  # type: ignore[no-redef]
    from vector_search import VectorEngine, VectorEngineError  # type: ignore[no-redef]

PLUGIN_NAME = "astrbot_plugin_image_trace"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".jfif"}

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

        # pHash 十六进制长度 = phash_hex_len(hash_size)（packbits 补齐到字节
        # 边界）；hash_size 必须为 4 的倍数，否则长度公式与旧数据对不上，
        # 全库记录都会被判为不匹配、索引静默清空
        hash_size = as_int(self.config.get("hash_size"), 16)
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
        self._cleanup_tmp()
        engine = self._engine_choice()
        vector_note = "（向量引擎已启用）" if self.vector.enabled and engine != "hash" else ""
        bucket_note = (
            f"（储存桶 {self._bed_mode()} 优先）" if self._bed_mode() in BUCKET_MODES else ""
        )
        logger.info(
            f"图片溯源插件已加载，当前图库共 {self.library.count()} 条，"
            f"引擎={engine} {vector_note}{bucket_note}"
        )

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    @staticmethod
    def _is_blank(value) -> bool:
        """未配置：None 或空白字符串（0 是合法取值，不能按假值处理）。"""
        return is_blank(value)

    def _dict_cfg(self, key: str) -> dict:
        """取对象型配置；手工改坏配置（非 dict）时退化为空 dict 而不是抛错。"""
        value = self.config.get(key)
        return value if isinstance(value, dict) else {}

    def _int_cfg(self, key: str, default: int) -> int:
        return as_int(self.config.get(key), default)

    def _float_cfg(self, key: str, default: float) -> float:
        return as_float(self.config.get(key), default)

    def _bool_cfg(self, key: str, default: bool) -> bool:
        """布尔配置：未配置（缺键/空串/null）时取默认值，与数值配置口径一致。"""
        value = self.config.get(key)
        if self._is_blank(value):
            return default
        return truthy(value)

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
            f"storage_bucket.{k}" for k in BUCKET_REQUIRED[mode] if self._is_blank(raw.get(k))
        ]
        if (
            mode == "oracle_oci"
            and not truthy(raw.get("oci_public_bucket"))
            and self._is_blank(raw.get("oci_public_base_url"))
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
        choice = str(self.config.get("search_engine") or "auto").strip().lower()
        return choice if choice in ("hash", "vector", "auto") else "auto"

    def _vector_index_on_register(self) -> bool:
        value = self._dict_cfg("vector_search").get("vector_index_on_register")
        if self._is_blank(value):
            return True
        return truthy(value)

    def _scan_dirs(self) -> list:
        raw = self.config.get("scan_dirs")
        if isinstance(raw, str):  # 手工配置成单个路径字符串时按一个目录处理
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [d.strip() for d in raw if isinstance(d, str) and d.strip()]

    # ------------------------------------------------------------------
    # 图片提取与解析
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_images(event: AstrMessageEvent) -> list:
        """提取消息链与被引用消息中的图片段（按链接/文件名去重）。"""
        found: list = []
        seen: set = set()

        def push(seg) -> None:
            key = getattr(seg, "url", None) or getattr(seg, "file", None) or id(seg)
            if key in seen:
                return
            seen.add(key)
            found.append(seg)

        for seg in event.message_obj.message or []:
            if isinstance(seg, Comp.Image):
                push(seg)
            elif isinstance(seg, Comp.Reply):
                for sub in getattr(seg, "chain", None) or []:
                    if isinstance(sub, Comp.Image):
                        push(sub)
        return found

    async def _resolve_local_file(self, seg) -> tuple[str, bool]:
        """把图片段解析为本地文件。返回 (路径, 是否为需要清理的临时文件)。

        优先走 AstrBot 内置媒体解析 convert_to_file_path()；其产物或兜底
        下载的内容都会先经 image_file_ok 校验——QQ 图床 URL 带 rkey 签名
        会过期、NTQQ/gchat 图床有防盗链，失败时常返回几十~几百字节的错误
        体而非图片，必须拦在引擎之前。两条路都无效时返回空路径，由调用方
        给出统一的「未能获取图片内容」提示。
        """
        try:
            path = await seg.convert_to_file_path()
            if path and os.path.isfile(path):
                if await asyncio.to_thread(image_file_ok, path):
                    return path, False
                logger.warning(f"内置解析产物不是有效图片（{self._peek_sniff(path)}），改走兜底下载")
        except Exception as e:
            logger.debug(f"convert_to_file_path 失败，尝试兜底下载: {e}")

        url = getattr(seg, "url", None)
        if url and str(url).startswith(("http://", "https://")):
            try:
                path = await self._download(str(url))
            except Exception as e:
                logger.warning(f"图片兜底下载失败: {e}")
                return "", False
            if await asyncio.to_thread(image_file_ok, path):
                return path, True
            logger.warning(
                f"兜底下载内容不是有效图片（{self._peek_sniff(path)}），链接可能已过期或被防盗链拦截"
            )
            self._remove_quiet(path)
        return "", False

    @staticmethod
    def _peek_sniff(path: str) -> str:
        """日志用的坏文件摘要：字节数 + 头部可打印片段，便于定位是谁返回的。"""
        try:
            with open(path, "rb") as f:
                head = f.read(48)
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
            return f"{os.path.getsize(path)} 字节, 头部 {text!r}"
        except OSError:
            return "无法读取"

    async def _download(self, url: str) -> str:
        client = await self._get_http()
        size_limit = max(1, self._int_cfg("max_download_mb", 20)) * 1024 * 1024
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
        # 临时文件名完全不受外部输入影响（固定扩展名），
        # 图片解码由 Pillow 按内容判断，与扩展名无关
        path = os.path.join(self.tmp_dir, f"q_{secrets.token_hex(8)}.jpg")
        try:
            async with await client.request(
                "GET",
                url,
                # prepare 契约为三参（url, method, cross_origin），签名不符
                # 会在请求发出前就 TypeError
                prepare=self._image_download_headers,
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                declared = resp.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > size_limit:
                    raise ValueError("图片超过大小上限")
                received = 0
                with open(path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                        received += len(chunk)
                        if received > size_limit:
                            raise ValueError("图片超过大小上限")
                        f.write(chunk)
        except Exception:
            self._remove_quiet(path)
            raise
        return path

    @staticmethod
    def _image_download_headers(u: str, _m: str, _c: bool) -> dict:
        """图片兜底下载的请求头。

        NTQQ（multimedia.nt.qq.com.cn）与旧版 gchat.qpic.cn 图床均启用了
        防盗链：缺 Referer 时会返回 403 或只有几十~几百字节的错误体。
        Referer 不是凭据，跨域重定向是否剥离交给 url_guard；重定向离开
        QQ 图床域名后因 host 不匹配自然不再携带。
        """
        if is_qq_image_bed_host(urlparse(u).hostname or ""):
            return {"headers": {"Referer": "https://gchat.qpic.cn/"}}
        return {}

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

    def _delivery_config(self) -> tuple[str, int, int]:
        return delivery_settings(self.config.get("image_delivery"))

    def _can_send_via_onebot(self, event: AstrMessageEvent) -> bool:
        try:
            if event.get_platform_name() != "aiocqhttp":
                return False
        except Exception:
            return False
        return getattr(event, "bot", None) is not None

    @staticmethod
    def _read_file_bytes(path: str) -> bytes:
        with open(path, "rb") as file:
            return file.read()

    @staticmethod
    def _onebot_text(header: str, blocks: list[dict]) -> str:
        parts = [header]
        for block in blocks:
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _vector_caption(block: dict, compressed: bool) -> str:
        fields = []
        if compressed:
            fields.append("已压缩 /原图")
        file_name = str(block.get("file_name") or "").strip()
        if file_name:
            fields.append(file_name)
        fields.extend(str(field) for field in block.get("fields", []) if field)
        prefix = str(block.get("prefix") or "").strip()
        caption = " · ".join(fields)
        return f"{prefix} {caption}".strip() if prefix else caption

    async def _send_via_onebot(
        self, event: AstrMessageEvent, header: str, blocks: list[dict], urls: list[str]
    ) -> bool:
        message = []
        sender_id = event.get_sender_id()
        if sender_id:
            message.append({"type": "at", "data": {"qq": str(sender_id)}})
        message.append({"type": "text", "data": {"text": self._onebot_text(header, blocks) + "\n"}})
        message.extend({"type": "image", "data": {"file": url}} for url in urls)

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

        try:
            await event.bot.call_action(action, **params)
        except Exception as exc:
            logger.warning(f"OneBot URL 直传图片失败，准备回退: {exc}")
            return False
        logger.info(f"OneBot URL 直传图片成功: {action}")
        return True

    async def _load_delivery_bytes(self, block: dict) -> bytes | None:
        url = str(block.get("url") or "")
        path = str(block.get("path") or "")
        temp_path = ""
        try:
            if url:
                temp_path = await self._download(url)
                path = temp_path
            if not path or not await asyncio.to_thread(image_file_ok, path):
                return None
            return await asyncio.to_thread(self._read_file_bytes, path)
        except Exception as exc:
            logger.warning(f"读取回传图片失败: {exc}")
            return None
        finally:
            if temp_path:
                self._remove_quiet(temp_path)

    async def _prepare_delivery_blocks(
        self, blocks: list[dict], *, compress: bool
    ) -> list[tuple[bytes, bool]] | None:
        prepared = []
        mode, max_side, quality = self._delivery_config()
        for block in blocks:
            if not block.get("url") and not block.get("path"):
                continue
            data = await self._load_delivery_bytes(block)
            if data is None:
                return None
            if not compress or mode == "original-url":
                prepared.append((data, False))
                continue
            result = await asyncio.to_thread(prepare_image_bytes, data, max_side, quality)
            if result is None:
                prepared.append((data, False))
                logger.warning(
                    f"图片本地压缩失败，回退原字节: 格式={sniff_image_format(data) or '未知'}, "
                    f"大小={len(data) / 1024:.0f}KB"
                )
            else:
                prepared.append(result)
        return prepared

    async def _yield_delivery(self, event: AstrMessageEvent, header: str, blocks: list[dict]):
        """统一回传入口：URL 直传优先，本地压缩只作为失败回退。"""
        mode, max_side, _ = self._delivery_config()
        image_blocks = [block for block in blocks if block.get("url") or block.get("path")]
        original_urls = [str(block["url"]) for block in image_blocks if block.get("url")]

        if (
            mode in {"scaled-url", "original-url"}
            and self._can_send_via_onebot(event)
            and len(original_urls) == len(image_blocks)
        ):
            if mode == "scaled-url" and all(
                is_napcat_parseable_url(url) for url in original_urls
            ):
                scaled_urls = [build_scaled_url(url, max_side) for url in original_urls]
                if await self._send_via_onebot(event, header, blocks, scaled_urls):
                    event.stop_event()
                    return
                if scaled_urls != original_urls and await self._send_via_onebot(
                    event, header, blocks, original_urls
                ):
                    event.stop_event()
                    return
            elif mode == "original-url":
                if await self._send_via_onebot(event, header, blocks, original_urls):
                    event.stop_event()
                    return

        should_compress = mode == "local-compress" or (
            mode == "scaled-url"
            and any(block.get("path") for block in image_blocks)
        )
        onebot_failed = self._can_send_via_onebot(event) and mode in {"scaled-url", "original-url"}
        if should_compress or (onebot_failed and len(original_urls) == len(image_blocks)):
            prepared = await self._prepare_delivery_blocks(
                image_blocks, compress=mode != "original-url"
            )
            if prepared is not None:
                fallback_header = header
                if any(compressed for _, compressed in prepared):
                    fallback_header += "（已压缩）"
                chain = [Comp.At(qq=event.get_sender_id()), Comp.Plain(" " + fallback_header)]
                image_index = 0
                for block in blocks:
                    if block.get("text"):
                        chain.append(Comp.Plain("\n" + str(block["text"])))
                    if block.get("url") or block.get("path"):
                        data, _ = prepared[image_index]
                        chain.append(Comp.Image.fromBytes(data))
                        image_index += 1
                yield event.chain_result(chain)
                return
            logger.warning("本地图片回退失败，改用标准消息链 URL 发送")

        chain = [Comp.At(qq=event.get_sender_id()), Comp.Plain(" " + header)]
        for block in blocks:
            if block.get("text"):
                chain.append(Comp.Plain("\n" + str(block["text"])))
            url = str(block.get("url") or "")
            path = str(block.get("path") or "")
            if url:
                target_url = build_scaled_url(url, max_side) if mode == "scaled-url" else url
                chain.append(Comp.Image.fromURL(target_url))
            elif path:
                chain.append(Comp.Image.fromFileSystem(path))
        yield event.chain_result(chain)

    async def _yield_separated_delivery(
        self, event: AstrMessageEvent, header: str, blocks: list[dict]
    ):
        mode, max_side, _ = self._delivery_config()
        onebot = self._can_send_via_onebot(event)
        prompt_sent = False
        used_onebot = False
        if onebot and await self._send_via_onebot(event, header, [], []):
            prompt_sent = True
            used_onebot = True
        if not prompt_sent:
            yield event.plain_result(header)

        for block in blocks:
            url = str(block.get("url") or "")
            path = str(block.get("path") or "")
            direct_url = ""
            if onebot and url and mode in {"scaled-url", "original-url"}:
                if mode == "scaled-url" and is_napcat_parseable_url(url):
                    direct_url = build_scaled_url(url, max_side)
                elif mode == "original-url":
                    direct_url = url

            if direct_url:
                block["text"] = self._vector_caption(block, direct_url != url)
                if await self._send_via_onebot(event, block["text"], [block], [direct_url]):
                    used_onebot = True
                    continue
                if direct_url != url:
                    block["text"] = self._vector_caption(block, False)
                    if await self._send_via_onebot(event, block["text"], [block], [url]):
                        used_onebot = True
                        continue

            if not url and not path:
                caption = self._vector_caption(block, False)
                yield event.plain_result(caption)
                continue

            needs_local_bytes = mode == "local-compress" or (
                mode == "scaled-url" and path
            ) or (onebot and url and mode in {"scaled-url", "original-url"})
            if needs_local_bytes:
                prepared = await self._prepare_delivery_blocks([block], compress=True)
                if prepared is not None:
                    data, compressed = prepared[0]
                    caption = self._vector_caption(block, compressed)
                    chain = [
                        Comp.At(qq=event.get_sender_id()),
                        Comp.Plain(" " + caption),
                        Comp.Image.fromBytes(data),
                    ]
                    yield event.chain_result(chain)
                    continue
                logger.warning("本地图片回退失败，改用标准消息链 URL 发送")

            target_url = build_scaled_url(url, max_side) if url and mode == "scaled-url" else url
            caption = self._vector_caption(block, bool(url and target_url != url))
            chain = [Comp.At(qq=event.get_sender_id()), Comp.Plain(" " + caption)]
            if url:
                chain.append(Comp.Image.fromURL(target_url))
            else:
                chain.append(Comp.Image.fromFileSystem(path))
            yield event.chain_result(chain)
        if used_onebot:
            event.stop_event()

    # ------------------------------------------------------------------
    # 指令：溯源
    # ------------------------------------------------------------------

    @filter.command("溯源", alias={"找原图"})
    async def trace(self, event: AstrMessageEvent):
        """发送或引用一张图片，按当前引擎（hash/vector/auto）找出相似的原图并回传"""
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
        if self.library.count() == 0:
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

    @staticmethod
    def _miss_message(fail_line: str, candidates: list, empty_note: str = "") -> str:
        """未命中回复的统一拼装：阈值提示 + 可选的“最接近候选”或空库说明。"""
        if candidates:
            return "\n".join([fail_line, "最接近的候选："] + candidates)
        if empty_note:
            return f"{fail_line}\n{empty_note}"
        return fail_line

    async def _trace_hash(self, event: AstrMessageEvent, seg) -> AsyncGenerator:
        threshold = self.hash_threshold
        top_n = self.top_n
        local_path = ""
        is_tmp = False
        try:
            local_path, is_tmp = await self._resolve_local_file(seg)
            if not local_path:
                yield event.plain_result("⚠️ 未能获取图片内容，图片链接可能已过期。")
                return

            feats: ImageFeatures = await asyncio.to_thread(
                compute_features, local_path, self.hash_size
            )
            # 至少取回 5 条候选：即便 top_n 配置为 0/较小值，AI 复核也需要
            # 足够的候选池才能剔除误报
            candidates = await asyncio.to_thread(self.library.search, feats.phash, max(5, top_n))
            hits = [m for m in candidates if m.similarity >= threshold]
            if hits and self._bool_cfg("ai_verify", False):
                hits = await self._ai_verify(local_path, hits)

            if hits:
                best = hits[0]
                lines = [f"✅ 溯源命中（相似度 {best.similarity * 100:.1f}%）#{best.id}"]
                if best.note:
                    lines.append(f"备注：{best.note}")
                if best.width and best.height:
                    lines.append(f"尺寸：{best.width}x{best.height}")
                if best.created_at:
                    lines.append(f"入库时间：{best.created_at}")
                block = {"text": " · ".join(lines[1:])}
                if best.image_url:
                    block["url"] = best.image_url
                elif best.file_path and os.path.isfile(best.file_path):
                    block["path"] = best.file_path
                if "url" in block or "path" in block:
                    async for result in self._yield_delivery(event, lines[0], [block]):
                        yield result
                else:
                    yield event.plain_result(
                        f"✅ 溯源命中（相似度 {best.similarity * 100:.1f}%）#{best.id}，"
                        f"但原图文件已不存在：{best.file_path or best.image_url or '未知路径'}"
                    )
                return

            candidate_lines = (
                [
                    f"· #{m.id} 相似度 {m.similarity * 100:.1f}%{f'（{m.note}）' if m.note else ''}"
                    for m in candidates[:top_n]
                ]
                if top_n > 0
                else []
            )
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
            if is_tmp:
                self._remove_quiet(local_path)

    # ------------------------------------------------------------------
    # 向量引擎
    # ------------------------------------------------------------------

    async def _vector_search_one(self, event, seg):
        """单图向量检索。返回 (kind, item)：hit / miss / nofile / error。

        hit/miss/nofile 时 item 为可直接 yield 的结果对象；error 时 item 为错误说明字符串。
        """
        top_n = self.top_n
        local_path = ""
        is_tmp = False
        try:
            local_path, is_tmp = await self._resolve_local_file(seg)
            if not local_path:
                return "nofile", event.plain_result("⚠️ 未能获取图片内容，图片链接可能已过期。")
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
            if is_tmp:
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
        """auto 引擎：向量优先，未命中或引擎不可用时回退哈希。"""
        kind, item = await self._vector_search_one(event, seg)
        if kind == "hit":
            async for result in item:
                yield result
            return
        if kind == "nofile":
            yield item
            return
        hash_empty = self.library.count() == 0
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
        async for it in self._trace_hash(event, seg):
            yield it

    async def _vector_hits_delivery(
        self, event: AstrMessageEvent, hits: list
    ) -> AsyncGenerator:
        """构造向量命中的去重回传请求，逐图独立发送。"""
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
            image_url = str(payload.get("image_url") or "")
            if image_url:
                block["url"] = image_url
            else:
                block["fields"].append("该条目没有可用直链，可能已被删除")
            blocks.append(block)
        async for result in self._yield_separated_delivery(event, header, blocks):
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

        任一环节失败都静默降级为纯哈希结果，不影响正常使用。
        """
        provider = None
        try:
            provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None or not hasattr(provider, "text_chat"):
            logger.info("未找到可用的 LLM 提供商，跳过 AI 复核。")
            return hits

        verified: list = []
        for cand in hits[:AI_VERIFY_MAX_CANDIDATES]:
            cand_path = ""
            if cand.file_path and os.path.isfile(cand.file_path):
                cand_path = cand.file_path
            if not cand_path:
                verified.append(cand)  # 候选无本地文件，无法复核，保留哈希结论
                continue
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
                if self._is_negative_answer(answer):
                    continue
                verified.append(cand)
            except Exception as e:
                logger.warning(f"AI 复核失败，保留候选 #{cand.id}: {e}")
                verified.append(cand)
        # 不能回退为未复核的 hits：候选被全部否决时应当报告未命中
        return verified

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
            local_path, is_tmp = await self._resolve_local_file(seg)
            if not local_path:
                yield event.plain_result("⚠️ 未能获取图片内容，图片链接可能已过期。")
                return
            size_limit = max(1, self._int_cfg("max_download_mb", 20)) * 1024 * 1024
            if os.path.getsize(local_path) > size_limit:
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
                "file_size": os.path.getsize(local_path),
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
                            os.path.getsize(local_path),
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
        lines.append(f"· AI 复核：{'开' if self._bool_cfg('ai_verify', False) else '关'}")
        yield event.plain_result("\n".join(lines))

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
            "· /溯源帮助 —— 显示本说明\n"
            "检索引擎：auto=向量优先（未命中/不可用自动回退哈希）；hash/vector 可在插件配置 search_engine 切换。\n"
            "提示：哈希阈值、向量阈值与模型、图床接口等均可在 WebUI 插件配置中调整。"
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def terminate(self):
        errors: list[str] = []
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

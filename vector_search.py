"""向量检索引擎（图片溯源双引擎之“向量”）的薄门面。

查询图片 -> 配置的多模态向量 AI（OpenAI 兼容 /v1/embeddings）生成向量
-> Qdrant REST 检索最相似的图床图片（相似度 = Qdrant cosine score）。

具体出网与协议细节拆到两个客户端：
- `search.embedding_client.EmbeddingClient`：输入序列化、/v1/embeddings 请求与解析；
- `search.qdrant_client.QdrantClient`：points URL/请求头、检索/翻页/计数、
  payload 反查与 upsert/delete。

本模块只负责读取配置、判定启用状态、保持日志/异常/阈值口径不变，并把公开
方法委托给上述客户端。`VectorEngineError` / `HttpError` / `InvalidEmbeddingJSON`
/ `parse_embedding_response` / `thumb_path_key` / `IMAGE_INPUTS` 继续从这里导出，
既有 `from vector_search import ...` 的调用方无需改动。

与服务器侧图床入库服务使用同一套协议与输入序列化，保证同一向量空间：
- embed_image_input=nemotron-vl:  裸 dataURL 字符串 + input_type（非对称模型，默认；
  NVIDIA llama-nemotron-embed-vl 系只接受这一种）
- embed_image_input=qwen-vl:      content 数组 [{"type":"image","image":<dataURL>}]
  （仅 Qwen3-VL-Embedding 系需要）
- embed_image_input=dataurl:      裸 dataURL 字符串
- embed_image_input=jina-image:   [{"image":<base64 无前缀>}]

出网请求一律经 url_guard.guarded_request 逐跳 SSRF 校验 + IP 钉扎
（qdrant_url / embed_base_url 必须是公网可达的 http/https 地址）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/自测环境
    import logging

    logger = logging.getLogger("vector_search")

from .common import as_float, as_int, read_bytes
from .features import image_mime
from .http_client import HttpClientGetter
from .search.embedding_client import IMAGE_INPUTS, EmbeddingClient
from .search.errors import (
    HttpError,
    InvalidEmbeddingJSON,
    VectorEngineError,
    parse_embedding_response,
)
from .search.qdrant_client import DUPLICATE_SCAN_CAP, QdrantClient, thumb_path_key
from .url_guard import (
    ResponseTooLargeError,
    UrlBlockedError,
    read_limited_text,
)

__all__ = [
    "DUPLICATE_SCAN_CAP",
    "IMAGE_INPUTS",
    "HttpError",
    "InvalidEmbeddingJSON",
    "VectorEngine",
    "VectorEngineError",
    "parse_embedding_response",
    "thumb_path_key",
]


class VectorEngine:
    """Qdrant + 多模态 embedding 的只读检索与登记同步客户端。"""

    def __init__(
        self,
        config,
        http_getter: HttpClientGetter,
    ):
        """http_getter: 异步可调用，返回共享 GuardedHttpClient。"""
        raw = config.get("vector_search")
        if not isinstance(raw, dict):
            raw = {}
        self.config_raw = raw
        self.qdrant_url = self._normalize_base_url(raw.get("qdrant_url"))
        self.qdrant_key = str(raw.get("qdrant_api_key") or "")
        self.collection = str(raw.get("collection_name") or "imgbed_images")
        self.embed_base_url = self._normalize_base_url(raw.get("embed_base_url"))
        self.embed_key = str(raw.get("embed_api_key") or "")
        self.embed_model = str(raw.get("embed_model") or "")
        self.image_input = str(raw.get("embed_image_input") or "nemotron-vl")
        if self.image_input not in IMAGE_INPUTS:
            # 配置错误在加载期就暴露（日志可见），而不是拖到第一次请求才失败
            logger.warning(
                f"embed_image_input 配置无效: {self.image_input!r}，"
                f"可选 {' / '.join(IMAGE_INPUTS)}；已回退为 nemotron-vl。"
            )
            self.image_input = "nemotron-vl"
        # 仅 nemotron-vl 的非对称模型区分 input_type；图片侧只能走 passage，
        # 这里保留配置值供诊断展示，实际请求强制 passage（见 EmbeddingClient）
        self.input_type = str(raw.get("embed_input_type") or "passage")
        if self.input_type not in ("query", "passage"):
            self.input_type = "passage"
        self.threshold = max(0.0, min(1.0, as_float(raw.get("similarity_threshold"), 0.80)))
        self.duplicate_threshold = max(
            0.0,
            min(1.0, as_float(raw.get("duplicate_vector_threshold"), 0.995)),
        )
        self.top_k = max(1, min(50, as_int(raw.get("top_k"), 5)))
        timeout_s = max(5, as_int(raw.get("request_timeout"), 30))
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._http_getter = http_getter
        # embed_api_key 不参与启用判定：网关未开鉴权时留空是合法场景
        self.enabled = bool(self.qdrant_url and self.embed_base_url and self.embed_model)
        # 反查通道（按 thumb_url / file_name 查 payload）只依赖 Qdrant、不碰
        # embed，因此启用判定必须与 enabled 解耦：只配了 Qdrant、缺 embed 时
        # enabled=False，但反查必须仍然可用。2026-10-20 线上排查实录：另一台
        # AstrBot 实例（v4.29.1）配了 Qdrant 却因 enabled 一刀切为 False，
        # 随机图命中缩略图后的反查全部静默失败，warning 里又无线索可查。
        self.qdrant_ready = bool(self.qdrant_url)
        # 两个客户端都通过回调延迟解析门面上的方法：这样测试/运行期替换
        # `_http_getter` / `_request_json` / `embed_file` 仍然即时生效。
        self._embed = EmbeddingClient(
            base_url=self.embed_base_url,
            api_key=self.embed_key,
            model=self.embed_model,
            image_input=self.image_input,
            timeout=self.timeout,
            http_getter=lambda: self._http_getter(),
            logger=logger,
        )
        self._qd = QdrantClient(
            base_url=self.qdrant_url,
            api_key=self.qdrant_key,
            collection=self.collection,
            top_k=self.top_k,
            request_json=self._request_json_proxy,
            embed_file=self._embed_file_for_upsert,
            logger=logger,
        )

    @staticmethod
    def _normalize_base_url(value: object) -> str:
        base_url = str(value or "").strip().rstrip("/")
        if base_url and not base_url.lower().startswith(("http://", "https://")):
            logger.warning("忽略非法向量服务地址（必须以 http/https 开头）")
            return ""
        return base_url

    @staticmethod
    def _debug_disabled(action: str) -> None:
        """反查入口不可用时的统一 DEBUG 说明。

        这些入口长期「静默返回 None」：调用方的 warning 只说「反查不到」，
        根因（Qdrant 未配置）连 DEBUG 日志里都找不到——2026-10-20 跨实例排查
        时只能逐台实测。统一留一行 DEBUG，把「为什么没查」说清楚。
        """
        logger.debug(
            f"向量引擎不可用（Qdrant 未配置），{action}按未命中处理；"
            "补齐 vector_search.qdrant_url 并重启插件后生效"
        )

    # ------------------------------------------------------------------
    # 配置诊断
    # ------------------------------------------------------------------

    def missing_fields(self) -> list:
        """返回未配置的必填项名称列表（供提示文案使用）。

        qdrant_api_key / embed_api_key 不列入：自托管无鉴权的 Qdrant、
        未开鉴权的 embedding 网关都是合法场景（与 __init__ 的 enabled
        判定口径一致）。
        """
        missing = []
        if not self.qdrant_url:
            missing.append("vector_search.qdrant_url")
        if not self.embed_base_url:
            missing.append("vector_search.embed_base_url")
        if not self.embed_model:
            missing.append("vector_search.embed_model")
        raw_qdrant_url = str(self.config_raw.get("qdrant_url") or "").strip()
        raw_embed_url = str(self.config_raw.get("embed_base_url") or "").strip()
        if raw_qdrant_url and not self.qdrant_url:
            missing.append("vector_search.qdrant_url（必须以 http/https 开头）")
        if raw_embed_url and not self.embed_base_url:
            missing.append("vector_search.embed_base_url（必须以 http/https 开头）")
        return missing

    def config_hint(self) -> str:
        return (
            "⚠️ 向量引擎未启用：请在插件配置的 vector_search 一节补齐："
            + "、".join(self.missing_fields() or ["（请检查 search_engine 设置）"])
            + "，然后在 WebUI 重启插件。"
        )

    # ------------------------------------------------------------------
    # 基础请求（一律走 SSRF 校验）
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        headers: dict | None = None,
    ):
        client = await self._http_getter()

        def prepare(_u: str, _m: str, cross_origin: bool = False) -> dict:
            return {
                "json": json_body,
                # 跨域重定向到第三方时不再携带 api-key 等鉴权头
                "headers": {} if cross_origin else (headers or {}),
                "timeout": self.timeout,
            }

        try:
            async with await client.request(method, url, prepare=prepare) as resp:
                text = await read_limited_text(resp)
                status = resp.status
        except VectorEngineError:
            raise
        except UrlBlockedError as e:
            raise VectorEngineError(
                "目标地址被安全策略拒绝",
                detail=f"目标地址被安全策略拒绝: {e}",
            ) from e
        except ResponseTooLargeError as e:
            raise VectorEngineError(f"响应体过大，已中止: {e}") from e
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise VectorEngineError(
                "网络请求失败（详情见日志）",
                detail=f"网络请求失败: {url}: {e}",
            ) from e
        return status, text

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        headers: dict | None = None,
    ) -> Any:
        status, text = await self._request(method, url, json_body=json_body, headers=headers)
        if status != 200:
            raise HttpError(status, text)
        try:
            return json.loads(text)
        except ValueError as e:
            raise VectorEngineError(
                "服务响应不是合法 JSON",
                detail=f"响应不是合法 JSON: {text[:500]}",
            ) from e

    async def _request_json_proxy(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        headers: dict | None = None,
    ) -> Any:
        """把门面上的 `_request_json` 延迟传给 QdrantClient（便于替换/打桩）。"""
        return await self._request_json(method, url, json_body=json_body, headers=headers)

    # ------------------------------------------------------------------
    # 向量化
    # ------------------------------------------------------------------

    async def embed_bytes(self, data: bytes, mime: str) -> list:
        """把图片字节向量化，返回浮点向量列表（详见 EmbeddingClient.embed_bytes）。"""
        return await self._embed.embed_bytes(data, mime)

    async def embed_file(self, path: str, *, mime: str | None = None) -> list:
        """把本地图片向量化；mime 由调用方提供时不再重复探测。

        mime 来自同一张图刚完成的一次解码（compute_features_with_mime 或
        image_mime），传进来即可省掉 embed_file 自己的 `image_mime()` 解码。
        """
        if not mime:
            mime = await asyncio.to_thread(image_mime, path)
        if mime is None:
            raise VectorEngineError("图片内容无效（下载不完整、链接已过期或格式不受支持）")
        data = await asyncio.to_thread(read_bytes, path)
        return await self.embed_bytes(data, mime)

    # ------------------------------------------------------------------
    # Qdrant 检索与状态
    # ------------------------------------------------------------------

    async def search(
        self, vector: list, limit: int | None = None, *, with_vector: bool = False
    ) -> list:
        """Qdrant 相似度检索，返回 [{id, score, payload, vector?}] 列表（未做阈值过滤）。"""
        return await self._qd.search(vector, limit, with_vector=with_vector)

    async def scroll_payloads(
        self,
        *,
        key: str,
        page_size: int = 512,
        max_points: int = 0,
        with_payload_keys: list | None = None,
    ) -> list:
        """遍历全集合收集存在 payload[key] 的点，返回 [{id, payload}]（详见 QdrantClient）。"""
        return await self._qd.scroll_payloads(
            key=key,
            page_size=page_size,
            max_points=max_points,
            with_payload_keys=with_payload_keys,
        )

    async def count(self) -> int:
        """返回集合点数；集合不存在按 0；连接/鉴权失败抛 VectorEngineError。"""
        return await self._qd.count()

    async def thumb_url_for(self, file_id: str) -> str | None:
        """取该原图对应点上的缩略图直链（payload.thumb_url）；没有则为 None。

        本方法**永不抛错**：缩略图只是提速手段，取不到就由调用方照常发原图。
        """
        key = str(file_id or "").strip()
        if not key:
            return None
        if not self.qdrant_ready:
            self._debug_disabled("按点 id 查缩略图替身")
            return None
        return await self._qd.thumb_url_for(key)

    async def original_url_for_thumb(self, thumb_url: str) -> str | None:
        """取该缩略图直链所属点上的原图直链（payload.image_url）；没有则为 None。

        与 thumb_url_for 互为逆操作。本方法**永不抛错**：反查只是纠正手段，
        失败时由调用方走原有的「按名直查」逻辑。
        """
        target = str(thumb_url or "").strip()
        if not target:
            return None
        info = await self.original_info_for_thumb(target)
        return info["url"] if info else None

    async def original_info_for_thumb(self, thumb_url: str) -> dict | None:
        """按缩略图直链取回**整个原图点**的信息：{"url": …, "file_name": …}。

        命中数非 1 一律放弃，永不抛错（匹配细节见 QdrantClient）。
        """
        target = str(thumb_url or "").strip()
        if not target:
            return None
        if not self.qdrant_ready:
            self._debug_disabled("按缩略图直链反查原图信息")
            return None
        return await self._qd.original_info_for_thumb(target)

    async def original_info_for_thumb_name(self, thumb_name: str) -> dict | None:
        """按「缩略图改名后的**文件名**」反查原图；无法唯一确定时返回 None。

        `match.text` 只用于缩小候选，判据是本地 basename 精确比对；
        候选非唯一（同名多目录）一律放弃。本方法**永不抛错**。
        """
        raw = str(thumb_name or "").strip()
        if not raw:
            return None
        if not self.qdrant_ready:
            self._debug_disabled("按缩略图文件名反查原图")
            return None
        return await self._qd.original_info_for_thumb_name(raw, scroll_filter=self._scroll_filter)

    async def original_by_file_name(self, file_name: str) -> dict | None:
        """按**裸文件名**反查原图：{"url": 带目录的权威直链, "file_name": 真名}。

        按值精确匹配 `file_name`；同名多份时先证明「是同一张图」再确定性选一份。
        本方法**永不抛错**，失败一律返回 None（详见 QdrantClient）。
        """
        target = str(file_name or "").strip()
        if not target:
            return None
        if not self.qdrant_ready:
            self._debug_disabled("按文件名反查原图")
            return None
        return await self._qd.original_by_file_name(target)

    async def file_name_matches(self, file_name: str) -> int | None:
        """该文件名在库里的命中数；查询失败返回 None（区别于「确实是 0 条」）。

        只服务于 `/原图` **失败路径的文案诊断**，不参与取值决策。
        """
        target = str(file_name or "").strip()
        if not target:
            return None
        if not self.qdrant_ready:
            self._debug_disabled("按文件名诊断命中数")
            return None
        return await self._qd.file_name_matches(target)

    async def _scroll_filter(self, query_filter: dict, label: str, limit: int = 1) -> list:
        """按 filter scroll 取回 payload 列表（保留门面上的可替换入口）。"""
        return await self._qd.scroll_filter(query_filter, label, limit)

    # ------------------------------------------------------------------
    # 登记原图时同步入向量库（图床无服务器侧钩子时的兜底通道）
    # ------------------------------------------------------------------

    @staticmethod
    def point_id_for(point_key: str) -> str:
        """Qdrant point ID 只接受整数或 UUID；用 UUID5 从内容指纹派生，保证幂等。"""
        return QdrantClient.point_id_for(point_key)

    async def _embed_file_for_upsert(self, path: str, mime: str = "") -> list:
        """upsert 时按需向量化。

        mime 为空时保持单参调用，兼容既有调用方/测试对 embed_file(path) 的替换。
        """
        return await (self.embed_file(path, mime=mime) if mime else self.embed_file(path))

    async def upsert_file(
        self,
        path: str,
        point_key: str,
        image_url: str,
        file_size: int,
        width: int | None,
        height: int | None,
        src: str = "",
        *,
        mime: str = "",
    ) -> None:
        """写入/覆盖一个向量点（payload 组装与请求细节见 QdrantClient）。"""
        await self._qd.upsert_file(
            path,
            point_key,
            image_url,
            file_size,
            width,
            height,
            src,
            mime=mime,
        )

    async def delete_point(self, point_key: str) -> None:
        """按内容指纹删除向量点；点或集合不存在时按已删除处理。"""
        await self._qd.delete_point(point_key)

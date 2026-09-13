"""向量检索引擎（图片溯源双引擎之“向量”）。

查询图片 -> 配置的多模态向量 AI（OpenAI 兼容 /v1/embeddings）生成向量
-> Qdrant REST 检索最相似的图床图片（相似度 = Qdrant cosine score）。

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
import base64
import json
import math
import os
import time
import uuid
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("vector_search")

try:
    from .common import as_float, as_int
    from .features import image_mime
    from .http_client import HttpClientGetter
    from .url_guard import (
        ResponseTooLargeError,
        UrlBlockedError,
        read_limited_text,
    )
except ImportError:  # 兼容插件以独立模块方式加载
    from common import as_float, as_int  # type: ignore[no-redef]
    from features import image_mime  # type: ignore[no-redef]
    from http_client import HttpClientGetter  # type: ignore[no-redef]
    from url_guard import (  # type: ignore[no-redef]
        ResponseTooLargeError,
        UrlBlockedError,
        read_limited_text,
    )


IMAGE_INPUTS = ("qwen-vl", "nemotron-vl", "dataurl", "jina-image")


class VectorEngineError(Exception):
    """向量引擎可展示给用户的错误。"""

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.detail = detail or message


class HttpError(VectorEngineError):
    """带 HTTP 状态码的向量服务错误（供调用方按状态码分支，如 404）。"""

    def __init__(self, status: int, body: str):
        super().__init__(f"服务返回 HTTP {status}", detail=f"HTTP {status}: {body[:500]}")
        self.status = status


class InvalidEmbeddingJSON(Exception):
    """Embedding 服务返回 HTTP 200，但响应体不是合法 JSON。"""

    def __init__(self, message: str, position: int | None = None):
        super().__init__(message)
        self.position = position


def parse_embedding_response(text: str) -> list[float]:
    """解析并校验 OpenAI 兼容 embeddings 响应中的 data[0].embedding。"""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidEmbeddingJSON(str(exc), exc.pos) from exc

    try:
        embedding = obj["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("embedding 响应缺少 data[0].embedding") from exc
    if not isinstance(embedding, list) or not embedding:
        raise ValueError("embedding 响应缺少 data[0].embedding")
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        for value in embedding
    ):
        raise ValueError("embedding 结果含非数值或非有限数值元素")
    return [float(value) for value in embedding]


def _invalid_json_context(text: str, exc: InvalidEmbeddingJSON) -> str:
    position = exc.position if exc.position is not None else 0
    start = max(0, position - 80)
    end = min(len(text), position + 80)
    return text[start:end]


def _mime_for(path: str) -> str:
    return image_mime(path) or "image/jpeg"


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


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
        # 这里保留配置值供诊断展示，实际请求强制 passage（见 embed_bytes）
        self.input_type = str(raw.get("embed_input_type") or "passage")
        if self.input_type not in ("query", "passage"):
            self.input_type = "passage"
        self.threshold = max(0.0, min(1.0, as_float(raw.get("similarity_threshold"), 0.80)))
        self.top_k = max(1, as_int(raw.get("top_k"), 5))
        timeout_s = max(5, as_int(raw.get("request_timeout"), 30))
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._http_getter = http_getter
        # embed_api_key 不参与启用判定：网关未开鉴权时留空是合法场景
        self.enabled = bool(self.qdrant_url and self.embed_base_url and self.embed_model)

    @staticmethod
    def _normalize_base_url(value: object) -> str:
        base_url = str(value or "").strip().rstrip("/")
        if base_url and not base_url.lower().startswith(("http://", "https://")):
            logger.warning("忽略非法向量服务地址（必须以 http/https 开头）")
            return ""
        return base_url

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

    # ------------------------------------------------------------------
    # 向量化
    # ------------------------------------------------------------------

    def _build_input(self, data_url: str, b64: str):
        if self.image_input == "qwen-vl":
            return [{"type": "image", "image": data_url}]
        if self.image_input == "nemotron-vl":
            # NVIDIA llama-nemotron-embed-vl 系：dataURL 字符串 + input_type
            # （非对称模型，图片只能走 passage 侧；入库与查询两侧必须一致）
            return data_url
        if self.image_input == "dataurl":
            return data_url
        if self.image_input == "jina-image":
            return [{"image": b64}]
        raise VectorEngineError(
            f"不支持的 embed_image_input={self.image_input}"
            "（可选 qwen-vl / nemotron-vl / dataurl / jina-image）"
        )

    async def embed_bytes(self, data: bytes, mime: str) -> list:
        """把图片字节向量化，返回浮点向量列表。

        请求按 embed_image_input 序列化；网络/响应/解析错误统一归为
        VectorEngineError（可展示给用户），429 带 retry-after 提示。
        """
        if not self.embed_base_url:
            raise VectorEngineError("embed_base_url 未配置，无法向量化图片。")
        if not self.embed_model:
            raise VectorEngineError("embed_model 未配置，无法向量化图片。")
        b64 = base64.b64encode(data).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
        payload = {
            "model": self.embed_model,
            "input": self._build_input(data_url, b64),
            "encoding_format": "float",
        }
        if self.image_input == "nemotron-vl":
            # 非对称模型：图片只能走 passage 侧（query 侧拒绝图片输入），
            # 与图床入库服务的图片 embed 侧保持一致
            payload["input_type"] = "passage"
        url = f"{self.embed_base_url}/embeddings"
        client = await self._http_getter()

        def prepare(_u: str, _m: str, cross_origin: bool = False) -> dict:
            # 未配置 embed_api_key 时不带鉴权头；跨域重定向时一律剥离
            headers = (
                {}
                if cross_origin
                else ({"Authorization": f"Bearer {self.embed_key}"} if self.embed_key else {})
            )
            return {
                "json": payload,
                "headers": headers,
                "timeout": self.timeout,
            }

        try:
            for attempt in (1, 2):
                async with await client.request("POST", url, prepare=prepare) as resp:
                    text = await read_limited_text(resp)
                    if resp.status != 200:
                        if resp.status == 429:
                            # NVIDIA 限流：给出冷静提示，避免插件侧重复请求加重流控
                            try:
                                retry_after = max(1, int(float(resp.headers.get("retry-after") or 5)))
                            except (TypeError, ValueError):
                                retry_after = 5
                            raise VectorEngineError(
                                f"向量服务限流（HTTP 429），请在约 {retry_after} 秒后重试"
                            )
                        mismatch_hint = "'dict' object has no attribute" in text
                        if resp.status in (500, 502, 503, 504) and attempt == 1 and not mismatch_hint:
                            # 部分网关对中等体积请求会偶发 500/502（如 NVIDIA 的
                            # "Missing request extension"），短暂退避后单次重试；
                            # 格式不匹配类 500 是确定性错误，重试无意义
                            await asyncio.sleep(2)
                            continue
                        hint = ""
                        if resp.status == 400:
                            if "cannot identify image" in text.lower():
                                hint = (
                                    "；图片内容无效（下载不完整、链接已过期或格式不受支持），"
                                    "请重新发送图片再试"
                                )
                            else:
                                hint = (
                                    "；模型拒绝该图片输入，请核对 embed_model 是否支持图片，"
                                    "或调整 embed_image_input（qwen-vl / nemotron-vl / dataurl / jina-image）"
                                )
                        elif mismatch_hint:
                            # 服务端把 input 逐项当字符串处理却收到 content 数组字典：
                            # embed_image_input 的序列化格式与该模型实际接受的不一致
                            hint = (
                                "；embedding 服务把 input 当纯文本解析，"
                                "embed_image_input 与该模型不匹配，"
                                "请改成模型/入库侧实际使用的格式（如 nemotron-vl / dataurl）"
                            )
                        raise VectorEngineError(
                            f"向量服务 HTTP {resp.status}{hint}",
                            detail=f"embedding HTTP {resp.status}: {text[:500]}",
                        )
                    try:
                        emb = parse_embedding_response(text)
                    except InvalidEmbeddingJSON as exc:
                        context = _invalid_json_context(text, exc)
                        logger.error(
                            "embedding 响应无效 JSON: "
                            f"响应长度={len(text)}, "
                            f"Content-Length={resp.headers.get('Content-Length', '<missing>')}, "
                            f"Content-Type={resp.headers.get('Content-Type', '<missing>')}, "
                            f"解析位置={exc.position}, 错误={exc}, 片段={context!r}"
                        )
                        if attempt == 1:
                            await asyncio.sleep(2)
                            continue
                        raise VectorEngineError(
                            "向量服务返回无效 JSON（已重试一次）",
                            detail=f"embedding JSON 解析失败: {exc}; 片段: {context}",
                        ) from exc
                break
        except VectorEngineError:
            raise
        except UrlBlockedError as e:
            raise VectorEngineError(
                "embedding 目标地址被安全策略拒绝",
                detail=f"embedding 目标地址被安全策略拒绝: {e}",
            ) from e
        except ResponseTooLargeError as e:
            raise VectorEngineError(f"embedding 响应体过大，已中止: {e}") from e
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise VectorEngineError(
                "embedding 请求失败（详情见日志）",
                detail=f"embedding 请求失败: {url}: {e}",
            ) from e
        except (KeyError, IndexError, ValueError) as e:
            raise VectorEngineError(
                "向量服务响应解析失败",
                detail=f"embedding 响应解析失败: {e}",
            ) from e

        if not isinstance(emb, list) or not emb:
            raise VectorEngineError("embedding 响应缺少 data[0].embedding")
        for n in emb:
            if not isinstance(n, (int, float)):
                raise VectorEngineError("embedding 结果含非数值元素")
        vector = [float(n) for n in emb]
        if any(not math.isfinite(value) for value in vector):
            raise VectorEngineError("向量服务返回了非有限数值")
        return vector

    async def embed_file(self, path: str) -> list:
        mime = await asyncio.to_thread(image_mime, path)
        if mime is None:
            raise VectorEngineError("图片内容无效（下载不完整、链接已过期或格式不受支持）")
        data = await asyncio.to_thread(_read_bytes, path)
        return await self.embed_bytes(data, mime)

    # ------------------------------------------------------------------
    # Qdrant 检索与状态
    # ------------------------------------------------------------------

    def _qd_headers(self) -> dict:
        return {"api-key": self.qdrant_key} if self.qdrant_key else {}

    def _points_url(self, operation: str = "") -> str:
        collection = quote(self.collection, safe="")
        url = f"{self.qdrant_url}/collections/{collection}/points"
        return f"{url}/{operation}" if operation else url

    async def search(self, vector: list, limit: int | None = None) -> list:
        """Qdrant 相似度检索，返回 [{id, score, payload}] 列表。

        返回的是 Qdrant 原始分数（未做阈值过滤，排序由 Qdrant 给出）；
        阈值过滤与排序展示由调用方完成。
        """
        if (
            not isinstance(vector, list)
            or not vector
            or any(
                not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector
            )
        ):
            raise VectorEngineError("查询向量无效")
        effective_limit = self.top_k if limit is None else max(1, limit)
        try:
            obj = await self._request_json(
                "POST",
                self._points_url("query"),
                json_body={
                    "query": vector,
                    "limit": effective_limit,
                    "with_payload": True,
                },
                headers=self._qd_headers(),
            )
            result = obj.get("result") or {}
            raw_hits = result.get("points", []) if isinstance(result, dict) else []
        except HttpError as exc:
            if exc.status != 404:
                raise
            obj = await self._request_json(
                "POST",
                self._points_url("search"),
                json_body={
                    "vector": vector,
                    "limit": effective_limit,
                    "with_payload": True,
                },
                headers=self._qd_headers(),
            )
            raw_hits = obj.get("result") or []
        if not isinstance(raw_hits, list):
            raw_hits = []
        hits = []
        for h in raw_hits:
            if not isinstance(h, dict):
                continue
            payload = h.get("payload") or {}
            try:
                score = float(h.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            hits.append(
                {
                    "id": str(h.get("id")),
                    "score": score,
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )
        return hits

    async def count(self) -> int:
        """返回集合点数；集合不存在按 0；连接/鉴权失败抛 VectorEngineError。"""
        url = self._points_url("count")
        try:
            obj = await self._request_json(
                "POST", url, json_body={"exact": True}, headers=self._qd_headers()
            )
            # result 可能显式为 null，此时 .get 的默认值不生效，需再兜一层
            return int((obj.get("result") or {}).get("count") or 0)
        except HttpError as e:
            # 按状态码判定集合缺失，避免响应体偶然含 "404" 字样时误判
            if e.status == 404:
                return 0
            raise

    # ------------------------------------------------------------------
    # 登记原图时同步入向量库（图床无服务器侧钩子时的兜底通道）
    # ------------------------------------------------------------------

    @staticmethod
    def point_id_for(point_key: str) -> str:
        """Qdrant point ID 只接受整数或 UUID；用 UUID5 从内容指纹派生，保证幂等。"""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, point_key))

    async def upsert_file(
        self,
        path: str,
        point_key: str,
        image_url: str,
        file_size: int,
        width: int | None,
        height: int | None,
        src: str = "",
    ) -> None:
        """写入/覆盖一个向量点。

        point_key 为内容指纹（调用方用 pHash 派生）：同图同 ID、与图床 URL
        规则解耦，任何能给出公开直链的图床模式都能同步且幂等。
        src 仅用于兼容图床侧的 payload 约定，插件自身只读 image_url。
        """
        vector = await self.embed_file(path)
        file_name = os.path.basename(urlparse(image_url).path) or os.path.basename(path)
        payload = {
            "src": src or image_url,
            "image_url": image_url,
            "file_name": file_name,
            "mime": _mime_for(path),
            "size_bytes": int(file_size or 0),
            "width": width,
            "height": height,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "channel": "",
        }
        # Qdrant point ID 只接受整数或 UUID；用 UUID5 从内容指纹派生，保证幂等
        point_id = self.point_id_for(point_key)
        url = f"{self._points_url()}?wait=true"
        body = {"points": [{"id": point_id, "vector": vector, "payload": payload}]}
        await self._request_json("PUT", url, json_body=body, headers=self._qd_headers())

    async def delete_point(self, point_key: str) -> None:
        """按内容指纹删除向量点；点或集合不存在时按已删除处理。

        只清理插件侧以同规则写入的点（point_key = f"phash:{phash}"）；
        图床侧钩子写入的点使用图床自己的 key 约定，其清理需在图床侧完成。
        """
        url = f"{self._points_url('delete')}?wait=true"
        body = {"points": [self.point_id_for(point_key)]}
        try:
            await self._request_json("POST", url, json_body=body, headers=self._qd_headers())
        except HttpError as e:
            if e.status == 404:  # 集合不存在等价于点已不存在
                return
            raise

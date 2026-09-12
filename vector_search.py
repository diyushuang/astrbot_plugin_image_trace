"""向量检索引擎（图片溯源双引擎之“向量”）。

查询图片 -> 配置的多模态向量 AI（OpenAI 兼容 /v1/embeddings）生成向量
-> Qdrant REST 检索最相似的图床图片（相似度 = Qdrant cosine score）。

与服务器侧图床入库服务使用同一套协议与输入序列化，保证同一向量空间：
- embed_image_input=qwen-vl:      content 数组 [{"type":"image","image":<dataURL>}]
- embed_image_input=nemotron-vl:  裸 dataURL 字符串 + input_type（非对称模型）
- embed_image_input=dataurl:      裸 dataURL 字符串
- embed_image_input=jina-image:   [{"image":<base64 无前缀>}]

出网请求一律经 url_guard.guarded_request 逐跳 SSRF 校验 + IP 钉扎
（qdrant_url / embed_base_url 必须是公网可达的 http/https 地址）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import aiohttp

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("vector_search")

try:
    from .common import as_float, as_int
    from .url_guard import (
        ResponseTooLargeError,
        UrlBlockedError,
        guarded_request,
        read_limited_text,
    )
except ImportError:  # 兼容插件以独立模块方式加载
    from common import as_float, as_int  # type: ignore[no-redef]
    from url_guard import (  # type: ignore[no-redef]
        ResponseTooLargeError,
        UrlBlockedError,
        guarded_request,
        read_limited_text,
    )


IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jfif": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
}

IMAGE_INPUTS = ("qwen-vl", "nemotron-vl", "dataurl", "jina-image")


class VectorEngineError(Exception):
    """向量引擎可展示给用户的错误。"""


class HttpError(VectorEngineError):
    """带 HTTP 状态码的向量服务错误（供调用方按状态码分支，如 404）。"""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


def _mime_for(path: str) -> str:
    return IMAGE_MIME.get(os.path.splitext(path)[1].lower(), "image/jpeg")


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


class VectorEngine:
    """Qdrant + 多模态 embedding 的只读检索与登记同步客户端。"""

    def __init__(
        self,
        config,
        session_getter: Callable[[], "aiohttp.ClientSession"],
    ):
        """session_getter: 异步可调用，返回共享 aiohttp.ClientSession。"""
        raw = config.get("vector_search")
        if not isinstance(raw, dict):
            raw = {}
        self.qdrant_url = str(raw.get("qdrant_url") or "").rstrip("/")
        self.qdrant_key = str(raw.get("qdrant_api_key") or "")
        self.collection = str(raw.get("collection_name") or "imgbed_images")
        self.embed_base_url = str(raw.get("embed_base_url") or "").rstrip("/")
        self.embed_key = str(raw.get("embed_api_key") or "")
        self.embed_model = str(raw.get("embed_model") or "")
        self.image_input = str(raw.get("embed_image_input") or "qwen-vl")
        if self.image_input not in IMAGE_INPUTS:
            # 配置错误在加载期就暴露（日志可见），而不是拖到第一次请求才失败
            logger.warning(
                f"embed_image_input 配置无效: {self.image_input!r}，"
                f"可选 {' / '.join(IMAGE_INPUTS)}；已回退为 qwen-vl。"
            )
            self.image_input = "qwen-vl"
        # 仅 nemotron-vl 的非对称模型区分 input_type；图片侧只能走 passage，
        # 这里保留配置值供诊断展示，实际请求强制 passage（见 embed_bytes）
        self.input_type = str(raw.get("embed_input_type") or "passage")
        if self.input_type not in ("query", "passage"):
            self.input_type = "passage"
        self.threshold = max(
            0.0, min(1.0, as_float(raw.get("similarity_threshold"), 0.80))
        )
        self.top_k = max(1, as_int(raw.get("top_k"), 5))
        timeout_s = max(5, as_int(raw.get("request_timeout"), 30))
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session_getter = session_getter
        self.enabled = bool(
            self.qdrant_url and self.embed_base_url and self.embed_key and self.embed_model
        )

    # ------------------------------------------------------------------
    # 配置诊断
    # ------------------------------------------------------------------

    def missing_fields(self) -> list:
        """返回未配置的必填项名称列表（供提示文案使用）。

        qdrant_api_key 不列入：自托管无鉴权的 Qdrant 是合法场景
        （与 __init__ 的 enabled 判定口径一致）。
        """
        missing = []
        if not self.qdrant_url:
            missing.append("vector_search.qdrant_url")
        if not self.embed_base_url:
            missing.append("vector_search.embed_base_url")
        if not self.embed_key:
            missing.append("vector_search.embed_api_key")
        if not self.embed_model:
            missing.append("vector_search.embed_model")
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
        json_body: Optional[dict] = None,
        headers: Optional[dict] = None,
    ):
        session = await self._session_getter()

        def prepare(_u: str, _m: str, cross_origin: bool = False) -> dict:
            return {
                "json": json_body,
                # 跨域重定向到第三方时不再携带 api-key 等鉴权头
                "headers": {} if cross_origin else (headers or {}),
                "timeout": self.timeout,
            }

        try:
            async with await guarded_request(session, method, url, prepare=prepare) as resp:
                text = await read_limited_text(resp)
                status = resp.status
        except VectorEngineError:
            raise
        except UrlBlockedError as e:
            raise VectorEngineError(f"目标地址被安全策略拒绝: {e}") from e
        except ResponseTooLargeError as e:
            raise VectorEngineError(f"响应体过大，已中止: {e}") from e
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise VectorEngineError(f"网络请求失败: {url}: {e}") from e
        return status, text

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> Any:
        status, text = await self._request(method, url, json_body=json_body, headers=headers)
        if status != 200:
            raise HttpError(status, text)
        try:
            return json.loads(text)
        except ValueError as e:
            raise VectorEngineError(f"响应不是合法 JSON: {text[:100]}") from e

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
        if not self.embed_key:
            raise VectorEngineError("embed_api_key 未配置，无法向量化图片。")
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
        session = await self._session_getter()

        def prepare(_u: str, _m: str, cross_origin: bool = False) -> dict:
            headers = {} if cross_origin else {"Authorization": f"Bearer {self.embed_key}"}
            return {
                "json": payload,
                "headers": headers,
                "timeout": self.timeout,
            }

        try:
            async with await guarded_request(session, "POST", url, prepare=prepare) as resp:
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
                    hint = ""
                    if resp.status == 400:
                        hint = (
                            "；模型拒绝该图片输入，请核对 embed_model 是否支持图片，"
                            "或调整 embed_image_input（qwen-vl / nemotron-vl / dataurl / jina-image）"
                        )
                    raise VectorEngineError(f"embedding HTTP {resp.status}{hint}: {text[:200]}")
                obj = json.loads(text)
                emb = obj["data"][0]["embedding"]
        except VectorEngineError:
            raise
        except UrlBlockedError as e:
            raise VectorEngineError(f"embedding 目标地址被安全策略拒绝: {e}") from e
        except ResponseTooLargeError as e:
            raise VectorEngineError(f"embedding 响应体过大，已中止: {e}") from e
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise VectorEngineError(f"embedding 请求失败: {e}") from e
        except (KeyError, IndexError, ValueError) as e:
            raise VectorEngineError(f"embedding 响应解析失败: {e}") from e

        if not isinstance(emb, list) or not emb:
            raise VectorEngineError("embedding 响应缺少 data[0].embedding")
        for n in emb:
            if not isinstance(n, (int, float)):
                raise VectorEngineError("embedding 结果含非数值元素")
        return [float(n) for n in emb]

    async def embed_file(self, path: str) -> list:
        data = await asyncio.to_thread(_read_bytes, path)
        return await self.embed_bytes(data, _mime_for(path))

    # ------------------------------------------------------------------
    # Qdrant 检索与状态
    # ------------------------------------------------------------------

    def _qd_headers(self) -> dict:
        return {"api-key": self.qdrant_key} if self.qdrant_key else {}

    async def search(self, vector: list, limit: Optional[int] = None) -> list:
        """Qdrant 相似度检索，返回 [{id, score, payload}] 列表。

        返回的是 Qdrant 原始分数（未做阈值过滤，排序由 Qdrant 给出）；
        阈值过滤与排序展示由调用方完成。
        """
        url = f"{self.qdrant_url}/collections/{self.collection}/points/search"
        body = {
            "vector": vector,
            "limit": limit or self.top_k,
            "with_payload": True,
        }
        obj = await self._request_json("POST", url, json_body=body, headers=self._qd_headers())
        result = obj.get("result") or []
        if not isinstance(result, list):
            return []
        hits = []
        for h in result:
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
        url = f"{self.qdrant_url}/collections/{self.collection}/points/count"
        try:
            obj = await self._request_json(
                "POST", url, json_body={"exact": True}, headers=self._qd_headers()
            )
            # result 可能显式为 null，此时 .get 的默认值不生效，需再兜一层
            return int((obj.get("result") or {}).get("count") or 0)
        except HttpError as e:
            # 按状态码判定集合缺失，避免响应体偶然含 "404" 字样时误判
            if e.status == 404 or "doesn't exist" in str(e):
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
        width: Optional[int],
        height: Optional[int],
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
        url = f"{self.qdrant_url}/collections/{self.collection}/points?wait=true"
        body = {"points": [{"id": point_id, "vector": vector, "payload": payload}]}
        await self._request_json("PUT", url, json_body=body, headers=self._qd_headers())

    async def delete_point(self, point_key: str) -> None:
        """按内容指纹删除向量点；点或集合不存在时按已删除处理。

        只清理插件侧以同规则写入的点（point_key = f"phash:{phash}"）；
        图床侧钩子写入的点使用图床自己的 key 约定，其清理需在图床侧完成。
        """
        url = f"{self.qdrant_url}/collections/{self.collection}/points/delete?wait=true"
        body = {"points": [self.point_id_for(point_key)]}
        try:
            await self._request_json("POST", url, json_body=body, headers=self._qd_headers())
        except HttpError as e:
            if e.status == 404:  # 集合不存在等价于点已不存在
                return
            raise

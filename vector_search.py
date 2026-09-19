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
from urllib.parse import quote, unquote, urlparse, urlsplit

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

# 同名多份时，最多取回多少个候选做「是否同一张图」的判定。
# 实测本部署重名组最多 4 份，50 有足够余量；设上限是为了防止极端脏数据
# （某个名字挂着成百上千个点）把一次反查拖成大批量拉取。
DUPLICATE_SCAN_CAP = 50


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


def thumb_path_key(url) -> str:
    """取直链里 `/file/` 之后的对象键（相对路径），用作反查的 host 无关判据。

    图床把「同一张图」以两种 host 暴露：内网 `192.9.240.227:7658` 与公网反代
    `img.dixc.de`（OpenResty → `172.19.0.2:8080`）。`payload.thumb_url` 里存的是
    **入库当时**的那一种（实测本部署全是内网 IP），而插件运行期手上的直链可能是
    另一种，于是整串精确匹配永远 0 命中。对象键与 host 无关，只按它匹配即可。

    编码差异同样要吸收：库里 `thumb_url` 存**原始未编码**形态（含中文），插件手上
    的直链通常已 percent-encode，故先 `unquote` 再取路径。取不到 `/file/` 段时
    退回整条路径（去掉前导 `/`），保证本函数对非常规直链也可用。
    """
    try:
        path = unquote(urlsplit(str(url or "")).path)
    except Exception:
        return ""
    if not path:
        return ""
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return ""
    if "file" in segments:
        segments = segments[segments.index("file") + 1 :]
    return "/".join(segments)


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
        self.duplicate_threshold = max(
            0.0,
            min(1.0, as_float(raw.get("duplicate_vector_threshold"), 0.995)),
        )
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

    async def search(
        self, vector: list, limit: int | None = None, *, with_vector: bool = False
    ) -> list:
        """Qdrant 相似度检索，返回 [{id, score, payload, vector?}] 列表。

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
                    "with_vector": with_vector,
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
                    "with_vector": with_vector,
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
                    **({"vector": h.get("vector")} if with_vector else {}),
                }
            )
        return hits

    async def scroll_payloads(
        self,
        *,
        key: str,
        page_size: int = 512,
        max_points: int = 0,
        with_payload_keys: list | None = None,
    ) -> list:
        """遍历全集合，收集**存在 payload[key] 的点**，返回 [{id, payload}]。

        专为「把服务端预置的感知哈希整体拉到本地建索引」而设：Qdrant 不支持
        「按键非空」服务端过滤（`is_not_empty` 会 400），只能整集合翻页后
        在本地筛。翻页用 `next_page_offset`，直到为空或达到 max_points。

        只取需要的 payload 键（with_payload_keys），2 万级点位下能把响应体
        从数十 MB 压到几 MB。`key` 传入的键只要不是非空字符串就跳过该点——
        服务端把退化图写成哨兵串（'-'）而非空值，正是为了让这类点能被本地
        判据识别出来，所以这里不做真值判断，只要求「非空字符串」。

        出错时抛出 VectorEngineError（由调用方决定降级），不做静默吞错：
        半截索引比没有索引更危险——它会给出「未命中」的确定结论。
        """
        if not key:
            return []
        wanted = [str(k) for k in (with_payload_keys or [key]) if str(k)]
        collected: list = []
        offset = None
        while True:
            body = {
                "limit": max(1, int(page_size)),
                "with_payload": wanted,
                "with_vector": False,
            }
            if offset is not None:
                body["offset"] = offset
            obj = await self._request_json(
                "POST", self._points_url("scroll"), json_body=body, headers=self._qd_headers()
            )
            result = obj.get("result") or {}
            points = result.get("points") if isinstance(result, dict) else None
            if not isinstance(points, list) or not points:
                break
            for point in points:
                if not isinstance(point, dict):
                    continue
                payload = point.get("payload")
                if not isinstance(payload, dict):
                    continue
                value = payload.get(key)
                if not isinstance(value, str) or not value.strip():
                    continue
                collected.append({"id": str(point.get("id")), "payload": payload})
                # 上限在页内也要生效：只在页间判定的话，一页 512 条会整页照收，
                # --limit 50 的试跑实际拿到 512 条，「先小样本验证」就失去意义
                if max_points > 0 and len(collected) >= max_points:
                    return collected
            offset = result.get("next_page_offset")
            if not offset:
                break
        return collected

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

    async def thumb_url_for(self, file_id: str) -> str | None:
        """取该原图对应点上的缩略图直链（payload.thumb_url）；没有则为 None。

        「一图一向量一替身」：点 id = UUID5(NAMESPACE_URL, 原图 id)，同一张原图的
        `image_url`（原图）与 `thumb_url`（缩略图）落在**同一个点**上。所以「原图 →
        缩略图」只需按 id 取点，既不需要映射表，也不能靠文件名——图床给缩略图改过
        名（加时间戳前缀、`@` 换 `_` 并移入 thumbnails/ 目录）。

        本方法**永不抛错**：缩略图只是提速手段，取不到就由调用方照常发原图，
        绝不能因为查不到替身而让发送失败。
        """
        key = str(file_id or "").strip()
        if not key or not self.enabled:
            return None
        try:
            obj = await self._request_json(
                "POST",
                self._points_url(),
                json_body={
                    "ids": [self.point_id_for(key)],
                    "with_payload": True,
                    "with_vector": False,
                },
                headers=self._qd_headers(),
            )
        except Exception as exc:  # 网络/鉴权/集合缺失一律视为「没有替身」
            logger.debug(f"查询缩略图失败（按无替身处理）{key}: {exc}")
            return None
        result = obj.get("result")
        if not isinstance(result, list) or not result:
            return None
        first = result[0]
        payload = first.get("payload") if isinstance(first, dict) else None
        if not isinstance(payload, dict):
            return None
        thumb = str(payload.get("thumb_url") or "").strip()
        return thumb or None

    async def original_url_for_thumb(self, thumb_url: str) -> str | None:
        """取该缩略图直链所属点上的原图直链（payload.image_url）；没有则为 None。

        与 thumb_url_for 互为逆操作。这里**不能**用 point_id_for 反推点 id：
        缩略图上传时被图床改过名（加时间戳前缀、`@` 换 `_` 并移入 thumbnails/），
        从缩略图 URL 无论如何算不出原图 id，UUID5 那条路是死的。唯一可行的办法
        是按 payload 找到那个点，再取同点的 `image_url`。

        匹配先按整串精确比对，再退回按路径比对（见 `_payload_by_thumb`）——后者
        用来吸收「入库记内网 IP、运行期持公网域名」这类 host 差异。

        命中数必须**恰好为 1**才返回：0 表示这个点没有缩略图（正常，属预期内），
        >1 表示数据异常（同一缩略图挂在多个点上），此时宁可返回 None 交调用方
        走原逻辑，也不能赌一个。

        本方法**永不抛错**：与 thumb_url_for 同理，反查只是纠正手段，失败时由
        调用方走原有的「按名直查」逻辑，绝不能让 /原图 因为反查失败而不可用。
        """
        target = str(thumb_url or "").strip()
        if not target or not self.enabled:
            return None
        payload = await self._payload_by_thumb(target)
        if payload is None:
            return None
        original = str(payload.get("image_url") or "").strip()
        return original or None

    async def original_info_for_thumb(self, thumb_url: str) -> dict | None:
        """按缩略图直链取回**整个原图点**的信息：{"url": …, "file_name": …}。

        比 original_url_for_thumb 多带一个 `file_name`，这一点很关键：缩略图名
        被图床改过（前置毫秒时间戳 + `@` 换 `_`），而 `@`→`_` 是**不可逆**的——
        光靠清洗缩略图名永远还原不出真正的文件名。同一个点的 payload 里
        `file_name` 存的正是**未经改名的原始文件名**（实测：
        file_name = `【微博@赵今麦工作室official】20250716-02：林其乐剧照.jpg`
        thumb_url = `…/thumbnails/1789658616018_【微博_赵今麦工作室official】….jpg`），
        所以展示名必须取自这里，而不是清洗缩略图名。

        file_name 可能缺失（图床侧 payload 约定不同），此时只返回 url，由调用方
        退回「按缩略图名清洗」的兜底展示。

        入参的 host 不必与库里存的一致：`_payload_by_thumb` 会先试整串精确匹配，
        再退回按路径匹配，故公网域名与内网 IP 两种写法都能反查到同一点。

        与 original_url_for_thumb 同样：命中数非 1 一律放弃，永不抛错。
        """
        target = str(thumb_url or "").strip()
        if not target or not self.enabled:
            return None
        payload = await self._payload_by_thumb(target)
        if payload is None:
            return None
        original = str(payload.get("image_url") or "").strip()
        if not original:
            return None
        return {
            "url": original,
            "file_name": str(payload.get("file_name") or "").strip(),
        }

    async def original_by_file_name(self, file_name: str) -> dict | None:
        """按**裸文件名**反查原图：{"url": 带目录的权威直链, "file_name": 真名}。

        为什么必须有这个方法：图床的 `/file/{path}` **只认完整对象键**（含各级
        目录），把裸文件名拼上去**必然 404**。而 `/原图 <文件名>` 这条路径手上
        只有名字，拼出来的就是 `/file/【微博@…】20200207-04：赵今麦海报.jpg`——
        图床里根本不存在这个对象，原图明明在
        `/file/7、综艺节目/爱奇艺《潮流合伙人》/《潮流合伙人》图集/` 下面。
        实测同一文件的 A/B 对照（线上 http://192.9.240.227:7658 与公网
        https://img.dixc.de 结果一致）：

            裸文件名   /file/{裸名}            → HTTP 404
            带完整目录 /file/7、综艺节目/…/{裸名} → HTTP 200

        所以正确做法是拿名字去 Qdrant 反查那条**入库时登记的权威直链**
        （payload.image_url 里带完整目录），而不是凭名字去猜目录。

        **按值精确匹配**（`match.value`，不是 `match.text`）：`file_name` 是图床
        登记的真名，含 `@`、中文冒号等字符，用 text 匹配会被切成词而误命中。
        实测该字段可以直接整串精确命中。

        命中数为 0（库里没有这个名字，或压根没进索引）时返回 None，交给调用方
        退回「原样拼 URL + 探测」的老逻辑。

        **命中数 >1（同名文件在不同目录下有多份）时不再一律放弃**——这是 v1.7.1
        的行为变更，理由见 `_duplicate_equivalent`。实测本部署 23,932 点里
        **6,127 个文件名（37.3%）是重名的**，若一律放弃，这些名字发
        `/原图 <文件名>` 会 100% 报「图床里没有找到」，而它们的原图明明就在
        索引里。改为：先判定这些同名点是否**指向同一张图**，是则确定性地选一份
        返回；只有确认「同名但内容不同」才放弃（那种情况下赌错会把别的图当原图
        发出去，比说「找不到」更糟）。

        本方法**永不抛错**，失败一律返回 None。
        """
        target = str(file_name or "").strip()
        if not target or not self.enabled:
            return None
        query_filter = {"must": [{"key": "file_name", "match": {"value": target}}]}
        label = f"文件名 {target}"
        hits = await self._count_filter(query_filter, label)
        if hits is None or hits == 0:
            return None
        if hits == 1:
            payloads = await self._scroll_filter(query_filter, label, 1)
            return self._original_info_from_payload(payloads[0] if payloads else None, target)
        # ---- 同名多份：先证明「是同一张图」，再确定性地选一份 ----
        payloads = await self._scroll_filter(query_filter, label, min(hits, DUPLICATE_SCAN_CAP))
        if not self._duplicate_equivalent(payloads):
            logger.debug(f"同名 {hits} 份且内容不同，放弃反查（需用户指明目录）: {label}")
            return None
        chosen = self._pick_duplicate(payloads)
        if chosen is None:
            return None
        info = self._original_info_from_payload(chosen, target)
        if info is not None:
            logger.info(
                f"同名 {hits} 份且内容一致，已确定性地取其一: {label} -> "
                f"{str(chosen.get('src') or chosen.get('image_url') or '')[:120]}"
            )
        return info

    @staticmethod
    def _original_info_from_payload(payload: dict | None, target: str) -> dict | None:
        """把 Qdrant payload 收敛成 {"url", "file_name"}；缺 image_url 时返回 None。

        缺 image_url 必须返回 None 而不是空直链：调用方拿空 url 会当成「查不到」
        走兜底，但若这里返 `{"url": ""}`，语义就变成了「命中了但直链是空的」，
        更容易在下游被误当作成功。
        """
        if not isinstance(payload, dict):
            return None
        original = str(payload.get("image_url") or "").strip()
        if not original:
            return None
        return {
            "url": original,
            "file_name": str(payload.get("file_name") or "").strip() or target,
        }

    @staticmethod
    def _duplicate_equivalent(payloads: list[dict]) -> bool:
        """判定「同名多份」的这些点是否**指向同一张图**。

        为什么可以有这个判定：图床里同一个文件被放进多个相册目录是常见操作，
        入库时每份各建一个点，于是同名点内容完全相同、只是目录不同。实测本部署
        6,127 个重名组**全部**满足：组内 `size_bytes` 逐组一致；随机抽 13 组下载
        全部副本算 sha256，**逐组字节完全相同**。故「同名 + 同大小」在本部署等价
        于「同一张图」，任选一份发给用户拿到的都是同一张图。

        判据（全部满足才算等价，任一不满足即判「不敢选」）：
        - 至少 2 个点；
        - 每个点的 `size_bytes` 都是**正数**且**组内完全一致**——缺大小就无从
          判定，一律当作不等价，宁可退回「找不到」也不赌；
        - `mime`（凡有值者）组内一致——防同名不同格式；
        - `phash`（凡有值者）组内一致——回填过的点能用时，它是比大小更硬的
          逐位证据，可**否决**「大小相同但内容不同」的巧合。

        注意 phash 只作否决、不作必要条件：本部署回填刚起步（实测仅 25 个点有
        值），要求 phash 存在会让这条修复对绝大多数名字失效。
        """
        if len(payloads) < 2:
            return False
        sizes: set = set()
        mimes: set = set()
        hashes: set = set()
        for payload in payloads:
            size = payload.get("size_bytes")
            if not isinstance(size, (int, float)) or isinstance(size, bool) or size <= 0:
                return False
            sizes.add(size)
            mime = str(payload.get("mime") or "").strip()
            if mime:
                mimes.add(mime)
            phash = str(payload.get("phash") or "").strip()
            if phash:
                hashes.add(phash)
        return len(sizes) == 1 and len(mimes) <= 1 and len(hashes) <= 1

    @staticmethod
    def _pick_duplicate(payloads: list[dict]) -> dict | None:
        """在已判定等价的同名候选里**确定性地**选一份。

        排序键取 `src`（图床相对路径），退化用 `image_url`。之所以要确定性而不是
        随便取第一个：Qdrant scroll 的返回顺序不保证稳定，同一个名字两次调用可能
        给出不同目录的直链，让用户看到「同一条命令发出的是不同链接」而怀疑数据在
        变。按路径字典序取最小，至少保证同名请求的结果可复现。
        """
        usable = [p for p in payloads if str(p.get("image_url") or "").strip()]
        if not usable:
            return None
        return min(usable, key=lambda p: str(p.get("src") or p.get("image_url") or ""))

    async def file_name_matches(self, file_name: str) -> int | None:
        """该文件名在库里的命中数；查询失败返回 None（区别于「确实是 0 条」）。

        只服务于 `/原图` **失败路径的文案诊断**，不参与取值决策。原因：反查落空
        后调用方会退回「裸名拼 URL + 探测」，而裸名必然 404，于是无论哪种落空原因
        都会报成「图床里没有找到」——对「同名多份但内容不同」这一种，图其实**确实
        存在**，用户照着提示去核对只会白跑。靠命中数把两种情况分开，才能给出可操作
        的提示（让用户补目录）。

        之所以单独发一次 count、而不是让 `original_by_file_name` 把原因带出来：
        后者要保持「返回 dict 或 None」的简单契约，而这条诊断只在失败时才需要，
        放在失败路径上按需付费，成功路径一次请求都不多发。
        """
        target = str(file_name or "").strip()
        if not target or not self.enabled:
            return None
        query_filter = {"must": [{"key": "file_name", "match": {"value": target}}]}
        return await self._count_filter(query_filter, f"文件名诊断 {target}")

    async def _payload_by_thumb(self, thumb_url: str) -> dict | None:
        """按 thumb_url 取回**唯一**命中点的 payload；非唯一/失败返回 None。

        匹配分两轮，先严后宽：

        1. **整串精确匹配**（原行为）——入参与库中值逐字节相同时最快也最准；
        2. **按路径匹配**——只用 `/file/` 之后的**相对路径**（含 `thumbnails/` 段）
           去匹配。

        第二轮是必需的，不是冗余：`thumb_url` 里存的是**入库当时**的 host
        （实测本部署 6000/6000 全是内网 `192.9.240.227:7658`），而插件运行期
        手上的直链可能来自公网域名（同一图床的 OpenResty 反代，`img.dixc.de`
        → `172.19.0.2:8080`）。同一个文件的两种写法 host 不同、路径逐字节相同，
        整串精确匹配必然 0 命中——v1.5.9 的 `/原图` 反查正是死在这里。按路径匹配
        把 host 排除在判据之外，两种写法都能命中。

        之所以**可以**只按路径匹配：图床对象键（`/file/` 之后的路径）是全局唯一的，
        实测采样 300 点，完整相对路径与去 `thumbnails/` 后的相对路径**均 300/300
        唯一命中、无多命中**。

        命中数必须**恰好为 1**：0 表示没有这个缩略图（正常），>1 表示数据异常
        （同一路径挂在多个点上），此时宁可返回 None 交调用方走原逻辑，也不赌一个。
        """
        target = str(thumb_url or "").strip()
        if not target:
            return None
        for candidate in (target, target.rstrip("/")):
            payload = await self._payload_by_thumb_exact(candidate)
            if payload is not None:
                return payload
        key = thumb_path_key(target)
        if not key:
            return None
        return await self._payload_by_thumb_path(key)

    async def _payload_by_thumb_exact(self, thumb_url: str) -> dict | None:
        """按 thumb_url 整串精确匹配（逐字节相同才命中）。"""
        query_filter = {"must": [{"key": "thumb_url", "match": {"value": thumb_url}}]}
        return await self._unique_payload(query_filter, f"整串 {thumb_url}")

    async def _payload_by_thumb_path(self, path_key: str) -> dict | None:
        """按 `/file/` 之后的相对路径匹配，绕开 host 差异。

        Qdrant 的 `match.text` 对 text 字段按词切分、区分大小写，路径里的 `/` 与
        中文标点会被当作分隔符；这里传入的是完整相对路径（如
        `thumbnails/1789660677557_【微博_兰蔻LANCOME】20230507-04：郑州线下活动.jpg`），
        实测对真实数据可整串命中且唯一。仍以 count 唯一性为准，不唯一即放弃。
        """
        query_filter = {"must": [{"key": "thumb_url", "match": {"text": path_key}}]}
        return await self._unique_payload(query_filter, f"路径 {path_key}")

    async def _count_filter(self, query_filter: dict, label: str) -> int | None:
        """count(exact) 取命中数；网络/鉴权/集合缺失一律返回 None（调用方按「查不到」处理）。

        单独抽出来是因为它现在有两个调用方（唯一性命中判定、同名多份判定），
        而两者对「查询失败」的语义完全一致——都必须是「查不到」而不是「0 条」。
        """
        try:
            count_obj = await self._request_json(
                "POST",
                self._points_url("count"),
                json_body={"exact": True, "filter": query_filter},
                headers=self._qd_headers(),
            )
            return int((count_obj.get("result") or {}).get("count") or 0)
        except Exception as exc:  # 网络/鉴权/集合缺失一律视为「查不到」
            logger.debug(f"反查计数失败（按查不到处理）{label}: {exc}")
            return None

    async def _scroll_filter(self, query_filter: dict, label: str, limit: int = 1) -> list:
        """按 filter scroll 取回 payload 列表；失败返回空列表。

        只回 payload（调用方要的是 image_url/file_name/size_bytes 这些字段），
        不取向量——2 万级点位上带向量会把响应体放大一到两个数量级。
        """
        try:
            obj = await self._request_json(
                "POST",
                self._points_url("scroll"),
                json_body={
                    "filter": query_filter,
                    "limit": max(1, int(limit)),
                    "with_payload": True,
                    "with_vector": False,
                },
                headers=self._qd_headers(),
            )
        except Exception as exc:
            logger.debug(f"反查取点失败（按查不到处理）{label}: {exc}")
            return []
        points = (obj.get("result") or {}).get("points")
        if not isinstance(points, list):
            return []
        payloads: list = []
        for point in points:
            payload = point.get("payload") if isinstance(point, dict) else None
            if isinstance(payload, dict):
                payloads.append(payload)
        return payloads

    async def _unique_payload(self, query_filter: dict, label: str) -> dict | None:
        """先 count(exact) 确认命中数恰为 1，再 scroll 取回该点 payload。

        count 便宜且能直接区分「没有」与「有多个」，避免拿到一页结果后还要自己
        判断是否唯一。两步共用同一个 filter，语义一致。
        """
        hits = await self._count_filter(query_filter, label)
        if hits != 1:
            # 0=没有这个缩略图（正常）；>1=数据异常，宁可不反查也不赌一个
            if hits is not None:
                logger.debug(f"缩略图反查命中 {hits} 条，放弃反查: {label}")
            return None
        payloads = await self._scroll_filter(query_filter, label, 1)
        return payloads[0] if payloads else None

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

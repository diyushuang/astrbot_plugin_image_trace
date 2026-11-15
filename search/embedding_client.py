"""Embedding 客户端：图片输入序列化 + OpenAI 兼容 /v1/embeddings 请求。

只负责「输入构造 + 出网请求 + 响应解析」，配置读取与启用判定留在
`vector_search.VectorEngine` 门面。网络错误统一收敛为 `VectorEngineError`，
429 带 retry-after 提示，对偶发 5xx 与无效 JSON 各保留一次重试。

与服务器侧图床入库服务使用同一套输入序列化，保证同一向量空间：
- embed_image_input=nemotron-vl:  裸 dataURL 字符串 + input_type（非对称模型，默认；
  NVIDIA llama-nemotron-embed-vl 系只接受这一种）
- embed_image_input=qwen-vl:      content 数组 [{"type":"image","image":<dataURL>}]
  （仅 Qwen3-VL-Embedding 系需要）
- embed_image_input=dataurl:      裸 dataURL 字符串
- embed_image_input=jina-image:   [{"image":<base64 无前缀>}]
"""

from __future__ import annotations

import asyncio
import base64

import aiohttp

try:
    from astrbot.api import logger as _module_logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    _module_logger = logging.getLogger("search.embedding_client")

from ..url_guard import ResponseTooLargeError, UrlBlockedError, read_limited_text
from .errors import InvalidEmbeddingJSON, VectorEngineError, parse_embedding_response

IMAGE_INPUTS = ("qwen-vl", "nemotron-vl", "dataurl", "jina-image")


def _invalid_json_context(text: str, exc: InvalidEmbeddingJSON) -> str:
    position = exc.position if exc.position is not None else 0
    start = max(0, position - 80)
    end = min(len(text), position + 80)
    return text[start:end]


class EmbeddingClient:
    """把图片字节向量化的 HTTP 客户端（不读取配置、不判启用）。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        image_input: str,
        timeout: aiohttp.ClientTimeout,
        http_getter,
        logger=None,
    ):
        """http_getter: 异步可调用，返回共享 GuardedHttpClient（延迟解析，便于替换）。"""
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.image_input = image_input
        self.timeout = timeout
        self._http_getter = http_getter
        self._logger = logger or _module_logger

    def build_input(self, data_url: str, b64: str):
        """按 embed_image_input 序列化 input 字段。"""
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
        if not self.base_url:
            raise VectorEngineError("embed_base_url 未配置，无法向量化图片。")
        if not self.model:
            raise VectorEngineError("embed_model 未配置，无法向量化图片。")
        b64 = base64.b64encode(data).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
        payload = {
            "model": self.model,
            "input": self.build_input(data_url, b64),
            "encoding_format": "float",
        }
        if self.image_input == "nemotron-vl":
            # 非对称模型：图片只能走 passage 侧（query 侧拒绝图片输入），
            # 与图床入库服务的图片 embed 侧保持一致
            payload["input_type"] = "passage"
        url = f"{self.base_url}/embeddings"
        client = await self._http_getter()

        def prepare(_u: str, _m: str, cross_origin: bool = False) -> dict:
            # 未配置 embed_api_key 时不带鉴权头；跨域重定向时一律剥离
            headers = (
                {}
                if cross_origin
                else ({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})
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
                        self._logger.error(
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

        # parse_embedding_response 已保证：非空 list[float]、逐元素为数值且有限，
        # 因此这里无需再做一遍 O(dim) 的二次校验。
        return emb

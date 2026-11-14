"""向量引擎的异常类型与响应解析。

拆出来供 embedding / Qdrant 两个客户端共用：客户端只依赖本模块，不反向引用
`vector_search` 门面，从而避免循环导入。`vector_search` 继续 re-export 这些
符号，既有 `from vector_search import ...` 的调用方无需改动。
"""

from __future__ import annotations

import json
import math


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

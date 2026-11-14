"""客户端边界单测：EmbeddingClient 与 QdrantClient（全部用假 HTTP，不触网）。

这些用例锁住拆分后的两个边界各自负责的事：
- EmbeddingClient：输入序列化（dataURL/base64）、/v1/embeddings 请求头与体、
  响应解析、非 JSON/HTTP 错误与重试口径；
- QdrantClient：points URL/请求头、query→search 回退、scroll/count、
  thumb/原图反查、upsert/delete。
门面 VectorEngine 的集成行为仍由 test_vector_search.py 覆盖。
"""

from __future__ import annotations

import aiohttp
import pytest

from astrbot_plugin_image_trace.search.embedding_client import EmbeddingClient
from astrbot_plugin_image_trace.search.errors import HttpError, VectorEngineError
from astrbot_plugin_image_trace.search.qdrant_client import QdrantClient

# ----------------------------------------------------------------------
# EmbeddingClient
# ----------------------------------------------------------------------


class _Body:
    """模拟 aiohttp StreamReader：按 read(n) 逐块消耗，空则 EOF。"""

    def __init__(self, text: str):
        self.data = text.encode()

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk, self.data = self.data, b""
        else:
            chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class FakeEmbedResponse:
    def __init__(self, status: int, body: str, headers: dict | None = None):
        self.status = status
        self.headers = {
            "Content-Length": str(len(body.encode())),
            "Content-Type": "application/json",
        }
        self.headers.update(headers or {})
        self.content = _Body(body)

    def get_encoding(self):
        return "utf-8"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class FakeHttpClient:
    """按顺序吐出预置响应，并记录每次请求的 method/url/kwargs。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def make_embedding_client(
    http_client: FakeHttpClient,
    *,
    image_input: str = "nemotron-vl",
    api_key: str = "secret",
) -> EmbeddingClient:
    async def get_client():
        return http_client

    return EmbeddingClient(
        base_url="https://embed.example.com/v1",
        api_key=api_key,
        model="image-model",
        image_input=image_input,
        timeout=aiohttp.ClientTimeout(total=7),
        http_getter=get_client,
    )


async def test_embedding_request_serializes_nemotron_dataurl():
    client = FakeHttpClient([FakeEmbedResponse(200, '{"data":[{"embedding":[0.5, 1]}]}')])
    embed = make_embedding_client(client)

    assert await embed.embed_bytes(b"\x00\x01", "image/png") == [0.5, 1.0]

    method, url, kwargs = client.calls[0]
    assert (method, url) == ("POST", "https://embed.example.com/v1/embeddings")
    prepared = kwargs["prepare"](url, method)
    assert prepared["json"] == {
        "model": "image-model",
        "input": "data:image/png;base64,AAE=",
        "encoding_format": "float",
        # 非对称模型：图片侧强制 passage
        "input_type": "passage",
    }
    assert prepared["headers"] == {"Authorization": "Bearer secret"}
    assert prepared["timeout"].total == 7
    # 跨域重定向到第三方时必须剥离鉴权头
    assert kwargs["prepare"](url, method, True)["headers"] == {}


def test_embedding_input_shapes_by_model_family():
    client = FakeHttpClient([])
    data_url, b64 = "data:image/jpeg;base64,QQ==", "QQ=="

    assert make_embedding_client(client, image_input="qwen-vl").build_input(
        data_url, b64
    ) == [{"type": "image", "image": data_url}]
    assert make_embedding_client(client, image_input="nemotron-vl").build_input(
        data_url, b64
    ) == data_url
    assert (
        make_embedding_client(client, image_input="dataurl").build_input(data_url, b64)
        == data_url
    )
    assert make_embedding_client(client, image_input="jina-image").build_input(
        data_url, b64
    ) == [{"image": b64}]
    with pytest.raises(VectorEngineError, match="不支持的 embed_image_input"):
        make_embedding_client(client, image_input="bogus").build_input(data_url, b64)


async def test_embedding_retries_invalid_json_once(monkeypatch):
    client = FakeHttpClient(
        [
            FakeEmbedResponse(200, "{invalid"),
            FakeEmbedResponse(200, '{"data":[{"embedding":[2]}]}'),
        ]
    )
    embed = make_embedding_client(client)
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(
        "astrbot_plugin_image_trace.search.embedding_client.asyncio.sleep", fake_sleep
    )

    assert await embed.embed_bytes(b"image", "image/png") == [2.0]
    assert len(client.calls) == 2
    assert delays == [2]


async def test_embedding_http_errors_surface_without_retry():
    bad_image = FakeHttpClient([FakeEmbedResponse(400, "cannot identify image file")])
    embed = make_embedding_client(bad_image)
    with pytest.raises(VectorEngineError, match="图片内容无效"):
        await embed.embed_bytes(b"image", "image/png")
    assert len(bad_image.calls) == 1

    limited = FakeHttpClient(
        [FakeEmbedResponse(429, "slow down", headers={"retry-after": "3"})]
    )
    embed = make_embedding_client(limited)
    with pytest.raises(VectorEngineError, match="约 3 秒后重试"):
        await embed.embed_bytes(b"image", "image/png")
    assert len(limited.calls) == 1


# ----------------------------------------------------------------------
# QdrantClient
# ----------------------------------------------------------------------


def make_qdrant(request_json, *, collection: str = "img bed", top_k: int = 5):
    async def embed_file(path, mime):
        return [0.2, 0.4]

    return QdrantClient(
        base_url="https://qdrant.example.com",
        api_key="secret",
        collection=collection,
        top_k=top_k,
        request_json=request_json,
        embed_file=embed_file,
    )


async def test_qdrant_search_uses_query_api_then_legacy_on_404():
    calls = []

    async def request_json(method, url, *, json_body=None, headers=None):
        calls.append((method, url, json_body, headers))
        if url.endswith("/query"):
            raise HttpError(404, "query endpoint missing")
        return {"result": [{"id": 7, "score": "0.9", "payload": {"image_url": "u"}}]}

    qd = make_qdrant(request_json)
    hits = await qd.search([0.1, 0.2], with_vector=True)

    assert calls[0][1] == "https://qdrant.example.com/collections/img%20bed/points/query"
    assert calls[0][2]["query"] == [0.1, 0.2]
    assert calls[0][2]["limit"] == 5
    assert calls[0][2]["with_vector"] is True
    assert calls[0][3] == {"api-key": "secret"}
    assert calls[1][1].endswith("/points/search")
    assert calls[1][2]["vector"] == [0.1, 0.2]
    assert hits == [
        {"id": "7", "score": 0.9, "payload": {"image_url": "u"}, "vector": None}
    ]


async def test_qdrant_search_rejects_invalid_vectors():
    async def request_json(method, url, *, json_body=None, headers=None):
        pytest.fail("无效向量不应发起请求")

    qd = make_qdrant(request_json)
    for vector in ([], [float("nan")], [float("inf")], ["1"]):
        with pytest.raises(VectorEngineError, match="查询向量无效"):
            await qd.search(vector)


async def test_qdrant_scroll_paginates_and_count_reads_result():
    pages = [
        {
            "result": {
                "points": [
                    {"id": "a", "payload": {"phash": "aa", "image_url": "u1"}},
                    {"id": "b", "payload": {"phash": "", "image_url": "u2"}},
                ],
                "next_page_offset": "b",
            }
        },
        {
            "result": {
                "points": [{"id": "c", "payload": {"phash": "cc", "image_url": "u3"}}],
                "next_page_offset": None,
            }
        },
    ]
    bodies = []

    async def request_json(method, url, *, json_body=None, headers=None):
        if url.endswith("/count"):
            return {"result": {"count": 7}}
        bodies.append(json_body)
        return pages.pop(0)

    qd = make_qdrant(request_json)

    assert await qd.count() == 7
    got = await qd.scroll_payloads(key="phash", page_size=2)

    assert [p["id"] for p in got] == ["a", "c"]
    assert bodies[0]["with_payload"] == ["phash"]
    assert "offset" not in bodies[0]
    assert bodies[1]["offset"] == "b"


async def test_qdrant_thumb_and_original_reverse_lookup():
    thumb = "https://img.example.com/file/thumbnails/1789660677557_x.jpg"
    stored = {
        "thumb_url": "http://10.0.0.5:7658/file/thumbnails/1789660677557_x.jpg",
        "image_url": "http://10.0.0.5:7658/file/12、日常分享/原图.jpg",
        "file_name": "原图.jpg",
    }
    filters = []

    async def request_json(method, url, *, json_body=None, headers=None):
        if url.endswith("/points"):
            return {"result": [{"id": "p", "payload": {"thumb_url": "https://thumb"}}]}
        if url.endswith("/count"):
            # 整串精确匹配（value）落空；按路径（text）唯一命中
            match = json_body["filter"]["must"][0]["match"]
            return {"result": {"count": 0 if "value" in match else 1}}
        filters.append(json_body)
        return {"result": {"points": [{"id": "p", "payload": stored}]}}

    qd = make_qdrant(request_json)

    assert await qd.thumb_url_for("orig-id") == "https://thumb"

    info = await qd.original_info_for_thumb(thumb)
    assert info == {"url": stored["image_url"], "file_name": "原图.jpg"}
    # 第一轮整串精确匹配用 value，第二轮按路径用 text（不含 host）
    assert filters[0]["filter"]["must"][0]["match"]["text"].startswith("thumbnails/")
    assert "img.example.com" not in filters[0]["filter"]["must"][0]["match"]["text"]

    async def name_lookup(method, url, *, json_body=None, headers=None):
        if url.endswith("/count"):
            return {"result": {"count": 1}}
        return {"result": {"points": [{"id": "p2", "payload": stored}]}}

    by_name = make_qdrant(name_lookup)
    assert await by_name.original_by_file_name("原图.jpg") == {
        "url": stored["image_url"],
        "file_name": "原图.jpg",
    }


async def test_qdrant_thumb_name_uses_injected_scroll_filter():
    seen = []

    async def scroll_filter(query_filter, label, limit=1):
        seen.append((query_filter, label, limit))
        return [
            {
                "thumb_url": (
                    "http://bed/file/thumbnails/"
                    "1789658616018_【微博_赵今麦工作室official】剧照.jpg"
                ),
                "image_url": "http://bed/file/12、日常分享/【微博@赵今麦工作室official】剧照.jpg",
                "file_name": "【微博@赵今麦工作室official】剧照.jpg",
            }
        ]

    async def request_json(method, url, *, json_body=None, headers=None):
        pytest.fail("注入了 scroll_filter 时不应自己发请求")

    qd = make_qdrant(request_json)
    info = await qd.original_info_for_thumb_name(
        "【微博_赵今麦工作室official】剧照.jpg", scroll_filter=scroll_filter
    )

    assert info is not None
    assert info["file_name"] == "【微博@赵今麦工作室official】剧照.jpg"
    assert seen[0][1] == "缩略图名 【微博_赵今麦工作室official】剧照.jpg"
    assert seen[0][2] == 20


async def test_qdrant_upsert_and_delete_point():
    calls = []

    async def request_json(method, url, *, json_body=None, headers=None):
        calls.append((method, url, json_body, headers))
        return {"result": {}}

    qd = make_qdrant(request_json)

    await qd.upsert_file(
        "image.jpg",
        "phash:00",
        "https://img.example.com/a%20b.jpg",
        123,
        10,
        20,
        src="/a b.jpg",
    )
    await qd.delete_point("phash:00")

    assert calls[0][0] == "PUT"
    assert calls[0][1] == "https://qdrant.example.com/collections/img%20bed/points?wait=true"
    point = calls[0][2]["points"][0]
    assert point["id"] == qd.point_id_for("phash:00")
    assert point["vector"] == [0.2, 0.4]
    # file_name 必须是解码后的真名，否则 /原图 按中文名精确反查会落空
    assert point["payload"]["file_name"] == "a b.jpg"
    assert point["payload"]["mime"] == "image/jpeg"
    assert calls[0][3] == {"api-key": "secret"}

    assert calls[1][0] == "POST"
    assert calls[1][1].endswith("/collections/img%20bed/points/delete?wait=true")
    assert calls[1][2] == {"points": [qd.point_id_for("phash:00")]}


async def test_qdrant_delete_tolerates_missing_collection():
    async def request_json(method, url, *, json_body=None, headers=None):
        raise HttpError(404, "collection not found")

    qd = make_qdrant(request_json)
    await qd.delete_point("phash:00")

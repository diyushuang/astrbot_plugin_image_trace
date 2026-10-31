import json

import pytest
from PIL import Image

from astrbot_plugin_image_trace.vector_search import (
    HttpError,
    InvalidEmbeddingJSON,
    VectorEngine,
    VectorEngineError,
    parse_embedding_response,
    thumb_path_key,
)


def make_engine(collection: str = "imgbed_images") -> VectorEngine:
    async def get_client():
        raise AssertionError("HTTP client should not be used in these tests")

    return VectorEngine(
        {
            "vector_search": {
                "qdrant_url": "https://qdrant.example.com",
                "qdrant_api_key": "secret",
                "collection_name": collection,
                "embed_base_url": "https://embed.example.com/v1",
                "embed_model": "image-model",
            }
        },
        get_client,
    )


async def test_search_uses_query_api_and_parses_points():
    engine = make_engine()
    calls = []

    async def request_json(method, url, *, json_body=None, headers=None):
        calls.append((method, url, json_body, headers))
        return {
            "result": {
                "points": [{"id": 1, "score": 0.92, "payload": {"image_url": "https://img"}}]
            }
        }

    engine._request_json = request_json
    hits = await engine.search([0.1, 0.2], limit=2)

    assert len(calls) == 1
    method, url, body, headers = calls[0]
    assert method == "POST"
    assert url == "https://qdrant.example.com/collections/imgbed_images/points/query"
    assert body == {"query": [0.1, 0.2], "limit": 2, "with_payload": True, "with_vector": False}
    assert headers == {"api-key": "secret"}
    assert hits[0]["id"] == "1"
    assert hits[0]["score"] == pytest.approx(0.92)


async def test_search_falls_back_to_legacy_api_on_404():
    engine = make_engine()
    calls = []

    async def request_json(method, url, *, json_body=None, headers=None):
        calls.append((url, json_body))
        if url.endswith("/points/query"):
            raise HttpError(404, "query endpoint not found")
        return {"result": [{"id": 2, "score": 0.88, "payload": {}}]}

    engine._request_json = request_json
    hits = await engine.search([1.0])

    assert len(calls) == 2
    assert calls[0][0].endswith("/points/query")
    assert calls[0][1]["query"] == [1.0]
    assert calls[1][0].endswith("/points/search")
    assert calls[1][1]["vector"] == [1.0]
    assert hits[0]["id"] == "2"


async def test_search_can_return_candidate_vectors_for_duplicate_clustering():
    engine = make_engine()
    calls = []

    async def request_json(method, url, *, json_body=None, headers=None):
        calls.append(json_body)
        return {
            "result": {
                "points": [
                    {
                        "id": 1,
                        "score": 0.92,
                        "vector": [0.1, 0.2],
                        "payload": {"image_url": "https://img"},
                    }
                ]
            }
        }

    engine._request_json = request_json
    hits = await engine.search([0.1, 0.2], with_vector=True)

    assert calls == [{"query": [0.1, 0.2], "limit": 5, "with_payload": True, "with_vector": True}]
    assert hits[0]["vector"] == [0.1, 0.2]


async def test_search_raises_when_both_apis_are_missing():
    engine = make_engine()

    async def request_json(method, url, *, json_body=None, headers=None):
        raise HttpError(404, "collection not found")

    engine._request_json = request_json
    with pytest.raises(HttpError):
        await engine.search([1.0])


async def test_search_rejects_invalid_vectors():
    engine = make_engine()
    for vector in ([], [float("nan")], [float("inf")], ["1"]):
        with pytest.raises(VectorEngineError):
            await engine.search(vector)


async def test_collection_name_is_url_encoded():
    engine = make_engine(collection="my collection/?")
    calls = []

    async def request_json(method, url, *, json_body=None, headers=None):
        calls.append(url)
        return {"result": {"points": []}}

    engine._request_json = request_json
    await engine.search([1.0])
    assert calls == ["https://qdrant.example.com/collections/my%20collection%2F%3F/points/query"]


async def test_count_upsert_and_delete_use_encoded_collection_and_uuid():
    engine = make_engine(collection="img bed")
    calls = []

    async def request_json(method, url, *, json_body=None, headers=None):
        calls.append((method, url, json_body))
        return {"result": {"count": 7}}

    async def embed_file(path):
        return [0.2, 0.4]

    engine._request_json = request_json
    engine.embed_file = embed_file

    assert await engine.count() == 7
    await engine.upsert_file(
        "image.jpg",
        "phash:00",
        "https://img.example.com/a%20b.jpg",
        123,
        10,
        20,
        src="/a b.jpg",
    )
    await engine.delete_point("phash:00")

    assert calls[0][1].endswith("/collections/img%20bed/points/count")
    assert calls[1][0] == "PUT"
    assert calls[1][1].endswith("/collections/img%20bed/points?wait=true")
    assert calls[1][2]["points"][0]["id"] == engine.point_id_for("phash:00")
    assert calls[1][2]["points"][0]["vector"] == [0.2, 0.4]
    # file_name 必须是解码后的真名，否则 /原图 按中文名精确反查会落空
    assert calls[1][2]["points"][0]["payload"]["file_name"] == "a b.jpg"
    assert calls[2][1].endswith("/collections/img%20bed/points/delete?wait=true")
    assert calls[2][2] == {"points": [engine.point_id_for("phash:00")]}


def test_invalid_base_urls_disable_engine_and_provide_safe_hint():
    async def get_client():
        raise AssertionError

    engine = VectorEngine(
        {
            "vector_search": {
                "qdrant_url": "ftp://qdrant.example.com",
                "embed_base_url": "qdrant.example.com",
                "embed_model": "model",
            }
        },
        get_client,
    )
    assert engine.enabled is False
    missing = engine.missing_fields()
    assert any("http/https" in item for item in missing)


def test_embed_image_input_defaults_and_invalid_fallback():
    # v1.3.7：默认与非法值回退均为 nemotron-vl——NVIDIA llama-nemotron-embed-vl
    # 系只接受裸 dataURL，旧默认 qwen-vl 的 content 数组会让服务端
    # .strip() 遇字典崩溃（HTTP 500 'dict' object has no attribute 'strip'）
    engine = make_engine()
    assert engine.image_input == "nemotron-vl"

    async def get_client():
        raise AssertionError("HTTP client should not be used in these tests")

    engine = VectorEngine(
        {
            "vector_search": {
                "qdrant_url": "https://qdrant.example.com",
                "embed_base_url": "https://embed.example.com/v1",
                "embed_model": "model",
                "embed_image_input": "bogus",
            }
        },
        get_client,
    )
    assert engine.image_input == "nemotron-vl"


def test_parse_embedding_response_validates_schema_and_values():
    assert parse_embedding_response('{"data":[{"embedding":[0.1, 2]}]}') == [0.1, 2.0]
    for body in (
        '{"data":[]}',
        '{"data":[{"embedding":[]}]}',
        '{"data":[{"embedding":["1"]}]}',
        '{"data":[{"embedding":[NaN]}]}',
        '{"data":[{"embedding":[Infinity]}]}',
    ):
        with pytest.raises(ValueError):
            parse_embedding_response(body)

    with pytest.raises(InvalidEmbeddingJSON):
        parse_embedding_response('{"data":[{"embedding":[1]}')


class FakeEmbedContent:
    """模拟 aiohttp StreamReader：读后消耗，读尽后返回 EOF。"""

    def __init__(self, body: str):
        self.body = body.encode()

    async def read(self, size: int = -1) -> bytes:
        if not self.body:
            return b""
        chunk = self.body if size < 0 else self.body[:size]
        self.body = self.body[len(chunk):]
        return chunk


class ChunkedEmbedContent:
    """模拟分块到达：read(n) 每次只返回一个已到达的网络分块，可能远小于 n。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def read(self, size: int = -1) -> bytes:
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


class FakeEmbedResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self.headers = {
            "Content-Length": str(len(body.encode())),
            "Content-Type": "application/json",
        }
        self.content = FakeEmbedContent(body)

    def get_encoding(self):
        return "utf-8"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class FakeEmbedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def use_embed_responses(engine, responses, monkeypatch):
    client = FakeEmbedClient(responses)
    delays = []

    async def get_client():
        return client

    async def fake_sleep(delay):
        delays.append(delay)

    engine._http_getter = get_client
    monkeypatch.setattr("astrbot_plugin_image_trace.vector_search.asyncio.sleep", fake_sleep)
    return client, delays


async def test_embed_file_uses_real_mime_and_rejects_invalid_images(tmp_path):
    engine = make_engine()
    valid = tmp_path / "png-as-jpg.jpg"
    Image.new("RGB", (2, 2)).save(valid, format="PNG")
    invalid = tmp_path / "invalid.jpg"
    invalid.write_bytes(b"not an image")
    calls = []

    async def embed_bytes(data, mime):
        calls.append((data, mime))
        return [0.1]

    engine.embed_bytes = embed_bytes
    assert await engine.embed_file(valid) == [0.1]
    assert calls[0][1] == "image/png"

    with pytest.raises(VectorEngineError, match="图片内容无效"):
        await engine.embed_file(invalid)
    assert len(calls) == 1


async def test_embed_bytes_reads_chunked_response_body(monkeypatch):
    # 线上故障回归：embedding 响应 39841 字节分块到达，单次 read 只拿到
    # 当前缓冲的 3893 字节就去解析 JSON。read_limited_bytes 必须循环读到
    # EOF 才能拿到完整响应体
    engine = make_engine()
    vector = [round(i * 0.001, 9) for i in range(2000)]
    body = json.dumps({"data": [{"embedding": vector}]}).encode()
    assert len(body) > 3893 * 2
    response = FakeEmbedResponse(200, body.decode())
    response.content = ChunkedEmbedContent([body[:3893], body[3893:]])
    client, delays = use_embed_responses(engine, [response], monkeypatch)

    assert await engine.embed_bytes(b"image", "image/png") == vector
    assert len(client.calls) == 1
    assert delays == []


async def test_invalid_embedding_json_retries_once_and_recovers(monkeypatch, caplog):
    engine = make_engine()
    bad_body = '{"data":[{"embedding":[1]},]}'
    # JSONDecodeError.pos 的取值随 Python 版本变化（3.14 与 3.10/3.12 差一位），
    # 断言不能写死；按当前解释器实际解析结果推导期望值。
    expected_pos = 0
    try:
        json.loads(bad_body)
    except json.JSONDecodeError as exc:
        expected_pos = exc.pos
    client, delays = use_embed_responses(
        engine,
        [
            FakeEmbedResponse(200, bad_body),
            FakeEmbedResponse(200, '{"data":[{"embedding":[1, 2]}]}'),
        ],
        monkeypatch,
    )

    with caplog.at_level("ERROR", logger="vector_search"):
        assert await engine.embed_bytes(b"image", "image/png") == [1.0, 2.0]

    assert len(client.calls) == 2
    assert delays == [2]
    assert "Content-Length=29" in caplog.text
    assert f"解析位置={expected_pos}" in caplog.text


async def test_invalid_embedding_json_has_one_retry_limit(monkeypatch):
    engine = make_engine()
    client, delays = use_embed_responses(
        engine,
        [
            FakeEmbedResponse(200, "{invalid"),
            FakeEmbedResponse(200, "{invalid"),
        ],
        monkeypatch,
    )

    with pytest.raises(VectorEngineError, match="向量服务返回无效 JSON（已重试一次）"):
        await engine.embed_bytes(b"image", "image/png")
    assert len(client.calls) == 2
    assert delays == [2]


async def test_5xx_and_invalid_json_share_two_request_limit(monkeypatch):
    engine = make_engine()
    client, delays = use_embed_responses(
        engine,
        [
            FakeEmbedResponse(502, "temporary"),
            FakeEmbedResponse(200, "{invalid"),
        ],
        monkeypatch,
    )

    with pytest.raises(VectorEngineError, match="向量服务返回无效 JSON（已重试一次）"):
        await engine.embed_bytes(b"image", "image/png")
    assert len(client.calls) == 2
    assert delays == [2]


async def test_schema_and_nonfinite_errors_do_not_retry(monkeypatch):
    engine = make_engine()
    client, delays = use_embed_responses(
        engine,
        [FakeEmbedResponse(200, '{"data":[{"embedding":[NaN]}]}')],
        monkeypatch,
    )

    with pytest.raises(VectorEngineError, match="向量服务响应解析失败"):
        await engine.embed_bytes(b"image", "image/png")
    assert len(client.calls) == 1
    assert delays == []


# ----------------------------------------------------------------------
# thumb_url → image_url 反查（/原图 命中缩略图时的纠正通路）
# ----------------------------------------------------------------------


async def test_original_url_for_thumb_matches_payload_and_returns_image_url():
    """按 thumb_url 精确匹配取回同点的 image_url——反查的唯一可行手段。

    不能按文件名倒推点 id：缩略图被图床改过名（前置毫秒时间戳、`@` 换 `_`），
    UUID5 那条路是死的。所以必须走 payload 过滤。
    """
    engine = make_engine()
    calls = []
    thumb = "http://bed/file/thumbnails/1789658616018_x.jpg"

    async def request_json(method, url, *, json_body=None, headers=None):
        calls.append((url, json_body))
        if url.endswith("/count"):
            return {"result": {"count": 1}}
        return {
            "result": {
                "points": [
                    {"id": "uuid-1", "payload": {"thumb_url": thumb, "image_url": "http://bed/file/x.jpg"}}
                ]
            }
        }

    engine._request_json = request_json
    assert await engine.original_url_for_thumb(thumb) == "http://bed/file/x.jpg"

    # 两次请求必须用**同一个** filter，否则 count 与 scroll 语义不一致
    assert len(calls) == 2
    count_filter = calls[0][1]["filter"]
    scroll_filter = calls[1][1]["filter"]
    assert count_filter == scroll_filter == {
        "must": [{"key": "thumb_url", "match": {"value": thumb}}]
    }
    assert calls[0][1]["exact"] is True


async def test_original_url_for_thumb_falls_back_to_path_when_host_differs():
    """host 不同时，整串精确匹配必然落空，必须退回「按对象键」匹配。

    线上真实故障：payload 记的是内网 `192.9.240.227:7658`，插件手上的直链来自
    公网反代 `img.dixc.de`（同一图床）。此时 count(精确) = 0，若不退回按路径
    匹配，`/原图` 就会报「找不到」——v1.5.9 首次上线即如此。
    """
    engine = make_engine()
    filters = []

    async def request_json(method, url, *, json_body=None, headers=None):
        filters.append((url, json_body.get("filter")))
        # 精确匹配落空；按路径（match.text）命中
        if "match" in json_body["filter"]["must"][0] and "value" in json_body["filter"]["must"][0]["match"]:
            return {"result": {"count": 0}}
        if url.endswith("/count"):
            return {"result": {"count": 1}}
        return {
            "result": {
                "points": [
                    {
                        "id": "uuid-1",
                        "payload": {
                            "thumb_url": "http://192.9.240.227:7658/file/thumbnails/1789660677557_【微博_兰蔻LANCOME】20230507-04：郑州线下活动.jpg",
                            "image_url": "http://192.9.240.227:7658/file/8%E3%80%81…/orig.jpg",
                            "file_name": "【微博@兰蔻LANCOME】20230507-04：郑州线下活动.jpg",
                        },
                    }
                ]
            }
        }

    engine._request_json = request_json
    # 插件手上的是公网域名形态（与 payload 的 host 不同）
    public_thumb = (
        "https://img.dixc.de/file/thumbnails/"
        "1789660677557_%E3%80%90%E5%BE%AE%E5%8D%9A_%E5%85%B0%E8%94%BBLANCOME"
        "%E3%80%9120230507-04%EF%BC%9A%E9%83%91%E5%B7%9E%E7%BA%BF%E4%B8%8B%E6%B4%BB%E5%8A%A8.jpg"
    )
    info = await engine.original_info_for_thumb(public_thumb)
    assert info is not None
    assert info["url"].endswith("orig.jpg")
    assert info["file_name"] == "【微博@兰蔻LANCOME】20230507-04：郑州线下活动.jpg"

    # 第二次尝试必须是按路径匹配（match.text，且不含 host）
    text_filter = filters[2][1]
    matched = text_filter["must"][0]["match"]
    assert "text" in matched, "host 不同时必须退回按路径匹配"
    assert "img.dixc.de" not in matched["text"]
    assert matched["text"].startswith("thumbnails/")


async def test_original_url_for_thumb_returns_none_when_not_unique():
    """命中数不为 1 一律放弃反查：0 是「没有替身」，>1 是数据异常。

    >1 时绝不能赌一个：赌错就会把**别的图**的原图发给用户，比发缩略图更糟。
    """
    engine = make_engine()

    async def count_two(method, url, *, json_body=None, headers=None):
        return {"result": {"count": 2}}

    engine._request_json = count_two
    assert await engine.original_url_for_thumb("http://bed/file/thumbnails/1_x.jpg") is None

    async def count_zero(method, url, *, json_body=None, headers=None):
        return {"result": {"count": 0}}

    engine._request_json = count_zero
    assert await engine.original_url_for_thumb("http://bed/file/thumbnails/1_x.jpg") is None


async def test_original_url_for_thumb_never_raises():
    """网络/鉴权/集合缺失一律按「查不到」处理：反查只是纠正手段，不能阻断 /原图。"""
    engine = make_engine()

    async def boom(method, url, *, json_body=None, headers=None):
        raise HttpError(500, "boom")

    engine._request_json = boom
    assert await engine.original_url_for_thumb("http://bed/file/thumbnails/1_x.jpg") is None


async def test_original_url_for_thumb_skips_work_when_disabled_or_blank():
    """空入参 / Qdrant 未配置时直接返回 None，一个请求都不发。

    enabled（完整向量检索）与反查通道自 1.7.3 起解耦：只缺 embed 时
    enabled=False 但反查仍可用，故这里钉死的是 qdrant_ready=False 的场景。
    """
    engine = make_engine()
    engine._request_json = lambda *a, **k: pytest.fail("不应发起请求")

    assert await engine.original_url_for_thumb("") is None
    assert await engine.original_url_for_thumb(None) is None

    engine.qdrant_ready = False
    assert await engine.original_url_for_thumb("http://bed/file/thumbnails/1_x.jpg") is None


# ----------------------------------------------------------------------
# file_name → image_url 反查（/原图 <文件名> 拿不到目录时的纠正通路）
# ----------------------------------------------------------------------


async def test_original_by_file_name_returns_payload_image_url_with_full_path():
    """按裸文件名反查，取回**带完整目录**的权威直链。

    线上真实故障：`/原图 【微博@潮流合伙人】20200207-04：赵今麦海报.jpg` 报
    「图床里没有找到」。因为插件把裸名直接拼成 `/file/{裸名}`，而图床的
    `/file/{path}` **只认完整对象键**，裸名必然 404（实测 A/B：裸名 404、
    带目录 200，内网与公网域名结果一致）。payload.image_url 才是入库时登记的
    那条带目录直链，故只能反查。
    """
    engine = make_engine()
    calls = []
    name = "【微博@潮流合伙人】20200207-04：赵今麦海报.jpg"
    with_path = (
        "http://192.9.240.227:7658/file/7%E3%80%81%E7%BB%BC%E8%89%BA%E8%8A%82%E7%9B%AE/"
        "%E7%88%B1%E5%A5%87%E8%89%BA%E3%80%8A%E6%BD%AE%E6%B5%81%E5%90%88%E4%BC%99%E4%BA%BA"
        "%E3%80%8B/%E3%80%8A%E6%BD%AE%E6%B5%81%E5%90%88%E4%BC%99%E4%BA%BA%E3%80%8B%E5%9B%BE"
        "%E9%9B%86/%E3%80%90%E5%BE%AE%E5%8D%9A%40%E6%BD%AE%E6%B5%81%E5%90%88%E4%BC%99%E4%BA%BA"
        "%E3%80%9120200207-04%EF%BC%9A%E8%B5%B5%E4%BB%8A%E9%BA%A6%E6%B5%B7%E6%8A%A5.jpg"
    )

    async def request_json(method, url, *, json_body=None, headers=None):
        calls.append((url, json_body))
        if url.endswith("/count"):
            return {"result": {"count": 1}}
        return {
            "result": {
                "points": [
                    {
                        "id": "918570e3-e077-5b32-a1e8-336ab7a70bce",
                        "payload": {
                            "file_name": name,
                            "image_url": with_path,
                            "src": "/file/7、综艺节目/…/" + name,
                        },
                    }
                ]
            }
        }

    engine._request_json = request_json
    info = await engine.original_by_file_name(name)
    assert info is not None
    assert info["url"] == with_path, "必须返回带完整目录的直链，而不是裸名的拼法"
    assert info["file_name"] == name

    # 必须按**值**精确匹配：file_name 含 @ 与中文冒号，用 match.text 会被切成词
    count_filter = calls[0][1]["filter"]
    assert count_filter == {"must": [{"key": "file_name", "match": {"value": name}}]}
    assert calls[0][1]["exact"] is True
    assert count_filter == calls[1][1]["filter"]


def _name_lookup_stub(payloads, *, count=None):
    """造一个按 file_name 反查的假 Qdrant：/count 返回命中数，/scroll 返回 payload。"""
    hits = len(payloads) if count is None else count

    async def request_json(method, url, *, json_body=None, headers=None):
        if url.endswith("/count"):
            return {"result": {"count": hits}}
        return {
            "result": {
                "points": [{"id": f"p{i}", "payload": p} for i, p in enumerate(payloads)]
            }
        }

    return request_json


async def test_original_by_file_name_returns_none_when_absent():
    """库里没有这个名字（命中 0）时返回 None，交给调用方走兜底。

    与旧用例的分工：旧版把「0 条」和「>1 条」都归为 None；>1 已按下面的
    等价性判据分别处理，这个用例现在只负责「确实 0 条」。
    """
    engine = make_engine()
    engine._request_json = _name_lookup_stub([], count=0)
    assert await engine.original_by_file_name("x.jpg") is None


async def test_original_by_file_name_resolves_duplicates_when_content_identical():
    """同名多份但**内容一致**时，确定性地取一份返回。

    线上真实故障：`/原图 【微博@YOUKESHU有棵树】20240308-05：抖音直播.jpg` 报
    「图床里没有找到」。该名字在 Qdrant 精确命中 **2 个点**（同一张图被放进两个
    相册目录），而旧实现要求命中数**恰好为 1**，于是主动放弃反查 → 退回裸名拼
    URL → 必然 404。实测这两个点 sha256 完全相同（3,594,079 字节），任取一份
    发给用户拿到的都是同一张图。

    本部署 23,932 点里 6,127 个文件名（37.3%）是重名的，故这条规则直接决定
    这些名字能不能被 /原图 取到。
    """
    engine = make_engine()
    name = "【微博@YOUKESHU有棵树】20240308-05：抖音直播.jpg"
    base = "http://192.9.240.227:7658/file/"
    first = {
        "file_name": name,
        "size_bytes": 3.43,
        "mime": "image/jpeg",
        "src": "/file/10、商务宣传/有棵树/有棵树抖音直播（20240304）/" + name,
        "image_url": base + "10%E3%80%81%E5%95%86%E5%8A%A1%E5%AE%A3%E4%BC%A0.jpg",
    }
    second = {
        "file_name": name,
        "size_bytes": 3.43,
        "mime": "image/jpeg",
        "src": "/file/9、访谈直播/【78】有棵树抖音直播/" + name,
        "image_url": base + "9%E3%80%81%E8%AE%BF%E8%B0%88%E7%9B%B4%E6%92%AD.jpg",
    }

    engine._request_json = _name_lookup_stub([first, second])
    info = await engine.original_by_file_name(name)
    assert info is not None, "同名但内容一致时必须取到一份，不能再报「找不到」"
    assert info["file_name"] == name
    # 确定性：按 src **字典序**取最小。注意是字典序不是数字序——'1' < '9'，
    # 所以 `/file/10、…` 排在 `/file/9、…` 之前。
    assert "10%E3%80%81" in info["url"]
    picked = info["url"]

    # Qdrant scroll 的返回顺序不保证稳定，故换一个顺序再查一次，结果必须相同，
    # 否则同一条命令两次会给出不同目录的直链，看起来像数据在变。
    engine._request_json = _name_lookup_stub([second, first])
    assert (await engine.original_by_file_name(name))["url"] == picked


async def test_original_by_file_name_gives_up_when_duplicates_differ_in_size():
    """同名但大小不同 → 确认不是同一张图，放弃。

    这是必须保留的安全边界：赌错会把**别的图**当原图发出去，比说「找不到」更糟。
    """
    engine = make_engine()
    engine._request_json = _name_lookup_stub(
        [
            {"file_name": "x.jpg", "size_bytes": 1.2, "image_url": "http://bed/file/a/x.jpg"},
            {"file_name": "x.jpg", "size_bytes": 3.4, "image_url": "http://bed/file/b/x.jpg"},
        ]
    )
    assert await engine.original_by_file_name("x.jpg") is None


async def test_original_by_file_name_gives_up_when_phash_conflicts():
    """大小相同但 phash 不同 → 放弃。phash 是比大小更硬的逐位证据，能否决巧合。"""
    engine = make_engine()
    engine._request_json = _name_lookup_stub(
        [
            {
                "file_name": "x.jpg",
                "size_bytes": 1.2,
                "phash": "aa" * 32,
                "image_url": "http://bed/file/a/x.jpg",
            },
            {
                "file_name": "x.jpg",
                "size_bytes": 1.2,
                "phash": "bb" * 32,
                "image_url": "http://bed/file/b/x.jpg",
            },
        ]
    )
    assert await engine.original_by_file_name("x.jpg") is None


async def test_original_by_file_name_gives_up_when_size_missing():
    """缺 size_bytes 就无从判定是否同一张图 → 一律当不等价，绝不赌。"""
    engine = make_engine()
    engine._request_json = _name_lookup_stub(
        [
            {"file_name": "x.jpg", "image_url": "http://bed/file/a/x.jpg"},
            {"file_name": "x.jpg", "image_url": "http://bed/file/b/x.jpg"},
        ]
    )
    assert await engine.original_by_file_name("x.jpg") is None


async def test_original_by_file_name_gives_up_when_mime_differs():
    """同名同大小但格式不同（png/jpeg）→ 放弃。"""
    engine = make_engine()
    engine._request_json = _name_lookup_stub(
        [
            {
                "file_name": "x.jpg",
                "size_bytes": 1.2,
                "mime": "image/jpeg",
                "image_url": "http://bed/file/a/x.jpg",
            },
            {
                "file_name": "x.jpg",
                "size_bytes": 1.2,
                "mime": "image/png",
                "image_url": "http://bed/file/b/x.jpg",
            },
        ]
    )
    assert await engine.original_by_file_name("x.jpg") is None


async def test_original_by_file_name_returns_none_when_scroll_yields_nothing():
    """count 说有 2 条，但 scroll 返回空（分页边界/删除竞态）→ 按查不到处理。"""
    engine = make_engine()
    engine._request_json = _name_lookup_stub([], count=2)
    assert await engine.original_by_file_name("x.jpg") is None


async def test_file_name_matches_distinguishes_zero_and_error():
    """诊断用命中数：0 条返回 0（明确），查询失败返回 None（说不准）。"""
    engine = make_engine()
    engine._request_json = _name_lookup_stub([], count=0)
    assert await engine.file_name_matches("x.jpg") == 0

    async def boom(method, url, *, json_body=None, headers=None):
        raise HttpError(500, "boom")

    engine._request_json = boom
    assert await engine.file_name_matches("x.jpg") is None


async def test_original_by_file_name_returns_none_when_payload_lacks_image_url():
    """payload 缺 image_url 时返回 None，不能返一条空直链让调用方误以为命中了。"""
    engine = make_engine()

    async def request_json(method, url, *, json_body=None, headers=None):
        if url.endswith("/count"):
            return {"result": {"count": 1}}
        return {"result": {"points": [{"id": "u1", "payload": {"file_name": "x.jpg"}}]}}

    engine._request_json = request_json
    assert await engine.original_by_file_name("x.jpg") is None


async def test_original_by_file_name_never_raises():
    """网络/鉴权/集合缺失一律按「查不到」处理：反查只是取目录的捷径，不能阻断 /原图。"""
    engine = make_engine()

    async def boom(method, url, *, json_body=None, headers=None):
        raise HttpError(500, "boom")

    engine._request_json = boom
    assert await engine.original_by_file_name("x.jpg") is None


async def test_original_by_file_name_skips_work_when_disabled_or_blank():
    """空入参 / Qdrant 未配置时直接返回 None，一个请求都不发（1.7.3 起反查只看 qdrant_ready）。"""
    engine = make_engine()
    engine._request_json = lambda *a, **k: pytest.fail("不应发起请求")

    assert await engine.original_by_file_name("") is None
    assert await engine.original_by_file_name(None) is None

    engine.qdrant_ready = False
    assert await engine.original_by_file_name("x.jpg") is None


# ----------------------------------------------------------------------
# thumb_path_key：反查的 host 无关判据
# ----------------------------------------------------------------------


def test_thumb_path_key_strips_host_and_scheme():
    """对象键只取 `/file/` 之后的部分，host/scheme 一律丢弃。

    同一张图在内网 IP 与公网反代下必须得到同一个键——这正是反查能跨 host
    命中的前提。
    """
    key = "thumbnails/1789660677557_【微博_兰蔻LANCOME】20230507-04：郑州线下活动.jpg"
    internal = f"http://192.9.240.227:7658/file/{key}"
    public = f"https://img.dixc.de/file/{key}"
    assert thumb_path_key(internal) == key
    assert thumb_path_key(public) == key
    assert thumb_path_key(internal) == thumb_path_key(public)


def test_thumb_path_key_decodes_percent_encoding():
    """库内存未编码串、插件手上多为已编码串，必须先 unquote 才能对上。"""
    key = "thumbnails/1789660677557_【微博_兰蔻LANCOME】20230507-04：郑州线下活动.jpg"
    encoded = (
        "https://img.dixc.de/file/thumbnails/"
        "1789660677557_%E3%80%90%E5%BE%AE%E5%8D%9A_%E5%85%B0%E8%94%BBLANCOME"
        "%E3%80%9120230507-04%EF%BC%9A%E9%83%91%E5%B7%9E%E7%BA%BF%E4%B8%8B%E6%B4%BB%E5%8A%A8.jpg"
    )
    assert thumb_path_key(encoded) == key


def test_thumb_path_key_falls_back_to_full_path_without_file_segment():
    """没有 `/file/` 段时退回整条路径，保证对非常规直链也可用。"""
    assert thumb_path_key("http://bed/thumbnails/1_x.jpg") == "thumbnails/1_x.jpg"
    assert thumb_path_key("thumbnails/1_x.jpg") == "thumbnails/1_x.jpg"


def test_thumb_path_key_handles_garbage_without_raising():
    """空值/畸形输入返回空串，绝不抛错——反查链路不能因入参异常而中断。"""
    assert thumb_path_key("") == ""
    assert thumb_path_key(None) == ""
    assert thumb_path_key("http://") == ""
    assert thumb_path_key("////") == ""


# ----------------------------------------------------------------------
# 反查通道的启用判定与可观测性（1.7.3，2026-10-20 线上排查实录）
# ----------------------------------------------------------------------


def make_engine_without_embed(collection: str = "imgbed_images") -> VectorEngine:
    """只配 Qdrant、缺 embed 的引擎——线上真实故障形态。

    2026-10-20 排查：另一台 AstrBot 实例（v4.29.1）随机图命中缩略图后反查
    全部失败。当时的判定 `enabled = qdrant_url and embed_base_url and
    embed_model` 把「反查」与「以图搜图」绑死，缺 embed 时整条反查通道被
    一刀切静默禁用——而反查只查 Qdrant payload，根本不碰 embed。
    """
    async def get_client():
        raise AssertionError("HTTP client should not be used in these tests")

    return VectorEngine(
        {
            "vector_search": {
                "qdrant_url": "https://qdrant.example.com",
                "qdrant_api_key": "secret",
                "collection_name": collection,
            }
        },
        get_client,
    )


async def test_reverse_lookup_works_without_embed_config():
    """缺 embed 配置时 enabled=False，但反查通道必须仍然可用。"""
    engine = make_engine_without_embed()
    assert engine.enabled is False
    assert engine.qdrant_ready is True

    thumb = "https://img.dixc.de/file/thumbnails/1789666357268_x.jpg"

    async def request_json(method, url, *, json_body=None, headers=None):
        if url.endswith("/count"):
            # 整串精确匹配（match.value）落空——host 与库内不同；按路径
            # 匹配（match.text）唯一命中
            match = json_body["filter"]["must"][0]["match"]
            return {"result": {"count": 0 if "value" in match else 1}}
        return {
            "result": {
                "points": [
                    {
                        "id": "uuid-1",
                        "payload": {
                            "thumb_url": (
                                "http://192.9.240.227:7658/file/thumbnails/"
                                "1789666357268_x.jpg"
                            ),
                            "image_url": "http://192.9.240.227:7658/file/12、日常分享/原图.jpg",
                            "file_name": "【小红书@赵今麦】20231110-02：MiuMiu假日派对.jpg",
                        },
                    }
                ]
            }
        }

    engine._request_json = request_json
    info = await engine.original_info_for_thumb(thumb)
    assert info is not None
    assert info["url"].endswith("原图.jpg")
    assert info["file_name"] == "【小红书@赵今麦】20231110-02：MiuMiu假日派对.jpg"

    # 按文件名反查同样只看 qdrant_ready
    async def count_one(method, url, *, json_body=None, headers=None):
        if url.endswith("/count"):
            return {"result": {"count": 1}}
        return {"result": {"points": [{"payload": {"image_url": "http://bed/file/a.jpg"}}]}}

    engine._request_json = count_one
    assert await engine.original_by_file_name("原图.jpg") is not None


async def test_reverse_lookup_without_qdrant_returns_none_with_debug_log(caplog):
    """Qdrant 未配置时返回 None，且必须留下「为什么没查」的 DEBUG 记录。

    静默失败曾是排查的最大障碍：warning 只说「反查不到」，根因无处可查。
    """
    async def get_client():
        raise AssertionError("HTTP client should not be used in these tests")

    engine = VectorEngine(
        {
            "vector_search": {
                "embed_base_url": "https://embed.example.com/v1",
                "embed_model": "image-model",
            }
        },
        get_client,
    )
    assert engine.enabled is False  # 缺 qdrant_url，enabled 照旧为 False
    assert engine.qdrant_ready is False

    engine._request_json = lambda *a, **k: pytest.fail("不应发起请求")
    with caplog.at_level("DEBUG", logger="astrbot"):
        assert (
            await engine.original_info_for_thumb("http://bed/file/thumbnails/1_x.jpg")
            is None
        )
        assert (
            await engine.original_by_file_name("x.jpg") is None
        )
    messages = [record.getMessage() for record in caplog.records]
    assert sum("向量引擎不可用" in m for m in messages) >= 2


async def test_original_info_for_thumb_name_matches_basename_locally():
    """按缩略图**文件名**反查：text 只是缩小候选，判据必须是本地 basename 比对。

    `match.text` 是分词匹配——用完整缩略图名去搜，会带回同一前缀下的邻居
    （`…_【微博_赵今麦工作室official】别的图.jpg`）。把 text 命中当结果就会
    张冠李戴，故必须逐条比对 basename。
    """
    engine = make_engine()
    wanted = "1789658616018_【微博_赵今麦工作室official】剧照.jpg"
    original = "http://bed/file/12、日常分享/【微博@赵今麦工作室official】剧照.jpg"
    payloads = [
        {
            "thumb_url": "http://bed/file/thumbnails/1789658616018_【微博_赵今麦工作室official】别的图.jpg",
            "image_url": "http://bed/file/12、日常分享/别的图.jpg",
            "file_name": "别的图.jpg",
        },
        {
            "thumb_url": f"http://bed/file/thumbnails/{wanted}",
            "image_url": original,
            "file_name": "【微博@赵今麦工作室official】剧照.jpg",
        },
    ]
    seen: list[tuple] = []

    async def fake_scroll(query_filter, label, limit=1):
        seen.append((query_filter, label, limit))
        return payloads

    engine._scroll_filter = fake_scroll
    info = await engine.original_info_for_thumb_name(wanted)

    assert info is not None
    assert info["url"] == original
    assert info["file_name"] == "【微博@赵今麦工作室official】剧照.jpg"
    # 时间戳前缀要先剥掉再搜（库里 thumb_url 也带前缀，但按剥后主名搜更稳）
    assert "1789658616018_" not in seen[0][1]
    assert seen[0][2] == 20, "text 匹配会带邻里，取候选页要够大"


async def test_original_info_for_thumb_name_gives_up_when_not_unique():
    """同一缩略图名在多目录下有多份时放弃反查，不赌一个。"""
    engine = make_engine()
    name = "1789658616018_同名.jpg"
    payloads = [
        {
            "thumb_url": "http://bed/file/thumbnails/1789658616018_同名.jpg",
            "image_url": "http://bed/file/a/同名.jpg",
            "file_name": "同名.jpg",
            "size_bytes": 0.72,
            "phash": "aaaa",
        },
        {
            "thumb_url": "http://bed/file/thumbnails/1789658616018_同名.jpg",
            "image_url": "http://bed/file/b/同名.jpg",
            "file_name": "同名.jpg",
            "size_bytes": 0.72,
            "phash": "bbbb",
        },
    ]

    async def fake_scroll(query_filter, label, limit=1):
        return payloads

    engine._scroll_filter = fake_scroll
    assert await engine.original_info_for_thumb_name(name) is None


async def test_original_info_for_thumb_name_ignores_non_filename_input():
    engine = make_engine()
    engine._scroll_filter = lambda *a, **k: pytest.fail("非文件名不应发起查询")
    assert await engine.original_info_for_thumb_name("") is None
    assert await engine.original_info_for_thumb_name("没有扩展名") is None


async def test_original_info_for_thumb_name_accepts_name_without_timestamp():
    """库里的 thumb_url 带毫秒前缀、调用方手上的名字已被剥掉时也必须命中。

    实测线上：`/原图` 走到反查时名字已过 `strip_leading_decoration`（剥了时间戳），
    而 `thumb_url` 里一定带前缀——只比原始形态会让命中数从 1 掉到 0。
    """
    engine = make_engine()
    original = "http://bed/file/12、日常分享/【微博@赵今麦工作室official】剧照.jpg"
    payloads = [
        {
            "thumb_url": "http://bed/file/thumbnails/1789658616018_【微博_赵今麦工作室official】剧照.jpg",
            "image_url": original,
            "file_name": "【微博@赵今麦工作室official】剧照.jpg",
        }
    ]

    async def fake_scroll(query_filter, label, limit=1):
        return payloads

    engine._scroll_filter = fake_scroll
    info = await engine.original_info_for_thumb_name("【微博_赵今麦工作室official】剧照.jpg")

    assert info is not None and info["url"] == original


async def test_original_info_for_thumb_name_tolerates_duplicate_points():
    """同一张图在库里被重复入库（多份缩略图副本）时，按原图直链去重后取其一。

    实测线上就有这种点：`1789658616018_…` 与 `1789659967539_…` 是同一张图的
    两份缩略图副本，`image_url` 相同。若一律按「命中必须恰好 1 条」放弃，
    这类图就永远反查不到——而它们是完全确定的。
    """
    engine = make_engine()
    original = "http://bed/file/12、日常分享/【微博@赵今麦工作室official】剧照.jpg"
    # 线上实测形态：同名两份、分别落在「12、日常分享/…」与「2、影视作品周边/…」，
    # size_bytes / thumb_size_bytes / phash 全部一致 —— 同一张图存了两份
    payloads = [
        {
            "src": "/file/12、日常分享/【微博图集@赵今麦工作室official】/剧照.jpg",
            "thumb_url": "http://bed/file/thumbnails/1789658616018_【微博_赵今麦工作室official】剧照.jpg",
            "image_url": original,
            "file_name": "【微博@赵今麦工作室official】剧照.jpg",
            "size_bytes": 0.72,
            "mime": "image/jpeg",
            "phash": "e19f71c31a39da710fa8651c",
        },
        {
            "src": "/file/2、影视作品周边/《樱桃琥珀》宣传图集/剧照.jpg",
            "thumb_url": "http://bed/file/thumbnails/1789659967539_【微博_赵今麦工作室official】剧照.jpg",
            "image_url": "http://bed/file/2、影视作品周边/《樱桃琥珀》宣传图集/剧照.jpg",
            "file_name": "【微博@赵今麦工作室official】剧照.jpg",
            "size_bytes": 0.72,
            "mime": "image/jpeg",
            "phash": "e19f71c31a39da710fa8651c",
        },
    ]

    async def fake_scroll(query_filter, label, limit=1):
        return payloads

    engine._scroll_filter = fake_scroll
    info = await engine.original_info_for_thumb_name("【微博_赵今麦工作室official】剧照.jpg")

    assert info is not None and info["url"] == original

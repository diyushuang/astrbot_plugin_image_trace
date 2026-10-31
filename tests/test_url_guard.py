import aiohttp
import pytest
from aiohttp import web

from astrbot_plugin_image_trace import url_guard
from astrbot_plugin_image_trace.url_guard import (
    DEFAULT_MAX_RESPONSE_BYTES,
    ResponseTooLargeError,
    UrlBlockedError,
    guarded_request,
    prepare_headers,
    read_limited_bytes,
    read_limited_text,
    resolve_public_addrs,
)


class FakeContent:
    """模拟 aiohttp StreamReader：读后消耗，读尽后返回 EOF。"""

    def __init__(self, data: bytes):
        self.data = data

    async def read(self, size: int = -1) -> bytes:
        if not self.data:
            return b""
        chunk = self.data if size < 0 else self.data[:size]
        self.data = self.data[len(chunk):]
        return chunk


class ChunkedContent:
    """模拟分块到达：read(n) 每次只返回一个已到达的网络分块，可能远小于 n。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def read(self, size: int = -1) -> bytes:
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


class FakeResponse:
    def __init__(self, status=200, headers=None, data=b"", location=""):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(data)
        self.location = location

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    # 与真实 aiohttp 一致：ClientResponse.release() 是同步方法。替身写成 async
    # 会掩盖生产代码里 `await resp.release()` 的 TypeError（重定向必炸）。
    def release(self):
        return None


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, method, url, allow_redirects=False, **kwargs):
        self.requests.append((method, url, allow_redirects, kwargs))
        return self.responses.pop(0)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://10.0.0.1",
        "http://192.168.1.1",
        "http://172.16.0.1",
        "http://100.64.0.1",
        "http://169.254.1.1",
        "http://[::1]/",
    ],
)
def test_resolve_blocks_non_public_ips(url):
    with pytest.raises(UrlBlockedError):
        resolve_public_addrs(url)


def test_resolve_allows_public_ip_literal():
    host, addresses = resolve_public_addrs("https://8.8.8.8")
    assert host == "8.8.8.8"
    assert addresses[0]["host"] == "8.8.8.8"


async def test_read_limited_bytes_rejects_oversized_response():
    response = FakeResponse(headers={"Content-Length": "999999999"})
    with pytest.raises(ResponseTooLargeError):
        await read_limited_bytes(response, limit=10)

    response = FakeResponse(headers={"Content-Length": "11"}, data=b"x" * 11)
    with pytest.raises(ResponseTooLargeError):
        await read_limited_bytes(response, limit=10)

    response = FakeResponse(data=b"x" * 11)
    with pytest.raises(ResponseTooLargeError):
        await read_limited_bytes(response, limit=10)


async def test_read_limited_bytes_reads_full_body_across_chunks():
    # 线上故障回归：aiohttp 的 read(n) 只返回当前缓冲的分块，单次调用
    # 会把分块到达的较大响应体静默截断，必须循环读到 EOF 拿到完整响应体
    response = FakeResponse(data=b"")
    response.content = ChunkedContent([b"x" * 3893, b"y" * 100, b"z"])
    assert (
        await read_limited_bytes(response, limit=DEFAULT_MAX_RESPONSE_BYTES)
        == b"x" * 3893 + b"y" * 100 + b"z"
    )

    response.content = ChunkedContent([b""])
    assert await read_limited_bytes(response) == b""


def test_prepare_headers_strips_credentials_on_cross_origin_redirect():
    def prepare(_url, _method, cross_origin):
        return {
            "headers": {
                "Authorization": "Bearer secret",
                "api-key": "secret",
                "cookie": "session=secret",
                "X-Safe": "yes",
            },
            "json": None if cross_origin else {"keep": True},
        }

    same_origin = prepare_headers(prepare, "https://a.example.com", "POST", cross_origin=False)
    cross_origin = prepare_headers(prepare, "https://b.example.com", "POST", cross_origin=True)
    assert same_origin["headers"]["Authorization"] == "Bearer secret"
    assert "Authorization" not in cross_origin["headers"]
    assert "api-key" not in cross_origin["headers"]
    assert "cookie" not in cross_origin["headers"]
    assert cross_origin["headers"]["X-Safe"] == "yes"
    assert cross_origin["json"] is None


async def test_guarded_request_blocks_redirect_to_private_address(monkeypatch):
    def resolve(url):
        if url == "https://public.example.com":
            return "public.example.com", []
        raise UrlBlockedError("private address")

    monkeypatch.setattr(url_guard, "resolve_public_addrs", resolve)
    session = FakeSession([FakeResponse(302, headers={"Location": "http://127.0.0.1"})])
    with pytest.raises(UrlBlockedError):
        async with await guarded_request(session, "GET", "https://public.example.com"):
            pass


async def test_guarded_request_rechecks_each_redirect_and_strips_credentials(monkeypatch):
    def resolve(url):
        if url in ("https://a.example.com", "https://b.example.com"):
            return url, []
        raise UrlBlockedError("unexpected host")

    monkeypatch.setattr(url_guard, "resolve_public_addrs", resolve)
    session = FakeSession(
        [
            FakeResponse(302, headers={"Location": "https://b.example.com"}),
            FakeResponse(200),
        ]
    )

    def prepare(_url, _method, cross_origin):
        headers = {"X-Safe": "yes"}
        if not cross_origin:
            headers.update({"Authorization": "Bearer secret", "api-key": "secret"})
        return {"headers": headers}

    async with await guarded_request(
        session, "GET", "https://a.example.com", prepare=prepare
    ) as response:
        assert response.status == 200

    assert len(session.requests) == 2
    assert session.requests[0][3]["headers"]["Authorization"] == "Bearer secret"
    assert "Authorization" not in session.requests[1][3]["headers"]
    assert "api-key" not in session.requests[1][3]["headers"]
    assert session.requests[1][3]["headers"]["X-Safe"] == "yes"


async def test_read_limited_text_without_charset_after_manual_stream_read():
    """回归：响应体经 read_limited_bytes 手动流式读取后，aiohttp 的
    _body 仍为空，get_encoding 在 Content-Type 未声明 charset（如
    text/html 错误页）时会抛 RuntimeError；read_limited_text 必须兜底
    为 utf-8 而不是让整条链路失败。用真实 aiohttp 服务端复现。"""

    async def html_handler(request):
        # content_type 指定 text/html 且不传 charset，模拟线上触发的错误页
        return web.Response(
            body="<!doctype html><html>gateway error</html>", content_type="text/html"
        )

    app = web.Application()
    app.router.add_get("/html", html_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"http://127.0.0.1:{port}/html") as resp,
        ):
            assert resp.charset is None
            # 修复前此处抛 RuntimeError: Cannot compute fallback encoding...
            text = await read_limited_text(resp)
            assert "gateway error" in text
    finally:
        await runner.cleanup()


async def test_read_limited_text_with_declared_charset():
    """头部声明了 charset 的响应仍按声明编码解码。"""

    async def gbk_handler(request):
        return web.Response(body="中文".encode("gbk"), content_type="text/plain", charset="gbk")

    app = web.Application()
    app.router.add_get("/gbk", gbk_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"http://127.0.0.1:{port}/gbk") as resp,
        ):
            assert resp.charset == "gbk"
            assert await read_limited_text(resp) == "中文"
    finally:
        await runner.cleanup()


def test_allowed_private_hosts_let_configured_host_through():
    """私网白名单：仅放行已配置的主机，其它私网地址仍拒绝，清空后恢复全拒。"""
    url_guard.set_allowed_private_hosts({"172.20.0.1"})
    try:
        host, addresses = resolve_public_addrs("http://172.20.0.1:7658/random")
        assert host == "172.20.0.1"
        assert addresses[0]["host"] == "172.20.0.1"
        assert addresses[0]["port"] == 7658
        # 白名单外的主机不受益，即使同属私网段
        with pytest.raises(UrlBlockedError):
            resolve_public_addrs("http://10.0.0.1")
        with pytest.raises(UrlBlockedError):
            resolve_public_addrs("http://127.0.0.1")
    finally:
        url_guard.set_allowed_private_hosts(())
    # 清空后原主机恢复拒绝（默认 fail-closed）
    with pytest.raises(UrlBlockedError):
        resolve_public_addrs("http://172.20.0.1:7658/random")


def test_per_call_allowed_private_hosts_are_isolated():
    """按调用传入的白名单不依赖模块级全局：多实例互不覆盖。"""
    url_guard.set_allowed_private_hosts(())
    host, addresses = resolve_public_addrs(
        "http://172.20.0.1:7658/random", {"172.20.0.1"}
    )
    assert host == "172.20.0.1"
    assert addresses[0]["host"] == "172.20.0.1"

    # 另一个实例的空白名单仍应拒绝同一主机
    with pytest.raises(UrlBlockedError):
        resolve_public_addrs("http://172.20.0.1:7658/random", set())

import aiohttp

from astrbot_plugin_image_trace import http_client
from astrbot_plugin_image_trace.http_client import GuardedHttpClient


class FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


async def test_guarded_http_client_reuses_one_session(monkeypatch):
    sessions = []
    prepares = []

    async def fake_guarded_request(session, method, url, *, prepare=None):
        sessions.append(session)
        prepares.append(prepare)
        return FakeResponse()

    monkeypatch.setattr(http_client, "guarded_request", fake_guarded_request)
    client = GuardedHttpClient()
    timeout = aiohttp.ClientTimeout(total=3)

    async with await client.request(
        "GET", "https://example.com", prepare=lambda _u, _m, _c: {"x": 1}, timeout=timeout
    ):
        pass
    async with await client.request("GET", "https://example.com/2"):
        pass

    assert len(sessions) == 2
    assert sessions[0] is sessions[1]
    kwargs = prepares[0]("https://example.com", "GET", False)
    assert kwargs == {"x": 1, "timeout": timeout}
    assert prepares[1]("https://example.com/2", "GET", False) == {}

    await client.close()

"""存储后端拆分后的行为基线：各后端独立可测，全部使用 fake HTTP 客户端。"""

from __future__ import annotations

import json

import aiohttp
import pytest

from astrbot_plugin_image_trace.image_bed import ImageBedClient
from astrbot_plugin_image_trace.storage.cloudflare_backend import CloudflareImgBedBackend
from astrbot_plugin_image_trace.storage.http_backend import GenericHttpBackend
from astrbot_plugin_image_trace.storage.modes import MODE_OCI, MODE_R2
from astrbot_plugin_image_trace.storage.object_backend import ObjectBackend


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, n: int) -> bytes:
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"", headers: dict | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def get_encoding(self) -> str:
        return "utf-8"


class _FakeHttpClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def request(self, method: str, url: str, *, prepare=None, timeout=None):
        self.calls.append({"method": method, "url": url, "prepare": prepare, "timeout": timeout})
        return self.response


def _getter(client: _FakeHttpClient):
    async def get() -> _FakeHttpClient:
        return client

    return get


def _timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=5)


def _json_response(payload, status: int = 200) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"))


async def _write_source(tmp_path) -> str:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"image-data")
    return str(source)


# ---------- generic_http ----------


async def test_generic_http_upload_returns_extracted_url(tmp_path):
    http = _FakeHttpClient(_json_response({"data": {"url": "https://cdn.example.com/a.jpg"}}))
    backend = GenericHttpBackend({"api_url": "https://bed.example/upload"}, _getter(http), _timeout)

    url = await backend.upload(await _write_source(tmp_path))

    assert url == "https://cdn.example.com/a.jpg"
    assert http.calls[0]["method"] == "POST"


async def test_generic_http_upload_rejects_non_2xx(tmp_path):
    http = _FakeHttpClient(_FakeResponse(500, b"boom"))
    backend = GenericHttpBackend({"api_url": "https://bed.example/upload"}, _getter(http), _timeout)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        await backend.upload(await _write_source(tmp_path))


async def test_generic_http_upload_rejects_non_json_body(tmp_path):
    http = _FakeHttpClient(_FakeResponse(200, b"<html>not json</html>"))
    backend = GenericHttpBackend({"api_url": "https://bed.example/upload"}, _getter(http), _timeout)

    with pytest.raises(RuntimeError, match="不是 JSON"):
        await backend.upload(await _write_source(tmp_path))


async def test_generic_http_upload_rejects_missing_url_path(tmp_path):
    http = _FakeHttpClient(_json_response({"data": {}}))
    backend = GenericHttpBackend({"api_url": "https://bed.example/upload"}, _getter(http), _timeout)

    with pytest.raises(RuntimeError, match="未能从响应路径"):
        await backend.upload(await _write_source(tmp_path))


# ---------- cloudflare_imgbed ----------


async def test_cloudflare_upload_builds_same_origin_url(tmp_path):
    http = _FakeHttpClient(_json_response([{"src": "/file/a.jpg"}]))
    backend = CloudflareImgBedBackend(
        {"cfi_base_url": "https://img.example.com/"}, _getter(http), _timeout
    )

    url = await backend.upload(await _write_source(tmp_path))

    assert url == "https://img.example.com/file/a.jpg"
    assert http.calls[0]["url"] == "https://img.example.com/upload"


async def test_cloudflare_upload_requires_src(tmp_path):
    http = _FakeHttpClient(_json_response([{}]))
    backend = CloudflareImgBedBackend(
        {"cfi_base_url": "https://img.example.com"}, _getter(http), _timeout
    )

    with pytest.raises(RuntimeError, match="缺少 src"):
        await backend.upload(await _write_source(tmp_path))


async def test_cloudflare_delete_posts_file_id_with_bearer_token():
    http = _FakeHttpClient(_json_response({"success": True}))
    backend = CloudflareImgBedBackend(
        {"cfi_base_url": "https://img.example.com", "cfi_token": "token"},
        _getter(http),
        _timeout,
    )

    assert await backend.delete("token", "a b.jpg") is True

    call = http.calls[0]
    assert call["url"] == "https://img.example.com/api/manage/delete/batch"
    kwargs = call["prepare"](call["url"], "POST")
    assert json.loads(kwargs["data"]) == {"fileIds": ["a b.jpg"]}
    assert kwargs["headers"]["Authorization"] == "Bearer token"


# ---------- R2 / OCI ----------


async def test_r2_upload_returns_public_url(tmp_path):
    http = _FakeHttpClient(_FakeResponse(200, b""))
    backend = ObjectBackend(
        {
            "r2_account_id": "acc",
            "r2_access_key_id": "ak",
            "r2_secret_access_key": "sk",
            "r2_bucket": "bucket",
            "r2_public_base_url": "https://cdn.example.com/prefix/",
        },
        _getter(http),
        MODE_R2,
        lambda: 10,
    )

    url = await backend.upload(await _write_source(tmp_path))

    assert url.startswith("https://cdn.example.com/prefix/trace_")
    assert url.endswith(".jpg")
    assert http.calls[0]["url"].startswith("https://acc.r2.cloudflarestorage.com/bucket/")


def test_r2_reverse_url_requires_same_origin_and_prefix():
    backend = ObjectBackend(
        {"r2_public_base_url": "https://cdn.example.com/prefix"}, _getter(None), MODE_R2, lambda: 10
    )

    assert backend.key_from_url("https://cdn.example.com/prefix/a%20b.jpg?x=1") == ("a b.jpg", True)
    assert backend.key_from_url("https://evil.example.com/prefix/a.jpg") == ("", False)
    assert backend.key_from_url("https://cdn.example.com/other/a.jpg") == ("", False)


def test_oci_public_url_prefers_custom_base_then_public_bucket():
    custom = ObjectBackend(
        {"oci_public_base_url": "https://cdn.example.com/prefix"},
        _getter(None),
        MODE_OCI,
        lambda: 10,
    )
    assert custom.oci_public_url("a b.jpg") == "https://cdn.example.com/prefix/a%20b.jpg"

    bucket = ObjectBackend(
        {
            "oci_namespace": "ns",
            "oci_region": "ap-osaka-1",
            "oci_bucket": "bucket",
            "oci_public_bucket": True,
        },
        _getter(None),
        MODE_OCI,
        lambda: 10,
    )
    assert bucket.oci_public_url("a b.jpg") == (
        "https://objectstorage.ap-osaka-1.oraclecloud.com/n/ns/b/bucket/o/a%20b.jpg"
    )


def test_oci_reverse_url_checks_origin_namespace_and_bucket():
    backend = ObjectBackend(
        {
            "oci_namespace": "ns",
            "oci_region": "ap-osaka-1",
            "oci_bucket": "bucket",
            "oci_public_bucket": True,
        },
        _getter(None),
        MODE_OCI,
        lambda: 10,
    )

    valid = "https://objectstorage.ap-osaka-1.oraclecloud.com/n/ns/b/bucket/o/a%20b.jpg?token=1"
    assert backend.key_from_url(valid) == ("a b.jpg", True)
    assert backend.key_from_url("https://evil.example.com/n/ns/b/bucket/o/a.jpg") == ("", False)
    assert backend.key_from_url("https://objectstorage.ap-osaka-1.oraclecloud.com/n/ns/b/x/o/a.jpg") == (
        "",
        False,
    )


# ---------- 门面回退 ----------


async def test_unknown_mode_falls_back_to_local_copy(tmp_path):
    async def get_client():
        raise AssertionError("unknown mode should not use HTTP")

    client = ImageBedClient(str(tmp_path), {"mode": "no-such-bed"}, get_client)
    stored = await client.store(await _write_source(tmp_path))

    assert stored.ok is True
    assert stored.url is None
    assert stored.file_path is not None
    assert "无法识别" in stored.message
    assert stored.file_path.startswith(str(tmp_path / "images"))

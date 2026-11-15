"""ImageSource / StorageRegistry 的接口与行为基线（Step 2）。

三条用例分别驱动真实实现：流式落盘的超限清理、缩放探测拒绝经注入回调上报、
存储注册表把 ImageBedClient 适配成 ImageStore 协议。
"""

from __future__ import annotations

import os

import pytest

from astrbot_plugin_image_trace.media import ImageSource
from astrbot_plugin_image_trace.storage import ImageStore, StorageRegistry


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, status=200, headers=None, chunks=None):
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(chunks or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def request(self, method, url, **_kwargs):
        self.calls.append((method, url))
        return self._response


def _source(tmp_path, client, **kwargs) -> ImageSource:
    async def getter():
        return client

    def remove(path):
        if os.path.exists(path):
            os.remove(path)

    return ImageSource(
        http_getter=getter,
        tmp_dir=str(tmp_path),
        remove_file=remove,
        size_limit_bytes=lambda: 1024,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_stream_response_to_temp_aborts_and_cleans_on_oversize(tmp_path):
    client = _FakeClient(_FakeResponse(chunks=[b"a" * 10, b"b" * 10]))
    source = _source(tmp_path, client)

    with pytest.raises(ValueError, match="超过大小上限"):
        await source.stream_response_to_temp(client._response, limit=15)

    # 超限必须删掉半成品，临时目录里不留垃圾
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_probe_reports_scaled_rejection_through_injected_callback(tmp_path):
    recorded = []
    client = _FakeClient(_FakeResponse(status=400))
    source = _source(tmp_path, client, on_scaled_unsupported=recorded.append)

    exists, length = await source.probe_uncached("https://img.example.com/x.jpg", allow_range=False)

    assert (exists, length) == (None, None)
    assert recorded == [400]
    # 能力拒绝不回退 GET，只发一次探测请求
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_storage_registry_adapts_image_bed_as_image_store(make_plugin, tmp_path):
    plugin = make_plugin()
    assert isinstance(plugin.storage, StorageRegistry)
    store = plugin.storage.active
    assert isinstance(store, ImageStore)

    src = tmp_path / "source.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    stored = await store.store(str(src))
    assert stored.ok is True
    assert plugin.storage.local_dir
    target = stored.url or stored.file_path
    assert target
    # 本地模式：src_path 原样返回、没有远端对象可删（返回 False 而不是抛错）
    assert store.src_path(target)
    assert await store.delete("https://example.com/not-ours.jpg") is False

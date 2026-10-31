"""图床哈希索引的后台刷新链路测试。

验证 scroll_payloads → HashIndex.build → 缓存落盘 → 检索可用这条完整链路，
以及刷新失败时「沿用旧缓存」而不是把索引清空。
"""

from __future__ import annotations

import pathlib
import time
import types

import pytest

VECTOR_CFG = {
    "search_engine": "auto",
    "vector_search": {
        "qdrant_url": "http://127.0.0.1:6333",
        "embed_base_url": "http://127.0.0.1:8000",
        "embed_model": "m",
    },
}


def _points(n: int = 3) -> list:
    return [
        {
            "id": f"p{i}",
            "payload": {
                "phash": f"{i:064x}",
                "image_url": f"https://img.example.com/file/{i}.jpg",
                "file_name": f"图{i}.jpg",
                "created_at": "2026-01-01 00:00:00",
            },
        }
        for i in range(n)
    ]


async def test_refresh_builds_index_and_writes_cache(make_plugin, data_dir, monkeypatch):
    plugin = make_plugin(**VECTOR_CFG)

    async def fake_scroll(**_kwargs):
        return _points(3)

    monkeypatch.setattr(plugin.vector, "scroll_payloads", fake_scroll)

    await plugin._refresh_remote_hash()

    assert plugin.remote_hash.count == 3
    assert plugin._hash_pool_size() == 3
    # 缓存路径由插件数据目录决定（data_dir/plugin_data/<插件名>/remote_phash.json）
    cache_file = pathlib.Path(plugin.remote_hash.cache_path)
    assert cache_file.is_file()
    assert cache_file.name == "remote_phash.json"
    assert plugin.remote_hash.search("0" * 64, limit=1)[0].file_name == "图0.jpg"


async def test_cache_reload_after_restart(make_plugin, data_dir, monkeypatch):
    """刷新的成果必须能被下一次启动读回，否则每次重启都要重拉 2.4 万点。"""
    plugin = make_plugin(**VECTOR_CFG)

    async def fake_scroll(**_kwargs):
        return _points(3)

    monkeypatch.setattr(plugin.vector, "scroll_payloads", fake_scroll)
    await plugin._refresh_remote_hash()

    fresh = make_plugin(**VECTOR_CFG)
    assert fresh.remote_hash.load_cache() is True
    assert fresh.remote_hash.count == 3


async def test_refresh_failure_keeps_existing_index(make_plugin, data_dir, monkeypatch):
    """刷新失败必须沿用旧索引——清空会让检索静默退化为「总是未命中」。"""
    plugin = make_plugin(**VECTOR_CFG)
    plugin.remote_hash.build(_points(2), remote_count=2)
    assert plugin.remote_hash.count == 2

    async def boom(**_kwargs):
        raise RuntimeError("qdrant unreachable")

    monkeypatch.setattr(plugin.vector, "scroll_payloads", boom)

    await plugin._refresh_remote_hash()  # 不应抛出

    assert plugin.remote_hash.count == 2


async def test_refresh_skips_when_disabled(make_plugin, data_dir, monkeypatch):
    plugin = make_plugin(**VECTOR_CFG)

    async def boom(**_kwargs):
        raise AssertionError("不应被调用")

    monkeypatch.setattr(plugin.vector, "scroll_payloads", boom)
    monkeypatch.setattr(plugin, "_bool_cfg", lambda key, default=None: False)

    await plugin._refresh_remote_hash()
    assert plugin.remote_hash.count == 0


async def test_start_refresh_is_idempotent(make_plugin, data_dir, monkeypatch):
    """重复触发不能起第二个任务——两批并发写缓存会互相覆盖。"""
    plugin = make_plugin(**VECTOR_CFG)
    calls = []

    async def fake_scroll(**_kwargs):
        calls.append(1)
        return _points(1)

    monkeypatch.setattr(plugin.vector, "scroll_payloads", fake_scroll)

    plugin.start_remote_hash_refresh()
    first = plugin._remote_hash_task
    plugin.start_remote_hash_refresh()
    assert plugin._remote_hash_task is first

    await first
    assert len(calls) == 1


async def test_initialize_starts_refresh(make_plugin, data_dir, monkeypatch):
    plugin = make_plugin(**VECTOR_CFG)

    async def fake_scroll(**_kwargs):
        return _points(2)

    monkeypatch.setattr(plugin.vector, "scroll_payloads", fake_scroll)

    await plugin.initialize()
    assert plugin._remote_hash_task is not None
    await plugin._remote_hash_task
    assert plugin.remote_hash.count == 2


async def test_initialize_noop_without_vector_engine(make_plugin, data_dir):
    plugin = make_plugin()  # 未配 qdrant_url → vector.enabled False
    await plugin.initialize()
    assert plugin._remote_hash_task is None


async def test_initialize_skips_refresh_when_cache_is_fresh(make_plugin, data_dir, monkeypatch):
    """缓存未超过 remote_hash_ttl 时，启动不再翻一遍全量 Qdrant。"""
    plugin = make_plugin(**VECTOR_CFG)

    async def boom(**_kwargs):
        raise AssertionError("缓存新鲜时不应发起刷新")

    monkeypatch.setattr(plugin.vector, "scroll_payloads", boom)
    plugin.remote_hash.build(_points(2), remote_count=2)
    plugin.remote_hash.built_at = time.time()

    await plugin.initialize()

    assert plugin._remote_hash_task is None
    assert plugin.remote_hash.count == 2


async def test_stale_cache_triggers_refresh_once_then_respects_ttl(
    make_plugin, data_dir, monkeypatch
):
    """超过 TTL 才刷新；刷新完成后的下一次 /溯源 不应再次全量拉取。"""
    plugin = make_plugin(**VECTOR_CFG)
    calls = []

    async def fake_scroll(**_kwargs):
        calls.append(1)
        return _points(1)

    monkeypatch.setattr(plugin.vector, "scroll_payloads", fake_scroll)
    plugin.remote_hash.build([], remote_count=0)
    plugin.remote_hash.built_at = time.time()

    # 空缓存即使 built_at 很新也必须重建：否则切换 hash_size 后旧缓存被过滤成
    # 0 条，TTL 内哈希腿会一直静默为空
    plugin._maybe_start_remote_hash_refresh()
    assert plugin._remote_hash_task is not None
    await plugin._remote_hash_task
    assert plugin.remote_hash.count == 1
    assert len(calls) == 1

    # 刷新已把 built_at 更新为当前时间且有数据：紧接着的 /溯源 不应再起任务
    plugin._maybe_start_remote_hash_refresh()
    assert len(calls) == 1

    plugin.remote_hash.built_at = time.time() - plugin._remote_hash_ttl() - 1
    plugin._maybe_start_remote_hash_refresh()
    assert plugin._remote_hash_task is not None
    await plugin._remote_hash_task
    assert len(calls) == 2


async def test_terminate_cancels_pending_refresh(make_plugin, data_dir, monkeypatch):
    """卸载时不能让刷新任务继续跑着写缓存文件。"""
    import asyncio

    plugin = make_plugin(**VECTOR_CFG)
    started = asyncio.Event()

    async def slow_scroll(**_kwargs):
        started.set()
        await asyncio.sleep(30)
        return _points(1)

    monkeypatch.setattr(plugin.vector, "scroll_payloads", slow_scroll)
    plugin.start_remote_hash_refresh()
    await started.wait()

    errors: list = []
    await plugin._stop_remote_hash_refresh(errors)

    assert errors == []
    assert plugin._remote_hash_task is None


async def test_scroll_payloads_requires_key(plugin_with_qdrant_stub):
    engine, recorder = plugin_with_qdrant_stub
    assert await engine.scroll_payloads(key="") == []
    assert recorder == []


@pytest.fixture
def plugin_with_qdrant_stub(monkeypatch):
    """构造一个 VectorEngine，把 _request_json 换成可记录调用的假实现。"""
    from astrbot_plugin_image_trace.vector_search import VectorEngine

    engine = VectorEngine(
        {
            "vector_search": {
                "qdrant_url": "http://127.0.0.1:6333",
                "embed_base_url": "http://127.0.0.1:8000",
                "embed_model": "m",
            }
        },
        http_getter=None,
    )
    recorder: list = []

    async def fake_request(method, url, *, json_body=None, headers=None):
        recorder.append({"method": method, "url": url, "body": json_body})
        return {"result": {"points": [], "next_page_offset": None}}

    monkeypatch.setattr(engine, "_request_json", fake_request)
    return engine, recorder


async def test_scroll_payloads_paginates_and_filters(plugin_with_qdrant_stub, monkeypatch):
    """翻页要走 next_page_offset，且只收 payload[key] 为非空字符串的点。"""
    engine, recorder = plugin_with_qdrant_stub
    pages = [
        {
            "result": {
                "points": [
                    {"id": "a", "payload": {"phash": "aa", "image_url": "u1"}},
                    {"id": "b", "payload": {"phash": "", "image_url": "u2"}},
                    {"id": "c", "payload": {"image_url": "u3"}},
                    {"id": "d", "payload": {"phash": "dd", "image_url": "u4"}},
                ],
                "next_page_offset": "d",
            }
        },
        {
            "result": {
                "points": [{"id": "e", "payload": {"phash": "ee", "image_url": "u5"}}],
                "next_page_offset": None,
            }
        },
    ]
    calls = {"n": 0}

    async def fake_request(method, url, *, json_body=None, headers=None):
        recorder.append(json_body)
        page = pages[calls["n"]]
        calls["n"] += 1
        return page

    monkeypatch.setattr(engine, "_request_json", fake_request)

    got = await engine.scroll_payloads(key="phash", page_size=4)

    assert [p["id"] for p in got] == ["a", "d", "e"]
    assert calls["n"] == 2
    assert "offset" not in recorder[0]
    assert recorder[1]["offset"] == "d"
    assert recorder[0]["with_payload"] == ["phash"]


async def test_scroll_payloads_respects_max_points(plugin_with_qdrant_stub, monkeypatch):
    engine, _recorder = plugin_with_qdrant_stub

    async def fake_request(method, url, *, json_body=None, headers=None):
        return {
            "result": {
                "points": [
                    {"id": str(i), "payload": {"phash": f"{i:04x}", "image_url": "u"}}
                    for i in range(10)
                ],
                "next_page_offset": "more",
            }
        }

    monkeypatch.setattr(engine, "_request_json", fake_request)

    got = await engine.scroll_payloads(key="phash", page_size=10, max_points=4)
    assert len(got) == 4


async def test_scroll_payloads_requests_only_needed_keys(plugin_with_qdrant_stub, monkeypatch):
    engine, _recorder = plugin_with_qdrant_stub
    seen = {}

    async def fake_request(method, url, *, json_body=None, headers=None):
        seen.update(json_body or {})
        return {"result": {"points": [], "next_page_offset": None}}

    monkeypatch.setattr(engine, "_request_json", fake_request)

    await engine.scroll_payloads(
        key="phash", with_payload_keys=["phash", "image_url", "file_name"]
    )
    assert seen["with_payload"] == ["phash", "image_url", "file_name"]
    assert seen["with_vector"] is False


async def test_status_line_distinguishes_four_states(make_plugin, data_dir, monkeypatch):
    """状态行要能区分「开关关了 / 向量没配 / 拉了但空 / 有数据」。

    这四种情况用户看到的排查方向完全不同，混成一句「图床哈希索引不可用」
    会让人无从下手。
    """
    plugin = make_plugin(**VECTOR_CFG)
    # 开关开着但索引还是空的（刚启动、拉取中、或拉取失败）—— 不能说「已关闭」，
    # 否则用户会去翻配置找开关，实际该看的是机器人日志里的拉取报错。
    assert "空" in plugin._remote_hash_line()

    plugin.remote_hash.build(_points(2), remote_count=3)
    line = plugin._remote_hash_line()
    assert "2 条可用" in line
    assert "建于" in line

    plugin.remote_hash.build([], remote_count=0)
    assert "空" in plugin._remote_hash_line()

    # 向量引擎未配置时，开关即使为真也拉不到数据，须与「手动关掉」区分。
    plugin.vector = types.SimpleNamespace(qdrant_ready=False)
    assert "未配置 Qdrant" in plugin._remote_hash_line()

    plugin.vector = types.SimpleNamespace(qdrant_ready=True)
    monkeypatch.setattr(plugin, "_bool_cfg", lambda key, default=False: False if key == "remote_hash_enabled" else default)
    assert "已关闭" in plugin._remote_hash_line()

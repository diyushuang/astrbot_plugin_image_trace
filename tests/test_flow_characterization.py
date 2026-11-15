"""R0 行为基线：在拆分 services 之前钉住 main 里重流程的真实分支。

这些用例只 monkeypatch 下游 I/O（网络、LLM、卡片合成、随机图接口），
流程本身仍走 `_trace_*` / `_checkin_flow` / `_random_media_delivery` /
`_lookup_imgbed_file`，避免只测 helper 返回值导致的「重构后行为漂移而测试仍绿」。
"""

from __future__ import annotations

import io
import types
from urllib.parse import unquote

import pytest
from PIL import Image as PILImage

from astrbot_plugin_image_trace import main as main_module
from astrbot_plugin_image_trace.image_delivery import build_imgbed_file_url
from astrbot_plugin_image_trace.library import MatchResult
from astrbot_plugin_image_trace.random_media import RandomMediaError
from astrbot_plugin_image_trace.search.base import SearchOutcome


async def _collect(agen):
    return [item async for item in agen]


def _text(result) -> str:
    return getattr(result, "text", None) or ""


def _chain_text(result) -> str:
    return "".join(getattr(segment, "text", "") for segment in result.chain)


def _jpeg_bytes() -> bytes:
    buffer = io.BytesIO()
    PILImage.effect_noise((64, 64), 32).convert("RGB").save(buffer, "JPEG")
    return buffer.getvalue()


def _jpeg_file(tmp_path, name: str):
    path = tmp_path / name
    path.write_bytes(_jpeg_bytes())
    return path


def _image_segment(url: str = "https://qq.example.com/a.jpg"):
    from astrbot.api.message_components import Image

    return Image.fromURL(url)


# ----------------------------------------------------------------------
# /溯源：空库、向量失败回退、AI 复核开关
# ----------------------------------------------------------------------


async def test_trace_reports_empty_library_without_search(make_plugin, make_event):
    """本地图库与图床哈希索引都为空时，直接提示建库，不进入检索。"""
    plugin = make_plugin(search_engine="hash")
    event = make_event("/溯源", chain=[_image_segment()])

    results = await _collect(plugin.trace(event))

    assert len(results) == 1
    assert "图库还是空的" in _text(results[0])


async def test_trace_auto_vector_error_falls_back_to_hash(
    make_plugin, make_event, monkeypatch, tmp_path
):
    """向量引擎报错（不是未命中）时仍回退哈希，并明确告知用户。"""
    plugin = make_plugin(search_engine="auto")
    query = _jpeg_file(tmp_path, "query.jpg")

    async def fake_resolve(_seg, *, probe):
        return main_module.ResolvedImage(path=str(query), mime="image/jpeg")

    async def fake_outcome(_image):
        return SearchOutcome(kind="error", error="向量服务不可用")

    monkeypatch.setattr(plugin, "_resolve_local", fake_resolve)
    monkeypatch.setattr(
        plugin, "_vector_engine", lambda: types.SimpleNamespace(search_outcome=fake_outcome)
    )
    monkeypatch.setattr(
        plugin,
        "_hash_engine",
        lambda: types.SimpleNamespace(
            merged_candidates=lambda _phash, _want: types.SimpleNamespace(
                hits=[], candidate_lines=[]
            )
        ),
    )
    monkeypatch.setattr(plugin, "_hash_pool_size", lambda: 1)
    monkeypatch.setattr(plugin, "_merged_hash_candidates", lambda *_a, **_k: ([], []))

    results = await _collect(plugin._trace_auto(make_event(""), _image_segment()))

    texts = [_text(item) for item in results]
    assert any("向量引擎不可用，改用哈希比对" in text for text in texts)
    assert any("未找到相似度达标" in text for text in texts)


async def test_trace_auto_vector_error_without_hash_reports_unavailable(
    make_plugin, make_event, monkeypatch, tmp_path
):
    """向量报错且没有可用的哈希兜底时，给出可操作的建库提示而不是空转。"""
    plugin = make_plugin(search_engine="auto")
    query = _jpeg_file(tmp_path, "query.jpg")

    async def fake_resolve(_seg, *, probe):
        return main_module.ResolvedImage(path=str(query), mime="image/jpeg")

    async def fake_outcome(_image):
        return SearchOutcome(kind="error", error="向量服务不可用")

    monkeypatch.setattr(plugin, "_resolve_local", fake_resolve)
    monkeypatch.setattr(
        plugin, "_vector_engine", lambda: types.SimpleNamespace(search_outcome=fake_outcome)
    )
    monkeypatch.setattr(plugin, "_hash_pool_size", lambda: 0)

    results = await _collect(plugin._trace_auto(make_event(""), _image_segment()))

    assert len(results) == 1
    assert "向量引擎不可用，且哈希图库为空" in _text(results[0])


async def test_trace_hash_ai_verify_rejects_negative_candidate(
    make_plugin, make_event, monkeypatch, tmp_path
):
    """ai_verify 开启时，模型回答「否」的候选必须被剔除并按未命中文案回复。"""
    plugin = make_plugin(basic={"ai_verify": True})
    query = _jpeg_file(tmp_path, "query.jpg")
    candidate = _jpeg_file(tmp_path, "candidate.jpg")
    hit = MatchResult(
        id=1,
        similarity=0.91,
        image_url=None,
        file_path=str(candidate),
        note=None,
        source="register",
        width=64,
        height=64,
        created_at=None,
    )
    calls: list[list[str]] = []

    class _Provider:
        async def text_chat(self, *, prompt, image_urls):
            calls.append(list(image_urls))
            return types.SimpleNamespace(completion_text="否")

    async def fake_provider():
        return _Provider()

    plugin.context.get_using_provider_async = fake_provider
    monkeypatch.setattr(
        plugin, "_merged_hash_candidates", lambda *_a, **_k: ([hit], ["· candidate.jpg"])
    )

    results = await _collect(
        plugin._trace_hash(
            make_event(""),
            _image_segment(),
            resolved=main_module.ResolvedImage(path=str(query), mime="image/jpeg"),
        )
    )

    assert len(calls) == 1
    assert calls[0] == [str(candidate), str(query)]
    assert len(results) == 1
    assert "未找到相似度达标" in _text(results[0])


async def test_trace_hash_ai_verify_off_never_calls_provider(
    make_plugin, make_event, monkeypatch, tmp_path
):
    """ai_verify 关闭时不问模型，哈希命中直接回传。"""
    plugin = make_plugin(basic={"ai_verify": False})
    query = _jpeg_file(tmp_path, "query.jpg")
    candidate = _jpeg_file(tmp_path, "candidate.jpg")
    hit = MatchResult(
        id=1,
        similarity=0.91,
        image_url=None,
        file_path=str(candidate),
        note=None,
        source="register",
        width=64,
        height=64,
        created_at=None,
    )

    class _Provider:
        async def text_chat(self, **_kwargs):
            raise AssertionError("ai_verify 关闭时不该调用 LLM")

    async def fake_provider():
        return _Provider()

    plugin.context.get_using_provider_async = fake_provider
    monkeypatch.setattr(
        plugin, "_merged_hash_candidates", lambda *_a, **_k: ([hit], ["· candidate.jpg"])
    )

    results = await _collect(
        plugin._trace_hash(
            make_event(""),
            _image_segment(),
            resolved=main_module.ResolvedImage(path=str(query), mime="image/jpeg"),
        )
    )

    assert len(results) == 1
    assert "溯源命中" in _chain_text(results[0])


# ----------------------------------------------------------------------
# /签到：首次、重复、卡片合成失败
# ----------------------------------------------------------------------


async def _fake_checkin_candidate(_directory, _settings, *, thumbnail=None):
    return (
        "https://img.example.com/file/thumb.jpg",
        "https://img.example.com/file/orig.jpg",
        "orig.jpg",
    )


async def _fake_prepare(_blocks, *, compress):
    assert compress is True
    return [(_jpeg_bytes(), True)]


class _Quote:
    async def pick(self) -> str:
        return "今日份好心情"


async def test_checkin_first_then_repeat_same_day(make_plugin, make_event, monkeypatch):
    """同一天第二次签到必须走「今日已签到」分支，但仍重新取图。"""
    plugin = make_plugin(checkin={"render_mode": "simple"})
    event = make_event("/签到", platform="telegram", sender_id="repeat-user")
    monkeypatch.setattr(plugin, "_random_image_candidate", _fake_checkin_candidate)
    monkeypatch.setattr(plugin, "_quote_library", lambda: _Quote())
    monkeypatch.setattr(plugin, "_prepare_delivery_blocks", _fake_prepare)

    first = await _collect(plugin.checkin(event))
    # 第二次签到是另一条消息事件；同一事件重复投递会被事件级去重盾拦住。
    second_event = make_event("/签到", platform="telegram", sender_id="repeat-user")
    second = await _collect(plugin.checkin(second_event))

    assert "签到成功" in _chain_text(first[0])
    assert "今日已签到" in _chain_text(second[0])


async def test_checkin_card_compose_failure_falls_back_to_simple(
    make_plugin, make_event, monkeypatch
):
    """卡片合成失败（返回 None）时退回轻量模式，签到信息与语句仍要发出。"""
    plugin = make_plugin(checkin={"render_mode": "card", "image_mode": "original"})
    event = make_event("/签到", platform="telegram", sender_id="card-fail-user")
    monkeypatch.setattr(plugin, "_random_image_candidate", _fake_checkin_candidate)
    monkeypatch.setattr(plugin, "_quote_library", lambda: _Quote())
    monkeypatch.setattr(plugin, "_prepare_delivery_blocks", _fake_prepare)

    async def no_card(*_args, **_kwargs):
        return None

    monkeypatch.setattr(plugin, "_checkin_card_path", no_card)

    results = await _collect(plugin.checkin(event))

    assert len(results) == 1
    text = _chain_text(results[0])
    assert "签到成功" in text
    assert "今日份好心情" in text


# ----------------------------------------------------------------------
# /随机图：取图接口失败
# ----------------------------------------------------------------------


async def test_random_media_fetch_failure_reports_readable_error(make_plugin, make_event):
    """随机图接口抛错时只发可读失败提示，不把异常抛回事件管线。"""
    plugin = make_plugin()
    settings = plugin._random_settings()

    async def failing_fetch(_directory, _content_type):
        raise RandomMediaError("接口 503，请稍后再试")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        plugin, "_random_client", lambda: types.SimpleNamespace(fetch=failing_fetch)
    )
    try:
        results = await _collect(
            plugin._random_media_delivery(make_event("/随机图"), "风景", "image", settings)
        )
    finally:
        monkeypatch.undo()

    assert len(results) == 1
    assert "获取随机媒体失败" in _text(results[0])


# ----------------------------------------------------------------------
# /原图 直查：精确反查、目录+文件名、探测失败、缩略图不得当原图
# ----------------------------------------------------------------------


def _bed_plugin(make_plugin):
    return make_plugin(random_media={"base_url": "https://bed.example.com"})


async def test_lookup_imgbed_file_exact_name_skips_probe(make_plugin, monkeypatch):
    """按名反查恰好命中时直接返回权威直链，不再裸名探测。"""
    plugin = _bed_plugin(make_plugin)
    asked: list[str] = []

    async def fake_by_name(name):
        asked.append(name)
        return {"file_name": "风景.jpg", "url": "https://bed.example.com/file/相册/风景.jpg"}

    async def probe_should_not_run(*_args, **_kwargs):
        raise AssertionError("反查命中后不该再探测裸名直链")

    plugin.vector = types.SimpleNamespace(
        qdrant_ready=True,
        original_by_file_name=fake_by_name,
    )
    monkeypatch.setattr(plugin, "_probe_url", probe_should_not_run)

    name, url, error = await plugin._lookup_imgbed_file("风景.jpg")

    assert asked == ["风景.jpg"]
    assert name == "风景.jpg"
    assert "相册/风景.jpg" in unquote(url)
    assert error == ""


async def test_lookup_imgbed_file_directory_name_uses_bed_path(make_plugin, monkeypatch):
    """带目录的名字不按裸名反查，直接拼图床路径并探测。"""
    plugin = _bed_plugin(make_plugin)
    calls: list[str] = []

    async def should_not_lookup(name):
        calls.append(name)
        raise AssertionError("带目录的名字不该走裸名反查")

    plugin.vector = types.SimpleNamespace(
        qdrant_ready=True,
        original_by_file_name=should_not_lookup,
        original_info_for_thumb_name=should_not_lookup,
    )
    candidate = build_imgbed_file_url(plugin._imgbed_base(), "风景/海边.jpg")

    async def fake_probe(url, *_args, **_kwargs):
        assert url == candidate
        return True, 128

    monkeypatch.setattr(plugin, "_probe_url", fake_probe)

    name, url, error = await plugin._lookup_imgbed_file("风景/海边.jpg")

    assert calls == []
    assert name == "风景/海边.jpg"
    assert url == candidate
    assert error == ""


async def test_lookup_imgbed_file_probe_failure_still_returns_url(make_plugin, monkeypatch):
    """探测结果未知（网络异常）不拦，仍把直链交给回传。"""
    plugin = _bed_plugin(make_plugin)
    candidate = build_imgbed_file_url(plugin._imgbed_base(), "海边.jpg")

    async def unknown_probe(_url, *_args, **_kwargs):
        return None, None

    monkeypatch.setattr(plugin, "_probe_url", unknown_probe)

    name, url, error = await plugin._lookup_imgbed_file("海边.jpg")

    assert name == "海边.jpg"
    assert url == candidate
    assert error == ""


async def test_lookup_imgbed_file_never_returns_thumbnail_as_original(
    make_plugin, monkeypatch
):
    """缩略图反查不到原图时必须报错，绝不把缩略图直链当原图返回。"""
    plugin = _bed_plugin(make_plugin)

    async def no_reverse(_thumb_url):
        return None

    async def probe_should_not_run(*_args, **_kwargs):
        raise AssertionError("缩略图查不到原图时不该继续探测缩略图直链")

    plugin.vector = types.SimpleNamespace(
        qdrant_ready=True,
        original_info_for_thumb=no_reverse,
    )
    monkeypatch.setattr(plugin, "_probe_url", probe_should_not_run)

    name, url, error = await plugin._lookup_imgbed_file("thumbnails/abc.jpg")

    assert name == ""
    assert url == ""
    assert "缩略图" in error

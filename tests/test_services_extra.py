"""服务层补强：签到失败分支、卡片交付清理、溯源错误分支。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from astrbot.api import message_components as Comp

from astrbot_plugin_image_trace.media.image_source import ResolvedImage
from astrbot_plugin_image_trace.search import SearchOutcome


def _image_segment(url: str = "https://img.example.com/a.jpg"):
    return Comp.Image.fromURL(url)


def _features(phash: str = "a" * 64):
    return SimpleNamespace(
        phash=phash, dhash="b" * 16, ahash="c" * 16, width=64, height=64
    )


async def _collect(agen):
    return [item async for item in agen]


def _text(result) -> str:
    return getattr(result, "text", "") or ""


def _outcome(*, streak=1, total=1, already=False):
    return SimpleNamespace(streak=streak, total=total, already=already)


# ----------------------------------------------------------------------
# 签到失败分支
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkin_disabled_store_unavailable_and_missing_sender(
    make_plugin, make_event, monkeypatch
):
    plugin = make_plugin(checkin={"enabled": False})
    results = await _collect(plugin.checkin(make_event("/签到")))
    assert "关闭" in _text(results[0])

    plugin = make_plugin()
    plugin.checkin_store = None
    results = await _collect(plugin.checkin(make_event("/签到")))
    assert "签到存储不可用" in _text(results[0])

    plugin = make_plugin()
    results = await _collect(plugin.checkin(make_event("/签到", sender_id="")))
    assert "无法识别发送者" in _text(results[0])


@pytest.mark.asyncio
async def test_checkin_write_failure_and_random_image_failure(
    make_plugin, make_event, monkeypatch
):
    plugin = make_plugin()
    monkeypatch.setattr(
        plugin.checkin_store,
        "check_in",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    results = await _collect(plugin.checkin(make_event("/签到")))
    assert "签到记录写入失败" in _text(results[0])

    plugin = make_plugin()
    monkeypatch.setattr(plugin.checkin_store, "check_in", lambda *_args: _outcome())
    monkeypatch.setattr(plugin, "_checkin_sender_name", lambda *_: _async_value(""))

    async def boom(*_args, **_kwargs):
        raise RuntimeError("random down")

    monkeypatch.setattr(plugin, "_random_image_candidate", boom)
    results = await _collect(plugin.checkin(make_event("/签到")))
    assert "取图失败" in _text(results[0])

    plugin = make_plugin()
    monkeypatch.setattr(
        plugin.checkin_store, "check_in", lambda *_args: _outcome(already=True)
    )
    monkeypatch.setattr(plugin, "_checkin_sender_name", lambda *_: _async_value(""))
    monkeypatch.setattr(plugin, "_random_image_candidate", boom)
    results = await _collect(plugin.checkin(make_event("/签到")))
    assert "今天已经签到过" in _text(results[0])


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_deliver_checkin_card_path_is_cleaned(
    make_plugin, make_event, monkeypatch, tmp_path
):
    plugin = make_plugin()
    card = tmp_path / "card.jpg"
    card.write_bytes(b"card")
    delivered = []

    async def fake_card_path(*_args, **_kwargs):
        return str(card)

    async def fake_delivery(event, header, blocks, **_kwargs):
        delivered.append((header, blocks, _kwargs))
        yield event.plain_result("sent")

    removed = []
    monkeypatch.setattr(plugin, "_checkin_card_path", fake_card_path)
    monkeypatch.setattr(plugin, "_yield_delivery", fake_delivery)
    monkeypatch.setattr(plugin, "_remove_quiet", lambda path: removed.append(path))

    event = make_event("/签到")
    settings = {"render_mode": "card", "image_mode": "thumbnail", "show_quote": True}
    results = await _collect(
        plugin.services.checkin._deliver_checkin(
            event,
            "标题",
            "语句",
            "页脚",
            "https://cdn.example.com/send.jpg",
            "https://cdn.example.com/original.jpg",
            "a.jpg",
            settings,
            card_source=str(card),
        )
    )

    assert _text(results[0]) == "sent"
    assert delivered and delivered[0][0] == "标题"
    assert removed == [str(card)]


# ----------------------------------------------------------------------
# 溯源错误分支
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_hash_no_file_and_exception_paths(
    make_plugin, make_event, monkeypatch
):
    plugin = make_plugin()
    event = make_event("/溯源", chain=[_image_segment()])

    async def no_file(_seg, *, probe):
        return ResolvedImage(path="", message="图片无法读取")

    monkeypatch.setattr(plugin, "_resolve_local", no_file)
    results = await _collect(plugin._trace_hash(event, _image_segment()))
    assert "图片无法读取" in _text(results[0])

    async def resolved(_seg, *, probe):
        return ResolvedImage(path="C:/tmp/a.jpg", features=_features())

    def boom(*_args):
        raise RuntimeError("hash down")

    monkeypatch.setattr(plugin, "_resolve_local", resolved)
    monkeypatch.setattr(plugin, "_merged_hash_candidates", boom)
    results = await _collect(plugin._trace_hash(event, _image_segment()))
    assert "处理图片时出错" in _text(results[0])


@pytest.mark.asyncio
async def test_trace_auto_no_file_miss_and_error_without_hash(
    make_plugin, make_event, monkeypatch
):
    plugin = make_plugin()
    event = make_event("/溯源", chain=[_image_segment()])

    async def no_file(_seg, *, probe):
        return ResolvedImage(path="", message="图片无法读取")

    monkeypatch.setattr(plugin, "_resolve_local", no_file)
    results = await _collect(plugin._trace_auto(event, _image_segment()))
    assert "图片无法读取" in _text(results[0])

    async def resolved(_seg, *, probe):
        return ResolvedImage(path="C:/tmp/a.jpg", features=_features())

    class _Engine:
        def __init__(self, outcome):
            self._outcome = outcome

        async def search_outcome(self, _image):
            return self._outcome

    monkeypatch.setattr(plugin, "_resolve_local", resolved)
    monkeypatch.setattr(
        plugin,
        "_auto_engine",
        lambda: _Engine(
            SimpleNamespace(
                vector=SearchOutcome(kind="miss", miss_text="向量未命中"),
                hash=None,
            )
        ),
    )
    results = await _collect(plugin._trace_auto(event, _image_segment()))
    text = "\n".join(_text(item) for item in results)
    assert "向量未命中" in text
    assert "哈希图库为空" in text

    monkeypatch.setattr(
        plugin,
        "_auto_engine",
        lambda: _Engine(
            SimpleNamespace(
                vector=SearchOutcome(kind="error", error="vector down"),
                hash=None,
            )
        ),
    )
    results = await _collect(plugin._trace_auto(event, _image_segment()))
    text = "\n".join(_text(item) for item in results)
    assert "向量引擎不可用，且哈希图库为空" in text

    monkeypatch.setattr(
        plugin,
        "_auto_engine",
        lambda: _Engine(
            SimpleNamespace(
                vector=SearchOutcome(kind="error", error="vector down"),
                hash=SimpleNamespace(hits=[], candidate_lines=[]),
            )
        ),
    )
    results = await _collect(plugin._trace_auto(event, _image_segment()))
    text = "\n".join(_text(item) for item in results)
    assert "改用哈希比对" in text


@pytest.mark.asyncio
async def test_vector_search_one_no_file_and_error(
    make_plugin, make_event, monkeypatch
):
    plugin = make_plugin()
    event = make_event("/溯源", chain=[_image_segment()])

    async def no_file(_seg, *, probe):
        return ResolvedImage(path="", message="图片无法读取")

    monkeypatch.setattr(plugin, "_resolve_local", no_file)
    kind, item = await plugin._vector_search_one(event, _image_segment())
    assert kind == "nofile"
    assert "图片无法读取" in _text(item)

    async def resolved(_seg, *, probe):
        return ResolvedImage(path="C:/tmp/a.jpg", mime="image/jpeg")

    class _Engine:
        async def search_outcome(self, _image):
            return SearchOutcome(kind="error", error="vector down")

    monkeypatch.setattr(plugin, "_resolve_local", resolved)
    monkeypatch.setattr(plugin, "_vector_engine", lambda: _Engine())
    kind, item = await plugin._vector_search_one(event, _image_segment())
    assert kind == "error"
    assert item == "vector down"

"""每日签到：状态存储、语句加载与主流程兜底。"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest
from PIL import Image as PILImage

from astrbot_plugin_image_trace.checkin_card import (
    DEFAULT_QUOTES,
    QuoteLibrary,
    compose_poster,
    resolve_font_path,
)
from astrbot_plugin_image_trace.checkin_store import CheckinStore


def test_load_quotes_falls_back_on_non_utf8_file(tmp_path):
    """ANSI/GBK 保存的 quotes.txt 不能让 /签到 抛 UnicodeDecodeError。"""
    path = tmp_path / "quotes.txt"
    path.write_bytes(b"\xff\xfe\x00bad")

    library = QuoteLibrary(str(path), source="builtin")

    assert library._quotes == DEFAULT_QUOTES


def test_quote_library_local_text_priority_and_comment_lines(tmp_path):
    path = tmp_path / "quotes.txt"
    path.write_text("文件里的句子\n", encoding="utf-8")

    library = QuoteLibrary(
        str(path),
        local_text="第一句\n# 注释\n\n第二句",
        source="builtin",
    )

    assert library._quotes == ("第一句", "第二句")


def test_checkin_store_streak_and_total(tmp_path):
    store = CheckinStore(str(tmp_path / "checkin.db"))
    today = date(2026, 10, 30)
    try:
        first = store.check_in("u1", today)
        assert (first.already, first.streak, first.total) == (False, 1, 1)

        repeat = store.check_in("u1", today)
        assert (repeat.already, repeat.streak, repeat.total) == (True, 1, 1)

        next_day = store.check_in("u1", today + timedelta(days=1))
        assert (next_day.already, next_day.streak, next_day.total) == (False, 2, 2)

        after_gap = store.check_in("u1", today + timedelta(days=3))
        assert (after_gap.already, after_gap.streak, after_gap.total) == (False, 1, 3)
    finally:
        store.close()


def test_compose_poster_returns_jpeg(tmp_path):
    font = resolve_font_path("")
    if font is None:
        pytest.skip("当前环境没有可用的中文字体")

    buffer = io.BytesIO()
    PILImage.effect_noise((320, 240), 48).convert("RGB").save(buffer, "JPEG")

    card = compose_poster(
        buffer.getvalue(),
        quote="今天的每一步，都算数。",
        footer="连续签到 3 天 · 累计 5 天",
    )

    assert card is not None
    assert card[:3] == b"\xff\xd8\xff"
    with PILImage.open(io.BytesIO(card)) as image:
        assert image.format == "JPEG"


async def test_checkin_flow_uses_builtin_quote_when_library_raises(
    make_plugin, make_event, monkeypatch
):
    """语句库构造/取句失败时签到仍要发出卡片文案，而不是抛回事件管线。"""
    import astrbot_plugin_image_trace.main as main_module

    plugin = make_plugin(checkin={"render_mode": "simple"})
    event = make_event("/签到", platform="telegram", sender_id="u1")

    async def fake_candidate(_directory, _settings):
        return (
            "https://img.example.com/file/thumb.jpg",
            "https://img.example.com/file/orig.jpg",
            "orig.jpg",
        )

    monkeypatch.setattr(plugin, "_random_image_candidate", fake_candidate)

    def broken_library():
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin, "_quote_library", broken_library)
    monkeypatch.setattr(main_module.random, "choice", lambda _seq: "FALLBACK")

    jpeg = io.BytesIO()
    PILImage.effect_noise((64, 64), 32).convert("RGB").save(jpeg, "JPEG")

    async def fake_prepare(_blocks, *, compress):
        assert compress is True
        return [(jpeg.getvalue(), True)]

    monkeypatch.setattr(plugin, "_prepare_delivery_blocks", fake_prepare)

    results = []
    async for result in plugin.checkin(event):
        results.append(result)

    assert len(results) == 1
    assert results[0].kind == "chain"
    text = "".join(getattr(segment, "text", "") for segment in results[0].chain)
    assert "签到成功" in text
    assert "FALLBACK" in text


async def test_checkin_reports_unavailable_store(make_plugin, make_event, monkeypatch):
    """签到 DB 初始化失败时只提示不可用，插件其余功能不受影响。"""
    import astrbot_plugin_image_trace.main as main_module

    def broken_store(*_args, **_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(main_module, "CheckinStore", broken_store)
    plugin = make_plugin()
    event = make_event("/签到", platform="telegram", sender_id="u1")

    results = []
    async for result in plugin.checkin(event):
        results.append(result)

    assert len(results) == 1
    assert "签到存储不可用" in results[0].text

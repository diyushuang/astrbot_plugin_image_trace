"""每日签到：状态存储、语句加载与主流程兜底。"""

from __future__ import annotations

import io
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image as PILImage

from astrbot_plugin_image_trace.checkin_card import (
    DEFAULT_QUOTES,
    QuoteLibrary,
    compose_poster,
    resolve_font_path,
    strip_emoji,
    wrap_text,
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


def test_compose_poster_downscales_oversized_source(monkeypatch):
    """源图像素超标时要降采样出图，而不是直接放弃合成。

    2026-11-02 线上实录：一张 8736×11648 的原图被拿去合成，旧逻辑按原始像素
    判定超过 50MP 就返回 None，整张卡片降级成没有边框的轻量模式。这里把阈值
    缩到同一数量级来复现：源图 6MP、闸门 10 万像素，旧逻辑必拒；新逻辑先用
    Image.draft 做 DCT 缩放（375×250）再判闸门，应照常出图。
    """
    import astrbot_plugin_image_trace.checkin_card as card_module

    if resolve_font_path("") is None:
        pytest.skip("当前环境没有可用的中文字体")

    monkeypatch.setattr(card_module, "POSTER_MAX_SIDE", 200)
    monkeypatch.setattr(card_module, "_MAX_LOAD_PIXELS", 100_000)

    buffer = io.BytesIO()
    PILImage.effect_noise((3000, 2000), 32).convert("RGB").save(buffer, "JPEG")

    card = compose_poster(
        buffer.getvalue(),
        quote="今天的每一步，都算数。",
        footer="连续签到 3 天 · 累计 5 天",
    )

    assert card is not None, "JPEG 可 DCT 缩放，不该因像素过大放弃合成"
    with PILImage.open(io.BytesIO(card)) as image:
        assert image.width < 300, f"源图未降采样，实际宽度 {image.width}"


def test_strip_emoji_drops_unsupported_glyphs():
    """卡片字体没有 emoji 字形：渲染前剔除，常用符号（★ ♪）保留。"""
    assert strip_emoji("坠入秋日棕调氛围里🍂🍂") == "坠入秋日棕调氛围里"
    assert strip_emoji("晚安 ✅") == "晚安"
    assert strip_emoji("★ 三星 · 音乐 ♪") == "★ 三星 · 音乐 ♪"
    assert strip_emoji("") == ""


def test_wrap_text_treats_newline_as_hard_break():
    """微博文案的换行要当物理行切开，否则 textlength 会因多行文本报错。"""
    from PIL import ImageFont

    font_path = resolve_font_path("")
    if font_path is None:
        pytest.skip("当前环境没有可用的中文字体")
    font = ImageFont.truetype(font_path, 28)

    lines = wrap_text("先锋无可替代\n我有我的答案", font, 600, max_lines=5)
    assert lines == ["先锋无可替代", "我有我的答案"]
    assert all("\n" not in line for line in lines)


def test_compose_poster_accepts_multiline_quote_with_emoji():
    """回归：多行 + emoji 文案曾让卡片合成抛错并静默降级为轻量模式。"""
    if resolve_font_path("") is None:
        pytest.skip("当前环境没有可用的中文字体")
    buffer = io.BytesIO()
    PILImage.effect_noise((320, 320), 20).convert("RGB").save(buffer, "JPEG")

    card = compose_poster(
        buffer.getvalue(),
        quote="坠入秋日棕调氛围里🍂\n@MiuMiu",
        footer="微博@赵今麦angel · 2026-10-19\n连续签到 3 天 · 累计 5 天",
        border_px=20,
        quote_max_lines=5,
    )

    assert card is not None, "多行文案不该让卡片合成失败"
    with PILImage.open(io.BytesIO(card)) as image:
        assert image.format == "JPEG"


def _stub_card_vector(monkeypatch, plugin, thumb):
    """向量引擎伪装成「已启用且能按原图 id 查到缩略图替身」。"""
    seen = []

    async def fake_thumb_url_for(file_id):
        seen.append(file_id)
        return thumb

    monkeypatch.setattr(plugin.vector, "qdrant_ready", True)
    monkeypatch.setattr(plugin.vector, "thumb_url_for", fake_thumb_url_for)
    return seen


async def test_checkin_settings_normalizes_image_mode(make_plugin):
    """image_mode 归一化：大小写无关、非法值回落 thumbnail（默认行为不变）。"""
    assert make_plugin()._checkin_settings()["image_mode"] == "thumbnail"
    assert (
        make_plugin(checkin={"image_mode": "ORIGINAL"})._checkin_settings()["image_mode"]
        == "original"
    )
    assert (
        make_plugin(checkin={"image_mode": "bogus"})._checkin_settings()["image_mode"]
        == "thumbnail"
    )


async def test_checkin_card_source_prefers_thumbnail(make_plugin, monkeypatch):
    """卡片合成一律用缩略图：即使关掉 send_thumbnail（/随机图 发原图）也照查。"""
    plugin = make_plugin(
        checkin={"render_mode": "card"},
        random_media={"base_url": "http://bed.example.com", "send_thumbnail": False},
    )
    settings = plugin._checkin_settings()
    thumb = "http://bed.example.com/thumbnails/a.jpg"
    seen = _stub_card_vector(monkeypatch, plugin, thumb)

    original = "http://bed.example.com/file/a.jpg"
    source = await plugin._checkin_card_source(original, original, settings)

    assert source == thumb
    assert seen == ["a.jpg"], "应按原图对象键查替身"


async def test_checkin_card_source_keeps_thumbnail_send_url(make_plugin, monkeypatch):
    """send_url 本身就是缩略图时直接用，不必再查一次替身。"""
    plugin = make_plugin(random_media={"base_url": "http://bed.example.com"})
    settings = plugin._checkin_settings()
    seen = _stub_card_vector(monkeypatch, plugin, "http://bed.example.com/thumbnails/x.jpg")

    thumb = "http://bed.example.com/thumbnails/a.jpg"
    source = await plugin._checkin_card_source(
        thumb, "http://bed.example.com/file/a.jpg", settings
    )

    assert source == thumb
    assert seen == []


async def test_checkin_card_source_falls_back_to_original(make_plugin, monkeypatch):
    """查不到替身时退回原图，由合成端降采样兜底，不能因此放弃卡片。"""
    plugin = make_plugin(random_media={"base_url": "http://bed.example.com"})
    settings = plugin._checkin_settings()
    _stub_card_vector(monkeypatch, plugin, "")

    original = "http://bed.example.com/file/a.jpg"
    source = await plugin._checkin_card_source(original, original, settings)

    assert source == original


async def test_checkin_card_source_original_mode_returns_original(
    make_plugin, monkeypatch
):
    """image_mode=original 时合成也用原图，且不去查缩略图替身。"""
    plugin = make_plugin(
        checkin={"render_mode": "card", "image_mode": "original"},
        random_media={"base_url": "http://bed.example.com"},
    )
    settings = plugin._checkin_settings()
    seen = _stub_card_vector(monkeypatch, plugin, "http://bed.example.com/thumbnails/a.jpg")

    original = "http://bed.example.com/file/a.jpg"
    thumb = "http://bed.example.com/thumbnails/a.jpg"
    assert await plugin._checkin_card_source(thumb, original, settings) == original
    assert seen == []


class _ThumbVector:
    """最小向量引擎替身：按原图 id 返回固定缩略图直链。"""

    qdrant_ready = True

    def __init__(self, thumb):
        self._thumb = thumb
        self.seen = []

    async def thumb_url_for(self, file_id):
        self.seen.append(file_id)
        return self._thumb


async def test_thumbnail_flag_overrides_random_media_setting(make_plugin, imgbed):
    """同一条取图链路，签到与随机图各按自己的配置决定发哪张。

    两个方向都测：send_thumbnail=True 时签到原图模式仍发原图；
    send_thumbnail=False 时签到缩略图模式仍反查替身。互不影响。
    """
    base = {"base_url": imgbed.base_url}
    thumb = f"{imgbed.base_url}/thumbnails/x.jpg"

    heavy = make_plugin(random_media={**base, "send_thumbnail": True})
    heavy.vector = _ThumbVector(thumb)
    send_url, original_url, _ = await heavy._random_image_candidate(
        "", heavy._random_settings(), thumbnail=False
    )
    assert send_url == original_url
    assert send_url.endswith("/file/x.jpg"), "签到原图模式要盖掉 send_thumbnail=True"

    light = make_plugin(random_media={**base, "send_thumbnail": False})
    light.vector = _ThumbVector(thumb)
    send_url, original_url, _ = await light._random_image_candidate(
        "", light._random_settings(), thumbnail=True
    )
    assert send_url.endswith("/thumbnails/x.jpg"), "签到缩略图模式要盖掉 send_thumbnail=False"
    assert original_url.endswith("/file/x.jpg"), "原图直链仍要留给 /原图 反查"


async def test_weibo_candidate_original_mode_ignores_local_thumb(make_plugin):
    """微博原图模式：发 image_url，且不把本地预缓存缩略图当合成源。"""
    import hashlib

    plugin = make_plugin(checkin={"mode": "weibo"})
    _write_weibo_map(plugin.weibo_dir, with_image=True)
    name = "【微博@赵今麦angel】20261019-01：秋日.jpg"
    key = hashlib.sha1(name.encode("utf-8")).hexdigest()
    Path(plugin.weibo_thumb_dir, f"{key}.jpg").write_bytes(b"cached")

    _, send_url, original_url, _, local_thumb = await plugin._weibo_candidate(
        plugin._random_settings(), image_mode="original"
    )
    assert local_thumb == "", "原图模式不得复用本地缩略图缓存"
    assert send_url == original_url
    assert send_url.endswith("/file/orig.jpg")

    _, send_url, _, _, local_thumb = await plugin._weibo_candidate(
        plugin._random_settings(), image_mode="thumbnail"
    )
    assert local_thumb, "缩略图模式应复用本地预缓存"
    assert send_url.endswith("/file/thumb.jpg")


async def test_checkin_flow_uses_builtin_quote_when_library_raises(
    make_plugin, make_event, monkeypatch
):
    """语句库构造/取句失败时签到仍要发出卡片文案，而不是抛回事件管线。"""
    import astrbot_plugin_image_trace.main as main_module

    plugin = make_plugin(checkin={"render_mode": "simple"})
    event = make_event("/签到", platform="telegram", sender_id="u1")

    async def fake_candidate(_directory, _settings, *, thumbnail=None):
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


def _write_weibo_map(weibo_dir, *, with_image: bool) -> None:
    import json
    from pathlib import Path

    weibo_dir = Path(weibo_dir)
    item = {
        "link": "https://weibo.com/2106192855/RnmlWjQgw",
        "code": "RnmlWjQgw",
        "account": "赵今麦angel",
        "published_at": "2026-10-19 10:10",
        "date": "20261019",
        "text": "先锋无可替代\n我有我的答案",
        "image": (
            {
                "file_name": "【微博@赵今麦angel】20261019-01：秋日.jpg",
                "image_url": "https://img.example.com/file/orig.jpg",
                "thumb_url": "https://img.example.com/file/thumb.jpg",
                "score": 0.99,
            }
            if with_image
            else None
        ),
    }
    weibo_dir.mkdir(parents=True, exist_ok=True)
    (weibo_dir / "weibo_post_map.json").write_text(
        json.dumps({"version": 1, "generated_at": 1, "items": [item]}, ensure_ascii=False),
        encoding="utf-8",
    )


async def test_checkin_weibo_mode_uses_map_copy_and_image(
    make_plugin, make_event, data_dir, monkeypatch
):
    """微博模式：文案来自映射表，不显示链接，卡片/文字带博主与发博日期。"""
    plugin = make_plugin(checkin={"mode": "weibo", "render_mode": "simple"})
    _write_weibo_map(plugin.weibo_dir, with_image=True)
    event = make_event("/签到", platform="telegram", sender_id="u1")

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
    text = "".join(getattr(segment, "text", "") for segment in results[0].chain)
    assert "先锋无可替代" in text
    assert "微博@赵今麦angel" in text
    assert "2026-10-19" in text
    assert "weibo.com" not in text


async def test_checkin_weibo_mode_falls_back_when_no_image(
    make_plugin, make_event, data_dir, monkeypatch
):
    """映射里的微博没有图时回退随机图 + 随机语句，签到不失败。"""
    import astrbot_plugin_image_trace.main as main_module

    plugin = make_plugin(checkin={"mode": "weibo", "render_mode": "simple"})
    _write_weibo_map(plugin.weibo_dir, with_image=False)
    event = make_event("/签到", platform="telegram", sender_id="u1")

    async def fake_candidate(_directory, _settings, *, thumbnail=None):
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

    text = "".join(getattr(segment, "text", "") for segment in results[0].chain)
    assert "FALLBACK" in text


def test_compose_poster_without_quote_skips_text_area():
    """show_quote=False 的卡片只剩「美图 + 页脚」，不能留一条空白文字带。"""
    if resolve_font_path("") is None:
        pytest.skip("当前环境没有可用的中文字体")

    buffer = io.BytesIO()
    PILImage.effect_noise((320, 240), 40).convert("RGB").save(buffer, "JPEG")
    data = buffer.getvalue()
    footer = "连续签到 3 天 · 累计 5 天"

    with_quote = compose_poster(data, quote="今天的每一步，都算数。", footer=footer)
    without = compose_poster(data, quote="", footer=footer, show_quote=False)

    assert with_quote and without
    with PILImage.open(io.BytesIO(with_quote)) as full:
        full_size = full.size
    with PILImage.open(io.BytesIO(without)) as bare:
        assert bare.width == full_size[0], "关掉文字只该减高度，不该改宽度"
        assert bare.height < full_size[1], "关掉文字后画布应更矮（文字区整块省掉）"


async def test_checkin_flow_hides_quote_and_shows_sender(
    make_plugin, make_event, monkeypatch
):
    """show_quote=False：只发美图 + 签到信息，且不再去取语句（省一次接口请求）。"""
    plugin = make_plugin(checkin={"render_mode": "simple", "show_quote": False})
    event = make_event("/签到", platform="telegram", sender_id="u1")

    async def fake_candidate(_directory, _settings, *, thumbnail=None):
        return (
            "https://img.example.com/file/thumb.jpg",
            "https://img.example.com/file/orig.jpg",
            "orig.jpg",
        )

    monkeypatch.setattr(plugin, "_random_image_candidate", fake_candidate)

    def should_not_pick():
        pytest.fail("show_quote=False 时不应再取语句")

    monkeypatch.setattr(plugin, "_quote_library", should_not_pick)

    jpeg = io.BytesIO()
    PILImage.effect_noise((64, 64), 32).convert("RGB").save(jpeg, "JPEG")

    async def fake_prepare(_blocks, *, compress):
        assert compress is True
        return [(jpeg.getvalue(), True)]

    monkeypatch.setattr(plugin, "_prepare_delivery_blocks", fake_prepare)

    results = []
    async for result in plugin.checkin(event):
        results.append(result)

    text = "".join(getattr(segment, "text", "") for segment in results[0].chain)
    assert "签到成功" in text
    assert "测试用户" in text, "show_sender 默认开启，文字里要带昵称"
    assert "连续 1 天" in text
    assert "💬" not in text, "关掉文字后不能再拼语句那一行"


async def test_checkin_flow_hides_sender_name(make_plugin, make_event, monkeypatch):
    """show_sender=False：文字里只剩天数，不带昵称。"""
    import astrbot_plugin_image_trace.main as main_module

    plugin = make_plugin(checkin={"render_mode": "simple", "show_sender": False})
    event = make_event("/签到", platform="telegram", sender_id="u1")

    async def fake_candidate(_directory, _settings, *, thumbnail=None):
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

    text = "".join(getattr(segment, "text", "") for segment in results[0].chain)
    assert "签到成功" in text
    assert "FALLBACK" in text
    assert "测试用户" not in text, "关掉昵称后不该出现发送者名字"


async def test_checkin_sender_name_normalizes_and_truncates(make_plugin, make_event):
    """昵称来自协议端：空白折叠 + 超长截断，缺方法时退回空串。"""
    plugin = make_plugin()
    event = make_event("/签到", sender_id="u1")
    event.get_sender_name = lambda: "  小  双\n二号  "
    assert await plugin._checkin_sender_name(event) == "小 双 二号"

    event.get_sender_name = lambda: "长" * 40
    trimmed = await plugin._checkin_sender_name(event)
    assert len(trimmed) == 17 and trimmed.endswith("…")

    def boom():
        raise RuntimeError("no name")

    event.get_sender_name = boom
    assert await plugin._checkin_sender_name(event) == ""


async def test_checkin_sender_name_prefers_onebot_group_card(make_plugin, make_event):
    """OneBot 原始事件的 sender.card（群名片）优先于框架昵称。"""
    plugin = make_plugin()
    event = make_event("/签到", sender_id="u1", group_id="g1")
    event.message_obj.raw_message = {
        "sender": {"card": "群昵称A", "nickname": "QQ昵称"}
    }

    assert await plugin._checkin_sender_name(event) == "群昵称A"


async def test_checkin_sender_name_member_card_beats_raw_message(make_plugin, make_event):
    """sender.card 存在时优先级最高，raw_message 与框架昵称都排在后面。"""
    plugin = make_plugin()
    event = make_event("/签到", sender_id="u1", group_id="g1")
    event.message_obj.sender = SimpleNamespace(card="群昵称B", nickname="QQ昵称")
    event.message_obj.raw_message = SimpleNamespace(sender={"card": "群昵称C"})

    assert await plugin._checkin_sender_name(event) == "群昵称B"


async def test_checkin_sender_name_falls_back_without_group_card(make_plugin, make_event):
    """群名片为空 / 全空白 / 缺失时，回退到原有昵称，不影响私聊与非 OneBot 平台。"""
    plugin = make_plugin()
    event = make_event("/签到", sender_id="u1")
    assert await plugin._checkin_sender_name(event) == "测试用户"

    event.message_obj.raw_message = {"sender": {"card": "   ", "nickname": "QQ昵称"}}
    assert await plugin._checkin_sender_name(event) == "测试用户"


async def test_checkin_sender_name_reads_object_raw_message_and_truncates(
    make_plugin, make_event
):
    """raw_message 为对象时也能取 card；群名片超长仍按 16 字截断。"""
    plugin = make_plugin()
    event = make_event("/签到", sender_id="u1", group_id="g1")
    event.message_obj.raw_message = SimpleNamespace(
        sender=SimpleNamespace(card="群昵称C")
    )
    assert await plugin._checkin_sender_name(event) == "群昵称C"

    event.message_obj.raw_message = {"sender": {"card": "长" * 40}}
    trimmed = await plugin._checkin_sender_name(event)
    assert len(trimmed) == 17 and trimmed.endswith("…")


class _FakeOneBotBot:
    """最小 OneBot 客户端替身：记录 call_action 并返回固定群成员信息。"""

    def __init__(self, card="", *, error=None):
        self.card = card
        self.error = error
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.error is not None:
            raise self.error
        return {"card": self.card, "nickname": "接口昵称"}


async def test_checkin_sender_name_queries_group_member_card(make_plugin, make_event):
    """事件里没有群名片时，向 OneBot 查 get_group_member_info 取权威 card。"""
    plugin = make_plugin()
    bot = _FakeOneBotBot(card="群昵称API")
    event = make_event(
        "/签到", platform="aiocqhttp", sender_id="u1", group_id="g1", bot=bot
    )

    assert await plugin._checkin_sender_name(event) == "群昵称API"
    assert bot.calls == [
        ("get_group_member_info", {"group_id": "g1", "user_id": "u1"})
    ]


async def test_checkin_sender_name_skips_api_when_event_has_card(make_plugin, make_event):
    """事件里已有群名片就不再请求群成员接口，省一次 OneBot 调用。"""
    plugin = make_plugin()
    bot = _FakeOneBotBot(card="接口群昵称")
    event = make_event(
        "/签到", platform="aiocqhttp", sender_id="u1", group_id="g1", bot=bot
    )
    event.message_obj.raw_message = {"sender": {"card": "事件群昵称"}}

    assert await plugin._checkin_sender_name(event) == "事件群昵称"
    assert bot.calls == []


async def test_checkin_sender_name_api_failure_falls_back(make_plugin, make_event):
    """群成员接口失败/超时时回退事件昵称，签到不能被接口拖垮。"""
    plugin = make_plugin()
    bot = _FakeOneBotBot(error=RuntimeError("api down"))
    event = make_event(
        "/签到", platform="aiocqhttp", sender_id="u1", group_id="g1", bot=bot
    )

    assert await plugin._checkin_sender_name(event) == "测试用户"


async def test_checkin_sender_name_non_onebot_does_not_call_api(make_plugin, make_event):
    """非 OneBot 平台没有群名片概念，不去调用群成员接口。"""
    plugin = make_plugin()
    bot = _FakeOneBotBot(card="接口群昵称")
    event = make_event(
        "/签到", platform="telegram", sender_id="u1", group_id="g1", bot=bot
    )

    assert await plugin._checkin_sender_name(event) == "测试用户"
    assert bot.calls == []


async def test_checkin_settings_defaults_for_text_switches(make_plugin):
    """两个新开关默认都为真（升级即保持原行为 + 带昵称）。"""
    settings = make_plugin()._checkin_settings()
    assert settings["show_quote"] is True
    assert settings["show_sender"] is True
    off = make_plugin(checkin={"show_quote": False, "show_sender": "false"})._checkin_settings()
    assert off["show_quote"] is False
    assert off["show_sender"] is False


async def test_checkin_line_drops_emoji_only_name(make_plugin):
    """昵称全是 emoji 时，页脚不能剩一个孤零零的「·」。"""
    plugin = make_plugin()
    outcome = SimpleNamespace(streak=3, total=10)

    assert plugin._checkin_line("小双", outcome) == "小双 · 连续签到 3 天 · 累计 10 天"
    assert plugin._checkin_line("小双🍂", outcome) == "小双 · 连续签到 3 天 · 累计 10 天"
    assert plugin._checkin_line("🍂🍂", outcome) == "连续签到 3 天 · 累计 10 天"
    assert plugin._checkin_line("", outcome) == "连续签到 3 天 · 累计 10 天"


async def test_checkin_import_writes_file(make_plugin, make_event, data_dir, tmp_path):
    """管理员导入 CSV 后文件落到 weibo/，回复包含条数。"""
    import astrbot.api.message_components as Comp

    source = tmp_path / "微博原创文案.csv"
    source.write_text(
        "序号,发布时间,来源,文案,微博链接\n"
        '1,"2026-10-19 10:10","微博@赵今麦angel","先锋无可替代","https://weibo.com/2106192855/RnmlWjQgw"\n',
        encoding="utf-8",
    )
    plugin = make_plugin()
    file_seg = Comp.File(name="微博原创文案.csv", file=str(source))
    event = make_event("/签到文案导入", chain=[file_seg])

    results = []
    async for result in plugin.checkin_import(event):
        results.append(result)

    assert len(results) == 1
    assert "已导入" in results[0].text
    assert "1 条文案" in results[0].text
    assert (Path(plugin.weibo_dir) / "微博原创文案.csv").is_file()

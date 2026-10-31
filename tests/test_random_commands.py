"""真实驱动命令处理器层的端到端测试（依赖 conftest 注入的 astrbot 桩）。

覆盖 /随机图、/随机视频、/原图、LLM 工具 sendRandomMedia、以及 /溯源 默认
（哈希）路径；全部经真实事件与 mock 图床 HTTP 服务跑通，而非直调私有工具。
"""

from unittest.mock import patch
from urllib.parse import quote, unquote

import pytest
from astrbot.api.message_components import Image
from PIL import Image as PILImage

from astrbot_plugin_image_trace.features import compute_features, heif_available


async def _collect(agen):
    return [item async for item in agen]


def _chain_images(result):
    return [seg for seg in (result.chain or []) if isinstance(seg, Image)]


def _chain_text(result):
    return " ".join(
        seg.text for seg in (result.chain or []) if getattr(seg, "text", None)
    )


async def test_random_image_delivers_scaled_url_with_filename_caption(
    make_plugin, imgbed, make_event
):
    plugin = make_plugin(
        random_media={"base_url": imgbed.base_url},
        image_delivery={"mode": "scaled-url"},
    )

    results = await _collect(plugin.random_image(make_event("/随机图", origin="g1")))

    assert len(results) == 1
    result = results[0]
    assert result.kind == "chain"
    assert "x.jpg" in _chain_text(result)
    images = _chain_images(result)
    assert len(images) == 1
    url = images[0].url
    assert "width=1920" in url
    assert "height=1920" in url
    assert "fallback=original" in url


async def test_random_media_forwards_directory_and_content_type(
    make_plugin, imgbed, make_event
):
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})

    await _collect(plugin.random_image(make_event("/随机图 风景", origin="g1")))
    await _collect(plugin.random_video(make_event("/随机视频 风景", origin="g1")))

    queries = imgbed.random_queries
    assert len(queries) == 2
    assert queries[0]["dir"] == "风景"
    assert queries[0]["content"] == "image"
    assert queries[1]["dir"] == "风景"
    assert queries[1]["content"] == "video"


async def test_random_media_uses_default_dir_when_no_directory_given(
    make_plugin, imgbed, make_event
):
    """未指定目录的无参 /随机图 必须带上配置的 default_dir。

    带 dir 参数是硬需求而非偏好：图床一旦配置了 randomImageAPI.allowedDir，
    不带 dir 的裸 /random 会被判为「目录不允许」直接 403。此前本插件虽暴露了
    default_dir 配置项，却从未真正传给客户端，导致该配置形同虚设。
    """
    plugin = make_plugin(
        random_media={"base_url": imgbed.base_url, "default_dir": "12、日常分享"}
    )

    await _collect(plugin.random_image(make_event("/随机图", origin="g1")))

    queries = imgbed.random_queries
    assert len(queries) == 1
    assert queries[0]["dir"] == "12、日常分享"
    assert queries[0]["content"] == "image"


async def test_random_media_explicit_directory_overrides_default_dir(
    make_plugin, imgbed, make_event
):
    """/随机图 带目录时以命令里的目录为准，不被 default_dir 覆盖。"""
    plugin = make_plugin(
        random_media={"base_url": imgbed.base_url, "default_dir": "12、日常分享"}
    )

    await _collect(plugin.random_image(make_event("/随机图 风景", origin="g1")))

    queries = imgbed.random_queries
    assert len(queries) == 1
    assert queries[0]["dir"] == "风景"


async def test_default_dir_pool_picks_a_member_each_call(
    make_plugin, imgbed, make_event
):
    """default_dir 给多个目录时，每次请求从中挑一个，从而尽量覆盖整库。

    图床 /random 的 dir 只收单值，所以「不指定目录时随机整库」只能靠客户端轮换
    目录来逼近；这里断言每次请求都落在池内，且池内多个成员确实都被用到。
    """
    pool = "10、商务宣传,11、时尚刊物,12、日常分享"
    plugin = make_plugin(
        random_media={"base_url": imgbed.base_url, "default_dir": pool}
    )

    for _ in range(40):
        await _collect(plugin.random_image(make_event("/随机图", origin="g1")))

    dirs = [q["dir"] for q in imgbed.random_queries]
    assert len(dirs) == 40
    assert set(dirs) <= set(pool.split(",")), f"出现了池外目录: {set(dirs)}"
    assert len(set(dirs)) > 1, "池内多项应被轮换使用，而不是恒取同一个"


async def test_original_image_returns_unprocessed_url_without_local_download(
    make_plugin, imgbed, make_event
):
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    origin = "g1"
    await _collect(plugin.random_image(make_event("/随机图", origin=origin)))

    with patch.object(plugin, "_download") as download_spy:
        results = await _collect(plugin.original_image(make_event("/原图", origin=origin)))

    download_spy.assert_not_called()
    result = results[0]
    assert result.kind == "chain"
    images = _chain_images(result)
    assert len(images) == 1
    url = images[0].url
    assert url == f"{imgbed.base_url}/file/x.jpg"
    for token in ("width=", "height=", "fit=", "fallback="):
        assert token not in url


async def test_original_image_strips_imgbed_processing_params(make_plugin, imgbed, make_event):
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    origin = "g1"
    seeded = f"{imgbed.base_url}/file/x.jpg?width=1920&height=1920&fit=cover&fallback=original"
    plugin.history.remember(origin, seeded, display_name="x.jpg")

    results = await _collect(plugin.original_image(make_event("/原图 x.jpg", origin=origin)))

    url = _chain_images(results[0])[0].url
    assert url == f"{imgbed.base_url}/file/x.jpg"


async def test_original_image_reports_missing_name_on_bed(
    make_plugin, imgbed, make_event, monkeypatch
):
    """会话历史里没有、图床里也探测不到时，提示要给出尝试过的完整直链。

    直查通路让 /原图 不再受「本会话回传过什么」限制，因此「找不到」的语义
    也变了：从「只能找回本会话最近回传过的图片」变成「图床里没有这个文件」，
    并附上探测用的地址，方便用户核对图床目录层级。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    origin = "g1"
    plugin.history.remember(origin, f"{imgbed.base_url}/file/x.jpg", display_name="x.jpg")

    async def always_missing(url, cache=None):
        return False, None

    monkeypatch.setattr(plugin, "_probe_url", always_missing)

    results = await _collect(plugin.original_image(make_event("/原图 nope.jpg", origin=origin)))

    assert results[0].kind == "plain"
    assert "图床里没有找到" in results[0].text
    assert "/file/nope.jpg" in results[0].text
    assert imgbed.base_url not in results[0].text, "群聊文案不得回显图床主机"


async def test_original_image_looks_up_bed_when_history_is_empty(
    make_plugin, imgbed, make_event
):
    """会话历史为空时也能按文件名直取图床原图——这是本次修复的核心场景。

    修复前这里只会回「本会话还没有回传过图片」；现在应当直接拼出图床直链、
    探测到文件存在后把原图 URL 发出去。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    event = make_event("/原图 sunset.jpg", origin="fresh")
    assert plugin.history.count("fresh") == 0, "本用例要求会话历史为空"

    results = await _collect(plugin.original_image(event))

    images = _chain_images(results[0])
    assert len(images) == 1
    assert images[0].url == f"{imgbed.base_url}/file/sunset.jpg"
    for token in ("width=", "height=", "fit=", "fallback="):
        assert token not in images[0].url


async def test_original_image_falls_back_to_quoted_image_name(
    make_plugin, imgbed, make_event
):
    """无参 /原图 在会话历史为空时，按引用图/消息里的文件名直查图床。"""
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    event = make_event(
        "/原图",
        origin="fresh",
        chain=[Image.fromURL(f"{imgbed.base_url}/file/quoted.jpg?width=1920")],
    )

    results = await _collect(plugin.original_image(event))

    images = _chain_images(results[0])
    assert len(images) == 1
    assert images[0].url == f"{imgbed.base_url}/file/quoted.jpg"


def _stub_name_lookup(monkeypatch, plugin, mapping):
    """把向量引擎伪装成「已启用且能按 file_name 反查」，记录被查的名字。

    mapping: {裸文件名: 带完整目录的原图直链}；不在映射里的返回 None。
    """
    seen = []

    async def fake_original_by_file_name(file_name):
        seen.append(file_name)
        url = mapping.get(file_name)
        if not url:
            return None
        return {"url": url, "file_name": file_name}

    monkeypatch.setattr(plugin.vector, "qdrant_ready", True)
    monkeypatch.setattr(plugin.vector, "original_by_file_name", fake_original_by_file_name)
    return seen


async def test_original_image_reverses_bare_name_to_authoritative_url(
    make_plugin, imgbed, make_event, monkeypatch
):
    """核心回归：`/原图 <裸文件名>` 必须先反查拿回**带完整目录**的权威直链。

    线上故障：`/原图 【微博@潮流合伙人】20200207-04：赵今麦海报.jpg` 报
    「图床里没有找到」。根因不是文件名有问题（前缀在 1.6.1 已清洗干净），
    而是图床的 `/file/{path}` **只认完整对象键**——该文件在
    `7、综艺节目/爱奇艺《潮流合伙人》/《潮流合伙人》图集/` 下面，而插件拼的是
    `/file/{裸名}`，**必然 404**。实测 A/B 对照（内网与公网域名结果一致）：

        裸文件名   /file/{裸名}              → HTTP 404
        带完整目录 /file/7、综艺节目/…/{裸名} → HTTP 200

    payload.image_url 里存的正是那条带目录的直链，所以只能反查，不能猜目录。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    name = "【微博@潮流合伙人】20200207-04：赵今麦海报.jpg"
    authoritative = (
        f"{imgbed.base_url}/file/7%E3%80%81%E7%BB%BC%E8%89%BA%E8%8A%82%E7%9B%AE/"
        "%E7%88%B1%E5%A5%87%E8%89%BA%E3%80%8A%E6%BD%AE%E6%B5%81%E5%90%88%E4%BC%99%E4%BA%BA"
        "%E3%80%8B/%E3%80%8A%E6%BD%AE%E6%B5%81%E5%90%88%E4%BC%99%E4%BA%BA%E3%80%8B%E5%9B%BE"
        "%E9%9B%86/%E3%80%90%E5%BE%AE%E5%8D%9A%40%E6%BD%AE%E6%B5%81%E5%90%88%E4%BC%99%E4%BA%BA"
        "%E3%80%9120200207-04%EF%BC%9A%E8%B5%B5%E4%BB%8A%E9%BA%A6%E6%B5%B7%E6%8A%A5.jpg"
    )
    seen = _stub_name_lookup(monkeypatch, plugin, {name: authoritative})
    # 探测必须打桩成「不存在」：若实现退回裸名拼 URL，就会在这里被拦下并报
    # 「图床里没有找到」——正好用来证明走的是反查而非老逻辑
    monkeypatch.setattr(plugin, "_probe_url", _probe_missing)

    event = make_event(f"/原图 {name}", origin="fresh")
    results = await _collect(plugin.original_image(event))

    images = _chain_images(results[0])
    assert len(images) == 1, f"应发出反查到的原图，实际 {_chain_text(results[0])!r}"
    assert images[0].url == authoritative, "必须是带完整目录的权威直链"
    assert seen == [name], "应按裸文件名去反查"


async def test_original_image_reverses_bare_thumbnail_name(
    make_plugin, imgbed, make_event, monkeypatch
):
    """URL 丢了 `thumbnails/` 段、只剩缩略图改名后的文件名时，仍要反查回原图。

    线上形态：用户引用机器人刚发的缩略图，QQ 把图转存到自己的 CDN，插件手上
    只有 `1789…_【微博_…】.jpg` 这种裸名。此时

    - `is_thumbnail_url(quoted)` 判不出来（路径里没有 `thumbnails` 段）；
    - `original_by_file_name` 也命中不了（真名里是 `@`、且没有时间戳前缀）；
    - 裸名拼 `/file/{name}` 必然 404。

    没有这条兜底，用户就只能收到「图床里没有找到」——而图其实就在图床里。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    thumb_name = "1789658616018_【微博_赵今麦工作室official】剧照.jpg"
    original = (
        f"{imgbed.base_url}/file/12、日常分享/"
        "【微博@赵今麦工作室official】剧照.jpg"
    )

    _stub_name_lookup(monkeypatch, plugin, {})  # 按真名反查落空

    async def fake_by_thumb_name(name):
        # 走到这里时毫秒时间戳前缀可能已被 strip_leading_decoration 剥掉，
        # 但被改名的缩略图主体（`@`→`_`）还在——反查靠的正是它
        assert name == thumb_name or name == "【微博_赵今麦工作室official】剧照.jpg", name
        return {"url": original, "file_name": "【微博@赵今麦工作室official】剧照.jpg"}

    monkeypatch.setattr(
        plugin.vector, "original_info_for_thumb_name", fake_by_thumb_name
    )

    async def boom(*a, **k):
        raise AssertionError("缩略图名反查已命中，不应再探测图床")

    monkeypatch.setattr(plugin, "_probe_url", boom)

    results = await _collect(plugin.original_image(make_event(f"/原图 {thumb_name}", origin="fresh")))

    images = _chain_images(results[0])
    assert len(images) == 1, f"应发出反查到的原图，实际 {_chain_text(results[0])!r}"
    assert unquote(images[0].url) == original


async def test_original_image_never_sends_thumbnail_url(
    make_plugin, imgbed, make_event, monkeypatch
):
    """最终直链仍指向 `thumbnails/` 时必须中止，而不是静默发出一张 720px 小图。

    走到发送前的每一条路径都「自认为」拿到的是原图；这里做最后一道校验，
    因为静默发小图对用户来说就是「/原图 又没生效」，无从判断问题在哪。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    thumb_url = f"{imgbed.base_url}/file/thumbnails/1789658616018_【微博_赵今麦工作室official】剧照.jpg"
    _stub_name_lookup(monkeypatch, plugin, {"x.jpg": thumb_url})

    results = await _collect(plugin.original_image(make_event("/原图 x.jpg", origin="fresh")))

    assert _chain_images(results[0]) == [], "缩略图直链绝不能发出去"
    assert "缩略图" in results[0].text


async def test_original_image_reports_ambiguous_when_duplicates_differ(
    make_plugin, imgbed, make_event, monkeypatch
):
    """同名多份且**内容不同**时，提示必须区别于「图床里没有找到」。

    两种落空的处置完全不同：未进索引要去 /溯源 补索引，同名多份要给目录。
    对后者报「图床里没有找到」是**错的**——图确实在图床里，只是有 N 份内容
    不同，用户照着「没有找到」去核对只会白跑。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    _stub_name_lookup(monkeypatch, plugin, {})  # 反查放弃（同名但不等价）
    monkeypatch.setattr(plugin, "_probe_url", _probe_missing)

    async def two_matches(file_name):
        return 2

    monkeypatch.setattr(plugin.vector, "file_name_matches", two_matches)

    results = await _collect(
        plugin.original_image(make_event("/原图 dup.jpg", origin="fresh"))
    )

    text = results[0].text
    assert "图床里没有找到" not in text, "同名多份时不得报「没有找到」，图是存在的"
    assert "2 份" in text and "目录" in text, f"应提示补目录，实际 {text!r}"


async def test_original_image_keeps_missing_message_when_name_not_indexed(
    make_plugin, imgbed, make_event, monkeypatch
):
    """库里确实没有这个名字（命中 0）时，仍报「图床里没有找到」并附尝试过的路径。

    这条是上一条的对照：诊断只用于**区分**，不能把「真的没有」也说成歧义。
    附的是路径而非完整直链：图床地址可能是内网 IP/域名，群聊不回显主机。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    _stub_name_lookup(monkeypatch, plugin, {})
    monkeypatch.setattr(plugin, "_probe_url", _probe_missing)

    async def zero_matches(file_name):
        return 0

    monkeypatch.setattr(plugin.vector, "file_name_matches", zero_matches)

    results = await _collect(
        plugin.original_image(make_event("/原图 nope.jpg", origin="fresh"))
    )

    assert "图床里没有找到" in results[0].text
    assert "/file/nope.jpg" in results[0].text
    assert imgbed.base_url not in results[0].text, "群聊文案不得回显图床主机"


async def test_original_image_diagnostic_failure_does_not_break_message(
    make_plugin, imgbed, make_event, monkeypatch
):
    """诊断查询自身出错时，必须退回原本的「没有找到」提示，不能把失败浮上来。

    诊断只是文案增强，不能成为新的故障点。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    _stub_name_lookup(monkeypatch, plugin, {})
    monkeypatch.setattr(plugin, "_probe_url", _probe_missing)

    async def boom(file_name):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(plugin.vector, "file_name_matches", boom)

    results = await _collect(
        plugin.original_image(make_event("/原图 nope.jpg", origin="fresh"))
    )

    assert "图床里没有找到" in results[0].text


async def test_original_image_name_lookup_hit_skips_probe(
    make_plugin, imgbed, make_event, monkeypatch
):
    """反查命中时不应再去探测图床：多一次往返没有意义（直链来自入库登记）。"""
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    name = "x.jpg"
    target = f"{imgbed.base_url}/file/12%E3%80%81%E6%97%A5%E5%B8%B8/{name}"
    _stub_name_lookup(monkeypatch, plugin, {name: target})

    async def boom(*a, **k):
        raise AssertionError("反查已命中，不应再探测图床")

    monkeypatch.setattr(plugin, "_probe_url", boom)

    results = await _collect(plugin.original_image(make_event(f"/原图 {name}", origin="fresh")))

    assert _chain_images(results[0])[0].url == target


async def test_original_image_name_lookup_falls_back_to_bare_probe(
    make_plugin, imgbed, make_event, monkeypatch
):
    """反查落空（未进索引 / 同名多份 / 向量检索关闭）时退回裸名直查的老逻辑。

    对「图床根目录下的文件」这种旧路径仍然有效——不能因为反查不到就让
    /原图 失去原本可用的能力。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    seen = _stub_name_lookup(monkeypatch, plugin, {})  # 一律查不到

    results = await _collect(
        plugin.original_image(make_event("/原图 sunset.jpg", origin="fresh"))
    )

    assert seen == ["sunset.jpg"], "应先尝试反查"
    assert _chain_images(results[0])[0].url == f"{imgbed.base_url}/file/sunset.jpg"


async def test_original_image_name_with_directory_skips_lookup(
    make_plugin, imgbed, make_event, monkeypatch
):
    """入参自带目录（含 `/`）时不做反查，直接交给图床按路径取，且目录必须保住。

    反查只匹配裸 file_name，带目录的名字在这一步本来就命中不了；而用户既然
    给了相对路径，按路径取更准。

    这里同时钉住一个曾经真实存在的坑：query 若走 clean_display_name 清洗，
    它内部的 media_filename 只取末段，会把这条路径削成 `海报.jpg`——用户
    **给足了信息反而查不到**。故改用保目录的 strip_leading_decoration，
    装饰前缀照样被剥掉，路径一字不动。

    探测打桩为「存在」：本用例只关心「拼出来的 URL 对不对、有没有多此一举
    反查」，不依赖 mock 图床能否真的服务多级路径。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    seen = _stub_name_lookup(monkeypatch, plugin, {})
    relative = "7、综艺节目/爱奇艺《潮流合伙人》/《潮流合伙人》图集/海报.jpg"

    async def probe_ok(url, cache=None, **kwargs):
        return True, 1234

    async def boom_thumb(name):
        raise AssertionError("带目录的名字不应触发缩略图名反查")

    monkeypatch.setattr(plugin, "_probe_url", probe_ok)
    monkeypatch.setattr(plugin.vector, "original_info_for_thumb_name", boom_thumb)

    results = await _collect(
        plugin.original_image(make_event(f"/原图 {relative}", origin="fresh"))
    )

    assert seen == [], "带目录的名字不应触发反查"
    url = _chain_images(results[0])[0].url
    assert unquote(url) == f"{imgbed.base_url}/file/{relative}", "目录层级必须完整保留"


async def test_original_image_keeps_directory_when_decoration_prefixed(
    make_plugin, imgbed, make_event, monkeypatch
):
    """带装饰前缀 + 多级目录同时出现时：前缀要剥掉、目录要留住。

    两个清洗目标作用于同一串，必须同一个函数一次达成——剥前缀靠
    strip_decoration_prefix（原地），保目录靠「不取末段」。此前用
    clean_display_name 只能满足前者：它会把这串削成 `海报.jpg`，路径整个丢掉，
    而图床按裸名取不到，用户「给足了信息反而查不到」。

    剥完仍含 `/`，故按设计跳过反查、直接按路径交给图床。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    seen = _stub_name_lookup(monkeypatch, plugin, {})
    relative = "7、综艺节目/爱奇艺《潮流合伙人》/海报.jpg"

    async def probe_ok(url, cache=None, **kwargs):
        return True, 1234

    monkeypatch.setattr(plugin, "_probe_url", probe_ok)

    results = await _collect(
        plugin.original_image(make_event(f"/原图 🖼️ {relative}", origin="fresh"))
    )

    url = _chain_images(results[0])[0].url
    assert "%F0%9F%96%BC" not in url, "装饰前缀不得进入直链"
    assert "🖼️" not in unquote(url)
    assert unquote(url) == f"{imgbed.base_url}/file/{relative}", "目录层级必须完整保留"
    assert seen == [], "剥完仍含目录，按设计不做反查"


async def test_strip_leading_decoration_preserves_directory_but_strips_prefix():
    """random_media.strip_leading_decoration 的单测：只动开头，其余一字不改。

    这是与 clean_display_name 的分工点——后者取末段（丢目录），适合展示与
    按裸名查库；前者原地剥前缀，适合用户手写的查询串。
    """
    from astrbot_plugin_image_trace import random_media

    relative = "7、综艺节目/爱奇艺《潮流合伙人》/海报.jpg"
    assert random_media.strip_leading_decoration(relative) == relative
    assert random_media.strip_leading_decoration(f"🖼️ {relative}") == relative
    assert random_media.strip_leading_decoration(f"1. 🖼️ {relative}") == relative
    # 时间戳前缀（用户从缩略图消息复制出来的形态）同样剥掉，路径不动
    assert random_media.strip_leading_decoration(f"1789658616018_{relative}") == relative
    assert random_media.strip_leading_decoration("") == ""
    assert random_media.strip_leading_decoration(None) == ""
    # 对照：clean_display_name 会丢目录（两者差异显式钉住，避免日后误用）
    assert random_media.clean_display_name(relative) == "海报.jpg"


async def test_original_image_reports_missing_when_both_paths_fail(
    make_plugin, imgbed, make_event, monkeypatch
):
    """反查落空 + 探测「确定不存在」时，仍如实报「找不到」并附上尝试过的直链。"""
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    _stub_name_lookup(monkeypatch, plugin, {})
    monkeypatch.setattr(plugin, "_probe_url", _probe_missing)

    results = await _collect(plugin.original_image(make_event("/原图 ghost.jpg", origin="fresh")))

    assert results[0].kind == "plain"
    assert "图床里没有找到" in results[0].text
    assert not _chain_images(results[0])


async def _probe_missing(url, cache=None, **kwargs):
    """探测桩：明确报告「不存在」（对应图床 404）。"""
    return False, None


async def test_original_image_strips_caption_prefix_from_quoted_name(
    make_plugin, imgbed, make_event
):
    """引用一条配文带 `🖼️ ` 的消息发 /原图，前缀不得被当成文件名拼进 URL。

    这是线上 404 的直接复现：回显的直链里带着 `%F0%9F%96%BC%EF%B8%8F%20`
    （即 `🖼️ `），说明前缀被编码进了路径。修复点是 random_media.clean_display_name
    新增的装饰前缀剥离——它同时服务于「展示」与「直查入参」两个用途。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    # 名字里带真实的中文方括号与 @：既验证前缀被剥掉，也验证标点没被误伤
    quoted = f"{imgbed.base_url}/file/" + quote("🖼️ 【抖音@赵今麦工作室】花絮.jpeg")
    event = make_event("/原图", origin="fresh", chain=[Image.fromURL(quoted)])

    results = await _collect(plugin.original_image(event))

    images = _chain_images(results[0])
    assert len(images) == 1
    url = images[0].url
    assert "%F0%9F%96%BC" not in url, "配文前缀不应被拼进直链"
    assert "🖼️" not in url
    # 直链是百分号编码的（中文/`@` 都要编码，`@`→%40），故解码后再比对内容：
    # 前缀剥掉、其余一字不动（`【`/`】`/`@` 完好）
    assert unquote(url) == f"{imgbed.base_url}/file/【抖音@赵今麦工作室】花絮.jpeg"


async def test_original_image_strips_decoration_prefix_in_explicit_query(
    make_plugin, imgbed, make_event
):
    """用户手动写 `/原图 🖼️ 图.jpg`（从配文里复制粘贴）也应取到图。"""
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    event = make_event("/原图 🖼️ 图.jpg", origin="fresh")

    results = await _collect(plugin.original_image(event))

    images = _chain_images(results[0])
    assert len(images) == 1
    assert unquote(images[0].url) == f"{imgbed.base_url}/file/图.jpg"
    assert "%F0%9F%96%BC" not in images[0].url, "配文前缀不应被拼进直链"


async def test_original_image_without_bed_base_reports_configuration(
    make_plugin, make_event
):
    """既没回传记录、也没配图床站点时，提示要指向配置项而不是空手而归。"""
    plugin = make_plugin()

    results = await _collect(plugin.original_image(make_event("/原图 sunset.jpg", origin="g1")))

    assert results[0].kind == "plain"
    assert "未配置图床站点地址" in results[0].text


async def test_original_image_on_empty_history(make_plugin, make_event):
    plugin = make_plugin(random_media={"base_url": "https://img.example.com"})

    results = await _collect(plugin.original_image(make_event("/原图", origin="fresh")))

    assert results[0].kind == "plain"
    assert "还没有回传过图片" in results[0].text


async def test_llm_tool_disabled_by_config(make_plugin, make_event):
    plugin = make_plugin(
        random_media={"base_url": "https://img.example.com", "enable_llm": False}
    )

    results = await _collect(plugin.send_random_media(make_event("来张随机图", origin="g1")))

    assert results[0].kind == "plain"
    assert "已被禁用" in results[0].text


def test_original_and_register_are_distinct_command_names(make_plugin):
    plugin = make_plugin()

    assert plugin.original_image.command_name == "原图"
    assert plugin.original_repair.command_name == "原图修复"


async def test_original_repair_renames_dirty_history_keys(make_plugin, imgbed, make_event):
    """`/原图修复` 把带装饰前缀的旧键改名成干净键，改名而非删除。

    旧键是 1.6.1 之前写入的污染（`_remember_blocks` 把配文前缀连文件名一起登记）。
    它们是纯内存 LRU，不会自己消失——只在该会话累积到上限被淘汰或进程重启时
    才可能挤掉，所以需要这个显式入口。

    这里直接往 `_history` 里塞脏键来模拟旧数据（新写入路径已不会产生脏键）。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    url = f"{imgbed.base_url}/file/a.jpg"
    plugin.history.remember("g1", url, display_name="🖼️ a.jpg")
    plugin.history.remember("g2", url, display_name="1789658616018_a.jpg")
    assert plugin.history.count("g1") == 2  # 主键 + 脏别名键

    results = await _collect(plugin.original_repair(make_event("/原图修复", origin="g1")))

    assert results[0].kind == "plain"
    assert "已清理 2 条" in results[0].text
    # 命中数量不变（改名而非删除），但名字已是干净形态
    assert plugin.history.find("g1", "a.jpg")[0] == "found"
    assert plugin.history.find("g2", "a.jpg")[0] == "found"


async def test_original_repair_reports_nothing_to_do_on_clean_history(
    make_plugin, imgbed, make_event
):
    """干净历史下不谎报处理量。"""
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    plugin.history.remember("g1", f"{imgbed.base_url}/file/a.jpg")

    results = await _collect(plugin.original_repair(make_event("/原图修复", origin="g1")))

    assert results[0].kind == "plain"
    assert "没有带前缀的脏记录" in results[0].text


async def test_random_image_caption_has_no_decoration_inside_filename(
    make_plugin, imgbed, make_event
):
    """配文只在前缀位置有一个图标，文件名体内不得再混入装饰字符。

    配文形态是 `{icon} {filename}`：图标属排版、文件名属内容。这条断言守住
    「两者不互相渗透」——若文件名体内的字符被挪进/复制进前缀区，就会在这里现形。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})

    results = await _collect(plugin.random_image(make_event("/随机图", origin="g1")))

    text = _chain_text(results[0]).strip()
    assert text.startswith("🖼️ ")
    assert text.count("🖼️") == 1
    assert text[len("🖼️ ") :] == "x.jpg"
    assert plugin.register_image.command_name == "登记原图"
    assert plugin.original_image.command_name != plugin.register_image.command_name
    # 注：AstrBot 按命令首个词精确匹配（/原图 与 /登记原图 首词不同）这一路由
    # 行为发生在 AstrBot 框架内，本桩不执行命令路由，无法在此运行时证明；
    # 此处只证明两者被注册为不同的命令名，路由假设记为未在本仓库验证。


def test_random_image_alias_matches_documented_alias(make_plugin):
    """README 指令表把 /随机图片 记为 /随机图 的别名，二者必须同步。

    文档与注册名漂移过一次（README 写了别名但代码没注册），用断言钉住，
    避免以后改文档忘改代码、或反之。
    """
    plugin = make_plugin()

    assert plugin.random_image.command_name == "随机图"
    assert "随机图片" in plugin.random_image.command_alias
    assert plugin.random_video.command_name == "随机视频"
    assert plugin.random_video.command_alias == set()


async def test_trace_hash_path_still_hits(make_plugin, imgbed, make_event, tmp_path):
    plugin = make_plugin(random_media={"base_url": imgbed.base_url}, search_engine="hash")

    image_path = tmp_path / "origin.jpg"
    PILImage.effect_noise((96, 96), 48).convert("RGB").save(image_path, format="JPEG")

    feats = compute_features(str(image_path), plugin.hash_size)
    plugin.library.add_if_absent(
        {
            "phash": feats.phash,
            "dhash": feats.dhash,
            "ahash": feats.ahash,
            "width": feats.width,
            "height": feats.height,
            "image_url": f"{imgbed.base_url}/file/orig.jpg",
            "note": "测试原图",
            "source": "register",
        }
    )

    event = make_event("/溯源", origin="g1", chain=[Image.fromFileSystem(str(image_path))])
    results = await _collect(plugin.trace(event))

    assert results, "溯源应回传命中结果"
    assert "溯源命中" in _chain_text(results[0])


async def test_trace_accepts_heic_query_image(make_plugin, imgbed, make_event, tmp_path):
    """HEIC 查询图要能正常溯源，而不是被判成「不是有效图片」。

    修复前 HEIC 会先被判无效、再白跑一次必然失败的兜底下载，用户只得到
    「未能获取图片内容」。装了 pillow-heif 后这条链路应当完整走通。
    """
    if not heif_available():
        pytest.skip("未安装 pillow-heif")

    plugin = make_plugin(random_media={"base_url": imgbed.base_url}, search_engine="hash")
    heic_path = tmp_path / "photo.heic"
    PILImage.new("RGB", (64, 48), color=(200, 30, 40)).save(heic_path, format="HEIF")

    feats = compute_features(str(heic_path), plugin.hash_size)
    plugin.library.add_if_absent(
        {
            "phash": feats.phash,
            "dhash": feats.dhash,
            "ahash": feats.ahash,
            "width": feats.width,
            "height": feats.height,
            "image_url": f"{imgbed.base_url}/file/photo.jpg",
            "note": "HEIC 原图",
            "source": "register",
        }
    )

    event = make_event("/溯源", origin="g1", chain=[Image.fromFileSystem(str(heic_path))])
    results = await _collect(plugin.trace(event))

    assert results, "HEIC 查询图应当正常溯源"
    assert "溯源命中" in _chain_text(results[0])


async def test_original_image_not_reported_missing_when_bed_denies_access(
    make_plugin, imgbed, make_event, monkeypatch
):
    """探测结果「说不准」时不得报「找不到」：403/429 只说明这次没读到。

    此前 classify_probe_status 把所有 4xx 判成「不存在」，于是文件确实存在、
    但被防盗链或限流拦下时，/原图 会误报「图床里没有找到 x.jpg」并拒绝发送。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    origin = "g1"

    async def forbidden(url, cache=None, **kwargs):
        return None, None  # 探测不出（403/429/网络异常一律是这一档）

    monkeypatch.setattr(plugin, "_probe_url", forbidden)

    results = await _collect(plugin.original_image(make_event("/原图 a.jpg", origin=origin)))

    assert results, "探测说不准时应照常把直链发出去，而不是拒绝"
    assert "图床里没有找到" not in _chain_text(results[0])


def _stub_thumbnail(monkeypatch, plugin, *, thumb, expect_id=None):
    """把向量引擎伪装成「已启用且能查到替身」，并记录被查询的文件 id。"""
    seen = []

    async def fake_thumb_url_for(file_id):
        seen.append(file_id)
        if expect_id is not None:
            assert file_id == expect_id, f"应按原图 id 查询，实际 {file_id!r}"
        return thumb

    monkeypatch.setattr(plugin.vector, "qdrant_ready", True)
    monkeypatch.setattr(plugin.vector, "thumb_url_for", fake_thumb_url_for)
    return seen


def _stub_reverse_lookup(monkeypatch, plugin, mapping, names=None):
    """把向量引擎伪装成「已启用且能按 thumb_url 反查原图」，记录被查的缩略图直链。

    mapping: {缩略图直链: 原图直链}；不在映射里的返回 None（模拟「查不到该替身」）。
    names:   {缩略图直链: 原图真名}；对应 payload.file_name（图床未改名的原始名）。
    """
    seen = []
    names = names or {}

    async def fake_original_info_for_thumb(thumb_url):
        seen.append(thumb_url)
        url = mapping.get(thumb_url)
        if not url:
            return None
        return {"url": url, "file_name": names.get(thumb_url, "")}

    monkeypatch.setattr(plugin.vector, "qdrant_ready", True)
    monkeypatch.setattr(
        plugin.vector, "original_info_for_thumb", fake_original_info_for_thumb
    )
    return seen


def test_bed_file_id_restores_percent_escapes():
    """图床 id 是对象键，含中文与 @ #；URL 里被编码过，必须还原成原始键。

    还原错了就算得出错误的点 id，替身永远查不到——而 `#` 若不还原还会被 URL
    解析器当成片段起点，连路径都是断的。
    """
    from astrbot_plugin_image_trace.main import ImageTracePlugin

    raw = "12、日常分享/【微博@赵今麦】/【微博@赵今麦】20250716-02：林其乐剧照.jpg"
    encoded = "/".join(quote(seg, safe="") for seg in raw.split("/"))
    assert ImageTracePlugin._bed_file_id(f"http://bed/file/{encoded}") == raw
    # 含 # 的 id：编码后必须还原，不能被当成片段
    hashed = "12、日常分享/【微博】20201016-16：#第13届金鹰节开幕式#.jpg"
    enc2 = "/".join(quote(seg, safe="") for seg in hashed.split("/"))
    assert ImageTracePlugin._bed_file_id(f"http://bed/file/{enc2}") == hashed
    # 非图床直链返回空串，调用方据此按「无替身」处理
    assert ImageTracePlugin._bed_file_id("http://bed/other/x.jpg") == ""


async def test_random_image_sends_thumbnail_but_remembers_original(
    make_plugin, imgbed, make_event, monkeypatch
):
    """随机图发缩略图替身（小文件好发），但会话历史必须留原图直链。

    这是「发送用替身、请求原图给原图」两条规则的交汇点：只要历史记成缩略图，
    此后 /原图 就只能拿回那张 720px 的小图，等于把替身当成了原图。
    """
    plugin = make_plugin(
        random_media={"base_url": imgbed.base_url},
        image_delivery={"mode": "original-url"},
    )
    thumb = f"{imgbed.base_url}/file/thumbnails/1789658616018_x.jpg"
    seen = _stub_thumbnail(monkeypatch, plugin, thumb=thumb, expect_id="x.jpg")

    results = await _collect(plugin.random_image(make_event("/随机图", origin="g1")))

    assert seen == ["x.jpg"], "应按图床返回的原图 id 去查替身"
    images = _chain_images(results[0])
    assert len(images) == 1
    assert images[0].url == thumb, "发出去的应当是缩略图替身"
    remembered = plugin.history.latest("g1")
    assert remembered is not None
    _name, remembered_url = remembered
    assert remembered_url == f"{imgbed.base_url}/file/x.jpg", "历史里必须是原图，不能是替身"


async def test_original_image_after_random_returns_true_original(
    make_plugin, imgbed, make_event, monkeypatch
):
    """发过缩略图之后，/原图 仍须给出真正的原图——端到端的核心保证。"""
    plugin = make_plugin(
        random_media={"base_url": imgbed.base_url},
        image_delivery={"mode": "original-url"},
    )
    origin = "g1"
    _stub_thumbnail(
        monkeypatch, plugin, thumb=f"{imgbed.base_url}/file/thumbnails/1789658616018_x.jpg"
    )

    await _collect(plugin.random_image(make_event("/随机图", origin=origin)))
    results = await _collect(plugin.original_image(make_event("/原图", origin=origin)))

    url = _chain_images(results[0])[0].url
    assert url == f"{imgbed.base_url}/file/x.jpg"
    assert "thumbnails" not in url, "/原图 绝不能返回缩略图"


async def test_random_image_falls_back_to_original_without_thumbnail(
    make_plugin, imgbed, make_event, monkeypatch
):
    """查不到替身（或替身为空）时照发原图：替身只是提速手段，不能影响可用性。"""
    plugin = make_plugin(
        random_media={"base_url": imgbed.base_url},
        image_delivery={"mode": "original-url"},
    )
    _stub_thumbnail(monkeypatch, plugin, thumb=None)

    results = await _collect(plugin.random_image(make_event("/随机图", origin="g1")))

    url = _chain_images(results[0])[0].url
    assert url == f"{imgbed.base_url}/file/x.jpg"


async def test_random_image_send_thumbnail_can_be_disabled(
    make_plugin, imgbed, make_event, monkeypatch
):
    """send_thumbnail=false 时始终发原图（替身查都不查）。"""
    plugin = make_plugin(
        random_media={"base_url": imgbed.base_url, "send_thumbnail": False},
        image_delivery={"mode": "original-url"},
    )
    seen = _stub_thumbnail(
        monkeypatch, plugin, thumb=f"{imgbed.base_url}/file/thumbnails/1_x.jpg"
    )

    results = await _collect(plugin.random_image(make_event("/随机图", origin="g1")))

    assert seen == [], "关闭后不应再去查替身"
    assert _chain_images(results[0])[0].url == f"{imgbed.base_url}/file/x.jpg"


async def test_random_caption_uses_original_filename(
    make_plugin, imgbed, make_event, monkeypatch
):
    """配文用原图文件名：缩略图被图床改过名（时间戳前缀 + @ 换 _），显示它没有意义。"""
    plugin = make_plugin(
        random_media={"base_url": imgbed.base_url},
        image_delivery={"mode": "original-url"},
    )
    _stub_thumbnail(
        monkeypatch, plugin, thumb=f"{imgbed.base_url}/file/thumbnails/1789658616018_x.jpg"
    )

    results = await _collect(plugin.random_image(make_event("/随机图", origin="g1")))

    text = _chain_text(results[0])
    assert "x.jpg" in text
    assert "1789658616018" not in text, "配文不应出现缩略图的时间戳前缀"


# ----------------------------------------------------------------------
# /原图 直查命中缩略图时必须反查回原图
# ----------------------------------------------------------------------


async def test_original_image_reverses_quoted_thumbnail_to_original(
    make_plugin, imgbed, make_event, monkeypatch
):
    """核心回归：引用上一条 /随机图 发出的缩略图再发 /原图，必须拿回原图。

    线上故障：/原图 的直查分支只按名字拼 URL，而引用消息里的名字正是
    图床改名后的缩略图名（`thumbnails/<时间戳>_…`），于是拼出缩略图直链、
    把 720px 小图又发了一遍。修复后必须先按 thumb_url 反查回原图。

    匹配形态取**实测部署的存储形态**（Qdrant payload 里 thumb_url 是未编码的
    原始串，image_url 是编码过的），因此这里反查入参也用原始未编码形态。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    thumb_name = "1789662583418_【微博_赵今麦工作室official】20260410-05：花絮.jpg"
    thumb_url = f"{imgbed.base_url}/file/thumbnails/{thumb_name}"
    # 原图直链在图床 payload 里是**编码后**的形态
    original_url = (
        f"{imgbed.base_url}/file/%E3%80%90%E5%BE%AE%E5%8D%9A%40%E8%B5%B5%E4%BB%8A%E9%BA%A6"
        "%E5%B7%A5%E4%BD%9C%E5%AE%A4official%E3%80%9120260410-05%EF%BC%9A%E8%8A%B1%E7%B5%AE.jpg"
    )
    seen = _stub_reverse_lookup(
        monkeypatch,
        plugin,
        {thumb_url: original_url},
        # payload.file_name 是图床**未改名**的原始名（@ 完好）
        names={thumb_url: "【微博@赵今麦工作室official】20260410-05：花絮.jpg"},
    )

    event = make_event("/原图", origin="fresh", chain=[Image.fromURL(thumb_url)])
    results = await _collect(plugin.original_image(event))

    images = _chain_images(results[0])
    assert len(images) == 1
    assert images[0].url == original_url
    assert "thumbnails" not in images[0].url, "/原图 绝不能返回缩略图"
    assert seen == [thumb_url], "应按引用图里的缩略图直链去反查"
    # 展示名取自 payload.file_name：`@`→`_` 不可逆，清洗缩略图名还原不出真名
    text = _chain_text(results[0])
    assert "【微博@赵今麦工作室official】" in text, f"应使用图床记录的真名，实际 {text!r}"
    assert "1789662583418" not in text, "文案不得含缩略图的时间戳前缀"
    assert "_赵今麦工作室official" not in text, "文案不得带 @→_ 的改名痕迹"


async def test_original_image_strips_thumbnail_timestamp_in_quoted_name(
    make_plugin, imgbed, make_event, monkeypatch
):
    """反查入参必须保留图床改名后的时间戳前缀——那是存储形态的一部分。

    容易搞错的一点：配文展示时要去掉时间戳前缀（那是噪声），但**反查匹配**
    时必须一字不差地用图床存储的原串，否则永远匹配不上。这条测试把两种口径
    分开钉住：反查入了带前缀的原串，而最终回传的是反查结果。
    """
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    ts = "1789658616018"
    raw_name = f"{ts}_【微博_赵今麦angel】20200228-01：《重生》剧照.jpg"
    thumb_url = f"{imgbed.base_url}/file/thumbnails/{raw_name}"
    original_url = (
        f"{imgbed.base_url}/file/12%E3%80%81%E6%97%A5%E5%B8%B8%E5%88%86%E4%BA%AB/"
        "%E3%80%90%E5%BE%AE%E5%8D%9A%40%E8%B5%B5%E4%BB%8A%E9%BA%A6angel%E3%80%91"
        "20200228-01%EF%BC%9A%E3%80%8A%E9%87%8D%E7%94%9F%E3%80%8B%E5%89%A7%E7%85%A7.jpg"
    )
    seen = _stub_reverse_lookup(monkeypatch, plugin, {thumb_url: original_url})

    event = make_event("/原图", origin="fresh", chain=[Image.fromURL(thumb_url)])
    results = await _collect(plugin.original_image(event))

    assert _chain_images(results[0])[0].url == original_url
    assert seen and ts in seen[0], f"反查应使用图床原样的缩略图直链，实际 {seen}"
    # 回传文案里的名字不得带时间戳前缀
    assert ts not in _chain_text(results[0]), "给用户看的文案不应含时间戳前缀"


async def test_original_image_thumbnail_without_reverse_reports_honestly(
    make_plugin, imgbed, make_event, monkeypatch
):
    """反查不到原图时宁可说清楚，也绝不把缩略图当原图发出去。"""
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    thumb_url = f"{imgbed.base_url}/file/thumbnails/1789658616018_x.jpg"
    _stub_reverse_lookup(monkeypatch, plugin, {})

    event = make_event("/原图", origin="fresh", chain=[Image.fromURL(thumb_url)])
    results = await _collect(plugin.original_image(event))

    assert results[0].kind == "plain"
    assert "缩略图" in results[0].text
    assert not _chain_images(results[0]), "反查失败时不得发出任何图片"


async def test_original_image_history_thumbnail_is_reversed(
    make_plugin, imgbed, make_event, monkeypatch
):
    """历史里被写进缩略图时也要纠正——这是防御，不依赖上游永远正确。"""
    plugin = make_plugin(random_media={"base_url": imgbed.base_url})
    origin = "g1"
    thumb_url = f"{imgbed.base_url}/file/thumbnails/1789658616018_x.jpg"
    original_url = f"{imgbed.base_url}/file/%E5%8E%9F%E5%9B%BE.jpg"
    _stub_reverse_lookup(monkeypatch, plugin, {thumb_url: original_url})

    # 绕过 _remember_blocks 的闸门，直接往历史里塞一条缩略图，模拟旧版本残留
    plugin.history.remember(origin, thumb_url, display_name="x.jpg")

    results = await _collect(plugin.original_image(make_event("/原图", origin=origin)))

    images = _chain_images(results[0])
    assert len(images) == 1
    assert images[0].url == original_url
    assert "thumbnails" not in images[0].url


async def test_random_image_never_remembers_thumbnail_in_history(
    make_plugin, imgbed, make_event, monkeypatch
):
    """历史永不记缩略图——即便调用方把替身当原图传进来也要被拦下。

    /原图 的取值完全依赖这段历史；一旦记进缩略图，此后 /原图 只能拿回小图。
    """
    plugin = make_plugin(
        random_media={"base_url": imgbed.base_url},
        image_delivery={"mode": "original-url"},
    )
    thumb_url = f"{imgbed.base_url}/file/thumbnails/1789658616018_x.jpg"

    plugin._remember_blocks(
        make_event("/随机图", origin="g1"), [{"url": thumb_url, "file_name": "x.jpg"}]
    )

    assert plugin.history.latest("g1") is None, "缩略图直链不得进入 /原图 历史"


# ---------------------------------------------------------------------------
# 内网直连（random_media.lan_base_url）
# ---------------------------------------------------------------------------


async def test_random_image_lan_mode_requests_and_delivers_via_lan(
    make_plugin, imgbed, make_event
):
    """配置 lan_base_url：接口请求与回传直链都走内网源，公网 base 只作主机匹配。"""
    plugin = make_plugin(
        random_media={
            "base_url": "https://img.example.com",
            "lan_base_url": imgbed.base_url,
        },
        image_delivery={"mode": "original-url"},
    )

    results = await _collect(plugin.random_image(make_event("/随机图", origin="g1")))

    images = _chain_images(results[0])
    assert len(images) == 1
    assert images[0].url.startswith(imgbed.base_url), "回传直链应保持内网源"


async def test_random_video_lan_mode_keeps_public_url_for_qq_official(
    make_plugin, imgbed, make_event
):
    """官方机器人发视频由腾讯服务器按 URL 拉流：内网直连下须改写回公网直链。

    /random 走内网时返回的直链本身就是内网源，所以“回退公网”必须是显式
    换源（swap 到 base_url），而不是简单保留改写前的地址。
    """
    from astrbot.api.message_components import Video

    plugin = make_plugin(
        random_media={
            "base_url": "https://img.example.com",
            "lan_base_url": imgbed.base_url,
        },
        image_delivery={"mode": "original-url"},
    )

    results = await _collect(
        plugin.random_video(make_event("/随机视频", origin="q1", platform="qq_official"))
    )

    videos = [seg for seg in results[0].chain if isinstance(seg, Video)]
    assert len(videos) == 1
    assert videos[0].url.startswith("https://img.example.com/"), (
        "官方机器人的视频直链必须是公网地址（腾讯服务器要能访问）"
    )


async def test_random_video_lan_mode_uses_lan_url_for_aiocqhttp(
    make_plugin, imgbed, make_event
):
    """aiocqhttp（NapCat 与 AstrBot 同机）由本机拉流：视频可安全走内网直连。"""
    from astrbot.api.message_components import Video

    plugin = make_plugin(
        random_media={
            "base_url": "https://img.example.com",
            "lan_base_url": imgbed.base_url,
        },
        image_delivery={"mode": "original-url"},
    )

    results = await _collect(
        plugin.random_video(make_event("/随机视频", origin="n1", platform="aiocqhttp"))
    )

    videos = [seg for seg in results[0].chain if isinstance(seg, Video)]
    assert len(videos) == 1
    assert videos[0].url.startswith(imgbed.base_url)


async def test_random_image_mention_prefix_only_on_aiocqhttp(
    make_plugin, imgbed, make_event
):
    """@发送者 前缀只给 aiocqhttp；qq_official 会把 At 转成 `<qqbot-at-user
    id=.../>` 标记原文拼进纯文本（纯文本消息不渲染），其余平台不得附带。"""
    from astrbot.api.message_components import At

    plugin = make_plugin(
        random_media={"base_url": imgbed.base_url},
        image_delivery={"mode": "original-url"},
    )

    result = (
        await _collect(plugin.random_image(make_event("/随机图", origin="a1", platform="aiocqhttp")))
    )[0]
    assert isinstance(result.chain[0], At), "aiocqhttp 应保留 @发送者 前缀"

    result = (
        await _collect(plugin.random_image(make_event("/随机图", origin="q1", platform="qq_official")))
    )[0]
    assert not any(isinstance(seg, At) for seg in result.chain), (
        "qq_official 不得附带 At（会显示为标记原文）"
    )


# ---------------------------------------------------------------------------
# qq_official 富媒体格式体检（假 jpg 的 HEIF/AVIF/WEBP 会被腾讯打回）
# ---------------------------------------------------------------------------


class _SniffStream:
    """模拟 aiohttp StreamReader：按请求长度消费、读尽返回 EOF。"""

    def __init__(self, data: bytes):
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._data)
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


class _SniffResp:
    def __init__(self, data: bytes, status: int = 200):
        self.status = status
        self.headers = {}
        self.content = _SniffStream(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class _SniffClient:
    async def request(self, method, url, *, prepare=None, timeout=None):
        return self._resp

    def __init__(self, resp):
        self._resp = resp


def _jpeg_head() -> bytes:
    import io as _io

    from PIL import Image as PILImage

    buf = _io.BytesIO()
    PILImage.effect_noise((32, 32), 32).convert("RGB").save(buf, format="JPEG")
    return buf.getvalue()


def _webp_bytes() -> bytes:
    import io as _io

    from PIL import Image as PILImage

    buf = _io.BytesIO()
    PILImage.effect_noise((32, 32), 32).convert("RGB").save(buf, format="WEBP")
    return buf.getvalue()


def _tiff_bytes() -> bytes:
    import io as _io

    from PIL import Image as PILImage

    buf = _io.BytesIO()
    PILImage.effect_noise((32, 32), 32).convert("RGB").save(buf, format="TIFF")
    return buf.getvalue()


async def test_offbox_image_delivery_passes_standard_jpeg(make_plugin):
    """标准 JPEG 头直接放行：返回 None，按原 URL 交付。"""
    plugin = make_plugin()

    async def getter():
        return _SniffClient(_SniffResp(_jpeg_head()))

    plugin._get_http = getter
    assert await plugin._offbox_image_delivery("http://x/l.jpg", {}) is None


async def test_offbox_image_delivery_passes_webp_per_docs(make_plugin):
    """官方文档展示口径含 webp：放行按 URL 原样交付（动图免转码）。"""
    plugin = make_plugin()

    async def getter():
        return _SniffClient(_SniffResp(_webp_bytes()))

    plugin._get_http = getter
    assert await plugin._offbox_image_delivery("http://x/l.webp", {}) is None


async def test_offbox_image_delivery_transcodes_undocumented_format(make_plugin, data_dir):
    """PIL 可解码但文档清单外的格式（TIFF）→ 转码为本地 JPEG 交付。"""
    import os

    from astrbot_plugin_image_trace.image_delivery import sniff_image_format

    plugin = make_plugin()

    async def getter():
        return _SniffClient(_SniffResp(_tiff_bytes()))

    plugin._get_http = getter
    outcome = await plugin._offbox_image_delivery("http://x/l.jpg", {})
    assert outcome is not None and outcome[0] == "path"
    path = outcome[1]
    try:
        with open(path, "rb") as f:
            assert sniff_image_format(f.read(16)) == "JPEG"
    finally:
        os.path.exists(path) and os.remove(path)


async def test_offbox_image_delivery_falls_back_to_thumb(make_plugin):
    """PIL 解不了的假格式 → 换发缩略图替身直链，且收进内网源。"""

    plugin = make_plugin()

    class _Vec:
        qdrant_ready = True

        async def thumb_url_for(self, file_id):
            return "https://img.example.com/file/thumbnails/1789658616018_x.jpg"

    plugin.vector = _Vec()

    async def getter():
        # ftyp 壳 + 无效负载：嗅探判 HEIF，PIL 也解不开
        return _SniffClient(_SniffResp(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64))

    plugin._get_http = getter
    settings = {
        "base_url": "https://img.example.com",
        "lan_base_url": "http://172.20.0.1:7658",
    }
    outcome = await plugin._offbox_image_delivery(
        "http://172.20.0.1:7658/file/abc.jpg", settings
    )
    assert outcome == ("url", "http://172.20.0.1:7658/file/thumbnails/1789658616018_x.jpg")


async def test_yield_delivery_offbox_rewrites_fake_format_to_path(
    make_plugin, imgbed, make_event
):
    """体检下沉在 _yield_delivery：qq_official 的假格式 block 被改写为本地 path，
    历史仍记原始直链（/原图 可找回），标准格式 block 原样保留。"""
    plugin = make_plugin(
        random_media={"base_url": imgbed.base_url},
        image_delivery={"mode": "original-url"},
    )

    async def getter():
        # TIFF：PIL 可解码、官方文档清单外 → 应转码为本地 JPEG
        return _SniffClient(_SniffResp(_tiff_bytes()))

    plugin._get_http = getter

    event = make_event("/随机图", origin="g1", platform="qq_official")
    fake_url = f"{imgbed.base_url}/file/fake.jpg"
    results = await _collect(
        plugin._yield_delivery(event, "🖼️ 假格式.jpg", [{"url": fake_url, "file_name": "fake.jpg"}])
    )

    images = [seg for seg in results[0].chain if isinstance(seg, Image)]
    assert len(images) == 1
    assert getattr(images[0], "path", None), "假格式应改写为本地转码文件交付"
    assert not images[0].url, "改写后不得再带外链 URL（腾讯拒收假格式）"
    assert plugin.history.latest("g1")[1] == fake_url, "历史必须仍记原始直链"

    import os

    assert not os.path.exists(images[0].path), "转码产物应在投递完成后清理"

    # 标准格式 block：体检放行、URL 原样保留
    plugin._get_http = None

    class _OkClient:
        async def request(self, *a, **k):
            return _SniffResp(_jpeg_head())

    async def ok_getter():
        return _OkClient()

    plugin._http = None
    plugin._get_http = ok_getter
    results = await _collect(
        plugin._yield_delivery(
            make_event("/随机图", origin="g2", platform="qq_official"),
            "标准格式",
            [{"url": f"{imgbed.base_url}/file/ok.jpg"}],
        )
    )
    images = [seg for seg in results[0].chain if isinstance(seg, Image)]
    assert len(images) == 1 and images[0].url, "标准格式应按 URL 原样交付"


async def test_yield_delivery_non_offbox_skips_rewrite(make_plugin, imgbed, make_event):
    """aiocqhttp（本机拉流平台）不做体检：零下载开销，URL 原样交付。"""
    plugin = make_plugin(
        random_media={"base_url": imgbed.base_url},
        image_delivery={"mode": "original-url"},
    )
    results = await _collect(
        plugin._yield_delivery(
            make_event("/随机图", origin="n1", platform="aiocqhttp"),
            "napcat",
            [{"url": f"{imgbed.base_url}/file/x.jpg"}],
        )
    )
    images = [seg for seg in results[0].chain if isinstance(seg, Image)]
    assert len(images) == 1 and images[0].url


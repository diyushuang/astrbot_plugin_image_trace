"""/溯源 向量命中的回传消息结构与配文。

需求：先单独发一条命中提示，再发**一条**包含全部相似图的消息（此前是每张图
各发一条）；该消息内图文交替，每张图上方紧跟它自己的配文；每张图的配文只
出现一遍（此前 OneBot 路径把 block 文案同时当标题一起拼进同一段文本，同一句
被算两遍）；压缩图的配文只留一句用法说明。

这些用例真实驱动 `plugin.trace(event)`，不直接调用内部发送函数。
"""

from __future__ import annotations

import asyncio
import io
import types

import pytest
from PIL import Image as PILImage

from astrbot_plugin_image_trace import main as main_module
from astrbot_plugin_image_trace.features import compute_features
from astrbot_plugin_image_trace.main import ImageTracePlugin  # noqa: F401  见 conftest 桩


class _FakeBot:
    """OneBot 原生接口替身：记录 call_action 的调用，便于断言直发内容。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        return {"status": "ok"}


@pytest.fixture
def vector_stub():
    """把向量引擎替换为"已启用"的最简替身。

    /溯源 只读 vector.enabled 与 vector.config_hint()；检索本身由用例通过
    _vector_search_one 注入，避免为了跑通投递层去搭 Qdrant 与向量服务。
    """

    def apply(plugin):
        plugin.vector = types.SimpleNamespace(
            enabled=True, config_hint=lambda: "向量引擎未配置"
        )
        return plugin

    return apply


def _hits(*names: str) -> list[dict]:
    """构造向量命中：image_url 为 ImgBed 标准 /file/ 直链（可被缩放）。"""
    return [
        {
            "id": str(index),
            "score": 0.95 - index * 0.01,
            "duplicate_count": 1,
            "duplicates": [],
            "payload": {
                "image_url": f"https://img.example.com/file/{name}",
                "file_name": name,
                "width": 1024,
                "height": 768,
            },
        }
        for index, name in enumerate(names)
    ]


async def _collect(agen):
    return [item async for item in agen]


def _install_hits(monkeypatch, plugin, hits):
    """让真实 /溯源 走向量命中分支：_trace_vector 会消费这里的异步生成器。"""

    async def fake_search(event, _seg):
        return "hit", plugin._vector_hits_delivery(event, hits)

    monkeypatch.setattr(plugin, "_vector_search_one", fake_search)


def _chain_images(result):
    from astrbot.api.message_components import Image

    return [seg for seg in result.chain if isinstance(seg, Image)]


def _chain_text(result) -> str:
    """把一条消息里所有文本段拼起来，用于统计文案出现次数。"""
    from astrbot.api.message_components import Plain

    return "\n".join(
        str(seg.text) for seg in result.chain if isinstance(seg, Plain)
    )


def _image_segment():
    from astrbot.api.message_components import Image

    return Image.fromURL("https://qq.example.com/a.jpg")


async def test_trace_sends_prompt_then_one_message_with_all_images(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """第一条是命中提示，第二条一条消息装下全部相似图。"""
    plugin = vector_stub(make_plugin(search_engine="vector"))
    hits = _hits("a.jpg", "b.jpg", "c.jpg")
    _install_hits(monkeypatch, plugin, hits)

    results = await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    assert len(results) == 2, "应恰好两条：命中提示 + 全部相似图"

    prompt, gallery = results
    assert prompt.kind == "plain"
    assert "向量检索命中" in prompt.text
    assert not _chain_images(prompt) if prompt.chain else True

    assert gallery.kind == "chain"
    assert len(_chain_images(gallery)) == 3, "三张相似图必须在同一条消息里"


async def test_gallery_message_does_not_repeat_the_hit_prompt(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """第二条消息不再重复命中提示标题（第一条已经说过）。"""
    plugin = vector_stub(make_plugin(search_engine="vector"))
    _install_hits(monkeypatch, plugin, _hits("a.jpg", "b.jpg"))

    results = await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    assert "向量检索命中" not in _chain_text(results[1])


async def test_onebot_gallery_caption_appears_once_per_image(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """OneBot 直发路径下每张图的配文只出现一遍，且图文交替、一一对应。

    重复的根因是调用方把 block 文案同时当标题传入，标题与逐图配文被拼进
    同一段文本，同一句于是被算两遍。
    """
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    bot = _FakeBot()
    _install_hits(monkeypatch, plugin, _hits("alpha.jpg", "beta.jpg"))
    event = make_event(
        "/溯源",
        platform="aiocqhttp",
        bot=bot,
        group_id="12345",
        chain=[_image_segment()],
    )

    results = await _collect(plugin.trace(event))

    # 第一条提示走 OneBot，第二条相似图也走 OneBot
    assert len(results) == 0, "两条都由 OneBot 原生发出，不应再走消息链"
    group_calls = [params for action, params in bot.calls if action == "send_group_msg"]
    # 提示那条只带文本；含图片段的那次才是相似图消息
    image_calls = [
        params
        for params in group_calls
        if any(seg["type"] == "image" for seg in params["message"])
    ]
    assert len(image_calls) == 1, "全部相似图应合并为一次直发"

    message = image_calls[0]["message"]
    text = "".join(seg["data"]["text"] for seg in message if seg["type"] == "text")
    assert text.count("alpha.jpg") == 1
    assert text.count("beta.jpg") == 1
    assert len([seg for seg in message if seg["type"] == "image"]) == 2
    assert event.stopped is True


async def test_onebot_gallery_text_first_then_all_images(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """消息段必须是「文字集中 → 图片集中」，且配文带序号可与图片对应。

    这是线上「前三张正方形缩略图、第四张尺寸正确」的修复：QQ 会把
    「text 段 + 紧邻其后的 image 段」合并渲染成图文混排卡片，卡片内的图片按
    行内插图裁成正方形缩略图。此前图文交替排列，每张图都紧跟在一段文字后面，
    于是几张图全是正方形。把全部 text 提到最前、image 连续排在末尾后，QQ 按
    普通大图渲染，宽高比得以保留。

    代价是配文不再紧贴各自的图片，因此必须前置序号（1. / 2. / 3.）来建立
    文字与图片的对应关系。
    """
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    bot = _FakeBot()
    _install_hits(monkeypatch, plugin, _hits("alpha.jpg", "beta.jpg", "gamma.jpg"))
    event = make_event(
        "/溯源",
        platform="aiocqhttp",
        bot=bot,
        group_id="12345",
        chain=[_image_segment()],
    )

    await _collect(plugin.trace(event))

    message = [
        params["message"]
        for action, params in bot.calls
        if action == "send_group_msg"
        and any(seg["type"] == "image" for seg in params["message"])
    ][0]

    # @ → 一段文本 → 三张图片，中间不再夹文字段（否则 QQ 会重新分组成卡片）
    assert [seg["type"] for seg in message] == ["at", "text", "image", "image", "image"]

    # 全部配文集中在同一段文本里，且每张图都靠序号保留可辨识的对应关系
    text = message[1]["data"]["text"]
    for index, name in enumerate(("alpha.jpg", "beta.jpg", "gamma.jpg"), 1):
        assert f"{index}." in text and name in text, f"{name} 的配文应带序号 {index}."
        assert text.count(name) == 1, f"{name} 的配文只应出现一遍"


async def test_onebot_image_segments_carry_download_timeout(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """每个 image 段都带 OneBot 标准的 timeout 字段，减少协议端下载超时。

    URL 段由协议端自行下载，大图 + 多图时下载耗时会挤占 NapCat 的 sendMsg
    回调窗口，超时后结果是「未知」因而不敢重发（用户可能收不到图）。显式放宽
    下载超时窗口可降低这种概率；base64 段不经下载，带该字段无副作用。
    """
    from astrbot_plugin_image_trace.common import ONEBOT_IMAGE_TIMEOUT

    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    bot = _FakeBot()
    _install_hits(monkeypatch, plugin, _hits("alpha.jpg", "beta.jpg"))
    event = make_event(
        "/溯源",
        platform="aiocqhttp",
        bot=bot,
        group_id="12345",
        chain=[_image_segment()],
    )

    await _collect(plugin.trace(event))

    message = _image_calls(bot)[0]["message"]
    images = [seg for seg in message if seg["type"] == "image"]
    assert images
    assert all(
        seg["data"]["timeout"] == ONEBOT_IMAGE_TIMEOUT for seg in images
    ), "每个图片段都应带下载超时"


async def test_onebot_image_timeout_honours_config_override(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """onebot_image_timeout 配置生效，且绝不会被写成 0。

    image 段的 timeout 是协议端下载图片的窗口。若用户误配 0/负数/非数字，
    绝不能原样写进消息段——那等于告诉协议端「立刻放弃下载」，图片必然拉不到。
    非法值一律收敛到安全区间 [10, 300]，非数字则落回默认值。
    """
    from astrbot_plugin_image_trace.common import ONEBOT_IMAGE_TIMEOUT

    for configured, expected in (
        (120, 120),  # 正常配置：原样生效
        (0, 10),  # 0 非法 → 收敛到下限 10（而非原样写 0）
        (-5, 10),  # 负数非法 → 收敛到下限 10
        (9999, 300),  # 越上界 → 收敛到 300
        ("abc", ONEBOT_IMAGE_TIMEOUT),  # 非数字 → 默认值
    ):
        plugin = vector_stub(
            make_plugin(
                search_engine="vector",
                image_delivery={
                    "mode": "scaled-url",
                    "onebot_image_timeout": configured,
                },
            )
        )
        bot = _FakeBot()
        _install_hits(monkeypatch, plugin, _hits("alpha.jpg"))
        event = make_event(
            "/溯源",
            platform="aiocqhttp",
            bot=bot,
            group_id="12345",
            chain=[_image_segment()],
        )

        await _collect(plugin.trace(event))

        images = [
            seg
            for seg in _image_calls(bot)[0]["message"]
            if seg["type"] == "image"
        ]
        assert images, f"配置 {configured!r} 时应有图片段"
        assert all(seg["data"]["timeout"] == expected for seg in images), (
            f"onebot_image_timeout={configured!r} 应生效为 {expected}"
        )


async def test_small_image_caption_has_no_compressed_claim(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """图片不超过 max_side 时图床不会缩放，配文不得再声称「已压缩」。

    这正是线上报的现象：文字写「已压缩」、用户收到的却是原图。根因是压缩
    标识由「URL 是否被改写」推导，而图床在图片不大时会按 fallback=original
    原样返回；现在改为按证据判定，尺寸不超限直接按原图处理。
    """
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    _install_hits(monkeypatch, plugin, _hits_with_size("scenery.jpg", 1024, 768))

    results = await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    caption = _chain_text(results[1])
    assert "已压缩" not in caption
    assert "scenery.jpg" in caption
    url = _chain_images(results[1])[0].url
    assert "width=" not in url, "不超过 max_side 的图不必再追加缩放参数"


async def test_caption_claims_compression_only_when_measured_smaller(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """大图 + 实测缩放版更小时，才写「已压缩，可发送 /原图 获取原图」。"""
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    _install_hits(monkeypatch, plugin, _hits_with_size("big.jpg", 4000, 3000))

    async def probe(url, cache=None, **kwargs):
        return (True, 120_000) if "width=" in url else (True, 900_000)

    monkeypatch.setattr(plugin, "_probe_url", probe)

    results = await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    caption = _chain_text(results[1])
    assert "已压缩，可发送 /原图 获取原图" in caption
    assert "已压缩 /原图" not in caption
    assert "big.jpg" in caption
    assert "width=" in _chain_images(results[1])[0].url


async def test_caption_omits_compression_when_bed_returns_original(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """实测缩放版并不更小（图床按 fallback=original 返回）时，不得声称已压缩。"""
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    _install_hits(monkeypatch, plugin, _hits_with_size("big.jpg", 4000, 3000))

    async def probe(url, cache=None, **kwargs):
        return (True, 900_000)

    monkeypatch.setattr(plugin, "_probe_url", probe)

    results = await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    assert "已压缩" not in _chain_text(results[1])


async def test_known_size_skips_the_original_probe(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """命中条目已带原图体积时，只需探测缩放版一次（省一次往返与一次图床处理）。

    图床侧钩子（img-indexer）入库时就会写 size_bytes，插件此前完全没用上，
    于是每张图都要先探原图再探缩放版。
    """
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    hits = _hits_with_size("big.jpg", 4000, 3000)
    hits[0]["payload"]["size_bytes"] = 900_000
    _install_hits(monkeypatch, plugin, hits)

    probed: list[str] = []

    async def probe(url, cache=None, **kwargs):
        probed.append(url)
        return (True, 120_000) if "width=" in url else (True, 900_000)

    monkeypatch.setattr(plugin, "_probe_url", probe)

    results = await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    assert len(probed) == 1, f"已知原图体积时只该探缩放版，实际探测 {probed}"
    assert "width=" in probed[0]
    assert "已压缩，可发送 /原图 获取原图" in _chain_text(results[1])


async def test_known_size_still_reports_original_when_bed_did_not_scale(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """已知原图体积、但缩放版并不更小时，仍按实测判成原图，不谎称已压缩。"""
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    hits = _hits_with_size("big.jpg", 4000, 3000)
    hits[0]["payload"]["size_bytes"] = 900_000
    _install_hits(monkeypatch, plugin, hits)

    async def probe(url, cache=None, **kwargs):
        return (True, 900_000)

    monkeypatch.setattr(plugin, "_probe_url", probe)

    results = await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    assert "已压缩" not in _chain_text(results[1])


async def test_probe_budget_exceeded_does_not_block_delivery(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """探测超出预算时按「无法判定」处理并照常发图，绝不无限期拖住发送。"""
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    _install_hits(monkeypatch, plugin, _hits_with_size("big.jpg", 4000, 3000))

    async def hang(url, cache=None, **kwargs):
        await asyncio.sleep(30)
        return (True, 1)

    monkeypatch.setattr(plugin, "_probe_url", hang)
    monkeypatch.setattr(main_module, "PLAN_TOTAL_TIMEOUT", 0.1)

    results = await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    assert len(_chain_images(results[1])) == 1, "探测没回来也必须把图发出去"
    assert "已压缩" not in _chain_text(results[1])


async def test_scaled_probe_is_skipped_after_capability_rejection(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """图床明确拒绝缩放请求后进入冷却期，冷却期内不再探测缩放版。"""
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    _install_hits(monkeypatch, plugin, _hits_with_size("big.jpg", 4000, 3000))

    calls: list[str] = []

    async def probe(url, cache=None, **kwargs):
        calls.append(url)
        # 缩放版被图床以 405 拒绝（该部署不支持缩放请求）
        if "width=" in url:
            plugin._note_scaled_unsupported(405)
            return None, None
        return True, 900_000

    monkeypatch.setattr(plugin, "_probe_url", probe)

    await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))
    first_round = len(calls)
    assert first_round >= 1

    # 下一张图：仍处冷却期，一次探测都不该发生
    plugin2 = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    plugin2._scaled_verify_unsupported_until = plugin._scaled_verify_unsupported_until
    _install_hits(monkeypatch, plugin2, _hits_with_size("big2.jpg", 4000, 3000))
    monkeypatch.setattr(plugin2, "_probe_url", probe)
    calls.clear()

    await _collect(plugin2.trace(make_event("/溯源", chain=[_image_segment()])))

    assert calls == [], "冷却期内不应再探测缩放版"


async def test_local_compress_caption_uses_usage_hint_only(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """本地压缩确实缩了字节，配文才给「已压缩」那句用法说明。"""
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "local-compress"})
    )
    _install_hits(monkeypatch, plugin, _hits("scenery.jpg"))

    async def fake_load(_block, **_kwargs):
        return _big_png_bytes()

    monkeypatch.setattr(plugin, "_load_delivery_bytes", fake_load)

    results = await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    caption = _chain_text(results[1])
    assert "已压缩，可发送 /原图 获取原图" in caption
    assert "已压缩 /原图" not in caption
    assert "scenery.jpg" in caption


async def test_original_url_mode_has_no_compressed_hint(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """original-url 模式下发的就是原图，不应出现压缩提示。"""
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "original-url"})
    )
    _install_hits(monkeypatch, plugin, _hits("scenery.jpg"))

    results = await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    caption = _chain_text(results[1])
    assert "已压缩" not in caption
    assert "scenery.jpg" in caption


async def test_local_compress_mixed_payload_small_url_big_base64(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """local-compress + OneBot：已知小图直发 URL 段，大图压缩后内联 base64。

    小图（体积已知且 ≤ 跳压缩阈值）本地压缩本就会跳过，下载再 base64 回发
    纯属浪费一次往返 + 33% 膨胀；大图则下载压缩后经 base64 段直达 QQ，不经
    协议端二次下载。整条链路零探测：local-compress 不依赖图床缩放。
    """
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "local-compress"})
    )
    hits = _hits_with_urls("small.jpg", "big.jpg")
    hits[0]["payload"]["size_bytes"] = 100_000  # 已知小图：跳过下载直发 URL
    _install_hits(monkeypatch, plugin, hits)

    probed: list[str] = []
    downloaded: list[str] = []

    async def probe(url, cache=None, **kwargs):
        probed.append(url)
        return True, 1

    async def fake_load(block, **_kwargs):
        downloaded.append(block["file_name"])
        return _big_png_bytes()

    monkeypatch.setattr(plugin, "_probe_url", probe)
    monkeypatch.setattr(plugin, "_load_delivery_bytes", fake_load)

    bot = _FakeBot()
    event = make_event(
        "/溯源", platform="aiocqhttp", bot=bot, group_id="1", chain=[_image_segment()]
    )

    results = await _collect(plugin.trace(event))

    assert results == [], "OneBot 直发成功，不应再走消息链"
    assert event.stopped is True
    assert len(_image_calls(bot)) == 1, "全部图片应合并为一次直发"
    message = _image_calls(bot)[0]["message"]
    positions = [index for index, seg in enumerate(message) if seg["type"] == "image"]
    assert len(positions) == 2
    # 小图：URL 段原样直发，协议端直接拉小文件
    assert message[positions[0]]["data"]["file"] == "https://img.example.com/file/small.jpg"
    # 大图：压缩结果内联 base64，不经协议端二次下载
    assert message[positions[1]]["data"]["file"].startswith("base64://")
    assert downloaded == ["big.jpg"], "小图不应被下载"
    assert probed == [], "local-compress 整条链路不应探测图床"

    # 配文按各自证据书写（全部集中在唯一那段文本里，按序号对应）：
    # 小图按原图不发「已压缩」，大图才给用法说明
    assert message[1]["type"] == "text"
    captions = message[1]["data"]["text"]
    for line in captions.splitlines():
        if "small.jpg" in line:
            assert "已压缩" not in line
        elif "big.jpg" in line:
            assert "已压缩，可发送 /原图 获取原图" in line
        else:
            raise AssertionError(f"意外的配文行: {line}")


async def test_local_compress_chain_path_never_probes(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """非 OneBot 平台的 local-compress 同样零探测：字节压缩后走标准消息链。

    scaled-url 需逐图探测原图/缩放版体积来判定压缩证据；local-compress 改为
    本地实测字节，大图小图都不必再向图床发探测请求（省往返 + 图床处理）。
    """
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "local-compress"})
    )
    _install_hits(monkeypatch, plugin, _hits_with_urls("a.jpg", "b.jpg"))

    probed: list[str] = []

    async def probe(url, cache=None, **kwargs):
        probed.append(url)
        return True, 1

    async def fake_load(_block, **_kwargs):
        return _big_png_bytes()

    monkeypatch.setattr(plugin, "_probe_url", probe)
    monkeypatch.setattr(plugin, "_load_delivery_bytes", fake_load)

    results = await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    assert probed == [], "local-compress 不依赖图床缩放，不应探测图床"
    images = _chain_images(results[1])
    assert len(images) == 2
    assert all(isinstance(image.file, bytes) for image in images), "字节直发而非 URL"
    assert "已压缩，可发送 /原图 获取原图" in _chain_text(results[1])


# ----------------------------------------------------------------------
# 探测与压缩的批处理行为
# ----------------------------------------------------------------------


def _hits_with_urls(*names: str) -> list[dict]:
    """一批大尺寸命中：每张都会走到「需要缩放」分支。"""
    hits = []
    for index, name in enumerate(names):
        hit = _hits_with_size(name, 4000, 3000)[0]
        hit["score"] = 0.95 - index * 0.01
        hits.append(hit)
    return hits


async def test_probes_run_concurrently_not_one_by_one(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """一批图的探测必须并发：串行时总耗时是所有探测之和。"""
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    _install_hits(monkeypatch, plugin, _hits_with_urls("a.jpg", "b.jpg", "c.jpg"))

    active = 0
    peak = 0

    async def probe(url, cache=None, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return True, 100_000

    monkeypatch.setattr(plugin, "_probe_url", probe)

    await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    assert peak > 1, "三张图的探测应当重叠执行，实际是串行"


async def test_delivery_plan_is_not_recomputed_on_fallback(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """OneBot 直发明确失败后回退时，不得把回传计划（含探测）重算一遍。"""
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    _install_hits(monkeypatch, plugin, _hits_with_urls("a.jpg"))
    bot = _RejectingBot()
    event = make_event(
        "/溯源", platform="aiocqhttp", bot=bot, group_id="1", chain=[_image_segment()]
    )

    plans = 0
    original_plan = plugin._url_delivery_plan

    async def counting_plan(*args, **kwargs):
        nonlocal plans
        plans += 1
        return await original_plan(*args, **kwargs)

    async def fake_load(_block, **_kwargs):
        return _big_png_bytes()

    monkeypatch.setattr(plugin, "_url_delivery_plan", counting_plan)
    monkeypatch.setattr(plugin, "_load_delivery_bytes", fake_load)

    results = await _collect(plugin.trace(event))

    assert len(_chain_images(results[0])) == 1, "明确失败后应回退为字节直发"
    assert plans == 1, f"回传计划只该算一次，实际 {plans} 次"


async def test_compression_path_decodes_each_image_once(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """本地压缩路径不得先做一次完整解码校验、再解码压缩。

    此前 _load_delivery_bytes 先 image_file_ok（完整解码）再由压缩解码一次，
    实测 4000x3000 JPEG 白花 78ms 并多占一份全尺寸位图。
    """
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "local-compress"})
    )
    _install_hits(monkeypatch, plugin, _hits("scenery.jpg"))
    calls: list[str] = []
    monkeypatch.setattr(
        main_module, "image_file_ok", lambda path: calls.append(path) or True
    )

    async def fake_load(_block, **_kwargs):
        return _big_png_bytes()

    monkeypatch.setattr(plugin, "_load_delivery_bytes", fake_load)

    await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    assert calls == [], "压缩路径不应再走一次落盘式完整校验"


async def test_parallel_compression_keeps_block_order(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """并发压缩后字节顺序仍与 blocks 一一对应（图片不能与配文错位）。"""
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "local-compress"})
    )
    _install_hits(monkeypatch, plugin, _hits("a.jpg", "b.jpg", "c.jpg"))

    sizes = {"a.jpg": (2500, 1000), "b.jpg": (1500, 600), "c.jpg": (900, 400)}

    async def fake_load(block, **_kwargs):
        from PIL import Image as PILImage

        buffer = io.BytesIO()
        PILImage.effect_noise(sizes[block["file_name"]], 64).convert("RGB").save(
            buffer, format="PNG"
        )
        return buffer.getvalue()

    monkeypatch.setattr(plugin, "_load_delivery_bytes", fake_load)

    results = await _collect(plugin.trace(make_event("/溯源", chain=[_image_segment()])))

    sent = []
    for image in _chain_images(results[1]):
        with PILImage.open(io.BytesIO(image.file)) as output:
            sent.append(output.size)
    # max_side=1920：a 被缩到 1920x768，b、c 不超限保持原尺寸
    assert sent == [(1920, 768), (1500, 600), (900, 400)]

    # 配文顺序也必须一致：每张图的配文里出现的文件名与图片顺序对应
    caption_positions = [
        _chain_text(results[1]).index(name) for name in ("a.jpg", "b.jpg", "c.jpg")
    ]
    assert caption_positions == sorted(caption_positions)


async def test_prompt_and_images_both_sent_on_onebot_path(
    make_plugin, make_event, monkeypatch, vector_stub
):
    """提示与图片并行的同时，两条都仍要发出且事件被停掉。"""
    plugin = vector_stub(
        make_plugin(search_engine="vector", image_delivery={"mode": "scaled-url"})
    )
    bot = _FakeBot()
    _install_hits(monkeypatch, plugin, _hits("a.jpg"))
    event = make_event(
        "/溯源", platform="aiocqhttp", bot=bot, group_id="1", chain=[_image_segment()]
    )

    results = await _collect(plugin.trace(event))

    texts = [
        "".join(seg["data"]["text"] for seg in params["message"] if seg["type"] == "text")
        for _action, params in bot.calls
    ]
    assert any("向量检索命中" in text for text in texts), "提示必须发出"
    assert any(
        any(seg["type"] == "image" for seg in params["message"])
        for _action, params in bot.calls
    ), "图片必须发出"
    assert results == []
    assert event.stopped is True


# ----------------------------------------------------------------------
# 重复发送：单次发送保证
# ----------------------------------------------------------------------
class ActionFailed(Exception):
    """类名与 aiocqhttp 的 ActionFailed 完全一致：异常分类按类名识别。"""


class _TimeoutBot:
    """直发超时替身：消息很可能已送达，只是响应丢了。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        raise asyncio.TimeoutError()


class _RejectingBot:
    """协议端明确回报失败（ActionFailed）：允许换通道重发一次。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        raise ActionFailed("send_group_msg failed")


def _big_png_bytes() -> bytes:
    """2500x1000 的 PNG：足够大，本地压缩一定会真的变小。"""
    buffer = io.BytesIO()
    PILImage.effect_noise((2500, 1000), 64).convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def _hits_with_size(name: str, width: int, height: int) -> list[dict]:
    """带尺寸的向量命中：回传计划据此判断是否需要图床缩放。"""
    return [
        {
            "id": name,
            "score": 0.97,
            "duplicate_count": 1,
            "duplicates": [],
            "payload": {
                "image_url": f"https://img.example.com/file/{name}",
                "file_name": name,
                "width": width,
                "height": height,
            },
        }
    ]


def _install_hash_hit(plugin, tmp_path, image_url):
    """登记一条哈希命中，并返回查询用的图片段（走本地文件，不依赖网络）。"""
    from astrbot.api.message_components import Image

    path = tmp_path / "origin.jpg"
    PILImage.effect_noise((96, 96), 48).convert("RGB").save(path, format="JPEG")
    feats = compute_features(str(path), plugin.hash_size)
    plugin.library.add_if_absent(
        {
            "phash": feats.phash,
            "dhash": feats.dhash,
            "ahash": feats.ahash,
            "width": feats.width,
            "height": feats.height,
            "image_url": image_url,
            "source": "register",
        }
    )
    return Image.fromFileSystem(str(path))


def _image_calls(bot) -> list[dict]:
    """只看带图片段的直发调用（纯文本提示不算）。"""
    return [
        params
        for _action, params in bot.calls
        if any(seg["type"] == "image" for seg in params["message"])
    ]


async def test_onebot_timeout_never_resends_the_image(
    make_plugin, make_event, imgbed, tmp_path
):
    """直发超时属于「结果未知」：不得再走字节/链条回退。

    修复前这里会继续回退并再发一次，用户于是收到两张一模一样的原图；且原图
    体积大、协议端要自己下载图床 URL，超时恰恰最容易发生在原图上。
    """
    plugin = make_plugin(search_engine="hash")
    bot = _TimeoutBot()
    seg = _install_hash_hit(plugin, tmp_path, f"{imgbed.base_url}/file/orig.jpg")
    event = make_event("/溯源", platform="aiocqhttp", bot=bot, group_id="1", chain=[seg])

    results = await _collect(plugin.trace(event))

    assert len(_image_calls(bot)) == 1, "超时后不得重发图片"
    assert results == [], "不得再产生消息链结果（那等于第二次发送）"
    assert event.stopped is True


async def test_onebot_explicit_failure_falls_back_exactly_once(
    make_plugin, make_event, imgbed, tmp_path
):
    """协议端明确回报失败时才允许回退，且回退只发一次。"""
    plugin = make_plugin(search_engine="hash")
    bot = _RejectingBot()
    seg = _install_hash_hit(plugin, tmp_path, f"{imgbed.base_url}/file/orig.jpg")
    event = make_event("/溯源", platform="aiocqhttp", bot=bot, group_id="1", chain=[seg])

    results = await _collect(plugin.trace(event))

    assert len(_image_calls(bot)) == 1, "明确失败后不再重复直发"
    assert len(results) == 1
    assert len(_chain_images(results[0])) == 1


async def test_delivery_is_sent_at_most_once_per_event(
    make_plugin, make_event, imgbed
):
    """事件级去重盾：同一事件内第二次回传直接跳过，不发第二遍。"""
    plugin = make_plugin()
    bot = _FakeBot()
    event = make_event("/溯源", platform="aiocqhttp", bot=bot, group_id="1")
    block = {"url": f"{imgbed.base_url}/file/a.jpg"}

    first = await _collect(plugin._yield_delivery(event, "标题", [block]))
    second = await _collect(plugin._yield_delivery(event, "标题", [block]))

    # 直发成功时消息由 OneBot 原生发出，_yield_delivery 不产出消息链结果
    assert first == []
    assert len(_image_calls(bot)) == 1
    assert second == []


async def test_multi_image_trace_delivers_each_hit(
    make_plugin, make_event, imgbed, tmp_path
):
    """一条消息带多张图时，每张图的命中都要投递，不能只发第一张。

    `_yield_delivery` 的事件级去重盾只应阻止同一批图的回退重发；trace 在
    每轮不同图片之间复位标记，并把 stop_event 推迟到整轮结束。
    """
    from astrbot.api.message_components import Image

    plugin = make_plugin(search_engine="hash")
    bot = _FakeBot()
    segments = []
    for index, sigma in enumerate((24, 64), 1):
        path = tmp_path / f"origin{index}.jpg"
        PILImage.effect_noise((96, 96), sigma).convert("RGB").save(path, "JPEG")
        feats = compute_features(str(path), plugin.hash_size)
        plugin.library.add_if_absent(
            {
                "phash": feats.phash,
                "dhash": feats.dhash,
                "ahash": feats.ahash,
                "width": feats.width,
                "height": feats.height,
                "image_url": f"{imgbed.base_url}/file/orig{index}.jpg",
                "source": "register",
            }
        )
        segments.append(Image.fromFileSystem(str(path)))

    event = make_event(
        "/溯源", platform="aiocqhttp", bot=bot, group_id="1", chain=segments
    )

    results = await _collect(plugin.trace(event))

    assert results == [], "OneBot 直发成功时不应再走消息链"
    image_calls = _image_calls(bot)
    assert len(image_calls) == 2, "两张查询图片各应投递一次"
    assert event.stopped is True


async def test_resolve_local_file_skips_fallback_for_heic_without_decoder(
    monkeypatch, tmp_path, make_plugin
):
    """认出 HEIC 容器却解不开时不再白跑一次兜底下载，并给出可操作提示。"""
    from astrbot.api.message_components import Image

    import astrbot_plugin_image_trace.main as main_module

    monkeypatch.setattr(main_module, "heif_available", lambda: False)
    plugin = make_plugin()

    fake = tmp_path / "photo.heic"
    fake.write_bytes(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1" + b"\x00" * 48)

    downloaded: list[str] = []

    async def spy(url):
        downloaded.append(url)
        raise AssertionError("HEIC 缺解码器时不该再走兜底下载")

    monkeypatch.setattr(plugin, "_download", spy)
    seg = Image(url="https://qq.example.com/photo.heic", path=str(fake))

    resolved = await plugin._resolve_local(seg, probe=plugin._probe_mime)
    path, is_tmp, message = resolved.path, resolved.is_tmp, resolved.message

    assert (path, is_tmp) == ("", False)
    assert "HEIC" in message
    assert downloaded == []


async def test_extract_images_dedupes_across_url_and_file_fields(make_event):
    """同一张图在消息链与被引用消息里字段不同时，不得被当成两张。

    只按「url 或 file」取键时，链里带 url、Reply.chain 里只有 file 的同一张图
    会被判定为两张，/溯源 于是对同一张图跑两遍、回传两遍。
    """
    from astrbot.api.message_components import Image as SegImage
    from astrbot.api.message_components import Reply

    plugin_cls = ImageTracePlugin
    same = "https://img.example.com/file/same.jpg"
    event = make_event(
        "/溯源",
        chain=[
            SegImage(url=same),
            Reply(chain=[SegImage(url=same)]),
            SegImage(url="https://img.example.com/file/other.jpg"),
        ],
    )

    images = plugin_cls._extract_images(event)

    assert len(images) == 2


async def test_auto_engine_resolves_query_image_only_once_on_hash_fallback(
    make_plugin, make_event, monkeypatch, tmp_path
):
    """auto 引擎向量未命中后回退哈希时，不得把同一张图重新解析/下载一遍。

    此前 _vector_search_one 与 _trace_hash 各调一次 _resolve_local_file：对
    QQ 图床链接就是两次下载 + 两次完整解码。现在由 _trace_auto 统一解析一次、
    两段下游共用（并统一清理临时文件）。
    """
    plugin = make_plugin(search_engine="auto")
    plugin.vector = types.SimpleNamespace(
        enabled=True,
        threshold=0.80,
        duplicate_threshold=0.995,
        top_k=5,
        config_hint=lambda: "",
    )

    async def no_hits(_vector, **_kwargs):
        return []

    async def fake_embed(_path, *, mime=None):
        return [0.1, 0.2]

    plugin.vector.embed_file = fake_embed
    plugin.vector.search = no_hits

    image_path = tmp_path / "query.jpg"
    PILImage.effect_noise((64, 64), 32).convert("RGB").save(image_path, format="JPEG")

    calls: list[str] = []

    async def fake_resolve(_seg, *, probe):
        calls.append(str(_seg))
        return main_module.ResolvedImage(
            path=str(image_path), mime="image/jpeg"
        )

    monkeypatch.setattr(plugin, "_resolve_local", fake_resolve)
    # 让哈希腿“非空”从而进入回退分支，但不真的查库
    monkeypatch.setattr(plugin, "_hash_pool_size", lambda: 1)
    monkeypatch.setattr(plugin, "_merged_hash_candidates", lambda *_a, **_k: ([], []))

    results = await _collect(plugin._trace_auto(make_event(""), _image_segment()))

    assert len(calls) == 1, f"同一张查询图只该解析一次，实际 {len(calls)} 次"
    assert any("尝试哈希比对" in (getattr(item, "text", None) or "") for item in results)
    assert any("未找到相似度达标" in (getattr(item, "text", None) or "") for item in results)


async def test_auto_engine_reuses_features_and_mime_from_single_decode(
    make_plugin, make_event, monkeypatch, tmp_path
):
    """auto 解析时算出的 features/MIME 必须被两腿复用，不得再解码一次。"""
    plugin = make_plugin(search_engine="auto")
    plugin.vector = types.SimpleNamespace(
        enabled=True,
        threshold=0.80,
        duplicate_threshold=0.995,
        top_k=5,
        config_hint=lambda: "",
    )

    embed_mimes = []

    async def fake_embed(_path, *, mime=None):
        embed_mimes.append(mime)
        return [0.1, 0.2]

    async def no_hits(_vector, **_kwargs):
        return []

    plugin.vector.embed_file = fake_embed
    plugin.vector.search = no_hits

    image_path = tmp_path / "query.jpg"
    PILImage.effect_noise((64, 64), 32).convert("RGB").save(image_path, format="JPEG")
    feats = compute_features(str(image_path), plugin.hash_size)

    calls = []

    def fake_features_with_mime(path, hash_size):
        calls.append(path)
        return feats, "image/jpeg"

    def forbid_features(*_args, **_kwargs):
        raise AssertionError("解析结果已带 features，不应再调用 compute_features")

    monkeypatch.setattr(main_module, "compute_features_with_mime", fake_features_with_mime)
    monkeypatch.setattr(main_module, "compute_features", forbid_features)
    monkeypatch.setattr(plugin, "_hash_pool_size", lambda: 1)
    monkeypatch.setattr(plugin, "_merged_hash_candidates", lambda *_a, **_k: ([], []))

    from astrbot.api.message_components import Image as SegImage

    seg = SegImage.fromFileSystem(str(image_path))
    results = await _collect(plugin._trace_auto(make_event(""), seg))

    assert len(calls) == 1
    assert embed_mimes == ["image/jpeg"]
    assert any("未找到相似度达标" in (getattr(item, "text", None) or "") for item in results)


async def test_download_bytes_reuses_open_response_for_large_images(
    make_plugin, monkeypatch, imgbed
):
    """声明体积超过内存阈值时，直接复用当前响应落盘，不再发第二次 GET。

    旧实现 release 当前响应后重新 `_download(url)`：同一张图两次出网，图床
    与带宽都白付一遍。这里把内存阈值临时压到 10 字节，让普通测试图走大图分支，
    并断言 `_download` 一次都没被调用。
    """
    plugin = make_plugin()
    monkeypatch.setattr(main_module, "MEMORY_DOWNLOAD_MAX_BYTES", 10)

    async def no_second_request(*_args, **_kwargs):
        raise AssertionError("大图分支不应重新发起下载")

    monkeypatch.setattr(plugin, "_download", no_second_request)

    data = await plugin._download_bytes(f"{imgbed.base_url}/file/x.jpg")

    assert data.startswith(b"\xff\xd8\xff"), "应拿到完整的 JPEG 字节"


async def test_fallback_chain_maps_url_index_to_url_blocks_only(
    make_plugin, make_event, monkeypatch
):
    """混合批（先 path 后 url）回退到消息链时，URL 图不得取到空串。

    回传计划 urls/evidences 与「有 url 或 path 的 block」同序，而消息链循环
    遍历全部 block。若下标只在遇到 url block 时推进，path block 会占用一个
    不对应 any url 的位置，导致后面 URL 图取到前一项的空直链。
    """
    plugin = make_plugin(delivery={"napcat_mode": "url"})
    event = make_event("/原图", platform="qqofficial")

    async def no_pipeline(_event, _header, blocks, *, mark_compressed=False):
        # 跳过格式体检/上传通道，直接验证最终消息链的 index 对齐
        return blocks, False

    monkeypatch.setattr(plugin, "_offbox_pipeline", no_pipeline)

    blocks = [
        {"path": "/tmp/transcoded.jpg", "file_name": "a.jpg"},
        {"url": "https://img.example.com/file/b.jpg", "file_name": "b.jpg"},
    ]
    results = await _collect(plugin._yield_delivery(event, "标题", blocks))

    images = _chain_images(results[0])
    assert len(images) == 2
    assert images[0].path == "/tmp/transcoded.jpg"
    assert images[1].url == "https://img.example.com/file/b.jpg"

"""发送层 S0 行为矩阵：``_yield_delivery`` 的真实分支基线。

第三轮重构前的 ``_yield_delivery`` 是整段编排（去重 → 建请求 → offbox 前置 →
计划 → 选通道 → 执行 → 记账 → 清理）。这里不改生产代码，直接驱动真实方法，
只把远端 I/O 替换成桩，锁住以下矩阵：

- 平台画像：aiocqhttp（NapCat）、aiocqhttp 无 bot、其它 OneBot 兼容端、qq_official；
- 模式：auto / native / bytes / url / url-scaled，以及 ``mode_override`` 入场；
- 通道结果：SENT / FAILED / UNKNOWN（超时）；
- 回退：只有明确失败才回退一次，回退目标正确，``fallback_reason`` 有记录；
- 文本与图片：单图、多图、只有文字、只有图片，OneBot 直发始终文字集中在前；
- 临时文件：成功 / 中止（未送达）/ 异常三条退出路径都清理；
- 记账：``_mark_event_sent``、``stop_event`` 与 ``_defer_event_stop`` 的时机。

这些用例只钉住**现状**，不修改任何产品代码。
"""

from __future__ import annotations

import pytest

from astrbot_plugin_image_trace.delivery import (
    ChainChannel,
    DeliveryConfig,
    DeliveryContext,
    DeliveryProfile,
    DeliveryRequest,
    OneBotChannel,
    plan_delivery,
)
from astrbot_plugin_image_trace.image_delivery import CompressionEvidence, SendOutcome

URL_A = "https://img.example.com/file/a.jpg"
URL_B = "https://img.example.com/file/b.jpg"


def _owned_block(path) -> dict:
    return {"text": "图 1", "path": str(path), "_tmp_owned": True}


class _OkBot:
    """OneBot 直发成功替身：记录每次 call_action。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        return {"status": "ok"}


def _image_calls(bot) -> list[dict]:
    """只看带图片段的直发调用。"""
    return [
        params
        for _action, params in bot.calls
        if any(seg["type"] == "image" for seg in params["message"])
    ]


def _chain_images(result):
    from astrbot.api.message_components import Image

    return [seg for seg in result.chain if isinstance(seg, Image)]


def _chain_plains(result):
    from astrbot.api.message_components import Plain

    return [seg for seg in result.chain if isinstance(seg, Plain)]


@pytest.mark.asyncio
async def test_tmp_owned_file_deleted_on_success(make_plugin, make_event, tmp_path, monkeypatch):
    plugin = make_plugin()
    owned = tmp_path / "transcoded-success.jpg"
    owned.write_bytes(b"fake-jpeg")
    block = _owned_block(owned)

    async def pipeline(_event, _header, _blocks, *, mark_compressed=False):
        # handled=True：上传通道已经自行发完，_yield_delivery 只会标记已发送
        return [dict(block)], True

    monkeypatch.setattr(plugin, "_offbox_pipeline", pipeline)
    event = make_event("/随机图", platform="qq_official", origin="tmp-cleanup-success")

    results = [item async for item in plugin._yield_delivery(event, "", [dict(block)])]

    assert results == []
    assert event.stopped is True
    assert not owned.exists()


@pytest.mark.asyncio
async def test_tmp_owned_file_deleted_when_delivery_raises(
    make_plugin, make_event, tmp_path, monkeypatch
):
    plugin = make_plugin()
    owned = tmp_path / "transcoded-error.jpg"
    owned.write_bytes(b"fake-jpeg")
    block = _owned_block(owned)

    async def pipeline(_event, _header, _blocks, *, mark_compressed=False):
        # handled=False：继续走后续通道；下面的 _delivery_config 会抛错，
        # 用来验证异常路径同样会进入 finally 清理。
        return [dict(block)], False

    def boom():
        raise RuntimeError("delivery config exploded")

    monkeypatch.setattr(plugin, "_offbox_pipeline", pipeline)
    monkeypatch.setattr(plugin, "_delivery_config", boom)
    event = make_event("/随机图", platform="qq_official", origin="tmp-cleanup-error")

    with pytest.raises(RuntimeError):
        [item async for item in plugin._yield_delivery(event, "", [dict(block)])]

    assert not owned.exists()


@pytest.mark.asyncio
async def test_tmp_owned_file_deleted_when_delivery_aborts(
    make_plugin, make_event, tmp_path, monkeypatch
):
    """中止路径（未走任何回传通道，事件已被标记已发送）同样清理临时文件。"""
    plugin = make_plugin(image_delivery={"mode": "native"})
    owned = tmp_path / "transcoded-abort.jpg"
    owned.write_bytes(b"fake-jpeg")
    block = _owned_block(owned)

    async def pipeline(target_event, _header, _blocks, *, mark_compressed=False):
        # 模拟「前序步骤已把本事件标记为已发送」：后续兜底必须直接返回
        target_event._image_trace_sent = True
        return [dict(block)], False

    monkeypatch.setattr(plugin, "_offbox_pipeline", pipeline)
    event = make_event("/随机图", platform="qq_official", origin="tmp-cleanup-abort")

    results = [item async for item in plugin._yield_delivery(event, "", [dict(block)])]

    assert results == []
    assert not owned.exists()


# ----------------------------------------------------------------------
# 平台画像 × 模式：直发与消息链的执行形态
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_image_direct_message_is_text_then_image(
    make_plugin, make_event
):
    """NapCat 直发单图：文字段集中在最前，图片段排在末尾。"""
    plugin = make_plugin(image_delivery={"mode": "url"})
    bot = _OkBot()
    event = make_event(
        "/随机图", platform="aiocqhttp", bot=bot, group_id="1"
    )

    results = [
        item
        async for item in plugin._yield_delivery(
            event, "标题", [{"url": URL_A, "file_name": "a.jpg"}]
        )
    ]

    assert results == []
    message = _image_calls(bot)[0]["message"]
    assert [seg["type"] for seg in message] == ["at", "text", "image"]
    assert message[1]["data"]["text"] == "标题"


@pytest.mark.asyncio
async def test_images_only_direct_message_keeps_images_contiguous(
    make_plugin, make_event
):
    """只有图片（block 无配文）时仍保持「文字 → 连续图片」两段结构。"""
    plugin = make_plugin(image_delivery={"mode": "url"})
    bot = _OkBot()
    event = make_event(
        "/随机图", platform="aiocqhttp", bot=bot, group_id="1"
    )

    results = [
        item
        async for item in plugin._yield_delivery(
            event, "标题", [{"url": URL_A}, {"url": URL_B}]
        )
    ]

    assert results == []
    message = _image_calls(bot)[0]["message"]
    assert [seg["type"] for seg in message] == ["at", "text", "image", "image"]
    assert message[1]["data"]["text"] == "标题"


@pytest.mark.asyncio
async def test_text_only_blocks_yield_chain_without_images(
    make_plugin, make_event
):
    """只有文字：不进入任何图片通道，直接产出纯文本消息链。"""
    plugin = make_plugin(image_delivery={"mode": "native"})
    event = make_event("/随机图", platform="telegram")

    results = [
        item
        async for item in plugin._yield_delivery(
            event, "标题", [{"text": "说明"}]
        )
    ]

    assert len(results) == 1
    plains = _chain_plains(results[0])
    assert _chain_images(results[0]) == []
    assert plains[0].text == "标题"
    assert "说明" in plains[1].text
    assert plugin._event_already_sent(event) is True
    # 消息链分支只标记已发送，stop_event 留给后续 respond stage
    assert event.stopped is False


@pytest.mark.asyncio
async def test_chain_path_keeps_header_first_and_caption_before_image(
    make_plugin, make_event
):
    """非直发平台的消息链：标题在最前，逐图配文紧跟各自的图片。

    注意这与 OneBot 直发的「文字集中在前」不同——消息链本身就是 AstrBot
    的结构化消息，没有 QQ 图文卡片的渲染分组问题，现状保持不变。
    """
    plugin = make_plugin(image_delivery={"mode": "native"})
    event = make_event("/随机图", platform="telegram")

    results = [
        item
        async for item in plugin._yield_delivery(
            event,
            "标题",
            [
                {"url": URL_A, "text": "甲"},
                {"url": URL_B, "text": "乙"},
            ],
        )
    ]

    assert len(results) == 1
    chain = results[0].chain
    assert _chain_plains(results[0])[0].text == "标题"
    assert len(_chain_images(results[0])) == 2
    # 标题 → 甲 → 图 A → 乙 → 图 B
    from astrbot.api.message_components import Image, Plain

    assert [isinstance(seg, Plain) for seg in chain] == [True, True, False, True, False]
    assert [isinstance(seg, Image) for seg in chain] == [False, False, True, False, True]


@pytest.mark.asyncio
async def test_aiocqhttp_without_bot_falls_back_to_url_chain(
    make_plugin, make_event
):
    """aiocqhttp 但 bot 缺席：画像判定不可直发，只产出 URL 消息链。"""
    plugin = make_plugin(image_delivery={"mode": "url"})
    event = make_event("/随机图", platform="aiocqhttp", bot=None)

    results = [
        item
        async for item in plugin._yield_delivery(
            event, "标题", [{"url": URL_A}]
        )
    ]

    assert len(results) == 1
    images = _chain_images(results[0])
    assert len(images) == 1 and images[0].url == URL_A
    # 消息链分支不主动 stop_event
    assert event.stopped is False


@pytest.mark.asyncio
async def test_other_onebot_compatible_platform_uses_url_chain(
    make_plugin, make_event
):
    """其它 OneBot 兼容端（平台名非 aiocqhttp）：不直发、不加原生 @。"""
    plugin = make_plugin(image_delivery={"mode": "native"})
    event = make_event("/随机图", platform="onebot", group_id="1")

    results = [
        item
        async for item in plugin._yield_delivery(
            event, "标题", [{"url": URL_A, "text": "甲"}]
        )
    ]

    assert len(results) == 1
    plains = _chain_plains(results[0])
    assert plains[0].text == "标题", "非 aiocqhttp 不应插入原生 @ 段"
    assert _chain_images(results[0])[0].url == URL_A


@pytest.mark.asyncio
async def test_native_mode_executes_url_chain_without_touching_onebot(
    make_plugin, make_event
):
    """native：不加工，直接交给消息链（URL 原样）。"""
    plugin = make_plugin(image_delivery={"mode": "native"})
    bot = _OkBot()
    event = make_event("/随机图", platform="aiocqhttp", bot=bot, group_id="1")

    results = [
        item
        async for item in plugin._yield_delivery(
            event, "标题", [{"url": URL_A}]
        )
    ]

    assert bot.calls == []
    assert _chain_images(results[0])[0].url == URL_A


@pytest.mark.asyncio
async def test_mode_override_url_forces_direct_send(
    make_plugin, make_event
):
    """mode_override 入场：自动模式下强制走 OneBot URL 直发。"""
    plugin = make_plugin(image_delivery={"mode": "auto"})
    bot = _OkBot()
    event = make_event("/原图", platform="aiocqhttp", bot=bot, group_id="1")

    results = [
        item
        async for item in plugin._yield_delivery(
            event, "标题", [{"url": URL_A}], mode_override="url"
        )
    ]

    assert results == []
    assert len(_image_calls(bot)) == 1
    assert event.stopped is True


@pytest.mark.asyncio
async def test_mode_override_native_skips_local_compression(
    make_plugin, make_event, monkeypatch
):
    """mode_override 入场：自动模式下强制 native，不进入本地压缩分支。"""
    plugin = make_plugin(image_delivery={"mode": "auto"})
    bot = _OkBot()
    event = make_event("/原图", platform="aiocqhttp", bot=bot, group_id="1")

    async def no_prepare(*_args, **_kwargs):
        raise AssertionError("native 覆盖不应进入字节准备")

    monkeypatch.setattr(plugin, "_prepare_delivery_blocks", no_prepare)

    results = [
        item
        async for item in plugin._yield_delivery(
            event, "标题", [{"url": URL_A}], mode_override="native"
        )
    ]

    assert bot.calls == []
    assert _chain_images(results[0])[0].url == URL_A


# ----------------------------------------------------------------------
# 通道结果：SENT / FAILED / UNKNOWN 与回退语义
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bytes_prepare_failure_falls_back_to_url_chain_once(
    make_plugin, make_event, monkeypatch
):
    """字节准备失败（None）→ 整批改走 URL 链，且只产出一次消息链。"""
    plugin = make_plugin(image_delivery={"mode": "bytes"})
    bot = _OkBot()
    event = make_event("/随机图", platform="aiocqhttp", bot=bot, group_id="1")

    async def no_bytes(*_args, **_kwargs):
        return None

    monkeypatch.setattr(plugin, "_prepare_delivery_blocks", no_bytes)

    results = [
        item
        async for item in plugin._yield_delivery(
            event, "标题", [{"url": URL_A}]
        )
    ]

    assert bot.calls == []
    assert len(results) == 1
    assert _chain_images(results[0])[0].url == URL_A
    assert plugin._event_already_sent(event) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expect_chain"),
    [
        (SendOutcome.FAILED, True),
        (SendOutcome.UNKNOWN, False),
        (SendOutcome.SENT, False),
    ],
    ids=["failed-falls-back", "unknown-treated-as-sent", "sent"],
)
async def test_bytes_channel_outcome_controls_fallback(
    make_plugin, make_event, monkeypatch, outcome, expect_chain
):
    """字节直发的三态：只有 FAILED 回退；UNKNOWN 按已送达绝不重发。"""
    plugin = make_plugin(image_delivery={"mode": "bytes"})
    bot = _OkBot()
    event = make_event("/随机图", platform="aiocqhttp", bot=bot, group_id="1")
    sent_calls: list[bool] = []

    async def prepared(*_args, **_kwargs):
        return [(b"jpeg-bytes", True)]

    async def fake_send(*_args, **_kwargs):
        sent_calls.append(True)
        return outcome

    monkeypatch.setattr(plugin, "_prepare_delivery_blocks", prepared)
    monkeypatch.setattr(plugin, "_send_via_onebot", fake_send)

    results = [
        item
        async for item in plugin._yield_delivery(
            event, "标题", [{"url": URL_A}]
        )
    ]

    assert len(sent_calls) == 1, "字节通道只尝试一次"
    assert bot.calls == [], "send 被替换，真实 bot 不应再被调用"
    assert plugin._event_already_sent(event) is True
    if expect_chain:
        assert len(results) == 1
        assert _chain_images(results[0])[0].url == URL_A, "回退目标是 URL 链"
        # 回退走消息链：已记账但 stop_event 交给 respond stage
        assert event.stopped is False
    else:
        assert results == [], "UNKNOWN / SENT 都不得再发第二次"
        assert event.stopped is True


def _plan(plugin, event, request, mode):
    return plan_delivery(
        request,
        DeliveryProfile.from_target(plugin._delivery_target(event)),
        DeliveryConfig(mode=mode, max_side=1920, quality=85, scaled_url_style="query"),
    )


@pytest.mark.asyncio
async def test_onebot_url_channel_records_fallback_reason(make_plugin, make_event, monkeypatch):
    """OneBot URL 直发 FAILED 时 DeliveryResult 记录明确的回退原因。"""
    plugin = make_plugin()
    event = make_event("", platform="aiocqhttp", bot=_OkBot(), group_id="1")
    request = DeliveryRequest(header="标题", blocks=[{"url": URL_A}])

    async def failed(*_args, **_kwargs):
        return SendOutcome.FAILED

    monkeypatch.setattr(plugin, "_send_via_onebot", failed)

    ctx = DeliveryContext(
        event=event,
        request=request,
        plan=_plan(plugin, event, request, "url"),
        image_blocks=request.blocks,
        url_plan=([URL_A], [CompressionEvidence.ORIGINAL]),
    )
    result = await OneBotChannel(plugin).deliver_url(ctx)

    assert result.outcome == "failed"
    assert result.sent_count == 0
    assert result.fallback_reason == "onebot_url_send_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prepared", "reason"),
    [
        (None, "onebot_prepare_failed"),
        (SendOutcome.FAILED, "onebot_bytes_send_failed"),
    ],
    ids=["prepare-failed", "send-failed"],
)
async def test_onebot_bytes_channel_records_fallback_reason(
    make_plugin, make_event, monkeypatch, prepared, reason
):
    """字节通道的两个失败源各有不同的 fallback_reason。"""
    plugin = make_plugin()
    event = make_event("", platform="aiocqhttp", bot=_OkBot(), group_id="1")
    request = DeliveryRequest(header="标题", blocks=[{"url": URL_A}])

    async def fake_local(*_args, **_kwargs):
        return prepared

    monkeypatch.setattr(plugin, "_local_compress_delivery", fake_local)

    ctx = DeliveryContext(
        event=event,
        request=request,
        plan=_plan(plugin, event, request, "bytes"),
        image_blocks=request.blocks,
    )
    result = await OneBotChannel(plugin).deliver_compressed(ctx)

    assert result.outcome == "failed"
    assert result.fallback_reason == reason


@pytest.mark.asyncio
async def test_chain_bytes_prepare_failure_records_fallback_reason(
    make_plugin, make_event, monkeypatch
):
    """消息链字节准备失败同样留下明确原因，交由 URL 链兜底。"""
    plugin = make_plugin()
    event = make_event("", platform="telegram")
    request = DeliveryRequest(header="标题", blocks=[{"url": URL_A}])

    async def no_bytes(*_args, **_kwargs):
        return None

    monkeypatch.setattr(plugin, "_prepare_delivery_blocks", no_bytes)
    ctx = DeliveryContext(
        event=event,
        request=request,
        plan=_plan(plugin, event, request, "url"),
        image_blocks=request.blocks,
    )
    result = await ChainChannel(plugin).deliver_bytes(ctx)

    assert result.outcome == "failed"
    assert result.fallback_reason == "local_prepare_failed"


# ----------------------------------------------------------------------
# 记账：mark_sent / stop_event / defer
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deferred_stop_marks_sent_but_leaves_event_running(
    make_plugin, make_event
):
    """多图溯源：投递成功只标记已发送，stop 推迟到整轮结束。"""
    plugin = make_plugin(image_delivery={"mode": "url"})
    bot = _OkBot()
    event = make_event("/溯源", platform="aiocqhttp", bot=bot, group_id="1")
    plugin._defer_event_stop(event, True)

    results = [
        item
        async for item in plugin._yield_delivery(
            event, "标题", [{"url": URL_A}]
        )
    ]

    assert results == []
    assert plugin._event_already_sent(event) is True
    assert event.stopped is False, "延迟 stop 时不应中断当前生成器"

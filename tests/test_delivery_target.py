"""推送目标画像 + 第三方转存前置流水线。

把「按平台分叉」的判断收成一个纯函数（image_delivery.delivery_target）之后，
这里锁定两件事：

1. 各平台的能力画像必须与历史行为一致（aiocqhttp 能直发/能原生 @；
   qq_official 走第三方转存但不需要 @；其它平台两者皆否）；
2. _offbox_pipeline 的顺序契约：格式体检改写后的 blocks 必须原样交给上传
   通道，否则会出现「假 jpg 已转成本地 path、上传通道却还拿着原 URL」。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from astrbot_plugin_image_trace.image_delivery import DeliveryTarget, delivery_target

# ----------------------------------------------------------------------
# 平台能力画像
# ----------------------------------------------------------------------


def test_aiocqhttp_can_direct_send_and_mention():
    target = delivery_target("aiocqhttp", has_bot=True)

    assert target.direct_send is True
    assert target.native_mention is True
    assert target.offbox_upload is False


def test_aiocqhttp_without_bot_cannot_direct_send():
    """bot 缺席时 call_action 必失败，不能声称能直发；@ 段仍可加（无害）。"""
    target = delivery_target("aiocqhttp", has_bot=False)

    assert target.direct_send is False
    assert target.native_mention is True


@pytest.mark.parametrize("name", ["qq_official", "qqofficial", "qqofficial_webhook", "QQ_Official"])
def test_qq_official_is_offbox_without_mention(name):
    """官方机器人经腾讯转存（要格式体检+上传通道），但没有原生 At 段。"""
    target = delivery_target(name)

    assert target.offbox_upload is True
    assert target.direct_send is False
    assert target.native_mention is False


@pytest.mark.parametrize("name", ["telegram", "discord", "wecom", "", "aiocqhttp_webhook"])
def test_other_platforms_have_no_special_capability(name):
    target = delivery_target(name)

    assert target.offbox_upload is False
    assert target.direct_send is False
    assert target.native_mention is False


def test_target_is_immutable():
    """画像在传递过程中不该被某个分支就地改写（否则又会分叉）。"""
    target = delivery_target("qq_official")
    assert isinstance(target, DeliveryTarget)
    with pytest.raises(FrozenInstanceError):
        target.direct_send = True  # type: ignore[misc]


# ----------------------------------------------------------------------
# 第三方转存前置流水线：顺序契约
# ----------------------------------------------------------------------


async def test_offbox_pipeline_feeds_rewritten_blocks_to_upload(
    make_plugin, monkeypatch
):
    """体检把 URL 改成 path 后，上传通道拿到的必须是改写后的 block。"""
    plugin = make_plugin(qq_upload={"mode": "bytes"})
    seen: dict = {}

    async def rewrite(blocks):
        renamed = [dict(block) for block in blocks]
        for block in renamed:
            block.pop("url", None)
            block["path"] = "/tmp/transcoded.jpg"
        return renamed

    async def takeover(_event, _header, blocks, *, mark_compressed=False):
        seen["blocks"] = blocks
        return True

    monkeypatch.setattr(plugin, "_offbox_rewrite_blocks", rewrite)
    monkeypatch.setattr(plugin, "_qq_upload_takeover", takeover)

    blocks, handled = await plugin._offbox_pipeline(
        object(), "标题", [{"url": "https://img.example.com/file/a.jpg"}]
    )

    assert handled is True
    assert seen["blocks"] == blocks, "上传通道必须收到体检后的同一份 blocks"
    assert seen["blocks"][0]["path"] == "/tmp/transcoded.jpg"
    assert "url" not in seen["blocks"][0]


async def test_offbox_pipeline_passes_through_when_not_taken_over(
    make_plugin, monkeypatch
):
    """通道未接管时返回原始改写结果 + handled=False，由调用方继续走原生路径。"""
    plugin = make_plugin(qq_upload={"mode": "native"})

    async def rewrite(blocks):
        return [dict(block) for block in blocks]

    monkeypatch.setattr(plugin, "_offbox_rewrite_blocks", rewrite)

    blocks, handled = await plugin._offbox_pipeline(
        object(), "标题", [{"url": "https://img.example.com/file/a.jpg"}]
    )

    assert handled is False
    assert blocks[0]["url"] == "https://img.example.com/file/a.jpg"

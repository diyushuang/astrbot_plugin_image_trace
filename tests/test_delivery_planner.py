"""发送计划器直测：纯函数，覆盖平台画像 × 通道模式组合。

这些用例只调用 plan_delivery，不建 event、不 mock 网络，锁住 4a 抽取的判定。
"""

from __future__ import annotations

from astrbot_plugin_image_trace.delivery import (
    DeliveryConfig,
    DeliveryProfile,
    DeliveryRequest,
    plan_delivery,
)

ONE_BOT = DeliveryProfile(name="aiocqhttp", direct_send=True, offbox_upload=False)
QQ_OFFICIAL = DeliveryProfile(name="qq_official", direct_send=False, offbox_upload=True)

URL_BLOCK = {"url": "https://example.com/a.jpg"}
UNPARSEABLE_BLOCK = {"url": "https://example.com/a.bin"}
PATH_BLOCK = {"path": "C:/tmp/a.jpg"}


def _plan(
    mode: str,
    *,
    profile: DeliveryProfile = ONE_BOT,
    blocks: list[dict] | None = None,
    allow_local_fallback: bool = True,
):
    return plan_delivery(
        DeliveryRequest(
            header="h",
            blocks=[URL_BLOCK] if blocks is None else blocks,
            allow_local_fallback=allow_local_fallback,
        ),
        profile,
        DeliveryConfig(mode=mode, max_side=1920, quality=85, scaled_url_style="query"),
    )


def test_onebot_auto_compresses_bytes_then_falls_back_to_url_chain() -> None:
    plan = _plan("auto")
    assert plan.run_offbox is False
    assert plan.direct_ok is False
    assert plan.local_compress is True
    assert plan.local_bytes_chain is False
    assert plan.prepare_compress is True
    assert plan.stages == ("onebot_bytes", "url_chain")


def test_onebot_bytes_uses_same_compressed_bytes_path() -> None:
    plan = _plan("bytes")
    assert plan.local_compress is True
    assert plan.stages == ("onebot_bytes", "url_chain")


def test_onebot_native_goes_straight_to_url_chain() -> None:
    plan = _plan("native")
    assert plan.direct_ok is False
    assert plan.local_compress is False
    assert plan.local_bytes_chain is False
    assert plan.stages == ("url_chain",)


def test_onebot_url_tries_direct_then_local_bytes_chain() -> None:
    plan = _plan("url")
    assert plan.direct_ok is True
    assert plan.local_compress is False
    assert plan.local_bytes_chain is True
    # url 模式本地准备不压缩，只把原图字节塞回消息链
    assert plan.prepare_compress is False
    assert plan.stages == ("onebot_url", "chain_bytes", "url_chain")


def test_url_scaled_unparseable_extension_skips_direct_send() -> None:
    plan = _plan("url-scaled", blocks=[UNPARSEABLE_BLOCK])
    assert plan.direct_ok is False
    assert plan.local_bytes_chain is True
    assert plan.prepare_compress is True
    assert plan.stages == ("chain_bytes", "url_chain")


def test_onebot_url_mixed_url_and_path_does_not_try_direct() -> None:
    plan = _plan("url", blocks=[URL_BLOCK, PATH_BLOCK])
    assert plan.direct_ok is False
    assert plan.local_bytes_chain is False
    assert plan.stages == ("url_chain",)


def test_qq_official_auto_runs_offbox_then_chain_bytes() -> None:
    plan = _plan("auto", profile=QQ_OFFICIAL)
    assert plan.run_offbox is True
    assert plan.direct_ok is False
    assert plan.local_compress is False  # 没有 OneBot 直发能力
    assert plan.local_bytes_chain is True
    assert plan.stages == ("offbox", "chain_bytes", "url_chain")


def test_qq_official_url_stays_url_chain_after_offbox() -> None:
    plan = _plan("url", profile=QQ_OFFICIAL)
    assert plan.run_offbox is True
    assert plan.direct_ok is False
    assert plan.local_bytes_chain is False
    assert plan.stages == ("offbox", "url_chain")


def test_disabled_local_fallback_never_enters_byte_paths() -> None:
    plan = _plan("auto", allow_local_fallback=False)
    assert plan.local_compress is False
    assert plan.local_bytes_chain is False
    assert plan.stages == ("url_chain",)


def test_url_scaled_with_path_prepares_compressed_bytes() -> None:
    plan = _plan("url-scaled", blocks=[PATH_BLOCK])
    assert plan.direct_ok is False
    assert plan.local_bytes_chain is True
    assert plan.prepare_compress is True
    assert plan.stages == ("chain_bytes", "url_chain")

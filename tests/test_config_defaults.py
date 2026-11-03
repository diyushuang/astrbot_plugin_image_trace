"""配置默认值与归一化：schema 与 TypedConfig 单一来源、储存桶优先级、签到开关。

这些用例都走插件真实读取路径（`_bucket_config` / `_effective_bed_cfg` /
`_checkin_settings`），而不是只测 helper，确保薄委托没有改变取值。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from astrbot_plugin_image_trace.config import typed

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "_conf_schema.json"

_GROUPS = {
    "basic": typed.BasicConfig,
    "delivery": typed.DeliveryConfig,
    "image_bed": typed.ImageBedConfig,
    "storage_bucket": typed.StorageBucketConfig,
    "random_media": typed.RandomMediaConfig,
    "checkin": typed.CheckinConfig,
    "vector_search": typed.VectorConfig,
    "limits": typed.LimitsConfig,
}


def _normalize(value):
    """schema 里列表型默认值是 list，TypedConfig 用 tuple 表示不可变。"""
    return list(value) if isinstance(value, tuple) else value


def test_defaults_match_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    mismatches = []
    for group, cls in _GROUPS.items():
        items = schema[group]["items"]
        defaults = cls()
        for field in dataclasses.fields(defaults):
            assert field.name in items, f"{group}.{field.name} 不在 schema 中"
            expected = items[field.name].get("default")
            actual = _normalize(getattr(defaults, field.name))
            if expected != actual:
                mismatches.append(f"{group}.{field.name}: schema={expected!r} typed={actual!r}")
        missing = set(items) - {field.name for field in dataclasses.fields(defaults)}
        assert not missing, f"{group} 在 schema 中多出字段: {sorted(missing)}"
    assert not mismatches, "默认值与 schema 不一致: " + "; ".join(mismatches)


@pytest.mark.asyncio
async def test_bucket_priority_and_completeness(make_plugin):
    r2 = {
        "mode": "cloudflare_r2",
        "r2_account_id": "acc",
        "r2_access_key_id": "ak",
        "r2_secret_access_key": "sk",
        "r2_bucket": "bucket",
        "r2_public_base_url": "https://cdn.example.com",
    }
    # 完整：储存桶覆盖图床 mode，并把桶字段并进生效配置
    plugin = make_plugin(
        image_bed={"mode": "local", "token": "imglab"},
        storage_bucket=r2,
    )
    assert plugin._bucket_config()["mode"] == "cloudflare_r2"
    merged = plugin._effective_bed_cfg()
    assert merged["mode"] == "cloudflare_r2"
    assert merged["r2_bucket"] == "bucket"
    assert merged["token"] == "imglab"

    # 缺必填项：不并入图床，保留图床自身 mode
    incomplete = dict(r2)
    incomplete.pop("r2_public_base_url")
    plugin = make_plugin(image_bed={"mode": "local"}, storage_bucket=incomplete)
    assert plugin._bucket_config() is None
    assert plugin._effective_bed_cfg()["mode"] == "local"

    # OCI：公共桶与公开地址二选一，缺两个都不满足时判定不完整
    oci = {
        "mode": "oracle_oci",
        "oci_namespace": "ns",
        "oci_region": "r",
        "oci_access_key_id": "ak",
        "oci_secret_access_key": "sk",
        "oci_bucket": "b",
    }
    plugin = make_plugin(storage_bucket=oci)
    assert plugin._bucket_config() is None
    plugin = make_plugin(storage_bucket={**oci, "oci_public_base_url": "https://cdn.example.com"})
    assert plugin._bucket_config()["mode"] == "oracle_oci"

    # 伪模式（如 v1.2.x 遗留在 image_bed 里的旧值）不参与优先
    plugin = make_plugin(storage_bucket={"mode": "cloudflare_r2"})
    assert plugin._bucket_config() is None


@pytest.mark.asyncio
async def test_checkin_image_mode_default_and_override(make_plugin):
    assert make_plugin()._checkin_settings()["image_mode"] == "thumbnail"
    assert make_plugin(checkin={"image_mode": "original"})._checkin_settings()["image_mode"] == "original"
    # 非法值回落 thumbnail，与签到其它枚举字段一致
    assert make_plugin(checkin={"image_mode": "large"})._checkin_settings()["image_mode"] == "thumbnail"
    # 与 random_media 的取图开关互不影响
    assert (
        make_plugin(checkin={"image_mode": "original"}, random_media={"send_thumbnail": False})
        ._checkin_settings()["image_mode"]
        == "original"
    )


@pytest.mark.asyncio
async def test_checkin_display_switches_default_and_normalize(make_plugin):
    defaults = make_plugin()._checkin_settings()
    assert defaults["show_quote"] is True
    assert defaults["show_sender"] is True

    # 未配置（None / 空串）按默认 true；显式 false 生效，两者互不覆盖
    settings = make_plugin(
        checkin={"show_quote": False, "show_sender": None}
    )._checkin_settings()
    assert settings["show_quote"] is False
    assert settings["show_sender"] is True

    settings = make_plugin(checkin={"show_quote": "", "show_sender": False})._checkin_settings()
    assert settings["show_quote"] is True
    assert settings["show_sender"] is False

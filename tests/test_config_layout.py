"""配置布局迁移与回落：旧布局 → basic / delivery / limits。

1.7.14 把散落的同类项合并成三个组。关键约束：AstrBot 加载插件配置时会按 schema
删除已不存在的键，所以旧键不能直接删名——它们以「隐藏镜像」留在 schema 里，
迁移只做搬移 + 打版本标记。这里锁定四件事：

1. 旧布局（顶层散项 + image_delivery / qq_upload 两组）的值被搬进新组，不丢；
2. 搬迁只做一次（有版本标记），此后用户改的新值不会被旧值盖回去；
3. 新键优先于旧键（`_cfg` 运行时回落）；
4. 迁移被绕过（配置被手改回旧布局）时旧键仍能生效。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot_plugin_image_trace.config.migrations import SCALAR_PATHS
from astrbot_plugin_image_trace.main import _LAYOUT_VERSION

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "_conf_schema.json"

# 15 个隐藏旧键：12 个顶层散项 + 2 个旧配置组 + 布局版本标记。
# 它们不能从 schema 删除——AstrBot 在插件初始化前就按 schema 丢弃未声明键，
# 删掉后老用户的旧值会在 migrate_config_layout 读到之前消失。
HIDDEN_LEGACY_KEYS = (
    *SCALAR_PATHS,
    "image_delivery",
    "qq_upload",
    "config_layout",
)


def test_migration_moves_legacy_scalars_into_groups(make_plugin):
    plugin = make_plugin(
        top_n=5,
        similarity_threshold=0.9,
        max_download_mb=30,
        remote_hash_ttl=600,
    )
    cfg = plugin.config

    assert cfg["basic"]["top_n"] == 5
    assert cfg["basic"]["hash_similarity_threshold"] == 0.9
    assert cfg["limits"]["max_download_mb"] == 30
    assert cfg["limits"]["remote_hash_ttl"] == 600
    assert cfg["config_layout"] == _LAYOUT_VERSION


def test_migration_merges_both_delivery_groups(make_plugin):
    plugin = make_plugin(
        image_delivery={"mode": "url", "max_side": 1280, "quality": 70},
        qq_upload={"mode": "bytes", "direct_max_mb": 5, "on_failure": "notify"},
    )
    delivery = plugin.config["delivery"]

    assert delivery["napcat_mode"] == "url"
    assert delivery["napcat_max_side"] == 1280
    assert delivery["napcat_quality"] == 70
    assert delivery["qq_mode"] == "bytes"
    assert delivery["qq_native_max_mb"] == 5
    assert delivery["qq_on_failure"] == "notify"


def test_migrated_values_reach_the_readers(make_plugin):
    """搬完家还要真的被读到：两条推送通道各自解析出新值。"""
    plugin = make_plugin(
        image_delivery={"mode": "url", "max_side": 1280, "quality": 70},
        qq_upload={"mode": "url", "url_max_mb": 30},
    )

    mode, max_side, quality, _style = plugin._delivery_config()
    assert (mode, max_side, quality) == ("url", 1280, 70)

    qq_mode, _native, url_max, _prefer, _upload, _failure = plugin._qq_upload_config()
    assert qq_mode == "url"
    assert url_max == 30 * 1024 * 1024


def test_legacy_values_win_on_first_migration(make_plugin):
    """首次迁移以旧值为准：新键此时可能只是 schema 默认值填的。"""
    plugin = make_plugin(
        delivery={"napcat_mode": "auto"},
        image_delivery={"mode": "url"},
    )

    assert plugin.config["delivery"]["napcat_mode"] == "url"
    assert plugin._delivery_config()[0] == "url"


def test_migration_runs_only_once(make_plugin):
    """有版本标记后不再搬移：面板改过的新值不会被旧值盖回去。"""
    plugin = make_plugin(image_delivery={"mode": "url"})
    assert plugin.config["delivery"]["napcat_mode"] == "url"

    # 模拟用户在面板里改成 native，再重启（= 再跑一次迁移）
    plugin.config["delivery"]["napcat_mode"] = "native"
    plugin._migrate_config_layout()

    assert plugin.config["delivery"]["napcat_mode"] == "native"
    assert plugin._delivery_config()[0] == "native"


def test_cfg_falls_back_to_legacy_when_group_missing(make_plugin):
    """迁移被绕过时（配置被手改回旧布局）旧键仍生效。"""
    plugin = make_plugin(top_n=7)
    plugin.config.pop("basic", None)
    plugin.config["top_n"] = 7

    assert plugin._int_cfg("top_n", 3) == 7


def test_first_value_keeps_zero_and_false(make_plugin):
    """0 / False 是合法配置，不能被当成"未配置"跳过。"""
    plugin = make_plugin()

    assert plugin._first_value(0, 5) == 0
    assert plugin._first_value(False, True) is False
    assert plugin._first_value(None, "", 5) == 5


@pytest.mark.parametrize(
    ("legacy", "group", "field"),
    [
        ("search_engine", "basic", "search_engine"),
        ("scan_dirs", "basic", "scan_dirs"),
        ("ai_verify", "basic", "ai_verify"),
        ("register_admin_only", "basic", "register_admin_only"),
        ("remote_hash_enabled", "limits", "remote_hash_enabled"),
        ("remote_hash_max_points", "limits", "remote_hash_max_points"),
    ],
)
def test_scalar_paths_cover_every_moved_key(legacy, group, field):
    """每个被搬走的顶层键都在映射表里挂到了正确的组和字段。"""
    from astrbot_plugin_image_trace.main import _SCALAR_PATHS

    assert _SCALAR_PATHS[legacy] == (group, field)


def test_hidden_legacy_keys_are_still_declared():
    """15 个隐藏旧键必须继续声明：删掉会让升级用户的旧值在迁移前被 schema 清空。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert len(HIDDEN_LEGACY_KEYS) == 15
    for key in HIDDEN_LEGACY_KEYS:
        assert key in schema, f"旧键 {key} 不能从 _conf_schema.json 删除"
        assert schema[key].get("invisible") is True, f"旧键 {key} 必须保持隐藏"


_LEGACY_SCALAR_VALUES = {
    "search_engine": "hash",
    "scan_dirs": ["C:\\imgs"],
    "similarity_threshold": 0.9,
    "hash_size": 8,
    "top_n": 5,
    "max_images_per_query": 2,
    "ai_verify": True,
    "register_admin_only": False,
    "max_download_mb": 30,
    "remote_hash_enabled": False,
    "remote_hash_ttl": 600,
    "remote_hash_max_points": 100,
}


@pytest.mark.parametrize(("legacy", "target"), sorted(SCALAR_PATHS.items()))
def test_every_legacy_scalar_migrates_to_the_correct_new_key(
    make_plugin, legacy, target
):
    """12 个顶层旧键逐个钉住：迁移后新组里的新键取值等于旧值。"""
    value = _LEGACY_SCALAR_VALUES[legacy]
    group, field = target
    plugin = make_plugin(**{legacy: value})
    assert plugin.config[group][field] == value
    assert plugin.config["config_layout"] == _LAYOUT_VERSION

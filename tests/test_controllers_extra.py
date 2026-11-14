"""控制器补强：登记原图、状态、删除、签到导入的边界分支。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from astrbot.api import message_components as Comp

from astrbot_plugin_image_trace.image_bed import MODE_CFB
from astrbot_plugin_image_trace.media.image_source import ResolvedImage
from astrbot_plugin_image_trace.storage.protocol import StoreResult


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


def _patch_resolve(monkeypatch, plugin, path, *, features=None, message=""):
    async def fake_resolve(_seg, *, probe):
        return ResolvedImage(
            path=str(path),
            mime="image/jpeg",
            features=features if features is not None else _features(),
            message=message,
        )

    monkeypatch.setattr(plugin, "_resolve_local", fake_resolve)


def _patch_store(monkeypatch, plugin, result: StoreResult):
    async def fake_store(_path):
        return result

    monkeypatch.setattr(plugin.storage.active, "store", fake_store)


# ----------------------------------------------------------------------
# /登记原图
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_rejects_non_admin(make_plugin, make_event):
    plugin = make_plugin()
    event = make_event("/登记原图", chain=[_image_segment()])
    event.is_admin = lambda: False

    results = await _collect(plugin.register_image(event))
    assert "仅管理员" in _text(results[0])


@pytest.mark.asyncio
async def test_register_without_image_prompts_usage(make_plugin, make_event):
    plugin = make_plugin()
    event = make_event("/登记原图")

    results = await _collect(plugin.register_image(event))
    assert "请与图片同条消息" in _text(results[0])


@pytest.mark.asyncio
async def test_register_reports_resolve_failure(make_plugin, make_event, monkeypatch, tmp_path):
    plugin = make_plugin()
    event = make_event("/登记原图", chain=[_image_segment()])

    async def fake_resolve(_seg, *, probe):
        return ResolvedImage(path="", message="图片无法读取")

    monkeypatch.setattr(plugin, "_resolve_local", fake_resolve)

    results = await _collect(plugin.register_image(event))
    assert "图片无法读取" in _text(results[0])


@pytest.mark.asyncio
async def test_register_rejects_oversized_file(make_plugin, make_event, monkeypatch, tmp_path):
    plugin = make_plugin()
    path = tmp_path / "big.jpg"
    path.write_bytes(b"x" * 32)
    event = make_event("/登记原图", chain=[_image_segment()])
    _patch_resolve(monkeypatch, plugin, path)
    monkeypatch.setattr(plugin, "_download_limit_bytes", lambda: 8)

    results = await _collect(plugin.register_image(event))
    assert "超过大小上限" in _text(results[0])


@pytest.mark.asyncio
async def test_register_reports_duplicate_before_upload(
    make_plugin, make_event, monkeypatch, tmp_path
):
    plugin = make_plugin()
    path = tmp_path / "a.jpg"
    path.write_bytes(b"x")
    event = make_event("/登记原图 备注", chain=[_image_segment()])
    _patch_resolve(monkeypatch, plugin, path)
    monkeypatch.setattr(
        plugin.library,
        "find_by_phash",
        lambda _phash: {"id": 7, "note": "已存在"},
    )

    results = await _collect(plugin.register_image(event))
    text = _text(results[0])
    assert "#7" in text and "已存在" in text


@pytest.mark.asyncio
async def test_register_success_with_public_url(
    make_plugin, make_event, monkeypatch, tmp_path
):
    plugin = make_plugin()
    path = tmp_path / "a.jpg"
    path.write_bytes(b"x" * 10)
    event = make_event("/登记原图 风景", chain=[_image_segment()])
    _patch_resolve(monkeypatch, plugin, path)
    monkeypatch.setattr(plugin.library, "find_by_phash", lambda _phash: None)
    monkeypatch.setattr(plugin.library, "add_if_absent", lambda _row: (3, None))
    _patch_store(
        monkeypatch,
        plugin,
        StoreResult(ok=True, url="https://cdn.example.com/a.jpg", message="已上传"),
    )

    results = await _collect(plugin.register_image(event))
    text = _text(results[0])
    assert "已登记原图 #3" in text
    assert "备注：风景" in text
    assert "直链：https://cdn.example.com/a.jpg" in text


@pytest.mark.asyncio
async def test_register_cleans_up_when_insert_loses_race(
    make_plugin, make_event, monkeypatch, tmp_path
):
    plugin = make_plugin()
    path = tmp_path / "a.jpg"
    path.write_bytes(b"x")
    event = make_event("/登记原图", chain=[_image_segment()])
    _patch_resolve(monkeypatch, plugin, path)
    monkeypatch.setattr(plugin.library, "find_by_phash", lambda _phash: None)
    monkeypatch.setattr(
        plugin.library,
        "add_if_absent",
        lambda _row: (None, {"id": 9, "note": "并发重复"}),
    )
    stored = StoreResult(ok=True, url="https://cdn.example.com/a.jpg", message="已上传")
    _patch_store(monkeypatch, plugin, stored)
    cleaned = []

    async def fake_cleanup(item):
        cleaned.append(item)

    monkeypatch.setattr(plugin, "_cleanup_stored", fake_cleanup)

    results = await _collect(plugin.register_image(event))
    assert "#9" in _text(results[0])
    assert cleaned == [stored]


@pytest.mark.asyncio
async def test_register_skips_vector_sync_for_imgbed_hook(
    make_plugin, make_event, monkeypatch, tmp_path
):
    plugin = make_plugin()
    path = tmp_path / "a.jpg"
    path.write_bytes(b"x")
    event = make_event("/登记原图", chain=[_image_segment()])
    _patch_resolve(monkeypatch, plugin, path)
    monkeypatch.setattr(plugin.library, "find_by_phash", lambda _phash: None)
    monkeypatch.setattr(plugin.library, "add_if_absent", lambda _row: (1, None))
    _patch_store(
        monkeypatch,
        plugin,
        StoreResult(ok=True, url="https://cdn.example.com/a.jpg", message="已上传"),
    )
    plugin.vector = SimpleNamespace(enabled=True)
    monkeypatch.setattr(plugin, "_bed_mode", lambda: MODE_CFB)

    results = await _collect(plugin.register_image(event))
    assert "已同步至 Qdrant" not in _text(results[0])


@pytest.mark.asyncio
async def test_register_vector_sync_success_and_failure(
    make_plugin, make_event, monkeypatch, tmp_path
):
    plugin = make_plugin()
    path = tmp_path / "a.jpg"
    path.write_bytes(b"x")
    event = make_event("/登记原图", chain=[_image_segment()])
    _patch_resolve(monkeypatch, plugin, path)
    monkeypatch.setattr(plugin.library, "find_by_phash", lambda _phash: None)
    monkeypatch.setattr(plugin.library, "add_if_absent", lambda _row: (1, None))
    stored = StoreResult(
        ok=True, url="https://cdn.example.com/a.jpg", message="已上传"
    )
    _patch_store(monkeypatch, plugin, stored)
    monkeypatch.setattr(plugin.storage.active, "src_path", lambda _url: "src/a.jpg")
    monkeypatch.setattr(plugin, "_bed_mode", lambda: "generic")

    calls = []

    async def ok_upsert(*args, **kwargs):
        calls.append((args, kwargs))

    plugin.vector = SimpleNamespace(enabled=True, upsert_file=ok_upsert)
    results = await _collect(plugin.register_image(event))
    assert "已同步至 Qdrant" in _text(results[0])
    assert calls

    async def bad_upsert(*_args, **_kwargs):
        raise RuntimeError("qdrant down")

    plugin.vector = SimpleNamespace(enabled=True, upsert_file=bad_upsert)
    results = await _collect(plugin.register_image(event))
    assert "向量同步失败" in _text(results[0])


@pytest.mark.asyncio
async def test_register_cleans_up_stored_on_unexpected_error(
    make_plugin, make_event, monkeypatch, tmp_path
):
    plugin = make_plugin()
    path = tmp_path / "a.jpg"
    path.write_bytes(b"x")
    event = make_event("/登记原图", chain=[_image_segment()])
    _patch_resolve(monkeypatch, plugin, path)
    monkeypatch.setattr(plugin.library, "find_by_phash", lambda _phash: None)
    stored = StoreResult(ok=True, url="https://cdn.example.com/a.jpg", message="已上传")
    _patch_store(monkeypatch, plugin, stored)
    cleaned = []

    async def fake_cleanup(item):
        cleaned.append(item)

    def boom(_row):
        raise RuntimeError("db locked")

    monkeypatch.setattr(plugin, "_cleanup_stored", fake_cleanup)
    monkeypatch.setattr(plugin.library, "add_if_absent", boom)

    results = await _collect(plugin.register_image(event))
    assert "登记失败" in _text(results[0])
    assert cleaned == [stored]


# ----------------------------------------------------------------------
# /溯源状态、/溯源删除
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reports_skipped_sources_and_vector_error(
    make_plugin, make_event, monkeypatch
):
    plugin = make_plugin()
    monkeypatch.setattr(
        plugin.library,
        "stats",
        lambda: {"total": 3, "indexed": 2, "skipped": 1, "by_source": {"scan": 2}},
    )

    async def boom():
        raise RuntimeError("qdrant down")

    plugin.vector = SimpleNamespace(
        enabled=True,
        count=boom,
        collection="images",
        embed_model="m",
        image_input="i",
    )
    event = make_event("/溯源状态")

    results = await _collect(plugin.status(event))
    text = _text(results[0])
    assert "共 3 条" in text
    assert "哈希精度不匹配" in text
    assert "来源：scan 2" in text
    assert "不可用（连接或鉴权异常" in text


@pytest.mark.asyncio
async def test_status_vector_count_success(make_plugin, make_event, monkeypatch):
    plugin = make_plugin()

    async def count():
        return 12

    plugin.vector = SimpleNamespace(
        enabled=True,
        count=count,
        collection="images",
        embed_model="m",
        image_input="i",
    )
    event = make_event("/溯源状态")

    text = _text((await _collect(plugin.status(event)))[0])
    assert "向量库：images，12 条" in text
    assert "Embed：m / i" in text


@pytest.mark.asyncio
async def test_delete_missing_and_success_with_cleanup(
    make_plugin, make_event, monkeypatch
):
    plugin = make_plugin()
    event = make_event("/溯源删除 7")
    monkeypatch.setattr(plugin.library, "get", lambda _id: None)

    results = await _collect(plugin.delete_entry(event, 7))
    assert "未找到编号 #7" in _text(results[0])

    row = {
        "id": 7,
        "phash": "a" * 64,
        "note": "风景",
        "file_path": "",
        "image_url": "https://cdn.example.com/a.jpg",
        "source": "register",
    }
    monkeypatch.setattr(plugin.library, "get", lambda _id: row)
    monkeypatch.setattr(plugin.library, "delete", lambda _id: None)
    removed = []
    monkeypatch.setattr(plugin, "_remove_local_copy", lambda item: removed.append(item))

    async def delete_remote(_url):
        return True

    monkeypatch.setattr(plugin.storage.active, "delete", delete_remote)

    results = await _collect(plugin.delete_entry(event, 7))
    text = _text(results[0])
    assert "已删除 #7" in text
    assert "风景" in text
    assert "远端对象已删除" in text
    assert removed == [row]


@pytest.mark.asyncio
async def test_delete_reports_library_error(make_plugin, make_event, monkeypatch):
    plugin = make_plugin()
    event = make_event("/溯源删除 7")

    def boom(_id):
        raise RuntimeError("db locked")

    monkeypatch.setattr(plugin.library, "get", boom)

    results = await _collect(plugin.delete_entry(event, 7))
    assert "删除失败" in _text(results[0])


# ----------------------------------------------------------------------
# /签到文案导入
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkin_import_rejects_non_admin(make_plugin, make_event):
    plugin = make_plugin()
    event = make_event("/签到文案导入")
    event.is_admin = lambda: False

    results = await _collect(plugin.checkin_import(event))
    assert "仅管理员" in _text(results[0])


@pytest.mark.asyncio
async def test_checkin_import_requires_file(make_plugin, make_event):
    plugin = make_plugin()
    event = make_event("/签到文案导入")

    results = await _collect(plugin.checkin_import(event))
    assert "请把 CSV 或 MD 文件" in _text(results[0])

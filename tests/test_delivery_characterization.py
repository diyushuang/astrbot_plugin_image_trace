"""发送层行为基线：临时文件清理。

重构前的 ``_yield_delivery`` 会在第三方转存平台的格式体检阶段产生本地转码
产物（``block["_tmp_owned"]``），并在生成器的 ``finally`` 里删除。这里不改生产
代码，直接驱动真实的 ``_yield_delivery``（只把「体检 + 上传接管」两个远端动作
替换成桩），锁定两种退出路径：

- 投递成功（通道接管已自行发完）；
- 投递中途抛异常。

两条路径都必须把临时文件删掉，否则每次 /随机图、/原图 都会累积一份转码图。
"""

from __future__ import annotations

import pytest


def _owned_block(path) -> dict:
    return {"text": "图 1", "path": str(path), "_tmp_owned": True}


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

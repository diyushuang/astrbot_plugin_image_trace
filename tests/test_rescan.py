"""`/溯源重扫` 的批量并发与 pHash 去重回归。"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_image_trace import main as main_module


def _fake_features_factory(lock: threading.Lock, state: dict):
    """用可控 sleep 的假特征计算，观察批次内是否真的并发。"""

    def fake(path: str, hash_size: int):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        data = Path(path).read_bytes()
        with lock:
            state["active"] -= 1
        digest = hashlib.sha256(data).hexdigest()
        return SimpleNamespace(
            phash=digest,
            dhash=digest[:16],
            ahash=digest[:16],
            width=1,
            height=1,
        )

    return fake


async def test_rescan_runs_bounded_concurrency_and_dedupes_phash(
    make_plugin, make_event, tmp_path, monkeypatch
):
    """重扫应并发解码（峰值 ≥ 2），且同内容的文件只入库一次。"""
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir()
    for index in range(3):
        (scan_dir / f"{index}.jpg").write_bytes(f"image-{index}".encode())
    (scan_dir / "copy.jpg").write_bytes((scan_dir / "0.jpg").read_bytes())

    lock = threading.Lock()
    state = {"active": 0, "peak": 0}
    monkeypatch.setattr(main_module, "compute_features", _fake_features_factory(lock, state))

    plugin = make_plugin(scan_dirs=[str(scan_dir)])
    event = make_event("/溯源重扫")

    results = [item async for item in plugin.rescan(event)]

    text = results[-1].text
    assert "共 4 张" in text
    assert "新增 3" in text
    assert "跳过 1" in text
    assert "失败 0" in text
    assert plugin.library.count() == 3
    assert state["peak"] >= 2, "重扫应在批次内并发解码"
    assert event.sent, "应通过 event.send 汇报开始/进度"

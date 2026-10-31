"""哈希两条腿（本地图库 + 图床镜像）合并检索的测试。

覆盖 _merged_hash_candidates 的排序口径，以及 _hash_pool_size 对「空库」判定的
修正——只看本地图库会在「本地空、图床有」时误报空库信息。
"""

from __future__ import annotations

import types

from astrbot_plugin_image_trace.hash_index import HashIndex
from astrbot_plugin_image_trace.library import MatchResult

VECTOR_CFG = {
    "search_engine": "auto",
    "vector_search": {
        "qdrant_url": "http://127.0.0.1:6333",
        "embed_base_url": "http://127.0.0.1:8000",
        "embed_model": "m",
    },
}


def _local(similarity: float, entry_id: int = 1, note: str = "本地备注") -> MatchResult:
    return MatchResult(
        id=entry_id,
        similarity=similarity,
        image_url="https://img.example.com/file/local.jpg",
        file_path="C:/images/local.jpg",
        note=note,
        source="register",
        width=100,
        height=50,
        created_at="2026-01-01 00:00:00",
        file_size=1234,
    )


def _remote_only(plugin, monkeypatch, remote_matches):
    """把本地腿置空、图床腿注入给定命中，隔离验证图床那条腿。"""
    monkeypatch.setattr(plugin.library, "search", lambda _p, _n: [])
    monkeypatch.setattr(plugin.remote_hash, "search", lambda _p, _n: remote_matches)
    monkeypatch.setattr(plugin.remote_hash, "_cache", (list(remote_matches), None, 16, 0))
    monkeypatch.setattr(type(plugin.remote_hash), "is_empty", property(lambda _s: False))
    monkeypatch.setattr(plugin, "remote_hash_on", lambda: True)


async def test_merge_prefers_local_on_equal_similarity(make_plugin, data_dir, monkeypatch):
    """同分时本地优先：本地能给出路径，回传链路更短、少一次网络往返。

    两条腿都给出 1.0，合并后本地那条必须排第一（唯一的区别就在 tie-break）。
    """
    plugin = make_plugin(**VECTOR_CFG)
    monkeypatch.setattr(plugin.library, "search", lambda _p, _n: [_local(1.0)])

    remote = HashIndex(str(data_dir / "r.json"), expected_phash_hex_len=64)
    remote.build(
        [
            {
                "id": "r",
                "payload": {
                    "phash": "0" * 64,
                    "image_url": "https://img.example.com/file/remote.jpg",
                    "file_name": "图床图.jpg",
                },
            }
        ]
    )
    plugin.remote_hash = remote

    hits, _lines = plugin._merged_hash_candidates("0" * 64, 5)

    assert len(hits) == 2
    assert hits[0].note == "本地备注"  # 本地腿的 note 非空，图床腿恒为 ""
    assert hits[0].file_path == "C:/images/local.jpg"  # 证明是本地那条排在前面
    assert hits[1].file_name == "图床图.jpg"


async def test_merge_ranks_by_similarity_not_by_leg(make_plugin, data_dir, monkeypatch):
    """本地腿的低分命中不能挡住图床腿的高分命中。

    这正是顺序短路（本地先查、没中才查图床）会犯的错：本地有条 0.86 的勉强命中，
    就直接返回了，图床那条 0.99 的完美命中永远轮不到。
    """
    plugin = make_plugin(**VECTOR_CFG)
    monkeypatch.setattr(plugin.library, "search", lambda _p, _n: [_local(0.86)])

    remote = HashIndex(str(data_dir / "r.json"), expected_phash_hex_len=64)
    remote.build(
        [
            {
                "id": "r",
                "payload": {
                    "phash": "f" * 64,
                    "image_url": "https://img.example.com/file/remote.jpg",
                    "file_name": "图床完美命中.jpg",
                },
            }
        ]
    )
    plugin.remote_hash = remote
    monkeypatch.setattr(plugin.library, "search", lambda _p, _n: [_local(0.86)])

    # 查询串与远端哈希相同 → 图床腿 1.0，本地腿固定 0.86
    hits, _lines = plugin._merged_hash_candidates("f" * 64, 5)

    assert len(hits) == 2
    assert hits[0].similarity > hits[1].similarity
    assert hits[0].file_name == "图床完美命中.jpg"


async def test_local_search_failure_does_not_kill_remote_leg(make_plugin, data_dir, monkeypatch):
    """本地腿抛错（SQLite 锁、库损坏）时，图床腿仍要能给出结果。"""

    def boom(_p, _n):
        raise RuntimeError("database is locked")

    plugin = make_plugin(**VECTOR_CFG)
    monkeypatch.setattr(plugin.library, "search", boom)

    remote = HashIndex(str(data_dir / "r.json"), expected_phash_hex_len=64)
    remote.build(
        [
            {
                "id": "r",
                "payload": {
                    "phash": "a" * 64,
                    "image_url": "https://img.example.com/file/remote.jpg",
                    "file_name": "图床图.jpg",
                },
            }
        ]
    )
    plugin.remote_hash = remote

    hits, _lines = plugin._merged_hash_candidates("a" * 64, 5)

    assert len(hits) == 1
    assert hits[0].file_name == "图床图.jpg"


async def test_hash_pool_size_counts_both_legs(make_plugin, data_dir):
    """空库判定要按两条腿之和：本地空但有图床索引时，不该提示「图库是空的」。

    误报空库会把用户引向 scan_dirs / 溯源重扫——而实际哈希腿完全可用。
    """
    plugin = make_plugin(**VECTOR_CFG)
    assert plugin._hash_pool_size() == 0

    remote = HashIndex(str(data_dir / "r.json"), expected_phash_hex_len=64)
    remote.build(
        [
            {
                "id": "r",
                "payload": {"phash": "a" * 64, "image_url": "https://img.example.com/f.jpg"},
            }
        ]
    )
    plugin.remote_hash = remote

    assert plugin._hash_pool_size() == 1


async def test_candidate_note_uses_file_name_for_remote(make_plugin):
    """图床腿没有 note，候选行必须退回文件名，否则括注恒为空。"""
    plugin = make_plugin(**VECTOR_CFG)
    from astrbot_plugin_image_trace.hash_index import RemoteMatch

    remote_match = RemoteMatch(
        id="r",
        similarity=0.9,
        image_url="https://img.example.com/f.jpg",
        file_name="图床文件名.jpg",
    )
    assert plugin._candidate_note(remote_match) == "图床文件名.jpg"
    assert plugin._candidate_note(_local(0.9)) == "本地备注"


async def test_remote_hash_disabled_when_qdrant_absent(make_plugin, monkeypatch):
    """Qdrant 未配置时拉不到数据，图床腿必须整体关闭而不是空跑。"""
    plugin = make_plugin(**VECTOR_CFG)
    plugin.vector = types.SimpleNamespace(qdrant_ready=False)
    assert plugin.remote_hash_on() is False

    plugin.vector = types.SimpleNamespace(qdrant_ready=True)
    assert plugin.remote_hash_on() is True
    monkeypatch.setattr(plugin, "_bool_cfg", lambda key, default=None: False)
    assert plugin.remote_hash_on() is False

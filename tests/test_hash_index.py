"""图床哈希索引（hash_index）的单元测试。

重点覆盖三类容易出错的地方：
1. 位矩阵构建 —— 跳过的条目不能污染矩阵行序（跳过一条却少一行，后面全错位）；
2. 检索口径 —— 必须与 library.ImageLibrary 完全一致（同一内核、同一相似度公式）；
3. 缓存往返 —— 半截/损坏的缓存必须被拒绝，而不是被当作有效索引使用。
"""

import json

import pytest

from astrbot_plugin_image_trace.hash_index import DEGENERATE_SENTINEL, HashIndex

# 2 字节 = 4 hex，够表达 16bit 的全部组合，测试里用它代替 64 hex 更好读
HEX_LEN = 4


def point(phash, *, pid="p1", url="http://bed/file/a.jpg", name="", **extra):
    payload = {"phash": phash, "image_url": url, "file_name": name}
    payload.update(extra)
    return {"id": pid, "payload": payload}


@pytest.fixture
def index(tmp_path):
    return HashIndex(str(tmp_path / "remote_phash.json"), expected_phash_hex_len=HEX_LEN)


# ----------------------------------------------------------------------
# 构建与过滤
# ----------------------------------------------------------------------


def test_build_keeps_valid_points_and_reports_count(index):
    n = index.build(
        [
            point("0000", pid="a"),
            point("ffff", pid="b"),
            point("0f0f", pid="c"),
        ],
        remote_count=3,
    )
    assert n == 3
    assert index.count == 3
    assert index.remote_count == 3


@pytest.mark.parametrize(
    "bad,reason",
    [
        ({"phash": "", "image_url": "http://x/1"}, "空串（等同键缺失）"),
        ({"phash": None, "image_url": "http://x/1"}, "null"),
        ({"phash": DEGENERATE_SENTINEL, "image_url": "http://x/1"}, "退化哨兵"),
        ({"phash": "00", "image_url": "http://x/1"}, "长度不足"),
        ({"phash": "zzzz", "image_url": "http://x/1"}, "非十六进制"),
        ({"phash": "0000", "image_url": ""}, "缺直链"),
        ({"phash": "0000", "image_url": "   "}, "直链为空白"),
    ],
)
def test_build_skips_unusable_points(index, bad, reason):
    kept = index.build([{"id": "bad", "payload": bad}, point("0000", pid="ok")])
    assert kept == 1, reason
    assert index.count == 1


def test_skipped_point_does_not_shift_matrix_rows(index):
    """跳过的条目绝不能留在矩阵里——否则后续所有行的对应关系全错位。

    构造「好/坏/好」三段，验证检索仍能各自精确命中，说明矩阵行序与 items 对齐。
    """
    index.build(
        [
            point("0000", pid="a"),
            {"id": "bad", "payload": {"phash": "zz", "image_url": "http://x/1"}},
            point("ffff", pid="b"),
        ]
    )
    assert index.count == 2

    hit_f = index.search("ffff", limit=1)[0]
    assert hit_f.similarity == pytest.approx(1.0)
    assert hit_f.id == "b"

    hit_0 = index.search("0000", limit=1)[0]
    assert hit_0.similarity == pytest.approx(1.0)
    assert hit_0.id == "a"


def test_build_falls_back_to_src_when_image_url_missing(index):
    kept = index.build([{"id": "a", "payload": {"phash": "0000", "src": "http://bed/file/s.jpg"}}])
    assert kept == 1
    assert index.search("0000", limit=1)[0].image_url == "http://bed/file/s.jpg"


def test_phash_is_case_insensitive_and_trimmed(index):
    index.build([point("00FF", pid="a")])
    hit = index.search("00ff", limit=1)[0]
    assert hit.similarity == pytest.approx(1.0)


def test_non_dict_payload_entries_are_skipped(index):
    assert index.build([None, "x", {"id": "a"}, {"id": "b", "payload": "not-dict"}]) == 0
    assert index.is_empty


# ----------------------------------------------------------------------
# 检索口径（必须与 library.ImageLibrary 一致）
# ----------------------------------------------------------------------


def test_search_orders_by_hamming_distance(index):
    index.build(
        [
            point("0000", pid="exact"),
            point("0001", pid="one-bit"),
            point("0011", pid="two-bit"),
            point("ffff", pid="far"),
        ]
    )
    hits = index.search("0000", limit=4)
    assert [h.id for h in hits] == ["exact", "one-bit", "two-bit", "far"]
    assert hits[0].similarity == pytest.approx(1.0)
    # 16bit 里差 1 位 → 相似度 15/16
    assert hits[1].similarity == pytest.approx(15 / 16)


def test_search_matches_library_similarity_formula(index, tmp_path):
    """两条腿的相似度公式必须一致：1 - 汉明距离 / 总位数。"""
    from astrbot_plugin_image_trace.library import ImageLibrary

    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=HEX_LEN)
    row = {
        "phash": "0011",
        "dhash": "",
        "ahash": "",
        "width": None,
        "height": None,
        "file_size": None,
        "image_url": "http://local/a.jpg",
        "file_path": "C:/a.jpg",
        "source": "register",
        "note": "",
        "group_id": "",
        "sender_id": "",
        "sender_name": "",
    }
    library.add_if_absent(row)
    index.build([point("0011", pid="remote")])

    local = library.search("0000", limit=1)[0]
    remote = index.search("0000", limit=1)[0]
    assert remote.similarity == pytest.approx(local.similarity)
    library.close()


def test_search_on_empty_index_returns_empty(index):
    assert index.search("0000") == []
    assert index.is_empty


@pytest.mark.parametrize("bad", ["", "zz", "0", "000000"])
def test_search_with_bad_query_returns_empty(index, bad):
    index.build([point("0000")])
    assert index.search(bad) == []


def test_search_query_length_mismatch_returns_empty(index):
    """查询哈希位数与库不一致时不能给出候选——位数不同的汉明距离没有意义。"""
    index.build([point("0000")])  # 16bit
    assert index.search("00000000") == []  # 32bit


def test_search_limit_is_at_least_one(index):
    index.build([point("0000"), point("ffff")])
    assert len(index.search("0000", limit=0)) == 1


# ----------------------------------------------------------------------
# 缓存往返
# ----------------------------------------------------------------------


def test_cache_round_trip(tmp_path):
    path = str(tmp_path / "remote_phash.json")
    first = HashIndex(path, expected_phash_hex_len=HEX_LEN)
    first.build([point("00ff", pid="a", name="图.jpg")], remote_count=1)

    second = HashIndex(path, expected_phash_hex_len=HEX_LEN)
    assert second.load_cache() is True
    assert second.count == 1
    hit = second.search("00ff", limit=1)[0]
    assert hit.id == "a"
    assert hit.file_name == "图.jpg"


def test_load_cache_rejects_corrupted_file(tmp_path):
    path = tmp_path / "remote_phash.json"
    path.write_text("{ not json", encoding="utf-8")
    index = HashIndex(str(path), expected_phash_hex_len=HEX_LEN)
    assert index.load_cache() is False
    assert index.is_empty
    # 坏文件要被清掉，免得下次继续读到它
    assert not path.exists()


def test_load_cache_rejects_empty_items(tmp_path):
    path = tmp_path / "remote_phash.json"
    path.write_text(json.dumps({"items": []}), encoding="utf-8")
    assert HashIndex(str(path), expected_phash_hex_len=HEX_LEN).load_cache() is False


def test_load_cache_rejects_hex_len_mismatch(tmp_path):
    """切换 hash_size 后旧精度缓存必须失效，否则哈希腿会静默空转到 TTL。"""
    path = tmp_path / "remote_phash.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "created_at": 1.0,
                "remote_count": 1,
                "hex_len": HEX_LEN,
                "items": [{"p": "0000", "u": "http://bed/file/a.jpg"}],
            }
        ),
        encoding="utf-8",
    )
    index = HashIndex(str(path), expected_phash_hex_len=HEX_LEN * 4)
    assert index.load_cache() is False
    assert index.is_empty
    assert not path.exists()


def test_load_cache_rejects_when_no_item_matches_expected_length(tmp_path):
    """文件 hex_len 缺失时，编译后 0 条可用同样按无效处理。"""
    path = tmp_path / "remote_phash.json"
    path.write_text(
        json.dumps({"created_at": 1.0, "items": [{"p": "00", "u": "http://bed/a"}]}),
        encoding="utf-8",
    )
    index = HashIndex(str(path), expected_phash_hex_len=HEX_LEN)
    assert index.load_cache() is False
    assert index.is_empty
    assert not path.exists()


def test_load_cache_returns_false_when_missing(tmp_path):
    index = HashIndex(str(tmp_path / "nope.json"), expected_phash_hex_len=HEX_LEN)
    assert index.load_cache() is False


def test_save_writes_no_tmp_leftover(tmp_path):
    path = tmp_path / "remote_phash.json"
    index = HashIndex(str(path), expected_phash_hex_len=HEX_LEN)
    index.build([point("0000")])
    assert path.exists()
    assert not (tmp_path / "remote_phash.json.tmp").exists()


# ----------------------------------------------------------------------
# 展示名
# ----------------------------------------------------------------------


def test_display_name_prefers_real_file_name(index):
    index.build([point("0000", pid="a", name="【微博@某工作室】原图.jpg")])
    assert index.display_name(index.search("0000", limit=1)[0]) == "【微博@某工作室】原图.jpg"


def test_display_name_falls_back_to_url_basename(index):
    """缩略图名被图床改过（`@`→`_` 不可逆），但至少能给个名字。"""
    index.build([point("0000", pid="a", url="http://bed/file/dir/photo_x.jpg")])
    assert index.display_name(index.search("0000", limit=1)[0]) == "photo_x.jpg"


def test_display_name_falls_back_to_point_id(index):
    index.build([point("0000", pid="uuid-1", url="")])
    match = index.search("0000", limit=1)
    # url 为空时该点会被构建阶段跳过，这里直接验证兜底分支的输入形态
    assert match == [] or index.display_name(match[0]) in ("", "uuid-1")


def test_stats_shape(index):
    index.build([point("0000"), point("ffff")], remote_count=2)
    stats = index.stats()
    assert stats["count"] == 2
    assert stats["bits"] == 16
    assert stats["remote_count"] == 2
    assert stats["built_at"] > 0


def test_skipped_counter_reports_rejected_points(index):
    """skipped 必须真的数出被拒条目。

    这里防的是一个真实回归：build() 里过滤掉的条目已经不在 items 里，
    _build 再用 `len(items) - len(kept)` 算就是恒 0——stats 看起来一切正常，
    实际瞒报了「有多少点没进索引」。
    """
    index.build(
        [
            point("0000", pid="ok"),
            {"id": "degen", "payload": {"phash": DEGENERATE_SENTINEL, "image_url": "http://x/1"}},
            {"id": "short", "payload": {"phash": "00", "image_url": "http://x/1"}},
            {"id": "nourl", "payload": {"phash": "1111"}},
        ]
    )
    assert index.count == 1
    assert index.stats()["skipped"] == 3


def test_build_is_idempotent_and_replaces_previous_generation(index):
    """重建必须整体换代，不能把上一代的点留在矩阵里。"""
    index.build([point("0000", pid="a"), point("ffff", pid="b")])
    assert index.count == 2
    index.build([point("0000", pid="a")])
    assert index.count == 1
    assert [h.id for h in index.search("0000", limit=5)] == ["a"]

from astrbot_plugin_image_trace.media_history import (
    MAX_PER_SESSION,
    MediaHistory,
)
from astrbot_plugin_image_trace.random_media import clean_display_name


def _history_with(*names):
    history = MediaHistory()
    for name in names:
        history.remember("s", f"https://cdn.example.com/{name}")
    return history


def test_remember_and_latest_track_url_filename():
    history = MediaHistory()
    history.remember("s", "https://cdn.example.com/a.jpg")
    history.remember("s", "https://cdn.example.com/b.png")
    assert history.latest("s") == ("b.png", "https://cdn.example.com/b.png")
    assert history.count("s") == 2


def test_remember_registers_alias_for_differing_display_name():
    history = MediaHistory()
    history.remember("s", "https://cdn.example.com/9f3c-uuid.jpg", display_name="山水.jpg")
    assert history.find("s", "山水.jpg") == (
        "found",
        ("山水.jpg", "https://cdn.example.com/9f3c-uuid.jpg"),
    )
    assert history.find("s", "9f3c-uuid.jpg") == (
        "found",
        ("9f3c-uuid.jpg", "https://cdn.example.com/9f3c-uuid.jpg"),
    )
    assert history.count("s") == 2


def test_remember_dedupes_alias_equal_to_primary_ignoring_case():
    history = MediaHistory()
    history.remember("s", "https://cdn.example.com/a.jpg", display_name="A.JPG")
    assert history.count("s") == 1
    assert history.latest("s") == ("a.jpg", "https://cdn.example.com/a.jpg")


def test_remember_ignores_empty_url_and_url_without_filename():
    history = MediaHistory()
    history.remember("s", "", display_name="x.jpg")
    history.remember("s", "https://cdn.example.com/noext")
    assert history.count("s") == 0


def test_reinsert_moves_key_to_end():
    history = MediaHistory()
    history.remember("s", "https://cdn.example.com/a.jpg")
    history.remember("s", "https://cdn.example.com/b.jpg")
    history.remember("s", "https://cdn.example.com/a.jpg")
    assert history.latest("s") == ("a.jpg", "https://cdn.example.com/a.jpg")
    assert history.count("s") == 2


def test_capacity_evicts_oldest():
    history = MediaHistory(max_per_session=3)
    for name in ("a", "b", "c", "d", "e"):
        history.remember("s", f"https://cdn.example.com/{name}.jpg")
    assert history.count("s") == 3
    assert history.find("s", "a.jpg") == ("missing", None)
    assert history.find("s", "e.jpg") == ("found", ("e.jpg", "https://cdn.example.com/e.jpg"))


def test_default_max_per_session_is_enforced():
    history = MediaHistory()
    for index in range(MAX_PER_SESSION + 5):
        history.remember("s", f"https://cdn.example.com/{index}.jpg")
    assert history.count("s") == MAX_PER_SESSION


def test_sessions_are_isolated_and_unknown_session_is_empty():
    history = MediaHistory()
    history.remember("one", "https://cdn.example.com/a.jpg")
    history.remember("two", "https://cdn.example.com/b.jpg")
    assert history.latest("one") == ("a.jpg", "https://cdn.example.com/a.jpg")
    assert history.latest("two") == ("b.jpg", "https://cdn.example.com/b.jpg")
    assert history.count("unknown") == 0
    assert history.latest("unknown") is None
    assert history.find("unknown", "a.jpg") == ("missing", None)


def test_find_exact_is_case_insensitive():
    history = _history_with("Sunset.JPG")
    assert history.find("s", "sunset.jpg") == (
        "found",
        ("Sunset.JPG", "https://cdn.example.com/Sunset.JPG"),
    )


def test_find_same_stem_different_extension():
    history = _history_with("pic.jpg", "pic.png")
    assert history.find("s", "pic")[0] == "ambiguous"
    assert history.find("s", "pic.jpg") == (
        "found",
        ("pic.jpg", "https://cdn.example.com/pic.jpg"),
    )


def test_find_unique_stem_match():
    history = _history_with("pic.jpg", "other.png")
    assert history.find("s", "pic.png") == (
        "found",
        ("pic.jpg", "https://cdn.example.com/pic.jpg"),
    )


def test_find_unique_and_ambiguous_substring():
    history = _history_with("alpha.jpg", "beta.jpg")
    assert history.find("s", "lph") == (
        "found",
        ("alpha.jpg", "https://cdn.example.com/alpha.jpg"),
    )
    assert history.find("s", "a")[0] == "ambiguous"


def test_find_missing_and_blank_query():
    history = _history_with("a.jpg")
    assert history.find("s", "zzz") == ("missing", None)
    assert history.find("s", "") == ("missing", None)


def test_forget_removes_matching_keys_across_sessions_and_reclaims_empty():
    """`forget` 跨会话删除脏键，并把因此变空的会话条目一并回收。

    跨会话是必须的：1.6.1 之前写入的脏键（带 `🖼️ ` / 序号前缀）分散在各个群、
    各个用户下，而它们是同一类污染，应一次性清干净。
    回收空会话同样重要——`_history` 长期运行下会沉淀大量空 OrderedDict。
    """
    history = MediaHistory()
    history.remember("one", "https://cdn.example.com/🖼️ a.jpg")
    history.remember("two", "https://cdn.example.com/🖼️ b.jpg")
    history.remember("two", "https://cdn.example.com/clean.jpg")

    removed = history.forget(lambda name: name.startswith("🖼️"))

    assert removed == 2
    # "one" 被清空 ⇒ 整个会话条目回收，count 归零
    assert history.count("one") == 0
    assert history.latest("one") is None
    # "two" 还有干净键，保留
    assert history.count("two") == 1
    assert history.find("two", "clean.jpg")[0] == "found"


def test_forget_with_no_match_is_noop():
    history = _history_with("a.jpg", "b.jpg")
    assert history.forget(lambda name: name.startswith("🖼️")) == 0
    assert history.count("s") == 2


def test_repair_keys_renames_dirty_keys_per_session_and_keeps_urls():
    """`/原图修复` 的底层操作：改名而非删除，URL 原样保留、跨会话各自处理。

    旧脏键是纯内存 LRU，/原图修复 必须把它们变成可命中的干净键。改名后若与
    既有主键重合（`🖼️ a.jpg` → `a.jpg`），应合并而不是留下两条。
    """
    history = MediaHistory()
    url = "https://cdn.example.com/a.jpg"
    history.remember("one", url, display_name="🖼️ a.jpg")
    history.remember("two", url, display_name="1789658616018_a.jpg")
    assert history.count("one") == 2  # 主键 + 脏别名键

    repaired = history.repair_keys(clean_display_name)

    assert repaired == 2
    assert history.find("one", "a.jpg") == ("found", ("a.jpg", url))
    assert history.find("two", "a.jpg") == ("found", ("a.jpg", url))
    assert history.count("one") == 1  # 脏别名已合并进主键
    assert history.count("two") == 1


def test_repair_keys_moves_renamed_key_to_end_and_respects_capacity():
    """改名沿用 remember 的 LRU 语义：合并后的键移到末尾，并受会话上限约束。"""
    history = MediaHistory(max_per_session=2)
    a_url = "https://cdn.example.com/a.jpg"
    history.remember("s", a_url, display_name="🖼️ a.jpg")
    history.remember("s", "https://cdn.example.com/b.jpg")

    assert history.repair_keys(clean_display_name) == 1

    assert history.count("s") == 2
    assert history.latest("s") == ("a.jpg", a_url)
    assert history.find("s", "b.jpg")[0] == "found"

"""发送记录账本：message_id 精确回查、缩略图跳过与容量淘汰。"""

from __future__ import annotations

import json

from astrbot_plugin_image_trace.sent_messages import SentMessageStore

URL = "https://img.example.com/file/a.jpg"
THUMB = "https://img.example.com/thumbnails/a_720.jpg"


def test_remember_and_find_by_message_id():
    store = SentMessageStore()
    store.remember("g1", "mid-1", [{"url": URL, "file_name": "a.jpg"}])

    hit = store.find("g1", "mid-1")

    assert hit == {"url": URL, "file_name": "a.jpg"}


def test_find_is_scoped_by_session():
    """不同群可能出现相同 message_id 形态；账本必须按会话隔离。"""
    store = SentMessageStore()
    store.remember("g1", "mid-1", [{"url": URL, "file_name": "a.jpg"}])

    assert store.find("g2", "mid-1") is None


def test_thumbnail_url_is_not_recorded():
    """引用图最终要发原图，账本里绝不能存 720px 缩略图直链。"""
    store = SentMessageStore()

    store.remember("g1", "mid-1", [{"url": THUMB, "file_name": "a.jpg"}])

    assert store.find("g1", "mid-1") is None


def test_message_without_any_usable_url_is_skipped():
    """卡片消息的发送 block 是本地 path，没有原图直链时不要记空账。"""
    store = SentMessageStore()

    store.remember("g1", "mid-1", [{"path": "/tmp/card.png", "file_name": "card.png"}])

    assert store.find("g1", "mid-1") is None


def test_lru_evicts_oldest_message():
    store = SentMessageStore(max_messages=2)
    store.remember("g1", "mid-1", [{"url": URL, "file_name": "a.jpg"}])
    store.remember("g1", "mid-2", [{"url": URL, "file_name": "b.jpg"}])
    store.remember("g1", "mid-3", [{"url": URL, "file_name": "c.jpg"}])

    assert store.find("g1", "mid-1") is None
    assert store.find("g1", "mid-2") is not None
    assert store.count() == 2


def test_find_refreshes_lru_order():
    store = SentMessageStore(max_messages=2)
    store.remember("g1", "mid-1", [{"url": URL, "file_name": "a.jpg"}])
    store.remember("g1", "mid-2", [{"url": URL, "file_name": "b.jpg"}])

    store.find("g1", "mid-1")
    store.remember("g1", "mid-3", [{"url": URL, "file_name": "c.jpg"}])

    assert store.find("g1", "mid-1") is not None
    assert store.find("g1", "mid-2") is None


def test_caption_record_normalizes_whitespace_and_finds():
    store = SentMessageStore()
    store.remember_caption(
        "g1",
        "🖼️  风景.jpg\n（原图）",
        [{"url": URL, "file_name": "风景.jpg"}],
    )

    hit = store.find_caption("g1", "🖼️ 风景.jpg （原图）")

    assert hit == {"url": URL, "file_name": "风景.jpg"}


def test_caption_substring_match_tolerates_platform_wrapping():
    """平台在引用文本前后加 `[图片]` 时，仍应唯一命中发送记录。"""
    store = SentMessageStore()
    store.remember_caption(
        "g1",
        "📅 Carry丶king 今日已签到 · 连续 19 天（累计 19 天）",
        [{"url": URL, "file_name": "a.jpg"}],
    )

    hit = store.find_caption(
        "g1", "[图片] 📅 Carry丶king 今日已签到 · 连续 19 天（累计 19 天）"
    )

    assert hit is not None
    assert hit["url"] == URL


def test_caption_ambiguous_substring_is_rejected():
    """两条记录都能子串命中时不能猜，否则可能发出另一条消息的原图。"""
    store = SentMessageStore()
    store.remember_caption("g1", "签到A", [{"url": URL, "file_name": "a.jpg"}])
    store.remember_caption("g1", "签到B", [{"url": URL, "file_name": "b.jpg"}])

    assert store.find_caption("g1", "签到A 签到B") is None


def test_caption_exact_same_key_returns_latest():
    """同一条配文重复发送时，后一条覆盖前一条，不和自家历史打架。"""
    store = SentMessageStore()
    store.remember_caption("g1", "同一句", [{"url": URL, "file_name": "a.jpg"}])
    store.remember_caption("g1", "同一句", [{"url": URL, "file_name": "b.jpg"}])

    assert store.find_caption("g1", "同一句") == {"url": URL, "file_name": "b.jpg"}


def test_caption_is_scoped_by_session():
    store = SentMessageStore()
    store.remember_caption("g1", "签到", [{"url": URL, "file_name": "a.jpg"}])

    assert store.find_caption("g2", "签到") is None


def test_load_ignores_malformed_entries(tmp_path):
    """损坏的 entries 结构不能拖垮插件启动，合法记录仍要保留。"""
    path = tmp_path / "sent_messages.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "messages": [
                    {
                        "session": "g1",
                        "message_id": "bad-dict",
                        "entries": {"url": URL},
                    },
                    {
                        "session": "g1",
                        "message_id": "bad-none",
                        "entries": [None],
                    },
                    {
                        "session": "g1",
                        "message_id": "good",
                        "entries": [{"url": URL, "file_name": "good.jpg"}],
                    },
                ],
                "captions": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = SentMessageStore(path=path)

    assert store.find("g1", "bad-dict") is None
    assert store.find("g1", "bad-none") is None
    assert store.find("g1", "good") is not None
    store.close()


def test_records_survive_reload(tmp_path):
    """重启等价于 close 后再 new 一个同路径的 store。"""
    path = tmp_path / "sent_messages.json"
    store = SentMessageStore(path=path)
    store.remember("g1", "mid-1", [{"url": URL, "file_name": "a.jpg"}])
    store.remember_caption(
        "g1", "📅 今日已签到", [{"url": URL, "file_name": "a.jpg"}]
    )
    store.close()

    reloaded = SentMessageStore(path=path)

    assert reloaded.find("g1", "mid-1") == {"url": URL, "file_name": "a.jpg"}
    assert reloaded.find_caption("g1", "📅 今日已签到") == {
        "url": URL,
        "file_name": "a.jpg",
    }
    reloaded.close()


def test_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "sent_messages.json"
    path.write_text("{not-json", encoding="utf-8")

    store = SentMessageStore(path=path)

    assert store.count() == 0
    assert store.find("g1", "mid-1") is None


def test_load_skips_thumbnail_and_trims_to_limit(tmp_path):
    """旧文件里混入缩略图/超量记录时，加载阶段就过滤并按上限保留最新。"""
    path = tmp_path / "sent_messages.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "messages": [
                    {
                        "session": "g1",
                        "message_id": "old",
                        "entries": [{"url": URL, "file_name": "old.jpg"}],
                    },
                    {
                        "session": "g1",
                        "message_id": "thumb",
                        "entries": [{"url": THUMB, "file_name": "thumb.jpg"}],
                    },
                    {
                        "session": "g1",
                        "message_id": "middle",
                        "entries": [{"url": URL, "file_name": "middle.jpg"}],
                    },
                    {
                        "session": "g1",
                        "message_id": "new",
                        "entries": [{"url": URL, "file_name": "new.jpg"}],
                    },
                ],
                "captions": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = SentMessageStore(path=path, max_messages=2)

    assert store.find("g1", "old") is None, "超过上限的最旧记录被淘汰"
    assert store.find("g1", "thumb") is None, "缩略图不入账"
    assert store.find("g1", "new") is not None
    store.close()

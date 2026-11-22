"""发送记录账本：message_id 精确回查、缩略图跳过与容量淘汰。"""

from __future__ import annotations

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

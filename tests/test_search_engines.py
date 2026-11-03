"""检索层单测：统一命中模型、两腿合并排序、向量阈值/去重、auto 回退策略。

这些用例直接打 search 包，不经过 main，锁住 Step 3 抽出的行为口径。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrbot_plugin_image_trace.search import (
    AutoSearchEngine,
    HashSearchEngine,
    QueryImage,
    SearchHit,
    SearchOutcome,
    VectorSearchEngine,
)


def _local(similarity: float, item_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=item_id,
        similarity=similarity,
        image_url=None,
        file_path=f"C:/img/{item_id}.jpg",
        file_name="",
        width=100,
        height=100,
        file_size=1024,
        created_at="2026-01-01",
        note=f"本地备注{item_id}",
    )


def _remote(similarity: float, item_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=item_id,
        similarity=similarity,
        image_url=f"https://example.com/{item_id}.jpg",
        file_name=f"{item_id}.jpg",
        width=100,
        height=100,
        file_size=1024,
        created_at="",
        note="",
    )


class _FakeLibrary:
    def __init__(self, matches):
        self._matches = matches

    def search(self, phash, want):
        return self._matches


class _FakeRemote:
    def __init__(self, matches):
        self._matches = matches
        self.is_empty = not matches

    def search(self, phash, want):
        return self._matches


def test_hash_engine_merges_legs_by_score_and_marks_sources() -> None:
    engine = HashSearchEngine(
        _FakeLibrary([_local(0.86, 1), _local(0.86, 2)]),
        _FakeRemote([_remote(0.95, 9)]),
        threshold=0.85,
        top_n=5,
        remote_enabled=lambda: True,
    )
    result = engine.merged_candidates("0" * 64, 5)

    assert [hit.id for hit in result.hits] == [9, 1, 2]
    assert [hit.source for hit in result.hits] == ["remote", "local", "local"]
    assert result.hits[0].image_url == "https://example.com/9.jpg"
    assert result.hits[1].file_path == "C:/img/1.jpg"
    assert result.hits[0].similarity == 0.95  # SearchHit 旧字段别名
    assert result.candidate_lines[0] == "· #9 相似度 95.0%（9.jpg）"


def test_vector_engine_filters_threshold_and_converts_hits() -> None:
    class _FakeVector:
        threshold = 0.80
        duplicate_threshold = 1.1  # 关掉向量近似合并，只验阈值/顺序
        top_k = 5

        async def embed_file(self, path, mime=None):
            return [1.0]

        async def search(self, vector, limit, with_vector):
            return [
                {
                    "id": "b",
                    "score": 0.7,
                    "payload": {"image_url": "https://example.com/b.jpg", "file_name": "b.jpg"},
                },
                {
                    "id": "a",
                    "score": 0.9,
                    "payload": {"image_url": "https://example.com/a.jpg", "file_name": "a.jpg"},
                },
            ]

    engine = VectorSearchEngine(_FakeVector(), top_n=5)
    outcome = asyncio.run(engine.search_outcome(QueryImage(path="q.jpg", mime="image/jpeg")))

    assert outcome.kind == "hit"
    assert [hit.id for hit in outcome.hits] == ["a"]
    assert outcome.hits[0].score == 0.9
    assert outcome.hits[0].url == "https://example.com/a.jpg"


def test_auto_engine_prefers_vector_and_skips_hash_on_hit() -> None:
    class _Vector:
        async def search_outcome(self, image):
            return SearchOutcome(
                kind="hit",
                hits=[SearchHit(id="a", score=0.9, url="https://example.com/a.jpg")],
            )

    class _Hash:
        def __init__(self):
            self.calls = 0

        def merged_candidates(self, phash, want):
            self.calls += 1
            return SimpleNamespace(hits=[], candidate_lines=[])

    hash_engine = _Hash()
    engine = AutoSearchEngine(
        _Vector(),
        hash_engine,
        hash_pool_size=lambda: 100,
        want=5,
    )
    result = asyncio.run(engine.search_outcome(QueryImage(path="q.jpg", phash="0" * 64)))

    assert result.vector.kind == "hit"
    assert result.hash is None
    assert hash_engine.calls == 0


def test_auto_engine_falls_back_to_hash_on_miss_or_empty_pool() -> None:
    class _Vector:
        async def search_outcome(self, image):
            return SearchOutcome(kind="miss", miss_text="no vector hit")

    class _Hash:
        def __init__(self):
            self.calls = 0

        def merged_candidates(self, phash, want):
            self.calls += 1
            return SimpleNamespace(hits=[], candidate_lines=["· #1 相似度 84.0%"])

    engine = AutoSearchEngine(
        _Vector(),
        _Hash(),
        hash_pool_size=lambda: 100,
        want=5,
    )
    result = asyncio.run(engine.search_outcome(QueryImage(path="q.jpg", phash="0" * 64)))
    assert result.hash is not None
    assert result.hash.candidate_lines == ["· #1 相似度 84.0%"]
    assert result.hash_empty is False

    hash_engine = _Hash()
    empty = AutoSearchEngine(
        _Vector(),
        hash_engine,
        hash_pool_size=lambda: 0,
        want=5,
    )
    result = asyncio.run(empty.search_outcome(QueryImage(path="q.jpg", phash="0" * 64)))
    assert result.hash is None
    assert result.hash_empty is True
    assert hash_engine.calls == 0

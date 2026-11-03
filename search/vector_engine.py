"""向量检索：把 VectorEngine 的原始结果收敛成统一命中模型。

阈值过滤、按相似度降序、同图去重都集中在 `search_outcome` 一处；`VectorEngine`
本身继续只负责「出网请求 + 返回 Qdrant 原始分数」，两者职责分开。
"""

from __future__ import annotations

from typing import Any

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("search.vector_engine")

try:
    from ..image_delivery import deduplicate_vector_hits
    from ..vector_search import VectorEngineError
except ImportError:  # 兼容插件以独立模块方式加载
    from image_delivery import deduplicate_vector_hits  # type: ignore[no-redef]
    from vector_search import VectorEngineError  # type: ignore[no-redef]

from .base import QueryImage, SearchHit, SearchOutcome, miss_message


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_hit(hit: dict) -> SearchHit:
    """Qdrant 原始命中 -> SearchHit；raw 保留原 dict 供去重日志/回传沿用旧口径。"""
    payload = hit.get("payload") or {}
    return SearchHit(
        id=str(hit.get("id")),
        score=float(hit.get("score") or 0.0),
        url=str(payload.get("image_url") or "") or None,
        file_name=str(payload.get("file_name") or ""),
        width=_as_int(payload.get("width")),
        height=_as_int(payload.get("height")),
        size_bytes=_as_int(payload.get("size_bytes")),
        source="vector",
        created_at=str(payload.get("created_at") or ""),
        raw=hit,
    )


class VectorSearchEngine:
    """Qdrant 向量的检索策略层（不负责出网，只做过滤/排序/去重）。"""

    def __init__(self, engine, *, top_n: int) -> None:
        self._engine = engine
        self._top_n = top_n

    async def search_outcome(self, image: QueryImage) -> SearchOutcome:
        try:
            if not image.path:
                return SearchOutcome(kind="nofile")
            vector = await self._engine.embed_file(image.path, mime=image.mime or None)
            hits = await self._engine.search(
                vector, limit=self._engine.top_k, with_vector=True
            )
            # 取回的全部达标命中（不止最高分那张），按相似度降序输出；
            # 数量上限即 top_k，需要更多就调大该配置
            good = sorted(
                (h for h in hits if h["score"] >= self._engine.threshold),
                key=lambda h: h["score"],
                reverse=True,
            )
            if good:
                good = deduplicate_vector_hits(good, self._engine.duplicate_threshold)
                return SearchOutcome(kind="hit", hits=[_to_hit(h) for h in good])
            candidate_lines = [
                f"· {h['payload'].get('file_name') or str(h['id']).rsplit('/', 1)[-1]}"
                f" 相似度 {h['score'] * 100:.1f}%"
                for h in hits[: self._top_n]
            ]
            return SearchOutcome(
                kind="miss",
                candidate_lines=candidate_lines,
                miss_text=miss_message(
                    f"❌ 向量库中未找到相似度达标的原图"
                    f"（阈值 {self._engine.threshold * 100:.0f}%）。",
                    candidate_lines,
                    empty_note="（向量库中暂无图床图片，等待图床上传侧钩子入库）",
                ),
            )
        except VectorEngineError as exc:
            logger.warning(f"向量检索出错: {getattr(exc, 'detail', exc)}")
            return SearchOutcome(kind="error", error=str(exc))
        except Exception:
            logger.error("向量检索异常", exc_info=True)
            return SearchOutcome(kind="error", error="内部错误（详见机器人日志）")

    async def search(self, image: QueryImage) -> list[SearchHit]:
        """SearchEngine 协议入口：只返回达阈值（且已去重）的命中。"""
        return (await self.search_outcome(image)).hits

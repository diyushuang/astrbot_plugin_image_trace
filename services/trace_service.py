"""R1 服务层：溯源检索流程：哈希/向量/auto 三条检索路径与命中回传。

服务通过构造注入的 host 门面访问 library / vector / storage / 图床等
依赖，禁止反向 import main；main 中的同名方法只做薄委托。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..config.typed import DEFAULT_BASIC
from ..features import compute_features, compute_features_with_mime
from ..media.image_source import ResolvedImage
from ..search import QueryImage, SearchHit


class TraceService:
    def __init__(self, *, host, bool_cfg) -> None:
        self.host = host
        # 配置门面注入：main 传入的读取器仍指向 main 薄委托，
        # 因此既有 monkeypatch(plugin._bool_cfg) 的用例继续生效。
        self.bool_cfg = bool_cfg

    async def _trace_hash(
        self, event: AstrMessageEvent, seg, resolved: ResolvedImage | None = None
    ) -> AsyncGenerator:
        """哈希检索；resolved 供 auto 引擎复用已解析的本地文件（含清理责任）。

        resolved 为 ResolvedImage（可能已带 features/MIME）；传了它表示调用方
        已解析过这张图，本方法不负责删除临时文件（由调用方统一清理）。
        """
        top_n = self.host.top_n
        owns_temp = resolved is None
        if resolved is None:
            resolved = await self.host._resolve_local(
                seg,
                probe=lambda path: compute_features_with_mime(path, self.host.hash_size),
            )
        local_path = resolved.path
        try:
            if not local_path:
                yield event.plain_result(resolved.message)
                return

            feats = resolved.features
            if feats is None:
                feats = await asyncio.to_thread(compute_features, local_path, self.host.hash_size)
            # 至少取回 5 条候选：即便 top_n 配置为 0/较小值，AI 复核也需要
            # 足够的候选池才能剔除误报
            hits, candidate_lines = await asyncio.to_thread(
                self.host._merged_hash_candidates, feats.phash, max(5, top_n)
            )
            async for result in self._hash_outcome_delivery(
                event, local_path, hits, candidate_lines
            ):
                yield result
        except Exception as e:
            logger.error(f"图片溯源处理失败: {e}", exc_info=True)
            yield event.plain_result("⚠️ 处理图片时出错，详情请查看机器人日志。")
        finally:
            if owns_temp and resolved is not None and resolved.is_tmp:
                self.host._remove_quiet(resolved.path)

    async def _hash_outcome_delivery(
        self, event: AstrMessageEvent, query_path: str, hits: list, candidate_lines: list
    ) -> AsyncGenerator:
        """哈希候选的统一渲染：AI 复核（仅哈希腿）→ 命中回传或未命中提示。"""
        if hits and self.bool_cfg("ai_verify", DEFAULT_BASIC.ai_verify):
            hits = await self.host._ai_verify(query_path, hits)

        if hits:
            async for result in self._hash_hit_result(event, hits[0]):
                yield result
            return

        yield event.plain_result(
            self.host._miss_message(
                f"❌ 图库中未找到相似度达标的原图（阈值 {self.host.hash_threshold * 100:.0f}%）。",
                candidate_lines,
            )
        )

    async def _hash_hit_result(self, event: AstrMessageEvent, best) -> AsyncGenerator:
        """把一条哈希命中渲染成回复（本地腿与图床腿共用）。

        两腿的差异只有两处：展示名（本地用备注、图床用文件名）与路径来源
        （本地文件 vs 远端直链）。其余展示与回传逻辑完全一致，故合并到一处，
        免得日后改一处漏一处。
        """
        title = f"✅ 溯源命中（相似度 {best.similarity * 100:.1f}%）#{best.id}"
        label = self.host._candidate_note(best)
        if label:
            title += f" · {label}"
        lines = []
        if best.width and best.height:
            lines.append(f"尺寸：{best.width}x{best.height}")
        if best.created_at:
            lines.append(f"入库时间：{best.created_at}")
        block = {"text": " · ".join(lines)}
        if best.width and best.height:
            # 供回传计划判断是否需要图床缩放，以及压缩标识是否成立
            block["width"] = best.width
            block["height"] = best.height
        if best.file_size and best.file_size > 0:
            # 入库时记录的原图体积：回传计划据此免探测原图体积
            block["size_bytes"] = int(best.file_size)
        if best.image_url:
            block["url"] = best.image_url
        elif best.file_path and os.path.isfile(best.file_path):
            block["path"] = best.file_path
        if "url" in block or "path" in block:
            async for result in self.host._yield_delivery(event, title, [block]):
                yield result
        else:
            # 本地路径/图床直链都可能暴露服务器目录或内网地址，只进日志
            logger.warning(
                f"哈希命中 #{best.id} 的原图不可用: "
                f"{best.file_path or best.image_url or '未知路径'}"
            )
            yield event.plain_result(
                f"{title}，但原图文件已不存在或无法访问（详情见机器人日志）。"
            )

    async def _vector_search_one(self, event, seg, resolved: ResolvedImage | None = None):
        """单图向量检索。返回 (kind, item)：hit / miss / nofile / error。

        hit/miss/nofile 时 item 为可直接 yield 的结果对象；error 时 item 为错误说明字符串。

        resolved 供 auto 引擎复用已解析的本地文件：传了它（ResolvedImage）就不再
        重复下载/解码，且临时文件由调用方统一清理；其中的 MIME 会直接交给
        embed_file，省掉为同一张图再取一次格式的解码。
        """
        owns_temp = resolved is None
        if resolved is None:
            resolved = await self.host._resolve_local(seg, probe=self.host._probe_mime)
        try:
            if not resolved.path:
                return "nofile", event.plain_result(resolved.message)
            outcome = await self.host._vector_engine().search_outcome(
                QueryImage(path=resolved.path, mime=resolved.mime)
            )
            if outcome.kind == "hit":
                return "hit", self._vector_hits_delivery(event, outcome.hits)
            if outcome.kind == "miss":
                return "miss", event.plain_result(outcome.miss_text)
            return "error", outcome.error
        finally:
            if owns_temp and resolved.is_tmp:
                self.host._remove_quiet(resolved.path)

    async def _trace_vector(self, event: AstrMessageEvent, seg) -> AsyncGenerator:
        """严格向量引擎：只用向量库，出错直接提示。"""
        kind, item = await self.host._vector_search_one(event, seg)
        if kind == "error":
            yield event.plain_result(f"⚠️ 向量检索引擎错误：{item}")
        elif kind == "hit":
            async for result in item:
                yield result
        else:
            yield item

    async def _trace_auto(self, event: AstrMessageEvent, seg) -> AsyncGenerator:
        """auto 引擎：向量优先，未命中或引擎不可用时回退哈希。

        图片只解析一次：解析时用 compute_features_with_mime 同时完成格式校验与
        感知特征，向量腿拿 MIME、哈希腿拿 features，两边都不再为同一张图重复
        解码。临时文件由这里的 finally 统一清理（下游收到 resolved 时不再删除）。
        """
        resolved = await self.host._resolve_local(
            seg,
            probe=lambda path: compute_features_with_mime(path, self.host.hash_size),
        )
        if not resolved.path:
            yield event.plain_result(resolved.message)
            return
        try:
            feats = resolved.features
            if feats is None:
                feats = await asyncio.to_thread(
                    compute_features, resolved.path, self.host.hash_size
                )
            outcome = await self.host._auto_engine().search_outcome(
                QueryImage(path=resolved.path, mime=resolved.mime, phash=feats.phash)
            )
            vector = outcome.vector
            if vector.kind == "hit":
                async for result in self._vector_hits_delivery(event, vector.hits):
                    yield result
                return
            if vector.kind == "nofile":
                yield event.plain_result(resolved.message)
                return
            if vector.kind == "miss":
                yield event.plain_result(vector.miss_text)
                if outcome.hash is None:
                    yield event.plain_result("（哈希图库为空，无兜底可执行）")
                    return
                yield event.plain_result("尝试哈希比对：")
            else:  # error
                logger.info(f"auto 引擎回退哈希：{vector.error}")
                if outcome.hash is None:
                    yield event.plain_result(
                        "⚠️ 向量引擎不可用，且哈希图库为空，无兜底可执行。"
                        "可发送 /登记原图 或执行 /溯源重扫 建库。"
                    )
                    return
                yield event.plain_result("⚠️ 向量引擎不可用，改用哈希比对：")
            async for item in self._hash_outcome_delivery(
                event,
                resolved.path,
                outcome.hash.hits,
                outcome.hash.candidate_lines,
            ):
                yield item
        finally:
            if resolved.is_tmp:
                self.host._remove_quiet(resolved.path)

    async def _vector_hits_delivery(
        self, event: AstrMessageEvent, hits: list
    ) -> AsyncGenerator:
        """构造向量命中的去重回传请求。

        先发一条纯文本命中提示，再把全部相似图合并进单独一条消息：第二条走
        _yield_delivery 的统一入口（一条消息含多图），逐图配文由 mark_compressed
        按各回传路径的实际压缩情形写入，因此不再重复命中提示标题。
        """
        # SearchHit 保留原始 Qdrant dict 于 raw；这里复原成旧结构，投递语义不变。
        hits = [
            hit.raw if isinstance(hit, SearchHit) and isinstance(hit.raw, dict) else hit
            for hit in hits
        ]
        total_hits = sum(int(hit.get("duplicate_count") or 1) for hit in hits)
        duplicate_count = total_hits - len(hits)
        multi = len(hits) > 1
        if duplicate_count:
            logger.info(
                "向量命中去重：原始 %d 张，合并重复 %d 张，回传 %d 张",
                total_hits,
                duplicate_count,
                len(hits),
            )
        for hit in hits:
            duplicates = hit.get("duplicates") or []
            if not duplicates:
                continue
            payload = hit.get("payload") or {}
            display_name = payload.get("file_name") or str(hit.get("id"))
            duplicate_ids = "、".join(str(item.get("id")) for item in duplicates)
            logger.info(
                "向量命中去重：保留 %s（%s），合并重复 %d 张（%s）",
                display_name,
                hit.get("id"),
                len(duplicates),
                duplicate_ids,
            )

        if multi:
            header = f"✅ 向量检索命中 {len(hits)} 张（按相似度降序），图片发送可能有延迟"
        else:
            header = (
                f"✅ 向量检索命中（相似度 {hits[0]['score'] * 100:.1f}%），"
                "图片发送可能有延迟"
            )

        blocks = []
        for idx, hit in enumerate(hits, 1):
            payload = hit.get("payload") or {}
            file_name = payload.get("file_name") or str(hit.get("id")).rsplit("/", 1)[-1]
            fields = []
            if multi:
                fields.append(f"相似度 {hit['score'] * 100:.1f}%")
            width, height = payload.get("width"), payload.get("height")
            if width and height:
                fields.append(f"{width}x{height}")
            if payload.get("created_at"):
                fields.append(str(payload["created_at"]))
            prefix = f"{idx}." if multi else ""
            block = {
                "prefix": prefix,
                "file_name": str(file_name or ""),
                "fields": fields,
            }
            if width and height:
                # 供回传计划判断是否需要图床缩放，以及压缩标识是否成立
                block["width"] = width
                block["height"] = height
            size_bytes = self.host._known_bytes(payload)
            if size_bytes:
                # 图床侧钩子（img-indexer）入库时已记录原图体积，直接用：
                # 回传计划因此不必再探测原图尺寸，省一次往返与一次图床处理
                block["size_bytes"] = size_bytes
            image_url = str(payload.get("image_url") or "")
            if image_url:
                block["url"] = image_url
            else:
                block["fields"].append("该条目没有可用直链，可能已被删除")
            blocks.append(block)
        prompt_via_onebot = False
        prompt_task = None
        if self.host._can_send_via_onebot(event):
            # 提示与图片并行发出：提示只走一次 call_action，串行 await 会让图片
            # 晚一个完整往返才起步。两条消息都在同一条 OneBot 连接上按顺序下发，
            # 提示先入队，正常情况下仍先到达。
            prompt_task = asyncio.create_task(self.host._send_prompt(event, header))
        else:
            yield event.plain_result(header)
        try:
            async for result in self.host._yield_delivery(event, "", blocks, mark_compressed=True):
                yield result
            if prompt_task is not None:
                # 只为 stop_event 决策等待提示结果；图片可能已经交付，不再回滚。
                prompt_via_onebot = await prompt_task
                prompt_task = None
                if not prompt_via_onebot:
                    # 协议端明确拒绝提示（FAILED）：图片可能已发，但标题不能丢，
                    # 改走标准消息链补发一次纯文本
                    yield event.plain_result(header)
        finally:
            # 投递层异常或消费者提前退出时回收提示任务，避免留下悬挂任务。
            if prompt_task is not None:
                prompt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await prompt_task
        # 提示用 OneBot 原生发出（图片那条未必）时，仍按旧语义停事件；若图片
        # 那条也走了 OneBot，_yield_delivery 已停过，这里重复调用无副作用。
        # 多图溯源期间由 trace 统一延迟 stop，否则管道会在首图后中断生成器。
        # 放在 yield 之后：第二条消息已经交付，停事件不会把它吞掉。
        if prompt_via_onebot and not self.host._defer_event_stop(event):
            event.stop_event()

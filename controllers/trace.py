"""溯源 / 状态 / 帮助指令控制器。"""

from __future__ import annotations

try:  # 包加载
    from ..config.loader import bool_cfg, engine_choice, scan_dirs
    from ..config.typed import DEFAULT_BASIC
except ImportError:  # 兼容插件以独立模块方式加载
    from config.loader import bool_cfg, engine_choice, scan_dirs
    from config.typed import DEFAULT_BASIC

from astrbot.api import logger


class TraceController:
    """承载 `溯源`、`溯源状态`、`溯源帮助` 三条指令的处理流程。"""

    def __init__(self, plugin, config) -> None:
        self.plugin = plugin
        self.config = config

    async def trace(self, event):
        """发送或引用一张图片，按当前引擎（hash/vector/auto）找出相似的原图并回传"""
        plugin = self.plugin
        plugin._maybe_start_remote_hash_refresh()  # 缓存过期才兜底刷新（幂等）
        images = plugin._extract_images(event)
        if not images:
            yield event.plain_result(plugin._usage())
            return
        # 多图查询期间不能逐图 stop_event：AstrBot 调度器一旦看到 stopped 就会
        # 中断当前生成器，后面的图片不会被处理。整轮结束后再统一 stop。
        plugin._defer_event_stop(event, True)
        try:
            engine = engine_choice(self.config)
            budget = plugin.max_images_per_query
            if engine == "vector":
                if not plugin.vector.enabled:
                    yield event.plain_result(plugin.vector.config_hint())
                    return
                for seg in images[:budget]:
                    plugin._reset_event_sent(event)
                    async for item in plugin._trace_vector(event, seg):
                        yield item
                return
            if engine == "auto" and plugin.vector.enabled:
                for seg in images[:budget]:
                    plugin._reset_event_sent(event)
                    async for item in plugin._trace_auto(event, seg):
                        yield item
                return
            # hash（或 auto 但向量引擎未配置）
            if plugin._hash_pool_size() == 0:
                empty_note = (
                    "（向量引擎未启用：若图床侧已部署向量库，请检查 vector_search 配置）"
                    if not plugin.vector.enabled
                    else ""
                )
                yield event.plain_result(
                    "图库还是空的：可发送 /登记原图 登记原图，"
                    "或在插件配置 scan_dirs 填写图床目录后执行 /溯源重扫。" + empty_note
                )
                return
            for seg in images[:budget]:
                plugin._reset_event_sent(event)
                async for item in plugin._trace_hash(event, seg):
                    yield item
        finally:
            plugin._defer_event_stop(event, False)
            event.stop_event()

    async def status(self, event):
        """查看图库统计与插件配置摘要"""
        plugin = self.plugin
        stats = plugin.library.stats()
        scan_dirs_value = scan_dirs(self.config)
        lines = [f"📚 图片溯源图库状态：共 {stats['total']} 条（索引 {stats['indexed']}）"]
        if stats["skipped"]:
            lines.append(
                f"⚠️ 有 {stats['skipped']} 条因哈希精度不匹配未参与检索，"
                "建议执行 /溯源重扫 force 重建。"
            )
        if stats["by_source"]:
            lines.append(
                "· 来源：" + "，".join(f"{k or '未知'} {v}" for k, v in stats["by_source"].items())
            )
        lines.append(
            f"· pHash 精度：{plugin.hash_size * plugin.hash_size}bit（hash_size={plugin.hash_size}）"
        )
        lines.append(f"· 相似度阈值：{plugin.hash_threshold * 100:.0f}%")
        lines.append(plugin._storage_line())
        engine = engine_choice(self.config)
        lines.append(f"· 检索引擎：{engine}（向量{'已启用' if plugin.vector.enabled else '未启用'}）")
        if plugin.vector.enabled:
            try:
                vec_count = await plugin.vector.count()
                count_text = f"{vec_count} 条"
            except Exception as e:
                # 原始错误可能包含内网地址/Key，只进日志，不进群聊
                logger.warning(f"查询向量库点数失败: {e}")
                count_text = "不可用（连接或鉴权异常，详情见机器人日志）"
            lines.append(f"· 向量库：{plugin.vector.collection}，{count_text}")
            lines.append(f"· Embed：{plugin.vector.embed_model} / {plugin.vector.image_input}")
        lines.append(f"· 扫描目录：{len(scan_dirs_value)} 个")
        lines.append(plugin._remote_hash_line())
        lines.append(await plugin._weibo_status_line())
        lines.append(
            f"· AI 复核：{'开' if bool_cfg(self.config, 'ai_verify', DEFAULT_BASIC.ai_verify) else '关'}"
        )
        yield event.plain_result("\n".join(lines))

    async def trace_help(self, event):
        """图片溯源插件使用帮助"""
        yield event.plain_result(self.plugin._usage())

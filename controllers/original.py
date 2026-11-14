"""原图 / 原图修复指令控制器。"""

from __future__ import annotations

from astrbot.api import logger

from ..image_delivery import build_original_url
from ..random_media import clean_display_name, is_thumbnail_url, strip_leading_decoration


class OriginalController:
    """承载 `原图`、`原图修复` 两条指令。"""

    def __init__(self, plugin, config) -> None:
        self.plugin = plugin
        self.config = config

    async def original_repair(self, event):
        """清理会话历史里带配文前缀/装饰的脏键（1.6.1 前的旧数据）。

        为什么需要这个命令：1.6.1 之前 `_remember_blocks` 会把配文前缀
        （`🖼️ `、`🎬 `、多图的 `1. `）连同文件名一起登记进历史。于是

            引用那条带前缀的消息发 /原图 → 历史/引用图里取到的名字自带前缀
            → 拼成 /file/🖼️%20【…】 → 必然 404
            → 提示文案里再复述一遍带前缀的名字，用户完全看不出问题在哪儿

        清洗逻辑现在已修（见 random_media.clean_display_name），**新写入的键是
        干净的**；但旧键是纯内存 LRU，不会自己消失——只在进程重启或该会话累积
        到上限被淘汰时才可能挤掉。故给一个显式入口。

        只管历史，**不动图床**：前缀从来不是图床里的真实文件，脏的只是本地键。
        """
        cleaned = self.plugin._repair_history_keys()
        if not cleaned:
            yield event.plain_result(
                "✅ 会话历史里没有带前缀的脏记录，无需修复。\n"
                "（前台提示文案本插件从不写入文件，无需清理）"
            )
            return
        logger.info(f"/原图修复 清理了 {cleaned} 条带前缀的会话历史键")
        yield event.plain_result(
            f"✅ 已清理 {cleaned} 条带前缀的会话历史记录。\n"
            "现在可以重新「引用那张图 + /原图」取原图了；"
            "若仍取不到，再引用一次 /随机图 刚发出的图重试。"
        )

    async def original_image(self, event):
        """重发最近回传过的图片的原图；也可直接按文件名到图床取（/原图 风景.jpg）"""
        plugin = self.plugin
        session = plugin._session_key(event)
        query = plugin._strip_command(event.message_str or "", ("原图",)).strip(" \t:：,，")
        name = ""
        url = ""
        # 引用图里的**原始**直链/名字：保留 `thumbnails/` 路径段，供缩略图判定与
        # 反查使用（反查要拼出与 Qdrant payload 同形的完整缩略图直链）
        quoted = ""
        # 取值来源，只用于日志：历史命中 / 按名直查 / 缩略图反查。
        # 线上排查时「/原图 到底走了哪条路」是最关键的一条信息，而历史命中
        # 这条最常见、却原本一行日志都不打，导致日志里完全看不到命令执行过。
        source = "history"
        if query:
            status, payload = plugin.history.find(session, query)
            if status == "ambiguous":
                candidates = "、".join(payload[:5])
                yield event.plain_result(f"匹配到多张图片，请写出更完整的文件名：{candidates}")
                return
            if status == "found":
                name, url = payload
            else:
                # 历史没命中时 query 会退化成**直查图床的文件名/相对路径**，此时它
                # 可能自带装饰前缀——最常见的是用户从配文里复制粘贴（配文是
                # `🖼️ 名字`，复制过来就带了 `🖼️ `）。不清洗就会拼出
                # `/file/🖼️%20名字` 而必然 404。命中历史的分支无需清洗：
                # 历史键本就是干净文件名（1.6.1 起写入时即已清洗）。
                #
                # 这里用 strip_leading_decoration 而**不是** clean_display_name：
                # 后者靠 media_filename 取末段，会把用户手写的多级相对路径
                # （`7、综艺节目/…/海报.jpg`）削成裸文件名——图床按裸名取不到，
                # 于是「用户给足了信息反而查不到」。前者原地剥前缀、保住目录。
                query = strip_leading_decoration(query) or query
        else:
            latest = plugin.history.latest(session)
            if latest is not None:
                name, url = latest
            else:
                # 会话里没有回传记录：退化为「按消息/引用图里的文件名直查图床」
                quoted = plugin._first_image_candidate(event)
                query = clean_display_name(quoted) or ""

        if not url:
            if not query:
                yield event.plain_result(plugin._original_usage())
                return
            source = "lookup"
            name, url, message = await plugin._lookup_imgbed_file(query, quoted=quoted)
            if not url:
                yield event.plain_result(message)
                return
        elif is_thumbnail_url(url):
            # 兜底：历史里若因任何原因（旧版本写入、外部注入）存进了缩略图直链，
            # 也要在发出前反查回原图。正常路径下历史记的是原图（见 _yield_delivery
            # 的 remember_blocks），这里是防御，不依赖上游一定正确。
            original_name, original_url = await plugin._original_from_thumbnail(
                plugin._bed_file_id(url) or name
            )
            if original_url:
                logger.info(f"/原图 历史命中缩略图，已反查回原图: {url} -> {original_url}")
                name, url = original_name, original_url
                source = "history-thumb-reverse"
            else:
                yield event.plain_result(
                    f"本会话记录的「{name}」是缩略图，且查不到它对应的原图；"
                    "可先用 /溯源 命中该图后再发 /原图。"
                )
                return
        # R6：/原图 只发 URL、不落地字节——剥离 ImgBed 处理参数还原未处理原文件，
        # 并强制 url 通道 + 禁用本地回退
        final_url = build_original_url(url)
        # 最后一次校验：走到这里说明上游每一条路径都「自认为」拿到的是原图，
        # 但只要最终直链仍指向缩略图目录，就绝不能发出去——静默发出一张 720px
        # 小图，用户只会觉得「/原图 又没生效」，比明确报错难排查得多。
        if is_thumbnail_url(final_url):
            logger.warning(f"/原图 最终直链仍是缩略图，已拒绝发送: {final_url}")
            yield event.plain_result(
                f"取到的「{name}」仍是图床里的缩略图直链，已中止发送；"
                "请改用原图文件名，或先 /溯源 命中该图后再发 /原图。"
            )
            return
        logger.info(f"/原图 发出（来源={source}）: {name} <- {final_url}")
        block = {"url": final_url, "file_name": name}
        async for result in plugin._yield_delivery(
            event,
            f"🖼️ {name}（原图）",
            [block],
            mode_override="url",
            allow_local_fallback=False,
        ):
            yield result

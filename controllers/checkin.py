"""签到 / 签到文案导入指令控制器。"""

from __future__ import annotations

import asyncio
import os
import secrets

try:  # 包加载
    from ..common import read_bytes, sanitize_error_text
    from ..weibo_posts import load_post_map, map_is_stale, parse_weibo_text
except ImportError:  # 兼容插件以独立模块方式加载
    from common import read_bytes, sanitize_error_text
    from weibo_posts import load_post_map, map_is_stale, parse_weibo_text

from astrbot.api import logger


class CheckinController:
    """承载 `签到`、`签到文案导入` 两条指令。"""

    def __init__(self, plugin, config) -> None:
        self.plugin = plugin
        self.config = config

    async def checkin(self, event):
        """每日签到，领取一张带边框与随机语句的随机缩略图"""
        async for result in self.plugin._checkin_flow(event):
            yield result

    async def checkin_import(self, event):
        """管理员：导入微博文案 CSV/MD 到插件数据目录。"""
        plugin = self.plugin
        if not event.is_admin():
            yield event.plain_result("仅管理员可导入签到文案。")
            return
        segment = plugin._first_file_segment(event)
        if segment is None:
            yield event.plain_result(
                "请把 CSV 或 MD 文件作为附件发送，并在同一条消息中发送 /签到文案导入。"
            )
            return
        name = os.path.basename(str(getattr(segment, "name", "") or "")).strip()
        try:
            getter = getattr(segment, "get_file", None)
            if callable(getter):
                try:
                    target = await getter(allow_return_url=True)
                except TypeError:  # 旧版本 get_file 可能没有该关键字
                    target = await getter()
            else:
                target = str(getattr(segment, "file", "") or "")
            if not target:
                yield event.plain_result("无法读取文件内容，请重新发送附件。")
                return
            if str(target).lower().startswith(("http://", "https://")):
                data = await plugin._download_bytes(str(target))
            else:
                data = await asyncio.to_thread(read_bytes, str(target))
        except Exception as exc:
            logger.warning(f"签到文案导入读取失败: {exc}")
            yield event.plain_result(f"读取文件失败：{sanitize_error_text(exc)}")
            return
        if len(data) > 4 * 1024 * 1024:
            yield event.plain_result("文件超过 4MB，请拆分后再导入。")
            return
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                yield event.plain_result("文件不是 UTF-8 编码，无法解析。")
                return
        extension = os.path.splitext(name)[1].lower()
        if extension not in {".csv", ".md", ".markdown"}:
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
            if "序号" in first_line and "发布时间" in first_line:
                extension = ".csv"
            elif first_line.startswith("# 微博原创文案"):
                extension = ".md"
            else:
                yield event.plain_result(
                    "无法识别文件格式：只支持 CSV（序号,发布时间,来源,文案,微博链接）"
                    "或微博原创文案 MD 导出。"
                )
                return
        base = os.path.basename(name) or f"weibo_import_{secrets.token_hex(4)}{extension}"
        base = base.replace("/", "_").replace("\\", "_").strip()
        if not base or base in {".", ".."} or not base.lower().endswith(
            (".csv", ".md", ".markdown")
        ):
            base = f"weibo_import_{secrets.token_hex(4)}{extension}"
        parsed = parse_weibo_text(text, base)
        if not parsed:
            yield event.plain_result("未解析到任何微博文案，请检查文件内容。")
            return
        try:
            await asyncio.to_thread(os.makedirs, plugin.weibo_dir, exist_ok=True)
            final_path = os.path.join(plugin.weibo_dir, base)
            temp_path = f"{final_path}.tmp"

            def write_text() -> None:
                with open(temp_path, "w", encoding="utf-8") as handle:
                    handle.write(text)
                os.replace(temp_path, final_path)

            await asyncio.to_thread(write_text)
        except OSError as exc:
            logger.warning(f"签到文案导入写入失败: {exc}")
            yield event.plain_result(f"写入失败：{sanitize_error_text(exc)}")
            return
        plugin._weibo_posts_cache = None
        plugin._weibo_meta_cache = {}
        plugin._weibo_signature = None
        accounts = sorted({post.account for post in parsed})
        dates = sorted({post.date for post in parsed})
        account_text = "、".join(accounts[:5]) + ("…" if len(accounts) > 5 else "")
        date_text = f"{dates[0][:4]}-{dates[0][4:6]}-{dates[0][6:]}" if dates else "-"
        if dates and dates[-1] != dates[0]:
            date_text += f" ~ {dates[-1][:4]}-{dates[-1][4:6]}-{dates[-1][6:]}"
        map_path = plugin._weibo_map_path()
        map_note = "映射表未生成：签到时会按同账号同日图片回退，建议运行 scripts/build_weibo_map.py。"
        if os.path.isfile(map_path):
            map_posts, _generated, map_meta = await asyncio.to_thread(load_post_map, map_path)
            if map_is_stale(map_path, plugin.weibo_dir):
                map_note = "源文件已更新，映射表比源文件旧；请重新运行 scripts/build_weibo_map.py。"
            else:
                map_note = (
                    f"映射表：{len(map_posts)} 条，可配对 "
                    f"{int(map_meta.get('matched_count') or 0)} 条。"
                )
        yield event.plain_result(
            f"✅ 已导入 {base}：{len(parsed)} 条文案，账号 {account_text}，"
            f"日期 {date_text}。\n{map_note}"
        )

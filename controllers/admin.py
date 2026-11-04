"""管理员指令控制器：重扫 / 删除。"""

from __future__ import annotations

import asyncio
import os

try:  # 包加载
    from ..common import RESCAN_BATCH_SIZE, RESCAN_PROGRESS_EVERY, SCAN_CONCURRENCY
    from ..config.loader import scan_dirs
    from ..features import compute_features
except ImportError:  # 兼容插件以独立模块方式加载
    from common import RESCAN_BATCH_SIZE, RESCAN_PROGRESS_EVERY, SCAN_CONCURRENCY
    from config.loader import scan_dirs
    from features import compute_features

from astrbot.api import logger


class AdminController:
    """承载 `溯源重扫`、`溯源删除` 两条管理员指令。"""

    def __init__(self, plugin, config) -> None:
        self.plugin = plugin
        self.config = config

    async def rescan(self, event):
        """扫描配置的本地图库目录，建立/更新索引（管理员）"""
        plugin = self.plugin
        try:
            force = "force" in (event.message_str or "").lower()
            valid_dirs = [os.path.abspath(d) for d in scan_dirs(self.config) if os.path.isdir(d)]
            invalid = len(scan_dirs(self.config)) - len(valid_dirs)
            if not valid_dirs:
                yield event.plain_result(
                    "未配置有效的扫描目录：请在插件配置 scan_dirs 中填写"
                    "图片库/图床存储目录的绝对路径。"
                )
                return

            # 目录遍历可能耗时很久，放到线程里避免阻塞事件循环
            files, scanned_dirs = await asyncio.to_thread(
                plugin._collect_image_files, valid_dirs
            )
            if force:
                await asyncio.to_thread(plugin.library.delete_by_source, "scan")
            known_paths = {
                os.path.normcase(os.path.abspath(p))
                for p in plugin.library.get_paths_for_source("scan")
            }
            # 一次性取回全部 pHash，在内存里去重，避免逐张查库；
            # 即便跨任务并发重扫漏判，phash 唯一索引 + INSERT OR IGNORE 也能兜底
            known_phashes = plugin.library.get_all_phash_hashes()
            pending: list = []
            added = skipped = failed = 0
            total = len(files)
            await event.send(
                event.plain_result(f"开始扫描 {len(valid_dirs)} 个目录，共 {total} 张图片…")
            )
            concurrency = max(1, SCAN_CONCURRENCY)
            processed = 0
            next_report = RESCAN_PROGRESS_EVERY
            for start in range(0, total, concurrency):
                batch_paths = files[start : start + concurrency]
                compute_paths = []
                for path in batch_paths:
                    if os.path.normcase(path) in known_paths:
                        skipped += 1
                    else:
                        compute_paths.append(path)
                if compute_paths:
                    results = await asyncio.gather(
                        *(
                            asyncio.to_thread(compute_features, path, plugin.hash_size)
                            for path in compute_paths
                        ),
                        return_exceptions=True,
                    )
                else:
                    results = []
                for path, result in zip(compute_paths, results, strict=True):
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    if isinstance(result, BaseException):
                        failed += 1
                        logger.warning(f"扫描图片失败 {path}: {result}")
                        continue
                    feats = result
                    if feats.phash in known_phashes:
                        skipped += 1
                        continue
                    try:
                        size_bytes = os.path.getsize(path)
                    except OSError as e:
                        failed += 1
                        logger.warning(f"扫描图片失败 {path}: {e}")
                        continue
                    known_phashes.add(feats.phash)
                    pending.append(
                        {
                            "phash": feats.phash,
                            "dhash": feats.dhash,
                            "ahash": feats.ahash,
                            "width": feats.width,
                            "height": feats.height,
                            "file_size": size_bytes,
                            "file_path": path,
                            "source": "scan",
                            "note": os.path.basename(path),
                        }
                    )
                    # 批量写入（单事务），避免逐条提交拖慢扫描
                    if len(pending) >= RESCAN_BATCH_SIZE:
                        batch = list(pending)
                        pending.clear()
                        added += await asyncio.to_thread(
                            plugin.library.add_many, batch, reload_cache=False
                        )
                processed += len(batch_paths)
                if processed >= next_report:
                    await event.send(
                        event.plain_result(
                            f"进度：{processed}/{total}，新增 {added + len(pending)}，"
                            f"跳过 {skipped}，失败 {failed}"
                        )
                    )
                    next_report = processed + RESCAN_PROGRESS_EVERY
            if pending:
                batch = list(pending)
                pending.clear()
                added += await asyncio.to_thread(plugin.library.add_many, batch, reload_cache=False)
            if added:
                await asyncio.to_thread(plugin.library.reload_cache)
            # 只清理"本次成功遍历的目录"下的失效条目，目录临时掉线不清索引
            removed = await asyncio.to_thread(
                plugin.library.prune_scan_missing,
                set(files),
                set(scanned_dirs),
            )
            result = f"✅ 重扫完成：共 {total} 张，新增 {added}，跳过 {skipped}，失败 {failed}"
            if removed:
                result += f"，清理失效条目 {removed}"
            failed_dirs = len(valid_dirs) - len(scanned_dirs)
            if failed_dirs:
                result += f"（注意：{failed_dirs} 个目录扫描失败已跳过）"
            elif invalid > 0:
                result += f"（注意：{invalid} 个配置目录无效已忽略）"
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"重扫失败: {e}", exc_info=True)
            yield event.plain_result("⚠️ 重扫失败，详情请查看机器人日志。")

    async def delete_entry(self, event, entry_id: int):
        """按编号删除图库条目（管理员）"""
        plugin = self.plugin
        try:
            row = plugin.library.get(entry_id)
            if row is None:
                yield event.plain_result(f"未找到编号 #{entry_id} 的条目。")
                return
            await asyncio.to_thread(plugin.library.delete, entry_id)
        except Exception as e:
            logger.error(f"删除条目 #{entry_id} 失败: {e}", exc_info=True)
            yield event.plain_result("⚠️ 删除失败，详情请查看机器人日志。")
            return
        notes: list = []
        # best-effort 清理插件侧写入的向量点（同图同 ID，按内容指纹派生）；
        # 图床侧钩子写入的点使用图床自身的 key 约定，其清理需在图床侧完成
        if plugin.vector.enabled and row["phash"]:
            try:
                await plugin.vector.delete_point(f"phash:{row['phash']}")
                notes.append("向量点已清除")
            except Exception as e:
                logger.warning(f"删除向量点失败 #{entry_id}: {e}")
                notes.append("向量点清除失败（详情见日志）")
        if row["image_url"]:
            try:
                if await plugin.storage.active.delete(row["image_url"]):
                    notes.append("远端对象已删除")
            except Exception as e:
                logger.warning(f"删除远端对象失败 #{entry_id}: {e}")
        plugin._remove_local_copy(row)
        # 展示只用备注：file_path / image_url 可能含服务器目录或内网地址，
        # 管理员命令的回执同样会进群聊，细节只写日志
        desc = row["note"] or "无备注"
        logger.info(
            f"/溯源删除 #{entry_id}: note={row['note'] or ''} "
            f"path={row['file_path'] or ''} url={row['image_url'] or ''}"
        )
        suffix = ("，" + "、".join(notes)) if notes else ""
        yield event.plain_result(f"✅ 已删除 #{entry_id}（{desc}）{suffix}")

"""登记原图指令控制器。"""

from __future__ import annotations

import asyncio
import os

from astrbot.api import logger

from ..config.loader import bool_cfg, vector_index_on_register
from ..config.typed import DEFAULT_BASIC
from ..features import compute_features, compute_features_with_mime
from ..image_bed import MODE_CFB


class RegisterController:
    """承载 `登记原图` 指令的处理流程。"""

    def __init__(self, plugin, config) -> None:
        self.plugin = plugin
        self.config = config

    async def register_image(self, event):
        """将消息或引用中的图片作为原图登记入库（默认仅管理员）"""
        plugin = self.plugin
        if bool_cfg(self.config, "register_admin_only", DEFAULT_BASIC.register_admin_only) and not event.is_admin():
            yield event.plain_result("⛔ 仅管理员可登记原图（可在插件配置中修改）。")
            return

        images = plugin._extract_images(event)
        if not images:
            yield event.plain_result(
                "请与图片同条消息发送 /登记原图 [备注]，或引用一张图片发送 /登记原图。"
            )
            return

        note = plugin._strip_command(event.message_str, ("登记原图", "原图登记"))
        seg = images[0]
        local_path = ""
        is_tmp = False
        stored = None
        db_saved = False
        try:
            resolved = await plugin._resolve_local(
                seg,
                probe=lambda path: compute_features_with_mime(path, plugin.hash_size),
            )
            local_path, is_tmp, fail_message = (
                resolved.path,
                resolved.is_tmp,
                resolved.message,
            )
            if not local_path:
                yield event.plain_result(fail_message)
                return
            size_bytes = os.path.getsize(local_path)
            if size_bytes > plugin._download_limit_bytes():
                yield event.plain_result("⚠️ 图片超过大小上限，已取消登记。")
                return

            feats = resolved.features
            if feats is None:
                feats = await asyncio.to_thread(compute_features, local_path, plugin.hash_size)
            dup = plugin.library.find_by_phash(feats.phash)
            if dup is not None:
                yield event.plain_result(plugin._dup_message(dup))
                return

            stored = await plugin.storage.active.store(local_path)
            row = {
                "phash": feats.phash,
                "dhash": feats.dhash,
                "ahash": feats.ahash,
                "width": feats.width,
                "height": feats.height,
                "file_size": size_bytes,
                "image_url": stored.url or "",
                "file_path": stored.file_path or "",
                "source": "register",
                "note": note,
                "group_id": event.get_group_id() or "",
                "sender_id": event.get_sender_id() or "",
                "sender_name": event.get_sender_name() or "",
            }
            # 查重与插入在库方法的同一个锁区间内完成：上传期间的并发同图
            # 登记不会产生重复行（不再依赖"检查与插入之间无 await"的约定）
            entry_id, dup = await asyncio.to_thread(plugin.library.add_if_absent, row)
            if dup is not None:
                await plugin._cleanup_stored(stored)
                yield event.plain_result(plugin._dup_message(dup))
                return
            db_saved = True
            lines = [
                f"✅ 已登记原图 #{entry_id}（{stored.message}）",
                f"尺寸：{feats.width}x{feats.height}",
            ]
            if note:
                lines.append(f"备注：{note}")
            if stored.url:
                lines.append(f"直链：{stored.url}")
            if plugin.vector.enabled and vector_index_on_register(self.config):
                if plugin._bed_mode() == MODE_CFB:
                    # 图床侧钩子会把上传自动同步进向量库，无需插件再写一次
                    logger.debug(
                        "cloudflare_imgbed 模式：上传由图床侧钩子自动入库，跳过插件侧向量同步"
                    )
                elif not stored.url:
                    # 本地副本模式没有可供回图的公开直链，向量命中后也无法回传
                    logger.debug("当前图床模式没有公开直链，跳过向量同步")
                else:
                    try:
                        await plugin.vector.upsert_file(
                            local_path,
                            # 内容指纹作为 point id：同图同 ID，与图床 URL 规则无关
                            f"phash:{feats.phash}",
                            stored.url,
                            size_bytes,
                            feats.width,
                            feats.height,
                            src=plugin.storage.active.src_path(stored.url),
                            mime=resolved.mime,
                        )
                        lines.append("已同步至 Qdrant 向量库")
                    except Exception as e:
                        # 图片已上传、记录已入库，向量同步失败只是增量失败，
                        # 不能让用户误以为整次登记失败
                        logger.warning(f"登记原图向量同步失败: {e}")
                        lines.append("（向量同步失败，可在图床侧触发钩子或对账补齐）")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            if stored is not None and not db_saved:
                await plugin._cleanup_stored(stored)
            logger.error(f"登记原图失败: {e}", exc_info=True)
            yield event.plain_result("⚠️ 登记失败，详情请查看机器人日志。")
        finally:
            if is_tmp:
                plugin._remove_quiet(local_path)

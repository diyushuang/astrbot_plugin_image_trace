"""微博签到文案池：映射表读取、缓存、状态行与候选图选择。

文案池的实现主体；`main` 保留同名薄委托与缓存属性，因此
``controllers.checkin`` 的缓存失效逻辑、测试对 ``plugin._weibo_candidate``
的调用都继续有效。本模块只接收 host 门面，不反向 import main。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
from urllib.parse import unquote

from astrbot.api import logger

from ..image_delivery import build_original_url, upgrade_to_https
from ..random_media import swap_url_origin
from ..weibo_posts import (
    WeiboPost,
    load_post_map,
    load_posts_from_dir,
    map_is_stale,
)


class WeiboService:
    """微博文案池的读取、缓存与签到候选选择。"""

    def __init__(self, *, host) -> None:
        self.host = host

    def map_path(self) -> str:
        return os.path.join(self.host.weibo_dir, "weibo_post_map.json")

    def signature_sync(self) -> tuple:
        """映射表 + 源文件 mtime 组合；任一方变化即触发重新加载。"""
        host = self.host
        try:
            map_mtime = os.path.getmtime(self.map_path())
        except OSError:
            map_mtime = 0.0
        source_mtime = 0.0
        try:
            for name in os.listdir(host.weibo_dir):
                if not name.lower().endswith((".csv", ".md", ".markdown")):
                    continue
                try:
                    source_mtime = max(
                        source_mtime,
                        os.path.getmtime(os.path.join(host.weibo_dir, name)),
                    )
                except OSError:
                    continue
        except OSError:
            pass
        return (round(map_mtime, 3), round(source_mtime, 3))

    def load_pool_sync(self) -> tuple[list[WeiboPost], dict]:
        """读映射表；映射缺失/过期时才解析 CSV/MD，避免签到热路径重复解析。

        映射表优先（带图），源文件只用来补新帖子：映射过期时把源文件里有、
        映射里没有的帖子追加进来，这些帖子的 image 为 None，签到走回退。
        """
        host = self.host
        map_path = self.map_path()
        posts, generated_at, map_meta = load_post_map(map_path)
        stale = map_is_stale(map_path, host.weibo_dir)
        source_posts: list[WeiboPost] = []
        source_files: list[str] = []
        errors: list[str] = []
        if not posts or stale:
            source_posts, source_files, errors = load_posts_from_dir(host.weibo_dir)
        merged = posts
        if not posts:
            merged = source_posts
        elif stale and source_posts:
            by_key = {(post.account.casefold(), post.published_at): post for post in posts}
            merged = [
                by_key.get((post.account.casefold(), post.published_at), post)
                for post in source_posts
            ]
            # 源文件被删掉时仍保留映射里的旧条目，避免已有配图丢失
            source_keys = {
                (post.account.casefold(), post.published_at) for post in source_posts
            }
            merged.extend(
                post
                for post in posts
                if (post.account.casefold(), post.published_at) not in source_keys
            )
        meta = {
            "count": len(merged),
            "matched_count": sum(1 for post in merged if post.image is not None),
            "map_count": len(posts),
            "map_generated_at": generated_at,
            "map_stale": stale,
            "source_files": [os.path.basename(path) for path in source_files],
            "errors": errors,
            "map_meta": map_meta,
        }
        return merged, meta

    async def posts(self) -> tuple[list[WeiboPost], dict]:
        """签到热路径：mtime 未变时直接返回内存池，不解析任何文件。"""
        host = self.host
        signature = await asyncio.to_thread(self.signature_sync)
        if host._weibo_posts_cache is not None and signature == host._weibo_signature:
            return host._weibo_posts_cache, host._weibo_meta_cache
        posts, meta = await asyncio.to_thread(self.load_pool_sync)
        host._weibo_posts_cache = posts
        host._weibo_meta_cache = meta
        host._weibo_signature = signature
        if posts:
            host._weibo_empty_warned = False
        elif not host._weibo_empty_warned:
            logger.warning(
                "微博签到模式已开启，但 %s 下没有文案文件或映射表；/签到 将回退随机语句。",
                host.weibo_dir,
            )
            host._weibo_empty_warned = True
        if meta.get("errors"):
            logger.warning("微博文案解析失败: %s", "；".join(meta["errors"]))
        return posts, meta

    async def status_line(self, settings: dict) -> str:
        """`/溯源状态` 的签到模式一行；settings 由调用方从 config 门面注入。"""
        # 两个维度各自独立，状态行里都报出来，省得用户改完不知道生效在哪一层：
        # render_mode=合不合成，image_mode=用缩略图还是原图。文字/昵称两个
        # 开关同样常被改错（以为关掉文字会把签到信息也关掉），一并报出。
        shape = (
            f"{'合成卡片' if settings['render_mode'] == 'card' else '图文直发'}"
            f"，取图：{'原图' if settings['image_mode'] == 'original' else '缩略图'}"
            f"，文字：{'显示' if settings['show_quote'] else '不显示'}"
            f"，群昵称：{'显示' if settings['show_sender'] else '不显示'}"
        )
        if settings["mode"] != "weibo":
            return f"· 签到模式：quote（随机图 + 随机语句；{shape}）"
        posts, meta = await self.posts()
        if not posts:
            return f"· 签到模式：weibo（未找到文案/映射，/签到 将回退随机语句；{shape}）"
        matched = int(meta.get("matched_count") or 0)
        suffix = "，源文件比映射新" if meta.get("map_stale") else ""
        if not meta.get("map_count"):
            suffix += "，映射表未生成"
        return (
            f"· 签到模式：weibo（文案 {len(posts)} 条，可配对 {matched} 条"
            f"{suffix}；{shape}）"
        )

    def thumb_path(self, file_name: str) -> str:
        """本地预缓存缩略图路径；文件名用 sha1，避免中文/特殊字符进路径。"""
        key = hashlib.sha1(str(file_name or "").encode("utf-8")).hexdigest()
        path = os.path.join(self.host.weibo_thumb_dir, f"{key}.jpg")
        return path if os.path.isfile(path) else ""

    async def candidate(
        self, random_settings: dict, *, image_mode: str = "thumbnail"
    ):
        """取一条微博及其图库图片；返回 (post, 发送直链, 原图直链, 文件名, 本地缩略图)。

        映射表里有图就直接用；没有图时退一步取同账号同日的任意已匹配图片，
        仍没有则返回 None，由调用方回退现有「随机图 + 随机语句」。

        ``本地缩略图`` 由映射脚本的 ``--prefetch-thumbs`` 预生成；只有
        ``image_mode=thumbnail`` 才会返回它（card 模式零网络合成），原图模式
        必须发/用原图，不能被本地缩略图顶替。

        ``image_mode`` 来自 ``checkin.image_mode``，只影响签到：thumbnail 取
        ``thumb_url``，original 取 ``image_url``。
        """
        host = self.host
        posts, _meta = await self.posts()
        if not posts:
            return None
        post = random.choice(posts)
        image = post.image
        if image is None:
            pool = [
                item.image
                for item in posts
                if item.image is not None
                and item.account.casefold() == post.account.casefold()
                and item.date == post.date
            ]
            if pool:
                image = random.choice(pool)
        if image is None:
            return None
        use_thumb = image_mode != "original"
        local_thumb = self.thumb_path(image.file_name) if use_thumb else ""
        original_url = image.image_url or image.thumb_url
        if original_url:
            original_url = build_original_url(unquote(original_url))
        # 原图模式下优先真原图直链，映射里缺 image_url 时才退 thumb_url
        preferred, fallback = (
            (image.thumb_url, original_url)
            if use_thumb
            else (original_url, image.thumb_url)
        )
        send_url = preferred or fallback
        lan_hosts = host._lan_url_hosts(random_settings)
        lan_base = str((random_settings or {}).get("lan_base_url") or "")
        if lan_hosts:
            send_url = swap_url_origin(send_url, lan_base, lan_hosts)
            original_url = swap_url_origin(original_url, lan_base, lan_hosts)
        base_url = str((random_settings or {}).get("base_url") or "")
        send_url = upgrade_to_https(send_url, base_url)
        original_url = upgrade_to_https(original_url, base_url)
        if not send_url and not local_thumb:
            return None
        return post, send_url, original_url, image.file_name, local_thumb

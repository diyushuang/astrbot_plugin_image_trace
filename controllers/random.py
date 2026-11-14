"""随机图 / 随机视频 / LLM 工具控制器。"""

from __future__ import annotations

from ..config.loader import random_settings
from ..random_media import extract_directory


class RandomController:
    """承载 `随机图`、`随机视频` 与 LLM 工具 `sendRandomMedia`。"""

    def __init__(self, plugin, config) -> None:
        self.plugin = plugin
        self.config = config

    def _settings(self) -> dict:
        return random_settings(self.config, self.plugin._allowed_private_hosts)

    async def random_image(self, event):
        """从图床随机取一张图片并回传（可带目录，如 /随机图 风景）"""
        settings = self._settings()
        directory, parsed_type = extract_directory(event.message_str or "")
        async for result in self.plugin.services.random._random_media_delivery(
            event, directory, parsed_type or "image", settings
        ):
            yield result

    async def random_video(self, event):
        """从图床随机取一个视频并回传（可带目录，如 /随机视频 风景）"""
        settings = self._settings()
        directory, parsed_type = extract_directory(event.message_str or "")
        async for result in self.plugin.services.random._random_media_delivery(
            event, directory, parsed_type or "video", settings
        ):
            yield result

    async def send_random_media(
        self,
        event,
        directory: str | None = None,
        content_type: str | None = None,
    ):
        """发送随机图片或视频。

        当用户请求随机图片或视频时使用此工具。

        Args:
            directory(string): 目录路径，指定从哪个目录获取随机媒体，可选。
            content_type(string): 内容类型，可选值为 image 或 video，可选。
        """
        settings = self._settings()
        if not settings["enable_llm"]:
            yield event.plain_result("LLM 调用随机媒体已被禁用，请使用 /随机图 或 /随机视频 命令")
            return
        parsed_directory, parsed_type = extract_directory(event.message_str or "")
        if directory is None and parsed_directory:
            directory = parsed_directory
        content_type = (parsed_type or content_type or "image").strip().lower()
        async for result in self.plugin.services.random._random_media_delivery(
            event, directory, content_type, settings
        ):
            yield result

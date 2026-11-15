"""本地副本后端：图床不可用或未配置图床时的兜底落盘。"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import time


class LocalBackend:
    """把原图复制到插件数据目录（`<data_dir>/images`）。"""

    def __init__(self, local_dir: str) -> None:
        self.local_dir = local_dir

    async def save(self, src_path: str) -> str:
        """复制原图并返回副本路径。"""
        ext = os.path.splitext(src_path)[1].lower() or ".jpg"
        dest = os.path.join(
            self.local_dir, f"{time.strftime('%Y%m%d')}_{secrets.token_hex(8)}{ext}"
        )
        # 文件复制放到线程里避免阻塞事件循环；大小上游已校验
        await asyncio.to_thread(shutil.copyfile, src_path, dest)
        return dest

"""CloudFlare-ImgBed 自建图床后端（官方 REST API）。

接口文档：https://cfbed.sanyue.de/<lang>/api/（上传 /upload、
删除 /api/manage/delete/batch）。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlencode, urlparse

import aiohttp

from ..common import USER_AGENT
from ..url_guard import read_limited_text
from .backend_utils import read_upload_file
from .urls import same_origin

if TYPE_CHECKING:
    from ..http_client import GuardedHttpClient


class CloudflareImgBedBackend:
    """按官方 REST API 上传 / 删除，并从站点直链反解文件路径。"""

    def __init__(
        self,
        config: dict,
        http_getter: Callable[[], Awaitable[GuardedHttpClient]],
        timeout: Callable[[], aiohttp.ClientTimeout],
    ) -> None:
        self.config = config
        self._http_getter = http_getter
        self._timeout = timeout

    def base(self) -> str:
        """站点地址（去尾斜杠）；未配置时抛错，由调用方决定回退方式。"""
        base = str(self.config.get("cfi_base_url") or "").strip().rstrip("/")
        if not base:
            raise ValueError("未配置 CloudFlare-ImgBed 站点地址 cfi_base_url")
        return base

    def file_id_from_url(self, image_url: str) -> str:
        """从站点直链反解文件路径（不含 `/file/` 前缀）；不属于本站时返回空串。"""
        parsed = urlparse(image_url)
        base_parsed = urlparse(self.base())
        if not same_origin(parsed, base_parsed):
            return ""
        prefix = base_parsed.path.rstrip("/") + "/file/"
        if not parsed.path.startswith(prefix):
            return ""
        return unquote(parsed.path[len(prefix) :])

    def src_path_for(self, image_url: str) -> str:
        """床内相对路径（`/file/xxx`）；不属于本站时返回空串。"""
        parsed = urlparse(image_url)
        base_parsed = urlparse(self.base())
        prefix = base_parsed.path.rstrip("/") + "/file/"
        if same_origin(parsed, base_parsed) and parsed.path.startswith(prefix):
            return f"/file/{unquote(parsed.path[len(prefix) :])}"
        return ""

    async def upload(self, src_path: str) -> str:
        """按官方文档调用 POST /upload（multipart，文件字段名 file）。

        鉴权二选一（可同时配置）：cfi_token 以 Authorization: Bearer
        请求头发送（Token 需 upload 权限，官方推荐格式）；cfi_auth_code
        以 authCode 查询参数发送。两者均未配置且站点未开启登录校验时
        无需鉴权。uploadChannel / channelName / uploadFolder 为官方
        可选查询参数。

        响应为非空数组 [{"src": "/file/xxx.jpg", "publicUrl": "..."}]。
        直链优先取站点地址 + src（忽略 publicUrl），保证落库的链接与
        站点同源，/溯源删除 才能按前缀反解出文件路径。
        """
        base = self.base()
        token = str(self.config.get("cfi_token") or "").strip()
        auth_code = str(self.config.get("cfi_auth_code") or "").strip()

        params: dict = {}
        if auth_code:
            params["authCode"] = auth_code
        upload_channel = str(self.config.get("cfi_upload_channel") or "").strip()
        if upload_channel:
            params["uploadChannel"] = upload_channel
        channel_name = str(self.config.get("cfi_channel_name") or "").strip()
        if channel_name:
            params["channelName"] = channel_name
        upload_folder = str(self.config.get("cfi_upload_folder") or "").strip()
        if upload_folder:
            params["uploadFolder"] = upload_folder

        api_url = f"{base}/upload"
        if params:
            api_url = f"{api_url}?{urlencode(params)}"

        file_bytes, filename, content_type = await read_upload_file(src_path)

        def prepare(_url: str, method: str, cross_origin: bool = False) -> dict:
            headers = {"User-Agent": USER_AGENT}
            if token and not cross_origin:
                headers["Authorization"] = f"Bearer {token}"
            if method != "POST":
                return {"headers": headers}
            # FormData 不可复用，每次请求（含重定向后的重发）都需重新构建
            form = aiohttp.FormData()
            form.add_field("file", file_bytes, filename=filename, content_type=content_type)
            return {"data": form, "headers": headers}

        client = await self._http_getter()
        async with await client.request(
            "POST", api_url, prepare=prepare, timeout=self._timeout()
        ) as resp:
            body = await read_limited_text(resp)
            if not 200 <= resp.status < 300:
                raise RuntimeError(f"CloudFlare-ImgBed 返回 HTTP {resp.status}: {body[:500]}")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"CloudFlare-ImgBed 响应不是 JSON: {body[:200]}") from e
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise RuntimeError(f"CloudFlare-ImgBed 上传响应不是非空数组: {body[:200]}")
        src = str(payload[0].get("src") or "").strip()
        if not src:
            raise RuntimeError(f"上传响应缺少 src 字段: {body[:200]}")
        url = src if src.startswith(("http://", "https://")) else f"{base}/{src.lstrip('/')}"
        if not url.startswith(("http://", "https://")):
            raise RuntimeError(f"未能从上传响应提取到图片直链: {body[:200]}")
        return url

    async def delete(self, token: str, file_id: str) -> bool:
        """按官方文档调用 POST /api/manage/delete/batch 删除单个文件。"""
        base = self.base()
        api_url = f"{base}/api/manage/delete/batch"
        body = json.dumps({"fileIds": [file_id]})

        def prepare(_url: str, method: str, cross_origin: bool = False) -> dict:
            headers = {"User-Agent": USER_AGENT}
            if not cross_origin:
                headers["Authorization"] = f"Bearer {token}"
            if method != "POST":
                return {"headers": headers}
            return {
                "data": body,
                "headers": {**headers, "Content-Type": "application/json"},
            }

        timeout = self._timeout()
        client = await self._http_getter()
        async with await client.request("POST", api_url, prepare=prepare, timeout=timeout) as resp:
            resp_body = await read_limited_text(resp)
            if not 200 <= resp.status < 300:
                raise RuntimeError(
                    f"CloudFlare-ImgBed 删除返回 HTTP {resp.status}: {resp_body[:500]}"
                )
        try:
            payload = json.loads(resp_body)
        except json.JSONDecodeError:
            return False
        return bool(isinstance(payload, dict) and payload.get("success"))

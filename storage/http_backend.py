"""通用 HTTP 图床上传后端。

兼容 Lsky Pro、EasyImages、Chevereto 等常见自建图床——只需配置上传地址、
鉴权头、文件字段名与响应直链的 JSON 路径。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import aiohttp

from ..common import USER_AGENT
from ..url_guard import read_limited_text
from .backend_utils import read_upload_file
from .urls import extract_json_path

if TYPE_CHECKING:
    from ..http_client import GuardedHttpClient


class GenericHttpBackend:
    """按配置调用通用 HTTP 上传接口。"""

    def __init__(
        self,
        config: dict,
        http_getter: Callable[[], Awaitable[GuardedHttpClient]],
        timeout: Callable[[], aiohttp.ClientTimeout],
    ) -> None:
        self.config = config
        self._http_getter = http_getter
        self._timeout = timeout

    async def upload(self, src_path: str) -> str:
        """按配置调用通用 HTTP 上传接口，成功返回图片直链。"""
        api_url = str(self.config.get("api_url") or "").strip()
        if not api_url:
            raise ValueError("未配置图床上传接口地址 api_url")

        field = str(self.config.get("file_field") or "file").strip() or "file"
        url_path = str(self.config.get("url_path") or "data.url").strip() or "data.url"
        extra_fields = self.config.get("extra_fields") or {}
        if not isinstance(extra_fields, dict):
            extra_fields = {}

        auth_header = str(self.config.get("auth_header") or "").strip()
        token = str(self.config.get("token") or "").strip()
        auth_value = ""
        if auth_header and token:
            auth_value = f"{self.config.get('auth_prefix') or ''}{token}"

        file_bytes, filename, content_type = await read_upload_file(src_path)

        def prepare(_url: str, method: str, cross_origin: bool = False) -> dict:
            headers = {"User-Agent": USER_AGENT}
            # 跨域重定向会把请求交给第三方，鉴权信息一律不再附带
            if auth_value and not cross_origin:
                headers[auth_header] = auth_value
            if method != "POST":
                return {"headers": headers}
            # FormData 不可复用，每次请求（含重定向后的重发）都需重新构建
            form = aiohttp.FormData()
            for key, value in extra_fields.items():
                form.add_field(str(key), str(value))
            form.add_field(field, file_bytes, filename=filename, content_type=content_type)
            return {"data": form, "headers": headers}

        client = await self._http_getter()
        async with await client.request(
            "POST", api_url, prepare=prepare, timeout=self._timeout()
        ) as resp:
            body = await read_limited_text(resp)
            if not 200 <= resp.status < 300:
                raise RuntimeError(f"图床返回 HTTP {resp.status}: {body[:500]}")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"图床响应不是 JSON: {body[:200]}") from e

        url = extract_json_path(payload, url_path)
        if not url or not str(url).startswith(("http://", "https://")):
            raise RuntimeError(f"未能从响应路径 {url_path!r} 提取到图片直链: {body[:200]}")
        return str(url)

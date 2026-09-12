"""泛用图床/对象存储客户端。

mode=local        : 原图副本保存到机器人数据目录（零配置，默认）。
mode=generic_http : 通过可配置的通用 HTTP 接口上传，兼容 Lsky Pro、
                    EasyImages、Chevereto 等常见自建图床——只需配置
                    上传地址、鉴权头、文件字段名与响应直链的 JSON 路径。
mode=cloudflare_r2: Cloudflare R2 对象存储（官方 S3 兼容 API），
                    endpoint https://<ACCOUNT_ID>.r2.cloudflarestorage.com，
                    签名 region 固定 auto；公开直链需在 Cloudflare 侧
                    开启 r2.dev 公开访问或绑定自定义域名。
mode=oracle_oci   : 甲骨文 OCI Object Storage（官方 Amazon S3 兼容 API），
                    endpoint https://<namespace>.compat.objectstorage.
                    <region>.oraclecloud.com，凭据为 Customer Secret Key；
                    公开直链支持公共存储桶原生 URL 或自定义地址（如
                    带前缀的预认证请求 PAR 地址）。
mode=cloudflare_imgbed: CloudFlare-ImgBed 自建图床（官方 REST API）：
                    POST /upload 上传（API Token 或上传鉴权码 authCode），
                    /溯源删除 时按 POST /api/manage/delete/batch 同步
                    删除远端文件（Token 需含 delete 权限）。

上传失败或无法生成长期有效的公开直链时，自动回退为本地副本保存，
保证登记流程不因图床故障而中断（溯源回图需要能长期访问的链接）。
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple
from urllib.parse import quote, unquote, urlencode, urlparse

import aiohttp

try:
    from .common import USER_AGENT, truthy
    from .s3_store import S3CompatClient
    from .url_guard import guarded_request, make_pinned_connector, read_limited_text
except ImportError:  # 兼容插件以独立模块方式加载
    from common import USER_AGENT, truthy  # type: ignore[no-redef]
    from s3_store import S3CompatClient  # type: ignore[no-redef]
    from url_guard import (  # type: ignore[no-redef]
        guarded_request,
        make_pinned_connector,
        read_limited_text,
    )

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("image_bed")

MODE_LOCAL = "local"
MODE_HTTP = "generic_http"
MODE_R2 = "cloudflare_r2"
MODE_OCI = "oracle_oci"
MODE_CFB = "cloudflare_imgbed"


@dataclass
class StoreResult:
    ok: bool
    url: Optional[str] = None  # 图床直链（若有）
    file_path: Optional[str] = None  # 本地文件路径（若有）
    message: str = ""


def extract_json_path(data: Any, path: str) -> Any:
    """按点号路径从嵌套 JSON 中取值，支持数组下标（如 data.links.0.url）。

    约定：键名含 "." 无法表达；任何一段取不到（键缺失/下标越界/类型
    不符）即返回 None；列表支持负下标（current[-1] 取末元素）。
    """
    current = data
    for part in path.split("."):
        part = part.strip()
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _public_url(base: str, key: str) -> str:
    """拼接公开直链：base 去尾斜杠 + URI 编码后的对象名。"""
    return f"{base.rstrip('/')}/{quote(key)}"


def _host_port(parsed) -> Tuple[str, Optional[int]]:
    """URL 的归一化 (主机名小写, 端口)，端口缺省按协议补全。"""
    try:
        port = parsed.port
    except ValueError:
        return "", None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return (parsed.hostname or "").lower(), port


def _same_origin(a, b) -> bool:
    """两个 parsed URL 是否同源（协议 + 主机 + 端口）。"""
    if a.scheme != b.scheme:
        return False
    return _host_port(a) == _host_port(b)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


class ImageBedClient:
    def __init__(self, data_dir: str, bed_config: dict):
        self.mode = str(bed_config.get("mode") or MODE_LOCAL).strip()
        self.config = bed_config or {}
        self.local_dir = os.path.join(data_dir, "images")
        os.makedirs(self.local_dir, exist_ok=True)
        # S3 兼容客户端按模式缓存复用（会话懒建，close() 统一释放）
        self._s3_clients: dict = {}

    # ---------- 对外接口 ----------

    async def store(self, src_path: str) -> StoreResult:
        """登记原图的落库：优先上传图床/对象存储，失败回退本地副本。"""
        if self.mode == MODE_HTTP:
            try:
                url = await self._upload(src_path)
                return StoreResult(ok=True, url=url, message="已上传至图床")
            except Exception as e:
                local = await self._copy_local(src_path)
                return StoreResult(
                    ok=True,
                    file_path=local,
                    message=f"图床上传失败，已保存本地副本（{e}）",
                )
        if self.mode == MODE_CFB:
            try:
                url = await self._upload_cf(src_path)
                return StoreResult(ok=True, url=url, message="已上传至 CloudFlare-ImgBed")
            except Exception as e:
                local = await self._copy_local(src_path)
                return StoreResult(
                    ok=True,
                    file_path=local,
                    message=f"CloudFlare-ImgBed 上传失败，已保存本地副本（{e}）",
                )
        if self.mode in (MODE_R2, MODE_OCI):
            provider = "Cloudflare R2" if self.mode == MODE_R2 else "OCI 对象存储"
            try:
                url = await self._upload_object(src_path)
                return StoreResult(ok=True, url=url, message=f"已上传至{provider}")
            except Exception as e:
                local = await self._copy_local(src_path)
                return StoreResult(
                    ok=True,
                    file_path=local,
                    message=f"{provider}上传失败，已保存本地副本（{e}）",
                )
        local = await self._copy_local(src_path)
        if self.mode != MODE_LOCAL:
            # schema 的 options 会拦住常见笔误，但手工改配置文件可绕过；
            # 静默按 local 处理会让人误以为配置生效了
            logger.warning(f"未知的图床模式 {self.mode!r}，已按本地副本保存。")
            return StoreResult(
                ok=True,
                file_path=local,
                message=f"图床模式 {self.mode!r} 无法识别，已保存本地副本",
            )
        return StoreResult(ok=True, file_path=local, message="已保存到本地图床")

    def _public_base(self, key: str, name: str) -> str:
        """取公开直链 base；缺少 http(s):// 前缀时按未配置处理。

        直链会入库并用于回图，scheme 不全的 base（如漏写 https:// 的
        r2.dev 子域）会产生非法直链：上传虽成功，回图却静默失败。
        """
        base = str(self.config.get(key) or "").strip().rstrip("/")
        if base and not base.lower().startswith(("http://", "https://")):
            logger.warning(f"{name} 缺少 http(s):// 前缀（当前值: {base}），已忽略。")
            return ""
        return base

    async def delete_remote(self, image_url: str) -> bool:
        """删除对象存储中的远端对象；URL 不属于当前配置时返回 False。

        对 cloudflare_r2 / oracle_oci 模式按公开直链反解对象名，与当前
        配置同源且路径前缀匹配后调用 DeleteObject；cloudflare_imgbed 模式
        按站点直链反解文件路径后调用官方删除 API（Token 需 delete 权限）。
        """
        if not image_url:
            return False
        try:
            parsed = urlparse(image_url)
            if self.mode == MODE_CFB:
                base = self._cf_base()
                base_parsed = urlparse(base)
                if not _same_origin(parsed, base_parsed):
                    return False
                prefix = base_parsed.path.rstrip("/") + "/file/"
                if not parsed.path.startswith(prefix):
                    return False
                file_id = unquote(parsed.path[len(prefix):])
                if not file_id:
                    return False
                token = str(self.config.get("cfi_token") or "").strip()
                if not token:
                    return False
                return await self._cf_delete(token, file_id)
            if self.mode == MODE_R2:
                base = self._public_base("r2_public_base_url", "r2_public_base_url")
                if not base:
                    return False
                base_parsed = urlparse(base)
                if not _same_origin(parsed, base_parsed):
                    return False
                prefix = base_parsed.path.rstrip("/") + "/"
                if not parsed.path.startswith(prefix):
                    return False
                key = unquote(parsed.path[len(prefix):])
                if not key:
                    return False
                client, bucket = self._r2_client()
                await client.delete_object(bucket, key)
                return True
            if self.mode == MODE_OCI:
                key, matched = self._oci_key_from_url(image_url)
                if not matched:
                    return False
                client, bucket = self._oci_client()
                await client.delete_object(bucket, key)
                return True
        except Exception as e:
            # 不吞异常原因：删除失败（token 权限不足、签名错误、网络故障）需要可诊断
            logger.warning(f"删除远端对象失败 [{self.mode}] {image_url}: {e}")
            return False
        return False

    def src_path_for(self, image_url: str) -> str:
        """从公开直链反解床内路径（仅用于向量 payload 的 src 字段）。

        能反解则返回床内相对路径（R2/OCI 为对象名、CFB 为 /file/xxx），
        否则原样返回直链。插件自身不依赖该字段，仅为兼容图床侧约定。
        """
        if not image_url:
            return ""
        try:
            parsed = urlparse(image_url)
            if self.mode == MODE_CFB:
                base = self._cf_base()
                base_parsed = urlparse(base)
                prefix = base_parsed.path.rstrip("/") + "/file/"
                if _same_origin(parsed, base_parsed) and parsed.path.startswith(prefix):
                    return f"/file/{unquote(parsed.path[len(prefix):])}"
            elif self.mode == MODE_R2:
                base = self._public_base("r2_public_base_url", "r2_public_base_url")
                if base:
                    base_parsed = urlparse(base)
                    if _same_origin(parsed, base_parsed):
                        prefix = base_parsed.path.rstrip("/") + "/"
                        if parsed.path.startswith(prefix):
                            key = unquote(parsed.path[len(prefix):])
                            if key:
                                return key
            elif self.mode == MODE_OCI:
                key, matched = self._oci_key_from_url(image_url)
                if matched:
                    return key
        except Exception as e:
            logger.debug(f"反解床内路径失败，回退直链: {e}")
        return image_url

    async def close(self) -> None:
        """释放缓存的 S3 客户端会话（插件卸载时调用）。"""
        for client, _bucket in self._s3_clients.values():
            await client.aclose()
        self._s3_clients.clear()

    # ---------- 通用 HTTP 图床 ----------

    async def _copy_local(self, src_path: str) -> str:
        ext = os.path.splitext(src_path)[1].lower() or ".jpg"
        dest = os.path.join(
            self.local_dir, f"{time.strftime('%Y%m%d')}_{secrets.token_hex(8)}{ext}"
        )
        # 最大 20MB 的整文件复制，放到线程里避免阻塞事件循环
        await asyncio.to_thread(shutil.copyfile, src_path, dest)
        return dest

    async def _upload(self, src_path: str) -> str:
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

        ext = os.path.splitext(src_path)[1].lower() or ".jpg"
        filename = f"trace_{time.strftime('%Y%m%d')}_{secrets.token_hex(8)}{ext}"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        file_bytes = await asyncio.to_thread(_read_bytes, src_path)

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

        timeout = aiohttp.ClientTimeout(total=int(self.config.get("timeout") or 30))
        async with aiohttp.ClientSession(
            timeout=timeout, connector=make_pinned_connector()
        ) as session:
            async with await guarded_request(session, "POST", api_url, prepare=prepare) as resp:
                body = await read_limited_text(resp)
                if not 200 <= resp.status < 300:
                    raise RuntimeError(f"图床返回 HTTP {resp.status}: {body[:200]}")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"图床响应不是 JSON: {body[:200]}") from e

        url = extract_json_path(payload, url_path)
        if not url or not str(url).startswith(("http://", "https://")):
            raise RuntimeError(f"未能从响应路径 {url_path!r} 提取到图片直链: {body[:200]}")
        return str(url)

    # ---------- CloudFlare-ImgBed（官方 REST API） ----------
    #
    # 接口文档：https://cfbed.sanyue.de/<lang>/api/（上传 /upload、
    # 删除 /api/manage/delete/batch）。

    def _cf_base(self) -> str:
        base = str(self.config.get("cfi_base_url") or "").strip().rstrip("/")
        if not base:
            raise ValueError("未配置 CloudFlare-ImgBed 站点地址 cfi_base_url")
        return base

    async def _upload_cf(self, src_path: str) -> str:
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
        base = self._cf_base()
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

        ext = os.path.splitext(src_path)[1].lower() or ".jpg"
        filename = f"trace_{time.strftime('%Y%m%d')}_{secrets.token_hex(8)}{ext}"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        file_bytes = await asyncio.to_thread(_read_bytes, src_path)

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

        timeout = aiohttp.ClientTimeout(total=int(self.config.get("timeout") or 30))
        async with aiohttp.ClientSession(
            timeout=timeout, connector=make_pinned_connector()
        ) as session:
            async with await guarded_request(session, "POST", api_url, prepare=prepare) as resp:
                body = await read_limited_text(resp)
                if not 200 <= resp.status < 300:
                    raise RuntimeError(f"CloudFlare-ImgBed 返回 HTTP {resp.status}: {body[:200]}")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"CloudFlare-ImgBed 响应不是 JSON: {body[:200]}") from e
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise RuntimeError(f"CloudFlare-ImgBed 上传响应不是非空数组: {body[:200]}")
        src = str(payload[0].get("src") or "").strip()
        if not src:
            raise RuntimeError(f"上传响应缺少 src 字段: {body[:200]}")
        if src.startswith(("http://", "https://")):
            url = src
        else:
            url = f"{base}/{src.lstrip('/')}"
        if not url.startswith(("http://", "https://")):
            raise RuntimeError(f"未能从上传响应提取到图片直链: {body[:200]}")
        return url

    async def _cf_delete(self, token: str, file_id: str) -> bool:
        """按官方文档调用 POST /api/manage/delete/batch 删除单个文件。"""
        base = self._cf_base()
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

        timeout = aiohttp.ClientTimeout(total=int(self.config.get("timeout") or 30))
        async with aiohttp.ClientSession(
            timeout=timeout, connector=make_pinned_connector()
        ) as session:
            async with await guarded_request(session, "POST", api_url, prepare=prepare) as resp:
                resp_body = await read_limited_text(resp)
                if not 200 <= resp.status < 300:
                    raise RuntimeError(
                        f"CloudFlare-ImgBed 删除返回 HTTP {resp.status}: {resp_body[:200]}"
                    )
        try:
            payload = json.loads(resp_body)
        except json.JSONDecodeError:
            return False
        return bool(isinstance(payload, dict) and payload.get("success"))

    # ---------- 对象存储（R2 / OCI，S3 兼容 API） ----------

    @staticmethod
    def _object_name(src_path: str) -> Tuple[str, str]:
        """生成对象名与 Content-Type（形如 trace_20260906_ab12cd34ef56ab78.jpg）。"""
        ext = os.path.splitext(src_path)[1].lower() or ".jpg"
        key = f"trace_{time.strftime('%Y%m%d')}_{secrets.token_hex(8)}{ext}"
        ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
        return key, ctype

    @staticmethod
    def _require(*pairs) -> None:
        missing = [name for value, name in pairs if not str(value or "").strip()]
        if missing:
            raise ValueError(f"缺少必填配置: {'、'.join(missing)}")

    def _r2_client(self) -> Tuple[S3CompatClient, str]:
        cached = self._s3_clients.get("r2")
        if cached is not None:
            return cached
        cfg = self.config
        account_id = str(cfg.get("r2_account_id") or "").strip()
        access_key = str(cfg.get("r2_access_key_id") or "").strip()
        secret_key = str(cfg.get("r2_secret_access_key") or "").strip()
        bucket = str(cfg.get("r2_bucket") or "").strip()
        self._require(
            (account_id, "r2_account_id"),
            (access_key, "r2_access_key_id"),
            (secret_key, "r2_secret_access_key"),
            (bucket, "r2_bucket"),
        )
        # 官方默认 endpoint；欧盟等司法区可在 r2_endpoint 中覆盖
        endpoint = (
            str(cfg.get("r2_endpoint") or "").strip()
            or f"https://{account_id}.r2.cloudflarestorage.com"
        )
        client = S3CompatClient(
            endpoint,
            access_key,
            secret_key,
            region="auto",  # R2 官方文档：签名 region 固定为 auto
            timeout=int(cfg.get("timeout") or 30),
        )
        self._s3_clients["r2"] = (client, bucket)
        return client, bucket

    def _oci_client(self) -> Tuple[S3CompatClient, str]:
        cached = self._s3_clients.get("oci")
        if cached is not None:
            return cached
        cfg = self.config
        namespace = str(cfg.get("oci_namespace") or "").strip()
        region = str(cfg.get("oci_region") or "").strip()
        access_key = str(cfg.get("oci_access_key_id") or "").strip()
        secret_key = str(cfg.get("oci_secret_access_key") or "").strip()
        bucket = str(cfg.get("oci_bucket") or "").strip()
        self._require(
            (namespace, "oci_namespace"),
            (region, "oci_region"),
            (access_key, "oci_access_key_id"),
            (secret_key, "oci_secret_access_key"),
            (bucket, "oci_bucket"),
        )
        # 官方 S3 兼容 API endpoint（仅支持 path-style）；
        # 特殊 realm 场景可用 oci_endpoint 覆盖
        endpoint = (
            str(cfg.get("oci_endpoint") or "").strip()
            or f"https://{namespace}.compat.objectstorage.{region}.oraclecloud.com"
        )
        client = S3CompatClient(
            endpoint,
            access_key,
            secret_key,
            region=region,  # 签名 region 使用 OCI 区域标识
            timeout=int(cfg.get("timeout") or 30),
        )
        self._s3_clients["oci"] = (client, bucket)
        return client, bucket

    def _oci_public_url(self, key: str) -> str:
        """OCI 公开直链：自定义地址（含前缀 PAR）优先，公共桶官方 URL 兜底。

        两者都不可用时抛错，由 store() 回退本地副本。
        """
        cfg = self.config
        custom = self._public_base("oci_public_base_url", "oci_public_base_url")
        if custom:
            return _public_url(custom, key)
        if self._bool_cfg(cfg.get("oci_public_bucket")):
            namespace = str(cfg.get("oci_namespace") or "").strip()
            region = str(cfg.get("oci_region") or "").strip()
            bucket = str(cfg.get("oci_bucket") or "").strip()
            self._require(
                (namespace, "oci_namespace"),
                (region, "oci_region"),
                (bucket, "oci_bucket"),
            )
            # 官方文档的对象 URL 格式（公共存储桶可直接匿名读取）
            return (
                f"https://objectstorage.{region}.oraclecloud.com"
                f"/n/{namespace}/b/{bucket}/o/{quote(key)}"
            )
        raise ValueError(
            "未配置公开访问方式：请设置 oci_public_base_url（自定义地址或"
            "带前缀的 PAR 地址），或开启 oci_public_bucket（公共存储桶）"
        )

    @staticmethod
    def _bool_cfg(value) -> bool:
        return truthy(value)

    def _oci_public_hosts(self) -> set:
        """OCI 公开直链允许的主机名集合（按当前配置推导）。"""
        hosts = set()
        region = str(self.config.get("oci_region") or "").strip()
        if region:
            hosts.add(f"objectstorage.{region}.oraclecloud.com")
        endpoint = str(self.config.get("oci_endpoint") or "").strip()
        if endpoint:
            host = urlparse(endpoint).hostname
            if host:
                hosts.add(host.lower())
        return hosts

    def _oci_key_from_url(self, image_url: str) -> Tuple[str, bool]:
        """从 OCI 公开直链反解对象名；仅接受与当前配置同源的 URL。

        必须校验主机：否则任何形如 https://任意域名/n/<命名空间>/b/<桶>/o/<对象名>
        的历史 URL 都能触发对配置存储桶内对象的删除。
        """
        parsed = urlparse(image_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "", False
        # 用 parsed.path 而非原始字符串，顺带剥掉 query/fragment，
        # 避免把 "对象名?token=..." 当成对象名去删除
        key_path = parsed.path

        custom = self._public_base("oci_public_base_url", "oci_public_base_url")
        if custom:
            base_parsed = urlparse(custom)
            if _same_origin(parsed, base_parsed):
                prefix = base_parsed.path.rstrip("/") + "/"
                if key_path.startswith(prefix):
                    key = unquote(key_path[len(prefix):])
                    if key:
                        return key, True

        if self._bool_cfg(self.config.get("oci_public_bucket")):
            namespace = str(self.config.get("oci_namespace") or "").strip()
            bucket = str(self.config.get("oci_bucket") or "").strip()
            if (parsed.hostname or "").lower() not in self._oci_public_hosts():
                return "", False
            parts = key_path.split("/o/", 1)
            if len(parts) == 2 and key_path.startswith("/n/"):
                seg = parts[0].strip("/").split("/")
                # /n/{namespace}/b/{bucket}/o/{key}
                if len(seg) == 4 and seg[0] == "n" and seg[2] == "b":
                    if seg[1] == namespace and seg[3] == bucket:
                        key = unquote(parts[1])
                        if key:
                            return key, True
        return "", False

    async def _upload_object(self, src_path: str) -> str:
        """上传到 R2 / OCI 对象存储，返回长期有效的公开直链。"""
        if self.mode == MODE_R2:
            client, bucket = self._r2_client()
            public_base = self._public_base("r2_public_base_url", "r2_public_base_url")
            if not public_base:
                raise ValueError(
                    "未配置有效的 r2_public_base_url（需在 Cloudflare 侧为存储桶开启"
                    " r2.dev 公开访问或绑定自定义域名，https:// 开头），溯源回图"
                    "需要长期有效的直链"
                )
            key, ctype = self._object_name(src_path)
            data = await asyncio.to_thread(_read_bytes, src_path)
            await client.put_object(bucket, key, data, ctype)
            return _public_url(public_base, key)

        # MODE_OCI
        client, bucket = self._oci_client()
        key, ctype = self._object_name(src_path)
        data = await asyncio.to_thread(_read_bytes, src_path)
        await client.put_object(bucket, key, data, ctype)
        return self._oci_public_url(key)

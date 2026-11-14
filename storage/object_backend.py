"""R2 / OCI 对象存储后端（S3 兼容 API）。

- Cloudflare R2：endpoint https://<ACCOUNT_ID>.r2.cloudflarestorage.com，
  签名 region 固定 auto；公开直链需在 Cloudflare 侧开启 r2.dev 公开访问
  或绑定自定义域名。
- 甲骨文 OCI Object Storage：endpoint
  https://<namespace>.compat.objectstorage.<region>.oraclecloud.com，
  凭据为 Customer Secret Key；公开直链支持公共存储桶原生 URL 或自定义
  地址（如带前缀的预认证请求 PAR 地址）。
"""

from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote, urlparse

from ..common import read_bytes, truthy
from ..s3_store import S3CompatClient
from .backend_utils import logger, new_filename
from .modes import MODE_OCI, MODE_R2
from .urls import public_url, same_origin

if TYPE_CHECKING:
    from ..http_client import GuardedHttpClient


class ObjectBackend:
    """按 mode（R2 / OCI）选择对象存储配置，负责上传、直链与反解。"""

    def __init__(
        self,
        config: dict,
        http_getter: Callable[[], Awaitable[GuardedHttpClient]],
        mode: str,
        timeout_seconds: Callable[[], int],
    ) -> None:
        self.config = config
        self.mode = mode
        self._http_getter = http_getter
        self._timeout_seconds = timeout_seconds
        # S3 兼容客户端按模式缓存复用；HTTP 会话由共享客户端统一释放
        self._s3_clients: dict = {}

    # ---------- 配置解析 ----------

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

    @staticmethod
    def _require(*pairs) -> None:
        missing = [name for value, name in pairs if not str(value or "").strip()]
        if missing:
            raise ValueError(f"缺少必填配置: {'、'.join(missing)}")

    @staticmethod
    def _bool_cfg(value) -> bool:
        return truthy(value)

    def _r2_client(self) -> tuple[S3CompatClient, str]:
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
            timeout=self._timeout_seconds(),
            http_getter=self._http_getter,
        )
        self._s3_clients["r2"] = (client, bucket)
        return client, bucket

    def _oci_client(self) -> tuple[S3CompatClient, str]:
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
            timeout=self._timeout_seconds(),
            http_getter=self._http_getter,
        )
        self._s3_clients["oci"] = (client, bucket)
        return client, bucket

    # ---------- 公开直链与反解 ----------

    def oci_public_url(self, key: str) -> str:
        """OCI 公开直链：自定义地址（含前缀 PAR）优先，公共桶官方 URL 兜底。

        两者都不可用时抛错，由调用方回退本地副本。
        """
        cfg = self.config
        custom = self._public_base("oci_public_base_url", "oci_public_base_url")
        if custom:
            return public_url(custom, key)
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

    def oci_key_from_url(self, image_url: str) -> tuple[str, bool]:
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
            if same_origin(parsed, base_parsed):
                prefix = base_parsed.path.rstrip("/") + "/"
                if key_path.startswith(prefix):
                    key = unquote(key_path[len(prefix) :])
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
                if (
                    len(seg) == 4
                    and seg[0] == "n"
                    and seg[1] == namespace
                    and seg[2] == "b"
                    and seg[3] == bucket
                ):
                    key = unquote(parts[1])
                    if key:
                        return key, True
        return "", False

    def r2_key_from_url(self, image_url: str) -> tuple[str, bool]:
        """从 R2 公开直链反解对象名；仅接受与公开 base 同源的 URL。"""
        base = self._public_base("r2_public_base_url", "r2_public_base_url")
        if not base:
            return "", False
        parsed = urlparse(image_url)
        base_parsed = urlparse(base)
        if not same_origin(parsed, base_parsed):
            return "", False
        prefix = base_parsed.path.rstrip("/") + "/"
        if not parsed.path.startswith(prefix):
            return "", False
        key = unquote(parsed.path[len(prefix) :])
        if not key:
            return "", False
        return key, True

    def key_from_url(self, image_url: str) -> tuple[str, bool]:
        """按当前 mode 反解对象名；不属于当前存储时返回 ("", False)。"""
        if self.mode == MODE_R2:
            return self.r2_key_from_url(image_url)
        if self.mode == MODE_OCI:
            return self.oci_key_from_url(image_url)
        return "", False

    # ---------- 上传 / 删除 ----------

    @staticmethod
    def _object_name(src_path: str) -> tuple[str, str]:
        """生成对象名与 Content-Type（形如 trace_20260906_ab12cd34ef56ab78.jpg）。"""
        key = new_filename(src_path)
        ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
        return key, ctype

    async def _put_object_bytes(self, client: S3CompatClient, bucket: str, src_path: str) -> str:
        """把文件上传为新的对象名，返回对象键（R2/OCI 共用）。"""
        key, ctype = self._object_name(src_path)
        data = await asyncio.to_thread(read_bytes, src_path)
        await client.put_object(bucket, key, data, ctype)
        return key

    async def upload(self, src_path: str) -> str:
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
            return public_url(public_base, await self._put_object_bytes(client, bucket, src_path))

        # MODE_OCI
        client, bucket = self._oci_client()
        return self.oci_public_url(await self._put_object_bytes(client, bucket, src_path))

    async def delete(self, image_url: str) -> bool:
        """按公开直链反解对象名并调用 DeleteObject；不属于当前存储时返回 False。"""
        key, matched = self.key_from_url(image_url)
        if not matched:
            return False
        if self.mode == MODE_R2:
            client, bucket = self._r2_client()
        elif self.mode == MODE_OCI:
            client, bucket = self._oci_client()
        else:
            return False
        await client.delete_object(bucket, key)
        return True

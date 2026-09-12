"""S3 兼容对象存储客户端（AWS Signature Version 4 签名）。

按两家对象存储的官方文档实现，共用一套 SigV4 签名：

- Cloudflare R2（developers.cloudflare.com/r2/api/s3/api/）：
  endpoint 形如 https://<ACCOUNT_ID>.r2.cloudflarestorage.com
  （欧盟司法区为 <ACCOUNT_ID>.eu.r2.cloudflarestorage.com），签名 region
  固定为 auto，服务名 s3，path-style URI：/{bucket}/{key}。
  PutObject 单次最大 5 GiB；DeleteObject 删除对象（响应 204）。

- Oracle Cloud Infrastructure Object Storage 的 Amazon S3 兼容 API
  （docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi.htm）：
  endpoint 形如
  https://<namespace>.compat.objectstorage.<region>.oraclecloud.com，
  凭据为 Customer Secret Key（Access Key / Secret Key），仅支持
  path-style，签名同样是 AWS Signature Version 4（region 填 OCI 区域
  标识，如 ap-osaka-1）。

签名只用标准库（hashlib/hmac），HTTP 请求经 url_guard.guarded_request
发出（SSRF 逐跳校验 + IP 钉扎）。PutObject/DeleteObject 均无查询参数，
故 CanonicalQueryString 固定为空串；一旦被重定向（目标或方法变化）签名即
不再成立，此时直接报错而不是发出必然 403 的请求。
"""

from __future__ import annotations

import hashlib
import hmac
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlparse

import aiohttp

try:
    from .common import USER_AGENT
    from .url_guard import UrlBlockedError, guarded_request, make_pinned_connector, read_limited_bytes
except ImportError:  # 兼容插件以独立模块方式加载
    from common import USER_AGENT  # type: ignore[no-redef]
    from url_guard import (  # type: ignore[no-redef]
        UrlBlockedError,
        guarded_request,
        make_pinned_connector,
        read_limited_bytes,
    )

EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()


def uri_encode_path(path: str) -> str:
    """对 URL 路径逐字符 URI 编码并保留斜杠，即 SigV4 的 CanonicalURI。"""
    return quote(path, safe="/")


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def derive_signing_key(secret_key: str, datestamp: str, region: str) -> bytes:
    """按 SigV4 规范逐级派生签名密钥（服务名固定 s3）。"""
    key = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), datestamp)
    key = _hmac_sha256(key, region)
    key = _hmac_sha256(key, "s3")
    return _hmac_sha256(key, "aws4_request")


def _canonical_host(parsed) -> str:
    """SigV4 签名用的 Host 头：主机名小写，且省略协议默认端口。

    必须与实际发出的 Host 头一致（yarl/aiohttp 会小写主机名并省略默认端口），
    否则 endpoint 写成大写域名或显式 :443 时签名与请求不符，必然 403。
    """
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("endpoint 缺少主机名")
    try:
        port = parsed.port
    except ValueError as e:
        raise ValueError(f"endpoint 端口不合法: {e}") from e
    default_port = 443 if parsed.scheme == "https" else 80
    if port and port != default_port:
        return f"{host}:{port}"
    return host


def sign_request(
    method: str,
    url: str,
    amz_date: str,
    payload_sha256_hex: str,
    access_key: str,
    secret_key: str,
    region: str,
    extra_signed_headers: Optional[dict] = None,
    canonical_uri: Optional[str] = None,
) -> str:
    """按 AWS Signature Version 4（S3 服务）生成 Authorization 请求头。

    默认签名头集合为 host;x-amz-content-sha256;x-amz-date，与发送的
    请求头保持一致；查询串为空（PutObject/DeleteObject 均无查询参数）。
    extra_signed_headers 仅用于对照 AWS 官方测试向量补充额外的签名头
    （如 date、x-amz-storage-class），正常运行时不传。
    canonical_uri 用于传入调用方已经编码好的路径，避免从 URL 再编码一次
    造成二次编码（对象名含 + % 空格时签名会与实际请求不一致）。
    """
    parsed = urlparse(url)
    if canonical_uri is None:
        canonical_uri = uri_encode_path(parsed.path or "/")
    datestamp = amz_date[:8]

    headers = {
        "host": _canonical_host(parsed),
        "x-amz-content-sha256": payload_sha256_hex,
        "x-amz-date": amz_date,
    }
    if extra_signed_headers:
        headers.update(
            {k.strip().lower(): str(v).strip() for k, v in extra_signed_headers.items()}
        )
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))

    canonical_request = (
        f"{method}\n{canonical_uri}\n\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_sha256_hex}"
    )
    scope = f"{datestamp}/{region}/s3/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signing_key = derive_signing_key(secret_key, datestamp, region)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def _s3_error_detail(body: bytes) -> str:
    """尽量从 S3 错误响应 XML 提取 <Code>/<Message>，解析失败时回退原文。"""
    text = body.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return text[:200]
    code = root.findtext("Code") or ""
    message = root.findtext("Message") or ""
    if code or message:
        return f"{code}: {message}".strip(": ")
    return text[:200]


class S3CompatClient:
    """最小化的 S3 兼容客户端，仅实现 PutObject 与 DeleteObject。"""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        region: str,
        timeout: int = 30,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.timeout = aiohttp.ClientTimeout(total=max(5, int(timeout)))
        self._session: Optional[aiohttp.ClientSession] = None

    async def put_object(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        await self._signed("PUT", f"/{bucket}/{key}", data=data, content_type=content_type)

    async def delete_object(self, bucket: str, key: str) -> None:
        await self._signed("DELETE", f"/{bucket}/{key}", expect=(200, 204))

    async def _get_session(self) -> aiohttp.ClientSession:
        """懒建长会话：对象存储 endpoint 固定，跨请求复用连接与 TLS 握手。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout, connector=make_pinned_connector()
            )
        return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _signed(
        self,
        method: str,
        path: str,
        *,
        data: bytes = b"",
        content_type: str = "",
        expect=(200,),
    ) -> None:
        encoded_path = uri_encode_path(path)
        url = self.endpoint + encoded_path
        payload_sha256 = hashlib.sha256(data).hexdigest() if data else EMPTY_PAYLOAD_SHA256

        def prepare(current_url: str, current_method: str, cross_origin: bool = False) -> dict:
            # SigV4 签名与目标 URL、方法绑定，重定向后无法复用；此处直接报错，
            # 避免发出必然 403 的请求（cross_origin 用不上，因为不再跟随重定向）。
            if current_method != method or current_url != url:
                raise UrlBlockedError(
                    "对象存储请求被重定向，已拒绝在非预期目标上复用签名: "
                    f"{current_method} {current_url}"
                )
            amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            headers = {
                "User-Agent": USER_AGENT,
                "x-amz-date": amz_date,
                "x-amz-content-sha256": payload_sha256,
            }
            if content_type and current_method == "PUT":
                headers["Content-Type"] = content_type
            headers["Authorization"] = sign_request(
                current_method,
                current_url,
                amz_date,
                payload_sha256,
                self.access_key,
                self.secret_key,
                self.region,
                canonical_uri=encoded_path,
            )
            kwargs = {"headers": headers}
            if current_method in ("PUT", "POST"):
                kwargs["data"] = data
            return kwargs

        session = await self._get_session()
        async with await guarded_request(session, method, url, prepare=prepare) as resp:
            body = await read_limited_bytes(resp)
            if resp.status not in expect:
                raise RuntimeError(
                    f"对象存储返回 HTTP {resp.status}: {_s3_error_detail(body)!r}"
                )

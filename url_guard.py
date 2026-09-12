"""SSRF 防护工具。

插件自身发起的所有网络请求（图床上传、图片下载兜底）必须经过本模块：

- resolve_public_addrs 解析 URL 主机并校验：仅允许 http/https 协议，且主机
  解析到的全部 IP 不得指向环回、私有、链路本地、保留、多播等非公网地址，
  校验通过时把已确认公网的地址一并返回；
- guarded_request 在此基础上发送请求：禁用 aiohttp 自动重定向，手动逐跳跟随
  并对每一跳重新校验；校验得到的 IP 通过 PinnedResolver 钉扎给 aiohttp 的连接
  阶段，连接时不再二次解析域名，从而堵住“校验时解析到公网、连接时解析到内网”
  的 DNS rebinding 绕过。

使用本模块的会话必须由 make_pinned_connector() 创建连接器，否则 PinnedResolver
不会生效（此时仅剩逐跳校验，rebinding 仍可绕过）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextvars import ContextVar
from typing import Any, Dict, List, Mapping
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult


class UrlBlockedError(Exception):
    """URL 未通过安全校验。"""


class ResponseTooLargeError(Exception):
    """响应体超过允许的大小上限。"""


# 单次响应的默认读取上限：本插件的接口都是小型 JSON，1 MiB 足够，
# 避免异常或被劫持的端点用超大响应体耗尽内存。
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024

# 跨域重定向时强制剥离的请求头（prepare 回调之外的兜底保护）
_CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "api-key",
        "x-api-key",
        "token",
        "x-token",
        "x-auth-token",
        "cookie",
    }
)


def _check_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, host: str) -> None:
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global  # 覆盖 100.64.0.0/10(CGNAT)、文档地址段等"非全局可路由"地址
    ):
        raise UrlBlockedError(f"已拒绝指向非公网地址的请求: {host} -> {ip}")


def _url_port(parsed) -> int:
    """取 URL 端口；缺省按协议补全，非法端口统一转为 UrlBlockedError。"""
    try:
        port = parsed.port
    except ValueError as e:
        raise UrlBlockedError(f"URL 端口不合法: {parsed.netloc}: {e}") from e
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise UrlBlockedError(f"URL 端口超出范围: {port}")
    return port


def _url_origin(url: str) -> str:
    """归一化的源标识（小写主机 + 实际端口），用于判断是否跨域。"""
    parsed = urlparse(url)
    return f"{(parsed.hostname or '').lower()}:{_url_port(parsed)}"


def resolve_public_addrs(url: str) -> tuple[str, List[ResolveResult]]:
    """校验 URL 并解析主机。返回 (主机名, 已确认公网的地址列表)。

    主机为 IP 字面量时同样返回一份已校验地址，调用方据此钉扎——
    不依赖 aiohttp 对 IP 直连跳过 resolver 的内部实现行为，升级后
    仍保持"未钉扎即拒绝"的 fail-closed 语义。
    """
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise UrlBlockedError(f"URL 解析失败: {e}") from e

    if parsed.scheme not in ("http", "https"):
        raise UrlBlockedError(f"仅允许 http/https 协议，收到: {parsed.scheme!r}")

    host = parsed.hostname
    if not host:
        raise UrlBlockedError("URL 缺少主机名")

    # 端口合法性先于任何放行判断，避免 http://1.2.3.4:99999/ 之类绕过
    port = _url_port(parsed)

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        _check_ip(ip, host)
        return host, [
            ResolveResult(
                hostname=host,
                host=str(ip),
                port=port,
                family=socket.AF_INET6 if ip.version == 6 else socket.AF_INET,
                proto=socket.IPPROTO_TCP,
                flags=0,
            )
        ]

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as e:
        raise UrlBlockedError(f"域名解析失败: {host}: {e}") from e
    if not infos:
        raise UrlBlockedError(f"域名解析结果为空: {host}")

    addrs: List[ResolveResult] = []
    for family, _type, proto, _canon, sockaddr in infos:
        ip_str = str(sockaddr[0])
        try:
            resolved = ipaddress.ip_address(ip_str)
        except ValueError as e:
            raise UrlBlockedError(f"域名解析到非法地址: {host}") from e
        _check_ip(resolved, host)
        addrs.append(
            ResolveResult(
                hostname=host,
                host=ip_str,
                port=int(sockaddr[1]) if len(sockaddr) > 1 else port,
                family=family,
                proto=proto,
                flags=0,
            )
        )
    return host, addrs


# 当前请求钉扎的地址：{小写主机名: [已校验的 ResolveResult]}。
# 用 ContextVar 承载，使并发的不同请求互不干扰。
_PINS: ContextVar[Mapping[str, List[ResolveResult]]] = ContextVar("url_guard_pins", default={})


class PinnedResolver(AbstractResolver):
    """只返回本次请求已校验过的 IP；未钉扎的主机一律拒绝（fail-closed）。"""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_UNSPEC,
    ) -> List[ResolveResult]:
        addrs = _PINS.get().get(host.lower())
        if not addrs:
            raise OSError(f"目标主机未经过 SSRF 校验，已拒绝连接: {host}")
        return addrs

    async def close(self) -> None:
        return None


def make_pinned_connector(**kwargs) -> aiohttp.TCPConnector:
    """创建启用 IP 钉扎的连接器；插件所有出网会话都应使用它。

    关闭 aiohttp 自带的 DNS 缓存：每个新连接都重新走钉扎逻辑，连接池复用则
    只可能复用此前已校验过的地址。
    """
    kwargs.setdefault("use_dns_cache", False)
    return aiohttp.TCPConnector(resolver=PinnedResolver(), **kwargs)


def prepare_headers(prepare, url: str, method: str, cross_origin: bool) -> Dict[str, Any]:
    """调用 prepare 回调取得请求参数；跨域重定向时剥离凭据类请求头。

    prepare 契约：prepare(url, method, cross_origin) -> dict，其中
    cross_origin 为 True 表示当前跳与首跳不同源，回调自身也应据此不再
    附带鉴权信息（此处的剥离仅针对常见凭据头名做兜底）。
    """
    if prepare is None:
        return {}
    kwargs = dict(prepare(url, method, cross_origin) or {})
    if cross_origin:
        headers = kwargs.get("headers")
        if headers:
            kwargs["headers"] = {
                k: v for k, v in headers.items() if str(k).lower() not in _CREDENTIAL_HEADERS
            }
    return kwargs


async def read_limited_bytes(
    resp: aiohttp.ClientResponse, limit: int = DEFAULT_MAX_RESPONSE_BYTES
) -> bytes:
    """读取响应体字节，超过 limit 立即中止，避免异常端点撑爆内存。"""
    declared = resp.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > limit:
        raise ResponseTooLargeError(f"响应体声明长度 {declared} 超过上限 {limit} 字节")
    raw = await resp.content.read(limit + 1)
    if len(raw) > limit:
        raise ResponseTooLargeError(f"响应体超过上限 {limit} 字节")
    return raw


async def read_limited_text(
    resp: aiohttp.ClientResponse, limit: int = DEFAULT_MAX_RESPONSE_BYTES
) -> str:
    """按响应声明的字符集读取文本，超过 limit 立即中止。"""
    raw = await read_limited_bytes(resp, limit)
    try:
        encoding = resp.get_encoding() or "utf-8"
    except (LookupError, ValueError):
        encoding = "utf-8"
    return raw.decode(encoding, errors="replace")


REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


async def guarded_request(session, method: str, url: str, *, max_redirects: int = 3, prepare=None):
    """发起经 SSRF 校验的 HTTP 请求，手动跟随重定向并对每一跳重新校验。

    aiohttp 默认自动跟随重定向，只校验首跳 URL 挡不住
    "公网地址 302 跳内网" 的绕过，因此这里禁用自动重定向、逐跳校验；
    并且每一跳都把校验过的 IP 钉扎给连接阶段，防止 DNS rebinding。

    prepare: 可选回调 prepare(url, method, cross_origin) -> dict，用于生成
    每次请求的额外参数（如 FormData、headers）；重定向后方法变化或跨域时会
    重新生成，回调应据 cross_origin 决定是否附带鉴权信息。
    返回最终的（非重定向）响应对象，由调用方负责关闭。
    """
    origin = _url_origin(url)
    current = url
    for hop in range(max_redirects + 1):
        host, addrs = await asyncio.to_thread(resolve_public_addrs, current)
        cross_origin = _url_origin(current) != origin
        kwargs = prepare_headers(prepare, current, method, cross_origin)

        pins = dict(_PINS.get())
        if addrs:
            pins[host.lower()] = addrs
        token = _PINS.set(pins)
        try:
            resp = await session.request(method, current, allow_redirects=False, **kwargs)
        finally:
            _PINS.reset(token)

        if resp.status not in REDIRECT_STATUSES:
            return resp
        location = resp.headers.get("Location", "")
        resp.release()
        if not location or hop == max_redirects:
            raise UrlBlockedError(f"重定向无效或次数超过上限 {max_redirects}: {url}")
        current = urljoin(current, location)
        if resp.status in (301, 302, 303):
            method = "GET"

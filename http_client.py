"""Shared guarded HTTP client for plugin outbound requests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import aiohttp

try:
    from .common import USER_AGENT
    from .url_guard import guarded_request, make_pinned_connector
except ImportError:  # Compatible with direct module imports used by tests.
    from common import USER_AGENT  # type: ignore[no-redef]
    from url_guard import guarded_request, make_pinned_connector  # type: ignore[no-redef]


PrepareCallback = Callable[[str, str, bool], dict]


class GuardedHttpClient:
    """Own one pinned ``aiohttp`` session for all plugin HTTP requests."""

    def __init__(self, allowed_private_hosts: set[str] | None = None) -> None:
        self._session: aiohttp.ClientSession | None = None
        # 同一个集合对象由调用方（插件实例）持有并原地更新；请求时按实例白名单
        # 豁免私网地址，不依赖 url_guard 的模块级全局集合
        self.allowed_private_hosts = allowed_private_hosts

    async def request(
        self,
        method: str,
        url: str,
        *,
        prepare: PrepareCallback | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> aiohttp.ClientResponse:
        """Run a guarded request and return the final response context."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                connector=make_pinned_connector(),
            )

        def wrapped_prepare(current_url: str, current_method: str, cross_origin: bool) -> dict:
            kwargs = (
                dict(prepare(current_url, current_method, cross_origin) or {}) if prepare else {}
            )
            if timeout is not None:
                kwargs["timeout"] = timeout
            return kwargs

        return await guarded_request(
            self._session,
            method,
            url,
            prepare=wrapped_prepare,
            **(
                {"allowed_private_hosts": self.allowed_private_hosts}
                if self.allowed_private_hosts is not None
                else {}
            ),
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


HttpClientGetter = Callable[[], Awaitable[GuardedHttpClient]]

"""图片获取：图片段 → 本地路径 / 字节，以及 URL 存在性探测。

本模块只处理「图片段（鸭子类型：url / convert_to_file_path）或 HTTP URL」，
不引用 AstrBot 的事件/消息组件；事件级提取（消息链去重）仍留在插件边界。
所有出网请求都经由注入的 `http_getter` 拿到的客户端，SSRF 校验不在此重复。
"""

from __future__ import annotations

import asyncio
import io
import os
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp

from ..common import (
    DOWNLOAD_CHUNK_SIZE,
    DOWNLOAD_TIMEOUT,
    MEMORY_DOWNLOAD_MAX_BYTES,
    PROBE_TIMEOUT,
    is_qq_image_bed_host,
    read_bytes,
)
from ..features import ImageFeatures, heif_available
from ..image_delivery import (
    classify_probe_status,
    content_length_from_headers,
    is_scaling_capability_rejection,
    sniff_image_format,
)

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("image_source")


@dataclass
class ResolvedImage:
    """一次解析的产物：本地路径、临时文件标记、失败提示，以及可选的 MIME/特征。

    MIME 与 features 只在调用方要求的探测模式下填充。带 features 的解析表示
    “这一次解码已经完成格式校验 + 感知特征”，下游不得再为同一张图重复解码。
    """

    path: str = ""
    is_tmp: bool = False
    message: str = ""
    mime: str = ""
    features: ImageFeatures | None = None


def describe_bad_image(path: str) -> tuple[str, str | None]:
    """坏产物摘要 + 按文件头识别的容器格式，供日志与失败分诊使用。

    第二项为 None 表示「连图片容器都认不出」（错误体、HTML、空文件），非
    None 表示「容器认得出但解不开」（缺解码器、截断）——两者的处置完全不同：
    前者才值得重下，后者重下也是同一份内容。函数为同步 IO，调用方应走
    asyncio.to_thread。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(64)
        size = os.path.getsize(path)
    except OSError:
        return "无法读取", None
    text = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
    return f"{size} 字节, 头部 {text!r}", sniff_image_format(head)


def download_fail_message() -> str:
    return (
        "⚠️ 未能获取图片内容：图片链接可能已过期（QQ 图床签名失效）或被防盗链拦截，"
        "请重新发送、或引用该图片后再试。"
    )


def decode_fail_message(sniffed: str, *, heif_ready: bool | None = None) -> str:
    """认得出容器却解不开时的提示；HEIC 缺解码器是最常见的一种。

    heif_ready 由调用方注入（缺省读本模块的 heif_available），以便沿用
    「运行时探测解码器是否可用」的可测试性，而不是把结论写死。
    """
    ready = heif_available() if heif_ready is None else heif_ready
    if sniffed == "HEIF" and not ready:
        return (
            "⚠️ 检测到 HEIC/HEIF 图片（iPhone 默认格式），但服务端未安装 pillow-heif "
            "解码支持，无法溯源。安装该依赖后重启机器人即可。"
        )
    return (
        f"⚠️ 未能解析图片内容：识别为 {sniffed} 但解码失败，"
        "文件可能已损坏或格式不受当前服务端支持，详情见机器人日志。"
    )


def image_download_headers(u: str, _m: str, _c: bool) -> dict:
    """图片兜底下载的请求头。

    NTQQ（multimedia.nt.qq.com.cn）与旧版 gchat.qpic.cn 图床均启用了
    防盗链：缺 Referer 时会返回 403 或只有几十~几百字节的错误体。两者的
    防盗链规则按各自站点校验，因此 Referer 必须与图片同族——拿旧图床的
    Referer 去请求 NTQQ，反而会被判成跨站盗用。Referer 不是凭据，跨域
    重定向是否剥离交给 url_guard；重定向离开该域名后因 host 不匹配自然
    不再携带。
    """
    host = (urlparse(u).hostname or "").lower()
    if not is_qq_image_bed_host(host):
        return {}
    if host == "multimedia.nt.qq.com.cn" or host.endswith(".nt.qq.com.cn"):
        return {"headers": {"Referer": "https://multimedia.nt.qq.com.cn/"}}
    return {"headers": {"Referer": "https://gchat.qpic.cn/"}}


def range_probe_headers(u: str, m: str, c: bool) -> dict:
    """在下载请求头基础上加 Range: bytes=0-0，用于退化探测响应体长度。"""
    base = dict(image_download_headers(u, m, c))
    headers = dict(base.get("headers") or {})
    headers["Range"] = "bytes=0-0"
    base["headers"] = headers
    return base


class ImageSource:
    """把图片段解析为本地文件，或按 URL 下载/探测。

    依赖全部由调用方注入，便于脱离插件实例测试：
    - http_getter：返回已做 SSRF 约束的 HTTP 客户端（协程）；
    - tmp_dir：临时文件目录（下载产物写入此处）；
    - remove_file：删除临时文件的回调（静默失败）；
    - size_limit_bytes：回传兜底下载的大小上限（读取配置）；
    - on_scaled_unsupported：探测到图床拒绝缩放请求时的回调（记冷却）；
    - heif_ready：HEIC 解码器可用性探测（缺省读 features.heif_available）；
    - memory_limit_bytes：内存下载阈值（缺省读 MEMORY_DOWNLOAD_MAX_BYTES）。
    """

    def __init__(
        self,
        *,
        http_getter: Callable[[], Awaitable[aiohttp.ClientSession]],
        tmp_dir: str,
        remove_file: Callable[[str], None],
        size_limit_bytes: Callable[[], int],
        on_scaled_unsupported: Callable[[int], None] | None = None,
        heif_ready: Callable[[], bool] | None = None,
        memory_limit_bytes: Callable[[], int] | None = None,
    ) -> None:
        self._http_getter = http_getter
        self._tmp_dir = tmp_dir
        self._remove_file = remove_file
        self._size_limit_bytes = size_limit_bytes
        self._on_scaled_unsupported = on_scaled_unsupported or (lambda _status: None)
        self._heif_ready = heif_ready or heif_available
        self._memory_limit_bytes = memory_limit_bytes or (lambda: MEMORY_DOWNLOAD_MAX_BYTES)

    @staticmethod
    def _timeout() -> aiohttp.ClientTimeout:
        """兜底下载/格式体检统一的出网超时。"""
        return aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)

    async def stream_response_to_temp(self, resp, limit: int) -> str:
        """把已打开的响应流式写入临时文件，返回路径；超限或失败即删文件。

        临时文件名完全不受外部输入影响（固定扩展名），图片解码由 Pillow
        按内容判断，与扩展名无关。
        """
        path = os.path.join(self._tmp_dir, f"q_{secrets.token_hex(8)}.jpg")
        try:
            received = 0
            with open(path, "wb") as file:
                async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                    received += len(chunk)
                    if received > limit:
                        raise ValueError("图片超过大小上限")
                    file.write(chunk)
        except Exception:
            self._remove_file(path)
            raise
        return path

    async def download(self, url: str, *, size_limit: int | None = None) -> str:
        """下载到临时文件，返回路径。

        size_limit 供「分片上传」这类明确知道上限的调用方覆盖默认值：QQ 分片
        通道允许 200MB，而 max_download_mb（默认 20MB）是给回传兜底用的，按它
        截断会把本可以发出去的大图判成「超过大小上限」。缺省 None 时仍按配置。
        """
        client = await self._http_getter()
        limit = (
            size_limit
            if size_limit is not None and size_limit > 0
            else self._size_limit_bytes()
        )
        async with await client.request(
            "GET",
            url,
            # prepare 契约为三参（url, method, cross_origin），签名不符
            # 会在请求发出前就 TypeError
            prepare=image_download_headers,
            timeout=self._timeout(),
        ) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > limit:
                raise ValueError("图片超过大小上限")
            return await self.stream_response_to_temp(resp, limit)

    async def download_bytes(self, url: str) -> bytes:
        """下载到内存；声明体积超过内存阈值时退回落盘读回。

        回退交付只需要字节（压缩、发送都在内存里完成）。内存占用由两道闸门
        控制：声明长度超过 MEMORY_DOWNLOAD_MAX_BYTES 时改走落盘；声明长度未知
        （分块传输）时仍读内存，但受 max_download_mb 上限约束——阈值取 8MB 而非
        上限值，正是为了让「未知长度」这种情况也留在可控范围内（并发上限 3，
        最坏约 24MB）。大图分支直接复用当前已打开的响应落盘，不再发第二次 GET。
        """
        client = await self._http_getter()
        size_limit = self._size_limit_bytes()
        async with await client.request(
            "GET", url, prepare=image_download_headers, timeout=self._timeout()
        ) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > size_limit:
                raise ValueError("图片超过大小上限")
            if declared.isdigit() and int(declared) > self._memory_limit_bytes():
                # 大图不冒险占内存：复用当前响应落盘后读回
                path = await self.stream_response_to_temp(resp, size_limit)
                try:
                    return await asyncio.to_thread(read_bytes, path)
                finally:
                    self._remove_file(path)
            buffer = io.BytesIO()
            received = 0
            async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                received += len(chunk)
                if received > size_limit:
                    raise ValueError("图片超过大小上限")
                buffer.write(chunk)
            return buffer.getvalue()

    async def probe(
        self, url: str, cache: dict | None = None, *, allow_range: bool = True
    ) -> tuple[bool | None, int | None]:
        """轻量探测 URL 是否存在及其响应体长度，返回 (是否存在, 字节数)。

        两个用途：判定图床缩放 URL 是否真的比原图小（压缩标识的唯一一手
        证据），以及 /原图 按文件名拼出的直链是否真的存在于图床。HEAD 优先，
        不支持时退化为只取 0-0 一个字节的 Range GET（仅限不带缩放参数的 URL：
        ImgBed 明确拒绝「Range + 缩放」的组合，退化为 Range 只会白花一次请求）。
        任何网络异常都返回 (None, None)——调用方必须把「探测不出来」与「确定
        不存在」分开处理，网络抖动不该被当成「图床没有这张图」。
        """
        if cache is not None and url in cache:
            return cache[url]
        result = await self.probe_uncached(url, allow_range=allow_range)
        if cache is not None:
            cache[url] = result
        return result

    async def probe_uncached(
        self, url: str, *, allow_range: bool = True
    ) -> tuple[bool | None, int | None]:
        """探测实现。探测用 PROBE_TIMEOUT 而非下载超时：探测只读响应头，
        此前共用 30s 会让一次卡住的探测把交付拖到分钟级。

        缩放 URL（allow_range=False）的 HEAD 405 特殊处理：CloudFlare-ImgBed
        的缩放 API 只接受 GET 不接受 HEAD，405 不等于「缩放不支持」。
        遇到 405 时回退到 GET（不带 Range）再探一次，确认缩放功能是否真的不可用。
        """
        client = await self._http_getter()
        timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT)

        # ---------- 第一步：HEAD 探测 ----------
        head_status: int | None = None
        try:
            async with await client.request(
                "HEAD", url, prepare=image_download_headers, timeout=timeout
            ) as resp:
                head_status = resp.status
                # 405 先不记冷却：它可能只是 HEAD 方法不被允许，而不是缩放
                # 功能本身不可用，下面回退 GET 再确认。
                if (
                    not allow_range
                    and resp.status != 405
                    and is_scaling_capability_rejection(resp.status)
                ):
                    self._on_scaled_unsupported(resp.status)
                exists = classify_probe_status(resp.status)
                if exists is False:
                    return False, None
                length = content_length_from_headers(resp.headers)
                if exists is True and length is not None:
                    return True, length
                if not allow_range and resp.status != 405:
                    # 非 405 的缩放探测失败（400/501/网络问题等）：不回退，
                    # 400/501 已在上文记冷却，其余按「无法判定」处理。
                    return None, None
        except Exception as exc:
            logger.debug(f"HEAD 探测失败 {url}: {exc}")
            if not allow_range:
                return None, None

        # ---------- 缩放 URL 的 405 回退：GET（不带 Range） ----------
        if not allow_range and head_status == 405:
            try:
                async with await client.request(
                    "GET", url, prepare=image_download_headers, timeout=timeout
                ) as resp:
                    # 只读响应头，不读响应体
                    if is_scaling_capability_rejection(resp.status):
                        # GET 也被拒绝：这才是真的缩放能力缺失
                        self._on_scaled_unsupported(resp.status)
                        return None, None
                    exists = classify_probe_status(resp.status)
                    length = content_length_from_headers(resp.headers)
                    if exists is True and length is not None:
                        return True, length
                    return None, None
            except Exception as exc:
                logger.debug(f"缩放 URL GET 回退探测失败 {url}: {exc}")
                return None, None

        # ---------- 普通 URL 的 Range GET 回退 ----------
        try:
            async with await client.request(
                "GET", url, prepare=range_probe_headers, timeout=timeout
            ) as resp:
                # 只读响应头，绝不读响应体：Range 被忽略而返回 200 时，
                # 完整读下来就等于把整张图下载了一遍，探测也就失去了意义
                return classify_probe_status(resp.status), content_length_from_headers(
                    resp.headers
                )
        except Exception as exc:
            logger.debug(f"URL 探测失败（无法判定存在性）{url}: {exc}")
        return None, None

    async def resolve(self, seg, *, probe) -> ResolvedImage:
        """把图片段解析为本地文件，并顺带执行 probe 的一次解码。

        probe(path) 成功时返回 (features|None, mime)，无效时返回 None 或抛错
        （两者都按“无效”处理）。这样「格式校验」与「特征计算」可以共用一次
        Image.open()：需要特征的调用方传 compute_features_with_mime，只需要
        MIME 的调用方传 _probe_mime。

        解析失败时的分诊与旧实现一致：
        - 认不出任何图片容器（错误体/防盗链响应）→ 才走自带下载兜底；
        - 认得出容器却解不开（缺解码器的 HEIC、截断文件）→ 直接返回具体原因，
          不再为同一内容白发一次下载。
        """

        def probe_path(path: str, is_tmp: bool) -> ResolvedImage | None:
            try:
                result = probe(path)
            except Exception:
                return None
            if not result:
                return None
            features, mime = result
            return ResolvedImage(path=path, is_tmp=is_tmp, mime=mime or "", features=features)

        try:
            path = await seg.convert_to_file_path()
            if path and os.path.isfile(path):
                resolved = await asyncio.to_thread(probe_path, path, False)
                if resolved is not None:
                    return resolved
                summary, sniffed = await asyncio.to_thread(describe_bad_image, path)
                if sniffed is not None:
                    logger.warning(
                        f"内置解析产物无法解码（{summary}，容器格式 {sniffed}），"
                        "链接指向同一内容，跳过兜底下载"
                    )
                    return ResolvedImage(
                        message=decode_fail_message(sniffed, heif_ready=self._heif_ready())
                    )
                logger.warning(f"内置解析产物不是有效图片（{summary}），改走兜底下载")
        except Exception as e:
            logger.debug(f"convert_to_file_path 失败，尝试兜底下载: {e}")

        url = getattr(seg, "url", None)
        if url and str(url).startswith(("http://", "https://")):
            try:
                path = await self.download(str(url))
            except Exception as e:
                logger.warning(f"图片兜底下载失败: {e}")
                return ResolvedImage(message=download_fail_message())
            resolved = await asyncio.to_thread(probe_path, path, True)
            if resolved is not None:
                return resolved
            summary, _ = await asyncio.to_thread(describe_bad_image, path)
            logger.warning(
                f"兜底下载内容不是有效图片（{summary}），链接可能已过期或被防盗链拦截"
            )
            self._remove_file(path)
        return ResolvedImage(message=download_fail_message())

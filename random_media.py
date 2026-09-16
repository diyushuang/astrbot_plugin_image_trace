"""CloudFlare-ImgBed 随机图 API 客户端与纯函数工具。

数据流：取随机图配置（由 main 归一化后传入）→ 拼接随机接口地址
（GET {base_url}{api_endpoint}?type=url&form=json[&dir=][&content=]）
→ 经 GuardedHttpClient 请求 → 把响应归一为绝对媒体 URL 交回 main，
由 main 决定发送方式（图片走 _yield_delivery、视频走标准消息链）。

本模块只负责“取到一条通过校验的媒体直链”：不关心发送策略、不落地字节；
所有出网请求都走 http_client.GuardedHttpClient（SSRF 校验 + IP 钉扎 + 逐跳
校验），不自行创建 aiohttp 会话。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from urllib.parse import unquote, urlencode, urljoin, urlsplit

import aiohttp

try:
    from .http_client import GuardedHttpClient
    from .url_guard import read_limited_text
except ImportError:  # 兼容插件以独立模块方式加载
    from http_client import GuardedHttpClient  # type: ignore[no-redef]
    from url_guard import read_limited_text  # type: ignore[no-redef]

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("random_media")

# 随机接口默认路径（ImgBed 官方文档的默认值）
RANDOM_ENDPOINT_DEFAULT = "/random"
# 允许的内容类型，与 ImgBed /random 的 content 参数取值一致
CONTENT_TYPES = frozenset({"image", "video"})
# 单次响应体读取上限：随机接口只回一行 JSON/文本，1 MiB 足够；避免异常或
# 被劫持的端点用超大响应体耗尽内存（与 url_guard 的默认上限同量级）
MAX_RESPONSE_BYTES = 1024 * 1024

# URL 扩展名 → 媒体类型：/random 未显式声明类型时按扩展名兜底判断。
# 取值自 suijitu 插件迁移，覆盖常见图片/视频容器格式。
IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tif", ".tiff"}
)
VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".avi", ".mov", ".wmv", ".flv", ".mkv", ".webm", ".m4v", ".3gp", ".ts"}
)
# 两类扩展名的合并元组，供 _looks_like_media_reference 的 endswith 判定复用；
# 预先拼好避免每次调用都重建元组
MEDIA_EXTENSIONS = tuple(IMAGE_EXTENSIONS) + tuple(VIDEO_EXTENSIONS)

HttpGetter = Callable[[], Awaitable[GuardedHttpClient]]


class RandomMediaError(Exception):
    """随机图接口调用失败：含配置非法、网络错误、响应无法解析。"""


def _is_media_content_type(header: str) -> bool:
    """响应头是否表明直接返回了媒体（image/* 或 video/*）。"""
    return str(header or "").split(";", 1)[0].strip().lower().startswith(("image/", "video/"))


def build_random_api_url(base_url, endpoint, *, directory, content_type) -> str:
    """拼接随机图接口地址并做参数校验，非法配置抛 RandomMediaError。

    域名必须是合法 http(s)、不含账号密码/查询参数/片段，接口路径必须是相对
    路径：任一不满足都直接抛错，让调用方给出可读提示，而不是发出一个必然
    被图床拒绝的请求。directory 为空时不带 dir 参数（由图床按默认目录取图）；
    content_type 为 image/video 时带 content 参数。
    """
    domain = str(base_url or "").strip().rstrip("/")
    if not domain:
        raise RandomMediaError("未配置随机图图床地址 base_url")
    parsed_domain = urlsplit(domain)
    if parsed_domain.scheme not in {"http", "https"} or not parsed_domain.netloc:
        raise RandomMediaError("图床地址必须是有效的 http(s) 地址")
    if parsed_domain.username or parsed_domain.password:
        raise RandomMediaError("图床地址不能包含账号或密码")
    if parsed_domain.query or parsed_domain.fragment:
        raise RandomMediaError("图床地址不能包含查询参数或片段")

    path = str(endpoint or RANDOM_ENDPOINT_DEFAULT).strip() or RANDOM_ENDPOINT_DEFAULT
    parsed_endpoint = urlsplit(path)
    if parsed_endpoint.scheme or parsed_endpoint.netloc:
        raise RandomMediaError("随机图接口必须是相对路径")

    params = {"type": "url", "form": "json"}
    target_dir = str(directory or "").strip()
    if target_dir:
        params["dir"] = target_dir
    kind = str(content_type or "").strip().lower()
    if kind:
        if kind not in CONTENT_TYPES:
            raise RandomMediaError(f"不支持的内容类型: {content_type}")
        params["content"] = kind
    # domain 已校验不含查询串，故接口地址恒用 '?' 起头
    return f"{domain}/{path.lstrip('/')}?{urlencode(params)}"


def _looks_like_media_reference(value: str) -> bool:
    """判断一段文本是否真的像“链接或路径”，而不是错误页或占位文字。

    图床出错时同样可能回 200 + 一段纯文本（"server is busy"、HTML 错误页、
    JSON 错误详情等）。少了这道闸，urljoin 会把它拼成一条语法上完全合法的
    URL 当图片直链发进群，用户看到破图而不是可诊断的错误。

    放行官方 type=url 的三种形态：完整链接、站点相对路径（以 / 开头，含
    /file/noext 这类无扩展名的床内文件），以及不带前导斜杠的床内相对路径
    （要求带媒体扩展名，否则与普通文本无从区分）。
    """
    candidate = str(value or "").strip()
    if not candidate or any(char.isspace() for char in candidate):
        return False
    if candidate.startswith(("/", "http://", "https://")):
        return True
    return "/" in candidate and candidate.lower().endswith(MEDIA_EXTENSIONS)


def resolve_media_url(value, response_url) -> str | None:
    """把接口返回的媒体地址解析为绝对 URL；非法地址返回 None。

    相对路径以响应地址为基准拼接（urljoin）；仅接受合法 http(s) 且不含账号
    密码的地址，其余一律拒绝，避免把 file://、data: 或带凭据的诡异地址回传。
    拼接前先过 _looks_like_media_reference：错误页文本也能被 urljoin 拼成
    合法 URL，仅靠 scheme/netloc 校验拦不住。
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _looks_like_media_reference(candidate):
        return None
    media_url = urljoin(str(response_url or ""), candidate)
    parsed = urlsplit(media_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    return media_url


def parse_random_response(text, response_url, content_type_header) -> str | None:
    """把 /random 的响应解析为绝对媒体 URL，无法识别时返回 None。

    兼容多种返回形态：
    - 直接返回媒体（Content-Type 为 image/* 或 video/*，此时响应地址本身即
      媒体直链，可能经重定向）
    - JSON 对象：顶层 url / src / publicUrl，或 data.url / data.src
    - JSON 数组：取首个元素的 url / src / publicUrl（兼容部分版本的返回格式）
    - 纯文本 URL

    多形态并存是图床不同配置/版本的产物，收在一处兼容最省事。
    """
    if _is_media_content_type(content_type_header):
        return resolve_media_url(response_url, response_url)
    body = str(text or "").strip()
    if not body:
        return None

    # 先尝试 JSON
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        # 不是 JSON：当纯文本 URL 处理
        return resolve_media_url(body, response_url)

    # 候选字段：按优先级尝试
    candidate_fields = ("url", "src", "publicUrl")

    def _extract(obj) -> str | None:
        """从 dict 中按优先级提取媒体地址字段。"""
        if not isinstance(obj, dict):
            return None
        for field in candidate_fields:
            val = obj.get(field)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None

    # 顶层对象
    value = _extract(payload)
    if value:
        return resolve_media_url(value, response_url)

    # data.url / data.src 等嵌套结构
    data_val = payload.get("data")
    if isinstance(data_val, dict):
        value = _extract(data_val)
        if value:
            return resolve_media_url(value, response_url)

    # 数组形式：取第一个元素
    if isinstance(payload, list) and payload:
        value = _extract(payload[0])
        if value:
            return resolve_media_url(value, response_url)

    # 都没找到
    return None


def media_filename(url) -> str | None:
    """从媒体 URL 提取文件名；末段不含点号（不像文件名）时返回 None。"""
    try:
        path = unquote(urlsplit(str(url or "")).path)
    except Exception:
        return None
    segments = [segment for segment in path.split("/") if segment]
    if segments and "." in segments[-1]:
        return segments[-1]
    return None


def media_kind(url, hint=None) -> str | None:
    """判断媒体类型，返回 "image" / "video" / None。

    优先用调用方给的 hint：命令与 LLM 工具已声明要图片还是视频，应当以它为准
    （图床直链可能没有扩展名）；hint 无法识别时再按 URL 扩展名兜底。
    """
    normalized = str(hint or "").strip().lower()
    if normalized in CONTENT_TYPES:
        return normalized
    path = unquote(urlsplit(str(url or "")).path).lower()
    if any(path.endswith(extension) for extension in IMAGE_EXTENSIONS):
        return "image"
    if any(path.endswith(extension) for extension in VIDEO_EXTENSIONS):
        return "video"
    return None


def extract_directory(message: str) -> tuple[str | None, str | None]:
    """从命令或自然语言文本中解析 (目录, 内容类型)。

    两种写法都要认：命令式 `/随机图 风景` 与自然语言 `来张随机图 风景`；因此
    先按命令正则取“命令词后的剩余部分”，不匹配再退化为关键词搜索。目录为空串
    表示“未指定目录”，由客户端回退到配置的 default_dir。自 suijitu 迁移并按
    本插件命令名（随机图/随机视频）对齐。
    """
    if not message:
        return None, None
    text = re.sub(r"\s+", " ", message.strip())

    command_match = re.match(r"^/(随机图|随机图片|随机视频)(?:\s+(.*))?$", text)
    if command_match:
        keyword, directory = command_match.groups()
        return (directory or "").strip(), "video" if keyword == "随机视频" else "image"

    keyword_match = re.search(r"随机视频|随机图片?|随机影片", text)
    if not keyword_match:
        return None, None
    keyword = keyword_match.group(0)
    directory = text[keyword_match.end() :].strip(" \t:：,，")
    return directory, "video" if keyword in {"随机视频", "随机影片"} else "image"


class RandomMediaClient:
    """ImgBed 随机图接口客户端：把 /random 响应归一为绝对媒体 URL。

    走 GuardedHttpClient，失败按指数退避重试。403 直接抛错且不重试：它代表
    “站点未开启随机图功能”这一配置前置条件，重试无意义，交给用户开启后再试。
    settings 由调用方（main._random_settings）归一化后传入，本类不再解析原始
    配置。
    """

    def __init__(self, settings: dict, http_getter: HttpGetter):
        self.settings = settings if isinstance(settings, dict) else {}
        self._http_getter = http_getter

    async def fetch(self, directory=None, content_type=None) -> str:
        """请求一条随机媒体直链；失败抛 RandomMediaError。"""
        api_url = build_random_api_url(
            self.settings.get("base_url"),
            self.settings.get("api_endpoint"),
            directory=directory,
            content_type=content_type,
        )
        token = str(self.settings.get("api_token") or "").strip()
        if token and urlsplit(api_url).scheme != "https":
            # 图床若是 http，Token 会明文过网，宁可拒绝也不泄露
            raise RandomMediaError("配置随机图 Token 时图床地址必须使用 https")

        def prepare(_url: str, _method: str, cross_origin: bool) -> dict:
            headers: dict = {}
            # 跨域重定向会把请求交给第三方，鉴权信息一律不再附带
            if token and not cross_origin:
                headers["Authorization"] = f"Bearer {token}"
            return {"headers": headers}

        timeout = aiohttp.ClientTimeout(
            total=max(0.1, float(self.settings.get("timeout") or 10.0))
        )
        retry_count = int(self.settings.get("retry_count") or 0)
        client = await self._http_getter()
        last_error = "获取随机媒体失败"
        for attempt in range(retry_count + 1):
            try:
                async with await client.request(
                    "GET", api_url, prepare=prepare, timeout=timeout
                ) as resp:
                    if resp.status == 403:
                        logger.warning("随机图接口返回 403：图床站点可能未开启随机图功能")
                        raise RandomMediaError(
                            "图床返回 403：站点可能未开启随机图功能，请先在图床后台开启后再试"
                        )
                    if not 200 <= resp.status < 300:
                        last_error = f"图床返回 HTTP {resp.status}"
                        logger.warning(f"随机图请求失败: {last_error}")
                    else:
                        body_text = await self._read_body(resp)
                        media_url = parse_random_response(
                            body_text,
                            str(resp.url),
                            resp.headers.get("Content-Type", ""),
                        )
                        if media_url:
                            return media_url
                        last_error = "图床未返回有效的媒体地址"
                        logger.warning(f"随机图请求失败: {last_error}")
                        logger.debug(
                            f"随机图响应解析失败，响应体（前300字）: {body_text[:300]!r}"
                        )
            except RandomMediaError:
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                last_error = f"请求失败: {exc}"
                logger.warning(f"随机图第 {attempt + 1} 次请求失败: {exc}")
            except Exception as exc:  # 读取响应等未预期错误：记日志后决定是否重试
                last_error = f"处理响应失败: {exc}"
                logger.warning(f"随机图第 {attempt + 1} 次请求异常: {exc}")
            if attempt < retry_count:
                await asyncio.sleep(min(2**attempt, 8))
        raise RandomMediaError(last_error)

    @staticmethod
    async def _read_body(resp) -> str:
        """读取响应文本；直接返回媒体（image/*、video/*）时无需读体。

        直接媒体（如视频）响应体可能很大，先看 Content-Type 再决定是否读，
        避免被 read_limited_text 的大小上限误判为失败。
        """
        if _is_media_content_type(resp.headers.get("Content-Type", "")):
            return ""
        return await read_limited_text(resp, MAX_RESPONSE_BYTES)

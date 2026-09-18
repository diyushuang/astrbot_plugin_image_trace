"""图片回传与向量命中去重的纯函数工具。"""

from __future__ import annotations

import asyncio
import contextlib
import io
import math
from collections.abc import Mapping
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

try:
    from PIL import Image as PILImage
    from PIL import ImageOps as PILImageOps
except ImportError:  # Pillow 缺失时本地压缩自动降级为原字节
    PILImage = None
    PILImageOps = None


DELIVERY_MODES = frozenset({"scaled-url", "original-url", "local-compress"})
IMGBED_MANAGED_QUERY_KEYS = frozenset({"width", "height", "fit", "fallback"})
NAPCAT_PARSEABLE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
)
NAPCAT_PARSEABLE_FORMATS = frozenset({"JPEG", "PNG", "GIF", "WEBP", "BMP", "TIFF"})
COMPRESS_MIN_BYTES = 200 * 1024

# 探测结果里「确定不存在」的状态码：只有这两个语义明确。
_NOT_FOUND_STATUSES = frozenset({404, 410})
# 表示「该部署处理不了缩放请求」的 API 级拒绝（见 is_scaling_capability_rejection）。
_CAPABILITY_REJECT_STATUSES = frozenset({400, 405, 501})

# 本地压缩的质量阶梯：目标是「不超过指定体积」时的最低质量与最大尝试次数。
# 每次尝试都是一次完整编码，故上限 3 次（首次 + 两次降档）。
COMPRESS_MIN_QUALITY = 45
COMPRESS_QUALITY_STEP = 10
COMPRESS_MAX_ATTEMPTS = 3


class CompressionEvidence(str, Enum):
    """图片回传时的压缩证据。

    COMPRESSED / ORIGINAL 都有依据（实测字节更小，或已知尺寸不会被图床缩放）；
    UNKNOWN 表示「无法证明压缩过」——此时配文一律不写「已压缩」，宁可少说，
    也不能出现「文字说已压缩、用户收到的却是原图」。
    """

    COMPRESSED = "compressed"
    ORIGINAL = "original"
    UNKNOWN = "unknown"


class SendOutcome(str, Enum):
    """OneBot 直发结果。只有 FAILED 允许换通道重发。

    UNKNOWN 是最需要小心的一档：请求可能已经送达（超时等），此时任何「重发」
    都会让用户收到两张一模一样的图。
    """

    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


_TIMEOUT_ERRORS = (TimeoutError, asyncio.TimeoutError)

# 协议端明确回报「这条消息没发出去」的异常类名（aiocqhttp 的 ActionFailed 等）。
# 注意这只是「类名像明确失败」，不等于「语义是明确失败」——超时也常被包进
# ActionFailed，故超时线索必须排在本判断之前（见 classify_send_error）。
# NapCat 的 retcode=1200 目前正是靠 message 里的 `Timeout:` 线索判成 UNKNOWN 的；
# 若将来出现「空 message + retcode=1200」这种没有任何超时线索的形态，会落到这里
# 按类名判 FAILED（偏保守、允许一次重发），届时再评估是否为 1200 单独特判。
_DEFINITE_FAILURE_NAMES = frozenset({"ActionFailed", "ApiNotAvailable"})

# 「连接根本没建起来」类错误：请求没到协议端，重发安全
_UNCONNECTED_HINTS = (
    "not connected",
    "connection closed",
    "connection is closed",
    "connect call failed",
    "cannot connect",
    "no connection",
)

# 「语义是超时」的文案线索：协议端常把超时包在 ActionFailed 里——NapCat 实测
# 就是 retcode=1200、“Timeout: NTEvent ... sendMsg”。类名看着像明确失败，
# 语义其实是结果未知，所以超时线索必须优先于类名/retcode 判断，否则会误触发
# 回退、把同一张图发两遍。timedout 覆盖无空格的写法（Node 网络栈的 ETIMEDOUT）。
_TIMEOUT_HINTS = ("timeout", "timed out", "timedout", "time out", "超时")


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """读取并限制整数配置，非法值回退默认值。"""
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(maximum, result))


SCALED_URL_STYLES = frozenset({"query", "cf-path"})


def delivery_settings(config: Any) -> tuple[str, int, int, str]:
    """归一化图片回传配置，返回 (mode, max_side, quality, scaled_url_style)。

    默认 local-compress：QQ 协议端（NapCat 等）实测会剥离 URL 查询参数，
    图床缩放 URL 直传拿回的是原图；本地压缩后的字节经 base64 段直达 QQ，
    压缩 100% 生效，配文证据也是实测精确值。

    scaled_url_style：scaled-url 模式下缩放参数的 URL 风格：
    - query（默认）：查询参数式，如 /file/abc.jpg?width=1920&height=1920&fallback=original
      优点是兼容 CloudFlare-ImgBed 原生接口，且任何图床站点都能用；
      缺点是 QQ 协议端下载时可能剥离查询参数，导致 fallback=original 返回原图。
    - cf-path：Cloudflare Image Resizing 路径式，如
      /cdn-cgi/image/width=1920,height=1920,quality=85,fit=scale-down/file/abc.jpg
      优点是缩放参数嵌在路径里，QQ 协议端剥离不掉，图床→OneBot 流量一定
      是压缩后的；缺点是需图床域名开启 Cloudflare Image Resizing（付费）。
    """
    raw = config if isinstance(config, dict) else {}
    mode = str(raw.get("mode") or "local-compress").strip().lower()
    if mode not in DELIVERY_MODES:
        mode = "local-compress"
    max_side = bounded_int(raw.get("max_side"), 1920, 1, 4096)
    quality = bounded_int(raw.get("quality"), 85, 1, 100)
    style = str(raw.get("scaled_url_style") or "query").strip().lower()
    if style not in SCALED_URL_STYLES:
        style = "query"
    return mode, max_side, quality, style


def _error_text(exc: BaseException) -> str:
    """把异常可见文案拼成小写文本，供超时/未连接线索匹配。

    str(exc) 对 aiocqhttp 的 ActionFailed 通常已含完整消息体；再补上它的
    message 属性，避免个别版本 __str__ 只给简短摘要时漏掉真正的失败原因。

    本函数在 except 块里被调用，是「异常处理的异常处理」，因此对畸形异常必须
    绝对健壮：str(exc)、getattr(exc, "message") 与 str(message) 任一处抛异常
    （自定义 __str__、取值即抛的 property 等）都就地跳过该部分、退化为空串，
    绝不让异常向上传播、掩盖真实错误。两者都取不到时返回空串，不影响后续兜底。
    """
    parts: list[str] = []
    # str(exc) 若抛异常（自定义 __str__ 等）则跳过该部分，静默退化为无此段。
    with contextlib.suppress(Exception):
        parts.append(str(exc))
    try:
        # getattr 也放进 try：message 可能是取值即抛的 property，默认值兜不住。
        message = getattr(exc, "message", None)
        if message is not None:
            parts.append(str(message))
    except Exception:
        pass
    return " ".join(part for part in parts if part).strip().lower()


def classify_send_error(exc: BaseException) -> SendOutcome:
    """把 OneBot 直发异常归为「明确失败」或「结果未知」。

    只有明确失败才允许换通道重发：超时是最典型的结果未知——请求已经发到协议
    端，只是响应在回程丢了；若当成失败再发一次，用户就会收到两张一模一样的图
    （原图体积大、协议端还要自己下载图床 URL，最容易踩这条）。

    注意协议端常把超时包装成 ActionFailed（NapCat 实测：retcode=1200，消息体里
    写着 `Timeout: NTEvent ... sendMsg`）。这种异常若先按类名或 retcode 判，会
    得出「已明确失败」的相反结论，进而触发回退把同一张图发两遍。因此这里的顺序
    是「异常类型 → 消息内容里的超时线索 → 类名 → retcode → 未连接线索」：让语义
    压过类型名。retcode 只在非 0 时才当作失败——OneBot 里 0 表示成功，异常却带着
    成功码时说明状态不明，宁可判 UNKNOWN 也不再重发；比较前按字符串归一化，
    让 0 与 "0" 一视同仁，避免字符串码被误当成失败而触发重发。

    判定结果只有三态，不接受「差不多算失败」的模糊档：这是防「同一张图发两遍」
    的闸门，宁可少发也不可多发。想区分超时的具体成因请用 describe_send_error。
    """
    if isinstance(exc, _TIMEOUT_ERRORS):
        return SendOutcome.UNKNOWN
    message = _error_text(exc)
    if any(hint in message for hint in _TIMEOUT_HINTS):
        return SendOutcome.UNKNOWN
    if type(exc).__name__ in _DEFINITE_FAILURE_NAMES:
        return SendOutcome.FAILED
    retcode = getattr(exc, "retcode", None)
    if retcode is not None and str(retcode).strip() != "0":
        return SendOutcome.FAILED
    if any(hint in message for hint in _UNCONNECTED_HINTS):
        return SendOutcome.FAILED
    return SendOutcome.UNKNOWN


# 发送结果的可诊断细分。三态（SENT/FAILED/UNKNOWN）决定「要不要回退」，这个细分
# 只决定「日志怎么讲」。分开的理由：UNKNOWN 是最容易被误读成「什么都没发生」的
# 一档，而它其实意味着「请求已发出、只是没拿到回执」——把这个信息、成因与对应的
# 处置写清楚，日志才有排障价值，而不是留一句「未知」让人无从下手。
SEND_REASON_SENT = "sent"
SEND_REASON_TIMEOUT = "timeout"
SEND_REASON_PROTOCOL_REJECT = "protocol-reject"
SEND_REASON_DISCONNECTED = "disconnected"
SEND_REASON_UNCLASSIFIED = "unclassified"


def send_outcome_reason(exc: BaseException | None) -> str:
    """给出发送异常的具体成因档位（仅用于日志与诊断展示）。

    与 classify_send_error 共用同一套线索，但**不参与回退决策**，因此可以比
    三态更细：超时再按「带 retcode 的协议端回调超时」与「纯超时」分开，前者多
    见于 NapCat 的 sendMsg 回调窗口耗尽，后者是网络栈层面的超时。
    """
    if exc is None:
        return SEND_REASON_SENT
    message = _error_text(exc)
    retcode = getattr(exc, "retcode", None)
    if isinstance(exc, _TIMEOUT_ERRORS):
        return SEND_REASON_TIMEOUT
    if any(hint in message for hint in _TIMEOUT_HINTS):
        return SEND_REASON_TIMEOUT
    # 未连接线索排在类名之前：`ActionFailed("not connected")` 这类异常类名看着
    # 像协议端拒绝，语义其实是「连接没建起来」，两者排查方向完全不同。
    if any(hint in message for hint in _UNCONNECTED_HINTS):
        return SEND_REASON_DISCONNECTED
    if type(exc).__name__ in _DEFINITE_FAILURE_NAMES:
        return SEND_REASON_PROTOCOL_REJECT
    if retcode is not None and str(retcode).strip() != "0":
        return SEND_REASON_PROTOCOL_REJECT
    return SEND_REASON_UNCLASSIFIED


def send_diagnosis(exc: BaseException) -> str:
    """给出发送异常的一句话结论（含成因与建议动作），供日志直接输出。

    目的：把「结果未知」变成一条**有结论的日志**——讲清「请求已发出、未拿到
    回执、因此按不重发处理」，再附上成因与可操作的排查方向。超时这一档在此处
    是**正常且预期**的现象（大图 + 多图 + 协议端自行下载时最容易触发），不是
    插件缺陷，所以要明确写出来，避免每次都被误当成故障上报。
    """
    reason = send_outcome_reason(exc)
    message = _error_text(exc)
    retcode = getattr(exc, "retcode", None)
    detail = f"retcode={retcode}" if retcode is not None else "无 retcode"
    if reason == SEND_REASON_TIMEOUT:
        if any(hint in message for hint in _TIMEOUT_HINTS) and "sendmsg" in message.replace(" ", ""):
            # NapCat 的 sendMsg 等待 QQNT onMsgInfoListUpdate 回调超时：图片消息
            # 需要客户端把图上传到 QQ 服务器后才回调，图越大/越多越容易超窗口
            return (
                "发送未确认：协议端 sendMsg 等待客户端回调超时（图片已上传但回执超时，"
                "通常是图片较大或一次发送多张所致，非插件缺陷），已按「不重发」处理以免重复发图；"
                f"{detail}。若频繁出现：升级 NapCat 至 4.7.8+ 并重启 QQ，"
                "或调小 image_delivery.max_side / 开启 target_kb 降低单图体积"
            )
        return (
            "发送未确认：请求已发出但等待响应超时，已按「不重发」处理以免重复发图；"
            f"{detail}。若频繁出现请检查与协议端之间的网络与协议端负载"
        )
    if reason == SEND_REASON_DISCONNECTED:
        return f"发送失败：与协议端的连接不可用，已改走回退通道；{detail}"
    if reason == SEND_REASON_PROTOCOL_REJECT:
        return f"发送失败：协议端明确拒绝，已改走回退通道；{detail}"
    return (
        "发送未确认：异常未匹配到已知成因，保守按「不重发」处理以免重复发图；"
        f"{type(exc).__name__}; {detail}"
    )


_CF_IMAGE_PREFIX = "/cdn-cgi/image/"


def is_cloudflare_imgbed_url(url: str) -> bool:
    """按 CloudFlare-ImgBed 标准 /file/ 路径识别可缩放直链。

    同时识别 Cloudflare Image Resizing 路径式（/cdn-cgi/image/.../file/...），
    因为这种 URL 的源文件仍是 ImgBed /file/ 直链，只是缩放参数嵌在路径里。
    """
    try:
        parsed = urlsplit(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        path = unquote(parsed.path)
        if path.startswith("/file/"):
            return True
        if path.startswith(_CF_IMAGE_PREFIX):
            # /cdn-cgi/image/.../file/... 才算——源文件在 /file/ 下
            return "/file/" in path[len(_CF_IMAGE_PREFIX):]
        return False
    except Exception:
        return False


def _extract_imgbed_file_path(path: str) -> str | None:
    """从 ImgBed URL 路径中提取 /file/... 部分，识别不到返回 None。

    支持两种格式：
      /file/abc.jpg                    → /file/abc.jpg
      /cdn-cgi/image/opts/file/abc.jpg → /file/abc.jpg
    """
    p = unquote(path)
    if p.startswith("/file/"):
        return p
    if p.startswith(_CF_IMAGE_PREFIX):
        rest = p[len(_CF_IMAGE_PREFIX):]
        idx = rest.find("/file/")
        if idx >= 0:
            return rest[idx:]
    return None


def _requote_imgbed_path(file_path: str) -> str:
    """把解引号后的 /file/ 路径按 URL 规则重新编码。

    _extract_imgbed_file_path 为匹配方便返回解引号路径，重建 URL 前必须
    重新编码，否则文件名里的空格/中文/特殊字符会原样进入 URL，协议端会
    截断或拒收。
    """
    return quote(file_path, safe="/:")


def build_scaled_url(url: str, max_side: int, quality: int = 85, style: str = "query") -> str:
    """构造 ImgBed 等比缩放 URL；非 ImgBed URL 原样返回。

    style="query"（默认）：查询参数式，兼容 CloudFlare-ImgBed 原生接口。
    style="cf-path"：Cloudflare Image Resizing 路径式，参数嵌在路径里，
                     QQ 协议端剥离不掉，能保证压缩生效。
    """
    if not is_cloudflare_imgbed_url(url):
        return url
    side = bounded_int(max_side, 1920, 1, 4096)
    q = bounded_int(quality, 85, 1, 100)
    parsed = urlsplit(url)
    file_path = _extract_imgbed_file_path(parsed.path)
    if not file_path:
        return url

    if style == "cf-path":
        # Cloudflare Image Resizing 路径式：/cdn-cgi/image/width=W,height=W,quality=Q,fit=scale-down/file/...
        options = f"width={side},height={side},quality={q},fit=scale-down"
        new_path = f"{_CF_IMAGE_PREFIX.rstrip('/')}/{options}{_requote_imgbed_path(file_path)}"
        return urlunsplit((parsed.scheme, parsed.netloc, new_path, parsed.query, parsed.fragment))

    # query 风格：在查询串里追加 width/height/fallback
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in IMGBED_MANAGED_QUERY_KEYS
    ]
    query.extend([("width", str(side)), ("height", str(side)), ("fallback", "original")])
    return urlunsplit(
        (parsed.scheme, parsed.netloc, _requote_imgbed_path(file_path), urlencode(query), parsed.fragment)
    )


def build_original_url(url: str) -> str:
    """还原 ImgBed 读取 API 意义上的“未处理原文件”直链。

    与 build_scaled_url 对称：
    - query 风格：剥离 width/height/fit/fallback 查询参数
    - cf-path 风格：剥离 /cdn-cgi/image/... 前缀，还原为 /file/ 直链

    非 ImgBed 直链原样返回。
    """
    if not is_cloudflare_imgbed_url(url):
        return url
    parsed = urlsplit(url)
    file_path = _extract_imgbed_file_path(parsed.path)
    if not file_path:
        return url
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in IMGBED_MANAGED_QUERY_KEYS
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, _requote_imgbed_path(file_path), urlencode(query), parsed.fragment)
    )


def is_scaling_needed(image_size, max_side) -> bool | None:
    """该图是否需要图床缩放：True 需要、False 不需要、None 尺寸未知。

    尺寸未知时返回 None 而不是 False：调用方必须能区分「确定不用缩放」与
    「不知道」——前者可以省掉缩放参数（图床不会放大，追加参数只是白白触发
    一次处理），后者只能照旧追加参数且不宣称压缩。
    """
    try:
        width, height = int(image_size[0]), int(image_size[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if width <= 0 or height <= 0:
        return None
    return max(width, height) > bounded_int(max_side, 1920, 1, 4096)


def compression_evidence(
    image_size, max_side, *, verified_smaller: bool | None = None
) -> CompressionEvidence:
    """判定回传这张图「已压缩 / 就是原图 / 说不准」。

    verified_smaller 是唯一的一手证据（实测缩放版字节数确实更小）。没有它时
    只能按尺寸推断：不超过 max_side 的图不会被图床放大，ImgBed 按
    fallback=original 原样返回，可以确定是原图；超过 max_side 或尺寸未知则
    无从判断，返回 UNKNOWN，配文据此不再写「已压缩」。
    """
    if verified_smaller is not None:
        return CompressionEvidence.COMPRESSED if verified_smaller else CompressionEvidence.ORIGINAL
    if is_scaling_needed(image_size, max_side) is False:
        return CompressionEvidence.ORIGINAL
    return CompressionEvidence.UNKNOWN


def build_imgbed_file_url(base_url: str, file_name: str) -> str | None:
    """按 CloudFlare-ImgBed 公开直链口径拼出 `{base}/file/{文件名}`。

    供 /原图 在会话历史未命中时直接按文件名到图床取原图。文件名允许带目录
    （如 `2026/09/abc.jpg`），但拒绝一切可能拼出跨站或路径穿越的输入：绝对
    URL、`..`、`//`、空名一律返回 None。拼出的结果还必须落回 ImgBed 的
    /file/ 口径，避免 base 配错时发出一条语法合法却指向别处的链接。
    """
    base = str(base_url or "").strip().rstrip("/")
    name = str(file_name or "").strip().strip("/")
    if not base or not name or ".." in name or "//" in name:
        return None
    parsed = urlsplit(name)
    if parsed.scheme or parsed.netloc:
        return None
    candidate = f"{base}/file/{quote(unquote(name), safe='/')}"
    return candidate if is_cloudflare_imgbed_url(candidate) else None


def is_napcat_parseable_url(url: str) -> bool:
    """判断 URL 扩展名对应的格式能否被 QQ 协议端解析出宽高。"""
    try:
        path = unquote(urlsplit(str(url or "")).path).lower()
    except Exception:
        return False
    return any(path.endswith(extension) for extension in NAPCAT_PARSEABLE_EXTENSIONS)


# 内联（base64）字节段的上限：与下载口径 MEMORY_DOWNLOAD_MAX_BYTES 对齐，
# 超过这个体积的 base64 消息有撑爆 websocket 帧的风险，改走 URL 段。
MAX_INLINE_IMAGE_BYTES = 8 * 1024 * 1024


def prefer_url_direct(block: Mapping) -> bool:
    """已知小图是否值得跳过下载、直接以 URL 段发送。

    与 prepare_image_bytes 的跳压缩阈值对齐：体积已知且 ≤ COMPRESS_MIN_BYTES
    的图本地压缩本就会被跳过，下载再 base64 回发纯属浪费一次往返 + 33% 膨胀，
    不如让协议端直接拉小文件。体积未知（0/缺字段）不享受该优化——万一是
    大图，URL 直传会绕过压缩。
    """
    try:
        size_bytes = int(block.get("size_bytes") or 0)
    except (TypeError, ValueError):
        return False
    url = str(block.get("url") or "")
    return 0 < size_bytes <= COMPRESS_MIN_BYTES and is_napcat_parseable_url(url)


def prefer_inline_bytes(
    data_len: int,
    compressed: bool,
    url: str,
    *,
    max_inline_bytes: int = MAX_INLINE_IMAGE_BYTES,
) -> bool:
    """拿到字节后决定发 base64 段还是 URL 段。

    压缩成功的字节必须内联——这是 local-compress 模式的全部意义，不能
    交给协议端二次下载。未压缩的字节仅在内联安全（≤上限）或 URL 不可用
    （无 URL / 协议端解析不了宽高）时内联；未压缩且超上限的巨型动图等
    交给 URL 段，避免单条消息过大。
    """
    if compressed or not url or not is_napcat_parseable_url(url):
        return True
    return data_len <= max_inline_bytes


def upgrade_to_https(url: str, base_url: str) -> str:
    """把与 base 同域的 http 直链升级为 https，其余原样返回。

    OneBot 直传前减少一次 301 跳转：部分协议端在跳转时会丢失查询参数或被
    CDN 拒绝，直接给 https 更稳。仅当 base 本身是 https 且两者同域（主机名与
    端口一致）时才升级，避免把第三方 http 地址误改。
    """
    try:
        base = urlsplit(str(base_url or ""))
        target = urlsplit(str(url or ""))
    except Exception:
        return url
    if (
        base.scheme == "https"
        and target.scheme == "http"
        and target.netloc
        and target.netloc.lower() == base.netloc.lower()
    ):
        return urlunsplit(("https", target.netloc, target.path, target.query, target.fragment))
    return url


def classify_probe_status(status: int | None) -> bool | None:
    """HTTP 状态码 → 资源是否存在：2xx/206 存在、404/410 不存在、其余 None。

    只有「确定不存在」才返回 False，且仅限 404/410 这两个语义明确的码。此前把
    所有 4xx 都判成「不存在」，于是 403（防盗链/访问规则拒绝）、429（限流）、
    405（方法不允许）都被当成「图床里没有这张图」——`/原图` 于文件明明存在时
    误报未找到，缩放探测也会误判成资源缺失。这些状态只能说明「这次没读到」，
    代表不了资源的存在性，一律归入 None（说不准）。
    """
    if status is None:
        return None
    if 200 <= status < 300:
        return True
    if status in _NOT_FOUND_STATUSES:
        return False
    return None


def is_scaling_capability_rejection(status: int | None) -> bool:
    """该状态码是否表示「当前部署处理不了缩放请求」。

    对应 CloudFlare-ImgBed Read API 的 API 级拒绝：405（带缩放参数的请求只接受
    GET）、501（未配置图片处理器）、400（缩放参数与 Range 同时使用等参数组合
    非法）。这类拒绝是**部署能力**问题，不是单张图的问题，因此调用方可以据此
    在一段时间内不再探测缩放版。
    """
    return status is not None and int(status) in _CAPABILITY_REJECT_STATUSES


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    """大小写不敏感地取响应头字段（aiohttp 的 CIMultiDict 与测试用普通 dict 都认）。"""
    for key in (name, name.lower(), name.upper()):
        try:
            value = headers.get(key)
        except Exception:
            return None
        if value is not None:
            return str(value).strip()
    return None


def content_length_from_headers(headers: Mapping[str, str] | None) -> int | None:
    """从响应头取响应体总长度：Content-Length 优先，206 退化解析 Content-Range。

    探测走 HEAD（无响应体）或 `Range: bytes=0-0` 的单字节 GET，前者靠
    Content-Length，后者靠 Content-Range 的 `bytes 0-0/总长`。拿不到长度时
    返回 None 表示「无法判定」——绝不能把 0 当成长度，否则会把「探测不出」
    误判成「缩放版更小」。
    """
    if headers is None:
        return None
    declared = _header_value(headers, "Content-Length")
    if declared is not None and declared.isdigit():
        return int(declared)
    content_range = _header_value(headers, "Content-Range")
    if content_range and "/" in content_range:
        size = content_range.rsplit("/", 1)[1].strip()
        if size.isdigit():
            return int(size)
    return None


def sniff_image_format(data: bytes) -> str | None:
    """按文件头识别图片格式，用于本地回退日志与压缩决策。"""
    if len(data) < 12:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if data.startswith(b"BM"):
        return "BMP"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand.startswith(b"av"):
            return "AVIF"
        if brand in (b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
            return "HEIF"
    stripped = data.lstrip(b" \t\r\n")
    if stripped.startswith((b"<svg", b"<?xml")):
        return "SVG"
    return None


def prepare_image_bytes(
    data: bytes,
    max_side: int,
    quality: int,
    *,
    target_bytes: int = 0,
    use_webp: bool = False,
) -> tuple[bytes, bool] | None:
    """压缩静态图；返回 (字节, 是否压缩)，失败返回 None。

    target_bytes > 0 时启用质量阶梯：首次按 quality 编码，仍超目标体积就按
    COMPRESS_QUALITY_STEP 逐档降质量重编，最多 COMPRESS_MAX_ATTEMPTS 次，下限
    COMPRESS_MIN_QUALITY。每档都是一次完整编码，故次数有上限——用 CPU 换体积，
    只在明确设了目标体积时才启用。

    use_webp 切换输出格式为 WebP（体积通常比同质量 JPEG 小 25%~60%，编码耗时
    则高一个量级），仅对本地压缩路径生效；动图仍原样返回，不重编码。
    """
    if PILImage is None:
        return None
    try:
        with PILImage.open(io.BytesIO(data)) as image:
            if getattr(image, "is_animated", False):
                return data, False
            original_format = (image.format or "").upper()
            if len(data) <= COMPRESS_MIN_BYTES and original_format in NAPCAT_PARSEABLE_FORMATS:
                return data, False
            image = PILImageOps.exif_transpose(image)
            side = bounded_int(max_side, 1920, 1, 4096)
            width, height = image.size
            if max(width, height) > side:
                scale = side / max(width, height)
                image = image.resize((round(width * scale), round(height * scale)), PILImage.LANCZOS)
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                background = PILImage.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            compressed = _encode_image(image, quality, target_bytes=target_bytes, use_webp=use_webp)
    except Exception:
        return None
    if len(compressed) >= len(data) and original_format in NAPCAT_PARSEABLE_FORMATS:
        return data, False
    return compressed, True


def _encode_image(image, quality: int, *, target_bytes: int, use_webp: bool) -> bytes:
    """按质量阶梯编码；返回最后一次编码结果（体积最小的一档未必是最后一次）。"""
    current = bounded_int(quality, 85, 1, 100)
    target = max(0, int(target_bytes or 0))
    best: bytes | None = None
    for _attempt in range(COMPRESS_MAX_ATTEMPTS):
        output = io.BytesIO()
        if use_webp:
            # method=4 在编码耗时与压缩率之间取平衡；method 越高越慢
            image.save(output, format="WEBP", quality=current, method=4)
        else:
            image.save(output, format="JPEG", quality=current, optimize=True)
        candidate = output.getvalue()
        if best is None or len(candidate) < len(best):
            best = candidate
        if not target or len(candidate) <= target or current <= COMPRESS_MIN_QUALITY:
            break
        current = max(COMPRESS_MIN_QUALITY, current - COMPRESS_QUALITY_STEP)
    return best if best is not None else b""


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算两个向量的余弦相似度；维度不一致或零向量返回 0。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def _normalized_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower() if parsed.hostname else ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port is not None and port != (443 if scheme == "https" else 80):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((scheme, netloc, parsed.path or "/", query, ""))


def _exact_hit_key(hit: dict) -> tuple[str, str] | None:
    payload = hit.get("payload") or {}
    src = str(payload.get("src") or "").strip()
    if src:
        return ("src", src)
    image_url = _normalized_url(str(payload.get("image_url") or ""))
    if image_url:
        return ("url", image_url)
    return None


def deduplicate_vector_hits(hits: list[dict], threshold: float = 0.995) -> list[dict]:
    """合并相同 URL/src 与向量近似相同的命中，保留相似度最高者。"""
    ordered = sorted(hits, key=lambda hit: float(hit.get("score") or 0.0), reverse=True)
    result: list[dict] = []
    exact_index: dict[tuple[str, str], int] = {}
    for hit in ordered:
        exact_key = _exact_hit_key(hit)
        if exact_key is not None and exact_key in exact_index:
            representative = result[exact_index[exact_key]]
            representative["duplicate_count"] = int(representative.get("duplicate_count") or 1) + 1
            representative.setdefault("duplicates", []).append(hit)
            continue

        merged = False
        vector = hit.get("vector")
        if isinstance(vector, list) and vector:
            for representative in result:
                representative_vector = representative.get("vector")
                if not isinstance(representative_vector, list) or not representative_vector:
                    continue
                if cosine_similarity(vector, representative_vector) >= threshold:
                    representative["duplicate_count"] = (
                        int(representative.get("duplicate_count") or 1) + 1
                    )
                    representative.setdefault("duplicates", []).append(hit)
                    merged = True
                    break
        if merged:
            continue

        item = dict(hit)
        item["duplicate_count"] = 1
        item["duplicates"] = []
        if exact_key is not None:
            exact_index[exact_key] = len(result)
        result.append(item)
    return result

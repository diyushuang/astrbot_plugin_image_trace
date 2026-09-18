"""插件内部共享的小工具与常量。

只放无业务语义的通用件（配置解析、User-Agent、常量），供 main /
image_bed / vector_search / s3_store 取用，避免同一份真值表与解析
逻辑在各模块各写一份。
"""

from __future__ import annotations

from typing import Any

USER_AGENT = "AstrBot-Plugin-ImageTrace/1.0"

# 查询图兜底下载的超时（秒）。vector_search / image_bed 的请求超时各自
# 可配置，下载兜底路径没有独立配置项，用固定值即可。
DOWNLOAD_TIMEOUT = 30

# 下载兜底路径的分块大小（字节）
DOWNLOAD_CHUNK_SIZE = 65536

# 探测（HEAD / Range）专用超时（秒）。探测只读响应头，与「下载整张图」的
# DOWNLOAD_TIMEOUT 完全不是一回事：此前两者共用 30s，一次卡住的探测就能把
# 整次交付拖到分钟级。自建图床实时缩放较慢时 5s 仍有余量。
PROBE_TIMEOUT = 5

# 单次回传计划的探测总预算（秒）。超出预算仍未返回的探测一律按「无法判定」
# 处理（配文不写「已压缩」），绝不让探测无限期拖住图片发送。
PLAN_TOTAL_TIMEOUT = 8

# 探测并发上限。图床多为自建单机，并发过高会挤占其处理能力；
# 4 足够把一批图的探测压到约一轮往返。
PROBE_CONCURRENCY = 4

# 探测到「图床当前处理不了缩放请求」（API 级拒绝，如缩放请求非 GET 的 405、
# 未配置图片处理器的 501）后的冷却时间（秒）。冷却期内不再为「已压缩」证据
# 探测缩放版，避免每次交付都白花一次请求。
SCALED_VERIFY_COOLDOWN_SECONDS = 600

# 本地压缩并发上限。Pillow 编解码期间释放 GIL，线程并行确有收益；
# 上限用于压住低配机器的 CPU 峰值。
COMPRESS_CONCURRENCY = 3

# 回退下载优先走内存的上限（字节）：超过则落盘，避免多张大图并发时内存峰值过高。
MEMORY_DOWNLOAD_MAX_BYTES = 8 * 1024 * 1024

# OneBot image 段的 timeout 字段（秒）：URL 段由协议端（NapCat 等）自行下载，
# 该值即协议端下载网络图片的超时。OneBot v11 规范里此参数默认「不超时」，但
# NapCat 实际另有 sendMsg 内部回调窗口；大图/多图时显式放宽可减少回调超时
# （表现为 retcode 1200 的 sendMsg Timeout，结果不确定因而不敢重发）。
# base64 段不经下载，带该字段无副作用。
ONEBOT_IMAGE_TIMEOUT = 60

# 临时文件最长保留时间（秒），超过后在插件加载/卸载时清理
TMP_MAX_AGE_SECONDS = 86400

# AI 复核最多送审的候选数
AI_VERIFY_MAX_CANDIDATES = 3

# 重扫批量入库的批次大小（单事务条数）
RESCAN_BATCH_SIZE = 500

# 重扫的进度汇报间隔（张数）
RESCAN_PROGRESS_EVERY = 200

# 字符串型布尔值的真值表；"0"/"false"/"no"/"off" 等一律按假处理
_TRUTHY = frozenset({"1", "true", "yes", "on", "开"})


def is_qq_image_bed_host(host: str) -> bool:
    """精确匹配 QQ 图床根域，避免伪造后缀误触发 Referer。"""
    normalized = (host or "").lower().rstrip(".")
    return normalized in {
        "multimedia.nt.qq.com.cn",
        "gchat.qpic.cn",
    } or normalized.endswith((".nt.qq.com.cn", ".qpic.cn"))


def truthy(value: Any) -> bool:
    """按布尔语义解释配置值：bool 原样返回，其余查真值表。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def is_blank(value: Any) -> bool:
    """未配置：None 或空白字符串。0 / False 是合法取值，不能按未配置处理。"""
    return value is None or (isinstance(value, str) and not value.strip())


def as_int(value: Any, default: int) -> int:
    """配置取整型；未配置或类型不合法时返回默认值。"""
    if is_blank(value):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float) -> float:
    """配置取浮点型；未配置或类型不合法时返回默认值。"""
    if is_blank(value):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

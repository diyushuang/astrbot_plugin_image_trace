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

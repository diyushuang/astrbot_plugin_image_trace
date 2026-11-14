"""URL / JSON 相关纯函数：图床直链拼接、嵌套取值与同源判定。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote


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


def public_url(base: str, key: str) -> str:
    """拼接公开直链：base 去尾斜杠 + URI 编码后的对象名。"""
    return f"{base.rstrip('/')}/{quote(key)}"


def host_port(parsed) -> tuple[str, int | None]:
    """URL 的归一化 (主机名小写, 端口)，端口缺省按协议补全。"""
    try:
        port = parsed.port
    except ValueError:
        return "", None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return (parsed.hostname or "").lower(), port


def same_origin(a, b) -> bool:
    """两个 parsed URL 是否同源（协议 + 主机 + 端口）。"""
    if a.scheme != b.scheme:
        return False
    return host_port(a) == host_port(b)

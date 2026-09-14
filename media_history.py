"""会话级原图历史（纯内存、无 IO）。

记录每个会话已回传图片的“原图直链”，供 /原图 命令按文件名找回。主键是 URL
文件名；当调用方给出的 display_name 与 URL 文件名不同时（/溯源 的向量命中
file_name 常与图床 CDN 直链的文件名不一致），额外登记一个别名键，使用户在
群里看到的那个名字也能命中。数据仅在事件循环内单线程读写，无需加锁。
"""

from __future__ import annotations

from collections import OrderedDict

try:
    from .random_media import media_filename
except ImportError:  # 兼容插件以独立模块方式加载
    from random_media import media_filename  # type: ignore[no-redef]

# 每个会话最多保留的历史键数 / 最多保留的会话数：纯内存缓存，防止长期运行下
# 无限增长；超出后按 LRU 淘汰最早的条目（会话级收敛）
MAX_PER_SESSION = 30
MAX_SESSIONS = 200

# 未提供会话标识时的兜底键，保证所有无会话场景归入同一分组而非丢弃
DEFAULT_SESSION = "default"


class MediaHistory:
    """会话隔离的 LRU 原图历史。"""

    def __init__(
        self, max_per_session: int = MAX_PER_SESSION, max_sessions: int = MAX_SESSIONS
    ) -> None:
        self._history: OrderedDict[str, OrderedDict[str, str]] = OrderedDict()
        self._max_per_session = max_per_session
        self._max_sessions = max_sessions

    @staticmethod
    def _key(session) -> str:
        return str(session or DEFAULT_SESSION)

    def remember(self, session, url, display_name=None) -> None:
        """记录一条原图直链；同键去重后移到末尾（LRU），超限裁剪。

        以 URL 文件名为主键；display_name 与主键不同名时额外登记别名键，使
        /溯源 回传的 file_name 也能被 /原图 命中。两个键都指向同一 URL。
        """
        target = str(url or "").strip()
        if not target:
            return
        primary = media_filename(target)
        alias = str(display_name or "").strip()
        keys: list[str] = [primary] if primary else []
        if alias and (not primary or alias.lower() != primary.lower()):
            keys.append(alias)
        if not keys:
            return

        history = self._history.setdefault(self._key(session), OrderedDict())
        for key in keys:
            # 先删后插：同键重记等价于把该键移到末尾，实现 LRU 语义
            history.pop(key, None)
            history[key] = target
        while len(history) > self._max_per_session:
            history.popitem(last=False)
        while len(self._history) > self._max_sessions:
            self._history.popitem(last=False)

    def latest(self, session) -> tuple[str, str] | None:
        """最近记录的一条 (名称, 原图直链)；该会话无记录时返回 None。"""
        history = self._history.get(self._key(session))
        if not history:
            return None
        name = next(reversed(history))
        return name, history[name]

    def find(self, session, query) -> tuple[str, object]:
        """按名称查找，返回 ("found" | "ambiguous" | "missing", 载荷)。

        匹配顺序：精确名（忽略大小写）→ 同名不同扩展（仅当结果唯一）→ 唯一
        子串。后两档命中多条时返回 "ambiguous" 并要求用户写出更完整的名字，
        避免“猜一张”造成误发。载荷：found 为 (名称, URL)，ambiguous 为候选名
        列表，missing 为 None。
        """
        history = self._history.get(self._key(session))
        if not history:
            return "missing", None
        needle = str(query or "").strip().lower()
        if not needle:
            return "missing", None
        for name, url in history.items():
            if name.lower() == needle:
                return "found", (name, url)

        stem = needle.rsplit(".", 1)[0]
        stem_matches = [
            (name, url)
            for name, url in history.items()
            if name.rsplit(".", 1)[0].lower() == stem
        ]
        if len(stem_matches) == 1:
            return "found", stem_matches[0]
        if stem_matches:
            return "ambiguous", [name for name, _ in stem_matches]

        substring_matches = [
            (name, url) for name, url in history.items() if needle in name.lower()
        ]
        if len(substring_matches) == 1:
            return "found", substring_matches[0]
        if substring_matches:
            return "ambiguous", [name for name, _ in substring_matches]
        return "missing", None

    def count(self, session) -> int:
        """该会话当前记录的历史键数（0 表示尚未回传过任何图片）。"""
        return len(self._history.get(self._key(session)) or {})

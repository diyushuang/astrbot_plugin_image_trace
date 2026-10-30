"""每日签到状态存储（SQLite，按用户全局计一次/天）。

为什么单独一个库：签到是业务状态，与图片库（pHash 索引）没有任何关联；塞进
`library.db` 会让「重建图片索引」与「签到记录」共享同一个迁移版本号，任一侧
改表都要另一个模块跟着回归。独立 `checkin.db` 只存一张表，升级/回滚都不影响
图片库。

并发约定与 library 一致：连接 `check_same_thread=False` + 单锁，调用方经
`asyncio.to_thread` 执行；本类内部不做任何异步。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkin (
    user_id    TEXT PRIMARY KEY,
    last_date  TEXT NOT NULL,
    streak     INTEGER NOT NULL DEFAULT 1,
    total      INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class CheckinOutcome:
    """一次签到判定的结果。already 为真时 streak/total 是当天已有值。"""

    already: bool
    streak: int
    total: int
    last_date: str


class CheckinStore:
    """按用户记录最近签到日、连续天数与累计天数。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def check_in(self, user_id: str, today: date) -> CheckinOutcome:
        """登记一次签到并返回判定结果。

        日期比较全用 `date.isoformat()` 字符串：调用方已按配置时区算好 today，
        本模块不猜时区。最后一次签到是昨天 → 连续天数 +1，否则从 1 重新计。
        同一天重复调用只读不写，保证并发下也只有一次「首次签到」。
        """
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id 不能为空")
        day = today.isoformat()
        yesterday = (today - timedelta(days=1)).isoformat()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with self._lock:
            row = self._conn.execute(
                "SELECT last_date, streak, total FROM checkin WHERE user_id = ?",
                (uid,),
            ).fetchone()
            if row is not None and str(row["last_date"]) == day:
                return CheckinOutcome(True, int(row["streak"]), int(row["total"]), day)

            streak = (
                int(row["streak"]) + 1
                if row is not None and str(row["last_date"]) == yesterday
                else 1
            )
            total = (int(row["total"]) if row is not None else 0) + 1
            self._conn.execute(
                """
                INSERT INTO checkin (user_id, last_date, streak, total, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_date = excluded.last_date,
                    streak = excluded.streak,
                    total = excluded.total,
                    updated_at = excluded.updated_at
                """,
                (uid, day, streak, total, now),
            )
            self._conn.commit()
        return CheckinOutcome(False, streak, total, day)

    def close(self) -> None:
        """关闭连接；重复调用安全。"""
        with self._lock, contextlib.suppress(Exception):
            self._conn.close()

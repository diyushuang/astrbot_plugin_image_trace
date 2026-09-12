"""图库存储与相似度检索。

SQLite 保存元数据；哈希十六进制串解码为 numpy 位矩阵常驻内存，
检索时用 XOR + 查表 popcount 批量计算汉明距离，纯内存操作，
数万张图的检索耗时在毫秒级。

并发约定：写路径持锁，内存缓存以单个 tuple 整体替换（Python 属性赋值
原子），读路径先取快照再计算——读写方在任意线程交错都不会读到
"新 ids + 旧矩阵"的错代组合。main 侧的重操作应经 asyncio.to_thread 调用。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

_POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.int64)

_SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phash TEXT NOT NULL,
    dhash TEXT NOT NULL,
    ahash TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    file_size INTEGER,
    image_url TEXT,
    file_path TEXT,
    source TEXT,
    note TEXT,
    group_id TEXT,
    sender_id TEXT,
    sender_name TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_images_phash ON images (phash);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_INSERT_SQL = (
    "INSERT OR IGNORE INTO images"
    " (phash, dhash, ahash, width, height, file_size,"
    " image_url, file_path, source, note, group_id, sender_id,"
    " sender_name, created_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


@dataclass
class MatchResult:
    id: int
    similarity: float
    image_url: Optional[str]
    file_path: Optional[str]
    note: Optional[str]
    source: Optional[str]
    width: Optional[int]
    height: Optional[int]
    created_at: Optional[str]


def _under_any(path: str, dirs: set) -> bool:
    """path 是否位于 dirs 中任一目录之下（含大小写/分隔符归一化）。"""
    for d in dirs:
        try:
            if os.path.commonpath([path, d]) == d:
                return True
        except ValueError:  # Windows 跨盘符等无法比较根的情形
            continue
    return False


class ImageLibrary:
    """本地图库：SQLite 元数据 + 内存哈希位矩阵。"""

    def __init__(self, db_path: str, expected_phash_hex_len: Optional[int] = None):
        self.db_path = db_path
        # 与当前 hash_size 匹配的 pHash 十六进制长度（统一由
        # features.phash_hex_len 计算）；不匹配的旧数据留在库中但不参与检索
        self.expected_phash_hex_len = expected_phash_hex_len
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._migrate()
        self._cache = ([], np.zeros((0, 0), dtype=np.uint8), 0, 0)
        self._reload_cache()

    # ---------- 内部工具 ----------

    def _migrate(self) -> None:
        """库结构版本迁移（须在持锁状态下调用）。

        v1 -> v2：v1 的 phash 无唯一约束，重复登记/并发重扫可能产生重复行；
        迁移时每个 phash 保留最早一条，再建唯一索引使后续写入天然幂等。
        """
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        version = int(row["value"]) if row else 1
        if version >= _SCHEMA_VERSION:
            return
        if version < 2:
            self._conn.execute(
                "DELETE FROM images WHERE id NOT IN"
                " (SELECT MIN(id) FROM images GROUP BY phash)"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_images_phash_unique"
                " ON images (phash)"
            )
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        self._conn.commit()

    def _reload_cache(self) -> None:
        """从数据库重建内存哈希矩阵（整体替换，读侧取快照）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, phash FROM images ORDER BY id"
            ).fetchall()
        ids: list = []
        raw_parts: list = []
        skipped = 0
        for row in rows:
            hex_str = row["phash"]
            if self.expected_phash_hex_len and len(hex_str) != self.expected_phash_hex_len:
                skipped += 1
                continue
            try:
                raw_parts.append(bytes.fromhex(hex_str))
            except ValueError:
                skipped += 1
                continue
            ids.append(row["id"])
        if raw_parts:
            matrix = np.frombuffer(b"".join(raw_parts), dtype=np.uint8).reshape(len(ids), -1)
        else:
            matrix = np.zeros((0, 0), dtype=np.uint8)
        bits = matrix.shape[1] * 8
        # 单个 tuple 一次赋值：读侧拿到的是完整一致的一代缓存
        self._cache = (ids, matrix, bits, skipped)

    @staticmethod
    def _hamming(cache: tuple, phash_hex: str) -> np.ndarray:
        """查询哈希与全库哈希的汉明距离；位数不一致的行记为最大距离。"""
        ids, matrix, bits, _skipped = cache
        n = len(ids)
        try:
            query = np.frombuffer(bytes.fromhex(phash_hex), dtype=np.uint8)
        except ValueError:
            return np.full(n, bits, dtype=np.int64)
        if matrix.size == 0 or query.size != matrix.shape[1]:
            return np.full(n, bits, dtype=np.int64)
        xor = np.bitwise_xor(matrix, query[None, :])
        return _POPCOUNT[xor].sum(axis=1)

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _row_params(row: dict, created_at: str) -> tuple:
        """把一条待插入记录整理成 SQL 参数（缺省字段留空）。"""
        return (
            row.get("phash", ""),
            row.get("dhash", ""),
            row.get("ahash", ""),
            row.get("width"),
            row.get("height"),
            row.get("file_size"),
            row.get("image_url", ""),
            row.get("file_path", ""),
            row.get("source", "scan"),
            row.get("note", ""),
            row.get("group_id", ""),
            row.get("sender_id", ""),
            row.get("sender_name", ""),
            created_at,
        )

    # ---------- 写入 ----------

    def add_if_absent(self, row: dict) -> tuple:
        """锁内完成"查重 + 插入"，返回 (entry_id, 重复行或 None)。

        供登记原图使用：检查与插入在同一个锁区间内完成，调用方即便在
        两次调用之间有网络 await，也不会与并发同图登记产生重复行；
        配合 phash 唯一索引，跨线程/跨进程的重复写入也会被静默忽略。
        """
        params = self._row_params(row, self._now())
        with self._lock:
            dup = self._conn.execute(
                "SELECT * FROM images WHERE phash = ? LIMIT 1", (row.get("phash", ""),)
            ).fetchone()
            if dup is not None:
                return None, dup
            cursor = self._conn.execute(_INSERT_SQL, params)
            self._conn.commit()
            entry_id = int(cursor.lastrowid)
        self._reload_cache()
        return entry_id, None

    def add_many(
        self,
        rows: list,
        *,
        batch_size: int = 500,
        reload_cache: bool = True,
    ) -> int:
        """批量插入记录（每 batch_size 条提交一次），返回实际插入条数。

        rows 为 dict 列表，字段同 add_if_absent 的 row 参数。逐条插入会产生
        N 次提交与 N 次内存索引重建，扫描大目录时既慢又会长时间占用事件
        循环；批量写入把提交次数降到 O(N/batch_size)。已存在的 phash
        （INSERT OR IGNORE）不计入返回值。
        """
        if not rows:
            return 0
        created_at = self._now()
        params = [self._row_params(r, created_at) for r in rows]
        inserted = 0
        with self._lock:
            for start in range(0, len(params), max(1, batch_size)):
                cursor = self._conn.executemany(_INSERT_SQL, params[start:start + batch_size])
                self._conn.commit()
                inserted += max(0, cursor.rowcount)
        if reload_cache:
            self._reload_cache()
        return inserted

    def reload_cache(self) -> None:
        """重建内存哈希矩阵；批量插入完成后调用一次即可。"""
        self._reload_cache()

    def delete(self, entry_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM images WHERE id = ?", (entry_id,))
            self._conn.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            self._reload_cache()
        return deleted

    def delete_by_source(self, source: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM images WHERE source = ?", (source,)
            )
            self._conn.commit()
            deleted = cursor.rowcount
        if deleted:
            self._reload_cache()
        return deleted

    def prune_scan_missing(self, keep_paths: set, scanned_dirs: Optional[set] = None) -> int:
        """删除来源为 scan、文件路径不在 keep_paths 中的条目，返回删除数量。

        scanned_dirs 限定清理范围：只处理 file_path 落在这些目录之下的
        条目。目录临时不可见（网络盘/移动盘掉线）时该目录不会进入
        scanned_dirs，其中的索引不会被误清，恢复后无需全量重扫。
        """
        keep = {os.path.normcase(p) for p in keep_paths}
        dirs = {os.path.normcase(d) for d in (scanned_dirs or set())}
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, file_path FROM images WHERE source = 'scan'"
            ).fetchall()
            stale = []
            for r in rows:
                fp = os.path.normcase(r["file_path"] or "")
                if fp in keep:
                    continue
                if scanned_dirs is not None and not _under_any(fp, dirs):
                    continue
                stale.append(r["id"])
            for entry_id in stale:
                self._conn.execute("DELETE FROM images WHERE id = ?", (entry_id,))
            self._conn.commit()
        if stale:
            self._reload_cache()
        return len(stale)

    # ---------- 查询 ----------

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM images").fetchone()
        return int(row["c"]) if row else 0

    def stats(self) -> dict:
        ids, _matrix, _bits, skipped = self._cache
        with self._lock:
            total_row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM images"
            ).fetchone()
            source_rows = self._conn.execute(
                "SELECT source, COUNT(*) AS c FROM images GROUP BY source"
            ).fetchall()
        return {
            "total": int(total_row["c"]) if total_row else 0,
            "by_source": {r["source"] or "": int(r["c"]) for r in source_rows},
            "indexed": len(ids),
            "skipped": skipped,
        }

    def get(self, entry_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM images WHERE id = ?", (entry_id,)
            ).fetchone()

    def get_many(self, entry_ids: list) -> list:
        """按 id 批量取回整行（检索 top-N 回表用，避免逐条查询）。"""
        if not entry_ids:
            return []
        placeholders = ",".join("?" for _ in entry_ids)
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM images WHERE id IN ({placeholders})",
                tuple(entry_ids),
            ).fetchall()

    def find_by_phash(self, phash: str) -> Optional[sqlite3.Row]:
        """按 pHash 精确查找（用于登记去重的快速预检）。"""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM images WHERE phash = ? LIMIT 1", (phash,)
            ).fetchone()

    def get_paths_for_source(self, source: str) -> set:
        with self._lock:
            rows = self._conn.execute(
                "SELECT file_path FROM images WHERE source = ?", (source,)
            ).fetchall()
        return {r["file_path"] for r in rows if r["file_path"]}

    def get_all_phash_hashes(self) -> set:
        """全部 pHash 集合；批量扫描时用于在内存中去重，避免逐张查库。"""
        with self._lock:
            rows = self._conn.execute("SELECT phash FROM images").fetchall()
        return {r["phash"] for r in rows if r["phash"]}

    def search(self, phash: str, limit: int = 5) -> list:
        """按 pHash 汉明距离检索，返回按相似度降序排列的结果。

        limit <= 0 时同样返回最多 1 条（与历史行为一致，调用方目前
        传入的 limit 恒为正数）。
        """
        cache = self._cache  # 快照：重建期间读到的也是完整一致的一代
        ids, _matrix, bits, _skipped = cache
        if not ids:
            return []
        dists = self._hamming(cache, phash)
        order = np.argsort(dists, kind="stable")[: max(1, limit)]
        wanted = [ids[int(idx)] for idx in order]
        by_id = {r["id"]: r for r in self.get_many(wanted)}
        results: list = []
        for idx, entry_id in zip(order, wanted):
            row = by_id.get(entry_id)
            if row is None:
                continue
            results.append(
                MatchResult(
                    id=int(row["id"]),
                    similarity=1.0 - float(dists[idx]) / max(1, bits),
                    image_url=row["image_url"],
                    file_path=row["file_path"],
                    note=row["note"],
                    source=row["source"],
                    width=row["width"],
                    height=row["height"],
                    created_at=row["created_at"],
                )
            )
        return results

    def close(self) -> None:
        with self._lock:
            self._conn.close()

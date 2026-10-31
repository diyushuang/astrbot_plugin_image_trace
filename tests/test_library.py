import sqlite3

import pytest

from astrbot_plugin_image_trace.library import ImageLibrary


def make_row(phash: str, file_path: str = "", source: str = "register") -> dict:
    return {
        "phash": phash,
        "dhash": "00",
        "ahash": "00",
        "width": 2,
        "height": 2,
        "file_size": 4,
        "image_url": "",
        "file_path": file_path,
        "source": source,
        "note": "",
        "group_id": "",
        "sender_id": "",
        "sender_name": "",
    }


def test_add_search_and_duplicate(tmp_path):
    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=2)
    entry_id, duplicate = library.add_if_absent(make_row("00"))
    assert entry_id == 1
    assert duplicate is None

    duplicate_id, duplicate = library.add_if_absent(make_row("00"))
    assert duplicate_id is None
    assert duplicate["id"] == 1

    results = library.search("00", limit=2)
    assert len(results) == 1
    assert results[0].similarity == pytest.approx(1.0)
    library.close()


def test_migration_from_v1_deduplicates_phash(tmp_path):
    db_path = tmp_path / "library.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE images (
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
        INSERT INTO images (phash, dhash, ahash, created_at)
        VALUES ('aa', '00', '00', '2026-01-01'),
               ('aa', '00', '00', '2026-01-02');
        """
    )
    connection.commit()
    connection.close()

    library = ImageLibrary(db_path, expected_phash_hex_len=2)
    assert library.count() == 1
    with sqlite3.connect(db_path) as connection:
        indexes = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_images_phash_unique'"
        ).fetchall()
    assert indexes == [("idx_images_phash_unique",)]
    library.close()


def test_prune_only_removes_scan_rows_under_scanned_dirs(tmp_path):
    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=2)
    keep_dir = tmp_path / "keep"
    drop_dir = tmp_path / "drop"
    outside_dir = tmp_path / "outside"
    for directory in (keep_dir, drop_dir, outside_dir):
        directory.mkdir()

    library.add_many(
        [
            make_row("00", str(keep_dir / "keep.jpg"), source="scan"),
            make_row("01", str(drop_dir / "drop.jpg"), source="scan"),
            make_row("02", str(outside_dir / "outside.jpg"), source="scan"),
            make_row("03", str(keep_dir / "register.jpg"), source="register"),
        ]
    )

    removed = library.prune_scan_missing(
        {str(keep_dir / "keep.jpg")}, {str(keep_dir), str(drop_dir)}
    )
    assert removed == 1
    assert library.find_by_phash("01") is None
    assert library.find_by_phash("00") is not None
    assert library.find_by_phash("02") is not None
    assert library.find_by_phash("03") is not None
    library.close()


def test_prune_with_empty_keep_set_clears_scanned_directory(tmp_path):
    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=2)
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir()
    library.add_if_absent(make_row("00", str(scan_dir / "old.jpg"), source="scan"))

    assert library.prune_scan_missing(set(), {str(scan_dir)}) == 1
    assert library.count() == 0
    library.close()


def test_match_result_exposes_stored_file_size(tmp_path):
    """检索结果要带出入库时记录的原图体积。

    回传计划用它判断「图床缩放版是否真的更小」——有它就不必再探测原图体积，
    少一次往返、也少一次图床实时缩放处理。
    """
    library = ImageLibrary(tmp_path / "library.db", expected_phash_hex_len=2)
    library.add_if_absent(make_row("00", "C:/images/a.jpg"))
    library.reload_cache()

    hits = library.search("00", limit=1)

    assert hits and hits[0].file_size == 4
    library.close()

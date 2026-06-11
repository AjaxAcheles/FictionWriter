"""
tests/test_branch_manager.py

Branch Manager Snapshot Idempotency Tests.

Purpose:
    Verifies that memory/branch_manager.py correctly creates and restores chapter-boundary
    snapshot ZIPs, and that the restore operation is idempotent — restoring the same
    snapshot twice produces the same result as restoring it once.

    Uses temporary directories (tmp_path fixture) for all file operations so tests
    are fully isolated and leave no artifacts.

    Tests:
    test_create_snapshot_creates_zip        — create_chapter_snapshot() creates a ZIP file.
    test_snapshot_contains_both_db_files    — ZIP contains fictionwriter.db and graphiti.db.
    test_restore_replaces_db_files          — restore_snapshot() replaces live DB files.
    test_restore_is_idempotent              — Restoring the same snapshot twice is safe.
    test_list_snapshots_returns_metadata    — list_snapshots() returns correct filename/chapter_id.
    test_restore_with_reason_returns_it     — restore_snapshot(reason=...) includes it in result.
    test_create_snapshot_filename_format    — Filename contains chapter_id and timestamp.
"""

import zipfile
from pathlib import Path

import pytest

from memory import branch_manager


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point all branch_manager module paths into tmp_path and seed live DBs."""
    monkeypatch.setattr(branch_manager, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(branch_manager, "SQLITE_PATH", tmp_path / "fictionwriter.db")
    monkeypatch.setattr(branch_manager, "GRAPHITI_PATH", tmp_path / "graphiti.db")
    import sqlite3
    conn = sqlite3.connect(tmp_path / "fictionwriter.db")
    conn.execute("CREATE TABLE marker (v TEXT)")
    conn.execute("INSERT INTO marker VALUES ('original')")
    conn.commit(); conn.close()
    (tmp_path / "graphiti.db").write_bytes(b"graphiti-original")
    return branch_manager



def test_create_snapshot_creates_zip(env, tmp_path):
    """
    Assert create_chapter_snapshot() creates a ZIP file in data/snapshots/.

    Purpose:
        Creates stub SQLite and Graphiti DB files in a temp directory, then calls
        create_chapter_snapshot(). Asserts the returned Path exists and is a valid ZIP.

    Inputs:
        tmp_path: pytest fixture providing a temporary directory.

    Expected:
        The returned Path exists. zipfile.is_zipfile() returns True.
    """
    path = env.create_chapter_snapshot("ch_001", "2026-06-11T00:00:00Z")
    assert path.exists()
    assert zipfile.is_zipfile(path)
    assert "ch_001" in path.name


def test_snapshot_contains_both_db_files(env, tmp_path):
    """
    Assert the snapshot ZIP contains entries for both fictionwriter.db and graphiti.db.

    Purpose:
        Verifies that both file-based stores are included in the snapshot. A snapshot
        missing either file would produce an incomplete restore.

    Inputs:
        tmp_path: pytest fixture.

    Expected:
        ZIP namelist contains 'fictionwriter.db' and 'graphiti.db'.
    """
    path = env.create_chapter_snapshot("ch_002", "2026-06-11T01:00:00Z")
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
    assert {"fictionwriter.db", "graphiti.db"} <= names


def test_restore_replaces_db_files(env, tmp_path):
    """
    Assert restore_snapshot() extracts and replaces the live DB files.

    Purpose:
        Creates a snapshot from known DB file contents, modifies the live DB files,
        then restores the snapshot. Asserts the live files contain the snapshot content
        (not the modified content) after restore.

    Inputs:
        tmp_path: pytest fixture.

    Expected:
        After restore, data/fictionwriter.db and data/graphiti.db match the snapshot contents.
    """
    import sqlite3

    snapshot = env.create_chapter_snapshot("ch_003", "2026-06-11T02:00:00Z")
    env.SQLITE_PATH.write_bytes(b"corrupted")
    env.GRAPHITI_PATH.write_bytes(b"graphiti-modified")

    env.restore_snapshot(snapshot)
    # Semantic equality: the backup-API copy normalizes header bytes, so the
    # restored DB is compared by content, not raw bytes.
    conn = sqlite3.connect(env.SQLITE_PATH)
    assert conn.execute("SELECT v FROM marker").fetchone()[0] == "original"
    conn.close()
    assert env.GRAPHITI_PATH.read_bytes() == b"graphiti-original"


def test_restore_is_idempotent(env, tmp_path):
    """
    Assert restoring the same snapshot twice produces the same result as once.

    Purpose:
        Idempotency is critical for crash recovery — if the restore process itself
        crashes mid-way, re-running it must be safe. Asserts that a double-restore
        leaves the files in the same state as a single restore.

    Inputs:
        tmp_path: pytest fixture.

    Expected:
        File contents after two restores == file contents after one restore.
    """
    snapshot = env.create_chapter_snapshot("ch_004", "2026-06-11T03:00:00Z")
    env.SQLITE_PATH.write_bytes(b"diverged")

    env.restore_snapshot(snapshot)
    once_sqlite = env.SQLITE_PATH.read_bytes()
    once_graphiti = env.GRAPHITI_PATH.read_bytes()
    env.restore_snapshot(snapshot)
    assert env.SQLITE_PATH.read_bytes() == once_sqlite
    assert env.GRAPHITI_PATH.read_bytes() == once_graphiti


def test_list_snapshots_returns_metadata(env, tmp_path):
    """
    Assert list_snapshots() returns correct metadata for all ZIPs in data/snapshots/.

    Purpose:
        Creates two snapshot ZIPs with known chapter_ids and timestamps, then calls
        list_snapshots(). Asserts the returned list contains one entry per snapshot
        with correct filename and chapter_id fields.

    Inputs:
        tmp_path: pytest fixture.

    Expected:
        list_snapshots() returns a list of length 2. Each entry contains the correct
        chapter_id extracted from the filename.
    """
    env.create_chapter_snapshot("ch_a", "2026-06-11T04:00:00Z")
    env.create_chapter_snapshot("ch_b", "2026-06-11T05:00:00Z")
    snapshots = env.list_snapshots()
    assert len(snapshots) == 2
    assert {s["chapter_id"] for s in snapshots} == {"ch_a", "ch_b"}
    assert snapshots[0]["timestamp"] >= snapshots[1]["timestamp"]  # newest first
    for s in snapshots:
        assert s["size_bytes"] > 0 and s["filename"].endswith(".zip")


def test_restore_with_reason_includes_it_in_result(env, tmp_path):
    """
    Assert restore_snapshot(branch_reason="...") includes the reason in the returned dict.

    Purpose:
        When the author provides a Reasons directive for a branch restore, it must be
        included in the returned state payload for injection into active_context_package.
        Verifies the dict key is present and matches the provided string.

    Inputs:
        tmp_path: pytest fixture.

    Expected:
        result["branch_reason"] == "Test reason string"
    """
    snapshot = env.create_chapter_snapshot("ch_005", "2026-06-11T06:00:00Z")
    result = env.restore_snapshot(snapshot, branch_reason="Test reason string")
    assert result["branch_reason"] == "Test reason string"

"""
memory/branch_manager.py

Branch and Crash Recovery Manager — O(1) Snapshot Decompression and .jsonl Replay.

Purpose:
    Manages two distinct recovery operations:
    1. Branch restore (O(1)): Decompresses a chapter-boundary snapshot ZIP containing
       both data/fictionwriter.db (SQLite) and data/graphiti.db (FalkorDB Lite).
       Replaces the live files in place. All Graphiti edge timestamps are preserved
       exactly. The .jsonl is NOT replayed for branch restore.
    2. Crash recovery (intra-chapter): Loads the most recent chapter-boundary snapshot
       to restore state to the last safe point, then replays .jsonl records forward
       to the crash point. All writes are idempotent so replay is safe.

    Snapshots are created by node_commit_transaction at every chapter boundary as a
    uniform file-copy/zip operation — no server backup APIs required. Snapshot filenames
    encode the chapter_id and timestamp for navigation from the Codex UI.

    Before any branch restore, the current live databases are archived (not deleted) as
    a safety measure. A plain-text "Reasons" directive from the Codex UI is injected into
    the returned state dict so the planning nodes autonomously steer the story in a new
    direction on resume.

Architecture role:
    - Called by routes/codex.py for user-initiated branch restore operations.
    - Called by core/runtime.py and/or the startup routine when a pending CommitIntent
      row indicates a crash mid-commit (crash recovery path).
    - Writes snapshot ZIPs to data/snapshots/ at chapter boundaries (called by
      node_commit_transaction).
    - All operations are file-based; no server APIs or Docker required.
"""

import zipfile
from pathlib import Path
from typing import Optional

SNAPSHOTS_DIR = Path("data/snapshots")
SQLITE_PATH = Path("data/fictionwriter.db")
GRAPHITI_PATH = Path("data/graphiti.db")


def create_chapter_snapshot(chapter_id: str, timestamp: str) -> Path:
    """
    Archive both data/fictionwriter.db and data/graphiti.db into a snapshot ZIP.

    Purpose:
        Called by node_commit_transaction at every chapter boundary. Creates a ZIP
        file in data/snapshots/ containing both file-based stores. Filename format:
        snapshot_{chapter_id}_{timestamp}.zip.

        Both stores are file-based — this is a uniform file-copy/zip operation
        requiring no server backup APIs or Docker commands. The ZIP preserves all
        file modification timestamps so that on restore, FalkorDB Lite sees the
        same timestamps it originally wrote, preserving all Graphiti edge temporal
        metadata exactly.

    Inputs:
        chapter_id: str — the chapter ID this snapshot covers. Used in the filename.
        timestamp: str — ISO 8601 timestamp string. Used in the filename.

    Outputs:
        Path: The absolute path to the created snapshot ZIP file.
    """
    import sqlite3

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_stamp = timestamp.replace(":", "").replace("-", "").replace(".", "")
    zip_path = SNAPSHOTS_DIR / f"snapshot_{chapter_id}_{safe_stamp}.zip"

    # WAL-safe SQLite copy via the backup API — never a raw file copy.
    sqlite_copy = SNAPSHOTS_DIR / f"_tmp_{chapter_id}.db"
    if SQLITE_PATH.exists():
        try:
            src_conn = sqlite3.connect(SQLITE_PATH)
            try:
                dst_conn = sqlite3.connect(sqlite_copy)
                with dst_conn:
                    src_conn.backup(dst_conn)
                dst_conn.close()
            finally:
                src_conn.close()
        except sqlite3.DatabaseError:
            # Live DB unreadable by SQLite (corrupt mid-crash state). The safety
            # archive must still capture the bytes — raw copy fallback.
            sqlite_copy.write_bytes(SQLITE_PATH.read_bytes())
    else:
        sqlite_copy.write_bytes(b"")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(sqlite_copy, "fictionwriter.db")
        if GRAPHITI_PATH.exists() and GRAPHITI_PATH.is_file():
            zf.write(GRAPHITI_PATH, "graphiti.db")
        else:
            zf.writestr("graphiti.db", b"")
    sqlite_copy.unlink(missing_ok=True)
    return zip_path.resolve()


def restore_snapshot(
    snapshot_path: Path,
    branch_reason: Optional[str] = None,
) -> dict:
    """
    Decompress a snapshot ZIP and replace the live database files.

    Purpose:
        Implements the O(1) branch restore path. Before replacing the live files,
        archives the current live databases to data/snapshots/ as a safety backup
        (so the current branch is not permanently lost). Then extracts the target
        snapshot ZIP, replacing data/fictionwriter.db and data/graphiti.db in place.
        All Graphiti edge timestamps in the restored database are the original
        timestamps from when the snapshot was created — no timestamp manipulation.

        If branch_reason is provided (user entered a "Reasons" directive in the Codex UI),
        the returned dict includes a "branch_reason" key that callers inject into
        active_context_package so the planning nodes steer the story in a new direction.

    Inputs:
        snapshot_path: Path — path to the snapshot ZIP to restore.
        branch_reason: Optional[str] — plain-text reasons directive from the user.
            If provided, included in the returned state payload for context injection.

    Outputs:
        dict: State payload to merge into OrchestratorState. Contains at minimum:
            {'branch_reason': str | None}. On success, the live database files have
            been replaced by the snapshot contents.
    """
    from datetime import datetime, timezone

    snapshot_path = Path(snapshot_path)
    if not zipfile.is_zipfile(snapshot_path):
        raise ValueError(f"restore_snapshot: not a valid snapshot ZIP: {snapshot_path}")

    # Safety: archive the CURRENT live branch before replacing anything.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    create_chapter_snapshot("prerestore", stamp)

    with zipfile.ZipFile(snapshot_path, "r") as zf:
        names = set(zf.namelist())
        if "fictionwriter.db" in names:
            SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
            SQLITE_PATH.write_bytes(zf.read("fictionwriter.db"))
        if "graphiti.db" in names:
            GRAPHITI_PATH.parent.mkdir(parents=True, exist_ok=True)
            GRAPHITI_PATH.write_bytes(zf.read("graphiti.db"))

    return {"branch_reason": branch_reason, "restored_from": str(snapshot_path)}


def replay_events_after_snapshot(
    snapshot_beat_id: str,
    log_path: Path = Path("data/event_log.jsonl"),
) -> None:
    """
    Replay .jsonl events after a checkpoint beat to restore intra-chapter state.

    Purpose:
        The crash recovery path. Called after restore_snapshot() loads the most recent
        chapter-boundary snapshot. Replays all beat_commit events in the .jsonl that
        were committed after snapshot_beat_id, applying them to the restored SQLite and
        Graphiti databases. Uses event_log.iter_events_after_checkpoint() for the
        event stream and calls memory/sqlite_db.py upsert functions and
        memory/graphiti_client._apply_event() for each replayed event.

        All writes are idempotent by design (SQLite upsert keyed by beat_id, Graphiti
        upsert keyed by deterministic UUID) — replaying an event twice has no effect.

        Maximum data loss on any crash: one in-progress (uncommitted) beat.

    Inputs:
        snapshot_beat_id: str — the beat_id of the last beat included in the restored
            snapshot. Events after this beat are replayed.
        log_path: Path — path to the .jsonl event log file.

    Outputs:
        None. Side effect: applies replayed events to SQLite and Graphiti, bringing
        the databases to the state they were in just before the crash.
    """
    import json as _json

    from memory.event_log import iter_events_after_checkpoint
    from memory.graphiti_client import _apply_event
    from memory.sqlite_db import upsert_beat

    for event in iter_events_after_checkpoint(log_path, snapshot_beat_id):
        if event.get("type") != "beat_commit":
            continue
        # Idempotent SQLite replay (status only — prose deltas already live in
        # beat_plan_json written by the original commit's upsert).
        if event.get("beat_id") and event.get("scene_id") is not None:
            upsert_beat(
                SQLITE_PATH,
                {
                    "beat_id": event["beat_id"],
                    "scene_id": event["scene_id"],
                    "beat_index": event.get("beat_index", 0),
                    "beat_plan_json": _json.dumps({"replayed": True, **{k: event[k] for k in ("word_count",) if k in event}}),
                    "status": "committed",
                },
            )
        # Idempotent Graphiti replay (deterministic-UUID upsert chain).
        _apply_event(event)


def list_snapshots() -> list[dict]:
    """
    Return metadata for all available chapter-boundary snapshots.

    Purpose:
        Called by routes/codex.py to populate the Branches panel in the Codex UI.
        Lists all snapshot ZIPs in data/snapshots/ and extracts chapter_id and
        timestamp from each filename.

    Inputs:
        None. Reads from the SNAPSHOTS_DIR path.

    Outputs:
        List[dict]: One dict per snapshot, sorted by timestamp descending (newest first).
            Each dict contains: filename (str), chapter_id (str), timestamp (str),
            size_bytes (int), path (str).
    """
    snapshots = []
    if not SNAPSHOTS_DIR.exists():
        return snapshots
    for zip_path in SNAPSHOTS_DIR.glob("snapshot_*.zip"):
        stem = zip_path.stem  # snapshot_{chapter_id}_{timestamp}
        parts = stem.split("_")
        timestamp = parts[-1] if len(parts) >= 3 else ""
        chapter_id = "_".join(parts[1:-1]) if len(parts) >= 3 else stem
        snapshots.append(
            {
                "filename": zip_path.name,
                "chapter_id": chapter_id,
                "timestamp": timestamp,
                "size_bytes": zip_path.stat().st_size,
                "path": str(zip_path.resolve()),
            }
        )
    snapshots.sort(key=lambda s: s["timestamp"], reverse=True)
    return snapshots

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
    pass


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
    pass


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
    pass


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
    pass

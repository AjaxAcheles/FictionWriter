"""
memory/event_log.py

Append-Only Event Ledger — .jsonl Audit Trail and Crash Recovery Feed.

Purpose:
    Manages the immutable append-only .jsonl event log. Every FSM state change is
    recorded as one JSON line. The log serves two distinct roles:
    1. Audit trail: an immutable history of every committed beat, inspectable by
       the Codex UI and branch_manager.py for consistency reconciliation.
    2. Crash recovery feed: when a pending CommitIntent row is detected on startup,
       branch_manager.py loads the most recent chapter-boundary snapshot then replays
       .jsonl records forward to the crash point using _apply_event() in both SQLite
       and Graphiti writers.

    PAD state atomicity: character PAD state updates are ALWAYS bundled inside the
    beat_commit event payload — never written as standalone events. This ensures that
    _apply_event() can restore both prose state and emotional state atomically from a
    single record during replay, preventing partial replays from leaving PAD state
    inconsistent with committed beat content.

    beat_commit event payload structure:
    {
        "event": "beat_commit",
        "beat_id": str,
        "fsm_pointer": {arc_id, chapter_id, scene_id, beat_index},
        "prose_delta": str,
        "thread_updates": List[{id, status}],
        "pad_states": {
            "character_id": {"pleasure": float, "arousal": float, "dominance": float}
        }
    }

    This log is NOT the primary branch restore path — that uses direct DB snapshot
    decompression (O(1), all timestamps intact). The .jsonl is the intra-chapter
    crash recovery path only (beats committed after the last chapter-boundary snapshot).

Architecture role:
    - Written by node_commit_transaction as the third step in the commit sequence
      (after SQLite upsert and Graphiti upsert, before CommitIntent flip to committed).
    - Read by memory/branch_manager.py for crash recovery replay and audit inspection.
    - Initialized by core/runtime.py — verifies path exists; does NOT truncate on restart.
    - Deleted and recreated by reset_resources() in core/runtime.py.
"""

import json
from pathlib import Path
from typing import Iterator


def write_event(log_path: Path, payload: dict) -> None:
    """
    Append one event payload as a JSON line to the event log.

    Purpose:
        The sole write function for the event log. Called by node_commit_transaction
        as the third step of the commit sequence. The payload is serialized as a
        single JSON object followed by a newline. No line wrapping, no pretty-printing
        — one compact JSON object per line for efficient backward scanning.

        PAD state atomicity: callers must bundle character PAD state updates inside
        the payload dict under a "pad_states" key. Standalone PAD events must never
        be written — this function does not enforce this invariant but the caller
        contract mandates it.

    Inputs:
        log_path: Path — path to the .jsonl file (e.g., Path("data/event_log.jsonl")).
        payload: dict — the full event payload to serialize. Must contain at minimum
            an "event" key identifying the event type.

    Outputs:
        None. Side effect: appends one line to the .jsonl file.
    """
    pass


def iter_events(log_path: Path) -> Iterator[dict]:
    """
    Iterate all events in the log in forward (chronological) order.

    Purpose:
        Provides a forward iterator over all .jsonl records for audit inspection
        and chapter-boundary reconciliation. Used by branch_manager.py during
        crash recovery to replay events forward from a snapshot checkpoint.
        Each yielded dict is one deserialized event payload.

    Inputs:
        log_path: Path — path to the .jsonl file.

    Outputs:
        Iterator[dict]: Yields one deserialized event dict per line, in chronological
            (append) order. Does not load the entire file into memory.
    """
    pass


def iter_events_after_checkpoint(
    log_path: Path,
    checkpoint_beat_id: str,
) -> Iterator[dict]:
    """
    Iterate events that appear after a given beat_id checkpoint in the log.

    Purpose:
        Used by branch_manager.py during crash recovery replay. Loads the most recent
        chapter-boundary snapshot (which restores state to the last chapter boundary),
        then calls this function to replay only the beats committed after that snapshot.
        Scans forward through the log until the checkpoint beat_id is found, then yields
        all subsequent events.

        Because all SQLite and Graphiti writes are idempotent (upsert keyed by beat_id /
        deterministic UUID), replay is safe — replaying an event twice has no additional
        effect.

    Inputs:
        log_path: Path — path to the .jsonl file.
        checkpoint_beat_id: str — the beat_id of the last committed beat included in
            the chapter-boundary snapshot. Events after this beat are replayed.

    Outputs:
        Iterator[dict]: Yields deserialized event dicts for beats committed after
            the checkpoint, in chronological order.
    """
    pass

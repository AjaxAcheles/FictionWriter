"""
routes/codex.py

Memory Codex Blueprint — Database Explorer and Branch Navigator.

Purpose:
    Provides the server-side routes for the Codex UI (templates/codex.html) — a
    read-mostly database explorer that surfaces the FSM's persistent memory stores
    for author inspection and navigation.

    Features:
    - Characters panel: lists all characters with their current PAD states and style profiles.
    - Threads panel: Kanban-style board showing open, progressing, and closed subplot threads.
      Supports drag-and-drop reordering of priority scores.
    - Branches panel: lists all chapter-boundary snapshot ZIPs with metadata. Allows the
      author to select a snapshot and initiate a branch restore.
    - Manuscript panel: assembled prose from committed beats, chapter by chapter.

    Key routes:
    GET  /codex             — Renders the Codex explorer view.
    GET  /codex/characters  — JSON array of all characters with PAD states.
    GET  /codex/threads     — JSON array of all threads with their status and priority.
    POST /codex/threads/priority — Update a thread's priority_score.
    GET  /codex/branches    — JSON array of all available snapshot ZIPs.
    POST /codex/restore     — Initiate a branch restore from a named snapshot.
    GET  /codex/manuscript  — Returns the full assembled manuscript text as plain text.

Architecture role:
    - All character and thread reads query SQLite directly via memory/sqlite_db.py.
    - Branch restore calls memory/branch_manager.restore_snapshot() with an optional
      "Reasons" directive. The restored state is injected into active_context_package
      for the planning nodes to steer the story in a new direction on resume.
    - The Codex is read-mostly; the only write operations are priority reordering
      and branch restore.
"""

import json
import sqlite3
from contextlib import closing

from quart import Blueprint, Response, render_template, request

codex_bp = Blueprint("codex", __name__)


@codex_bp.route("/codex")
async def codex_view():
    """Render the Memory Codex explorer view (panel data loads lazily via JSON)."""
    from core import runtime
    from memory import sqlite_db

    with closing(sqlite_db.get_connection(runtime.SQLITE_PATH)) as conn:
        counts = {
            "characters": conn.execute("SELECT COUNT(*) FROM Characters").fetchone()[0],
            "threads": conn.execute("SELECT COUNT(*) FROM Threads").fetchone()[0],
        }
    from memory.branch_manager import list_snapshots
    counts["branches"] = len(list_snapshots())
    return await render_template("codex.html", counts=counts)


@codex_bp.route("/codex/characters")
async def get_characters():
    """Characters with latest PAD state and style-profile presence flag."""
    from core import runtime
    from memory import sqlite_db

    # The Characters table may not exist yet on a fresh project (schema not
    # initialized). Degrade to an empty panel rather than a fatal 500.
    try:
        chars = sqlite_db.get_characters(runtime.SQLITE_PATH)
    except sqlite3.OperationalError:
        return []

    result = []
    for char in chars:
        history = sqlite_db.get_recent_character_emotions(runtime.SQLITE_PATH, char["char_id"], limit=1)
        pad = (
            {k: history[0][k] for k in ("pleasure", "arousal", "dominance")}
            if history else {"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0}
        )
        style_path = runtime.STYLES_DIR / f"style_char_{char['char_id']}.json"
        result.append(
            {"id": char["char_id"], "name": char["name"], "description": char.get("description") or "",
             "pad": pad, "has_style_profile": style_path.exists()}
        )
    return result


@codex_bp.route("/codex/threads")
async def get_threads():
    """All threads, priority DESC, for the Kanban board."""
    from core import runtime
    from memory import sqlite_db

    with closing(sqlite_db.get_connection(runtime.SQLITE_PATH)) as conn:
        rows = conn.execute("SELECT * FROM Threads ORDER BY priority DESC").fetchall()
    return [
        {"id": r["thread_id"], "name": r["name"], "description": r["description"] or "",
         "status": r["status"], "priority_score": r["priority"]}
        for r in rows
    ]


@codex_bp.route("/codex/threads/priority", methods=["POST"])
async def update_thread_priority():
    """Persist a Kanban drag's new priority_score."""
    from core import runtime
    from memory import sqlite_db

    payload = await request.get_json(force=True)
    with closing(sqlite_db.get_connection(runtime.SQLITE_PATH)) as conn:
        conn.execute(
            "UPDATE Threads SET priority = ? WHERE thread_id = ?",
            (float(payload["priority_score"]), payload["thread_id"]),
        )
        conn.commit()
    return {"status": "updated", "thread_id": payload["thread_id"]}


@codex_bp.route("/codex/branches")
async def get_branches():
    """Snapshot metadata for the Branches panel (newest first)."""
    from memory.branch_manager import list_snapshots

    return list_snapshots()


@codex_bp.route("/codex/restore", methods=["POST"])
async def restore_branch():
    """Pause-guarded O(1) branch restore from a named snapshot."""
    from memory import branch_manager
    from routes import control

    payload = await request.get_json(force=True)
    if not control.is_paused():
        return {"status": "error", "message": "FSM must be paused before restore"}, 409

    snapshot_path = branch_manager.SNAPSHOTS_DIR / payload["snapshot_filename"]
    if not snapshot_path.exists():
        return {"status": "error", "message": f"snapshot not found: {payload['snapshot_filename']}"}, 404
    result = branch_manager.restore_snapshot(snapshot_path, branch_reason=payload.get("reasons"))
    return {"status": "restored", "snapshot": payload["snapshot_filename"],
            "branch_reason": result.get("branch_reason")}


@codex_bp.route("/codex/manuscript")
async def get_manuscript():
    """
    Assembled manuscript (optionally one chapter), reading order.

    Default: plain text (legacy consumers, tests). With ?format=json: one
    structured record per committed scene so the dashboard can rebuild the
    timeline/outline with real scene identity instead of guessing block
    boundaries from paragraph splits.
    """
    from core import runtime
    from memory import sqlite_db

    chapter_id = request.args.get("chapter_id")
    query = (
        "SELECT s.scene_id, s.chapter_id, s.description, s.prose_text, s.word_count "
        "FROM Scenes s "
        "JOIN Chapters c ON s.chapter_id = c.chapter_id "
        "JOIN Arcs a ON c.arc_id = a.arc_id "
        "WHERE s.prose_text IS NOT NULL "
    )
    params: tuple = ()
    if chapter_id:
        query += "AND s.chapter_id = ? "
        params = (chapter_id,)
    query += "ORDER BY a.created_at ASC, c.chapter_index ASC, s.scene_index ASC"
    with closing(sqlite_db.get_connection(runtime.SQLITE_PATH)) as conn:
        rows = conn.execute(query, params).fetchall()

    if request.args.get("format") == "json":
        return [
            {"scene_id": r["scene_id"], "chapter_id": r["chapter_id"],
             "description": r["description"] or "", "text": (r["prose_text"] or "").strip(),
             "word_count": r["word_count"]}
            for r in rows if r["prose_text"]
        ]
    text = "\n\n".join((r["prose_text"] or "").strip() for r in rows if r["prose_text"])
    return Response(text, content_type="text/plain; charset=utf-8")


@codex_bp.route("/codex/raptor")
async def get_raptor_tree():
    """RAPTOR nodes grouped by level for the breadcrumb panel."""
    from core import runtime
    from memory import sqlite_db

    # RAPTOR summaries are populated only after the first chapter-boundary
    # consolidation; the table may not exist on an early-stage project. Return an
    # empty tree instead of a fatal 500 so the panel renders "tree is empty".
    try:
        with closing(sqlite_db.get_connection(runtime.SQLITE_PATH)) as conn:
            rows = conn.execute(
                "SELECT node_id, level, summary_text, created_at FROM RaptorNodes "
                "ORDER BY level DESC, created_at DESC"
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {"node_id": r["node_id"], "level": r["level"],
         "summary_text": r["summary_text"] or "", "created_at": r["created_at"]}
        for r in rows
    ]


@codex_bp.route("/codex/events")
async def get_events():
    """Most recent event-log records (virtualized panel; newest first)."""
    from core import runtime
    from memory.event_log import tail_events

    limit = int(request.args.get("limit", 200))
    return tail_events(runtime.EVENT_LOG_PATH, limit)[::-1]

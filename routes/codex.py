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

from quart import Blueprint, render_template, request

codex_bp = Blueprint("codex", __name__)


@codex_bp.route("/codex")
async def codex_view():
    """
    Render the Memory Codex explorer view.

    Purpose:
        Serves templates/codex.html with initial counts for Characters, Threads,
        and Branches panels. The detailed data for each panel is loaded lazily
        via the JSON API endpoints below.

    Inputs:
        None (GET request).

    Outputs:
        Rendered HTML response for the codex template.
    """
    pass


@codex_bp.route("/codex/characters")
async def get_characters():
    """
    Return all characters with their current PAD states and style profile status.

    Purpose:
        Queries SQLite Characters and CharacterEmotions tables for all character
        rows. Also checks data/styles/ for the existence of style_char_{id}.json
        files to indicate whether a style profile has been distilled for each character.

    Inputs:
        None (GET request).

    Outputs:
        JSON array: [{id, name, description, pad: {pleasure, arousal, dominance},
            has_style_profile: bool}]
    """
    pass


@codex_bp.route("/codex/threads")
async def get_threads():
    """
    Return all subplot threads sorted by priority_score DESC.

    Purpose:
        Queries SQLite Threads table for all thread rows. Used to populate the
        Kanban-style thread board in the Codex UI. Threads are grouped by status
        (open, progressing, closed) in the frontend.

    Inputs:
        None (GET request).

    Outputs:
        JSON array: [{id, description, status, priority_score}]
    """
    pass


@codex_bp.route("/codex/threads/priority", methods=["POST"])
async def update_thread_priority():
    """
    Update a thread's priority_score (e.g., after drag-and-drop reordering).

    Purpose:
        Receives a thread_id and new priority_score from the frontend Kanban drag.
        Updates the Threads table in SQLite. The updated priority takes effect at
        the next node_plan_arc or node_plan_chapter invocation.

    Inputs:
        POST body (JSON): {"thread_id": str, "priority_score": float}

    Outputs:
        JSON: {"status": "updated", "thread_id": str}
    """
    pass


@codex_bp.route("/codex/branches")
async def get_branches():
    """
    Return metadata for all available chapter-boundary snapshot ZIPs.

    Purpose:
        Calls memory/branch_manager.list_snapshots() to enumerate all ZIP files
        in data/snapshots/. Returns metadata for the Branches panel in the Codex UI,
        sorted newest first.

    Inputs:
        None (GET request).

    Outputs:
        JSON array: [{filename, chapter_id, timestamp, size_bytes, path}]
    """
    pass


@codex_bp.route("/codex/restore", methods=["POST"])
async def restore_branch():
    """
    Initiate a branch restore from a named snapshot.

    Purpose:
        Receives a snapshot filename and optional "Reasons" directive. Calls
        memory/branch_manager.restore_snapshot() to decompress the ZIP and replace
        the live database files. If a Reasons directive is provided, it is injected
        into the active_context_package so the planning nodes steer the story in a
        new direction on resume.

        The FSM must be paused (pause_requested=True) before branch restore is safe.
        This endpoint checks pause state and returns an error if the FSM is running.

    Inputs:
        POST body (JSON): {"snapshot_filename": str, "reasons": str | null}

    Outputs:
        JSON: {"status": "restored", "snapshot": str} on success.
        JSON: {"status": "error", "message": "FSM must be paused before restore"} if running.
    """
    pass


@codex_bp.route("/codex/manuscript")
async def get_manuscript():
    """
    Return the full assembled manuscript text from committed beats.

    Purpose:
        Queries SQLite for all committed Beat rows ordered by arc → chapter → scene →
        beat_index. Assembles the prose_delta fields into the full manuscript text.
        Returns as plain text for display or export.

    Inputs:
        Optional query param: ?chapter_id=str to filter to a specific chapter.

    Outputs:
        Plain text response: the assembled manuscript prose.
    """
    pass

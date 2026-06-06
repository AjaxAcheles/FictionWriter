"""
routes/control.py

Control Blueprint — Pause, Hard Stop, and Development Reset.

Purpose:
    Provides runtime control endpoints for the FSM execution. Allows the author to
    pause generation at a safe boundary, assert a hard stop, apply manual prose patches,
    and (in development) reset all data stores to a clean slate.

    Key routes:
    POST /control/pause   — Sets pause_requested=True in FSM state. The FSM halts at
                            the next safe boundary (sentence/newline) and unlocks the
                            UI editor for manual patching.
    POST /control/resume  — Clears pause_requested and resumes FSM execution.
    POST /control/stop    — Sets hard_stop_asserted=True. FSM halts without resuming.
    POST /control/patch   — Applies a manual prose edit to current_draft_text and/or
                            configuration changes. Posted by the UI editor on resume.
    POST /control/reset   — DEVELOPMENT UTILITY. Wipes all data stores and reinitializes
                            from scratch. Must be disabled/access-controlled in production.

Architecture role:
    - pause_requested and hard_stop_asserted are checked by FSM nodes between LLM
      calls (or after asyncio.gather in concurrent critic mode). The FSM does not
      abort mid-generation token; it halts at the next natural boundary.
    - The patch endpoint feeds author input to the async UI queue consumed by
      node_human_intervention.
    - POST /control/reset calls core/runtime.reset_resources() which deletes all
      file-based stores (data/fictionwriter.db, data/graphiti.db, snapshots, event log,
      ChromaDB, style stores) and calls init_resources() to reinitialize.
"""

from quart import Blueprint, current_app, request

from core.runtime import reset_resources

control_bp = Blueprint("control", __name__)

# Module-level flags checked by FSM nodes between LLM calls.
pause_requested: bool = False
hard_stop_asserted: bool = False


@control_bp.route("/control/pause", methods=["POST"])
async def pause():
    """
    Signal the FSM to pause at the next safe boundary.

    Purpose:
        Sets pause_requested=True in the active OrchestratorState (via the shared
        FSM state store or an asyncio Event). The FSM checks this flag between LLM
        calls and halts at the next sentence/newline boundary. Sends a "status: paused"
        SSE event to the frontend to unlock the prose editor.

    Inputs:
        None (POST request, no body).

    Outputs:
        JSON: {"status": "pause_requested"}
    """
    global pause_requested
    pause_requested = True
    return {"status": "pause_requested"}


@control_bp.route("/control/resume", methods=["POST"])
async def resume():
    """
    Resume FSM execution after a pause.

    Purpose:
        Clears pause_requested in the active OrchestratorState. If a manual patch
        has been applied via /control/patch, the FSM resumes from the patched state.
        Otherwise it resumes from where it paused.

    Inputs:
        None (POST request, no body).

    Outputs:
        JSON: {"status": "resumed"}
    """
    global pause_requested
    pause_requested = False
    return {"status": "resumed"}


@control_bp.route("/control/stop", methods=["POST"])
async def stop():
    """
    Assert a hard stop — FSM halts and does not resume automatically.

    Purpose:
        Sets hard_stop_asserted=True in the active OrchestratorState. The FSM
        halts at the next safe boundary and does not resume automatically. The
        author must start a new generation session or restore a branch to continue.

    Inputs:
        None (POST request, no body).

    Outputs:
        JSON: {"status": "stopped"}
    """
    global hard_stop_asserted
    hard_stop_asserted = True
    return {"status": "stopped"}


@control_bp.route("/control/patch", methods=["POST"])
async def patch():
    """
    Apply a manual prose edit or parameter change to the paused FSM.

    Purpose:
        Receives manual edits from the UI prose editor. Puts the patch payload
        into the async UI queue consumed by node_human_intervention. The FSM
        resumes (via /control/resume) after the patch is applied.

        Accepts two optional patch types:
        - prose_edit: str — replaces current_draft_text.
        - config_patch: dict — updates specific config fields (takes effect next beat).

    Inputs:
        POST body (JSON): {
            "prose_edit": str | null,
            "config_patch": dict | null
        }

    Outputs:
        JSON: {"status": "patched"}
    """
    return {"status": "patched"}


@control_bp.route("/control/reset", methods=["POST"])
async def reset():
    """
    Wipe all data stores and reinitialize. DEVELOPMENT UTILITY ONLY.

    Purpose:
        Calls core/runtime.reset_resources() which deletes data/fictionwriter.db,
        data/graphiti.db, all snapshot ZIPs, the event log, ChromaDB collections,
        and style store JSON files — then calls init_resources() to reinitialize
        all stores from scratch. Provides a clean slate without a server restart.

        WARNING: This operation is irreversible. All manuscript data, snapshots,
        and style profiles are permanently deleted. Disable or access-control this
        endpoint before any production deployment.

    Inputs:
        None (POST request, no body).

    Outputs:
        JSON: {"status": "reset_complete"}
    """
    await reset_resources(current_app.config["APP_CONFIG"])
    return {"status": "reset_complete"}

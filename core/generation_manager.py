"""
core/generation_manager.py

Background Generation Manager — owns the FSM task lifecycle.

Purpose:
    Fixes the two systemic frontend-state bugs:

    1. BACKGROUND PERSISTENCE. The old /generate handler fire-and-forgot
       `create_task(app.ainvoke(...))` without keeping a reference — Python's
       event loop holds tasks weakly, so the running generation could be
       garbage-collected mid-flight (perceived as "generation stops when I
       click off the page"). The manager keeps a strong module-level reference;
       generation runs server-side regardless of page focus, tab visibility,
       or SSE disconnects.

    2. OBSERVABLE PIPELINE. The graph is driven via astream(stream_mode="debug"),
       which emits a `task` event at every node START. Each one is published to
       the SSE bus as a `pipeline_status` event with a human-readable stage
       label — the UI always knows exactly what the pipeline is doing. The
       same snapshot is queryable at GET /status so a freshly-(re)loaded page
       can reattach to an in-flight run instead of staring at a blank screen.

Architecture role:
    - routes/dashboard.py /generate → start();  /status → snapshot().
    - Publishes: pipeline_status, generation_complete, generation_error.
    - Single-flight: a second start() while running returns False (HTTP 409).
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

from core import stream_bus
from core.logger import get_logger

logger = get_logger("generation_manager")

#: Human-readable stage labels for the pipeline status indicator.
NODE_LABELS = {
    "node_plan_global": "Planning story arcs…",
    "node_plan_arc": "Planning chapters…",
    "node_plan_chapter": "Scheduling scenes…",
    "node_plan_beat": "Partitioning beats…",
    "node_assemble_context": "Assembling context…",
    "node_draft_prose": "Drafting prose…",
    "node_programmatic_audit": "Auditing draft…",
    "node_adversarial_critics": "Running critic committee…",
    "node_revise_prose": "Revising draft…",
    "node_commit_transaction": "Committing beat…",
    "node_compress_memory": "Consolidating chapter memory…",
    "node_craft_consultant": "Consulting craft advisor…",
    "node_freeze_and_escalate": "Recovering — escalation ladder…",
    "node_human_intervention": "Awaiting author input…",
}

_task: Optional[asyncio.Task] = None  # STRONG reference — never let the GC eat the run
_status: dict = {
    "running": False,
    "stage": None,
    "stage_label": "Idle",
    "project_id": None,
    "started_at": None,
    "finished_at": None,
    "nodes_executed": 0,
    "last_error": None,
}


def is_running() -> bool:
    """True while a generation task is alive."""
    return _task is not None and not _task.done()


def snapshot() -> dict:
    """
    Current status snapshot for GET /status and page-reattach.

    elapsed_s is computed server-side so the frontend timer never depends on
    the browser parsing started_at (timezone/format skew across clients).
    """
    snap = dict(_status)
    if snap["running"] and snap["started_at"]:
        try:
            started = datetime.fromisoformat(snap["started_at"])
            snap["elapsed_s"] = max(
                0, int((datetime.now(timezone.utc) - started).total_seconds())
            )
        except ValueError:
            snap["elapsed_s"] = None
    return snap


def _set(**fields) -> None:
    _status.update(fields)


def start(initial_state: dict, project_id: str = "default") -> bool:
    """
    Launch the FSM as a managed background task. Returns False when a run is
    already in flight (single-flight guard — the route answers 409).
    """
    global _task
    if is_running():
        return False

    _set(
        running=True, stage=None, stage_label="Starting pipeline…",
        project_id=project_id, started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None, nodes_executed=0, last_error=None,
    )
    stream_bus.publish({"type": "pipeline_status", "stage": "startup",
                        "label": "Starting pipeline…", "running": True})
    _task = asyncio.get_event_loop().create_task(_run(initial_state))
    return True


async def _run(initial_state: dict) -> None:
    """Drive the compiled graph, narrating every node start to the SSE bus."""
    from fsm.graph import compile_graph

    start_time = time.monotonic()
    try:
        graph = compile_graph()
        async for event in graph.astream(
            initial_state, config={"recursion_limit": 10_000}, stream_mode="debug"
        ):
            if not isinstance(event, dict) or event.get("type") != "task":
                continue
            node = (event.get("payload") or {}).get("name") or "?"
            label = NODE_LABELS.get(node, f"Running {node}…")
            _set(stage=node, stage_label=label, nodes_executed=_status["nodes_executed"] + 1)
            stream_bus.publish({"type": "pipeline_status", "stage": node,
                                "label": label, "running": True})

        _set(running=False, stage=None, stage_label="Complete",
             finished_at=datetime.now(timezone.utc).isoformat())
        stream_bus.publish({"type": "generation_complete",
                            "duration_s": round(time.monotonic() - start_time, 1)})
        logger.info("generation complete in %.1fs", time.monotonic() - start_time)
    except asyncio.CancelledError:
        _set(running=False, stage=None, stage_label="Stopped",
             finished_at=datetime.now(timezone.utc).isoformat())
        stream_bus.publish({"type": "pipeline_status", "stage": "stopped",
                            "label": "Stopped", "running": False})
        raise
    except Exception as e:  # noqa: BLE001 — surfaced to the UI, never swallowed silently
        logger.error("generation failed: %r", e)
        _set(running=False, stage=None, stage_label="Failed",
             finished_at=datetime.now(timezone.utc).isoformat(), last_error=repr(e))
        stream_bus.publish({"type": "generation_error", "error": repr(e)})


def cancel() -> bool:
    """Hard-cancel the in-flight task (Hard Stop). True if something was cancelled."""
    if is_running():
        _task.cancel()
        return True
    return False

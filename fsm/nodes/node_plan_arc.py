"""
fsm/nodes/node_plan_arc.py

Level 2 Planning Node — Arc Planner. SPRINT 3 SEEDING STUB.

Purpose (full version — Sprint 4):
    Expands the global plan into multi-chapter acts, evaluating which subplot
    threads open, progress, or close, using the planner endpoint.

Sprint 3 behavior:
    Seeds one default Chapter row under the active arc when the arc has no
    chapters (idempotent), and points fsm_pointer.chapter_id at the first
    incomplete chapter.
"""

import time
from datetime import datetime, timezone

from core import runtime
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from memory import sqlite_db

logger = get_logger("node_plan_arc")


async def node_plan_arc(state: OrchestratorState) -> dict:
    """Seed the default chapter and set fsm_pointer.chapter_id (Sprint 3 stub)."""
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    db = runtime.SQLITE_PATH
    try:
        chapters = sqlite_db.get_remaining_chapters(db, pointer.arc_id)
        if not chapters:
            chapter_id = f"{pointer.arc_id}_ch_001"
            sqlite_db.insert_row(
                db,
                "Chapters",
                {
                    "chapter_id": chapter_id,
                    "arc_id": pointer.arc_id,
                    "title": "Chapter One",
                    "chapter_index": 0,
                    "status": "planned",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        else:
            chapter_id = chapters[0]["chapter_id"]
        updated = pointer.model_copy(update={"chapter_id": chapter_id})
        log_node_event(logger, updated.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return {"fsm_pointer": updated}
    except Exception as e:
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e))
        raise

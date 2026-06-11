"""
fsm/nodes/node_plan_global.py

Level 1 Planning Node — Global Story Planner. SPRINT 3 SEEDING STUB.

Purpose (full version — Sprint 4):
    Generates the master timeline and thematic boundaries for the entire book
    via the planner endpoint, runs the Density Model for word count estimation,
    and accepts existing_arcs for continuation-arc generation.

Sprint 3 behavior:
    Seeds a single default Arc row when none exists (idempotent INSERT OR
    IGNORE) and points fsm_pointer.arc_id at the first arc. This unblocks the
    vertical slice's plan_beat → commit loop without LLM planning calls.
"""

import time
from datetime import datetime, timezone

from core import runtime
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from memory import sqlite_db

logger = get_logger("node_plan_global")

DEFAULT_ARC_ID = "arc_001"


async def node_plan_global(state: OrchestratorState) -> dict:
    """Seed the default arc and set fsm_pointer.arc_id (Sprint 3 stub)."""
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    db = runtime.SQLITE_PATH
    try:
        sqlite_db.insert_row(
            db,
            "Arcs",
            {
                "arc_id": DEFAULT_ARC_ID,
                "title": "Arc One",
                "summary": "Vertical-slice seed arc (replaced by Sprint 4 global planning).",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        updated = pointer.model_copy(update={"arc_id": DEFAULT_ARC_ID})
        log_node_event(logger, updated.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return {"fsm_pointer": updated}
    except Exception as e:
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e))
        raise

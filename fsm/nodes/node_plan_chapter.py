"""
fsm/nodes/node_plan_chapter.py

Level 3 Planning Node — Chapter Scene Scheduler. SPRINT 3 SEEDING STUB.

Purpose (full version — Sprint 4):
    Schedules scenes to advance the active arc via the planner endpoint,
    executes the Granularity Protection Filter, and accepts THREAD_PARADOX
    constraint injection from node_freeze_and_escalate Tier 4.

Sprint 3 behavior:
    Seeds one default Scene row in the active chapter when none remain open
    (idempotent), and points fsm_pointer.scene_id at the first open scene.
    Marks the chapter 'active'.
"""

import time

from core import runtime
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from memory import sqlite_db

logger = get_logger("node_plan_chapter")

DEFAULT_SCENE_WORD_BUDGET = 1200


async def node_plan_chapter(state: OrchestratorState) -> dict:
    """Seed/select the next open scene and set fsm_pointer.scene_id (Sprint 3 stub)."""
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    db = runtime.SQLITE_PATH
    try:
        load_config()  # config read at node entry (parity with planning cascade policy)
        open_scenes = sqlite_db.get_remaining_scenes(db, pointer.chapter_id)
        if not open_scenes:
            from contextlib import closing
            with closing(sqlite_db.get_connection(db)) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM Scenes WHERE chapter_id = ?", (pointer.chapter_id,)
                ).fetchone()[0]
            scene_id = f"{pointer.chapter_id}_sc_{count + 1:03d}"
            sqlite_db.insert_row(
                db,
                "Scenes",
                {
                    "scene_id": scene_id,
                    "chapter_id": pointer.chapter_id,
                    "scene_index": count,
                    "description": "Vertical-slice seed scene (replaced by Sprint 4 chapter planning).",
                    "word_budget": DEFAULT_SCENE_WORD_BUDGET,
                    "word_count": 0,
                },
            )
        else:
            scene_id = open_scenes[0]["scene_id"]
        sqlite_db.set_chapter_status(db, pointer.chapter_id, "active")
        updated = pointer.model_copy(update={"scene_id": scene_id, "beat_index": 0})
        log_node_event(logger, updated.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return {"fsm_pointer": updated}
    except Exception as e:
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e))
        raise

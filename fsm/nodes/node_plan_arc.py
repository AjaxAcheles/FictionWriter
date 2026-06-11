"""
fsm/nodes/node_plan_arc.py

Level 2 Planning Node — Arc Planner (full Sprint 4 implementation).

Purpose:
    Expands the active arc into multi-chapter acts. Evaluates which subplot
    threads open, escalate, or close across the arc and applies those thread
    events to the Threads table. Writes Chapter stub rows to SQLite and points
    fsm_pointer.chapter_id at the first incomplete chapter.

Architecture role:
    - Triggered by node_plan_global or node_commit_transaction (arc complete).
    - Yields to node_plan_chapter. Prompt: prompts/node_plan_arc.xml.j2.
"""

import json
import time
from datetime import datetime, timezone
from typing import List

from pydantic import BaseModel, ConfigDict

from core import runtime
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from llm import call_llm as call_llm_module
from memory import sqlite_db
from memory.raptor import get_raptor_summaries
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_plan_arc")


class ChapterStub(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    description: str = ""
    status: str = "planned"


class ThreadEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    thread_id: str
    event: str  # "open" | "escalate" | "close"
    chapter_index: int = 0


class ArcPlan(BaseModel):
    """Planner output: {'chapter_stubs': [...], 'thread_events': [...]}."""

    model_config = ConfigDict(extra="ignore")

    chapter_stubs: List[ChapterStub]
    thread_events: List[ThreadEvent] = []


def apply_thread_event(db, event: ThreadEvent) -> None:
    """Apply one thread lifecycle event to the Threads table."""
    from contextlib import closing
    with closing(sqlite_db.get_connection(db)) as conn:
        if event.event == "open":
            conn.execute("UPDATE Threads SET status = 'open' WHERE thread_id = ?", (event.thread_id,))
        elif event.event == "close":
            conn.execute("UPDATE Threads SET status = 'closed' WHERE thread_id = ?", (event.thread_id,))
        elif event.event == "escalate":
            conn.execute(
                "UPDATE Threads SET priority = MIN(priority + 0.25, 1.0) WHERE thread_id = ?",
                (event.thread_id,),
            )
        conn.commit()


async def node_plan_arc(state: OrchestratorState) -> dict:
    """
    Plan the chapter structure for the active arc.

    Outputs (merged into OrchestratorState):
        fsm_pointer: chapter_id set to the first incomplete chapter.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    config = load_config()
    db = runtime.SQLITE_PATH

    try:
        remaining = sqlite_db.get_remaining_chapters(db, pointer.arc_id)
        if remaining:
            updated = pointer.model_copy(update={"chapter_id": remaining[0]["chapter_id"]})
            log_node_event(logger, updated.model_dump(), (time.monotonic() - start) * 1000.0, "success")
            return {"fsm_pointer": updated}

        arc = sqlite_db.get_row(db, "Arcs", "arc_id", pointer.arc_id) or {}
        threads = sqlite_db.get_open_threads(db)
        summaries = get_raptor_summaries(db, pointer.scene_id, levels=["global", "arc"])

        prompt = PromptLoader().load_and_render(
            "node_plan_arc.xml.j2",
            {
                "arc_id": pointer.arc_id,
                "arc_description": arc.get("summary") or "",
                "raptor_root_summary": summaries.get("global") or summaries.get("arc") or "",
                "threads_queue": json.dumps(threads),
                "word_count_budget": config.project.word_count_target,
            },
        )
        plan = await call_llm_module.call_llm_structured(
            config.endpoints.planner,
            [{"role": "user", "content": prompt}],
            ArcPlan,
            retry_cap=config.model_validate_retry_cap,
        )

        now = datetime.now(timezone.utc).isoformat()
        first_chapter_id = None
        for index, stub in enumerate(plan.chapter_stubs):
            sqlite_db.insert_row(
                db, "Chapters",
                {"chapter_id": stub.id, "arc_id": pointer.arc_id,
                 "title": stub.description[:80] or f"Chapter {index + 1}",
                 "chapter_index": index, "status": "planned", "created_at": now},
            )
            if first_chapter_id is None:
                first_chapter_id = stub.id
        for event in plan.thread_events:
            apply_thread_event(db, event)

        updated = pointer.model_copy(update={"chapter_id": first_chapter_id or pointer.chapter_id})
        log_node_event(logger, updated.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return {"fsm_pointer": updated}
    except Exception as e:
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e))
        raise

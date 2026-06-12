"""
fsm/nodes/node_plan_global.py

Level 1 Planning Node — Global Story Planner (full Sprint 4 implementation).

Purpose:
    Generates the master timeline and thematic boundaries for the entire book.
    Runs the Density Model (planner-endpoint estimation of word allocation by
    subplot complexity) and writes top-level Arc rows plus tracked subplot
    Thread rows to SQLite. Accepts existing arcs so edge_commit_router can
    request a continuation arc when all arcs are exhausted below the word target.

Architecture role:
    - Triggered once at START, or by edge_commit_router for continuation arcs.
    - Yields to node_plan_arc. Prompt: prompts/node_plan_global.xml.j2.
"""

import json
import time
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from core import runtime
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from llm import call_llm as call_llm_module
from memory import sqlite_db
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_plan_global")


class PlannedArc(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    # Small local models drift on key names — accept the common variants rather
    # than burning validation retries on synonyms.
    title: str = Field(validation_alias=AliasChoices("title", "name"))
    description: str = Field(default="", validation_alias=AliasChoices("description", "summary"))
    word_allocation: int = Field(
        default=0,
        validation_alias=AliasChoices("word_allocation", "estimated_word_count", "word_count"),
    )
    chapter_count_range: Optional[str] = None


class PlannedThread(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str = ""
    priority: float = 0.5


class GlobalPlan(BaseModel):
    """Planner output: {'arcs': [...], 'threads': [...]}."""

    model_config = ConfigDict(extra="ignore")

    arcs: List[PlannedArc]
    threads: List[PlannedThread] = []

    @model_validator(mode="before")
    @classmethod
    def _wrap_bare_list(cls, data):
        """
        Tolerance shim: models told to emit an arc plan frequently return the
        bare ARRAY of arcs instead of the documented wrapper object. Wrapping it
        here turns a guaranteed StructuredOutputError into a clean parse.
        """
        if isinstance(data, list):
            return {"arcs": data, "threads": []}
        return data


async def node_plan_global(state: OrchestratorState) -> dict:
    """
    Plan (or extend) the master arc timeline and subplot thread set.

    Outputs (merged into OrchestratorState):
        fsm_pointer: arc_id set to the first incomplete arc.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    config = load_config()
    db = runtime.SQLITE_PATH

    try:
        from contextlib import closing
        with closing(sqlite_db.get_connection(db)) as conn:
            existing = [dict(r) for r in conn.execute("SELECT * FROM Arcs ORDER BY created_at ASC")]

        prompt = PromptLoader().load_and_render(
            "node_plan_global.xml.j2",
            {
                "genre": config.project.genre,
                "premise": config.project.premise,
                "word_count_target": config.project.word_count_target,
                "world_rules": "",  # populated by the Sprint 5 ingestion pipeline
                "existing_arcs": json.dumps(
                    [{"arc_id": a["arc_id"], "title": a["title"], "summary": a["summary"]} for a in existing]
                ),
                "density_envelope": json.dumps(
                    {"word_count_target": config.project.word_count_target,
                     "existing_word_count": sqlite_db.get_total_word_count(db)}
                ),
            },
        )
        plan = await call_llm_module.call_llm_structured(
            config.endpoints.planner,
            [{"role": "user", "content": prompt}],
            GlobalPlan,
            retry_cap=config.model_validate_retry_cap,
        )

        now = datetime.now(timezone.utc).isoformat()
        first_arc_id = None
        for arc in plan.arcs:
            sqlite_db.insert_row(
                db, "Arcs",
                {"arc_id": arc.id, "title": arc.title, "summary": arc.description, "created_at": now},
            )
            if first_arc_id is None and not _arc_completed(db, arc.id):
                first_arc_id = arc.id
        for thread in plan.threads:
            sqlite_db.insert_row(
                db, "Threads",
                {"thread_id": thread.id, "name": thread.name, "description": thread.description,
                 "priority": thread.priority, "status": "open"},
            )

        updated = pointer.model_copy(update={"arc_id": first_arc_id or pointer.arc_id})
        log_node_event(logger, updated.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return {"fsm_pointer": updated}
    except Exception as e:
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e))
        raise


def _arc_completed(db, arc_id: str) -> bool:
    """An arc is complete when it has chapters and none remain open."""
    from contextlib import closing
    with closing(sqlite_db.get_connection(db)) as conn:
        total = conn.execute("SELECT COUNT(*) FROM Chapters WHERE arc_id = ?", (arc_id,)).fetchone()[0]
    return total > 0 and not sqlite_db.get_remaining_chapters(db, arc_id)

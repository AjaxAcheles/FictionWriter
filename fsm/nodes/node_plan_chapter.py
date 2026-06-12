"""
fsm/nodes/node_plan_chapter.py

Level 3 Planning Node — Chapter Scene Scheduler (full Sprint 4 implementation).

Purpose:
    Schedules scenes to advance the active arc. Executes the Granularity
    Protection Filter: every scheduled scene receives at least
    GRANULARITY_FLOOR_WORDS so downstream beats never starve.

    THREAD_PARADOX constraint injection: when triggered by
    node_freeze_and_escalate Tier 4 (replan_count > cap), the paradox
    description arrives in active_context_package["paradox_constraint"] and is
    passed to the planner as a hard constraint — the paradox outcome is treated
    as a forbidden outcome via the existing Thread-priority logic.

Architecture role:
    - Triggered by node_plan_arc, node_commit_transaction, or
      node_freeze_and_escalate (Tier 4). Yields to node_plan_beat.
    - Prompt: prompts/node_plan_chapter.xml.j2.
"""

import json
import time
from typing import List

from pydantic import BaseModel, ConfigDict, model_validator

from core import runtime, stream_bus
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from llm import call_llm as call_llm_module
from memory import sqlite_db
from memory.graphiti_client import query_point_in_time_subgraph
from memory.raptor import get_raptor_summaries
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_plan_chapter")

GRANULARITY_FLOOR_WORDS = 400  # Granularity Protection Filter minimum per scene
DEFAULT_CHAPTER_WORD_BUDGET = 4000


class PlannedScene(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    description: str = ""
    word_budget: int = GRANULARITY_FLOOR_WORDS
    ordering: int = 0


async def _character_locations(db, chapter_id: str) -> str:
    """
    Current LOCATED_IN facts for all characters, one per line.

    Queried from the Graphiti point-in-time subgraph so the scene schedule
    respects travel time and physical separation. Empty string when the graph
    is degraded/empty (the stub driver returns no edges).
    """
    characters = sqlite_db.get_characters(db)
    if not characters:
        return ""
    names = {c["char_id"]: c["name"] for c in characters}
    edges = await query_point_in_time_subgraph(
        entity_ids=list(names),
        active_event_id=f"{chapter_id}_chapter_planning",
    )
    lines = []
    for edge in edges or []:
        if "located" not in (edge.get("edge_type") or "").lower():
            continue
        a = edge.get("entity_a_id", "?")
        lines.append(f"- {names.get(a, a)} is in {edge.get('entity_b_id', '?')}")
    return "\n".join(lines)


class ChapterPlan(BaseModel):
    """Planner output: {'scenes': [...]}."""

    model_config = ConfigDict(extra="ignore")

    scenes: List[PlannedScene]

    @model_validator(mode="before")
    @classmethod
    def _wrap_bare_list(cls, data):
        """Bare array → treat as the scenes list (tolerance shim)."""
        if isinstance(data, list):
            return {"scenes": data}
        return data


async def node_plan_chapter(state: OrchestratorState) -> dict:
    """
    Schedule scenes for the active chapter (paradox-aware).

    Outputs (merged into OrchestratorState):
        fsm_pointer: scene_id set to the first open scene, beat_index reset to 0.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    config = load_config()
    db = runtime.SQLITE_PATH

    try:
        open_scenes = sqlite_db.get_remaining_scenes(db, pointer.chapter_id)

        # Chapter-advance guard. edge_commit_router routes back here on chapter
        # advance but cannot mutate the pointer — a conditional edge only returns
        # a destination string. So when the active chapter is already 'completed'
        # and has no open scenes, this node must advance pointer.chapter_id itself;
        # otherwise it regenerates fresh scenes into the finished chapter, flips it
        # back to 'active', and loops forever (the post-chapter infinite loop).
        if not open_scenes:
            current = sqlite_db.get_row(db, "Chapters", "chapter_id", pointer.chapter_id) or {}
            if current.get("status") == "completed":
                remaining = sqlite_db.get_remaining_chapters(db, pointer.arc_id)
                if not remaining:
                    # Arc fully planned — arc/END advancement is owned by
                    # edge_commit_router (steps 4–5), so nothing remains to schedule.
                    log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "success")
                    return {"fsm_pointer": pointer}
                pointer = pointer.model_copy(
                    update={"chapter_id": remaining[0]["chapter_id"], "scene_id": "", "beat_index": 0}
                )
                open_scenes = sqlite_db.get_remaining_scenes(db, pointer.chapter_id)

        if open_scenes:
            sqlite_db.set_chapter_status(db, pointer.chapter_id, "active")
            updated = pointer.model_copy(update={"scene_id": open_scenes[0]["scene_id"], "beat_index": 0})
            log_node_event(logger, updated.model_dump(), (time.monotonic() - start) * 1000.0, "success")
            return {"fsm_pointer": updated}

        chapter = sqlite_db.get_row(db, "Chapters", "chapter_id", pointer.chapter_id) or {}
        arc = sqlite_db.get_row(db, "Arcs", "arc_id", pointer.arc_id) or {}
        threads = sqlite_db.get_open_threads(db)
        summaries = get_raptor_summaries(db, pointer.scene_id, levels=["chapter"])
        package = state.get("active_context_package") or {}
        paradox = package.get("paradox_constraint") or ""

        prompt = PromptLoader().load_and_render(
            "node_plan_chapter.xml.j2",
            {
                "chapter_id": pointer.chapter_id,
                "chapter_description": chapter.get("title") or "",
                "arc_description": arc.get("summary") or "",
                "chapter_word_budget": DEFAULT_CHAPTER_WORD_BUDGET,
                "raptor_chapter_summaries": summaries.get("chapter", ""),
                "threads_priority_queue": json.dumps(threads),
                "paradox_constraint": paradox,
                "character_locations": await _character_locations(db, pointer.chapter_id),
            },
        )
        plan = await call_llm_module.call_llm_structured(
            config.endpoints.planner,
            [{"role": "user", "content": prompt}],
            ChapterPlan,
            retry_cap=config.model_validate_retry_cap,
        )

        from contextlib import closing
        with closing(sqlite_db.get_connection(db)) as conn:
            existing = conn.execute(
                "SELECT COUNT(*) FROM Scenes WHERE chapter_id = ?", (pointer.chapter_id,)
            ).fetchone()[0]

        first_scene_id = None
        for offset, scene in enumerate(sorted(plan.scenes, key=lambda s: s.ordering)):
            sqlite_db.insert_row(
                db, "Scenes",
                {
                    "scene_id": scene.id,
                    "chapter_id": pointer.chapter_id,
                    "scene_index": existing + offset,
                    "description": scene.description,
                    # Granularity Protection Filter: never schedule below the floor.
                    "word_budget": max(scene.word_budget, GRANULARITY_FLOOR_WORDS),
                    "word_count": 0,
                },
            )
            stream_bus.publish({
                "type": "planning",
                "level": "chapter",
                "chapter_id": pointer.chapter_id,
                "text": f"scene {scene.id}: {scene.description}",
            })
            if first_scene_id is None:
                first_scene_id = scene.id

        sqlite_db.set_chapter_status(db, pointer.chapter_id, "active")
        updated = pointer.model_copy(update={"scene_id": first_scene_id or pointer.scene_id, "beat_index": 0})
        log_node_event(logger, updated.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return {"fsm_pointer": updated}
    except Exception as e:
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e))
        raise

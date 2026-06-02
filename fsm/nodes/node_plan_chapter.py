"""
fsm/nodes/node_plan_chapter.py

Level 3 Planning Node — Chapter Scene Scheduler.

Purpose:
    Schedules scenes to advance the active arc within the current chapter.
    Applies the Granularity Protection Filter to ensure no scene is planned with
    a word_budget below the minimum allocation (preventing beats from being
    pathologically short when a large outline is forced into a small word budget).

    Also handles THREAD_PARADOX constraint injection: when triggered by
    node_freeze_and_escalate (Tier 4, replan_count > 2), the paradox description
    is injected as a hard planning constraint into this node's context package.
    The chapter replanner treats it identically to a Thread-priority constraint —
    the paradox entity/event is flagged as a forbidden outcome and the scene
    schedule is restructured using existing Thread-priority logic. No special
    paradox code path is needed.

Architecture role:
    - Third tier of the planning cascade. Called at scene boundaries, chapter
      boundaries, and arc boundaries (edge_commit_router routes to this node for
      both scene-advance and chapter-advance cases).
    - Uses the high-tier (large) inference endpoint (config.endpoints.planner).
    - Loads prompt from: prompts/node_plan_chapter.xml.j2 via PromptLoader.
    - Emits a structured JSON log entry via get_logger("node_plan_chapter").
"""

import time

from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_plan_chapter")


async def node_plan_chapter(state: OrchestratorState) -> dict:
    """
    Schedule scenes for the current chapter and write Scene rows to SQLite.

    Purpose:
        Reads the active Arc and Chapter rows, the last two Chapter summaries
        from RAPTOR, the Threads priority queue, and any paradox constraint
        injected by node_freeze_and_escalate. Generates a scene schedule with
        word budgets. Applies the Granularity Protection Filter: if any scene's
        word_budget falls below the minimum, the filter macro-prunes the outline
        to redistribute word budget before writing to SQLite.

        Scene rows are written with an `ordering` integer column (not created_at)
        so that sort order is stable even when scenes are created in rapid succession.

    Inputs (from OrchestratorState):
        state['fsm_pointer']: FSM_Pointer — chapter_id used to fetch the active
            Chapter row and its arc context.
        state['active_context_package']: Dict — may contain a paradox_constraint
            key injected by node_freeze_and_escalate Tier 4.
        [Reads from SQLite: active Arc, active Chapter, Threads priority queue]
        [Reads from RAPTOR: last two Chapter-level summaries for pacing context]

    Outputs (dict merged into OrchestratorState):
        fsm_pointer: Updated with the first scene_id for this chapter.
        [Side effects: Writes Scene rows to SQLite Scenes table with status='planned'
         and ordering integers. Updates Chapter status to 'active'.]

    Relationships:
        - Triggered by: node_plan_arc, edge_commit_router (scene/chapter boundary),
          or node_freeze_and_escalate (Tier 4, replan_count > 2).
        - Yields to: node_plan_beat (via direct edge in graph.py).
        - Uses: call_llm() with config.endpoints.planner endpoint.
        - Prompt: prompts/node_plan_chapter.xml.j2
    """
    pass

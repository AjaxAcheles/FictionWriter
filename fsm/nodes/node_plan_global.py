"""
fsm/nodes/node_plan_global.py

Level 1 Planning Node — Master Timeline Generator.

Purpose:
    Generates the master story timeline, thematic boundaries, and density model
    for the entire manuscript. Runs the Density Model to estimate total word counts
    based on subplot complexity: counts conflicts and characters to assign a
    mathematical "density envelope" that lets the Thread Priority Queue organically
    stretch or shrink chapter counts based on subplot resolution.

    Invoked exactly once at FSM initialization. Also invoked by edge_commit_router
    when all arcs are exhausted but the word_count_target has not been met — in
    that case it accepts existing_arcs context to generate a continuation arc
    rather than restarting from scratch.

    Writes the top-level StoryArc stub rows to the SQLite Arcs table with
    status='planned'. Does not draft prose.

Architecture role:
    - Entry point of the hierarchical planning cascade (Global → Arc → Chapter → Beat).
    - Uses the high-tier (large) inference endpoint (config.endpoints.planner).
    - Loads prompt from: prompts/node_plan_global.xml.j2 via PromptLoader.
    - Emits a structured JSON log entry via get_logger("node_plan_global").
"""

import time

from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_plan_global")


async def node_plan_global(state: OrchestratorState) -> dict:
    """
    Generate the master story timeline and write Arc stubs to SQLite.

    Purpose:
        Constructs the highest-level narrative structure: thematic arcs, the
        subplot density envelope, and the master word-count budget. Streams the
        generated outline bullet-by-bullet to the UI via SSE. On continuation
        runs (all arcs exhausted, word target not met), existing Arc rows are
        passed as context so the planner extends rather than restarts the story.

    Inputs (from OrchestratorState):
        state['project_id']: str — used to query SQLite for project_metadata
            (genre, word_count_target, premise) and world rules.
        state['fsm_pointer']: FSM_Pointer — included in log entry snapshot.
        [Reads from SQLite: project_metadata, world rules, existing Arcs if
         continuation mode is active]

    Outputs (dict merged into OrchestratorState):
        fsm_pointer: Updated with the first planned arc_id.
        [Side effects: Writes Arc rows to SQLite Arcs table with status='planned']

    Relationships:
        - Triggered by: FSM START edge, or edge_commit_router (continuation mode).
        - Yields to: node_plan_arc (via direct edge in graph.py).
        - Uses: call_llm() with config.endpoints.planner endpoint.
        - Prompt: prompts/node_plan_global.xml.j2
    """
    pass

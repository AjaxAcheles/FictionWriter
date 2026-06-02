"""
fsm/routers/edge_commit_router.py

Dynamic relational advancement router — routes after node_commit_transaction.

Purpose:
    Determines the next planning node after a beat is successfully committed by
    querying SQLite ground truth rather than relying on internal counters or
    pre-planned beat counts. This decouples advancement routing from static
    planning values and protects the FSM when node_freeze_and_escalate Tier 3
    dynamically subdivides beats mid-flight (creating new Beat rows that the
    pre-planned count does not know about).

    Query order (first match wins):
    1. Beats table: remaining beats in the current scene? → "node_plan_beat"
    2. Scenes table: remaining scenes in the current chapter (ORDER BY ordering ASC)?
       → "node_plan_chapter"  (scene advance)
    3. Chapters table: remaining chapters in the current arc? → "node_plan_chapter"
       (chapter advance — node_plan_chapter handles both scene and chapter scheduling)
    4. Arc exhaustion + word count: SUM(word_count) across all arcs < word_count_target?
       → "node_plan_global"  (generates a continuation arc; accepts existing_arcs)
    5. Manuscript complete: all arcs exhausted AND word count target met → "END"
       (triggers the Export Pipeline)

    The Scenes table query uses `ordering ASC` (not `created_at ASC`) to prevent
    chronological sort bugs when scenes are generated simultaneously.

Architecture role:
    - Registered in fsm/graph.py as the conditional edge after node_commit_transaction.
    - Reads SQLite directly (not from OrchestratorState counts) for all advancement
      decisions. fsm_pointer in state provides the current context for query filtering.
    - node_compress_memory is called synchronously by node_commit_transaction at chapter
      boundaries before routing occurs — this router does not call it directly.
"""

from fsm.state import OrchestratorState


def edge_commit_router(state: OrchestratorState) -> str:
    """
    Query SQLite to determine the next planning node after a successful beat commit.

    Purpose:
        Implements the five-step query cascade described in the module docstring.
        All queries filter by the current fsm_pointer fields (arc_id, chapter_id,
        scene_id) to scope lookups to the active narrative position.

    Inputs:
        state['fsm_pointer']: FSM_Pointer — current arc/chapter/scene/beat position.
            Used as filter context for all SQLite queries.

    Outputs:
        str: One of:
            "node_plan_beat"    — more beats remain in the current scene.
            "node_plan_chapter" — scene or chapter boundary; schedule next unit.
            "node_plan_global"  — arc exhausted but word target not met; continue.
            "END"               — manuscript complete; trigger Export Pipeline.
    """
    pass

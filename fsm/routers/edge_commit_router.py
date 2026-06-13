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

import json
from datetime import datetime, timezone

from core.logger import get_logger
from fsm.state import OrchestratorState

logger = get_logger("edge_commit_router")


def edge_commit_router(state: OrchestratorState) -> str:
    """
    Query SQLite ground truth to pick the next planning node after a commit.

    Cascade (first match wins):
    1. Open scene with work remaining (planned beats, or the scene-advancement
       guard kept it open) → "node_plan_beat".
    2. Remaining scenes in the chapter → "node_plan_chapter".
    3. Remaining chapters in the arc → "node_plan_chapter".
    4. Arcs exhausted, word target unmet → "node_plan_global" (continuation arc).
    5. Manuscript complete → "END".

    Every decision is emitted as one structured JSON line to logs/fsm.log
    (router/destination/reason/fsm_pointer) so a path that loops back into the
    planning cascade can be traced post-mortem.
    """
    from core import runtime
    from core.config_loader import load_config
    from memory import sqlite_db

    config = load_config()
    db = runtime.SQLITE_PATH
    pointer = state["fsm_pointer"]

    # 1. Current scene still open? (covers both planned beats remaining AND the
    #    scene-advancement guard keeping a short scene open for extension.)
    scene = sqlite_db.get_row(db, "Scenes", "scene_id", pointer.scene_id)
    if scene is not None and scene.get("committed_at") is None:
        destination, reason = "node_plan_beat", "scene_open"
    # 2. Remaining scenes in the current chapter (scene_index ASC).
    elif sqlite_db.get_remaining_scenes(db, pointer.chapter_id):
        destination, reason = "node_plan_chapter", "scene_advance"
    # 3. Remaining chapters in the current arc.
    elif sqlite_db.get_remaining_chapters(db, pointer.arc_id):
        destination, reason = "node_plan_chapter", "chapter_advance"
    # 4. Arc exhaustion + word count check.
    elif sqlite_db.get_total_word_count(db) < config.project.word_count_target:
        destination, reason = "node_plan_global", "continuation_arc"
    # 5. Manuscript complete — Export Pipeline.
    else:
        destination, reason = "END", "manuscript_complete"

    logger.info(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "router": "edge_commit_router",
        "destination": destination,
        "reason": reason,
        "fsm_pointer": pointer.model_dump(),
    }))
    return destination

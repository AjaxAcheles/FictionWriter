"""
fsm/nodes/node_freeze_and_escalate.py

Fallback Subgraph — Structural Circuit Breaker (full Sprint 4 implementation).

Purpose:
    Fires autonomously with no human required. Reads escalation_tier from
    OrchestratorState to determine which tier to execute — no tier repeats on
    re-entry. Advances escalation_tier before routing.

    Tier 1 — Constraint Relaxation & Parameter Mutation: writes
        transient_dc_override (0.20) and per-beat generation overrides
        (temperature/seed mutation) into the context package. → revise.
    Tier 2 — Context Stripping: clears the override; drops HNSW flavor and
        scene-level RAPTOR from the package, keeping hard relational ground
        truth (arc summary, threads, Graphiti facts). → revise.
    Tier 3 — Beat Subdivision: splits the failing beat into two 'planned'
        beats (Granularity Floor honored): part 1 carries the first half of
        the entry/exit span, part 2 the remainder. Subsequent beat indices are
        shifted up. retry budget refreshed. → plan_beat (re-selects part 1).
    Tier 4 — Scrap & Replan: abandons the beat, resets retry_count, increments
        replan_count, preserves failed_beat_cache fingerprints.
        replan_count <= cap → plan_beat; > cap → plan_chapter with the paradox/
        failure description injected as a hard planning constraint.
    Last resort (all tiers consumed): pause_requested = True. Headless mode
        converts this to hard_stop_asserted = True, exports best_seen_draft to
        data/exports/, stamps a terminal_escalation event, and halts cleanly.

Architecture role:
    - Called by edge_mode_selector (has_paradox or retry_count > max).
    - freeze_router routes by the tier just executed.
"""

import json
import random
import time
from contextlib import closing
from datetime import datetime, timezone

from core import runtime
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from memory import sqlite_db
from memory.event_log import write_event

logger = get_logger("node_freeze_and_escalate")

TIER1_DC_OVERRIDE = 0.20
GRANULARITY_FLOOR_WORDS = 150  # minimum word budget for a subdivided beat half


def _subdivide_beat(db, scene_id: str, beat_index: int) -> bool:
    """
    Tier 3: split the beat at (scene_id, beat_index) into two planned beats.

    Returns False (no-op) when the beat is missing or too small to honor the
    Granularity Floor in both halves.
    """
    beat = sqlite_db.get_beat_by_index(db, scene_id, beat_index)
    if beat is None:
        return False
    plan = json.loads(beat.get("beat_plan_json") or "{}")
    budget = int(plan.get("word_budget") or 0)
    if budget < GRANULARITY_FLOOR_WORDS * 2:
        return False

    half = budget // 2
    midpoint = f"Midpoint of: {plan.get('exit_constraints') or plan.get('description') or ''}"

    with closing(sqlite_db.get_connection(db)) as conn:
        # Shift subsequent indices up to make room for part 2 (descending order
        # avoids transient index collisions).
        rows = conn.execute(
            "SELECT beat_id, beat_index FROM Beats WHERE scene_id = ? AND beat_index > ? "
            "ORDER BY beat_index DESC",
            (scene_id, beat_index),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE Beats SET beat_index = ? WHERE beat_id = ?",
                (row["beat_index"] + 1, row["beat_id"]),
            )
        conn.commit()

    part1 = {**plan, "word_budget": half, "exit_constraints": midpoint,
             "description": f"{plan.get('description') or ''} (part 1 of 2)"}
    part2 = {**plan, "word_budget": budget - half, "entry_constraints": midpoint,
             "description": f"{plan.get('description') or ''} (part 2 of 2)"}

    sqlite_db.upsert_beat(db, {"beat_id": beat["beat_id"], "scene_id": scene_id,
                               "beat_index": beat_index, "beat_plan_json": json.dumps(part1),
                               "status": "planned"})
    sqlite_db.upsert_beat(db, {"beat_id": f"{beat['beat_id']}_sub", "scene_id": scene_id,
                               "beat_index": beat_index + 1, "beat_plan_json": json.dumps(part2),
                               "status": "planned"})
    return True


def _paradox_constraint_text(state: OrchestratorState) -> str:
    """Render failed_beat_cache fingerprints as a hard planning constraint."""
    fingerprints = state.get("failed_beat_cache") or []
    if not fingerprints:
        return "Previous approaches to this beat failed repeatedly; restructure the chapter to avoid them."
    lines = [
        f"- FORBIDDEN OUTCOME ({f.get('error_code', 'FAILURE')}): {f.get('fingerprint', '')} "
        f"(fix guidance: {f.get('suggested_fix', '')})"
        for f in fingerprints
    ]
    return "Hard planning constraints from failed approaches:\n" + "\n".join(lines)


async def node_freeze_and_escalate(state: OrchestratorState) -> dict:
    """
    Execute the next escalation tier. See module docstring for tier semantics.

    Outputs (merged into OrchestratorState): tier-dependent; always advances
    escalation_tier except at the terminal step.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    config = load_config()
    db = runtime.SQLITE_PATH
    tier = state.get("escalation_tier", 0)
    package = dict(state.get("active_context_package") or {})

    try:
        if tier == 0:
            # Tier 1 — constraint relaxation + parameter mutation (this beat only).
            package["generation_overrides"] = {
                "temperature": round(random.uniform(0.85, 1.05), 2),
                "seed": random.randint(1, 2**31 - 1),
            }
            update = {
                "escalation_tier": 1,
                "transient_dc_override": TIER1_DC_OVERRIDE,
                "has_paradox": False,
                "active_context_package": package,
            }
        elif tier == 1:
            # Tier 2 — context stripping to hard relational ground truth.
            package["hnsw_flavor"] = ""
            package["raptor_scene_summary"] = ""
            package.pop("generation_overrides", None)
            update = {
                "escalation_tier": 2,
                "transient_dc_override": None,
                "has_paradox": False,
                "active_context_package": package,
            }
        elif tier == 2:
            # Tier 3 — beat subdivision (falls through to Tier 4 when impossible).
            if _subdivide_beat(db, pointer.scene_id, pointer.beat_index):
                update = {
                    "escalation_tier": 3,
                    "has_paradox": False,
                    "retry_count": 0,
                    "critic_failures": None,  # fresh audit slate for the halves
                }
            else:
                logger.warning("Tier 3 subdivision impossible (granularity floor) — advancing to Tier 4.")
                update = _tier4(state, config, db, package)
        elif tier == 3:
            update = _tier4(state, config, db, package)
        else:
            # Last resort — all four tiers consumed.
            update = _terminal(state, config)
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "escalated")
        return update
    except Exception as e:
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e))
        raise


def _tier4(state: OrchestratorState, config, db, package: dict) -> dict:
    """Tier 4 — scrap & replan. Fingerprints preserved across the reset."""
    pointer = state["fsm_pointer"]
    beat = sqlite_db.get_beat_by_index(db, pointer.scene_id, pointer.beat_index)
    if beat is not None:
        sqlite_db.upsert_beat(
            db,
            {"beat_id": beat["beat_id"], "scene_id": pointer.scene_id,
             "beat_index": pointer.beat_index, "beat_plan_json": beat.get("beat_plan_json"),
             "status": "abandoned"},
        )
    replan_count = state.get("replan_count", 0) + 1
    update: dict = {
        "escalation_tier": 4,
        "retry_count": 0,
        "replan_count": replan_count,
        "has_paradox": False,
        "critic_failures": None,
        "current_draft_text": "",
    }
    if replan_count > config.generation.replan_count_max:
        # Chapter-level replan with the paradox/failure description as a hard constraint.
        package["paradox_constraint"] = _paradox_constraint_text(state)
        update["active_context_package"] = package
    return update


def _terminal(state: OrchestratorState, config) -> dict:
    """Last resort: pause — converted to a clean hard stop in headless mode."""
    if not config.headless_mode:
        return {"pause_requested": True}

    best = state.get("best_seen_draft") or state.get("current_draft_text") or ""
    pointer = state["fsm_pointer"]
    runtime.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    export_path = runtime.EXPORTS_DIR / f"best_seen_{pointer.scene_id}_beat_{pointer.beat_index}.txt"
    export_path.write_text(best, encoding="utf-8")
    write_event(
        runtime.EVENT_LOG_PATH,
        {
            "type": "terminal_escalation",
            "scene_id": pointer.scene_id,
            "beat_index": pointer.beat_index,
            "export_path": str(export_path),
            "exported_words": len(best.split()),
            "at": datetime.now(timezone.utc).isoformat(),
        },
    )
    logger.warning("headless terminal escalation: best_seen_draft exported to %s", export_path)
    return {"hard_stop_asserted": True, "pause_requested": False}


def freeze_router(state: OrchestratorState) -> str:
    """
    Route by the tier node_freeze_and_escalate just executed.

    Tiers 1-2 → node_revise_prose (relaxed / stripped re-attempt).
    Tier 3   → node_plan_beat (re-selects the subdivided part 1).
    Tier 4   → node_plan_beat (replan_count <= cap) or node_plan_chapter (> cap).
    Terminal → node_human_intervention (interactive) — headless hard stop also
               lands there; the node observes hard_stop_asserted and halts.
    """
    from core.config_loader import load_config
    from routes import control  # lazy: POST /control/pause cannot mutate FSM state

    if state.get("pause_requested") or state.get("hard_stop_asserted") or control.is_paused():
        return "node_human_intervention"
    tier = state.get("escalation_tier", 0)
    if tier in (1, 2):
        return "node_revise_prose"
    if tier == 3:
        return "node_plan_beat"
    if tier == 4:
        config = load_config()
        if state.get("replan_count", 0) > config.generation.replan_count_max:
            return "node_plan_chapter"
        return "node_plan_beat"
    return "node_human_intervention"

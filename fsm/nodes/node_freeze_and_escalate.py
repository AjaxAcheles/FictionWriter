"""
fsm/nodes/node_freeze_and_escalate.py

Fallback Subgraph — Structural Circuit Breaker. SPRINT 3 MINIMAL STUB.

Purpose (full version — Sprint 4):
    Four-tier autonomous recovery: Tier 1 constraint relaxation
    (transient_dc_override), Tier 2 context stripping, Tier 3 beat subdivision,
    Tier 4 scrap-and-replan with replan_count routing, plus the headless
    terminal policy (pause→hard stop conversion with best_seen_draft export).

Sprint 3 behavior:
    Advances escalation_tier and applies Tier 1 (transient_dc_override=0.20)
    on first entry; any further entry sets pause_requested=True (interactive)
    so the vertical slice has a terminal escape hatch. Tiers 2-4 land in
    Sprint 4. The routing function reads escalation_tier from state.
"""

import time

from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState

logger = get_logger("node_freeze_and_escalate")

TIER1_DC_OVERRIDE = 0.20


async def node_freeze_and_escalate(state: OrchestratorState) -> dict:
    """Sprint 3: Tier 1 relaxation on first entry; pause on re-entry."""
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    tier = state.get("escalation_tier", 0)
    try:
        if tier == 0:
            update = {"escalation_tier": 1, "transient_dc_override": TIER1_DC_OVERRIDE,
                      "has_paradox": False, "retry_count": 0}
            outcome = "escalated"
        else:
            update = {"escalation_tier": tier + 1, "pause_requested": True}
            outcome = "escalated"
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, outcome)
        return update
    except Exception as e:
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e))
        raise


def freeze_router(state: OrchestratorState) -> str:
    """
    Post-escalation routing (Sprint 3 subset).

    Tier 1 (just applied) → node_revise_prose for the relaxed re-attempt.
    Re-entry (pause set) → node_human_intervention.
    """
    if state.get("pause_requested"):
        return "node_human_intervention"
    return "node_revise_prose"

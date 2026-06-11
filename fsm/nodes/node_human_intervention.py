"""
fsm/nodes/node_human_intervention.py

Manual Override Interface. SPRINT 3 MINIMAL STUB.

Purpose (full version — Sprint 4):
    Unlocks the Quart UI editor on pause_requested/hard_stop_asserted, applies
    manual prose/parameter patches, and runs the post-edit autonomous
    reconciliation pass (LLM extraction diff against Threads + Graphiti) before
    re-auditing — with HUMAN_EDIT_UNRECONCILED audit logging on failure.

Sprint 3 behavior:
    Terminal no-op: logs the intervention request and clears pause_requested so
    test invocations terminate cleanly. The FSM never blocks on a human.
"""

import time

from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState

logger = get_logger("node_human_intervention")


async def node_human_intervention(state: OrchestratorState) -> dict:
    """Sprint 3 stub: acknowledge and clear the pause request."""
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    logger.info("human intervention requested at %s (Sprint 3 stub: acknowledging)", pointer)
    log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "success")
    return {"pause_requested": False}

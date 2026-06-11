"""
fsm/nodes/node_craft_consultant.py

Fallback Subgraph — Craft Consultant deadlock breaker. SPRINT 3 MINIMAL STUB.

Purpose (full version — Sprint 4):
    Queries the craft_consultant endpoint for a structural craft diagnosis to
    break looping hallucinations, injecting a meta-directive into the active
    context package consumed by node_revise_prose.

Sprint 3 behavior:
    Injects a deterministic generic diagnosis (no LLM call) so the routing
    topology and the craft_diagnosis prompt path are exercised end-to-end.
"""

import time

from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState

logger = get_logger("node_craft_consultant")

_GENERIC_DIAGNOSIS = (
    "Structural diagnosis (generic): the revision loop has stalled. Re-approach the "
    "beat from its exit constraints backward — write the final image first, then the "
    "minimum causal chain that reaches it. Cut any sentence not on that chain."
)


async def node_craft_consultant(state: OrchestratorState) -> dict:
    """Inject a craft diagnosis meta-directive into the context package."""
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    package = dict(state.get("active_context_package") or {})
    package["craft_diagnosis"] = _GENERIC_DIAGNOSIS
    log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "success")
    return {"active_context_package": package}

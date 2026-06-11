"""
fsm/routers/edge_mode_selector.py

Primary quality-gate conditional edge — routes after node_adversarial_critics.

Purpose:
    Evaluates the health of the current drafted beat and returns a routing string
    that LangGraph uses to select the next node. This is the central decision point
    of the revision loop: it determines whether a beat is clean enough to commit,
    needs revision, is deadlocked and needs consultant intervention, or is
    structurally broken and requires escalation.

    Routing logic (strict order — first match wins):
    0. has_paradox is True → "node_freeze_and_escalate"
       Paradoxes are structural failures, not quality failures. The revision loop
       cannot fix a THREAD_PARADOX; escalation is mandatory and immediate.
    1. critic_failures is empty AND Dc < threshold → "node_commit_transaction"
       Threshold is transient_dc_override if set (Tier 1 relaxation active),
       else config thresholds.stel_cosine_distance (proposed default 0.12).
    2. retry_count > retry_count_max (default 5) → "node_freeze_and_escalate"
    3. retry_count > craft_consultant_threshold (default 3) → "node_craft_consultant"
    4. else (failures exist, retry_count <= 3) → "node_revise_prose"

    Note on Burrows' Delta: computed by node_programmatic_audit and surfaced as
    telemetry on the UI stylometric drift graph. It does NOT influence routing here —
    only STEL cosine distance (Dc) gates the commit path.

Architecture role:
    - Registered in fsm/graph.py as the conditional edge after node_adversarial_critics.
    - Reads OrchestratorState but writes nothing — returns a routing string only.
    - config is accessed via the app.config["APP_CONFIG"] singleton (injected at
      module level or passed as a closure) to read threshold values.
"""

from fsm.state import OrchestratorState


def edge_mode_selector(state: OrchestratorState) -> str:
    """
    Route after node_adversarial_critics — strict first-match-wins ordering.

    0. has_paradox → "node_freeze_and_escalate" (structural; bypasses retry gate).
    1. no failures AND Dc < threshold → "node_commit_transaction"
       (threshold = transient_dc_override when set, else config Dc).
    2. retry_count > generation.retry_count_max (5) → "node_freeze_and_escalate".
    3. retry_count > generation.craft_consultant_threshold (3) → "node_craft_consultant".
    4. else → "node_revise_prose".
    """
    from core.config_loader import load_config

    config = load_config()

    if state.get("has_paradox"):
        return "node_freeze_and_escalate"

    override = state.get("transient_dc_override")
    threshold = override if override is not None else config.thresholds.stel_cosine_distance
    failures = state.get("critic_failures") or []
    if not failures and state.get("stylometric_distance", 0.0) < threshold:
        return "node_commit_transaction"

    retry_count = state.get("retry_count", 0)
    if retry_count > config.generation.retry_count_max:
        return "node_freeze_and_escalate"
    if retry_count > config.generation.craft_consultant_threshold:
        return "node_craft_consultant"
    return "node_revise_prose"

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
    Evaluate draft health and return the next node name.

    Purpose:
        Implements the five-branch routing decision described in the module
        docstring. All threshold comparisons use config values read from
        AppConfig.thresholds (stel_cosine_distance, retry_count_max,
        craft_consultant_threshold). transient_dc_override from state supersedes
        the config Dc threshold when set (Tier 1 relaxation active).

    Inputs:
        state['has_paradox']: bool — if True, bypass all other checks and escalate.
        state['critic_failures']: List[FailureObject] — empty = clean draft.
        state['stylometric_distance']: float — STEL cosine Dc (stubbed 0.0 Sprint 1–4).
        state['transient_dc_override']: float | None — Tier 1 relaxed threshold.
        state['retry_count']: int — number of revision attempts this beat.

    Outputs:
        str: One of:
            "node_commit_transaction"   — clean draft, commit it.
            "node_revise_prose"         — failures present, retry_count within budget.
            "node_craft_consultant"     — deadlock threshold exceeded (retry > 3).
            "node_freeze_and_escalate"  — paradox detected or retry budget exhausted.
    """
    pass

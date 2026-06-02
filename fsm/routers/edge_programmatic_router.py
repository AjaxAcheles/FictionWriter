"""
fsm/routers/edge_programmatic_router.py

Fast-path bypass router — routes immediately after node_programmatic_audit.

Purpose:
    A LangGraph conditional edge that short-circuits the expensive LLM critic
    committee for drafts that are clearly clean. When the fast path fires, it
    routes directly to node_commit_transaction, saving approximately 3 LLM
    inference calls per beat. This is the primary latency mitigation for
    high-volume generation runs.

    Routing logic (first match wins):
    - FAST PATH: retry_count == 0 AND len(critic_failures) == 0 AND
      stylometric_distance < (Dc_threshold * fast_path_multiplier)
      → "node_commit_transaction"
      The 0.7 multiplier (config thresholds.programmatic_fast_path_multiplier)
      provides a safety margin, ensuring fast path only fires on clearly clean
      drafts well below the routing threshold. If transient_dc_override is set,
      it is used as the base threshold instead of the config value.
    - STANDARD PATH: otherwise → "node_adversarial_critics"

Architecture role:
    - Registered in fsm/graph.py as the conditional edge after node_programmatic_audit.
    - Intentionally simple: if in any doubt (non-zero retry_count, any failures,
      or Dc above the multiplied threshold), always routes to node_adversarial_critics.
    - The fast path is a performance optimization only — correctness is never
      sacrificed. A missed slop issue costs one revision cycle; an incorrect
      commit costs a chapter-level replan.
"""

from fsm.state import OrchestratorState


def edge_programmatic_router(state: OrchestratorState) -> str:
    """
    Determine whether to fast-path to commit or run the full critic committee.

    Purpose:
        Implements the two-branch routing logic described in the module docstring.
        Reads thresholds from AppConfig (injected at module level or via closure).
        Uses transient_dc_override from state when set (Tier 1 relaxation active).

    Inputs:
        state['retry_count']: int — must be 0 for fast path to trigger.
        state['critic_failures']: List[FailureObject] — must be empty for fast path.
        state['stylometric_distance']: float — STEL Dc; compared against
            threshold * programmatic_fast_path_multiplier.
        state['transient_dc_override']: float | None — if set, used as base
            threshold instead of config stel_cosine_distance.

    Outputs:
        str: One of:
            "node_commit_transaction" — fast path; draft is clearly clean.
            "node_adversarial_critics" — standard path; run the critic committee.
    """
    pass

"""
fsm/nodes/node_craft_consultant.py

Fallback Subgraph — Craft Consultant deadlock breaker (full Sprint 4).

Purpose:
    When retry_count exceeds the craft threshold, queries the specialized
    craft_consultant endpoint for a structural diagnosis of the failure
    pattern, injecting it as a meta-directive into active_context_package
    ("craft_diagnosis") consumed by node_revise_prose.

    Error path: one retry; on a second failure the deterministic generic
    diagnosis is injected instead (logged at WARNING). The node never raises
    for transport failures — a missing diagnosis must not deepen a deadlock.

Architecture role:
    - Triggered by edge_mode_selector (retry_count in (4, 5)).
    - Yields to node_revise_prose. Prompt: prompts/node_craft_consultant.xml.j2.
"""

import json
import time

from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from llm import call_llm as call_llm_module
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_craft_consultant")

GENERIC_DIAGNOSIS = (
    "Structural diagnosis (generic fallback): the revision loop has stalled. "
    "Re-approach the beat from its exit constraints backward — write the final "
    "image first, then the minimum causal chain that reaches it. Cut any sentence "
    "not on that chain."
)


async def node_craft_consultant(state: OrchestratorState) -> dict:
    """
    Obtain a craft diagnosis and inject it into the context package.

    Outputs (merged into OrchestratorState):
        active_context_package: with "craft_diagnosis" set.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    config = load_config()
    package = dict(state.get("active_context_package") or {})
    failures = [
        f.model_dump() if hasattr(f, "model_dump") else dict(f)
        for f in (state.get("critic_failures") or [])
    ]

    prompt = PromptLoader().load_and_render(
        "node_craft_consultant.xml.j2",
        {
            "current_draft": state.get("current_draft_text", ""),
            "failures": json.dumps(failures),
            "retry_count": state.get("retry_count", 0),
            "beat_entry_constraints": package.get("beat_entry_constraints", ""),
            "beat_exit_constraints": package.get("beat_exit_constraints", ""),
            "pad_behavioral_constraints": package.get("pad_behavioral_constraints", ""),
        },
    )

    diagnosis = ""
    for attempt in range(2):
        try:
            diagnosis = await call_llm_module.collect_llm_response(
                config.endpoints.craft_consultant,
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                stream=False,
            )
            if diagnosis and diagnosis.strip():
                diagnosis = diagnosis.strip()
                break
            diagnosis = ""
        except Exception as e:  # noqa: BLE001 — diagnosis is best-effort by contract
            logger.warning("craft consultant attempt %d failed: %r", attempt + 1, e)
    if not diagnosis:
        logger.warning("craft consultant fell back to generic diagnosis.")
        diagnosis = GENERIC_DIAGNOSIS

    package["craft_diagnosis"] = diagnosis
    log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "success")
    return {"active_context_package": package}

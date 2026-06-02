"""
fsm/nodes/node_craft_consultant.py

Deadlock Breaker Node — Structural Craft Diagnosis (inside Fallback_Subgraph).

Purpose:
    Fires when the FSM is trapped in a revision loop beyond the craft_consultant_threshold
    (config.generation.craft_consultant_threshold, default 3). Queries a specialized
    craft-focused model (config.endpoints.craft_consultant — small-to-mid-tier, optimized
    for diagnostic reasoning) for a structural craft diagnosis of the current draft's
    failures. The diagnosis payload is injected into active_context_package as a
    meta-directive so that node_revise_prose can use it to break the looping hallucination.

    Example diagnosis: "The dialogue feels flat because the PAD dominance score of
    character X is not being reflected in their word choice. Consider more direct,
    assertive phrasing in the attribution verbs."

    This node does not modify the prose itself — it only produces a diagnostic payload.
    node_revise_prose receives the updated context package and uses the craft diagnosis
    alongside the FailureObject list for its revision prompt.

Architecture role:
    - Part of the Fallback_Subgraph (logical grouping; not a separate LangGraph subgraph).
    - Triggered by edge_mode_selector when retry_count > craft_consultant_threshold.
    - Always followed by node_revise_prose — the diagnosis is immediately actionable.
    - If node_revise_prose still fails after craft consultation, edge_mode_selector
      routes to node_freeze_and_escalate on the next cycle (retry_count will exceed max).
    - Uses: call_llm() with config.endpoints.craft_consultant endpoint.
    - Loads prompt from: prompts/node_craft_consultant.xml.j2 via PromptLoader.
    - Emits a structured JSON log entry via get_logger("node_craft_consultant").
"""

import time

from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_craft_consultant")


async def node_craft_consultant(state: OrchestratorState) -> dict:
    """
    Generate a structural craft diagnosis and inject it into the context package.

    Purpose:
        Sends current_draft_text and the accumulated critic_failures to the
        craft_consultant endpoint. The model produces a prescriptive diagnosis
        explaining WHY the draft is failing (not just what to fix). The diagnosis
        is appended to active_context_package under a "craft_diagnosis" key for
        node_revise_prose to include in its revision prompt.

    Inputs (from OrchestratorState):
        state['current_draft_text']: str — the draft that has failed repeated revision.
        state['critic_failures']: List[FailureObject] — accumulated failure history
            that the craft consultant diagnoses.

    Outputs (dict merged into OrchestratorState):
        active_context_package: dict — the existing package with a "craft_diagnosis"
            key added containing the model's prescriptive diagnosis string.

    Relationships:
        - Triggered by: edge_mode_selector (retry_count > craft_consultant_threshold,
          i.e., retry_count is 4 or 5).
        - Yields to: node_revise_prose (direct edge in graph.py).
        - Uses: call_llm() with config.endpoints.craft_consultant endpoint.
        - Prompt: prompts/node_craft_consultant.xml.j2
    """
    pass

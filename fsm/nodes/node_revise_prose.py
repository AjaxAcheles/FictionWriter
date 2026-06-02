"""
fsm/nodes/node_revise_prose.py

Prose Revision Node — Verbatim-Text Targeted Draft Correction.

Purpose:
    Prompts the drafter LLM with the accumulated FailureObject list to produce
    a targeted correction of the current draft. Uses verbatim-text targeting
    (FailureObject.offending_text) rather than integer character spans to
    locate the offending passage, eliminating coordinate hallucination from
    the model.

    Revision targeting cascade:
    1. str.find(offending_text) — exact substring match.
    2. Fuzzy match (difflib SequenceMatcher or similar) — if exact fails.
    3. Full-beat rewrite using the full failure list as guidance — if the substring
       cannot be located (hallucinated offending_text).

    Two-tier token budget management:
    - Tier A: Run the same drop-priority pruning as node_assemble_context. Drop
      HNSW flavor first, then RAPTOR scene-level summaries. Run tokenizer check
      using EndpointConfig.tokenizer_family routing.
    - Tier B: If still over budget after Tier A, drop to a lean context set:
      current_draft_text + critic_failures + craft diagnosis (if present) +
      the active Beat's SQLite entry (entry/exit constraints, PAD target).
      No RAPTOR, no HNSW.

    If node_craft_consultant has run, its diagnosis payload is included in the
    revision context as a meta-directive to help break looping hallucinations.

Architecture role:
    - Loops back to node_programmatic_audit after every revision — all corrected
      text must pass the full audit gauntlet again.
    - Increments retry_count on each invocation. Clears critic_failures to prepare
      for re-validation (the next audit pass builds a fresh failure list).
    - Uses: call_llm() with config.endpoints.drafter endpoint.
    - Loads prompt from: prompts/node_revise_prose.xml.j2 via PromptLoader.
    - Emits a structured JSON log entry via get_logger("node_revise_prose").
"""

import time

from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_revise_prose")


async def node_revise_prose(state: OrchestratorState) -> dict:
    """
    Apply targeted corrections to the current draft based on FailureObject list.

    Purpose:
        Renders the revision prompt with the offending_text targets, suggested fixes,
        and optional craft consultant diagnosis. Applies two-tier token budget pruning
        before firing the LLM call. Locates each offending passage via str.find() then
        fuzzy match then full-beat rewrite fallback. Returns the corrected text and
        resets the critic_failures list for re-validation.

    Inputs (from OrchestratorState):
        state['current_draft_text']: str — the prose to correct.
        state['critic_failures']: List[FailureObject] — offending_text + suggested_fix
            targeting info for each identified issue.
        state['active_context_package']: dict — used for Tier A pruning decisions.
        state['retry_count']: int — current revision count (returned incremented by 1).
        [Reads from SQLite: active Beat row (Tier B fallback — entry/exit constraints,
         PAD target)]
        [Reads from AppConfig: endpoints.drafter, thresholds for token budget math]

    Outputs (dict merged into OrchestratorState):
        current_draft_text: str — the corrected prose.
        retry_count: int — incremented by 1.
        critic_failures: List[FailureObject] — cleared to [] to prepare for re-audit.

    Relationships:
        - Triggered by: edge_mode_selector (failures present, retry_count <= 3).
        - Yields to: node_programmatic_audit (loops back — corrected text must
          pass the full gauntlet again).
        - Uses: call_llm() with config.endpoints.drafter endpoint.
        - Prompt: prompts/node_revise_prose.xml.j2
    """
    pass

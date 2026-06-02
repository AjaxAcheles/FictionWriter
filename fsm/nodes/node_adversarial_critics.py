"""
fsm/nodes/node_adversarial_critics.py

Stage 2 Critics — LLM Adversarial Critic Committee.

Purpose:
    Executes three independent LLM critic calls (Continuity, Dialogue, Pacing) that
    evaluate the current draft against factual, stylistic, and structural standards.
    Each critic is grammar-constrained to return FailureObject instances; output is
    validated by FailureObject.model_validate() with retry on schema violation.

    Execution mode (controlled by EndpointConfig.supports_concurrent_critics):
    - False: Critics run as sequential async calls. Hard policy for endpoints where
      concurrent calls would exhaust available inference resources.
    - True: Critics run via asyncio.gather for full parallel throughput.

    Grammar constraint strategy (EndpointConfig.grammar_constraint_strategy):
    - "gbnf": sampler-level GBNF grammar enforcement via gbnf_compiler.py.
    - "json_mode": vendor JSON mode. Both paths followed by model_validate() retry.

    Thread-consistency check (Continuity critic only):
    The Continuity critic queries the active Threads table from SQLite and verifies
    the draft does not contradict any open Thread's required resolution. If a
    contradiction is detected (e.g., character death with an unresolved Thread):
    - Emits FailureObject with error_code="THREAD_PARADOX".
    - Sets has_paradox=True in the returned state dict.
    - Writes the paradox fingerprint to failed_beat_cache.
    This causes edge_mode_selector to bypass the retry_count gate and route
    immediately to node_freeze_and_escalate.

    The pause_requested flag is checked between critic calls (or after asyncio.gather
    in concurrent mode), giving the human override a clean intercept point.

Architecture role:
    - Most expensive node in the revision loop (~3 LLM calls per beat).
    - _programmatic_router fast path exists specifically to skip this node on clean drafts.
    - Each critic's reasoning is streamed to the UI via SSE for transparency.
    - Uses: call_llm() with config.endpoints.critic endpoint.
    - Prompts: prompts/node_adversarial_critics_continuity.xml.j2,
               prompts/node_adversarial_critics_dialogue.xml.j2,
               prompts/node_adversarial_critics_pacing.xml.j2
    - Emits a structured JSON log entry via get_logger("node_adversarial_critics").
"""

import asyncio
import time

from core.logger import get_logger, log_node_event
from fsm.state import FailureObject, OrchestratorState
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_adversarial_critics")


async def _run_continuity_critic(
    draft_text: str, context_package: dict
) -> list[FailureObject]:
    """
    Run the Continuity critic LLM call with Thread-consistency check.

    Purpose:
        Sends the draft and context to the critic endpoint with the continuity
        prompt. Grammar-constrains output to FailureObject schema. Validates with
        model_validate(); retries on schema violation. Also queries SQLite Threads
        table to verify no open Thread's required resolution is contradicted.
        Emits a THREAD_PARADOX FailureObject if a contradiction is found.

    Inputs:
        draft_text: str — the current beat's prose text.
        context_package: dict — assembled context including factual reference
            (character states, item locations, thread statuses).

    Outputs:
        List[FailureObject]: Zero or more continuity failures. May include a
            THREAD_PARADOX entry if an open Thread contradiction is detected.
    """
    pass


async def _run_dialogue_critic(
    draft_text: str, context_package: dict
) -> list[FailureObject]:
    """
    Run the Dialogue critic LLM call.

    Purpose:
        Evaluates dialogue for character voice consistency, subtext, and PAD
        dominance reflection in word choice. Grammar-constrains output to
        FailureObject schema. Validates with model_validate(); retries on violation.
        Streams reasoning to the UI via SSE.

    Inputs:
        draft_text: str — the current beat's prose text.
        context_package: dict — includes character PAD states and style store baselines.

    Outputs:
        List[FailureObject]: Zero or more dialogue failures.
    """
    pass


async def _run_pacing_critic(
    draft_text: str, context_package: dict
) -> list[FailureObject]:
    """
    Run the Pacing critic LLM call.

    Purpose:
        Evaluates scene rhythm, tension curve trajectory, and word-budget adherence
        against the beat's planned entry/exit constraints. Grammar-constrains output
        to FailureObject schema. Validates with model_validate(); retries on violation.

    Inputs:
        draft_text: str — the current beat's prose text.
        context_package: dict — includes beat constraints, word budget, and
            PAD target values.

    Outputs:
        List[FailureObject]: Zero or more pacing failures.
    """
    pass


async def node_adversarial_critics(state: OrchestratorState) -> dict:
    """
    Run the three-critic committee and return all discovered failures.

    Purpose:
        Orchestrates the three critic functions (continuity, dialogue, pacing)
        in either sequential or concurrent mode based on EndpointConfig.
        Checks pause_requested between calls. Collects all FailureObjects and
        checks for THREAD_PARADOX entries to set has_paradox flag. Returns
        all findings for appending to critic_failures via the operator.add reducer.

    Inputs (from OrchestratorState):
        state['current_draft_text']: str — prose to critique.
        state['active_context_package']: dict — factual reference for critics.
        state['pause_requested']: bool — checked between critic calls.
        [Reads from AppConfig: endpoints.critic (concurrent mode, grammar strategy)]
        [Reads from SQLite: Threads table (continuity critic only)]

    Outputs (dict merged into OrchestratorState):
        critic_failures: List[FailureObject] — new failures to append.
        has_paradox: bool — True if any FailureObject has error_code="THREAD_PARADOX".
        failed_beat_cache: List[Dict] — THREAD_PARADOX fingerprint appended if paradox.

    Relationships:
        - Triggered by: edge_programmatic_router (standard path).
        - Yields to: edge_mode_selector (conditional edge in graph.py).
        - Uses: call_llm() with config.endpoints.critic, asyncio.gather or sequential.
    """
    pass

"""
fsm/nodes/node_adversarial_critics.py

Stage 2 Critics — three grammar-constrained LLM critic calls.

Purpose:
    Executes the Continuity, Dialogue, and Pacing critics against the current
    draft. Execution mode follows EndpointConfig.supports_concurrent_critics:
    False → strict serial execution (hard policy for resource-constrained
    endpoints); True → asyncio.gather for full parallel throughput.

    Every critic call is grammar-constrained to the FailureObject schema via
    call_llm_structured (GBNF or vendor JSON mode per endpoint). A critic that
    finds no fault returns error_code "NONE" — these sentinels are filtered out
    and never appended to critic_failures.

    Thread-consistency check (Continuity critic): the open Threads table is
    injected into the continuity prompt; a contradiction with an open Thread's
    required resolution yields error_code THREAD_PARADOX, which sets
    has_paradox=True and writes a compact fingerprint to failed_beat_cache.

    pause_requested is checked between serial critic calls (and once after
    gather completes in concurrent mode) as the human-override intercept point.

Architecture role:
    - Triggered by edge_programmatic_router (standard path).
    - Yields to edge_mode_selector.
    - StructuredOutputError from the adapter layer propagates upward as a hard
      error for the FSM escalation ladder — the critics never loop indefinitely.
"""

import asyncio
import json
import time

from core import runtime, stream_bus
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.state import FailureObject, OrchestratorState
from llm import call_llm as call_llm_module
from memory import sqlite_db
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_adversarial_critics")

CRITICS = ("continuity", "dialogue", "pacing")


async def _run_critic(critic: str, config, draft: str, package: dict, threads: list[dict]) -> FailureObject:
    """Fire one grammar-constrained critic call and return its FailureObject."""
    # PromptLoader runs StrictUndefined — every template variable must be present.
    context = {
        "current_draft": draft,
        "open_threads": json.dumps(threads),
        "actual_word_count": len(draft.split()),
        "scene_word_budget": package.get("scene_word_budget", package.get("beat_word_budget", 0)),
        "graphiti_facts": package.get("graphiti_facts", ""),
        "character_states": package.get("character_states", ""),
        "epistemic_beliefs": package.get("epistemic_beliefs", ""),
        "pad_behavioral_constraints": package.get("pad_behavioral_constraints", ""),
        "beat_entry_constraints": package.get("beat_entry_constraints", ""),
        "beat_exit_constraints": package.get("beat_exit_constraints", ""),
        "beat_word_budget": package.get("beat_word_budget", 0),
        "author_style_baseline": package.get("author_style_baseline", ""),
        "raptor_chapter_summary": package.get("raptor_chapter_summary", ""),
    }
    prompt = PromptLoader().load_and_render(f"node_adversarial_critics_{critic}", context)
    failure = await call_llm_module.call_llm_structured(
        config.endpoints.critic,
        [{"role": "user", "content": prompt}],
        FailureObject,
        retry_cap=config.model_validate_retry_cap,
    )
    stream_bus.publish({"type": "critic_result", "critic": critic, "error_code": failure.error_code})
    return failure


async def node_adversarial_critics(state: OrchestratorState) -> dict:
    """
    Run the three-critic committee against current_draft_text.

    Outputs (merged into OrchestratorState):
        critic_failures: real failures (NONE sentinels filtered) — append reducer.
        has_paradox: True when any THREAD_PARADOX was emitted.
        failed_beat_cache: paradox fingerprints (append reducer) when applicable.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    config = load_config()
    draft = state["current_draft_text"]
    package = state.get("active_context_package") or {}
    threads = sqlite_db.get_open_threads(runtime.SQLITE_PATH)

    try:
        results: list[FailureObject] = []
        if config.endpoints.critic.supports_concurrent_critics:
            results = list(
                await asyncio.gather(
                    *(_run_critic(c, config, draft, package, threads) for c in CRITICS)
                )
            )
            # Single intercept point after the gather completes (cloud mode).
            _ = state.get("pause_requested")
        else:
            for critic in CRITICS:
                if state.get("pause_requested"):
                    logger.info("pause_requested set — halting critic sequence after %d critics", len(results))
                    break
                results.append(await _run_critic(critic, config, draft, package, threads))

        failures = [f for f in results if f.error_code.upper() != "NONE"]
        paradoxes = [f for f in failures if f.error_code == "THREAD_PARADOX"]

        update: dict = {"critic_failures": failures}
        if paradoxes:
            update["has_paradox"] = True
            update["failed_beat_cache"] = [
                {
                    "beat_id": f"{pointer.scene_id}_beat_{pointer.beat_index}",
                    "error_code": p.error_code,
                    "fingerprint": p.offending_text[:120],
                    "suggested_fix": p.suggested_fix[:200],
                }
                for p in paradoxes
            ]

        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return update
    except Exception as e:
        log_node_event(
            logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e)
        )
        raise

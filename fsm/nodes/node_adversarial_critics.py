"""
fsm/nodes/node_adversarial_critics.py

Stage 2 Critics — three agentic, grammar-constrained LLM critics.

Purpose:
    Executes the Continuity, Dialogue, and Pacing critics against the current
    draft. Execution mode follows EndpointConfig.supports_concurrent_critics:
    False → strict serial execution (hard policy for resource-constrained
    endpoints); True → asyncio.gather for full parallel throughput.

    Each critic runs as a bounded "pull" agent rather than a one-shot call. It
    is given the memory-retrieval tools from fsm/critic_tools.py and may call
    them — RAPTOR summaries, the temporal knowledge graph, prior committed prose
    — to verify a suspected continuity/lore/fact error before judging. The loop
    runs at most MAX_AGENT_ITERATIONS turns: each turn streams the model's inner
    monologue (agent_thought) to the dashboard, and any tool call is published
    (agent_action), executed via execute_critic_tool, and its result published
    (agent_observation) before being fed back as a tool-role message. The runtime
    ToolContext (db_path, scene_id, active_event_id, chapter_id) is injected by
    this node so the model can never point a tool at the wrong scene.

    After the agent loop settles, the accumulated transcript is closed with a
    final-verdict instruction and sent through the EXISTING grammar-constrained
    call_llm_structured path (FailureObject, GBNF or vendor JSON mode per
    endpoint). A critic that finds no fault returns error_code "NONE" — these
    sentinels are filtered out and never appended to critic_failures.

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
from fsm.critic_tools import CRITIC_TOOL_SCHEMAS, ToolContext, execute_critic_tool
from fsm.state import FSM_Pointer, FailureObject, OrchestratorState
from llm import call_llm as call_llm_module
from memory import sqlite_db
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_adversarial_critics")

CRITICS = ("continuity", "dialogue", "pacing")

# Hard ceiling on tool-calling turns per critic before the forced final verdict.
MAX_AGENT_ITERATIONS = 4

# System instruction prepended to every critic's agentic transcript.
_AGENT_SYSTEM_TEMPLATE = "node_adversarial_critics_agent_system.xml.j2"


def _summarize_tool_result(result: str) -> str:
    """Short human-readable digest of a raw tool result string for the bus."""
    return result if len(result) <= 120 else result[:120] + "..."


def _pause_flagged(state: OrchestratorState) -> bool:
    """
    True when a pause is pending — from FSM state OR the module-level flag set
    by POST /control/pause. The route cannot mutate LangGraph state mid-run, so
    the intercept points must consult both sources.
    """
    if state.get("pause_requested"):
        return True
    from routes import control  # lazy import: keeps fsm import-time independent of routes

    return control.is_paused()


def _build_tool_context(pointer: FSM_Pointer) -> ToolContext:
    """Construct the runtime-scoped ToolContext for one critic from FSM state."""
    return ToolContext(
        db_path=runtime.SQLITE_PATH,
        scene_id=pointer.scene_id,
        active_event_id=f"{pointer.scene_id}_beat_{pointer.beat_index}",
        chapter_id=getattr(pointer, "chapter_id", None),
    )


async def _run_critic(
    critic: str, config, draft: str, package: dict, threads: list[dict], pointer: FSM_Pointer
) -> FailureObject:
    """
    Run one critic as a bounded "pull" agent, then issue its final verdict.

    The critic may call the memory-retrieval tools (CRITIC_TOOL_SCHEMAS) for up
    to MAX_AGENT_ITERATIONS turns to verify suspected errors before the existing
    grammar-constrained FailureObject call closes the transcript. The inner
    monologue, tool actions, and tool observations stream to the dashboard.
    """
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
    loader = PromptLoader()
    system_prompt = loader.load_and_render(_AGENT_SYSTEM_TEMPLATE, {})
    critic_prompt = loader.load_and_render(f"node_adversarial_critics_{critic}.xml.j2", context)
    ctx = _build_tool_context(pointer)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": critic_prompt},
    ]

    for _ in range(MAX_AGENT_ITERATIONS):
        content_parts: list[str] = []
        tool_calls: list[dict] = []
        async for event in call_llm_module.call_llm(
            config.endpoints.critic, messages, tools=CRITIC_TOOL_SCHEMAS
        ):
            if event["type"] == "content":
                content_parts.append(event["text"])
            elif event["type"] == "tool_calls":
                tool_calls = event["tool_calls"]

        content = "".join(content_parts)
        if content:
            stream_bus.publish({"type": "agent_thought", "critic": critic, "text": content})

        if not tool_calls:
            if content:
                messages.append({"role": "assistant", "content": content})
            break

        messages.append({"role": "assistant", "content": content or None, "tool_calls": tool_calls})
        for call in tool_calls:
            name = call["function"]["name"]
            raw_args = call["function"].get("arguments", "")
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {}
            stream_bus.publish({"type": "agent_action", "critic": critic, "tool": name, "args": args})

            result = await execute_critic_tool(name, args, ctx)
            stream_bus.publish(
                {
                    "type": "agent_observation",
                    "critic": critic,
                    "tool": name,
                    "summary": _summarize_tool_result(result),
                    "result_chars": len(result),
                }
            )
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    messages.append(
        {
            "role": "user",
            "content": (
                "Based only on the draft and any verified tool results above, now issue "
                "your final verdict as a single FailureObject JSON object."
            ),
        }
    )
    failure = await call_llm_module.call_llm_structured(
        config.endpoints.critic,
        messages,
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
                    *(_run_critic(c, config, draft, package, threads, pointer) for c in CRITICS)
                )
            )
            # Single intercept point after the gather completes (cloud mode).
            _ = _pause_flagged(state)
        else:
            for critic in CRITICS:
                if _pause_flagged(state):
                    logger.info("pause_requested set — halting critic sequence after %d critics", len(results))
                    break
                results.append(await _run_critic(critic, config, draft, package, threads, pointer))

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

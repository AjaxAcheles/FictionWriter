"""
fsm/nodes/node_revise_prose.py

Targeted Revision Node — verbatim-text failure correction.

Purpose:
    Prompts the drafter with the FailureObject list to produce a targeted
    correction. Verbatim targeting sequence per failure:
    1. str.find() on offending_text (exact).
    2. difflib-based fuzzy match (ratio >= 0.8 over a sliding window).
    3. If no failure can be located at all → full-beat rewrite mode (the model
       rewrites the whole beat guided by the failure list).

    Two-tier revision token budget:
    - Tier A: drop-priority pruning of the context package (hnsw_flavor first,
      then raptor_scene_summary) + tokenizer check via EndpointConfig routing.
    - Tier B: lean set — current draft + failure list + craft diagnosis (if
      present) + the active beat's SQLite entry only.

Architecture role:
    - Triggered by edge_mode_selector (or node_craft_consultant).
    - Loops back to node_programmatic_audit.
    - Increments retry_count; clears critic_failures (None sentinel for the
      append_or_clear reducer).
"""

import difflib
import json
import time

from core import runtime
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from llm import call_llm as call_llm_module
from llm.tokenizer import fits_in_budget
from memory import sqlite_db
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_revise_prose")

FUZZY_THRESHOLD = 0.8


def locate_offending_text(draft: str, offending: str) -> bool:
    """
    True when offending text is locatable in the draft (exact, then fuzzy).

    Fuzzy pass: slides a window of len(offending) across the draft in
    half-window steps and accepts any SequenceMatcher ratio >= FUZZY_THRESHOLD.
    """
    if not offending:
        return False
    if draft.find(offending) != -1:
        return True
    window = len(offending)
    if window == 0 or len(draft) < window // 2:
        return False
    step = max(1, window // 2)
    matcher = difflib.SequenceMatcher(a=offending.lower())
    for i in range(0, max(1, len(draft) - window // 2), step):
        matcher.set_seq2(draft[i : i + window].lower())
        if matcher.ratio() >= FUZZY_THRESHOLD:
            return True
    return False


def _tier_a_context(package: dict, endpoint, draft: str, failures_text: str) -> str:
    """Tier A: drop-priority pruned package flattened to a context block."""
    pruned = dict(package)
    for drop_key in ("hnsw_flavor", "raptor_scene_summary"):
        text = "\n".join(str(v) for v in pruned.values()) + draft + failures_text
        if fits_in_budget(
            text,
            endpoint.context_window,
            endpoint.reserved_output_tokens,
            endpoint.tokenizer_family,
            endpoint.model_name,
        ):
            break
        pruned[drop_key] = ""
    return "\n".join(f"{k}: {v}" for k, v in pruned.items() if v)


async def node_revise_prose(state: OrchestratorState) -> dict:
    """
    Produce a corrected draft from the failure list.

    Outputs (merged into OrchestratorState):
        current_draft_text: corrected prose.
        retry_count: incremented by 1.
        critic_failures: None — explicit clear via append_or_clear reducer.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    config = load_config()
    endpoint = config.endpoints.drafter
    draft = state["current_draft_text"]
    failures = state.get("critic_failures") or []
    package = state.get("active_context_package") or {}

    try:
        failure_dicts = [
            f.model_dump() if hasattr(f, "model_dump") else dict(f) for f in failures
        ]
        # Verbatim targeting: full rewrite only when NO failure can be located.
        locatable = [f for f in failure_dicts if locate_offending_text(draft, f["offending_text"])]
        full_rewrite = len(locatable) == 0 and len(failure_dicts) > 0
        failures_text = json.dumps(failure_dicts)

        # Tier A pruned context; Tier B lean fallback.
        context_block = _tier_a_context(package, endpoint, draft, failures_text)
        beat = sqlite_db.get_beat_by_index(runtime.SQLITE_PATH, pointer.scene_id, pointer.beat_index)
        plan = json.loads((beat or {}).get("beat_plan_json") or "{}")
        craft_diagnosis = (package.get("craft_diagnosis") or "") if isinstance(package, dict) else ""

        tier_b_needed = not fits_in_budget(
            context_block + draft + failures_text,
            endpoint.context_window,
            endpoint.reserved_output_tokens,
            endpoint.tokenizer_family,
            endpoint.model_name,
        )
        if tier_b_needed:
            logger.info("revision token budget: dropping to Tier B lean context set")
            context_block = ""  # lean set: draft + failures + diagnosis + beat entry only

        prompt = PromptLoader().load_and_render(
            "node_revise_prose",
            {
                "current_draft": draft,
                "failures": failures_text,
                "beat_entry_constraints": plan.get("entry_constraints") or "",
                "beat_exit_constraints": plan.get("exit_constraints") or "",
                "pad_behavioral_constraints": plan.get("pad_constraint") or "",
                "craft_diagnosis": craft_diagnosis,
                "revision_number": state.get("retry_count", 0) + 1,
            },
        )
        revised = await call_llm_module.collect_llm_response(
            endpoint,
            [{"role": "user", "content": prompt}],
            temperature=0.5,
            stream=False,
        )

        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return {
            "current_draft_text": revised.strip() or draft,
            "retry_count": state.get("retry_count", 0) + 1,
            "critic_failures": None,  # explicit clear (append_or_clear reducer)
        }
    except Exception as e:
        log_node_event(
            logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e)
        )
        raise

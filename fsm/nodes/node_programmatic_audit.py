"""
fsm/nodes/node_programmatic_audit.py

Stage 1 Critics — model-free programmatic draft auditing.

Purpose:
    Runs regex parsers and lexical math against the current draft with zero LLM
    inference cost:

    - Passive-voice check (DENSITY-based): a PASSIVE_VOICE FailureObject is
      emitted only when more than config.thresholds.passive_voice_density
      (default 0.25) of the beat's sentences are passive. A single passive
      sentence in a fifteen-sentence beat never triggers.
    - STEL cosine distance (Dc): computed via style_store.compute_stel_cosine_distance —
      stubbed to 0.0 until real embeddings land in Sprint 5+. Only Dc gates routing.
    - Burrows' Delta: computed for UI telemetry only; logged, never routed on.
    - best_seen_draft: if the current draft has fewer failures than the stored
      one (or the field is None), it becomes the new best_seen_draft. In-memory
      only — the recovery payload for the headless terminal policy.

Architecture role:
    - Triggered by node_draft_prose or node_revise_prose.
    - Yields to edge_programmatic_router (fast-path bypass decision).
"""

import re
import time

from core import runtime
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.state import FailureObject, OrchestratorState
from memory.style_store import compute_stel_cosine_distance, get_author_style

logger = get_logger("node_programmatic_audit")

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
# be-verb (+ optional adverb) + past participle. Heuristic, deliberately
# conservative: regular -ed participles plus a list of common irregulars.
_PASSIVE_RE = re.compile(
    r"\b(?:is|are|was|were|be|been|being)\s+(?:\w+ly\s+)?"
    r"(?:\w+ed|born|begun|broken|brought|built|caught|chosen|done|drawn|driven|"
    r"eaten|fallen|felt|found|forgiven|frozen|given|gone|held|hidden|kept|known|"
    r"laid|led|left|lost|made|meant|met|paid|put|read|said|seen|sent|set|shaken|"
    r"shown|shut|sold|spoken|spent|stolen|taken|taught|thrown|told|torn|understood|"
    r"woken|won|worn|written)\b",
    re.IGNORECASE,
)

_FUNCTION_WORDS = (
    "the of and to a in that it was he she for on with as at by but be this had "
    "not are from or have an they which one you were all we when there can"
).split()


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences (terminal punctuation heuristic)."""
    return [s for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]


def is_passive(sentence: str) -> bool:
    """True when the sentence matches the be-verb + past-participle pattern."""
    return bool(_PASSIVE_RE.search(sentence))


def passive_density(text: str) -> tuple[float, list[str]]:
    """
    Fraction of passive sentences and the passive sentence list.

    Outputs:
        (density, passive_sentences). density is 0.0 for empty text.
    """
    sentences = split_sentences(text)
    if not sentences:
        return 0.0, []
    passive = [s for s in sentences if is_passive(s)]
    return len(passive) / len(sentences), passive


def burrows_delta(text: str, baseline: dict) -> float:
    """
    Advisory Burrows' Delta against the author baseline (telemetry only).

    Simplified implementation: mean absolute difference of normalized
    function-word frequencies vs the baseline's stored vector (or the corpus
    uniform prior when no baseline exists). Never influences routing.
    """
    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return 0.0
    freqs = [words.count(w) / len(words) for w in _FUNCTION_WORDS]
    base_vector = (baseline or {}).get("burrows_delta") or [0.0] * len(_FUNCTION_WORDS)
    if len(base_vector) != len(_FUNCTION_WORDS):
        base_vector = [0.0] * len(_FUNCTION_WORDS)
    return sum(abs(f - b) for f, b in zip(freqs, base_vector)) / len(_FUNCTION_WORDS)


async def node_programmatic_audit(state: OrchestratorState) -> dict:
    """
    Run the Stage 1 programmatic audit on current_draft_text.

    Outputs (merged into OrchestratorState):
        critic_failures: PASSIVE_VOICE FailureObjects (append reducer) — [] when clean.
        stylometric_distance: STEL Dc (stub 0.0 until Sprint 5+).
        best_seen_draft: updated per the fewest-failures rule.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    config = load_config()
    draft = state["current_draft_text"]

    try:
        failures: list[FailureObject] = []
        density, passive_sentences = passive_density(draft)
        if density > config.thresholds.passive_voice_density:
            worst = max(passive_sentences, key=len)
            failures.append(
                FailureObject(
                    error_code="PASSIVE_VOICE",
                    offending_text=worst[:200],
                    suggested_fix=(
                        f"Rewrite in active voice. {density:.0%} of sentences are passive "
                        f"(threshold {config.thresholds.passive_voice_density:.0%})."
                    ),
                    critic_source="programmatic",
                )
            )

        author_style = get_author_style(runtime.STYLES_DIR)
        dc = compute_stel_cosine_distance(draft, author_style.get("frozen_baseline") or {})
        delta = burrows_delta(draft, author_style.get("frozen_baseline") or {})
        logger.info("telemetry: burrows_delta=%.4f stel_dc=%.4f passive_density=%.2f", delta, dc, density)

        # best_seen_draft: fewest-failures rule. The failure count of the stored
        # best draft is tracked in best_seen_failure_count (in-memory only).
        total_failures = len(failures)
        best = state.get("best_seen_draft")
        best_count = state.get("best_seen_failure_count")
        update: dict = {
            "critic_failures": failures,
            "stylometric_distance": dc,
        }
        if best is None or best_count is None or total_failures < best_count:
            update["best_seen_draft"] = draft
            update["best_seen_failure_count"] = total_failures

        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return update
    except Exception as e:
        log_node_event(
            logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e)
        )
        raise

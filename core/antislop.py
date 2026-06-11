"""
core/antislop.py

Antislop black-box interface — cliché and slop detection/correction stubs.

Purpose:
    Defines the two-function public interface that node_draft_prose calls after
    each prose generation:
    - detect_slop(text) → List[SlopFlag]: Identifies cliché or slop spans.
    - resolve_slop(text, flags) → str: Corrects flagged spans via LLM or direct fix.

    Both functions are STUBS for the vertical slice (Sprint 1–5). detect_slop
    returns an empty list; resolve_slop returns the input text unchanged. The
    interface is designed to be implementation-agnostic — the internal matching
    algorithm, dictionary sourcing, and streaming strategy are an open research
    problem (see project outline/Problems_To_Address.md) scheduled for Sprint 6.

    The interface works with streaming on or off: detect_slop can receive either
    complete text or partial streamed text; resolve_slop corrects the result.
    node_draft_prose does not need to change when the stubs are replaced with
    real implementations in Sprint 6.

Architecture role:
    - Called exclusively by node_draft_prose after prose generation completes.
    - Decoupled from the FSM graph wiring — node_draft_prose imports these two
      functions directly. No other node calls this interface.
    - The SlopFlag dataclass defines the contract between detect_slop and
      resolve_slop. Both Sprint 6 implementations must conform to this schema.
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List, Optional


@dataclass
class SlopFlag:
    """
    A single detected cliché or slop span returned by detect_slop.

    Purpose:
        Represents one flagged problem in the generated prose. resolve_slop
        receives the full list of SlopFlags and uses them to target corrections.
        The schema is intentionally minimal so Sprint 6 can extend it without
        breaking the call sites in node_draft_prose.

    Fields:
        offending_text: The verbatim substring identified as slop or cliché.
            Used by resolve_slop for str.find() targeting (same approach as
            FailureObject in node_revise_prose — avoids integer span hallucination).
        category: Classification of the slop type (e.g., "cliche", "passive_filler",
            "adverb_cluster"). Informational; resolve_slop may use it to select
            a correction strategy.
        severity: Float 0.0–1.0 indicating how strongly the flag applies. Used
            by resolve_slop to prioritize corrections when the token budget is tight.
    """

    offending_text: str
    category: str = "cliche"
    severity: float = 1.0
    replacement: Optional[str] = None


_DICTIONARY_PATH = Path(__file__).resolve().parent / "slop_dictionary.json"

# Structural detector: the same sentence opener used 3+ times in a row.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@lru_cache(maxsize=1)
def _load_dictionary() -> tuple:
    """
    Load and compile the slop dictionary once per process.

    Returns a tuple of (compiled_regex, category, severity, replacement) rows.
    're:'-prefixed patterns are raw regexes; plain patterns are matched as
    case-insensitive substrings with word-ish boundaries.
    """
    data = json.loads(_DICTIONARY_PATH.read_text(encoding="utf-8"))
    compiled = []
    for entry in data["entries"]:
        pattern = entry["pattern"]
        if pattern.startswith("re:"):
            regex = re.compile(pattern[3:], re.IGNORECASE)
        else:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)
        compiled.append(
            (regex, entry.get("category", "cliche"), float(entry.get("severity", 1.0)),
             entry.get("replacement"))
        )
    return tuple(compiled)


def _detect_repeated_openers(text: str) -> List[SlopFlag]:
    """Flag 3+ consecutive sentences sharing the same first word (monotony)."""
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    flags: List[SlopFlag] = []
    run_start = 0
    for i in range(1, len(sentences) + 1):
        same = (
            i < len(sentences)
            and sentences[i].split()[:1] == sentences[run_start].split()[:1]
            and sentences[run_start].split()
        )
        if not same:
            if i - run_start >= 3:
                opener = sentences[run_start].split()[0]
                flags.append(
                    SlopFlag(
                        offending_text=opener,
                        category="repeated_opener",
                        severity=0.5,
                    )
                )
            run_start = i
    return flags


def detect_slop(text: str) -> List[SlopFlag]:
    """
    Identify cliché and slop spans in generated prose. STUB — returns empty list.

    Purpose:
        The detection half of the antislop black-box interface. Receives generated
        prose (complete or partial streamed text) and returns a list of SlopFlag
        instances identifying problematic spans. node_draft_prose calls this after
        each generation, then passes the result to resolve_slop.

        CURRENT STATUS: Sprint 1–5 stub. Always returns []. The full implementation
        (matching algorithm, slop dictionary, streaming strategy) is a Sprint 6
        research spike defined as an open problem in project outline/Problems_To_Address.md.

    Inputs:
        text: The generated prose string to scan. May be a partial streaming
            chunk or a complete beat's worth of text.

    Outputs:
        List[SlopFlag]: Zero or more SlopFlag instances identifying offending spans.

        Sprint 6 implementation: case-insensitive dictionary phrase/regex
        matching (core/slop_dictionary.json) plus structural detectors
        (adverb clusters via dictionary regex, repeated sentence openers).
        Pure and deterministic — identical text in, identical flags out.
    """
    if not text:
        return []
    flags: List[SlopFlag] = []
    seen: set = set()
    for regex, category, severity, replacement in _load_dictionary():
        for match in regex.finditer(text):
            span_text = match.group(0)
            key = (span_text.lower(), category)
            if key in seen:
                continue  # one flag per distinct offending span per category
            seen.add(key)
            flags.append(
                SlopFlag(
                    offending_text=span_text,
                    category=category,
                    severity=severity,
                    replacement=replacement,
                )
            )
    flags.extend(_detect_repeated_openers(text))
    flags.sort(key=lambda f: -f.severity)
    return flags


def resolve_slop(text: str, flags: List[SlopFlag]) -> str:
    """
    Correct flagged slop spans in generated prose. STUB — returns text unchanged.

    Purpose:
        The correction half of the antislop black-box interface. Receives the
        original prose and a list of SlopFlags from detect_slop. Returns a
        corrected version of the text with flagged spans replaced or reworded.
        node_draft_prose overwrites current_draft_text with the return value.

        CURRENT STATUS: Sprint 1–5 stub. Always returns text unchanged. The full
        implementation may use a secondary LLM call, a direct programmatic fix, or
        a dictionary substitution — implementation strategy is a Sprint 6 research
        spike and does not affect the interface contract.

    Inputs:
        text: The original generated prose string.
        flags: The List[SlopFlag] returned by detect_slop for this text.
            If empty (as returned by the stub), no correction is needed.

    Outputs:
        str: The corrected prose string. If flags is empty or no corrections are
        applied, returns text unchanged.

        Sprint 6 implementation: deterministic direct fixes. Flags whose
        dictionary entry carries a replacement are substituted case-insensitively
        (whitespace normalized when the replacement is an empty string). Flags
        without a replacement are left in place — they remain visible to the
        Stage 1/2 critics, and endpoints with supports_inference_antislop=True
        can resolve them at the sampler level instead. Idempotent: re-running on
        corrected output applies no further changes.
    """
    if not flags:
        return text
    corrected = text
    for flag in flags:
        if flag.replacement is None:
            continue
        pattern = re.compile(re.escape(flag.offending_text), re.IGNORECASE)
        corrected = pattern.sub(flag.replacement, corrected)
    # Normalize doubled spaces left by empty-string replacements.
    corrected = re.sub(r"[ \t]{2,}", " ", corrected)
    corrected = re.sub(r" ([,.;!?])", r"\1", corrected)
    return corrected

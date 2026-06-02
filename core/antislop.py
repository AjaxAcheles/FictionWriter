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

from dataclasses import dataclass
from typing import List


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
        Currently always returns [].
    """
    return []


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
        applied, returns text unchanged. Currently always returns text unchanged.
    """
    return text

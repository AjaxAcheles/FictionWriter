"""
evals/judge.py

LLM judge — scores critic findings against injected ground truth.

Purpose:
    Given the injected-error records for a beat and the FailureObjects the
    critic committee reported for that beat, the judge LLM classifies:
    - each injected error as CAUGHT (some critic finding identifies it) or MISSED;
    - each critic finding that matches no injected error as HALLUCINATED or
      LEGITIMATE (a real issue the generator produced by accident — counted
      separately so accidental real catches don't poison the FP rate).

    The verdict is grammar-constrained via call_llm_structured (the same
    bounded-validation path production critics use), on config.endpoints.judge
    when configured, else the critic endpoint.
"""

import json
from typing import List, Optional

from pydantic import BaseModel, ConfigDict

from fsm.state import FailureObject
from llm import call_llm as call_llm_module


class ErrorVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    error_class: str
    caught: bool
    matched_finding_index: Optional[int] = None


class FindingVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    finding_index: int
    classification: str  # "match" | "legitimate" | "hallucinated"


class JudgeVerdict(BaseModel):
    """Judge output for one beat."""

    model_config = ConfigDict(extra="ignore")

    error_verdicts: List[ErrorVerdict]
    finding_verdicts: List[FindingVerdict] = []


def judge_endpoint(config):
    """The configured judge endpoint, falling back to the critic endpoint."""
    return config.endpoints.judge or config.endpoints.critic


async def judge_beat(
    config,
    beat_text: str,
    injected: list,
    findings: List[FailureObject],
) -> JudgeVerdict:
    """
    Score one beat's critic findings against its injected ground truth.

    Outputs:
        JudgeVerdict — one ErrorVerdict per injected error, one FindingVerdict
        per critic finding.
    """
    injected_payload = [
        {"index": i, "error_class": e.error_class, "description": e.description,
         "mutated_span": e.mutated_span}
        for i, e in enumerate(injected)
    ]
    findings_payload = [
        {"index": i, **f.model_dump()} for i, f in enumerate(findings)
    ]
    messages = [
        {
            "role": "user",
            "content": (
                "You are scoring a fiction continuity critic. Below are (A) errors "
                "deliberately planted in a draft beat, and (B) the findings the critic "
                "reported. For each planted error decide whether any finding identifies "
                "it (caught) and which finding index matched. For each finding decide: "
                "'match' (it identifies a planted error), 'legitimate' (a real issue "
                "not planted), or 'hallucinated' (no real issue). Respond with JSON: "
                '{"error_verdicts": [{"error_class": ..., "caught": bool, '
                '"matched_finding_index": int|null}], "finding_verdicts": '
                '[{"finding_index": int, "classification": "match"|"legitimate"|"hallucinated"}]}'
                f"\n\n(A) PLANTED ERRORS:\n{json.dumps(injected_payload)}"
                f"\n\n(B) CRITIC FINDINGS:\n{json.dumps(findings_payload)}"
                f"\n\nBEAT TEXT:\n{beat_text[:4000]}"
            ),
        }
    ]
    return await call_llm_module.call_llm_structured(
        judge_endpoint(config),
        messages,
        JudgeVerdict,
        retry_cap=config.model_validate_retry_cap,
        max_tokens=2048,
    )

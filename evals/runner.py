"""
evals/runner.py

Eval orchestrator — manuscript → injection → critics → judge → metrics.

Purpose:
    The repeatable correctness signal for the critic pipeline:
    1. Generate a seeded test manuscript (evals/manuscript_generator.py).
    2. Inject known continuity errors (evals/error_injection.py).
    3. Run every beat through node_adversarial_critics — REAL LLM calls against
       the configured critic endpoint (mock the adapter in unit tests).
    4. Judge each beat's findings against the planted ground truth.
    5. Aggregate: critic recall, false-positive rate, drift fidelity.
    6. Write a JSON report to data/evals/eval_{timestamp}.json.

CLI:
    uv run python -m evals.runner --beats 8 --errors 4 --seed 7

Metrics:
    recall              = caught / injected
    false_positive_rate = hallucinated / total critic findings (0.0 when none)
    drift_fidelity      = 1 - population stdev of stylometric_distance across
                          beats (a stable stub or stable real Dc both score 1.0;
                          erratic drift readings reduce fidelity).
"""

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import asdict
from datetime import datetime, timezone

from core import runtime
from core.config_loader import load_config
from evals.error_injection import inject_errors
from evals.judge import judge_beat
from evals.manuscript_generator import generate_manuscript
from fsm.nodes.node_adversarial_critics import node_adversarial_critics
from fsm.nodes.node_programmatic_audit import node_programmatic_audit
from fsm.state import FSM_Pointer


def _eval_state(beat_text: str, beat_index: int, ledger) -> dict:
    """Minimal OrchestratorState for a critic pass over one eval beat."""
    return {
        "project_id": "eval",
        "fsm_pointer": FSM_Pointer(
            arc_id="eval_arc", chapter_id="eval_ch", scene_id="eval_sc", beat_index=beat_index
        ),
        "active_context_package": {
            "beat_word_budget": len(beat_text.split()),
            "graphiti_facts": "\n".join(
                f"- {name} keeps {facts['prop']} at {facts['location']}"
                for name, facts in ledger.characters.items()
            ),
            "character_states": json.dumps(ledger.characters),
            "thread_statuses": json.dumps(ledger.open_threads),
        },
        "current_draft_text": beat_text,
        "streaming_buffer": "",
        "critic_failures": [],
        "stylometric_distance": 0.0,
        "retry_count": 0,
        "replan_count": 0,
        "escalation_tier": 0,
        "has_paradox": False,
        "transient_dc_override": None,
        "pause_requested": False,
        "hard_stop_asserted": False,
        "failed_beat_cache": [],
        "best_seen_draft": None,
        "best_seen_failure_count": None,
    }


async def run_eval(beats: int = 8, errors: int = 4, seed: int = 7) -> dict:
    """
    Execute one full eval run and return (and persist) the report dict.
    """
    config = load_config()
    beat_texts, ledger = generate_manuscript(seed, beats=beats)
    injected = inject_errors(beat_texts, ledger, errors, seed)
    injected_by_beat = {}
    for error in injected:
        injected_by_beat.setdefault(error.beat_index, []).append(error)

    per_beat = []
    caught = missed = hallucinated = legitimate = total_findings = 0
    drift_readings = []

    for beat_index, beat_text in enumerate(beat_texts):
        state = _eval_state(beat_text, beat_index, ledger)
        audit_update = await node_programmatic_audit(state)
        drift_readings.append(audit_update.get("stylometric_distance", 0.0))

        critic_update = await node_adversarial_critics(state)
        findings = critic_update.get("critic_failures", [])
        total_findings += len(findings)

        beat_injected = injected_by_beat.get(beat_index, [])
        if beat_injected or findings:
            verdict = await judge_beat(config, beat_text, beat_injected, findings)
            for ev in verdict.error_verdicts:
                caught += int(ev.caught)
                missed += int(not ev.caught)
            for fv in verdict.finding_verdicts:
                hallucinated += int(fv.classification == "hallucinated")
                legitimate += int(fv.classification == "legitimate")
            per_beat.append(
                {
                    "beat_index": beat_index,
                    "injected": [asdict(e) for e in beat_injected],
                    "findings": [f.model_dump() for f in findings],
                    "error_verdicts": [ev.model_dump() for ev in verdict.error_verdicts],
                    "finding_verdicts": [fv.model_dump() for fv in verdict.finding_verdicts],
                }
            )

    recall = caught / len(injected) if injected else 1.0
    false_positive_rate = hallucinated / total_findings if total_findings else 0.0
    drift_fidelity = (
        1.0 - statistics.pstdev(drift_readings) if len(drift_readings) > 1 else 1.0
    )

    report = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "beats": beats,
        "injected_errors": len(injected),
        "metrics": {
            "recall": round(recall, 4),
            "false_positive_rate": round(false_positive_rate, 4),
            "drift_fidelity": round(drift_fidelity, 4),
            "caught": caught,
            "missed": missed,
            "hallucinated": hallucinated,
            "legitimate_unplanted": legitimate,
            "total_findings": total_findings,
        },
        "per_beat": per_beat,
    }

    evals_dir = runtime.DATA_DIR / "evals"
    evals_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = evals_dir / f"eval_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="FictionWriter critic eval harness")
    parser.add_argument("--beats", type=int, default=8)
    parser.add_argument("--errors", type=int, default=4)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    start = time.monotonic()
    report = asyncio.run(run_eval(beats=args.beats, errors=args.errors, seed=args.seed))
    metrics = report["metrics"]
    print(json.dumps(metrics, indent=2))
    print(f"report: {report['report_path']}  ({time.monotonic() - start:.1f}s)")


if __name__ == "__main__":
    main()

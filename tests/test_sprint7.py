"""
tests/test_sprint7.py

Sprint 7 Test Suite — LLM-as-Judge Eval Harness.

Purpose:
    Verifies the seeded manuscript generator, the four error injectors, judge
    verdict plumbing, metric math, and the end-to-end runner — all against
    mocked critic/judge transports. Live-endpoint runs use the same code path
    via `python -m evals.runner`.
"""

import json

import pytest

from core import runtime
from evals.error_injection import inject_errors
from evals.manuscript_generator import build_ledger, generate_manuscript
from fsm.state import FailureObject
from memory import sqlite_db


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SQLITE_PATH", tmp_path / "fictionwriter.db")
    monkeypatch.setattr(runtime, "STYLES_DIR", tmp_path / "styles")
    sqlite_db.init_db(runtime.SQLITE_PATH)

    class _FakeEncoder:
        def encode(self, text):
            return text.split()

    monkeypatch.setattr("llm.tokenizer._get_tiktoken_encoder", lambda: _FakeEncoder())
    return runtime


# --------------------------------------------------------------------------- #
# Generator                                                                   #
# --------------------------------------------------------------------------- #


def test_generator_deterministic_and_in_envelope():
    beats_a, ledger_a = generate_manuscript(seed=7, beats=8, words_per_beat=800)
    beats_b, ledger_b = generate_manuscript(seed=7, beats=8, words_per_beat=800)
    assert beats_a == beats_b and ledger_a.characters == ledger_b.characters

    total_words = sum(len(b.split()) for b in beats_a)
    assert 5000 <= total_words <= 10000  # blueprint envelope

    beats_c, _ = generate_manuscript(seed=8, beats=8, words_per_beat=800)
    assert beats_c != beats_a  # seed sensitivity


def test_generator_ledger_consistency():
    beats, ledger = generate_manuscript(seed=3, beats=4)
    for name, facts in ledger.characters.items():
        anchor = f"{name} keeps the {facts['prop']} close at {facts['location']}."
        assert all(anchor in beat for beat in beats)


# --------------------------------------------------------------------------- #
# Error injection                                                             #
# --------------------------------------------------------------------------- #


def test_injectors_mutate_named_beats_with_metadata():
    beats, ledger = generate_manuscript(seed=5, beats=8)
    pristine = list(beats)
    injected = inject_errors(beats, ledger, count=4, seed=5)

    assert len(injected) == 4
    assert len({e.beat_index for e in injected}) == 4  # distinct beats
    assert {e.error_class for e in injected} == {
        "fact_flip", "name_swap", "timeline_reversal", "thread_paradox"
    }
    for error in injected:
        assert beats[error.beat_index] != pristine[error.beat_index]
        assert error.mutated_span in beats[error.beat_index]
    untouched = set(range(8)) - {e.beat_index for e in injected}
    for i in untouched:
        assert beats[i] == pristine[i]


def test_injection_deterministic_and_bounded():
    beats_a, ledger_a = generate_manuscript(seed=11, beats=6)
    beats_b, ledger_b = generate_manuscript(seed=11, beats=6)
    inj_a = inject_errors(beats_a, ledger_a, 3, seed=11)
    inj_b = inject_errors(beats_b, ledger_b, 3, seed=11)
    assert [(e.beat_index, e.error_class) for e in inj_a] == [
        (e.beat_index, e.error_class) for e in inj_b
    ]
    with pytest.raises(ValueError):
        inject_errors(beats_a, ledger_a, 99, seed=1)


# --------------------------------------------------------------------------- #
# Judge plumbing                                                              #
# --------------------------------------------------------------------------- #


async def test_judge_beat_builds_verdict(env, monkeypatch):
    import evals.judge as judge_module

    captured = {}

    async def fake_structured(endpoint, messages, schema_model, retry_cap, **kwargs):
        captured["prompt"] = messages[0]["content"]
        captured["endpoint"] = endpoint
        return schema_model.model_validate(
            {
                "error_verdicts": [
                    {"error_class": "fact_flip", "caught": True, "matched_finding_index": 0}
                ],
                "finding_verdicts": [{"finding_index": 0, "classification": "match"}],
            }
        )

    monkeypatch.setattr(judge_module.call_llm_module, "call_llm_structured", fake_structured)

    from core.config_loader import load_config
    from evals.error_injection import InjectedError

    config = load_config()
    finding = FailureObject(
        error_code="CONTINUITY_BREAK", offending_text="x", suggested_fix="y", critic_source="continuity"
    )
    injected = InjectedError(0, "fact_flip", "prop swapped", "mutated span")
    verdict = await judge_module.judge_beat(config, "beat text", [injected], [finding])

    assert verdict.error_verdicts[0].caught is True
    assert "PLANTED ERRORS" in captured["prompt"]
    # judge falls back to the critic endpoint when judge is unset
    assert captured["endpoint"] is config.endpoints.critic


# --------------------------------------------------------------------------- #
# End-to-end runner with mocked transports                                    #
# --------------------------------------------------------------------------- #


async def test_run_eval_end_to_end_metrics(env, monkeypatch):
    """
    3 injected errors; mocked critics flag the injected beats (plus one
    hallucination on a clean beat); mocked judge scores 2 caught / 1 missed.
    Expected: recall 2/3; FP rate = hallucinated / total findings.
    """
    import evals.runner as runner
    from fsm.nodes import node_adversarial_critics as nac

    beats = 6
    _, ledger = generate_manuscript(seed=4, beats=beats)
    planted = inject_errors(
        [b for b in generate_manuscript(seed=4, beats=beats)[0]], ledger, 3, seed=4
    )
    error_beats = sorted(e.beat_index for e in planted)
    clean_beat = next(i for i in range(beats) if i not in error_beats)

    # --- mock the critic committee: one finding per injected beat + one hallucination
    async def fake_critics(state):
        idx = state["fsm_pointer"].beat_index
        if idx in error_beats or idx == clean_beat:
            return {
                "critic_failures": [
                    FailureObject(
                        error_code="CONTINUITY_BREAK",
                        offending_text=f"beat {idx} span",
                        suggested_fix="fix it",
                        critic_source="continuity",
                    )
                ]
            }
        return {"critic_failures": []}

    monkeypatch.setattr(runner, "node_adversarial_critics", fake_critics)

    # --- mock the judge: catches errors on the first two error beats, misses the
    # third; classifies findings as match except the clean beat (hallucinated).
    async def fake_judge(config, beat_text, injected, findings):
        from evals.judge import ErrorVerdict, FindingVerdict, JudgeVerdict

        error_verdicts = []
        for e in injected:
            caught = e.beat_index in error_beats[:2]
            error_verdicts.append(
                ErrorVerdict(error_class=e.error_class, caught=caught,
                             matched_finding_index=0 if caught else None)
            )
        finding_verdicts = [
            FindingVerdict(
                finding_index=i,
                classification="hallucinated" if not injected else "match",
            )
            for i in range(len(findings))
        ]
        return JudgeVerdict(error_verdicts=error_verdicts, finding_verdicts=finding_verdicts)

    monkeypatch.setattr(runner, "judge_beat", fake_judge)

    report = await runner.run_eval(beats=beats, errors=3, seed=4)
    metrics = report["metrics"]

    assert metrics["caught"] == 2 and metrics["missed"] == 1
    assert metrics["recall"] == pytest.approx(2 / 3, abs=1e-4)
    assert metrics["total_findings"] == 4  # 3 error beats + 1 clean-beat hallucination
    assert metrics["hallucinated"] == 1
    assert metrics["false_positive_rate"] == pytest.approx(0.25)
    assert metrics["drift_fidelity"] == pytest.approx(1.0)  # stub Dc is constant 0.0

    # Report persisted under DATA_DIR/evals.
    report_path = report["report_path"]
    persisted = json.loads(open(report_path).read())
    assert persisted["metrics"]["recall"] == metrics["recall"]
    assert len(persisted["per_beat"]) == 4  # judged beats only (3 error + 1 hallucinating)

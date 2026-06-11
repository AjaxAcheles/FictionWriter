"""
tests/test_sprint3.py

Sprint 3 Vertical Slice Test Suite.

Purpose:
    Exercises the FSM vertical slice in isolation against mocked LLM transports:
    PAD pipeline math and fallback, the programmatic audit, all three routers,
    revision targeting, the intent-record commit sequence, and the end-to-end
    draft -> audit -> commit smoke test (the gate for Sprint 6 work).

    No live inference endpoint is required for any test in this file.
"""

import asyncio
import json
from pathlib import Path

import pytest

from core import runtime
from fsm.state import FSM_Pointer, FailureObject, append_or_clear
from memory import sqlite_db


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated data stores: every runtime path points into tmp_path."""
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SQLITE_PATH", tmp_path / "fictionwriter.db")
    monkeypatch.setattr(runtime, "GRAPHITI_PATH", tmp_path / "graphiti.db")
    monkeypatch.setattr(runtime, "EVENT_LOG_PATH", tmp_path / "event_log.jsonl")
    monkeypatch.setattr(runtime, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(runtime, "STYLES_DIR", tmp_path / "styles")
    sqlite_db.init_db(runtime.SQLITE_PATH)

    # Hermetic tokenizer: tiktoken's first use downloads its BPE vocab over the
    # network. Tests must run offline (CI / sandboxes), so the cached encoder is
    # replaced with a whitespace splitter — the budget *routing* logic stays real.
    class _FakeEncoder:
        def encode(self, text):
            return text.split()

    monkeypatch.setattr("llm.tokenizer._get_tiktoken_encoder", lambda: _FakeEncoder())
    return runtime


def seed_narrative(db: Path, word_budget: int = 1200) -> FSM_Pointer:
    """Seed one arc/chapter/scene and return a pointer at beat 0."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    sqlite_db.insert_row(db, "Arcs", {"arc_id": "arc_001", "title": "Arc", "summary": "", "created_at": now})
    sqlite_db.insert_row(
        db, "Chapters",
        {"chapter_id": "ch_001", "arc_id": "arc_001", "title": "Ch", "chapter_index": 0,
         "status": "active", "created_at": now},
    )
    sqlite_db.insert_row(
        db, "Scenes",
        {"scene_id": "sc_001", "chapter_id": "ch_001", "scene_index": 0,
         "description": "A tense meeting at the harbor.", "word_budget": word_budget, "word_count": 0},
    )
    sqlite_db.insert_row(
        db, "Characters",
        {"char_id": "char_mara", "name": "Mara", "role": "protagonist", "description": "A smuggler."},
    )
    return FSM_Pointer(arc_id="arc_001", chapter_id="ch_001", scene_id="sc_001", beat_index=0)


def seed_beat(db: Path, beat_index: int = 0, status: str = "planned") -> str:
    beat_id = f"sc_001_beat_{beat_index}"
    sqlite_db.upsert_beat(
        db,
        {
            "beat_id": beat_id,
            "scene_id": "sc_001",
            "beat_index": beat_index,
            "beat_plan_json": json.dumps(
                {
                    "description": "Mara confronts the dock master.",
                    "word_budget": 600,
                    "entry_constraints": "Mara stands at the pier gate.",
                    "exit_constraints": "Mara holds the manifest.",
                    "raw_pad_targets": {"char_mara": {"pleasure": -0.4, "arousal": 0.6, "dominance": 0.5}},
                    "pad_constraint": "Mara is coiled and assertive.",
                }
            ),
            "status": status,
        },
    )
    return beat_id


def base_state(pointer: FSM_Pointer) -> dict:
    return {
        "project_id": "test",
        "fsm_pointer": pointer,
        "active_context_package": {},
        "current_draft_text": "",
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


ACTIVE_PROSE = (
    "Mara walks the pier and counts the boats. She grips the manifest and stares "
    "down the dock master. He shrugs and lights a cigarette. She snatches the "
    "ledger from his hands and reads every line aloud. The gulls scream overhead. "
)


# --------------------------------------------------------------------------- #
# State reducer                                                               #
# --------------------------------------------------------------------------- #


def test_append_or_clear_reducer():
    """Lists append; None clears — the contract revise/commit rely on."""
    assert append_or_clear([1], [2]) == [1, 2]
    assert append_or_clear(None, [2]) == [2]
    assert append_or_clear([1, 2], None) == []


# --------------------------------------------------------------------------- #
# PAD pipeline                                                                #
# --------------------------------------------------------------------------- #


def test_pad_region_key_deterministic():
    from fsm.nodes.node_plan_beat import pad_region_key

    assert pad_region_key(0.5, 0.5, 0.5) == "high_pleasure_high_arousal_high_dominance"
    assert pad_region_key(-0.1, 0.9, -0.9) == "low_pleasure_high_arousal_low_dominance"
    # Boundary: 0.0 is 'high' on every axis.
    assert pad_region_key(0.0, 0.0, 0.0) == "high_pleasure_high_arousal_high_dominance"


def test_pad_regions_lookup_covers_all_octants():
    from fsm.nodes.node_plan_beat import load_pad_regions, pad_region_key

    regions = load_pad_regions()
    for p in (0.5, -0.5):
        for a in (0.5, -0.5):
            for d in (0.5, -0.5):
                assert pad_region_key(p, a, d) in regions


def test_ewma_pad_math():
    from fsm.nodes.node_plan_beat import ewma_pad

    rows_newest_first = [
        {"pleasure": 1.0, "arousal": 0.0, "dominance": 0.0},
        {"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0},
    ]
    out = ewma_pad(rows_newest_first, alpha=0.5)
    # oldest 0.0 seeds; newest 1.0 folds in: 0.5*1.0 + 0.5*0.0 = 0.5
    assert out["pleasure"] == pytest.approx(0.5)
    assert ewma_pad([], 0.35) == {"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0}


async def test_pad_translation_fallback(monkeypatch):
    """Two consecutive transport failures fall back to the static baseline."""
    from core.config_loader import load_config
    from fsm.nodes import node_plan_beat as npb
    from prompts.prompt_loader import PromptLoader

    calls = []

    async def failing(endpoint, messages, **kwargs):
        calls.append(1)
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(npb.call_llm_module, "collect_llm_response", failing)
    result = await npb._translate_pad_constraint(
        load_config(), PromptLoader(), "Mara", "low_pleasure_high_arousal_low_dominance",
        "BASELINE-STRING", "scene intent", "beat description",
    )
    assert result == "BASELINE-STRING"
    assert len(calls) == 2  # one retry, then fallback


# --------------------------------------------------------------------------- #
# Programmatic audit                                                          #
# --------------------------------------------------------------------------- #


def test_passive_density_math():
    from fsm.nodes.node_programmatic_audit import passive_density

    text = "The door was opened by Mara. She walks in. He sits down. The fire crackles."
    density, passive = passive_density(text)
    assert density == pytest.approx(0.25)
    assert len(passive) == 1


async def test_audit_density_threshold(env, monkeypatch):
    """One passive sentence in many active ones does NOT trigger; majority does."""
    from fsm.nodes.node_programmatic_audit import node_programmatic_audit

    pointer = seed_narrative(env.SQLITE_PATH)
    state = base_state(pointer)

    state["current_draft_text"] = ACTIVE_PROSE + "The rope was cut by someone."
    update = await node_programmatic_audit(state)
    assert update["critic_failures"] == []
    assert update["best_seen_draft"] == state["current_draft_text"]

    state["current_draft_text"] = (
        "The door was opened by the guard. The note was written by Mara. "
        "The boat was sunk by the storm."
    )
    update = await node_programmatic_audit(state)
    assert len(update["critic_failures"]) == 1
    assert update["critic_failures"][0].error_code == "PASSIVE_VOICE"
    assert update["critic_failures"][0].critic_source == "programmatic"


async def test_best_seen_draft_fewest_failures_rule(env):
    from fsm.nodes.node_programmatic_audit import node_programmatic_audit

    pointer = seed_narrative(env.SQLITE_PATH)
    state = base_state(pointer)

    # First pass: 1 failure → becomes best (best is None).
    state["current_draft_text"] = (
        "The door was opened by the guard. The note was written by Mara. "
        "The boat was sunk by the storm."
    )
    update = await node_programmatic_audit(state)
    assert update["best_seen_draft"] == state["current_draft_text"]
    state.update(update)

    # Second pass: clean draft (0 failures < 1) → replaces best.
    state["current_draft_text"] = ACTIVE_PROSE
    update = await node_programmatic_audit(state)
    assert update["best_seen_draft"] == ACTIVE_PROSE
    state.update(update)

    # Third pass: failing draft again (1 failure > 0) → best unchanged.
    state["current_draft_text"] = "The window was smashed by the wind. The sail was torn by hail."
    update = await node_programmatic_audit(state)
    assert "best_seen_draft" not in update


# --------------------------------------------------------------------------- #
# Routers                                                                     #
# --------------------------------------------------------------------------- #


def _fail(code="CONTINUITY_BREAK", source="continuity"):
    return FailureObject(error_code=code, offending_text="x", suggested_fix="y", critic_source=source)


def test_edge_mode_selector_strict_ordering():
    from fsm.routers.edge_mode_selector import edge_mode_selector

    pointer = FSM_Pointer(arc_id="a", chapter_id="c", scene_id="s", beat_index=0)
    state = base_state(pointer)

    # Step 0: paradox wins regardless of retry_count or failures.
    state.update(has_paradox=True, retry_count=0, critic_failures=[])
    assert edge_mode_selector(state) == "node_freeze_and_escalate"

    # Step 1: clean + Dc below threshold → commit.
    state.update(has_paradox=False, critic_failures=[], stylometric_distance=0.0)
    assert edge_mode_selector(state) == "node_commit_transaction"

    # Step 1 threshold: transient override takes precedence over config.
    state.update(stylometric_distance=0.15, transient_dc_override=0.20)
    assert edge_mode_selector(state) == "node_commit_transaction"
    state.update(transient_dc_override=None)

    # Steps 2-4: strict elif ordering at the documented boundaries.
    state.update(critic_failures=[_fail()], stylometric_distance=0.0)
    for retry, expected in [
        (0, "node_revise_prose"),
        (3, "node_revise_prose"),
        (4, "node_craft_consultant"),
        (5, "node_craft_consultant"),
        (6, "node_freeze_and_escalate"),
    ]:
        state["retry_count"] = retry
        assert edge_mode_selector(state) == expected, f"retry_count={retry}"


def test_edge_programmatic_router_fast_path():
    from fsm.routers.edge_programmatic_router import edge_programmatic_router

    pointer = FSM_Pointer(arc_id="a", chapter_id="c", scene_id="s", beat_index=0)
    state = base_state(pointer)

    # Clean first draft, Dc 0.0 < 0.12 * 0.7 → fast path.
    assert edge_programmatic_router(state) == "node_commit_transaction"

    # Any retry disables the fast path.
    state["retry_count"] = 1
    assert edge_programmatic_router(state) == "node_adversarial_critics"
    state["retry_count"] = 0

    # Any failure disables the fast path.
    state["critic_failures"] = [_fail()]
    assert edge_programmatic_router(state) == "node_adversarial_critics"
    state["critic_failures"] = []

    # Dc above the multiplied gate (0.084) but below the threshold → standard path.
    state["stylometric_distance"] = 0.1
    assert edge_programmatic_router(state) == "node_adversarial_critics"


def test_edge_commit_router_cascade(env, monkeypatch):
    from fsm.routers.edge_commit_router import edge_commit_router

    pointer = seed_narrative(env.SQLITE_PATH)
    state = base_state(pointer)

    # 1. Scene open → plan_beat.
    assert edge_commit_router(state) == "node_plan_beat"

    # 2. Scene closed, another open scene in chapter → plan_chapter.
    sqlite_db.close_scene(env.SQLITE_PATH, "sc_001")
    sqlite_db.insert_row(
        env.SQLITE_PATH, "Scenes",
        {"scene_id": "sc_002", "chapter_id": "ch_001", "scene_index": 1,
         "description": "", "word_budget": 500, "word_count": 0},
    )
    assert edge_commit_router(state) == "node_plan_chapter"

    # 3. All scenes closed, chapter still open → plan_chapter (chapter advance).
    sqlite_db.close_scene(env.SQLITE_PATH, "sc_002")
    assert edge_commit_router(state) == "node_plan_chapter"

    # 4. Chapter completed, word target unmet → plan_global (continuation arc).
    sqlite_db.set_chapter_status(env.SQLITE_PATH, "ch_001", "completed")
    assert edge_commit_router(state) == "node_plan_global"

    # 5. Word target met → END.
    import core.config_loader as cl
    real = cl.load_config()
    real.project.word_count_target = 0
    monkeypatch.setattr(cl, "load_config", lambda *a, **k: real)
    assert edge_commit_router(state) == "END"


# --------------------------------------------------------------------------- #
# Revision targeting                                                          #
# --------------------------------------------------------------------------- #


def test_locate_offending_text_exact_fuzzy_miss():
    from fsm.nodes.node_revise_prose import locate_offending_text

    draft = "Mara grips the manifest and stares down the dock master."
    assert locate_offending_text(draft, "grips the manifest")           # exact
    assert locate_offending_text(draft, "grips the manifesto")          # fuzzy
    assert not locate_offending_text(draft, "the kraken rises slowly")  # miss
    assert not locate_offending_text(draft, "")


async def test_revise_prose_increments_and_clears(env, monkeypatch):
    from fsm.nodes import node_revise_prose as nrp

    pointer = seed_narrative(env.SQLITE_PATH)
    seed_beat(env.SQLITE_PATH)
    state = base_state(pointer)
    state["current_draft_text"] = "The rope was cut by someone unseen."
    state["critic_failures"] = [
        FailureObject(
            error_code="PASSIVE_VOICE",
            offending_text="The rope was cut by someone unseen.",
            suggested_fix="Make it active.",
            critic_source="programmatic",
        )
    ]
    state["retry_count"] = 2

    async def fake_collect(endpoint, messages, **kwargs):
        return "Someone unseen cut the rope."

    monkeypatch.setattr(nrp.call_llm_module, "collect_llm_response", fake_collect)
    update = await nrp.node_revise_prose(state)
    assert update["current_draft_text"] == "Someone unseen cut the rope."
    assert update["retry_count"] == 3
    assert update["critic_failures"] is None  # explicit clear sentinel


# --------------------------------------------------------------------------- #
# Adversarial critics                                                         #
# --------------------------------------------------------------------------- #


async def test_critics_filter_none_and_flag_paradox(env, monkeypatch):
    from fsm.nodes import node_adversarial_critics as nac

    pointer = seed_narrative(env.SQLITE_PATH)
    state = base_state(pointer)
    state["current_draft_text"] = ACTIVE_PROSE
    state["active_context_package"] = {"beat_word_budget": 600}

    responses = {
        "continuity": FailureObject(
            error_code="THREAD_PARADOX", offending_text="dock master dies",
            suggested_fix="He must survive for the open Thread.", critic_source="continuity",
        ),
        "dialogue": FailureObject(error_code="NONE", offending_text="", suggested_fix="", critic_source="dialogue"),
        "pacing": FailureObject(error_code="NONE", offending_text="", suggested_fix="", critic_source="pacing"),
    }
    order = []

    async def fake_structured(endpoint, messages, schema_model, retry_cap, **kwargs):
        critic = next(c for c in ("continuity", "dialogue", "pacing") if c in messages[0]["content"].lower() or responses)
        # Identify by call order instead (serial mode is deterministic).
        name = ("continuity", "dialogue", "pacing")[len(order)]
        order.append(name)
        return responses[name]

    monkeypatch.setattr(nac.call_llm_module, "call_llm_structured", fake_structured)
    update = await nac.node_adversarial_critics(state)

    assert order == ["continuity", "dialogue", "pacing"]  # serial (config: False)
    assert len(update["critic_failures"]) == 1            # NONE sentinels filtered
    assert update["has_paradox"] is True
    assert update["failed_beat_cache"][0]["error_code"] == "THREAD_PARADOX"


# --------------------------------------------------------------------------- #
# Commit transaction                                                          #
# --------------------------------------------------------------------------- #


async def test_commit_transaction_lifecycle(env):
    from fsm.nodes.node_commit_transaction import node_commit_transaction

    pointer = seed_narrative(env.SQLITE_PATH, word_budget=10_000)  # scene stays open
    beat_id = seed_beat(env.SQLITE_PATH)
    state = base_state(pointer)
    state["current_draft_text"] = ACTIVE_PROSE
    state["retry_count"] = 3
    state["escalation_tier"] = 1
    state["has_paradox"] = True

    update = await node_commit_transaction(state)

    # SQLite ground truth
    beat = sqlite_db.get_row(env.SQLITE_PATH, "Beats", "beat_id", beat_id)
    assert beat["status"] == "committed"
    scene = sqlite_db.get_row(env.SQLITE_PATH, "Scenes", "scene_id", "sc_001")
    assert scene["word_count"] == len(ACTIVE_PROSE.split())
    assert scene["committed_at"] is None  # guard: budget not met → scene stays open
    assert sqlite_db.scan_pending_commit_intents(env.SQLITE_PATH) == []

    # PAD row clamped + written
    emotions = sqlite_db.get_recent_character_emotions(env.SQLITE_PATH, "char_mara")
    assert emotions and emotions[0]["beat_id"] == beat_id

    # Event log
    events = list(open(env.EVENT_LOG_PATH))
    assert len(events) == 1 and json.loads(events[0])["type"] == "beat_commit"

    # State resets + pointer advance
    assert update["fsm_pointer"].beat_index == 1
    assert update["retry_count"] == 0
    assert update["escalation_tier"] == 0
    assert update["has_paradox"] is False
    assert update["critic_failures"] is None
    assert update["failed_beat_cache"] is None
    assert update["best_seen_draft"] is None
    assert update["current_draft_text"] == ""


async def test_commit_closes_scene_and_chapter(env):
    """Scene guard closes the scene; chapter boundary writes the RAPTOR summary."""
    from fsm.nodes.node_commit_transaction import node_commit_transaction

    word_budget = len(ACTIVE_PROSE.split()) * 2  # two beats exactly meet the budget
    pointer = seed_narrative(env.SQLITE_PATH, word_budget=word_budget)

    state = base_state(pointer)
    for beat_index in (0, 1):
        seed_beat(env.SQLITE_PATH, beat_index=beat_index)
        state["fsm_pointer"] = pointer.model_copy(update={"beat_index": beat_index})
        state["current_draft_text"] = ACTIVE_PROSE
        update = await node_commit_transaction(state)

    scene = sqlite_db.get_row(env.SQLITE_PATH, "Scenes", "scene_id", "sc_001")
    assert scene["committed_at"] is not None
    chapter = sqlite_db.get_row(env.SQLITE_PATH, "Chapters", "chapter_id", "ch_001")
    assert chapter["status"] == "completed"

    # RAPTOR chapter summary written by the synchronous compress call
    from memory.raptor import get_raptor_summaries
    summaries = get_raptor_summaries(env.SQLITE_PATH, "sc_001", levels=["chapter"])
    assert summaries["chapter"] != ""

    # Snapshot ZIP archived
    assert list(env.SNAPSHOTS_DIR.glob("snapshot_*.zip"))


async def test_commit_is_idempotent(env):
    """Re-committing the same beat (crash replay) does not double-append prose."""
    from fsm.nodes.node_commit_transaction import node_commit_transaction

    pointer = seed_narrative(env.SQLITE_PATH, word_budget=10_000)
    seed_beat(env.SQLITE_PATH)
    state = base_state(pointer)
    state["current_draft_text"] = ACTIVE_PROSE

    await node_commit_transaction(state)
    first_count = sqlite_db.get_row(env.SQLITE_PATH, "Scenes", "scene_id", "sc_001")["word_count"]
    await node_commit_transaction(state)  # replay
    second_count = sqlite_db.get_row(env.SQLITE_PATH, "Scenes", "scene_id", "sc_001")["word_count"]
    assert first_count == second_count


# --------------------------------------------------------------------------- #
# Graph + smoke test                                                          #
# --------------------------------------------------------------------------- #


def test_graph_compiles():
    from fsm.graph import compile_graph

    app = compile_graph()
    assert app is not None


async def test_full_vertical_slice_smoke(env, monkeypatch):
    """
    Integration smoke test: pre-written context → full draft→audit→commit loop.
    Asserts the FSM pointer advanced and all stores wrote. Mocked transports.
    Must pass before Sprint 6 begins (Master Blueprint requirement).
    """
    from fsm.nodes import node_plan_beat as npb
    from fsm.nodes import node_draft_prose as ndp
    from fsm.nodes.node_plan_global import node_plan_global
    from fsm.nodes.node_plan_arc import node_plan_arc
    from fsm.nodes.node_plan_chapter import node_plan_chapter
    from fsm.nodes.node_assemble_context import node_assemble_context
    from fsm.nodes.node_programmatic_audit import node_programmatic_audit
    from fsm.nodes.node_commit_transaction import node_commit_transaction
    from fsm.routers.edge_programmatic_router import edge_programmatic_router
    from fsm.routers.edge_commit_router import edge_commit_router

    # --- mocked adapter layer -------------------------------------------------
    beat_words = len(ACTIVE_PROSE.split())

    async def fake_structured(endpoint, messages, schema_model, retry_cap, **kwargs):
        # Sprint 4 made the upper planners LLM-backed — dispatch by schema.
        if schema_model.__name__ == "GlobalPlan":
            return schema_model.model_validate(
                {"arcs": [{"id": "arc_001", "title": "Arc", "description": ""}], "threads": []}
            )
        if schema_model.__name__ == "ArcPlan":
            return schema_model.model_validate(
                {"chapter_stubs": [{"id": "ch_001", "description": "Ch"}], "thread_events": []}
            )
        if schema_model.__name__ == "ChapterPlan":
            return schema_model.model_validate(
                {"scenes": [{"id": "sc_001", "description": "Harbor.", "word_budget": 100, "ordering": 0}]}
            )
        return schema_model.model_validate(
            {
                "beats": [
                    {
                        "id": "b0", "scene_id": "ignored", "beat_index": 0,
                        "description": "Mara confronts the dock master.",
                        "word_budget": beat_words,
                        "entry_constraints": "Mara at the gate.",
                        "exit_constraints": "Mara holds the manifest.",
                        "raw_pad_targets": {"char_mara": {"pleasure": -0.4, "arousal": 0.6, "dominance": 0.5}},
                        "pad_region": "low_pleasure_high_arousal_high_dominance",
                    },
                    {
                        "id": "b1", "scene_id": "ignored", "beat_index": 1,
                        "description": "Mara reads the manifest.",
                        "word_budget": beat_words,
                        "entry_constraints": "Mara holds the manifest.",
                        "exit_constraints": "Mara knows the route.",
                        "raw_pad_targets": {},
                        "pad_region": None,
                    },
                ]
            }
        )

    async def fake_collect(endpoint, messages, **kwargs):
        return "Mara moves with clipped, deliberate aggression."

    async def fake_stream(endpoint, messages, **kwargs):
        for chunk in (ACTIVE_PROSE[: len(ACTIVE_PROSE) // 2], ACTIVE_PROSE[len(ACTIVE_PROSE) // 2 :]):
            yield chunk

    import llm.call_llm as adapter
    monkeypatch.setattr(adapter, "call_llm_structured", fake_structured)
    monkeypatch.setattr(adapter, "collect_llm_response", fake_collect)
    monkeypatch.setattr(adapter, "call_llm", fake_stream)
    monkeypatch.setattr(npb.call_llm_module, "call_llm_structured", fake_structured)
    monkeypatch.setattr(npb.call_llm_module, "collect_llm_response", fake_collect)
    monkeypatch.setattr(ndp.call_llm_module, "call_llm", fake_stream)

    # Seed the scene with a budget exactly two beats can close.
    pointer = seed_narrative(env.SQLITE_PATH, word_budget=beat_words * 2)
    state = base_state(pointer)

    def merge(update: dict):
        for key, value in update.items():
            if key in ("critic_failures", "failed_beat_cache"):
                state[key] = append_or_clear(state.get(key), value)
            else:
                state[key] = value

    # Planning cascade (stubs select the seeded rows).
    merge(await node_plan_global(state))
    merge(await node_plan_arc(state))
    merge(await node_plan_chapter(state))

    committed = 0
    for _ in range(2):
        merge(await npb.node_plan_beat(state))
        merge(await node_assemble_context(state))
        assert state["active_context_package"]["pad_behavioral_constraints"]
        merge(await ndp.node_draft_prose(state))
        assert state["current_draft_text"].startswith("Mara walks the pier")
        merge(await node_programmatic_audit(state))
        route = edge_programmatic_router(state)
        assert route == "node_commit_transaction", "clean draft must take the fast path"
        merge(await node_commit_transaction(state))
        committed += 1

    # FSM pointer advanced past both beats.
    assert state["fsm_pointer"].beat_index == 2
    # Both beats committed; scene closed; chapter completed.
    assert sqlite_db.get_committed_beat_count(env.SQLITE_PATH, "sc_001") == 2
    assert sqlite_db.get_row(env.SQLITE_PATH, "Scenes", "scene_id", "sc_001")["committed_at"]
    # Event log carries both commits.
    assert len(list(open(env.EVENT_LOG_PATH))) == 2
    # No pending intents.
    assert sqlite_db.scan_pending_commit_intents(env.SQLITE_PATH) == []
    # Dynamic advancement: chapter is done, word target unmet → continuation planning.
    assert edge_commit_router(state) == "node_plan_global"

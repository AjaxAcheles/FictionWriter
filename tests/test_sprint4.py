"""
tests/test_sprint4.py

Sprint 4 Test Suite — Full Planning Hierarchy & Fallback Subgraph.

Purpose:
    Exercises the LLM planning cascade (global/arc/chapter) against mocked
    structured outputs, the four-tier escalation ladder with the headless
    terminal policy, craft consultant fallback semantics, LLM chapter
    summarization fallback, and the post-edit human reconciliation pass.
    No live inference endpoint is required.
"""

import json
from pathlib import Path

import pytest

from core import runtime
from fsm.state import FSM_Pointer, FailureObject
from memory import sqlite_db

from tests.test_sprint3 import ACTIVE_PROSE, base_state, seed_beat, seed_narrative


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SQLITE_PATH", tmp_path / "fictionwriter.db")
    monkeypatch.setattr(runtime, "GRAPHITI_PATH", tmp_path / "graphiti.db")
    monkeypatch.setattr(runtime, "EVENT_LOG_PATH", tmp_path / "event_log.jsonl")
    monkeypatch.setattr(runtime, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(runtime, "STYLES_DIR", tmp_path / "styles")
    monkeypatch.setattr(runtime, "EXPORTS_DIR", tmp_path / "exports")
    sqlite_db.init_db(runtime.SQLITE_PATH)

    class _FakeEncoder:
        def encode(self, text):
            return text.split()

    monkeypatch.setattr("llm.tokenizer._get_tiktoken_encoder", lambda: _FakeEncoder())
    return runtime


def make_structured(payload_by_model: dict):
    """Build a fake call_llm_structured returning canned payloads per schema name."""

    async def fake(endpoint, messages, schema_model, retry_cap, **kwargs):
        return schema_model.model_validate(payload_by_model[schema_model.__name__])

    return fake


# --------------------------------------------------------------------------- #
# Planning hierarchy                                                          #
# --------------------------------------------------------------------------- #


async def test_plan_global_writes_arcs_and_threads(env, monkeypatch):
    from fsm.nodes import node_plan_global as npg

    monkeypatch.setattr(
        npg.call_llm_module,
        "call_llm_structured",
        make_structured(
            {
                "GlobalPlan": {
                    "arcs": [
                        {"id": "arc_alpha", "title": "Alpha", "description": "Rise.", "word_allocation": 150000},
                        {"id": "arc_beta", "title": "Beta", "description": "Fall.", "word_allocation": 150000},
                    ],
                    "threads": [
                        {"id": "th_debt", "name": "The Debt", "description": "Mara owes the cartel.", "priority": 0.9}
                    ],
                }
            }
        ),
    )
    pointer = FSM_Pointer(arc_id="", chapter_id="", scene_id="", beat_index=0)
    update = await npg.node_plan_global(base_state(pointer))

    assert update["fsm_pointer"].arc_id == "arc_alpha"
    assert sqlite_db.get_row(env.SQLITE_PATH, "Arcs", "arc_id", "arc_beta")["title"] == "Beta"
    threads = sqlite_db.get_open_threads(env.SQLITE_PATH)
    assert threads and threads[0]["thread_id"] == "th_debt"


async def test_plan_arc_writes_chapters_and_applies_thread_events(env, monkeypatch):
    from fsm.nodes import node_plan_arc as npa

    seed_narrative(env.SQLITE_PATH)
    sqlite_db.insert_row(
        env.SQLITE_PATH, "Threads",
        {"thread_id": "th_x", "name": "X", "description": "", "priority": 0.5, "status": "open"},
    )
    # Complete the seeded chapter so planning fires.
    sqlite_db.set_chapter_status(env.SQLITE_PATH, "ch_001", "completed")

    monkeypatch.setattr(
        npa.call_llm_module,
        "call_llm_structured",
        make_structured(
            {
                "ArcPlan": {
                    "chapter_stubs": [
                        {"id": "ch_new_1", "description": "The heist begins."},
                        {"id": "ch_new_2", "description": "The betrayal."},
                    ],
                    "thread_events": [{"thread_id": "th_x", "event": "close", "chapter_index": 1}],
                }
            }
        ),
    )
    pointer = FSM_Pointer(arc_id="arc_001", chapter_id="", scene_id="", beat_index=0)
    update = await npa.node_plan_arc(base_state(pointer))

    assert update["fsm_pointer"].chapter_id == "ch_new_1"
    assert sqlite_db.get_row(env.SQLITE_PATH, "Chapters", "chapter_id", "ch_new_2") is not None
    thread = sqlite_db.get_row(env.SQLITE_PATH, "Threads", "thread_id", "th_x")
    assert thread["status"] == "closed"


async def test_plan_chapter_schedules_scenes_with_granularity_floor(env, monkeypatch):
    from fsm.nodes import node_plan_chapter as npc

    seed_narrative(env.SQLITE_PATH)
    sqlite_db.close_scene(env.SQLITE_PATH, "sc_001")  # no open scenes → planning fires

    captured = {}

    async def fake_structured(endpoint, messages, schema_model, retry_cap, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return schema_model.model_validate(
            {
                "scenes": [
                    {"id": "sc_new_1", "description": "Docks at dawn.", "word_budget": 100, "ordering": 0},
                    {"id": "sc_new_2", "description": "The warehouse.", "word_budget": 900, "ordering": 1},
                ]
            }
        )

    monkeypatch.setattr(npc.call_llm_module, "call_llm_structured", fake_structured)

    pointer = FSM_Pointer(arc_id="arc_001", chapter_id="ch_001", scene_id="sc_001", beat_index=3)
    state = base_state(pointer)
    state["active_context_package"] = {"paradox_constraint": "FORBIDDEN: the dock master must not die."}
    update = await npc.node_plan_chapter(state)

    assert update["fsm_pointer"].scene_id == "sc_new_1"
    assert update["fsm_pointer"].beat_index == 0
    # Granularity Protection Filter: 100 → floor (400).
    scene = sqlite_db.get_row(env.SQLITE_PATH, "Scenes", "scene_id", "sc_new_1")
    assert scene["word_budget"] == npc.GRANULARITY_FLOOR_WORDS
    # Paradox constraint reached the planner prompt.
    assert "dock master must not die" in captured["prompt"]


# --------------------------------------------------------------------------- #
# Craft consultant                                                            #
# --------------------------------------------------------------------------- #


async def test_craft_consultant_injects_diagnosis(env, monkeypatch):
    from fsm.nodes import node_craft_consultant as ncc

    async def fake_collect(endpoint, messages, **kwargs):
        return "Diagnosis: dominance is over-narrated; externalize it."

    monkeypatch.setattr(ncc.call_llm_module, "collect_llm_response", fake_collect)
    pointer = seed_narrative(env.SQLITE_PATH)
    state = base_state(pointer)
    state["critic_failures"] = [
        FailureObject(error_code="DIALOGUE_FLAT", offending_text="x", suggested_fix="y", critic_source="dialogue")
    ]
    update = await ncc.node_craft_consultant(state)
    assert update["active_context_package"]["craft_diagnosis"].startswith("Diagnosis:")


async def test_craft_consultant_falls_back_after_two_failures(env, monkeypatch):
    from fsm.nodes import node_craft_consultant as ncc

    calls = []

    async def failing(endpoint, messages, **kwargs):
        calls.append(1)
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(ncc.call_llm_module, "collect_llm_response", failing)
    pointer = seed_narrative(env.SQLITE_PATH)
    update = await ncc.node_craft_consultant(base_state(pointer))
    assert len(calls) == 2
    assert update["active_context_package"]["craft_diagnosis"] == ncc.GENERIC_DIAGNOSIS


# --------------------------------------------------------------------------- #
# Escalation ladder                                                           #
# --------------------------------------------------------------------------- #


async def test_escalation_tier1_and_tier2(env):
    from fsm.nodes.node_freeze_and_escalate import (
        TIER1_DC_OVERRIDE, freeze_router, node_freeze_and_escalate,
    )

    pointer = seed_narrative(env.SQLITE_PATH)
    state = base_state(pointer)
    state["active_context_package"] = {"hnsw_flavor": "salty air", "raptor_scene_summary": "stuff"}
    state["retry_count"] = 6

    # Tier 1
    update = await node_freeze_and_escalate(state)
    assert update["escalation_tier"] == 1
    assert update["transient_dc_override"] == TIER1_DC_OVERRIDE
    assert "generation_overrides" in update["active_context_package"]
    assert "retry_count" not in update  # ladder progression preserved
    state.update(update)
    assert freeze_router(state) == "node_revise_prose"

    # Tier 2
    update = await node_freeze_and_escalate(state)
    assert update["escalation_tier"] == 2
    assert update["transient_dc_override"] is None
    assert update["active_context_package"]["hnsw_flavor"] == ""
    assert update["active_context_package"]["raptor_scene_summary"] == ""
    state.update(update)
    assert freeze_router(state) == "node_revise_prose"


async def test_escalation_tier3_subdivides_beat(env):
    from fsm.nodes.node_freeze_and_escalate import freeze_router, node_freeze_and_escalate

    pointer = seed_narrative(env.SQLITE_PATH)
    seed_beat(env.SQLITE_PATH, beat_index=0)   # word_budget 600 — divisible
    seed_beat(env.SQLITE_PATH, beat_index=1)   # subsequent beat: index must shift
    state = base_state(pointer)
    state["escalation_tier"] = 2

    update = await node_freeze_and_escalate(state)
    assert update["escalation_tier"] == 3
    assert update["retry_count"] == 0
    state.update(update)
    assert freeze_router(state) == "node_plan_beat"

    part1 = sqlite_db.get_beat_by_index(env.SQLITE_PATH, "sc_001", 0)
    part2 = sqlite_db.get_beat_by_index(env.SQLITE_PATH, "sc_001", 1)
    shifted = sqlite_db.get_beat_by_index(env.SQLITE_PATH, "sc_001", 2)
    assert part1["status"] == "planned" and part2["beat_id"].endswith("_sub")
    plan1 = json.loads(part1["beat_plan_json"])
    plan2 = json.loads(part2["beat_plan_json"])
    assert plan1["word_budget"] + plan2["word_budget"] == 600
    assert plan1["exit_constraints"] == plan2["entry_constraints"]  # midpoint stitch
    assert shifted["beat_id"] == "sc_001_beat_1"  # original beat 1 shifted to index 2


async def test_escalation_tier4_replan_routing(env):
    from fsm.nodes.node_freeze_and_escalate import freeze_router, node_freeze_and_escalate

    pointer = seed_narrative(env.SQLITE_PATH)
    seed_beat(env.SQLITE_PATH)
    state = base_state(pointer)
    state["escalation_tier"] = 3
    state["failed_beat_cache"] = [
        {"beat_id": "sc_001_beat_0", "error_code": "THREAD_PARADOX",
         "fingerprint": "dock master dies", "suggested_fix": "keep him alive"}
    ]

    # First Tier 4 entry: replan_count 0 → 1 (≤ cap 2) → plan_beat.
    update = await node_freeze_and_escalate(state)
    assert update["escalation_tier"] == 4
    assert update["replan_count"] == 1
    assert update["retry_count"] == 0
    beat = sqlite_db.get_row(env.SQLITE_PATH, "Beats", "beat_id", "sc_001_beat_0")
    assert beat["status"] == "abandoned"
    state.update(update)
    assert freeze_router(state) == "node_plan_beat"

    # Beyond the cap: paradox constraint injected → plan_chapter.
    state["escalation_tier"] = 3
    state["replan_count"] = 2
    update = await node_freeze_and_escalate(state)
    assert update["replan_count"] == 3
    assert "dock master dies" in update["active_context_package"]["paradox_constraint"]
    state.update(update)
    assert freeze_router(state) == "node_plan_chapter"


async def test_terminal_escalation_interactive_and_headless(env, monkeypatch):
    from fsm.nodes import node_freeze_and_escalate as nfe

    pointer = seed_narrative(env.SQLITE_PATH)
    state = base_state(pointer)
    state["escalation_tier"] = 4
    state["best_seen_draft"] = "The best partial draft we ever saw."

    # Interactive: pause only.
    update = await nfe.node_freeze_and_escalate(state)
    assert update == {"pause_requested": True}
    state_after = dict(state); state_after.update(update)
    assert nfe.freeze_router(state_after) == "node_human_intervention"

    # Headless: pause converts to a clean hard stop + export.
    import core.config_loader as cl
    config = cl.load_config()
    config.headless_mode = True
    monkeypatch.setattr(nfe, "load_config", lambda *a, **k: config)

    update = await nfe.node_freeze_and_escalate(state)
    assert update["hard_stop_asserted"] is True
    assert update["pause_requested"] is False
    exports = list(env.EXPORTS_DIR.glob("best_seen_*.txt"))
    assert exports and exports[0].read_text() == "The best partial draft we ever saw."
    events = [json.loads(line) for line in open(env.EVENT_LOG_PATH)]
    assert any(e["type"] == "terminal_escalation" for e in events)


# --------------------------------------------------------------------------- #
# Human intervention reconciliation                                           #
# --------------------------------------------------------------------------- #


async def test_human_edit_reconciliation_applies_thread_updates(env, monkeypatch):
    from fsm.nodes import node_human_intervention as nhi

    pointer = seed_narrative(env.SQLITE_PATH)
    sqlite_db.insert_row(
        env.SQLITE_PATH, "Threads",
        {"thread_id": "th_debt", "name": "The Debt", "description": "", "priority": 0.9, "status": "open"},
    )

    async def fake_structured(endpoint, messages, schema_model, retry_cap, **kwargs):
        return schema_model.model_validate(
            {"thread_updates": [{"thread_id": "th_debt", "new_status": "closed", "rationale": "paid off"}]}
        )

    monkeypatch.setattr(nhi.call_llm_module, "call_llm_structured", fake_structured)
    nhi.push_intervention({"action": "edit", "text": "Mara pays the cartel in full."})

    update = await nhi.node_human_intervention(base_state(pointer))
    assert update["current_draft_text"] == "Mara pays the cartel in full."
    assert update["pause_requested"] is False
    thread = sqlite_db.get_row(env.SQLITE_PATH, "Threads", "thread_id", "th_debt")
    assert thread["status"] == "closed"


async def test_human_edit_unreconciled_flag_on_double_failure(env, monkeypatch):
    from fsm.nodes import node_human_intervention as nhi

    pointer = seed_narrative(env.SQLITE_PATH)
    calls = []

    async def failing(endpoint, messages, schema_model, retry_cap, **kwargs):
        calls.append(1)
        raise RuntimeError("extraction endpoint down")

    monkeypatch.setattr(nhi.call_llm_module, "call_llm_structured", failing)
    nhi.push_intervention({"action": "edit", "text": "Edited prose."})

    update = await nhi.node_human_intervention(base_state(pointer))
    assert len(calls) == 2  # retried once
    assert update["current_draft_text"] == "Edited prose."  # edit applied regardless
    events = [json.loads(line) for line in open(env.EVENT_LOG_PATH)]
    assert any(e["type"] == "HUMAN_EDIT_UNRECONCILED" for e in events)


# --------------------------------------------------------------------------- #
# Compression                                                                 #
# --------------------------------------------------------------------------- #


async def test_compress_memory_llm_with_fallback(env, monkeypatch):
    from fsm.nodes import node_compress_memory as ncm
    from memory.raptor import get_raptor_summaries

    pointer = seed_narrative(env.SQLITE_PATH)
    sqlite_db.append_scene_prose(env.SQLITE_PATH, "sc_001", ACTIVE_PROSE, 50)
    sqlite_db.close_scene(env.SQLITE_PATH, "sc_001")

    # LLM path.
    async def fake_collect(endpoint, messages, **kwargs):
        return "Chapter summary: Mara seizes the manifest at the docks."

    monkeypatch.setattr(ncm.call_llm_module, "collect_llm_response", fake_collect)
    await ncm.node_compress_memory(base_state(pointer))
    assert "seizes the manifest" in get_raptor_summaries(env.SQLITE_PATH, "sc_001", ["chapter"])["chapter"]

    # Fallback path: both attempts fail → deterministic extraction.
    async def failing(endpoint, messages, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(ncm.call_llm_module, "collect_llm_response", failing)
    await ncm.node_compress_memory(base_state(pointer))
    summary = get_raptor_summaries(env.SQLITE_PATH, "sc_001", ["chapter"])["chapter"]
    assert summary != "" and "seizes the manifest" not in summary

"""
tests/test_context_injection.py

Context-Injection & Continuity Upgrades.

Purpose:
    Pins the cross-level context improvements:
    1. Drop-priority pruning drops distant context (arc, chapter) before the
       scene summary — the drafter's immediate continuity anchor goes last.
    2. trailing_prose: the drafter package carries the last ~300 words of
       committed prose (same scene, or prior scene's close as fallback).
    3. Beat planning entry context: when extending an open scene, the binding
       context is beat N-1's exit_constraints, not the prior scene's prose.
    4. node_plan_beat injects world_state (Graphiti facts + open threads) into
       the partitioner prompt and publishes 'planning' SSE bullets.
    5. node_plan_chapter injects character LOCATED_IN facts.
    6. node_plan_global injects the character roster.
"""

import pytest

from core import runtime, stream_bus
from fsm.state import FSM_Pointer
from memory import sqlite_db

from tests.test_sprint3 import ACTIVE_PROSE, base_state, seed_beat, seed_narrative


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SQLITE_PATH", tmp_path / "fictionwriter.db")
    monkeypatch.setattr(runtime, "STYLES_DIR", tmp_path / "styles")
    monkeypatch.setattr(runtime, "EVENT_LOG_PATH", tmp_path / "event_log.jsonl")
    monkeypatch.setattr(runtime, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(runtime, "GRAPHITI_PATH", tmp_path / "graphiti.db")
    sqlite_db.init_db(runtime.SQLITE_PATH)

    class _FakeEncoder:
        def encode(self, text):
            return text.split()

    monkeypatch.setattr("llm.tokenizer._get_tiktoken_encoder", lambda: _FakeEncoder())
    return runtime


@pytest.fixture
def bus_events(monkeypatch):
    """Capture every stream_bus publish."""
    captured = []
    real = stream_bus.publish
    monkeypatch.setattr(stream_bus, "publish", lambda ev: (captured.append(ev), real(ev)))
    return captured


# --------------------------------------------------------------------------- #
# Drop-priority pruning order                                                 #
# --------------------------------------------------------------------------- #


async def test_pruning_drops_distant_context_before_scene_summary(env, monkeypatch):
    """hnsw → arc → chapter dropped; scene summary survives to the last."""
    from fsm.nodes import node_assemble_context as nac

    pointer = seed_narrative(env.SQLITE_PATH)
    seed_beat(env.SQLITE_PATH)
    monkeypatch.setattr(
        nac, "get_raptor_summaries",
        lambda db, scene_id, levels: {"arc": "ARC_S", "chapter": "CH_S", "scene": "SCENE_S"},
    )
    # Budget passes only on the 4th check: after hnsw, arc, and chapter drop.
    checks = iter([False, False, False, True])
    monkeypatch.setattr(nac, "fits_in_budget", lambda *a, **k: next(checks))

    update = await nac.node_assemble_context(base_state(pointer))
    package = update["active_context_package"]
    assert package["hnsw_flavor"] == ""
    assert package["raptor_arc_summary"] == ""
    assert package["raptor_chapter_summary"] == ""
    assert package["raptor_scene_summary"] == "SCENE_S"


# --------------------------------------------------------------------------- #
# Trailing prose                                                              #
# --------------------------------------------------------------------------- #


async def test_trailing_prose_from_same_scene(env):
    from fsm.nodes import node_assemble_context as nac

    pointer = seed_narrative(env.SQLITE_PATH)
    seed_beat(env.SQLITE_PATH)
    sqlite_db.append_scene_prose(
        env.SQLITE_PATH, "sc_001", ACTIVE_PROSE, len(ACTIVE_PROSE.split())
    )

    update = await nac.node_assemble_context(base_state(pointer))
    trailing = update["active_context_package"]["trailing_prose"]
    assert trailing.endswith("The gulls scream overhead.")
    assert len(trailing.split()) <= 300


def test_trailing_prose_falls_back_to_previous_scene(env):
    from fsm.nodes.node_assemble_context import _trailing_prose

    seed_narrative(env.SQLITE_PATH)
    sqlite_db.append_scene_prose(
        env.SQLITE_PATH, "sc_001", ACTIVE_PROSE, len(ACTIVE_PROSE.split())
    )
    sqlite_db.close_scene(env.SQLITE_PATH, "sc_001")
    sqlite_db.insert_row(
        env.SQLITE_PATH, "Scenes",
        {"scene_id": "sc_002", "chapter_id": "ch_001", "scene_index": 1,
         "description": "", "word_budget": 400, "word_count": 0},
    )
    assert _trailing_prose(env.SQLITE_PATH, "sc_002").endswith("The gulls scream overhead.")


# --------------------------------------------------------------------------- #
# Beat planning context                                                       #
# --------------------------------------------------------------------------- #


def test_scene_entry_context_uses_prev_beat_exit_constraints(env):
    """Scene extension: beat N enters exactly where committed beat N-1 exited."""
    from fsm.nodes.node_plan_beat import _scene_entry_context

    seed_narrative(env.SQLITE_PATH)
    seed_beat(env.SQLITE_PATH, beat_index=0, status="committed")
    assert (
        _scene_entry_context(env.SQLITE_PATH, "sc_001", existing_count=1)
        == "Mara holds the manifest."
    )


async def test_plan_beat_prompt_carries_world_state(env, monkeypatch, bus_events):
    from fsm.nodes import node_plan_beat as npb

    pointer = seed_narrative(env.SQLITE_PATH)
    sqlite_db.insert_row(
        env.SQLITE_PATH, "Threads",
        {"thread_id": "th_1", "name": "The missing manifest",
         "description": "Find it.", "priority": 0.9, "status": "open"},
    )

    captured = {}

    async def fake_structured(endpoint, messages, schema_model, retry_cap, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return schema_model.model_validate(
            {"beats": [{
                "id": "b0", "scene_id": "sc_001", "beat_index": 0,
                "description": "Mara confronts the dock master.",
                "word_budget": 100, "entry_constraints": "e", "exit_constraints": "x",
            }]}
        )

    async def fake_collect(endpoint, messages, **kwargs):
        return "tailored constraint"

    async def fake_graph(entity_ids, active_event_id, max_hops=2):
        return [{"entity_a_id": "char_mara", "entity_b_id": "the_harbor",
                 "edge_type": "LOCATED_IN", "confidence": 0.95}]

    monkeypatch.setattr(npb.call_llm_module, "call_llm_structured", fake_structured)
    monkeypatch.setattr(npb.call_llm_module, "collect_llm_response", fake_collect)
    monkeypatch.setattr(npb, "query_point_in_time_subgraph", fake_graph)

    await npb.node_plan_beat(base_state(pointer))

    assert "<world_state>" in captured["prompt"]
    assert "char_mara LOCATED_IN the_harbor" in captured["prompt"]
    assert "The missing manifest" in captured["prompt"]
    planning = [e for e in bus_events if e["type"] == "planning"]
    assert planning and planning[0]["level"] == "beat"
    assert "Mara confronts the dock master." in planning[0]["text"]


# --------------------------------------------------------------------------- #
# Chapter planning context                                                    #
# --------------------------------------------------------------------------- #


async def test_plan_chapter_prompt_carries_character_locations(env, monkeypatch, bus_events):
    from fsm.nodes import node_plan_chapter as npc

    seed_narrative(env.SQLITE_PATH)
    sqlite_db.close_scene(env.SQLITE_PATH, "sc_001")  # no open scenes → planning fires

    captured = {}

    async def fake_structured(endpoint, messages, schema_model, retry_cap, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return schema_model.model_validate(
            {"scenes": [{"id": "sc_new", "description": "d", "word_budget": 500, "ordering": 0}]}
        )

    async def fake_graph(entity_ids, active_event_id, max_hops=2):
        return [{"entity_a_id": "char_mara", "entity_b_id": "the_harbor",
                 "edge_type": "Located_In"}]

    monkeypatch.setattr(npc.call_llm_module, "call_llm_structured", fake_structured)
    monkeypatch.setattr(npc, "query_point_in_time_subgraph", fake_graph)

    pointer = FSM_Pointer(arc_id="arc_001", chapter_id="ch_001", scene_id="sc_001", beat_index=0)
    await npc.node_plan_chapter(base_state(pointer))

    assert "<character_locations>" in captured["prompt"]
    assert "Mara is in the_harbor" in captured["prompt"]
    planning = [e for e in bus_events if e["type"] == "planning"]
    assert planning and planning[0]["level"] == "chapter"


# --------------------------------------------------------------------------- #
# Global planning context                                                     #
# --------------------------------------------------------------------------- #


async def test_plan_global_prompt_carries_character_roster(env, monkeypatch, bus_events):
    from fsm.nodes import node_plan_global as npg

    sqlite_db.insert_row(
        env.SQLITE_PATH, "Characters",
        {"char_id": "char_mara", "name": "Mara", "role": "protagonist",
         "description": "A smuggler."},
    )

    captured = {}

    async def fake_structured(endpoint, messages, schema_model, retry_cap, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return schema_model.model_validate(
            {"arcs": [{"id": "arc_x", "title": "T", "description": "D", "word_allocation": 100}],
             "threads": []}
        )

    monkeypatch.setattr(npg.call_llm_module, "call_llm_structured", fake_structured)

    pointer = FSM_Pointer(arc_id="", chapter_id="", scene_id="", beat_index=0)
    await npg.node_plan_global(base_state(pointer))

    assert "<character_roster>" in captured["prompt"]
    assert "Mara (protagonist): A smuggler." in captured["prompt"]
    planning = [e for e in bus_events if e["type"] == "planning"]
    assert planning and planning[0]["level"] == "global"

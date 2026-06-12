"""
tests/test_frontend_state.py

Frontend State & Background-Execution Tests.

Purpose:
    Pins the three systemic fixes behind the UI redesign:
    1. Background persistence — the generation manager holds a strong task
       reference, narrates node starts as pipeline_status events, survives with
       zero SSE consumers, and enforces single-flight (/generate → 409).
    2. Story-stream state — node_draft_prose announces beat_start before
       chunks; node_revise_prose emits draft_replaced so revisions REPLACE
       streamed text instead of appending after it.
    3. Reattach — GET /status returns the snapshot a reloading page needs.
"""

import asyncio
import json
from pathlib import Path

import pytest

from core import generation_manager, runtime, stream_bus
from fsm.state import FailureObject
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


def _tiny_graph():
    """A two-node stand-in graph compatible with astream(stream_mode='debug')."""
    from typing_extensions import TypedDict

    from langgraph.graph import END, START, StateGraph

    class S(TypedDict):
        x: int

    async def node_draft_prose(state):
        await asyncio.sleep(0.01)
        return {"x": state["x"] + 1}

    async def node_commit_transaction(state):
        return {"x": state["x"] * 2}

    g = StateGraph(S)
    g.add_node("node_draft_prose", node_draft_prose)
    g.add_node("node_commit_transaction", node_commit_transaction)
    g.add_edge(START, "node_draft_prose")
    g.add_edge("node_draft_prose", "node_commit_transaction")
    g.add_edge("node_commit_transaction", END)
    return g.compile()


# --------------------------------------------------------------------------- #
# Generation manager                                                          #
# --------------------------------------------------------------------------- #


async def test_manager_runs_in_background_and_narrates(monkeypatch, bus_events):
    """Strong task ref + pipeline_status per node start + completion event."""
    import fsm.graph as graph_module

    monkeypatch.setattr(graph_module, "compile_graph", _tiny_graph)

    assert generation_manager.start({"x": 1}, project_id="t") is True
    assert generation_manager.is_running() is True

    # Single-flight guard while running.
    assert generation_manager.start({"x": 1}) is False

    # No SSE consumer is subscribed — generation must not care.
    while generation_manager.is_running():
        await asyncio.sleep(0.01)

    stages = [e["stage"] for e in bus_events if e["type"] == "pipeline_status"]
    assert "node_draft_prose" in stages and "node_commit_transaction" in stages
    labels = [e["label"] for e in bus_events if e["type"] == "pipeline_status"]
    assert "Drafting prose…" in labels  # human-readable, not raw node names
    assert any(e["type"] == "generation_complete" for e in bus_events)

    snap = generation_manager.snapshot()
    assert snap["running"] is False
    assert snap["stage_label"] == "Complete"
    assert snap["nodes_executed"] >= 2

    # Re-arm allowed after completion.
    assert generation_manager.start({"x": 1}) is True
    while generation_manager.is_running():
        await asyncio.sleep(0.01)


async def test_manager_surfaces_errors(monkeypatch, bus_events):
    import fsm.graph as graph_module

    def broken_graph():
        from typing_extensions import TypedDict

        from langgraph.graph import END, START, StateGraph

        class S(TypedDict):
            x: int

        async def node_boom(state):
            raise RuntimeError("kaboom")

        g = StateGraph(S)
        g.add_node("node_boom", node_boom)
        g.add_edge(START, "node_boom")
        g.add_edge("node_boom", END)
        return g.compile()

    monkeypatch.setattr(graph_module, "compile_graph", broken_graph)
    assert generation_manager.start({"x": 1}) is True
    while generation_manager.is_running():
        await asyncio.sleep(0.01)

    assert any(e["type"] == "generation_error" and "kaboom" in e["error"] for e in bus_events)
    snap = generation_manager.snapshot()
    assert snap["last_error"] and snap["running"] is False


async def test_manager_cancel(monkeypatch, bus_events):
    import fsm.graph as graph_module

    def slow_graph():
        from typing_extensions import TypedDict

        from langgraph.graph import END, START, StateGraph

        class S(TypedDict):
            x: int

        async def node_slow(state):
            await asyncio.sleep(30)
            return {}

        g = StateGraph(S)
        g.add_node("node_slow", node_slow)
        g.add_edge(START, "node_slow")
        g.add_edge("node_slow", END)
        return g.compile()

    monkeypatch.setattr(graph_module, "compile_graph", slow_graph)
    assert generation_manager.start({"x": 1}) is True
    await asyncio.sleep(0.05)
    assert generation_manager.cancel() is True
    await asyncio.sleep(0.05)
    assert generation_manager.is_running() is False
    assert generation_manager.snapshot()["stage_label"] == "Stopped"


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def client(env):
    from quart import Quart

    from routes.control import control_bp
    from routes.dashboard import dashboard_bp

    app = Quart(__name__, template_folder=str(Path("templates").resolve()),
                static_folder=str(Path("static").resolve()))
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(control_bp)
    return app.test_client()


async def test_generate_409_when_running(client, monkeypatch):
    monkeypatch.setattr(generation_manager, "is_running", lambda: True)
    response = await client.post("/generate")
    assert response.status_code == 409
    assert (await response.get_json())["status"] == "already_running"


async def test_status_endpoint_snapshot(client, env, monkeypatch):
    monkeypatch.setattr(
        generation_manager, "snapshot",
        lambda: {"running": True, "stage": "node_draft_prose",
                 "stage_label": "Drafting prose…", "nodes_executed": 12,
                 "started_at": "2026-06-12T00:00:00+00:00", "finished_at": None,
                 "project_id": "default", "last_error": None},
    )
    data = await (await client.get("/status")).get_json()
    assert data["stage_label"] == "Drafting prose…"
    assert "committed_words" in data and "word_count_target" in data


# --------------------------------------------------------------------------- #
# Beat lifecycle events                                                       #
# --------------------------------------------------------------------------- #


async def test_draft_prose_announces_beat_start(env, monkeypatch, bus_events):
    from fsm.nodes import node_draft_prose as ndp

    pointer = seed_narrative(env.SQLITE_PATH)
    state = base_state(pointer)
    state["active_context_package"] = {
        "beat_description": "Mara confronts the dock master.",
        "beat_entry_constraints": "", "beat_exit_constraints": "",
        "beat_word_budget": 100, "pad_behavioral_constraints": "",
        "character_states": "", "thread_statuses": "", "graphiti_facts": "",
        "epistemic_beliefs": "", "raptor_arc_summary": "", "raptor_chapter_summary": "",
        "raptor_scene_summary": "", "hnsw_flavor": "", "author_style_baseline": "{}",
        "trailing_prose": "",
    }

    async def fake_stream(endpoint, messages, **kwargs):
        yield "Mara walks the pier."

    monkeypatch.setattr(ndp.call_llm_module, "call_llm", fake_stream)
    await ndp.node_draft_prose(state)

    types = [e["type"] for e in bus_events]
    start_idx = types.index("beat_start")
    chunk_idx = types.index("draft_chunk")
    assert start_idx < chunk_idx, "beat_start must precede the first chunk"
    assert bus_events[start_idx]["beat_id"] == "sc_001_beat_0"
    assert bus_events[start_idx]["description"] == "Mara confronts the dock master."


async def test_revise_prose_emits_draft_replaced(env, monkeypatch, bus_events):
    from fsm.nodes import node_revise_prose as nrp

    pointer = seed_narrative(env.SQLITE_PATH)
    seed_beat(env.SQLITE_PATH)
    state = base_state(pointer)
    state["current_draft_text"] = "The rope was cut by someone."
    state["critic_failures"] = [
        FailureObject(error_code="PASSIVE_VOICE", offending_text="The rope was cut by someone.",
                      suggested_fix="Active voice.", critic_source="programmatic")
    ]

    async def fake_collect(endpoint, messages, **kwargs):
        return "Someone cut the rope."

    monkeypatch.setattr(nrp.call_llm_module, "collect_llm_response", fake_collect)
    update = await nrp.node_revise_prose(state)

    replaced = [e for e in bus_events if e["type"] == "draft_replaced"]
    assert len(replaced) == 1
    assert replaced[0]["beat_id"] == "sc_001_beat_0"
    assert replaced[0]["text"] == "Someone cut the rope."
    assert replaced[0]["text"] == update["current_draft_text"]

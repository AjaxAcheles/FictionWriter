"""
tests/test_critic_tools.py

Unit tests for the critic tool registry (fsm/critic_tools.py).

Purpose:
    Verifies the OpenAI function-calling schemas are structurally valid and
    that execute_critic_tool dispatches each tool to the right memory function
    with LLM args and ToolContext args merged correctly, serializes results to
    JSON, and converts every failure mode (backend exception, unknown tool,
    missing arguments) into a JSON error string instead of raising.

    All three memory functions are monkeypatched — no SQLite, FalkorDB, or
    ChromaDB backends are touched.
"""

import json
from pathlib import Path

from fsm import critic_tools
from fsm.critic_tools import CRITIC_TOOL_SCHEMAS, ToolContext, execute_critic_tool

EXPECTED_TOOLS = {
    "get_raptor_summaries",
    "query_point_in_time_subgraph",
    "query_flavor_vectors",
}


def _ctx(tmp_path: Path, chapter_id="ch-3") -> ToolContext:
    return ToolContext(
        db_path=tmp_path / "fictionwriter.db",
        scene_id="scene-7",
        active_event_id="event-42",
        chapter_id=chapter_id,
    )


# ---------------------------------------------------------------------------
# Schema structure
# ---------------------------------------------------------------------------


def test_schemas_structurally_valid():
    assert {s["function"]["name"] for s in CRITIC_TOOL_SCHEMAS} == EXPECTED_TOOLS
    for schema in CRITIC_TOOL_SCHEMAS:
        assert schema["type"] == "function"
        fn = schema["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict) and params["properties"]
        assert isinstance(params["required"], list)
        for key in params["required"]:
            assert key in params["properties"]


def test_schemas_expose_only_llm_decidable_params():
    by_name = {s["function"]["name"]: s["function"]["parameters"] for s in CRITIC_TOOL_SCHEMAS}
    assert set(by_name["get_raptor_summaries"]["properties"]) == {"levels"}
    assert set(by_name["query_point_in_time_subgraph"]["properties"]) == {
        "entity_ids",
        "max_hops",
    }
    assert set(by_name["query_flavor_vectors"]["properties"]) == {
        "query_text",
        "n_results",
    }


# ---------------------------------------------------------------------------
# Dispatch: argument merging (LLM args + ToolContext injection)
# ---------------------------------------------------------------------------


async def test_raptor_dispatch_injects_db_path_and_scene_id(tmp_path, monkeypatch):
    calls = []

    def fake_raptor(*, db_path, scene_id, levels):
        calls.append({"db_path": db_path, "scene_id": scene_id, "levels": levels})
        return {"chapter": "Chapter summary.", "arc": ""}

    monkeypatch.setattr(critic_tools.raptor, "get_raptor_summaries", fake_raptor)
    ctx = _ctx(tmp_path)
    out = await execute_critic_tool("get_raptor_summaries", {"levels": ["chapter", "arc"]}, ctx)

    assert calls == [
        {
            "db_path": tmp_path / "fictionwriter.db",
            "scene_id": "scene-7",
            "levels": ["chapter", "arc"],
        }
    ]
    assert json.loads(out) == {"chapter": "Chapter summary.", "arc": ""}


async def test_graphiti_dispatch_injects_active_event_id(tmp_path, monkeypatch):
    calls = []

    async def fake_graph(*, entity_ids, active_event_id, max_hops):
        calls.append(
            {"entity_ids": entity_ids, "active_event_id": active_event_id, "max_hops": max_hops}
        )
        return [{"edge_id": "e1", "entity_a_id": "mara", "entity_b_id": "knife"}]

    monkeypatch.setattr(critic_tools.graphiti_client, "query_point_in_time_subgraph", fake_graph)
    ctx = _ctx(tmp_path)
    out = await execute_critic_tool(
        "query_point_in_time_subgraph", {"entity_ids": ["mara", "knife"], "max_hops": 3}, ctx
    )

    assert calls == [
        {"entity_ids": ["mara", "knife"], "active_event_id": "event-42", "max_hops": 3}
    ]
    assert json.loads(out) == [{"edge_id": "e1", "entity_a_id": "mara", "entity_b_id": "knife"}]


async def test_graphiti_dispatch_defaults_max_hops(tmp_path, monkeypatch):
    calls = []

    async def fake_graph(*, entity_ids, active_event_id, max_hops):
        calls.append(max_hops)
        return []

    monkeypatch.setattr(critic_tools.graphiti_client, "query_point_in_time_subgraph", fake_graph)
    out = await execute_critic_tool(
        "query_point_in_time_subgraph", {"entity_ids": ["mara"]}, _ctx(tmp_path)
    )
    assert calls == [2]
    assert json.loads(out) == []


async def test_chroma_dispatch_injects_exclude_chapter_id(tmp_path, monkeypatch):
    calls = []

    def fake_chroma(*, query_text, n_results, exclude_chapter_id):
        calls.append(
            {
                "query_text": query_text,
                "n_results": n_results,
                "exclude_chapter_id": exclude_chapter_id,
            }
        )
        return [{"text": "old prose", "metadata": {"chapter_id": "ch-1"}, "distance": 0.2}]

    monkeypatch.setattr(critic_tools.chroma_client, "query_flavor_vectors", fake_chroma)
    out = await execute_critic_tool(
        "query_flavor_vectors", {"query_text": "rusty knife motif", "n_results": 2}, _ctx(tmp_path)
    )

    assert calls == [
        {"query_text": "rusty knife motif", "n_results": 2, "exclude_chapter_id": "ch-3"}
    ]
    assert json.loads(out)[0]["text"] == "old prose"


async def test_chroma_dispatch_defaults_and_none_chapter(tmp_path, monkeypatch):
    calls = []

    def fake_chroma(*, query_text, n_results, exclude_chapter_id):
        calls.append({"n_results": n_results, "exclude_chapter_id": exclude_chapter_id})
        return []

    monkeypatch.setattr(critic_tools.chroma_client, "query_flavor_vectors", fake_chroma)
    out = await execute_critic_tool(
        "query_flavor_vectors", {"query_text": "x"}, _ctx(tmp_path, chapter_id=None)
    )
    assert calls == [{"n_results": 5, "exclude_chapter_id": None}]
    assert json.loads(out) == []


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


async def test_non_json_native_values_serialized_via_default_str(tmp_path, monkeypatch):
    def fake_raptor(*, db_path, scene_id, levels):
        return {"path": db_path}  # Path is not JSON-native — default=str must apply

    monkeypatch.setattr(critic_tools.raptor, "get_raptor_summaries", fake_raptor)
    out = await execute_critic_tool("get_raptor_summaries", {"levels": ["beat"]}, _ctx(tmp_path))
    assert json.loads(out) == {"path": str(tmp_path / "fictionwriter.db")}


# ---------------------------------------------------------------------------
# Failure modes: always a JSON error string, never an exception
# ---------------------------------------------------------------------------


async def test_backend_exception_returns_json_error(tmp_path, monkeypatch):
    def exploding(*, query_text, n_results, exclude_chapter_id):
        raise RuntimeError("ChromaDB not initialized")

    monkeypatch.setattr(critic_tools.chroma_client, "query_flavor_vectors", exploding)
    out = await execute_critic_tool("query_flavor_vectors", {"query_text": "x"}, _ctx(tmp_path))
    payload = json.loads(out)
    assert "error" in payload
    assert "ChromaDB not initialized" in payload["error"]


async def test_async_backend_exception_returns_json_error(tmp_path, monkeypatch):
    async def exploding(*, entity_ids, active_event_id, max_hops):
        raise ConnectionError("FalkorDB down")

    monkeypatch.setattr(critic_tools.graphiti_client, "query_point_in_time_subgraph", exploding)
    out = await execute_critic_tool(
        "query_point_in_time_subgraph", {"entity_ids": ["mara"]}, _ctx(tmp_path)
    )
    payload = json.loads(out)
    assert "error" in payload
    assert "FalkorDB down" in payload["error"]


async def test_unknown_tool_returns_json_error(tmp_path):
    out = await execute_critic_tool("summon_dragon", {}, _ctx(tmp_path))
    payload = json.loads(out)
    assert "error" in payload
    assert "summon_dragon" in payload["error"]


async def test_missing_required_arg_returns_json_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        critic_tools.raptor, "get_raptor_summaries", lambda **kw: {}
    )
    out = await execute_critic_tool("get_raptor_summaries", {}, _ctx(tmp_path))
    payload = json.loads(out)
    assert "error" in payload
    assert "levels" in payload["error"]

"""
tests/test_agentic_critics.py

Agentic adversarial-critics loop test suite (Unit 4).

Purpose:
    Exercises node_adversarial_critics' bounded tool-calling "pull" loop in
    isolation against mocked transports — no live LLM and no real databases.
    Verifies the dashboard event contract (agent_thought / agent_action /
    agent_observation / critic_result), the tool-role message round-trip into
    the final structured verdict, the MAX_AGENT_ITERATIONS ceiling, the
    no-tools fast path, and end-to-end NONE filtering + THREAD_PARADOX handling
    through node_adversarial_critics.
"""

import json
from pathlib import Path

import pytest

from core import runtime, stream_bus
from fsm.nodes import node_adversarial_critics as nac
from fsm.state import FSM_Pointer, FailureObject
from memory import sqlite_db


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated SQLite store under tmp_path so get_open_threads has a real DB."""
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SQLITE_PATH", tmp_path / "fictionwriter.db")
    sqlite_db.init_db(runtime.SQLITE_PATH)
    return runtime


@pytest.fixture
def bus_events(monkeypatch):
    """Capture every stream_bus publish."""
    captured: list = []
    real = stream_bus.publish
    monkeypatch.setattr(stream_bus, "publish", lambda ev: (captured.append(ev), real(ev)))
    return captured


def _pointer() -> FSM_Pointer:
    return FSM_Pointer(arc_id="arc_001", chapter_id="ch_001", scene_id="sc_001", beat_index=2)


def _config(concurrent: bool = False):
    from core.config_loader import EndpointConfig, load_config

    config = load_config()
    config.endpoints.critic = EndpointConfig(
        base_url="http://mock-endpoint/v1",
        api_key="test-key",
        model_name="test-model",
        supports_inference_antislop=False,
        tokenizer_family="char_heuristic",
        supports_concurrent_critics=concurrent,
        grammar_constraint_strategy="json_mode",
    )
    return config


def _base_state(pointer: FSM_Pointer) -> dict:
    return {
        "project_id": "test",
        "fsm_pointer": pointer,
        "active_context_package": {},
        "current_draft_text": "Mara opened the brass-bound chest.",
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


def _content_event(text: str) -> dict:
    return {"type": "content", "text": text}


def _tool_calls_event(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {
        "type": "tool_calls",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def _none_verdict(critic: str) -> FailureObject:
    return FailureObject(
        error_code="NONE", offending_text="", suggested_fix="", critic_source=critic
    )


# --------------------------------------------------------------------------- #
# _run_critic: one tool round-trip                                            #
# --------------------------------------------------------------------------- #


async def test_run_critic_tool_round_trip(env, monkeypatch, bus_events):
    """First iteration emits content + a tool call; second iteration only content."""
    calls_seen: list[list[dict]] = []
    iteration = {"n": 0}

    async def fake_call_llm(endpoint, messages, tools=None, **kwargs):
        # Snapshot the messages array as passed each turn.
        calls_seen.append([dict(m) for m in messages])
        assert tools is nac.CRITIC_TOOL_SCHEMAS
        if iteration["n"] == 0:
            iteration["n"] += 1
            yield _content_event("I should verify the chest's contents.")
            yield _tool_calls_event("get_raptor_summaries", {"levels": ["chapter"]})
        else:
            yield _content_event("Confirmed; the draft is consistent.")

    tool_received: dict = {}

    async def fake_execute(name, args, ctx):
        tool_received["name"] = name
        tool_received["args"] = args
        tool_received["ctx"] = ctx
        return json.dumps({"chapter": "Mara hid the chest in the hold."})

    structured_received: dict = {}

    async def fake_structured(endpoint, messages, schema_model, retry_cap, **kwargs):
        structured_received["messages"] = messages
        return _none_verdict("continuity")

    monkeypatch.setattr(nac.call_llm_module, "call_llm", fake_call_llm)
    monkeypatch.setattr(nac, "execute_critic_tool", fake_execute)
    monkeypatch.setattr(nac.call_llm_module, "call_llm_structured", fake_structured)

    pointer = _pointer()
    failure = await nac._run_critic("continuity", _config(), "draft", {}, [], pointer)

    assert failure.error_code == "NONE"

    # --- contract-exact event sequence on the bus
    types = [e["type"] for e in bus_events]
    assert types == [
        "agent_thought",
        "agent_action",
        "agent_observation",
        "agent_thought",
        "critic_result",
    ]

    thought = bus_events[0]
    assert thought == {
        "type": "agent_thought",
        "critic": "continuity",
        "text": "I should verify the chest's contents.",
    }
    action = bus_events[1]
    assert action == {
        "type": "agent_action",
        "critic": "continuity",
        "tool": "get_raptor_summaries",
        "args": {"levels": ["chapter"]},
    }
    obs = bus_events[2]
    assert obs["type"] == "agent_observation"
    assert obs["critic"] == "continuity"
    assert obs["tool"] == "get_raptor_summaries"
    assert set(obs.keys()) == {"type", "critic", "tool", "summary", "result_chars"}
    raw = json.dumps({"chapter": "Mara hid the chest in the hold."})
    assert obs["result_chars"] == len(raw)

    # --- execute_critic_tool got merged args + the correct ToolContext
    assert tool_received["name"] == "get_raptor_summaries"
    assert tool_received["args"] == {"levels": ["chapter"]}
    ctx = tool_received["ctx"]
    assert ctx.db_path == runtime.SQLITE_PATH
    assert ctx.scene_id == "sc_001"
    assert ctx.active_event_id == "sc_001_beat_2"
    assert ctx.chapter_id == "ch_001"

    # --- the tool-role message was appended and passed into the structured call
    tool_msgs = [m for m in structured_received["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert tool_msgs[0]["content"] == raw

    # final user verdict instruction closes the transcript
    assert structured_received["messages"][-1]["role"] == "user"


# --------------------------------------------------------------------------- #
# _run_critic: ceiling on tool-calling turns                                  #
# --------------------------------------------------------------------------- #


async def test_run_critic_stops_at_max_iterations(env, monkeypatch, bus_events):
    """A model that always calls tools is cut off after MAX_AGENT_ITERATIONS."""
    llm_turns = {"n": 0}

    async def fake_call_llm(endpoint, messages, tools=None, **kwargs):
        llm_turns["n"] += 1
        yield _tool_calls_event("query_flavor_vectors", {"query_text": "chest"}, call_id="c")

    async def fake_execute(name, args, ctx):
        return json.dumps([])

    async def fake_structured(endpoint, messages, schema_model, retry_cap, **kwargs):
        return _none_verdict("continuity")

    monkeypatch.setattr(nac.call_llm_module, "call_llm", fake_call_llm)
    monkeypatch.setattr(nac, "execute_critic_tool", fake_execute)
    monkeypatch.setattr(nac.call_llm_module, "call_llm_structured", fake_structured)

    await nac._run_critic("continuity", _config(), "draft", {}, [], _pointer())

    assert llm_turns["n"] == nac.MAX_AGENT_ITERATIONS
    action_count = sum(1 for e in bus_events if e["type"] == "agent_action")
    assert action_count == nac.MAX_AGENT_ITERATIONS


# --------------------------------------------------------------------------- #
# _run_critic: no tool calls                                                  #
# --------------------------------------------------------------------------- #


async def test_run_critic_no_tools_single_iteration(env, monkeypatch, bus_events):
    """A model that never calls tools: one turn, no action/observation events."""
    llm_turns = {"n": 0}

    async def fake_call_llm(endpoint, messages, tools=None, **kwargs):
        llm_turns["n"] += 1
        yield _content_event("The draft is clean.")

    async def fake_execute(name, args, ctx):
        raise AssertionError("execute_critic_tool must not be called")

    async def fake_structured(endpoint, messages, schema_model, retry_cap, **kwargs):
        return _none_verdict("dialogue")

    monkeypatch.setattr(nac.call_llm_module, "call_llm", fake_call_llm)
    monkeypatch.setattr(nac, "execute_critic_tool", fake_execute)
    monkeypatch.setattr(nac.call_llm_module, "call_llm_structured", fake_structured)

    failure = await nac._run_critic("dialogue", _config(), "draft", {}, [], _pointer())

    assert llm_turns["n"] == 1
    assert failure.error_code == "NONE"
    types = [e["type"] for e in bus_events]
    assert "agent_action" not in types
    assert "agent_observation" not in types
    assert types == ["agent_thought", "critic_result"]


# --------------------------------------------------------------------------- #
# node_adversarial_critics: NONE filtering + THREAD_PARADOX end-to-end        #
# --------------------------------------------------------------------------- #


async def test_node_none_filtering_and_paradox(env, monkeypatch, bus_events):
    """
    Serial run with mocked verdicts: continuity → THREAD_PARADOX, dialogue → NONE,
    pacing → PACING_DRAG. NONE filtered; paradox sets has_paradox + cache.
    """

    async def fake_call_llm(endpoint, messages, tools=None, **kwargs):
        # No tool calls — drive straight to the structured verdict.
        yield _content_event("thinking")

    verdicts = {
        "continuity": FailureObject(
            error_code="THREAD_PARADOX",
            offending_text="Mara dies at the pier",
            suggested_fix="Resolve thread T1 before her death",
            critic_source="continuity",
        ),
        "dialogue": _none_verdict("dialogue"),
        "pacing": FailureObject(
            error_code="PACING_DRAG",
            offending_text="three paragraphs of inventory",
            suggested_fix="Trim the inventory beat",
            critic_source="pacing",
        ),
    }

    # Serial mode preserves CRITICS order, so drive verdicts by call sequence.
    seq = {"i": 0}
    order = list(nac.CRITICS)

    async def fake_structured_seq(endpoint, messages, schema_model, retry_cap, **kwargs):
        critic = order[seq["i"]]
        seq["i"] += 1
        return verdicts[critic]

    monkeypatch.setattr(nac.call_llm_module, "call_llm", fake_call_llm)
    monkeypatch.setattr(nac.call_llm_module, "call_llm_structured", fake_structured_seq)

    state = _base_state(_pointer())
    update = await nac.node_adversarial_critics(state)

    codes = sorted(f.error_code for f in update["critic_failures"])
    assert codes == ["PACING_DRAG", "THREAD_PARADOX"]  # NONE filtered out
    assert update["has_paradox"] is True
    assert len(update["failed_beat_cache"]) == 1
    cache = update["failed_beat_cache"][0]
    assert cache["beat_id"] == "sc_001_beat_2"
    assert cache["error_code"] == "THREAD_PARADOX"
    assert cache["fingerprint"] == "Mara dies at the pier"

    # critic_result published for each critic.
    results = [e for e in bus_events if e["type"] == "critic_result"]
    assert {e["critic"] for e in results} == {"continuity", "dialogue", "pacing"}

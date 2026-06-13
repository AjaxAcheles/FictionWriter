"""
tests/test_llm_tools.py

LLM Tool-Call Streaming Test Suite (Unit 1 of the agentic critics work).

Purpose:
    Exercises call_llm()'s tool-calling extensions against mocked httpx
    transports — no live endpoint is required for any test in this file.

Tests:
    test_tools_none_streaming_backward_compat   — tools=None still yields plain str chunks.
    test_tools_none_nonstream_backward_compat   — tools=None stream=False yields one full string.
    test_tools_streaming_content_events         — tools provided: content deltas arrive as dicts.
    test_tool_call_fragments_buffered           — Multi-fragment arguments assemble into one
                                                  tool_calls event with valid concatenated JSON.
    test_parallel_tool_calls_buffered_by_index  — Distinct index values buffer independently.
    test_nonstream_message_tool_calls           — stream=False message.tool_calls yields the
                                                  final-shape event (plus content if present).
    test_payload_tools_only_when_provided       — tools/tool_choice appended to the payload
                                                  only when tools is passed.
    test_tool_call_deltas_ignored_without_tools — tools=None drops tool_call deltas silently.
"""

import json

import httpx

import llm.call_llm as call_llm_module
from core.config_loader import EndpointConfig
from llm.call_llm import call_llm

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Search the knowledge graph.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
]


def _endpoint() -> EndpointConfig:
    return EndpointConfig(
        base_url="http://mock-endpoint/v1",
        api_key="test-key",
        model_name="test-model",
        supports_inference_antislop=False,
        tokenizer_family="char_heuristic",
        supports_concurrent_critics=True,
        grammar_constraint_strategy="json_mode",
    )


def _sse_body_from_deltas(deltas: list[dict]) -> bytes:
    """Build an OpenAI-format SSE byte stream from raw delta dicts."""
    lines = ["data: " + json.dumps({"choices": [{"delta": d}]}) for d in deltas]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


def _patch_transport(monkeypatch, handler) -> None:
    """Route call_llm's internally created AsyncClient through a MockTransport."""
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(call_llm_module.httpx, "AsyncClient", factory)


def _sse_handler(deltas: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body_from_deltas(deltas),
        )

    return handler


# ---------------------------------------------------------------------------
# Backward compatibility — tools=None
# ---------------------------------------------------------------------------


async def test_tools_none_streaming_backward_compat(monkeypatch):
    """Assert tools=None streaming yields plain str chunks exactly as before."""
    deltas = [{"role": "assistant"}, {"content": "The "}, {"content": "harbor."}]
    _patch_transport(monkeypatch, _sse_handler(deltas))

    received = [c async for c in call_llm(_endpoint(), [{"role": "user", "content": "go"}], stream=True)]
    assert received == ["The ", "harbor."]
    assert all(isinstance(c, str) for c in received)


async def test_tools_none_nonstream_backward_compat(monkeypatch):
    """Assert tools=None stream=False yields exactly one full-response string."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "Full text."}}]})

    _patch_transport(monkeypatch, handler)
    received = [c async for c in call_llm(_endpoint(), [{"role": "user", "content": "go"}], stream=False)]
    assert received == ["Full text."]


async def test_tool_call_deltas_ignored_without_tools(monkeypatch):
    """Assert tools=None silently drops tool_call deltas and yields only content strings."""
    deltas = [
        {"content": "thinking"},
        {"tool_calls": [{"index": 0, "id": "x", "function": {"name": "f", "arguments": "{}"}}]},
    ]
    _patch_transport(monkeypatch, _sse_handler(deltas))

    received = [c async for c in call_llm(_endpoint(), [{"role": "user", "content": "go"}], stream=True)]
    assert received == ["thinking"]


# ---------------------------------------------------------------------------
# Tool-call streaming — content events and fragment buffering
# ---------------------------------------------------------------------------


async def test_tools_streaming_content_events(monkeypatch):
    """Assert content deltas arrive as {"type": "content", "text": ...} dicts when tools given."""
    deltas = [{"content": "Checking "}, {"content": "memory."}]
    _patch_transport(monkeypatch, _sse_handler(deltas))

    received = [
        c async for c in call_llm(_endpoint(), [{"role": "user", "content": "go"}], stream=True, tools=TOOLS)
    ]
    assert received == [
        {"type": "content", "text": "Checking "},
        {"type": "content", "text": "memory."},
    ]


async def test_tool_call_fragments_buffered(monkeypatch):
    """
    Assert a multi-fragment tool-call argument stream is buffered into one final
    tool_calls event whose concatenated arguments parse as valid JSON.
    """
    deltas = [
        {"content": "Let me look that up."},
        {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                         "function": {"name": "search_memory", "arguments": ""}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": '{"qu'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": 'ery": "bra'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": 'ss key"}'}}]},
    ]
    _patch_transport(monkeypatch, _sse_handler(deltas))

    received = [
        c async for c in call_llm(_endpoint(), [{"role": "user", "content": "go"}], stream=True, tools=TOOLS)
    ]
    assert received[0] == {"type": "content", "text": "Let me look that up."}
    final = received[-1]
    assert final["type"] == "tool_calls"
    assert final["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "search_memory", "arguments": '{"query": "brass key"}'},
        }
    ]
    assert json.loads(final["tool_calls"][0]["function"]["arguments"]) == {"query": "brass key"}
    # Exactly one terminal tool_calls event, after all content events.
    assert [e["type"] for e in received] == ["content", "tool_calls"]


async def test_parallel_tool_calls_buffered_by_index(monkeypatch):
    """Assert deltas with distinct index values accumulate into independent tool calls."""
    deltas = [
        {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "search_memory", "arguments": '{"query":'}}]},
        {"tool_calls": [{"index": 1, "id": "call_b", "function": {"name": "get_scene", "arguments": '{"scene_id":'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": ' "key"}'}}]},
        {"tool_calls": [{"index": 1, "function": {"arguments": ' "s7"}'}}]},
    ]
    _patch_transport(monkeypatch, _sse_handler(deltas))

    received = [
        c async for c in call_llm(_endpoint(), [{"role": "user", "content": "go"}], stream=True, tools=TOOLS)
    ]
    assert len(received) == 1
    final = received[0]
    assert final["type"] == "tool_calls"
    assert final["tool_calls"] == [
        {"id": "call_a", "type": "function",
         "function": {"name": "search_memory", "arguments": '{"query": "key"}'}},
        {"id": "call_b", "type": "function",
         "function": {"name": "get_scene", "arguments": '{"scene_id": "s7"}'}},
    ]


# ---------------------------------------------------------------------------
# Non-stream path with tools
# ---------------------------------------------------------------------------


async def test_nonstream_message_tool_calls(monkeypatch):
    """Assert stream=False with message.tool_calls yields content then the final-shape event."""
    tool_calls = [
        {
            "id": "call_9",
            "type": "function",
            "function": {"name": "search_memory", "arguments": '{"query": "lighthouse"}'},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Searching.", "tool_calls": tool_calls}}]},
        )

    _patch_transport(monkeypatch, handler)
    received = [
        c async for c in call_llm(_endpoint(), [{"role": "user", "content": "go"}], stream=False, tools=TOOLS)
    ]
    assert received == [
        {"type": "content", "text": "Searching."},
        {"type": "tool_calls", "tool_calls": tool_calls},
    ]


async def test_nonstream_tool_calls_without_content(monkeypatch):
    """Assert stream=False with null content and tool_calls yields only the tool_calls event."""
    tool_calls = [
        {"id": "call_2", "type": "function", "function": {"name": "search_memory", "arguments": "{}"}}
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": None, "tool_calls": tool_calls}}]},
        )

    _patch_transport(monkeypatch, handler)
    received = [
        c async for c in call_llm(_endpoint(), [{"role": "user", "content": "go"}], stream=False, tools=TOOLS)
    ]
    assert received == [{"type": "tool_calls", "tool_calls": tool_calls}]


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


async def test_payload_tools_only_when_provided(monkeypatch):
    """Assert tools/tool_choice appear in the request payload only when tools is passed."""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _patch_transport(monkeypatch, handler)

    async for _ in call_llm(_endpoint(), [{"role": "user", "content": "go"}], stream=False):
        pass
    async for _ in call_llm(_endpoint(), [{"role": "user", "content": "go"}], stream=False, tools=TOOLS):
        pass
    async for _ in call_llm(
        _endpoint(), [{"role": "user", "content": "go"}], stream=False, tools=TOOLS, tool_choice="required"
    ):
        pass

    no_tools, with_tools, required = payloads
    assert "tools" not in no_tools
    assert "tool_choice" not in no_tools
    assert with_tools["tools"] == TOOLS
    assert with_tools["tool_choice"] == "auto"
    assert required["tool_choice"] == "required"

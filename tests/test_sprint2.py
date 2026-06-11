"""
tests/test_sprint2.py

Sprint 2 API Adapter Layer Test Suite.

Purpose:
    Exercises the full inference adapter layer in isolation against mocked httpx
    transports — no live endpoint is required for any test in this file.

Tests:
    test_gbnf_compiles_failureobject       — GBNF output matches the golden grammar string.
    test_gbnf_deterministic                — Identical schema in → byte-identical grammar out.
    test_call_llm_streaming_assembly       — SSE chunks yielded in order, full string assembled.
    test_call_llm_nonstream_single_chunk   — stream=False yields exactly one full-response chunk.
    test_call_llm_transient_retry          — Two ReadTimeouts then success; 5s/15s backoff order.
    test_call_llm_retry_exhaustion         — Three transport failures raise after exactly 3 attempts.
    test_model_validate_retry_cap_enforced — Invalid output stops at cap, fallback fires, hard error.
    test_extraction_fallback_recovers_fenced_json — ```json fenced output recovered and validated.
    test_grammar_strategy_selection        — "gbnf" attaches grammar; "json_mode" sets vendor flag.
    test_llm_io_logging                    — One call writes exactly one llm_io.log JSON line.
    test_prompt_templates_render           — The five Sprint 2 templates render non-empty output.
    test_chroma_roundtrip                  — Init → add → query returns the seeded nearest item.
    test_chroma_empty_query_returns_empty  — Querying an empty collection returns [] cleanly.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from jinja2 import meta

import llm.call_llm as call_llm_module
from fsm.state import FailureObject
from llm.call_llm import (
    StructuredOutputError,
    _extract_first_json_object,
    call_llm,
    call_llm_structured,
    collect_llm_response,
)
from llm.gbnf_compiler import json_schema_to_gbnf
from memory import chroma_client
from core.config_loader import EndpointConfig
from prompts.prompt_loader import PromptLoader

VALID_FAILURE_JSON = json.dumps(
    {
        "error_code": "CONTINUITY_BREAK",
        "offending_text": "the brass key was still in the drawer",
        "suggested_fix": "The key moved to Mara's pocket in chapter two.",
        "critic_source": "continuity",
    }
)


def _endpoint(strategy: str = "json_mode") -> EndpointConfig:
    return EndpointConfig(
        base_url="http://mock-endpoint/v1",
        api_key="test-key",
        model_name="test-model",
        supports_inference_antislop=False,
        tokenizer_family="char_heuristic",
        supports_concurrent_critics=True,
        grammar_constraint_strategy=strategy,
    )


def _sse_body(chunks: list[str]) -> bytes:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": c}}]})
        for c in chunks
    ]
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


def _patch_sleep(monkeypatch) -> list:
    """Replace asyncio.sleep inside call_llm with a recorder (no real waiting)."""
    recorded: list = []

    async def fake_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(call_llm_module.asyncio, "sleep", fake_sleep)
    return recorded


# ---------------------------------------------------------------------------
# GBNF compiler
# ---------------------------------------------------------------------------


def test_gbnf_compiles_failureobject():
    """
    Assert the compiled FailureObject grammar matches the golden string.

    Purpose:
        The compiler must be pure and deterministic — identical schema in,
        byte-identical grammar out — so the grammar attached to "gbnf" endpoint
        payloads is stable across runs and versions of this codebase.
    """
    golden = (
        'root ::= ws "{" ws "\\"error_code\\"" ws ":" ws string '
        '"," ws "\\"offending_text\\"" ws ":" ws string '
        '"," ws "\\"suggested_fix\\"" ws ":" ws string '
        '"," ws "\\"critic_source\\"" ws ":" ws string "}" ws\n'
        'string ::= "\\"" char* "\\"" ws\n'
        'char ::= [^"\\\\\\x00-\\x1F] | "\\\\" (["\\\\/bfnrt] | "u" '
        "[0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F])\n"
        'number ::= "-"? ([0-9] | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [-+]? [0-9]+)? ws\n'
        'integer ::= "-"? ([0-9] | [1-9] [0-9]*) ws\n'
        'boolean ::= ("true" | "false") ws\n'
        'null ::= "null" ws\n'
        "ws ::= [ \\t\\n\\r]*\n"
    )
    assert json_schema_to_gbnf(FailureObject) == golden


def test_gbnf_deterministic():
    """Identical schema in → byte-identical grammar string out, across calls."""
    assert json_schema_to_gbnf(FailureObject) == json_schema_to_gbnf(FailureObject)


# ---------------------------------------------------------------------------
# call_llm — streaming, retry, grammar
# ---------------------------------------------------------------------------


async def test_call_llm_streaming_assembly(monkeypatch):
    """
    Assert SSE chunks are yielded in order and assemble to the full response.

    Purpose:
        node_draft_prose relays these chunks to the Quart SSE endpoint in Sprint 3 —
        ordering and lossless assembly are the contract.
    """
    expected_chunks = ["The ", "harbor ", "lights ", "flickered."]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body(expected_chunks),
        )

    _patch_transport(monkeypatch, handler)
    received = [c async for c in call_llm(_endpoint(), [{"role": "user", "content": "go"}], stream=True)]
    assert received == expected_chunks
    assert "".join(received) == "The harbor lights flickered."


async def test_call_llm_nonstream_single_chunk(monkeypatch):
    """Assert stream=False yields exactly one chunk carrying the full response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Full response text."}}]},
        )

    _patch_transport(monkeypatch, handler)
    received = [c async for c in call_llm(_endpoint(), [{"role": "user", "content": "go"}], stream=False)]
    assert received == ["Full response text."]


async def test_call_llm_transient_retry(monkeypatch):
    """
    Assert two ReadTimeouts then success → 3 attempts total, 5s then 15s backoff.

    Purpose:
        The transient retry loop keeps the FSM stable when an inference endpoint
        restarts mid-generation. Backoff order (5s, 15s) is asserted exactly.
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise httpx.ReadTimeout("simulated timeout", request=request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "recovered"}}]}
        )

    _patch_transport(monkeypatch, handler)
    sleeps = _patch_sleep(monkeypatch)

    result = await collect_llm_response(_endpoint(), [{"role": "user", "content": "go"}])
    assert result == "recovered"
    assert attempts["n"] == 3
    assert sleeps == [5, 15]


async def test_call_llm_retry_exhaustion(monkeypatch):
    """Assert three consecutive transport failures raise after exactly 3 attempts."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("endpoint down", request=request)

    _patch_transport(monkeypatch, handler)
    _patch_sleep(monkeypatch)

    with pytest.raises(httpx.ConnectError):
        await collect_llm_response(_endpoint(), [{"role": "user", "content": "go"}])
    assert attempts["n"] == 3


async def test_grammar_strategy_selection(monkeypatch):
    """
    Assert "gbnf" endpoints attach the compiled grammar and "json_mode" endpoints
    set the vendor JSON-mode flag instead.
    """
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": VALID_FAILURE_JSON}}]})

    _patch_transport(monkeypatch, handler)
    grammar = json_schema_to_gbnf(FailureObject)

    await collect_llm_response(_endpoint("gbnf"), [{"role": "user", "content": "go"}], grammar=grammar)
    await collect_llm_response(_endpoint("json_mode"), [{"role": "user", "content": "go"}], grammar=grammar)

    gbnf_payload, json_mode_payload = payloads
    assert gbnf_payload["grammar"] == grammar
    assert "response_format" not in gbnf_payload
    assert json_mode_payload["response_format"] == {"type": "json_object"}
    assert "grammar" not in json_mode_payload


# ---------------------------------------------------------------------------
# Bounded schema-validation sequence
# ---------------------------------------------------------------------------


async def test_model_validate_retry_cap_enforced(monkeypatch):
    """
    Assert repeatedly schema-invalid output stops at retry cap, runs the extraction
    fallback, and raises StructuredOutputError — never loops indefinitely.
    """
    calls = {"n": 0}

    async def fake_collect(endpoint, messages, **kwargs):
        calls["n"] += 1
        return "I am not JSON at all, and contain no braces."

    monkeypatch.setattr(call_llm_module, "collect_llm_response", fake_collect)

    with pytest.raises(StructuredOutputError):
        await call_llm_structured(_endpoint(), [{"role": "user", "content": "go"}], FailureObject, retry_cap=3)
    assert calls["n"] == 3


async def test_extraction_fallback_recovers_fenced_json(monkeypatch):
    """
    Assert output wrapped in ```json fences is recovered by the lenient pass after
    cap exhaustion and validates into a FailureObject instance.
    """
    fenced = f"```json\n{VALID_FAILURE_JSON}\n```"

    async def fake_collect(endpoint, messages, **kwargs):
        return fenced

    monkeypatch.setattr(call_llm_module, "collect_llm_response", fake_collect)

    result = await call_llm_structured(
        _endpoint(), [{"role": "user", "content": "go"}], FailureObject, retry_cap=2
    )
    assert isinstance(result, FailureObject)
    assert result.error_code == "CONTINUITY_BREAK"
    assert result.critic_source == "continuity"


def test_extract_first_json_object_balanced_scan():
    """Braces inside string literals must not unbalance the extraction scan."""
    text = 'preamble {"a": "value with } brace", "b": {"c": 1}} trailing {"d": 2}'
    assert _extract_first_json_object(text) == '{"a": "value with } brace", "b": {"c": 1}}'
    assert _extract_first_json_object("no braces here") is None


# ---------------------------------------------------------------------------
# LLM IO logging
# ---------------------------------------------------------------------------


async def test_llm_io_logging(monkeypatch):
    """
    Assert a single call writes exactly one JSON line to logs/llm_io.log carrying
    the request payload, assembled response text, and duration_ms.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body(["logged ", "response"]),
        )

    _patch_transport(monkeypatch, handler)

    log_path = Path("logs/llm_io.log")
    lines_before = len(log_path.read_text(encoding="utf-8").splitlines()) if log_path.exists() else 0

    await collect_llm_response(_endpoint(), [{"role": "user", "content": "log me"}], stream=True)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == lines_before + 1

    entry = json.loads(lines[-1])
    assert entry["response_text"] == "logged response"
    assert entry["duration_ms"] >= 0
    assert entry["request"]["model"] == "test-model"
    assert entry["request"]["messages"] == [{"role": "user", "content": "log me"}]


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

SPRINT2_TEMPLATES = [
    "node_draft_prose.xml.j2",
    "node_plan_beat.xml.j2",
    "node_adversarial_critics_continuity.xml.j2",
    "node_adversarial_critics_dialogue.xml.j2",
    "node_adversarial_critics_pacing.xml.j2",
]


@pytest.mark.parametrize("template_name", SPRINT2_TEMPLATES)
def test_prompt_templates_render(template_name):
    """
    Assert each Sprint 2 template renders through load_and_render with a
    representative context and produces non-empty output.

    Purpose:
        Templates use StrictUndefined — a missing variable raises UndefinedError.
        The context is built from the template's own undeclared variables so this
        test stays valid as templates evolve.
    """
    loader = PromptLoader()
    source = loader.env.loader.get_source(loader.env, template_name)[0]
    variables = meta.find_undeclared_variables(loader.env.parse(source))
    context = {var: f"TEST_{var.upper()}" for var in variables}

    rendered = loader.load_and_render(template_name, context)
    assert rendered.strip()
    for var in variables:
        assert f"TEST_{var.upper()}" in rendered


# ---------------------------------------------------------------------------
# ChromaDB roundtrip
# ---------------------------------------------------------------------------


def test_chroma_roundtrip(tmp_path):
    """
    Assert init → add embeddings → query returns the seeded nearest neighbor with
    text, metadata, and cosine distance fields.
    """
    chroma_client.init_chroma_collections(tmp_path)
    try:
        chroma_client.add_prose_embedding(
            "The salt wind howled across the empty harbor at midnight.",
            {"scene_id": "s1", "chapter_id": "c1", "arc_id": "a1", "beat_id": "b1"},
            embedding_id="b1",
        )
        chroma_client.add_prose_embedding(
            "Accountants reviewed the quarterly ledgers in fluorescent silence.",
            {"scene_id": "s2", "chapter_id": "c2", "arc_id": "a1", "beat_id": "b2"},
            embedding_id="b2",
        )

        results = chroma_client.query_flavor_vectors(
            "wind howling over the harbor at night", n_results=2
        )
        assert len(results) == 2
        nearest = results[0]
        assert nearest["metadata"]["beat_id"] == "b1"
        assert "harbor" in nearest["text"]
        assert 0.0 <= nearest["distance"] <= 2.0  # chroma cosine distance space

        # Idempotent upsert: re-adding the same ID must not duplicate.
        chroma_client.add_prose_embedding(
            "The salt wind howled across the empty harbor at midnight.",
            {"scene_id": "s1", "chapter_id": "c1", "arc_id": "a1", "beat_id": "b1"},
            embedding_id="b1",
        )
        assert len(chroma_client.query_flavor_vectors("harbor wind", n_results=10)) == 2
    finally:
        chroma_client.reset_collections(tmp_path)


def test_chroma_empty_query_returns_empty(tmp_path):
    """Assert querying a freshly initialized (empty) collection returns [] cleanly."""
    chroma_client.init_chroma_collections(tmp_path)
    try:
        assert chroma_client.query_flavor_vectors("anything at all") == []
    finally:
        chroma_client.reset_collections(tmp_path)

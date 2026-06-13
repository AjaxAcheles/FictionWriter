"""
llm/call_llm.py

Async LLM HTTP Wrapper — Streaming-First with Retry Loop.

Purpose:
    The sole interface between the FSM and LLM inference endpoints. Accepts a
    rendered prompt payload and an EndpointConfig, sends an HTTP request to the
    configured base_url using the OpenAI API spec, and returns either a full
    response string (stream=False) or an async generator of token chunks (stream=True).

    Retry loop: implements a 3-attempt retry with backoff (5s then 15s) for transient
    HTTP exceptions: ConnectError, ReadTimeout, and other httpx transport errors.
    Errors that exhaust all retries are surfaced as hard failures to the caller (FSM
    node), which then routes to node_freeze_and_escalate via edge_mode_selector.

    Tool-call streaming: when call_llm() is given OpenAI-format tool definitions
    (tools=...), it yields structured dict events — content deltas plus one final
    assembled tool_calls event built from buffered argument fragments. When tools
    is None (every pre-existing call site), behavior is unchanged: plain str chunks.

    Grammar constraints: if EndpointConfig.grammar_constraint_strategy is "gbnf",
    the compiled GBNF string from gbnf_compiler.py is included in the request payload.
    If "json_mode", the JSON mode flag is set on the request. Both paths are followed
    by caller-side model_validate() for schema enforcement.

    Every invocation is logged to logs/llm_io.log via core/logger.get_llm_io_logger()
    and log_llm_call(). Per-token chunks are NOT logged — only the fully assembled
    response text is recorded after streaming completes.

Architecture role:
    - Called by every node that makes LLM requests: all planning nodes, node_draft_prose,
      node_revise_prose, node_adversarial_critics, node_craft_consultant,
      node_compress_memory, and node_plan_beat (PAD translation step 3).
    - stream=True is mandatory for all prose generation and critic streaming to the UI.
    - Uses httpx.AsyncClient for async HTTP — compatible with the Quart event loop.
"""

import asyncio
import json
import re
import time
from typing import AsyncIterator, Optional, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from core.config_loader import EndpointConfig
from core.logger import get_llm_io_logger, log_llm_call
from llm.gbnf_compiler import json_schema_to_gbnf

llm_io_logger = get_llm_io_logger()

RETRY_DELAYS = [5, 15]
MAX_ATTEMPTS = 3

# Transient transport errors eligible for the backoff retry loop. Includes the
# httpx base TransportError (covers ConnectError, ReadTimeout, RemoteProtocolError,
# PoolTimeout, etc.) — non-transient HTTP status errors are NOT in this set.
TRANSIENT_EXCEPTIONS = (httpx.TransportError,)

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    """
    Hard failure for the bounded schema-validation sequence.

    Raised by call_llm_structured() when the model_validate retry cap is exhausted
    AND the lenient extraction fallback fails (or its extracted object still fails
    validation). Surfaced to the FSM escalation ladder by the calling node — the
    structured call never loops indefinitely.
    """

    def __init__(self, message: str, last_response: str):
        super().__init__(message)
        self.last_response = last_response


async def call_llm(
    endpoint: EndpointConfig,
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
    stream: bool = True,
    grammar: Optional[str] = None,
    tools: Optional[list[dict]] = None,
    tool_choice: str = "auto",
) -> AsyncIterator[str | dict]:
    """
    Send a chat completion request to an OpenAI-compatible inference endpoint.

    Purpose:
        Constructs the HTTP payload from the provided messages, temperature, and
        grammar constraint. Sends the request to endpoint.base_url with Bearer
        auth via endpoint.api_key. If stream=True, yields token chunks as they
        arrive via server-sent events. If stream=False, assembles the full response
        and yields it as a single chunk (for compatibility with the same async
        generator interface).

        Retry loop: on ConnectError or ReadTimeout, waits RETRY_DELAYS[attempt]
        seconds and retries. After MAX_ATTEMPTS exhausted, raises the last exception
        for the calling node to handle (route to node_freeze_and_escalate).

        After streaming completes (or on single-response return), logs the full request
        payload and assembled response to llm_io.log via log_llm_call().

    Inputs:
        endpoint: EndpointConfig — the target inference endpoint configuration.
            Provides base_url, api_key, model_name, and grammar_constraint_strategy.
        messages: List[dict] — the OpenAI-format messages array
            (e.g., [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]).
        temperature: float — sampling temperature (default 0.7).
        max_tokens: Optional[int] — maximum tokens to generate. None = endpoint default.
        stream: bool — if True, yields tokens as they arrive. If False, yields the
            full response as one chunk. Default True.
        grammar: Optional[str] — GBNF grammar string for grammar-constrained endpoints.
            Included in the payload if endpoint.grammar_constraint_strategy is "gbnf".
            Ignored for "json_mode" endpoints.
        tools: Optional[list[dict]] — OpenAI-format tool definitions. When provided,
            "tools" and "tool_choice" are appended to the payload and the iterator
            switches to dict events (see Outputs). When None (default), payload and
            yield behavior are byte-identical to the pre-tools implementation.
        tool_choice: str — OpenAI tool_choice value ("auto", "none", "required").
            Only sent when tools is provided. Default "auto".

    Outputs:
        AsyncIterator[str | dict]:
            tools is None (backward-compatible mode): yields plain str token chunks
                (stream=True) or a single full response string (stream=False),
                exactly as before. Tool-call deltas, if any, are ignored.
            tools provided: yields {"type": "content", "text": <str>} per content
                delta, and — after the stream ends — one final
                {"type": "tool_calls", "tool_calls": [{"id": ..., "type": "function",
                "function": {"name": ..., "arguments": <complete JSON string>}}, ...]}
                event if the model emitted tool calls. Argument fragments streamed
                token-by-token are buffered per tool-call index and concatenated
                into complete JSON strings before the final event is yielded.

    Raises:
        httpx.ConnectError / httpx.ReadTimeout: After all retries exhausted.
        httpx.HTTPStatusError: For 4xx/5xx responses that are not transient.

    Note on retry semantics: the retry loop only protects request establishment
    and streams that fail BEFORE the first chunk is yielded to the caller. Once a
    chunk has been yielded, a mid-stream transport error is re-raised immediately —
    retrying would duplicate already-streamed output in the UI.
    """
    payload: dict = {
        "model": endpoint.model_name,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if grammar is not None:
        if endpoint.grammar_constraint_strategy == "gbnf":
            payload["grammar"] = grammar
        elif endpoint.grammar_constraint_strategy == "json_mode":
            payload["response_format"] = {"type": "json_object"}
    if tools is not None:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    headers = {"Authorization": f"Bearer {endpoint.api_key}"}
    url = f"{endpoint.base_url.rstrip('/')}"
    start = time.monotonic()

    last_exc: Optional[Exception] = None
    for attempt in range(MAX_ATTEMPTS):
        chunks: list[str] = []
        tool_buffers: dict[int, dict] = {}
        yielded_any = False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
                if stream:
                    async with client.stream("POST", url, json=payload, headers=headers) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            event = _parse_sse_line(line)
                            if event is None:
                                continue
                            if event["type"] == "content":
                                chunks.append(event["text"])
                                yielded_any = True
                                if tools is None:
                                    yield event["text"]
                                else:
                                    yield event
                            elif event["type"] == "tool_call_delta" and tools is not None:
                                _buffer_tool_call_deltas(tool_buffers, event["tool_calls"])
                    if tool_buffers:
                        final_event = _assemble_tool_calls(tool_buffers)
                        chunks.append(_tool_calls_repr(final_event["tool_calls"]))
                        yielded_any = True
                        yield final_event
                else:
                    response = await client.post(url, json=payload, headers=headers)
                    response.raise_for_status()
                    body = response.json()
                    message = body["choices"][0]["message"]
                    if tools is None:
                        full_text = message["content"]
                        chunks.append(full_text)
                        yielded_any = True
                        yield full_text
                    else:
                        content = message.get("content")
                        if content:
                            chunks.append(content)
                            yielded_any = True
                            yield {"type": "content", "text": content}
                        raw_tool_calls = message.get("tool_calls")
                        if raw_tool_calls:
                            chunks.append(_tool_calls_repr(raw_tool_calls))
                            yielded_any = True
                            yield {"type": "tool_calls", "tool_calls": raw_tool_calls}

            _log_call(payload, "".join(chunks), start)
            return

        except TRANSIENT_EXCEPTIONS as e:
            if yielded_any:
                # Cannot retry after output has reached the caller — re-raise.
                _log_call(payload, "".join(chunks), start, error=repr(e))
                raise
            last_exc = e
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_DELAYS[attempt])
        except httpx.HTTPStatusError as e:
            _log_call(payload, "", start, error=repr(e))
            raise

    _log_call(payload, "", start, error=repr(last_exc))
    raise last_exc  # type: ignore[misc]  # always set when loop exhausts


def _parse_sse_line(line: str) -> Optional[dict]:
    """
    Extract the delta event from one OpenAI-format SSE line.

    Returns:
        {"type": "content", "text": <str>} for content deltas,
        {"type": "tool_call_delta", "tool_calls": [<raw delta tool_calls list>]}
        for tool-call deltas, or None for blank lines, comments, [DONE] sentinels,
        and deltas that carry neither (e.g., role-only first delta).
    """
    line = line.strip()
    if not line or not line.startswith("data:"):
        return None
    data = line[len("data:"):].strip()
    if data == "[DONE]":
        return None
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    choices = parsed.get("choices") or []
    if not choices:
        return None
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    if content:
        return {"type": "content", "text": content}
    tool_calls = delta.get("tool_calls")
    if tool_calls:
        return {"type": "tool_call_delta", "tool_calls": tool_calls}
    return None


def _buffer_tool_call_deltas(buffers: dict[int, dict], deltas: list[dict]) -> None:
    """
    Accumulate one SSE chunk's raw delta tool_calls entries into per-index buffers.

    Purpose:
        OpenAI-compatible servers stream tool calls fragmented: the first delta for
        an index carries id and function.name; subsequent deltas append
        function.arguments JSON-string fragments token-by-token. Each entry is keyed
        by its "index" so parallel tool calls accumulate independently.
    """
    for entry in deltas:
        buf = buffers.setdefault(entry.get("index", 0), {"id": "", "name": "", "arguments": ""})
        if entry.get("id"):
            buf["id"] = entry["id"]
        fn = entry.get("function") or {}
        if fn.get("name"):
            buf["name"] += fn["name"]
        if fn.get("arguments"):
            buf["arguments"] += fn["arguments"]


def _assemble_tool_calls(buffers: dict[int, dict]) -> dict:
    """
    Build the final {"type": "tool_calls", ...} event from buffered fragments.

    Tool calls are emitted in index order, each in the complete OpenAI shape:
    {"id": ..., "type": "function", "function": {"name": ..., "arguments": <json str>}}.
    """
    return {
        "type": "tool_calls",
        "tool_calls": [
            {
                "id": buf["id"],
                "type": "function",
                "function": {"name": buf["name"], "arguments": buf["arguments"]},
            }
            for _, buf in sorted(buffers.items())
        ],
    }


def _tool_calls_repr(tool_calls: list[dict]) -> str:
    """Textual representation of tool calls for the llm_io.log response field."""
    return "[tool_calls] " + json.dumps(tool_calls)


def _log_call(payload: dict, response_text: str, start: float, error: Optional[str] = None) -> None:
    """Write exactly one llm_io.log line for a completed (or failed) invocation."""
    request_payload = dict(payload)
    if error is not None:
        request_payload["error"] = error
    log_llm_call(
        llm_io_logger,
        request_payload=request_payload,
        response_text=response_text,
        duration_ms=(time.monotonic() - start) * 1000.0,
    )


async def collect_llm_response(
    endpoint: EndpointConfig,
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
    stream: bool = False,
    grammar: Optional[str] = None,
) -> str:
    """
    Convenience wrapper: consume call_llm() fully and return the assembled string.

    Purpose:
        Used by call sites that need the complete response rather than a live
        stream (planning nodes, critics, the structured-validation sequence).
        Identical retry/logging semantics to call_llm().
    """
    chunks = [
        chunk
        async for chunk in call_llm(
            endpoint, messages, temperature=temperature, max_tokens=max_tokens, stream=stream, grammar=grammar
        )
    ]
    return "".join(chunks)


_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)


def _extract_first_json_object(text: str) -> Optional[str]:
    """
    Lenient extraction pass: strip markdown fences, return the first balanced
    JSON VALUE — object {...} or array [...].

    Purpose:
        The fallback for the bounded schema-validation sequence. Walks the text
        character-by-character tracking bracket depth and JSON string context
        (so brackets inside string literals don't unbalance the scan). Arrays
        are included because schema models carry bare-list tolerance shims
        (e.g. GlobalPlan wraps a bare arc array) — an extracted array can still
        validate.

    Outputs:
        The first balanced JSON object/array substring, or None if none exists.
    """
    cleaned = _FENCE_RE.sub("", text)
    open_to_close = {"{": "}", "[": "]"}
    stack: list[str] = []
    start_idx: Optional[int] = None
    in_string = False
    escaped = False
    for i, ch in enumerate(cleaned):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            if stack:
                in_string = True
        elif ch in open_to_close:
            if not stack:
                start_idx = i
            stack.append(open_to_close[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return cleaned[start_idx : i + 1]
    return None


_KEY_ALIASES = {
    "ArcPlan": {
        "chapter_stubs": ["chapters", "stubs", "chapter_list"],
        "thread_events": ["threads", "events", "thread_list"],
    },
    "ChapterPlan": {
        "scenes": ["schedule", "scene_list", "chapter_scenes"],
    },
    "BeatPlanList": {
        "beats": ["beat_list", "partitions", "scene_beats"],
    },
}


def _remap_keys(json_str: str, schema_name: str) -> Optional[str]:
    """
    Remap common LLM key-name aliases to schema canonical field names.

    Purpose:
        Zero-cost (no LLM call) tolerance shim. When the LLM ignores the prompt's
        schema instruction and outputs e.g. 'chapters' instead of 'chapter_stubs',
        this function rewrites the key to the canonical name so
        model_validate_json passes without a retry.

    Rules:
        - Only applies to dicts (bare arrays pass through to the @model_validator).
        - Skips remapping if the canonical key already exists, preventing duplicates.
        - Returns None when no remapping was needed or the JSON is unparseable,
          so the caller falls through to the existing retry/error path.

    Cost: one json.loads / json.dumps round-trip — microseconds, zero network.
    """
    aliases = _KEY_ALIASES.get(schema_name)
    if not aliases:
        return None
    try:
        obj = json.loads(json_str)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    # Build reverse map: alias -> canonical
    alias_to_canonical = {}
    for canonical, alias_list in aliases.items():
        for alias in alias_list:
            alias_to_canonical[alias] = canonical

    # If any canonical key already exists, skip remapping to avoid duplicates.
    if any(canonical in obj for canonical in aliases):
        return None

    changed = False
    new_obj = {}
    for k, v in obj.items():
        if k in alias_to_canonical:
            new_obj[alias_to_canonical[k]] = v
            changed = True
        else:
            new_obj[k] = v
    return json.dumps(new_obj) if changed else None


async def call_llm_structured(
    endpoint: EndpointConfig,
    messages: list[dict],
    schema_model: Type[T],
    retry_cap: int,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
) -> T:
    """
    Bounded schema-validation sequence for structured (critic) calls.

    Purpose:
        Fires a non-streaming LLM call constrained to schema_model's JSON schema
        (grammar strategy per endpoint: compiled GBNF attached for "gbnf",
        vendor JSON-mode flag for "json_mode"), then validates the parsed output
        with schema_model.model_validate().

        On schema-invalid output, retries the call up to retry_cap times
        (AppConfig.model_validate_retry_cap, default 3). On cap exhaustion, runs
        the lenient extraction pass (strip markdown fences, grab the first
        balanced {...} object) over the LAST response and attempts model_validate()
        once more. If extraction fails or validation still rejects, raises
        StructuredOutputError — the loop-safety guarantee: this call never spins.

    Inputs:
        endpoint: Target EndpointConfig (provides grammar_constraint_strategy).
        messages: OpenAI-format messages array.
        schema_model: Pydantic model class the output must validate against.
        retry_cap: Maximum validation retries (from AppConfig.model_validate_retry_cap).
        temperature: Sampling temperature (default 0.2 — structured calls run cold).
        max_tokens: Optional generation cap.

    Outputs:
        A validated schema_model instance.

    Raises:
        StructuredOutputError: On cap exhaustion + failed extraction fallback.
        httpx errors: Propagated from call_llm() (transient retries exhausted).
    """
    grammar = (
        json_schema_to_gbnf(schema_model)
        if endpoint.grammar_constraint_strategy == "gbnf"
        else ""  # non-None sentinel → call_llm sets the json_mode vendor flag
    )

    last_response = ""
    for _ in range(retry_cap):
        last_response = await collect_llm_response(
            endpoint, messages, temperature=temperature, max_tokens=max_tokens, stream=False, grammar=grammar
        )
        try:
            return schema_model.model_validate_json(last_response)
        except ValidationError:
            pass
        # Cheap in-attempt tolerance before burning another LLM call: models
        # routinely wrap valid JSON in markdown fences or stray prose. The
        # extraction is pure string work — loop safety is unchanged.
        extracted = _extract_first_json_object(last_response)
        if extracted is not None:
            try:
                return schema_model.model_validate_json(extracted)
            except ValidationError:
                pass
            # Free key-remapping pass before burning another LLM call.
            remapped = _remap_keys(extracted, schema_model.__name__)
            if remapped is not None:
                try:
                    return schema_model.model_validate_json(remapped)
                except ValidationError:
                    pass
            continue

    extracted = _extract_first_json_object(last_response)
    if extracted is not None:
        try:
            return schema_model.model_validate_json(extracted)
        except ValidationError:
            pass

    raise StructuredOutputError(
        f"Structured output failed {schema_model.__name__} validation after "
        f"{retry_cap} attempt(s) and the lenient extraction fallback. "
        "Surfacing to the FSM escalation ladder.",
        last_response=last_response,
    )

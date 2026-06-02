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

from typing import AsyncIterator, Optional

import httpx

from core.config_loader import EndpointConfig
from core.logger import get_llm_io_logger, log_llm_call

llm_io_logger = get_llm_io_logger()

RETRY_DELAYS = [5, 15]
MAX_ATTEMPTS = 3


async def call_llm(
    endpoint: EndpointConfig,
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
    stream: bool = True,
    grammar: Optional[str] = None,
) -> AsyncIterator[str]:
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

    Outputs:
        AsyncIterator[str]: Yields token chunks (stream=True) or a single full
            response string (stream=False). Callers use `async for chunk in call_llm(...)`.

    Raises:
        httpx.ConnectError / httpx.ReadTimeout: After all retries exhausted.
        httpx.HTTPStatusError: For 4xx/5xx responses that are not transient.
    """
    pass

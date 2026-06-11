"""
fsm/nodes/node_draft_prose.py

Primary Generation Engine — streams beat prose from the drafter endpoint.

Purpose:
    Renders the drafter prompt from the active context package, fires
    call_llm(stream=True) against config.endpoints.drafter, relays each token
    chunk to the Quart SSE bus, and passes the completed text through the
    antislop black-box interface (detect_slop/resolve_slop — no-op stubs until
    Sprint 6; the interface contract is final).

Architecture role:
    - Triggered by node_assemble_context. Yields to node_programmatic_audit.
    - Streams chunks via core/stream_bus.publish (consumed by routes/dashboard).
    - Overwrites current_draft_text and streaming_buffer in OrchestratorState.
"""

import time

from core import stream_bus
from core.antislop import detect_slop, resolve_slop
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from llm import call_llm as call_llm_module
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_draft_prose")


async def node_draft_prose(state: OrchestratorState) -> dict:
    """
    Draft the current beat's prose, streaming chunks to the UI.

    Outputs (merged into OrchestratorState):
        current_draft_text: the antislop-resolved completed prose.
        streaming_buffer: the raw assembled stream text.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    config = load_config()
    package = state["active_context_package"]

    try:
        prompt = PromptLoader().load_and_render("node_draft_prose", package)
        messages = [{"role": "user", "content": prompt}]

        chunks: list[str] = []
        async for chunk in call_llm_module.call_llm(
            config.endpoints.drafter,
            messages,
            temperature=0.7,
            max_tokens=None,
            stream=True,
        ):
            chunks.append(chunk)
            stream_bus.publish({"type": "draft_chunk", "text": chunk})

        raw_text = "".join(chunks)
        flags = detect_slop(raw_text)
        resolved = resolve_slop(raw_text, flags)
        stream_bus.publish({"type": "draft_complete", "word_count": len(resolved.split())})

        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return {"current_draft_text": resolved, "streaming_buffer": raw_text}
    except Exception as e:
        log_node_event(
            logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e)
        )
        raise

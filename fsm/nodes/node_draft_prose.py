"""
fsm/nodes/node_draft_prose.py

Primary Prose Generation Node — Streaming LLM Drafter with Antislop Interface.

Purpose:
    The main generation engine. Formats the assembled context package into the
    prose generation prompt, fires call_llm(stream=True) with the drafter endpoint,
    and streams the output token-by-token to the Quart SSE endpoint for live UI
    display. Accumulates the full response in streaming_buffer.

    After generation completes, passes the full assembled text through the
    antislop black-box interface:
    - detect_slop(text) → List[SlopFlag]: identifies cliché/slop spans.
    - resolve_slop(text, flags) → str: corrects flagged spans.

    Both antislop functions are STUBS for Sprints 1–5 (detect_slop returns [],
    resolve_slop returns text unchanged). Sprint 6 will replace the stubs without
    any changes to this node — the interface is implementation-agnostic.

    The beat_start SSE event is emitted before generation begins, carrying the
    beat_id so the frontend can wrap incoming tokens in a data-beat-id div and
    clear stale tokens if the same beat_id is re-used after a revision.

Architecture role:
    - Only node that emits SSE prose chunks to the frontend. All other streaming
      (critic reasoning, planning bullets) is handled by their respective nodes.
    - Uses: call_llm() with config.endpoints.drafter endpoint, stream=True.
    - Loads prompt from: prompts/node_draft_prose.xml.j2 via PromptLoader.
    - Emits a structured JSON log entry via get_logger("node_draft_prose").
"""

import time

from core.antislop import detect_slop, resolve_slop
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_draft_prose")


async def node_draft_prose(state: OrchestratorState) -> dict:
    """
    Stream prose generation and pass output through the antislop interface.

    Purpose:
        Loads the prose generation prompt template, renders it with context from
        active_context_package, and sends the request to the drafter endpoint
        with stream=True. Yields each token chunk to the Quart SSE queue as it
        arrives. After the stream completes, calls detect_slop() then resolve_slop()
        on the fully assembled text. Overwrites current_draft_text with the
        antislop-resolved output.

    Inputs (from OrchestratorState):
        state['active_context_package']: Dict — fully assembled context payload
            from node_assemble_context, including beat constraints, character states,
            RAPTOR summaries, and HNSW flavor context.

    Outputs (dict merged into OrchestratorState):
        current_draft_text: str — the antislop-resolved final prose text.
        streaming_buffer: str — cleared after antislop resolution (full text
            is now in current_draft_text).

    Relationships:
        - Triggered by: node_assemble_context (direct edge).
        - Yields to: node_programmatic_audit (direct edge in graph.py).
        - Uses: call_llm() with config.endpoints.drafter, stream=True.
        - Calls: detect_slop(), resolve_slop() from core/antislop.py.
        - Prompt: prompts/node_draft_prose.xml.j2
    """
    pass

"""
fsm/nodes/node_compress_memory.py

Memory Consolidation Node — chapter-boundary RAPTOR summarization.

Purpose:
    Synchronous, blocking LangGraph node fired by node_commit_transaction at
    every chapter boundary. Runs cluster_scenes() (Sprint 1-5 placeholder:
    identity clustering; Leiden drop-in arrives in Sprint 6 with no caller
    changes) over the completed chapter's committed scene texts, generates a
    flat chapter-level summary, and writes it to the RaptorNodes SQLite table.

    Sprint 3 implementation note: the summary is a deterministic flat
    extraction (leading sentences of each scene cluster, length-capped). LLM
    summarization replaces the extraction step in Sprint 4 — the RAPTOR write
    path and caller contract do not change.

    Staleness is by design: mid-chapter beats read the PRIOR chapter's summary.

Architecture role:
    - Called synchronously by node_commit_transaction (never via graph edge in
      the vertical slice). Yields control back to the commit path.
"""

import time

from core import runtime
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from llm import call_llm as call_llm_module
from fsm.state import OrchestratorState
from memory import sqlite_db
from memory.raptor import cluster_scenes, write_raptor_node_full

logger = get_logger("node_compress_memory")

_SUMMARY_CHAR_CAP = 1200


async def _summarize_clusters(clusters: list[str]) -> str:
    """
    Sprint 4: LLM flat summarization on the planner endpoint, retried once,
    falling back to the deterministic extraction. Consolidation must never
    block the commit path on a flaky endpoint.
    """
    config = load_config()
    joined = "\n\n---\n\n".join(clusters)[:12_000]
    messages = [
        {
            "role": "user",
            "content": (
                "Summarize the following chapter's scenes into one dense, factual "
                "chapter summary (6-10 sentences). Preserve names, causal links, and "
                "any irreversible state changes. Plain prose only.\n\n" + joined
            ),
        }
    ]
    for attempt in range(2):
        try:
            text = await call_llm_module.collect_llm_response(
                config.endpoints.planner, messages, temperature=0.2, stream=False
            )
            if text and text.strip():
                return text.strip()[:_SUMMARY_CHAR_CAP * 2]
        except Exception as e:  # noqa: BLE001 — fallback path below
            logger.warning("chapter summarization attempt %d failed: %r", attempt + 1, e)
    logger.warning("chapter summarization fell back to deterministic extraction.")
    return flat_summarize(clusters)


def flat_summarize(cluster_texts: list[str]) -> str:
    """Deterministic flat summary: first two sentences of each cluster, capped."""
    pieces = []
    for text in cluster_texts:
        sentences = [s.strip() for s in text.replace("\n", " ").split(".") if s.strip()]
        pieces.append(". ".join(sentences[:2]) + ("." if sentences else ""))
    return " ".join(pieces)[:_SUMMARY_CHAR_CAP]


async def node_compress_memory(state: OrchestratorState) -> dict:
    """
    Consolidate the completed chapter into a RAPTOR chapter summary node.

    Outputs:
        {} — no OrchestratorState changes; side effect is the RaptorNodes write.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    db = runtime.SQLITE_PATH

    try:
        scene_texts = sqlite_db.get_scene_texts_for_chapter(db, pointer.chapter_id)
        clusters = cluster_scenes(scene_texts)
        summary = await _summarize_clusters(clusters)
        write_raptor_node_full(
            db,
            node_id=f"raptor_chapter_{pointer.chapter_id}",
            parent_id=f"raptor_arc_{pointer.arc_id}",
            level="chapter",
            summary=summary,
            scene_ids=[],
        )
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return {}
    except Exception as e:
        log_node_event(
            logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e)
        )
        raise

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
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from memory import sqlite_db
from memory.raptor import cluster_scenes, write_raptor_node_full

logger = get_logger("node_compress_memory")

_SUMMARY_CHAR_CAP = 1200


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
        summary = flat_summarize(clusters)
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

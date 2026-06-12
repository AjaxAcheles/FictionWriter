"""
fsm/nodes/node_assemble_context.py

Retrieval Orchestrator — builds the active context package for drafting.

Purpose:
    Model-free programmatic node. Executes live queries against SQLite
    (relational), Graphiti (temporal subgraph), RAPTOR (hierarchical summaries),
    and ChromaDB (HNSW flavor vectors), then applies drop-priority pruning so
    the assembled payload fits the drafter endpoint's input token budget.

    Three-tier coreference confidence (Graphiti provisional links):
    - confidence >= HIGH_CONFIDENCE (0.85): injected as absolute fact.
    - confidence >= config.thresholds.coreference_confidence_floor: injected as
      an Epistemic Belief, explicitly flagged as an unconfirmed assumption.
    - below the floor: excluded entirely.

    Drop-priority pruning order (lowest-value context dropped first):
    1. HNSW flavor exemplars (hnsw_flavor)
    2. RAPTOR arc-level summary (raptor_arc_summary)
    3. RAPTOR chapter-level summary (raptor_chapter_summary)
    4. RAPTOR scene-level summary (raptor_scene_summary)
    The scene summary is the absolute last thing dropped — it is the drafter's
    immediate continuity anchor; distant (arc/chapter) context goes first.
    After each drop the token budget is re-checked via fits_in_budget using the
    drafter endpoint's tokenizer_family routing.

Architecture role:
    - Triggered by node_plan_beat (or node_human_intervention after rollback).
    - Yields to node_draft_prose; package keys match node_draft_prose.xml.j2.
    - RAPTOR and Graphiti reads are live from Sprint 3 onward — the Graphiti
      stub driver may return an empty subgraph, but the query is always issued.
"""

import json
import time

from core import runtime
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from llm.tokenizer import fits_in_budget
from memory import sqlite_db
from memory.chroma_client import query_flavor_vectors
from memory.graphiti_client import query_point_in_time_subgraph
from memory.raptor import get_raptor_summaries
from memory.style_store import get_author_style

logger = get_logger("node_assemble_context")

HIGH_CONFIDENCE = 0.85


def partition_coreference_links(
    edges: list[dict], confidence_floor: float
) -> tuple[list[dict], list[dict]]:
    """
    Split Graphiti edges into (facts, epistemic_beliefs) via the three-tier system.

    Inputs:
        edges: edge dicts; each may carry a 'confidence' float (absent = 1.0,
            i.e. a confirmed, non-provisional edge).
        confidence_floor: config.thresholds.coreference_confidence_floor.

    Outputs:
        (facts, beliefs). Low-confidence edges are excluded entirely.
    """
    facts, beliefs = [], []
    for edge in edges:
        confidence = float(edge.get("confidence", 1.0))
        if confidence >= HIGH_CONFIDENCE:
            facts.append(edge)
        elif confidence >= confidence_floor:
            beliefs.append(edge)
    return facts, beliefs


def _edges_to_text(edges: list[dict]) -> str:
    """Render edge dicts as one-fact-per-line prompt text."""
    lines = []
    for e in edges:
        a = e.get("entity_a_id", "?")
        b = e.get("entity_b_id", "?")
        rel = e.get("edge_type", "related_to")
        suffix = f" ({e['attributes']})" if e.get("attributes") else ""
        lines.append(f"- {a} {rel} {b}{suffix}")
    return "\n".join(lines)


def _package_text(package: dict) -> str:
    """Flatten the package for token counting."""
    return "\n".join(str(v) for v in package.values())


def _trailing_prose(db, scene_id: str, word_limit: int = 300) -> str:
    """
    The last ~word_limit words of committed prose immediately preceding this
    beat — the drafter's seamless-transition anchor (voice + physical blocking).

    Prefers the active scene's own accumulated prose (previous beats in the
    same scene); falls back to the closing prose of the last committed scene
    in the same chapter when this is the scene's first beat.
    """
    from contextlib import closing

    scene = sqlite_db.get_row(db, "Scenes", "scene_id", scene_id)
    if scene is None:
        return ""
    prose = scene.get("prose_text") or ""
    if not prose:
        with closing(sqlite_db.get_connection(db)) as conn:
            rows = conn.execute(
                "SELECT prose_text FROM Scenes WHERE chapter_id = ? "
                "AND committed_at IS NOT NULL AND scene_index < ? "
                "ORDER BY scene_index DESC LIMIT 1",
                (scene["chapter_id"], scene["scene_index"]),
            ).fetchall()
        prose = (rows[0]["prose_text"] if rows else "") or ""
    words = prose.split()
    return " ".join(words[-word_limit:])


async def node_assemble_context(state: OrchestratorState) -> dict:
    """
    Build and prune the active_context_package for the current beat.

    Outputs (merged into OrchestratorState):
        active_context_package: dict keyed to the node_draft_prose template
        variables, completely overwriting any previous package.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    config = load_config()
    db = runtime.SQLITE_PATH

    try:
        beat = sqlite_db.get_beat_by_index(db, pointer.scene_id, pointer.beat_index)
        if beat is None:
            raise RuntimeError(
                f"node_assemble_context: no Beat at ({pointer.scene_id!r}, {pointer.beat_index})"
            )
        plan = json.loads(beat.get("beat_plan_json") or "{}")

        # --- live store queries ------------------------------------------------
        threads = sqlite_db.get_open_threads(db)
        characters = sqlite_db.get_characters(db)
        char_states = []
        for char in characters:
            history = sqlite_db.get_recent_character_emotions(db, char["char_id"], limit=1)
            pad = (
                {k: history[0][k] for k in ("pleasure", "arousal", "dominance")}
                if history
                else {"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0}
            )
            char_states.append(
                f"- {char['name']} ({char.get('role') or 'character'}): "
                f"{char.get('description') or ''} PAD={json.dumps(pad)}"
            )

        edges = await query_point_in_time_subgraph(
            entity_ids=[c["char_id"] for c in characters],
            active_event_id=f"{pointer.scene_id}_beat_{pointer.beat_index}",
        )
        facts, beliefs = partition_coreference_links(
            list(edges or []), config.thresholds.coreference_confidence_floor
        )

        summaries = get_raptor_summaries(
            db, pointer.scene_id, levels=["arc", "chapter", "scene"]
        )
        try:
            flavor = query_flavor_vectors(
                plan.get("description") or "", n_results=3,
                exclude_chapter_id=pointer.chapter_id,
            )
        except RuntimeError:
            flavor = []  # Chroma not initialized (unit-test contexts) — flavor is optional
        author_style = get_author_style(runtime.STYLES_DIR)

        package = {
            "beat_description": plan.get("description") or "",
            "beat_entry_constraints": plan.get("entry_constraints") or "",
            "beat_exit_constraints": plan.get("exit_constraints") or "",
            "beat_word_budget": plan.get("word_budget") or 0,
            "pad_behavioral_constraints": plan.get("pad_constraint") or "",
            "character_states": "\n".join(char_states),
            "thread_statuses": "\n".join(
                f"- {t['name']} (priority {t['priority']}): {t.get('description') or ''}"
                for t in threads
            ),
            "graphiti_facts": _edges_to_text(facts),
            "epistemic_beliefs": _edges_to_text(beliefs),
            "raptor_arc_summary": summaries.get("arc", ""),
            "raptor_chapter_summary": summaries.get("chapter", ""),
            "raptor_scene_summary": summaries.get("scene", ""),
            "hnsw_flavor": "\n---\n".join(f.get("text", "") for f in flavor),
            "author_style_baseline": json.dumps(author_style.get("frozen_baseline") or {}),
            # Immediate continuity anchor — never in the drop-priority list.
            "trailing_prose": _trailing_prose(db, pointer.scene_id),
        }

        # --- drop-priority pruning ---------------------------------------------
        endpoint = config.endpoints.drafter
        for drop_key in (
            "hnsw_flavor",
            "raptor_arc_summary",
            "raptor_chapter_summary",
            "raptor_scene_summary",
        ):
            if fits_in_budget(
                _package_text(package),
                endpoint.context_window,
                endpoint.reserved_output_tokens,
                endpoint.tokenizer_family,
                endpoint.model_name,
            ):
                break
            package[drop_key] = ""
            logger.info("drop-priority pruning: dropped %s", drop_key)

        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "ok")
        return {"active_context_package": package}
    except Exception as e:
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "error", error=repr(e))
        raise

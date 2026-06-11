"""
fsm/nodes/node_commit_transaction.py

Database Writer — intent-record commit sequence for validated beats.

Purpose:
    Commits a clean beat to all stores in strict order:
    1. CommitIntent row, status='pending' (crash detection sentinel).
    2. SQLite writes — idempotent upserts keyed by beat_id: beat status,
       scene prose append + word count, PAD state rows.
    3. Graphiti temporal edges — deterministic-UUID upserts (idempotent).
    4. .jsonl event log append (beat_commit record).
    5. CommitIntent row flipped to status='committed'.
    6. State resets: retry_count=0, replan_count=0, escalation_tier=0,
       has_paradox=False, transient_dc_override=None, failed_beat_cache cleared,
       best_seen_draft=None, current_draft_text="".
    7. fsm_pointer advanced to the next beat index.

    Scene-advancement guard: after the beat commits, the scene is closed
    (committed_at stamped) only when committed word count >= scene word_budget
    AND committed beats >= config.thresholds.beats_per_scene_min. This prevents
    premature scene closure when generation runs short.

    Chapter boundary (all scenes in the chapter closed): Epistemic Belief
    promotion pass (200-token window scan of committed text — a structural
    no-op while the Graphiti driver returns no provisional links), snapshot ZIP
    of data/fictionwriter.db (sqlite3.Connection.backup()) + data/graphiti.db
    (file copy; Redis SAVE is issued when the FalkorDB Lite driver is live),
    then a synchronous node_compress_memory call before yielding to the router.

Architecture role:
    - Triggered by edge_mode_selector (clean pass) or edge_programmatic_router
      (fast path). Yields to edge_commit_router.
"""

import json
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from core import runtime, stream_bus
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState
from memory import sqlite_db
from memory.chroma_client import add_prose_embedding
from memory.event_log import write_event
from memory.graphiti_client import upsert_temporal_edge
from fsm.nodes.node_compress_memory import node_compress_memory

logger = get_logger("node_commit_transaction")


def snapshot_databases(snapshot_dir: Path, sqlite_path: Path, graphiti_path: Path, label: str) -> Path:
    """
    Chapter-boundary snapshot ZIP — the O(1) branch restore payload.

    data/fictionwriter.db is snapshotted via sqlite3.Connection.backup() (never
    a raw copy — WAL-safe). data/graphiti.db is copied directly; when the
    FalkorDB Lite driver is live a synchronous Redis SAVE is issued first.
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    zip_path = snapshot_dir / f"snapshot_{label}_{stamp}.zip"

    sqlite_copy = snapshot_dir / f"_tmp_{label}.db"
    src = sqlite3.connect(sqlite_path)
    try:
        dst = sqlite3.connect(sqlite_copy)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()

    try:
        from memory import graphiti_client
        client = getattr(graphiti_client, "_graphiti_client", None)
        if client is not None:
            redis_conn = getattr(getattr(client, "graph_driver", None), "redis", None)
            if redis_conn is not None:
                redis_conn.save()  # synchronous flush before file copy
    except Exception as e:  # pragma: no cover — driver-internal best effort
        logger.warning("graphiti SAVE before snapshot failed: %r", e)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(sqlite_copy, "fictionwriter.db")
        if graphiti_path.exists() and graphiti_path.is_file():
            zf.write(graphiti_path, "graphiti.db")
    sqlite_copy.unlink(missing_ok=True)
    return zip_path


async def node_commit_transaction(state: OrchestratorState) -> dict:
    """
    Execute the intent-record commit sequence for the validated beat.

    Outputs (merged into OrchestratorState):
        fsm_pointer (advanced), current_draft_text="", streaming_buffer="",
        retry_count=0, replan_count=0, escalation_tier=0, has_paradox=False,
        transient_dc_override=None, critic_failures=None (clear),
        failed_beat_cache=None (clear), best_seen_draft=None,
        best_seen_failure_count=None.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    config = load_config()
    db = runtime.SQLITE_PATH
    draft = state["current_draft_text"]
    # Use the Beat row's actual beat_id when present — Tier 3 subdivisions create
    # rows whose IDs are not derivable from (scene_id, beat_index) alone.
    beat = sqlite_db.get_beat_by_index(db, pointer.scene_id, pointer.beat_index)
    beat_id = (beat or {}).get("beat_id") or f"{pointer.scene_id}_beat_{pointer.beat_index}"

    try:
        # 1. intent record (pending)
        intent_id = sqlite_db.create_commit_intent(db, beat_id)

        # 2. SQLite idempotent writes
        plan = json.loads((beat or {}).get("beat_plan_json") or "{}")
        word_count = len(draft.split())
        already_committed = beat is not None and beat.get("status") == "committed"
        sqlite_db.upsert_beat(
            db,
            {
                "beat_id": beat_id,
                "scene_id": pointer.scene_id,
                "beat_index": pointer.beat_index,
                "beat_plan_json": json.dumps({**plan, "prose": draft, "word_count": word_count}),
                "status": "committed",
            },
        )
        if not already_committed:
            sqlite_db.append_scene_prose(db, pointer.scene_id, draft, word_count)
        for char_id, target in (plan.get("raw_pad_targets") or {}).items():
            try:
                sqlite_db.upsert_character_emotion(
                    db,
                    char_id,
                    {
                        "beat_id": beat_id,
                        "pleasure": max(-1.0, min(1.0, float(target.get("pleasure", 0.0)))),
                        "arousal": max(-1.0, min(1.0, float(target.get("arousal", 0.0)))),
                        "dominance": max(-1.0, min(1.0, float(target.get("dominance", 0.0)))),
                    },
                )
            except sqlite3.IntegrityError as e:
                logger.warning("PAD write skipped for %s: %r", char_id, e)

        # 3. Graphiti temporal edge (deterministic UUID upsert; no-op chain in slice)
        await upsert_temporal_edge(
            entity_a_id=pointer.scene_id,
            entity_b_id=beat_id,
            edge_type="CONTAINS_BEAT",
            valid_from_event_id=beat_id,
            valid_until_event_id=None,
            attributes={"word_count": word_count},
        )

        # 3b. flavor vector for future HNSW retrieval (best-effort)
        try:
            add_prose_embedding(
                text=draft,
                metadata={
                    "scene_id": pointer.scene_id,
                    "chapter_id": pointer.chapter_id,
                    "arc_id": pointer.arc_id,
                },
                embedding_id=beat_id,
            )
        except Exception:
            pass  # Chroma uninitialized in unit-test contexts; flavor is optional

        # 4. event log
        write_event(
            runtime.EVENT_LOG_PATH,
            {
                "type": "beat_commit",
                "beat_id": beat_id,
                "arc_id": pointer.arc_id,
                "chapter_id": pointer.chapter_id,
                "scene_id": pointer.scene_id,
                "beat_index": pointer.beat_index,
                "word_count": word_count,
                "committed_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # 5. intent → committed
        sqlite_db.mark_commit_intent_committed(db, intent_id)
        stream_bus.publish({"type": "beat_committed", "beat_id": beat_id, "word_count": word_count})
        stream_bus.publish({"type": "word_count", "total": sqlite_db.get_total_word_count(db) + word_count})
        for char_id, target in (plan.get("raw_pad_targets") or {}).items():
            stream_bus.publish({"type": "pad_update", "char_id": char_id, "pad": target})
        stream_bus.publish({"type": "drift", "stel_dc": state.get("stylometric_distance", 0.0)})

        # Scene-advancement guard
        scene = sqlite_db.get_row(db, "Scenes", "scene_id", pointer.scene_id) or {}
        committed_beats = sqlite_db.get_committed_beat_count(db, pointer.scene_id)
        scene_done = (
            int(scene.get("word_count") or 0) >= int(scene.get("word_budget") or 0)
            and committed_beats >= config.thresholds.beats_per_scene_min
        )
        if scene_done:
            sqlite_db.close_scene(db, pointer.scene_id)
            # Chapter boundary?
            if not sqlite_db.get_remaining_scenes(db, pointer.chapter_id):
                sqlite_db.set_chapter_status(db, pointer.chapter_id, "completed")
                _promote_epistemic_beliefs(db, pointer.chapter_id)
                snapshot_databases(
                    runtime.SNAPSHOTS_DIR, db, runtime.GRAPHITI_PATH, pointer.chapter_id
                )
                await node_compress_memory(state)  # synchronous blocking consolidation

        # 6-7. state resets + pointer advance
        updated = pointer.model_copy(update={"beat_index": pointer.beat_index + 1})
        log_node_event(logger, updated.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return {
            "fsm_pointer": updated,
            "current_draft_text": "",
            "streaming_buffer": "",
            "retry_count": 0,
            "replan_count": 0,
            "escalation_tier": 0,
            "has_paradox": False,
            "transient_dc_override": None,
            "critic_failures": None,      # explicit clear
            "failed_beat_cache": None,    # explicit clear
            "best_seen_draft": None,
            "best_seen_failure_count": None,
        }
    except Exception as e:
        log_node_event(
            logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e)
        )
        raise


def _promote_epistemic_beliefs(db: Path, chapter_id: str) -> None:
    """
    Chapter-boundary Epistemic Belief promotion (200-token window scan).

    Structural placement for the Sprint 5 ingestion pipeline: while the Graphiti
    stub driver exposes no provisional links, the pass scans zero links — the
    hook point, window math, and call site are final.
    """
    texts = sqlite_db.get_scene_texts_for_chapter(db, chapter_id)
    _ = " ".join(texts).split()[:200]  # 200-token window basis (no links to promote yet)
    logger.info("epistemic belief promotion pass completed for chapter %s (0 provisional links)", chapter_id)

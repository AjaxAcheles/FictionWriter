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
    promotion pass (200-token window scan of committed text against the pending
    provisional coreference claims — reinforced claims are confirmed in the
    provisional store and stamped into Graphiti as permanent facts), snapshot ZIP
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


async def snapshot_databases(snapshot_dir: Path, sqlite_path: Path, graphiti_path: Path, label: str) -> Path:
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
        # Flush the FalkorDB server's in-memory state to its AOF/RDB before the
        # snapshot is stamped. With the dockerized server (docker-compose.yml)
        # durability lives in the falkordb_data volume, so this is a best-effort
        # consistency point, not a file copied into the ZIP.
        from memory import graphiti_client
        client = getattr(graphiti_client, "_graphiti_client", None)
        if client is not None:
            driver = getattr(client, "driver", None) or getattr(client, "graph_driver", None)
            falkor = getattr(driver, "client", None)
            redis_conn = getattr(falkor, "connection", None) or getattr(falkor, "redis", None)
            if redis_conn is not None:
                result = redis_conn.bgsave() if hasattr(redis_conn, "bgsave") else redis_conn.save()
                if hasattr(result, "__await__"):
                    await result
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
                # Crash-recovery contract (memory/event_log.py): the prose delta
                # must ride in the beat_commit record so intra-chapter replay can
                # restore Beat plans and Scene prose after a snapshot restore.
                "prose_delta": draft,
                "committed_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # 5. intent → committed
        sqlite_db.mark_commit_intent_committed(db, intent_id)
        stream_bus.publish({"type": "beat_committed", "beat_id": beat_id, "word_count": word_count})
        # append_scene_prose already bumped this beat's words — no manual add.
        stream_bus.publish({"type": "word_count", "total": sqlite_db.get_total_word_count(db)})
        for char_id, target in (plan.get("raw_pad_targets") or {}).items():
            stream_bus.publish({"type": "pad_update", "char_id": char_id, "pad": target})
        # drift telemetry is published by node_programmatic_audit, which computes
        # both stel_dc and burrows_delta — publishing a stel-only event here
        # zeroed the advisory line on the UI drift graph.

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
                await _promote_epistemic_beliefs(db, pointer.chapter_id)
                await snapshot_databases(
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


PROMOTION_WINDOW_TOKENS = 200


def _normalize_tokens(text: str) -> list[str]:
    """Lowercased, punctuation-stripped tokens for window co-occurrence scans."""
    return [t.strip(".,!?;:\"'()[]—–-") for t in text.lower().split()]


def _cooccur_within_window(
    tokens: list[str], name_tokens: list[str], pronoun: str, window: int
) -> bool:
    """True when the entity name and the pronoun appear within `window` tokens."""
    if not name_tokens:
        return False
    span = len(name_tokens)
    name_positions = [
        i for i in range(len(tokens) - span + 1) if tokens[i : i + span] == name_tokens
    ]
    if not name_positions:
        return False
    pronoun_positions = [i for i, t in enumerate(tokens) if t == pronoun]
    return any(
        abs(p - n) <= window for p in pronoun_positions for n in name_positions
    )


async def _promote_epistemic_beliefs(db: Path, chapter_id: str) -> None:
    """
    Chapter-boundary Epistemic Belief promotion (200-token window heuristic).

    For each pending provisional coreference claim, the chapter's committed text
    is scanned: when the linked entity's name and the claimed pronoun co-occur
    within a 200-token window, the claim is reinforced — confirmed in the
    provisional store and stamped into Graphiti as a permanent high-confidence
    REFERS_TO fact (deterministic-UUID upsert, no-op while the graph backend is
    degraded). Claims never reinforced stay pending for the non-blocking
    Alignment review UI; contradiction requires conflicting-antecedent evidence
    this heuristic does not infer, so nothing is auto-dropped.
    """
    from memory import provisional_store
    from memory.graphiti_client import upsert_temporal_edge

    pending = provisional_store.list_pending()
    texts = sqlite_db.get_scene_texts_for_chapter(db, chapter_id)
    tokens = _normalize_tokens(" ".join(texts))
    if not pending or not tokens:
        logger.info(
            "epistemic belief promotion pass completed for chapter %s "
            "(%d provisional links, 0 promoted)", chapter_id, len(pending),
        )
        return

    char_names = {c["char_id"]: c["name"] for c in sqlite_db.get_characters(db)}
    promoted = 0
    for claim in pending:
        entity_id = claim.get("linked_entity_id") or ""
        pronoun = (claim.get("pronoun_text") or "").lower().strip()
        # Entity ID falls back to its trailing slug when no Character row exists.
        name = char_names.get(entity_id) or entity_id.rsplit("_", 1)[-1]
        name_tokens = _normalize_tokens(name)
        if not pronoun or not name_tokens:
            continue
        if not _cooccur_within_window(tokens, name_tokens, pronoun, PROMOTION_WINDOW_TOKENS):
            continue
        if provisional_store.confirm(claim["claim_id"]):
            promoted += 1
            await upsert_temporal_edge(
                entity_a_id=f"pronoun_{pronoun}",
                entity_b_id=entity_id,
                edge_type="REFERS_TO",
                valid_from_event_id=chapter_id,
                valid_until_event_id=None,
                attributes={"confidence": 1.0, "claim_id": claim["claim_id"],
                            "promoted_by": "epistemic_promotion"},
            )
    logger.info(
        "epistemic belief promotion pass completed for chapter %s (%d provisional links, %d promoted)",
        chapter_id, len(pending), promoted,
    )

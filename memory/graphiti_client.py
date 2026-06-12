"""
memory/graphiti_client.py

Temporal Knowledge Graph Client — Graphiti + FalkorDB Lite.

Purpose:
    Manages all interactions with the Graphiti temporal knowledge graph, backed by
    FalkorDB Lite (falkordblite + graphiti-core). No Docker, no external server —
    FalkorDB Lite runs as a managed subprocess communicating over Unix domain sockets,
    storing graph data to data/graphiti.db. The client API is identical to the full
    FalkorDB server client; switching to production deployment is a one-line change
    (FalkorLiteDriver → FalkorDriver).

    The graph stores entities as nodes and facts as edges with exact temporal validity
    windows (valid_from_event_id, valid_until_event_id). This enables chronologically
    bounded subgraph traversal: edges outside the active event's temporal window are
    filtered out, providing a flawless point-in-time snapshot of world state for
    the Continuity critic.

    Idempotent edge writes: all Graphiti edge writes use deterministic UUIDs:
    uuid5(NAMESPACE_OID, f"{entity_a_id}:{entity_b_id}:{edge_type}:{valid_from_event_id}")
    This makes crash recovery replay safe — replaying a .jsonl record recomputes
    the identical UUID and upserts rather than duplicates.

    Graph schema (custom, written via raw Cypher through the FalkorDriver):
    - Nodes: (:Entity {entity_id}) — MERGE-keyed by entity_id.
    - Edges: [:FACT {edge_id, edge_type, valid_from_event_id,
      valid_until_event_id, confidence, attributes_json, valid_from_ts,
      valid_until_ts}] — MERGE-keyed by the deterministic edge_id.
    Event IDs are beat IDs (no inherent ordering), so point-in-time filtering
    uses the write timestamps (valid_from_ts / valid_until_ts), stamped once at
    first write (coalesce keeps them stable across idempotent replays).

    _apply_event() is the sync replay funnel: it accepts either a raw edge
    payload (from upsert_temporal_edge) or a beat_commit event record (from
    branch_manager crash-recovery replay), normalizes it to edge dicts, and
    runs the async graph write in whichever loop context is available.

    Branch restore: FalkorDB Lite stores data in data/graphiti.db on disk. Snapshotting
    is a uniform file-copy operation identical to SQLite. On branch restore, the target
    snapshot ZIP is decompressed and data/graphiti.db is replaced directly — all original
    edge timestamps are preserved exactly.

Architecture role:
    - Initialized by core/runtime.py via init_graphiti_client() on startup and reset.
    - Queried by node_assemble_context for temporal entity facts and coreference links.
    - Written by node_commit_transaction (idempotent upsert keyed by deterministic UUID).
    - Snapshotted at chapter boundaries via a Redis BGSAVE/SAVE against the
      FalkorDB server (docker-compose.yml); legacy file-copy guards remain for
      pre-server data directories.
    - Restored by memory/branch_manager.py for branch operations and crash recovery.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid5, NAMESPACE_OID

_graphiti_client = None

# ISO strings compare lexicographically — this sentinel means "now" (no cutoff).
_FAR_FUTURE_TS = "9999-12-31T23:59:59+00:00"


async def init_graphiti_client(config=None):
    """
    Initialize the Graphiti client against the local FalkorDB server.

    Purpose:
        Connects graphiti-core's FalkorDriver to the FalkorDB instance started by
        docker-compose.yml (service: falkordb, default localhost:6379) and calls
        build_indices_and_constraints() to set up the graph schema. Called by
        core/runtime.py on startup and after reset.

        Graceful degradation: if the FalkorDB server is unreachable (container
        not running, e.g. unit tests or a dev box without Docker), a WARNING is
        logged and the module continues with no client — upsert_temporal_edge()
        falls through to the no-op _apply_event() chain and
        query_point_in_time_subgraph() returns []. The FSM never blocks on the
        graph backend being up.

    Inputs:
        config: AppConfig | None — connection parameters are read from
            config.graphiti (host, port, username, password, database).
            None falls back to localhost:6379 / "fictionwriter".

    Outputs:
        The initialized Graphiti client instance (module-level singleton),
        or None when the server is unreachable.
    """
    global _graphiti_client

    graphiti_cfg = getattr(config, "graphiti", None)
    host = getattr(graphiti_cfg, "host", "localhost")
    port = getattr(graphiti_cfg, "port", 6379)
    username = getattr(graphiti_cfg, "username", None)
    password = getattr(graphiti_cfg, "password", None)
    database = getattr(graphiti_cfg, "database", "fictionwriter")

    try:
        # Reachability + protocol probe BEFORE constructing the driver: the
        # redis client blocks the event loop during connect retries, so failures
        # must be caught here with a hard 2s bound. A RESP PING is sent and
        # "+PONG" is required — a port that merely accepts TCP (or a non-Redis
        # service) degrades cleanly instead of hanging the boot path.
        import socket

        with socket.create_connection((host, port), timeout=2.0) as probe:
            probe.settimeout(2.0)
            probe.sendall(b"*1\r\n$4\r\nPING\r\n")
            response = probe.recv(64)
        if not response.startswith(b"+PONG"):
            raise OSError(f"unexpected PING response: {response[:32]!r}")
    except OSError as e:
        logging.getLogger(__name__).warning(
            "FalkorDB server unreachable at %s:%s (%r) — graph features degraded "
            "to no-op. Start it with: docker compose up -d", host, port, e,
        )
        _graphiti_client = None
        return None

    try:
        from graphiti_core import Graphiti
        from graphiti_core.driver.falkordb_driver import FalkorDriver

        driver = FalkorDriver(
            host=host, port=port, username=username, password=password, database=database
        )
        client = Graphiti(graph_driver=driver)
        await client.build_indices_and_constraints()
    except Exception as e:  # noqa: BLE001 — anything answering 6379 that is not a
        # working FalkorDB (e.g. a plain Redis without the graph module) lands here.
        logging.getLogger(__name__).warning(
            "FalkorDB handshake at %s:%s failed (%r) — graph features degraded to "
            "no-op. Is the falkordb-server container healthy? (docker compose ps)",
            host, port, e,
        )
        _graphiti_client = None
        return None

    _graphiti_client = client
    logging.getLogger(__name__).info(
        "Graphiti connected to FalkorDB at %s:%s (database=%s).", host, port, database
    )
    return _graphiti_client


def get_graphiti_client():
    """The module-level Graphiti singleton, or None when degraded/uninitialized."""
    return _graphiti_client


def _get_driver():
    """The GraphDriver behind the singleton, or None when degraded."""
    if _graphiti_client is None:
        return None
    return getattr(_graphiti_client, "driver", None) or getattr(
        _graphiti_client, "graph_driver", None
    )


def compute_edge_id(
    entity_a_id: str, entity_b_id: str, edge_type: str, valid_from_event_id: str
) -> str:
    """The deterministic UUID every edge write is keyed by (idempotency contract)."""
    return str(
        uuid5(NAMESPACE_OID, f"{entity_a_id}:{entity_b_id}:{edge_type}:{valid_from_event_id}")
    )


async def _write_edge(edge: dict) -> None:
    """
    Execute one idempotent FACT-edge upsert against FalkorDB.

    MERGE-keyed by edge_id; valid_from_ts / valid_until_ts are stamped once at
    first write (coalesce), so crash-recovery replays never shift the temporal
    window. Never raises — a failed write degrades to a WARNING (the FSM must
    not block on the graph backend).
    """
    driver = _get_driver()
    if driver is None:
        return
    attributes = edge.get("attributes") or {}
    until_clause = (
        ",\n            r.valid_until_ts = coalesce(r.valid_until_ts, $now)"
        if edge.get("valid_until_event_id")
        else ""
    )
    query = f"""
        MERGE (a:Entity {{entity_id: $a}})
        MERGE (b:Entity {{entity_id: $b}})
        MERGE (a)-[r:FACT {{edge_id: $edge_id}}]->(b)
        SET r.edge_type = $edge_type,
            r.valid_from_event_id = $valid_from,
            r.valid_until_event_id = $valid_until,
            r.confidence = $confidence,
            r.attributes_json = $attributes_json,
            r.valid_from_ts = coalesce(r.valid_from_ts, $now){until_clause}
    """
    try:
        await driver.execute_query(
            query,
            a=edge["entity_a_id"],
            b=edge["entity_b_id"],
            edge_id=edge["edge_id"],
            edge_type=edge.get("edge_type") or "related_to",
            valid_from=edge.get("valid_from_event_id"),
            valid_until=edge.get("valid_until_event_id"),
            confidence=float(attributes.get("confidence", edge.get("confidence", 1.0))),
            attributes_json=json.dumps(attributes),
            now=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:  # noqa: BLE001 — graph writes are best-effort by contract
        logging.getLogger(__name__).warning(
            "Graphiti edge write failed for %s (%r) — edge dropped.", edge.get("edge_id"), e
        )


async def upsert_temporal_edge(
    entity_a_id: str,
    entity_b_id: str,
    edge_type: str,
    valid_from_event_id: str,
    valid_until_event_id: Optional[str],
    attributes: dict,
) -> None:
    """
    Insert or update a temporal edge with a deterministic UUID (idempotent).

    Purpose:
        Writes one fact edge to the Graphiti graph. The UUID is computed as:
        uuid5(NAMESPACE_OID, f"{entity_a_id}:{entity_b_id}:{edge_type}:{valid_from_event_id}")
        This determinism makes repeated calls (e.g., during crash recovery replay)
        safe — the upsert replaces the existing edge rather than creating a duplicate.

        Degraded mode (no FalkorDB server): a silent no-op — the FSM never blocks
        on the graph backend.

    Inputs:
        entity_a_id: str — source entity node ID.
        entity_b_id: str — target entity node ID.
        edge_type: str — the relationship type (e.g., "Owns", "Located_In").
        valid_from_event_id: str — event ID at which this fact became true.
        valid_until_event_id: Optional[str] — event ID at which this fact was
            invalidated. None if the fact is currently still true.
        attributes: dict — additional edge metadata (e.g., confidence, provisional flag).

    Outputs:
        None. Side effect: upserts one temporal edge in the Graphiti graph.
    """
    await _write_edge({
        "edge_id": compute_edge_id(entity_a_id, entity_b_id, edge_type, valid_from_event_id),
        "entity_a_id": entity_a_id,
        "entity_b_id": entity_b_id,
        "edge_type": edge_type,
        "valid_from_event_id": valid_from_event_id,
        "valid_until_event_id": valid_until_event_id,
        "attributes": attributes,
    })


_EDGE_HOP_QUERY = """
    MATCH (a:Entity)-[r:FACT]->(b:Entity)
    WHERE (a.entity_id IN $ids OR b.entity_id IN $ids)
      AND r.valid_from_ts <= $active_ts
      AND (r.valid_until_ts IS NULL OR r.valid_until_ts > $active_ts)
    RETURN r.edge_id AS edge_id,
           a.entity_id AS entity_a_id,
           b.entity_id AS entity_b_id,
           r.edge_type AS edge_type,
           r.confidence AS confidence,
           r.attributes_json AS attributes_json,
           r.valid_from_event_id AS valid_from_event_id
"""

_EVENT_TS_QUERY = """
    MATCH (:Entity)-[r:FACT]->(:Entity)
    WHERE r.valid_from_event_id = $eid
    RETURN min(r.valid_from_ts) AS ts
"""


async def query_point_in_time_subgraph(
    entity_ids: list[str],
    active_event_id: str,
    max_hops: int = 2,
) -> list[dict]:
    """
    Return temporal edges within the valid window of the given event ID.

    Purpose:
        Frontier BFS from the seed entities, one Cypher query per hop, collecting
        FACT edges valid at the active event's point in time. Event IDs carry no
        inherent ordering, so the temporal window is evaluated on the write
        timestamps: the active event resolves to the valid_from_ts of any edge it
        created; an unknown event (the beat currently being drafted — the normal
        case) means "now", i.e. all currently-valid edges.

        Degraded mode (no FalkorDB server) and any query failure return [] —
        the FSM never blocks on the graph backend.

    Inputs:
        entity_ids: List[str] — seed entity IDs to start traversal from.
        active_event_id: str — the event ID representing "now" in the story.
        max_hops: int — maximum traversal depth (default 2).

    Outputs:
        List[dict]: edge dicts with entity_a_id, entity_b_id, edge_type,
            confidence, attributes (dict), valid_from_event_id, edge_id —
            the shape partition_coreference_links and _edges_to_text consume.
    """
    driver = _get_driver()
    if driver is None or not entity_ids:
        return []

    try:
        active_ts = _FAR_FUTURE_TS
        result = await driver.execute_query(_EVENT_TS_QUERY, eid=active_event_id)
        records = (result or [None])[0] or []
        if records and records[0].get("ts"):
            active_ts = records[0]["ts"]

        frontier = list(dict.fromkeys(entity_ids))
        seen_entities = set(frontier)
        edges_by_id: dict[str, dict] = {}
        for _ in range(max(1, max_hops)):
            if not frontier:
                break
            result = await driver.execute_query(
                _EDGE_HOP_QUERY, ids=frontier, active_ts=active_ts
            )
            records = (result or [None])[0] or []
            frontier = []
            for record in records:
                edge_id = record.get("edge_id")
                if not edge_id or edge_id in edges_by_id:
                    continue
                try:
                    attributes = json.loads(record.get("attributes_json") or "{}")
                except json.JSONDecodeError:
                    attributes = {}
                edges_by_id[edge_id] = {
                    "edge_id": edge_id,
                    "entity_a_id": record.get("entity_a_id"),
                    "entity_b_id": record.get("entity_b_id"),
                    "edge_type": record.get("edge_type"),
                    "confidence": float(record.get("confidence") or 1.0),
                    "attributes": attributes,
                    "valid_from_event_id": record.get("valid_from_event_id"),
                }
                for entity in (record.get("entity_a_id"), record.get("entity_b_id")):
                    if entity and entity not in seen_entities:
                        seen_entities.add(entity)
                        frontier.append(entity)
        return list(edges_by_id.values())
    except Exception as e:  # noqa: BLE001 — graph reads are best-effort by contract
        logging.getLogger(__name__).warning(
            "Graphiti point-in-time query failed (%r) — returning empty subgraph.", e
        )
        return []


def _edges_from_payload(event_payload: dict) -> list[dict]:
    """
    Normalize an _apply_event payload to writable edge dicts.

    Two payload shapes arrive here:
    - A raw edge dict (entity_a_id/entity_b_id keys) — applied as-is, with the
      deterministic edge_id recomputed when absent.
    - A beat_commit event record from the .jsonl log — reconstructed into the
      identical CONTAINS_BEAT edge node_commit_transaction originally wrote
      (same deterministic UUID, so replay is an idempotent upsert).
    """
    if event_payload.get("entity_a_id") and event_payload.get("entity_b_id"):
        edge = dict(event_payload)
        edge.setdefault(
            "edge_id",
            compute_edge_id(
                edge["entity_a_id"],
                edge["entity_b_id"],
                edge.get("edge_type") or "related_to",
                edge.get("valid_from_event_id") or "",
            ),
        )
        return [edge]

    if event_payload.get("type") == "beat_commit":
        scene_id = event_payload.get("scene_id")
        beat_id = event_payload.get("beat_id")
        if not scene_id or not beat_id:
            return []
        return [{
            "edge_id": compute_edge_id(scene_id, beat_id, "CONTAINS_BEAT", beat_id),
            "entity_a_id": scene_id,
            "entity_b_id": beat_id,
            "edge_type": "CONTAINS_BEAT",
            "valid_from_event_id": beat_id,
            "valid_until_event_id": None,
            "attributes": {"word_count": event_payload.get("word_count", 0)},
        }]

    return []


def _apply_event(event_payload: dict) -> None:
    """
    Replay one event payload against the Graphiti graph (sync funnel).

    Purpose:
        Called by memory/branch_manager.py during crash recovery replay to
        reapply Graphiti writes from .jsonl event records. Normalizes the
        payload via _edges_from_payload and executes the idempotent upserts.

        Sync/async bridge: crash recovery runs synchronously before the FSM
        resumes, so with no running loop the writes execute via asyncio.run.
        Inside a live loop (e.g. a route handler) they are scheduled as a task
        — graph writes are best-effort and idempotent, so completion ordering
        is not load-bearing.

    Inputs:
        event_payload: dict — an edge dict or a beat_commit event record.

    Outputs:
        None. Side effect: upserts the normalized edges (no-op when degraded).
    """
    if _graphiti_client is None:
        return
    edges = _edges_from_payload(event_payload)
    if not edges:
        return

    async def _apply_all() -> None:
        for edge in edges:
            await _write_edge(edge)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_apply_all())
        return
    loop.create_task(_apply_all())

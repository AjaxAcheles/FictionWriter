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

    _apply_event() is a no-op stub in early development to allow crash recovery logic
    to be tested end-to-end before full Graphiti writes are wired in.

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

import logging
from pathlib import Path
from typing import Optional
from uuid import uuid5, NAMESPACE_OID

_graphiti_client = None


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

        Sprint 1: delegates to _apply_event() no-op stub. Full Graphiti writes wired
        in Sprint 3+.

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
    edge_id = str(uuid5(NAMESPACE_OID, f"{entity_a_id}:{entity_b_id}:{edge_type}:{valid_from_event_id}"))
    _apply_event({
        "edge_id": edge_id,
        "entity_a_id": entity_a_id,
        "entity_b_id": entity_b_id,
        "edge_type": edge_type,
        "valid_from_event_id": valid_from_event_id,
        "valid_until_event_id": valid_until_event_id,
        "attributes": attributes,
    })


async def query_point_in_time_subgraph(
    entity_ids: list[str],
    active_event_id: str,
    max_hops: int = 2,
) -> list[dict]:
    """
    Return temporal edges within the valid window of the given event ID.

    Purpose:
        Sprint 1 stub — returns empty list. Full traversal implemented in Sprint 3+
        once Graphiti writes are wired in.

    Inputs:
        entity_ids: List[str] — seed entity IDs to start traversal from.
        active_event_id: str — the event ID representing "now" in the story.
        max_hops: int — maximum traversal depth (default 2).

    Outputs:
        List[dict]: Empty list in Sprint 1. Sprint 3+: temporal edge dicts within
            the valid window.
    """
    return []


def _apply_event(event_payload: dict) -> None:
    """
    Replay one beat_commit event against the Graphiti graph. NO-OP STUB.

    Purpose:
        Called during crash recovery replay to reapply Graphiti writes from .jsonl
        event records. Currently a no-op stub to allow crash recovery logic to be
        tested end-to-end before full Graphiti writes are wired in. When implemented,
        it extracts temporal edge data from the event_payload and calls upsert_temporal_edge().

    Inputs:
        event_payload: dict — a beat_commit event dict from the .jsonl log.

    Outputs:
        None. No-op stub.
    """
    return

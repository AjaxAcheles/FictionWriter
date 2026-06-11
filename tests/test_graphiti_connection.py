"""
tests/test_graphiti_connection.py

FalkorDB-backed Graphiti client connection tests.

Purpose:
    Verifies the docker-compose FalkorDB integration surface WITHOUT requiring
    a running container: config parsing/defaults, the unreachable-server
    degradation path, the wrong-server degradation path, and the intact no-op
    chain while degraded. Live connectivity is exercised on a dev box with
    `docker compose up -d` + `uv run python app.py`.
"""

import asyncio
import socket
import threading

import pytest
import yaml

from core.config_loader import AppConfig, GraphitiConfig
from memory import graphiti_client


def _valid_config_dict() -> dict:
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_graphiti_section_parses_and_defaults():
    raw = _valid_config_dict()
    config = AppConfig(**raw)
    assert config.graphiti.host == "localhost"
    assert config.graphiti.port == 6379
    assert config.graphiti.database == "fictionwriter"

    # Section omitted entirely → defaults (old config.yaml files keep parsing).
    raw.pop("graphiti", None)
    assert AppConfig(**raw).graphiti.port == 6379

    # extra='forbid' still enforced inside the section.
    with pytest.raises(Exception):
        GraphitiConfig(host="x", bad_key=True)


async def test_init_degrades_when_server_unreachable(monkeypatch):
    """Closed port → None client, WARNING, no exception."""
    config = AppConfig(**_valid_config_dict())
    # An OS-assigned free port that nothing listens on.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    config.graphiti.host = "127.0.0.1"
    config.graphiti.port = free_port

    client = await graphiti_client.init_graphiti_client(config)
    assert client is None
    assert graphiti_client.get_graphiti_client() is None


async def test_init_degrades_on_non_falkor_listener():
    """Something accepts TCP on the port but is not FalkorDB → degrade, not crash."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def _accept_and_close():
        try:
            conn, _ = server.accept()
            conn.close()
        except OSError:
            pass

    thread = threading.Thread(target=_accept_and_close, daemon=True)
    thread.start()
    try:
        config = AppConfig(**_valid_config_dict())
        config.graphiti.host = "127.0.0.1"
        config.graphiti.port = port
        client = await asyncio.wait_for(graphiti_client.init_graphiti_client(config), timeout=20)
        assert client is None
    finally:
        server.close()


async def test_noop_chain_intact_while_degraded():
    """Degraded mode: queries return [] and edge upserts are silent no-ops."""
    graphiti_client._graphiti_client = None
    edges = await graphiti_client.query_point_in_time_subgraph(
        entity_ids=["a"], active_event_id="e1"
    )
    assert edges == []
    await graphiti_client.upsert_temporal_edge("a", "b", "KNOWS", "e1", None, {})

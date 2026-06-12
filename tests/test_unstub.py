"""
tests/test_unstub.py

Un-Stubbed Subsystem Tests — Graphiti writes/queries, Epistemic Belief
promotion, and RAPTOR tree rehydration.

Purpose:
    Pins the replacement of the three intentional placeholders:
    1. Graphiti: upsert_temporal_edge executes a real MERGE keyed by the
       deterministic UUID; _apply_event reconstructs the CONTAINS_BEAT edge from
       a beat_commit .jsonl record (sync replay path); query_point_in_time_subgraph
       runs a frontier BFS and returns consumer-shaped edge dicts. Degraded mode
       (no FalkorDB) stays a silent no-op (pinned in test_graphiti_connection).
    2. Epistemic promotion: pending provisional claims reinforced by a 200-token
       window co-occurrence are confirmed and stamped into Graphiti; unreinforced
       claims stay pending for the Alignment UI.
    3. RAPTOR: init_raptor_tree rehydrates the in-memory tree from RaptorNodes.
"""

import json

import pytest

from core import runtime
from memory import graphiti_client, provisional_store, sqlite_db

from tests.test_sprint3 import seed_narrative


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SQLITE_PATH", tmp_path / "fictionwriter.db")
    monkeypatch.setattr(runtime, "STYLES_DIR", tmp_path / "styles")
    monkeypatch.setattr(runtime, "EVENT_LOG_PATH", tmp_path / "event_log.jsonl")
    monkeypatch.setattr(runtime, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(runtime, "GRAPHITI_PATH", tmp_path / "graphiti.db")
    sqlite_db.init_db(runtime.SQLITE_PATH)
    return runtime


class FakeDriver:
    """Records execute_query calls; returns canned (records, header, None)."""

    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    async def execute_query(self, cypher_query_, **kwargs):
        self.calls.append((cypher_query_, kwargs))
        records = self._results.pop(0) if self._results else []
        return records, [], None


class FakeClient:
    def __init__(self, driver):
        self.driver = driver


# --------------------------------------------------------------------------- #
# Graphiti writes                                                             #
# --------------------------------------------------------------------------- #


async def test_upsert_temporal_edge_executes_idempotent_merge(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(graphiti_client, "_graphiti_client", FakeClient(driver))

    await graphiti_client.upsert_temporal_edge(
        "char_mara", "the_harbor", "LOCATED_IN", "sc_001_beat_0", None,
        {"confidence": 0.9},
    )

    assert len(driver.calls) == 1
    cypher, params = driver.calls[0]
    assert "MERGE" in cypher
    assert params["edge_id"] == graphiti_client.compute_edge_id(
        "char_mara", "the_harbor", "LOCATED_IN", "sc_001_beat_0"
    )
    assert params["confidence"] == 0.9
    assert params["a"] == "char_mara" and params["b"] == "the_harbor"


def test_apply_event_replays_beat_commit_record(monkeypatch):
    """Sync replay path (crash recovery): event record → CONTAINS_BEAT upsert."""
    driver = FakeDriver()
    monkeypatch.setattr(graphiti_client, "_graphiti_client", FakeClient(driver))

    graphiti_client._apply_event(
        {"type": "beat_commit", "beat_id": "sc1_beat_0", "scene_id": "sc1",
         "word_count": 42}
    )

    assert len(driver.calls) == 1
    _, params = driver.calls[0]
    # Identical deterministic UUID to what node_commit_transaction wrote live.
    assert params["edge_id"] == graphiti_client.compute_edge_id(
        "sc1", "sc1_beat_0", "CONTAINS_BEAT", "sc1_beat_0"
    )
    assert json.loads(params["attributes_json"])["word_count"] == 42


def test_apply_event_ignores_unknown_payloads(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(graphiti_client, "_graphiti_client", FakeClient(driver))
    graphiti_client._apply_event({"type": "HUMAN_EDIT_UNRECONCILED"})
    assert driver.calls == []


# --------------------------------------------------------------------------- #
# Graphiti point-in-time query                                                #
# --------------------------------------------------------------------------- #


async def test_query_point_in_time_bfs_expands_frontier(monkeypatch):
    record = {
        "edge_id": "e1", "entity_a_id": "char_mara", "entity_b_id": "the_harbor",
        "edge_type": "LOCATED_IN", "confidence": 0.9, "attributes_json": "{}",
        "valid_from_event_id": "sc_001_beat_0",
    }
    # Call order: event-ts resolution, hop 1, hop 2 (expanded frontier, empty).
    driver = FakeDriver(results=[[{"ts": None}], [record], []])
    monkeypatch.setattr(graphiti_client, "_graphiti_client", FakeClient(driver))

    edges = await graphiti_client.query_point_in_time_subgraph(
        ["char_mara"], "sc_001_beat_1"
    )

    assert len(edges) == 1
    edge = edges[0]
    assert edge["entity_a_id"] == "char_mara"
    assert edge["edge_type"] == "LOCATED_IN"
    assert edge["confidence"] == 0.9
    assert edge["attributes"] == {}
    assert len(driver.calls) == 3
    # Hop 2 seeds from the entities hop 1 discovered.
    assert driver.calls[2][1]["ids"] == ["the_harbor"]
    # Unknown active event → "now" sentinel cutoff.
    assert driver.calls[1][1]["active_ts"] == graphiti_client._FAR_FUTURE_TS


async def test_query_returns_empty_on_driver_failure(monkeypatch):
    class BrokenDriver:
        async def execute_query(self, cypher_query_, **kwargs):
            raise RuntimeError("graph down")

    monkeypatch.setattr(graphiti_client, "_graphiti_client", FakeClient(BrokenDriver()))
    edges = await graphiti_client.query_point_in_time_subgraph(["a"], "e1")
    assert edges == []


# --------------------------------------------------------------------------- #
# Epistemic Belief promotion                                                  #
# --------------------------------------------------------------------------- #


async def test_promotion_confirms_reinforced_claims_only(env):
    from fsm.nodes.node_commit_transaction import _promote_epistemic_beliefs

    seed_narrative(env.SQLITE_PATH)  # creates Characters row: char_mara / "Mara"
    sqlite_db.append_scene_prose(
        env.SQLITE_PATH, "sc_001",
        "Mara stared across the harbor. She counted the boats one by one.", 12,
    )
    sqlite_db.close_scene(env.SQLITE_PATH, "sc_001")  # promotion reads committed scenes
    ids = provisional_store.add_claims([
        {"pronoun_text": "She", "linked_entity_id": "char_mara", "confidence": 0.6},
        {"pronoun_text": "he", "linked_entity_id": "char_dockmaster", "confidence": 0.6},
    ])

    await _promote_epistemic_beliefs(env.SQLITE_PATH, "ch_001")

    statuses = {c["claim_id"]: c["status"] for c in provisional_store._load()}
    assert statuses[ids[0]] == "confirmed"   # Mara + "she" co-occur in window
    assert statuses[ids[1]] == "pending"     # never reinforced — left for review


async def test_promotion_stamps_confirmed_fact_into_graph(env, monkeypatch):
    from fsm.nodes.node_commit_transaction import _promote_epistemic_beliefs

    driver = FakeDriver()
    monkeypatch.setattr(graphiti_client, "_graphiti_client", FakeClient(driver))
    seed_narrative(env.SQLITE_PATH)
    sqlite_db.append_scene_prose(
        env.SQLITE_PATH, "sc_001", "Mara frowned. She left the pier.", 6,
    )
    sqlite_db.close_scene(env.SQLITE_PATH, "sc_001")
    provisional_store.add_claims(
        [{"pronoun_text": "she", "linked_entity_id": "char_mara", "confidence": 0.6}]
    )

    await _promote_epistemic_beliefs(env.SQLITE_PATH, "ch_001")

    assert len(driver.calls) == 1
    _, params = driver.calls[0]
    assert params["a"] == "pronoun_she" and params["b"] == "char_mara"
    assert params["confidence"] == 1.0


def test_cooccurrence_window_logic():
    from fsm.nodes.node_commit_transaction import (
        _cooccur_within_window, _normalize_tokens,
    )

    tokens = _normalize_tokens("Mara walked. " + "filler " * 250 + "She slept.")
    assert _cooccur_within_window(tokens, ["mara"], "she", 200) is False  # 252 apart
    assert _cooccur_within_window(tokens, ["mara"], "she", 300) is True
    assert _cooccur_within_window(tokens, ["absent"], "she", 200) is False


# --------------------------------------------------------------------------- #
# RAPTOR rehydration                                                          #
# --------------------------------------------------------------------------- #


def test_init_raptor_tree_rehydrates_from_sqlite(env):
    from memory.raptor import init_raptor_tree, write_raptor_node_full

    write_raptor_node_full(
        env.SQLITE_PATH, "n_ch1", None, "chapter", "Chapter summary.", ["sc_001"]
    )
    write_raptor_node_full(
        env.SQLITE_PATH, "n_sc1", "n_ch1", "scene", "Scene summary.", ["sc_001"]
    )

    tree = init_raptor_tree(env.SQLITE_PATH)
    assert tree["n_ch1"]["level"] == "chapter"
    assert tree["n_ch1"]["parent_id"] is None
    assert tree["n_ch1"]["summary"] == "Chapter summary."
    assert tree["n_sc1"]["parent_id"] == "n_ch1"
    assert tree["n_ch1"]["updated_at"]


def test_init_raptor_tree_empty_when_db_missing(tmp_path):
    from memory.raptor import init_raptor_tree

    assert init_raptor_tree(tmp_path / "missing.db") == {}

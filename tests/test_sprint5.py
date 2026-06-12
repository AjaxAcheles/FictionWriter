"""
tests/test_sprint5.py

Sprint 5 Test Suite — Full Stack Integration, Codex UI & Ingestion.

Purpose:
    Quart test-client coverage for every JSON endpoint, settings write-back
    validation, the pause guard on branch restore, the sliding-window chunker,
    the heuristic NER + three-tier coreference pipeline, and the provisional
    claim lifecycle. No live endpoint or browser required.
"""

import json
from pathlib import Path

import pytest

from core import runtime
from memory import provisional_store, sqlite_db

from tests.test_sprint3 import ACTIVE_PROSE, seed_narrative


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SQLITE_PATH", tmp_path / "fictionwriter.db")
    monkeypatch.setattr(runtime, "GRAPHITI_PATH", tmp_path / "graphiti.db")
    monkeypatch.setattr(runtime, "EVENT_LOG_PATH", tmp_path / "event_log.jsonl")
    monkeypatch.setattr(runtime, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(runtime, "STYLES_DIR", tmp_path / "styles")
    monkeypatch.setattr(provisional_store, "STORE_PATH", tmp_path / "claims.json")
    sqlite_db.init_db(runtime.SQLITE_PATH)

    class _FakeEncoder:
        def encode(self, text):
            return text.split()

    monkeypatch.setattr("llm.tokenizer._get_tiktoken_encoder", lambda: _FakeEncoder())
    return runtime


@pytest.fixture
async def client(env):
    """Quart test client over a bare app (no resource init — routes hit tmp stores)."""
    from quart import Quart

    from routes.alignment import alignment_bp
    from routes.codex import codex_bp
    from routes.control import control_bp
    from routes.dashboard import dashboard_bp
    from routes.settings import settings_bp

    app = Quart(__name__, template_folder=str(Path("templates").resolve()),
                static_folder=str(Path("static").resolve()))
    for bp in (dashboard_bp, control_bp, alignment_bp, codex_bp, settings_bp):
        app.register_blueprint(bp)
    return app.test_client()


# --------------------------------------------------------------------------- #
# Provisional claim store                                                     #
# --------------------------------------------------------------------------- #


def test_provisional_store_lifecycle(env):
    ids = provisional_store.add_claims(
        [{"pronoun_text": "she", "linked_entity_id": "char_mara", "confidence": 0.6}]
    )
    assert len(provisional_store.list_pending()) == 1
    assert provisional_store.confirm(ids[0]) is True
    assert provisional_store.list_pending() == []
    assert provisional_store.confirm("nonexistent") is False


# --------------------------------------------------------------------------- #
# Ingestion                                                                   #
# --------------------------------------------------------------------------- #


async def test_chunker_overlap_and_coverage(env):
    from ingestion.pipeline import _chunk_text

    words = [f"w{i}" for i in range(1000)]
    text = " ".join(words)
    chunks = [c async for c in _chunk_text(text, window_tokens=200, tokenizer_family="char_heuristic")]

    assert len(chunks) >= 2
    # Coverage: every word appears in at least one chunk.
    seen = set()
    for chunk in chunks:
        seen.update(chunk.split())
    assert seen == set(words)
    # Overlap: consecutive chunks share words (50% default).
    first, second = set(chunks[0].split()), set(chunks[1].split())
    assert first & second


async def test_ner_and_coreference_tiers(env):
    from ingestion.coreference import extract_entities, resolve_coreferences

    text = (
        "Mara Voss stood at the dock. Mara Voss counted the boats while the rain fell. "
        "She lifted the manifest. Far away across the grey water and beyond the long "
        "breakwater and past every anchored hull and distant light, "
        + "the harbor slept and the night wore on and nothing moved at all. " * 20
        + "He waited."
    )
    entities = await extract_entities(text, "proj")
    names = {e["name"] for e in entities}
    assert "Mara Voss" in names

    links = await resolve_coreferences(text, entities, confidence_floor=0.65)
    she = next(l for l in links if l["pronoun_text"].lower() == "she")
    assert she["link_type"] == "high" and she["provisional"] is False
    he = next(l for l in links if l["pronoun_text"].lower() == "he")
    # Distant pronoun: confidence decayed below the floor → MLI provisional 'mid'.
    assert he["link_type"] == "mid" and he["provisional"] is True


async def test_ingest_manuscript_writes_stores(env, tmp_path):
    from core.config_loader import load_config
    from ingestion.pipeline import ingest_manuscript

    manuscript = tmp_path / "manuscript.txt"
    manuscript.write_text(
        "Mara Voss docked the skiff. Mara Voss waved. " * 30
        + ("filler words drift along the quay " * 400)
        + " She smiled at last."
    )
    await ingest_manuscript(manuscript, load_config(), "proj")
    chars = sqlite_db.get_characters(env.SQLITE_PATH)
    assert any(c["name"] == "Mara Voss" for c in chars)


# --------------------------------------------------------------------------- #
# Codex endpoints                                                             #
# --------------------------------------------------------------------------- #


async def test_codex_characters_threads_raptor_events(env, client):
    pointer = seed_narrative(env.SQLITE_PATH)
    sqlite_db.insert_row(
        env.SQLITE_PATH, "Threads",
        {"thread_id": "th_1", "name": "Debt", "description": "", "priority": 0.7, "status": "open"},
    )
    from memory.raptor import write_raptor_node_full
    write_raptor_node_full(env.SQLITE_PATH, "raptor_ch_001", None, "chapter", "Chapter summary.", [])
    from memory.event_log import write_event
    write_event(env.EVENT_LOG_PATH, {"type": "beat_commit", "beat_id": "b0"})

    chars = await (await client.get("/codex/characters")).get_json()
    assert chars[0]["name"] == "Mara" and "pad" in chars[0]

    threads = await (await client.get("/codex/threads")).get_json()
    assert threads[0]["id"] == "th_1"

    response = await client.post(
        "/codex/threads/priority", json={"thread_id": "th_1", "priority_score": 0.95}
    )
    assert (await response.get_json())["status"] == "updated"
    assert sqlite_db.get_row(env.SQLITE_PATH, "Threads", "thread_id", "th_1")["priority"] == 0.95

    raptor = await (await client.get("/codex/raptor")).get_json()
    assert raptor[0]["node_id"] == "raptor_ch_001"

    events = await (await client.get("/codex/events")).get_json()
    assert events[0]["type"] == "beat_commit"


async def test_codex_manuscript_assembly_order(env, client):
    seed_narrative(env.SQLITE_PATH)
    sqlite_db.append_scene_prose(env.SQLITE_PATH, "sc_001", "First scene prose.", 3)
    sqlite_db.insert_row(
        env.SQLITE_PATH, "Scenes",
        {"scene_id": "sc_002", "chapter_id": "ch_001", "scene_index": 1,
         "description": "", "word_budget": 100, "word_count": 0},
    )
    sqlite_db.append_scene_prose(env.SQLITE_PATH, "sc_002", "Second scene prose.", 3)

    response = await client.get("/codex/manuscript")
    text = (await response.get_data()).decode()
    assert text.index("First scene prose.") < text.index("Second scene prose.")

    # Structured form (?format=json) — used by dashboard hydration.
    scenes = await (await client.get("/codex/manuscript?format=json")).get_json()
    assert [s["scene_id"] for s in scenes] == ["sc_001", "sc_002"]
    assert scenes[0]["text"] == "First scene prose."
    assert scenes[0]["chapter_id"] == "ch_001"


async def test_codex_restore_requires_pause(env, client, monkeypatch):
    from memory import branch_manager
    from routes import control

    monkeypatch.setattr(branch_manager, "SNAPSHOTS_DIR", env.SNAPSHOTS_DIR)
    monkeypatch.setattr(branch_manager, "SQLITE_PATH", env.SQLITE_PATH)
    monkeypatch.setattr(branch_manager, "GRAPHITI_PATH", env.GRAPHITI_PATH)
    seed_narrative(env.SQLITE_PATH)
    snapshot = branch_manager.create_chapter_snapshot("ch_001", "2026-06-11T00:00:00Z")

    monkeypatch.setattr(control, "pause_requested", False)
    response = await client.post(
        "/codex/restore", json={"snapshot_filename": snapshot.name, "reasons": None}
    )
    assert response.status_code == 409

    monkeypatch.setattr(control, "pause_requested", True)
    response = await client.post(
        "/codex/restore", json={"snapshot_filename": snapshot.name, "reasons": "Go darker."}
    )
    data = await response.get_json()
    assert data["status"] == "restored" and data["branch_reason"] == "Go darker."


# --------------------------------------------------------------------------- #
# Alignment endpoints                                                         #
# --------------------------------------------------------------------------- #


async def test_alignment_claim_endpoints(env, client):
    ids = provisional_store.add_claims(
        [{"pronoun_text": "he", "linked_entity_id": "char_dock", "confidence": 0.6}]
    )
    claims = await (await client.get("/alignment/claims")).get_json()
    assert len(claims) == 1

    response = await client.post("/alignment/confirm", json={"claim_id": ids[0]})
    assert (await response.get_json())["status"] == "confirmed"
    assert await (await client.get("/alignment/claims")).get_json() == []

    response = await client.post("/alignment/contradict", json={"claim_id": "missing"})
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Settings endpoints                                                          #
# --------------------------------------------------------------------------- #


async def test_settings_write_back_roundtrip(env, client, tmp_path, monkeypatch):
    import shutil

    work = tmp_path / "cfg"
    work.mkdir()
    shutil.copy("config.yaml", work / "config.yaml")
    monkeypatch.chdir(work)

    response = await client.post(
        "/settings", form={"stel_cosine_distance": "0.18", "retry_count_max": "7"}
    )
    data = await response.get_json()
    assert data["status"] == "updated"

    text = (work / "config.yaml").read_text()
    assert "stel_cosine_distance: 0.18" in text
    assert "retry_count_max: 7" in text
    # Comments preserved by the targeted rewrite.
    assert "# Dc gate used by edge_mode_selector" in text

    from core.config_loader import load_config
    config = load_config(work / "config.yaml")
    assert config.thresholds.stel_cosine_distance == 0.18
    assert config.generation.retry_count_max == 7


async def test_settings_rejects_invalid_value(env, client, tmp_path, monkeypatch):
    import shutil

    work = tmp_path / "cfg"
    work.mkdir()
    original = Path("config.yaml").read_text()
    shutil.copy("config.yaml", work / "config.yaml")
    monkeypatch.chdir(work)

    response = await client.post("/settings", form={"retry_count_max": "not_a_number"})
    assert response.status_code == 400
    assert (work / "config.yaml").read_text() == original  # untouched


# --------------------------------------------------------------------------- #
# Page renders                                                                #
# --------------------------------------------------------------------------- #


async def test_pages_render(env, client, monkeypatch):
    from memory import branch_manager

    monkeypatch.setattr(branch_manager, "SNAPSHOTS_DIR", env.SNAPSHOTS_DIR)
    for path in ("/", "/settings", "/alignment", "/codex"):
        response = await client.get(path)
        assert response.status_code == 200, path
        body = (await response.get_data()).decode()
        assert "FictionWriter" in body

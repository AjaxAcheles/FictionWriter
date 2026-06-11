"""
tests/test_sprint6.py

Sprint 6 Test Suite — Antislop & Leiden.

Purpose:
    Verifies the real detect_slop/resolve_slop implementations behind the
    Sprint 1 interface (callers unchanged) and the Leiden cluster_scenes
    drop-in behind the Sprint 1 placeholder contract. The Sprint 3 smoke test
    continues to pass untouched — that is the stub-replacement guarantee.
"""

import pytest

from core.antislop import SlopFlag, detect_slop, resolve_slop
from memory.raptor import cluster_scenes


# --------------------------------------------------------------------------- #
# detect_slop                                                                 #
# --------------------------------------------------------------------------- #


def test_detect_dictionary_phrases():
    text = (
        "Her voice was barely above a whisper. The ruin stood as a testament to "
        "the old wars, its palpable dread a rich tapestry of grief."
    )
    flags = detect_slop(text)
    categories = {f.offending_text.lower(): f.category for f in flags}
    assert "barely above a whisper" in categories
    assert categories["a testament to"] == "llm_ism"
    assert "rich tapestry" in categories
    # Sorted by severity descending.
    severities = [f.severity for f in flags]
    assert severities == sorted(severities, reverse=True)


def test_detect_adverb_cluster_regex():
    flags = detect_slop("He moved quickly quietly carefully through the dark.")
    assert any(f.category == "adverb_cluster" for f in flags)


def test_detect_repeated_openers():
    text = "Mara ran. Mara jumped. Mara swore. The night swallowed her."
    flags = detect_slop(text)
    assert any(f.category == "repeated_opener" and f.offending_text == "Mara" for f in flags)


def test_clean_text_returns_no_flags():
    text = (
        "Mara walks the pier and counts the boats. She grips the manifest and "
        "stares down the dock master. Gulls scream overhead."
    )
    assert detect_slop(text) == []


def test_detect_is_deterministic():
    text = "It was a testament to her will, barely above a whisper."
    assert detect_slop(text) == detect_slop(text)


def test_empty_text():
    assert detect_slop("") == []


# --------------------------------------------------------------------------- #
# resolve_slop                                                                #
# --------------------------------------------------------------------------- #


def test_resolve_applies_dictionary_replacements():
    text = "The ruin was a testament to the old wars."
    out = resolve_slop(text, detect_slop(text))
    assert "a testament to" not in out.lower()
    assert "proof of" in out


def test_resolve_preserves_unreplaceable_flags():
    text = "A symphony of bells rang out."  # dictionary entry without replacement
    flags = detect_slop(text)
    assert flags and flags[0].replacement is None
    assert resolve_slop(text, flags) == text


def test_resolve_is_idempotent():
    text = "In that moment her voice fell barely above a whisper, a testament to fear."
    once = resolve_slop(text, detect_slop(text))
    twice = resolve_slop(once, detect_slop(once))
    assert once == twice


def test_resolve_empty_replacement_normalizes_whitespace():
    text = "She couldn't help but smile, then turned away."
    out = resolve_slop(text, detect_slop(text))
    assert "couldn't help but" not in out.lower()
    assert "  " not in out


def test_resolve_with_no_flags_is_identity():
    assert resolve_slop("Clean text.", []) == "Clean text."


def test_interface_contract_unchanged():
    """node_draft_prose's call shape: detect → resolve, str in str out."""
    flags = detect_slop("some draft text")
    assert isinstance(flags, list)
    out = resolve_slop("some draft text", flags)
    assert isinstance(out, str)


# --------------------------------------------------------------------------- #
# cluster_scenes (Leiden drop-in)                                             #
# --------------------------------------------------------------------------- #

HARBOR_A = "the harbor docks ships saltwater pier boats fishermen nets tide " * 25
HARBOR_B = "the harbor docks ships saltwater pier boats fishermen tide gulls " * 25
DESERT = "desert sand dunes scorching caravan camels oasis mirage heat sun " * 25


def test_cluster_groups_similar_scenes():
    clusters = cluster_scenes([HARBOR_A, DESERT, HARBOR_B])
    assert len(clusters) == 2
    harbor_cluster = next(c for c in clusters if "harbor" in c)
    assert HARBOR_A.strip() in harbor_cluster and HARBOR_B.strip() in harbor_cluster
    assert "desert" not in harbor_cluster


def test_cluster_all_dissimilar_yields_singletons():
    forest = "ancient forest moss ferns owls canopy shadows roots loam dusk " * 25
    clusters = cluster_scenes([HARBOR_A, DESERT, forest])
    assert len(clusters) == 3


def test_cluster_deterministic_and_order_preserving():
    scenes = [HARBOR_A, DESERT, HARBOR_B]
    first = cluster_scenes(scenes)
    second = cluster_scenes(scenes)
    assert first == second
    # Cluster ordering follows the lowest member index: harbor (0) before desert (1).
    assert "harbor" in first[0] and "desert" in first[1]


def test_cluster_edge_cases():
    assert cluster_scenes([]) == []
    assert cluster_scenes(["only scene"]) == ["only scene"]


async def test_compress_memory_caller_unchanged(tmp_path, monkeypatch):
    """node_compress_memory iterates the Leiden output identically to the stub."""
    from core import runtime
    from fsm.nodes import node_compress_memory as ncm
    from memory import sqlite_db
    from memory.raptor import get_raptor_summaries
    from tests.test_sprint3 import base_state, seed_narrative

    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SQLITE_PATH", tmp_path / "fictionwriter.db")
    monkeypatch.setattr(runtime, "STYLES_DIR", tmp_path / "styles")
    sqlite_db.init_db(runtime.SQLITE_PATH)
    pointer = seed_narrative(runtime.SQLITE_PATH)
    sqlite_db.append_scene_prose(runtime.SQLITE_PATH, "sc_001", HARBOR_A, 100)
    sqlite_db.close_scene(runtime.SQLITE_PATH, "sc_001")

    async def failing(endpoint, messages, **kwargs):
        raise RuntimeError("no endpoint in unit test")

    monkeypatch.setattr(ncm.call_llm_module, "collect_llm_response", failing)
    await ncm.node_compress_memory(base_state(pointer))
    summary = get_raptor_summaries(runtime.SQLITE_PATH, "sc_001", ["chapter"])["chapter"]
    assert summary != ""

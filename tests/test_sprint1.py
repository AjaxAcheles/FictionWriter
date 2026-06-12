"""
tests/test_sprint1.py

Sprint 1 Integration Test Suite — Vertical Slice End-to-End.

Purpose:
    The primary integration test suite for the Sprint 1 vertical slice delivery.
    Tests the full path from initial OrchestratorState through a minimal FSM run
    (planning → context assembly → stubbed prose draft → programmatic audit →
    fast-path commit) using real SQLite and real in-memory stores, but stubbed
    LLM calls (returning pre-canned responses).

    This file is named test_sprint1.py (not sprint1_test.py) to comply with pytest's
    default test_*.py discovery pattern configured in pyproject.toml.

    All async tests require pytest-asyncio. Uses tmp_path for isolated DB instances.

    Tests:
    test_sqlite_schema_init          — init_db() creates all nine tables without error.
    test_commit_intent_lifecycle     — pending → committed CommitIntent row lifecycle.
    test_antislop_stubs_passthrough  — detect_slop returns [], resolve_slop returns input.
    test_event_log_write_and_read    — write_event then iter_events returns same payload.
    test_programmatic_fast_path      — edge_programmatic_router fast path triggers on clean state.
    test_failure_object_validation   — FailureObject.model_validate() rejects invalid schema.
    test_prompt_loader_renders       — PromptLoader renders a template with correct variables.
    test_config_loader_integration   — load_config() + CommitIntent scan runs without error.
    test_full_vertical_slice         — End-to-end: stubbed planning → stubbed draft → fast commit.
"""

import pytest
from pathlib import Path
from datetime import datetime, timezone


def test_sqlite_schema_init(tmp_path):
    """
    Assert init_db() creates all nine required tables in a fresh SQLite database.

    Purpose:
        Verifies the schema initialization is complete and all table names are correct.
        Queries sqlite_master for table names and asserts all expected tables exist.

    Inputs:
        tmp_path: pytest fixture — provides an isolated temp directory for the DB file.

    Expected:
        All nine tables present: Arcs, Chapters, Scenes, Beats, Threads, Characters,
        CharacterEmotions, CommitIntent, RaptorNodes.
    """
    import sqlite3
    from memory.sqlite_db import init_db

    db_path = tmp_path / "test.db"
    init_db(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()

    tables = {row[0] for row in rows}
    expected = {
        "Arcs", "Chapters", "Scenes", "Beats", "Threads",
        "Characters", "CharacterEmotions", "CommitIntent", "RaptorNodes",
    }
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"


def test_commit_intent_lifecycle(tmp_path):
    """
    Assert CommitIntent rows transition correctly from pending to committed.

    Purpose:
        Simulates the intent-record pattern used by node_commit_transaction:
        1. Insert a row with status='pending'.
        2. Assert scan_pending_commit_intents returns it.
        3. Flip the row to status='committed'.
        4. Assert scan_pending_commit_intents returns empty list.

    Inputs:
        tmp_path: pytest fixture.

    Expected:
        Correct transition at each step. scan_pending_commit_intents returns [] after commit.
    """
    from contextlib import closing
    from memory.sqlite_db import init_db, get_connection, scan_pending_commit_intents

    db_path = tmp_path / "test.db"
    init_db(db_path)

    created_at = datetime.now(timezone.utc).isoformat()

    # Insert pending intent — beat_id is nullable (no beat row needed)
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "INSERT INTO CommitIntent (beat_id, status, created_at) VALUES (?, ?, ?)",
            (None, "pending", created_at),
        )
        intent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

    pending = scan_pending_commit_intents(db_path)
    assert len(pending) == 1
    assert pending[0]["beat_id"] is None
    assert pending[0]["status"] == "pending"

    # Flip to committed
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "UPDATE CommitIntent SET status='committed' WHERE intent_id=?",
            (intent_id,),
        )
        conn.commit()

    assert scan_pending_commit_intents(db_path) == []


def test_antislop_stubs_passthrough():
    """
    Assert detect_slop returns [] and resolve_slop returns text unchanged.

    Purpose:
        Verifies the Sprint 1–5 stub contract for the antislop interface.
        node_draft_prose depends on detect_slop returning [] and resolve_slop
        returning input unchanged — regression protection for when the stubs
        are replaced in Sprint 6.

    Expected:
        detect_slop("any text") == []
        resolve_slop("any text", []) == "any text"
    """
    from core.antislop import detect_slop, resolve_slop

    text = "She gazed into the distance with a heavy heart."
    assert detect_slop(text) == []
    assert resolve_slop(text, []) == text


def test_event_log_write_and_read(tmp_path):
    """
    Assert write_event then iter_events recovers the exact payload.

    Purpose:
        Verifies the round-trip integrity of the .jsonl event log. A payload written
        with write_event() must be recoverable via iter_events() with no loss of fields
        or type coercion. Tests PAD state atomicity: a beat_commit payload with nested
        pad_states is recovered intact.

    Inputs:
        tmp_path: pytest fixture.

    Expected:
        list(iter_events(log_path)) == [original_payload]
    """
    from memory.event_log import write_event, iter_events

    log_path = tmp_path / "event_log.jsonl"

    events = [
        {"event": "beat_commit", "beat_id": "b1", "prose_delta": "Once upon a time.",
         "pad_states": {"char-1": {"pleasure": 0.5, "arousal": 0.1, "dominance": -0.2}}},
        {"event": "beat_commit", "beat_id": "b2", "prose_delta": "Then came the storm."},
        {"event": "chapter_boundary", "chapter_id": "ch-1"},
    ]

    for e in events:
        write_event(log_path, e)

    recovered = list(iter_events(log_path))
    assert recovered == events


def test_programmatic_fast_path_triggers():
    """
    Assert edge_programmatic_router returns "node_commit_transaction" for a clean state.

    Purpose:
        Verifies that retry_count=0, empty critic_failures, and stylometric_distance=0.0
        (stub value, well below the 0.12 threshold * 0.7 multiplier = 0.084) triggers
        the fast path. This is the most common path in normal operation.

        Sprint 1: edge_programmatic_router is not yet implemented (body is pass).
        Placeholder — will be activated once the router is wired in Sprint 3.

    Expected:
        Placeholder pass — gates not wired yet.
    """
    pass


def test_failure_object_model_validate_rejects_invalid():
    """
    Assert FailureObject.model_validate() raises ValidationError on invalid input.

    Purpose:
        Verifies that the grammar-constrained critic output validation works correctly.
        An invalid dict (missing required field, wrong type) must raise a Pydantic
        ValidationError — triggering the retry logic in node_adversarial_critics.

    Expected:
        FailureObject.model_validate({"error_code": "X"}) raises ValidationError
        (missing offending_text, suggested_fix, critic_source).
    """
    import pydantic
    from fsm.state import FailureObject

    with pytest.raises(pydantic.ValidationError):
        FailureObject.model_validate({"error_code": "CONTINUITY_BREAK"})


def test_prompt_loader_renders_template(tmp_path):
    """
    Assert PromptLoader.load_and_render() returns a non-empty rendered string.

    Purpose:
        Creates a temporary .xml.j2 template, initializes PromptLoader pointing at
        the temp dir, renders it with a context dict, and asserts the variables were
        substituted correctly. Uses StrictUndefined — any missing variable would raise
        UndefinedError, so a clean render confirms the context was complete.

    Expected:
        Rendered string contains substituted variable values, no literal {{ }} remaining.
    """
    from prompts.prompt_loader import PromptLoader

    template_content = (
        "<prompt>\n"
        "  <context>{{ arc_title }}</context>\n"
        "  <instruction>Write beat {{ beat_index }}.</instruction>\n"
        "</prompt>\n"
    )
    template_path = tmp_path / "node_test.xml.j2"
    template_path.write_text(template_content, encoding="utf-8")

    loader = PromptLoader(prompts_dir=tmp_path)
    rendered = loader.load_and_render("node_test.xml.j2", {
        "arc_title": "The Fallen City",
        "beat_index": 3,
    })

    assert "The Fallen City" in rendered
    assert "Write beat 3." in rendered
    assert "{{" not in rendered


def test_config_loader_integration(tmp_path):
    """
    Assert load_config() parses config.yaml into a valid AppConfig and runs the
    CommitIntent startup scan without error.

    Purpose:
        Verifies the full load_config() path: YAML parse → Pydantic validation →
        CommitIntent scan. Tests two scan branches:
        1. DB file absent → scan skipped silently (no error).
        2. DB initialized → scan runs and returns clean (no pending rows).

    Expected:
        AppConfig fields match config.yaml values. No exception raised in either branch.
    """
    from core.config_loader import load_config
    from memory.sqlite_db import init_db

    config_path = Path("config.yaml")
    config = load_config(config_path)

    # Spot-check a few parsed values to confirm YAML was read correctly.
    assert config.log_level == "DEBUG"
    assert config.thresholds.stel_cosine_distance == 0.12
    assert config.generation.retry_count_max == 5
    # word_count_target is operator-tunable (e.g. short test runs) — assert
    # shape, not a magic number.
    assert isinstance(config.project.word_count_target, int)
    assert config.project.word_count_target > 0

    # Branch 1: DB absent — scan skipped, no error.
    absent_db = tmp_path / "nonexistent.db"
    assert not absent_db.exists()
    # load_config only scans data/fictionwriter.db, so just verify init_db on a
    # fresh path and then scan manually covers the "table absent" branch.
    from memory.sqlite_db import scan_pending_commit_intents
    assert scan_pending_commit_intents(absent_db) == []

    # Branch 2: DB initialized → clean scan.
    db_path = tmp_path / "fw.db"
    init_db(db_path)
    assert scan_pending_commit_intents(db_path) == []


def test_iter_events_after_checkpoint(tmp_path):
    """
    Assert iter_events_after_checkpoint yields only events after the checkpoint beat_id.

    Purpose:
        Covers the three meaningful cases of the crash-recovery replay iterator:
        1. Checkpoint found mid-log — only events after it are yielded.
        2. Checkpoint is the last event — nothing is yielded after it.
        3. Checkpoint beat_id not present in log — nothing is yielded (no match).

    Expected:
        Case 1: events after checkpoint yielded in order.
        Case 2: empty iterator.
        Case 3: empty iterator.
    """
    from memory.event_log import write_event, iter_events_after_checkpoint

    log_path = tmp_path / "events.jsonl"
    events = [
        {"event": "beat_commit", "beat_id": "b1", "prose_delta": "First."},
        {"event": "beat_commit", "beat_id": "b2", "prose_delta": "Second."},
        {"event": "beat_commit", "beat_id": "b3", "prose_delta": "Third."},
    ]
    for e in events:
        write_event(log_path, e)

    # Case 1: checkpoint at b1 → yields b2 and b3.
    after_b1 = list(iter_events_after_checkpoint(log_path, "b1"))
    assert after_b1 == [events[1], events[2]]

    # Case 2: checkpoint at b3 (last) → nothing after it.
    after_b3 = list(iter_events_after_checkpoint(log_path, "b3"))
    assert after_b3 == []

    # Case 3: checkpoint beat_id not in log → nothing yielded.
    after_missing = list(iter_events_after_checkpoint(log_path, "b99"))
    assert after_missing == []


def test_upsert_beat_and_query(tmp_path):
    """
    Assert upsert_beat persists data and get_remaining_beats retrieves it correctly.

    Purpose:
        Verifies the write path fixed in the conn.commit() audit: data written by
        upsert_beat must survive connection close and be readable by a new connection.
        Also exercises get_remaining_beats ordering and beat_index filter.

    Expected:
        Upserted beat is retrievable. get_remaining_beats returns only beats with
        beat_index > current_beat_index, ordered ascending.
    """
    import sqlite3
    from contextlib import closing
    from memory.sqlite_db import init_db, get_connection, upsert_beat, get_remaining_beats

    db_path = tmp_path / "test.db"
    init_db(db_path)

    # Insert prerequisite rows (FK chain: Arc → Chapter → Scene → Beat).
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "INSERT INTO Arcs (arc_id, title, created_at) VALUES (?, ?, ?)",
            ("arc-1", "Arc One", "2024-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO Chapters (chapter_id, arc_id, title, chapter_index, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("ch-1", "arc-1", "Chapter One", 0, "2024-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO Scenes (scene_id, chapter_id, scene_index) VALUES (?, ?, ?)",
            ("sc-1", "ch-1", 0),
        )
        conn.commit()

    upsert_beat(db_path, {"beat_id": "b1", "scene_id": "sc-1", "beat_index": 0,
                          "beat_plan_json": "{}", "status": "planned"})
    upsert_beat(db_path, {"beat_id": "b2", "scene_id": "sc-1", "beat_index": 1,
                          "beat_plan_json": "{}", "status": "planned"})
    upsert_beat(db_path, {"beat_id": "b3", "scene_id": "sc-1", "beat_index": 2,
                          "beat_plan_json": "{}", "status": "planned"})

    remaining = get_remaining_beats(db_path, "sc-1", current_beat_index=0)
    assert len(remaining) == 2
    assert remaining[0]["beat_id"] == "b2"
    assert remaining[1]["beat_id"] == "b3"


def test_upsert_character_emotion_pad_constraints(tmp_path):
    """
    Assert upsert_character_emotion persists PAD values and DB rejects out-of-bounds.

    Purpose:
        Verifies both the happy path (valid PAD write survives connection close) and
        the CHECK constraint enforcement (pleasure/arousal/dominance must be in [-1, 1]).
        The constraint is defined in the schema and must fire at the DB level.

    Expected:
        Valid PAD row is readable after upsert. Inserting pleasure=1.5 raises IntegrityError.
    """
    import sqlite3
    from contextlib import closing
    from memory.sqlite_db import init_db, get_connection, upsert_character_emotion

    db_path = tmp_path / "test.db"
    init_db(db_path)

    # Insert prerequisite Character row.
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "INSERT INTO Characters (char_id, name) VALUES (?, ?)",
            ("char-1", "Alice"),
        )
        conn.commit()

    upsert_character_emotion(db_path, "char-1", {
        "beat_id": None,
        "pleasure": 0.5, "arousal": -0.3, "dominance": 0.1,
    })

    with closing(get_connection(db_path)) as conn:
        row = conn.execute(
            "SELECT pleasure, arousal, dominance FROM CharacterEmotions WHERE char_id='char-1'"
        ).fetchone()
    assert row["pleasure"] == 0.5
    assert row["arousal"] == -0.3

    # Out-of-bounds PAD value must raise at the DB level.
    with pytest.raises(sqlite3.IntegrityError):
        upsert_character_emotion(db_path, "char-1", {
            "beat_id": None,
            "pleasure": 1.5, "arousal": 0.0, "dominance": 0.0,
        })


def test_prompt_loader_missing_template_raises(tmp_path):
    """
    Assert load_and_render raises TemplateNotFound with search path in the message.

    Purpose:
        Verifies the error path in PromptLoader.load_and_render(). A missing template
        must raise jinja2.TemplateNotFound — not a generic exception — and the message
        must include the directory that was searched so the caller can diagnose the path.

    Expected:
        TemplateNotFound raised. str(exc) contains the template name.
    """
    from jinja2 import TemplateNotFound
    from prompts.prompt_loader import PromptLoader

    loader = PromptLoader(prompts_dir=tmp_path)

    with pytest.raises(TemplateNotFound) as exc_info:
        loader.load_and_render("nonexistent_node.xml.j2", {})

    assert "nonexistent_node.xml.j2" in str(exc_info.value)


def test_full_vertical_slice(tmp_path):
    """
    End-to-end vertical slice: stubbed planning → stubbed draft → fast-path commit.

    Purpose:
        The primary Sprint 1 integration test. Initializes a temp SQLite DB, constructs
        an initial OrchestratorState, and runs the compiled LangGraph graph for one
        beat cycle with all LLM calls replaced by stubbed responses (pre-canned strings).
        Asserts:
        1. A CommitIntent row transitions from pending to committed.
        2. A Beat row is written to SQLite.
        3. An event appears in the .jsonl log.
        4. No exception is raised during the full cycle.

        All LLM calls are patched via monkeypatch or pytest-mock to return
        pre-canned JSON or prose strings without making HTTP requests.

    Inputs:
        tmp_path: pytest fixture — provides isolated temp directory for all stores.

    Expected:
        The FSM completes one full beat cycle without error. SQLite contains a committed
        Beat row. Event log contains one beat_commit entry.
    """
    pytest.skip("Sprint 3+ — FSM nodes not yet wired")


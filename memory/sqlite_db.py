"""
memory/sqlite_db.py

Relational Hub — SQLite ACID Database Manager.

Purpose:
    The ground truth for all structured narrative data. Manages schema creation,
    CRUD operations, and B-Tree index queries for the nine core tables:
    Arcs, Chapters, Scenes, Beats, Threads, Characters, CharacterEmotions,
    CommitIntent, and RaptorNodes.

    Uses raw sqlite3 (not an ORM) for exact query path control and to enforce
    strict CHECK constraints at the database level (e.g., PAD values BETWEEN
    -1.00 AND 1.00, status fields as closed enums). ORM abstractions would mask
    the precise index scan behavior that the B-Tree search algorithm relies on.

    The CommitIntent table implements the intent-record pattern for crash recovery:
    node_commit_transaction writes status='pending' before any writes, then flips
    to status='committed' after all three stores (SQLite, Graphiti, .jsonl) succeed.
    On startup, config_loader.py scans for pending rows indicating a prior crash.

    Scenes table uses an integer `scene_index` column (not `created_at`) for scene
    ordering within a chapter — prevents chronological sort bugs when scenes are
    created in rapid succession.

    RaptorNodes table persists the RAPTOR tree across restarts — the tree is not
    held in-memory only. node_compress_memory writes to this table at chapter boundaries.

Architecture role:
    - Initialized by core/runtime.py via init_db() on startup and reset.
    - Read by node_assemble_context (character states, threads, beat constraints),
      node_adversarial_critics (Threads table for THREAD_PARADOX check),
      edge_commit_router (Beats, Scenes, Chapters tables for advancement routing).
    - Written by all planning nodes (Arc/Chapter/Scene/Beat rows) and
      node_commit_transaction (prose deltas, PAD states, CommitIntent lifecycle).
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS Arcs (
    arc_id      TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    summary     TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS Chapters (
    chapter_id      TEXT PRIMARY KEY,
    arc_id          TEXT NOT NULL REFERENCES Arcs(arc_id),
    title           TEXT NOT NULL,
    chapter_index   INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'planned',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS Scenes (
    scene_id        TEXT PRIMARY KEY,
    chapter_id      TEXT NOT NULL REFERENCES Chapters(chapter_id),
    scene_index     INTEGER NOT NULL,
    prose_text      TEXT,
    word_count      INTEGER NOT NULL DEFAULT 0,
    committed_at    TEXT
);

CREATE TABLE IF NOT EXISTS Beats (
    beat_id         TEXT PRIMARY KEY,
    scene_id        TEXT NOT NULL REFERENCES Scenes(scene_id),
    beat_index      INTEGER NOT NULL,
    beat_plan_json  TEXT,
    status          TEXT NOT NULL DEFAULT 'planned'
);

CREATE TABLE IF NOT EXISTS Threads (
    thread_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    priority    REAL NOT NULL DEFAULT 0.0,
    status      TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS Characters (
    char_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT,
    description TEXT
);

CREATE TABLE IF NOT EXISTS CharacterEmotions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    char_id     TEXT NOT NULL REFERENCES Characters(char_id),
    beat_id     TEXT REFERENCES Beats(beat_id),
    pleasure    REAL NOT NULL CHECK(pleasure >= -1.0 AND pleasure <= 1.0),
    arousal     REAL NOT NULL CHECK(arousal >= -1.0 AND arousal <= 1.0),
    dominance   REAL NOT NULL CHECK(dominance >= -1.0 AND dominance <= 1.0),
    recorded_at TEXT NOT NULL,
    UNIQUE(char_id, beat_id)
);

CREATE TABLE IF NOT EXISTS CommitIntent (
    intent_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    beat_id     TEXT REFERENCES Beats(beat_id),
    status      TEXT NOT NULL CHECK(status IN ('pending', 'committed')),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS RaptorNodes (
    node_id                 TEXT PRIMARY KEY,
    level                   INTEGER NOT NULL,
    summary_text            TEXT,
    source_scene_ids_json   TEXT,
    created_at              TEXT NOT NULL
);
"""


def init_db(db_path: Path) -> None:
    """
    Create all tables with correct schema, constraints, and indexes.

    Purpose:
        Idempotent schema initialization using CREATE TABLE IF NOT EXISTS.
        Defines all nine tables with their CHECK constraints, foreign keys,
        and ordering columns. Called by core/runtime.py on startup and reset.
        Safe to call on an already-initialized database — no data is modified.

    Inputs:
        db_path: Path — absolute path to the SQLite database file
            (e.g., Path("data/fictionwriter.db")).

    Outputs:
        None. Side effect: creates the SQLite file and all tables if they do not exist.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA_SQL)


def get_connection(db_path: Path) -> sqlite3.Connection:
    """
    Return a SQLite connection with row_factory set to sqlite3.Row.

    Purpose:
        Provides a consistent connection factory used by all query functions.
        sqlite3.Row row_factory allows column access by name (row["column"]) in
        addition to index access, improving query result readability across
        all callers.

    Inputs:
        db_path: Path — path to the SQLite database file.

    Outputs:
        sqlite3.Connection — open connection. Caller is responsible for closing
        (or using as a context manager for automatic close on commit/rollback).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def scan_pending_commit_intents(db_path: Path) -> list[dict]:
    """
    Find any CommitIntent rows with status='pending' left by a prior crash.

    Purpose:
        Called by core/config_loader.py at application startup. A pending row
        indicates the process crashed between writing status='pending' and
        flipping to status='committed'. The application startup routine logs
        these rows and flags them for human review or automated replay before
        the FSM resumes — the FSM should not continue past a pending commit
        without resolving the interrupted beat.

    Inputs:
        db_path: Path — path to the SQLite database file.

    Outputs:
        List[dict]: Zero or more dicts representing pending CommitIntent rows.
            Each dict contains: intent_id, beat_id, status, created_at.
            Empty list if no pending rows exist.
    """
    try:
        with closing(get_connection(db_path)) as conn:
            rows = conn.execute(
                "SELECT intent_id, beat_id, status, created_at "
                "FROM CommitIntent WHERE status='pending'"
            ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []
        raise


def upsert_beat(db_path: Path, beat_data: dict) -> None:
    """
    Insert or update a Beat row, keyed by beat_id (idempotent).

    Purpose:
        Used by node_commit_transaction for the SQLite write step of the commit
        sequence. Idempotent by design — replaying a crash recovery .jsonl record
        produces the identical upsert with no duplication. Also used by
        node_plan_beat to write the initial Beat rows during planning.

    Inputs:
        db_path: Path — path to the SQLite database file.
        beat_data: dict — must contain 'beat_id', 'scene_id', 'beat_index',
            'beat_plan_json', 'status'.

    Outputs:
        None. Side effect: inserts or updates one Beat row in the Beats table.
    """
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO Beats (beat_id, scene_id, beat_index, beat_plan_json, status)
            VALUES (:beat_id, :scene_id, :beat_index, :beat_plan_json, :status)
            ON CONFLICT(beat_id) DO UPDATE SET
                beat_index      = excluded.beat_index,
                beat_plan_json  = excluded.beat_plan_json,
                status          = excluded.status
            """,
            beat_data,
        )
        conn.commit()


def upsert_character_emotion(db_path: Path, character_id: str, pad: dict) -> None:
    """
    Insert or update a CharacterEmotions row for one character per beat.

    Purpose:
        Called by node_commit_transaction as part of the SQLite write step.
        PAD states are always committed as part of the beat_commit event (bundled,
        not standalone) to ensure atomic crash recovery replay. The CHECK constraints
        on pleasure, arousal, dominance BETWEEN -1.00 AND 1.00 are enforced at the
        database level — the FSM cannot accidentally write a PAD value outside bounds.

    Inputs:
        db_path: Path — path to the SQLite database file.
        character_id: str — the character whose PAD state is being updated.
        pad: dict — must contain 'beat_id', 'pleasure', 'arousal', 'dominance'.

    Outputs:
        None. Side effect: inserts or updates one CharacterEmotions row.
    """
    recorded_at = datetime.now(timezone.utc).isoformat()
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO CharacterEmotions
                (char_id, beat_id, pleasure, arousal, dominance, recorded_at)
            VALUES (:char_id, :beat_id, :pleasure, :arousal, :dominance, :recorded_at)
            ON CONFLICT(char_id, beat_id) DO UPDATE SET
                pleasure    = excluded.pleasure,
                arousal     = excluded.arousal,
                dominance   = excluded.dominance,
                recorded_at = excluded.recorded_at
            """,
            {
                "char_id": character_id,
                "beat_id": pad.get("beat_id"),
                "pleasure": pad["pleasure"],
                "arousal": pad["arousal"],
                "dominance": pad["dominance"],
                "recorded_at": recorded_at,
            },
        )
        conn.commit()


def get_remaining_beats(db_path: Path, scene_id: str, current_beat_index: int) -> list[dict]:
    """
    Return all Beat rows for a scene with beat_index > current_beat_index.

    Purpose:
        Called by edge_commit_router to determine whether more beats remain in the
        current scene. The query filters by scene_id and beat_index > current value
        so that Tier 3 subdivisions (which add new Beat rows with higher indexes)
        are correctly detected as remaining work.

    Inputs:
        db_path: Path — path to the SQLite database file.
        scene_id: str — the active scene's ID.
        current_beat_index: int — beats with index <= this are already committed.

    Outputs:
        List[dict]: Remaining Beat rows ordered by beat_index ASC. Empty list if
            all beats in the scene are committed.
    """
    with closing(get_connection(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM Beats "
            "WHERE scene_id = ? AND beat_index > ? "
            "ORDER BY beat_index ASC",
            (scene_id, current_beat_index),
        ).fetchall()
    return [dict(row) for row in rows]


def get_remaining_scenes(db_path: Path, chapter_id: str) -> list[dict]:
    """
    Return all Scene rows for a chapter with status != 'completed', ordered by scene_index ASC.

    Purpose:
        Called by edge_commit_router to determine whether more scenes remain in the
        current chapter. Uses the `scene_index` integer column (not created_at) for
        sort stability when scenes were created in rapid succession.

    Inputs:
        db_path: Path — path to the SQLite database file.
        chapter_id: str — the active chapter's ID.

    Outputs:
        List[dict]: Remaining (not completed) Scene rows ordered by scene_index ASC.
            Empty list if all scenes in the chapter are completed.
    """
    with closing(get_connection(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM Scenes "
            "WHERE chapter_id = ? AND committed_at IS NULL "
            "ORDER BY scene_index ASC",
            (chapter_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_remaining_chapters(db_path: Path, arc_id: str) -> list[dict]:
    """
    Return all Chapter rows for an arc with status != 'completed'.

    Purpose:
        Called by edge_commit_router to determine whether more chapters remain in
        the current arc before checking for arc exhaustion.

    Inputs:
        db_path: Path — path to the SQLite database file.
        arc_id: str — the active arc's ID.

    Outputs:
        List[dict]: Remaining (not completed) Chapter rows. Empty list if all
            chapters in the arc are completed.
    """
    with closing(get_connection(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM Chapters "
            "WHERE arc_id = ? AND status != 'completed' "
            "ORDER BY chapter_index ASC",
            (arc_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_total_word_count(db_path: Path) -> int:
    """
    Return SUM(word_count) across all committed Scene rows.

    Purpose:
        Called by edge_commit_router's arc exhaustion check to determine whether
        the manuscript has reached its word_count_target. If all arcs are exhausted
        but this sum is below the target, the router routes back to node_plan_global
        for a continuation arc.

    Inputs:
        db_path: Path — path to the SQLite database file.

    Outputs:
        int: Total committed word count across all scenes.
    """
    with closing(get_connection(db_path)) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(word_count), 0) FROM Scenes WHERE committed_at IS NOT NULL"
        ).fetchone()
    return int(row[0])

"""
core/runtime.py

Application-level resource lifecycle manager.

Purpose:
    Exposes init_resources(config) which initializes all six persistent memory
    stores from scratch: SQLite schema creation, FalkorDB Lite / Graphiti client
    initialization, RAPTOR tree hydration from RaptorNodes table, ChromaDB
    collection setup, style store file creation if absent, and event log path
    verification.

    Also implements the reset sequence invoked by POST /control/reset:
    deletes data/fictionwriter.db, data/graphiti.db (directory), all snapshot ZIPs
    in data/snapshots/, the event log, and all ChromaDB collection data — then
    calls init_resources() to reinitialize everything from scratch. All stores
    are file-based so the reset is a uniform file-deletion operation with no
    server APIs or Docker calls required.

Architecture role:
    - Called once by app.py create_app() at server startup.
    - Called again by routes/control.py POST /control/reset handler at runtime.
    - The reset endpoint must be disabled or access-controlled in production
      deployments — it wipes all manuscript data with no confirmation.
    - All individual memory store initializations (SQLite schema, Graphiti client,
      RAPTOR, Chroma, style stores) are delegated to their respective modules in
      memory/. runtime.py orchestrates the call order but does not contain
      store-specific logic.
"""

import shutil
from pathlib import Path

from core.config_loader import AppConfig
from memory.chroma_client import init_chroma_collections, reset_collections
from memory.graphiti_client import init_graphiti_client
from memory.raptor import init_raptor_tree
from memory.sqlite_db import init_db
from memory.style_store import init_style_store


DATA_DIR = Path("data")
LOGS_DIR = Path("logs")
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
STYLES_DIR = DATA_DIR / "styles"
SQLITE_PATH = DATA_DIR / "fictionwriter.db"
# Legacy FalkorDB Lite data file. The graph now lives in the FalkorDB server
# container (docker-compose.yml); this path remains only so pre-server data
# directories and old snapshot ZIPs stay restorable/cleanable.
GRAPHITI_PATH = DATA_DIR / "graphiti.db"
EVENT_LOG_PATH = DATA_DIR / "event_log.jsonl"
EXPORTS_DIR = DATA_DIR / "exports"


async def init_resources(config: AppConfig) -> None:
    """
    Initialize all persistent memory stores from scratch.

    Purpose:
        Ensures all required directories exist, then initializes each of the six
        persistent stores in dependency order:
        1. SQLite (memory/sqlite_db.py) — schema creation via init_db().
        2. Graphiti + FalkorDB Lite (memory/graphiti_client.py) — client init
           and index/constraint building.
        3. RAPTOR tree (memory/raptor.py) — rehydrated from RaptorNodes SQLite
           table on restart; starts empty on first run.
        4. ChromaDB (memory/chroma_client.py) — collection creation or retrieval.
        5. Style stores (memory/style_store.py) — creates default JSON files if
           absent. Does not overwrite existing profiles.
        6. Event log (memory/event_log.py) — verifies path exists; does not
           truncate existing log on restart (only reset() does that).

    Inputs:
        config: The validated AppConfig from load_config(). Used to pass store
            paths and any store-specific configuration.

    Outputs:
        None. All side effects are file-system and in-memory store initialization.

    Raises:
        Propagates any exception raised by individual store initializers. If any
        store fails to initialize, the exception surfaces immediately and the
        server does not start.
    """
    for directory in (DATA_DIR, LOGS_DIR, SNAPSHOTS_DIR, STYLES_DIR, EXPORTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    init_db(SQLITE_PATH)
    await init_graphiti_client(config)
    init_chroma_collections(DATA_DIR)
    init_raptor_tree(SQLITE_PATH)
    init_style_store(STYLES_DIR)

    # Event log has no init function — ensure the file exists without truncating.
    EVENT_LOG_PATH.touch(exist_ok=True)


async def reset_resources(config: AppConfig) -> None:
    """
    Wipe all runtime data stores and reinitialize from scratch.

    Purpose:
        Implements the development reset utility reachable via POST /control/reset.
        Deletes all file-based runtime data in strict order, then calls
        init_resources() to provide a clean slate without a server restart.

        Deletion targets:
        - data/fictionwriter.db (SQLite database file)
        - data/graphiti.db (FalkorDB Lite directory — removed recursively)
        - data/snapshots/*.zip (all chapter-boundary snapshot archives)
        - data/event_log.jsonl (append-only event ledger)
        - ChromaDB collection data (via chroma_client.reset_collections())
        - data/styles/*.json (style store JSON files)

        All targets are file-based — no server API calls or Docker operations.

    Inputs:
        config: The validated AppConfig. Passed through to init_resources() after
            deletion is complete.

    Outputs:
        None. All side effects are file-system deletions and reinitialization.

    Warning:
        This operation is irreversible and destroys all manuscript data, snapshots,
        and style profiles. Disable or access-control this endpoint before any
        production deployment.
    """
    SQLITE_PATH.unlink(missing_ok=True)

    # FalkorDB Lite may persist data/graphiti.db as either a single file or a
    # directory depending on the driver build — delete whichever form exists so
    # the reset path never raises NotADirectoryError / IsADirectoryError.
    if GRAPHITI_PATH.is_dir():
        shutil.rmtree(GRAPHITI_PATH)
    else:
        GRAPHITI_PATH.unlink(missing_ok=True)

    for snapshot in SNAPSHOTS_DIR.glob("*.zip"):
        snapshot.unlink()

    EVENT_LOG_PATH.unlink(missing_ok=True)

    reset_collections(DATA_DIR)

    for style_file in STYLES_DIR.glob("*.json"):
        style_file.unlink()

    await init_resources(config)
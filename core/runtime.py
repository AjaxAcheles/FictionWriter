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

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.config_loader import AppConfig


DATA_DIR = Path("data")
LOGS_DIR = Path("logs")
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
STYLES_DIR = DATA_DIR / "styles"
SQLITE_PATH = DATA_DIR / "fictionwriter.db"
GRAPHITI_PATH = DATA_DIR / "graphiti.db"
EVENT_LOG_PATH = DATA_DIR / "event_log.jsonl"


def init_resources(config: "AppConfig") -> None:
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
    pass


def reset_resources(config: "AppConfig") -> None:
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
    pass

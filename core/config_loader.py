"""
core/config_loader.py

Validated configuration loader and startup integrity checker.

Purpose:
    Parses config.yaml into a deeply nested Pydantic AppConfig object using
    extra='forbid' on every nested model, so any typo in config.yaml raises a
    fatal ValidationError at boot rather than silently propagating bad values.

    Also performs the CommitIntent startup scan: queries the SQLite CommitIntent
    table for any rows with status='pending'. A pending row indicates the process
    crashed mid-commit. load_config() flags these for human review or automated
    replay before allowing the FSM to resume.

    API key fields in EndpointConfig are overridable via environment variables
    (see .env.example) using pydantic-settings nested env var syntax.

Architecture role:
    - Called once by app.py create_app() and stored in app.config["APP_CONFIG"].
    - node_plan_beat re-reads config at node entry so Settings-page slider changes
      take effect at the next beat boundary without a server restart.
    - The AppConfig object (not the raw YAML dict) is the single source of truth
      for all numeric thresholds and endpoint routing throughout the application.
"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict


class EndpointConfig(BaseModel):
    """
    Per-endpoint LLM inference configuration.

    Purpose:
        Stores the connection parameters, tokenizer family, and output-constraint
        strategy for one named inference role (planner, drafter, critic, etc.).
        Used by call_llm.py for HTTP routing and by node_assemble_context for
        accurate token budget calculation.

    Fields:
        base_url: OpenAI-compatible HTTP base URL for the inference server.
        api_key: Bearer token. Override via .env (see .env.example).
        model_name: Model identifier passed in every request payload.
        supports_inference_antislop: Reserved for endpoints that support native
            antislop inference — currently unused, stub for Sprint 6.
        tokenizer_family: "tiktoken" | "hf_auto" | "char_heuristic". Controls
            which tokenizer node_assemble_context uses for token budget math.
        supports_concurrent_critics: If True, node_adversarial_critics runs all
            3 critics via asyncio.gather. If False, critics run sequentially.
        grammar_constraint_strategy: "gbnf" | "json_mode". Controls how
            node_adversarial_critics enforces FailureObject schema at the sampler.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: str
    model_name: str
    supports_inference_antislop: bool
    tokenizer_family: str
    supports_concurrent_critics: bool
    grammar_constraint_strategy: str


class EndpointsConfig(BaseModel):
    """
    Container for all named endpoint roles.

    Purpose:
        Groups all EndpointConfig instances by role so callers can do
        app_config.endpoints.drafter without dictionary key lookups.

    Fields:
        planner: High-tier model for node_plan_global/arc/chapter.
        drafter: Mid-to-large model for node_draft_prose/node_revise_prose.
        critic: High-tier model for node_adversarial_critics (all 3 calls).
        pad_translator: Fast small model for PAD Grounded Translation Pipeline.
        craft_consultant: Small-to-mid diagnostic model for node_craft_consultant.
    """

    model_config = ConfigDict(extra="forbid")

    planner: EndpointConfig
    drafter: EndpointConfig
    critic: EndpointConfig
    pad_translator: EndpointConfig
    craft_consultant: EndpointConfig


class ThresholdsConfig(BaseModel):
    """
    All numeric thresholds used across the FSM and memory stack.

    Purpose:
        Centralizes every proposed-default numeric constant so they are
        configurable from config.yaml and the Settings UI without Python edits.
        node_plan_beat reads these at node entry; changes take effect on the
        next beat boundary.

    Fields:
        stel_cosine_distance: Dc gate for edge_mode_selector (0–1 range, default 0.12).
        ewma_alpha: PAD emotional EWMA alpha (default 0.35).
        coreference_confidence_floor: Min confidence before MLI fallback (default 0.65).
        passive_voice_density: Passive sentence fraction threshold (default 0.25).
        raptor_cluster_similarity: RAPTOR clustering cosine threshold (Sprint 6, default 0.65).
        voice_evolution_l2_norm_limit: Max L2 drift per character style store (default 0.30).
        programmatic_fast_path_multiplier: Dc multiplier for _programmatic_router (default 0.7).
        beats_per_scene_min: Min committed beats before scene advances (default 2).
    """

    model_config = ConfigDict(extra="forbid")

    stel_cosine_distance: float = 0.12
    ewma_alpha: float = 0.35
    coreference_confidence_floor: float = 0.65
    passive_voice_density: float = 0.25
    raptor_cluster_similarity: float = 0.65
    voice_evolution_l2_norm_limit: float = 0.30
    programmatic_fast_path_multiplier: float = 0.7
    beats_per_scene_min: int = 2


class GenerationConfig(BaseModel):
    """
    Retry and replan escalation caps for the FSM recovery loop.

    Purpose:
        Controls when edge_mode_selector escalates from revision to craft
        consulting to full freeze-and-escalate. All values map to strict
        inequality checks in edge_mode_selector (retry_count > threshold).

    Fields:
        retry_count_max: edge_mode_selector routes to node_freeze_and_escalate
            when retry_count exceeds this value (default 5, so fires at 6+).
        craft_consultant_threshold: Routes to node_craft_consultant when
            retry_count exceeds this value (default 3, so fires at 4–5).
        replan_count_max: node_freeze_and_escalate routes to node_plan_chapter
            instead of node_plan_beat when replan_count exceeds this value (default 2).
    """

    model_config = ConfigDict(extra="forbid")

    retry_count_max: int = 5
    craft_consultant_threshold: int = 3
    replan_count_max: int = 2


class IngestionConfig(BaseModel):
    """
    Non-blocking manuscript ingestion pipeline parameters.

    Purpose:
        Controls the sliding-window chunker in ingestion/pipeline.py.

    Fields:
        sliding_window_tokens: Token window size for overlapping historical
            text chunks during staged NER extraction (default 2000).
    """

    model_config = ConfigDict(extra="forbid")

    sliding_window_tokens: int = 2000


class ProjectConfig(BaseModel):
    """
    Project-level manuscript parameters.

    Purpose:
        Stores the target word count used by the _commit_router arc exhaustion
        check to determine when the manuscript is complete.

    Fields:
        word_count_target: Total target word count for the manuscript (default 300000).
    """

    model_config = ConfigDict(extra="forbid")

    word_count_target: int = 300000


class AppConfig(BaseModel):
    """
    Root configuration object. The single source of truth for all runtime parameters.

    Purpose:
        Parsed from config.yaml by load_config(). extra='forbid' ensures any
        unknown top-level key raises a fatal ValidationError at boot, catching
        typos before any LLM calls are made. Stored in app.config["APP_CONFIG"]
        and passed to subsystems that need routing or threshold values.

    Fields:
        project: Manuscript-level parameters (word_count_target).
        log_level: Logging verbosity for fsm.log (DEBUG/INFO/WARNING/ERROR).
        thresholds: All numeric FSM and memory thresholds.
        generation: Retry and replan escalation caps.
        ingestion: Sliding-window chunker parameters.
        endpoints: Named LLM endpoint configurations by role.
    """

    model_config = ConfigDict(extra="forbid")

    project: ProjectConfig
    log_level: str = "DEBUG"
    thresholds: ThresholdsConfig
    generation: GenerationConfig
    ingestion: IngestionConfig
    endpoints: EndpointsConfig


def load_config(config_path: Path = Path("config.yaml")) -> AppConfig:
    """
    Parse config.yaml into a validated AppConfig and run the CommitIntent startup scan.

    Purpose:
        Single entry point for application configuration. Reads and validates
        config.yaml. Any unknown key or type mismatch raises a Pydantic
        ValidationError with the exact offending field, halting startup cleanly
        before any resource initialization occurs.

        After parsing, scans the SQLite CommitIntent table for pending rows. A
        pending row indicates a crash mid-commit on the previous run. The scan
        result is logged but does not block startup — the FSM checks this
        independently before resuming generation.

    Inputs:
        config_path: Path to config.yaml. Defaults to config.yaml in the
            current working directory.

    Outputs:
        AppConfig: The validated, fully populated configuration object.

    Raises:
        FileNotFoundError: If config.yaml does not exist at config_path.
        pydantic.ValidationError: If any key is unknown or any value fails type
            validation. The error message includes the exact offending field path.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r") as f:
        raw = yaml.safe_load(f)

    config = AppConfig(**raw)

    db_path = Path("data/fictionwriter.db")
    if db_path.exists():
        _scan_commit_intents_at_startup(db_path)

    return config


def _scan_commit_intents_at_startup(db_path: Path) -> None:
    # Uses stdlib sqlite3 (not aiosqlite) because load_config() is called before
    # the async event loop starts — no async context is available yet.
    logger = logging.getLogger(__name__)
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT intent_id, beat_id, created_at FROM CommitIntent WHERE status='pending'"
            ).fetchall()
        if rows:
            logger.warning(
                "CommitIntent startup scan: %d pending row(s) detected — "
                "prior crash suspected. Replay required before FSM resumes. "
                "intent_ids=%s",
                len(rows),
                [r[0] for r in rows],
            )
        else:
            logger.debug("CommitIntent startup scan: clean (no pending rows).")
    except sqlite3.OperationalError:
        # CommitIntent table not yet created (schema not initialized).
        logger.debug("CommitIntent startup scan: table absent, skipping.")

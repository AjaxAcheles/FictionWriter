"""
memory/style_store.py

Voice Baseline Manager — Stylometric Mathematical Profile Store.

Purpose:
    Manages the flat JSON files that store frozen baseline and rolling EMA profiles
    of authorial and character voice. Two metric types are stored per profile:
    1. Burrows' Delta vectors: lexical frequency z-score arrays. Used for advisory
       stylometric drift telemetry (rendered on the UI drift graph). Does NOT gate
       edge_mode_selector routing.
    2. STEL embeddings: 384-dimensional sentence-transformer embeddings. The cosine
       distance (Dc, 0–1) between the draft's STEL embedding and the frozen baseline
       is the sole metric that gates edge_mode_selector routing. Proposed default
       threshold: 0.12 (configurable via config.yaml).

    Files:
    - data/styles/style_author.json: Global manuscript voice. One file. is_frozen=True
      after initial Author Profile Deep Distillation.
    - data/styles/style_char_{id}.json: One file per character ID. Generated when a
      character is introduced.

    Each file is independent: drift detection and the L2-norm Voice Evolution check
    (config.thresholds.voice_evolution_l2_norm_limit, default 0.30) run per-store,
    not on a shared aggregate.

    Voice Evolution: after major arcs, a character's STEL baseline can be updated
    (reflecting genuine character development). The L2-norm of the delta between
    the new evolved baseline and the original frozen baseline must not exceed 0.30.

Architecture role:
    - Read by node_programmatic_audit for Burrows' Delta computation (advisory).
    - Read by edge_mode_selector for STEL Dc calculation (routing gate).
    - Written by node_commit_transaction for rolling EMA updates.
    - Voice Evolution check runs in node_commit_transaction after each arc boundary.
    - Initialized by core/runtime.py — creates default JSON stubs if files are absent.
"""

import json
from pathlib import Path
from typing import Optional

import numpy as np

STYLES_DIR = Path("data/styles")
AUTHOR_STYLE_FILE = STYLES_DIR / "style_author.json"


def init_style_store(styles_dir: Path) -> None:
    """
    Create the styles directory and initialize the author style file if absent.

    Purpose:
        Called by core/runtime.py during init_resources(). Creates data/styles/
        if it does not exist. If style_author.json does not exist, writes a default
        empty profile (is_frozen=False, empty baseline vectors). Does NOT overwrite
        existing profiles — only creates missing files.

    Inputs:
        styles_dir: Path — path to the styles directory (data/styles/).

    Outputs:
        None. Side effect: creates directory and/or default style_author.json.
    """
    pass


def get_author_style(styles_dir: Path) -> dict:
    """
    Load and return the global author style profile.

    Purpose:
        Reads style_author.json and returns the full profile dict. Called by
        node_programmatic_audit (for Burrows' Delta baseline) and edge_mode_selector
        (for STEL cosine distance calculation).

    Inputs:
        styles_dir: Path — path to the styles directory.

    Outputs:
        dict: Author style profile with fields:
            is_frozen (bool), frozen_baseline (dict with 'burrows_delta' and 'stel'
            arrays), rolling_ema (dict with same structure).
    """
    pass


def get_character_style(styles_dir: Path, character_id: str) -> Optional[dict]:
    """
    Load and return a character's style profile, or None if it does not exist.

    Purpose:
        Reads style_char_{character_id}.json. Returns None if the file does not
        exist (character has not yet had a style profile distilled). Called by
        edge_mode_selector for per-character Dc calculation and voice evolution checks.

    Inputs:
        styles_dir: Path — path to the styles directory.
        character_id: str — the character's unique ID.

    Outputs:
        Optional[dict]: Character style profile dict (same structure as author profile),
            or None if the character has no style file yet.
    """
    pass


def update_rolling_ema(
    styles_dir: Path,
    profile_type: str,
    new_burrows_vector: list[float],
    new_stel_embedding: list[float],
    character_id: Optional[str] = None,
    ewma_alpha: float = 0.35,
) -> None:
    """
    Update the rolling EMA profile for one style store file.

    Purpose:
        Called by node_commit_transaction after each beat commit to update the
        rolling EMA (Exponentially Weighted Moving Average) of the voice profile.
        Formula: ema_new = alpha * new_value + (1 - alpha) * ema_old.
        ewma_alpha from config.thresholds.ewma_alpha (default 0.35).

        The frozen_baseline is never modified by this function — it is set once
        during initial distillation and read-only thereafter (except for Voice Evolution
        updates which must pass the L2-norm check).

    Inputs:
        styles_dir: Path — path to the styles directory.
        profile_type: str — "author" or "character".
        new_burrows_vector: List[float] — Burrows' Delta z-score vector for this beat.
        new_stel_embedding: List[float] — 384-dim STEL embedding for this beat.
        character_id: Optional[str] — required when profile_type is "character".
        ewma_alpha: float — EWMA alpha value (default 0.35, from config).

    Outputs:
        None. Side effect: updates rolling_ema in the corresponding JSON file.
    """
    pass


def check_voice_evolution_boundary(
    styles_dir: Path,
    character_id: str,
    proposed_new_baseline: list[float],
    l2_norm_limit: float = 0.30,
) -> bool:
    """
    Check whether a proposed baseline evolution stays within the L2-norm limit.

    Purpose:
        Called before updating a character's frozen_baseline after a major arc.
        Computes the L2-norm distance between the proposed new baseline STEL
        embedding and the original frozen baseline. Returns True if the distance
        is within the limit (evolution is permitted), False if it exceeds the limit
        (evolution is rejected — the character's voice has drifted too far from
        their original baseline, indicating uncontrolled voice degradation).

    Inputs:
        styles_dir: Path — path to the styles directory.
        character_id: str — the character whose baseline would be updated.
        proposed_new_baseline: List[float] — the candidate new STEL embedding.
        l2_norm_limit: float — maximum allowed L2 distance (default 0.30,
            from config.thresholds.voice_evolution_l2_norm_limit).

    Outputs:
        bool: True if L2 distance <= l2_norm_limit (evolution permitted).
              False if L2 distance > l2_norm_limit (evolution rejected).
    """
    pass


def compute_stel_cosine_distance(
    draft_text: str,
    styles_dir: Path,
) -> float:
    """
    Compute the STEL cosine distance (Dc) between draft text and the author baseline.

    Purpose:
        Called by node_programmatic_audit to calculate the stylometric_distance
        field. The returned Dc value is then checked by edge_mode_selector against
        config.thresholds.stel_cosine_distance (default 0.12) to gate routing.

        STUB for Sprints 1–4: returns 0.0 (a perfect stylometric match) to allow
        the routing logic to be tested without live embedding infrastructure.
        Sprint 5+ will replace this with real STEL embedding generation.

    Inputs:
        draft_text: str — the prose text to evaluate.
        styles_dir: Path — path to the styles directory containing the author profile.

    Outputs:
        float: STEL cosine distance, range 0.0–1.0. 0.0 = identical to baseline,
            1.0 = maximally different. Currently returns 0.0 (Sprint 1–4 stub).
    """
    return 0.0

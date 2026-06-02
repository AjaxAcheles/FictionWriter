"""
fsm/nodes/node_programmatic_audit.py

Stage 1 Critic — Model-Free Programmatic Validation.

Purpose:
    Runs fast, compute-cheap Python scripts to catch basic layout errors and
    prose quality issues without burning LLM inference. Three primary checks:

    1. Passive voice density: counts passive sentences via regex. A FailureObject
       with error_code="PASSIVE_VOICE" is emitted ONLY if the fraction of passive
       sentences exceeds config.thresholds.passive_voice_density (default 0.25).
       Density-based, not occurrence-based — a single passive sentence in a 15-
       sentence beat does not trigger a failure.

    2. Stylometric distance: calculates Burrows' Delta (z-score distance between
       the draft's lexical frequency vector and the frozen author style baseline).
       The Burrows' Delta value is written to stylometric_distance for UI telemetry.
       It does NOT trigger a FailureObject — it is advisory only. The STEL cosine
       distance (Dc) gates edge_mode_selector, not Burrows' Delta.
       NOTE: STEL cosine Dc calculation is stubbed at 0.0 for Sprints 1–4.

    3. Timeline/schema checks: regex-based validation of any structural markers
       (e.g., chapter header format, explicit scene breaks) if applicable.

Architecture role:
    - Model-free node — no LLM calls. Pure Python regex and math.
    - Must complete in < 1 second for typical beat lengths.
    - Routes to _programmatic_router (conditional edge) after execution.
    - Appends FailureObjects to critic_failures via the Annotated operator.add reducer.
    - Emits a structured JSON log entry via get_logger("node_programmatic_audit").
"""

import time

from core.logger import get_logger, log_node_event
from fsm.state import FailureObject, OrchestratorState

logger = get_logger("node_programmatic_audit")


async def node_programmatic_audit(state: OrchestratorState) -> dict:
    """
    Run regex and math validation on the current draft text.

    Purpose:
        Scans current_draft_text with model-free Python checks. Constructs
        FailureObject instances for any violations found and returns them for
        appending to the critic_failures list via the operator.add reducer.
        Also computes and returns the stylometric_distance (Burrows' Delta)
        for UI telemetry. Sets stylometric_distance to 0.0 for the STEL cosine
        component (Sprint 1–4 stub).

    Inputs (from OrchestratorState):
        state['current_draft_text']: str — the prose to validate.
        [Reads from memory/style_store.py: frozen author baseline vectors for
         Burrows' Delta calculation]

    Outputs (dict merged into OrchestratorState):
        critic_failures: List[FailureObject] — new failures to append (may be
            empty list if draft is clean). The operator.add reducer appends these
            to the existing list; they do not overwrite previous critic findings.
        stylometric_distance: float — Burrows' Delta score (advisory telemetry).

    Relationships:
        - Triggered by: node_draft_prose (direct edge), or node_revise_prose
          (revision loop — all revised text must pass the full gauntlet again).
        - Routes to: _programmatic_router (edge_programmatic_router conditional edge).
        - No LLM calls.
    """
    pass

"""
fsm/nodes/node_commit_transaction.py

Database Writer Node — Intent-Record Pattern Commit with Idempotent Writes.

Purpose:
    Writes validated prose and state deltas to all three persistent stores using
    the intent-record pattern for recoverable eventual consistency. Write order:
    SQLite → Graphiti → .jsonl (always in this order to allow crash recovery replay).

    Intent-record lifecycle:
    1. Insert CommitIntent row to SQLite with status='pending' (before any writes).
       A process crash after this point leaves a detectable pending row on restart.
    2. Write prose deltas, PAD states (bundled in beat_commit event), and Thread
       updates to SQLite — upsert keyed by beat_id (idempotent).
    3. Stamp temporal edges in Graphiti — upsert keyed by deterministic UUID:
       uuid5(NAMESPACE_OID, f"{entity_a_id}:{entity_b_id}:{edge_type}:{valid_from_event_id}")
    4. Append beat_commit record to .jsonl event log (PAD state bundled inside
       the event payload — never as a standalone event, for atomic crash recovery).
    5. Flip CommitIntent row to status='committed'.
    6. Reset retry_count, replan_count, escalation_tier to 0. Clear has_paradox,
       transient_dc_override, failed_beat_cache. Clear current_draft_text.
    7. Route via edge_commit_router (conditional edge) — queries SQLite to determine
       the next planning node based on remaining beats/scenes/chapters/arcs.

    At chapter boundaries, additional steps fire synchronously before routing:
    - Epistemic Belief promotion: scan committed beat texts (200-token window) to
      confirm, contradict, or persist provisional coreference links.
    - Archive a snapshot ZIP of both data/fictionwriter.db and data/graphiti.db.
      Both are file-based stores — snapshotting is a uniform file-copy/zip operation.
    - Call node_compress_memory synchronously before advancing to node_plan_chapter.

    Voice Evolution check: after each commit, recalculates STEL embedding per
    character style store and validates the L2-norm delta against
    config.thresholds.voice_evolution_l2_norm_limit (default 0.30).

Architecture role:
    - The only node that writes to SQLite, Graphiti, and the event log.
    - All writes are idempotent — crash recovery replays from the last chapter
      snapshot + .jsonl records forward with no risk of duplicate entries.
    - Emits a structured JSON log entry via get_logger("node_commit_transaction").
"""

import time
from pathlib import Path
from uuid import uuid5, NAMESPACE_OID

from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState

logger = get_logger("node_commit_transaction")


async def node_commit_transaction(state: OrchestratorState) -> dict:
    """
    Write validated beat data to all persistent stores using the intent-record pattern.

    Purpose:
        Implements the full commit sequence described in the module docstring.
        Handles both beat-boundary and chapter-boundary commit paths. The chapter
        boundary path includes Epistemic Belief promotion, DB snapshot archiving,
        and a synchronous call to node_compress_memory before advancing.

    Inputs (from OrchestratorState):
        state['current_draft_text']: str — the validated prose to commit.
        state['fsm_pointer']: FSM_Pointer — identifies the beat being committed.
        state['critic_failures']: List[FailureObject] — should be empty at commit time.
        [Reads from SQLite: provisional Graphiti coreference links at chapter boundaries]
        [Reads from AppConfig: thresholds.voice_evolution_l2_norm_limit]

    Outputs (dict merged into OrchestratorState):
        fsm_pointer: FSM_Pointer — advanced to the next beat/scene/chapter position.
        retry_count: int — reset to 0.
        replan_count: int — reset to 0.
        escalation_tier: int — reset to 0.
        has_paradox: bool — reset to False.
        transient_dc_override: None — cleared.
        failed_beat_cache: List — cleared to [].
        current_draft_text: str — cleared to "".
        critic_failures: List — cleared to [].

    Relationships:
        - Triggered by: edge_mode_selector (clean draft), or edge_programmatic_router
          (fast path bypass).
        - Routes to: determined by edge_commit_router querying SQLite.
        - Calls: node_compress_memory synchronously at chapter boundaries.
        - Side effects: writes to SQLite, Graphiti, .jsonl. Archives snapshot ZIP.
    """
    pass

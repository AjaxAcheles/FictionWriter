"""
fsm/state.py

LangGraph FSM state schema definitions.

Purpose:
    Defines the three core data structures that flow through the entire FSM:
    1. OrchestratorState — the LangGraph state TypedDict with Annotated reducers.
    2. FSM_Pointer — a Pydantic BaseModel tracking the active position in the
       hierarchical narrative structure (arc → chapter → scene → beat).
    3. FailureObject — the strict Pydantic schema the LLM critics are constrained
       to return, used for revision targeting and escalation routing.

    The Annotated[List[FailureObject], operator.add] reducer on critic_failures is
    the critical LangGraph contract: it instructs the graph to append new failure
    objects to the existing list rather than overwriting it when multiple critic
    nodes return partial results. Without this, each critic call would clobber the
    previous critic's findings.

Architecture role:
    - Imported by every node_*.py file and every router in fsm/routers/.
    - OrchestratorState is the single argument type and return type for all node
      functions (nodes return a dict subset that gets merged into this state).
    - FailureObject is instantiated by node_programmatic_audit and
      node_adversarial_critics; consumed by node_revise_prose and edge_mode_selector.
"""

import operator
from typing import Annotated, Dict, List, Optional

from pydantic import BaseModel
from typing_extensions import TypedDict


class FSM_Pointer(BaseModel):
    """
    Tracks the current position of the FSM within the narrative hierarchy.

    Purpose:
        A lightweight value object updated by planning nodes and node_commit_transaction
        as the FSM advances through the story structure. Included in every structured
        log entry (fsm.log) as a snapshot of where the FSM was when a node executed.

    Fields:
        arc_id: ID of the active Arc row in SQLite.
        chapter_id: ID of the active Chapter row in SQLite.
        scene_id: ID of the active Scene row in SQLite.
        beat_index: Zero-based index of the current beat within the active scene.
    """

    arc_id: str
    chapter_id: str
    scene_id: str
    beat_index: int


class FailureObject(BaseModel):
    """
    Strict schema for LLM critic and programmatic audit failure reports.

    Purpose:
        The exact structure that node_adversarial_critics is grammar-constrained
        to produce, and that node_programmatic_audit constructs programmatically.
        Uses verbatim text targeting (offending_text as a quoted substring) rather
        than integer character spans — LLMs cannot reliably count raw characters,
        so integer spans produce misaligned coordinates that corrupt the draft.

        node_revise_prose locates offending_text via str.find() first, then fuzzy
        match if exact search fails, then falls back to full-beat rewrite if the
        substring cannot be found (e.g., the model hallucinated the span).

    Fields:
        error_code: Categorical fault identifier. Known values:
            "PASSIVE_VOICE"     — density-based passive sentence check.
            "CONTINUITY_BREAK"  — factual contradiction with established narrative state.
            "THREAD_PARADOX"    — draft contradicts an open Thread's required resolution.
                                  Sets has_paradox=True; bypasses retry gate in edge_mode_selector.
            "PACING_ISSUE"      — scene rhythm, tension curve, or word-budget violation.
            "DIALOGUE_FLAT"     — dialogue-specific voice or subtext failure.
        offending_text: Short verbatim quoted substring of the problematic passage.
        suggested_fix: Replacement text or corrective instruction for node_revise_prose.
        critic_source: Origin of this failure. One of "continuity", "dialogue",
            "pacing", or "programmatic".
    """

    error_code: str
    offending_text: str
    suggested_fix: str
    critic_source: str


class OrchestratorState(TypedDict):
    """
    The central nervous system of the LangGraph FSM.

    Purpose:
        Passed between every node and edge in the graph. LangGraph merges node
        return dicts into this state — fields not returned by a node retain their
        current values. The Annotated[List[...], operator.add] reducers on
        critic_failures and failed_beat_cache instruct LangGraph to append new
        items rather than overwrite existing ones when nodes return partial lists.

    Fields:
        project_id: Stable identifier for the active project/manuscript.
        fsm_pointer: Current position in the narrative hierarchy (FSM_Pointer).
        active_context_package: Assembled prompt payload dict built by
            node_assemble_context. Overwritten completely on each assembly pass.
        current_draft_text: The prose being actively drafted or revised.
            Overwritten by node_draft_prose and node_revise_prose.
            Cleared to "" by node_commit_transaction on successful commit.
        streaming_buffer: Tracks the live HTTP stream for the antislop interface.
            Written incrementally by node_draft_prose as chunks arrive.
        critic_failures: Accumulated list of FailureObject instances from the
            current revision cycle. operator.add reducer — appends, never overwrites.
            Cleared by node_revise_prose before re-entering the audit gauntlet.
            Also cleared by node_commit_transaction on successful commit.
        stylometric_distance: STEL cosine distance (0–1) from the most recent
            programmatic audit. Used by edge_mode_selector (Dc gate) and
            _programmatic_router (fast-path gate). Stubbed at 0.0 for Sprints 1–4.
        retry_count: Number of revision attempts for the current beat. Incremented
            by node_revise_prose. Reset to 0 by node_commit_transaction and by
            node_freeze_and_escalate Tier 4.
        replan_count: Number of Tier 4 scrap-and-replan attempts for the current
            beat. Capped at generation.replan_count_max (default 2). Incremented by
            node_freeze_and_escalate Tier 4. Reset to 0 by node_commit_transaction.
        escalation_tier: Current active tier inside node_freeze_and_escalate (1–4).
            Prevents any tier from repeating on re-entry to the escalation node.
            Reset to 0 by node_commit_transaction.
        has_paradox: Set True by node_adversarial_critics when a THREAD_PARADOX
            FailureObject is emitted. Checked by edge_mode_selector BEFORE the
            retry_count elif chain — routes immediately to node_freeze_and_escalate
            regardless of retry_count. Reset to False by node_commit_transaction.
        transient_dc_override: Written by node_freeze_and_escalate Tier 1 to
            temporarily relax the STEL cosine distance threshold for one recovery
            attempt. edge_mode_selector reads this before the config threshold.
            None when not active. Cleared on successful commit or tier advancement.
        pause_requested: Set True when the user clicks Pause in the UI, or as a
            last resort by node_freeze_and_escalate after all four tiers fail.
            node_human_intervention checks this flag to unlock the UI editor.
        hard_stop_asserted: Set True when the user clicks Hard Stop. The FSM halts
            at the next safe boundary and does not resume automatically.
        failed_beat_cache: Scene-scoped list of failure fingerprint dicts for
            node_freeze_and_escalate autonomous recovery. Includes THREAD_PARADOX
            fingerprints. Preserved across retry_count resets (Tier 4). Stores
            compact fingerprints only (not full prose text). Cleared by
            node_commit_transaction on successful commit.
    """

    project_id: str
    fsm_pointer: FSM_Pointer
    active_context_package: Dict
    current_draft_text: str
    streaming_buffer: str
    critic_failures: Annotated[List[FailureObject], operator.add]
    stylometric_distance: float
    retry_count: int
    replan_count: int
    escalation_tier: int
    has_paradox: bool
    transient_dc_override: Optional[float]
    pause_requested: bool
    hard_stop_asserted: bool
    failed_beat_cache: Annotated[List[Dict], operator.add]

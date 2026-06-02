"""
fsm/nodes/node_human_intervention.py

Optional Human Override Node — UI Pause/Resume Interrupt Handler.

Purpose:
    Handles optional manual author intervention via the Quart UI. The FSM is
    designed for fully autonomous operation — this node is NEVER required and the
    FSM never blocks waiting for it in normal operation. It is triggered only when:
    1. The user explicitly clicks "Pause" in the Live Command Center UI.
    2. hard_stop_asserted is True (user clicked "Hard Stop").
    3. As a last resort from node_freeze_and_escalate after all four autonomous
       recovery tiers fail and replan_count is exhausted.

    When active, it unlocks the Quart UI editor, allowing the author to:
    - Manually edit the current prose in the text editor.
    - Patch configuration parameters via the Settings page.
    - Issue a rollback or branch command (handled by branch_manager.py).
    - Resume autonomous generation from the patched state.

    The node yields to node_assemble_context to rebuild the prompt with any
    patched state, or directly to node_programmatic_audit if the user only
    edited prose text (context package is still valid).

    Human oversight improves output quality when applied, but its absence never
    blocks or degrades the FSM's autonomous operation.

Architecture role:
    - Available as a LangGraph interrupt() target from almost any node (triggered
      by pause_requested or hard_stop_asserted flags being checked in node logic).
    - Also registered as a direct node in graph.py for the last-resort escalation path.
    - Reads from an async UI queue (populated by the Quart /control/patch endpoint)
      to receive author inputs without blocking the event loop.
    - Emits a structured JSON log entry via get_logger("node_human_intervention").
"""

import time

from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState

logger = get_logger("node_human_intervention")


async def node_human_intervention(state: OrchestratorState) -> dict:
    """
    Wait for and apply author input from the Quart UI editor queue.

    Purpose:
        Checks pause_requested and hard_stop_asserted flags. If hard_stop_asserted,
        returns a terminal state without resuming. If pause_requested, polls the async
        UI input queue for author patches. Applies patches to the appropriate state
        fields (current_draft_text for prose edits, fsm_pointer for rollbacks/branches).
        Clears pause_requested after applying the patch.

        If the author provides a "Reasons" directive for a branch restore, the directive
        text is injected into active_context_package so the planning nodes autonomously
        steer the story in a new direction on resume. Branch file operations are
        delegated to memory/branch_manager.py.

    Inputs (from OrchestratorState):
        state['pause_requested']: bool — triggers the UI unlock.
        state['hard_stop_asserted']: bool — triggers terminal halt.
        state['current_draft_text']: str — may be overwritten by author prose edit.
        state['fsm_pointer']: FSM_Pointer — may be overwritten by rollback/branch.
        [Reads from async Quart UI queue: author patch payloads]

    Outputs (dict merged into OrchestratorState):
        pause_requested: bool — cleared to False after patch is applied.
        current_draft_text: str — overwritten if author edited prose.
        fsm_pointer: FSM_Pointer — overwritten if author issued rollback/branch.
        active_context_package: dict — "branch_reason" key added if Reasons directive given.

    Relationships:
        - Triggered by: LangGraph interrupt() from any node, or direct edge from
          node_freeze_and_escalate (last resort).
        - Yields to: node_assemble_context (if context needs rebuilding after patch),
          or node_programmatic_audit (if author only edited prose text).
    """
    pass

"""
fsm/nodes/node_human_intervention.py

Manual Override Interface with Post-Edit Reconciliation (full Sprint 4).

Purpose:
    Optional manual override — the FSM never blocks waiting for a human. When
    pause_requested or hard_stop_asserted is set, this node consumes any
    pending human action from the module-level intervention queue (pushed by
    the Quart routes in Sprint 5):

    - Prose edit {"action": "edit", "text": ...}: overwrites current_draft_text,
      then runs the post-edit autonomous reconciliation pass — an LLM extraction
      call diffs the new draft against open Threads and applies status updates
      so node_adversarial_critics does not emit a spurious THREAD_PARADOX on
      legitimately intended plot changes. The extraction is retried once; on a
      second failure a WARNING is logged and HUMAN_EDIT_UNRECONCILED is appended
      to the .jsonl audit log without blocking FSM progression.
    - Rollback/branch {"action": "rollback", "fsm_pointer": {...}}: overwrites
      the fsm_pointer (the caller has already restored the snapshot via
      memory/branch_manager.py).
    - No pending action: clears pause_requested and returns (hard stop is
      observed and left intact — the FSM halts at the boundary).

Architecture role:
    - Last-resort yield target of node_freeze_and_escalate; also reachable via
      pause from the UI. Prose edits yield to node_programmatic_audit;
      rollbacks yield to node_assemble_context (graph edge — Sprint 5 wiring).
"""

import json
import time
from typing import List

from pydantic import BaseModel, ConfigDict

from core import runtime
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.state import FSM_Pointer, OrchestratorState
from llm import call_llm as call_llm_module
from memory import sqlite_db
from memory.event_log import write_event

logger = get_logger("node_human_intervention")

# Module-level queue: routes push {"action": ..., ...}; the node consumes FIFO.
_intervention_queue: list[dict] = []


def push_intervention(action: dict) -> None:
    """Queue a human action for the next node_human_intervention pass."""
    _intervention_queue.append(action)


class ThreadUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    thread_id: str
    new_status: str  # "open" | "closed"
    rationale: str = ""


class ReconciliationResult(BaseModel):
    """Extraction output: thread status updates implied by the human edit."""

    model_config = ConfigDict(extra="ignore")

    thread_updates: List[ThreadUpdate] = []


async def reconcile_human_edit(draft: str, config) -> ReconciliationResult:
    """
    LLM extraction pass: diff the edited draft against open Threads.

    Retried once by the caller; raises on failure so the caller can apply the
    HUMAN_EDIT_UNRECONCILED policy.
    """
    threads = sqlite_db.get_open_threads(runtime.SQLITE_PATH)
    messages = [
        {
            "role": "user",
            "content": (
                "A human author manually edited the draft below. Compare it against the "
                "open subplot threads and report any thread whose required resolution is "
                "now satisfied or contradicted by the edit. Respond with JSON: "
                '{"thread_updates": [{"thread_id": ..., "new_status": "open"|"closed", '
                '"rationale": ...}]}. Empty list if nothing changed.\n\n'
                f"OPEN THREADS:\n{json.dumps(threads)}\n\nEDITED DRAFT:\n{draft}"
            ),
        }
    ]
    return await call_llm_module.call_llm_structured(
        config.endpoints.planner,
        messages,
        ReconciliationResult,
        retry_cap=config.model_validate_retry_cap,
    )


async def node_human_intervention(state: OrchestratorState) -> dict:
    """
    Apply a pending human action (if any) and reconcile database ground truth.

    Outputs (merged into OrchestratorState): action-dependent — see module
    docstring.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    config = load_config()

    try:
        action = _intervention_queue.pop(0) if _intervention_queue else None

        if action is None:
            update = {"pause_requested": False}

        elif action.get("action") == "rollback":
            update = {
                "fsm_pointer": FSM_Pointer(**action["fsm_pointer"]),
                "pause_requested": False,
                "critic_failures": None,
                "current_draft_text": "",
            }

        elif action.get("action") == "edit":
            new_draft = action.get("text", "")
            reconciled = False
            for attempt in range(2):
                try:
                    result = await reconcile_human_edit(new_draft, config)
                    from fsm.nodes.node_plan_arc import ThreadEvent, apply_thread_event
                    for tu in result.thread_updates:
                        apply_thread_event(
                            runtime.SQLITE_PATH,
                            ThreadEvent(
                                thread_id=tu.thread_id,
                                event="close" if tu.new_status == "closed" else "open",
                            ),
                        )
                    reconciled = True
                    logger.info(
                        "post-edit reconciliation applied %d thread update(s).",
                        len(result.thread_updates),
                    )
                    break
                except Exception as e:  # noqa: BLE001 — retry-once policy
                    logger.warning("post-edit reconciliation attempt %d failed: %r", attempt + 1, e)
            if not reconciled:
                write_event(
                    runtime.EVENT_LOG_PATH,
                    {
                        "type": "HUMAN_EDIT_UNRECONCILED",
                        "scene_id": pointer.scene_id,
                        "beat_index": pointer.beat_index,
                        "note": "post-edit reconciliation failed twice; beat flagged for review",
                    },
                )
            update = {
                "current_draft_text": new_draft,
                "pause_requested": False,
                "critic_failures": None,  # re-audit from a clean slate
            }
        else:
            logger.warning("unknown intervention action: %r", action)
            update = {"pause_requested": False}

        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return update
    except Exception as e:
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e))
        raise

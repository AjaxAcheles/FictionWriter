"""
core/stream_bus.py

Process-local SSE event fanout bus.

Purpose:
    Decouples FSM nodes (producers) from the Quart /stream SSE endpoint
    (consumers). node_draft_prose publishes token chunks and lifecycle events;
    each connected SSE client owns one asyncio.Queue subscription and receives
    every published event. Publishing never blocks the FSM: queues are bounded
    and events are dropped (oldest-client-suffers) when a slow consumer's queue
    is full — generation latency is never gated on UI consumption.

Architecture role:
    - publish() is called from FSM nodes (node_draft_prose chunk relay,
      node_adversarial_critics reasoning stream, commit notifications).
    - subscribe()/unsubscribe() are called by routes/dashboard.stream() per
      SSE connection.
    - Process-local only: a single Quart process serves the UI. No cross-process
      delivery is required (or attempted).
"""

import asyncio
from datetime import datetime, timezone
from typing import Dict

_MAX_QUEUE_SIZE = 1024

_subscribers: Dict[int, asyncio.Queue] = {}
_next_id = 0

# ---------------------------------------------------------------------------
# Live reattach snapshot.
#
# A page that reloads mid-draft can rebuild committed prose from
# GET /codex/manuscript, but the IN-FLIGHT beat (the block currently streaming
# tokens) lives only in transient OrchestratorState and would be lost. publish()
# is the single chokepoint every SSE event passes through, so we passively
# reconstruct the active beat + its accumulated draft buffer + a short tail of
# engine log lines here. GET /status reads live_snapshot() to reattach.
# Process-local, same as the bus itself.
# ---------------------------------------------------------------------------

_MAX_LIVE_LOGS = 10
# kind tags mirror the dashboard.js glassLine() palette so replayed lines keep
# their colour. Only event types worth narrating in the Glass Engine are kept.
_LOG_KINDS = {"pipeline_status": "reflect", "planning": "reflect", "status": "revise"}


def _empty_live() -> dict:
    return {
        "active_beat": None,   # {beat_id, beat_index, description, scene_id}
        "draft_text": "",      # accumulated draft_chunk text for the active beat
        "beat_state": None,    # drafting | revising | escalated
        "recent_logs": [],     # last _MAX_LIVE_LOGS {ts, text, kind}
    }


_live: dict = _empty_live()


def reset_live() -> None:
    """Clear the reattach snapshot. Called by generation_manager.start()."""
    global _live
    _live = _empty_live()


def live_snapshot() -> dict:
    """A copy of the current reattach state for GET /status."""
    return {
        "active_beat": dict(_live["active_beat"]) if _live["active_beat"] else None,
        "draft_text": _live["draft_text"],
        "beat_state": _live["beat_state"],
        "recent_logs": list(_live["recent_logs"]),
    }


def _record(event: dict) -> None:
    """Update the reattach snapshot from one published event (best-effort)."""
    et = event.get("type")
    if et == "beat_start":
        _live["active_beat"] = {
            "beat_id": event.get("beat_id"),
            "beat_index": event.get("beat_index"),
            "description": event.get("description", ""),
            "scene_id": event.get("scene_id", ""),
        }
        _live["draft_text"] = ""
        _live["beat_state"] = "drafting"
    elif et == "draft_chunk":
        if _live["active_beat"]:
            _live["draft_text"] += event.get("text", "")
    elif et == "draft_complete":
        if _live["active_beat"]:
            _live["beat_state"] = "revising"
    elif et == "draft_replaced":
        bid = event.get("beat_id")
        if bid:
            ab = _live["active_beat"] or {}
            ab["beat_id"] = bid
            _live["active_beat"] = ab
        _live["draft_text"] = event.get("text", "")
        _live["beat_state"] = "revising"
    elif et == "beat_committed":
        # Committed prose rehydrates from /codex/manuscript; drop the live block
        # so a reattach doesn't show it twice.
        ab = _live["active_beat"]
        if ab and ab.get("beat_id") == event.get("beat_id"):
            _live["active_beat"] = None
            _live["draft_text"] = ""
            _live["beat_state"] = None
    elif et == "pipeline_status" and event.get("stage") == "node_freeze_and_escalate":
        if _live["active_beat"]:
            _live["beat_state"] = "escalated"

    kind = _LOG_KINDS.get(et)
    if kind is not None:
        text = event.get("label") or event.get("text")
        if text:
            _live["recent_logs"].append(
                {"ts": datetime.now(timezone.utc).isoformat(), "text": text, "kind": kind}
            )
            del _live["recent_logs"][:-_MAX_LIVE_LOGS]


def subscribe() -> tuple[int, asyncio.Queue]:
    """
    Register a new SSE consumer.

    Outputs:
        (subscriber_id, queue): the ID is the unsubscribe handle; the queue
        yields event dicts as published.
    """
    global _next_id
    _next_id += 1
    queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
    _subscribers[_next_id] = queue
    return _next_id, queue


def unsubscribe(subscriber_id: int) -> None:
    """Remove a consumer; safe to call twice."""
    _subscribers.pop(subscriber_id, None)


def publish(event: dict) -> None:
    """
    Fan an event dict out to all subscribers without blocking.

    Purpose:
        Non-blocking delivery: if a consumer's queue is full the event is
        silently dropped for that consumer only. The FSM never awaits UI
        consumption.

    Inputs:
        event: dict — JSON-serializable payload, e.g.
            {"type": "draft_chunk", "text": "..."} or {"type": "beat_committed", ...}.
    """
    _record(event)
    for queue in _subscribers.values():
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


def subscriber_count() -> int:
    """Number of active SSE consumers (used by tests and diagnostics)."""
    return len(_subscribers)

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
from typing import Dict

_MAX_QUEUE_SIZE = 1024

_subscribers: Dict[int, asyncio.Queue] = {}
_next_id = 0


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
    for queue in _subscribers.values():
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


def subscriber_count() -> int:
    """Number of active SSE consumers (used by tests and diagnostics)."""
    return len(_subscribers)

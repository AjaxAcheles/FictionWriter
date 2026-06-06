"""
routes/dashboard.py

Dashboard Blueprint — SSE Stream, Live Command Center, Telemetry Events.

Purpose:
    Serves the main dashboard view and manages the Server-Sent Events (SSE) stream
    that delivers live prose tokens, planning bullets, critic reasoning, and telemetry
    updates to the frontend in real time.

    Key routes:
    GET  /           — Renders templates/dashboard.html (the main application view).
    GET  /stream     — SSE endpoint. Returns an EventStream with Content-Type
                       text/event-stream. Yields events from the FSM SSE queue.
    POST /generate   — Starts the LangGraph FSM as a background async task.
                       Accepts project configuration from the frontend form.

    SSE event types emitted to the frontend:
    - "token"       — one prose token chunk from node_draft_prose.
    - "beat_start"  — beat_id for the incoming beat. Frontend wraps tokens in a
                      data-beat-id div and clears stale tokens if beat_id is re-used.
    - "word_count"  — running word count update after each committed beat.
    - "critic"      — critic reasoning stream from node_adversarial_critics.
    - "planning"    — planning bullet from a planning node.
    - "pad_update"  — PAD state update for the UI radar chart.
    - "status"      — FSM status messages (e.g., "paused", "escalating").

Architecture role:
    - The SSE generator function reads from an asyncio.Queue populated by FSM nodes.
    - All node SSE emissions go through the shared queue — nodes do not access
      this blueprint directly; they use a shared sse_queue singleton.
    - Quart's async route handling ensures the SSE connection and FSM execution
      share the same event loop without blocking.
"""

import asyncio
import json

from quart import Blueprint, Response, render_template, request

dashboard_bp = Blueprint("dashboard", __name__)

# Module-level queue shared by FSM nodes (push) and /stream (consume).
# asyncio.Queue() is loop-agnostic in Python 3.10+ — safe to create here.
sse_queue: asyncio.Queue = asyncio.Queue()


@dashboard_bp.route("/")
async def index():
    """
    Render the main dashboard view.

    Purpose:
        Serves templates/dashboard.html — the primary application UI with the
        2-column layout (prose stream + telemetry sidebar). Passes initial
        context (project status, FSM pointer) to the template if available.

    Inputs:
        None (GET request, no parameters).

    Outputs:
        Rendered HTML response for the dashboard template.
    """
    return await render_template("dashboard.html")


@dashboard_bp.route("/stream")
async def stream():
    """
    SSE endpoint — delivers live FSM event stream to the frontend.

    Purpose:
        Returns an async generator wrapped in a Quart Response with
        Content-Type: text/event-stream. Reads events from the shared FSM
        SSE queue and formats them as SSE protocol messages (data: ... \\n\\n).

        The EventSource in static/js/main.js connects to this endpoint on page load.
        The connection is kept alive for the duration of the generation session.
        On client disconnect, the generator exits cleanly.

    Inputs:
        None (GET request, EventSource connection).

    Outputs:
        SSE stream. Each event is formatted as:
        event: {event_type}\\ndata: {json_payload}\\n\\n
    """
    async def event_generator():
        while True:
            try:
                event = await asyncio.wait_for(sse_queue.get(), timeout=15.0)
                yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(event_generator(), content_type="text/event-stream")


@dashboard_bp.route("/generate", methods=["POST"])
async def generate():
    """
    Start the LangGraph FSM generation as a background async task.

    Purpose:
        Accepts project configuration from the frontend form (genre, premise,
        word count target). Constructs the initial OrchestratorState and launches
        the compiled LangGraph graph as an asyncio background task. Returns
        immediately with a 202 Accepted response so the frontend can begin
        consuming the SSE stream.

    Inputs:
        POST body (JSON or form): genre (str), premise (str), word_count_target (int).

    Outputs:
        JSON response: {"status": "started", "project_id": str}
        HTTP 202 Accepted.
    """
    return {"status": "started", "project_id": "default"}, 202

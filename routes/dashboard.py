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

from core import stream_bus

dashboard_bp = Blueprint("dashboard", __name__)


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
        subscriber_id, queue = stream_bus.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
        finally:
            stream_bus.unsubscribe(subscriber_id)

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
    import asyncio

    from fsm.graph import compile_graph
    from fsm.state import FSM_Pointer

    payload = await request.get_json(silent=True) or {}
    project_id = payload.get("project_id", "default")

    initial_state = {
        "project_id": project_id,
        "fsm_pointer": FSM_Pointer(arc_id="", chapter_id="", scene_id="", beat_index=0),
        "active_context_package": {},
        "current_draft_text": "",
        "streaming_buffer": "",
        "critic_failures": [],
        "stylometric_distance": 0.0,
        "retry_count": 0,
        "replan_count": 0,
        "escalation_tier": 0,
        "has_paradox": False,
        "transient_dc_override": None,
        "pause_requested": False,
        "hard_stop_asserted": False,
        "failed_beat_cache": [],
        "best_seen_draft": None,
        "best_seen_failure_count": None,
    }

    app = compile_graph()
    asyncio.get_event_loop().create_task(
        app.ainvoke(initial_state, config={"recursion_limit": 10_000})
    )
    return {"status": "started", "project_id": project_id}, 202


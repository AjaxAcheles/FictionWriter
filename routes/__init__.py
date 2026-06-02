"""
routes/__init__.py

Quart async HTTP blueprint package.

Purpose:
    Contains all server-side route handlers for the FictionWriter web UI. Each module
    defines one Quart Blueprint registered by app.py. All route handler functions are
    async def, sharing the Quart event loop with the LangGraph FSM execution.

    Blueprints:
    dashboard.py  — SSE stream, word_count events, beat_start events, Live Command Center.
    alignment.py  — Non-blocking Alignment Dashboard: coreference Claim Card review UI.
    settings.py   — config.yaml editor routes and endpoint connectivity testing.
    control.py    — Graceful pause, hard stop, and POST /control/reset (dev utility).
    codex.py      — Memory Codex: Characters, Threads, Branches explorer with Kanbans.
"""

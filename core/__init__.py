"""
core/__init__.py

Centralized infrastructure utilities package.

Purpose:
    Houses cross-cutting concerns shared by every layer of the application:
    configuration loading, structured logging, resource lifecycle management,
    and the antislop black-box interface. Nothing in this package contains
    narrative or FSM logic — it is pure infrastructure.

Exports:
    config_loader  — Pydantic-validated AppConfig loader with CommitIntent scan.
    logger         — Structured JSON node logger and LLM IO logger.
    runtime        — Application-level resource lifecycle (init, reset).
    antislop       — Stubbed detect_slop / resolve_slop interface.
"""

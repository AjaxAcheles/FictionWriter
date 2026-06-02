"""
tests/__init__.py

Pytest test suite for the FictionWriter FSM orchestration system.

Purpose:
    Houses all automated tests for the application. Pytest is configured via
    pyproject.toml to discover files matching test_*.py in this directory.

    Test suite overview:
    test_config.py          — Pydantic validation failure states (extra keys, type errors).
    test_smoke.py           — Baseline FSM routing smoke tests (happy path trace).
    test_tokenizer.py       — Token counting math against tiktoken, HF, and char_heuristic.
    test_branch_manager.py  — SQLite + FalkorDB Lite ZIP snapshot decompression idempotency.
    test_sprint1.py         — Sprint 1 integration test suite: vertical slice end-to-end.

    All tests use pytest-asyncio for async test functions (LangGraph nodes are async).
    No mocking of SQLite or Graphiti — tests use real in-memory or temp-file stores
    to prevent mock/prod divergence.
"""

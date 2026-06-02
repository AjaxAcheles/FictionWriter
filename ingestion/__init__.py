"""
ingestion/__init__.py

Non-blocking asynchronous manuscript ingestion pipeline.

Purpose:
    Handles importing existing manuscripts into the FictionWriter knowledge stores
    without blocking the FSM drafting loop or requiring human prompts. The ingestion
    pipeline runs as an async background process outside the LangGraph synchronous
    execution loop.

    Modules:
    pipeline.py      — Coordinates sliding-window chunking (configurable window size)
                       and orchestrates staged NER extraction across all chunks.
    coreference.py   — Staged NER extraction (proper names first, then pronouns) with
                       Maximum Likelihood Imputation for low-confidence coreference links.
                       Generates provisional coreference links written to Graphiti.
"""

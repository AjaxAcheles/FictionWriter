"""
ingestion/pipeline.py

Manuscript Ingestion Pipeline Coordinator — Sliding Window Chunker.

Purpose:
    Coordinates the import of existing manuscript text into the FictionWriter
    knowledge stores. Reads historical text in overlapping sliding windows
    (window size configurable via config.ingestion.sliding_window_tokens, default 2000)
    to ensure continuity across chunk boundaries. Each chunk is passed to
    coreference.py for staged NER extraction.

    The pipeline is non-blocking and asynchronous — it runs as a background async
    task outside the LangGraph drafting loop so it does not block prose generation.
    The FSM can continue generating new content while historical content is being
    ingested in parallel.

    Output: provisional coreference links written to Graphiti with confidence scores.
    node_assemble_context handles these via the three-tier confidence system:
    - High confidence: injected as absolute fact.
    - Mid confidence: injected as Epistemic Beliefs (flagged as unconfirmed in prompt).
    - Low confidence: excluded entirely.

    node_commit_transaction resolves mid-confidence Epistemic Beliefs at chapter
    boundaries via the 200-token window heuristic. The Alignment Dashboard Claim Cards
    UI provides optional (non-blocking) human review of provisional links.

Architecture role:
    - Triggered by routes/alignment.py when the user uploads a manuscript file.
    - Runs as an asyncio background task — does not block the Quart event loop.
    - Writes provisional coreference data to Graphiti via memory/graphiti_client.py.
    - Writes character and entity data to SQLite via memory/sqlite_db.py.
"""

from pathlib import Path
from typing import AsyncIterator, Optional

from core.config_loader import AppConfig


async def ingest_manuscript(
    manuscript_path: Path,
    config: AppConfig,
    project_id: str,
) -> None:
    """
    Import a manuscript file into the knowledge stores using sliding window chunking.

    Purpose:
        Reads the manuscript file, chunks it into overlapping windows of
        config.ingestion.sliding_window_tokens tokens each, and processes each
        chunk through the coreference pipeline. Runs as a background async task.
        Progress is emitted as SSE events to the Alignment Dashboard UI.

        Overlapping windows (50% overlap between consecutive chunks) ensure that
        entities mentioned near the end of one chunk and the beginning of the next
        are correctly linked, even when the chunk boundary splits a sentence.

    Inputs:
        manuscript_path: Path — path to the uploaded manuscript file (plain text).
        config: AppConfig — used for sliding_window_tokens and endpoint configuration.
        project_id: str — the target project ID for writing entities to SQLite.

    Outputs:
        None. Side effects: writes character, entity, and provisional coreference
        link data to SQLite and Graphiti.
    """
    pass


async def _chunk_text(
    text: str,
    window_tokens: int,
    tokenizer_family: str,
    overlap_fraction: float = 0.5,
) -> AsyncIterator[str]:
    """
    Yield overlapping text chunks from the input manuscript text.

    Purpose:
        Splits the input text into overlapping windows of approximately window_tokens
        tokens each, with overlap_fraction overlap between consecutive windows (default
        50%). Overlap ensures entity continuity across chunk boundaries.

        Uses the tokenizer_family routing from llm/tokenizer.py for accurate chunk sizing.
        The last chunk may be shorter than window_tokens if the text doesn't divide evenly.

    Inputs:
        text: str — the full manuscript text to chunk.
        window_tokens: int — target size of each chunk in tokens.
        tokenizer_family: str — tokenizer to use for size estimation.
        overlap_fraction: float — fraction of the window to overlap (default 0.5 = 50%).

    Outputs:
        AsyncIterator[str]: Yields one text chunk per iteration in document order.
    """
    pass

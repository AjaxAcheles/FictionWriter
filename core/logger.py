"""
core/logger.py

Structured JSON logging utilities for all LangGraph nodes and LLM IO.

Purpose:
    Provides two shared logging utilities:
    1. get_logger(node_name) / log_node_event() — structured JSON FSM node logger
       that writes to logs/fsm.log via a RotatingFileHandler (10MB, 5 backups).
    2. get_llm_io_logger() / log_llm_call() — dedicated LLM request/response logger
       that writes to logs/llm_io.log via a RotatingFileHandler (50MB, 3 backups).

    Both loggers write one JSON object per line. The FSM logger records node-level
    outcomes (node name, fsm_pointer snapshot, duration, outcome, error). The LLM
    IO logger records inference boundary traffic (full request payload, assembled
    response text, duration_ms) for debugging and prompt auditing.

    Neither logger writes to stdout or the SSE stream — those are separate channels.

Architecture role:
    - Every node_*.py calls get_logger(node_name) at module level and calls
      log_node_event() at the end of each execution with wall-clock duration and
      outcome. This is a mandatory contract for all FSM nodes.
    - call_llm.py calls get_llm_io_logger() and log_llm_call() after each
      inference request completes. Per-token streaming chunks are NOT logged;
      only the final assembled response text is recorded.
    - Log level is read from AppConfig.log_level (set in config.yaml).
"""

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


def get_logger(node_name: str) -> logging.Logger:
    """
    Return a structured JSON logger bound to a specific LangGraph node name.

    Purpose:
        Creates (or retrieves from cache) a Logger that writes JSON-formatted
        entries to logs/fsm.log. Uses a RotatingFileHandler capped at 10MB with
        5 backup files. The formatter outputs the raw message string, which
        log_node_event() pre-serializes as JSON.

        Idempotent — multiple calls with the same node_name return the same
        Logger instance without adding duplicate handlers.

    Inputs:
        node_name: The canonical LangGraph node name string, e.g.
            "node_adversarial_critics". Used as the logger name and included
            in every log entry's "node" field.

    Outputs:
        logging.Logger: A configured Logger instance. Callers pass this to
            log_node_event() to emit structured entries.
    """
    pass


def log_node_event(
    logger: logging.Logger,
    fsm_pointer: dict,
    duration_ms: float,
    outcome: str,
    error: Optional[str] = None,
) -> None:
    """
    Emit one structured JSON log entry for a completed LangGraph node execution.

    Purpose:
        Serializes node execution metadata into a single JSON line and writes it
        to logs/fsm.log via the provided logger. Called by every node_* function
        at the end of its execution, regardless of success or failure.

    Inputs:
        logger: The Logger returned by get_logger(node_name). Its .name field
            is used as the "node" value in the JSON payload.
        fsm_pointer: A dict snapshot of the current FSM_Pointer fields
            (arc_id, chapter_id, scene_id, beat_index) at time of logging.
        duration_ms: Wall-clock execution time for this node in milliseconds.
        outcome: One of "success", "failure", or "escalated".
        error: Optional error message string if outcome is "failure" or
            "escalated". None if outcome is "success".

    Outputs:
        None. Writes one line to logs/fsm.log.
    """
    pass


def get_llm_io_logger() -> logging.Logger:
    """
    Return the shared LLM request/response logger.

    Purpose:
        Creates (or retrieves from cache) a Logger that writes JSON-formatted
        entries to logs/llm_io.log. Uses a RotatingFileHandler capped at 50MB
        with 3 backup files. Separate from the FSM node logger — this file
        captures the raw inference boundary traffic for debugging and latency
        analysis, not FSM-level outcomes.

        Idempotent — repeated calls return the same Logger instance.

    Inputs:
        None.

    Outputs:
        logging.Logger: A configured Logger instance. Callers pass this to
            log_llm_call() after each inference request completes.
    """
    pass


def log_llm_call(
    logger: logging.Logger,
    request_payload: dict,
    response_text: str,
    duration_ms: float,
) -> None:
    """
    Emit one structured JSON log entry for a completed LLM inference call.

    Purpose:
        Serializes the full request payload, the final assembled response text
        (NOT per-token chunks), and wall-clock duration into one JSON line and
        writes it to logs/llm_io.log. Called by call_llm.py after the streaming
        response is fully assembled.

    Inputs:
        logger: The Logger returned by get_llm_io_logger().
        request_payload: The full dict sent to the inference endpoint — includes
            messages, model name, temperature, max_tokens, grammar constraint.
        response_text: The complete assembled response string after all streaming
            chunks are concatenated. Per-token chunks are not logged.
        duration_ms: Wall-clock time from request send to final token received,
            in milliseconds.

    Outputs:
        None. Writes one line to logs/llm_io.log.
    """
    pass

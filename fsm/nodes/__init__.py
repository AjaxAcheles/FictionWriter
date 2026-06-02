"""
fsm/nodes/__init__.py

LangGraph FSM node functions package.

Purpose:
    Each module in this package defines one async LangGraph node function.
    Nodes are the execution units of the FSM: they receive OrchestratorState,
    perform their specific work (LLM calls, DB reads/writes, Python computation),
    and return a dict of state fields to be merged back into OrchestratorState.

    All node functions are async def. All emit a structured JSON log entry via
    get_logger(node_name) from core/logger.py at the end of every execution.
    All load LLM prompts exclusively from prompts/ via PromptLoader — no prompt
    text is hardcoded in any Python file.
"""

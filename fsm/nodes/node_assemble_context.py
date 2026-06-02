"""
fsm/nodes/node_assemble_context.py

Context Assembly Node — Multi-Database Retrieval and Token-Budget Pruning.

Purpose:
    The retrieval orchestrator. Executes parallel database queries across all four
    persistent stores (SQLite B-Tree, Graphiti subgraph traversal, RAPTOR semantic
    tree, ChromaDB HNSW) to build the context package that will be injected into
    the prose generation prompt.

    Applies drop-priority token pruning to fit the assembled context within the
    active endpoint's context window. Drop order (lowest priority first):
    1. ChromaDB HNSW flavor context (dropped first)
    2. RAPTOR scene-level summaries
    3. RAPTOR chapter-level summaries (retained last; arc summary always included)

    Token counting uses per-endpoint tokenizer routing (EndpointConfig.tokenizer_family):
    - "tiktoken": cl100k_base encoding (OpenAI-compatible endpoints).
    - "hf_auto": transformers.AutoTokenizer for HuggingFace-served models.
    - "char_heuristic": character_count ÷ 4 as last-resort fallback.

    Coreference confidence tiers when querying Graphiti:
    - High confidence: injected as absolute fact.
    - Mid confidence: injected as Epistemic Beliefs (explicitly flagged as unconfirmed).
    - Low confidence: excluded entirely.

Architecture role:
    - Model-free programmatic node — no LLM calls. Pure DB queries and math.
    - Overwrites active_context_package completely on each execution.
    - From Sprint 3 onward, executes live queries against SQLite and Graphiti.
      RAPTOR and Graphiti reads are never stubbed; they use real query methods
      against persisted data stores from the moment this node is wired in.
    - Emits a structured JSON log entry via get_logger("node_assemble_context").
"""

import time

from core.logger import get_logger, log_node_event
from fsm.state import OrchestratorState

logger = get_logger("node_assemble_context")


async def node_assemble_context(state: OrchestratorState) -> dict:
    """
    Query all memory stores and build the token-budget-constrained context package.

    Purpose:
        Reads fsm_pointer to scope all DB queries to the current narrative position.
        Executes queries against SQLite (characters, threads, PAD states, beat
        constraints), Graphiti (temporal entity graph, coreference links),
        RAPTOR (arc and chapter summaries), and ChromaDB (associative flavor vectors).
        Assembles results into active_context_package and applies drop-priority
        pruning until the payload fits within the endpoint's token budget.

    Inputs (from OrchestratorState):
        state['fsm_pointer']: FSM_Pointer — scopes all DB queries.
        [Reads EndpointConfig.tokenizer_family from AppConfig for token counting]
        [Queries: SQLite, Graphiti, RAPTOR, ChromaDB]

    Outputs (dict merged into OrchestratorState):
        active_context_package: Dict — overwrites the previous package completely.
            Contains: beat_constraints, character_states, thread_statuses,
            graphiti_facts, raptor_summaries, hnsw_flavor, epistemic_beliefs.

    Relationships:
        - Triggered by: node_plan_beat (direct edge), or node_human_intervention.
        - Yields to: node_draft_prose (via direct edge in graph.py).
        - No LLM calls. Pure computation and DB reads.
    """
    pass

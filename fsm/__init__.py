"""
fsm/__init__.py

LangGraph Finite State Machine domain package.

Purpose:
    Contains all components of the deterministic FSM that orchestrates the
    fiction generation pipeline. Organized into three sub-packages:
    - state.py: OrchestratorState TypedDict, FSM_Pointer, FailureObject schemas.
    - graph.py: LangGraph StateGraph wiring — nodes, edges, conditional routers.
    - routers/: Conditional edge functions (edge_mode_selector, _commit_router,
      _programmatic_router) decoupled from nodes to prevent circular imports.
    - nodes/: All async LangGraph node functions, one file per node.

    No business logic lives in this __init__.py. Consumers import directly from
    the sub-modules (e.g., from fsm.state import OrchestratorState).
"""

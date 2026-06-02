"""
fsm/routers/__init__.py

Conditional edge functions for the LangGraph FSM.

Purpose:
    Houses the three conditional edge functions that LangGraph calls to determine
    the next node after certain transitions. Decoupled from the node files to
    prevent circular imports (nodes import state; routers import state and may
    import nodes; graph.py imports both).

    Routers:
    - edge_mode_selector: Primary quality gate after node_adversarial_critics.
    - edge_commit_router: Dynamic relational router after node_commit_transaction.
    - edge_programmatic_router: Fast-path bypass after node_programmatic_audit.
"""

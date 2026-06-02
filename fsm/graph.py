"""
fsm/graph.py

LangGraph StateGraph compiler — wires all nodes and conditional edges together.

Purpose:
    Constructs the compiled LangGraph application by declaring all nodes,
    adding all directed edges, and mapping conditional edges to their router
    functions. The resulting compiled graph is the runnable FSM that drives
    the entire fiction generation pipeline.

    Node declaration order in the graph does not imply execution order —
    execution order is determined entirely by edges. The START → node_plan_global
    edge defines the single entry point; the Export Pipeline terminal state
    defines the exit.

    The Fallback_Subgraph (node_craft_consultant and node_freeze_and_escalate)
    is wired as a set of regular nodes within the main graph — LangGraph does not
    require a literal subgraph construct here. Their isolation is logical, not
    structural.

Architecture role:
    - compile_graph() is called once at application startup (or on demand during
      testing) and the returned compiled app is stored for reuse.
    - The graph is stateless between invocations — all mutable state lives in
      OrchestratorState, not in the graph object itself.
    - Conditional edge routing functions live in fsm/routers/ to prevent circular
      imports between node files and the graph wiring.
"""

from langgraph.graph import StateGraph, START, END

from fsm.state import OrchestratorState
from fsm.nodes.node_plan_global import node_plan_global
from fsm.nodes.node_plan_arc import node_plan_arc
from fsm.nodes.node_plan_chapter import node_plan_chapter
from fsm.nodes.node_plan_beat import node_plan_beat
from fsm.nodes.node_assemble_context import node_assemble_context
from fsm.nodes.node_draft_prose import node_draft_prose
from fsm.nodes.node_programmatic_audit import node_programmatic_audit
from fsm.nodes.node_adversarial_critics import node_adversarial_critics
from fsm.nodes.node_revise_prose import node_revise_prose
from fsm.nodes.node_commit_transaction import node_commit_transaction
from fsm.nodes.node_compress_memory import node_compress_memory
from fsm.nodes.node_craft_consultant import node_craft_consultant
from fsm.nodes.node_freeze_and_escalate import node_freeze_and_escalate
from fsm.nodes.node_human_intervention import node_human_intervention
from fsm.routers.edge_mode_selector import edge_mode_selector
from fsm.routers.edge_commit_router import edge_commit_router
from fsm.routers.edge_programmatic_router import edge_programmatic_router


def compile_graph():
    """
    Construct and compile the complete LangGraph FSM.

    Purpose:
        Declares all nodes and edges in the fiction generation state machine,
        then compiles the graph into a runnable LangGraph application. The
        compiled app exposes .invoke() and .astream() for synchronous and
        async execution respectively.

        Edge topology summary:
        - START → node_plan_global
        - node_plan_global → node_plan_arc
        - node_plan_arc → node_plan_chapter
        - node_plan_chapter → node_plan_beat
        - node_plan_beat → node_assemble_context
        - node_assemble_context → node_draft_prose
        - node_draft_prose → node_programmatic_audit
        - node_programmatic_audit → [_programmatic_router] → node_adversarial_critics | node_commit_transaction
        - node_adversarial_critics → [edge_mode_selector] → node_commit_transaction | node_revise_prose | node_craft_consultant | node_freeze_and_escalate
        - node_revise_prose → node_programmatic_audit  (revision loop back)
        - node_craft_consultant → node_revise_prose
        - node_freeze_and_escalate → [tier routing] → node_revise_prose | node_plan_beat | node_plan_chapter | node_human_intervention
        - node_commit_transaction → [edge_commit_router] → node_plan_beat | node_plan_chapter | node_plan_arc | node_plan_global | END
        - node_compress_memory → node_plan_chapter
        - node_human_intervention → node_assemble_context | node_programmatic_audit

    Inputs:
        None.

    Outputs:
        A compiled LangGraph application (CompiledStateGraph). Call .invoke()
        with an initial OrchestratorState dict to run the FSM synchronously,
        or .astream() to run it asynchronously and yield state deltas.
    """
    pass

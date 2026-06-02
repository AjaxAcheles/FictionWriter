"""
memory/__init__.py

Persistent memory stack package — the six storage systems of the hybrid memory architecture.

Purpose:
    Each module in this package manages one of the six persistent stores that allow the
    FSM to scale to 300K+ words without context rot. The stores are organized by their
    search algorithm and injection target:

    sqlite_db.py      — Relational hub. B-Tree deterministic search. Injection: Planners.
    graphiti_client.py — Temporal knowledge graph (Graphiti + FalkorDB Lite).
                         Chronologically bounded subgraph traversal. Injection: Continuity critic.
    raptor.py         — Hierarchical semantic summaries (RAPTOR tree).
                         Top-down semantic tree traversal. Injection: Prose Drafter.
    event_log.py      — Append-only .jsonl audit ledger. Linear backward scan.
                         Injection: Audit and crash recovery only.
    style_store.py    — Voice baseline JSON matrices. Dual-metric stylometric search.
                         Injection: edge_mode_selector, Voice Evolution Monitor.
    chroma_client.py  — HNSW vector index (ChromaDB). Approximate nearest neighbor.
                         Injection: Prose Drafter (associative flavor only).
    branch_manager.py — Not a store itself. Orchestrates O(1) snapshot decompression
                         for branching and .jsonl replay for crash recovery.
"""

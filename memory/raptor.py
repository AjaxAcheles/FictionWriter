"""
memory/raptor.py

RAPTOR Hierarchical Semantic Summary Tree Manager.

Purpose:
    Manages the RAPTOR (Recursive Abstractive Processing for Tree-Organized Retrieval)
    tree that provides macro-level narrative context to the prose drafter without
    overflowing the model's token limits. The tree is a recursive hierarchy:
    - Leaf nodes: committed scene texts.
    - Chapter nodes: cluster summaries of scenes within one chapter.
    - Arc nodes: summaries of chapter clusters within one arc.
    - Global node: the top-level manuscript summary.

    The tree is persisted to the RaptorNodes SQLite table (not held in-memory only)
    and survives application restarts. node_compress_memory writes to this table at
    every chapter boundary, blocking until consolidation is complete.

    Staleness policy: mid-chapter beats intentionally read the prior chapter's RAPTOR
    summary. This is accepted, by-design staleness — the blocking synchronous execution
    of node_compress_memory prevents write races but does not change mid-chapter reads.

    cluster_scenes() is a PLACEHOLDER for Sprint 1–5. It returns scene_texts unmodified.
    The full implementation (embed → cosine similarity matrix → threshold graph at
    config.thresholds.raptor_cluster_similarity → Leiden Algorithm → summarize clusters)
    is a Sprint 6 drop-in. node_compress_memory iterates the return value identically
    for both the stub and the real implementation — no changes to the caller are required.

Architecture role:
    - Queried by node_assemble_context via get_raptor_summaries() for context injection.
    - Written by node_compress_memory via write_raptor_node() at chapter boundaries.
    - Initialized by core/runtime.py via init_raptor_tree() on startup (rehydrates
      from RaptorNodes SQLite table) and reset (starts with empty tree).
"""

import json
from pathlib import Path
from typing import Optional


def cluster_scenes(scene_texts: list[str]) -> list[str]:
    """
    Cluster scene texts into semantic groups for RAPTOR summarization. PLACEHOLDER.

    Purpose:
        The clustering step of the RAPTOR tree building process. Receives a list of
        committed scene texts for a chapter and returns grouped clusters for
        summarization. node_compress_memory calls this function and iterates the
        result to generate chapter-level summaries.

        CURRENT STATUS: Sprint 1–5 placeholder. Returns scene_texts unmodified
        (each scene is its own "cluster" of one). The return type is identical for
        both the stub and the real implementation — node_compress_memory requires
        no changes when Sprint 6 replaces this function.

        FULL IMPLEMENTATION (Sprint 6 drop-in):
        1. Embed each scene text using a sentence transformer model.
        2. Build a cosine similarity matrix across all embeddings.
        3. Threshold the similarity graph at config.thresholds.raptor_cluster_similarity
           (proposed default 0.65).
        4. Apply Leiden Algorithm community detection to find semantic clusters.
        5. Return a list of strings, one per cluster (scenes in a cluster concatenated).

    Inputs:
        scene_texts: List[str] — committed scene texts for one chapter.

    Outputs:
        List[str]: Clustered scene text groups — one concatenated string per
            semantic cluster, deterministic ordering (clusters ordered by their
            lowest member index; members joined in document order).

    Sprint 6 implementation (self-contained, no igraph dependency):
        1. Embed each scene with the deterministic hashed-trigram embedding
           shared with the ChromaDB client (no model download, fully offline).
        2. Cosine similarity matrix via numpy.
        3. Threshold the similarity graph at
           config.thresholds.raptor_cluster_similarity (default 0.65).
        4. Leiden-style community detection: greedy local moving over the
           thresholded weighted graph, followed by a refinement pass that splits
           internally-disconnected communities (the property Leiden guarantees
           over Louvain).
    """
    if len(scene_texts) <= 1:
        return list(scene_texts)

    import numpy as np

    from memory.chroma_client import _hashed_trigram_embedding

    try:
        from core.config_loader import load_config

        threshold = load_config().thresholds.raptor_cluster_similarity
    except Exception:  # config unavailable in isolated unit tests
        threshold = 0.65

    vectors = np.array([_hashed_trigram_embedding(t) for t in scene_texts], dtype=float)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    unit = vectors / norms
    similarity = unit @ unit.T

    n = len(scene_texts)
    adjacency = np.where(similarity >= threshold, similarity, 0.0)
    np.fill_diagonal(adjacency, 0.0)

    labels = _leiden_communities(adjacency)

    clusters: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        clusters.setdefault(label, []).append(index)
    ordered = sorted(clusters.values(), key=lambda members: members[0])
    return ["\n\n".join(scene_texts[i] for i in members) for members in ordered]


def _leiden_communities(adjacency) -> list[int]:
    """
    Leiden-style community detection on a weighted adjacency matrix.

    Local moving phase: greedily move each node to the neighboring community
    with the highest positive modularity gain until no move improves. Refinement
    phase: split any community whose induced subgraph is internally disconnected
    (the well-connectedness guarantee distinguishing Leiden from Louvain).
    Deterministic: nodes are visited in index order.
    """
    import numpy as np

    n = adjacency.shape[0]
    labels = list(range(n))
    total_weight = adjacency.sum() / 2.0
    if total_weight == 0.0:
        return labels  # no edges above threshold — every scene is a singleton
    degrees = adjacency.sum(axis=1)

    improved = True
    sweeps = 0
    while improved and sweeps < 10:
        improved = False
        sweeps += 1
        for node in range(n):
            current = labels[node]
            best_label, best_gain = current, 0.0
            neighbor_labels = {labels[j] for j in range(n) if adjacency[node, j] > 0.0}
            for candidate in sorted(neighbor_labels):
                if candidate == current:
                    continue
                # Modularity gain of moving `node` into `candidate`.
                k_in_new = sum(adjacency[node, j] for j in range(n) if labels[j] == candidate)
                k_in_old = sum(
                    adjacency[node, j] for j in range(n) if labels[j] == current and j != node
                )
                sum_new = sum(degrees[j] for j in range(n) if labels[j] == candidate)
                sum_old = sum(degrees[j] for j in range(n) if labels[j] == current and j != node)
                gain = (k_in_new - k_in_old) / total_weight - degrees[node] * (
                    sum_new - sum_old
                ) / (2.0 * total_weight**2)
                if gain > best_gain + 1e-12:
                    best_gain, best_label = gain, candidate
            if best_label != current:
                labels[node] = best_label
                improved = True

    # Refinement: split internally-disconnected communities via BFS components.
    next_label = max(labels) + 1
    for community in sorted(set(labels)):
        members = [i for i in range(n) if labels[i] == community]
        if len(members) <= 1:
            continue
        unvisited = set(members)
        components = []
        while unvisited:
            seed = min(unvisited)
            stack, component = [seed], {seed}
            unvisited.discard(seed)
            while stack:
                node = stack.pop()
                for j in list(unvisited):
                    if adjacency[node, j] > 0.0:
                        unvisited.discard(j)
                        component.add(j)
                        stack.append(j)
            components.append(sorted(component))
        for extra in components[1:]:
            for i in extra:
                labels[i] = next_label
            next_label += 1
    return labels


def init_raptor_tree(db_path: Path) -> dict:
    """
    Rehydrate the RAPTOR tree from the RaptorNodes SQLite table on startup.

    Purpose:
        Called by core/runtime.py during init_resources(). Reads all existing
        RaptorNode rows from SQLite and builds an in-memory tree representation
        for fast traversal by node_assemble_context. On first run (no existing nodes),
        returns an empty tree dict. Does not write any data.

    Inputs:
        db_path: Path — path to the SQLite database file containing RaptorNodes table.

    Outputs:
        dict: In-memory tree representation keyed by node ID. Each value is a dict
            with: id, parent_id, level, summary, updated_at. parent_id is decoded
            from source_scene_ids_json (the table has no parent_id column).
    """
    import sqlite3
    from contextlib import closing

    from memory.sqlite_db import get_connection

    if not Path(db_path).exists():
        return {}
    try:
        with closing(get_connection(db_path)) as conn:
            rows = conn.execute(
                "SELECT node_id, level, summary_text, source_scene_ids_json, created_at "
                "FROM RaptorNodes"
            ).fetchall()
    except sqlite3.OperationalError:
        return {}  # RaptorNodes table absent (schema not initialized yet)

    tree: dict = {}
    for row in rows:
        try:
            meta = json.loads(row["source_scene_ids_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        tree[row["node_id"]] = {
            "id": row["node_id"],
            "parent_id": meta.get("parent_id"),
            "level": _INT_TO_LEVEL.get(row["level"], row["level"]),
            "summary": row["summary_text"],
            "updated_at": row["created_at"],
        }
    return tree


def write_raptor_node(
    db_path: Path,
    node_id: str,
    parent_id: Optional[str],
    level: str,
    summary: str,
) -> None:
    """
    Insert or update one RaptorNode row in the SQLite RaptorNodes table.

    Purpose:
        Called by node_compress_memory to persist a generated summary into the
        RAPTOR tree. Uses upsert (INSERT OR REPLACE) keyed by node_id for
        idempotency — replaying from a crash snapshot does not create duplicate nodes.
        level must be one of: 'beat', 'scene', 'chapter', 'arc', 'global'.

    Inputs:
        db_path: Path — path to the SQLite database file.
        node_id: str — unique identifier for this RAPTOR node.
        parent_id: Optional[str] — ID of the parent node (None for global root).
        level: str — hierarchy level ('beat'|'scene'|'chapter'|'arc'|'global').
        summary: str — the LLM-generated summary text for this cluster.

    Outputs:
        None. Side effect: inserts or updates one RaptorNodes row.

    Note:
        The RaptorNodes table has no parent_id column; parent linkage is encoded
        inside source_scene_ids_json as {"parent_id": ..., "scene_ids": [...]}.
        Callers that need scene provenance should pass it via write_raptor_node_full.
    """
    write_raptor_node_full(db_path, node_id, parent_id, level, summary, scene_ids=[])


_LEVEL_TO_INT = {"beat": 0, "scene": 1, "chapter": 2, "arc": 3, "global": 4}
_INT_TO_LEVEL = {v: k for k, v in _LEVEL_TO_INT.items()}


def write_raptor_node_full(
    db_path: Path,
    node_id: str,
    parent_id: Optional[str],
    level: str,
    summary: str,
    scene_ids: list[str],
) -> None:
    """write_raptor_node with explicit scene provenance (Sprint 3)."""
    if level not in _LEVEL_TO_INT:
        raise ValueError(f"write_raptor_node: unknown level {level!r}")
    from contextlib import closing
    from datetime import datetime, timezone

    from memory.sqlite_db import get_connection

    with closing(get_connection(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO RaptorNodes (node_id, level, summary_text, source_scene_ids_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                level = excluded.level,
                summary_text = excluded.summary_text,
                source_scene_ids_json = excluded.source_scene_ids_json
            """,
            (
                node_id,
                _LEVEL_TO_INT[level],
                summary,
                json.dumps({"parent_id": parent_id, "scene_ids": scene_ids}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def get_raptor_summaries(
    db_path: Path,
    scene_id: str,
    levels: list[str],
) -> dict:
    """
    Retrieve RAPTOR summary nodes relevant to the current scene for context injection.

    Purpose:
        Called by node_assemble_context to fetch multi-level RAPTOR summaries for
        the active scene. Executes a top-down semantic tree traversal: evaluates
        the active scene intent via cosine similarity at the root, walks down matching
        parent branches, and extracts relevant intermediate summaries at the requested
        hierarchy levels.

        Sprint 1–4: returns empty summaries (RAPTOR tree is empty until chapter
        boundaries are reached). From Sprint 3 onward, reads real data from SQLite.

    Inputs:
        db_path: Path — path to the SQLite database file.
        scene_id: str — the active scene's ID, used to scope the traversal.
        levels: List[str] — which hierarchy levels to retrieve (e.g., ['chapter', 'arc']).

    Outputs:
        dict: Summaries keyed by level (e.g., {'chapter': '...', 'arc': '...'}).
            Missing levels have empty string values.

    Note:
        Sprint 3 implementation: most-recent summary per requested level (the
        semantic tree traversal with cosine gating arrives with real embeddings
        in Sprint 5+). Reads live RaptorNodes data — never stubbed.
    """
    from contextlib import closing

    from memory.sqlite_db import get_connection

    result = {level: "" for level in levels}
    with closing(get_connection(db_path)) as conn:
        for level in levels:
            level_int = _LEVEL_TO_INT.get(level)
            if level_int is None:
                continue
            row = conn.execute(
                "SELECT summary_text FROM RaptorNodes WHERE level = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (level_int,),
            ).fetchone()
            if row and row[0]:
                result[level] = row[0]
    return result

"""
memory/chroma_client.py

HNSW Vector Index Client — ChromaDB Associative Flavor Context.

Purpose:
    Manages interactions with the ChromaDB HNSW vector index, used exclusively for
    injecting associative flavor into the prose generation context: sensory callbacks,
    atmospheric imagery, and localized prose exemplars from earlier chapters.

    This store is NOT used for factual continuity (that's Graphiti) or structural
    planning (that's SQLite/RAPTOR). It is a supplementary "flavor layer" that enriches
    prose generation with thematic associations. Because it is the lowest-priority
    context layer, it is the first to be dropped during token budget pruning (both in
    node_assemble_context's drop-priority sequence and in node_freeze_and_escalate
    Tier 2 Context Stripping).

    The ChromaDB collection is file-based and initialized by core/runtime.py. Reset
    (POST /control/reset) deletes all collection data via reset_collections() then
    recreates empty collections via init_chroma_collections().

Architecture role:
    - Queried by node_assemble_context for flavor vectors using HNSW approximate
      nearest neighbor search against the active scene's intent embedding.
    - Dropped entirely by node_freeze_and_escalate Tier 2 when the FSM is in
      structural recovery mode.
    - Written to when new scenes are committed (adds prose exemplar embeddings).
    - Initialized by core/runtime.py via init_chroma_collections() on startup/reset.
"""

import hashlib
import math
import shutil
from pathlib import Path
from typing import Callable, Optional

_COLLECTION_NAME = "prose_flavor"
_CHROMA_SUBDIR = "chroma"

# Module-level singletons set by init_chroma_collections().
_client = None
_collection = None

_EMBEDDING_DIM = 384


def _hashed_trigram_embedding(text: str) -> list[float]:
    """
    Deterministic, dependency-free placeholder embedding.

    Purpose:
        Maps text to a 384-dim L2-normalized hashed character-trigram count vector.
        Fully deterministic (no model download, no network) so the HNSW index is
        functional and testable from Sprint 2 onward. Real semantic embeddings are
        the Sprint 5+ drop-in: replace via set_embedding_function() — no changes
        to callers, the collection schema, or node_assemble_context.
    """
    vec = [0.0] * _EMBEDDING_DIM
    padded = f"  {text.lower()}  "
    for i in range(len(padded) - 2):
        trigram = padded[i : i + 3]
        idx = int.from_bytes(hashlib.md5(trigram.encode("utf-8")).digest()[:4], "big") % _EMBEDDING_DIM
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0.0:
        vec = [v / norm for v in vec]
    return vec


_embedding_fn: Callable[[str], list[float]] = _hashed_trigram_embedding


def set_embedding_function(fn: Callable[[str], list[float]]) -> None:
    """
    Swap the embedding function (Sprint 5+ real-model drop-in point).

    Inputs:
        fn: Callable mapping a text string to a fixed-length float vector.
            Must be dimensionally consistent across all calls within one collection.
    """
    global _embedding_fn
    _embedding_fn = fn


def init_chroma_collections(data_dir: Path) -> None:
    """
    Initialize or retrieve the ChromaDB collection for prose flavor embeddings.

    Purpose:
        Called by core/runtime.py during init_resources(). Creates the ChromaDB
        client pointed at data_dir and ensures the 'prose_flavor' collection exists.
        If the collection already exists (restart case), retrieves it without
        overwriting existing embeddings.

    Inputs:
        data_dir: Path — the data/ directory where ChromaDB stores its files.

    Outputs:
        None. Side effect: ChromaDB client and 'prose_flavor' collection are
        initialized and available for subsequent queries.
    """
    global _client, _collection
    import chromadb

    chroma_dir = Path(data_dir) / _CHROMA_SUBDIR
    chroma_dir.mkdir(parents=True, exist_ok=True)
    _client = chromadb.PersistentClient(path=str(chroma_dir))
    # Embeddings are always supplied explicitly (see _embedding_fn) — the
    # collection's default embedding function is never invoked, so no model
    # download occurs.
    _collection = _client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def reset_collections(data_dir: Path) -> None:
    """
    Delete all ChromaDB collections and their associated vector data.

    Purpose:
        Called by core/runtime.py during reset_resources(). Deletes all ChromaDB
        collections. Called before init_chroma_collections() in the reset sequence
        to provide a clean slate. ChromaDB is file-based — this is a file-deletion
        operation requiring no server API calls.

    Inputs:
        data_dir: Path — the data/ directory containing ChromaDB files.

    Outputs:
        None. Side effect: all ChromaDB collection data is deleted.
    """
    global _client, _collection
    _collection = None
    _client = None
    chroma_dir = Path(data_dir) / _CHROMA_SUBDIR
    if chroma_dir.exists():
        shutil.rmtree(chroma_dir)


def add_prose_embedding(
    text: str,
    metadata: dict,
    embedding_id: Optional[str] = None,
) -> None:
    """
    Embed and store one prose passage in the ChromaDB 'prose_flavor' collection.

    Purpose:
        Called when a new beat is committed to add its prose text to the HNSW
        index for future associative retrieval. metadata should include scene_id,
        chapter_id, and arc_id so retrieved vectors can be filtered by narrative
        proximity. embedding_id is derived from beat_id for idempotent upserts.

    Inputs:
        text: str — the committed beat prose text to embed and store.
        metadata: dict — filter metadata attached to the embedding
            (scene_id, chapter_id, arc_id, beat_id).
        embedding_id: Optional[str] — stable ID for upsert idempotency.
            Defaults to a hash of the text if not provided.

    Outputs:
        None. Side effect: one embedding is added/upserted to the collection.

    Raises:
        RuntimeError: If init_chroma_collections() has not been called.
    """
    if _collection is None:
        raise RuntimeError("ChromaDB not initialized — call init_chroma_collections() first.")
    if embedding_id is None:
        embedding_id = hashlib.sha256(text.encode("utf-8")).hexdigest()
    _collection.upsert(
        ids=[embedding_id],
        embeddings=[_embedding_fn(text)],
        documents=[text],
        metadatas=[metadata],
    )


def query_flavor_vectors(
    query_text: str,
    n_results: int = 5,
    exclude_chapter_id: Optional[str] = None,
) -> list[dict]:
    """
    Retrieve associative flavor exemplars nearest to the query text.

    Purpose:
        Called by node_assemble_context to inject flavor context into the prose
        generation prompt. Uses HNSW approximate nearest neighbor search. Optionally
        excludes vectors from the current chapter (exclude_chapter_id) to prevent
        self-referential flavor injection.

        Results are the lowest-priority context layer and will be the first dropped
        during token budget pruning. node_assemble_context checks available token
        budget before including HNSW results.

    Inputs:
        query_text: str — the active scene intent text used as the query vector.
        n_results: int — maximum number of flavor exemplars to return (default 5).
        exclude_chapter_id: Optional[str] — if provided, filters out results from
            this chapter to avoid retrieving text too recently generated.

    Outputs:
        List[dict]: Up to n_results flavor exemplar dicts, each containing:
            text (str), metadata (dict with scene_id/chapter_id/arc_id),
            distance (float, 0–1 cosine distance).

    Raises:
        RuntimeError: If init_chroma_collections() has not been called.
    """
    if _collection is None:
        raise RuntimeError("ChromaDB not initialized — call init_chroma_collections() first.")

    # Empty collection is the normal state in early chapters — return [] cleanly.
    if _collection.count() == 0:
        return []

    where = {"chapter_id": {"$ne": exclude_chapter_id}} if exclude_chapter_id else None
    results = _collection.query(
        query_embeddings=[_embedding_fn(query_text)],
        n_results=n_results,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    documents = results.get("documents") or [[]]
    metadatas = results.get("metadatas") or [[]]
    distances = results.get("distances") or [[]]
    return [
        {"text": doc, "metadata": meta, "distance": dist}
        for doc, meta, dist in zip(documents[0], metadatas[0], distances[0])
    ]

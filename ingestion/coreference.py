"""
ingestion/coreference.py

Staged NER Extraction and Maximum Likelihood Coreference Imputation.

Purpose:
    Performs entity recognition and coreference resolution on text chunks from the
    ingestion pipeline. Uses a two-stage approach to anchor proper names before
    evaluating ambiguous pronouns:

    Stage 1 — Proper Name Anchoring:
        Extracts formal proper names (characters, locations, items) using NER.
        These become high-confidence entity nodes in the Graphiti knowledge graph.

    Stage 2 — Pronoun Coreference Resolution:
        Evaluates pronouns in context against the anchored entity set. If coreference
        linkage confidence meets or exceeds config.thresholds.coreference_confidence_floor
        (proposed default 0.65), the link is written as high-confidence. If confidence
        falls below the floor, Maximum Likelihood Imputation (MLI) is applied: binds
        the pronoun to the most statistically probable entity based on recency,
        syntactic role, and gender agreement. MLI links are written as provisional
        (mid-confidence) with a "provisional" tag.

    All links are written to Graphiti via memory/graphiti_client.py. node_commit_transaction
    resolves provisional Epistemic Beliefs at chapter boundaries using the 200-token
    window heuristic: if the entity name and the linked pronoun appear within a 200-token
    window of committed beat text, the provisional tag is upgraded to confirmed or
    contradicted and dropped accordingly.

Architecture role:
    - Called by ingestion/pipeline.py for each text chunk.
    - The Alignment Dashboard (routes/alignment.py, templates/alignment.html) provides
      an optional non-blocking UI for human review of provisional links — the FSM
      never waits for this review.
    - Writes entity and coreference data to Graphiti and SQLite.
"""

from typing import Optional


async def extract_entities(chunk_text: str, project_id: str) -> list[dict]:
    """
    Stage 1: Extract proper name entities from a text chunk.

    Purpose:
        Runs NER on the chunk text to identify characters, locations, and items.
        Each identified entity is returned as a dict with a suggested entity ID,
        name, type, and confidence score. High-confidence proper names are written
        to SQLite Characters table and as entity nodes in Graphiti.

        The NER approach (LLM-based, spaCy-based, or hybrid) is not specified here
        and is an implementation decision for the Sprint that wires in ingestion.
        The function signature is fixed; the implementation is internal.

    Inputs:
        chunk_text: str — the manuscript text chunk to analyze.
        project_id: str — used for entity ID namespacing.

    Outputs:
        List[dict]: Extracted entities. Each dict contains:
            entity_id (str), name (str), type (str: 'character'|'location'|'item'),
            confidence (float 0–1), mentions (List[str]: verbatim text spans).
    """
    pass


async def resolve_coreferences(
    chunk_text: str,
    entities: list[dict],
    confidence_floor: float = 0.65,
) -> list[dict]:
    """
    Stage 2: Resolve pronoun coreferences and apply MLI for low-confidence links.

    Purpose:
        Given a text chunk and its extracted entities, identifies all pronoun mentions
        and attempts to link each to an entity. For links meeting confidence_floor,
        writes a high-confidence Graphiti edge. For links below the floor, applies
        Maximum Likelihood Imputation (recency + syntactic role + gender agreement)
        and writes a provisional (mid-confidence) edge tagged as unconfirmed.

        confidence_floor is read from config.thresholds.coreference_confidence_floor
        (proposed default 0.65). Configurable via config.yaml.

    Inputs:
        chunk_text: str — the manuscript text chunk.
        entities: List[dict] — entity dicts from extract_entities() for this chunk.
        confidence_floor: float — minimum confidence for a non-MLI coreference link.

    Outputs:
        List[dict]: Coreference link dicts. Each contains:
            pronoun_text (str), linked_entity_id (str), confidence (float),
            provisional (bool), link_type (str: 'high'|'mid'|'low').
            Low-confidence links after MLI are still returned as 'mid' (MLI result).
    """
    pass

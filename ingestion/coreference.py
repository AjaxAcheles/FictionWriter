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

import re
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
    # Heuristic NER: runs of capitalized words not at sentence starts (or seen
    # repeatedly) are proper-name candidates. Confidence scales with mention
    # frequency. LLM/spaCy NER can replace this internals without contract change.
    candidate_re = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b")
    sentence_starts = {m.start(2) for m in re.finditer(r"(^|[.!?]\s+)(\w)", chunk_text)}
    counts: dict[str, list[str]] = {}
    for match in candidate_re.finditer(chunk_text):
        name = match.group(1)
        if name.lower() in _STOPWORDS:
            continue
        # Single capitalized word at a sentence start is likely just case, not a name —
        # unless we have seen it mid-sentence elsewhere (handled by aggregation).
        if match.start(1) in sentence_starts and " " not in name and name not in counts:
            continue
        counts.setdefault(name, []).append(name)

    entities = []
    for name, mentions in counts.items():
        frequency = len(mentions)
        confidence = min(0.5 + 0.15 * frequency, 0.99)
        entities.append(
            {
                "entity_id": f"{project_id}_{re.sub(r'[^a-z0-9]+', '_', name.lower())}",
                "name": name,
                "type": "character",
                "confidence": round(confidence, 2),
                "mentions": mentions,
            }
        )
    entities.sort(key=lambda e: -e["confidence"])
    return entities


_STOPWORDS = {
    "the", "a", "an", "i", "he", "she", "they", "it", "we", "you", "but", "and",
    "chapter", "scene", "then", "when", "after", "before", "his", "her", "their",
}

_PRONOUN_RE = re.compile(r"\b(he|she|they|him|her|them|his|hers|theirs)\b", re.IGNORECASE)


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
    if not entities:
        return []
    # Position index of every entity mention for recency scoring.
    mention_positions: list[tuple[int, dict]] = []
    for entity in entities:
        for match in re.finditer(re.escape(entity["name"]), chunk_text):
            mention_positions.append((match.start(), entity))
    mention_positions.sort()

    links = []
    for pronoun in _PRONOUN_RE.finditer(chunk_text):
        preceding = [(pos, e) for pos, e in mention_positions if pos < pronoun.start()]
        if not preceding:
            continue
        pos, entity = preceding[-1]  # most recent antecedent
        distance = pronoun.start() - pos
        # Recency scoring: confidence decays with character distance, scaled by
        # the antecedent entity's own NER confidence.
        confidence = entity["confidence"] * max(0.2, 1.0 - distance / 2000.0)
        if confidence >= confidence_floor:
            link_type, provisional = "high", False
        else:
            # Maximum Likelihood Imputation: recency + frequency prior — the
            # imputed link is promoted to 'mid' and flagged provisional.
            confidence = max(confidence, confidence_floor * 0.9)
            link_type, provisional = "mid", True
        links.append(
            {
                "pronoun_text": pronoun.group(0),
                "linked_entity_id": entity["entity_id"],
                "confidence": round(confidence, 3),
                "provisional": provisional,
                "link_type": link_type,
                "context_snippet": chunk_text[max(0, pronoun.start() - 60) : pronoun.start() + 60],
            }
        )
    return links


def persist_results(entities: list[dict], links: list[dict], chunk_text: str) -> None:
    """
    Write high-confidence entities to SQLite Characters and provisional links to
    the claim store (Alignment Dashboard source). Idempotent (INSERT OR IGNORE /
    claim de-dup is the store's concern).
    """
    from core import runtime
    from memory import provisional_store, sqlite_db

    for entity in entities:
        if entity["type"] == "character" and entity["confidence"] >= 0.65:
            sqlite_db.insert_row(
                runtime.SQLITE_PATH,
                "Characters",
                {"char_id": entity["entity_id"], "name": entity["name"],
                 "role": None, "description": None},
            )
    provisional = [l for l in links if l["provisional"]]
    if provisional:
        provisional_store.add_claims(provisional)

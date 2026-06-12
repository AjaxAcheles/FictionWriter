"""
memory/provisional_store.py

Provisional Coreference Claim Store (JSON-file backed).

Purpose:
    Holds mid-confidence coreference links awaiting confirmation — the data
    source for the Alignment Dashboard claim cards. Structurally this stands in
    for Graphiti provisional edges while the FalkorDB Lite driver chain remains
    a no-op; the public API mirrors the edge lifecycle (pending → confirmed |
    contradicted) so swapping the backing store requires no route changes.

    All writes are atomic (tmp file + os.replace).

Architecture role:
    - Written by ingestion/coreference.py (provisional links from MLI).
    - Read/updated by routes/alignment.py (claim cards, confirm/contradict).
    - The chapter-boundary Epistemic Belief promotion pass (node_commit_transaction)
      will consume this store when real Graphiti writes land.
"""

import json
import os
import uuid
from pathlib import Path

STORE_PATH = Path("data/provisional_claims.json")


def _load() -> list[dict]:
    if not STORE_PATH.exists():
        return []
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save(claims: list[dict]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(claims, indent=2), encoding="utf-8")
    os.replace(tmp, STORE_PATH)


def add_claims(claims: list[dict]) -> list[str]:
    """
    Append provisional claims; assigns claim_id and status='pending'.

    Inputs:
        claims: dicts with pronoun_text, linked_entity_id, confidence,
            context_snippet (optional).

    Outputs:
        list[str]: assigned claim IDs.
    """
    existing = _load()
    ids = []
    for claim in claims:
        claim_id = claim.get("claim_id") or f"claim_{uuid.uuid4().hex[:12]}"
        ids.append(claim_id)
        existing.append({**claim, "claim_id": claim_id, "status": "pending"})
    _save(existing)
    return ids


def list_pending() -> list[dict]:
    """All claims with status='pending', insertion order."""
    return [c for c in _load() if c.get("status") == "pending"]


def _set_status(claim_id: str, status: str) -> bool:
    claims = _load()
    for claim in claims:
        if claim.get("claim_id") == claim_id:
            claim["status"] = status
            _save(claims)
            return True
    return False


def confirm(claim_id: str) -> bool:
    """Promote a claim to a permanent high-confidence fact. False if unknown."""
    return _set_status(claim_id, "confirmed")


def contradict(claim_id: str) -> bool:
    """Drop a claim permanently. False if unknown."""
    return _set_status(claim_id, "contradicted")

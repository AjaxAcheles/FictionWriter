"""
evals/ — LLM-as-Judge evaluation harness (Sprint 7).

Generates seeded test manuscripts with known injected continuity errors, runs
them through the real critic pipeline, and scores critic performance with a
judge LLM. Outputs recall, false-positive rate, and drift fidelity.
"""

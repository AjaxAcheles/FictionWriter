"""
evals/manuscript_generator.py

Deterministic seeded test-manuscript generator.

Purpose:
    Authors a short (5-10k word) multi-beat manuscript from a structured fact
    ledger — characters, props, locations, and a strict event timeline. The
    ledger is the ground truth that evals/error_injection.py mutates against
    and the judge scores against. Fully deterministic per seed: no LLM calls,
    so eval baselines are reproducible and CI-runnable.

Architecture role:
    - Consumed by evals/runner.py. The ledger travels with the manuscript so
      the judge can verify critic findings against authoritative facts.
"""

import random
from dataclasses import dataclass, field


@dataclass
class FactLedger:
    """Ground-truth facts for one test manuscript."""

    characters: dict = field(default_factory=dict)   # name -> {"role", "prop", "location"}
    timeline: list = field(default_factory=list)     # ordered event descriptions
    open_threads: list = field(default_factory=list) # {"name", "required_resolution"}


_NAMES = ["Mara", "Tobias", "Yusra", "Calder", "Ilse", "Petro"]
_PROPS = ["brass compass", "sealed manifest", "bone-handled knife", "tin lantern",
          "wax-stamped letter", "silver flask"]
_LOCATIONS = ["the harbor office", "the breakwater", "the chart room",
              "the customs shed", "the tavern loft", "the dry dock"]
_ACTIONS = [
    "studies the tide tables and marks the ledger",
    "bars the door and checks the window latch",
    "counts the crates twice and signs the slip",
    "argues with the clerk over the tariff",
    "traces the route on the salt-stained chart",
    "pockets the key and snuffs the lamp",
]


def build_ledger(seed: int, character_count: int = 3) -> FactLedger:
    """Deterministic fact ledger for a seed."""
    rng = random.Random(seed)
    names = rng.sample(_NAMES, character_count)
    props = rng.sample(_PROPS, character_count)
    locations = rng.sample(_LOCATIONS, character_count)
    ledger = FactLedger()
    for name, prop, location in zip(names, props, locations):
        ledger.characters[name] = {"role": "crew", "prop": prop, "location": location}
    ledger.timeline = [
        f"{names[i % len(names)]} {rng.choice(_ACTIONS)}" for i in range(6)
    ]
    ledger.open_threads = [
        {"name": f"The {props[0]} debt", "required_resolution": f"{names[0]} must keep the {props[0]} until the final chapter."}
    ]
    return ledger


def generate_manuscript(seed: int, beats: int = 8, words_per_beat: int = 800) -> tuple[list[str], FactLedger]:
    """
    Generate `beats` beat texts (~words_per_beat each) consistent with the ledger.

    Outputs:
        (beat_texts, ledger) — total length lands in the 5-10k word envelope for
        the default 8 x 800 configuration.
    """
    rng = random.Random(seed * 7919 + 1)
    ledger = build_ledger(seed)
    names = list(ledger.characters)

    beat_texts = []
    for beat_index in range(beats):
        sentences = []
        # Anchor sentences restate ledger facts — the critic's checkable surface.
        for name, facts in ledger.characters.items():
            sentences.append(f"{name} keeps the {facts['prop']} close at {facts['location']}.")
        sentences.append(f"Event order holds: {ledger.timeline[beat_index % len(ledger.timeline)]} "
                         f"before {ledger.timeline[(beat_index + 1) % len(ledger.timeline)]}.")
        while sum(len(s.split()) for s in sentences) < words_per_beat:
            actor = rng.choice(names)
            sentences.append(
                f"{actor} {rng.choice(_ACTIONS)}, and the grey water slaps the pilings "
                f"while gulls wheel over {rng.choice(_LOCATIONS)}."
            )
        beat_texts.append(" ".join(sentences))
    return beat_texts, ledger

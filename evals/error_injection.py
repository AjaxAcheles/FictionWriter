"""
evals/error_injection.py

Known-error injectors for eval manuscripts.

Purpose:
    Mutates generated beat texts to plant continuity errors with exact,
    machine-readable metadata. Each injector mutates exactly one beat and
    returns an InjectedError record carrying the beat index, error class,
    human-readable description, and the mutated span — everything the judge
    needs to score a critic finding as a catch.

Error classes:
    fact_flip         — a character's signature prop/location is swapped.
    name_swap         — an action is attributed to the wrong character.
    timeline_reversal — an "X before Y" ordering statement is inverted.
    thread_paradox    — an open thread's required resolution is violated.
"""

import random
from dataclasses import dataclass

from evals.manuscript_generator import FactLedger


@dataclass
class InjectedError:
    """Ground-truth record of one planted continuity error."""

    beat_index: int
    error_class: str
    description: str
    mutated_span: str


def inject_fact_flip(beats: list[str], ledger: FactLedger, beat_index: int) -> InjectedError:
    """Swap two characters' signature props inside one beat."""
    names = list(ledger.characters)
    a, b = names[0], names[1]
    prop_a = ledger.characters[a]["prop"]
    prop_b = ledger.characters[b]["prop"]
    original = f"{a} keeps the {prop_a}"
    mutated = f"{a} keeps the {prop_b}"
    beats[beat_index] = beats[beat_index].replace(original, mutated, 1)
    return InjectedError(
        beat_index=beat_index,
        error_class="fact_flip",
        description=f"{a}'s prop is {prop_a}, not {prop_b}.",
        mutated_span=mutated,
    )


def inject_name_swap(beats: list[str], ledger: FactLedger, beat_index: int) -> InjectedError:
    """Attribute a ledger character's location to a different character."""
    names = list(ledger.characters)
    a, b = names[0], names[-1]
    location_a = ledger.characters[a]["location"]
    original = f"at {location_a}."
    mutated = f"at {location_a}, though everyone knows {b} holds that post."
    beats[beat_index] = beats[beat_index].replace(original, mutated, 1)
    return InjectedError(
        beat_index=beat_index,
        error_class="name_swap",
        description=f"{location_a} belongs to {a}; attributing it to {b} contradicts the ledger.",
        mutated_span=mutated,
    )


def inject_timeline_reversal(beats: list[str], ledger: FactLedger, beat_index: int) -> InjectedError:
    """Invert the beat's 'X before Y' ordering anchor sentence."""
    text = beats[beat_index]
    marker = "Event order holds: "
    start = text.index(marker)
    end = text.index(".", start)
    sentence = text[start + len(marker): end]
    x, y = sentence.split(" before ")
    mutated = f"{marker}{y} before {x}."
    beats[beat_index] = text[:start] + mutated + text[end + 1:]
    return InjectedError(
        beat_index=beat_index,
        error_class="timeline_reversal",
        description=f"Canonical order is '{x}' before '{y}'; the beat reverses it.",
        mutated_span=mutated,
    )


def inject_thread_paradox(beats: list[str], ledger: FactLedger, beat_index: int) -> InjectedError:
    """Violate an open thread's required resolution inside one beat."""
    thread = ledger.open_threads[0]
    keeper = next(iter(ledger.characters))
    prop = ledger.characters[keeper]["prop"]
    mutated = (f" {keeper} hurls the {prop} into the black water and watches it sink, "
               f"done with it forever.")
    beats[beat_index] += mutated
    return InjectedError(
        beat_index=beat_index,
        error_class="thread_paradox",
        description=f"Open thread '{thread['name']}' requires: {thread['required_resolution']}",
        mutated_span=mutated.strip(),
    )


_INJECTORS = [inject_fact_flip, inject_name_swap, inject_timeline_reversal, inject_thread_paradox]


def inject_errors(beats: list[str], ledger: FactLedger, count: int, seed: int) -> list[InjectedError]:
    """
    Plant `count` errors across distinct beats, deterministic per seed.

    Cycles through the four injector families; beats are chosen without
    replacement so each error is independently attributable.
    """
    rng = random.Random(seed * 104729 + 3)
    if count > len(beats):
        raise ValueError(f"cannot inject {count} errors into {len(beats)} beats")
    target_beats = rng.sample(range(len(beats)), count)
    return [
        _INJECTORS[i % len(_INJECTORS)](beats, ledger, beat_index)
        for i, beat_index in enumerate(target_beats)
    ]

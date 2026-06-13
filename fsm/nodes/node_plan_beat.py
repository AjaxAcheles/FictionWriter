"""
fsm/nodes/node_plan_beat.py

Level 4 Planning Node — Scene Beat Partitioner with PAD Translation Pipeline.

Purpose:
    Slices the immediate scene into actionable beats with strict physical entry/exit
    constraints. Calculates target emotional acceleration (ΔE) for each character via
    EWMA on their PAD (Pleasure-Arousal-Dominance) state vectors.

    Executes the PAD Grounded Translation Pipeline sequentially:
    1. Calculate raw PAD coordinates (EWMA with alpha from config.thresholds.ewma_alpha).
    2. Look up the deterministic baseline behavioral constraint string from a static
       JSON dictionary keyed by PAD region (prompts/pad_regions.json).
    3. Send that baseline string + scene intent to a fast small-tier LLM
       (config.endpoints.pad_translator) to adapt constraints to narrative context.
    4. Write the tailored constraint string into the Beat row in SQLite.

    Error path for step 3: if the LLM call fails (timeout, garbage output, or two
    consecutive failures after one retry), the pipeline falls back to the raw
    static-JSON baseline string. Planning continues unblocked. Fallback is logged
    at WARNING level. Steps 1, 2, and 4 always execute.

    Config read policy: reads config.yaml at node entry. Settings-page slider changes
    (Dc threshold, EWMA alpha) take effect at the next beat boundary.

    Scene advancement guard: the scene is closed by node_commit_transaction only when
    BOTH (committed word count >= scene word_budget) AND (committed beats >=
    beats_per_scene_min). This node extends an open scene with additional beats when
    re-entered after all planned beats commit but the guard says the scene needs more.

Architecture role:
    - Lowest tier of the planning cascade; directly precedes context assembly.
    - Uses: config.endpoints.planner for beat partitioning (steps 1-2 prompt) and
      config.endpoints.pad_translator for PAD translation step 3.
    - Loads prompts: prompts/node_plan_beat.xml.j2 and prompts/node_plan_beat_pad.xml.j2.
    - Emits a structured JSON log entry via get_logger("node_plan_beat").
"""

import json
import time
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, model_validator

from core import runtime, stream_bus
from core.config_loader import load_config
from core.logger import get_logger, log_node_event
from fsm.nodes.node_assemble_context import _edges_to_text, partition_coreference_links
from fsm.state import OrchestratorState
from llm import call_llm as call_llm_module
from memory import sqlite_db
from memory.graphiti_client import query_point_in_time_subgraph
from prompts.prompt_loader import PromptLoader

logger = get_logger("node_plan_beat")

PAD_REGIONS_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "pad_regions.json"


class PlannedBeat(BaseModel):
    """One beat partition as returned by the planner LLM."""

    model_config = ConfigDict(extra="ignore")

    id: str
    scene_id: str
    beat_index: int
    description: str
    word_budget: int
    entry_constraints: str
    exit_constraints: str
    raw_pad_targets: dict = {}
    pad_region: Optional[str] = None
    status: str = "planned"


class BeatPlanList(BaseModel):
    """Top-level planner output schema: {'beats': [...]}."""

    model_config = ConfigDict(extra="ignore")

    beats: List[PlannedBeat]

    @model_validator(mode="before")
    @classmethod
    def _wrap_bare_list(cls, data):
        """Bare array → treat as the beats list (tolerance shim)."""
        if isinstance(data, list):
            return {"beats": data}
        return data


def load_pad_regions() -> dict:
    """Load the static PAD region lookup dictionary (step 2 source of truth)."""
    with PAD_REGIONS_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def pad_region_key(pleasure: float, arousal: float, dominance: float) -> str:
    """
    Deterministic PAD octant key for the static lookup dictionary.

    Each axis maps to 'high' when its EWMA coordinate >= 0.0, else 'low'.
    Identical coordinates always produce the identical key — step 2 of the
    pipeline is fully deterministic.
    """
    p = "high" if pleasure >= 0.0 else "low"
    a = "high" if arousal >= 0.0 else "low"
    d = "high" if dominance >= 0.0 else "low"
    return f"{p}_pleasure_{a}_arousal_{d}_dominance"


def ewma_pad(emotion_rows: list[dict], alpha: float) -> dict:
    """
    EWMA over a character's PAD history (newest row first).

    Purpose:
        Computes the smoothed current PAD coordinate used for region lookup.
        EWMA is applied oldest -> newest so the newest row carries weight alpha:
        s = alpha * x_t + (1 - alpha) * s_{t-1}.

    Outputs:
        {'pleasure': float, 'arousal': float, 'dominance': float} — neutral
        origin (0,0,0) when the character has no recorded history.
    """
    if not emotion_rows:
        return {"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0}
    ordered = list(reversed(emotion_rows))  # oldest first
    state = {k: float(ordered[0][k]) for k in ("pleasure", "arousal", "dominance")}
    for row in ordered[1:]:
        for k in state:
            state[k] = alpha * float(row[k]) + (1.0 - alpha) * state[k]
    return state


async def _translate_pad_constraint(
    config,
    loader: PromptLoader,
    character_name: str,
    region_key: str,
    baseline: str,
    scene_intent: str,
    beat_description: str,
) -> str:
    """
    PAD pipeline step 3 — best-effort LLM adaptation with retry-once fallback.

    Two consecutive failures (one retry) fall back to the raw static baseline
    string, logged at WARNING. Garbage output (empty/whitespace) counts as a
    failure. Never raises.
    """
    prompt = loader.load_and_render("node_plan_beat_pad.xml.j2",
        {
            "raw_baseline_constraint": baseline,
            "pad_region": region_key,
            "scene_intent": scene_intent,
            "character_name": character_name,
            "beat_description": beat_description,
        },
    )
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(2):
        try:
            text = await call_llm_module.collect_llm_response(
                config.endpoints.pad_translator, messages, temperature=0.4, stream=False
            )
            if text and text.strip():
                return text.strip()
        except Exception as e:  # noqa: BLE001 — step 3 is best-effort by contract
            logger.warning(
                "PAD translation attempt %d failed for %s: %r", attempt + 1, character_name, e
            )
    logger.warning(
        "PAD translation fell back to static baseline for %s (region=%s).",
        character_name,
        region_key,
    )
    return baseline


def _scene_entry_context(db: Path, scene_id: str, existing_count: int = 0) -> str:
    """
    What is physically true at the point this planning pass starts.

    When the scene already has committed beats (scene extension after the
    advancement guard kept it open), the binding context is the last committed
    beat's exit_constraints — new beats must enter exactly where beat N-1
    exited. Otherwise falls back to the last committed scene's closing prose.
    """
    from contextlib import closing

    if existing_count > 0:
        prev = sqlite_db.get_beat_by_index(db, scene_id, existing_count - 1)
        if prev is not None:
            exit_constraints = (
                json.loads(prev.get("beat_plan_json") or "{}").get("exit_constraints") or ""
            )
            if exit_constraints:
                return exit_constraints

    scene = sqlite_db.get_row(db, "Scenes", "scene_id", scene_id)
    if scene is None:
        return ""
    with closing(sqlite_db.get_connection(db)) as conn:
        rows = conn.execute(
            "SELECT * FROM Scenes WHERE chapter_id = ? AND committed_at IS NOT NULL "
            "AND scene_index < ? ORDER BY scene_index DESC LIMIT 1",
            (scene["chapter_id"], scene["scene_index"]),
        ).fetchall()
    if not rows:
        return ""
    return (dict(rows[0]).get("prose_text") or "")[-500:]


async def node_plan_beat(state: OrchestratorState) -> dict:
    """
    Partition the active scene into beats and execute the PAD Translation Pipeline.

    Behavior:
        - If planned (uncommitted) beats already exist for the active scene, the
          pointer simply advances to the lowest planned beat_index — no replanning.
        - Otherwise the planner endpoint partitions the scene (extending it when
          the scene-advancement guard kept it open), the PAD pipeline runs for the
          scene's characters, and Beat rows are written to SQLite.

    Outputs (merged into OrchestratorState):
        fsm_pointer: beat_index set to the next beat to draft.
    """
    start = time.monotonic()
    pointer = state["fsm_pointer"]
    config = load_config()  # node-entry re-read: Settings changes apply at beat boundary
    db = runtime.SQLITE_PATH

    try:
        planned = sqlite_db.get_planned_beats(db, pointer.scene_id)
        if planned:
            next_index = planned[0]["beat_index"]
            updated = pointer.model_copy(update={"beat_index": next_index})
            log_node_event(logger, updated.model_dump(), (time.monotonic() - start) * 1000.0, "success")
            return {"fsm_pointer": updated}

        scene = sqlite_db.get_row(db, "Scenes", "scene_id", pointer.scene_id)
        if scene is None:
            raise RuntimeError(f"node_plan_beat: active scene {pointer.scene_id!r} not found")

        loader = PromptLoader()
        characters = sqlite_db.get_characters(db)
        alpha = config.thresholds.ewma_alpha

        pad_states = {}
        for char in characters:
            history = sqlite_db.get_recent_character_emotions(db, char["char_id"])
            pad_states[char["char_id"]] = ewma_pad(history, alpha)

        existing_count = sqlite_db.get_committed_beat_count(db, pointer.scene_id)

        # Hard-ceiling fail-safe: the scene-extension path re-enters this node to
        # add beats whenever the advancement guard keeps a scene open. A scene that
        # never satisfies the guard would extend forever (silently, below the
        # LangGraph recursion_limit backstop). Refuse to extend past the configured
        # maximum, surfacing the runaway to the escalation ladder instead.
        if existing_count >= config.thresholds.max_beats_per_scene:
            raise RuntimeError(
                f"node_plan_beat: scene {pointer.scene_id!r} already has {existing_count} "
                f"committed beats (max_beats_per_scene={config.thresholds.max_beats_per_scene}) "
                f"— refusing to extend; runaway loop surfaced to the escalation ladder."
            )

        # World-state injection: the partitioner sees the same point-in-time
        # facts and open threads the drafter will, so it cannot schedule
        # physically impossible actions (items teleporting, dead threads).
        edges = await query_point_in_time_subgraph(
            entity_ids=[c["char_id"] for c in characters],
            active_event_id=f"{pointer.scene_id}_beat_{existing_count}",
        )
        facts, _beliefs = partition_coreference_links(
            list(edges or []), config.thresholds.coreference_confidence_floor
        )
        threads = sqlite_db.get_open_threads(db)

        prompt = loader.load_and_render("node_plan_beat.xml.j2",
            {
                "scene_id": pointer.scene_id,
                "scene_description": scene.get("description") or "",
                "scene_word_budget": scene.get("word_budget") or 0,
                "beats_per_scene_min": config.thresholds.beats_per_scene_min,
                "character_pad_states": json.dumps(pad_states),
                "ewma_alpha": alpha,
                "scene_entry_context": _scene_entry_context(db, pointer.scene_id, existing_count),
                "graphiti_facts": _edges_to_text(facts),
                "thread_statuses": "\n".join(
                    f"- {t['name']} (priority {t['priority']}): {t.get('description') or ''}"
                    for t in threads
                ),
            },
        )
        plan = await call_llm_module.call_llm_structured(
            config.endpoints.planner,
            [{"role": "user", "content": prompt}],
            BeatPlanList,
            retry_cap=config.model_validate_retry_cap,
        )
        if not plan.beats:
            # An empty plan would reset the pointer to beat 0 (already committed)
            # and spin the draft→commit loop forever without growing the scene.
            raise RuntimeError(
                f"node_plan_beat: planner returned an empty beat plan for scene "
                f"{pointer.scene_id!r} — surfacing to the escalation ladder."
            )

        regions = load_pad_regions()
        scene_intent = scene.get("description") or ""
        char_names = {c["char_id"]: c["name"] for c in characters}

        first_new_index = None
        for offset, beat in enumerate(plan.beats):
            beat_index = existing_count + offset
            # Step 1 (deterministic recompute): EWMA toward the planner's raw targets.
            pad_constraints: list[str] = []
            for char_id, current in pad_states.items():
                target = beat.raw_pad_targets.get(char_id) or current
                projected = {
                    k: alpha * float(target.get(k, current[k])) + (1.0 - alpha) * current[k]
                    for k in ("pleasure", "arousal", "dominance")
                }
                # Step 2: deterministic static lookup (LLM pad_region is advisory only).
                region = pad_region_key(**projected)
                baseline = regions.get(beat.pad_region or "") or regions[region]
                # Step 3: best-effort adaptation with static fallback.
                tailored = await _translate_pad_constraint(
                    config,
                    loader,
                    char_names.get(char_id, char_id),
                    region,
                    baseline,
                    scene_intent,
                    beat.description,
                )
                pad_constraints.append(tailored)
                pad_states[char_id] = projected

            # Step 4: persist the Beat row with the tailored constraint string.
            beat_id = f"{pointer.scene_id}_beat_{beat_index}"
            sqlite_db.upsert_beat(
                db,
                {
                    "beat_id": beat_id,
                    "scene_id": pointer.scene_id,
                    "beat_index": beat_index,
                    "beat_plan_json": json.dumps(
                        {
                            "description": beat.description,
                            "word_budget": beat.word_budget,
                            "entry_constraints": beat.entry_constraints,
                            "exit_constraints": beat.exit_constraints,
                            "raw_pad_targets": beat.raw_pad_targets,
                            "pad_constraint": "\n".join(pad_constraints),
                        }
                    ),
                    "status": "planned",
                },
            )
            stream_bus.publish({
                "type": "planning",
                "level": "beat",
                "scene_id": pointer.scene_id,
                "text": f"beat {beat_index}: {beat.description}",
            })
            if first_new_index is None:
                first_new_index = beat_index

        updated = pointer.model_copy(update={"beat_index": first_new_index})
        log_node_event(logger, updated.model_dump(), (time.monotonic() - start) * 1000.0, "success")
        return {"fsm_pointer": updated}
    except Exception as e:
        log_node_event(logger, pointer.model_dump(), (time.monotonic() - start) * 1000.0, "failure", error=repr(e))
        raise

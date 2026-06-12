"""
tests/test_planner_tolerance.py

Regression tests for the gemma:2b "bare fenced array" failure (2026-06-11).

Purpose:
    The first live Ollama run died with StructuredOutputError in node_plan_global:
    the model returned a markdown-fenced JSON ARRAY of arcs while GlobalPlan
    required a wrapper object. These tests pin the three fixes:
    1. Bare-list tolerance shims on every planner schema.
    2. Fence/array-aware extraction inside the bounded validation loop.
    3. Field-name alias tolerance on PlannedArc.
"""

import pytest

from fsm.nodes.node_plan_arc import ArcPlan
from fsm.nodes.node_plan_beat import BeatPlanList
from fsm.nodes.node_plan_chapter import ChapterPlan
from fsm.nodes.node_plan_global import GlobalPlan
from llm.call_llm import _extract_first_json_object

# Abbreviated verbatim shape from logs/llm_io.log (gemma:2b-instruct response).
GEMMA_FENCED_ARRAY = """```json
[
  {
    "id": "arc_1",
    "title": "The Inciting Crisis",
    "description": "Seo-yeon and Junho meet in the Busan refugee camps.",
    "thematic_purpose": "Love amidst adversity",
    "word_range_start": 0,
    "word_range_end": 60000,
    "ordering": 1
  },
  {
    "id": "arc_2",
    "title": "Rising Stakes",
    "description": "A painful separation as the war intensifies.",
    "thematic_purpose": "The cost of war",
    "word_range_start": 60000,
    "word_range_end": 150000,
    "ordering": 2
  }
]
```"""


def test_extraction_handles_fenced_arrays():
    extracted = _extract_first_json_object(GEMMA_FENCED_ARRAY)
    assert extracted is not None
    assert extracted.startswith("[") and extracted.endswith("]")


def test_global_plan_accepts_bare_fenced_array():
    """The exact 2026-06-11 failure payload must now validate."""
    extracted = _extract_first_json_object(GEMMA_FENCED_ARRAY)
    plan = GlobalPlan.model_validate_json(extracted)
    assert [a.id for a in plan.arcs] == ["arc_1", "arc_2"]
    assert plan.arcs[0].title == "The Inciting Crisis"
    assert plan.threads == []


def test_planned_arc_field_aliases():
    plan = GlobalPlan.model_validate(
        {"arcs": [{"id": "a1", "name": "Aliased", "summary": "s", "estimated_word_count": 1200}]}
    )
    assert plan.arcs[0].title == "Aliased"
    assert plan.arcs[0].description == "s"
    assert plan.arcs[0].word_allocation == 1200


def test_all_planner_schemas_wrap_bare_lists():
    assert ArcPlan.model_validate([{"id": "ch_1", "description": "d"}]).chapter_stubs[0].id == "ch_1"
    assert ChapterPlan.model_validate(
        [{"id": "sc_1", "description": "d", "word_budget": 500, "ordering": 0}]
    ).scenes[0].id == "sc_1"
    assert BeatPlanList.model_validate(
        [{
            "id": "b0", "scene_id": "sc_1", "beat_index": 0, "description": "d",
            "word_budget": 300, "entry_constraints": "e", "exit_constraints": "x",
        }]
    ).beats[0].beat_index == 0


async def test_structured_loop_recovers_fenced_array_first_attempt(monkeypatch):
    """One LLM call, fenced array response → parsed GlobalPlan, no retries burned."""
    import llm.call_llm as adapter
    from core.config_loader import load_config

    calls = []

    async def fake_collect(endpoint, messages, **kwargs):
        calls.append(1)
        return GEMMA_FENCED_ARRAY

    monkeypatch.setattr(adapter, "collect_llm_response", fake_collect)
    config = load_config()
    plan = await adapter.call_llm_structured(
        config.endpoints.planner, [{"role": "user", "content": "plan"}],
        GlobalPlan, retry_cap=3,
    )
    assert len(calls) == 1
    assert len(plan.arcs) == 2

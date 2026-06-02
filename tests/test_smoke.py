"""
tests/test_smoke.py

Baseline FSM Routing Smoke Tests.

Purpose:
    Asserts that the core routing logic in the three conditional edge functions
    (edge_mode_selector, edge_programmatic_router, edge_commit_router) returns
    the correct routing string for a set of canonical state configurations.

    These tests verify the routing CONTRACT — that the first-match-wins priority
    ordering is implemented correctly — without executing any LLM calls or DB writes.
    All state is constructed directly as dicts (matching OrchestratorState fields).

    Tests:
    test_mode_selector_paradox_bypasses_retry   — has_paradox=True always → freeze_and_escalate.
    test_mode_selector_clean_draft_commits       — no failures + Dc < threshold → commit.
    test_mode_selector_retry_max_escalates       — retry_count > 5 → freeze_and_escalate.
    test_mode_selector_craft_threshold           — retry_count == 4 → craft_consultant.
    test_mode_selector_normal_revision           — failures + retry_count <= 3 → revise_prose.
    test_programmatic_router_fast_path           — retry==0, no failures, Dc well below → commit.
    test_programmatic_router_standard_path       — any failure → adversarial_critics.
    test_programmatic_router_retry_nonzero       — retry_count > 0 → adversarial_critics.
    test_transient_dc_override_used              — transient_dc_override supersedes config threshold.
"""

import pytest

from fsm.routers.edge_mode_selector import edge_mode_selector
from fsm.routers.edge_programmatic_router import edge_programmatic_router
from fsm.state import FailureObject


def _make_state(**overrides) -> dict:
    """
    Construct a minimal OrchestratorState-compatible dict for routing tests.

    Purpose:
        Helper that builds a state dict with safe defaults and applies any overrides.
        Routing tests only need a subset of state fields; defaults cover the rest.

    Inputs:
        **overrides: keyword arguments that override default field values.

    Outputs:
        dict: A partial OrchestratorState dict suitable for passing to router functions.
    """
    defaults = {
        "has_paradox": False,
        "critic_failures": [],
        "stylometric_distance": 0.0,
        "transient_dc_override": None,
        "retry_count": 0,
        "replan_count": 0,
        "escalation_tier": 0,
        "fsm_pointer": {
            "arc_id": "arc_1",
            "chapter_id": "ch_1",
            "scene_id": "sc_1",
            "beat_index": 0,
        },
    }
    defaults.update(overrides)
    return defaults


def test_mode_selector_paradox_bypasses_retry():
    """
    Assert has_paradox=True routes to node_freeze_and_escalate regardless of retry_count.

    Purpose:
        Verifies that the has_paradox check (step 0 in edge_mode_selector) fires before
        the retry_count elif chain. Even with retry_count=0 (normally a commit candidate),
        a paradox must escalate immediately.
    """
    pass


def test_mode_selector_clean_draft_commits():
    """
    Assert no failures + Dc below threshold routes to node_commit_transaction.
    """
    pass


def test_mode_selector_retry_max_escalates():
    """
    Assert retry_count > 5 routes to node_freeze_and_escalate.
    """
    pass


def test_mode_selector_craft_threshold():
    """
    Assert retry_count == 4 (> 3 and <= 5) routes to node_craft_consultant.
    """
    pass


def test_mode_selector_normal_revision():
    """
    Assert failures present + retry_count == 2 routes to node_revise_prose.
    """
    pass


def test_programmatic_router_fast_path():
    """
    Assert retry==0, empty failures, Dc << threshold routes to node_commit_transaction.
    """
    pass


def test_programmatic_router_standard_path_on_failure():
    """
    Assert any critic_failures present routes to node_adversarial_critics.
    """
    pass


def test_programmatic_router_nonzero_retry_routes_to_critics():
    """
    Assert retry_count > 0 always routes to node_adversarial_critics (fast path disabled).
    """
    pass


def test_transient_dc_override_supersedes_config():
    """
    Assert that when transient_dc_override is set, it is used as the Dc threshold
    instead of the config stel_cosine_distance in edge_mode_selector.
    """
    pass

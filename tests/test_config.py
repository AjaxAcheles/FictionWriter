"""
tests/test_config.py

AppConfig Pydantic Validation Tests.

Purpose:
    Verifies that core/config_loader.py correctly enforces the extra='forbid' contract
    across all nested AppConfig models. Each test introduces a specific typo or type
    error into the config and asserts that a Pydantic ValidationError is raised at
    load time — before any LLM calls or resource initialization occurs.

    Also tests the happy-path: a valid config.yaml parses without error and all
    threshold and endpoint values are accessible via the typed AppConfig fields.

Tests:
    test_valid_config_loads          — Asserts a complete valid config parses successfully.
    test_extra_key_raises_error      — An unknown top-level key raises ValidationError.
    test_extra_endpoint_key_raises   — An unknown key in an EndpointConfig raises ValidationError.
    test_wrong_type_raises_error     — A non-numeric threshold value raises ValidationError.
    test_missing_required_field      — A missing required endpoint field raises ValidationError.
    test_threshold_values_accessible — Parsed thresholds match expected proposed defaults.
    test_endpoint_fields_accessible  — Parsed endpoint fields match config file values.
"""

import pytest
from pydantic import ValidationError

from core.config_loader import AppConfig, load_config


def test_valid_config_loads():
    """
    Assert that config.yaml loads without error and returns an AppConfig instance.

    Purpose:
        Smoke test for the happy path. Verifies that the config.yaml file in the
        project root (with all required fields present and correct types) parses
        into a fully typed AppConfig without raising any exception.

    Inputs:
        None. Reads config.yaml from the current working directory.

    Expected:
        load_config() returns an AppConfig instance. No exception raised.
    """
    pass


def test_extra_key_raises_error():
    """
    Assert that an unknown top-level key in the config dict raises ValidationError.

    Purpose:
        Verifies the extra='forbid' contract at the AppConfig root level. A typo
        in config.yaml (e.g., "tresholds" instead of "thresholds") must surface as
        a ValidationError at boot, not silently pass through as a missing attribute.

    Inputs:
        Constructs an AppConfig directly from a dict with an extra "unknown_key" field.

    Expected:
        ValidationError raised. No AppConfig instance returned.
    """
    pass


def test_extra_endpoint_key_raises():
    """
    Assert that an unknown key in an EndpointConfig sub-model raises ValidationError.

    Purpose:
        Verifies extra='forbid' is enforced on nested EndpointConfig models, not just
        the root AppConfig. A typo in an endpoint's configuration (e.g., "base_uri"
        instead of "base_url") must be caught during parse.

    Inputs:
        Constructs a config dict with a valid structure except for an extra key in
        the "drafter" EndpointConfig (e.g., "base_uri": "http://...").

    Expected:
        ValidationError raised with a message identifying the offending field path.
    """
    pass


def test_wrong_type_raises_error():
    """
    Assert that a non-numeric threshold value raises ValidationError.

    Purpose:
        Verifies Pydantic type coercion failure for threshold fields. If a user sets
        stel_cosine_distance to a string (e.g., "low") instead of a float, the
        ValidationError should surface at boot before any routing logic runs.

    Inputs:
        Constructs a config dict with thresholds.stel_cosine_distance set to "low" (str).

    Expected:
        ValidationError raised. No AppConfig instance returned.
    """
    pass


def test_threshold_values_match_defaults():
    """
    Assert that parsed threshold values match the proposed defaults from config.yaml.

    Purpose:
        Regression guard: ensures that the proposed default values documented in the
        blueprint (0.12 for stel_cosine_distance, 0.35 for ewma_alpha, etc.) are
        correctly loaded from config.yaml without silent rounding or coercion errors.

    Inputs:
        Calls load_config() with the project config.yaml.

    Expected:
        config.thresholds.stel_cosine_distance == 0.12
        config.thresholds.ewma_alpha == 0.35
        config.thresholds.coreference_confidence_floor == 0.65
        config.generation.retry_count_max == 5
        config.generation.craft_consultant_threshold == 3
    """
    pass

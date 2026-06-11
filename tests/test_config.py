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
    test_threshold_values_match_defaults — Parsed thresholds match expected proposed defaults.
    test_endpoint_config_accepts_new_fields — Sprint 2 EndpointConfig fields parse.
    test_model_validate_retry_cap_parses — Global generation keys parse with defaults + overrides.
"""

import copy

import pytest
import yaml
from pydantic import ValidationError

from core.config_loader import AppConfig, EndpointConfig, load_config


def _valid_config_dict() -> dict:
    """Load config.yaml as a plain dict for mutation-based negative tests."""
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


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
    config = load_config()
    assert isinstance(config, AppConfig)


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
    raw = _valid_config_dict()
    raw["unknown_key"] = "typo"
    with pytest.raises(ValidationError):
        AppConfig(**raw)


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
    raw = _valid_config_dict()
    raw["endpoints"]["drafter"]["base_uri"] = "http://localhost:9999/v1"
    with pytest.raises(ValidationError) as exc_info:
        AppConfig(**raw)
    assert "base_uri" in str(exc_info.value)


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
    raw = _valid_config_dict()
    raw["thresholds"]["stel_cosine_distance"] = "low"
    with pytest.raises(ValidationError):
        AppConfig(**raw)


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
    """
    config = load_config()
    assert config.thresholds.stel_cosine_distance == 0.12
    assert config.thresholds.ewma_alpha == 0.35


def test_endpoint_config_accepts_new_fields():
    """
    Assert the three Sprint 2 EndpointConfig fields parse correctly.

    Purpose:
        Sprint 2 Tier 1 contract: tokenizer_family, supports_concurrent_critics,
        and grammar_constraint_strategy must be accepted by EndpointConfig with
        extra='forbid' still in force.

    Inputs:
        Constructs an EndpointConfig directly with all seven fields populated.

    Expected:
        The instance carries the three new fields with their given values.
    """
    ep = EndpointConfig(
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        model_name="llama3.3:70b",
        supports_inference_antislop=False,
        tokenizer_family="tiktoken",
        supports_concurrent_critics=True,
        grammar_constraint_strategy="gbnf",
    )
    assert ep.tokenizer_family == "tiktoken"
    assert ep.supports_concurrent_critics is True
    assert ep.grammar_constraint_strategy == "gbnf"


def test_model_validate_retry_cap_parses():
    """
    Assert model_validate_retry_cap parses with its default and accepts an override.

    Purpose:
        Sprint 2 begins consuming model_validate_retry_cap inside call_llm_structured.
        The key must default to 3 when absent from config.yaml and accept an explicit
        integer override. headless_mode must likewise default to False.

    Inputs:
        Parses config dicts with the key absent, then with an explicit override.

    Expected:
        Default 3 when absent; override value respected; headless_mode defaults False.
    """
    raw = _valid_config_dict()
    raw.pop("model_validate_retry_cap", None)
    raw.pop("headless_mode", None)
    config = AppConfig(**raw)
    assert config.model_validate_retry_cap == 3
    assert config.headless_mode is False

    raw_override = _valid_config_dict()
    raw_override["model_validate_retry_cap"] = 5
    assert AppConfig(**raw_override).model_validate_retry_cap == 5

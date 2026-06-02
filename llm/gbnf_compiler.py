"""
llm/gbnf_compiler.py

DIY Pydantic Schema → GBNF Grammar String Compiler.

Purpose:
    Provides a lightweight, dependency-free converter that maps Pydantic model
    schemas to GBNF (Grammar-Based Newline Format) strings for grammar-constrained
    sampling on endpoints that support it (e.g., llama.cpp-based servers via Ollama).

    Grammar-constrained sampling enforces the output schema at the sampler level,
    preventing the model from generating JSON that violates the FailureObject schema.
    This is particularly important for node_adversarial_critics where three concurrent
    critic calls must all produce valid FailureObject instances.

    The compiler is a DIY implementation that handles the subset of JSON Schema
    features used by FailureObject and other FSM schemas:
    - object with required string fields
    - array of objects
    - enum string fields
    - optional fields

    This avoids dependency on heavy external grammar libraries (e.g., lark, guidance)
    that would increase package size significantly for a capability used only on
    GBNF-supporting endpoints.

    STATUS: Stub for Sprint 1. Implemented in Sprint 2.
    Endpoints using grammar_constraint_strategy="json_mode" do not require this module.

Architecture role:
    - Called by node_adversarial_critics when EndpointConfig.grammar_constraint_strategy
      is "gbnf" to generate the grammar string included in the HTTP request payload.
    - Not called for "json_mode" endpoints — those use the vendor JSON mode flag.
    - Output is passed as the grammar parameter to call_llm().
"""

from typing import Type

from pydantic import BaseModel


def json_schema_to_gbnf(model: Type[BaseModel]) -> str:
    """
    Convert a Pydantic BaseModel class to a GBNF grammar string.

    Purpose:
        Extracts the JSON schema from the Pydantic model using model.model_json_schema()
        and converts it to a GBNF grammar that constrains LLM sampling to outputs
        valid against the schema. The generated GBNF is passed as the grammar field
        in the HTTP request payload for llama.cpp-compatible endpoints.

        Supports the FSM's required schema features:
        - Top-level object with required string properties.
        - String properties with optional enum constraints.
        - Array properties containing objects.
        - Optional (nullable) properties.

        Does NOT attempt to support all possible JSON Schema features — only the
        subset required by FailureObject and similar narrow schemas used in this system.

        STATUS: Sprint 1 stub. Returns an empty string (no grammar constraint active).
        Full implementation is Sprint 2.

    Inputs:
        model: Type[BaseModel] — a Pydantic model class (not an instance).
            The class is queried via model.model_json_schema() to extract the schema.

    Outputs:
        str: A GBNF grammar string that constrains sampling to valid schema output.
            Currently returns "" (Sprint 1 stub — no grammar constraint active).
    """
    return ""

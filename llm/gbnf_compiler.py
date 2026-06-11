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

    Inputs:
        model: Type[BaseModel] — a Pydantic model class (not an instance).
            The class is queried via model.model_json_schema() to extract the schema.

    Outputs:
        str: A GBNF grammar string that constrains sampling to valid schema output.

    Raises:
        ValueError: If the schema uses a JSON Schema feature outside the supported subset.
    """
    return _json_schema_to_gbnf(model.model_json_schema())


def _json_schema_to_gbnf(schema: dict) -> str:
    """
    Convert a JSON Schema dict (Pydantic model_json_schema() output) to a GBNF grammar.

    Pure and deterministic: identical schema in → byte-identical grammar string out,
    so tests can assert against a golden string.

    Supported subset:
        - Top-level object with properties (required and optional).
        - string properties, with optional enum constraints (string enums only).
        - number / integer / boolean properties.
        - Nullable properties (anyOf [X, null] — Pydantic's Optional encoding).
        - array properties with object or scalar items.
        - $ref / $defs resolution for nested object models.

    Property order follows the schema's "properties" insertion order (Pydantic
    preserves field declaration order). Required and optional properties are all
    emitted as mandatory keys in declaration order — optional properties accept
    null as a value rather than being omitted. This keeps the grammar regular
    (no combinatorial key-presence alternation) while remaining schema-valid for
    Pydantic validation, which treats explicit null as the absent-optional value.
    """
    if schema.get("type") != "object":
        raise ValueError("Top-level schema must be an object.")

    defs = schema.get("$defs", {})
    rules: dict[str, str] = {}

    root_body = _object_rule_body(schema, defs, rules, prefix="root-obj")
    rules["root"] = f"ws {root_body} ws"

    # Shared terminal rules — emitted once, in a fixed order, after the structural rules.
    rules["string"] = r'"\"" char* "\"" ws'
    rules["char"] = r'[^"\\\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F])'
    rules["number"] = r'"-"? ([0-9] | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [-+]? [0-9]+)? ws'
    rules["integer"] = r'"-"? ([0-9] | [1-9] [0-9]*) ws'
    rules["boolean"] = r'("true" | "false") ws'
    rules["null"] = r'"null" ws'
    rules["ws"] = r'[ \t\n\r]*'

    return "\n".join(f"{name} ::= {body}" for name, body in rules.items()) + "\n"


def _object_rule_body(schema: dict, defs: dict, rules: dict, prefix: str) -> str:
    """Emit the GBNF body for one object schema; registers sub-rules in `rules`."""
    properties: dict = schema.get("properties", {})
    if not properties:
        return '"{" ws "}"'

    parts: list[str] = ['"{" ws']
    for i, (prop_name, prop_schema) in enumerate(properties.items()):
        value_ref = _value_rule(prop_schema, defs, rules, prefix=f"{prefix}-{_sanitize(prop_name)}")
        sep = '"," ws ' if i > 0 else ""
        parts.append(f'{sep}"\\"{prop_name}\\"" ws ":" ws {value_ref}')
    parts.append('"}"')
    return " ".join(parts)


def _value_rule(prop_schema: dict, defs: dict, rules: dict, prefix: str) -> str:
    """Return a GBNF expression (rule reference or inline) for one property schema."""
    # $ref → resolve against $defs and emit a dedicated object rule.
    if "$ref" in prop_schema:
        ref_name = prop_schema["$ref"].split("/")[-1]
        if ref_name not in defs:
            raise ValueError(f"Unresolvable $ref: {prop_schema['$ref']}")
        rule_name = _sanitize(ref_name.lower())
        if rule_name not in rules:
            rules[rule_name] = "PENDING"  # placeholder guards against $ref cycles
            rules[rule_name] = _object_rule_body(defs[ref_name], defs, rules, prefix=rule_name) + " ws"
        return rule_name

    # anyOf [X, {"type": "null"}] — Pydantic Optional[X] encoding.
    if "anyOf" in prop_schema:
        non_null = [s for s in prop_schema["anyOf"] if s.get("type") != "null"]
        has_null = len(non_null) != len(prop_schema["anyOf"])
        if len(non_null) != 1:
            raise ValueError(f"Unsupported anyOf composition in schema at {prefix}.")
        inner = _value_rule(non_null[0], defs, rules, prefix=prefix)
        if has_null:
            rule_name = f"{prefix}-opt"
            rules[rule_name] = f"{inner} | null"
            return rule_name
        return inner

    prop_type = prop_schema.get("type")

    if prop_type == "string":
        enum_values = prop_schema.get("enum")
        if enum_values:
            if not all(isinstance(v, str) for v in enum_values):
                raise ValueError(f"Only string enums are supported (at {prefix}).")
            rule_name = f"{prefix}-enum"
            alternatives = " | ".join(f'"\\"{v}\\"" ws' for v in enum_values)
            rules[rule_name] = alternatives
            return rule_name
        return "string"

    if prop_type == "number":
        return "number"
    if prop_type == "integer":
        return "integer"
    if prop_type == "boolean":
        return "boolean"
    if prop_type == "null":
        return "null"

    if prop_type == "array":
        items_schema = prop_schema.get("items", {})
        item_ref = _value_rule(items_schema, defs, rules, prefix=f"{prefix}-item")
        rule_name = f"{prefix}-arr"
        rules[rule_name] = f'"[" ws ({item_ref} ("," ws {item_ref})*)? "]" ws'
        return rule_name

    if prop_type == "object":
        rule_name = f"{prefix}-obj"
        rules[rule_name] = _object_rule_body(prop_schema, defs, rules, prefix=rule_name) + " ws"
        return rule_name

    raise ValueError(f"Unsupported JSON Schema type {prop_type!r} at {prefix}.")


def _sanitize(name: str) -> str:
    """GBNF rule names allow [a-zA-Z0-9-]; replace anything else with '-'."""
    return "".join(c if c.isalnum() or c == "-" else "-" for c in name)

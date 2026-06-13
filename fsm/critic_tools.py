"""
fsm/critic_tools.py

Critic tool registry — OpenAI function-calling schemas + async dispatcher for
the memory-retrieval tools available to the adversarial critics.

Purpose:
    Defines CRITIC_TOOL_SCHEMAS, the OpenAI-compliant tool declarations the
    critic agent loop advertises to the LLM, and execute_critic_tool, the
    single dispatch point that maps a tool call back onto the real memory
    functions (RAPTOR summaries, Graphiti point-in-time subgraph, ChromaDB
    flavor vectors).

    The schemas expose ONLY the parameters the LLM is allowed to decide
    (hierarchy levels, entity IDs, hop depth, query text, result count).
    Runtime-scoped arguments — db_path, scene_id, active_event_id, and the
    current chapter exclusion — are injected from a ToolContext the FSM node
    constructs, so the model can never point a tool at the wrong scene or
    leak the in-progress chapter back into its own retrieval.

    Every tool result is serialized to a JSON string for the tool-role
    message. Failures (unknown tool, bad arguments, backend exceptions) are
    returned as {"error": "..."} JSON — tool failures are observations for
    the agent to reason over, never FSM crashes.

Architecture role:
    - Consumed by node_adversarial_critics' bounded tool-calling loop.
    - Pure adapter layer: no FSM state access, no LLM calls, no retries —
      backend degradation policies live in the memory modules themselves.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from memory import chroma_client, graphiti_client, raptor

CRITIC_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_raptor_summaries",
            "description": (
                "Retrieve hierarchical RAPTOR narrative summaries for the current "
                "scene. Use this to check the established big-picture context — "
                "what has already happened at the chapter, arc, or global level — "
                "before flagging a continuity or pacing failure. Returns one "
                "summary string per requested level (empty string if that level "
                "has no summary yet, which is normal early in the story)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "levels": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["beat", "scene", "chapter", "arc", "global"],
                        },
                        "description": (
                            "Which hierarchy levels to retrieve, from most local "
                            "to most global: 'beat', 'scene', 'chapter', 'arc', "
                            "'global'. E.g. ['chapter', 'arc'] to verify a fact "
                            "against mid-range story memory."
                        ),
                    },
                },
                "required": ["levels"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_point_in_time_subgraph",
            "description": (
                "Query the temporal knowledge graph for facts valid at the "
                "current story moment. Use this to verify hard continuity claims "
                "— relationships, possessions, locations, character states — "
                "before declaring a contradiction. Returns FACT edges between "
                "entities that are valid right now (facts invalidated by later "
                "events are excluded). An empty list means no recorded facts "
                "touch those entities, not that the draft is wrong."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Seed entity IDs (characters, items, places) to start "
                            "the graph traversal from — the entities involved in "
                            "the fact you are verifying."
                        ),
                    },
                    "max_hops": {
                        "type": "integer",
                        "default": 2,
                        "description": (
                            "Maximum traversal depth from the seed entities. Use 1 "
                            "for direct facts only, 2 (default) to include "
                            "second-degree relationships."
                        ),
                    },
                },
                "required": ["entity_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_flavor_vectors",
            "description": (
                "Semantic search over previously committed prose (excluding the "
                "chapter currently being drafted). Use this to check how a motif, "
                "voice, or detail was actually rendered earlier — e.g. to verify "
                "a character's established speech patterns or whether a described "
                "detail matches prior prose. Returns the nearest prose excerpts "
                "with their scene/chapter metadata and cosine distance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query_text": {
                        "type": "string",
                        "description": (
                            "Natural-language description of the prose you are "
                            "looking for (a motif, a line of dialogue, a scene "
                            "detail). Embedded and matched against committed beats."
                        ),
                    },
                    "n_results": {
                        "type": "integer",
                        "default": 5,
                        "description": "Maximum number of prose excerpts to return.",
                    },
                },
                "required": ["query_text"],
            },
        },
    },
]


@dataclass
class ToolContext:
    """
    Runtime-scoped arguments injected into every tool call — the parameters
    the LLM must not control. Built by the critics node from FSM state.
    """

    db_path: Path
    scene_id: str
    active_event_id: str
    chapter_id: Optional[str]


async def execute_critic_tool(name: str, args: dict, ctx: ToolContext) -> str:
    """
    Dispatch one critic tool call to the underlying memory function.

    Purpose:
        Merges the LLM-provided arguments with the runtime ToolContext and
        invokes the matching memory function (awaiting async backends).
        The result is JSON-serialized for the tool-role message. Any failure
        — unknown tool name, missing/invalid arguments, or an exception from
        the memory layer — is captured and returned as a JSON error object so
        the agent loop observes the failure instead of crashing the FSM.

    Inputs:
        name: str — tool name as emitted by the LLM (must match a schema name).
        args: dict — parsed JSON arguments from the LLM tool call.
        ctx: ToolContext — runtime-only arguments (db_path, scene_id,
            active_event_id, chapter_id) supplied by the critics node.

    Outputs:
        str: JSON string of the tool result, or '{"error": "..."}' on failure.
    """
    try:
        result: Any
        if name == "get_raptor_summaries":
            result = raptor.get_raptor_summaries(
                db_path=ctx.db_path,
                scene_id=ctx.scene_id,
                levels=args["levels"],
            )
        elif name == "query_point_in_time_subgraph":
            result = await graphiti_client.query_point_in_time_subgraph(
                entity_ids=args["entity_ids"],
                active_event_id=ctx.active_event_id,
                max_hops=args.get("max_hops", 2),
            )
        elif name == "query_flavor_vectors":
            result = chroma_client.query_flavor_vectors(
                query_text=args["query_text"],
                n_results=args.get("n_results", 5),
                exclude_chapter_id=ctx.chapter_id,
            )
        else:
            return json.dumps({"error": f"Unknown tool: {name!r}"})
        return json.dumps(result, default=str)
    except Exception as exc:  # noqa: BLE001 — tool failures are agent observations
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

"""
llm/__init__.py

LLM inference network boundary package.

Purpose:
    Contains all code that crosses the network boundary to inference endpoints.
    The system is API-agnostic: all endpoints are addressed via the OpenAI HTTP API
    spec, allowing any compatible inference server (Ollama, vLLM, OpenAI, etc.) to
    be configured without code changes. Routing is determined entirely by config.yaml.

    Modules:
    call_llm.py      — Async HTTP wrapper with stream=True support and 3-attempt retry.
    tokenizer.py     — Per-endpoint token counting (tiktoken / AutoTokenizer / char÷4).
    gbnf_compiler.py — DIY Pydantic schema → GBNF string converter for grammar-constrained
                       sampling endpoints.
"""

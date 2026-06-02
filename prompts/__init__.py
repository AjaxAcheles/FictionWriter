"""
prompts/__init__.py

Jinja2 XML prompt template package.

Purpose:
    All LLM instructions for the FictionWriter FSM are defined here as Jinja2 XML
    templates (.xml.j2 files). No prompt text is hardcoded in any Python file.
    This decoupling means prompt tuning requires no Python edits and no node
    redeployment — only template file edits.

    PromptLoader (prompt_loader.py) is the shared utility that loads and renders
    templates by node name at call time. Each node calls PromptLoader at the start
    of its LLM request, passing the Jinja2 context variables appropriate to that node.

    Template naming convention:
    - node_{name}.xml.j2        — Primary generation prompt for that node.
    - node_{name}_{variant}.xml.j2 — Variant prompt (e.g., node_plan_beat_pad.xml.j2
      for the dedicated PAD Translation Pipeline LLM call within node_plan_beat).
    - node_adversarial_critics_{type}.xml.j2 — One template per critic type
      (continuity, dialogue, pacing).
"""

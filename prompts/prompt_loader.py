"""
prompts/prompt_loader.py

Jinja2 XML Prompt Template Loader.

Purpose:
    Provides the shared PromptLoader utility used by every LangGraph node that
    makes LLM calls. Loads Jinja2 XML templates from the prompts/ directory by
    node name and renders them with the provided context variables at call time.

    Template loading uses Jinja2's FileSystemLoader pointed at the prompts/ directory.
    Templates are cached in the Jinja2 environment after first load to avoid repeated
    disk reads during high-frequency generation sessions (multiple beats per chapter).

    The rendered output is an XML string that is passed directly as the LLM system
    or user message content. The XML structure (with tags like <context>, <instruction>,
    <constraints>) is a design convention — no XML parsing occurs at the Python level;
    the LLM receives and interprets the XML as structured text.

Architecture role:
    - Instantiated once per node module (at module level) or once per node invocation
      (either pattern is acceptable; module-level is preferred for caching efficiency).
    - Called by every node_*.py that makes an LLM call before constructing the messages
      array for call_llm().
    - Template files must exist in the prompts/ directory for the corresponding node
      to function. Missing templates raise a TemplateNotFound error at render time.
"""

from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

PROMPTS_DIR = Path(__file__).parent


class PromptLoader:
    """
    Cached Jinja2 template loader for FSM node prompts.

    Purpose:
        Wraps a Jinja2 Environment with FileSystemLoader pointing to the prompts/
        directory. Exposes a single load_and_render() method that loads a template
        by filename and renders it with the provided context variables.

        The Jinja2 environment caches parsed templates after first access, so
        repeated calls to load_and_render() for the same template within a session
        incur only the rendering cost (variable substitution), not the parse cost.

    Usage:
        loader = PromptLoader()
        rendered = loader.load_and_render("node_draft_prose.xml.j2", {
            "beat_constraints": "...",
            "character_states": "...",
            ...
        })
    """

    def __init__(self, prompts_dir: Optional[Path] = None) -> None:
        """
        Initialize the Jinja2 environment with a FileSystemLoader.

        Purpose:
            Creates the Jinja2 Environment with FileSystemLoader pointed at the
            prompts/ directory. Sets undefined=StrictUndefined so that missing
            template variables raise an UndefinedError at render time rather than
            silently substituting empty strings (which would corrupt prompts).

        Inputs:
            prompts_dir: Optional[Path] — directory containing .xml.j2 templates.
                Defaults to the prompts/ directory (same directory as this file).

        Outputs:
            None. Side effect: self.env is initialized and ready for template loading.
        """
        pass

    def load_and_render(self, template_name: str, context: dict) -> str:
        """
        Load a Jinja2 template by filename and render it with the given context.

        Purpose:
            Retrieves the named template from the Jinja2 environment (loading from
            disk on first access, cached thereafter). Renders the template with the
            provided context variables and returns the rendered XML string.

            template_name must be the filename only (e.g., "node_plan_global.xml.j2"),
            not a full path — the FileSystemLoader handles path resolution.

        Inputs:
            template_name: str — the .xml.j2 filename to load and render.
            context: dict — Jinja2 template variables to substitute. Keys must match
                all {{ variable_name }} references in the template or an UndefinedError
                is raised (StrictUndefined mode).

        Outputs:
            str: The fully rendered XML string, ready to be used as an LLM message
                content string.

        Raises:
            jinja2.TemplateNotFound: If template_name does not exist in prompts_dir.
            jinja2.UndefinedError: If a variable referenced in the template is missing
                from the context dict.
        """
        pass

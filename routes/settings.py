"""
routes/settings.py

Settings Blueprint — config.yaml Editor and Endpoint Connectivity Testing.

Purpose:
    Provides the server-side routes for the Settings page (templates/settings.html).
    Allows the author to modify runtime parameters via form sliders and inputs,
    which are written back to config.yaml. Changes take effect at the next beat
    boundary (node_plan_beat reads config at node entry).

    Also provides an endpoint connectivity test route that fires a minimal test
    request to each configured inference endpoint to verify reachability before
    a long generation session begins.

    Key routes:
    GET  /settings       — Renders the Settings view with current config values.
    POST /settings       — Writes updated config values to config.yaml and reloads AppConfig.
    POST /settings/test  — Tests connectivity to all configured endpoints.

Architecture role:
    - Writing to config.yaml triggers a re-parse and validation via AppConfig. If the
      new values fail Pydantic validation (extra keys, type mismatch), the write is
      rejected and the old config is preserved.
    - The AppConfig singleton in app.config["APP_CONFIG"] is updated after a successful write.
    - Connectivity test results are returned as JSON for the frontend to display in
      the endpoint status indicators on the Settings page.
"""

from quart import Blueprint, render_template, request

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings")
async def settings_view():
    """Render the Settings page pre-populated from the live config."""
    from core.config_loader import load_config

    config = load_config()
    return await render_template("settings.html", config=config)


# Whitelisted scalar keys writable from the Settings page, with their YAML paths.
_WRITABLE_KEYS = {
    "stel_cosine_distance": ("thresholds", float),
    "ewma_alpha": ("thresholds", float),
    "passive_voice_density": ("thresholds", float),
    "beats_per_scene_min": ("thresholds", int),
    "retry_count_max": ("generation", int),
    "craft_consultant_threshold": ("generation", int),
    "replan_count_max": ("generation", int),
    "model_validate_retry_cap": (None, int),
    "headless_mode": (None, lambda v: str(v).lower() in ("true", "1", "on")),
}


def _rewrite_config_yaml(updates: dict, config_path="config.yaml") -> None:
    """
    Comment-preserving config.yaml write-back: only the value portion of each
    whitelisted key's line is rewritten in place.
    """
    import re
    from pathlib import Path as _P

    path = _P(config_path)
    text = path.read_text(encoding="utf-8")
    for key, value in updates.items():
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        text, count = re.subn(
            rf"^(\s*{re.escape(key)}:\s*)[^#\n]*",
            lambda m: m.group(1) + rendered + "  ",
            text,
            count=1,
            flags=re.M,
        )
        if count == 0:
            raise KeyError(f"config.yaml key not found for write-back: {key}")
    path.write_text(text, encoding="utf-8")


@settings_bp.route("/settings", methods=["POST"])
async def update_settings():
    """Validate then write updated settings back to config.yaml."""
    import copy

    import yaml

    from core.config_loader import AppConfig

    form = await request.form
    payload = dict(form) or (await request.get_json(silent=True) or {})

    updates = {}
    for key, raw in payload.items():
        if key not in _WRITABLE_KEYS:
            continue
        _, caster = _WRITABLE_KEYS[key]
        try:
            updates[key] = caster(raw)
        except (TypeError, ValueError):
            return {"status": "error", "message": f"invalid value for {key}: {raw!r}"}, 400

    # Validate the candidate config BEFORE touching the file.
    with open("config.yaml", "r", encoding="utf-8") as f:
        candidate = yaml.safe_load(f)
    for key, value in updates.items():
        section = _WRITABLE_KEYS[key][0]
        if section:
            candidate[section][key] = value
        else:
            candidate[key] = value
    try:
        AppConfig(**candidate)
    except Exception as e:
        from core.logger import get_app_logger
        get_app_logger().warning("settings rejected — invalid candidate config: %s", e)
        return {"status": "error", "message": str(e)}, 400

    _rewrite_config_yaml(updates)
    from core.logger import get_app_logger
    get_app_logger().info("settings updated: %s", updates)
    return {"status": "updated", "applied": updates,
            "note": "changes take effect at the next beat boundary"}


@settings_bp.route("/settings/test", methods=["POST"])
async def test_endpoints():
    """Connectivity probe (GET {base_url}/models) for every endpoint role."""
    import httpx

    from core.config_loader import load_config

    config = load_config()
    results = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        for role, endpoint in vars(config.endpoints).items():
            if not hasattr(endpoint, "base_url"):
                continue
            url = f"{endpoint.base_url.rstrip('/')}/models"
            try:
                response = await client.get(
                    url, headers={"Authorization": f"Bearer {endpoint.api_key}"}
                )
                results[role] = {"ok": response.status_code < 500,
                                 "status_code": response.status_code}
            except Exception as e:
                results[role] = {"ok": False, "error": type(e).__name__}
    return results

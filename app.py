"""
app.py

Entry point for the FictionWriter Quart async server.

Purpose:
    Bootstraps the entire application: loads and validates AppConfig via core/config_loader.py,
    initializes all persistent memory stores via core/runtime.py, registers all route blueprints,
    and starts the Quart ASGI server. All route handlers and background SSE generators are async,
    sharing the same event loop as LangGraph node functions to prevent deadlocks.

Architecture role:
    - Quart (not Flask) is mandatory here. Quart's native async/await support allows LangGraph's
      async node functions and the SSE streaming endpoint to run on the same event loop without
      blocking. Synchronous Flask would deadlock when awaiting LangGraph graph execution.
    - Blueprints are registered here; business logic lives entirely in the routes/ package.
    - POST /control/reset (registered via routes/control.py) calls runtime.init_resources()
      to wipe and reinitialize all file-based stores without a server restart.

Usage:
    uv run app.py
"""

from pathlib import Path

from quart import Quart

from core.config_loader import load_config
from core.logger import configure_log_level
from core.runtime import init_resources
from routes.dashboard import dashboard_bp
from routes.control import control_bp


def create_app(config_path: Path = Path("config.yaml")) -> Quart:
    """
    Application factory.

    Purpose:
        Constructs and configures the Quart application instance. Loads AppConfig,
        configures logging, registers Sprint-1-ready blueprints, and schedules
        async resource initialization via before_serving.

    Inputs:
        config_path: Path to config.yaml. Defaults to config.yaml in the working
            directory. Overridable in tests to point at a fixture config.

    Outputs:
        A fully configured Quart application instance ready to serve requests.

    Notes:
        init_resources() runs inside before_serving (not here) so it executes
        within the event loop — required once init_resources uses aiosqlite.
        Additional blueprints (alignment, settings, codex) are registered in
        Sprint 4 when their route handlers are implemented.
    """
    app = Quart(__name__)

    config = load_config(config_path)
    configure_log_level(config.log_level)
    app.config["APP_CONFIG"] = config

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(control_bp)

    @app.before_serving
    async def startup() -> None:
        await init_resources(app.config["APP_CONFIG"])

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

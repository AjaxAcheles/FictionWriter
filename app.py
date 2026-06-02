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

from quart import Quart

from core.config_loader import load_config
from core.runtime import init_resources
from routes.dashboard import dashboard_bp
from routes.alignment import alignment_bp
from routes.settings import settings_bp
from routes.control import control_bp
from routes.codex import codex_bp


def create_app() -> Quart:
    """
    Application factory.

    Purpose:
        Constructs and configures the Quart application instance. Loads AppConfig,
        initializes all memory stores, and registers all route blueprints.

    Inputs:
        None — reads config.yaml and .env from the working directory.

    Outputs:
        A fully configured Quart application instance ready to serve requests.

    Notes:
        init_resources() is called synchronously here during factory construction.
        It is also reachable at runtime via POST /control/reset for a clean slate
        without restarting the server process.
    """
    app = Quart(__name__)

    app.config["APP_CONFIG"] = load_config()

    init_resources(app.config["APP_CONFIG"])

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(alignment_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(control_bp)
    app.register_blueprint(codex_bp)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

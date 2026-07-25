"""HTTP API adapter: FastAPI app, routers and dependency wiring."""

from src.adapters.api.app import app, create_app

__all__ = ["app", "create_app"]

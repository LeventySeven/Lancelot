"""FastAPI import-surface compatibility. Goal (WBS U38): an existing FastAPI
app's import block runs unchanged. `FastAPI = App` is the headline alias."""
from lancelot.app import App
from lancelot.routing import APIRouter

FastAPI = App

__all__ = ["FastAPI", "APIRouter"]

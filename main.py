"""Service entrypoint: serves the local metadata catalog HTTP API."""
from metadata_catalog.api import app

__all__ = ["app"]

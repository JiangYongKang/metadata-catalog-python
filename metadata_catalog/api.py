"""FastAPI HTTP layer exposing the catalog."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .catalog import MetadataCatalog
from .errors import (
    AssetNotFoundError,
    CatalogError,
    ColumnNotFoundError,
    ConcurrentUpdateError,
    LineageCycleError,
)
from .impact import analyze_impact


class ColumnIn(BaseModel):
    name: str
    data_type: str = ""
    description: str = ""


class AssetIn(BaseModel):
    asset_id: str
    name: str = ""
    asset_type: str = "table"
    columns: list[ColumnIn] = Field(default_factory=list)
    description: str = ""
    expected_version: int | None = None
    change_note: str = ""


class LineageIn(BaseModel):
    upstream: str
    downstream: str
    upstream_column: str | None = None
    downstream_column: str | None = None


def _asset_dict(asset) -> dict:
    return {
        "asset_id": asset.id,
        "name": asset.name,
        "asset_type": asset.asset_type,
        "description": asset.description,
        "current_version": asset.current_version,
        "columns": [{"name": c.name, "data_type": c.data_type,
                     "description": c.description} for c in asset.columns],
        "created_at": asset.created_at,
        "updated_at": asset.updated_at,
    }


def create_app(catalog: MetadataCatalog | None = None) -> FastAPI:
    app = FastAPI(title="Local Metadata Catalog", version="0.1.0")
    app.state.catalog = catalog or MetadataCatalog()

    @app.exception_handler(CatalogError)
    async def _catalog_error_handler(request, exc: CatalogError):
        status = {
            AssetNotFoundError: 404,
            ColumnNotFoundError: 404,
            LineageCycleError: 422,
            ConcurrentUpdateError: 409,
        }.get(type(exc), 400)
        return JSONResponse(status_code=status,
                            content={"error": exc.to_dict()})

    @app.post("/assets")
    def register_asset(payload: AssetIn):
        cat = app.state.catalog
        asset = cat.register_asset(
            payload.asset_id,
            payload.name or payload.asset_id,
            payload.asset_type,
            [(c.name, c.data_type, c.description) for c in payload.columns],
            description=payload.description,
            expected_version=payload.expected_version,
            change_note=payload.change_note,
        )
        return _asset_dict(asset)

    @app.get("/assets")
    def list_assets():
        return [_asset_dict(a) for a in app.state.catalog.list_assets()]

    @app.get("/assets/{asset_id}")
    def get_asset(asset_id: str):
        return _asset_dict(app.state.catalog.get_asset(asset_id))

    @app.get("/assets/{asset_id}/versions")
    def list_versions(asset_id: str):
        versions = app.state.catalog.list_versions(asset_id)
        return [{
            "version": v.version, "name": v.name, "asset_type": v.asset_type,
            "description": v.description, "change_note": v.change_note,
            "created_at": v.created_at,
            "columns": [{"name": c.name, "data_type": c.data_type}
                        for c in v.columns],
        } for v in versions]

    @app.post("/lineage")
    def add_lineage(payload: LineageIn):
        edge = app.state.catalog.add_lineage(
            payload.upstream, payload.downstream,
            payload.upstream_column, payload.downstream_column)
        return {"upstream": edge.upstream, "downstream": edge.downstream,
                "upstream_column": edge.upstream_column,
                "downstream_column": edge.downstream_column,
                "column_level": edge.is_column_level}

    @app.get("/lineage")
    def list_lineage():
        return [{
            "upstream": e.upstream, "downstream": e.downstream,
            "upstream_column": e.upstream_column,
            "downstream_column": e.downstream_column,
            "column_level": e.is_column_level,
        } for e in app.state.catalog.list_lineage()]

    @app.get("/impact/{asset_id}")
    def impact(asset_id: str, direction: str = "downstream",
               max_depth: int | None = None):
        return analyze_impact(app.state.catalog, asset_id,
                              direction=direction, max_depth=max_depth).to_dict()

    @app.get("/")
    def root():
        return {"service": "metadata-catalog", "status": "ok"}

    return app


app = create_app()

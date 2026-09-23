"""可选的 FastAPI HTTP 门面：把目录能力暴露为本地 HTTP 接口。

所有写接口内部仍走 MetadataCatalog 的加锁逻辑；
业务拒绝（悬空引用/环/并发冲突）映射为可区分的 error code 与 4xx 状态码，
绝不静默成功。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .catalog import MetadataCatalog
from .exceptions import (
    AssetNotFoundError,
    ConcurrentUpdateError,
    CycleDetectedError,
    DanglingReferenceError,
    DuplicateColumnError,
)
from .models import Asset, Column, ColumnLineage, LineageEdge


class ColumnIn(BaseModel):
    name: str
    type: str = ""
    description: str = ""


class AssetIn(BaseModel):
    id: str = ""
    kind: str = ""
    name: str = ""
    columns: list[ColumnIn] = Field(default_factory=list)
    description: str = ""
    expected_version: int | None = None
    reason: str = ""


class ColumnLineageIn(BaseModel):
    upstream_column: str
    downstream_column: str


class LineageIn(BaseModel):
    upstream_id: str
    downstream_id: str
    columns: list[ColumnLineageIn] = Field(default_factory=list)
    description: str = ""


def _to_asset(payload: AssetIn) -> Asset:
    return Asset(
        id=payload.id,
        kind=payload.kind,
        name=payload.name,
        columns=tuple(Column(c.name, c.type, c.description) for c in payload.columns),
        description=payload.description,
    )


def _error_code(exc: Exception) -> tuple[int, str]:
    mapping = {
        DanglingReferenceError: (422, "dangling_reference"),
        CycleDetectedError: (422, "cycle_detected"),
        DuplicateColumnError: (422, "duplicate_column"),
        ConcurrentUpdateError: (409, "concurrent_update"),
        AssetNotFoundError: (404, "asset_not_found"),
    }
    for exc_type, (status, code) in mapping.items():
        if isinstance(exc, exc_type):
            return status, code
    return (400, "bad_request")


def create_app(catalog: MetadataCatalog | None = None) -> FastAPI:
    """构造 FastAPI 应用，可注入共享的 catalog 实例。"""
    app = FastAPI(title="metadata-catalog", version="0.1.0")
    app.state.catalog = catalog if catalog is not None else MetadataCatalog()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.put("/assets/{asset_id}")
    def put_asset(asset_id: str, payload: AssetIn) -> dict:
        if payload.id and payload.id != asset_id:
            raise HTTPException(status_code=400, detail={
                "error": "id_mismatch",
                "message": f"path id {asset_id!r} != body id {payload.id!r}",
            })
        asset = _to_asset(payload.model_copy(update={"id": asset_id}))
        try:
            result = app.state.catalog.register_asset(
                asset, expected_version=payload.expected_version, reason=payload.reason
            )
        except Exception as exc:  # noqa: BLE001 - 统一映射为可区分错误码
            status, code = _error_code(exc)
            raise HTTPException(status_code=status, detail={
                "error": code, "message": str(exc),
            }) from exc
        return {
            "asset_id": result.asset_id, "version": result.version,
            "created": result.created, "merged": result.merged,
            "detail": result.detail,
        }

    @app.get("/assets/{asset_id}")
    def get_asset(asset_id: str) -> dict:
        try:
            asset = app.state.catalog.get_asset(asset_id)
        except AssetNotFoundError as exc:
            raise HTTPException(status_code=404, detail={
                "error": "asset_not_found", "message": str(exc),
            }) from exc
        return {
            "id": asset.id, "kind": asset.kind, "name": asset.name,
            "description": asset.description,
            "version": app.state.catalog.get_version(asset_id),
            "columns": [
                {"name": c.name, "type": c.type, "description": c.description}
                for c in asset.columns
            ],
        }

    @app.get("/assets/{asset_id}/history")
    def get_history(asset_id: str) -> dict:
        try:
            revisions = app.state.catalog.history(asset_id)
        except AssetNotFoundError as exc:
            raise HTTPException(status_code=404, detail={
                "error": "asset_not_found", "message": str(exc),
            }) from exc
        return {
            "asset_id": asset_id,
            "revisions": [
                {
                    "version": rv.version,
                    "changed_at": rv.changed_at.isoformat(),
                    "reason": rv.reason,
                    "columns": [c.name for c in rv.asset.columns],
                }
                for rv in revisions
            ],
        }

    @app.put("/lineage")
    def put_lineage(payload: LineageIn) -> dict:
        edge = LineageEdge(
            upstream_id=payload.upstream_id,
            downstream_id=payload.downstream_id,
            columns=tuple(
                ColumnLineage(c.upstream_column, c.downstream_column)
                for c in payload.columns
            ),
            description=payload.description,
        )
        try:
            result = app.state.catalog.register_lineage(edge)
        except Exception as exc:  # noqa: BLE001
            status, code = _error_code(exc)
            raise HTTPException(status_code=status, detail={
                "error": code, "message": str(exc),
            }) from exc
        return {
            "edge": result.asset_id, "created": result.created,
            "merged": result.merged, "detail": result.detail,
        }

    @app.get("/impact/{asset_id}")
    def get_impact(
        asset_id: str,
        direction: str = "downstream",
        max_depth: int | None = None,
    ) -> dict:
        try:
            return app.state.catalog.impact(
                asset_id, direction, max_depth=max_depth
            )
        except AssetNotFoundError as exc:
            raise HTTPException(status_code=404, detail={
                "error": "asset_not_found", "message": str(exc),
            }) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={
                "error": "bad_request", "message": str(exc),
            }) from exc

    return app


app = create_app()

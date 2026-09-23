"""Catalog errors carrying stable, distinguishable machine-readable codes."""
from __future__ import annotations


class CatalogError(Exception):
    code = "catalog_error"

    def __init__(self, message: str = "", *, asset: str | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.asset = asset

    def to_dict(self) -> dict:
        data = {"code": self.code, "message": self.message}
        if self.asset is not None:
            data["asset"] = self.asset
        return data


class AssetNotFoundError(CatalogError):
    code = "asset_not_found"


class ColumnNotFoundError(CatalogError):
    code = "column_not_found"

    def __init__(self, message: str = "", *, asset: str | None = None, column: str | None = None):
        super().__init__(message, asset=asset)
        self.column = column

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.column is not None:
            data["column"] = self.column
        return data


class LineageCycleError(CatalogError):
    code = "lineage_cycle"

    def __init__(self, message: str = "", *, asset: str | None = None, cycle: list[str] | None = None):
        super().__init__(message, asset=asset)
        self.cycle = cycle or []

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["cycle"] = self.cycle
        return data


class ConcurrentUpdateError(CatalogError):
    code = "concurrent_update"

    def __init__(self, message: str = "", *, asset: str | None = None,
                 expected_version: int | None = None, actual_version: int | None = None):
        super().__init__(message, asset=asset)
        self.expected_version = expected_version
        self.actual_version = actual_version

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.expected_version is not None:
            data["expected_version"] = self.expected_version
        if self.actual_version is not None:
            data["actual_version"] = self.actual_version
        return data

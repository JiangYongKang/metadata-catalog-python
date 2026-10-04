"""Catalog errors carrying stable, distinguishable machine-readable codes."""
from __future__ import annotations

import math

# stable error codes (single source of truth)
ASSET_NOT_FOUND = "asset_not_found"
COLUMN_NOT_FOUND = "column_not_found"
LINEAGE_CYCLE = "lineage_cycle"
CONCURRENT_UPDATE = "concurrent_update"
INVALID_TIME = "invalid_time"
BATCH_REJECTED = "batch_rejected"


def _json_safe(value):
    """Coerce non-finite floats (NaN/Inf) to strings so error payloads stay
    JSON-serializable; lists are sanitized element-wise."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


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
    code = ASSET_NOT_FOUND


class ColumnNotFoundError(CatalogError):
    code = COLUMN_NOT_FOUND

    def __init__(self, message: str = "", *, asset: str | None = None, column: str | None = None):
        super().__init__(message, asset=asset)
        self.column = column

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.column is not None:
            data["column"] = self.column
        return data


class LineageCycleError(CatalogError):
    code = LINEAGE_CYCLE

    def __init__(self, message: str = "", *, asset: str | None = None, cycle: list[str] | None = None):
        super().__init__(message, asset=asset)
        self.cycle = cycle or []

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["cycle"] = self.cycle
        return data


class LineageTimeError(CatalogError):
    """Point-in-time / version reconstruction parameter or range error."""
    code = INVALID_TIME

    def __init__(self, message: str = "", *, asset: str | None = None,
                 reason: str | None = None, requested=None,
                 earliest: float | None = None, latest: float | None = None,
                 version: int | None = None):
        super().__init__(message, asset=asset)
        self.reason = reason
        self.requested = requested
        self.earliest = earliest
        self.latest = latest
        self.version = version

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.reason is not None:
            data["reason"] = self.reason
        if self.requested is not None:
            data["requested"] = _json_safe(self.requested)
        if self.earliest is not None:
            data["earliest"] = _json_safe(self.earliest)
        if self.latest is not None:
            data["latest"] = _json_safe(self.latest)
        if self.version is not None:
            data["version"] = self.version
        return data


class BatchRejectedError(CatalogError):
    """A lineage batch failed validation; the whole batch is rejected."""
    code = BATCH_REJECTED

    def __init__(self, message: str = "", *, batch_id: str | None = None,
                 failures: list | None = None):
        super().__init__(message)
        self.batch_id = batch_id
        self.failures = failures or []

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.batch_id is not None:
            data["batch_id"] = self.batch_id
        data["failures"] = [
            f.to_dict() if hasattr(f, "to_dict") else dict(f)
            for f in self.failures
        ]
        return data


class ConcurrentUpdateError(CatalogError):
    code = CONCURRENT_UPDATE

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

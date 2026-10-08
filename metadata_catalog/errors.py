"""Catalog errors carrying stable, distinguishable machine-readable codes."""
from __future__ import annotations

from dataclasses import dataclass


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


class SelfDependencyError(LineageCycleError):
    """A lineage edge that winds back onto the same asset.

    Still code 'lineage_cycle' (and an HTTP 422, and caught by
    ``except LineageCycleError``), but ``kind`` distinguishes the three
    shapes that must be rejected separately:

    - 'asset_self'           : whole asset -> itself (a -> a)
    - 'column_self'          : one column -> itself (a.x -> a.x)
    - 'asset_own_column'     : whole asset -> one of its own columns, in
                               either direction (a -> a.x / a.x -> a)

    An edge between two *different* columns of the same asset (a.x -> a.y)
    is none of these and stays legal.
    """

    KIND_ASSET_SELF = "asset_self"
    KIND_COLUMN_SELF = "column_self"
    KIND_ASSET_OWN_COLUMN = "asset_own_column"

    def __init__(self, message: str = "", *, asset: str | None = None,
                 kind: str, upstream_column: str | None = None,
                 downstream_column: str | None = None):
        super().__init__(message, asset=asset, cycle=[asset, asset]
                         if asset is not None else [])
        self.kind = kind
        self.upstream_column = upstream_column
        self.downstream_column = downstream_column

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["kind"] = self.kind
        data["upstream_column"] = self.upstream_column
        data["downstream_column"] = self.downstream_column
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


class InvalidTimePointError(CatalogError):
    """The as-of time parameter itself is not a legal point in time."""
    code = "invalid_time_point"

    def __init__(self, message: str = "", *, value=None):
        super().__init__(message)
        self.value = value

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["value"] = self.value
        return data


class TimePointOutOfRangeError(CatalogError):
    """The time is well-formed but cannot be reconstructed: it lies outside
    the audited lineage history window."""
    code = "time_point_out_of_range"

    def __init__(self, message: str = "", *, requested=None,
                 earliest: float | None = None, latest: float | None = None):
        super().__init__(message)
        self.requested = requested
        self.earliest = earliest
        self.latest = latest

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["requested"] = self.requested
        if self.earliest is not None:
            data["earliest"] = self.earliest
        if self.latest is not None:
            data["latest"] = self.latest
        return data


@dataclass(frozen=True)
class BatchFailure:
    """One rejected item inside a batch lineage change."""
    index: int
    code: str
    message: str
    upstream: str
    downstream: str
    upstream_column: str | None = None
    downstream_column: str | None = None
    operation: str = "add"
    # for lineage_cycle failures, the self-dependency shape:
    # 'asset_self' / 'column_self' / 'asset_own_column'; None for genuine
    # multi-node cycles and for non-cycle failures.
    kind: str | None = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "operation": self.operation,
            "code": self.code,
            "kind": self.kind,
            "message": self.message,
            "upstream": self.upstream,
            "downstream": self.downstream,
            "upstream_column": self.upstream_column,
            "downstream_column": self.downstream_column,
        }


class BatchLineageError(CatalogError):
    """A batch change was rejected as a whole; every offending item is listed."""
    code = "batch_lineage_rejected"

    def __init__(self, message: str = "", *, failures: list[BatchFailure] | None = None):
        super().__init__(message)
        self.failures = failures or []

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["failures"] = [f.to_dict() for f in self.failures]
        return data

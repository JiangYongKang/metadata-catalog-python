"""Core domain model: assets (with columns), lineage edges and asset versions."""
from __future__ import annotations

from dataclasses import dataclass, field
from time import time

LINEAGE_OP_ADD = "add"
LINEAGE_OP_REMOVE = "remove"


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str = ""
    description: str = ""


@dataclass(frozen=True)
class LineageEdge:
    """upstream -> downstream: downstream depends on upstream."""
    upstream: str
    downstream: str
    upstream_column: str | None = None
    downstream_column: str | None = None
    registered_at: float = field(default_factory=time)

    @property
    def is_column_level(self) -> bool:
        return self.upstream_column is not None or self.downstream_column is not None

    def key(self) -> tuple:
        return (self.upstream, self.downstream,
                self.upstream_column or "", self.downstream_column or "")

    def asset_key(self) -> tuple[str, str]:
        """Asset-level identity of the edge (ignores columns)."""
        return (self.upstream, self.downstream)


@dataclass(frozen=True)
class AssetVersion:
    version: int
    name: str
    asset_type: str
    columns: tuple[Column, ...]
    description: str
    created_at: float
    change_note: str = ""


@dataclass
class Asset:
    id: str
    name: str
    asset_type: str
    columns: tuple[Column, ...] = ()
    description: str = ""
    current_version: int = 0
    history: list[AssetVersion] = field(default_factory=list)
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)


@dataclass(frozen=True)
class BatchOp:
    """One item of a batch lineage change.

    op: 'add' (register an edge) or 'remove' (cancel an edge).
    """
    op: str
    upstream: str
    downstream: str
    upstream_column: str | None = None
    downstream_column: str | None = None

    def edge_key(self) -> tuple:
        return (self.upstream, self.downstream,
                self.upstream_column or "", self.downstream_column or "")


@dataclass(frozen=True)
class LineageChange:
    """One immutable lineage mutation record (the audit log entry).

    seq is the global, monotonically increasing change number (starting at 1);
    before/after carry the state of the edge itself (present or not), and
    batch_id groups changes committed by the same batch transaction.
    """
    seq: int
    at: float
    op: str
    upstream: str
    downstream: str
    upstream_column: str | None
    downstream_column: str | None
    before_present: bool
    after_present: bool
    batch_id: int | None = None

    def edge_key(self) -> tuple:
        return (self.upstream, self.downstream,
                self.upstream_column or "", self.downstream_column or "")

    def involves(self, asset_id: str) -> bool:
        return self.upstream == asset_id or self.downstream == asset_id

    def role_of(self, asset_id: str) -> str | None:
        if self.upstream == asset_id:
            return "upstream"
        if self.downstream == asset_id:
            return "downstream"
        return None

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "at": self.at,
            "op": self.op,
            "upstream": self.upstream,
            "downstream": self.downstream,
            "upstream_column": self.upstream_column,
            "downstream_column": self.downstream_column,
            "before_present": self.before_present,
            "after_present": self.after_present,
            "batch_id": self.batch_id,
        }


@dataclass
class LineageAuditResult:
    """Audit trail query outcome.

    ``status`` distinguishes the two 'looks empty' situations:
    - 'found_changes': the asset exists and has lineage change records;
    - 'no_changes':     the asset exists but never took part in any lineage
                        registration/cancellation (records is empty by fact);
    - 'asset_unknown':  no such asset exists (records is empty by absence).
    """
    asset: str
    status: str
    records: list[LineageChange] = field(default_factory=list)
    start: float | None = None
    end: float | None = None

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "status": self.status,
            "start": self.start,
            "end": self.end,
            "records": [r.to_dict() for r in self.records],
        }

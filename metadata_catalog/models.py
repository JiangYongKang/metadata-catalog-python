"""Core domain model: assets (with columns), lineage edges and asset versions."""
from __future__ import annotations

from dataclasses import dataclass, field
from time import time


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

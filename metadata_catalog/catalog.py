"""In-memory metadata catalog with optimistic concurrency control."""
from __future__ import annotations

import threading
from time import time

from .errors import (
    AssetNotFoundError,
    ColumnNotFoundError,
    ConcurrentUpdateError,
    LineageCycleError,
)
from .models import Asset, AssetVersion, Column, LineageEdge


class MetadataCatalog:
    def __init__(self) -> None:
        self._assets: dict[str, Asset] = {}
        self._edges: set[tuple] = set()
        self._edge_objs: dict[tuple, LineageEdge] = {}
        self._lock = threading.RLock()

    # ---- assets -----------------------------------------------------------
    @staticmethod
    def _normalize_columns(columns) -> tuple[Column, ...]:
        result = []
        seen: set[str] = set()
        for c in columns or ():
            if isinstance(c, Column):
                col = c
            elif isinstance(c, dict):
                col = Column(
                    name=str(c["name"]),
                    data_type=str(c.get("data_type", c.get("type", ""))),
                    description=str(c.get("description", "")),
                )
            elif isinstance(c, (tuple, list)):
                col = Column(name=str(c[0]),
                             data_type=str(c[1]) if len(c) > 1 else "",
                             description=str(c[2]) if len(c) > 2 else "")
            else:
                col = Column(name=str(c))
            if col.name in seen:
                raise ValueError(f"duplicate column in registration: {col.name}")
            seen.add(col.name)
            result.append(col)
        return tuple(result)

    @staticmethod
    def _merge_columns(existing: tuple[Column, ...],
                       incoming: tuple[Column, ...]) -> tuple[Column, ...]:
        """Union by name. Existing columns are never dropped; same-name
        columns take the incoming definition; new columns are appended."""
        by_name = {c.name: c for c in existing}
        merged = list(existing)
        for col in incoming:
            if col.name in by_name:
                idx = next(i for i, c in enumerate(merged) if c.name == col.name)
                merged[idx] = col
                by_name[col.name] = col
            else:
                merged.append(col)
                by_name[col.name] = col
        return tuple(merged)

    def register_asset(self, asset_id, name, asset_type, columns=(), description="",
                       expected_version=None, change_note=""):
        """Idempotently register / evolve an asset.

        - Unknown asset: created at version 1.
        - Known asset, identical payload: no new version (idempotent).
        - Known asset, changed payload: merged (columns unioned by name,
          existing columns never dropped) and a new version is recorded.
        - expected_version: optimistic-concurrency guard; must match the
          current version (use 0 for creation). None means unconditional.
        """
        asset_id = str(asset_id)
        incoming_cols = self._normalize_columns(columns)
        with self._lock:
            existing = self._assets.get(asset_id)
            now = time()
            if existing is None:
                if expected_version is not None and expected_version != 0:
                    raise ConcurrentUpdateError(
                        f"asset {asset_id!r} does not exist; expected version "
                        f"{expected_version} but actual version is 0",
                        asset=asset_id, expected_version=expected_version,
                        actual_version=0)
                asset = Asset(
                    id=asset_id, name=name, asset_type=asset_type,
                    columns=incoming_cols, description=description,
                    current_version=1, created_at=now, updated_at=now)
                asset.history.append(AssetVersion(
                    version=1, name=name, asset_type=asset_type,
                    columns=incoming_cols, description=description,
                    created_at=now, change_note=change_note or "initial registration"))
                self._assets[asset_id] = asset
                return asset

            if expected_version is not None and expected_version != existing.current_version:
                raise ConcurrentUpdateError(
                    f"stale update for asset {asset_id!r}: expected version "
                    f"{expected_version}, current is {existing.current_version}",
                    asset=asset_id, expected_version=expected_version,
                    actual_version=existing.current_version)

            merged_cols = self._merge_columns(existing.columns, incoming_cols)
            changed = (
                merged_cols != existing.columns
                or name != existing.name
                or asset_type != existing.asset_type
                or description != existing.description
            )
            if not changed:
                return existing

            note = change_note or self._describe_column_change(existing.columns, merged_cols,
                                                               existing.name != name)
            existing.columns = merged_cols
            existing.name = name
            existing.asset_type = asset_type
            existing.description = description
            existing.current_version += 1
            existing.updated_at = now
            existing.history.append(AssetVersion(
                version=existing.current_version, name=name, asset_type=asset_type,
                columns=merged_cols, description=description, created_at=now,
                change_note=note))
            return existing

    @staticmethod
    def _describe_column_change(old: tuple[Column, ...], new: tuple[Column, ...],
                                renamed: bool) -> str:
        old_names = {c.name for c in old}
        new_names = {c.name for c in new}
        added = sorted(new_names - old_names)
        old_by = {c.name: c for c in old}
        altered = [c.name for c in new
                   if c.name in old_by and old_by[c.name] != c]
        parts = []
        if added:
            parts.append(f"columns added: {', '.join(added)}")
        if altered:
            parts.append(f"columns redefined: {', '.join(altered)}")
        if renamed:
            parts.append("metadata updated")
        return "; ".join(parts) or "definition updated"

    def get_asset(self, asset_id: str) -> Asset:
        with self._lock:
            asset = self._assets.get(str(asset_id))
            if asset is None:
                raise AssetNotFoundError(f"unknown asset: {asset_id!r}",
                                         asset=str(asset_id))
            return asset

    def asset_exists(self, asset_id: str) -> bool:
        with self._lock:
            return str(asset_id) in self._assets

    def list_assets(self) -> list[Asset]:
        with self._lock:
            return [self._assets[k] for k in sorted(self._assets)]

    def get_version(self, asset_id: str, version: int) -> AssetVersion:
        asset = self.get_asset(asset_id)
        for v in asset.history:
            if v.version == version:
                return v
        raise AssetNotFoundError(
            f"asset {asset_id!r} has no version {version}", asset=asset_id)

    def list_versions(self, asset_id: str) -> list[AssetVersion]:
        asset = self.get_asset(asset_id)
        return list(asset.history)

    # ---- lineage ----------------------------------------------------------
    def _validate_endpoint(self, asset_id: str, column: str | None):
        asset_id = str(asset_id)
        asset = self._assets.get(asset_id)
        if asset is None:
            raise AssetNotFoundError(
                f"lineage references unknown asset: {asset_id!r}", asset=asset_id)
        if column is not None:
            names = {c.name for c in asset.columns}
            if column not in names:
                raise ColumnNotFoundError(
                    f"lineage references unknown column {column!r} on asset "
                    f"{asset_id!r} (known columns: {sorted(names)})",
                    asset=asset_id, column=column)

    def _find_path(self, src: str, dst: str) -> list[str]:
        """Deterministic DFS path from src to dst following edge direction
        (downstream adjacency). Returns [] when no path exists."""
        stack: list[tuple[str, list[str]]] = [(src, [src])]
        seen = {src}
        while stack:
            node, path = stack.pop()
            nxt = sorted({e.downstream for e in self._edge_objs.values()
                          if e.upstream == node})
            # reverse so alphabetical order is explored first
            for neighbor in reversed(nxt):
                if neighbor == dst:
                    return path + [neighbor]
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append((neighbor, path + [neighbor]))
        return []

    def add_lineage(self, upstream, downstream, upstream_column=None,
                    downstream_column=None) -> LineageEdge:
        """Register an upstream -> downstream dependency.

        Rejects:
        - AssetNotFoundError: either endpoint asset is unknown (dangling ref)
        - ColumnNotFoundError: a referenced column does not exist
        - LineageCycleError: the edge would close a cycle; cycle path attached
        Idempotent: re-adding the identical edge returns the stored edge.
        Column-level edges never overwrite asset-level or other column edges.
        """
        upstream, downstream = str(upstream), str(downstream)
        if upstream == downstream and not (upstream_column or downstream_column):
            raise LineageCycleError(
                f"self-dependency rejected: {upstream!r} -> itself",
                asset=upstream, cycle=[upstream, upstream])
        with self._lock:
            self._validate_endpoint(upstream, upstream_column)
            self._validate_endpoint(downstream, downstream_column)

            # Would this edge make downstream reachable ... -> upstream?
            path = self._find_path(downstream, upstream)
            if path:
                cycle = path + [downstream]
                raise LineageCycleError(
                    f"cycle rejected: adding {upstream!r} -> {downstream!r} "
                    f"would close cycle {' -> '.join(cycle)}",
                    asset=downstream, cycle=cycle)

            edge = LineageEdge(upstream, downstream, upstream_column, downstream_column)
            k = edge.key()
            existing = self._edge_objs.get(k)
            if existing is not None:
                return existing
            self._edges.add(k)
            self._edge_objs[k] = edge
            return edge

    def remove_lineage(self, upstream, downstream, upstream_column=None,
                       downstream_column=None) -> bool:
        edge = LineageEdge(str(upstream), str(downstream), upstream_column,
                           downstream_column)
        with self._lock:
            k = edge.key()
            if k in self._edge_objs:
                del self._edge_objs[k]
                self._edges.discard(k)
                return True
            return False

    def list_lineage(self) -> list[LineageEdge]:
        with self._lock:
            return [self._edge_objs[k] for k in sorted(self._edge_objs)]

    # ---- graph helpers ----------------------------------------------------
    def upstream_neighbors(self, node: str) -> list[str]:
        """Direct upstream assets (assets feeding node), sorted."""
        node = str(node)
        with self._lock:
            return sorted({e.upstream for e in self._edge_objs.values()
                           if e.downstream == node and e.upstream != node})

    def downstream_neighbors(self, node: str) -> list[str]:
        """Direct downstream assets (assets fed by node), sorted."""
        node = str(node)
        with self._lock:
            return sorted({e.downstream for e in self._edge_objs.values()
                           if e.upstream == node and e.downstream != node})

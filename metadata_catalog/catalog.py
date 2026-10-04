"""In-memory metadata catalog with optimistic concurrency control."""
from __future__ import annotations

import math
import threading
from bisect import bisect_right
from collections.abc import Callable
from time import time

from .errors import (
    AssetNotFoundError,
    BatchFailure,
    BatchLineageError,
    ColumnNotFoundError,
    ConcurrentUpdateError,
    InvalidTimePointError,
    LineageCycleError,
    TimePointOutOfRangeError,
)
from .models import (
    LINEAGE_OP_ADD,
    LINEAGE_OP_REMOVE,
    Asset,
    AssetVersion,
    BatchOp,
    Column,
    LineageChange,
    LineageEdge,
    LineageAuditResult,
)
from .history import HistoricalLineageView


class MetadataCatalog:
    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._assets: dict[str, Asset] = {}
        self._edges: set[tuple] = set()
        self._edge_objs: dict[tuple, LineageEdge] = {}
        self._lock = threading.RLock()
        # injectable, monotone non-decreasing clock (defaults to wall clock)
        self._clock = clock or time
        # append-only lineage audit trail
        self._audit: list[LineageChange] = []
        self._audit_times: list[float] = []   # parallel to _audit, for bisect
        self._next_seq = 1
        # asset id -> global seq numbers that touched it (append order = time)
        self._asset_seq_index: dict[str, list[int]] = {}
        # lazily built replay checkpoints: seq -> edge-key set snapshot
        self._checkpoints: dict[int, frozenset] = {}
        self._checkpoint_every = 128

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
            now = float(self._clock())
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

    def _find_path(self, src: str, dst: str,
                   edge_objs: dict[tuple, LineageEdge] | None = None) -> list[str]:
        """Deterministic DFS path from src to dst following edge direction
        (downstream adjacency). Returns [] when no path exists."""
        objs = self._edge_objs if edge_objs is None else edge_objs
        stack: list[tuple[str, list[str]]] = [(src, [src])]
        seen = {src}
        while stack:
            node, path = stack.pop()
            nxt = sorted({e.downstream for e in objs.values()
                          if e.upstream == node})
            # reverse so alphabetical order is explored first
            for neighbor in reversed(nxt):
                if neighbor == dst:
                    return path + [neighbor]
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append((neighbor, path + [neighbor]))
        return []

    def _now(self) -> float:
        """Monotone non-decreasing timestamp: equal wall readings in one
        process still get strictly ordered, distinct times, which keeps the
        audit trail a total order."""
        t = float(self._clock())
        if self._audit and t < self._audit[-1].at:
            t = self._audit[-1].at
        if self._audit and t == self._audit[-1].at:
            t = self._audit[-1].at + 1e-9
        return t

    def _record_lineage_change(self, op: str, edge: LineageEdge,
                               before_present: bool, after_present: bool,
                               at: float, batch_id: int | None = None) -> LineageChange:
        """Append one audit record (caller already holds the lock)."""
        change = LineageChange(
            seq=self._next_seq, at=at, op=op,
            upstream=edge.upstream, downstream=edge.downstream,
            upstream_column=edge.upstream_column,
            downstream_column=edge.downstream_column,
            before_present=before_present, after_present=after_present,
            batch_id=batch_id)
        self._next_seq += 1
        self._audit.append(change)
        self._audit_times.append(at)
        self._asset_seq_index.setdefault(edge.upstream, []).append(change.seq)
        if edge.downstream != edge.upstream:
            self._asset_seq_index.setdefault(edge.downstream, []).append(change.seq)
        return change

    def _self_loop_rejected(self, upstream: str, downstream: str,
                            upstream_column, downstream_column) -> bool:
        """A dependency that winds back onto the exact same node:

        - asset edge a -> a;
        - column edge on one asset where the two columns are also identical
          (a.x -> a.x). A column edge between two *different* columns of the
          same asset is not a self-loop at the node level."""
        if upstream != downstream:
            return False
        if upstream_column is None and downstream_column is None:
            return True
        return (upstream_column or "") == (downstream_column or "")

    def add_lineage(self, upstream, downstream, upstream_column=None,
                    downstream_column=None) -> LineageEdge:
        """Register an upstream -> downstream dependency.

        Rejects:
        - AssetNotFoundError: either endpoint asset is unknown (dangling ref)
        - ColumnNotFoundError: a referenced column does not exist
        - LineageCycleError: the edge would close a cycle; cycle path attached
          (this includes a -> a and a column depending on itself)
        Idempotent: re-adding the identical edge returns the stored edge and
        writes no audit record. Every actual registration appends an audit
        record carrying the before/after state.
        Column-level edges never overwrite asset-level or other column edges.
        """
        upstream, downstream = str(upstream), str(downstream)
        if self._self_loop_rejected(upstream, downstream,
                                    upstream_column, downstream_column):
            raise LineageCycleError(
                f"self-dependency rejected: {upstream!r} -> itself"
                + (f" column {upstream_column!r}" if upstream_column else ""),
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

            edge = LineageEdge(upstream, downstream, upstream_column,
                               downstream_column, registered_at=self._now())
            k = edge.key()
            existing = self._edge_objs.get(k)
            if existing is not None:
                return existing
            self._edges.add(k)
            self._edge_objs[k] = edge
            self._record_lineage_change(LINEAGE_OP_ADD, edge, False, True,
                                        edge.registered_at)
            return edge

    def remove_lineage(self, upstream, downstream, upstream_column=None,
                       downstream_column=None) -> bool:
        """Cancel an upstream -> downstream dependency.

        Removing a non-existent edge is a no-op (returns False, no audit
        record). An actual removal appends an audit record with the
        before/after state. Returns True when an edge was cancelled."""
        edge = LineageEdge(str(upstream), str(downstream), upstream_column,
                           downstream_column)
        with self._lock:
            k = edge.key()
            if k in self._edge_objs:
                stored = self._edge_objs.pop(k)
                self._edges.discard(k)
                self._record_lineage_change(LINEAGE_OP_REMOVE, stored, True,
                                            False, self._now())
                return True
            return False

    def list_lineage(self) -> list[LineageEdge]:
        with self._lock:
            return [self._edge_objs[k] for k in sorted(self._edge_objs)]

    # ---- batch lineage changes -------------------------------------------
    @staticmethod
    def _normalize_batch_ops(ops) -> list[BatchOp]:
        normalized: list[BatchOp] = []
        for i, item in enumerate(ops):
            if isinstance(item, BatchOp):
                op = item
            elif isinstance(item, dict):
                op = BatchOp(
                    op=str(item.get("op", LINEAGE_OP_ADD)),
                    upstream=str(item["upstream"]),
                    downstream=str(item["downstream"]),
                    upstream_column=item.get("upstream_column"),
                    downstream_column=item.get("downstream_column"))
            else:
                raise TypeError(
                    f"batch item #{i} must be a BatchOp or dict, got {type(item)!r}")
            if op.op not in (LINEAGE_OP_ADD, LINEAGE_OP_REMOVE):
                raise ValueError(
                    f"batch item #{i}: op must be 'add' or 'remove', got {op.op!r}")
            normalized.append(op)
        return normalized

    def _validate_batch_item(self, idx: int, op: BatchOp,
                             candidate: dict) -> BatchFailure | None:
        """Validate one item against assets/columns, self-loops and cycles
        as they would stand after the whole batch. Returns None when valid."""
        def fail(code: str, message: str) -> BatchFailure:
            return BatchFailure(idx, code, message, op.upstream, op.downstream,
                                op.upstream_column, op.downstream_column, op.op)

        # endpoints (assets then columns)
        for endpoint, column in ((op.upstream, op.upstream_column),
                                 (op.downstream, op.downstream_column)):
            asset = self._assets.get(endpoint)
            if asset is None:
                return fail("asset_not_found",
                            f"references unknown asset: {endpoint!r}")
            if column is not None and column not in {c.name for c in asset.columns}:
                return fail("column_not_found",
                            f"references unknown column {column!r} on asset "
                            f"{endpoint!r}")

        # self-dependency (asset-level or identical-column column-level)
        if self._self_loop_rejected(op.upstream, op.downstream,
                                    op.upstream_column, op.downstream_column):
            return fail("lineage_cycle",
                        f"self-dependency rejected: {op.upstream!r} -> itself"
                        + (f" column {op.upstream_column!r}"
                           if op.upstream_column else ""))

        # cycles only matter for genuinely new adds; check against the
        # would-be post-batch graph (the edge itself can never lie on a
        # downstream -> upstream simple path, so its placeholder is harmless)
        if op.op == LINEAGE_OP_ADD and op.edge_key() not in self._edge_objs:
            path = self._find_path(op.downstream, op.upstream, candidate)
            if path:
                cycle = path + [op.downstream]
                return fail("lineage_cycle",
                            f"would close cycle {' -> '.join(cycle)}")
        return None

    def commit_lineage_batch(self, ops) -> dict:
        """Atomically apply a batch of edge additions/removals.

        All-or-nothing: every item is validated first (unknown asset/column,
        self-dependency including a column depending on itself, and cycles
        against the would-be post-batch graph). If anything is wrong the
        whole batch is rejected with BatchLineageError listing each offending
        item with its index and reason, and the graph is left byte-for-byte
        unchanged. Idempotent items (adding an existing edge / removing a
        missing one) are valid and simply produce no change/audit record.
        Committed changes share one batch timestamp and one batch id, with
        per-edge seq numbers in input order.
        """
        ops = self._normalize_batch_ops(ops)
        with self._lock:
            # phase 1: validate every item against the simulated result
            candidate = dict(self._edge_objs)
            for op in ops:
                if op.op == LINEAGE_OP_ADD:
                    candidate.setdefault(op.edge_key(), None)
                else:
                    candidate.pop(op.edge_key(), None)
            # None placeholders must look present for path checks
            placeholder = {k: (e if e is not None else
                               LineageEdge(*self._decode_key(k)))
                           for k, e in candidate.items()}
            failures: list[BatchFailure] = []
            for idx, op in enumerate(ops):
                failure = self._validate_batch_item(idx, op, placeholder)
                if failure is not None:
                    failures.append(failure)
            if failures:
                raise BatchLineageError(
                    f"batch rejected: {len(failures)} of {len(ops)} item(s) "
                    f"invalid; nothing was applied",
                    failures=failures)

            # phase 2: apply (validation proved the graph stays acyclic)
            at = self._now()
            batch_id = self._next_seq
            applied: list[LineageChange] = []
            for op in ops:
                k = op.edge_key()
                if op.op == LINEAGE_OP_ADD:
                    if k in self._edge_objs:
                        continue  # idempotent add: no mutation, no audit
                    edge = LineageEdge(op.upstream, op.downstream,
                                       op.upstream_column, op.downstream_column,
                                       registered_at=at)
                    self._edges.add(k)
                    self._edge_objs[k] = edge
                    applied.append(self._record_lineage_change(
                        LINEAGE_OP_ADD, edge, False, True, at, batch_id))
                else:
                    stored = self._edge_objs.pop(k, None)
                    self._edges.discard(k)
                    if stored is None:
                        continue  # removing a missing edge: no audit
                    applied.append(self._record_lineage_change(
                        LINEAGE_OP_REMOVE, stored, True, False, at, batch_id))
            return {"batch_id": batch_id, "submitted": len(ops),
                    "applied": len(applied), "changes": applied}

    @staticmethod
    def _decode_key(k: tuple) -> tuple:
        upstream, downstream, uc, dc = k
        return (upstream, downstream, uc or None, dc or None)
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

    # ---- lineage audit ----------------------------------------------------
    @staticmethod
    def _validate_time_range(start, end) -> tuple[float | None, float | None]:
        def check(value, label):
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidTimePointError(
                    f"{label} must be a numeric unix timestamp, got {value!r}",
                    value=value)
            v = float(value)
            if not math.isfinite(v):
                raise InvalidTimePointError(
                    f"{label} must be finite, got {value!r}", value=value)
            return v

        s, e = check(start, "start"), check(end, "end")
        if s is not None and e is not None and s > e:
            raise InvalidTimePointError(
                f"time range inverted: start {s} is after end {e}",
                value=(s, e))
        return s, e

    def lineage_audit(self, asset_id, start: float | None = None,
                      end: float | None = None) -> LineageAuditResult:
        """Return the full lineage change trail touching one asset.

        - Unknown asset raises AssetNotFoundError (unmistakably different
          from an existing asset that simply has no changes yet).
        - An existing asset without any lineage change returns status
          'no_changes' and an empty record list.
        - Otherwise status is 'found_changes', ordered oldest -> newest;
          each record says which relation was added/removed and carries the
          before/after presence. An optional [start, end] time window filters.
        """
        asset_id = str(asset_id)
        s, e = self._validate_time_range(start, end)
        with self._lock:
            if asset_id not in self._assets:
                raise AssetNotFoundError(
                    f"lineage audit for unknown asset: {asset_id!r}",
                    asset=asset_id)
            seqs = self._asset_seq_index.get(asset_id)
            if not seqs:
                return LineageAuditResult(asset_id, "no_changes", [], s, e)
            records = [self._audit[i - 1] for i in seqs]
            if s is not None:
                records = [r for r in records if r.at >= s]
            if e is not None:
                records = [r for r in records if r.at <= e]
            return LineageAuditResult(asset_id, "found_changes", records, s, e)

    # ---- point-in-time reconstruction ------------------------------------
    @staticmethod
    def _validate_as_of(at) -> float:
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            raise InvalidTimePointError(
                f"as-of time must be a numeric unix timestamp, got {at!r}",
                value=at)
        t = float(at)
        if not math.isfinite(t):
            raise InvalidTimePointError(
                f"as-of time must be finite, got {at!r}", value=at)
        return t

    def _state_at_seq(self, target: int) -> dict[tuple, LineageEdge]:
        """Reconstruct edge storage (key -> edge) after change ``target``.

        Lazily materialised checkpoints every ``_checkpoint_every`` changes
        bound replay length to at most ~2x that interval regardless of the
        total history size: rebuilding one checkpoint reads at most
        ``_checkpoint_every`` records from the previous checkpoint, and the
        final replay reads at most ``_checkpoint_every`` more. Live queries
        never touch this path, so large histories don't slow them down.
        """
        # walk checkpoint boundaries at or below target, materialising any
        # missing ones from the nearest existing checkpoint
        cp = (target // self._checkpoint_every) * self._checkpoint_every
        if cp > 0 and cp not in self._checkpoints:
            prev = cp - self._checkpoint_every
            if prev > 0 and prev not in self._checkpoints:
                self._state_at_seq(prev)
            keys = set(self._checkpoints.get(prev, frozenset()))
            for change in self._audit[prev:cp]:
                if change.op == LINEAGE_OP_ADD:
                    keys.add(change.edge_key())
                else:
                    keys.discard(change.edge_key())
            self._checkpoints[cp] = frozenset(keys)
        keys = set(self._checkpoints.get(cp, frozenset()))
        start = cp

        edges: dict[tuple, LineageEdge] = {}
        for change in self._audit[start:target]:
            k = change.edge_key()
            if change.op == LINEAGE_OP_ADD:
                up, dn, uc, dc = k
                edge = LineageEdge(up, dn, uc or None, dc or None,
                                   registered_at=change.at)
                edges[k] = edge
                keys.add(k)
            else:
                edges.pop(k, None)
                keys.discard(k)
        # surviving keys whose add predates the replay window: use the live
        # stored object when the edge is still present, otherwise recreate
        for k in keys - edges.keys():
            live = self._edge_objs.get(k)
            if live is not None:
                edges[k] = live
            else:
                up, dn, uc, dc = k
                edges[k] = LineageEdge(up, dn, uc or None, dc or None)
        return edges

    def lineage_as_of(self, at) -> HistoricalLineageView:
        """Reconstruct the lineage graph exactly as it stood at timestamp
        ``at`` (inclusive: changes with time <= at are reflected).

        - InvalidTimePointError: ``at`` is not a finite numeric timestamp.
        - TimePointOutOfRangeError: no lineage state exists for ``at`` --
          earlier than the first audited change (nothing existed yet, but
          that is unknowable as 'lineage') or later than the newest state.
        Repeated calls with the same timestamp return identical graphs.
        """
        t = self._validate_as_of(at)
        with self._lock:
            if not self._audit:
                raise TimePointOutOfRangeError(
                    "no lineage history has been recorded, so no point in "
                    "time can be reconstructed",
                    requested=t, earliest=None, latest=None)
            earliest = self._audit[0].at
            latest = self._audit[-1].at
            if t < earliest or t > latest:
                raise TimePointOutOfRangeError(
                    f"time {t} is outside the reconstructible lineage "
                    f"window [{earliest}, {latest}]",
                    requested=t, earliest=earliest, latest=latest)
            seq = bisect_right(self._audit_times, t)
            edges = self._state_at_seq(seq)
            return HistoricalLineageView(self, t, edges, source="time")

    def lineage_at_version(self, asset_id, version: int) -> HistoricalLineageView:
        """Reconstruct the lineage graph as it stood when ``asset_id`` was at
        ``version`` (the graph at that version's creation time, clamped to
        the newest known state -- a young lineage system predating any
        lineage change means 'the lineage as it was then': empty)."""
        asset_version = self.get_version(asset_id, version)
        t = asset_version.created_at
        with self._lock:
            if not self._audit:
                return HistoricalLineageView(self, t, {}, source="version",
                                             ref=version)
            earliest = self._audit[0].at
            if t < earliest:
                # lineage did not exist yet when this version was created
                return HistoricalLineageView(self, t, {}, source="version",
                                             ref=version)
            latest = self._audit[-1].at
            if t > latest:
                t = latest  # version newer than the last lineage change
            seq = bisect_right(self._audit_times, t)
            edges = self._state_at_seq(seq)
            return HistoricalLineageView(self, t, edges, source="version",
                                         ref=version)

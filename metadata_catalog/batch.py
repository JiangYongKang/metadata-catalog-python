"""Atomic batch lineage changes (all-or-nothing commits).

Validation/planning lives here as pure functions over the current edge map
and asset table; the catalog owns locking and application.
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import (
    ASSET_NOT_FOUND,
    COLUMN_NOT_FOUND,
    LINEAGE_CYCLE,
)
from .models import LineageEdge

OP_ADD = "add"
OP_REMOVE = "remove"
_ACTIONS = (OP_ADD, OP_REMOVE)


@dataclass(frozen=True)
class LineageOp:
    """One operation inside a batch commit."""
    action: str           # OP_ADD | OP_REMOVE
    upstream: str
    downstream: str
    upstream_column: str | None = None
    downstream_column: str | None = None

    @property
    def edge(self) -> LineageEdge:
        return LineageEdge(self.upstream, self.downstream,
                           self.upstream_column, self.downstream_column)

    def to_dict(self) -> dict:
        return {"action": self.action, "upstream": self.upstream,
                "downstream": self.downstream,
                "upstream_column": self.upstream_column,
                "downstream_column": self.downstream_column}


@dataclass(frozen=True)
class BatchItemFailure:
    index: int
    code: str
    message: str
    op: LineageOp
    asset: str | None = None
    column: str | None = None
    cycle: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        data = {"index": self.index, "code": self.code, "message": self.message,
                "op": self.op.to_dict()}
        if self.asset is not None:
            data["asset"] = self.asset
        if self.column is not None:
            data["column"] = self.column
        if self.cycle:
            data["cycle"] = list(self.cycle)
        return data


@dataclass(frozen=True)
class _PlanEntry:
    """Per-op planning result for ops that passed validation."""
    index: int
    op: LineageOp
    key: tuple
    before_present: bool
    changed: bool


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    committed: bool
    applied: tuple[LineageOp, ...]
    changes: tuple = ()          # audit LineageChange records of the commit
    skipped: tuple[int, ...] = ()

    def to_dict(self) -> dict:
        return {"batch_id": self.batch_id, "committed": self.committed,
                "applied": [op.to_dict() for op in self.applied],
                "skipped": list(self.skipped),
                "changes": [c.to_dict() for c in self.changes]}


def is_self_dependency(upstream: str, downstream: str,
                       upstream_column: str | None,
                       downstream_column: str | None) -> tuple[str, ...] | None:
    """Return a cycle path if the op makes something depend on itself.

    Covers both asset-level loops (a -> a) and column-level self loops
    (a.id -> a.id, including mixed whole-asset <-> own-column edges).
    Column transformations within one asset are allowed only when the two
    referenced columns differ (a.x -> a.y).
    """
    if upstream != downstream:
        return None
    if upstream_column is None and downstream_column is None:
        return (upstream, upstream)
    if upstream_column == downstream_column:
        return (upstream, upstream)
    # exactly one side names a column: asset-level self loop
    if upstream_column is None or downstream_column is None:
        return (upstream, upstream)
    return None


def normalize_ops(ops) -> list[LineageOp]:
    """Coerce LineageOp/dict inputs into LineageOp; raise ValueError on bad shapes."""
    if ops is None:
        raise ValueError("batch ops must be a non-None iterable")
    normalized: list[LineageOp] = []
    for i, raw in enumerate(ops):
        if isinstance(raw, LineageOp):
            op = raw
        elif isinstance(raw, dict):
            data = dict(raw)
            action = str(data.get("action", "")).strip()
            if action not in _ACTIONS:
                raise ValueError(
                    f"op[{i}]: action must be one of {_ACTIONS}, got {action!r}")
            try:
                op = LineageOp(
                    action=action,
                    upstream=str(data["upstream"]),
                    downstream=str(data["downstream"]),
                    upstream_column=data.get("upstream_column"),
                    downstream_column=data.get("downstream_column"),
                )
            except KeyError as exc:
                raise ValueError(f"op[{i}] is missing required field: {exc}") from None
        else:
            raise ValueError(
                f"op[{i}] must be a dict or LineageOp, got {type(raw).__name__}")
        normalized.append(op)
    return normalized


def _find_path(edge_objs: dict[tuple, LineageEdge],
               src: str, dst: str) -> list[str]:
    """Deterministic DFS path src -> dst following downstream adjacency."""
    stack: list[tuple[str, list[str]]] = [(src, [src])]
    seen = {src}
    while stack:
        node, path = stack.pop()
        nxt = sorted({e.downstream for e in edge_objs.values()
                      if e.upstream == node})
        for neighbor in reversed(nxt):
            if neighbor == dst:
                return path + [neighbor]
            if neighbor not in seen:
                seen.add(neighbor)
                stack.append((neighbor, path + [neighbor]))
    return []


def _validate_endpoint(op: LineageOp, assets: dict, side: str,
                       asset_id: str, column: str | None, index: int):
    asset = assets.get(asset_id)
    if asset is None:
        return BatchItemFailure(
            index=index, code=ASSET_NOT_FOUND,
            message=f"op[{index}] {op.action}: {side} references unknown "
                    f"asset {asset_id!r}",
            op=op, asset=asset_id)
    if column is not None:
        names = {c.name for c in asset.columns}
        if column not in names:
            return BatchItemFailure(
                index=index, code=COLUMN_NOT_FOUND,
                message=f"op[{index}] {op.action}: {side} references unknown "
                        f"column {column!r} on asset {asset_id!r} "
                        f"(known columns: {sorted(names)})",
                op=op, asset=asset_id, column=column)
    return None


def plan_batch(ops: list[LineageOp], *, assets: dict,
               edge_objs: dict[tuple, LineageEdge]
               ) -> tuple[list[_PlanEntry], list[BatchItemFailure]]:
    """Validate every op against the graph with earlier ops tentatively applied.

    Returns (entries, failures). Entries cover all ops that passed validation
    (each flagged changed/skipped). Any non-empty failures list means the
    caller must reject the whole batch and apply nothing.
    """
    tentative: dict[tuple, LineageEdge] = dict(edge_objs)
    entries: list[_PlanEntry] = []
    failures: list[BatchItemFailure] = []

    for i, op in enumerate(ops):
        u, v = op.upstream, op.downstream
        failure = _validate_endpoint(op, assets, "upstream", u,
                                     op.upstream_column, i)
        if failure is None and op.action == OP_ADD:
            failure = _validate_endpoint(op, assets, "downstream", v,
                                         op.downstream_column, i)
        if failure is None:
            failure = _validate_endpoint(op, assets, "downstream", v,
                                         None, i)
        if failure is not None:
            failures.append(failure)
            continue

        cycle = is_self_dependency(u, v, op.upstream_column,
                                   op.downstream_column)
        if cycle is not None:
            failures.append(BatchItemFailure(
                index=i, code=LINEAGE_CYCLE,
                message=f"op[{i}] {op.action}: self-dependency rejected: "
                        f"{u!r} -> itself",
                op=op, asset=u, cycle=cycle))
            continue

        k = op.edge.key()
        before_present = k in tentative
        if op.action == OP_ADD:
            path = _find_path(tentative, v, u)
            if path:
                cycle = tuple(path + [v])
                failures.append(BatchItemFailure(
                    index=i, code=LINEAGE_CYCLE,
                    message=f"op[{i}] add: edge {u!r} -> {v!r} would close "
                            f"cycle {' -> '.join(cycle)}",
                    op=op, asset=v, cycle=cycle))
                continue
            tentative[k] = op.edge
        else:
            tentative.pop(k, None)

        entries.append(_PlanEntry(
            index=i, op=op, key=k, before_present=before_present,
            changed=(op.action == OP_ADD) != before_present))

    return entries, failures

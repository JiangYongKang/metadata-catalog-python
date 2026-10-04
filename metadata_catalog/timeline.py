"""Point-in-time / asset-version reconstruction of the lineage graph."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .audit import ACTION_ADD, ACTION_REMOVE
from .errors import LineageTimeError
from .models import LineageEdge


@dataclass(frozen=True)
class LineageSnapshot:
    """Immutable lineage graph as of one moment in time.

    Neighbor lookups are O(degree) over frozen local sets and edge listing is
    O(E); impact analysis on a snapshot never touches the live graph or its
    lock, so a heavy retrospective cannot slow ordinary queries.
    """
    as_of: float
    edges: tuple[LineageEdge, ...] = ()
    _upstream_of: dict[str, frozenset[str]] = field(default_factory=dict,
                                                    compare=False, repr=False)
    _downstream_of: dict[str, frozenset[str]] = field(default_factory=dict,
                                                      compare=False, repr=False)
    nodes: frozenset[str] = frozenset()

    def list_edges(self) -> list[LineageEdge]:
        return list(self.edges)

    def upstream_neighbors(self, node: str) -> list[str]:
        """Assets feeding node as of this snapshot, sorted."""
        return sorted(self._upstream_of.get(str(node), frozenset()) - {str(node)})

    def downstream_neighbors(self, node: str) -> list[str]:
        """Assets fed by node as of this snapshot, sorted."""
        return sorted(self._downstream_of.get(str(node), frozenset()) - {str(node)})

    def asset_exists(self, asset_id: str) -> bool:
        return str(asset_id) in self.nodes


def validate_timestamp(value, *, name: str = "as_of") -> float:
    """Coerce to a finite float or raise LineageTimeError(reason=...)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LineageTimeError(
            f"{name} must be a numeric timestamp (epoch seconds), got "
            f"{value!r} ({type(value).__name__})",
            reason="invalid_type", requested=value)
    ts = float(value)
    if math.isnan(ts) or math.isinf(ts):
        raise LineageTimeError(
            f"{name} must be finite, got {value!r}",
            reason="invalid_value", requested=ts)
    return ts


def build_snapshot(changes, as_of, *, nodes=None) -> LineageSnapshot:
    """Fold effective audit events up to ``as_of`` (inclusive).

    Same timestamp is fully included, so a batch committed at one instant is
    visible atomically at that instant. The fold is a pure function of
    (changes, as_of) => repeated calls with equal inputs return equal graphs.
    """
    as_of = float(as_of)
    edge_map: dict[tuple, LineageEdge] = {}
    for c in changes:
        if c.timestamp > as_of:
            break  # changes arrive in append (chronological) order
        if c.action == ACTION_ADD:
            edge_map[c.key] = LineageEdge(
                c.upstream, c.downstream, c.upstream_column,
                c.downstream_column, registered_at=c.timestamp)
        elif c.action == ACTION_REMOVE:
            edge_map.pop(c.key, None)

    edges = tuple(edge_map[k] for k in sorted(edge_map))
    up: dict[str, set[str]] = defaultdict(set)
    down: dict[str, set[str]] = defaultdict(set)
    seen_nodes: set[str] = set()
    for e in edges:
        seen_nodes.add(e.upstream)
        seen_nodes.add(e.downstream)
        up[e.downstream].add(e.upstream)
        down[e.upstream].add(e.downstream)

    if nodes is None:
        known = frozenset(seen_nodes)
    else:
        known = frozenset(nodes) | seen_nodes
    return LineageSnapshot(
        as_of=as_of,
        edges=edges,
        _upstream_of={k: frozenset(v) for k, v in up.items()},
        _downstream_of={k: frozenset(v) for k, v in down.items()},
        nodes=known,
    )


def reconstruct_as_of(catalog, as_of) -> LineageSnapshot:
    """Validate the requested time and return the lineage snapshot at it.

    Reconstructable window: [catalog creation time, current clock time].
    Errors rather than returning an empty/normal-looking graph when:
    - as_of is not a finite numeric timestamp  (reason=invalid_type/value)
    - as_of precedes catalog creation          (reason=before_history)
    - as_of is later than the current clock    (reason=future)
    Times between catalog creation and the first lineage change reconstruct a
    provably empty graph; every repeat with the same timestamp folds the same
    events and yields the same edges.
    """
    ts = validate_timestamp(as_of)
    with catalog._lock:
        changes = catalog._audit.effective_changes()
        earliest = catalog._audit.first_timestamp
        started_at = catalog._started_at
        now = catalog._clock()
        born = dict(catalog._asset_born)

    if ts < started_at:
        raise LineageTimeError(
            f"requested time {ts} precedes catalog creation at {started_at}; "
            f"lineage state before that moment is not reconstructable",
            reason="before_history", requested=ts, earliest=started_at,
            latest=now)
    if ts > now:
        latest = earliest if earliest is not None else started_at
        raise LineageTimeError(
            f"requested time {ts} is later than the current clock {now}; "
            f"the future cannot be reconstructed",
            reason="future", requested=ts, earliest=started_at, latest=latest)

    nodes = frozenset(asset_id for asset_id, born_at in born.items()
                      if born_at <= ts)
    return build_snapshot(changes, ts, nodes=nodes)


def reconstruct_for_asset_version(catalog, asset_id, version) -> LineageSnapshot:
    """Lineage as it stood when ``asset_id`` reached structural ``version``.

    The version snapshot's ``created_at`` anchors the fold (all lineage
    changes up to and including that moment). Unknown asset / unknown version
    propagate AssetNotFoundError; an invalid version number or time-range
    violation propagates LineageTimeError.
    """
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise LineageTimeError(
            f"version must be a positive integer, got {version!r}",
            reason="invalid_version", asset=str(asset_id), version=version)
    snapshot_version = catalog.get_version(str(asset_id), version)
    return reconstruct_as_of(catalog, snapshot_version.created_at)

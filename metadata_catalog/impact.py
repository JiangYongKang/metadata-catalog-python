"""Depth-limited, deterministic upstream/downstream impact analysis."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .errors import AssetNotFoundError


@dataclass
class ImpactResult:
    start: str
    direction: str
    max_depth: int | None
    # node -> minimum depth (edge hops) from start; start is depth 0
    depths: dict[str, int] = field(default_factory=dict)
    # BFS tree edges in discovery order: (edge_depth, source, target)
    paths: list[tuple[int, str, str]] = field(default_factory=list)
    # edges to already-visited nodes (cycles / duplicate paths), never re-walked
    repeated_edges: list[tuple[int, str, str]] = field(default_factory=list)
    # nodes at the depth boundary that still have unexpanded neighbors
    truncated_at: list[str] = field(default_factory=list)

    @property
    def nodes(self) -> list[str]:
        """All visited nodes (including start), ordered by (depth, name)."""
        return sorted(self.depths, key=lambda n: (self.depths[n], n))

    @property
    def impacted_nodes(self) -> list[str]:
        """Impacted nodes excluding the start, ordered by (depth, name)."""
        return [n for n in self.nodes if n != self.start]

    @property
    def truncated(self) -> bool:
        return bool(self.truncated_at)

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "direction": self.direction,
            "max_depth": self.max_depth,
            "nodes": [{"id": n, "depth": self.depths[n]} for n in self.nodes],
            "paths": [{"depth": d, "source": s, "target": t}
                      for d, s, t in self.paths],
            "repeated_edges": [{"depth": d, "source": s, "target": t}
                               for d, s, t in self.repeated_edges],
            "truncated_at": self.truncated_at,
        }


def analyze_impact(catalog, start, direction: str = "downstream",
                   max_depth: int | None = None) -> ImpactResult:
    """BFS impact analysis.

    direction: 'upstream' (sources feeding ``start``) or 'downstream'
    (consumers depending on ``start``).
    max_depth: maximum edge hops; None / negative means unlimited.
    Each node is recorded at its minimum depth only, so cycles and duplicate
    paths (diamonds) never cause repeated traversal or unbounded growth.
    Neighbor expansion order is alphabetical => results are reproducible.
    """
    start = str(start)
    if direction not in ("upstream", "downstream"):
        raise ValueError(f"direction must be 'upstream' or 'downstream', "
                         f"got {direction!r}")
    if not catalog.asset_exists(start):
        raise AssetNotFoundError(f"impact analysis from unknown asset: "
                                 f"{start!r}", asset=start)
    if max_depth is not None and max_depth < 0:
        max_depth = None

    neighbors = (catalog.upstream_neighbors if direction == "upstream"
                 else catalog.downstream_neighbors)

    result = ImpactResult(start=start, direction=direction, max_depth=max_depth)
    result.depths[start] = 0
    seen_repeated: set[tuple[int, str, str]] = set()
    queue: deque[tuple[str, int]] = deque([(start, 0)])

    while queue:
        node, depth = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            if neighbors(node):
                result.truncated_at.append(node)
            continue
        for nb in neighbors(node):
            edge_depth = depth + 1
            if nb not in result.depths:
                result.depths[nb] = edge_depth
                result.paths.append((edge_depth, node, nb))
                queue.append((nb, edge_depth))
            else:
                rec = (edge_depth, node, nb)
                if rec not in seen_repeated:
                    seen_repeated.add(rec)
                    result.repeated_edges.append(rec)

    result.truncated_at = sorted(set(result.truncated_at))
    return result

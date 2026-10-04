"""Point-in-time reconstruction of the lineage graph (as-of queries).

A :class:`HistoricalLineageView` is an immutable snapshot of the lineage
edges as they stood at one past moment.  It exposes the same
``asset_exists`` / ``upstream_neighbors`` / ``downstream_neighbors`` surface
as the live catalog, so :func:`~metadata_catalog.impact.analyze_impact` runs
on a reconstructed graph exactly as on the live one.
"""
from __future__ import annotations

from .models import LineageEdge


class HistoricalLineageView:
    """Immutable read-only lineage graph reconstructed at one point in time.

    ``edges`` maps the four-tuple edge key to the LineageEdge as stored when
    the change happened (its registered_at is therefore historical too).
    ``source`` is 'time' (reconstructed at a timestamp) or 'version'
    (reconstructed at an asset version's creation time).
    """

    def __init__(self, catalog, as_of: float, edges: dict, *,
                 source: str = "time", ref: str | int | None = None):
        self._catalog = catalog
        self.as_of = as_of
        self.source = source
        self.ref = ref
        # copy so later live writes can never mutate the snapshot
        self._edge_objs: dict[tuple, LineageEdge] = dict(edges)

    # -- graph surface shared with MetadataCatalog --------------------------
    def asset_exists(self, asset_id: str) -> bool:
        # Asset existence is answered by the (locked, authoritative) catalog;
        # the snapshot only captures the lineage edge set.
        return self._catalog.asset_exists(str(asset_id))

    def upstream_neighbors(self, node: str) -> list[str]:
        """Direct upstream assets feeding ``node`` at this point in time."""
        node = str(node)
        return sorted({e.upstream for e in self._edge_objs.values()
                       if e.downstream == node and e.upstream != node})

    def downstream_neighbors(self, node: str) -> list[str]:
        """Direct downstream assets fed by ``node`` at this point in time."""
        node = str(node)
        return sorted({e.downstream for e in self._edge_objs.values()
                       if e.upstream == node and e.downstream != node})

    # -- snapshot inspection ------------------------------------------------
    def edges(self) -> list[LineageEdge]:
        return [self._edge_objs[k] for k in sorted(self._edge_objs)]

    def edge_count(self) -> int:
        return len(self._edge_objs)

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "source": self.source,
            "ref": self.ref,
            "edge_count": len(self._edge_objs),
            "edges": [{
                "upstream": e.upstream,
                "downstream": e.downstream,
                "upstream_column": e.upstream_column,
                "downstream_column": e.downstream_column,
                "column_level": e.is_column_level,
            } for e in self.edges()],
        }

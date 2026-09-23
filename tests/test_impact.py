"""Tests for depth-limited impact analysis on diamonds, cycles and large graphs."""
import unittest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import AssetNotFoundError
from metadata_catalog.impact import analyze_impact

from tests._helpers import log_case


class ImpactAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.catalog = MetadataCatalog()

    def _build_diamond(self):
        # a -> {b,c} -> d -> e -> f
        for aid in "abcdef":
            self.catalog.register_asset(aid, aid, "table")
        for u, v in [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"),
                     ("d", "e"), ("e", "f")]:
            self.catalog.add_lineage(u, v)

    def test_downstream_full_and_depths(self):
        self._build_diamond()
        r = analyze_impact(self.catalog, "a", "downstream")
        log_case("full downstream", start="a",
                 nodes=r.nodes, depths=r.depths, repeated=r.repeated_edges)
        self.assertEqual(r.nodes, ["a", "b", "c", "d", "e", "f"],
                         f"reason: unexpected node set/order {r.nodes}")
        self.assertEqual(r.depths["d"], 2,
                         "reason: diamond node d must be minimum depth 2")
        self.assertEqual(len(r.repeated_edges), 1,
                         f"reason: exactly one duplicate path (diamond), got {r.repeated_edges}")

    def test_depth_limit_truncates_boundary(self):
        self._build_diamond()
        r = analyze_impact(self.catalog, "a", max_depth=2)
        log_case("depth=2", start="a", impacted=r.impacted_nodes,
                 truncated_at=r.truncated_at)
        self.assertEqual(r.impacted_nodes, ["b", "c", "d"])
        self.assertEqual(r.truncated_at, ["d"],
                         f"reason: d sits on boundary with unexpanded edges, got {r.truncated_at}")
        self.assertTrue(r.truncated)

    def test_depth_zero_visits_only_start(self):
        self._build_diamond()
        r = analyze_impact(self.catalog, "a", max_depth=0)
        self.assertEqual(r.impacted_nodes, [])
        self.assertEqual(r.truncated_at, ["a"])

    def test_upstream_direction(self):
        self._build_diamond()
        r = analyze_impact(self.catalog, "f", "upstream")
        log_case("upstream from f", nodes=r.nodes, depths=r.depths)
        self.assertEqual(r.depths["a"], 4)
        self.assertEqual(set(r.impacted_nodes), {"e", "d", "b", "c", "a"})

    def test_unknown_start_rejected(self):
        log_case("unknown start", start="nope")
        with self.assertRaises(AssetNotFoundError) as ctx:
            analyze_impact(self.catalog, "nope")
        self.assertEqual(ctx.exception.code, "asset_not_found")

    def test_repeated_paths_never_blow_up(self):
        """Many parallel paths (a->x_i->b for 1000 x_i) stay linear-time."""
        n = 1000
        self.catalog.register_asset("a", "a", "table")
        self.catalog.register_asset("b", "b", "table")
        for i in range(n):
            aid = f"x{i:04d}"
            self.catalog.register_asset(aid, aid, "table")
            self.catalog.add_lineage("a", aid)
            self.catalog.add_lineage(aid, "b")
        r = analyze_impact(self.catalog, "a")
        log_case("fan-in/fan-out graph", paths_per_b=n, visited=len(r.depths),
                 repeated_edges=len(r.repeated_edges))
        self.assertEqual(len(r.depths), n + 2,
                         "reason: every node visited exactly once")
        self.assertEqual(r.depths["b"], 2)
        self.assertEqual(len(r.repeated_edges), n - 1,
                         "reason: second..nth arrival at b reported, never re-walked")

    def test_results_deterministic(self):
        self._build_diamond()
        runs = [tuple(analyze_impact(self.catalog, "a").nodes) for _ in range(30)]
        self.assertEqual(len(set(runs)), 1,
                         f"reason: nondeterministic output across runs: {set(runs)}")
        print("  deterministic ordering confirmed over 30 runs:", runs[0])

    def test_graph_with_cycle_terminates(self):
        """Even if a cyclic graph appears (bypassing registration guards),
        BFS terminates, visits each node once and reports cycle edges."""
        for aid in "abc":
            self.catalog.register_asset(aid, aid, "table")
        # inject a->b->c->a directly into storage
        from metadata_catalog.models import LineageEdge
        for u, v in [("a", "b"), ("b", "c"), ("c", "a")]:
            edge = LineageEdge(u, v)
            self.catalog._edges.add(edge.key())
            self.catalog._edge_objs[edge.key()] = edge
        r = analyze_impact(self.catalog, "a", max_depth=None)
        log_case("cycle a->b->c->a", visited=r.nodes, paths=r.paths,
                 repeated=r.repeated_edges)
        self.assertEqual(set(r.depths), {"a", "b", "c"},
                         "reason: cycle nodes visited exactly once each")
        self.assertEqual(r.depths, {"a": 0, "b": 1, "c": 2},
                         f"reason: min depths wrong on cycle: {r.depths}")
        self.assertEqual(len(r.repeated_edges), 1,
                         "reason: closing edge c->a reported as repeated")
        r2 = analyze_impact(self.catalog, "a", max_depth=1)
        self.assertEqual(set(r2.depths), {"a", "b"},
                         "reason: depth limit must contain traversal on a cycle")
        print("  cycle traversal bounded; depth=1 nodes:", r2.nodes)


if __name__ == "__main__":
    unittest.main(verbosity=2)

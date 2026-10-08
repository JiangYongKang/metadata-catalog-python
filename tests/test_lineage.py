"""Tests for dangling references and cycle rejection."""
import unittest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import (
    AssetNotFoundError,
    ColumnNotFoundError,
    LineageCycleError,
)

from tests._helpers import log_case


class LineageValidationTest(unittest.TestCase):
    def setUp(self):
        self.catalog = MetadataCatalog()
        for aid in ("a", "b", "c", "d"):
            self.catalog.register_asset(
                aid, aid, "table", [("id", "bigint"), ("v", "string")])

    def test_dangling_upstream_rejected(self):
        """Unknown upstream asset -> asset_not_found (distinguishable code)."""
        log_case("dangling upstream", edge=("ghost", "b"),
                 existing_assets=["a", "b", "c", "d"])
        with self.assertRaises(AssetNotFoundError) as ctx:
            self.catalog.add_lineage("ghost", "b")
        err = ctx.exception
        self.assertEqual(err.code, "asset_not_found",
                         f"reason: expected asset_not_found, got {err.code}")
        self.assertEqual(err.asset, "ghost",
                         f"reason: offending asset id should be exposed, got {err.asset}")

    def test_dangling_downstream_rejected(self):
        """Unknown downstream asset -> asset_not_found."""
        log_case("dangling downstream", edge=("a", "ghost"))
        with self.assertRaises(AssetNotFoundError) as ctx:
            self.catalog.add_lineage("a", "ghost")
        self.assertEqual(ctx.exception.asset, "ghost")

    def test_dangling_column_rejected_distinctly(self):
        """Unknown column -> column_not_found, distinct from missing asset."""
        log_case("dangling column", edge=("a.id?", "b.id"),
                 known_columns_of_a=["id", "v"])
        with self.assertRaises(ColumnNotFoundError) as ctx:
            self.catalog.add_lineage("a", "b", upstream_column="missing",
                                     downstream_column="id")
        err = ctx.exception
        self.assertEqual(err.code, "column_not_found",
                         f"reason: column error must differ from asset error, got {err.code}")
        self.assertEqual(err.asset, "a")
        self.assertEqual(err.column, "missing")

    def test_self_dependency_is_a_cycle(self):
        """a -> a is rejected on the spot with the distinguishable
        self_dependency code (a specialised LineageCycleError)."""
        log_case("self dependency", edge=("a", "a"))
        with self.assertRaises(LineageCycleError) as ctx:
            self.catalog.add_lineage("a", "a")
        err = ctx.exception
        self.assertEqual(err.code, "self_dependency",
                         f"reason: self-dependency must be distinguishable "
                         f"from a multi-asset cycle, got {err.code}")
        self.assertEqual(err.kind, "asset")
        self.assertIn("a", err.cycle)

    def test_three_node_cycle_rejected_with_path(self):
        """a->b, b->c accepted; c->a rejected; cycle path must be reported."""
        self.catalog.add_lineage("a", "b")
        self.catalog.add_lineage("b", "c")
        log_case("closing edge c->a", existing_edges=["a->b", "b->c"])
        with self.assertRaises(LineageCycleError) as ctx:
            self.catalog.add_lineage("c", "a")
        err = ctx.exception
        self.assertEqual(err.code, "lineage_cycle")
        self.assertEqual(err.cycle[0], "a",
                         f"reason: cycle should start at reachable a, got {err.cycle}")
        self.assertEqual(err.cycle[-1], "a",
                         f"reason: cycle must close back on a, got {err.cycle}")
        print("  detected cycle:", " -> ".join(err.cycle))

    def test_diamond_shortcut_is_not_a_cycle(self):
        """Diamond + shortcut a->d is valid and must NOT be rejected."""
        self.catalog.add_lineage("a", "b")
        self.catalog.add_lineage("a", "c")
        self.catalog.add_lineage("b", "d")
        self.catalog.add_lineage("c", "d")
        log_case("shortcut edge a->d on diamond",
                 existing_edges=["a->b", "a->c", "b->d", "c->d"])
        edge = self.catalog.add_lineage("a", "d")
        self.assertEqual((edge.upstream, edge.downstream), ("a", "d"),
                         "reason: an added shortcut is not a cycle")

    def test_column_level_edges_are_independent(self):
        """Column edges and asset edges coexist; re-registration is idempotent."""
        e1 = self.catalog.add_lineage("a", "b", "id", "id")
        e2 = self.catalog.add_lineage("a", "b", "id", "id")
        e3 = self.catalog.add_lineage("a", "b", "v", "v")
        self.assertIs(e1, e2, "reason: identical re-registration must be idempotent")
        self.assertIsNot(e1, e3, "reason: different columns are different edges")
        edges = self.catalog.list_lineage()
        self.assertEqual(len(edges), 2,
                         f"reason: two distinct column edges expected, got {len(edges)}")

    def test_failed_cycle_keeps_graph_unchanged(self):
        """A rejected edge must leave no partial state behind."""
        self.catalog.add_lineage("a", "b")
        before = len(self.catalog.list_lineage())
        with self.assertRaises(LineageCycleError):
            self.catalog.add_lineage("b", "a")
        after = len(self.catalog.list_lineage())
        self.assertEqual(before, after,
                         f"reason: rejected edge mutated graph ({before} -> {after})")


if __name__ == "__main__":
    unittest.main(verbosity=2)

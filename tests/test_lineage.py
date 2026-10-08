"""Tests for dangling references and cycle rejection."""
import unittest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import (
    AssetNotFoundError,
    ColumnNotFoundError,
    LineageCycleError,
    SelfDependencyError,
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
        """a -> a is an immediate cycle."""
        log_case("self dependency", edge=("a", "a"))
        with self.assertRaises(LineageCycleError) as ctx:
            self.catalog.add_lineage("a", "a")
        self.assertEqual(ctx.exception.code, "lineage_cycle")
        self.assertIn("a", ctx.exception.cycle)

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


class SelfDependencyShapeTest(unittest.TestCase):
    """The three self-dependency shapes are each rejected on the spot with a
    distinguishable kind, while same-table different-column relations and
    normal cross-asset relations remain legal."""

    def setUp(self):
        self.catalog = MetadataCatalog()
        self.catalog.register_asset(
            "a", "a", "table",
            [("id", "bigint"), ("v", "string"), ("x", "int")])
        self.catalog.register_asset(
            "b", "b", "table", [("id", "bigint"), ("v", "string")])

    def _assert_self_dependency(self, label, kind, **kwargs):
        before = {e.key() for e in self.catalog.list_lineage()}
        with self.assertRaises(SelfDependencyError) as ctx:
            self.catalog.add_lineage("a", "a", **kwargs)
        err = ctx.exception
        log_case(label, endpoints=("a", "a"),
                 columns=(kwargs.get("upstream_column"),
                          kwargs.get("downstream_column")),
                 code=err.code, kind=err.kind, cycle=err.cycle)
        self.assertEqual(err.code, "lineage_cycle")
        self.assertEqual(err.kind, kind,
                         f"reason: {label} must classify as {kind}, got {err.kind}")
        self.assertEqual(err.cycle, ["a", "a"])
        self.assertIsInstance(err, LineageCycleError,
                              "reason: self-dependency stays a lineage_cycle")
        after = {e.key() for e in self.catalog.list_lineage()}
        self.assertEqual(before, after,
                         "reason: rejected self-dependency must not touch the graph")

    def test_whole_asset_to_itself(self):
        self._assert_self_dependency("self dependency a->a",
                                     SelfDependencyError.KIND_ASSET_SELF)

    def test_same_column_to_itself(self):
        self._assert_self_dependency("same column a.id->a.id",
                                     SelfDependencyError.KIND_COLUMN_SELF,
                                     upstream_column="id",
                                     downstream_column="id")

    def test_table_to_its_own_column_downstream_side(self):
        self._assert_self_dependency("whole table -> own column a->a.id",
                                     SelfDependencyError.KIND_ASSET_OWN_COLUMN,
                                     downstream_column="id")

    def test_own_column_to_table_upstream_side(self):
        self._assert_self_dependency("own column -> whole table a.id->a",
                                     SelfDependencyError.KIND_ASSET_OWN_COLUMN,
                                     upstream_column="id")

    def test_different_columns_of_same_table_are_legal(self):
        """a.id -> a.v and further same-table column relations are not cycles
        and must register cleanly, one after another."""
        e1 = self.catalog.add_lineage("a", "a", "id", "v")
        log_case("same-table different columns #1",
                 edge=("a", "a"), columns=("id", "v"))
        e2 = self.catalog.add_lineage("a", "a", "v", "x")
        log_case("same-table different columns #2 after #1 exists",
                 edge=("a", "a"), columns=("v", "x"))
        e3 = self.catalog.add_lineage("a", "a", "id", "x")
        self.assertEqual([e3.upstream_column, e3.downstream_column],
                         ["id", "x"])
        keys = {e.key() for e in self.catalog.list_lineage()}
        self.assertEqual(len(keys), 3,
                         "reason: three distinct same-table column edges expected")
        # registering one already present is idempotent, not a self-loop error
        again = self.catalog.add_lineage("a", "a", "id", "v")
        self.assertIs(again, e1)
        self.assertEqual(len(self.catalog.list_lineage()), 3)

    def test_one_sided_column_self_dependency_does_not_poison_later_edges(self):
        """A rejected table<->own-column registration must leave no edge that
        would make later legal relations look cyclic."""
        with self.assertRaises(SelfDependencyError):
            self.catalog.add_lineage("a", "a", upstream_column="id")
        with self.assertRaises(SelfDependencyError):
            self.catalog.add_lineage("a", "a", downstream_column="v")
        # these must still work: no phantom edge remains in the graph
        e1 = self.catalog.add_lineage("a", "a", "id", "v")
        e2 = self.catalog.add_lineage("a", "b", "id", "id")
        self.assertEqual(len(self.catalog.list_lineage()), 2)
        log_case("poisoning check",
                 legal_after_rejects=[("a", "a", "id", "v"),
                                      ("a", "b", "id", "id")])

    def test_legal_intra_asset_edge_does_not_block_cross_asset_cycles_check(self):
        """Real multi-asset cycles are still detected even while legal
        same-table column relations exist."""
        self.catalog.add_lineage("a", "a", "id", "v")
        self.catalog.add_lineage("a", "b")
        with self.assertRaises(LineageCycleError) as ctx:
            self.catalog.add_lineage("b", "a")
        err = ctx.exception
        log_case("cross-asset cycle beside intra-asset edge",
                 existing=["a.id->a.v", "a->b"], closing="b->a",
                 cycle=err.cycle)
        self.assertNotIsInstance(err, SelfDependencyError)
        self.assertEqual(err.cycle, ["a", "b", "a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

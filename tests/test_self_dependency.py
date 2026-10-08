"""Regression tests for self-dependency rules and single/batch consistency.

Covered business scenarios:
- every self-dependency shape is rejected on the spot with the
  distinguishable code 'self_dependency': whole table -> itself (a->a),
  same column -> itself (a.x->a.x), whole table -> its own column (a->a.x),
  column -> its own table (a.x->a);
- two *different* columns of one table (a.x->a.y) and ordinary cross-asset
  relations stay legal;
- single registration (add_lineage) and batch commit (commit_lineage_batch)
  reach the SAME conclusion for the SAME relation;
- a rejected operation never mutates the graph (no edge, no audit record),
  a rejected batch applies nothing at all, and one stray self-referencing
  edge already stored in the graph cannot make legitimate relations be
  misjudged as cycles.
"""
import unittest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import (
    BatchLineageError,
    LineageCycleError,
    SelfLineageError,
)
from metadata_catalog.models import LineageEdge
from tests._helpers import log_case

SELF_DEPENDENCY_CASES = [
    # (label, upstream_column, downstream_column, expected kind)
    ("whole table -> itself", None, None, "asset"),
    ("same column -> itself", "x", "x", "column"),
    ("whole table -> its own column", None, "x", "asset_to_column"),
    ("column -> its own table", "x", None, "column_to_asset"),
]


class SelfDependencyRejectionTest(unittest.TestCase):
    def setUp(self):
        self.catalog = MetadataCatalog()
        for aid in ("a", "b"):
            self.catalog.register_asset(
                aid, aid, "table", [("x", "int"), ("y", "int")])

    def test_every_self_dependency_shape_rejected_single_entry(self):
        for label, uc, dc, kind in SELF_DEPENDENCY_CASES:
            with self.subTest(shape=label):
                log_case("single-entry self-dependency", shape=label,
                         edge=("a", uc, "->", "a", dc))
                with self.assertRaises(SelfLineageError) as ctx:
                    self.catalog.add_lineage("a", "a", uc, dc)
                err = ctx.exception
                # still a LineageCycleError for backwards-compatible handling
                self.assertIsInstance(err, LineageCycleError)
                self.assertEqual(err.code, "self_dependency",
                                 f"reason: {label} must carry the "
                                 f"distinguishable self_dependency code")
                self.assertEqual(err.kind, kind,
                                 f"reason: {label} misclassified as {err.kind}")
                self.assertEqual(err.cycle, ["a", "a"])
        self.assertEqual(self.catalog.list_lineage(), [],
                         "reason: rejected self-dependencies must not be stored")

    def test_every_self_dependency_shape_rejected_in_batch(self):
        ops = [{"op": "add", "upstream": "a", "downstream": "a",
                "upstream_column": uc, "downstream_column": dc}
               for _, uc, dc, _ in SELF_DEPENDENCY_CASES]
        log_case("batch of self-dependencies",
                 shapes=[label for label, *_ in SELF_DEPENDENCY_CASES])
        with self.assertRaises(BatchLineageError) as ctx:
            self.catalog.commit_lineage_batch(ops)
        failures = ctx.exception.failures
        log_case("batch rejection detail",
                 reported=[(f.index, f.code, f.message) for f in failures])
        self.assertEqual([f.index for f in failures], [0, 1, 2, 3],
                         "reason: every self-dependency item must be listed")
        self.assertTrue(all(f.code == "self_dependency" for f in failures),
                        "reason: batch uses the same distinguishable code "
                        "as single registration")
        self.assertEqual(self.catalog.list_lineage(), [],
                         "reason: rejected batch applies nothing at all")

    def test_rejection_leaves_no_trace_in_graph_or_audit(self):
        before_audit = self.catalog.lineage_audit("a")
        self.assertEqual(before_audit.status, "no_changes")
        for _, uc, dc, _ in SELF_DEPENDENCY_CASES:
            with self.assertRaises(SelfLineageError):
                self.catalog.add_lineage("a", "a", uc, dc)
        log_case("post-rejection state", edges=len(self.catalog.list_lineage()),
                 audit_status=self.catalog.lineage_audit("a").status)
        self.assertEqual(self.catalog.list_lineage(), [])
        self.assertEqual(self.catalog.lineage_audit("a").status, "no_changes",
                         "reason: a rejected registration must not even "
                         "write an audit record")


class IntraAssetColumnRelationTest(unittest.TestCase):
    def setUp(self):
        self.catalog = MetadataCatalog()
        for aid in ("a", "b"):
            self.catalog.register_asset(
                aid, aid, "table", [("x", "int"), ("y", "int")])

    def test_same_table_different_columns_accepted_single_entry(self):
        edge = self.catalog.add_lineage("a", "a", "x", "y")
        log_case("intra-asset column relation", edge=edge.key(),
                 stored=len(self.catalog.list_lineage()))
        self.assertEqual(edge.key(), ("a", "a", "x", "y"))
        self.assertEqual(len(self.catalog.list_lineage()), 1)

    def test_same_table_different_columns_accepted_in_batch(self):
        result = self.catalog.commit_lineage_batch([
            {"op": "add", "upstream": "a", "downstream": "a",
             "upstream_column": "x", "downstream_column": "y"},
        ])
        log_case("intra-asset relation via batch", applied=result["applied"],
                 stored=len(self.catalog.list_lineage()))
        self.assertEqual(result["applied"], 1,
                         "reason: batch must not misjudge a.x->a.y as a cycle")
        self.assertEqual(len(self.catalog.list_lineage()), 1)

    def test_single_and_batch_agree_on_every_relation_shape(self):
        """For each relation shape, both entrances must give the same
        accept/reject conclusion."""
        relations = [
            ("cross-asset asset-level", "a", "b", None, None, True),
            ("cross-asset column-level", "a", "b", "x", "y", True),
            ("intra-asset different columns", "a", "a", "x", "y", True),
            ("intra-asset reversed columns", "a", "a", "y", "x", True),
            ("whole table -> itself", "a", "a", None, None, False),
            ("same column -> itself", "a", "a", "x", "x", False),
            ("whole table -> own column", "a", "a", None, "x", False),
            ("column -> own table", "a", "a", "x", None, False),
        ]
        for label, up, dn, uc, dc, expected_ok in relations:
            with self.subTest(relation=label):
                single_ok = batch_ok = False
                try:
                    self.catalog.add_lineage(up, dn, uc, dc)
                    single_ok = True
                except (SelfLineageError, LineageCycleError):
                    pass
                try:
                    self.catalog.commit_lineage_batch([
                        {"op": "add", "upstream": up, "downstream": dn,
                         "upstream_column": uc, "downstream_column": dc}])
                    batch_ok = True
                except BatchLineageError:
                    pass
                log_case("entrance consistency", relation=label,
                         single_accepted=single_ok, batch_accepted=batch_ok,
                         expected=expected_ok)
                self.assertEqual(single_ok, expected_ok,
                                 f"reason: single registration disagrees on "
                                 f"{label}")
                self.assertEqual(batch_ok, expected_ok,
                                 f"reason: batch commit disagrees on {label}")

    def test_cross_asset_relations_unaffected(self):
        self.catalog.add_lineage("a", "b")
        self.catalog.add_lineage("a", "b", "x", "y")
        edges = {e.key() for e in self.catalog.list_lineage()}
        log_case("cross-asset relations", edges=sorted(edges))
        self.assertEqual(edges, {("a", "b", "", ""), ("a", "b", "x", "y")})


class GraphSelfConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.catalog = MetadataCatalog()
        for aid in ("a", "b", "c"):
            self.catalog.register_asset(
                aid, aid, "table", [("x", "int"), ("y", "int")])

    def _inject_legacy_edge(self, upstream, downstream, uc=None, dc=None):
        """Simulate a self-referencing edge written before the rules were
        enforced (e.g. an older deployment or a migrated store)."""
        edge = LineageEdge(upstream, downstream, uc, dc)
        self.catalog._edge_objs[edge.key()] = edge
        self.catalog._edges.add(edge.key())
        return edge

    def test_stray_self_referencing_edge_does_not_poison_cycle_checks(self):
        """A stored a->a.x edge must not make later legitimate relations on
        the same table be misjudged as cycles."""
        self._inject_legacy_edge("a", "a", None, "x")
        log_case("legacy poisoned graph",
                 stray_edge=("a", "a", None, "x"),
                 then_register=["a.y->a.x (intra-asset)", "a->b", "b->c"])
        intra = self.catalog.add_lineage("a", "a", "y", "x")
        self.assertEqual(intra.key(), ("a", "a", "y", "x"),
                         "reason: legit intra-asset relation misjudged "
                         "because of a stray self-referencing edge")
        self.catalog.add_lineage("a", "b")
        self.catalog.add_lineage("b", "c")
        self.assertEqual(len(self.catalog.list_lineage()), 4)

    def test_stray_edge_does_not_create_phantom_cycles_elsewhere(self):
        """c->a is legal while a stray a->a.x exists; only a REAL path
        c -> ... -> a would make it a cycle."""
        self._inject_legacy_edge("a", "a", None, "x")
        self.catalog.add_lineage("a", "b")
        edge = self.catalog.add_lineage("c", "a")
        log_case("no phantom cycle", added=edge.key())
        self.assertEqual(edge.key(), ("c", "a", "", ""))
        # and a genuine cycle is still caught on top of the stray edge:
        # b->c would close b -> c -> a -> b
        with self.assertRaises(LineageCycleError) as ctx:
            self.catalog.add_lineage("b", "c")
        self.assertEqual(ctx.exception.code, "lineage_cycle")

    def test_real_cycles_still_detected_after_fixes(self):
        self.catalog.add_lineage("a", "b")
        self.catalog.add_lineage("b", "c")
        log_case("genuine 3-node cycle", closing_edge=("c", "a"))
        with self.assertRaises(LineageCycleError) as ctx:
            self.catalog.add_lineage("c", "a")
        self.assertEqual(ctx.exception.code, "lineage_cycle",
                         "reason: genuine cycles must keep the generic code")
        self.assertEqual(ctx.exception.cycle[0], "a")

    def test_rejected_batch_with_self_dependency_applies_nothing(self):
        self.catalog.add_lineage("a", "b")
        before = {e.key() for e in self.catalog.list_lineage()}
        with self.assertRaises(BatchLineageError) as ctx:
            self.catalog.commit_lineage_batch([
                {"op": "add", "upstream": "b", "downstream": "c"},      # valid
                {"op": "add", "upstream": "a", "downstream": "a",
                 "downstream_column": "x"},                              # a->a.x
                {"op": "add", "upstream": "c", "downstream": "c",
                 "upstream_column": "x", "downstream_column": "x"},      # c.x->c.x
            ])
        codes = [(f.index, f.code) for f in ctx.exception.failures]
        log_case("mixed batch with self-dependencies", failures=codes,
                 graph_before=sorted(before),
                 graph_after=sorted(e.key() for e in self.catalog.list_lineage()))
        self.assertEqual(codes, [(1, "self_dependency"), (2, "self_dependency")])
        self.assertEqual({e.key() for e in self.catalog.list_lineage()}, before,
                         "reason: rejected batch must not apply even the "
                         "valid item")


if __name__ == "__main__":
    unittest.main(verbosity=2)

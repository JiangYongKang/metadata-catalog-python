"""Tests for point-in-time / per-version lineage reconstruction and running
impact analysis on reconstructed graphs."""
import unittest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import (
    AssetNotFoundError,
    InvalidTimePointError,
    TimePointOutOfRangeError,
)
from metadata_catalog.impact import analyze_impact

from tests._helpers import log_case
from tests.test_audit import FakeClock


class PointInTimeReconstructionTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.catalog = MetadataCatalog(clock=self.clock)
        for aid in "abcdef":
            self.catalog.register_asset(aid, aid, "table",
                                        [("id", "bigint")])

    def _timeline(self):
        # a->b, b->c, a->d, then cancel a->d ; record each change time
        timeline = []
        for edge in (("a", "b", None, None),
                     ("b", "c", None, None),
                     ("a", "d", None, None)):
            self.catalog.add_lineage(*edge)
            timeline.append(self.catalog._audit[-1].at)
        self.catalog.remove_lineage("a", "d")
        timeline.append(self.catalog._audit[-1].at)
        return timeline  # [t_ab, t_bc, t_ad, t_remove_ad]

    def test_graph_matches_each_historical_moment(self):
        t_ab, t_bc, t_ad, t_rm = self._timeline()
        expect = {
            t_ab: {("a", "b")},
            t_bc: {("a", "b"), ("b", "c")},
            t_ad: {("a", "b"), ("b", "c"), ("a", "d")},
            t_rm: {("a", "b"), ("b", "c")},
        }
        for t, want in expect.items():
            view = self.catalog.lineage_as_of(t)
            got = {(e.upstream, e.downstream) for e in view.edges()}
            log_case("as-of snapshot", at=t, want=sorted(want),
                     got=sorted(got))
            self.assertEqual(got, want,
                             f"reason: graph at {t} mismatches history")

    def test_same_timestamp_is_fully_reproducible(self):
        self._timeline()
        t = self.catalog._audit[1].at
        views = [[(e.upstream, e.downstream)
                  for e in self.catalog.lineage_as_of(t).edges()]
                 for _ in range(20)]
        self.assertEqual(len({tuple(v) for v in views}), 1,
                         "reason: repeated reconstruction must be identical")
        log_case("determinism 20 runs", at=t, edges=views[0])

    def test_reconstructed_impact_matches_live_impact_at_that_moment(self):
        """The headline property: impact on the reconstructed graph equals
        what a live query at that point in time would have returned."""
        self._timeline()
        t = self.catalog._audit[2].at   # moment a->d still exists
        view = self.catalog.lineage_as_of(t)
        then = analyze_impact(view, "a", "downstream")
        # rebuild a brand-new catalog reproducing the same live state
        fresh = MetadataCatalog(clock=FakeClock())
        for aid in "abcdef":
            fresh.register_asset(aid, aid, "table", [("id", "bigint")])
        fresh.add_lineage("a", "b")
        fresh.add_lineage("b", "c")
        fresh.add_lineage("a", "d")
        now = analyze_impact(fresh, "a", "downstream")
        log_case("reconstruction vs live", at=t,
                 reconstructed=(then.nodes, then.depths),
                 live=(now.nodes, now.depths))
        self.assertEqual(then.nodes, now.nodes)
        self.assertEqual(then.depths, now.depths)
        # and it differs from *today's* live graph (a->d was since removed)
        today = analyze_impact(self.catalog, "a", "downstream")
        self.assertNotIn("d", today.nodes)
        self.assertIn("d", then.nodes)

    def test_illegal_time_arguments_are_errors(self):
        self._timeline()
        for bad in ("noon", None, True, float("nan"), float("inf"), -float("inf")):
            log_case("invalid as-of time", value=bad)
            with self.assertRaises(InvalidTimePointError) as ctx:
                self.catalog.lineage_as_of(bad)
            self.assertEqual(ctx.exception.code, "invalid_time_point")

    def test_time_outside_window_is_an_error_not_empty_graph(self):
        t_ab, _, _, _ = self._timeline()
        earliest = self.catalog._audit[0].at
        latest = self.catalog._audit[-1].at
        log_case("window bounds", earliest=earliest, latest=latest)
        for bad, label in ((earliest - 10.0, "before first change"),
                           (latest + 10.0, "after newest state")):
            with self.assertRaises(TimePointOutOfRangeError) as ctx:
                self.catalog.lineage_as_of(bad)
            err = ctx.exception
            log_case(label, requested=bad, window=(err.earliest, err.latest))
            self.assertEqual(err.code, "time_point_out_of_range")
            self.assertEqual((err.earliest, err.latest), (earliest, latest))

    def test_no_history_at_all_reports_out_of_range(self):
        with self.assertRaises(TimePointOutOfRangeError):
            self.catalog.lineage_as_of(1500.0)

    def test_reconstruction_by_asset_version(self):
        # version 1 of b is created before any lineage; v2 after a->b, b->c
        self.catalog.add_lineage("a", "b")
        self.catalog.add_lineage("b", "c")
        self.catalog.register_asset("b", "b", "table",
                                    [("id", "bigint"), ("new", "int")],
                                    change_note="add new column")
        v1 = self.catalog.lineage_at_version("b", 1)
        v2 = self.catalog.lineage_at_version("b", 2)
        log_case("version reconstruction",
                 v1_edges=[(e.upstream, e.downstream) for e in v1.edges()],
                 v2_edges=[(e.upstream, e.downstream) for e in v2.edges()])
        self.assertEqual(v1.edge_count(), 0,
                         "reason: no lineage existed when b v1 was created")
        self.assertEqual({(e.upstream, e.downstream) for e in v2.edges()},
                         {("a", "b"), ("b", "c")})
        r1 = analyze_impact(v1, "b", "upstream")
        r2 = analyze_impact(v2, "b", "upstream")
        self.assertEqual(r1.nodes, ["b"])
        self.assertEqual(r2.nodes, ["b", "a"])
        with self.assertRaises(AssetNotFoundError):
            self.catalog.lineage_at_version("ghost", 1)
        with self.assertRaises(AssetNotFoundError):
            self.catalog.lineage_at_version("b", 99)

    def test_replay_crosses_checkpoint_boundaries_correctly(self):
        """Force >2 checkpoints (128 each) and verify deep reconstruction,
        including a removal that lands after a checkpoint."""
        n = 300
        for i in range(n):
            aid = f"n{i:03d}"
            self.catalog.register_asset(aid, aid, "table")
            self.catalog.add_lineage(aid, "f")
        t_mid = self.catalog._audit[149].at
        view_mid = self.catalog.lineage_as_of(t_mid)
        log_case("mid-history reconstruction", checkpoints=sorted(self.catalog._checkpoints),
                 edges_at_150=view_mid.edge_count())
        self.assertEqual(view_mid.edge_count(), 150)
        self.catalog.remove_lineage("n000", "f")
        view_end = self.catalog.lineage_as_of(self.catalog._audit[-1].at)
        self.assertEqual(view_end.edge_count(), n - 1)
        self.assertNotIn(("n000", "f", "", ""), view_end._edge_objs)
        # second deep query uses cached checkpoints; results stay consistent
        again = self.catalog.lineage_as_of(t_mid)
        self.assertEqual(again.edge_count(), 150)


if __name__ == "__main__":
    unittest.main(verbosity=2)

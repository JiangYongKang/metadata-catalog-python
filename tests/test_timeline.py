"""Point-in-time / asset-version reconstruction tests.

Covers: invalid time parameters, reconstruction == the live impact result
captured at that moment, repeat consistency, version-anchored reconstruction
and folding performance on a large graph.
"""
import time as wall
import unittest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import AssetNotFoundError, LineageTimeError
from metadata_catalog.impact import analyze_impact

from tests._helpers import log_case
from tests.test_audit import FakeClock


class TimelineReconstructionTest(unittest.TestCase):
    def setUp(self):
        self.clk = FakeClock(1000.0)
        self.cat = MetadataCatalog(clock=self.clk)
        for aid in ("a", "b", "c", "d", "e"):
            self.cat.register_asset(aid, aid, "table", [("id", "bigint")])
            self.clk.tick()

    def _capture_live(self, moments, label):
        """Freeze current clock value and snapshot the live impact answers."""
        ts = self.clk()
        moments[label] = {
            "ts": ts,
            "down_a": analyze_impact(self.cat, "a", "downstream").to_dict(),
            "up_d": analyze_impact(self.cat, "d", "upstream").to_dict(),
            "edges": sorted((e.upstream, e.downstream)
                            for e in self.cat.list_lineage()),
        }

    def test_reconstruction_matches_live_answers_at_each_moment(self):
        moments = {}
        # moment 1: a->b->c
        self.cat.add_lineage("a", "b"); self.clk.tick()
        self.cat.add_lineage("b", "c"); self.clk.tick()
        self._capture_live(moments, "m1_chain")
        self.clk.tick()
        # moment 2: c->d added
        self.cat.add_lineage("c", "d"); self.clk.tick()
        self._capture_live(moments, "m2_to_d")
        self.clk.tick()
        # moment 3: reroute b->c to b->d (diamond-ish then chain changes)
        self.cat.remove_lineage("b", "c"); self.clk.tick()
        self.cat.add_lineage("b", "d"); self.clk.tick()
        self._capture_live(moments, "m3_rerouted")
        self.clk.tick()
        # moment 4: add a shortcut a->d
        self.cat.add_lineage("a", "d"); self.clk.tick()
        self._capture_live(moments, "m4_shortcut")

        for label, captured in moments.items():
            snap = self.cat.lineage_as_of(captured["ts"])
            log_case(f"reconstruct {label}", as_of=captured["ts"],
                     live_edges=captured["edges"],
                     snap_edges=sorted((e.upstream, e.downstream)
                                       for e in snap.list_edges()))
            self.assertEqual(
                sorted((e.upstream, e.downstream) for e in snap.list_edges()),
                captured["edges"],
                f"reason[{label}]: reconstructed edges differ from live edges")
            down = analyze_impact(snap, "a", "downstream").to_dict()
            up = analyze_impact(snap, "d", "upstream").to_dict()
            self.assertEqual(down, captured["down_a"],
                             f"reason[{label}]: reconstructed downstream "
                             f"impact must match the answer given on site")
            self.assertEqual(up, captured["up_d"],
                             f"reason[{label}]: reconstructed upstream "
                             f"impact must match the answer given on site")

    def test_same_timestamp_repeatedly_identical(self):
        self.cat.add_lineage("a", "b"); self.clk.tick()
        self.cat.add_lineage("b", "c"); ts = self.clk.tick()
        runs = []
        for _ in range(20):
            snap = self.cat.lineage_as_of(ts)
            runs.append((snap.as_of,
                         tuple(e.key() for e in snap.list_edges()),
                         tuple(analyze_impact(snap, "a").nodes)))
        self.assertEqual(len(set(runs)), 1,
                         f"reason: retrospective queries at one instant must "
                         f"be identical across repeats, got {set(runs)}")
        log_case("repeat consistency over 20 runs", as_of=ts,
                 result=runs[0][1:])

    def test_invalid_timestamp_is_explicit_error_never_empty_graph(self):
        self.cat.add_lineage("a", "b"); self.clk.tick()
        cases = [("string", "then", "invalid_type"),
                 (float("nan"), None, "invalid_value"),
                 (float("inf"), None, "invalid_value"),
                 (999.0, None, "before_history"),
                 (self.clk() + 10_000, None, "future")]
        for raw, _, reason in cases:
            log_case("invalid as_of", requested=raw, expect=reason)
            with self.assertRaises(LineageTimeError) as ctx:
                self.cat.lineage_as_of(raw)
            err = ctx.exception
            self.assertEqual(err.reason, reason,
                             f"reason: {raw!r} should be {reason}, got "
                             f"{err.reason}")
            self.assertEqual(err.code, "invalid_time")
        # before catalog creation: diagnostic carries the boundary
        try:
            self.cat.lineage_as_of(999.0)
        except LineageTimeError as err:
            self.assertIsNotNone(err.earliest)
            self.assertEqual(err.requested, 999.0)

    def test_empty_graph_in_window_is_reconstructable_as_empty(self):
        # catalog exists, no lineage yet; current clock is inside the window
        ts = self.clk()
        snap = self.cat.lineage_as_of(ts)
        self.assertEqual(snap.list_edges(), [])
        log_case("empty in-window snapshot", as_of=ts, edges=0)

    def test_version_anchored_reconstruction(self):
        # v1 of b exists before any edge; edges are added; b then evolves to v2
        v1_created = self.cat.get_version("b", 1).created_at
        self.cat.add_lineage("a", "b"); self.clk.tick()
        self.cat.add_lineage("b", "c"); self.clk.tick()
        self.cat.register_asset("b", "b", "table",
                                [("id", "bigint"), ("x", "int")])
        v2_created = self.cat.get_version("b", 2).created_at

        snap_v1 = self.cat.lineage_at_asset_version("b", 1)
        snap_v2 = self.cat.lineage_at_asset_version("b", 2)
        log_case("version anchored", v1_created=v1_created, v2_created=v2_created,
                 v1_edges=[(e.upstream, e.downstream) for e in snap_v1.list_edges()],
                 v2_edges=[(e.upstream, e.downstream) for e in snap_v2.list_edges()])
        self.assertEqual(snap_v1.as_of, v1_created)
        self.assertEqual(snap_v1.list_edges(), [],
                         "reason: lineage did not exist when b was at v1")
        self.assertEqual(snap_v2.as_of, v2_created)
        self.assertEqual(sorted((e.upstream, e.downstream)
                                for e in snap_v2.list_edges()),
                         [("a", "b"), ("b", "c")])
        self.assertEqual(analyze_impact(snap_v2, "a", "downstream").nodes,
                         ["a", "b", "c"])

        with self.assertRaises(AssetNotFoundError):
            self.cat.lineage_at_asset_version("b", 99)
        with self.assertRaises(AssetNotFoundError):
            self.cat.lineage_at_asset_version("ghost", 1)
        with self.assertRaises(LineageTimeError) as ctx:
            self.cat.lineage_at_asset_version("b", 0)
        self.assertEqual(ctx.exception.reason, "invalid_version")

    def test_batch_commit_visible_atomically_at_its_instant(self):
        self.cat.add_lineage("a", "b"); self.clk.tick()
        commit_t = self.clk()
        self.cat.commit_lineage_batch([
            {"action": "add", "upstream": "b", "downstream": "c"},
            {"action": "add", "upstream": "c", "downstream": "d"},
        ])
        # exactly one timestamp before the batch
        before = self.cat.lineage_as_of(commit_t - 0.001)
        at = self.cat.lineage_as_of(commit_t)
        self.assertEqual([(e.upstream, e.downstream) for e in before.list_edges()],
                         [("a", "b")])
        self.assertEqual(sorted((e.upstream, e.downstream) for e in at.list_edges()),
                         [("a", "b"), ("b", "c"), ("c", "d")],
                         "reason: a batch must appear as one atomic instant")
        log_case("batch atomicity in reconstruction", commit_t=commit_t,
                 before=1, at=3)

    def test_reconstruction_is_lightweight_and_does_not_slow_live_queries(self):
        n = 5000
        self.cat.register_asset("hub", "hub", "table", [("id", "bigint")])
        self.cat.register_asset("leaf0", "leaf0", "table", [("id", "bigint")])
        ops = []
        for i in range(1, n + 1):
            aid = f"node{i:05d}"
            self.cat.register_asset(aid, aid, "table", [("id", "bigint")])
            ops.append({"action": "add", "upstream": "hub", "downstream": aid})
        self.clk.tick()
        self.cat.commit_lineage_batch(ops)
        self.clk.tick()
        as_of = self.clk()

        t0 = wall.perf_counter()
        for _ in range(100):
            self.cat.downstream_neighbors("hub")
        live_elapsed = wall.perf_counter() - t0

        t0 = wall.perf_counter()
        snap = self.cat.lineage_as_of(as_of)
        fold_elapsed = wall.perf_counter() - t0
        log_case("large graph reconstruction", edges=n,
                 fold_seconds=round(fold_elapsed, 4),
                 live_100_queries_seconds=round(live_elapsed, 4))
        self.assertEqual(len(snap.list_edges()), n)
        self.assertLess(fold_elapsed, 2.0,
                        "reason: folding 5k events must stay linear-time")
        self.assertLess(live_elapsed, 0.5,
                        "reason: retrospective machinery must not slow down "
                        "ordinary neighbor queries")
        # snapshots are immutable views: live mutations do not alter them
        frozen = tuple(e.key() for e in snap.list_edges())
        self.cat.add_lineage("hub", "leaf0")
        self.assertEqual(tuple(e.key() for e in snap.list_edges()), frozen,
                         "reason: an old snapshot must be immune to later writes")


if __name__ == "__main__":
    unittest.main(verbosity=2)

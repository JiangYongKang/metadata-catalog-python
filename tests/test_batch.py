"""Atomic batch lineage commit tests: all-or-nothing validation, per-item
failure reasons, and concurrency (no lost commits, no torn reads)."""
import threading
import unittest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import (
    BatchRejectedError,
    LineageCycleError,
)

from tests._helpers import log_case
from tests.test_audit import FakeClock


class BatchValidationTest(unittest.TestCase):
    def setUp(self):
        self.clk = FakeClock()
        self.cat = MetadataCatalog(clock=self.clk)
        for aid in ("a", "b", "c", "d"):
            self.cat.register_asset(aid, aid, "table",
                                    [("id", "bigint"), ("v", "string")])

    def test_one_bad_op_rolls_back_the_whole_batch(self):
        ops = [
            {"action": "add", "upstream": "a", "downstream": "b"},
            {"action": "add", "upstream": "a", "downstream": "ghost"},
            {"action": "add", "upstream": "c", "downstream": "d"},
        ]
        log_case("mixed good/bad batch", ops=[(i, o["action"],
                 f"{o['upstream']}->{o['downstream']}") for i, o in enumerate(ops)])
        with self.assertRaises(BatchRejectedError) as ctx:
            self.cat.commit_lineage_batch(ops)
        err = ctx.exception
        self.assertEqual(err.code, "batch_rejected")
        self.assertEqual([f.index for f in err.failures], [1],
                         "reason: exactly op[1] is bad")
        self.assertEqual(err.failures[0].code, "asset_not_found")
        self.assertEqual(err.failures[0].asset, "ghost")
        self.assertEqual(self.cat.list_lineage(), [],
                         "reason: none of the good ops may land when one op "
                         "is bad — the graph must stay exactly as before")
        # and nothing is audited for a rejected batch
        self.assertEqual(self.cat.all_lineage_changes(), [])

    def test_multiple_failures_are_reported_per_item(self):
        ops = [
            {"action": "add", "upstream": "ghost1", "downstream": "a"},
            {"action": "add", "upstream": "a", "downstream": "ghost2"},
            {"action": "add", "upstream": "a", "downstream": "b",
             "upstream_column": "id", "downstream_column": "nope"},
            {"action": "add", "upstream": "a", "downstream": "b"},
        ]
        with self.assertRaises(BatchRejectedError) as ctx:
            self.cat.commit_lineage_batch(ops)
        failures = ctx.exception.failures
        log_case("per-item failures",
                 failures=[(f.index, f.code, f.asset, f.column) for f in failures])
        self.assertEqual([f.index for f in failures], [0, 1, 2])
        self.assertEqual([f.code for f in failures],
                         ["asset_not_found", "asset_not_found",
                          "column_not_found"])
        self.assertEqual(failures[2].column, "nope")
        # each failure carries a human-readable reason and the offending op
        for f in failures:
            self.assertTrue(f.message)
            self.assertEqual(f.op.upstream, ops[f.index]["upstream"])

    def test_self_dependencies_including_column_level_are_rejected(self):
        cases = [
            ("asset self loop",
             {"action": "add", "upstream": "a", "downstream": "a"}),
            ("same column self loop",
             {"action": "add", "upstream": "a", "downstream": "a",
              "upstream_column": "id", "downstream_column": "id"}),
            ("asset<->own-column self loop",
             {"action": "add", "upstream": "a", "downstream": "a",
              "upstream_column": "id", "downstream_column": None}),
        ]
        for title, op in cases:
            log_case(title, op=op)
            with self.assertRaises(BatchRejectedError) as ctx:
                self.cat.commit_lineage_batch([op])
            f = ctx.exception.failures[0]
            self.assertEqual(f.code, "lineage_cycle",
                             f"reason[{title}]: must be lineage_cycle")
            self.assertIn("a", f.cycle)

    def test_cycle_formed_inside_one_batch_is_rejected(self):
        self.cat.commit_lineage_batch([
            {"action": "add", "upstream": "a", "downstream": "b"},
            {"action": "add", "upstream": "b", "downstream": "c"},
        ])
        log_case("closing edge in same batch", existing=["a->b", "b->c"],
                 bad="c->a")
        with self.assertRaises(BatchRejectedError) as ctx:
            self.cat.commit_lineage_batch([
                # valid op first, then the cycle closer: whole batch fails
                {"action": "add", "upstream": "a", "downstream": "d"},
                {"action": "add", "upstream": "c", "downstream": "a"},
            ])
        self.assertEqual([f.index for f in ctx.exception.failures], [1])
        self.assertEqual(self.cat.list_lineage().__len__(), 2,
                         "reason: even the valid first op must be rolled back")

    def test_different_columns_between_same_asset_is_allowed(self):
        # a.id -> a.v is a real column transformation, not a self loop
        result = self.cat.commit_lineage_batch([
            {"action": "add", "upstream": "a", "downstream": "a",
             "upstream_column": "id", "downstream_column": "v"},
        ])
        self.assertTrue(result.committed)
        self.assertEqual(len(result.applied), 1)
        log_case("a.id -> a.v accepted", edge="a.id->a.v")

    def test_idempotent_ops_are_skipped_not_reapplied(self):
        ops = [{"action": "add", "upstream": "a", "downstream": "b"}]
        r1 = self.cat.commit_lineage_batch(ops)
        r2 = self.cat.commit_lineage_batch(ops)
        self.assertEqual(len(r1.applied), 1)
        self.assertEqual(r2.applied, ())
        self.assertEqual(r2.skipped, (0,))
        # a remove of a missing edge is likewise a no-op, not an error
        r3 = self.cat.commit_lineage_batch([
            {"action": "remove", "upstream": "c", "downstream": "d"}])
        self.assertTrue(r3.committed)
        self.assertEqual(r3.applied, ())
        log_case("idempotent batch", applied_1=1, applied_2=0, skipped=r2.skipped)

    def test_empty_batch_is_a_noop_success(self):
        r = self.cat.commit_lineage_batch([])
        self.assertTrue(r.committed)
        self.assertEqual(r.applied, ())

    def test_remove_and_readd_mix_applies_in_order(self):
        self.cat.add_lineage("a", "b")
        self.clk.tick()
        r = self.cat.commit_lineage_batch([
            {"action": "remove", "upstream": "a", "downstream": "b"},
            {"action": "add", "upstream": "a", "downstream": "b"},
        ])
        # remove then re-add: net effect present, one removal + one addition
        actions = [(c.action, c.changed) for c in r.changes]
        self.assertEqual(actions, [("remove", True), ("add", True)])
        self.assertTrue(self.cat.asset_exists and
                        any(e.upstream == "a" and e.downstream == "b"
                            for e in self.cat.list_lineage()))
        log_case("remove+readd in one batch", actions=actions)


class BatchConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.cat = MetadataCatalog()
        # chain anchors + many disjoint nodes
        self.cat.register_asset("hub", "hub", "table")
        for i in range(200):
            self.cat.register_asset(f"n{i:03d}", f"n{i:03d}", "table")

    def test_concurrent_batches_lose_no_commits(self):
        n_threads = 16
        per_thread = 12  # 192 distinct edges on disjoint nodes
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def worker(t: int):
            ops = [{"action": "add", "upstream": "hub",
                    "downstream": f"n{t * per_thread + i:03d}"}
                   for i in range(per_thread)]
            barrier.wait()
            try:
                self.cat.commit_lineage_batch(ops)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,))
                   for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        edges = self.cat.list_lineage()
        log_case("concurrent batches", threads=n_threads,
                 ops_per_thread=per_thread, committed_edges=len(edges))
        self.assertEqual(len(edges), n_threads * per_thread,
                         "reason: every edge of every concurrent batch must "
                         "land — no lost updates")
        targets = {f"n{t * per_thread + i:03d}"
                   for t in range(n_threads) for i in range(per_thread)}
        self.assertEqual({e.downstream for e in edges}, targets)
        # one audit entry per committed edge
        self.assertEqual(len(self.cat.all_lineage_changes()),
                         n_threads * per_thread)

    def test_reader_never_sees_a_half_applied_batch(self):
        stop = threading.Event()
        torn: list[tuple] = []
        checked = {"count": 0}

        def reader():
            # batches commit pairs (hub->n{2k}, hub->n{2k+1}); any consistent
            # snapshot shows even-numbered members only in pairs
            while not stop.is_set():
                downs = {e.downstream for e in self.cat.list_lineage()}
                checked["count"] += 1
                for k in range(200):
                    pair = (f"n{2 * k:03d}" in downs, f"n{2 * k + 1:03d}" in downs)
                    if pair == (True, False):
                        torn.append(pair)

        def writer():
            for k in range(100):
                self.cat.commit_lineage_batch([
                    {"action": "add", "upstream": "hub",
                     "downstream": f"n{2 * k:03d}"},
                    {"action": "add", "upstream": "hub",
                     "downstream": f"n{2 * k + 1:03d}"},
                ])

        rt = threading.Thread(target=reader)
        rt.start()
        wt = threading.Thread(target=writer)
        wt.start()
        wt.join()
        stop.set()
        rt.join()
        log_case("readers during batch commits", checks=checked["count"],
                 torn_reads=len(torn))
        self.assertEqual(torn, [],
                         "reason: a reader must never observe one edge of a "
                         "batch without the other")
        self.assertEqual(len(self.cat.list_lineage()), 200)

    def test_single_edge_legacy_api_still_validates_and_audits(self):
        # legacy single registration semantics preserved under the new layer
        edge = self.cat.add_lineage("hub", "n000")
        self.assertEqual((edge.upstream, edge.downstream), ("hub", "n000"))
        self.assertTrue(self.cat.remove_lineage("hub", "n000"))
        with self.assertRaises(Exception):
            self.cat.add_lineage("hub", "n000", "missing_col", "missing_col")
        log_case("legacy single-edge API unchanged",
                 changes=self.cat.all_lineage_changes().__len__())

    def test_stress_committers_and_rejecters_serialize_cleanly(self):
        """40 threads: half commit valid batches, half intentionally submit
        batches that must be rejected. No error escapes, no good edge is lost
        and no rejected edge ever appears."""
        n = 40
        barrier = threading.Barrier(n)
        good_threads = n // 2
        rejected: list[int] = []
        accepted: list[int] = []
        lock = threading.Lock()

        def worker(t: int):
            barrier.wait()
            for round_i in range(5):
                if t < good_threads:
                    leaf = f"s{t:03d}_{round_i}"
                    self.cat.register_asset(leaf, leaf, "table")
                    try:
                        self.cat.commit_lineage_batch([
                            {"action": "add", "upstream": "hub",
                             "downstream": leaf}])
                        with lock:
                            accepted.append(t * 100 + round_i)
                    except BatchRejectedError:  # pragma: no cover
                        pass
                else:
                    try:
                        self.cat.commit_lineage_batch([
                            {"action": "add", "upstream": "hub",
                             "downstream": f"ghost_{t}_{round_i}"}])
                    except BatchRejectedError:
                        with lock:
                            rejected.append(t * 100 + round_i)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        downs = {e.downstream for e in self.cat.list_lineage()}
        log_case("stress: good vs rejected batches",
                 good_commits=len(accepted), rejected_batches=len(rejected),
                 edges=len(downs))
        self.assertEqual(len(accepted), good_threads * 5,
                         "reason: every valid batch must commit exactly once")
        self.assertEqual(len(rejected), (n - good_threads) * 5,
                         "reason: every bad batch must be rejected")
        self.assertEqual(len(downs), good_threads * 5,
                         "reason: graph contains only committed edges")
        self.assertFalse(any(d.startswith("ghost_") for d in downs),
                         "reason: a rejected dangling edge must never land")


if __name__ == "__main__":
    unittest.main(verbosity=2)

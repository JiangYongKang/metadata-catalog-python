"""Tests for atomic batch lineage changes: all-or-nothing commit,
per-item rejection reasons and concurrency (no lost changes, no torn reads)."""
import threading
import unittest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import BatchLineageError
from metadata_catalog.models import BatchOp

from tests._helpers import log_case


class BatchLineageTest(unittest.TestCase):
    def setUp(self):
        self.catalog = MetadataCatalog()
        for aid in ("a", "b", "c", "d"):
            self.catalog.register_asset(
                aid, aid, "table", [("id", "bigint"), ("v", "string")])
        self.catalog.add_lineage("a", "b")

    def test_one_bad_item_rolls_back_the_whole_batch(self):
        before = {e.key() for e in self.catalog.list_lineage()}
        log_case("batch with one bad relation",
                 items=["add b->c (valid)", "add a->ghost (unknown asset)"],
                 graph_before=sorted(before))
        with self.assertRaises(BatchLineageError) as ctx:
            self.catalog.commit_lineage_batch([
                {"op": "add", "upstream": "b", "downstream": "c"},
                {"op": "add", "upstream": "a", "downstream": "ghost"},
            ])
        failures = ctx.exception.failures
        self.assertEqual(len(failures), 1)
        f = failures[0]
        log_case("rejected item", index=f.index, code=f.code,
                 message=f.message, edge=(f.upstream, f.downstream))
        self.assertEqual((f.index, f.code), (1, "asset_not_found"))
        after = {e.key() for e in self.catalog.list_lineage()}
        self.assertEqual(before, after,
                         "reason: whole batch must roll back, valid item included")

    def test_multiple_failures_all_reported_per_item(self):
        with self.assertRaises(BatchLineageError) as ctx:
            self.catalog.commit_lineage_batch([
                {"op": "add", "upstream": "a", "downstream": "x"},   # 0 asset
                {"op": "add", "upstream": "a", "downstream": "b",
                 "upstream_column": "id", "downstream_column": "nope"},  # 1 column
                {"op": "add", "upstream": "c", "downstream": "c",
                 "upstream_column": "id", "downstream_column": "id"},  # 2 col self-loop
                {"op": "add", "upstream": "b", "downstream": "a"},     # 3 cycle
                {"op": "add", "upstream": "c", "downstream": "d"},     # 4 valid
            ])
        failures = ctx.exception.failures
        log_case("per-item failures",
                 reported=[(f.index, f.code) for f in failures])
        self.assertEqual([f.index for f in failures], [0, 1, 2, 3],
                         "reason: every bad item listed, valid item absent")
        codes = {f.index: f.code for f in failures}
        self.assertEqual(codes[0], "asset_not_found")
        self.assertEqual(codes[1], "column_not_found")
        self.assertEqual(codes[2], "self_dependency",
                         "reason: column self-loop must carry the "
                         "distinguishable self_dependency code")
        self.assertEqual(codes[3], "lineage_cycle")
        self.assertEqual(len(self.catalog.list_lineage()), 1,
                         "reason: rejected batch leaves graph untouched")

    def test_cross_item_cycle_inside_one_batch_rejected(self):
        with self.assertRaises(BatchLineageError) as ctx:
            self.catalog.commit_lineage_batch([
                {"op": "add", "upstream": "d", "downstream": "a"},
                {"op": "add", "upstream": "a", "downstream": "d"},
            ])
        idxs = [f.index for f in ctx.exception.failures]
        log_case("intra-batch mutual edges", failures=idxs)
        self.assertIn(1, idxs,
                      "reason: a->d closes d->a added earlier in the same batch")

    def test_mixed_adds_and_removes_commit_together(self):
        self.catalog.add_lineage("b", "c")
        result = self.catalog.commit_lineage_batch([
            BatchOp("add", "c", "d"),
            BatchOp("remove", "a", "b"),
            {"op": "remove", "upstream": "c", "downstream": "d"},  # no-op? no -> still missing
        ])
        # third item removes an edge not present at validation time (c->d is
        # added earlier in the same batch) -> it *is* present, gets removed
        edges = {(e.upstream, e.downstream) for e in self.catalog.list_lineage()}
        log_case("mixed batch applied", applied=result["applied"],
                 edges=sorted(edges), batch_id=result["batch_id"])
        self.assertEqual(edges, {("b", "c")},
                         "reason: add a->b removed, c->d added then removed")
        # audited as one transaction
        self.assertTrue(all(ch.batch_id == result["batch_id"]
                            for ch in result["changes"]))

    def test_empty_batch_is_a_valid_noop(self):
        result = self.catalog.commit_lineage_batch([])
        self.assertEqual((result["submitted"], result["applied"]), (0, 0))

    def test_idempotent_items_apply_silently(self):
        result = self.catalog.commit_lineage_batch([
            {"op": "add", "upstream": "a", "downstream": "b"},  # exists
            {"op": "remove", "upstream": "c", "downstream": "d"},  # missing
        ])
        self.assertEqual(result["applied"], 0)
        self.assertEqual(len(self.catalog.list_lineage()), 1)


class ConcurrentBatchTest(unittest.TestCase):
    def setUp(self):
        self.catalog = MetadataCatalog()
        # hub 'h' and 16 groups; group i adds h->g_i then g_i->s_i (a chain)
        self.n_groups = 16
        for aid in ["h", "s"] + [f"g{i:02d}" for i in range(self.n_groups)]:
            self.catalog.register_asset(aid, aid, "table")

    def test_concurrent_batches_lose_no_change(self):
        n = self.n_groups
        barrier = threading.Barrier(n)

        def submit_group(i: int):
            g = f"g{i:02d}"
            barrier.wait()
            self.catalog.commit_lineage_batch([
                {"op": "add", "upstream": "h", "downstream": g},
                {"op": "add", "upstream": g, "downstream": "s"},
            ])

        threads = [threading.Thread(target=submit_group, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        edges = {(e.upstream, e.downstream) for e in self.catalog.list_lineage()}
        log_case("concurrent batches", groups=n, edge_count=len(edges))
        for i in range(n):
            g = f"g{i:02d}"
            self.assertIn(("h", g), edges,
                          f"reason: edge h->{g} lost under concurrency")
            self.assertIn((g, "s"), edges,
                          f"reason: edge {g}->s lost under concurrency")
        self.assertEqual(len(edges), 2 * n,
                         "reason: every batch must commit both edges exactly once")

    def test_reader_never_sees_a_half_applied_batch(self):
        """While 16 writers each commit a 2-edge batch, a reader thread
        continuously snapshots the graph: it must always observe either
        both edges of a batch or neither, and no committed change may be
        lost. Exceptions inside worker threads are collected and fail the
        test -- a thread that dies silently can no longer fake a pass."""
        n = self.n_groups
        barrier = threading.Barrier(n + 1)
        stop = threading.Event()
        torn: list[tuple] = []
        thread_errors: list[str] = []
        checked = {"reads": 0}

        def submit_group(i: int):
            g = f"g{i:02d}"
            try:
                barrier.wait()
                self.catalog.commit_lineage_batch([
                    {"op": "add", "upstream": "h", "downstream": g},
                    {"op": "add", "upstream": g, "downstream": "s"},
                ])
            except Exception as exc:  # noqa: BLE001 - any failure here is a real bug
                thread_errors.append(f"writer {g}: {exc!r}")

        def reader():
            try:
                barrier.wait()
                while not stop.is_set():
                    edges = self.catalog.list_lineage()
                    pairs = {(e.upstream, e.downstream) for e in edges}
                    checked["reads"] += 1
                    for i in range(n):
                        g = f"g{i:02d}"
                        only_one = (("h", g) in pairs) != ((g, "s") in pairs)
                        if only_one:
                            torn.append((g, sorted(pairs)))
            except Exception as exc:  # noqa: BLE001 - a dying reader must not go unnoticed
                thread_errors.append(f"reader: {exc!r}")

        writers = [threading.Thread(target=submit_group, args=(i,))
                   for i in range(n)]
        rthread = threading.Thread(target=reader)
        for t in writers:
            t.start()
        rthread.start()
        for t in writers:
            t.join()
        stop.set()
        rthread.join()

        final_pairs = {(e.upstream, e.downstream)
                       for e in self.catalog.list_lineage()}
        log_case("read/write race", writers=n, reads=checked["reads"],
                 torn_observations=len(torn),
                 thread_errors=thread_errors or "none",
                 final_edge_count=len(final_pairs))
        self.assertEqual(thread_errors, [],
                         "reason: no worker thread may die with an exception")
        self.assertGreater(checked["reads"], 0,
                           "reason: the reader must actually have run")
        self.assertEqual(torn, [],
                         "reason: a reader saw exactly one edge of a 2-edge batch")
        for i in range(n):
            g = f"g{i:02d}"
            self.assertIn(("h", g), final_pairs,
                          f"reason: edge h->{g} lost under concurrency")
            self.assertIn((g, "s"), final_pairs,
                          f"reason: edge {g}->s lost under concurrency")
        self.assertEqual(len(final_pairs), 2 * n,
                         "reason: every concurrent batch must commit both "
                         "edges exactly once")


if __name__ == "__main__":
    unittest.main(verbosity=2)

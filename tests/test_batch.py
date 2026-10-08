"""Tests for atomic batch lineage changes: all-or-nothing commit,
per-item rejection reasons and concurrency (no lost changes, no torn reads)."""
import threading
import unittest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import (
    BatchLineageError,
    LineageCycleError,
    SelfDependencyError,
)
from metadata_catalog.models import BatchOp

from tests._helpers import log_case


# Every batch in the concurrency tests has this fixed shape: two edges that
# must appear or disappear as one unit (h -> g_i, g_i -> s).
def _group_batch(group: str) -> list[dict]:
    return [
        {"op": "add", "upstream": "h", "downstream": group},
        {"op": "add", "upstream": group, "downstream": "s"},
    ]


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
        self.assertEqual(codes[2], "lineage_cycle")
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

    def _final_edge_pairs(self) -> set[tuple[str, str]]:
        return {(e.upstream, e.downstream) for e in self.catalog.list_lineage()}

    def test_concurrent_batches_lose_no_change(self):
        n = self.n_groups
        barrier = threading.Barrier(n)
        writer_errors: list[tuple[int, str]] = []

        def submit_group(i: int):
            g = f"g{i:02d}"
            barrier.wait()
            try:
                # the two edges are one atomic batch
                self.catalog.commit_lineage_batch(_group_batch(g))
            except Exception as exc:  # captured, never swallowed silently
                writer_errors.append((i, repr(exc)))

        threads = [threading.Thread(target=submit_group, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        edges = self._final_edge_pairs()
        log_case("concurrent batches", groups=n, edge_count=len(edges),
                 writer_errors=writer_errors)
        self.assertEqual(writer_errors, [],
                         "reason: no writer thread may die with an exception")
        for i in range(n):
            g = f"g{i:02d}"
            self.assertIn(("h", g), edges,
                          f"reason: edge h->{g} lost under concurrency")
            self.assertIn((g, "s"), edges,
                          f"reason: edge {g}->s lost under concurrency")
        self.assertEqual(len(edges), 2 * n,
                         "reason: every batch must commit both edges exactly once")

    def test_reader_never_sees_a_half_applied_batch(self):
        """A reader racing committing writers must only ever observe the graph
        before a whole batch or after it: for every group either both edges of
        its 2-edge batch are present or neither. The reader reports any
        exception into ``reader_errors`` (a crashing reader fails the test
        instead of silently passing), and it must perform a real, positive
        number of reads while batches are being committed."""
        n = self.n_groups
        barrier = threading.Barrier(n + 1)
        stop = threading.Event()
        first_commit = threading.Event()
        writers_done = threading.Event()
        torn: list[tuple] = []
        reader_errors: list[str] = []
        checked = {"reads": 0, "mid_flight_reads": 0}

        def submit_group(i: int):
            g = f"g{i:02d}"
            barrier.wait()
            # all batches here are valid and idempotent, so a commit must not
            # raise; letting an exception escape would kill the thread, which
            # is exactly what the error capture below protects against
            self.catalog.commit_lineage_batch(_group_batch(g))
            first_commit.set()

        def reader():
            try:
                while not stop.is_set():
                    # one locked snapshot; list_lineage() returns LineageEdge
                    # objects -- use attributes, never unpack the objects
                    edges = self.catalog.list_lineage()
                    pairs = {(e.upstream, e.downstream) for e in edges}
                    checked["reads"] += 1
                    if first_commit.is_set() and not writers_done.is_set():
                        checked["mid_flight_reads"] += 1
                    for i in range(n):
                        g = f"g{i:02d}"
                        only_one = (("h", g) in pairs) != ((g, "s") in pairs)
                        if only_one:
                            torn.append((g, sorted(pairs)))
            except Exception as exc:
                # surfaced on the main thread; the race assertion must not be
                # allowed to "pass" while the reader died on its first read
                reader_errors.append(repr(exc))

        writers = [threading.Thread(target=submit_group, args=(i,))
                   for i in range(n)]
        rthread = threading.Thread(target=reader)
        rthread.start()
        for t in writers:
            t.start()
        barrier.wait()  # release every writer simultaneously
        for t in writers:
            t.join()
        writers_done.set()
        stop.set()
        rthread.join(timeout=5)
        self.assertFalse(rthread.is_alive(),
                         "reason: reader thread failed to stop")

        edges = self._final_edge_pairs()
        log_case("read/write race",
                 reads=checked["reads"],
                 reads_while_batches_in_flight=checked["mid_flight_reads"],
                 torn_observations=len(torn),
                 reader_errors=reader_errors,
                 final_edge_count=len(edges),
                 expected_edges=2 * n)
        self.assertEqual(reader_errors, [],
                         "reason: reader thread raised and died; the torn-read "
                                 "assertion would otherwise never be exercised")
        self.assertGreater(checked["reads"], 0,
                           "reason: reader must actually read the graph")
        self.assertEqual(torn, [],
                         "reason: a reader saw exactly one edge of a 2-edge "
                                 "batch (a torn, half-applied graph)")
        # raced commits must still have lost no change
        self.assertEqual(len(edges), 2 * n,
                         "reason: raced batches lost committed edges")
        for i in range(n):
            g = f"g{i:02d}"
            self.assertIn(("h", g), edges, f"reason: h->{g} lost")
            self.assertIn((g, "s"), edges, f"reason: {g}->s lost")

    def test_repeated_concurrent_batches_stay_atomic_and_consistent(self):
        """Stress variant: writers keep re-submitting the same valid batches
        (idempotent) while a reader scans every snapshot. Each observed graph
        keeps the all-or-nothing pair invariant and the final graph is exactly
        the expected 2*n edges -- never more, never fewer."""
        n = self.n_groups
        rounds = 25
        barrier = threading.Barrier(n + 1)
        stop = threading.Event()
        first_commit = threading.Event()   # set once any batch has committed
        writers_done = threading.Event()   # set after every writer has joined
        torn: list[tuple] = []
        reader_errors: list[str] = []
        checked = {"reads": 0, "mid_flight_reads": 0}

        def submit_group(i: int):
            g = f"g{i:02d}"
            barrier.wait()
            for r in range(rounds):
                if stop.is_set():
                    break
                self.catalog.commit_lineage_batch(_group_batch(g))
                if r == 0:
                    first_commit.set()

        def reader():
            try:
                while not stop.is_set():
                    pairs = {(e.upstream, e.downstream)
                             for e in self.catalog.list_lineage()}
                    checked["reads"] += 1
                    # a batch has committed and at least one writer is still
                    # committing -> this snapshot genuinely overlapped
                    # in-flight batches (keeps the no-torn-read assertion
                    # from being vacuous)
                    if first_commit.is_set() and not writers_done.is_set():
                        checked["mid_flight_reads"] += 1
                    for i in range(n):
                        g = f"g{i:02d}"
                        if (("h", g) in pairs) != ((g, "s") in pairs):
                            torn.append((g, sorted(pairs)))
            except Exception as exc:
                reader_errors.append(repr(exc))

        writers = [threading.Thread(target=submit_group, args=(i,))
                   for i in range(n)]
        rthread = threading.Thread(target=reader)
        rthread.start()
        for t in writers:
            t.start()
        barrier.wait()
        for t in writers:
            t.join()
        writers_done.set()
        stop.set()
        rthread.join(timeout=5)
        self.assertFalse(rthread.is_alive())

        edges = self._final_edge_pairs()
        log_case("repeated batch stress", rounds_per_writer=rounds,
                 reads=checked["reads"],
                 reads_overlapping_in_flight_commits=checked["mid_flight_reads"],
                 torn_observations=len(torn),
                 reader_errors=reader_errors, final_edge_count=len(edges))
        self.assertEqual(reader_errors, [])
        self.assertGreater(checked["reads"], 0)
        self.assertGreater(checked["mid_flight_reads"], 0,
                           "reason: the reader must demonstrably overlap commits, "
                                   "otherwise the no-torn-read assertion is vacuous")
        self.assertEqual(torn, [])
        self.assertEqual(len(edges), 2 * n)


class BatchSelfDependencyParityTest(unittest.TestCase):
    """Batch and single registration must reach the same verdict for the same
    relation, and self-dependency failures must carry the same distinguishable
    kinds as single registration."""

    def setUp(self):
        self.catalog = MetadataCatalog()
        self.catalog.register_asset(
            "a", "a", "table",
            [("id", "bigint"), ("v", "string"), ("x", "int")])
        self.catalog.register_asset("b", "b", "table", [("id", "bigint")])

    SHAPES = [
        ("a->a",
         {"upstream": "a", "downstream": "a"},
         SelfDependencyError.KIND_ASSET_SELF),
        ("a.id->a.id",
         {"upstream": "a", "downstream": "a",
          "upstream_column": "id", "downstream_column": "id"},
         SelfDependencyError.KIND_COLUMN_SELF),
        ("a->a.id (table to own column)",
         {"upstream": "a", "downstream": "a", "downstream_column": "id"},
         SelfDependencyError.KIND_ASSET_OWN_COLUMN),
        ("a.id->a (own column to table)",
         {"upstream": "a", "downstream": "a", "upstream_column": "id"},
         SelfDependencyError.KIND_ASSET_OWN_COLUMN),
    ]

    def test_self_dependency_shapes_rejected_per_kind(self):
        for label, item, kind in self.SHAPES:
            cat = MetadataCatalog()
            cat.register_asset("a", "a", "table",
                               [("id", "bigint"), ("v", "string")])
            with self.assertRaises(BatchLineageError) as ctx:
                cat.commit_lineage_batch([item])
            failures = ctx.exception.failures
            self.assertEqual(len(failures), 1)
            f = failures[0]
            log_case("batch self-dependency", relation=label,
                     index=f.index, code=f.code, kind=f.kind)
            self.assertEqual(f.code, "lineage_cycle")
            self.assertEqual(f.kind, kind,
                             f"reason: {label} must report kind {kind}")
            self.assertEqual(len(cat.list_lineage()), 0,
                             "reason: rejected batch must not write anything")

    def test_single_and_batch_agree_on_every_relation(self):
        """For every relation in the matrix, a fresh single registration and a
        fresh one-item batch produce the same verdict (accept or the same
        code+kind). Same-table different-column relations are legal via both
        entries -- this is the bug where batch falsely called them cycles."""
        legal = [
            ("a.id->a.v (same table, different columns)",
             {"upstream": "a", "downstream": "a",
              "upstream_column": "id", "downstream_column": "v"}),
            ("a.v->a.x (same table, different columns)",
             {"upstream": "a", "downstream": "a",
              "upstream_column": "v", "downstream_column": "x"}),
            ("a.id->b.id (cross asset, column level)",
             {"upstream": "a", "downstream": "b",
              "upstream_column": "id", "downstream_column": "id"}),
            ("a->b (cross asset, asset level)",
             {"upstream": "a", "downstream": "b"}),
        ]

        def fresh():
            cat = MetadataCatalog()
            cat.register_asset("a", "a", "table",
                               [("id", "bigint"), ("v", "string"),
                                ("x", "int")])
            cat.register_asset("b", "b", "table", [("id", "bigint")])
            return cat

        def verdict_single(item):
            cat = fresh()
            try:
                cat.add_lineage(item["upstream"], item["downstream"],
                                item.get("upstream_column"),
                                item.get("downstream_column"))
                return ("accepted", len(cat.list_lineage()))
            except LineageCycleError as exc:
                return ("rejected", getattr(exc, "kind", None),
                        exc.code)

        def verdict_batch(item):
            cat = fresh()
            try:
                result = cat.commit_lineage_batch([{"op": "add", **item}])
                return ("accepted", result["applied"])
            except BatchLineageError as exc:
                f = exc.failures[0]
                return ("rejected", f.kind, f.code)

        matrix = [(label, item, kind) for label, item, kind in self.SHAPES]
        for label, item, _kind in matrix:
            s, b = verdict_single(item), verdict_batch(item)
            log_case("single-vs-batch verdict", relation=label,
                     single=s, batch=b)
            self.assertEqual(s[0], b[0],
                             f"reason: verdict mismatch for {label}: "
                             f"single={s}, batch={b}")
            self.assertEqual(s[1:], b[1:],
                             f"reason: code/kind mismatch for {label}")

        for label, item in legal:
            s, b = verdict_single(item), verdict_batch(item)
            log_case("single-vs-batch verdict", relation=label,
                     single=s, batch=b)
            self.assertEqual(s, ("accepted", 1),
                             f"reason: {label} must be accepted singly: {s}")
            self.assertEqual(b, ("accepted", 1),
                             f"reason: {label} must be accepted in batch: {b}")

    def test_same_table_column_edge_in_larger_batch_does_not_block_others(self):
        """A legal same-table column relation inside a multi-item batch must
        not make the batch or its sibling items look cyclic."""
        result = self.catalog.commit_lineage_batch([
            {"op": "add", "upstream": "a", "downstream": "b"},
            {"op": "add", "upstream": "a", "downstream": "a",
             "upstream_column": "id", "downstream_column": "v"},
            {"op": "add", "upstream": "a", "downstream": "a",
             "upstream_column": "v", "downstream_column": "x"},
        ])
        edges = sorted((e.upstream, e.downstream,
                        e.upstream_column, e.downstream_column)
                       for e in self.catalog.list_lineage())
        log_case("legal intra-asset edges in batch",
                 applied=result["applied"], edges=edges)
        self.assertEqual(result["applied"], 3)
        self.assertEqual(len(edges), 3)

    def test_batch_with_second_legal_edge_after_first_is_committed(self):
        """The contamination bug: once a legal same-table relation is stored,
        the same kind of relation submitted later (singly or batched) must not
        be reported as a cycle."""
        self.catalog.add_lineage("a", "a", "id", "v")
        # same relation repeated -> idempotent, not cyclic
        result = self.catalog.commit_lineage_batch([
            {"op": "add", "upstream": "a", "downstream": "a",
             "upstream_column": "id", "downstream_column": "v"}])
        self.assertEqual(result["applied"], 0)
        # a second, different legal relation -> applied
        result = self.catalog.commit_lineage_batch([
            {"op": "add", "upstream": "a", "downstream": "a",
             "upstream_column": "id", "downstream_column": "x"}])
        self.assertEqual(result["applied"], 1)
        log_case("no contamination after stored intra-asset edge",
                 edge_count=len(self.catalog.list_lineage()))


if __name__ == "__main__":
    unittest.main(verbosity=2)

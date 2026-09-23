"""Tests for version evolution and concurrent update conflicts."""
import threading
import unittest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import ConcurrentUpdateError

from tests._helpers import log_case


class VersionEvolutionTest(unittest.TestCase):
    def test_idempotent_registration_creates_one_version(self):
        cat = MetadataCatalog()
        cols = [("id", "bigint")]
        cat.register_asset("a", "A", "table", cols, description="d")
        cat.register_asset("a", "A", "table", cols, description="d")
        a = cat.register_asset("a", "A", "table", cols, description="d")
        self.assertEqual(a.current_version, 1,
                         "reason: identical re-registration must not create versions")
        self.assertEqual(len(cat.list_versions("a")), 1)

    def test_merge_keeps_existing_columns_and_associations(self):
        """Adding a column must never drop existing columns or lineage."""
        cat = MetadataCatalog()
        cat.register_asset("b", "B", "table", [("id", "bigint")])
        cat.register_asset("a", "A", "table", [("id", "bigint"), ("name", "string")])
        cat.add_lineage("a", "b", "name", "id")
        cat.register_asset("a", "A", "table", [("email", "string")])
        a = cat.get_asset("a")
        log_case("evolution merge", columns=[c.name for c in a.columns],
                 lineage_count=len(cat.list_lineage()))
        self.assertEqual([c.name for c in a.columns], ["id", "name", "email"],
                         "reason: merge is union-by-name; old columns must survive")
        self.assertEqual(len(cat.list_lineage()), 1,
                         "reason: existing lineage must survive re-registration")
        # lineage pointing at a still-valid column remains usable
        edge = cat.list_lineage()[0]
        self.assertEqual(edge.upstream_column, "name")

    def test_history_is_traceable(self):
        cat = MetadataCatalog()
        cat.register_asset("a", "A", "table", [("id", "bigint")], change_note="v1")
        cat.register_asset("a", "A", "table", [("name", "string")], change_note="v2")
        v1 = cat.get_version("a", 1)
        v2 = cat.get_version("a", 2)
        log_case("history", v1_columns=[c.name for c in v1.columns],
                 v2_columns=[c.name for c in v2.columns],
                 notes=[v.change_note for v in cat.list_versions("a")])
        self.assertEqual([c.name for c in v1.columns], ["id"])
        self.assertEqual([c.name for c in v2.columns], ["id", "name"],
                         "reason: version 2 snapshot must include merged columns")
        self.assertEqual(v1.change_note, "v1")

    def test_conflict_exposed_not_silently_overwritten(self):
        cat = MetadataCatalog()
        cat.register_asset("a", "A", "table")
        cat.register_asset("a", "A", "table", [("x", "int")])  # now v2
        log_case("stale CAS", expected_version=1, actual_version=2)
        with self.assertRaises(ConcurrentUpdateError) as ctx:
            cat.register_asset("a", "A", "table", [("y", "int")],
                               expected_version=1, change_note="stale")
        err = ctx.exception
        self.assertEqual(err.code, "concurrent_update",
                         f"reason: conflict needs its own code, got {err.code}")
        self.assertEqual((err.expected_version, err.actual_version), (1, 2),
                         "reason: expected/actual versions must be exposed for diagnosis")
        self.assertEqual([c.name for c in cat.get_asset("a").columns], ["x"],
                         "reason: rejected write must not change current state")

    def test_create_guard_distinguishes_missing_asset(self):
        cat = MetadataCatalog()
        with self.assertRaises(ConcurrentUpdateError) as ctx:
            cat.register_asset("a", "A", "table", expected_version=3)
        self.assertEqual((ctx.exception.expected_version,
                          ctx.exception.actual_version), (3, 0),
                         "reason: creation against missing asset reports actual=0")


class ConcurrentWritersTest(unittest.TestCase):
    def test_parallel_cas_writes_no_lost_update(self):
        """Phase 1: N writers all start from the same stale base version, so
        conflicts are guaranteed (only one first-wave CAS can win); losers
        retry with a fresh version and every column must land.
        Phase 2: a non-retrying writer using the old version must receive a
        ConcurrentUpdateError carrying distinguishable versions."""
        cat = MetadataCatalog()
        cat.register_asset("t", "T", "table", [("base", "int")])
        n = 12
        baseline = 1  # captured by every writer before the race
        successes: list[str] = []
        conflict_events: list[tuple] = []
        barrier = threading.Barrier(n)

        def writer(i: int):
            col = f"col{i:02d}"
            barrier.wait()
            v = baseline
            while True:
                try:
                    cat.register_asset("t", "T", "table", [(col, "int")],
                                       expected_version=v)
                    successes.append(col)
                    return
                except ConcurrentUpdateError as exc:
                    conflict_events.append((i, exc.expected_version,
                                            exc.actual_version))
                    v = cat.get_asset("t").current_version  # refresh and retry

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = cat.get_asset("t")
        names = {c.name for c in final.columns}
        log_case("phase 1 parallel CAS", writers=n,
                 first_wave_conflicts=len(conflict_events),
                 successful=sorted(successes), final_version=final.current_version)
        self.assertEqual(len(successes), n,
                         "reason: every retrying writer must commit exactly once")
        self.assertGreaterEqual(len(conflict_events), n - 1,
                                "reason: at most one first-wave writer may win")
        for i in range(n):
            self.assertIn(f"col{i:02d}", names,
                          f"reason: column col{i:02d} was lost under concurrency")
        self.assertEqual(final.current_version, 1 + n,
                         "reason: exactly one version increment per success")

        # phase 2: stale, non-retrying write must be rejected, not overwrite
        log_case("phase 2 stale write", expected_version=baseline,
                 actual_version=final.current_version)
        with self.assertRaises(ConcurrentUpdateError) as ctx:
            cat.register_asset("t", "T", "table", [("late", "int")],
                               expected_version=baseline)
        err = ctx.exception
        self.assertEqual(err.expected_version, baseline)
        self.assertEqual(err.actual_version, 1 + n)
        self.assertNotIn("late", {c.name for c in cat.get_asset("t").columns},
                         "reason: rejected write must leave no partial effect")


if __name__ == "__main__":
    unittest.main(verbosity=2)

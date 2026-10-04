"""Tests for the append-only lineage change audit trail."""
import unittest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import AssetNotFoundError, InvalidTimePointError

from tests._helpers import log_case


class FakeClock:
    """Deterministic, strictly increasing timeline."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


class LineageAuditTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.catalog = MetadataCatalog(clock=self.clock)
        for aid in ("a", "b", "c"):
            self.catalog.register_asset(
                aid, aid, "table", [("id", "bigint"), ("v", "string")])

    def test_register_and_cancel_leave_before_after_records(self):
        t_add_ab = self.clock.t
        self.catalog.add_lineage("a", "b")
        self.catalog.add_lineage("a", "b", "id", "id")  # column edge
        self.catalog.remove_lineage("a", "b")
        result = self.catalog.lineage_audit("a")
        log_case("audit trail for a", status=result.status,
                 records=[(r.seq, r.op, r.before_present, r.after_present)
                          for r in result.records])
        self.assertEqual(result.status, "found_changes")
        ops = [(r.op, r.before_present, r.after_present) for r in result.records]
        self.assertEqual(ops, [
            ("add", False, True),
            ("add", False, True),
            ("remove", True, False),
        ], "reason: every real mutation records before/after presence")
        self.assertTrue(all(r.at > t_add_ab for r in result.records))
        self.assertEqual([r.seq for r in result.records], [1, 2, 3],
                         "reason: seq is a gapless global order")

    def test_idempotent_ops_write_no_audit(self):
        self.catalog.add_lineage("a", "b")
        self.catalog.add_lineage("a", "b")          # duplicate add
        self.catalog.remove_lineage("a", "c")       # removing missing edge
        n = len(self.catalog.lineage_audit("a").records)
        log_case("idempotent operations", records=n)
        self.assertEqual(n, 1,
                         "reason: duplicate add / missing remove add no records")

    def test_unknown_asset_distinct_from_asset_without_changes(self):
        # existing asset that has never touched lineage
        quiet = self.catalog.lineage_audit("c")
        log_case("existing but quiet asset c", status=quiet.status,
                 records=quiet.records)
        self.assertEqual(quiet.status, "no_changes")
        self.assertEqual(quiet.records, [])
        # asset that does not exist at all -> hard error, never a silent []
        with self.assertRaises(AssetNotFoundError) as ctx:
            self.catalog.lineage_audit("ghost")
        log_case("unknown asset queried", code=ctx.exception.code,
                 asset=ctx.exception.asset)
        self.assertEqual(ctx.exception.code, "asset_not_found")

    def test_time_range_filter_is_inclusive(self):
        self.catalog.add_lineage("a", "b")   # t=1002 (assets ate 1001..3)
        t1 = self.catalog.list_lineage()[0].registered_at
        self.catalog.add_lineage("a", "c")
        t2 = self.catalog._audit[-1].at
        self.catalog.remove_lineage("a", "b")
        t3 = self.catalog._audit[-1].at
        window = self.catalog.lineage_audit("a", start=t2, end=t3)
        log_case("window [t2, t3]", window=(t2, t3),
                 seqs=[r.seq for r in window.records])
        self.assertEqual([r.seq for r in window.records], [2, 3],
                         "reason: range boundaries are inclusive")
        only_first = self.catalog.lineage_audit("a", start=t1, end=t1)
        self.assertEqual([r.seq for r in only_first.records], [1])

    def test_invalid_time_range_rejected(self):
        self.catalog.add_lineage("a", "b")
        for kwargs in ({"start": "yesterday"}, {"end": float("nan")},
                       {"start": 2000.0, "end": 1000.0}):
            log_case("invalid audit window", **kwargs)
            with self.assertRaises(InvalidTimePointError):
                self.catalog.lineage_audit("a", **kwargs)

    def test_trail_reconstructs_relation_lifecycle_ends_agrees(self):
        """Records seen from either endpoint tell the same story."""
        self.catalog.add_lineage("a", "b")
        self.catalog.add_lineage("a", "b", "v", "v")
        self.catalog.remove_lineage("a", "b", "v", "v")
        ra = self.catalog.lineage_audit("a")
        rb = self.catalog.lineage_audit("b")
        self.assertEqual([r.seq for r in ra.records],
                         [r.seq for r in rb.records])
        roles = {(r.seq, r.role_of("a"), r.role_of("b"))
                 for r in ra.records}
        log_case("roles from both ends", roles=sorted(roles))
        self.assertEqual(roles, {(1, "upstream", "downstream"),
                                 (2, "upstream", "downstream"),
                                 (3, "upstream", "downstream")})


if __name__ == "__main__":
    unittest.main(verbosity=2)

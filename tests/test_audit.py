"""Audit trail tests: automatic before/after records, per-asset history,
time-range filtering, and unknown-asset vs. no-changes distinguishability."""
import unittest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import AssetNotFoundError, LineageTimeError

from tests._helpers import log_case


class FakeClock:
    """Deterministic, externally advanced clock for reproducible timestamps."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def tick(self, dt: float = 1.0) -> float:
        self.t += dt
        return self.t


class LineageAuditTest(unittest.TestCase):
    def setUp(self):
        self.clk = FakeClock()
        self.cat = MetadataCatalog(clock=self.clk)
        for aid in ("a", "b", "c", "d"):
            self.cat.register_asset(aid, aid, "table",
                                    [("id", "bigint"), ("v", "string")])
            self.clk.tick()

    def test_every_register_and_remove_is_audited_with_states(self):
        t1 = self.clk()
        self.cat.add_lineage("a", "b")
        self.clk.tick()
        # idempotent re-register: still audited, but flagged changed=False
        self.cat.add_lineage("a", "b")
        self.clk.tick()
        self.cat.remove_lineage("a", "b")
        self.clk.tick()
        # removing a missing edge is audited as a no-op as well
        self.cat.remove_lineage("a", "b")

        changes = self.cat.lineage_changes("a")
        log_case("audit states for a", timeline=[
            (c.action, c.before, c.after, c.changed, c.timestamp)
            for c in changes], key_inputs=("a->b add", "a->b add again",
                                            "a->b remove", "a->b remove again"))
        self.assertEqual([c.action for c in changes],
                         ["add", "add", "remove", "remove"])
        self.assertEqual([c.changed for c in changes],
                         [True, False, True, False])
        self.assertEqual((changes[0].before, changes[0].after),
                         ("absent", "present"))
        self.assertEqual((changes[2].before, changes[2].after),
                         ("present", "absent"))
        self.assertEqual([c.timestamp for c in changes],
                         sorted(c.timestamp for c in changes),
                         "reason: audit entries must be chronological")
        self.assertEqual(changes[0].timestamp, t1)
        self.assertEqual([c.seq for c in changes],
                         list(range(changes[0].seq, changes[-1].seq + 1)),
                         "reason: sequence numbers must be gap-free")

    def test_full_asset_history_shows_when_each_edge_came_and_went(self):
        self.cat.add_lineage("a", "b"); t_add_ab = self.clk.tick()
        self.cat.add_lineage("b", "c"); t_add_bc = self.clk.tick()
        self.cat.add_lineage("a", "c"); t_add_ac = self.clk.tick()
        self.cat.remove_lineage("b", "c"); t_del_bc = self.clk.tick()

        history = self.cat.lineage_changes("c")
        log_case("full history around c",
                 entries=[(c.action, f"{c.upstream}->{c.downstream}")
                          for c in history],
                 add_bc=t_add_bc, add_ac=t_add_ac, del_bc=t_del_bc)
        self.assertEqual([(c.action, c.upstream, c.downstream) for c in history],
                         [("add", "b", "c"), ("add", "a", "c"),
                          ("remove", "b", "c")],
                         "reason: every add/remove touching c must be listed "
                         "in order so each edge's origin and removal is visible")

    def test_time_range_filter_inclusive(self):
        self.cat.add_lineage("a", "b")
        t1 = self.clk()
        self.clk.tick()
        self.cat.add_lineage("b", "c")
        t2 = self.clk()
        self.clk.tick()
        self.cat.add_lineage("c", "d")
        t3 = self.clk()

        rng = self.cat.all_lineage_changes(start=t1, end=t2)
        log_case("range [t1,t2]", range=(t1, t2),
                 picked=[(c.timestamp, f"{c.upstream}->{c.downstream}")
                         for c in rng])
        self.assertEqual([c.timestamp for c in rng], [t1, t2])
        self.assertEqual(self.cat.all_lineage_changes(start=t3 + 0.5), [])
        self.assertEqual(self.cat.all_lineage_changes(end=t1 - 0.5), [])
        # per-asset range filter
        touching_a = self.cat.lineage_changes("a", start=t1)
        self.assertEqual([(c.upstream, c.downstream) for c in touching_a],
                         [("a", "b")])
        touching_c = self.cat.all_lineage_changes(start=t2, end=t3)
        self.assertEqual([(c.upstream, c.downstream) for c in touching_c],
                         [("b", "c"), ("c", "d")])

    def test_unknown_asset_vs_known_asset_without_changes_are_distinct(self):
        log_case("unknown asset queried", asset="ghost",
                 existing=sorted(a.id for a in self.cat.list_assets()))
        with self.assertRaises(AssetNotFoundError) as ctx:
            self.cat.lineage_changes("ghost")
        self.assertEqual(ctx.exception.code, "asset_not_found")
        self.assertEqual(ctx.exception.asset, "ghost")

        self.cat.register_asset("quiet", "quiet", "table")
        log_case("existing asset with zero lineage changes", asset="quiet")
        self.assertEqual(self.cat.lineage_changes("quiet"), [],
                         "reason: known asset with no changes returns an empty "
                         "list, never the same signal as an unknown asset")

    def test_invalid_range_bounds_rejected(self):
        with self.assertRaises(LineageTimeError) as ctx:
            self.cat.all_lineage_changes(start=2000, end=1000)
        self.assertEqual(ctx.exception.reason, "invalid_range")
        log_case("inverted range", start=2000, end=1000,
                 code=ctx.exception.code, reason=ctx.exception.reason)

    def test_batch_changes_carry_batch_id(self):
        self.clk.tick()
        result = self.cat.commit_lineage_batch([
            {"action": "add", "upstream": "a", "downstream": "c"},
            {"action": "add", "upstream": "c", "downstream": "d"},
        ])
        for c in self.cat.lineage_changes("c"):
            self.assertEqual(c.batch_id, result.batch_id)
        log_case("batch-tagged audit entries", batch_id=result.batch_id,
                 entries=[(c.action, f"{c.upstream}->{c.downstream}")
                          for c in self.cat.lineage_changes("c")])


if __name__ == "__main__":
    unittest.main(verbosity=2)

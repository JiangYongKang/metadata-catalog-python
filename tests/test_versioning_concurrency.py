"""版本演进与并发更新：历史可追溯、冲突可区分、并发下无丢失更新。

运行：uv run python -m unittest tests.test_versioning_concurrency -v
"""

from __future__ import annotations

import threading
import unittest

from metadata_catalog import (
    Asset,
    Column,
    ConcurrentUpdateError,
    ConflictResult,
    LineageEdge,
    MetadataCatalog,
    RegisterResult,
)


def log(title: str, **fields) -> None:
    body = ", ".join(f"{k}={v!r}" for k, v in fields.items())
    print(f"[case] {title}: {body}")


class TestVersioning(unittest.TestCase):
    def setUp(self) -> None:
        self.cat = MetadataCatalog()
        self.cat.register_asset(
            Asset("orders", columns=(Column("id"),)), reason="init"
        )
        self.cat.register_asset(Asset("users", columns=(Column("id"),)))
        self.cat.register_lineage(LineageEdge("users", "orders"))

    def test_history_records_every_structural_change(self) -> None:
        self.cat.register_asset(
            Asset("orders", columns=(Column("id"), Column("amount"))),
            reason="add amount",
        )
        self.cat.register_asset(
            Asset("orders",
                  columns=(Column("id"), Column("amount"), Column("ts"))),
            reason="add ts",
        )
        history = self.cat.history("orders")
        versions = [(rv.version, tuple(c.name for c in rv.asset.columns), rv.reason)
                    for rv in history]
        log("history", revisions=versions)
        self.assertEqual(versions[0], (1, ("id",), "init"))
        self.assertEqual(versions[1], (2, ("id", "amount"), "add amount"))
        self.assertEqual(versions[2], (3, ("id", "amount", "ts"), "add ts"))

    def test_lineage_survives_asset_re_registration(self) -> None:
        # 资产结构演进后，已有血缘不得丢失
        self.cat.register_asset(
            Asset("orders", columns=(Column("id"), Column("amount")))
        )
        edge = self.cat.lineage_between("users", "orders")
        self.assertEqual((edge.upstream_id, edge.downstream_id), ("users", "orders"))
        impact = self.cat.impact("users", "downstream")
        self.assertIn("orders", {n["id"] for n in impact["nodes"]})
        log("lineage-preserved", downstream=[n["id"] for n in impact["nodes"]])

    def test_idempotent_re_register_does_not_bump_version(self) -> None:
        v_before = self.cat.get_version("orders")
        result = self.cat.register_asset(
            Asset("orders", columns=(Column("id"),))
        )
        log("idempotent-asset", before=v_before, after=result.version,
            detail=result.detail)
        self.assertEqual(result.version, v_before)
        self.assertTrue(result.merged)
        self.assertEqual(len(self.cat.history("orders")), 1)

    def test_stale_expected_version_raises_distinct_conflict(self) -> None:
        # 另一处先把 orders 推进到 v2
        self.cat.register_asset(
            Asset("orders", columns=(Column("id"), Column("amount")))
        )
        with self.assertRaises(ConcurrentUpdateError) as ctx:
            self.cat.register_asset(
                Asset("orders", columns=(Column("id"), Column("stale"))),
                expected_version=1,
            )
        log("conflict-raise", expected=1, actual=2)
        message = str(ctx.exception)
        self.assertIn("expected version 1", message)
        self.assertIn("current version is 2", message)
        # 被拒的变更不得落库（无静默覆盖）
        names = {c.name for c in self.cat.get_asset("orders").columns}
        self.assertNotIn("stale", names)
        self.assertIn("amount", names)

    def test_try_update_returns_structured_conflict(self) -> None:
        ok, conflict = self.cat.try_update_asset(
            Asset("orders", columns=(Column("id"), Column("x"))),
            expected_version=99,
        )
        self.assertFalse(ok)
        self.assertIsInstance(conflict, ConflictResult)
        self.assertEqual(conflict.reason, "version_mismatch")
        self.assertEqual(conflict.expected_version, 99)
        self.assertEqual(conflict.actual_version, 1)
        log("conflict-result", reason=conflict.reason,
            expected=conflict.expected_version, actual=conflict.actual_version)

        ok2, result = self.cat.try_update_asset(
            Asset("orders", columns=(Column("id"), Column("x"))),
            expected_version=1, reason="cas add x",
        )
        self.assertTrue(ok2)
        self.assertIsInstance(result, RegisterResult)
        self.assertEqual(result.version, 2)


class TestConcurrentUpdates(unittest.TestCase):
    def test_parallel_competing_updates_only_one_wins(self) -> None:
        """8 个线程基于同一初始版本竞争追加各自的列。

        语义：只有一个线程能成功提交（其余必须拿到可区分的冲突），
        最终版本号必须等于初始版本 + 成功数，杜绝丢失更新。
        """
        cat = MetadataCatalog()
        cat.register_asset(Asset("cfg", columns=(Column("base"),)))
        start = threading.Barrier(8)
        outcomes: list[tuple[int, str]] = []
        lock = threading.Lock()

        def worker(idx: int) -> None:
            start.wait()
            # 所有线程都基于相同的初始版本 1 做 CAS
            ok, result = cat.try_update_asset(
                Asset("cfg", columns=(Column("base"), Column(f"col{idx}"))),
                expected_version=1,
                reason=f"worker-{idx}",
            )
            with lock:
                if ok:
                    outcomes.append((idx, f"committed-v{result.version}"))
                else:
                    outcomes.append((idx, f"conflict:{result.reason}"))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        committed = [o for o in outcomes if o[1].startswith("committed")]
        conflicts = [o for o in outcomes if o[1].startswith("conflict")]
        log("parallel-cas", committed=committed,
            conflict_count=len(conflicts), final_version=cat.get_version("cfg"))
        self.assertEqual(len(committed), 1, msg="恰好一个竞争者应成功")
        self.assertEqual(len(conflicts), 7, msg="其余竞争者必须显式收到冲突")
        self.assertEqual(cat.get_version("cfg"), 2,
                         msg="不得出现丢失更新导致的版本错算")
        # 历史只有两条修订（初始 + 唯一成功的提交）
        self.assertEqual([rv.version for rv in cat.history("cfg")], [1, 2])

    def test_concurrent_register_lineage_is_consistent(self) -> None:
        """并发给不同下游登记血缘，全部应成功且互不丢失。"""
        cat = MetadataCatalog()
        cat.register_asset(Asset("src"))
        for i in range(20):
            cat.register_asset(Asset(f"sink{i}"))
        barrier = threading.Barrier(20)
        errors: list[Exception] = []

        def worker(i: int) -> None:
            barrier.wait()
            try:
                cat.register_lineage(LineageEdge("src", f"sink{i}"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], msg=f"并发血缘登记出现意外错误: {errors!r}")
        result = cat.impact("src", "downstream")
        log("parallel-lineage", sinks=result["affected_count"])
        self.assertEqual(result["affected_count"], 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)

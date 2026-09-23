"""影响分析：深度限制、环、菱形重复路径、大规模图下的稳定性与有界性。

运行：uv run python -m unittest tests.test_impact_analysis -v
"""

from __future__ import annotations

import time
import unittest

from metadata_catalog import Asset, LineageEdge, MetadataCatalog
from metadata_catalog.exceptions import AssetNotFoundError


def log(title: str, **fields) -> None:
    body = ", ".join(f"{k}={v!r}" for k, v in fields.items())
    print(f"[case] {title}: {body}")


class TestImpactAnalysis(unittest.TestCase):
    def _diamond_catalog(self) -> MetadataCatalog:
        # 菱形+环：a -> b,c -> d -> e；另加 d -> a 会成环，故加 d->b 形成环
        cat = MetadataCatalog()
        for node in "abcde":
            cat.register_asset(Asset(node))
        cat.register_lineage(LineageEdge("a", "b"))
        cat.register_lineage(LineageEdge("a", "c"))
        cat.register_lineage(LineageEdge("b", "d"))
        cat.register_lineage(LineageEdge("c", "d"))
        cat.register_lineage(LineageEdge("d", "e"))
        return cat

    def test_downstream_depths_are_shortest_path(self) -> None:
        cat = self._diamond_catalog()
        result = cat.impact("a", "downstream")
        depths = {n["id"]: n["depth"] for n in result["nodes"]}
        log("diamond-downstream", depths=depths, truncated=result["truncated"])
        self.assertEqual(depths, {"a": 0, "b": 1, "c": 1, "d": 2, "e": 3})
        self.assertFalse(result["truncated"])

    def test_max_depth_limits_and_signals_truncation(self) -> None:
        cat = self._diamond_catalog()
        result = cat.impact("a", "downstream", max_depth=1)
        ids = [n["id"] for n in result["nodes"]]
        log("max-depth-1", reached=ids, truncated=result["truncated"])
        self.assertEqual(ids, ["a", "b", "c"])
        self.assertTrue(result["truncated"], msg="边界外仍有节点时必须标记 truncated")

        # depth=0 只含起点
        result0 = cat.impact("a", "downstream", max_depth=0)
        self.assertEqual([n["id"] for n in result0["nodes"]], ["a"])
        self.assertTrue(result0["truncated"])

    def test_cyclic_graph_traversal_is_bounded_and_stable(self) -> None:
        cat = MetadataCatalog()
        for node in "abc":
            cat.register_asset(Asset(node))
        # 环 a->b->c->a
        cat.register_lineage(LineageEdge("a", "b"))
        cat.register_lineage(LineageEdge("b", "c"))
        # 最后这条在无环校验下会被拒绝，因此用直接构图验证遍历器；
        # 业务上环的拒绝已由 test_lineage_validation 覆盖。
        from metadata_catalog.graph import LineageGraph
        g = LineageGraph()
        for n in "abc":
            g.ensure_node(n)
        g.add_edge(LineageEdge("a", "b"))
        g.add_edge(LineageEdge("b", "c"))
        g.add_edge(LineageEdge("c", "a"))
        from metadata_catalog.analysis import analyze_impact
        r1 = analyze_impact(g, "a", "downstream", None)
        r2 = analyze_impact(g, "a", "downstream", None)
        ids1 = [(n["id"], n["depth"]) for n in r1["nodes"]]
        log("cyclic-bfs", result=ids1)
        self.assertEqual(ids1, [("a", 0), ("b", 1), ("c", 2)],
                         msg="环上每个节点只应出现一次，深度为最短距离")
        self.assertEqual(ids1, [(n["id"], n["depth"]) for n in r2["nodes"]],
                         msg="重复遍历结果必须完全一致（可复现）")

    def test_upstream_analysis_mirrors_downstream(self) -> None:
        cat = self._diamond_catalog()
        result = cat.impact("d", "upstream")
        depths = {n["id"]: n["depth"] for n in result["nodes"]}
        log("diamond-upstream", depths=depths)
        self.assertEqual(depths, {"d": 0, "b": 1, "c": 1, "a": 2})

    def test_large_graph_is_bounded_and_fast(self) -> None:
        # 500 个节点的链 + 大量重复回边尝试（重复路径），验证无无界膨胀
        cat = MetadataCatalog()
        n = 500
        for i in range(n):
            cat.register_asset(Asset(f"n{i}"))
        for i in range(n - 1):
            cat.register_lineage(LineageEdge(f"n{i}", f"n{i+1}"))
        # 重复登记同样的边（幂等），不应改变分析结果
        for i in range(0, n - 1, 50):
            cat.register_lineage(LineageEdge(f"n{i}", f"n{i+1}"))

        started = time.perf_counter()
        result = cat.impact("n0", "downstream")
        elapsed_ms = (time.perf_counter() - started) * 1000
        log("large-chain", nodes=result["node_count"], elapsed_ms=round(elapsed_ms, 3))
        self.assertEqual(result["node_count"], n)
        self.assertEqual(result["nodes"][-1]["depth"], n - 1)
        self.assertLess(elapsed_ms, 2000, msg="500 节点遍历异常缓慢，疑似重复计算")

        limited = cat.impact("n0", "downstream", max_depth=10)
        self.assertEqual(limited["node_count"], 11)
        self.assertTrue(limited["truncated"])

    def test_unknown_start_asset_is_rejected(self) -> None:
        cat = MetadataCatalog()
        with self.assertRaises(AssetNotFoundError):
            cat.impact("missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)

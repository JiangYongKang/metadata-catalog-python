"""血缘登记校验：悬空引用与成环必须被明确拒绝，且原因可区分。

运行：uv run python -m unittest tests.test_lineage_validation -v
"""

from __future__ import annotations

import unittest

from metadata_catalog import (
    Asset,
    Column,
    ColumnLineage,
    CycleDetectedError,
    DanglingReferenceError,
    LineageEdge,
    MetadataCatalog,
)


def log(title: str, **fields) -> None:
    """打印关键输入与判定依据，便于复现与排障。"""
    body = ", ".join(f"{k}={v!r}" for k, v in fields.items())
    print(f"[case] {title}: {body}")


class TestLineageValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.cat = MetadataCatalog()
        # 三张表：users(id, name) / orders(id, user_id) / report(uid)
        self.cat.register_asset(Asset(
            "users", kind="table",
            columns=(Column("id", "bigint"), Column("name", "string")),
        ))
        self.cat.register_asset(Asset(
            "orders", kind="table",
            columns=(Column("id", "bigint"), Column("user_id", "bigint")),
        ))
        self.cat.register_asset(Asset(
            "report", kind="table",
            columns=(Column("uid", "bigint"),),
        ))

    def test_rejects_dangling_upstream_asset(self) -> None:
        edge = LineageEdge("ghost", "orders")
        log("dangling-upstream", edge=(edge.upstream_id, edge.downstream_id))
        with self.assertRaises(DanglingReferenceError) as ctx:
            self.cat.register_lineage(edge)
        # 判定原因必须可区分：消息中明确指出是上游资产缺失
        self.assertIn("upstream asset", str(ctx.exception),
                      msg=f"拒绝原因不可区分: {ctx.exception!s}")
        self.assertNotIn("orders->ghost", str(ctx.exception))

    def test_rejects_dangling_downstream_asset(self) -> None:
        edge = LineageEdge("users", "unknown_table")
        log("dangling-downstream", edge=(edge.upstream_id, edge.downstream_id))
        with self.assertRaises(DanglingReferenceError) as ctx:
            self.cat.register_lineage(edge)
        self.assertIn("downstream asset", str(ctx.exception),
                      msg=f"拒绝原因不可区分: {ctx.exception!s}")

    def test_rejects_dangling_column(self) -> None:
        # users.id -> orders.user_id 合法；这里故意引用不存在的列
        edge = LineageEdge(
            "users", "orders",
            (ColumnLineage("not_a_col", "user_id"),),
        )
        log("dangling-column", edge="users->orders",
            upstream_column="not_a_col", downstream_column="user_id")
        with self.assertRaises(DanglingReferenceError) as ctx:
            self.cat.register_lineage(edge)
        self.assertIn("upstream column", str(ctx.exception),
                      msg=f"应明确报告上游列不存在: {ctx.exception!s}")

        edge2 = LineageEdge(
            "users", "orders",
            (ColumnLineage("id", "missing_dst_col"),),
        )
        with self.assertRaises(DanglingReferenceError) as ctx:
            self.cat.register_lineage(edge2)
        self.assertIn("downstream column", str(ctx.exception),
                      msg=f"应明确报告下游列不存在: {ctx.exception!s}")

    def test_rejects_self_loop_and_indirect_cycle(self) -> None:
        self.cat.register_lineage(LineageEdge(
            "users", "orders",
            (ColumnLineage("id", "user_id"),),
        ))
        self.cat.register_lineage(LineageEdge("orders", "report"))

        with self.assertRaises(CycleDetectedError):
            self.cat.register_lineage(LineageEdge("users", "users"))
        log("self-loop rejected", edge="users->users")

        # orders/report -> users 都会令 users->orders->report->users 成环
        for closing in ("orders", "report"):
            with self.subTest(closing=closing):
                with self.assertRaises(CycleDetectedError) as ctx:
                    self.cat.register_lineage(LineageEdge(closing, "users"))
                self.assertIn("cycle", str(ctx.exception),
                              msg=f"成环拒绝原因不可区分: {ctx.exception!s}")
        log("indirect-cycle rejected", closing_edges=("orders->users", "report->users"))

    def test_dangling_distinguished_from_cycle(self) -> None:
        """当两种问题同时存在时，必须报悬空引用而非笼统失败。"""
        with self.assertRaises(DanglingReferenceError):
            # ghost->users：端点未知且会自环式闭合，应优先判悬空
            self.cat.register_lineage(LineageEdge("ghost", "users"))

    def test_idempotent_edge_merges_columns_without_loss(self) -> None:
        e1 = LineageEdge("users", "orders", (ColumnLineage("id", "user_id"),))
        e2 = LineageEdge("users", "orders", (ColumnLineage("name", "user_id"),))
        self.cat.register_lineage(e1)
        result = self.cat.register_lineage(e2)
        log("idempotent-lineage", merged=result.merged, detail=result.detail)
        edge = self.cat.lineage_between("users", "orders")
        pairs = {(c.upstream_column, c.downstream_column) for c in edge.columns}
        self.assertEqual(
            pairs,
            {("id", "user_id"), ("name", "user_id")},
            msg="重复登记不得丢失已有关联列",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""血缘图：边的存储与图遍历原语（纯结构，不含业务校验）。

- 邻接表保存有向边 upstream -> downstream；
- 同一对节点的重复边做列级血缘的集合并（幂等、不丢失）；
- 遍历统一使用带 visited 集合的 BFS，环与重复路径下有界且结果稳定。
"""

from __future__ import annotations

from collections.abc import Iterable

from .models import LineageEdge


class LineageGraph:
    """以邻接表保存血缘边；同一对 (upstream, downstream) 的边幂等合并。"""

    def __init__(self) -> None:
        # downstream 视角：node -> 其下游集合
        self._down: dict[str, set[str]] = {}
        # upstream 视角：node -> 其上游集合
        self._up: dict[str, set[str]] = {}
        self._edges: dict[tuple[str, str], LineageEdge] = {}

    def ensure_node(self, node_id: str) -> None:
        """确保孤立节点也存在于图中（资产即使无边也可被遍历）。"""
        self._down.setdefault(node_id, set())
        self._up.setdefault(node_id, set())

    def add_edge(self, edge: LineageEdge) -> bool:
        """加入一条边；若边已存在则对列级血缘求并。

        返回 True 表示这是一条结构上的新边；False 表示边已存在仅做合并。
        """
        key = (edge.upstream_id, edge.downstream_id)
        existing = self._edges.get(key)
        is_new = existing is None
        if is_new:
            self._down.setdefault(edge.upstream_id, set()).add(edge.downstream_id)
            self._up.setdefault(edge.downstream_id, set()).add(edge.upstream_id)
            self._down.setdefault(edge.downstream_id, set())
            self._up.setdefault(edge.upstream_id, set())
            self._edges[key] = edge
        else:
            merged_columns = tuple(
                sorted(set(existing.columns) | set(edge.columns),
                       key=lambda c: (c.upstream_column, c.downstream_column))
            )
            # 描述不覆盖已有的非空描述
            description = existing.description or edge.description
            self._edges[key] = LineageEdge(
                upstream_id=edge.upstream_id,
                downstream_id=edge.downstream_id,
                columns=merged_columns,
                description=description,
            )
        return is_new

    def edges(self) -> list[LineageEdge]:
        """按 (upstream, downstream) 排序返回全部边，保证稳定可复现。"""
        return [self._edges[k] for k in sorted(self._edges)]

    def get_edge(self, upstream_id: str, downstream_id: str) -> LineageEdge | None:
        return self._edges.get((upstream_id, downstream_id))

    def neighbors(self, node_id: str, direction: str) -> list[str]:
        """返回某节点在指定方向（upstream/downstream）上的邻居，已排序。"""
        table = self._up if direction == "upstream" else self._down
        return sorted(table.get(node_id, ()))

    def has_path(self, src: str, dst: str) -> bool:
        """是否存在从 src 到 dst 的有向路径（含 src == dst）。"""
        if src == dst:
            return True
        seen: set[str] = set()
        stack = [src]
        while stack:
            node = stack.pop()
            for nxt in self._down.get(node, ()):
                if nxt == dst:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return False

    def would_create_cycle(self, upstream: str, downstream: str) -> bool:
        """新增 upstream->downstream 是否会形成环（含自环）。

        判定依据：downstream 已能沿下游方向到达 upstream，则新边将闭合成环。
        """
        if upstream == downstream:
            return True
        return self.has_path(downstream, upstream)

    def traverse(
        self,
        start: str,
        direction: str,
        max_depth: int | None,
    ) -> dict[str, int]:
        """BFS 遍历，返回 {node_id: 最短深度}。

        - depth 0 为起点本身；
        - max_depth=None 表示不限深度；
        - visited 集合保证节点只访问一次，环/重复路径下结果有界；
        - 结果按节点 id 排序消费（见 analysis 层），保证可复现。
        """
        if direction not in ("upstream", "downstream"):
            raise ValueError(f"unknown direction: {direction!r}")
        if max_depth is not None and max_depth < 0:
            raise ValueError("max_depth must be >= 0 or None")

        result: dict[str, int] = {start: 0}
        frontier = [start]
        depth = 0
        while frontier:
            if max_depth is not None and depth >= max_depth:
                break
            depth += 1
            next_frontier: list[str] = []
            for node in frontier:
                for nxt in self.neighbors(node, direction):
                    if nxt not in result:
                        result[nxt] = depth
                        next_frontier.append(nxt)
            frontier = next_frontier
        return result

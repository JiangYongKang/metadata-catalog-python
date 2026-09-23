"""影响分析：基于 LineageGraph 的稳定 BFS 与结果格式化。

结果稳定性规则：
- 遍历只依赖图结构，BFS 使用 visited 集合，每个节点恰好访问一次；
- 输出节点按 (depth, id) 排序，环与重复路径下完全可复现；
- 深度限制下额外给出 truncated 标记，说明结论是否被截断。
"""

from __future__ import annotations

from .graph import LineageGraph

VALID_DIRECTIONS = ("upstream", "downstream")


def analyze_impact(
    graph: LineageGraph,
    start: str,
    direction: str,
    max_depth: int | None,
) -> dict:
    """执行影响分析并返回可解释的结构化结果。

    返回字段：
      start/direction/max_depth：回显关键输入；
      truncated：达到深度上限且更深层仍存在节点时为 True；
      nodes：[{"id", "depth"}]，起点 depth=0，按 (depth, id) 排序；
      affected_count：不含起点本身的受影响节点数。
    """
    if direction not in VALID_DIRECTIONS:
        raise ValueError(
            f"direction must be one of {VALID_DIRECTIONS}, got {direction!r}"
        )

    depths = graph.traverse(start, direction, max_depth)

    truncated = False
    if max_depth is not None:
        boundary = [node for node, d in depths.items() if d == max_depth]
        visited = set(depths)
        truncated = any(
            nxt not in visited
            for node in boundary
            for nxt in graph.neighbors(node, direction)
        )

    ordered = sorted(depths.items(), key=lambda kv: (kv[1], kv[0]))
    return {
        "start": start,
        "direction": direction,
        "max_depth": max_depth,
        "truncated": truncated,
        "node_count": len(ordered),
        "affected_count": len(ordered) - 1,
        "nodes": [{"id": node_id, "depth": depth} for node_id, depth in ordered],
    }

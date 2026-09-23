"""元数据目录：对外的登记、血缘、影响分析、版本与并发门面。

并发模型：
- 单一可重入锁串行化所有写操作，保证状态一致；
- 资产采用乐观版本号（expected_version）做丢失更新防护：
  * register_asset(expected_version=...)：版本过期抛 ConcurrentUpdateError；
  * try_update_asset：版本过期不抛异常，返回结构化 ConflictResult。
- 每次实质性变更追加不可变 AssetRevision，历史结构完整可追溯；
- 血缘边独立于资产修订保存，资产重新登记（含版本演进）不删除任何已有边。
"""

from __future__ import annotations

import threading

from . import analysis
from .exceptions import (
    AssetNotFoundError,
    ConcurrentUpdateError,
    CycleDetectedError,
    DanglingReferenceError,
    DuplicateColumnError,
)
from .graph import LineageGraph
from .models import (
    Asset,
    AssetRevision,
    ConflictResult,
    LineageEdge,
    RegisterResult,
)
from .models import _utcnow  # noqa: PLC2701


def _merge_asset(existing: Asset, incoming: Asset) -> tuple[Asset, bool]:
    """幂等合并两个同名资产定义。

    规则：列按列名做键合并（列定义取新值，不删除旧列）；
    kind/name/description 的空值不覆盖已有非空值。
    返回 (合并后资产, 是否发生实质变化)。
    """
    by_name: dict[str, object] = {}
    order: list[str] = []
    for col in (*existing.columns, *incoming.columns):
        if col.name not in by_name:
            by_name[col.name] = col
            order.append(col.name)
        else:
            # 新登记可补充/更新类型与描述，但不删除列
            old = by_name[col.name]
            by_name[col.name] = type(col)(
                name=col.name,
                type=col.type or old.type,
                description=col.description or old.description,
            )
    columns = tuple(by_name[n] for n in order)
    merged = Asset(
        id=existing.id,
        kind=incoming.kind or existing.kind,
        name=incoming.name or existing.name,
        columns=columns,
        description=incoming.description or existing.description,
    )
    return merged, merged != existing


class MetadataCatalog:
    """线程安全的本地内存元数据目录。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._assets: dict[str, Asset] = {}
        self._current_version: dict[str, int] = {}
        self._history: dict[str, list[AssetRevision]] = {}
        self._graph = LineageGraph()

    # ------------------------------------------------------------------ #
    # 资产登记与版本
    # ------------------------------------------------------------------ #
    def register_asset(
        self,
        asset: Asset,
        *,
        expected_version: int | None = None,
        reason: str = "",
    ) -> RegisterResult:
        """登记或更新资产。

        - 资产不存在：创建，版本为 1；
        - 已存在且内容相同：幂等无操作，版本不变；
        - 已存在且内容有差异：合并后生成新版本（历史保留）；
        - expected_version 不为 None 时做乐观并发校验，过期即抛
          ConcurrentUpdateError（与悬空引用、环等错误类型可区分）。
        """
        if not asset.id:
            raise ValueError("asset.id must be a non-empty string")
        names = [c.name for c in asset.columns]
        if len(names) != len(set(names)):
            dup = sorted({n for n in names if names.count(n) > 1})
            raise DuplicateColumnError(
                f"asset {asset.id!r} has duplicate columns: {dup}"
            )

        with self._lock:
            existing = self._assets.get(asset.id)
            created = existing is None

            if existing is not None and expected_version is not None:
                actual = self._current_version[asset.id]
                if expected_version != actual:
                    raise ConcurrentUpdateError(
                        f"asset {asset.id!r}: expected version {expected_version}, "
                        f"but current version is {actual}"
                    )

            if created:
                self._assets[asset.id] = asset
                self._current_version[asset.id] = 1
                self._graph.ensure_node(asset.id)
                revision = AssetRevision(
                    version=1, asset=asset, changed_at=_utcnow(), reason=reason
                )
                self._history.setdefault(asset.id, []).append(revision)
                return RegisterResult(
                    asset_id=asset.id, version=1, created=True,
                    detail="asset created",
                )

            merged, changed = _merge_asset(existing, asset)
            if not changed:
                return RegisterResult(
                    asset_id=asset.id,
                    version=self._current_version[asset.id],
                    created=False, merged=True,
                    detail="idempotent no-op: definition unchanged",
                )

            new_version = self._current_version[asset.id] + 1
            self._assets[asset.id] = merged
            self._current_version[asset.id] = new_version
            self._history[asset.id].append(
                AssetRevision(
                    version=new_version, asset=merged,
                    changed_at=_utcnow(), reason=reason,
                )
            )
            return RegisterResult(
                asset_id=asset.id, version=new_version,
                created=False, merged=True,
                detail="definition merged; new revision created",
            )

    def try_update_asset(
        self,
        asset: Asset,
        *,
        expected_version: int,
        reason: str = "",
    ) -> tuple[bool, RegisterResult | ConflictResult]:
        """乐观更新的非抛异常形式。

        成功返回 (True, RegisterResult)；
        版本冲突返回 (False, ConflictResult)，调用方必须显式处理，
        绝不会静默覆盖对方的更新。
        """
        with self._lock:
            actual = self._current_version.get(asset.id)
            if actual is None:
                # 资产尚未创建：仅当调用方预期版本为 0 时允许创建
                if expected_version == 0:
                    return True, self.register_asset(asset, reason=reason)
                return False, ConflictResult(
                    asset_id=asset.id,
                    expected_version=expected_version,
                    actual_version=0,
                    reason="asset_not_found",
                )
            if expected_version != actual:
                return False, ConflictResult(
                    asset_id=asset.id,
                    expected_version=expected_version,
                    actual_version=actual,
                    reason="version_mismatch",
                )
            return True, self.register_asset(
                asset, expected_version=expected_version, reason=reason
            )

    def get_asset(self, asset_id: str) -> Asset:
        with self._lock:
            if asset_id not in self._assets:
                raise AssetNotFoundError(f"asset not found: {asset_id!r}")
            return self._assets[asset_id]

    def get_version(self, asset_id: str) -> int:
        with self._lock:
            if asset_id not in self._assets:
                raise AssetNotFoundError(f"asset not found: {asset_id!r}")
            return self._current_version[asset_id]

    def history(self, asset_id: str) -> list[AssetRevision]:
        """返回资产的全部历史修订（按版本升序），可解释结构演进。"""
        with self._lock:
            if asset_id not in self._assets:
                raise AssetNotFoundError(f"asset not found: {asset_id!r}")
            return list(self._history[asset_id])

    # ------------------------------------------------------------------ #
    # 血缘登记
    # ------------------------------------------------------------------ #
    def register_lineage(self, edge: LineageEdge) -> RegisterResult:
        """登记一条血缘边。

        拒绝原因（异常类型可区分）：
        - DanglingReferenceError：端点资产未登记，或列级血缘引用了
          上游/下游资产上不存在的列；
        - CycleDetectedError：该边会使血缘图成环。
        悬空引用优先于环判定（先保证端点齐全再谈结构合法性）。
        已存在的边幂等合并列级血缘，绝不丢失关联。
        """
        with self._lock:
            if edge.upstream_id not in self._assets:
                raise DanglingReferenceError(
                    f"lineage {edge.upstream_id!r} -> {edge.downstream_id!r}: "
                    f"upstream asset is not registered"
                )
            if edge.downstream_id not in self._assets:
                raise DanglingReferenceError(
                    f"lineage {edge.upstream_id!r} -> {edge.downstream_id!r}: "
                    f"downstream asset is not registered"
                )

            upstream_cols = self._assets[edge.upstream_id].column_names()
            downstream_cols = self._assets[edge.downstream_id].column_names()
            for cl in edge.columns:
                if cl.upstream_column not in upstream_cols:
                    raise DanglingReferenceError(
                        f"lineage {edge.upstream_id!r} -> {edge.downstream_id!r}: "
                        f"upstream column {cl.upstream_column!r} does not exist"
                    )
                if cl.downstream_column not in downstream_cols:
                    raise DanglingReferenceError(
                        f"lineage {edge.upstream_id!r} -> {edge.downstream_id!r}: "
                        f"downstream column {cl.downstream_column!r} does not exist"
                    )

            if self._graph.would_create_cycle(edge.upstream_id, edge.downstream_id):
                raise CycleDetectedError(
                    f"lineage {edge.upstream_id!r} -> {edge.downstream_id!r} "
                    f"would create a cycle"
                )

            is_new = self._graph.add_edge(edge)
            return RegisterResult(
                asset_id=f"{edge.upstream_id}->{edge.downstream_id}",
                version=self._current_version[edge.downstream_id],
                created=is_new, merged=not is_new,
                detail="edge created" if is_new else "edge existed; columns merged",
            )

    def lineage_between(self, upstream_id: str, downstream_id: str) -> LineageEdge:
        with self._lock:
            edge = self._graph.get_edge(upstream_id, downstream_id)
            if edge is None:
                raise AssetNotFoundError(
                    f"no lineage edge {upstream_id!r} -> {downstream_id!r}"
                )
            return edge

    def all_lineage(self) -> list[LineageEdge]:
        with self._lock:
            return self._graph.edges()

    # ------------------------------------------------------------------ #
    # 影响分析
    # ------------------------------------------------------------------ #
    def impact(
        self,
        asset_id: str,
        direction: str = "downstream",
        *,
        max_depth: int | None = None,
    ) -> dict:
        """从任一资产出发做上游/下游影响分析，深度可限定。

        结果稳定可复现（节点按 (depth, id) 排序），visited 保证有界，
        含环或重复路径的图上不会重复计算或膨胀。
        """
        with self._lock:
            if asset_id not in self._assets:
                raise AssetNotFoundError(f"asset not found: {asset_id!r}")
            return analysis.analyze_impact(
                self._graph, asset_id, direction, max_depth
            )

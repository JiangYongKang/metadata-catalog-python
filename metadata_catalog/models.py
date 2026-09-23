"""领域模型：资产（含列级）、血缘边、版本修订。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Column:
    """资产内的一列。"""

    name: str
    type: str = ""
    description: str = ""


@dataclass(frozen=True)
class Asset:
    """数据资产定义（不可变值对象，配合版本号使用）。"""

    id: str
    kind: str = ""
    name: str = ""
    columns: tuple[Column, ...] = ()
    description: str = ""

    def column_names(self) -> frozenset[str]:
        return frozenset(c.name for c in self.columns)


@dataclass(frozen=True)
class ColumnLineage:
    """列级血缘：下游列 downstream_column 由上游列 upstream_column 派生。"""

    upstream_column: str
    downstream_column: str


@dataclass(frozen=True)
class LineageEdge:
    """一条血缘边：upstream -> downstream，支持可选的列级映射。

    columns: 列级血缘对的集合；重复登记按集合求并，天然幂等且不丢失关联。
    """

    upstream_id: str
    downstream_id: str
    columns: tuple[ColumnLineage, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class AssetRevision:
    """资产的一次历史修订快照。"""

    version: int
    asset: Asset
    changed_at: datetime
    reason: str = ""


@dataclass(frozen=True)
class ConflictResult:
    """并发冲突的结构化暴露结果（不抛异常时使用）。"""

    asset_id: str
    expected_version: int
    actual_version: int
    reason: str = "version_mismatch"


@dataclass(frozen=True)
class RegisterResult:
    """登记/更新操作的结构化结果，便于调用方判定与日志记录。"""

    asset_id: str
    version: int
    created: bool
    merged: bool = False
    detail: str = ""

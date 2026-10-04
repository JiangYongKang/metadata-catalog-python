"""Append-only audit trail of every lineage registration / removal."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from time import time

# audit action / state constants
ACTION_ADD = "add"
ACTION_REMOVE = "remove"

STATE_PRESENT = "present"
STATE_ABSENT = "absent"


@dataclass(frozen=True)
class LineageChange:
    """One audited lineage mutation with before/after state.

    changed=False records an attempted no-op (e.g. re-adding an edge that
    already existed) so the attempt itself stays auditable without polluting
    state reconstruction.
    """
    seq: int
    timestamp: float
    action: str  # ACTION_ADD | ACTION_REMOVE
    upstream: str
    downstream: str
    upstream_column: str | None
    downstream_column: str | None
    before: str           # STATE_PRESENT | STATE_ABSENT
    after: str
    changed: bool
    batch_id: str | None = None
    change_note: str = ""

    @property
    def key(self) -> tuple:
        return (self.upstream, self.downstream,
                self.upstream_column or "", self.downstream_column or "")

    def involves_asset(self, asset_id: str) -> bool:
        return self.upstream == asset_id or self.downstream == asset_id

    def to_dict(self) -> dict:
        return {
            "seq": self.seq, "timestamp": self.timestamp, "action": self.action,
            "upstream": self.upstream, "downstream": self.downstream,
            "upstream_column": self.upstream_column,
            "downstream_column": self.downstream_column,
            "before": self.before, "after": self.after, "changed": self.changed,
            "batch_id": self.batch_id, "change_note": self.change_note,
        }


def _in_range(ts: float, start: float | None, end: float | None) -> bool:
    if start is not None and ts < start:
        return False
    if end is not None and ts > end:
        return False
    return True


class AuditLog:
    """Append-only lineage change journal.

    Appends and reads are guarded by their own lock. The catalog writes while
    holding its (outer) lock; no code path acquires the catalog lock while
    already holding this one, so nesting stays deadlock-free.
    """

    def __init__(self) -> None:
        self._changes: list[LineageChange] = []
        self._seq = 0
        self._lock = threading.Lock()

    def record(self, action, upstream, downstream, upstream_column,
               downstream_column, before_present, changed, *,
               timestamp=None, batch_id=None, change_note="") -> LineageChange:
        if action not in (ACTION_ADD, ACTION_REMOVE):
            raise ValueError(f"unknown audit action: {action!r}")
        before = STATE_PRESENT if before_present else STATE_ABSENT
        after_present = before_present
        if changed:
            after_present = action == ACTION_ADD
        after = STATE_PRESENT if after_present else STATE_ABSENT
        with self._lock:
            self._seq += 1
            change = LineageChange(
                seq=self._seq,
                timestamp=float(time() if timestamp is None else timestamp),
                action=action,
                upstream=str(upstream),
                downstream=str(downstream),
                upstream_column=upstream_column,
                downstream_column=downstream_column,
                before=before,
                after=after,
                changed=bool(changed),
                batch_id=batch_id,
                change_note=change_note or "",
            )
            self._changes.append(change)
        return change

    def changes_for_asset(self, asset_id, *, start=None, end=None) -> list[LineageChange]:
        """Full chronological history of changes touching one asset.

        Inclusive on both bounds. The caller is responsible for distinguishing
        "asset unknown" (raise) from "asset known, no changes" (empty list).
        """
        asset_id = str(asset_id)
        with self._lock:
            return [c for c in self._changes
                    if c.involves_asset(asset_id)
                    and _in_range(c.timestamp, start, end)]

    def all_changes(self, *, start=None, end=None) -> list[LineageChange]:
        with self._lock:
            return [c for c in self._changes
                    if _in_range(c.timestamp, start, end)]

    def effective_changes(self) -> list[LineageChange]:
        """Changes that actually mutated the graph (fold basis for snapshots)."""
        with self._lock:
            return [c for c in self._changes if c.changed]

    @property
    def first_timestamp(self) -> float | None:
        """Earliest audited activity (None when nothing was ever attempted)."""
        with self._lock:
            return self._changes[0].timestamp if self._changes else None

    @property
    def last_timestamp(self) -> float | None:
        """Latest audited activity."""
        with self._lock:
            return self._changes[-1].timestamp if self._changes else None

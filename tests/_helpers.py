"""Shared test helpers with diagnostic logging."""
from __future__ import annotations


def log_case(title: str, **fields) -> None:
    print(f"\n[CASE] {title}")
    for k, v in fields.items():
        print(f"  {k}: {v}")

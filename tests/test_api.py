"""HTTP 门面集成测试：拒绝原因映射为可区分错误码。

运行：uv run python -m unittest tests.test_api -v
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from metadata_catalog.api import create_app


class TestApi(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app())
        self.client.put("/assets/a", json={"columns": [{"name": "id"}]})
        self.client.put("/assets/b", json={"columns": [{"name": "x"}]})

    def test_error_codes_are_distinguishable(self) -> None:
        r = self.client.put(
            "/lineage", json={"upstream_id": "a", "downstream_id": "nope"}
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["error"], "dangling_reference")

        self.client.put("/lineage", json={"upstream_id": "a", "downstream_id": "b"})
        r = self.client.put(
            "/lineage", json={"upstream_id": "b", "downstream_id": "a"}
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["error"], "cycle_detected")

        r = self.client.put(
            "/assets/a",
            json={"columns": [{"name": "id"}, {"name": "z"}],
                  "expected_version": 1},
        )
        self.assertEqual(r.status_code, 200)  # v1 仍是当前版本（仅加过 b）
        r = self.client.put(
            "/assets/a",
            json={"columns": [{"name": "id"}, {"name": "y"}],
                  "expected_version": 1},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["error"], "concurrent_update")
        print("[case] http-error-codes: dangling=422/dangling_reference, "
              "cycle=422/cycle_detected, conflict=409/concurrent_update")

    def test_history_and_impact_endpoints(self) -> None:
        self.client.put(
            "/assets/a", json={"columns": [{"name": "id"}, {"name": "n"}],
                               "expected_version": 1}
        )
        self.client.put("/lineage", json={"upstream_id": "a", "downstream_id": "b"})
        hist = self.client.get("/assets/a/history").json()
        self.assertEqual([rv["version"] for rv in hist["revisions"]], [1, 2])
        impact = self.client.get("/impact/a?direction=downstream&max_depth=1").json()
        self.assertEqual([n["id"] for n in impact["nodes"]], ["a", "b"])
        print(f"[case] http-history-impact: versions="
              f"{[rv['version'] for rv in hist['revisions']]}, "
              f"impact_nodes={[n['id'] for n in impact['nodes']]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

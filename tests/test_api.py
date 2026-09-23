"""HTTP-level integration tests: endpoints, status codes and error codes."""
import unittest

from fastapi.testclient import TestClient

from metadata_catalog.api import create_app
from metadata_catalog.catalog import MetadataCatalog

from tests._helpers import log_case


class ApiIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(MetadataCatalog()))

    def test_full_flow_distinguisher(self):
        # register two assets
        r1 = self.client.post("/assets", json={"asset_id": "a", "asset_type": "table"})
        r2 = self.client.post("/assets", json={"asset_id": "b", "asset_type": "table"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(self.client.post("/lineage",
                         json={"upstream": "a", "downstream": "b"}).status_code, 200)

        cases = [
            ("dangling asset",
             self.client.post("/lineage", json={"upstream": "a", "downstream": "x"}),
             404, "asset_not_found"),
            ("cycle",
             self.client.post("/lineage", json={"upstream": "b", "downstream": "a"}),
             422, "lineage_cycle"),
            ("unknown impact start",
             self.client.get("/impact/x"),
             404, "asset_not_found"),
        ]
        for title, resp, want_status, want_code in cases:
            log_case(title, status=resp.status_code, body=resp.json())
            self.assertEqual(resp.status_code, want_status,
                             f"reason[{title}]: status {resp.status_code} != {want_status}")
            self.assertEqual(resp.json()["error"]["code"], want_code,
                             f"reason[{title}]: wrong code in {resp.json()}")

    def test_conflict_is_409_with_versions(self):
        c = self.client
        c.post("/assets", json={"asset_id": "a", "asset_type": "table"})
        c.post("/assets", json={"asset_id": "a", "asset_type": "table",
                                "columns": [{"name": "x"}]})
        resp = c.post("/assets", json={
            "asset_id": "a", "asset_type": "table",
            "columns": [{"name": "y"}], "expected_version": 1})
        log_case("concurrent update via HTTP", status=resp.status_code, body=resp.json())
        self.assertEqual(resp.status_code, 409)
        body = resp.json()["error"]
        self.assertEqual(body["code"], "concurrent_update")
        self.assertEqual(body["expected_version"], 1)
        self.assertEqual(body["actual_version"], 2)

    def test_impact_endpoint_depth_limit(self):
        c = self.client
        for aid in "abc":
            c.post("/assets", json={"asset_id": aid, "asset_type": "table"})
        c.post("/lineage", json={"upstream": "a", "downstream": "b"})
        c.post("/lineage", json={"upstream": "b", "downstream": "c"})
        resp = c.get("/impact/a", params={"direction": "downstream", "max_depth": 1})
        body = resp.json()
        log_case("impact depth=1", nodes=body["nodes"], truncated=body["truncated_at"])
        self.assertEqual([n["id"] for n in body["nodes"]], ["a", "b"])
        self.assertEqual(body["truncated_at"], ["b"])

    def test_versions_endpoint(self):
        c = self.client
        c.post("/assets", json={"asset_id": "a", "asset_type": "table",
                                "columns": [{"name": "id"}]})
        c.post("/assets", json={"asset_id": "a", "asset_type": "table",
                                "columns": [{"name": "email"}]})
        resp = c.get("/assets/a/versions")
        versions = resp.json()
        log_case("version history", count=len(versions),
                 columns=[[x["name"] for x in v["columns"]] for v in versions])
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual([x["name"] for x in versions[1]["columns"]], ["id", "email"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

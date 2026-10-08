"""HTTP-level integration tests: endpoints, status codes and error codes."""
import time
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

    def test_audit_reconstruction_and_batch_endpoints(self):
        c = self.client
        for aid in "abcd":
            c.post("/assets", json={"asset_id": aid, "asset_type": "table",
                                    "columns": [{"name": "id"}]})
        # two single-edge registrations with distinct commit instants
        first = c.post("/lineage", json={"upstream": "a", "downstream": "b"})
        self.assertEqual(first.status_code, 200)
        import time
        time.sleep(0.01)  # guarantee distinct wall-clock readings
        second = c.post("/lineage", json={"upstream": "b", "downstream": "c"})
        self.assertEqual(second.status_code, 200)
        audit_a = c.get("/lineage/audit/a").json()
        t_first = audit_a["records"][0]["at"]
        t_second = c.get("/lineage/audit/c").json()["records"][0]["at"]

        # atomic batch (both changes share one commit instant)
        batch = c.post("/lineage/batch", json={"changes": [
            {"op": "add", "upstream": "c", "downstream": "d"},
            {"op": "add", "upstream": "a", "downstream": "d"}]})
        self.assertEqual(batch.status_code, 200, batch.text)
        self.assertEqual(batch.json()["applied"], 2)
        self.assertEqual(len({ch["at"] for ch in batch.json()["changes"]}), 1,
                         "reason: one batch commits at one instant")

        # batch with a bad relation -> 422 with per-item failures, nothing applied
        before = len(c.get("/lineage").json())
        bad = c.post("/lineage/batch", json={"changes": [
            {"op": "add", "upstream": "b", "downstream": "d"},
            {"op": "add", "upstream": "a", "downstream": "ghost"}]})
        log_case("batch rollback via HTTP", status=bad.status_code,
                 failures=bad.json()["error"]["failures"])
        self.assertEqual(bad.status_code, 422)
        self.assertEqual([f["index"] for f in bad.json()["error"]["failures"]], [1])
        self.assertEqual(len(c.get("/lineage").json()), before,
                         "reason: rolled-back batch must not add the valid edge")

        # audit: unknown vs existing-without-changes stay distinguishable
        self.assertEqual(c.get("/lineage/audit/ghost").status_code, 404)
        found = c.get("/lineage/audit/a")
        self.assertEqual(found.json()["status"], "found_changes")

        # reconstruction: one edge at the first instant, two at the second
        snap1 = c.get("/lineage/as-of", params={"at": t_first})
        log_case("as-of HTTP", at=t_first, body=snap1.json())
        self.assertEqual(snap1.json()["edge_count"], 1)
        self.assertEqual(c.get("/lineage/as-of",
                               params={"at": t_second}).json()["edge_count"], 2)
        self.assertEqual(c.get("/lineage/as-of",
                               params={"at": t_first - 100}).status_code, 422)
        # per-version reconstruction (d v1 existed before any lineage touched it)
        atv = c.get("/lineage/at-version/d", params={"version": 1})
        self.assertEqual(atv.json()["edge_count"], 0)

    def test_self_dependency_kinds_and_intra_asset_columns_via_http(self):
        c = self.client
        c.post("/assets", json={"asset_id": "a", "asset_type": "table",
                                "columns": [{"name": "id"}, {"name": "v"}]})

        shapes = [
            ("a->a", {}, "asset_self"),
            ("a.id->a.id", {"upstream_column": "id",
                            "downstream_column": "id"}, "column_self"),
            ("a->a.id", {"downstream_column": "id"}, "asset_own_column"),
            ("a.id->a", {"upstream_column": "id"}, "asset_own_column"),
        ]
        for label, extra, kind in shapes:
            resp = c.post("/lineage",
                          json={"upstream": "a", "downstream": "a", **extra})
            log_case("HTTP self-dependency", relation=label,
                     status=resp.status_code,
                     error=resp.json()["error"])
            self.assertEqual(resp.status_code, 422)
            self.assertEqual(resp.json()["error"]["code"], "lineage_cycle")
            self.assertEqual(resp.json()["error"]["kind"], kind)

        # legal same-table different-column relation: single and batch
        ok_single = c.post("/lineage", json={
            "upstream": "a", "downstream": "a",
            "upstream_column": "id", "downstream_column": "v"})
        self.assertEqual(ok_single.status_code, 200, ok_single.text)
        ok_batch = c.post("/lineage/batch", json={"changes": [
            {"upstream": "a", "downstream": "a",
             "upstream_column": "v", "downstream_column": "id"}]})
        self.assertEqual(ok_batch.status_code, 200, ok_batch.text)
        self.assertEqual(ok_batch.json()["applied"], 1)

        # batch self-dependency failure carries the kind and applies nothing
        before = len(c.get("/lineage").json())
        bad = c.post("/lineage/batch", json={"changes": [
            {"upstream": "a", "downstream": "a", "upstream_column": "id"}]})
        self.assertEqual(bad.status_code, 422)
        f = bad.json()["error"]["failures"][0]
        log_case("HTTP batch self-dependency", kind=f["kind"], code=f["code"])
        self.assertEqual((f["code"], f["kind"]),
                         ("lineage_cycle", "asset_own_column"))
        self.assertEqual(len(c.get("/lineage").json()), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)

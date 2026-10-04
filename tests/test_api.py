"""HTTP-level integration tests: endpoints, status codes and error codes."""
import unittest

from fastapi.testclient import TestClient

from metadata_catalog.api import create_app
from metadata_catalog.catalog import MetadataCatalog

from tests._helpers import log_case


class ControllableClock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def tick(self, dt=1.0):
        self.t += dt
        return self.t


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


class AuditReconstructionBatchApiTest(unittest.TestCase):
    def setUp(self):
        self.clk = ControllableClock()
        self.client = TestClient(create_app(MetadataCatalog(clock=self.clk)))
        c = self.client
        for aid in ("a", "b", "c", "d"):
            c.post("/assets", json={"asset_id": aid, "asset_type": "table",
                                    "columns": [{"name": "id"}]})
            self.clk.tick()
        for u, v in [("a", "b"), ("b", "c"), ("c", "d")]:
            c.post("/lineage", json={"upstream": u, "downstream": v})
            self.clk.tick()
        self.chain_ts = self.clk()
        self.clk.tick()
        c.post("/lineage", json={"upstream": "a", "downstream": "d"})
        self.clk.tick()

    def test_audit_endpoint_distinguishes_unknown_and_empty(self):
        c = self.client
        r_unknown = c.get("/assets/ghost/lineage-changes")
        self.assertEqual(r_unknown.status_code, 404)
        self.assertEqual(r_unknown.json()["error"]["code"], "asset_not_found")
        c.post("/assets", json={"asset_id": "quiet", "asset_type": "table"})
        r_empty = c.get("/assets/quiet/lineage-changes")
        self.assertEqual(r_empty.status_code, 200)
        self.assertEqual(r_empty.json(), [])
        r_list = c.get("/assets/c/lineage-changes")
        self.assertEqual(r_list.status_code, 200)
        log_case("audit over HTTP", unknown=r_unknown.status_code,
                 empty=r_empty.json(),
                 c_entries=[(x["action"], f"{x['upstream']}->{x['downstream']}")
                            for x in r_list.json()])
        self.assertEqual(len(r_list.json()), 2)

    def test_as_of_and_impact_endpoints(self):
        c = self.client
        r = c.get("/lineage/as-of", params={"as_of": self.chain_ts})
        self.assertEqual(r.status_code, 200)
        edges = sorted((e["upstream"], e["downstream"])
                       for e in r.json()["edges"])
        log_case("GET /lineage/as-of", as_of=self.chain_ts, edges=edges)
        self.assertEqual(edges, [("a", "b"), ("b", "c"), ("c", "d")])

        r = c.get("/impact/as-of/a", params={"as_of": self.chain_ts})
        self.assertEqual([n["id"] for n in r.json()["nodes"]],
                         ["a", "b", "c", "d"])
        # shortcut edge is absent at the chain moment but present now
        self.assertEqual(r.json()["nodes"][-1]["id"], "d")
        live = c.get("/impact/a").json()
        self.assertIn({"depth": 1, "source": "a", "target": "d"},
                      live["paths"])

    def test_invalid_times_return_explicit_errors(self):
        c = self.client
        cases = [
            ("nan", 400, "invalid_value"),
            ("inf", 400, "invalid_value"),
            ("0", 400, "before_history"),
            (str(self.clk() + 9999), 400, "future"),
            ("not-a-time", 422, None),  # rejected at parameter binding
        ]
        for raw, want_status, want_reason in cases:
            r = c.get("/lineage/as-of", params={"as_of": raw})
            log_case("invalid time via HTTP", as_of=raw, status=r.status_code,
                     body=r.json())
            self.assertEqual(r.status_code, want_status,
                             f"reason: {raw!r} -> {r.status_code}")
            if want_reason:
                self.assertEqual(r.json()["error"]["reason"], want_reason)

    def test_version_snapshot_endpoint(self):
        c = self.client
        r = c.get("/assets/b/lineage-at-version/1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["edges"], [])
        r = c.get("/assets/ghost/lineage-at-version/1")
        self.assertEqual(r.status_code, 404)
        log_case("version snapshot endpoint", body=r.json())

    def test_batch_endpoint_commit_and_rollback(self):
        c = self.client
        # one bad op => 422 batch_rejected with per-item failure; nothing lands
        r = c.post("/lineage/batch", json={"ops": [
            {"action": "add", "upstream": "a", "downstream": "c"},
            {"action": "add", "upstream": "ghost", "downstream": "d"},
        ]})
        self.assertEqual(r.status_code, 422)
        body = r.json()["error"]
        self.assertEqual(body["code"], "batch_rejected")
        self.assertEqual([(f["index"], f["code"]) for f in body["failures"]],
                         [(1, "asset_not_found")])
        log_case("batch rejected over HTTP", failures=body["failures"])

        # a good batch commits atomically
        r = c.post("/lineage/batch", json={
            "change_note": "reroute",
            "ops": [
                {"action": "add", "upstream": "b", "downstream": "d"},
                {"action": "remove", "upstream": "c", "downstream": "d"},
            ]})
        self.assertEqual(r.status_code, 200, r.text)
        payload = r.json()
        self.assertTrue(payload["committed"])
        self.assertEqual(len(payload["applied"]), 2)
        self.assertEqual({ch["change_note"] for ch in payload["changes"]},
                         {"reroute"})
        log_case("batch committed over HTTP", batch_id=payload["batch_id"],
                 applied=payload["applied"])

        # malformed action -> 400 (bad batch shape)
        r = c.post("/lineage/batch", json={"ops": [{"action": "wat",
                                                    "upstream": "a",
                                                    "downstream": "b"}]})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)

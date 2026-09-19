"""HTTP 端到端测试：在播种《草书诗帖》场景的服务器上走通全链路。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import create_handler, build_services, Handler, SERVICE_ID


def _request(base, method, path, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(base + path, data=data, method=method,
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


class HttpIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = create_handler(build_services(seed_demo=True))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_01_series_listed_with_quota(self):
        status, body = _request(self.base, "GET", "/api/series")
        self.assertEqual(status, 200)
        lim = next(s for s in body["series"] if s["id"] == "LIM_1")
        self.assertEqual(lim["edition_size"], 99)
        self.assertEqual(lim["remaining"], 99)

    def test_02_evaluate_blocks_wrong_region(self):
        status, body = _request(self.base, "POST", "/api/evaluate", {
            "series_id": "LIM_1", "region": "US", "medium": "digital",
            "purpose": "distribution"})
        self.assertEqual(status, 200)
        self.assertFalse(body["all_satisfied"])
        self.assertNotIn("_snapshot_records", body)

    def test_03_issue_and_retry_idempotent(self):
        payload = {"series_id": "LIM_1", "recipient": "user-http-1",
                   "idempotency_key": "http-pay-1", "region": "CN",
                   "medium": "digital", "purpose": "distribution"}
        status, body = _request(self.base, "POST", "/api/editions/issue", payload)
        self.assertEqual(status, 201)
        self.assertFalse(body["replayed"])
        number = body["edition"]["edition_number"]
        # 接口不回传内部证据全文，只给散列
        self.assertNotIn("evidence", body["edition"])
        self.assertRegex(body["edition"]["evidence_hash"], r"^[0-9a-f]{64}$")

        status2, body2 = _request(self.base, "POST", "/api/editions/issue", payload)
        self.assertEqual(status2, 200)
        self.assertTrue(body2["replayed"])
        self.assertEqual(body2["edition"]["edition_number"], number)

    def test_04_issue_with_incomplete_license_is_422_and_no_slot(self):
        status, body = _request(self.base, "POST", "/api/editions/issue", {
            "series_id": "LIM_1", "recipient": "x",
            "idempotency_key": "http-pay-bad", "region": "US",
            "medium": "digital", "purpose": "distribution"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "license_incomplete")
        status, series = _request(self.base, "GET", "/api/series")
        lim = next(s for s in series["series"] if s["id"] == "LIM_1")
        self.assertEqual(lim["issued"], 1)

    def test_05_validation_error_on_missing_field(self):
        status, body = _request(self.base, "POST", "/api/editions/issue",
                                {"series_id": "LIM_1"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_06_lineage_and_summary_after_issue(self):
        number = "LIM_1-01"
        status, full = _request(self.base, "GET",
                                f"/api/editions/{number}/lineage")
        self.assertEqual(status, 200)
        ids = [n["node"]["id"] for n in full["lineage"]]
        self.assertEqual(ids, ["ORG_ZYSM", "BATCH_HD2026", "CROP_1", "WORK_1"])
        self.assertTrue(full["chain_valid"])
        self.assertTrue(any(d["action"] == "edition_issued"
                            for d in full["decisions"]))

        status, summary = _request(
            self.base, "GET",
            f"/api/editions/{number}/summary?region=CN&medium=digital"
            "&purpose=distribution")
        self.assertEqual(status, 200)
        self.assertTrue(summary["authentic"])
        blob = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("terms_ref", blob)
        self.assertNotIn("HT-", blob)

    def test_07_screening_replace_and_freeze(self):
        # 再发一件用于展映
        payload = {"series_id": "LIM_2", "recipient": "user-http-2",
                   "idempotency_key": "http-pay-2", "region": "CN",
                   "medium": "digital", "purpose": "distribution"}
        _, body = _request(self.base, "POST", "/api/editions/issue", payload)
        number = body["edition"]["edition_number"]

        from domain import utcnow
        from datetime import timedelta
        when = (utcnow() + timedelta(days=5)).isoformat(timespec="seconds")
        status, screening = _request(self.base, "POST", "/api/screenings", {
            "screening_id": "SCR-HTTP", "edition_id": number, "region": "CN",
            "medium": "screen", "purpose": "screening",
            "scheduled_at": when, "material_ref": "a.mp4"})
        self.assertEqual(status, 201)

        status, screening = _request(
            self.base, "POST", "/api/screenings/SCR-HTTP/material",
            {"material_ref": "b.mp4"})
        self.assertEqual(status, 200)
        self.assertEqual([m["material_ref"] for m in screening["materials"]],
                         ["a.mp4", "b.mp4"])

        _request(self.base, "POST", "/api/screenings/SCR-HTTP/occur", {})
        status, body = _request(
            self.base, "POST", "/api/screenings/SCR-HTTP/material",
            {"material_ref": "c.mp4"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "screening_frozen")

    def test_08_chain_endpoint(self):
        status, body = _request(self.base, "GET", "/api/chain")
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        self.assertGreater(body["length"], 0)

    def test_09_default_handler_is_exposed(self):
        # 保持既有脚手架契约：Handler 可直接导入且服务身份不变
        self.assertIsNotNone(Handler)
        self.assertEqual(SERVICE_ID, "digital-art-rights")


if __name__ == "__main__":
    unittest.main()

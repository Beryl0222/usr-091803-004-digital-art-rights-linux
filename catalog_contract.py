"""验证数字资产谱系、许可评估、幂等发行与证据留存的领域契约。"""

import json
import unittest

from catalog import (
    STATUS_DISPUTE,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    STATUS_VALID,
    Catalog,
    Conflict,
    LicenseDenied,
)

NOW = "2026-03-01"


def build_catalog(with_publish=True):
    """搭好《草书诗帖》拆分发行的最小场景。"""
    cat = Catalog()
    cat.register_work(work_id="W-ZYM", title="草书诗帖", author="祝允明", repository="灵境艺术馆")
    cat.register_batch(batch_id="B-01", work_id="W-ZYM", acquired_at="2025-12-20", method="高清扫描")
    cat.register_asset(asset_id="A-ROOT", work_id="W-ZYM", batch_id="B-01", crop={"region": "全卷"},
                       parent_asset_id=None, kind="采集母版", content_hash="hash-root", at="2026-01-01")
    cat.register_asset(asset_id="A-1", work_id="W-ZYM", batch_id="B-01", crop={"region": "局部一"},
                       parent_asset_id="A-ROOT", kind="限量", content_hash="hash-a1", at="2026-01-02")
    cat.register_contribution(asset_id="A-1", party="扫描团队", role="采集")
    cat.register_contribution(asset_id="A-1", party="策展团队", role="策划")
    cat.register_license(license_id="L-EXH", right_type="展映", licensor="展映机构",
                         territories=("CN",), media=("线上",), purposes=("展览", "发行"),
                         valid_from="2025-01-01", valid_to="2027-12-31", contract_ref="HT-EXH-001")
    if with_publish:
        cat.register_license(license_id="L-PUB", right_type="出版", licensor="出版机构",
                             territories=("CN",), media=("线上",), purposes=("发行",),
                             valid_from="2025-01-01", valid_to="2026-06-30", contract_ref="HT-PUB-001")
    cat.register_edition(edition_id="E-1", code="CAOSHU-1", asset_id="A-1", total=2,
                         required_rights=("展映", "出版"))
    return cat


def locked_catalog():
    cat = build_catalog()
    cat.lock_edition_quota("E-1", territory="CN", medium="线上", purpose="发行", at=NOW)
    return cat


class LicenseLockTest(unittest.TestCase):
    def test_lock_denied_until_all_licenses_present(self):
        cat = build_catalog(with_publish=False)
        with self.assertRaises(LicenseDenied):
            cat.lock_edition_quota("E-1", territory="CN", medium="线上", purpose="发行", at=NOW)
        self.assertFalse(cat.editions["E-1"].quota_locked)
        decisions = [e for e in cat.ledger.events if e["type"] == "license.decision"]
        self.assertTrue(decisions, "被拒的评估也要留下证据")

    def test_lock_granted_when_licenses_cover_request(self):
        cat = locked_catalog()
        self.assertTrue(cat.editions["E-1"].quota_locked)

    def test_lock_denied_outside_territory(self):
        cat = build_catalog()
        with self.assertRaises(LicenseDenied):
            cat.lock_edition_quota("E-1", territory="US", medium="线上", purpose="发行", at=NOW)


class MintIdempotencyTest(unittest.TestCase):
    def test_payment_retry_returns_same_collectible(self):
        cat = locked_catalog()
        first = cat.confirm_payment("pay-1", "E-1", "owner-a", NOW)
        retry = cat.confirm_payment("pay-1", "E-1", "owner-a", NOW)
        self.assertEqual(first["serial"], "CAOSHU-1-0001")
        self.assertEqual(retry["serial"], first["serial"])
        self.assertEqual(cat.editions["E-1"].sold, 1)

    def test_edition_never_exceeds_total(self):
        cat = locked_catalog()
        cat.confirm_payment("pay-1", "E-1", "owner-a", NOW)
        cat.confirm_payment("pay-2", "E-1", "owner-b", NOW)
        with self.assertRaises(Conflict):
            cat.confirm_payment("pay-3", "E-1", "owner-c", NOW)
        self.assertEqual(cat.editions["E-1"].sold, 2)

    def test_mint_requires_locked_quota(self):
        cat = build_catalog()
        with self.assertRaises(Conflict):
            cat.confirm_payment("pay-1", "E-1", "owner-a", NOW)

    def test_chain_callback_retry_does_not_duplicate(self):
        cat = locked_catalog()
        cat.confirm_payment("pay-1", "E-1", "owner-a", NOW)
        first = cat.confirm_chain_callback("cb-1", "pay-1", "0xabc", NOW)
        retry = cat.confirm_chain_callback("cb-1", "pay-1", "0xabc", NOW)
        self.assertEqual(first, retry)
        self.assertEqual(len(cat.collectibles), 1)
        anchored = [e for e in cat.ledger.events if e["type"] == "chain.anchored"]
        self.assertEqual(len(anchored), 1)


class ExhibitionImmutabilityTest(unittest.TestCase):
    def test_replacing_display_does_not_rewrite_past_exhibition(self):
        cat = locked_catalog()
        shown = cat.record_exhibition("X-1", "A-1", "一号厅", "CN", "线上", NOW)
        cat.replace_display("A-1", "hash-a1-v2", "2026-04-01")
        again = cat.record_exhibition("X-2", "A-1", "二号厅", "CN", "线上", "2026-04-02")
        self.assertEqual(shown["asset_version"], 1)
        self.assertEqual(shown["content_hash"], "hash-a1")
        self.assertEqual(cat.exhibitions["X-1"]["content_hash"], "hash-a1")
        self.assertEqual(again["asset_version"], 2)
        self.assertEqual(again["content_hash"], "hash-a1-v2")

    def test_exhibition_denied_after_revocation(self):
        cat = locked_catalog()
        cat.record_exhibition("X-1", "A-1", "一号厅", "CN", "线上", NOW)
        cat.revoke_license("L-EXH", "2026-03-15")
        with self.assertRaises(LicenseDenied):
            cat.record_exhibition("X-2", "A-1", "二号厅", "CN", "线上", "2026-03-16")
        self.assertIn("X-1", cat.exhibitions, "撤销不影响已经发生的展映")


class DisputeAndTransferTest(unittest.TestCase):
    def test_dispute_blocks_transfer_and_mint_until_closed(self):
        cat = locked_catalog()
        cat.confirm_payment("pay-1", "E-1", "owner-a", NOW)
        cat.open_dispute("D-1", "A-ROOT", "来源归属存疑", "2026-03-10")
        with self.assertRaises(Conflict):
            cat.transfer_collectible("CAOSHU-1-0001", "owner-b", "2026-03-11")
        with self.assertRaises(Conflict, msg="母版争议冻结衍生资产发行"):
            cat.confirm_payment("pay-2", "E-1", "owner-b", "2026-03-11")
        cat.close_dispute("D-1", "2026-03-20")
        moved = cat.transfer_collectible("CAOSHU-1-0001", "owner-b", "2026-03-21")
        self.assertEqual(moved["owner"], "owner-b")

    def test_transfer_keeps_evidence_snapshot(self):
        cat = locked_catalog()
        cat.confirm_payment("pay-1", "E-1", "owner-a", NOW)
        cat.transfer_collectible("CAOSHU-1-0001", "owner-b", NOW)
        event = [e for e in cat.ledger.events if e["type"] == "ownership.transferred"][0]
        self.assertEqual(event["snapshot"]["from"], "owner-a")
        self.assertTrue(event["snapshot"]["licenses"])
        self.assertTrue(event["hash"])


class RightsStatusTest(unittest.TestCase):
    def test_status_transitions(self):
        cat = locked_catalog()
        cat.confirm_payment("pay-1", "E-1", "owner-a", NOW)
        self.assertEqual(cat.rights_status("CAOSHU-1-0001", NOW), STATUS_VALID)
        self.assertEqual(cat.rights_status("CAOSHU-1-0001", "2026-07-01"), STATUS_EXPIRED)
        cat.revoke_license("L-EXH", "2026-03-15")
        self.assertEqual(cat.rights_status("CAOSHU-1-0001", NOW), STATUS_REVOKED)
        cat.open_dispute("D-1", "A-1", "授权争议", "2026-03-16")
        self.assertEqual(cat.rights_status("CAOSHU-1-0001", NOW), STATUS_DISPUTE)

    def test_expiry_sweep_records_evidence(self):
        cat = locked_catalog()
        expired = cat.sweep_expiry("2026-07-01")
        self.assertEqual(expired, ["L-PUB"])
        events = [e for e in cat.ledger.events if e["type"] == "license.expired"]
        self.assertEqual(len(events), 1)
        self.assertIn("contract_ref", events[0]["snapshot"], "内部证据保留合同编号")


class LineageTest(unittest.TestCase):
    def test_lineage_traces_serial_back_to_source(self):
        cat = locked_catalog()
        cat.confirm_payment("pay-1", "E-1", "owner-a", NOW)
        cat.record_exhibition("X-1", "A-1", "一号厅", "CN", "线上", NOW)
        lineage = cat.lineage("CAOSHU-1-0001")
        self.assertEqual(lineage["work"]["title"], "草书诗帖")
        self.assertEqual(lineage["batch"]["batch_id"], "B-01")
        self.assertEqual([a["asset_id"] for a in lineage["asset_chain"]], ["A-1", "A-ROOT"])
        self.assertEqual(lineage["asset_chain"][0]["crop"], {"region": "局部一"})
        self.assertEqual({c["party"] for c in lineage["contributions"]}, {"扫描团队", "策展团队"})
        self.assertEqual(lineage["edition"]["sold"], 1)
        self.assertEqual(lineage["exhibitions"][0]["exhibition_id"], "X-1")
        types = {e["type"] for e in lineage["decisions"]}
        self.assertIn("license.decision", types)
        self.assertIn("collectible.minted", types)
        self.assertIn("edition.quota_locked", types)

    def test_ledger_is_hash_chained(self):
        cat = locked_catalog()
        cat.confirm_payment("pay-1", "E-1", "owner-a", NOW)
        events = cat.ledger.events
        self.assertEqual(events[0]["prev"], "GENESIS")
        for prev, cur in zip(events, events[1:]):
            self.assertEqual(cur["prev"], prev["hash"])


class HttpApiTest(unittest.TestCase):
    """通过 HTTP 接口走通登记、锁定、发行与反查的最小链路。"""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer

        from service import Handler

        Handler.catalog = Catalog()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _post(self, path, payload):
        from urllib.request import Request, urlopen

        data = json.dumps(payload).encode("utf-8")
        request = Request(f"{self.base_url}{path}", data=data,
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=2) as response:
            return json.load(response)

    def _get(self, path):
        from urllib.request import urlopen

        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            return json.load(response)

    def test_end_to_end_flow(self):
        self._post("/works", {"work_id": "W-1", "title": "草书诗帖", "author": "祝允明",
                              "repository": "灵境艺术馆"})
        self._post("/batches", {"batch_id": "B-1", "work_id": "W-1",
                                "acquired_at": "2025-12-20", "method": "高清扫描"})
        self._post("/assets", {"asset_id": "A-1", "work_id": "W-1", "batch_id": "B-1",
                               "crop": {"region": "局部一"}, "parent_asset_id": None,
                               "kind": "限量", "content_hash": "hash-1", "at": NOW})
        self._post("/licenses", {"license_id": "L-1", "right_type": "展映", "licensor": "展映机构",
                                 "territories": ["CN"], "media": ["线上"], "purposes": ["发行", "展览"],
                                 "valid_from": "2025-01-01", "valid_to": "2027-12-31",
                                 "contract_ref": "HT-1"})
        self._post("/editions", {"edition_id": "E-1", "code": "CS-1", "asset_id": "A-1",
                                 "total": 1, "required_rights": ["展映"]})
        locked = self._post("/editions/E-1/lock", {"territory": "CN", "medium": "线上",
                                                   "purpose": "发行", "at": NOW})
        self.assertTrue(locked["quota_locked"])
        minted = self._post("/payments/confirm", {"idempotency_key": "pay-1", "edition_id": "E-1",
                                                  "owner": "owner-a", "at": NOW})
        self.assertEqual(minted["serial"], "CS-1-0001")
        retry = self._post("/payments/confirm", {"idempotency_key": "pay-1", "edition_id": "E-1",
                                                 "owner": "owner-a", "at": NOW})
        self.assertEqual(retry["serial"], minted["serial"])
        lineage = self._get("/lineage/CS-1-0001")
        self.assertEqual(lineage["work"]["title"], "草书诗帖")
        digest = self._get(f"/public/digest/CS-1-0001?at={NOW}")
        self.assertEqual(digest["rights_status"], STATUS_VALID)
        self.assertNotIn("HT-1", json.dumps(digest, ensure_ascii=False))


class PublicDigestTest(unittest.TestCase):
    def test_digest_verifies_and_hides_contract(self):
        cat = locked_catalog()
        cat.confirm_payment("pay-1", "E-1", "owner-a", NOW)
        digest = cat.public_digest("CAOSHU-1-0001", NOW)
        self.assertEqual(digest["rights_status"], STATUS_VALID)
        self.assertTrue(cat.verify_public("CAOSHU-1-0001", NOW, digest["verification"]))
        self.assertFalse(cat.verify_public("CAOSHU-1-0001", NOW, "0" * 64))
        leaked = json.dumps(digest, ensure_ascii=False)
        self.assertNotIn("HT-EXH-001", leaked)
        self.assertNotIn("HT-PUB-001", leaked)
        self.assertNotIn("contract_ref", leaked)
        self.assertNotIn("owner-a", leaked, "持有人身份只以承诺哈希出现")


if __name__ == "__main__":
    unittest.main()

"""谱系反查、决策链与对外脱敏摘要测试。"""

import json
import unittest

import scenario
from licensing import MEDIA_DIGITAL, MEDIA_SCREEN, PURPOSE_DISTRIBUTION, PURPOSE_PUBLIC_SCREENING
from datetime import timedelta
from domain import utcnow


class FullLineageTest(unittest.TestCase):
    def setUp(self):
        self.registry, self.svc, self.prov = scenario.build()
        self.edition, _ = self.svc.issue(
            "LIM_1", "alice", "pay-1", "CN", MEDIA_DIGITAL,
            PURPOSE_DISTRIBUTION)

    def test_lineage_walks_back_to_original(self):
        full = self.prov.full_lineage(self.edition["id"])
        ids = [n["node"]["id"] for n in full["lineage"]]
        self.assertEqual(ids, ["ORG_ZYSM", "BATCH_HD2026", "CROP_1", "WORK_1"])
        kinds = [n["node"]["kind"] for n in full["lineage"]]
        self.assertEqual(kinds[0], "original_work")
        # 原作上能看到馆藏方贡献
        original = full["lineage"][0]
        parties = {c["party_id"] for c in original["contributions"]}
        self.assertIn("LJ", parties)
        # 数字作品上能看到创作方贡献
        work = full["lineage"][-1]
        creators = {c["party_id"] for c in work["contributions"]}
        self.assertIn("DS1", creators)

    def test_decisions_cover_every_authorization_event(self):
        self.svc.revoke_license("LIC_PUB_DIG")
        full = self.prov.full_lineage(self.edition["id"])
        actions = [d["action"] for d in full["decisions"]]
        self.assertIn("edition_issued", actions)
        self.assertIn("license_granted", actions)
        self.assertIn("license_revoked", actions)
        self.assertTrue(full["chain_valid"])
        # 每条决定都带证据
        for decision in full["decisions"]:
            self.assertIn("entry_hash", decision)
            self.assertIn("evidence", decision)


class PublicSummaryTest(unittest.TestCase):
    def setUp(self):
        self.registry, self.svc, self.prov = scenario.build()
        self.edition, _ = self.svc.issue(
            "LIM_1", "alice", "pay-1", "CN", MEDIA_DIGITAL,
            PURPOSE_DISTRIBUTION)

    def test_summary_authentic_and_redacts_contract(self):
        summary = self.prov.public_summary(
            self.edition["id"], "CN", MEDIA_DIGITAL, PURPOSE_DISTRIBUTION)
        self.assertTrue(summary["authentic"])
        self.assertEqual(summary["edition_number"], "LIM_1-01")
        self.assertTrue(summary["rights"]["all_satisfied"])
        blob = json.dumps(summary, ensure_ascii=False)
        # 内部合同编号、备注、权利方身份绝不出现在对外摘要
        for secret in ("terms_ref", "HT-LJ", "HT-CBS", "HT-DS",
                       "licensor", "holder", "中华书画出版社", "收益分成"):
            self.assertNotIn(secret, blob)
        # 但可验证的证据散列在
        self.assertRegex(summary["proof"]["issuance_snapshot_hash"],
                         r"^[0-9a-f]{64}$")
        self.assertTrue(summary["proof"]["chain_valid"])

    def test_summary_current_state_vs_historical_proof(self):
        before = self.prov.public_summary(
            self.edition["id"], "CN", MEDIA_DIGITAL, PURPOSE_DISTRIBUTION)
        self.svc.revoke_license("LIC_COLL_DIG")
        after = self.prov.public_summary(
            self.edition["id"], "CN", MEDIA_DIGITAL, PURPOSE_DISTRIBUTION)
        # 历史发行证据不变
        self.assertEqual(after["proof"]["issuance_snapshot_hash"],
                         before["proof"]["issuance_snapshot_hash"])
        # 当前权利状态实时变化
        self.assertFalse(after["rights"]["all_satisfied"])
        self.assertFalse(after["rights"]["items"]["collection"]["satisfied"])

    def test_screening_public_status(self):
        when = (utcnow() + timedelta(days=10)).isoformat(timespec="seconds")
        self.svc.schedule_screening(
            "SCR-9", self.edition["id"], "CN", MEDIA_SCREEN,
            PURPOSE_PUBLIC_SCREENING, when, "a.mp4")
        self.svc.replace_screening_material("SCR-9", "b.mp4")
        self.svc.mark_screening_occurred("SCR-9")
        status = self.prov.public_screening_status("SCR-9")
        self.assertTrue(status["frozen"])
        self.assertEqual(status["status"], "occurred")
        self.assertEqual([m["material_ref"] for m in status["materials"]],
                         ["a.mp4", "b.mp4"])
        self.assertFalse(status["materials"][0]["active"])
        blob = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("terms_ref", blob)


class DecisionChainTest(unittest.TestCase):
    def test_tampering_is_detected(self):
        registry, svc, _ = scenario.build()
        svc.issue("LIM_2", "u", "k", "CN", MEDIA_DIGITAL, PURPOSE_DISTRIBUTION)
        self.assertTrue(registry.verify_chain())
        # 改写历史决定
        registry.log[0]["payload"]["regions"] = ["US"]
        self.assertFalse(registry.verify_chain())

    def test_deleting_entry_breaks_chain(self):
        registry, svc, _ = scenario.build()
        svc.issue("LIM_2", "u", "k", "CN", MEDIA_DIGITAL, PURPOSE_DISTRIBUTION)
        del registry.log[2]
        self.assertFalse(registry.verify_chain())


if __name__ == "__main__":
    unittest.main()

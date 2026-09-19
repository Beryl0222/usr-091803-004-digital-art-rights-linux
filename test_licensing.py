"""许可判定引擎测试：地域/媒介/用途/期限组合与生命周期状态。"""

import unittest

import licensing
from domain import RIGHT_PUBLICATION, RIGHT_COLLECTION, RIGHT_CREATION


def lic(right="collection", **kw):
    params = dict(
        license_id="L1", right=right, licensor_party_id="A", holder_party_id="B",
        regions=["CN"], media=["digital"], purposes=["distribution"],
        effective_from="2026-01-01T00:00:00+00:00",
        effective_until="2026-12-31T23:59:59+00:00")
    params.update(kw)
    return licensing.new_license(**params)


class StatusLifecycleTest(unittest.TestCase):
    def test_time_window(self):
        record = lic()
        self.assertEqual(licensing.status_at(record, "2025-12-31T23:59:59+00:00"),
                         licensing.ST_PENDING)
        self.assertEqual(licensing.status_at(record, "2026-06-01T00:00:00+00:00"),
                         licensing.ST_ACTIVE)
        self.assertEqual(licensing.status_at(record, "2027-01-01T00:00:00+00:00"),
                         licensing.ST_EXPIRED)

    def test_revocation_overrides_window(self):
        record = lic()
        record["events"].append(
            {"type": "revoked", "at": "2026-03-01T00:00:00+00:00", "detail": {}})
        self.assertEqual(licensing.status_at(record, "2026-06-01T00:00:00+00:00"),
                         licensing.ST_REVOKED)

    def test_dispute_open_and_resolve(self):
        record = lic()
        record["events"].append(
            {"type": "dispute_opened", "at": "2026-02-01T00:00:00+00:00",
             "detail": {}})
        self.assertEqual(licensing.status_at(record, "2026-06-01T00:00:00+00:00"),
                         licensing.ST_DISPUTED)
        record["events"].append(
            {"type": "dispute_resolved", "at": "2026-05-01T00:00:00+00:00",
             "detail": {}})
        self.assertEqual(licensing.status_at(record, "2026-06-01T00:00:00+00:00"),
                         licensing.ST_ACTIVE)
        # 争议解决后又发生新争议
        record["events"].append(
            {"type": "dispute_opened", "at": "2026-08-01T00:00:00+00:00",
             "detail": {}})
        self.assertEqual(licensing.status_at(record, "2026-09-01T00:00:00+00:00"),
                         licensing.ST_DISPUTED)

    def test_transfer_pending_until_completed(self):
        record = lic()
        record["events"].append(
            {"type": "transfer_initiated", "at": "2026-02-01T00:00:00+00:00",
             "detail": {"to_party_id": "C"}})
        self.assertEqual(licensing.status_at(record, "2026-03-01T00:00:00+00:00"),
                         licensing.ST_TRANSFER_PENDING)
        record["events"].append(
            {"type": "transfer_completed", "at": "2026-04-01T00:00:00+00:00",
             "detail": {}})
        self.assertEqual(licensing.status_at(record, "2026-05-01T00:00:00+00:00"),
                         licensing.ST_ACTIVE)


class ScopeMatchingTest(unittest.TestCase):
    def test_region_media_purpose_must_all_match(self):
        record = lic(regions=["CN"], media=["digital"], purposes=["distribution"])
        self.assertTrue(licensing.scope_matches(
            record, "CN", "digital", "distribution", "2026-06-01T00:00:00+00:00"))
        self.assertFalse(licensing.scope_matches(
            record, "US", "digital", "distribution", "2026-06-01T00:00:00+00:00"))
        self.assertFalse(licensing.scope_matches(
            record, "CN", "print", "distribution", "2026-06-01T00:00:00+00:00"))
        self.assertFalse(licensing.scope_matches(
            record, "CN", "digital", "screening", "2026-06-01T00:00:00+00:00"))

    def test_wildcards(self):
        record = lic(regions=["*"], media=["*"], purposes=["*"])
        self.assertTrue(licensing.scope_matches(
            record, "JP", "screen", "screening", "2026-06-01T00:00:00+00:00"))


class EvaluationTest(unittest.TestCase):
    def _three_licenses(self):
        return [
            lic(RIGHT_PUBLICATION, license_id="LP", regions=["CN"]),
            lic(RIGHT_COLLECTION, license_id="LC", regions=["CN"]),
            lic(RIGHT_CREATION, license_id="LCR", regions=["*"]),
        ]

    def test_all_satisfied_with_snapshot(self):
        result = licensing.evaluate(
            self._three_licenses(), ["N1"],
            [RIGHT_PUBLICATION, RIGHT_COLLECTION, RIGHT_CREATION],
            "CN", "digital", "distribution", "2026-06-01T00:00:00+00:00")
        self.assertTrue(result["all_satisfied"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["snapshot"]["license_count"], 3)
        self.assertRegex(result["snapshot"]["snapshot_hash"], r"^[0-9a-f]{64}$")

    def test_missing_right_and_scope_mismatch(self):
        result = licensing.evaluate(
            self._three_licenses(), ["N1"],
            [RIGHT_PUBLICATION, RIGHT_COLLECTION, RIGHT_CREATION],
            "US", "digital", "distribution", "2026-06-01T00:00:00+00:00")
        self.assertFalse(result["all_satisfied"])
        self.assertEqual(set(result["missing"]),
                         {RIGHT_PUBLICATION, RIGHT_COLLECTION})
        self.assertEqual(result["rights"][RIGHT_PUBLICATION]["reason"],
                         "scope_mismatch")

    def test_no_license_reason(self):
        result = licensing.evaluate(
            [lic(RIGHT_COLLECTION, license_id="LC")], ["N1"],
            [RIGHT_PUBLICATION, RIGHT_COLLECTION],
            "CN", "digital", "distribution", "2026-06-01T00:00:00+00:00")
        self.assertEqual(result["rights"][RIGHT_PUBLICATION]["reason"],
                         "no_license")

    def test_expired_has_priority_over_pending(self):
        records = [
            lic(RIGHT_COLLECTION, license_id="future",
                regions=["JP"],
                effective_from="2026-01-01T00:00:00+00:00"),
            lic(RIGHT_COLLECTION, license_id="past", regions=["JP"],
                effective_from="2020-01-01T00:00:00+00:00",
                effective_until="2020-12-31T23:59:59+00:00"),
        ]
        result = licensing.evaluate(
            records, ["N1"], [RIGHT_COLLECTION],
            "JP", "digital", "distribution", "2021-06-01T00:00:00+00:00")
        self.assertEqual(result["rights"][RIGHT_COLLECTION]["reason"],
                         licensing.ST_EXPIRED)

    def test_node_scoped_license_must_overlap_lineage(self):
        record = lic(license_id="LC", node_ids=["OTHER_CROP"])
        result = licensing.evaluate(
            [record], ["CROP_1", "WORK_1"], [RIGHT_COLLECTION],
            "CN", "digital", "distribution", "2026-06-01T00:00:00+00:00")
        self.assertFalse(result["all_satisfied"])
        # 锚定祖先时覆盖派生节点
        record["node_ids"] = ["CROP_1"]
        result = licensing.evaluate(
            [record], ["CROP_1", "WORK_1"], [RIGHT_COLLECTION],
            "CN", "digital", "distribution", "2026-06-01T00:00:00+00:00")
        self.assertTrue(result["all_satisfied"])


class SnapshotImmutabilityTest(unittest.TestCase):
    def test_snapshot_detached_from_live_record(self):
        record = lic()
        snap = licensing.snapshot_licenses([record])
        record["status"] = "revoked"
        record["regions"] = ["US"]
        self.assertEqual(snap["licenses"]["L1"]["status"], "active")
        self.assertEqual(snap["licenses"]["L1"]["regions"], ["CN"])
        self.assertEqual(snap["license_digests"]["L1"],
                         licensing.digest(snap["licenses"]["L1"]))


if __name__ == "__main__":
    unittest.main()

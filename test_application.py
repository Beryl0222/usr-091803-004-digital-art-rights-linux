"""应用服务测试：名额锁定、幂等发行、两段式确认、展映冻结、让渡。"""

import concurrent.futures
import unittest
from datetime import timedelta

from domain import utcnow
import scenario
from errors import LicenseError, EditionError, ConflictError
from licensing import (
    MEDIA_DIGITAL, MEDIA_SCREEN, PURPOSE_DISTRIBUTION,
    PURPOSE_PUBLIC_SCREENING,
)

SCREEN_WHEN = (utcnow() + timedelta(days=12)).isoformat(timespec="seconds")
EVAL_AT = (utcnow() - timedelta(days=1)).isoformat(timespec="seconds")


class IssueTest(unittest.TestCase):
    def setUp(self):
        self.registry, self.svc, _ = scenario.build()

    def test_issue_number_and_snapshot(self):
        edition, replayed = self.svc.issue(
            "LIM_1", "user-1", "pay-1", "CN", MEDIA_DIGITAL,
            PURPOSE_DISTRIBUTION)
        self.assertFalse(replayed)
        self.assertEqual(edition["edition_number"], "LIM_1-01")
        self.assertEqual(edition["serial"], 1)
        self.assertEqual(edition["lineage"],
                         ["LIM_1", "WORK_1", "CROP_1", "BATCH_HD2026", "ORG_ZYSM"])
        self.assertIn("snapshot", edition["evidence"])
        self.assertRegex(edition["evidence"]["snapshot_hash"], r"^[0-9a-f]{64}$")

    def test_retry_same_idempotency_key_never_creates_twice(self):
        first, replayed1 = self.svc.issue(
            "LIM_1", "user-1", "pay-SAME", "CN", MEDIA_DIGITAL,
            PURPOSE_DISTRIBUTION)
        second, replayed2 = self.svc.issue(
            "LIM_1", "user-1", "pay-SAME", "CN", MEDIA_DIGITAL,
            PURPOSE_DISTRIBUTION)
        third, replayed3 = self.svc.issue(
            "LIM_1", "attacker-replaced-recipient", "pay-SAME",
            "CN", MEDIA_DIGITAL, PURPOSE_DISTRIBUTION)
        self.assertFalse(replayed1)
        self.assertTrue(replayed2 and replayed3)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["id"], third["id"])
        # 重试即使替换 recipient 也不能改变首次结果
        self.assertEqual(third["recipient"], "user-1")
        self.assertEqual(self.registry.issued_count("LIM_1"), 1)

    def test_concurrent_same_key_single_edition(self):
        def hit(_):
            edition, _ = self.svc.issue(
                "LIM_1", "user", "KEY", "CN", MEDIA_DIGITAL,
                PURPOSE_DISTRIBUTION)
            return edition["id"]

        with concurrent.futures.ThreadPoolExecutor(20) as pool:
            ids = set(pool.map(hit, range(20)))
        self.assertEqual(ids, {"LIM_1-01"})
        self.assertEqual(self.registry.issued_count("LIM_1"), 1)

    def test_concurrent_distinct_keys_serials_strict(self):
        def hit(i):
            edition, _ = self.svc.issue(
                "LIM_2", f"u{i}", f"K-{i}", "CN", MEDIA_DIGITAL,
                PURPOSE_DISTRIBUTION)
            return edition["serial"]

        with concurrent.futures.ThreadPoolExecutor(16) as pool:
            serials = list(pool.map(hit, range(12)))
        self.assertEqual(sorted(serials), list(range(1, 13)))
        self.assertEqual(self.registry.issued_count("LIM_2"), 12)

    def test_license_incomplete_blocks_slot_and_edition(self):
        with self.assertRaises(LicenseError) as ctx:
            self.svc.issue("LIM_1", "u", "k1", "US", MEDIA_DIGITAL,
                           PURPOSE_DISTRIBUTION)
        self.assertIn("publication", ctx.exception.details["missing"])
        # 被拒绝不占名额
        self.assertEqual(self.registry.issued_count("LIM_1"), 0)
        self.assertEqual(self.registry.list("editions"), [])

    def test_dispute_period_blocks_issue(self):
        self.svc.open_dispute("LIC_CRE_1", reason="归属争议")
        with self.assertRaises(LicenseError) as ctx:
            self.svc.issue("LIM_1", "u", "k", "CN", MEDIA_DIGITAL,
                           PURPOSE_DISTRIBUTION)
        self.assertEqual(
            ctx.exception.details["rights"]["creation"]["reason"], "disputed")

    def test_sold_out(self):
        self.svc.create_series("TINY", "WORK_1", "压测系列", edition_size=2)
        self.svc.issue("TINY", "a", "ka", "CN", MEDIA_DIGITAL,
                       PURPOSE_DISTRIBUTION)
        self.svc.issue("TINY", "b", "kb", "CN", MEDIA_DIGITAL,
                       PURPOSE_DISTRIBUTION)
        with self.assertRaises(EditionError) as ctx:
            self.svc.issue("TINY", "c", "kc", "CN", MEDIA_DIGITAL,
                           PURPOSE_DISTRIBUTION)
        self.assertEqual(ctx.exception.code, "sold_out")

    def test_free_edition_unlimited_and_global(self):
        a, _ = self.svc.issue("FREE", "a", "fa", "US", MEDIA_DIGITAL,
                              PURPOSE_DISTRIBUTION)
        b, _ = self.svc.issue("FREE", "b", "fb", "JP", MEDIA_DIGITAL,
                              PURPOSE_DISTRIBUTION)
        self.assertIsNone(a["edition_size"])
        self.assertEqual(a["serial"], 1)
        self.assertEqual(b["serial"], 2)

    def test_edition_transfer_is_append_only(self):
        edition, _ = self.svc.issue("LIM_1", "alice", "k", "CN",
                                    MEDIA_DIGITAL, PURPOSE_DISTRIBUTION)
        original_hash = edition["evidence"]["snapshot_hash"]
        moved = self.svc.transfer_edition(edition["id"], "bob")
        self.assertEqual(moved["recipient"], "bob")
        self.assertEqual(moved["transfers"][0]["from"], "alice")
        self.assertEqual(moved["transfers"][0]["to"], "bob")
        # 编号与首发证据不变
        self.assertEqual(moved["id"], edition["id"])
        self.assertEqual(moved["evidence"]["snapshot_hash"], original_hash)


class ReservationTest(unittest.TestCase):
    def setUp(self):
        self.registry, self.svc, _ = scenario.build()

    def test_lock_then_confirm(self):
        reservation = self.svc.reserve_slot(
            "LIM_3", "CN", MEDIA_DIGITAL, PURPOSE_DISTRIBUTION,
            reservation_id="R-1")
        self.assertEqual(reservation["status"], "locked")
        self.assertEqual(reservation["number"], "LIM_3-01")
        edition, replayed = self.svc.confirm_reservation(
            "R-1", "buyer", "cb-1")
        self.assertFalse(replayed)
        self.assertEqual(edition["id"], "LIM_3-01")
        # 确认回调重试
        again, replayed = self.svc.confirm_reservation(
            "R-1", "buyer", "cb-1")
        self.assertTrue(replayed)
        self.assertEqual(again["id"], "LIM_3-01")
        self.assertEqual(self.registry.issued_count("LIM_3"), 1)

    def test_confirm_after_revocation_denied_no_edition(self):
        self.svc.reserve_slot(
            "LIM_3", "CN", MEDIA_DIGITAL, PURPOSE_DISTRIBUTION,
            reservation_id="R-2")
        self.svc.revoke_license("LIC_PUB_DIG")
        with self.assertRaises(LicenseError):
            self.svc.confirm_reservation("R-2", "buyer", "cb-2")
        self.assertEqual(self.registry.list("editions"), [])

    def test_cannot_lock_when_license_incomplete(self):
        with self.assertRaises(LicenseError):
            self.svc.reserve_slot("LIM_1", "US", MEDIA_DIGITAL,
                                  PURPOSE_DISTRIBUTION, reservation_id="R-x")


class ScreeningTest(unittest.TestCase):
    def setUp(self):
        self.registry, self.svc, _ = scenario.build()
        self.edition, _ = self.svc.issue(
            "LIM_1", "u", "k", "CN", MEDIA_DIGITAL, PURPOSE_DISTRIBUTION)

    def _schedule(self, sid="SCR-1", when=SCREEN_WHEN):
        return self.svc.schedule_screening(
            sid, self.edition["id"], "CN", MEDIA_SCREEN,
            PURPOSE_PUBLIC_SCREENING, when, "v1.mp4", at=EVAL_AT)

    def test_screening_requires_screening_right(self):
        with self.assertRaises(LicenseError) as ctx:
            self.svc.schedule_screening(
                "S-x", self.edition["id"], "US", MEDIA_SCREEN,
                PURPOSE_PUBLIC_SCREENING, SCREEN_WHEN, "v1.mp4", at=EVAL_AT)
        self.assertIn("screening", ctx.exception.details["missing"])

    def test_material_replace_keeps_history(self):
        self._schedule()
        updated = self.svc.replace_screening_material("SCR-1", "v2.mp4")
        versions = [(m["version"], m["active"]) for m in updated["materials"]]
        self.assertEqual(versions, [(1, False), (2, True)])

    def test_occurred_screening_is_frozen(self):
        self._schedule()
        self.svc.replace_screening_material("SCR-1", "v2.mp4")
        self.svc.mark_screening_occurred("SCR-1")
        with self.assertRaises(ConflictError) as ctx:
            self.svc.replace_screening_material("SCR-1", "v3.mp4")
        self.assertEqual(ctx.exception.code, "screening_frozen")
        # 实际发生展映所用素材（v2）与授权快照都被保留
        screening = self.registry.get("screenings", "SCR-1")
        active = [m for m in screening["materials"] if m["active"]]
        self.assertEqual([m["material_ref"] for m in active], ["v2.mp4"])
        self.assertIn("snapshot", screening["evidence"])

    def test_historical_screening_survives_later_revocation(self):
        self._schedule()
        before = self.registry.get("screenings", "SCR-1")["evidence"]["snapshot_hash"]
        self.svc.mark_screening_occurred("SCR-1")
        self.svc.revoke_license("LIC_SCR")
        after = self.registry.get("screenings", "SCR-1")["evidence"]["snapshot_hash"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

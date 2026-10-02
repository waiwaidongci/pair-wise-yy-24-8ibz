from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import DomainError, RadioDB


class QuotaLedgerTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        # 两个节目版本，均授权华东；广告主"青柠"
        self.ad = self.db.add_program("青柠广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        self.ad2 = self.db.add_program("青柠广告-新版", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_contract_grants_and_schedule_occupies(self):
        cid = self.db.add_contract("青柠", "华东", "2026-09-28", self.ad, 2)
        self.assertEqual(2, self.db.get_contract(cid)["remaining"])
        self.db.schedule_slot("2026-09-28", "09:00", self.ad, "华东")
        self.db.schedule_slot("2026-09-28", "10:00", self.ad, "华东")
        c = self.db.get_contract(cid)
        self.assertEqual(0, c["remaining"])
        self.assertEqual(2, c["occupied"])

    def test_overschedule_shows_remaining_and_conflicts(self):
        cid = self.db.add_contract("青柠", "华东", "2026-09-28", self.ad, 1)
        first = self.db.schedule_slot("2026-09-28", "09:00", self.ad, "华东")
        with self.assertRaisesRegex(DomainError, "剩余名额 0"):
            self.db.schedule_slot("2026-09-28", "10:00", self.ad, "华东")
        try:
            self.db.schedule_slot("2026-09-28", "10:00", self.ad, "华东")
        except DomainError as exc:
            self.assertIn(f"#{first}", str(exc))
            self.assertIn("冲突节目", str(exc))
        self.assertEqual(1, self.db.get_contract(cid)["occupied"])

    def test_version_change_recomputes_unbroadcast_quota(self):
        cid = self.db.add_contract("青柠", "华东", "2026-09-28", self.ad, 1)
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.ad, "华东")
        self.assertEqual(0, self.db.get_contract(cid)["remaining"])
        # 改版到新版本：旧版本占用释放，新版本无合同不占名额
        self.db.replace_slot(slot, self.ad2)
        c = self.db.get_contract(cid)
        self.assertEqual(1, c["remaining"])
        self.assertEqual(0, c["occupied"])

    def test_cancel_releases_quota(self):
        cid = self.db.add_contract("青柠", "华东", "2026-09-28", self.ad, 1)
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.ad, "华东")
        self.db.cancel_slot(slot)
        c = self.db.get_contract(cid)
        self.assertEqual(1, c["remaining"])
        self.assertEqual(0, c["occupied"])

    def test_playout_writeoff_is_idempotent_by_receipt(self):
        cid = self.db.add_contract("青柠", "华东", "2026-09-28", self.ad, 2)
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.ad, "华东")
        self.db.record_playout(slot, "09:00", 5, receipt_no="R001")
        self.db.record_playout(slot, "09:00", 5, receipt_no="R001")  # 同一次回执
        c = self.db.get_contract(cid)
        self.assertEqual(1, c["written_off"])
        self.assertEqual(1, c["remaining"])
        # 不同回执号（补量）再核销一次
        self.db.record_playout(slot, "09:05", 5, receipt_no="R002")
        c = self.db.get_contract(cid)
        self.assertEqual(2, c["written_off"])
        self.assertEqual(0, c["remaining"])

    def test_recover_contract_rebuilds_ledger(self):
        cid = self.db.add_contract("青柠", "华东", "2026-09-28", self.ad, 2)
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.ad, "华东")
        self.db.record_playout(slot, "09:00", 5, receipt_no="R001")
        # 把账删乱，再凭合同号恢复
        self.db.conn.execute("DELETE FROM quota_movements WHERE contract_id=? AND kind!='grant'", (cid,))
        self.db.conn.commit()
        self.assertEqual(0, self.db.get_contract(cid)["written_off"])
        self.db.recover_contract(cid)
        c = self.db.get_contract(cid)
        self.assertEqual(1, c["written_off"])
        self.assertEqual(1, c["remaining"])

    def test_regions_count_separately(self):
        self.db.authorize_region(self.ad, "华北")
        hd = self.db.add_contract("青柠", "华东", "2026-09-28", self.ad, 1)
        hb = self.db.add_contract("青柠", "华北", "2026-09-28", self.ad, 1)
        self.db.schedule_slot("2026-09-28", "09:00", self.ad, "华东")
        self.db.schedule_slot("2026-09-28", "10:00", self.ad, "华北")
        self.assertEqual(0, self.db.get_contract(hd)["remaining"])
        self.assertEqual(0, self.db.get_contract(hb)["remaining"])

    def test_concurrent_schedulers_first_come_first_served(self):
        cid = self.db.add_contract("青柠", "华东", "2026-09-28", self.ad, 1)
        db2 = RadioDB(self.path)
        results = {}

        def worker(name, db, start):
            try:
                results[name] = ("ok", db.schedule_slot("2026-09-28", start, self.ad, "华东"))
            except DomainError as exc:
                results[name] = ("fail", str(exc))

        t1 = threading.Thread(target=worker, args=("A", self.db, "09:00"))
        t2 = threading.Thread(target=worker, args=("B", db2, "10:00"))
        t1.start(); t2.start(); t1.join(); t2.join()
        db2.close()
        oks = [n for n, (s, _) in results.items() if s == "ok"]
        fails = [n for n, (s, _) in results.items() if s == "fail"]
        self.assertEqual(1, len(oks))
        self.assertEqual(1, len(fails))
        self.assertIn("剩余名额 0", results[fails[0]][1])
        self.assertEqual(0, self.db.get_contract(cid)["remaining"])

    def test_availability_shows_occupied_and_free(self):
        self.db.schedule_slot("2026-09-28", "09:00", self.ad, "华东")
        av = self.db.get_availability("2026-09-28", "华东")
        self.assertEqual(1, len(av["occupied"]))
        self.assertEqual("09:00", av["occupied"][0]["start_time"])
        self.assertEqual("09:05", av["occupied"][0]["end_time"])
        free_ranges = [(f["start_time"], f["end_time"]) for f in av["free"]]
        self.assertIn(("06:00", "09:00"), free_ranges)
        self.assertIn(("09:05", "24:00"), free_ranges)


if __name__ == "__main__":
    unittest.main()

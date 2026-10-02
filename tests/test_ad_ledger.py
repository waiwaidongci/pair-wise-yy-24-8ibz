import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adledger import AdLedger, QuotaConflict
from database import DomainError


class AdLedgerTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.ledger = AdLedger(self.path)
        self.items = [
            {"region": "华东", "air_date": "2026-09-28", "version": "A版", "bought": 2},
            {"region": "华北", "air_date": "2026-09-28", "version": "A版", "bought": 1},
            {"region": "华东", "air_date": "2026-09-28", "version": "B版", "bought": 1},
        ]
        self.ledger.create_contract("HT-001", "青柠饮品", self.items)

    def tearDown(self):
        self.ledger.close()
        os.unlink(self.path)

    def acct(self, region="华东", version="A版"):
        return self.ledger._account_dict(
            self.ledger._account("HT-001", region, "2026-09-28", version))

    def test_contract_recovery_is_idempotent(self):
        again = self.ledger.create_contract("HT-001", "青柠饮品", self.items)
        self.assertTrue(again["recovered"])
        self.assertEqual(2, self.acct()["bought"])
        # 凭合同号恢复时若篡改明细必须报错，防止两份账
        tampered = [dict(it, bought=9) for it in self.items]
        with self.assertRaisesRegex(DomainError, "不一致"):
            self.ledger.create_contract("HT-001", "青柠饮品", tampered)

    def test_regions_are_counted_separately(self):
        self.ledger.book_slot("HT-001", "华东", "2026-09-28", "A版", "09:00", 5)
        self.ledger.book_slot("HT-001", "华东", "2026-09-28", "A版", "09:10", 5)
        # 华东售罄不影响华北联播名额
        self.ledger.book_slot("HT-001", "华北", "2026-09-28", "A版", "09:00", 5)
        self.assertEqual(0, self.acct("华东")["remaining"])
        self.assertEqual(0, self.acct("华北")["remaining"])

    def test_concurrent_bookings_first_come_first_served(self):
        results: list = []
        errors: list = []

        def book(start: str):
            try:
                results.append(self.ledger.book_slot(
                    "HT-001", "华东", "2026-09-28", "B版", start, 5, "排期员"))
            except QuotaConflict as exc:
                errors.append(exc)

        threads = [threading.Thread(target=book, args=(t,)) for t in ("10:00", "10:30")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(1, len(results))
        self.assertEqual(1, len(errors))
        loser = errors[0].payload
        self.assertEqual("sold_out", loser["reason"])
        self.assertEqual(0, loser["remaining"])
        # 后到的人看到剩余名额、占用和剩余时段
        self.assertEqual(1, len(loser["day"]["occupied"]))
        self.assertNotIn("10:00-10:05", loser["day"]["free"])

    def test_time_overlap_conflict_shows_conflict_program(self):
        self.ledger.book_slot("HT-001", "华东", "2026-09-28", "A版", "11:00", 5)
        with self.assertRaises(QuotaConflict) as ctx:
            self.ledger.book_slot("HT-001", "华东", "2026-09-28", "B版", "11:02", 5)
        self.assertEqual("time_overlap", ctx.exception.payload["reason"])
        self.assertEqual("A版", ctx.exception.payload["day"]["occupied"][0]["version"])

    def test_revision_recomputes_only_un_aired(self):
        s1 = self.ledger.book_slot("HT-001", "华东", "2026-09-28", "A版", "08:00", 5)["slot"]["id"]
        s2 = self.ledger.book_slot("HT-001", "华东", "2026-09-28", "A版", "09:00", 5)["slot"]["id"]
        # 先核销 s1（已播），再改版：只重算 s2
        self.ledger.submit_receipt("R-1", "HT-001", "华东", "2026-09-28", "A版", s1)
        outcome = self.ledger.revise_slot(s2, "B版")
        self.assertTrue(outcome["changed"])
        self.assertEqual("B版", outcome["slot"]["version"])
        a = self.acct("华东", "A版")
        b = self.acct("华东", "B版")
        # A版：买2、已播核销1、未播占用被转走 -> 剩余1
        self.assertEqual((2, 0, 1, 1), (a["bought"], a["reserved"], a["verified"], a["remaining"]))
        # B版：买1、占用1 -> 剩余0
        self.assertEqual((1, 1, 0, 0), (b["bought"], b["reserved"], b["verified"], b["remaining"]))
        # 已播的名额版本不可再改，旧版本上仍留着已播核销
        with self.assertRaisesRegex(DomainError, "已播出"):
            self.ledger.revise_slot(s1, "B版")

    def test_revision_blocked_when_target_sold_out(self):
        s = self.ledger.book_slot("HT-001", "华东", "2026-09-28", "A版", "08:00", 5)["slot"]["id"]
        # 占掉 B版唯一名额后，A->B 改版必须失败且原占用不动
        self.ledger.book_slot("HT-001", "华东", "2026-09-28", "B版", "12:00", 5)
        with self.assertRaises(QuotaConflict):
            self.ledger.revise_slot(s, "B版")
        self.assertEqual(1, self.acct("华东", "A版")["reserved"])

    def test_cancel_releases_only_un_aired_quota(self):
        s1 = self.ledger.book_slot("HT-001", "华东", "2026-09-28", "A版", "08:00", 5)["slot"]["id"]
        s2 = self.ledger.book_slot("HT-001", "华东", "2026-09-28", "A版", "09:00", 5)["slot"]["id"]
        self.ledger.submit_receipt("R-2", "HT-001", "华东", "2026-09-28", "A版", s1)
        self.ledger.cancel_slot(s2)
        a = self.acct()
        self.assertEqual((0, 1, 1), (a["reserved"], a["verified"], a["remaining"]))
        with self.assertRaisesRegex(DomainError, "已播出"):
            self.ledger.cancel_slot(s1)

    def test_receipt_settles_once_and_is_idempotent(self):
        s = self.ledger.book_slot("HT-001", "华东", "2026-09-28", "A版", "08:00", 5)["slot"]["id"]
        first = self.ledger.submit_receipt("R-3", "HT-001", "华东", "2026-09-28", "A版", s)
        self.assertFalse(first["duplicate"])
        self.assertEqual((0, 1, 1), (first["account"]["reserved"], first["account"]["verified"],
                                     first["account"]["remaining"]))
        # 写入失败后凭回执号重提：不重复核销
        again = self.ledger.submit_receipt("R-3", "HT-001", "华东", "2026-09-28", "A版", s)
        self.assertTrue(again["duplicate"])
        self.assertEqual(1, self.acct()["verified"])
        # 同一排期重复补量：播后才暴露为异常，不扣次
        dup = self.ledger.submit_receipt("R-4", "HT-001", "华东", "2026-09-28", "A版", s, "补量")
        self.assertEqual("duplicate_makegood", dup["receipt"]["settle_status"])
        self.assertEqual(1, self.acct()["verified"])
        board = self.ledger.board("2026-09-28", "华东")
        self.assertIn("duplicate_makegood", {e["kind"] for e in board["exceptions"]})

    def test_over_delivery_exposed_after_air(self):
        self.ledger.submit_receipt("R-5", "HT-001", "华北", "2026-09-28", "A版", None)
        # 账面已无剩余，再来一张实播回执即超投
        out = self.ledger.submit_receipt("R-6", "HT-001", "华北", "2026-09-28", "A版", None)
        self.assertEqual("over_delivery", out["receipt"]["settle_status"])
        self.assertEqual(1, self.acct("华北")["verified"])
        board = self.ledger.board("2026-09-28", "华北")
        self.assertIn("over_delivery", {e["kind"] for e in board["exceptions"]})


if __name__ == "__main__":
    unittest.main()

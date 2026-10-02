"""广告名额账：合同、排期、播出回执共用同一份名额账。

扣次维度为 (合同号, 地区, 日期, 节目版本)。各地区联播互不挤占；
排期占名额(reserved)，播出回执核销(verified)，改版/撤档只重算未播部分。
所有写操作在 BEGIN IMMEDIATE 事务内完成，先到先得，后到者拿到剩余名额与冲突信息。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime

from database import DomainError


class QuotaConflict(DomainError):
    """名额不足或时段冲突，payload 携带剩余名额、占用与剩余时段供页面展示。"""

    def __init__(self, message: str, payload: dict) -> None:
        super().__init__(message)
        self.payload = payload


def _hm(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _hm_text(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _date_text(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise DomainError("日期必须使用 YYYY-MM-DD") from exc


class AdLedger:
    def __init__(self, path: str = "radio.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._schema()

    def close(self) -> None:
        self.conn.close()

    def _schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ad_contracts (
                  contract_no TEXT PRIMARY KEY,
                  advertiser TEXT NOT NULL,
                  note TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ad_quota (
                  contract_no TEXT NOT NULL REFERENCES ad_contracts(contract_no),
                  region TEXT NOT NULL,
                  air_date TEXT NOT NULL,
                  version TEXT NOT NULL,
                  bought INTEGER NOT NULL CHECK(bought > 0),
                  reserved INTEGER NOT NULL DEFAULT 0,
                  verified INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(contract_no, region, air_date, version),
                  CHECK(reserved >= 0),
                  CHECK(verified >= 0),
                  CHECK(reserved + verified <= bought)
                );
                CREATE TABLE IF NOT EXISTS ad_slots (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  contract_no TEXT NOT NULL REFERENCES ad_contracts(contract_no),
                  region TEXT NOT NULL,
                  air_date TEXT NOT NULL,
                  version TEXT NOT NULL,
                  start_time TEXT NOT NULL,
                  duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
                  status TEXT NOT NULL DEFAULT 'planned'
                    CHECK(status IN ('planned','aired','cancelled')),
                  operator TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ad_slots_date_region
                  ON ad_slots(air_date, region);
                CREATE TABLE IF NOT EXISTS ad_slot_revisions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  slot_id INTEGER NOT NULL REFERENCES ad_slots(id) ON DELETE CASCADE,
                  old_version TEXT NOT NULL,
                  new_version TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ad_receipts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  receipt_no TEXT NOT NULL UNIQUE,
                  contract_no TEXT NOT NULL REFERENCES ad_contracts(contract_no),
                  region TEXT NOT NULL,
                  air_date TEXT NOT NULL,
                  version TEXT NOT NULL,
                  slot_id INTEGER REFERENCES ad_slots(id),
                  settle_status TEXT NOT NULL
                    CHECK(settle_status IN ('settled','over_delivery','duplicate_makegood')),
                  note TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ad_settlement_exceptions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  air_date TEXT NOT NULL,
                  receipt_id INTEGER REFERENCES ad_receipts(id) ON DELETE CASCADE,
                  kind TEXT NOT NULL,
                  detail TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                """
            )
            self.conn.commit()

    # ---- 读视图 -----------------------------------------------------------

    def _account(self, contract_no: str, region: str, air_date: str, version: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM ad_quota WHERE contract_no=? AND region=? AND air_date=? AND version=?",
            (contract_no, region, air_date, version),
        ).fetchone()

    @staticmethod
    def _account_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["remaining"] = data["bought"] - data["reserved"] - data["verified"]
        return data

    def day_view(self, air_date: str, region: str) -> dict:
        """某地区某日的占用时段与剩余时段。"""
        rows = self.conn.execute(
            "SELECT s.*, c.advertiser FROM ad_slots s JOIN ad_contracts c ON c.contract_no=s.contract_no "
            "WHERE s.air_date=? AND s.region=? AND s.status!='cancelled' ORDER BY s.start_time, s.id",
            (air_date, region),
        ).fetchall()
        occupied: list[dict] = []
        intervals: list[tuple[int, int]] = []
        for row in rows:
            start = _hm(row["start_time"])
            end = start + row["duration_minutes"]
            occupied.append({
                "slot_id": row["id"], "contract_no": row["contract_no"], "advertiser": row["advertiser"],
                "version": row["version"], "status": row["status"], "operator": row["operator"],
                "start": row["start_time"], "end": _hm_text(min(end, 1440)),
            })
            intervals.append((start, min(end, 1440)))
        intervals.sort()
        free: list[str] = []
        cursor = 0
        for start, end in intervals:
            if start > cursor:
                free.append(f"{_hm_text(cursor)}-{_hm_text(start)}")
            cursor = max(cursor, end)
        if cursor < 1440:
            free.append(f"{_hm_text(cursor)}-24:00")
        return {"air_date": air_date, "region": region, "occupied": occupied, "free": free}

    def board(self, air_date: str, region: str | None = None) -> dict:
        _date_text(air_date)
        sql = ("SELECT q.*, c.advertiser FROM ad_quota q JOIN ad_contracts c ON c.contract_no=q.contract_no "
               "WHERE q.air_date=?")
        params: list[object] = [air_date]
        if region:
            sql += " AND q.region=?"
            params.append(region)
        accounts = [self._account_dict(r) for r in self.conn.execute(
            sql + " ORDER BY q.region, q.contract_no, q.version", params).fetchall()]
        regions = sorted({a["region"] for a in accounts}) if region is None else [region]
        return {
            "air_date": air_date,
            "accounts": accounts,
            "days": [self.day_view(air_date, r) for r in regions],
            "exceptions": [dict(r) for r in self.conn.execute(
                "SELECT * FROM ad_settlement_exceptions WHERE air_date=? ORDER BY id DESC", (air_date,)
            ).fetchall()],
        }

    def get_contract(self, contract_no: str) -> dict:
        header = self.conn.execute("SELECT * FROM ad_contracts WHERE contract_no=?", (contract_no,)).fetchone()
        if not header:
            raise DomainError(f"合同 {contract_no} 不存在，无法凭合同号恢复")
        items = [self._account_dict(r) for r in self.conn.execute(
            "SELECT * FROM ad_quota WHERE contract_no=? ORDER BY region,air_date,version", (contract_no,)
        ).fetchall()]
        return {"contract_no": contract_no, "advertiser": header["advertiser"],
                "note": header["note"], "created_at": header["created_at"], "items": items}

    def get_receipt(self, receipt_no: str) -> dict:
        row = self.conn.execute("SELECT * FROM ad_receipts WHERE receipt_no=?", (receipt_no,)).fetchone()
        if not row:
            raise DomainError(f"回执 {receipt_no} 不存在")
        return dict(row)

    # ---- 写入 -------------------------------------------------------------

    def create_contract(self, contract_no: str, advertiser: str, items: list[dict], note: str = "") -> dict:
        """建合同并立账。合同号即幂等键：写入失败后凭同一合同号重提只会恢复原账，不会重复加名额。"""
        contract_no = (contract_no or "").strip()
        advertiser = (advertiser or "").strip()
        if not contract_no:
            raise DomainError("合同号不能为空")
        if not advertiser:
            raise DomainError("广告主不能为空")
        if not items:
            raise DomainError("合同至少要有一条名额明细")
        clean: list[tuple[str, str, str, int]] = []
        seen: set[tuple[str, str, str]] = set()
        for raw in items:
            region = str(raw.get("region", "")).strip()
            air_date = _date_text(str(raw.get("air_date", "")))
            version = str(raw.get("version", "")).strip()
            try:
                bought = int(raw.get("bought", 0))
            except (TypeError, ValueError) as exc:
                raise DomainError("购买次数必须是整数") from exc
            if not region or not version:
                raise DomainError("地区和节目版本不能为空")
            if bought <= 0:
                raise DomainError("购买次数必须大于0")
            key = (region, air_date, version)
            if key in seen:
                raise DomainError(f"合同明细重复: {region}/{air_date}/{version}")
            seen.add(key)
            clean.append((region, air_date, version, bought))
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.conn.execute("SELECT 1 FROM ad_contracts WHERE contract_no=?", (contract_no,)).fetchone()
            if existing:
                recovered = self.get_contract(contract_no)
                stored = sorted((i["region"], i["air_date"], i["version"], i["bought"]) for i in recovered["items"])
                if stored != sorted(clean):
                    raise DomainError(f"合同号 {contract_no} 已存在，且名额明细与本次提交不一致")
                recovered["recovered"] = True
                self.conn.execute("COMMIT")
                return recovered
            now = datetime.now().isoformat()
            self.conn.execute(
                "INSERT INTO ad_contracts(contract_no,advertiser,note,created_at) VALUES(?,?,?,?)",
                (contract_no, advertiser, note.strip(), now),
            )
            for region, air_date, version, bought in clean:
                self.conn.execute(
                    "INSERT INTO ad_quota(contract_no,region,air_date,version,bought) VALUES(?,?,?,?,?)",
                    (contract_no, region, air_date, version, bought),
                )
            self.conn.execute("COMMIT")
        result = self.get_contract(contract_no)
        result["recovered"] = False
        return result

    def _conflict(self, reason: str, message: str, air_date: str, region: str,
                  account_row: sqlite3.Row | None) -> QuotaConflict:
        payload: dict = {"reason": reason, "day": self.day_view(air_date, region)}
        payload["account"] = self._account_dict(account_row) if account_row else None
        payload["remaining"] = payload["account"]["remaining"] if account_row else 0
        return QuotaConflict(message, payload)

    def book_slot(self, contract_no: str, region: str, air_date: str, version: str,
                  start_time: str, duration_minutes: int, operator: str = "") -> dict:
        """排期扣次（占未播名额）。并发提交时事务串行化，先到先得。"""
        contract_no = (contract_no or "").strip()
        region = (region or "").strip()
        version = (version or "").strip()
        air_date = _date_text(air_date)
        start = _hm(start_time)
        if duration_minutes <= 0:
            raise DomainError("时段时长必须大于0")
        end = start + duration_minutes
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            account = self._account(contract_no, region, air_date, version)
            if not account:
                raise DomainError(f"合同 {contract_no} 在 {region}/{air_date}/{version} 没有名额账")
            for other in self.conn.execute(
                "SELECT * FROM ad_slots WHERE air_date=? AND region=? AND status!='cancelled'",
                (air_date, region),
            ).fetchall():
                other_start = _hm(other["start_time"])
                other_end = other_start + other["duration_minutes"]
                if start < other_end and other_start < end:
                    exc = self._conflict(
                        "time_overlap",
                        f"与排期 #{other['id']}（{other['version']} {other['start_time']}）时段冲突",
                        air_date, region, account,
                    )
                    exc.payload["conflict_slot_id"] = other["id"]
                    raise exc
            if account["bought"] - account["reserved"] - account["verified"] <= 0:
                raise self._conflict("sold_out", "名额已售罄，无法继续排期", air_date, region, account)
            cur = self.conn.execute(
                "INSERT INTO ad_slots(contract_no,region,air_date,version,start_time,duration_minutes,operator,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (contract_no, region, air_date, version, start_time, duration_minutes,
                 (operator or "").strip(), datetime.now().isoformat()),
            )
            slot_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE ad_quota SET reserved=reserved+1 WHERE contract_no=? AND region=? AND air_date=? AND version=?",
                (contract_no, region, air_date, version),
            )
            self.conn.execute("COMMIT")
        return {"slot": self.get_slot(slot_id), "account": self._account_dict(
            self._account(contract_no, region, air_date, version))}

    def revise_slot(self, slot_id: int, new_version: str) -> dict:
        """节目版本改版：只重算未播部分——旧版本释放占用，新版本扣次；已播核销不动。"""
        new_version = (new_version or "").strip()
        if not new_version:
            raise DomainError("新版本号不能为空")
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            slot = self.conn.execute("SELECT * FROM ad_slots WHERE id=?", (slot_id,)).fetchone()
            if not slot:
                raise DomainError("排期不存在")
            if slot["status"] == "cancelled":
                raise DomainError("已撤档排期不能改版")
            if slot["status"] == "aired":
                raise DomainError("已播出部分不可改版，核销仍记在旧版本上")
            if new_version == slot["version"]:
                self.conn.execute("COMMIT")
                return {"slot": self.get_slot(slot_id), "changed": False}
            target = self._account(slot["contract_no"], slot["region"], slot["air_date"], new_version)
            if not target:
                raise DomainError(
                    f"合同 {slot['contract_no']} 在 {slot['region']}/{slot['air_date']}/{new_version} 没有名额账")
            if target["bought"] - target["reserved"] - target["verified"] <= 0:
                raise self._conflict("sold_out", f"新版本 {new_version} 名额不足，改版失败",
                                     slot["air_date"], slot["region"], target)
            self.conn.execute(
                "UPDATE ad_quota SET reserved=reserved-1 WHERE contract_no=? AND region=? AND air_date=? AND version=?",
                (slot["contract_no"], slot["region"], slot["air_date"], slot["version"]),
            )
            self.conn.execute(
                "UPDATE ad_quota SET reserved=reserved+1 WHERE contract_no=? AND region=? AND air_date=? AND version=?",
                (slot["contract_no"], slot["region"], slot["air_date"], new_version),
            )
            self.conn.execute("UPDATE ad_slots SET version=? WHERE id=?", (new_version, slot_id))
            self.conn.execute(
                "INSERT INTO ad_slot_revisions(slot_id,old_version,new_version,created_at) VALUES(?,?,?,?)",
                (slot_id, slot["version"], new_version, datetime.now().isoformat()),
            )
            self.conn.execute("COMMIT")
        return {"slot": self.get_slot(slot_id), "changed": True}

    def cancel_slot(self, slot_id: int) -> dict:
        """撤档：释放未播名额。已播核销保留，不可撤。"""
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            slot = self.conn.execute("SELECT * FROM ad_slots WHERE id=?", (slot_id,)).fetchone()
            if not slot:
                raise DomainError("排期不存在")
            if slot["status"] == "cancelled":
                self.conn.execute("COMMIT")
                return {"slot": self.get_slot(slot_id), "released": False}
            if slot["status"] == "aired":
                raise DomainError("已播出排期不能撤档，核销保留")
            self.conn.execute("UPDATE ad_slots SET status='cancelled' WHERE id=?", (slot_id,))
            self.conn.execute(
                "UPDATE ad_quota SET reserved=reserved-1 WHERE contract_no=? AND region=? AND air_date=? AND version=?",
                (slot["contract_no"], slot["region"], slot["air_date"], slot["version"]),
            )
            self.conn.execute("COMMIT")
        return {"slot": self.get_slot(slot_id), "released": True}

    def submit_receipt(self, receipt_no: str, contract_no: str, region: str, air_date: str,
                       version: str, slot_id: int | None = None, note: str = "") -> dict:
        """播出回执核销。同一回执号只核销一次（重提幂等返回）；超投/重复补量在播后落异常账。"""
        receipt_no = (receipt_no or "").strip()
        contract_no = (contract_no or "").strip()
        region = (region or "").strip()
        version = (version or "").strip()
        air_date = _date_text(air_date)
        if not receipt_no:
            raise DomainError("回执号不能为空")
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            duplicate = self.conn.execute(
                "SELECT * FROM ad_receipts WHERE receipt_no=?", (receipt_no,)).fetchone()
            if duplicate:
                same = (duplicate["contract_no"] == contract_no and duplicate["region"] == region
                        and duplicate["air_date"] == air_date and duplicate["version"] == version
                        and (duplicate["slot_id"] or None) == (slot_id or None))
                if not same:
                    raise DomainError(f"回执号 {receipt_no} 已存在，但核销内容与本次提交不一致")
                self.conn.execute("COMMIT")
                return {"receipt": dict(duplicate), "duplicate": True}

            account = self._account(contract_no, region, air_date, version)
            if not account:
                raise DomainError(f"合同 {contract_no} 在 {region}/{air_date}/{version} 没有名额账")
            slot = None
            settle_status = "settled"
            if slot_id is not None:
                slot = self.conn.execute("SELECT * FROM ad_slots WHERE id=?", (slot_id,)).fetchone()
                if not slot or (slot["contract_no"], slot["region"], slot["air_date"], slot["version"]) != (
                        contract_no, region, air_date, version):
                    raise DomainError("回执与排期的合同/地区/日期/节目版本不一致")
                if slot["status"] == "cancelled":
                    raise DomainError("已撤档排期不能核销")
                if slot["status"] == "aired":
                    settle_status = "duplicate_makegood"
            elif account["bought"] - account["reserved"] - account["verified"] <= 0:
                settle_status = "over_delivery"

            cur = self.conn.execute(
                "INSERT INTO ad_receipts(receipt_no,contract_no,region,air_date,version,slot_id,settle_status,note,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (receipt_no, contract_no, region, air_date, version, slot_id, settle_status,
                 (note or "").strip(), datetime.now().isoformat()),
            )
            receipt_id = int(cur.lastrowid)
            if settle_status == "settled":
                if slot is not None:
                    self.conn.execute(
                        "UPDATE ad_quota SET reserved=reserved-1, verified=verified+1 "
                        "WHERE contract_no=? AND region=? AND air_date=? AND version=?",
                        (contract_no, region, air_date, version),
                    )
                    self.conn.execute("UPDATE ad_slots SET status='aired' WHERE id=?", (slot_id,))
                else:
                    self.conn.execute(
                        "UPDATE ad_quota SET verified=verified+1 "
                        "WHERE contract_no=? AND region=? AND air_date=? AND version=?",
                        (contract_no, region, air_date, version),
                    )
            else:
                kind = "over_delivery" if settle_status == "over_delivery" else "duplicate_makegood"
                detail = ("实播次数已超出合同购买次数" if kind == "over_delivery"
                          else f"排期 #{slot_id} 已核销，本次属于重复补量")
                self.conn.execute(
                    "INSERT INTO ad_settlement_exceptions(air_date,receipt_id,kind,detail,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (air_date, receipt_id, kind, detail, datetime.now().isoformat()),
                )
            self.conn.execute("COMMIT")

        receipt = self.get_receipt(receipt_no)
        account_row = self._account(contract_no, region, air_date, version)
        return {"receipt": receipt, "duplicate": False, "account": self._account_dict(account_row)}

    def get_slot(self, slot_id: int) -> dict:
        row = self.conn.execute(
            "SELECT s.*, c.advertiser FROM ad_slots s JOIN ad_contracts c ON c.contract_no=s.contract_no WHERE s.id=?",
            (slot_id,),
        ).fetchone()
        if not row:
            raise DomainError("排期不存在")
        return dict(row)

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM ad_contracts").fetchone()[0]:
            return
        self.create_contract("HT-2026-0001", "青柠饮品", [
            {"region": "华东", "air_date": "2026-09-28", "version": "青柠15秒-A版", "bought": 3},
            {"region": "华北", "air_date": "2026-09-28", "version": "青柠15秒-A版", "bought": 2},
            {"region": "华东", "air_date": "2026-09-28", "version": "青柠15秒-B版", "bought": 2},
        ], "演示合同")
        self.book_slot("HT-2026-0001", "华东", "2026-09-28", "青柠15秒-A版", "11:00", 5, "排期员甲")

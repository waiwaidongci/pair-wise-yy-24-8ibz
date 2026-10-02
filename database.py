from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path


class DomainError(ValueError):
    """A business-rule violation that should be shown to the API caller."""


PROGRAM_KINDS = {"music", "ad", "talk", "live"}
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _minutes(value: str) -> int:
    if value == "24:00":
        return 24 * 60
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _overlap(a_start: str, a_duration: int, b_start: str, b_duration: int) -> bool:
    start_a, start_b = _minutes(a_start), _minutes(b_start)
    return start_a < start_b + b_duration and start_b < start_a + a_duration


class RadioDB:
    """SQLite-backed radio scheduling service.

    The service keeps planning and actual playout separate. A replacement is
    accepted only when the complete plan remains valid; reconciliation never
    rewrites the plan, it records discrepancies for operators.
    """

    def __init__(self, path: str = "radio.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        # Let concurrent BEGIN IMMEDIATE calls wait for each other instead of
        # failing with SQLITE_BUSY, so two schedulers submitting at the same
        # time are serialized first-come-first-served.
        self.conn.execute("PRAGMA busy_timeout = 10000")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS programs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              kind TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              start_date TEXT NOT NULL,
              end_date TEXT NOT NULL,
              sponsor TEXT,
              cooldown_minutes INTEGER NOT NULL DEFAULT 0 CHECK(cooldown_minutes >= 0),
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
              UNIQUE(title, start_date, end_date)
            );
            CREATE TABLE IF NOT EXISTS program_regions (
              program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
              region TEXT NOT NULL,
              PRIMARY KEY(program_id, region)
            );
            CREATE TABLE IF NOT EXISTS blocked_windows (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              region TEXT NOT NULL,
              weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
              start_time TEXT NOT NULL,
              end_time TEXT NOT NULL,
              reason TEXT NOT NULL,
              CHECK(start_time < end_time)
            );
            CREATE TABLE IF NOT EXISTS sponsor_policies (
              sponsor TEXT PRIMARY KEY,
              min_gap_minutes INTEGER NOT NULL CHECK(min_gap_minutes >= 0)
            );
            CREATE TABLE IF NOT EXISTS slots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL,
              start_time TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              program_id INTEGER NOT NULL REFERENCES programs(id),
              region TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'planned'
                CHECK(status IN ('planned','replaced','cancelled')),
              replaced_from INTEGER REFERENCES programs(id),
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_slots_date_region ON slots(air_date, region);
            CREATE TABLE IF NOT EXISTS playout_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
              actual_start TEXT NOT NULL,
              actual_duration_minutes INTEGER NOT NULL CHECK(actual_duration_minutes >= 0),
              actual_program_id INTEGER REFERENCES programs(id),
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_exceptions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL,
              slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
              kind TEXT NOT NULL,
              detail TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(air_date, slot_id, kind)
            );
            CREATE TABLE IF NOT EXISTS contracts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              advertiser TEXT NOT NULL,
              region TEXT NOT NULL,
              air_date TEXT NOT NULL,
              program_id INTEGER NOT NULL REFERENCES programs(id),
              total_count INTEGER NOT NULL CHECK(total_count > 0),
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(advertiser, region, air_date, program_id)
            );
            CREATE TABLE IF NOT EXISTS quota_movements (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
              delta INTEGER NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('grant','occupy','release','writeoff')),
              ref_type TEXT NOT NULL,
              ref_id INTEGER NOT NULL,
              idem_key TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_quota_movements_contract ON quota_movements(contract_id);
            CREATE INDEX IF NOT EXISTS idx_quota_movements_slot ON quota_movements(ref_type, ref_id);
            """
        )
        self.conn.commit()
        # Lightweight migration: older databases lack the receipt_no column on playout_logs.
        cols = [row[1] for row in self.conn.execute("PRAGMA table_info(playout_logs)").fetchall()]
        if "receipt_no" not in cols:
            self.conn.execute("ALTER TABLE playout_logs ADD COLUMN receipt_no TEXT")
            self.conn.commit()

    def seed_demo(self) -> None:
        existing = self.conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
        if existing:
            return
        music = self.add_program("晨间轻音乐", "music", 30, "2026-01-01", "2026-12-31", "青柠饮品", 45, ["华东"])
        news = self.add_program("城市早报", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        ad = self.add_program("青柠饮品广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠饮品", 60, ["华东"])
        self.add_sponsor_policy("青柠饮品", 90)
        self.add_blocked_window("华东", 0, "08:00", "08:30", "周一设备检修")
        self.schedule_slot("2026-09-28", "09:00", music, "华东")
        self.schedule_slot("2026-09-28", "10:00", news, "华东")
        self.schedule_slot("2026-09-28", "11:00", ad, "华东")

    def add_program(self, title: str, kind: str, duration_minutes: int, start_date: str, end_date: str,
                    sponsor: str | None = None, cooldown_minutes: int = 0,
                    regions: list[str] | None = None) -> int:
        if not title.strip():
            raise DomainError("节目名称不能为空")
        if kind not in PROGRAM_KINDS:
            raise DomainError(f"不支持的节目类型: {kind}")
        if duration_minutes <= 0:
            raise DomainError("节目时长必须大于0")
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        if end < start:
            raise DomainError("授权结束日期不能早于开始日期")
        if cooldown_minutes < 0:
            raise DomainError("冷却时间不能为负数")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO programs(title,kind,duration_minutes,start_date,end_date,sponsor,cooldown_minutes) VALUES(?,?,?,?,?,?,?)",
                (title.strip(), kind, duration_minutes, start_date, end_date, (sponsor or "").strip() or None, cooldown_minutes),
            )
            program_id = int(cur.lastrowid)
            for region in regions or []:
                self.conn.execute("INSERT INTO program_regions(program_id,region) VALUES(?,?)", (program_id, region.strip()))
        return program_id

    def authorize_region(self, program_id: int, region: str) -> None:
        if not region.strip():
            raise DomainError("地区不能为空")
        with self.transaction():
            if not self.conn.execute("SELECT 1 FROM programs WHERE id=?", (program_id,)).fetchone():
                raise DomainError("节目不存在")
            self.conn.execute("INSERT OR IGNORE INTO program_regions(program_id,region) VALUES(?,?)", (program_id, region.strip()))

    def add_sponsor_policy(self, sponsor: str, min_gap_minutes: int) -> None:
        if not sponsor.strip() or min_gap_minutes < 0:
            raise DomainError("赞助商和最小间隔必须有效")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO sponsor_policies(sponsor,min_gap_minutes) VALUES(?,?) "
                "ON CONFLICT(sponsor) DO UPDATE SET min_gap_minutes=excluded.min_gap_minutes",
                (sponsor.strip(), min_gap_minutes),
            )

    def add_blocked_window(self, region: str, weekday: int, start_time: str, end_time: str, reason: str) -> int:
        if weekday not in range(7) or _minutes(start_time) >= _minutes(end_time):
            raise DomainError("禁播时段参数无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason) VALUES(?,?,?,?,?)",
                (region.strip(), weekday, start_time, end_time, reason.strip() or "禁播"),
            )
        return int(cur.lastrowid)

    # ------------------------------------------------------------------
    # 广告合同与名额账
    # ------------------------------------------------------------------
    def add_contract(self, advertiser: str, region: str, air_date: str, program_id: int,
                     total_count: int, note: str = "") -> int:
        advertiser = advertiser.strip()
        region = region.strip()
        if not advertiser:
            raise DomainError("广告主不能为空")
        if not region:
            raise DomainError("地区不能为空")
        if total_count <= 0:
            raise DomainError("购买次数必须大于0")
        try:
            datetime.strptime(air_date, "%Y-%m-%d")
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        if not self.conn.execute("SELECT 1 FROM programs WHERE id=?", (program_id,)).fetchone():
            raise DomainError("节目版本不存在")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO contracts(advertiser,region,air_date,program_id,total_count,note,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (advertiser, region, air_date, program_id, total_count, note, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("已存在相同广告主/地区/日期/节目版本的合同") from exc
            contract_id = int(cur.lastrowid)
            self.conn.execute(
                "INSERT INTO quota_movements(contract_id,delta,kind,ref_type,ref_id,idem_key,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (contract_id, total_count, "grant", "contract", contract_id,
                 f"contract:{contract_id}:grant", datetime.now().isoformat()),
            )
        return contract_id

    def _contract_for(self, advertiser: str, region: str, air_date: str, program_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM contracts WHERE advertiser=? AND region=? AND air_date=? AND program_id=? ORDER BY id LIMIT 1",
            (advertiser, region, air_date, program_id),
        ).fetchone()

    def _contract_summary(self, contract_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        if not row:
            raise DomainError("合同不存在")
        agg = self.conn.execute(
            "SELECT "
            "COALESCE(SUM(delta) FILTER (WHERE kind='occupy'),0) AS occ, "
            "COALESCE(SUM(delta) FILTER (WHERE kind='release'),0) AS rel, "
            "COALESCE(SUM(delta) FILTER (WHERE kind='writeoff'),0) AS woff "
            "FROM quota_movements WHERE contract_id=?",
            (contract_id,),
        ).fetchone()
        d = dict(row)
        d["occupied"] = -(int(agg["occ"]) + int(agg["rel"]))
        d["released"] = int(agg["rel"])
        d["written_off"] = -int(agg["woff"])
        d["remaining"] = int(row["total_count"]) + int(agg["occ"]) + int(agg["rel"]) + int(agg["woff"])
        return d

    def get_contract(self, contract_id: int) -> dict:
        return self._contract_summary(contract_id)

    def list_contracts(self, region: str | None = None, air_date: str | None = None,
                        advertiser: str | None = None) -> list[dict]:
        sql = "SELECT id FROM contracts WHERE 1=1"
        params: list[object] = []
        if region:
            sql += " AND region=?"
            params.append(region)
        if air_date:
            sql += " AND air_date=?"
            params.append(air_date)
        if advertiser:
            sql += " AND advertiser=?"
            params.append(advertiser)
        sql += " ORDER BY id"
        return [self._contract_summary(int(r["id"])) for r in self.conn.execute(sql, params).fetchall()]

    def _slot_row(self, slot_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT s.*, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id WHERE s.id=?",
            (slot_id,),
        ).fetchone()

    def _occupy_for_slot(self, slot_id: int) -> int | None:
        """Hold one quota against the contract matching the slot's current version.

        Raises DomainError with the remaining quota and the conflicting programs
        when the contract has no quota left, so the later scheduler sees both.
        """
        slot = self._slot_row(slot_id)
        if not slot:
            raise DomainError("排期不存在")
        sponsor = slot["sponsor"]
        if not sponsor:
            return None
        contract = self._contract_for(sponsor, slot["region"], slot["air_date"], slot["program_id"])
        if not contract:
            return None
        summary = self._contract_summary(int(contract["id"]))
        if summary["remaining"] < 1:
            conflicts = [dict(r) for r in self.conn.execute(
                "SELECT s.id, s.start_time, s.duration_minutes, s.status, p.title "
                "FROM slots s JOIN programs p ON p.id=s.program_id "
                "WHERE s.region=? AND s.air_date=? AND s.program_id=? AND s.status!='cancelled' AND s.id!=? "
                "ORDER BY s.start_time",
                (slot["region"], slot["air_date"], slot["program_id"], slot_id),
            ).fetchall()]
            conflict_text = "；".join(
                f"#{c['id']} {c['title']} {c['start_time']}({c['status']})" for c in conflicts
            ) or "无"
            raise DomainError(
                f"合同 {contract['id']}（{sponsor} {slot['region']} {slot['air_date']} 版本#{slot['program_id']}）"
                f"剩余名额 {summary['remaining']} 次，无法占用；冲突节目: {conflict_text}"
            )
        self.conn.execute(
            "INSERT OR IGNORE INTO quota_movements(contract_id,delta,kind,ref_type,ref_id,idem_key,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (int(contract["id"]), -1, "occupy", "slot", slot_id,
             f"slot:{slot_id}:occupy", datetime.now().isoformat()),
        )
        return int(contract["id"])

    def _release_for_slot(self, slot_id: int) -> None:
        occ = self.conn.execute(
            "SELECT contract_id FROM quota_movements WHERE ref_type='slot' AND ref_id=? AND kind='occupy'",
            (slot_id,),
        ).fetchone()
        if not occ:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO quota_movements(contract_id,delta,kind,ref_type,ref_id,idem_key,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (int(occ["contract_id"]), +1, "release", "slot", slot_id,
             f"slot:{slot_id}:release", datetime.now().isoformat()),
        )

    def _writeoff_for_slot(self, slot_id: int, playout_id: int, receipt_no: str | None = None) -> None:
        """Release the slot's hold and record the write-off, idempotently."""
        slot = self._slot_row(slot_id)
        if not slot:
            return
        occ = self.conn.execute(
            "SELECT contract_id FROM quota_movements WHERE ref_type='slot' AND ref_id=? AND kind='occupy'",
            (slot_id,),
        ).fetchone()
        contract_id = int(occ["contract_id"]) if occ else None
        if occ:
            self.conn.execute(
                "INSERT OR IGNORE INTO quota_movements(contract_id,delta,kind,ref_type,ref_id,idem_key,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (contract_id, +1, "release", "slot", slot_id,
                 f"slot:{slot_id}:release", datetime.now().isoformat()),
            )
        elif slot["sponsor"]:
            contract = self._contract_for(slot["sponsor"], slot["region"], slot["air_date"], slot["program_id"])
            if contract:
                contract_id = int(contract["id"])
        if contract_id is None:
            return
        idem = f"receipt:{receipt_no}" if (receipt_no or "").strip() else f"playout:{playout_id}"
        self.conn.execute(
            "INSERT OR IGNORE INTO quota_movements(contract_id,delta,kind,ref_type,ref_id,idem_key,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (contract_id, -1, "writeoff", "playout", playout_id, idem, datetime.now().isoformat()),
        )

    def recover_contract(self, contract_id: int) -> dict:
        """Rebuild the ledger for a contract from current slots and playouts.

        Used after a failed write to restore the account by contract number:
        every non-grant movement is dropped and recomputed from scratch.
        """
        contract = self.conn.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        if not contract:
            raise DomainError("合同不存在")
        with self.transaction():
            self.conn.execute("DELETE FROM quota_movements WHERE contract_id=? AND kind!='grant'", (contract_id,))
            slots = self.conn.execute(
                "SELECT s.*, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id "
                "WHERE s.region=? AND s.air_date=? AND s.program_id=? AND s.status!='cancelled' "
                "ORDER BY s.start_time",
                (contract["region"], contract["air_date"], contract["program_id"]),
            ).fetchall()
            for slot in slots:
                if slot["sponsor"] != contract["advertiser"]:
                    continue
                playouts = self.conn.execute(
                    "SELECT id, receipt_no FROM playout_logs WHERE slot_id=? ORDER BY id", (slot["id"],)
                ).fetchall()
                if playouts:
                    # Reconstruct the full lifecycle: the hold was placed at
                    # schedule time and released on the first receipt; every
                    # receipt is one consumption.
                    self.conn.execute(
                        "INSERT OR IGNORE INTO quota_movements(contract_id,delta,kind,ref_type,ref_id,idem_key,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (contract_id, -1, "occupy", "slot", slot["id"],
                         f"slot:{slot['id']}:occupy", datetime.now().isoformat()),
                    )
                    self.conn.execute(
                        "INSERT OR IGNORE INTO quota_movements(contract_id,delta,kind,ref_type,ref_id,idem_key,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (contract_id, +1, "release", "slot", slot["id"],
                         f"slot:{slot['id']}:release", datetime.now().isoformat()),
                    )
                    for pl in playouts:
                        idem = f"receipt:{pl['receipt_no']}" if pl["receipt_no"] else f"playout:{pl['id']}"
                        self.conn.execute(
                            "INSERT OR IGNORE INTO quota_movements(contract_id,delta,kind,ref_type,ref_id,idem_key,created_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (contract_id, -1, "writeoff", "playout", pl["id"], idem, datetime.now().isoformat()),
                        )
                else:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO quota_movements(contract_id,delta,kind,ref_type,ref_id,idem_key,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (contract_id, -1, "occupy", "slot", slot["id"],
                         f"slot:{slot['id']}:occupy", datetime.now().isoformat()),
                    )
        return self._contract_summary(contract_id)

    def get_availability(self, air_date: str, region: str,
                         day_start: str = "06:00", day_end: str = "24:00") -> dict:
        """Occupied slots and remaining free time ranges for a day/region."""
        try:
            datetime.strptime(air_date, "%Y-%m-%d")
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        self.conn.execute("SELECT 1 FROM program_regions WHERE region=? LIMIT 1", (region,)).fetchone()
        occupied: list[dict] = []
        intervals: list[tuple[int, int]] = []
        for row in self.conn.execute(
            "SELECT s.*, p.title FROM slots s JOIN programs p ON p.id=s.program_id "
            "WHERE s.air_date=? AND s.region=? AND s.status!='cancelled' ORDER BY s.start_time",
            (air_date, region),
        ).fetchall():
            start = _minutes(row["start_time"])
            end = start + int(row["duration_minutes"])
            occupied.append({
                "id": row["id"], "title": row["title"], "program_id": row["program_id"],
                "start_time": row["start_time"], "end_time": f"{end // 60:02d}:{end % 60:02d}",
                "status": row["status"],
            })
            intervals.append((start, end))
        intervals.sort()
        merged: list[tuple[int, int]] = []
        for start, end in intervals:
            if merged and start < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        free: list[dict] = []
        cursor = _minutes(day_start)
        close = _minutes(day_end)
        for start, end in merged:
            if start > cursor:
                free.append({"start_time": f"{cursor // 60:02d}:{cursor % 60:02d}",
                             "end_time": f"{min(start, close) // 60:02d}:{min(start, close) % 60:02d}"})
            cursor = max(cursor, end)
        if cursor < close:
            free.append({"start_time": f"{cursor // 60:02d}:{cursor % 60:02d}",
                         "end_time": f"{close // 60:02d}:{close % 60:02d}"})
        return {"date": air_date, "region": region, "occupied": occupied, "free": free}

    def _validate_slot(self, air_date: str, start_time: str, duration: int, program_id: int,
                       region: str, ignore_slot_id: int | None = None) -> None:
        try:
            day = datetime.strptime(air_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("播出日期必须使用 YYYY-MM-DD") from exc
        try:
            _minutes(start_time)
        except ValueError as exc:
            raise DomainError("开始时间必须使用 HH:MM") from exc
        if duration <= 0:
            raise DomainError("排期时长必须大于0")
        program = self.conn.execute("SELECT * FROM programs WHERE id=? AND active=1", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在或未启用")
        if program["duration_minutes"] != duration:
            raise DomainError(f"排期时长必须等于节目时长 {program['duration_minutes']} 分钟")
        if not (program["start_date"] <= air_date <= program["end_date"]):
            raise DomainError("播出日期超出授权窗口")
        if not self.conn.execute(
            "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (program_id, region)
        ).fetchone():
            raise DomainError(f"节目未授权在{region}播出")
        end_minutes = _minutes(start_time) + duration
        blocked = self.conn.execute(
            "SELECT * FROM blocked_windows WHERE region=? AND weekday=?",
            (region, day.weekday()),
        ).fetchall()
        for window in blocked:
            if _minutes(window["start_time"]) < end_minutes and _minutes(start_time) < _minutes(window["end_time"]):
                raise DomainError(f"与禁播时段冲突: {window['reason']}")
        sql = "SELECT * FROM slots WHERE air_date=? AND region=? AND status!='cancelled'"
        params: list[object] = [air_date, region]
        if ignore_slot_id is not None:
            sql += " AND id!=?"
            params.append(ignore_slot_id)
        for existing in self.conn.execute(sql, params).fetchall():
            if _overlap(start_time, duration, existing["start_time"], existing["duration_minutes"]):
                raise DomainError(f"与排期 #{existing['id']} 时间重叠")
        if program["cooldown_minutes"]:
            previous = self.conn.execute(
                "SELECT * FROM slots WHERE air_date=? AND region=? AND program_id=? AND status!='cancelled' AND id!=? "
                "AND start_time < ? ORDER BY start_time DESC LIMIT 1",
                (air_date, region, program_id, ignore_slot_id or -1, start_time),
            ).fetchone()
            if previous:
                gap = _minutes(start_time) - (_minutes(previous["start_time"]) + previous["duration_minutes"])
                if gap < program["cooldown_minutes"]:
                    raise DomainError(f"与上一期节目间隔不足冷却时间 {program['cooldown_minutes']} 分钟")
        if program["sponsor"]:
            policy = self.conn.execute("SELECT min_gap_minutes FROM sponsor_policies WHERE sponsor=?", (program["sponsor"],)).fetchone()
            if policy:
                gap = policy["min_gap_minutes"]
                all_sponsored = self.conn.execute(
                    "SELECT s.*, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id "
                    "WHERE s.air_date=? AND s.region=? AND s.status!='cancelled' AND p.sponsor=? AND s.id!=?",
                    (air_date, region, program["sponsor"], ignore_slot_id or -1),
                ).fetchall()
                for other in all_sponsored:
                    if _overlap(start_time, duration, other["start_time"], other["duration_minutes"]):
                        raise DomainError(f"与赞助商 {program['sponsor']} 的其他节目冲突")
                    distance = abs(_minutes(start_time) - (_minutes(other["start_time"]) + other["duration_minutes"]))
                    if distance < gap:
                        raise DomainError(f"与赞助商 {program['sponsor']} 的节目间隔不足 {gap} 分钟")

    def schedule_slot(self, air_date: str, start_time: str, program_id: int, region: str) -> int:
        program = self.conn.execute("SELECT duration_minutes FROM programs WHERE id=?", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在")
        with self.transaction():
            self._validate_slot(air_date, start_time, int(program["duration_minutes"]), program_id, region)
            cur = self.conn.execute(
                "INSERT INTO slots(air_date,start_time,duration_minutes,program_id,region,created_at) VALUES(?,?,?,?,?,?)",
                (air_date, start_time, int(program["duration_minutes"]), program_id, region, datetime.now().isoformat()),
            )
            slot_id = int(cur.lastrowid)
            # Occupy the contract quota last: a quota failure rolls the whole
            # schedule back, so a rejected slot is never saved.
            self._occupy_for_slot(slot_id)
        return slot_id

    def replace_slot(self, slot_id: int, new_program_id: int) -> dict:
        """Replace a planned item and revalidate the resulting plan atomically.

        The unbroadcast quota is recomputed: the old version's hold is released
        and the new version's contract is occupied instead. A version change on
        an already-aired slot leaves its write-off untouched.
        """
        with self.transaction():
            slot = self.conn.execute("SELECT * FROM slots WHERE id=? AND status='planned'", (slot_id,)).fetchone()
            if not slot:
                raise DomainError("只能替换尚未播出且状态为 planned 的排期")
            program = self.conn.execute("SELECT * FROM programs WHERE id=?", (new_program_id,)).fetchone()
            if not program:
                raise DomainError("替换节目不存在")
            self._validate_slot(slot["air_date"], slot["start_time"], int(program["duration_minutes"]), new_program_id, slot["region"], slot_id)
            already_aired = self.conn.execute("SELECT 1 FROM playout_logs WHERE slot_id=? LIMIT 1", (slot_id,)).fetchone() is not None
            if not already_aired:
                self._release_for_slot(slot_id)
            self.conn.execute(
                "UPDATE slots SET program_id=?, duration_minutes=?, replaced_from=?, status='replaced' WHERE id=?",
                (new_program_id, int(program["duration_minutes"]), slot["program_id"], slot_id),
            )
            if not already_aired:
                self._occupy_for_slot(slot_id)
        return self.get_slot(slot_id)

    def get_slot(self, slot_id: int) -> dict:
        row = self.conn.execute(
            "SELECT s.*, p.title, p.kind, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id WHERE s.id=?",
            (slot_id,),
        ).fetchone()
        if not row:
            raise DomainError("排期不存在")
        return dict(row)

    def cancel_slot(self, slot_id: int) -> dict:
        """Pull a slot from the schedule; its unbroadcast hold is released."""
        with self.transaction():
            slot = self.conn.execute(
                "SELECT * FROM slots WHERE id=? AND status!='cancelled'", (slot_id,)
            ).fetchone()
            if not slot:
                raise DomainError("排期不存在或已撤档")
            self._release_for_slot(slot_id)
            self.conn.execute("UPDATE slots SET status='cancelled' WHERE id=?", (slot_id,))
        return self.get_slot(slot_id)

    def record_playout(self, slot_id: int, actual_start: str, actual_duration_minutes: int,
                       actual_program_id: int | None = None, note: str = "",
                       receipt_no: str | None = None) -> int:
        if not self.conn.execute("SELECT 1 FROM slots WHERE id=?", (slot_id,)).fetchone():
            raise DomainError("排期不存在")
        if actual_duration_minutes < 0:
            raise DomainError("实际时长不能为负数")
        _minutes(actual_start)
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO playout_logs(slot_id,actual_start,actual_duration_minutes,actual_program_id,note,receipt_no,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (slot_id, actual_start, actual_duration_minutes, actual_program_id, note,
                 (receipt_no or "").strip() or None, datetime.now().isoformat()),
            )
            playout_id = int(cur.lastrowid)
            # Idempotent write-off: the same receipt (receipt_no) never consumes
            # quota twice. A hold is released first, then the write-off recorded.
            self._writeoff_for_slot(slot_id, playout_id, receipt_no)
        return playout_id

    def reconcile_date(self, air_date: str) -> list[dict]:
        """Compare the latest playout per slot with the plan and persist exceptions."""
        try:
            datetime.strptime(air_date, "%Y-%m-%d")
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        with self.transaction():
            self.conn.execute("DELETE FROM reconciliation_exceptions WHERE air_date=?", (air_date,))
            slots = self.conn.execute(
                "SELECT s.*, p.title, p.sponsor, p.kind FROM slots s JOIN programs p ON p.id=s.program_id "
                "WHERE s.air_date=? AND s.status!='cancelled' ORDER BY s.start_time", (air_date,)
            ).fetchall()
            exceptions: list[tuple[int, str, str]] = []
            for slot in slots:
                log = self.conn.execute(
                    "SELECT * FROM playout_logs WHERE slot_id=? ORDER BY id DESC LIMIT 1", (slot["id"],)
                ).fetchone()
                if not log:
                    exceptions.append((slot["id"], "missed", "没有实播记录"))
                    continue
                actual_program_id = log["actual_program_id"] or slot["program_id"]
                if actual_program_id != slot["program_id"]:
                    exceptions.append((slot["id"], "wrong_program", f"计划节目 #{slot['program_id']}，实播节目 #{actual_program_id}"))
                delta = log["actual_duration_minutes"] - slot["duration_minutes"]
                if abs(delta) > 30:
                    kind = "overrun" if delta > 0 else "underrun"
                    exceptions.append((slot["id"], kind, f"与计划相差 {delta:+d} 分钟"))
                actual = self.conn.execute(
                    "SELECT p.* FROM programs p WHERE p.id=?", (actual_program_id,)
                ).fetchone()
                if actual:
                    region_ok = self.conn.execute(
                        "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (actual_program_id, slot["region"])
                    ).fetchone()
                    if not region_ok or not (actual["start_date"] <= air_date <= actual["end_date"]):
                        exceptions.append((slot["id"], "out_of_license", "实播节目超出地区或日期授权"))
            for slot_id, kind, detail in exceptions:
                self.conn.execute(
                    "INSERT INTO reconciliation_exceptions(air_date,slot_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                    (air_date, slot_id, kind, detail, datetime.now().isoformat()),
                )
        return self.get_exceptions(air_date)

    def get_exceptions(self, air_date: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM reconciliation_exceptions WHERE air_date=? ORDER BY slot_id, kind", (air_date,)
        ).fetchall()]

    def snapshot(self) -> dict:
        programs = [dict(row) for row in self.conn.execute("SELECT * FROM programs ORDER BY id").fetchall()]
        slots = [dict(row) for row in self.conn.execute(
            "SELECT s.*, p.title, p.kind FROM slots s JOIN programs p ON p.id=s.program_id ORDER BY s.air_date,s.start_time"
        ).fetchall()]
        return {"programs": programs, "slots": slots,
                "contracts": self.list_contracts(),
                "exceptions": [dict(row) for row in self.conn.execute(
                    "SELECT * FROM reconciliation_exceptions ORDER BY id DESC LIMIT 50"
                ).fetchall()]}

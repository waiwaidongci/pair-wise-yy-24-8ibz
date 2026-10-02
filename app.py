from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from database import DomainError, RadioDB
from adledger import AdLedger, QuotaConflict

BASE = Path(__file__).resolve().parent
DB_PATH = os.environ.get("RADIO_DB", str(BASE / "radio.db"))


class Handler(BaseHTTPRequestHandler):
    db = RadioDB(DB_PATH)
    ads = AdLedger(DB_PATH)

    def log_message(self, fmt, *args):
        return

    def _json(self, status: int, payload) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DomainError("请求体必须是合法 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            if parsed.path in ("/", "/index.html"):
                data = (BASE / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if parsed.path == "/api/state":
                return self._json(200, self.db.snapshot())
            if parsed.path == "/api/reconciliation":
                date = parse_qs(parsed.query).get("date", [""])[0]
                if not date:
                    raise DomainError("缺少 date 参数")
                return self._json(200, {"exceptions": self.db.get_exceptions(date)})
            if parsed.path == "/api/ads/board":
                query = parse_qs(parsed.query)
                date = query.get("date", [""])[0]
                if not date:
                    raise DomainError("缺少 date 参数")
                region = query.get("region", [""])[0]
                return self._json(200, self.ads.board(date, region or None))
            if len(parts) == 4 and parts[:3] == ["api", "ads", "contracts"]:
                return self._json(200, self.ads.get_contract(parts[3]))
            if len(parts) == 4 and parts[:3] == ["api", "ads", "receipts"]:
                return self._json(200, self.ads.get_receipt(parts[3]))
            if len(parts) == 4 and parts[:3] == ["api", "ads", "slots"]:
                return self._json(200, {"slot": self.ads.get_slot(int(parts[3]))})
            self._json(404, {"ok": False, "error": "接口不存在"})
        except QuotaConflict as exc:
            self._json(409, {"ok": False, "error": str(exc), "conflict": exc.payload})
        except DomainError as exc:
            self._json(400, {"ok": False, "error": str(exc)})

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parsed.path == "/api/programs":
                program_id = self.db.add_program(
                    str(body.get("title", "")), str(body.get("kind", "music")),
                    int(body.get("duration_minutes", 0)), str(body.get("start_date", "")),
                    str(body.get("end_date", "")), body.get("sponsor"), int(body.get("cooldown_minutes", 0)),
                    body.get("regions") or [],
                )
                return self._json(201, {"ok": True, "id": program_id})
            if parsed.path == "/api/schedule":
                slot_id = self.db.schedule_slot(
                    str(body.get("air_date", "")), str(body.get("start_time", "")),
                    int(body.get("program_id", 0)), str(body.get("region", "")),
                )
                return self._json(201, {"ok": True, "id": slot_id, "slot": self.db.get_slot(slot_id)})
            if parsed.path == "/api/playout":
                log_id = self.db.record_playout(
                    int(body.get("slot_id", 0)), str(body.get("actual_start", "")),
                    int(body.get("actual_duration_minutes", 0)),
                    int(body["actual_program_id"]) if body.get("actual_program_id") else None,
                    str(body.get("note", "")),
                )
                return self._json(201, {"ok": True, "id": log_id})
            if parsed.path == "/api/reconcile":
                return self._json(200, {"ok": True, "exceptions": self.db.reconcile_date(str(body.get("date", "")))})
            if parsed.path == "/api/ads/contracts":
                return self._json(201, {"ok": True, "contract": self.ads.create_contract(
                    str(body.get("contract_no", "")), str(body.get("advertiser", "")),
                    body.get("items") or [], str(body.get("note", "")),
                )})
            if parsed.path == "/api/ads/slots":
                result = self.ads.book_slot(
                    str(body.get("contract_no", "")), str(body.get("region", "")),
                    str(body.get("air_date", "")), str(body.get("version", "")),
                    str(body.get("start_time", "")), int(body.get("duration_minutes", 0)),
                    str(body.get("operator", "")),
                )
                return self._json(201, {"ok": True, **result})
            if parsed.path == "/api/ads/receipts":
                result = self.ads.submit_receipt(
                    str(body.get("receipt_no", "")), str(body.get("contract_no", "")),
                    str(body.get("region", "")), str(body.get("air_date", "")),
                    str(body.get("version", "")),
                    int(body["slot_id"]) if body.get("slot_id") else None,
                    str(body.get("note", "")),
                )
                return self._json(201, {"ok": True, **result})
            if len(parts) == 5 and parts[:2] == ["api", "ads"] and parts[4] == "revise":
                return self._json(200, {"ok": True, **self.ads.revise_slot(int(parts[3]), str(body.get("new_version", "")))})
            if len(parts) == 5 and parts[:2] == ["api", "ads"] and parts[4] == "cancel":
                return self._json(200, {"ok": True, **self.ads.cancel_slot(int(parts[3]))})
            if len(parts) == 4 and parts[:2] == ["api", "slots"] and parts[3] == "replace":
                return self._json(200, {"ok": True, "slot": self.db.replace_slot(int(parts[2]), int(body.get("new_program_id", 0)))})
            if len(parts) == 4 and parts[:2] == ["api", "programs"] and parts[3] == "regions":
                self.db.authorize_region(int(parts[2]), str(body.get("region", "")))
                return self._json(201, {"ok": True})
            self._json(404, {"ok": False, "error": "接口不存在"})
        except QuotaConflict as exc:
            self._json(409, {"ok": False, "error": str(exc), "conflict": exc.payload})
        except (DomainError, ValueError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})


def main() -> None:
    RadioDB(DB_PATH).seed_demo()
    AdLedger(DB_PATH).seed_demo()
    port = int(os.environ.get("PORT", "8111"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Radio scheduling service: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

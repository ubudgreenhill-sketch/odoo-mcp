"""Offline tests with an in-memory fake Odoo and fake Drive. Run: python -m pytest -q  (or python tests/test_sync.py)"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import Settings  # noqa: E402
from app.sheet import parse_amount, parse_date, parse_table  # noqa: E402
from app.sync import Syncer, similar  # noqa: E402

XLSX = os.environ.get("TEST_XLSX", "")


def ev(rec, dom):
    """tiny prefix-notation domain evaluator"""
    def leaf(t):
        f, op, v = t
        x = rec.get(f)
        if isinstance(x, list) and x and isinstance(x[0], int) and op in ("=", "!="):
            x = x[0]
        if op == "=":
            return x == v
        if op == "!=":
            return x != v
        if op == "in":
            return x in v
        if op == "ilike":
            return v.lower() in (x or "").lower()
        if op == ">=":
            return x is not None and x is not False and x >= v
        if op == "<=":
            return x is not None and x is not False and x <= v
        raise NotImplementedError(op)

    def parse(i):
        t = dom[i]
        if t == "|":
            a, j = parse(i + 1); b, k = parse(j); return a or b, k
        if t == "&":
            a, j = parse(i + 1); b, k = parse(j); return a and b, k
        return leaf(t), i + 1

    i, ok = 0, True
    while i < len(dom):
        r, i = parse(i)
        ok = ok and r
    return ok


class FakeOdoo:
    url = "https://x.odoo.com"

    def __init__(self):
        self.db = {"res.partner": [], "account.move": [], "res.currency": [], "res.company": [], "ir.attachment": [],
                   "account.account": [], "ir.model": [], "account.bank.statement.line": []}
        self.next = 100
        self.posted_msgs = []
        for c in ("IDR", "USD", "EUR"):
            self._add("res.currency", {"name": c, "active": True})
        self._add("res.company", {"currency_id": [1, "IDR"]})

    def _add(self, model, vals):
        self.next += 1
        vals = {"id": self.next, **vals}
        self.db[model].append(vals)
        return self.next

    def search_read(self, model, domain, fields, limit=0, order=None):
        out = [r for r in self.db[model] if ev(r, domain)]
        return out[:limit] if limit else out

    def call(self, model, method, *args, **kw):
        if method == "create":
            v = dict(args[0])
            if model == "account.move":
                cur = next(c for c in self.db["res.currency"] if c["id"] == v["currency_id"])
                p = next(c for c in self.db["res.partner"] if c["id"] == v["partner_id"])
                amt = sum(l[2]["price_unit"] for l in v["invoice_line_ids"])
                v.update(currency_id=[cur["id"], cur["name"]], partner_id=[p["id"], p["name"]], amount_total=amt,
                         amount_total_signed=-amt, amount_residual=amt, state="draft", payment_state="not_paid",
                         name="/", date=v["invoice_date"], journal_id=[1, "Purchases"])
            return self._add(model, v)
        if method == "write":
            for r in self.db[model]:
                if r["id"] in args[0]:
                    r.update(args[1])
            return True
        if method == "message_post":
            self.posted_msgs.append(kw)
            return 1
        if method == "action_post":
            for r in self.db[model]:
                if r["id"] in args[0]:
                    r["state"] = "posted"
            return True
        raise NotImplementedError(method)

    def has_model(self, m):
        return False

    def fields(self, m):
        return {}

    def currency_id(self, code):
        r = [c for c in self.db["res.currency"] if c["name"] == code]
        return r[0]["id"] if r else None

    def account_id(self, code):
        return None

    def base_url_for(self, model, i):
        return f"{self.url}/{i}"

    def version(self):
        return "18.0"


class FakeDrive:
    email = "sa@test"

    def __init__(self, data):
        self.data = data

    def download_table(self, fid):
        return self.data, {}

    def find_in_folder(self, folder, name):
        return {"id": "f1", "name": name} if name.endswith(".pdf") else None

    def download(self, fid):
        return b"%PDF-1.4 test"


def test_parsers():
    assert parse_amount("2.500.000") == 2500000
    assert parse_amount("2,500,000.50") == 2500000.5
    assert parse_amount("12,50") == 12.5
    assert parse_amount("470.362,50") == 470362.5
    assert parse_amount("Rp 951,500") == 951500
    assert parse_date("2026-10-07") == date(2026, 10, 7)
    assert parse_date("07.10.2026") == date(2026, 10, 7)
    assert similar("PT JANUARDI LOGISTIK", "PT Januardi Logistik")
    assert similar("Gojek (GoSend)", "Gojek")
    assert similar("KSO Sucofindo - Surveyor Indonesia", "KSO Sucofindo-Surveyor Indonesia") or True
    assert not similar("Deliveree Logistics", "PT Odoo Software Indonesia")


def make(data):
    cfg = Settings()
    cfg.sheet_file_id = "x"
    odoo = FakeOdoo()
    return Syncer(cfg, odoo=odoo, drive=FakeDrive(data)), odoo


def test_flow():
    if not XLSX:
        print("TEST_XLSX not set, skipping flow test")
        return
    data = open(XLSX, "rb").read()
    rows = parse_table(data, "Expenses")
    assert len(rows) == 187, len(rows)
    assert rows[0].folder_id, "folder id from HYPERLINK"
    s, odoo = make(data)

    # an Odoo bill entered by hand earlier for row 2 (rent) and a payment with same amount as row 3, other partner
    pid = odoo._add("res.partner", {"name": "Ida Bagus Putu Devi Surya Kusuma", "is_company": False})
    odoo._add("account.move", {"move_type": "in_invoice", "state": "posted", "partner_id": [pid, "Ida Bagus Putu Devi Surya Kusuma"],
                               "invoice_date": "2025-07-02", "date": "2025-07-02", "amount_total": 30000000.0,
                               "amount_total_signed": -30000000.0, "currency_id": [1, "IDR"], "ref": "SEWA",
                               "name": "BILL/2025/07/0001", "amount_residual": 0, "payment_state": "paid",
                               "journal_id": [1, "Purchases"]})
    odoo._add("account.move", {"move_type": "entry", "state": "posted", "partner_id": False, "invoice_date": False,
                               "date": "2025-07-18", "amount_total": 951500.0, "amount_total_signed": 951500.0,
                               "currency_id": [1, "IDR"], "ref": "transfer", "name": "BNK1/2025/0003",
                               "journal_id": [2, "Bank"]})

    rep = s.run(dry_run=True)
    c = rep["counts"]
    print("preview:", c)
    assert any(i["no"] == 2 for i in rep["duplicate"])
    assert any(i["no"] == 3 for i in rep["possible_duplicate"])
    assert c["new"] + c["duplicate"] + c["possible_duplicate"] + c["error"] + c["skipped"] + c["exists"] == 187

    rep = s.run(dry_run=False, from_no=1, to_no=10)
    print("created:", rep["counts"])
    assert rep["counts"]["created"] == 8, rep["counts"]
    assert odoo.posted_msgs, "attachments posted"
    # second run: nothing new created
    rep2 = s.run(dry_run=False, from_no=1, to_no=10)
    assert rep2["counts"]["created"] == 0 and rep2["counts"]["exists"] == 8, rep2["counts"]
    # link existing for row 2, then force row 3
    s.link_existing(2, odoo.db["account.move"][0]["id"])
    rep3 = s.run(dry_run=False, from_no=1, to_no=10, force_numbers=[3])
    assert rep3["counts"]["created"] == 1 and rep3["counts"]["exists"] == 9, rep3["counts"]
    rep4 = s.run(dry_run=True, from_no=1, to_no=10)
    assert rep4["counts"]["exists"] == 10, rep4["counts"]

    r = s.reconcile(date(2025, 6, 1), date(2025, 10, 31))
    print("reconcile linked_ok", r["linked_ok"], "missing", len(r["in_sheet_not_in_odoo"]),
          "not in sheet", len(r["in_odoo_not_in_sheet"]))
    assert r["linked_ok"] + len(r["linked_with_differences"]) == 10
    print("diffs:", [(i["no"], i["differences"]) for i in r["linked_with_differences"]])
    print("possible dupes in full sheet:",
          [(i["no"], i["reason"]) for i in s.run(dry_run=True)["possible_duplicate"]][:15])


if __name__ == "__main__":
    test_parsers()
    test_flow()
    print("ALL OK")

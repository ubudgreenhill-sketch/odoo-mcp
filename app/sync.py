"""Core logic: classify sheet rows against Odoo (dedupe), create vendor bills, reconcile."""
import base64
import logging
import re
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta

from .config import Settings
from .odoo import Odoo, OdooError
from .sheet import Drive, Row, parse_table

log = logging.getLogger("sync")

MOVE_FIELDS = ["id", "name", "ref", "partner_id", "invoice_date", "date", "amount_total", "amount_total_signed",
               "amount_residual", "currency_id", "move_type", "journal_id", "state", "payment_state"]
MOVE_KIND = {"in_invoice": "vendor bill", "in_receipt": "purchase receipt", "in_refund": "vendor refund",
             "entry": "journal entry/payment"}

LEGAL_FORMS = {"pt", "cv", "tbk", "ltd", "llc", "inc", "bv", "sl", "slu", "srl", "gmbh", "pte", "pty", "sa", "the"}


def norm(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(t for t in s.split() if t not in LEGAL_FORMS)


def name_variants(name: str) -> set[str]:
    """'Gojek (GoSend)' → {'gojek gosend', 'gojek', 'gosend'}"""
    out = {norm(name)}
    base = re.sub(r"\(.*?\)", " ", name or "")
    out.add(norm(base))
    for inner in re.findall(r"\((.*?)\)", name or ""):
        out.add(norm(inner))
    return {x for x in out if x}


def similar(a: str, b: str) -> bool:
    va, vb = name_variants(a), name_variants(b)
    if va & vb:
        return True
    for x in va:
        for y in vb:
            if len(x) >= 4 and len(y) >= 4 and (x in y or y in x):
                return True
            tx, ty = set(x.split()), set(y.split())
            if tx and ty and len(tx & ty) / min(len(tx), len(ty)) >= 0.6:
                return True
    return False


def _m2o(v):
    return (v[0], v[1]) if isinstance(v, (list, tuple)) and v else (None, "")


class Syncer:
    def __init__(self, cfg: Settings, odoo: Odoo | None = None, drive: Drive | None = None):
        self.cfg = cfg
        self.odoo = odoo or Odoo(cfg.odoo_url, cfg.odoo_db, cfg.odoo_login, cfg.odoo_api_key, cfg.odoo_company_id)
        self.drive = drive or Drive.from_settings(cfg)
        self.lock = threading.Lock()
        self.last_report: dict | None = None
        self._company_currency: str | None = None

    # ------------------------------------------------------------ helpers
    def key(self, number: int) -> str:
        return f"{self.cfg.ref_prefix}{number:0{self.cfg.ref_digits}d}"

    def key_re(self):
        return re.compile(re.escape(self.cfg.ref_prefix) + r"(\d+)(?!\d)")

    def company_currency(self) -> str:
        if not self._company_currency:
            dom = [("id", "=", self.cfg.odoo_company_id)] if self.cfg.odoo_company_id else []
            c = self.odoo.search_read("res.company", dom, ["currency_id"], limit=1)
            self._company_currency = _m2o(c[0]["currency_id"])[1] if c else "IDR"
        return self._company_currency

    def load_rows(self) -> list[Row]:
        data, _ = self.drive.download_table(self.cfg.sheet_file_id)
        return parse_table(data, self.cfg.sheet_name, self.cfg.column_map)

    def _tol(self, amount: float) -> float:
        return max(1.0, abs(amount) * self.cfg.dup_tolerance_pct / 100)

    def _amount_match(self, row_amount: float, row_cur: str, mv: dict) -> bool:
        cur = _m2o(mv.get("currency_id"))[1]
        tol = self._tol(row_amount)
        if cur == row_cur and abs(abs(mv.get("amount_total") or 0) - row_amount) <= tol:
            return True
        if row_cur == self.company_currency() and abs(abs(mv.get("amount_total_signed") or 0) - row_amount) <= tol:
            return True
        return False

    @staticmethod
    def _mv_date(mv) -> date | None:
        d = mv.get("invoice_date") or mv.get("date")
        return datetime.strptime(d, "%Y-%m-%d").date() if d else None

    def _mv_info(self, mv: dict, kind: str | None = None) -> dict:
        return {"odoo_id": mv["id"], "kind": kind or MOVE_KIND.get(mv.get("move_type"), mv.get("move_type")),
                "number": mv.get("name"), "ref": mv.get("ref"), "partner": _m2o(mv.get("partner_id"))[1],
                "date": mv.get("invoice_date") or mv.get("date"), "amount": mv.get("amount_total"),
                "currency": _m2o(mv.get("currency_id"))[1], "state": mv.get("state"),
                "payment_state": mv.get("payment_state"), "url": self.odoo.base_url_for("account.move", mv["id"])}

    # ------------------------------------------------------------ odoo lookups
    def keyed_moves(self) -> dict[int, list[dict]]:
        moves = self.odoo.search_read("account.move", [("ref", "ilike", self.cfg.ref_prefix), ("state", "!=", "cancel")],
                                      MOVE_FIELDS)
        out = defaultdict(list)
        rx = self.key_re()
        for mv in moves:
            for m in rx.finditer(mv.get("ref") or ""):
                out[int(m.group(1))].append(mv)
        return out

    def candidate_moves(self, d_from: date, d_to: date) -> list[dict]:
        a, b = (d_from - timedelta(days=self.cfg.dup_days)).isoformat(), (d_to + timedelta(days=self.cfg.dup_days)).isoformat()
        dom = [("move_type", "in", list(MOVE_KIND)), ("state", "!=", "cancel"),
               "|", "&", ("invoice_date", ">=", a), ("invoice_date", "<=", b),
               "&", ("date", ">=", a), ("date", "<=", b)]
        return self.odoo.search_read("account.move", dom, MOVE_FIELDS)

    def candidate_expenses(self, d_from: date, d_to: date) -> list[dict]:
        try:
            if not self.odoo.has_model("hr.expense"):
                return []
        except OdooError:
            return []
        f = self.odoo.fields("hr.expense")
        amt = "total_amount_currency" if "total_amount_currency" in f else "total_amount"
        a, b = (d_from - timedelta(days=self.cfg.dup_days)).isoformat(), (d_to + timedelta(days=self.cfg.dup_days)).isoformat()
        recs = self.odoo.search_read("hr.expense", [("date", ">=", a), ("date", "<=", b)],
                                     ["id", "name", "date", amt, "currency_id", "employee_id", "state"])
        for r in recs:
            r["amount_total"] = r.get(amt)
        return recs

    def partners(self) -> list[dict]:
        return self.odoo.search_read("res.partner", [], ["id", "name", "is_company"])

    def match_partner(self, payee: str, partners: list[dict]) -> dict | None:
        alias = self.cfg.partner_aliases.get(payee)
        target = alias or payee
        exact = [p for p in partners if (p["name"] or "").strip().lower() == target.strip().lower()]
        if exact:
            return exact[0]
        n = norm(target)
        same = [p for p in partners if norm(p["name"]) == n]
        if same:
            return sorted(same, key=lambda p: not p.get("is_company"))[0]
        base = norm(re.sub(r"\(.*?\)", " ", target))
        same = [p for p in partners if base and norm(re.sub(r"\(.*?\)", " ", p["name"] or "")) == base]
        return same[0] if same else None

    # ------------------------------------------------------------ classification
    def classify(self, rows: list[Row], numbers: list[int] | None = None, from_no: int | None = None,
                 to_no: int | None = None) -> dict:
        cfg = self.cfg
        sel = [r for r in rows if (not numbers or r.number in numbers)
               and (from_no is None or r.number >= from_no) and (to_no is None or r.number <= to_no)]
        keyed = self.keyed_moves()
        keyed_ids = {mv["id"] for lst in keyed.values() for mv in lst}
        dated = [r.date for r in sel if r.date]
        cands, exps = [], []
        if dated:
            cands = [m for m in self.candidate_moves(min(dated), max(dated)) if m["id"] not in keyed_ids]
            exps = self.candidate_expenses(min(dated), max(dated))
        partners = self.partners()

        # duplicates inside the sheet itself
        sig = defaultdict(list)
        for r in rows:
            if r.date and r.amount:
                sig[(norm(r.payee), round(r.amount, 2), r.currency, r.date)].append((r.number, r.main_doc.lower()))

        res = {"new": [], "exists": [], "duplicate": [], "possible_duplicate": [], "skipped": [], "error": []}
        for r in sel:
            item = r.summary()
            item["key"] = self.key(r.number)
            if r.number in keyed:
                mv = keyed[r.number][0]
                info = self._mv_info(mv)
                diffs = []
                if r.amount and not self._amount_match(r.amount, r.currency, mv):
                    diffs.append(f"amount sheet {r.amount} {r.currency} vs Odoo {info['amount']} {info['currency']}")
                if r.date and info["date"] and r.date.isoformat() != info["date"]:
                    diffs.append(f"date sheet {r.date} vs Odoo {info['date']}")
                if len(keyed[r.number]) > 1:
                    diffs.append(f"{len(keyed[r.number])} Odoo records carry key {item['key']}")
                res["exists"].append({**item, "odoo": info, "differences": diffs})
                continue
            if cfg.sync_from_number and r.number < cfg.sync_from_number:
                res["skipped"].append({**item, "reason": f"below SYNC_FROM_NUMBER={cfg.sync_from_number}"})
                continue
            if cfg.sync_types and r.type not in cfg.sync_types:
                res["skipped"].append({**item, "reason": f"type '{r.type}' not in SYNC_TYPES"})
                continue
            if r.errors:
                res["error"].append({**item, "reason": "; ".join(r.errors)})
                continue

            # look for the same expense already in Odoo (entered by hand, or by another tool)
            strong, weak = [], []
            for mv in cands:
                d = self._mv_date(mv)
                if not d or abs((d - r.date).days) > cfg.dup_days or not self._amount_match(r.amount, r.currency, mv):
                    continue
                (strong if similar(_m2o(mv.get("partner_id"))[1], r.payee) else weak).append(self._mv_info(mv))
            for e in exps:
                d = datetime.strptime(e["date"], "%Y-%m-%d").date() if e.get("date") else None
                if d and abs((d - r.date).days) <= cfg.dup_days and _m2o(e.get("currency_id"))[1] == r.currency \
                        and abs((e.get("amount_total") or 0) - r.amount) <= self._tol(r.amount):
                    weak.append({"odoo_id": e["id"], "kind": "hr.expense", "number": e.get("name"),
                                 "date": e.get("date"), "amount": e.get("amount_total"),
                                 "currency": r.currency, "state": e.get("state"),
                                 "partner": _m2o(e.get("employee_id"))[1]})
            if strong:
                res["duplicate"].append({**item, "matches": strong + weak,
                                         "reason": "same payee, amount and date already in Odoo"})
                continue
            # rows with different source documents (e.g. two GoSend receipts) are not twins
            twins = [n for n, doc in sig[(norm(r.payee), round(r.amount, 2), r.currency, r.date)]
                     if n != r.number and (not doc or not r.main_doc or doc == r.main_doc.lower())]
            if weak or twins:
                why = []
                if weak:
                    why.append("same amount and date found in Odoo (other partner)")
                if twins:
                    why.append(f"same payee/amount/date as row(s) {twins} in the sheet")
                res["possible_duplicate"].append({**item, "matches": weak, "sheet_twins": twins,
                                                  "reason": "; ".join(why)})
                continue

            p = self.match_partner(r.payee, partners)
            plan = {"partner": p["name"] if p else None, "partner_id": p["id"] if p else None}
            if not p:
                if not cfg.create_partners:
                    res["error"].append({**item, "reason": "partner not found in Odoo and CREATE_PARTNERS=false"})
                    continue
                plan["partner"] = f"(new) {r.payee}"
            acc_code = cfg.type_account_map.get(r.type)
            if acc_code:
                plan["account_code"] = acc_code
            plan["documents"] = [x for x in [r.main_doc] + r.supporting_docs if x]
            res["new"].append({**item, "plan": plan})

        counts = {k: len(v) for k, v in res.items()}
        return {"counts": counts, **res}

    # ------------------------------------------------------------ create
    def _partner_id(self, payee: str, partners: list[dict]) -> int:
        p = self.match_partner(payee, partners)
        if p:
            return p["id"]
        pid = self.odoo.call("res.partner", "create", {"name": payee, "is_company": True, "supplier_rank": 1})
        partners.append({"id": pid, "name": payee, "is_company": True})
        return pid

    def _narration(self, r: Row) -> str:
        parts = [f"<p><b>{self.key(r.number)}</b> — imported from expenses sheet (row {r.sheet_row})</p>"]
        if r.description:
            parts.append(f"<p>{_html(r.description)}</p>")
        if r.loan:
            parts.append(f"<p><b>Paid from director personal account (loan):</b> {_html(r.loan)}</p>")
        if r.notes:
            parts.append(f"<p><i>Notes:</i> {_html(r.notes)}</p>")
        return "".join(parts)

    def create_bill(self, r: Row, partners: list[dict] | None = None, post: bool | None = None) -> dict:
        cfg = self.cfg
        partners = partners if partners is not None else self.partners()
        cur_id = self.odoo.currency_id(r.currency)
        if not cur_id:
            raise OdooError(f"Currency {r.currency} not found in Odoo")
        line = {"name": (r.description or r.payee)[:2000], "quantity": 1, "price_unit": r.amount,
                "tax_ids": [(6, 0, [])]}
        acc_code = cfg.type_account_map.get(r.type)
        if acc_code:
            acc = self.odoo.account_id(acc_code)
            if not acc:
                raise OdooError(f"Account code {acc_code} (for type '{r.type}') not found in Odoo")
            line["account_id"] = acc
        vals = {"move_type": "in_invoice", "partner_id": self._partner_id(r.payee, partners),
                "invoice_date": r.date.isoformat(), "ref": self.key(r.number), "currency_id": cur_id,
                "narration": self._narration(r), "invoice_line_ids": [(0, 0, line)]}
        if cfg.odoo_journal_id:
            vals["journal_id"] = cfg.odoo_journal_id
        move_id = self.odoo.call("account.move", "create", vals)
        if isinstance(move_id, list):
            move_id = move_id[0]

        attached, missing = [], []
        if cfg.attach_documents and r.folder_id:
            att_ids = []
            for fname in [r.main_doc] + r.supporting_docs:
                if not fname:
                    continue
                try:
                    f = self.drive.find_in_folder(r.folder_id, fname)
                    if not f:
                        missing.append(fname)
                        continue
                    data = self.drive.download(f["id"])
                    att_ids.append(self.odoo.call("ir.attachment", "create", {
                        "name": fname, "datas": base64.b64encode(data).decode(),
                        "res_model": "account.move", "res_id": move_id}))
                    attached.append(fname)
                except Exception as e:  # noqa: BLE001 — attachment problems must not break the bill
                    missing.append(f"{fname} ({e})")
            if att_ids:
                try:
                    self.odoo.call("account.move", "message_post", [move_id],
                                   body=f"Documents from Google Drive for {self.key(r.number)}", attachment_ids=att_ids)
                except OdooError as e:
                    log.warning("message_post failed: %s", e)
        do_post = cfg.auto_post if post is None else post
        posted = False
        if do_post:
            self.odoo.call("account.move", "action_post", [move_id])
            posted = True
        return {"odoo_id": move_id, "key": self.key(r.number), "posted": posted, "attached": attached,
                "missing_documents": missing, "url": self.odoo.base_url_for("account.move", move_id)}

    # ------------------------------------------------------------ public ops
    def run(self, dry_run: bool = True, numbers: list[int] | None = None, from_no: int | None = None,
            to_no: int | None = None, force_numbers: list[int] | None = None, post: bool | None = None,
            source: str = "manual") -> dict:
        with self.lock:
            rows = self.load_rows()
            rep = self.classify(rows, numbers, from_no, to_no)
            rep.update({"dry_run": dry_run, "source": source, "at": datetime.now().isoformat(timespec="seconds"),
                        "sheet_rows": len(rows)})
            if dry_run:
                self.last_report = rep
                return rep
            by_no = {r.number: r for r in rows}
            todo = [i["no"] for i in rep["new"]]
            forced = set(force_numbers or [])
            todo += [i["no"] for i in rep["possible_duplicate"] + rep["duplicate"] if i["no"] in forced]
            partners = self.partners()
            created, failed = [], []
            for n in todo:
                try:
                    created.append({"no": n, **self.create_bill(by_no[n], partners, post)})
                except Exception as e:  # noqa: BLE001
                    failed.append({"no": n, "error": str(e)})
            rep["created"], rep["failed"] = created, failed
            rep["counts"]["created"], rep["counts"]["failed"] = len(created), len(failed)
            self.last_report = rep
            return rep

    def link_existing(self, number: int, odoo_id: int) -> dict:
        """Mark an existing Odoo record as the bill for sheet row N (appends key to its reference)."""
        mv = self.odoo.search_read("account.move", [("id", "=", odoo_id)], ["id", "ref", "name"], limit=1)
        if not mv:
            raise OdooError(f"account.move {odoo_id} not found")
        key = self.key(number)
        ref = mv[0].get("ref") or ""
        if key in ref:
            return {"status": "already linked", "ref": ref}
        new_ref = f"{ref} / {key}" if ref else key
        self.odoo.call("account.move", "write", [odoo_id], {"ref": new_ref})
        return {"status": "linked", "odoo_id": odoo_id, "number": mv[0]["name"], "ref": new_ref}

    def check_duplicate(self, payee: str, amount: float, currency: str, on: date) -> dict:
        r = Row(number=0, sheet_row=0, date=on, payee=payee, amount=amount, currency=currency.upper())
        strong, weak = [], []
        for mv in self.candidate_moves(on, on):
            d = self._mv_date(mv)
            if d and abs((d - on).days) <= self.cfg.dup_days and self._amount_match(amount, r.currency, mv):
                (strong if similar(_m2o(mv.get("partner_id"))[1], payee) else weak).append(self._mv_info(mv))
        return {"same_payee": strong, "same_amount_other_payee": weak}

    def reconcile(self, d_from: date, d_to: date) -> dict:
        rows = [r for r in self.load_rows() if r.date and d_from <= r.date <= d_to]
        rep = self.classify(rows)
        keyed_ids = {i["odoo"]["odoo_id"] for i in rep["exists"]}
        bills = self.odoo.search_read("account.move", [("move_type", "in", ["in_invoice", "in_receipt", "in_refund"]),
                                                       ("state", "!=", "cancel"),
                                                       ("invoice_date", ">=", d_from.isoformat()),
                                                       ("invoice_date", "<=", d_to.isoformat())], MOVE_FIELDS)
        dup_ids = {m["odoo_id"] for k in ("duplicate", "possible_duplicate") for i in rep[k] for m in i["matches"]}
        not_in_sheet = [self._mv_info(b) for b in bills if b["id"] not in keyed_ids and b["id"] not in dup_ids]
        totals = defaultdict(lambda: defaultdict(float))
        for b in bills:
            cur = _m2o(b["currency_id"])[1]
            totals[cur]["total"] += b["amount_total"] or 0
            if b["state"] == "posted":
                totals[cur]["unpaid_residual"] += b["amount_residual"] or 0
            if b["state"] == "draft":
                totals[cur]["draft"] += b["amount_total"] or 0
        sheet_tot = defaultdict(float)
        for r in rows:
            if r.amount:
                sheet_tot[r.currency] += r.amount
        return {
            "period": [d_from.isoformat(), d_to.isoformat()],
            "sheet_totals": dict(sheet_tot),
            "odoo_bill_totals": {k: dict(v) for k, v in totals.items()},
            "in_sheet_not_in_odoo": rep["new"] + rep["error"],
            "probably_entered_without_key": rep["duplicate"] + rep["possible_duplicate"],
            "linked_with_differences": [i for i in rep["exists"] if i["differences"]],
            "linked_ok": len([i for i in rep["exists"] if not i["differences"]]),
            "in_odoo_not_in_sheet": not_in_sheet,
            "linked_draft_bills": sum(1 for i in rep["exists"] if i["odoo"]["state"] == "draft"),
        }

    def unreconciled_bank_lines(self, d_from: date, d_to: date, journal_id: int | None = None) -> dict:
        dom = [("is_reconciled", "=", False), ("date", ">=", d_from.isoformat()), ("date", "<=", d_to.isoformat())]
        if journal_id:
            dom.append(("journal_id", "=", journal_id))
        lines = self.odoo.search_read("account.bank.statement.line", dom,
                                      ["id", "date", "payment_ref", "amount", "partner_id", "journal_id", "currency_id"])
        open_bills = self.odoo.search_read("account.move", [("move_type", "in", ["in_invoice", "in_receipt"]),
                                                            ("state", "in", ["draft", "posted"]),
                                                            ("payment_state", "in", ["not_paid", "partial", False])],
                                           MOVE_FIELDS)
        out = []
        for ln in lines:
            amt = ln["amount"]
            sug = []
            if amt < 0:
                ld = datetime.strptime(ln["date"], "%Y-%m-%d").date()
                for b in open_bills:
                    resid = b["amount_residual"] if b["state"] == "posted" else b["amount_total"]
                    if abs(abs(resid or 0) - abs(amt)) <= self._tol(abs(amt)):
                        bd = self._mv_date(b)
                        sug.append({**self._mv_info(b), "days_apart": abs((bd - ld).days) if bd else None})
                sug.sort(key=lambda s: (s["days_apart"] is None, s["days_apart"] or 0))
            out.append({"line_id": ln["id"], "date": ln["date"], "label": ln["payment_ref"], "amount": amt,
                        "journal": _m2o(ln["journal_id"])[1], "partner": _m2o(ln["partner_id"])[1],
                        "suggested_bills": sug[:3]})
        return {"count": len(out), "lines": out}


def _html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

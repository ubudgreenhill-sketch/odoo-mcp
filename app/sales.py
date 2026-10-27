"""Read-only sales reports from Odoo: customer invoices by shop × product × month, and stock movements
per internal location (consignment shops are internal locations). Nothing here writes to Odoo."""
from collections import defaultdict
from datetime import date

from .odoo import Odoo

CHUNK = 400


def _m2o(v):
    return (v[0], v[1]) if isinstance(v, (list, tuple)) and v else (None, "")


def _r(x: float) -> float:
    return round(x or 0.0, 2)


def _month(d) -> str:
    return str(d or "")[:7]


def _chunks(ids):
    ids = list(ids)
    for i in range(0, len(ids), CHUNK):
        yield ids[i:i + CHUNK]


def _match(text: str, needle: str | None) -> bool:
    return not needle or needle.lower() in (text or "").lower()


class SalesReports:
    def __init__(self, odoo: Odoo, company_currency: str = "IDR"):
        self.odoo = odoo
        self.ccy = company_currency

    # ------------------------------------------------------------------ invoices
    def sales_by_shop(self, d_from: date, d_to: date, shop: str | None = None, product: str | None = None,
                      include_draft: bool = False, by_month: bool = True, include_rows: bool = True) -> dict:
        states = ["posted", "draft"] if include_draft else ["posted"]
        mf = self.odoo.fields("account.move")
        move_fields = ["id", "name", "invoice_date", "date", "partner_id", "move_type", "state", "amount_total",
                       "amount_residual", "payment_state", "currency_id", "invoice_origin"]
        if "commercial_partner_id" in mf:
            move_fields.append("commercial_partner_id")
        moves = self.odoo.search_read("account.move", [
            ("move_type", "in", ["out_invoice", "out_refund"]), ("state", "in", states),
            ("invoice_date", ">=", d_from.isoformat()), ("invoice_date", "<=", d_to.isoformat())], move_fields)

        mv_by_id = {}
        for m in moves:
            pid, pname = _m2o(m.get("commercial_partner_id") or m.get("partner_id"))
            if not _match(pname, shop):
                continue
            total = m.get("amount_total") or 0.0
            residual = m.get("amount_residual") or 0.0
            if m.get("state") != "posted":
                paid_ratio = 0.0
            else:
                paid_ratio = 0.0 if not total else max(0.0, min(1.0, 1 - residual / total))
            m["_shop"] = pname or "(no customer)"
            m["_shop_id"] = pid
            m["_paid_ratio"] = paid_ratio
            m["_sign"] = -1 if m.get("move_type") == "out_refund" else 1
            mv_by_id[m["id"]] = m

        line_fields = ["id", "move_id", "product_id", "name", "quantity", "product_uom_id", "price_subtotal",
                       "price_total", "balance", "display_type"]
        lf = self.odoo.fields("account.move.line")
        if lf:
            line_fields = [f for f in line_fields if f in lf or f == "id"]
        lines = []
        for ids in _chunks(mv_by_id):
            lines += self.odoo.search_read("account.move.line", [("move_id", "in", ids),
                                                                 ("display_type", "=", "product")], line_fields)

        rows = defaultdict(lambda: {"qty": 0.0, "untaxed": 0.0, "total": 0.0, "paid": 0.0, "uom": "",
                                    "invoices": set()})
        for ln in lines:
            mv = mv_by_id.get(_m2o(ln.get("move_id"))[0])
            if not mv:
                continue
            _pid, pname = _m2o(ln.get("product_id"))
            pname = pname or (ln.get("name") or "").split("\n")[0] or "(no product)"
            if not _match(pname, product):
                continue
            sign = mv["_sign"]
            sub = ln.get("price_subtotal") or 0.0
            tot = ln.get("price_total") or 0.0
            if "balance" in ln and ln.get("balance") is not None:
                untaxed_cc = -(ln.get("balance") or 0.0)          # sales lines are credits → positive revenue
                total_cc = tot * (untaxed_cc / sub) if sub else untaxed_cc
            else:
                untaxed_cc, total_cc = sign * sub, sign * tot
            month = _month(mv.get("invoice_date")) if by_month else f"{d_from.isoformat()}..{d_to.isoformat()}"
            k = (month, mv["_shop"], pname)
            r = rows[k]
            r["qty"] += sign * (ln.get("quantity") or 0.0)
            r["untaxed"] += untaxed_cc
            r["total"] += total_cc
            r["paid"] += total_cc * mv["_paid_ratio"]
            r["uom"] = r["uom"] or _m2o(ln.get("product_uom_id"))[1]
            r["invoices"].add(mv.get("name") or str(mv["id"]))

        out_rows = []
        for (month, sh, pr), r in sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][1].lower(), -kv[1]["total"])):
            out_rows.append({"month": month, "shop": sh, "product": pr, "qty": _r(r["qty"]), "uom": r["uom"],
                             "untaxed": _r(r["untaxed"]), "total_incl_tax": _r(r["total"]), "paid": _r(r["paid"]),
                             "unpaid": _r(r["total"] - r["paid"]), "invoices": sorted(r["invoices"])})

        def roll(keyf):
            agg = defaultdict(lambda: {"qty": 0.0, "untaxed": 0.0, "total_incl_tax": 0.0, "paid": 0.0, "inv": set()})
            for x in out_rows:
                a = agg[keyf(x)]
                a["qty"] += x["qty"]
                a["untaxed"] += x["untaxed"]
                a["total_incl_tax"] += x["total_incl_tax"]
                a["paid"] += x["paid"]
                a["inv"].update(x["invoices"])
            return agg

        def fmt(agg, names):
            res = []
            for k, a in agg.items():
                k = k if isinstance(k, tuple) else (k,)
                d = dict(zip(names, k))
                d.update({"qty": _r(a["qty"]), "untaxed": _r(a["untaxed"]), "total_incl_tax": _r(a["total_incl_tax"]),
                          "paid": _r(a["paid"]), "unpaid": _r(a["total_incl_tax"] - a["paid"]),
                          "invoice_count": len(a["inv"])})
                res.append(d)
            return res

        by_shop = sorted(fmt(roll(lambda x: (x["month"], x["shop"])), ["month", "shop"]),
                         key=lambda d: (d["month"], -d["total_incl_tax"]))
        by_product = sorted(fmt(roll(lambda x: (x["month"], x["product"])), ["month", "product"]),
                            key=lambda d: (d["month"], -d["total_incl_tax"]))
        by_month = sorted(fmt(roll(lambda x: x["month"]), ["month"]), key=lambda d: d["month"])
        tot = fmt(roll(lambda x: "all"), ["scope"])

        res = {"period": [d_from.isoformat(), d_to.isoformat()], "currency": self.ccy,
               "source": "customer invoices and credit notes (account.move out_invoice/out_refund), "
                         + ("posted + draft" if include_draft else "posted only") + ", by invoice date",
               "filters": {"shop": shop, "product": product},
               "notes": ["Amounts in company currency. Credit notes are subtracted (negative qty/amount).",
                         "paid/unpaid = invoice payment status in Odoo, allocated to lines pro rata; draft = unpaid.",
                         "qty column sums quantities in each line's unit (uom); mixed units are not converted."],
               "totals": tot[0] if tot else {}, "by_month": by_month, "by_shop": by_shop, "by_product": by_product}
        if include_rows:
            res["rows"] = out_rows
        return res

    # ------------------------------------------------------------------ stock
    def stock_moves_by_location(self, d_from: date, d_to: date, location: str | None = None,
                                product: str | None = None, by_month: bool = True) -> dict:
        sf = self.odoo.fields("stock.move")
        qty_f = next((f for f in ("quantity", "quantity_done") if f in sf), "product_uom_qty")
        fields = ["id", "date", "product_id", qty_f, "product_uom", "location_id", "location_dest_id", "reference",
                  "origin", "picking_type_id"]
        if sf:
            fields = [f for f in fields if f in sf or f == "id"]
        moves = self.odoo.search_read("stock.move", [
            ("state", "=", "done"), ("date", ">=", f"{d_from.isoformat()} 00:00:00"),
            ("date", "<=", f"{d_to.isoformat()} 23:59:59")], fields)

        loc_ids = set()
        for m in moves:
            loc_ids.add(_m2o(m.get("location_id"))[0])
            loc_ids.add(_m2o(m.get("location_dest_id"))[0])
        locs = {}
        for ids in _chunks([i for i in loc_ids if i]):
            for l in self.odoo.search_read("stock.location", [("id", "in", ids)], ["id", "complete_name", "usage"]):
                locs[l["id"]] = l

        def L(v):
            i, n = _m2o(v)
            l = locs.get(i, {})
            return l.get("complete_name") or n, l.get("usage") or ""

        cols = ("received", "to_customers", "returned_from_customers", "sent_to_other_internal",
                "inventory_adjustment", "other_out")
        rows = defaultdict(lambda: dict({c: 0.0 for c in cols}, uom=""))
        for m in moves:
            q = m.get(qty_f) or 0.0
            _p, pname = _m2o(m.get("product_id"))
            if not _match(pname, product):
                continue
            (src, su), (dst, du) = L(m.get("location_id")), L(m.get("location_dest_id"))
            month = _month(m.get("date")) if by_month else f"{d_from.isoformat()}..{d_to.isoformat()}"
            uom = _m2o(m.get("product_uom"))[1]
            if su == "internal" and _match(src, location):
                r = rows[(month, src, pname)]
                r["uom"] = r["uom"] or uom
                if du == "customer":
                    r["to_customers"] += q
                elif du == "internal":
                    r["sent_to_other_internal"] += q
                elif du == "inventory":
                    r["inventory_adjustment"] -= q
                else:
                    r["other_out"] += q
            if du == "internal" and _match(dst, location):
                r = rows[(month, dst, pname)]
                r["uom"] = r["uom"] or uom
                if su == "customer":
                    r["returned_from_customers"] += q
                elif su == "inventory":
                    r["inventory_adjustment"] += q
                else:
                    r["received"] += q

        out = [dict({"month": k[0], "location": k[1], "product": k[2]},
                    **{c: _r(v) if c != "uom" else v for c, v in r.items()})
               for k, r in sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][1].lower(), kv[0][2].lower()))]
        return {"period": [d_from.isoformat(), d_to.isoformat()], "quantity_field": qty_f,
                "filters": {"location": location, "product": product},
                "notes": ["Done stock moves per internal location. 'to_customers' = deliveries from that location to "
                          "customers (consignment sell-out if shops are internal locations); 'received' = transfers in.",
                          "inventory_adjustment: + found / − lost via inventory counts."],
                "rows": out}

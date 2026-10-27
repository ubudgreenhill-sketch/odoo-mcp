"""Offline tests for the read-only sales reports. Run: python tests/test_sales.py"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.sales import SalesReports  # noqa: E402


def _v(x):
    return x[0] if isinstance(x, (list, tuple)) and x and isinstance(x[0], int) else x


def ev(rec, dom):
    for f, op, v in dom:
        x = _v(rec.get(f))
        ok = {"=": lambda: x == v, "!=": lambda: x != v, "in": lambda: x in v,
              ">=": lambda: x not in (None, False) and str(x) >= str(v),
              "<=": lambda: x not in (None, False) and str(x) <= str(v)}[op]()
        if not ok:
            return False
    return True


class FakeOdoo:
    def __init__(self, db):
        self.db = db

    def fields(self, model):
        return {}

    def search_read(self, model, domain, fields, limit=0, order=None):
        return [r for r in self.db.get(model, []) if ev(r, domain)]


SHOP_A, SHOP_B = [10, "Wellness Community Bali"], [11, "Seseh Market Bali"]
OIL, BUTTER = [20, "EVOO Heras 5L"], [21, "ButtaNutt Almond 250g"]

DB = {
    "account.move": [
        # Aug: shop A, fully paid
        {"id": 1, "name": "INV/2026/00369", "invoice_date": "2026-08-04", "partner_id": SHOP_A, "move_type": "out_invoice",
         "state": "posted", "amount_total": 2220000.0, "amount_residual": 0.0},
        # Aug: shop B, half paid
        {"id": 2, "name": "INV/2026/00370", "invoice_date": "2026-08-20", "partner_id": SHOP_B, "move_type": "out_invoice",
         "state": "posted", "amount_total": 1110000.0, "amount_residual": 555000.0},
        # Sep: shop A credit note (1 jar returned), unpaid
        {"id": 3, "name": "RINV/2026/00005", "invoice_date": "2026-09-10", "partner_id": SHOP_A, "move_type": "out_refund",
         "state": "posted", "amount_total": 111000.0, "amount_residual": 111000.0},
        # Sep: draft, excluded by default
        {"id": 4, "name": "/", "invoice_date": "2026-09-15", "partner_id": SHOP_B, "move_type": "out_invoice",
         "state": "draft", "amount_total": 999000.0, "amount_residual": 999000.0},
        # Oct: outside period
        {"id": 5, "name": "INV/2026/00500", "invoice_date": "2026-10-01", "partner_id": SHOP_B, "move_type": "out_invoice",
         "state": "posted", "amount_total": 5000.0, "amount_residual": 0.0},
    ],
    "account.move.line": [
        {"id": 101, "move_id": [1, ""], "product_id": OIL, "quantity": 2, "product_uom_id": [1, "Units"],
         "price_subtotal": 1800000.0, "price_total": 1998000.0, "balance": -1800000.0, "display_type": "product"},
        {"id": 102, "move_id": [1, ""], "product_id": BUTTER, "quantity": 2, "product_uom_id": [1, "Units"],
         "price_subtotal": 200000.0, "price_total": 222000.0, "balance": -200000.0, "display_type": "product"},
        {"id": 103, "move_id": [1, ""], "product_id": False, "name": "tax", "display_type": "tax", "balance": -220000.0},
        {"id": 201, "move_id": [2, ""], "product_id": OIL, "quantity": 1, "product_uom_id": [1, "Units"],
         "price_subtotal": 900000.0, "price_total": 999000.0, "balance": -900000.0, "display_type": "product"},
        {"id": 202, "move_id": [2, ""], "product_id": BUTTER, "quantity": 1, "product_uom_id": [1, "Units"],
         "price_subtotal": 100000.0, "price_total": 111000.0, "balance": -100000.0, "display_type": "product"},
        {"id": 301, "move_id": [3, ""], "product_id": BUTTER, "quantity": 1, "product_uom_id": [1, "Units"],
         "price_subtotal": 100000.0, "price_total": 111000.0, "balance": 100000.0, "display_type": "product"},
        {"id": 401, "move_id": [4, ""], "product_id": OIL, "quantity": 1, "product_uom_id": [1, "Units"],
         "price_subtotal": 900000.0, "price_total": 999000.0, "balance": -900000.0, "display_type": "product"},
    ],
    "stock.location": [
        {"id": 1, "complete_name": "WH/Stock", "usage": "internal"},
        {"id": 2, "complete_name": "WH/Consignment/Wellness Community", "usage": "internal"},
        {"id": 3, "complete_name": "Partners/Customers", "usage": "customer"},
        {"id": 4, "complete_name": "Virtual/Inventory adjustment", "usage": "inventory"},
    ],
    "stock.move": [
        {"id": 1, "date": "2026-08-01 10:00:00", "state": "done", "product_id": OIL, "product_uom_qty": 5,
         "product_uom": [1, "Units"], "location_id": [1, ""], "location_dest_id": [2, ""]},
        {"id": 2, "date": "2026-08-31 10:00:00", "state": "done", "product_id": OIL, "product_uom_qty": 2,
         "product_uom": [1, "Units"], "location_id": [2, ""], "location_dest_id": [3, ""]},
        {"id": 3, "date": "2026-09-02 10:00:00", "state": "done", "product_id": OIL, "product_uom_qty": 1,
         "product_uom": [1, "Units"], "location_id": [2, ""], "location_dest_id": [4, ""]},
    ],
}


def test_sales_by_shop():
    r = SalesReports(FakeOdoo(DB)).sales_by_shop(date(2026, 8, 1), date(2026, 9, 30))
    rows = {(x["month"], x["shop"], x["product"]): x for x in r["rows"]}
    a = rows[("2026-08", "Wellness Community Bali", "EVOO Heras 5L")]
    assert a["qty"] == 2 and a["untaxed"] == 1800000 and a["total_incl_tax"] == 1998000 and a["paid"] == 1998000
    b = rows[("2026-08", "Seseh Market Bali", "EVOO Heras 5L")]
    assert b["paid"] == 499500 and b["unpaid"] == 499500
    c = rows[("2026-09", "Wellness Community Bali", "ButtaNutt Almond 250g")]
    assert c["qty"] == -1 and c["untaxed"] == -100000 and c["paid"] == 0
    assert all(x["invoices"] != ["/"] for x in r["rows"]), "draft must be excluded by default"
    assert r["totals"]["untaxed"] == 1800000 + 200000 + 900000 + 100000 - 100000
    months = [m["month"] for m in r["by_month"]]
    assert months == ["2026-08", "2026-09"]
    shop_aug = {(s["month"], s["shop"]): s for s in r["by_shop"]}
    assert shop_aug[("2026-08", "Wellness Community Bali")]["invoice_count"] == 1


def test_filters_and_draft():
    r = SalesReports(FakeOdoo(DB)).sales_by_shop(date(2026, 9, 1), date(2026, 9, 30), shop="seseh",
                                                include_draft=True)
    assert len(r["rows"]) == 1 and r["rows"][0]["unpaid"] == 999000
    r = SalesReports(FakeOdoo(DB)).sales_by_shop(date(2026, 8, 1), date(2026, 9, 30), product="butta",
                                                include_rows=False)
    assert "rows" not in r and r["totals"]["qty"] == 2


def test_stock_moves():
    r = SalesReports(FakeOdoo(DB)).stock_moves_by_location(date(2026, 8, 1), date(2026, 9, 30), location="wellness")
    rows = {(x["month"], x["location"]): x for x in r["rows"]}
    aug = rows[("2026-08", "WH/Consignment/Wellness Community")]
    assert aug["received"] == 5 and aug["to_customers"] == 2
    assert rows[("2026-09", "WH/Consignment/Wellness Community")]["inventory_adjustment"] == -1
    assert all("WH/Stock" != x["location"] for x in r["rows"])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

"""MCP server: Google Drive expenses sheet → Odoo vendor bills, with dedupe and reconciliation tools."""
import json
import logging
from datetime import date, datetime

import anyio
from apscheduler.schedulers.background import BackgroundScheduler
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import settings
from .sync import Syncer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

missing = settings.missing()
if missing:
    log.error("Missing environment variables: %s", ", ".join(missing))

syncer = Syncer(settings) if not missing else None

mcp = FastMCP(
    "Green Hills Expenses → Odoo",
    instructions=(
        "Imports rows of the Green Hills expenses sheet (Google Drive) into Odoo as vendor bills. "
        "Every bill gets reference EXP-<row №>; rows already in Odoo are never created twice. "
        "Always run preview_sync first and show the user new rows, duplicates and possible duplicates; "
        "only call run_sync after the user confirms. Never force-create possible duplicates without the user's "
        "explicit approval for those row numbers."),
    host="0.0.0.0",
    port=settings.port,
    streamable_http_path=f"/{settings.mcp_secret or 'mcp-secret-not-set'}/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _need():
    if not syncer:
        raise RuntimeError(f"Server not configured, missing env vars: {', '.join(missing)}")
    return syncer


def _d(s: str | None, default: date) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date() if s else default


def _out(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str, indent=1)


async def _thread(fn, *a, **kw):
    return await anyio.to_thread.run_sync(lambda: fn(*a, **kw))


@mcp.tool()
async def preview_sync(from_no: int | None = None, to_no: int | None = None,
                       numbers: list[int] | None = None) -> str:
    """Dry run: read the expenses sheet and classify each row against Odoo WITHOUT creating anything.
    Groups: new (will be created), exists (already in Odoo by EXP key, with differences if any),
    duplicate (same payee+amount+date found in Odoo without key — entered earlier by hand),
    possible_duplicate (same amount+date but other partner, or twin rows in the sheet),
    skipped, error. Optional filters: row № range or explicit list."""
    s = _need()
    return _out(await _thread(s.run, dry_run=True, numbers=numbers, from_no=from_no, to_no=to_no))


@mcp.tool()
async def run_sync(from_no: int | None = None, to_no: int | None = None, numbers: list[int] | None = None,
                   force_numbers: list[int] | None = None, post: bool | None = None) -> str:
    """Create Odoo vendor bills for rows classified as 'new' (draft unless post=true / AUTO_POST).
    Rows in duplicate/possible_duplicate are created ONLY if their № is listed in force_numbers
    (requires the user's explicit confirmation). Documents from the row's Drive folder are attached."""
    s = _need()
    return _out(await _thread(s.run, dry_run=False, numbers=numbers, from_no=from_no, to_no=to_no,
                              force_numbers=force_numbers, post=post))


@mcp.tool()
async def link_existing_bill(row_no: int, odoo_move_id: int) -> str:
    """Declare that an existing Odoo record (account.move id) IS the expense from sheet row №.
    Appends the EXP key to its reference so it is treated as 'exists' from now on (no new bill)."""
    s = _need()
    return _out(await _thread(s.link_existing, row_no, odoo_move_id))


@mcp.tool()
async def check_duplicate(payee: str, amount: float, currency: str, on_date: str) -> str:
    """Check whether an expense (payee, amount, currency, date YYYY-MM-DD) is already recorded in Odoo
    (vendor bills, receipts, refunds, journal entries/payments within ±DUP_DAYS)."""
    s = _need()
    return _out(await _thread(s.check_duplicate, payee, amount, currency, _d(on_date, date.today())))


@mcp.tool()
async def reconcile_sheet_vs_odoo(date_from: str, date_to: str) -> str:
    """Reconciliation for a period (YYYY-MM-DD): sheet totals vs Odoo vendor-bill totals, rows missing in
    Odoo, rows probably entered without key, linked bills whose amount/date differ, and Odoo vendor bills
    that are not in the sheet. Also unpaid residual and draft totals."""
    s = _need()
    return _out(await _thread(s.reconcile, _d(date_from, date.today()), _d(date_to, date.today())))


@mcp.tool()
async def unreconciled_bank_lines(date_from: str, date_to: str, journal_id: int | None = None) -> str:
    """List bank statement lines not yet reconciled in Odoo for the period, with suggested open vendor
    bills of the same amount (read-only; reconcile in Odoo's bank reconciliation screen)."""
    s = _need()
    return _out(await _thread(s.unreconciled_bank_lines, _d(date_from, date.today()), _d(date_to, date.today()),
                              journal_id))


@mcp.tool()
async def last_sync_report() -> str:
    """Return the report of the most recent sync (scheduled or manual)."""
    s = _need()
    return _out(s.last_report or {"info": "no sync has run since the server started"})


@mcp.tool()
async def connector_status() -> str:
    """Configuration and connectivity check: Odoo version/login, sheet access, schedule."""
    if not syncer:
        return _out({"ok": False, "missing_env": missing})

    def chk():
        out = {"schedule_enabled": settings.schedule_enabled, "interval_min": settings.sync_interval_minutes,
               "auto_post": settings.auto_post, "ref_prefix": settings.ref_prefix,
               "service_account": syncer.drive.email}
        try:
            out["odoo_version"] = syncer.odoo.version()
            out["odoo_uid"] = syncer.odoo.uid
            out["company_currency"] = syncer.company_currency()
        except Exception as e:  # noqa: BLE001
            out["odoo_error"] = str(e)
        try:
            m = syncer.drive.meta(settings.sheet_file_id)
            out["sheet"] = {"name": m["name"], "mime": m["mimeType"], "modified": m["modifiedTime"]}
            out["sheet_rows"] = len(syncer.load_rows())
        except Exception as e:  # noqa: BLE001
            out["sheet_error"] = str(e)
        return out

    return _out(await _thread(chk))


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "configured": not missing,
                         "last_sync": (syncer.last_report or {}).get("at") if syncer else None})


def scheduled_job():
    try:
        rep = syncer.run(dry_run=False, source="schedule")
        log.info("Scheduled sync: %s", rep["counts"])
    except Exception:  # noqa: BLE001
        log.exception("Scheduled sync failed")


def main():
    if syncer and settings.schedule_enabled:
        sch = BackgroundScheduler(timezone="Asia/Makassar")
        sch.add_job(scheduled_job, "interval", minutes=settings.sync_interval_minutes,
                    next_run_time=datetime.now(), max_instances=1, coalesce=True)
        sch.start()
        log.info("Scheduler on: every %s min", settings.sync_interval_minutes)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()

"""All settings come from environment variables (set them in Railway → Variables)."""
import json
import os


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "да")


def _json(name: str, default):
    v = os.getenv(name)
    if not v:
        return default
    return json.loads(v)


class Settings:
    def __init__(self) -> None:
        # --- Odoo ---
        self.odoo_url = os.getenv("ODOO_URL", "").rstrip("/")          # https://green-hills-inc.odoo.com
        self.odoo_db = os.getenv("ODOO_DB", "")                         # green-hills-inc
        self.odoo_login = os.getenv("ODOO_LOGIN", "")                   # user e-mail
        self.odoo_api_key = os.getenv("ODOO_API_KEY", "")
        self.odoo_company_id = int(os.getenv("ODOO_COMPANY_ID", "0") or 0)
        self.odoo_journal_id = int(os.getenv("ODOO_PURCHASE_JOURNAL_ID", "0") or 0)

        # --- Google ---
        # either a service account JSON ...
        self.google_sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        # ... or an OAuth client (Client ID / Client secret) + refresh token (get it via /<MCP_SECRET>/oauth/start)
        self.google_client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        self.google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
        self.google_refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN", "")
        self.sheet_file_id = os.getenv("SHEET_FILE_ID", "")
        self.sheet_name = os.getenv("SHEET_NAME", "Expenses")
        # optional override of header names: {"number": "№", "payee": "Payee", ...}
        self.column_map = _json("COLUMN_MAP", {})

        # --- Business rules ---
        self.ref_prefix = os.getenv("REF_PREFIX", "EXP-")
        self.ref_digits = int(os.getenv("REF_DIGITS", "4"))
        # Type column → Odoo account code, e.g. {"Expense": "610000", "Tax / duty payment": "217000"}
        self.type_account_map = _json("TYPE_ACCOUNT_MAP", {})
        # Only these Type values are synced (empty list = all)
        self.sync_types = _json("SYNC_TYPES", [])
        # Payee in sheet → exact partner name in Odoo
        self.partner_aliases = _json("PARTNER_ALIASES", {})
        self.create_partners = _bool("CREATE_PARTNERS", True)
        self.auto_post = _bool("AUTO_POST", False)
        self.attach_documents = _bool("ATTACH_DOCUMENTS", True)
        self.sync_from_number = int(os.getenv("SYNC_FROM_NUMBER", "0") or 0)

        # duplicate search window / tolerance
        self.dup_days = int(os.getenv("DUP_DAYS", "5"))
        self.dup_tolerance_pct = float(os.getenv("DUP_TOLERANCE_PCT", "0.1"))

        # --- Schedule ---
        self.schedule_enabled = _bool("SCHEDULE_ENABLED", False)
        self.sync_interval_minutes = int(os.getenv("SYNC_INTERVAL_MINUTES", "60"))

        # --- Server ---
        self.mcp_secret = os.getenv("MCP_SECRET", "")
        self.port = int(os.getenv("PORT", "8000"))
        dom = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
        self.public_url = (os.getenv("PUBLIC_URL", "") or (f"https://{dom}" if dom else "")).rstrip("/")

    @property
    def google_oauth_ready(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_refresh_token)

    def missing(self) -> list[str]:
        req = {
            "ODOO_URL": self.odoo_url, "ODOO_DB": self.odoo_db, "ODOO_LOGIN": self.odoo_login,
            "ODOO_API_KEY": self.odoo_api_key,
            "GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_CLIENT_ID+GOOGLE_CLIENT_SECRET+GOOGLE_REFRESH_TOKEN":
                self.google_sa_json or self.google_oauth_ready,
            "SHEET_FILE_ID": self.sheet_file_id, "MCP_SECRET": self.mcp_secret,
        }
        return [k for k, v in req.items() if not v]


settings = Settings()

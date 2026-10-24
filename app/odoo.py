"""Thin Odoo external-API client (XML-RPC; tested design for Odoo Online 19 with an API key)."""
import threading
import xmlrpc.client


class OdooError(Exception):
    pass


class Odoo:
    def __init__(self, url: str, db: str, login: str, api_key: str, company_id: int = 0):
        self.url, self.db, self.login, self.key = url, db, login, api_key
        self.company_id = company_id
        self._uid = None
        self._lock = threading.Lock()
        self._fields_cache: dict[str, dict] = {}

    # -- low level ---------------------------------------------------------
    def _proxy(self, endpoint: str):
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/{endpoint}", allow_none=True)

    @property
    def uid(self) -> int:
        if self._uid is None:
            uid = self._proxy("common").authenticate(self.db, self.login, self.key, {})
            if not uid:
                raise OdooError("Odoo authentication failed: check ODOO_DB / ODOO_LOGIN / ODOO_API_KEY")
            self._uid = uid
        return self._uid

    def call(self, model: str, method: str, *args, **kwargs):
        if self.company_id:
            ctx = dict(kwargs.pop("context", {}) or {})
            ctx.setdefault("allowed_company_ids", [self.company_id])
            kwargs["context"] = ctx
        with self._lock:
            try:
                return self._proxy("object").execute_kw(self.db, self.uid, self.key, model, method, list(args), kwargs)
            except xmlrpc.client.Fault as e:
                msg = (e.faultString or "").strip().splitlines()
                raise OdooError(msg[-1] if msg else str(e)) from None

    def search_read(self, model, domain, fields, limit=0, order=None):
        kw = {"fields": fields}
        if limit:
            kw["limit"] = limit
        if order:
            kw["order"] = order
        return self.call(model, "search_read", domain, **kw)

    def fields(self, model: str) -> dict:
        if model not in self._fields_cache:
            try:
                self._fields_cache[model] = self.call(model, "fields_get", attributes=["type"])
            except OdooError:
                self._fields_cache[model] = {}
        return self._fields_cache[model]

    def has_model(self, model: str) -> bool:
        return bool(self.search_read("ir.model", [("model", "=", model)], ["id"], limit=1))

    # -- helpers -----------------------------------------------------------
    def version(self) -> str:
        return self._proxy("common").version().get("server_version", "?")

    def base_url_for(self, model: str, rec_id: int) -> str:
        return f"{self.url}/web#model={model}&id={rec_id}&view_type=form"

    def currency_id(self, code: str) -> int | None:
        r = self.search_read("res.currency", [("name", "=", code.upper())], ["id", "active"], limit=1)
        if not r:
            r = self.call("res.currency", "search_read", [("name", "=", code.upper()), ("active", "=", False)],
                          fields=["id", "active"], limit=1)
            if r:
                raise OdooError(f"Currency {code} exists but is not active in Odoo (Accounting → Settings → Currencies)")
            return None
        return r[0]["id"]

    def account_id(self, code: str) -> int | None:
        r = self.search_read("account.account", [("code", "=", code)], ["id"], limit=1)
        return r[0]["id"] if r else None

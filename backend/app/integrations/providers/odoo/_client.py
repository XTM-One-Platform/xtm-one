# SPDX-License-Identifier: Apache-2.0

"""The Odoo external API client: one call shape over two transports.

Odoo answers its external API in two forms, both with an API key:

* JSON-2 (Odoo 19 and later): ``POST {base}/json/2/{model}/{method}`` with
  ``Authorization: bearer <key>`` and ``X-Odoo-Database``; named arguments in
  the body, ``ids`` for a record method, ``context`` alongside.
* JSON-RPC (every supported version, the fallback): ``POST {base}/jsonrpc``,
  ``common.authenticate`` once for the user id, then ``object.execute_kw``.

:class:`OdooClient` probes JSON-2 first and keeps the transport that answered,
so a tool writes ``call(model, method, ids=..., **kwargs)`` whatever the
instance runs. Every call carries the companies it acts for as
``allowed_company_ids`` in the context (Odoo's multi-company switch). Faults
are mapped to one plain sentence: the server traceback (``debug``) is never
answered, the API key never appears in an answer or a log.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    RateLimiter,
    app_token_cache_key,
    get_shared_rate_limiter,
    safe_request,
)

PROVIDER = "Odoo"
SOURCE = "integration:odoo"
TIMEOUT = 60
SEPARATOR = "||"

#: Odoo methods that change nothing: a call of any other method is a write.
READ_METHODS = frozenset(
    {
        "search_read",
        "read",
        "search",
        "search_count",
        "fields_get",
        "read_group",
        "name_search",
        "name_get",
        "default_get",
        "check_access_rights",
        "context_get",
        "web_search_read",
        "web_read",
        "formatted_read_group",
        "get_views",
    }
)

#: An instance the JSON-2 API does not serve answers one of these on the probe.
_JSON2_ABSENT = frozenset({404, 405, 501})

_RATE_PER_SECOND = 5.0
_BURST = 10.0
_MAX_IN_FLIGHT = 4

#: Odoo exception class (last dotted part) -> what to tell the agent.
_FAULTS = {
    "AccessDenied": "Odoo refused the login or API key (wrong database, login or key, or the key was revoked)",
    "AccessError": "Odoo refused the operation for this user (access rights or record rules)",
    "MissingError": "the record does not exist or was deleted",
    "UserError": "Odoo refused the operation",
    "ValidationError": "Odoo rejected the values",
    "RedirectWarning": "Odoo refused the operation",
    "LockError": "the record is locked by another transaction",
    "CacheMiss": "a field could not be read",
}


class OdooError(Exception):
    """A call Odoo refused or that never reached it; ``str()`` is safe to answer."""


@dataclass(frozen=True)
class OdooCredential:
    """The stored credential ``base_url||database||login||api_key[||company_ids]``.

    *company_ids* (comma-separated) are the companies a call acts for when the
    tool names none; empty means the user's current company.
    """

    base_url: str
    database: str
    login: str
    api_key: str
    company_ids: tuple[int, ...] = ()

    @classmethod
    def parse(cls, token: str) -> OdooCredential:
        parts = (token or "").split(SEPARATOR)
        if len(parts) < 4:
            parts = ["", "", "", parts[-1] if parts else ""]
        base, database, login, key = (p.strip() for p in parts[:4])
        companies = tuple(
            int(c) for c in (parts[4] if len(parts) > 4 else "").replace(" ", "").split(",") if c.isdigit()
        )
        return cls(base_url=base.rstrip("/"), database=database, login=login, api_key=key, company_ids=companies)

    @staticmethod
    def build(base_url: str, database: str, login: str, api_key: str, company_ids: str = "") -> str:
        """The stored credential from the integration's fields."""
        parts = [base_url.strip().rstrip("/"), database.strip(), login.strip(), api_key.strip()]
        companies = ",".join(c for c in str(company_ids or "").replace(" ", "").split(",") if c.isdigit())
        if companies:
            parts.append(companies)
        return SEPARATOR.join(parts)

    def missing(self) -> str:
        """The first field the integration lacks, else ''."""
        for name, value in (
            ("the Odoo URL", self.base_url),
            ("the database", self.database),
            ("the login", self.login),
            ("the API key", self.api_key),
        ):
            if not value:
                return name
        return ""


def fault_message(name: str, message: str, action: str) -> str:
    """One sentence for an Odoo fault; *message* is Odoo's own text, bounded."""
    short = (name or "").rsplit(".", 1)[-1]
    lead = _FAULTS.get(short, "Odoo answered with an error")
    detail = " ".join(str(message or "").split())[:500]
    return f"Error: {lead} while trying to {action}" + (f": {detail}" if detail else ".")


@dataclass
class OdooClient:
    """Calls one Odoo database as one user, through JSON-2 or JSON-RPC."""

    credential: OdooCredential
    #: ``json2`` or ``jsonrpc`` once known; ``""`` until the first call probes.
    transport: str = ""
    uid: int | None = None
    _http: Any = field(default=None, repr=False)

    # -- plumbing -------------------------------------------------------------------

    async def __aenter__(self) -> OdooClient:
        self._http = egress_client(timeout=TIMEOUT)
        await self._http.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http is not None:
            await self._http.__aexit__(*exc)
            self._http = None

    def _limiter(self) -> RateLimiter:
        cred = self.credential
        return get_shared_rate_limiter(
            f"odoo:{app_token_cache_key('odoo', cred.base_url, cred.database, cred.login, cred.api_key)}",
            rate=_RATE_PER_SECOND,
            capacity=_BURST,
            max_concurrency=_MAX_IN_FLIGHT,
        )

    async def _post(
        self, url: str, body: dict[str, Any], headers: dict[str, str], action: str, *, read: bool
    ) -> httpx.Response:
        if self._http is None:
            raise OdooError("Error: the Odoo client is not open.")
        async with self._limiter().slot():
            resp, err = await safe_request(
                self._http,
                "post",
                url,
                PROVIDER,
                action,
                json=body,
                headers=headers,
                expected_statuses={400, 401, 403, 404, 405, 422, 500, 501},
                retry_statuses=TRANSIENT_RETRY_STATUSES if read else None,
            )
        if resp is None:
            raise OdooError(err or f"Error: Odoo did not answer while trying to {action}.")
        return resp

    @staticmethod
    def _json(resp: httpx.Response, action: str) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise OdooError(
                f"Error: Odoo answered {resp.status_code} with something that is not JSON while trying to {action} "
                "(check the URL: it must be the Odoo instance root, e.g. https://example.odoo.com)."
            ) from exc

    # -- JSON-RPC -------------------------------------------------------------------

    async def _jsonrpc(self, service: str, method: str, args: list[Any], action: str, *, read: bool) -> Any:
        body = {"jsonrpc": "2.0", "method": "call", "params": {"service": service, "method": method, "args": args}}
        resp = await self._post(
            f"{self.credential.base_url}/jsonrpc",
            body,
            {"Content-Type": "application/json", "Accept": "application/json"},
            action,
            read=read,
        )
        if resp.status_code in (404, 405, 501):
            raise OdooError(
                f"Error: {self.credential.base_url} does not serve the Odoo external API (HTTP {resp.status_code}); "
                "check the Odoo URL."
            )
        data = self._json(resp, action)
        if isinstance(data, dict) and data.get("error"):
            error: dict[str, Any] = data["error"] if isinstance(data["error"], dict) else {}
            raw = error.get("data")
            detail: dict[str, Any] = raw if isinstance(raw, dict) else {}
            raise OdooError(
                fault_message(detail.get("name", ""), detail.get("message") or error.get("message", ""), action)
            )
        if resp.status_code >= 400:
            raise OdooError(f"Error: Odoo answered HTTP {resp.status_code} while trying to {action}.")
        return data.get("result") if isinstance(data, dict) else None

    async def _authenticate(self) -> int:
        if self.uid is not None:
            return self.uid
        cred = self.credential
        uid = await self._jsonrpc(
            "common", "authenticate", [cred.database, cred.login, cred.api_key, {}], "sign in", read=True
        )
        if not uid:
            raise OdooError(
                "Error: Odoo refused the login or API key (wrong database, login or key, or the key was revoked)."
            )
        self.uid = int(uid)
        return self.uid

    # -- JSON-2 ---------------------------------------------------------------------

    def _json2_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"bearer {self.credential.api_key}",
            "X-Odoo-Database": self.credential.database,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _json2(self, model: str, method: str, body: dict[str, Any], action: str, *, read: bool) -> Any:
        resp = await self._post(
            f"{self.credential.base_url}/json/2/{model}/{method}", body, self._json2_headers(), action, read=read
        )
        if resp.status_code in _JSON2_ABSENT and self.transport == "":
            return _ABSENT
        if resp.status_code == 401:
            raise OdooError("Error: Odoo refused the API key (401: wrong key, wrong database, or the key was revoked).")
        data = self._json(resp, action)
        if resp.status_code >= 400:
            info = data if isinstance(data, dict) else {}
            raise OdooError(fault_message(info.get("name", ""), info.get("message", ""), action))
        return data

    # -- the one call shape ---------------------------------------------------------

    async def call(
        self,
        model: str,
        method: str,
        *,
        ids: Iterable[int] | None = None,
        company_ids: Iterable[int] = (),
        context: dict[str, Any] | None = None,
        action: str = "",
        **kwargs: Any,
    ) -> Any:
        """Run ``model.method`` and answer its result, or raise :class:`OdooError`."""
        missing = self.credential.missing()
        if missing:
            raise OdooError(f"Error: the Odoo integration has no {missing} configured; edit the integration.")
        action = action or f"call {model}.{method}"
        read = method in READ_METHODS
        ctx = dict(context or {})
        companies = [int(c) for c in company_ids if str(c).strip()] or list(self.credential.company_ids)
        if companies:
            ctx["allowed_company_ids"] = companies
        id_list = None if ids is None else [int(i) for i in ids]
        if self.transport in ("", "json2"):
            body: dict[str, Any] = {k: v for k, v in kwargs.items() if v is not None}
            if id_list is not None:
                body["ids"] = id_list
            if ctx:
                body["context"] = ctx
            result = await self._json2(model, method, body, action, read=read)
            if result is not _ABSENT:
                self.transport = "json2"
                return result
            self.transport = "jsonrpc"
        uid = await self._authenticate()
        cred = self.credential
        args: list[Any] = [] if id_list is None else [id_list]
        call_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        if ctx:
            call_kwargs["context"] = ctx
        return await self._jsonrpc(
            "object",
            "execute_kw",
            [cred.database, uid, cred.api_key, model, method, args, call_kwargs],
            action,
            read=read,
        )

    async def version(self) -> dict[str, Any]:
        """The server version (``common.version``); ``{}`` when the instance hides it."""
        try:
            data = await self._jsonrpc("common", "version", [], "read the server version", read=True)
        except OdooError:
            return {}
        return data if isinstance(data, dict) else {}

    async def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        *,
        limit: int = 0,
        offset: int = 0,
        order: str | None = None,
        company_ids: Iterable[int] = (),
        action: str = "",
    ) -> list[dict[str, Any]]:
        rows = await self.call(
            model,
            "search_read",
            company_ids=company_ids,
            action=action or f"read {model} records",
            domain=domain,
            fields=fields,
            limit=limit or None,
            offset=offset or None,
            order=order,
        )
        return rows if isinstance(rows, list) else []

    async def count(self, model: str, domain: list[Any], *, company_ids: Iterable[int] = ()) -> int:
        total = await self.call(
            model, "search_count", company_ids=company_ids, action=f"count {model} records", domain=domain
        )
        try:
            return int(total)
        except (TypeError, ValueError):
            return 0


#: The JSON-2 probe's "not served here" marker.
_ABSENT = object()

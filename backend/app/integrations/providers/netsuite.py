# SPDX-License-Identifier: Apache-2.0

"""Oracle NetSuite integration - finance reads through SuiteQL and the REST Record API, guarded writes.

API docs: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_1559132836.html
Auth, two modes:
  - OAuth 2.0 machine-to-machine (the default): the client credentials grant
    with a JWT assertion signed by the private key of a certificate uploaded on
    the integration record (``kid`` = the certificate id), PS256 for an RSA key,
    ES256 for an EC key. The access token (60 min) is cached.
  - Token-Based Authentication: OAuth 1.0a with HMAC-SHA256, every request
    signed with the consumer key and secret and the token id and secret,
    ``realm`` = the account id.
Hosts: ``<account>.suitetalk.api.netsuite.com`` (REST web services, SuiteQL,
the token endpoint) and ``<account>.restlets.api.netsuite.com`` (RESTlets);
a sandbox account id ``1234567_SB1`` is the host ``1234567-sb1``.

Endpoints used:
  - POST /services/rest/auth/oauth2/v1/token - the client credentials grant
  - POST /services/rest/query/v1/suiteql - SuiteQL (paged, ``Prefer: transient``)
  - GET  /services/rest/record/v1/metadata-catalog[/<type>] - record types and fields
  - GET/POST/PATCH /services/rest/record/v1/<type>[/<id>] - records
  - GET/POST /app/site/hosting/restlet.nl?script=&deploy= - a RESTlet

The credential ``integration_tools._get_token`` assembles (the secret last,
split greedily):
``account||m2m||client_id||certificate_id||scopes||private_key`` or
``account||tba||consumer_key||consumer_secret||token_id||token_secret``.

Every amount a tool reports carries its currency, its subsidiary and its
period. Entity bank details are read and written by two named tools alone
(``get_netsuite_entity_bank_details``, ``set_netsuite_entity_bank_details``)
so a binding can withhold them: the generic SuiteQL and record tools refuse
the Electronic Bank Payments tables and fields (``BANK_DETAILS_PATTERN``).
Posting transactions, period close and bank-detail writes ask a person first
(always_ask).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.file_delivery import deliver_file, file_content_type
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    app_token_cache_key,
    clip_text,
    get_cached_app_token,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)
from app.tools.base import CallableToolExecutor, ToolCall, ToolDefinition, ToolResult

_PROVIDER = "NetSuite"
_SOURCE = "integration:netsuite"
_TIMEOUT = 60
_MAX_OUTPUT = 40_000
_SUITEQL_PAGE = 1000
_DEFAULT_ROWS = 200
_MAX_ROWS = 5000
_DEFAULT_SCOPES = "rest_webservices"

#: Master records create/update_netsuite_record writes (a write, approval as the binding sets it).
MASTER_RECORDS = {
    "customer": "customer",
    "vendor": "vendor",
    "contact": "contact",
    "employee": "employee",
    "inventoryitem": "inventoryItem",
    "noninventorysaleitem": "nonInventorySaleItem",
    "noninventorypurchaseitem": "nonInventoryPurchaseItem",
    "noninventoryresaleitem": "nonInventoryResaleItem",
    "serviceitem": "serviceSaleItem",
    "servicesaleitem": "serviceSaleItem",
    "servicepurchaseitem": "servicePurchaseItem",
    "department": "department",
    "classification": "classification",
    "location": "location",
    "subsidiary": "subsidiary",
    "account": "account",
    "currency": "currency",
}

#: Posting transactions create/update_netsuite_transaction writes: they move the
#: ledger or money, so every call asks a person first (always_ask).
POSTING_RECORDS = {
    "vendorbill": "vendorBill",
    "vendorcredit": "vendorCredit",
    "vendorpayment": "vendorPayment",
    "purchaseorder": "purchaseOrder",
    "invoice": "invoice",
    "creditmemo": "creditMemo",
    "customerpayment": "customerPayment",
    "salesorder": "salesOrder",
    "journalentry": "journalEntry",
    "intercompanyjournalentry": "advInterCompanyJournalEntry",
    "advintercompanyjournalentry": "advInterCompanyJournalEntry",
    "deposit": "deposit",
    "check": "check",
    "transferorder": "transferOrder",
    "expensereport": "expenseReport",
}

#: The Electronic Bank Payments record of entity bank details (its default ids).
BANK_DETAILS_RECORD = "customrecord_2663_entity_bank_details"

#: Electronic Bank Payments records and fields (``customrecord_2663_*``,
#: ``custrecord_2663_*``): the generic SuiteQL and record tools refuse them so
#: withholding the two bank-detail tools withholds the data.
BANK_DETAILS_PATTERN = re.compile(r"\bcust(?:om)?record_2663_", re.I)
_BANK_DETAILS_REFUSAL = (
    "Error: Electronic Bank Payments records and fields (customrecord_2663_*, custrecord_2663_*) are read with "
    "get_netsuite_entity_bank_details and written with set_netsuite_entity_bank_details only."
)

#: Transaction types as SuiteQL stores them (``transaction.type``), by the name a tool takes.
TRANSACTION_TYPES = {
    "invoice": "CustInvc",
    "credit_memo": "CustCred",
    "customer_payment": "CustPymt",
    "sales_order": "SalesOrd",
    "vendor_bill": "VendBill",
    "vendor_credit": "VendCred",
    "vendor_payment": "VendPymt",
    "purchase_order": "PurchOrd",
    "journal_entry": "Journal",
    "intercompany_journal": "ICJournal",
    "deposit": "Deposit",
    "check": "Check",
    "transfer": "Transfer",
    "expense_report": "ExpRept",
}

_INCOME_STATEMENT_TYPES = ("Income", "COGS", "Expense", "OthIncome", "OthExpense")
_SEGMENTS = {"department": "department", "class": "classification", "location": "location"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STATUS = re.compile(r"^[A-Za-z0-9_:]{1,40}$")
_RECORD_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
_FIELD_ID = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_SCRIPT_ID = re.compile(r"^[A-Za-z0-9_]{1,100}$")
_ROLE_HINT = (
    "The NetSuite role of this integration lacks a permission for this call: give it the permission "
    "the error names (the integration setup guide lists them), then retry."
)
_PERIOD_HINT = (
    "The accounting period of this transaction is closed or locked for this role: post to an open "
    "period, or ask a NetSuite administrator to unlock it."
)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def account_host(account_id: str) -> str:
    """The host label of *account_id*: ``1234567_SB1`` -> ``1234567-sb1``."""
    return (account_id or "").strip().lower().replace("_", "-")


def account_realm(account_id: str) -> str:
    """The OAuth 1.0a realm of *account_id*: ``1234567-sb1`` -> ``1234567_SB1``."""
    return (account_id or "").strip().upper().replace("-", "_")


def rest_base(account_id: str) -> str:
    return f"https://{account_host(account_id)}.suitetalk.api.netsuite.com/services/rest"


def restlet_url(account_id: str) -> str:
    return f"https://{account_host(account_id)}.restlets.api.netsuite.com/app/site/hosting/restlet.nl"


def token_url(account_id: str) -> str:
    return f"{rest_base(account_id)}/auth/oauth2/v1/token"


def normalize_private_key(key: str) -> str:
    """A PEM key as pasted: newlines restored when a one-line field stripped them."""
    text = (key or "").strip().replace("\\n", "\n")
    if "\n" in text or "-----" not in text:
        return text
    match = re.match(r"(-----BEGIN [A-Z ]+-----)(.*?)(-----END [A-Z ]+-----)$", text, re.S)
    if not match:
        return text
    body = re.sub(r"\s+", "", match.group(2))
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return "\n".join([match.group(1), *lines, match.group(3)])


def compound_credential(secret: str, extra: Mapping[str, Any]) -> str:
    """The credential the tools run on, from the stored secret and extra_config; ``""`` when incomplete.

    *secret* is the API Key field: the certificate's private key (OAuth 2.0
    machine-to-machine) or the token secret (Token-Based Authentication).
    """
    account = str(extra.get("account_id") or "").strip()
    mode = str(extra.get("auth_mode") or "m2m").strip().lower()
    secret = (secret or "").strip()
    if not (account and secret) or not re.match(r"^[A-Za-z0-9_-]{1,40}$", account):
        return ""
    script = str(extra.get("companion_script_id") or "").strip()
    deploy = str(extra.get("companion_deploy_id") or "").strip()
    if _SCRIPT_ID.match(script) and _SCRIPT_ID.match(deploy):
        # The companion RESTlet rides with the account: account@script:deploy.
        account = f"{account}@{script}:{deploy}"
    if mode == "tba":
        consumer_key = str(extra.get("consumer_key") or "").strip()
        consumer_secret = str(extra.get("consumer_secret") or "").strip()
        token_id = str(extra.get("token_id") or "").strip()
        if not (consumer_key and consumer_secret and token_id):
            return ""
        return f"{account}||tba||{consumer_key}||{consumer_secret}||{token_id}||{secret}"
    client_id = str(extra.get("oauth_client_id") or "").strip()
    certificate_id = str(extra.get("certificate_id") or "").strip()
    scopes = str(extra.get("oauth_scopes") or _DEFAULT_SCOPES).strip() or _DEFAULT_SCOPES
    if not (client_id and certificate_id):
        return ""
    return f"{account}||m2m||{client_id}||{certificate_id}||{scopes}||{secret}"


def parse_token(token: str) -> dict[str, str]:
    """The fields of a compound credential (see the module docstring)."""
    raw_account, mode, _rest = parse_compound_token(token or "", 3, greedy_last=True)
    account, _sep, companion = raw_account.partition("@")
    mode = (mode or "").lower()
    if mode == "tba":
        _a, _m, consumer_key, consumer_secret, token_id, token_secret = parse_compound_token(token, 6, greedy_last=True)
        return {
            "account_id": account,
            "mode": "tba",
            "consumer_key": consumer_key,
            "consumer_secret": consumer_secret,
            "token_id": token_id,
            "token_secret": token_secret,
            "companion": companion,
        }
    _a, _m, client_id, certificate_id, scopes, private_key = parse_compound_token(token, 6, greedy_last=True)
    return {
        "account_id": account,
        "mode": "m2m",
        "client_id": client_id,
        "certificate_id": certificate_id,
        "scopes": scopes or _DEFAULT_SCOPES,
        "private_key": private_key,
        "companion": companion,
    }


def _encode(value: str) -> str:
    """RFC 3986 percent-encoding, as OAuth 1.0a signs it."""
    return quote(str(value), safe="~")


def tba_authorization(
    method: str,
    url: str,
    query: Mapping[str, str],
    fields: Mapping[str, str],
    *,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    """The OAuth 1.0a ``Authorization`` header of one request, signed with HMAC-SHA256.

    *url* is the request URL without its query string; *query* its query
    parameters (signed with the oauth parameters, sorted).
    """
    oauth = {
        "oauth_consumer_key": fields["consumer_key"],
        "oauth_token": fields["token_id"],
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_version": "1.0",
    }
    pairs = sorted((_encode(k), _encode(v)) for k, v in [*oauth.items(), *query.items()])
    normalized = "&".join(f"{k}={v}" for k, v in pairs)
    base_string = "&".join([method.upper(), _encode(url), _encode(normalized)])
    key = f"{_encode(fields['consumer_secret'])}&{_encode(fields['token_secret'])}"
    signature = base64.b64encode(hmac.new(key.encode(), base_string.encode(), hashlib.sha256).digest()).decode()
    header = {"realm": account_realm(fields["account_id"]), **oauth, "oauth_signature": signature}
    return "OAuth " + ", ".join(f'{k}="{_encode(v)}"' for k, v in header.items())


def client_assertion(fields: Mapping[str, str], *, now: int | None = None) -> str:
    """The JWT the client credentials grant presents, signed with the certificate's private key."""
    key_text = normalize_private_key(fields["private_key"])
    private_key = serialization.load_pem_private_key(key_text.encode(), password=None)
    if isinstance(private_key, ec.EllipticCurvePrivateKey):
        algorithm = "ES256"
    elif isinstance(private_key, rsa.RSAPrivateKey):
        algorithm = "PS256"
    else:
        raise ValueError("the certificate key must be an RSA or EC private key")
    issued = int(now if now is not None else time.time())
    scopes = [s.strip() for s in re.split(r"[,\s]+", fields.get("scopes") or _DEFAULT_SCOPES) if s.strip()]
    claims = {
        "iss": fields["client_id"],
        "scope": scopes,
        "aud": token_url(fields["account_id"]),
        "iat": issued,
        "exp": issued + 3600,
    }
    return jwt.encode(claims, private_key, algorithm=algorithm, headers={"kid": fields["certificate_id"], "typ": "JWT"})


async def m2m_access_token(fields: Mapping[str, str]) -> tuple[str | None, str | None]:
    """An access token from the client credentials grant, cached until shortly before it expires."""
    if not (fields.get("client_id") and fields.get("certificate_id") and fields.get("private_key")):
        return None, "Error: NetSuite needs the client ID, the certificate ID and the certificate's private key."
    cache_key = app_token_cache_key(
        "netsuite",
        account_host(fields["account_id"]),
        fields["client_id"],
        fields["certificate_id"],
        fields.get("scopes") or "",
        fields["private_key"],
    )
    cached = get_cached_app_token(cache_key)
    if cached:
        return cached, None
    try:
        assertion = client_assertion(fields)
    except (ValueError, TypeError) as exc:
        return None, (
            "Error: the NetSuite certificate private key could not be read "
            f"({format_exception_message(exc)}); paste the PEM private key of the certificate."
        )
    async with egress_client(timeout=_TIMEOUT) as client:
        resp, err = await safe_request(
            client,
            "post",
            token_url(fields["account_id"]),
            _PROVIDER,
            "get an access token (OAuth 2.0 client credentials)",
            data={
                "grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion,
            },
            headers={"Accept": "application/json"},
        )
    if err or resp is None:
        return None, (
            f"{err or 'Error: no answer from NetSuite.'}\nCheck the account ID, the client ID, the certificate ID "
            "(Setup > Integration > OAuth 2.0 Client Credentials) and that the integration record allows the "
            "Client Credentials (M2M) grant with the scopes set here."
        )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    access = str((body or {}).get("access_token") or "")
    if not access:
        return None, "Error: NetSuite answered the token request without an access token."
    set_cached_app_token(cache_key, access, int((body or {}).get("expires_in") or 3600))
    return access, None


def _with_hint(err: str) -> str:
    lowered = err.lower()
    if "insufficient_permission" in lowered or ("permission" in lowered and "403" in lowered):
        return f"{err}\n{_ROLE_HINT}"
    if "period" in lowered and ("locked" in lowered or "closed" in lowered):
        return f"{err}\n{_PERIOD_HINT}"
    return err


# ---------------------------------------------------------------------------
# SQL helpers: every value interpolated into SuiteQL is checked first
# ---------------------------------------------------------------------------


def _int_id(value: Any, name: str) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a NetSuite internal id (a number), got {value!r}") from exc
    if number < 0:
        raise ValueError(f"{name} must be a positive internal id")
    return number


def _sql_date(value: str, name: str) -> str:
    text = (value or "").strip()
    if not _DATE.match(text):
        raise ValueError(f"{name} must be a date YYYY-MM-DD, got {value!r}")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid date YYYY-MM-DD, got {value!r}") from exc
    return f"TO_DATE('{text}', 'YYYY-MM-DD')"


def _sql_text(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _format_rows(rows: list[dict], columns: list[str] | None = None) -> str:
    if not rows:
        return "No row."
    cols = columns or [k for k in rows[0] if k != "links"]
    lines = ["\t".join(cols)]
    for row in rows:
        lines.append("\t".join("" if row.get(c) is None else str(row.get(c)).replace("\t", " ") for c in cols))
    return "\n".join(lines)


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value or "0")


# ---------------------------------------------------------------------------
# Client: the signed REST calls and the paged SuiteQL read the tools and the poller share
# ---------------------------------------------------------------------------


def _client(fields: Mapping[str, str]) -> tuple[Any, Any, Any]:
    """``(_headers, _call, _suiteql)`` bound to one parsed credential."""
    base = rest_base(fields["account_id"])

    async def _headers(method: str, url: str, query: Mapping[str, str]) -> tuple[dict[str, str] | None, str | None]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if fields["mode"] == "tba":
            if not all(fields.get(k) for k in ("consumer_key", "consumer_secret", "token_id", "token_secret")):
                return None, "Error: NetSuite TBA needs the consumer key and secret and the token ID and secret."
            headers["Authorization"] = tba_authorization(method, url, query, fields)
            return headers, None
        access, err = await m2m_access_token(fields)
        if err or not access:
            return None, err or "Error: no NetSuite access token."
        headers["Authorization"] = f"Bearer {access}"
        return headers, None

    async def _call(
        method: str,
        url: str,
        action: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Any = None,
        read: bool = False,
        extra_headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, str | None, dict[str, str]]:
        query = {k: str(v) for k, v in (query or {}).items()}
        headers, err = await _headers(method, url, query)
        if err or headers is None:
            return None, err or "Error", {}
        headers.update(extra_headers or {})
        full_url = url
        if query:
            full_url += "?" + "&".join(f"{_encode(k)}={_encode(v)}" for k, v in query.items())
        kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            kwargs["content"] = json.dumps(body).encode()
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                method,
                full_url,
                _PROVIDER,
                action,
                retry_statuses=TRANSIENT_RETRY_STATUSES if read else None,
                **kwargs,
            )
        if err or resp is None:
            return None, _with_hint(err or "Error: no answer from NetSuite."), {}
        if not resp.content:
            return {}, None, dict(resp.headers)
        try:
            return resp.json(), None, dict(resp.headers)
        except ValueError:
            return {"text": resp.text}, None, dict(resp.headers)

    async def _suiteql(sql: str, max_rows: int, action: str) -> tuple[list[dict], bool, int | None, str | None]:
        rows: list[dict] = []
        offset = 0
        total: int | None = None
        has_more = False
        while len(rows) < max_rows:
            limit = min(_SUITEQL_PAGE, max_rows - len(rows))
            data, err, _ = await _call(
                "post",
                f"{base}/query/v1/suiteql",
                action,
                query={"limit": str(limit), "offset": str(offset)},
                body={"q": sql},
                read=True,
                extra_headers={"Prefer": "transient"},
            )
            if err:
                return rows, False, total, err
            items = [i for i in (data or {}).get("items", []) if isinstance(i, dict)]
            rows.extend({k: v for k, v in i.items() if k != "links"} for i in items)
            total = (data or {}).get("totalResults", total)
            has_more = bool((data or {}).get("hasMore"))
            if not has_more or not items:
                break
            offset += len(items)
        return rows, has_more, total, None

    return _headers, _call, _suiteql


async def read_suiteql(token: str, sql: str, max_rows: int, action: str) -> tuple[list[dict], bool, str | None]:
    """``(rows, has_more, error)`` of one SuiteQL read on a compound credential (the event poller)."""
    fields = parse_token(token)
    if not fields.get("account_id"):
        return [], False, "Error: the NetSuite integration has no account ID."
    _headers, _call, _suiteql = _client(fields)
    rows, has_more, _total, err = await _suiteql(sql, max_rows, action)
    return rows, has_more, err


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    fields = parse_token(token)
    if not fields.get("account_id"):
        return []
    account = fields["account_id"]
    base = rest_base(account)
    persist_file_fn = kwargs.get("persist_file_fn")
    companion_script, _sep, companion_deploy = (fields.get("companion") or "").partition(":")
    if not (_SCRIPT_ID.match(companion_script) and _SCRIPT_ID.match(companion_deploy)):
        companion_script = companion_deploy = ""
    _headers, _call, _suiteql = _client(fields)

    async def _query_text(sql: str, max_rows: int, action: str, header: str = "") -> str:
        rows, has_more, total, err = await _suiteql(sql, max_rows, action)
        if err:
            return err
        lines = [header] if header else []
        shown = f"{len(rows)} row(s)" + (f" of {total}" if total is not None else "")
        if has_more:
            shown += f"; more rows remain: raise max_rows (at most {_MAX_ROWS}) or narrow the query"
        lines.append(shown + ":")
        lines.append(_format_rows(rows))
        return clip_text("\n".join(lines), _MAX_OUTPUT, "run_netsuite_suiteql with a narrower query")

    def _rows_arg(max_rows: Any) -> int:
        try:
            return max(1, min(int(max_rows or _DEFAULT_ROWS), _MAX_ROWS))
        except (TypeError, ValueError):
            return _DEFAULT_ROWS

    # ── Generic reads ────────────────────────────────────────────────────

    async def run_netsuite_suiteql(query: str, max_rows: int = _DEFAULT_ROWS) -> str:
        """Run a SuiteQL SELECT, paged, at most max_rows rows."""
        sql = (query or "").strip().rstrip(";")
        if not re.match(r"^(select|with)\b", sql, re.I):
            return "Error: run_netsuite_suiteql runs a SELECT (or WITH ... SELECT) query only."
        if BANK_DETAILS_PATTERN.search(sql):
            return _BANK_DETAILS_REFUSAL
        return await _query_text(sql, _rows_arg(max_rows), "run a SuiteQL query")

    async def describe_netsuite_records(record_type: str = "") -> str:
        """List the record types, or the fields of one record type, from the metadata catalog."""
        rtype = (record_type or "").strip()
        if not rtype:
            data, err, _ = await _call("get", f"{base}/record/v1/metadata-catalog", "list record types", read=True)
            if err:
                return err
            names = sorted(str(i.get("name")) for i in (data or {}).get("items", []) if isinstance(i, dict))
            return clip_text(
                f"{len(names)} record type(s): " + ", ".join(names),
                _MAX_OUTPUT,
                "describe_netsuite_records with a record_type",
            )
        if not _RECORD_TYPE.match(rtype):
            return "Error: record_type is a NetSuite record type name such as vendorBill."
        if BANK_DETAILS_PATTERN.search(rtype):
            return _BANK_DETAILS_REFUSAL
        data, err, _ = await _call(
            "get",
            f"{base}/record/v1/metadata-catalog/{quote(rtype, safe='')}",
            f"describe record type {rtype}",
            read=True,
            extra_headers={"Accept": "application/schema+json"},
        )
        if err:
            return err
        properties = (data or {}).get("properties") or {}
        lines = [f"{rtype}: {len(properties)} field(s)"]
        for name, spec in sorted(properties.items()):
            if not isinstance(spec, dict):
                continue
            kind = spec.get("type") or ("record" if "$ref" in spec or "properties" in spec else "")
            title = spec.get("title") or ""
            lines.append(f"  {name} ({kind}){': ' + title if title else ''}")
        return clip_text("\n".join(lines), _MAX_OUTPUT, "run_netsuite_suiteql on the record's table")

    async def get_netsuite_record(record_type: str, record_id: str, fields: str = "", expand: bool = False) -> str:
        """Read one record through the REST Record API."""
        rtype = (record_type or "").strip()
        if not _RECORD_TYPE.match(rtype):
            return "Error: record_type is a NetSuite record type name such as vendorBill."
        if BANK_DETAILS_PATTERN.search(rtype) or BANK_DETAILS_PATTERN.search(fields or ""):
            return _BANK_DETAILS_REFUSAL
        query: dict[str, str] = {}
        if (fields or "").strip():
            query["fields"] = ",".join(f.strip() for f in fields.split(",") if f.strip())
        if expand:
            query["expandSubResources"] = "true"
        data, err, _ = await _call(
            "get",
            f"{base}/record/v1/{quote(rtype, safe='')}/{quote(str(record_id).strip(), safe='')}",
            f"read {rtype} {record_id}",
            query=query,
            read=True,
        )
        if err:
            return err
        cleaned = _strip_links(data)
        return clip_text(
            f"{rtype} {record_id}:\n" + json.dumps(cleaned, indent=1, default=str),
            _MAX_OUTPUT,
            "get_netsuite_record with fields",
        )

    async def list_netsuite_records(record_type: str, q: str = "", limit: int = 100, offset: int = 0) -> str:
        """List record ids of a type, with an optional REST query filter."""
        rtype = (record_type or "").strip()
        if not _RECORD_TYPE.match(rtype):
            return "Error: record_type is a NetSuite record type name such as vendorBill."
        if BANK_DETAILS_PATTERN.search(rtype) or BANK_DETAILS_PATTERN.search(q or ""):
            return _BANK_DETAILS_REFUSAL
        query = {"limit": str(max(1, min(int(limit or 100), 1000))), "offset": str(max(0, int(offset or 0)))}
        if (q or "").strip():
            query["q"] = q.strip()
        data, err, _ = await _call(
            "get", f"{base}/record/v1/{quote(rtype, safe='')}", f"list {rtype}", query=query, read=True
        )
        if err:
            return err
        ids = [str(i.get("id")) for i in (data or {}).get("items", []) if isinstance(i, dict)]
        more = " (more: raise offset)" if (data or {}).get("hasMore") else ""
        return (
            f"{len(ids)} {rtype} record(s) of {(data or {}).get('totalResults', len(ids))}{more}: "
            + ", ".join(ids)
            + "\nRead one with get_netsuite_record."
        )

    # ── Finance core ─────────────────────────────────────────────────────

    async def list_netsuite_subsidiaries() -> str:
        """The subsidiaries (OneWorld) with their parent, country and base currency."""
        sql = (
            "SELECT s.id, s.name, s.fullname, BUILTIN.DF(s.parent) AS parent, s.country, "
            "BUILTIN.DF(s.currency) AS base_currency, s.iselimination, s.isinactive "
            "FROM subsidiary s ORDER BY s.fullname"
        )
        return await _query_text(sql, 1000, "list subsidiaries")

    async def list_netsuite_accounting_periods(year: str = "", include_adjustments: bool = False) -> str:
        """Accounting periods with open, closed and locked flags."""
        where = ["ap.isyear = 'F'", "ap.isquarter = 'F'"]
        if not include_adjustments:
            where.append("ap.isadjust = 'F'")
        if (year or "").strip():
            try:
                y = int(year)
            except ValueError:
                return "Error: year is a four-digit year."
            where.append(f"EXTRACT(YEAR FROM ap.enddate) = {y}")
        sql = (
            "SELECT ap.id, ap.periodname, ap.startdate, ap.enddate, ap.closed, ap.aplocked, ap.arlocked, "
            "ap.alllocked, ap.isposting FROM accountingperiod ap WHERE "
            + " AND ".join(where)
            + " ORDER BY ap.startdate"
        )
        return await _query_text(sql, 1000, "list accounting periods")

    async def list_netsuite_accounts(account_type: str = "", search: str = "", include_inactive: bool = False) -> str:
        """The chart of accounts."""
        where = []
        if not include_inactive:
            where.append("a.isinactive = 'F'")
        if (account_type or "").strip():
            if not _STATUS.match(account_type.strip()):
                return "Error: account_type is a NetSuite account type such as Bank, AcctRec, Expense."
            where.append(f"a.accttype = {_sql_text(account_type.strip())}")
        if (search or "").strip():
            needle = _sql_text(f"%{search.strip().lower()}%")
            where.append(f"(LOWER(a.acctnumber) LIKE {needle} OR LOWER(a.fullname) LIKE {needle})")
        sql = (
            "SELECT a.id, a.acctnumber, a.fullname, a.accttype, BUILTIN.DF(a.currency) AS currency, "
            "a.eliminate AS intercompany, BUILTIN.DF(a.subsidiary) AS subsidiaries FROM account a"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY a.acctnumber"
        )
        return await _query_text(sql, _MAX_ROWS, "list accounts")

    async def list_netsuite_segments(segment: str) -> str:
        """Departments, classes or locations."""
        table = _SEGMENTS.get((segment or "").strip().lower())
        if not table:
            return "Error: segment is department, class or location."
        sql = (
            f"SELECT t.id, t.name, t.fullname, BUILTIN.DF(t.subsidiary) AS subsidiaries, t.isinactive "
            f"FROM {table} t ORDER BY t.fullname"
        )
        return await _query_text(sql, _MAX_ROWS, f"list {segment}s")

    async def list_netsuite_currencies(as_of: str = "", base_currency: str = "") -> str:
        """Currencies, and the exchange rates effective on a date."""
        currencies = await _query_text(
            "SELECT c.id, c.symbol, c.name, c.isbasecurrency, c.isinactive FROM currency c ORDER BY c.symbol",
            1000,
            "list currencies",
            "Currencies:",
        )
        try:
            day = _sql_date(as_of or datetime.now(UTC).strftime("%Y-%m-%d"), "as_of")
        except ValueError as exc:
            return f"Error: {exc}"
        where = [f"r.effectivedate <= {day}"]
        if (base_currency or "").strip():
            if not _STATUS.match(base_currency.strip()):
                return "Error: base_currency is a currency symbol such as EUR."
            where.append(f"BUILTIN.DF(r.basecurrency) = {_sql_text(base_currency.strip().upper())}")
        rates = await _query_text(
            "SELECT BUILTIN.DF(r.basecurrency) AS base, BUILTIN.DF(r.transactioncurrency) AS currency, "
            "r.exchangerate, r.effectivedate FROM currencyrate r WHERE "
            + " AND ".join(where)
            + " AND r.effectivedate = (SELECT MAX(r2.effectivedate) FROM currencyrate r2 WHERE "
            "r2.basecurrency = r.basecurrency AND r2.transactioncurrency = r.transactioncurrency AND "
            f"r2.effectivedate <= {day}) ORDER BY base, currency",
            2000,
            "read exchange rates",
            "Exchange rates (1 currency = rate base):",
        )
        return f"{currencies}\n\n{rates}"

    async def search_netsuite_entities(
        entity: str, search: str = "", subsidiary_id: str = "", max_rows: int = _DEFAULT_ROWS
    ) -> str:
        """Customers or vendors, by name, id or email."""
        table = {"customer": "customer", "vendor": "vendor"}.get((entity or "").strip().lower())
        if not table:
            return "Error: entity is customer or vendor."
        where = []
        if (search or "").strip():
            needle = _sql_text(f"%{search.strip().lower()}%")
            where.append(
                f"(LOWER(e.entityid) LIKE {needle} OR LOWER(e.companyname) LIKE {needle} "
                f"OR LOWER(e.email) LIKE {needle})"
            )
        try:
            if (subsidiary_id or "").strip():
                where.append(f"e.subsidiary = {_int_id(subsidiary_id, 'subsidiary_id')}")
        except ValueError as exc:
            return f"Error: {exc}"
        sql = (
            f"SELECT e.id, e.entityid, e.companyname, e.email, BUILTIN.DF(e.subsidiary) AS subsidiary, "
            f"BUILTIN.DF(e.currency) AS currency, e.balance AS balance, BUILTIN.DF(e.terms) AS terms, "
            f"e.isinactive FROM {table} e" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY e.entityid"
        )
        return await _query_text(sql, _rows_arg(max_rows), f"search {table}s")

    async def list_netsuite_items(search: str = "", item_type: str = "", max_rows: int = _DEFAULT_ROWS) -> str:
        """Items with their type and accounts."""
        where = ["i.isinactive = 'F'"]
        if (search or "").strip():
            needle = _sql_text(f"%{search.strip().lower()}%")
            where.append(f"(LOWER(i.itemid) LIKE {needle} OR LOWER(i.displayname) LIKE {needle})")
        if (item_type or "").strip():
            if not _STATUS.match(item_type.strip()):
                return "Error: item_type is a NetSuite item type such as InvtPart, Service, NonInvtPart."
            where.append(f"i.itemtype = {_sql_text(item_type.strip())}")
        sql = (
            "SELECT i.id, i.itemid, i.displayname, i.itemtype, BUILTIN.DF(i.incomeaccount) AS income_account, "
            "BUILTIN.DF(i.expenseaccount) AS expense_account FROM item i WHERE "
            + " AND ".join(where)
            + " ORDER BY i.itemid"
        )
        return await _query_text(sql, _rows_arg(max_rows), "list items")

    async def list_netsuite_transactions(
        transaction_type: str,
        subsidiary_id: str = "",
        period_id: str = "",
        account_id: str = "",
        entity_id: str = "",
        status: str = "",
        from_date: str = "",
        to_date: str = "",
        min_amount: float | None = None,
        max_rows: int = _DEFAULT_ROWS,
    ) -> str:
        """Transactions of a type with filters, each with amount, currency, subsidiary and period."""
        ttype = TRANSACTION_TYPES.get((transaction_type or "").strip().lower())
        if not ttype:
            return "Error: transaction_type is one of " + ", ".join(sorted(TRANSACTION_TYPES)) + "."
        where = [f"t.type = {_sql_text(ttype)}", "tl.mainline = 'T'"]
        try:
            if (subsidiary_id or "").strip():
                where.append(f"tl.subsidiary = {_int_id(subsidiary_id, 'subsidiary_id')}")
            if (period_id or "").strip():
                where.append(f"t.postingperiod = {_int_id(period_id, 'period_id')}")
            if (entity_id or "").strip():
                where.append(f"t.entity = {_int_id(entity_id, 'entity_id')}")
            if (account_id or "").strip():
                acct = _int_id(account_id, "account_id")
                where.append(
                    f"EXISTS (SELECT 1 FROM transactionaccountingline x WHERE x.transaction = t.id "
                    f"AND x.account = {acct})"
                )
            if (from_date or "").strip():
                where.append(f"t.trandate >= {_sql_date(from_date, 'from_date')}")
            if (to_date or "").strip():
                where.append(f"t.trandate <= {_sql_date(to_date, 'to_date')}")
        except ValueError as exc:
            return f"Error: {exc}"
        if (status or "").strip():
            if not _STATUS.match(status.strip()):
                return "Error: status is a NetSuite status code such as A, B or VendBill:A."
            code = status.strip()
            where.append(f"(t.status = {_sql_text(code)} OR BUILTIN.DF(t.status) = {_sql_text(code)})")
        if min_amount is not None:
            where.append(f"ABS(t.foreigntotal) >= {float(min_amount)}")
        sql = (
            "SELECT t.id, t.tranid, t.trandate, BUILTIN.DF(t.status) AS status, BUILTIN.DF(t.entity) AS entity, "
            "t.foreigntotal AS amount, BUILTIN.DF(t.currency) AS currency, t.exchangerate, "
            "BUILTIN.DF(tl.subsidiary) AS subsidiary, BUILTIN.DF(t.postingperiod) AS period, t.duedate, "
            "t.memo, t.approvalstatus, t.lastmodifieddate "
            "FROM transaction t JOIN transactionline tl ON tl.transaction = t.id WHERE "
            + " AND ".join(where)
            + " ORDER BY t.trandate DESC, t.id DESC"
        )
        return await _query_text(sql, _rows_arg(max_rows), f"list {transaction_type} transactions")

    async def get_netsuite_transaction(transaction_id: str) -> str:
        """One transaction: header and every accounting line."""
        try:
            tid = _int_id(transaction_id, "transaction_id")
        except ValueError as exc:
            return f"Error: {exc}"
        header = await _query_text(
            "SELECT t.id, t.tranid, BUILTIN.DF(t.type) AS type, t.trandate, BUILTIN.DF(t.status) AS status, "
            "BUILTIN.DF(t.entity) AS entity, t.foreigntotal AS amount, BUILTIN.DF(t.currency) AS currency, "
            "t.exchangerate, BUILTIN.DF(t.postingperiod) AS period, t.memo, t.approvalstatus, t.createddate, "
            f"t.lastmodifieddate FROM transaction t WHERE t.id = {tid}",
            1,
            f"read transaction {tid}",
            "Header:",
        )
        lines = await _query_text(
            "SELECT tl.linesequencenumber AS line, BUILTIN.DF(tal.account) AS account, tal.debit, tal.credit, "
            "tal.amount AS amount_base, tl.foreignamount AS amount_transaction, "
            "BUILTIN.DF(tl.subsidiary) AS subsidiary, BUILTIN.DF(sub.currency) AS base_currency, "
            "BUILTIN.DF(tl.department) AS department, BUILTIN.DF(tl.class) AS class, "
            "BUILTIN.DF(tl.location) AS location, BUILTIN.DF(tl.entity) AS line_entity, tl.memo "
            "FROM transactionaccountingline tal "
            "JOIN transactionline tl ON tl.transaction = tal.transaction AND tl.id = tal.transactionline "
            "JOIN subsidiary sub ON sub.id = tl.subsidiary "
            "JOIN accountingbook ab ON ab.id = tal.accountingbook AND ab.isprimary = 'T' "
            f"WHERE tal.transaction = {tid} AND tal.posting = 'T' ORDER BY tl.linesequencenumber",
            2000,
            f"read the lines of transaction {tid}",
            "Accounting lines (primary book; amount_base in the subsidiary's base currency):",
        )
        return f"{header}\n\n{lines}"

    def _ledger_sql(subsidiary: int, period: int, consolidated: bool, account_filter: str, period_filter: str) -> str:
        amount = (
            f"BUILTIN.CONSOLIDATE(tal.amount, 'LEDGER', 'DEFAULT', 'DEFAULT', {subsidiary}, {period}, 'DEFAULT')"
            if consolidated
            else "tal.amount"
        )
        scope = (
            f"tl.subsidiary IN (SELECT s.id FROM subsidiary s WHERE s.id = {subsidiary} OR s.parent = {subsidiary})"
            if consolidated
            else f"tl.subsidiary = {subsidiary}"
        )
        return (
            f"SELECT a.acctnumber, a.fullname AS account, a.accttype, SUM({amount}) AS balance "
            "FROM transactionaccountingline tal "
            "JOIN transaction t ON t.id = tal.transaction "
            "JOIN transactionline tl ON tl.transaction = tal.transaction AND tl.id = tal.transactionline "
            "JOIN account a ON a.id = tal.account "
            "JOIN accountingperiod ap ON ap.id = t.postingperiod "
            "JOIN accountingbook ab ON ab.id = tal.accountingbook AND ab.isprimary = 'T' "
            f"WHERE tal.posting = 'T' AND {scope} AND {period_filter}{account_filter} "
            "GROUP BY a.acctnumber, a.fullname, a.accttype HAVING SUM(" + amount + ") <> 0 "
            "ORDER BY a.acctnumber"
        )

    async def _context(subsidiary: int, period: int) -> tuple[str, str]:
        rows, _more, _total, err = await _suiteql(
            "SELECT s.name, BUILTIN.DF(s.currency) AS currency, "
            f"(SELECT ap.periodname FROM accountingperiod ap WHERE ap.id = {period}) AS period "
            f"FROM subsidiary s WHERE s.id = {subsidiary}",
            1,
            "read the subsidiary and the period",
        )
        if err or not rows:
            return f"subsidiary {subsidiary}, period {period}", "base currency"
        row = rows[0]
        return f"{row.get('name')}, period {row.get('period')}", str(row.get("currency") or "base currency")

    async def _statement(
        title: str, subsidiary_id: str, period_id: str, consolidated: bool, account_filter: str, period_filter: str
    ) -> str:
        try:
            sub = _int_id(subsidiary_id, "subsidiary_id")
            per = _int_id(period_id, "period_id")
        except ValueError as exc:
            return f"Error: {exc}"
        where, currency = await _context(sub, per)
        sql = _ledger_sql(sub, per, consolidated, account_filter, period_filter.format(period=per))
        rows, has_more, _total, err = await _suiteql(sql, _MAX_ROWS, title.lower())
        if err:
            return err
        scope = (
            "consolidated to it (its children included, at the period's consolidation rates)"
            if consolidated
            else "this subsidiary alone"
        )
        lines = [f"{title}: {where}, {scope}. Amounts in {currency}."]
        total = 0.0
        for row in rows:
            value = float(row.get("balance") or 0)
            total += value
            lines.append(
                f"  {row.get('acctnumber') or ''} {row.get('account')} ({row.get('accttype')}): "
                f"{_money(value)} {currency}"
            )
        lines.append(f"Total: {_money(total)} {currency} ({len(rows)} account(s))")
        if has_more:
            lines.append("More accounts remain: run run_netsuite_suiteql for the rest.")
        return clip_text("\n".join(lines), _MAX_OUTPUT, "run_netsuite_suiteql")

    cumulative = "ap.enddate <= (SELECT p.enddate FROM accountingperiod p WHERE p.id = {period})"

    async def get_netsuite_trial_balance(subsidiary_id: str, period_id: str, consolidated: bool = False) -> str:
        """Trial balance at the end of a period, per subsidiary or consolidated."""
        return await _statement("Trial balance", subsidiary_id, period_id, consolidated, "", cumulative)

    async def get_netsuite_income_statement(
        subsidiary_id: str, period_id: str, from_period_id: str = "", consolidated: bool = False
    ) -> str:
        """Income statement over a period range (default: the one period)."""
        try:
            start = _int_id(from_period_id or period_id, "from_period_id")
        except ValueError as exc:
            return f"Error: {exc}"
        types = ", ".join(_sql_text(t) for t in _INCOME_STATEMENT_TYPES)
        period_filter = (
            f"ap.startdate >= (SELECT p.startdate FROM accountingperiod p WHERE p.id = {start}) AND " + cumulative
        )
        return await _statement(
            "Income statement (credit balances negative)",
            subsidiary_id,
            period_id,
            consolidated,
            f" AND a.accttype IN ({types})",
            period_filter,
        )

    async def get_netsuite_balance_sheet(subsidiary_id: str, period_id: str, consolidated: bool = False) -> str:
        """Balance sheet at the end of a period."""
        types = ", ".join(_sql_text(t) for t in (*_INCOME_STATEMENT_TYPES, "NonPosting", "Stat"))
        return await _statement(
            "Balance sheet (credit balances negative; current-year earnings not yet closed to equity)",
            subsidiary_id,
            period_id,
            consolidated,
            f" AND a.accttype NOT IN ({types})",
            cumulative,
        )

    async def get_netsuite_aging(ledger: str, as_of: str = "", subsidiary_id: str = "") -> str:
        """AR or AP aging: open amounts by entity in current, 1-30, 31-60, 61-90 and 90+ day buckets."""
        ttype = {"ar": "CustInvc", "ap": "VendBill"}.get((ledger or "").strip().lower())
        if not ttype:
            return "Error: ledger is ar (receivables) or ap (payables)."
        try:
            day = _sql_date(as_of or datetime.now(UTC).strftime("%Y-%m-%d"), "as_of")
            sub = (
                f" AND tl.subsidiary = {_int_id(subsidiary_id, 'subsidiary_id')}"
                if (subsidiary_id or "").strip()
                else ""
            )
        except ValueError as exc:
            return f"Error: {exc}"
        due = f"({day} - NVL(t.duedate, t.trandate))"
        open_amount = "t.foreignamountremaining"
        buckets = (
            f"SUM(CASE WHEN {due} <= 0 THEN {open_amount} ELSE 0 END) AS current_amount, "
            f"SUM(CASE WHEN {due} BETWEEN 1 AND 30 THEN {open_amount} ELSE 0 END) AS d1_30, "
            f"SUM(CASE WHEN {due} BETWEEN 31 AND 60 THEN {open_amount} ELSE 0 END) AS d31_60, "
            f"SUM(CASE WHEN {due} BETWEEN 61 AND 90 THEN {open_amount} ELSE 0 END) AS d61_90, "
            f"SUM(CASE WHEN {due} > 90 THEN {open_amount} ELSE 0 END) AS d90_plus, "
            f"SUM({open_amount}) AS total_open"
        )
        sql = (
            f"SELECT BUILTIN.DF(t.entity) AS entity, BUILTIN.DF(t.currency) AS currency, "
            f"BUILTIN.DF(tl.subsidiary) AS subsidiary, {buckets} "
            "FROM transaction t JOIN transactionline tl ON tl.transaction = t.id AND tl.mainline = 'T' "
            f"WHERE t.type = {_sql_text(ttype)} AND {open_amount} <> 0 AND t.trandate <= {day}{sub} "
            "GROUP BY BUILTIN.DF(t.entity), BUILTIN.DF(t.currency), BUILTIN.DF(tl.subsidiary) "
            "ORDER BY total_open DESC"
        )
        label = "Receivables" if ttype == "CustInvc" else "Payables"
        return await _query_text(
            sql,
            _MAX_ROWS,
            f"read the {label.lower()} aging",
            f"{label} aging as of {as_of or 'today'} (open amounts in the transaction currency; days past due):",
        )

    async def get_netsuite_intercompany_balances(period_id: str, subsidiary_id: str = "") -> str:
        """Intercompany balances at the end of a period by subsidiary and counterparty, to reconcile pairs."""
        try:
            per = _int_id(period_id, "period_id")
            sub = (
                f" AND tl.subsidiary = {_int_id(subsidiary_id, 'subsidiary_id')}"
                if (subsidiary_id or "").strip()
                else ""
            )
        except ValueError as exc:
            return f"Error: {exc}"
        sql = (
            "SELECT BUILTIN.DF(tl.subsidiary) AS subsidiary, BUILTIN.DF(tl.entity) AS counterparty, "
            "a.acctnumber, a.fullname AS account, BUILTIN.DF(s.currency) AS base_currency, "
            "SUM(tal.amount) AS balance "
            "FROM transactionaccountingline tal "
            "JOIN transaction t ON t.id = tal.transaction "
            "JOIN transactionline tl ON tl.transaction = tal.transaction AND tl.id = tal.transactionline "
            "JOIN account a ON a.id = tal.account AND a.eliminate = 'T' "
            "JOIN subsidiary s ON s.id = tl.subsidiary "
            "JOIN accountingperiod ap ON ap.id = t.postingperiod "
            "JOIN accountingbook ab ON ab.id = tal.accountingbook AND ab.isprimary = 'T' "
            f"WHERE tal.posting = 'T' AND {cumulative.format(period=per)}{sub} "
            "GROUP BY BUILTIN.DF(tl.subsidiary), BUILTIN.DF(tl.entity), a.acctnumber, a.fullname, "
            "BUILTIN.DF(s.currency) HAVING SUM(tal.amount) <> 0 ORDER BY subsidiary, counterparty"
        )
        return await _query_text(
            sql,
            _MAX_ROWS,
            "read intercompany balances",
            "Intercompany balances (accounts flagged for elimination) at the end of the period, in each "
            "subsidiary's base currency; a pair reconciles when both sides net to zero after translation:",
        )

    async def list_netsuite_bank_lines(
        from_date: str, to_date: str, account_id: str = "", subsidiary_id: str = "", max_rows: int = _DEFAULT_ROWS
    ) -> str:
        """Posted lines on bank accounts over a date range."""
        try:
            where = [
                f"t.trandate >= {_sql_date(from_date, 'from_date')}",
                f"t.trandate <= {_sql_date(to_date, 'to_date')}",
            ]
            if (account_id or "").strip():
                where.append(f"tal.account = {_int_id(account_id, 'account_id')}")
            if (subsidiary_id or "").strip():
                where.append(f"tl.subsidiary = {_int_id(subsidiary_id, 'subsidiary_id')}")
        except ValueError as exc:
            return f"Error: {exc}"
        sql = (
            "SELECT t.trandate, BUILTIN.DF(t.type) AS type, t.tranid, BUILTIN.DF(tal.account) AS bank_account, "
            "tal.amount AS amount_base, tl.foreignamount AS amount_transaction, BUILTIN.DF(t.currency) AS currency, "
            "BUILTIN.DF(tl.subsidiary) AS subsidiary, BUILTIN.DF(t.entity) AS entity, t.memo, tal.cleared "
            "FROM transactionaccountingline tal "
            "JOIN transaction t ON t.id = tal.transaction "
            "JOIN transactionline tl ON tl.transaction = tal.transaction AND tl.id = tal.transactionline "
            "JOIN account a ON a.id = tal.account AND a.accttype = 'Bank' "
            "JOIN accountingbook ab ON ab.id = tal.accountingbook AND ab.isprimary = 'T' "
            "WHERE tal.posting = 'T' AND " + " AND ".join(where) + " ORDER BY t.trandate, t.id"
        )
        return await _query_text(sql, _rows_arg(max_rows), "list bank lines")

    async def get_netsuite_entity_bank_details(
        entity_id: str, record_type: str = BANK_DETAILS_RECORD, parent_field: str = "custrecord_2663_parent_vendor"
    ) -> str:
        """The bank details of a vendor or customer (Electronic Bank Payments record by default)."""
        try:
            eid = _int_id(entity_id, "entity_id")
        except ValueError as exc:
            return f"Error: {exc}"
        rtype = (record_type or BANK_DETAILS_RECORD).strip().lower()
        field = (parent_field or "custrecord_2663_parent_vendor").strip().lower()
        if not (_FIELD_ID.match(rtype) and _FIELD_ID.match(field)):
            return "Error: record_type and parent_field are NetSuite record and field ids."
        return await _query_text(
            f"SELECT * FROM {rtype} WHERE {field} = {eid}",
            100,
            f"read the bank details of entity {eid}",
            f"Bank details of entity {eid} ({rtype}):",
        )

    # ── Writes ───────────────────────────────────────────────────────────

    def _body_error(body: Any) -> str | None:
        if not isinstance(body, dict) or not body:
            return "Error: body is a JSON object of the record's fields."
        return None

    def _new_id(headers: Mapping[str, str]) -> str:
        location = headers.get("location") or headers.get("Location") or ""
        return location.rstrip("/").rsplit("/", 1)[-1] if location else "?"

    async def _create(rtype: str, body: Any) -> str:
        problem = _body_error(body)
        if problem:
            return problem
        _data, err, headers = await _call("post", f"{base}/record/v1/{rtype}", f"create a {rtype}", body=body)
        if err:
            return err
        new_id = _new_id(headers)
        return f"{rtype} {new_id} created; read it back with get_netsuite_record({rtype}, {new_id})."

    async def _update(rtype: str, record_id: str, body: Any) -> str:
        problem = _body_error(body)
        if problem:
            return problem
        rid = quote(str(record_id).strip(), safe="")
        _data, err, _ = await _call(
            "patch", f"{base}/record/v1/{rtype}/{rid}", f"update {rtype} {record_id}", body=body
        )
        if err:
            return err
        return f"{rtype} {record_id} updated ({', '.join(sorted(body))})."

    def _kind(table: Mapping[str, str], record_type: str) -> str | None:
        return table.get((record_type or "").strip().lower().replace("_", ""))

    def _choices(table: Mapping[str, str]) -> str:
        return ", ".join(sorted(set(table.values())))

    async def create_netsuite_record(record_type: str, body: dict) -> str:
        """Create a master record: customer, vendor, contact, employee, item, segment, subsidiary, account."""
        rtype = _kind(MASTER_RECORDS, record_type)
        if not rtype:
            return (
                f"Error: record_type is one of {_choices(MASTER_RECORDS)}; transactions go through "
                "create_netsuite_transaction."
            )
        return await _create(rtype, body)

    async def update_netsuite_record(record_type: str, record_id: str, body: dict) -> str:
        """Update fields of a master record."""
        rtype = _kind(MASTER_RECORDS, record_type)
        if not rtype:
            return (
                f"Error: record_type is one of {_choices(MASTER_RECORDS)}; transactions go through "
                "update_netsuite_transaction."
            )
        return await _update(rtype, record_id, body)

    async def create_netsuite_transaction(record_type: str, body: dict) -> str:
        """Create a posting transaction (bill, invoice, credit memo, payment, journal, intercompany journal...)."""
        rtype = _kind(POSTING_RECORDS, record_type)
        if not rtype:
            return f"Error: record_type is one of {_choices(POSTING_RECORDS)}."
        return await _create(rtype, body)

    async def update_netsuite_transaction(record_type: str, record_id: str, body: dict) -> str:
        """Update fields of a posting transaction (approval status included)."""
        rtype = _kind(POSTING_RECORDS, record_type)
        if not rtype:
            return f"Error: record_type is one of {_choices(POSTING_RECORDS)}."
        return await _update(rtype, record_id, body)

    async def set_netsuite_entity_bank_details(
        entity_id: str,
        body: dict,
        bank_details_id: str = "",
        record_type: str = BANK_DETAILS_RECORD,
        parent_field: str = "custrecord_2663_parent_vendor",
    ) -> str:
        """Create (no bank_details_id) or update the bank details of a vendor or customer."""
        try:
            eid = _int_id(entity_id, "entity_id")
        except ValueError as exc:
            return f"Error: {exc}"
        rtype = (record_type or BANK_DETAILS_RECORD).strip().lower()
        field = (parent_field or "custrecord_2663_parent_vendor").strip().lower()
        if not (_FIELD_ID.match(rtype) and _FIELD_ID.match(field)):
            return "Error: record_type and parent_field are NetSuite record and field ids."
        problem = _body_error(body)
        if problem:
            return problem
        if (bank_details_id or "").strip():
            return await _update(rtype, bank_details_id, body)
        return await _create(rtype, {field: {"id": str(eid)}, **body})

    async def set_netsuite_period_status(
        period_id: str,
        closed: bool | None = None,
        ap_locked: bool | None = None,
        ar_locked: bool | None = None,
        all_locked: bool | None = None,
    ) -> str:
        """Close, reopen, lock or unlock an accounting period."""
        try:
            per = _int_id(period_id, "period_id")
        except ValueError as exc:
            return f"Error: {exc}"
        body = {
            key: value
            for key, value in (
                ("closed", closed),
                ("apLocked", ap_locked),
                ("arLocked", ar_locked),
                ("allLocked", all_locked),
            )
            if value is not None
        }
        if not body:
            return "Error: set at least one of closed, ap_locked, ar_locked, all_locked."
        result = await _update("accountingPeriod", str(per), body)
        if "INVALID" in result.upper() or "not supported" in result.lower():
            result += (
                "\nThis account does not accept the change through REST: run Period Close Management in the "
                "NetSuite UI, or deploy the companion RESTlet (setup guide) and use call_netsuite_restlet."
            )
        return result

    async def call_netsuite_restlet(
        script_id: str, deploy_id: str, method: str = "GET", payload: Any = None, params: dict | None = None
    ) -> str:
        """Call a RESTlet deployed on the account (sensitive: it runs whatever the script does)."""
        verb = (method or "GET").strip().upper()
        if verb not in ("GET", "POST", "PUT", "DELETE"):
            return "Error: method is GET, POST, PUT or DELETE."
        if not (_SCRIPT_ID.match(str(script_id)) and _SCRIPT_ID.match(str(deploy_id))):
            return "Error: script_id and deploy_id are the RESTlet's script and deployment ids."
        query = {"script": str(script_id), "deploy": str(deploy_id)}
        for key, value in (params or {}).items():
            query[str(key)] = str(value)
        data, err, _ = await _call(
            verb.lower(),
            restlet_url(account),
            f"call RESTlet {script_id}/{deploy_id}",
            query=query,
            body=payload if verb in ("POST", "PUT") else None,
            read=verb == "GET",
        )
        if err:
            if "invalid_scope" in err.lower() or "restlets" in err.lower():
                err += "\nThe integration's OAuth 2.0 scopes must include restlets (OAuth scopes field)."
            return err
        return clip_text(
            f"RESTlet {script_id}/{deploy_id} answered:\n" + json.dumps(data, indent=1, default=str),
            _MAX_OUTPUT,
            "call_netsuite_restlet with narrower params",
        )

    # ── Saved searches and the file cabinet: the companion RESTlet ──────────

    def _companion() -> tuple[str, str] | None:
        if not (companion_script and companion_deploy):
            return None
        return companion_script, companion_deploy

    _no_companion = (
        "Error: saved searches and the file cabinet need the companion RESTlet (integration setup guide): deploy "
        "it, then set its script and deploy ids in the integration's companion RESTlet fields."
    )

    async def _companion_call(verb: str, payload: dict, action: str, read: bool) -> tuple[Any, str | None]:
        ids = _companion()
        if not ids:
            return None, _no_companion
        data, err, _ = await _call(
            verb, restlet_url(account), action, query={"script": ids[0], "deploy": ids[1]}, body=payload, read=read
        )
        if not err and isinstance(data, dict) and data.get("error"):
            err = f"Error: NetSuite companion RESTlet: {data.get('error')}"
        return data, err

    async def run_netsuite_saved_search(search_id: str, max_rows: int = _DEFAULT_ROWS) -> str:
        """Run a saved search by its id (customsearch_...) or internal id."""
        if not _SCRIPT_ID.match(str(search_id)):
            return "Error: search_id is a saved search id such as customsearch_ap_open or its internal id."
        data, err = await _companion_call(
            "post",
            {"action": "search", "id": str(search_id), "limit": _rows_arg(max_rows)},
            f"run saved search {search_id}",
            True,
        )
        if err:
            return err
        rows = [r for r in (data or {}).get("rows", []) if isinstance(r, dict)]
        text = f"Saved search {search_id}: {len(rows)} row(s)" + (" (more remain)" if (data or {}).get("more") else "")
        return clip_text(text + ":\n" + _format_rows(rows), _MAX_OUTPUT, "run_netsuite_saved_search with max_rows")

    async def download_netsuite_file(file_id: str = "", transaction_id: str = "") -> str | ToolResult:
        """Read a file cabinet file, or render a transaction as PDF, through the companion RESTlet."""
        if bool((file_id or "").strip()) == bool((transaction_id or "").strip()):
            return "Error: give file_id (file cabinet) or transaction_id (its PDF), one of them."
        try:
            payload = (
                {"action": "file", "id": _int_id(file_id, "file_id")}
                if (file_id or "").strip()
                else {"action": "pdf", "id": _int_id(transaction_id, "transaction_id")}
            )
        except ValueError as exc:
            return f"Error: {exc}"
        data, err = await _companion_call("post", payload, f"read {payload['action']} {payload['id']}", True)
        if err:
            return err
        name = str((data or {}).get("name") or f"netsuite-{payload['id']}.pdf")
        content = str((data or {}).get("content") or "")
        if not content:
            return f"Error: NetSuite returned no content for {payload['action']} {payload['id']}."
        try:
            raw = base64.b64decode(content, validate=True)
        except ValueError:
            raw = content.encode()
        return await deliver_file(
            data=raw,
            filename=name,
            content_type=file_content_type(str((data or {}).get("type") or ""), name),
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            details=[f"NetSuite {'transaction PDF' if payload['action'] == 'pdf' else 'file'} {payload['id']}"],
        )

    async def attach_netsuite_file(record_type: str, record_id: str, file_id: str) -> str:
        """Attach a file cabinet file to a record (a bill, an invoice, a vendor...)."""
        if not _RECORD_TYPE.match((record_type or "").strip()):
            return "Error: record_type is a NetSuite record type such as vendorbill."
        try:
            rid = _int_id(record_id, "record_id")
            fid = _int_id(file_id, "file_id")
        except ValueError as exc:
            return f"Error: {exc}"
        _data, err = await _companion_call(
            "post",
            {"action": "attach", "type": record_type.strip().lower(), "id": rid, "file": fid},
            f"attach file {fid} to {record_type} {rid}",
            False,
        )
        if err:
            return err
        return f"File {fid} attached to {record_type} {rid}."

    registry = (
        ("run_netsuite_suiteql", run_netsuite_suiteql),
        ("describe_netsuite_records", describe_netsuite_records),
        ("get_netsuite_record", get_netsuite_record),
        ("list_netsuite_records", list_netsuite_records),
        ("list_netsuite_subsidiaries", list_netsuite_subsidiaries),
        ("list_netsuite_accounting_periods", list_netsuite_accounting_periods),
        ("list_netsuite_accounts", list_netsuite_accounts),
        ("list_netsuite_segments", list_netsuite_segments),
        ("list_netsuite_currencies", list_netsuite_currencies),
        ("search_netsuite_entities", search_netsuite_entities),
        ("list_netsuite_items", list_netsuite_items),
        ("list_netsuite_transactions", list_netsuite_transactions),
        ("get_netsuite_transaction", get_netsuite_transaction),
        ("get_netsuite_trial_balance", get_netsuite_trial_balance),
        ("get_netsuite_income_statement", get_netsuite_income_statement),
        ("get_netsuite_balance_sheet", get_netsuite_balance_sheet),
        ("get_netsuite_aging", get_netsuite_aging),
        ("get_netsuite_intercompany_balances", get_netsuite_intercompany_balances),
        ("list_netsuite_bank_lines", list_netsuite_bank_lines),
        ("get_netsuite_entity_bank_details", get_netsuite_entity_bank_details),
        ("run_netsuite_saved_search", run_netsuite_saved_search),
        ("download_netsuite_file", download_netsuite_file),
        ("create_netsuite_record", create_netsuite_record),
        ("update_netsuite_record", update_netsuite_record),
        ("create_netsuite_transaction", create_netsuite_transaction),
        ("update_netsuite_transaction", update_netsuite_transaction),
        ("set_netsuite_entity_bank_details", set_netsuite_entity_bank_details),
        ("set_netsuite_period_status", set_netsuite_period_status),
        ("attach_netsuite_file", attach_netsuite_file),
        ("call_netsuite_restlet", call_netsuite_restlet),
    )
    for name, fn in registry:
        executor.register(name, fn)
    return _definitions()


def _strip_links(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_links(v) for k, v in value.items() if k != "links"}
    if isinstance(value, list):
        return [_strip_links(v) for v in value]
    return value


def _restlet_is_read(arguments: Mapping[str, Any]) -> bool:
    return str(arguments.get("method") or "GET").strip().upper() == "GET"


def _definitions() -> list[ToolDefinition]:
    sub = {"type": "string", "description": "Subsidiary internal id (list_netsuite_subsidiaries)"}
    period = {"type": "string", "description": "Accounting period internal id (list_netsuite_accounting_periods)"}
    consolidated = {
        "type": "boolean",
        "default": False,
        "description": "true: the subsidiary and its children consolidated into its currency",
    }
    rows = {"type": "integer", "default": _DEFAULT_ROWS, "maximum": _MAX_ROWS}
    record_type = {"type": "string", "description": "Record type, e.g. vendorBill, journalEntry, customer"}
    master = {"type": "string", "enum": sorted(set(MASTER_RECORDS.values()))}
    posting = {"type": "string", "enum": sorted(set(POSTING_RECORDS.values()))}

    def write(
        name: str, description: str, properties: dict, required: list[str], *, sensitive: bool = False
    ) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": properties, "required": required},
            source=_SOURCE,
            always_ask=sensitive,
        )

    def read(
        name: str,
        description: str,
        properties: dict,
        required: list[str] | None = None,
        *,
        workspace_ingest: bool = False,
    ) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": properties, "required": required or []},
            source=_SOURCE,
            read_only=True,
            workspace_ingest=workspace_ingest,
        )

    return [
        read(
            "run_netsuite_suiteql",
            "Run a NetSuite SuiteQL SELECT (tables: transaction, transactionline, transactionaccountingline, "
            "account, subsidiary, accountingperiod, customer, vendor...), paged, at most max_rows rows; "
            "BUILTIN.DF(field) gives a display name.",
            {"query": {"type": "string"}, "max_rows": rows},
            ["query"],
        ),
        read(
            "describe_netsuite_records",
            "List NetSuite record types, or the fields of one record type, from the REST metadata catalog.",
            {"record_type": {"type": "string"}},
        ),
        read(
            "get_netsuite_record",
            "Read one NetSuite record by type and internal id through the REST Record API; fields narrows it, "
            "expand includes sublists (lines).",
            {
                "record_type": record_type,
                "record_id": {"type": "string"},
                "fields": {"type": "string", "description": "Comma-separated field ids"},
                "expand": {"type": "boolean", "default": False},
            },
            ["record_type", "record_id"],
        ),
        read(
            "list_netsuite_records",
            "List the internal ids of NetSuite records of a type, optionally filtered with a REST query "
            "(e.g. 'lastModifiedDate AFTER \"2026-01-01\"').",
            {
                "record_type": record_type,
                "q": {"type": "string"},
                "limit": {"type": "integer", "default": 100, "maximum": 1000},
                "offset": {"type": "integer", "default": 0},
            },
            ["record_type"],
        ),
        read(
            "list_netsuite_subsidiaries",
            "List the NetSuite subsidiaries (OneWorld) with their parent, country, base currency and elimination flag.",
            {},
        ),
        read(
            "list_netsuite_accounting_periods",
            "List NetSuite accounting periods with their dates and closed, AP locked, AR locked and all "
            "locked flags, optionally for one year.",
            {"year": {"type": "string"}, "include_adjustments": {"type": "boolean", "default": False}},
        ),
        read(
            "list_netsuite_accounts",
            "List the NetSuite chart of accounts: number, name, type, currency, intercompany flag, "
            "subsidiaries; filter by type (Bank, AcctRec, AcctPay, Expense...) or a search text.",
            {
                "account_type": {"type": "string"},
                "search": {"type": "string"},
                "include_inactive": {"type": "boolean", "default": False},
            },
        ),
        read(
            "list_netsuite_segments",
            "List NetSuite departments, classes or locations with their subsidiaries.",
            {"segment": {"type": "string", "enum": sorted(_SEGMENTS)}},
            ["segment"],
        ),
        read(
            "list_netsuite_currencies",
            "List NetSuite currencies and the exchange rates effective on a date (default today), "
            "optionally for one base currency.",
            {"as_of": {"type": "string", "description": "YYYY-MM-DD"}, "base_currency": {"type": "string"}},
        ),
        read(
            "search_netsuite_entities",
            "Search NetSuite customers or vendors by name, id or email: subsidiary, currency, balance, terms.",
            {
                "entity": {"type": "string", "enum": ["customer", "vendor"]},
                "search": {"type": "string"},
                "subsidiary_id": sub,
                "max_rows": rows,
            },
            ["entity"],
        ),
        read(
            "list_netsuite_items",
            "List NetSuite items with their type and income and expense accounts.",
            {"search": {"type": "string"}, "item_type": {"type": "string"}, "max_rows": rows},
        ),
        read(
            "list_netsuite_transactions",
            "List NetSuite transactions of a type (invoice, vendor_bill, journal_entry, intercompany_journal, "
            "customer_payment...) filtered by subsidiary, period, account, entity, status, dates or minimum "
            "amount; each with amount, currency, subsidiary, period.",
            {
                "transaction_type": {"type": "string", "enum": sorted(TRANSACTION_TYPES)},
                "subsidiary_id": sub,
                "period_id": period,
                "account_id": {"type": "string"},
                "entity_id": {"type": "string"},
                "status": {"type": "string"},
                "from_date": {"type": "string", "description": "YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "YYYY-MM-DD"},
                "min_amount": {"type": "number"},
                "max_rows": rows,
            },
            ["transaction_type"],
        ),
        read(
            "get_netsuite_transaction",
            "Read one NetSuite transaction: header, then every accounting line with account, debit, credit, "
            "base and transaction amounts, subsidiary, department, class and location.",
            {"transaction_id": {"type": "string"}},
            ["transaction_id"],
        ),
        read(
            "get_netsuite_trial_balance",
            "NetSuite trial balance at the end of a period for one subsidiary, or consolidated to it, "
            "every account with its balance in the subsidiary's currency.",
            {"subsidiary_id": sub, "period_id": period, "consolidated": consolidated},
            ["subsidiary_id", "period_id"],
        ),
        read(
            "get_netsuite_income_statement",
            "NetSuite income statement for a subsidiary, or consolidated, over from_period_id to period_id "
            "(default the one period), by account.",
            {"subsidiary_id": sub, "period_id": period, "from_period_id": period, "consolidated": consolidated},
            ["subsidiary_id", "period_id"],
        ),
        read(
            "get_netsuite_balance_sheet",
            "NetSuite balance sheet at the end of a period for a subsidiary, or consolidated, by account.",
            {"subsidiary_id": sub, "period_id": period, "consolidated": consolidated},
            ["subsidiary_id", "period_id"],
        ),
        read(
            "get_netsuite_aging",
            "NetSuite AR or AP aging as of a date: open amounts by entity, currency and subsidiary in current, "
            "1-30, 31-60, 61-90 and 90+ days past due.",
            {"ledger": {"type": "string", "enum": ["ar", "ap"]}, "as_of": {"type": "string"}, "subsidiary_id": sub},
            ["ledger"],
        ),
        read(
            "get_netsuite_intercompany_balances",
            "NetSuite intercompany balances at the end of a period by subsidiary and counterparty on accounts "
            "flagged for elimination, to reconcile subsidiary pairs.",
            {"period_id": period, "subsidiary_id": sub},
            ["period_id"],
        ),
        read(
            "list_netsuite_bank_lines",
            "List posted NetSuite lines on bank accounts between two dates, optionally one account or "
            "subsidiary: amounts, currency, entity, cleared flag.",
            {
                "from_date": {"type": "string"},
                "to_date": {"type": "string"},
                "account_id": {"type": "string"},
                "subsidiary_id": sub,
                "max_rows": rows,
            },
            ["from_date", "to_date"],
        ),
        read(
            "get_netsuite_entity_bank_details",
            "Read the bank details of a NetSuite vendor or customer (Electronic Bank Payments record by default).",
            {
                "entity_id": {"type": "string"},
                "record_type": {"type": "string", "default": BANK_DETAILS_RECORD},
                "parent_field": {"type": "string", "default": "custrecord_2663_parent_vendor"},
            },
            ["entity_id"],
        ),
        read(
            "run_netsuite_saved_search",
            "Run a NetSuite saved search by id (customsearch_...) through the companion RESTlet: its columns and rows.",
            {"search_id": {"type": "string"}, "max_rows": rows},
            ["search_id"],
        ),
        read(
            "download_netsuite_file",
            "Download a NetSuite file cabinet file (file_id) or a transaction rendered as PDF (transaction_id) into "
            "the conversation, through the companion RESTlet.",
            {"file_id": {"type": "string"}, "transaction_id": {"type": "string"}},
            workspace_ingest=True,
        ),
        write(
            "create_netsuite_record",
            "Create a NetSuite master record (customer, vendor, contact, employee, item, department, class, "
            "location, subsidiary, account, currency) from a JSON body of REST Record fields.",
            {"record_type": master, "body": {"type": "object"}},
            ["record_type", "body"],
        ),
        write(
            "update_netsuite_record",
            "Update fields of a NetSuite master record (PATCH of the given fields).",
            {"record_type": master, "record_id": {"type": "string"}, "body": {"type": "object"}},
            ["record_type", "record_id", "body"],
        ),
        write(
            "create_netsuite_transaction",
            "Create a NetSuite posting transaction (vendor bill, invoice, credit memo, vendor or customer payment, "
            "journal entry, intercompany journal, deposit...) from a JSON body. Asks a person before every call.",
            {"record_type": posting, "body": {"type": "object"}},
            ["record_type", "body"],
            sensitive=True,
        ),
        write(
            "update_netsuite_transaction",
            "Update fields of a NetSuite posting transaction (approval status included). Asks a person before "
            "every call.",
            {"record_type": posting, "record_id": {"type": "string"}, "body": {"type": "object"}},
            ["record_type", "record_id", "body"],
            sensitive=True,
        ),
        write(
            "set_netsuite_entity_bank_details",
            "Create (no bank_details_id) or update the bank details of a NetSuite vendor or customer. Asks a "
            "person before every call.",
            {
                "entity_id": {"type": "string"},
                "body": {"type": "object"},
                "bank_details_id": {"type": "string"},
                "record_type": {"type": "string", "default": BANK_DETAILS_RECORD},
                "parent_field": {"type": "string", "default": "custrecord_2663_parent_vendor"},
            },
            ["entity_id", "body"],
            sensitive=True,
        ),
        write(
            "set_netsuite_period_status",
            "Close, reopen, lock or unlock a NetSuite accounting period (closed, ap_locked, ar_locked, all_locked). "
            "Asks a person before every call.",
            {
                "period_id": period,
                "closed": {"type": "boolean"},
                "ap_locked": {"type": "boolean"},
                "ar_locked": {"type": "boolean"},
                "all_locked": {"type": "boolean"},
            },
            ["period_id"],
            sensitive=True,
        ),
        write(
            "attach_netsuite_file",
            "Attach a NetSuite file cabinet file to a record (bill, invoice, vendor...) through the companion RESTlet.",
            {"record_type": {"type": "string"}, "record_id": {"type": "string"}, "file_id": {"type": "string"}},
            ["record_type", "record_id", "file_id"],
        ),
        ToolDefinition(
            name="call_netsuite_restlet",
            description=(
                "Call a RESTlet deployed on the NetSuite account by script and deploy id (GET reads; POST, PUT, "
                "DELETE run whatever the script does and ask a person first). Needs the restlets scope."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "script_id": {"type": "string"},
                    "deploy_id": {"type": "string"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"], "default": "GET"},
                    "payload": {"type": "object"},
                    "params": {"type": "object"},
                },
                "required": ["script_id", "deploy_id"],
            },
            source=_SOURCE,
            always_ask=True,
            read_only_call=_restlet_is_read,
        ),
    ]


async def validate_connection(token: str) -> tuple[bool, str]:
    """Test Connection: one SuiteQL query naming the subsidiaries the role reaches."""
    fields = parse_token(token)
    if not fields.get("account_id"):
        return False, "Missing the NetSuite account ID."
    executor = CallableToolExecutor()
    if not build_tools(token, executor):
        return False, "Incomplete NetSuite credentials."
    if fields["mode"] == "m2m":
        _access, err = await m2m_access_token(fields)
        if err:
            return False, err.removeprefix("Error: ")
    result = await executor.execute_tool(ToolCall(id="netsuite-test", name="list_netsuite_subsidiaries", arguments={}))
    text = str(result.content or "")
    if text.startswith("Error"):
        return False, text.removeprefix("Error: ")
    first = text.splitlines()[0] if text else ""
    mode = "OAuth 2.0 machine-to-machine" if fields["mode"] == "m2m" else "Token-Based Authentication"
    return True, f"Connected to NetSuite account {account_realm(fields['account_id'])} ({mode}): {first}"

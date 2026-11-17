# SPDX-License-Identifier: Apache-2.0

"""Databricks integration - lakehouse SQL, Unity Catalog, Jobs, AI and files.

Full-fledged Databricks Data Intelligence Platform integration exposing a
workspace to an agentic team. Databricks unifies the data warehouse (SQL
warehouses + Delta tables), the governance layer (Unity Catalog), the
orchestration layer (Jobs / Workflows on job clusters) and the AI layer
(Mosaic AI Model Serving + Vector Search) behind one REST surface on a single
workspace host.

Tool surface (this module):
  - SQL: run a guarded read-only query (``query_databricks_sql``), run
    arbitrary SQL including DML/DDL (``execute_databricks_sql``), fetch a
    long-running statement's outcome later (``get_databricks_statement``) and
    cancel one (``cancel_databricks_statement``); list the SQL warehouses that
    execute them (``list_databricks_warehouses``).
  - Unity Catalog: browse catalogs / schemas / tables
    (``list_databricks_catalogs``, ``list_databricks_schemas``,
    ``list_databricks_tables``) and read a table's full column schema, owner
    and storage details (``describe_databricks_table``).
  - Jobs & Workflows: list and inspect jobs (``list_databricks_jobs``,
    ``get_databricks_job``), trigger one with parameters
    (``run_databricks_job``), follow its runs including the failing task's
    error (``list_databricks_job_runs``, ``get_databricks_job_run``), cancel a
    run (``cancel_databricks_job_run``), and inspect the compute
    (``list_databricks_clusters``, ``get_databricks_cluster``).
  - Mosaic AI: list and inspect Model Serving endpoints
    (``list_databricks_serving_endpoints``, ``get_databricks_serving_endpoint``),
    invoke one (``query_databricks_serving_endpoint``), and run similarity
    search over a Vector Search index (``list_databricks_vector_indexes``,
    ``query_databricks_vector_index``).
  - Files: list Unity Catalog volumes (``list_databricks_volumes``), browse a
    volume, DBFS or workspace path (``list_databricks_files``), read a file's
    content (``read_databricks_file``), store a whole volume / DBFS file or a
    workspace export as a file of the run (``download_databricks_file``) and
    write any file of the run to them (``upload_databricks_file``).

Authentication
--------------
Both credential styles Databricks supports are handled, and which one is used
is inferred from whether a service principal client ID was configured:

* **Personal access token (PAT)** - the token is sent as
  ``Authorization: Bearer <pat>``. Generated in the workspace under Settings >
  Developer > Access tokens.
* **OAuth machine-to-machine (M2M)** - a service principal's client ID +
  OAuth secret are exchanged for a short-lived workspace token at
  ``{host}/oidc/v1/token`` (``grant_type=client_credentials``, scope
  ``all-apis``, HTTP Basic credentials) and that token is then used as the
  bearer. Minted tokens are cached in-process keyed by a hash of the
  credentials, so a multi-tool agentic run reuses one token instead of
  re-minting on every call (mirrors the Wiz / Picus app-token cache).

The compound ``host||client_id||warehouse_id||secret`` token is assembled by
``integration_tools._get_token``: the workspace host, the (optional) service
principal client ID and the (optional) default SQL warehouse ID live in
``extra_config``, and the secret - the PAT *or* the OAuth secret depending on
whether a client ID is set - lives in ``api_key_enc``. The secret is the
FINAL, greedy segment so a value containing ``||`` still round-trips.

The stored host is the workspace URL, which serves both the REST API and the
web UI, so it is safe to surface to agents as a deep-link target.

Read-only SQL
-------------
``query_databricks_sql`` refuses anything that is not a pure read. The guard
strips comments and string/backtick literals first, then requires a leading
``SELECT`` / ``WITH`` / ``SHOW`` / ``DESCRIBE`` / ``EXPLAIN`` / ``VALUES`` and
rejects any mutating keyword surviving in the stripped text. It is deliberately
conservative (``EXPLAIN DELETE …`` is refused) - agents that genuinely need to
mutate the lakehouse use ``execute_databricks_sql``, which is classified as a
write tool and can therefore be withheld by tool scoping.

Rate limits: Databricks applies per-workspace and per-endpoint limits and
answers HTTP 429 when exceeded - requests ride the shared ``safe_request``
retry with decorrelated jitter, and idempotent reads additionally opt into the
transient 502/503/504 retry.

Exports ``resolve_workspace_url()`` and ``validate_connection()`` shared with
the connectivity test.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import (
    PersistFileFn,
    deliver_file,
    fetch_file,
    file_content_type,
    read_pipe_file,
    uploaded_file_answer,
)
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    app_token_cache_key,
    format_entity_list,
    format_pagination_hint,
    get_cached_app_token,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)
from app.services.content_safety import single_line
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "Databricks"
_SOURCE = "integration:databricks"

_DEFAULT_TIMEOUT = 60
_TOKEN_TIMEOUT = 20
# Model Serving invocations can legitimately take a while (large generations,
# scale-to-zero cold starts), so they get their own, longer budget.
_INVOKE_TIMEOUT = 300

# OAuth M2M: the workspace token endpoint and the all-encompassing scope
# Databricks documents for service principals.
_OIDC_TOKEN_PATH = "/oidc/v1/token"  # noqa: S105 — a URL path, not a secret
_OAUTH_SCOPE = "all-apis"
# Workspace OAuth tokens live ~1 h; fall back to a conservative 50 min when the
# token endpoint omits ``expires_in``. The shared cache applies its own skew.
_OAUTH_DEFAULT_TTL = 3000

# Statement Execution API: the server-side wait budget on the initial POST
# (50 s is the documented maximum), then client-side polling up to
# ``_SQL_MAX_POLL_SECONDS`` before handing the statement id back to the agent.
_SQL_WAIT_TIMEOUT = "50s"
_SQL_POLL_INTERVAL = 2.0
_SQL_MAX_POLL_SECONDS = 120.0

# Result-shaping caps. Rows and cells are bounded so a wide SELECT can never
# blow up the agent's context window.
_MAX_ROWS = 100
_MAX_ROWS_HARD = 500
_MAX_CELL_CHARS = 200
_MAX_LIST_RESULTS = 100
# Cap on the bytes returned by ``read_databricks_file`` (a text preview, not a
# transfer mechanism).
_MAX_FILE_BYTES = 100_000
_MAX_FILE_BYTES_HARD = 500_000
# A download / upload stores the whole file. The Workspace API
# exports and imports at most 10 MB per request, DBFS reads and writes 1 MB
# blocks.
_MAX_TRANSFER_BYTES = 100 * 1024 * 1024
_MAX_WORKSPACE_BYTES = 10 * 1024 * 1024
_DBFS_BLOCK_BYTES = 1024 * 1024
_FILE_TIMEOUT = 300
# Workspace export format -> file extension (SOURCE keeps the language's own).
_EXPORT_FORMATS = {
    "SOURCE": "",
    "HTML": "html",
    "JUPYTER": "ipynb",
    "DBC": "dbc",
    "R_MARKDOWN": "Rmd",
    "AUTO": "",
}

# Terminal statement states from the Statement Execution API.
_SQL_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"})

# --- Read-only SQL guard ---------------------------------------------------
# Statements whose leading keyword makes them a pure read.
_READ_ONLY_LEADING = frozenset(
    {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "VALUES", "TABLE"}
)
# Keywords that must not survive comment/literal stripping in a read-only
# statement. Identifiers legitimately named after one of these are protected by
# the backtick stripping in ``_strip_sql_noise``.
#
# Deliberately EXCLUDED because they are ordinary SQL vocabulary that appears
# inside genuine reads, and their dangerous forms are already caught by the
# leading-keyword check: ``REPLACE`` (a string function; ``CREATE OR REPLACE``
# leads with CREATE), ``COMMENT`` (a very common column name; ``COMMENT ON``
# leads with COMMENT, which is not an allowed leading keyword) and ``LOAD``
# (``load_time``-style columns; ``LOAD DATA`` leads with LOAD).
_MUTATING_KEYWORDS = frozenset(
    {
        "ALTER",
        "ANALYZE",
        "CACHE",
        "CALL",
        "COPY",
        "CREATE",
        "DELETE",
        "DENY",
        "DROP",
        "EXECUTE",
        "GRANT",
        "INSERT",
        "MERGE",
        "MSCK",
        "OPTIMIZE",
        "REFRESH",
        "RESET",
        "RESTORE",
        "REVOKE",
        "SET",
        "TRUNCATE",
        "UNCACHE",
        "UPDATE",
        "UPSERT",
        "USE",
        "VACUUM",
    }
)

# Leading keywords whose every Spark SQL form is a read, so the "no mutating
# keyword anywhere" scan is skipped for them. Without this exemption legitimate
# introspection like ``SHOW CREATE TABLE t`` or ``SHOW GRANT ON …`` would be
# refused for containing CREATE / GRANT.
_SELF_EVIDENT_READ_LEADING = frozenset({"SHOW", "DESCRIBE", "DESC"})

# ``--`` line comments, ``/* */`` block comments, and the three literal
# delimiters Databricks SQL accepts. Applied in order by ``_strip_sql_noise``.
_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n\r]*")
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_SQL_LITERAL_RE = re.compile(
    r"'(?:[^'\\]|\\.|'')*'"  # single-quoted string
    r"|\"(?:[^\"\\]|\\.|\"\")*\""  # double-quoted identifier/string
    r"|`(?:[^`]|``)*`",  # backtick-quoted identifier
    re.DOTALL,
)
_SQL_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_JOB_RUN_LIFE_CYCLE_ICONS: dict[str, str] = {
    "TERMINATED": "✅",
    "INTERNAL_ERROR": "❌",
    "SKIPPED": "⏭️",
    "RUNNING": "▶️",
    "PENDING": "⏳",
    "QUEUED": "⏳",
    "BLOCKED": "⏸️",
    "WAITING_FOR_RETRY": "⏳",
    "TERMINATING": "⏹️",
}

_JOB_RESULT_STATE_ICONS: dict[str, str] = {
    "SUCCESS": "✅",
    "SUCCESS_WITH_FAILURES": "⚠️",
    "FAILED": "❌",
    "TIMEDOUT": "⏱️",
    "CANCELED": "\U0001f6d1",
    "MAXIMUM_CONCURRENT_RUNS_REACHED": "⚠️",
    "EXCLUDED": "⏭️",
    "UPSTREAM_FAILED": "❌",
    "UPSTREAM_CANCELED": "\U0001f6d1",
}


# ---------------------------------------------------------------------------
# Workspace URL + credential helpers
# ---------------------------------------------------------------------------


def resolve_workspace_url(workspace_url: str) -> str:
    """Normalise a Databricks workspace URL to a bare ``https://host`` origin.

    Accepts every form a user is likely to paste - the browser URL of the
    workspace, a value with a trailing slash, a bare host, or a URL carrying a
    deep-link path / query / fragment. A raw ``http://`` URL is upgraded to
    ``https://`` (requests carry the bearer token and must never travel in
    cleartext). There is NO default: the host is workspace-specific (e.g.
    ``https://dbc-1234abcd-5e6f.cloud.databricks.com``,
    ``https://adb-1234567890123456.7.azuredatabricks.net``), so an empty value
    stays empty and surfaces a clear call-time configuration error.
    """
    value = (workspace_url or "").strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered.startswith("http://"):
        logger.warning(
            "Databricks workspace URL was supplied over http://; upgrading to "
            "https:// so the access token is not sent in cleartext."
        )
        value = "https://" + value[len("http://") :]
    elif not lowered.startswith("https://"):
        value = "https://" + value
    # Keep only the origin - a pasted deep link (``/sql/warehouses/abc``) or a
    # query string would otherwise be prefixed onto every API path.
    for separator in ("?", "#"):
        value = value.split(separator, 1)[0]
    scheme, _, rest = value.partition("://")
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}".rstrip("/") if host else ""


def _auth_error(client_id: str) -> str:
    """Credential-specific authentication guidance for the agent."""
    if client_id:
        return (
            "Error: Failed to authenticate with Databricks using OAuth "
            "machine-to-machine credentials. Check that the service principal "
            "client ID and OAuth secret are correct and not revoked (workspace "
            "Settings > Identity and access > Service principals > the "
            "principal > Secrets), and that the service principal has been "
            "granted access to this workspace."
        )
    return (
        "Error: Failed to authenticate with Databricks. Check that the "
        "personal access token is correct and has not expired (workspace "
        "Settings > Developer > Access tokens)."
    )


async def request_oauth_token(
    host: str, client_id: str, client_secret: str
) -> tuple[str | None, int]:
    """Exchange service-principal credentials for a workspace OAuth token.

    Posts a ``client_credentials`` grant to ``{host}/oidc/v1/token`` with the
    credentials in an HTTP Basic header (the form Databricks documents for
    OAuth M2M). Returns ``(access_token, expires_in_seconds)`` on success, or
    ``(None, 0)`` on any failure - the caller turns that into the
    credential-specific :func:`_auth_error` message.
    """
    if not (host and client_id and client_secret):
        return None, 0
    try:
        async with egress_client(timeout=_TOKEN_TIMEOUT) as client:
            resp = await client.post(
                f"{host}{_OIDC_TOKEN_PATH}",
                data={"grant_type": "client_credentials", "scope": _OAUTH_SCOPE},
                auth=(client_id, client_secret),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "Databricks OAuth token request failed (%s)",
            format_exception_message(exc),
        )
        return None, 0
    if not resp.is_success:
        logger.warning(
            "Databricks OAuth token request returned HTTP %s", resp.status_code
        )
        return None, 0
    try:
        data = resp.json()
    except ValueError:
        logger.warning("Databricks OAuth token response was not valid JSON")
        return None, 0
    if not isinstance(data, dict):
        return None, 0
    token = data.get("access_token")
    if not token or not isinstance(token, str):
        logger.warning("Databricks OAuth token response contained no token")
        return None, 0
    expires_in = data.get("expires_in")
    ttl = expires_in if isinstance(expires_in, int) and expires_in > 0 else _OAUTH_DEFAULT_TTL
    return token, ttl


async def validate_connection(
    workspace_url: str, client_id: str, secret: str
) -> tuple[bool, str]:
    """Verify Databricks credentials against a workspace.

    Returns ``(ok, message)``. When *client_id* is set the OAuth M2M exchange
    runs first; either way the resulting bearer token is probed against the
    SCIM ``Me`` endpoint, which every credential can reach and which confirms
    the host is a real workspace API (a 2xx body that is not a JSON object is
    a login/SSO/proxy page, not a success). Used by the integration
    connectivity test.
    """
    host = resolve_workspace_url(workspace_url)
    client_id = (client_id or "").strip()
    secret = (secret or "").strip()
    if not host:
        return False, (
            "Missing workspace URL. Enter your Databricks workspace URL (e.g. "
            "https://dbc-1234abcd-5e6f.cloud.databricks.com) in extra config."
        )
    if not secret:
        return False, (
            "Missing credential. Enter the personal access token - or the "
            "service principal's OAuth secret when a client ID is configured - "
            "in the API Key field."
        )
    bearer = secret
    if client_id:
        bearer, _ttl = await request_oauth_token(host, client_id, secret)
        if not bearer:
            return False, (
                "Authentication failed - could not obtain a Databricks OAuth "
                "token. Check the service principal client ID and OAuth secret, "
                "and that the workspace URL matches the principal's workspace."
            )
    try:
        async with egress_client(timeout=_TOKEN_TIMEOUT) as client:
            resp = await client.get(
                f"{host}/api/2.0/preview/scim/v2/Me",
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        return False, (
            "Could not reach the Databricks workspace "
            f"({format_exception_message(exc)}) - check the workspace URL."
        )
    if resp.status_code in (401, 403):
        return False, (
            "Databricks rejected the credential (HTTP "
            f"{resp.status_code}) - check that it is valid for this workspace "
            "and has not been revoked."
        )
    if not resp.is_success:
        return False, (
            f"Databricks returned HTTP {resp.status_code} - check that the "
            "workspace URL points at the workspace itself (e.g. "
            "https://dbc-1234abcd-5e6f.cloud.databricks.com), not a proxy or "
            "an account-console URL."
        )
    try:
        body = resp.json()
    except ValueError:
        return False, (
            "Reached the URL, but it returned a non-JSON response - check that "
            "the workspace URL points at the Databricks workspace API, not a "
            "login page or proxy."
        )
    if not isinstance(body, dict):
        return False, (
            "Reached the URL, but the response was not a Databricks API "
            "object - check the workspace URL."
        )
    who = body.get("userName") or body.get("displayName") or ""
    suffix = f" (authenticated as {who})" if who else ""
    return True, f"Databricks is connected and authenticated{suffix}."


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------


class DatabricksIntegration(BaseIntegration):
    """Databricks Data Intelligence Platform integration."""

    provider_name = "databricks"
    display_name = "Databricks"
    icon = "Database"
    description = (
        "Connect Databricks to query the lakehouse with SQL, browse Unity "
        "Catalog, run and monitor Jobs, invoke Model Serving and Vector Search "
        "endpoints, and read files from Unity Catalog volumes."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        # Databricks OAuth here is the machine-to-machine (client-credentials)
        # flow against the workspace token endpoint, not a user-consent
        # authorization-code flow, so there is no authorize URL to pre-fill.
        return None

    async def validate_connection(self, token: str) -> bool:
        host, client_id, _warehouse_id, secret = parse_compound_token(
            token, 4, greedy_last=True
        )
        ok, _msg = await validate_connection(host, client_id, secret)
        return ok

    async def get_user_info(self, token: str) -> dict:
        host, client_id, _warehouse_id, secret = (
            part.strip() for part in parse_compound_token(token, 4, greedy_last=True)
        )
        host = resolve_workspace_url(host)
        if not (host and secret):
            return {}
        bearer = secret
        if client_id:
            bearer, _ttl = await request_oauth_token(host, client_id, secret)
            if not bearer:
                return {"client_id": client_id}
        try:
            async with egress_client(timeout=_TOKEN_TIMEOUT) as client:
                resp = await client.get(
                    f"{host}/api/2.0/preview/scim/v2/Me",
                    headers={"Authorization": f"Bearer {bearer}"},
                )
                if not resp.is_success:
                    return {}
                body = resp.json()
        except (httpx.HTTPError, ValueError):
            return {}
        if not isinstance(body, dict):
            return {}
        return {
            "id": body.get("id", ""),
            "username": body.get("userName", ""),
            "display_name": body.get("displayName", ""),
            "workspace_url": host,
        }


# ---------------------------------------------------------------------------
# Read-only SQL guard
# ---------------------------------------------------------------------------


def _strip_sql_noise(sql: str) -> str:
    """Remove comments and quoted literals/identifiers from *sql*.

    Everything removed is replaced by a single space so adjacent keywords
    cannot be glued into one token. Block comments are stripped before line
    comments so a ``--`` inside ``/* … */`` cannot swallow the closing
    delimiter, and literals last so a quote inside a comment is inert.
    """
    stripped = _SQL_BLOCK_COMMENT_RE.sub(" ", sql)
    stripped = _SQL_LINE_COMMENT_RE.sub(" ", stripped)
    return _SQL_LITERAL_RE.sub(" ", stripped)


def check_read_only_sql(sql: str) -> str | None:
    """Return an error message if *sql* is not a pure read, else ``None``.

    Deliberately conservative: the check runs on the comment- and
    literal-stripped text, requires a leading read keyword, and refuses any
    mutating keyword anywhere in what remains — except after a
    self-evidently-read leading keyword (``SHOW`` / ``DESCRIBE``), where every
    Spark SQL form is a read. A column or table literally named ``update`` is
    fine as long as it is backtick-quoted (the standard Databricks way to quote
    such an identifier).
    """
    if not (sql or "").strip():
        return "Error: statement is required."
    stripped = _strip_sql_noise(sql)
    words = _SQL_WORD_RE.findall(stripped)
    if not words:
        return (
            "Error: no SQL statement found (the input is only comments or "
            "string literals)."
        )
    leading = words[0].upper()
    if leading not in _READ_ONLY_LEADING:
        return (
            f"Error: query_databricks_sql only runs read-only statements, and "
            f"this one starts with '{leading}'. Allowed: "
            f"{', '.join(sorted(_READ_ONLY_LEADING))}. Use "
            "execute_databricks_sql for statements that modify data or schema."
        )
    if leading in _SELF_EVIDENT_READ_LEADING:
        return None
    found = sorted({w.upper() for w in words if w.upper() in _MUTATING_KEYWORDS})
    if found:
        return (
            "Error: query_databricks_sql refuses statements containing "
            f"{', '.join(found)} because they can modify data or session "
            "state. Use execute_databricks_sql for that, or backtick-quote the "
            "identifier if it is only a column/table name."
        )
    return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _page_empty_msg(noun: str, next_token: Any) -> str:
    """Empty-page wording that defers to the pagination hint when one follows.

    The Unity Catalog list APIs document that a page may contain zero results
    while still returning a ``next_page_token``, and that the token's *absence*
    is the only end-of-results signal. Rendering "No catalogs found." and then
    appending "[More results available - pass page_token=...]" is a
    contradiction, and the agent resolves it by believing the first half and
    stopping on the first sparse page.
    """
    if next_token:
        return (
            f"No {noun}s on this page, but more pages follow - keep paging with "
            "the token below (an empty page is normal; only a missing token "
            "means the end)."
        )
    return f"No {noun}s found."


def _clamp(value: Any, default: int, lo: int = 1, hi: int = _MAX_LIST_RESULTS) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


def _trunc(text: Any, max_len: int = _MAX_CELL_CHARS) -> str:
    if text is None:
        return "NULL"
    text = str(text)
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _ms(value: Any) -> str:
    """Render an epoch-milliseconds timestamp as ``YYYY-MM-DD HH:MM`` UTC."""
    try:
        seconds = int(value) / 1000
    except (TypeError, ValueError):
        return "N/A"
    if seconds <= 0:
        return "N/A"
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%d %H:%M")


def _duration(ms_value: Any) -> str:
    """Render a millisecond duration compactly (``1h 04m``, ``12s``)."""
    try:
        total = int(ms_value)
    except (TypeError, ValueError):
        return "N/A"
    if total <= 0:
        return "N/A"
    seconds, hours = total // 1000, total // 3_600_000
    if hours:
        return f"{hours}h {(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds}s"


def _path_segment(value: str) -> str:
    """Percent-encode a single URL path segment (``/`` escaped)."""
    return quote(str(value or "").strip(), safe="")


def _file_path(value: str) -> str:
    """Percent-encode a Files API path, preserving its ``/`` separators."""
    path = str(value or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    return quote(path, safe="/")


def _parse_json_arg(raw: str, param: str) -> tuple[Any, str | None]:
    """Parse a JSON-string tool argument, returning ``(value, error)``."""
    text = (raw or "").strip()
    if not text:
        return None, None
    try:
        return json.loads(text), None
    except ValueError as exc:
        return None, (
            f"Error: {param} must be valid JSON ({format_exception_message(exc)})."
        )


def _sql_parameters(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a ``{name: value}`` mapping to Statement API parameter objects."""
    params: list[dict[str, Any]] = []
    for name, value in raw.items():
        entry: dict[str, Any] = {"name": str(name)}
        if value is not None:
            entry["value"] = value if isinstance(value, str) else json.dumps(value)
        params.append(entry)
    return params


def _statement_error(payload: dict[str, Any]) -> str:
    """Extract a readable message from a statement's ``status.error``."""
    status = payload.get("status")
    if not isinstance(status, dict):
        return ""
    error = status.get("error")
    if not isinstance(error, dict):
        return ""
    code = str(error.get("error_code") or "").strip()
    message = str(error.get("message") or "").strip()
    if code and message:
        return f"{code}: {_trunc(message, 800)}"
    return _trunc(code or message, 800)


def format_statement_result(payload: dict[str, Any], max_rows: int) -> str:
    """Render a terminal statement payload as a compact, bounded text table."""
    statement_id = str(payload.get("statement_id") or "?")
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    state = str(status.get("state") or "UNKNOWN")
    if state != "SUCCEEDED":
        detail = _statement_error(payload)
        return (
            f"Statement {statement_id} finished with state {state}."
            + (f"\n{detail}" if detail else "")
        )

    manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else {}
    schema = manifest.get("schema") if isinstance(manifest.get("schema"), dict) else {}
    columns = [c for c in (schema.get("columns") or []) if isinstance(c, dict)]
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    rows = [r for r in (result.get("data_array") or []) if isinstance(r, list)]

    if not columns and not rows:
        # DML/DDL and other statements that return no result set.
        return f"Statement {statement_id} succeeded (no rows returned)."

    header = " | ".join(
        f"{c.get('name', '?')} ({c.get('type_text', '?')})" for c in columns
    ) or "(no columns)"
    shown = rows[:max_rows]
    body = "\n".join(
        "  " + " | ".join(_trunc(cell) for cell in row) for row in shown
    )
    total = manifest.get("total_row_count")
    total_text = total if isinstance(total, int) and total >= 0 else len(rows)
    lines = [
        f"Statement {statement_id} succeeded - {total_text} row(s), "
        f"{len(columns)} column(s).",
        f"Columns: {header}",
    ]
    if body:
        lines.append(body)
    else:
        lines.append("  (no rows)")
    if len(rows) > len(shown):
        lines.append(
            f"[Showing {len(shown)} of {len(rows)} fetched rows - raise limit "
            "or narrow the query.]"
        )
    if manifest.get("truncated") or result.get("next_chunk_internal_link"):
        lines.append(
            "[Databricks truncated the result set - add a LIMIT or a more "
            "selective WHERE clause to see the rest.]"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Databricks tools from a compound credential token.

    ``persist_file_fn`` (injected by ``integration_tools``) stores a
    downloaded volume, DBFS or workspace file as a file of the run.

    ``token`` is ``host||client_id||warehouse_id||secret`` (the secret is the
    final, greedy segment). ``client_id`` is empty for PAT auth and set for
    OAuth M2M; ``warehouse_id`` is the optional default SQL warehouse. Missing
    credentials still build the full tool surface -
    ``integration_tools._get_token`` returns a partial compound token so a
    misconfiguration surfaces as a clear call-time error instead of the tools
    silently disappearing (mirrors wiz / cybersixgill / picus).
    """
    raw_host, client_id, default_warehouse_id, secret = (
        part.strip() for part in parse_compound_token(token, 4, greedy_last=True)
    )
    host = resolve_workspace_url(raw_host)
    cache_key = app_token_cache_key("databricks", host, client_id, secret)

    def _creds_error() -> str | None:
        if not host:
            return (
                "Error: the Databricks workspace URL is not configured. Enter "
                "it in the integration's extra config (e.g. "
                "https://dbc-1234abcd-5e6f.cloud.databricks.com)."
            )
        if not secret:
            return (
                "Error: the Databricks credential is not configured. Enter the "
                "personal access token - or the service principal's OAuth "
                "secret when a client ID is set - in the API Key field."
            )
        return None

    async def _bearer() -> str | None:
        """Return the bearer token, minting/reusing an OAuth one when needed."""
        if not client_id:
            return secret or None
        cached = get_cached_app_token(cache_key)
        if cached:
            return cached
        access, ttl = await request_oauth_token(host, client_id, secret)
        if access:
            set_cached_app_token(cache_key, access, ttl)
        return access

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        timeout: int = _DEFAULT_TIMEOUT,
        idempotent: bool = True,
        **kwargs: Any,
    ) -> tuple[Any, str | None]:
        """Call a workspace REST endpoint, returning ``(parsed_json, error)``.

        Reads opt into the shared transient 502/503/504 retry; mutations never
        do, so a write can't be applied twice behind a failing gateway.
        """
        creds_err = _creds_error()
        if creds_err:
            return None, creds_err
        bearer = await _bearer()
        if not bearer:
            return None, _auth_error(client_id)
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
        }
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{host}{path}",
                _PROVIDER,
                action,
                headers=headers,
                retry_statuses=TRANSIENT_RETRY_STATUSES if idempotent else None,
                **kwargs,
            )
            if err:
                return None, err
            if not resp.content:
                return {}, None
            try:
                return resp.json(), None
            except ValueError:
                logger.warning(
                    "Databricks returned a non-JSON body for %s (status %s)",
                    action,
                    resp.status_code,
                )
                return None, (
                    f"Error: {_PROVIDER} returned a non-JSON response for "
                    f"'{action}'. Check that the workspace URL points at the "
                    "workspace API and not at a login page or proxy."
                )

    async def _raw(
        method: str, path: str, action: str, *, idempotent: bool = True, **kwargs: Any
    ) -> tuple[httpx.Response | None, str | None]:
        """Call a workspace endpoint for raw bytes (a file body, not JSON)."""
        creds_err = _creds_error()
        if creds_err:
            return None, creds_err
        bearer = await _bearer()
        if not bearer:
            return None, _auth_error(client_id)
        headers = {"Authorization": f"Bearer {bearer}", **kwargs.pop("headers", {})}
        async with egress_client(timeout=_FILE_TIMEOUT) as client:
            return await safe_request(
                client,
                method,
                f"{host}{path}",
                _PROVIDER,
                action,
                headers=headers,
                retry_statuses=TRANSIENT_RETRY_STATUSES if idempotent else None,
                **kwargs,
            )

    async def _api_dict(
        method: str, path: str, action: str, **kwargs: Any
    ) -> tuple[dict[str, Any], str | None]:
        """``_api`` variant that also enforces a JSON *object* response."""
        data, err = await _api(method, path, action, **kwargs)
        if err:
            return {}, err
        if not isinstance(data, dict):
            return {}, (
                f"Error: {_PROVIDER} returned an unexpected response shape for "
                f"'{action}' (expected a JSON object)."
            )
        return data, None

    def _resolve_warehouse(warehouse_id: str) -> tuple[str, str | None]:
        """Pick the explicit warehouse, else the configured default."""
        chosen = (warehouse_id or "").strip() or default_warehouse_id
        if not chosen:
            return "", (
                "Error: no SQL warehouse specified. Pass warehouse_id (see "
                "list_databricks_warehouses), or set a default warehouse ID in "
                "the integration's extra config."
            )
        return chosen, None

    async def _await_statement(
        payload: dict[str, Any], action: str
    ) -> tuple[dict[str, Any], str | None]:
        """Poll a statement to a terminal state (bounded), returning its payload."""
        waited = 0.0
        while True:
            status = payload.get("status")
            state = ""
            if isinstance(status, dict):
                state = str(status.get("state") or "")
            if state in _SQL_TERMINAL_STATES:
                return payload, None
            statement_id = str(payload.get("statement_id") or "").strip()
            if not statement_id:
                return {}, (
                    f"Error: {_PROVIDER} did not return a statement id for "
                    f"'{action}'."
                )
            if waited >= _SQL_MAX_POLL_SECONDS:
                return {}, (
                    f"The statement is still {state or 'running'} after "
                    f"{int(_SQL_MAX_POLL_SECONDS)}s. It keeps running on the "
                    f"warehouse - call get_databricks_statement with "
                    f"statement_id='{statement_id}' to collect the result, or "
                    "cancel_databricks_statement to stop it."
                )
            await asyncio.sleep(_SQL_POLL_INTERVAL)
            waited += _SQL_POLL_INTERVAL
            payload, err = await _api_dict(
                "get",
                f"/api/2.0/sql/statements/{_path_segment(statement_id)}",
                action,
            )
            if err:
                return {}, err

    async def _run_statement(
        statement: str,
        warehouse_id: str,
        catalog: str,
        schema: str,
        row_limit: int,
        parameters: str,
        action: str,
    ) -> str:
        """Shared execution path for the read-only and unrestricted SQL tools."""
        warehouse, err = _resolve_warehouse(warehouse_id)
        if err:
            return err
        rows = _clamp(row_limit, _MAX_ROWS, hi=_MAX_ROWS_HARD)
        body: dict[str, Any] = {
            "statement": statement,
            "warehouse_id": warehouse,
            "wait_timeout": _SQL_WAIT_TIMEOUT,
            "on_wait_timeout": "CONTINUE",
            "format": "JSON_ARRAY",
            "disposition": "INLINE",
            # An INLINE response is capped at 25 MiB and a result set that
            # exceeds it does NOT come back truncated - Databricks cancels the
            # statement and the request fails, so an unbounded `SELECT * FROM
            # big_table` would error instead of returning its first rows. Bound
            # the result set on the server at the number of rows that would be
            # rendered anyway. Unlike a SQL LIMIT clause, `row_limit` also sets
            # the manifest's `truncated` flag, which `format_statement_result`
            # already turns into a notice for the agent.
            "row_limit": rows,
        }
        if catalog.strip():
            body["catalog"] = catalog.strip()
        if schema.strip():
            body["schema"] = schema.strip()
        if parameters.strip():
            parsed, parse_err = _parse_json_arg(parameters, "parameters")
            if parse_err:
                return parse_err
            if not isinstance(parsed, dict):
                return (
                    "Error: parameters must be a JSON object mapping marker "
                    'names to values, e.g. {"since": "2026-01-01"}.'
                )
            body["parameters"] = _sql_parameters(parsed)

        payload, err = await _api_dict(
            "post",
            "/api/2.0/sql/statements",
            action,
            json=body,
            timeout=_DEFAULT_TIMEOUT,
            # POSTing a statement is not idempotent (it starts execution), so
            # it deliberately opts out of the transient-status retry.
            idempotent=False,
        )
        if err:
            return err
        payload, err = await _await_statement(payload, action)
        if err:
            return err
        # Same `rows` the server was told to stop at, so the wire limit and the
        # rendered limit cannot drift apart.
        return format_statement_result(payload, rows)

    # =====================================================================
    # 0. SQL WAREHOUSES + STATEMENT EXECUTION
    # =====================================================================

    async def list_databricks_warehouses() -> str:
        """List the SQL warehouses that can execute statements."""
        data, err = await _api_dict(
            "get", "/api/2.0/sql/warehouses", "list SQL warehouses"
        )
        if err:
            return err
        warehouses = [w for w in (data.get("warehouses") or []) if isinstance(w, dict)]

        def _fmt(wh: dict) -> str:
            marker = " (default)" if wh.get("id") == default_warehouse_id else ""
            return (
                f"  {wh.get('name', 'Unnamed')}{marker} - {wh.get('state', '?')}\n"
                f"    ID: {wh.get('id', '?')} | Size: {wh.get('cluster_size', '?')} | "
                f"Type: {wh.get('warehouse_type', '?')} | "
                f"Serverless: {'yes' if wh.get('enable_serverless_compute') else 'no'} | "
                f"Auto-stop: {wh.get('auto_stop_mins', '?')} min"
            )

        return format_entity_list(
            warehouses,
            _fmt,
            noun="SQL warehouse",
            empty_msg=(
                "No SQL warehouses found. Create one in the workspace under "
                "SQL > SQL Warehouses, or check that the credential can see it."
            ),
        )

    async def query_databricks_sql(
        statement: str = "",
        warehouse_id: str = "",
        catalog: str = "",
        schema: str = "",
        limit: int = _MAX_ROWS,
        parameters: str = "",
    ) -> str:
        """Run a read-only SQL statement on a SQL warehouse."""
        guard = check_read_only_sql(statement)
        if guard:
            return guard
        return await _run_statement(
            statement, warehouse_id, catalog, schema, limit, parameters, "run SQL query"
        )

    async def execute_databricks_sql(
        statement: str = "",
        warehouse_id: str = "",
        catalog: str = "",
        schema: str = "",
        limit: int = _MAX_ROWS,
        parameters: str = "",
    ) -> str:
        """Run any SQL statement, including DML and DDL."""
        if not (statement or "").strip():
            return "Error: statement is required."
        return await _run_statement(
            statement,
            warehouse_id,
            catalog,
            schema,
            limit,
            parameters,
            "execute SQL statement",
        )

    async def get_databricks_statement(
        statement_id: str = "", limit: int = _MAX_ROWS
    ) -> str:
        """Fetch the status and result of a previously started statement."""
        if not statement_id.strip():
            return "Error: statement_id is required."
        payload, err = await _api_dict(
            "get",
            f"/api/2.0/sql/statements/{_path_segment(statement_id)}",
            "get statement result",
        )
        if err:
            return err
        status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        state = str(status.get("state") or "UNKNOWN")
        if state not in _SQL_TERMINAL_STATES:
            return (
                f"Statement {statement_id.strip()} is still {state}. Call this "
                "tool again in a few seconds, or cancel it with "
                "cancel_databricks_statement."
            )
        return format_statement_result(
            payload, _clamp(limit, _MAX_ROWS, hi=_MAX_ROWS_HARD)
        )

    async def cancel_databricks_statement(statement_id: str = "") -> str:
        """Cancel a running SQL statement."""
        if not statement_id.strip():
            return "Error: statement_id is required."
        _data, err = await _api(
            "post",
            f"/api/2.0/sql/statements/{_path_segment(statement_id)}/cancel",
            "cancel statement",
            idempotent=False,
        )
        if err:
            return err
        return f"Cancellation requested for statement {statement_id.strip()}."

    # =====================================================================
    # 1. UNITY CATALOG
    # =====================================================================

    async def list_databricks_catalogs(limit: int = 50, page_token: str = "") -> str:
        """List Unity Catalog catalogs visible to the credential."""
        params: dict[str, Any] = {"max_results": _clamp(limit, 50)}
        if page_token.strip():
            params["page_token"] = page_token.strip()
        data, err = await _api_dict(
            "get", "/api/2.1/unity-catalog/catalogs", "list catalogs", params=params
        )
        if err:
            return err
        catalogs = [c for c in (data.get("catalogs") or []) if isinstance(c, dict)]

        def _fmt(cat: dict) -> str:
            comment = _trunc(cat.get("comment"), 120) if cat.get("comment") else ""
            return (
                f"  {cat.get('name', 'Unnamed')} "
                f"[{cat.get('catalog_type', 'CATALOG')}] - owner "
                f"{cat.get('owner', 'N/A')}" + (f"\n    {comment}" if comment else "")
            )

        out = format_entity_list(
            catalogs,
            _fmt,
            noun="catalog",
            empty_msg=_page_empty_msg("catalog", data.get("next_page_token")),
        )
        return out + format_pagination_hint(data.get("next_page_token"), "page_token")

    async def list_databricks_schemas(
        catalog_name: str = "", limit: int = 50, page_token: str = ""
    ) -> str:
        """List the schemas (databases) inside a Unity Catalog catalog."""
        if not catalog_name.strip():
            return "Error: catalog_name is required (see list_databricks_catalogs)."
        params: dict[str, Any] = {
            "catalog_name": catalog_name.strip(),
            "max_results": _clamp(limit, 50),
        }
        if page_token.strip():
            params["page_token"] = page_token.strip()
        data, err = await _api_dict(
            "get", "/api/2.1/unity-catalog/schemas", "list schemas", params=params
        )
        if err:
            return err
        schemas = [s for s in (data.get("schemas") or []) if isinstance(s, dict)]

        def _fmt(sch: dict) -> str:
            comment = _trunc(sch.get("comment"), 120) if sch.get("comment") else ""
            return (
                f"  {sch.get('full_name') or sch.get('name', 'Unnamed')} - owner "
                f"{sch.get('owner', 'N/A')}" + (f"\n    {comment}" if comment else "")
            )

        out = format_entity_list(
            schemas,
            _fmt,
            noun="schema",
            empty_msg=_page_empty_msg("schema", data.get("next_page_token")),
        )
        return out + format_pagination_hint(data.get("next_page_token"), "page_token")

    async def list_databricks_tables(
        catalog_name: str = "",
        schema_name: str = "",
        limit: int = 50,
        page_token: str = "",
    ) -> str:
        """List the tables and views in a Unity Catalog schema."""
        if not (catalog_name.strip() and schema_name.strip()):
            return (
                "Error: catalog_name and schema_name are both required (see "
                "list_databricks_catalogs / list_databricks_schemas)."
            )
        params: dict[str, Any] = {
            "catalog_name": catalog_name.strip(),
            "schema_name": schema_name.strip(),
            # Unity Catalog caps tables/list at 50 per page (unlike catalogs and
            # schemas, which allow far more) — asking for more is a 400.
            "max_results": _clamp(limit, 50, hi=50),
            # Column metadata is verbose and available on demand via
            # describe_databricks_table, so keep the listing compact.
            "omit_columns": "true",
        }
        if page_token.strip():
            params["page_token"] = page_token.strip()
        data, err = await _api_dict(
            "get", "/api/2.1/unity-catalog/tables", "list tables", params=params
        )
        if err:
            return err
        tables = [t for t in (data.get("tables") or []) if isinstance(t, dict)]

        def _fmt(tbl: dict) -> str:
            comment = _trunc(tbl.get("comment"), 120) if tbl.get("comment") else ""
            return (
                f"  {tbl.get('full_name') or tbl.get('name', 'Unnamed')} "
                f"[{tbl.get('table_type', '?')}/{tbl.get('data_source_format', '?')}] "
                f"- owner {tbl.get('owner', 'N/A')}"
                + (f"\n    {comment}" if comment else "")
            )

        out = format_entity_list(
            tables,
            _fmt,
            noun="table",
            empty_msg=_page_empty_msg("table", data.get("next_page_token")),
        )
        return out + format_pagination_hint(data.get("next_page_token"), "page_token")

    async def describe_databricks_table(full_name: str = "") -> str:
        """Get a table's columns, types, owner and storage details."""
        name = full_name.strip()
        if name.count(".") != 2:
            return (
                "Error: full_name must be the three-level Unity Catalog name "
                "'catalog.schema.table' (e.g. main.default.events)."
            )
        data, err = await _api_dict(
            "get",
            f"/api/2.1/unity-catalog/tables/{_path_segment(name)}",
            "describe table",
        )
        if err:
            return err
        columns = [c for c in (data.get("columns") or []) if isinstance(c, dict)]
        col_lines = "\n".join(
            f"  {c.get('name', '?')}: {c.get('type_text', '?')}"
            f"{'' if c.get('nullable', True) else ' NOT NULL'}"
            + (f" - {_trunc(c.get('comment'), 120)}" if c.get("comment") else "")
            for c in columns
        )
        lines = [
            f"{data.get('full_name', name)} "
            f"[{data.get('table_type', '?')}/{data.get('data_source_format', '?')}]",
            f"Owner: {data.get('owner', 'N/A')} | "
            f"Created: {_ms(data.get('created_at'))} | "
            f"Updated: {_ms(data.get('updated_at'))}",
        ]
        if data.get("comment"):
            lines.append(f"Comment: {_trunc(data.get('comment'), 500)}")
        if data.get("storage_location"):
            lines.append(f"Storage: {_trunc(data.get('storage_location'), 300)}")
        lines.append(f"Columns ({len(columns)}):")
        lines.append(col_lines or "  (none reported)")
        return "\n".join(lines)

    # =====================================================================
    # 2. JOBS & WORKFLOWS
    # =====================================================================

    async def list_databricks_jobs(
        name_filter: str = "", limit: int = 25, page_token: str = ""
    ) -> str:
        """List Jobs (Workflows) defined in the workspace."""
        params: dict[str, Any] = {"limit": _clamp(limit, 25)}
        if name_filter.strip():
            params["name"] = name_filter.strip()
        if page_token.strip():
            params["page_token"] = page_token.strip()
        data, err = await _api_dict(
            "get", "/api/2.2/jobs/list", "list jobs", params=params
        )
        if err:
            return err
        jobs = [j for j in (data.get("jobs") or []) if isinstance(j, dict)]

        def _fmt(job: dict) -> str:
            settings = job.get("settings") if isinstance(job.get("settings"), dict) else {}
            schedule = settings.get("schedule") if isinstance(settings.get("schedule"), dict) else {}
            cron = schedule.get("quartz_cron_expression")
            paused = schedule.get("pause_status") == "PAUSED"
            trigger = (
                f"cron {cron} ({'paused' if paused else 'active'})"
                if cron
                else "manual / triggered"
            )
            tasks = settings.get("tasks") or []
            return (
                f"  {settings.get('name', 'Unnamed job')}\n"
                f"    Job ID: {job.get('job_id', '?')} | Creator: "
                f"{job.get('creator_user_name', 'N/A')} | Tasks: {len(tasks)} | "
                f"Schedule: {trigger}"
            )

        out = format_entity_list(
            jobs,
            _fmt,
            noun="job",
            empty_msg=_page_empty_msg(
                "job",
                data.get("next_page_token") if data.get("has_more") else None,
            ),
        )
        if data.get("has_more"):
            out += format_pagination_hint(data.get("next_page_token"), "page_token")
        return out

    async def get_databricks_job(job_id: str = "") -> str:
        """Get a job's definition: tasks, dependencies, schedule and compute."""
        if not job_id.strip():
            return "Error: job_id is required (see list_databricks_jobs)."
        data, err = await _api_dict(
            "get", "/api/2.2/jobs/get", "get job", params={"job_id": job_id.strip()}
        )
        if err:
            return err
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        tasks = [t for t in (settings.get("tasks") or []) if isinstance(t, dict)]
        schedule = settings.get("schedule") if isinstance(settings.get("schedule"), dict) else {}

        def _task_line(task: dict) -> str:
            kind = next(
                (
                    key.removesuffix("_task")
                    for key in task
                    if key.endswith("_task") and isinstance(task.get(key), dict)
                ),
                "unknown",
            )
            depends = ", ".join(
                str(d.get("task_key"))
                for d in (task.get("depends_on") or [])
                if isinstance(d, dict)
            )
            return (
                f"  {task.get('task_key', '?')} [{kind}]"
                + (f" - after {depends}" if depends else "")
            )

        lines = [
            f"{settings.get('name', 'Unnamed job')} (job ID {data.get('job_id', '?')})",
            f"Creator: {data.get('creator_user_name', 'N/A')} | Created: "
            f"{_ms(data.get('created_time'))} | Format: "
            f"{settings.get('format', 'N/A')}",
        ]
        if schedule.get("quartz_cron_expression"):
            lines.append(
                f"Schedule: {schedule.get('quartz_cron_expression')} "
                f"({schedule.get('timezone_id', 'UTC')}, "
                f"{schedule.get('pause_status', 'UNPAUSED')})"
            )
        if settings.get("max_concurrent_runs"):
            lines.append(f"Max concurrent runs: {settings.get('max_concurrent_runs')}")
        params = settings.get("parameters") or []
        if params:
            lines.append(
                "Job parameters: "
                + ", ".join(
                    f"{p.get('name')}={p.get('default')!r}"
                    for p in params
                    if isinstance(p, dict)
                )
            )
        lines.append(f"Tasks ({len(tasks)}):")
        lines.append("\n".join(_task_line(t) for t in tasks) or "  (none)")
        return "\n".join(lines)

    async def run_databricks_job(
        job_id: str = "", job_parameters: str = "", notebook_params: str = ""
    ) -> str:
        """Trigger a job run now, optionally with parameters."""
        if not job_id.strip():
            return "Error: job_id is required (see list_databricks_jobs)."
        body: dict[str, Any] = {}
        try:
            body["job_id"] = int(job_id.strip())
        except ValueError:
            return "Error: job_id must be a numeric Databricks job ID."
        for raw, key, api_key in (
            (job_parameters, "job_parameters", "job_parameters"),
            (notebook_params, "notebook_params", "notebook_params"),
        ):
            if not raw.strip():
                continue
            parsed, parse_err = _parse_json_arg(raw, key)
            if parse_err:
                return parse_err
            if not isinstance(parsed, dict):
                return f"Error: {key} must be a JSON object of name/value pairs."
            body[api_key] = {str(k): str(v) for k, v in parsed.items()}
        data, err = await _api_dict(
            "post", "/api/2.2/jobs/run-now", "run job", json=body, idempotent=False
        )
        if err:
            return err
        run_id = data.get("run_id", "?")
        return (
            f"Job {body['job_id']} triggered - run ID {run_id}. Follow it with "
            f"get_databricks_job_run(run_id='{run_id}')."
        )

    async def list_databricks_job_runs(
        job_id: str = "",
        active_only: bool = False,
        completed_only: bool = False,
        limit: int = 20,
        page_token: str = "",
    ) -> str:
        """List job runs, newest first, optionally scoped to one job."""
        if active_only and completed_only:
            return "Error: active_only and completed_only are mutually exclusive."
        # The Jobs API documents runs/list as "greater than 0 and less than
        # 25" per page (jobs/list allows 100), so 24 is the ceiling and 20 is
        # the API's own default - sending 25 would risk a 400 from the tool's
        # own default value.
        params: dict[str, Any] = {"limit": _clamp(limit, 20, hi=24)}
        if job_id.strip():
            params["job_id"] = job_id.strip()
        if active_only:
            params["active_only"] = "true"
        if completed_only:
            params["completed_only"] = "true"
        if page_token.strip():
            params["page_token"] = page_token.strip()
        data, err = await _api_dict(
            "get", "/api/2.2/jobs/runs/list", "list job runs", params=params
        )
        if err:
            return err
        runs = [r for r in (data.get("runs") or []) if isinstance(r, dict)]

        def _fmt(run: dict) -> str:
            state = run.get("state") if isinstance(run.get("state"), dict) else {}
            life_cycle = str(state.get("life_cycle_state") or "?")
            result_state = str(state.get("result_state") or "")
            icon = _JOB_RESULT_STATE_ICONS.get(
                result_state
            ) or _JOB_RUN_LIFE_CYCLE_ICONS.get(life_cycle, "⚪")
            outcome = f"{life_cycle}" + (f"/{result_state}" if result_state else "")
            return (
                f"  {icon} {run.get('run_name') or 'Run'} - {outcome}\n"
                f"    Run ID: {run.get('run_id', '?')} | Job ID: "
                f"{run.get('job_id', '?')} | Started: "
                f"{_ms(run.get('start_time'))} | Duration: "
                f"{_duration(run.get('run_duration') or run.get('execution_duration'))} | "
                f"Trigger: {run.get('trigger', 'N/A')}"
            )

        out = format_entity_list(
            runs,
            _fmt,
            noun="job run",
            empty_msg=_page_empty_msg(
                "job run",
                data.get("next_page_token") if data.get("has_more") else None,
            ),
        )
        if data.get("has_more"):
            out += format_pagination_hint(data.get("next_page_token"), "page_token")
        return out

    async def get_databricks_job_run(
        run_id: str = "", include_output: bool = True
    ) -> str:
        """Get a run's per-task outcome, and the failing task's error message."""
        if not run_id.strip():
            return "Error: run_id is required (see list_databricks_job_runs)."
        data, err = await _api_dict(
            "get", "/api/2.2/jobs/runs/get", "get job run", params={"run_id": run_id.strip()}
        )
        if err:
            return err
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]
        life_cycle = str(state.get("life_cycle_state") or "?")
        result_state = str(state.get("result_state") or "")
        icon = _JOB_RESULT_STATE_ICONS.get(result_state) or _JOB_RUN_LIFE_CYCLE_ICONS.get(
            life_cycle, "⚪"
        )
        lines = [
            f"{icon} {data.get('run_name') or 'Run'} (run ID "
            f"{data.get('run_id', '?')}, job ID {data.get('job_id', '?')})",
            f"State: {life_cycle}" + (f"/{result_state}" if result_state else ""),
            f"Started: {_ms(data.get('start_time'))} | Duration: "
            f"{_duration(data.get('run_duration') or data.get('execution_duration'))} | "
            f"Trigger: {data.get('trigger', 'N/A')}",
        ]
        if state.get("state_message"):
            lines.append(f"Message: {_trunc(state.get('state_message'), 500)}")
        if data.get("run_page_url"):
            lines.append(f"Run page: {data.get('run_page_url')}")

        failed_task_run_id = ""
        if tasks:
            lines.append(f"Tasks ({len(tasks)}):")
            for task in tasks:
                t_state = task.get("state") if isinstance(task.get("state"), dict) else {}
                t_result = str(t_state.get("result_state") or "")
                t_life = str(t_state.get("life_cycle_state") or "?")
                t_icon = _JOB_RESULT_STATE_ICONS.get(
                    t_result
                ) or _JOB_RUN_LIFE_CYCLE_ICONS.get(t_life, "⚪")
                lines.append(
                    f"  {t_icon} {task.get('task_key', '?')} - {t_life}"
                    + (f"/{t_result}" if t_result else "")
                    + f" ({_duration(task.get('execution_duration'))}), task run "
                    f"ID {task.get('run_id', '?')}"
                )
                if t_result in ("FAILED", "TIMEDOUT") and not failed_task_run_id:
                    failed_task_run_id = str(task.get("run_id") or "")

        if include_output and failed_task_run_id:
            out_data, out_err = await _api_dict(
                "get",
                "/api/2.2/jobs/runs/get-output",
                "get job run output",
                params={"run_id": failed_task_run_id},
            )
            if out_err:
                lines.append(f"(Could not read the failing task's output: {out_err})")
            else:
                error = out_data.get("error")
                trace = out_data.get("error_trace")
                if error:
                    lines.append(f"Failure: {_trunc(error, 800)}")
                if trace:
                    lines.append(f"Trace (truncated):\n{_trunc(trace, 1500)}")
        return "\n".join(lines)

    async def cancel_databricks_job_run(run_id: str = "") -> str:
        """Cancel a running job run."""
        if not run_id.strip():
            return "Error: run_id is required."
        _data, err = await _api(
            "post",
            "/api/2.2/jobs/runs/cancel",
            "cancel job run",
            json={"run_id": run_id.strip()},
            idempotent=False,
        )
        if err:
            return err
        return f"Cancellation requested for run {run_id.strip()}."

    async def list_databricks_clusters(limit: int = 25, page_token: str = "") -> str:
        """List all-purpose compute clusters in the workspace."""
        params: dict[str, Any] = {"page_size": _clamp(limit, 25)}
        if page_token.strip():
            params["page_token"] = page_token.strip()
        data, err = await _api_dict(
            "get", "/api/2.1/clusters/list", "list clusters", params=params
        )
        if err:
            return err
        clusters = [c for c in (data.get("clusters") or []) if isinstance(c, dict)]

        def _fmt(cluster: dict) -> str:
            workers = cluster.get("num_workers")
            autoscale = cluster.get("autoscale")
            if isinstance(autoscale, dict):
                size = (
                    f"{autoscale.get('min_workers', '?')}-"
                    f"{autoscale.get('max_workers', '?')} workers (autoscale)"
                )
            else:
                size = f"{workers if workers is not None else '?'} workers"
            return (
                f"  {cluster.get('cluster_name', 'Unnamed')} - "
                f"{cluster.get('state', '?')}\n"
                f"    Cluster ID: {cluster.get('cluster_id', '?')} | Runtime: "
                f"{cluster.get('spark_version', '?')} | Node: "
                f"{cluster.get('node_type_id', '?')} | {size}"
            )

        out = format_entity_list(
            clusters,
            _fmt,
            noun="cluster",
            empty_msg=_page_empty_msg("cluster", data.get("next_page_token")),
        )
        return out + format_pagination_hint(data.get("next_page_token"), "page_token")

    async def get_databricks_cluster(cluster_id: str = "") -> str:
        """Get a cluster's configuration, state and termination reason."""
        if not cluster_id.strip():
            return "Error: cluster_id is required (see list_databricks_clusters)."
        data, err = await _api_dict(
            "get",
            "/api/2.1/clusters/get",
            "get cluster",
            params={"cluster_id": cluster_id.strip()},
        )
        if err:
            return err
        lines = [
            f"{data.get('cluster_name', 'Unnamed')} (cluster ID "
            f"{data.get('cluster_id', '?')})",
            f"State: {data.get('state', '?')}"
            + (
                f" - {_trunc(data.get('state_message'), 300)}"
                if data.get("state_message")
                else ""
            ),
            f"Runtime: {data.get('spark_version', '?')} | Node type: "
            f"{data.get('node_type_id', '?')} | Driver: "
            f"{data.get('driver_node_type_id', '?')}",
            f"Creator: {data.get('creator_user_name', 'N/A')} | Started: "
            f"{_ms(data.get('start_time'))} | Auto-terminate: "
            f"{data.get('autotermination_minutes', '?')} min",
        ]
        reason = data.get("termination_reason")
        if isinstance(reason, dict) and reason:
            lines.append(
                f"Termination: {reason.get('code', '?')} "
                f"({reason.get('type', '?')}) - "
                f"{_trunc((reason.get('parameters') or {}).get('reason'), 300)}"
            )
        return "\n".join(lines)

    # =====================================================================
    # 3. MOSAIC AI - MODEL SERVING + VECTOR SEARCH
    # =====================================================================

    async def list_databricks_serving_endpoints() -> str:
        """List Mosaic AI Model Serving endpoints."""
        data, err = await _api_dict(
            "get", "/api/2.0/serving-endpoints", "list serving endpoints"
        )
        if err:
            return err
        endpoints = [e for e in (data.get("endpoints") or []) if isinstance(e, dict)]

        def _fmt(ep: dict) -> str:
            state = ep.get("state") if isinstance(ep.get("state"), dict) else {}
            config = ep.get("config") if isinstance(ep.get("config"), dict) else {}
            served = [
                str(m.get("name") or m.get("model_name") or "?")
                for m in (config.get("served_entities") or config.get("served_models") or [])
                if isinstance(m, dict)
            ]
            return (
                f"  {ep.get('name', 'Unnamed')} - ready: "
                f"{state.get('ready', '?')}, update: "
                f"{state.get('config_update', 'N/A')}\n"
                f"    Task: {ep.get('task', 'N/A')} | Serves: "
                f"{', '.join(served[:4]) or 'N/A'}"
            )

        return format_entity_list(endpoints, _fmt, noun="serving endpoint")

    async def get_databricks_serving_endpoint(name: str = "") -> str:
        """Get a serving endpoint's served entities, traffic split and state."""
        if not name.strip():
            return "Error: name is required (see list_databricks_serving_endpoints)."
        data, err = await _api_dict(
            "get",
            f"/api/2.0/serving-endpoints/{_path_segment(name)}",
            "get serving endpoint",
        )
        if err:
            return err
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        config = data.get("config") if isinstance(data.get("config"), dict) else {}
        entities = [
            e
            for e in (config.get("served_entities") or config.get("served_models") or [])
            if isinstance(e, dict)
        ]
        traffic = config.get("traffic_config") if isinstance(config.get("traffic_config"), dict) else {}
        lines = [
            f"{data.get('name', name.strip())} - ready: {state.get('ready', '?')}, "
            f"update: {state.get('config_update', 'N/A')}",
            f"Task: {data.get('task', 'N/A')} | Creator: "
            f"{data.get('creator', 'N/A')} | Updated: "
            f"{_ms(data.get('last_updated_timestamp'))}",
            f"Served entities ({len(entities)}):",
        ]
        for entity in entities:
            lines.append(
                f"  {entity.get('name', '?')} -> "
                f"{entity.get('entity_name') or entity.get('model_name', '?')} "
                f"v{entity.get('entity_version') or entity.get('model_version', '?')} "
                f"({entity.get('workload_size', 'N/A')}, scale-to-zero: "
                f"{entity.get('scale_to_zero_enabled', 'N/A')})"
            )
        routes = [r for r in (traffic.get("routes") or []) if isinstance(r, dict)]
        if routes:
            lines.append(
                "Traffic: "
                + ", ".join(
                    f"{r.get('served_model_name') or r.get('served_entity_name', '?')}="
                    f"{r.get('traffic_percentage', '?')}%"
                    for r in routes
                )
            )
        return "\n".join(lines)

    async def query_databricks_serving_endpoint(
        name: str = "",
        prompt: str = "",
        payload: str = "",
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        """Invoke a Model Serving endpoint with a prompt or a raw payload."""
        if not name.strip():
            return "Error: name is required (see list_databricks_serving_endpoints)."
        if payload.strip():
            body, parse_err = _parse_json_arg(payload, "payload")
            if parse_err:
                return parse_err
            if not isinstance(body, dict):
                return (
                    "Error: payload must be a JSON object matching the "
                    "endpoint's signature (e.g. {\"inputs\": [...]} or "
                    '{"messages": [...]}).'
                )
        elif prompt.strip():
            body = {
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": _clamp(max_tokens, 512, hi=8192),
            }
            try:
                body["temperature"] = max(0.0, min(float(temperature), 2.0))
            except (TypeError, ValueError):
                body["temperature"] = 0.0
        else:
            return (
                "Error: pass either prompt (for a chat/completions endpoint) or "
                "payload (a JSON body matching the endpoint's signature)."
            )
        data, err = await _api(
            "post",
            f"/serving-endpoints/{_path_segment(name)}/invocations",
            "query serving endpoint",
            json=body,
            timeout=_INVOKE_TIMEOUT,
            idempotent=False,
        )
        if err:
            return err
        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    message = first.get("message")
                    if isinstance(message, dict) and message.get("content"):
                        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                        suffix = (
                            f"\n\n[tokens: {usage.get('prompt_tokens', '?')} in / "
                            f"{usage.get('completion_tokens', '?')} out]"
                            if usage
                            else ""
                        )
                        return f"{message['content']}{suffix}"
                    if first.get("text"):
                        return str(first["text"])
        return _trunc(json.dumps(data, ensure_ascii=False), 4000)

    async def list_databricks_vector_indexes(endpoint_name: str = "") -> str:
        """List Vector Search indexes served by a Vector Search endpoint."""
        if not endpoint_name.strip():
            return (
                "Error: endpoint_name is required - it is the Vector Search "
                "endpoint hosting the indexes (Compute > Vector Search in the "
                "workspace)."
            )
        data, err = await _api_dict(
            "get",
            "/api/2.0/vector-search/indexes",
            "list vector search indexes",
            params={"endpoint_name": endpoint_name.strip()},
        )
        if err:
            return err
        indexes = [i for i in (data.get("vector_indexes") or []) if isinstance(i, dict)]

        def _fmt(index: dict) -> str:
            return (
                f"  {index.get('name', 'Unnamed')} "
                f"[{index.get('index_type', '?')}]\n"
                f"    Endpoint: {index.get('endpoint_name', '?')} | Primary key: "
                f"{index.get('primary_key', 'N/A')}"
            )

        return format_entity_list(indexes, _fmt, noun="vector search index")

    async def query_databricks_vector_index(
        index_name: str = "",
        query_text: str = "",
        columns: str = "",
        num_results: int = 10,
        filters: str = "",
    ) -> str:
        """Run a similarity search against a Vector Search index."""
        if not index_name.strip():
            return (
                "Error: index_name is required - the three-level Unity Catalog "
                "name of the index (see list_databricks_vector_indexes)."
            )
        if not query_text.strip():
            return "Error: query_text is required."
        column_list = [c.strip() for c in (columns or "").split(",") if c.strip()]
        body: dict[str, Any] = {
            "query_text": query_text,
            "num_results": _clamp(num_results, 10, hi=50),
        }
        if column_list:
            body["columns"] = column_list
        if filters.strip():
            parsed, parse_err = _parse_json_arg(filters, "filters")
            if parse_err:
                return parse_err
            if not isinstance(parsed, dict):
                return (
                    "Error: filters must be a JSON object, e.g. "
                    '{"year >=": 2024, "category": "security"}.'
                )
            body["filters_json"] = json.dumps(parsed)
        data, err = await _api_dict(
            "post",
            f"/api/2.0/vector-search/indexes/{_path_segment(index_name)}/query",
            "query vector search index",
            json=body,
            idempotent=False,
        )
        if err:
            return err
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        manifest = data.get("manifest") if isinstance(data.get("manifest"), dict) else {}
        col_names = [
            str(c.get("name", "?"))
            for c in (manifest.get("columns") or [])
            if isinstance(c, dict)
        ]
        rows = [r for r in (result.get("data_array") or []) if isinstance(r, list)]
        if not rows:
            return "No matching documents found."
        lines = [f"Found {len(rows)} match(es) in {index_name.strip()}:"]
        for row in rows:
            pairs = [
                f"{col_names[i] if i < len(col_names) else f'col{i}'}={_trunc(cell, 400)}"
                for i, cell in enumerate(row)
            ]
            lines.append("  " + " | ".join(pairs))
        return "\n".join(lines)

    # =====================================================================
    # 4. VOLUMES + FILES
    # =====================================================================

    async def list_databricks_volumes(
        catalog_name: str = "",
        schema_name: str = "",
        limit: int = 50,
        page_token: str = "",
    ) -> str:
        """List Unity Catalog volumes in a schema."""
        if not (catalog_name.strip() and schema_name.strip()):
            return "Error: catalog_name and schema_name are both required."
        params: dict[str, Any] = {
            "catalog_name": catalog_name.strip(),
            "schema_name": schema_name.strip(),
            "max_results": _clamp(limit, 50),
        }
        if page_token.strip():
            params["page_token"] = page_token.strip()
        data, err = await _api_dict(
            "get",
            "/api/2.1/unity-catalog/volumes",
            "list volumes",
            params=params,
        )
        if err:
            return err
        volumes = [v for v in (data.get("volumes") or []) if isinstance(v, dict)]

        def _fmt(vol: dict) -> str:
            return (
                f"  {vol.get('full_name') or vol.get('name', 'Unnamed')} "
                f"[{vol.get('volume_type', '?')}] - owner "
                f"{vol.get('owner', 'N/A')}\n"
                f"    Path: /Volumes/{vol.get('catalog_name', '?')}/"
                f"{vol.get('schema_name', '?')}/{vol.get('name', '?')}"
            )

        out = format_entity_list(
            volumes,
            _fmt,
            noun="volume",
            empty_msg=_page_empty_msg("volume", data.get("next_page_token")),
        )
        return out + format_pagination_hint(data.get("next_page_token"), "page_token")

    async def list_databricks_files(path: str = "", page_token: str = "") -> str:
        """List the entries of a Unity Catalog volume directory or DBFS path."""
        target = (path or "").strip()
        if not target:
            return (
                "Error: path is required - a Unity Catalog volume directory "
                "(/Volumes/catalog/schema/volume/...) or a DBFS path "
                "(dbfs:/... or /dbfs/...)."
            )
        if _is_dbfs_path(target):
            data, err = await _api_dict(
                "get",
                "/api/2.0/dbfs/list",
                "list DBFS files",
                params={"path": _normalize_dbfs_path(target)},
            )
            if err:
                return err
            entries = [f for f in (data.get("files") or []) if isinstance(f, dict)]

            def _fmt_dbfs(entry: dict) -> str:
                kind = "dir " if entry.get("is_dir") else "file"
                size = "" if entry.get("is_dir") else f" ({entry.get('file_size', 0)} bytes)"
                return f"  [{kind}] {entry.get('path', '?')}{size}"

            return format_entity_list(entries, _fmt_dbfs, noun="entry")

        if not _is_volume_path(target):
            data, err = await _api_dict(
                "get",
                "/api/2.0/workspace/list",
                "list workspace objects",
                params={"path": target},
            )
            if err:
                return err
            objects = [o for o in (data.get("objects") or []) if isinstance(o, dict)]

            def _fmt_workspace(obj: dict) -> str:
                kind = str(obj.get("object_type") or "?").lower()
                language = f" [{str(obj.get('language')).lower()}]" if obj.get("language") else ""
                size = f" ({obj['size']} bytes)" if isinstance(obj.get("size"), int) else ""
                return f"  [{kind}] {obj.get('path', '?')}{language}{size}"

            return format_entity_list(
                objects,
                _fmt_workspace,
                noun="workspace object",
                empty_msg=f"No workspace objects under {target}.",
            )

        params: dict[str, Any] = {}
        if page_token.strip():
            params["page_token"] = page_token.strip()
        data, err = await _api_dict(
            "get",
            f"/api/2.0/fs/directories{_file_path(target)}",
            "list volume files",
            params=params,
        )
        if err:
            return err
        entries = [c for c in (data.get("contents") or []) if isinstance(c, dict)]

        def _fmt(entry: dict) -> str:
            kind = "dir " if entry.get("is_directory") else "file"
            size = "" if entry.get("is_directory") else f" ({entry.get('file_size', 0)} bytes)"
            modified = (
                f" - modified {_ms(entry.get('last_modified'))}"
                if entry.get("last_modified")
                else ""
            )
            return f"  [{kind}] {entry.get('path', entry.get('name', '?'))}{size}{modified}"

        out = format_entity_list(
            entries,
            _fmt,
            noun="entry",
            empty_msg=_page_empty_msg("entry", data.get("next_page_token")),
        )
        return out + format_pagination_hint(data.get("next_page_token"), "page_token")

    async def read_databricks_file(path: str = "", max_bytes: int = _MAX_FILE_BYTES) -> str:
        """Read the text content of a volume or DBFS file (bounded preview)."""
        target = (path or "").strip()
        if not target:
            return (
                "Error: path is required - a Unity Catalog volume file "
                "(/Volumes/catalog/schema/volume/file.csv) or a DBFS path."
            )
        cap = _clamp(max_bytes, _MAX_FILE_BYTES, lo=1, hi=_MAX_FILE_BYTES_HARD)
        if _is_dbfs_path(target):
            data, err = await _api_dict(
                "get",
                "/api/2.0/dbfs/read",
                "read DBFS file",
                params={
                    "path": _normalize_dbfs_path(target),
                    "offset": 0,
                    # cap + 1, mirroring the volume path's inclusive
                    # ``Range: bytes=0-{cap}``, so ``_render_file``'s "is there
                    # more?" test stays exact: asking for exactly ``cap`` makes
                    # a truncated read indistinguishable from a complete file
                    # and the agent reasons over a silently cut preview.
                    # DBFS caps a single read at 1 MiB and cap maxes out at
                    # _MAX_FILE_BYTES_HARD, so cap + 1 stays well inside it.
                    "length": min(cap + 1, 1_048_576),
                },
            )
            if err:
                return err
            encoded = data.get("data")
            if not isinstance(encoded, str):
                return f"Error: {_PROVIDER} returned no data for '{target}'."
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                return f"Error: {_PROVIDER} returned a malformed body for '{target}'."
            return _render_file(target, raw, cap)

        if not _is_volume_path(target):
            exported, err = await _api_dict(
                "get",
                "/api/2.0/workspace/export",
                "export workspace object",
                params={"path": target, "format": "SOURCE"},
            )
            if err:
                return err
            try:
                raw = base64.b64decode(exported.get("content") or "", validate=True)
            except (ValueError, binascii.Error):
                return f"Error: {_PROVIDER} returned a malformed export for '{target}'."
            return _render_file(target, raw, cap)

        creds_err = _creds_error()
        if creds_err:
            return creds_err
        bearer = await _bearer()
        if not bearer:
            return _auth_error(client_id)
        url = f"{host}/api/2.0/fs/files{_file_path(target)}"
        auth_headers = {"Authorization": f"Bearer {bearer}"}
        async with egress_client(timeout=_DEFAULT_TIMEOUT) as client:
            # Bound the transfer at the network layer instead of downloading a
            # multi-GB Delta/Parquet file just to render 100 kB of it. Asking
            # for ``cap`` + 1 bytes (``bytes=0-cap`` is inclusive) keeps
            # ``_render_file``'s "is there more?" test exact: a file longer than
            # the cap still arrives one byte over it. A server that ignores the
            # Range header returns the whole body and the client-side
            # truncation applies as before, so this is a pure optimisation.
            resp, err = await safe_request(
                client,
                "get",
                url,
                _PROVIDER,
                "read volume file",
                headers={**auth_headers, "Range": f"bytes=0-{cap}"},
                # A zero-byte file answers 416 to *any* byte range. That is a
                # benign outcome, not a failure, so opt into receiving the
                # response (and suppress the warning log) in order to branch.
                expected_statuses={416},
                retry_statuses=TRANSIENT_RETRY_STATUSES,
            )
            if resp is not None and resp.status_code == 416:
                # Empty file, or a backend that refuses ranges outright: fall
                # back to the plain unranged read.
                resp, err = await safe_request(
                    client,
                    "get",
                    url,
                    _PROVIDER,
                    "read volume file",
                    headers=auth_headers,
                    retry_statuses=TRANSIENT_RETRY_STATUSES,
                )
            if err:
                return err
            if resp is None:
                return f"Error: {_PROVIDER} returned no response for '{target}'."
            return _render_file(target, resp.content, cap)

    def _too_large(target: str, size: int, limit: int) -> str:
        return (
            f"{target} is {round(size / 1024 / 1024, 1)} MB, over the "
            f"{limit // 1024 // 1024} MB transfer limit."
        )

    async def _read_dbfs(path: str) -> tuple[bytes | None, str | None]:
        """Read a whole DBFS file in 1 MB blocks (``/api/2.0/dbfs/read``)."""
        status, err = await _api_dict(
            "get", "/api/2.0/dbfs/get-status", "get DBFS file status", params={"path": path}
        )
        if err:
            return None, err
        if status.get("is_dir"):
            return None, f"Error: {path} is a directory - list it with list_databricks_files."
        size = int(status.get("file_size") or 0)
        if size > _MAX_TRANSFER_BYTES:
            return None, _too_large(path, size, _MAX_TRANSFER_BYTES)
        chunks: list[bytes] = []
        offset = 0
        while offset < size:
            block, err = await _api_dict(
                "get",
                "/api/2.0/dbfs/read",
                "read DBFS file",
                params={"path": path, "offset": offset, "length": _DBFS_BLOCK_BYTES},
            )
            if err:
                return None, err
            try:
                raw = base64.b64decode(block.get("data") or "", validate=True)
            except (ValueError, binascii.Error):
                return None, f"Error: {_PROVIDER} returned a malformed body for '{path}'."
            if not raw:
                break
            chunks.append(raw)
            offset += len(raw)
        if offset != size:
            return None, (
                f"Error: {_PROVIDER} returned {offset} of the {size} bytes of '{path}' "
                "(it changed while it was read, or the read was cut) - nothing was stored."
            )
        return b"".join(chunks), None

    async def download_databricks_file(
        path: str = "", format: str = "SOURCE"
    ) -> str | ToolResult:
        """Store a whole volume, DBFS or workspace file (a notebook exported) as a file of the run."""
        target = (path or "").strip()
        if not target:
            return (
                "Error: path is required - a Unity Catalog volume file "
                "(/Volumes/...), a DBFS path (dbfs:/...) or a workspace path "
                "(/Users/.../notebook)."
            )
        links: list[tuple[str, str | None]] = []
        name = target.rstrip("/").rsplit("/", 1)[-1] or "databricks-file"
        if _is_dbfs_path(target):
            dbfs_path = _normalize_dbfs_path(target)
            data, err = await _read_dbfs(dbfs_path)
            if err:
                return err
            if dbfs_path.startswith("/FileStore/"):
                links.append(("Download URL", f"{host}/files/{dbfs_path[len('/FileStore/'):]}"))
        elif _is_volume_path(target):
            url_path = f"/api/2.0/fs/files{_file_path(target)}"
            creds_err = _creds_error()
            if creds_err:
                return creds_err
            bearer = await _bearer()
            if not bearer:
                return _auth_error(client_id)
            async with egress_client(timeout=_FILE_TIMEOUT) as client:
                fetched = await fetch_file(
                    client,
                    "get",
                    f"{host}{url_path}",
                    provider=_PROVIDER,
                    action="download volume file",
                    max_bytes=_MAX_TRANSFER_BYTES,
                    headers={"Authorization": f"Bearer {bearer}"},
                )
            if isinstance(fetched, str):
                return fetched
            data = fetched.data
            links.append(("Download URL", f"{host}{url_path}"))
        else:
            fmt = (format or "SOURCE").strip().upper()
            if fmt not in _EXPORT_FORMATS:
                return f"Error: format must be one of {', '.join(_EXPORT_FORMATS)}."
            exported, err = await _api_dict(
                "get",
                "/api/2.0/workspace/export",
                "export workspace object",
                params={"path": target, "format": fmt},
            )
            if err:
                return err
            try:
                data = base64.b64decode(exported.get("content") or "", validate=True)
            except (ValueError, binascii.Error):
                return f"Error: {_PROVIDER} returned a malformed export for '{target}'."
            extension = _EXPORT_FORMATS[fmt] or str(exported.get("file_type") or "")
            if extension and not name.lower().endswith(f".{extension.lower()}"):
                name = f"{name}.{extension}"
            links.append(
                (
                    "Download URL",
                    f"{host}/api/2.0/workspace/export?path={quote(target, safe='/')}"
                    f"&format={fmt}&direct_download=true",
                )
            )
        if len(data) > _MAX_TRANSFER_BYTES:
            return _too_large(target, len(data), _MAX_TRANSFER_BYTES)
        content_type = file_content_type("", name)
        if content_type == "application/octet-stream" and name.rsplit(".", 1)[-1].lower() in _TEXT_EXTENSIONS:
            content_type = "text/plain"
        return await deliver_file(
            data=data,
            filename=name,
            content_type=content_type,
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=links,
            details=[f"Path: {target}"],
        )

    async def upload_databricks_file(
        file_id: str,
        path: str,
        overwrite: bool = False,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Write a file of the run to a volume, DBFS or the workspace, byte-for-byte."""
        target = (path or "").strip()
        if not target:
            return "Error: path is required (a /Volumes/..., dbfs:/... or workspace path)."
        workspace = not _is_dbfs_path(target) and not _is_volume_path(target)
        piped = await read_pipe_file(
            file_context,
            file_id,
            max_bytes=_MAX_WORKSPACE_BYTES if workspace else _MAX_TRANSFER_BYTES,
            target=f"{_PROVIDER} {'workspace' if workspace else 'storage'}",
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, filename, _ = piped
        file_id = file_id.strip()
        folder = target if target.endswith("/") else ""
        if folder:
            target += filename
        if _is_dbfs_path(target):
            dbfs_path = _normalize_dbfs_path(target)
            created, err = await _api_dict(
                "post",
                "/api/2.0/dbfs/create",
                "create DBFS file",
                json={"path": dbfs_path, "overwrite": bool(overwrite)},
                idempotent=False,
            )
            if err:
                return err
            handle = created.get("handle")
            for start in range(0, len(data), _DBFS_BLOCK_BYTES):
                _, err = await _api_dict(
                    "post",
                    "/api/2.0/dbfs/add-block",
                    "write DBFS block",
                    json={
                        "handle": handle,
                        "data": base64.b64encode(data[start : start + _DBFS_BLOCK_BYTES]).decode(),
                    },
                    idempotent=False,
                )
                if err:
                    return err
            _, err = await _api_dict(
                "post", "/api/2.0/dbfs/close", "close DBFS file", json={"handle": handle}, idempotent=False
            )
            if err:
                return err
        elif not workspace:
            _, err = await _raw(
                "put",
                f"/api/2.0/fs/files{_file_path(target)}",
                "upload volume file",
                idempotent=False,
                params={"overwrite": "true" if overwrite else "false"},
                headers={"Content-Type": "application/octet-stream"},
                content=data,
            )
            if err:
                return err
        else:
            _, err = await _api_dict(
                "post",
                "/api/2.0/workspace/import",
                "import workspace file",
                json={
                    "path": target,
                    "content": base64.b64encode(data).decode(),
                    "format": "AUTO",
                    "overwrite": bool(overwrite),
                },
                idempotent=False,
            )
            if err:
                return err
        # A folder path took the stored name: the full path then names the
        # file, so it goes in the block with it.
        where = f"the folder {folder} under its stored name" if folder else target
        return uploaded_file_answer(
            f"Wrote file {file_id} ({round(len(data) / 1024, 1)} KB) to {where}.",
            target=_PROVIDER,
            filename=filename,
            details=[f"Path: {single_line(target)}"] if folder else [],
        )

    # =====================================================================
    # REGISTER ALL TOOLS
    # =====================================================================

    executor.register("list_databricks_warehouses", list_databricks_warehouses)
    executor.register("query_databricks_sql", query_databricks_sql)
    executor.register("execute_databricks_sql", execute_databricks_sql)
    executor.register("get_databricks_statement", get_databricks_statement)
    executor.register("cancel_databricks_statement", cancel_databricks_statement)
    executor.register("list_databricks_catalogs", list_databricks_catalogs)
    executor.register("list_databricks_schemas", list_databricks_schemas)
    executor.register("list_databricks_tables", list_databricks_tables)
    executor.register("describe_databricks_table", describe_databricks_table)
    executor.register("list_databricks_jobs", list_databricks_jobs)
    executor.register("get_databricks_job", get_databricks_job)
    executor.register("run_databricks_job", run_databricks_job)
    executor.register("list_databricks_job_runs", list_databricks_job_runs)
    executor.register("get_databricks_job_run", get_databricks_job_run)
    executor.register("cancel_databricks_job_run", cancel_databricks_job_run)
    executor.register("list_databricks_clusters", list_databricks_clusters)
    executor.register("get_databricks_cluster", get_databricks_cluster)
    executor.register(
        "list_databricks_serving_endpoints", list_databricks_serving_endpoints
    )
    executor.register(
        "get_databricks_serving_endpoint", get_databricks_serving_endpoint
    )
    executor.register(
        "query_databricks_serving_endpoint", query_databricks_serving_endpoint
    )
    executor.register("list_databricks_vector_indexes", list_databricks_vector_indexes)
    executor.register("query_databricks_vector_index", query_databricks_vector_index)
    executor.register("list_databricks_volumes", list_databricks_volumes)
    executor.register("list_databricks_files", list_databricks_files)
    executor.register("read_databricks_file", read_databricks_file)
    executor.register("download_databricks_file", download_databricks_file)
    executor.register("upload_databricks_file", upload_databricks_file)

    _warehouse_hint = (
        " Defaults to the warehouse configured on the integration."
        if default_warehouse_id
        else " Required unless a default warehouse is set on the integration."
    )

    return [
        # -- 0. SQL warehouses + statement execution -----------------------
        ToolDefinition(
            name="list_databricks_warehouses",
            description=(
                "List the Databricks SQL warehouses available in the workspace "
                "with their ID, state, size and serverless flag. Call this "
                "first to get a warehouse_id for query_databricks_sql when no "
                "default warehouse is configured."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="query_databricks_sql",
            description=(
                "Run a READ-ONLY SQL query on a Databricks SQL warehouse and "
                "return the rows as a table. Only SELECT / WITH / SHOW / "
                "DESCRIBE / EXPLAIN / VALUES statements are accepted — anything "
                "that could modify data, schema or session state is refused "
                "(use execute_databricks_sql for those). Use three-level Unity "
                "Catalog names (catalog.schema.table) or pass catalog/schema to "
                "set the default. Prefer parameter markers (:name) with the "
                "parameters argument over string-concatenating values into the "
                "SQL. Long-running queries return a statement_id to poll with "
                "get_databricks_statement."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "statement": {
                        "type": "string",
                        "description": "The read-only SQL statement to run",
                    },
                    "warehouse_id": {
                        "type": "string",
                        "description": (
                            "SQL warehouse to run on (see "
                            "list_databricks_warehouses)." + _warehouse_hint
                        ),
                    },
                    "catalog": {
                        "type": "string",
                        "description": "Default catalog for unqualified names",
                    },
                    "schema": {
                        "type": "string",
                        "description": "Default schema for unqualified names",
                    },
                    "limit": {
                        "type": "integer",
                        "default": _MAX_ROWS,
                        "description": f"Max rows to render (1-{_MAX_ROWS_HARD})",
                    },
                    "parameters": {
                        "type": "string",
                        "description": (
                            "JSON object binding :name parameter markers, e.g. "
                            '{"since": "2026-01-01", "min_count": 10}'
                        ),
                    },
                },
                "required": ["statement"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="execute_databricks_sql",
            description=(
                "Run ANY SQL statement on a Databricks SQL warehouse, including "
                "INSERT, UPDATE, DELETE, MERGE, COPY INTO, CREATE/ALTER/DROP, "
                "OPTIMIZE, VACUUM and GRANT. This MODIFIES the lakehouse — use "
                "query_databricks_sql for reads. Verify the target table with "
                "describe_databricks_table first, and scope destructive "
                "statements with an explicit WHERE clause."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "statement": {
                        "type": "string",
                        "description": "The SQL statement to execute",
                    },
                    "warehouse_id": {
                        "type": "string",
                        "description": (
                            "SQL warehouse to run on." + _warehouse_hint
                        ),
                    },
                    "catalog": {"type": "string"},
                    "schema": {"type": "string"},
                    "limit": {"type": "integer", "default": _MAX_ROWS},
                    "parameters": {
                        "type": "string",
                        "description": "JSON object binding :name parameter markers",
                    },
                },
                "required": ["statement"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_databricks_statement",
            description=(
                "Fetch the status and results of a SQL statement started "
                "earlier by query_databricks_sql or execute_databricks_sql "
                "(use the statement_id they reported when the query outlived "
                "the inline wait budget)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "statement_id": {"type": "string"},
                    "limit": {"type": "integer", "default": _MAX_ROWS},
                },
                "required": ["statement_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="cancel_databricks_statement",
            description=(
                "Cancel a running Databricks SQL statement by its statement_id, "
                "freeing the warehouse."
            ),
            input_schema={
                "type": "object",
                "properties": {"statement_id": {"type": "string"}},
                "required": ["statement_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 1. Unity Catalog ----------------------------------------------
        ToolDefinition(
            name="list_databricks_catalogs",
            description=(
                "List the Unity Catalog catalogs the credential can see (the "
                "top level of the catalog.schema.table namespace). Start here "
                "when exploring what data exists in the lakehouse."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 50},
                    "page_token": {
                        "type": "string",
                        "description": "Pagination token from a previous call",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_databricks_schemas",
            description=(
                "List the schemas (databases) inside a Unity Catalog catalog, "
                "with owner and comment."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "catalog_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                    "page_token": {"type": "string"},
                },
                "required": ["catalog_name"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_databricks_tables",
            description=(
                "List the tables and views in a Unity Catalog schema with their "
                "type, format and owner. Use describe_databricks_table for the "
                "column schema of one of them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "catalog_name": {"type": "string"},
                    "schema_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                    "page_token": {"type": "string"},
                },
                "required": ["catalog_name", "schema_name"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="describe_databricks_table",
            description=(
                "Get a Unity Catalog table's full column schema (names, types, "
                "nullability, comments) plus owner, format, storage location "
                "and timestamps. Call this before writing SQL against a table "
                "so column names and types are correct."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "full_name": {
                        "type": "string",
                        "description": (
                            "Three-level name catalog.schema.table, e.g. "
                            "main.default.events"
                        ),
                    }
                },
                "required": ["full_name"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 2. Jobs & workflows -------------------------------------------
        ToolDefinition(
            name="list_databricks_jobs",
            description=(
                "List Databricks Jobs (Workflows) with their job ID, creator, "
                "task count and schedule. name_filter matches the EXACT job "
                "name (case-insensitive), not a substring - omit it and scan "
                "the list when you only know part of the name."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name_filter": {
                        "type": "string",
                        "description": (
                            "Exact job name, case-insensitive - the Jobs API "
                            "matches the whole name, not a substring"
                        ),
                    },
                    "limit": {"type": "integer", "default": 25},
                    "page_token": {"type": "string"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_databricks_job",
            description=(
                "Get a job's definition: its tasks and their types, the "
                "dependency graph between them, the cron schedule and pause "
                "status, job-level parameters, and concurrency settings."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="run_databricks_job",
            description=(
                "Trigger a Databricks job to run NOW, optionally overriding its "
                "parameters. This starts real compute and can write data — "
                "confirm the job is the intended one with get_databricks_job "
                "first. Returns a run_id to follow with get_databricks_job_run."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "job_parameters": {
                        "type": "string",
                        "description": (
                            "JSON object of job-level parameter overrides, e.g. "
                            '{"env": "prod"}'
                        ),
                    },
                    "notebook_params": {
                        "type": "string",
                        "description": (
                            "JSON object of notebook widget parameters for "
                            "notebook tasks"
                        ),
                    },
                },
                "required": ["job_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_databricks_job_runs",
            description=(
                "List job runs newest-first with their life-cycle and result "
                "state, start time and duration. Scope to one job with job_id, "
                "or to currently-running / finished runs with active_only / "
                "completed_only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "active_only": {"type": "boolean", "default": False},
                    "completed_only": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 20, "description": "1-24"},
                    "page_token": {"type": "string"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_databricks_job_run",
            description=(
                "Get a job run's outcome task by task, with durations, the run "
                "page URL, and — when a task failed — that task's error message "
                "and stack trace. Use this to diagnose a failed workflow."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "include_output": {
                        "type": "boolean",
                        "default": True,
                        "description": "Fetch the failing task's error output",
                    },
                },
                "required": ["run_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="cancel_databricks_job_run",
            description="Cancel a running Databricks job run by its run_id.",
            input_schema={
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_databricks_clusters",
            description=(
                "List all-purpose compute clusters with their state, Databricks "
                "Runtime version, node type and worker count."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 25},
                    "page_token": {"type": "string"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_databricks_cluster",
            description=(
                "Get a cluster's configuration and current state, including its "
                "termination reason when it is not running."
            ),
            input_schema={
                "type": "object",
                "properties": {"cluster_id": {"type": "string"}},
                "required": ["cluster_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 3. Mosaic AI ----------------------------------------------------
        ToolDefinition(
            name="list_databricks_serving_endpoints",
            description=(
                "List Mosaic AI Model Serving endpoints in the workspace with "
                "their readiness, task type and served models."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_databricks_serving_endpoint",
            description=(
                "Get a Model Serving endpoint's served entities (model and "
                "version, workload size, scale-to-zero) and traffic split."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="query_databricks_serving_endpoint",
            description=(
                "Invoke a Databricks Model Serving endpoint. Pass prompt for a "
                "chat/completions endpoint, or payload with a raw JSON body "
                "matching a custom model's signature (e.g. "
                '{"inputs": [[1, 2]]} or {"dataframe_records": [...]}). '
                "Returns the assistant message for chat endpoints and the raw "
                "JSON otherwise."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Endpoint name"},
                    "prompt": {
                        "type": "string",
                        "description": "User prompt for a chat/completions endpoint",
                    },
                    "payload": {
                        "type": "string",
                        "description": (
                            "Raw JSON request body (overrides prompt) for "
                            "custom-signature endpoints"
                        ),
                    },
                    "max_tokens": {"type": "integer", "default": 512},
                    "temperature": {"type": "number", "default": 0.0},
                },
                "required": ["name"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_databricks_vector_indexes",
            description=(
                "List the Vector Search indexes served by a Vector Search "
                "endpoint, with their index type and primary key."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "endpoint_name": {
                        "type": "string",
                        "description": "Vector Search endpoint hosting the indexes",
                    }
                },
                "required": ["endpoint_name"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="query_databricks_vector_index",
            description=(
                "Run a semantic similarity search against a Databricks Vector "
                "Search index and return the matching rows. Use this to ground "
                "an answer in embedded lakehouse documents."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "index_name": {
                        "type": "string",
                        "description": (
                            "Three-level index name catalog.schema.index"
                        ),
                    },
                    "query_text": {"type": "string"},
                    "columns": {
                        "type": "string",
                        "description": "Comma-separated columns to return",
                    },
                    "num_results": {"type": "integer", "default": 10},
                    "filters": {
                        "type": "string",
                        "description": (
                            "JSON object of metadata filters, e.g. "
                            '{"year >=": 2024}'
                        ),
                    },
                },
                "required": ["index_name", "query_text"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 4. Volumes + files ----------------------------------------------
        ToolDefinition(
            name="list_databricks_volumes",
            description=(
                "List the Unity Catalog volumes in a schema with their type, "
                "owner and /Volumes path (the path to pass to "
                "list_databricks_files)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "catalog_name": {"type": "string"},
                    "schema_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                    "page_token": {"type": "string"},
                },
                "required": ["catalog_name", "schema_name"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_databricks_files",
            description=(
                "List the files and subdirectories at a path — a Unity "
                "Catalog volume directory (/Volumes/catalog/schema/volume/...), "
                "a legacy DBFS path (dbfs:/... or /dbfs/...), or a workspace "
                "folder (/Users/..., /Workspace/..., /Repos/... - notebooks, "
                "files, directories)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "page_token": {"type": "string"},
                },
                "required": ["path"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="read_databricks_file",
            description=(
                "Read the text content of a file stored in a Unity Catalog "
                "volume, DBFS or the workspace (a notebook's source). Returns a "
                "bounded preview (binary files are reported, not dumped) — use "
                "it for CSV/JSON/log/config files; download_databricks_file "
                "stores the whole file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_bytes": {
                        "type": "integer",
                        "default": _MAX_FILE_BYTES,
                        "description": f"Byte cap (max {_MAX_FILE_BYTES_HARD})",
                    },
                },
                "required": ["path"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="download_databricks_file",
            workspace_ingest=True,
            description=(
                "Store a whole file as a file of the conversation (download "
                "card + file_id to forward, read inline) with its download URL: "
                "a Unity Catalog volume file (/Volumes/...), a DBFS file "
                "(dbfs:/...), or a workspace object - a notebook exported as "
                "SOURCE (default), HTML, JUPYTER, DBC or R_MARKDOWN, or a "
                "workspace file (AUTO). Workspace exports are limited to 10 MB."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "format": {
                        "type": "string",
                        "enum": list(_EXPORT_FORMATS),
                        "default": "SOURCE",
                        "description": "Workspace export format (ignored for volume and DBFS files)",
                    },
                },
                "required": ["path"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="upload_databricks_file",
            description=(
                "Write a file of the conversation (attached, generated or "
                "downloaded - by file_id) byte-for-byte to a Unity Catalog "
                "volume (/Volumes/...), DBFS (dbfs:/...) or the workspace (a "
                "notebook or file, imported as AUTO, up to 10 MB). A path "
                "ending with '/' keeps the file's own name. overwrite replaces "
                "an existing file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "A file attached, generated or downloaded in this conversation",
                    },
                    "path": {"type": "string", "description": "Destination path"},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["file_id", "path"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]


# ---------------------------------------------------------------------------
# File helpers (module level so the tests can exercise them directly)
# ---------------------------------------------------------------------------


def _is_dbfs_path(path: str) -> bool:
    """Return ``True`` for a legacy DBFS path (as opposed to a UC volume path)."""
    lowered = (path or "").strip().lower()
    return lowered.startswith("dbfs:") or lowered.startswith("/dbfs/")


def _is_volume_path(path: str) -> bool:
    """Return ``True`` for a Unity Catalog volume path (``/Volumes/...``).

    Anything that is neither a volume nor a DBFS path is a workspace path
    (``/Users/...``, ``/Workspace/...``, ``/Repos/...``).
    """
    return (path or "").strip().lstrip("/").lower().startswith("volumes/")


# Workspace source files served as text when ``mimetypes`` has no type for them.
_TEXT_EXTENSIONS = frozenset({"py", "sql", "scala", "r", "rmd", "log", "yml", "yaml", "sh", "md"})


def _normalize_dbfs_path(path: str) -> str:
    """Normalise ``dbfs:/x`` / ``/dbfs/x`` to the ``/x`` the DBFS API expects."""
    value = (path or "").strip()
    if value.lower().startswith("dbfs:"):
        value = value[len("dbfs:") :]
    elif value.lower().startswith("/dbfs/"):
        value = value[len("/dbfs") :]
    return value if value.startswith("/") else "/" + value


def _render_file(path: str, raw: bytes, cap: int) -> str:
    """Render *raw* file bytes as a bounded UTF-8 preview.

    Binary content is reported rather than dumped: a NUL byte in the sampled
    prefix is the cheap, reliable signal (no text format Databricks stores in a
    volume contains one), and undecodable bytes are the fallback signal.
    """
    truncated = len(raw) > cap
    sample = raw[:cap]
    # Bytes actually rendered. Equal to len(sample) unless the repair path below
    # drops an incomplete trailing sequence, in which case ``exc.start`` is the
    # exact new length - so the reported count stays honest without re-encoding
    # the (up to 500 kB) decoded text just to measure it.
    rendered = len(sample)
    if b"\x00" in sample[:4096]:
        return (
            f"{path} looks like a binary file ({len(raw)} bytes read) - it "
            "cannot be rendered as text."
        )
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A truncated read can cut a multi-byte character in half, which is a
        # boundary artefact, not a binary file - without this, any non-ASCII
        # file larger than the cap (accented CSV, JSON with emoji, i18n logs)
        # is reported as unreadable whenever the cut lands mid-sequence. Drop
        # the incomplete tail (a UTF-8 sequence is at most 4 bytes, so only a
        # failure within the last 3 can be one) and keep reporting "not text"
        # when the undecodable byte is somewhere real. ``exc.start`` is the
        # first invalid byte, so everything before it decodes by definition.
        if truncated and exc.start >= len(sample) - 3:
            text = sample[: exc.start].decode("utf-8")
            rendered = exc.start
        else:
            return (
                f"{path} is not valid UTF-8 text ({len(raw)} bytes read) - it "
                "cannot be rendered as text."
            )
    header = f"{path} ({rendered} bytes shown"
    header += ", truncated):" if truncated else "):"
    return f"{header}\n{text}"

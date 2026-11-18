"""Wordsmith (wordsmith.ai), the legal AI platform for in-house legal teams.

Every operation of the documented public API is reachable (see
:mod:`.operations`, the single table the tools are built from, and the
coverage matrix ``dev-docs/reference/wordsmith-api-coverage.md``):

* ``get_wordsmith_me``: who the credential acts as (Test Connection too);
* ``send_wordsmith_agent_question``: ask an agent (wake it), wait up to 20 s
  for the answer, follow up in its session;
* one ``get_wordsmith_<area>`` read tool and one ``manage_wordsmith_<area>``
  write tool per area, each taking ``operation`` plus the path parameters,
  ``query`` and ``body``: a read-only binding withholds every ``manage_*``;
* ``wait_wordsmith_operation``: wait for a long-running operation;
* ``upload_wordsmith_file`` / ``download_wordsmith_file``: a conversation
  file to Wordsmith byte for byte, and back;
* ``wordsmith_api_request`` / ``get_wordsmith_api_request``: any ``/api/v1``
  route, for one Wordsmith adds after this table (the read variant refuses
  every method but GET).

Credentials: an API key (``sk-ws-api1-...``) or an OAuth access token, both
sent as ``Authorization: Bearer``. The token is stored ``host||credential``
when a region host is chosen (``api.us.wordsmith.ai``), else the credential
alone. Rate limits are per organisation (400 requests a minute): one shared
limiter per credential, and 429 / 503 are retried after ``Retry-After``.

Everything Wordsmith returns is third-party content: answers go to the model
inside an untrusted block, and a secret Wordsmith answers with (a webhook
signing secret, a ``callback_secret``) is never echoed.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.file_delivery import deliver_file, file_content_type
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    RateLimiter,
    app_token_cache_key,
    get_shared_rate_limiter,
    parse_compound_token,
    safe_request,
)
from app.integrations.providers.wordsmith.operations import (
    DEDICATED,
    GROUPS,
    IDEMPOTENT_STARTS,
    LEGACY_ALIASES,
    Group,
    Operation,
    covered_routes,
)
from app.services.content_safety import wrap_untrusted
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

__all__ = [
    "DEDICATED",
    "GROUPS",
    "LEGACY_ALIASES",
    "build_tools",
    "covered_routes",
    "redact_secrets",
    "resolve_base_url",
    "validate_connection",
]

PROVIDER = "Wordsmith"
SOURCE = "integration:wordsmith"
DEFAULT_BASE_URL = "https://api.wordsmith.ai/api/v1"
#: Region hosts Wordsmith documents. Either forwards to the other, the
#: region one saves a hop.
REGION_BASE_URLS = {
    "default": DEFAULT_BASE_URL,
    "eu": DEFAULT_BASE_URL,
    "us": "https://api.us.wordsmith.ai/api/v1",
}
#: Wordsmith allows 400 requests a minute per organisation.
_RATE_PER_SECOND = 6.0
_BURST = 20.0
_MAX_CONCURRENCY = 8
_TIMEOUT = 60.0
#: Longest answer body handed to the model; the rest is counted.
_MAX_BODY_CHARS = 40_000
#: Keys whose value is a secret Wordsmith returns once; never shown.
_SECRET_KEYS = frozenset({"secret", "signing_secret", "webhook_secret", "callback_secret", "api_key", "client_secret"})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def resolve_base_url(region: str | None) -> str:
    """The API base for the *region* chosen on the integration."""
    value = (region or "").strip().lower()
    if value.startswith("https://"):
        host = value.rstrip("/")
        if host in {url.lower() for url in REGION_BASE_URLS.values()}:
            return host
        return DEFAULT_BASE_URL
    return REGION_BASE_URLS.get(value, DEFAULT_BASE_URL)


def split_token(token: str) -> tuple[str, str]:
    """``(base_url, credential)`` from the stored token."""
    raw = (token or "").strip()
    if "||" in raw:
        base, credential = parse_compound_token(raw, 2, greedy_last=True)
        return resolve_base_url(base), credential.strip()
    return DEFAULT_BASE_URL, raw


def redact_secrets(value: Any) -> Any:
    """*value* with every secret field replaced, recursively."""
    if isinstance(value, dict):
        return {
            key: (
                "[redacted: shown once by Wordsmith, never repeated]"
                if key in _SECRET_KEYS and item
                else redact_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def _error_detail(resp: httpx.Response | None, err: str) -> str:
    """A refusal with Wordsmith's ``error_code`` and the scope it needs."""
    if resp is None:
        return err if err.startswith("Error") else f"Error: {err}"
    code = ""
    message = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            code = str(data.get("error_code") or data.get("error") or "")
            message = str(data.get("message") or data.get("error_description") or "")
    except ValueError:
        pass
    scope = ""
    challenge = resp.headers.get("WWW-Authenticate", "")
    if 'scope="' in challenge:
        scope = challenge.split('scope="', 1)[1].split('"', 1)[0]
    parts = [f"Error: Wordsmith answered HTTP {resp.status_code}"]
    if code:
        parts.append(f"error_code {code}")
    if scope:
        parts.append(f"needs scope {scope}")
    line = ", ".join(parts) + "."
    if message:
        line += "\n" + wrap_untrusted(message[:500], f"{PROVIDER} error message")
    if code in {"insufficient_scope", "full_access_key_required", "not_available_to_connected_apps"}:
        line += "\nThe credential cannot do this: grant the scope on the key (or reconnect OAuth), or use a full-access API key."
    return line


class WordsmithClient:
    """One credential's access to the API: limiter, retries, errors."""

    def __init__(self, token: str) -> None:
        self.base_url, self.credential = split_token(token)

    def _limiter(self) -> RateLimiter:
        return get_shared_rate_limiter(
            f"wordsmith:{app_token_cache_key('wordsmith', self.credential)}",
            rate=_RATE_PER_SECOND,
            capacity=_BURST,
            max_concurrency=_MAX_CONCURRENCY,
        )

    def headers(self, *, idempotency_key: str = "") -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.credential}", "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any = None,
        idempotency_key: str = "",
        action: str = "",
    ) -> tuple[httpx.Response | None, str | None]:
        """Send one request; ``(response, None)`` or ``(response or None, error)``."""
        if not self.credential:
            return None, "Error: the Wordsmith integration has no API key or OAuth token."
        method = method.upper()
        url = f"{self.base_url}{path}"
        kwargs: dict[str, Any] = {"headers": self.headers(idempotency_key=idempotency_key)}
        if query:
            kwargs["params"] = {key: _query_value(value) for key, value in query.items() if value is not None}
        if body is not None and method != "GET":
            kwargs["json"] = body
        # A read, or a start carrying an Idempotency-Key, is safe to replay
        # after a 502/503/504; any other write is sent once.
        if method == "GET" or idempotency_key:
            kwargs["retry_statuses"] = TRANSIENT_RETRY_STATUSES | {500}
        async with self._limiter().slot(), egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                method.lower(),
                url,
                PROVIDER,
                action or f"{method} {path}",
                expected_statuses={400, 401, 403, 404, 409, 410, 422, 429, 500, 503},
                **kwargs,
            )
        if err:
            return resp, _error_detail(resp, err)
        return resp, None


def _query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return value


def _render(resp: httpx.Response, label: str) -> str:
    """The answer for the model: status, then the body in an untrusted block."""
    if resp.status_code == 204 or not resp.content:
        return f"Done: Wordsmith answered HTTP {resp.status_code} with no content."
    try:
        data: Any = redact_secrets(resp.json())
        text = json.dumps(data, ensure_ascii=False, indent=1, default=str)
    except ValueError:
        text = resp.text
    shown = text[:_MAX_BODY_CHARS]
    more = len(text) - len(shown)
    head = f"Wordsmith HTTP {resp.status_code} ({label})."
    if resp.status_code == 202 and resp.headers.get("Location"):
        head += f" Still running: poll {resp.headers['Location']} after {resp.headers.get('Retry-After', '5')} s."
    tail = f"\n[{more} more characters not shown: narrow the query (limit, cursor, page).]" if more > 0 else ""
    return head + "\n" + wrap_untrusted(shown, f"{PROVIDER} {label}") + tail


def _fill_path(op: Operation, values: dict[str, Any]) -> tuple[str | None, str | None]:
    path = op.path
    for name in op.path_params:
        value = str(values.get(name) or "").strip()
        if not value:
            return None, f"Error: operation '{op.operation}' needs {name}."
        if "/" in value or value in {".", ".."}:
            return None, f"Error: {name} must be an id, not a path."
        path = path.replace("{" + name + "}", value)
    return path, None


_PATH_PARAM_DESCRIPTIONS = {
    "agent_id": "Agent id, or 'default' for the general Wordsmith agent.",
    "resource_type": "repositories, reports, playbooks, blueprints or sessions.",
    "source": "Research source.",
    "link_type": "Link type (as returned by the task).",
}


def _group_tool(group: Group, client: WordsmithClient, executor: CallableToolExecutor) -> ToolDefinition:
    ops = {op.operation: op for op in group.operations}
    path_params = sorted({name for op in group.operations for name in op.path_params})

    async def handler(
        operation: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | list[Any] | None = None,
        **ids: Any,
    ) -> str:
        op = ops.get((operation or "").strip())
        if op is None:
            return f"Error: unknown operation '{operation}'. One of: {', '.join(ops)}."
        path, error = _fill_path(op, ids)
        if error:
            return error
        idempotency_key = ""
        if (op.method, op.path) in IDEMPOTENT_STARTS:
            idempotency_key = str((query or {}).pop("idempotency_key", "") or "") if isinstance(query, dict) else ""
            idempotency_key = idempotency_key or str(uuid.uuid4())
        resp, err = await client.request(
            op.method,
            path or "",
            query=query if isinstance(query, dict) else None,
            body=body,
            idempotency_key=idempotency_key,
            action=op.operation,
        )
        if err:
            return err
        if resp is None:
            return "Error: Wordsmith gave no answer."
        answer = _render(resp, op.operation)
        if idempotency_key:
            answer += f"\nIdempotency-Key: {idempotency_key} (send it again as query.idempotency_key to retry safely)."
        return answer

    handler.__name__ = group.tool
    executor.register(group.tool, handler)
    lines = [f"- {op.operation}: {op.method} {op.path}. {op.summary}" for op in group.operations]
    verb = "Read" if group.access == "read" else "Change"
    description = f"{verb} Wordsmith {group.title}. operation picks the call:\n" + "\n".join(lines)
    properties: dict[str, Any] = {
        "operation": {"type": "string", "enum": list(ops), "description": "The call to make."},
        "query": {"type": "object", "description": "Query parameters (filters, limit, cursor, wait)."},
    }
    if any(op.method != "GET" for op in group.operations):
        properties["body"] = {"type": ["object", "array"], "description": "JSON body, as the Wordsmith docs define it."}
    for name in path_params:
        properties[name] = {
            "type": "string",
            "description": _PATH_PARAM_DESCRIPTIONS.get(name, f"The {name.replace('_', ' ')}."),
        }
    return ToolDefinition(
        name=group.tool,
        description=description,
        input_schema={"type": "object", "properties": properties, "required": ["operation"]},
        source=SOURCE,
        priority=0 if group.access == "read" else 1,
        read_only=group.access == "read",
    )


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    """Every Wordsmith tool for the stored *token*."""
    client = WordsmithClient(token)
    persist_file_fn = kwargs.get("persist_file_fn")
    tools = [_group_tool(group, client, executor) for group in GROUPS]

    async def get_wordsmith_me() -> str:
        """The user and organisation the credential acts as."""
        resp, err = await client.request("GET", "/me", action="get me")
        if err:
            return err
        if resp is None:
            return "Error: Wordsmith gave no answer."
        return _render(resp, "me")

    async def send_wordsmith_agent_question(
        question: str,
        agent_id: str = "default",
        session_id: str = "",
        file_ids: list[str] | None = None,
        upload_job_ids: list[str] | None = None,
        urls: list[str] | None = None,
        wait: int = 20,
        visibility: str = "",
        callback_url: str = "",
        idempotency_key: str = "",
    ) -> str:
        """Ask a Wordsmith agent (a new session, or a follow-up in session_id)."""
        if not (question or "").strip():
            return "Error: question is empty."
        if len(question) > 10_000:
            return "Error: question is longer than Wordsmith's 10,000 characters."
        attachments: list[dict[str, Any]] = [{"file_id": item} for item in file_ids or []]
        attachments += [{"upload_job_id": item} for item in upload_job_ids or []]
        attachments += [{"url": item} for item in urls or []]
        if len(attachments) > 10:
            return "Error: Wordsmith takes at most 10 attachments per question."
        body: dict[str, Any] = {"question": question, "wait": max(0, min(int(wait or 0), 20))}
        if attachments:
            body["attachments"] = attachments
        if session_id.strip():
            body["session_id"] = session_id.strip()
        if visibility.strip():
            body["permissions"] = {"visibility": visibility.strip()}
        if callback_url.strip():
            body["callback_url"] = callback_url.strip()
        agent = (agent_id or "default").strip()
        if "/" in agent:
            return "Error: agent_id must be an id or 'default'."
        key = idempotency_key.strip() or str(uuid.uuid4())
        resp, err = await client.request(
            "POST", f"/agents/{agent}/questions", body=body, idempotency_key=key, action="ask agent"
        )
        if err:
            return err
        if resp is None:
            return "Error: Wordsmith gave no answer."
        answer = _render(resp, "question")
        status = ""
        with contextlib.suppress(ValueError, AttributeError):
            status = str(resp.json().get("status") or "")
        if status == "in_progress":
            answer += (
                "\nNot answered yet: read it with get_wordsmith_agents operation=get_question "
                "(agent_id, question_id = the id above), or wait_wordsmith_operation on its operation_id."
            )
        elif status == "filtered":
            answer += "\nThe agent declined the question (filtered, final): do not retry it."
        return answer + f"\nIdempotency-Key: {key}"

    async def wait_wordsmith_operation(operation_id: str, wait: int = 20) -> str:
        """Wait up to 20 s for an operation, then answer its state."""
        if not operation_id.strip() or "/" in operation_id:
            return "Error: operation_id must be an id."
        resp, err = await client.request(
            "GET",
            f"/operations/{operation_id.strip()}",
            query={"wait": max(0, min(int(wait or 0), 20))},
            action="wait operation",
        )
        if err:
            return err
        if resp is None:
            return "Error: Wordsmith gave no answer."
        return _render(resp, "operation")

    async def upload_wordsmith_file(
        source_file_id: str, file_name: str = "", file_context: FileContext | None = None
    ) -> str:
        """Upload a conversation file byte for byte and return its Wordsmith file_id."""
        if file_context is None:
            return "Error: no conversation files are available to upload."
        resolved = await file_context.resolve_upload(source_file_id.strip())
        if resolved is None:
            return f"Error: '{source_file_id}' is not an available file of this conversation."
        data, original_name, content_type = resolved
        name = (file_name or "").strip() or original_name or "document"
        mime = content_type or "application/octet-stream"
        resp, err = await client.request(
            "POST", "/files/upload-url", body={"file_name": name, "content_type": mime}, action="upload url"
        )
        if err:
            return err
        if resp is None:
            return "Error: Wordsmith gave no answer."
        grant = resp.json()
        upload_url = str(grant.get("upload_url") or "")
        job_id = str(grant.get("job_id") or "")
        if not upload_url.startswith("https://") or not job_id:
            return "Error: Wordsmith did not return an upload URL."
        async with egress_client(timeout=_TIMEOUT) as raw:
            put, put_err = await safe_request(
                raw, "put", upload_url, PROVIDER, "upload file", content=data, headers={"Content-Type": mime}
            )
        if put_err or put is None:
            return f"Error: the upload to Wordsmith's storage failed: {put_err}"
        resp, err = await client.request("POST", f"/files/uploads/{job_id}/complete", action="complete upload")
        if err:
            return err
        if resp is None:
            return "Error: Wordsmith gave no answer."
        return (
            _render(resp, "uploaded file") + f"\nupload_job_id {job_id} was used; pass the file id above as file_ids."
        )

    async def download_wordsmith_file(file_id: str, task_id: str = "") -> str | ToolResult:
        """Store a Wordsmith file (or a task file) as a file of this run."""
        if not file_id.strip() or "/" in file_id or "/" in task_id:
            return "Error: file_id (and task_id) must be ids."
        path = (
            f"/tasks/{task_id.strip()}/files/{file_id.strip()}/download"
            if task_id.strip()
            else f"/files/{file_id.strip()}/download"
        )
        resp, err = await client.request("GET", path, action="download link")
        if err:
            return err
        if resp is None:
            return "Error: Wordsmith gave no answer."
        link = resp.json()
        url = str(link.get("url") or "")
        if not url.startswith("https://"):
            return "Error: Wordsmith returned no download link."
        async with egress_client(timeout=_TIMEOUT, follow_redirects=True) as raw:
            fetched, fetch_err = await safe_request(raw, "get", url, PROVIDER, "download file")
        if fetch_err or fetched is None:
            return f"Error: the download failed: {fetch_err}"
        name = str(link.get("file_name") or f"wordsmith-{file_id}")
        return await deliver_file(
            data=fetched.content,
            filename=name,
            content_type=file_content_type(
                str(link.get("content_type") or fetched.headers.get("content-type") or ""), name
            ),
            source=PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[("Web URL", link.get("web_url"))],
        )

    async def _raw(method: str, path: str, query: dict[str, Any] | None, body: Any, idempotency_key: str) -> str:
        method = (method or "GET").upper()
        path = "/" + (path or "").strip().lstrip("/")
        if path.startswith("/api/v1/"):
            path = path[len("/api/v1") :]
        if ".." in path or "://" in path or "?" in path:
            return "Error: path is a route under /api/v1 such as /tasks/ID/comments; put parameters in query."
        if method not in {"GET", *_WRITE_METHODS}:
            return "Error: method is GET, POST, PUT, PATCH or DELETE."
        resp, err = await client.request(
            method, path, query=query, body=body, idempotency_key=idempotency_key.strip(), action=f"{method} {path}"
        )
        if err:
            return err
        if resp is None:
            return "Error: Wordsmith gave no answer."
        return _render(resp, f"{method} {path}")

    async def get_wordsmith_api_request(path: str, query: dict[str, Any] | None = None) -> str:
        """GET any /api/v1 route."""
        return await _raw("GET", path, query, None, "")

    async def wordsmith_api_request(
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | list[Any] | None = None,
        idempotency_key: str = "",
    ) -> str:
        """Any method on any /api/v1 route (a write)."""
        return await _raw(method, path, query, body, idempotency_key)

    for fn in (
        get_wordsmith_me,
        send_wordsmith_agent_question,
        wait_wordsmith_operation,
        upload_wordsmith_file,
        download_wordsmith_file,
        get_wordsmith_api_request,
        wordsmith_api_request,
    ):
        executor.register(fn.__name__, fn)

    str_list = {"type": "array", "items": {"type": "string"}}
    tools += [
        ToolDefinition(
            name="get_wordsmith_me",
            description="Who the Wordsmith credential acts as: user, email, organisation.",
            input_schema={"type": "object", "properties": {}},
            source=SOURCE,
            priority=0,
            read_only=True,
        ),
        ToolDefinition(
            name="send_wordsmith_agent_question",
            description=(
                "Ask a Wordsmith agent a legal question (reviews, drafting, research), with up to 10 attachments. "
                "Waits up to 20 s for the answer; a follow-up passes session_id. status filtered = declined, final."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "At most 10,000 characters."},
                    "agent_id": {"type": "string", "description": "Agent id (get_wordsmith_agents), or 'default'."},
                    "session_id": {"type": "string", "description": "Continue this session."},
                    "file_ids": {**str_list, "description": "Wordsmith file ids (upload_wordsmith_file)."},
                    "upload_job_ids": {**str_list, "description": "Upload job ids."},
                    "urls": {**str_list, "description": "Public URLs of documents."},
                    "wait": {"type": "integer", "description": "Seconds to wait for the answer, 0 to 20."},
                    "visibility": {"type": "string", "enum": ["private", "organization", "public"]},
                    "callback_url": {"type": "string", "description": "Webhook called when the answer is ready."},
                    "idempotency_key": {"type": "string", "description": "Reuse a previous key to retry safely."},
                },
                "required": ["question"],
            },
            source=SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="wait_wordsmith_operation",
            description="Wait up to 20 s for a Wordsmith operation (a question, a review, a document action) and read its state.",
            input_schema={
                "type": "object",
                "properties": {
                    "operation_id": {"type": "string"},
                    "wait": {"type": "integer", "description": "0 to 20."},
                },
                "required": ["operation_id"],
            },
            source=SOURCE,
            priority=0,
            read_only=True,
        ),
        ToolDefinition(
            name="upload_wordsmith_file",
            description="Upload a file of this conversation to Wordsmith byte for byte; returns its Wordsmith file id.",
            input_schema={
                "type": "object",
                "properties": {"source_file_id": {"type": "string"}, "file_name": {"type": "string"}},
                "required": ["source_file_id"],
            },
            source=SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="download_wordsmith_file",
            description="Download a Wordsmith file (or a task's file with task_id) as a file of this run.",
            input_schema={
                "type": "object",
                "properties": {"file_id": {"type": "string"}, "task_id": {"type": "string"}},
                "required": ["file_id"],
            },
            source=SOURCE,
            priority=1,
            read_only=True,
        ),
        ToolDefinition(
            name="get_wordsmith_api_request",
            description="GET any Wordsmith /api/v1 route not covered by another tool.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "e.g. /tasks/ID/comments"},
                    "query": {"type": "object"},
                },
                "required": ["path"],
            },
            source=SOURCE,
            priority=2,
            read_only=True,
        ),
        ToolDefinition(
            name="wordsmith_api_request",
            description="Call any Wordsmith /api/v1 route with any method (a write): for a route no other tool covers.",
            input_schema={
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                    "path": {"type": "string"},
                    "query": {"type": "object"},
                    "body": {"type": ["object", "array"]},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["method", "path"],
            },
            source=SOURCE,
            priority=2,
        ),
    ]
    return tools


async def validate_connection(token: str) -> tuple[bool, str]:
    """Test Connection: ``GET /me``, then whom and which organisation it names."""
    client = WordsmithClient(token)
    resp, err = await client.request("GET", "/me", action="test connection")
    if err:
        return False, err.removeprefix("Error: ").split("\n", 1)[0]
    if resp is None:
        return False, "Wordsmith gave no answer."
    try:
        data = resp.json()
    except ValueError:
        return False, "Wordsmith answered /me with something that is not JSON."
    if not isinstance(data, dict):
        return False, "Unexpected answer from Wordsmith /me."
    who = data.get("user_email") or data.get("user_name") or data.get("user_id") or "unknown user"
    org = data.get("customer_name") or data.get("customer_id") or "unknown organisation"
    host = client.base_url.split("//", 1)[-1].split("/", 1)[0]
    return True, f"Wordsmith is connected as {who} in {org} ({host})"

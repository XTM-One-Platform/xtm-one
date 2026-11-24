# SPDX-License-Identifier: Apache-2.0

"""The Rydoo tools: every operation of the public API, one tool each.

``operations.json`` lists the 117 operations of the Rydoo public API
(reference and Postman collection, 21 Nov 2026): method, path, the query
parameters the API documents, a summary and a family (``group``). Each
becomes a tool whose input schema is built from it:

* path parameters (``{branchId}``, ``{approverLine}``...) are required
  string arguments, snake_cased;
* query parameters keep their Rydoo name (``branchId``, ``exportedAtStartDate``,
  ``Limit``) so the call is what the reference shows;
* a POST, PUT or PATCH takes ``body``, the JSON object the reference
  documents, sent as is; the two multipart uploads take a ``file_id`` of the
  run instead.

Read or write is the HTTP method: a GET is a read, anything else a write.
Names carry a read verb on a GET and a write verb otherwise, which the
classifier (``services.tool_classification``) confirms, so a read-only
binding withholds every write. Every write is ``always_ask``: the Finance
guardrail is a call-time approval, never a missing tool. The ``bank_details``
family (payment methods: cards and employee bank accounts) is built like
the rest; account and card numbers are masked in every answer, whatever the
tool.

Multi-entity: Rydoo's legal entities are its branches. ``list_rydoo_branches``
names them; every tool whose endpoint filters by branch takes ``branchId``
(or ``BranchId``), and the branch is part of every expense, user and group
the API answers.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from app.integrations.file_delivery import deliver_file, read_pipe_file
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

from ._client import PROVIDER, SOURCE, RydooClient, Upload, seg

#: The longest text a tool answers; the rest is cut with a note.
MAX_ANSWER_CHARS = 24_000
#: The largest file an upload tool sends (receipts and e-invoices).
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
REDACTED = "[redacted]"
#: The family of the payment-method tools (cards and employee bank accounts).
BANK_DETAILS_GROUP = "bank_details"

#: Keys whose value is never answered.
_SECRET_KEYS = frozenset(
    {"token", "accesstoken", "apikey", "clientsecret", "secret", "password", "cvv", "cvc", "pin", "webhooksecret"}
)
#: Keys whose value is masked to its last four characters.
_MASKED_KEYS = frozenset(
    {"iban", "accountnumber", "bankaccountnumber", "cardnumber", "pan", "routingnumber", "sortcode", "bban"}
)
#: The multipart uploads: the form field Rydoo reads the file from.
_UPLOAD_FIELDS = {"upload_rydoo_receipt": "file", "submit_rydoo_e_invoice": "file"}
#: Upload tools that take an idempotency key (the e-invoice reception).
_IDEMPOTENT_UPLOADS = frozenset({"submit_rydoo_e_invoice"})
#: The binary download.
_DOWNLOADS = frozenset({"download_rydoo_receipt"})


def _norm(key: Any) -> str:
    return str(key).replace("_", "").replace("-", "").lower()


def mask(value: Any) -> Any:
    """A bank or card number as its last four characters."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return value
    text = str(value).replace(" ", "").replace("-", "")
    if len(text) <= 4:
        return "****"
    return f"****{text[-4:]}"


def sanitize(data: Any) -> Any:
    """*data* with every secret redacted and every account or card number masked."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            norm = _norm(key)
            if norm in _SECRET_KEYS:
                out[key] = REDACTED
            elif norm in _MASKED_KEYS:
                out[key] = mask(value)
            else:
                out[key] = sanitize(value)
        return out
    if isinstance(data, list):
        return [sanitize(item) for item in data]
    return data


def render(title: str, data: Any) -> str:
    """A tool answer: a title line, then the sanitized data as compact JSON, bounded."""
    if data is None:
        return title
    body = json.dumps(sanitize(data), ensure_ascii=False, default=str, separators=(",", ":"))
    if len(body) > MAX_ANSWER_CHARS:
        body = (
            body[:MAX_ANSWER_CHARS]
            + f"... [cut at {MAX_ANSWER_CHARS} of {len(body)} characters: narrow the call with its filters "
            "or limit / offset]"
        )
    return f"{title}\n{body}"


def _snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


@dataclass(frozen=True)
class Operation:
    """One tool: its name, the call, its arguments."""

    name: str
    summary: str
    method: str
    path: str
    group: str
    query: tuple[str, ...]

    @property
    def write(self) -> bool:
        """Read or write is the HTTP method, never the name."""
        return self.method != "GET"

    @property
    def path_params(self) -> tuple[tuple[str, str], ...]:
        """``(placeholder, argument)`` of every ``{param}`` of the path."""
        params = []
        for part in self.path.split("/"):
            if part.startswith("{") and part.endswith("}"):
                raw = part[1:-1]
                params.append((raw, _snake(raw)))
        return tuple(params)

    @property
    def upload_field(self) -> str:
        return _UPLOAD_FIELDS.get(self.name, "")

    @property
    def takes_body(self) -> bool:
        return self.method in ("POST", "PUT", "PATCH") and not self.upload_field


@cache
def operations() -> tuple[Operation, ...]:
    """The operation table, read once from ``operations.json``."""
    raw = json.loads((Path(__file__).parent / "operations.json").read_text(encoding="utf-8"))
    return tuple(
        Operation(
            name=item["name"],
            summary=item["summary"],
            method=item["method"].upper(),
            path=item["path"],
            group=item["group"],
            query=tuple(dict.fromkeys(item.get("query") or ())),
        )
        for item in raw
    )


_STR = {"type": "string"}


def _query_schema(name: str) -> dict[str, Any]:
    low = name.lower()
    if low in ("limit", "offset"):
        return {"type": "integer", "minimum": 0, "description": f"{name}: page size / rows to skip"}
    if low.startswith(("is", "has", "include", "only", "load")):
        return {"type": "boolean", "description": name}
    if low == "branchids":
        return {"type": "array", "items": _STR, "description": "Branch (entity) ids"}
    if low in ("branchid",):
        return {**_STR, "description": "Branch (legal entity) id: list them with list_rydoo_branches"}
    if "date" in low:
        return {**_STR, "description": f"{name} (ISO 8601, e.g. 2026-11-01)"}
    return {**_STR, "description": name}


def _schema(op: Operation) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for raw, arg in op.path_params:
        description = f"The {raw} of the path"
        if raw == "approverLine":
            description = "The approval line: approver line number (1, 2...) or its name, as Rydoo documents it"
        props[arg] = {**_STR, "description": description}
        required.append(arg)
    for name in op.query:
        props[name] = _query_schema(name)
    if op.takes_body:
        props["body"] = {
            "type": "object",
            "description": "The JSON body the Rydoo API reference documents for this operation, sent as is",
        }
        if op.method in ("POST", "PUT"):
            required.append("body")
    if op.upload_field:
        props["file_id"] = {**_STR, "description": "A file of this conversation (attached, generated or downloaded)"}
        props["fields"] = {
            "type": "object",
            "description": "Other multipart form fields the reference documents (e.g. expenseId), as strings",
        }
        required.append("file_id")
    return {"type": "object", "properties": props, "required": required}


def _description(op: Operation) -> str:
    text = f"Rydoo: {op.summary}. {op.method} {op.path}."
    if op.group == BANK_DETAILS_GROUP:
        text += " Bank details: account and card numbers are masked."
    if op.write:
        text += " Write: a person approves each call."
    return text[:300]


def _page_note(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("nextCursor", "startingAfter", "next"):
            if data.get(key):
                return f" (more: pass startingAfter={data[key]})" if key != "next" else " (more pages)"
    return ""


def build_operation_tools(
    client: RydooClient,
    executor: CallableToolExecutor,
    *,
    persist_file_fn: Any = None,
) -> list[ToolDefinition]:
    """One tool per operation, registered on *executor*."""
    tools: list[ToolDefinition] = []
    for op in operations():
        tools.append(_build_one(op, client, executor, persist_file_fn))
    return tools


def _build_one(
    op: Operation, client: RydooClient, executor: CallableToolExecutor, persist_file_fn: Any
) -> ToolDefinition:
    async def handler(file_context: FileContext | None = None, **kwargs: Any) -> str | ToolResult:
        return await call(op, client, kwargs, file_context=file_context, persist_file_fn=persist_file_fn)

    handler.__name__ = op.name
    executor.register(op.name, handler)
    return ToolDefinition(
        name=op.name,
        description=_description(op),
        input_schema=_schema(op),
        source=SOURCE,
        # Branches first: every multi-entity call starts from them.
        priority=0 if op.name == "list_rydoo_branches" else (2 if op.write else 1),
        # The Finance guardrail: every write asks a person at call time.
        always_ask=op.write,
    )


async def call(
    op: Operation,
    client: RydooClient,
    args: dict[str, Any],
    *,
    file_context: FileContext | None = None,
    persist_file_fn: Any = None,
) -> str | ToolResult:
    """Run *op* with the model's *args*; answers text or a file result."""
    path = op.path
    for raw, arg in op.path_params:
        value = args.get(arg)
        if value in (None, ""):
            return f"Error: {arg} is required."
        path = path.replace("{" + raw + "}", seg(value))
    params = {name: args.get(name) for name in op.query if args.get(name) not in (None, "")}
    action = op.summary[0].lower() + op.summary[1:]
    kwargs: dict[str, Any] = {"params": params}
    if op.takes_body:
        body = args.get("body")
        if body is None and op.method == "PATCH":
            body = {}
        if not isinstance(body, (dict, list)):
            return "Error: body must be the JSON object the Rydoo API reference documents for this operation."
        kwargs["body"] = body
    if op.upload_field:
        piped = await read_pipe_file(
            file_context, str(args.get("file_id") or ""), max_bytes=MAX_UPLOAD_BYTES, target=PROVIDER
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, filename, content_type = piped
        raw_fields = args.get("fields")
        fields: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
        kwargs["files"] = [Upload(op.upload_field, filename, data, content_type)]
        kwargs["form"] = {str(k): str(v) for k, v in fields.items() if v is not None}
        if op.name in _IDEMPOTENT_UPLOADS:
            # One key per call: a retried approval never submits the invoice twice.
            kwargs["headers"] = {"Idempotency-Key": str(uuid.uuid4())}
    if op.name in _DOWNLOADS:
        kwargs["accept"] = "application/pdf"
    resp, err = await client.request(op.method, path, action, **kwargs)
    if err or resp is None:
        return err or f"Error: {PROVIDER} gave no answer."
    if op.name in _DOWNLOADS:
        if not resp.content:
            return f"{PROVIDER}: no receipt file for this expense."
        expense = str(args.get("id") or "expense")
        return await deliver_file(
            data=resp.content,
            filename=f"rydoo-receipt-{expense[:36]}.pdf",
            content_type=resp.content_type or "application/pdf",
            source=SOURCE,
            persist_file_fn=persist_file_fn,
        )
    if op.write:
        title = f"{PROVIDER}: done: {op.summary.lower()}."
    else:
        title = f"{PROVIDER}: {op.summary.lower()}{_page_note(resp.data)}."
    return render(title, resp.data)

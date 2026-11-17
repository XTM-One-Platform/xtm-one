# SPDX-License-Identifier: Apache-2.0

"""DocuSign integration - send documents for e-signature (eSignature REST API v2.1).

API docs: https://developers.docusign.com/docs/esign-rest-api/
Auth: OAuth 2.0 Authorization Code Grant (default) or JWT Grant (an integration
key, an impersonated user id and the RSA private key of the integration key).
Hosts: ``account-d.docusign.com`` (demo) / ``account.docusign.com``
(production) for OAuth; the account's own ``base_uri`` (read once from
``/oauth/userinfo``) for the REST API.

Endpoints used:
  - /oauth/userinfo - the accounts of the token, their base_uri
  - /oauth/token - the JWT Grant
  - /v2.1/accounts/{accountId}/envelopes - create, list
  - /v2.1/accounts/{accountId}/envelopes/{id} - status, send, void
  - /v2.1/accounts/{accountId}/envelopes/{id}/recipients - recipients, resend
  - /v2.1/accounts/{accountId}/envelopes/{id}/documents - documents, download
  - /v2.1/accounts/{accountId}/envelopes/{id}/views/recipient - embedded signing
  - /v2.1/accounts/{accountId}/templates - templates

The credential ``integration_tools._get_token`` assembles:
``environment||account_id||oauth||access_token`` (OAuth, the token refreshed by
the platform) or ``environment||account_id||jwt||integration_key||user_id||private_key``
(JWT Grant; the key is the last field, split greedily).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import jwt

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.file_delivery import (
    deliver_file,
    fetch_file,
    file_content_type,
    read_pipe_file,
)
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    app_token_cache_key,
    bearer_auth_headers,
    get_cached_app_token,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "DocuSign"
_SOURCE = "integration:docusign"
#: Largest document sent in an envelope or downloaded from one.
_MAX_FILE_BYTES = 25 * 1024 * 1024
_MAX_DOCUMENTS = 10
_JWT_SCOPE = "signature impersonation"
_DOCUMENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
_TAB_KEYS = {
    "sign_here": "signHereTabs",
    "initial_here": "initialHereTabs",
    "date_signed": "dateSignedTabs",
    "full_name": "fullNameTabs",
    "title": "titleTabs",
    "text": "textTabs",
}
_ENVELOPE_STATUSES = frozenset(
    {"created", "sent", "delivered", "completed", "declined", "voided", "signed", "deleted"}
)


# ---------------------------------------------------------------------------
# Hosts and credentials
# ---------------------------------------------------------------------------


def auth_host(environment: str) -> str:
    """The OAuth host of *environment*: ``demo`` or anything else (production)."""
    return (
        "account-d.docusign.com"
        if (environment or "").strip().lower() == "demo"
        else "account.docusign.com"
    )


def normalize_private_key(key: str) -> str:
    """A PEM key as pasted: newlines restored when a one-line field stripped them."""
    text = (key or "").strip().replace("\\n", "\n")
    if "\n" in text or "-----" not in text:
        return text
    match = re.match(r"(-----BEGIN [A-Z ]+-----)(.*?)(-----END [A-Z ]+-----)$", text, re.S)
    if not match:
        return text
    head, body, tail = match.groups()
    body = re.sub(r"\s+", "", body)
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return "\n".join([head, *lines, tail])


def compound_credential(
    secret: str, extra: dict, *, jwt_grant: bool, integration_key: str = ""
) -> str:
    """The compound credential the tools and the connection test run on.

    *secret* is the OAuth access token, or the RSA private key of the JWT Grant;
    *extra* the integration's ``extra_config`` (``environment``, ``account_id``,
    and for the JWT Grant ``integration_key`` and ``user_id``); *integration_key*
    the stored client id, used when ``extra`` has no ``integration_key``. The
    JWT Grant needs all three of the key material, the integration key and the
    user id: an empty string says one is missing. Format in the module docstring.
    """
    extra = extra if isinstance(extra, dict) else {}
    environment = str(extra.get("environment") or "production").strip()
    account_id = str(extra.get("account_id") or "").strip()
    if not jwt_grant:
        return f"{environment}||{account_id}||oauth||{secret or ''}"
    key_id = (str(extra.get("integration_key") or "").strip() or (integration_key or "")).strip()
    user_id = str(extra.get("user_id") or "").strip()
    if not (secret and key_id and user_id):
        return ""
    return "||".join([environment, account_id, "jwt", key_id, user_id, secret])


def parse_token(token: str) -> dict[str, str]:
    """The fields of the compound credential (see the module docstring)."""
    environment, account_id, mode, rest = parse_compound_token(
        token or "", 4, greedy_last=True
    )
    mode = mode.strip().lower()
    if mode not in ("oauth", "jwt"):
        # A bare token (the catalog's dummy, a legacy row): an OAuth access
        # token on the production host, the default account.
        return {
            "environment": "production",
            "account_id": "",
            "mode": "oauth",
            "access_token": (token or "").strip(),
        }
    fields = {
        "environment": environment.strip().lower() or "production",
        "account_id": account_id.strip(),
        "mode": mode,
    }
    if mode == "jwt":
        integration_key, user_id, private_key = parse_compound_token(rest, 3, greedy_last=True)
        fields.update(
            integration_key=integration_key.strip(),
            user_id=user_id.strip(),
            private_key=normalize_private_key(private_key),
        )
    else:
        fields["access_token"] = rest.strip()
    return fields


async def jwt_grant_token(
    environment: str, integration_key: str, user_id: str, private_key: str
) -> tuple[str | None, str | None]:
    """An access token minted with the JWT Grant, cached until shortly before it expires."""
    if not (integration_key and user_id and private_key):
        return None, (
            "Error: DocuSign JWT Grant needs the integration key, the user ID to "
            "impersonate and the RSA private key."
        )
    cache_key = app_token_cache_key("docusign", environment, integration_key, user_id, private_key)
    cached = get_cached_app_token(cache_key)
    if cached:
        return cached, None
    host = auth_host(environment)
    now = int(time.time())
    try:
        assertion = jwt.encode(
            {
                "iss": integration_key,
                "sub": user_id,
                "aud": host,
                "iat": now,
                "exp": now + 3600,
                "scope": _JWT_SCOPE,
            },
            private_key,
            algorithm="RS256",
        )
    except Exception as exc:
        logger.warning("DocuSign: cannot sign the JWT assertion: %s", format_exception_message(exc))
        return None, (
            "Error: DocuSign JWT Grant failed - the RSA private key could not sign the "
            "assertion (check the key)."
        )
    async with egress_client(timeout=20) as client:
        resp, err = await safe_request(
            client,
            "post",
            f"https://{host}/oauth/token",
            _PROVIDER,
            "mint a JWT Grant access token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
    if err:
        if "consent_required" in err:
            err += (
                "\nThe impersonated user has not consented to the integration key: open "
                f"https://{host}/oauth/auth?response_type=code&scope=signature%20impersonation "
                "once, signed in as that user, with client_id set to the integration key and "
                "redirect_uri set to one of the key's redirect URIs."
            )
        return None, err
    data = resp.json() or {}
    access = data.get("access_token")
    if not access:
        return None, "Error: DocuSign answered the JWT Grant without an access token."
    set_cached_app_token(cache_key, access, data.get("expires_in"))
    return access, None


async def resolve_account(
    environment: str, access_token: str, account_id: str = ""
) -> tuple[dict[str, str] | None, str | None]:
    """``{account_id, account_name, base_url}`` of *account_id* (or the default
    account) for *access_token*, from ``/oauth/userinfo``; cached per token."""
    cache_key = app_token_cache_key("docusign-account", environment, account_id, access_token)
    cached = get_cached_app_token(cache_key)
    if cached:
        return json.loads(cached), None
    host = auth_host(environment)
    async with egress_client(timeout=20) as client:
        resp, err = await safe_request(
            client,
            "get",
            f"https://{host}/oauth/userinfo",
            _PROVIDER,
            "read the accounts of the token",
            headers=bearer_auth_headers(access_token, extra={"Accept": "application/json"}),
            retry_statuses=TRANSIENT_RETRY_STATUSES,
        )
    if err:
        return None, err
    accounts = [a for a in (resp.json() or {}).get("accounts", []) if isinstance(a, dict)]
    chosen = None
    wanted = (account_id or "").strip().lower()
    if wanted:
        chosen = next((a for a in accounts if str(a.get("account_id", "")).lower() == wanted), None)
        if chosen is None:
            listed = ", ".join(f"{a.get('account_name')} ({a.get('account_id')})" for a in accounts)
            return None, (
                f"Error: the DocuSign account '{account_id}' is not one of the token's accounts"
                + (f": {listed}." if listed else ".")
            )
    else:
        chosen = next((a for a in accounts if a.get("is_default")), accounts[0] if accounts else None)
    if chosen is None or not chosen.get("base_uri"):
        return None, (
            "Error: DocuSign returned no account for this token - check the user and the account ID."
        )
    base_uri = str(chosen["base_uri"]).rstrip("/")
    account = {
        "account_id": str(chosen.get("account_id", "")),
        "account_name": str(chosen.get("account_name", "")),
        "base_url": f"{base_uri}/restapi/v2.1/accounts/{quote(str(chosen.get('account_id', '')), safe='')}",
    }
    set_cached_app_token(cache_key, json.dumps(account), 3600)
    return account, None


async def _access_token(fields: dict[str, str]) -> tuple[str | None, str | None]:
    if fields["mode"] == "jwt":
        return await jwt_grant_token(
            fields["environment"], fields["integration_key"], fields["user_id"], fields["private_key"]
        )
    access = fields.get("access_token", "")
    if not access:
        return None, "Error: the DocuSign integration has no access token - authorize it again."
    return access, None


async def validate_connection(token: str) -> tuple[bool, str]:
    """``(ok, message)`` after ``/oauth/userinfo`` and ``GET /v2.1/accounts/{id}``."""
    fields = parse_token(token)
    access, err = await _access_token(fields)
    if err or not access:
        return False, err or "Error: no DocuSign credential."
    account, err = await resolve_account(fields["environment"], access, fields["account_id"])
    if err or account is None:
        return False, err or "Error: no DocuSign account."
    async with egress_client(timeout=20) as client:
        resp, err = await safe_request(
            client,
            "get",
            account["base_url"],
            _PROVIDER,
            "read the account",
            headers=bearer_auth_headers(access, extra={"Accept": "application/json"}),
            retry_statuses=TRANSIENT_RETRY_STATUSES,
        )
    if err:
        return False, err
    name = (resp.json() or {}).get("accountName") or account["account_name"]
    return True, (
        f"DocuSign is connected: account '{name}' ({account['account_id']}, {fields['environment']})."
    )


# ---------------------------------------------------------------------------
# Payload helpers (pure, tested)
# ---------------------------------------------------------------------------


def _is_document(filename: str, content_type: str) -> bool:
    lowered = (filename or "").lower()
    return content_type in _DOCUMENT_TYPES or lowered.endswith((".pdf", ".docx", ".doc"))


def build_document(document_id: int, filename: str, data: bytes) -> dict[str, str]:
    """One ``documents[]`` entry: the bytes base64-encoded, byte for byte."""
    name = filename or f"document-{document_id}"
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else "pdf"
    return {
        "documentId": str(document_id),
        "name": name,
        "fileExtension": extension,
        "documentBase64": base64.b64encode(data).decode("ascii"),
    }


def build_tabs(anchor_tabs: list[dict] | None) -> dict[str, list[dict]]:
    """DocuSign ``tabs`` from ``[{anchor_string, type, x_offset, y_offset, page?}]``.

    Without anchors the envelope is free-form: the signer places the fields.
    """
    tabs: dict[str, list[dict]] = {}
    for tab in anchor_tabs or []:
        if not isinstance(tab, dict) or not str(tab.get("anchor_string", "")).strip():
            continue
        key = _TAB_KEYS.get(str(tab.get("type", "sign_here")).strip().lower(), "signHereTabs")
        entry: dict[str, Any] = {
            "anchorString": str(tab["anchor_string"]).strip(),
            "anchorUnits": "pixels",
            "anchorXOffset": str(int(tab.get("x_offset", 0) or 0)),
            "anchorYOffset": str(int(tab.get("y_offset", 0) or 0)),
            "anchorIgnoreIfNotPresent": "false",
        }
        if tab.get("page"):
            entry["pageNumber"] = str(int(tab["page"]))
        if key == "textTabs" and tab.get("value") is not None:
            entry["value"] = str(tab["value"])
        tabs.setdefault(key, []).append(entry)
    return tabs


def build_recipients(
    signers: list[dict],
    carbon_copies: list[dict] | None,
    tabs: dict[str, list[dict]],
    *,
    embedded: bool,
) -> tuple[dict[str, list[dict]], str | None]:
    """DocuSign ``recipients`` from the tool's signers and carbon copies."""
    out_signers: list[dict] = []
    for index, signer in enumerate(signers or [], start=1):
        if not isinstance(signer, dict):
            return {}, "Error: each signer is an object {name, email, routing_order?, role?}."
        name = str(signer.get("name", "")).strip()
        email = str(signer.get("email", "")).strip()
        if not name or not email or "@" not in email:
            return {}, f"Error: signer {index} needs a name and an email address."
        entry: dict[str, Any] = {
            "recipientId": str(index),
            "name": name,
            "email": email,
            "routingOrder": str(int(signer.get("routing_order", index) or index)),
        }
        if signer.get("role"):
            entry["roleName"] = str(signer["role"])[:100]
        if embedded:
            entry["clientUserId"] = email
        if tabs:
            entry["tabs"] = tabs
        out_signers.append(entry)
    if not out_signers:
        return {}, "Error: at least one signer is required."
    recipients: dict[str, list[dict]] = {"signers": out_signers}
    ccs: list[dict] = []
    for index, cc in enumerate(carbon_copies or [], start=len(out_signers) + 1):
        if not isinstance(cc, dict):
            continue
        name = str(cc.get("name", "")).strip()
        email = str(cc.get("email", "")).strip()
        if name and email:
            ccs.append(
                {"recipientId": str(index), "name": name, "email": email, "routingOrder": str(index)}
            )
    if ccs:
        recipients["carbonCopies"] = ccs
    return recipients, None


def _fmt_recipient(r: dict) -> str:
    parts = [f"{r.get('name', '?')} <{r.get('email', '?')}>", f"status: {r.get('status', '?')}"]
    for key, label in (
        ("sentDateTime", "sent"),
        ("deliveredDateTime", "delivered"),
        ("signedDateTime", "signed"),
        ("declinedDateTime", "declined"),
    ):
        if r.get(key):
            parts.append(f"{label} {r[key]}")
    if r.get("declinedReason"):
        parts.append(f"reason: {str(r['declinedReason'])[:200]}")
    return " | ".join(parts)


def format_envelope(
    envelope: dict, recipients: dict | None = None, documents: list | None = None
) -> str:
    """A readable envelope: status, dates, recipients, documents."""
    lines = [
        f"Envelope {envelope.get('envelopeId', '?')} - {envelope.get('emailSubject', '')} - "
        f"status: {envelope.get('status', '?')}"
    ]
    for key, label in (
        ("createdDateTime", "Created"),
        ("sentDateTime", "Sent"),
        ("deliveredDateTime", "Delivered"),
        ("completedDateTime", "Completed"),
        ("declinedDateTime", "Declined"),
        ("voidedDateTime", "Voided"),
        ("expireDateTime", "Expires"),
    ):
        if envelope.get(key):
            lines.append(f"  {label}: {envelope[key]}")
    if envelope.get("voidedReason"):
        lines.append(f"  Void reason: {str(envelope['voidedReason'])[:200]}")
    if recipients:
        for kind, label in (("signers", "Signers"), ("carbonCopies", "Carbon copies")):
            items = [r for r in recipients.get(kind, []) if isinstance(r, dict)]
            if items:
                lines.append(f"  {label}:")
                lines.extend(f"    - {_fmt_recipient(r)}" for r in items)
    if documents:
        lines.append("  Documents:")
        lines.extend(
            f"    - {d.get('name', '?')} (document id: {d.get('documentId', '?')}, {d.get('type', 'content')})"
            for d in documents
            if isinstance(d, dict)
        )
    return "\n".join(lines)


def _web_link(environment: str, envelope_id: str) -> str:
    host = "appdemo.docusign.com" if environment == "demo" else "app.docusign.com"
    return f"https://{host}/documents/details/{quote(envelope_id, safe='')}"


# ---------------------------------------------------------------------------
# Envelope events (integration_event triggers, polled by services/event_poller)
# ---------------------------------------------------------------------------

#: The envelope status each envelope event stands for.
ENVELOPE_EVENT_STATUSES = {
    "envelope_completed": "completed",
    "envelope_declined": "declined",
    "envelope_voided": "voided",
}
RECIPIENT_COMPLETED = "recipient_completed"
#: Every DocuSign event an ``integration_event`` trigger can select.
ENVELOPE_EVENTS = frozenset({*ENVELOPE_EVENT_STATUSES, RECIPIENT_COMPLETED})
#: Recipient statuses DocuSign gives a signer who has signed.
_SIGNED_STATUSES = frozenset({"completed", "signed"})
_FRACTION_PAST_MICROSECONDS = re.compile(r"(\.\d{6})\d+")


def event_instant(value: object) -> datetime:
    """A DocuSign timestamp as an aware datetime; the oldest one possible when unreadable.

    DocuSign writes up to seven fractional digits, one more than a datetime holds.
    """
    text = _FRACTION_PAST_MICROSECONDS.sub(r"\1", str(value or "")).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def envelope_changed_at(envelope: dict) -> str:
    """When *envelope* last changed, as DocuSign wrote it (the poll's cursor)."""
    return str(
        envelope.get("statusChangedDateTime")
        or envelope.get("lastModifiedDateTime")
        or envelope.get("completedDateTime")
        or ""
    )


def _signers(envelope: dict) -> list[dict]:
    recipients = envelope.get("recipients") if isinstance(envelope.get("recipients"), dict) else {}
    return [r for r in recipients.get("signers", []) if isinstance(r, dict)]


def envelope_event_payload(
    envelope: dict, event: str, *, environment: str = "production", recipient: dict | None = None
) -> dict:
    """The trigger payload of a DocuSign envelope event.

    ``message_id`` names the event (the envelope and its new status, or the
    envelope and the recipient who signed), so each one runs once per
    assignment; ``envelope_id`` names the envelope to act on.
    """
    envelope_id = str(envelope.get("envelopeId", ""))
    status = str(envelope.get("status", "")).lower()
    sender = envelope.get("sender") if isinstance(envelope.get("sender"), dict) else {}
    signers = [
        {
            "name": str(s.get("name", "")),
            "email": str(s.get("email", "")),
            "status": str(s.get("status", "")),
            "signed_at": str(s.get("signedDateTime", "")),
            "declined_reason": str(s.get("declinedReason", ""))[:500],
        }
        for s in _signers(envelope)
    ]
    acting = recipient
    if acting is None and event == "envelope_declined":
        acting = next((s for s in _signers(envelope) if str(s.get("status", "")).lower() == "declined"), None)
    if event == RECIPIENT_COMPLETED and recipient is not None:
        message_id = f"{envelope_id}:recipient:{recipient.get('recipientId', recipient.get('email', ''))}:completed"
    else:
        message_id = f"{envelope_id}:{status}"
    return {
        "envelope_id": envelope_id,
        "status": status,
        "subject": str(envelope.get("emailSubject", "")),
        "sender_email": str(sender.get("email", "")),
        "sender_name": str(sender.get("userName", "")),
        "signer_email": str((acting or {}).get("email", "")),
        "signer_name": str((acting or {}).get("name", "")),
        "signer_emails": [s["email"] for s in signers if s["email"]],
        "signers": signers,
        "sent_at": str(envelope.get("sentDateTime", "")),
        "completed_at": str(envelope.get("completedDateTime", "")),
        "declined_at": str(envelope.get("declinedDateTime", "")),
        "voided_at": str(envelope.get("voidedDateTime", "")),
        "voided_reason": str(envelope.get("voidedReason", ""))[:500],
        "status_changed_at": envelope_changed_at(envelope),
        "web_link": _web_link(environment, envelope_id) if envelope_id else "",
        "message_id": message_id if envelope_id else "",
        "trigger_type": "integration_event",
        "event": event,
        "provider": "docusign",
    }


def envelope_events(
    envelope: dict, event: str, *, since: str = "", environment: str = "production"
) -> list[dict]:
    """The payloads of *event* that *envelope* carries.

    An envelope event when the envelope has its status; ``recipient_completed``
    once per signer who signed after *since* (every signer who signed when
    *since* is empty).
    """
    if event in ENVELOPE_EVENT_STATUSES:
        if str(envelope.get("status", "")).lower() != ENVELOPE_EVENT_STATUSES[event]:
            return []
        return [envelope_event_payload(envelope, event, environment=environment)]
    if event != RECIPIENT_COMPLETED:
        return []
    floor = event_instant(since) if since else None
    payloads = []
    for signer in _signers(envelope):
        if str(signer.get("status", "")).lower() not in _SIGNED_STATUSES:
            continue
        if floor is not None and event_instant(signer.get("signedDateTime")) <= floor:
            continue
        payloads.append(envelope_event_payload(envelope, event, environment=environment, recipient=signer))
    return payloads


async def list_envelope_changes(
    token: str, from_date: str, count: int
) -> tuple[list[dict], str, str | None]:
    """``(envelopes, environment, error)``: the envelopes changed since *from_date*,
    oldest first, with their recipients (``GET /envelopes``, listStatusChanges)."""
    fields = parse_token(token)
    environment = fields["environment"]
    access, err = await _access_token(fields)
    if err or not access:
        return [], environment, err or "Error: no DocuSign credential."
    account, err = await resolve_account(environment, access, fields["account_id"])
    if err or account is None:
        return [], environment, err or "Error: no DocuSign account."
    async with egress_client(timeout=30) as client:
        resp, err = await safe_request(
            client,
            "get",
            f"{account['base_url']}/envelopes",
            _PROVIDER,
            "list the envelopes that changed",
            headers=bearer_auth_headers(access, extra={"Accept": "application/json"}),
            params={
                "from_date": from_date,
                "order": "asc",
                "order_by": "last_modified",
                "count": max(1, min(int(count), 100)),
                "include": "recipients",
            },
            retry_statuses=TRANSIENT_RETRY_STATUSES,
        )
    if err:
        return [], environment, err
    try:
        data = resp.json() or {}
    except ValueError:
        data = {}
    envelopes = data.get("envelopes") if isinstance(data, dict) else None
    return [e for e in envelopes or [] if isinstance(e, dict)], environment, None


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build the DocuSign tools from the compound credential (module docstring)."""
    fields = parse_token(token)
    if not (fields.get("access_token") or fields.get("private_key")):
        return []
    environment = fields["environment"]
    persist_file_fn = _kwargs.get("persist_file_fn")

    async def _session() -> tuple[str, str, str | None]:
        """``(access_token, account base URL, error)``."""
        access, err = await _access_token(fields)
        if err or not access:
            return "", "", err
        account, err = await resolve_account(environment, access, fields["account_id"])
        if err or account is None:
            return "", "", err
        return access, account["base_url"], None

    def _headers(access: str) -> dict[str, str]:
        return bearer_auth_headers(
            access, extra={"Accept": "application/json", "Content-Type": "application/json"}
        )

    async def _call(
        method: str,
        path: str,
        action: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
        read: bool = False,
    ) -> tuple[dict | None, str | None]:
        access, base, err = await _session()
        if err:
            return None, err
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{base}/{path.lstrip('/')}",
                _PROVIDER,
                action,
                headers=_headers(access),
                params=params,
                json=body,
                retry_statuses=TRANSIENT_RETRY_STATUSES if read else None,
            )
        if err:
            return None, err
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {"items": data}, None

    async def _envelope_payload(
        file_ids: list[str],
        email_subject: str,
        email_message: str,
        signers: list[dict],
        carbon_copies: list[dict] | None,
        anchor_tabs: list[dict] | None,
        status: str,
        embedded: bool,
        file_context: FileContext | None,
    ) -> tuple[dict | None, str | None]:
        subject = (email_subject or "").strip()
        if not subject:
            return None, "Error: email_subject is required."
        ids = [str(f).strip() for f in (file_ids or []) if str(f).strip()]
        if not ids:
            return None, (
                "Error: file_ids must name at least one PDF or Word file of the conversation."
            )
        if len(ids) > _MAX_DOCUMENTS:
            return None, f"Error: at most {_MAX_DOCUMENTS} documents per envelope."
        documents: list[dict] = []
        for index, file_id in enumerate(ids, start=1):
            piped = await read_pipe_file(
                file_context, file_id, max_bytes=_MAX_FILE_BYTES, target="DocuSign"
            )
            if isinstance(piped, str):
                return None, piped
            data, filename, content_type = piped
            if not _is_document(filename, content_type):
                return None, (
                    f"Error: '{filename}' ({content_type}) is not a PDF or Word document - "
                    "DocuSign envelopes take PDF, DOC or DOCX files."
                )
            documents.append(build_document(index, filename, data))
        tabs = build_tabs(anchor_tabs)
        recipients, err = build_recipients(signers, carbon_copies, tabs, embedded=embedded)
        if err:
            return None, err
        wanted = (status or "sent").strip().lower()
        if wanted not in ("created", "sent"):
            return None, "Error: status must be 'created' (a draft) or 'sent'."
        payload: dict[str, Any] = {
            "emailSubject": subject[:100],
            "documents": documents,
            "recipients": recipients,
            "status": wanted,
        }
        if (email_message or "").strip():
            payload["emailBlurb"] = email_message.strip()[:10000]
        return payload, None

    async def _signing_urls(envelope_id: str, recipients: dict, return_url: str) -> list[str]:
        lines: list[str] = []
        for signer in recipients.get("signers", []):
            data, err = await _call(
                "post",
                f"envelopes/{quote(envelope_id, safe='')}/views/recipient",
                f"create the signing view of {signer['email']}",
                body={
                    "returnUrl": return_url,
                    "authenticationMethod": "none",
                    "email": signer["email"],
                    "userName": signer["name"],
                    "clientUserId": signer["clientUserId"],
                },
            )
            if err:
                lines.append(f"  {signer['email']}: {err.splitlines()[0]}")
            else:
                lines.append(f"  {signer['email']}: {data.get('url', '?')}")
        return lines

    # ── Envelopes ────────────────────────────────────────────────────────

    async def create_docusign_envelope(
        file_ids: list[str],
        email_subject: str,
        signers: list[dict],
        email_message: str = "",
        carbon_copies: list[dict] | None = None,
        anchor_tabs: list[dict] | None = None,
        status: str = "sent",
        embedded: bool = False,
        return_url: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Send files of the conversation to signers as a DocuSign envelope."""
        if embedded and not (return_url or "").strip():
            return "Error: embedded signing needs a return_url."
        payload, err = await _envelope_payload(
            file_ids,
            email_subject,
            email_message,
            signers,
            carbon_copies,
            anchor_tabs,
            status,
            embedded,
            file_context,
        )
        if err or payload is None:
            return ToolResult(tool_call_id="", content=err or "Error", is_error=True)
        data, err = await _call("post", "envelopes", "create the envelope", body=payload)
        if err:
            return err
        envelope_id = str(data.get("envelopeId", ""))
        lines = [
            f"Envelope {envelope_id} {data.get('status', payload['status'])}: "
            f"'{payload['emailSubject']}' to {len(payload['recipients']['signers'])} signer(s), "
            f"{len(payload['documents'])} document(s).",
            f"Web link: {_web_link(environment, envelope_id)}",
        ]
        if not build_tabs(anchor_tabs):
            lines.append(
                "No anchor tabs given: the signers place their signature themselves (free-form signing)."
            )
        if embedded and envelope_id:
            lines.append("Embedded signing URLs (valid 5 minutes):")
            lines.extend(await _signing_urls(envelope_id, payload["recipients"], return_url.strip()))
        return "\n".join(lines)

    async def send_docusign_envelope(envelope_id: str) -> str:
        """Send a draft ('created') envelope."""
        data, err = await _call(
            "put",
            f"envelopes/{quote(envelope_id.strip(), safe='')}",
            f"send envelope {envelope_id}",
            body={"status": "sent"},
        )
        if err:
            return err
        return f"Envelope {envelope_id} sent (status: {data.get('status', 'sent')})."

    async def get_docusign_envelope(envelope_id: str) -> str:
        """Status, dates, recipients and documents of an envelope."""
        eid = quote(envelope_id.strip(), safe="")
        envelope, err = await _call("get", f"envelopes/{eid}", f"read envelope {envelope_id}", read=True)
        if err:
            return err
        recipients, r_err = await _call(
            "get", f"envelopes/{eid}/recipients", f"read the recipients of {envelope_id}", read=True
        )
        documents, d_err = await _call(
            "get", f"envelopes/{eid}/documents", f"read the documents of {envelope_id}", read=True
        )
        text = format_envelope(
            envelope,
            recipients if not r_err else None,
            (documents or {}).get("envelopeDocuments") if not d_err else None,
        )
        for problem in (r_err, d_err):
            if problem:
                text += f"\n  Not read: {problem.splitlines()[0]}"
        return f"{text}\nWeb link: {_web_link(environment, envelope_id.strip())}"

    async def list_docusign_envelopes(
        from_date: str = "",
        to_date: str = "",
        status: str = "",
        search_text: str = "",
        count: int = 20,
    ) -> str:
        """List envelopes changed since from_date (default: the last 30 days), newest first."""
        params: dict[str, Any] = {
            "from_date": (from_date or "").strip()
            or (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "order": "desc",
            "order_by": "last_modified",
            "count": max(1, min(int(count or 20), 100)),
            "include": "recipients",
        }
        if (to_date or "").strip():
            params["to_date"] = to_date.strip()
        wanted = (status or "").strip().lower()
        if wanted:
            if wanted not in _ENVELOPE_STATUSES:
                return f"Error: status must be one of {', '.join(sorted(_ENVELOPE_STATUSES))}."
            params["status"] = wanted
        if (search_text or "").strip():
            params["search_text"] = search_text.strip()[:200]
        data, err = await _call("get", "envelopes", "list envelopes", params=params, read=True)
        if err:
            return err
        envelopes = [e for e in data.get("envelopes", []) if isinstance(e, dict)]
        if not envelopes:
            return "No envelope matches."
        lines = [f"{len(envelopes)} envelope(s) (of {data.get('totalSetSize', len(envelopes))}):"]
        lines.extend(format_envelope(e, e.get("recipients")) for e in envelopes)
        return "\n".join(lines)

    async def download_docusign_document(
        envelope_id: str, document_id: str = "combined", certificate: bool = False
    ) -> str | ToolResult:
        """Download a document of an envelope (default: all documents combined) as PDF."""
        access, base, err = await _session()
        if err:
            return err
        doc = (document_id or "combined").strip() or "combined"
        eid = envelope_id.strip()
        url = f"{base}/envelopes/{quote(eid, safe='')}/documents/{quote(doc, safe='')}"
        async with egress_client(timeout=120) as client:
            fetched = await fetch_file(
                client,
                "get",
                url,
                provider=_PROVIDER,
                action=f"download document {doc} of envelope {eid}",
                max_bytes=_MAX_FILE_BYTES,
                headers=bearer_auth_headers(access),
                params={"certificate": "true" if certificate else "false"},
            )
        if isinstance(fetched, str):
            return fetched
        filename = fetched.filename or f"docusign-{eid}-{doc}.pdf"
        return await deliver_file(
            data=fetched.data,
            filename=filename,
            content_type=file_content_type(fetched.content_type, filename),
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[("Envelope", _web_link(environment, eid))],
            details=[
                f"Document {doc} of envelope {eid}"
                + (" with the certificate of completion." if certificate else ".")
            ],
        )

    async def void_docusign_envelope(envelope_id: str, reason: str) -> str:
        """Void a sent envelope with a reason the recipients see."""
        if not (reason or "").strip():
            return "Error: a reason is required to void an envelope."
        data, err = await _call(
            "put",
            f"envelopes/{quote(envelope_id.strip(), safe='')}",
            f"void envelope {envelope_id}",
            body={"status": "voided", "voidedReason": reason.strip()[:200]},
        )
        if err:
            return err
        return f"Envelope {envelope_id} voided (status: {data.get('status', 'voided')})."

    async def resend_docusign_envelope(envelope_id: str) -> str:
        """Resend the signing notification to the recipients still pending."""
        eid = quote(envelope_id.strip(), safe="")
        recipients, err = await _call(
            "get", f"envelopes/{eid}/recipients", f"read the recipients of {envelope_id}", read=True
        )
        if err:
            return err
        pending = [
            {"recipientId": r.get("recipientId"), "name": r.get("name"), "email": r.get("email")}
            for r in recipients.get("signers", [])
            if isinstance(r, dict) and r.get("status") in ("sent", "delivered", "created")
        ]
        if not pending:
            return f"Envelope {envelope_id}: no signer is pending, nothing to resend."
        _, err = await _call(
            "put",
            f"envelopes/{eid}/recipients",
            f"resend envelope {envelope_id}",
            params={"resend_envelope": "true"},
            body={"signers": pending},
        )
        if err:
            return err
        return f"Envelope {envelope_id} resent to {', '.join(str(p['email']) for p in pending)}."

    # ── Templates ────────────────────────────────────────────────────────

    async def list_docusign_templates(search_text: str = "", count: int = 50) -> str:
        """List the account's templates with their roles."""
        params: dict[str, Any] = {
            "count": max(1, min(int(count or 50), 100)),
            "include": "recipients",
        }
        if (search_text or "").strip():
            params["search_text"] = search_text.strip()[:200]
        data, err = await _call("get", "templates", "list templates", params=params, read=True)
        if err:
            return err
        templates = [t for t in data.get("envelopeTemplates", []) if isinstance(t, dict)]
        if not templates:
            return "No template matches."
        lines = [f"{len(templates)} template(s):"]
        for t in templates:
            roles = ", ".join(
                str(s.get("roleName", "?"))
                for s in (t.get("recipients") or {}).get("signers", [])
                if isinstance(s, dict)
            )
            lines.append(
                f"- {t.get('name', '?')} (template id: {t.get('templateId', '?')})"
                + (f" roles: {roles}" if roles else "")
                + (f" - {str(t.get('description'))[:120]}" if t.get("description") else "")
            )
        return "\n".join(lines)

    async def create_docusign_envelope_from_template(
        template_id: str,
        template_roles: list[dict],
        email_subject: str = "",
        email_message: str = "",
        status: str = "sent",
    ) -> str:
        """Create (and send) an envelope from a template, filling its roles."""
        roles = []
        for role in template_roles or []:
            if not isinstance(role, dict):
                continue
            name = str(role.get("name", "")).strip()
            email = str(role.get("email", "")).strip()
            role_name = str(role.get("role_name", "")).strip()
            if not (name and email and role_name):
                return "Error: each template role is {role_name, name, email}."
            roles.append({"roleName": role_name, "name": name, "email": email})
        if not roles:
            return "Error: template_roles must fill at least one role."
        wanted = (status or "sent").strip().lower()
        if wanted not in ("created", "sent"):
            return "Error: status must be 'created' (a draft) or 'sent'."
        body: dict[str, Any] = {
            "templateId": template_id.strip(),
            "templateRoles": roles,
            "status": wanted,
        }
        if (email_subject or "").strip():
            body["emailSubject"] = email_subject.strip()[:100]
        if (email_message or "").strip():
            body["emailBlurb"] = email_message.strip()[:10000]
        data, err = await _call(
            "post", "envelopes", f"create an envelope from template {template_id}", body=body
        )
        if err:
            return err
        envelope_id = str(data.get("envelopeId", ""))
        return (
            f"Envelope {envelope_id} {data.get('status', wanted)} from template {template_id} "
            f"to {', '.join(r['email'] for r in roles)}.\nWeb link: {_web_link(environment, envelope_id)}"
        )

    for name, fn in (
        ("create_docusign_envelope", create_docusign_envelope),
        ("send_docusign_envelope", send_docusign_envelope),
        ("get_docusign_envelope", get_docusign_envelope),
        ("list_docusign_envelopes", list_docusign_envelopes),
        ("download_docusign_document", download_docusign_document),
        ("void_docusign_envelope", void_docusign_envelope),
        ("resend_docusign_envelope", resend_docusign_envelope),
        ("list_docusign_templates", list_docusign_templates),
        ("create_docusign_envelope_from_template", create_docusign_envelope_from_template),
    ):
        executor.register(name, fn)

    person = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
        "required": ["name", "email"],
    }
    signer = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "email": {"type": "string"},
            "routing_order": {
                "type": "integer",
                "description": "Signing order (1 first); default: the position in the list",
            },
            "role": {"type": "string"},
        },
        "required": ["name", "email"],
    }
    anchor_tab = {
        "type": "object",
        "properties": {
            "anchor_string": {
                "type": "string",
                "description": "Text of the document the field is anchored to, e.g. '/sig1/'",
            },
            "type": {"type": "string", "enum": sorted(_TAB_KEYS), "default": "sign_here"},
            "x_offset": {"type": "integer", "default": 0},
            "y_offset": {"type": "integer", "default": 0},
            "page": {"type": "integer"},
        },
        "required": ["anchor_string"],
    }
    envelope_id = {"type": "string", "description": "Envelope id (a GUID)"}
    status = {
        "type": "string",
        "enum": ["created", "sent"],
        "default": "sent",
        "description": "sent (default) sends it; created keeps a draft to send with send_docusign_envelope",
    }
    return [
        ToolDefinition(
            name="create_docusign_envelope",
            description=(
                "Send one to ten PDF or Word files of the conversation (their file_ids, forwarded byte for "
                "byte) to named signers for e-signature as one DocuSign envelope, with an email subject and "
                "message, optional carbon copies and optional anchor tabs (a sign_here, date_signed, full_name, "
                "title or text field placed on an anchor string of the document); without anchor tabs the "
                "signers place their signature themselves. Returns the envelope id, its status, the DocuSign "
                "web link and, when embedded is true with a return_url, a signing URL per signer."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": _MAX_DOCUMENTS,
                    },
                    "email_subject": {"type": "string"},
                    "email_message": {"type": "string"},
                    "signers": {"type": "array", "items": signer, "minItems": 1},
                    "carbon_copies": {"type": "array", "items": person},
                    "anchor_tabs": {"type": "array", "items": anchor_tab},
                    "status": status,
                    "embedded": {"type": "boolean", "default": False},
                    "return_url": {
                        "type": "string",
                        "description": "Where DocuSign sends an embedded signer afterwards",
                    },
                },
                "required": ["file_ids", "email_subject", "signers"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="send_docusign_envelope",
            description=(
                "Send a DocuSign envelope created as a draft (status 'created'): the signers receive "
                "the signing email."
            ),
            input_schema={
                "type": "object",
                "properties": {"envelope_id": envelope_id},
                "required": ["envelope_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_docusign_envelope",
            description=(
                "Read a DocuSign envelope: status (created, sent, delivered, completed, declined, voided), its "
                "created, sent and completed times, every recipient with their status and times, and its "
                "documents with their ids (for download_docusign_document)."
            ),
            input_schema={
                "type": "object",
                "properties": {"envelope_id": envelope_id},
                "required": ["envelope_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_docusign_envelopes",
            description=(
                "List DocuSign envelopes changed since from_date (ISO 8601; default the last 30 days), newest "
                "first, optionally until to_date, with a status filter (sent, delivered, completed, declined, "
                "voided, created) and a search text over subjects and recipients."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_date": {"type": "string"},
                    "to_date": {"type": "string"},
                    "status": {"type": "string"},
                    "search_text": {"type": "string"},
                    "count": {"type": "integer", "default": 20, "maximum": 100},
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="download_docusign_document",
            workspace_ingest=True,
            description=(
                "Download a document of a DocuSign envelope as PDF into the conversation, byte for byte: "
                "document_id 'combined' (default) merges every document, a document id from "
                "get_docusign_envelope takes one; certificate adds the certificate of completion. Gives a "
                "download card, a file_id to forward to any tool that takes a file and the [[FILE:...]] "
                "marker to include verbatim."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "envelope_id": envelope_id,
                    "document_id": {"type": "string", "default": "combined"},
                    "certificate": {"type": "boolean", "default": False},
                },
                "required": ["envelope_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="void_docusign_envelope",
            description="Void a sent DocuSign envelope with a reason; the recipients can no longer sign it.",
            input_schema={
                "type": "object",
                "properties": {"envelope_id": envelope_id, "reason": {"type": "string"}},
                "required": ["envelope_id", "reason"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="resend_docusign_envelope",
            description=(
                "Resend the DocuSign signing notification to the signers of an envelope who have not "
                "completed it."
            ),
            input_schema={
                "type": "object",
                "properties": {"envelope_id": envelope_id},
                "required": ["envelope_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_docusign_templates",
            description=(
                "List the DocuSign templates of the account with their ids, roles and descriptions, "
                "optionally filtered by a search text."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search_text": {"type": "string"},
                    "count": {"type": "integer", "default": 50, "maximum": 100},
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_docusign_envelope_from_template",
            description=(
                "Create and send (or keep as a draft) a DocuSign envelope from a template, filling each "
                "template role with a signer {role_name, name, email}; the template's documents and fields "
                "are used."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "template_id": {"type": "string"},
                    "template_roles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role_name": {"type": "string"},
                                "name": {"type": "string"},
                                "email": {"type": "string"},
                            },
                            "required": ["role_name", "name", "email"],
                        },
                        "minItems": 1,
                    },
                    "email_subject": {"type": "string"},
                    "email_message": {"type": "string"},
                    "status": status,
                },
                "required": ["template_id", "template_roles"],
            },
            source=_SOURCE,
        ),
    ]

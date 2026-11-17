# SPDX-License-Identifier: Apache-2.0

"""Letsignit integration - the email signatures of a company.

Letsignit (https://letsignit.com) composes every employee's email signature:
a business card from their directory attributes, the campaign banner of the
moment, a variant for replies. Its add-in signs what a person composes in
Outlook; an email an agent sends through Microsoft Graph is never composed in
Outlook, so the platform reads the signature from Letsignit's public API
instead and inserts it itself:

- the mailbox of an agent account whose signature source is a Letsignit
  integration, for every reply it sends;
- the Outlook mail tools of an agent this integration is bound to, for the
  drafts and the emails they write.

Both read it through ``services/email_signatures`` (the reader registered at
the end of this module); the tools below expose the rest of the public API:
read the signature of a person, read / create / update the variables of /
delete a Letsignit user.

The key reaches every employee, not only the mailbox the agent signs for, and
what a write changes lands in every email that person sends afterwards: the
three user writes declare ``always_ask``, so each call waits for a
person whatever the approval settings say, unless an administrator
suspended approvals for the whole platform. The two reads stay exempt like
the other directory reads (``hitl_whitelist_platform.HAND_GATED``).

Authentication is a single **API key** with the ``api`` scope (Letsignit >
Settings > API keys, scope "Public API"), sent as ``Authorization: Bearer``.
Every call targets ``https://api.letsignit.com/public/api/v1``.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.connectivity import ProbeContext, ProbeResult, register_connectivity
from app.integrations.http import bearer_auth_headers, safe_request
from app.services.email_signatures import EmailSignature, SignatureUnavailableError, register_signature_source
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

PROVIDER = "letsignit"
_PROVIDER = "Letsignit"
_SOURCE = "integration:letsignit"

LETSIGNIT_API = "https://api.letsignit.com/public/api/v1"

#: The directory attributes a Letsignit user carries (the API's ``Variables``).
USER_VARIABLES: tuple[str, ...] = (
    "full_name",
    "given_name",
    "family_name",
    "job_title",
    "department",
    "phone_primary",
    "phone_work",
    "phone_mobile",
    "fax",
    "address",
    "city",
    "postal_code",
    "state",
    "country",
    "address_work",
    "company_name",
    "website",
)
_EMAIL_RE = re.compile(r"[^@\s/?#]+@[^@\s/?#]+\.[^@\s/?#]+")
_MAX_HTML_CHARS = 20_000
_MAX_VALUE_CHARS = 1_000


def build_letsignit_headers(api_key: str) -> dict[str, str]:
    """``Authorization: Bearer`` + ``Accept: application/json``; raises on a blank key (shared with the probe)."""
    return bearer_auth_headers(api_key, extra={"Accept": "application/json"})


def _email_path(email: Any) -> str | None:
    """*email* as a path segment, or ``None`` when it is not an email address."""
    value = str(email or "").strip()
    if not _EMAIL_RE.fullmatch(value):
        return None
    return quote(value, safe="@")


def _text(html: str) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text("\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _images(html: str) -> int:
    return len(BeautifulSoup(html or "", "html.parser").find_all("img"))


def _variables(raw: Any, *, allow_empty: bool) -> tuple[dict[str, Any] | None, str | None]:
    """The ``variables`` payload, or an error naming what is wrong."""
    if raw is None or raw == {}:
        return ({}, None) if allow_empty else (None, "Error: give at least one variable to set.")
    if not isinstance(raw, dict):
        return None, "Error: 'variables' must be an object of variable -> value."
    out: dict[str, Any] = {}
    unknown = []
    for key, value in raw.items():
        if key == "custom":
            if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                return None, "Error: 'custom' must be an object of custom variable name -> text."
            out["custom"] = {k: v[:_MAX_VALUE_CHARS] for k, v in value.items()}
        elif key in USER_VARIABLES:
            if value is not None and not isinstance(value, str):
                return None, f"Error: '{key}' must be text (or null to clear it)."
            out[key] = value[:_MAX_VALUE_CHARS] if isinstance(value, str) else None
        else:
            unknown.append(str(key))
    if unknown:
        return None, (
            f"Error: unknown variable(s) {', '.join(sorted(unknown))}. "
            f"Letsignit variables: {', '.join(USER_VARIABLES)}, and 'custom' for the custom ones."
        )
    return out, None


def _format_user(user: dict) -> str:
    lines = [
        f"Letsignit user {user.get('email') or '?'} (id {user.get('id') or '?'})",
        f"Type: {user.get('type') or '?'}" + (f" | Subsidiary: {user['subsidiary']}" if user.get("subsidiary") else ""),
    ]
    if user.get("roles"):
        lines.append(f"Roles: {', '.join(str(r) for r in user['roles'])}")
    variables = user.get("variables") or {}
    filled = [f"  {key}: {variables[key]}" for key in (*USER_VARIABLES, "domain_name") if variables.get(key)]
    if filled:
        lines.append("Variables:")
        lines.extend(filled)
    custom = variables.get("custom") or {}
    if custom:
        lines.append("Custom variables:")
        lines.extend(f"  {key}: {value}" for key, value in sorted(custom.items()))
    if not filled and not custom:
        lines.append("No variable set.")
    return "\n".join(lines)


async def _get_signature(client: Any, headers: dict[str, str], email_path: str) -> tuple[Any, str | None]:
    return await safe_request(
        client,
        "get",
        f"{LETSIGNIT_API}/signature/{email_path}",
        _PROVIDER,
        "read the signature",
        headers=headers,
        expected_statuses={401, 403, 404},
    )


def _signature_of(payload: Any) -> EmailSignature | None:
    if not isinstance(payload, dict):
        return None
    html = payload.get("content") if isinstance(payload.get("content"), str) else ""
    reply = payload.get("reply") if isinstance(payload.get("reply"), str) else ""
    if not (html.strip() or reply.strip()):
        return None
    return EmailSignature(html=html or reply, reply_html=reply, name=str(payload.get("name") or ""))


_REFUSED = {
    401: "Letsignit refused the API key: create a new one in Letsignit > Settings > API keys.",
    403: "The Letsignit API key lacks the Public API ('api') scope: edit the key in Letsignit > Settings > API keys.",
}


# ---------------------------------------------------------------------------
# The signature reader (services/email_signatures)
# ---------------------------------------------------------------------------


async def read_letsignit_signature(api_key: str, address: str) -> EmailSignature | None:
    """The signature Letsignit composes for *address* now; ``None`` when it has none for it."""
    email_path = _email_path(address)
    if email_path is None:
        raise SignatureUnavailableError(f"'{address}' is not an email address Letsignit can look up.")
    async with egress_client(timeout=10) as client:
        resp, err = await _get_signature(client, build_letsignit_headers(api_key), email_path)
    if resp is None:
        raise SignatureUnavailableError(f"Letsignit could not be reached: {err}")
    if resp.status_code == 404:
        return None
    if resp.status_code in _REFUSED:
        raise SignatureUnavailableError(_REFUSED[resp.status_code])
    if err:
        raise SignatureUnavailableError(f"Letsignit answered HTTP {resp.status_code}.")
    try:
        return _signature_of(resp.json())
    except ValueError:
        raise SignatureUnavailableError("Letsignit answered with something that is not JSON.") from None


register_signature_source(PROVIDER, read_letsignit_signature)


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class LetsignitIntegration(BaseIntegration):
    """Letsignit email signature management."""

    provider_name = PROVIDER
    display_name = "Letsignit"
    icon = "PenLine"
    description = (
        "Connect Letsignit so what agents send carries each person's current email "
        "signature and banner, and manage Letsignit users and their signature variables."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        if not (token or "").strip():
            return False
        async with egress_client(timeout=15) as client:
            resp, _err = await _get_signature(client, build_letsignit_headers(token), "connection-check@example.com")
        return resp is not None and resp.status_code in (200, 404)

    async def get_user_info(self, token: str) -> dict:
        return {}


async def check_connectivity(ctx: ProbeContext) -> ProbeResult:
    """Read the signature of the integration's owner: a 404 still proves the key works."""
    from app.models.user import User

    key = (ctx.token or "").strip()
    if not key:
        return ProbeResult.failed("Missing API key.")
    owner = None
    owner_id = getattr(ctx.integration, "user_id", None)
    if ctx.db is not None and owner_id is not None:
        owner = (await ctx.db.execute(select(User.email).where(User.id == owner_id))).scalar_one_or_none()
    owner = owner if _email_path(owner) else None
    async with egress_client(timeout=15) as client:
        resp, err = await _get_signature(
            client, build_letsignit_headers(key), _email_path(owner) or "connection-check@example.com"
        )
    if resp is None:
        return ProbeResult.failed(err or "Letsignit could not be reached.")
    if resp.status_code in _REFUSED:
        return ProbeResult.failed(_REFUSED[resp.status_code])
    if owner is None:
        if resp.status_code in (200, 404):
            return ProbeResult.ok("Connected: Letsignit accepted the API key.")
        return ProbeResult.failed(err or f"Letsignit answered HTTP {resp.status_code}.")
    if resp.status_code == 404:
        return ProbeResult.ok(f"Connected. Letsignit holds no signature for {owner}.")
    if err:
        return ProbeResult.failed(err)
    try:
        signature = _signature_of(resp.json())
    except ValueError:
        return ProbeResult.failed("Letsignit answered with something that is not JSON.")
    name = f" '{signature.name}'" if signature and signature.name else ""
    return ProbeResult.ok(f"Connected. Letsignit composes the signature{name} for {owner}.")


register_connectivity(PROVIDER, check_connectivity)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def build_tools(token: str, executor: CallableToolExecutor, **_kwargs: Any) -> list[ToolDefinition]:
    api_key = (token or "").strip()

    async def _call(method: str, path: str, action: str, **kwargs: Any) -> tuple[Any, str | None]:
        async with egress_client(timeout=20) as client:
            return await safe_request(
                client,
                method,
                f"{LETSIGNIT_API}{path}",
                _PROVIDER,
                action,
                headers=build_letsignit_headers(api_key),
                expected_statuses={404, 409},
                **kwargs,
            )

    def _bad_email(email: Any) -> str:
        return f"Error: '{email}' is not an email address."

    async def get_letsignit_signature(email: str, format: str = "text") -> str:
        """The signature Letsignit composes for a person now."""
        email_path = _email_path(email)
        if email_path is None:
            return _bad_email(email)
        resp, err = await _call("get", f"/signature/{email_path}", "read the signature")
        if resp is not None and resp.status_code == 404:
            return f"Letsignit holds no signature for {email}."
        if err:
            return err
        signature = _signature_of(resp.json())
        if signature is None:
            return f"Letsignit holds no signature for {email}."
        lines = [f"Signature '{signature.name or 'unnamed'}' of {email}"]
        if format == "html":
            lines.append("New emails:")
            lines.append(signature.html[:_MAX_HTML_CHARS])
            if signature.reply_html:
                lines.append("Replies:")
                lines.append(signature.reply_html[:_MAX_HTML_CHARS])
            if max(len(signature.html), len(signature.reply_html)) > _MAX_HTML_CHARS:
                lines.append(f"(cut at {_MAX_HTML_CHARS} characters)")
        else:
            lines.append(f"New emails ({_images(signature.html)} image(s)):")
            lines.append(_text(signature.html) or "(images only)")
            if signature.reply_html:
                lines.append(f"Replies ({_images(signature.reply_html)} image(s)):")
                lines.append(_text(signature.reply_html) or "(images only)")
            else:
                lines.append("Replies use the same signature.")
        return "\n".join(lines)

    async def get_letsignit_user(email: str) -> str:
        """A Letsignit user and the variables its signature is composed from."""
        email_path = _email_path(email)
        if email_path is None:
            return _bad_email(email)
        resp, err = await _call("get", f"/users/{email_path}", "read the user")
        if resp is not None and resp.status_code == 404:
            return f"No Letsignit user has the email {email}."
        if err:
            return err
        return _format_user(resp.json() or {})

    async def create_letsignit_user(email: str, variables: dict[str, Any] | None = None) -> str:
        """Add a person to Letsignit, with the variables their signature is composed from."""
        email_path = _email_path(email)
        if email_path is None:
            return _bad_email(email)
        payload, problem = _variables(variables, allow_empty=True)
        if problem:
            return problem
        resp, err = await _call(
            "post", "/users", "create the user", json={"email": str(email).strip(), "variables": payload or {"custom": {}}}
        )
        if resp is not None and resp.status_code == 409:
            return f"A Letsignit user with the email {email} already exists: update its variables instead."
        if err:
            return err
        return f"Created the Letsignit user {email} (id {(resp.json() or {}).get('id') or '?'})."

    async def update_letsignit_user_variables(email: str, variables: dict[str, Any]) -> str:
        """Set variables of a Letsignit user: their signature shows the new values."""
        email_path = _email_path(email)
        if email_path is None:
            return _bad_email(email)
        payload, problem = _variables(variables, allow_empty=False)
        if problem:
            return problem
        resp, err = await _call(
            "put", f"/users/{email_path}/variables", "update the user's variables", json={"variables": payload}
        )
        if resp is not None and resp.status_code == 404:
            return f"No Letsignit user has the email {email}."
        if err:
            return err
        return f"Updated {', '.join(sorted(payload or {}))} of the Letsignit user {email}."

    async def delete_letsignit_user(email: str) -> str:
        """Remove a person from Letsignit."""
        email_path = _email_path(email)
        if email_path is None:
            return _bad_email(email)
        resp, err = await _call("delete", f"/users/{email_path}", "delete the user")
        if resp is not None and resp.status_code == 404:
            return f"No Letsignit user has the email {email}."
        if err:
            return err
        return f"Deleted the Letsignit user {email}."

    executor.register("get_letsignit_signature", get_letsignit_signature)
    executor.register("get_letsignit_user", get_letsignit_user)
    executor.register("create_letsignit_user", create_letsignit_user)
    executor.register("update_letsignit_user_variables", update_letsignit_user_variables)
    executor.register("delete_letsignit_user", delete_letsignit_user)

    email_param = {"type": "string", "description": "The person's email address, as known to Letsignit."}
    variables_param = {
        "type": "object",
        "description": (
            "Signature variables to set, by name: "
            + ", ".join(USER_VARIABLES)
            + ". Text values; null clears one. 'custom' is an object of the company's custom variables (name -> text)."
        ),
        "properties": {
            **{name: {"type": ["string", "null"]} for name in USER_VARIABLES},
            "custom": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "additionalProperties": False,
    }
    return [
        ToolDefinition(
            name="get_letsignit_signature",
            description=(
                "Read the email signature Letsignit composes for a person now (their highest-priority "
                "signature with the current banner), and its variant for replies when there is one. "
                "format='text' (default) gives what it says and how many images it carries; "
                "format='html' gives the HTML. With this integration bound next to a Microsoft one, the "
                "Outlook mail tools insert the signature themselves: never paste it into their body. Use "
                "it to check what a person's signature says, or to sign an email sent through another service."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "email": email_param,
                    "format": {"type": "string", "enum": ["text", "html"], "default": "text"},
                },
                "required": ["email"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_letsignit_user",
            description=(
                "Read a Letsignit user by email: type, roles, subsidiary, and the variables their "
                "signature is composed from (name, job title, phones, address, custom variables). "
                "Use before update_letsignit_user_variables to see the current values."
            ),
            input_schema={"type": "object", "properties": {"email": email_param}, "required": ["email"]},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_letsignit_user",
            description=(
                "Add a person to Letsignit (a user the directory sync did not bring), with the variables "
                "their signature is composed from. Refused when a user with that email exists: use "
                "update_letsignit_user_variables then."
            ),
            input_schema={
                "type": "object",
                "properties": {"email": email_param, "variables": variables_param},
                "required": ["email"],
            },
            source=_SOURCE,
            priority=1,
            always_ask=True,
        ),
        ToolDefinition(
            name="update_letsignit_user_variables",
            description=(
                "Set signature variables of a Letsignit user (job title, phone, address, custom "
                "variables...). Only the variables given change; their signature shows the new values "
                "from then on. A directory sync may later overwrite the values it manages."
            ),
            input_schema={
                "type": "object",
                "properties": {"email": email_param, "variables": variables_param},
                "required": ["email", "variables"],
            },
            source=_SOURCE,
            priority=1,
            always_ask=True,
        ),
        ToolDefinition(
            name="delete_letsignit_user",
            description=(
                "Delete a Letsignit user: they no longer get a signature from Letsignit. Permanent, "
                "cannot be undone (create_letsignit_user adds them again, without their history)."
            ),
            input_schema={"type": "object", "properties": {"email": email_param}, "required": ["email"]},
            source=_SOURCE,
            priority=2,
            always_ask=True,
        ),
    ]

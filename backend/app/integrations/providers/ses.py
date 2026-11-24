# SPDX-License-Identifier: Apache-2.0

"""Amazon SES (Simple Email Service) integration – email distribution at scale.

Full-fledged Amazon SES v2 integration designed as a best-in-class building
block for agents that send and manage email. It covers the complete email
lifecycle: sending (simple, templated, and bulk), sender identity verification,
reusable templates, configuration sets, the account-level suppression list,
contact lists / contacts (with topic subscriptions for unsubscribe handling),
and account/quota visibility.

Tool categories (31 tools):
  - Sending: send a simple email (signed when the integration's Email
    signature setting resolves a signature integration), send a
    templated email, send a bulk templated email to many recipients, read the
    signature a From address is sent with
  - Account: get sending quota, send rate, reputation, and production access
  - Identities: list / get / create (verify) / delete email & domain identities
  - Templates: list / get / create / update / delete email templates
  - Configuration sets: list / get / create / delete
  - Suppression list: list / get / add / delete suppressed destinations
  - Contact lists: list / get / create / delete
  - Contacts: list / get / create / update (subscribe/unsubscribe topics) / delete

Authentication is **AWS IAM credentials** (an access key). The integration
stores an Access Key ID + Secret Access Key + region; the compound
``region||access_key_id||secret_access_key||configuration_set`` token is
assembled by ``integration_tools._get_token`` from the dedicated API-key column
(secret) and the encrypted ``extra_config`` (access key ID, region, optional
default configuration set). Every request is signed with AWS Signature Version
4 (service ``ses``) via :mod:`app.integrations.aws_sigv4` over the shared async
``httpx`` path — no synchronous ``boto3`` calls in the event loop.

The SES v2 REST API is reached at ``https://email.{region}.amazonaws.com`` under
the ``/v2/email/`` path prefix.
"""

from __future__ import annotations

import json as _json
import logging
from email.utils import parseaddr
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.aws_sigv4 import (
    build_canonical_querystring,
    encode_path,
    sign_request,
)
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.services.email_signatures import (
    MailSigner,
    append_to_body,
    register_email_sender,
    signed_text,
    text_to_html,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

# SES sends email: each integration has an Email signature setting.
register_email_sender("ses")

_PROVIDER = "Amazon SES"
_SERVICE = "ses"  # SigV4 signing name (the endpoint host is ``email.…``)
_DEFAULT_REGION = "us-east-1"
_DEFAULT_TIMEOUT = 30
_API_PREFIX = "/v2/email"

# Reason codes accepted by the account-level suppression list.
_SUPPRESSION_REASONS = ("BOUNCE", "COMPLAINT")

_AUTH_ERROR = (
    "Error: Amazon SES credentials are missing or incomplete. Ensure the "
    "integration has an AWS Access Key ID, Secret Access Key, and region "
    "configured, and that the IAM principal has the required ses:* permissions."
)


# ---------------------------------------------------------------------------
# Endpoint + credential helpers
# ---------------------------------------------------------------------------


def resolve_host(region: str) -> str:
    """Return the SES v2 endpoint host for *region* (e.g. ``email.us-east-1.amazonaws.com``)."""
    region = (region or _DEFAULT_REGION).strip() or _DEFAULT_REGION
    return f"email.{region}.amazonaws.com"


async def _signed_request(
    *,
    method: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    path: str,
    query: dict[str, Any] | None = None,
    body: Any | None = None,
    action: str,
    timeout: int = _DEFAULT_TIMEOUT,
    expected_statuses: set[int] | None = None,
) -> tuple[httpx.Response | None, str | None]:
    """Sign an SES v2 request with SigV4 and execute it via ``safe_request``.

    Returns the ``(response, error)`` tuple from :func:`safe_request`. The same
    canonical (encoded) path / query / body bytes that are signed are sent on
    the wire so the signature always matches.
    """
    host = resolve_host(region)
    canonical_uri = encode_path(path)
    canonical_qs = build_canonical_querystring(query)
    payload = b"" if body is None else _json.dumps(body).encode("utf-8")

    signed_headers = sign_request(
        method=method,
        service=_SERVICE,
        region=(region or _DEFAULT_REGION).strip() or _DEFAULT_REGION,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        host=host,
        canonical_uri=canonical_uri,
        canonical_querystring=canonical_qs,
        payload=payload,
    )
    headers = {**signed_headers, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    url = f"https://{host}{canonical_uri}"
    if canonical_qs:
        url = f"{url}?{canonical_qs}"

    request_kwargs: dict[str, Any] = {"headers": headers}
    if body is not None:
        request_kwargs["content"] = payload

    async with egress_client(timeout=timeout) as client:
        return await safe_request(
            client,
            method.lower(),
            url,
            _PROVIDER,
            action,
            expected_statuses=expected_statuses,
            **request_kwargs,
        )


async def validate_connection(
    region: str, access_key_id: str, secret_access_key: str
) -> tuple[bool, str]:
    """Verify SES credentials by calling GetAccount.

    Returns ``(ok, message)``. A 200 confirms the access key, secret, and region
    are valid and the IAM principal can read account details. Used by the
    integration connectivity test.
    """
    if not access_key_id or not secret_access_key:
        return False, "Missing AWS Access Key ID or Secret Access Key."
    resp, err = await _signed_request(
        method="GET",
        region=region,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        path=f"{_API_PREFIX}/account",
        action="verify account",
        timeout=15,
    )
    if err or resp is None:
        return False, (
            err
            or "Authentication failed — check the access key, secret, and region."
        )
    try:
        data = resp.json()
    except Exception:
        data = {}
    sending = data.get("SendingEnabled")
    production = data.get("ProductionAccessEnabled")
    mode = "production" if production else "sandbox"
    return True, (
        f"Amazon SES is connected (region {region or _DEFAULT_REGION}, {mode} "
        f"mode, sending {'enabled' if sending else 'disabled'})."
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 300) -> str:
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _split_addresses(value: str | list[str] | None) -> list[str]:
    """Normalise a comma/semicolon-separated string (or list) of addresses."""
    if not value:
        return []
    items = value if isinstance(value, list) else str(value).replace(";", ",").split(",")
    # Coerce each item to str before stripping: LLM tool calls sometimes pass a
    # list containing non-strings, and a bare ``a.strip()`` would raise
    # AttributeError and fail the whole send.
    return [s for a in items if a and (s := str(a).strip())]


def _signature_block(markup: str) -> str:
    """A signature's markup as the block an SES email ends with."""
    return f'<div class="signature">{markup}</div>' if markup else ""


def _sender_address(from_address: str) -> str:
    """The address of a From value such as ``'Name <sender@example.com>'``."""
    return parseaddr(from_address or "")[1].strip()


def _as_template_data(value: Any) -> str:
    """Coerce template data to the JSON *string* the SES API expects."""
    if value is None or value == "":
        return "{}"
    if isinstance(value, str):
        return value
    try:
        return _json.dumps(value)
    except (TypeError, ValueError):
        return "{}"


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Amazon SES tools.

    ``token`` is the compound ``region||access_key_id||secret_access_key||
    configuration_set`` assembled by ``integration_tools._get_token`` from the
    integration's stored AWS credentials and region.
    """
    region, access_key_id, secret_access_key, default_config_set = parse_compound_token(
        token, 4
    )
    region = (region or _DEFAULT_REGION).strip() or _DEFAULT_REGION
    # The integration's Email signature setting as resolved for this run:
    # the signature of the From address, read from its signature integration.
    signer = MailSigner(_kwargs.get("signature_fn"), _kwargs.get("signature_plan"))

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[dict | None, str | None]:
        """Authenticated SES request helper returning ``(json, error)``."""
        if not access_key_id or not secret_access_key:
            return None, _AUTH_ERROR
        resp, err = await _signed_request(
            method=method,
            region=region,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            path=path,
            query=query,
            body=body,
            action=action,
            timeout=timeout,
        )
        if err:
            return None, err
        try:
            return resp.json(), None
        except Exception:
            # 200 with an empty body (PutSuppressedDestination, Delete*, …).
            return {}, None

    def _config_set(override: str = "") -> str:
        """Resolve the effective configuration set (explicit override > default)."""
        return (override or "").strip() or (default_config_set or "").strip()

    # ══════════════════════════════════════════════════════════════════════
    # 0. SENDING
    # ══════════════════════════════════════════════════════════════════════

    async def send_ses_email(
        from_address: str,
        to: str,
        subject: str,
        text_body: str = "",
        html_body: str = "",
        cc: str = "",
        bcc: str = "",
        reply_to: str = "",
        configuration_set: str = "",
        contact_list_name: str = "",
        topic_name: str = "",
    ) -> str:
        """Send a simple email (subject + text and/or HTML body), signed when the integration signs."""
        to_list = _split_addresses(to)
        if not to_list and not _split_addresses(cc) and not _split_addresses(bcc):
            return "Error: at least one recipient (to, cc, or bcc) is required."
        if not text_body and not html_body:
            return "Error: provide a text_body and/or an html_body."

        signature = await signer.markup(_sender_address(from_address), reply=False)
        if signature:
            # The HTML part ends with the signature (a text-only email gains one),
            # the text part with its text rendering.
            html_body = append_to_body(html_body or text_to_html(text_body), _signature_block(signature))
            if text_body:
                text_body = signed_text(text_body, signature)

        body_block: dict[str, Any] = {}
        if text_body:
            body_block["Text"] = {"Data": text_body, "Charset": "UTF-8"}
        if html_body:
            body_block["Html"] = {"Data": html_body, "Charset": "UTF-8"}

        payload: dict[str, Any] = {
            "FromEmailAddress": from_address,
            "Destination": {
                "ToAddresses": to_list,
                "CcAddresses": _split_addresses(cc),
                "BccAddresses": _split_addresses(bcc),
            },
            "Content": {
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": body_block,
                }
            },
        }
        reply_list = _split_addresses(reply_to)
        if reply_list:
            payload["ReplyToAddresses"] = reply_list
        cfg = _config_set(configuration_set)
        if cfg:
            payload["ConfigurationSetName"] = cfg
        if contact_list_name and topic_name:
            payload["ListManagementOptions"] = {
                "ContactListName": contact_list_name,
                "TopicName": topic_name,
            }

        data, err = await _api(
            "post", f"{_API_PREFIX}/outbound-emails", "send email", body=payload
        )
        if err:
            return err
        return f"Email sent to {', '.join(to_list) or 'recipients'}. MessageId: {data.get('MessageId', '?')}"

    async def send_ses_templated_email(
        from_address: str,
        to: str,
        template_name: str,
        template_data: dict | str = "",
        cc: str = "",
        bcc: str = "",
        reply_to: str = "",
        configuration_set: str = "",
        contact_list_name: str = "",
        topic_name: str = "",
    ) -> str:
        """Send an email rendered from a stored template with personalization data."""
        to_list = _split_addresses(to)
        if not to_list and not _split_addresses(cc) and not _split_addresses(bcc):
            return "Error: at least one recipient (to, cc, or bcc) is required."
        if not template_name:
            return "Error: template_name is required."

        payload: dict[str, Any] = {
            "FromEmailAddress": from_address,
            "Destination": {
                "ToAddresses": to_list,
                "CcAddresses": _split_addresses(cc),
                "BccAddresses": _split_addresses(bcc),
            },
            "Content": {
                "Template": {
                    "TemplateName": template_name,
                    "TemplateData": _as_template_data(template_data),
                }
            },
        }
        reply_list = _split_addresses(reply_to)
        if reply_list:
            payload["ReplyToAddresses"] = reply_list
        cfg = _config_set(configuration_set)
        if cfg:
            payload["ConfigurationSetName"] = cfg
        if contact_list_name and topic_name:
            payload["ListManagementOptions"] = {
                "ContactListName": contact_list_name,
                "TopicName": topic_name,
            }

        data, err = await _api(
            "post", f"{_API_PREFIX}/outbound-emails", "send templated email", body=payload
        )
        if err:
            return err
        return (
            f"Templated email ('{template_name}') sent to "
            f"{', '.join(to_list) or 'recipients'}. MessageId: {data.get('MessageId', '?')}"
        )

    async def send_ses_bulk_email(
        from_address: str,
        template_name: str,
        recipients: list[dict],
        default_template_data: dict | str = "",
        reply_to: str = "",
        configuration_set: str = "",
    ) -> str:
        """Send a templated email to many recipients in one call (SendBulkEmail).

        ``recipients`` is a list of objects, each with a ``to`` address (string,
        comma-separated for multiple) and optional ``replacement_data`` (object)
        that overrides the default template data for that recipient.
        """
        if not template_name:
            return "Error: template_name is required."
        if not recipients:
            return "Error: provide at least one recipient in 'recipients'."

        entries: list[dict[str, Any]] = []
        for r in recipients:
            if not isinstance(r, dict):
                continue
            to_list = _split_addresses(r.get("to") or r.get("email"))
            if not to_list:
                continue
            entry: dict[str, Any] = {"Destination": {"ToAddresses": to_list}}
            replacement = r.get("replacement_data")
            if replacement is not None:
                entry["ReplacementEmailContent"] = {
                    "ReplacementTemplate": {
                        "ReplacementTemplateData": _as_template_data(replacement)
                    }
                }
            entries.append(entry)

        if not entries:
            return "Error: no valid recipients with a 'to' address were provided."

        payload: dict[str, Any] = {
            "FromEmailAddress": from_address,
            "DefaultContent": {
                "Template": {
                    "TemplateName": template_name,
                    "TemplateData": _as_template_data(default_template_data),
                }
            },
            "BulkEmailEntries": entries,
        }
        reply_list = _split_addresses(reply_to)
        if reply_list:
            payload["ReplyToAddresses"] = reply_list
        cfg = _config_set(configuration_set)
        if cfg:
            payload["ConfigurationSetName"] = cfg

        data, err = await _api(
            "post",
            f"{_API_PREFIX}/outbound-bulk-emails",
            "send bulk email",
            body=payload,
        )
        if err:
            return err
        results = data.get("BulkEmailEntryResults") or []
        ok = sum(1 for r in results if (r.get("Status") == "SUCCESS"))
        failed = [
            f"{e['Destination']['ToAddresses'][0]}: {r.get('Status')} ({r.get('Error', '')})"
            for e, r in zip(entries, results, strict=False)
            if r.get("Status") != "SUCCESS"
        ]
        # Report acceptance out of the entries we attempted, not the number of
        # results SES returned: if SES ever returns fewer/no results the
        # denominator stays meaningful (e.g. "0/3", never a misleading "0/0").
        summary = f"Bulk send complete: {ok}/{len(entries)} accepted."
        if failed:
            summary += "\nFailures:\n  " + "\n  ".join(failed[:25])
        return summary

    async def get_ses_email_signature(from_address: str) -> str:
        """The signature send_ses_email adds for *from_address*, and where it comes from."""
        address = _sender_address(from_address)
        if not signer.signs:
            return signer.report(address, "")
        if not address:
            return "Error: from_address must be an email address, e.g. sender@example.com."
        return signer.report(address, await signer.markup(address, reply=False))

    # ══════════════════════════════════════════════════════════════════════
    # 1. ACCOUNT
    # ══════════════════════════════════════════════════════════════════════

    async def get_ses_account() -> str:
        """Get the SES account sending status, quota, send rate, and reputation."""
        data, err = await _api("get", f"{_API_PREFIX}/account", "get account")
        if err:
            return err
        quota = data.get("SendQuota") or {}
        suppression = data.get("SuppressionAttributes") or {}
        return "\n".join(
            [
                f"Region: {region}",
                f"Sending enabled: {data.get('SendingEnabled')}",
                f"Production access: {data.get('ProductionAccessEnabled')} "
                f"(if False, the account is in the sandbox — recipients must be verified)",
                f"Enforcement status: {data.get('EnforcementStatus', 'N/A')}",
                f"Max 24h send: {quota.get('Max24HourSend', 'N/A')} | "
                f"Max send rate: {quota.get('MaxSendRate', 'N/A')}/s | "
                f"Sent last 24h: {quota.get('SentLast24Hours', 'N/A')}",
                f"Suppressed reasons: {', '.join(suppression.get('SuppressedReasons', []) or []) or 'None'}",
                f"Dedicated IP auto-warmup: {data.get('DedicatedIpAutoWarmupEnabled', 'N/A')}",
            ]
        )

    # ══════════════════════════════════════════════════════════════════════
    # 2. EMAIL IDENTITIES (verification)
    # ══════════════════════════════════════════════════════════════════════

    async def list_ses_identities(page_size: int = 50, next_token: str = "") -> str:
        """List verified (and pending) email and domain identities."""
        query: dict[str, Any] = {"PageSize": min(max(page_size, 1), 1000)}
        if next_token:
            query["NextToken"] = next_token
        data, err = await _api(
            "get", f"{_API_PREFIX}/identities", "list identities", query=query
        )
        if err:
            return err
        identities = data.get("EmailIdentities") or []

        def _fmt(i: dict) -> str:
            return (
                f"  {i.get('IdentityName', '?')} [{i.get('IdentityType', '?')}] — "
                f"verification: {i.get('VerificationStatus', '?')} | "
                f"sending: {i.get('SendingEnabled')}"
            )

        out = format_entity_list(identities, _fmt, noun="identity")
        if data.get("NextToken"):
            out += f'\n[More results — pass next_token="{data["NextToken"]}"]'
        return out

    async def get_ses_identity(identity: str) -> str:
        """Get verification status, DKIM, and MAIL FROM details for an identity."""
        data, err = await _api(
            "get",
            f"{_API_PREFIX}/identities/{identity}",
            f"get identity {identity}",
        )
        if err:
            return err
        dkim = data.get("DkimAttributes") or {}
        mail_from = data.get("MailFromAttributes") or {}
        lines = [
            f"Identity: {identity}",
            f"Type: {data.get('IdentityType', '?')}",
            f"Verified for sending: {data.get('VerifiedForSendingStatus')}",
            f"Feedback forwarding: {data.get('FeedbackForwardingStatus')}",
            f"DKIM signing: {dkim.get('SigningEnabled')} | status: {dkim.get('Status', 'N/A')}",
        ]
        tokens = dkim.get("Tokens") or []
        if tokens:
            lines.append(
                "DKIM CNAME tokens (add as TOKEN._domainkey CNAME records):"
            )
            lines.extend(f"  - {t}" for t in tokens)
        if mail_from.get("MailFromDomain"):
            lines.append(
                f"MAIL FROM domain: {mail_from.get('MailFromDomain')} "
                f"(status: {mail_from.get('MailFromDomainStatus', 'N/A')})"
            )
        return "\n".join(lines)

    async def create_ses_identity(identity: str) -> str:
        """Create/verify an email address or domain identity.

        For an email address, SES sends a verification link. For a domain, SES
        returns DKIM CNAME tokens to publish in DNS.
        """
        if not identity:
            return "Error: an email address or domain is required."
        data, err = await _api(
            "post",
            f"{_API_PREFIX}/identities",
            f"create identity {identity}",
            body={"EmailIdentity": identity},
        )
        if err:
            return err
        dkim = data.get("DkimAttributes") or {}
        tokens = dkim.get("Tokens") or []
        lines = [
            f"Identity '{identity}' created (type: {data.get('IdentityType', '?')}).",
            f"Verified for sending: {data.get('VerifiedForSendingStatus')}",
        ]
        if "@" in identity:
            lines.append(
                "A verification email was sent — the recipient must click the link."
            )
        if tokens:
            lines.append("Publish these DKIM CNAME records to verify the domain:")
            lines.extend(
                f"  {t}._domainkey.{identity} CNAME {t}.dkim.amazonses.com"
                for t in tokens
            )
        return "\n".join(lines)

    async def delete_ses_identity(identity: str) -> str:
        """Delete an email or domain identity."""
        _data, err = await _api(
            "delete",
            f"{_API_PREFIX}/identities/{identity}",
            f"delete identity {identity}",
        )
        if err:
            return err
        return f"Identity '{identity}' deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 3. EMAIL TEMPLATES
    # ══════════════════════════════════════════════════════════════════════

    async def list_ses_templates(page_size: int = 50, next_token: str = "") -> str:
        """List email templates."""
        query: dict[str, Any] = {"PageSize": min(max(page_size, 1), 100)}
        if next_token:
            query["NextToken"] = next_token
        data, err = await _api(
            "get", f"{_API_PREFIX}/templates", "list templates", query=query
        )
        if err:
            return err
        templates = data.get("TemplatesMetadata") or []

        def _fmt(t: dict) -> str:
            return f"  {t.get('TemplateName', '?')} (created {str(t.get('CreatedTimestamp', ''))[:19]})"

        out = format_entity_list(templates, _fmt, noun="template")
        if data.get("NextToken"):
            out += f'\n[More results — pass next_token="{data["NextToken"]}"]'
        return out

    async def get_ses_template(template_name: str) -> str:
        """Get the subject and body content of an email template."""
        data, err = await _api(
            "get",
            f"{_API_PREFIX}/templates/{template_name}",
            f"get template {template_name}",
        )
        if err:
            return err
        content = data.get("TemplateContent") or {}
        return "\n".join(
            [
                f"Template: {data.get('TemplateName', template_name)}",
                f"Subject: {content.get('Subject', '(none)')}",
                f"\nText body:\n{_trunc(content.get('Text'), 1000) or '(none)'}",
                f"\nHTML body:\n{_trunc(content.get('Html'), 1500) or '(none)'}",
            ]
        )

    async def create_ses_template(
        template_name: str, subject: str, text_body: str = "", html_body: str = ""
    ) -> str:
        """Create an email template. Use {{placeholders}} for personalization."""
        if not template_name:
            return "Error: template_name is required."
        if not subject:
            return "Error: a subject is required."
        if not text_body and not html_body:
            return "Error: provide a text_body and/or an html_body."
        content: dict[str, str] = {"Subject": subject}
        if text_body:
            content["Text"] = text_body
        if html_body:
            content["Html"] = html_body
        _data, err = await _api(
            "post",
            f"{_API_PREFIX}/templates",
            f"create template {template_name}",
            body={"TemplateName": template_name, "TemplateContent": content},
        )
        if err:
            return err
        return f"Template '{template_name}' created."

    async def update_ses_template(
        template_name: str, subject: str, text_body: str = "", html_body: str = ""
    ) -> str:
        """Update an existing email template's subject and body."""
        if not template_name:
            return "Error: template_name is required."
        if not subject:
            return "Error: a subject is required."
        if not text_body and not html_body:
            return "Error: provide a text_body and/or an html_body."
        content: dict[str, str] = {"Subject": subject}
        if text_body:
            content["Text"] = text_body
        if html_body:
            content["Html"] = html_body
        _data, err = await _api(
            "put",
            f"{_API_PREFIX}/templates/{template_name}",
            f"update template {template_name}",
            body={"TemplateContent": content},
        )
        if err:
            return err
        return f"Template '{template_name}' updated."

    async def delete_ses_template(template_name: str) -> str:
        """Delete an email template."""
        _data, err = await _api(
            "delete",
            f"{_API_PREFIX}/templates/{template_name}",
            f"delete template {template_name}",
        )
        if err:
            return err
        return f"Template '{template_name}' deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 4. CONFIGURATION SETS
    # ══════════════════════════════════════════════════════════════════════

    async def list_ses_configuration_sets(
        page_size: int = 50, next_token: str = ""
    ) -> str:
        """List configuration sets (groups of sending rules / event destinations)."""
        query: dict[str, Any] = {"PageSize": min(max(page_size, 1), 1000)}
        if next_token:
            query["NextToken"] = next_token
        data, err = await _api(
            "get",
            f"{_API_PREFIX}/configuration-sets",
            "list configuration sets",
            query=query,
        )
        if err:
            return err
        names = data.get("ConfigurationSets") or []
        if not names:
            return "No configuration sets found."
        out = f"Found {len(names)} configuration set(s):\n" + "\n".join(
            f"  - {n}" for n in names
        )
        if data.get("NextToken"):
            out += f'\n[More results — pass next_token="{data["NextToken"]}"]'
        return out

    async def get_ses_configuration_set(configuration_set_name: str) -> str:
        """Get the details of a configuration set."""
        data, err = await _api(
            "get",
            f"{_API_PREFIX}/configuration-sets/{configuration_set_name}",
            f"get configuration set {configuration_set_name}",
        )
        if err:
            return err
        delivery = data.get("DeliveryOptions") or {}
        sending = data.get("SendingOptions") or {}
        reputation = data.get("ReputationOptions") or {}
        suppression = data.get("SuppressionOptions") or {}
        return "\n".join(
            [
                f"Configuration set: {data.get('ConfigurationSetName', configuration_set_name)}",
                f"Sending enabled: {sending.get('SendingEnabled')}",
                f"TLS policy: {delivery.get('TlsPolicy', 'N/A')}",
                f"Reputation metrics: {reputation.get('ReputationMetricsEnabled', 'N/A')}",
                f"Suppressed reasons: {', '.join(suppression.get('SuppressedReasons', []) or []) or 'None'}",
            ]
        )

    async def create_ses_configuration_set(
        configuration_set_name: str,
        tls_policy: str = "OPTIONAL",
        reputation_metrics: bool = True,
        sending_enabled: bool = True,
    ) -> str:
        """Create a configuration set. tls_policy is REQUIRE or OPTIONAL."""
        body: dict[str, Any] = {
            "ConfigurationSetName": configuration_set_name,
            "DeliveryOptions": {"TlsPolicy": (tls_policy or "OPTIONAL").upper()},
            "ReputationOptions": {"ReputationMetricsEnabled": reputation_metrics},
            "SendingOptions": {"SendingEnabled": sending_enabled},
        }
        _data, err = await _api(
            "post",
            f"{_API_PREFIX}/configuration-sets",
            f"create configuration set {configuration_set_name}",
            body=body,
        )
        if err:
            return err
        return f"Configuration set '{configuration_set_name}' created."

    async def delete_ses_configuration_set(configuration_set_name: str) -> str:
        """Delete a configuration set."""
        _data, err = await _api(
            "delete",
            f"{_API_PREFIX}/configuration-sets/{configuration_set_name}",
            f"delete configuration set {configuration_set_name}",
        )
        if err:
            return err
        return f"Configuration set '{configuration_set_name}' deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 5. SUPPRESSION LIST
    # ══════════════════════════════════════════════════════════════════════

    async def list_ses_suppressed_destinations(
        reason: str = "", page_size: int = 50, next_token: str = ""
    ) -> str:
        """List addresses on the account suppression list (bounces/complaints)."""
        query: dict[str, Any] = {"PageSize": min(max(page_size, 1), 1000)}
        if reason:
            query["Reason"] = reason.upper()
        if next_token:
            query["NextToken"] = next_token
        data, err = await _api(
            "get",
            f"{_API_PREFIX}/suppression/addresses",
            "list suppressed destinations",
            query=query,
        )
        if err:
            return err
        items = data.get("SuppressedDestinationSummaries") or []

        def _fmt(s: dict) -> str:
            return (
                f"  {s.get('EmailAddress', '?')} — reason: {s.get('Reason', '?')} "
                f"(since {str(s.get('LastUpdateTime', ''))[:19]})"
            )

        out = format_entity_list(items, _fmt, noun="suppressed address")
        if data.get("NextToken"):
            out += f'\n[More results — pass next_token="{data["NextToken"]}"]'
        return out

    async def get_ses_suppressed_destination(email_address: str) -> str:
        """Check whether an address is on the suppression list, and why."""
        if not access_key_id or not secret_access_key:
            return _AUTH_ERROR
        # SES returns 404 (NotFoundException) when the address is NOT on the
        # suppression list — a benign "not suppressed" result here. The inner
        # ``_api`` helper doesn't expose ``expected_statuses`` (and would lose
        # the status code), so call ``_signed_request`` directly and branch on
        # ``resp.status_code`` BEFORE the error string, which ``safe_request``
        # still populates for an expected (opted-in) non-2xx status.
        resp, err = await _signed_request(
            method="GET",
            region=region,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            path=f"{_API_PREFIX}/suppression/addresses/{email_address}",
            action=f"get suppressed destination {email_address}",
            expected_statuses={404},
        )
        if resp is not None and resp.status_code == 404:
            return f"{email_address} is not on the suppression list."
        if err:
            return err
        try:
            data = resp.json()
        except Exception:
            data = {}
        dest = data.get("SuppressedDestination") or {}
        return (
            f"{dest.get('EmailAddress', email_address)} is suppressed — "
            f"reason: {dest.get('Reason', '?')} "
            f"(last updated {str(dest.get('LastUpdateTime', ''))[:19]})"
        )

    async def add_ses_suppressed_destination(
        email_address: str, reason: str = "BOUNCE"
    ) -> str:
        """Add an address to the account suppression list. reason: BOUNCE or COMPLAINT."""
        reason = (reason or "BOUNCE").upper()
        if reason not in _SUPPRESSION_REASONS:
            return f"Error: reason must be one of {', '.join(_SUPPRESSION_REASONS)}."
        _data, err = await _api(
            "put",
            f"{_API_PREFIX}/suppression/addresses",
            f"suppress {email_address}",
            body={"EmailAddress": email_address, "Reason": reason},
        )
        if err:
            return err
        return f"{email_address} added to the suppression list (reason: {reason})."

    async def delete_ses_suppressed_destination(email_address: str) -> str:
        """Remove an address from the account suppression list."""
        _data, err = await _api(
            "delete",
            f"{_API_PREFIX}/suppression/addresses/{email_address}",
            f"unsuppress {email_address}",
        )
        if err:
            return err
        return f"{email_address} removed from the suppression list."

    # ══════════════════════════════════════════════════════════════════════
    # 6. CONTACT LISTS
    # ══════════════════════════════════════════════════════════════════════

    async def list_ses_contact_lists() -> str:
        """List contact lists (SES allows only one contact list per AWS account)."""
        data, err = await _api(
            "get", f"{_API_PREFIX}/contact-lists", "list contact lists"
        )
        if err:
            return err
        lists = data.get("ContactLists") or []

        def _fmt(c: dict) -> str:
            return f"  {c.get('ContactListName', '?')} (updated {str(c.get('LastUpdatedTimestamp', ''))[:19]})"

        return format_entity_list(lists, _fmt, noun="contact list")

    async def get_ses_contact_list(contact_list_name: str) -> str:
        """Get a contact list's description and topics."""
        data, err = await _api(
            "get",
            f"{_API_PREFIX}/contact-lists/{contact_list_name}",
            f"get contact list {contact_list_name}",
        )
        if err:
            return err
        topics = data.get("Topics") or []
        lines = [
            f"Contact list: {data.get('ContactListName', contact_list_name)}",
            f"Description: {data.get('Description', '(none)')}",
            f"Topics ({len(topics)}):",
        ]
        for t in topics:
            lines.append(
                f"  - {t.get('TopicName', '?')}: {t.get('DisplayName', '')} "
                f"(default: {t.get('DefaultSubscriptionStatus', '?')})"
            )
        return "\n".join(lines)

    async def create_ses_contact_list(
        contact_list_name: str, description: str = "", topics: list[dict] | None = None
    ) -> str:
        """Create a contact list. topics is a list of {TopicName, DisplayName, DefaultSubscriptionStatus}."""
        body: dict[str, Any] = {"ContactListName": contact_list_name}
        if description:
            body["Description"] = description
        if topics:
            body["Topics"] = topics
        _data, err = await _api(
            "post",
            f"{_API_PREFIX}/contact-lists",
            f"create contact list {contact_list_name}",
            body=body,
        )
        if err:
            return err
        return f"Contact list '{contact_list_name}' created."

    async def delete_ses_contact_list(contact_list_name: str) -> str:
        """Delete a contact list and all its contacts."""
        _data, err = await _api(
            "delete",
            f"{_API_PREFIX}/contact-lists/{contact_list_name}",
            f"delete contact list {contact_list_name}",
        )
        if err:
            return err
        return f"Contact list '{contact_list_name}' deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 7. CONTACTS
    # ══════════════════════════════════════════════════════════════════════

    async def list_ses_contacts(
        contact_list_name: str, page_size: int = 50, next_token: str = ""
    ) -> str:
        """List contacts in a contact list."""
        body: dict[str, Any] = {"PageSize": min(max(page_size, 1), 1000)}
        if next_token:
            body["NextToken"] = next_token
        data, err = await _api(
            "post",
            f"{_API_PREFIX}/contact-lists/{contact_list_name}/contacts/list",
            f"list contacts in {contact_list_name}",
            body=body,
        )
        if err:
            return err
        contacts = data.get("Contacts") or []

        def _fmt(c: dict) -> str:
            return (
                f"  {c.get('EmailAddress', '?')} — "
                f"unsubscribed from all: {c.get('UnsubscribeAll', False)} "
                f"(updated {str(c.get('LastUpdatedTimestamp', ''))[:19]})"
            )

        out = format_entity_list(contacts, _fmt, noun="contact")
        if data.get("NextToken"):
            out += f'\n[More results — pass next_token="{data["NextToken"]}"]'
        return out

    async def get_ses_contact(contact_list_name: str, email_address: str) -> str:
        """Get a contact's topic subscription preferences."""
        data, err = await _api(
            "get",
            f"{_API_PREFIX}/contact-lists/{contact_list_name}/contacts/{email_address}",
            f"get contact {email_address}",
        )
        if err:
            return err
        prefs = data.get("TopicPreferences") or []
        lines = [
            f"Contact: {data.get('EmailAddress', email_address)}",
            f"Unsubscribed from all: {data.get('UnsubscribeAll', False)}",
            "Topic preferences:",
        ]
        for p in prefs:
            lines.append(
                f"  - {p.get('TopicName', '?')}: {p.get('SubscriptionStatus', '?')}"
            )
        if not prefs:
            lines.append("  (none)")
        return "\n".join(lines)

    async def create_ses_contact(
        contact_list_name: str,
        email_address: str,
        topic_preferences: list[dict] | None = None,
        unsubscribe_all: bool = False,
    ) -> str:
        """Add a contact to a list.

        ``topic_preferences`` is a list of {TopicName, SubscriptionStatus} where
        SubscriptionStatus is OPT_IN or OPT_OUT.
        """
        body: dict[str, Any] = {
            "EmailAddress": email_address,
            "UnsubscribeAll": unsubscribe_all,
        }
        if topic_preferences:
            body["TopicPreferences"] = topic_preferences
        _data, err = await _api(
            "post",
            f"{_API_PREFIX}/contact-lists/{contact_list_name}/contacts",
            f"create contact {email_address}",
            body=body,
        )
        if err:
            return err
        return f"Contact '{email_address}' added to '{contact_list_name}'."

    async def update_ses_contact(
        contact_list_name: str,
        email_address: str,
        topic_preferences: list[dict] | None = None,
        unsubscribe_all: bool | None = None,
    ) -> str:
        """Update a contact's topic subscriptions or unsubscribe them from all."""
        body: dict[str, Any] = {}
        if topic_preferences is not None:
            body["TopicPreferences"] = topic_preferences
        if unsubscribe_all is not None:
            body["UnsubscribeAll"] = unsubscribe_all
        if not body:
            return "Error: provide topic_preferences and/or unsubscribe_all to update."
        _data, err = await _api(
            "put",
            f"{_API_PREFIX}/contact-lists/{contact_list_name}/contacts/{email_address}",
            f"update contact {email_address}",
            body=body,
        )
        if err:
            return err
        return f"Contact '{email_address}' updated."

    async def delete_ses_contact(contact_list_name: str, email_address: str) -> str:
        """Remove a contact from a contact list."""
        _data, err = await _api(
            "delete",
            f"{_API_PREFIX}/contact-lists/{contact_list_name}/contacts/{email_address}",
            f"delete contact {email_address}",
        )
        if err:
            return err
        return f"Contact '{email_address}' removed from '{contact_list_name}'."

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("send_ses_email", send_ses_email)
    executor.register("send_ses_templated_email", send_ses_templated_email)
    executor.register("send_ses_bulk_email", send_ses_bulk_email)
    executor.register("get_ses_email_signature", get_ses_email_signature)
    executor.register("get_ses_account", get_ses_account)
    executor.register("list_ses_identities", list_ses_identities)
    executor.register("get_ses_identity", get_ses_identity)
    executor.register("create_ses_identity", create_ses_identity)
    executor.register("delete_ses_identity", delete_ses_identity)
    executor.register("list_ses_templates", list_ses_templates)
    executor.register("get_ses_template", get_ses_template)
    executor.register("create_ses_template", create_ses_template)
    executor.register("update_ses_template", update_ses_template)
    executor.register("delete_ses_template", delete_ses_template)
    executor.register("list_ses_configuration_sets", list_ses_configuration_sets)
    executor.register("get_ses_configuration_set", get_ses_configuration_set)
    executor.register("create_ses_configuration_set", create_ses_configuration_set)
    executor.register("delete_ses_configuration_set", delete_ses_configuration_set)
    executor.register(
        "list_ses_suppressed_destinations", list_ses_suppressed_destinations
    )
    executor.register("get_ses_suppressed_destination", get_ses_suppressed_destination)
    executor.register("add_ses_suppressed_destination", add_ses_suppressed_destination)
    executor.register(
        "delete_ses_suppressed_destination", delete_ses_suppressed_destination
    )
    executor.register("list_ses_contact_lists", list_ses_contact_lists)
    executor.register("get_ses_contact_list", get_ses_contact_list)
    executor.register("create_ses_contact_list", create_ses_contact_list)
    executor.register("delete_ses_contact_list", delete_ses_contact_list)
    executor.register("list_ses_contacts", list_ses_contacts)
    executor.register("get_ses_contact", get_ses_contact)
    executor.register("create_ses_contact", create_ses_contact)
    executor.register("update_ses_contact", update_ses_contact)
    executor.register("delete_ses_contact", delete_ses_contact)

    src = "integration:ses"
    _str = {"type": "string"}
    # What the model reads is what this session does.
    template_signature = (
        " The stored template is sent as it is: no signature is added." if signer.signs else ""
    )
    return [
        # ── 0. Sending ────────────────────────────────────────────────────
        ToolDefinition(
            name="send_ses_email",
            description=(
                "Send an email via Amazon SES with a subject and a text and/or "
                "HTML body. The From address must be a verified SES identity. "
                "Recipients (to/cc/bcc) are comma-separated. Optionally set a "
                "configuration set and list-management options (contact list + "
                "topic) so SES adds a working unsubscribe link."
                f"{signer.tool_note()}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_address": {
                        "type": "string",
                        "description": "Verified sender, e.g. 'Name <sender@example.com>'",
                    },
                    "to": {
                        "type": "string",
                        "description": "Recipient address(es), comma-separated",
                    },
                    "subject": _str,
                    "text_body": {
                        "type": "string",
                        "description": "Plain-text body (provide this and/or html_body)",
                    },
                    "html_body": {
                        "type": "string",
                        "description": "HTML body (provide this and/or text_body)",
                    },
                    "cc": {"type": "string", "description": "CC address(es), comma-separated"},
                    "bcc": {"type": "string", "description": "BCC address(es), comma-separated"},
                    "reply_to": {
                        "type": "string",
                        "description": "Reply-To address(es), comma-separated",
                    },
                    "configuration_set": {
                        "type": "string",
                        "description": "Configuration set name (optional override)",
                    },
                    "contact_list_name": {
                        "type": "string",
                        "description": "Contact list for unsubscribe link (optional)",
                    },
                    "topic_name": {
                        "type": "string",
                        "description": "Topic within the contact list (optional)",
                    },
                },
                "required": ["from_address", "to", "subject"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="send_ses_templated_email",
            description=(
                "Send an email rendered from a stored SES template, replacing "
                "{{placeholders}} with template_data. The From address must be a "
                f"verified identity.{template_signature}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_address": _str,
                    "to": {"type": "string", "description": "Recipient(s), comma-separated"},
                    "template_name": _str,
                    "template_data": {
                        "type": "object",
                        "description": "Personalization values for the template placeholders",
                    },
                    "cc": _str,
                    "bcc": _str,
                    "reply_to": _str,
                    "configuration_set": _str,
                    "contact_list_name": _str,
                    "topic_name": _str,
                },
                "required": ["from_address", "to", "template_name"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="send_ses_bulk_email",
            description=(
                "Send a templated email to many recipients in one call. Provide "
                "a template_name, optional default_template_data, and a "
                "'recipients' array where each item has a 'to' address and "
                "optional 'replacement_data' (per-recipient personalization). "
                f"Returns per-recipient success/failure.{template_signature}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_address": _str,
                    "template_name": _str,
                    "recipients": {
                        "type": "array",
                        "description": "Per-recipient entries",
                        "items": {
                            "type": "object",
                            "properties": {
                                "to": {
                                    "type": "string",
                                    "description": "Recipient address(es), comma-separated",
                                },
                                "replacement_data": {
                                    "type": "object",
                                    "description": "Per-recipient template values (optional)",
                                },
                            },
                            "required": ["to"],
                        },
                    },
                    "default_template_data": {
                        "type": "object",
                        "description": "Default template values when a recipient omits replacement_data",
                    },
                    "reply_to": _str,
                    "configuration_set": _str,
                },
                "required": ["from_address", "template_name", "recipients"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_ses_email_signature",
            description=(
                "Retrieve the email signature send_ses_email adds for a From address, and where it "
                "comes from (the integration's signature integration). When there is none, it says "
                "why and how to set one up."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_address": {
                        "type": "string",
                        "description": "The sender address, as send_ses_email takes it (e.g. sender@example.com)",
                    },
                },
                "required": ["from_address"],
            },
            source=src,
            priority=2,
        ),
        # ── 1. Account ────────────────────────────────────────────────────
        ToolDefinition(
            name="get_ses_account",
            description=(
                "Get the Amazon SES account status: whether sending is enabled, "
                "production vs sandbox access, the 24-hour sending quota, max "
                "send rate, messages sent in the last 24h, and reputation."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=src,
            priority=1,
        ),
        # ── 2. Identities ─────────────────────────────────────────────────
        ToolDefinition(
            name="list_ses_identities",
            description=(
                "List email-address and domain identities with their "
                "verification status and whether sending is enabled."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page_size": {"type": "integer", "default": 50},
                    "next_token": {"type": "string"},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_ses_identity",
            description=(
                "Get an identity's verification status, DKIM signing status and "
                "CNAME tokens, and MAIL FROM configuration."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "identity": {
                        "type": "string",
                        "description": "Email address or domain",
                    }
                },
                "required": ["identity"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="create_ses_identity",
            description=(
                "Verify a new sender identity. For an email address SES sends a "
                "verification link; for a domain SES returns DKIM CNAME records "
                "to publish in DNS."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "identity": {
                        "type": "string",
                        "description": "Email address or domain to verify",
                    }
                },
                "required": ["identity"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="delete_ses_identity",
            description="Delete a verified email-address or domain identity.",
            input_schema={
                "type": "object",
                "properties": {"identity": {"type": "string"}},
                "required": ["identity"],
            },
            source=src,
            priority=3,
        ),
        # ── 3. Templates ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_ses_templates",
            description="List the email templates defined in the account.",
            input_schema={
                "type": "object",
                "properties": {
                    "page_size": {"type": "integer", "default": 50},
                    "next_token": {"type": "string"},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_ses_template",
            description="Get the subject, text, and HTML content of an email template.",
            input_schema={
                "type": "object",
                "properties": {"template_name": {"type": "string"}},
                "required": ["template_name"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="create_ses_template",
            description=(
                "Create an email template. Use {{placeholder}} syntax in the "
                "subject and bodies for later personalization."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "template_name": {"type": "string"},
                    "subject": {"type": "string"},
                    "text_body": {"type": "string"},
                    "html_body": {"type": "string"},
                },
                "required": ["template_name", "subject"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="update_ses_template",
            description="Update an existing email template's subject and body.",
            input_schema={
                "type": "object",
                "properties": {
                    "template_name": {"type": "string"},
                    "subject": {"type": "string"},
                    "text_body": {"type": "string"},
                    "html_body": {"type": "string"},
                },
                "required": ["template_name", "subject"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="delete_ses_template",
            description="Delete an email template.",
            input_schema={
                "type": "object",
                "properties": {"template_name": {"type": "string"}},
                "required": ["template_name"],
            },
            source=src,
            priority=3,
        ),
        # ── 4. Configuration sets ─────────────────────────────────────────
        ToolDefinition(
            name="list_ses_configuration_sets",
            description="List configuration sets (groups of sending rules and event destinations).",
            input_schema={
                "type": "object",
                "properties": {
                    "page_size": {"type": "integer", "default": 50},
                    "next_token": {"type": "string"},
                },
                "required": [],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="get_ses_configuration_set",
            description="Get the delivery, sending, reputation, and suppression options of a configuration set.",
            input_schema={
                "type": "object",
                "properties": {"configuration_set_name": {"type": "string"}},
                "required": ["configuration_set_name"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="create_ses_configuration_set",
            description=(
                "Create a configuration set. tls_policy is REQUIRE or OPTIONAL; "
                "reputation_metrics and sending_enabled default to true."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "configuration_set_name": {"type": "string"},
                    "tls_policy": {
                        "type": "string",
                        "enum": ["REQUIRE", "OPTIONAL"],
                        "default": "OPTIONAL",
                    },
                    "reputation_metrics": {"type": "boolean", "default": True},
                    "sending_enabled": {"type": "boolean", "default": True},
                },
                "required": ["configuration_set_name"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="delete_ses_configuration_set",
            description="Delete a configuration set.",
            input_schema={
                "type": "object",
                "properties": {"configuration_set_name": {"type": "string"}},
                "required": ["configuration_set_name"],
            },
            source=src,
            priority=3,
        ),
        # ── 5. Suppression list ───────────────────────────────────────────
        ToolDefinition(
            name="list_ses_suppressed_destinations",
            description=(
                "List addresses on the account-level suppression list. Filter by "
                "reason (BOUNCE or COMPLAINT)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "BOUNCE or COMPLAINT (optional)"},
                    "page_size": {"type": "integer", "default": 50},
                    "next_token": {"type": "string"},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_ses_suppressed_destination",
            description="Check whether a specific address is on the suppression list and why.",
            input_schema={
                "type": "object",
                "properties": {"email_address": {"type": "string"}},
                "required": ["email_address"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="add_ses_suppressed_destination",
            description="Add an address to the account suppression list. reason: BOUNCE or COMPLAINT.",
            input_schema={
                "type": "object",
                "properties": {
                    "email_address": {"type": "string"},
                    "reason": {"type": "string", "default": "BOUNCE"},
                },
                "required": ["email_address"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="delete_ses_suppressed_destination",
            description="Remove an address from the account suppression list (allow sending to it again).",
            input_schema={
                "type": "object",
                "properties": {"email_address": {"type": "string"}},
                "required": ["email_address"],
            },
            source=src,
            priority=3,
        ),
        # ── 6. Contact lists ──────────────────────────────────────────────
        ToolDefinition(
            name="list_ses_contact_lists",
            description="List contact lists in the account.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="get_ses_contact_list",
            description="Get a contact list's description and its topics.",
            input_schema={
                "type": "object",
                "properties": {"contact_list_name": {"type": "string"}},
                "required": ["contact_list_name"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="create_ses_contact_list",
            description=(
                "Create a contact list. 'topics' is an optional array of "
                "{TopicName, DisplayName, DefaultSubscriptionStatus, Description} "
                "(DefaultSubscriptionStatus is OPT_IN or OPT_OUT)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "contact_list_name": {"type": "string"},
                    "description": {"type": "string"},
                    "topics": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["contact_list_name"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="delete_ses_contact_list",
            description="Delete a contact list and all of its contacts.",
            input_schema={
                "type": "object",
                "properties": {"contact_list_name": {"type": "string"}},
                "required": ["contact_list_name"],
            },
            source=src,
            priority=3,
        ),
        # ── 7. Contacts ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_ses_contacts",
            personal_records=True,
            description="List the contacts in a contact list.",
            input_schema={
                "type": "object",
                "properties": {
                    "contact_list_name": {"type": "string"},
                    "page_size": {"type": "integer", "default": 50},
                    "next_token": {"type": "string"},
                },
                "required": ["contact_list_name"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_ses_contact",
            personal_records=True,
            description="Get a contact's per-topic subscription preferences.",
            input_schema={
                "type": "object",
                "properties": {
                    "contact_list_name": {"type": "string"},
                    "email_address": {"type": "string"},
                },
                "required": ["contact_list_name", "email_address"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="create_ses_contact",
            description=(
                "Add a contact to a list. 'topic_preferences' is an optional "
                "array of {TopicName, SubscriptionStatus} (OPT_IN or OPT_OUT)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "contact_list_name": {"type": "string"},
                    "email_address": {"type": "string"},
                    "topic_preferences": {"type": "array", "items": {"type": "object"}},
                    "unsubscribe_all": {"type": "boolean", "default": False},
                },
                "required": ["contact_list_name", "email_address"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="update_ses_contact",
            description=(
                "Update a contact's topic subscriptions, or unsubscribe them "
                "from all topics (unsubscribe_all=true)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "contact_list_name": {"type": "string"},
                    "email_address": {"type": "string"},
                    "topic_preferences": {"type": "array", "items": {"type": "object"}},
                    "unsubscribe_all": {"type": "boolean"},
                },
                "required": ["contact_list_name", "email_address"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="delete_ses_contact",
            description="Remove a contact from a contact list.",
            input_schema={
                "type": "object",
                "properties": {
                    "contact_list_name": {"type": "string"},
                    "email_address": {"type": "string"},
                },
                "required": ["contact_list_name", "email_address"],
            },
            source=src,
            priority=3,
        ),
    ]

# SPDX-License-Identifier: Apache-2.0

"""Sublime Security integration - agentic email detection and response.

Full-fledged Sublime Security (sublime.security) integration covering the
Sublime Platform API (``/v0``) surfaces an agentic security team needs:

Message groups (the triage queue)
  - List with rich triage filters (flagged, reviewed, user-reported, sender /
    recipient / subject, flagged-rule severity, attack score verdict, time
    ranges) and search by attachment hash, filename, or free text
  - Group detail (flagged rules, message previews, user reports, link clicks)
  - Bulk review/classify with response actions, and single-group trash /
    quarantine / restore / dismiss

Messages
  - Metadata, raw EML retrieval for phishing analysis, Attack Score + ASA
    (Autonomous Security Analyst) verdict, per-message response actions,
    and async task polling

Hunting
  - MQL hunt jobs: start over a time range with triage pre-filters, poll
    status, page detailed results

Detection rules
  - List/search, get (including MQL source), validate MQL, create / update /
    activate / deactivate / delete

Lists (threat intel)
  - List by entry type, get with entries, create / update / delete and entry
    management (add / delete / overwrite)

Visibility and enrichment
  - User phishing reports, protected mailbox inventory, email bombs (list +
    dismiss), audit log events, and ml.link_analysis URL enrichment

Authentication
--------------
The Sublime Platform API authenticates every request with a single API key
sent as a Bearer token (``Authorization: Bearer <API_KEY>``), created in the
dashboard under Automate > API. ``integration_tools._get_token`` hands this
builder the compound ``base_url||api_key`` token; ``base_url`` is optional and
defaults to ``https://platform.sublime.security`` (the common Sublime Cloud
host - regional tenants such as ``https://eu.platform.sublime.security`` and
self-hosted deployments override it).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    bearer_auth_headers,
    format_entity_list,
    format_pagination_hint,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Sublime Security"
_SOURCE = "integration:sublime"

# Default Sublime Cloud host (NA-East). Regional tenants (na-west., ca., uk.,
# eu., au. platform.sublime.security) and self-hosted deployments override it
# via the stored base_url.
_DEFAULT_BASE_URL = "https://platform.sublime.security"

# Message-group / message classifications (review + group actions).
_CLASSIFICATIONS = (
    "malicious",
    "benign",
    "spam",
    "graymail",
    "simulation",
    "unwanted",
    "violation",
    "non-violation",
)

# Rule severities (list filter + create/update).
_SEVERITIES = ("informational", "low", "medium", "high", "critical")

# Hunt job polling (POST /v0/hunt-jobs -> GET /v0/hunt-jobs/{id}).
_HUNT_POLL_INTERVAL = 2.0
_HUNT_MAX_POLLS = 12

# Response actions shared by the bulk review endpoint and the per-message
# actions endpoint (POST /v0/message-groups/review, POST /v0/messages/{id}/actions).
_MESSAGE_ACTIONS = (
    "restore",
    "warning_banner",
    "quarantine",
    "trash",
    "move_to_spam",
    "move_to_graymail",
    "delete_calendar_events",
)

# Report labels accepted by the single-group action endpoints.
_REPORT_LABELS = (
    "spam",
    "phishing",
    "false_positive",
    "false_negative",
    "phishing_simulation",
    "graymail",
    "violation",
    "non-violation",
)

# Single-group actions (POST /v0/message-groups/{id}/{action}).
_GROUP_ACTIONS: frozenset[str] = frozenset(
    {"trash", "quarantine", "restore", "dismiss"}
)

# Consolidated rule lifecycle actions.
_RULE_ACTIONS: frozenset[str] = frozenset({"activate", "deactivate", "delete"})

# Consolidated list management actions.
_LIST_ACTIONS: frozenset[str] = frozenset(
    {
        "create",
        "update_description",
        "delete",
        "add_entry",
        "delete_entry",
        "set_entries",
    }
)


def _path(value: Any) -> str:
    """Percent-encode a value for use as a single URL path segment.

    User-supplied ids (message ids, group canonical ids, rule ids, ...) are
    interpolated into request paths; encoding with ``safe=""`` prevents an id
    containing ``/`` or ``..`` from escaping its path segment and turning a
    scoped tool into a generic API caller (same pattern as the Sekoia /
    Recorded Future / ServiceNow providers).
    """
    return quote(str(value).strip(), safe="")


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _ts(value: Any) -> str:
    """Render a Sublime ISO timestamp compactly (drop sub-second noise)."""
    if value is None or value == "":
        return "?"
    return str(value)[:19]


def _clamp(value: Any, default: int, maximum: int) -> int:
    """Clamp a limit-style integer into [1, maximum]."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if number <= 0:
        number = default
    return max(1, min(number, maximum))


def _offset(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def sublime_api_base(base_url: str) -> str:
    """Return the Sublime Platform API base URL (no trailing slash).

    Whitespace is stripped before the default fallback and the trailing-slash
    trim (matching ``sekoia_api_base`` / ``rapid7_api_base``), so a pasted
    ``base_url`` with surrounding whitespace - or an entirely whitespace-only
    value - normalises to a valid host instead of an empty string.
    """
    return ((base_url or "").strip() or _DEFAULT_BASE_URL).rstrip("/")


def build_sublime_headers(api_key: str) -> dict[str, str]:
    """Build Sublime Platform API request headers (Bearer key + JSON accept).

    The key is normalised by ``bearer_auth_headers`` (stripped, blank-guarded)
    so pasted whitespace can never produce a malformed ``Authorization``
    header. Single chokepoint shared by the tool builder and the connectivity
    test.
    """
    return bearer_auth_headers(api_key, extra={"Accept": "application/json"})


class SublimeIntegration(BaseIntegration):
    """Sublime Security email detection and response (API key, Bearer auth)."""

    provider_name = "sublime"
    display_name = "Sublime Security"
    icon = "MailWarning"
    description = (
        "Connect Sublime Security for email detection and response: triage "
        "flagged message groups, review and remediate malicious mail, hunt "
        "with MQL, manage detection rules and threat-intel lists, and enrich "
        "alerts with attack scores and link analysis."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        """Sublime uses a Bearer API key, not OAuth."""
        return None

    async def validate_connection(self, token: str) -> bool:
        """Validate the API key with a 1-row rules read (cheap, read-only)."""
        base_url, api_key = (
            p.strip() for p in parse_compound_token(token, 2, greedy_last=True)
        )
        if not api_key:
            return False
        api = sublime_api_base(base_url)
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/rules",
                _PROVIDER,
                "validate API key",
                headers=build_sublime_headers(api_key),
                params={"limit": 1},
            )
            return err is None and resp is not None and resp.is_success

    async def get_user_info(self, token: str) -> dict:
        """Return minimal account context (identity is bound to the API key)."""
        base_url, _ = (
            p.strip() for p in parse_compound_token(token, 2, greedy_last=True)
        )
        return {"name": "Sublime Security", "base_url": sublime_api_base(base_url)}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_message_group(group: dict) -> str:
    """Compact one-group summary: first preview + rules + review state."""
    messages = group.get("messages") or []
    first = messages[0] if messages and isinstance(messages[0], dict) else {}
    sender = first.get("sender") or {}
    mailbox = first.get("mailbox") or {}
    rules = group.get("flagged_rules") or []
    reports = group.get("user_reports") or []
    line = (
        f"  {group.get('id', '?')} [{group.get('state') or 'no state'}] "
        f"{_trunc(first.get('subject'), 80) or '(no subject)'}\n"
        f"    From: {sender.get('email', '?')} -> {mailbox.get('email', '?')} | "
        f"Messages: {len(messages)} | Created: {_ts(first.get('created_at'))}"
    )
    state_bits: list[str] = []
    if group.get("classification"):
        state_bits.append(f"classification {group['classification']}")
    if group.get("review_status"):
        state_bits.append(f"review {group['review_status']}")
    if reports:
        state_bits.append(f"{len(reports)} user report(s)")
    if state_bits:
        line += f"\n    {' | '.join(state_bits)}"
    if rules:
        names = ", ".join(_trunc(r.get("name"), 50) for r in rules[:4])
        line += f"\n    Flagged rules ({len(rules)}): {names}"
        if len(rules) > 4:
            line += ", ..."
    return line


def _fmt_rule(rule: dict) -> str:
    return (
        f"  {_trunc(rule.get('name'), 80)} "
        f"[{'active' if rule.get('active') else 'inactive'}] "
        f"severity {rule.get('severity') or 'n/a'} | type {rule.get('type') or '?'}\n"
        f"    ID: {rule.get('id', '?')} | Updated: {_ts(rule.get('updated_at'))}"
        + (f"\n    {_trunc(rule.get('description'), 140)}" if rule.get("description") else "")
    )


def _group_list_footer(total: Any, offset: int, shown: int) -> str:
    if isinstance(total, int) and total > offset + shown:
        return (
            f"\n[Showing {shown} from offset {offset} of {total} - "
            "pass a higher offset for more]"
        )
    return ""


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Sublime Security tools from a ``base_url||api_key`` token."""
    # ``parse_compound_token`` does not normalise - strip each segment so pasted
    # whitespace can't yield a malformed base URL / Authorization header, and so
    # the empty-credential guard treats a whitespace-only key as missing.
    base_url, api_key = (
        part.strip() for part in parse_compound_token(token, 2, greedy_last=True)
    )
    if not api_key:
        logger.debug(
            "Sublime Security API key is empty - returning no tools. Edit the "
            "integration and set the API key."
        )
        return []

    api = sublime_api_base(base_url)
    headers = build_sublime_headers(api_key)

    def _client(timeout: int = 30) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_sublime_status() -> str:
        """Validate the Sublime Security connection and API key."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/rules",
                _PROVIDER,
                "validate API key",
                headers=headers,
                params={"limit": 1},
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
        total = data.get("total") if isinstance(data, dict) else None
        suffix = (
            f" The deployment has {total} detection rule(s)."
            if isinstance(total, int)
            else ""
        )
        return f"Connected to Sublime Security at {api}. API key is valid.{suffix}"

    # ══════════════════════════════════════════════════════════════════════
    # 1. MESSAGE GROUPS (triage queue)
    # ══════════════════════════════════════════════════════════════════════

    async def list_sublime_message_groups(
        flagged: bool | None = None,
        reviewed: bool | None = None,
        user_reported: bool | None = None,
        sender_email: str = "",
        sender_domain: str = "",
        recipient_email: str = "",
        mailbox_email: str = "",
        subject: str = "",
        flagged_rule_severity: str = "",
        attack_score_verdict: str = "",
        created_after: str = "",
        created_before: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """List message groups with triage filters (the Sublime review queue)."""
        params: dict[str, Any] = {
            "limit": _clamp(limit, 20, 500),
            "offset": _offset(offset),
        }
        if flagged is not None:
            params["flagged"] = flagged
        if reviewed is not None:
            params["reviewed"] = reviewed
        if user_reported is not None:
            params["user_reported"] = user_reported
        if sender_email.strip():
            params["sender_email__is"] = sender_email.strip()
        if sender_domain.strip():
            params["sender_domain__is"] = sender_domain.strip()
        if recipient_email.strip():
            params["recipient_email__is"] = recipient_email.strip()
        if mailbox_email.strip():
            params["mailbox_email__is"] = mailbox_email.strip()
        if subject.strip():
            params["subject__is"] = subject.strip()
        if flagged_rule_severity.strip():
            params["flagged_rule_severity__is"] = flagged_rule_severity.strip()
        if attack_score_verdict.strip():
            params["attack_score_verdict__is"] = attack_score_verdict.strip()
        if created_after.strip():
            params["created_at__gte"] = created_after.strip()
        if created_before.strip():
            params["created_at__lt"] = created_before.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/message-groups",
                _PROVIDER,
                "list message groups",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        groups = data.get("message_groups") or []
        total = data.get("total")
        out = format_entity_list(
            groups,
            _fmt_message_group,
            noun="message group",
            total=total if isinstance(total, int) else None,
            empty_msg="No message groups matched the filters.",
        )
        return out + _group_list_footer(total, params["offset"], len(groups))

    async def search_sublime_message_groups(
        query: str = "",
        attachment_md5: str = "",
        attachment_sha256: str = "",
        file_name: str = "",
        created_after: str = "",
        created_before: str = "",
        page_token: str = "",
    ) -> str:
        """Search message groups by free text, attachment hash, or filename."""
        if query.strip() and (
            attachment_md5.strip() or attachment_sha256.strip() or file_name.strip()
        ):
            return (
                "Error: query is a standalone free-text search - it cannot be "
                "combined with attachment_md5, attachment_sha256, or "
                "file_name. Use query alone, or the attachment filters "
                "without query."
            )
        params: dict[str, Any] = {}
        if query.strip():
            params["any"] = query.strip()
        if attachment_md5.strip():
            params["attachment_md5"] = attachment_md5.strip()
        if attachment_sha256.strip():
            params["attachment_sha256"] = attachment_sha256.strip()
        if file_name.strip():
            params["file_name"] = file_name.strip()
        if created_after.strip():
            params["created_at[gte]"] = created_after.strip()
        if created_before.strip():
            params["created_at[lt]"] = created_before.strip()
        if page_token.strip():
            params["page_token"] = page_token.strip()
        if not params:
            return (
                "Error: provide at least one search criterion (query, "
                "attachment_md5, attachment_sha256, or file_name)."
            )
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/message-groups/search",
                _PROVIDER,
                "search message groups",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        groups = data.get("message_groups") or []
        out = format_entity_list(
            groups,
            _fmt_message_group,
            noun="message group",
            empty_msg="No message groups matched the search.",
        )
        next_token = data.get("next_page_token")
        if next_token:
            out += format_pagination_hint(str(next_token), param_name="page_token")
        return out

    async def get_sublime_message_group(group_id: str) -> str:
        """Get one message group by canonical id (full triage detail)."""
        gid = group_id.strip()
        if not gid:
            return "Error: group_id is required."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/message-groups/{_path(gid)}",
                _PROVIDER,
                f"get message group {gid}",
                headers=headers,
            )
            if err:
                return err
            group = resp.json() or {}
        lines = [
            f"Message group {group.get('id', gid)}",
            f"State: {group.get('state') or 'n/a'} | "
            f"Classification: {group.get('classification') or 'n/a'} | "
            f"Review: {group.get('review_status') or 'n/a'}"
            + (f" ({group.get('review_label')})" if group.get("review_label") else ""),
        ]
        if group.get("review_comment"):
            lines.append(f"Review comment: {_trunc(group.get('review_comment'), 200)}")
        rules = group.get("flagged_rules") or []
        if rules:
            lines.append(f"Flagged rules ({len(rules)}):")
            for rule in rules[:10]:
                tags = rule.get("tags") or []
                tag_str = f" [{', '.join(map(str, tags[:4]))}]" if tags else ""
                lines.append(
                    f"  - {_trunc(rule.get('name'), 90)}{tag_str} (id {rule.get('id', '?')})"
                )
            if len(rules) > 10:
                lines.append(f"  ... and {len(rules) - 10} more")
        messages = group.get("messages") or []
        if messages:
            lines.append(f"Messages ({len(messages)}):")
            for msg in messages[:8]:
                sender = msg.get("sender") or {}
                mailbox = msg.get("mailbox") or {}
                state_bits = []
                if msg.get("read_at"):
                    state_bits.append("read")
                if msg.get("replied_at"):
                    state_bits.append("replied")
                if msg.get("forwarded_at"):
                    state_bits.append("forwarded")
                state = f" [{', '.join(state_bits)}]" if state_bits else ""
                lines.append(
                    f"  - {msg.get('id', '?')}: {_trunc(msg.get('subject'), 70)}\n"
                    f"    {sender.get('email', '?')} -> {mailbox.get('email', '?')} | "
                    f"delivered: {msg.get('delivered')}{state} | {_ts(msg.get('created_at'))}"
                )
            if len(messages) > 8:
                lines.append(f"  ... and {len(messages) - 8} more")
        reports = group.get("user_reports") or []
        if reports:
            lines.append(f"User reports ({len(reports)}):")
            for report in reports[:5]:
                lines.append(
                    f"  - {report.get('reporter', '?')} via {report.get('channel', '?')} "
                    f"at {_ts(report.get('reported_at'))}"
                )
        clicks = group.get("message_links_clicked") or []
        if clicks:
            lines.append(f"Clicked links ({len(clicks)}):")
            for click in clicks[:5]:
                events = click.get("clicks") or []
                lines.append(
                    f"  - {_trunc(click.get('url'), 100)} ({len(events)} click(s))"
                )
        lines.append(
            "Use get_sublime_message for per-message detail, "
            "get_sublime_message_attack_score for the AI verdict."
        )
        return "\n".join(lines)

    async def review_sublime_message_groups(
        message_group_ids: str,
        classification: str,
        action: str = "",
        review_comment: str = "",
    ) -> str:
        """Bulk review: classify message groups and optionally apply an action."""
        ids = [gid.strip() for gid in message_group_ids.split(",") if gid.strip()]
        if not ids:
            return "Error: message_group_ids is required (comma-separated canonical ids)."
        cls = classification.lower().strip()
        if cls not in (*_CLASSIFICATIONS, "skip"):
            return (
                "Error: classification must be one of "
                f"{', '.join((*_CLASSIFICATIONS, 'skip'))}."
            )
        body: dict[str, Any] = {"message_group_ids": ids, "classification": cls}
        act = action.lower().strip()
        if act:
            if act not in _MESSAGE_ACTIONS:
                return f"Error: action must be one of {', '.join(_MESSAGE_ACTIONS)}."
            body["action"] = act
        if review_comment.strip():
            body["review_comment"] = review_comment.strip()
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/v0/message-groups/review",
                _PROVIDER,
                "review message groups",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
        applied = data.get("actions") or ([act] if act else [])
        summary = (
            f"Reviewed {len(ids)} message group(s) as '{cls}'"
            + (f" with action(s): {', '.join(map(str, applied))}" if applied else "")
            + "."
        )
        if data.get("task_id"):
            summary += (
                f" Actions run asynchronously - track with "
                f"get_sublime_task(task_id='{data['task_id']}')."
            )
        return summary

    async def manage_sublime_message_group(
        action: str,
        group_id: str,
        classification: str = "",
        report_label: str = "",
        review_comment: str = "",
    ) -> str:
        """Trash / quarantine / restore / dismiss ONE message group."""
        act = action.lower().strip()
        if act not in _GROUP_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_GROUP_ACTIONS))}."
        gid = group_id.strip()
        if not gid:
            return "Error: group_id is required."
        body: dict[str, Any] = {}
        cls = classification.lower().strip()
        if cls:
            if cls not in _CLASSIFICATIONS:
                return (
                    f"Error: classification must be one of {', '.join(_CLASSIFICATIONS)}."
                )
            body["classification"] = cls
        label = report_label.lower().strip()
        if label:
            if label not in _REPORT_LABELS:
                return f"Error: report_label must be one of {', '.join(_REPORT_LABELS)}."
            body["report_label"] = label
        if review_comment.strip():
            body["review_comment"] = review_comment.strip()
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/v0/message-groups/{_path(gid)}/{act}",
                _PROVIDER,
                f"{act} message group {gid}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
        summary = (
            f"Message group {gid} {act} applied to all messages in the group, "
            "including future ones."
        )
        if act == "restore":
            summary = (
                f"Message group {gid} restored - previously trashed/quarantined "
                "messages are put back and automatic actions are turned off."
            )
        if isinstance(data, dict) and data.get("task_id"):
            summary += (
                f" Runs asynchronously - track with "
                f"get_sublime_task(task_id='{data['task_id']}')."
            )
        return summary

    # ══════════════════════════════════════════════════════════════════════
    # 2. MESSAGES
    # ══════════════════════════════════════════════════════════════════════

    async def get_sublime_message(message_id: str) -> str:
        """Get one message's metadata by Sublime message id."""
        mid = message_id.strip()
        if not mid:
            return "Error: message_id is required."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/messages/{_path(mid)}",
                _PROVIDER,
                f"get message {mid}",
                headers=headers,
            )
            if err:
                return err
            msg = resp.json() or {}
        sender = msg.get("sender") or {}
        mailbox = msg.get("mailbox") or {}
        recipients = [r.get("email", "?") for r in (msg.get("recipients") or [])]
        lines = [
            f"Message {msg.get('id', mid)} (group {msg.get('canonical_id', '?')})",
            f"Subject: {_trunc(msg.get('subject'), 140)}",
            f"From: {sender.get('email', '?')}"
            + (f" ({sender.get('display_name')})" if sender.get("display_name") else ""),
            f"To: {_trunc(', '.join(recipients), 200) or '?'} | "
            f"Mailbox: {mailbox.get('email', '?')}",
            f"Created: {_ts(msg.get('created_at'))} | "
            f"Landed in spam: {msg.get('landed_in_spam')} | "
            f"Read: {_ts(msg.get('read_at')) if msg.get('read_at') else 'no'} | "
            f"Replied: {_ts(msg.get('replied_at')) if msg.get('replied_at') else 'no'}",
        ]
        forwards = msg.get("forward_recipients") or []
        if forwards:
            lines.append(f"Forwarded to: {_trunc(', '.join(map(str, forwards)), 200)}")
        return "\n".join(lines)

    async def get_sublime_message_eml(message_id: str, max_chars: int = 4000) -> str:
        """Fetch the raw EML of a message (truncated) for phishing analysis."""
        mid = message_id.strip()
        if not mid:
            return "Error: message_id is required."
        capped = _clamp(max_chars, 4000, 20000)
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/messages/{_path(mid)}/eml",
                _PROVIDER,
                f"get message EML {mid}",
                headers=headers,
            )
            if err:
                return err
            raw = resp.text or ""
        if not raw.strip():
            return f"Message {mid} returned an empty EML body."
        header = f"Raw EML for message {mid} ({len(raw)} chars"
        if len(raw) > capped:
            return f"{header}, showing first {capped}):\n{raw[:capped]}\n[...truncated]"
        return f"{header}):\n{raw}"

    async def get_sublime_message_attack_score(message_id: str) -> str:
        """Get the Attack Score (+ top signals) and ASA verdict of a message."""
        mid = message_id.strip()
        if not mid:
            return "Error: message_id is required."
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/messages/{_path(mid)}/attack_score",
                _PROVIDER,
                f"get attack score {mid}",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() or {}
            lines = [
                f"Attack score for message {mid}: {data.get('score', 'n/a')}/100 "
                f"(verdict: {data.get('verdict') or 'unknown'})",
            ]
            if data.get("graymail_score") is not None:
                lines.append(f"Graymail score: {data.get('graymail_score')}/100")
            signals = data.get("top_signals") or []
            if signals:
                lines.append("Top signals:")
                for signal in signals[:8]:
                    lines.append(
                        f"  {signal.get('rank', '?')}. [{signal.get('category', '?')}] "
                        f"{_trunc(signal.get('description'), 140)}"
                    )
            # ASA (Autonomous Security Analyst) verdict is feature-gated -
            # degrade gracefully when the deployment does not expose it.
            asa_resp, asa_err = await safe_request(
                client,
                "get",
                f"{api}/v0/messages/{_path(mid)}/asa_verdict",
                _PROVIDER,
                f"get ASA verdict {mid}",
                headers=headers,
                expected_statuses={404},
            )
            if asa_err is None and asa_resp is not None and asa_resp.is_success:
                asa = asa_resp.json() or {}
                if asa.get("verdict"):
                    lines.append(f"ASA verdict: {asa['verdict']}")
        return "\n".join(lines)

    async def get_sublime_task(task_id: str) -> str:
        """Check the status of an asynchronous Sublime action task."""
        tid = task_id.strip()
        if not tid:
            return "Error: task_id is required."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/tasks/{_path(tid)}",
                _PROVIDER,
                f"get task {tid}",
                headers=headers,
            )
            if err:
                return err
            task = resp.json() or {}
        line = (
            f"Task {task.get('id', tid)}: {task.get('state', '?')} "
            f"(created {_ts(task.get('created_at'))})"
        )
        if task.get("error"):
            line += f"\nError: {_trunc(task.get('error'), 300)}"
        return line

    async def act_on_sublime_message(action: str, message_id: str) -> str:
        """Apply a response action to ONE individual message."""
        act = action.lower().strip()
        if act not in _MESSAGE_ACTIONS:
            return f"Error: action must be one of {', '.join(_MESSAGE_ACTIONS)}."
        mid = message_id.strip()
        if not mid:
            return "Error: message_id is required."
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/v0/messages/{_path(mid)}/actions",
                _PROVIDER,
                f"{act} message {mid}",
                headers=headers,
                json={"action": act},
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
        summary = f"Action '{act}' submitted for message {mid}."
        if isinstance(data, dict) and data.get("task_id"):
            summary += (
                f" Runs asynchronously - track with "
                f"get_sublime_task(task_id='{data['task_id']}')."
            )
        return summary

    # ══════════════════════════════════════════════════════════════════════
    # 3. HUNTING (MQL hunt jobs)
    # ══════════════════════════════════════════════════════════════════════

    async def start_sublime_hunt(
        source: str,
        range_start_time: str,
        range_end_time: str,
        name: str = "",
        triage_flagged: bool = False,
        triage_reported: bool = False,
        triage_email_bomb: bool = False,
    ) -> str:
        """Start an MQL hunt job over a time range (polls briefly for results)."""
        if not source.strip():
            return "Error: source (the MQL query) is required."
        if not range_start_time.strip() or not range_end_time.strip():
            return (
                "Error: range_start_time and range_end_time are required "
                "(UTC ISO 8601, e.g. '2026-07-01T00:00:00Z')."
            )
        body: dict[str, Any] = {
            "source": source.strip(),
            "range_start_time": range_start_time.strip(),
            "range_end_time": range_end_time.strip(),
        }
        if name.strip():
            body["name"] = name.strip()
        if triage_flagged:
            body["triage_flagged"] = True
        if triage_reported:
            body["triage_reported"] = True
        if triage_email_bomb:
            body["triage_email_bomb"] = True
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/v0/hunt-jobs",
                _PROVIDER,
                "start hunt job",
                headers=headers,
                json=body,
            )
            if err:
                return err
            hunt_id = (resp.json() or {}).get("hunt_job_id")
            if not hunt_id:
                return "Error: Sublime did not return a hunt_job_id."
            status = ""
            for _ in range(_HUNT_MAX_POLLS):
                await asyncio.sleep(_HUNT_POLL_INTERVAL)
                st_resp, st_err = await safe_request(
                    client,
                    "get",
                    f"{api}/v0/hunt-jobs/{_path(hunt_id)}",
                    _PROVIDER,
                    "poll hunt job",
                    headers=headers,
                )
                if st_err:
                    return (
                        f"Hunt job {hunt_id} started but polling failed: {st_err} "
                        "Check it later with get_sublime_hunt_results."
                    )
                job = st_resp.json() or {}
                status = str(job.get("status", ""))
                if status in ("COMPLETED", "FAILED", "CANCELED"):
                    break
            if status == "FAILED":
                return (
                    f"Hunt job {hunt_id} FAILED: "
                    f"{_trunc((st_resp.json() or {}).get('error'), 300) or 'unknown error'}"
                )
            if status == "CANCELED":
                return f"Hunt job {hunt_id} was canceled."
            if status != "COMPLETED":
                return (
                    f"Hunt job {hunt_id} is still running (status "
                    f"{status or 'IN_PROGRESS'}). Fetch results later with "
                    f"get_sublime_hunt_results(hunt_job_id='{hunt_id}')."
                )
        result = await get_sublime_hunt_results(str(hunt_id))
        return f"Hunt job {hunt_id} COMPLETED.\n{result}"

    async def get_sublime_hunt_results(
        hunt_job_id: str, limit: int = 20, offset: int = 0
    ) -> str:
        """Get a hunt job's status and its matching message groups."""
        hid = hunt_job_id.strip()
        if not hid:
            return "Error: hunt_job_id is required."
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/hunt-jobs/{_path(hid)}",
                _PROVIDER,
                f"get hunt job {hid}",
                headers=headers,
            )
            if err:
                return err
            job = resp.json() or {}
            status = str(job.get("status", "?"))
            header = (
                f"Hunt job {hid} [{status}] over "
                f"{_ts(job.get('range_start_time'))} .. {_ts(job.get('range_end_time'))}\n"
                f"MQL: {_trunc(job.get('source'), 200)}"
            )
            if job.get("error"):
                header += f"\nError: {_trunc(job.get('error'), 300)}"
            if status != "COMPLETED":
                return header + "\nResults are available once the status is COMPLETED."
            params = {
                "limit": _clamp(limit, 20, 500),
                "offset": _offset(offset),
            }
            res_resp, res_err = await safe_request(
                client,
                "get",
                f"{api}/v0/hunt-jobs/{_path(hid)}/results",
                _PROVIDER,
                f"get hunt job results {hid}",
                headers=headers,
                params=params,
            )
            if res_err:
                return res_err
            data = res_resp.json() or {}
        groups = data.get("message_groups") or []
        total = data.get("total_group_count")
        out = format_entity_list(
            groups,
            _fmt_message_group,
            noun="matching message group",
            total=total if isinstance(total, int) else None,
            empty_msg="The hunt completed with no matching message groups.",
        )
        if job.get("results_truncated"):
            out += "\n[Results were truncated by the hunt results limit]"
        return header + "\n" + out + _group_list_footer(total, params["offset"], len(groups))

    # ══════════════════════════════════════════════════════════════════════
    # 4. DETECTION RULES (read)
    # ══════════════════════════════════════════════════════════════════════

    async def list_sublime_rules(
        search: str = "",
        in_feed: bool | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List detection/triage rules (case-insensitive search on name/MQL)."""
        params: dict[str, Any] = {
            "limit": _clamp(limit, 25, 500),
            "offset": _offset(offset),
        }
        if search.strip():
            params["search"] = search.strip()
        if in_feed is not None:
            params["in_feed"] = in_feed
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/rules",
                _PROVIDER,
                "list rules",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        rules = data.get("rules") or []
        total = data.get("total")
        out = format_entity_list(
            rules,
            _fmt_rule,
            noun="rule",
            total=total if isinstance(total, int) else None,
        )
        return out + _group_list_footer(total, params["offset"], len(rules))

    async def get_sublime_rule(rule_id: str) -> str:
        """Get one rule with its full MQL source and metadata."""
        rid = rule_id.strip()
        if not rid:
            return "Error: rule_id is required."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/rules/{_path(rid)}",
                _PROVIDER,
                f"get rule {rid}",
                headers=headers,
            )
            if err:
                return err
            rule = resp.json() or {}
        lines = [
            f"Rule: {_trunc(rule.get('name'), 120)} (id {rule.get('id', rid)})",
            f"State: {'active' if rule.get('active') else 'inactive'}"
            + (" (passive mode)" if rule.get("passive") else "")
            + f" | Severity: {rule.get('severity') or 'n/a'} | "
            f"Type: {rule.get('type') or '?'} | Maturity: {rule.get('maturity') or 'n/a'}",
            f"Created: {_ts(rule.get('created_at'))} | Updated: {_ts(rule.get('updated_at'))}",
        ]
        if rule.get("description"):
            lines.append(f"Description: {_trunc(rule.get('description'), 400)}")
        if rule.get("tags"):
            lines.append(f"Tags: {_trunc(', '.join(map(str, rule['tags'])), 200)}")
        if rule.get("attack_types"):
            lines.append(
                f"Attack types: {_trunc(', '.join(map(str, rule['attack_types'])), 200)}"
            )
        if rule.get("tactics_and_techniques"):
            lines.append(
                "Tactics and techniques: "
                f"{_trunc(', '.join(map(str, rule['tactics_and_techniques'])), 200)}"
            )
        if rule.get("false_positives"):
            lines.append(
                f"Known false positives: "
                f"{_trunc('; '.join(map(str, rule['false_positives'])), 300)}"
            )
        if rule.get("action_ids"):
            lines.append(f"Attached action ids: {', '.join(map(str, rule['action_ids']))}")
        if rule.get("source"):
            lines.append(f"\nMQL source:\n{str(rule['source'])[:2500]}")
        return "\n".join(lines)

    async def validate_sublime_rule(
        source: str, name: str = "Draft rule", type: str = "detection"
    ) -> str:
        """Validate MQL + rule fields without saving anything."""
        if not source.strip():
            return "Error: source (the MQL to validate) is required."
        body: dict[str, Any] = {
            "name": name.strip() or "Draft rule",
            "source": source.strip(),
        }
        if type.strip():
            body["type"] = type.strip()
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/v0/rules/validate",
                _PROVIDER,
                "validate rule",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json() or {}
        if data.get("validation_error"):
            return f"Rule is INVALID: {_trunc(data['validation_error'], 500)}"
        lines = ["Rule is valid."]
        if data.get("functions"):
            lines.append(f"Functions used: {', '.join(map(str, data['functions']))}")
        if data.get("list"):
            lines.append(f"Lists referenced: {', '.join(map(str, data['list']))}")
        if data.get("is_org_dependent"):
            lines.append(
                "Note: the rule depends on org-specific fields, lists, or functions."
            )
        return "\n".join(lines)

    def _rule_optional_fields(
        description: str,
        severity: str,
        tags: str,
        references: str,
        false_positives: str,
    ) -> dict[str, Any] | str:
        """Shared optional-field shaping for rule create/update. Returns an
        error string when a constrained value is invalid."""
        body: dict[str, Any] = {}
        if description.strip():
            body["description"] = description.strip()
        sev = severity.lower().strip()
        if sev:
            if sev not in _SEVERITIES:
                return f"Error: severity must be one of {', '.join(_SEVERITIES)}."
            body["severity"] = sev
        if tags.strip():
            body["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        if references.strip():
            body["references"] = [r.strip() for r in references.split(",") if r.strip()]
        if false_positives.strip():
            body["false_positives"] = [
                f.strip() for f in false_positives.split(";") if f.strip()
            ]
        return body

    async def create_sublime_rule(
        name: str,
        source: str,
        description: str = "",
        severity: str = "",
        type: str = "detection",
        active: bool = False,
        tags: str = "",
        references: str = "",
        false_positives: str = "",
    ) -> str:
        """Create a detection/DLP/triage rule (validate the MQL first)."""
        if not name.strip() or not source.strip():
            return "Error: name and source (MQL) are both required."
        optional = _rule_optional_fields(
            description, severity, tags, references, false_positives
        )
        if isinstance(optional, str):
            return optional
        body: dict[str, Any] = {
            "name": name.strip(),
            "source": source.strip(),
            "active": bool(active),
            **optional,
        }
        if type.strip():
            body["type"] = type.strip()
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/v0/rules",
                _PROVIDER,
                "create rule",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
        rule_id = data.get("rule_id") or data.get("id") or "?"
        state = "active" if body["active"] else "inactive (activate with manage_sublime_rule)"
        return f"Rule '{body['name']}' created with id {rule_id} - {state}."

    async def update_sublime_rule(
        rule_id: str,
        name: str,
        source: str,
        description: str = "",
        severity: str = "",
        tags: str = "",
        references: str = "",
        false_positives: str = "",
    ) -> str:
        """Update a rule to a new definition (PUT - name and source required)."""
        rid = rule_id.strip()
        if not rid:
            return "Error: rule_id is required."
        if not name.strip() or not source.strip():
            return (
                "Error: name and source (MQL) are both required - the update "
                "replaces the rule definition. Fetch current values with "
                "get_sublime_rule first."
            )
        optional = _rule_optional_fields(
            description, severity, tags, references, false_positives
        )
        if isinstance(optional, str):
            return optional
        body: dict[str, Any] = {
            "name": name.strip(),
            "source": source.strip(),
            **optional,
        }
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "put",
                f"{api}/v0/rules/{_path(rid)}",
                _PROVIDER,
                f"update rule {rid}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
        updated = data.get("name") or body["name"]
        return f"Rule '{updated}' ({rid}) updated."

    async def manage_sublime_rule(action: str, rule_id: str) -> str:
        """Activate / deactivate / permanently delete a rule."""
        act = action.lower().strip()
        if act not in _RULE_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_RULE_ACTIONS))}."
        rid = rule_id.strip()
        if not rid:
            return "Error: rule_id is required."
        async with _client(timeout=60) as client:
            if act == "delete":
                _, err = await safe_request(
                    client,
                    "delete",
                    f"{api}/v0/rules/{_path(rid)}",
                    _PROVIDER,
                    f"delete rule {rid}",
                    headers=headers,
                )
                if err:
                    return err
                return f"Rule {rid} permanently deleted. This cannot be undone."
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/v0/rules/{_path(rid)}/{act}",
                _PROVIDER,
                f"{act} rule {rid}",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
        name = data.get("name") if isinstance(data, dict) else None
        label = f"'{name}' ({rid})" if name else rid
        return f"Rule {label} {'activated' if act == 'activate' else 'deactivated'}."

    # ══════════════════════════════════════════════════════════════════════
    # 5. LISTS (threat intel, read)
    # ══════════════════════════════════════════════════════════════════════

    async def list_sublime_lists(entry_type: str = "string", name: str = "") -> str:
        """List Sublime lists (string / user_group / provider_org_unit)."""
        params: dict[str, Any] = {"entry_type": entry_type.strip() or "string"}
        if name.strip():
            params["name"] = name.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/lists",
                _PROVIDER,
                "list lists",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        lists = data.get("lists") or []

        def _fmt(item: dict) -> str:
            return (
                f"  {_trunc(item.get('name'), 60)} "
                f"[{'editable' if item.get('editable') else 'system'}] "
                f"(id {item.get('id', '?')})\n"
                f"    {_trunc(item.get('description'), 120)} | "
                f"Updated: {_ts(item.get('updated_at'))}"
            )

        return format_entity_list(
            lists,
            _fmt,
            noun="list",
            empty_msg=f"No {params['entry_type']} lists found.",
        )

    async def get_sublime_list(list_id: str, max_entries: int = 100) -> str:
        """Get one list's metadata and its entries."""
        lid = list_id.strip()
        if not lid:
            return "Error: list_id is required."
        capped = _clamp(max_entries, 100, 1000)
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/lists/{_path(lid)}",
                _PROVIDER,
                f"get list {lid}",
                headers=headers,
            )
            if err:
                return err
            info = resp.json() or {}
            entries_resp, entries_err = await safe_request(
                client,
                "get",
                f"{api}/v0/lists/{_path(lid)}/entries",
                _PROVIDER,
                f"get list entries {lid}",
                headers=headers,
            )
            if entries_err:
                return entries_err
            entries_data = entries_resp.json() or {}
        entries = entries_data.get("entries") or []
        lines = [
            f"List: {info.get('name', lid)} (id {info.get('id', lid)})",
            f"Type: {info.get('entry_type', '?')} | "
            f"Editable: {info.get('editable')} | Updated: {_ts(info.get('updated_at'))}",
            f"Description: {_trunc(info.get('description'), 200)}",
            f"Entries ({len(entries)}):",
        ]
        for entry in entries[:capped]:
            lines.append(f"  - {_trunc(entry, 120)}")
        if len(entries) > capped:
            lines.append(f"  ... and {len(entries) - capped} more (raise max_entries)")
        return "\n".join(lines)

    async def manage_sublime_list(
        action: str,
        list_id: str = "",
        name: str = "",
        description: str = "",
        entry: str = "",
        entries: str = "",
    ) -> str:
        """Manage lists: create/update/delete + add/delete/set entries."""
        act = action.lower().strip()
        if act not in _LIST_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_LIST_ACTIONS))}."
        lid = list_id.strip()
        if act != "create" and not lid:
            return f"Error: list_id is required for action '{act}'."

        async with _client(timeout=60) as client:
            if act == "create":
                if not name.strip() or not description.strip():
                    return "Error: name and description are both required to create a list."
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{api}/v0/lists",
                    _PROVIDER,
                    "create list",
                    headers=headers,
                    json={"name": name.strip(), "description": description.strip()},
                )
                if err:
                    return err
                data = resp.json() or {}
                return (
                    f"List '{data.get('name', name.strip())}' created with id "
                    f"{data.get('id', '?')}. Reference it in MQL by name."
                )

            if act == "update_description":
                if not description.strip():
                    return "Error: description is required for update_description."
                _, err = await safe_request(
                    client,
                    "patch",
                    f"{api}/v0/lists/{_path(lid)}",
                    _PROVIDER,
                    f"update list {lid}",
                    headers=headers,
                    json={"description": description.strip()},
                )
                if err:
                    return err
                return f"List {lid} description updated."

            if act == "delete":
                _, err = await safe_request(
                    client,
                    "delete",
                    f"{api}/v0/lists/{_path(lid)}",
                    _PROVIDER,
                    f"delete list {lid}",
                    headers=headers,
                )
                if err:
                    return err
                return (
                    f"List {lid} permanently deleted. This cannot be undone - "
                    "rules referencing it by name will stop matching."
                )

            if act == "add_entry":
                value = entry.strip()
                if not value:
                    return "Error: entry is required for add_entry."
                if len(value) > 2000:
                    return "Error: a list entry may be at most 2000 characters."
                _, err = await safe_request(
                    client,
                    "post",
                    f"{api}/v0/lists/{_path(lid)}/entries/entry",
                    _PROVIDER,
                    f"add entry to list {lid}",
                    headers=headers,
                    json={"string": value},
                )
                if err:
                    return err
                return f"Entry '{_trunc(value, 120)}' added to list {lid} (if not already present)."

            if act == "delete_entry":
                value = entry.strip()
                if not value:
                    return "Error: entry is required for delete_entry."
                _, err = await safe_request(
                    client,
                    "delete",
                    f"{api}/v0/lists/{_path(lid)}/entries/entry",
                    _PROVIDER,
                    f"delete entry from list {lid}",
                    headers=headers,
                    params={"string": value},
                )
                if err:
                    return err
                return f"Entry '{_trunc(value, 120)}' deleted from list {lid} (if it was present)."

            # set_entries (destructive overwrite)
            values = [line.strip() for line in entries.splitlines() if line.strip()]
            if not values:
                return (
                    "Error: entries is required for set_entries (one value per "
                    "line). This OVERWRITES all existing entries - use "
                    "add_entry to append instead."
                )
            if any(len(v) > 2000 for v in values):
                return "Error: a list entry may be at most 2000 characters."
            _, err = await safe_request(
                client,
                "put",
                f"{api}/v0/lists/{_path(lid)}/entries",
                _PROVIDER,
                f"set entries of list {lid}",
                headers=headers,
                json={"entries": values},
            )
            if err:
                return err
            return (
                f"List {lid} entries OVERWRITTEN with {len(values)} value(s) - "
                "previous entries were replaced."
            )

    # ══════════════════════════════════════════════════════════════════════
    # 6. VISIBILITY + ENRICHMENT
    # ══════════════════════════════════════════════════════════════════════

    async def list_sublime_user_reports(
        reported_after: str = "", reported_before: str = "", limit: int = 25
    ) -> str:
        """List user phishing reports (who reported what, when, via which channel)."""
        params: dict[str, Any] = {"limit": _clamp(limit, 25, 500)}
        if reported_after.strip():
            params["reported_at[gte]"] = reported_after.strip()
        if reported_before.strip():
            params["reported_at[lt]"] = reported_before.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/user-reports",
                _PROVIDER,
                "list user reports",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        reports = data.get("user_reports") or []

        def _fmt(report: dict) -> str:
            return (
                f"  {_ts(report.get('reported_at'))} {report.get('reporter', '?')} "
                f"via {report.get('channel', '?')}\n"
                f"    Reported group: {report.get('reported_message_canonical_id', '?')}"
            )

        return format_entity_list(
            reports,
            _fmt,
            noun="user report",
            total=data.get("count") if isinstance(data.get("count"), int) else None,
            empty_msg="No user reports in the selected window.",
        )

    async def list_sublime_mailboxes(
        search: str = "",
        active: bool | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List protected mailboxes (inventory + subscription health)."""
        params: dict[str, Any] = {
            "limit": _clamp(limit, 25, 500),
            "offset": _offset(offset),
        }
        if search.strip():
            params["search"] = search.strip()
        if active is not None:
            params["active"] = active
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/mailboxes",
                _PROVIDER,
                "list mailboxes",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        mailboxes = data.get("mailboxes") or []
        total = data.get("total")

        def _fmt(mbx: dict) -> str:
            line = (
                f"  {mbx.get('email_address', '?')} "
                f"[{'active' if mbx.get('active') else 'inactive'}] "
                f"(id {mbx.get('id', '?')})"
            )
            if mbx.get("subscription_error_status"):
                line += f"\n    Subscription error: {mbx['subscription_error_status']}"
            return line

        out = format_entity_list(
            mailboxes,
            _fmt,
            noun="mailbox",
            total=total if isinstance(total, int) else None,
        )
        return out + _group_list_footer(total, params["offset"], len(mailboxes))

    async def list_sublime_email_bombs(
        active: bool | None = None,
        dismissed: bool | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """List detected email bombs (mailbox flooding campaigns)."""
        params: dict[str, Any] = {
            "limit": _clamp(limit, 20, 500),
            "offset": _offset(offset),
        }
        if active is not None:
            params["active"] = active
        if dismissed is not None:
            params["dismissed"] = dismissed
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/messages/bombs",
                _PROVIDER,
                "list email bombs",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        bombs = data.get("email_bombs") or []
        total = data.get("total")

        def _fmt(bomb: dict) -> str:
            mailbox = bomb.get("mailbox") or {}
            reviewed = "reviewed" if bomb.get("reviewed_at") else "NOT reviewed"
            return (
                f"  {bomb.get('id', '?')} on {mailbox.get('email', '?')} "
                f"[{'ACTIVE' if bomb.get('active') else 'inactive'}, {reviewed}]\n"
                f"    {bomb.get('message_count', '?')} messages from "
                f"{_ts(bomb.get('start_time'))} to {_ts(bomb.get('end_time'))}"
            )

        out = format_entity_list(
            bombs,
            _fmt,
            noun="email bomb",
            total=total if isinstance(total, int) else None,
            empty_msg="No email bombs matched the filters.",
        )
        return out + _group_list_footer(total, params["offset"], len(bombs))

    async def dismiss_sublime_email_bomb(bomb_id: str, review_comment: str = "") -> str:
        """Dismiss an email bomb, marking it as reviewed."""
        bid = bomb_id.strip()
        if not bid:
            return "Error: bomb_id is required."
        body: dict[str, Any] = {}
        if review_comment.strip():
            body["review_comment"] = review_comment.strip()
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{api}/v0/messages/bombs/{_path(bid)}/dismiss",
                _PROVIDER,
                f"dismiss email bomb {bid}",
                headers=headers,
                json=body,
            )
            if err:
                return err
        return f"Email bomb {bid} dismissed (marked as reviewed)."

    async def list_sublime_audit_events(
        event_type: str = "",
        created_after: str = "",
        created_before: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List platform audit log events (who did what, when)."""
        params: dict[str, Any] = {
            "limit": _clamp(limit, 25, 500),
            "offset": _offset(offset),
        }
        if event_type.strip():
            params["type"] = event_type.strip()
        if created_after.strip():
            params["created_at[gte]"] = created_after.strip()
        if created_before.strip():
            params["created_at[lt]"] = created_before.strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/v0/audit-log/events",
                _PROVIDER,
                "list audit events",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        events = data.get("events") or []
        total = data.get("total")

        def _fmt(event: dict) -> str:
            user = event.get("created_by") or {}
            who = user.get("email_address") or "system"
            request = (event.get("data") or {}).get("request") or {}
            line = f"  {_ts(event.get('created_at'))} [{event.get('type', '?')}] by {who}"
            if request.get("path"):
                line += f"\n    {request.get('method', '?')} {_trunc(request.get('path'), 120)}"
            return line

        out = format_entity_list(
            events,
            _fmt,
            noun="audit event",
            total=total if isinstance(total, int) else None,
            empty_msg="No audit events matched the filters.",
        )
        return out + _group_list_footer(total, params["offset"], len(events))

    async def analyze_sublime_link(url: str, no_logo_detect: bool = False) -> str:
        """Analyze a URL with Sublime's ml.link_analysis (redirects, brand logos)."""
        if not url.strip():
            return "Error: url is required."
        body: dict[str, Any] = {"url": url.strip()}
        if no_logo_detect:
            body["no_logo_detect"] = True
        async with _client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/v0/enrichment/link_analysis/evaluate",
                _PROVIDER,
                "analyze link",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json() or {}
        lines = [f"Link analysis for {url.strip()}:"]
        effective = data.get("effective_url")
        if isinstance(effective, dict):
            lines.append(f"Effective URL: {_trunc(effective.get('url'), 200)}")
        elif effective:
            lines.append(f"Effective URL: {_trunc(effective, 200)}")
        for key in ("status_code", "title", "error"):
            if data.get(key) not in (None, ""):
                lines.append(f"{key.replace('_', ' ').capitalize()}: {_trunc(data[key], 200)}")
        redirects = data.get("redirect_history") or data.get("redirects") or []
        if isinstance(redirects, list) and redirects:
            lines.append(f"Redirect chain ({len(redirects)} hops):")
            for hop in redirects[:8]:
                hop_url = hop.get("url") if isinstance(hop, dict) else hop
                if isinstance(hop_url, dict):
                    hop_url = hop_url.get("url")
                lines.append(f"  -> {_trunc(hop_url, 150)}")
        logos = data.get("logo_detections") or data.get("logos") or []
        if isinstance(logos, list) and logos:
            names = [
                str(logo.get("name", logo)) if isinstance(logo, dict) else str(logo)
                for logo in logos[:6]
            ]
            lines.append(f"Brand logos detected: {', '.join(names)}")
        if len(lines) == 1:
            lines.append(f"Raw response: {_trunc(str(data), 800)}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_sublime_status", get_sublime_status)
    executor.register("list_sublime_message_groups", list_sublime_message_groups)
    executor.register("search_sublime_message_groups", search_sublime_message_groups)
    executor.register("get_sublime_message_group", get_sublime_message_group)
    executor.register("review_sublime_message_groups", review_sublime_message_groups)
    executor.register("manage_sublime_message_group", manage_sublime_message_group)
    executor.register("get_sublime_message", get_sublime_message)
    executor.register("get_sublime_message_eml", get_sublime_message_eml)
    executor.register("get_sublime_message_attack_score", get_sublime_message_attack_score)
    executor.register("act_on_sublime_message", act_on_sublime_message)
    executor.register("get_sublime_task", get_sublime_task)
    executor.register("start_sublime_hunt", start_sublime_hunt)
    executor.register("get_sublime_hunt_results", get_sublime_hunt_results)
    executor.register("list_sublime_rules", list_sublime_rules)
    executor.register("get_sublime_rule", get_sublime_rule)
    executor.register("validate_sublime_rule", validate_sublime_rule)
    executor.register("create_sublime_rule", create_sublime_rule)
    executor.register("update_sublime_rule", update_sublime_rule)
    executor.register("manage_sublime_rule", manage_sublime_rule)
    executor.register("list_sublime_lists", list_sublime_lists)
    executor.register("get_sublime_list", get_sublime_list)
    executor.register("manage_sublime_list", manage_sublime_list)
    executor.register("list_sublime_user_reports", list_sublime_user_reports)
    executor.register("list_sublime_mailboxes", list_sublime_mailboxes)
    executor.register("list_sublime_email_bombs", list_sublime_email_bombs)
    executor.register("dismiss_sublime_email_bomb", dismiss_sublime_email_bomb)
    executor.register("list_sublime_audit_events", list_sublime_audit_events)
    executor.register("analyze_sublime_link", analyze_sublime_link)

    return [
        ToolDefinition(
            name="get_sublime_status",
            description=(
                "Validate the Sublime Security connection and API key. Use "
                "first if other Sublime tools fail with authentication errors."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_sublime_message_groups",
            description=(
                "List Sublime Security message groups (the email triage queue) "
                "with filters: flagged (matched a detection rule), reviewed, "
                "user_reported, sender/recipient/mailbox email, sender domain, "
                "exact subject, flagged rule severity, attack score verdict, "
                "and a created-at time range. Returns canonical group ids for "
                "follow-up calls (get_sublime_message_group, review/actions). "
                "Start with flagged=true, reviewed=false for the open queue."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "flagged": {
                        "type": "boolean",
                        "description": "Only groups with at least one flagged message",
                    },
                    "reviewed": {
                        "type": "boolean",
                        "description": "Only groups that have (true) or have not (false) been reviewed",
                    },
                    "user_reported": {
                        "type": "boolean",
                        "description": "Only groups with at least one user phishing report",
                    },
                    "sender_email": {
                        "type": "string",
                        "description": "Exact sender email, e.g. 'attacker@evil.example'",
                    },
                    "sender_domain": {
                        "type": "string",
                        "description": "Exact sender domain, e.g. 'evil.example'",
                    },
                    "recipient_email": {"type": "string", "description": "Exact recipient email"},
                    "mailbox_email": {"type": "string", "description": "Exact mailbox email"},
                    "subject": {"type": "string", "description": "Exact message subject"},
                    "flagged_rule_severity": {
                        "type": "string",
                        "enum": list(_SEVERITIES),
                        "description": "Only groups flagged by rules of this severity",
                    },
                    "attack_score_verdict": {
                        "type": "string",
                        "enum": [
                            "unknown",
                            "likely_benign",
                            "suspicious",
                            "malicious",
                            "graymail",
                            "spam",
                        ],
                        "description": "Only groups with this attack score verdict",
                    },
                    "created_after": {
                        "type": "string",
                        "description": "Inclusive start, UTC ISO 8601 (e.g. '2026-07-01T00:00:00Z')",
                    },
                    "created_before": {
                        "type": "string",
                        "description": "Exclusive end, UTC ISO 8601",
                    },
                    "limit": {"type": "integer", "default": 20, "description": "Max 500"},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_sublime_message_groups",
            description=(
                "Search all Sublime message groups by free text (query searches "
                "every field), attachment hash (MD5 32 chars / SHA256 64 "
                "chars), or attachment filename (case-insensitive wildcard). "
                "Use for IOC sweeps like 'which mailboxes received this "
                "attachment?'. query is not combinable with the other fields."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search across all fields (use alone)",
                    },
                    "attachment_md5": {
                        "type": "string",
                        "description": "Attachment MD5 (32 hex chars)",
                    },
                    "attachment_sha256": {
                        "type": "string",
                        "description": "Attachment SHA256 (64 hex chars)",
                    },
                    "file_name": {
                        "type": "string",
                        "description": "Attachment filename (wildcard match)",
                    },
                    "created_after": {
                        "type": "string",
                        "description": "Inclusive start, UTC ISO 8601",
                    },
                    "created_before": {
                        "type": "string",
                        "description": "Exclusive end, UTC ISO 8601",
                    },
                    "page_token": {
                        "type": "string",
                        "description": "Pagination token returned by the previous call",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sublime_message_group",
            description=(
                "Get one Sublime message group by canonical id: state, "
                "classification, review status, flagged rules with tags, all "
                "message previews (sender, mailbox, delivered/read/replied), "
                "user reports, and clicked links. The core triage detail view."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_id": {
                        "type": "string",
                        "description": "Canonical message group id",
                    },
                },
                "required": ["group_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="review_sublime_message_groups",
            description=(
                "Review one or more Sublime message groups in bulk: set the "
                "classification (malicious, benign, spam, graymail, "
                "simulation, unwanted, violation, non-violation, or skip) and "
                "optionally apply a response action (trash, quarantine, "
                "restore, warning_banner, move_to_spam, move_to_graymail, "
                "delete_calendar_events) to ALL messages in the groups, "
                "including future ones. Actions run asynchronously - a "
                "task_id is returned for get_sublime_task."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "message_group_ids": {
                        "type": "string",
                        "description": "Comma-separated canonical message group ids",
                    },
                    "classification": {
                        "type": "string",
                        "enum": [*list(_CLASSIFICATIONS), "skip"],
                        "description": "Classification to record ('skip' = act without classifying)",
                    },
                    "action": {
                        "type": "string",
                        "enum": list(_MESSAGE_ACTIONS),
                        "description": "Optional response action to apply to every message",
                    },
                    "review_comment": {
                        "type": "string",
                        "description": "Optional comment describing the reason",
                    },
                },
                "required": ["message_group_ids", "classification"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_sublime_message_group",
            description=(
                "Apply a remediation to ONE Sublime message group: 'trash' "
                "(O365 -> Recoverable Items, Google -> Trash label; also "
                "auto-trashes future messages in the group), 'quarantine', "
                "'restore' (undo trash/quarantine and stop automatic "
                "actions), or 'dismiss' (mark handled without mailbox "
                "changes). Optionally record a classification, report label, "
                "and review comment. Affects every message in the group - "
                "use act_on_sublime_message for a single message."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_GROUP_ACTIONS),
                    },
                    "group_id": {
                        "type": "string",
                        "description": "Canonical message group id",
                    },
                    "classification": {
                        "type": "string",
                        "enum": list(_CLASSIFICATIONS),
                        "description": "Optional classification to record with the action",
                    },
                    "report_label": {
                        "type": "string",
                        "enum": list(_REPORT_LABELS),
                        "description": "Optional label to apply to the reviewed messages",
                    },
                    "review_comment": {
                        "type": "string",
                        "description": "Optional comment describing the reason",
                    },
                },
                "required": ["action", "group_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_sublime_message",
            description=(
                "Get one Sublime message's metadata by message id: subject, "
                "sender, recipients, mailbox, canonical group id, spam "
                "placement, and read/replied/forwarded state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "Sublime message id (UUID)"},
                },
                "required": ["message_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sublime_message_eml",
            description=(
                "Fetch the raw EML (full headers + body) of a Sublime message "
                "for phishing analysis - header inspection, URL extraction, "
                "authentication results. Output is truncated to max_chars."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "Sublime message id (UUID)"},
                    "max_chars": {
                        "type": "integer",
                        "default": 4000,
                        "description": "Max characters of EML to return (up to 20000)",
                    },
                },
                "required": ["message_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sublime_message_attack_score",
            description=(
                "Get Sublime's AI verdict for a message: Attack Score (0-100, "
                "higher = more likely an attack), graymail score, the top "
                "contributing signals, and the ASA (Autonomous Security "
                "Analyst) verdict when available. Use during triage to "
                "prioritize."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "Sublime message id (UUID)"},
                },
                "required": ["message_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="act_on_sublime_message",
            description=(
                "Apply a response action to ONE individual Sublime message "
                "(not the whole group): trash, quarantine, restore, "
                "warning_banner, move_to_spam, move_to_graymail, or "
                "delete_calendar_events. Runs asynchronously - returns a "
                "task_id for get_sublime_task. Use "
                "manage_sublime_message_group to act on every message in a "
                "group at once."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(_MESSAGE_ACTIONS),
                    },
                    "message_id": {
                        "type": "string",
                        "description": "Sublime message id (UUID)",
                    },
                },
                "required": ["action", "message_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_sublime_task",
            description=(
                "Check an asynchronous Sublime action task by id (returned by "
                "review/trash/quarantine/message actions). States: pending, "
                "started, succeeded, failed, retrying."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task id (UUID)"},
                },
                "required": ["task_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="start_sublime_hunt",
            description=(
                "Start a Sublime hunt job: run an MQL (Message Query Language) "
                "query against all processed mail in a UTC time range, e.g. "
                "\"sender.email.domain.domain == 'evil.example'\". Optional "
                "triage pre-filters restrict to flagged / user-reported / "
                "email-bomb messages (OR-combined). Polls briefly and returns "
                "results if the hunt finishes quickly; otherwise returns the "
                "hunt_job_id to check with get_sublime_hunt_results."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "MQL source, e.g. \"any(body.links, .href_url.domain.root_domain == 'evil.example')\"",
                    },
                    "range_start_time": {
                        "type": "string",
                        "description": "Inclusive start, UTC ISO 8601 (e.g. '2026-06-01T00:00:00Z')",
                    },
                    "range_end_time": {
                        "type": "string",
                        "description": "Exclusive end, UTC ISO 8601 (e.g. '2026-07-01T00:00:00Z')",
                    },
                    "name": {"type": "string", "description": "Optional hunt job name"},
                    "triage_flagged": {
                        "type": "boolean",
                        "default": False,
                        "description": "Pre-filter to flagged messages only",
                    },
                    "triage_reported": {
                        "type": "boolean",
                        "default": False,
                        "description": "Pre-filter to user-reported messages only",
                    },
                    "triage_email_bomb": {
                        "type": "boolean",
                        "default": False,
                        "description": "Pre-filter to email-bomb messages only",
                    },
                },
                "required": ["source", "range_start_time", "range_end_time"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sublime_hunt_results",
            description=(
                "Get a Sublime hunt job's status (IN_PROGRESS / COMPLETED / "
                "FAILED / CANCELED) and, once completed, its matching message "
                "groups with pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hunt_job_id": {"type": "string", "description": "Hunt job id"},
                    "limit": {"type": "integer", "default": 20, "description": "Max 500"},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["hunt_job_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_sublime_rules",
            description=(
                "List Sublime detection/triage rules with a case-insensitive "
                "substring search across rule name, description, and MQL "
                "source. in_feed filters to rules that are (or are not) "
                "managed by a rule feed. Returns rule ids for get_sublime_rule."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Substring search across name/description/MQL",
                    },
                    "in_feed": {
                        "type": "boolean",
                        "description": "Restrict to rules that are (true) / are not (false) in a feed",
                    },
                    "limit": {"type": "integer", "default": 25, "description": "Max 500"},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sublime_rule",
            description=(
                "Get one Sublime rule by id: full MQL source, active/passive "
                "state, severity, type (detection/dlp/triage), tags, attack "
                "types, tactics, known false positives, and attached actions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string", "description": "Rule id (UUID)"},
                },
                "required": ["rule_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="validate_sublime_rule",
            description=(
                "Validate MQL and rule fields WITHOUT saving anything. Returns "
                "the validation error if invalid, plus the functions and lists "
                "the rule references. Always run before create_sublime_rule or "
                "update_sublime_rule."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "MQL source to validate"},
                    "name": {
                        "type": "string",
                        "default": "Draft rule",
                        "description": "Rule name (used only for validation)",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["detection", "dlp", "triage"],
                        "default": "detection",
                    },
                },
                "required": ["source"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_sublime_rule",
            description=(
                "Create a Sublime rule from MQL. Types: 'detection' (flags "
                "matching inbound mail), 'dlp', or 'triage'. Rules are "
                "created INACTIVE unless active=true - validate the MQL with "
                "validate_sublime_rule first, then activate with "
                "manage_sublime_rule once tested."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Rule name"},
                    "source": {"type": "string", "description": "MQL source"},
                    "description": {"type": "string", "description": "What the rule detects and why"},
                    "severity": {"type": "string", "enum": list(_SEVERITIES)},
                    "type": {
                        "type": "string",
                        "enum": ["detection", "dlp", "triage"],
                        "default": "detection",
                    },
                    "active": {
                        "type": "boolean",
                        "default": False,
                        "description": "Activate immediately (default false - review first)",
                    },
                    "tags": {"type": "string", "description": "Comma-separated freeform tags"},
                    "references": {
                        "type": "string",
                        "description": "Comma-separated reference URLs",
                    },
                    "false_positives": {
                        "type": "string",
                        "description": "Known false-positive descriptions, ';'-separated",
                    },
                },
                "required": ["name", "source"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_sublime_rule",
            description=(
                "Update a Sublime rule to a new definition (full replace - "
                "name and source are required; fetch the current values with "
                "get_sublime_rule first, then send the modified definition). "
                "Validate changed MQL with validate_sublime_rule before "
                "updating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string", "description": "Rule id (UUID)"},
                    "name": {"type": "string", "description": "Rule name (required by the API)"},
                    "source": {"type": "string", "description": "MQL source (required by the API)"},
                    "description": {"type": "string"},
                    "severity": {"type": "string", "enum": list(_SEVERITIES)},
                    "tags": {"type": "string", "description": "Comma-separated freeform tags"},
                    "references": {
                        "type": "string",
                        "description": "Comma-separated reference URLs",
                    },
                    "false_positives": {
                        "type": "string",
                        "description": "Known false-positive descriptions, ';'-separated",
                    },
                },
                "required": ["rule_id", "name", "source"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_sublime_rule",
            description=(
                "Rule lifecycle: 'activate' (starts flagging matching "
                "messages), 'deactivate' (stops flagging), or 'delete' "
                "(PERMANENT, cannot be undone - prefer deactivate to keep "
                "history)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_RULE_ACTIONS),
                    },
                    "rule_id": {"type": "string", "description": "Rule id (UUID)"},
                },
                "required": ["action", "rule_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_sublime_lists",
            description=(
                "List Sublime lists by entry type. 'string' lists hold "
                "threat-intel values referenced from MQL (e.g. malicious "
                "attachment hashes, VIP addresses); 'user_group' and "
                "'provider_org_unit' lists mirror directory groups. Returns "
                "list ids for get_sublime_list / manage_sublime_list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entry_type": {
                        "type": "string",
                        "enum": ["string", "user_group", "provider_org_unit"],
                        "default": "string",
                    },
                    "name": {"type": "string", "description": "Exact list name filter"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_sublime_list",
            description=(
                "Get one Sublime list by id with its metadata and entries "
                "(capped at max_entries)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "list_id": {"type": "string", "description": "List id (UUID)"},
                    "max_entries": {
                        "type": "integer",
                        "default": 100,
                        "description": "Max entries to display (up to 1000)",
                    },
                },
                "required": ["list_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_sublime_list",
            description=(
                "Manage Sublime threat-intel lists (referenced from MQL by "
                "name). Actions: 'create' (name + description required), "
                "'update_description', 'add_entry' (idempotent append, e.g. "
                "a malicious attachment SHA256), 'delete_entry', "
                "'set_entries' (OVERWRITES all entries with the provided "
                "newline-separated values), and 'delete' (PERMANENT list "
                "deletion - rules referencing it stop matching). System "
                "lists (editable=false) reject writes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_LIST_ACTIONS),
                    },
                    "list_id": {
                        "type": "string",
                        "description": "List id (UUID) - required for every action except create",
                    },
                    "name": {
                        "type": "string",
                        "description": "New list name (create) - used to reference the list in MQL",
                    },
                    "description": {
                        "type": "string",
                        "description": "List description (create / update_description)",
                    },
                    "entry": {
                        "type": "string",
                        "description": "Single entry value, max 2000 chars (add_entry / delete_entry)",
                    },
                    "entries": {
                        "type": "string",
                        "description": (
                            "One value per line, max 2000 chars each "
                            "(set_entries - replaces ALL entries)"
                        ),
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_sublime_user_reports",
            description=(
                "List user phishing reports: reporter, channel, time, and the "
                "canonical id of the reported message group (feed into "
                "get_sublime_message_group). Filter by a reported-at window."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "reported_after": {
                        "type": "string",
                        "description": "Inclusive start, UTC ISO 8601",
                    },
                    "reported_before": {
                        "type": "string",
                        "description": "Exclusive end, UTC ISO 8601",
                    },
                    "limit": {"type": "integer", "default": 25, "description": "Max 500"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_sublime_mailboxes",
            description=(
                "List the mailboxes Sublime protects, with active state and "
                "provider-subscription errors. Search across names and email "
                "addresses."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Search across mailbox names and email addresses",
                    },
                    "active": {
                        "type": "boolean",
                        "description": "Only effectively active (true) or inactive (false) mailboxes",
                    },
                    "limit": {"type": "integer", "default": 25, "description": "Max 500"},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_sublime_email_bombs",
            description=(
                "List detected email bombs (mailbox flooding campaigns used to "
                "bury fraud confirmations): target mailbox, message count, "
                "time window, active state, and review state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "active": {
                        "type": "boolean",
                        "description": "Only currently active (true) or inactive (false) bombs",
                    },
                    "dismissed": {
                        "type": "boolean",
                        "description": "Only dismissed (true) or not-yet-dismissed (false) bombs",
                    },
                    "limit": {"type": "integer", "default": 20, "description": "Max 500"},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="dismiss_sublime_email_bomb",
            description=(
                "Dismiss a Sublime email bomb, marking it as reviewed by an "
                "analyst. Optionally record a comment explaining the "
                "decision. Get bomb ids from list_sublime_email_bombs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "bomb_id": {"type": "string", "description": "Email bomb id (UUID)"},
                    "review_comment": {
                        "type": "string",
                        "description": "Optional comment describing the reason",
                    },
                },
                "required": ["bomb_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_sublime_audit_events",
            description=(
                "List Sublime platform audit log events: event type, actor, "
                "time, and originating API request. Use to answer 'who "
                "trashed/reviewed/changed what?'. Platform deployments retain "
                "7 days; Cloud/Enterprise retain everything."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "event_type": {"type": "string", "description": "Filter by event type"},
                    "created_after": {
                        "type": "string",
                        "description": "Inclusive start, UTC ISO 8601",
                    },
                    "created_before": {
                        "type": "string",
                        "description": "Exclusive end, UTC ISO 8601",
                    },
                    "limit": {"type": "integer", "default": 25, "description": "Max 500"},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="analyze_sublime_link",
            description=(
                "Analyze a URL with Sublime's ml.link_analysis enrichment: "
                "follows rewrites/redirects to the effective URL and detects "
                "impersonated brand logos on the landing page. Use to verify "
                "suspicious links found in messages."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to analyze"},
                    "no_logo_detect": {
                        "type": "boolean",
                        "default": False,
                        "description": "Skip brand logo detection (faster)",
                    },
                },
                "required": ["url"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]

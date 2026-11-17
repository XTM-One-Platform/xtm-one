"""What every Microsoft tool shares: Graph constants, the session context and parsing helpers."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from app.integrations.http import AutoRefreshClient, safe_request
from app.tools.base import FileContext

_PROVIDER = "Microsoft"
_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_API = _BASE_URL

# Max bytes moved by the file transfer tools (download to platform file /
# upload to OneDrive/SharePoint).  Well under Graph's 250 MB simple-PUT cap.
_MS_MAX_FILE_BYTES = 100 * 1024 * 1024

# Max concurrent Microsoft Graph API requests per agent tool session.
# Limiting concurrency prevents a burst of parallel tool calls from all
# hitting rate limits simultaneously and cascading into a retry storm that
# exhausts the DB and storage connection pools.
_MICROSOFT_CONCURRENCY = 5

# Unsubstituted template-style tokens ({{team_id}}, <team_id>, [team_id], and
# the HTML-escaped &lt;team_id&gt; form) that an LLM emits verbatim when a
# prompt variable was never bound.  Real Teams team IDs are GUIDs and channel
# IDs look like ``19:...@thread.tacv2`` — neither can match these anchored
# patterns.  Same detection as the GitHub login guard.
_TEAMS_TEMPLATE_TOKEN_RE = re.compile(r"^\{\{.+\}\}$|^<[^>]+>$|^\[[^\]]+\]$|^&lt;.+&gt;$")


def _is_placeholder_id(value: str, placeholders: frozenset[str]) -> bool:
    """True when ``value`` is empty, a known placeholder word, or a template token."""
    return (
        not value
        or value.lower() in placeholders
        or bool(_TEAMS_TEMPLATE_TOKEN_RE.match(value))
    )


def _parse_iso_datetime(value: object) -> datetime:
    """Parse an ISO 8601 datetime string and normalize to UTC.

    Accepts the trailing ``Z`` shorthand (``2026-05-06T22:33:00Z``) as well
    as the verbose ``+HH:MM`` offset that the LLM may emit. Naive inputs
    are interpreted as UTC. Raises ``ValueError`` with a tool-readable
    message on malformed inputs (including non-string inputs like ``None``,
    numbers, or objects) so the caller can surface it as a tool error
    instead of letting an ``AttributeError`` from ``.strip()`` bubble up as
    a generic executor exception.
    """
    if not isinstance(value, str):
        raise ValueError(
            f"datetime value must be an ISO 8601 string, got {type(value).__name__}."
        )
    raw = value.strip()
    if not raw:
        raise ValueError("datetime value must be a non-empty ISO 8601 string.")
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(
            f"Invalid ISO 8601 datetime '{value}': expected format "
            f"like '2026-05-06T22:33:00Z' or '2026-05-06T22:33:00+02:00'."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _strip_html(text: str | None) -> str:
    """Strip HTML tags from *text* and collapse whitespace."""
    if not text:
        return ""
    if "<" in text and ">" in text:
        text = re.sub(r"<[^>]+>", "", text).strip()
        text = re.sub(r"\s+", " ", text)
    return text


def _parse_graph_timestamp(value: str | None) -> datetime | None:
    """Parse a Graph ISO-8601 timestamp (``...Z`` suffix, optional fractional
    seconds) into an aware :class:`datetime`, or ``None`` when missing/invalid.

    Graph timestamps are not lexicographically comparable across formats — a
    fractional-seconds value like ``10:00:00.153Z`` string-compares *less than*
    the plain ``10:00:00Z`` cutoff (``"." < "Z"``), so time-window filters must
    compare parsed datetimes, never raw strings.

    :func:`datetime.fromisoformat` on Python 3.11+ accepts Graph's 7-digit
    fractional seconds (``.0000000Z``) directly, truncating to microseconds.
    A value without any UTC designator parses to a *naive* datetime, which
    would raise ``TypeError`` when compared against an aware cutoff — assume
    UTC in that case (Graph emits UTC everywhere).
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _coerce_int(value: Any, default: int) -> int:
    """Best-effort int coercion for LLM-provided tool arguments.

    ``ToolCall.arguments`` are not type-coerced by ``CallableToolExecutor``,
    so an integer parameter can arrive as a string (``"20"``) or ``None``.
    Passing such a value straight into ``min()`` / ``max()`` raises
    ``TypeError``, failing the whole tool call and wasting an LLM iteration —
    coerce defensively and fall back to *default* for anything unusable.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


async def _resolve_platform_file(
    file_id: str, file_context: FileContext | None
) -> tuple[tuple[bytes, str, str] | None, str | None]:
    """Resolve a platform ``file_id`` to bytes. Returns ``(resolved, error)``."""
    if file_context is None:
        return None, (
            "Error: no file context available — files can only be "
            "forwarded from a conversation or assignment run."
        )
    resolved = await file_context.resolve_upload(file_id)
    if resolved is None:
        return None, (
            f"Error: file '{file_id}' was not found in this conversation. "
            "Use a file_id from an attached or generated file."
        )
    data = resolved[0]
    if len(data) > _MS_MAX_FILE_BYTES:
        return None, (
            f"Error: file is {round(len(data) / 1024 / 1024, 1)} MB, over "
            f"the {_MS_MAX_FILE_BYTES // 1024 // 1024} MB upload limit."
        )
    return resolved, None


class MicrosoftContext:
    """What every Microsoft tool of one tool session shares.

    One context is built per ``build_tools`` call and handed to each domain's
    ``register``: the bearer headers and the refresh hook behind every
    :class:`AutoRefreshClient`, the callbacks ``integration_tools`` injects,
    and one semaphore that caps the in-flight Graph requests of the session
    at :data:`_MICROSOFT_CONCURRENCY`. A burst of parallel tool calls (e.g.
    20 concurrent ``update_microsoft_task``) would otherwise hit the rate
    limit together and retry in lockstep, exhausting the DB and storage
    connection pools.
    """

    def __init__(
        self,
        token: str,
        refresh_fn: Callable[[], Awaitable[str | None]] | None = None,
        *,
        persist_file_fn: Callable[..., Awaitable[Any]] | None = None,
        integration_id: str | None = None,
        agent_id: str | None = None,
        sp_token_fn: Callable[[str], Awaitable[str | None]] | None = None,
        signature_fn: Callable[[str], Awaitable[Any]] | None = None,
        signature_plan: Any = None,
        send_origin: dict[str, str] | None = None,
        compose_style: str | None = None,
    ) -> None:
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.refresh_fn = refresh_fn
        # Keeps a downloaded file as a file of the conversation or run; None
        # outside such a scope (the tools then return inline FileRefs).
        self.persist_file_fn = persist_file_fn
        # The mailbox a send goes through, so the new_email poller of THIS
        # mailbox skips the agent's own mail. None records nothing.
        self.integration_id = integration_id
        # The agent whose run the tools act for: the scope of the mail send
        # guard (one answer per conversation across the agent's runs).
        # None guards nothing: a send outside an agent's tools.
        self.agent_id = agent_id
        self.sp_token_fn = sp_token_fn
        # The integration's signature integration: address -> the
        # person's ``EmailSignature``, or None (the mail tools then copy it
        # from the sent mail, as without one). ``signature_plan`` is the
        # integration's Email signature setting as resolved for the run
        # (``email_signatures.SignaturePlan``); None signs whenever a
        # reader is given.
        self.signature_fn = signature_fn
        self.signature_plan = signature_plan
        # What produced the tool calls of this session (the run, the agent,
        # the conversation): the origin of a send logged in the activity log
        # of the agent accounts on this integration.
        self.send_origin = dict(send_origin or {})
        # The integration's Compose style setting: the CSS the text of a body
        # the mail tools render carries (``email_html.compose_declarations``:
        # None is Outlook's native compose style, "none" adds none).
        self.compose_style = compose_style or None
        self.sem = asyncio.Semaphore(_MICROSOFT_CONCURRENCY)

    async def log_account_send(
        self,
        account_provider: str,
        tool: str,
        *,
        conversation_id: str | None,
        text: str,
        message_id: str | None = None,
        conversation_type: str | None = None,
        conversation_name: str | None = None,
        recipients: list[str] | None = None,
        error: str | None = None,
        thread_route: dict[str, str] | None = None,
    ) -> None:
        """Log a message sent through this integration in its agent accounts' activity log.

        *thread_route* names the channel thread a channel send posted in: the
        accounts then hear its follow-ups as after a rule reply.
        Nothing when the session has no integration; a failed write is a
        server-side warning, never the send's failure.
        """
        if not self.integration_id:
            return
        from app.services.agent_accounts.sent_log import record_proactive_send

        await record_proactive_send(
            self.integration_id,
            provider=account_provider,
            tool=tool,
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            conversation_name=conversation_name,
            message_id=message_id,
            text=text,
            recipients=recipients,
            error=error,
            origin=self.send_origin,
            thread_route=thread_route,
        )

    def client(self) -> AutoRefreshClient:
        return AutoRefreshClient(self.headers, self.refresh_fn)

    async def request(
        self,
        client: Any,
        method: str,
        url: str,
        provider: str,
        action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        """Concurrency-limited wrapper around ``safe_request``.

        ``AutoRefreshClient`` sends a request's ``headers`` INSTEAD of its own,
        which would drop the bearer token, so headers given for one Graph
        request are laid over the session's. A raw client (a pre-authenticated
        upload URL) keeps exactly the headers it is given.
        """
        if isinstance(client, AutoRefreshClient) and kwargs.get("headers"):
            kwargs["headers"] = {**self.headers, **kwargs["headers"]}
        async with self.sem:
            return await safe_request(client, method, url, provider, action, **kwargs)

    async def collect_all(
        self,
        client: AutoRefreshClient,
        url: str,
        action: str,
        params: dict | None = None,
    ) -> tuple[list[dict], str | None]:
        """GET a Graph collection, following ``@odata.nextLink`` across pages.

        Returns ``(items, error)`` where ``items`` aggregates the ``value``
        arrays of every page, so a lookup never misses an entity that lands on
        a later page.
        """
        items: list[dict] = []
        next_url: str | None = url
        next_params: dict | None = params
        while next_url:
            kw: dict = {"params": next_params} if next_params else {}
            resp, err = await self.request(client, "get", next_url, _PROVIDER, action, **kw)
            if err:
                return [], err
            data = resp.json()
            items.extend(data.get("value", []))
            next_url = data.get("@odata.nextLink")
            next_params = None  # nextLink already carries the query parameters
        return items, None

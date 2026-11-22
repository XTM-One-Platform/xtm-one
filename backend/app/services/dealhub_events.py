# SPDX-License-Identifier: Apache-2.0

"""DealHub quote events for ``integration_event`` assignments.

An assignment with ``{"provider": "dealhub", "event": <event>}`` is woken by
the quotes of the DealHub account its integration is connected to, through two
mechanisms that share one payload shape:

* **Webhooks** (real time): a DealHub administrator adds a webhook (System
  Settings > API Settings > Webhooks) whose URL is the integration's webhook
  URL (``POST /api/hooks/integrations/{integration_id}``), with the quote events
  wanted and the **Token** authentication type. DealHub then sends
  ``Authorization: Bearer <token>`` with every delivery; the token is stored
  on the integration (``webhook_token``). A delivery without it, or with
  another, is refused (401); an integration with no token refuses every
  delivery. DealHub signs nothing else: the bearer token is the whole proof.
* **Polling** (the fallback, every ``POLL_INTERVAL_SECONDS``): DealHub's quote
  list has no "changed since" filter, so the poll reads the quotes
  (``GET /api/v2/quotes``, ``feature=info,summary``, at most ``MAX_POLL_PAGES``
  pages of 50) and compares each quote's status with the one it read last time
  (the map is the assignment's poll cursor, written once the events ran). A
  quote that changed status to the event's status is one event. The first poll
  only records the statuses: enabling a trigger never replays history.

The webhook-only events (``quote_draft`` edits of an existing draft,
``quote_primary``, ``quote_unpublished``) have no status change a poll could
see. A change a webhook already delivered is recorded per assignment
(``webhook_statuses_key``) and the poll skips it, so a quote change runs once
whichever mechanism reads it first; a webhook delayed past the next poll can
still run a second time, which the payload's ``delivery`` tells apart.

Filter keys (``filter_reason``): ``status`` (quote statuses, case-insensitive),
``discount_min`` / ``discount_max`` (``total_discount``, percent), ``owner``
(the logins or domains of who acted: ``executed_by`` for a webhook, the
submitter or creator for a poll), ``primary_only`` (true: the primary quote of
its opportunity only) and ``external_opportunity_id`` (CRM opportunity ids).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

PROVIDER = "dealhub"
POLL_INTERVAL_SECONDS = 900
#: Quotes read per poll at most (50 a page, DealHub's ceiling).
PAGE_SIZE = 50
MAX_POLL_PAGES = 40
#: The integration's extra field holding the webhook bearer token.
TOKEN_FIELD = "webhook_token"  # noqa: S105 - the field name, not a secret
#: A delivery whose execution date is older than this is ignored (replay).
REPLAY_WINDOW = timedelta(days=7)
_WEBHOOK_STATUS_TTL_SECONDS = 86400 * 7

logger = logging.getLogger(__name__)

#: event -> (DealHub webhook event, quote status a poll sees, label, when).
#: A ``None`` status is an event only a webhook delivers.
EVENT_SPECS: dict[str, tuple[str, str | None, str, str]] = {
    "quote_draft": ("draftQuote", "Draft", "DealHub quote saved as draft", "a quote is created or saved as a draft"),
    "quote_primary": (
        "primaryQuote",
        None,
        "DealHub quote set as primary",
        "a quote becomes the primary quote of its opportunity",
    ),
    "quote_pending_approval": (
        "quotePendingApproval",
        "ApprovalProcess",
        "DealHub quote submitted for approval",
        "a quote is submitted for approval or an approval step is decided",
    ),
    "quote_ready": (
        "quoteReady",
        "ReadyToBeSent",
        "DealHub quote approved",
        "a quote is approved and ready to be sent",
    ),
    "quote_rejected": ("quoteRejected", "Rejected", "DealHub quote rejected", "an approver rejects a quote"),
    "quote_recalled": ("quoteRecalled", "Recalled", "DealHub quote recalled", "a quote is recalled from approval"),
    "quote_published": ("quotePublished", "Published", "DealHub quote published", "a quote is published to the buyer"),
    "quote_unpublished": ("quoteUnpublished", None, "DealHub quote unpublished", "a published quote is unpublished"),
    "quote_won": ("quoteWon", "Won", "DealHub quote signed (won)", "a quote is signed and marked won"),
}
EVENTS = frozenset(EVENT_SPECS)
_BY_DEALHUB_EVENT = {spec[0]: event for event, spec in EVENT_SPECS.items()}
#: The events a poll can see, by the quote status they end in.
_BY_STATUS = {spec[1].casefold(): event for event, spec in EVENT_SPECS.items() if spec[1]}
#: The date a poll stamps a status change with, when the quote carries one.
_STATUS_DATE = {"approvalprocess": "submission_date", "won": "won_date"}

FILTER_KEYS = {
    "status": "Only quotes in these statuses (Draft, ApprovalProcess, ReadyToBeSent, Rejected, Recalled, Published, Won)",
    "discount_min": "Only quotes whose total discount (percent) is at least this value",
    "discount_max": "Only quotes whose total discount (percent) is at most this value",
    "owner": "Only changes made by these DealHub logins or email domains (the submitter or creator when polled)",
    "primary_only": "true: only the primary quote of its opportunity",
    "external_opportunity_id": "Only quotes of these CRM opportunity ids",
}


def event_of(dealhub_event: object) -> str | None:
    """The trigger event of a DealHub webhook event name (``quoteWon`` -> ``quote_won``)."""
    return _BY_DEALHUB_EVENT.get(str(dealhub_event or "").strip())


def polled(event: str) -> bool:
    """Whether a poll can see *event* (a status change)."""
    spec = EVENT_SPECS.get(event)
    return bool(spec and spec[1])


#: The two descriptions of the catalog events (the label names the change).
HYBRID_DESCRIPTION = (
    "Carries quote_id, quote_status, owner, the totals and discounts, the approval step and the playbook "
    "answers. Real time through a DealHub webhook, with polling every 15 minutes as the fallback. "
    "Each change runs once."
)
WEBHOOK_DESCRIPTION = (
    "Carries quote_id, quote_status, owner, the totals and discounts and the playbook answers. "
    "Real time through a DealHub webhook only. Each change runs once."
)


def catalog_events() -> list[dict[str, Any]]:
    """The ``available_events`` of the ``dealhub`` catalog entry."""
    events = []
    for event, (_dealhub_event, status, label, _when) in EVENT_SPECS.items():
        events.append(
            {
                "event": event,
                "label": label,
                "description": HYBRID_DESCRIPTION if status else WEBHOOK_DESCRIPTION,
                "mechanism": "hybrid" if status else "webhook",
                "filter_keys": dict(FILTER_KEYS),
            }
        )
    return events


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _summary_fields(summary: object) -> dict[str, Any]:
    summary = summary if isinstance(summary, Mapping) else {}
    return {
        "currency": _text(summary.get("currency")),
        "total_list_price": _number(summary.get("total_list_price")),
        "total_net_price": _number(summary.get("total_net_price")),
        "total_discount": _number(summary.get("total_discount")),
        "sales_discount": _number(summary.get("sales_discount")),
    }


def _answers(additional: object) -> dict[str, str]:
    """``additional_data`` (a list of one-key objects) as one mapping."""
    answers: dict[str, str] = {}
    if isinstance(additional, list):
        for entry in additional:
            if isinstance(entry, Mapping):
                for key, value in entry.items():
                    answers[str(key)] = _text(value)
    return answers


def webhook_payload(delivery: Mapping[str, Any], event: str) -> dict[str, Any] | None:
    """The assignment payload of one webhook delivery; ``None`` without a quote id."""
    quote_id = _text(delivery.get("dealhub_quote_id"))
    if not quote_id:
        return None
    approval = delivery.get("approval_details")
    approval = approval if isinstance(approval, Mapping) else {}
    executed_at = _text(delivery.get("execution_date"))
    payload: dict[str, Any] = {
        "trigger_type": "integration_event",
        "provider": PROVIDER,
        "event": event,
        "dealhub_event": _text(delivery.get("event")),
        "delivery": "webhook",
        "message_id": f"dealhub:{quote_id}:{event}:{executed_at or 'now'}",
        "quote_id": quote_id,
        "quote_status": _text(delivery.get("status")),
        "dealhub_opportunity_id": _text(delivery.get("dealhub_opportunity_id")),
        "external_opportunity_id": _text(delivery.get("external_opportunity_id")),
        "external_quote_id": _text(delivery.get("external_quote_id")),
        "external_customer_id": _text(delivery.get("external_customer_id")),
        "primary_quote": bool(delivery.get("primary_quote")),
        "owner": _text(delivery.get("executed_by")),
        "impersonated_by": _text(delivery.get("impersonated_by")),
        "occurred_at": executed_at,
        "note_for_approval": _text(delivery.get("note_for_approval")),
        "approval_status": _text(approval.get("status")),
        "approval_reviewed_by": _text(approval.get("reviewed_by")),
        "approval_review_date": _text(approval.get("review_date")),
        "approval_reviewer_comment": _text(approval.get("reviewer_comment")),
        "playbook_answers": _answers(delivery.get("additional_data")),
    }
    payload.update(_summary_fields(delivery.get("quote_summary")))
    return payload


def poll_payload(quote: Mapping[str, Any], event: str, previous_status: str | None) -> dict[str, Any] | None:
    """The assignment payload of a status change a poll read; ``None`` without a quote id."""
    quote_id = _text(quote.get("dealhub_quote_id"))
    if not quote_id:
        return None
    info = quote.get("info")
    info = info if isinstance(info, Mapping) else {}
    status = _text(quote.get("status"))
    stamp = _text(info.get(_STATUS_DATE.get(status.casefold(), ""))) or f"from:{previous_status or 'new'}"
    payload: dict[str, Any] = {
        "trigger_type": "integration_event",
        "provider": PROVIDER,
        "event": event,
        "dealhub_event": EVENT_SPECS[event][0],
        "delivery": "poll",
        "message_id": f"dealhub:{quote_id}:{event}:poll:{stamp}",
        "quote_id": quote_id,
        "quote_status": status,
        "previous_status": previous_status or "",
        "quote_name": _text(info.get("dealhub_quote_name")),
        "quote_number": _text(info.get("dealhub_quote_number")),
        "quote_url": _text(info.get("quote_url")),
        "account_name": _text(info.get("account_name")),
        "dealhub_opportunity_id": _text(info.get("dealhub_opportunity_id")),
        "external_opportunity_id": _text(info.get("external_opportunity_id")),
        "external_quote_id": _text(info.get("external_quote_id")),
        "external_customer_id": _text(info.get("external_customer_id")),
        "primary_quote": bool(info.get("primary_quote")),
        "owner": _text(info.get("submitted_by")) or _text(info.get("created_by")),
        "occurred_at": _text(info.get(_STATUS_DATE.get(status.casefold(), ""))),
        "expiration_date": _text(info.get("expiration_date")),
    }
    payload.update(_summary_fields(quote.get("summary")))
    return payload


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


def _strings(value: object) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item).strip() for item in items if str(item).strip()]


def _owner_matches(owner: str, entry: str) -> bool:
    owner, entry = owner.casefold(), entry.casefold().lstrip("@")
    if not owner:
        return False
    return owner == entry or ("@" not in entry and owner.endswith(f"@{entry}"))


def filter_reason(payload: Mapping[str, Any], filter_config: Mapping[str, Any]) -> str | None:
    """Why the trigger's filter drops *payload*, ``None`` when it creates a run."""
    statuses = {s.casefold() for s in _strings(filter_config.get("status"))}
    if statuses and _text(payload.get("quote_status")).casefold() not in statuses:
        return "quote status is not in status"
    discount = _number(payload.get("total_discount"))
    low = _number(filter_config.get("discount_min"))
    if low is not None and (discount is None or discount < low):
        return "total discount is below discount_min"
    high = _number(filter_config.get("discount_max"))
    if high is not None and (discount is None or discount > high):
        return "total discount is above discount_max"
    owners = _strings(filter_config.get("owner"))
    if owners and not any(_owner_matches(_text(payload.get("owner")), entry) for entry in owners):
        return "owner is not in owner"
    primary_only = filter_config.get("primary_only")
    if (primary_only is True or str(primary_only).strip().lower() == "true") and not payload.get("primary_quote"):
        return "quote is not the primary quote"
    opportunities = {o.casefold() for o in _strings(filter_config.get("external_opportunity_id"))}
    if opportunities and _text(payload.get("external_opportunity_id")).casefold() not in opportunities:
        return "opportunity is not in external_opportunity_id"
    return None


# ---------------------------------------------------------------------------
# Webhook authentication
# ---------------------------------------------------------------------------


def webhook_tokens(extra: Mapping[str, Any]) -> list[str]:
    """The configured webhook tokens (several separated by commas while rotating)."""
    raw = extra.get(TOKEN_FIELD) if isinstance(extra, Mapping) else None
    if not isinstance(raw, str):
        return []
    return [token.strip() for token in raw.split(",") if token.strip()]


def verify_bearer(authorization: str | None, tokens: list[str]) -> bool:
    """Whether ``Authorization: Bearer <token>`` names one of *tokens* (constant time)."""
    if not tokens or not authorization:
        return False
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return False
    from app.core.security import timing_safe_str_eq

    presented = value.strip()
    matched = False
    for token in tokens:
        matched |= timing_safe_str_eq(presented, token)
    return matched


def _execution_instant(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def delivery_too_old(delivery: Mapping[str, Any], now: datetime | None = None) -> bool:
    """A delivery executed before the replay window (a replay): ignored."""
    instant = _execution_instant(delivery.get("execution_date"))
    return instant is not None and instant < (now or datetime.now(UTC)) - REPLAY_WINDOW


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


def webhook_statuses_key(assignment_id: object) -> str:
    """Per assignment: quote id -> the status a webhook delivered last."""
    return f"dealhub_webhook_status:{assignment_id}"


async def record_webhook_status(redis: Any, assignment_id: object, quote_id: str, status: str) -> None:
    if not quote_id or not status:
        return
    key = webhook_statuses_key(assignment_id)
    await redis.hset(key, quote_id, status)
    await redis.expire(key, _WEBHOOK_STATUS_TTL_SECONDS)


async def webhook_statuses(redis: Any, assignment_id: object) -> dict[str, str]:
    raw = await redis.hgetall(webhook_statuses_key(assignment_id)) or {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        k = key.decode() if isinstance(key, bytes) else str(key)
        v = value.decode() if isinstance(value, bytes) else str(value)
        out[k] = v
    return out


def load_statuses(stored: object) -> dict[str, str] | None:
    """The status map a poll cursor holds; ``None`` before the first poll."""
    if stored is None:
        return None
    text = stored.decode() if isinstance(stored, bytes) else str(stored)
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, Mapping):
        return None
    statuses = data.get("statuses")
    if not isinstance(statuses, Mapping):
        return None
    return {str(k): str(v) for k, v in statuses.items()}


def dump_statuses(statuses: Mapping[str, str]) -> str:
    return json.dumps({"statuses": dict(statuses)}, separators=(",", ":"), sort_keys=True)


def poll_events(
    quotes: Sequence[Mapping[str, Any]],
    event: str,
    known: dict[str, str] | None,
    *,
    delivered: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The *event* payloads of the status changes since *known*, and the new status map.

    *known* ``None`` (the first poll) records the statuses and answers no event.
    A change a webhook already delivered (*delivered*: quote id -> status) is
    recorded and skipped. A quote absent from this read keeps its last status
    (the read may stop at ``MAX_POLL_PAGES``).
    """
    statuses = dict(known or {})
    events: list[dict[str, Any]] = []
    wanted = EVENT_SPECS[event][1] if event in EVENT_SPECS else None
    for quote in quotes:
        quote_id = _text(quote.get("dealhub_quote_id"))
        status = _text(quote.get("status"))
        if not quote_id or not status:
            continue
        previous = statuses.get(quote_id)
        statuses[quote_id] = status
        if known is None or wanted is None or previous == status:
            continue
        if status.casefold() != wanted.casefold():
            continue
        if (delivered or {}).get(quote_id, "").casefold() == status.casefold():
            continue
        payload = poll_payload(quote, event, previous)
        if payload is not None:
            events.append(payload)
    return events, statuses


async def list_quotes(credential: str) -> tuple[list[dict[str, Any]], str | None]:
    """Every quote the account holds, up to ``MAX_POLL_PAGES`` pages, with its info and summary."""
    from app.integrations.providers.dealhub._client import DealHubClient, parse_dealhub_token

    client = DealHubClient(parse_dealhub_token(credential))
    quotes: list[dict[str, Any]] = []
    for page in range(MAX_POLL_PAGES):
        resp, err = await client.request(
            "GET",
            "/api/v2/quotes",
            "list the quotes",
            params={"feature": ["info", "summary"], "offset": page * PAGE_SIZE, "limit": PAGE_SIZE},
        )
        if err:
            return quotes, err
        raw = resp.data if resp is not None else None
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        raw_batch = data.get("quotes")
        batch: list[Any] = raw_batch if isinstance(raw_batch, list) else []
        quotes.extend(dict(q) for q in batch if isinstance(q, Mapping))
        raw_info = data.get("info")
        info: Mapping[str, Any] = raw_info if isinstance(raw_info, Mapping) else {}
        if not batch or not info.get("more_results_matching_the_request"):
            return quotes, None
    logger.info("DealHub poll stopped at %d quotes (%d pages)", len(quotes), MAX_POLL_PAGES)
    return quotes, None


# ---------------------------------------------------------------------------
# Webhook delivery
# ---------------------------------------------------------------------------


def _answer(status: str, triggered: list[dict[str, Any]] | None = None, reason: str = "") -> dict[str, Any]:
    answer: dict[str, Any] = {"status": status, "triggered": triggered or [], "count": len(triggered or [])}
    if reason:
        answer["reason"] = reason
    return answer


async def dispatch_delivery(integration_id: str, payload: Any) -> dict[str, Any]:
    """Start or queue a run per assignment of the delivery's event (the caller verified the token)."""
    from fastapi import HTTPException

    from app.core.database import async_session
    from app.core.redis import redis_client
    from app.services.agentic_gate import refusal_code
    from app.services.assignment_engine import trigger_or_queue_assignment
    from app.services.assignment_pause import AssignmentOwnerDeactivatedError
    from app.services.docusign_connect import provider_assignments
    from app.services.integration_event_filters import (
        event_filter_reason,
        record_filtered_events,
        trigger_filter,
        without_filter_signals,
    )
    from app.services.slack_events import claim_seen, release_seen

    if not isinstance(payload, Mapping):
        return _answer("ignored", reason="not a DealHub quote webhook delivery (a JSON object)")
    event = event_of(payload.get("event"))
    if event is None:
        return _answer("ignored", reason="not a DealHub quote event this platform reads")
    if delivery_too_old(payload):
        logger.info("DealHub delivery for integration %s older than the replay window: ignored", integration_id)
        return _answer("ignored", reason="delivery older than the replay window")
    redis = await redis_client()
    triggered: list[dict[str, Any]] = []
    filtered: dict[Any, int] = {}
    async with async_session() as db:
        for wf, agent in await provider_assignments(db, integration_id, PROVIDER, {event}):
            event_payload = webhook_payload(payload, event)
            if event_payload is None:
                continue
            msg_id = event_payload["message_id"]
            if not await claim_seen(redis, wf.id, msg_id):
                continue
            await record_webhook_status(redis, wf.id, event_payload["quote_id"], event_payload["quote_status"])
            filter_config = trigger_filter(wf.trigger_config or {})
            if filter_config and event_filter_reason(event, event_payload, filter_config) is not None:
                filtered[wf.id] = filtered.get(wf.id, 0) + 1
                continue
            without_filter_signals(event_payload)
            trigger_context = {
                "type": "integration_event",
                "provider": PROVIDER,
                "event": event,
                "integration_id": str(integration_id),
                "message_id": msg_id,
                "delivery": "dealhub_webhook",
                "triggered_at": datetime.now(UTC).isoformat(),
            }
            try:
                result = await trigger_or_queue_assignment(
                    wf, agent, event_payload, db=db, trigger_context=trigger_context, overflow="reject"
                )
            except HTTPException as exc:
                await release_seen(redis, wf.id, msg_id)
                if refusal_code(exc) is None and not isinstance(exc, AssignmentOwnerDeactivatedError):
                    raise
                continue
            except ValueError:
                await release_seen(redis, wf.id, msg_id)
                continue
            except Exception:
                await release_seen(redis, wf.id, msg_id)
                raise
            if result.status in ("queue_full", "dropped"):
                await release_seen(redis, wf.id, msg_id)
                continue
            triggered.append({"assignment": wf.name, "agent": agent.name, "event": event, "run_id": result.run_id})
    for assignment_id, count in filtered.items():
        await record_filtered_events(assignment_id, count)
    logger.info("DealHub delivery for integration %s (%s): %d run(s)", integration_id, event, len(triggered))
    return _answer("ok", triggered)

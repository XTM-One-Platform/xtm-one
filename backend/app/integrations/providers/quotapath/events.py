"""QuotaPath events for ``integration_event`` assignments, by polling.

QuotaPath's public API has no webhook: what it exposes is the payout list with
an ``updated_since`` filter (``/payout/``, ``/payout/resolved/``) and the list
of payouts still owed (``/payout/unresolved/``). ``event_poller`` reads them
every ``POLL_INTERVAL_SECONDS``:

* ``payout_updated``: a payout created or changed since the last poll
  (``/payout/?updated_since=``): a deal credited, a rate or quota changed;
* ``payout_resolved``: a payout resolved (paid out) since the last poll
  (``/payout/resolved/?updated_since=``): the period's payouts were locked
  and paid;
* ``payout_unresolved``: a payout newly owed (``/payout/unresolved/``, a
  payout id not seen before): the accrual grows.

Each payout becomes one event payload whose ``message_id`` is the payout id
and its update stamp, so a payout updated twice runs twice and a re-read runs
nothing. The first poll of an assignment starts from now.
"""

from __future__ import annotations

from typing import Any

from ._client import QuotaPathClient

PROVIDER = "quotapath"

#: Event -> the payout list it reads and whether it filters by update time.
EVENT_SOURCES: dict[str, tuple[str, bool]] = {
    "payout_updated": ("/payout/", True),
    "payout_resolved": ("/payout/resolved/", True),
    "payout_unresolved": ("/payout/unresolved/", False),
}
EVENTS = frozenset(EVENT_SOURCES)

#: Commissions move with deals: a quarter of an hour is soon enough.
POLL_INTERVAL_SECONDS = 900

EVENT_LABELS: dict[str, str] = {
    "payout_updated": "Payout created or updated",
    "payout_resolved": "Payout resolved",
    "payout_unresolved": "Payout newly owed",
}


def payout_event(row: dict[str, Any], event_type: str) -> dict[str, Any]:
    """The payload one payout hands to the assignment."""
    payout_id = str(row.get("id") or row.get("deal_uuid") or "")
    stamp = row.get("updated_ts") or ""
    entity = {
        "type": "quotapath_payout",
        "id": payout_id,
        "rep_name": row.get("target_user_name"),
        "rep_email": row.get("target_user_email"),
        "plan": row.get("plan_name"),
        "path": row.get("path_name"),
        "deal_name": row.get("deal_name"),
        "deal_uuid": row.get("deal_uuid"),
        "date": row.get("date") or row.get("deal_date"),
        "value": row.get("value"),
        "owed": row.get("owed"),
        "kind": row.get("kind"),
        "updated_ts": row.get("updated_ts"),
    }
    return {
        "message_id": f"quotapath:{event_type}:{payout_id}:{stamp}" if payout_id else "",
        "provider": PROVIDER,
        "event": event_type,
        "entity": entity,
        "summary": (
            f"QuotaPath payout {EVENT_LABELS[event_type].lower()}: {row.get('target_user_name') or ''} "
            f"{row.get('plan_name') or ''} {row.get('value')}"
        ).strip(),
    }


async def list_payouts(api_key: str, event_type: str, since: int | None) -> tuple[list[dict[str, Any]], str | None]:
    """``(payouts, error)``: the event's payouts, updated at or after *since* where it filters."""
    path, by_update = EVENT_SOURCES[event_type]
    params: dict[str, Any] = {}
    if by_update and since is not None:
        params["updated_since"] = int(since)
    rows, _, err = await QuotaPathClient(api_key).list_all(path, "list payouts for an event", params, max_rows=2000)
    if err and not rows:
        return [], err
    return [r for r in rows if isinstance(r, dict)], None

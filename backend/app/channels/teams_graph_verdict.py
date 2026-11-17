# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Deciding Teams Graph notifications with no I/O.

The pure half of ``POST /api/channels/teams/graph-notifications``: a
subscription record, what it says was issued, the verdict on one notification
and the decision on a whole POST from its raw body. ``teams.py`` holds the I/O
around it (the records read from Redis, the gap markers, the queued tasks). A
large POST is decided in a worker process (``teams_graph_worker.py``) that
imports this module alone, so it stays light: the standard library,
``app.core.security`` and the Graph path-segment rule.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.security import decrypt_value, timing_safe_str_eq
from app.integrations.providers.graph_path import _validate_path_segment

# Every subscription was created with ``copilot_{team_id}`` before random
# clientStates (2026-07-02); anyone who knows a team id can compute it.
_LEGACY_CLIENT_STATE_PREFIX = "copilot_"


def _subscription_key(team_id: str, channel_id: str) -> str:
    return f"teams:priv_ch_sub:{team_id}:{channel_id}"


def _read_subscription_record(raw: str | bytes) -> dict | None:
    """A Redis subscription record as a dict, or ``None`` when it cannot be read."""
    try:
        record = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def _issued_client_state(record: dict, team_id: str) -> str | None:
    """The clientState the subscription in *record* was created with, or ``None``.

    - ``client_state_enc``: a random value, Fernet-encrypted (this release).
    - ``client_state``: a random value stored in plaintext by the releases of
      2026-07-02 onwards; the renewal pass re-stores it encrypted.
    - neither key: a record written before random values existed, so the
      subscription was created with the legacy value; the renewal pass
      replaces it with a random one when verification is required. Only such
      a record makes that value a proof - without a record it proves nothing.
    """
    if "client_state_enc" in record:
        encrypted = record["client_state_enc"]
        if not isinstance(encrypted, str) or not encrypted:
            return None
        try:
            return decrypt_value(encrypted)
        except Exception:
            return None
    if "client_state" in record:
        plain = record["client_state"]
        return plain if isinstance(plain, str) and plain else None
    return f"{_LEGACY_CLIENT_STATE_PREFIX}{team_id}"


# ``issued`` default of the helpers below: the record's clientState is not read
# yet (``None`` is a value: it cannot be read).
_NOT_READ: Any = object()


def _record_state(record: dict, team_id: str, issued: str | None = _NOT_READ) -> str:
    """How the renewal pass treats *record*.

    *issued*: ``_issued_client_state(record, team_id)`` when the caller read it.

    ``current``: a readable encrypted clientState. ``plaintext``: re-stored
    encrypted. ``legacy`` (the derivable value) and ``unprovable`` (a
    clientState that cannot be read - for instance encrypted under a previous
    SECRET_KEY - or no subscription id): none of its notifications can be
    told from a forgery, so the subscription is replaced when verification is
    required, and kept and renewed as before otherwise.
    """
    sub_id = record.get("subscription_id")
    if not (isinstance(sub_id, str) and sub_id):
        return "unprovable"
    if issued is _NOT_READ and ("client_state_enc" in record or "client_state" in record):
        issued = _issued_client_state(record, team_id)
    if "client_state_enc" in record:
        return "current" if issued else "unprovable"
    if "client_state" in record:
        return "plaintext" if issued else "unprovable"
    return "legacy"


def _grace_open(entry: dict, now: datetime) -> bool:
    try:
        return datetime.fromisoformat(entry.get("accept_until") or "") > now
    except (TypeError, ValueError):
        return False


def _accepted_subscriptions(
    record: dict, team_id: str, issued: str | None = _NOT_READ
) -> list[tuple[str, str]]:
    """The (subscription id, clientState) pairs a notification for the channel may prove.

    *issued*: ``_issued_client_state(record, team_id)`` when the caller read it.
    """
    accepted = []
    sub_id = record.get("subscription_id")
    if issued is _NOT_READ:
        issued = _issued_client_state(record, team_id)
    if isinstance(sub_id, str) and sub_id and issued:
        accepted.append((sub_id, issued))
    now = datetime.now(UTC)
    replaced = record.get("replaced_subscriptions")
    for entry in replaced if isinstance(replaced, list) else ():
        # An entry must say what it issued: one without must not read as legacy.
        if not (isinstance(entry, dict) and "client_state_enc" in entry and _grace_open(entry, now)):
            continue
        old_id, old_state = entry.get("id"), _issued_client_state(entry, team_id)
        if isinstance(old_id, str) and old_id and old_state:
            accepted.append((old_id, old_state))
    return accepted


def _as_uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _tenant_matches(notif: dict, record: dict) -> bool:
    """Whether *notif* comes from the tenant its subscription was created in.

    Graph sends ``tenantId`` with every notification. The recorded tenant is
    the bot's configured one, which may be a domain name: that is not compared.
    """
    recorded = _as_uuid(record.get("tenant_id"))
    provided = notif.get("tenantId")
    if recorded is None or provided is None:
        return True
    return _as_uuid(provided) == recorded


def _record_proves(
    record: dict, notif: dict, team_id: str, accepted: tuple[tuple[str, str], ...] | None = None
) -> bool:
    """Whether *notif* names a subscription of *record* and echoes its clientState.

    *accepted* is ``_accepted_subscriptions(record, team_id)`` when the caller
    holds it already (``_IssuedStates``).
    """
    provided_id = notif.get("subscriptionId")
    provided_state = notif.get("clientState")
    if not (isinstance(provided_id, str) and isinstance(provided_state, str) and _tenant_matches(notif, record)):
        return False
    proven = False
    for sub_id, issued in _accepted_subscriptions(record, team_id) if accepted is None else accepted:
        # Every pair and both halves are compared: the time taken says neither
        # which subscription matched nor which half failed.
        proven |= timing_safe_str_eq(provided_id, sub_id) & timing_safe_str_eq(provided_state, issued)
    return proven


def _lifecycle_notification_proven(
    record: dict | None, notif: dict, team_id: str, accepted: tuple[tuple[str, str], ...] | None = None
) -> bool:
    """Whether a lifecycle notification proves the channel's current subscription.

    Never merely unverified: it renews or recreates the subscription. A
    replaced subscription still accepted for its change notifications has
    nothing left to renew.
    """
    return (
        record is not None
        and _record_proves(record, notif, team_id, accepted)
        and record.get("subscription_id") == notif.get("subscriptionId")
    )


def _compatible_client_state(record: dict | None, team_id: str, issued: str | None = _NOT_READ) -> str | None:
    """The clientState earlier releases compared a notification with, ``None`` when it cannot be read.

    *issued*: ``_issued_client_state(record, team_id)`` when the caller read it.

    They compared the clientState alone - never the subscription id or the
    tenant - with the plaintext ``client_state`` of the channel's record, and
    with the legacy ``copilot_{team_id}`` when the record held none (no
    record, an unreadable one, one written before random values). An
    encrypted value stands for the plaintext one it replaced.
    """
    legacy = f"{_LEGACY_CLIENT_STATE_PREFIX}{team_id}"
    if record is None:
        return legacy
    if "client_state" in record:
        plain = record["client_state"]
        if isinstance(plain, str) and plain:
            return plain
        # Empty: they fell back to the legacy value. Anything else: they
        # could compare it with nothing, and matched nothing.
        return legacy if not plain else ""
    encrypted = record.get("client_state_enc")
    if isinstance(encrypted, str) and encrypted:
        # Here the record's own clientState is exactly this encrypted value.
        return _issued_client_state(record, team_id) if issued is _NOT_READ else issued
    return legacy


@dataclass(frozen=True)
class _IssuedStates:
    """What a channel's record says was issued, its clientState decrypted once for a whole batch."""

    #: ``_accepted_subscriptions``: the pairs a notification may prove.
    accepted: tuple[tuple[str, str], ...]
    #: ``_compatible_client_state``: what earlier releases compared the clientState with.
    compatible: str | None
    #: ``_record_state``, ``None`` without a record.
    state: str | None

    @classmethod
    def of(cls, record: dict | None, team_id: str) -> _IssuedStates:
        if record is None:
            return cls((), _compatible_client_state(None, team_id), None)
        issued = _issued_client_state(record, team_id)
        return cls(
            tuple(_accepted_subscriptions(record, team_id, issued)),
            _compatible_client_state(record, team_id, issued),
            _record_state(record, team_id, issued),
        )


def _notification_verdict(
    record: dict | None,
    notif: dict,
    team_id: str,
    *,
    recorded: bool = False,
    issued: _IssuedStates | None = None,
) -> tuple[str, str]:
    """``("proven", "")``, ``("unverified", <reason>)`` or ``("refused", "")``.

    Proven: *notif* proves a subscription of *record* (``_record_proves``).
    Unverified: it does not, but earlier releases acted on it - the same
    clientState comparison (``_compatible_client_state``), or, for a
    clientState that can no longer be decrypted, the recorded subscription id
    and tenant, which a genuine notification always carries. Acted on unless
    verification is required. *recorded*: a record exists, readable or not.
    *issued*: ``_IssuedStates.of(record, team_id)`` when the caller holds it.
    """
    accepted = issued.accepted if issued is not None else None
    if record is not None and _record_proves(record, notif, team_id, accepted):
        return "proven", ""
    provided_state = notif.get("clientState")
    expected = issued.compatible if issued is not None else _compatible_client_state(record, team_id)
    if expected is not None:
        if isinstance(provided_state, str) and provided_state and timing_safe_str_eq(provided_state, expected):
            if record is None:
                return "unverified", "unreadable_record" if recorded else "no_record"
            if expected == f"{_LEGACY_CLIENT_STATE_PREFIX}{team_id}":
                return "unverified", "derivable_client_state"
            return "unverified", "subscription_not_matched"
        return "refused", ""
    provided_id = notif.get("subscriptionId")
    recorded_id = record.get("subscription_id") if record is not None else None
    if (
        isinstance(provided_id, str)
        and isinstance(recorded_id, str)
        and recorded_id
        and timing_safe_str_eq(provided_id, recorded_id)
        and _tenant_matches(notif, record)
    ):
        return "unverified", "unreadable_client_state"
    return "refused", ""


@dataclass(frozen=True)
class _BatchDecision:
    """What the route does with the entries of one POST (``_decide_notifications``)."""

    #: The entries a task is queued for, in batch order.
    accepted: list[dict]
    #: Entries that prove no subscription recorded for their channel.
    refused: int
    #: Entries that are neither a lifecycle notification nor a new channel message.
    ignored: int


@dataclass(frozen=True)
class _BatchCandidates:
    """The entries of one POST that name a channel to decide against (``_notification_candidates``)."""

    #: One per entry to decide, in batch order: the entry, its team and channel,
    #: and the message it names (``None`` for a lifecycle notification).
    entries: list[tuple[dict, str, str, tuple[str, ...] | None]]
    #: Entries that are neither a lifecycle notification nor a new channel message.
    ignored: int
    #: The subscription key of every channel the entries name, with its team and channel.
    channels: dict[str, tuple[str, str]]


def _notification_batch(raw_body: bytes) -> list | None:
    """The ``value`` array of a POST: ``None`` for a body that is not JSON, ``[]`` for one that names none."""
    try:
        body = json.loads(raw_body)
    except (ValueError, RecursionError):
        return None
    notifications = body.get("value") if isinstance(body, dict) else None
    return notifications if isinstance(notifications, list) else []


def _notification_candidates(
    notifications: list, lifecycle_team: str | None, lifecycle_channel: str | None
) -> _BatchCandidates:
    """Sort the entries of a batch into the ones to decide and the ones ignored; no I/O."""
    candidates: list[tuple[dict, str, str, tuple[str, ...] | None]] = []
    ignored = 0
    for notif in notifications:
        if isinstance(notif, dict) and "lifecycleEvent" in notif:
            # The channel comes from the lifecycle URL; with none, nothing is proven.
            candidates.append((notif, lifecycle_team or "", lifecycle_channel or "", None))
            continue
        resource = notif.get("resource") if isinstance(notif, dict) else None
        if not isinstance(resource, str) or notif.get("changeType") != "created":
            ignored += 1
            continue
        team_id, channel_id, message_id, reply_id = _parse_graph_resource(resource)
        if not (team_id and channel_id and message_id):
            ignored += 1
            continue
        candidates.append((notif, team_id, channel_id, (team_id, channel_id, message_id, reply_id or "")))
    channels = {_subscription_key(t, c): (t, c) for _notif, t, c, _message in candidates if t and c}
    return _BatchCandidates(candidates, ignored, channels)


def _decide_candidates(
    batch: _BatchCandidates, raw_records: dict[str, Any], require_verification: bool
) -> tuple[_BatchDecision, list[tuple[str, str]]]:
    """Decide every candidate against the records read for its channel; no I/O.

    *raw_records* holds the raw record of each channel that has one; a channel
    it does not name has none. Also returns the (team, channel) of each
    channel whose refused entries must mark its notification gap once the
    channel is known to be monitored.
    """
    states: dict[str, tuple[dict | None, bool, _IssuedStates, bool]] = {}
    for key, (team_id, _channel_id) in batch.channels.items():
        raw = raw_records.get(key)
        record = _read_subscription_record(raw) if raw else None
        issued = _IssuedStates.of(record, team_id)
        unverifiable = record is None or issued.state == "unprovable"
        states[key] = (record, bool(raw), issued, unverifiable)
    accepted: list[dict] = []
    queued_messages: set[tuple[str, ...]] = set()
    refused = 0
    refused_unverifiable: dict[str, tuple[str, str]] = {}
    for notif, t, c, message in batch.entries:
        key = _subscription_key(t, c)
        if key not in states:
            refused += 1
            continue
        record, recorded, issued, unverifiable = states[key]
        if message is None:
            if _lifecycle_notification_proven(record, notif, t, issued.accepted):
                accepted.append(notif)
            else:
                refused += 1
            continue
        verdict, _reason = _notification_verdict(record, notif, t, recorded=recorded, issued=issued)
        if verdict == "proven" or (verdict == "unverified" and not require_verification):
            if message not in queued_messages:
                queued_messages.add(message)
                accepted.append(notif)
            continue
        refused += 1
        if unverifiable:
            refused_unverifiable[key] = (t, c)
    return _BatchDecision(accepted, refused, batch.ignored), list(refused_unverifiable.values())


def _read_batch(
    raw_body: bytes, lifecycle_team: str | None, lifecycle_channel: str | None
) -> tuple[int | None, _BatchCandidates | None]:
    """The number of entries of a POST (``None``: not JSON) and its candidates (``None`` without an entry)."""
    notifications = _notification_batch(raw_body)
    if not notifications:
        return (None if notifications is None else 0), None
    return len(notifications), _notification_candidates(notifications, lifecycle_team, lifecycle_channel)


def summarize_post(
    raw_body: bytes, lifecycle_team: str | None, lifecycle_channel: str | None
) -> tuple[int | None, list[str], int, int]:
    """What the route needs before reading records, and nothing larger.

    The number of entries (``None`` for a body that is not JSON), the
    subscription key of every channel the candidates name, the number of
    candidates and the number of entries ignored.
    """
    count, batch = _read_batch(raw_body, lifecycle_team, lifecycle_channel)
    if batch is None:
        return count, [], 0, 0
    return count, list(batch.channels), len(batch.entries), batch.ignored


def decide_post(
    raw_body: bytes,
    lifecycle_team: str | None,
    lifecycle_channel: str | None,
    raw_records: dict[str, Any],
    require_verification: bool,
) -> tuple[_BatchDecision, list[tuple[str, str]]]:
    """``_decide_candidates`` on a POST read again from its raw body.

    Nothing is kept between ``summarize_post`` and this call, which may run
    in another process than the first.
    """
    _count, batch = _read_batch(raw_body, lifecycle_team, lifecycle_channel)
    if batch is None:
        return _BatchDecision([], 0, 0), []
    return _decide_candidates(batch, raw_records, require_verification)


_GRAPH_RESOURCE_RE = re.compile(
    r"/?teams\('([^']+)'\)/channels\('([^']+)'\)/messages\('([^']+)'\)(?:/replies\('([^']+)'\))?"
)


def _parse_graph_resource(resource: str) -> tuple[str, str, str, str | None]:
    """Parse a Graph notification resource string.

    Handles both formats, for a channel message and for a reply in its thread:
      - ``teams('tid')/channels('cid')/messages('mid')[/replies('rid')]``
      - ``teams/tid/channels/cid/messages/mid[/replies/rid]``

    Returns ``(team_id, channel_id, message_id, reply_id)``: for a reply,
    ``message_id`` is its thread's root. Anything that does not name exactly
    one message is ``("", "", "", None)``, and so is an id that could leave
    its path segment: every id goes into a Graph URL and a Redis key.
    """
    m = _GRAPH_RESOURCE_RE.fullmatch(resource)
    if m:
        ids = [group for group in m.groups() if group is not None]
    else:
        parts = resource.removeprefix("/").split("/")
        keywords = ["teams", "channels", "messages", "replies"][: len(parts) // 2]
        ids = parts[1::2] if len(parts) in (6, 8) and parts[0::2] == keywords else []
    if not ids or not all(_is_graph_path_segment(value) for value in ids):
        return "", "", "", None
    return ids[0], ids[1], ids[2], ids[3] if len(ids) == 4 else None


def _is_graph_path_segment(value: str) -> bool:
    """Whether *value* stays one URL path segment: the Teams bot tools' rule, dot segments included."""
    return _validate_path_segment(value, "id") is None

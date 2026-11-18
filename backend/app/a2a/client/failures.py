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

"""One classification of every outbound failure: transient, or durable.

The single thing the circuit breaker and the registration's health verdict
both read (FR-053). They ask different questions — "should I stop trying" and
"why is this unusable" — and deriving both from one classification is what
stops them disagreeing about the same partner.

The distinction that matters is not severity, it is **whether retrying could
ever succeed**:

* **transient** — a timeout, a connection error, a partner 5xx. The partner
  may be back in a minute; counting these is what the breaker is for.
* **durable** — a credential that cannot mint a token, a partner rejecting a
  token we did mint, no protocol version in common, a card that fails
  verification, a destination refused. Retrying cannot fix any of them, so
  they never touch the breaker's counter and instead make the registration
  unusable with a reason an operator can act on.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.a2a import (
    FAILURE_DURABLE,
    FAILURE_TRANSIENT,
    UNUSABLE_CARD_UNFETCHABLE,
    UNUSABLE_CREDENTIAL_NO_TOKEN,
    UNUSABLE_CREDENTIAL_REJECTED,
    UNUSABLE_REASONS,
    UNUSABLE_REQUEST_REJECTED,
)


@dataclass(frozen=True)
class Classification:
    """What kind of failure this was, and why.

    ``reason`` is ``None`` for a transient failure on purpose: the closed
    reason set describes why a registration is UNUSABLE, and a partner that
    timed out is not unusable — it is unreachable right now. Giving transient
    failures a reason would put them on a surface that means something else.
    """

    kind: str
    reason: str | None = None

    @property
    def is_durable(self) -> bool:
        return self.kind == FAILURE_DURABLE

    @property
    def is_transient(self) -> bool:
        return self.kind == FAILURE_TRANSIENT


TRANSIENT = Classification(FAILURE_TRANSIENT)


def durable(reason: str) -> Classification:
    """A durable failure with a reason from the closed set.

    The membership check is not defensive padding: it is what stops a call
    site inventing a reason no surface knows how to explain, which is how a
    closed set stops being closed.
    """
    if reason not in UNUSABLE_REASONS:
        raise ValueError(
            f"{reason!r} is not in the closed unusable-reason set. "
            "Add it to app.models.a2a.UNUSABLE_REASONS with the requirement "
            "it serves, or use an existing value."
        )
    return Classification(FAILURE_DURABLE, reason)


def classify_http_status(status: int) -> Classification:
    """Classify a partner's HTTP status.

    401/403 is durable and 5xx is transient, which is the whole of the
    interesting content here: a rejected credential will be rejected again,
    and a partner's own server error will not.

    404 is durable rather than transient, deliberately. A partner endpoint
    that is not there is a configuration problem — a wrong card URL, an agent
    the partner deleted — and retrying it every run for a cooldown is noise
    an operator never sees, where an unusable verdict is something they can
    act on.
    """
    if status in (401, 403):
        return durable(UNUSABLE_CREDENTIAL_REJECTED)
    if status == 404:
        return durable(UNUSABLE_CARD_UNFETCHABLE)
    if 500 <= status < 600:
        return TRANSIENT
    if 400 <= status < 500:
        # Any other 4xx is us, not them: a request the partner has rejected on
        # its merits will be rejected again next run. The reason names the
        # surface that refused — the request, not the card — so the reader is
        # not sent to re-check a card URL that is fine.
        return durable(UNUSABLE_REQUEST_REJECTED)
    return TRANSIENT


#: What a partner answers a request naming a task it will not act on: an
#: ended task (A2A's ``UnsupportedOperation``, Google's "is in terminal
#: state"), an id it does not know, or a state that conflicts.
_TASK_REFUSAL_STATUSES = frozenset({400, 404, 409, 410, 422})


def refuses_the_named_task(status: int) -> bool:
    """Whether *status*, answered to a request naming a partner task, refuses
    that task rather than this registration.

    The id came from the partner, so the request reached the agent that
    issued it: the refusal concerns that task alone, and the next delegation
    is a fresh request it may well accept. Durable for the registration it
    would refuse that next delegation too, until an operator re-verifies a
    registration nothing is wrong with. A registration that really is broken
    still shows it on the next request that names no task.
    """
    return status in _TASK_REFUSAL_STATUSES


def classify_exception(exc: BaseException) -> Classification:
    """Classify a raised failure.

    Unknown exception types classify as **transient**, and that is a
    deliberate choice in the less-safe direction for once. A durable
    classification takes a registration out of service and demands operator
    attention; reaching that on an exception nobody anticipated would turn a
    library's odd error into an outage of the partner relationship. A wrong
    transient classification costs a cooldown, which self-heals.
    """
    from app.a2a.client.destinations import DestinationRefusedError

    if isinstance(exc, DestinationRefusedError):
        # The guard already classified it — https, forbidden address, or a
        # name that will not resolve are three different operator problems.
        # Re-deriving one here would be a second classification of the same
        # failure, and the two would disagree the moment either changed.
        return durable(exc.reason)
    return TRANSIENT


#: The one non-exception failure path that classifies outside this module.
CREDENTIAL_CANNOT_MINT = durable(UNUSABLE_CREDENTIAL_NO_TOKEN)

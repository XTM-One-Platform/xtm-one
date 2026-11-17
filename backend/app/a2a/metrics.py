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

"""Telemetry for the A2A inbound surface.

External traffic is labelled distinctly from internal traffic so that load
originating outside the organisation is separately observable. Without the
distinction an operator watching run volume cannot tell a busy team from a
partner integration misbehaving - which matters here because the platform
deliberately sets no cap on concurrent external runs, making observation the
only way that condition is noticed.

Per-process values, like every other collector here: treat as spike/trend
signals rather than exact totals.
"""

from __future__ import annotations

import logging

from app.core.metrics import get_or_create_counter

logger = logging.getLogger(__name__)

#: Submissions accepted, by whether the agent is company-managed and whether
#: the submission was a deduplicated repeat.
a2a_tasks_submitted = get_or_create_counter(
    "a2a_tasks_submitted_total",
    "External A2A task submissions accepted",
    ["company_managed", "duplicate"],
)

#: Terminal outcomes, so a partner integration failing shows up as a shape
#: change rather than only in logs.
a2a_tasks_completed = get_or_create_counter(
    "a2a_tasks_completed_total",
    "External A2A tasks reaching a terminal state",
    ["state"],
)

#: Refusals, by reason. The security-relevant series: a spike in
#: ``unauthenticated`` or ``rejected_platform_key`` is what a credential probe
#: looks like from the outside.
a2a_requests_refused = get_or_create_counter(
    "a2a_requests_refused_total",
    "External A2A requests refused",
    ["reason"],
)

#: Exposure register changes: ``action`` is ``expose`` or ``revoke``, and
#: ``reason`` names what caused it — ``bound``/``unbound`` for the operator
#: binding or unbinding an A2A integration, and the ``models.a2a`` revocation
#: reasons for the automatic teardowns a tier, sharing, ownership or disable
#: change forces.
a2a_exposure_changes = get_or_create_counter(
    "a2a_exposure_changes_total",
    "A2A exposure grants and revocations",
    ["action", "reason"],
)


def record_submission(company_managed: bool, *, duplicate: bool) -> None:
    a2a_tasks_submitted.labels(
        company_managed=str(company_managed).lower(),
        duplicate=str(duplicate).lower(),
    ).inc()


def record_terminal(state: str) -> None:
    a2a_tasks_completed.labels(state=state).inc()


def record_refusal(reason: str) -> None:
    """Count a refusal and leave a log line for it.

    A refusal is answered before any task or run exists, so it leaves no trace
    an operator can find from a partner's report ("three messages were
    refused") unless it is logged here. The reason only: never the credential,
    the message or the caller's identifiers, which the audit log carries where
    they belong.
    """
    a2a_requests_refused.labels(reason=reason).inc()
    logger.info("A2A request refused: reason=%s", reason)


#: Push notification delivery attempts, by outcome. ``delivered`` or
#: ``failed`` — and the failed series is the one an operator needs, because a
#: callback is the only part of this surface where the platform depends on
#: something outside it. Delivery is best-effort by design, so a partner going
#: dark produces no error anywhere else: without this counter it is invisible
#: until the partner complains.
a2a_push_deliveries = get_or_create_counter(
    "a2a_push_deliveries_total",
    "A2A push notification delivery attempts",
    ["outcome"],
)

#: Callbacks abandoned after exhausting their attempt budget. Separate from the
#: failed series above rather than a label on it: one failure is a blip and this
#: is the platform having stopped trying, which is the thing worth alerting on.
a2a_push_abandoned = get_or_create_counter(
    "a2a_push_abandoned_total",
    "A2A push notification callbacks abandoned after exhausting their attempts",
    [],
)


def record_exposure_change(action: str, reason: str) -> None:
    """Count one exposure change. Called ONLY from ``a2a.hooks``.

    That is not an incidental fact about the current call sites — it is what
    makes the counter trustworthy. Exposure follows an integration binding and
    the flush listener is the single place either direction is written, so
    every change is counted exactly once however the binding was edited.

    ``reason`` is required rather than defaulted. It defaulted to ``"manual"``
    while an explicit expose/revoke endpoint existed; nothing is manual now,
    and a default would silently mislabel automatic revocations as operator
    actions — the exact distinction an operator reads this metric for.
    """
    a2a_exposure_changes.labels(action=action, reason=reason).inc()


def record_push_delivery(outcome: str) -> None:
    a2a_push_deliveries.labels(outcome=outcome).inc()


def record_push_abandoned() -> None:
    """Count one callback the platform has stopped trying to reach.

    Paired with an audit entry rather than replacing it: the counter says how
    often this happens and the audit says which partner, task and address, and
    an operator asked "why was this partner not told" needs the second.
    """
    a2a_push_abandoned.inc()



# --- Outbound (A2A client) ------------------------------------------------
# Separate series from the inbound ones above, not a label on them. Load this
# platform SENDS and load it RECEIVES answer different operational questions
# — one is a partner's capacity problem and the other is ours — and a shared
# series makes the common "how much traffic are we causing" query require
# knowing to filter by a direction label.

#: One per delegation attempt. ``target`` is the mechanism (which partner
#: platform), ``outcome`` the terminal classification.
a2a_delegations = get_or_create_counter(
    "a2a_delegations_total",
    "A2A delegations this platform sent to a partner",
    ["target", "outcome"],
)

#: Delegations refused before any request left the process — switch off,
#: registration unusable, circuit open, card change unaccepted, content-type
#: mismatch. Distinct from a failed delegation because a refusal costs the
#: partner nothing and means something is wrong on OUR side.
#: ``target`` mirrors the label on the attempt series above: a refusal is
#: still traffic this platform decided not to send to a particular partner
#: platform, and without it "which provider is refusing" cannot be asked of
#: metrics at all.
a2a_delegations_refused = get_or_create_counter(
    "a2a_delegations_refused_total",
    "A2A delegations refused before a request was sent",
    ["target", "reason"],
)

#: Card verifications, by what they concluded. ``result`` is ``usable``,
#: ``unusable`` or ``suspended``; ``trigger`` says whether the sweep, an
#: operator or a stale-verdict delegation asked for it.
a2a_card_verifications = get_or_create_counter(
    "a2a_card_verifications_total",
    "A2A partner card verifications",
    ["result", "trigger"],
)


def record_delegation(target: str, outcome: str) -> None:
    a2a_delegations.labels(target=target, outcome=outcome).inc()


def record_delegation_refused(target: str, reason: str) -> None:
    """Count a delegation this platform refused to send.

    Called from the single place a delegation concludes, so a counter reading
    zero means no delegation was refused rather than that nothing was
    instrumented (FR-043). The same property the inbound exposure counter
    relies on.
    """
    a2a_delegations_refused.labels(target=target, reason=reason).inc()


def record_card_verification(result: str, trigger: str) -> None:
    a2a_card_verifications.labels(result=result, trigger=trigger).inc()

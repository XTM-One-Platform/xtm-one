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

"""Handing one unit of work to a partner, and following it to an outcome.

The pre-flight order below is not arbitrary — it goes from cheapest and most
certain to most expensive, so a delegation that was never going to happen
costs nothing and never reaches the partner:

1. chat availability, 2. the binding's direction, 3. the health verdict,
4. the circuit, 5. verdict freshness, 6. the accepted card, 7. the recorded
endpoint, 8. content types, 9. the origin.

**Refusing costs the partner nothing.** That is why a refusal is recorded with
its own outcome (``refused_preflight``) rather than as a failure: a failure
means we tried and something went wrong at the other end, and conflating the
two would make a misconfiguration on our side look like a partner problem in
every dashboard that counts them.

The one place the fail-closed rule is genuinely tempting to bend is step 5.
A stale verdict that cannot be re-verified refuses the delegation — it does
NOT fall back to what the verdict used to say. See :func:`_check_freshness`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from app.a2a.client import breaker, credential_state
from app.a2a.client.credentials import (
    CredentialError,
    forget_token,
    outbound_headers,
    sends_instance_identity,
)
from app.a2a.client.destinations import (
    DestinationRefusedError,
    assert_same_origin,
    origin_of,
)
from app.a2a.client.failures import (
    CREDENTIAL_CANNOT_MINT,
    Classification,
    classify_exception,
    classify_http_status,
    durable,
    refuses_the_named_task,
)
from app.a2a.client.registration import (
    VerdictWriteContendedError,
    reverify_before_delegating,
    verdict_is_stale,
)
from app.a2a.protocol import (
    DIALECT_PROTO_V10,
    TASK_NOT_FOUND,
    TERMINAL_STATES,
    TRANSPORT_JSONRPC,
    TRANSPORT_REST,
    A2AState,
    dialect_for,
    instance_metadata,
    method_for,
    normalise_transport,
    outbound_configuration,
    outbound_message,
    outbound_push_field,
    read_parts,
    read_send_result,
    read_state,
    rest_alternate_prefix,
    rest_cancel_path,
    rest_prefix,
    rest_send_path,
    rest_task_path,
)
from app.core.config import settings as app_settings
from app.models.a2a import (
    MECHANISM_ASKED,
    MECHANISM_CALLED_BACK,
    OUTCOME_CANCELLED,
    OUTCOME_COMPLETED,
    OUTCOME_CONTENT_TYPE_MISMATCH,
    OUTCOME_CREDENTIAL_REJECTED,
    OUTCOME_PARTNER_FAILED,
    OUTCOME_REFUSED_PREFLIGHT,
    OUTCOME_STILL_RUNNING,
    OUTCOME_TIMEOUT,
    OUTCOME_UNREACHABLE,
    UNUSABLE_CARD_MALFORMED,
    UNUSABLE_CREDENTIAL_NO_TOKEN,
    UNUSABLE_NEVER_VERIFIED,
    USABILITY_UNUSABLE,
    USABILITY_USABLE,
)

if TYPE_CHECKING:
    from sqlalchemy import Select

logger = logging.getLogger(__name__)

#: How often to ask a partner whether it is done. Polling does not consume a
#: loop iteration (the platform's wait primitive is deliberately not charged
#: one), so this is bounded by the delegation ceiling rather than by the
#: calling agent's reasoning budget.
_POLL_INTERVAL_SECONDS = 2.0

#: Connecting is not the partner working; the ceiling is far too long for it.
_CONNECT_TIMEOUT_SECONDS = 10.0


def _connect_timeout_context() -> str:
    """What the process looked like when a connect timed out.

    A connect resolves the host through ``loop.getaddrinfo``, which runs on
    the loop's default executor; anyio's thread limiter covers the rest of
    the to_thread work. A saturated executor, or an event loop carrying many
    tasks, delays a connect that never reaches the peer. Logged so a timeout
    names its cause. Never raises: it only decorates a failure already being
    handled.
    """
    from app.core.executor import default_executor_state

    parts: list[str] = [default_executor_state()]
    try:
        import anyio.to_thread

        limiter = anyio.to_thread.current_default_thread_limiter()
        parts.append(
            f"thread_limiter={limiter.borrowed_tokens}/{limiter.total_tokens}"
        )
    except Exception as exc:  # diagnostics only
        parts.append(f"thread_limiter=unknown ({type(exc).__name__})")
    try:
        parts.append(f"loop_tasks={len(asyncio.all_tasks())}")
    except Exception as exc:  # diagnostics only
        parts.append(f"loop_tasks=unknown ({type(exc).__name__})")
    return " ".join(parts)


#: A cancel follows a partner that stopped answering; it must not wait a
#: second ceiling for the same silence.
_CANCEL_TIMEOUT_SECONDS = 15.0


class PartnerStillRunningError(Exception):
    """The attempt ended and the partner's task had not.

    Distinct from a timeout on purpose. A timeout means the partner stopped
    answering; this means it is answering and simply has not finished, which is
    an ordinary thing for real work to do and must not cost the caller the
    result (FR-001).
    """

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"Partner task {task_id} is still running.")


#: A pre-flight refusal that is transient: the target's verdict was being
#: written by another session (a sweep, a Verify, another delegation). The
#: same call succeeds a few seconds later.
OUTCOME_VERIFICATION_CONTENDED = "verification_contended"

#: A pre-flight refusal that is transient: whether chat is available could not
#: be read (the AI switch or the licence read failed). Not "AI disabled" or
#: "Community Edition": nothing is misconfigured, and the same call succeeds
#: once the read does (#411).
OUTCOME_CHAT_STATE_UNREADABLE = "chat_state_unreadable"

CHAT_STATE_UNREADABLE_DETAIL = (
    "Whether agentic chat is available on this deployment could not be read "
    "(a transient database error), so nothing was sent to the partner. This "
    "is not a configuration problem: send the same call again in a few seconds."
)

#: The pre-flight refusals that say nothing about the partner task: the same
#: collect succeeds a few seconds later. Refusing one of them must not end the
#: delegation a collect was asking about (#317).
RETRYABLE_PREFLIGHT_REASONS = frozenset({OUTCOME_VERIFICATION_CONTENDED, OUTCOME_CHAT_STATE_UNREADABLE})

_RETRY_COLLECT_HINT = (
    "The delegation is still outstanding: collect again in a few seconds with "
    "the same continue_task_id and no message. The answer is not lost."
)
# A continuation that carries a follow-up was refused before anything was
# sent: the collect hint would make the caller drop its message (#320).
_RETRY_SEND_HINT = (
    "The delegation is still outstanding but your follow-up message was NOT "
    "sent: send it again in a few seconds with the same continue_task_id and "
    "the same message."
)


class PreflightRefusedError(Exception):
    """A delegation refused before any request left the process."""

    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class CallingRun:
    """Who is delegating, recorded at submission time.

    Passed in rather than resolved here because this module must not go
    looking for the caller: FR-042 requires attribution to key on what was
    recorded when the work was sent, never on what an agent's bindings say
    later — two registrations may be bound to one agent and would otherwise
    each report the other's traffic.
    """

    agent_id: uuid.UUID
    user_id: uuid.UUID
    run_ref: uuid.UUID | None = None
    #: Set on the chat path, where ``run_ref`` is NULL. It is what cancels an
    #: outstanding task when the user abandons the conversation, since the turn
    #: that made the delegation ends the moment the task is handed back.
    conversation_ref: uuid.UUID | None = None
    #: The work item the inbox dispatched the calling run for, from the
    #: dispatcher's own record (never the payload). A later run of the same
    #: agent for the same item may collect a task an earlier run of that item
    #: started, while the earlier run is still recorded as running (#600).
    work_item_ref: uuid.UUID | None = None


@dataclass
class DelegationResult:
    """What the calling agent gets back.

    Deliberately not an exception on the failure paths: every one of these is
    an ordinary outcome the agent may reason about and possibly work around,
    and raising would make "the partner said no" indistinguishable from "the
    platform broke" at the call site.
    """

    outcome: str
    result_text: str | None = None
    partner_reason: str | None = None
    detail: str | None = None
    partner_task_id: str | None = None
    question: str | None = None
    protocol_version: str | None = None
    signature_state: str | None = None
    continuation_count: int = 0
    #: Set on a hand-back where the partner will call us back. It changes what
    #: the calling agent should do next — wait to be told, rather than ask
    #: again — which FR-021 excepts from "no difference visible to the calling
    #: agent" precisely because the still-running outcome is the exception.
    awaiting_callback: bool = False

    @property
    def succeeded(self) -> bool:
        return self.outcome == OUTCOME_COMPLETED

    @property
    def needs_input(self) -> bool:
        return self.question is not None

    @property
    def still_running(self) -> bool:
        return self.outcome == OUTCOME_STILL_RUNNING


async def _check_freshness(db, integration, target) -> None:
    """Re-verify a stale verdict, and refuse if that cannot be completed.

    FR-050, and the single most important refusal in this module. A verdict's
    age is exactly the moment when falling back to what it used to say feels
    harmless — the partner was fine an hour ago, the check is only a
    formality, the delegation is already in flight. It is not harmless: a
    partner that changed its card, rotated to a credential we no longer hold,
    or moved its endpoint is precisely the case a stale verdict cannot see.

    The verdict is written in a short transaction of its own, never in the
    delegation's: a sweep, a Verify or another delegation holding the target
    row must not leave this send waiting out the statement timeout and its
    session rolled back. A row still held after a short wait is a retryable
    refusal, not a send on the old verdict.
    """
    if not verdict_is_stale(target):
        return
    try:
        await reverify_before_delegating(integration, target)
    except VerdictWriteContendedError as exc:
        raise PreflightRefusedError(OUTCOME_VERIFICATION_CONTENDED, str(exc)) from exc
    # The ROW, not the returned verdict: ``apply_verdict`` returns a usable
    # verdict while suspending the row for a downgrade or an unaccepted
    # change, and the usability gate ran before this re-verification.
    if target.usability != USABILITY_USABLE:
        raise PreflightRefusedError(
            target.unusable_reason or UNUSABLE_NEVER_VERIFIED,
            "Re-verification before delegating failed: "
            f"{target.last_verification_error or target.unusable_reason}",
        )


def _is_text(declared: str) -> bool:
    """Whether *declared* is a content type this platform's text fits.

    Both spellings are live: the media type ``text/plain``, and the bare
    ``text`` that Azure Foundry's card declares.
    """
    kind = str(declared).strip().lower()
    return kind == "text" or kind.startswith("text/")


def _check_content_types(target) -> None:
    """Refuse before submitting when the card says this exchange cannot work.

    Cheaper for everyone than discovering it from a partner's error, and it
    is the difference between "this partner does not accept text" and an
    opaque 400 (FR-028). This platform always sends text and reads text or
    data parts, so the input side is the whole check.
    """
    accepted = target.declared_input_types or []
    if accepted and not any(_is_text(t) for t in accepted):
        raise PreflightRefusedError(
            OUTCOME_CONTENT_TYPE_MISMATCH,
            f"Partner accepts {accepted}; this platform sends text.",
        )


async def _chat_available(db: Any) -> bool:
    """``is_chat_enabled``, read in a session of its own (#411).

    Not on the caller's session: the calls of one model round run together and
    share the run's session, and a read that fails there (a concurrent use, an
    aborted transaction) answers ``False``, reported as "AI is disabled, or
    Community Edition" although nothing is. A read that could not be made is
    refused as :data:`OUTCOME_CHAT_STATE_UNREADABLE`, a retryable reason, never
    as ``chat_unavailable``; a switch or licence read as off still is.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from app.api.v1.platform import is_chat_enabled
    from app.core.database import async_session
    from app.services import ai_state

    try:
        async with async_session() as own:
            available = await is_chat_enabled(own)
    except (SQLAlchemyError, OSError) as exc:
        # The licence read (``_load_ps``) raises where the switch's read is
        # caught: both are "could not be read".
        raise PreflightRefusedError(OUTCOME_CHAT_STATE_UNREADABLE, CHAT_STATE_UNREADABLE_DETAIL) from exc
    if not available and ai_state.cached_state() is None and ai_state.last_read_failed():
        # ``is_ai_enabled`` answers ``False`` to a failed read and caches only
        # what it read: no cached answer and a failed last read is "unknown".
        raise PreflightRefusedError(OUTCOME_CHAT_STATE_UNREADABLE, CHAT_STATE_UNREADABLE_DETAIL)
    return bool(available)


async def preflight(db, integration, target) -> None:
    """Every check that must pass before a request leaves the process."""
    from app.api.v1.platform import CHAT_UNAVAILABLE_DETAIL

    # Chat availability first (A2A is offered exactly while agentic chat is):
    # the one check that says nothing about this registration, so a deployment
    # without chat pays for nothing else. Also asked when the tool is built;
    # this is the second line, for a licence or AI toggle that moved between
    # the two.
    if not await _chat_available(db):
        raise PreflightRefusedError("chat_unavailable", CHAT_UNAVAILABLE_DETAIL)
    # A target EXISTS only for an outbound registration — ``sync_targets`` is
    # called for no other direction, and ``change_direction`` deletes them —
    # so having one IS the direction check. Asking the database again would
    # buy nothing and cost a query on every delegation.
    if target is None:
        raise PreflightRefusedError(
            "not_outbound", "This integration does not face outward."
        )
    if target.usability != USABILITY_USABLE:
        reason = target.unusable_reason or UNUSABLE_NEVER_VERIFIED
        detail = f"Registration is {target.usability}."
        if reason == UNUSABLE_CREDENTIAL_NO_TOKEN:
            # Name the repair: there is no self-service re-registration.
            detail = f"{detail} {credential_state.repair_hint(integration)}"
        raise PreflightRefusedError(reason, detail)
    if breaker.is_open(target):
        raise PreflightRefusedError(
            "circuit_open",
            f"Paused after {target.consecutive_transient_failures} "
            "consecutive failures.",
        )
    await _check_freshness(db, integration, target)
    if (
        target.accepted_card_fingerprint is not None
        and target.card_fingerprint != target.accepted_card_fingerprint
    ):
        raise PreflightRefusedError(
            "card_change_unaccepted",
            "The partner changed its card; accept the change to resume.",
        )
    # The endpoint the CARD declared, never the card's own location: the card
    # is a GET-only discovery document, and no fallback to ``card_url``
    # because improvising it as a destination IS that bug. A row without one
    # is stale by definition and was re-verified two checks ago.
    if not target.invocation_url:
        raise PreflightRefusedError(
            "endpoint_unknown",
            "This registration has no recorded invocation endpoint. Use Test "
            "Connection to re-read the partner's card.",
        )
    _check_content_types(target)


def _part_text(part: object) -> str | None:
    """One part rendered as something a calling model can read.

    Text parts are themselves. DATA parts are serialised, and that is the case
    this function exists for: an agent configured for JSON output answers with
    a ``DataPart``, which both wires carry under ``data`` (v0.3 adds
    ``kind: "data"``). Reading only ``text`` would report every structured
    partner answer as a completed delegation with an empty result — the worst
    failure shape available, because the calling agent is told it succeeded.

    File parts are deliberately not rendered: a URI is not the answer, and
    inventing prose about one would put words in the partner's mouth.
    """
    if not isinstance(part, dict):
        return None
    if part.get("text"):
        return str(part["text"])
    data = part.get("data")
    if isinstance(data, dict | list):
        import json

        return json.dumps(data, ensure_ascii=False, indent=2)
    return None


def _parts_text(container: object) -> list[str]:
    """Every part of one Message or Artifact that renders as text."""
    return [
        rendered
        for part in read_parts(container)
        if (rendered := _part_text(part))
    ]


def _artifact_text(payload: dict) -> list[str]:
    """What the partner PRODUCED: the text of every artifact on a task."""
    return [
        rendered
        for artifact in payload.get("artifacts") or []
        for rendered in _parts_text(artifact)
    ]


def _status_text(payload: dict) -> list[str]:
    """What the partner SAID: the text of a task's status message."""
    return _parts_text((payload.get("status") or {}).get("message"))


def _terminal_reason(payload: dict[str, Any]) -> str | None:
    """What the partner gave as the reason of a failed or canceled task.

    A2A carries it in the status message; a ``status.reason`` some partners
    add is kept in front of it. ``None`` when the partner said nothing.
    """
    reason = (payload.get("status") or {}).get("reason")
    said = [str(reason)] if isinstance(reason, str) and reason.strip() else []
    said += [text for text in _status_text(payload) if text not in said]
    return _bounded(said) or None


def _terminal_detail(state: A2AState | None, payload: dict[str, Any]) -> str:
    """Our own line on a failed or canceled task: the state seen, and whether the partner said why."""
    seen = state.value if state is not None else "unknown"
    if _terminal_reason(payload):
        return f"The partner ended the task in state '{seen}' and gave the reason in partner_reason."
    return f"The partner ended the task in state '{seen}' and gave no reason."


def _bounded(chunks: list[str]) -> str:
    """Join, bounded by ``a2a_max_message_chars`` — the inbound half's ceiling (FR-027)."""
    text = "\n".join(chunks)
    ceiling = app_settings.a2a_max_message_chars
    if len(text) <= ceiling:
        return text
    # Marked, not merely cut. The inbound half REFUSES an over-long message;
    # refusing here would throw away work the partner has already done, so this
    # truncates instead — but a silent cut hands the model a partial answer
    # that reads as a complete one, which is the worse of the two failures.
    return text[:ceiling] + f"\n[truncated at {ceiling} characters]"


def _extract_text(payload: dict) -> str:
    """A completed task's artifacts, or its status message when it has none."""
    return _bounded(_artifact_text(payload) or _status_text(payload))


def _extract_message_text(message: dict | None) -> str:
    """The text of a Message the partner answered a send with directly.

    A Message carries ``parts``; a Task carries ``artifacts``. Reading a
    Message with :func:`_extract_text` finds neither artifacts nor a status,
    and returns the empty string for an answer that was right there.
    """
    return "\n".join(_parts_text(message))


def _task_state(payload: dict) -> A2AState | None:
    """The state a partner reports, in whatever spelling it used.

    ``None`` means unrecognised, which the caller refuses whole (FR-026).
    Delegated to :func:`app.a2a.protocol.read_state` because the spelling is a
    per-version wire concern and there is exactly one module allowed to know
    those. Never normalise case here: v1.0 states are proto enum NAMES, and
    proto-JSON is all the REST binding speaks.
    """
    return read_state((payload.get("status") or {}).get("state"))


class PartnerLostTaskError(Exception):
    """The partner cannot find a task it just created.

    Its own exception because the alternative is worse than useless: a 404 on
    the task surface classifies as durable ``card_unfetchable``, which would
    mark the whole registration unusable - a card-shaped reason for a partner
    that has simply not retained one task.

    Observed against Vertex AI Agent Engine: a non-blocking send answers
    SUBMITTED with a task id, and every poll for that id is 404 forever. It
    keeps no task store between requests. So this is a failure of ONE
    delegation, recorded as such, and the registration stays usable.
    """

    def __init__(self, task_id: str, detail: str = ""):
        self.task_id = task_id
        self.detail = detail
        super().__init__(
            f"The partner does not retain task {task_id}"
            f"{f' - {detail}' if detail else ''}. It may only answer a "
            "blocking send."
        )


class UnrecognisedPartnerStateError(Exception):
    """A task state no supported protocol version defines."""


class PartnerRpcError(Exception):
    """The partner answered a JSON-RPC ERROR rather than a result.

    A JSON-RPC error is carried at HTTP **200** with an ``error`` member and
    no ``result``. Reading ``result`` unconditionally turns every partner-side
    error into an empty payload, then a phantom task id, then "unrecognised
    task state: <absent>" — discarding the one diagnosis an operator actually
    needs: what the partner said was wrong, in words.

    JSON-RPC only. The REST binding carries its errors as an HTTP status with
    a body, which is :class:`_PartnerHttpError`.
    """

    def __init__(self, code: object, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Partner reported JSON-RPC error {code}: {message}")

    @property
    def is_task_not_found(self) -> bool:
        """Whether the partner answered A2A ``TaskNotFoundError`` (-32001).

        The code may arrive as an int or as its decimal text; anything else
        is not that error.
        """
        try:
            return int(str(self.code).strip()) == TASK_NOT_FOUND
        except ValueError:
            return False


class _PartnerHttpError(Exception):
    """An HTTP-status failure. ``detail`` is ours; ``partner_said`` is the
    partner's own words, kept apart because only the second is enveloped
    before the calling model reads it."""

    def __init__(
        self,
        status: int,
        classification: Classification,
        detail: str = "",
        *,
        partner_said: str = "",
    ):
        self.status = status
        self.classification = classification
        self.detail = detail
        self.partner_said = partner_said
        said = f": {partner_said}" if partner_said else ""
        ours = f" ({detail})" if detail else ""
        super().__init__(f"Partner returned HTTP {status}{said}{ours}")


def _raise_for_rpc_error(envelope: dict) -> None:
    """Refuse an envelope carrying ``error``, before anything reads ``result``.

    Checked on both JSON-RPC legs — submit and poll. A JSON-RPC response
    carries either member, never both, and the transport status is 200 for
    both, so this is the only place the distinction exists.
    """
    error = envelope.get("error")
    if not isinstance(error, dict):
        return
    raise PartnerRpcError(
        error.get("code", "unknown"),
        str(error.get("message") or "no message"),
    )


def _read_body(response) -> dict:
    """A partner's response body as a dict, refusing an oversized one.

    The size ceiling (``A2A_CLIENT_RESPONSE_MAX_BYTES``, 1 MiB) is enforced
    here because this is the one place both bindings read a body.

    Refused as a MALFORMED response rather than truncated. A truncated JSON
    body is not a smaller answer, it is not an answer.
    """
    content = response.content or b""
    ceiling = app_settings.a2a_client_response_max_bytes
    if ceiling and len(content) > ceiling:
        raise _PartnerHttpError(
            response.status_code,
            durable(UNUSABLE_CARD_MALFORMED),
            f"response body is {len(content)} bytes, over the {ceiling}-byte ceiling",
        )
    if not content:
        return {}
    try:
        body = response.json()
    except Exception as exc:
        raise _PartnerHttpError(
            response.status_code,
            durable(UNUSABLE_CARD_MALFORMED),
            f"response body is not JSON ({type(exc).__name__})",
        ) from exc
    return body if isinstance(body, dict) else {}


def _http_error_detail(body: dict) -> str:
    """What a partner said went wrong, on any HTTP-status failure.

    Google's API frontend answers ``{"error": {"code", "message", "status"}}``
    and the A2A REST binding carries the same shape, so one reader serves
    both bindings. Best effort by design: a partner that says nothing useful must not
    turn into an exception of its own.
    """
    error = body.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("status")
        if message:
            return str(message)
    return ""


@dataclass(frozen=True)
class _Answer:
    """What one wire call produced.

    Both bindings answer a send with a Task **or** a Message, so both are
    carried and the caller decides. A Message means the partner answered
    without opening a task — the v1.0 default asks for exactly that — and it
    must not be read as a Task: it has no id and no status, and a successful
    delegation would be reported as "unrecognised task state: <absent>".
    """

    task: dict | None = None
    message: dict | None = None


class _Wire:
    """The three operations a delegation needs, over one binding.

    A class per binding rather than branches at each call site: the JSON-RPC
    and REST shapes differ in the method name, the URL, the request body AND
    the response envelope, and four scattered ``if`` statements would be four
    chances to update three of them.
    """

    def __init__(self, *, endpoint: str, version: str, headers: dict[str, str]):
        self.endpoint = endpoint.rstrip("/")
        self.version = version
        self.headers = headers

    def message(self, text: str, *, submission_id: str, task_id: str | None) -> dict:
        """The message to send, spelled for this binding and version.

        Built HERE rather than by the caller because the body is a property of
        the binding AND the version: shaping by version alone sends a v0.3
        REST partner v0.3 JSON-RPC shapes. The table lives in
        ``protocol.dialect_for``; the wire knows only which binding it is.
        """
        return outbound_message(
            text,
            dialect=dialect_for(self.transport, self.version),
            message_id=submission_id,
            task_id=task_id or None,
            metadata=self.metadata,
        )

    #: Set only when the integration opted in to sending this instance's
    #: identity (#708); ``None`` keeps the message to the composed text alone.
    metadata: dict[str, Any] | None = None

    #: Set when this delegation registers a callback, so the partner is asked
    #: for one in the same request that submits the work — a second round trip
    #: would leave a window where the task can finish unannounced.
    push_config: dict | None = None

    def configuration(self) -> dict | None:
        """What to ask the partner for alongside the message, if anything."""
        dialect = dialect_for(self.transport, self.version)
        configuration = outbound_configuration(dialect)
        if self.push_config is None:
            return configuration
        # Blocking and a callback ask for the same answer twice. Worse, they
        # cancel each other out: a held connection means there is no polling
        # phase for the grace period to shorten, so the attempt runs to the
        # ceiling and the callback — the whole point of registering one —
        # arrives at a caller that has already been waiting for it inline.
        # This platform's own inbound surface honours blocking, so against it
        # the two together are strictly worse than either alone.
        if dialect == DIALECT_PROTO_V10:
            # v1.0 inverted the flag and its default: blocking is what a
            # partner does when told nothing, so declining it has to be said.
            configuration = {**(configuration or {}), "returnImmediately": True}
        else:
            configuration = {
                k: v for k, v in (configuration or {}).items() if k != "blocking"
            }
        return {**configuration, outbound_push_field(dialect): self.push_config}

    async def submit(
        self, client, *, text: str, submission_id: str, task_id: str | None = None
    ) -> _Answer:
        raise NotImplementedError

    async def fetch(self, client, task_id: str) -> _Answer:
        raise NotImplementedError

    async def cancel(self, client, task_id: str) -> None:
        raise NotImplementedError


class _JsonRpcWire(_Wire):
    """A2A over JSON-RPC 2.0 — one POST endpoint, the method in the body."""

    transport = TRANSPORT_JSONRPC

    async def _call(self, client, method: str, params: dict) -> dict:
        response = await client.post(
            self.endpoint,
            headers=self.headers,
            json={"jsonrpc": "2.0", "id": "1", "method": method, "params": params},
        )
        if response.status_code != 200:
            body = _read_body_quietly(response)
            raise _PartnerHttpError(
                response.status_code,
                classify_http_status(response.status_code),
                partner_said=_http_error_detail(body),
            )
        envelope = _read_body(response)
        _raise_for_rpc_error(envelope)
        result = envelope.get("result")
        return result if isinstance(result, dict) else {}

    async def submit(
        self, client, *, text: str, submission_id: str, task_id: str | None = None
    ) -> _Answer:
        params: dict = {
            "message": self.message(
                text, submission_id=submission_id, task_id=task_id
            )
        }
        configuration = self.configuration()
        if configuration:
            params["configuration"] = configuration
        result = await self._call(client, method_for("send", self.version), params)
        task, immediate = read_send_result(result)
        return _Answer(task=task, message=immediate)

    async def fetch(self, client, task_id: str) -> _Answer:
        return _Answer(
            task=await self._call(
                client, method_for("get", self.version), {"id": task_id}
            )
        )

    async def cancel(self, client, task_id: str) -> None:
        await self._call(client, method_for("cancel", self.version), {"id": task_id})


class _RestWire(_Wire):
    """A2A over HTTP+JSON — the operation is the URL, the body is the request.

    What Google's Agent Platform serves, and the reason this class exists: its
    A2A surface offers no JSON-RPC binding at all, so ``google_a2a``
    registrations verified a card and then failed every delegation against it.

    One wrinkle worth naming. The two versions disagree about whether the
    version belongs in the path: v0.3's proto declares
    ``post: "/v1/message:send"``, while v1.0's reference server mounts the
    bare paths under a prefix the deployment chooses and the card's interface
    URL carries. Both conventions are live — Google's own client appends
    ``/v1`` to the URL its card declares — so the first 404 retries with the
    other one and the answer is remembered for the rest of the delegation.
    """

    transport = TRANSPORT_REST

    def __init__(self, **kw):
        super().__init__(**kw)
        self._prefix: str | None = None
        self._tried_alternate = False

    def _url(self, path: str) -> str:
        url = f"{self.endpoint}{path}"
        # The path is built from the endpoint the verification pinned, so this
        # cannot normally fail — asserted anyway because a partner-supplied
        # task id goes into it, and "cannot normally fail" is where the
        # interesting failures live.
        assert_same_origin(url, origin_of(self.endpoint))
        return url

    def _prefixes(self) -> tuple[str, str]:
        primary = rest_prefix(self.version) if self._prefix is None else self._prefix
        return primary, rest_alternate_prefix(self.version)

    async def _request(self, client, method: str, build_path, *, json_body=None) -> dict:
        primary, alternate = self._prefixes()
        response = await client.request(
            method,
            self._url(build_path(primary)),
            headers=self.headers,
            json=json_body,
        )
        if response.status_code == 404 and not self._tried_alternate:
            # Not a missing task: a task that does not exist answers 404 too,
            # and telling them apart from here is not possible. Retrying the
            # other prefix once is cheap, and a partner that answers 404 to
            # both leaves the status untouched for the caller to classify.
            self._tried_alternate = True
            retry = await client.request(
                method,
                self._url(build_path(alternate)),
                headers=self.headers,
                json=json_body,
            )
            if retry.status_code != 404:
                logger.info(
                    "a2a.client: REST partner answers under the %r prefix",
                    alternate or "(none)",
                )
                self._prefix = alternate
                response = retry
        if response.status_code not in (200, 201, 202):
            raise _PartnerHttpError(
                response.status_code,
                classify_http_status(response.status_code),
                partner_said=_http_error_detail(_read_body_quietly(response)),
            )
        return _read_body(response)

    async def submit(
        self, client, *, text: str, submission_id: str, task_id: str | None = None
    ) -> _Answer:
        body = await self._request(
            client,
            "POST",
            lambda prefix: rest_send_path(self.version, prefix=prefix),
            json_body=_without_none(
                {
                    "message": self.message(
                        text, submission_id=submission_id, task_id=task_id
                    ),
                    "configuration": self.configuration(),
                }
            ),
        )
        task, immediate = read_send_result(body)
        return _Answer(task=task, message=immediate)

    async def fetch(self, client, task_id: str) -> _Answer:
        # A bare Task in both versions, so there is no envelope to unwrap.
        try:
            body = await self._request(
                client,
                "GET",
                lambda prefix: rest_task_path(task_id, self.version, prefix=prefix),
            )
        except _PartnerHttpError as exc:
            if exc.status == 404:
                # On a POLL, a 404 is a lost task, not a missing endpoint - the
                # send that created it answered 200 at the same origin moments
                # earlier. Classifying it as ``card_unfetchable`` would suspend
                # the registration over one delegation.
                raise PartnerLostTaskError(task_id, exc.partner_said) from exc
            raise
        return _Answer(task=body)

    async def cancel(self, client, task_id: str) -> None:
        await self._request(
            client,
            "POST",
            lambda prefix: rest_cancel_path(task_id, self.version, prefix=prefix),
            json_body={},
        )


def wire_for(target, *, endpoint: str, version: str, headers: dict[str, str]) -> _Wire:
    """The binding this target was VERIFIED against, never one re-derived now.

    A missing ``transport`` is JSON-RPC: rows written before the column
    existed were all verified by code that understood nothing else, and the
    specification's own default for an unnamed transport is the same.
    """
    if normalise_transport(getattr(target, "transport", None)) == TRANSPORT_REST:
        return _RestWire(endpoint=endpoint, version=version, headers=headers)
    return _JsonRpcWire(endpoint=endpoint, version=version, headers=headers)


def _without_none(body: dict) -> dict:
    """Drop absent members rather than sending nulls a proto parser refuses."""
    return {k: v for k, v in body.items() if v is not None}


def _read_body_quietly(response) -> dict:
    """A body read for DIAGNOSIS only — never raises, whatever arrives."""
    try:
        return _read_body(response)
    except Exception:
        return {}


async def _within(deadline: float, start):
    """Await ``start()``, but never past the delegation's deadline.

    The client timeout is per request and a blocking send may legitimately
    use the whole ceiling, so only this bounds the delegation as a whole.
    ``start`` is a callable so a passed deadline creates no coroutine.
    """
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError("Delegation ceiling reached.")
    return await asyncio.wait_for(start(), remaining)


async def _poll_to_outcome(
    client,
    wire: _Wire,
    *,
    task_id: str,
    deadline: float,
    grace: float | None = None,
    submitted: bool = False,
) -> dict:
    """Poll until terminal, ``input-required``, or the deadline.

    Returns the last task payload. At the deadline it raises
    :class:`PartnerStillRunningError` once the partner has answered at least
    once — the task is alive and the attempt is over, which is a hand-back, not
    a failure — and :class:`TimeoutError` before that, which the caller treats
    as a partner that stopped answering.

    Running out of clock and a read the partner did not answer are different
    things, and only the second says anything about the partner. ``submitted``
    covers the first: a submission that came back with a task id proves the
    partner took the work, and under a grace period there may be no time for a
    status read to prove it again — so the deadline expiring is a hand-back.
    A read that actively times out is the partner having gone quiet, whatever
    the submission once showed, and stays the timeout it looks like.
    """
    answered = False
    # Where a callback will report the outcome, the attempt ends at the grace
    # period rather than the ceiling — long enough that short work still
    # answers in the calling agent's own turn, and no longer (FR-001).
    if grace is not None:
        deadline = min(deadline, asyncio.get_running_loop().time() + grace)
    while True:
        try:
            answer = await _within(deadline, lambda: wire.fetch(client, task_id))
        except TimeoutError:
            # The clock, not the partner: ``_within`` raises this when the
            # deadline is reached, so a submission is evidence enough.
            if not (answered or submitted):
                raise
            raise PartnerStillRunningError(task_id) from None
        except httpx.TimeoutException:
            # A read the partner did not answer. Never having had one, the
            # task may be dead or may never have existed, and calling that
            # "still running" would invent a fact.
            if not answered:
                raise
            raise PartnerStillRunningError(task_id) from None
        answered = True
        payload = answer.task or {}
        state = _task_state(payload)
        if state is None:
            raise UnrecognisedPartnerStateError(
                str((payload.get("status") or {}).get("state") or "<absent>")
            )
        if state in TERMINAL_STATES or state is A2AState.INPUT_REQUIRED:
            return payload
        remaining = deadline - asyncio.get_running_loop().time()
        await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, max(remaining, 0.01)))


def _log_outcome(integration, target, result) -> None:
    """Say, in the log, what happened to one delegation.

    Logged because the database row and the tool result are not enough: the
    calling model paraphrases the detail away, and the row is not where an
    operator investigating a silent agent looks first.

    What is deliberately NOT logged: the partner's own prose - the result text
    and any question it asked back. That is untrusted content of unknown
    sensitivity, and a log is the wrong place for it. Machine-readable reasons,
    our own diagnostics and the partner's task id are enough to act on.
    """
    integration_id = getattr(integration, "id", None)
    target_id = getattr(target, "id", None)
    if result.outcome == OUTCOME_COMPLETED:
        logger.info(
            "a2a.client: delegation completed - integration=%s target=%s "
            "task=%s version=%s",
            integration_id,
            target_id,
            result.partner_task_id or "-",
            result.protocol_version or "-",
        )
        return
    logger.warning(
        "a2a.client: delegation %s - integration=%s target=%s task=%s "
        "reason=%s detail=%s",
        result.outcome,
        integration_id,
        target_id,
        result.partner_task_id or "-",
        result.partner_reason or "-",
        result.detail or "(none given)",
    )


async def _outstanding_row(
    db, integration, partner_task_id: str | None, *, calling=None
):
    """The unconcluded row for this exchange, or ``None``.

    Keyed on the partner's task id within the integration, which is what a
    collect and a callback both arrive holding — and, when the caller is known,
    scoped to that caller exactly as ``_may_continue`` scopes the ownership
    check it mirrors.

    That symmetry is the point. This is the WRITE side: whatever it returns,
    ``_record`` concludes. A task id that reached here without passing the read
    side could otherwise end a delegation belonging to another run, whose
    caller is then never resumed. FR-046 gates the read; this gates the write,
    and the two must agree on whose row it is.
    """
    if not partner_task_id:
        return None
    from sqlalchemy import select

    from app.models.a2a import A2ADelegation

    stmt = select(A2ADelegation).where(
        A2ADelegation.integration_id == integration.id,
        A2ADelegation.partner_task_id == partner_task_id,
        A2ADelegation.outcome.is_(None),
    )
    return (await db.execute(_scoped_to_caller(stmt, calling))).scalars().first()


def _scoped_to_caller(stmt: Select[Any], calling: object) -> Select[Any]:
    """*stmt* narrowed to the delegations *calling* may continue (FR-046).

    The user always, when known. The run when the caller has one (``run_ref``
    is NULL for a chat delegation, where the user is what separates one caller
    from another) - OR a run of the SAME agent that has ended: a still-running
    task outlives the run that sent it (#12079), and a later run of that agent
    for that user is the only caller left to collect it. The same agent in
    the SAME conversation may also continue it (#448): a chat turn is its own
    run, so the turn that receives the partner's question or completion is
    never the turn that started the task. The same agent dispatched for the
    SAME work item may also collect it (#600): a run wound down by the
    iteration backstop releases its item before its own end is written, so
    the next run of that item starts while the starting run still reads as
    running. Another agent, another user, another conversation's or another
    work item's run still going stays refused.
    """
    from sqlalchemy import and_, or_, select

    from app.models.a2a import A2ADelegation
    from app.models.assignment import AssignmentRun
    from app.services.assignment_engine import TERMINAL_RUN_STATUSES

    user_id = getattr(calling, "user_id", None)
    if user_id is not None:
        stmt = stmt.where(A2ADelegation.user_id == user_id)
    run_ref = getattr(calling, "run_ref", None)
    if run_ref is None:
        return stmt
    agent_id = getattr(calling, "agent_id", None)
    if agent_id is None:
        return stmt.where(A2ADelegation.run_ref == run_ref)
    ended_runs = select(AssignmentRun.id).where(AssignmentRun.status.in_(TERMINAL_RUN_STATUSES))
    allowed = [
        A2ADelegation.run_ref == run_ref,
        and_(
            A2ADelegation.agent_id == agent_id,
            A2ADelegation.run_ref.in_(ended_runs),
        ),
    ]
    conversation_ref = getattr(calling, "conversation_ref", None)
    if conversation_ref is not None:
        allowed.append(
            and_(
                A2ADelegation.agent_id == agent_id,
                A2ADelegation.conversation_ref == conversation_ref,
            )
        )
    work_item_ref = getattr(calling, "work_item_ref", None)
    if work_item_ref is not None:
        context = AssignmentRun.trigger_context
        same_item_runs = select(AssignmentRun.id).where(
            AssignmentRun.agent_id == agent_id,
            context["type"].as_string() == "work_item",
            context["work_item_id"].as_string() == str(work_item_ref),
        )
        allowed.append(
            and_(
                A2ADelegation.agent_id == agent_id,
                A2ADelegation.run_ref.in_(same_item_runs),
            )
        )
    return stmt.where(or_(*allowed))


def _who(
    integration,
    target,
    calling,
    *,
    partner_task_id: str | None,
    protocol_version: str | None,
    signature_state: str | None,
) -> dict:
    """The columns that say who delegated to whom, on every row shape.

    One place, so a column added to the identity cannot reach one writer and
    not another.
    """
    return {
        "integration_id": integration.id,
        "target_id": getattr(target, "id", None),
        "agent_id": calling.agent_id,
        "user_id": calling.user_id,
        "run_ref": calling.run_ref,
        "conversation_ref": calling.conversation_ref,
        "partner_task_id": partner_task_id,
        "protocol_version": protocol_version or "",
        "signature_state": signature_state or "",
    }


async def _publish_pending_row(
    *, row_id, integration, target, calling, task_id: str, secret: str, version: str,
    signature_state: str, submitted_at,
) -> None:
    """Commit the delegation row as soon as a callback could arrive.

    A callback names this row, and a partner can call the moment its task
    finishes — which is while the caller's own turn is still open. The caller's
    transaction commits at the end of that turn, so a row written into it is
    invisible exactly when the callback needs it, and the receiver answers the
    uniform not-found it answers an unknown delegation with. The partner then
    burns a delivery attempt against a row it could never have found.

    So it is written here, in its own session, and committed: the caller's
    transaction is not this function's to commit, and the row has to outlive
    the question of whether that transaction ever succeeds.

    Best-effort. Failing means the callback will not authenticate and the floor
    collects the task instead — later than it should, and not lost.
    """
    from app.a2a.client.callbacks import store_secret
    from app.core.database import async_session
    from app.models.a2a import A2ADelegation

    try:
        async with async_session() as own_db:
            own_db.add(
                A2ADelegation(
                    id=row_id,
                    **_who(
                        integration,
                        target,
                        calling,
                        partner_task_id=task_id,
                        protocol_version=version,
                        signature_state=signature_state,
                    ),
                    mechanism=MECHANISM_CALLED_BACK,
                    callback_secret=store_secret(secret),
                    # Claimed by the attempt that is still polling. The route
                    # goes live the moment this commits, so work that finishes
                    # inside the grace period — the case the grace period is
                    # for — would otherwise have the callback collecting and
                    # resuming the caller while the inline poll reads the same
                    # terminal task: the answer delivered twice, and a second
                    # row. Given back the moment this attempt stops.
                    callback_claimed_at=datetime.now(UTC),
                    submitted_at=submitted_at,
                )
            )
            await own_db.commit()
    except Exception:
        logger.warning(
            "a2a.client: could not publish the row a callback needs for task %s",
            task_id,
            exc_info=True,
        )


#: Kept referenced: asyncio holds only weak references, so a bare
#: ``create_task`` can be collected before it has collected.
_BACKGROUND_COLLECTS: set = set()

#: The same, for the cancels a cancelled or reaped run sends onward.
_BACKGROUND_CANCELS: set = set()


async def _release_pending_claim(row_id) -> bool:
    """Give the callback claim back, and say whether a partner called.

    Its own transaction, for the reason ``_publish_pending_row`` has one: the
    caller's is not this function's to commit. A release left inside it is
    invisible to the collect that acts on it — ``act_on_callback`` opens a
    session of its own and re-reads the row — and is undone if the caller rolls
    back, dropping the callback this whole mechanism exists to keep. Relying on
    the row lock to serialise the two works, and works by accident: it blocks a
    pooled connection until the caller commits, which on the tool path is the
    end of the turn.

    The stamp is cleared with the release, and only the value that was read: it
    has been acted on at that point, so leaving it set would spawn a collect on
    every later release for as long as the row lived.
    """
    from sqlalchemy import select, update

    from app.core.database import async_session
    from app.models.a2a import A2ADelegation

    try:
        async with async_session() as own_db:
            seen = (
                await own_db.execute(
                    select(A2ADelegation.callback_seen_at).where(
                        A2ADelegation.id == row_id
                    )
                )
            ).scalar_one_or_none()
            await own_db.execute(
                update(A2ADelegation)
                .where(A2ADelegation.id == row_id)
                .values(callback_claimed_at=None)
            )
            if seen is not None:
                await own_db.execute(
                    update(A2ADelegation)
                    .where(
                        A2ADelegation.id == row_id,
                        A2ADelegation.callback_seen_at == seen,
                    )
                    .values(callback_seen_at=None)
                )
            await own_db.commit()
            return seen is not None
    except Exception:
        logger.warning(
            "a2a.client: could not release the callback claim on delegation %s",
            row_id,
            exc_info=True,
        )
        return False


#: How long shutdown waits for detached A2A work. Long enough for a collect
#: that is nearly done, short enough that a rolling restart does not notice.
_DRAIN_TIMEOUT_SECONDS = 5.0


async def drain_background_a2a() -> None:
    """Wait for detached A2A work before the process goes away.

    A collect holds the delegation's claim while it runs, so a process that
    stops in the middle of one leaves the row claimed until the claim lapses —
    a whole ceiling in which neither the floor nor a later callback can act.
    The sets keep these tasks from being garbage-collected; this is what makes
    a restart deterministic rather than a source of stranded claims.

    Both claim-holding sets are drained: the collect spawned when an attempt
    releases its claim, and the one the callback route spawns. So are the
    cancels a cancelled or reaped run spawns — a restart is the last moment
    anything will tell that partner to stop. Never raises: a shutdown path has
    nowhere to report to, and a task that failed has already logged.
    """
    from app.a2a.client.callbacks import BACKGROUND_CALLBACKS

    pending = (
        list(_BACKGROUND_COLLECTS)
        + list(BACKGROUND_CALLBACKS)
        + list(_BACKGROUND_CANCELS)
        + list(_BACKGROUND_TARGET_WRITES)
    )
    if not pending:
        return
    with suppress(Exception):
        # Bounded, because the trade above only holds while the wait is
        # shorter than the stranded claim it buys. A collect goes through the
        # ordinary delegation path, so it is bounded by the delegation ceiling
        # plus whatever the partner's connect takes — one slow partner would
        # otherwise hold the process in shutdown for the whole ceiling, on
        # every rolling restart. What does not finish in a few seconds was not
        # nearly done, and letting it lapse costs exactly the claim timeout
        # that not draining at all would have cost.
        await asyncio.wait(pending, timeout=_DRAIN_TIMEOUT_SECONDS)


def spawn_run_cancel(run_ids) -> None:
    """Stop what *run_ids* delegated onward, off the path that asked.

    Detached with its own session: reaching N partners is N network calls, and
    the caller is a partner waiting to be told its cancel took. Never raises —
    a partner that cannot be told must not turn a cancel or a sweep into a
    failure of its own.

    The fast path only. ``cascade_cancel`` already committed the obligation
    (``cancel_pending_at``) with the decision, so a process killed before this
    finishes leaves the client sweep to re-send it
    (``sweep.reissue_owed_cancels``).
    """
    from app.core.database import async_session

    ids = [rid for rid in run_ids if rid is not None]
    if not ids:
        return

    async def _run() -> None:
        # A session per run: the partner calls go out before the commit, so a
        # failure part-way through the tree must not roll back the rows whose
        # partners have already been told.
        for run_id in ids:
            with suppress(Exception):
                async with async_session() as own_db:
                    await cancel_outstanding_for(own_db, run_ref=run_id)
                    await own_db.commit()

    _bg = asyncio.create_task(_run())
    _BACKGROUND_CANCELS.add(_bg)
    _bg.add_done_callback(_BACKGROUND_CANCELS.discard)


def _spawn_collect(delegation_id) -> None:
    """Collect a delegation whose partner called while we held the claim."""
    from app.a2a.client.callbacks import act_on_callback

    _bg = asyncio.create_task(
        act_on_callback(delegation_id, mechanism=MECHANISM_CALLED_BACK)
    )
    _BACKGROUND_COLLECTS.add(_bg)
    _bg.add_done_callback(_BACKGROUND_COLLECTS.discard)


async def _record_outstanding(
    db,
    integration,
    target,
    calling,
    result,
    *,
    submitted_at,
    mechanism: str,
    row_id=None,
    callback_secret: str | None = None,
) -> None:
    """Persist a delegation that has been handed back, still running.

    Written now rather than at conclusion because three things need the row
    while the task is live: a collect, for its ownership check; the cancel
    cascade, to know what to cancel; and a partner's callback, to find the run
    to resume. An outstanding row carries no outcome and no ``terminal_at`` —
    the absence of a conclusion is what "still running" means.

    Best-effort on the same terms as ``_record``, and for the same reason: a
    bookkeeping failure must not cost the caller its transaction.

    Returns the id of a row whose callback claim the caller must release —
    this attempt published it and has stopped polling — or ``None``. The
    release is the caller's because it belongs outside this savepoint; see
    ``_release_pending_claim``.
    """
    if calling is None or db is None or not result.partner_task_id:
        return
    from app.models.a2a import A2ADelegation

    try:
        async with db.begin_nested():
            existing = await _outstanding_row(
                db, integration, result.partner_task_id, calling=calling
            )
            if existing is not None:
                # Published before the grace poll, and claimed by it. This
                # attempt has stopped now, so the claim goes back — in its own
                # transaction, outside this savepoint: see
                # ``_release_pending_claim``.
                release_id = existing.id
                return release_id
            from app.a2a.client.callbacks import store_secret

            db.add(
                A2ADelegation(
                    id=row_id or uuid.uuid4(),
                    **_who(
                        integration,
                        target,
                        calling,
                        partner_task_id=result.partner_task_id,
                        protocol_version=result.protocol_version,
                        signature_state=result.signature_state,
                    ),
                    mechanism=mechanism,
                    callback_secret=(
                        store_secret(callback_secret) if callback_secret else None
                    ),
                    continuation_count=result.continuation_count,
                    submitted_at=submitted_at,
                )
            )
            await db.flush()
    except Exception:
        logger.warning(
            "a2a.client: could not record outstanding delegation for integration %s",
            integration.id,
            exc_info=True,
        )


async def _record(db, integration, target, calling, result, *, submitted_at) -> None:
    """Persist one delegation attempt.

    Written for EVERY attempt including a pre-flight refusal, because "we
    refused to send this" is a fact an operator investigating a silent agent
    needs as much as a failure at the partner. The row's lifetime follows the
    run it belongs to (FR-044).

    Best-effort, and a SAVEPOINT is what makes that true rather than merely
    intended. Catching the exception is not enough on its own: a failed flush
    leaves the session requiring a rollback, so the CALLER's commit then
    raises ``PendingRollbackError`` and a delegation that actually succeeded
    is reported to the calling agent as a platform failure — precisely the
    outcome this contract exists to prevent. The failed row also stays pending
    in the session, so without a rollback it re-raises at whatever flushes
    next, attributing this failure to unrelated work.

    ``begin_nested`` confines both: the savepoint is rolled back, the pending
    row goes with it, and the surrounding transaction stays usable. The record
    still commits with the caller's transaction when it succeeds, which is why
    a separate session is the wrong tool here — the row belongs to the same
    unit of work as everything else the delegation wrote.

    Logged, because a silently missing record is how an audit trail stops
    being one.
    """
    _log_outcome(integration, target, result)
    if calling is None or db is None:
        return
    from app.models.a2a import A2ADelegation

    try:
        async with db.begin_nested():
            existing = await _outstanding_row(
                db, integration, result.partner_task_id, calling=calling
            )
            if existing is not None:
                # Handed back earlier and now concluded: the same exchange, so
                # the same row. A second would make one delegation look like
                # two to every count an operator reads.
                existing.outcome = result.outcome
                existing.failure_kind = getattr(target, "last_failure_kind", None)
                existing.partner_reason = result.partner_reason
                existing.continuation_count = result.continuation_count
                existing.terminal_at = datetime.now(UTC)
            else:
                db.add(
                    A2ADelegation(
                        **_who(
                            integration,
                            target,
                            calling,
                            partner_task_id=result.partner_task_id,
                            protocol_version=result.protocol_version,
                            signature_state=result.signature_state,
                        ),
                        # Concluded inside one attempt, so nothing else was
                        # ever going to report it (FR-020).
                        mechanism=MECHANISM_ASKED,
                        outcome=result.outcome,
                        failure_kind=getattr(target, "last_failure_kind", None),
                        partner_reason=result.partner_reason,
                        continuation_count=result.continuation_count,
                        submitted_at=submitted_at,
                        terminal_at=datetime.now(UTC),
                    )
                )
            await db.flush()
    except Exception:
        logger.warning(
            "a2a.client: could not record delegation for integration %s",
            integration.id,
            exc_info=True,
        )


#: Every target column a delegation writes from its outcome: the breaker's
#: counters and the usability a durable failure sets.
_OUTCOME_COLUMNS = (
    "consecutive_transient_failures",
    "circuit_open_until",
    "last_failure_kind",
    "usability",
    "unusable_reason",
    "last_verification_error",
)


#: How long a delegation polls for a target row another session holds before
#: it hands the outcome write to a detached task. Short: the caller waits.
_TARGET_OUTCOME_WAIT_SECONDS = 1.0
_TARGET_OUTCOME_POLL_SECONDS = 0.1

#: The detached write waits for the row at most this long (``lock_timeout``),
#: then gives the counts up rather than queue behind a stuck holder.
_TARGET_OUTCOME_LOCK_TIMEOUT = "30s"

#: Kept referenced like the collects: the detached outcome writes.
_BACKGROUND_TARGET_WRITES: set[asyncio.Task[None]] = set()

#: The row was written and committed.
_WRITTEN = "written"
#: Another session holds the row.
_HELD = "held"
#: The row no longer exists.
_GONE = "gone"


async def _write_target_now(target_id: Any, changed: dict[str, Any]) -> str:
    """Claim the target row with ``SKIP LOCKED`` in a session of its own and write *changed*."""
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.a2a import A2AClientTarget

    async with async_session() as own_db:
        row = (
            await own_db.execute(
                select(A2AClientTarget).where(A2AClientTarget.id == target_id).with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if row is None:
            exists = (
                await own_db.execute(select(A2AClientTarget.id).where(A2AClientTarget.id == target_id))
            ).scalar_one_or_none()
            await own_db.rollback()
            return _HELD if exists is not None else _GONE
        for name, value in changed.items():
            setattr(row, name, value)
        await own_db.commit()
    return _WRITTEN


async def _write_target_when_free(target_id: Any, changed: dict[str, Any]) -> None:
    """Write *changed* once the holder of the row lets it go, bounded by ``lock_timeout``.

    Detached from the delegation: the wait is this task's, never the run's.
    Never raises; a write that cannot land loses these counts, logged.
    """
    from sqlalchemy import select, text

    from app.core.database import async_session
    from app.models.a2a import A2AClientTarget

    try:
        async with async_session() as own_db:
            await own_db.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": _TARGET_OUTCOME_LOCK_TIMEOUT},
            )
            row = (
                await own_db.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id).with_for_update())
            ).scalar_one_or_none()
            if row is None:
                await own_db.rollback()
                return
            for name, value in changed.items():
                setattr(row, name, value)
            await own_db.commit()
    except Exception:
        logger.warning(
            "a2a.client: could not write the outcome of target %s once its holder let it go; counts lost",
            target_id,
            exc_info=True,
        )


async def _commit_target_outcome(target: Any) -> None:
    """Write what this delegation changed on its target in a session of its own.

    The breaker and the usability a send sets are facts about the partner,
    not part of the calling run's unit of work. Flushed by the caller's
    session, the UPDATE takes the target row lock inside the run's own
    transaction and holds it until the run commits, which may be the rest
    of the run: every other delegation to that partner that re-verifies
    claims the row with ``SKIP LOCKED`` and is refused as
    ``verification_contended`` meanwhile.

    Written and committed at once, then marked committed on *target* so the
    caller's session writes nothing for it. A row another session holds is
    polled for a moment, then written by a detached task once its holder
    lets it go: never left to the caller's session, whose next flush would
    wait for that holder and then keep the lock for the rest of the run.
    The caller may itself be the holder: the detached write then lands once
    the run commits. A target the caller's session does not persist (a stub,
    a transient row) is left as it is.
    """
    if not hasattr(target, "_sa_instance_state"):
        return
    import time

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm.attributes import set_committed_value

    state = sa_inspect(target)
    if not state.persistent:
        return
    changed = {name: getattr(target, name) for name in _OUTCOME_COLUMNS if state.attrs[name].history.has_changes()}
    if not changed:
        return
    target_id = target.id
    deadline = time.monotonic() + _TARGET_OUTCOME_WAIT_SECONDS
    try:
        while True:
            outcome = await _write_target_now(target_id, changed)
            if outcome != _HELD or time.monotonic() >= deadline:
                break
            await asyncio.sleep(_TARGET_OUTCOME_POLL_SECONDS)
    except Exception:
        logger.warning(
            "a2a.client: could not write the outcome of target %s on its own; left to the caller's transaction",
            getattr(target, "id", None),
            exc_info=True,
        )
        return
    if outcome == _GONE:
        return
    if outcome == _HELD:
        _bg = asyncio.create_task(_write_target_when_free(target_id, dict(changed)))
        _BACKGROUND_TARGET_WRITES.add(_bg)
        _bg.add_done_callback(_BACKGROUND_TARGET_WRITES.discard)
    for name, value in changed.items():
        set_committed_value(target, name, value)


async def _conclude(
    db, integration, target, calling, result: DelegationResult, *, submitted_at: datetime
) -> DelegationResult:
    """Count one delegation's outcome, record it, and hand it back.

    The single exit of :func:`delegate`, so no path can record a row without
    counting it. A refusal is its own series: it cost the partner nothing.
    """
    from app.a2a import metrics as a2a_metrics

    await _commit_target_outcome(target)
    if result.outcome == OUTCOME_REFUSED_PREFLIGHT:
        a2a_metrics.record_delegation_refused(
            integration.provider, result.partner_reason or "unknown"
        )
    else:
        a2a_metrics.record_delegation(integration.provider, result.outcome)
    await _record(db, integration, target, calling, result, submitted_at=submitted_at)
    return result


async def _refuse_collect_for_now(
    integration: Any, target: Any, result: DelegationResult, task_id: str, *, message_unsent: bool = False
) -> DelegationResult:
    """Hand back a retryable refusal of a collect without recording it.

    Counted like any refusal, but never passed to ``_record``: with the task
    id it would conclude the outstanding row, without it it would insert a
    concluded second one. The row stays outstanding for the caller's next
    collect and for the floor. ``message_unsent`` says the attempt carried a
    follow-up message, which the hint then asks the caller to send again.
    """
    from app.a2a import metrics as a2a_metrics

    await _commit_target_outcome(target)
    a2a_metrics.record_delegation_refused(integration.provider, result.partner_reason or "unknown")
    result.partner_task_id = task_id
    hint = _RETRY_SEND_HINT if message_unsent else _RETRY_COLLECT_HINT
    result.detail = f"{result.detail} {hint}" if result.detail else hint
    return result


@dataclass(frozen=True)
class CancelOutcome:
    """What a teardown achieved, as two facts rather than one.

    ``cancelled`` is the partners that were told to stop; ``concluded`` is the
    rows that ended locally because there was nobody left to tell. Only the
    first supports "the partner has been told to stop", which is what a user
    acts on.

    Truthy and int-able on the total, for the callers that only ask whether
    there was anything.
    """

    cancelled: int = 0
    concluded: int = 0

    @property
    def total(self) -> int:
        return self.cancelled + self.concluded

    def __bool__(self) -> bool:
        return bool(self.total)

    def __int__(self) -> int:
        return self.total


async def _outstanding_rows(
    db,
    *,
    run_ref=None,
    conversation_ref=None,
    partner_task_id: str | None = None,
    integration_id=None,
    delegation_id=None,
    submitted_before=None,
) -> list:
    """Unconcluded delegations in scope, as ``(row, integration)`` pairs.

    The scope is the run or the conversation, never the user's whole history:
    a task id is not a capability, so naming someone else's reaches nothing.

    ``partner_task_id`` narrows to one piece of work, ``integration_id`` to one
    partner — the cancel tool is built per registration, and an agent may be
    bound to two (FR-042). Both narrowings apply together: a task id is the
    partner's to choose, so two partners may issue the same string.

    ``delegation_id`` is the one scope that needs no run or conversation: the
    client sweep re-sending an owed cancel names the row it found, and
    no model ever supplies it.

    ``submitted_before`` keeps work sent after that moment out of scope: a
    conversation Stop is acknowledged before its cascade runs, and a turn the
    user starts in between delegates work the Stop never covered.
    """
    if run_ref is None and conversation_ref is None and delegation_id is None:
        return []
    from sqlalchemy import select

    from app.models.a2a import A2ADelegation
    from app.models.integration import Integration

    conditions = [
        A2ADelegation.outcome.is_(None),
        A2ADelegation.partner_task_id.isnot(None),
    ]
    if delegation_id is not None:
        conditions.append(A2ADelegation.id == delegation_id)
    else:
        scope = (
            A2ADelegation.run_ref if run_ref is not None else A2ADelegation.conversation_ref
        )
        conditions.append(scope == (run_ref if run_ref is not None else conversation_ref))
    if partner_task_id is not None:
        conditions.append(A2ADelegation.partner_task_id == str(partner_task_id))
    if integration_id is not None:
        conditions.append(A2ADelegation.integration_id == integration_id)
    if submitted_before is not None:
        conditions.append(A2ADelegation.submitted_at <= submitted_before)
    return list(
        (
            await db.execute(
                select(A2ADelegation, Integration)
                .join(Integration, Integration.id == A2ADelegation.integration_id)
                .where(*conditions)
                .order_by(A2ADelegation.submitted_at.desc())
            )
        ).all()
    )


async def outstanding_for_listing(
    db, *, run_ref=None, conversation_ref=None
) -> list[dict]:
    """Partner work this run or conversation still has running.

    For the background-task list: a delegation is not a ``SubAgentTask``, so
    without this the list a model reaches for when asked what is running holds
    no partner work and answers "nothing", which reads as "it finished".
    """
    return [
        {
            "delegation_id": str(row.id),
            "partner": integration.name,
            "status": "running",
            "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        }
        for row, integration in await _outstanding_rows(
            db, run_ref=run_ref, conversation_ref=conversation_ref
        )
    ]


async def cancel_outstanding_for(
    db,
    *,
    run_ref=None,
    conversation_ref=None,
    partner_task_id: str | None = None,
    integration_id=None,
    delegation_id=None,
    submitted_before=None,
) -> CancelOutcome:
    """Cancel unconcluded partner tasks in scope. FR-005.

    Scoped and narrowed by ``_outstanding_rows``. Two scopes, because the
    boundary is the work that would collect the result, not the turn that
    started it: a chat turn ends the moment a task is handed back, so
    cancelling there would cancel everything seconds after creating it. What
    ends collection is an assignment run reaching its outcome, or a user
    abandoning the conversation.

    Every row reached ends here, whatever happened at the partner: left
    outstanding it survives its run, and the floor then re-collects it on every
    sweep and resumes a caller that finished long ago. ``mechanism`` moves with
    the outcome — FR-020 makes it the column an operator reads to answer how a
    delegation ended where it did, and a row cancelled locally did not get
    there by being called back.

    Best-effort per task: a partner that cannot be told must not turn a
    teardown into a failure of its own.
    """
    cancelled = 0
    concluded = 0
    for row, integration in await _outstanding_rows(
        db,
        run_ref=run_ref,
        conversation_ref=conversation_ref,
        partner_task_id=partner_task_id,
        integration_id=integration_id,
        delegation_id=delegation_id,
        submitted_before=submitted_before,
    ):
        target = await _target_for(db, row.target_id)
        told = False
        if target is None:
            logger.warning(
                "a2a.client: no target left for outstanding task %s; "
                "concluding it locally",
                row.partner_task_id,
            )
        else:
            try:
                await cancel(integration, target, row.partner_task_id)
                told = True
            except Exception:
                logger.warning(
                    "a2a.client: could not cancel outstanding task %s at the "
                    "partner; concluding it locally",
                    row.partner_task_id,
                    exc_info=True,
                )
        # The outcome discharges an owed cancel whether or not the partner
        # could be told - a retry would not change what this row records.
        # ``cancel_pending_at`` stays: it is the record that the caller was
        # cancelled, which a collect that raced this cancel reads.
        row.outcome = OUTCOME_CANCELLED
        row.terminal_at = datetime.now(UTC)
        row.mechanism = MECHANISM_ASKED
        if told:
            cancelled += 1
        else:
            concluded += 1
    return CancelOutcome(cancelled=cancelled, concluded=concluded)


async def mark_cancel_owed(
    db, *, run_refs=None, conversation_ref=None, submitted_before=None
) -> int:
    """Record that the outstanding partner tasks in scope are owed a cancel.

    The partner is told later, off the path that decided (``spawn_run_cancel``
    and the conversation cascade detach it), so without this the obligation
    exists only in memory: a hard kill after the decision commits loses it,
    and nothing else ever cancels at the partner - abandonment by design
    cancels nothing. Written through the CALLER's session so it
    commits with the decision; the client sweep re-sends what is still owed
    (``sweep.reissue_owed_cancels``). Returns how many rows it marked.

    Only rows not already marked, so a repeated cascade keeps the first time
    the cancel was owed, which is what the sweep's grace is measured from.
    ``submitted_before`` leaves out work sent after the decision, as in
    ``cancel_outstanding_for``.
    """
    from sqlalchemy import update

    from app.models.a2a import A2ADelegation

    if run_refs is not None:
        refs = [r for r in run_refs if r is not None]
        if not refs:
            return 0
        scope = A2ADelegation.run_ref.in_(refs)
    elif conversation_ref is not None:
        scope = A2ADelegation.conversation_ref == conversation_ref
    else:
        return 0
    conditions = [
        scope,
        A2ADelegation.outcome.is_(None),
        A2ADelegation.partner_task_id.isnot(None),
        A2ADelegation.cancel_pending_at.is_(None),
    ]
    if submitted_before is not None:
        conditions.append(A2ADelegation.submitted_at <= submitted_before)
    result = await db.execute(
        update(A2ADelegation)
        .where(*conditions)
        .values(cancel_pending_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def _instance_identity(db: Any) -> dict[str, Any] | None:
    """This instance's identity as outbound metadata, or ``None`` if unreadable.

    The Instance ID of Settings > Parameters (``PLATFORM_ID`` when forced) and
    the resolved display name: the two public values ``GET /platform/config``
    serves. A read that fails sends nothing rather than failing the delegation.
    """
    try:
        from app.api.v1.platform_settings_helpers import get_platform_settings
        from app.services.branding import get_cached_branding

        settings_row = await get_platform_settings(db)
        return instance_metadata(str(settings_row.id), get_cached_branding().name)
    except Exception:
        logger.warning("a2a.client: instance identity unreadable, sent without it")
        return None


async def _target_for(db, target_id):
    """The registration an outstanding delegation was sent under, or ``None``."""
    if target_id is None:
        return None
    from sqlalchemy import select

    from app.models.a2a import A2AClientTarget

    return (
        await db.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id))
    ).scalar_one_or_none()


async def _sibling_owner_run(db: Any, integration: Any, target: Any, calling: Any, task_id: str) -> uuid.UUID | None:
    """The still-running run of the SAME agent and user that holds *task_id*.

    Only read to word a ``continuation_not_owned`` refusal (#600): a sibling
    run of this agent owns the task, so the answer is to wait for it, never to
    resend. Scoped to the caller's agent and user, so it names nothing another
    agent or user owns. ``None`` when there is no such run or it cannot be
    read: the refusal then stays generic.
    """
    from sqlalchemy import select

    from app.models.a2a import A2ADelegation

    agent_id = getattr(calling, "agent_id", None)
    user_id = getattr(calling, "user_id", None)
    if agent_id is None or user_id is None:
        return None
    stmt = select(A2ADelegation.run_ref).where(
        A2ADelegation.integration_id == integration.id,
        A2ADelegation.partner_task_id == task_id,
        A2ADelegation.agent_id == agent_id,
        A2ADelegation.user_id == user_id,
        A2ADelegation.run_ref.is_not(None),
    )
    target_id = getattr(target, "id", None)
    if target_id is not None:
        stmt = stmt.where(A2ADelegation.target_id == target_id)
    try:
        owner = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    except Exception:
        logger.debug("a2a.client: owner run of task %s not read", task_id, exc_info=True)
        return None
    return owner if isinstance(owner, uuid.UUID) else None


def _not_owned_detail(owner_run: uuid.UUID | None) -> str:
    """The ``continuation_not_owned`` refusal text (#600).

    Never "send it again": a resend duplicates the work on the partner's side.
    When a still-running run of the same agent owns the task, it is named and
    the caller is told to collect once it ends or from the completion event.
    """
    if owner_run is not None:
        return (
            f"Run {owner_run} of this agent started that task and is still "
            "recorded as running. Do not resend: collect it again (same "
            "continue_task_id, no message) once that run ends, or let the run "
            "the a2a.delegation.completed event starts collect it."
        )
    return (
        "No task this run may continue has that id. Pass the full task id the "
        "first call returned (a shortened id is resolved only when it is "
        "unique among your own tasks). If it was passed whole, another caller "
        "started that task. Do not resend work already delegated; a new "
        "delegation is only for work nobody sent."
    )


# The shortest continue_task_id read as a prefix (#978): the 8 characters an
# agent keeps from an id it was shown. Shorter matches too much to be meant.
_TASK_ID_PREFIX_MIN = 8


async def _resolve_owned_prefix(db: Any, integration: Any, target: Any, calling: Any, task_id: str) -> str | None:
    """The one task id *task_id* is a prefix of, among the caller's own, or ``None``.

    Read only after the exact ownership check failed (#978): a shortened id of
    the caller's own task is resolved instead of refused as another caller's.
    Scoped by ``_scoped_to_caller`` exactly like ``_may_continue``, so it never
    matches what another agent, user or run owns. Too short, ambiguous or
    unreadable gives ``None`` and the refusal stands.
    """
    if len(task_id) < _TASK_ID_PREFIX_MIN:
        return None
    from sqlalchemy import select

    from app.models.a2a import A2ADelegation

    stmt = select(A2ADelegation.partner_task_id).where(
        A2ADelegation.integration_id == integration.id,
        A2ADelegation.partner_task_id.startswith(task_id, autoescape=True),
    )
    target_id = getattr(target, "id", None)
    if target_id is not None:
        stmt = stmt.where(A2ADelegation.target_id == target_id)
    stmt = _scoped_to_caller(stmt, calling).distinct().limit(2)
    try:
        matches = [m for m in (await db.execute(stmt)).scalars().all() if isinstance(m, str) and m]
    except Exception:
        logger.debug("a2a.client: task id prefix %s not resolved", task_id, exc_info=True)
        return None
    return matches[0] if len(matches) == 1 else None


async def _may_continue(db, integration, target, calling, task_id: str) -> bool:
    """Whether THIS caller may continue the task it names (FR-046).

    Matched on integration, target and user, plus the run when the caller has
    one — ``run_ref`` is NULL for a chat delegation, where the user is what
    stops one user continuing another's task and reading the answer. A run of
    the same agent and user may also collect a task whose starting run has
    ended (``_scoped_to_caller``, #12079).
    """
    from sqlalchemy import select

    from app.models.a2a import A2ADelegation

    stmt = select(A2ADelegation.id).where(
        A2ADelegation.integration_id == integration.id,
        A2ADelegation.partner_task_id == task_id,
    )
    target_id = getattr(target, "id", None)
    if target_id is not None:
        stmt = stmt.where(A2ADelegation.target_id == target_id)
    stmt = _scoped_to_caller(stmt, calling)
    return (await db.execute(stmt.limit(1))).scalar_one_or_none() is not None


# Outcomes a collect answers from the caller's own row: only what the partner
# itself decided. A timeout or a cancel is recorded after a best-effort cancel
# at the partner whose result is not stored (``_cancel_at_partner`` and
# ``cancel_outstanding_for`` swallow its failure), so the partner's task may
# still be running or finished: those rows, like anything this side concluded
# on a transient failure of its own call, are asked of the partner again.
_READ_BACK_OUTCOMES = frozenset({OUTCOME_PARTNER_FAILED})


async def _recorded_conclusion(
    db: Any, integration: Any, target: Any, calling: CallingRun | None, task_id: str
) -> DelegationResult | None:
    """The caller's own concluded row for *task_id*, read back, or ``None``.

    Answered only when nothing about the task is left to learn: no row of this
    caller is still outstanding for it, and the newest one ended with an
    outcome the partner itself decided (``_READ_BACK_OUTCOMES``: a failure).
    That is what the calling agent asked about and could not read before
    (no tool returned the row). A completed row is never answered from here
    (the answer text is not stored, a collect re-reads it from the partner),
    nor an abandoned one (this side gave up on a silent partner, whose answer
    a collect may still get), nor a ``timeout`` or ``cancelled`` one (the
    cancel at the partner is best effort and its result is not recorded), nor
    one this side concluded on a transient failure of its own call
    (``unreachable``, ``credential_rejected``, a pre-flight refusal such as
    ``circuit_open``): the partner's task may still be running or finished, so
    the collect goes to the partner again.
    """
    from sqlalchemy import select

    from app.models.a2a import A2ADelegation

    stmt = select(A2ADelegation).where(
        A2ADelegation.integration_id == integration.id,
        A2ADelegation.partner_task_id == task_id,
    )
    target_id = getattr(target, "id", None)
    if target_id is not None:
        stmt = stmt.where(A2ADelegation.target_id == target_id)
    stmt = _scoped_to_caller(stmt, calling).order_by(A2ADelegation.submitted_at.desc())
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows or any(row.outcome is None for row in rows):
        return None
    row = rows[0]
    if row.outcome not in _READ_BACK_OUTCOMES or row.abandoned_at is not None:
        return None
    ended = row.terminal_at.isoformat() if row.terminal_at else "an unrecorded time"
    return DelegationResult(
        outcome=row.outcome,
        partner_reason=row.partner_reason,
        detail=(
            f"This delegation already ended as '{row.outcome}' at {ended}"
            f" (waited by {row.mechanism or 'an unrecorded mechanism'})."
            " This is the recorded outcome: nothing was sent to the partner."
            " Send the work as a new delegation to try again."
        ),
        partner_task_id=task_id,
        protocol_version=row.protocol_version,
        signature_state=row.signature_state,
        continuation_count=row.continuation_count or 0,
    )


async def delegate(
    db,
    integration,
    target,
    *,
    message: str = "",
    continue_task_id: str | None = None,
    calling: CallingRun | None = None,
    ceiling_seconds: float | None = None,
) -> DelegationResult:
    """Submit one unit of work and follow it to an outcome. Never raises.

    Never raising is the contract the tool depends on: a calling agent must
    get an outcome it can reason about for every case, and an exception
    escaping here would surface as a tool error indistinguishable from a
    platform bug.
    """
    submitted_at = datetime.now(UTC)

    # The task this attempt is entitled to conclude. NOT ``continue_task_id``:
    # that is a partner task id the calling model supplied verbatim, and until
    # ``_may_continue`` has passed it may name another run's work. ``_record``
    # matches a row on integration + task id + unconcluded, without the run and
    # user scoping the ownership check applies — so concluding from an
    # unchecked id ends the legitimate caller's row, which the floor and the
    # teardown then both skip. Set below once the id is this run's, and moved
    # to the submitted task's id once there is one.
    owned_task_id: str | None = None
    if continue_task_id:
        # Resolved BEFORE the pre-flight, though the refusal below stays where
        # it is: a pre-flight refusal of a collect — the circuit opening, a
        # verdict going stale — has to be able to conclude the row it was
        # collecting, and it cannot do that without knowing the id is ours.
        continuation_owned = await _may_continue(
            db, integration, target, calling, continue_task_id
        )
        if not continuation_owned:
            # #978: a unique prefix of the caller's own task id names it.
            resolved = await _resolve_owned_prefix(db, integration, target, calling, continue_task_id)
            if resolved is not None:
                continue_task_id = resolved
                continuation_owned = True
        if continuation_owned:
            owned_task_id = continue_task_id
            if not message:
                # A collect of a delegation that already ended: the caller's
                # only way to read its own row (outcome, reason, when). Read
                # back, never recorded again, nothing sent.
                recorded = await _recorded_conclusion(db, integration, target, calling, continue_task_id)
                if recorded is not None:
                    return recorded

    def conclude(result: DelegationResult):
        if owned_task_id and not result.partner_task_id:
            # A refusal after the row exists still concerns that row, and only
            # the task id can find it. Without one, ``_record`` inserts a
            # SECOND row and leaves the first outstanding — which the floor
            # re-picks every cycle, resuming the caller again each time.
            result.partner_task_id = owned_task_id
        return _conclude(db, integration, target, calling, result, submitted_at=submitted_at)

    try:
        await preflight(db, integration, target)
    except PreflightRefusedError as refusal:
        refused = DelegationResult(
            outcome=OUTCOME_REFUSED_PREFLIGHT,
            partner_reason=refusal.reason,
            detail=refusal.detail,
        )
        if owned_task_id and refusal.reason in RETRYABLE_PREFLIGHT_REASONS:
            # A transient refusal of a collect says nothing about the task:
            # concluding its row would end a delegation that is still running,
            # which the floor then skips and the next collect duplicates.
            return await _refuse_collect_for_now(
                integration, target, refused, owned_task_id, message_unsent=bool(message)
            )
        concluded: DelegationResult = await conclude(refused)
        return concluded

    if continue_task_id and not continuation_owned:
        # FR-046. The task id is the ONE field of a delegation the calling
        # model supplies verbatim, so without this a model could resume a task
        # this run never started — another run's, or another partner's — and
        # read the answer.
        return await conclude(
            DelegationResult(
                outcome=OUTCOME_REFUSED_PREFLIGHT,
                partner_reason="continuation_not_owned",
                detail=_not_owned_detail(await _sibling_owner_run(db, integration, target, calling, continue_task_id)),
            )
        )

    # Pre-flight refused a row without one, so this is set.
    endpoint = target.invocation_url
    version = target.agreed_protocol_version
    # The partner's dedup key for this submission (v0.3 requires it). Minted
    # once here rather than per request, so a retry inside this call is the
    # same submission to the partner rather than a second run.
    submission_id = str(uuid.uuid4())
    signature_state = target.signature_state
    # Allocated up front because the callback URL names it, and the partner is
    # told that URL in the same request that submits the work — before there is
    # any row to take an id from.
    delegation_id = uuid.uuid4()
    # Continuing with nothing to say is a collect: has the partner finished?
    # The same argument rather than a second tool, because a tool's description
    # is serialised into the model's schema on every turn of every run bound to
    # this partner.
    collecting = bool(continue_task_id) and not (message or "").strip()
    # The caller may bound its own attempt below the configured ceiling. A
    # sweep asking "is it terminal yet" of many rows in turn needs an answer,
    # not a full wait: at the ceiling one still-running partner costs the whole
    # cycle, and twenty-five of them cost more than the interval between
    # cycles.
    # ``is None``, not ``or``: zero is a legitimate bound — hand back at once,
    # no grace — and would otherwise restore the full ceiling on exactly the
    # collect this argument exists to shorten.
    ceiling = (
        app_settings.a2a_client_delegation_ceiling_seconds
        if ceiling_seconds is None
        else ceiling_seconds
    )
    deadline = asyncio.get_running_loop().time() + ceiling
    task_id = continue_task_id

    async with httpx.AsyncClient(
        # Per request: a blocking send may run the whole ceiling. The
        # delegation as a whole is bounded by ``_within``.
        timeout=httpx.Timeout(ceiling, connect=_CONNECT_TIMEOUT_SECONDS),
        follow_redirects=False,
    ) as client:
        try:
            headers = await outbound_headers(integration, client)
        except CredentialError as exc:
            breaker.record_failure(target, CREDENTIAL_CANNOT_MINT)
            target.usability = USABILITY_UNUSABLE
            target.unusable_reason = CREDENTIAL_CANNOT_MINT.reason
            target.last_verification_error = str(exc)
            # The integration's own status follows its credential (#12346):
            # ``error`` with the reason, owner and admins told once.
            await credential_state.record_mint_failure(integration, str(exc))
            rejected = DelegationResult(
                outcome=OUTCOME_CREDENTIAL_REJECTED,
                detail=f"{exc} {credential_state.repair_hint(integration)}",
                protocol_version=version,
                signature_state=signature_state,
            )
            return await conclude(rejected)
        except Exception as exc:
            # The token endpoint, not the partner: transient, so it counts
            # toward the circuit and marks nothing unusable. Caught because
            # this function never raises and ``outbound_headers`` reaches
            # the network.
            if isinstance(exc, httpx.ConnectTimeout):
                logger.warning(
                    "a2a.client: token mint connect timeout - integration=%s %s",
                    getattr(integration, "id", None),
                    _connect_timeout_context(),
                )
            breaker.record_failure(target, classify_exception(exc))
            unreachable = DelegationResult(
                outcome=OUTCOME_UNREACHABLE,
                detail=(
                    "Could not mint a token for this partner — "
                    f"{type(exc).__name__}: {exc}"
                ),
                protocol_version=version,
                signature_state=signature_state,
            )
            return await conclude(unreachable)

        def _unreachable(exc: Exception) -> DelegationResult:
            """The partner was not reached. Counts toward the circuit."""
            breaker.record_failure(target, classify_exception(exc))
            return DelegationResult(
                outcome=OUTCOME_UNREACHABLE,
                detail=f"{type(exc).__name__}: {exc}",
                partner_task_id=task_id,
                protocol_version=version,
                signature_state=signature_state,
            )

        # FR-023: the agreed version is declared explicitly on every request,
        # never left to a partner's default.
        headers["A2A-Version"] = version or ""

        try:
            invocation = target.pinned_origin
            # FR-034 again at request time, not only at verification: a card
            # re-read between then and now could have moved.
            assert_same_origin(endpoint, invocation)

            # The binding recorded at verification, never one chosen now.
            wire = wire_for(target, endpoint=endpoint, version=version or "", headers=headers)
            # Who is calling, only where the operator opted this integration
            # in (#708). Off by default: the message stays the text alone.
            if sends_instance_identity(integration):
                wire.metadata = await _instance_identity(db)

            # Ask to be told, where the partner offers it and this deployment
            # can be reached. Registering discloses this deployment's address,
            # which is why an operator can refuse it outright.
            callback_secret = None
            if not collecting and getattr(target, "declares_callbacks", False):
                from app.a2a.client import callbacks as callback_svc

                if await callback_svc.deployment_is_reachable():
                    callback_secret = callback_svc.mint_secret()
                    wire.push_config = {
                        "url": callback_svc.callback_url(delegation_id),
                        "token": callback_secret,
                    }

            # The wire shapes its own message: what goes in the body is a
            # property of the binding, and only partly of the version.
            if collecting:
                # A collect asks how the partner's task is doing and sends
                # nothing, because there is nothing to say. An unset state
                # drops straight into the poll below.
                payload: dict = {}
                state = None
            else:
                answer = await _within(
                    deadline,
                    lambda: wire.submit(
                        client,
                        text=message,
                        submission_id=submission_id,
                        task_id=task_id,
                    ),
                )

                if answer.task is None and answer.message is not None:
                    # The partner answered outright rather than opening a
                    # task. Nothing to poll, nothing to cancel, and no task id
                    # to record.
                    breaker.record_success(target)
                    immediate = DelegationResult(
                        outcome=OUTCOME_COMPLETED,
                        result_text=_extract_message_text(answer.message),
                        partner_task_id=task_id,
                        protocol_version=version,
                        signature_state=signature_state,
                        continuation_count=1 if continue_task_id else 0,
                    )
                    return await conclude(immediate)

                payload = answer.task or {}
                task_id = str(payload.get("id") or task_id or "")
                # The partner has named the task, so this attempt owns it —
                # including on a FIRST submission, where a failure after the
                # pending row was published would otherwise conclude with no
                # id at all: a second row, and the published one left
                # outstanding with its claim held.
                owned_task_id = task_id or owned_task_id
                state = _task_state(payload)

                if callback_secret and task_id and calling is not None:
                    # Before the partner can possibly call: it has the URL now.
                    await _publish_pending_row(
                        row_id=delegation_id,
                        integration=integration,
                        target=target,
                        calling=calling,
                        task_id=task_id,
                        secret=callback_secret,
                        version=version or "",
                        signature_state=signature_state or "",
                        submitted_at=submitted_at,
                    )

            if state is None or state not in TERMINAL_STATES:
                payload = await _poll_to_outcome(
                    client,
                    wire,
                    task_id=task_id,
                    deadline=deadline,
                    grace=(
                        app_settings.a2a_client_grace_seconds
                        if callback_secret
                        else None
                    ),
                    # Evidence the task exists, which is what separates "the
                    # partner has not finished" from "there may be nothing
                    # there". A submission returned the id; a collect owns the
                    # row it is collecting, which ``_may_continue`` proved
                    # before this attempt began. Either way the deadline is a
                    # hand-back — and saying otherwise makes it a CANCEL,
                    # which for a bounded collect stops work that is still
                    # running and blames the partner for it.
                    submitted=True,
                )
                state = _task_state(payload)

            if state is A2AState.INPUT_REQUIRED:
                breaker.record_success(target)
                # The question is the status message; an artifact produced
                # before asking is a partial result, not the question.
                said = _status_text(payload)
                produced = _artifact_text(payload)
                asked = DelegationResult(
                    outcome=OUTCOME_COMPLETED,
                    question=_bounded(said or produced) or "The partner needs more input.",
                    result_text=_bounded(produced) if said and produced else None,
                    partner_task_id=task_id,
                    protocol_version=version,
                    signature_state=signature_state,
                    continuation_count=1 if continue_task_id else 0,
                )
                return await conclude(asked)
            if state is A2AState.COMPLETED:
                breaker.record_success(target)
                done = DelegationResult(
                    outcome=OUTCOME_COMPLETED,
                    result_text=_extract_text(payload),
                    partner_task_id=task_id,
                    protocol_version=version,
                    signature_state=signature_state,
                    continuation_count=1 if continue_task_id else 0,
                )
                return await conclude(done)
            outcome = (
                OUTCOME_CANCELLED
                if state is A2AState.CANCELED
                else OUTCOME_PARTNER_FAILED
            )
            result = DelegationResult(
                outcome=outcome,
                partner_reason=_terminal_reason(payload),
                detail=_terminal_detail(state, payload),
                partner_task_id=task_id,
                protocol_version=version,
                signature_state=signature_state,
            )
        # On every failure below ``detail`` is ours and ``partner_reason`` is
        # the partner's words; the tool envelopes only the second.
        except PartnerRpcError as exc:
            # Not counted against the breaker: a considered answer from a
            # reachable partner, and retrying an invalid request cannot succeed.
            # TaskNotFoundError on a request naming a task is the JSON-RPC twin
            # of the HTTP 404/410 branch below: the task ended or was purged,
            # which says nothing about the partner; the agent must not retry
            # the collect, it sends the work again.
            rpc_detail = (
                "Partner no longer knows this task (A2A TaskNotFoundError): it "
                "has ended or was purged on the remote side. Do not collect it "
                "again; send the work as a new delegation."
                if task_id and exc.is_task_not_found
                else "Partner rejected the request with a JSON-RPC error."
            )
            result = DelegationResult(
                outcome=OUTCOME_PARTNER_FAILED,
                partner_reason=f"{exc.code}: {exc.message}",
                detail=rpc_detail,
                partner_task_id=task_id,
                protocol_version=version,
                signature_state=signature_state,
            )
        except PartnerLostTaskError as exc:
            # Not counted against the breaker and not marked unusable: the
            # partner is plainly reachable, it just does not keep tasks.
            result = DelegationResult(
                outcome=OUTCOME_PARTNER_FAILED,
                partner_reason=exc.detail or None,
                detail=(
                    "The partner does not retain the task it created, so its "
                    "answer cannot be fetched — it may only answer a blocking "
                    "send."
                ),
                partner_task_id=exc.task_id,
                protocol_version=version,
                signature_state=signature_state,
            )
        except UnrecognisedPartnerStateError as exc:
            result = DelegationResult(
                outcome=OUTCOME_PARTNER_FAILED,
                partner_reason=str(exc) or None,
                detail=(
                    "Partner reported an unrecognised task state — one no "
                    "supported A2A version defines."
                ),
                partner_task_id=task_id,
                protocol_version=version,
                signature_state=signature_state,
            )
        except _PartnerHttpError as exc:
            if task_id and refuses_the_named_task(exc.status):
                # The partner refused the task this request named - ended, or
                # never kept - not the registration. Not counted against the
                # breaker either: a considered answer from a reachable partner.
                result = DelegationResult(
                    outcome=OUTCOME_PARTNER_FAILED,
                    partner_reason=exc.partner_said or None,
                    detail=(
                        f"Partner refused this task (HTTP {exc.status}): it has "
                        "ended or the partner no longer knows it. Send the work "
                        "as a new delegation."
                    ),
                    partner_task_id=task_id,
                    protocol_version=version,
                    signature_state=signature_state,
                )
            else:
                breaker.record_failure(target, exc.classification)
                if exc.classification.is_durable:
                    target.usability = USABILITY_UNUSABLE
                    target.unusable_reason = exc.classification.reason
                if exc.status in (401, 403):
                    forget_token(integration)
                outcome = (
                    OUTCOME_CREDENTIAL_REJECTED
                    if exc.status in (401, 403)
                    else OUTCOME_UNREACHABLE
                )
                result = DelegationResult(
                    outcome=outcome,
                    partner_reason=exc.partner_said or None,
                    detail=exc.detail or f"Partner returned HTTP {exc.status}.",
                    partner_task_id=task_id,
                    protocol_version=version,
                    signature_state=signature_state,
                )
        except httpx.ConnectTimeout as exc:
            # Ahead of the timeout branch below, which it is a subclass of:
            # nothing reached the partner, so there is nothing to cancel.
            result = _unreachable(exc)
        except PartnerStillRunningError as still:
            # FR-001. The attempt is over and the partner's task is not, which
            # is not a failure and must not cancel: the work is real, it is
            # paid for, and a later collect returns it. Recorded as an
            # outstanding row so a collect can prove ownership, the cancel
            # cascade can find it, and a callback can find the run.
            breaker.record_success(target)
            await _commit_target_outcome(target)
            running = DelegationResult(
                outcome=OUTCOME_STILL_RUNNING,
                partner_task_id=still.task_id,
                protocol_version=version,
                signature_state=signature_state,
                continuation_count=1 if continue_task_id else 0,
                awaiting_callback=bool(callback_secret),
            )
            release_id = await _record_outstanding(
                db,
                integration,
                target,
                calling,
                running,
                submitted_at=submitted_at,
                mechanism=(
                    MECHANISM_CALLED_BACK if callback_secret else MECHANISM_ASKED
                ),
                row_id=delegation_id,
                callback_secret=callback_secret,
            )
            if release_id is not None and await _release_pending_claim(release_id):
                # A partner called while this attempt held the claim, so its
                # task is finished and nothing else will be told. Collected now
                # that the claim is back and committed, rather than left to the
                # floor — which would not look at this row for another cycle.
                _spawn_collect(release_id)
            _log_outcome(integration, target, running)
            # Deliberately not ``conclude``: nothing has concluded.
            return running
        except (TimeoutError, httpx.TimeoutException):
            # ``_within`` at the deadline with the partner never having
            # answered, or one stalled request — ``httpx.ReadTimeout`` is not
            # a ``TimeoutError``.
            await _cancel_at_partner(client, wire, task_id)
            breaker.record_failure(target, classify_exception(TimeoutError()))
            result = DelegationResult(
                outcome=OUTCOME_TIMEOUT,
                detail=f"No terminal state within {ceiling}s.",
                partner_task_id=task_id,
                protocol_version=version,
                signature_state=signature_state,
            )
        except DestinationRefusedError as exc:
            result = DelegationResult(
                outcome=OUTCOME_REFUSED_PREFLIGHT,
                partner_reason=exc.reason,
                detail=str(exc),
                protocol_version=version,
                signature_state=signature_state,
            )
        except Exception as exc:
            result = _unreachable(exc)

    return await conclude(result)


async def _cancel_at_partner(client, wire: _Wire, task_id: str) -> None:
    """Best effort: tell the partner to stop working on an abandoned task.

    Best effort because the alternative is worse. A cancel that fails must not
    turn a timeout into an error the calling agent has to handle - the
    delegation is already over from our side, and the partner's own timeouts
    are its business.
    """
    if not task_id:
        return
    try:
        await asyncio.wait_for(wire.cancel(client, task_id), _CANCEL_TIMEOUT_SECONDS)
    except Exception:
        logger.info("a2a.client: could not cancel task %s at the partner", task_id)


async def cancel(integration, target, task_id: str) -> None:
    """Cancel an outstanding partner task from outside the delegation.

    Used when the CALLING run is cancelled or fails with a task still open
    (FR-025), and when a run ends with a partner still awaiting input.

    Sent to the INVOCATION endpoint, never ``card_url``: the discovery route
    is GET-only, and a best-effort cancel aimed there fails silently and
    leaves partner tasks running.
    """
    # Deliberately not gated on chat availability: this is teardown of work
    # that was offered when it started, and leaving the partner working toward
    # an answer nobody will give is worse. The message carries only the task
    # id.
    endpoint = target.invocation_url
    if not endpoint:
        logger.info(
            "a2a.client: no invocation endpoint recorded, cannot cancel task %s",
            task_id,
        )
        return
    version = target.agreed_protocol_version or ""
    async with httpx.AsyncClient(
        timeout=_CANCEL_TIMEOUT_SECONDS, follow_redirects=False
    ) as client:
        try:
            headers = await outbound_headers(integration, client)
        except Exception:
            # Best effort, like the cancel itself: the partner's own timeouts
            # retire the task, and the run teardown must not fail over it.
            logger.info(
                "a2a.client: could not mint a token to cancel task %s", task_id
            )
            return
        headers["A2A-Version"] = version
        await _cancel_at_partner(
            client,
            wire_for(target, endpoint=endpoint, version=version, headers=headers),
            task_id,
        )

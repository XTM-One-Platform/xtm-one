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

"""One of two modules permitted to import ``a2a_sdk`` — the shaping half.

Everything else in ``app/a2a`` and in the API layer speaks in platform terms
and calls into here. See the package docstring for why the boundary exists.

The other permitted importer is :mod:`app.a2a.client.sdk`, which contains the
SDK's *client* half — card resolution and JWS signature verification. That is
a genuinely different concern from the type and version shaping below: there
is exactly one **shaper**, and it is here. What both sides preserve is the
invariant that matters: ``a2a_sdk`` types never leave ``app/a2a``.
``tests/test_a2a_client_sdk_containment.py`` pins the two-importer list.

What lives here:

- protocol-version negotiation,
- the v1.0 <-> v0.3 shaping of every value that crosses the wire,
- run-status -> A2A task-state mapping,
- the structured JSON-RPC error envelope.

Two shapers would eventually disagree about what "v0.3" means, and the
disagreement would show up as a partner seeing a card that does not match the
task surface it describes — which is why version shaping lives here and not
in ``card.py``, even though the card path needs it first.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final
from urllib.parse import quote

# --- the containment boundary: a2a_sdk imports appear ONLY below this line ---
from a2a.compat.v0_3.versions import (
    PROTOCOL_VERSION_0_3,
    PROTOCOL_VERSION_1_0,
)
from a2a.types import TaskState as _SdkTaskState

# --- and nowhere else in the file, or the codebase ---------------------------

#: Header a caller uses to declare which protocol version it speaks.
VERSION_HEADER: Final = "A2A-Version"

SUPPORTED_VERSIONS: Final = (PROTOCOL_VERSION_1_0, PROTOCOL_VERSION_0_3)

#: An absent version header means v1.0: both target platforms speak v1.0 and
#: neither declares a version when it fetches a card. A v0.3 partner asks by
#: header, and on the task surface by its method names.
DEFAULT_VERSION: Final = PROTOCOL_VERSION_1_0


def major_minor(value: object) -> str:
    """``Major.Minor``, which is what the specification compares.

    Both vendors publish a patch level — the reference SDK's ``AgentCard``
    defaults ``protocolVersion`` to ``0.3.0`` — so every comparison against a
    partner-declared version has to normalise, and through THIS function, so
    they all normalise the same way. Two comparisons normalising differently
    read one partner two ways: negotiation accepts ``0.3.0`` as ``0.3``, and
    downgrade detection then reports the partner as having WITHDRAWN ``0.3``.
    """
    return ".".join(str(value).strip().split(".")[:2])


def offered_versions(declared: Any) -> set[str]:
    """Everything a partner declares, normalised for comparison."""
    if not declared:
        return set()
    return {major_minor(v) for v in declared if str(v).strip()}


def still_offers(declared: Any, agreed: str | None) -> bool:
    """Whether *agreed* is still on offer, compared as negotiation compares it."""
    if not agreed:
        return True
    return major_minor(agreed) in offered_versions(declared)


def _version_key(value: str) -> tuple[int, ...]:
    """``Major.Minor`` as integers, so versions order numerically.

    Total by construction. These strings come from a partner's card, and the
    callers sit under one documented never to raise, so a component ``int``
    refuses is skipped rather than propagated - whatever the reason it
    refuses. Pre-testing the characters covers only the reasons already
    thought of: ``str.isdigit`` admits non-ASCII digits, and even an ASCII
    one is refused past the interpreter's conversion limit.
    """
    parts: list[int] = []
    for component in major_minor(value).split("."):
        try:
            parts.append(int(component))
        except ValueError:
            continue
    return tuple(parts)


def only_newer_than(declared: Any, agreed: str | None) -> bool:
    """Whether everything *declared* is strictly newer than *agreed*.

    Separates a partner that upgraded off the agreed version from one that
    withdrew it. Numeric, because ``"1.0" < "0.3"`` as strings.
    """
    if not agreed:
        return False
    offered = offered_versions(declared)
    if not offered:
        return False
    agreed_key = _version_key(agreed)
    return all(_version_key(v) > agreed_key for v in offered)


def negotiate_outbound_version(
    partner_versions: list[str] | tuple[str, ...] | None,
) -> str | None:
    """The highest version this platform and *partner_versions* both support.

    ``None`` when there is nothing in common, which makes the registration
    unusable rather than being attempted on a guess (FR-051).

    Outbound callers MUST use this and MUST NOT fall back to
    :data:`DEFAULT_VERSION`. That constant is right below and would look
    correct, but it encodes an INBOUND rule: what to serve a caller that
    declares nothing. Outbound we are the client, and FR-023 requires the
    version be declared explicitly on every request — so a partner that
    advertises nothing is a partner we have not agreed a version with, not a
    partner we assume a version of.

    Distinct from :func:`negotiate_version`, which resolves an INBOUND
    caller's declared header and does fall back to the default. Same word,
    opposite direction; keeping both names in one module is deliberate so the
    contrast is visible at the point of use.
    """
    offered = offered_versions(partner_versions)
    for supported in SUPPORTED_VERSIONS:  # ordered highest-first
        if supported in offered:
            return supported
    return None


class A2AState(StrEnum):
    """Platform-side names for A2A task states.

    Deliberately not the SDK enum: the rest of the package must not have to
    import ``a2a_sdk`` to talk about a task's state.

    The values are the **v0.3 wire values** (``canceled`` with one L,
    whatever the specification's prose suggests — the SDK enum is the
    authority). They are NOT the v1.0 wire values: the v1.0 JSON-RPC binding
    is proto-JSON, and the reference client parses responses with
    ``json_format.ParseDict``, whose enum fields accept only the proto enum
    NAMES (``TASK_STATE_COMPLETED``), never these spellings. The authority is
    always what the SDK *parses*, never its enum types — those agree across
    versions where the parsers do not. :func:`state_wire` is the one place
    the per-version spelling happens; ``test_a2a_protocol_shaping``
    round-trips every state through the reference parser.
    """

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"


#: ``AssignmentRun.status`` -> A2A state. The platform lifecycle is
#: ``pending -> delayed -> running -> awaiting_human -> completed/failed``,
#: plus ``cancelled``, which existed already as a status reserved "for a
#: future explicit-cancellation feature" - this surface is that feature.
_RUN_STATUS_TO_STATE: Final[dict[str, A2AState]] = {
    "pending": A2AState.SUBMITTED,
    "delayed": A2AState.SUBMITTED,
    "running": A2AState.WORKING,
    "awaiting_human": A2AState.INPUT_REQUIRED,
    "completed": A2AState.COMPLETED,
    "failed": A2AState.FAILED,
    "cancelled": A2AState.CANCELED,
    "skipped": A2AState.FAILED,
}

TERMINAL_STATES: Final = frozenset(
    {A2AState.COMPLETED, A2AState.FAILED, A2AState.CANCELED, A2AState.REJECTED}
)

_SDK_STATE_BY_A2A: Final[dict[A2AState, int]] = {
    A2AState.SUBMITTED: _SdkTaskState.TASK_STATE_SUBMITTED,
    A2AState.WORKING: _SdkTaskState.TASK_STATE_WORKING,
    A2AState.INPUT_REQUIRED: _SdkTaskState.TASK_STATE_INPUT_REQUIRED,
    A2AState.COMPLETED: _SdkTaskState.TASK_STATE_COMPLETED,
    A2AState.FAILED: _SdkTaskState.TASK_STATE_FAILED,
    A2AState.CANCELED: _SdkTaskState.TASK_STATE_CANCELED,
    A2AState.REJECTED: _SdkTaskState.TASK_STATE_REJECTED,
}


class UnsupportedVersionError(ValueError):
    """Raised when a caller declares a protocol version we do not speak."""


def negotiate_version(declared: str | None) -> str:
    """Resolve the protocol version for a request.

    ``None`` (header absent) yields :data:`DEFAULT_VERSION`. An unrecognised
    value raises rather than falling back, because guessing on this surface
    would silently serve a partner a shape it cannot parse.
    """
    if declared is None or not declared.strip():
        return DEFAULT_VERSION
    value = declared.strip()
    if value in SUPPORTED_VERSIONS:
        return value
    # Accept a bare major.minor even when the caller sends a patch level:
    # the specification uses Major.Minor for compatibility decisions.
    short = ".".join(value.split(".")[:2])
    if short in SUPPORTED_VERSIONS:
        return short
    raise UnsupportedVersionError(
        f"Unsupported A2A protocol version {declared!r}. "
        f"Supported: {', '.join(SUPPORTED_VERSIONS)}"
    )


def is_legacy(version: str) -> bool:
    return version == PROTOCOL_VERSION_0_3


# --- Method vocabulary ---------------------------------------------------
# The two versions do not share a JSON-RPC method namespace: v0.3 uses
# path-style names, v1.0 renamed every method to its gRPC service method.
# The reference SDK's v1.0 client sends NO ``A2A-Version`` header on any
# request — it derives its dialect from the card — so on the task surface
# the method name is the only reliable statement of which parser awaits the
# response, and it outranks the header. (The header still matters on the
# card route, where there is no method name to read.)

#: method name -> (canonical operation, the version whose wire it belongs to)
_OPS_BY_METHOD: Final[dict[str, tuple[str, str]]] = {
    "message/send": ("send", PROTOCOL_VERSION_0_3),
    "tasks/get": ("get", PROTOCOL_VERSION_0_3),
    "tasks/cancel": ("cancel", PROTOCOL_VERSION_0_3),
    "SendMessage": ("send", PROTOCOL_VERSION_1_0),
    "GetTask": ("get", PROTOCOL_VERSION_1_0),
    "CancelTask": ("cancel", PROTOCOL_VERSION_1_0),
}


#: The push-notification config family, in both vocabularies. Served: a card
#: declaring ``pushNotifications: true`` must answer these, and the four
#: operations are the same four whichever namespace a partner speaks.
_PUSH_OPS_BY_METHOD: Final[dict[str, tuple[str, str]]] = {
    "tasks/pushNotificationConfig/set": ("push_set", PROTOCOL_VERSION_0_3),
    "tasks/pushNotificationConfig/get": ("push_get", PROTOCOL_VERSION_0_3),
    "tasks/pushNotificationConfig/list": ("push_list", PROTOCOL_VERSION_0_3),
    "tasks/pushNotificationConfig/delete": ("push_delete", PROTOCOL_VERSION_0_3),
    "CreateTaskPushNotificationConfig": ("push_set", PROTOCOL_VERSION_1_0),
    "GetTaskPushNotificationConfig": ("push_get", PROTOCOL_VERSION_1_0),
    "ListTaskPushNotificationConfigs": ("push_list", PROTOCOL_VERSION_1_0),
    "DeleteTaskPushNotificationConfig": ("push_delete", PROTOCOL_VERSION_1_0),
}

#: The push operations, for a caller that needs to know it is on that surface
#: without re-listing the method names.
PUSH_OPERATIONS: Final = frozenset(
    {"push_set", "push_get", "push_list", "push_delete"}
)


def resolve_method(method: Any) -> tuple[str, str] | None:
    """Resolve a JSON-RPC method name to ``(operation, version)``.

    ``None`` for anything outside the vocabulary — including v1.0 methods this
    surface deliberately does not serve (``ListTasks``), which get the same
    ``UnsupportedOperationError`` an unknown string gets.
    """
    if not isinstance(method, str):
        return None
    return _OPS_BY_METHOD.get(method) or _PUSH_OPS_BY_METHOD.get(method)


#: What a CALLER names each operation, derived from the tables above so that
#: every operation a server reads is one a caller can name.
_METHODS_BY_VERSION: Final[dict[str, dict[str, str]]] = {
    version: {
        operation: method
        for method, (operation, method_version) in {
            **_OPS_BY_METHOD,
            **_PUSH_OPS_BY_METHOD,
        }.items()
        if method_version == version
    }
    for version in SUPPORTED_VERSIONS
}


def method_for(operation: str, version: str) -> str:
    """The name *operation* is called by in *version*'s vocabulary.

    A server resolves the response shape from the method name, which
    outranks the caller's header, so the two must agree or the answer comes
    back in a shape the caller's own parser rejects. Neither argument is
    defaulted: guessing a vocabulary sends a partner a method it answers
    ``-32601`` to, on every request.
    """
    declared = major_minor(version)
    vocabulary = _METHODS_BY_VERSION.get(declared)
    if vocabulary is None:
        raise ValueError(
            f"No A2A method vocabulary for version {version!r}. "
            f"Known: {', '.join(_METHODS_BY_VERSION)}"
        )
    try:
        return vocabulary[operation]
    except KeyError:
        raise ValueError(
            f"No A2A {declared} method for operation {operation!r}. "
            f"Known: {', '.join(sorted(vocabulary))}"
        ) from None


# --- Transport bindings ------------------------------------------------------
# A2A defines three: JSON-RPC 2.0, gRPC, and HTTP+JSON/REST. They are not
# variants of one wire — a client that implements one does not reach a partner
# serving another — and a card names which it serves.
#
# This platform SERVES only JSONRPC (``a2a.card`` declares exactly that) and
# CONSUMES two. The asymmetry is deliberate: serving a second binding is a
# second public surface to secure, while consuming one is a path through code
# that already exists. Google's Agent Platform serves its A2A surface as
# HTTP+JSON only, so without this the ``google_a2a`` provider verified a
# partner's card and then failed every delegation against it.

TRANSPORT_JSONRPC: Final = "JSONRPC"
TRANSPORT_REST: Final = "HTTP+JSON"
TRANSPORT_GRPC: Final = "GRPC"

#: Ordered by preference, highest first — the same shape as
#: :data:`SUPPORTED_VERSIONS`, and read the same way by the interface
#: selection in ``a2a.client.verification``. JSON-RPC comes first because it
#: is what this platform serves and therefore what its own federation path
#: exercises on every test run; REST is reached only when a partner offers
#: nothing else.
SUPPORTED_TRANSPORTS: Final = (TRANSPORT_JSONRPC, TRANSPORT_REST)

#: A card that names no transport is a JSON-RPC card. Stated by the
#: specification for ``preferredTransport``, and the reference SDK's own
#: v0.3 -> v1.0 conversion hard-codes the same default
#: (``preferred_transport or 'JSONRPC'``).
DEFAULT_TRANSPORT: Final = TRANSPORT_JSONRPC

#: Spellings seen in the wild, mapped onto the canonical token. The
#: specification's own value for REST carries a ``+``, which is exactly the
#: character a partner is most likely to spell differently.
_TRANSPORT_ALIASES: Final[dict[str, str]] = {
    "JSONRPC": TRANSPORT_JSONRPC,
    "JSON-RPC": TRANSPORT_JSONRPC,
    "JSON_RPC": TRANSPORT_JSONRPC,
    "HTTP+JSON": TRANSPORT_REST,
    "HTTP_JSON": TRANSPORT_REST,
    "HTTP-JSON": TRANSPORT_REST,
    "HTTPJSON": TRANSPORT_REST,
    "REST": TRANSPORT_REST,
    "GRPC": TRANSPORT_GRPC,
}


# --- Wire dialects ----------------------------------------------------------
# THREE, not two, and the third is the one that cost two round trips to a live
# partner. v0.3 has two representations of itself: pydantic models for its
# JSON-RPC binding, and a proto for its REST and gRPC ones. They disagree about
# the field that carries a message's content.
#
# | binding  | version | message content | part discriminator | role      |
# |----------|---------|-----------------|--------------------|-----------|
# | JSON-RPC | 0.3     | ``parts``       | ``kind``           | user      |
# | JSON-RPC | 1.0     | ``parts``       | -                  | ROLE_USER |
# | proto    | 0.3     | **``content``** | -                  | ROLE_USER |
# | proto    | 1.0     | ``parts``       | -                  | ROLE_USER |
#
# Vertex AI Agent Engine serves v0.3 over REST, so the proto applies and
# ``parts`` there is an unknown field. Observed, in order, against one partner:
#
#   ParseError: Message type "a2a.v1.Message" has no field named "kind"
#   HTTP 400                                    (then: ...named "parts")
#
# Which is also why this is a table and not a pair of booleans. Two of the
# three axes - the field name, the discriminator, the role spelling - move
# independently, and each time one was inferred from another it was wrong.

DIALECT_JSONRPC_V03: Final = "jsonrpc/0.3"
DIALECT_PROTO_V03: Final = "proto/0.3"
DIALECT_PROTO_V10: Final = "proto/1.0"


def dialect_for(transport: str | None, version: str) -> str:
    """Which dialect belongs on the wire for this (binding, version).

    v1.0's JSON-RPC binding is proto-JSON too, so it shares a dialect with the
    proto bindings: there are four combinations and three answers.
    """
    if not is_legacy(version):
        return DIALECT_PROTO_V10
    if transport == TRANSPORT_JSONRPC:
        return DIALECT_JSONRPC_V03
    return DIALECT_PROTO_V03


def outbound_message(
    text: str,
    *,
    dialect: str,
    message_id: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    """One caller-side ``Message``, spelled for *dialect*.

    Carries ONLY the composed text (FR-032). ``messageId`` is required by v0.3
    and is the receiving surface's dedup key. ``task_id`` rides on the MESSAGE,
    which is where every version puts it - no send-parameters type has a task
    id of its own.

    Written as an explicit table rather than derived from booleans, because
    every time one of these three fields was inferred from another the answer
    was wrong for some real partner.
    """
    if dialect == DIALECT_JSONRPC_V03:
        message: dict[str, Any] = {
            "kind": "message",
            "role": "user",
            "parts": [{"kind": "text", "text": text}],
        }
    elif dialect == DIALECT_PROTO_V03:
        message = {"role": "ROLE_USER", "content": [{"text": text}]}
    else:
        message = {"role": "ROLE_USER", "parts": [{"text": text}]}

    message["messageId"] = message_id
    if task_id:
        message["taskId"] = task_id
    return message


def outbound_configuration(dialect: str) -> dict[str, Any] | None:
    """Ask for a BLOCKING send: hold the response until the task is terminal.

    ``None`` for v1.0, where blocking is already the default and the flag is
    inverted (``returnImmediately``) - so sending nothing asks for the same
    thing.

    Not an optimisation. Vertex AI Agent Engine accepts a non-blocking send,
    answers ``TASK_STATE_SUBMITTED`` with a task id, and then cannot find that
    task: ``GET /v1/tasks/{id}`` is 404 immediately and stays 404. It keeps no
    task store across requests, so the ONLY way to get an answer out of it is
    to be holding the connection when the answer arrives.

    Which is the symmetric fact to one this platform already knew from the
    other direction: its own inbound surface implements blocking send because
    ADK's client never polls. Both halves of the reference implementation
    assume blocking; polling is the fallback, not the norm.
    """
    if dialect in (DIALECT_JSONRPC_V03, DIALECT_PROTO_V03):
        return {"blocking": True}
    return None


def outbound_push_field(dialect: str) -> str:
    """What ``configuration`` calls a callback registration in *dialect*.

    Three spellings for one idea: v0.3's JSON-RPC binding says
    ``pushNotificationConfig``, its proto binding named the field
    ``pushNotification``, and v1.0 renamed it again and widened the type to a
    ``TaskPushNotificationConfig``. The body is the same either way — every
    version's type carries ``url`` and ``token`` — so only the key moves.

    Getting it wrong is silent on a v0.3 REST partner, which ignores the
    unknown field and registers nothing while this side has already minted a
    secret and told its calling agent the answer will arrive by itself.
    """
    if dialect == DIALECT_JSONRPC_V03:
        return "pushNotificationConfig"
    if dialect == DIALECT_PROTO_V03:
        return "pushNotification"
    return "taskPushNotificationConfig"


def read_parts(container: Any) -> list:
    """The parts of a Message or an Artifact, whichever field carries them.

    Read tolerantly and with no version logic at all: an Artifact says
    ``parts`` in both protos, a Message says ``content`` in the v0.3 one, and a
    reader that had to know which was looking at would get it wrong in exactly
    the place it matters - the status message of an ``input-required`` task,
    which is the partner's question.
    """
    if not isinstance(container, dict):
        return []
    for field in ("parts", "content"):
        value = container.get(field)
        if isinstance(value, list):
            return value
    return []


def normalise_transport(value: Any) -> str | None:
    """The canonical token for a partner-declared transport, or ``None``.

    ``None`` for anything unrecognised rather than a guess: an unknown binding
    is one we cannot speak, and treating it as the default would aim JSON-RPC
    envelopes at a surface that never asked for them.
    """
    if not isinstance(value, str):
        return None
    return _TRANSPORT_ALIASES.get(value.strip().upper().replace(" ", ""))


def transport_supported(transport: str | None) -> bool:
    return transport in SUPPORTED_TRANSPORTS


# The REST binding's paths, which differ between the two versions in one way
# that matters: v0.3's proto declares them with the version in the path
# (``post: "/v1/message:send"``), while v1.0's reference server mounts them
# under a prefix the deployment chooses and the card's interface URL carries
# — so its client appends the bare path. Getting this wrong is a 404 on every
# submission, so it is derived from the negotiated version rather than
# guessed, and ``a2a.client.delegation`` retries once with the other
# convention on a 404 rather than marking a partner unusable over a prefix.


def rest_prefix(version: str) -> str:
    return "/v1" if is_legacy(version) else ""


def rest_alternate_prefix(version: str) -> str:
    """The other convention, for the one-shot retry."""
    return "" if is_legacy(version) else "/v1"


def rest_send_path(version: str, *, prefix: str | None = None) -> str:
    base = rest_prefix(version) if prefix is None else prefix
    return f"{base}/message:send"


#: The proto binding addresses a task by RESOURCE NAME, ``tasks/{task_id}``,
#: while ``Task.id`` is the bare identifier. A partner following Google's API
#: conventions may hand back either, and joining a resource name onto the
#: collection path yields ``/v1/tasks/tasks%2Fabc`` and a 404 on every poll.
_TASK_RESOURCE_PREFIX: Final = "tasks/"


def rest_task_path(task_id: str, version: str, *, prefix: str | None = None) -> str:
    base = rest_prefix(version) if prefix is None else prefix
    name = task_id.removeprefix(_TASK_RESOURCE_PREFIX)
    # Quoted whole: an id is one path segment, so a partner-supplied slash
    # must not become one.
    return f"{base}/tasks/{quote(name, safe='')}"


def rest_cancel_path(task_id: str, version: str, *, prefix: str | None = None) -> str:
    return f"{rest_task_path(task_id, version, prefix=prefix)}:cancel"


def blocking_requested(configuration: Any, version: str) -> bool:
    """Whether this send asked the server to hold the response until terminal.

    The two versions spell this with OPPOSITE polarities, and the difference
    is behavioural, not cosmetic. v0.3 has ``configuration.blocking``,
    default false — Foundry sends no flag and polls ``tasks/get`` itself.
    v1.0 inverted it to ``returnImmediately``, default false — i.e. **a
    blocking send is the v1.0 default**, and the reference client leans on
    that: it has no polling loop at all, makes exactly one ``SendMessage``
    call, and renders whatever task state comes back as the final answer —
    so a task returned as SUBMITTED reaches the user as an empty reply while
    the run completes unread.

    Both key spellings are accepted for the v1.0 flag because proto-JSON
    parsers accept both; emitters use camelCase.
    """
    cfg = configuration if isinstance(configuration, dict) else {}
    if is_legacy(version):
        return bool(cfg.get("blocking"))
    return not (cfg.get("returnImmediately") or cfg.get("return_immediately"))


def declared_push_config(configuration: Any) -> dict | None:
    """A callback registration carried on the submission itself, if any.

    Every spelling this surface can be sent is accepted: a partner's version is
    its own, and we serve both on one route. v1.0's type nests nothing extra
    that matters here — ``url`` and ``token`` sit at the same level in both —
    so the caller reads one shape whichever arrived.

    Both key spellings are accepted for each, because proto-JSON parsers accept
    both; emitters use camelCase.
    """
    cfg = configuration if isinstance(configuration, dict) else {}
    for key in (
        "pushNotificationConfig",
        "push_notification_config",
        "pushNotification",
        "push_notification",
        "taskPushNotificationConfig",
        "task_push_notification_config",
    ):
        declared = cfg.get(key)
        if declared:
            return declared
    return None


def run_status_to_state(status: str | None) -> A2AState:
    """Map an ``AssignmentRun.status`` onto an A2A state.

    An unrecognised status maps to ``FAILED`` rather than to a working state.
    That is the fail-closed direction: a partner polling forever because the
    platform grew a status this mapping does not know is worse than a partner
    told the work did not succeed.
    """
    if status is None:
        return A2AState.SUBMITTED
    return _RUN_STATUS_TO_STATE.get(status, A2AState.FAILED)


def sdk_state_value(state: A2AState) -> int:
    """The SDK's protobuf enum value, for callers that need the wire enum."""
    return _SDK_STATE_BY_A2A[state]


def state_wire(state: A2AState, version: str) -> str:
    """Spell *state* for the wire of *version*.

    v0.3 uses the lowercase spellings (``A2AState``'s own values); the v1.0
    JSON-RPC binding is proto-JSON, whose enum fields carry the proto enum
    NAME. Derived from the SDK's enum rather than hand-copied, for the same
    reason the v0.3 spellings live in a tested enum: the reference parser is
    the authority, and a string it does not accept fails the partner's
    deserialization of every response.
    """
    if is_legacy(version):
        return state.value
    return _SdkTaskState.Name(_SDK_STATE_BY_A2A[state])


#: Every spelling a partner may use for a task state, mapped onto the platform
#: name. Built from the enum rather than hand-listed so a state added to
#: :class:`A2AState` cannot be readable in one direction only.
_STATE_BY_WIRE: Final[dict[str, A2AState]] = {}
for _state in A2AState:
    # v0.3's own spelling, the underscore variant proto-JSON parsers emit for
    # it, and the v1.0 proto enum NAME.
    _STATE_BY_WIRE[_state.value] = _state
    _STATE_BY_WIRE[_state.value.replace("-", "_")] = _state
    _STATE_BY_WIRE[_SdkTaskState.Name(_SDK_STATE_BY_A2A[_state])] = _state
#: The two-L spelling. Tolerated inbound only: the SDK enum says one L and
#: :func:`state_wire` emits that, but the specification's prose uses both and a
#: partner's own model may have followed the prose.
_STATE_BY_WIRE["cancelled"] = A2AState.CANCELED


def read_state(value: Any) -> A2AState | None:
    """Read a partner's task state in any version's spelling.

    ``None`` for anything outside the vocabulary, which the caller refuses
    WHOLE rather than mapping to the nearest known state (FR-026).

    ``status.state`` arrives in per-version spellings: v0.3's hyphenated
    value, the underscore variant proto-JSON parsers emit, and the v1.0 proto
    enum NAME (``TASK_STATE_COMPLETED``) — the common case over REST, since
    the REST binding is proto-JSON throughout. All must be read here, in one
    place, or a completed delegation is reported as an unrecognised state.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return (
        _STATE_BY_WIRE.get(text)
        or _STATE_BY_WIRE.get(text.lower())
        or _STATE_BY_WIRE.get(text.upper())
    )


# --- Part shaping ------------------------------------------------------------
# v0.3's Part union is TextPart / FilePart / DataPart, discriminated by
# ``kind`` — not ``type``, which is the pre-0.3 spelling and appears in NO
# v0.3 model. The reference python SDK defaults ``kind`` and ignores unknown
# fields, so it round-trips either spelling silently; Foundry's parser is
# strict and rejects a part (and the whole response) without the
# discriminator.
#
# v1.0's Part is the PROTO message: flat oneof fields ``text`` / ``raw`` /
# ``url`` / ``data`` plus ``filename``/``mediaType``, parsed by the reference
# client with ``json_format.ParseDict`` — strict about field names.
# ``structuredData`` and a nested file object are pre-donation draft
# spellings that appear in no proto field: emit them and a real v1.0 client
# rejects every artifact. The proto descriptor is the authority here, never
# either specification's prose, and the shaping tests parse each shape with
# the SDK.


def text_part(value: str, version: str) -> dict[str, Any]:
    if is_legacy(version):
        return {"kind": "text", "text": value}
    return {"text": value}


def file_part(uri: str, name: str | None, version: str) -> dict[str, Any]:
    if is_legacy(version):
        return {"kind": "file", "file": {"uri": uri, "name": name}}
    part: dict[str, Any] = {"url": uri}
    if name:
        part["filename"] = name
    return part


def data_part(value: dict[str, Any], version: str) -> dict[str, Any]:
    if is_legacy(version):
        return {"kind": "data", "data": value}
    return {"data": value}


def agent_message(
    parts: list[dict[str, Any]],
    version: str,
    *,
    message_id: str,
    task_id: str | None = None,
    context_id: str | None = None,
) -> dict[str, Any]:
    """Shape an agent-side ``Message`` for the wire of *version*.

    The role spelling is per-version for the same reason the states are:
    v0.3 spells it ``agent``; the v1.0 proto enum is ``ROLE_AGENT``, and the
    reference parser accepts nothing else.

    ``parts`` is right here because this surface serves JSON-RPC only, where
    both versions spell it that way. It is NOT universally right: the v0.3
    proto - what a REST or gRPC partner parses - carries content under
    ``content``, which is why the outbound client shapes by dialect
    (:func:`outbound_message`) rather than by version.
    """
    message: dict[str, Any] = {
        "role": "agent" if is_legacy(version) else "ROLE_AGENT",
        "parts": parts,
        "messageId": message_id,
    }
    if task_id is not None:
        message["taskId"] = task_id
    if context_id is not None:
        message["contextId"] = context_id
    if is_legacy(version):
        # v0.3's Message carries the same REQUIRED ``kind`` discriminator as
        # Task and the parts — see the part-shaping note above.
        message["kind"] = "message"
    return message


def status_message(text: str, version: str, *, message_id: str) -> dict[str, Any]:
    """Shape a ``TaskStatus.message`` carrying *text* from the agent side.

    Used to give a FAILED task a stated cause. Without one, every failure —
    quota exhausted, tool crash, LLM timeout — reads identically to a partner,
    which FR-036 explicitly forbids for quota exhaustion.
    """
    return agent_message([text_part(text, version)], version, message_id=message_id)


def send_result(task: dict[str, Any], version: str) -> dict[str, Any]:
    """Envelope a rendered task as the send-operation's result.

    v0.3's ``message/send`` result IS the task (a Task-or-Message union the
    partner discriminates by ``kind``). v1.0's ``SendMessage`` result is a
    ``SendMessageResponse`` — a proto oneof — so the task must sit under a
    ``task`` key or the reference client's ``ParseDict`` refuses the whole
    response. ``GetTask``/``CancelTask`` results are a bare ``Task`` in both
    versions and need no envelope.
    """
    if is_legacy(version):
        return task
    return {"task": task}


def read_send_result(result: Any) -> tuple[dict | None, dict | None]:
    """Split a send response into ``(task, message)``, either of which is ``None``.

    Every binding answers a send with a Task **or** a Message, and both must
    be read: a fast partner answers with a Message immediately — the v1.0
    default asks for exactly that — and reading it as a Task ends in
    "unrecognised task state: <absent>".

    Two envelopes exist, and which one arrives is a property of the BINDING
    rather than of the version — worth stating because keying it on the
    version is the obvious mistake and it fails in only one of the four
    combinations:

    * the proto bindings (REST, gRPC) define ``SendMessageResponse`` as a
      ``oneof``, in **both** versions, so the payload sits under a ``task`` or
      ``message`` key;
    * v0.3's JSON-RPC binding returns the bare Task-or-Message union,
      discriminated by ``kind``.

    Reading the keys first is unambiguous rather than merely convenient: a
    Task has no ``task`` field and a Message has no ``message`` field in any
    version, so their presence can only be the envelope.

    A body carrying neither is read by shape — a status or an id means Task,
    parts mean Message — because both target platforms' surfaces are Preview
    and a missing discriminator is the likeliest thing to change.
    """
    if not isinstance(result, dict):
        return None, None

    task = result.get("task")
    if isinstance(task, dict):
        return task, None
    message = result.get("message")
    if isinstance(message, dict):
        return None, message

    kind = result.get("kind")
    if kind == "message":
        return None, result
    if kind == "task":
        return result, None
    if isinstance(result.get("status"), dict) or result.get("id"):
        return result, None
    # Both spellings: a proto Message says ``content``.
    if read_parts(result):
        return None, result
    return None, None


def read_part(part: dict[str, Any]) -> tuple[str, Any]:
    """Read an inbound part in either shape, returning ``(kind, value)``.

    Inbound is version-tolerant on purpose: a partner mid-migration may send
    either shape, and refusing a well-formed part because it used the other
    spelling would be pedantry with no security benefit.
    """
    if not isinstance(part, dict):
        # A string element would otherwise pass the ``in`` membership tests
        # below as a SUBSTRING check and then crash on subscripting; treat any
        # non-object part as unrecognised so the caller rejects it cleanly.
        return "unknown", None
    if "text" in part:
        return "text", part["text"]
    if "structuredData" in part:
        return "data", part["structuredData"]
    if "data" in part:
        return "data", part["data"]
    if "url" in part:
        return "file", part["url"]
    if "file" in part:
        return "file", part["file"]
    if "raw" in part:
        return "raw", part["raw"]
    return "unknown", None


# --- Error envelope ----------------------------------------------------------
# Every failure returned to a partner carries a machine-readable code. Free
# text alone is not acceptable: partner tooling branches on the code.

JSONRPC_PARSE_ERROR: Final = -32700
JSONRPC_INVALID_REQUEST: Final = -32600
JSONRPC_METHOD_NOT_FOUND: Final = -32601
JSONRPC_INVALID_PARAMS: Final = -32602
JSONRPC_INTERNAL_ERROR: Final = -32603

# A2A named errors (spec-assigned application range).
TASK_NOT_FOUND: Final = -32001
TASK_NOT_CANCELABLE: Final = -32002
PUSH_NOTIFICATION_NOT_SUPPORTED: Final = -32003
UNSUPPORTED_OPERATION: Final = -32004
CONTENT_TYPE_NOT_SUPPORTED: Final = -32005
#: -32009, not -32006: the SDK reserves -32006 for ``InvalidAgentResponseError``
#: (``a2a.utils.errors``, and pinned as a ``Literal`` on the v0.3 wire type), so
#: emitting a version refusal there tells an SDK-based partner the agent
#: answered invalidly.
VERSION_NOT_SUPPORTED: Final = -32009
RATE_LIMITED: Final = -32029
QUOTA_EXHAUSTED: Final = -32030

_ERROR_NAMES: Final[dict[int, str]] = {
    TASK_NOT_FOUND: "TaskNotFoundError",
    TASK_NOT_CANCELABLE: "TaskNotCancelableError",
    PUSH_NOTIFICATION_NOT_SUPPORTED: "PushNotificationNotSupportedError",
    UNSUPPORTED_OPERATION: "UnsupportedOperationError",
    CONTENT_TYPE_NOT_SUPPORTED: "ContentTypeNotSupportedError",
    VERSION_NOT_SUPPORTED: "VersionNotSupportedError",
    RATE_LIMITED: "RateLimitedError",
    QUOTA_EXHAUSTED: "QuotaExhaustedError",
}


def jsonrpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def jsonrpc_error(
    req_id: Any, code: int, message: str, details: list[dict] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    name = _ERROR_NAMES.get(code)
    if name:
        error["data"] = {"@type": name}
        if details:
            error["data"]["details"] = details
    elif details:
        error["data"] = {"details": details}
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def not_found_error(req_id: Any) -> dict[str, Any]:
    """The single uniform not-found returned for every negative lookup.

    An unknown task id, a task owned by another client, an unexposed agent and
    an unknown card token all return THIS. Distinguishing them would let a
    caller enumerate what exists by reading the difference.
    """
    return jsonrpc_error(req_id, TASK_NOT_FOUND, "Not found")

"""Capability pre-check for unattended OpenCTI runs.

OpenCTI records every request refused with ``FORBIDDEN_ACCESS`` as an
``event_scope: 'unauthorized'`` activity event attributed to the calling user
and tagged with the operation name. A per-user fan-out (the weekly CTEM stage
assessments, the monthly exposure briefing) runs as each user in turn, so a
query only some roles may run left "<user> tries an unauthorized read" in the
customer's audit log every week for every user who could not run it - an
activity pattern indistinguishable from a compromised account, for data the user
could never have received anyway.

In an unattended run the provider therefore reads the acting user's
capabilities once (``me`` and ``about`` need no capability, so the probe itself
is never refused) and does not send a call the user's role cannot pass. Users
who hold the capabilities are queried exactly as before.

A requirement is a tuple of groups that must ALL hold, a group holding when the
user has ANY one of its capabilities: OpenCTI's ``@auth(for: [...])`` is an
any-of, but one call can cross several checks - the PIR entity query runs on the
KNOWLEDGE root ``stixDomainObjects`` and selects ``pirInformation``, gated on
PIRAPI, and the audit roots add a per-stream check in their resolver. Every
entry is a schema ``@auth`` directive or a resolver check that raises
``ForbiddenAccess``; extend the table only with requirements checked there (the
"typically" hints of ``_common._CAPABILITY_BY_ROOT_FIELD`` are best effort and
must not suppress data). Where OpenCTI releases differ, the lenient requirement
applies: a false skip silently hides data the run is entitled to.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from app.core.utils import format_exception_message
from app.tools.base import CallableToolExecutor, ToolResult

from . import _common
from .audit import _normalize_mode, _normalize_stream

logger = logging.getLogger(__name__)

_BYPASS = "BYPASS"

#: Groups that must all hold; a group holds with any one of its capabilities.
Requirement = tuple[tuple[str, ...], ...]


def _all(*capabilities: str) -> Requirement:
    """A requirement needing every one of *capabilities*."""
    return tuple((capability,) for capability in capabilities)


TOOL_CAPABILITIES: dict[str, Requirement] = {
    # playbooks / playbook / playbookComponents / playbookExecute: AUTOMATION
    "manage_opencti_playbook": _all("AUTOMATION"),
    "edit_opencti_playbook_graph": _all("AUTOMATION"),
    # pirs / pir: PIRAPI
    "manage_opencti_pir": _all("PIRAPI"),
    # stixDomainObjects: KNOWLEDGE, and the pirInformation it selects: PIRAPI
    "query_opencti_pir_entities": _all("KNOWLEDGE", "PIRAPI"),
    # pirRelationships admits KNOWLEDGE or PIRAPI, but its resolver answers no
    # user without PIRAPI (getPirWithAccessCheck, every release with PIRs).
    "query_opencti_pir_relationships": _all("PIRAPI"),
    # securityCoverage / securityCoverages: KNOWLEDGE
    "manage_opencti_security_coverage": _all("KNOWLEDGE"),
    # audits / auditsNumber, whatever stream; the stream check is added by
    # ``_audit_stream_requirement``.
    "query_opencti_activity": (
        (
            "SETTINGS_SETACCESSES",
            "SETTINGS_SECURITYACTIVITY",
            "VIRTUAL_ORGANIZATION_ADMIN",
        ),
    ),
}

#: Per-action overrides for the consolidated ``manage_*`` tools, whose actions
#: map to mutations with stricter ``@auth`` than the reads.
ACTION_CAPABILITIES: dict[str, dict[str, Requirement]] = {
    "manage_opencti_playbook": {
        # playbookAdd / playbookDelete / playbookFieldPatch / playbookDuplicate
        "create": _all("AUTOMATION_AUTMANAGE"),
        "delete": _all("AUTOMATION_AUTMANAGE"),
        "toggle": _all("AUTOMATION_AUTMANAGE"),
        "duplicate": _all("AUTOMATION_AUTMANAGE"),
    },
    # Every graph edit is a playbook*Node / *Link / UpdatePositions mutation.
    "edit_opencti_playbook_graph": {
        action: _all("AUTOMATION_AUTMANAGE")
        for action in (
            "add_node",
            "replace_node",
            "insert_node",
            "add_link",
            "delete_node",
            "delete_link",
            "update_positions",
        )
    },
    "manage_opencti_pir": {
        # pirAdd / pirFieldPatch / pirDelete
        "create": _all("PIRAPI_PIRUPDATE"),
        "update": _all("PIRAPI_PIRUPDATE"),
        "delete": _all("PIRAPI_PIRUPDATE"),
        # pirFlagElement / pirUnflagElement
        "flag": _all(_BYPASS),
        "unflag": _all(_BYPASS),
    },
    "manage_opencti_security_coverage": {
        # securityCoverageAdd / securityCoverageFieldPatch: KNOWLEDGE_KNUPDATE
        "create": _all("KNOWLEDGE_KNUPDATE"),
        "update": _all("KNOWLEDGE_KNUPDATE"),
        # securityCoverageDelete: KNOWLEDGE_KNUPDATE_KNDELETE
        "delete": _all("KNOWLEDGE_KNUPDATE_KNDELETE"),
    },
}

#: ``computeAuditTypes`` refuses a stream the user may not read. ``audits``
#: (list mode) applies it in every supported release, ``auditsNumber`` (count
#: mode) only from 7.260828.0: an older or unreadable release answers the
#: count to anyone who passes the root directive.
_AUDIT_STREAM_CAPABILITY = {"History": "KNOWLEDGE", "Activity": "SETTINGS_SECURITYACTIVITY"}
_AUDITS_NUMBER_STREAM_CHECK_SINCE = (7, 260828, 0)

#: ``CAPABILITIES_IN_DRAFT_NAMES``: the capabilities a role can also grant in
#: drafts only. In a draft (the request's or the one open in the user's UI)
#: OpenCTI accepts them from the user's ``capabilitiesInDraft`` too. That field
#: and those grants arrived together in 6.9.0; before it, only base
#: capabilities count.
_DRAFT_CAPABILITIES_SINCE = (6, 9, 0)
_CAPABILITIES_IN_DRAFT = (
    "KNOWLEDGE",
    "KNOWLEDGE_KNPARTICIPATE",
    "KNOWLEDGE_KNUPDATE",
    "KNOWLEDGE_KNUPDATE_KNDELETE",
    "KNOWLEDGE_KNUPDATE_KNMERGE",
    "KNOWLEDGE_KNUPDATE_KNBYPASSREFERENCE",
    "KNOWLEDGE_KNUPDATE_KNBYPASSFIELDS",
    "KNOWLEDGE_KNUPDATE_KNORGARESTRICT",
    "KNOWLEDGE_KNUPLOAD",
    "KNOWLEDGE_KNASKIMPORT",
    "KNOWLEDGE_KNENRICHMENT",
    "SETTINGS_SETLABELS",
)


def _audit_stream_requirement(
    arguments: dict[str, Any], platform_version: tuple[int, int, int] | None
) -> Requirement:
    capability = _AUDIT_STREAM_CAPABILITY.get(_normalize_stream(arguments.get("stream")))
    if capability is None:
        return ()
    if _normalize_mode(arguments.get("mode")) == "count" and (
        platform_version is None or platform_version < _AUDITS_NUMBER_STREAM_CHECK_SINCE
    ):
        return ()
    return _all(capability)


def required_capabilities(
    tool: str,
    arguments: dict[str, Any],
    platform_version: tuple[int, int, int] | None = None,
) -> Requirement:
    """What *tool* needs for this call (see ``Requirement``)."""
    action = str(arguments.get("action") or "").strip().lower()
    requirement = ACTION_CAPABILITIES.get(tool, {}).get(action) or TOOL_CAPABILITIES[tool]
    if tool == "query_opencti_activity":
        requirement += _audit_stream_requirement(arguments, platform_version)
    return requirement


def has_any_capability(held: Iterable[str], required: Iterable[str]) -> bool:
    """Mirror OpenCTI's ``@auth`` check (``authDirective.ts::checkCapabilities``).

    BYPASS admits everything; otherwise a required capability is held when it
    is a substring of a held capability name - OpenCTI's names are
    hierarchical, so ``KNOWLEDGE_KNUPDATE_KNDELETE`` holds ``KNOWLEDGE_KNUPDATE``.
    """
    held = list(held)
    if _BYPASS in held:
        return True
    return any(
        req != _BYPASS and any(req in name for name in held) for req in required
    )


def missing_capabilities(held: Iterable[str], requirement: Requirement) -> Requirement:
    """The groups of *requirement* that *held* does not satisfy (none: allowed)."""
    held = list(held)
    if _BYPASS in held:
        return ()
    return tuple(group for group in requirement if not has_any_capability(held, group))


def _draft_grantable(group: tuple[str, ...]) -> bool:
    return has_any_capability(_CAPABILITIES_IN_DRAFT, group)


_PROBE_QUERY = "query MeCapabilities { me { capabilities { name } } about { version } }"
_DRAFT_PROBE_QUERY = "query MeDraftCapabilities { me { capabilitiesInDraft { name } } }"
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _parse_version(value: object) -> tuple[int, int, int] | None:
    """``"7.260917.0"`` -> ``(7, 260917, 0)``; ``None`` when unreadable."""
    match = _VERSION_RE.match(value.strip()) if isinstance(value, str) else None
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def _names(entries: Any) -> frozenset[str]:
    return frozenset(str(c["name"]) for c in entries if c.get("name"))


class CapabilityProbe:
    """The acting user's capabilities and the platform version, read once per build."""

    def __init__(self, base_url: str, headers: Any) -> None:
        self._base_url = base_url
        self._headers = headers
        self._lock = asyncio.Lock()
        self._loaded = False
        self._draft_loaded = False
        self.capabilities: frozenset[str] | None = None
        self.version: tuple[int, int, int] | None = None
        self._draft: frozenset[str] | None = None

    async def load(self) -> frozenset[str] | None:
        """The capability names, or ``None`` when they could not be read."""
        async with self._lock:
            if not self._loaded:
                self._loaded = True
                # Fail open: a probe that cannot run, or answers something
                # unexpected, must never break the tool.
                try:
                    data = await self._query(_PROBE_QUERY)
                    capabilities = _names(data["me"]["capabilities"])
                    about = data.get("about")
                except Exception as exc:
                    logger.warning(
                        "OpenCTI capability probe failed (%s); capability-gated "
                        "tools run unchecked for this build",
                        format_exception_message(exc),
                    )
                else:
                    self.capabilities = capabilities
                    self.version = _parse_version(
                        about.get("version") if isinstance(about, dict) else None
                    )
        return self.capabilities

    async def draft_capabilities(self) -> frozenset[str] | None:
        """What the user's role grants in drafts, or ``None`` when unreadable."""
        async with self._lock:
            if not self._draft_loaded:
                self._draft_loaded = True
                if self.version is not None and self.version < _DRAFT_CAPABILITIES_SINCE:
                    self._draft = frozenset()
                else:
                    try:
                        data = await self._query(_DRAFT_PROBE_QUERY)
                        self._draft = _names(data["me"]["capabilitiesInDraft"])
                    except Exception as exc:
                        logger.info(
                            "OpenCTI draft capability probe failed (%s); a "
                            "requirement drafts can grant skips nothing for this build",
                            format_exception_message(exc),
                        )
        return self._draft

    async def _query(self, query: str) -> dict[str, Any]:
        async with _common._client(self._headers, timeout=30) as client:
            data, err = await _common._graphql(client, self._base_url, self._headers, query)
        if err:
            raise RuntimeError(err)
        return data


def _describe(groups: Requirement) -> str:
    return " and ".join(
        group[0] if len(group) == 1 else f"one of {', '.join(group)}" for group in groups
    )


def _skipped_message(tool: str, missing: Requirement) -> str:
    # Kept clear of the auth-failure wording (``tools.base``): a skip is not a
    # broken credential and must not count toward the assignment auth quarantine.
    return (
        f"Skipped: the OpenCTI account this run acts as does not hold "
        f"{_describe(missing)}, which `{tool}` needs for this call, so the "
        "request was not sent - OpenCTI would refuse it and record the refusal "
        "against this user in its audit log. Continue without it: it is not "
        "available to this user."
    )


def _gated(
    tool: str,
    handler: Callable[..., Awaitable[Any]],
    probe: CapabilityProbe,
) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        held = await probe.load()
        if held is not None:
            missing = missing_capabilities(
                held, required_capabilities(tool, kwargs, probe.version)
            )
            if missing and all(_draft_grantable(group) for group in missing):
                # Whether this call runs in a draft (the request's, or the one
                # open in the user's UI) is not known here, so a user whose
                # drafts grant what is missing is never skipped.
                draft = await probe.draft_capabilities()
                missing = () if draft is None else missing_capabilities(held | draft, missing)
            if missing:
                logger.info(
                    "OpenCTI capability pre-check: skipped %s (needs %s)",
                    tool,
                    _describe(missing),
                )
                return ToolResult(
                    tool_call_id="", content=_skipped_message(tool, missing), no_op=True
                )
        return await handler(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Every GraphQL document an unattended run sends, whatever builds it
# ---------------------------------------------------------------------------

#: ``@auth(for: [...])`` of every Query and Mutation root field of the OpenCTI
#: schema (``config/schema`` and ``src/modules``), one any-of list per field.
#: A field absent from the table needs authentication only.
_ROOT_FIELD_TABLE: dict[str, dict[str, list[str]]] = json.loads(
    Path(__file__).with_name("root_field_capabilities.json").read_text(encoding="utf-8")
)

#: Resolver checks stricter than the root directive: ``pirRelationships``
#: admits KNOWLEDGE, but ``getPirWithAccessCheck`` answers no user without PIRAPI.
_ROOT_FIELD_OVERRIDES: dict[str, Requirement] = {"pirRelationships": _all("PIRAPI")}

_TOKEN_RE = re.compile(r"\.\.\.|[{}()@:]|[A-Za-z_][A-Za-z0-9_]*")
_STRING_RE = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"')
_COMMENT_RE = re.compile(r"#[^\n]*")


def root_fields(query: str) -> tuple[str, list[str]]:
    """``(operation type, top-level field names)`` of a GraphQL document, aliases resolved."""
    text = _COMMENT_RE.sub("", _STRING_RE.sub('""', query))
    head = re.match(r"\s*(query|mutation|subscription)\b", text)
    kind = head.group(1) if head else "query"
    start = text.find("{")
    if start < 0:
        return kind, []
    fields: list[str] = []
    tokens = _TOKEN_RE.findall(text, start)
    braces = parens = 0
    index = 0
    while index < len(tokens):
        part = tokens[index]
        if part == "{":
            braces += 1
        elif part == "}":
            braces -= 1
            if braces == 0:
                break
        elif part == "(":
            parens += 1
        elif part == ")":
            parens -= 1
        elif part == "@":
            index += 1  # a directive name, never a field
        elif braces == 1 and parens == 0 and part not in {":", "..."}:
            if index + 2 < len(tokens) and tokens[index + 1] == ":":
                part = tokens[index + 2]  # alias: the field is after the colon
                index += 2
            fields.append(part)
        index += 1
    return kind, fields


def query_requirement(query: str) -> Requirement:
    """What the root fields of *query* need: one any-of group per gated field."""
    kind, fields = root_fields(query)
    table = _ROOT_FIELD_TABLE.get(kind) or {}
    groups: list[tuple[str, ...]] = []
    for field in fields:
        requirement = _ROOT_FIELD_OVERRIDES.get(field)
        if requirement is None:
            capabilities = table.get(field)
            requirement = (tuple(capabilities),) if capabilities else ()
        groups.extend(group for group in requirement if group not in groups)
    return tuple(groups)


async def unsent_reason(headers: Any, query: str) -> str | None:
    """Why *query* must not be sent on *headers*, or ``None`` to send it.

    Only headers carrying a ``capability_probe`` (an unattended run, a
    background CTEM build) are checked; a probe that cannot be read sends
    everything, as the tool gate does.
    """
    probe = getattr(headers, "capability_probe", None)
    if probe is None:
        return None
    requirement = query_requirement(query)
    if not requirement:
        return None  # the probes themselves (me, about) always pass here
    held = await probe.load()
    if held is None:
        return None
    missing = missing_capabilities(held, requirement)
    if missing and all(_draft_grantable(group) for group in missing):
        draft = await probe.draft_capabilities()
        missing = () if draft is None else missing_capabilities(held | draft, missing)
    if not missing:
        return None
    _kind, fields = root_fields(query)
    return (
        f"Not sent: the OpenCTI account this run acts as does not hold "
        f"{_describe(missing)}, which {', '.join(fields)} needs, so the request "
        "was not made - OpenCTI would refuse it and record the refusal against "
        "this user in its audit log. Continue without it."
    )


def attach_probe(headers: Any, base_url: str) -> Any:
    """*headers*, checked from now on by :func:`unsent_reason` (shared probe).

    A plain dict is copied into a :class:`RefreshableHeaders` (no refresh) so
    it can carry the probe.
    """
    from app.integrations.http import RefreshableHeaders

    if not isinstance(headers, RefreshableHeaders):
        headers = RefreshableHeaders(headers)
    if getattr(headers, "capability_probe", None) is None:
        headers.capability_probe = CapabilityProbe(base_url, headers)
    return headers


def install_capability_gate(
    executor: CallableToolExecutor, base_url: str, headers: Any
) -> None:
    """Wrap the capability-gated tools registered on *executor*, and check
    every GraphQL document the run's tools send (:func:`unsent_reason`).

    Meant for unattended runs only: in interactive chat the user asked for the
    action, and a refusal (with its audit entry) is an accurate record of it.
    """
    probe = CapabilityProbe(base_url, headers)
    # A plain dict cannot carry it: the tool gate below still applies.
    with contextlib.suppress(AttributeError):
        headers.capability_probe = probe
    for tool in TOOL_CAPABILITIES:
        handler = executor._handlers.get(tool)
        if handler is None:
            continue
        executor.register(tool, _gated(tool, handler, probe))

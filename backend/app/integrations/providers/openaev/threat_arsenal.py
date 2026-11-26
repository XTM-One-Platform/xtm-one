# SPDX-License-Identifier: Apache-2.0

"""OpenAEV Threat Arsenal tools (the v3 successor to the payload API).

``manage_openaev_threat_arsenal`` drives ``/api/threat_arsenals``, the OpenAEV
v3 surface for "actions" - an injector contract plus its payload, which is what
the Threat Arsenal screen manages. It replaces the legacy ``/api/payloads``
API, whose controller is now annotated ``@Deprecated`` at class level.

Two v3 details this module has to respect:

* **Expectations are part of the action contract.** ``action_expectations`` is
  mandatory on create: it declares what the action MEASURES (was the attack
  prevented, detected, did it prove a vulnerability). An action created with
  the wrong expectations produces simulation results that answer the wrong
  question, so the parameter is explicit rather than hardcoded.
* **Some routes are tenant-scoped only.** ``delete`` and
  ``security_platforms`` exist ONLY under ``/api/tenants/{tenantId}/...``, so
  they 404 on the unprefixed path. The tenant id is resolved automatically
  from ``GET /api/settings`` (``PlatformCapabilities``), so callers never have
  to know about tenants.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    COMMAND_EXECUTORS,
    CONTRACT_OUTPUT_TYPES,
    EXPECTATION_TYPES,
    PAYLOAD_EXPECTATION_TYPES,
    PLATFORMS,
    PROVIDING_FILTER_KEY,
    SECURITY_PLATFORM_TYPES,
    TECHNICAL_EXPECTATION_TYPES,
    PlatformCapabilities,
    _api,
    _expand_findings,
    _fmt_arsenal_action,
    _merge_filter_clause,
    _merge_fork_candidates,
    _normalize_output_parsers,
    _providing_filter_clause,
    _resolve_output_types,
    _search_body,
    _validate_filter_group,
    _without_providing_filter,
    json_answer,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"

_ARSENAL = "/api/threat_arsenals"

#: ``Payload.PAYLOAD_SOURCE`` / ``PAYLOAD_STATUS`` / ``PAYLOAD_EXECUTION_ARCH``.
_ACTION_SOURCES = ["COMMUNITY", "FILIGRAN", "MANUAL"]
_ACTION_STATUSES = ["UNVERIFIED", "VERIFIED", "DEPRECATED"]
_ACTION_ARCHS = ["x86_64", "arm64", "ALL_ARCHITECTURES"]

#: Action types accepted by ``ThreatArsenalActionCreateInput.action_type``
#: (the payload type discriminator).
_ACTION_TYPES = [
    "Command",
    "Executable",
    "FileDrop",
    "DnsResolution",
    "NetworkTraffic",
]

#: Marker tag stamped on every action authored through this tool. Everything
#: that reaches ``manage_openaev_threat_arsenal`` was authored by an agent, so
#: the provenance tag is applied deterministically here instead of relying on
#: the orchestrator to remember to resolve-and-pass a tag id (it never did).
_AI_TAG_NAME = "ai generated"
_AI_TAG_COLOR = "#8b5cf6"

#: Deterministic provenance marker for a fork, keyed on the SOURCE action id.
#: The atomic ``fork`` stamps ``ai-fork-src:<source_id>`` on the copy so a later
#: cycle can find an existing fork of the same source and REUSE it instead of
#: minting a second identical copy (the cross-cycle double-dup this run produced).
#: The key is unique per source, so any action carrying it is by construction a
#: fork of that source authored by this tool - the dedup is a code-level lookup,
#: never the LLM "confirming" a fork exists.
_FORK_MARKER_TAG_PREFIX = "ai-fork-src:"
_FORK_MARKER_TAG_COLOR = "#8b5cf6"

#: Trailing "(duplicate)" (any case, optional surrounding space) that OpenAEV's
#: ``POST /{id}/duplicate`` appends to the copy's name. Stripped as part of the
#: atomic fork so the copy never lands with the tell-tale half-forked name.
_DUPLICATE_SUFFIX_RE = re.compile(r"\s*\(duplicate\)\s*$", re.IGNORECASE)


#: Max concurrent findings-required arsenal WRITES (fork/create) per event loop.
#: The orphaned "(duplicate)" copies came from parallel fork bursts (7-10 writes
#: in ONE agent-loop iteration) whose dedup/marker checks race: two forks of the
#: same source both read "no existing fork" and both mint a copy. Serializing to
#: 3 both honours the operator's "max 3 at a time" and shrinks that race window
#: to at most 3 in flight. Only engaged for autonomous authors
#: (arsenal_findings_required); chat / other agents stay uncapped.
_ARSENAL_WRITE_CONCURRENCY = 3

#: Loop-keyed so the primitive is never shared across event loops (a semaphore
#: created in one loop and awaited in another raises). Keyed on id(loop): a GC'd
#: loop's stale entry only ever yields a fresh semaphore, which is harmless.
_arsenal_write_semaphores: dict[int, asyncio.Semaphore] = {}


def _arsenal_write_semaphore() -> asyncio.Semaphore:
    """Return the write semaphore bound to the running loop (lazily created)."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    sem = _arsenal_write_semaphores.get(key)
    if sem is None:
        sem = asyncio.Semaphore(_ARSENAL_WRITE_CONCURRENCY)
        _arsenal_write_semaphores[key] = sem
    return sem


def _arsenal_write_guard(action: str):
    """Context manager serializing a findings-required fork/create/duplicate write.

    Returns the loop-bound semaphore for a gated write when
    ``arsenal_findings_required()`` is active (orchestrator + Payload Creator),
    else a no-op context so chat and other agents stay uncapped. Wrapping the
    WHOLE fork/create/duplicate branch (not each HTTP call) keeps a single op's
    duplicate -> finalize -> verify sequence under one permit, so at most
    ``_ARSENAL_WRITE_CONCURRENCY`` run at once. ``duplicate`` MINTS a copy too, so
    it is capped exactly like fork/create - otherwise a parallel burst of raw
    duplicates would reopen the orphan-race the cap exists to close.
    """
    from app.services.builtin_tools.filtering import arsenal_findings_required

    if action in ("fork", "create", "duplicate") and arsenal_findings_required():
        return _arsenal_write_semaphore()
    return contextlib.nullcontext()


def _fork_marker_tag_name(source_id: str) -> str:
    """Deterministic provenance tag name for a fork of ``source_id``."""
    return f"{_FORK_MARKER_TAG_PREFIX}{source_id}"


def _strip_duplicate_suffix(name: str) -> str:
    """Remove the trailing '(duplicate)' OpenAEV appends when duplicating."""
    if not isinstance(name, str):
        return ""
    return _DUPLICATE_SUFFIX_RE.sub("", name).strip()


def _action_findings_count(action: dict) -> int:
    """Count the output elements that are FLAGGED as findings.

    The CORE fork gate: OpenAEV chaining is findings-based, so an action that
    produces nothing cannot feed a chained step - forking one and leaving it
    findings-less yields a chain-useless copy. Counts ONLY nested output-parser
    elements whose ``contract_output_element_is_finding`` is exactly ``True``.

    Deliberately does NOT trust ``action_providing``: that server-derived list
    is computed from each element's TYPE regardless of the finding flag (the
    OpenAEV DTO comment says as much), so a copy carrying a typed-but-non-finding
    element would read as "provides credentials" yet emit no finding and dead-end
    the chain. ``isFinding`` is a PRIMITIVE boolean that silently persists
    ``false`` when omitted (no 400), which is exactly why the tool now forces it
    true on every authored element and why the verify must gate on it directly.
    """
    count = 0
    parsers = action.get("action_output_parsers")
    if isinstance(parsers, list):
        for parser in parsers:
            if not isinstance(parser, dict):
                continue
            elements = parser.get("output_parser_contract_output_elements")
            if not isinstance(elements, list):
                continue
            for element in elements:
                if (
                    isinstance(element, dict)
                    and element.get("contract_output_element_is_finding") is True
                ):
                    count += 1
    return count


#: Body keys whose presence marks a create as a COMMAND/EXECUTABLE payload - one
#: that RUNS and emits stdout/stderr/a dropped file. For those, findings come
#: ONLY from REGEX output parsers, so a command action with none is chain-useless
#: (OpenAEV chaining is findings-based). Prevention/detection-expectation creates
#: whose findings come from a security platform carry none of these keys, so the
#: findings-required gate never touches them (no false positive on a "was it
#: blocked?" test).
_COMMAND_PAYLOAD_KEYS = (
    "command_content",
    "executable_file",
    "file_drop_file",
    "dns_resolution_hostname",
)


def _body_is_command_based(body: dict) -> bool:
    """True when a create body carries executable/command payload content."""
    return any(body.get(k) for k in _COMMAND_PAYLOAD_KEYS)


def _is_unknown_filter_error(err: str) -> bool:
    """True when OpenAEV rejected a search because a filter key is not indexed.

    The platform answers an unrecognised ``filterGroup`` key (e.g. the model
    guessing ``base_id`` on the arsenal) with a Spring ``PropertyReferenceException``
    surfaced as "... not handled by Queryable annotation ..." (an HTTP 400/500).
    A different search key would only guess again, so this is caught and turned
    into an actionable message rather than retried.
    """
    low = err.lower()
    return "queryable" in low or "not handled by" in low or "propertyreference" in low


def _is_transient_server_error(err: str) -> bool:
    """True when a search failed with a 5xx (e.g. the transient full-text 500)."""
    low = err.lower()
    return "server error (5" in low or "http 50" in low


_NATIVE_DUPLICATE_MSG = (
    "This action is a native injector (not payload-based) and cannot be "
    "duplicated. Reuse its injector_contract_id / action_id as-is. Duplicate "
    "only payload-based arsenal actions (Command, FileDrop, Executable, "
    "DnsResolution, catalog/community copies, etc.), then add REGEX parsers "
    "on the copy. Do not invent a weaker original Command as a substitute."
)


def _native_duplicate_error(err: str) -> str | None:
    """Return the structured native-injector duplicate message, else None.

    OpenAEV rejects duplicating a native injector (a contract with no payload)
    with a BadRequestException. Current releases phrase it "This threat arsenal
    item is a native injector (not payload-based) and cannot be duplicated...";
    older ones said "Only threat arsenal items based on a payload can be
    duplicated". Match on EITHER substring, regardless of the status code, so the
    rewrite survives both the OpenAEV wording change and whatever status it
    attaches (a 400 in current releases, a 404 historically). When the status is
    a 404 the message must not be left as ``_annotate_not_found``'s
    RBAC-three-meanings text, or the model invents a weaker Command instead of
    reusing the id.
    """
    low = (err or "").lower()
    if "not payload-based" in low or "based on a payload" in low:
        return _NATIVE_DUPLICATE_MSG
    return None


#: Keys copied verbatim from the ``ThreatArsenalActionFullOutput`` (GET) onto
#: the ``ThreatArsenalActionUpdateInput`` (PUT) - the two DTOs share these JSON
#: keys. ``action_name`` is derived separately (GET returns a locale->label
#: map, the update wants a single string) and ``action_source`` /
#: ``action_status`` are intentionally excluded (the update DTO has no such
#: field, so carrying them is a harmless no-op we skip for clarity).
_UPDATE_CARRY_KEYS = (
    "action_platforms",
    "action_description",
    "command_executor",
    "command_content",
    "action_execution_arch",
    "action_expectations",
    "action_expected_security_platforms",
    "executable_file",
    "file_drop_file",
    "dns_resolution_hostname",
    "action_arguments",
    "action_prerequisites",
    "action_cleanup_executor",
    "action_cleanup_command",
    "action_tags",
    "action_attack_patterns",
    "action_detection_remediations",
    "action_output_parsers",
    "action_domains",
)


def _derive_action_name(current: dict) -> str:
    """Pull a single display name out of a GET action's locale->label map."""
    labels = current.get("action_labels")
    if isinstance(labels, dict) and labels:
        return labels.get("en") or next(
            (v for v in labels.values() if isinstance(v, str) and v), ""
        )
    name = current.get("action_name")
    return name if isinstance(name, str) else ""


def _normalize_carried_output_parsers(parsers: object) -> object:
    """Coerce GET-shaped output parsers into the PUT (input) shape.

    Every ``output_parser_*`` / ``contract_output_element_*`` key already round-
    trips into ``ContractOutputElementInput`` (which binds
    ``contract_output_element_tags`` as a ``List<String>`` of tag ids). The GET
    output serialises that field with ``MultiIdSetSerializer``, i.e. as a list
    of id STRINGS, so in practice it is already input-shaped and carrying it
    forward verbatim is correct. This pass is therefore defensive and, above
    all, MUST NOT lose the ids: id strings are preserved as-is, and the only
    reshaping is flattening a full Tag object (``{"tag_id"/"id": ...}``) down to
    its id should the API ever hand back the object shape. Dropping the string
    ids (the naive "keep only dicts" flatten) would wipe every parser tag on an
    update, which is exactly the bug this guards against. Only touched for the
    CARRIED value - a caller that supplies its own ``action_output_parsers``
    already sends the input shape.
    """
    if not isinstance(parsers, list):
        return parsers
    for parser in parsers:
        if not isinstance(parser, dict):
            continue
        elements = parser.get("output_parser_contract_output_elements")
        if not isinstance(elements, list):
            continue
        for element in elements:
            if not isinstance(element, dict):
                continue
            tags = element.get("contract_output_element_tags")
            if isinstance(tags, list):
                normalized: list[str] = []
                for tag in tags:
                    if isinstance(tag, str):
                        if tag:
                            normalized.append(tag)
                    elif isinstance(tag, dict):
                        tag_id = tag.get("tag_id") or tag.get("id")
                        if tag_id:
                            normalized.append(tag_id)
                element["contract_output_element_tags"] = normalized
    return parsers


def _merge_update_body(
    current: dict, changes: dict, *, require_domain: bool = True
) -> tuple[dict, str]:
    """Build a COMPLETE update body from the action's current state + changes.

    ``require_domain`` (default True) hard-fails when the merged body would carry
    no ``action_domains``. The fork finalize path passes ``require_domain=False``:
    the OpenAEV duplicate inherits the source's domains (``PayloadService.duplicate``),
    so the copy already carries whatever domains the source had - a domain-less
    SOURCE should fall through to the copy's server-inherited (possibly empty)
    domains and let OpenAEV rule on it, rather than being blocked client-side and
    leaving the just-duplicated copy orphaned half-forked.

    ``PUT /api/threat_arsenals/{id}`` validates ``ThreatArsenalActionUpdateInput``
    as a whole (``action_name`` @NotBlank, ``action_domains`` @NotNull, and the
    service additionally requires ``action_execution_arch`` + ``action_expectations``
    for payload-based actions) and REPLACES every field it carries. A partial
    patch therefore either 400s on the missing mandatory fields or silently
    wipes the fields it omits - which is why every in-place output-parser fix
    was being rejected as a bad request. So we seed the body from the current
    action and let the caller's explicit changes win on top. The nested
    ``action_output_parsers`` / ``action_detection_remediations`` round-trip from
    the GET (output) shape into the PUT (input) shape is safe: the request mapper
    has FAIL_ON_UNKNOWN_PROPERTIES disabled, so the extra output-only keys
    (ids, timestamps) are ignored and the shared snake_case keys bind.
    """
    merged: dict = {}
    name = _derive_action_name(current)
    if name:
        merged["action_name"] = name
    for key in _UPDATE_CARRY_KEYS:
        value = current.get(key)
        if value is not None:
            if key == "action_output_parsers":
                value = _normalize_carried_output_parsers(value)
            merged[key] = value
    # Caller-supplied fields override the carried-forward state.
    merged.update(changes)
    # Association id lists must never reach the API as null: findAllById rejects
    # a null argument with a confusing "Ids must not be null" 400.
    for assoc_key in ("action_domains", "action_tags", "action_attack_patterns"):
        if merged.get(assoc_key) is None:
            merged[assoc_key] = []
    # ThreatArsenalService.updateActionPayloadBased hard-rejects a payload-based
    # update whose action_expectations is null with a 400 ("action_expectations
    # is required for payload-based actions"). Unlike the create DTO (which marks
    # it @NotNull), the field is NULLABLE on the Payload entity, so a source /
    # forked action whose expectations were never set (common for collector- and
    # catalog-imported content - exactly what the orchestrator forks) comes back
    # from the GET as null. The carry loop above drops null values, so the PUT
    # left without action_expectations and OpenAEV returned HTTP 400 on EVERY
    # in-place fork-finalize and update of such an action - the copy was then
    # abandoned half-forked. Seed the
    # service-required payload fields with the same safe defaults 'create' uses so
    # the full-replace PUT is always well-formed (an empty list means "no
    # expectations", which is what a null value already meant). action_execution_arch
    # is @NotNull on the entity so the GET always carries it, but default it too for
    # defence in depth; a caller-supplied value still wins via merged.update above.
    if merged.get("action_expectations") is None:
        merged["action_expectations"] = []
    if not merged.get("action_execution_arch"):
        merged["action_execution_arch"] = "ALL_ARCHITECTURES"
    if not merged.get("action_name"):
        return {}, (
            "Cannot update: the existing action has no readable name and none was "
            "supplied. Pass name=... explicitly."
        )
    if require_domain and not merged.get("action_domains"):
        return {}, (
            "Cannot update: the action has no security domain and none was "
            "supplied. Pass domain_ids=[...] (see list_openaev_domains)."
        )
    return merged, ""


def register(
    base_url: str,
    headers: dict[str, str],
    executor: CallableToolExecutor,
    *,
    capabilities: PlatformCapabilities,
) -> list[ToolDefinition]:
    async def manage_openaev_threat_arsenal(
        action: str = "",
        action_id: str = "",
        action_type: str = "",
        name: str = "",
        description: str = "",
        platforms: list[str] | None = None,
        expectations: list[str] | None = None,
        expected_security_platforms: dict | None = None,
        execution_arch: str = "",
        source: str = "",
        status: str = "",
        command_executor: str = "",
        command_content: str = "",
        dns_resolution_hostname: str = "",
        cleanup_executor: str = "",
        cleanup_command: str = "",
        domain_ids: list[str] | None = None,
        attack_pattern_ids: list[str] | None = None,
        tag_ids: list[str] | None = None,
        class_name: str = "",
        filter_group: str = "",
        include_content: bool = False,
        config: str = "",
        findings: list[dict] | None = None,
        search: str = "",
        limit: int = 20,
        output_types: list[str] | None = None,
    ) -> str:
        action = (action or "").strip().lower()
        from app.services.builtin_tools.filtering import (
            PHASE_GATE_LEAD,
            is_advisory_forbidden_arsenal_action,
            is_orchestrator_forbidden_arsenal_action,
            orchestrator_may_delete_action,
            orchestrator_phase_gate_blocked,
            record_orchestrator_arsenal_authored,
            record_orchestrator_session_action,
        )

        if is_advisory_forbidden_arsenal_action(action):
            return (
                f"Error: action '{action}' is not allowed for a consulted "
                "specialist. On a CONSULT you ADVISE only - return the "
                "reuse/fork/create decision plus a compact spec; creating, "
                "updating, forking, duplicating, or deleting arsenal actions is "
                "not available here. When the orchestrator DELEGATES you (a "
                "background task) to author an ORIGINAL, create/update it there. "
                "Forking a payload-based dead-end (duplicate + rename + AI tag + "
                "REGEX parsers, verified) is the Attack Path Orchestrator's own "
                "atomic 'fork' operation, not the specialist's."
            )
        if is_orchestrator_forbidden_arsenal_action(action):
            # Only bulk_delete lands here now. The orchestrator is deliberately
            # FREE to author (create/duplicate/update/fork) and to clean up its
            # OWN creations (session-scoped delete) - we judge the run by the
            # OUTCOME, not by shrinking the toolset. bulk_delete is the one
            # exception: its scope is a FILTER, not an id list, so it cannot be
            # bounded to "only what this run created", and the one hard line is
            # that the orchestrator must never remove another session's / someone
            # else's payloads.
            return (
                "Error: bulk_delete is not available to the Attack Path "
                "Orchestrator: its scope is a filter that could match actions "
                "from other sessions or authored by someone else, and you must "
                "only ever remove payloads YOU created in THIS run. To purge an "
                "unused or bad copy you authored this run, call action='delete' "
                "with its action_id (allowed only for your own run's creations). "
                "Everything else is available to you - reuse, fork, duplicate, "
                "update, create - and you are judged on the outcome: as few "
                "payloads as possible, each producing real findings, each USED in "
                "an authored step, nothing chain-useless left behind."
            )

        # Step-before-more phase gate: the orchestrator must interleave step
        # authoring with arsenal authoring instead of batching many
        # fork/create/duplicate calls and never authoring a step (the
        # 79-iteration, zero-step run). Once authored arsenal items run
        # PHASE_GATE_LEAD ahead of authored steps, refuse a NEW
        # fork/create/duplicate until a step lands. duplicate MINTS a copy too, so
        # it is counted and gated exactly like fork/create - otherwise a burst of
        # raw duplicates would bypass the gate and reopen the orphan pattern.
        # No-op for chat, other agents, and delegated specialists (marker not set
        # / counter None).
        if (
            action in ("fork", "create", "duplicate")
            and orchestrator_phase_gate_blocked()
        ):
            return (
                "Phase gate: you have authored at least "
                f"{PHASE_GATE_LEAD} threat-arsenal items without authoring an "
                "attack-path step that USES them, so further fork/create/duplicate "
                "is paused. Call author_openaev_attack_path_step now to wire one of "
                "your authored/forked items - or an already-available "
                "finding-producing action - into the scenario, then author arsenal "
                "again. Interleave authoring steps with authoring arsenal: build "
                "the scenario incrementally (available-finding action -> fork -> "
                "step), do not batch many copies first."
            )

        extra: dict = {}
        if config:
            try:
                extra = json.loads(config)
            except ValueError as exc:
                return f"config must be a valid JSON object: {exc}"
            if not isinstance(extra, dict):
                return "config must be a JSON object of field->value pairs."

        parsed_filter: dict | None = None
        if filter_group:
            try:
                parsed_filter = json.loads(filter_group)
            except ValueError as exc:
                return f"filter_group must be valid JSON: {exc}"
            filter_err = _validate_filter_group(parsed_filter)
            if filter_err:
                return f"filter_group is invalid. {filter_err}"

        # output_types on a LIST prefers actions that PRODUCE those finding
        # types (reuse). Payload-based actions matching the same search that
        # produce none are merged in afterwards as fork candidates. Native
        # injectors without a payload are not added. Merged as an extra AND
        # clause so it composes with any caller-supplied filter_group. Only
        # the two list actions consume it: anywhere else it would either be
        # silently ignored (get/create/update/...) or silently reshape the
        # scope of a destructive/aggregate call (bulk_delete, counts), so any
        # other action rejects it loudly instead.
        providing_narrowed = False
        if output_types:
            if action not in ("list", "list_non_tabletop"):
                return (
                    "output_types only applies to action='list' / "
                    "'list_non_tabletop'. To scope counts or bulk_delete by "
                    "producible finding types, put an explicit "
                    f"'{PROVIDING_FILTER_KEY}' clause in filter_group instead."
                )
            resolved_outputs, outputs_err = _resolve_output_types(output_types)
            if outputs_err:
                return outputs_err
            if resolved_outputs:
                parsed_filter, merge_err = _merge_filter_clause(
                    parsed_filter, _providing_filter_clause(resolved_outputs)
                )
                if merge_err:
                    return merge_err
                providing_narrowed = True

        def _enum(value: str, allowed: list[str], label: str) -> tuple[str, str]:
            if not value:
                return "", ""
            match = next((a for a in allowed if a.lower() == value.strip().lower()), None)
            if match is None:
                return "", f"{label} must be one of: {', '.join(allowed)} (got '{value}')."
            return match, ""

        def _enum_list(
            values: list[str], allowed: list[str], label: str
        ) -> tuple[list[str], str]:
            resolved: list[str] = []
            for v in values:
                match, err = _enum(v, allowed, label)
                if err:
                    return [], err
                resolved.append(match)
            return resolved, ""

        def _body(for_create: bool) -> tuple[dict, str]:
            body: dict = dict(extra)
            # Expand the first-class flat `findings` param into OpenAEV's nested
            # output-parser DTO (isFinding=true stamped) and merge it with any
            # parsers the caller already put in `config`. This is the path the
            # model should use - it stops hand-building the nested JSON that has
            # failed for many cycles. The result still flows through
            # _normalize_output_parsers below, so validation stays in one place.
            if findings is not None:
                expanded, ferr = _expand_findings(findings)
                if ferr:
                    return {}, ferr
                if expanded:
                    existing = body.get("action_output_parsers")
                    body["action_output_parsers"] = (
                        [*existing, *expanded]
                        if isinstance(existing, list)
                        else expanded
                    )
            parser_err = _normalize_output_parsers(body)
            if parser_err:
                return {}, parser_err
            if action_type:
                value, err = _enum(action_type, _ACTION_TYPES, "action_type")
                if err:
                    return {}, err
                body["action_type"] = value
            if name:
                body["action_name"] = name
            if description:
                body["action_description"] = description
            if platforms is not None:
                values, err = _enum_list(platforms, PLATFORMS, "platforms")
                if err:
                    return {}, err
                body["action_platforms"] = values
            if expectations is not None:
                human = [
                    str(v).strip().upper()
                    for v in expectations
                    if str(v).strip().upper() in EXPECTATION_TYPES
                    and str(v).strip().upper() not in PAYLOAD_EXPECTATION_TYPES
                ]
                if human:
                    return (
                        {},
                        f"expectations {', '.join(human)} cannot be set on a "
                        "threat arsenal action: OpenAEV payloads accept only "
                        f"{', '.join(PAYLOAD_EXPECTATION_TYPES)}. An "
                        "evidence-only action is judged on its findings; human "
                        "validation is a manual inject, not a payload expectation.",
                    )
                values, err = _enum_list(expectations, PAYLOAD_EXPECTATION_TYPES, "expectations")
                if err:
                    return {}, err
                body["action_expectations"] = values
            if expected_security_platforms is not None:
                # Map of technical-expectation-type -> list of security platform
                # types expected to fulfil it. Empty list (or an omitted type) =
                # any security platform. Only PREVENTION / DETECTION /
                # VULNERABILITY can be scoped - human-validated types are not.
                if not isinstance(expected_security_platforms, dict):
                    return (
                        {},
                        "expected_security_platforms must be an object mapping an "
                        "expectation type (PREVENTION, DETECTION, VULNERABILITY) to "
                        "a list of security platform types "
                        f"({', '.join(SECURITY_PLATFORM_TYPES)}).",
                    )
                scoped: dict[str, list[str]] = {}
                for raw_type, raw_platforms in expected_security_platforms.items():
                    etype, err = _enum(
                        str(raw_type),
                        TECHNICAL_EXPECTATION_TYPES,
                        "expected_security_platforms key",
                    )
                    if err:
                        return {}, err
                    if not isinstance(raw_platforms, list):
                        return (
                            {},
                            "expected_security_platforms values must be lists of "
                            f"security platform types (got {raw_platforms!r} for "
                            f"{etype}).",
                        )
                    resolved, err = _enum_list(
                        [str(p) for p in raw_platforms],
                        SECURITY_PLATFORM_TYPES,
                        "expected_security_platforms",
                    )
                    if err:
                        return {}, err
                    scoped[etype] = resolved
                body["action_expected_security_platforms"] = scoped
            if execution_arch:
                value, err = _enum(execution_arch, _ACTION_ARCHS, "execution_arch")
                if err:
                    return {}, err
                body["action_execution_arch"] = value
            if source:
                value, err = _enum(source, _ACTION_SOURCES, "source")
                if err:
                    return {}, err
                body["action_source"] = value
            if status:
                value, err = _enum(status, _ACTION_STATUSES, "status")
                if err:
                    return {}, err
                body["action_status"] = value
            if command_executor:
                value, err = _enum(
                    command_executor, COMMAND_EXECUTORS, "command_executor"
                )
                if err:
                    return {}, err
                body["command_executor"] = value
            if command_content:
                body["command_content"] = command_content
            if dns_resolution_hostname:
                body["dns_resolution_hostname"] = dns_resolution_hostname
            if cleanup_executor:
                value, err = _enum(
                    cleanup_executor, COMMAND_EXECUTORS, "cleanup_executor"
                )
                if err:
                    return {}, err
                body["action_cleanup_executor"] = value
            if cleanup_command:
                body["action_cleanup_command"] = cleanup_command
            if domain_ids is not None:
                body["action_domains"] = domain_ids
            if attack_pattern_ids is not None:
                body["action_attack_patterns"] = attack_pattern_ids
            if tag_ids is not None:
                body["action_tags"] = tag_ids
            if for_create:
                # The API marks these @NotNull; supply the same defaults the UI
                # uses so the agent only has to think about the ones that
                # change behaviour.
                body.setdefault("action_source", "MANUAL")
                body.setdefault("action_status", "VERIFIED")
                body.setdefault("action_execution_arch", "ALL_ARCHITECTURES")
                # Association id lists must never reach the API as null: the
                # payload-creation path feeds them straight into Spring Data's
                # findAllById, which rejects a null argument with a confusing
                # "Ids must not be null" 400. Coerce a missing OR explicitly-null
                # value (the agent can pass {"action_attack_patterns": null} via
                # config, which setdefault would leave untouched) to an empty
                # list when the agent supplies no associations.
                for _assoc_key in ("action_attack_patterns", "action_tags"):
                    if body.get(_assoc_key) is None:
                        body[_assoc_key] = []
            return body, ""

        async def _ensure_ai_tag_id() -> str | None:
            """Resolve (idempotently) the id of the 'ai generated' marker tag.

            Best-effort: any failure just means the action is authored untagged,
            which must never block creation. ``/api/tags/upsert`` dedupes by
            name, so repeated calls converge on one tag.
            """
            resp, err = await _api(
                base_url, headers, "post", "/api/tags/upsert", "resolve ai tag",
                json={"tag_name": _AI_TAG_NAME, "tag_color": _AI_TAG_COLOR},
            )
            if err:
                return None
            try:
                return resp.json().get("tag_id")
            except (ValueError, AttributeError):
                return None

        async def _ensure_fork_marker_tag_id(source_id: str) -> str | None:
            """Resolve (idempotently) the deterministic fork-provenance tag id.

            Keyed on the SOURCE action id so every fork of one source carries the
            same unique marker - the cross-cycle dedup key. Best-effort like the
            AI tag: a failure only means this fork will not be dedup-discoverable
            later, never that the fork is blocked.
            """
            resp, err = await _api(
                base_url, headers, "post", "/api/tags/upsert",
                "resolve fork marker tag",
                json={
                    "tag_name": _fork_marker_tag_name(source_id),
                    "tag_color": _FORK_MARKER_TAG_COLOR,
                },
            )
            if err:
                return None
            try:
                return resp.json().get("tag_id")
            except (ValueError, AttributeError):
                return None

        async def _find_existing_fork(marker_tag_id: str) -> dict | None:
            """Return an existing AI-fork carrying ``marker_tag_id``, else None.

            Deterministic cross-cycle dedup: searches the arsenal by the
            source-unique provenance tag (``injector_contract_tags`` path
            ``tags.id``) and CONFIRMS the marker on the candidate's full state
            before reusing it, so a loose server-side operator coercion can never
            make an unrelated action look like a fork. Best-effort: any error
            just falls through to minting a fresh fork.
            """
            body = _search_body(
                size=5,
                filter_group={
                    "mode": "and",
                    "filters": [
                        {
                            "key": "injector_contract_tags",
                            "operator": "eq",
                            "values": [marker_tag_id],
                        }
                    ],
                },
            )
            resp, err = await _api(
                base_url, headers, "post", f"{_ARSENAL}/search",
                "search existing forks", json=body,
            )
            if err:
                return None
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not isinstance(items, list):
                return None
            for item in items:
                if not isinstance(item, dict):
                    continue
                cand_id = item.get("injector_contract_id") or item.get("action_id")
                if not cand_id:
                    continue
                cur_resp, gerr = await _api(
                    base_url, headers, "get", f"{_ARSENAL}/{cand_id}",
                    "get candidate fork",
                )
                if gerr:
                    continue
                current = cur_resp.json()
                tags = [
                    str(t)
                    for t in (
                        current.get("action_tags")
                        or current.get("injector_contract_tags")
                        or []
                    )
                ]
                if marker_tag_id in tags:
                    return current
            return None

        async def _verify_fork(
            target_id: str, want_name: str, ai_tag_id: str | None
        ) -> tuple[bool, str, dict]:
            """Read back a fork and check the invariants the fork guarantees.

            The CORE gate is that it PRODUCES findings (>=1) - a findings-less
            copy cannot feed a chained step. Name (no "(duplicate)", matches the
            requested name) is repaired-or-failed; the AI tag is best-effort
            (a GET projection that omits ``action_tags`` must not fail the fork).
            """
            resp, err = await _api(
                base_url, headers, "get", f"{_ARSENAL}/{target_id}",
                "verify fork",
            )
            if err:
                return False, err, {}
            current = resp.json()
            name = _derive_action_name(current)
            if _DUPLICATE_SUFFIX_RE.search(name or ""):
                return False, f"the copy is still named '{name}' (unrenamed)", current
            if want_name and name.strip().lower() != want_name.strip().lower():
                return (
                    False,
                    f"the copy is named '{name}', expected '{want_name}'",
                    current,
                )
            if _action_findings_count(current) < 1:
                return (
                    False,
                    "the copy produces no findings (no REGEX output parser landed)",
                    current,
                )
            tags = [str(t) for t in (current.get("action_tags") or [])]
            if ai_tag_id and tags and ai_tag_id not in tags:
                return False, "the 'ai generated' tag did not land", current
            return True, "", current

        async def _stamp_fork_marker(copy_id: str, marker_tag_id: str) -> None:
            """Best-effort: stamp the fork provenance marker on a fresh copy NOW.

            Landed BEFORE the rename/parser finalize - a tiny tag-only PUT that
            cannot hit the output-parser validation the finalize might - so the
            copy is dedup-discoverable even if finalize later fails. A retry then
            REUSES and repairs THIS copy instead of minting a second one, which is
            what lets the finalize path stay a simple adapt-in-place with no
            delete/restart dance. Never raises: finalize re-adds the marker on
            success anyway, so a miss here only weakens dedup on the rare
            hard-PUT-failure path.
            """
            with contextlib.suppress(Exception):
                cur_resp, gerr = await _api(
                    base_url, headers, "get", f"{_ARSENAL}/{copy_id}",
                    "get fresh fork copy",
                )
                if gerr or cur_resp is None:
                    return
                current = cur_resp.json()
                # Read tags from EITHER projection a GET can return them under -
                # action_tags OR injector_contract_tags - the same way
                # _find_existing_fork and _verify_fork do. Reading only action_tags
                # would (a) miss a marker that already landed and re-PUT it, and
                # (b) drop tags held under injector_contract_tags when the PUT
                # rewrites the canonical action_tags field.
                tags = [
                    str(t)
                    for t in (
                        current.get("action_tags")
                        or current.get("injector_contract_tags")
                        or []
                    )
                ]
                if marker_tag_id in tags:
                    return
                merged, merr = _merge_update_body(
                    current, {"action_tags": [*tags, marker_tag_id]},
                    require_domain=False,
                )
                if merr:
                    return
                await _api(
                    base_url, headers, "put", f"{_ARSENAL}/{copy_id}",
                    "stamp fork marker", json=merged,
                )

        async def _finalize_fork(
            target_id: str,
            changes: dict,
            *,
            ai_tag_id: str | None,
            marker_tag_id: str | None,
            want_parsers: object,
            reused: bool,
            current: dict | None = None,
        ) -> str:
            """Adapt a copy IN PLACE until it verifies: rename, tag, apply
            parsers, PUT, read back, repair once.

            Shared by the fresh-duplicate path and the reuse path so a half-baked
            earlier fork converges on reuse rather than a second copy. There is no
            delete/restart dance: the copy already carries the fork provenance
            marker (stamped right after the duplicate), so every failure just
            returns an actionable, self-correcting reason naming the copy id and a
            retry REUSES and repairs the SAME copy. One repair attempt (re-apply
            the REGEX outputs) covers a transient server-side parser drop.
            """

            def _dedup_hint() -> str:
                # Be honest about dedup so a retry's expectation matches reality.
                # The reuse path found THIS copy BY its marker, so it is definitely
                # present. A fresh duplicate had the marker stamped up front, but
                # only when the marker tag resolved (marker_tag_id) AND that
                # best-effort PUT landed. Never assert a guaranteed reuse the copy
                # cannot back up, or the caller retries expecting dedup and STILL
                # leaks a second copy.
                if reused:
                    return (
                        " It carries the fork provenance marker, so a repeat fork "
                        "of this source REUSES and repairs it - it will NOT create "
                        "another copy."
                    )
                if marker_tag_id:
                    return (
                        " It was stamped with the fork provenance marker before "
                        "finalize, so a repeat fork of this source should REUSE and "
                        "repair it rather than create another copy; if that stamp "
                        "did not land, delete the stray '(duplicate)'-named copy "
                        "from the OpenAEV UI before retrying."
                    )
                return (
                    " The fork provenance marker could not be resolved, so a retry "
                    "cannot rediscover this copy and may create a second one; "
                    "delete the stray '(duplicate)'-named copy from the OpenAEV UI "
                    "before retrying."
                )

            if current is None:
                cur_resp, gerr = await _api(
                    base_url, headers, "get", f"{_ARSENAL}/{target_id}",
                    "get fork before finalize",
                )
                if gerr:
                    return f"{gerr} (fork copy action_id {target_id})." + _dedup_hint()
                current = cur_resp.json()

            merge_changes = dict(changes)
            want_name = (
                str(changes.get("action_name") or "").strip()
                or _strip_duplicate_suffix(_derive_action_name(current))
            )
            merge_changes["action_name"] = want_name
            tags = [str(t) for t in (current.get("action_tags") or [])]
            for extra_tag in (
                ai_tag_id,
                marker_tag_id,
                *(str(t) for t in (changes.get("action_tags") or [])),
            ):
                if extra_tag and extra_tag not in tags:
                    tags.append(extra_tag)
            merge_changes["action_tags"] = tags

            merged, merr = _merge_update_body(
                current, merge_changes, require_domain=False
            )
            if merr:
                return merr
            _, perr = await _api(
                base_url, headers, "put", f"{_ARSENAL}/{target_id}",
                "finalize fork", json=merged,
            )
            if perr:
                return (
                    f"{perr} (fork copy action_id {target_id})." + _dedup_hint()
                    + " Fix the reported problem (usually an output-parser field) "
                    "and fork again, or adapt the copy directly with "
                    "action='update'."
                )

            ok, reason, final = await _verify_fork(target_id, want_name, ai_tag_id)
            if not ok and want_parsers:
                # Repair once: re-apply the REGEX outputs on top of the read-back
                # state (covers a transient server-side parser drop).
                repair, rerr = _merge_update_body(
                    final or current,
                    {
                        "action_name": want_name,
                        "action_tags": tags,
                        "action_output_parsers": want_parsers,
                    },
                    require_domain=False,
                )
                if not rerr:
                    _, perr2 = await _api(
                        base_url, headers, "put", f"{_ARSENAL}/{target_id}",
                        "repair fork", json=repair,
                    )
                    if not perr2:
                        ok, reason, final = await _verify_fork(
                            target_id, want_name, ai_tag_id
                        )
            if not ok:
                return (
                    f"Fork verification failed: {reason} (fork copy action_id "
                    f"{target_id})." + _dedup_hint() + " Re-run action='fork' with "
                    "the 'findings' param - a flat list like findings=[{\"type\": "
                    "\"credentials\", \"regex\": \"([^:]+):(.+)\", \"fields\": "
                    "{\"username\": 1, \"password\": 2}}] - so the tool builds the "
                    "output parser for you (a copy that produces no findings "
                    "cannot feed a chained step). If OpenAEV rejected the update, "
                    "its message is above."
                )
            # Count this toward the step-before-more phase gate: a verified fork
            # is a findings-carrying arsenal item the orchestrator now owes a step
            # for. No-op outside the orchestrator's own run. Counting reuse too is
            # deliberate - a failed fork never reaches here, so each source-fork is
            # counted once at its first verified success.
            record_orchestrator_arsenal_authored()
            # NOTE: the session-scoped delete ledger is populated at MINT time in
            # the fork action below (right after the fresh duplicate), NEVER here.
            # _finalize_fork is shared with the cross-cycle REUSE path, whose
            # target_id can be a fork minted by a PRIOR run that _find_existing_fork
            # rediscovered via the source-keyed provenance marker; recording it here
            # would let THIS run delete another session's payload (reuse-then-delete)
            # - the exact cross-session delete the ledger exists to prevent. A copy
            # this run minted itself was already recorded at its mint, so reuse of it
            # stays deletable without this shared path ever adding a (foreign) id.
            verb = "Reused" if reused else "Forked"
            return f"{verb} threat arsenal action.\n" + _fmt_arsenal_action(
                final, base_url, detailed=True
            )

        def _scoped_search_body() -> dict:
            body = _search_body(
                size=min(limit, 50),
                filter_group=parsed_filter,
                text_search=search or None,
            )
            if include_content:
                body["includeContentDetails"] = True
            return body

        if action in ("list", "list_non_tabletop"):
            path = (
                f"{_ARSENAL}/search"
                if action == "list"
                else f"{_ARSENAL}/search/non-tabletop"
            )
            resp, err = await _api(
                base_url, headers, "post", path, "search threat arsenal",
                json=_scoped_search_body(),
            )
            fts_degraded = False
            if err:
                # An unrecognised filter key (the model guessing e.g. base_id)
                # is a dead end - retrying with another key only guesses again,
                # so surface it as an actionable message naming the safe knobs.
                if parsed_filter and _is_unknown_filter_error(err):
                    return (
                        "Error: OpenAEV cannot search the threat arsenal by that "
                        "filter key - it is not an indexed/Queryable attribute, so "
                        "the request was rejected. Use free-text via search=... "
                        "(matches name/description), or filter on an indexed key "
                        "taken from an actual search result: injector_contract_tags "
                        "(tag ids), injector_contract_platforms, "
                        "injector_contract_arch, injector_contract_attack_patterns. "
                        "Do not filter by base_id / payload_id / contract-content "
                        "fields; they are not searchable. Original error: " + err
                    )
                # A transient 5xx on the server-side full-text search is absorbed:
                # retry once WITHOUT textSearch (filter/tag scope still applies) so
                # the search degrades to results rather than a hard failure.
                if search and _is_transient_server_error(err):
                    retry_body = _search_body(
                        size=min(limit, 50),
                        filter_group=parsed_filter,
                        text_search=None,
                    )
                    if include_content:
                        retry_body["includeContentDetails"] = True
                    resp, err = await _api(
                        base_url, headers, "post", path,
                        "search threat arsenal (no full-text)",
                        json=retry_body,
                    )
                    fts_degraded = not err
                if err:
                    return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not isinstance(items, list):
                items = [items] if items else []
            total = data.get("totalElements") if isinstance(data, dict) else None
            if providing_narrowed:
                fork_body = _search_body(
                    size=min(limit, 50),
                    filter_group=_without_providing_filter(parsed_filter),
                    text_search=search or None,
                )
                if include_content:
                    fork_body["includeContentDetails"] = True
                resp2, err2 = await _api(
                    base_url, headers, "post", path,
                    "search threat arsenal (fork candidates)",
                    json=fork_body,
                )
                if not err2:
                    data2 = resp2.json()
                    extra = (
                        data2.get("content", data2)
                        if isinstance(data2, dict)
                        else data2
                    )
                    if not isinstance(extra, list):
                        extra = [extra] if extra else []
                    items = _merge_fork_candidates(items, extra)
                    total = len(items)
            if not items:
                if fts_degraded:
                    return (
                        "No threat arsenal actions found. Note: OpenAEV's "
                        "full-text search was temporarily unavailable (5xx), so "
                        f"the free-text term '{search}' was dropped and only the "
                        "filter/tag scope was applied - retry shortly to include "
                        "the name/description match."
                    )
                return "No threat arsenal actions found."
            header = f"Threat arsenal actions ({len(items)}"
            header += f" of {total})" if total is not None else ")"
            if fts_degraded:
                header += (
                    f" [full-text search for '{search}' was temporarily "
                    "unavailable and was skipped; results are filter/tag scoped]"
                )
            return f"{header}:\n\n" + "\n\n".join(
                _fmt_arsenal_action(a, base_url) for a in items
            )

        if action == "get":
            if not action_id:
                return "action_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"{_ARSENAL}/{action_id}",
                "get threat arsenal action",
            )
            if err:
                return err
            return _fmt_arsenal_action(resp.json(), base_url, detailed=True)

        if action == "create":
            body, berr = _body(for_create=True)
            if berr:
                return berr
            # No-duplicate-fallback guard: the "(duplicate)" suffix is exactly what
            # OpenAEV's POST /{id}/duplicate stamps on a copy. A 'create' carrying
            # it is the signature of a broken fork fallback - re-minting a
            # half-forked copy as a BRAND-NEW action, which leaves BOTH the intended
            # name and an "X (duplicate)" polluting the catalog (the very leak this
            # tool is meant to prevent). A payload-based dead-end is adapted with
            # action='fork' (duplicate -> rename -> AI tag -> parsers -> verify,
            # reusing an existing fork of the same source), never by creating a
            # duplicate-named original - so keep this cheap guard even though the
            # fork path no longer has a create fallback.
            if _DUPLICATE_SUFFIX_RE.search(str(body.get("action_name") or "")):
                return (
                    "Refused to create an action whose name ends in '(duplicate)'. "
                    "That suffix is what OpenAEV's duplicate endpoint stamps on a "
                    "copy, so creating it as a new action is the broken fork "
                    "fallback that leaves BOTH the original and an orphaned "
                    "'(duplicate)' in the catalog. To adapt a payload-based "
                    "dead-end into a findings-producing action, use action='fork' "
                    "with the SOURCE action_id and the 'findings' param; fork "
                    "reuses and repairs an existing fork of the same source "
                    "instead of minting a second copy. Do not re-create the "
                    "half-forked copy here."
                )
            missing = [
                label
                for label, key in (
                    ("action_type", "action_type"),
                    ("name", "action_name"),
                    ("platforms", "action_platforms"),
                    ("expectations", "action_expectations"),
                    ("domain_ids", "action_domains"),
                )
                if not body.get(key)
            ]
            if missing:
                return (
                    f"{', '.join(missing)} are required for 'create'. expectations "
                    "declares what the action measures - use PREVENTION and/or "
                    "DETECTION for an attack you want blocked or alerted on, "
                    "VULNERABILITY for a CVE assessment. Use list_openaev_domains "
                    "for domain_ids."
                )
            # Findings-required gate (WS4-C): a COMMAND/EXECUTABLE action that runs
            # and emits output nobody parses produces no findings, and OpenAEV
            # chaining is findings-based - so it is chain-useless the moment it is
            # created (the "dumb weak payload" symptom). Enforce it in code, before
            # the write lands, so the caller (orchestrator OR delegate Payload
            # Creator) gets an actionable message and self-corrects instead of
            # minting a dead-end. Scoped BOTH to command payloads (a findings-less
            # PREVENTION/DETECTION expectation test measured by a security platform
            # is exempt) AND to autonomous authors via arsenal_findings_required()
            # - a human/other agent building a command test through the same tool
            # keeps the unrestricted contract.
            from app.services.builtin_tools.filtering import arsenal_findings_required

            if (
                arsenal_findings_required()
                and _body_is_command_based(body)
                and _action_findings_count(body) < 1
            ):
                return (
                    "Refused to create a chain-useless action: this is a "
                    "command/executable payload that will RUN but declares no "
                    "REGEX output parser, so it produces NO findings and cannot "
                    "feed a chained step (OpenAEV chaining is findings-based). "
                    "Re-issue 'create' with action_output_parsers (via config) "
                    "containing >=1 REGEX parser whose "
                    "output_parser_contract_output_elements capture a finding "
                    "(output_parser_type=REGEX; output_parser_mode=STDOUT/STDERR/"
                    "READ_FILE; element types like ipv4, hostname, credentials, "
                    "cve, port, text). Only a findings-less PREVENTION/DETECTION "
                    "expectation test (measured by a security platform, not a "
                    "parser) is exempt - and that is not a command payload."
                )
            # Stamp the AI-provenance tag so every authored action is traceable
            # in the Threat Arsenal UI without the agent having to resolve and
            # pass a tag id itself. Deterministic + additive: it never replaces
            # tags the caller supplied. Bound findings-required create
            # concurrency (same orphan-race guard as fork); no-op for chat /
            # non-autonomous callers.
            async with _arsenal_write_guard(action):
                ai_tag_id = await _ensure_ai_tag_id()
                if ai_tag_id:
                    tags = body.get("action_tags") or []
                    if ai_tag_id not in tags:
                        body["action_tags"] = [*tags, ai_tag_id]
                resp, err = await _api(
                    base_url, headers, "post", _ARSENAL,
                    "create threat arsenal action",
                    json=body,
                )
                if err:
                    return err
                # Count toward the step-before-more phase gate (no-op outside the
                # orchestrator's own run). The orchestrator is free to create, so
                # this fires for it too now.
                record_orchestrator_arsenal_authored()
                created = resp.json()
                # Remember this id in the per-run ledger so the orchestrator may
                # later purge it if it goes unused (session-scoped delete). No-op
                # outside the orchestrator's own run.
                record_orchestrator_session_action(
                    created.get("injector_contract_id") or created.get("action_id")
                )
                return "Created threat arsenal action.\n" + _fmt_arsenal_action(
                    created, base_url, detailed=True
                )

        if action == "update":
            if not action_id:
                return "action_id is required for action 'update'."
            body, berr = _body(for_create=False)
            if berr:
                return berr
            if not body:
                return (
                    "Provide at least one field to update (name, description, "
                    "platforms, expectations, command_content, cleanup_command, "
                    "domain_ids, attack_pattern_ids, tag_ids, or config)."
                )
            # The API's PUT is a full-replace validated as a whole, so a partial
            # patch is rejected. Fetch the current action and merge the caller's
            # changes on top so the request carries every mandatory field and
            # leaves unspecified fields intact.
            current_resp, gerr = await _api(
                base_url, headers, "get", f"{_ARSENAL}/{action_id}",
                "get threat arsenal action",
            )
            if gerr:
                return gerr
            merged, merr = _merge_update_body(current_resp.json(), body)
            if merr:
                return merr
            resp, err = await _api(
                base_url, headers, "put", f"{_ARSENAL}/{action_id}",
                "update threat arsenal action", json=merged,
            )
            if err:
                return err
            return "Updated threat arsenal action.\n" + _fmt_arsenal_action(
                resp.json(), base_url, detailed=True
            )

        if action == "fork":
            if not action_id:
                return "action_id is required for action 'fork'."
            # Caller-supplied fields (REGEX parsers via config, optional name /
            # domain / expectation overrides) that make the copy chain-useful.
            fork_changes, berr = _body(for_create=False)
            if berr:
                return berr
            # Bound findings-required fork concurrency (orphan-race guard). The
            # whole duplicate -> finalize -> verify sequence runs under one
            # permit so at most _ARSENAL_WRITE_CONCURRENCY forks are in flight -
            # the parallel-burst races that minted the orphaned copies.
            async with _arsenal_write_guard(action):
                want_parsers = fork_changes.get("action_output_parsers")
                ai_tag_id = await _ensure_ai_tag_id()
                marker_tag_id = await _ensure_fork_marker_tag_id(action_id)

                # 1) Deterministic cross-cycle dedup: reuse (and repair) an
                # existing AI-fork of this source instead of minting a second
                # identical copy.
                if marker_tag_id:
                    existing = await _find_existing_fork(marker_tag_id)
                    if existing is not None:
                        existing_id = existing.get(
                            "injector_contract_id"
                        ) or existing.get("action_id")
                        if existing_id:
                            return await _finalize_fork(
                                existing_id,
                                fork_changes,
                                ai_tag_id=ai_tag_id,
                                marker_tag_id=marker_tag_id,
                                want_parsers=want_parsers,
                                reused=True,
                                current=existing,
                            )

                # 2) Duplicate the source, then finalize atomically (rename ->
                # AI tag -> parsers -> PUT -> read-back verify -> repair once).
                dup_resp, err = await _api(
                    base_url, headers, "post",
                    f"{_ARSENAL}/{action_id}/duplicate",
                    "duplicate threat arsenal action",
                )
                if err:
                    native = _native_duplicate_error(err)
                    return native or err
                dup = dup_resp.json()
                new_id = dup.get("injector_contract_id") or dup.get("action_id")
                if not new_id:
                    return "Fork failed: the duplicate call returned no action id."
                # Session-scoped delete ledger: record the id THIS run just MINTED
                # (the fresh duplicate) so the orchestrator may later purge this copy
                # if it goes unused. Recorded HERE at the mint - and never on the
                # cross-cycle REUSE branch above, whose id can belong to a PRIOR run
                # rediscovered by the source-keyed marker (recording that would let
                # this run delete another session's payload). Recording at the mint
                # rather than after verify also keeps a copy this run made deletable
                # even if finalize fails - a half-baked own copy is exactly what
                # session-scoped cleanup should be able to remove. No-op outside the
                # orchestrator's own run.
                record_orchestrator_session_action(new_id)
                # Land the fork provenance marker on the copy up front so it is
                # dedup-discoverable even if the finalize below fails - a retry
                # then REUSES and repairs THIS copy instead of minting a second
                # one. This is what lets finalize stay a simple adapt-in-place
                # with no delete/restart dance.
                if marker_tag_id:
                    await _stamp_fork_marker(new_id, marker_tag_id)
                return await _finalize_fork(
                    new_id,
                    fork_changes,
                    ai_tag_id=ai_tag_id,
                    marker_tag_id=marker_tag_id,
                    want_parsers=want_parsers,
                    reused=False,
                )

        if action == "duplicate":
            if not action_id:
                return "action_id is required for action 'duplicate'."
            # Bound findings-required duplicate concurrency (the same orphan-race
            # guard fork/create use): a raw duplicate MINTS a copy too, so a
            # parallel burst must be capped exactly like fork. No-op for chat /
            # non-autonomous callers.
            async with _arsenal_write_guard(action):
                resp, err = await _api(
                    base_url, headers, "post",
                    f"{_ARSENAL}/{action_id}/duplicate",
                    "duplicate threat arsenal action",
                )
                if err:
                    native = _native_duplicate_error(err)
                    if native:
                        return native
                    return err
                dup = resp.json()
                new_id = dup.get("injector_contract_id") or dup.get("action_id")
                # Account for the minted copy IMMEDIATELY, before the best-effort
                # tagging awaits below: the POST already created it, so if this task
                # is cancelled during tagging the copy must still be counted toward
                # the phase gate AND recorded as one this run may clean up
                # (session-scoped delete) - otherwise a cancelled-mid-tag duplicate
                # leaks an orphan this session can no longer purge. A duplicate
                # MINTS an arsenal item exactly like fork/create, so it consumes the
                # phase-gate lead the same way and a burst of raw duplicates cannot
                # bypass the gate. Both no-op outside the orchestrator's own run;
                # the ledger add ignores a missing id.
                record_orchestrator_arsenal_authored()
                record_orchestrator_session_action(new_id)
                # Stamp the AI-provenance tag on the copy (best-effort). A bare
                # duplicate otherwise inherits only the source's tags, leaving the
                # copy without the 'ai generated' marker every authored action
                # carries. Never blocks: a tagging failure still returns the copy.
                ai_tag_id = await _ensure_ai_tag_id()
                if ai_tag_id and new_id:
                    cur_resp, gerr = await _api(
                        base_url, headers, "get", f"{_ARSENAL}/{new_id}",
                        "get duplicated action",
                    )
                    if not gerr:
                        current = cur_resp.json()
                        tags = [str(t) for t in (current.get("action_tags") or [])]
                        if ai_tag_id not in tags:
                            merged, merr = _merge_update_body(
                                current, {"action_tags": [*tags, ai_tag_id]}
                            )
                            if not merr:
                                put_resp, perr = await _api(
                                    base_url, headers, "put",
                                    f"{_ARSENAL}/{new_id}",
                                    "tag duplicated action", json=merged,
                                )
                                if not perr:
                                    dup = put_resp.json()
                return "Duplicated threat arsenal action.\n" + _fmt_arsenal_action(
                    dup, base_url
                )

        if action == "delete":
            if not action_id:
                return "action_id is required for action 'delete'."
            # Session-scoped cleanup: the orchestrator is FREE to purge its OWN
            # unused or bad creations, but must NEVER delete a payload from
            # another session or authored elsewhere (Samuel's one hard line).
            # orchestrator_may_delete_action is True for any non-orchestrator
            # caller (chat / other agents keep the full delete contract) and,
            # during the orchestrator's own run, only for an id this run created
            # (create/fork/duplicate populate the per-run ledger).
            if not orchestrator_may_delete_action(action_id):
                return (
                    f"Refused to delete {action_id}: the Attack Path Orchestrator "
                    "may only delete threat-arsenal actions it CREATED in this run "
                    "(an unused or bad fork/create you want to purge). This id was "
                    "not authored by the current run, so it belongs to another "
                    "session or was authored elsewhere - never delete those. To "
                    "clean up, delete one of the copies THIS run created, or leave "
                    "it in place."
                )
            tenant_id = await capabilities.tenant_id()
            if not tenant_id:
                return (
                    "Deleting a threat arsenal action requires the tenant-scoped "
                    "route, and the default tenant id could not be read from "
                    "GET /api/settings (the API token needs platform-settings "
                    "read access). Delete it from the OpenAEV UI instead."
                )
            _, err = await _api(
                base_url, headers, "delete", f"{_ARSENAL}/{action_id}",
                "delete threat arsenal action", tenant_id=tenant_id,
            )
            if err:
                return err
            return f"Deleted threat arsenal action {action_id}."

        if action == "bulk_delete":
            if parsed_filter is None and not search:
                return (
                    "bulk_delete needs a scope: pass filter_group and/or search so "
                    "the deletion is bounded. It permanently removes every action "
                    "matching that scope."
                )
            resp, err = await _api(
                base_url, headers, "post", f"{_ARSENAL}/bulk-delete",
                "bulk delete threat arsenal actions", json=_scoped_search_body(),
            )
            if err:
                return err
            out = resp.json() if resp.content else {}
            count = out.get("deleted_count", out.get("deletedCount", "?"))
            return (
                f"Bulk-deleted {count} threat arsenal action(s). Bulk deletes are "
                "NOT atomic (each chunk commits separately), so the returned count "
                "is the source of truth for what actually went."
            )

        if action in ("domain_counts", "author_counts", "facet_counts"):
            endpoint = action.replace("_", "-")
            resp, err = await _api(
                base_url, headers, "post", f"{_ARSENAL}/{endpoint}",
                f"threat arsenal {action}", json=_scoped_search_body(),
            )
            if err:
                return err
            return json_answer(resp.json(), 3000)

        if action == "argument_types":
            # The trailing slash is REQUIRED by the mapping. The result is
            # narrower when INJECT_CHAINING is disabled (3 primitive types).
            resp, err = await _api(
                base_url, headers, "get", f"{_ARSENAL}/argument-types/",
                "list argument types",
            )
            if err:
                return err
            types = resp.json() or []
            chaining = await capabilities.is_feature_enabled("INJECT_CHAINING")
            note = (
                ""
                if chaining
                else (
                    "\n\nNote: INJECT_CHAINING is disabled on this platform, so "
                    "only the base primitive types are offered."
                )
            )
            return f"Argument types ({len(types)}): {json_answer(types, 2000)}{note}"

        if action == "security_platforms":
            if not action_id:
                return "action_id is required for action 'security_platforms'."
            tenant_id = await capabilities.tenant_id()
            if not tenant_id:
                return (
                    "Reading the security platforms of an action requires the "
                    "tenant-scoped route, and the default tenant id could not be "
                    "read from GET /api/settings."
                )
            resp, err = await _api(
                base_url, headers, "get",
                f"{_ARSENAL}/{action_id}/security-platforms",
                "list action security platforms", tenant_id=tenant_id,
            )
            if err:
                return err
            items = resp.json() or []
            if not items:
                return (
                    f"No security platforms are referenced by the detection "
                    f"remediations of action {action_id}."
                )
            lines = [f"Security platforms in the remediation ({len(items)}):"]
            for sp in items:
                label = sp.get("asset_name") or sp.get("security_platform_name", "?")
                sp_id = sp.get("asset_id") or sp.get("security_platform_id", "N/A")
                sp_type = sp.get("security_platform_type", "?")
                lines.append(f"- {label} [{sp_type}] (security_platform_id: {sp_id})")
            return "\n".join(lines)

        if action == "schemas":
            resp, err = await _api(
                base_url, headers, "post", f"{_ARSENAL}/schemas",
                "threat arsenal schemas",
                params={"filterableOnly": "true"},
                json=[],
            )
            if err:
                return err
            return json_answer(resp.json(), 3000)

        return (
            f"Unknown action '{action}'. Valid actions: list, list_non_tabletop, "
            "get, create, update, fork, duplicate, delete, bulk_delete, "
            "domain_counts, author_counts, facet_counts, argument_types, "
            "security_platforms, schemas."
        )

    executor.register("manage_openaev_threat_arsenal", manage_openaev_threat_arsenal)

    return [
        ToolDefinition(
            name="manage_openaev_threat_arsenal",
            description=(
                "Manage the OpenAEV Threat Arsenal - the catalogue of ACTIONS "
                "(an injector contract plus its payload) that simulations and "
                "atomic tests execute. This is the v3 replacement for the "
                "deprecated payload API and the right tool for browsing or "
                "authoring attack content. Actions: 'list' (search; "
                "include_content=true also returns the contract content), "
                "'list_non_tabletop' (technical actions only - excludes email, "
                "SMS, challenges and media pressure), 'get', 'create', 'update', "
                "'fork', 'duplicate', 'delete', 'bulk_delete' (scoped, NOT "
                "atomic), 'domain_counts' / 'author_counts' / 'facet_counts' "
                "(aggregates for a search scope), 'argument_types', "
                "'security_platforms' (which products the action's detection "
                "remediations target), 'schemas' (filterable attribute names). "
                "IMPORTANT: 'expectations' declares what the action MEASURES once "
                "executed - PREVENTION (the attack should be blocked), DETECTION "
                "(it should raise an alert), VULNERABILITY (it proves a CVE is "
                "present). An action with no expectations executes but measures "
                "nothing, so it proves nothing about the defenses. REUSE a "
                "contract that already PRODUCES the needed findings. If a "
                "PAYLOAD-BASED action is relevant but produces none, 'fork' it in "
                "ONE call: pass action_id (the source) and the outputs via the "
                "dedicated 'findings' param - a flat list like findings="
                '[{"type": "credentials", "regex": "([^:]+):(.+)", "fields": '
                '{"username": 1, "password": 2}}] - and the tool builds OpenAEV\'s '
                "nested output-parser DTO (isFinding=true) for you. Do NOT "
                "hand-build action_output_parsers in config; that flat/nested JSON "
                "is what kept failing. 'fork' duplicates the source, strips "
                "'(duplicate)', stamps 'ai generated', applies the findings, then "
                "READS BACK and verifies the copy produces findings - and reuses "
                "an existing fork of the same source instead of making a second "
                "copy. Prefer 'fork' over the manual 'duplicate'+'update' dance. "
                "NEVER update a shared catalog original in place; NEVER "
                "fork/duplicate a native injector (no payload) - reuse its id. "
                "'create' is only for genuinely new content when nothing reusable "
                "or forkable serves the objective. On update/fork, pass findings "
                "(and any changed fields); the tool fetches the current action and "
                "merges. Every action authored through this tool is automatically "
                "tagged 'ai generated'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "list_non_tabletop", "get", "create", "update",
                            "fork", "duplicate", "delete", "bulk_delete",
                            "domain_counts", "author_counts", "facet_counts",
                            "argument_types", "security_platforms", "schemas",
                        ],
                    },
                    "action_id": {"type": "string", "description": "Threat arsenal action (injector contract) ID."},
                    "action_type": {
                        "type": "string",
                        "enum": _ACTION_TYPES,
                        "description": "Payload kind of the action (required on create).",
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "platforms": {
                        "type": "array",
                        "items": {"type": "string", "enum": PLATFORMS},
                        "description": "Target platforms (required on create).",
                    },
                    "expectations": {
                        "type": "array",
                        "items": {"type": "string", "enum": PAYLOAD_EXPECTATION_TYPES},
                        "description": (
                            "What the action measures. PREVENTION = the control "
                            "should block it; DETECTION = the control should alert "
                            "on it; VULNERABILITY = the action proves a CVE is "
                            "present (inverted polarity - success means NOT "
                            "vulnerable); human validation is a manual inject, "
                            "not an action expectation. Required on create. Pair with "
                            "expected_security_platforms to scope which product "
                            "families should catch each expectation."
                        ),
                    },
                    "expected_security_platforms": {
                        "type": "object",
                        "description": (
                            "OPTIONAL per-expectation scope: which security product "
                            "families are expected to fulfil each TECHNICAL "
                            "expectation. Object keyed by expectation type "
                            "(PREVENTION / DETECTION / VULNERABILITY), each mapping "
                            "to a list of platform types from "
                            f"{', '.join(SECURITY_PLATFORM_TYPES)}. Example: "
                            '{"DETECTION": ["EDR", "XDR", "SIEM"], "PREVENTION": '
                            '["EDR"]}. STRONGLY PREFERRED over the default: scope '
                            "each expectation to the controls that could realistically "
                            "catch that technique (e.g. an EDR/XDR for endpoint "
                            "prevention/detection, a SIEM/NDR for network or log-based "
                            "detection, a VULNERABILITY_SCANNER for a CVE check) "
                            "instead of leaving it as 'any security platform', which "
                            "over-counts unrelated tools as expected to respond. Omit "
                            "a type, or give it an empty list, to keep 'any security "
                            "platform' for that one."
                        ),
                        # Constrain the KEYS to the technical expectation types so
                        # the schema matches the runtime validation (the handler
                        # rejects any other key) - otherwise a client/LLM could emit
                        # a payload that validates yet fails at execution.
                        "propertyNames": {"enum": TECHNICAL_EXPECTATION_TYPES},
                        "additionalProperties": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": SECURITY_PLATFORM_TYPES,
                            },
                        },
                    },
                    "execution_arch": {"type": "string", "enum": _ACTION_ARCHS},
                    "source": {"type": "string", "enum": _ACTION_SOURCES},
                    "status": {"type": "string", "enum": _ACTION_STATUSES},
                    "command_executor": {"type": "string", "enum": COMMAND_EXECUTORS},
                    "command_content": {"type": "string", "description": "Command to run (Command actions)."},
                    "dns_resolution_hostname": {"type": "string", "description": "Hostname to resolve (DnsResolution actions)."},
                    "cleanup_executor": {"type": "string", "enum": COMMAND_EXECUTORS},
                    "cleanup_command": {"type": "string", "description": "Command that reverts the action. Always provide one for destructive content."},
                    "domain_ids": {"type": "array", "items": {"type": "string"}, "description": "Security domain IDs (required on create - see list_openaev_domains)."},
                    "attack_pattern_ids": {"type": "array", "items": {"type": "string"}},
                    "tag_ids": {"type": "array", "items": {"type": "string"}},
                    "class_name": {"type": "string", "description": "Reserved for schema lookups."},
                    "filter_group": {"type": "string", "description": "FilterGroup JSON to scope list / counts / bulk_delete."},
                    "include_content": {"type": "boolean", "description": "Include contract content details in list results.", "default": False},
                    "config": {"type": "string", "description": "JSON object of raw action_* fields merged into the body (e.g. action_arguments, action_prerequisites, action_detection_remediations). For output parsers, prefer the dedicated 'findings' param over hand-building action_output_parsers here."},
                    "findings": {
                        "type": "array",
                        "description": (
                            "The findings this action PRODUCES, for create/fork/"
                            "update. This is the preferred, model-friendly way to "
                            "add outputs - the tool expands each entry into "
                            "OpenAEV's nested output-parser DTO and marks it a "
                            "finding, so a forked/created action actually feeds "
                            "chained steps. Each entry: {\"type\": a finding type "
                            "from the output_types list (credentials, username, "
                            "ipv4, cve, port, computer, share, ...), \"regex\": the "
                            "capture pattern, and OPTIONALLY \"fields\": a "
                            "{primitive: capture-group-index} map (e.g. "
                            "{\"username\": 1, \"password\": 2}) and \"mode\": "
                            "STDOUT (default), STDERR or READ_FILE}. Example: "
                            '[{"type": "credentials", "regex": "([^:]+):(.+)", '
                            '"fields": {"username": 1, "password": 2}}].'
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": CONTRACT_OUTPUT_TYPES},
                                "regex": {"type": "string"},
                                "mode": {"type": "string", "enum": ["STDOUT", "STDERR", "READ_FILE"]},
                                "fields": {"type": "object"},
                                "name": {"type": "string"},
                                "key": {"type": "string"},
                            },
                            "required": ["type", "regex"],
                        },
                    },
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                    "output_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "For 'list'/'list_non_tabletop' ONLY (rejected on any other "
                            "action): prefer actions that PRODUCE at least one of these "
                            "finding types (any-of). Payload-based matches that produce "
                            "none are still returned as FORK CANDIDATES; native injectors "
                            "without a payload are not. Allowed: "
                            f"{', '.join(CONTRACT_OUTPUT_TYPES)}. "
                            "Use action='get' to see a specific action's produced findings."
                        ),
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]

# SPDX-License-Identifier: Apache-2.0

"""OpenAEV integration – Adversarial Exposure Validation platform.

Provides comprehensive tools for managing OpenAEV scenarios, assets,
teams, attack patterns, payloads, injects, atomic testing, and more
via the OpenAEV REST API.

Token format passed by the dispatcher: ``base_url||api_token``
(the extra_config base_url is prepended by ``_get_token`` in
integration_tools.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

import httpx

from app.core.egress import egress_client
from app.core.url_identity import redact_url
from app.integrations.http import (
    JWT_PROACTIVE_REFRESH_SKEW_SECONDS as _JWT_PROACTIVE_REFRESH_SKEW_SECONDS,
)
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    AutoRefreshClient,
    RefreshFn,
    clip_text,
    is_not_found_error,
    safe_request,
)
from app.integrations.http import (
    RefreshableHeaders as _RefreshableHeaders,
)
from app.integrations.http import (
    bearer_seconds_remaining as _bearer_seconds_remaining,
)
from app.integrations.http import (
    ensure_fresh_token as _ensure_fresh_token,
)

logger = logging.getLogger(__name__)

_PROVIDER = "OpenAEV"

#: Characters of compact JSON a single-object read (an inject, its status trace,
#: an atomic test, a dashboard, the settings) answers before its cut marker.
JSON_READ_CHARS = 12000


def json_answer(value: Any, limit: int = JSON_READ_CHARS) -> str:
    """*value* as compact JSON, cut past *limit* characters with a marker.

    An OpenAEV answer never ends mid-JSON without saying so: a cut answer ends
    with ``... [truncated, N more characters]`` (``clip_text``), so the agent
    knows the object goes on and reads a narrower call instead of trusting a
    partial one.
    """
    return clip_text(json.dumps(value, default=str), limit)


def json_window(value: Any, offset: int = 0, limit: int = JSON_READ_CHARS) -> str:
    """*value* as compact JSON, read by *limit*-character windows from *offset*.

    A read past *limit* is no longer a dead end: the window is cut at an exact
    character (never a word boundary, so the next window starts where this one
    stops) and ends with ``... [truncated, N more characters: read on with
    offset=END]``; a window past the first starts with ``[characters A to B of
    TOTAL]``. Offset 0 on an answer that fits is the plain JSON.
    """
    text = json.dumps(value, default=str)
    total = len(text)
    offset = max(0, offset)
    if offset == 0 and total <= limit:
        return text
    if offset >= total:
        return f"offset {offset} is past the end: the answer is {total} characters."
    end = min(total, offset + limit)
    head = f"[characters {offset} to {end} of {total}]\n" if offset else ""
    tail = f" ... [truncated, {total - end} more characters: read on with offset={end}]" if end < total else ""
    return f"{head}{text[offset:end]}{tail}"


#: Inject content keys that name targets, mapped to the inject relation OpenAEV
#: actually targets. The "Update the inject" form and the executor both read the
#: relations (inject_assets, inject_asset_groups); the same ids left in
#: inject_content alone are ignored: the form shows the field empty and
#: "Required", the injector receives no target.
_CONTENT_TARGET_KEYS: tuple[tuple[str, str], ...] = (
    ("asset_groups", "asset_group_ids"),
    ("assets", "asset_ids"),
)


def _content_ids(content: dict[str, Any], key: str) -> list[str]:
    raw = content.get(key)
    if not isinstance(raw, list):
        return []
    return [v.strip() for v in raw if isinstance(v, str) and v.strip()]


def mirror_content_targets(
    content: Any,
    asset_ids: list[str] | None,
    asset_group_ids: list[str] | None,
) -> tuple[list[str] | None, list[str] | None, str, str]:
    """Carry the targets an inject content names onto the inject relations.

    Returns ``(asset_ids, asset_group_ids, note, error)``. A relation the caller
    did not pass takes the ids of ``content.asset_groups`` / ``content.assets``,
    and *note* says so. A relation the caller passed that misses an id the
    content names is refused in *error* before any write: OpenAEV targets only
    the relation, so that id would be silently skipped.
    """
    if not isinstance(content, dict):
        return asset_ids, asset_group_ids, "", ""
    relations: dict[str, list[str] | None] = {
        "asset_ids": asset_ids,
        "asset_group_ids": asset_group_ids,
    }
    notes: list[str] = []
    for key, param in _CONTENT_TARGET_KEYS:
        ids = _content_ids(content, key)
        if not ids:
            continue
        given = relations[param]
        if given is None:
            relations[param] = ids
            notes.append(f"{param} set from content.{key} ({len(ids)})")
            continue
        missing = [i for i in ids if i not in given]
        if missing:
            return (
                asset_ids,
                asset_group_ids,
                "",
                (
                    f"content.{key} names {', '.join(missing)}, absent from {param}. "
                    f"OpenAEV targets only {param} (the inject form and the executor "
                    f"read it, never content.{key}): add them to {param} or drop "
                    f"them from content."
                ),
            )
    note = ""
    if notes:
        note = "Targets: " + "; ".join(notes) + ", since OpenAEV targets the inject relations, never the content."
    return relations["asset_ids"], relations["asset_group_ids"], note, ""


def _relation_ids(raw: Any, id_key: str) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for v in raw:
        if isinstance(v, str) and v:
            out.append(v)
        elif isinstance(v, dict) and v.get(id_key):
            out.append(str(v[id_key]))
    return out


def inject_carries_relations(inject: Any) -> bool:
    """True when an inject answer carries its target relations at all.

    OpenAEV answers the atomic-testing routes with ``InjectResultOverviewOutput``,
    which has no ``inject_assets`` / ``inject_asset_groups``: their absence
    means "not in this answer", never "empty".
    """
    return isinstance(inject, dict) and ("inject_assets" in inject or "inject_asset_groups" in inject)


def inject_targets_line(
    inject: Any,
    asked_asset_ids: list[str] | None = None,
    asked_asset_group_ids: list[str] | None = None,
) -> str:
    """One line naming the targets an inject answer stores on its relations.

    Empty when the answer is not an object or carries neither relation key (the
    relations are absent from that answer, not empty: no count, no warning). A
    content that selects targets (``target_selector``) with both relations
    empty is flagged: the injector would receive no target. A ``manual``
    selector reads ``content.targets`` instead: those are named, and only an
    empty list is flagged. Ids asked by the
    write (*asked_asset_ids*, *asked_asset_group_ids*) and missing from the
    stored relations are named: OpenAEV drops an unknown id or an id of another
    tenant without an error.
    """
    if not inject_carries_relations(inject):
        return ""
    groups = _relation_ids(inject.get("inject_asset_groups"), "asset_group_id")
    assets = _relation_ids(inject.get("inject_assets"), "asset_id")
    line = (
        f"Targets stored: {len(groups)} asset group(s)"
        + (f" [{', '.join(groups)}]" if groups else "")
        + f", {len(assets)} asset(s)"
        + (f" [{', '.join(assets)}]" if assets else "")
        + "."
    )
    missing = [a for a in asked_asset_ids or [] if a not in assets]
    missing += [g for g in asked_asset_group_ids or [] if g not in groups]
    if missing:
        line += f" Not stored: {', '.join(missing)}; unknown id or another tenant."
    content = inject.get("inject_content")
    if not isinstance(content, dict) or not content.get("target_selector"):
        return line
    if content.get("target_selector") == "manual":
        manual = _manual_content_targets(content)
        if manual:
            return line + f" Manual targets (content.targets, read by the injector): {', '.join(manual)}."
        return line + (
            " Warning: the content selects manual targets but content.targets is "
            "empty: the injector receives no target."
        )
    if not groups and not assets:
        line += (
            " Warning: the content selects targets but no asset or asset group "
            "is stored: the injector receives no target."
        )
    return line


def _manual_content_targets(content: dict[str, Any]) -> list[str]:
    """The targets a ``target_selector = manual`` content names in ``targets``.

    The injector (Nmap and the like) reads them from the content, never from the
    inject relations. OpenAEV stores them as one text (comma or line separated)
    or as a list.
    """
    raw = content.get("targets")
    values = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        out.extend(part.strip() for part in re.split(r"[,\n]", value) if part.strip())
    return out


def stored_targets_line(
    answer: Any,
    stored: Any,
    asked_asset_ids: list[str] | None = None,
    asked_asset_group_ids: list[str] | None = None,
) -> str:
    """The targets line of an atomic-test write or read.

    *stored* is the follow-up ``GET /api/injects/{id}`` answer (None when it
    failed), read first; then the write's own *answer*. When neither carries the
    relations the line says the stored targets are unknown instead of claiming
    zero.
    """
    for source in (stored, answer):
        if inject_carries_relations(source):
            return inject_targets_line(source, asked_asset_ids, asked_asset_group_ids)
    return (
        "Targets stored: not readable (OpenAEV's answer carries no relations and "
        "the inject read failed); read the inject again to check them."
    )


#: Inject keys moved to the end of an inject read's JSON: the bulky ones
#: (content, documents, expectation and status trees) so the relations and
#: scalar fields are read before any cut marker.
_INJECT_BULKY_KEYS: tuple[str, ...] = (
    "inject_content",
    "inject_documents",
    "inject_expectations",
    "inject_status",
    "inject_kill_chain_phases",
    "inject_attack_patterns",
)


def inject_read_answer(inject: Any, limit: int = JSON_READ_CHARS, offset: int = 0) -> str:
    """An inject read: its targets line first, then its JSON, bulky keys last.

    The stored targets (``inject_asset_groups``, ``inject_assets``) are always
    named in full ahead of the JSON, so a large ``inject_content`` (a long
    command line, many arguments) that pushes the answer past *limit* never
    hides the relations the executor targets. The JSON is read by windows
    (``json_window``): a cut names the *offset* that reads on, the targets
    line still first. Not an object: its JSON alone.
    """
    if not isinstance(inject, dict):
        return json_window(inject, offset, limit)
    ordered = {k: v for k, v in inject.items() if k not in _INJECT_BULKY_KEYS}
    ordered.update({k: inject[k] for k in _INJECT_BULKY_KEYS if k in inject})
    head = inject_targets_line(inject)
    body = json_window(ordered, offset, limit)
    return f"{head}\n{body}" if head else body


# Transient upstream statuses that IDEMPOTENT OpenAEV calls opt into retrying at
# the HTTP layer (via ``_api(..., retry_transient=True)``). The shared gateway
# set is 502/503/504; OpenAEV also surfaces its own transient faults (a brief EE
# capability re-check, a DB deadlock, a restarting replica) as a bare 500, so
# that is added here. A quiet, backed-off HTTP-layer retry turns such a blip into
# one successful tool call instead of an error the autonomous orchestrator then
# re-issues many times in a row (the 6x scope-PUT retry storm the operator saw).
# Only ever opt in calls that are safe to repeat - reads, PUT scope, PUT step
# update, evaluate, and author (idempotent while a step is still pending).
_OPENAEV_TRANSIENT_RETRY_STATUSES = frozenset({500}) | TRANSIENT_RETRY_STATUSES

#: Callback injected by ``integration_tools`` that persists tool-generated
#: bytes as a real downloadable ``FileAttachment`` (``(filename, data,
#: content_type) -> FileRef | None``). Used by the reporting download action.
PersistFileFn = Callable[[str, bytes, str], Awaitable[Any]]

# Map (entity_type → UI path under base_url) for OpenAEV entities that have
# a known canonical UI route. Mirrors the constants in OpenAEV's
# ``openaev-front/src/constants/BaseUrls.ts``. Entities whose UI surface is
# query-only (attack_patterns, vulnerabilities, injector_contracts) are
# deliberately absent — no link is better than a wrong link, so the formatter
# emits no ``Link:`` line for those types.
#
# OpenAEV v3 reorganised the admin routes. Verified against the front router
# (``openaev-front/src/admin/Index.tsx``), NOT ``constants/BaseUrls.ts`` - that
# file still exports ``PAYLOAD_BASE_URL = '/admin/payloads'`` for which no
# route exists any more. The moves that invalidated our previous map:
#   ``/admin/assets/endpoints`` -> ``/admin/assets``
#   ``/admin/teams/teams``      -> ``/admin/teams``
#   ``/admin/teams/players``    -> ``/admin/persons``
#   ``/admin/payloads``         -> ``/admin/threat-arsenal`` (LIST ONLY - the
#       Threat Arsenal has no per-action detail route, so a payload deep link
#       cannot be built at all and ``payload`` is deliberately absent here.)
# Only entity types with a verified ``/<section>/:id`` detail route belong in
# this map; anything else must emit no link at all.
_AEV_UI_PATHS: dict[str, str] = {
    "asset": "/admin/assets",
    "asset_group": "/admin/asset_groups",
    "security_platform": "/admin/security_platforms",
    "team": "/admin/teams",
    "player": "/admin/persons",
    "organization": "/admin/organizations",
    "scenario": "/admin/scenarios",
    "exercise": "/admin/simulations",
    "atomic_testing": "/admin/atomic_testings",
    "finding": "/admin/findings",
}


def _aev_link(base_url: str, entity_type: str, entity_id: str | None) -> str:
    """Return a markdown ``Link:`` line for the entity, or ``""``.

    Returns an empty string when:
      * ``base_url`` is empty (never emit a bare path the chat UI would
        mis-resolve against its own origin);
      * ``entity_id`` is missing / ``"N/A"``;
      * ``entity_type`` is not in ``_AEV_UI_PATHS``.
    """
    if not base_url or not entity_id or entity_id == "N/A":
        return ""
    path = _AEV_UI_PATHS.get(entity_type)
    if not path:
        return ""
    return f"  Link: [Open in OpenAEV]({base_url}{path}/{entity_id})"

# ---------------------------------------------------------------------------
# Scenario / payload enums (matching OpenAEV API constants)
# ---------------------------------------------------------------------------

SCENARIO_CATEGORIES = [
    "global-crisis",
    "attack-scenario",
    "media-pressure",
    "data-exfiltration",
    "capture-the-flag",
    "vulnerability-exploitation",
    "lateral-movement",
    "url-filtering",
]

SCENARIO_MAIN_FOCUS = [
    "incident-response",
    "endpoint-protection",
    "web-filtering",
    "standard-operating-procedure",
    "crisis-communication",
    "strategic-reaction",
]

SCENARIO_SEVERITIES = ["low", "medium", "high", "critical"]

PAYLOAD_TYPES = ["Command", "Executable", "FileDrop", "DnsResolution", "NetworkTraffic"]

COMMAND_EXECUTORS = ["psh", "cmd", "bash", "sh"]

PLATFORMS = ["Linux", "Windows", "MacOS", "Container", "Service", "Generic", "Internal"]

# ---------------------------------------------------------------------------
# Output-parser enums (payload / threat-arsenal "output parsers")
#
# Mirrors ``ParserMode`` and ``ContractOutputType`` in openaev-model. The LLM
# routinely invents near-miss tokens here (``STD_OUT`` for ``STDOUT``, ``host``
# for a type that does not exist), so we validate/normalize the nested
# ``*_output_parsers`` before they reach OpenAEV: obvious aliases are corrected
# silently and unknown tokens fail locally with the exact allowed set, instead
# of round-tripping to a 400 (or, before hardening, a 500).
# ---------------------------------------------------------------------------

#: ``io.openaev.database.model.ParserMode``.
PARSER_MODES = ["STDOUT", "STDERR", "READ_FILE"]

#: ``io.openaev.database.model.ParserType`` - the API accepts ONLY REGEX.
PARSER_TYPES = ["REGEX"]

#: Safe spelling aliases the model tends to emit -> the canonical ParserMode.
_PARSER_MODE_ALIASES = {
    "STD_OUT": "STDOUT",
    "STDOUT": "STDOUT",
    "STD_ERR": "STDERR",
    "STDERR": "STDERR",
    "READFILE": "READ_FILE",
    "READ_FILE": "READ_FILE",
    "FILE": "READ_FILE",
}

#: ``io.openaev.database.model.ContractOutputType`` (the finding types a parser
#: can emit). These are the exact lowercase tokens the API accepts.
CONTRACT_OUTPUT_TYPES = [
    "text",
    "number",
    "port",
    "portscan",
    "ipv4",
    "ipv6",
    "credentials",
    "username",
    "admin_username",
    "sid",
    "asset",
    "computer",
    "share",
    "cve",
    "vulnerability",
    "group",
    "file",
    "delegation",
    "password_policy",
    "asreproastable_account",
    "kerberoastable_account",
    "account_with_password_not_required",
    "expectation_signature",
    "action_output",
]

#: Safe aliases for contract-output types -> the canonical token.
_CONTRACT_OUTPUT_TYPE_ALIASES = {
    "ip": "ipv4",
    "ipv_4": "ipv4",
    "ipv_6": "ipv6",
    "user": "username",
    "hostname": "computer",
}

#: The ``FilterGroup`` key OpenAEV uses to select injector contracts / threat
#: arsenal actions by the finding types they can PRODUCE. Mirrors
#: ``ProvidingFilterSpecificationBuilder.PROVIDING_FILTER_KEY`` (openaev-api): the
#: platform matches a contract when ANY requested output type is produced, via
#: the payload output parsers OR the native-injector content ``outputs`` array.
#: Filtering by producing is for REUSE. Payload-based contracts that produce
#: none are fork candidates and must still be returned alongside (merged after
#: the producing-filtered search). Native injectors without a payload are not
#: added as fork candidates - reuse their id; they cannot be duplicated.
PROVIDING_FILTER_KEY = "injector_contract_providing"


def _resolve_output_types(values: list[str]) -> tuple[list[str], str]:
    """Canonicalize + validate a list of contract output-type tokens.

    Returns ``(resolved, "")`` on success or ``([], error)`` naming the allowed
    set. This matters because OpenAEV resolves an UNKNOWN providing value to the
    empty set and returns zero rows with a 200 - a silent "no matches" that looks
    like "no such contract exists". Failing locally with the exact token list
    keeps a typo from being read as an absence.
    """
    resolved: list[str] = []
    for v in values:
        if not isinstance(v, str) or not v.strip():
            continue
        lowered = v.strip().lower()
        canonical = _CONTRACT_OUTPUT_TYPE_ALIASES.get(lowered, lowered)
        if canonical not in CONTRACT_OUTPUT_TYPES:
            return [], (
                "output_types must be drawn from: "
                f"{', '.join(CONTRACT_OUTPUT_TYPES)} (got '{v}')."
            )
        if canonical not in resolved:
            resolved.append(canonical)
    return resolved, ""


def _providing_filter_clause(output_types: list[str]) -> dict:
    """Build the ``providing`` filter selecting contracts producing ANY of the types.

    ``output_types`` is already resolved by ``_resolve_output_types``. Uses ``eq``
    (any-of) - the operator OpenAEV's generic query builder honours across BOTH
    contract shapes: a join on ``payload.outputParsers.contractOutputElements.type``
    for payload actions and an ILIKE over the native-injector ``content`` JSON.
    The caller merges the returned clause into a ``FilterGroup``.
    """
    return {"key": PROVIDING_FILTER_KEY, "operator": "eq", "values": output_types}


def _merge_filter_clause(filter_group: dict | None, clause: dict) -> tuple[dict, str]:
    """AND-compose ``clause`` with an existing ``FilterGroup`` (or wrap it).

    Returns ``(merged, "")`` on success or ``({}, error)``. OpenAEV filter
    groups are FLAT - ``Filters.Filter`` (openaev-model) has no nested group -
    so a narrowing clause cannot be AND-composed with an ``or`` group by
    nesting. Appending it to an ``or`` group would OR it in and silently
    BROADEN the results (the opposite of narrowing), so that case fails loudly
    instead. A missing mode defaults to ``and``, matching the platform.
    """
    if not filter_group:
        return {"mode": "and", "filters": [clause]}, ""
    mode = str(filter_group.get("mode") or "and").lower()
    if mode != "and":
        return {}, (
            f"The '{clause.get('key')}' clause narrows results, so it only "
            "composes with a filter_group whose mode is 'and'. OpenAEV filter "
            "groups cannot be nested, so appending it to your 'or' group would "
            "OR it in and broaden the results instead of narrowing them. "
            "Either drop output_types or restate the filter_group as an 'and' "
            "group."
        )
    merged = dict(filter_group)
    merged["filters"] = [*(filter_group.get("filters") or []), clause]
    return merged, ""


def _is_payload_based_contract(item: dict) -> bool:
    """True when the contract/action is backed by a duplicable payload.

    Injector contracts: ``injector_contract_payload`` dict with ``payload_id``,
    or ``injector_contract_payload_type`` set. Arsenal actions:
    ``action_payload`` dict with ``payload_id``. Native injectors (NetExec,
    Nmap, email, ...) have none of these and cannot be duplicated.
    """
    payload = item.get("injector_contract_payload")
    if not isinstance(payload, dict):
        payload = item.get("action_payload")
    if isinstance(payload, dict) and payload.get("payload_id"):
        return True
    payload_type = item.get("injector_contract_payload_type")
    return bool(isinstance(payload_type, str) and payload_type.strip())


def _without_providing_filter(filter_group: dict | None) -> dict | None:
    """Drop producing-filter clauses; return None if no filters remain.

    Used for the second (fork-candidate) search: same text/other filters,
    no ``injector_contract_providing`` clause.
    """
    if not isinstance(filter_group, dict):
        return None
    kept = [
        clause
        for clause in (filter_group.get("filters") or [])
        if not (isinstance(clause, dict) and clause.get("key") == PROVIDING_FILTER_KEY)
    ]
    if not kept:
        return None
    stripped = dict(filter_group)
    stripped["filters"] = kept
    return stripped


def _merge_fork_candidates(producing: list[dict], unfiltered: list[dict]) -> list[dict]:
    """Keep producing-filter hits first, then payload-based extras.

    Appends unfiltered items whose id (``injector_contract_id`` or
    ``action_id``) is not yet seen AND that are payload-based. Native
    injectors are not appended - they cannot be forked.
    """
    seen: set[str] = set()
    merged: list[dict] = []
    for item in producing:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("injector_contract_id") or item.get("action_id") or "")
        if item_id:
            seen.add(item_id)
        merged.append(item)
    for item in unfiltered:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("injector_contract_id") or item.get("action_id") or "")
        if item_id and item_id in seen:
            continue
        if not _is_payload_based_contract(item):
            continue
        if item_id:
            seen.add(item_id)
        merged.append(item)
    return merged


_KIND_PAYLOAD = "  Kind: payload-based (duplicable)"
_KIND_NATIVE = "  Kind: native injector (reuse, not duplicable)"
_PRODUCES_NONE_FORK = (
    "  Produces findings: none - FORK CANDIDATE. Duplicate this payload-based "
    "action and add REGEX parsers on the copy (output_parser_type=REGEX; "
    "finding type in contract_output_element_type). Do not skip it; do not "
    "invent a weaker original."
)
_PRODUCES_NONE_NATIVE = (
    "  Produces findings: none - native injector (not payload-based). Reuse "
    "this contract id as-is; it cannot be duplicated."
)

#: Output-element KEYS that name a real finding primitive. Pairing one of these
#: with a ``text`` / ``action_output`` type is the classic dead-branch bug: the
#: chaining engine decomposes ``text`` to the ``text`` primitive ONLY (and
#: ``action_output`` to ``action_output`` only), so a downstream trigger that
#: filters on e.g. ``username`` NEVER matches a ``text`` finding no matter what
#: its key is called - the whole branch silently never fires. We map the key ->
#: the typed output that actually carries that primitive so the error names the
#: fix. (Mirrors the ``ChainingTypeRegistry`` decomposition in openaev-model.)
_PRIMITIVE_KEYS_NEEDING_TYPED_OUTPUT = {
    "username": "username (or credentials)",
    "user": "username (or credentials)",
    "password": "credentials",
    "pass": "credentials",
    "pwd": "credentials",
    "credential": "credentials",
    "credentials": "credentials",
    "creds": "credentials",
    "hash": "credentials",
    "ntlm": "credentials",
    "port": "port (or portscan)",
    "ports": "portscan",
    "host": "computer (or portscan)",
    "hostname": "computer",
    "computer": "computer",
    "computername": "computer",
    "cve": "cve",
    "ipv4": "ipv4",
    "ipv6": "ipv6",
    "sid": "sid",
    "share": "share",
    "share_name": "share",
    "kerberoastable_account": "kerberoastable_account",
    "asreproastable_account": "asreproastable_account",
}


def _derive_element_labels(etype: str, index: int) -> tuple[str, str]:
    """Derive a human name + unique key for an output element from its type.

    The finding SEMANTICS live entirely in ``contract_output_element_type`` +
    ``contract_output_element_is_finding`` (that is what the chaining engine keys
    on); ``contract_output_element_name`` / ``contract_output_element_key`` are
    @NotBlank labels with no chaining meaning. The model kept omitting them,
    which turned every hand-built nested parser into an opaque finalize 400, so
    they are derived here instead of demanded from the caller.
    """
    base = (etype or "finding").replace("_", " ").strip() or "finding"
    name = base[:1].upper() + base[1:]
    key = etype if index == 0 else f"{etype}_{index + 1}"
    return name, key


def _fmt_group_index(value: object) -> str:
    """Coerce a capture-group index into OpenAEV's ``$n$`` index_values form.

    ``RegexGroup.indexValues`` (@NotBlank) is a ``$``-delimited list of capture
    group indexes (``$1$`` for group 1, ``$1$2$`` for groups 1 and 2). Accept a
    bare int (``1``), a bare numeric string (``"1"``), or an already-formatted
    ``"$1$"`` so the model can pass the natural ``{"username": 1}`` map.
    """
    # A bool is an int subclass but is never a valid capture-group index: True
    # would map to group 1 and False to the invalid group 0 ("$0$" - capture
    # groups are 1-indexed). Treat either as "unspecified" and fall back to the
    # group-1 default so a stray boolean can never emit an invalid index_values.
    if isinstance(value, bool):
        return "$1$"
    if isinstance(value, int):
        return f"${value}$"
    text = str(value or "").strip()
    if not text:
        return "$1$"
    if text.startswith("$") and text.endswith("$"):
        return text
    if text.isdigit():
        return f"${text}$"
    return text


def _lift_flat_parser_element(parser: dict) -> dict | None:
    """Lift a legacy FLAT parser into one nested element, else ``None``.

    The tool used to tell the model to put ``output_parser_regex`` and
    ``contract_output_element_type`` at the PARSER level. The normalizer only
    reads the nested ``output_parser_contract_output_elements[]`` list, so that
    flat shape was silently dropped (the parser stored an action with NO
    outputs) or shipped without a ``mode`` (finalize 400). Rather than keep
    failing, synthesise the nested element the model plainly meant: rule + type
    are lifted, capture groups are lifted from a flat ``fields`` map or a
    parser-level ``regex_groups`` list, and ``isFinding`` is forced true.
    ``name`` / ``key`` are derived downstream. Returns ``None`` when there is not
    even a rule + type to build from, so the caller can reject with guidance.
    """
    rule = parser.get("output_parser_regex") or parser.get(
        "contract_output_element_rule"
    )
    etype = parser.get("contract_output_element_type")
    if not (isinstance(rule, str) and rule.strip()):
        return None
    if not (isinstance(etype, str) and etype.strip()):
        return None
    element: dict = {
        "contract_output_element_type": etype.strip(),
        "contract_output_element_rule": rule.strip(),
        "contract_output_element_is_finding": True,
    }
    groups: list[dict] = []
    raw_fields = parser.get("fields")
    if isinstance(raw_fields, dict):
        for gfield, gindex in raw_fields.items():
            if str(gfield).strip():
                groups.append(
                    {
                        "regex_group_field": str(gfield).strip(),
                        "regex_group_index_values": _fmt_group_index(gindex),
                    }
                )
    raw_groups = parser.get("contract_output_element_regex_groups") or parser.get(
        "regex_groups"
    )
    if isinstance(raw_groups, list):
        for grp in raw_groups:
            if isinstance(grp, dict) and str(grp.get("regex_group_field") or "").strip():
                groups.append(
                    {
                        "regex_group_field": str(grp["regex_group_field"]).strip(),
                        "regex_group_index_values": _fmt_group_index(
                            grp.get("regex_group_index_values")
                            or grp.get("index")
                            or 1
                        ),
                    }
                )
    if groups:
        element["contract_output_element_regex_groups"] = groups
    return element


def _normalize_output_parsers(body: dict) -> str | None:
    """Normalize/validate the nested output-parser enums in ``body`` in place.

    Handles both the payload key (``payload_output_parsers``) and the
    threat-arsenal key (``action_output_parsers``). Corrects known spelling
    aliases (e.g. ``STD_OUT`` -> ``STDOUT``) and returns an error string naming
    the allowed values for any token that still cannot be resolved, so the
    caller can surface it to the model instead of shipping a bad request.
    Returns ``None`` when everything is valid.
    """
    for parsers_key in ("payload_output_parsers", "action_output_parsers"):
        parsers = body.get(parsers_key)
        if not isinstance(parsers, list):
            continue
        for parser in parsers:
            if not isinstance(parser, dict):
                continue
            ptype = parser.get("output_parser_type")
            if not isinstance(ptype, str) or not ptype.strip():
                parser["output_parser_type"] = "REGEX"
            else:
                stripped = ptype.strip()
                if stripped.upper() in PARSER_TYPES:
                    parser["output_parser_type"] = "REGEX"
                else:
                    return (
                        "output_parser_type must be REGEX (OpenAEV ParserType "
                        "has only that value). The finding type belongs in "
                        "contract_output_element_type (e.g. credentials, "
                        "username, cve), not in output_parser_type. Do not "
                        f"send this to OpenAEV (got '{ptype}')."
                    )
            # A parser with no nested element is the classic "no outputs" bug:
            # the model put the finding on the PARSER (flat output_parser_regex +
            # contract_output_element_type) and the normalizer used to silently
            # drop it. Lift that flat shape into a real nested element; if there
            # is not even a rule + type to build one, REJECT with the required
            # shape (and point at the easy `findings` param) instead of storing
            # an element-less parser that produces nothing.
            elements = parser.get("output_parser_contract_output_elements")
            if not isinstance(elements, list) or not elements:
                lifted = _lift_flat_parser_element(parser)
                if lifted is None:
                    return (
                        "output parser has no finding element: it carries neither "
                        "a nested output_parser_contract_output_elements[] entry "
                        "nor the flat output_parser_regex + "
                        "contract_output_element_type to build one, so it would "
                        "store an action with NO findings. Use the 'findings' "
                        "param instead - it builds the nested DTO for you: "
                        'findings=[{"type": "credentials", "regex": '
                        '"([^:]+):(.+)", "fields": {"username": 1, "password": '
                        '2}}]. Allowed finding types: '
                        f"{', '.join(CONTRACT_OUTPUT_TYPES)}."
                    )
                elements = [lifted]
                parser["output_parser_contract_output_elements"] = elements
                # The flat regex has been consumed into the element; drop it so
                # the parser body OpenAEV sees is clean (it ignores unknown keys,
                # but leaving a stale flat regex is confusing in logs).
                parser.pop("output_parser_regex", None)
            # Mode is @NotNull on OutputParserInput; a missing one is a guaranteed
            # finalize 400. Canonicalize a supplied mode; default a missing one to
            # STDOUT (by far the common case - a command emits its findings to
            # stdout) so the write is always well-formed.
            mode = parser.get("output_parser_mode")
            if isinstance(mode, str) and mode.strip():
                canonical = _PARSER_MODE_ALIASES.get(mode.strip().upper())
                if canonical is None:
                    return (
                        "output_parser_mode must be one of: "
                        f"{', '.join(PARSER_MODES)} (got '{mode}')."
                    )
                parser["output_parser_mode"] = canonical
            else:
                parser["output_parser_mode"] = "STDOUT"
            for elem_index, element in enumerate(elements):
                if not isinstance(element, dict):
                    return (
                        "each output_parser_contract_output_elements entry must be "
                        "an object with contract_output_element_type + "
                        "contract_output_element_rule. Prefer the 'findings' param."
                    )
                # Type first: name/key are DERIVED from it, and it is what the
                # chaining engine keys on. OpenAEV binds each element to
                # ContractOutputElementInput (type @NotNull, rule/name/key
                # @NotBlank); an LLM that omits one triggers an opaque 400 at
                # finalize which, on a fork, orphans the just-duplicated copy.
                etype = element.get("contract_output_element_type")
                if not isinstance(etype, str) or not etype.strip():
                    return (
                        "output parser element is missing "
                        "'contract_output_element_type' (the finding type it "
                        "produces, e.g. credentials, username, cve, ipv4, port). "
                        "Use the 'findings' param to avoid hand-building this: "
                        f"allowed types are {', '.join(CONTRACT_OUTPUT_TYPES)}."
                    )
                lowered = etype.strip().lower()
                canonical = _CONTRACT_OUTPUT_TYPE_ALIASES.get(lowered, lowered)
                if canonical not in CONTRACT_OUTPUT_TYPES:
                    return (
                        "contract_output_element_type must be one of: "
                        f"{', '.join(CONTRACT_OUTPUT_TYPES)} (got '{etype}')."
                    )
                element["contract_output_element_type"] = canonical
                # isFinding is a PRIMITIVE boolean on the OpenAEV DTO: omit it and
                # it silently persists false, so the element stores but produces no
                # finding (the copy-with-no-findings bug). Default a missing/None
                # flag to true; REJECT any non-boolean (0/1, "true", ...) - such a
                # value would pass straight through to OpenAEV's finalize
                # bean-validation AND slip past _action_findings_count (which
                # requires `is True`), so the fork would verify as finding-less. A
                # real False is preserved: a legitimate non-finding element the
                # dead-branch guard below keys on.
                is_finding = element.get("contract_output_element_is_finding")
                if is_finding is None:
                    element["contract_output_element_is_finding"] = True
                elif not isinstance(is_finding, bool):
                    return (
                        "contract_output_element_is_finding must be a boolean "
                        "(true or false); it defaults to true when omitted. Prefer "
                        "the 'findings' param, which always sets it correctly."
                    )
                # name / key are @NotBlank labels with no chaining meaning; derive
                # them from the type when absent instead of rejecting (the model
                # kept omitting them and 400-ing the finalize).
                dname, dkey = _derive_element_labels(canonical, elem_index)
                if not str(element.get("contract_output_element_name") or "").strip():
                    element["contract_output_element_name"] = dname
                if not str(element.get("contract_output_element_key") or "").strip():
                    element["contract_output_element_key"] = dkey
                # rule (the capture regex) cannot be derived - it is the one field
                # only the caller knows.
                rule = element.get("contract_output_element_rule")
                if not isinstance(rule, str) or not rule.strip():
                    return (
                        "output parser element is missing "
                        "'contract_output_element_rule' (the capture regex that "
                        "extracts the value). Use the 'findings' param - its "
                        "'regex' field maps to this."
                    )
                # Mirror OpenAEV's StringUtils.isValidRegex on the capture regex
                # so a bad pattern fails HERE with an actionable reason instead of
                # as an opaque bean-validation 400 at finalize (which, on a fork,
                # orphaned the just-duplicated copy). OpenAEV rejects, in this
                # order: length > 100, nested quantifiers like ``(a+)+`` (its
                # ReDoS guard), and anything that will not compile.
                if len(rule) > 100:
                    return (
                        "contract_output_element_rule regex is too long "
                        f"({len(rule)} chars); OpenAEV rejects any regex over "
                        "100 characters. Anchor on a short, specific fragment "
                        "around the value you capture instead of matching the "
                        "whole line."
                    )
                if re.search(r"\([^(]*[+*][^)]*\)[+*]", rule):
                    return (
                        "contract_output_element_rule regex contains nested "
                        "quantifiers (e.g. '(a+)+'), which OpenAEV rejects for "
                        "ReDoS-safety. Rewrite it so no '+'/'*' is applied to a "
                        "group that already contains a '+'/'*'."
                    )
                try:
                    re.compile(rule)
                except re.error as exc:
                    return (
                        "contract_output_element_rule is not a valid regex "
                        f"({exc}). Fix the capture pattern so it compiles."
                    )
                # Regex groups are OPTIONAL (regexGroups defaults to an empty set),
                # but any supplied group needs a non-blank field + index_values.
                # Coerce a bare/int index (1) into OpenAEV's $1$ form so the model
                # can pass the natural shape.
                groups = element.get("contract_output_element_regex_groups")
                if isinstance(groups, list):
                    for grp in groups:
                        if not isinstance(grp, dict):
                            continue
                        gfield = grp.get("regex_group_field")
                        if not isinstance(gfield, str) or not gfield.strip():
                            return (
                                "output parser regex group is missing the required "
                                "'regex_group_field' (the finding primitive it "
                                "fills, e.g. username). Use the 'findings' param's "
                                "'fields' map to avoid hand-building groups."
                            )
                        grp["regex_group_field"] = gfield.strip()
                        gidx = grp.get("regex_group_index_values")
                        if gidx is None or (
                            isinstance(gidx, str) and not gidx.strip()
                        ):
                            return (
                                "output parser regex group is missing the required "
                                "'regex_group_index_values' (the capture-group "
                                "index, e.g. $1$). Use the 'findings' param's "
                                "'fields' map (e.g. {\"username\": 1})."
                            )
                        grp["regex_group_index_values"] = _fmt_group_index(gidx)

                # Dead-branch guard: a finding element typed text/action_output but
                # keyed as a real primitive (username, password, port, ...) never
                # chains, because text -> text primitive only. Catch it here with the
                # fix, rather than shipping a path that silently never fires. We look
                # at the element key/name AND at the regex capture-group field names
                # (contract_output_element_regex_groups[].regex_group_field): a parser
                # typed 'text' whose groups are named 'username'/'password' is just as
                # obviously a credentials finding downgraded to text.
                if canonical in ("text", "action_output") and (
                    element.get("contract_output_element_is_finding") is not False
                ):
                    key = (
                        element.get("contract_output_element_key")
                        or element.get("contract_output_element_name")
                        or ""
                    )
                    candidate_names = [str(key)]
                    groups = element.get("contract_output_element_regex_groups")
                    if isinstance(groups, list):
                        for grp in groups:
                            if isinstance(grp, dict) and grp.get("regex_group_field"):
                                candidate_names.append(str(grp["regex_group_field"]))
                    suggested = None
                    key_norm = ""
                    for candidate in candidate_names:
                        norm = candidate.strip().lower().replace(" ", "_")
                        hit = _PRIMITIVE_KEYS_NEEDING_TYPED_OUTPUT.get(norm)
                        if hit:
                            key_norm = norm
                            suggested = hit
                            break
                    if suggested:
                        where = (
                            f"keyed '{key}'"
                            if str(key).strip().lower().replace(" ", "_") == key_norm
                            else f"whose regex group '{key_norm}'"
                        )
                        return (
                            f"output parser element {where} is typed "
                            f"'{canonical}', which the chaining engine turns into a "
                            f"'{canonical}' finding primitive ONLY - a downstream trigger "
                            f"that filters on '{key_norm}' will never match it, so the "
                            "attack path dead-ends there. Set "
                            f"contract_output_element_type to {suggested} so the finding "
                            f"decomposes into the '{key_norm}' primitive the chain waits "
                            "on. Use 'text' only for values nothing downstream chains on."
                        )
    return None


def _expand_findings(findings: object) -> tuple[list[dict], str]:
    """Expand the first-class flat ``findings`` param into nested output parsers.

    The model repeatedly failed to hand-build OpenAEV's nested
    ``action_output_parsers`` DTO, landing forks with no outputs or a finalize
    400. This turns a dead-simple flat shape -

        [{"type": "credentials", "regex": "([^:]+):(.+)",
          "fields": {"username": 1, "password": 2}, "mode": "STDOUT"}]

    into the exact ``OutputParserInput`` / ``ContractOutputElementInput`` /
    ``RegexGroupInput`` structure OpenAEV expects, ALWAYS stamping
    ``contract_output_element_is_finding=true`` (a primitive boolean that
    silently persists ``false`` when omitted - the copy-with-no-findings bug).
    Findings are grouped into one parser per capture mode. Returns
    ``(parsers, "")`` or ``([], error)`` naming the missing field; the result is
    still run through :func:`_normalize_output_parsers` by the caller, so type /
    regex / mode validation stays in one place.
    """
    if not isinstance(findings, list) or not findings:
        return [], (
            "findings must be a non-empty list of objects, each with a finding "
            "'type' (e.g. credentials, username, cve, ipv4, port) and a 'regex' "
            "capture pattern, optionally 'fields' (a {primitive: capture-group-"
            "index} map) and 'mode' (STDOUT/STDERR/READ_FILE, default STDOUT)."
        )
    by_mode: dict[str, list[dict]] = {}
    mode_order: list[str] = []
    elem_index = 0
    for idx, finding in enumerate(findings):
        if not isinstance(finding, dict):
            return [], f"findings[{idx}] must be an object with 'type' and 'regex'."
        ftype = finding.get("type") or finding.get("contract_output_element_type")
        regex = finding.get("regex") or finding.get("rule")
        if not isinstance(ftype, str) or not ftype.strip():
            return [], (
                f"findings[{idx}] is missing 'type' - the finding type it "
                "produces (credentials, username, cve, ipv4, port, ...)."
            )
        if not isinstance(regex, str) or not regex.strip():
            return [], (
                f"findings[{idx}] is missing 'regex' - the capture pattern that "
                "extracts the value from the action output."
            )
        mode = str(finding.get("mode") or "STDOUT")
        element: dict = {
            "contract_output_element_type": ftype.strip(),
            "contract_output_element_rule": regex.strip(),
            "contract_output_element_is_finding": True,
        }
        name = finding.get("name")
        key = finding.get("key")
        if isinstance(name, str) and name.strip():
            element["contract_output_element_name"] = name.strip()
        if isinstance(key, str) and key.strip():
            element["contract_output_element_key"] = key.strip()
        # Derive the @NotBlank name/key labels from the type when the caller
        # omits them so the element is always complete - the model kept leaving
        # them out and 400-ing the finalize.
        dname, dkey = _derive_element_labels(ftype.strip(), elem_index)
        element.setdefault("contract_output_element_name", dname)
        element.setdefault("contract_output_element_key", dkey)
        groups: list[dict] = []
        fields = finding.get("fields")
        if isinstance(fields, dict):
            for gfield, gindex in fields.items():
                if not str(gfield).strip():
                    continue
                groups.append(
                    {
                        "regex_group_field": str(gfield).strip(),
                        "regex_group_index_values": _fmt_group_index(gindex),
                    }
                )
        elif isinstance(fields, list):
            for grp in fields:
                if isinstance(grp, dict) and str(grp.get("regex_group_field") or "").strip():
                    groups.append(
                        {
                            "regex_group_field": str(grp["regex_group_field"]).strip(),
                            "regex_group_index_values": _fmt_group_index(
                                grp.get("regex_group_index_values")
                                or grp.get("index")
                                or 1
                            ),
                        }
                    )
        if groups:
            element["contract_output_element_regex_groups"] = groups
        if mode not in by_mode:
            by_mode[mode] = []
            mode_order.append(mode)
        by_mode[mode].append(element)
        elem_index += 1
    parsers = [
        {
            "output_parser_type": "REGEX",
            "output_parser_mode": mode,
            "output_parser_contract_output_elements": by_mode[mode],
        }
        for mode in mode_order
    ]
    return parsers, ""


# ---------------------------------------------------------------------------
# Reporting module enums (OpenAEV v3 ``/api/reportings``)
#
# Mirrors ``openaev-model`` ``ReportingContextType`` / ``ReportingFormat`` /
# ``ReportingTimeRange`` / ``ReportingModuleType`` / ``ReportingSchedulePeriod``.
# The legacy ``/api/reports`` surface (``report_*`` fields) was DELETED in v3;
# this module replaces it.
# ---------------------------------------------------------------------------

REPORTING_CONTEXT_TYPES = [
    "PLATFORM",
    "SIMULATION",
    "SCENARIO",
    "ATOMIC_TESTING",
    "ENDPOINT",
    "ASSET_GROUP",
    "PLAYER",
    "TEAM",
]

REPORTING_FORMATS = ["PDF", "HTML"]

REPORTING_TIME_RANGES = [
    "LAST_7_DAYS",
    "LAST_30_DAYS",
    "LAST_90_DAYS",
    "LAST_180_DAYS",
    "LAST_365_DAYS",
    "ALL_TIME",
]

REPORTING_MODULE_TYPES = [
    "COVER",
    "EXECUTIVE_SUMMARY",
    "SUBJECT_DETAILS",
    "MITRE_COVERAGE",
    "RESULTS_BREAKDOWN",
    "SECURITY_DOMAINS",
    "SCORE_TRENDS",
    "FAILED_EXPECTATIONS",
    "FINDINGS",
    "ATTACK_PATHS",
    "CUSTOM_MARKDOWN",
]

REPORTING_SCHEDULE_PERIODS = ["HOUR", "DAY", "WEEK", "MONTH"]

REPORTING_GENERATION_STATUSES = ["PENDING", "RUNNING", "SUCCESS", "ERROR"]

# ---------------------------------------------------------------------------
# Expectations
#
# An inject is the attack; an EXPECTATION is the verdict on the defense.
# ``inject_status`` says whether the attack ran, ``inject_expectation_*`` says
# whether the security control worked. They are computed by disjoint code paths
# and can disagree in every combination - a fully EXECUTED simulation whose
# detection expectations all FAILED is a security failure, not a success.
#
# Mirrors ``BaseInjectExpectation.EXPECTATION_TYPE`` / ``EXPECTATION_STATUS``
# (openaev-model) and ``ExpectationType`` (openaev-api), which is where the
# per-type labels below come from.
# ---------------------------------------------------------------------------

#: The six stored expectation types (the discriminator on ``injects_expectations``).
EXPECTATION_TYPES = [
    "PREVENTION",
    "DETECTION",
    "VULNERABILITY",
    "MANUAL",
    "ARTICLE",
    "CHALLENGE",
]

#: The technical expectation types that can be scoped to specific security
#: platform types (``SecurityPlatform.SECURITY_PLATFORM_TYPE``). MANUAL /
#: ARTICLE / CHALLENGE are fulfilled by people, not products, so they never
#: carry a platform scope.
TECHNICAL_EXPECTATION_TYPES = ["PREVENTION", "DETECTION", "VULNERABILITY"]

#: ``SecurityPlatform.SECURITY_PLATFORM_TYPE`` - the product families an
#: expectation can be scoped to. Empty scope for a given expectation means
#: "any security platform" (the legacy default).
SECURITY_PLATFORM_TYPES = [
    "EDR",
    "XDR",
    "SIEM",
    "SOAR",
    "NDR",
    "ISPM",
    "LLM_FIREWALL",
    "AI_GATEWAY",
    "VULNERABILITY_SCANNER",
]

#: The four REPORTING families results aggregate into. MANUAL / ARTICLE /
#: CHALLENGE all collapse into HUMAN_RESPONSE, so a results payload never
#: contains those three names.
EXPECTATION_RESULT_TYPES = [
    "PREVENTION",
    "DETECTION",
    "VULNERABILITY",
    "HUMAN_RESPONSE",
]

#: ``EXPECTATION_STATUS``. PARTIAL never appears on an individual expectation
#: row (``computeStatus`` can only return UNKNOWN / PENDING / SUCCESS / FAILED);
#: it exists only in aggregates.
EXPECTATION_STATUSES = ["SUCCESS", "PARTIAL", "PENDING", "FAILED", "UNKNOWN"]

#: (result type -> status -> the wording OpenAEV's own UI uses). Reusing the
#: platform's vocabulary keeps agent answers consistent with what the user sees
#: on screen, and makes VULNERABILITY's inverted polarity impossible to miss.
_EXPECTATION_LABELS: dict[str, dict[str, str]] = {
    "PREVENTION": {
        "SUCCESS": "Prevented",
        "FAILED": "Not Prevented",
        "PARTIAL": "Partially Prevented",
        "PENDING": "Pending",
        "UNKNOWN": "Unknown",
    },
    "DETECTION": {
        "SUCCESS": "Detected",
        "FAILED": "Not Detected",
        "PARTIAL": "Partially Detected",
        "PENDING": "Pending",
        "UNKNOWN": "Unknown",
    },
    "VULNERABILITY": {
        "SUCCESS": "Not vulnerable",
        "FAILED": "Vulnerable",
        "PARTIAL": "Partially vulnerable",
        "PENDING": "Pending",
        "UNKNOWN": "Unknown",
    },
    "HUMAN_RESPONSE": {
        "SUCCESS": "Successful",
        "FAILED": "Failed",
        "PARTIAL": "Partial",
        "PENDING": "Pending",
        "UNKNOWN": "Unknown",
    },
}

#: What SUCCESS actually proves, per family. Emitted alongside results so the
#: agent cannot flatten "detected" into "blocked", or read a vulnerability
#: SUCCESS as "the scanner found something".
_EXPECTATION_MEANING: dict[str, str] = {
    "PREVENTION": (
        "SUCCESS means the attack was BLOCKED by a security control; FAILED "
        "means it was allowed to run"
    ),
    "DETECTION": (
        "SUCCESS means the attack was SEEN (an alert was raised) - it still "
        "executed; FAILED means it ran unnoticed"
    ),
    "VULNERABILITY": (
        "INVERTED polarity: SUCCESS means the target is NOT vulnerable, FAILED "
        "means a scanner PROVED it vulnerable (the finding you care about)"
    ),
    "HUMAN_RESPONSE": (
        "SUCCESS means a human (or team) reacted as the playbook expects; it is "
        "validated manually, not by a security product"
    ),
}

#: The literal result text the expiration manager writes on a per-source row
#: that nobody ever answered.
EXPECTATION_EXPIRED_RESULT = "Expired"


def expectation_result_type(expectation_type: str) -> str:
    """Map a stored expectation type onto its reporting family."""
    value = (expectation_type or "").strip().upper()
    if value in ("MANUAL", "ARTICLE", "CHALLENGE"):
        return "HUMAN_RESPONSE"
    return value


def expectation_label(expectation_type: str, status: str) -> str:
    """Human label for a (type, status) pair, e.g. DETECTION+FAILED -> 'Not Detected'."""
    family = expectation_result_type(expectation_type)
    status_key = (status or "").strip().upper()
    return _EXPECTATION_LABELS.get(family, {}).get(status_key, status_key or "Unknown")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: The proactive-refresh machinery for the short-lived cross-platform JWT lives
#: in ``app.integrations.http`` (:class:`RefreshableHeaders`,
#: :func:`ensure_fresh_token`, :func:`bearer_seconds_remaining`) so the OpenCTI
#: provider gets the same behaviour from the same code. It is imported under the
#: historical underscore names above and re-exported below.


class OpenAEVHeaders(_RefreshableHeaders):
    """Request headers that also carry the tool set's tenant resolver.

    Set once in ``build_tools``; ``None`` (a bare collector) means unscoped.
    """

    tenant_scope: PlatformCapabilities | None = None
    #: The caller's read rights (``read_scope.ReadScope``), read by the count and list reads.
    read_scope: Any = None


def _headers(api_token: str, refresh_fn: RefreshFn | None = None) -> OpenAEVHeaders:
    return OpenAEVHeaders(
        {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        refresh_fn=refresh_fn,
    )


def _search_body(
    page: int = 0,
    size: int = 20,
    filter_group: dict | None = None,
    text_search: str = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {"page": page, "size": size}
    if filter_group:
        body["filterGroup"] = filter_group
    if text_search:
        body["textSearch"] = text_search
    return body


#: OpenAEV ``Filters.FilterOperator`` / ``Filters.FilterMode`` (openaev-model).
#: As of v3 the platform NO LONGER rejects an unknown operator or mode: both
#: enums gained a tolerant ``@JsonCreator`` that silently coerces anything
#: unrecognised to ``eq`` / ``and`` and only warn-logs server-side. A
#: typo therefore returns the WRONG ROWS with HTTP 200 instead of a 400, so the
#: validation has to happen here, before the request leaves.
FILTER_OPERATORS = [
    "eq",
    "not_eq",
    "contains",
    "not_contains",
    "starts_with",
    "not_starts_with",
    "gt",
    "gte",
    "lt",
    "lte",
    "empty",
    "not_empty",
]

FILTER_MODES = ["and", "or"]


def _validate_filter_group(filter_group: Any) -> str:
    """Validate a caller-supplied ``FilterGroup``. Returns ``""`` when valid.

    Guards against OpenAEV's silent operator/mode coercion: an unknown operator
    would otherwise degrade to ``eq`` and quietly change the meaning of the
    query. Nested groups (``filters[].filterGroup``) are validated recursively.
    """
    if filter_group is None:
        return ""
    if not isinstance(filter_group, dict):
        return "filterGroup must be a JSON object with 'mode' and 'filters'."
    mode = filter_group.get("mode")
    if mode is not None and str(mode).lower() not in FILTER_MODES:
        return (
            f"Unknown filter mode '{mode}'. OpenAEV silently treats an "
            f"unrecognised mode as 'and', which would change the meaning of the "
            f"query. Use one of: {', '.join(FILTER_MODES)}."
        )
    filters = filter_group.get("filters")
    if filters is None:
        return ""
    if not isinstance(filters, list):
        return "filterGroup.filters must be a list of filter objects."
    for idx, f in enumerate(filters):
        if not isinstance(f, dict):
            return f"filterGroup.filters[{idx}] must be a JSON object."
        operator = f.get("operator")
        if operator is not None and str(operator).lower() not in FILTER_OPERATORS:
            return (
                f"Unknown filter operator '{operator}' at filters[{idx}]. "
                f"OpenAEV silently treats an unrecognised operator as 'eq', so "
                f"the query would return the wrong rows with a 200. Use one of: "
                f"{', '.join(FILTER_OPERATORS)}."
            )
        if not f.get("key"):
            return (
                f"filterGroup.filters[{idx}] is missing 'key'. Filter keys are "
                "the entity's own attribute names (e.g. asset_name, "
                "exercise_status, finding_type) - take them from a search result "
                "or from the entity's filterable-attribute list rather than "
                "guessing."
            )
        nested = f.get("filterGroup")
        if nested is not None:
            nested_err = _validate_filter_group(nested)
            if nested_err:
                return nested_err
    return ""


def _contains_filter(key: str, value: str) -> dict:
    return {
        "mode": "or",
        "filters": [{"key": key, "operator": "contains", "values": [value]}],
    }


def _multi_contains_filter(keys: list[str], value: str) -> dict:
    return {
        "mode": "or",
        "filters": [
            {"key": k, "operator": "contains", "values": [value]} for k in keys
        ],
    }


def _require_response(resp: httpx.Response | None) -> httpx.Response:
    """Return a non-null response or raise on an unexpected integration state."""
    if resp is None:
        raise RuntimeError(
            "OpenAEV request unexpectedly returned no response after a successful safe_request call."
        )
    return resp


def _tenant_path(path: str, tenant_id: str) -> str:
    """Rewrite ``/api/<rest>`` to ``/api/tenants/{tenant_id}/<rest>``.

    OpenAEV multi-tenancy is GA in v3: nearly every controller maps both the
    legacy path (which operates on the caller's default tenant) and a
    tenant-scoped twin (``TenantUriUtils.TENANT_PREFIX``). A handful of routes -
    the Threat Arsenal delete / export / security-platforms endpoints and the
    whole chaining surface - exist ONLY in the tenant-prefixed form, so a
    request without the prefix 404s.

    Returns *path* unchanged when there is no tenant id or the path is already
    tenant-scoped.
    """
    if not tenant_id or not path.startswith("/api/"):
        return path
    if path.startswith("/api/tenants/"):
        return path
    return f"/api/tenants/{quote(tenant_id, safe='')}{path[len('/api'):]}"


# Stay on the base path: no tenant twin (settings, which is also how the tenant
# is found; platform-*; schemas and engine/schemas; me/*, only GET /me has one), or the run decides
# its tenant (autonomous-runs, @RunTenantScope: the prefixed path would 404 others).
_UNSCOPED_PREFIXES = (
    "/api/me",
    "/api/settings",
    "/api/platform-users",
    "/api/platform-roles",
    "/api/platform-groups",
    "/api/schemas",
    "/api/engine/schemas",
    "/api/autonomous-runs",
)


def _is_unscoped(path: str) -> bool:
    return any(
        path == p or path.startswith((f"{p}/", f"{p}?")) for p in _UNSCOPED_PREFIXES
    )


#: Errors the tenant choice can cause; the tenant hints are only relevant to these.
_TENANT_SCOPE_ERRORS = ("TENANT_WRITE_SCOPE", "TENANT_SELECTOR_REQUIRED", "TENANT_ACCESS_DENIED")


def _lookup_hint(cause: str) -> str:
    # The cause may carry multi-line 404 / anti-spin prose: keep the first sentence.
    cause = re.split(r"(?<=[.!?])\s", cause.strip(), maxsplit=1)[0][:160]
    return (
        f" Note: the OpenAEV tenant for this request could not be resolved "
        f"({cause}); on a multi-tenant OpenAEV this request needs it."
    )


_CHOSEN_TENANT_LEAD = " Note: this request went to the OpenAEV tenant "


def _chosen_tenant_hint(scope: TenantScope) -> str:
    """Name the tenant a request went to, for a ``TENANT_ACCESS_DENIED`` answer."""
    if not scope.tenant_id:
        return ""
    if scope.configured:
        origin = "set in the integration's Tenant field"
    elif scope.memberships_unknown:
        origin = (
            "the default tenant, as the account's tenants could not be read; "
            "set the integration's Tenant field to use another one"
        )
    else:
        origin = "chosen automatically; set the integration's Tenant field to use another one"
    return (
        f"{_CHOSEN_TENANT_LEAD}{scope.tenant_id} ({origin}); "
        "the integration's account must belong to it."
    )


def _with_scope_hint(err: str | None, hint: str) -> str | None:
    """Append *hint* to *err* when *err* is a tenant-scope rejection or a not-found.

    A not-found also names the tenant the request went to and how it was chosen
    (#1792): two runs of one agent on two tenants (a delegated child on another
    account than its parent) read "not found" as "absent" otherwise.
    """
    if not (err and hint):
        return err
    if any(marker in err for marker in _TENANT_SCOPE_ERRORS):
        return f"{err}{hint}"
    # Only the tenant the request went to; a failed lookup is no cause of a 404.
    if hint.startswith(_CHOSEN_TENANT_LEAD) and is_not_found_error(err):
        return f"{err}{hint}"
    return err


async def _scope_path(headers: dict[str, str], path: str) -> tuple[str, str]:
    """Route *path* to the chosen tenant's twin; returns ``(path, hint)``.

    The hint explains a failed lookup (the path stays unscoped) or names the
    tenant the path went to; ``_with_scope_hint`` adds it to tenant errors and not-founds.
    Headers without a resolver (read-only collectors such as
    ``ctem_metrics_collector``) are unscoped.
    """
    resolver = getattr(headers, "tenant_scope", None)
    if (
        resolver is None
        or not path.startswith("/api/")
        or path.startswith("/api/tenants/")
        or _is_unscoped(path)
    ):
        return path, ""
    scope = await resolver.tenant_scope()
    if scope.error:
        return path, _lookup_hint(scope.error)
    return _tenant_path(path, scope.tenant_id), _chosen_tenant_hint(scope)


async def _api(
    base_url: str,
    headers: dict[str, str],
    method: str,
    path: str,
    action: str,
    *,
    json: Any = None,
    params: dict | None = None,
    client: httpx.AsyncClient | None = None,
    tenant_id: str = "",
    retry_transient: bool = False,
    scoped: bool = True,
) -> tuple[httpx.Response | None, str | None]:
    """Execute a single OpenAEV REST call.

    Returns ``(response, None)`` on success or ``(None, error_string)`` on
    failure. ``path`` is appended to ``base_url`` (include the leading
    ``/api/...``). Shared by every domain module so request handling stays
    consistent with the original provider. Pass *client* to reuse a single
    ``AsyncClient`` across a batch of calls (the caller owns its lifecycle);
    otherwise a short-lived client is created for this one call. Pass
    *tenant_id* to target a tenant-scoped route (see ``_tenant_path``). Without
    one the path is scoped to the default tenant; *scoped*=False opts out and is
    for the settings probe ONLY (it runs inside the lookup it would wait on).

    Set *retry_transient* on an IDEMPOTENT call to retry a transient upstream
    5xx (:data:`_OPENAEV_TRANSIENT_RETRY_STATUSES`) with backed-off jitter at the
    HTTP layer, so a brief OpenAEV blip resolves inside this one call instead of
    surfacing an error the caller (the autonomous orchestrator) then re-issues
    many times. Never set it on a non-idempotent mutation (a double-apply risk).
    A 5xx that still fails after the retry gets an anti-spin hint appended so the
    model does not immediately re-fire the same call.
    """
    scope_hint = ""
    if not tenant_id and scoped:
        # An explicit tenant_id (Threat Arsenal, chaining) wins; otherwise scope
        # to the tool set's default tenant (spec 026).
        path, scope_hint = await _scope_path(headers, path)
    path = _tenant_path(path, tenant_id)
    # A ``_RefreshableHeaders`` carries a token re-mint callback (the
    # platform-managed JWT re-minter, WS2). When present and the caller did not
    # inject its own client, route this call through an ``AutoRefreshClient`` so
    # a mid-run 401/403 re-mints the JWT and retries once - mutating the shared
    # headers dict so every later call on this tool-set recovers too. Otherwise
    # keep the original short-lived plain client (static-token integrations).
    refresh_fn = getattr(headers, "refresh_fn", None)
    # Proactively re-mint the JWT if it is about to expire, BEFORE sending, so a
    # long run's callbacks never send an expired token and never trigger the
    # per-gather-call 401/403 burst. Coalesced across concurrent calls; safe for
    # both the AutoRefreshClient and the injected-client (batch) paths since it
    # only updates the shared headers dict every call reads from. No-op for a
    # static token (no refresh_fn, or a non-JWT bearer with no readable expiry).
    if refresh_fn is not None:
        await _ensure_fresh_token(headers, refresh_fn)
    async with AsyncExitStack() as stack:
        # Reuse the injected client (caller owns its lifecycle) or open a
        # short-lived one that the stack closes when this call returns.
        c: httpx.AsyncClient | AutoRefreshClient
        if client is not None:
            c = client
        elif refresh_fn is not None:
            c = await stack.enter_async_context(
                AutoRefreshClient(headers, refresh_fn, timeout=30)
            )
        else:
            c = await stack.enter_async_context(egress_client(timeout=30))
        resp, err = await safe_request(
            c,
            method,
            f"{base_url}{path}",
            _PROVIDER,
            action,
            headers=headers,
            json=json,
            params=params,
            retry_statuses=(
                _OPENAEV_TRANSIENT_RETRY_STATUSES if retry_transient else None
            ),
        )
    if err:
        err = _annotate_cross_tenant_404(_annotate_not_found(err), path)
        if retry_transient:
            err = _degrade_after_transient_retry(err)
        return None, _with_scope_hint(err, scope_hint)
    return _require_response(resp), None


#: The finding statuses that close a finding at source (OpenAEV #9233, CTEM-14). ``OPEN`` is the
#: only other status; a finding is never inferred closed from its absence in a search.
FINDING_CLOSED_STATUSES: frozenset[str] = frozenset({"NOT_REPRODUCED", "RESOLVED"})
#: What an OpenAEV delete answers about the findings of what it deleted (OpenAEV #9233: a finding
#: survives its inject, scenario, simulation or atomic test; its occurrence loses the link).
FINDINGS_KEPT_AS_HISTORY = "Its findings are kept in OpenAEV as history."


@dataclass(frozen=True)
class Finding:
    """One OpenAEV finding as XTM One reads it (OpenAEV #9233: one per type, value and location).

    The single parser of a finding: every reader (CTEM cases, CTEM metrics, the tool
    answers) goes through :func:`parse_finding`, so a change of the OpenAEV finding
    payload is a change of this function and its contract test
    (``tests/test_openaev_finding_contract.py``).
    """

    id: str
    type: str
    value: str
    #: ``OPEN`` | ``NOT_REPRODUCED`` | ``RESOLVED``, upper case; ``None`` when the answer carries none
    #: (``POST /api/findings/search`` does not return it; ``GET /api/findings/{id}`` does).
    status: str | None
    #: The location: ``(asset id, asset name)`` pairs; empty for a user or unknown location.
    assets: tuple[tuple[str, str], ...]
    #: The inject of the latest occurrence.
    inject_id: str | None
    created_at: str | None
    updated_at: str | None
    last_tested: str | None
    retest_inject_id: str | None

    @property
    def closed(self) -> bool:
        """True only when OpenAEV says the finding is closed (re-test status)."""
        return self.status in FINDING_CLOSED_STATUSES

    @property
    def asset_ids(self) -> tuple[str, ...]:
        return tuple(i for i, _ in self.assets if i)

    @property
    def asset_labels(self) -> tuple[str, ...]:
        """Each asset's name, else its id."""
        return tuple(dict.fromkeys(n or i for i, n in self.assets if n or i))


def _finding_assets(raw: Any) -> tuple[tuple[str, str], ...]:
    """``finding_assets`` as objects (search) or as id strings (``GET /api/findings/{id}``)."""
    out: list[tuple[str, str]] = []
    for a in raw if isinstance(raw, list) else []:
        if isinstance(a, dict):
            pair = (str(a.get("asset_id") or ""), str(a.get("asset_name") or ""))
        elif isinstance(a, str):
            pair = (a, "")
        else:
            continue
        if (pair[0] or pair[1]) and pair not in out:
            out.append(pair)
    return tuple(out)


def parse_finding(raw: Any) -> Finding | None:
    """Parse one OpenAEV finding (search row or ``GET /api/findings/{id}``); ``None`` without an id."""
    if not isinstance(raw, dict):
        return None
    fid = str(raw.get("finding_id") or "").strip()
    if not fid:
        return None
    inject = raw.get("finding_inject")
    inject_id = (inject.get("inject_id") if isinstance(inject, dict) else None) or raw.get("finding_inject_id")
    status = str(raw.get("finding_status") or "").strip().upper() or None

    def _text(key: str) -> str | None:
        value = raw.get(key)
        return str(value) if value else None

    return Finding(
        id=fid,
        type=str(raw.get("finding_type") or "").strip().lower(),
        value=str(raw.get("finding_value") or "").strip(),
        status=status,
        assets=_finding_assets(raw.get("finding_assets")),
        inject_id=str(inject_id) if inject_id else None,
        created_at=_text("finding_created_at"),
        updated_at=_text("finding_updated_at"),
        last_tested=_text("finding_last_tested"),
        retest_inject_id=_text("finding_retest_inject_id"),
    )


async def scoped_safe_request(
    client: httpx.AsyncClient | AutoRefreshClient,
    method: str,
    url: str,
    provider: str,
    action: str,
    **kwargs: Any,
) -> tuple[httpx.Response | None, str | None]:
    """``safe_request`` with *url* first routed to the default tenant.

    Same signature, so ``core.py`` only swaps the name; the resolver comes from ``headers``.
    """
    hint = ""
    headers = kwargs.get("headers")
    resolver = getattr(headers, "tenant_scope", None)
    if resolver is not None and url.startswith(f"{resolver.base_url}/api/"):
        # Split on the known base URL: it may itself contain "/api/".
        path, hint = await _scope_path(headers, url[len(resolver.base_url) :])
        url = f"{resolver.base_url}{path}"
    elif resolver is not None:
        logger.debug(
            "OpenAEV tenant scoping skipped, URL not under %s: %s",
            redact_url(resolver.base_url),
            redact_url(url),
        )
    resp, err = await safe_request(client, method, url, provider, action, **kwargs)
    return resp, _with_scope_hint(_annotate_cross_tenant_404(err, url), hint)


def _degrade_after_transient_retry(err: str) -> str:
    """Append anti-spin guidance to a 5xx that survived the HTTP-layer retry.

    Idempotent autonomous callbacks retry a transient 5xx with backoff, so a 5xx
    that still reaches the model is persistent. Without this nudge the
    orchestrator re-issues the same call over and over (the scope-PUT retry storm
    the operator saw), burning iterations and tokens. One sentence, no prompt
    inflation.
    """
    if "server error (5" not in err:
        return err
    return (
        f"{err} This call already retried automatically and still failed - do "
        "NOT immediately re-issue it; continue with another action, or record a "
        "GAP and move on."
    )


def _annotate_not_found(err: str) -> str:
    """Explain OpenAEV's overloaded 404 so the agent stops guessing.

    OpenAEV deliberately masks RBAC denials as 404 (``AccessDeniedException`` ->
    ``ErrorMessage("Not Found")`` in ``RestBehavior``), and preview-gated
    endpoints (``ATTACK_PATH``, ``INJECT_CHAINING``) also answer 404 when the
    feature flag is off. A bare "not found" therefore sends the agent hunting
    for a wrong id when the real cause is permissions or a disabled feature.
    """
    if not is_not_found_error(err):
        return err
    return (
        f"{err} In OpenAEV a 404 can mean three different things: the entity "
        "really does not exist, the API token lacks permission for it (RBAC "
        "denials are deliberately reported as 404, not 403), or the endpoint "
        "belongs to a preview feature that is disabled on this platform. Do not "
        "retry with a different id until the other two are ruled out - say which "
        "of the three it is, or that it cannot be distinguished, rather than "
        "reporting the entity as missing."
    )


_TENANT_IN_PATH = re.compile(r"/api/tenants/([^/?]+)")


def _annotate_cross_tenant_404(err: str | None, path_or_url: str) -> str | None:
    """A tenant-scoped request sees only its tenant: an ID from another one also answers 404."""
    if not err or not is_not_found_error(err):
        return err
    match = _TENANT_IN_PATH.search(path_or_url)
    if not match:
        return err
    return (
        f"{err} On a multi-tenant OpenAEV it can also be an entity of another tenant: "
        f"this request only sees the tenant {unquote(match.group(1))}."
    )


def _select_tenant(default_tenant_id: str, member_ids: list[str] | None) -> tuple[str, str | None]:
    """The tenant the caller's requests go to, as OpenAEV's ``fallbackSelector`` picks it.

    One membership is that tenant, several are the default when it is among them, else an
    explicit choice is needed. The default is a platform constant, not the caller's: naming a
    tenant the caller is not in answers 403. ``None`` (unknown memberships) keeps the default.
    """
    if member_ids is None:
        return default_tenant_id, None
    if len(member_ids) == 1:
        return member_ids[0], None
    if default_tenant_id in member_ids:
        return default_tenant_id, None
    if not member_ids:
        return "", (
            "the caller belongs to no tenant: add the integration's account to a "
            "tenant, or set the OpenAEV integration's Tenant field"
        )
    return "", (
        f"the caller belongs to {len(member_ids)} tenants and none of them is the "
        "default tenant: set the OpenAEV integration's Tenant field"
    )


@dataclass(frozen=True)
class TenantScope:
    """Tenant lookup: resolved (``tenant_id``), none (both empty) or failed.

    ``error`` marks a FAILED lookup, which ``PlatformCapabilities.tenant_id()`` conflates with none.
    ``configured`` marks a tenant set on the integration rather than chosen;
    ``memberships_unknown`` one chosen without knowing the account's tenants.
    """

    tenant_id: str = ""
    error: str | None = None
    configured: bool = False
    memberships_unknown: bool = False


class PlatformCapabilities:
    """Lazily-fetched, per-tool-set snapshot of ``GET /api/settings``.

    Two things an OpenAEV v3 client cannot hardcode:

    * the **tenant id** the caller's requests go to (the integration's Tenant
      field when set, else chosen by ``_select_tenant``), needed to build
      ``/api/tenants/{id}/...`` URLs;
    * the **enabled preview features** (``enabled_dev_features``), because
      flagged endpoints answer 404 rather than 403 when disabled, which is
      indistinguishable from a wrong URL.

    The snapshot is fetched at most once per tool-set (one integration bind, so
    one agent turn) and cached - including a negative result, so a platform that
    denies ``PLATFORM_SETTING`` reads does not pay one failed request per tool
    call. The "all preview features on" wildcard means every preview feature is
    on; OpenAEV configures it as ``"*"`` but ``GET /api/settings`` serialises the
    ``PreviewFeature`` enum by NAME, so it comes across the wire as the string
    ``"FEATURE_FLAG_ALL"`` (this is also what OpenAEV's own frontend matches on).
    We therefore accept BOTH tokens - matching only ``"*"`` made every flagged
    tool wrongly report the feature disabled whenever the platform ran with
    ``enabled-dev-features=*``.
    """

    _FEATURE_FLAG_ALL_TOKENS = frozenset({"*", "FEATURE_FLAG_ALL"})

    def __init__(
        self, base_url: str, headers: dict[str, str], *, configured_tenant_id: str = ""
    ) -> None:
        self._base_url = base_url
        self._headers = headers
        # The integration's Tenant field: wins over the automatic choice, without lookup.
        self._configured_tenant_id = configured_tenant_id.strip()
        self._loaded = False
        self._settings: dict[str, Any] = {}
        self._error: str | None = None
        self._tenant_id = ""
        # Kept apart from ``_error``, which means "settings unreadable" (feature gating).
        self._tenant_error: str | None = None
        self._memberships_unknown = False
        self._lock = asyncio.Lock()

    async def _load(self) -> None:
        if self._loaded:
            return
        # A concurrent first caller waits here instead of reading empty settings.
        async with self._lock:
            if self._loaded:
                return
            try:
                resp, err = await _api(
                    self._base_url,
                    self._headers,
                    "get",
                    "/api/settings",
                    "read settings",
                    # Idempotent; a lone 5xx would otherwise unscope the whole tool set.
                    retry_transient=True,
                    # Holding the lock here: consulting the resolver would deadlock.
                    scoped=False,
                )
                if err:
                    self._error = f"GET /api/settings failed: {err}"
                else:
                    data = resp.json()
                    self._settings = data if isinstance(data, dict) else {}
                    default_tenant_id = str(self._settings.get("default_tenant_id") or "")
                    if default_tenant_id and not self._configured_tenant_id:
                        members = await self._member_tenant_ids()
                        self._memberships_unknown = members is None
                        self._tenant_id, self._tenant_error = _select_tenant(
                            default_tenant_id, members
                        )
                    logger.debug("OpenAEV tenant: %s", self._tenant_id or "none")
            except Exception as exc:  # e.g. a 200 with a non-JSON proxy page
                # Every tool depends on this probe: report, never raise.
                self._error = f"settings response unreadable: {exc}"
            # Only on completion: a cancelled load must not cache "no tenant".
            self._loaded = True

    async def _member_tenant_ids(self) -> list[str] | None:
        """The caller's tenant ids (``GET /api/me/tenants``), or ``None`` when unknown."""
        resp, err = await _api(
            self._base_url,
            self._headers,
            "get",
            "/api/me/tenants",
            "read tenant memberships",
            retry_transient=True,
            # Called from _load, under the lock: same reason as the settings probe.
            scoped=False,
        )
        if err:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, list):
            return None
        return [
            str(t["tenant_id"]) for t in data if isinstance(t, dict) and t.get("tenant_id")
        ]

    @property
    def base_url(self) -> str:
        return self._base_url

    async def tenant_id(self) -> str:
        """The caller's tenant id (see ``_select_tenant``), or ``""`` when unresolved."""
        return (await self.tenant_scope()).tenant_id

    async def tenant_scope(self) -> TenantScope:
        """Tri-state lookup: resolved, no tenant, or failed (with the cause)."""
        if self._configured_tenant_id:
            # Holds even when the settings are unreadable: nothing to look up.
            return TenantScope(self._configured_tenant_id, None, configured=True)
        await self._load()
        return TenantScope(
            self._tenant_id,
            self._error or self._tenant_error,
            memberships_unknown=self._memberships_unknown,
        )

    async def enabled_features(self) -> list[str]:
        await self._load()
        raw = self._settings.get("enabled_dev_features") or []
        if not isinstance(raw, list):
            return []
        return [str(f) for f in raw]

    async def version(self) -> str:
        await self._load()
        return str(self._settings.get("platform_version") or "")

    @classmethod
    def has_all_features_wildcard(cls, features: list[str]) -> bool:
        """True when *features* contains the "all preview features on" wildcard.

        Accepts both the config value ``"*"`` and the ``"FEATURE_FLAG_ALL"`` enum
        name that OpenAEV actually serialises on ``GET /api/settings``.
        """
        return any(token in features for token in cls._FEATURE_FLAG_ALL_TOKENS)

    async def is_feature_enabled(self, feature: str) -> bool:
        features = await self.enabled_features()
        if self.has_all_features_wildcard(features):
            return True
        return feature in features

    async def require_feature(self, feature: str, surface: str) -> str:
        """Return an actionable message when *feature* is off, else ``""``.

        Checked BEFORE issuing the request so a disabled preview feature
        produces a precise explanation instead of the platform's bare 404.
        """
        if await self.is_feature_enabled(feature):
            return ""
        if self._error:
            # Could not read the settings, so the flag state is unknown - let
            # the call through rather than blocking on a probe failure.
            return ""
        enabled = await self.enabled_features()
        listed = ", ".join(enabled) if enabled else "none"
        return (
            f"{surface} is a preview feature on OpenAEV and is not enabled on "
            f"this platform (enabled_dev_features: {listed}). An administrator "
            f"has to add '{feature}' to the enabled_dev_features configuration; "
            "until then these endpoints return 404."
        )


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _fmt_asset(a: dict, base_url: str = "") -> str:
    name = a.get("asset_name") or a.get("endpoint_name") or a.get("asset_id", "?")
    asset_id = a.get("asset_id") or a.get("endpoint_id", "N/A")
    lines = [f"[Asset] {name}"]
    lines.append(f"  ID: {asset_id}")
    link = _aev_link(base_url, "asset", asset_id)
    if link:
        lines.append(link)
    if a.get("asset_hostname"):
        lines.append(f"  Hostname: {a.get('asset_hostname')}")
    ips = a.get("asset_ips") or []
    if ips:
        lines.append(f"  IPs: {', '.join(ips)}")
    macs = a.get("asset_mac_addresses") or []
    if macs:
        lines.append(f"  MACs: {', '.join(macs)}")
    platform = a.get("endpoint_platform") or a.get("asset_platform")
    if platform:
        lines.append(f"  Platform: {platform}")
    if a.get("endpoint_arch"):
        lines.append(f"  Arch: {a['endpoint_arch']}")
    for key, label in _ASSET_CLASSIFICATION_LINES:
        value = a.get(key)
        if value is not None and value != "":
            lines.append(f"  {label}: {value}")
    if a.get("asset_description"):
        lines.append(f"  Description: {a['asset_description'][:300]}")
    tags = [t.get("tag_id") if isinstance(t, dict) else t for t in a.get("asset_tags") or []]
    tags = [str(t) for t in tags if t]
    if tags:
        lines.append(f"  Tags: {', '.join(tags)}")
    return "\n".join(lines)


#: The fields manage_openaev_asset writes, printed so a write can be read back
#: (an internet-facing false is printed too: it is a value, not an absence).
_ASSET_CLASSIFICATION_LINES = (
    ("asset_category", "Category"),
    ("asset_subcategory", "Subcategory"),
    ("asset_criticality", "Criticality"),
    ("asset_internet_facing", "Internet-facing"),
    ("asset_url", "URL"),
    ("asset_external_reference", "External reference"),
)


def _fmt_asset_group(g: dict) -> str:
    # AssetGroups have no canonical detail URL — the OpenAEV UI surfaces
    # them only via search filters. Omit the link rather than emit a
    # broken / query-shaped one.
    lines = [f"[AssetGroup] {g.get('asset_group_name', '?')}"]
    lines.append(f"  ID: {g.get('asset_group_id', 'N/A')}")
    if g.get("asset_group_description"):
        lines.append(f"  Description: {g['asset_group_description'][:300]}")
    if g.get("asset_group_assets_number") is not None:
        lines.append(f"  Assets count: {g['asset_group_assets_number']}")
    if "asset_group_dynamic_filter" in g:
        described = describe_filter_group(g.get("asset_group_dynamic_filter"))
        lines.append(
            f"  Dynamic filter: {clip_text(described, 600)}"
            if described
            else "  Dynamic filter: none (static members only)"
        )
    static_ids = g.get("asset_group_assets")
    dynamic_ids = g.get("asset_group_dynamic_assets")
    if isinstance(static_ids, list) or isinstance(dynamic_ids, list):
        counts = []
        if isinstance(static_ids, list):
            counts.append(f"{len(static_ids)} static")
        if isinstance(dynamic_ids, list):
            counts.append(f"{len(dynamic_ids)} dynamic")
        lines.append(f"  Member ids on the group: {', '.join(counts)}")
    tags = g.get("asset_group_tags") or []
    if tags:
        lines.append(f"  Tags: {', '.join(tags)}")
    return "\n".join(lines)


#: Members listed per call by get_openaev_asset_group (OpenAEV pages them).
ASSET_GROUP_MEMBERS_MAX = 100


def describe_filter_group(group: Any) -> str:
    """An OpenAEV ``FilterGroup`` as one readable line (``""`` when empty).

    ``asset_name contains [web, db] AND (asset_tags eq [t1] OR ...)``: the
    values of one filter are alternatives, the group's mode joins the filters.
    """
    if not isinstance(group, dict):
        return ""
    mode = str(group.get("mode") or "and").upper()
    parts: list[str] = []
    for f in group.get("filters") or []:
        if not isinstance(f, dict):
            continue
        nested = f.get("filterGroup")
        if isinstance(nested, dict):
            inner = describe_filter_group(nested)
            if inner:
                parts.append(f"({inner})")
            continue
        key = f.get("key")
        if not key:
            continue
        operator = f.get("operator") or "eq"
        values = [str(v) for v in (f.get("values") or [])]
        if operator in ("empty", "not_empty") or not values:
            parts.append(f"{key} {operator}")
        else:
            parts.append(f"{key} {operator} [{', '.join(values)}]")
    return f" {mode} ".join(parts)


def _fmt_asset_group_member(m: dict[str, Any]) -> str:
    name = m.get("asset_name") or m.get("endpoint_name") or "?"
    details = [f"id: {m.get('asset_id') or m.get('endpoint_id') or 'N/A'}"]
    for label, key in (
        ("type", "asset_type"),
        ("hostname", "asset_hostname"),
        ("platform", "endpoint_platform"),
        ("criticality", "asset_criticality"),
    ):
        if m.get(key):
            details.append(f"{label}: {m[key]}")
    line = f"    - {name} ({', '.join(details)})"
    if m.get("is_static") is not None:
        line += " [static]" if m["is_static"] else " [dynamic]"
    return line


def fmt_asset_group_members(page: Any, page_index: int, size: int, search: str = "") -> list[str]:
    """Lines for one page of ``POST /api/asset_groups/{id}/assets/search``."""
    if not isinstance(page, dict):
        return ["  Members: not listed (OpenAEV answered no page)."]
    content = [m for m in page.get("content") or [] if isinstance(m, dict)]
    total = page.get("totalElements")
    if not isinstance(total, int):
        total = len(content)
    scope = f" matching '{search}'" if search else ""
    if total == 0:
        return [f"  Members{scope}: none (static and dynamic)."]
    start = page_index * size
    if not content:
        return [f"  Members{scope}: {total} in all; page {page_index} is past the last one."]
    end = start + len(content)
    lines = [f"  Members{scope}: {total} in all (static and dynamic), showing {start + 1}-{end}:"]
    lines.extend(_fmt_asset_group_member(m) for m in content)
    if end < total:
        lines.append(f"    ... {total - end} more: call again with members_page={page_index + 1}.")
    return lines


def asset_group_member_ids_lines(g: dict[str, Any], reason: str, limit: int = 20) -> list[str]:
    """Fallback when the member search is refused: the ids the group carries."""
    lines = [f"  Members: not listed by name ({clip_text(reason, 200)})."]
    for label, key in (("Static", "asset_group_assets"), ("Dynamic", "asset_group_dynamic_assets")):
        ids = g.get(key)
        if isinstance(ids, list) and ids:
            shown = ", ".join(str(i) for i in ids[:limit])
            more = f" (+{len(ids) - limit} more)" if len(ids) > limit else ""
            lines.append(f"    {label} member ids: {shown}{more}")
    return lines


def _fmt_team(t: dict, base_url: str = "") -> str:
    team_id = t.get("team_id", "N/A")
    lines = [f"[Team] {t.get('team_name', '?')}"]
    lines.append(f"  ID: {team_id}")
    link = _aev_link(base_url, "team", team_id)
    if link:
        lines.append(link)
    if t.get("team_description"):
        lines.append(f"  Description: {t['team_description'][:300]}")
    if t.get("team_users_number") is not None:
        lines.append(f"  Members: {t['team_users_number']}")
    tags = t.get("team_tags") or []
    if tags:
        lines.append(f"  Tags: {', '.join(tags)}")
    return "\n".join(lines)


def _fmt_player(p: dict, base_url: str = "") -> str:
    name = (
        f"{p.get('user_firstname', '')} {p.get('user_lastname', '')}".strip()
        or p.get("user_email", "?")
    )
    user_id = p.get("user_id", "N/A")
    lines = [f"[Player] {name}"]
    lines.append(f"  ID: {user_id}")
    link = _aev_link(base_url, "player", user_id)
    if link:
        lines.append(link)
    lines.append(f"  Email: {p.get('user_email', 'N/A')}")
    if p.get("user_phone"):
        lines.append(f"  Phone: {p['user_phone']}")
    if p.get("user_organization"):
        lines.append(f"  Organization: {p['user_organization']}")
    return "\n".join(lines)


def _fmt_attack_pattern(ap: dict) -> str:
    name = ap.get("attack_pattern_name", "?")
    ext_id = ap.get("attack_pattern_external_id", "")
    label = f"{name} ({ext_id})" if ext_id else name
    lines = [f"[AttackPattern] {label}"]
    lines.append(f"  ID: {ap.get('attack_pattern_id', 'N/A')}")
    if ap.get("attack_pattern_description"):
        desc = ap["attack_pattern_description"][:400]
        lines.append(f"  Description: {desc}")
    if ap.get("attack_pattern_platforms"):
        lines.append(f"  Platforms: {', '.join(ap['attack_pattern_platforms'])}")
    if ap.get("attack_pattern_parent"):
        lines.append(f"  Parent: {ap['attack_pattern_parent']}")
    return "\n".join(lines)


def _fmt_scenario(s: dict, base_url: str = "") -> str:
    scenario_id = s.get("scenario_id", "N/A")
    lines = [f"[Scenario] {s.get('scenario_name', '?')}"]
    lines.append(f"  ID: {scenario_id}")
    link = _aev_link(base_url, "scenario", scenario_id)
    if link:
        lines.append(link)
    if s.get("scenario_description"):
        lines.append(f"  Description: {s['scenario_description'][:400]}")
    if s.get("scenario_subtitle"):
        lines.append(f"  Subtitle: {s['scenario_subtitle']}")
    if s.get("scenario_category"):
        lines.append(f"  Category: {s['scenario_category']}")
    if s.get("scenario_severity"):
        lines.append(f"  Severity: {s['scenario_severity']}")
    if s.get("scenario_main_focus"):
        lines.append(f"  Main focus: {s['scenario_main_focus']}")
    lines.extend(_scenario_state_lines(s))
    return "\n".join(lines)


# OpenAEV marks the scenarios its starter pack installs with this dependency;
# a scenario without it was created or imported by a user (3.x does not tell
# those two apart).
_SCENARIO_BUILTIN_DEPENDENCY = "STARTERPACK"

# Key get_openaev_scenario adds to the scenario payload: the inject count read
# from GET /api/scenarios/{id}/injects (an int), or None when that read failed.
SCENARIO_INJECT_COUNT_KEY = "_inject_count"


def count_scenario_injects(payload: Any) -> int | None:
    """Inject count of a GET /api/scenarios/{id}/injects answer (list or page)."""
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        total = payload.get("totalElements")
        if isinstance(total, int):
            return total
        content = payload.get("content")
        if isinstance(content, list):
            return len(content)
    return None


def _scenario_state_lines(s: dict[str, Any]) -> list[str]:
    """Recurrence, origin, inject count and dates of a scenario, when returned.

    Each line is rendered only when OpenAEV returned the field it reads, so a
    trimmed payload (an older OpenAEV, a search projection) adds nothing.
    """
    lines: list[str] = []
    if "scenario_recurrence" in s:
        cron = s.get("scenario_recurrence")
        if cron:
            recurrence = f"  Recurrence: {cron} (cron, UTC)"
            if s.get("scenario_recurrence_start"):
                recurrence += f", from {s['scenario_recurrence_start']}"
            if s.get("scenario_recurrence_end"):
                recurrence += f", until {s['scenario_recurrence_end']}"
            lines.append(recurrence)
        else:
            lines.append("  Recurrence: not planned")
    if "scenario_dependencies" in s:
        dependencies = s.get("scenario_dependencies") or []
        if _SCENARIO_BUILTIN_DEPENDENCY in dependencies:
            lines.append("  Origin: built-in (starter pack)")
        else:
            lines.append("  Origin: created or imported by a user")
    injects = s.get("scenario_injects")
    if isinstance(injects, list):
        lines.append(f"  Injects: {len(injects)}")
    elif SCENARIO_INJECT_COUNT_KEY in s:
        # Set by get_openaev_scenario from GET /api/scenarios/{id}/injects:
        # the scenario read itself does not return its injects.
        count = s.get(SCENARIO_INJECT_COUNT_KEY)
        lines.append(f"  Injects: {count if isinstance(count, int) else 'not returned'}")
    if "scenario_tags" in s:
        tags = s.get("scenario_tags") or []
        lines.append(f"  Tags: {len(tags) if isinstance(tags, list) and tags else 'none'}")
    if s.get("scenario_created_at"):
        lines.append(f"  Created: {s['scenario_created_at']}")
    if s.get("scenario_updated_at"):
        lines.append(f"  Updated: {s['scenario_updated_at']}")
    return lines


def _simulation_state_lines(e: dict[str, Any]) -> list[str]:
    """Parent scenario, dates and inject count of a simulation, when returned.

    A simulation a scenario recurrence created carries the scenario id and its
    start date; without them a reader cannot tell that the recurrence fired.
    Each line is rendered only when OpenAEV returned the field it reads.
    """
    lines: list[str] = []
    scenario = e.get("exercise_scenario")
    if isinstance(scenario, dict):
        scenario = scenario.get("scenario_id")
    if scenario:
        lines.append(f"  Scenario: {scenario}")
    for key, label in (
        ("exercise_start_date", "Start"),
        ("exercise_end_date", "End"),
        ("exercise_created_at", "Created"),
        ("exercise_updated_at", "Updated"),
    ):
        if e.get(key):
            lines.append(f"  {label}: {e[key]}")
    injects = e.get("exercise_injects")
    if isinstance(injects, list):
        lines.append(f"  Injects: {len(injects)}")
    return lines


#: Injects listed with their execution status on a simulation read.
SIMULATION_INJECT_ROWS = 25


def _simulation_inject_status_lines(rows: Any) -> list[str]:
    """One line per inject of a simulation with its execution status.

    ``rows`` is the answer of ``GET /api/exercises/{id}/injects/results``.
    The status says whether the inject ran (SUCCESS, ERROR, PENDING...), not
    whether the defense worked; an inject not executed yet has no status.
    """
    if not isinstance(rows, list):
        return []
    rows = [r for r in rows if isinstance(r, dict)]
    lines = [f"  Inject statuses ({len(rows)}):"]
    if not rows:
        lines.append("    none")
        return lines
    for row in rows[:SIMULATION_INJECT_ROWS]:
        status = row.get("inject_status")
        if isinstance(status, dict):
            status = status.get("status_name")
        title = row.get("inject_title") or "?"
        lines.append(f"    {title} (id: {row.get('inject_id', '?')}): {status or 'not executed'}")
    if len(rows) > SIMULATION_INJECT_ROWS:
        lines.append(
            f"    {len(rows) - SIMULATION_INJECT_ROWS} more not shown; "
            "manage_openaev_inject action 'results' lists them all."
        )
    return lines


def _fmt_finding(f: dict, base_url: str = "") -> str:
    finding = parse_finding(f)
    if finding is None:
        return f"[Finding] {f.get('finding_value', '?') if isinstance(f, dict) else '?'}\n  ID: N/A"
    lines = [f"[Finding] {finding.value or '?'}"]
    lines.append(f"  ID: {finding.id}")
    link = _aev_link(base_url, "finding", finding.id)
    if link:
        lines.append(link)
    lines.append(f"  Type: {finding.type or 'N/A'}")
    if finding.status:
        lines.append(f"  Status: {finding.status}")
    # The inject of the latest occurrence, so the orchestrator ties a finding back to the
    # step that last produced it (search rows carry it as finding_inject.inject_id).
    if finding.inject_id:
        lines.append(f"  Inject: {finding.inject_id}")
    if finding.asset_labels:
        lines.append(f"  Assets: {', '.join(finding.asset_labels)}")
    if finding.updated_at:
        lines.append(f"  Last seen: {finding.updated_at}")
    return "\n".join(lines)


def _fmt_vulnerability(v: dict) -> str:
    lines = [f"[Vulnerability] {v.get('vulnerability_external_id', '?')}"]
    lines.append(f"  ID: {v.get('vulnerability_id', 'N/A')}")
    if v.get("vulnerability_description"):
        lines.append(f"  Description: {v['vulnerability_description'][:400]}")
    if v.get("vulnerability_cvss_v31") is not None:
        lines.append(f"  CVSS v3.1: {v['vulnerability_cvss_v31']}")
    if v.get("vulnerability_vuln_status"):
        lines.append(f"  Status: {v['vulnerability_vuln_status']}")
    if v.get("vulnerability_published"):
        lines.append(f"  Published: {v['vulnerability_published']}")
    if v.get("vulnerability_cisa_vulnerability_name"):
        lines.append(f"  CISA name: {v['vulnerability_cisa_vulnerability_name']}")
    if v.get("vulnerability_remediation"):
        lines.append(f"  Remediation: {v['vulnerability_remediation'][:300]}")
    return "\n".join(lines)


def _collect_attack_pattern_ids(raw: object) -> list[str]:
    """Compact ATT&CK / pattern ids from a list of strings or objects."""
    ids: list[str] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return ids
    for item in raw:
        value = ""
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict):
            value = str(
                item.get("attack_pattern_external_id")
                or item.get("external_id")
                or item.get("attack_pattern_id")
                or ""
            ).strip()
        if value and value not in seen:
            seen.add(value)
            ids.append(value)
    return ids


def _fmt_injector_contract(c: dict) -> str:
    label = (c.get("injector_contract_labels") or {}).get("en", "Unnamed contract")
    lines = [f"[InjectorContract] {label}"]
    lines.append(f"  ID: {c.get('injector_contract_id', 'N/A')}")
    payload = c.get("injector_contract_payload")
    if isinstance(payload, dict):
        lines.append(f"  PayloadID: {payload.get('payload_id', 'N/A')}")
        pname = payload.get("payload_name")
        if pname:
            lines.append(f"  PayloadName: {pname}")
        executor = payload.get("command_executor") or payload.get("payload_executor")
        if executor:
            lines.append(f"  Executor: {executor}")
    payload_based = _is_payload_based_contract(c)
    lines.append(_KIND_PAYLOAD if payload_based else _KIND_NATIVE)
    if c.get("injector_contract_platforms"):
        lines.append(f"  Platforms: {', '.join(c['injector_contract_platforms'])}")
    if c.get("injector_contract_payload_type"):
        lines.append(f"  Payload type: {c['injector_contract_payload_type']}")
    if c.get("injector_contract_injector_type"):
        lines.append(f"  Injector type: {c['injector_contract_injector_type']}")
    if c.get("injector_name"):
        lines.append(f"  Injector: {c['injector_name']}")
    attack_ids = _collect_attack_pattern_ids(
        c.get("injector_contract_attack_patterns_external_id")
    )
    for extra in _collect_attack_pattern_ids(c.get("injector_contract_attack_patterns")):
        if extra not in attack_ids:
            attack_ids.append(extra)
    if attack_ids:
        lines.append(f"  ATT&CK: {', '.join(attack_ids)}")
    if c.get("injector_contract_atomic_testing"):
        lines.append("  Atomic testing: Yes")
    # Producible finding types (the "providing" set). Present when the search /
    # get response carries it. Empty list: payload-based is a FORK CANDIDATE
    # (duplicate + parsers on the copy); native injectors cannot be duplicated.
    providing = c.get("injector_contract_providing")
    if isinstance(providing, list):
        if providing:
            lines.append(f"  Produces findings: {', '.join(str(p) for p in providing)}")
        else:
            lines.append(_PRODUCES_NONE_FORK if payload_based else _PRODUCES_NONE_NATIVE)
    lines.extend(_fmt_contract_fields(c.get("injector_contract_content")))
    return "\n".join(lines)


# Input fields of a contract, as ``injector_contract_content`` (a JSON string)
# declares them: an agent builds valid inject content from this list instead
# of reading the injector's source code.
_CONTRACT_FIELDS_MAX = 40
_CONTRACT_CHOICES_MAX = 25
_CONTRACT_VALUE_CHARS = 200


def _contract_value(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return clip_text(text, _CONTRACT_VALUE_CHARS)


def _contract_choices(choices: Any) -> list[str]:
    """``value (label)`` items of a select (a map) or a choice list."""
    items: list[str] = []
    if isinstance(choices, dict):
        for value, label in choices.items():
            items.append(f"{value} ({label})" if label not in (None, "", value) else str(value))
    elif isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict):
                value = choice.get("value", choice.get("key"))
                label = choice.get("label")
                items.append(f"{value} ({label})" if label not in (None, "", value) else str(value))
            else:
                items.append(str(choice))
    return items


def _contract_condition(fields: Any, values: Any) -> str:
    """``when target_selector = assets`` from the condition fields and values."""
    keys = [str(k) for k in fields] if isinstance(fields, list) else []
    parts: list[str] = []
    for key in keys:
        if isinstance(values, dict) and key in values:
            parts.append(f"{key} = {_contract_value(values[key])}")
        else:
            parts.append(f"{key} set")
    if not keys and values:
        parts.append(_contract_value(values))
    return " and ".join(parts)


def _fmt_contract_fields(content: Any) -> list[str]:
    """Lines listing the input fields of a contract's ``injector_contract_content``.

    One line per field: key, type, label, mandatory or read-only, cardinality,
    default, choices, linked fields and the visibility / mandatory conditions.
    Absent or malformed content adds no line.
    """
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            return []
    if not isinstance(content, dict) or not isinstance(content.get("fields"), list):
        return []
    fields = [f for f in content["fields"] if isinstance(f, dict) and f.get("key")]
    if not fields:
        return []
    lines = [f"  Input fields ({len(fields)}; inject_content keys, mandatory ones marked):"]
    for field in fields[:_CONTRACT_FIELDS_MAX]:
        head = f"    - {field['key']} [{field.get('type', '?')}]"
        label = field.get("label")
        if label and label != field["key"]:
            head += f" {_contract_value(label)}"
        flags = []
        if field.get("mandatory"):
            flags.append("mandatory")
        if field.get("readOnly"):
            flags.append("read-only")
        if field.get("cardinality"):
            flags.append(f"cardinality {field['cardinality']}")
        if flags:
            head += f" ({', '.join(flags)})"
        lines.append(head)
        if field["key"] == "expectations":
            available = field.get("availableExpectations") or field.get("predefinedExpectations") or []
            types = [
                str(e.get("expectation_type")) for e in available if isinstance(e, dict) and e.get("expectation_type")
            ]
            if types:
                lines.append(f"        expectations offered: {', '.join(types)}")
            continue
        default = field.get("defaultValue")
        if default not in (None, "", [], {}):
            lines.append(f"        default: {_contract_value(default)}")
        choices = _contract_choices(field.get("choices"))
        if choices:
            shown = ", ".join(choices[:_CONTRACT_CHOICES_MAX])
            more = len(choices) - _CONTRACT_CHOICES_MAX
            if more > 0:
                shown += f" (+{more} more)"
            lines.append(f"        choices: {shown}")
        linked = [
            str(lf.get("key")) for lf in field.get("linkedFields") or [] if isinstance(lf, dict) and lf.get("key")
        ]
        if linked:
            lines.append(f"        linked to: {', '.join(linked)}")
        visible = _contract_condition(field.get("visibleConditionFields"), field.get("visibleConditionValues"))
        if visible:
            lines.append(f"        shown when: {visible}")
        required_when = _contract_condition(
            field.get("mandatoryConditionFields"), field.get("mandatoryConditionValues")
        )
        if required_when:
            lines.append(f"        mandatory when: {required_when}")
        groups = field.get("mandatoryGroups")
        if isinstance(groups, list) and groups:
            lines.append(f"        one of mandatory with: {', '.join(str(g) for g in groups)}")
    hidden = len(fields) - _CONTRACT_FIELDS_MAX
    if hidden > 0:
        lines.append(f"    ... {hidden} more field(s) not shown")
    return lines


def _fmt_payload(p: dict, base_url: str = "") -> str:
    payload_id = p.get("payload_id", "N/A")
    lines = [f"[Payload] {p.get('payload_name', '?')}"]
    lines.append(f"  ID: {payload_id}")
    link = _aev_link(base_url, "payload", payload_id)
    if link:
        lines.append(link)
    lines.append(f"  Type: {p.get('payload_type', 'N/A')}")
    if p.get("payload_description"):
        lines.append(f"  Description: {p['payload_description'][:300]}")
    if p.get("payload_platforms"):
        lines.append(f"  Platforms: {', '.join(p['payload_platforms'])}")
    if p.get("command_executor"):
        lines.append(f"  Executor: {p['command_executor']}")
    if p.get("command_content"):
        cmd = p["command_content"][:300]
        lines.append(f"  Command: {cmd}")
    if p.get("payload_status"):
        lines.append(f"  Status: {p['payload_status']}")
    return "\n".join(lines)


# OpenAEV ExecutionStatus: only these close an execution (isClosed(), plus the
# legacy SUCCESS alias of EXECUTED). DRAFT, QUEUING, EXECUTING and PENDING are
# set before or during a run, so the presence of a status never means it ran.
_INJECT_STATUSES_EXECUTED = frozenset(
    {"EXECUTED", "SUCCESS", "ERROR", "PARTIAL", "MAYBE_PREVENTED", "MAYBE_PARTIAL_PREVENTED"}
)
_INJECT_STATUSES_IN_PROGRESS = frozenset({"QUEUING", "EXECUTING", "PENDING"})


def _inject_row_status_name(row: dict[str, Any]) -> str:
    status = row.get("inject_status")
    if isinstance(status, dict):
        status = status.get("status_name")
    return str(status or "").strip().upper()


def _inject_row_state(row: dict[str, Any]) -> str:
    """Classify an inject result row: ``executed``, ``in_progress`` or ``not_executed``.

    Executed means a closed execution status (xtm-one #2238). A disabled
    inject, a DRAFT or a null status never sent has not run; a queued,
    executing or pending one (or one sent with no closed status) may still
    produce results.
    """
    name = _inject_row_status_name(row)
    if name in _INJECT_STATUSES_EXECUTED:
        return "executed"
    if row.get("inject_enabled") is False:
        return "not_executed"
    if name in _INJECT_STATUSES_IN_PROGRESS:
        return "in_progress"
    if name in ("", "DRAFT"):
        return "in_progress" if row.get("inject_sent_at") else "not_executed"
    return "in_progress"


def _inject_row_executed(row: dict[str, Any]) -> bool:
    """Whether an inject result row has run: its status closes an execution."""
    return _inject_row_state(row) == "executed"


def _no_expectation_results_message(executed: bool | None) -> str:
    """Name which case an empty expectation result is, when the caller knows.

    An inject that has executed and still carries no result row declares no
    expectations (it can only produce findings): waiting does not help. One
    that has not executed may still produce results.
    """
    if executed is True:
        return (
            "No expectation results: the inject has executed and declares no "
            "expectations, so none will come (findings only). Its execution "
            "measures nothing about the security posture."
        )
    if executed is False:
        return "No expectation results yet: the inject has not executed. Read again after it runs."
    return (
        "No expectation results. Either the inject has not executed yet, or it "
        "declares no expectations - in which case its execution measures "
        "nothing about the security posture."
    )


def _simulation_no_results_message(subject: str, rows: Any, rows_err: str | None) -> str:
    """Explain an empty simulation result from its inject result rows.

    ``rows`` is the ``/api/exercises/{id}/injects/results`` answer. Each inject
    is either executed with no expectation (findings only) or not executed
    yet, so the caller knows whether to wait or to stop looking. A draft,
    disabled or never sent inject is counted as not executed, never as
    executed (xtm-one #2238).
    """
    head = f"No expectation results for {subject}." if subject else "No expectation results."
    if rows_err or not isinstance(rows, list):
        detail = f" (inject statuses not read: {rows_err})" if rows_err else ""
        return (
            f"{head} Either its injects have not executed yet, or they declare no expectations (findings only).{detail}"
        )
    injects = [r for r in rows if isinstance(r, dict)]
    if not injects:
        return f"{head} The simulation has no inject."
    states = [(r, _inject_row_state(r)) for r in injects]
    executed = [r for r, s in states if s == "executed"]
    running = [r for r, s in states if s == "in_progress"]
    not_run = [r for r, s in states if s == "not_executed"]
    lines = [head]
    if executed:
        lines.append(
            f"  {len(executed)} inject(s) executed and declare no expectations, so "
            "no result will come for them (findings only); they measure nothing "
            "about the security posture."
        )
    if running:
        lines.append(f"  {len(running)} inject(s) in progress (queued, executing or pending): results may still come.")
    if not_run:
        lines.append(
            f"  {len(not_run)} inject(s) not executed (draft, disabled or never sent): "
            "nothing ran, so nothing was measured; no result comes for them until "
            "they are enabled and run."
        )
    for row, state in states[:SIMULATION_INJECT_ROWS]:
        title = row.get("inject_title") or row.get("inject_id") or "?"
        status = _inject_row_status_name(row) or "no status"
        if state == "executed":
            label = f"executed ({status}), no expectations"
        elif state == "in_progress":
            label = f"in progress ({status})"
        else:
            disabled = ", disabled" if row.get("inject_enabled") is False else ""
            label = f"not executed ({status}{disabled})"
        lines.append(f"    {title} (id: {row.get('inject_id', '?')}): {label}")
    if len(injects) > SIMULATION_INJECT_ROWS:
        lines.append(f"    {len(injects) - SIMULATION_INJECT_ROWS} more not shown")
    return "\n".join(lines)


async def simulation_results_answer(api: Any, base_url: str, headers: dict[str, str], exercise_id: str) -> str:
    """Answer a simulation's expectation results, naming why one is empty.

    ``api`` is the calling module's ``_api`` (tests patch it per module). An
    empty result reads the inject result rows once, so the answer says which
    injects executed with no expectation and which have not run yet.
    """
    resp, err = await api(
        base_url,
        headers,
        "get",
        f"/api/exercises/{exercise_id}/results",
        "get simulation results",
    )
    if err:
        return err
    subject = f"simulation {exercise_id}"
    results = resp.json()
    if isinstance(results, list) and results:
        return _fmt_expectation_results_by_type(results, subject=subject)
    rows_resp, rows_err = await api(
        base_url,
        headers,
        "get",
        f"/api/exercises/{exercise_id}/injects/results",
        "inject results",
    )
    rows = rows_resp.json() if rows_resp is not None and not rows_err else None
    return _simulation_no_results_message(subject, rows, rows_err)


def _fmt_expectation_results_by_type(results: Any, *, subject: str = "", executed: bool | None = None) -> str:
    """Render ``List<ExpectationResultsByType>`` with its semantics attached.

    This is the shape returned by the simulation-results endpoints and carried
    on every inject result row: ``{type, avgResult, distribution:[{id,label,
    value}]}`` (camelCase - unlike the snake_case entity fields).

    Two counting rules are surfaced explicitly because they are the classic
    source of wrong conclusions:

    * ``avgResult`` averages only rows that already have a score, so PENDING
      rows are EXCLUDED. One success plus ninety-nine pending averages to
      SUCCESS.
    * the success rate divides by every row INCLUDING pending, so it can be far
      lower than ``avgResult`` suggests.
    """
    if not isinstance(results, list) or not results:
        return _no_expectation_results_message(executed)

    lines: list[str] = []
    if subject:
        lines.append(f"Expectation results for {subject}:")
    for entry in results:
        if not isinstance(entry, dict):
            continue
        family = expectation_result_type(str(entry.get("type") or ""))
        status = str(entry.get("avgResult") or "UNKNOWN").upper()
        label = expectation_label(family, status)
        distribution = entry.get("distribution") or []
        counts: dict[str, int] = {}
        for bucket in distribution:
            if isinstance(bucket, dict):
                counts[str(bucket.get("id", "")).upper()] = int(bucket.get("value") or 0)
        success = counts.get("SUCCESS", 0)
        pending = counts.get("PENDING", 0)
        partial = counts.get("PARTIAL", 0)
        failed = counts.get("FAILED", 0)
        total = success + pending + partial + failed

        lines.append("")
        lines.append(f"[{family}] {status} - {label}")
        if total:
            lines.append(
                f"  Targets: {success} success, {failed} failed, {partial} partial, "
                f"{pending} pending (of {total})"
            )
            rate = (success / total) * 100 if total else 0.0
            lines.append(f"  Success rate: {rate:.0f}% (pending counted as not-yet-successful)")
        elif status == "UNKNOWN":
            lines.append(
                "  This expectation type is declared on the inject but has no "
                "result rows yet - it has most likely not been executed."
            )
        lines.append(f"  Meaning: {_EXPECTATION_MEANING.get(family, '')}")
        if pending and status in ("SUCCESS", "PARTIAL"):
            lines.append(
                f"  Caution: {pending} target(s) are still PENDING and are NOT "
                "included in the average above, so this verdict can still change."
            )
    return "\n".join(lines).strip()


def _fmt_expectation_row(e: dict) -> str:
    """Render one expectation row (``InjectExpectationOutput``).

    Surfaces the three things that decide how the row must be read: the
    hierarchy level (agent / asset / asset group / player / team), the
    validation mode behind ``inject_expectation_group``, and the per-source
    results - including sources that answered ``Expired``, which means "this
    platform never reported", not "this platform said no".
    """
    exp_type = str(e.get("inject_expectation_type") or "?").upper()
    status = str(e.get("inject_expectation_status") or "UNKNOWN").upper()
    label = expectation_label(exp_type, status)
    lines = [f"[{exp_type}] {status} - {label}"]
    lines.append(f"  ID: {e.get('inject_expectation_id', 'N/A')}")
    if e.get("inject_expectation_name"):
        lines.append(f"  Name: {e['inject_expectation_name']}")

    # Level is encoded by which target field is set, not by an explicit column.
    if e.get("inject_expectation_agent"):
        level = "agent (leaf - written directly by collectors)"
    elif e.get("inject_expectation_asset"):
        level = "asset (derived from its agents unless the asset is agentless)"
    elif e.get("inject_expectation_asset_group"):
        level = "asset group (always derived - cannot be validated directly)"
    elif e.get("inject_expectation_user"):
        level = "player (leaf)"
    elif e.get("inject_expectation_team"):
        level = "team (derived from its players)"
    else:
        level = "unattributed"
    lines.append(f"  Level: {level}")

    score = e.get("inject_expectation_score")
    expected = e.get("inject_expectation_expected_score")
    if score is None:
        lines.append(
            f"  Score: not scored yet (expected {expected}) - PENDING means the "
            "collection window is still open, NOT that the control failed"
        )
    else:
        lines.append(f"  Score: {score} / expected {expected} (SUCCESS needs score >= expected)")

    if e.get("inject_expectation_group") is not None:
        mode = (
            "at least one target must validate"
            if e.get("inject_expectation_group")
            else "all targets must validate"
        )
        lines.append(f"  Validation mode: {mode}")

    platforms = e.get("inject_expectation_expected_security_platforms") or []
    if platforms:
        lines.append(f"  Expected security platforms: {', '.join(str(p) for p in platforms)}")

    results = e.get("inject_expectation_results") or []
    if results:
        lines.append(f"  Per-source results ({len(results)}):")
        expired = 0
        unanswered = 0
        for r in results:
            if not isinstance(r, dict):
                continue
            text = r.get("result")
            src = r.get("sourceName") or r.get("sourceId") or "?"
            platform = r.get("sourcePlatform")
            suffix = f" [{platform}]" if platform else ""
            if not text:
                unanswered += 1
                lines.append(f"    - {src}{suffix}: no answer yet")
                continue
            if str(text) == EXPECTATION_EXPIRED_RESULT:
                expired += 1
            lines.append(f"    - {src}{suffix}: {text}")
        if expired:
            lines.append(
                f"  Note: {expired} source(s) reported 'Expired' - they never "
                "answered within the collection window. The platform then closed "
                "the expectation with the type's default verdict (failure for "
                "detection/prevention/human, 'Not vulnerable' for vulnerability), "
                "so this is an absence of evidence, not a measured result."
            )
        if unanswered:
            lines.append(
                f"  Note: {unanswered} expected source(s) have not answered yet, "
                "which is why the row can still be PENDING."
            )
        if len(results) > 1:
            lines.append(
                "  Note: across sources the HIGHEST result wins - one product "
                "detecting is enough for SUCCESS even if the others missed it."
            )
    traces = e.get("inject_expectation_traces") or []
    if traces:
        lines.append(f"  Alert evidence (traces): {len(traces)}")
    return "\n".join(lines)


def _fmt_arsenal_action(a: dict, base_url: str = "", *, detailed: bool = False) -> str:
    """Format a Threat Arsenal action (``ThreatArsenalAction*`` outputs)."""
    labels = a.get("action_labels")
    if isinstance(labels, dict):
        name = labels.get("en") or next(iter(labels.values()), None) or "?"
    else:
        name = (
            a.get("action_name")
            or (a.get("injector_contract_labels") or {}).get("en")
            or "?"
        )
    action_id = a.get("injector_contract_id") or a.get("action_id") or "N/A"
    lines = [f"[Action] {name}"]
    lines.append(f"  action_id: {action_id}")
    payload = a.get("action_payload")
    if isinstance(payload, dict):
        pid = payload.get("payload_id")
        ptype = payload.get("payload_type")
        lines.append(f"  Payload: {payload.get('payload_name', '?')} [{ptype}] (id: {pid})")
        link = _aev_link(base_url, "payload", pid)
        if link:
            lines.append(link)
    payload_based = _is_payload_based_contract(a)
    lines.append(_KIND_PAYLOAD if payload_based else _KIND_NATIVE)
    if a.get("action_injector_type"):
        lines.append(f"  Injector: {a['action_injector_type']}")
    platforms = a.get("action_platforms") or []
    if platforms:
        lines.append(f"  Platforms: {', '.join(str(p) for p in platforms)}")
    expectations = a.get("action_expectations") or []
    if expectations:
        families = sorted({expectation_result_type(str(x)) for x in expectations})
        lines.append(f"  Measures: {', '.join(families)}")
    elif detailed:
        lines.append(
            "  Measures: nothing - this action declares no expectations, so "
            "executing it proves nothing about the security controls."
        )
    if a.get("action_author_name"):
        lines.append(
            f"  Author: {a['action_author_name']} ({a.get('action_author_type', '?')})"
        )
    # Producible finding/output types (action_providing on the FULL 'get' output).
    # Empty list: payload-based is a FORK CANDIDATE; native injectors cannot be
    # duplicated - reuse the id.
    providing = a.get("action_providing")
    if isinstance(providing, list):
        if providing:
            lines.append(f"  Produces findings: {', '.join(str(p) for p in providing)}")
        elif detailed:
            lines.append(_PRODUCES_NONE_FORK if payload_based else _PRODUCES_NONE_NATIVE)
    if detailed:
        domains = a.get("action_domains_ids") or []
        if domains:
            lines.append(f"  Domain IDs: {', '.join(sorted(str(d) for d in domains))}")
        attack_patterns = a.get("action_attack_patterns_ids") or []
        if attack_patterns:
            lines.append(f"  Attack pattern IDs: {len(attack_patterns)}")
        if action_id != "N/A":
            lines.append(
                "  Input fields (keys, types, choices, defaults, conditions): "
                f"get_openaev_injector_contract(contract_id='{action_id}')"
            )
    return "\n".join(lines)


def _fmt_reporting(r: dict) -> str:
    """Format a reporting template (``/api/reportings``)."""
    lines = [f"[Reporting] {r.get('reporting_name', '?')}"]
    lines.append(f"  ID: {r.get('reporting_id', 'N/A')}")
    ctx_type = r.get("reporting_context_type")
    if ctx_type:
        ctx = f"  Subject: {ctx_type}"
        if r.get("reporting_context_id"):
            ctx += f" ({r['reporting_context_id']})"
        lines.append(ctx)
    if r.get("reporting_description"):
        lines.append(f"  Description: {r['reporting_description'][:300]}")
    if r.get("reporting_default_format"):
        lines.append(f"  Default format: {r['reporting_default_format']}")
    if r.get("reporting_time_range"):
        lines.append(f"  Time range: {r['reporting_time_range']}")
    modules = r.get("reporting_modules") or []
    if modules:
        types = [
            m.get("module_type", "?") if isinstance(m, dict) else str(m) for m in modules
        ]
        lines.append(f"  Modules ({len(types)}): {', '.join(types)}")
    schedules = r.get("reporting_schedules") or []
    if schedules:
        lines.append(f"  Schedules: {len(schedules)}")
    return "\n".join(lines)


def _fmt_reporting_generation(g: dict) -> str:
    """Format one reporting generation (a rendered report run)."""
    gen_id = g.get("reporting_generation_id", "N/A")
    status = g.get("reporting_generation_status", "?")
    lines = [f"[Generation] {gen_id} - {status}"]
    if g.get("reporting_generation_format"):
        lines.append(f"  Format: {g['reporting_generation_format']}")
    if g.get("reporting_generation_trigger"):
        lines.append(f"  Trigger: {g['reporting_generation_trigger']}")
    if g.get("reporting_generation_created_at"):
        lines.append(f"  Created: {g['reporting_generation_created_at']}")
    if g.get("reporting_generation_completed_at"):
        lines.append(f"  Completed: {g['reporting_generation_completed_at']}")
    if g.get("reporting_generation_error"):
        lines.append(f"  Error: {str(g['reporting_generation_error'])[:300]}")
    if status == "SUCCESS":
        lines.append(
            f"  Download: use action='download' with generation_id={gen_id}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


__all__ = [
    "ASSET_GROUP_MEMBERS_MAX",
    "COMMAND_EXECUTORS",
    "EXPECTATION_EXPIRED_RESULT",
    "EXPECTATION_RESULT_TYPES",
    "EXPECTATION_STATUSES",
    "EXPECTATION_TYPES",
    "FILTER_MODES",
    "FILTER_OPERATORS",
    "JSON_READ_CHARS",
    "PAYLOAD_TYPES",
    "PLATFORMS",
    "REPORTING_CONTEXT_TYPES",
    "REPORTING_FORMATS",
    "REPORTING_GENERATION_STATUSES",
    "REPORTING_MODULE_TYPES",
    "REPORTING_SCHEDULE_PERIODS",
    "REPORTING_TIME_RANGES",
    "SCENARIO_CATEGORIES",
    "SCENARIO_MAIN_FOCUS",
    "SCENARIO_SEVERITIES",
    "SECURITY_PLATFORM_TYPES",
    "TECHNICAL_EXPECTATION_TYPES",
    "_AEV_UI_PATHS",
    "_JWT_PROACTIVE_REFRESH_SKEW_SECONDS",
    "_PROVIDER",
    "OpenAEVHeaders",
    "PersistFileFn",
    "PlatformCapabilities",
    "TenantScope",
    "_RefreshableHeaders",
    "_aev_link",
    "_annotate_not_found",
    "_bearer_seconds_remaining",
    "_contains_filter",
    "_ensure_fresh_token",
    "_fmt_arsenal_action",
    "_fmt_asset",
    "_fmt_asset_group",
    "_fmt_attack_pattern",
    "_fmt_expectation_results_by_type",
    "_fmt_expectation_row",
    "_fmt_finding",
    "_fmt_injector_contract",
    "_fmt_payload",
    "_fmt_player",
    "_fmt_reporting",
    "_fmt_reporting_generation",
    "_fmt_scenario",
    "_fmt_team",
    "_fmt_vulnerability",
    "_headers",
    "_multi_contains_filter",
    "_require_response",
    "_scope_path",
    "_search_body",
    "_tenant_path",
    "_validate_filter_group",
    "_with_scope_hint",
    "asset_group_member_ids_lines",
    "describe_filter_group",
    "expectation_label",
    "expectation_result_type",
    "fmt_asset_group_members",
    "inject_carries_relations",
    "inject_targets_line",
    "is_not_found_error",
    "json_answer",
    "logger",
    "mirror_content_targets",
    "scoped_safe_request",
    "stored_targets_line",
]

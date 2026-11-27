# SPDX-License-Identifier: Apache-2.0

"""OpenAEV integration - core tools (relocated from the single-file provider).

Exposes ``register_core`` which builds the original OpenAEV tool set
(assets, teams, scenarios, payloads, injects, atomic testing, findings,
reference data, exercises). Shared helpers live in ``_common``; the
provider entry point ``build_tools`` lives in ``__init__``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from typing import Any
from urllib.parse import quote

from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    ASSET_GROUP_MEMBERS_MAX,
    COMMAND_EXECUTORS,
    CONTRACT_OUTPUT_TYPES,
    EXPECTATION_TYPES,
    FINDINGS_KEPT_AS_HISTORY,
    PAYLOAD_TYPES,
    PLATFORMS,
    SCENARIO_CATEGORIES,
    SCENARIO_INJECT_COUNT_KEY,
    SCENARIO_MAIN_FOCUS,
    SCENARIO_SEVERITIES,
    _aev_link,
    _api,
    _assets_carry_tags,
    _contains_filter,
    _fmt_asset,
    _fmt_asset_group,
    _fmt_attack_pattern,
    _fmt_finding,
    _fmt_injector_contract,
    _fmt_payload,
    _fmt_player,
    _fmt_scenario,
    _fmt_team,
    _fmt_vulnerability,
    _merge_fork_candidates,
    _multi_contains_filter,
    _openaev_tag_names,
    _providing_filter_clause,
    _require_response,
    _resolve_output_types,
    _search_body,
    _simulation_state_lines,
    _validate_filter_group,
    asset_group_member_ids_lines,
    asset_group_not_found,
    count_scenario_injects,
    describe_filter_group,
    fmt_asset_group_members,
    inject_targets_line,
    inventory_group_source_id,
    is_not_found_error,
    mirror_content_targets,
    scoped_safe_request,
    stored_targets_line,
    tool_client,
    ui_tenant_id,
)
from .read_scope import area_scope, count_text, with_scope

logger = logging.getLogger(__name__)

#: OpenAEV's own default name per expectation type, as
#: ``io.openaev.expectation.ExpectationBuilderService`` builds it (VULNERABILITY
#: is ``ExpectationType.VULNERABILITY.successLabel``). Used only for a type the
#: injector contract does not offer: a type it offers keeps the contract's own
#: object, name included, exactly as-is (#2480). XTM One never writes a name
#: of its own: VULNERABILITY has inverted polarity (SUCCESS = not vulnerable),
#: so a home-made label such as "Vulnerability confirmed" read the opposite way.
_OPENAEV_DEFAULT_EXPECTATION_NAMES = {
    "PREVENTION": "Prevention",
    "DETECTION": "Detection",
    "LOGGING": "Logging",
    "VULNERABILITY": "Not vulnerable",
    "CHALLENGE": "Expect targets to complete the challenge(s)",
    "ARTICLE": "Expect targets to read the article(s)",
    "MANUAL": "Manual expectation",
}


def _contract_offered_expectations(content_json: str | None) -> dict[str, dict[str, Any]]:
    """The contract's own expectation object per type, first one of each type.

    Read from the ``key == "expectations"`` field of ``injector_contract_content``:
    ``availableExpectations`` first, then the legacy ``predefinedExpectations``.
    Malformed or absent content returns ``{}``.
    """
    if not content_json:
        return {}
    try:
        content = json.loads(content_json)
    except (TypeError, ValueError):
        return {}
    if not isinstance(content, dict) or not isinstance(content.get("fields"), list):
        return {}
    offered: dict[str, dict[str, Any]] = {}
    for field in content["fields"]:
        if not isinstance(field, dict) or field.get("key") != "expectations":
            continue
        for list_key in ("availableExpectations", "predefinedExpectations"):
            for expectation in field.get(list_key) or []:
                if isinstance(expectation, dict) and isinstance(expectation.get("expectation_type"), str):
                    offered.setdefault(expectation["expectation_type"], expectation)
    return offered


def _build_inject_expectations(
    types: list[str], offered: dict[str, dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Turn expectation TYPE names into ``inject_content.expectations`` objects.

    The scenario-creator wires native injects (e.g. HTTP Request) whose contract
    predefines only a MANUAL expectation; to be scored against the security
    stack the inject must carry its own DETECTION / PREVENTION expectations, and
    OpenAEV only keeps the contract's predefined ones when ``inject_content`` has
    no ``expectations`` key (``putIfAbsent``), so these REPLACE the default.

    A type the contract offers (``offered``, from ``_contract_offered_expectations``)
    is the contract's own object, its name unchanged; the generic object below
    only fills the keys it leaves out. A type it does not offer gets OpenAEV's
    default name for the type (``_OPENAEV_DEFAULT_EXPECTATION_NAMES``), score
    100 and an empty platform scope, i.e. "any security platform".
    """
    expectations: list[dict[str, Any]] = []
    for expectation_type in types:
        expectation: dict[str, Any] = {
            "expectation_type": expectation_type,
            "expectation_name": _OPENAEV_DEFAULT_EXPECTATION_NAMES.get(expectation_type, expectation_type),
            "expectation_description": "",
            "expectation_score": 100,
            "expectation_expectation_group": False,
            "expectation_expected_security_platform_types": [],
        }
        if offered and expectation_type in offered:
            expectation.update(offered[expectation_type])
        expectations.append(expectation)
    return expectations


def _resolve_expectation_types(raw_types: list[str]) -> tuple[list[str], str | None]:
    """Canonicalise caller-supplied expectation names against ``EXPECTATION_TYPES``.

    Case-insensitive, order-preserving. Returns ``(types, None)`` on success or
    ``([], message)`` for the first unknown name, so ``create_openaev_payload``
    and ``add_openaev_inject_to_scenario`` share one validation instead of two
    copies that can drift. The empty-list case is left to the caller: a payload
    and an inject word that rejection differently.
    """
    resolved: list[str] = []
    for raw in raw_types:
        match = next(
            (t for t in EXPECTATION_TYPES if t.lower() == str(raw).strip().lower()),
            None,
        )
        if match is None:
            return [], (
                f"Unknown expectation type '{raw}'. Valid types: "
                f"{', '.join(EXPECTATION_TYPES)}."
            )
        resolved.append(match)
    return resolved, None


#: The expectation types a technical inject is auto-scored on when the caller
#: omits ``expectations`` (see ``add_openaev_inject_to_scenario``). PREVENTION
#: and DETECTION are the security-control pair every EDR/SIEM collector fills for
#: any executed technical inject. VULNERABILITY is deliberately NOT auto-added: a
#: vulnerability result is produced only by a vuln-scanner inject, so defaulting
#: it onto a generic technical step would leave an expectation no collector ever
#: resolves (it would Expire as a failure). A Nuclei / vuln step is scored on
#: VULNERABILITY by passing ``expectations`` explicitly.
_AUTO_SCORE_EXPECTATION_TYPES = ("PREVENTION", "DETECTION")

#: Expectation types fulfilled by a security product rather than a person. A
#: contract that PREDEFINES any of them already scores the inject against the
#: security stack (OpenAEV seeds the predefined ones, with their platform scope,
#: when ``inject_content`` carries no ``expectations``), so it is never
#: defaulted. LOGGING is OpenAEV's newer payload expectation, not one this tool
#: writes, hence its absence from ``EXPECTATION_TYPES``.
_SECURITY_CONTROL_EXPECTATION_TYPES = frozenset(
    {"PREVENTION", "DETECTION", "VULNERABILITY", "LOGGING"}
)


def _contract_default_expectations(content_json: str | None) -> list[dict[str, Any]]:
    """Expectations to write on an inject whose caller passed none.

    OpenAEV stores a contract's expectations in its ``injector_contract_content``
    JSON, under the field ``key == "expectations"``, as an
    ``availableExpectations`` array: each object carries an ``expectation_type``,
    its ``expectation_is_predefined`` flag and, for a technical type, the
    ``expectation_expected_security_platform_types`` it is scoped to. OpenAEV
    seeds the inject with the predefined items when ``inject_content`` has no
    ``expectations`` key. Legacy contracts list only predefined items, under
    ``predefinedExpectations``.

    * The contract predefines a security-control expectation (a payload
      contract's own types, the HTTP Request contract's DETECTION + PREVENTION):
      return ``[]``, so OpenAEV seeds them with the scope their author chose.
    * It only OFFERS PREVENTION / DETECTION (predefines none, or a lone MANUAL):
      return the contract's own PREVENTION / DETECTION objects, in that order,
      platform scope included, so the inject is scored instead of measuring
      nothing.
    * It offers neither (email, article, challenge): return ``[]``, its
      human-response expectation stands.

    Best-effort: malformed or absent content returns ``[]``.
    """
    if not content_json:
        return []
    try:
        content = json.loads(content_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(content, dict) or not isinstance(content.get("fields"), list):
        return []
    offered: dict[str, dict[str, Any]] = {}
    for field in content["fields"]:
        if not isinstance(field, dict) or field.get("key") != "expectations":
            continue
        for list_key, legacy in (("availableExpectations", False), ("predefinedExpectations", True)):
            for expectation in field.get(list_key) or []:
                if not isinstance(expectation, dict):
                    continue
                expectation_type = expectation.get("expectation_type")
                predefined = legacy or expectation.get("expectation_is_predefined") is True
                if predefined and expectation_type in _SECURITY_CONTROL_EXPECTATION_TYPES:
                    return []
                if isinstance(expectation_type, str):
                    offered.setdefault(expectation_type, expectation)
    defaults: list[dict[str, Any]] = []
    for expectation_type in _AUTO_SCORE_EXPECTATION_TYPES:
        if expectation_type in offered:
            # The generic object fills any key the contract leaves out; the
            # contract's own values (name, score, platform scope) win.
            expectation = _build_inject_expectations([expectation_type])[0]
            expectation.update(offered[expectation_type])
            defaults.append(expectation)
    return defaults


#: Expectation types a security platform of a given type fulfils. A predefined
#: one scoped to platform types none of which is registered in the tenant can
#: never be met: it would expire as a certain failure (#2378).
_PLATFORM_SCOPED_EXPECTATION_TYPES = frozenset({"PREVENTION", "DETECTION"})


def _contract_predefined_expectations(content_json: str | None) -> list[dict[str, Any]]:
    """The expectations OpenAEV seeds an inject of this contract with.

    The ``expectation_is_predefined`` items of ``availableExpectations`` (every
    item of a legacy ``predefinedExpectations`` list), in contract order, each
    a full scored object (the contract's own values win). Malformed or absent
    content returns ``[]``.
    """
    if not content_json:
        return []
    try:
        content = json.loads(content_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(content, dict) or not isinstance(content.get("fields"), list):
        return []
    predefined: list[dict[str, Any]] = []
    for field in content["fields"]:
        if not isinstance(field, dict) or field.get("key") != "expectations":
            continue
        for list_key, legacy in (("availableExpectations", False), ("predefinedExpectations", True)):
            for expectation in field.get(list_key) or []:
                if not isinstance(expectation, dict):
                    continue
                expectation_type = expectation.get("expectation_type")
                if not isinstance(expectation_type, str):
                    continue
                if legacy or expectation.get("expectation_is_predefined") is True:
                    full = _build_inject_expectations([expectation_type])[0]
                    full.update(expectation)
                    predefined.append(full)
    return predefined


def _unmeetable_platform_types(expectation: dict[str, Any], registered_types: set[str]) -> list[str]:
    """The platform types a PREVENTION / DETECTION expectation is scoped to when
    none of them is registered, else ``[]`` (an empty scope means any platform)."""
    if expectation.get("expectation_type") not in _PLATFORM_SCOPED_EXPECTATION_TYPES:
        return []
    scope = [
        str(t).upper()
        for t in expectation.get("expectation_expected_security_platform_types") or []
        if isinstance(t, str) and t.strip()
    ]
    if not scope or any(t in registered_types for t in scope):
        return []
    return scope


def _expectations_for_registered_platforms(
    content_json: str | None, registered_types: set[str] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a contract's predefined expectations by what the tenant can measure.

    Returns ``(kept, left_out)``: ``left_out`` holds every predefined PREVENTION
    or DETECTION expectation scoped to security platform types none of which is
    in ``registered_types``; ``kept`` is the rest of the predefined set, in
    contract order. ``registered_types`` None (the platforms could not be read)
    returns ``([], [])``: nothing is decided and the inject keeps OpenAEV's own
    seeding.
    """
    if registered_types is None:
        return [], []
    registered = {t.upper() for t in registered_types}
    kept: list[dict[str, Any]] = []
    left_out: list[dict[str, Any]] = []
    for expectation in _contract_predefined_expectations(content_json):
        (left_out if _unmeetable_platform_types(expectation, registered) else kept).append(expectation)
    return kept, left_out


def _left_out_line(left_out: list[dict[str, Any]], registered_types: set[str], *, kept: bool = True) -> str:
    """The tool answer's line naming the expectations left out and why.

    ``kept`` False: nothing else is predefined, so they stay on the inject
    (an empty list would make OpenAEV seed them again) and the line says so.
    """
    parts = [
        f"{e['expectation_type']} (expects {', '.join(e.get('expectation_expected_security_platform_types') or [])})"
        for e in left_out
    ]
    registered = ", ".join(sorted(registered_types)) or "none"
    head = "Expectations left out" if kept else "Expectations that cannot be met (kept: nothing else is predefined)"
    tail = "pass `expectations` explicitly to keep them." if kept else "expect them to expire as missed."
    return (
        f"{head}: {'; '.join(parts)}. No security platform of those types is "
        f"registered in this OpenAEV (registered: {registered}), so they could never be met; {tail}"
    )


#: The ``UpdateScenarioInput`` fields OpenAEV's own scenario forms send back on
#: ``PUT /api/scenarios/{id}``, read from ``GET`` under the same JSON names.
_SCENARIO_UPDATE_FIELDS = (
    "scenario_name",
    "scenario_subtitle",
    "scenario_description",
    "scenario_category",
    "scenario_main_focus",
    "scenario_severity",
    "scenario_default_kill_chain",
    "scenario_external_reference",
    "scenario_external_url",
    "scenario_tags",
    "scenario_message_header",
    "scenario_message_footer",
    "scenario_mail_from_name",
    "scenario_mails_reply_to",
    "scenario_custom_dashboard",
    "scenario_lessons_enabled",
)


def _findings_timeout_hint(err: str, scope: str, narrower: str) -> str:
    """Name the scope a timed-out findings search used and a narrower one (#2594)."""
    if "timed out" not in err:
        return err
    return f"Error: OpenAEV timed out searching findings scoped to {scope}. Retry narrower: {narrower}."


def register_core(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Build the original OpenAEV tool set (relocated, unchanged)."""
    hdrs = headers

    # ── 1. Search assets ──────────────────────────────────────────────

    async def search_openaev_assets(search: str = "", limit: int = 20, filter_group: str = "") -> str:
        """Search for assets (endpoints) by name, hostname, or IP, or by a FilterGroup.

        ``filter_group`` is a FilterGroup JSON on the Asset model keys (``asset_status``
        is a query-time formula the analytics engine does not index, #2406). With it,
        ``search`` becomes the platform text search; without it, the name / hostname /
        IP contains filter as before. The answer always carries the total, so a count
        needs ``limit=1``.
        """
        parsed_group: dict | None = None
        if filter_group:
            try:
                parsed_group = json.loads(filter_group)
            except ValueError as exc:
                return f"filter_group must be valid JSON: {exc}"
            filter_err = _validate_filter_group(parsed_group)
            if filter_err:
                return f"filter_group is invalid. {filter_err}"
        if parsed_group:
            body = _search_body(size=max(1, min(limit, 50)), filter_group=parsed_group, text_search=search)
        else:
            body = _search_body(
                size=max(1, min(limit, 50)),
                filter_group=_multi_contains_filter(["asset_name", "asset_hostname", "asset_ips"], search),
            )
        # Route through _api (not a raw httpx client) so a long autonomous run gets
        # the WS2 AutoRefreshClient JWT auto-refresh: this read otherwise 401s ~10 min
        # in when the platform-managed token expires and stays broken. Idempotent, so
        # it opts into the transient-5xx retry too.
        resp, err = await _api(
            base_url,
            hdrs,
            "post",
            "/api/endpoints/search",
            "search assets",
            json=body,
            retry_transient=True,
        )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        items = data.get("content", [])
        criteria = describe_filter_group(parsed_group) if parsed_group else ""
        if search:
            criteria = f"{criteria} AND text '{search}'" if criteria else f"'{search}'"
        tag_names = await _openaev_tag_names(base_url, hdrs) if _assets_carry_tags(items) else {}
        return format_entity_list(
            items,
            partial(_fmt_asset, base_url=base_url, tag_names=tag_names),
            noun="asset",
            total=data.get("totalElements"),
            empty_msg=f"No assets found for {criteria or 'this search'} (total 0).",
        )

    # ── 2. Get asset ──────────────────────────────────────────────────

    async def get_openaev_asset(asset_id: str) -> str:
        """Get full details of a specific asset by ID."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "get",
                f"{base_url}/api/endpoints/{asset_id}",
                _PROVIDER,
                "fetch asset",
                headers=hdrs,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        if not data:
            return f"Asset '{asset_id}' not found."
        tag_names = await _openaev_tag_names(base_url, hdrs) if _assets_carry_tags([data]) else {}
        return _fmt_asset(data, base_url, tag_names)

    # ── 3. Search asset groups ────────────────────────────────────────

    async def search_openaev_asset_groups(search: str, limit: int = 20) -> str:
        """Search for asset groups by name."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "post",
                f"{base_url}/api/asset_groups/search",
                _PROVIDER,
                "search asset groups",
                headers=hdrs,
                json=_search_body(
                    size=min(limit, 50),
                    filter_group=_contains_filter("asset_group_name", search),
                ),
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        items = data.get("content", [])
        return format_entity_list(
            items,
            _fmt_asset_group,
            noun="asset group",
            total=data.get("totalElements"),
            empty_msg=f"No asset groups found for '{search}'.",
        )

    # ── 4. Get asset group ────────────────────────────────────────────

    async def get_openaev_asset_group(
        group_id: str,
        members_limit: int = 20,
        members_page: int = 0,
        member_search: str = "",
    ) -> str:
        """An asset group with its dynamic filter and its members, paged.

        An inventory id of a group synced from OpenAEV is read as that group's OpenAEV id (#2714).
        """
        requested = str(group_id or "").strip()
        resolved = await inventory_group_source_id(requested)
        group_id = resolved or requested
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "get",
                f"{base_url}/api/asset_groups/{group_id}",
                _PROVIDER,
                "fetch asset group",
                headers=hdrs,
            )
        if err:
            return asset_group_not_found(err, requested) if is_not_found_error(err) and not resolved else err
        resp = _require_response(resp)
        data = resp.json()
        if not data:
            return asset_group_not_found("empty answer", requested)
        out = _fmt_asset_group(data)
        if resolved:
            out = (
                f"Read as OpenAEV asset group {resolved}: '{requested}' is its XTM One inventory id "
                f"(pass the source_id next time).\n{out}"
            )
        size = max(0, min(int(members_limit), ASSET_GROUP_MEMBERS_MAX))
        if size == 0:
            return out
        page = max(0, int(members_page))
        # Static and dynamic members alike: OpenAEV resolves the dynamic
        # filter server-side on this search, the group read carries ids only.
        async with tool_client(hdrs, timeout=30) as client:
            members_resp, members_err = await scoped_safe_request(
                client,
                "post",
                f"{base_url}/api/asset_groups/{group_id}/assets/search",
                _PROVIDER,
                "list asset group members",
                headers=hdrs,
                json=_search_body(page=page, size=size, text_search=member_search.strip()),
            )
        if members_err or members_resp is None:
            lines = asset_group_member_ids_lines(data, members_err or "OpenAEV answered no member page")
        else:
            lines = fmt_asset_group_members(members_resp.json(), page, size, member_search.strip())
        return "\n".join([out, *lines])

    # ── 5. Search teams ───────────────────────────────────────────────

    async def search_openaev_teams(search: str, limit: int = 20) -> str:
        """Search for teams by name."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "post",
                f"{base_url}/api/teams/search",
                _PROVIDER,
                "search teams",
                headers=hdrs,
                json=_search_body(
                    size=min(limit, 50),
                    filter_group=_contains_filter("team_name", search),
                ),
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        items = data.get("content", [])
        return format_entity_list(
            items,
            partial(_fmt_team, base_url=base_url),
            noun="team",
            total=data.get("totalElements"),
            empty_msg=f"No teams found for '{search}'.",
        )

    # ── 6. Get team ───────────────────────────────────────────────────

    async def get_openaev_team(team_id: str) -> str:
        """Get full details of a specific team by ID."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "get",
                f"{base_url}/api/teams/{team_id}",
                _PROVIDER,
                "fetch team",
                headers=hdrs,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        return _fmt_team(data, base_url) if data else f"Team '{team_id}' not found."

    # ── 7. Search players ─────────────────────────────────────────────

    async def search_openaev_players(search: str, limit: int = 20) -> str:
        """Search for players/users by email, first name, or last name."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "post",
                f"{base_url}/api/players/search",
                _PROVIDER,
                "search players",
                headers=hdrs,
                json=_search_body(
                    size=min(limit, 50),
                    filter_group=_multi_contains_filter(
                        ["user_email", "user_firstname", "user_lastname"], search
                    ),
                ),
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        items = data.get("content", [])
        return format_entity_list(
            items,
            partial(_fmt_player, base_url=base_url),
            noun="player",
            total=data.get("totalElements"),
            empty_msg=f"No players found for '{search}'.",
        )

    # ── 8. Search attack patterns ─────────────────────────────────────

    async def search_openaev_attack_patterns(
        search: str = "", limit: int = 20, query: str = ""
    ) -> str:
        """Search attack patterns (MITRE ATT&CK techniques) or list all."""
        # LLMs frequently call this with `query` (the common search-tool
        # convention) instead of the declared `search`. Accept it as an alias so
        # a mislabelled argument doesn't waste a whole agent iteration.
        search = search or query
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "post",
                f"{base_url}/api/attack_patterns/search",
                _PROVIDER,
                "search attack patterns",
                headers=hdrs,
                json=_search_body(size=min(limit, 100), text_search=search),
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        items = data.get("content", [])
        return format_entity_list(
            items,
            _fmt_attack_pattern,
            noun="attack pattern",
            total=data.get("totalElements"),
            empty_msg=f"No attack patterns found{' for ' + repr(search) if search else ''}.",
        )

    # ── 9. Get attack pattern ─────────────────────────────────────────

    async def get_openaev_attack_pattern(attack_pattern_id: str) -> str:
        """Get full details of a specific attack pattern by ID."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "get",
                f"{base_url}/api/attack_patterns/{attack_pattern_id}",
                _PROVIDER,
                "fetch attack pattern",
                headers=hdrs,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        return (
            _fmt_attack_pattern(data)
            if data
            else f"Attack pattern '{attack_pattern_id}' not found."
        )

    # ── 10. Search scenarios ──────────────────────────────────────────

    async def search_openaev_scenarios(search: str = "", limit: int = 20) -> str:
        """Search for scenarios or list all."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "post",
                f"{base_url}/api/scenarios/search",
                _PROVIDER,
                "search scenarios",
                headers=hdrs,
                json=_search_body(size=min(limit, 50), text_search=search),
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        items = data.get("content", [])
        return format_entity_list(
            items,
            partial(_fmt_scenario, base_url=base_url, tenant_id=await ui_tenant_id(hdrs)),
            noun="scenario",
            total=data.get("totalElements"),
            empty_msg=f"No scenarios found{' for ' + repr(search) if search else ''}.",
        )

    # ── 11. Get scenario ──────────────────────────────────────────────

    async def get_openaev_scenario(scenario_id: str) -> str:
        """Get full details of a specific scenario by ID."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "get",
                f"{base_url}/api/scenarios/{scenario_id}",
                _PROVIDER,
                "fetch scenario",
                headers=hdrs,
            )
            if err:
                return err
            resp = _require_response(resp)
            data = resp.json()
            if not data:
                return f"Scenario '{scenario_id}' not found."
            if isinstance(data, dict) and not isinstance(data.get("scenario_injects"), list):
                # The scenario read (ScenarioOutput) carries no injects: count them
                # from the scenario's inject list, or say the count was not returned.
                count_resp, count_err = await scoped_safe_request(
                    client,
                    "get",
                    f"{base_url}/api/scenarios/{scenario_id}/injects",
                    _PROVIDER,
                    "count scenario injects",
                    headers=hdrs,
                )
                count = None
                if not count_err and count_resp is not None:
                    try:
                        count = count_scenario_injects(count_resp.json())
                    except ValueError:
                        count = None
                data = {**data, SCENARIO_INJECT_COUNT_KEY: count}
        return _fmt_scenario(data, base_url, await ui_tenant_id(hdrs))

    # ── 12. Create scenario ───────────────────────────────────────────

    async def create_openaev_scenario(
        name: str,
        description: str = "",
        subtitle: str = "",
        category: str = "attack-scenario",
        main_focus: str = "incident-response",
        severity: str = "high",
        tags: list[str] | None = None,
    ) -> str:
        """Create a new scenario in OpenAEV."""
        payload: dict[str, Any] = {
            "scenario_name": name,
            "scenario_description": description,
            "scenario_subtitle": subtitle,
            "scenario_category": category,
            "scenario_main_focus": main_focus,
            "scenario_severity": severity,
        }
        if tags:
            payload["scenario_tags"] = tags
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "post",
                f"{base_url}/api/scenarios",
                _PROVIDER,
                "create scenario",
                headers=hdrs,
                json=payload,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        sid = data.get("scenario_id", "unknown")
        return (
            f"Scenario created: [{data.get('scenario_name', name)}] "
            f"(ID: {sid}, Category: {category}, Severity: {severity})\n"
            f"URL: {base_url}/admin/scenarios/{sid}"
        )

    # ── 13. Update scenario ───────────────────────────────────────────

    async def update_openaev_scenario(
        scenario_id: str,
        name: str | None = None,
        description: str | None = None,
        subtitle: str | None = None,
        category: str | None = None,
        main_focus: str | None = None,
        severity: str | None = None,
    ) -> str:
        """Update an existing scenario."""
        payload: dict[str, Any] = {}
        if name is not None:
            payload["scenario_name"] = name
        if description is not None:
            payload["scenario_description"] = description
        if subtitle is not None:
            payload["scenario_subtitle"] = subtitle
        if category is not None:
            payload["scenario_category"] = category
        if main_focus is not None:
            payload["scenario_main_focus"] = main_focus
        if severity is not None:
            payload["scenario_severity"] = severity
        if not payload:
            return "No fields to update."
        async with tool_client(hdrs, timeout=30) as client:
            # The PUT is a full-entity update (UpdateScenarioInput copied onto
            # the scenario): a missing name is a 400 and the other missing
            # fields are wiped, tags included (only reply-to is kept). Send the
            # stored fields with the changes laid over them.
            resp, err = await scoped_safe_request(
                client,
                "get",
                f"{base_url}/api/scenarios/{scenario_id}",
                _PROVIDER,
                "fetch scenario",
                headers=hdrs,
            )
            if err:
                return err
            current = _require_response(resp).json()
            if not isinstance(current, dict) or not current:
                return f"Error: scenario {scenario_id} was not found."
            body = {
                field: current[field]
                for field in _SCENARIO_UPDATE_FIELDS
                if current.get(field) is not None
            }
            body.update(payload)
            resp, err = await scoped_safe_request(
                client,
                "put",
                f"{base_url}/api/scenarios/{scenario_id}",
                _PROVIDER,
                "update scenario",
                headers=hdrs,
                json=body,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        return f"Scenario {scenario_id} updated.\n" + _fmt_scenario(data, base_url, await ui_tenant_id(hdrs))

    # ── 14. Delete scenario ───────────────────────────────────────────

    async def delete_openaev_scenario(scenario_id: str) -> str:
        """Delete a scenario (permanent); OpenAEV keeps its findings as history (OpenAEV #9233)."""
        async with tool_client(hdrs, timeout=30) as client:
            _, err = await scoped_safe_request(
                client,
                "delete",
                f"{base_url}/api/scenarios/{scenario_id}",
                _PROVIDER,
                "delete scenario",
                headers=hdrs,
            )
        if err:
            return err
        return f"Scenario {scenario_id} deleted. {FINDINGS_KEPT_AS_HISTORY}"

    # ── 15. Create payload ────────────────────────────────────────────

    async def create_openaev_payload(
        payload_type: str,
        name: str,
        platforms: list[str],
        domain_ids: list[str] | None = None,
        description: str = "",
        command_executor: str = "",
        command_content: str = "",
        dns_resolution_hostname: str = "",
        attack_pattern_ids: list[str] | None = None,
        tag_ids: list[str] | None = None,
        cleanup_executor: str = "",
        cleanup_command: str = "",
        expectations: list[str] | None = None,
    ) -> str:
        """Create a payload (Command, DnsResolution, Executable, FileDrop, NetworkTraffic)."""
        # Expectations decide what the payload MEASURES once executed, so they
        # are a real parameter rather than an invisible constant. The historical
        # default (prevention + detection) is kept for callers that omit them.
        resolved_expectations = ["PREVENTION", "DETECTION"]
        if expectations is not None:
            resolved_expectations, err = _resolve_expectation_types(expectations)
            if err:
                return err
            if not resolved_expectations:
                return (
                    "expectations cannot be an empty list: a payload with no "
                    "expectations executes but measures nothing, so it proves "
                    "nothing about the security controls. Omit the parameter to "
                    "use the PREVENTION + DETECTION default."
                )
        body: dict[str, Any] = {
            "payload_type": payload_type,
            "payload_name": name,
            "payload_description": description,
            "payload_platforms": platforms,
            "payload_source": "MANUAL",
            "payload_status": "VERIFIED",
            "payload_execution_arch": "ALL_ARCHITECTURES",
            "payload_expectations": resolved_expectations,
            "payload_attack_patterns": attack_pattern_ids or [],
            "payload_domains": domain_ids or [],
            "payload_tags": tag_ids or [],
        }
        if payload_type == "Command":
            body["command_executor"] = command_executor
            body["command_content"] = command_content
        if payload_type == "DnsResolution":
            body["dns_resolution_hostname"] = dns_resolution_hostname
        if cleanup_executor:
            body["payload_cleanup_executor"] = cleanup_executor
        if cleanup_command:
            body["payload_cleanup_command"] = cleanup_command
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "post",
                f"{base_url}/api/payloads",
                _PROVIDER,
                "create payload",
                headers=hdrs,
                json=body,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        pid = data.get("payload_id", "unknown")

        contract_id = "unknown"
        try:
            async with tool_client(hdrs, timeout=15) as c2:
                for _attempt in range(5):
                    await asyncio.sleep(1.0 + _attempt * 1.5)
                    cr, _ = await scoped_safe_request(
                        c2,
                        "post",
                        f"{base_url}/api/injector_contracts/search",
                        _PROVIDER,
                        "find injector contract for payload",
                        headers=hdrs,
                        json=_search_body(size=10, text_search=name),
                    )
                    if cr:
                        for item in cr.json().get("content", []):
                            p = item.get("injector_contract_payload")
                            if isinstance(p, dict) and p.get("payload_id") == pid:
                                contract_id = item.get(
                                    "injector_contract_id", "unknown"
                                )
                                break
                    if contract_id != "unknown":
                        break
        except Exception:
            message = "Failed to resolve injector contract for payload %s after creation"
            logger.warning(message, pid, exc_info=True)

        if contract_id == "unknown":
            return (
                f"Payload created: [{data.get('payload_name', name)}] "
                f"(ID: {pid}, Type: {payload_type}). "
                f"WARNING: The injector contract could not be resolved yet. "
                f"Use search_openaev_injector_contracts to find it by name "
                f"before creating injects. Do NOT fabricate contract IDs."
            )
        return (
            f"Payload created: [{data.get('payload_name', name)}] "
            f"(ID: {pid}, Type: {payload_type}, "
            f"InjectorContract: {contract_id})"
        )

    # ── 16. Get payload ───────────────────────────────────────────────

    async def get_openaev_payload(payload_id: str) -> str:
        """Get full details of a payload by ID."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "get",
                f"{base_url}/api/payloads/{payload_id}",
                _PROVIDER,
                "fetch payload",
                headers=hdrs,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        return _fmt_payload(data, base_url) if data else f"Payload '{payload_id}' not found."

    # ── 17. Search payloads ───────────────────────────────────────────

    async def search_openaev_payloads(search: str = "", limit: int = 20) -> str:
        """Search for payloads or list all."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "post",
                f"{base_url}/api/payloads/search",
                _PROVIDER,
                "search payloads",
                headers=hdrs,
                json=_search_body(size=min(limit, 50), text_search=search),
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        items = data.get("content", [])
        return format_entity_list(
            items,
            partial(_fmt_payload, base_url=base_url),
            noun="payload",
            total=data.get("totalElements"),
            empty_msg=f"No payloads found{' for ' + repr(search) if search else ''}.",
        )

    # ── 18. Add inject to scenario ────────────────────────────────────

    async def _registered_security_platform_types() -> set[str] | None:
        """The security platform types registered in this OpenAEV tenant, or
        None when they cannot be read (the caller then decides nothing)."""
        try:
            resp, err = await _api(
                base_url,
                hdrs,
                "post",
                "/api/security_platforms/search",
                "search security platforms for expectations scope",
                json=_search_body(size=500),
                retry_transient=True,
            )
            if err or resp is None:
                logger.warning(
                    "Could not read the security platforms to scope an inject's expectations (%s); "
                    "the inject keeps the contract's predefined ones",
                    err or "no response",
                )
                return None
            data = resp.json()
        except Exception:
            logger.warning("Failed to read the security platforms", exc_info=True)
            return None
        items = data.get("content") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return None
        return {
            str(p["security_platform_type"]).upper()
            for p in items
            if isinstance(p, dict) and isinstance(p.get("security_platform_type"), str)
        }

    async def _contract_content_json(contract_id: str) -> str | None:
        """The contract's ``injector_contract_content`` JSON, ``None`` when it
        cannot be read (logged, so an inject named with OpenAEV's defaults
        instead of the contract's own names can be traced to it)."""
        try:
            async with tool_client(hdrs, timeout=15) as client:
                resp, err = await scoped_safe_request(
                    client,
                    "get",
                    f"{base_url}/api/injector_contracts/{contract_id}",
                    _PROVIDER,
                    "fetch injector contract expectations",
                    headers=hdrs,
                )
            if err or resp is None:
                logger.warning(
                    "Could not read injector contract %s for its expectations (%s); "
                    "types it offers get OpenAEV's default names",
                    contract_id,
                    err or "no response",
                )
                return None
            content_json = resp.json().get("injector_contract_content")
        except Exception:
            logger.warning(
                "Failed to read contract %s expectations; types it offers get OpenAEV's default names",
                contract_id,
                exc_info=True,
            )
            return None
        return content_json if isinstance(content_json, str) else None

    async def _contract_expectations_default(contract_id: str) -> tuple[list[dict[str, Any]], str]:
        """Expectations to default this contract's inject to, and a line for the
        tool answer (empty when nothing was left out).

        ``_contract_default_expectations`` first; when it keeps the contract's
        predefined set, the predefined PREVENTION / DETECTION expectations scoped
        to security platform types the tenant has not registered are left out
        (#2378): the inject then carries the predefined set minus those.
        Best-effort: when the contract or the platforms cannot be read the inject
        keeps its predefined expectations, and the failure is logged so an
        unscored inject can be traced to it.
        """
        content_json = await _contract_content_json(contract_id)
        if content_json is None:
            return [], ""
        defaults = _contract_default_expectations(content_json)
        if defaults:
            return defaults, ""
        predefined = _contract_predefined_expectations(content_json)
        if not any(
            e.get("expectation_type") in _PLATFORM_SCOPED_EXPECTATION_TYPES
            and e.get("expectation_expected_security_platform_types")
            for e in predefined
        ):
            return [], ""
        registered = await _registered_security_platform_types()
        kept, left_out = _expectations_for_registered_platforms(content_json, registered)
        if not left_out or registered is None:
            return [], ""
        if not kept:
            # OpenAEV seeds the predefined set again when the list is empty, so
            # an inject left with nothing measurable keeps it and says why.
            return [], _left_out_line(left_out, registered, kept=False)
        return kept, _left_out_line(left_out, registered)

    async def add_openaev_inject_to_scenario(
        scenario_id: str,
        title: str,
        injector_contract_id: str,
        description: str = "",
        delay_seconds: int = 0,
        team_ids: list[str] | None = None,
        asset_ids: list[str] | None = None,
        asset_group_ids: list[str] | None = None,
        content: str = "",
        expectations: list[str] | None = None,
    ) -> str:
        """Add an inject (action step) to a scenario."""
        body: dict[str, Any] = {
            "inject_title": title,
            "inject_description": description,
            "inject_injector_contract": injector_contract_id,
            "inject_depends_duration": delay_seconds,
            "inject_teams": team_ids or [],
            "inject_assets": asset_ids or [],
            "inject_asset_groups": asset_group_ids or [],
        }
        inject_content: dict[str, Any]
        expectations_note = ""
        if content:
            try:
                inject_content = json.loads(content)
            except json.JSONDecodeError:
                inject_content = {"content": content}
            if not isinstance(inject_content, dict):
                inject_content = {"content": content}
        else:
            inject_content = {}
        assets, groups, targets_note, targets_err = mirror_content_targets(inject_content, asset_ids, asset_group_ids)
        if targets_err:
            return targets_err
        body["inject_assets"] = assets or []
        body["inject_asset_groups"] = groups or []
        # Expectations decide what the inject MEASURES once executed.
        #
        # * Caller passed types -> they REPLACE the contract's predefined ones
        #   (OpenAEV keeps the contract's only when inject_content has no
        #   non-empty "expectations").
        # * Caller already put expectations in content -> left exactly as given.
        # * Neither -> ``_contract_default_expectations``: a contract that
        #   predefines a security-control expectation keeps it (OpenAEV seeds it
        #   with its platform scope); one that only offers prevention /
        #   detection is scored on them; a human-response contract (email,
        #   article, challenge) keeps its own. The tool is generic (also reached
        #   over /mcp/openaev), so nothing a contract predefines is overwritten.
        if expectations is not None:
            resolved_types, err = _resolve_expectation_types(expectations)
            if err:
                return err
            if not resolved_types:
                return (
                    "expectations cannot be an empty list: pass the types the "
                    "inject is scored on (e.g. [\"PREVENTION\", \"DETECTION\"] for "
                    "a technical step, [\"MANUAL\"] for a human-action step), or "
                    "omit it to keep the injector contract's predefined "
                    "expectations."
                )
            # Each type is the contract's own expectation object, its name as
            # the contract writes it (#2480); a type the contract does not
            # offer gets OpenAEV's default for the type.
            offered = _contract_offered_expectations(await _contract_content_json(injector_contract_id))
            inject_content["expectations"] = _build_inject_expectations(resolved_types, offered)
        elif not inject_content.get("expectations"):
            # An empty list counts as absent, as OpenAEV's setExpectations does.
            default_expectations, expectations_note = await _contract_expectations_default(injector_contract_id)
            if default_expectations:
                inject_content["expectations"] = default_expectations
        body["inject_content"] = inject_content
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "post",
                f"{base_url}/api/scenarios/{scenario_id}/injects",
                _PROVIDER,
                "add inject to scenario",
                headers=hdrs,
                json=body,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        iid = data.get("inject_id", "unknown")
        lines = [f"Inject added to scenario {scenario_id}: [{title}] (Inject ID: {iid})"]
        lines += [t for t in (targets_note, inject_targets_line(data), expectations_note) if t]
        return "\n".join(lines)

    # ── 19. Search injector contracts ─────────────────────────────────

    async def search_openaev_injector_contracts(
        search: str = "", limit: int = 30, output_types: list[str] | None = None
    ) -> str:
        """Search injector contracts (available inject types/payloads).

        ``output_types`` selects contracts that already PRODUCE those findings
        (reuse). The tool also returns payload-based contracts matching the
        text search that are NOT in that producing set (fork candidates).
        Native injectors without a payload are not added as fork candidates.
        """
        filter_group = None
        resolved: list[str] = []
        if output_types:
            resolved, resolve_err = _resolve_output_types(output_types)
            if resolve_err:
                return resolve_err
            if resolved:
                filter_group = {
                    "mode": "and",
                    "filters": [_providing_filter_clause(resolved)],
                }
        size = min(limit, 100)
        # Route through _api so a long autonomous run keeps its JWT fresh (WS2
        # AutoRefreshClient) instead of 401-ing ~10 min in; idempotent reads, so both
        # calls opt into the transient-5xx retry.
        resp, err = await _api(
            base_url,
            hdrs,
            "post",
            "/api/injector_contracts/search",
            "search injector contracts",
            json=_search_body(
                size=size, text_search=search, filter_group=filter_group
            ),
            retry_transient=True,
        )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        items = data.get("content", [])
        if not isinstance(items, list):
            items = []
        if resolved:
            resp2, err2 = await _api(
                base_url,
                hdrs,
                "post",
                "/api/injector_contracts/search",
                "search injector contracts (fork candidates)",
                json=_search_body(size=size, text_search=search, filter_group=None),
                retry_transient=True,
            )
            if not err2:
                resp2 = _require_response(resp2)
                extra = resp2.json().get("content", [])
                if not isinstance(extra, list):
                    extra = []
                items = _merge_fork_candidates(items, extra)
        items = items[:size]
        # Describe the scope with the CANONICAL tokens actually sent in the
        # filter (aliases resolved, casing/whitespace normalized), never the
        # raw input - the diagnostic must reflect the query that ran.
        scope = repr(search) if search else ""
        if resolved:
            scope = (
                f"{scope} producing {', '.join(resolved)}"
                if scope
                else f"producing {', '.join(resolved)}"
            )
        return format_entity_list(
            items,
            _fmt_injector_contract,
            noun="injector contract",
            total=data.get("totalElements") if not resolved else len(items),
            empty_msg=f"No injector contracts found{' for ' + scope if scope else ''}.",
        )

    # ── 20. Get injector contract ─────────────────────────────────────

    async def get_openaev_injector_contract(contract_id: str) -> str:
        """Get full details of an injector contract by ID."""
        # Route through _api for the WS2 JWT auto-refresh on a long run; idempotent GET.
        resp, err = await _api(
            base_url,
            hdrs,
            "get",
            f"/api/injector_contracts/{contract_id}",
            "fetch injector contract",
            retry_transient=True,
        )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        return (
            _fmt_injector_contract(data)
            if data
            else f"Contract '{contract_id}' not found."
        )

    # ── 21. Create atomic testing ─────────────────────────────────────

    async def create_openaev_atomic_testing(
        title: str,
        injector_contract_id: str,
        description: str = "",
        asset_ids: list[str] | None = None,
        asset_group_ids: list[str] | None = None,
        content: str = "",
    ) -> str:
        """Create an atomic test (one-off inject execution against assets)."""
        body: dict[str, Any] = {
            "inject_title": title,
            "inject_description": description,
            "inject_injector_contract": injector_contract_id,
            "inject_assets": asset_ids or [],
            "inject_asset_groups": asset_group_ids or [],
            "inject_teams": [],
            "inject_all_teams": False,
        }
        if content:
            try:
                body["inject_content"] = json.loads(content)
            except json.JSONDecodeError:
                body["inject_content"] = {"content": content}
        else:
            body["inject_content"] = {}
        assets, groups, targets_note, targets_err = mirror_content_targets(
            body["inject_content"], asset_ids, asset_group_ids
        )
        if targets_err:
            return targets_err
        body["inject_assets"] = assets or []
        body["inject_asset_groups"] = groups or []
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "post",
                f"{base_url}/api/atomic-testings",
                _PROVIDER,
                "create atomic test",
                headers=hdrs,
                json=body,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        if not isinstance(data, dict):
            data = {}
        aid = data.get("inject_id", "unknown")
        lines = [
            f"Atomic test created: [{title}] (ID: {aid})",
            f"URL: {base_url}/admin/atomic_testings/{aid}",
        ]
        # The atomic-testing answer (InjectResultOverviewOutput) carries no
        # relations: read them from the inject itself (a read, never a write).
        stored: Any = None
        if data.get("inject_id"):
            read, read_err = await _api(
                base_url,
                hdrs,
                "get",
                f"/api/injects/{data['inject_id']}",
                "read the stored targets of the atomic test",
                retry_transient=True,
            )
            if not read_err and read is not None:
                try:
                    stored = read.json()
                except ValueError:
                    stored = None
        targets = stored_targets_line(data, stored, body["inject_assets"], body["inject_asset_groups"])
        lines += [t for t in (targets_note, targets) if t]
        return "\n".join(lines)

    # ── 22. Search findings ───────────────────────────────────────────

    async def search_openaev_findings(
        search: str = "",
        limit: int = 20,
        simulation_id: str = "",
        inject_id: str = "",
    ) -> str:
        """Search for security findings or list all.

        Pass ``simulation_id`` to scope the search to ONE run's simulation: it goes
        to OpenAEV's per-simulation route (``/api/findings/exercises/{id}/search``),
        whose distinct search stays inside that simulation, so a text search does
        not scan every occurrence of the value across the tenant (#2594). On a busy
        tenant an UNSCOPED
        search can surface, PROOF-cite, or promote ANOTHER run's finding, so the
        autonomous orchestrator should always pass its run's simulation id when it
        verifies what a step produced.

        Pass ``inject_id`` to read the findings ONE inject (or atomic testing)
        produced: the search goes to OpenAEV's per-inject route
        (``/api/findings/injects/{id}/search``), which checks the inject exists
        in scope, so an unknown id answers not found instead of an empty list.
        """
        simulation_id = (simulation_id or "").strip()
        inject_id = (inject_id or "").strip()
        filter_group = None
        # Inject route: the simulation stays a body filter on top of it. Simulation
        # alone: the per-simulation route scopes the search itself (#2594).
        if simulation_id and inject_id:
            filter_group = {
                "mode": "and",
                "filters": [
                    {
                        "key": "finding_simulation",
                        "operator": "eq",
                        "values": [simulation_id],
                    }
                ],
            }
        # Route through _api so a long autonomous run keeps its JWT fresh (WS2); the
        # distinct flag rides as a query param and the idempotent read opts into the
        # transient-5xx retry.
        if inject_id:
            path = f"/api/findings/injects/{quote(inject_id, safe='')}/search"
            action, used = "search findings of inject", f"inject {inject_id}"
            narrower = "a more specific search text or a lower limit"
        elif simulation_id:
            path = f"/api/findings/exercises/{quote(simulation_id, safe='')}/search"
            action, used = "search findings of simulation", f"simulation {simulation_id}"
            narrower = "inject_id"
        else:
            path = "/api/findings/search"
            action, used = "search findings", "the whole tenant"
            narrower = "simulation_id or inject_id"
        resp, err = await _api(
            base_url,
            hdrs,
            "post",
            path,
            action,
            params={"distinct": "true"},
            json=_search_body(
                size=min(limit, 50), text_search=search, filter_group=filter_group
            ),
            retry_transient=True,
        )
        if err:
            return _findings_timeout_hint(err, used, narrower)
        resp = _require_response(resp)
        data = resp.json()
        items = data.get("content", [])
        scope = repr(search) if search else ""
        if inject_id:
            scope = f"{scope} of inject {inject_id}" if scope else f"inject {inject_id}"
        if simulation_id:
            scope = (
                f"{scope} in simulation {simulation_id}"
                if scope
                else f"in simulation {simulation_id}"
            )
        empty = f"No findings found{' for ' + scope if scope else ''}"
        if not items:
            # OpenAEV answers an empty page to an identity without ACCESS_FINDINGS (#1444).
            read, cause = await area_scope(hdrs, "findings")
            if read != "full":
                return with_scope(f"{empty}: {count_text(0, read)}.", read, cause)
        return format_entity_list(
            items,
            partial(_fmt_finding, base_url=base_url),
            noun="finding",
            total=data.get("totalElements"),
            empty_msg=f"{empty}.",
        )

    # ── 23. Get vulnerability by CVE ──────────────────────────────────

    async def get_openaev_vulnerability(cve_id: str) -> str:
        """Get a vulnerability by its CVE ID (e.g., CVE-2024-1234)."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "get",
                f"{base_url}/api/vulnerabilities/external-id/{cve_id}",
                _PROVIDER,
                "fetch vulnerability",
                headers=hdrs,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        return (
            _fmt_vulnerability(data) if data else f"Vulnerability '{cve_id}' not found."
        )

    # ── 24. Full text search ──────────────────────────────────────────

    async def full_text_search_openaev(search: str) -> str:
        """Full-text search across all OpenAEV entity types. Returns counts per type."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "post",
                f"{base_url}/api/fulltextsearch",
                _PROVIDER,
                "full-text search",
                headers=hdrs,
                json={"searchTerm": search},
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        if not data:
            return f"No results for '{search}'."
        lines = [f"Full-text search results for '{search}':"]
        for entity_type, info in sorted(data.items()):
            count = info.get("count", 0) if isinstance(info, dict) else info
            if count > 0:
                lines.append(f"  {entity_type}: {count} results")
        if len(lines) == 1:
            return f"No results for '{search}'."
        return "\n".join(lines)

    # ── 25. List kill chain phases ────────────────────────────────────

    async def list_openaev_kill_chain_phases() -> str:
        """List all kill chain phases available in OpenAEV."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "get",
                f"{base_url}/api/kill_chain_phases",
                _PROVIDER,
                "list kill chain phases",
                headers=hdrs,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        if not data:
            return "No kill chain phases found."
        lines = ["Kill chain phases:"]
        for phase in data:
            lines.append(
                f"  [{phase.get('phase_kill_chain_name', '?')}] "
                f"{phase.get('phase_name', '?')} "
                f"(order: {phase.get('phase_order', '?')}, "
                f"ID: {phase.get('phase_id', 'N/A')})"
            )
        return "\n".join(lines)

    # ── 26. List tags ─────────────────────────────────────────────────

    async def list_openaev_tags() -> str:
        """List all tags available in OpenAEV."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "get",
                f"{base_url}/api/tags",
                _PROVIDER,
                "list tags",
                headers=hdrs,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        if not data:
            return "No tags found."
        lines = ["Tags:"]
        for tag in data:
            lines.append(
                f"  {tag.get('tag_name', '?')} (ID: {tag.get('tag_id', 'N/A')})"
            )
        return "\n".join(lines)

    # ── 27. List domains ────────────────────────────────────────────

    async def list_openaev_domains() -> str:
        """List all domains (security domains) available in OpenAEV."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "get",
                f"{base_url}/api/domains",
                _PROVIDER,
                "list domains",
                headers=hdrs,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        if not data:
            return "No domains found."
        lines = ["Domains:"]
        for domain in data:
            lines.append(
                f"  {domain.get('domain_name', '?')} "
                f"(color: {domain.get('domain_color', '?')}, "
                f"ID: {domain.get('domain_id', 'N/A')})"
            )
        return "\n".join(lines)

    # ── 28. Search exercises/simulations ──────────────────────────────

    def _fmt_exercise(ex: dict) -> str:
        name = ex.get("exercise_name", "?")
        eid = ex.get("exercise_id", "N/A")
        desc = (ex.get("exercise_description") or "")[:200]
        line = f"[Exercise] {name}\n  ID: {eid}"
        # The closure already has ``base_url`` from ``build_tools`` —
        # use the same ``_aev_link`` helper as the module-level
        # formatters so the link gates on empty base_url consistently.
        link = _aev_link(base_url, "exercise", eid)
        if link:
            line += "\n" + link
        if desc:
            line += f"\n  Description: {desc}"
        if ex.get("exercise_status"):
            line += f"\n  Status: {ex['exercise_status']}"
        for state_line in _simulation_state_lines(ex):
            line += "\n" + state_line
        return line

    async def search_openaev_exercises(search: str = "", limit: int = 20) -> str:
        """Search for exercises (simulations) or list all."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "post",
                f"{base_url}/api/exercises/search",
                _PROVIDER,
                "search exercises",
                headers=hdrs,
                json=_search_body(size=min(limit, 50), text_search=search),
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        items = data.get("content", [])
        return format_entity_list(
            items,
            _fmt_exercise,
            noun="exercise",
            total=data.get("totalElements"),
            empty_msg=f"No exercises found{' for ' + repr(search) if search else ''}.",
        )

    # ── 28. Get exercise ──────────────────────────────────────────────

    async def get_openaev_exercise(exercise_id: str) -> str:
        """Get full details of a specific exercise/simulation by ID."""
        async with tool_client(hdrs, timeout=30) as client:
            resp, err = await scoped_safe_request(
                client,
                "get",
                f"{base_url}/api/exercises/{exercise_id}",
                _PROVIDER,
                "fetch exercise",
                headers=hdrs,
            )
        if err:
            return err
        resp = _require_response(resp)
        data = resp.json()
        if not data:
            return f"Exercise '{exercise_id}' not found."
        name = data.get("exercise_name", "?")
        eid = data.get("exercise_id", "N/A")
        lines = [f"[Exercise] {name}", f"  ID: {eid}"]
        link = _aev_link(base_url, "exercise", eid)
        if link:
            lines.append(link)
        if data.get("exercise_description"):
            lines.append(f"  Description: {data['exercise_description'][:400]}")
        if data.get("exercise_status"):
            lines.append(f"  Status: {data['exercise_status']}")
        if data.get("exercise_subtitle"):
            lines.append(f"  Subtitle: {data['exercise_subtitle']}")
        lines.extend(_simulation_state_lines(data))
        return "\n".join(lines)

    # ── Register all tools ────────────────────────────────────────────

    executor.register("search_openaev_assets", search_openaev_assets)
    executor.register("get_openaev_asset", get_openaev_asset)
    executor.register("search_openaev_asset_groups", search_openaev_asset_groups)
    executor.register("get_openaev_asset_group", get_openaev_asset_group)
    executor.register("search_openaev_teams", search_openaev_teams)
    executor.register("get_openaev_team", get_openaev_team)
    executor.register("search_openaev_players", search_openaev_players)
    executor.register("search_openaev_attack_patterns", search_openaev_attack_patterns)
    executor.register("get_openaev_attack_pattern", get_openaev_attack_pattern)
    executor.register("search_openaev_scenarios", search_openaev_scenarios)
    executor.register("get_openaev_scenario", get_openaev_scenario)
    executor.register("create_openaev_scenario", create_openaev_scenario)
    executor.register("update_openaev_scenario", update_openaev_scenario)
    executor.register("delete_openaev_scenario", delete_openaev_scenario)
    executor.register("create_openaev_payload", create_openaev_payload)
    executor.register("get_openaev_payload", get_openaev_payload)
    executor.register("search_openaev_payloads", search_openaev_payloads)
    executor.register("add_openaev_inject_to_scenario", add_openaev_inject_to_scenario)
    executor.register(
        "search_openaev_injector_contracts", search_openaev_injector_contracts
    )
    executor.register("get_openaev_injector_contract", get_openaev_injector_contract)
    executor.register("create_openaev_atomic_testing", create_openaev_atomic_testing)
    executor.register("search_openaev_findings", search_openaev_findings)
    executor.register("get_openaev_vulnerability", get_openaev_vulnerability)
    executor.register("full_text_search_openaev", full_text_search_openaev)
    executor.register("list_openaev_kill_chain_phases", list_openaev_kill_chain_phases)
    executor.register("list_openaev_tags", list_openaev_tags)
    executor.register("list_openaev_domains", list_openaev_domains)
    executor.register("search_openaev_exercises", search_openaev_exercises)
    executor.register("get_openaev_exercise", get_openaev_exercise)

    # ── Tool definitions ──────────────────────────────────────────────

    return [
        # 1. Search assets
        ToolDefinition(
            name="search_openaev_assets",
            priority=0,
            description=(
                "Search for assets (endpoints) in OpenAEV by name, hostname, or IP address, "
                "or count them by model keys (asset_status, asset_category...) with filter_group. "
                "The answer always carries the total."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Search term (name, hostname, or IP)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20); 1 to count",
                        "default": 20,
                    },
                    "filter_group": {
                        "type": "string",
                        "description": (
                            'FilterGroup JSON on Asset model keys, e.g. {"mode":"and","filters":'
                            '[{"key":"asset_status","operator":"eq","values":["ACTIVE"]}]}'
                        ),
                    },
                },
                "required": [],
            },
            source="integration:openaev",
        ),
        # 2. Get asset
        ToolDefinition(
            name="get_openaev_asset",
            priority=0,
            description="Get full details of a specific asset (endpoint) by its OpenAEV ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "The asset's OpenAEV ID",
                    }
                },
                "required": ["asset_id"],
            },
            source="integration:openaev",
        ),
        # 3. Search asset groups
        ToolDefinition(
            name="search_openaev_asset_groups",
            description="Search for asset groups in OpenAEV by name. Asset groups are collections of assets.",
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Search term (group name)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["search"],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 4. Get asset group
        ToolDefinition(
            name="get_openaev_asset_group",
            description=(
                "Get an asset group by ID: its dynamic filter, tags and its "
                "members, static and dynamic, paged (name, id, type, hostname)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_id": {
                        "type": "string",
                        "description": "The OpenAEV asset group ID (list_asset_groups: a row's source_id)",
                    },
                    "members_limit": {
                        "type": "integer",
                        "description": "Members per page, 0 to 100 (default 20; 0: none)",
                    },
                    "members_page": {
                        "type": "integer",
                        "description": "Members page, from 0",
                    },
                    "member_search": {
                        "type": "string",
                        "description": "Only members whose name contains it",
                    },
                },
                "required": ["group_id"],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 5. Search teams
        ToolDefinition(
            name="search_openaev_teams",
            description="Search for teams in OpenAEV by name. Teams group players for exercises and scenarios.",
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Search term (team name)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["search"],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 6. Get team
        ToolDefinition(
            name="get_openaev_team",
            description="Get full details of a specific team by ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {"type": "string", "description": "The team's ID"}
                },
                "required": ["team_id"],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 7. Search players
        ToolDefinition(
            name="search_openaev_players",
            description="Search for players (users/people) in OpenAEV by email, first name, or last name.",
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Search term (email, first name, last name)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["search"],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 8. Search attack patterns
        ToolDefinition(
            name="search_openaev_attack_patterns",
            description=(
                "Search MITRE ATT&CK attack patterns in OpenAEV. "
                "Returns technique IDs, names, descriptions, and platforms."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": (
                            "Search term (technique name or ID like T1059). "
                            "Alias: 'query'."
                        ),
                        "default": "",
                    },
                    "query": {
                        "type": "string",
                        "description": "Alias for 'search'.",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": [],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 9. Get attack pattern
        ToolDefinition(
            name="get_openaev_attack_pattern",
            description="Get full details of a specific MITRE ATT&CK attack pattern by ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "attack_pattern_id": {
                        "type": "string",
                        "description": "The attack pattern's OpenAEV ID",
                    }
                },
                "required": ["attack_pattern_id"],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 10. Search scenarios
        ToolDefinition(
            name="search_openaev_scenarios",
            priority=0,
            description="Search for adversary simulation scenarios in OpenAEV or list all scenarios.",
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Search term (optional)",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": [],
            },
            source="integration:openaev",
        ),
        # 11. Get scenario
        ToolDefinition(
            name="get_openaev_scenario",
            priority=0,
            description="Get full details of a specific scenario by ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "scenario_id": {
                        "type": "string",
                        "description": "The scenario's OpenAEV ID",
                    }
                },
                "required": ["scenario_id"],
            },
            source="integration:openaev",
        ),
        # 12. Create scenario
        ToolDefinition(
            name="create_openaev_scenario",
            priority=0,
            description=(
                "Create a new adversary simulation scenario in OpenAEV. "
                "A scenario defines an attack storyline with injects (steps), targets, and teams."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Scenario name"},
                    "description": {
                        "type": "string",
                        "description": "Detailed description",
                        "default": "",
                    },
                    "subtitle": {
                        "type": "string",
                        "description": "Short subtitle",
                        "default": "",
                    },
                    "category": {
                        "type": "string",
                        "description": f"Category: {', '.join(SCENARIO_CATEGORIES)}",
                        "default": "attack-scenario",
                    },
                    "main_focus": {
                        "type": "string",
                        "description": f"Main focus: {', '.join(SCENARIO_MAIN_FOCUS)}",
                        "default": "incident-response",
                    },
                    "severity": {
                        "type": "string",
                        "enum": SCENARIO_SEVERITIES,
                        "description": "Severity level",
                        "default": "high",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tag IDs to apply",
                    },
                },
                "required": ["name"],
            },
            source="integration:openaev",
        ),
        # 13. Update scenario
        ToolDefinition(
            name="update_openaev_scenario",
            description="Update an existing scenario (name, description, category, severity, etc.).",
            input_schema={
                "type": "object",
                "properties": {
                    "scenario_id": {
                        "type": "string",
                        "description": "Scenario ID to update",
                    },
                    "name": {"type": "string", "description": "New name (optional)"},
                    "description": {
                        "type": "string",
                        "description": "New description (optional)",
                    },
                    "subtitle": {
                        "type": "string",
                        "description": "New subtitle (optional)",
                    },
                    "category": {
                        "type": "string",
                        "description": "New category (optional)",
                    },
                    "main_focus": {
                        "type": "string",
                        "description": "New main focus (optional)",
                    },
                    "severity": {
                        "type": "string",
                        "enum": SCENARIO_SEVERITIES,
                        "description": "New severity (optional)",
                    },
                },
                "required": ["scenario_id"],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 14. Delete scenario
        ToolDefinition(
            name="delete_openaev_scenario",
            description="Delete a scenario from OpenAEV. This is permanent.",
            input_schema={
                "type": "object",
                "properties": {
                    "scenario_id": {
                        "type": "string",
                        "description": "Scenario ID to delete",
                    },
                },
                "required": ["scenario_id"],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 15. Create payload
        ToolDefinition(
            name="create_openaev_payload",
            description=(
                "Create a payload for use in injects and atomic tests. "
                "Payload types: Command (shell command), DnsResolution (DNS lookup), "
                "Executable, FileDrop, NetworkTraffic. The 'expectations' parameter "
                "declares what the payload MEASURES once executed and defaults to "
                "PREVENTION + DETECTION. For authoring new attack content prefer "
                "manage_openaev_threat_arsenal, the v3 surface that manages the "
                "payload and its injector contract together."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "payload_type": {
                        "type": "string",
                        "enum": PAYLOAD_TYPES,
                        "description": "Payload type",
                    },
                    "name": {"type": "string", "description": "Payload name"},
                    "platforms": {
                        "type": "array",
                        "items": {"type": "string", "enum": PLATFORMS},
                        "description": "Target platforms",
                    },
                    "domain_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Domain IDs (security domains). Use list_openaev_domains to get available IDs. Required by OpenAEV.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Payload description",
                        "default": "",
                    },
                    "command_executor": {
                        "type": "string",
                        "enum": COMMAND_EXECUTORS,
                        "description": "Command executor (for Command type): psh, cmd, bash, sh",
                    },
                    "command_content": {
                        "type": "string",
                        "description": "Shell command to execute (for Command type)",
                    },
                    "dns_resolution_hostname": {
                        "type": "string",
                        "description": "Hostname to resolve (for DnsResolution type)",
                    },
                    "attack_pattern_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Attack pattern IDs to link",
                    },
                    "tag_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tag IDs to attach. Use list_openaev_tags to get available IDs.",
                    },
                    "cleanup_executor": {
                        "type": "string",
                        "description": "Cleanup command executor (optional)",
                    },
                    "cleanup_command": {
                        "type": "string",
                        "description": "Cleanup command to run after payload (optional)",
                    },
                    "expectations": {
                        "type": "array",
                        "items": {"type": "string", "enum": EXPECTATION_TYPES},
                        "description": (
                            "What executing this payload MEASURES. PREVENTION = a "
                            "control should block it; DETECTION = a control should "
                            "alert on it; VULNERABILITY = it proves a CVE is "
                            "present (inverted polarity - success means NOT "
                            "vulnerable). Defaults to PREVENTION + DETECTION when "
                            "omitted. Never pass an empty list: a payload with no "
                            "expectations still runs but measures nothing."
                        ),
                    },
                },
                "required": ["payload_type", "name", "platforms", "domain_ids"],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 16. Get payload
        ToolDefinition(
            name="get_openaev_payload",
            description=(
                "Get full details of a payload by ID. Reads the legacy (deprecated) "
                "payload record; prefer get_openaev_injector_contract / "
                "search_openaev_injector_contracts (which already carry the payload) or "
                "manage_openaev_threat_arsenal for current v3 content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "payload_id": {"type": "string", "description": "Payload ID"}
                },
                "required": ["payload_id"],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 17. Search payloads
        ToolDefinition(
            name="search_openaev_payloads",
            description=(
                "Search for payloads in OpenAEV or list all. Reads the legacy (deprecated) "
                "payload surface; prefer search_openaev_injector_contracts (which already "
                "carries the payload) or manage_openaev_threat_arsenal for current v3 content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Search term (optional)",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": [],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 18. Add inject to scenario
        ToolDefinition(
            name="add_openaev_inject_to_scenario",
            description=(
                "Add an inject (action step) to a scenario. Each inject represents one step "
                "in the attack simulation (e.g., phishing email, lateral movement, data exfiltration). "
                "Requires an injector contract ID that defines the inject type. Pass "
                "'expectations' to score a technical step against the security stack; omit it to "
                "keep the injector contract's predefined expectations."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scenario_id": {
                        "type": "string",
                        "description": "Target scenario ID",
                    },
                    "title": {"type": "string", "description": "Inject title"},
                    "injector_contract_id": {
                        "type": "string",
                        "description": "Injector contract ID defining the inject type",
                    },
                    "description": {
                        "type": "string",
                        "description": "Inject description",
                        "default": "",
                    },
                    "delay_seconds": {
                        "type": "integer",
                        "description": "Delay before execution in seconds (0 = immediate)",
                        "default": 0,
                    },
                    "team_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Target team IDs",
                    },
                    "asset_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Target asset IDs",
                    },
                    "asset_group_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Target asset group IDs",
                    },
                    "content": {
                        "type": "string",
                        "description": "Inject content as JSON string (contract-specific fields)",
                    },
                    "expectations": {
                        "type": "array",
                        "items": {"type": "string", "enum": EXPECTATION_TYPES},
                        "description": (
                            "Expectation TYPES this inject is scored on, written into "
                            "inject_content.expectations, e.g. ['VULNERABILITY'] for a "
                            "scan step or ['MANUAL'] for a human-action step. When set, "
                            "these REPLACE the injector contract's predefined "
                            "expectations, including a payload's own platform scope, so "
                            "omit it unless you mean to override them. OMITTED, a contract "
                            "that predefines security-control expectations keeps them, one "
                            "that only offers prevention/detection is scored on them, and "
                            "a human-response contract (email, article, challenge) keeps "
                            "its own; any expectations already in 'content' are kept. An empty "
                            "list is rejected."
                        ),
                    },
                },
                "required": ["scenario_id", "title", "injector_contract_id"],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 19. Search injector contracts
        ToolDefinition(
            name="search_openaev_injector_contracts",
            priority=2,
            description=(
                "Search for injector contracts in OpenAEV. Injector contracts define "
                "available inject types (email, command execution, HTTP request, etc.). "
                "Use this to find the right contract ID before adding injects to scenarios. "
                "Pass output_types to prefer contracts that already PRODUCE those finding "
                "types (reuse). Payload-based contracts matching the text search that "
                "produce none are also returned as FORK CANDIDATES. Native injectors "
                "without a payload are not added as fork candidates. Each returned "
                "contract lists Kind and what it 'Produces findings:' (or 'none')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Search term (optional)",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 30)",
                        "default": 30,
                    },
                    "output_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Prefer contracts producing at least one of these finding "
                            "types (any-of). Payload-based matches that produce none are "
                            "still returned as fork candidates. Allowed: "
                            f"{', '.join(CONTRACT_OUTPUT_TYPES)}."
                        ),
                    },
                },
                "required": [],
            },
            source="integration:openaev",
        ),
        # 20. Get injector contract
        ToolDefinition(
            name="get_openaev_injector_contract",
            priority=2,
            description=(
                "Get full details of an injector contract by ID, with its input "
                "fields (inject_content keys, types, mandatory flags, defaults, "
                "choices, linked fields and visibility conditions): build inject "
                "content from them, never from the injector's source code."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "contract_id": {
                        "type": "string",
                        "description": "Injector contract ID",
                    }
                },
                "required": ["contract_id"],
            },
            source="integration:openaev",
        ),
        # 21. Create atomic testing
        ToolDefinition(
            name="create_openaev_atomic_testing",
            description=(
                "Create an atomic test in OpenAEV. Atomic tests execute a single inject "
                "against specific assets to validate detection/prevention capabilities. "
                "Requires an injector contract ID."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Atomic test title"},
                    "injector_contract_id": {
                        "type": "string",
                        "description": "Injector contract ID",
                    },
                    "description": {
                        "type": "string",
                        "description": "Test description",
                        "default": "",
                    },
                    "asset_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Target asset IDs",
                    },
                    "asset_group_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Target asset group IDs",
                    },
                    "content": {
                        "type": "string",
                        "description": "Test content as JSON string (contract-specific)",
                    },
                },
                "required": ["title", "injector_contract_id"],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 22. Search findings
        ToolDefinition(
            name="search_openaev_findings",
            priority=0,
            description=(
                "Search for security findings in OpenAEV (detected issues on assets - "
                "credentials, open ports, CVEs, kerberoastable accounts, shares...). Pass "
                "simulation_id to scope the search to ONE run's simulation: on a busy "
                "tenant an unscoped search can surface, PROOF-cite, or promote ANOTHER "
                "run's finding, so on an autonomous run ALWAYS pass the run's simulation "
                "id when verifying what a step produced. Pass inject_id to read the "
                "findings one inject or atomic testing produced (an unknown inject id "
                "answers not found)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Search term (optional)",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                    "simulation_id": {
                        "type": "string",
                        "description": (
                            "Optional simulation id to scope findings to a single run "
                            "(the run's simulation id from the cycle context). Strongly "
                            "recommended on an autonomous run to avoid seeing another "
                            "run's findings."
                        ),
                        "default": "",
                    },
                    "inject_id": {
                        "type": "string",
                        "description": (
                            "Optional inject or atomic testing id: only the findings that inject produced."
                        ),
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:openaev",
        ),
        # 23. Get vulnerability by CVE
        ToolDefinition(
            name="get_openaev_vulnerability",
            description="Look up a vulnerability in OpenAEV by its CVE ID (e.g., CVE-2024-1234).",
            input_schema={
                "type": "object",
                "properties": {
                    "cve_id": {
                        "type": "string",
                        "description": "CVE ID (e.g., CVE-2024-1234)",
                    }
                },
                "required": ["cve_id"],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 24. Full text search
        ToolDefinition(
            name="full_text_search_openaev",
            priority=0,
            description=(
                "Full-text search across all OpenAEV entity types. "
                "Returns counts per entity type (assets, teams, scenarios, etc.). "
                "Use this for broad discovery before drilling into specific entity types."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Search term"}
                },
                "required": ["search"],
            },
            source="integration:openaev",
        ),
        # 25. List kill chain phases
        ToolDefinition(
            name="list_openaev_kill_chain_phases",
            priority=2,
            description="List all kill chain phases in OpenAEV (MITRE ATT&CK, Lockheed Martin, etc.).",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:openaev",
        ),
        # 26. List tags
        ToolDefinition(
            name="list_openaev_tags",
            priority=2,
            description="List all tags available in OpenAEV for categorizing entities.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:openaev",
        ),
        # 27. List domains
        ToolDefinition(
            name="list_openaev_domains",
            priority=2,
            description="List all security domains available in OpenAEV. Domain IDs are required when creating payloads.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:openaev",
        ),
        # 28. Search exercises
        ToolDefinition(
            name="search_openaev_exercises",
            description="Search for exercises (simulations) in OpenAEV or list all.",
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Search term (optional)",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": [],
            },
            source="integration:openaev",
            priority=1,
        ),
        # 28. Get exercise
        ToolDefinition(
            name="get_openaev_exercise",
            description="Get full details of a specific exercise/simulation by ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "exercise_id": {"type": "string", "description": "Exercise ID"}
                },
                "required": ["exercise_id"],
            },
            source="integration:openaev",
            priority=1,
        ),
    ]

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

"""Auto-provisioned OpenCTI triggers for ``integration_event`` assignments.

An assignment listening to OpenCTI (``trigger_type == "integration_event"``,
``trigger_config.provider == "opencti"``) that declares
``trigger_config.provision`` gets the two OpenCTI objects that make OpenCTI
call it, created under the integration's own identity:

* a Generic webhook notifier posting to the assignment's receiver
  (``/api/hooks/integrations/{integration_id}/assignments/{assignment_id}``),
  authenticated with the integration's ``webhook_secret`` in
  ``X-OpenCTI-Token``. No secret, no provisioning: an unauthenticated
  receiver is never wired;
* a live knowledge trigger carrying the declared filters (entity types,
  labels, minimum score, event types) and that notifier.

Both are named ``XTM One - <fleet> - <purpose>``. The pass
(:func:`maintain`, run by ``services/trigger_provisioning.py`` on the scheduler tick) is idempotent:
an assignment without state is provisioned, a provisioned one is checked again
every :data:`RECHECK_SECONDS` and repaired when an object was deleted or
edited in OpenCTI, and one that no longer declares ``provision``, is disabled
or deleted (:func:`deprovision`, the delete route) has both objects
removed. The state lives in ``trigger_config.opencti_provisioning`` (ids,
fingerprint, status, error, checked_at): the assignment API returns it with
``trigger_config``, next to ``last_triggered_at`` (the last event that ran).

``provision`` contract::

    {"fleet": "Threat response", "purpose": "New intrusion sets",
     "entity_types": ["Intrusion-Set", "Report"], "labels": ["ai"],
     "min_score": 50, "event_types": ["create"],
     "organization_context": ["pir_ids", "regions", "sector"], "pir_min_score": 1}

Vulnerability signals (CTEM fleet F2): ``cisa_kev`` (true: on the CISA KEV
list, ``x_opencti_cisa_kev``) and ``min_epss_score`` (0 to 1, gte on
``x_opencti_epss_score``); both set, either one matches (one OR group).
``from_types`` keeps a relationship or sighting whose source is of these
types (``fromTypes``; a sighting's ``sighting_of_ref``).

``organization_context`` names the fields of the organization profile
(``services/organization_context.py``, #8987) the trigger derives its filters
from, read again at every check so a profile change repairs the trigger.
``pir_ids`` adds OpenCTI's ``pir_score`` filter on the profile's PIRs (an
entity flagged in one of them with at least ``pir_min_score``); ``pir_ids``
in ``provision`` (OpenCTI ids) keeps a subset of them, an id the profile
does not hold being an error naming it. ``regions``
and ``sector`` resolve to OpenCTI's Country entities (alpha-2 or alpha-3
alias) and Sector entity (name or alias), and add one OR group: a container
holding one of them (``objects``) or a relationship pointing at one
(``toId``). A declared field the profile leaves empty, or a value OpenCTI
holds no entity for, is an error naming it: the trigger is never
provisioned wider than the fleet asked.

``organizations`` (#9157 slice M4, ``dev-docs/ctem-fleets.md`` section 9.4)
names whose events the trigger listens to: ``primary`` (the default,
nothing changes), ``tracked`` (every tracked organization with monitoring
on and an OpenCTI identity) or a list of relationships (``provider``,
``customer``, ``partner``, ``subsidiary``). A tracked scope adds one OR
group: a container holding one of the organizations or their sectors
(``objects``), a relationship or sighting pointing at one (``toId``), or an
entity flagged in their PIRs (``pir_score``). The filter values are the
union of the organizations' ids, so one trigger serves them all. The state
keeps each organization's OpenCTI ids (``organizations``) for the receiver
to resolve which one an event concerns (``services/organization_events.py``).
A tracked scope reads no primary profile field: ``organization_context`` is
refused with it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.country_codes import ISO_3166_ALPHA3_TO_ALPHA2

logger = logging.getLogger(__name__)

PROVISION_KEY = "provision"
STATE_KEY = "opencti_provisioning"
NAME_PREFIX = "XTM One"
#: OpenCTI's built-in Generic webhook notifier connector (``notifier-statics.ts``).
WEBHOOK_CONNECTOR_ID = "08f9f00f-4e52-4466-ae27-be9fa9813a88"
RECHECK_SECONDS = 900
VALID_EVENT_TYPES = ("create", "update", "delete")
#: Profile fields a trigger can derive its filters from. ``regions`` and
#: ``sector`` are resolved to the OpenCTI Country and Sector entities, then
#: matched through ``objects`` (a container holding them) or ``toId`` (a
#: relationship pointing at them): live stix filtering has no ``regardingOf``.
DERIVABLE_CONTEXT_FIELDS = ("pir_ids", "regions", "sector")
#: ``provision.mode``: a live knowledge trigger (default), or (CTEM-05) an OpenCTI
#: playbook "Listen PIR events" then "Send to notifier". OpenCTI flags an entity
#: in a PIR through its internal ``in-pir`` relationship, which a live trigger
#: never sees; the playbook entry is the consumer OpenCTI built for it.
TRIGGER_MODE = "trigger"
PIR_PLAYBOOK_MODE = "pir_playbook"
PROVISION_MODES = (TRIGGER_MODE, PIR_PLAYBOOK_MODE)
#: OpenCTI's playbook components (``modules/playbook/components``).
PIR_ENTRY_COMPONENT = "PLAYBOOK_DATA_STREAM_PIR"
NOTIFIER_COMPONENT = "PLAYBOOK_NOTIFIER_COMPONENT"
#: ``provision.organizations`` values besides a list of relationships.
PRIMARY_SCOPE = "primary"
TRACKED_SCOPE = "tracked"
TRACKED_RELATIONSHIPS = ("provider", "customer", "partner", "subsidiary")

#: The body OpenCTI posts (EJS, rendered with JSON escaping). ``notification_id``
#: is unique per change (trigger, entity, event, modification time), so the
#: receiver's de-duplication drops a redelivery and never a new change.
WEBHOOK_TEMPLATE = (
    '{"notification_id": "<%= notification.id %>:<%= data[0].instance.id %>:'
    '<%= data[0].type %>:<%= data[0].instance.modified %>", '
    '"type": "<%= data[0].type %>", "message": "<%= data[0].message %>", '
    '"data": <%- JSON.stringify(data[0].instance) %>}'
)

Gql = Callable[[str, dict | None], Awaitable[tuple[dict, str | None]]]


class ProvisionSpecError(ValueError):
    """``trigger_config.provision`` cannot be turned into an OpenCTI trigger."""


def _str_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ProvisionSpecError(f"provision.{field} must be a list of non-empty strings")
    return sorted({v.strip() for v in value})


def _organizations_scope(value: Any) -> str | list[str]:
    """``primary``, ``tracked`` or the sorted relationships ``provision.organizations`` names."""
    if value in (None, "", PRIMARY_SCOPE):
        return PRIMARY_SCOPE
    if value == TRACKED_SCOPE:
        return TRACKED_SCOPE
    relationships = _str_list(value, "organizations")
    unknown = [r for r in relationships if r not in TRACKED_RELATIONSHIPS]
    if unknown or not relationships:
        raise ProvisionSpecError(
            f"provision.organizations: use primary, tracked or a list of {list(TRACKED_RELATIONSHIPS)}"
        )
    return relationships


def read_provision_spec(trigger_config: Any) -> dict[str, Any] | None:
    """The normalised ``provision`` block, ``None`` when the trigger declares none.

    Raises :class:`ProvisionSpecError` on a block that names no usable trigger.
    """
    if not isinstance(trigger_config, dict) or trigger_config.get("provider") != "opencti":
        return None
    raw = trigger_config.get(PROVISION_KEY)
    if raw in (None, False, {}):
        return None
    if not isinstance(raw, dict):
        raise ProvisionSpecError("provision must be an object")
    event_types = _str_list(raw.get("event_types"), "event_types") or ["create"]
    unknown = [e for e in event_types if e not in VALID_EVENT_TYPES]
    if unknown:
        raise ProvisionSpecError(f"provision.event_types: unknown {unknown}, use {list(VALID_EVENT_TYPES)}")
    min_score = raw.get("min_score")
    if min_score is not None and (
        isinstance(min_score, bool) or not isinstance(min_score, int) or not 0 <= min_score <= 100
    ):
        raise ProvisionSpecError("provision.min_score must be an integer from 0 to 100")
    entity_types = _str_list(raw.get("entity_types"), "entity_types")
    labels = _str_list(raw.get("labels"), "labels")
    from_types = _str_list(raw.get("from_types"), "from_types")
    # CTEM-08: the target side of a relationship, the relationship type, a CVSS floor.
    to_types = _str_list(raw.get("to_types"), "to_types")
    relationship_types = _str_list(raw.get("relationship_types"), "relationship_types")
    min_cvss = raw.get("min_cvss")
    if min_cvss is not None and (
        isinstance(min_cvss, bool) or not isinstance(min_cvss, int | float) or not 0 <= min_cvss <= 10
    ):
        raise ProvisionSpecError("provision.min_cvss must be a number from 0 to 10")
    cisa_kev = raw.get("cisa_kev")
    if cisa_kev is not None and not isinstance(cisa_kev, bool):
        raise ProvisionSpecError("provision.cisa_kev must be true or false")
    min_epss = raw.get("min_epss_score")
    if min_epss is not None and (
        isinstance(min_epss, bool) or not isinstance(min_epss, int | float) or not 0 <= min_epss <= 1
    ):
        raise ProvisionSpecError("provision.min_epss_score must be a number from 0 to 1")
    context = raw.get("organization_context")
    if context is not None and not isinstance(context, str | list):
        raise ProvisionSpecError(
            "provision.organization_context must be a list of profile field names, "
            f'e.g. ["pir_ids"] (any of {list(DERIVABLE_CONTEXT_FIELDS)}): the values come from the profile'
        )
    context_fields = _str_list(context, "organization_context")
    # A subset of the profile PIRs (by OpenCTI id): naming it implies pir_ids.
    pir_subset = _str_list(raw.get("pir_ids"), "pir_ids")
    if pir_subset and "pir_ids" not in context_fields:
        context_fields = [*context_fields, "pir_ids"]
    not_derivable = [f for f in context_fields if f not in DERIVABLE_CONTEXT_FIELDS]
    if not_derivable:
        raise ProvisionSpecError(
            f"provision.organization_context: {not_derivable} cannot be derived yet, "
            f"use {list(DERIVABLE_CONTEXT_FIELDS)}"
        )
    pir_min_score = raw.get("pir_min_score", 1)
    if isinstance(pir_min_score, bool) or not isinstance(pir_min_score, int) or not 0 <= pir_min_score <= 100:
        raise ProvisionSpecError("provision.pir_min_score must be an integer from 0 to 100")
    mode = raw.get("mode") or TRIGGER_MODE
    if mode not in PROVISION_MODES:
        raise ProvisionSpecError(f"provision.mode: use one of {list(PROVISION_MODES)}")
    if mode == PIR_PLAYBOOK_MODE and "pir_ids" not in context_fields:
        raise ProvisionSpecError(
            "provision.mode pir_playbook listens to the profile PIRs: organization_context must name pir_ids"
        )
    organizations = _organizations_scope(raw.get("organizations"))
    if organizations != PRIMARY_SCOPE and context_fields:
        raise ProvisionSpecError(
            "provision.organization_context reads the primary profile: "
            "it is not combined with tracked organizations"
        )
    if (
        organizations == PRIMARY_SCOPE
        and not entity_types
        and not labels
        and min_score is None
        and not context_fields
        and not from_types
        and not to_types
        and not relationship_types
        and min_cvss is None
        and cisa_kev is None
        and min_epss is None
    ):
        raise ProvisionSpecError(
            "provision needs at least one of entity_types, labels, min_score, from_types, to_types, "
            "relationship_types, min_cvss, cisa_kev, min_epss_score or organization_context: "
            "a trigger on every change of the platform is never provisioned"
        )
    fleet = str(raw.get("fleet") or "").strip() or "Fleet"
    purpose = str(raw.get("purpose") or "").strip() or "Events"
    return {
        "fleet": fleet[:80],
        "purpose": purpose[:80],
        "entity_types": entity_types,
        "labels": labels,
        "min_score": min_score,
        "from_types": from_types,
        "to_types": to_types,
        "relationship_types": relationship_types,
        "min_cvss": float(min_cvss) if min_cvss is not None else None,
        "cisa_kev": cisa_kev,
        "min_epss_score": float(min_epss) if min_epss is not None else None,
        "event_types": event_types,
        "organization_context": context_fields,
        "pir_min_score": pir_min_score,
        "pir_subset": pir_subset,
        "mode": mode,
        "organizations": organizations,
        # Filled from the tracked organizations by :func:`with_tracked_organizations`.
        "organization_refs": [],
        "organization_sectors": [],
        "organization_pir_ids": [],
        # Filled from the profile by :func:`derive_from_organization_context`.
        "pir_ids": [],
        "regions": [],
        "sectors": [],
    }


def pir_score_filters(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """One OpenCTI composite ``pir_score`` filter per minimum score, each naming its PIRs (#2435). Pure.

    The PIRs of the spec sharing a minimum go in one filter; a spec without
    ``pir_thresholds`` (a profile saved before the Intelligence policy) answers
    one filter at ``pir_min_score``, as before.
    """
    pir_ids = sorted(spec.get("pir_ids") or [])
    own = spec.get("pir_thresholds") or {}
    by_score: dict[int, list[str]] = {}
    for pid in pir_ids:
        by_score.setdefault(int(own.get(pid, spec.get("pir_min_score", 1))), []).append(pid)
    return [
        {
            "key": ["pir_score"],
            "values": [
                {"key": "score", "values": [str(score)], "operator": "gte"},
                {"key": "pir_ids", "values": ids},
            ],
            "operator": "eq",
            "mode": "or",
        }
        for score, ids in sorted(by_score.items())
    ]


def derive_from_organization_context(spec: dict[str, Any], profile: Any) -> tuple[dict[str, Any], list[str]]:
    """``(spec, missing)``: *spec* with the profile-derived values, and the declared fields left empty.

    *profile* is an ``OrganizationProfile`` (``None`` before the first save:
    every declared field is missing). Nothing is guessed: a missing field
    keeps the spec as it was and is named.
    """
    derived = dict(spec)
    missing: list[str] = []
    declared = spec.get("organization_context", [])
    if "pir_ids" in declared:
        pir_ids = sorted(getattr(profile, "pir_ids", None) or [])
        subset = spec.get("pir_subset") or []
        absent = [pid for pid in subset if pid not in pir_ids]
        if subset and not absent:
            pir_ids = list(subset)
        if absent:
            missing.append(f"pir_ids {absent} (absent from the profile PIRs)")
        elif pir_ids:
            derived["pir_ids"] = pir_ids
            # Each PIR's own minimum score from the Intelligence policy (#2435), the spec's otherwise.
            own = getattr(profile, "pir_min_scores", None) or {}
            derived["pir_thresholds"] = {pid: int(own.get(pid, spec.get("pir_min_score", 1))) for pid in pir_ids}
        else:
            missing.append("pir_ids")
    if "regions" in declared:
        regions = sorted(getattr(profile, "regions", None) or [])
        if regions:
            derived["regions"] = regions
        else:
            missing.append("regions")
    if "sector" in declared:
        sector = str(getattr(profile, "sector", "") or "").strip()
        if sector:
            derived["sectors"] = [sector]
        else:
            missing.append("sector")
    return derived, missing


def tracked_refs(spec: dict[str, Any], organizations: list[Any], profiles: dict[Any, Any]) -> dict[str, Any]:
    """*spec* with the tracked organizations of its scope: monitoring on, an OpenCTI identity, not archived.

    *profiles* maps an organization id to its ``TrackedOrganizationProfile``
    (absent before the first save). Pure: :func:`with_tracked_organizations`
    reads the rows.
    """
    scope = spec.get("organizations", PRIMARY_SCOPE)
    refs: list[dict[str, Any]] = []
    pir_ids: set[str] = set()
    for o in organizations:
        if getattr(o, "role", None) != "tracked" or getattr(o, "archived_at", None) is not None:
            continue
        if getattr(o, "monitoring", "off") != "on" or not getattr(o, "opencti_organization_id", None):
            continue
        if scope != TRACKED_SCOPE and getattr(o, "relationship", None) not in scope:
            continue
        profile = profiles.get(o.id)
        sector = str(getattr(profile, "sector", "") or "").strip()
        pir_ids.update(getattr(profile, "pir_ids", None) or [])
        refs.append(
            {
                "id": str(o.id),
                "name": o.name,
                "identity_id": o.opencti_organization_id,
                "sectors": [sector] if sector else [],
            }
        )
    derived = dict(spec)
    derived["organization_refs"] = sorted(refs, key=lambda r: r["id"])
    derived["organization_sectors"] = sorted({s for r in refs for s in r["sectors"]})
    derived["organization_pir_ids"] = sorted(pir_ids)
    return derived


async def with_tracked_organizations(db: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """*spec* with the tracked organizations of its scope, read at every check (a change repairs the trigger)."""
    from app.services import organization_context as org_context
    from app.services import organizations as orgs

    rows = await orgs.list_organizations(db)
    profiles: dict[Any, Any] = {}
    for o in rows:
        if o.role != "tracked":
            continue
        version = await org_context.current_version(db, o.id)
        if version is not None:
            profiles[o.id] = orgs.parse_tracked_profile(version.profile)
    return tracked_refs(spec, rows, profiles)


def object_name(spec: dict[str, Any]) -> str:
    return f"{NAME_PREFIX} - {spec['fleet']} - {spec['purpose']}"


def receiver_url(base_url: str, integration_id: str, assignment_id: str) -> str:
    return f"{base_url.rstrip('/')}/api/hooks/integrations/{integration_id}/assignments/{assignment_id}"


def notifier_configuration(url: str, secret: str, *, template: str = WEBHOOK_TEMPLATE) -> str:
    """The Generic webhook ``notifier_configuration`` (OpenCTI's JSON string)."""
    return json.dumps(
        {
            "url": url,
            "verb": "POST",
            "template": template,
            "params": [],
            "headers": [
                {"attribute": "Content-Type", "value": "application/json"},
                {"attribute": "X-OpenCTI-Token", "value": secret},
            ],
        },
        sort_keys=True,
    )


def trigger_filters(
    spec: dict[str, Any],
    label_ids: list[str],
    scope_ids: list[str] | None = None,
    organization_ids: list[str] | None = None,
) -> str:
    """The OpenCTI ``FilterGroup`` (JSON string) of *spec*; labels as resolved ids.

    *scope_ids* are the internal ids of the Country and Sector entities the
    profile's ``regions`` and ``sector`` resolved to: the entity must be a
    container holding one of them (``objects``) or a relationship pointing at
    one (``toId``). OpenCTI turns both into standard ids before matching.

    *organization_ids* are the tracked organizations' identities and sectors
    (``provision.organizations``): one OR group of ``objects``, ``toId`` and
    the organizations' PIRs (``pir_score``).
    """
    filters: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    if spec["entity_types"]:
        filters.append({"key": ["entity_type"], "values": spec["entity_types"], "operator": "eq", "mode": "or"})
    if label_ids:
        filters.append({"key": ["objectLabel"], "values": sorted(label_ids), "operator": "eq", "mode": "or"})
    if spec["min_score"] is not None:
        filters.append(
            {"key": ["x_opencti_score"], "values": [str(spec["min_score"])], "operator": "gte", "mode": "or"}
        )
    if spec.get("from_types"):
        # A relationship or sighting whose source is of these types (stix-testers
        # ``testRelationFromTypes``: ``sighting_of_ref`` for a sighting).
        filters.append({"key": ["fromTypes"], "values": spec["from_types"], "operator": "eq", "mode": "or"})
    if spec.get("to_types"):
        # A relationship whose target is of these types (``testRelationToTypes``:
        # ``target_type`` of the relationship, parent types included).
        filters.append({"key": ["toTypes"], "values": spec["to_types"], "operator": "eq", "mode": "or"})
    if spec.get("relationship_types"):
        # The relationship type (``has``, ``targets``...): OpenCTI's internal type of
        # a relationship is its relationship type, so ``entity_type`` matches it.
        filters.append({"key": ["entity_type"], "values": spec["relationship_types"], "operator": "eq", "mode": "or"})
    if spec.get("min_cvss") is not None:
        # Read on the delivered object (``testCvssScore``): a Vulnerability, never a relationship.
        filters.append(
            {"key": ["x_opencti_cvss_base_score"], "values": [str(spec["min_cvss"])], "operator": "gte", "mode": "or"}
        )
    # KEV and EPSS are alternatives ("KEV or EPSS jump"): one OR group.
    vuln_signals: list[dict[str, Any]] = []
    if spec.get("cisa_kev") is not None:
        vuln_signals.append(
            {"key": ["x_opencti_cisa_kev"], "values": [str(spec["cisa_kev"]).lower()], "operator": "eq", "mode": "or"}
        )
    if spec.get("min_epss_score") is not None:
        vuln_signals.append(
            {"key": ["x_opencti_epss_score"], "values": [str(spec["min_epss_score"])], "operator": "gte", "mode": "or"}
        )
    if len(vuln_signals) == 1:
        filters.extend(vuln_signals)
    elif vuln_signals:
        groups.append({"mode": "or", "filters": vuln_signals, "filterGroups": []})
    if spec.get("pir_ids"):
        # OpenCTI's composite PIR filter (stix-testers ``testPirScore``): an
        # entity flagged in one of these PIRs with at least the score. One
        # filter per minimum score (#2435), any of them matching.
        score_filters = pir_score_filters(spec)
        if len(score_filters) == 1:
            filters.append(score_filters[0])
        else:
            groups.append({"mode": "or", "filters": score_filters, "filterGroups": []})
    if scope_ids:
        ids = sorted(set(scope_ids))
        groups.append(
            {
                "mode": "or",
                "filters": [
                    {"key": ["objects"], "values": ids, "operator": "eq", "mode": "or"},
                    {"key": ["toId"], "values": ids, "operator": "eq", "mode": "or"},
                ],
                "filterGroups": [],
            }
        )
    if organization_ids:
        ids = sorted(set(organization_ids))
        concerned: list[dict[str, Any]] = [
            {"key": ["objects"], "values": ids, "operator": "eq", "mode": "or"},
            {"key": ["toId"], "values": ids, "operator": "eq", "mode": "or"},
        ]
        if spec.get("organization_pir_ids"):
            concerned.append(
                {
                    "key": ["pir_score"],
                    "values": [
                        {"key": "score", "values": [str(spec.get("pir_min_score", 1))], "operator": "gte"},
                        {"key": "pir_ids", "values": sorted(spec["organization_pir_ids"])},
                    ],
                    "operator": "eq",
                    "mode": "or",
                }
            )
        groups.append({"mode": "or", "filters": concerned, "filterGroups": []})
    return json.dumps({"mode": "and", "filters": filters, "filterGroups": groups}, sort_keys=True)


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def _same_filters(stored: Any, desired: str) -> bool:
    try:
        left = json.loads(stored) if isinstance(stored, str) else stored
        return left == json.loads(desired)
    except (TypeError, ValueError):
        return False


async def _resolve_label_ids(gql: Gql, labels: list[str]) -> tuple[list[str], list[str]]:
    """``(ids, missing)``: each label name matched exactly (case-insensitive)."""
    ids: list[str] = []
    missing: list[str] = []
    for label in labels:
        data, err = await gql(
            "query XtmLabels($search: String) { labels(first: 50, search: $search) "
            "{ edges { node { id value } } } }",
            {"search": label},
        )
        if err:
            raise RuntimeError(err)
        nodes = [e.get("node") or {} for e in ((data.get("labels") or {}).get("edges") or [])]
        match = next((n for n in nodes if str(n.get("value", "")).lower() == label.lower()), None)
        if match and match.get("id"):
            ids.append(match["id"])
        else:
            missing.append(label)
    return ids, missing


_ALPHA2_TO_ALPHA3: dict[str, str] = {two: three for three, two in ISO_3166_ALPHA3_TO_ALPHA2.items()}


async def _resolve_country_ids(gql: Gql, regions: list[str]) -> tuple[list[str], list[str]]:
    """``(ids, missing)``: each ISO alpha-2 region matched to the Country carrying it or its alpha-3 code as an alias."""
    ids: list[str] = []
    missing: list[str] = []
    for region in regions:
        codes = {region.upper(), _ALPHA2_TO_ALPHA3.get(region.upper(), "")} - {""}
        data, err = await gql(
            "query XtmCountries($search: String) { countries(first: 50, search: $search) "
            "{ edges { node { id name x_opencti_aliases } } } }",
            {"search": _ALPHA2_TO_ALPHA3.get(region.upper(), region)},
        )
        if err:
            raise RuntimeError(err)
        nodes = [e.get("node") or {} for e in ((data.get("countries") or {}).get("edges") or [])]
        match = next(
            (
                n
                for n in nodes
                if codes & {str(a).strip().upper() for a in (n.get("x_opencti_aliases") or []) if isinstance(a, str)}
            ),
            None,
        )
        if match and match.get("id"):
            ids.append(match["id"])
        else:
            missing.append(region)
    return ids, missing


async def _resolve_sector_ids(gql: Gql, sectors: list[str]) -> tuple[list[str], list[str]]:
    """``(ids, missing)``: each sector matched to the Sector of that name or alias (case-insensitive)."""
    ids: list[str] = []
    missing: list[str] = []
    for sector in sectors:
        data, err = await gql(
            "query XtmSectors($search: String) { sectors(first: 50, search: $search) "
            "{ edges { node { id name x_opencti_aliases } } } }",
            {"search": sector},
        )
        if err:
            raise RuntimeError(err)
        nodes = [e.get("node") or {} for e in ((data.get("sectors") or {}).get("edges") or [])]
        wanted = sector.strip().lower()
        match = next(
            (
                n
                for n in nodes
                if wanted
                in {str(v).strip().lower() for v in [n.get("name"), *(n.get("x_opencti_aliases") or [])] if v}
            ),
            None,
        )
        if match and match.get("id"):
            ids.append(match["id"])
        else:
            missing.append(sector)
    return ids, missing


async def _standard_ids(gql: Gql, internal_ids: list[str]) -> dict[str, str]:
    """``{internal id: standard id}`` of OpenCTI entities (a delivery carries STIX standard ids)."""
    found: dict[str, str] = {}
    for internal_id in internal_ids:
        data, err = await gql(
            "query XtmStandardId($id: String!) { stixCoreObject(id: $id) { id standard_id } }", {"id": internal_id}
        )
        if err:
            raise RuntimeError(err)
        node = data.get("stixCoreObject") or {}
        if node.get("standard_id"):
            found[internal_id] = str(node["standard_id"])
    return found


async def _organization_scope(
    gql: Gql, spec: dict[str, Any]
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """``(filter ids, organizations for the receiver, unresolved sectors)`` of a tracked scope.

    Each organization of ``spec["organization_refs"]`` keeps its identity and
    sector ids, internal and standard, so the receiver resolves which one an
    event concerns from the ids the delivery carries.
    """
    sectors = sorted({s for ref in spec["organization_refs"] for s in ref.get("sectors") or []})
    sector_by_name: dict[str, str] = {}
    missing: list[str] = []
    for sector in sectors:
        ids, absent = await _resolve_sector_ids(gql, [sector])
        if ids:
            sector_by_name[sector] = ids[0]
        missing += absent
    internal = sorted({ref["identity_id"] for ref in spec["organization_refs"]} | set(sector_by_name.values()))
    standard = await _standard_ids(gql, internal)
    receiver: list[dict[str, Any]] = []
    for ref in spec["organization_refs"]:
        own = [ref["identity_id"], *[sector_by_name[s] for s in ref.get("sectors") or [] if s in sector_by_name]]
        receiver.append(
            {
                "id": ref["id"],
                "name": ref["name"],
                "identity_ids": sorted({ref["identity_id"], standard.get(ref["identity_id"], "")} - {""}),
                "sector_ids": sorted({i for s in own[1:] for i in (s, standard.get(s, "")) if i}),
            }
        )
    return internal, receiver, missing


async def _find_by_name(gql: Gql, root: str, name: str) -> str | None:
    """The id of the object named exactly *name* (a state lost by a save is adopted, never duplicated)."""
    data, err = await gql(
        f"query XtmFind($search: String) {{ {root}(first: 50, search: $search) {{ edges {{ node {{ id name }} }} }} }}",
        {"search": name},
    )
    if err:
        raise RuntimeError(err)
    for edge in (data.get(root) or {}).get("edges") or []:
        node = edge.get("node") or {}
        if node.get("name") == name and node.get("id"):
            return node["id"]
    return None


async def _ensure_notifier(gql: Gql, notifier_id: str | None, name: str, config: str) -> str:
    notifier_id = notifier_id or await _find_by_name(gql, "notifiers", name)
    if notifier_id:
        data, err = await gql(
            "query XtmNotifier($id: String!) { notifier(id: $id) { id name notifier_configuration } }",
            {"id": notifier_id},
        )
        if err:
            raise RuntimeError(err)
        current = data.get("notifier")
        if current:
            edits = []
            if current.get("name") != name:
                edits.append({"key": "name", "value": [name]})
            if current.get("notifier_configuration") != config:
                edits.append({"key": "notifier_configuration", "value": [config]})
            if edits:
                _, err = await gql(
                    "mutation XtmNotifierPatch($id: ID!, $input: [EditInput!]!) "
                    "{ notifierFieldPatch(id: $id, input: $input) { id } }",
                    {"id": notifier_id, "input": edits},
                )
                if err:
                    raise RuntimeError(err)
            return notifier_id
    data, err = await gql(
        "mutation XtmNotifierAdd($input: NotifierAddInput!) { notifierAdd(input: $input) { id } }",
        {
            "input": {
                "name": name,
                "description": "Provisioned by XTM One: delivers this trigger's events to an assignment.",
                "notifier_connector_id": WEBHOOK_CONNECTOR_ID,
                "notifier_configuration": config,
            }
        },
    )
    if err:
        raise RuntimeError(err)
    created = (data.get("notifierAdd") or {}).get("id")
    if not created:
        raise RuntimeError("OpenCTI returned no notifier id")
    return created


async def _ensure_trigger(
    gql: Gql, trigger_id: str | None, name: str, filters: str, event_types: list[str], notifier_id: str
) -> str:
    trigger_id = trigger_id or await _find_by_name(gql, "triggersKnowledge", name)
    if trigger_id:
        data, err = await gql(
            "query XtmTrigger($id: String!) { triggerKnowledge(id: $id) "
            "{ id name event_types filters notifiers { id } } }",
            {"id": trigger_id},
        )
        if err:
            raise RuntimeError(err)
        current = data.get("triggerKnowledge")
        if current and sorted(current.get("event_types") or []) == sorted(event_types):
            edits = []
            if current.get("name") != name:
                edits.append({"key": "name", "value": [name]})
            if not _same_filters(current.get("filters"), filters):
                edits.append({"key": "filters", "value": [filters]})
            if [n.get("id") for n in (current.get("notifiers") or [])] != [notifier_id]:
                edits.append({"key": "notifiers", "value": [notifier_id]})
            if edits:
                _, err = await gql(
                    "mutation XtmTriggerPatch($id: ID!, $input: [EditInput!]!) "
                    "{ triggerKnowledgeFieldPatch(id: $id, input: $input) { id } }",
                    {"id": trigger_id, "input": edits},
                )
                if err:
                    raise RuntimeError(err)
            return trigger_id
        if current:
            # Event types cannot be patched: replace the trigger.
            await _delete(gql, "triggerKnowledgeDelete", trigger_id)
    data, err = await gql(
        "mutation XtmTriggerAdd($input: TriggerLiveAddInput!) { triggerKnowledgeLiveAdd(input: $input) { id } }",
        {
            "input": {
                "name": name,
                "description": "Provisioned by XTM One for an assignment; edits are reverted.",
                "event_types": event_types,
                "notifiers": [notifier_id],
                "instance_trigger": False,
                "filters": filters,
            }
        },
    )
    if err:
        raise RuntimeError(err)
    created = (data.get("triggerKnowledgeLiveAdd") or {}).get("id")
    if not created:
        raise RuntimeError("OpenCTI returned no trigger id")
    return created


#: The notifier body of a PIR playbook delivery. A playbook digest has no trigger
#: (``notification``); each item names the playbook node (``notification_id``)
#: and carries the flagged entity (``data[0].instance``, its PIR information
#: in the OpenCTI extension) and the in-pir event's message. Two score changes
#: carry the same message ("Update 2 elements ... internal-relationship", read in
#: the lab), so the entity's PIR information (score and score date) enters the id
#: too: each change is new, a redelivery of the same one is dropped by the receiver.
#: OpenCTI master renders the entity as loaded when the in-pir relationship is
#: patched, before its own PIR information is rewritten: on an update the score
#: delivered is the one before the change (lab, 21 Nov 2026). The signal records it
#: as the score at delivery; the fleet reads the live score from OpenCTI.
#: OpenCTI renders the template through safeEjs, which refuses the whole notification
#: ("Invalid javascript") when one expression does not parse: the tests parse and
#: render every expression of both templates.
PIR_WEBHOOK_TEMPLATE = (
    '{"notification_id": "<%= data[0].notification_id %>:<%= data[0].instance.id %>:'
    "<%= data[0].type %>:<%= data[0].message %>:"
    "<%= JSON.stringify(((data[0].instance.extensions || {})"
    "['extension-definition--ea279b3e-5c71-4632-ac08-831c66a786ba'] || {}).pir_information) %>\", "
    '"type": "<%= data[0].type %>", "message": "<%= data[0].message %>", '
    '"xtm_source": "pir_playbook", '
    '"data": <%- JSON.stringify(data[0].instance) %>}'
)


#: The array properties of the provisioned PIR playbook whose items OpenCTI's
#: playbook form draws as chips (``ComboboxField``: the chip text is the item's
#: ``label``, its icon the item's ``type``) while the runtime reads ``value``
#: alone (``PirField`` for ``inPirFilters``, ``ObjectMembersField`` for
#: ``authorized_members``). They are written as the form writes them, and
#: compared by their set of ``value``: a label or a type rewritten by a save in
#: OpenCTI is not a drift.
CHIP_ITEM_KEYS: dict[str, tuple[str, ...]] = {
    PIR_ENTRY_COMPONENT: ("inPirFilters",),
    NOTIFIER_COMPONENT: ("authorized_members",),
}


def pir_playbook_nodes(
    spec: dict[str, Any],
    notifier_id: str,
    member_id: str,
    member_name: str = "",
    pir_names: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The two node configurations of a PIR playbook: (Listen PIR events, Send to notifier).

    The entry listens to the profile PIRs (``inPirFilters`` items are
    ``{"value": id}``, as OpenCTI's listener reads them) on an entity entering a
    PIR (``create``) and on an update of a flagged entity or of its in-pir
    relationship, a score change included (``update``); ``filters`` keeps the
    entities whose score in those PIRs is at least ``pir_min_score`` (the string
    form OpenCTI stores). The notifier delivers to the integration's own OpenCTI
    user only: OpenCTI filters each delivery by what that user may read.

    Each chip item carries ``label`` and ``type`` as the OpenCTI form saves
    them (an item with ``value`` alone is drawn as an empty chip); a name
    OpenCTI did not answer falls back to the id, never to an empty label.
    """
    pir_ids = sorted(spec.get("pir_ids") or [])
    names = pir_names or {}
    per_score = pir_score_filters(spec)
    if len(per_score) <= 1:
        score = per_score[0]["values"][0]["values"][0] if per_score else str(spec.get("pir_min_score", 1))
        score_filter: dict[str, Any] = {
            "mode": "and",
            "filters": [{"key": ["pir_score"], "values": [score], "operator": "gte", "mode": "or"}],
            "filterGroups": [],
        }
    else:
        # Several minimum scores (#2435): OpenCTI rewrites a first-level pir_score filter to every
        # listened PIR (listenPirEventsUtils.formatFiltersForPirPlaybookComponent), so each PIR's
        # own minimum goes in a nested group as the composite filter, which it reads as written.
        score_filter = {
            "mode": "and",
            "filters": [],
            "filterGroups": [{"mode": "or", "filters": per_score, "filterGroups": []}],
        }
    # The declared entity types narrow the playbook as they narrow a trigger (#2121): two
    # assignments on the same PIRs with different types never deliver each other's entities.
    if spec.get("entity_types"):
        score_filter["filters"].append(
            {"key": ["entity_type"], "values": list(spec["entity_types"]), "operator": "eq", "mode": "or"}
        )
    entry = {
        "inPirFilters": [{"value": pid, "label": names.get(pid) or pid, "type": "Pir"} for pid in pir_ids],
        "create": True,
        "update": True,
        "delete": False,
        "create_rel": False,
        "filters": json.dumps(score_filter, sort_keys=True),
    }
    notify = {
        "notifiers": [notifier_id],
        "authorized_members": [{"value": member_id, "label": member_name or member_id, "type": "User"}],
    }
    return entry, notify


def _chip_values(items: Any) -> list[str] | None:
    """The sorted ``value`` of each chip item, or ``None`` when *items* is not a list of them."""
    if not isinstance(items, list):
        return None
    values: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("value"), str):
            return None
        values.append(item["value"])
    return sorted(values)


def same_node_configuration(component_id: str, wanted: dict[str, Any], stored: Any) -> bool:
    """Whether *stored* is *wanted*, the chip items of :data:`CHIP_ITEM_KEYS` compared by ``value`` only."""
    if not isinstance(stored, dict):
        return False
    keys = CHIP_ITEM_KEYS.get(component_id, ())
    if {k: v for k, v in wanted.items() if k not in keys} != {k: v for k, v in stored.items() if k not in keys}:
        return False
    return all(_chip_values(wanted.get(k)) == _chip_values(stored.get(k)) for k in keys)


def _without_chip_labels(component_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """*config* with each chip item reduced to its ``value`` (the form provisioned before the labels)."""
    keys = CHIP_ITEM_KEYS.get(component_id, ())
    return {k: ([{"value": i["value"]} for i in v] if k in keys else v) for k, v in config.items()}


def unlabelled_chip_items(component_id: str, stored: dict[str, Any]) -> bool:
    """Whether a chip item of *stored* has no ``label`` (OpenCTI draws it as an empty chip)."""
    for key in CHIP_ITEM_KEYS.get(component_id, ()):
        for item in stored.get(key) or []:
            label = item.get("label") if isinstance(item, dict) else None
            if not isinstance(label, str) or not label.strip():
                return True
    return False


def _stored_nodes(definition: Any) -> dict[str, dict[str, Any]]:
    """``component_id -> node`` of a stored ``playbook_definition``, its ``configuration`` parsed."""
    try:
        parsed = json.loads(definition) if isinstance(definition, str) else definition
    except ValueError:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for node in (parsed or {}).get("nodes") or []:
        raw = node.get("configuration")
        try:
            config = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            config = None
        out[str(node.get("component_id"))] = {**node, "configuration": config}
    return out


async def _own_user(gql: Gql) -> tuple[str, str]:
    """``(id, name)`` of the integration's OpenCTI user."""
    data, err = await gql("query XtmMe { me { id name } }", None)
    if err:
        raise RuntimeError(err)
    me = data.get("me") or {}
    user_id = me.get("id")
    if not user_id:
        raise RuntimeError("OpenCTI returned no id for the integration's user")
    return str(user_id), str(me.get("name") or "")


async def _pir_names(gql: Gql, pir_ids: list[str]) -> dict[str, str]:
    """``id -> name`` of the PIRs the integration's user reads, for the chip labels.

    A label is display only: a failed read leaves the ids as labels and never
    stops the provisioning.
    """
    if not pir_ids:
        return {}
    data, err = await gql("query XtmPirs { pirs(first: 500) { edges { node { id name } } } }", None)
    if err:
        return {}
    names: dict[str, str] = {}
    for edge in ((data or {}).get("pirs") or {}).get("edges") or []:
        node = (edge or {}).get("node") or {}
        if node.get("id") in pir_ids and node.get("name"):
            names[str(node["id"])] = str(node["name"])
    return names


async def _relabel_node(gql: Gql, playbook_id: str, node: dict[str, Any], config: dict[str, Any]) -> None:
    """Write *config* on *node* in place (``playbookReplaceNode``: same id, links and position)."""
    _, err = await gql(
        "mutation XtmNodeReplace($id: ID!, $nodeId: ID!, $input: PlaybookAddNodeInput!) "
        "{ playbookReplaceNode(id: $id, nodeId: $nodeId, input: $input) }",
        {
            "id": playbook_id,
            "nodeId": node.get("id"),
            "input": {
                "name": node.get("name") or "",
                "component_id": node.get("component_id"),
                "position": node.get("position") or {"x": 0, "y": 0},
                "configuration": json.dumps(config, sort_keys=True),
            },
        },
    )
    if err:
        raise RuntimeError(err)


async def _ensure_pir_playbook(
    gql: Gql, playbook_id: str | None, name: str, entry: dict[str, Any], notify: dict[str, Any]
) -> str:
    """One running playbook *name* holding *entry* then *notify*; rebuilt on any drift.

    The chip items (:data:`CHIP_ITEM_KEYS`) are compared by ``value``: a node
    whose items carry the right values but no label (provisioned before the
    labels) is patched in place, a label rewritten in OpenCTI is kept.
    """
    playbook_id = playbook_id or await _find_by_name(gql, "playbooks", name)
    if playbook_id:
        data, err = await gql(
            "query XtmPlaybook($id: String!) { playbook(id: $id) { id name playbook_running playbook_definition } }",
            {"id": playbook_id},
        )
        if err:
            raise RuntimeError(err)
        current = data.get("playbook")
        if current:
            nodes = _stored_nodes(current.get("playbook_definition"))
            wanted = {PIR_ENTRY_COMPONENT: entry, NOTIFIER_COMPONENT: notify}
            if (
                current.get("name") == name
                and current.get("playbook_running")
                and set(nodes) == set(wanted)
                and all(
                    same_node_configuration(cid, config, nodes[cid]["configuration"]) for cid, config in wanted.items()
                )
            ):
                for cid, config in wanted.items():
                    if unlabelled_chip_items(cid, nodes[cid]["configuration"]):
                        await _relabel_node(gql, playbook_id, nodes[cid], config)
                return playbook_id
            # Edited in OpenCTI: rebuilt, never patched node by node.
            await _delete(gql, "playbookDelete", playbook_id)
    data, err = await gql(
        "mutation XtmPlaybookAdd($input: PlaybookAddInput!) { playbookAdd(input: $input) { id } }",
        {"input": {"name": name, "description": "Provisioned by XTM One for an assignment; edits are reverted."}},
    )
    if err:
        raise RuntimeError(err)
    created = (data.get("playbookAdd") or {}).get("id")
    if not created:
        raise RuntimeError("OpenCTI returned no playbook id")
    node_add = (
        "mutation XtmNodeAdd($id: ID!, $input: PlaybookAddNodeInput!) { playbookAddNode(id: $id, input: $input) }"
    )
    data, err = await gql(
        node_add,
        {
            "id": created,
            "input": {
                "name": "Listen PIR events",
                "component_id": PIR_ENTRY_COMPONENT,
                "position": {"x": 0, "y": 0},
                "configuration": json.dumps(entry, sort_keys=True),
            },
        },
    )
    if err:
        raise RuntimeError(err)
    entry_id = str(data.get("playbookAddNode") or "")
    if not entry_id:
        raise RuntimeError("OpenCTI returned no node id for the PIR entry")
    _, err = await gql(
        node_add,
        {
            "id": created,
            "input": {
                "name": "Send to XTM One",
                "component_id": NOTIFIER_COMPONENT,
                "position": {"x": 0, "y": 150},
                "configuration": json.dumps(notify, sort_keys=True),
                "parentNodeId": entry_id,
                "parentPortId": "out",
            },
        },
    )
    if err:
        raise RuntimeError(err)
    _, err = await gql(
        "mutation XtmPlaybookRun($id: ID!, $input: [EditInput!]!) { playbookFieldPatch(id: $id, input: $input) { id } }",
        {"id": created, "input": [{"key": "playbook_running", "value": ["true"]}]},
    )
    if err:
        raise RuntimeError(err)
    return str(created)


async def _delete(gql: Gql, field: str, object_id: str) -> None:
    _, err = await gql(f"mutation XtmDelete($id: ID!) {{ {field}(id: $id) }}", {"id": object_id})
    if err and "not found" not in err.lower():
        raise RuntimeError(err)


async def reconcile(
    gql: Gql,
    spec: dict[str, Any],
    state: dict[str, Any] | None,
    *,
    url: str,
    secret: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bring OpenCTI to *spec*; returns the new state (never raises)."""
    state = dict(state or {})
    stamp = (now or datetime.now(UTC)).isoformat()
    name = object_name(spec)
    try:
        label_ids, missing = await _resolve_label_ids(gql, spec["labels"])
        if missing:
            return {**state, "status": "error", "error": f"labels not found in OpenCTI: {missing}", "checked_at": stamp}
        country_ids, missing_regions = await _resolve_country_ids(gql, spec.get("regions") or [])
        sector_ids, missing_sectors = await _resolve_sector_ids(gql, spec.get("sectors") or [])
        if missing_regions or missing_sectors:
            unresolved = [f"regions {missing_regions}"] if missing_regions else []
            unresolved += [f"sector {missing_sectors}"] if missing_sectors else []
            return {
                **state,
                "status": "error",
                "error": f"organization context not found in OpenCTI: {', '.join(unresolved)}",
                "checked_at": stamp,
            }
        organization_ids: list[str] = []
        receiver: list[dict[str, Any]] = []
        if spec.get("organizations", PRIMARY_SCOPE) != PRIMARY_SCOPE:
            organization_ids, receiver, missing_org_sectors = await _organization_scope(gql, spec)
            if missing_org_sectors:
                return {
                    **state,
                    "status": "error",
                    "error": f"tracked organizations' sectors not found in OpenCTI: {missing_org_sectors}",
                    "checked_at": stamp,
                }
        if spec.get("mode") == PIR_PLAYBOOK_MODE:
            config = notifier_configuration(url, secret, template=PIR_WEBHOOK_TEMPLATE)
            notifier_id = await _ensure_notifier(gql, state.get("notifier_id"), name, config)
            if state.get("trigger_id"):
                # The assignment moved from a live trigger to the PIR playbook.
                await _delete(gql, "triggerKnowledgeDelete", state["trigger_id"])
            member_id, member_name = await _own_user(gql)
            pir_names = await _pir_names(gql, list(spec.get("pir_ids") or []))
            entry, notify = pir_playbook_nodes(spec, notifier_id, member_id, member_name, pir_names)
            playbook_id = await _ensure_pir_playbook(gql, state.get("playbook_id"), name, entry, notify)
            return {
                "status": "provisioned",
                "error": None,
                "name": name,
                "mode": PIR_PLAYBOOK_MODE,
                "notifier_id": notifier_id,
                "playbook_id": playbook_id,
                # Chip labels are display only: a renamed PIR or user is not a new provisioning.
                "fingerprint": fingerprint(
                    name,
                    json.dumps(
                        [
                            _without_chip_labels(PIR_ENTRY_COMPONENT, entry),
                            _without_chip_labels(NOTIFIER_COMPONENT, notify),
                        ],
                        sort_keys=True,
                    ),
                    url,
                ),
                "checked_at": stamp,
            }
        config = notifier_configuration(url, secret)
        filters = trigger_filters(spec, label_ids, country_ids + sector_ids, organization_ids)
        notifier_id = await _ensure_notifier(gql, state.get("notifier_id"), name, config)
        if state.get("playbook_id"):
            # The assignment moved from the PIR playbook back to a live trigger.
            await _delete(gql, "playbookDelete", state["playbook_id"])
        trigger_id = await _ensure_trigger(
            gql, state.get("trigger_id"), name, filters, spec["event_types"], notifier_id
        )
    except Exception as exc:  # one assignment never breaks the pass
        return {**state, "status": "error", "error": str(exc)[:500], "checked_at": stamp}
    provisioned = {
        "status": "provisioned",
        "error": None,
        "name": name,
        "notifier_id": notifier_id,
        "trigger_id": trigger_id,
        "fingerprint": fingerprint(name, filters, ",".join(spec["event_types"]), url),
        "checked_at": stamp,
    }
    if receiver:
        provisioned["organizations"] = receiver
    return provisioned


async def remove(gql: Gql, state: dict[str, Any]) -> str | None:
    """Delete the provisioned trigger or playbook, then notifier; the error text, ``None`` when done."""
    try:
        if state.get("trigger_id"):
            await _delete(gql, "triggerKnowledgeDelete", state["trigger_id"])
        if state.get("playbook_id"):
            await _delete(gql, "playbookDelete", state["playbook_id"])
        if state.get("notifier_id"):
            await _delete(gql, "notifierDelete", state["notifier_id"])
    except Exception as exc:
        return str(exc)[:500]
    return None


def due_for_check(state: Any, now: datetime) -> bool:
    if not isinstance(state, dict) or state.get("status") != "provisioned":
        return True
    try:
        checked = datetime.fromisoformat(str(state.get("checked_at")))
    except ValueError:
        return True
    return now - checked >= timedelta(seconds=RECHECK_SECONDS)


# ── Database side ─────────────────────────────────────────────────────────────


def bound_opencti_integration_id(assignment: Any, agent: Any) -> str | None:
    """The OpenCTI integration the assignment binds (its own binding, else the agent's)."""
    from app.services.trigger_provisioning import direct_binding_id

    return direct_binding_id(assignment, agent, "opencti")


async def opencti_integration_id(db: Any, assignment: Any, agent: Any) -> tuple[str | None, str]:
    """``(integration_id, "")`` the assignment provisions and reads through, else ``(None, reason)``.

    A direct binding first; else the agent's ``opencti`` template binding,
    resolved for the provisioning run-as user as a run would resolve it
    (never the seed owner, #9188).
    """
    from app.services.fleet_run_as import NO_RUN_AS_REASON, provisioning_user_id
    from app.services.trigger_provisioning import template_integration_id, uses_template

    integration_id = bound_opencti_integration_id(assignment, agent)
    if integration_id:
        return integration_id, ""
    if not uses_template(agent, "opencti"):
        return None, "no OpenCTI integration bound to the assignment or its agent"
    user_id = provisioning_user_id(assignment, agent)
    if user_id is None:
        return None, NO_RUN_AS_REASON
    return await template_integration_id(db, agent, "opencti", "OpenCTI", user_id)


async def _connection(db: Any, assignment: Any, agent: Any) -> tuple[Gql, str, str, str] | str:
    """``(gql, integration_id, base_url, secret)`` or the reason it cannot be built."""
    from app.models.integration import Integration
    from app.services.trigger_provisioning import receiver_secret

    integration_id, reason = await opencti_integration_id(db, assignment, agent)
    if not integration_id:
        return reason
    integration = await db.get(Integration, uuid.UUID(integration_id))
    if integration is None or integration.provider != "opencti":
        return f"OpenCTI integration {integration_id} not found"
    # The receiver checks X-OpenCTI-Token against this reading, a vault reference resolved (#6080).
    try:
        # A platform-managed integration gets its secret generated here (#970).
        secret = await receiver_secret(integration)
    except Exception as exc:  # unreadable config, unreadable vault secret or unusable secret
        return f"the OpenCTI integration's webhook_secret cannot be read: {type(exc).__name__}"
    if not secret:
        return "the OpenCTI integration has no webhook_secret: set one so the receiver authenticates OpenCTI"
    from app.services.fleet_run_as import NO_RUN_AS_REASON, provisioning_user_id

    # A fleet automation never provisions as the seed owner (#9188).
    user_id = provisioning_user_id(assignment, agent)
    if user_id is None:
        logger.warning("OpenCTI provisioning of assignment %s skipped: %s", assignment.id, NO_RUN_AS_REASON)
        return NO_RUN_AS_REASON
    connection = await integration_gql(db, integration, user_id)
    if isinstance(connection, str):
        return connection
    gql, api_base = connection
    return gql, integration_id, api_base, secret


async def integration_gql(db: Any, integration: Any, user_id: Any, *, timeout: int = 30) -> tuple[Gql, str] | str:
    """``(gql, base_url)`` on one OpenCTI integration for ``user_id``, or the reason it cannot be built.

    The caller hands the integration a trigger is bound to (the assignment's or
    its agent's binding, or the one the receiver authenticated): provisioning
    here, and the CTEM-08 PIR score rule, which re-reads a delivered entity's
    live score with the same integration (services/ctem_trigger_rules.py).
    """
    from app.integrations.providers.opencti._common import _client, _graphql
    from app.services.integration_tools import _get_token

    token = await _get_token(
        db, str(integration.id), "opencti", user_id=str(user_id) if user_id else None, integ=integration
    )
    if not token or "||" not in token:
        return "no OpenCTI token could be resolved for the integration"
    api_base, _, api_token = token.partition("||")
    api_base = api_base.strip().rstrip("/")
    headers: Any = {"Authorization": f"Bearer {api_token.strip()}", "Content-Type": "application/json"}
    if getattr(integration, "auth_type", None) == "platform_managed":
        from app.integrations.providers.opencti.capabilities import attach_probe

        # A registered platform is called with the run-as user's own JWT: a
        # query their role cannot pass (notifiers need SETTINGS_SETCUSTOMIZATION)
        # is never sent, or OpenCTI logs it as their unauthorized read. A
        # company-managed static key is called exactly as before.
        headers = attach_probe(headers, api_base)

    async def gql(query: str, variables: dict | None = None) -> tuple[dict, str | None]:
        async with _client(headers, timeout=timeout) as client:
            return await _graphql(client, api_base, headers, query, variables)

    return gql, api_base


def _with_state(trigger_config: dict, state: dict | None) -> dict:
    updated = dict(trigger_config)
    if state is None:
        updated.pop(STATE_KEY, None)
    else:
        updated[STATE_KEY] = state
    return updated


async def provision_one(db: Any, assignment: Any, agent: Any, *, now: datetime | None = None) -> dict | None:
    """Reconcile one assignment; writes and returns its new state (``None`` = nothing held)."""
    from app.core.config import settings

    now = now or datetime.now(UTC)
    config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
    state = config.get(STATE_KEY) if isinstance(config.get(STATE_KEY), dict) else None
    try:
        spec = read_provision_spec(config) if assignment.enabled else None
    except ProvisionSpecError as exc:
        new_state = {**(state or {}), "status": "error", "error": str(exc), "checked_at": now.isoformat()}
        assignment.trigger_config = _with_state(config, new_state)
        return new_state
    conn = await _connection(db, assignment, agent)
    if spec is None:
        if not state:
            return None
        if isinstance(conn, str):
            new_state = {**state, "status": "orphaned", "error": conn, "checked_at": now.isoformat()}
            assignment.trigger_config = _with_state(config, new_state)
            return new_state
        error = await remove(conn[0], state)
        new_state = None if error is None else {**state, "status": "error", "error": error, "checked_at": now.isoformat()}
        assignment.trigger_config = _with_state(config, new_state)
        return new_state
    if isinstance(conn, str):
        new_state = {**(state or {}), "status": "error", "error": conn, "checked_at": now.isoformat()}
        assignment.trigger_config = _with_state(config, new_state)
        return new_state
    gql, integration_id, _api_base, secret = conn
    if spec["organization_context"]:
        from app.services import organization_context as org_context

        row = await org_context.current_version(db)
        # The hydrated profile: its PIR fields are the PIRs enabled in the Intelligence policy (#2435).
        profile = await org_context.hydrated_profile(db, row) if row is not None else None
        spec, missing = derive_from_organization_context(spec, profile)
        if missing:
            new_state = {
                **(state or {}),
                "status": "error",
                "error": f"organization context: {', '.join(missing)} not filled "
                "(Settings > Organization); the trigger is not provisioned without them",
                "checked_at": now.isoformat(),
            }
            assignment.trigger_config = _with_state(config, new_state)
            return new_state
    if spec["organizations"] != PRIMARY_SCOPE:
        spec = await with_tracked_organizations(db, spec)
        if not spec["organization_refs"]:
            new_state = {
                **(state or {}),
                "status": "error",
                "error": "organizations: no tracked organization in this scope has monitoring on and an "
                "OpenCTI identity (Settings > Organization); the trigger is not provisioned without one",
                "checked_at": now.isoformat(),
            }
            assignment.trigger_config = _with_state(config, new_state)
            return new_state
    url = receiver_url(settings.base_url, integration_id, str(assignment.id))
    new_state = await reconcile(gql, spec, state, url=url, secret=secret, now=now)
    new_state["integration_id"] = integration_id
    assignment.trigger_config = _with_state(config, new_state)
    return new_state


async def deprovision(db: Any, assignment: Any) -> None:
    """Best-effort removal before an assignment is deleted; never raises."""
    from app.models.agent import Agent

    config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
    state = config.get(STATE_KEY)
    if not isinstance(state, dict) or not (
        state.get("trigger_id") or state.get("notifier_id") or state.get("playbook_id")
    ):
        return
    try:
        agent = await db.get(Agent, assignment.agent_id)
        conn = await _connection(db, assignment, agent)
        if isinstance(conn, str):
            logger.warning("OpenCTI provisioning of assignment %s not removed: %s", assignment.id, conn)
            return
        error = await remove(conn[0], state)
        if error:
            logger.warning("OpenCTI provisioning of assignment %s not removed: %s", assignment.id, error)
    except Exception as exc:
        logger.warning("OpenCTI provisioning of assignment %s not removed: %s", assignment.id, exc)


async def maintain(db: Any, *, now: datetime | None = None) -> int:
    """Scheduler pass: reconcile every due OpenCTI assignment; returns how many changed."""
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.assignment import Assignment

    now = now or datetime.now(UTC)
    rows = (
        await db.execute(
            select(Assignment, Agent)
            .join(Agent, Agent.id == Assignment.agent_id)
            .where(Assignment.trigger_type == "integration_event")
        )
    ).all()
    changed = 0
    for assignment, agent in rows:
        config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
        if config.get("provider") != "opencti":
            continue
        if PROVISION_KEY not in config and STATE_KEY not in config:
            continue
        wants = PROVISION_KEY in config and assignment.enabled
        if wants and not due_for_check(config.get(STATE_KEY), now):
            continue
        before = config.get(STATE_KEY)
        after = await provision_one(db, assignment, agent, now=now)
        if (before or {}).get("fingerprint") != (after or {}).get("fingerprint") or (before or {}).get(
            "status"
        ) != (after or {}).get("status"):
            changed += 1
    await db.commit()
    return changed

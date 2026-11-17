"""OpenCRQ threat catalog tools (read-only).

* ``list_opencrq_threats``          - the threat catalog (actors, intrusion
                                      sets, campaigns) with their activity.
* ``get_opencrq_threat``            - one threat: profile, targeting, CVEs,
                                      techniques and the attack outcomes it fits.
* ``list_opencrq_scenario_threats`` - the threats relevant to a risk scenario.
* ``list_opencrq_cve_threats``      - the threats citing a CVE, with their
                                      activity.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    CVE_ID_PATTERN,
    OFFSET_PROP,
    TENANT_PROP,
    _as_int,
    _client,
    _client_only_hint,
    _fmt_empty_page,
    _fmt_share,
    _grc_link,
    _page,
    _resolve_tenant,
    _text,
    _trpc,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

_USER_SESSION = " Needs a user session (a service account is refused)."

_ENTITY_TYPES = ["threat-actor", "intrusion-set", "campaign"]

_SORT_KEYS = [
    "outcomeScore",
    "activityScore",
    "name",
    "mitreGroupId",
    "cveCount",
    "techniqueCount",
    "stixCreated",
    "updatedAt",
]

#: Sort keys read A to Z; OpenCRQ sorts descending unless told otherwise.
_ASCENDING_KEYS = {"name", "mitreGroupId"}

#: The labels OpenCRQ's front gives an actor class; any other value is unclassified.
_ACTOR_CLASSES = {
    "opportunistic_crime": "opportunistic crime",
    "ransomware_operator": "ransomware operator",
    "state_sponsored": "state sponsored",
    "hacktivist": "hacktivist",
}

_PAGE_SIZE = 30

#: Mirrors OpenCRQ's definition of an active threat: observed less than this long ago.
_ACTIVE_WINDOW = timedelta(days=90)


def _fmt_activity(t: dict) -> str:
    """A threat's activity score and the date it was last observed.

    An undated threat carries no measured score (null), and ``lastActivityAt``
    falls back to an authoring date: only ``lastObservedAt`` says when the actor
    was seen.
    """
    if not t.get("lastObservedAt"):
        return "activity unknown"
    text = "activity"
    if t.get("activityScore") is not None:
        text += f" {_fmt_share(t['activityScore'])}"
    return f"{text}, last observed {str(t['lastObservedAt'])[:10]}"


def _is_active(t: dict) -> bool:
    """Whether ``lastObservedAt`` is less than ``_ACTIVE_WINDOW`` before now (a future date has age 0)."""
    raw = t.get("lastObservedAt")
    if not isinstance(raw, str):
        return False
    try:
        observed = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return max(datetime.now(UTC) - observed, timedelta(0)) < _ACTIVE_WINDOW


def _label(t: dict) -> str:
    name = t.get("name") or "?"
    mitre = t.get("mitreGroupId")
    kind = t.get("entityType") or "?"
    return f"{name} ({mitre}, {kind})" if mitre else f"{name} ({kind})"


def _fmt_threat_row(t: dict, outcome_filtered: bool = False) -> str:
    line = f"- {_label(t)}: {_fmt_activity(t)}"
    if t.get("topOutcomeName"):
        match = "match with" if outcome_filtered else "best outcome match"
        line += f"; {match} {t['topOutcomeName']} ({_fmt_share(t.get('topOutcomeScore'))})"
    counts = []
    if t.get("cveCount") is not None:
        counts.append(f"{t['cveCount']} CVEs")
    if t.get("techniqueCount") is not None:
        counts.append(f"{t['techniqueCount']} techniques")
    if counts:
        line += f"; {', '.join(counts)}"
    return f"{line} (id: {t.get('id')})"


def _threat_rank(t: dict) -> float:
    """OpenCRQ's unrounded threat score, out of 100, which is what its front ranks on.

    ``activityWeight`` is the activity factor OpenCRQ applies, a floor for an undated threat.
    """
    return (t.get("combinedRelevance") or 0) * (t.get("activityWeight") or 0) * 100


def _threat_score(t: dict) -> int:
    """The threat score as OpenCRQ shows it: ``Math.round`` of the ranked value, halves up."""
    return math.floor(_threat_rank(t) + 0.5)


def _names(rows: list[dict], key: str = "name", cap: int = 20) -> str:
    values = [str(r.get(key)) for r in rows if r.get(key)]
    shown = ", ".join(values[:cap])
    if len(values) > cap:
        shown += f", ... and {len(values) - cap} more"
    return shown


def _fmt_threat_detail(
    detail: dict, outcomes: list[dict], base_url: str, tenant_id: str
) -> str:
    a = detail.get("analysis") or {}
    threat_id = a.get("id", "N/A")
    lines = [f"[Threat] {_label(a)}", f"  ID: {threat_id}"]
    link = _grc_link(base_url, tenant_id, "threat", threat_id)
    if link:
        lines.append(link)
    lines.append(f"  Activity: {_fmt_activity(detail.get('activity') or a)}")
    actor_class = a.get("actorClassOverride") or a.get("actorClass")
    lines.append(f"  Actor class: {_ACTOR_CLASSES.get(actor_class, 'unclassified actor')}")
    for key, label in (
        ("primaryMotivation", "Primary motivation"),
        ("resourceLevel", "Resource level"),
        ("sophistication", "Sophistication"),
    ):
        if a.get(key):
            lines.append(f"  {label}: {a[key]}")
    for key, label in (("firstSeen", "First seen"), ("lastSeen", "Last seen")):
        if a.get(key):
            lines.append(f"  {label}: {str(a[key])[:10]}")
    if a.get("description"):
        lines.append(f"  Description: {str(a['description'])[:500]}")
    if detail.get("attributedTo"):
        lines.append(f"  Attributed to: {_names(detail['attributedTo'])}")
    if detail.get("threatActorTypes"):
        lines.append(f"  Actor types: {', '.join(detail['threatActorTypes'])}")
    intent = detail.get("declaredIntent") or {}
    goals = [g.get("value") for g in intent.get("goals") or [] if g.get("value")]
    if goals:
        lines.append(f"  Goals: {'; '.join(str(g) for g in goals[:5])}")
    for key, label in (("locations", "Targeted locations"), ("sectors", "Targeted sectors")):
        rows = detail.get(key) or []
        if rows:
            matching = [r.get("name") for r in rows if r.get("matchesOrganization")]
            text = _names(rows)
            if matching:
                text += f" (matching your organizations: {', '.join(matching)})"
            lines.append(f"  {label}: {text}")
    cves = detail.get("cves") or []
    if cves:
        lines.append(f"  Exploited CVEs ({len(cves)}): {_names(cves, 'cveId')}")
    techniques = detail.get("techniques") or []
    if techniques:
        shown = [
            f"{t.get('mitreExternalId')} {t.get('name')}".strip()
            for t in techniques
        ]
        text = "; ".join(shown[:20])
        if len(shown) > 20:
            text += f"; ... and {len(shown) - 20} more"
        lines.append(f"  Techniques ({len(techniques)}): {text}")
    fitting = [o for o in outcomes if o.get("score")]
    if fitting:
        ranked = sorted(fitting, key=lambda o: o["score"], reverse=True)
        lines.append("  Attack outcomes it fits:")
        for o in ranked[:10]:
            lines.append(
                f"    - {o.get('outcomeName', '?')}: {_fmt_share(o.get('score'))}"
                f" ({o.get('matchCount', 0)} matching techniques)"
            )
    return "\n".join(lines)


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def list_opencrq_threats(
        search: str = "",
        entity_type: str = "",
        attack_outcome_id: str = "",
        sort_by: str = "outcomeScore",
        page: int = 1,
        limit: int = 20,
        tenant_id: str = "",
    ) -> str:
        search, entity_type = _text(search), _text(entity_type)
        attack_outcome_id = _text(attack_outcome_id)
        sort_by = _text(sort_by) or "outcomeScore"
        if entity_type and entity_type not in _ENTITY_TYPES:
            return "entity_type must be one of: " + ", ".join(_ENTITY_TYPES) + "."
        if sort_by not in _SORT_KEYS:
            return "sort_by must be one of: " + ", ".join(_SORT_KEYS) + "."
        page, limit = _as_int(page), _as_int(limit)
        if page is None or page < 1:
            return "page must be an integer >= 1."
        if limit is None or not 1 <= limit <= 100:
            return "limit must be an integer between 1 and 100."
        tenant, err = await _resolve_tenant(base_url, headers, _text(tenant_id))
        if err:
            return err
        payload: dict = {
            "page": page,
            "limit": limit,
            "sortBy": sort_by,
            "sortDesc": sort_by not in _ASCENDING_KEYS,
        }
        if search:
            payload["search"] = search[:200]
        if entity_type:
            payload["entityType"] = entity_type
        if attack_outcome_id:
            payload["outcomeId"] = attack_outcome_id
        result, err = await _trpc(
            base_url, headers, "threats.list", "list threats",
            input=payload, tenant_id=tenant,
        )
        if err:
            return err
        items = (result or {}).get("items") or []
        total = (result or {}).get("total", len(items))
        if not items:
            return _fmt_empty_page(
                "No threats matched in the OpenCRQ threat catalog.", page, total
            )
        scope = " matching the attack outcome" if attack_outcome_id else ""
        lines = [f"Threats{scope} (page {page}, showing {len(items)} of {total}):"]
        lines += [_fmt_threat_row(t, bool(attack_outcome_id)) for t in items]
        pending = (result or {}).get("pending") or 0
        if pending:
            lines.append(
                f"{pending} threats are not scored yet: their counts and outcome "
                "match are blank, not zero."
            )
        return "\n".join(lines)

    async def get_opencrq_threat(threat_id: str = "", tenant_id: str = "") -> str:
        threat_id = _text(threat_id)
        if not threat_id:
            return "threat_id is required."
        async with _client(headers) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, _text(tenant_id), client=client
            )
            if err:
                return err
            detail, err = await _trpc(
                base_url, headers, "threats.getDetail", "get threat",
                input={"id": threat_id}, tenant_id=tenant, client=client,
            )
            if err:
                return _client_only_hint(err)
            if not detail:
                return f"Threat {threat_id} not found."
            outcomes, err = await _trpc(
                base_url, headers, "threats.getOutcomeScoresForAnalysis",
                "get threat outcome scores",
                input={"analysisId": threat_id}, tenant_id=tenant, client=client,
            )
        text = _fmt_threat_detail(detail, outcomes or [], base_url, tenant)
        if err:
            text += f"\n  (Attack outcome scores unavailable: {err})"
        return text

    async def list_opencrq_scenario_threats(
        risk_scenario_id: str = "", offset: int = 0, tenant_id: str = ""
    ) -> str:
        risk_scenario_id = _text(risk_scenario_id)
        if not risk_scenario_id:
            return "risk_scenario_id is required."
        offset = _as_int(offset)
        if offset is None or offset < 0:
            return "offset must be an integer >= 0."
        tenant, err = await _resolve_tenant(base_url, headers, _text(tenant_id))
        if err:
            return err
        result, err = await _trpc(
            base_url, headers, "threats.getRelevantForScenario",
            "get threats relevant to scenario",
            input={"scenarioId": risk_scenario_id}, tenant_id=tenant,
        )
        if err:
            return _client_only_hint(err)
        items = (result or {}).get("items") or []
        below_gate = (result or {}).get("belowGateClaimCount") or 0
        if not items:
            text = "No threat in the catalog is relevant to this risk scenario."
            if below_gate:
                text += (
                    f" {below_gate} targeting claims were left out for low "
                    "confidence."
                )
            return text
        ranked = sorted(
            items,
            key=lambda t: (_threat_rank(t), t.get("combinedRelevance") or 0),
            reverse=True,
        )
        shown, more = _page(ranked, offset, _PAGE_SIZE)
        lines = [
            f"Threats relevant to the risk scenario ({len(items)}), by threat "
            "score (relevance x activity, out of 100):"
        ]
        for t in shown:
            matches = [
                label
                for key, label in (
                    ("outcomeMatch", "attack outcome"),
                    ("countryMatch", f"country {t.get('matchedCountry') or ''}".strip()),
                    ("sectorMatch", f"sector {t.get('matchedSector') or ''}".strip()),
                )
                if t.get(key)
            ]
            lines.append(
                f"- {_label(t)}: threat score {_threat_score(t)}, relevance "
                f"{_fmt_share(t.get('combinedRelevance'))}, "
                f"matches {', '.join(matches) or 'none'}; {_fmt_activity(t)}"
                f" (id: {t.get('id')})"
            )
        if more:
            lines.append(more)
        if below_gate:
            lines.append(
                f"{below_gate} targeting claims were left out for low confidence."
            )
        return "\n".join(lines)

    async def list_opencrq_cve_threats(
        cve_id: str = "", offset: int = 0, tenant_id: str = ""
    ) -> str:
        cve_id = _text(cve_id).upper()
        if not CVE_ID_PATTERN.match(cve_id):
            return "cve_id is required, as a CVE identifier (e.g. 'CVE-2024-3400')."
        offset = _as_int(offset)
        if offset is None or offset < 0:
            return "offset must be an integer >= 0."
        tenant, err = await _resolve_tenant(base_url, headers, _text(tenant_id))
        if err:
            return err
        rows, err = await _trpc(
            base_url, headers, "threats.getByCveId", "get threats citing CVE",
            input={"cveId": cve_id}, tenant_id=tenant,
        )
        if err:
            return _client_only_hint(err)
        rows = rows or []
        if not rows:
            return f"No threat in the OpenCRQ catalog cites {cve_id}."
        active = sum(1 for t in rows if _is_active(t))
        lines = [f"Threats citing {cve_id} ({len(rows)}, {active} active):"]
        shown, more = _page(rows, offset, _PAGE_SIZE)
        lines += [_fmt_threat_row(t) for t in shown]
        if more:
            lines.append(more)
        return "\n".join(lines)

    executor.register("list_opencrq_threats", list_opencrq_threats)
    executor.register("get_opencrq_threat", get_opencrq_threat)
    executor.register("list_opencrq_scenario_threats", list_opencrq_scenario_threats)
    executor.register("list_opencrq_cve_threats", list_opencrq_cve_threats)

    return [
        ToolDefinition(
            name="list_opencrq_threats",
            description=(
                "List the OpenCRQ threat catalog (threat actors, intrusion sets, "
                "campaigns imported from threat intelligence) with each threat's "
                "activity, best-matching attack outcome and CVE / technique "
                "counts. Sorted by attack-outcome match by default."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": (
                            "Start of the threat name, case-insensitive: "
                            "'APT2' finds APT28 and APT29, 'Bear' does not "
                            "find Fancy Bear."
                        ),
                    },
                    "entity_type": {"type": "string", "enum": _ENTITY_TYPES},
                    "attack_outcome_id": {
                        "type": "string",
                        "description": (
                            "Only the threats matching this attack outcome "
                            "(UUID, see list_opencrq_attack_outcomes), with "
                            "their match with it."
                        ),
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": _SORT_KEYS,
                        "default": "outcomeScore",
                        "description": "Scores, counts and dates sort highest first, names A to Z.",
                    },
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    },
                    **TENANT_PROP,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_threat",
            description=(
                "Get one OpenCRQ threat: profile and activity, targeted countries "
                "and sectors, exploited CVEs, ATT&CK techniques, and the attack "
                "outcomes it fits." + _USER_SESSION
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_id": {
                        "type": "string",
                        "description": "Threat UUID (from list_opencrq_threats).",
                    },
                    **TENANT_PROP,
                },
                "required": ["threat_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_scenario_threats",
            description=(
                "List the threats relevant to an OpenCRQ risk scenario, ranked "
                "as OpenCRQ ranks them, by threat score (relevance times "
                "activity), with what makes each relevant (attack outcome, "
                "targeted country, targeted sector) and its activity."
                + _USER_SESSION
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "risk_scenario_id": {
                        "type": "string",
                        "description": "Risk scenario UUID.",
                    },
                    **OFFSET_PROP,
                    **TENANT_PROP,
                },
                "required": ["risk_scenario_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_cve_threats",
            description=(
                "List the threats of the OpenCRQ catalog that cite a CVE, with "
                "their activity: tells whether a vulnerability is exploited by "
                "active threats." + _USER_SESSION
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve_id": {
                        "type": "string",
                        "description": "CVE identifier, e.g. 'CVE-2024-3400'.",
                    },
                    **OFFSET_PROP,
                    **TENANT_PROP,
                },
                "required": ["cve_id"],
            },
            source=_SOURCE,
        ),
    ]

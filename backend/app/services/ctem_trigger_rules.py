# SPDX-License-Identifier: Apache-2.0

"""CTEM trigger rules (CTEM-08): "a vulnerability on software we run", checked before any model call.

An ``integration_event`` assignment on OpenCTI may declare, in its
``trigger_config``, ``"runs_in_profile": "source"``: a delivery of a
relationship (``has`` from a Software, Infrastructure or System to a
Vulnerability) then starts a run only when its source names a tech stack
product or an asset of the primary organization profile. The receiver reads
the rule after the signature check and before the trigger's filter, gate or
run: a relationship on software the organization does not run costs no model
call and is counted as filtered with its reason.

The signal itself is recorded by the generic OpenCTI listener
(``services/ctem_signal_listeners.py``) with the source in its evidence, so
the scope gate resolves it to the same product.

A second rule, ``"pir_min_score": "policy"``, reads a delivery of the
provisioned PIR playbook (``xtm_source`` ``pir_playbook``, CTEM-05): the run
starts only when the entity's PIR score reaches the threshold the
prioritization policy sets for its kind (``pir_min_score_vulnerability`` for a
Vulnerability, ``pir_min_score_threat`` for threat activity). OpenCTI delivers
the entity before it rewrites its PIR information, so the score is read live
from OpenCTI with the trigger's integration, never taken from the delivery; a
score that cannot be read drops the delivery naming why (a rule written to
narrow never widens on an outage). Design: ``dev-docs/ctem-fleets.md``
section "Trigger rules".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The trigger_config key, and the side of the relationship it reads.
RULE_KEY = "runs_in_profile"
RULE_SIDES: tuple[str, ...] = ("source",)

#: The trigger_config key of the PIR score rule; its one value reads the thresholds of the policy.
PIR_RULE_KEY = "pir_min_score"
PIR_RULE_VALUES: tuple[str, ...] = ("policy",)
#: The OpenCTI entity types whose PIR score is held to the threat threshold (the signal table's threats).
THREAT_TYPES: frozenset[str] = frozenset(
    {"intrusion-set", "threat-actor", "threat-actor-group", "threat-actor-individual", "campaign", "malware"}
)
_LIVE_PIR_QUERY = (
    "query XtmPirScore($id: String!, $pirId: ID!) {"
    " stixDomainObject(id: $id) { id pirInformation(pirId: $pirId) { pir_score } } }"
)


def rule_of(trigger_config: object) -> str:
    """The side a trigger's rule reads, ``""`` when it declares none (an unknown side is no rule)."""
    if not isinstance(trigger_config, Mapping):
        return ""
    side = trigger_config.get(RULE_KEY)
    return side if isinstance(side, str) and side in RULE_SIDES else ""


def _octi_extension(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """The extension that names the relationship's source (OpenCTI's ``extension-definition--...``)."""
    ext = data.get("extensions")
    if not isinstance(ext, Mapping):
        return {}
    return next((v for v in ext.values() if isinstance(v, Mapping) and "source_value" in v), {})


def source_name(payload: object) -> str:
    """The name of the source of the relationship an OpenCTI delivery carries, ``""`` when none.

    A provisioned notifier posts the whole STIX relationship as ``data``: its
    OpenCTI extension names the source (``source_value``, a Software name or
    CPE, an Infrastructure or System name).
    """
    if not isinstance(payload, Mapping):
        return ""
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return ""
    value = _octi_extension(data).get("source_value") or data.get("source_value")
    return value.strip() if isinstance(value, str) else ""


def drop_reason(profile: Any, payload: object) -> str | None:
    """Why the rule drops *payload* for *profile* (``None``: the source runs here, the run starts).

    Pure: *profile* is the primary ``OrganizationProfile`` (``None`` before
    its first save: nothing runs here yet).
    """
    from app.services.ctem_scope_gate import runs_in_profile

    name = source_name(payload)
    if not name:
        return "runs_in_profile: the delivery names no relationship source"
    if profile is None:
        return "runs_in_profile: the organization profile is not saved yet"
    if not runs_in_profile(profile, name):
        return f"runs_in_profile: {name[:120]} is not in the tech stack or the assets"
    return None


def pir_rule_of(trigger_config: object) -> bool:
    """Whether a trigger declares the PIR score rule (an unknown value is no rule)."""
    if not isinstance(trigger_config, Mapping):
        return False
    return trigger_config.get(PIR_RULE_KEY) in PIR_RULE_VALUES


def pir_kind(payload: object) -> str:
    """``vulnerability``, ``threat`` or ``""``: which policy threshold a PIR playbook delivery is held to."""
    if not isinstance(payload, Mapping) or payload.get("xtm_source") != "pir_playbook":
        return ""
    data = payload.get("data")
    kind = data.get("type") if isinstance(data, Mapping) else None
    kind = kind.strip().lower() if isinstance(kind, str) else ""
    if kind == "vulnerability":
        return "vulnerability"
    return "threat" if kind in THREAT_TYPES else ""


def delivered_pir_ids(payload: object) -> list[str]:
    """The PIRs a PIR playbook delivery says the entity is flagged in (ids only; the score is read live)."""
    from app.services.ctem_signal_listeners import _pir_matches

    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), Mapping):
        return []
    return [p["pir_id"] for p in _pir_matches(payload["data"], payload)]


def type_drop_reason(trigger_config: object, payload: object) -> str | None:
    """Why a PIR playbook delivery is not for this listener: its entity is not of the provision's types.

    A PIR playbook delivers every entity flagged in the profile PIRs; two
    listeners on the same PIRs (vulnerabilities, threat activity) each keep
    their own ``provision.entity_types``. No types declared: every kind is kept.
    """
    provision = trigger_config.get("provision") if isinstance(trigger_config, Mapping) else None
    types = provision.get("entity_types") if isinstance(provision, Mapping) else None
    wanted = {t.strip().lower() for t in types if isinstance(t, str)} if isinstance(types, list) else set()
    data = payload.get("data") if isinstance(payload, Mapping) else None
    kind = data.get("type") if isinstance(data, Mapping) else None
    kind = kind.strip().lower() if isinstance(kind, str) else ""
    if not wanted or kind in wanted:
        return None
    return f"pir_min_score: {kind or 'an entity'} is not one of this listener's entity types"


def threshold_for(policy: Any, kind: str) -> int:
    """The policy's minimum PIR score for *kind* (the policy defaults when no profile is saved)."""
    from app.services.organization_context import PrioritizationPolicy

    policy = policy if policy is not None else PrioritizationPolicy()
    if kind == "vulnerability":
        return int(policy.pir_min_score_vulnerability)
    return int(policy.pir_min_score_threat)


#: The evidence key and event field (#1678): a relevant threat signal whose delivery named a PIR
#: score at or above the policy's ``pir_min_score_threat``. The seeded threat signal run filters on it.
PIR_FLOOR_KEY = "pir_floor_met"


def pir_floor_met(pirs: object, threshold: int) -> bool:
    """Whether one PIR match (``[{pir_id, score_at_delivery}]``) reaches *threshold*. Pure.

    The score is the one at delivery, possibly the previous one (see
    ``ctem_signal_listeners._pir_matches``): the run it starts reads the live
    score before acting. No PIR match is below the floor.
    """
    for pir in pirs if isinstance(pirs, list) else []:
        score = pir.get("score_at_delivery") if isinstance(pir, Mapping) else None
        if isinstance(score, int | float) and not isinstance(score, bool) and score >= threshold:
            return True
    return False


def pir_drop_reason(kind: str, threshold: int, live_scores: Mapping[str, float | None]) -> str | None:
    """Why the PIR score rule drops a delivery, from the live score per PIR (``None``: the run starts).

    Pure. The run starts when the entity reaches *threshold* in at least one
    PIR; a PIR whose score could not be read counts as below.
    """
    if not kind:
        return None
    if not live_scores:
        return "pir_min_score: the delivery names no PIR the entity is flagged in"
    known = [s for s in live_scores.values() if isinstance(s, int | float) and not isinstance(s, bool)]
    if any(s >= threshold for s in known):
        return None
    if not known:
        return "pir_min_score: the live PIR score could not be read from OpenCTI"
    return f"pir_min_score: {kind} PIR score {max(known):g} is below the policy threshold {threshold}"


async def _live_gql(db: Any, trigger_config: object, agent: Any, receiver_integration_id: Any) -> Any:
    """A GraphQL caller on the trigger's OpenCTI integration, or the reason it cannot be built."""
    import uuid

    from app.models.integration import Integration
    from app.services.integration_credentials import agent_acting_user_id
    from app.services.opencti_trigger_provisioning import integration_gql
    from app.services.trigger_integration_access import trigger_integration_id

    integration_id = receiver_integration_id or trigger_integration_id(trigger_config, agent)
    if not integration_id and isinstance(trigger_config, dict):
        # A template-bound agent binds no id: the provisioner's resolved one is
        # the integration the trigger was provisioned on and delivers through.
        from app.services.opencti_trigger_provisioning import STATE_KEY

        state = trigger_config.get(STATE_KEY)
        integration_id = state.get("integration_id") if isinstance(state, dict) else None
    if isinstance(integration_id, list):
        integration_id = integration_id[0] if integration_id else None
    try:
        integration = await db.get(Integration, uuid.UUID(str(integration_id))) if integration_id else None
    except ValueError:
        integration = None
    if integration is None or integration.provider != "opencti":
        return "no OpenCTI integration to read the live PIR score with"
    # The token goes through the provisioner's sanctioned resolver (the tool
    # funnel's credential gate), on the one integration this trigger is bound to.
    connection = await integration_gql(db, integration, agent_acting_user_id(agent), timeout=15)
    if isinstance(connection, str):
        return "no OpenCTI token to read the live PIR score with"
    return connection[0]


async def live_pir_scores(gql: Any, entity_id: str, pir_ids: list[str]) -> dict[str, float | None]:
    """The entity's live score in each PIR (``None`` for a PIR OpenCTI did not answer)."""
    scores: dict[str, float | None] = {}
    for pir_id in pir_ids[:10]:
        try:
            data, err = await gql(_LIVE_PIR_QUERY, {"id": entity_id, "pirId": pir_id})
        except Exception:  # a transport failure reads as no score: the rule drops, naming why
            data, err = {}, "unreachable"
        node = (data or {}).get("stixDomainObject") if not err else None
        info = node.get("pirInformation") if isinstance(node, dict) else None
        score = info.get("pir_score") if isinstance(info, dict) else None
        scores[pir_id] = score if isinstance(score, int | float) and not isinstance(score, bool) else None
    return scores


async def _primary_profile(db: Any) -> Any:
    from app.services import organization_context as org_context
    from app.services.organizations import get_primary

    primary = await get_primary(db)
    if primary is None:
        return None
    row, profile = await org_context.current_profile(db, primary.id)  # assets from the records (#1543)
    return profile if row else None


async def pir_filter_reason(
    db: Any, trigger_config: object, payload: object, *, agent: Any = None, receiver_integration_id: Any = None
) -> str | None:
    """:func:`pir_drop_reason` for a trigger declaring the PIR rule, the live score read; ``None`` otherwise."""
    if not pir_rule_of(trigger_config):
        return None
    if not isinstance(payload, Mapping) or payload.get("xtm_source") != "pir_playbook":
        return None
    wrong_type = type_drop_reason(trigger_config, payload)
    if wrong_type is not None:
        return wrong_type
    kind = pir_kind(payload)
    if not kind:
        # Fail closed: a playbook delivery of a type with no policy threshold never starts a run.
        data = payload.get("data")
        raw = data.get("type") if isinstance(data, Mapping) else None
        name = raw.strip().lower() if isinstance(raw, str) and raw.strip() else "an entity"
        return f"pir_min_score: {name} has no PIR score threshold in the policy"
    pir_ids = delivered_pir_ids(payload)
    if not pir_ids:
        return pir_drop_reason(kind, 0, {})
    profile = await _primary_profile(db)
    threshold = threshold_for(profile.prioritization_policy if profile is not None else None, kind)
    data = payload.get("data") if isinstance(payload, Mapping) else None
    entity_id = data.get("id") if isinstance(data, Mapping) else None
    if not isinstance(entity_id, str) or not entity_id:
        return "pir_min_score: the delivery names no entity id"
    gql = await _live_gql(db, trigger_config, agent, receiver_integration_id)
    if isinstance(gql, str):
        return f"pir_min_score: {gql}"
    return pir_drop_reason(kind, threshold, await live_pir_scores(gql, entity_id, pir_ids))


async def filter_reason(
    db: Any, trigger_config: object, payload: object, *, agent: Any = None, receiver_integration_id: Any = None
) -> str | None:
    """Why a CTEM trigger rule drops the delivery (the profile rule, then the PIR score rule); ``None``: it runs."""
    if rule_of(trigger_config):
        reason = drop_reason(await _primary_profile(db), payload)
        if reason is not None:
            return reason
    return await pir_filter_reason(
        db, trigger_config, payload, agent=agent, receiver_integration_id=receiver_integration_id
    )

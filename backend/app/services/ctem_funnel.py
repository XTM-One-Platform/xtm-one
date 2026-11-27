# SPDX-License-Identifier: Apache-2.0

"""The CTEM prioritization funnel: how the raw vulnerability volume narrows to
what needs action (#11546, XSIAM 4.0 parity, proposal A).

Pure: no I/O. The collector gathers the counts (``ctem_metrics_collector``) and
this module turns them into ordered layers, each with its count, its state and
the call to action that unlocks it when its input is missing.

Honesty rule: a layer whose input is not configured has no count (``None``,
never 0) and never counts as filtered: the layers below it read the last live
layer instead. Counts never increase down the funnel; a source that answers a
larger number for a narrower layer (two OpenCTI queries read at different
instants) is capped at the layer above, and the cap is flagged.

Layers, in order (the PR body carries the same table):

* ``known``: every vulnerability the user can read in OpenCTI; the OpenAEV
  CVE findings ride along as the asset-side entry when OpenAEV is connected.
* ``relevant``: vulnerabilities related to software matching the organization
  software inventory (CPE, or product and vendor). Not configured without one.
* ``exploitable``: known-exploited (CISA KEV) or EPSS above ``EPSS_THRESHOLD``,
  on the last live layer.
* ``exposed``: on a reachable asset, by the rule of
  :mod:`app.services.asset_reachability` (a vulnerability drops only when
  every asset it sits on is proven unreachable; a user endpoint is always
  reachable; ``unknown`` stays in). Live when the caller joined the counted
  vulnerabilities to the profile assets; not available, with the reachability
  split of the assets read, when the profile holds assets but no join exists
  for the counted set; not configured only when the profile holds no asset.
* ``business_impact``: the exploitable vulnerabilities of the software an
  inventory asset supporting a primary asset runs (a direct link, a link
  through an asset group, or a rule: ``organization_primary_asset_links`` read by
  :func:`app.services.ctem_primary_assets.load_index`, the index the scope gate
  reads). Not configured only when no asset is linked to a primary asset; not
  available while the count cannot be read (no software inventory, OpenCTI
  unanswered).
* ``policy``: that set narrowed by the saved prioritization policy, with the
  scope gate's rules: CVSS and EPSS minimums (an unscored vulnerability passes
  only when ``missing_score`` is ``pass``, KEV passes when ``kev_always_in``),
  product and asset-group exclusions, the unscoped accepted risks on a CVE not
  expired, and the minimum primary asset criticality. Not configured only when
  no policy was saved. Without a primary asset link it narrows the exploitable
  layer (``basis``).
* ``open_issues``: the CTEM Cases, filled by the UI from the Cases API.

:func:`funnel_scope` is the pure part of the two organization layers: which
software of the inventory each one keeps and the OpenCTI filters of the policy.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

#: EPSS above which a vulnerability counts as likely exploited (the collector's
#: ``_F_HIGH_EPSS`` and ``_F_EXPLOITABLE`` filters use the same value).
EPSS_THRESHOLD = 0.5

LIVE = "live"
NOT_CONFIGURED = "not_configured"
NOT_AVAILABLE = "not_available"
#: Why ``exposed`` is not available: the profile holds assets, none joined to the counted vulnerabilities.
EXPOSED_NOT_LINKED = "assets_not_linked"

#: Why ``business_impact`` or ``policy`` is configured yet has no count: the organization has no software inventory.
SCOPE_NO_SOFTWARE = "no_software"
#: ... or OpenCTI did not answer the count.
SCOPE_UNREAD = "unread"

#: Layer key -> (source, the organization context category that unlocks it, or None).
LAYERS: tuple[tuple[str, str, str | None], ...] = (
    ("known", "opencti", None),
    ("relevant", "organization", "software"),
    ("exploitable", "opencti", None),
    ("exposed", "organization", "assets"),
    ("business_impact", "organization", "primary_assets"),
    ("policy", "organization", "prioritization_policy"),
    ("open_issues", "ctem_cases", None),
)


_CRITICALITY = ("low", "medium", "high", "critical")
_CVE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)
#: Most accepted CVE ids a policy filter carries (the rest stay counted, said in ``accepted_unfiltered``).
MAX_ACCEPTED_FILTER = 200


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def policy_rules(policy: Any) -> list[str]:
    """The rules of *policy* (a :class:`PrioritizationPolicy`) the funnel applies; ``[]`` when none was set."""
    if policy is None:
        return []
    rules: list[str] = []
    if getattr(policy, "cvss_min", None) is not None:
        rules.append("cvss_min")
    if getattr(policy, "epss_min", None) is not None:
        rules.append("epss_min")
    if any(e.kind == "product" for e in getattr(policy, "exclusions", []) or []):
        rules.append("product_exclusions")
    if any(e.kind == "asset_group" for e in getattr(policy, "exclusions", []) or []):
        rules.append("asset_group_exclusions")
    if getattr(policy, "accepted_risks", None):
        rules.append("accepted_risks")
    if getattr(policy, "min_primary_asset_criticality", ""):
        rules.append("min_primary_asset_criticality")
    return rules


def _score_filter(key: str, minimum: float, kev: bool, *, keep_unscored: bool) -> dict[str, Any]:
    """*key* at least *minimum*, KEV kept when *kev*; an unscored vulnerability kept only when
    *keep_unscored* (``missing_score`` ``pass``: the gate holds it under ``hold``)."""
    filters: list[dict[str, Any]] = [{"key": [key], "values": [str(minimum)], "operator": "gte"}]
    if keep_unscored:
        filters.append({"key": [key], "values": [], "operator": "nil"})
    if kev:
        filters.append({"key": ["x_opencti_cisa_kev"], "values": ["true"]})
    return {"mode": "or", "filters": filters, "filterGroups": []}


def funnel_scope(
    *,
    profile: Any,
    index: Any,
    stack: list[dict[str, Any]],
    today: date,
) -> dict[str, dict[str, Any]]:
    """What the ``business_impact`` and ``policy`` layers keep, from the live configuration. Pure.

    *profile* is the :class:`OrganizationProfile`, *index* the
    :class:`~app.services.ctem_primary_assets.PrimaryAssetIndex` of its
    organization (``None``: not read), *stack* the software inventory the
    relevant layer matches (``_organization_software``). Answers
    ``{"business": {...}, "policy": {...}}``; each holds ``configured``,
    ``labels`` (the stack labels whose vulnerabilities it counts, ``None``
    when it narrows no software), ``reason`` (``no_software``) when configured
    without a software inventory, ``empty`` when the inventory is there and
    the scope keeps none of it (the layer then reads a live 0),
    and its facts (primary assets and supporting assets reached; the rules and
    the OpenCTI ``filter_groups`` of the policy).
    """
    reachers = list(getattr(index, "reachers", None) or [])
    primaries = {_norm(p.name)[:200]: p for p in getattr(profile, "primary_assets", None) or []}
    policy = getattr(profile, "prioritization_policy", None)
    rules = policy_rules(policy)

    def labels_run_by(holders: list[Any]) -> list[str]:
        """The stack labels whose product one of *holders* runs (either direction, whole word).

        Looked up by token (:class:`ProductIndex`), never as a products x stack
        scan: that scan blocked the event loop for minutes on a real inventory (#2032).
        """
        from app.services.ctem_primary_assets import ProductIndex

        products = ProductIndex(p for r in holders for p in r.products.values())
        if not products.values:
            return []
        verdicts: dict[str, bool] = {}
        out: list[str] = []
        for e in stack:
            ref = e.get("product") or ""
            if ref not in verdicts:
                verdicts[ref] = products.any_match(ref)
            if verdicts[ref]:
                out.append(e["label"])
        return out

    business: dict[str, Any] = {"configured": bool(reachers)}
    if reachers:
        business |= {
            "primary_assets": len({link.primary_asset_key for r in reachers for link, _k, _t in r.links}),
            "supporting_assets": len(reachers),
            "truncated": bool(getattr(index, "truncated", False)),
        }
        labels = labels_run_by(reachers) if stack else []
        business["labels"] = labels or None
        _mark_empty(business, labels, stack)
        business["basis"] = "exploitable"

    out_policy: dict[str, Any] = {"configured": bool(rules)}
    if not rules or policy is None:
        return {"business": business, "policy": out_policy}
    excluded_products = {_norm(e.value) for e in policy.exclusions if e.kind == "product"}
    excluded_groups = {_norm(e.value) for e in policy.exclusions if e.kind == "asset_group"}
    floor = policy.min_primary_asset_criticality
    if reachers:
        kept: list[Any] = []
        for r in reachers:
            links = [
                (link, kind, through)
                for link, kind, through in r.links
                if not (kind == "asset_group" and _norm(through) in excluded_groups)
                and (
                    not floor
                    or _CRITICALITY.index(getattr(primaries.get(link.primary_asset_key), "criticality", "high"))
                    >= _CRITICALITY.index(floor)
                )
            ]
            if links:
                kept.append(r)
        candidates = labels_run_by(kept) if stack else []
        basis = "business_impact"
    else:
        candidates = [e["label"] for e in stack]
        basis = "exploitable"
    labels = [
        label
        for label in candidates
        if not any(x and (x == _norm(label) or x in _norm(label).split()) for x in excluded_products)
    ]
    groups: list[dict[str, Any]] = []
    keep_unscored = policy.missing_score == "pass"
    if policy.cvss_min is not None:
        groups.append(
            _score_filter(
                "x_opencti_cvss_base_score", policy.cvss_min, policy.kev_always_in, keep_unscored=keep_unscored
            )
        )
    if policy.epss_min is not None:
        groups.append(
            _score_filter("x_opencti_epss_score", policy.epss_min, policy.kev_always_in, keep_unscored=keep_unscored)
        )
    accepted = sorted(
        {
            r.subject.strip().upper()
            for r in policy.accepted_risks
            if not r.scope.strip() and r.expires_on >= today and _CVE.match(r.subject.strip())
        }
    )
    if accepted:
        groups.append(
            {
                "mode": "and",
                "filters": [{"key": ["name"], "values": accepted[:MAX_ACCEPTED_FILTER], "operator": "not_eq"}],
                "filterGroups": [],
            }
        )
    out_policy |= {
        "rules": rules,
        "basis": basis,
        "labels": labels or None,
        "filter_groups": groups,
        "accepted_filtered": min(len(accepted), MAX_ACCEPTED_FILTER),
        "accepted_unfiltered": max(len(accepted) - MAX_ACCEPTED_FILTER, 0),
    }
    _mark_empty(out_policy, labels, stack)
    return {"business": business, "policy": out_policy}


def _mark_empty(part: dict[str, Any], labels: list[str], stack: list[dict[str, Any]]) -> None:
    """A scope that keeps no software: ``no_software`` without an inventory, else ``empty`` (a live 0)."""
    if labels:
        return
    if stack:
        part["empty"] = True
    else:
        part["reason"] = SCOPE_NO_SOFTWARE


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def build_funnel(
    *,
    known: Any,
    relevant: Any = None,
    exploitable_known: Any = None,
    exploitable_relevant: Any = None,
    asset_findings: Any = None,
    filters: dict[str, dict[str, Any] | None] | None = None,
    reachability: dict[str, int] | None = None,
    exposed: Any = None,
    observed: Any = None,
    business: Any = None,
    business_scope: dict[str, Any] | None = None,
    policy: Any = None,
    policy_scope: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The funnel from the collected counts, ``None`` without a known count.

    ``relevant`` and ``exploitable_relevant`` are ``None`` when the profile has
    no software inventory (or OpenCTI could not answer the tailored query): the
    ``relevant`` layer is then not configured and ``exploitable`` reads the
    known vulnerabilities (``exploitable_known``).

    ``filters`` maps a layer key to the OpenCTI FilterGroup of the list it
    counts (the stage page opens that list in OpenCTI); a live layer without
    one carries ``filters: None``: the known layer links to the unfiltered
    list, a narrower layer is not a link (#11954), since the whole list is a
    wider set than its count.

    ``reachability`` is the split of the profile assets
    (:func:`~app.services.asset_reachability.reachability_split`), ``None`` or
    a total of 0 when the profile holds no asset; ``exposed`` is the count of
    the last live layer's vulnerabilities found on an asset not proven
    unreachable, ``None`` when they could not be joined to the profile assets;
    ``observed`` the count of them found on any asset (a vulnerability found on
    no asset is never exposed, #2268), shown as ``observed`` on the layer.

    ``business_scope`` and ``policy_scope`` are the ``business`` and ``policy``
    parts of :func:`funnel_scope` (``None``: not configured); ``business`` and
    ``policy`` the counts OpenCTI answered for them (``None``: not read).
    """
    filters = filters or {}
    known_n = _count(known)
    if known_n is None:
        return None
    layers: list[dict[str, Any]] = []
    previous = known_n
    capped = False

    def add(key: str, count: int | None, state: str, *, basis: str | None = None, extra: dict | None = None) -> None:
        nonlocal previous, capped
        source, category = next((s, c) for k, s, c in LAYERS if k == key)
        if state == LIVE and count is not None:
            if count > previous:
                count, capped = previous, True
            drop = previous - count
            previous = count
        else:
            count, drop = None, None
        layers.append(
            {
                "key": key,
                "count": count,
                "drop": drop,
                "state": state,
                "source": source,
                "category": category if state == NOT_CONFIGURED else None,
                "basis": basis,
                "filters": filters.get(key) if state == LIVE else None,
                **(extra or {}),
            }
        )

    add("known", known_n, LIVE, extra={"asset_findings": _count(asset_findings)})
    # ``add`` sets ``drop`` against the layer above; the first layer has none.
    layers[0]["drop"] = None

    relevant_n = _count(relevant)
    tailored = relevant_n is not None
    add("relevant", relevant_n, LIVE if tailored else NOT_CONFIGURED)

    exploitable_n = _count(exploitable_relevant) if tailored else _count(exploitable_known)
    add(
        "exploitable",
        exploitable_n,
        LIVE if exploitable_n is not None else NOT_AVAILABLE,
        basis="relevant" if tailored else "known",
    )
    split = reachability if reachability and reachability.get("total") else None
    exposed_n = _count(exposed) if split else None
    if split is None:
        add("exposed", None, NOT_CONFIGURED)
    elif exposed_n is None:
        add("exposed", None, NOT_AVAILABLE, extra={"reason": EXPOSED_NOT_LINKED, "reachability": dict(split)})
    else:
        add("exposed", exposed_n, LIVE, extra={"reachability": dict(split), "observed": _count(observed)})
    for key, count, scope in (("business_impact", business, business_scope), ("policy", policy, policy_scope)):
        scope = scope or {}
        facts = {
            k: v
            for k, v in scope.items()
            if k not in ("configured", "reason", "labels", "filter_groups", "basis", "empty")
        }
        if not scope.get("configured"):
            add(key, None, NOT_CONFIGURED)
        elif scope.get("empty"):
            # The inventory is there and the scope keeps none of it: a live 0, never "No software inventory".
            add(key, 0, LIVE, basis=scope.get("basis"), extra=facts)
        elif _count(count) is None:
            add(key, None, NOT_AVAILABLE, extra={"reason": scope.get("reason") or SCOPE_UNREAD, **facts})
        else:
            add(key, _count(count), LIVE, basis=scope.get("basis"), extra=facts)
    add("open_issues", None, NOT_AVAILABLE)

    return {
        "layers": layers,
        # What needs action today: the narrowest live layer.
        "needs_action": previous,
        "epss_threshold": EPSS_THRESHOLD,
        "capped": capped,
    }

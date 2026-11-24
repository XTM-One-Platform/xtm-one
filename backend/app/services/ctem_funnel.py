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
* ``business_impact``, ``policy``: read the crown jewel to asset mapping and
  the prioritization policy of the organization context (proposal B). Not
  configured until those fields exist.
* ``open_issues``: the CTEM Cases (proposal C). Not available until it ships.
"""

from __future__ import annotations

from typing import Any

#: EPSS above which a vulnerability counts as likely exploited (the collector's
#: ``_F_HIGH_EPSS`` and ``_F_EXPLOITABLE`` filters use the same value).
EPSS_THRESHOLD = 0.5

LIVE = "live"
NOT_CONFIGURED = "not_configured"
NOT_AVAILABLE = "not_available"
#: Why ``exposed`` is not available: the profile holds assets, none joined to the counted vulnerabilities.
EXPOSED_NOT_LINKED = "assets_not_linked"

#: Layer key -> (source, the organization context category that unlocks it, or None).
LAYERS: tuple[tuple[str, str, str | None], ...] = (
    ("known", "opencti", None),
    ("relevant", "organization", "software"),
    ("exploitable", "opencti", None),
    ("exposed", "organization", "assets"),
    ("business_impact", "organization", "crown_jewels"),
    ("policy", "organization", "prioritization_policy"),
    ("open_issues", "ctem_cases", None),
)


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
    filters: dict[str, dict | None] | None = None,
    reachability: dict[str, int] | None = None,
    exposed: Any = None,
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
    the last live layer's vulnerabilities left on an exposed asset, ``None``
    when they could not be joined to the profile assets.
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
        add("exposed", exposed_n, LIVE, extra={"reachability": dict(split)})
    for key in ("business_impact", "policy"):
        add(key, None, NOT_CONFIGURED)
    add("open_issues", None, NOT_AVAILABLE)

    return {
        "layers": layers,
        # What needs action today: the narrowest live layer.
        "needs_action": previous,
        "epss_threshold": EPSS_THRESHOLD,
        "capped": capped,
    }

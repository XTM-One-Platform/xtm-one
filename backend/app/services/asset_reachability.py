# SPDX-License-Identifier: Apache-2.0

"""Whether an attacker can reach an asset of the organization profile: the rule of the Exposed layer.

Pure: no I/O. The CTEM prioritization funnel (``ctem_funnel``) reads it to say
which vulnerabilities sit on an exposed asset; ``dev-docs/asset-model.md``
section 2 carries the same rule.

The rule (normative):

1. Exposed removes a vulnerability only when every asset it sits on is
   PROVEN unreachable. In doubt it stays in.
2. A user endpoint is always reachable, whatever ``internet_facing`` says:
   category ``host`` with subcategory ``workstation``, ``laptop`` or
   ``thin_client``, and every ``mobile_device`` (a legacy ``endpoint`` row is
   read as ``host`` / ``workstation``). Users open email and browse the web on
   them: phishing, the browser and removable media reach them, and a local
   privilege escalation is the second step of most intrusions.
3. ``internet_facing`` ``yes`` is reachable; ``unknown`` stays in (never
   filtered).
4. Only ``internet_facing`` ``no`` on an asset that is not a user endpoint (a
   server, a virtual machine, a network or OT device...) is unreachable, and
   only when nothing else marks it reachable: an asset group it belongs to
   with ``internet_facing`` ``yes`` makes it reachable.
5. "Internet-facing only" is the opt-in policy ``internet_exposure_required``,
   never the meaning of Exposed; that policy drops user endpoint findings too.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

REACHABLE = "reachable"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"

#: Why an asset got its verdict.
REASON_ENDPOINT = "endpoint"
REASON_INTERNET_FACING = "internet_facing_yes"
REASON_GROUP = "group_internet_facing"
REASON_UNKNOWN = "unknown"
REASON_UNREACHABLE = "proven_unreachable"

#: ``host`` subcategories a person works on: always reachable (rule 2).
ENDPOINT_HOST_SUBCATEGORIES: frozenset[str] = frozenset({"workstation", "laptop", "thin_client"})
#: Categories whose every asset is a user endpoint (rule 2).
ENDPOINT_CATEGORIES: frozenset[str] = frozenset({"mobile_device"})


def _field(asset: Any, name: str) -> str:
    value = asset.get(name) if isinstance(asset, Mapping) else getattr(asset, name, "")
    return str(value or "").strip().lower()


def is_user_endpoint(asset: Any) -> bool:
    """Whether *asset* (an ``AssetEntry`` or its dict) is a device a person uses (rule 2)."""
    category = _field(asset, "category")
    if category in ENDPOINT_CATEGORIES:
        return True
    if category == "host" and _field(asset, "subcategory") in ENDPOINT_HOST_SUBCATEGORIES:
        return True
    # A row not upgraded yet still names the #11543 flat type.
    return _field(asset, "type") == "endpoint"


def _internet_facing_groups(groups: Iterable[Any]) -> set[str]:
    return {_field(group, "name") for group in groups if _field(group, "internet_facing") == "yes"}


def asset_reachability(asset: Any, groups: Iterable[Any] = ()) -> tuple[str, str]:
    """``(verdict, reason)`` of *asset*: ``reachable``, ``unreachable`` or ``unknown`` (rules 1 to 4).

    *groups* are the profile's asset groups (``AssetGroupEntry`` or dicts); the
    asset joins one through its ``group`` name.
    """
    if is_user_endpoint(asset):
        return REACHABLE, REASON_ENDPOINT
    facing = _field(asset, "internet_facing") or "unknown"
    if facing == "yes":
        return REACHABLE, REASON_INTERNET_FACING
    group = _field(asset, "group")
    if group and group in _internet_facing_groups(groups):
        return REACHABLE, REASON_GROUP
    if facing == "no":
        return UNREACHABLE, REASON_UNREACHABLE
    return UNKNOWN, REASON_UNKNOWN


def reachability_split(assets: Iterable[Any], groups: Iterable[Any] = ()) -> dict[str, int]:
    """How many profile assets are reachable, unreachable and unknown, and how many are user endpoints."""
    groups = list(groups)
    split = {"total": 0, REACHABLE: 0, UNREACHABLE: 0, UNKNOWN: 0, "endpoints": 0}
    for asset in assets:
        verdict, reason = asset_reachability(asset, groups)
        split["total"] += 1
        split[verdict] += 1
        if reason == REASON_ENDPOINT:
            split["endpoints"] += 1
    return split


def _asset_keys(asset: Any) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    source, external_id = _field(asset, "source"), _field(asset, "external_id")
    if source and external_id:
        keys.append(("id", f"{source}:{external_id}"))
    name = _field(asset, "name")
    if name:
        keys.append(("name", name))
    return keys


def _verdicts(assets: Iterable[Any], groups: list[Any]) -> dict[tuple[str, str], str]:
    verdicts: dict[tuple[str, str], str] = {}
    for asset in assets:
        verdict, _ = asset_reachability(asset, groups)
        for key in _asset_keys(asset):
            verdicts.setdefault(key, verdict)
    return verdicts


def _proven_unreachable(refs: Iterable[Any], verdicts: dict[tuple[str, str], str]) -> bool:
    # The id match wins over the name match; an asset the profile does not hold is unknown.
    refs = list(refs)
    return bool(refs) and all(
        next((verdicts[key] for key in _asset_keys(ref) if key in verdicts), UNKNOWN) == UNREACHABLE for ref in refs
    )


def exposed_count(findings: Iterable[Iterable[Any]], assets: Iterable[Any], groups: Iterable[Any] = ()) -> int:
    """How many *findings* stay on an exposed asset: a finding drops only when every asset it sits on is proven unreachable.

    Each finding is the list of the assets it sits on, each a dict with
    ``source`` and ``external_id`` (matched on the profile row with the same
    source and id) or ``name`` (matched on the row name). A finding with no
    asset, or with an asset the profile does not hold, stays in (rule 1).
    """
    verdicts = _verdicts(assets, list(groups))
    return sum(1 for refs in findings if not _proven_unreachable(refs, verdicts))


def unreachable_keys(
    findings: Mapping[str, Iterable[Any]], assets: Iterable[Any], groups: Iterable[Any] = ()
) -> list[str]:
    """The keys of *findings* whose every asset is proven unreachable, sorted: the ones Exposed drops.

    *findings* maps a vulnerability (a CVE name) to every asset it was found
    on, in the form :func:`exposed_count` reads; the same rule applies (a key
    with no asset, or with one asset not proven unreachable, stays in).
    """
    verdicts = _verdicts(assets, list(groups))
    return sorted(key for key, refs in findings.items() if _proven_unreachable(refs, verdicts))

# SPDX-License-Identifier: Apache-2.0

"""CTEM risk movers: the objects that moved most between two posture snapshots.

The command center briefing card draws the top movers of the period as signed
bars (dev-docs/ctem-stage-viz.md, briefing row B3). A posture snapshot stores
stage metrics only, so each daily snapshot also keeps a bounded map of the
per-object values the collectors already read (``snapshot_objects``), and the
movers compare today's map with the latest earlier one (``compute_movers``).

Per user, as the rest of the posture: the values come from the collectors run
with the user's own credentials and are stored on that user's snapshot row, so
a user never sees another user's objects. No value is ever guessed: an object
is a mover only when both snapshots hold it, or when it appears or disappears
(then its whole value is the change, and its ``change`` says which).

Object kinds and their value:
- ``technique``: a validated ATT&CK technique (Validation outcome matrix), the
  share of its prevention expectations met, in percent;
- ``attack_domain``: an OpenAEV attack domain in scope (Scoping), its injector
  contract count;
- ``vulnerability``: a top vulnerability by CVSS (Prioritization), its EPSS
  probability in percent. The top list is a ranking, so a vulnerability that
  enters or leaves it did not move: only a change of a held value counts.
"""

from __future__ import annotations

from typing import Any

#: Most objects a snapshot keeps (the Validation matrix alone allows 600 techniques).
MAX_SNAPSHOT_OBJECTS = 1000
#: Movers served by default: the top five by absolute change.
DEFAULT_MOVERS = 5

#: Unit of each object kind's value, as the chart prints it after the number.
KIND_UNITS = {
    "technique": "%",
    "attack_domain": "contracts",
    "vulnerability": "% EPSS",
}
#: Kinds whose appearance or disappearance is a move (the whole set is read).
_MEMBERSHIP_KINDS = frozenset({"technique", "attack_domain"})


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _put(objects: dict[str, dict[str, Any]], kind: str, oid: Any, label: Any, value: float | None) -> None:
    if value is None or oid in (None, "") or len(objects) >= MAX_SNAPSHOT_OBJECTS:
        return
    objects[f"{kind}:{oid}"] = {"label": str(label or oid), "value": round(value, 2)}


def snapshot_objects(stage_facts: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
    """``{"<kind>:<id>": {"label", "value"}}`` from one posture build's stage facts.

    ``None`` when no object was measured (no product answered), so an empty
    build never serves as the base of a comparison.

    Only objects with a measured value are kept: a technique without a
    prevention expectation, a domain without a count or a vulnerability without
    an EPSS score is left out rather than stored as zero.
    """
    objects: dict[str, dict[str, Any]] = {}
    matrix = (stage_facts.get("validation") or {}).get("outcome_matrix") or {}
    for item in matrix.get("items") or []:
        prevention = (item or {}).get("prevention") or {}
        total = _number(prevention.get("total"))
        success = _number(prevention.get("success"))
        if not total or success is None:
            continue
        _put(objects, "technique", item.get("code") or item.get("id"), item.get("label"), 100.0 * success / total)
    for domain in (stage_facts.get("scoping") or {}).get("attack_domains") or []:
        name = (domain or {}).get("name")
        _put(objects, "attack_domain", name, name, _number(domain.get("contracts")))
    for vuln in (stage_facts.get("prioritization") or {}).get("top_vulnerabilities") or []:
        name = (vuln or {}).get("name")
        epss = _number(vuln.get("epss"))
        _put(objects, "vulnerability", name, name, None if epss is None else 100.0 * epss)
    return objects or None


def compute_movers(
    previous: dict[str, dict[str, Any]] | None,
    current: dict[str, dict[str, Any]] | None,
    *,
    since: str | None = None,
    limit: int = DEFAULT_MOVERS,
) -> dict[str, Any]:
    """The top ``limit`` objects by absolute change from ``previous`` to ``current``.

    ``status`` is ``insufficient_history`` while no earlier snapshot holds
    objects (the chart then says when movers start, never a guessed value),
    else ``ready`` (``items`` may be empty when nothing moved). Each item:
    ``kind``, ``id``, ``label``, ``value`` (now, ``None`` when gone),
    ``previous`` (``None`` when new), ``delta`` (signed), ``unit`` and
    ``change`` (``changed``, ``new`` or ``gone``). Ties keep a stable order:
    larger change first, then the label.
    """
    if not previous or not current:
        return {"status": "insufficient_history", "since": None, "items": []}
    items: list[dict[str, Any]] = []
    for key in previous.keys() | current.keys():
        kind, _, oid = key.partition(":")
        if kind not in KIND_UNITS:
            continue
        before, now = previous.get(key), current.get(key)
        old = _number((before or {}).get("value"))
        new = _number((now or {}).get("value"))
        if old is not None and new is not None:
            change, delta = "changed", new - old
        elif kind not in _MEMBERSHIP_KINDS:
            continue
        elif new is not None:
            change, delta = "new", new
        elif old is not None:
            change, delta = "gone", -old
        else:
            continue
        if round(delta, 2) == 0:
            continue
        items.append(
            {
                "kind": kind,
                "id": oid,
                "label": str((now or before or {}).get("label") or oid),
                "value": new,
                "previous": old,
                "delta": round(delta, 2),
                "unit": KIND_UNITS[kind],
                "change": change,
            }
        )
    items.sort(key=lambda m: (-abs(m["delta"]), m["label"].casefold(), m["kind"]))
    return {"status": "ready", "since": since, "items": items[: max(limit, 0)]}

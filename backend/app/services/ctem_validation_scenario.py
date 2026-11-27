# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: the OpenAEV scenario of a case, its playability and the validation gate card (#2468).

The validation step builds (or adopts) an OpenAEV scenario that emulates the
threat. A scenario whose techniques have no enabled inject carrying a payload
tests nothing of the threat: launching it proves nothing. The orchestrator
records the scenario on the case with what it read (techniques of the package,
techniques with at least one enabled payload inject, what the playable injects
test), and the platform states it on the ``ctem.validation.non_readonly`` card:

- :func:`record_scenario`: one ``scenario`` event on the case (``to`` the
  scenario id, ``reason`` its name, ``evidence`` its OpenAEV page, ``from`` the
  playability figures); the same scenario again writes nothing;
- :func:`scenario_of` / :func:`scenario_for`: the newest scenario, and the case
  page's ``scenario`` (link and figures), name and link to an OpenAEV reader only;
- :func:`complete_gate_ask`: the validation card keyed by the case carries the
  scenario and the threat package in ``links``, and, when the scenario is not
  playable, opens with "Scenario not playable" and offers the three options
  below instead of a launch, whatever the agent wrote; on a patch (upgrade) case,
  which validation evidence never closes, the agent's options whose pass closes
  the case (remediation or installed-version evidence) are kept first and one
  of them is recommended, each option saying whether its pass closes the case
  (#2683, the #2461 rule).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The case history kind of the scenario record.
SCENARIO_KIND = "scenario"
#: The first words of the card of a scenario that is not playable.
NOT_PLAYABLE_LEAD = "Scenario not playable:"
#: The question of a card whose scenario is not playable: none of its options launches it (#2468).
NOT_PLAYABLE_QUESTION = "The scenario cannot test this threat: how should the case be validated?"
_MAX_CONTEXT_CHARS = 800
_MAX_TESTS_CHARS = 200
#: The agent options a not-playable card of a patch case keeps beside its own three (#2683).
_MAX_KEPT_OPTIONS = 3
#: The remediation kind of a patch case: only remediation evidence closes it, never a validation (#2683).
PATCH_KIND = "upgrade"
#: What the validation options of a patch case say of their pass (#2683, the #2461 rule).
PATCH_NOT_CLOSING = "; its pass does not close the case: a patch case closes on remediation evidence only."
#: What a kept remediation option of a patch case says of its pass when the agent did not say it.
PATCH_CLOSING = " Its pass closes the case."
#: Words of an option that brings remediation evidence on a patch case (installed or fixed version read).
_REMEDIATION_MARKERS: tuple[str, ...] = ("installed version", "fixed version", "remediation evidence")
#: Characters of a raw list a person should not read in the not-playable line (#2683).
_RAW_LIST = re.compile(r"[\[\]{}\"]")
#: ``playable/total``, the ``from`` value of a scenario event.
_FIGURES = re.compile(r"^(\d{1,4})/(\d{1,4})(?:\|(.*))?$", re.S)
#: An OpenAEV scenario page, with or without the tenant segment.
_SCENARIO_URL = re.compile(
    r"^https?://[^\s/?#]+(?:/[A-Za-z0-9._-]+)*/scenarios/([0-9a-fA-F-]{36})(?:/[A-Za-z0-9._-]*)?$"
)

#: The options of a card whose scenario is not playable (#2468): never a plain launch.
NOT_PLAYABLE_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "key": "A",
        "label": "Complete the scenario, then validate",
        "effect": (
            "The OpenAEV Payload Creator builds payloads for exactly the techniques that have none; "
            "a new validation gate follows with the completed scenario."
        ),
    },
    {
        "key": "B",
        "label": "Validate manually",
        "effect": "A person tests the threat outside this scenario and records the result on the case.",
    },
    {
        "key": "C",
        "label": "Reject and clean up",
        "effect": (
            "No validation: delete the scenario, set its arsenal actions UNVERIFIED, turn the coverage's auto "
            "enrichment off, keep the Grouping."
        ),
    },
)


def _int(value: object) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def playability(total: int, playable: int) -> dict[str, Any]:
    """``{techniques_total, techniques_playable, techniques_missing, playable}`` (pure).

    Playable only when every technique of the package has an enabled inject carrying
    a payload; a scenario with no technique is never playable.
    """
    total = max(int(total), 0)
    playable = min(max(int(playable), 0), total)
    return {
        "techniques_total": total,
        "techniques_playable": playable,
        "techniques_missing": total - playable,
        "playable": total > 0 and playable == total,
    }


def record_scenario(
    db: AsyncSession,
    case: Any,
    scenario: object,
    previous: Mapping[str, Any] | None = None,
    *,
    actor: str,
    now: datetime | None = None,
) -> Any:
    """Attach the OpenAEV scenario of the validation to the case (#2468). The caller commits.

    *scenario* is ``{scenario_id, name, url, techniques_total, techniques_playable, playable_tests}``:
    the scenario id, its name, its OpenAEV page (the tenant segment included on a
    tenant-scoped OpenAEV), the techniques of the package and how many have an
    enabled inject carrying a payload, and what the playable injects test. The same
    scenario with the same figures again writes nothing; never moves the case state.
    """
    from app.services import ctem_case_loop as loop
    from app.services import ctem_cases as rules

    if not isinstance(scenario, dict):
        raise rules.CaseRuleError(
            "scenario must be an object with scenario_id, name, url, techniques_total and techniques_playable"
        )
    try:
        scenario_id = str(uuid.UUID(str(scenario.get("scenario_id") or "").strip()))
    except ValueError:
        raise rules.CaseRuleError("scenario.scenario_id must be the OpenAEV scenario id (its UUID)") from None
    url = str(scenario.get("url") or "").strip()
    found = _SCENARIO_URL.match(url) if url else None
    if url and (found is None or found.group(1).lower() != scenario_id.lower()):
        raise rules.CaseRuleError(
            "scenario.url must be the OpenAEV page of that scenario (.../scenarios/ then its id, the tenant "
            "segment included on a tenant-scoped OpenAEV)"
        )
    total = _int(scenario.get("techniques_total"))
    playable = _int(scenario.get("techniques_playable"))
    if total is None or playable is None or playable > total:
        raise rules.CaseRuleError(
            "scenario.techniques_total and scenario.techniques_playable are counts read from the scenario: the "
            "techniques of the package, and those with at least one enabled inject carrying a payload"
        )
    tests = " ".join(str(scenario.get("playable_tests") or "").split())[:_MAX_TESTS_CHARS]
    stored = {
        "scenario_id": scenario_id,
        "name": str(scenario.get("name") or "").strip()[:300] or None,
        "url": url or None,
        "figures": f"{playable}/{total}" + (f"|{tests}" if tests else ""),
    }
    if previous and all(previous.get(k) == v for k, v in stored.items()):
        return case
    db.add(
        loop._event(
            case.id,
            actor,
            SCENARIO_KIND,
            now or datetime.now(UTC),
            from_value=stored["figures"],
            to_value=scenario_id,
            reason=stored["name"],
            evidence=stored["url"],
        )
    )
    return case


def _figures(raw: object) -> tuple[dict[str, Any], str | None]:
    found = _FIGURES.match(str(raw or ""))
    if found is None:
        return {}, None
    tests = (found.group(3) or "").strip() or None
    return playability(int(found.group(2)), int(found.group(1))), tests


def scenario_of(events: list[Any]) -> dict[str, Any] | None:
    """The newest ``scenario`` event of the case as a dict, else ``None`` (pure)."""
    rows = [row for row in events if row.kind == SCENARIO_KIND and row.at is not None and row.to_value]
    if not rows:
        return None
    newest = max(rows, key=lambda row: row.at)
    figures, tests = _figures(newest.from_value)
    return {
        "scenario_id": newest.to_value,
        "name": newest.reason,
        "url": newest.evidence,
        "figures": newest.from_value,
        **figures,
        "playable_tests": tests,
        "at": newest.at.isoformat() if newest.at else None,
    }


def scenario_for(events: list[Any], view: Any) -> dict[str, Any] | None:
    """The case page's scenario: figures to every viewer, name, id and link to an OpenAEV reader only."""
    found = scenario_of(events)
    if found is None:
        return None
    figures = {k: found.get(k) for k in ("techniques_total", "techniques_playable", "techniques_missing", "playable")}
    if not getattr(view, "openaev", False):
        return {"restricted": True, **figures, "at": found["at"]}
    return {
        "restricted": False,
        "scenario_id": found["scenario_id"],
        "name": found["name"],
        "url": found["url"],
        **figures,
        "playable_tests": found["playable_tests"],
        "at": found["at"],
    }


def _plain(text: object) -> str | None:
    """*text* when a person can read it in a sentence, else ``None`` (pure, #2683).

    A raw list (brackets, braces, quotes) or a text holding an id, a key or a
    timestamp is never written on a card: the reader gets plain words.
    """
    from app.services.decision_ask import unreadable_reasons

    value = " ".join(str(text or "").split()).strip(" .;,")
    if not value or _RAW_LIST.search(value) or unreadable_reasons(value):
        return None
    return value


def not_playable_line(found: Mapping[str, Any], threat: str | None) -> str:
    """The first line of the card of a scenario that is not playable, in plain words (pure, #2683)."""
    missing = found.get("techniques_missing") or 0
    total = found.get("techniques_total") or 0
    line = f"{NOT_PLAYABLE_LEAD} {missing} of {total} techniques have no payload"
    tests = _plain(found.get("playable_tests"))
    if tests:
        line += f"; the only playable injects test {tests}, not {_plain(threat) or 'the threat'}"
    elif not found.get("techniques_playable"):
        line += "; no inject of the scenario can run"
    return line + "."


def _closes_patch_case(option: Mapping[str, Any]) -> bool:
    """Whether an agent option of a patch case brings evidence that closes it (pure, #2683)."""
    from app.services.ctem_control_measurement import option_outcome

    outcome = option_outcome(option)
    if outcome is not None:
        return outcome == "closes"
    text = f"{option.get('label') or ''} {option.get('effect') or ''}".casefold()
    return any(marker in text for marker in _REMEDIATION_MARKERS)


def _patch_options(ask: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """The options of a not-playable card of a patch case and its recommendation (pure, #2683).

    The agent's options whose pass closes the case come first (at most
    :data:`_MAX_KEPT_OPTIONS`), then the three not-playable options, each
    saying its pass does not close the case; keys are rewritten A, B, C...
    The recommendation is the agent's when it is a kept option, else the first
    kept option, else the completion of the scenario (nothing closes the case).
    """
    from app.services.ctem_control_measurement import CLOSES_WORDS
    from app.services.decision_ask import MAX_EFFECT_CHARS, fit

    standard = {o["label"] for o in NOT_PLAYABLE_OPTIONS}
    given = [o for o in (ask.get("options") or []) if isinstance(o, Mapping) and o.get("label") not in standard]
    kept = [o for o in given if _closes_patch_case(o)][:_MAX_KEPT_OPTIONS]
    recommended = str(ask.get("recommendation") or "")
    keys = iter("ABCDEFGH")
    options: list[dict[str, Any]] = []
    recommendation: str | None = None
    for option in kept:
        key = next(keys)
        effect = str(option.get("effect") or "").strip()
        if CLOSES_WORDS not in f"{option.get('label') or ''} {effect}".casefold():
            effect = fit(f"{effect}{PATCH_CLOSING}".strip(), MAX_EFFECT_CHARS)
        options.append({**dict(option), "key": key, "effect": effect})
        if recommendation is None or str(option.get("key")) == recommended:
            recommendation = key
    first_standard: str | None = None
    for option in NOT_PLAYABLE_OPTIONS:
        key = next(keys)
        first_standard = first_standard or key
        effect = option["effect"]
        if option["key"] != "C":
            effect = fit(effect.rstrip(".") + PATCH_NOT_CLOSING, MAX_EFFECT_CHARS)
        options.append({**option, "key": key, "effect": effect})
    return options, recommendation or first_standard


def complete_ask(
    ask: Mapping[str, Any],
    found: Mapping[str, Any] | None,
    package: Mapping[str, Any] | None,
    threat: str | None,
    *,
    patch_case: bool = False,
) -> dict[str, Any]:
    """*ask* with the scenario and the threat package in ``links`` and, when the scenario is not
    playable, the not-playable line first and the three options of :data:`NOT_PLAYABLE_OPTIONS` (pure).

    On a patch case (*patch_case*), the agent's options that close the case are kept and recommended
    (:func:`_patch_options`, #2683).
    """
    from app.services.decision_ask import lead_then_fit

    out = dict(ask)
    links = [dict(link) for link in (out.get("links") or []) if isinstance(link, Mapping)]
    known = {str(link.get("url") or "") for link in links}
    wanted: list[dict[str, str]] = []
    if found and found.get("url"):
        wanted.append({"label": f"Scenario: {found.get('name') or 'OpenAEV'}"[:120], "url": str(found["url"])})
    if package and package.get("url"):
        wanted.append(
            {"label": f"Threat package: {package.get('name') or 'OpenCTI'}"[:120], "url": str(package["url"])}
        )
    for link in wanted:
        if link["url"] not in known:
            links.append(link)
            known.add(link["url"])
    if links:
        out["links"] = links[:5]
    if found and found.get("techniques_total") is not None and not found.get("playable"):
        head = not_playable_line(found, threat)
        rest = str(out.get("context") or "").strip()
        if not rest.startswith(NOT_PLAYABLE_LEAD):
            out["context"] = lead_then_fit(head, rest, _MAX_CONTEXT_CHARS)
        out["question"] = NOT_PLAYABLE_QUESTION
        if patch_case:
            out["options"], out["recommendation"] = _patch_options(ask)
        else:
            out["options"] = [dict(o) for o in NOT_PLAYABLE_OPTIONS]
            out["recommendation"] = "A"
    return out


async def complete_gate_ask(
    db: AsyncSession, correlation_key: object, ask: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """*ask* completed from its case when it is a validation card, else as given (#2468).

    Never fails a work item write: a case that cannot be read leaves the ask as given.
    """
    if not ask:
        return ask
    from app.services import ctem_control_measurement as measurement

    case_id = measurement.case_id_of(correlation_key)
    if case_id is None:
        return ask
    from app.models.ctem_case import CtemCase
    from app.services import ctem_cases_store as store
    from app.services import ctem_cases_view as view_of

    try:
        case = await db.get(CtemCase, case_id)
        if case is None:
            return ask
        events = (await store.events_of(db, [case.id])).get(case.id, [])
    except Exception:  # the card is written whatever the case answers
        logger.info("CTEM validation scenario unavailable for %s", case_id, exc_info=True)
        return ask
    found = scenario_of(events)
    package = view_of.threat_package_of(events)
    if found is None and package is None:
        return ask
    # The threat is what the scenario emulates: the threat package, else the scenario itself; never
    # the case title, which names the remediation (UX round 1 of #2485).
    threat = (package or {}).get("name") or (found or {}).get("name") or None
    return complete_ask(ask, found, package, threat, patch_case=getattr(case, "remediation_kind", None) == PATCH_KIND)

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
  which validation evidence never closes, every agent option but a plain launch
  is kept in its order, each saying whether its pass closes the case (a version
  read or remediation evidence closes it, a cleanup or a validation does not),
  one that closes it is recommended, and a stock option is added only for an
  intent the agent did not cover, within the card's option cap (#2683, #2749,
  the #2461 rule).
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
#: The remediation kind of a patch case: only remediation evidence closes it, never a validation (#2683).
PATCH_KIND = "upgrade"
#: What the validation options of a patch case say of their pass (#2683, the #2461 rule).
PATCH_NOT_CLOSING = "; its pass does not close the case: a patch case closes on remediation evidence only."
#: What a kept remediation option of a patch case says of its pass when the agent did not say it.
PATCH_CLOSING = " Its pass closes the case."
#: What an agent option of a patch case that is no remediation evidence says when the agent did not say it.
PATCH_AGENT_NOT_CLOSING = " Its pass does not close the case: a patch case closes on remediation evidence only."
#: What a cleanup option of a patch case says: it runs no validation (#2749).
PATCH_CLEANUP = " No validation runs: this does not close the case."
#: An option that brings remediation evidence on a patch case: a version read, installed, reported or checked (#2749).
_REMEDIATION = re.compile(
    r"\bremediation evidence\b"
    r"|\b(?:installed|fixed|product|running|patched|software)\s+(?:\w+\s+)?versions?\b"
    r"|\bversions?\s+(?:check|checks|read|reading|readings|report|reports)\b"
    r"|\b(?:read|reads|reading|report|reports|reporting|record|records|check|checks|checking)\b[^.;]{0,60}\bversions?\b",
    re.I,
)
#: The intent of an option, read from its key and label (#2749): a cleanup, a manual validation, a completion.
_CLEANUP = re.compile(r"\b(?:reject|rejects|clean|cleanup|do not run|don't run|discard|abandon)\b", re.I)
_MANUAL = re.compile(r"\b(?:manual|manually|by hand)\b", re.I)
_COMPLETE = re.compile(r"\b(?:complete|completes|completion|build|builds|payloads?)\b", re.I)
#: A plain launch of the scenario: never offered on a not-playable card (#2468), whoever wrote it.
_LAUNCH = re.compile(r"^\s*(?:launch|run|play|start)\b", re.I)
#: The intent each stock not-playable option carries, by its key in :data:`NOT_PLAYABLE_OPTIONS`.
_STOCK_INTENT = {"A": "complete", "B": "manual", "C": "cleanup"}
#: A letter an agent put before its label (``(C) Do not run``, ``B. Validate``): keys are rewritten by position.
_LETTER_PREFIX = re.compile(r"^\s*(?:\(?[A-H]\)|[A-H][.:)])\s+")
#: A sentence a shortened text keeps first: a figure or a request to the person deciding (#2749).
_KEEP_SENTENCE = re.compile(r"\d|\?|\b(?:please|give|tell|confirm|provide|state|explain|reply|answer|choose)\b", re.I)
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


def _keeps_sentence(sentence: str) -> bool:
    return bool(_KEEP_SENTENCE.search(sentence))


def _words(value: object) -> str:
    return " ".join(str(value or "").replace("_", " ").replace("-", " ").split())


def _intent(option: Mapping[str, Any]) -> str | None:
    """``cleanup``, ``manual``, ``complete`` or ``None``: what an option does, from its key and label (pure, #2749).

    A completion that closes the case (a version check built into the scenario)
    is remediation evidence, not the stock completion, so it carries no intent here.
    """
    text = f"{_words(option.get('key'))} {_words(option.get('label'))}"
    if _CLEANUP.search(text):
        return "cleanup"
    if _MANUAL.search(text):
        return "manual"
    if _COMPLETE.search(text) and not _closes_patch_case(option):
        return "complete"
    if _LAUNCH.match(_label(option)) and not _closes_patch_case(option):
        return "launch"
    return None


def _closes_patch_case(option: Mapping[str, Any]) -> bool:
    """Whether an agent option of a patch case brings evidence that closes it (pure, #2683, #2749).

    A cleanup never does, whatever its text says ("closes the case without
    validation"); an option that says its pass does not close the case does not;
    a version read, report or check, or remediation evidence, does, however worded.
    """
    from app.services.ctem_control_measurement import option_outcome

    head = f"{_words(option.get('key'))} {_words(option.get('label'))}"
    if _CLEANUP.search(head):
        return False
    outcome = option_outcome(option)
    if outcome == "not_closing":
        return False
    text = f"{head} {option.get('effect') or ''}"
    return outcome == "closes" or bool(_REMEDIATION.search(text))


def _label(option: Mapping[str, Any]) -> str:
    label = str(option.get("label") or "").strip()
    return _LETTER_PREFIX.sub("", label).strip() or label


def _with_outcome(effect: str, suffix: str) -> str:
    """*effect* then *suffix* within the effect cap, the effect shortened at a sentence end (pure, #2749).

    A text a person reads keeps whole sentences, its figures and its requests to
    the reader first; a fragment an earlier cap marked is dropped. When not one
    sentence fits (a single long sentence), the effect keeps a word-boundary
    prefix, marked, rather than leaving the outcome alone on the card.
    """
    from app.services.decision_ask import CUT_MARK, MAX_EFFECT_CHARS, fit, fit_sentences

    effect = effect.strip()
    if effect.endswith(CUT_MARK):
        ends = [i for i, char in enumerate(effect) if char in ".!?"]
        effect = effect[: ends[-1] + 1] if ends else ""
    room = MAX_EFFECT_CHARS - len(suffix)
    kept = fit_sentences(effect, room, _keeps_sentence)
    if not kept and effect:
        kept = fit(effect, room)
    return f"{kept}{suffix}".strip()


def _agent_option(option: Mapping[str, Any], key: str) -> dict[str, Any]:
    """An agent option of a patch card: its key by position, its label without a letter, its outcome said."""
    from app.services.ctem_control_measurement import option_outcome

    effect = str(option.get("effect") or "").strip()
    stated = option_outcome(option)
    if _intent(option) == "cleanup":
        suffix = "" if stated == "not_closing" else PATCH_CLEANUP
    elif _closes_patch_case(option):
        suffix = "" if stated == "closes" else PATCH_CLOSING
    else:
        suffix = "" if stated == "not_closing" else PATCH_AGENT_NOT_CLOSING
    return {**dict(option), "key": key, "label": _label(option), "effect": _with_outcome(effect, suffix)}


def _patch_options(ask: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    """The options of a not-playable card of a patch case, its recommendation, the stock labels left out.

    Pure (#2683, #2749). Every agent option but a plain launch (#2468) is kept, in its order, its key
    rewritten A, B, C..., a letter prefix of its label dropped, and its effect
    saying whether its pass closes the case (remediation evidence such as a
    version read closes it; a cleanup or a validation does not). A stock
    not-playable option is added only when no agent option carries its intent
    (completion, manual validation, cleanup), and only within
    :data:`~app.services.decision_ask.MAX_OPTIONS`: the agent's set is never cut,
    the stock options that do not fit are returned to be named on the card.
    The recommendation is the agent's when its option closes the case, else
    the first agent option that closes it, else the first option.
    """
    from app.services.decision_ask import MAX_EFFECT_CHARS, MAX_OPTIONS, fit

    # A stock option the platform wrote on an earlier pass (its label and its effect) is not the agent's:
    # an agent option with the same label and its own effect stays (#2749).
    standard = {(o["label"], o["effect"]) for o in NOT_PLAYABLE_OPTIONS} | {
        (o["label"], fit(o["effect"].rstrip(".") + PATCH_NOT_CLOSING, MAX_EFFECT_CHARS)) for o in NOT_PLAYABLE_OPTIONS
    }
    given = [
        o
        for o in (ask.get("options") or [])
        if isinstance(o, Mapping) and (o.get("label"), o.get("effect")) not in standard and _intent(o) != "launch"
    ][:MAX_OPTIONS]
    covered = {_intent(o) for o in given}
    wanted = [o for o in NOT_PLAYABLE_OPTIONS if _STOCK_INTENT[o["key"]] not in covered]
    room = max(MAX_OPTIONS - len(given), 0)
    stock, left_out = wanted[:room], [o["label"] for o in wanted[room:]]
    keys = iter("ABCDEFGH")
    options = [_agent_option(option, next(keys)) for option in given]
    recommended = str(ask.get("recommendation") or "")
    recommendation: str | None = None
    for source, option in zip(given, options, strict=True):
        if _closes_patch_case(source) and (recommendation is None or str(source.get("key")) == recommended):
            recommendation = option["key"]
    for option in stock:
        effect = option["effect"]
        if _STOCK_INTENT[option["key"]] != "cleanup":
            effect = fit(effect.rstrip(".") + PATCH_NOT_CLOSING, MAX_EFFECT_CHARS)
        options.append({**option, "key": next(keys), "effect": effect})
    return options, recommendation or (options[0]["key"] if options else None), left_out


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

    On a patch case (*patch_case*), every agent option is kept and one that closes the case recommended
    (:func:`_patch_options`, #2683, #2749). The agent's context keeps its figures and requests first
    when it does not fit after the platform line (#2749).
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
        out["question"] = NOT_PLAYABLE_QUESTION
        if patch_case:
            out["options"], out["recommendation"], left_out = _patch_options(ask)
            if left_out:
                head += f" Not shown for room: {', '.join(left_out)}."
            if not rest.startswith(NOT_PLAYABLE_LEAD):
                out["context"] = lead_then_fit(head, rest, _MAX_CONTEXT_CHARS, keep=_keeps_sentence)
        else:
            if not rest.startswith(NOT_PLAYABLE_LEAD):
                out["context"] = lead_then_fit(head, rest, _MAX_CONTEXT_CHARS)
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

# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: the version match a remediation or mobilize decision states (#1472).

An upgrade case is a proposal to move a product to a fixed release. Whether the
organization's hosts are vulnerable depends on two facts the loop may not hold:
the version its assets run (the asset inventory, ``ctem_case_versions``) and the
range of versions the vulnerabilities affect. This module states the match, or
its blindness, so a person never approves tickets that assert hosts are
vulnerable when nothing compared a version:

- :func:`version_match`: the three facts (running, affected, fixed) and
  ``blind`` when the running version or the affected range is unknown;
- :func:`complete_ask`: a decision ask whose context carries the version match
  line, and, when blind, the plain statement and the "verify the running
  version, then upgrade" wording;
- :func:`complete_gate_ask`: the platform completion of a gate card keyed by a
  case (``ctem:remediation:``, ``ctem:f1:case-mobilize:``, ``ctem:f2:mobilize:``
  and the case id) on an upgrade case, called where the work item is written,
  so the line never depends on the prompt.

Never a guessed version: what no source answered is written ``unknown`` with
the reason.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import ctem_cases as rules

logger = logging.getLogger(__name__)

#: The work item keys of the gates that decide or mobilize a case's remediation,
#: each followed by the case id (builtin_agents.CTEM_*_GATE_PREFIXES).
GATE_KEY_PREFIXES: tuple[str, ...] = (
    "ctem:remediation:",
    "ctem:f1:case-mobilize:",
    "ctem:f2:mobilize:",
)
#: The line every completed card carries, first in its context.
LINE_LEAD = "Version match:"
BLIND_LEAD = "Proposed without version matching:"
#: Never prescribes an upgrade: the card's recommended option may be another treatment (#2174).
BLIND_ACTION = "Verify the running version before acting: the hosts are not known to be vulnerable."
#: The wording before #2174, still recognised in an agent's context so it is never stated twice.
_LEGACY_BLIND_ACTION = "Verify the running version, then upgrade: the hosts are not known to be vulnerable."
NO_AFFECTED = "unknown: OpenCTI holds no affected versions"
UNKNOWN = "unknown"
#: The most running versions named on the line; the rest are counted.
MAX_LINE_VERSIONS = 3
_MAX_CONTEXT_CHARS = 800


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _running_text(running: Mapping[str, Any] | None, product_label: str | None) -> tuple[str, bool]:
    """The running versions in words, and whether any comparable version is known."""
    product = _clean(product_label) or "the product"
    if not running or not running.get("versions"):
        return f"unknown: no asset reports {product}", False
    rows = [v for v in running.get("versions") or () if isinstance(v, Mapping)]
    known = [v for v in rows if _clean(v.get("version")) and _clean(v.get("version")) != UNKNOWN]
    parts = [
        f"{_clean(v.get('version'))}"
        + (f" ({_clean(v.get('platform'))})" if _clean(v.get("platform")) else "")
        + f" on {int(v.get('asset_count') or 0)} asset(s)"
        for v in rows
    ]
    more = len(parts) - MAX_LINE_VERSIONS + int(running.get("more_versions") or 0)
    text = ", ".join(parts[:MAX_LINE_VERSIONS]) + (f", {more} more version(s)" if more > 0 else "")
    if not known:
        return f"unknown: {int(running.get('asset_count') or 0)} asset(s) run {product} with no version reported", False
    return text, True


def version_match(
    running: Mapping[str, Any] | None,
    *,
    product_label: str | None,
    fixed_version: str | None,
    affected: str | None = None,
) -> dict[str, Any]:
    """The version match of an upgrade case (pure).

    *running* is ``ctem_case_versions.for_case``'s answer (``None`` when no
    asset runs the product); *affected* the range of affected versions a source
    gave, else unknown. ``blind`` is true when the running version or the
    affected range is unknown: the proposal is then made without matching.
    """
    running_text, running_known = _running_text(running, product_label)
    affected_text = _clean(affected) or NO_AFFECTED
    fixed_text = _clean(fixed_version) or UNKNOWN
    blind = not running_known or not _clean(affected)
    match: dict[str, Any] = {
        "running": running_text,
        "affected": affected_text,
        "fixed": fixed_text,
        "blind": blind,
    }
    if running_known and running:
        for key in ("assets_below_fixed", "assets_on_fixed", "assets_not_comparable"):
            if key in running:
                match[key] = int(running.get(key) or 0)
        if running.get("platform_unverified"):
            match["platform_unverified"] = True
    return match


def affected_from_outcomes(issues: Any) -> str | None:
    """The affected versions OpenCTI placed, in words, from the case's open issues (pure, #1783).

    *issues* are the case view's issue rows; each may carry ``version_outcomes``
    ({``version``, ``outcome``}). ``None`` when OpenCTI placed no running
    version (all unknown, or no outcome): the range stays unknown. No range
    is computed: the outcome of each version is OpenCTI's ``has`` relation.
    """
    affected: set[str] = set()
    not_affected: set[str] = set()
    unknown: set[str] = set()
    for issue in issues or ():
        if not isinstance(issue, Mapping) or issue.get("open") is False:
            continue
        for row in issue.get("version_outcomes") or ():
            if not isinstance(row, Mapping) or not _clean(row.get("version")):
                continue
            version, outcome = _clean(row.get("version")), _clean(row.get("outcome"))
            {"affected": affected, "not_affected": not_affected}.get(outcome, unknown).add(version)
    not_affected -= affected
    unknown -= affected | not_affected
    if not affected and not not_affected:
        return None
    parts = [f"{', '.join(sorted(affected))} per OpenCTI" if affected else "none of the running versions per OpenCTI"]
    if not_affected:
        parts.append(f"not affected: {', '.join(sorted(not_affected))}")
    if unknown:
        parts.append(f"not known to OpenCTI: {', '.join(sorted(unknown))}")
    return "; ".join(parts)


#: The case marker of a version-blind case (#2004): kept, never discarded, the version to validate first.
UNVERIFIED_NOT_KNOWN = "running version not verified: OpenCTI does not know"
UNVERIFIED_NOT_REPORTED = "running version not verified: no running version reported"


def unverified_reason(issues: Any) -> str | None:
    """Why a case's running version is not verified, else ``None`` (pure, #2004).

    *issues* are the case's open OpenCTI issues with their details
    (``ctem_cases.SourceIssue``). The case is to validate when one of them has
    an installed version OpenCTI cannot place (outcome ``unknown``) or names no
    running version at all. Nothing is discarded and nothing is derived from
    versions: membership, priority and SLA are unchanged; the marker clears when
    OpenCTI places every running version.

    A signal member that names a CVE (#2041) carries no version outcome: it is
    unverified the same way, unless an OpenCTI issue of the same CVE is in
    *issues* (that issue's outcomes judge it) or the signal reports the running
    version itself (``installed_version``, read from its evidence, #2755).
    Missing data is unverified.
    """
    not_known: set[str] = set()
    not_reported = False
    issues = list(issues or ())
    judged = {_clean(getattr(i, "cve", None)).upper() for i in issues if getattr(i, "source", None) == "opencti"}
    for issue in issues:
        source = getattr(issue, "source", None)
        if source == "signal":
            cve = _clean(getattr(issue, "cve", None)).upper()
            if cve and cve not in judged and not _clean(getattr(issue, "installed_version", None)):
                not_reported = True
            continue
        if source != "opencti" or getattr(issue, "not_affected", None):
            continue
        # Typed facts (#2038); a pair stored before it reads the same way.
        outcomes = rules.version_facts(getattr(issue, "version_outcomes", None))
        if not outcomes and not _clean(getattr(issue, "installed_version", None)):
            not_reported = True
            continue
        not_known.update(_clean(f.version) for f in outcomes if _clean(f.outcome) == UNKNOWN and _clean(f.version))
    if not_known:
        return f"{UNVERIFIED_NOT_KNOWN} {', '.join(sorted(not_known))}"
    return UNVERIFIED_NOT_REPORTED if not_reported else None


def blind_reasons(match: Mapping[str, Any]) -> list[str]:
    """What is unknown, in words: ``running version unknown``, ``affected range unknown``."""
    reasons: list[str] = []
    if str(match.get("running") or "").startswith(UNKNOWN):
        reasons.append("running version unknown")
    if str(match.get("affected") or "").startswith(UNKNOWN):
        reasons.append("affected range unknown")
    return reasons


def card_lines(match: Mapping[str, Any]) -> list[str]:
    """The lines a card carries: the version match, and when blind the plain statement and the action wording."""
    lines = [f"{LINE_LEAD} running {match['running']}; affected {match['affected']}; fixed {match['fixed']}."]
    if match.get("blind"):
        reasons = blind_reasons(match) or ["versions not compared"]
        lines.append(f"{BLIND_LEAD} {', '.join(reasons)}.")
        lines.append(BLIND_ACTION)
    return lines


def has_line(ask: Mapping[str, Any] | None) -> bool:
    """Whether an ask already states a version match (its context names it)."""
    return LINE_LEAD.casefold() in str((ask or {}).get("context") or "").casefold()


def complete_ask(ask: Mapping[str, Any], match: Mapping[str, Any]) -> dict[str, Any]:
    """*ask* with the version match lines first in its context (pure; an ask that has them is kept as is).

    The lines come first so the cut at the context limit takes the agent's
    text, never the version match.
    """
    out = dict(ask)
    if has_line(out):
        return out
    from app.services.decision_ask import fit, full_context, with_full_context

    head = "\n".join(card_lines(match))
    # The agent's own blindness lines say what the head now says: never twice (#1598).
    kept = [line for line in full_context(out).splitlines() if not _states_blindness(line)]
    rest = "\n".join(kept).strip()
    whole = f"{head}\n{rest}" if rest else head
    out["context"] = fit(whole, _MAX_CONTEXT_CHARS)
    # What the cut leaves out stays reachable under "Full context" (#2800).
    return with_full_context(out, whole)


def _states_blindness(line: str) -> bool:
    """Whether a context line repeats the blind statement or the blind action of :func:`card_lines`."""
    text = _clean(line).casefold()
    return text.startswith(BLIND_LEAD.casefold()) or text in (
        BLIND_ACTION.casefold(),
        _LEGACY_BLIND_ACTION.casefold(),
    )


def case_id_of(correlation_key: object) -> uuid.UUID | None:
    """The case id of a remediation or mobilize gate key, else ``None`` (pure)."""
    if not isinstance(correlation_key, str):
        return None
    key = correlation_key.strip()
    for prefix in GATE_KEY_PREFIXES:
        if key.startswith(prefix):
            try:
                return uuid.UUID(key[len(prefix) :].strip())
            except ValueError:
                return None
    return None


async def match_for_case(db: AsyncSession, case: Any) -> dict[str, Any] | None:
    """The version match of an upgrade *case*, ``None`` for any other case."""
    from app.services import ctem_case_versions as case_versions

    if getattr(case, "remediation_kind", None) != "upgrade":
        return None
    running = await case_versions.for_case(db, case)
    return version_match(running, product_label=case.product_label, fixed_version=case.fixed_version)


async def complete_gate_ask(
    db: AsyncSession, correlation_key: object, ask: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """*ask* completed with the version match when it is a gate card of an upgrade case, else unchanged.

    A card the agent already gave the line keeps its own words. Never fails a
    work item write: a case that cannot be read leaves the ask as given.
    """
    if not ask or has_line(ask):
        return ask
    case_id = case_id_of(correlation_key)
    if case_id is None:
        return ask
    from app.models.ctem_case import CtemCase

    try:
        case = await db.get(CtemCase, case_id)
        match = await match_for_case(db, case) if case is not None else None
    except Exception:  # the card is written whatever the inventory answers
        logger.info("CTEM version match unavailable for %s", case_id, exc_info=True)
        return ask
    return ask if match is None else complete_ask(ask, match)

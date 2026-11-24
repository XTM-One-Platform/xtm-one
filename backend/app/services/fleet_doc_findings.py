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

"""Configuration findings of a fleet's generated documentation (#10246).

The documentation generator reads a fleet's configuration; what it finds wrong
with it (a description naming an automation the fleet does not have, a
sensitive action with no approval, a member without a role...) is not prose
inside a section any more: the model returns it as a structured list, each
finding with a kind, the member concerned, one line and a suggested fix. The
About page shows them to the people who manage the fleet as actions (Fix with
AI, Open, Dismiss); the fleet tool lists them for an orchestrator.

* **Validated.** :func:`clean_findings` keeps the findings that match
  :data:`FINDING_KINDS` and the length limits, at most :data:`FINDINGS_MAX`.
* **No meta-comment in a section.** The prompt forbids them; a sentence that
  still reads like one (:data:`META_PATTERNS`) is moved out of its section into
  a finding of kind ``other`` by :func:`move_meta_comments`.
* **A stable id (#10536).** The model names the object a finding concerns
  (``subject``: an automation, an integration, a setting); the id hashes the
  kind, the member and that subject, never the sentence, so the same finding
  keeps its id across regenerations however the model words it.
* **Dismissals that hold (#10536).** A dismissal (``documentation.dismissed_findings``)
  records who, when, an optional reason and the fingerprint of the member it
  names; it holds until that member is reconfigured or for
  :data:`DISMISSAL_DAYS` days, whatever else changes in the fleet. A dismissal
  with a reason is fed back to the generator as a decided point
  (:func:`decided_points`) so it is not raised again. A fixed finding
  disappears at the next generation, since the model no longer returns it.
* **The same findings while nothing changed (#10546 retest).** The model is
  not deterministic: two regenerations of an unchanged fleet answered
  different sets. :func:`stable_findings` keeps a stored finding while the
  print of the member it names (the fleet's own print for a finding naming no
  known member) is the one stored with it, and takes the model's new findings
  only for the members whose print changed; a fixed finding goes when its
  member is reconfigured. The prints are stored with the findings
  (``documentation.finding_prints``); without them (a first generation) the
  model's set is taken whole.
* **A dismissal follows the point, not the words (#12016).** A regeneration that
  retakes a scope may word the same point with another subject, so another id.
  :func:`same_point` matches two findings on kind, member and an equivalent
  subject (any subject for a one-per-member kind, :data:`ONE_PER_MEMBER_KINDS`):
  a holding dismissal hides every finding that means its point, and
  :func:`stable_findings` gives a retaken finding the id of the stored or
  dismissed finding it means (:func:`anchored`). A dismissal lapses only when the
  part of the member its finding is about changes (:data:`KIND_FACETS`, #12099: a
  role label edit never) or after :data:`DISMISSAL_DAYS` days
  (:func:`lapse_reason`, said by the fleet tool); a change of the fleet's gates
  re-checks whether a gate finding still stands but never lapses its dismissal.
* **A lapsed dismissal is a live suggestion (#12099).** :func:`lapsed_points` gives
  back the finding of a lapsed dismissal a regeneration dropped, so every count
  (the fleet tool, the About page chip, get_fleet, the fleet health) reads it,
  until the next whole regeneration: one that does not raise its point again drops
  the record (:func:`without_unraised_lapsed`, #12181), its premise being gone.
* **Former records settled (#12181).** A dismissal of the former shape (the
  member's whole print, role label included) is moved to its facet print while it
  holds (:func:`settled_dismissals`), before a member edit, at a dismissal and at a
  regeneration, so a role label, parent or exposure edit never lapses it.
* **Restore brings the finding back (#10546 retest).** A dismissal keeps the
  finding it hides; :func:`with_restored` puts it back in the stored findings
  when a regeneration had dropped it, with the same id.
* **A role finding computed in code.** ``member_without_role`` is not the model's any more:
  :func:`role_findings` raises it for every home member with an empty role label, so fleets of
  any size are treated alike, and it goes as soon as the label is set.
* **No finding against the configuration (#10536).** :func:`drop_contradicted`
  drops a ``missing_automation`` or ``other`` finding that names an automation
  the member has, or says a member with automations has none.

Nothing here calls a model or starts a run.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

#: The kinds of finding the model may return; anything else is ``other``.
FINDING_KINDS: tuple[str, ...] = (
    "description_mismatch",
    "missing_automation",
    "no_gate_on_sensitive_action",
    "missing_integration",
    "member_without_role",
    "other",
)
FINDINGS_MAX = 20
FINDING_TEXT_MAX = 300
FINDING_MEMBER_MAX = 120
#: Dismissals kept per fleet (the newest).
DISMISSALS_KEPT = 100
#: Days a dismissal holds while the member it names is unchanged.
DISMISSAL_DAYS = 90
DISMISSAL_REASON_MAX = 200
#: Decided points given back to the generator.
DECIDED_MAX = 40

#: Sentences that comment on the configuration instead of explaining the fleet (Sam, 9 Nov 2026):
#: "Its description also names...", "The owner should confirm...", "No separate assignment ... is listed".
META_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"\b(its|the) description (also )?(names|mentions|says|refers to)\b",
        r"\bthe owner should\b",
        r"\bshould (confirm|check|verify|clarify)\b",
        r"\bno (separate |dedicated |specific )?[\w -]{0,40}\b(is|are) (listed|configured|given|provided|shown)\b",
        r"\b(is|are) not (listed|configured|given|provided) in the (configuration|facts)\b",
        r"\bthe facts (do not|don't) (say|show|name|list)\b",
        r"\bnot (stated|specified) in the configuration\b",
    )
)

_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_BULLET = re.compile(r"^\s*([-*]|\d+\.)\s+")


def _norm(value: object) -> str:
    """*value* lower-cased, quotes and punctuation at the ends dropped, spaces collapsed."""
    return " ".join(str(value or "").lower().replace('"', " ").replace("'", " ").split()).strip(" .:;,")


def finding_id(finding: dict[str, Any]) -> str:
    """A stable id of *finding*: kind, member and subject (#10536); the line only when it names no subject."""
    subject = _norm(finding.get("subject"))
    last = f"subject:{subject}" if subject else _norm(finding.get("finding"))
    body = "|".join((_norm(finding.get("kind")), _norm(finding.get("member")), last))
    return hashlib.sha256(body.encode()).hexdigest()[:12]


def _text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit] if isinstance(value, (str, int, float)) else ""


def clean_findings(raw: object) -> list[dict[str, Any]]:
    """The valid findings of a model answer, each ``{id, kind, member, subject, finding, suggested_fix}``."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        line = _text(item.get("finding"), FINDING_TEXT_MAX)
        if not line:
            continue
        kind = str(item.get("kind") or "other")
        clean = {
            "kind": kind if kind in FINDING_KINDS else "other",
            "member": _text(item.get("member"), FINDING_MEMBER_MAX),
            "subject": _text(item.get("subject"), FINDING_MEMBER_MAX),
            "finding": line,
            "suggested_fix": _text(item.get("suggested_fix"), FINDING_TEXT_MAX),
        }
        clean["id"] = finding_id(clean)
        if clean["id"] in seen:
            continue
        seen.add(clean["id"])
        out.append(clean)
        if len(out) == FINDINGS_MAX:
            break
    return out


def is_meta_comment(sentence: str) -> bool:
    """Whether *sentence* comments on the configuration rather than explaining the fleet."""
    return any(p.search(sentence) for p in META_PATTERNS)


def move_meta_comments(sections: dict[str, str]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """*sections* without their meta-comment sentences, and one ``other`` finding per sentence moved.

    A section left empty by the move keeps its text: a section is never blanked by the guard.
    """
    cleaned: dict[str, str] = {}
    moved: list[dict[str, Any]] = []
    for key, text in sections.items():
        kept_lines: list[str] = []
        found: list[str] = []
        for line in text.split("\n"):
            parts = [m.group(0) for m in _SENTENCE.finditer(line)]
            meta = [s.strip() for s in parts if s.strip() and is_meta_comment(s)]
            if not meta:
                kept_lines.append(line)
                continue
            found += [_BULLET.sub("", s) for s in meta]
            rest = "".join(s for s in parts if s.strip() not in meta).rstrip()
            if _BULLET.sub("", rest).strip():
                kept_lines.append(rest)
        new_text = "\n".join(kept_lines).strip()
        if found and new_text:
            cleaned[key] = new_text
            moved += [{"kind": "other", "member": "", "finding": s, "suggested_fix": ""} for s in found]
        else:
            cleaned[key] = text
    return cleaned, clean_findings(moved)


def merge_findings(*lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The findings of *lists* in order, one per id, at most :data:`FINDINGS_MAX`."""
    return clean_findings([f for items in lists for f in items])


#: The subject of a computed ``member_without_role`` finding: its id is then kind, member and this subject.
ROLE_SUBJECT = "role label"


def role_findings(members: object) -> list[dict[str, Any]]:
    """One ``member_without_role`` finding per home member whose role label is empty, computed in code.

    The rule (xtm-one, fleet member role label tool): every home member of the draft context with an
    empty ``role_label`` gets the finding, whatever the fleet's size; a member with a label gets none.
    The model no longer raises the kind, so a one-member fleet and a five-member fleet are treated alike.
    """
    raw = [
        {
            "kind": "member_without_role",
            "member": name,
            "subject": ROLE_SUBJECT,
            "finding": f"{name} has no role label.",
            "suggested_fix": "Set a role label saying what it does in the fleet.",
        }
        for m in (members if isinstance(members, list) else [])
        if isinstance(m, dict)
        and (name := " ".join(str(m.get("name") or "").split()))
        and not str(m.get("role_label") or "").strip()
    ]
    return clean_findings(raw)


def with_role_findings(findings: list[dict[str, Any]], members: object) -> list[dict[str, Any]]:
    """*findings* with the role findings replaced by :func:`role_findings` of *members*."""
    rest = [f for f in findings if f.get("kind") != "member_without_role"]
    return merge_findings(rest, role_findings(members))


def member_key(name: object) -> str:
    """The key of a member in a member-fingerprint map: its name normalised; ``""`` is the fleet itself."""
    return _norm(name)


def dismissal_record(
    doc: dict[str, Any],
    finding: dict[str, Any],
    member_print: str | None,
    *,
    by: str,
    by_name: str = "",
    reason: str = "",
    now: datetime | None = None,
    facet: str = "",
) -> list[dict[str, Any]]:
    """The dismissals of *doc* with *finding* dismissed now (replacing an older one of the same id).

    The record keeps the finding itself, so the dismissed list and the decided points still name it once
    the generator stops returning it. *facet* names the part of the member *member_print* hashes
    (:func:`dismissal_print`, #12099); a record without one compares the member's whole print.
    """
    fid = finding["id"]
    kept = [d for d in doc.get("dismissed_findings") or [] if isinstance(d, dict) and d.get("id") != fid]
    record = {
        "id": fid,
        "kind": finding.get("kind") or "other",
        "member": finding.get("member") or "",
        "subject": finding.get("subject") or "",
        "finding": finding.get("finding") or "",
        "suggested_fix": finding.get("suggested_fix") or "",
        "member_print": member_print,
        **({"print_facet": facet} if facet else {}),
        "at": (now or datetime.now(UTC)).isoformat(),
        "by": by,
        "by_name": by_name[:FINDING_MEMBER_MAX],
        "reason": " ".join(str(reason or "").split())[:DISMISSAL_REASON_MAX],
    }
    return [*kept, record][-DISMISSALS_KEPT:]


#: Kinds raised at most once per member: any subject (or none) names the same point.
ONE_PER_MEMBER_KINDS: tuple[str, ...] = ("member_without_role",)
#: Words a subject carries that never tell two points apart.
_SUBJECT_FILLER = frozenset({"a", "an", "the", "of", "to", "on", "for", "its", "tool", "action", "setting"})


def _words(value: object) -> frozenset[str]:
    """The words of *value* (``send_email`` and ``Send email`` alike), filler left out."""
    return frozenset(w for w in re.findall(r"[a-z0-9]+", str(value or "").lower()) if w not in _SUBJECT_FILLER)


def same_point(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether findings (or dismissal records) *a* and *b* raise the same point, however worded (#12016).

    Same kind and same member (normalised); then, for a :data:`ONE_PER_MEMBER_KINDS` kind, any subject;
    otherwise two subjects whose words are equal or one within the other; one side naming no subject
    matches when the other's subject words all appear in its line; neither naming one, the same line.
    """
    if _norm(a.get("kind")) != _norm(b.get("kind")) or member_key(a.get("member")) != member_key(b.get("member")):
        return False
    if str(a.get("kind") or "") in ONE_PER_MEMBER_KINDS:
        return True
    sa, sb = _words(a.get("subject")), _words(b.get("subject"))
    if sa and sb:
        return sa <= sb or sb <= sa
    if sa or sb:
        named, line = (sa, b) if sa else (sb, a)
        return named <= _words(line.get("finding"))
    return _norm(a.get("finding")) == _norm(b.get("finding"))


def anchored(findings: list[dict[str, Any]], anchors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*findings*, each one meaning an *anchors* finding or dismissal record keeping that anchor's id (#12016).

    The finding takes the anchor's member and subject (and its line when the anchor names no subject),
    so :func:`finding_id` answers the anchor's id; its suggested fix stays the new one.
    """
    marks = [x for x in anchors if isinstance(x, dict) and x.get("id")]
    known = {x["id"] for x in marks}
    out: list[dict[str, Any]] = []
    for f in findings:
        anchor = None if f.get("id") in known else next((x for x in marks if same_point(f, x)), None)
        if anchor is not None:
            f = {**f, "member": anchor.get("member") or "", "subject": anchor.get("subject") or ""}
            if not f["subject"]:
                f["finding"] = anchor.get("finding") or f.get("finding") or ""
            f["id"] = finding_id(f)
        out.append(f)
    return out


#: The part of a member a finding of each kind is about (#12099): its dismissal lapses only when that
#: part changes. ``card``: name, description, persona; ``tools``: tools, integrations, MCP servers,
#: skills, handovers, approvals and automations; ``config``: everything but the role label. A role
#: label edit lapses no dismissal.
KIND_FACETS: dict[str, str] = {
    "description_mismatch": "card",
    "no_gate_on_sensitive_action": "tools",
    "missing_automation": "tools",
    "missing_integration": "tools",
}
PRINT_FACETS: tuple[str, ...] = ("card", "tools", "config")


def facet_of(kind: object) -> str:
    """The print facet a finding of *kind* depends on (:data:`KIND_FACETS`, else ``config``)."""
    return KIND_FACETS.get(str(kind or ""), "config")


def facet_key(member: object, facet: str) -> str:
    """The key of *member*'s *facet* print among the member prints."""
    return f"{member_key(member)}#{facet}"


def dismissal_print(finding: dict[str, Any], prints: dict[str, str]) -> tuple[str, str | None]:
    """``(facet, print)`` a dismissal of *finding* stores: the print of the part its kind is about.

    Prints without facets (a member unknown to them, a reader of the former shape) give the member's whole
    print and no facet, the rule before #12099.
    """
    facet = facet_of(finding.get("kind"))
    key = facet_key(finding.get("member"), facet)
    if key in prints:
        return facet, prints[key]
    return "", prints.get(member_key(finding.get("member")))


def lapse_reason(record: dict[str, Any], prints: dict[str, str], now: datetime | None = None) -> str | None:
    """Why *record* no longer hides its point, ``None`` while it holds (#10536, #12016, #12099).

    A dismissal holds while the part of the member its finding is about (``print_facet``,
    :data:`KIND_FACETS`) keeps the print stored with it, for :data:`DISMISSAL_DAYS` days; a record of
    the former shape (no facet) compares the member's whole print. The fleet's gates are not part of it:
    a gate that fixes a gate finding removes the finding itself.
    """
    try:
        at = datetime.fromisoformat(str(record.get("at")))
    except ValueError:
        return "recorded in a former format"
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    if (now or datetime.now(UTC)) - at > timedelta(days=DISMISSAL_DAYS):
        return f"older than {DISMISSAL_DAYS} days"
    facet = str(record.get("print_facet") or "")
    if _print_changed(record, prints):
        name = record.get("member") or "the fleet"
        part = {"card": "description", "tools": "tools or automations"}.get(facet)
        return f"{name} was reconfigured since" + (f" ({part})" if part else "")
    return None


def _print_changed(record: dict[str, Any], prints: dict[str, str]) -> bool:
    """Whether the part of the member *record* is about no longer has the print stored with it."""
    facet = str(record.get("print_facet") or "")
    key = facet_key(record.get("member"), facet) if facet in PRINT_FACETS else member_key(record.get("member"))
    return prints.get(key) != record.get("member_print")


def _holds(record: dict[str, Any], prints: dict[str, str], now: datetime) -> bool:
    """Whether *record* still hides its point (:func:`lapse_reason` answers none)."""
    return lapse_reason(record, prints, now) is None


def is_dismissed(finding: dict[str, Any], holding: list[dict[str, Any]]) -> bool:
    """Whether one of the *holding* dismissals hides *finding*: its id, or the same point (#12016)."""
    return any(d.get("id") == finding.get("id") or same_point(finding, d) for d in holding)


def lapsed_dismissals(
    doc: dict[str, Any] | None, prints: dict[str, str], now: datetime | None = None
) -> list[tuple[dict[str, Any], str]]:
    """The dismissals of *doc* that no longer hold, newest first, each with :func:`lapse_reason`."""
    if not isinstance(doc, dict):
        return []
    records = [d for d in doc.get("dismissed_findings") or [] if isinstance(d, dict) and d.get("id")]
    out: list[tuple[dict[str, Any], str]] = []
    for d in reversed(records):
        why = lapse_reason(d, prints, now)
        if why is not None:
            out.append((d, why))
    return out


def holding_dismissals(
    doc: dict[str, Any] | None, prints: dict[str, str], now: datetime | None = None
) -> list[dict[str, Any]]:
    """The dismissals of *doc* that still hold, newest first."""
    if not isinstance(doc, dict):
        return []
    when = now or datetime.now(UTC)
    records = [d for d in doc.get("dismissed_findings") or [] if isinstance(d, dict) and d.get("id")]
    return [d for d in reversed(records) if _holds(d, prints, when)]


def visible_findings(
    doc: dict[str, Any] | None, prints: dict[str, str], now: datetime | None = None
) -> list[dict[str, Any]]:
    """The stored findings of *doc* a manager sees: every one but those a holding dismissal hides."""
    if not isinstance(doc, dict):
        return []
    holding = holding_dismissals(doc, prints, now)
    return [f for f in clean_findings(doc.get("findings")) if not is_dismissed(f, holding)]


def with_lapsed(
    doc: dict[str, Any] | None,
    prints: dict[str, str],
    shown: list[dict[str, Any]],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """*shown* with every lapsed dismissal of *doc* counted as a live suggestion (#12099).

    A shown row a lapsed dismissal means is tagged ``lapsed`` (why, #12016); a lapsed dismissal whose
    finding a regeneration dropped from the stored list is given back from its record, tagged the same,
    so it is counted and dismissable by its id. A good-practice kind is never given back: it is computed
    live and a fixed one is gone.
    """
    from app.services.fleet_practice_suggestions import is_practice_kind

    out = [dict(f) for f in shown]
    for record, why in lapsed_dismissals(doc, prints, now):
        match = next((f for f in out if f.get("id") == record["id"] or same_point(f, record)), None)
        if match is not None:
            match.setdefault("lapsed", why)
            continue
        if is_practice_kind(record.get("kind")):
            continue
        out.append(
            {
                **{k: record.get(k) or "" for k in ("id", "kind", "member", "subject", "finding", "suggested_fix")},
                "lapsed": why,
            }
        )
    return out


def settled_dismissals(
    records: list[dict[str, Any]], prints: dict[str, str], now: datetime | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """*records* with each holding dismissal of the former shape given its facet print, and whether any was (#12181).

    A record written before #12099 carries the member's whole print, which a role label, parent or exposure
    edit changes: it is moved to the print of the part its kind is about (:func:`dismissal_print`) while it
    still holds, so a later member edit lapses it only when that part changes. A lapsed record is left as is.
    """
    when = now or datetime.now(UTC)
    out: list[dict[str, Any]] = []
    changed = False
    for d in records:
        if isinstance(d, dict) and d.get("id") and not d.get("print_facet") and _holds(d, prints, when):
            facet, member_print = dismissal_print(d, prints)
            if facet:
                d = {**d, "print_facet": facet, "member_print": member_print}
                changed = True
        out.append(d)
    return out, changed


def without_unraised_lapsed(
    records: list[dict[str, Any]], prints: dict[str, str], findings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """*records* without the lapsed dismissals a whole regeneration did not raise again (#12181).

    The regeneration read every member as it is now: a dismissal lapsed because its member's part changed,
    whose point is not among its *findings*, is about a premise that no longer holds (the description was
    fixed), so it is dropped and :func:`with_lapsed` gives nothing back for it. A dismissal lapsed by age
    (its member unchanged) and a good-practice record (computed live) are kept.
    """
    from app.services.fleet_practice_suggestions import is_practice_kind

    kept: list[dict[str, Any]] = []
    for d in records:
        if (
            isinstance(d, dict)
            and d.get("id")
            and not is_practice_kind(d.get("kind"))
            and _print_changed(d, prints)
            and not any(f.get("id") == d["id"] or same_point(f, d) for f in findings)
        ):
            continue
        kept.append(d)
    return kept


def lapsed_points(
    doc: dict[str, Any] | None,
    prints: dict[str, str],
    shown: list[dict[str, Any]],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The rows :func:`with_lapsed` adds to *shown*: lapsed dismissals whose finding is not in it."""
    return with_lapsed(doc, prints, shown, now)[len(shown) :]


def without_dismissals(doc: dict[str, Any], ids: set[str] | None) -> tuple[list[dict[str, Any]], int]:
    """The dismissals of *doc* with *ids* restored (every one when ``None``), and how many were removed."""
    records = [d for d in doc.get("dismissed_findings") or [] if isinstance(d, dict)]
    kept = [d for d in records if ids is not None and d.get("id") not in ids]
    return kept, len(records) - len(kept)


def with_restored(findings: list[dict[str, Any]], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*findings* with the finding of each restored dismissal *records* put back when absent, same id.

    A regeneration may have dropped a dismissed finding from the stored list (the record still names it):
    restoring it must make it active again, never leave it in neither list. A restored finding is kept
    within :data:`FINDINGS_MAX`, the oldest stored ones giving way.
    """
    present = {f["id"] for f in findings}
    back = [
        f
        for f in clean_findings([r for r in records if isinstance(r, dict) and r.get("id") not in present])
        if f["id"] not in present
    ]
    if not back:
        return findings
    return [*findings[: max(FINDINGS_MAX - len(back), 0)], *back][-FINDINGS_MAX:]


#: The print key of the fleet's approval gates among the finding prints: a gate finding is fixed there.
GATES_PRINT_KEY = "@fleet-gates"


def gates_print(autonomy_level: object, approval_gates: object) -> str:
    """A short hash of what a ``no_gate_on_sensitive_action`` finding depends on besides its member."""
    body = repr([str(autonomy_level or ""), approval_gates or []])
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def _scope(finding: dict[str, Any], prints: dict[str, str]) -> str:
    """The print key a finding depends on: its member when the prints know it, else the fleet itself."""
    key = member_key(finding.get("member"))
    return key if key in prints else ""


def stable_findings(
    previous: list[dict[str, Any]],
    new: list[dict[str, Any]],
    previous_prints: dict[str, str] | None,
    prints: dict[str, str],
    dismissed: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The findings to store after a whole regeneration: the same set while the configuration is unchanged.

    A stored finding stays (same id) while the print of its scope (:func:`_scope`) equals the one stored
    with it; a new finding is taken only when its scope's print changed. Without stored prints (a first
    generation, a version of the former shape) the *new* set is taken whole. A new finding that means a
    stored finding or a *dismissed* record keeps that id (:func:`anchored`, #12016).
    """
    new = anchored(new, [*previous, *(dismissed or [])])
    if not isinstance(previous_prints, dict) or not previous_prints:
        return merge_findings(new)

    def unchanged(finding: dict[str, Any]) -> bool:
        scope = _scope(finding, prints)
        same = scope in previous_prints and previous_prints.get(scope) == prints.get(scope)
        if same and finding.get("kind") == "no_gate_on_sensitive_action":
            # A gate added on the fleet fixes it: the gates' print counts too.
            same = previous_prints.get(GATES_PRINT_KEY) == prints.get(GATES_PRINT_KEY)
        return same

    kept = [f for f in previous if unchanged(f)]
    added = [f for f in new if not unchanged(f)]
    return merge_findings(kept, added)


def decided_points(doc: dict[str, Any] | None, prints: dict[str, str]) -> list[str]:
    """The holding dismissals that carry a reason, as lines the generator reads as decided (#10536)."""
    out: list[str] = []
    for d in holding_dismissals(doc, prints):
        if not d.get("reason"):
            continue
        who = f"{d['member']}: " if d.get("member") else ""
        what = d.get("subject") or d.get("finding") or ""
        out.append(f"{who}{what} ({d['kind']}). Decided: {d['reason']}")
        if len(out) == DECIDED_MAX:
            break
    return out


#: "has no assignment", "lists no automation", "no assignment is listed": a member said to have none.
_NONE_CLAIM = re.compile(
    r"\b(?:(?:lists|has|with|having)\s+no\s+(?:assignments?|automations?)\b(?!\s+(?:for|that|to|which|on)\b)"
    r"|no\s+(?:assignments?|automations?)\s+(?:is|are)\s+(?:listed|attached|configured))",
    re.I,
)
#: Kinds a configuration contradiction can drop; a gate or a role finding is not about automations.
CHECKED_KINDS = ("missing_automation", "other")
_NAME_MIN = 8


def _automation_core(name: str) -> str:
    """An automation's name without its "Member: " prefix, normalised."""
    head, sep, rest = name.partition(": ")
    return _norm(rest if sep and rest else head)


def drop_contradicted(
    findings: list[dict[str, Any]], automations_by_member: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """*findings* without those the configuration contradicts (#10536), checked in code, no model call.

    A ``missing_automation`` or ``other`` finding about a member is dropped when it names an automation
    that member has, or, naming no subject, says that member has no automation while it has at least one.
    *automations_by_member* maps :func:`member_key` of a member's name (and of its slug) to its automation names.
    """
    kept: list[dict[str, Any]] = []
    for f in findings:
        names = automations_by_member.get(member_key(f.get("member"))) or []
        if f.get("kind") in CHECKED_KINDS and names:
            text = _norm(f"{f.get('subject') or ''} {f.get('finding') or ''}")
            subject = _norm(f.get("subject"))
            cores = [c for c in (_automation_core(n) for n in names) if len(c) >= _NAME_MIN]
            named = any(c in text for c in cores) or (
                len(subject) >= _NAME_MIN and any(subject in _norm(n) for n in names)
            )
            # "Has no automation" about the member itself (no subject named); a named missing thing stays.
            none_claim = not subject and _NONE_CLAIM.search(str(f.get("finding") or ""))
            if named or none_claim:
                continue
        kept.append(f)
    return kept


def _provider_words(provider: str) -> set[str]:
    """The forms a provider key takes in a sentence: ``google_search`` -> ``google_search``, ``google search``."""
    key = _norm(provider)
    return {w for w in (key, key.replace("_", " "), key.replace("-", " ")) if len(w) >= 3}


def drop_bound_integrations(
    findings: list[dict[str, Any]], integrations_by_member: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """*findings* without a ``missing_integration`` the member's bindings contradict, checked in code.

    *integrations_by_member* maps :func:`member_key` of a member's name (and of its slug) to the provider
    keys it is bound to, directly (``integration_bindings``) or through a template binding resolved per
    user at run time (``integration_template_bindings``). A finding naming one of those providers in its
    subject or its line is dropped: the member has the integration, the model misread it.
    """
    kept: list[dict[str, Any]] = []
    for f in findings:
        providers = integrations_by_member.get(member_key(f.get("member"))) or []
        if f.get("kind") == "missing_integration" and providers:
            text = _norm(f"{f.get('subject') or ''} {f.get('finding') or ''}")
            words = {w for p in providers for w in _provider_words(p)}
            if any(re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text) for w in words):
                continue
        kept.append(f)
    return kept


__all__ = [
    "DISMISSAL_DAYS",
    "FINDINGS_MAX",
    "FINDING_KINDS",
    "GATES_PRINT_KEY",
    "KIND_FACETS",
    "META_PATTERNS",
    "ONE_PER_MEMBER_KINDS",
    "PRINT_FACETS",
    "anchored",
    "clean_findings",
    "decided_points",
    "dismissal_print",
    "dismissal_record",
    "drop_bound_integrations",
    "drop_contradicted",
    "facet_key",
    "facet_of",
    "finding_id",
    "gates_print",
    "holding_dismissals",
    "is_dismissed",
    "is_meta_comment",
    "lapse_reason",
    "lapsed_dismissals",
    "lapsed_points",
    "member_key",
    "merge_findings",
    "move_meta_comments",
    "role_findings",
    "same_point",
    "settled_dismissals",
    "stable_findings",
    "visible_findings",
    "with_lapsed",
    "with_restored",
    "with_role_findings",
    "without_dismissals",
    "without_unraised_lapsed",
]

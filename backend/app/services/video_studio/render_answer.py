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

"""What a render's or a scene preview's answer is made of: one verdict, and nothing else to copy.

Every wave of the 21 Oct 2026 sweep ended with two or three residual items reported by hand: the answers mixed
what blocks, what only warns, what a setting the author chose makes the video show and the platform's sizing in
one paragraph, and the agent copied all of it. An answer is now, as its first line says it in JSON and its text
after:

* ``status``: ``green`` when :attr:`RenderAnswer.findings` is empty, ``blocked`` when one of them blocks, else
  ``warnings`` - a warning alone never blocks;
* ``findings``: what blocks or warns (``render_checks.Finding``): ``{kind, severity: blocks | warns, at, end,
  scene, what, fix}``, with the frame it shows;
* ``by_design``: what a setting the author chose makes the video show - a contain fit's bands, the colours at its
  light, what its key took out of the margin (``insert_checks.classify``), a card or a statement holding its picture
  (``frame_freeze.DesignedHolds``): ``{what, because, insert, scene}``;
* ``facts``: what this render measured that blocks nothing - its checks that passed, its inserts' numbers, a
  neighbour's finding its own preview judges, its notes, its estimate and its drawing: ``{what, ...}``;
* ``infra_once``: the advice on the platform's sizing its pre-flight was the first to need
  (``render_preflight.Infra``), on the answer of the call that started it only; a scene preview's, what its
  worker needs so its previews start at once, when it waited for memory (``render_preflight.infra_of``);
  and what the render harness drew wrong (``render_checks.harness_defects``), with its frame - never on a
  later drawing of its scene as it is now that drew it on the same frames; and a bezel reading of a join frame drawn
  smaller than the footage the save measured, set aside for the save's measure it disagreed with
  (``render_checks.Checks.infra``).

"All gates green" is ``findings`` empty (:meth:`RenderAnswer.verdict`): the agent reports the findings, and
nothing else as an issue.

A finished render followed by ``get_video_jobs`` answers compact by default (:meth:`RenderAnswer.compact_dict`,
:meth:`RenderAnswer.compact_text`): its findings in full, its ``headlines`` (what each check heard and watched,
``render_checks.check_lines``), the count of its by-design and facts lines and the platform lines, the whole answer
stored as a file (issue #8719: a 150 s render with 9 screen inserts answered about 100,000 characters inline).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.video_studio import insert_checks, render_checks

GREEN, WARNINGS, BLOCKED = "green", "warnings", "blocked"
#: A finding's severity as an answer says it: a failing one blocks, a warning warns.
BLOCKS, WARNS = "blocks", "warns"
#: The kind of the finding of a render that ended without drawing what its checks would judge.
RENDER_FAILED = "render_failed"


@dataclass
class RenderAnswer:
    """The verdict of one render or scene preview, and what it is made of."""

    findings: list[dict] = field(default_factory=list)
    by_design: list[dict] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    infra_once: list[str] = field(default_factory=list)
    #: What each check heard and watched (``render_checks.check_lines``), also among :attr:`facts`: the lines a
    #: compact answer keeps.
    headlines: list[str] = field(default_factory=list)
    #: Which release judged and drew it (:func:`versions_of`): the quality report version, the screen inserts'
    #: drawing version, the live drawing version and the build; empty for a render that stored no checks.
    versions: dict = field(default_factory=dict)

    @property
    def blocking(self) -> int:
        """How many of :attr:`findings` block."""
        return sum(1 for item in self.findings if item.get("severity") == BLOCKS)

    @property
    def status(self) -> str:
        """``green`` when nothing blocks or warns, ``blocked`` when a finding blocks, else ``warnings``."""
        if not self.findings:
            return GREEN
        return BLOCKED if self.blocking else WARNINGS

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "findings": self.findings,
            "by_design": self.by_design,
            "facts": self.facts,
            "infra_once": self.infra_once,
            **self.versions,
        }

    def compact_dict(self) -> dict:
        """:meth:`as_dict` with the by-design and facts lists counted, never listed: the findings in full. The
        headlines are counted too: :meth:`compact_text`, beside it in the same answer, lists them once (issue #10480)."""
        return {
            "status": self.status,
            "findings": self.findings,
            "by_design_count": len(self.by_design),
            "facts_count": len(self.facts),
            "headlines_count": len(self.compact_headlines()),
            "infra_once": self.infra_once,
            **self.versions,
        }

    def compact_text(self) -> str:
        """The verdict, every finding, the headline lines and the platform lines; the by-design and facts counted."""
        lines = [self.verdict().replace("By design and facts below are no findings", "By design and facts are no findings")]
        if self.findings:
            lines.append("Findings:")
            lines.extend(f"- {_finding_text(item)}" for item in self.findings)
        if headlines := self.compact_headlines():
            lines.append("Checks (what each heard and watched; no finding):")
            lines.extend(f"- {text}" for text in headlines)
        if self.by_design or self.facts:
            lines.append(
                f"Not listed here: {len(self.by_design)} by-design and {len(self.facts)} facts line(s), no finding "
                "among them: the full report holds them."
            )
        if self.infra_once:
            lines.append("Platform (said once, for the administrator; no finding):")
            lines.extend(f"- {text}" for text in self.infra_once)
        if line := versions_line(self.versions):
            lines.append(line)
        return "\n".join(lines)

    def compact_headlines(self) -> list[str]:
        """:attr:`headlines` each said once, in their order: a line the start and the end of a render both say is one."""
        return list(dict.fromkeys(self.headlines))

    def verdict(self) -> str:
        """The answer's summary, read off :attr:`findings` alone."""
        if not self.findings:
            return (
                "Verdict: all gates green - no finding. By design and facts below are no findings: report none of "
                "them as an issue."
            )
        blocking = self.blocking
        warning = len(self.findings) - blocking
        if not blocking:
            return (
                f"Verdict: not blocked - {warning} warning{'s' if warning != 1 else ''}, nothing blocking. These are "
                "the issues to report or fix, and the only ones."
            )
        counted = ", ".join(
            part
            for part in (f"{blocking} blocking" if blocking else "", f"{warning} warning" if warning else "")
            if part
        )
        return (
            f"Verdict: blocked - {len(self.findings)} finding{'s' if len(self.findings) != 1 else ''} ({counted}). "
            "These are the issues to report or fix, and the only ones."
        )

    def text(self) -> str:
        """The verdict, then each list under its heading."""
        lines = [self.verdict()]
        if self.findings:
            lines.append("Findings:")
            lines.extend(f"- {_finding_text(item)}" for item in self.findings)
        if self.by_design:
            lines.append("By design (what a setting the author chose shows; no finding):")
            lines.extend(f"- {_where(item)}{item['because']}: {item['what']}" for item in self.by_design)
        if self.facts:
            lines.append("Facts (what this render measured; no finding):")
            lines.extend(f"- {_where(item)}{item['what']}" for item in self.facts)
        if self.infra_once:
            lines.append("Platform (said once, for the administrator; no finding):")
            lines.extend(f"- {text}" for text in self.infra_once)
        if line := versions_line(self.versions):
            lines.append(line)
        return "\n".join(lines)


def versions_of(checks: dict | None, drawing: dict | None = None) -> dict:
    """``{quality_report_version, drawing_version, live_drawing_version, build}`` of a render's stored *checks*
    (``render_checks.report_provenance``; *drawing*: its ``VideoRender.drawing``, read when the checks predate
    ``drawing_version``): what tells whether a merged compositing fix drew it without reading the source of the
    build. ``{}`` for a render that stored no checks."""
    found = render_checks.report_provenance(checks, None, drawing)
    if found is None:
        return {}
    return {
        "quality_report_version": found["quality_report_version"],
        "drawing_version": found["drawing_version"],
        "live_drawing_version": found["live_drawing_version"],
        "build": found["build"],
    }


def versions_line(versions: dict) -> str:
    """``Judged under quality report v52, drawn at screen-insert drawing 21 (live 21), build 1.261106.0``: the line an
    answer ends with (:func:`versions_of`), a render drawn at an older drawing told to render again; ``""`` for none."""
    if not versions:
        return ""
    report = versions.get("quality_report_version")
    drawn_at, live = versions.get("drawing_version"), versions.get("live_drawing_version")
    text = f"Judged under quality report {f'v{report}' if report is not None else 'version not recorded'}, "
    text += f"drawn at screen-insert drawing {drawn_at if drawn_at is not None else 'not recorded'} (live {live})"
    text += f", build {versions['build']}" if versions.get("build") else ", build not recorded"
    if drawn_at is None or (live is not None and drawn_at < live):
        text += f": drawn at drawing {drawn_at if drawn_at is not None else 'not recorded'}, live {live}: render again"
    return text + "."


def _where(item: dict) -> str:
    if item.get("insert"):
        return f"insert {item['insert']}: "
    return f"scene `{item['scene']}`: " if item.get("scene") else ""


def _finding_text(item: dict) -> str:
    when = render_checks.clock(item.get("at")) if item.get("at") is not None else ""
    if when and item.get("end") is not None:
        when += f"-{render_checks.clock(item.get('end'))}"
    where = " ".join(part for part in (when, f"scene `{item['scene']}`" if item.get("scene") else "") if part)
    frame = f" (frame: file `{item['frame_file_id']}`)" if item.get("frame_file_id") else ""
    head = f"[{item.get('severity')}]" + (f" {where}" if where else "") + f" ({item.get('kind')})"
    return f"{head}: {item.get('what')} Fix: {str(item.get('fix') or '').rstrip('.')}.{frame}"


def _what(message: str, fix: str) -> str:
    """*message* without the fix it ends with, when it does: the fix is listed apart."""
    for separator in (" - ", ": "):
        if fix and message.endswith(f"{separator}{fix}"):
            return message[: -len(separator) - len(fix)].rstrip()
    return message


def finding_item(finding: dict) -> dict:
    """A stored finding (``render_checks.Finding.as_dict``) as an answer lists it."""
    fix = render_checks.finding_fix(finding)
    data = finding.get("data") or {}
    item = {
        "kind": finding.get("kind"),
        "severity": BLOCKS if finding.get("severity") == render_checks.FAIL else WARNS,
        "at": finding.get("at"),
        "end": finding.get("end"),
        "scene": finding.get("scene_id"),
        "what": _what(str(finding.get("message") or ""), fix),
        "fix": fix,
    }
    if data.get("frame_file_id"):
        item["frame_file_id"] = data["frame_file_id"]
    if data.get("insert"):
        item["insert"] = data["insert"]
    return item


def of_checks(checks: dict | None) -> RenderAnswer:
    """What a render's *checks* make of its answer: its findings, its inserts' measurements classed
    (``render_checks.insert_measured``), what each check heard and watched, their notes."""
    answer = RenderAnswer()
    for finding in render_checks.findings_of(checks):
        if (finding.get("data") or {}).get("neighbour") and finding.get("severity") != render_checks.FAIL:
            # A neighbour's, seen in the transition: its own preview judges it, never this one - but for what
            # blocks on the join's frames, this preview's own (``render_checks.settle_neighbours``).
            answer.facts.append(
                {
                    "scene": finding.get("scene_id"),
                    "what": f"seen in the transition from this scene: {finding.get('message')}",
                }
            )
            continue
        answer.findings.append(finding_item(finding))
    found = render_checks.insert_findings(checks)
    for report in render_checks.insert_reports(checks):
        label = str(report.get("label") or report.get("key") or "")
        neighbour = bool(report.get("neighbour"))
        for measured in render_checks.insert_measured(report, found.get(str(report.get("key")), set())):
            if measured.cls == insert_checks.FINDING:
                continue
            item = {"insert": label, "scene": report.get("scene_id"), "what": measured.text}
            if neighbour:
                # Measured on this preview's frames of the join: numbers, never a deferral to another preview.
                answer.facts.append({**item, "what": f"a neighbour's, measured on this preview's frames of the join: {measured.text}"})
            elif measured.cls == insert_checks.BY_DESIGN:
                answer.by_design.append({**item, "because": measured.because})
            else:
                answer.facts.append(item)
    # A card, a statement or a still asked still holding its picture: its design, never a frozen picture.
    for held in ((checks or {}).get("frames") or {}).get("by_design") or []:
        if isinstance(held, dict):
            answer.by_design.append(
                {
                    "scene": held.get("scene"),
                    "what": (
                        f"the picture holds still {float(held.get('seconds') or 0.0):.1f} s "
                        f"({render_checks.clock(held.get('at'))} to {render_checks.clock(held.get('end'))})"
                    ),
                    "because": held.get("because") or "still by design",
                }
            )
    answer.versions = versions_of(checks)
    answer.headlines = render_checks.check_lines(checks)
    answer.facts.extend({"what": line} for line in answer.headlines)
    answer.facts.extend({"what": str(note)} for note in (checks or {}).get("notes") or [])
    # What the render harness drew wrong: for the administrator, once - the drawing of its scene as it is now
    # that said it first named, never said again nor a finding.
    for defect in render_checks.harness_of(checks):
        data = defect.get("data") or {}
        if data.get("said", True):
            answer.infra_once.append(render_checks.harness_line(defect))
        else:
            answer.facts.append(
                {
                    "scene": defect.get("scene_id"),
                    "what": f"the render harness's defect render {data.get('repeated_from')} said, on the same frames: "
                    f"{defect.get('kind')} at {render_checks.clock(defect.get('at'))}",
                }
            )
    # What the platform's own measurements disagreed on: a bezel reading set aside for the save's.
    answer.infra_once.extend(str(line) for line in (checks or {}).get("infra") or [] if line)
    return answer


def of_render(
    render: Any,
    checks: dict | None,
    *,
    facts: list[str] | tuple[str, ...] = (),
    infra_once: list[str] | tuple[str, ...] = (),
    headlines: list[str] | tuple[str, ...] | None = None,
) -> RenderAnswer:
    """The answer of a finished *render* - a whole render or a scene preview -, its *checks* naming the files of the
    conversation (``render_checks.with_files``): :func:`of_checks`, its notes, *facts* (its estimate, what it measured,
    its stalls), *headlines* (the same in their short form for the compact answer; *facts* when not given) and
    *infra_once* (the advice its pre-flight says, on the call that started it). A render that ended
    with no finding of its checks to say why - its memory, a crash, a cancel - blocks with that reason; one the
    platform failed (``VideoRender.failure``) says so, that it ran again once, and that the video needs no
    change (``render_retry.finding_words``)."""
    from app.models.video_studio import JOB_READY
    from app.services.video_studio import render_retry, render_segments

    answer = of_checks(checks)
    answer.versions = versions_of(checks, getattr(render, "drawing", None))
    answer.facts.extend({"what": str(note)} for note in getattr(render, "notes", None) or [])
    # Failed by its checks after its drawing: its frames kept for the next render of its revision.
    if (kept_until := render_segments.frames_kept_until(render)) is not None:
        said = render_segments.kept_until_line(kept_until)
        answer.facts.insert(0, {"what": said[:1].upper() + said[1:]})
    answer.facts.extend({"what": text} for text in facts if text)
    # Its estimate, what it measured and its stalls: headlines a compact answer keeps too.
    answer.headlines.extend(text for text in (facts if headlines is None else headlines) if text)
    answer.infra_once.extend(text for text in infra_once if text)
    if getattr(render, "status", None) != JOB_READY and not answer.blocking:
        what = f"{getattr(render, 'status', 'failed')}: {getattr(render, 'error', None) or 'no reason given'}"
        fix = "what its reason names, then render again"
        platform = render_retry.finding_words(getattr(render, "failure", None))
        if platform is not None:
            what, fix = f"{what.rstrip()} {platform[0]}", platform[1]
        answer.findings.insert(
            0,
            {
                "kind": RENDER_FAILED,
                "severity": BLOCKS,
                "at": None,
                "end": None,
                "scene": getattr(render, "scene_id", None),
                "what": what,
                "fix": fix,
            },
        )
    return answer

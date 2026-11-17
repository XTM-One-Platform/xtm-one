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

"""The subjects of an AI shot against its prompt.

A video model asked for "six real cows in a meadow" may draw them with human
hands, melt one into another, or end the shot with people where the cows
stood; a shot that "stays a real cow" may not. Each such shot used to be
marked ready, rendered, and rejected by the person. After a shot with a prompt
is stored, :func:`check` reads its first, middle and last frames against what
the prompt asked and fails the shot when a subject is missing, of another
kind, deformed or replaced, or when something the negative prompt excludes
shows.

Two readings by the person's own chat model, the verdict computed here
(:func:`drifted`), never asked of the model - the ``text_drift`` pattern:

1. the prompt alone (text, no picture): the subjects it names - living
   beings, people, characters, the objects the shot is about - with their
   kind (:func:`read_prompt`), and the camera it asks for (:data:`CAMERAS`:
   still, moving, or not said - what the save-time quality check holds the
   shot's motion against), and which terms of the negative prompt are
   about the camera moving (``camera_terms``): a still frame cannot
   show a camera move, so those are never asked of the frames - the shot's
   measured motion answers them (``frame_qc.camera_motion`` against its
   ``max_camera_motion``). A prompt naming no subject (a landscape, an
   abstract motion), with nothing excluded and nothing avoided, ends the check
   there: its frames are never read. The answer is kept with the shot for the
   same prompt, so a shot generated again does not ask it again;
2. the three frames in ONE call, with the subjects, the terms of the
   negative prompt (split in code, :func:`excluded_terms`) and what the
   template's art direction avoids: what each frame shows of each
   subject - there or not, of the kind asked or not, what took its place, its
   deformities - and which of those terms it shows.

A subject that leaves the frame for a moment is not a drift; one that never
shows, shows as another kind, is replaced or deformed is, and so is a term of
the author's negative prompt shown. A plural subject described by variants
("five cats, each a different colour: ginger, grey, white") is one
subject of several (:attr:`Subject.several`) - its count and its variants
said to the frames reading, subjects of one kind read apart grouped so in code
(:func:`grouped`) - checked as several distinct subjects of its kind, none
identical: a variant not shown is never a finding. A term only the template avoids is never
a drift: the shot is kept and says it shows it (:meth:`ContentCheck.avoided_note`),
a template's taste being no reason to throw away a paid shot. A check that
cannot run (no chat model, an unusable answer) leaves the shot ready with a note.

A shot made from a picture opens on that picture: a term of the negative
prompt the start picture already shows (a raised hand under "waving") is
read once before the shot is paid for (:func:`read_start_picture`, kept in
:data:`START_SHOWN_KEY`, said by ``validate_only``) and never a finding of
the first frame; the middle and last frames are judged against the start
picture for it, a finding only where it is new or grows (issue #10053).

On a shot carrying a screen insert the frames reading is also told
the screen's rectangle and says of each excluded term seen whether it shows
only where it enters it (``Finding.keep_out``): such a term is held to the
take's measure of what stands in front of the screen
(``insert_keep_out.judge``), never to the reading alone.

Each finding has a severity, decided here (:func:`drifted`): a subject
missing, of another kind or replaced is ``major``; a deformity or an excluded
term is ``major`` unless the model observed it shows only to a close look at
a still (``noticeable: false``), then ``minor`` - a detail nobody sees at
1080p. A shot fails on its major findings; its minor ones are noted, unless
the shot was asked strict (``accept_minor_drift: false``,
:func:`failing`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: The frames of the shot read, by the name the model is told, and where each is taken.
FRAMES = ("first_frame", "middle_frame", "last_frame")
#: The most subjects a prompt is read for: a shot list names a handful.
MAX_SUBJECTS = 6
#: The most variants a subject of several keeps: what the frames reading is told of it, never checked one by one.
MAX_VARIANTS = 12
#: The most excluded terms read from a negative prompt.
MAX_EXCLUDED = 12
#: The params key keeping the subjects read from a shot's prompt (with the digest of that prompt).
SUBJECTS_KEY = "content_subjects"
#: The params key of a shot asked strict: ``False`` fails it on minor findings too.
ACCEPT_MINOR_KEY = "accept_minor_drift"
#: The params key of the negative prompt's terms the start picture already shows: ``{"digest", "terms"}``, the digest
#: of the negative prompt and the start picture read (:func:`start_digest`).
START_SHOWN_KEY = "start_shows_excluded"
_FRAME_MAX_SIDE = 1024
#: The camera a prompt asks for: kept still, moving, or not said.
CAMERA_LOCKED, CAMERA_MOVING, CAMERA_UNSPECIFIED = "locked", "moving", "unspecified"
CAMERAS = (CAMERA_LOCKED, CAMERA_MOVING, CAMERA_UNSPECIFIED)
MAJOR, MINOR = "major", "minor"


@dataclass(frozen=True)
class Subject:
    """A subject the prompt names: what it is called, and its kind (``an animal: a cow``).

    A plural subject described by variants ("five cats, each a different colour: ginger, grey, white") is one
    subject of several: its *count* when the prompt says it, its *variants* as the prompt lists them - checked as
    several distinct subjects of its kind, none identical, never variant by variant (:func:`drifted`)."""

    name: str
    kind: str
    count: int | None = None
    variants: tuple[str, ...] = ()

    @property
    def several(self) -> bool:
        return (self.count or 0) >= 2 or len(self.variants) >= 2

    def as_dict(self) -> dict:
        extra = {"count": self.count, "variants": list(self.variants)} if self.several else {}
        return {"name": self.name, "kind": self.kind, **extra}

    @classmethod
    def from_dict(cls, item: dict) -> Subject:
        count = item.get("count")
        variants = item.get("variants") if isinstance(item.get("variants"), list) else []
        return cls(
            str(item.get("name") or "").strip()[:80],
            str(item.get("kind") or item.get("name") or "").strip()[:120],
            int(count) if isinstance(count, int | float) and not isinstance(count, bool) and count >= 2 else None,
            tuple(str(v).strip()[:40] for v in variants if isinstance(v, str) and v.strip())[:MAX_VARIANTS],
        )


def grouped(subjects: list[Subject]) -> list[Subject]:
    """*subjects* with those of one kind told apart only by their names - "ginger cat", "grey cat" - as one subject of
    several, its variants their names: the variants a prompt lists are never a contract one by one. A subject
    of several already keeps its own; the first of a kind gives the group its place."""
    by_kind: dict[str, list[Subject]] = {}
    for subject in subjects:
        by_kind.setdefault(subject.kind.strip().lower(), []).append(subject)
    out: list[Subject] = []
    for subject in subjects:
        same = by_kind[subject.kind.strip().lower()]
        if len(same) < 2:
            out.append(subject)
        elif subject is same[0]:
            variants = tuple(dict.fromkeys(v for member in same for v in (member.variants or (member.name,))))
            counts = [member.count for member in same if member.count]
            out.append(Subject(subject.kind, subject.kind, max(counts) if counts else None, variants[:MAX_VARIANTS]))
    return out


@dataclass(frozen=True)
class Finding:
    """Something a frame shows that the prompt did not ask for, and how much it shows (:data:`MAJOR` / :data:`MINOR`)."""

    subject: str
    frame: str
    what: str
    severity: str = MAJOR
    #: An excluded term shown only on screens: each screen's four corners, in shares of the frame.
    screens: tuple[tuple[tuple[float, float], ...], ...] = ()
    #: The finding is a term of the negative prompt shown: :attr:`subject` is that term.
    excluded: bool = False
    #: An excluded term shown only where it enters the keep-out rectangle of a screen insert: judged by the
    #: take's measure of what stands in front of the screen (``insert_keep_out.judge``).
    keep_out: bool = False


def excluded_shown(findings: list[Finding]) -> list[str]:
    """The negative prompt's terms *findings* show, once each, as the negative prompt spells them."""
    return list(dict.fromkeys(f.subject for f in findings if f.excluded))


def _listed(findings: list[Finding], start_findings: list[dict] | None = None) -> str:
    def said(f: Finding) -> str:
        inherited = ", inherited from the start picture" if inherited_from_start(f, start_findings) else ""
        return (
            f"{f.what} ({f.frame.replace('_', ' ')}, {f.severity}{inherited})"
            if f.frame
            else f"{f.what} ({f.severity})"
        )

    shown = "; ".join(said(f) for f in findings[:6])
    return shown + (f"; and {len(findings) - 6} more" if len(findings) > 6 else "")


@dataclass
class ContentCheck:
    """What the check found: the findings, or why it could not tell."""

    checked: bool
    findings: list[Finding] = field(default_factory=list)
    note: str | None = None
    #: The subjects read from the prompt, kept with the shot (:data:`SUBJECTS_KEY`).
    subjects: list[Subject] = field(default_factory=list)
    #: The camera the prompt asks for (:data:`CAMERAS`), kept with the subjects; ``None`` when not read.
    camera: str | None = None
    #: What the template's art direction avoids that a frame shows: said, never a drift.
    avoided_seen: list[str] = field(default_factory=list)
    #: The negative prompt's terms about the camera moving: never read on a frame, measured instead.
    camera_terms: list[str] = field(default_factory=list)
    #: The first-frame findings the start picture itself showed (:data:`START_FINDINGS_KEY`): a take's first-frame
    #: finding among them is said inherited from the start picture, not drift of the model.
    start_findings: list[dict] = field(default_factory=list)

    def avoided_note(self, template: str | None) -> str | None:
        """The note a shot showing what its template avoids carries, or ``None``."""
        if not self.avoided_seen:
            return None
        return (
            f"The shot shows {', '.join(self.avoided_seen)}, which the {template or 'template'} art direction "
            "avoids: kept - generate it again in place if it stands out in the video."
        )

    def failing(self, *, accept_minor: bool = True) -> list[Finding]:
        """The findings that fail the shot: its major ones, every one when minor drift is not accepted."""
        return [f for f in self.findings if not accept_minor or f.severity == MAJOR]

    def accepted(self, *, accept_minor: bool = True) -> list[Finding]:
        """The findings the shot is kept with: its minor ones when minor drift is accepted."""
        return [f for f in self.findings if accept_minor and f.severity == MINOR]

    def reason(self, *, accept_minor: bool = True, findings: list[Finding] | None = None) -> str:
        """The failure a drifted shot carries, each failing finding (or *findings*) named with its severity."""
        failing = self.failing(accept_minor=accept_minor) if findings is None else findings
        inherited = [f for f in failing if inherited_from_start(f, self.start_findings)]
        remedy = (
            " The start picture already shows it: start from another picture, use reference images instead, or "
            "pass content_check: false for a shot meant to transform the scene."
            if inherited
            else ""
        )
        return (
            f"content drift: the shot does not show what its prompt asks - "
            f"{_listed(failing, self.start_findings)}. Generate it again with the subjects described "
            "more precisely (or reference images of them), a calmer motion, and what must not appear in the "
            f"negative prompt.{remedy}"
        )

    def cleanable(self, *, accept_minor: bool = True) -> bool:
        """Whether every failing finding is an excluded term shown only on located screens."""
        failing = self.failing(accept_minor=accept_minor)
        return bool(failing) and all(f.screens for f in failing)

    def sightings(self) -> list[Any]:
        """Every screen a finding located, as ``stray_screens.Sighting``: each read frame's screens, once."""
        from app.services.video_studio.stray_screens import Sighting

        found: dict[tuple, Any] = {}
        for finding in self.findings:
            for corners in finding.screens:
                found.setdefault((finding.frame, corners), Sighting(finding.frame, corners))
        return list(found.values())

    def failing_after(self, cleaned: dict, *, accept_minor: bool = True) -> list[Finding]:
        """The failing findings a cleanup did not answer: those not shown only on screens it drew."""
        from app.services.video_studio.stray_screens import Sighting, accepted

        return [
            f
            for f in self.failing(accept_minor=accept_minor)
            if not f.screens or not accepted([Sighting(f.frame, corners) for corners in f.screens], cleaned)
        ]

    def accepted_note(self, *, accept_minor: bool = True) -> str | None:
        """The note of a shot kept with minor drift: what differs, seen only up close."""
        kept = self.accepted(accept_minor=accept_minor)
        if not kept:
            return None
        return (
            f"Minor content drift accepted (details seen only up close, not at a glance): {_listed(kept)}. "
            "accept_minor_drift: false fails a shot on such details."
        )


def _split_terms(negative_prompt: str | None) -> list[str]:
    terms = [t.strip(" .\t") for t in re.split(r"[,;\n]+", negative_prompt or "")]
    return list(dict.fromkeys(t for t in terms if t))


def excluded_terms(negative_prompt: str | None) -> list[str]:
    """The terms a negative prompt excludes, split on commas, semicolons and lines, once each."""
    return _split_terms(negative_prompt)[:MAX_EXCLUDED]


def without_terms(negative_prompt: str | None, terms: list[str]) -> str | None:
    """*negative_prompt* without *terms* (matched as :func:`excluded_terms` splits it, whatever their case): ``None`` empty."""
    dropped = {term.lower() for term in terms}
    kept = [term for term in _split_terms(negative_prompt) if term.lower() not in dropped]
    return ", ".join(kept) or None


def prompt_digest(prompt: str, negative_prompt: str | None) -> str:
    return hashlib.sha256(f"{prompt}\n--\n{negative_prompt or ''}".encode()).hexdigest()[:24]


# ── What the start picture already shows ────────────────────────────────────


def start_digest(negative_prompt: str | None, start_file_id: str | None) -> str | None:
    """The key of a start picture read against a negative prompt; ``None`` for a shot from no picture or no term."""
    terms = excluded_terms(negative_prompt)
    if not start_file_id or not terms:
        return None
    return hashlib.sha256(f"{start_file_id}\n--\n{'|'.join(t.lower() for t in terms)}".encode()).hexdigest()[:24]


def kept_start_shown(params: dict, digest: str | None) -> list[str] | None:
    """The terms kept as already shown by the start picture for *digest*; ``None`` when none was read for it."""
    kept = params.get(START_SHOWN_KEY)
    if digest is None or not isinstance(kept, dict) or kept.get("digest") != digest:
        return None
    return [str(t) for t in kept.get("terms") or [] if str(t).strip()]


def keep_start_shown(params: dict, digest: str | None, terms: list[str] | None) -> None:
    """Keep (or drop, for no reading) what the start picture shows of the negative prompt with the shot."""
    params.pop(START_SHOWN_KEY, None)
    if digest is not None and terms is not None:
        params[START_SHOWN_KEY] = {"digest": digest, "terms": list(terms)}


def start_shown_note(terms: list[str]) -> str | None:
    """What the answer says of the negative terms the start picture already shows; ``None`` for none."""
    if not terms:
        return None
    return (
        f"The start picture already shows: {', '.join(terms)} - judged from the middle frame on, a finding only where "
        "the take shows more of it than the start picture; reword the negative prompt (or start from another picture) "
        "when the shot must not show it at all."
    )


_START_INSTRUCTION = (
    "The attached image is the picture a video shot starts from. Which of these terms does it already show: {terms}? "
    "Report only what you see. Answer with one JSON object only: {{\"shown\": [\"...\"]}}, each term copied exactly."
)


def parse_start_shown(content: str, terms: list[str]) -> list[str] | None:
    """Which of *terms* a model's answer to :data:`_START_INSTRUCTION` says the picture shows; ``None`` unusable."""
    data = _json_of(content)
    if not isinstance(data, dict) or not isinstance(data.get("shown"), list):
        return None
    said = {_term_of(item).lower() for item in data["shown"]}
    return [term for term in terms if term.lower() in said]


async def read_start_picture(user_id: object, picture: tuple[bytes, str], negative_prompt: str | None) -> list[str] | None:
    """The negative prompt's terms *picture* (a shot's start picture) already shows, read by the person's chat model
    before the shot is paid for; ``[]`` for none, ``None`` when it could not be read (logged)."""
    from app.core.database import async_session
    from app.llm.base import ChatMessage, FileRef
    from app.llm.router import get_llm_for_user

    terms = excluded_terms(negative_prompt)
    if not terms:
        return []
    data, mime = picture
    try:
        async with async_session() as db:
            llm, model = await get_llm_for_user(db, str(user_id))
        response = await llm.chat(
            [
                ChatMessage(
                    role="user",
                    content=_START_INSTRUCTION.format(terms="; ".join(terms)),
                    files=[FileRef.from_bytes(data, f"start_picture.{mime.rsplit('/', 1)[-1]}", mime)],
                )
            ],
            model=model,
            temperature=0.0,
            max_tokens=400,
        )
    except Exception as exc:  # advisory: the take is then judged on every frame as before
        logger.info("Start picture not read against the negative prompt: %s", format_exception_message(exc))
        return None
    return parse_start_shown(response.content, terms)


async def start_shown(
    user_id: object, params: dict, negative_prompt: str | None, start_file_id: str | None, picture: tuple[bytes, str] | None
) -> list[str]:
    """The negative terms a shot's start picture already shows: as kept for this picture and negative prompt
    (:func:`kept_start_shown`), else read now from *picture* and kept; ``[]`` when there is none to read."""
    digest = start_digest(negative_prompt, start_file_id)
    if digest is None:
        return []
    kept = kept_start_shown(params, digest)
    if kept is not None:
        return kept
    if picture is None:
        return []
    read = await read_start_picture(user_id, picture, negative_prompt)
    keep_start_shown(params, digest, read)
    return read or []


# ── What the start picture already fails of the prompt's subjects ──────────

#: The params key of the first-frame findings the start picture itself shows of the prompt's subjects:
#: ``{"digest", "findings": [{"subject", "what"}]}`` (:func:`start_content_digest`).
START_FINDINGS_KEY = "start_content_findings"
#: The code a request is refused with when its start picture already fails the content check (issue #10895).
START_FAILS_CODE = "start_fails_content_check"


def start_content_digest(prompt: str | None, negative_prompt: str | None, start_file_id: str | None) -> str | None:
    """The key of a start picture read against a prompt's subjects; ``None`` for a shot from no picture or no prompt."""
    if not start_file_id or not (prompt or "").strip():
        return None
    keyed = f"{start_file_id}\n--\n{prompt_digest(prompt or '', negative_prompt)}"
    return hashlib.sha256(keyed.encode()).hexdigest()[:24]


def kept_start_findings(params: dict | None, digest: str | None) -> list[dict] | None:
    """The first-frame findings kept as shown by the start picture for *digest*; ``None`` when none was read for it."""
    kept = (params or {}).get(START_FINDINGS_KEY)
    if digest is None or not isinstance(kept, dict) or kept.get("digest") != digest:
        return None
    return [
        {"subject": str(f.get("subject") or ""), "what": str(f.get("what") or "")}
        for f in kept.get("findings") or []
        if isinstance(f, dict) and str(f.get("what") or "").strip()
    ]


def keep_start_findings(params: dict, digest: str | None, findings: list[dict] | None) -> None:
    """Keep (or drop, for no reading) what the start picture fails of the prompt's subjects with the shot."""
    params.pop(START_FINDINGS_KEY, None)
    if digest is not None and findings is not None:
        params[START_FINDINGS_KEY] = {"digest": digest, "findings": list(findings)}


def first_frame_failures(findings: list[Finding]) -> list[dict]:
    """The findings of a start picture read as a first frame that would fail a take's first frame too: major,
    on that frame. A subject not in the picture, or only one of several, is never one: it may come in later."""
    return [{"subject": f.subject, "what": f.what} for f in findings if f.frame == FRAMES[0] and f.severity == MAJOR]


def _shape(subject: str, what: str) -> str:
    """Which kind of subject finding *what* is - identical, another kind, replaced, deformed - whatever the model said."""
    rest = what[len(subject) :] if what.lower().startswith(subject.lower()) else what
    for marker, shape in (
        (": two or more look identical", "identical"),
        (" shows as ", "kind"),
        (" replaced by ", "replaced"),
        (" with ", "anomaly"),
    ):
        if rest.startswith(marker):
            return shape
    return rest.strip().lower()


def inherited_from_start(finding: Finding, start_findings: list[dict] | None) -> bool:
    """Whether *finding*, of a take's first frame, is one its start picture already shows (same subject, same kind
    of finding): the take opens on that picture, so it is no drift of the model."""
    if finding.frame != FRAMES[0] or finding.excluded or not start_findings:
        return False
    shape = _shape(finding.subject, finding.what)
    return any(
        str(s.get("subject") or "").lower() == finding.subject.lower()
        and _shape(str(s.get("subject") or ""), str(s.get("what") or "")) == shape
        for s in start_findings
    )


def start_fails_reason(findings: list[dict]) -> str:
    """Why a request whose start picture already fails the content check is refused, and what to do instead."""
    listed = "; ".join(str(f.get("what") or "") for f in findings[:6])
    return (
        f"{START_FAILS_CODE}: the start picture already shows what the content check fails a take's first frame on - "
        f"{listed}. Every take opens on that picture, so every take would fail the same way, whatever the prompt says. "
        "Start from another picture, give reference images of the subjects instead of a start frame, or pass "
        "content_check: false when the shot is meant to transform the scene. Nothing was queued or paid."
    )


async def read_start_content(
    user_id: object, picture: tuple[bytes, str], *, prompt: str, negative_prompt: str | None, params: dict
) -> list[dict] | None:
    """The first-frame findings *picture* (a shot's start picture) already shows of the subjects *prompt* names,
    read by the person's chat model before the shot is paid for (:func:`first_frame_failures`); ``[]`` for none or a
    prompt naming no subject, ``None`` when it could not be read (logged). The prompt's subjects are kept in *params*
    as the take's check keeps them (:func:`keep_subjects`), so that check does not read the prompt again."""
    from app.core.database import async_session
    from app.llm.router import get_llm_for_user

    digest = prompt_digest(prompt, negative_prompt)
    try:
        async with async_session() as db:
            llm, model = await get_llm_for_user(db, str(user_id))
        subjects = _kept_subjects(params, digest)
        camera, moves = kept_camera(params), kept_camera_terms(params, digest)
        if subjects is None or camera is None or moves is None:
            subjects, camera, moves = await read_prompt(llm, model, prompt, excluded_terms(negative_prompt))
            if subjects is None:
                return None
            keep_subjects(params, digest, subjects, camera, moves)
        if not subjects:
            return []
        answer = await observe(llm, model, subjects, [], {FRAMES[0]: picture})
    except Exception as exc:  # advisory: the take is then judged as before
        logger.info("Start picture not read against the prompt's subjects: %s", format_exception_message(exc))
        return None
    if not isinstance(answer, dict) or not isinstance(answer.get("frames"), dict):
        return None
    return first_frame_failures(drifted(subjects, [], answer))


def _json_of(content: str) -> Any:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1 :] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except ValueError:
        return None


def parse_subjects(content: str) -> list[Subject] | None:
    """The subjects of a model's answer to :data:`_SUBJECTS_INSTRUCTION`; ``None`` when unusable."""
    data = _json_of(content)
    if not isinstance(data, dict) or not isinstance(data.get("subjects"), list):
        return None
    subjects = [Subject.from_dict(item) for item in data["subjects"] if isinstance(item, dict) and str(item.get("name") or "").strip()]
    return grouped(list({s.name.lower(): s for s in subjects}.values()))[:MAX_SUBJECTS]


def parse_camera(content: str) -> str:
    """The camera of a model's answer to :data:`_SUBJECTS_INSTRUCTION`: one of :data:`CAMERAS`, unspecified otherwise."""
    data = _json_of(content)
    camera = str(data.get("camera") or "").strip().lower() if isinstance(data, dict) else ""
    return camera if camera in CAMERAS else CAMERA_UNSPECIFIED


def kept_camera(params: dict | None) -> str | None:
    """The camera read from a shot's prompt (:data:`CAMERAS`), ``None`` when its prompt was never read for it."""
    kept = (params or {}).get(SUBJECTS_KEY)
    camera = kept.get("camera") if isinstance(kept, dict) else None
    return camera if camera in CAMERAS else None


def parse_camera_terms(content: str, excluded: list[str]) -> list[str]:
    """Which of *excluded* the model's answer says are about the camera moving: only terms the negative prompt has."""
    data = _json_of(content)
    named = data.get("camera_terms") if isinstance(data, dict) else None
    said = {str(term).strip().lower() for term in named or [] if isinstance(term, str) and term.strip()}
    return [term for term in excluded if term.lower() in said]


def kept_camera_terms(params: dict | None, digest: str) -> list[str] | None:
    """The negative-prompt terms about the camera read from a shot's prompt; ``None`` when not read for *digest*."""
    kept = (params or {}).get(SUBJECTS_KEY)
    if not isinstance(kept, dict) or kept.get("digest") != digest or not isinstance(kept.get("camera_terms"), list):
        return None
    return [str(term) for term in kept["camera_terms"] if isinstance(term, str)]


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _term_of(item: Any) -> str:
    """The term of an ``excluded_seen`` entry, a bare string or ``{"term", "noticeable"}``; ``""`` for none."""
    text = item.get("term") if isinstance(item, dict) else item
    return text.strip() if isinstance(text, str) else ""


def _screens_of(item: Any) -> tuple[tuple[tuple[float, float], ...], ...]:
    """The screens an ``excluded_seen`` entry shows its term on, when it shows nowhere else; ``()`` otherwise.

    Only a reading asked to locate them (``locate=True``) gives them; a
    screen whose corners are no convex quad of the frame is not one, and an
    entry not saying ``elsewhere: false`` may show the term off its screens.
    """
    from app.services.video_studio.stray_screens import parse_corners

    if not isinstance(item, dict) or item.get("elsewhere") is not False or not isinstance(item.get("screens"), list):
        return ()
    screens = tuple(corners for corners in (parse_corners(raw) for raw in item["screens"]) if corners is not None)
    return screens if len(screens) == len(item["screens"]) else ()


def _keep_out_of(item: Any) -> bool:
    """Whether an ``excluded_seen`` entry shows its term only where it enters the keep-out rectangle: only an
    entry saying ``keep_out: true`` does."""
    return isinstance(item, dict) and item.get("keep_out") is True


def drifted(subjects: list[Subject], excluded: list[str], answer: Any, *, start_shown: list[str] | None = None) -> list[Finding]:
    """The findings of a model's observation of the frames (:data:`_FRAMES_INSTRUCTION`), decided here.

    A subject that shows in no frame is missing; one that shows as another
    kind, with something in its place, or deformed is drifted in that frame; a
    term the negative prompt excludes that a frame shows is a finding of its
    own. An observation about a subject or a term the check did not ask about
    is ignored; a subject out of one frame and in another left the frame, which
    is no drift.

    Missing, another kind and replaced are major; a deformity or an excluded
    term is minor only when the model said it is not noticeable at a glance
    (``noticeable: false``), major otherwise - an observation given as a bare
    string, without that judgement, included.

    A subject of several (:attr:`Subject.several`) is several distinct
    subjects of its kind, none identical: two of them looking the same in a
    frame is major, and so is one of them alone on every frame it is counted
    on; which of its variants shows is never asked, nor a finding.

    A term the start picture already shows (*start_shown*) is never a finding of the first frame, which is that
    picture: the frames reading reports it on the later frames only where it is new or grows (issue #10053).
    """
    frames = answer.get("frames") if isinstance(answer, dict) else None
    if not isinstance(frames, dict):
        return []
    asked = {s.name.lower(): s for s in subjects}
    banned = {t.lower(): t for t in excluded}
    opened_on = {t.lower() for t in start_shown or []}
    findings: dict[tuple[str, str, str], Finding] = {}
    seen_somewhere: set[str] = set()
    observed: set[str] = set()
    counted: dict[str, list[int]] = {}

    def add(
        subject: str,
        frame: str,
        what: str,
        severity: str = MAJOR,
        screens: tuple = (),
        excluded: bool = False,
        keep_out: bool = False,
    ) -> None:
        key = (subject.lower(), frame, what.lower())
        known = findings.get(key)
        if known is None:
            findings[key] = Finding(subject, frame, what, severity, screens, excluded, keep_out)
            return
        # The same term listed again in a frame, one entry per screen: shown only on screens when every
        # entry says so, on all of theirs; one entry saying otherwise leaves the finding uncleanable. Only in front
        # of the screen insert likewise: when every entry says so.
        both = known.screens + tuple(s for s in screens if s not in known.screens) if known.screens and screens else ()
        worse = MAJOR if MAJOR in (known.severity, severity) else MINOR
        findings[key] = Finding(known.subject, frame, known.what, worse, both, known.excluded, known.keep_out and keep_out)

    def observation(item: Any, text_key: str) -> tuple[str, str] | None:
        """``(what was seen, its severity)`` of an anomaly or an excluded term, written either way."""
        if isinstance(item, str):
            return (item.strip(), MAJOR) if item.strip() else None
        if isinstance(item, dict) and str(item.get(text_key) or "").strip():
            return str(item[text_key]).strip(), MINOR if _flag(item.get("noticeable")) is False else MAJOR
        return None

    for frame in FRAMES:
        report = frames.get(frame)
        if not isinstance(report, dict):
            continue
        for item in report.get("subjects") or []:
            if not isinstance(item, dict):
                continue
            subject = asked.get(str(item.get("name") or "").strip().lower())
            if subject is None:
                continue
            observed.add(subject.name.lower())
            present = _flag(item.get("present"))
            replaced_by = str(item.get("replaced_by") or "").strip()
            if replaced_by:
                add(subject.name, frame, f"{subject.name} replaced by {replaced_by[:80]}")
            if present is not True:
                continue
            seen_somewhere.add(subject.name.lower())
            if _flag(item.get("kind_matches")) is False:
                seen_as = str(item.get("seen_as") or "something else").strip()[:80]
                add(subject.name, frame, f"{subject.name} shows as {seen_as}, not {subject.kind}")
            if subject.several:
                if _flag(item.get("identical")) is True:
                    add(subject.name, frame, f"{subject.name}: two or more look identical, where each is a different one")
                count = item.get("count")
                if isinstance(count, int | float) and not isinstance(count, bool):
                    counted.setdefault(subject.name.lower(), []).append(int(count))
            for anomaly in item.get("anomalies") or []:
                if seen := observation(anomaly, "what"):
                    add(subject.name, frame, f"{subject.name} with {seen[0][:80]}", seen[1])
        for term in report.get("excluded_seen") or []:
            if (seen := observation(term, "term")) and (known := banned.get(seen[0].lower())):
                if frame == FRAMES[0] and known.lower() in opened_on:
                    # The first frame is the start picture: what it already showed is no drift of the take.
                    continue
                add(
                    known,
                    frame,
                    f"shows {known}, which the negative prompt excludes",
                    seen[1],
                    _screens_of(term),
                    excluded=True,
                    keep_out=_keep_out_of(term),
                )
    for key, subject in asked.items():
        if key in observed and key not in seen_somewhere:
            add(subject.name, "", f"{subject.name} never shows")
        elif counted.get(key) and max(counted[key]) < 2:
            add(subject.name, "", f"{subject.name}: only one shows, where several are asked")
    return list(findings.values())


def terms_seen(terms: list[str], answer: Any) -> list[str]:
    """Which of *terms* a frame of the observation (:data:`_FRAMES_INSTRUCTION`) shows, each once, in *terms*' order."""
    frames = answer.get("frames") if isinstance(answer, dict) else None
    reported = {
        _term_of(term).lower()
        for report in (frames.values() if isinstance(frames, dict) else [])
        if isinstance(report, dict)
        for term in report.get("excluded_seen") or []
    }
    return [term for term in terms if term.lower() in reported]


_SUBJECTS_INSTRUCTION = (
    "A video shot was generated from the prompt below. List the subjects it names: the living beings, people, "
    "characters and the objects the shot is about, each with its kind (for an animal its species, for a person "
    "who they are). Several subjects of one kind told apart only by a list of variants (colours, breeds, sizes) are "
    "one subject: its kind, how many when the prompt says it (count) and the variants as listed (variants). Leave "
    "out the setting, the light, the camera and the style. A prompt naming no subject (a "
    "landscape, an abstract motion) has none. Then say what the prompt asks of the camera: locked (it must not "
    "move at all), moving (it asks for a camera move), or unspecified.{excluded} Answer with one JSON object "
    'only: {{"subjects": [{{"name": "...", "kind": "...", "count": null, "variants": []}}], '
    '"camera": "locked|moving|unspecified", "camera_terms": ["..."]}}.\n\nPrompt: {prompt}'
)
_CAMERA_TERMS_INSTRUCTION = (
    " The shot must not show these terms: {terms}. In camera_terms, copy exactly those that are about the camera "
    "moving (a push-in, a pan, a shake) rather than something a single picture shows; none, an empty list."
)

_FRAMES_INSTRUCTION = (
    "The attached images are, in order, the {names} of a generated video shot. Its prompt names these subjects: "
    "{subjects}.{excluded} For each image and each subject, say whether it is there (present), whether it is of "
    "the kind named (kind_matches), what it looks like when it is not (seen_as), what stands where it stood when "
    "something else took its place (replaced_by), and its deformities (anomalies: extra or missing limbs, human "
    "hands or a human face on an animal, bodies merged or morphing, a subject turning into another). For each "
    "deformity and each excluded thing seen, say whether a viewer would notice it at a glance on a 1080p screen "
    "(noticeable: true) or only by looking closely at the still image (noticeable: false).{several}{screens} Report "
    "only what you see. Answer with one JSON object only: "
    '{{"frames": {{"{first}": {{"subjects": [{{"name": "...", "present": true, "kind_matches": true, "seen_as": "", '
    '"replaced_by": ""{group_fields}, "anomalies": [{{"what": "...", "noticeable": true}}]}}], '
    '"excluded_seen": [{{"term": "...", "noticeable": true{seen_fields}}}]}}}}}}, one key per image.'
)
#: What the frames reading also says of a subject of several: how many of it each image shows, and whether two
#: of them look the same - never which of its variants shows.
_SEVERAL_INSTRUCTION = (
    " For a subject named as several, say how many of them the image shows (count) and whether two or more of them "
    "look the same (identical); which of its listed variants shows does not matter."
)
_SEVERAL_FIELDS = ', "count": 2, "identical": false'
#: What the reading also says of each excluded thing seen, for a shot whose stray screens are cleaned.
_SCREENS_INSTRUCTION = (
    " For each excluded thing seen, list the screens of the image it shows on - the displays of monitors, "
    "laptops, tablets, phones and televisions - each by the four corners of its display area, as [x, y] shares "
    "of the image width and height from 0 to 1, in the order top left, top right, bottom right, bottom left "
    "(screens), and say whether it also shows anywhere other than on those screens (elsewhere)."
)
_SCREENS_FIELDS = ', "screens": [[[0.1, 0.2], [0.4, 0.2], [0.4, 0.5], [0.1, 0.5]]], "elsewhere": false'
#: What the reading also says of each excluded thing seen, for a shot carrying a screen insert.
_KEEP_OUT_INSTRUCTION = (
    " The shot's screen fills the rectangle {box} of each image (shares of its width and height from the top left "
    "corner). For each excluded thing seen, say whether it shows only where it enters that rectangle, in front of the "
    "screen (keep_out: true), or anywhere else in the image too (keep_out: false)."
)
_KEEP_OUT_FIELD = ', "keep_out": false'
#: What the reading is told of the negative terms the start picture already shows (the first image is that picture).
_START_SHOWN_INSTRUCTION = (
    " The shot starts from a picture that already shows: {terms}. The first image is that picture: list those terms in "
    "its excluded_seen as you see them; on each later image, list one of them only where it is new or clearly more "
    "than the first image shows (a hand already raised that waves, not a hand that stays or comes down)."
)


async def read_prompt(
    llm: Any, model: str | None, prompt: str, excluded: list[str] | None = None
) -> tuple[list[Subject] | None, str, list[str]]:
    """The subjects *prompt* names, the camera it asks for and which *excluded* terms are about the camera moving.

    Read by *llm* from the text alone; the subjects are ``None`` when the answer is unusable.
    """
    from app.llm.base import ChatMessage

    terms = list(excluded or [])
    asked = _CAMERA_TERMS_INSTRUCTION.format(terms="; ".join(terms)) if terms else ""
    response = await llm.chat(
        [ChatMessage(role="user", content=_SUBJECTS_INSTRUCTION.format(prompt=prompt[:4000], excluded=asked))],
        model=model,
        temperature=0.0,
        max_tokens=800,
    )
    return parse_subjects(response.content), parse_camera(response.content), parse_camera_terms(response.content, terms)


def _listed_subject(subject: Subject) -> str:
    """How the frames reading is told of *subject*: ``cows (cow)``, or a subject of several with its count and
    variants: ``cats (cat; several, 5 of them, each a different one: ginger, grey, white)``."""
    if not subject.several:
        return f"{subject.name} ({subject.kind})"
    count = f", {subject.count} of them" if subject.count else ""
    variants = f": {', '.join(subject.variants)}" if subject.variants else ""
    return f"{subject.name} ({subject.kind}; several{count}, each a different one{variants})"


async def observe(
    llm: Any,
    model: str | None,
    subjects: list[Subject],
    excluded: list[str],
    frames: dict[str, tuple[bytes, str]],
    *,
    locate: bool = False,
    keep_out: tuple[float, float, float, float] | None = None,
    start_shown: list[str] | None = None,
) -> Any:
    """What each frame shows of *subjects* and of the *excluded* terms, read by *llm* in one call.

    *locate* also asks, for each excluded term seen, the screens it shows on and whether it shows elsewhere;
    *keep_out* (a screen insert's rectangle, ``insert_keep_out.rectangle``) whether it shows only where it enters it;
    *start_shown* (the terms the start picture already shows) that a later frame lists one only where it is new or grows.
    """
    from app.llm.base import ChatMessage, FileRef
    from app.services.video_studio.insert_keep_out import spans

    names = list(frames)
    listed = ", ".join(_listed_subject(s) for s in subjects) or "none (report no subject)"
    banned = f" The shot must not show any of: {', '.join(excluded)} - list in excluded_seen those an image shows." if excluded else ""
    opened_on = [t for t in start_shown or [] if t.lower() in {e.lower() for e in excluded}]
    if opened_on:
        banned += _START_SHOWN_INSTRUCTION.format(terms=", ".join(opened_on))
    files = [FileRef.from_bytes(data, f"{name}.{mime.rsplit('/', 1)[-1]}", mime) for name, (data, mime) in frames.items()]
    located = locate and bool(excluded)
    fenced = keep_out is not None and bool(excluded)
    several = any(s.several for s in subjects)
    prompt = _FRAMES_INSTRUCTION.format(
        names=", ".join(names),
        subjects=listed,
        excluded=banned,
        first=names[0],
        several=_SEVERAL_INSTRUCTION if several else "",
        group_fields=_SEVERAL_FIELDS if several else "",
        screens=(_SCREENS_INSTRUCTION if located else "")
        + (_KEEP_OUT_INSTRUCTION.format(box=spans(keep_out)) if fenced else ""),
        seen_fields=(_SCREENS_FIELDS if located else "") + (_KEEP_OUT_FIELD if fenced else ""),
    )
    response = await llm.chat(
        [ChatMessage(role="user", content=prompt, files=files)], model=model, temperature=0.0, max_tokens=3000
    )
    return _json_of(response.content)


def _kept_subjects(params: dict, digest: str) -> list[Subject] | None:
    kept = params.get(SUBJECTS_KEY)
    if not isinstance(kept, dict) or kept.get("digest") != digest or not isinstance(kept.get("subjects"), list):
        return None
    # An older reading lists a plural subject's variants one by one: grouped as it is read now.
    return grouped([Subject.from_dict(s) for s in kept["subjects"] if isinstance(s, dict) and s.get("name")])


def keep_subjects(
    params: dict,
    digest: str,
    subjects: list[Subject],
    camera: str = CAMERA_UNSPECIFIED,
    camera_terms: list[str] | None = None,
) -> None:
    """Remember what was read from a prompt with the shot, for the same prompt generated again."""
    params[SUBJECTS_KEY] = {
        "digest": digest,
        "subjects": [s.as_dict() for s in subjects],
        "camera": camera,
        "camera_terms": list(camera_terms or []),
    }


async def prompt_camera(user_id: object, *, prompt: str, negative_prompt: str | None, params: dict) -> str | None:
    """The camera *prompt* asks for (:data:`CAMERAS`), read once and kept with its subjects; ``None`` when unread.

    The reading :func:`check` makes and keeps (:func:`keep_subjects`), made
    earlier when a take needs it first - whether to stabilise it - so
    the check that follows reads no prompt again.
    """
    from app.core.database import async_session
    from app.llm.router import get_llm_for_user

    if not (prompt or "").strip():
        return None
    digest = prompt_digest(prompt, negative_prompt)
    camera = kept_camera(params)
    if _kept_subjects(params, digest) is not None and camera is not None and kept_camera_terms(params, digest) is not None:
        return camera
    try:
        async with async_session() as db:
            llm, model = await get_llm_for_user(db, str(user_id))
        subjects, camera, moves = await read_prompt(llm, model, prompt, excluded_terms(negative_prompt))
    except Exception as exc:  # advisory: the take is then judged as made
        logger.info("The camera of a shot's prompt was not read: %s", format_exception_message(exc))
        return None
    if subjects is None:
        return None
    keep_subjects(params, digest, subjects, camera, moves)
    return camera


async def check(
    user_id: object,
    *,
    prompt: str,
    negative_prompt: str | None,
    video_path: str,
    duration: float,
    workdir: str,
    params: dict,
    avoided: list[str] | None = None,
    locate_screens: bool = False,
    keep_out: tuple[float, float, float, float] | None = None,
    start_shown: list[str] | None = None,
    start_findings: list[dict] | None = None,
) -> ContentCheck:
    """Read a made shot against its prompt (see the module); the subjects read are kept in *params*.

    *avoided*: what the template's art direction avoids, read with the
    rest and reported in ``avoided_seen``, never as a drift. *locate_screens*
    (a shot whose stray screens are cleaned) also has each excluded
    term located on the screens it shows on (``Finding.screens``); *keep_out*
    (the rectangle of a screen insert) has each said shown only where
    it enters that rectangle or not (``Finding.keep_out``); *start_shown*
    (:func:`start_shown`) the negative terms the start picture already shows,
    never a finding of the first frame (:func:`drifted`); *start_findings* (:func:`kept_start_findings`) the
    first-frame findings the start picture itself showed, said inherited in the reason. Read as the
    shot's person (the job bound them, ``bind_llm_caller``), with their chat
    model, resolved on a short session of its own.
    """
    from pathlib import Path

    from app.core.database import async_session
    from app.llm.router import get_llm_for_user
    from app.services.video_studio import encoder

    if not (prompt or "").strip():
        return ContentCheck(False)
    digest = prompt_digest(prompt, negative_prompt)
    excluded = excluded_terms(negative_prompt)
    try:
        async with async_session() as db:
            llm, model = await get_llm_for_user(db, str(user_id))
        subjects = _kept_subjects(params, digest)
        camera = kept_camera(params)
        moves = kept_camera_terms(params, digest)
        if subjects is None or camera is None or moves is None:
            subjects, camera, moves = await read_prompt(llm, model, prompt, excluded)
            if subjects is None:
                return ContentCheck(False, note="The shot was not checked against its prompt: the reading came back unusable.")
            keep_subjects(params, digest, subjects, camera, moves)
        # A camera move is not something a still frame shows: it is measured on the frames (``frame_qc``).
        moving = {term.lower() for term in moves}
        excluded = [term for term in excluded if term.lower() not in moving]
        known = {term.lower() for term in excluded} | moving
        # The template's own terms, past the author's: never truncated by the author's cap.
        taste = [term for term in dict.fromkeys(t.strip() for t in avoided or []) if term and term.lower() not in known]
        if not subjects and not excluded and not taste:
            # A landscape, an abstract motion, nothing excluded: nothing to keep the same, no frame is read.
            return ContentCheck(True, camera=camera, camera_terms=moves)
        frames: dict[str, tuple[bytes, str]] = {}
        for name, at in zip(FRAMES, (0.0, max(duration, 0.0) / 2, None), strict=True):
            out = Path(workdir) / f"content-check-{name}.jpg"
            await encoder.extract_still(video_path, at=at, out_path=str(out), max_side=_FRAME_MAX_SIDE)
            frames[name] = (out.read_bytes(), "image/jpeg")
        answer = await observe(
            llm,
            model,
            subjects,
            [*excluded, *taste],
            frames,
            locate=locate_screens,
            keep_out=keep_out,
            start_shown=start_shown,
        )
    except Exception as exc:  # advisory: a shot is never lost because it could not be read
        logger.info("Content check of a shot not run: %s", format_exception_message(exc))
        return ContentCheck(
            False, note=f"The shot was not checked against its prompt: {format_exception_message(exc)[:200]}"
        )
    if not isinstance(answer, dict) or not isinstance(answer.get("frames"), dict):
        return ContentCheck(
            False,
            note="The shot was not checked against its prompt: the reading came back unusable.",
            subjects=subjects,
            camera=camera,
            camera_terms=moves,
        )
    return ContentCheck(
        True,
        drifted(subjects, excluded, answer, start_shown=start_shown),
        subjects=subjects,
        camera=camera,
        avoided_seen=terms_seen(taste, answer),
        camera_terms=moves,
        start_findings=list(start_findings or []),
    )

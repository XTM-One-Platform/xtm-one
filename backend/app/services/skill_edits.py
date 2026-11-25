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

"""Partial edits and the wipe guard of a skill body.

``update_skill`` used to replace the whole body of a skill: a one-section change
resent the whole skill, and a placeholder string replaced a 27 KB skill three
times. This module gives the builder tools what ``update_knowledge_document``
has: an outline, one section read, ``replace_section``,
``insert_after``, ``append`` and exact find/replace ``edit`` modes, and a guard
refusing a ``replace`` that would wipe most of a long skill.

Sections are resolved with the parser ``load_skill(section=)`` uses
(:func:`app.services.builtin_tools.skills.skill_sections` and
:func:`~app.services.builtin_tools.skills.find_sections`), so one selector
names the same heading for a read, a load and a write. On top of it a selector
may be an outline index (``#3``) or a heading path (``Parent > Child``).

The same modes edit one text field of an agent or an assignment
(``update_agent`` persona / guardrails / description, ``update_assignment``
prompt / activation_prompt / description): :func:`partial_mode_args` checks the
arguments and :func:`partial_text_update` applies the mode and answers the
bounded unified diff, so a one-paragraph change of a 29,000-character prompt
never travels whole through the context.

The module is pure: no database, no I/O.
"""

from __future__ import annotations

import re

from app.services.builtin_tools.skills import SkillSection, find_sections, skill_sections

#: Update modes ``update_skill`` accepts; ``replace`` is the historical one.
SKILL_UPDATE_MODES = ("replace", "append", "replace_section", "insert_after", "edit")

#: Modes that address one heading section and need ``section``.
SECTION_MODES = ("replace_section", "insert_after")

#: A ``replace`` keeping less than this share of the stored body is refused
#: unless ``confirm_shrink`` is set (same ratio as the knowledge tools).
SHRINK_GUARD_RATIO = 0.5
#: ...for a stored body of at least this many characters.
SHRINK_GUARD_MIN_CHARS = 2_000
#: Over a body that long, a content under this many characters is never a
#: skill: it is refused even with ``confirm_shrink``.
WIPE_GUARD_MIN_CHARS = 50

#: A content that is only a placeholder token: ``[[PLACEHOLDER]]``,
#: ``<placeholder>``, ``{{content}}``, ``TODO``, ``...``.
_PLACEHOLDER_ONLY_RE = re.compile(
    r"^\s*(?:\[\[[^\]\n]{0,80}\]\]|\{\{[^}\n]{0,80}\}\}|<[^>\n]{0,80}>"
    r"|placeholder|todo|tbd|\.{3}|…|content|null|none)\s*$",
    re.IGNORECASE,
)
_INDEX_RE = re.compile(r"^#(\d+)$")
_PATH_SEPARATOR = ">"
_MAX_LISTED = 12


class SkillEditError(ValueError):
    """A skill edit that cannot be applied as written; nothing was changed."""


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[*_`#]", "", text).casefold().split())


def _ancestors(sections: list[SkillSection], target: SkillSection) -> list[SkillSection]:
    """The headings enclosing *target*, outermost first."""
    chain: list[SkillSection] = []
    for section in sections:
        if section.start >= target.start:
            break
        if section.level < target.level and section.end >= target.end:
            while chain and chain[-1].level >= section.level:
                chain.pop()
            chain.append(section)
    return chain


def _listed(sections: list[SkillSection], picked: list[SkillSection]) -> str:
    index = {s.start: i for i, s in enumerate(sections, start=1)}
    names = [f"#{index[s.start]} '{s.title}'" for s in picked[:_MAX_LISTED]]
    more = f" and {len(picked) - _MAX_LISTED} more" if len(picked) > _MAX_LISTED else ""
    return "; ".join(names) + more


def resolve_section(content: str, selector: object, *, noun: str = "skill") -> SkillSection:
    """The one section of *content* that *selector* names; :class:`SkillEditError` otherwise.

    *selector* is an outline index (``#3``), a heading path (``Parent > Child``)
    or what ``load_skill(section=)`` accepts (an exact title, a leading number
    such as ``16``, else a unique title fragment).
    """
    raw = str(selector if selector is not None else "").strip()
    if not raw:
        raise SkillEditError("'section' is empty: give a heading text, a heading path or an outline index such as '#3'.")
    sections = skill_sections(content)
    if not sections:
        raise SkillEditError(f"the {noun} has no Markdown heading, so it has no section to address.")

    index_match = _INDEX_RE.match(raw)
    if index_match is not None:
        index = int(index_match.group(1))
        if 1 <= index <= len(sections):
            return sections[index - 1]
        raise SkillEditError(f"section index {index} is out of range: the outline has {len(sections)} headings (#1 to #{len(sections)}).")

    if _PATH_SEPARATOR in raw:
        parts = [_norm(p) for p in raw.split(_PATH_SEPARATOR)]
        if any(not p for p in parts):
            raise SkillEditError(f"section path '{raw}' has an empty part; write it as 'Parent > Heading'.")
        wanted, parents = parts[-1], parts[:-1]
        hits = []
        for section in sections:
            if _norm(section.title) != wanted:
                continue
            chain = [_norm(a.title) for a in _ancestors(sections, section)]
            if len(chain) >= len(parents) and chain[-len(parents):] == parents:
                hits.append(section)
    else:
        hits = find_sections(sections, raw)

    if len(hits) == 1:
        return hits[0]
    if hits:
        raise SkillEditError(
            f"section '{raw}' matches {len(hits)} headings: {_listed(sections, hits)}. "
            "Name one with its outline index (e.g. '#N') or a heading path 'Parent > Heading'."
        )
    raise SkillEditError(f"no heading matches section '{raw}'. Headings: {_listed(sections, sections)}.")


def format_outline(content: str) -> str:
    """One line per heading: index, level, offset and length, indented by level."""
    sections = skill_sections(content)
    if not sections:
        return "Outline: this skill has no Markdown heading."
    lines = [
        f"Outline ({len(sections)} headings, {len(content)} characters; '#N' is the index "
        "a 'section' argument accepts, offsets are character offsets in the content):"
    ]
    for i, section in enumerate(sections, start=1):
        indent = "  " * (section.level - 1)
        lines.append(
            f"#{i} {indent}{'#' * section.level} {section.title} "
            f"[offset {section.start}, {section.end - section.start} chars]"
        )
    return "\n".join(lines)


def _require_heading_first(content: str, noun: str = "skill") -> None:
    first = next((line for line in content.splitlines() if line.strip()), "")
    if not re.match(r"^#{1,6}[ \t]+\S", first):
        raise SkillEditError(
            "'content' must be a whole section starting with its Markdown heading line "
            f"(e.g. '## Title'), so the {noun} keeps its structure. Nothing was changed."
        )


def _require_headings(previous: str, mode: str, noun: str) -> None:
    """Refuse a section mode on a text with no heading, naming the modes that apply (#2193)."""
    if not skill_sections(previous):
        raise SkillEditError(
            f"mode '{mode}' addresses a Markdown heading section, and this {noun} has no "
            "Markdown heading. Use mode 'edit' (exact find/replace, e.g. to insert text "
            "after a paragraph) or 'append'."
        )


def _terminated(content: str, followed: bool) -> str:
    if followed and content and not content.endswith("\n"):
        return f"{content}\n"
    return content


def guard_replace(previous: str, content: str, *, confirm_shrink: bool, noun: str = "skill") -> None:
    """Refuse a whole-body replace that would wipe a long skill (or the *noun* edited)."""
    if len(previous) < SHRINK_GUARD_MIN_CHARS:
        return
    stripped = content.strip()
    if len(stripped) < WIPE_GUARD_MIN_CHARS or _PLACEHOLDER_ONLY_RE.match(stripped):
        raise SkillEditError(
            f"this 'content' ({len(stripped)} characters) looks like a placeholder, not a "
            f"{noun}, and would replace the current body of {len(previous)} characters. "
            "Nothing was changed. To change one part, use mode 'replace_section' or 'edit'; "
            f"to replace the {noun}, send its whole new body."
        )
    if not confirm_shrink and len(content) < len(previous) * SHRINK_GUARD_RATIO:
        raise SkillEditError(
            f"this replace would shrink the {noun} from {len(previous)} to {len(content)} "
            f"characters (more than {int((1 - SHRINK_GUARD_RATIO) * 100)}% removed). "
            "Nothing was changed. To change one part, use mode 'replace_section' or "
            f"'edit'; if the whole {noun} really is meant to become this short, call again "
            "with confirm_shrink=true."
        )


def new_skill_content(
    mode: str,
    previous: str,
    *,
    content: str = "",
    section: object = None,
    edits: object = None,
    confirm_shrink: bool = False,
    noun: str = "skill",
) -> tuple[str, str]:
    """``(new_content, detail)`` for one update *mode* over *previous*.

    *noun* names the text edited in the refusals (``update_prompt`` passes
    ``prompt``).

    Raises :class:`SkillEditError` with a message the model can act on; the
    caller stores nothing then.
    """
    if mode not in SKILL_UPDATE_MODES:
        raise SkillEditError(f"unknown mode '{mode}': use one of {', '.join(SKILL_UPDATE_MODES)}.")
    if mode == "edit":
        from app.services.builtin_tools.media import FileEditError, apply_text_edits

        try:
            new_content, account = apply_text_edits(previous, edits)
        except FileEditError as exc:
            raise SkillEditError(str(exc)) from exc
        return new_content, " Edits applied: " + "; ".join(account) + "."
    if not content.strip():
        raise SkillEditError(f"mode '{mode}' needs a non-empty 'content'.")
    if mode == "append":
        if not previous or previous.endswith("\n\n"):
            separator = ""
        elif previous.endswith("\n"):
            separator = "\n"
        else:
            separator = "\n\n"
        return f"{previous}{separator}{content}", f" Appended {len(content)} characters at the end."
    if mode in SECTION_MODES:
        _require_headings(previous, mode, noun)
        _require_heading_first(content, noun)
        target = resolve_section(previous, section, noun=noun)
        index = skill_sections(previous).index(target) + 1
        if mode == "replace_section":
            body = _terminated(content, target.end < len(previous))
            new_content = previous[: target.start] + body + previous[target.end :]
            return new_content, (
                f" Section #{index} '{target.title}' (was characters {target.start}-{target.end}) "
                f"is now characters {target.start}-{target.start + len(body)}; every character "
                "outside it is unchanged."
            )
        at = target.end
        prefix = "\n" if at > 0 and not previous[:at].endswith("\n") else ""
        body = prefix + _terminated(content, at < len(previous))
        new_content = previous[:at] + body + previous[at:]
        return new_content, (
            f" New section inserted after section #{index} '{target.title}', at characters "
            f"{at + len(prefix)}-{at + len(body)}."
        )
    guard_replace(previous, content, confirm_shrink=confirm_shrink, noun=noun)
    return content, ""


def partial_mode_args(
    mode: object,
    field: object,
    *,
    fields: tuple[str, ...],
    whole: dict[str, object],
    content: object = "",
) -> tuple[str, str]:
    """``(mode, field)`` of a text-field update, normalised; ``mode`` ``replace`` is the whole-text path.

    *fields* are the text fields the tool edits partially, the first one the
    default; *whole* maps each of them to the value passed whole in the same
    call. Raises :class:`SkillEditError` for an unknown mode or field, a field
    also passed whole, or ``content`` without a mode that takes it.
    """
    raw_mode = mode.strip().lower() if isinstance(mode, str) and mode.strip() else "replace"
    if raw_mode not in SKILL_UPDATE_MODES:
        raise SkillEditError(f"unknown mode '{raw_mode}': use one of {', '.join(SKILL_UPDATE_MODES)}.")
    raw_field = field.strip().lower() if isinstance(field, str) and field.strip() else fields[0]
    if raw_field not in fields:
        raise SkillEditError(f"unknown field '{raw_field}': a partial mode edits one of {', '.join(fields)}.")
    has_content = isinstance(content, str) and bool(content.strip())
    if raw_mode == "replace":
        if has_content:
            raise SkillEditError(
                f"'content' is read by the modes append, replace_section and insert_after only; "
                f"to replace the whole text pass it as '{raw_field}'."
            )
        return raw_mode, raw_field
    passed = whole.get(raw_field)
    if isinstance(passed, str) and passed.strip():
        raise SkillEditError(
            f"mode '{raw_mode}' changes '{raw_field}' from its stored text: do not pass '{raw_field}' "
            "whole in the same call."
        )
    if raw_mode == "edit" and has_content:
        raise SkillEditError("mode 'edit' takes 'edits', not 'content'.")
    return raw_mode, raw_field


def partial_text_update(
    mode: str,
    previous: str | None,
    *,
    field: str,
    content: object = "",
    section: object = None,
    edits: object = None,
) -> tuple[str, str]:
    """``(new_text, read_back)`` for a partial *mode* over the stored text of *field*.

    The read-back is the detail of the change and its bounded unified diff,
    never the text. Raises :class:`SkillEditError`; the caller stores nothing then.
    """
    from app.services.sandbox_workspace.editing import unified_diff

    if mode == "replace":
        raise SkillEditError("mode 'replace' takes the whole text, not a partial update.")
    before = previous or ""
    new_text, detail = new_skill_content(
        mode,
        before,
        content=content if isinstance(content, str) else "",
        section=section,
        edits=edits,
        noun=field,
    )
    if new_text == before:
        raise SkillEditError(f"this {mode} leaves '{field}' unchanged.")
    diff = unified_diff(field, before, new_text)
    return new_text, f"{field} ({mode}):{detail}\n```diff\n{diff}```"

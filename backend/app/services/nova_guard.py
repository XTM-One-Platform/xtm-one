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

"""Nova-compatible prompt guard — keyword tier (#nova).

Parses and evaluates the **keyword** subset of the Nova ``.nov`` rule format
against a user prompt, so the platform can run active pre-LLM guardrails
without pulling Nova's heavy ML dependencies (``sentence-transformers`` /
``transformers`` / ``torch``, which the ``nova-hunting`` package requires even
for keyword-only use).

Supported today, matching the Nova grammar:

    rule <Name> {
        meta:
            description = "..."
            category    = "Jailbreak"
        keywords:
            $a = "literal substring"      // case-insensitive substring
            $b = /regex pattern/i         // regex, optional flags i m s
        condition:
            keywords.$a or keywords.$b    // and / or / not / parentheses
    }

The ``semantics:`` and ``llm:`` sections are parsed for their variables and
the range of their numbers (0 to 1, as the Nova engine requires: a similarity
threshold, and for ``llm:`` the temperature of the model's call), and
otherwise IGNORED here: any reference to ``semantics.$x`` / ``llm.$x`` in the
condition evaluates to False — so a full Nova rule stays loadable and its
keyword logic still runs, and those tiers light up with the full engine.

The evaluator is pure and total: a malformed rule raises ``NovaRuleError`` at
parse time (used to validate on write); at match time nothing raises, so one
bad rule can never break a prompt from being served.
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the matching worker imports this module: no models at runtime
    from app.models.nova_rule import NovaRule

# The full Nova engine (``nova-hunting``) is an OPTIONAL deployment extra, not
# a default dependency — it pulls sentence-transformers / transformers / torch
# (~2 GB) even for keyword-only use. The keyword tier below is always available
# and self-contained; when the package IS installed (see requirements-nova.txt),
# a later increment will delegate the ``semantics:`` and ``llm:`` tiers to it.
NOVA_HUNTING_AVAILABLE = importlib.util.find_spec("nova") is not None

# ─── Parse model ─────────────────────────────────────────────────────────────


class NovaRuleError(ValueError):
    """A ``.nov`` rule that could not be parsed."""


@dataclass(frozen=True)
class _Keyword:
    name: str  # without the leading `$`
    matcher: object  # compiled regex (re.Pattern) — literals are escaped


@dataclass(frozen=True)
class ParsedRule:
    name: str
    meta: dict[str, str]
    keywords: dict[str, _Keyword]
    condition: str  # raw condition expression (evaluated lazily)
    # Section names present but unsupported (semantics/llm) — their refs are
    # forced False so the keyword logic still decides the outcome.
    inert_vars: frozenset[str] = field(default_factory=frozenset)

    @property
    def category(self) -> str:
        return self.meta.get("category", "")


_RULE_RE = re.compile(r"rule\s+([A-Za-z_]\w*)\s*\{(.*)\}\s*$", re.DOTALL)
_PATTERN_LINE_RE = re.compile(r"^\$(\w+)\s*=\s*(.+)$")
_PATTERN_VALUE_RE = re.compile(r'^"(.+)"\s*\(\s*([^()]*?)\s*\)\s*$')

#: What the number after a ``semantics:`` / ``llm:`` pattern means. The Nova
#: engine refuses one outside 0-1, so the platform does too: a rule it accepts
#: is one the engine can load. An ``llm:`` value is passed to the model as its
#: temperature and the verdict is a yes / no.
_PATTERN_VALUE_MEANING = {
    "semantics": "the minimum similarity between the message and the pattern",
    "llm": "the temperature of the model's yes/no call, not a confidence threshold",
}
_SECTION_RE = re.compile(
    r"(meta|keywords|semantics|llm|condition)\s*:", re.IGNORECASE
)
_META_LINE_RE = re.compile(r'(\w+)\s*=\s*"((?:[^"\\]|\\.)*)"')
_KW_LITERAL_RE = re.compile(r'\$(\w+)\s*=\s*"((?:[^"\\]|\\.)*)"')
_KW_REGEX_RE = re.compile(r"\$(\w+)\s*=\s*/(.+)/([imsx]*)\s*$")
_VAR_REF_RE = re.compile(r"(keywords|semantics|llm)\.\$(\w+)")


def _split_sections(body: str) -> dict[str, str]:
    """Split a rule body into ``{section_name: raw_text}``."""
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(body))
    for i, m in enumerate(matches):
        name = m.group(1).lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[name] = body[start:end].strip()
    return sections


def _regex_flags(letters: str) -> int:
    flags = 0
    mapping = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}
    for ch in letters:
        flags |= mapping.get(ch, 0)
    return flags


def _block_end(text: str, i: int) -> int | None:
    """The index of the ``}`` that closes the block whose ``{`` is at *i*, or
    ``None`` when it never closes. Braces inside ``"..."`` strings, ``/.../``
    regexes and ``//`` / ``/* */`` comments do not count."""
    n = len(text)
    depth = 0
    in_str = False
    in_regex = False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif in_regex:
            if c == "\\":
                i += 2
                continue
            if c == "/":
                in_regex = False
        elif c == '"':
            in_str = True
        elif text.startswith("//", i):
            # A comment: an empty regex or one opening on `*` is no regex.
            newline = text.find("\n", i)
            if newline < 0:
                return None
            i = newline
            continue
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            if close < 0:
                return None
            i = close + 2
            continue
        elif c == "/":
            in_regex = True  # .nov has no division, so '/' opens a regex
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


_RULE_HEADER_RE = re.compile(r"rule\s+([A-Za-z_]\w*)\s*\{")


def _rule_blocks(text: str) -> list[tuple[str, int, int | None]]:
    """Every rule a ``.nov`` file declares, as ``(name, start, end)``: *end* is
    the index of the ``}`` closing it, ``None`` when it never closes.

    Only a ``rule X {`` header at top level declares a rule: one in a
    ``//`` / ``/* */`` comment, or inside another rule's body (a keyword
    string, a regex, a comment), declares nothing. After a block that never
    closes, nothing can be told apart from its body, so every later header is
    read as a rule of its own.
    """
    blocks: list[tuple[str, int, int | None]] = []
    n = len(text)
    i = 0
    while i < n:
        if text.startswith("//", i):
            newline = text.find("\n", i)
            i = n if newline < 0 else newline + 1
            continue
        if text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = n if close < 0 else close + 2
            continue
        header = _RULE_HEADER_RE.match(text, i)
        if header and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            end = _block_end(text, header.end() - 1)
            blocks.append((header.group(1), i, end))
            if end is None:
                for later in _RULE_HEADER_RE.finditer(text, header.end()):
                    blocks.append((later.group(1), later.start(), _block_end(text, later.end() - 1)))
                break
            i = end + 1
            continue
        i += 1
    return blocks


def split_rules(text: str) -> list[str]:
    """Split a ``.nov`` file that may hold several ``rule X { ... }`` blocks into
    individual rule texts.

    Brace-matched, but skips braces inside ``"..."`` strings, ``/.../``
    regexes and comments so a keyword like ``/[..]{3,}/`` does not end the
    block early, and a header inside a comment or another rule is no rule
    (:func:`_rule_blocks`). A block that does not close is dropped (the caller
    parses each returned block, so a malformed one is skipped there). One rule
    in, one out.
    """
    return [text[start : end + 1] for _name, start, end in _rule_blocks(text) if end is not None]


def declared_rule_names(text: str) -> set[str]:
    """The rules a ``.nov`` file declares, whether their blocks close or not."""
    return {name for name, _start, _end in _rule_blocks(text)}


# Rules arrive from a public repo and their regexes run against every user
# message in the pre-LLM path. Python's `re` has no timeout and runs in C, so a
# catastrophically-backtracking pattern would block the worker's event loop —
# a supply-chain availability risk for a platform-wide guard. We cannot make
# `re` interruptible, so we refuse the shapes that cause it, at parse time:
# a quantified group that is itself quantified ((a+)+, (a*)* , (a|b)+*, …) is
# the classic trigger, and an unbounded pattern length compounds it.
_MAX_REGEX_LEN = 1000
_NESTED_QUANTIFIER_RE = re.compile(r"\([^)]*[+*}][^)]*\)\s*[+*{]")


def _reject_pathological_regex(rule_name: str, var: str, pattern: str) -> None:
    """Raise when *pattern* has a shape known to backtrack catastrophically."""
    if len(pattern) > _MAX_REGEX_LEN:
        raise NovaRuleError(
            f"Rule `{rule_name}` keyword ${var}: regex longer than "
            f"{_MAX_REGEX_LEN} characters."
        )
    if _NESTED_QUANTIFIER_RE.search(pattern):
        raise NovaRuleError(
            f"Rule `{rule_name}` keyword ${var}: nested quantifier "
            "(e.g. `(a+)+`) can backtrack catastrophically and is refused."
        )


def _check_pattern_values(rule_name: str, tier: str, section: str) -> None:
    """Raise when a ``semantics:`` / ``llm:`` pattern's number is outside 0-1."""
    for raw in section.splitlines():
        line = _PATTERN_LINE_RE.match(raw.strip())
        value = line and _PATTERN_VALUE_RE.match(line.group(2).strip())
        if not value:
            continue  # no number: the engine's default applies
        try:
            number = float(value.group(2))
        except ValueError:
            number = -1.0
        if not 0 <= number <= 1:
            raise NovaRuleError(
                f"Rule `{rule_name}` {tier} ${line.group(1)}: `{value.group(2)}` is not a "
                f"number between 0 and 1 ({_PATTERN_VALUE_MEANING[tier]})."
            )


def parse_rule(text: str) -> ParsedRule:
    """Parse a ``.nov`` rule into a :class:`ParsedRule`, or raise."""
    if not isinstance(text, str) or not text.strip():
        raise NovaRuleError("Empty rule.")
    m = _RULE_RE.search(text.strip())
    if not m:
        raise NovaRuleError("Expected `rule <Name> { ... }`.")
    name, body = m.group(1), m.group(2)
    sections = _split_sections(body)

    if "condition" not in sections:
        raise NovaRuleError(f"Rule `{name}` has no condition section.")

    meta: dict[str, str] = {}
    for line in sections.get("meta", "").splitlines():
        lm = _META_LINE_RE.search(line)
        if lm:
            meta[lm.group(1)] = lm.group(2)

    keywords: dict[str, _Keyword] = {}
    for raw in sections.get("keywords", "").splitlines():
        line = raw.strip()
        if not line:
            continue
        rx = _KW_REGEX_RE.match(line)
        if rx:
            _reject_pathological_regex(name, rx.group(1), rx.group(2))
            try:
                compiled = re.compile(rx.group(2), _regex_flags(rx.group(3)))
            except re.error as exc:
                raise NovaRuleError(
                    f"Rule `{name}` keyword ${rx.group(1)}: bad regex — {exc}"
                ) from exc
            keywords[rx.group(1)] = _Keyword(rx.group(1), compiled)
            continue
        lit = _KW_LITERAL_RE.match(line)
        if lit:
            # Literal → case-insensitive substring match (Nova default).
            # Unescape ONLY what the .nov grammar escapes. `unicode_escape`
            # would round-trip through Latin-1 and turn any non-ASCII literal
            # into mojibake ("café" → "cafÃ©"), so a French or Japanese keyword
            # would never match.
            value = lit.group(2).replace('\\"', '"').replace("\\\\", "\\")
            keywords[lit.group(1)] = _Keyword(
                lit.group(1), re.compile(re.escape(value), re.IGNORECASE)
            )
            continue
        raise NovaRuleError(f"Rule `{name}`: unparseable keyword line `{line}`.")

    # Unsupported tiers: collect their variable names so condition refs to them
    # resolve to False rather than NameError.
    inert: set[str] = set()
    for tier in ("semantics", "llm"):
        _check_pattern_values(name, tier, sections.get(tier, ""))
        for lm in re.finditer(r"\$(\w+)\s*=", sections.get(tier, "")):
            inert.add(f"{tier}.{lm.group(1)}")

    condition = " ".join(sections["condition"].split())
    parsed = ParsedRule(
        name=name,
        meta=meta,
        keywords=keywords,
        condition=condition,
        inert_vars=frozenset(inert),
    )
    # Compile-check the condition against empty matches so a syntactically
    # broken condition is caught on write, not at match time.
    _evaluate_condition(parsed, prompt="", _probe=True)
    return parsed


def has_semantic_patterns(rule_text: str) -> bool:
    """Whether *rule_text* parses and declares a ``semantics:`` pattern, whose
    threshold only means what it says on a calibrated embedding model."""
    try:
        parsed = parse_rule(rule_text or "")
    except NovaRuleError:
        return False
    return any(var.startswith("semantics.") for var in parsed.inert_vars)


# ─── Evaluation ──────────────────────────────────────────────────────────────


def _evaluate_condition(rule: ParsedRule, prompt: str, *, _probe: bool = False) -> bool:
    """Evaluate the boolean condition over the keyword matches for *prompt*.

    Only ``and`` / ``or`` / ``not`` / parentheses and ``keywords.$x`` /
    ``semantics.$x`` / ``llm.$x`` references are allowed. The expression is
    translated to a restricted Python boolean eval over a values dict — never
    the raw prompt — so a rule can compute nothing but True/False.
    """
    names: dict[str, bool] = {}

    def _ref(match: re.Match[str]) -> str:
        tier, var = match.group(1), match.group(2)
        key = f"_{tier}_{var}"
        if tier == "keywords":
            kw = rule.keywords.get(var)
            names[key] = bool(kw and kw.matcher.search(prompt)) if not _probe else False
        else:
            # semantics/llm are inert for now → always False.
            names[key] = False
        return key

    expr = _VAR_REF_RE.sub(_ref, rule.condition)
    # Normalize operators and forbid anything but boolean grammar.
    py = re.sub(r"\bAND\b", "and", expr, flags=re.IGNORECASE)
    py = re.sub(r"\bOR\b", "or", py, flags=re.IGNORECASE)
    py = re.sub(r"\bNOT\b", "not", py, flags=re.IGNORECASE)
    if not re.fullmatch(r"[\s()a-zA-Z0-9_]*(?:and|or|not|[\s()a-zA-Z0-9_])*", py):
        raise NovaRuleError(f"Rule `{rule.name}`: unsupported condition syntax.")
    # Only our generated `_tier_var` names and boolean keywords may appear.
    allowed = set(names) | {"and", "or", "not", "True", "False"}
    for token in re.findall(r"[A-Za-z_]\w*", py):
        if token not in allowed:
            raise NovaRuleError(
                f"Rule `{rule.name}`: unknown reference `{token}` in condition."
            )
    try:
        return bool(eval(py, {"__builtins__": {}}, names))  # noqa: S307 — sandboxed
    except Exception as exc:
        if _probe:
            raise NovaRuleError(
                f"Rule `{rule.name}`: invalid condition — {exc}"
            ) from exc
        return False


@dataclass(frozen=True)
class NovaMatch:
    rule_name: str
    category: str
    action: str  # carried from the NovaRule row (block/log)
    matched_keywords: tuple[str, ...]


def evaluate(
    prompt: str, rules: list[tuple[ParsedRule, str]]
) -> list[NovaMatch]:
    """Run every parsed rule against *prompt*.

    ``rules`` is a list of ``(ParsedRule, action)`` — action comes from the
    stored :class:`~app.models.nova_rule.NovaRule` row. Returns one
    :class:`NovaMatch` per rule that fired. Never raises: a rule that errors
    mid-match is skipped, so a bad rule can't stop a prompt being served.
    """
    out: list[NovaMatch] = []
    if not prompt:
        return out
    for rule, action in rules:
        try:
            if not _evaluate_condition(rule, prompt):
                continue
            hit = tuple(
                name
                for name, kw in rule.keywords.items()
                if kw.matcher.search(prompt)
            )
            out.append(
                NovaMatch(
                    rule_name=rule.name,
                    category=rule.category,
                    action=action,
                    matched_keywords=hit,
                )
            )
        except Exception:  # noqa: S112 — one bad rule mustn't break the guard
            continue
    return out


def worst_action(matches: list[NovaMatch]) -> str | None:
    """The strongest action among matches: ``block`` wins over ``log``; None
    when nothing matched."""
    if not matches:
        return None
    return "block" if any(m.action == "block" for m in matches) else "log"


# ─── DB loading ──────────────────────────────────────────────────────────────


# Shown to the user when a `block` rule fires, before any LLM call.
NOVA_REFUSAL_MESSAGE = (
    "Sorry, I can't process that message — it was flagged by a safety rule. "
    "Please rephrase your request."
)

# The actor of a match recorded by the platform on its own: the acting user's
# account is gone, so the row is anchored on the agent's owner (``system:`` +
# this, see ``audit.log_security_event``).
NOVA_GUARD_AUDIT_ACTOR = "nova_guard"


async def evaluate_prompt(
    db, prompt: str, *, extra_rule_ids: list[str] | None = None
) -> list[NovaMatch]:
    """Load the platform rules and evaluate them against *prompt*.

    Convenience for the agent-run pre-check: one call from raw user message
    to the list of fired rules. Never raises.

    ``extra_rule_ids`` adds rules selected for THIS run only (a flow's
    ``capability.nova_rule`` nodes) on top of the always-on platform rules,
    deduplicated by id so a rule that is both platform-managed and explicitly
    wired is evaluated once.

    The rules are parsed and matched off the event loop, each within a budget
    (:mod:`app.services.nova_matching`); a rule that cannot be parsed is
    skipped with a warning, as the write path validates rules and only a row
    that predates a grammar change gets here.
    """
    import logging

    from app.services import nova_matching

    rows = await load_platform_rules(db)
    if extra_rule_ids:
        platform_ids = {row.id for row in rows}
        rows = rows + [row for row in await load_rules_by_ids(db, extra_rule_ids) if row.id not in platform_ids]
    if not rows:
        return []
    outcomes = await nova_matching.match_keyword_rules(
        prompt,
        [nova_matching.KeywordRule(f"{row.name} ({row.id})", row.rule_text, row.action) for row in rows],
    )
    matches: list[NovaMatch] = []
    for row, outcome in zip(rows, outcomes, strict=True):
        if isinstance(outcome, nova_matching.Unparseable):
            logging.getLogger(__name__).warning("Skipping unparseable Nova rule %s: %s", row.id, outcome.error)
        elif outcome is not None:
            matches.append(outcome)
    return matches


async def load_rules_by_ids(db, rule_ids: list[str]) -> list[NovaRule]:
    """The enabled NovaRules named in *rule_ids*.

    For per-run wiring (flow ``capability.nova_rule`` nodes): the caller has
    already resolved which rules the run may use (the flow catalog only offers
    the author's visible rules), so this just loads them by id and skips the
    disabled ones. Order and duplicates in *rule_ids* do not matter — a set
    drives the query.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from app.models.nova_rule import NovaRule

    ids: set[_uuid.UUID] = set()
    for raw in rule_ids:
        try:
            ids.add(raw if isinstance(raw, _uuid.UUID) else _uuid.UUID(str(raw)))
        except (ValueError, AttributeError, TypeError):
            continue
    if not ids:
        return []
    return list(
        (
            await db.execute(
                select(NovaRule).where(
                    NovaRule.id.in_(ids), NovaRule.enabled.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )


async def load_platform_rules(db) -> list[NovaRule]:
    """Every enabled company-managed NovaRule.

    Platform rules (``is_company_managed``) apply to all agents.
    """
    from sqlalchemy import select

    from app.models.nova_rule import NovaRule

    return list(
        (
            await db.execute(
                select(NovaRule).where(
                    NovaRule.enabled.is_(True),
                    NovaRule.is_company_managed.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )

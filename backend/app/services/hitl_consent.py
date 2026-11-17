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

"""Whether a human's human-in-the-loop reply approves one irreversible action.

A tool that escalates an irreversible operation (today ``merge_opencti_entities``)
derives a short action code from that exact operation and asks the human to
reply ``APPROVE <code>``. The reply is recorded verbatim in
``AssignmentRun.human_input_history`` by ``POST /assignments/runs/{id}/respond``
and read back here instead of being taken on the model's word. Consent to an
irreversible action fails closed: a reply approves the codes its approvals name
or nothing, and the human is simply asked again.

A reply approves ``code`` only when all of this holds:

* nothing precedes ``APPROVE`` / ``APPROVED`` (any case) but whitespace,
  Markdown emphasis, a heading or a list marker (``*``, ``#``, ``+``, a bullet,
  a dash followed by a space), quote marks, ``(``, ``[``, an inverted
  exclamation mark and symbols that cannot say no: a ``>`` quote, ``!``, a dash
  on the keyword, ``~``, a checkbox or any other symbol can disown or reverse
  what follows;
* the code comes right after the keyword, separated only by whitespace, ``:``,
  ``,``, ``*``, quote marks, ``(``, ``[`` or a spaced dash, and is not
  continued by a letter, digit, mark, underscore or hyphen - a kana, CJK
  ideograph or hangul letter excepted, which cannot extend an ASCII code;
* the fullwidth and CJK forms an input method types for that punctuation
  (stops, commas, a colon, brackets, the katakana middle dot as a bullet) count
  as their ASCII counterparts, and a question mark counts as punctuation
  wherever it stands: a question in the reply does not void it;
* every action code the reply names is one of the approvals it opens with, and
  this one only once: "APPROVE <A> - no, I meant <B>" approves nothing. Several
  approvals back to back, each with its own keyword and nothing between them
  but whitespace, commas, semicolons, sentence stops, spaced dashes, ``&`` or
  "and" (anything else, a word above all, could retract the first), approve
  each of their codes. An action code carries ``code``'s prefix; a bare hex
  token (a date, a ticket or a git reference) is not one, unless it is
  ``code``'s own body retyped;
* it strikes nothing through (``~``, an overlay drawn through a character) and
  carries nothing else that could say no: besides prose (letters, digits,
  marks, spaces and punctuation) it may only hold printable ASCII, currency
  signs and symbols that cannot say no (check marks, OK, a thumbs up, and the
  smiles, thanks, legal marks and contact icons of a sign-off or a signature).
  A list of refusal symbols is never complete, so any other symbol (a ballot
  box with an X, a prohibition sign, a frown, a thinking face) and any control,
  private-use or unassigned character approves nothing;
* an approval that opens inside a quote mark or backtick is followed only by
  closing marks and other quoted approvals: a quoted "APPROVE <A>" followed by
  anything else is a mention, not a use;
* it carries no bidi or display control, which can make what is shown differ
  from what is parsed. Other invisible format characters (zero-width spaces,
  joiners, soft hyphens, tag characters) are dropped first, so they can neither
  hide a longer code nor break a genuine one, and codes are also looked for with
  the invisible marks, fillers and control characters (variation selectors, the
  grapheme joiner) dropped, so these cannot hide another code or a retraction.

Across a run's history the human's latest word on a code wins: a later answered
reply naming the code without approving it withdraws an earlier approval.

Prose is deliberately not interpreted: "APPROVE <A> - absolutely not" still
approves, because the escalation asks for a reply that STARTS with the approval.
A relay recording replies from a channel that quotes the original message
(email, Teams, Slack) must strip the quote first; left in, it can only cause a
refusal, never an approval.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from app.tools.base import HITL_APPROVAL_KEYWORD

__all__ = ["hitl_history_approves", "hitl_reply_approves"]

# Spelled out because the patterns are compiled with ``re.ASCII`` (so IGNORECASE
# never folds a non-ASCII letter onto the keyword or the code), which narrows
# ``\s`` to ASCII whitespace.
_WS = r"\s\x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
_QUOTES = "`\"'\u2018-\u201f\u00ab\u00bb\u2039\u203a\u300c-\u300f\uff02\uff07"
_DASHES = "\\-\u2010-\u2015\u2212\ufe58\ufe63\uff0d"
# The fullwidth and CJK forms of the ASCII punctuation accepted around an
# approval: opening and closing brackets, stops and an exclamation mark, the
# commas and the colon that may separate the keyword from the code, then a
# semicolon.
_CJK_OPENING = "\uff08\uff3b\u3008\u300a\u3010\u3014\u3016\u3018\u301a"
_CJK_CLOSING = "\uff09\uff3d\u3009\u300b\u3011\u3015\u3017\u3019\u301b"
_CJK_STOPS = "\u3002\uff0e\uff61\uff01"
_CJK_SEPARATORS = "\u3001\uff0c\uff64\uff1a"
_CJK_PAUSES = f"{_CJK_SEPARATORS}\uff1b"
# Hiragana, katakana (halfwidth too), CJK ideographs and hangul (syllables and
# jamo, halfwidth too, but not the invisible fillers): a word of these scripts
# may follow a code with no space. Planes 2 and 3 hold only CJK ideographs; an
# unassigned code point there still refuses the reply.
_CJK_LETTERS = (
    "\u3041-\u3096\u30a1-\u30fa\u30fc\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7a3"
    "\u1100-\u115e\u1161-\u11ff\u3131-\u3163\u3165-\u318e\ua960-\ua97c\ud7b0-\ud7c6\ud7cb-\ud7fb"
    "\uf900-\ufaff\uff66-\uff9d\uffa1-\uffbe\uffc2-\uffc7\uffca-\uffcf\uffd2-\uffd7\uffda-\uffdc"
    "\U00020000-\U0003ffff"
)
# Every visible question mark and interrobang, pinned against ``unicodedata``
# by tests/test_hitl_consent.py: a question does not void an approval, so they
# read as punctuation wherever the approval takes punctuation.
_QUESTION_MARKS = (
    "?\u00bf\u037e\u055e\u061f\u1367\u1945\u203d\u2047-\u2049\u225f\u2370\u2753\u2754"
    "\u2a7b\u2a7c\u2cfa\u2cfb\u2e18\u2e2e\u2e54\u3244\ua60f\ua6f7\ufe16\ufe56\uff1f"
    "\U00011143\U0001e95f\U0001f679-\U0001f67b\U0001fbc4"
)
_ASKS_RE = re.compile(f"[{_QUESTION_MARKS}]")

_DISPLAY_CONTROLS_RE = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u206f\ufff9-\ufffb]")
_QUOTE_MARK_RE = re.compile(f"[{_QUOTES}]")
_QUOTE_RUN_RE = re.compile(f"[{_QUOTES}]+")
_CLOSING_ONLY_RE = re.compile(rf"[{_WS}*.!)\]{_QUOTES}{_QUESTION_MARKS}{_CJK_STOPS}{_CJK_CLOSING}]*", re.ASCII)
_DASH_RE = re.compile(f"[{_DASHES}]")
_HEX_RE = re.compile(r"[0-9A-Fa-f]+")
# Letters, digits, spaces, tabs, line breaks and printable ASCII but "~" (a
# strike-through): dropped in C, so the Unicode database is only read for the
# few distinct characters left. Not ``\s``: it also matches invisible controls
# (``\x1c``-``\x1f``, ``\x85``) that could split a code unseen.
_PROSE_RE = re.compile(r"[\w \t\n\r\x21-\x2f\x3a-\x40\x5b-\x60\x7b-\x7d]+")
# A list of refusal symbols is never complete, so a symbol refuses the reply
# unless it cannot say no (currency signs are allowed too).
_ALLOWED_SYMBOLS = frozenset(
    # Agreement: check marks, a ballot box with a check, OK, a thumbs up.
    "\u2611\u2705\u2713\u2714\U0001f197\U0001f44c\U0001f44d\U0001f5f8\U0001f5f9"
    # Skin tones, which only colour the emoji before them.
    "\U0001f3fb\U0001f3fc\U0001f3fd\U0001f3fe\U0001f3ff"
    # A sign-off: smiles, thanks, a wave, applause, a handshake, a heart, a star.
    "\u2728\u2764\u2b50\U0001f31f\U0001f389\U0001f44b\U0001f44f\U0001f499\U0001f49a\U0001f4aa"
    "\U0001f600\U0001f601\U0001f603\U0001f604\U0001f60a\U0001f642\U0001f64f\U0001f680\U0001f91d"
    # A signature: legal marks, a degree, an accent typed as an apostrophe,
    # arrows, contact and calendar icons.
    "\u00a9\u00ae\u00b0\u00b4\u2120\u2122\u2192\u21d2\u260e\u2706\u2709\u27a1"
    "\U0001f310\U0001f3e2\U0001f4bc\U0001f4c5\U0001f4cd\U0001f4de\U0001f4e7\U0001f4f1\U0001f517\U0001f5d3"
)
_SYMBOL_CLASS = "".join(sorted(_ALLOWED_SYMBOLS))
# Between two approvals an arrow reads as "instead of", so it opens none.
_CHAINED_SYMBOL_CLASS = "".join(sorted(_ALLOWED_SYMBOLS - set("\u2192\u21d2\u27a1")))

# Tables of the Unicode database, pinned against ``unicodedata`` by
# tests/test_hitl_consent.py so a Unicode upgrade fails there, not here.
# Every format (Cf) character: the display controls among them refuse a reply,
# the others are invisible and dropped before matching.
_FORMAT_RE = re.compile(
    "[\u00ad\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u180e\u200b-\u200f"
    "\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb\U000110bd\U000110cd"
    "\U00013430-\U0001343f\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0001"
    "\U000e0020-\U000e007f]"
)
# Every combining overlay: a stroke, slash or ring drawn through a character
# strikes it out, while the other combining marks are prose.
_OVERLAY_RE = re.compile("[\u0334-\u0338\u20d2\u20d3\u20d8-\u20da\u20e5\u20e6\u20ea\u20eb\U00010efc]")
# Every invisible mark and filler (the grapheme joiner, variation selectors,
# Khmer inherent vowels, Hangul and Khitan fillers): prose, but dropped before
# looking for codes so that it cannot hide one.
_INVISIBLE_RE = re.compile(
    "[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180d\u180f\u3164\ufe00-\ufe0f\uffa0"
    "\U00016fe4\U000e0100-\U000e01ef]"
)
# Every control but a tab or a line break: refused in an approval, and dropped
# with the marks above before looking for codes.
_CONTROLS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


# Every run is possessive (``*+``, ``++``): backtracking through a run of
# separators would keep an engine frame per character of the reply.
def _lead(symbols: str) -> str:
    one = rf"[{_WS}*#+\u00a1\u2022\u30fb(\[{_CJK_OPENING}{_QUOTES}{_QUESTION_MARKS}{symbols}\ufe0e\ufe0f]*+"
    # A list dash needs a space after it: "-APPROVE" reads as a negation.
    return rf"(?P<lead>{one}(?:-[{_WS}]++{one})?)"


def _approval(code: str) -> str:
    """The keyword and an ASCII code of ``code``'s shape (``code`` itself if it has none)."""
    shape = _code_shape(code)
    if shape is None:
        token = re.escape(code)
    else:
        prefix, body = shape
        chars = "0-9A-Fa-f" if _HEX_RE.fullmatch(body) else "0-9A-Za-z"
        token = rf"{re.escape(prefix)}-[{chars}]{{{len(body)}}}"
    return (
        rf"{re.escape(HITL_APPROVAL_KEYWORD)}D?"
        rf"(?:[{_WS}:,*(\[{_CJK_OPENING}{_CJK_SEPARATORS}{_QUOTES}{_QUESTION_MARKS}]"
        rf"|[\-\u2013\u2014](?=[{_WS}]))++"
        rf"(?P<code>{token})"
        rf"(?=[{_WS}.,;:!()\[\]}}*{_QUOTES}{_QUESTION_MARKS}{_SYMBOL_CLASS}{_CJK_STOPS}{_CJK_PAUSES}"
        rf"{_CJK_OPENING}{_CJK_CLOSING}{_CJK_LETTERS}\u2026\u2013\u2014]|\Z)"
    )


@lru_cache(maxsize=256)
def _opening_re(code: str) -> re.Pattern[str]:
    return re.compile(_lead(_SYMBOL_CLASS) + _approval(code), re.IGNORECASE | re.ASCII)


@lru_cache(maxsize=256)
def _next_approval_re(code: str) -> re.Pattern[str]:
    # What closes the previous approval, then whitespace, punctuation or "and":
    # a word between two approvals could retract the first one.
    joint = (
        rf"[)\]}}{_QUOTES}{_CJK_CLOSING}]*+"
        rf"(?:[{_WS},;.&\u2026\u3001\uff0c\uff64\uff1b{_CJK_STOPS}{_QUESTION_MARKS}]"
        rf"|[!\-\u2013\u2014](?=[{_WS}])|(?<![A-Za-z])and(?![A-Za-z]))*+"
    )
    return re.compile(joint + _lead(_CHAINED_SYMBOL_CLASS) + _approval(code), re.IGNORECASE | re.ASCII)


def _approvals(text: str, code: str) -> Iterator[re.Match[str]]:
    """The approvals ``text`` opens with, back to back, each naming a code of ``code``'s shape."""
    approval = _opening_re(code).match(text)
    following = _next_approval_re(code)
    while approval is not None:
        yield approval
        approval = following.match(text, approval.end())


@lru_cache(maxsize=256)
def _code_shape(code: str) -> tuple[str, str] | None:
    """``(prefix, body)`` of a ``<letters>-<alphanumerics>`` code such as ``M-1A2B3C4D``."""
    shape = re.fullmatch(r"([A-Za-z]+)-([0-9A-Za-z]+)", code)
    return (shape[1], shape[2]) if shape else None


def _body_re(body: str) -> str:
    """A code body of ``body``'s shape, fullwidth digits and letters included."""
    body_chars = (
        "0-9A-Fa-f\uff10-\uff19\uff21-\uff26\uff41-\uff46"
        if _HEX_RE.fullmatch(body)
        else "0-9A-Za-z\uff10-\uff19\uff21-\uff3a\uff41-\uff5a"
    )
    return rf"[{body_chars}]{{{len(body)}}}"


def _as_typed(text: str) -> str:
    """A pattern for ``text`` typed in ASCII or fullwidth characters."""
    return "".join(f"[{re.escape(ch)}{chr(ord(ch) + 0xFEE0)}]" for ch in text)


@lru_cache(maxsize=256)
def _code_mention_re(code: str) -> re.Pattern[str]:
    """Anything a human could mean as an action code of ``code``'s shape."""
    shape = _code_shape(code)
    if shape is None:
        return re.compile(
            rf"(?<![0-9A-Za-z]){re.escape(code)}(?![0-9A-Za-z])",
            re.IGNORECASE | re.ASCII,
        )
    prefix, body = shape
    body_re = _body_re(body)
    return re.compile(
        rf"(?<![A-Za-z])[A-Za-z\uff21-\uff3a\uff41-\uff5a]{{{len(prefix)}}}[{_DASHES}]?{body_re}"
        # A human retyping a code may drop its dash (above), its prefix or both
        # (below). A whole code is consumed above before its body could match.
        rf"|(?<![0-9A-Za-z]){body_re}(?![0-9A-Za-z])",
        re.IGNORECASE,
    )


@lru_cache(maxsize=256)
def _action_code_re(code: str) -> re.Pattern[str]:
    """An action code a reply names: one with ``code``'s prefix, dash or not, or
    ``code``'s own body alone. Any other bare token of that shape is not one: a
    date, a ticket number or a short git hash is indistinguishable from a code
    retyped without its prefix.
    """
    shape = _code_shape(code)
    if shape is None:
        return _code_mention_re(code)
    prefix, body = shape
    return re.compile(
        rf"(?<![A-Za-z]){_as_typed(prefix)}[{_DASHES}]?{_body_re(body)}"
        rf"|(?<![0-9A-Za-z]){_as_typed(body)}(?![0-9A-Za-z])",
        re.IGNORECASE,
    )


def _visible(text: str) -> str:
    return _FORMAT_RE.sub("", text)


def _code_views(visible_text: str) -> tuple[str, ...]:
    """The texts codes are looked for in: as shown, and without the invisible
    characters that could split one. Dropping them can also glue a code to the
    word after it, so both are read; the approval itself is matched as shown.
    """
    hidden = _CONTROLS_RE.sub("", _INVISIBLE_RE.sub("", visible_text))
    return (visible_text,) if hidden == visible_text else (visible_text, hidden)


def _refuses(text: str) -> bool:
    """True when ``text`` strikes something out or carries what could say no.

    An approval may carry prose (letters, digits, marks, whitespace, punctuation),
    printable ASCII, currency signs, question marks and ``_ALLOWED_SYMBOLS``, less
    ``~`` and combining overlays. Any other symbol and any control, private-use or
    unassigned character refuses it.
    """
    for ch in set(_PROSE_RE.sub("", text)):
        if _ASKS_RE.match(ch):
            continue
        if _OVERLAY_RE.match(ch):
            return True
        category = unicodedata.category(ch)
        if category[0] not in "LNPZ" and category not in ("Mc", "Mn", "Sc") and ch not in _ALLOWED_SYMBOLS:
            return True
    return False


def _quote_marks(text: str, start: int, stop: int) -> int:
    # Counted by runs: a list of every mark would cost memory per character.
    return sum(run.end() - run.start() for run in _QUOTE_RUN_RE.finditer(text, start, stop))


def _canonical_code(token: str) -> str:
    return _DASH_RE.sub("-", unicodedata.normalize("NFKC", token)).upper()


def _names_code(reply: str, code: str) -> bool:
    shape = _code_shape(code)
    forms = (code, shape[1], shape[0] + shape[1]) if shape else (code,)
    wanted = {_canonical_code(form) for form in forms}
    return any(
        _canonical_code(token) in wanted
        for view in _code_views(_visible(reply))
        for token in _code_mention_re(code).findall(view)
    )


def hitl_reply_approves(reply: str, action_code: str) -> bool:
    """True when ``reply`` is an unambiguous approval of ``action_code``."""
    code = (action_code or "").strip()
    if not code or not isinstance(reply, str) or _DISPLAY_CONTROLS_RE.search(reply):
        return False
    text = _visible(reply)
    wanted = code.upper()
    has_quotes = _QUOTE_MARK_RE.search(text) is not None
    approvals, approves, quoted, end, opened = 0, False, False, 0, 0
    for approval in _approvals(text, code):
        opens_quoted = False
        if has_quotes:
            lead = approval.start("lead")
            # A quote mark between two approvals that closes none of those the
            # previous one opened opens this one, glued to the code or not.
            unclosed = max(0, _quote_marks(text, approval.start(), lead) - opened)
            opened = unclosed + _quote_marks(text, lead, approval.start("code"))
            opens_quoted = unclosed > 0 or _QUOTE_MARK_RE.search(text, lead, approval.end("lead")) is not None
        # A quoted approval followed by an unquoted one is a mention, not a use.
        if quoted and not opens_quoted:
            return False
        approvals += 1
        approves = approves or approval["code"].upper() == wanted
        quoted = opens_quoted
        end = approval.end()
    if not approves or _refuses(text):
        return False
    # Each code the reply names must be one of its approvals.
    codes = _action_code_re(code)
    if any(sum(1 for _ in codes.finditer(view)) != approvals for view in _code_views(text)):
        return False
    if quoted:
        return _CLOSING_ONLY_RE.fullmatch(text, end) is not None
    return True


def hitl_history_approves(history: Any, action_code: str) -> bool:
    """True when ``history`` records a human approving ``action_code``.

    Only *answered* entries count (a dismissal is not consent), and the latest
    answered reply that names the code decides: one naming it without approving
    it withdraws an earlier approval, while a reply about another code or about
    nothing at all leaves it standing.
    """
    code = (action_code or "").strip()
    if not code or not isinstance(history, list):
        return False
    approved = False
    for entry in history:
        if not isinstance(entry, dict) or entry.get("resolution") != "answered":
            continue
        # Only text is a reply: str() of a list reads as a bracketed, quoted one.
        response = entry.get("response")
        reply = response if isinstance(response, str) else ""
        if hitl_reply_approves(reply, code):
            approved = True
        elif _names_code(reply, code):
            approved = False
    return approved

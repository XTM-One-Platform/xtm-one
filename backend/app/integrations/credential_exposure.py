# SPDX-License-Identifier: Apache-2.0

"""Exposure data is rendered as presence, never by value (#5686).

The providers that monitor leaks (stealer logs, breach dumps, compromised
payment cards, PII exposure, "stolen credentials" alarms) return the leaked
secret itself: a password, a session cookie, a token, a card number, a CVV.
A tool result is read by the model, stored in run and chat messages, traced,
and may be posted to a channel, so a provider that formats such a record
renders each secret as ``captured`` / ``not captured`` (a card number as its
BIN and last four digits) and passes every free-text value it shows through a
value-shape scrub. Truncating is not redacting: a password fits in 60
characters. The full record stays in the vendor's console.

Two rules, one module:

* :func:`is_secret_key` - a field whose NAME names a secret (``password``,
  ``cookie``, ``cvv``, ``cardNumber``, ``private_key``, ...), matched as
  whole snake / camel / kebab-case parts, so ``sessionCookie`` and
  ``card_number`` match and ``passport`` or ``spinner`` do not. Such a field
  is rendered by :func:`render_exposed_value` as presence (or a masked card).
* :func:`scrub_exposure_text` - a free-text VALUE (a preview, an alarm text,
  a leaked post): an ``account:password`` (or ``email|password``) pair keeps
  the account and masks the secret, a URL's ``user:password@`` userinfo is
  masked, a ``password=...`` assignment is masked, a 13-19 digit Luhn-valid
  number is reduced to BIN + last four, and the token shapes of
  :func:`app.core.redaction.redact_secrets` follow. A secret never ends at
  punctuation it may hold: unquoted, it runs to the end of its
  whitespace-delimited token; a ``Cookie`` / ``Set-Cookie`` /
  ``Authorization`` header value runs to the end of its line; a quoted one
  to its closing quote (escaped quotes skipped).

Field names and value shapes only: nothing here knows a vendor. A provider
that formats exposure data still declares the fields it renders by value
(an allow-list), so a field it does not know is never shown by value.
"""

from __future__ import annotations

import itertools
import re
from typing import Any

from app.core.redaction import redact_secrets

CAPTURED = "captured"
NOT_CAPTURED = "not captured"
#: What a masked secret reads as inside free text.
REDACTED = "[redacted]"

#: Name parts that make a field a secret on their own.
_SECRET_KEY_PARTS = frozenset(
    {
        "password",
        "passwords",
        "passwd",
        "pass",
        "passphrase",
        "pwd",
        "secret",
        "secrets",
        "token",
        "tokens",
        "cookie",
        "cookies",
        "session",
        "sessions",
        "sessionid",
        "sessid",
        "phpsessid",
        "jsessionid",
        "sid",
        "jwt",
        "authorization",
        "credential",
        "credentials",
        "cvv",
        "cvv2",
        "cvc",
        "cvc2",
        "ccv",
        "pin",
        "pan",
        "ssn",
        "otp",
        "totp",
        "mnemonic",
        "apikey",
        "privatekey",
        "cardnumber",
        "ccnumber",
        "ccnum",
    }
)

#: Two adjacent name parts that together make a field a secret.
_SECRET_KEY_PAIRS = frozenset(
    {
        ("private", "key"),
        ("api", "key"),
        ("access", "key"),
        ("auth", "key"),
        ("security", "code"),
        ("seed", "phrase"),
        ("recovery", "phrase"),
        ("credit", "card"),
        ("card", "number"),
        ("card", "no"),
        ("card", "num"),
        ("cc", "number"),
        ("cc", "no"),
        ("cc", "num"),
    }
)

#: The secret fields whose value is a payment card number (rendered as BIN +
#: last four rather than presence).
_CARD_KEY_PARTS = frozenset({"pan", "cardnumber", "ccnumber", "ccnum"})
_CARD_KEY_PAIRS = frozenset(
    {
        ("credit", "card"),
        ("card", "number"),
        ("card", "no"),
        ("card", "num"),
        ("cc", "number"),
        ("cc", "no"),
        ("cc", "num"),
    }
)

_CAMEL_LOWER_UPPER = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_KEY_PART_SEPARATOR = re.compile(r"[^A-Za-z0-9]+")

#: Free text is cut to this length before the scrub, so a hostile value cannot
#: make the shape heuristics run long; every caller shows far less, and what
#: the cut drops is never shown.
MAX_SCRUB_CHARS = 4_000

#: ``scheme://user:password@host`` - the userinfo password, up to the last
#: ``@`` of the authority (a leaked password is not percent-encoded).
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^\s/@:\"'<>]+):([^\s/\"'<>]+)@")

#: ``key: value`` / ``key=value`` / ``"key": "value"``; the key decides. Only
#: the key and its separator are consumed, so a key that names no secret costs
#: nothing and a secret assignment nested in its value is still found; the
#: value of a secret key is read from where the match ends. The quotes may be
#: escaped (JSON embedded in a JSON string).
_KEY_SEPARATOR_MATCH = re.compile(r"(?<![\w.\-])(?P<key>[A-Za-z][\w.\-]{0,39})\\?[\"']?\s*[:=]\s*")
#: A quoted value, an escaped quote inside it included, or a value between
#: escaped quotes; the closing quote sits on the same line.
_QUOTED_VALUE = re.compile(
    r"\"(?P<dq>(?:\\.|[^\"\\\n])*)\"|'(?P<sq>(?:\\.|[^'\\\n])*)'|\\(?P<eq>[\"'])(?P<ev>.*?)\\(?P=eq)"
)
#: A secret never ends at punctuation it may contain (``Pa$$;w0rd``,
#: ``Ab&12cd``): an unquoted secret runs to the end of its whitespace-delimited
#: token, and a header that carries a list of secrets (a ``Cookie`` header is
#: ``a=1; b=2``, an ``Authorization`` header ``Bearer x``) to the end of its line.
_TOKEN = re.compile(r"\S*")
_LINE = re.compile(r"[^\r\n]*")
_LINE_VALUE_KEY_PARTS = frozenset({"cookie", "cookies", "authorization"})

#: ``account:secret`` (or ``email|secret``, the combo-list shape) where the
#: account is a user name or an email address; the secret is read from where
#: the match ends, to the end of its token.
_ACCOUNT_SEPARATOR = re.compile(r"(?<![\w.+%@\-])(?P<account>[\w.+%\-]+(?:@[\w.\-]+)?)(?P<sep>[:|])(?=\S)(?!//)")
#: A port after a dotted host (``acme.example:8443/path``, ``...:443,``).
_PORT = re.compile(r"\d{1,5}(?![\w@:\-]|\.\w)")

#: 13-19 digits, optionally grouped by spaces or dashes.
_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")

_HEX_GROUP = re.compile(r"[0-9A-Fa-f]{1,4}")


def _key_parts(key: Any) -> tuple[str, ...]:
    text = _CAMEL_LOWER_UPPER.sub(r"\1_\2", _CAMEL_ACRONYM.sub(r"\1_\2", str(key)))
    return tuple(part for part in _KEY_PART_SEPARATOR.split(text.lower()) if part)


def _pairs(parts: tuple[str, ...]) -> set[tuple[str, str]]:
    return set(itertools.pairwise(parts))


def is_secret_key(key: Any) -> bool:
    """Whether a field named *key* holds a secret (matched on whole name parts)."""
    parts = _key_parts(key)
    return bool(_SECRET_KEY_PARTS.intersection(parts) or _SECRET_KEY_PAIRS & _pairs(parts))


def is_card_number_key(key: Any) -> bool:
    """Whether a field named *key* holds a payment card number."""
    parts = _key_parts(key)
    return bool(_CARD_KEY_PARTS.intersection(parts) or _CARD_KEY_PAIRS & _pairs(parts))


def presence(value: Any) -> str:
    """``captured`` when *value* holds something, else ``not captured``.

    A boolean (or a ``"true"`` / ``"false"`` string) is the vendor saying
    whether the secret was seen, and is read as such.
    """
    if isinstance(value, bool):
        captured = value
    elif value is None:
        captured = False
    elif isinstance(value, (int, float)):
        captured = value != 0
    elif isinstance(value, str):
        captured = value.strip().lower() not in ("", "false", "0", "none", "null", "n/a", "-")
    else:
        try:
            captured = len(value) > 0
        except TypeError:
            captured = True
    return CAPTURED if captured else NOT_CAPTURED


def luhn_valid(digits: str) -> bool:
    """Luhn checksum of a string of digits."""
    if not digits.isdigit():
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char)
        if index % 2:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def mask_card_number(value: Any) -> str:
    """A card number as BIN (first six) + last four, the rest masked.

    A value with too few digits to be a card number reads as presence.
    """
    digits = re.sub(r"\D", "", str(value if value is not None else ""))
    if len(digits) < 10:
        return presence(value)
    return f"{digits[:6]}{'*' * max(len(digits) - 10, 2)}{digits[-4:]}"


def _is_masked(value: str) -> bool:
    # Masked by an earlier pass: ``password=[redacted]`` read again, or the
    # ``bob:[redacted]@host`` an URL userinfo leaves.
    return value == REDACTED or value.startswith(REDACTED + "@")


def _mask_url_userinfo(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}:{REDACTED}@"


def _secret_value_span(text: str, pos: int, key: str) -> tuple[int, int]:
    """Where the value of the secret *key* starting at *pos* lies in *text*."""
    quoted = _QUOTED_VALUE.match(text, pos)
    if quoted:
        group = next(name for name in ("dq", "sq", "ev") if quoted.group(name) is not None)
        return quoted.span(group)
    if text.startswith(('"', "'"), pos):
        # An opening quote closed on no line: the rest of the line.
        return pos + 1, _LINE.match(text, pos + 1).end()
    if text.startswith("//", pos):
        # ``session://host`` is a URL scheme, not an assignment.
        return pos, pos
    reader = _LINE if _LINE_VALUE_KEY_PARTS.intersection(_key_parts(key)) else _TOKEN
    return pos, reader.match(text, pos).end()


def _mask_key_values(text: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in _KEY_SEPARATOR_MATCH.finditer(text):
        key = match.group("key")
        if match.start() < cursor or not is_secret_key(key):
            continue
        start, end = _secret_value_span(text, match.end(), key)
        if start == end or _is_masked(text[start:end]):
            continue
        pieces.append(text[cursor:start])
        pieces.append(mask_card_number(text[start:end]) if is_card_number_key(key) else REDACTED)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 19 and luhn_valid(digits):
        return mask_card_number(digits)
    return match.group(0)


def _is_account_secret(text: str, match: re.Match[str]) -> bool:
    account = match.group("account")
    if not any(c.isalpha() for c in account):
        # A time / number pair (``10:30``).
        return False
    if match.group("sep") == "|":
        # ``a|b`` is a combo-list pair only after an email address.
        return "@" in account
    if "@" not in account and "." in account and _PORT.match(text, match.end()):
        # ``host.example:8443`` - a port, not a password.
        return False
    # A group of an IPv6 address (``2001:db8::1``) is not an account.
    return not _HEX_GROUP.fullmatch(account)


def _mask_account_secrets(text: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in _ACCOUNT_SEPARATOR.finditer(text):
        if match.start() < cursor or not _is_account_secret(text, match):
            continue
        start = match.end()
        end = _TOKEN.match(text, start).end()
        if _is_masked(text[start:end]):
            continue
        pieces.append(text[cursor:start])
        pieces.append(REDACTED)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def scrub_exposure_text(value: Any) -> str:
    """Mask the secret-shaped parts of a free-text *value*, keep the rest.

    The result covers the first :data:`MAX_SCRUB_CHARS` characters only: a
    caller truncates it for display, never the other way round (a cut made
    before the scrub can split a secret out of the shape that masks it).
    """
    if value is None:
        return ""
    text = str(value)[:MAX_SCRUB_CHARS]
    text = _URL_USERINFO.sub(_mask_url_userinfo, text)
    text = _mask_key_values(text)
    text = _CARD_CANDIDATE.sub(_mask_card, text)
    text = _mask_account_secrets(text)
    return redact_secrets(text)


def render_exposed_value(key: Any, value: Any) -> str:
    """One field of an exposure record as text that never carries its secret.

    A secret-named field reads as presence (a card number as BIN + last
    four); any other value is scrubbed with :func:`scrub_exposure_text`.
    """
    if is_secret_key(key):
        return mask_card_number(value) if is_card_number_key(key) else presence(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(scrub_exposure_text(item) for item in value if isinstance(item, (str, int, float)))
    return scrub_exposure_text(value)

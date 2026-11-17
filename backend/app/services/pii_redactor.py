# SPDX-License-Identifier: Apache-2.0

"""PII detection and redaction layer.

Provides regex-based detection and redaction of personally identifiable
information (PII) and secrets from text before it is written to external
systems (e.g. GitHub issues, Jira tickets, incident reports).

Supported entity types
----------------------
EMAIL        – email addresses
IP_ADDRESS   – IPv4 and IPv6 addresses
PHONE        – US-format phone numbers with separators (e.g. ``415-867-5309``,
               ``(415) 867-5309``, ``+1 415.867.5309``); requires at least one
               separator to keep false positives down. Bare-digit E.164
               (``+14155559876``) is intentionally NOT matched.
CREDIT_CARD  – 13–19-digit card numbers (Visa, Mastercard, Amex, Discover, JCB)
SSN          – US Social Security Numbers (NNN-NN-NNNN)
JWT_TOKEN    – JSON Web Tokens (three base64url segments separated by dots)
AWS_KEY      – AWS access key IDs (AKIA/ASIA/AROA prefix + 16 uppercase chars)
API_KEY      – HTTP Bearer / Token / ``X-API-Key`` / ``X-Auth-Token`` /
               ``X-Access-Token`` header values. Key/value assignments
               (``api_key=...``) are handled by the PASSWORD rule, not here.
PRIVATE_KEY  – PEM-encoded private key blocks
PASSWORD     – Password/secret/``api_key``/``api_token`` assignments
               (``password=…``, ``secret: '…'``, ``api_key=…``, etc.)

Usage::

    from app.services.pii_redactor import redact_pii, RedactionConfig

    result = redact_pii("User john@example.com logged in from 10.0.0.1")
    print(result.redacted_text)
    # "User [EMAIL] logged in from [IP_ADDRESS]"
    print(result.summary)
    # {"EMAIL": 1, "IP_ADDRESS": 1}

Configuration::

    # Disable phone-number scanning (too many false positives for your domain)
    config = RedactionConfig(enabled_types=RedactionConfig.DEFAULT_ENABLED_TYPES - {"PHONE"})
    result = redact_pii(text, config=config)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar

# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------

PLACEHOLDERS: dict[str, str] = {
    "EMAIL": "[EMAIL]",
    "IP_ADDRESS": "[IP_ADDRESS]",
    "PHONE": "[PHONE]",
    "CREDIT_CARD": "[CREDIT_CARD]",
    "SSN": "[SSN]",
    "JWT_TOKEN": "[JWT_TOKEN]",
    "AWS_KEY": "[AWS_KEY]",
    "API_KEY": "[API_KEY]",
    "PRIVATE_KEY": "[PRIVATE_KEY]",
    "PASSWORD": "[PASSWORD]",
}

#: Frozenset of all entity type names supported by this module.
ALL_ENTITY_TYPES: frozenset[str] = frozenset(PLACEHOLDERS)

#: Frozenset of every placeholder *string* this module ever emits.  Used by
#: the partial-replacement (``replace_group``) path to skip matches whose
#: captured value is already one of our placeholders — protects more
#: specific rules that ran earlier (PRIVATE_KEY in particular) from being
#: overwritten by a broader rule (PASSWORD) that would otherwise match the
#: replacement text on a second pass.  Without this guard,
#: ``private_key=<PEM block>`` would be redacted as ``[PRIVATE_KEY]`` by
#: the PRIVATE_KEY rule, then re-matched by PASSWORD on
#: ``private_key=[PRIVATE_KEY]`` and overwritten as
#: ``private_key=[PASSWORD]`` — losing the specific classification and
#: inflating the event count.
_PLACEHOLDER_VALUES: frozenset[str] = frozenset(PLACEHOLDERS.values())


# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rule:
    entity_type: str
    pattern: re.Pattern  # type: ignore[type-arg]
    # When set, only this 1-indexed capture group is replaced; surrounding
    # context (e.g. the key name in ``password=<secret>``) is kept.
    replace_group: int | None = None


# Rules are evaluated in order – more specific (lower false-positive risk)
# patterns come first so that higher-specificity entities are redacted before
# the broader rules have a chance to match them.
_RULES: list[_Rule] = [
    # ── PEM-encoded private key blocks (multi-line) ────────────────────────
    _Rule(
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----"
            r".*?"
            r"-----END\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----",
            re.DOTALL | re.IGNORECASE,
        ),
    ),
    # ── JSON Web Tokens (three base64url segments) ─────────────────────────
    _Rule(
        "JWT_TOKEN",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+\b"
        ),
    ),
    # ── AWS access key IDs ─────────────────────────────────────────────────
    _Rule(
        "AWS_KEY",
        re.compile(
            r"\b(?:AKIA|ASIA|AROA|AIDA|AIPA|ANPA|ANVA|APKA)[0-9A-Z]{16}\b"
        ),
    ),
    # ── US Social Security Numbers ─────────────────────────────────────────
    _Rule(
        "SSN",
        re.compile(
            # Exclude historically unassigned area numbers
            r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"
        ),
    ),
    # ── Credit / debit card numbers (major card brands) ───────────────────
    _Rule(
        "CREDIT_CARD",
        re.compile(
            r"\b(?:"
            r"4[0-9]{12}(?:[0-9]{3,6})?"  # Visa 13-16-19 digit
            r"|5[1-5][0-9]{14}"  # Mastercard (old BIN range)
            r"|2(?:2[2-9][1-9]|[3-6]\d{2}|7(?:[01]\d|2[0-1]))\d{12}"  # MC new
            r"|3[47][0-9]{13}"  # Amex
            r"|3(?:0[0-5]|[68][0-9])[0-9]{11}"  # Diners Club
            r"|6(?:011|5[0-9]{2})[0-9]{12}"  # Discover
            r"|(?:2131|1800|35\d{3})\d{11}"  # JCB
            r")\b"
        ),
    ),
    # ── Email addresses ────────────────────────────────────────────────────
    _Rule(
        "EMAIL",
        re.compile(
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
            re.IGNORECASE,
        ),
    ),
    # ── IPv4 addresses ─────────────────────────────────────────────────────
    _Rule(
        "IP_ADDRESS",
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
        ),
    ),
    # ── IPv6 addresses (full and compressed forms) ─────────────────────────
    _Rule(
        "IP_ADDRESS",
        re.compile(
            r"(?<![:\w])"
            r"(?:"
            r"(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"
            r"|(?:[0-9a-fA-F]{1,4}:){1,7}:"
            r"|:(?::[0-9a-fA-F]{1,4}){1,7}"
            r"|(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}"
            r"|(?:[0-9a-fA-F]{1,4}:){1,5}(?::[0-9a-fA-F]{1,4}){1,2}"
            r"|(?:[0-9a-fA-F]{1,4}:){1,4}(?::[0-9a-fA-F]{1,4}){1,3}"
            r"|(?:[0-9a-fA-F]{1,4}:){1,3}(?::[0-9a-fA-F]{1,4}){1,4}"
            r"|(?:[0-9a-fA-F]{1,4}:){1,2}(?::[0-9a-fA-F]{1,4}){1,5}"
            r"|[0-9a-fA-F]{1,4}:(?::[0-9a-fA-F]{1,4}){1,6}"
            r"|::(?:[0-9a-fA-F]{1,4}:){0,5}[0-9a-fA-F]{1,4}"
            r"|fe80:(?::[0-9a-fA-F]{0,4}){0,4}%[0-9a-zA-Z]{1,}"
            r")"
            r"(?![:\w])"
        ),
    ),
    # ── Password / secret assignments ──────────────────────────────────────
    # Matches: password=mysecret  secret: "abc123"  api_key='xyz'
    # Only the *value* (capture group 2) is replaced with [PASSWORD]; any
    # surrounding quotes are preserved by capturing the opening quote
    # (group 1, possibly empty) and matching the closing quote via a
    # backreference to that same group. The value class explicitly
    # excludes whitespace and quotes so the greedy ``\S+`` no longer eats
    # the trailing quote (which would produce ``secret: '[PASSWORD]``
    # with an unbalanced opening quote).
    _Rule(
        "PASSWORD",
        re.compile(
            r"(?:password|passwd|pwd|secret|api[-_]?(?:key|token)|"
            r"auth[-_]?token|access[-_]?token|private[-_]?key|passphrase)"
            r"\s*[:=]\s*(['\"]?)([^\s'\"]{4,})\1",
            re.IGNORECASE,
        ),
        replace_group=2,
    ),
    # ── HTTP Bearer / Token header values ──────────────────────────────────
    _Rule(
        "API_KEY",
        re.compile(
            r"(?:Bearer\s+|Token\s+)([A-Za-z0-9._\-+/=]{20,})",
            re.IGNORECASE,
        ),
        replace_group=1,
    ),
    # ── X-API-Key / X-Auth-Token header values ─────────────────────────────
    _Rule(
        "API_KEY",
        re.compile(
            r"(?:X-API-Key|X-Auth-Token|X-Access-Token)\s*:\s*([A-Za-z0-9._\-+/=]{20,})",
            re.IGNORECASE,
        ),
        replace_group=1,
    ),
    # ── US phone numbers (require at least one separator to reduce FP) ─────
    _Rule(
        "PHONE",
        re.compile(
            r"\b(?:\+?1[-.\s])?\(?[2-9]\d{2}\)?[-.\s]\d{3}[-.\s]\d{4}\b"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class RedactionConfig:
    """Configuration for PII redaction behaviour.

    Parameters
    ----------
    enabled_types:
        Set of entity types to scan and redact.  Defaults to all supported
        types (see :data:`ALL_ENTITY_TYPES`).  Pass a subset to disable
        scanning for specific categories, e.g.::

            RedactionConfig(enabled_types=RedactionConfig.DEFAULT_ENABLED_TYPES - {"PHONE"})
    """

    # ``ClassVar`` keeps this out of the dataclass machinery entirely so
    # the class-level ``RedactionConfig.DEFAULT_ENABLED_TYPES`` expression
    # documented above is unambiguously a ``frozenset`` (the previous
    # ``field(default=...)`` form worked at runtime because dataclasses
    # materialise immutable ``default=`` values at the class attribute,
    # but the intent reads more clearly as an explicit class constant).
    DEFAULT_ENABLED_TYPES: ClassVar[frozenset[str]] = ALL_ENTITY_TYPES

    enabled_types: frozenset[str] = field(default_factory=lambda: ALL_ENTITY_TYPES)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class RedactionEvent:
    """A single redaction event — entity detected and replaced.

    The actual PII value is deliberately **not** stored — only its type
    and approximate position in the text at the time of detection are
    recorded for audit purposes.
    """

    entity_type: str
    """Category of the detected PII (e.g. ``'EMAIL'``, ``'IP_ADDRESS'``)."""

    start: int
    """Start offset in the text at the time this rule was applied."""

    end: int
    """End offset (exclusive) in the text at the time this rule was applied."""


@dataclass
class RedactionResult:
    """Result of a PII redaction pass over a text string."""

    redacted_text: str
    """Input text with all detected PII replaced by typed placeholders."""

    original_length: int
    """Character length of the original (unredacted) input."""

    events: list[RedactionEvent]
    """Individual redaction events in detection order."""

    summary: dict[str, int]
    """Aggregate count of redacted entities per type, e.g. ``{"EMAIL": 2}``."""

    was_redacted: bool
    """``True`` if at least one PII entity was detected and replaced."""


# ---------------------------------------------------------------------------
# Core redaction function
# ---------------------------------------------------------------------------


def redact_pii(
    text: str,
    config: RedactionConfig | None = None,
) -> RedactionResult:
    """Scan *text* for PII and replace matches with typed placeholders.

    Parameters
    ----------
    text:
        The input string to scan (e.g. a GitHub issue title or body).
    config:
        Optional :class:`RedactionConfig`.  Defaults to scanning all
        supported entity types.

    Returns
    -------
    :class:`RedactionResult`
        Contains the redacted text, individual events (suitable for audit
        logging), and an aggregate summary.

    Notes
    -----
    Rules are applied sequentially in the order they appear in ``_RULES``.
    Positions recorded in :class:`RedactionEvent` reflect offsets *in the
    text as it existed when that specific rule was applied*, not necessarily
    in the original input — earlier substitutions may shift later offsets.
    This is intentional: the events are for audit logging only (no raw PII
    is stored), so approximate positions are sufficient.
    """
    if not text:
        return RedactionResult(
            redacted_text=text,
            original_length=len(text),
            events=[],
            summary={},
            was_redacted=False,
        )

    effective_config = config or RedactionConfig()
    events: list[RedactionEvent] = []
    result = text

    for rule in _RULES:
        if rule.entity_type not in effective_config.enabled_types:
            continue

        placeholder = PLACEHOLDERS[rule.entity_type]

        # Build a replacer closure that captures the *current* rule and
        # placeholder to avoid late-binding issues inside the loop.
        def _make_replacer(
            _rule: _Rule = rule,
            _placeholder: str = placeholder,
        ):
            def _replacer(m: re.Match) -> str:  # type: ignore[type-arg]
                # Partial-replacement rules (those with ``replace_group``)
                # must NOT overwrite a placeholder emitted by an earlier,
                # more-specific rule in the same pipeline run.  The
                # canonical case is ``private_key=<PEM block>``: the
                # PRIVATE_KEY rule replaces the PEM block with
                # ``[PRIVATE_KEY]``, then the PASSWORD rule (whose key
                # list includes ``private_key``) would otherwise match
                # ``private_key=[PRIVATE_KEY]`` and rewrite it as
                # ``private_key=[PASSWORD]`` — losing the classification
                # and inflating the event count.  Skip the substitution
                # (and the event) when the captured value is exactly one
                # of our own placeholders.
                if _rule.replace_group is not None:
                    captured = m.group(_rule.replace_group)
                    if captured in _PLACEHOLDER_VALUES:
                        return m.group(0)

                events.append(
                    RedactionEvent(
                        entity_type=_rule.entity_type,
                        start=m.start(),
                        end=m.end(),
                    )
                )
                if _rule.replace_group is not None:
                    # Partial replacement: keep the key name, replace the
                    # secret value only.
                    full = m.group(0)
                    grp_start = m.start(_rule.replace_group) - m.start()
                    grp_end = m.end(_rule.replace_group) - m.start()
                    return full[:grp_start] + _placeholder + full[grp_end:]
                return _placeholder

            return _replacer

        result = rule.pattern.sub(_make_replacer(), result)

    summary: dict[str, int] = {}
    for ev in events:
        summary[ev.entity_type] = summary.get(ev.entity_type, 0) + 1

    return RedactionResult(
        redacted_text=result,
        original_length=len(text),
        events=events,
        summary=summary,
        was_redacted=bool(events),
    )

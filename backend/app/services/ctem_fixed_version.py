# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: version arithmetic for the version a case must reach (CTEM-11, #894, #946).

Design: ``dev-docs/ctem-cases.md`` section 4.2bis. Pure functions over plain
data. The knowledge itself (which version of a product has a vulnerability,
which version remediates it) lives in OpenCTI: the case reader
(``ctem_cases_source``) reads ``Software has Vulnerability`` and ``Software
remediates Vulnerability`` and only orders the versions it found here.

- :func:`parse_version` and :func:`compare_versions` order versions with a
  generic scheme (numeric parts compare as numbers, a pre-release tag such as
  ``rc1`` sorts before the release, an epoch ``1:`` and a ``v`` prefix are
  read). Distribution backports are not visible to it.
- :func:`smallest_fix_above` picks the smallest remediating version above the
  declared one.
- :func:`version_from_course_of_action` reads the version a course of action
  names when it is a plain upgrade ("Upgrade to 9.0.98"), the fallback.
- :func:`smallest_fixing_every` folds the members of one product onto one
  target. Nothing is guessed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

#: Reason recorded on a member OpenCTI does not relate to the declared version.
NOT_AFFECTED_PREFIX = "not affected: version"
#: Named for a stack entry whose declared version has no Software node in OpenCTI.
VERSION_NOT_KNOWN = "version not known to OpenCTI"
#: Named for a member when neither a remediating Software nor a course of action names a version.
FIXED_VERSION_UNKNOWN = "fixed version unknown"

_PRE_RELEASE = {"dev": -4, "alpha": -3, "a": -3, "beta": -2, "b": -2, "pre": -1, "rc": -1, "c": -1}
_TOKEN = re.compile(r"\d+|[a-z]+")
_EPOCH = re.compile(r"^(\d+):(.+)$")
_UPGRADE_COA = re.compile(
    r"^\s*(?:upgrade|update|patch)\b(?:[\w .+-]*?)\b(?:to|>=)\s*(?:version\s+)?v?(\d+(?:[.\-_]\w+)*)\s*$",
    re.IGNORECASE,
)
_MAX_PARTS = 16


def parse_version(value: str | None) -> tuple[Any, ...] | None:
    """A sortable key for *value*, ``None`` when it holds no version.

    Each part is ``(rank, number, text)``: a number ranks 1, a pre-release tag
    ranks below 0 so ``1.0rc1`` sorts before ``1.0``, any other word ranks 2
    (a post-release letter such as OpenSSL's ``1.1.1w``). Parts are padded with
    zeros so ``1.2`` equals ``1.2.0`` and the key compares as a plain tuple.
    """
    text = (value or "").strip().lower()
    if not text or text in ("*", "-"):
        return None
    epoch = 0
    match = _EPOCH.match(text)
    if match:
        epoch, text = int(match.group(1)), match.group(2)
    if text.startswith("v") and len(text) > 1 and text[1].isdigit():
        text = text[1:]
    parts: list[tuple[int, int, str]] = []
    for token in _TOKEN.findall(text):
        if token.isdigit():
            parts.append((1, int(token), ""))
        else:
            parts.append((_PRE_RELEASE.get(token, 2), 0, token))
    if not parts or parts[0][0] != 1 or len(parts) > _MAX_PARTS:
        return None
    parts.extend([(1, 0, "")] * (_MAX_PARTS - len(parts)))
    return (epoch, tuple(parts))


def compare_versions(left: str, right: str) -> int | None:
    """-1, 0 or 1; ``None`` when either side is not a version."""
    a, b = parse_version(left), parse_version(right)
    if a is None or b is None:
        return None
    return (a > b) - (a < b)


def same_version(left: str | None, right: str | None) -> bool:
    """True when both sides are the same version (``1.2`` equals ``1.2.0``), else a plain text match."""
    cmp = compare_versions(left or "", right or "")
    if cmp is not None:
        return cmp == 0
    return bool((left or "").strip()) and (left or "").strip().lower() == (right or "").strip().lower()


def smallest_fix_above(versions: Iterable[str | None], installed: str | None) -> str | None:
    """The smallest of *versions* above *installed* (any when nothing is declared), else ``None``."""
    known = [v.strip() for v in versions if v and parse_version(v) is not None]
    if installed and parse_version(installed) is not None:
        known = [v for v in known if (compare_versions(v, installed) or 0) > 0]
    if not known:
        return None
    return min(known, key=lambda v: parse_version(v) or ())


def version_from_course_of_action(name: str | None) -> str | None:
    """The version a course of action names when it is a plain upgrade, else ``None``."""
    match = _UPGRADE_COA.match(name or "")
    if not match:
        return None
    version = match.group(1)
    return version if parse_version(version) is not None else None


def smallest_fixing_every(versions: Iterable[str]) -> str | None:
    """The smallest version that fixes every member: the greatest of their fixed versions."""
    known = [v for v in versions if parse_version(v) is not None]
    if not known:
        return None
    return max(known, key=lambda v: parse_version(v) or ())

# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: the version a case must reach (CTEM-11, #894).

Design: ``dev-docs/ctem-cases.md`` section 4.2bis. Pure functions over plain
data; the case reader (``ctem_cases_source``) feeds them.

- :func:`parse_version` and :func:`compare_versions` order versions with a
  generic scheme (numeric parts compare as numbers, a pre-release tag such as
  ``rc1`` sorts before the release, an epoch ``1:`` and a ``v`` prefix are
  read). Distribution backports are not visible to it.
- :class:`AffectedRange` is one affected range of one product: start and end
  with their inclusive flags, the fixed version (the excluded end, or a stated
  fix). :func:`ranges_from_nvd2` reads them from an NVD 2.0 CVE record
  (``configurations[].nodes[].cpeMatch[]``), the shape enrichment providers
  serve.
- :func:`opencti_ranges` is the hook for OpenCTI's affected ranges on a
  Vulnerability (CTEM-09). OpenCTI has no such attribute yet, so it answers
  nothing; it is the first source the resolver asks once it does.
- :func:`version_from_course_of_action` reads the version a course of action
  names when it is a plain upgrade ("Upgrade to 9.0.98").
- :func:`resolve` answers, for one vulnerability on one product, the fixed
  version to reach and where it came from, or that the declared version is
  outside every affected range ("not affected: version"), or nothing
  ("fixed version unknown"). Nothing is guessed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: Where a fixed version came from, in the order the resolver asks.
SOURCE_OPENCTI = "opencti_ranges"
SOURCE_COURSE_OF_ACTION = "course_of_action"
SOURCE_PROVIDER = "provider"

#: Reason recorded on a member whose declared version is outside every affected range.
NOT_AFFECTED_PREFIX = "not affected: version"

_PRE_RELEASE = {"dev": -4, "alpha": -3, "a": -3, "beta": -2, "b": -2, "pre": -1, "rc": -1, "c": -1}
_TOKEN = re.compile(r"\d+|[a-z]+")
_EPOCH = re.compile(r"^(\d+):(.+)$")
_UPGRADE_COA = re.compile(
    r"^\s*(?:upgrade|update|patch)\b(?:[\w .+-]*?)\b(?:to|>=)\s*(?:version\s+)?v?(\d+(?:[.\-_]\w+)*)\s*$",
    re.IGNORECASE,
)
_VERSION_COLUMN = 5  # cpe:2.3:part:vendor:product:version:...
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


@dataclass(frozen=True)
class AffectedRange:
    """One affected range of one product (bounds are versions, ``None`` when open)."""

    start: str | None = None
    start_inclusive: bool = True
    end: str | None = None
    end_inclusive: bool = False
    #: The version that fixes this range (the excluded end, or a stated fix).
    fixed: str | None = None
    #: The exact affected version when the range is a single version.
    exact: str | None = None

    def contains(self, version: str) -> bool | None:
        """True or False, ``None`` when *version* cannot be compared."""
        if parse_version(version) is None:
            return None
        if self.exact is not None:
            same = compare_versions(version, self.exact)
            return None if same is None else same == 0
        if self.start is not None:
            cmp = compare_versions(version, self.start)
            if cmp is None or cmp < 0 or (cmp == 0 and not self.start_inclusive):
                return False if cmp is not None else None
        if self.end is not None:
            cmp = compare_versions(version, self.end)
            if cmp is None or cmp > 0 or (cmp == 0 and not self.end_inclusive):
                return False if cmp is not None else None
        return True


def _cpe_part(cpe: str, index: int) -> str:
    parts = cpe.split(":")
    return parts[index].lower() if len(parts) > index else ""


def _same_name(left: str, right: str) -> bool:
    fold = re.compile(r"[\s_\-]+")
    return bool(left) and fold.sub("", left.lower()) == fold.sub("", right.lower())


def ranges_from_nvd2(record: dict[str, Any], *, vendor: str, product: str) -> list[AffectedRange]:
    """The vulnerable ranges of *product* in an NVD 2.0 CVE record.

    Reads ``configurations[].nodes[].cpeMatch[]`` entries marked vulnerable
    whose CPE names the product (and the vendor, when both sides name one).
    """
    out: list[AffectedRange] = []
    for config in record.get("configurations") or []:
        for node in (config or {}).get("nodes") or []:
            for match in (node or {}).get("cpeMatch") or []:
                if not isinstance(match, dict) or not match.get("vulnerable"):
                    continue
                criteria = str(match.get("criteria") or "")
                if not _same_name(_cpe_part(criteria, 4), product):
                    continue
                cpe_vendor = _cpe_part(criteria, 3)
                if vendor and cpe_vendor and cpe_vendor != "*" and not _same_name(cpe_vendor, vendor):
                    continue
                start_in, start_ex = match.get("versionStartIncluding"), match.get("versionStartExcluding")
                end_in, end_ex = match.get("versionEndIncluding"), match.get("versionEndExcluding")
                if start_in or start_ex or end_in or end_ex:
                    out.append(
                        AffectedRange(
                            start=start_in or start_ex,
                            start_inclusive=not start_ex,
                            end=end_ex or end_in,
                            end_inclusive=not end_ex,
                            fixed=end_ex or None,
                        )
                    )
                    continue
                version = _cpe_part(criteria, _VERSION_COLUMN)
                if parse_version(version) is not None:
                    out.append(AffectedRange(exact=version))
    return out


def opencti_ranges(vulnerability: dict[str, Any]) -> list[AffectedRange]:
    """OpenCTI's affected ranges on a Vulnerability node (CTEM-09).

    OpenCTI has no range attribute today; this hook answers nothing until it
    does, then reads it here and the resolver uses it before any other source.
    """
    del vulnerability
    return []


def version_from_course_of_action(name: str | None) -> str | None:
    """The version a course of action names when it is a plain upgrade, else ``None``."""
    match = _UPGRADE_COA.match(name or "")
    if not match:
        return None
    version = match.group(1)
    return version if parse_version(version) is not None else None


@dataclass(frozen=True)
class Resolution:
    """What the resolver found for one vulnerability on one product."""

    fixed_version: str | None = None
    source: str | None = None
    #: Set when the declared version is outside every affected range: the reason.
    not_affected: str | None = None


def _smallest_fix_above(ranges: Iterable[AffectedRange], installed: str | None) -> str | None:
    fixes = [r.fixed for r in ranges if r.fixed and parse_version(r.fixed) is not None]
    if installed and parse_version(installed) is not None:
        # The fixes of the ranges the installed version is in, above it.
        held = [
            r.fixed
            for r in ranges
            if r.fixed and r.contains(installed) and (compare_versions(r.fixed, installed) or 0) > 0
        ]
        fixes = held or []
    if not fixes:
        return None
    return min(fixes, key=lambda v: parse_version(v) or ())


def resolve(
    *,
    installed: str | None,
    opencti: Iterable[AffectedRange] = (),
    course_of_action_version: str | None = None,
    provider: Iterable[AffectedRange] = (),
) -> Resolution:
    """The fixed version of one vulnerability on one product, sources in order.

    OpenCTI ranges, then a course of action naming a version, then the
    connected enrichment provider's ranges. With a declared *installed*
    version, ranges that do not contain it are ignored, and a source whose
    ranges are known and contain none of them answers "not affected".
    """
    installed = (installed or "").strip() or None
    order: list[tuple[str, list[AffectedRange] | str]] = [
        (SOURCE_OPENCTI, list(opencti)),
        (SOURCE_COURSE_OF_ACTION, course_of_action_version or ""),
        (SOURCE_PROVIDER, list(provider)),
    ]
    for source, answer in order:
        if isinstance(answer, str):
            if answer:
                return Resolution(fixed_version=answer, source=source)
            continue
        if not answer:
            continue
        if installed and parse_version(installed) is not None:
            verdicts = [r.contains(installed) for r in answer]
            if all(v is False for v in verdicts):
                return Resolution(not_affected=f"{NOT_AFFECTED_PREFIX} {installed} is outside the affected ranges")
        fixed = _smallest_fix_above(answer, installed)
        if fixed:
            return Resolution(fixed_version=fixed, source=source)
    return Resolution()


def smallest_fixing_every(versions: Iterable[str]) -> str | None:
    """The smallest version that fixes every member: the greatest of their fixed versions."""
    known = [v for v in versions if parse_version(v) is not None]
    if not known:
        return None
    return max(known, key=lambda v: parse_version(v) or ())

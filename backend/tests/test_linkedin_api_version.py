"""Regression test: LinkedIn API version must be an active version.

LinkedIn sunsets API versions roughly one year after release. This test
pins that the ``LinkedIn-Version`` header used by the integration is not
the deprecated ``202402`` value (which caused HTTP 426 errors) and is at
least as recent as ``202501`` to guard against accidental roll-back.
"""

from __future__ import annotations

from app.integrations.providers.linkedin import _VERSION, _headers


def test_linkedin_version_is_not_deprecated_202402():
    """Guard against re-introducing the expired February-2024 API version."""
    assert _VERSION != "202402", (
        f"LinkedIn-Version '{_VERSION}' is the deprecated 20240201 version "
        "which returns HTTP 426. Update _VERSION to a currently active version."
    )


def test_linkedin_version_is_recent_enough():
    """The version string must be YYYYMM format and >= 202501 (Jan 2025).

    LinkedIn's versioning policy supports each version for ~1 year, so any
    version older than 202501 is almost certainly expired as of mid-2026.
    Year and month are both validated against plausible ranges so a typo
    like ``"202613"`` (invalid month) or ``"209901"`` (implausible year)
    fails the test with a precise diagnostic rather than silently passing
    the numeric ``>= 202501`` comparison.
    """
    assert _VERSION.isdigit() and len(_VERSION) == 6, (
        f"LinkedIn-Version '{_VERSION}' must be a 6-digit YYYYMM string."
    )
    year, month = int(_VERSION[:4]), int(_VERSION[4:])
    assert 1 <= month <= 12, (
        f"LinkedIn-Version '{_VERSION}' has invalid month {month:02d}."
    )
    # Sanity bound on the year: LinkedIn's versioned API launched in 2022;
    # anything outside [2024, 2099] is almost certainly a typo or
    # accidental rollback to an extreme value.
    assert 2024 <= year <= 2099, (
        f"LinkedIn-Version '{_VERSION}' has implausible year {year}."
    )
    assert int(_VERSION) >= 202501, (
        f"LinkedIn-Version '{_VERSION}' is older than 202501 and likely expired. "
        "Update _VERSION in linkedin.py to a currently active version."
    )


def test_headers_include_linkedin_version():
    """_headers() must always propagate LinkedIn-Version from _VERSION."""
    hdrs = _headers("dummy-token")
    assert "LinkedIn-Version" in hdrs
    assert hdrs["LinkedIn-Version"] == _VERSION

"""Tests for the SharePoint host allow-list regex.

``_SHAREPOINT_HOST_RE`` gates the host that becomes an OAuth scope audience
(``https://{host}/.default``) and the base of SharePoint HTTP requests when
extracting channel-meeting transcripts. It must accept only real SharePoint
Online hosts (``<tenant>.sharepoint.<known-tld>``) and reject look-alike hosts
with a multi-segment suffix (e.g. ``evil.sharepoint.com.evil.com``).
"""

from __future__ import annotations

import pytest

from app.services.integration_tools import _SHAREPOINT_HOST_RE


@pytest.mark.parametrize(
    "host",
    [
        "contoso.sharepoint.com",
        "contoso-my.sharepoint.com",
        "contoso-admin.sharepoint.com",
        "contoso.sharepoint.us",  # GCC High
        "contoso.sharepoint.de",  # legacy Germany
        "contoso.sharepoint.cn",  # 21Vianet China
    ],
)
def test_accepts_real_sharepoint_hosts(host):
    assert _SHAREPOINT_HOST_RE.match(host) is not None


@pytest.mark.parametrize(
    "host",
    [
        # The original vulnerability: a multi-segment suffix after "sharepoint".
        "evil.sharepoint.com.evil.com",
        "contoso.sharepoint.com.attacker.net",
        # Unknown / spoofed TLDs.
        "contoso.sharepoint.evil",
        "contoso.sharepoint.io",
        # No tenant label / not a SharePoint host at all.
        "sharepoint.com",
        "evilsharepoint.com",
        "contoso.notsharepoint.com",
        # Extra label before sharepoint (not a valid SPO host shape).
        "a.b.sharepoint.com",
        "",
    ],
)
def test_rejects_lookalike_and_invalid_hosts(host):
    assert _SHAREPOINT_HOST_RE.match(host) is None

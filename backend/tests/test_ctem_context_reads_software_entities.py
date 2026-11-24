# SPDX-License-Identifier: Apache-2.0

"""#1495: the posture's context state counts the Software entities as the organization's software.

A synced inventory (Intune, Defender, OpenAEV, SBOM) fills the software field
of the context with no profile row typed, so the CTEM posture no longer reads
it as missing.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.api.v1 import ctem_metrics
from app.services import organization_context as oc


def _profile(software: list[dict[str, str]]) -> oc.OrganizationProfile:
    return oc.parse_profile({"name": "Acme", "software": software})


async def test_synced_software_fills_the_context_field(monkeypatch):
    async def _software_profile(_db, _org=None):
        return SimpleNamespace(organization_id=None), _profile([{"product": "Edge", "vendor": "Microsoft"}])

    monkeypatch.setattr(oc, "software_profile", _software_profile)
    out = await ctem_metrics._with_context(None, {"stages": []})
    assert "software" not in out["context"]["missing_fields"]


async def test_no_software_at_all_is_still_missing(monkeypatch):
    async def _software_profile(_db, _org=None):
        return SimpleNamespace(organization_id=None), _profile([])

    monkeypatch.setattr(oc, "software_profile", _software_profile)
    out = await ctem_metrics._with_context(None, {"stages": []})
    assert "software" in out["context"]["missing_fields"]


async def test_no_saved_profile_reads_missing(monkeypatch):
    async def _software_profile(_db, _org=None):
        return None, _profile([])

    monkeypatch.setattr(oc, "software_profile", _software_profile)
    out = await ctem_metrics._with_context(None, {"stages": []})
    assert out["context"]["state"] == "missing"

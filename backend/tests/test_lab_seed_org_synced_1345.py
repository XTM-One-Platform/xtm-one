"""Organization rows written by platform syncs in a declared lab (#1345, dev:harness-gaps lab-no-synced-organization-rows)."""

from __future__ import annotations

import pytest

from tests.test_lab_seed_declarations import LAB, _lab_namespace, _lab_source

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")

BASE = "http://localhost:3100"
NAMES = ("LAB_ORG_PROFILE", "LAB_ORG_SYNCED_STATES", "LAB_ORG_SYNCED_ROWS", "_created", "seed_org_synced")


def _api(calls: list, existing: list | None = None, store: dict | None = None):
    store = store if store is not None else {"profile": {"name": "Lab sample organization", "sector": "Finance"}}
    counter = iter(range(1, 100))

    def api(method, path, token=None, body=None):
        calls.append((method, path, body))
        if path == "/organization-context/syncs" and method == "GET":
            return list(existing or [])
        if path == "/organization-context/syncs" and method == "POST":
            return {"id": f"sync-{next(counter)}", **body}
        if path == "/organization-context" and method == "PUT":
            store["profile"] = body["profile"]
            return {"version": 2}
        if path == "/organization-context" and method == "GET":
            return {"version": 1, "profile": store["profile"]}
        return {}

    return api


def test_saves_one_disabled_sync_per_section_and_synced_rows_with_their_sync_id():
    from app.services import organization_context as org_context
    from app.services.organization_context_import import SECTION_SOURCES

    calls: list = []
    ns = _lab_namespace(*NAMES, api=_api(calls))
    answer = ns["seed_org_synced"]("t", BASE)
    assert answer == {"id": "version 2, 4 synced rows in 2 sections", "url": f"{BASE}/settings/organization"}

    posted = [c[2] for c in calls if c[:2] == ("POST", "/organization-context/syncs")]
    assert [(p["section"], p["source"]) for p in posted] == [
        ("crown_jewels", "opencrq"),
        ("software", "openaev"),
    ]
    assert all(p["enabled"] is False and p["source"] in SECTION_SOURCES[p["section"]] for p in posted)

    (profile,) = [c[2]["profile"] for c in calls if c[0] == "PUT"]
    assert profile["name"] == "Lab sample organization"
    parsed = org_context.parse_profile(profile)
    for section, sync in (("crown_jewels", "sync-1"), ("tech_stack", "sync-2")):
        rows = getattr(parsed, section)
        synced = [r for r in rows if r.external_id]
        typed = [r for r in rows if not r.external_id]
        assert synced and typed, section
        assert {r.sync_id for r in synced} == {sync}, section
        assert {r.source for r in typed} == {"manual"} and not any(r.sync_id for r in typed), section


def test_keeps_an_existing_sync_of_the_section_and_source():
    calls: list = []
    existing = [{"id": "kept-1", "section": "crown_jewels", "source": "opencrq"}]
    ns = _lab_namespace(*NAMES, api=_api(calls, existing))
    ns["seed_org_synced"]("t", BASE)
    posted = [c[2]["section"] for c in calls if c[:2] == ("POST", "/organization-context/syncs")]
    assert posted == ["software"]
    (profile,) = [c[2]["profile"] for c in calls if c[0] == "PUT"]
    assert {r.get("sync_id") for r in profile["crown_jewels"] if r.get("external_id")} == {"kept-1"}


def test_raises_when_the_read_back_lost_the_sync_ids():
    calls: list = []
    store: dict = {"profile": {}}
    api = _api(calls, store=store)

    def lossy(method, path, token=None, body=None):
        answer = api(method, path, token, body)
        if method == "PUT":
            store["profile"] = {
                k: [{**r, "sync_id": ""} for r in v] if isinstance(v, list) else v for k, v in body["profile"].items()
            }
        return answer

    ns = _lab_namespace(*NAMES, api=lossy)
    with pytest.raises(RuntimeError, match="GET /organization-context answers crown_jewels"):
        ns["seed_org_synced"]("t", BASE)


def test_the_seed_runs_under_the_switch_after_the_profile_stub():
    seed = _lab_source("seed_entities")
    assert "if lab_switch('LAB_ORG_SYNCED', LAB_ORG_SYNCED_STATES) == 'on':" in seed
    assert "seeded['org synced rows'] = seed_org_synced(token, base)" in seed
    assert "skipped['org synced rows']" in seed
    assert seed.index("seed_org_ai_draft_stub(token, base)") < seed.index("seed_org_synced(token, base)")

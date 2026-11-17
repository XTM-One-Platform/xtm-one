"""Unit tests for the platform URL guard.

Pins the deterministic post-processing contract over final assistant
responses: detection (markdown links, raw URLs, relative paths),
route-manifest validation, auto-correction of hallucinated segments,
the universal ``/dashboard/id/<uuid>`` resolver fallback, search-link /
plain-text degradation, external-URL immunity, fail-open behaviour,
the feature flag, and JSON manifest override merging.
"""

from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.services import url_guard
from app.services.url_guard import (
    URLGuardStats,
    guard_urls,
    reset_manifest_cache,
)

BASE = "https://demo.opencti.io"
UUID = "9a614a83-3bcd-4b62-91d0-d1b7e0a9f6b1"


@pytest.fixture(autouse=True)
def _clean_manifest_cache():
    reset_manifest_cache()
    yield
    reset_manifest_cache()


# ---------------------------------------------------------------------------
# Valid URLs are untouched
# ---------------------------------------------------------------------------


def test_valid_markdown_link_untouched():
    text = f"See [the report]({BASE}/dashboard/analyses/reports/{UUID})."
    assert guard_urls(text, [BASE]) == text


def test_valid_raw_url_untouched():
    text = f"Open {BASE}/dashboard/observations/indicators/{UUID} now."
    assert guard_urls(text, [BASE]) == text


def test_valid_id_resolver_link_untouched():
    text = f"[Open in OpenCTI]({BASE}/dashboard/id/{UUID})"
    assert guard_urls(text, [BASE]) == text


def test_repo_emitted_routes_are_all_valid():
    # The manifest must validate every route pattern this repo itself
    # emits (see integrations/providers/opencti/).
    for path in (
        f"/dashboard/id/{UUID}",
        f"/dashboard/workspaces/dashboards/{UUID}",
        f"/dashboard/workspaces/investigations/{UUID}",
        f"/dashboard/data/import/draft/{UUID}",
    ):
        text = f"Link: [item]({BASE}{path})"
        assert guard_urls(text, [BASE]) == text, path


def test_native_router_top_level_sections_are_all_valid():
    # Native-navigation parity (embedded chatbot in OpenCTI): every
    # top-level section of the OpenCTI front router
    # (opencti-front/src/private/Index.tsx) must validate, otherwise
    # the guard would "fix" a WORKING in-app link into a resolver /
    # search fallback and break native navigation.
    for path in (
        f"/dashboard/pirs/{UUID}",
        "/dashboard/audits/knowledge",
        "/dashboard/trash",
        "/dashboard/trash/operations",
        "/dashboard/search_bulk",
        "/dashboard/xtm-hub/agentic-workspace",
        "/dashboard/profile/me",
    ):
        text = f"Link: [item]({BASE}{path})"
        assert guard_urls(text, [BASE]) == text, path


def test_relative_links_stay_relative_through_every_rewrite():
    # The embedded chatbot routes RELATIVE links through the host
    # router (native navigation). A guard rewrite must never turn a
    # relative path into an absolute URL (which would only stay native
    # by the same-origin reduction) — base_url is "" for relative
    # matches and must remain so on every pipeline path.
    # 1. Correction path.
    out = guard_urls(f"[IOC](/dashboard/threats/indicators/{UUID})", [BASE])
    assert out == f"[IOC](/dashboard/observations/indicators/{UUID})"
    # 2. UUID resolver fallback path.
    out = guard_urls(f"[thing](/dashboard/nonsense/{UUID})", [BASE])
    assert out == f"[thing](/dashboard/id/{UUID})"
    # 3. Search-link degradation path.
    out = guard_urls("[APT41 report](/dashboard/bogus/section)", [BASE])
    assert out == "[APT41 report](/dashboard/search/APT41%20report)"


def test_data_trash_corrected_to_top_level_trash():
    # Trash is a top-level route (Index.tsx /trash/*), not a Data
    # sub-route — the UI only surfaces it in the Data menu.
    out = guard_urls(f"[trash]({BASE}/dashboard/data/trash)", [BASE])
    assert out == f"[trash]({BASE}/dashboard/trash)"


# ---------------------------------------------------------------------------
# Auto-correction (FR4)
# ---------------------------------------------------------------------------


def test_issue_example_threats_indicators_corrected():
    text = f"[IOC]({BASE}/dashboard/threats/indicators/{UUID})"
    out = guard_urls(text, [BASE])
    assert out == f"[IOC]({BASE}/dashboard/observations/indicators/{UUID})"


def test_raw_url_corrected():
    text = f"See {BASE}/dashboard/threats/indicators/{UUID} for details."
    out = guard_urls(text, [BASE])
    assert f"{BASE}/dashboard/observations/indicators/{UUID}" in out
    assert "threats/indicators" not in out


def test_unique_section_reverse_lookup():
    # "sightings" only exists under "events" — wrong section is remapped.
    text = f"[sighting]({BASE}/dashboard/observations/sightings/{UUID})"
    out = guard_urls(text, [BASE])
    assert out == f"[sighting]({BASE}/dashboard/events/sightings/{UUID})"


def test_singular_to_plural_correction():
    text = f"[report]({BASE}/dashboard/analyses/report/{UUID})"
    out = guard_urls(text, [BASE])
    assert out == f"[report]({BASE}/dashboard/analyses/reports/{UUID})"


def test_query_string_preserved_on_correction():
    text = f"[IOC]({BASE}/dashboard/threats/indicators/{UUID}?tab=overview)"
    out = guard_urls(text, [BASE])
    assert (
        out
        == f"[IOC]({BASE}/dashboard/observations/indicators/{UUID}?tab=overview)"
    )


# ---------------------------------------------------------------------------
# UUID resolver fallback (FR4 universal strategy)
# ---------------------------------------------------------------------------


def test_uuid_fallback_to_id_resolver():
    # Fabricated section that no rule can fix, but a UUID is present.
    text = f"[entity]({BASE}/dashboard/fabricated_section/things/{UUID})"
    out = guard_urls(text, [BASE])
    assert out == f"[entity]({BASE}/dashboard/id/{UUID})"


def test_uuid_fallback_for_raw_url():
    text = f"Check {BASE}/dashboard/nonsense/{UUID} please."
    out = guard_urls(text, [BASE])
    assert f"{BASE}/dashboard/id/{UUID}" in out


def test_uuid_fallback_preserves_query_suffix():
    text = f"[entity]({BASE}/dashboard/fabricated_section/things/{UUID}?tab=knowledge#top)"
    out = guard_urls(text, [BASE])
    assert out == f"[entity]({BASE}/dashboard/id/{UUID}?tab=knowledge#top)"


# ---------------------------------------------------------------------------
# Fallback handling (FR5/FR6)
# ---------------------------------------------------------------------------


def test_unverifiable_markdown_link_becomes_search_link():
    text = f"[APT28 Activity Report]({BASE}/dashboard/bogus/section)"
    out = guard_urls(text, [BASE])
    assert out == (
        f"[APT28 Activity Report]({BASE}/dashboard/search/"
        "APT28%20Activity%20Report)"
    )


def test_meaningless_link_text_degrades_to_plain_text():
    text = f"Go here: [x]({BASE}/dashboard/bogus/section) now."
    out = guard_urls(text, [BASE])
    assert out == "Go here: x now."


def test_unsalvageable_raw_url_degrades_to_inline_code():
    text = f"Open {BASE}/dashboard/bogus/section now."
    out = guard_urls(text, [BASE])
    assert out == f"Open `{BASE}/dashboard/bogus/section` now."


# ---------------------------------------------------------------------------
# External URLs and non-dashboard platform URLs are never touched
# ---------------------------------------------------------------------------


def test_external_url_with_dashboard_path_untouched():
    text = "See [docs](https://example.com/dashboard/threats/indicators/abc)."
    assert guard_urls(text, [BASE]) == text


def test_external_raw_url_untouched():
    text = "Visit https://github.com/dashboard/settings for info."
    assert guard_urls(text, [BASE]) == text


def test_platform_non_dashboard_path_untouched():
    text = f"API docs at {BASE}/graphql and {BASE}/public/feeds."
    assert guard_urls(text, [BASE]) == text


def test_presentation_slide_links_pass_through_next_to_a_corrected_route():
    # The deck tools hand the agent ``/presentations/<deck>#/<slide id>`` —
    # relative, as a Markdown target, or absolutized on the
    # platform's own host. None of those is an OpenCTI / OpenAEV route, so
    # the guard leaves every one of them byte-identical, fragment included
    # (a fragment it "corrected" would point the reader at the wrong
    # slide), while a hallucinated route in the same reply is still fixed.
    deck = "6f1c2c3a-9b1e-4d55-8a0f-0f8f8a3c2b11"
    slide = "a1B2-c3_d"
    text = (
        f"Done - see [slide 4](/presentations/{deck}#/{slide}) or "
        f"/presentations/{deck}#/3, also {BASE}/presentations/{deck}#/{slide}. "
        f"The indicator is at [IOC]({BASE}/dashboard/threats/indicators/{UUID})."
    )
    out = guard_urls(text, [BASE])
    assert f"[slide 4](/presentations/{deck}#/{slide})" in out
    assert f"/presentations/{deck}#/3," in out
    assert f"{BASE}/presentations/{deck}#/{slide}." in out
    assert f"[IOC]({BASE}/dashboard/observations/indicators/{UUID})" in out
    assert "threats/indicators" not in out
    # Without a hallucinated route in the text the fast path returns the
    # reply as is — a slide link alone never wakes the guard.
    links_only = f"See [slide 4](/presentations/{deck}#/{slide}) and /presentations/{deck}#/3."
    assert guard_urls(links_only, [BASE]) == links_only


# ---------------------------------------------------------------------------
# Relative paths
# ---------------------------------------------------------------------------


def test_relative_valid_path_untouched():
    text = f"Open /dashboard/analyses/reports/{UUID} in the platform."
    assert guard_urls(text, []) == text


def test_relative_invalid_path_corrected_without_base_urls():
    text = f"Open /dashboard/threats/indicators/{UUID} in the platform."
    out = guard_urls(text, [])
    assert f"/dashboard/observations/indicators/{UUID}" in out


def test_relative_markdown_link_uuid_fallback():
    text = f"[entity](/dashboard/wrong_section/{UUID})"
    out = guard_urls(text, [])
    assert out == f"[entity](/dashboard/id/{UUID})"


def test_relative_valid_path_with_query_untouched():
    # The query string must not leak into route validation — a valid
    # relative URL with ?query#fragment stays untouched.
    text = f"Open /dashboard/observations/indicators/{UUID}?filters=recent#top now."
    assert guard_urls(text, []) == text


def test_relative_path_query_preserved_on_correction():
    text = f"[IOC](/dashboard/threats/indicators/{UUID}?tab=overview)"
    out = guard_urls(text, [])
    assert out == f"[IOC](/dashboard/observations/indicators/{UUID}?tab=overview)"


def test_protocol_relative_url_untouched():
    text = f"See [x](//evil.example.com/dashboard/threats/indicators/{UUID})."
    assert guard_urls(text, [BASE]) == text


def test_relative_path_in_prose_parentheses_corrected():
    # A "(" right before the path is prose, not a markdown target — the
    # relative detector must still match and correct it.
    text = f"Open it (/dashboard/threats/indicators/{UUID}) in the platform."
    out = guard_urls(text, [])
    assert out == f"Open it (/dashboard/observations/indicators/{UUID}) in the platform."


def test_raw_url_in_prose_parentheses_keeps_closing_paren():
    # The unbalanced ")" belongs to the prose and must survive correction.
    text = f"See ({BASE}/dashboard/threats/indicators/{UUID}) for details."
    out = guard_urls(text, [BASE])
    assert out == f"See ({BASE}/dashboard/observations/indicators/{UUID}) for details."


# ---------------------------------------------------------------------------
# Code blocks and inline code are skipped
# ---------------------------------------------------------------------------


def test_fenced_code_block_untouched():
    bad = f"{BASE}/dashboard/threats/indicators/{UUID}"
    text = f"```\ncurl {bad}\n```\nAnd [link]({bad})."
    out = guard_urls(text, [BASE])
    assert f"curl {bad}" in out  # code block untouched
    assert f"[link]({BASE}/dashboard/observations/indicators/{UUID})" in out


def test_inline_code_untouched():
    bad = f"{BASE}/dashboard/threats/indicators/{UUID}"
    text = f"Use `{bad}` in your script."
    assert guard_urls(text, [BASE]) == text


# ---------------------------------------------------------------------------
# Fail-open, feature flag, fast bail-out
# ---------------------------------------------------------------------------


def test_fail_open_on_internal_error(monkeypatch):
    text = f"[IOC]({BASE}/dashboard/threats/indicators/{UUID})"

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(url_guard, "_load_manifest", _boom)
    assert guard_urls(text, [BASE]) == text


def test_weird_input_handled():
    weird = "[]() [a](not a url) /dashboard/ http://"
    # Must not raise, and external/garbage stays as-is.
    out = guard_urls(weird, [BASE])
    assert isinstance(out, str)


def test_flag_off_bypasses_guard(monkeypatch):
    monkeypatch.setattr(settings, "url_guard_enabled", False)
    text = f"[IOC]({BASE}/dashboard/threats/indicators/{UUID})"
    assert guard_urls(text, [BASE]) == text


def test_no_dashboard_content_fast_bailout():
    text = "Just a normal answer with https://example.com links."
    assert guard_urls(text, [BASE]) == text


def test_empty_text():
    assert guard_urls("", [BASE]) == ""


# ---------------------------------------------------------------------------
# Base URL matching details
# ---------------------------------------------------------------------------


def test_host_match_is_case_insensitive():
    text = f"[IOC](https://DEMO.opencti.io/dashboard/threats/indicators/{UUID})"
    out = guard_urls(text, [BASE])
    assert "/dashboard/observations/indicators/" in out


def test_base_url_with_path_prefix():
    base = "https://host.example.com/opencti"
    text = f"[IOC]({base}/dashboard/threats/indicators/{UUID})"
    out = guard_urls(text, [base])
    assert out == f"[IOC]({base}/dashboard/observations/indicators/{UUID})"


def test_same_host_bases_with_and_without_path_prefix():
    # A bare base on the same host must not shadow a path-prefixed base
    # checked later — matching continues across same-host candidates.
    bare = "https://host.example.com"
    prefixed = "https://host.example.com/opencti"
    text = f"[IOC]({prefixed}/dashboard/threats/indicators/{UUID})"
    out = guard_urls(text, [bare, prefixed])
    assert out == f"[IOC]({prefixed}/dashboard/observations/indicators/{UUID})"


def test_stats_counters():
    stats = URLGuardStats()
    text = (
        f"[ok]({BASE}/dashboard/analyses/reports/{UUID}) "
        f"[fix]({BASE}/dashboard/threats/indicators/{UUID}) "
        f"[gone]({BASE}/dashboard/bogus/section)"
    )
    guard_urls(text, [BASE], stats_out=stats)
    assert stats.detected == 3
    assert stats.validated == 1
    assert stats.corrected == 1
    assert stats.removed == 1


# ---------------------------------------------------------------------------
# JSON manifest override (FR8)
# ---------------------------------------------------------------------------


def test_json_override_merging(tmp_path, monkeypatch):
    override = {
        "routes": {
            "observations": ["custom_section"],
            "brand_new": ["alpha", "beta"],
        },
        "corrections": {"legacy/alpha": "brand_new/alpha"},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(override), encoding="utf-8")
    monkeypatch.setattr(settings, "url_guard_manifest_path", str(path))
    reset_manifest_cache()

    # New sub-section merged into an existing section.
    text = f"[x2]({BASE}/dashboard/observations/custom_section/{UUID})"
    assert guard_urls(text, [BASE]) == text

    # Entirely new section accepted.
    text = f"[x2]({BASE}/dashboard/brand_new/alpha/{UUID})"
    assert guard_urls(text, [BASE]) == text

    # Override correction rule applied.
    text = f"[fix]({BASE}/dashboard/legacy/alpha/{UUID})"
    out = guard_urls(text, [BASE])
    assert out == f"[fix]({BASE}/dashboard/brand_new/alpha/{UUID})"

    # Built-ins survive the merge: a known built-in route must still be
    # accepted by guard_urls with the override active (asserting on the
    # module constant would pass even if merging dropped built-ins).
    text = f"[ok]({BASE}/dashboard/observations/indicators/{UUID})"
    assert guard_urls(text, [BASE]) == text


def test_json_override_invalid_file_falls_back(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(settings, "url_guard_manifest_path", str(path))
    reset_manifest_cache()

    text = f"[IOC]({BASE}/dashboard/threats/indicators/{UUID})"
    out = guard_urls(text, [BASE])
    assert "/dashboard/observations/indicators/" in out


# ---------------------------------------------------------------------------
# OpenAEV (/admin/...) — previously unguarded entirely
# ---------------------------------------------------------------------------

AEV = "https://demo.openaev.io"


def test_openaev_valid_routes_untouched():
    # Every route the OpenAEV provider itself emits must validate, or the
    # guard would demote a working in-app link.
    for path in (
        f"/admin/assets/{UUID}",
        f"/admin/asset_groups/{UUID}",
        f"/admin/security_platforms/{UUID}",
        f"/admin/teams/{UUID}",
        f"/admin/persons/{UUID}",
        f"/admin/organizations/{UUID}",
        f"/admin/scenarios/{UUID}",
        f"/admin/simulations/{UUID}",
        f"/admin/atomic_testings/{UUID}",
        f"/admin/findings/{UUID}",
        "/admin/threat-arsenal",
        "/admin/workspaces/custom_dashboards",
        "/admin/settings/security/users",
        "/admin/integrations/collectors",
    ):
        text = f"Link: [item]({AEV}{path})"
        assert guard_urls(text, [AEV]) == text, path


def test_openaev_stale_v2_routes_are_corrected():
    # v3 renames whose first segment no longer exists, so validation fails
    # and the correction rules get a chance to run.
    cases = {
        f"/admin/exercises/{UUID}": f"/admin/simulations/{UUID}",
        f"/admin/reports/{UUID}": f"/admin/reporting/{UUID}",
        f"/admin/workspaces/dashboards/{UUID}": (
            f"/admin/workspaces/custom_dashboards/{UUID}"
        ),
    }
    for bad, good in cases.items():
        out = guard_urls(f"[x]({AEV}{bad})", [AEV])
        assert good in out, f"{bad} should be corrected to {good}, got {out}"


def test_openaev_payloads_route_is_corrected_to_threat_arsenal():
    # The payload screen became the Threat Arsenal, which has NO per-action
    # detail route, so the trailing id must be DROPPED. Asserting only that the
    # new prefix appears would also pass for /admin/threat-arsenal/<uuid>,
    # which just relocates the 404 - so pin the exact target and assert the id
    # is gone.
    out = guard_urls(f"[x]({AEV}/admin/payloads/{UUID})", [AEV])
    assert f"[x]({AEV}/admin/threat-arsenal)" == out
    assert UUID not in out


def test_openaev_list_only_section_drops_a_hallucinated_id():
    # A detail link invented for a list-only screen is truncated to the
    # section root, not left to 404 and not demoted to plain text.
    out = guard_urls(f"[x]({AEV}/admin/threat-arsenal/{UUID})", [AEV])
    assert f"[x]({AEV}/admin/threat-arsenal)" == out
    assert UUID not in out


def test_openaev_list_only_section_bare_route_still_valid():
    # The guard must not over-reach: the bare list route is untouched.
    text = f"[x]({AEV}/admin/threat-arsenal)"
    assert guard_urls(text, [AEV]) == text


def test_openaev_dynamic_section_ids_are_accepted():
    # Sections whose detail page is /<section>/<id> must accept ANY second
    # segment: over-restricting them would demote working links. This is
    # also why the legacy /admin/teams/players alias cannot be corrected.
    for path in (
        f"/admin/teams/{UUID}",
        "/admin/teams/players",
        f"/admin/assets/endpoints/{UUID}",
    ):
        text = f"[x]({AEV}{path})"
        assert guard_urls(text, [AEV]) == text, path


def test_openaev_unknown_route_degrades_without_inventing_a_resolver():
    # OpenAEV has no /admin/id/<uuid> resolver and no /admin/search/<text>
    # route, so an unfixable link must degrade to text rather than be
    # rewritten to a path that does not exist.
    out = guard_urls(f"[Some entity]({AEV}/admin/nonexistent/{UUID})", [AEV])
    assert "/admin/id/" not in out
    assert "/admin/search/" not in out
    assert "(" not in out or "http" not in out  # no active hyperlink remains
    assert "Some entity" in out


def test_openaev_raw_unknown_route_becomes_inline_code():
    out = guard_urls(f"Open {AEV}/admin/bogus/{UUID} now.", [AEV])
    assert f"`{AEV}/admin/bogus/{UUID}`" in out


def test_openaev_relative_admin_path_is_guarded():
    # The relative alternative previously matched only /dashboard/.
    assert guard_urls(f"Go to /admin/simulations/{UUID}", []) == (
        f"Go to /admin/simulations/{UUID}"
    )
    out = guard_urls(f"Go to /admin/payloads/{UUID}", [])
    assert "/admin/threat-arsenal" in out


def test_opencti_and_openaev_manifests_do_not_leak_into_each_other():
    # An OpenCTI section under /admin (or vice versa) is not valid.
    out = guard_urls(f"[x]({AEV}/admin/observations/indicators/{UUID})", [AEV])
    assert f"{AEV}/admin/observations/indicators/" not in out
    out = guard_urls(f"[x]({BASE}/dashboard/simulations/{UUID})", [BASE])
    assert f"{BASE}/dashboard/simulations/{UUID}" not in out


def test_openaev_stats_counted():
    stats = URLGuardStats()
    guard_urls(f"[x]({AEV}/admin/payloads/{UUID})", [AEV], stats_out=stats)
    assert stats.detected == 1
    assert stats.corrected == 1

"""Tests for the MITRE ATT&CK integration provider.

Covers the lenient config-token parsing (credential-less provider), bundle
indexing (ATT&CK IDs, types, relationships, matrix tactic order), tool
registration, object resolution (ATT&CK ID / STIX ID / name / alias), search,
technique + object listings with filters, relationship traversal with
procedure examples, revoked-object replacement hints, and domain validation.
"""

from __future__ import annotations

import copy
import json
import time

import httpx
import pytest

from app.integrations.providers import mitre_attack
from app.integrations.providers.mitre_attack import (
    ATTACK_DOMAINS,
    DEFAULT_SOURCE_URL,
    MitreAttackIntegration,
    _build_store,
    build_tools,
    bundle_url,
    index_url,
    normalize_domain,
    parse_attack_config,
    sanitize_source_url,
)
from app.services.integration_tools import _SafeIntegrationExecutor

# ---------------------------------------------------------------------------
# Synthetic STIX bundle (a miniature enterprise-attack domain)
# ---------------------------------------------------------------------------

_TACTIC_EXEC = "x-mitre-tactic--00000000-0000-4000-8000-00000000000a"
_TACTIC_IA = "x-mitre-tactic--00000000-0000-4000-8000-00000000000b"
_MATRIX = "x-mitre-matrix--00000000-0000-4000-8000-00000000000c"
_TECH_PARENT = "attack-pattern--00000000-0000-4000-8000-000000000001"
_TECH_SUB = "attack-pattern--00000000-0000-4000-8000-000000000002"
_TECH_REVOKED = "attack-pattern--00000000-0000-4000-8000-000000000003"
_GROUP = "intrusion-set--00000000-0000-4000-8000-000000000004"
_MALWARE = "malware--00000000-0000-4000-8000-000000000005"
_MITIGATION = "course-of-action--00000000-0000-4000-8000-000000000006"
_CAMPAIGN = "campaign--00000000-0000-4000-8000-000000000007"


def _ref(external_id: str, path: str) -> dict:
    return {
        "source_name": "mitre-attack",
        "external_id": external_id,
        "url": f"https://attack.mitre.org/{path}/{external_id.replace('.', '/')}",
    }


def _rel(rel_id: str, rel_type: str, source: str, target: str, **extra) -> dict:
    return {
        "type": "relationship",
        "id": f"relationship--00000000-0000-4000-8000-0000000000{rel_id}",
        "relationship_type": rel_type,
        "source_ref": source,
        "target_ref": target,
        **extra,
    }


_BUNDLE = {
    "type": "bundle",
    "id": "bundle--00000000-0000-4000-8000-0000000000ff",
    "objects": [
        {
            "type": "x-mitre-collection",
            "id": "x-mitre-collection--00000000-0000-4000-8000-0000000000fe",
            "name": "Enterprise ATT&CK",
            "x_mitre_version": "19.1",
        },
        {
            "type": "x-mitre-matrix",
            "id": _MATRIX,
            "name": "Enterprise ATT&CK",
            "tactic_refs": [_TACTIC_IA, _TACTIC_EXEC],
            "external_references": [_ref("enterprise-attack", "matrices")],
        },
        {
            "type": "x-mitre-tactic",
            "id": _TACTIC_EXEC,
            "name": "Execution",
            "description": "The adversary is trying to run malicious code.",
            "x_mitre_shortname": "execution",
            "external_references": [_ref("TA0002", "tactics")],
        },
        {
            "type": "x-mitre-tactic",
            "id": _TACTIC_IA,
            "name": "Initial Access",
            "description": "The adversary is trying to get into your network.",
            "x_mitre_shortname": "initial-access",
            "external_references": [_ref("TA0001", "tactics")],
        },
        {
            "type": "attack-pattern",
            "id": _TECH_PARENT,
            "name": "Command and Scripting Interpreter",
            "description": "Adversaries may abuse command and script interpreters. LSASS is unrelated here.",
            "x_mitre_is_subtechnique": False,
            "x_mitre_platforms": ["Windows", "Linux", "macOS"],
            "x_mitre_detection": "Monitor command-line arguments.",
            "kill_chain_phases": [
                {"kill_chain_name": "mitre-attack", "phase_name": "execution"}
            ],
            "modified": "2026-01-15T00:00:00.000Z",
            "external_references": [_ref("T1059", "techniques")],
        },
        {
            "type": "attack-pattern",
            "id": _TECH_SUB,
            "name": "PowerShell",
            "description": "Adversaries may abuse PowerShell commands and scripts.",
            "x_mitre_is_subtechnique": True,
            "x_mitre_platforms": ["Windows"],
            "kill_chain_phases": [
                {"kill_chain_name": "mitre-attack", "phase_name": "execution"}
            ],
            "modified": "2025-06-01T00:00:00.000Z",
            "external_references": [_ref("T1059.001", "techniques")],
        },
        {
            "type": "attack-pattern",
            "id": _TECH_REVOKED,
            "name": "Scripting",
            "description": "Old scripting technique, replaced by T1059.",
            "revoked": True,
            "kill_chain_phases": [
                {"kill_chain_name": "mitre-attack", "phase_name": "execution"}
            ],
            "external_references": [_ref("T1064", "techniques")],
        },
        {
            "type": "intrusion-set",
            "id": _GROUP,
            "name": "APT29",
            "aliases": ["APT29", "Cozy Bear", "The Dukes"],
            "description": "APT29 is a threat group attributed to the SVR.",
            "external_references": [_ref("G0016", "groups")],
        },
        {
            "type": "malware",
            "id": _MALWARE,
            "name": "Mimikatz",
            "x_mitre_aliases": ["Mimikatz"],
            "is_family": False,
            "description": "Credential dumper. LSASS memory access tool.",
            "x_mitre_platforms": ["Windows"],
            "external_references": [_ref("S0002", "software")],
        },
        {
            "type": "course-of-action",
            "id": _MITIGATION,
            "name": "Execution Prevention",
            "description": "Block execution of code on a system.",
            "external_references": [_ref("M1038", "mitigations")],
        },
        {
            "type": "campaign",
            "id": _CAMPAIGN,
            "name": "SolarWinds Compromise",
            "aliases": ["SolarWinds Compromise"],
            "description": "Supply chain compromise campaign.",
            "first_seen": "2019-08-01T00:00:00.000Z",
            "last_seen": "2021-01-01T00:00:00.000Z",
            "external_references": [_ref("C0024", "campaigns")],
        },
        _rel(
            "a1",
            "uses",
            _GROUP,
            _TECH_PARENT,
            description="APT29 has used encoded PowerShell commands.",
        ),
        _rel("a2", "uses", _GROUP, _MALWARE),
        _rel("a3", "uses", _MALWARE, _TECH_PARENT),
        _rel("a4", "mitigates", _MITIGATION, _TECH_PARENT),
        _rel("a5", "subtechnique-of", _TECH_SUB, _TECH_PARENT),
        _rel("a6", "attributed-to", _CAMPAIGN, _GROUP),
        _rel("a7", "revoked-by", _TECH_REVOKED, _TECH_PARENT),
        # Deprecated relationship must be ignored by traversal.
        _rel("a8", "uses", _CAMPAIGN, _TECH_SUB, x_mitre_deprecated=True),
    ],
}


def _make_store() -> mitre_attack._AttackStore:
    return _build_store("enterprise-attack", "", _BUNDLE)


def _build(token: str = "enterprise-attack||||", monkeypatch=None, store=None):  # noqa: S107 - config token, not a secret
    executor = _SafeIntegrationExecutor()
    tools = build_tools(token, executor)
    if monkeypatch is not None:
        the_store = store or _make_store()

        async def _fake_get_store(domain, version, source_url):
            if domain == the_store.domain:
                return the_store, None
            return None, f"MITRE ATT&CK error: no test data for {domain}"

        monkeypatch.setattr(mitre_attack, "_get_store", _fake_get_store)
    return executor._handlers, tools


# ---------------------------------------------------------------------------
# Config parsing (credential-less token)
# ---------------------------------------------------------------------------


def test_parse_attack_config_defaults_on_blank_and_dummy_tokens():
    for token in ("", "||", "__dummy__", "https://dummy.example.com||__dummy__"):
        domains, version, source = parse_attack_config(token)
        assert domains == list(ATTACK_DOMAINS)
        assert version == ""
        # None of these tokens carries a third (source_url) segment - the
        # URL-shaped value lands in the domains slot and is dropped as an
        # unknown domain - so every case falls back to the official source.
        assert source == DEFAULT_SOURCE_URL


def test_parse_attack_config_domains_version_and_source():
    domains, version, source = parse_attack_config(
        "enterprise, ics-attack||v19.1||https://mirror.example.com/attack/"
    )
    assert domains == ["enterprise-attack", "ics-attack"]
    assert version == "19.1"
    assert source == "https://mirror.example.com/attack"

    # Unknown domains are dropped; an all-invalid list falls back to all three.
    domains, _, _ = parse_attack_config("bogus,also-bad||not-a-version||ftp://nope")
    assert domains == list(ATTACK_DOMAINS)
    _, version, source = parse_attack_config("enterprise||not-a-version||ftp://nope")
    assert version == ""
    assert source == DEFAULT_SOURCE_URL


def test_sanitize_source_url_strips_query_and_rejects_unusable():
    # Query and fragment are stripped, trailing slash removed.
    assert sanitize_source_url("https://mirror.example.com/attack/?a=1#frag") == (
        "https://mirror.example.com/attack"
    )
    # Userinfo, non-http(s) schemes, and control characters are rejected.
    assert sanitize_source_url("https://user:pw@mirror.example.com/x") == ""
    assert sanitize_source_url("ftp://mirror.example.com") == ""
    assert sanitize_source_url("https://mirror.example.com/\x00") == ""
    assert sanitize_source_url("https://mirror.example.com/\x7f") == ""
    assert sanitize_source_url("") == ""


@pytest.mark.asyncio
async def test_connectivity_test_tolerates_non_dict_extra_config(monkeypatch):
    """Test Connection never raises when extra_config decodes to non-object JSON.

    ``test_provider_connectivity`` documents a never-raises contract, but the
    caller decodes extra_config from arbitrary JSON — a list/scalar payload
    (API misuse) must degrade to the defaults instead of an AttributeError.
    """
    from app.api.v1 import integration_testing

    captured: dict[str, str] = {}

    class _FakeResponse:
        status_code = 200

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            captured["url"] = url
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    result = await integration_testing.test_provider_connectivity(
        "mitre_attack", "public", ["not", "a", "dict"], "ATT&CK"
    )
    assert result["status"] == "ok"
    # The non-dict extra is coerced to {} — the probe targets the default source.
    assert captured["url"] == index_url(DEFAULT_SOURCE_URL)


def test_domain_aliases_and_urls():
    assert normalize_domain("Enterprise") == "enterprise-attack"
    assert normalize_domain("ICS") == "ics-attack"
    assert normalize_domain("unknown") == ""
    assert bundle_url(DEFAULT_SOURCE_URL, "mobile-attack", "") == (
        f"{DEFAULT_SOURCE_URL}/mobile-attack/mobile-attack.json"
    )
    assert bundle_url(DEFAULT_SOURCE_URL, "ics-attack", "18.1") == (
        f"{DEFAULT_SOURCE_URL}/ics-attack/ics-attack-18.1.json"
    )
    assert index_url(DEFAULT_SOURCE_URL) == f"{DEFAULT_SOURCE_URL}/index.json"


def test_provider_class_contract():
    integ = MitreAttackIntegration()
    assert integ.provider_name == "mitre_attack"
    assert integ.auth_types == ["none"]
    assert integ.get_oauth_defaults() is None


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_all_tools_registered_even_with_dummy_token():
    handlers, tools = _build("__dummy__")
    names = {t.name for t in tools}
    assert names == set(handlers)
    assert names == {
        "search_mitre_attack",
        "get_mitre_attack_object",
        "list_mitre_attack_tactics",
        "list_mitre_attack_techniques",
        "list_mitre_attack_objects",
        "get_mitre_attack_related",
        "get_mitre_attack_status",
    }
    by_name = {t.name: t for t in tools}
    # The knowledge base is read-only: every tool is a read.
    assert by_name["search_mitre_attack"].priority == 0
    assert by_name["get_mitre_attack_related"].priority == 0
    assert by_name["get_mitre_attack_status"].priority == 2
    assert all(t.source == "integration:mitre_attack" for t in tools)


# ---------------------------------------------------------------------------
# Store indexing
# ---------------------------------------------------------------------------


def test_build_store_indexes_ids_types_relationships_and_tactic_order():
    store = _make_store()
    assert store.version == "19.1"
    assert store.by_attack_id["T1059"] == _TECH_PARENT
    assert store.by_attack_id["T1059.001"] == _TECH_SUB
    assert store.by_attack_id["G0016"] == _GROUP
    assert len(store.by_type["attack-pattern"]) == 3
    # Matrix tactic_refs define the display order: initial-access first.
    assert store.tactic_order[_TACTIC_IA] < store.tactic_order[_TACTIC_EXEC]
    # Relationships indexed both ways.
    assert any(
        r["relationship_type"] == "uses" for r in store.rels_by_source[_GROUP]
    )
    assert any(
        r["relationship_type"] == "mitigates"
        for r in store.rels_by_target[_TECH_PARENT]
    )


def test_build_store_indexes_names_and_aliases():
    store = _make_store()
    # Primary names and aliases both resolve through the name index.
    assert store.by_name["apt29"] == [_GROUP]
    assert store.by_name["cozy bear"] == [_GROUP]
    assert store.by_name["the dukes"] == [_GROUP]
    assert store.by_name["mimikatz"] == [_MALWARE]


def test_build_store_whitelists_relationship_types():
    bundle = copy.deepcopy(_BUNDLE)
    bundle["objects"].append(_rel("b9", "related-to", _GROUP, _MALWARE))
    store = _build_store("enterprise-attack", "", bundle)
    rel_types = {
        r["relationship_type"]
        for rels in store.rels_by_source.values()
        for r in rels
    }
    assert "related-to" not in rel_types
    assert "uses" in rel_types


# ---------------------------------------------------------------------------
# Stale-cache fallback + retry back-off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_cache_served_with_retry_backoff(monkeypatch):
    store = _make_store()
    store.fetched_at = time.monotonic() - (mitre_attack._CACHE_TTL_SECONDS + 60)
    assert store.needs_refresh
    cache_key = f"{DEFAULT_SOURCE_URL}|enterprise-attack|latest"
    monkeypatch.setitem(mitre_attack._STORE_CACHE, cache_key, store)

    calls = {"count": 0}

    class _FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            calls["count"] += 1
            raise httpx.ConnectError("source down")

    monkeypatch.setattr(mitre_attack.httpx, "AsyncClient", _FailingClient)

    # First call attempts a refresh, fails, and serves the stale cache.
    got, err = await mitre_attack._get_store(
        "enterprise-attack", "", DEFAULT_SOURCE_URL
    )
    assert got is store and err is None
    assert calls["count"] == 1
    assert not store.needs_refresh  # retry deferred

    # Subsequent calls inside the back-off window don't retry the download.
    got, err = await mitre_attack._get_store(
        "enterprise-attack", "", DEFAULT_SOURCE_URL
    )
    assert got is store and err is None
    assert calls["count"] == 1

    # Once the back-off expires, a refresh is attempted again.
    store.retry_after = time.monotonic() - 1
    got, err = await mitre_attack._get_store(
        "enterprise-attack", "", DEFAULT_SOURCE_URL
    )
    assert got is store and err is None
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_oversized_bundle_is_rejected(monkeypatch):
    """A mirror returning a body beyond _MAX_BUNDLE_BYTES must not be parsed."""

    class _HugeResp:
        status_code = 200
        content = b"x" * 128

    class _HugeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            return _HugeResp()

    monkeypatch.setattr(mitre_attack.httpx, "AsyncClient", _HugeClient)
    monkeypatch.setattr(mitre_attack, "_STORE_CACHE", {})
    monkeypatch.setattr(mitre_attack, "_STORE_LOCKS", {})
    monkeypatch.setattr(mitre_attack, "_MAX_BUNDLE_BYTES", 64)

    # No cached copy: the oversized body is an error, never parsed.
    got, err = await mitre_attack._get_store("enterprise-attack", "", DEFAULT_SOURCE_URL)
    assert got is None
    assert "far larger than any official ATT&CK release" in (err or "")

    # With a stale cached copy: serve it and defer the retry instead.
    store = _make_store()
    store.fetched_at = time.monotonic() - (mitre_attack._CACHE_TTL_SECONDS + 60)
    cache_key = f"{DEFAULT_SOURCE_URL}|enterprise-attack|latest"
    mitre_attack._STORE_CACHE[cache_key] = store
    got, err = await mitre_attack._get_store("enterprise-attack", "", DEFAULT_SOURCE_URL)
    assert got is store and err is None
    assert not store.needs_refresh  # retry deferred


@pytest.mark.asyncio
async def test_cache_evicts_least_recently_used_store(monkeypatch):
    bundle_bytes = json.dumps(_BUNDLE).encode()

    class _Resp:
        status_code = 200
        content = bundle_bytes

    class _OKClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(mitre_attack.httpx, "AsyncClient", _OKClient)
    monkeypatch.setattr(mitre_attack, "_STORE_CACHE", {})
    monkeypatch.setattr(mitre_attack, "_STORE_LOCKS", {})
    monkeypatch.setattr(mitre_attack, "_CACHE_MAX_STORES", 2)
    source = "https://mirror.example.com/attack"

    await mitre_attack._get_store("enterprise-attack", "", source)
    await mitre_attack._get_store("mobile-attack", "", source)
    # enterprise-attack was fetched first but is the most recently USED store
    # (set explicitly — time.monotonic() granularity on Windows is too coarse
    # to distinguish back-to-back calls), so the cap must evict mobile-attack
    # (least recently used), not the oldest-fetched store.
    ent = mitre_attack._STORE_CACHE[f"{source}|enterprise-attack|latest"]
    mob = mitre_attack._STORE_CACHE[f"{source}|mobile-attack|latest"]
    assert ent.fetched_at <= mob.fetched_at
    ent.last_used = time.monotonic() + 100
    mob.last_used = time.monotonic() - 100
    await mitre_attack._get_store("ics-attack", "", source)

    keys = set(mitre_attack._STORE_CACHE)
    assert f"{source}|enterprise-attack|latest" in keys
    assert f"{source}|ics-attack|latest" in keys
    assert f"{source}|mobile-attack|latest" not in keys
    # The evicted store's idle lock is pruned too.
    assert f"{source}|mobile-attack|latest" not in mitre_attack._STORE_LOCKS


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_matches_names_aliases_and_ids(monkeypatch):
    handlers, _ = _build(monkeypatch=monkeypatch)
    search = handlers["search_mitre_attack"]

    result = await search(query="Cozy Bear")
    assert "G0016" in result and "APT29" in result

    result = await search(query="T1059.001")
    assert "PowerShell" in result

    # Description-only matches rank after name matches but are found.
    result = await search(query="LSASS")
    assert "S0002" in result and "T1059" in result

    # Type filter narrows results.
    result = await search(query="LSASS", object_types=["software"])
    assert "S0002" in result and "T1059 " not in result

    # Revoked objects are hidden unless include_deprecated is set.
    assert "T1064" not in await search(query="Scripting")
    assert "T1064" in await search(query="Scripting", include_deprecated=True)

    assert "Error" in await search(query="  ")
    assert "unknown object type" in await search(query="x", object_types=["bogus"])


@pytest.mark.asyncio
async def test_pagination_tolerates_malformed_limit_and_offset(monkeypatch):
    """LLM tool args are not schema-coerced — bad limit/offset must not raise."""
    handlers, _ = _build(monkeypatch=monkeypatch)
    search = handlers["search_mitre_attack"]

    # Numeric strings are honoured; garbage falls back to the defaults.
    assert "G0016" in await search(query="Cozy Bear", limit="5", offset="0")
    for bad in ("abc", None, ["5"], "1.5"):
        result = await search(query="Cozy Bear", limit=bad, offset=bad)
        assert "G0016" in result

    page, hint = mitre_attack._paginate(list(range(30)), "abc", None)
    assert page == list(range(25)) and "offset=25" in hint

    # A malformed/None limit falls back to the tool's own documented default,
    # not the global one (search_mitre_attack documents default 20).
    page, _hint = mitre_attack._paginate(list(range(30)), None, 0, default_limit=20)
    assert page == list(range(20))

    # Valid falsy values are preserved, not treated as missing: limit=0 is
    # clamped to the 1-item minimum instead of falling back to the default.
    page, _hint = mitre_attack._paginate(list(range(30)), 0, 0)
    assert page == [0]
    assert mitre_attack._coerce_int(0, 25) == 0
    assert mitre_attack._coerce_int(None, 25) == 25


# ---------------------------------------------------------------------------
# Get object
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_object_by_id_name_and_alias(monkeypatch):
    handlers, _ = _build(monkeypatch=monkeypatch)
    get_obj = handlers["get_mitre_attack_object"]

    result = await get_obj(attack_id="T1059")
    assert "Command and Scripting Interpreter" in result
    assert "https://attack.mitre.org/techniques/T1059" in result
    assert "Detection: Monitor command-line arguments." in result
    # Relationship summary includes mitigations, sub-techniques, and users.
    assert "M1038" in result
    assert "T1059.001" in result
    assert "G0016" in result

    # Exact alias resolution.
    result = await get_obj(attack_id="The Dukes")
    assert "APT29" in result

    # STIX id resolution.
    result = await get_obj(attack_id=_MALWARE)
    assert "Mimikatz" in result

    # include_relationships=False keeps the output lean.
    result = await get_obj(attack_id="T1059", include_relationships=False)
    assert "Relationships:" not in result

    result = await get_obj(attack_id="T9999")
    assert "No ATT&CK object found" in result


def test_campaign_window_handles_null_last_seen():
    """An explicit null last_seen renders as '?', never the string 'None'."""
    store = _make_store()
    campaign = dict(store.objects[_CAMPAIGN])
    detail = mitre_attack._format_detail(store, campaign)
    assert "Campaign window: 2019-08-01 -> 2021-01-01" in detail

    campaign["last_seen"] = None
    detail = mitre_attack._format_detail(store, campaign)
    assert "Campaign window: 2019-08-01 -> ?" in detail

    del campaign["last_seen"]
    detail = mitre_attack._format_detail(store, campaign)
    assert "Campaign window: 2019-08-01 -> ?" in detail


@pytest.mark.asyncio
async def test_get_revoked_object_points_to_replacement(monkeypatch):
    handlers, _ = _build(monkeypatch=monkeypatch)
    result = await handlers["get_mitre_attack_object"](attack_id="T1064")
    assert "[REVOKED]" in result
    assert "REVOKED and replaced by" in result
    assert "T1059" in result


@pytest.mark.asyncio
async def test_get_revoked_object_surfaces_inactive_replacement(monkeypatch):
    # Chained revocation: the replacement is itself revoked. _related_entries
    # filters inactive objects, so the fallback loop must still surface it.
    old_tech = "attack-pattern--00000000-0000-4000-8000-0000000000e1"
    bundle = {
        "type": "bundle",
        "id": "bundle--00000000-0000-4000-8000-0000000000fd",
        "objects": [
            {
                "type": "attack-pattern",
                "id": old_tech,
                "name": "Really Old Scripting",
                "revoked": True,
                "external_references": [_ref("T1065", "techniques")],
            },
            {
                "type": "attack-pattern",
                "id": _TECH_REVOKED,
                "name": "Scripting",
                "revoked": True,
                "external_references": [_ref("T1064", "techniques")],
            },
            _rel("e1", "revoked-by", old_tech, _TECH_REVOKED),
        ],
    }
    store = _build_store("enterprise-attack", "", bundle)
    handlers, _ = _build(monkeypatch=monkeypatch, store=store)
    result = await handlers["get_mitre_attack_object"](attack_id="T1065")
    assert "REVOKED and replaced by" in result
    assert "T1064" in result


# ---------------------------------------------------------------------------
# Tactics + techniques listings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tactics_matrix_order_and_counts(monkeypatch):
    handlers, _ = _build(monkeypatch=monkeypatch)
    result = await handlers["list_mitre_attack_tactics"]()
    # Matrix order: initial-access before execution.
    assert result.index("TA0001") < result.index("TA0002")
    # One active parent technique in execution (sub + revoked don't count).
    assert "shortname: execution, 1 techniques" in result


@pytest.mark.asyncio
async def test_list_techniques_filters(monkeypatch):
    handlers, _ = _build(monkeypatch=monkeypatch)
    list_techniques = handlers["list_mitre_attack_techniques"]

    result = await list_techniques(tactic="execution")
    assert "T1059" in result and "T1059.001" not in result and "T1064" not in result

    result = await list_techniques(subtechniques="only")
    assert "T1059.001" in result and "T1059 " not in result

    result = await list_techniques(platform="linux", subtechniques="include")
    assert "T1059" in result and "T1059.001" not in result

    result = await list_techniques(modified_since="2026-01-01", subtechniques="include")
    assert "T1059" in result and "T1059.001" not in result

    assert "Error" in await list_techniques(subtechniques="bogus")
    assert "No techniques found" in await list_techniques(tactic="impact")


# ---------------------------------------------------------------------------
# Generic object listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_objects_by_type_and_name_filter(monkeypatch):
    handlers, _ = _build(monkeypatch=monkeypatch)
    list_objects = handlers["list_mitre_attack_objects"]

    result = await list_objects(object_type="groups")
    assert "G0016" in result

    # Software = malware + tool STIX types.
    result = await list_objects(object_type="software")
    assert "S0002" in result

    result = await list_objects(object_type="campaigns", name_filter="solarwinds")
    assert "C0024" in result

    result = await list_objects(object_type="groups", name_filter="nomatch")
    assert "No groups found" in result

    assert "unknown object type" in await list_objects(object_type="bogus")


# ---------------------------------------------------------------------------
# Relationship traversal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_related_group_techniques_with_procedure_examples(monkeypatch):
    handlers, _ = _build(monkeypatch=monkeypatch)
    related = handlers["get_mitre_attack_related"]

    result = await related(
        attack_id="G0016",
        relationship="uses",
        direction="outgoing",
        related_type="techniques",
    )
    assert "T1059" in result
    assert "APT29 has used encoded PowerShell commands." in result
    assert "S0002" not in result  # related_type filter applied

    # Incoming: which groups use T1059?
    result = await related(
        attack_id="T1059", relationship="uses", direction="incoming"
    )
    assert "G0016" in result and "S0002" in result

    # Campaign attribution.
    result = await related(attack_id="C0024", relationship="attributed-to")
    assert "G0016" in result

    # Deprecated relationships are ignored.
    result = await related(attack_id="C0024", relationship="uses")
    assert "No relationships found" in result

    assert "unknown relationship" in await related(
        attack_id="G0016", relationship="bogus"
    )
    assert "direction must be" in await related(
        attack_id="G0016", direction="sideways"
    )
    assert "No ATT&CK object found" in await related(attack_id="T9999")


# ---------------------------------------------------------------------------
# Domain validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain_validation_and_enabled_domain_restriction(monkeypatch):
    handlers, _ = _build("enterprise||||", monkeypatch=monkeypatch)
    search = handlers["search_mitre_attack"]

    assert "unknown ATT&CK domain" in await search(query="x", domain="bogus")
    assert "not enabled" in await search(query="x", domain="ics-attack")
    # Shorthand aliases resolve.
    result = await search(query="APT29", domain="enterprise")
    assert "G0016" in result


@pytest.mark.asyncio
async def test_attack_techniques_lists_live_techniques_with_their_definitions(monkeypatch):
    """The catalogue the extraction classifies behaviours against: live techniques and
    sub-techniques only (T1064 is revoked), sorted, with tactics and the definition."""
    _build(monkeypatch=monkeypatch)
    techniques = await mitre_attack.attack_techniques()
    assert [t.attack_id for t in techniques] == ["T1059", "T1059.001"]
    assert techniques[1].name == "PowerShell"
    assert techniques[1].tactics == ("execution",)
    assert techniques[1].description == "Adversaries may abuse PowerShell commands and scripts."


@pytest.mark.asyncio
async def test_attack_techniques_is_none_without_a_store(monkeypatch):
    _build(monkeypatch=monkeypatch)
    assert await mitre_attack.attack_techniques(domain="mobile-attack") is None


def test_the_builder_declares_the_attack_source_its_tools_read() -> None:
    """The ATT&CK source is a capability the provider declares (``attack_source_sink``),
    so the generic integration build names no vendor; a builder not declaring a sink is
    never handed one (invariant 18)."""
    from app.integrations.providers import mitre_attack
    from app.services.integration_tools import _declares
    from app.tools.base import CallableToolExecutor

    given: list[tuple[str, str]] = []
    mitre_attack.build_tools("enterprise||17.1||https://mirror.internal/attack", CallableToolExecutor(),
                             attack_source_sink=given.append)
    assert given == [("https://mirror.internal/attack", "17.1")]
    assert _declares(mitre_attack.build_tools, "attack_source_sink")
    assert not _declares(mitre_attack.build_tools, "platform_names_sink")

    def undeclared(token: str, executor: object, **kwargs: object) -> list:
        return []

    assert not _declares(undeclared, "attack_source_sink")

"""Tests for the MITRE ATLAS integration provider.

Covers tool introspection with the catalog dummy token, source-URL
resolution, the git-symlink indirection resolution, the TTL cache (incl.
forced refresh), format-version validation, matrix/tactic/technique
formatting, keyword search with type filters and ranking, mitigation and
case-study filters + formatting (incl. attack-chain ordering), relationship
traversal in both directions with type filtering, the ATT&CK crosswalk
(hit / miss / list-all modes), filtering + pagination, id normalization,
and input validation. Every fetch routes through the shared
``safe_request`` helper, which is monkeypatched here so no network is
touched.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml

from app.integrations.providers import mitre_atlas as atlas_mod
from app.integrations.providers.mitre_atlas import (
    ATLAS_DEFAULT_SOURCE_URL,
    MitreAtlasIntegration,
    build_tools,
    resolve_atlas_source_url,
)
from app.tools.base import CallableToolExecutor, ToolCall

_SOURCE_URL = "https://mirror.example.com/dist/v6/ATLAS-latest.yaml"

_SAMPLE_DATA: dict[str, Any] = {
    "format-version": "6.0.0",
    "collection": {
        "id": "ATLAS-collection",
        "name": "ATLAS",
        "version": "2026.06",
        "modified-date": "2026-05-27",
        "object-type": "collection",
    },
    "matrix": {
        "id": "ATLAS-matrix",
        "name": "ATLAS",
        "object-type": "matrix",
    },
    "tactics": {
        "AML.TA0002": {
            "id": "AML.TA0002",
            "name": "Reconnaissance",
            "description": "The adversary is trying to gather information about the AI system. More detail here.",
            "object-type": "tactic",
            "attack-reference": {
                "id": "TA0043",
                "url": "https://attack.mitre.org/tactics/TA0043/",
            },
        },
        "AML.TA0001": {
            "id": "AML.TA0001",
            "name": "AI Attack Staging",
            "description": "The adversary is leveraging their knowledge to tailor the attack.",
            "object-type": "tactic",
        },
    },
    "techniques": {
        "AML.T0005": {
            "id": "AML.T0005",
            "name": "Create Proxy AI Model",
            "description": (
                "Adversaries may obtain models to serve as proxies. See "
                "[Full AI Model Access](/techniques/AML.T0044) for more."
            ),
            "object-type": "technique",
            "maturity": "Demonstrated",
            "platforms": ["Predictive AI", "Generative AI"],
            "modified-date": "2026-05-27",
            "references": [
                {"title": "Example paper", "url": "https://example.com/paper"}
            ],
        },
        "AML.T0005.000": {
            "id": "AML.T0005.000",
            "name": "Train Proxy via Gathered AI Artifacts",
            "description": "Sub-technique detail.",
            "object-type": "technique",
            "maturity": "Demonstrated",
            "platforms": ["Predictive AI"],
        },
        "AML.T0040": {
            "id": "AML.T0040",
            "name": "AI Model Inference API Access",
            "description": "Adversaries may use prompt injection against inference APIs.",
            "object-type": "technique",
            "maturity": "Realized",
            "platforms": ["Generative AI", "Agentic AI"],
            "attack-reference": {
                "id": "T1595",
                "url": "https://attack.mitre.org/techniques/T1595/",
            },
        },
    },
    "mitigations": {
        "AML.M0000": {
            "id": "AML.M0000",
            "name": "Limit Public Release of Information",
            "description": "Limit the public release of technical information.",
            "object-type": "mitigation",
            "categories": ["Policy"],
            "lifecycle-phases": ["Business and Data Understanding"],
        },
    },
    "case-studies": {
        "AML.CS0000": {
            "id": "AML.CS0000",
            "name": "Evasion of Deep Learning Detector",
            "description": "The team trained a proxy model to evade the detector.",
            "object-type": "case-study",
            "type": "Exercise",
            "actor": "Palo Alto Networks AI Research Team",
            "target": "Palo Alto Networks malware detection system",
            "date": "2020-01-01",
            "references": [],
        },
    },
    "relationships": {
        "ATLAS-matrix": {
            "sequences": [
                {
                    "source": "ATLAS-matrix",
                    "target": "AML.TA0002",
                    "relationship-type": "sequences",
                    "position": 1,
                },
                {
                    "source": "ATLAS-matrix",
                    "target": "AML.TA0001",
                    "relationship-type": "sequences",
                    "position": 2,
                },
            ]
        },
        "AML.T0005": {
            "achieves": [
                {
                    "source": "AML.T0005",
                    "target": "AML.TA0001",
                    "relationship-type": "achieves",
                }
            ]
        },
        "AML.T0005.000": {
            "achieves": [
                {
                    "source": "AML.T0005.000",
                    "target": "AML.TA0001",
                    "relationship-type": "achieves",
                }
            ],
            "specializes": [
                {
                    "source": "AML.T0005.000",
                    "target": "AML.T0005",
                    "relationship-type": "specializes",
                }
            ],
        },
        "AML.T0040": {
            "achieves": [
                {
                    "source": "AML.T0040",
                    "target": "AML.TA0002",
                    "relationship-type": "achieves",
                }
            ]
        },
        "AML.M0000": {
            "mitigates": [
                {
                    "source": "AML.M0000",
                    "target": "AML.T0005",
                    "relationship-type": "mitigates",
                    "description": "Limit the connection between public info and production models.",
                }
            ]
        },
        "AML.CS0000": {
            "employs": [
                {
                    "source": "AML.CS0000",
                    "target": "AML.T0005",
                    "relationship-type": "employs",
                    "description": "Trained a proxy model.",
                    "tactic": "AML.TA0001",
                    "step-id": "S00",
                    "leads-to": ["S01"],
                },
                {
                    "source": "AML.CS0000",
                    "target": "AML.T0040",
                    "relationship-type": "employs",
                    "description": "Queried the inference API.",
                    "tactic": "AML.TA0002",
                    "step-id": "S01",
                },
            ]
        },
    },
}

_SAMPLE_YAML = yaml.safe_dump(_SAMPLE_DATA, sort_keys=False)


class _StubResponse:
    def __init__(self, text: str):
        self.text = text
        # Mirror httpx.Response: `.content` is the raw body in bytes (the
        # oversized-dataset guard measures bytes, not decoded characters).
        self.content = text.encode("utf-8")


class _Recorder:
    """Fake ``safe_request`` that routes by URL and records every call."""

    def __init__(self, router):
        self._router = router
        self.calls: list[str] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append(url)
        body = self._router(url)
        if body is None:
            return None, f"Error: HTTP 404 for {url}"
        return _StubResponse(body), None


@pytest.fixture(autouse=True)
def _clear_cache():
    atlas_mod._CACHE.clear()
    atlas_mod._CACHE_LOCKS.clear()
    yield
    atlas_mod._CACHE.clear()
    atlas_mod._CACHE_LOCKS.clear()


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(atlas_mod, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_SOURCE_URL, executor=executor)
    return executor, recorder


def _yaml_router(url: str) -> str | None:
    return _SAMPLE_YAML


async def _call(executor: CallableToolExecutor, name: str, **arguments: Any):
    return await executor.execute_tool(
        ToolCall(id="c1", name=name, arguments=arguments)
    )


# ---------------------------------------------------------------------------
# Introspection + source URL resolution
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(token="__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert {
        "get_atlas_matrix",
        "list_atlas_tactics",
        "get_atlas_tactic",
        "list_atlas_techniques",
        "get_atlas_technique",
        "search_atlas",
        "get_atlas_status",
        "list_atlas_mitigations",
        "get_atlas_mitigation",
        "list_atlas_case_studies",
        "get_atlas_case_study",
        "get_atlas_related_objects",
        "map_attack_to_atlas",
    } <= names
    assert len(tools) == 13
    # The knowledge base is read-only upstream - every tool is a read.
    assert all(t.priority == 0 for t in tools)
    assert all(t.source == "integration:mitre_atlas" for t in tools)


def test_resolve_atlas_source_url():
    assert resolve_atlas_source_url("") == ATLAS_DEFAULT_SOURCE_URL
    assert resolve_atlas_source_url(None) == ATLAS_DEFAULT_SOURCE_URL
    assert resolve_atlas_source_url("__dummy__") == ATLAS_DEFAULT_SOURCE_URL
    assert resolve_atlas_source_url("__no_auth__") == ATLAS_DEFAULT_SOURCE_URL
    assert resolve_atlas_source_url("  https://mirror.local/ATLAS.yaml  ") == (
        "https://mirror.local/ATLAS.yaml"
    )
    # Malformed overrides fall back to the official default: userinfo
    # credentials, control characters, and host-less URLs are rejected.
    assert (
        resolve_atlas_source_url("https://user:s3cret@mirror.local/ATLAS.yaml")
        == ATLAS_DEFAULT_SOURCE_URL
    )
    assert (
        resolve_atlas_source_url("https://mirror.local/A\r\nTLAS.yaml")
        == ATLAS_DEFAULT_SOURCE_URL
    )
    assert resolve_atlas_source_url("http:///ATLAS.yaml") == ATLAS_DEFAULT_SOURCE_URL
    # Query / fragment are stripped so tokens never reach fetch logs and
    # the per-URL cache key stays canonical.
    assert resolve_atlas_source_url(
        "https://mirror.local/ATLAS.yaml?token=s3cret#frag"
    ) == "https://mirror.local/ATLAS.yaml"


# ---------------------------------------------------------------------------
# Data layer: symlink indirection, cache, validation
# ---------------------------------------------------------------------------


def test_symlink_body_detection():
    assert atlas_mod._looks_like_symlink_body("ATLAS-2026.06.yaml")
    assert atlas_mod._looks_like_symlink_body("v6/ATLAS-latest.yaml\n")
    assert not atlas_mod._looks_like_symlink_body(_SAMPLE_YAML)
    assert not atlas_mod._looks_like_symlink_body("format-version: 6.0.0")
    assert not atlas_mod._looks_like_symlink_body("")
    # A git-symlink target is always a short RELATIVE path: absolute,
    # network-path (//host/... would urljoin onto a DIFFERENT host), full
    # URL, and backslashed forms must never be followed.
    assert not atlas_mod._looks_like_symlink_body("/etc/ATLAS.yaml")
    assert not atlas_mod._looks_like_symlink_body("//evil.example/ATLAS.yaml")
    assert not atlas_mod._looks_like_symlink_body("https://evil.example/ATLAS.yaml")
    assert not atlas_mod._looks_like_symlink_body("..\\evil\\ATLAS.yaml")
    # ".." traversal segments would climb OUT of the source directory via
    # urljoin - reject anywhere in the path, not just at the start.
    assert not atlas_mod._looks_like_symlink_body("../ATLAS.yaml")
    assert not atlas_mod._looks_like_symlink_body("v6/../ATLAS.yaml")
    assert not atlas_mod._looks_like_symlink_body("v6/../../ATLAS.yaml")
    # Dotted FILE NAMES (release versions) are not traversal segments.
    assert atlas_mod._looks_like_symlink_body("ATLAS-2026.06.yaml")


@pytest.mark.asyncio
async def test_fetch_resolves_one_level_of_symlink_indirection(monkeypatch):
    def router(url: str) -> str | None:
        if url == _SOURCE_URL:
            return "ATLAS-2026.06.yaml"
        if url == "https://mirror.example.com/dist/v6/ATLAS-2026.06.yaml":
            return _SAMPLE_YAML
        return None

    executor, recorder = _build(monkeypatch, router)
    result = await _call(executor, "get_atlas_status")
    assert not result.is_error
    assert "release 2026.06" in result.content
    assert recorder.calls == [
        _SOURCE_URL,
        "https://mirror.example.com/dist/v6/ATLAS-2026.06.yaml",
    ]


@pytest.mark.asyncio
async def test_dataset_is_cached_between_calls(monkeypatch):
    executor, recorder = _build(monkeypatch, _yaml_router)
    r1 = await _call(executor, "get_atlas_matrix")
    r2 = await _call(executor, "list_atlas_tactics")
    assert not r1.is_error and not r2.is_error
    assert len(recorder.calls) == 1  # second call served from the TTL cache


@pytest.mark.asyncio
async def test_status_refresh_forces_refetch(monkeypatch):
    executor, recorder = _build(monkeypatch, _yaml_router)
    await _call(executor, "get_atlas_status")
    await _call(executor, "get_atlas_status")
    assert len(recorder.calls) == 1
    result = await _call(executor, "get_atlas_status", refresh=True)
    assert not result.is_error
    assert len(recorder.calls) == 2


@pytest.mark.asyncio
async def test_unsupported_format_version_is_rejected(monkeypatch):
    legacy = yaml.safe_dump({"format-version": "5.6.0", "tactics": {}})
    executor, _ = _build(monkeypatch, lambda url: legacy)
    result = await _call(executor, "get_atlas_matrix")
    assert "unsupported ATLAS data format version '5.6.0'" in result.content


@pytest.mark.asyncio
async def test_non_atlas_document_is_rejected(monkeypatch):
    executor, _ = _build(monkeypatch, lambda url: "just: some\nrandom: yaml")
    result = await _call(executor, "get_atlas_matrix")
    assert "unexpected document" in result.content


@pytest.mark.asyncio
async def test_validate_connection_and_user_info(monkeypatch):
    recorder = _Recorder(_yaml_router)
    monkeypatch.setattr(atlas_mod, "safe_request", recorder)
    integration = MitreAtlasIntegration()
    assert await integration.validate_connection(_SOURCE_URL) is True
    info = await integration.get_user_info(_SOURCE_URL)
    assert info["release"] == "2026.06"
    assert info["tactics"] == 2
    assert info["techniques"] == 3
    assert info["mitigations"] == 1
    assert info["case_studies"] == 1


@pytest.mark.asyncio
async def test_fetch_error_is_surfaced(monkeypatch):
    executor, _ = _build(monkeypatch, lambda url: None)
    result = await _call(executor, "get_atlas_matrix")
    assert "Error" in result.content


# ---------------------------------------------------------------------------
# Matrix + tactics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matrix_shows_tactics_in_official_order(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "get_atlas_matrix")
    assert not result.is_error
    content = result.content
    assert "release 2026.06" in content
    # Matrix order comes from the sequences positions, not alphabetical ids.
    assert content.index("AML.TA0002") < content.index("AML.TA0001")
    assert "1. [AML.TA0002] Reconnaissance" in content


@pytest.mark.asyncio
async def test_get_tactic_normalizes_bare_id_and_lists_techniques(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "get_atlas_tactic", tactic_id="ta0001")
    assert not result.is_error
    content = result.content
    assert "[AML.TA0001] AI Attack Staging" in content
    assert "https://atlas.mitre.org/tactics/AML.TA0001" in content
    assert "[AML.T0005] Create Proxy AI Model" in content
    assert "[AML.T0005.000]" in content


@pytest.mark.asyncio
async def test_get_tactic_shows_attack_counterpart(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "get_atlas_tactic", tactic_id="AML.TA0002")
    assert "ATT&CK counterpart: TA0043" in result.content


@pytest.mark.asyncio
async def test_get_tactic_rejects_invalid_id(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "get_atlas_tactic", tactic_id="AML.T0005")
    assert "not a valid ATLAS tactic id" in result.content


@pytest.mark.asyncio
async def test_list_tactics_without_description_has_clean_punctuation(monkeypatch):
    data = copy.deepcopy(_SAMPLE_DATA)
    del data["tactics"]["AML.TA0001"]["description"]
    yaml_text = yaml.safe_dump(data, sort_keys=False)
    executor, _ = _build(monkeypatch, lambda url: yaml_text)
    result = await _call(executor, "list_atlas_tactics")
    assert not result.is_error
    # A tactic without a description ends at the technique count - never
    # the dangling ". ." a blank summary suffix would produce.
    assert "[AML.TA0001] AI Attack Staging - 2 technique(s).\n" in (
        result.content + "\n"
    )
    assert ". ." not in result.content


@pytest.mark.asyncio
async def test_list_tactics_paginates(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "list_atlas_tactics", limit=1, offset=0)
    assert "Found 2 ATLAS tactics" in result.content
    assert "AML.TA0002" in result.content
    assert "AML.TA0001" not in result.content
    assert "pass offset=1" in result.content


# ---------------------------------------------------------------------------
# Techniques
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_techniques_filters(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)

    result = await _call(executor, "list_atlas_techniques", platform="Agentic AI")
    assert "AML.T0040" in result.content
    assert "AML.T0005" not in result.content.replace("AML.T0040", "")

    result = await _call(executor, "list_atlas_techniques", maturity="Realized")
    assert "AML.T0040" in result.content
    assert "Found 1 ATLAS technique" in result.content

    result = await _call(executor, "list_atlas_techniques", tactic_id="AML.TA0001")
    assert "AML.T0005" in result.content
    assert "AML.T0040" not in result.content

    result = await _call(executor, "list_atlas_techniques", parent_only=True)
    assert "AML.T0005.000" not in result.content
    assert "Found 2 ATLAS technique" in result.content


@pytest.mark.asyncio
async def test_list_techniques_no_match_hints_valid_values(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "list_atlas_techniques", maturity="Imaginary")
    assert "Feasible, Demonstrated, Realized" in result.content


@pytest.mark.asyncio
async def test_get_technique_full_dossier(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "get_atlas_technique", technique_id="T0005")
    assert not result.is_error
    content = result.content
    assert "[AML.T0005] Create Proxy AI Model" in content
    assert "https://atlas.mitre.org/techniques/AML.T0005" in content
    assert "Demonstrated" in content
    assert "[AML.TA0001] AI Attack Staging" in content
    # relationships from the reverse indexes
    assert "[AML.T0005.000] Train Proxy via Gathered AI Artifacts" in content
    assert "[AML.M0000] Limit Public Release of Information" in content
    assert "[AML.CS0000] Evasion of Deep Learning Detector" in content
    # site-relative markdown links are rewritten to absolute URLs
    assert "(https://atlas.mitre.org/techniques/AML.T0044)" in content
    assert "](/techniques/" not in content
    # references surface
    assert "Example paper" in content


@pytest.mark.asyncio
async def test_get_technique_shows_parent_and_attack_reference(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(
        executor, "get_atlas_technique", technique_id="AML.T0005.000"
    )
    assert "Sub-technique of: [AML.T0005]" in result.content

    result = await _call(executor, "get_atlas_technique", technique_id="AML.T0040")
    assert "ATT&CK counterpart: T1595" in result.content


@pytest.mark.asyncio
async def test_get_technique_unknown_id(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "get_atlas_technique", technique_id="AML.T9999")
    assert "was not found in ATLAS release 2026.06" in result.content


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_ranks_name_matches_first(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "search_atlas", query="proxy")
    content = result.content
    # Name matches (T0005, T0005.000) rank before description-only matches.
    assert content.index("AML.T0005") < content.index("AML.CS0000")


@pytest.mark.asyncio
async def test_search_matches_case_study_actor_and_target(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "search_atlas", query="palo alto")
    assert "AML.CS0000" in result.content


@pytest.mark.asyncio
async def test_search_object_type_filter_and_validation(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(
        executor, "search_atlas", query="proxy", object_types="technique"
    )
    assert "AML.T0005" in result.content
    assert "AML.CS0000" not in result.content

    result = await _call(
        executor, "search_atlas", query="proxy", object_types="banana"
    )
    assert "not a valid object type" in result.content


@pytest.mark.asyncio
async def test_search_requires_query(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "search_atlas", query="   ")
    assert "query is required" in result.content


# ---------------------------------------------------------------------------
# Mitigations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_mitigations_filters(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)

    result = await _call(executor, "list_atlas_mitigations", category="Policy")
    assert "[AML.M0000] Limit Public Release of Information" in result.content
    assert "mitigates 1 technique(s)" in result.content

    result = await _call(
        executor, "list_atlas_mitigations", category="Technical - AI"
    )
    assert "No ATLAS mitigations match" in result.content
    assert "Policy, Technical - AI, Technical - Cyber" in result.content

    result = await _call(
        executor,
        "list_atlas_mitigations",
        lifecycle_phase="Business and Data Understanding",
    )
    assert "AML.M0000" in result.content

    result = await _call(
        executor, "list_atlas_mitigations", technique_id="AML.T0005"
    )
    assert "AML.M0000" in result.content

    result = await _call(
        executor, "list_atlas_mitigations", technique_id="AML.T0040"
    )
    assert "No ATLAS mitigations match" in result.content


@pytest.mark.asyncio
async def test_get_mitigation_shows_mitigated_techniques_with_use(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "get_atlas_mitigation", mitigation_id="M0000")
    content = result.content
    assert "[AML.M0000] Limit Public Release of Information (mitigation)" in content
    assert "https://atlas.mitre.org/mitigations/AML.M0000" in content
    assert "Categories: Policy" in content
    assert "[AML.T0005] Create Proxy AI Model" in content
    # the per-edge use description surfaces
    assert "Limit the connection between public info" in content


@pytest.mark.asyncio
async def test_get_mitigation_rejects_invalid_id(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(
        executor, "get_atlas_mitigation", mitigation_id="AML.T0005"
    )
    assert "not a valid ATLAS mitigation id" in result.content


# ---------------------------------------------------------------------------
# Case studies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_case_studies_filters(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)

    result = await _call(executor, "list_atlas_case_studies", case_type="Exercise")
    assert "AML.CS0000" in result.content
    assert "2 attack step(s)" in result.content
    # Proper singular/plural - never the "case stud(ies)" placeholder form.
    assert "Found 1 ATLAS case study (newest first)" in result.content
    assert "stud(ies)" not in result.content

    result = await _call(executor, "list_atlas_case_studies", case_type="Incident")
    assert "No ATLAS case studies match" in result.content

    result = await _call(executor, "list_atlas_case_studies", case_type="banana")
    assert "not a valid case study type" in result.content

    result = await _call(
        executor, "list_atlas_case_studies", technique_id="AML.T0005"
    )
    assert "AML.CS0000" in result.content

    result = await _call(executor, "list_atlas_case_studies", keyword="palo alto")
    assert "AML.CS0000" in result.content

    result = await _call(executor, "list_atlas_case_studies", since_year=2021)
    assert "No ATLAS case studies match" in result.content

    result = await _call(executor, "list_atlas_case_studies", since_year=2019)
    assert "AML.CS0000" in result.content


@pytest.mark.asyncio
async def test_get_case_study_orders_attack_chain(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "get_atlas_case_study", case_study_id="CS0000")
    content = result.content
    assert "[AML.CS0000] Evasion of Deep Learning Detector" in content
    assert "https://atlas.mitre.org/studies/AML.CS0000" in content
    assert "Actor: Palo Alto Networks AI Research Team" in content
    assert "Attack chain (2 steps):" in content
    # steps ordered by step-id, each with technique + tactic context
    s00 = content.index("S00: [AML.T0005] Create Proxy AI Model (AI Attack Staging)")
    s01 = content.index("S01: [AML.T0040] AI Model Inference API Access (Reconnaissance)")
    assert s00 < s01
    assert "Trained a proxy model." in content


@pytest.mark.asyncio
async def test_get_case_study_rejects_invalid_id(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(
        executor, "get_atlas_case_study", case_study_id="AML.T0005"
    )
    assert "not a valid ATLAS case-study id" in result.content


# ---------------------------------------------------------------------------
# Relationship traversal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_related_objects_both_directions(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(
        executor, "get_atlas_related_objects", object_id="AML.T0005"
    )
    content = result.content
    # outgoing: achieves the tactic
    assert "--achieves--> [AML.TA0001] AI Attack Staging (tactic)" in content
    # incoming: specialized by the sub-technique, mitigated, employed
    assert "<--specializes-- [AML.T0005.000]" in content
    assert "<--mitigates-- [AML.M0000]" in content
    assert "<--employs-- [AML.CS0000]" in content


@pytest.mark.asyncio
async def test_related_objects_type_filter(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(
        executor,
        "get_atlas_related_objects",
        object_id="AML.T0005",
        relationship_type="mitigates",
    )
    content = result.content
    assert "<--mitigates-- [AML.M0000]" in content
    assert "achieves" not in content.split("Relationship semantics")[0]

    result = await _call(
        executor,
        "get_atlas_related_objects",
        object_id="AML.T0005",
        relationship_type="banana",
    )
    assert "not a valid relationship type" in result.content


@pytest.mark.asyncio
async def test_related_objects_unknown_id(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(
        executor, "get_atlas_related_objects", object_id="AML.T9999"
    )
    assert "was not found in ATLAS release" in result.content


# ---------------------------------------------------------------------------
# ATT&CK crosswalk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_map_attack_to_atlas_hit(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "map_attack_to_atlas", attack_id="t1595")
    content = result.content
    assert "ATT&CK T1595 maps to 1 ATLAS object(s)" in content
    assert "[AML.T0040] AI Model Inference API Access" in content
    assert "https://atlas.mitre.org/techniques/AML.T0040" in content


@pytest.mark.asyncio
async def test_map_attack_to_atlas_miss_and_invalid(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "map_attack_to_atlas", attack_id="T9999")
    assert "No ATLAS object references ATT&CK T9999" in result.content

    result = await _call(executor, "map_attack_to_atlas", attack_id="AML.T0005")
    assert "not a valid MITRE ATT&CK id" in result.content


@pytest.mark.asyncio
async def test_map_attack_to_atlas_lists_all_mappings(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "map_attack_to_atlas")
    content = result.content
    assert "All ATT&CK -> ATLAS mappings" in content
    assert "T1595 -> [AML.T0040]" in content
    assert "TA0043 -> [AML.TA0002]" in content


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reports_counts_and_redacts_custom_mirror(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "get_atlas_status")
    content = result.content
    assert "release 2026.06" in content
    assert "2 tactics" in content
    assert "3 techniques" in content
    # Singular counts render singular nouns (never "1 mitigations").
    assert "1 mitigation," in content
    assert "1 case study" in content
    assert "1 mitigations" not in content
    assert "1 case studies" not in content
    # Unique ATLAS objects across the crosswalk (TA0043 -> AML.TA0002,
    # T1595 -> AML.T0040), never per-mapping pair counts.
    assert "ATT&CK-mapped objects: 2" in content
    # A configured mirror may be internal infrastructure - the URL must
    # never appear in agent-visible output.
    assert _SOURCE_URL not in content
    assert "custom mirror" in content
    assert "https://atlas.mitre.org" in content


@pytest.mark.asyncio
async def test_status_shows_official_source_url(monkeypatch):
    recorder = _Recorder(_yaml_router)
    monkeypatch.setattr(atlas_mod, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token="", executor=executor)  # resolves to the official URL
    result = await _call(executor, "get_atlas_status")
    assert ATLAS_DEFAULT_SOURCE_URL in result.content
    assert "custom mirror" not in result.content


@pytest.mark.asyncio
async def test_status_reports_failed_forced_refresh(monkeypatch):
    calls = {"n": 0}

    def flaky_router(url: str) -> str | None:
        calls["n"] += 1
        return _SAMPLE_YAML if calls["n"] == 1 else None

    executor, _ = _build(monkeypatch, flaky_router)
    first = await _call(executor, "get_atlas_status")
    assert "just refreshed" in first.content
    # Forced refresh hits the (now failing) upstream and falls back to the
    # cached release - the status must say so instead of "just refreshed".
    result = await _call(executor, "get_atlas_status", refresh=True)
    assert not result.is_error
    assert "refresh FAILED" in result.content
    assert "release 2026.06" in result.content


@pytest.mark.asyncio
async def test_failed_refresh_backs_off_before_retrying(monkeypatch):
    calls = {"n": 0}

    def flaky_router(url: str) -> str | None:
        calls["n"] += 1
        return _SAMPLE_YAML if calls["n"] == 1 else None

    executor, recorder = _build(monkeypatch, flaky_router)
    await _call(executor, "get_atlas_matrix")
    assert len(recorder.calls) == 1

    # Expire the TTL: the next call attempts a refresh, fails, serves the
    # stale index, and arms the retry back-off.
    index = atlas_mod._CACHE[_SOURCE_URL]
    index.fetched_at -= atlas_mod._CACHE_TTL_SECONDS + 1
    result = await _call(executor, "get_atlas_matrix")
    assert not result.is_error
    assert "release 2026.06" in result.content
    assert len(recorder.calls) == 2
    assert index.retry_after > 0

    # While the back-off is armed, further calls serve stale WITHOUT
    # hitting the upstream source again.
    result = await _call(executor, "list_atlas_tactics")
    assert not result.is_error
    assert len(recorder.calls) == 2

    # An explicit forced refresh bypasses the back-off...
    result = await _call(executor, "get_atlas_status", refresh=True)
    assert "refresh FAILED" in result.content
    assert len(recorder.calls) == 3

    # ...and once the back-off deadline passes, implicit refreshes resume.
    index.retry_after = 0.0
    await _call(executor, "get_atlas_matrix")
    assert len(recorder.calls) == 4


@pytest.mark.asyncio
async def test_stale_served_when_refresh_returns_bad_document(monkeypatch):
    calls = {"n": 0}

    def degrading_router(url: str) -> str | None:
        calls["n"] += 1
        return _SAMPLE_YAML if calls["n"] == 1 else "just: some\nrandom: yaml"

    executor, recorder = _build(monkeypatch, degrading_router)
    await _call(executor, "get_atlas_matrix")
    index = atlas_mod._CACHE[_SOURCE_URL]
    index.fetched_at -= atlas_mod._CACHE_TTL_SECONDS + 1

    # The refresh fetches a document that fails validation - the stale
    # index is served (with back-off) instead of surfacing an error.
    result = await _call(executor, "get_atlas_matrix")
    assert not result.is_error
    assert "release 2026.06" in result.content
    assert len(recorder.calls) == 2
    assert index.retry_after > 0


@pytest.mark.asyncio
async def test_oversized_dataset_is_rejected(monkeypatch):
    monkeypatch.setattr(atlas_mod, "_MAX_DATASET_BYTES", 64)
    executor, _ = _build(monkeypatch, _yaml_router)
    result = await _call(executor, "get_atlas_matrix")
    assert "far larger than any ATLAS release" in result.content


@pytest.mark.asyncio
async def test_malformed_section_shapes_are_rejected(monkeypatch):
    # A section with the wrong container type must fail the (index, err)
    # contract with a user-facing error - never raise out of _build_index().
    bad_tactics = yaml.safe_dump(
        {"format-version": "6.0.0", "tactics": ["not", "a", "mapping"]}
    )
    executor, _ = _build(monkeypatch, lambda url: bad_tactics)
    result = await _call(executor, "get_atlas_matrix")
    assert "malformed dataset" in result.content
    assert "'tactics' is not a mapping" in result.content

    bad_entries = yaml.safe_dump(
        {
            "format-version": "6.0.0",
            "tactics": {"AML.TA0001": {"id": "AML.TA0001"}},
            "techniques": {"AML.T0005": "just a string"},
        }
    )
    atlas_mod._CACHE.clear()
    executor, _ = _build(monkeypatch, lambda url: bad_entries)
    result = await _call(executor, "get_atlas_matrix")
    assert "'techniques' contains non-mapping entries" in result.content


@pytest.mark.asyncio
async def test_stale_served_when_refresh_returns_malformed_sections(monkeypatch):
    calls = {"n": 0}

    def degrading_router(url: str) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return _SAMPLE_YAML
        return yaml.safe_dump({"format-version": "6.0.0", "tactics": []})

    executor, _ = _build(monkeypatch, degrading_router)
    await _call(executor, "get_atlas_matrix")
    index = atlas_mod._CACHE[_SOURCE_URL]
    index.fetched_at -= atlas_mod._CACHE_TTL_SECONDS + 1

    result = await _call(executor, "get_atlas_matrix")
    assert not result.is_error
    assert "release 2026.06" in result.content
    assert index.retry_after > 0


@pytest.mark.asyncio
async def test_connectivity_tolerates_non_dict_extra(monkeypatch):
    # extra_config is decoded with a bare json.loads by the caller - a
    # non-object JSON document (list/scalar) must not crash the
    # never-raises contract of test_provider_connectivity.
    from app.api.v1.integration_testing import test_provider_connectivity

    recorder = _Recorder(_yaml_router)
    monkeypatch.setattr(atlas_mod, "safe_request", recorder)
    result = await test_provider_connectivity(
        "mitre_atlas", "", ["not", "a", "dict"], "MITRE ATLAS"
    )
    assert result["status"] == "ok"
    assert "release 2026.06" in result["message"]


def test_catalog_category_is_a_known_frontend_category():
    # The marketplace groups providers by integration_category - a value
    # missing from the frontend taxonomy lands in the unlabelled "Other"
    # group. MITRE ATLAS belongs under Threat Intelligence (like MISP).
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    entry = PROVIDER_CATALOG["mitre_atlas"]
    assert entry["integration_category"] == "threat_intelligence"
    assert entry["auth_type"] == "none"


# ---------------------------------------------------------------------------
# Pagination: out-of-range offsets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_range_offset_reports_pagination_not_no_match(monkeypatch):
    executor, _ = _build(monkeypatch, _yaml_router)

    result = await _call(executor, "list_atlas_techniques", offset=50)
    # The hint reports the offset the caller actually passed, not a value
    # clamped to the total.
    assert "No more results at offset=50" in result.content
    assert "only 3 result(s) match" in result.content
    assert "No ATLAS techniques match" not in result.content

    result = await _call(executor, "search_atlas", query="proxy", offset=50)
    assert "No more results at offset=" in result.content
    assert "No ATLAS objects match" not in result.content

    result = await _call(executor, "list_atlas_mitigations", offset=50)
    assert "No more results at offset=" in result.content

    result = await _call(executor, "list_atlas_case_studies", offset=50)
    assert "No more results at offset=" in result.content

    result = await _call(executor, "list_atlas_tactics", offset=50)
    assert "No more results at offset=" in result.content

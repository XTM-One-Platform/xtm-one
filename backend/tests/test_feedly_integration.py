"""Tests for the Feedly integration provider.

Covers the credential chokepoint (Bearer header building + blank-token
guard), tool registration, article formatting, streams pagination, the power
search body construction, and input validation.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import feedly
from app.integrations.providers.feedly import (
    FeedlyIntegration,
    build_feedly_headers,
    build_tools,
)
from app.services.integration_tools import _SafeIntegrationExecutor

_TOKEN = "fe_test_token"


class _FakeResp:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _build(token: str = _TOKEN):
    executor = _SafeIntegrationExecutor()
    tools = build_tools(token, executor)
    return executor._handlers, tools


def _capture(monkeypatch, payload=None, status: int = 200) -> dict:
    """Patch safe_request to capture call args and return a canned response."""
    captured: dict = {}

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        return _FakeResp(status, payload), None

    monkeypatch.setattr(feedly, "safe_request", _fake_safe_request)
    return captured


# ---------------------------------------------------------------------------
# Credential chokepoint
# ---------------------------------------------------------------------------


def test_headers_strip_whitespace_and_use_bearer():
    headers = build_feedly_headers("  fe_token \n")
    assert headers["Authorization"] == "Bearer fe_token"
    assert headers["Accept"] == "application/json"


def test_blank_token_builds_no_tools():
    handlers, tools = _build("   ")
    assert tools == []
    assert handlers == {}


def test_provider_class_contract():
    integ = FeedlyIntegration()
    assert integ.provider_name == "feedly"
    assert integ.auth_types == ["api_key"]
    assert integ.get_oauth_defaults() is None


def test_all_tools_registered():
    handlers, tools = _build()
    names = {t.name for t in tools}
    assert names == set(handlers)
    assert names == {
        "get_feedly_profile",
        "list_feedly_folders",
        "list_feedly_boards",
        "list_feedly_ai_feeds",
        "get_feedly_stream_articles",
        "get_feedly_article",
        "search_feedly_articles",
        "search_feedly_entities",
        "lookup_feedly_entity",
        "get_feedly_cve",
        "get_feedly_entities",
        "get_feedly_entity_relationships",
        "get_feedly_stream_iocs",
        "get_feedly_trending_threats",
        "query_feedly_vulnerability_dashboard",
        "query_feedly_cyberattacks",
        "get_feedly_emerging_trends",
        "ask_feedly_ai",
        "list_feedly_team_users",
        "add_feedly_article_to_board",
        "annotate_feedly_article",
        "remove_feedly_article_from_board",
    }
    by_name = {t.name: t for t in tools}
    assert by_name["get_feedly_stream_articles"].priority == 0
    assert by_name["get_feedly_cve"].priority == 0
    # Writes sort after reads; destructive ops last.
    assert by_name["add_feedly_article_to_board"].priority == 1
    assert by_name["annotate_feedly_article"].priority == 1
    assert by_name["remove_feedly_article_from_board"].priority == 2
    # The status probe sorts after the read tools.
    assert by_name["get_feedly_profile"].priority == 2


def test_schemas_accept_case_insensitive_enum_inputs():
    """Pre-dispatch schema validation must not reject inputs the handlers accept.

    The agent loop validates tool arguments against ``input_schema`` BEFORE
    dispatch, so any value the runtime normalizers accept (lowercase severity,
    interval, growth stage, size) must also pass the schema - otherwise the
    tool never runs for inputs the implementation explicitly supports.
    """
    from app.tools.agent_loop import validate_tool_arguments

    _, tools = _build()
    by_name = {t.name: t for t in tools}
    for tool_name, args in (
        ("search_feedly_articles", {"query": "x", "severity": "high"}),
        (
            "get_feedly_entity_relationships",
            {
                "entity_type": "Actor",
                "entity_id": "nlp/f/entity/gz:ta:x",
                "interval": "last_30_days",
            },
        ),
        (
            "get_feedly_emerging_trends",
            {"growth_stages": ["exploding"], "sizes": ["known"]},
        ),
        ("get_feedly_trending_threats", {"kind": "Threat_Actors"}),
        (
            "query_feedly_vulnerability_dashboard",
            {"sort_order": "DESC"},
        ),
    ):
        feedback = validate_tool_arguments(
            tool_name, args, by_name[tool_name].input_schema
        )
        assert feedback is None, f"{tool_name} rejected valid input: {feedback}"


# ---------------------------------------------------------------------------
# Stream discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_folders_formats_stream_ids(monkeypatch):
    captured = _capture(
        monkeypatch,
        [
            {
                "id": "enterprise/acme/category/fa84365d-6433-400e-bbbf-9d3c010889c4",
                "label": "CTI Vendor Reports",
                "feeds": [
                    {"title": "Check Point Blog"},
                    {"title": "Unit 42"},
                ],
            }
        ],
    )
    handlers, _ = _build()
    result = await handlers["list_feedly_folders"]()

    assert captured["url"].endswith("/v3/enterprise/collections")
    assert captured["headers"]["Authorization"] == f"Bearer {_TOKEN}"
    assert "CTI Vendor Reports" in result
    assert "enterprise/acme/category/fa84365d-6433-400e-bbbf-9d3c010889c4" in result
    assert "2 feed(s)" in result


@pytest.mark.asyncio
async def test_list_boards_and_ai_feeds(monkeypatch):
    captured = _capture(
        monkeypatch,
        [
            {
                "id": "enterprise/acme/tag/002820c3-918f-4052-b5c9-595091c2e419",
                "label": "Escalations",
                "totalEntries": 4,
                "lastEntryAdded": 1737736477703,
            }
        ],
    )
    handlers, _ = _build()
    result = await handlers["list_feedly_boards"]()
    assert captured["url"].endswith("/v3/enterprise/tags")
    assert "Escalations" in result
    assert "enterprise/acme/tag/002820c3-918f-4052-b5c9-595091c2e419" in result

    captured = _capture(
        monkeypatch,
        {
            "userAlerts": [],
            "enterpriseAlerts": [
                {
                    "label": "Ransomware Watch",
                    "feedId": "feed/https://feedly.com/f/alert/fbe3c317",
                    "velocity": 4.1,
                    "layers": [
                        {
                            "type": "matches",
                            "parts": [{"text": "ransomware"}],
                            "salience": "about",
                        }
                    ],
                }
            ],
        },
    )
    result = await handlers["list_feedly_ai_feeds"]()
    assert captured["url"].endswith("/v3/alerts/")
    assert "Ransomware Watch" in result
    assert "feed/https://feedly.com/f/alert/fbe3c317" in result
    assert "ransomware" in result


# ---------------------------------------------------------------------------
# Content reading
# ---------------------------------------------------------------------------


_ARTICLE = {
    "id": "abc+def=_17ee7d2facc:14ddcbb:aa31659c",
    "title": "Threat actors target VMware Horizon servers",
    "published": 1649805845596,
    "canonicalUrl": "https://example.com/post",
    "origin": {"title": "Example Blog"},
    "leoSummary": {"sentences": [{"text": "Attackers exploit Log4Shell."}]},
    "entities": [
        {"label": "Log4Shell", "type": "vulnerability"},
        {"label": "Cobalt Strike", "type": "malwareFamily"},
    ],
}


@pytest.mark.asyncio
async def test_stream_articles_paginates_and_formats(monkeypatch):
    captured = _capture(
        monkeypatch,
        {"items": [_ARTICLE], "continuation": "tok123"},
    )
    handlers, _ = _build()
    result = await handlers["get_feedly_stream_articles"](
        stream_id="enterprise/acme/category/uuid-1", count=250
    )

    assert captured["url"].endswith("/v3/streams/contents")
    assert captured["params"]["streamId"] == "enterprise/acme/category/uuid-1"
    # count is clamped to the API maximum of 100.
    assert captured["params"]["count"] == 100
    assert "VMware Horizon" in result
    assert "Example Blog" in result
    assert "abc+def=_17ee7d2facc:14ddcbb:aa31659c" in result
    assert "Log4Shell" in result
    assert 'continuation="tok123"' in result


@pytest.mark.asyncio
async def test_stream_articles_requires_stream_id(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run without a stream_id")

    monkeypatch.setattr(feedly, "safe_request", _must_not_call)
    handlers, _ = _build()
    assert "Error" in await handlers["get_feedly_stream_articles"](stream_id="  ")


@pytest.mark.asyncio
async def test_get_article_encodes_entry_id_and_reports_404(monkeypatch):
    captured = _capture(monkeypatch, [_ARTICLE])
    handlers, _ = _build()
    result = await handlers["get_feedly_article"](
        entry_id="abc+def=_17ee7d2facc:14ddcbb:aa31659c"
    )
    # Entry ids embed +, = and : - the path segment must be fully encoded.
    assert "/v3/entries/abc%2Bdef%3D_17ee7d2facc%3A14ddcbb%3Aaa31659c" in captured["url"]
    assert "VMware Horizon" in result

    async def _fake_404(client, method, url, provider, action, **kwargs):
        return _FakeResp(404, {}), "Feedly error 404"

    monkeypatch.setattr(feedly, "safe_request", _fake_404)
    result = await handlers["get_feedly_article"](entry_id="missing-id")
    assert "No article found" in result


@pytest.mark.asyncio
async def test_search_builds_layer_body(monkeypatch):
    captured = _capture(monkeypatch, {"items": [_ARTICLE]})
    handlers, _ = _build()
    result = await handlers["search_feedly_articles"](
        query="supply chain attack",
        entity_ids=["nlp/f/entity/gz:org:microsoft"],
        stream_ids=["enterprise/acme/category/uuid-1"],
        severity="high",
        language="en",
        newer_than_hours=24,
    )

    assert captured["method"] == "post"
    assert captured["url"].endswith("/v3/search/contents")
    body = captured["json"]
    matches = body["layers"][0]
    assert matches["type"] == "matches"
    assert {"id": "nlp/f/entity/gz:org:microsoft"} in matches["parts"]
    assert {"text": "supply chain attack"} in matches["parts"]
    assert {"type": "security", "severities": ["HIGH"]} in body["layers"]
    assert {"type": "language", "languages": ["en"]} in body["layers"]
    assert body["source"]["items"] == [
        {"type": "stream", "id": "enterprise/acme/category/uuid-1"}
    ]
    assert "newerThan" in captured["params"]
    assert "VMware Horizon" in result


@pytest.mark.asyncio
async def test_search_validates_inputs(monkeypatch):
    async def _must_not_call(*args, **kwargs):
        raise AssertionError("safe_request must not run for invalid input")

    monkeypatch.setattr(feedly, "safe_request", _must_not_call)
    handlers, _ = _build()
    assert "Error" in await handlers["search_feedly_articles"]()
    assert "severity" in await handlers["search_feedly_articles"](
        query="x", severity="EXTREME"
    )


# ---------------------------------------------------------------------------
# Threat intelligence - entities & insight cards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_entities_formats_ids(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "entities": [
                {
                    "id": "nlp/f/entity/gz:ta:d9e5be22-1a04-4956-af6c-37af02330980",
                    "label": "LAPSUS",
                    "type": "threatActor",
                }
            ]
        },
    )
    handlers, _ = _build()
    result = await handlers["search_feedly_entities"](query="lapsus")
    assert captured["url"].endswith("/v3/search/entities")
    assert captured["params"]["query"] == "lapsus"
    assert "LAPSUS" in result
    assert "nlp/f/entity/gz:ta:d9e5be22-1a04-4956-af6c-37af02330980" in result

    # A bare-list payload (no envelope) must not crash the formatter.
    _capture(
        monkeypatch,
        [{"id": "nlp/f/entity/gz:mal:x", "label": "Kinsing", "type": "malwareFamily"}],
    )
    result = await handlers["search_feedly_entities"](query="kinsing")
    assert "Kinsing" in result

    assert "Error" in await handlers["search_feedly_entities"](query="  ")


_CVE_CARD = {
    "cveid": "CVE-2021-26084",
    "description": "OGNL injection vulnerability in Confluence Server.",
    "trending": True,
    "cvssV3": {"baseScore": 9.8, "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"},
    "cvssCategoryEstimate": "HIGH",
    "epssScore": "0.32908",
    "patched": True,
    "exploits": ["http://example.com/exploit"],
    "proofOfExploits": ["http://example.com/itw"],
    "vulnerableProducts": {"vendor": "atlassian", "products": ["confluence"]},
    "relationships": [
        {
            "threat": {
                "id": "nlp/f/entity/gz:mal:ef0e3a56",
                "label": "Kinsing",
                "type": "malwareFamily",
            }
        }
    ],
}


@pytest.mark.asyncio
async def test_get_cve_formats_insight_card(monkeypatch):
    captured = _capture(monkeypatch, _CVE_CARD)
    handlers, _ = _build()
    result = await handlers["get_feedly_cve"](cve_id="cve-2021-26084", with_stats=True)
    # CVE id is uppercased and used as the entity path.
    assert "/v3/entities/CVE-2021-26084" in captured["url"]
    assert captured["params"] == {"withStats": "true"}
    assert "CVE-2021-26084" in result
    assert "TRENDING" in result
    assert "CVSS v3 9.8" in result
    assert "EPSS 0.32908" in result
    assert "exploited in the wild" in result
    assert "patch available" in result
    assert "Kinsing" in result

    assert "Error" in await handlers["get_feedly_cve"](cve_id="not-a-cve")


@pytest.mark.asyncio
async def test_lookup_entity_encodes_id_and_reports_404(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "id": "nlp/f/entity/gz:mal:ef0e3a56",
            "label": "Kinsing",
            "type": "malwareFamily",
            "aliases": ["kinsing-malware"],
            "description": "Cryptojacking malware.",
        },
    )
    handlers, _ = _build()
    result = await handlers["lookup_feedly_entity"](
        entity_id="nlp/f/entity/gz:mal:ef0e3a56"
    )
    assert "/v3/entities/nlp%2Ff%2Fentity%2Fgz%3Amal%3Aef0e3a56" in captured["url"]
    assert "Kinsing" in result
    assert "kinsing-malware" in result

    async def _fake_404(client, method, url, provider, action, **kwargs):
        return _FakeResp(404, {}), "Feedly error 404"

    monkeypatch.setattr(feedly, "safe_request", _fake_404)
    assert "No Feedly entity found" in await handlers["lookup_feedly_entity"](
        entity_id="nlp/f/entity/missing"
    )


@pytest.mark.asyncio
async def test_bulk_entities_validates_and_posts_ids(monkeypatch):
    captured = _capture(monkeypatch, [_CVE_CARD])
    handlers, _ = _build()
    result = await handlers["get_feedly_entities"](
        entity_ids=["CVE-2021-26084", " "]
    )
    assert captured["url"].endswith("/v3/entities/.mget")
    assert captured["json"] == ["CVE-2021-26084"]
    assert "CVE-2021-26084" in result

    assert "Error" in await handlers["get_feedly_entities"](entity_ids=[])
    assert "at most 100" in await handlers["get_feedly_entities"](
        entity_ids=[f"CVE-2024-{i}" for i in range(101)]
    )


@pytest.mark.asyncio
async def test_entity_relationships_builds_url_and_formats_ttps(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "actors": [
                {
                    "count": 3,
                    "entity": {
                        "id": "nlp/f/entity/gz:ta:78bf726c",
                        "label": "MUSTANG PANDA",
                        "type": "threatActor",
                    },
                }
            ],
            "ttps": [
                {
                    "mitreId": "T1105",
                    "technique": "Ingress Tool Transfer",
                    "trend": "stable",
                    "mitigations": [
                        {"mitreId": "M1031", "label": "Network Intrusion Prevention"}
                    ],
                }
            ],
        },
    )
    handlers, _ = _build()
    result = await handlers["get_feedly_entity_relationships"](
        entity_type="malware",
        entity_id="nlp/f/entity/gz:mal:fd035735",
        interval="last_30_days",
    )
    assert "/v3/ml/relationships/malware/nlp%2Ff%2Fentity%2Fgz%3Amal%3Afd035735" in (
        captured["url"]
    )
    assert captured["params"]["intervalType"] == "LAST_30_DAYS"
    assert "MUSTANG PANDA" in result
    assert "T1105" in result
    assert "M1031" in result

    assert "Error" in await handlers["get_feedly_entity_relationships"](
        entity_type="campaign", entity_id="x"
    )
    assert "Error" in await handlers["get_feedly_entity_relationships"](
        entity_type="actor", entity_id="x", interval="ALWAYS"
    )


# ---------------------------------------------------------------------------
# Threat intelligence - IoCs, landscape & dashboards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_iocs_summarizes_stix_and_uses_link_header(monkeypatch):
    captured: dict = {}

    class _RespWithLink(_FakeResp):
        headers = {
            "Link": (
                "<https://api.feedly.com/v3/enterprise/ioc?streamId=x"
                "&continuation=tok456&count=20>; rel=next"
            )
        }

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return (
            _RespWithLink(
                200,
                {
                    "type": "bundle",
                    "objects": [
                        {"type": "report", "name": "Log4Shell campaign",
                         "external_references": [{"url": "https://example.com/post"}]},
                        {"type": "ipv4-addr", "value": "140.246.171.141"},
                        {"type": "domain-name", "value": "b.oracleservice.top"},
                        {
                            "type": "indicator",
                            "pattern": "[file:hashes.MD5 = '29bc15a6f0ff99084e986c3e6ab1208c']",
                        },
                    ],
                },
            ),
            None,
        )

    monkeypatch.setattr(feedly, "safe_request", _fake_safe_request)
    handlers, _ = _build()
    result = await handlers["get_feedly_stream_iocs"](
        stream_id="enterprise/acme/category/uuid-1"
    )
    assert captured["url"].endswith("/v3/enterprise/ioc")
    assert captured["params"]["streamId"] == "enterprise/acme/category/uuid-1"
    assert "newerThan" in captured["params"]
    assert "140.246.171.141 (ipv4-addr)" in result
    assert "b.oracleservice.top" in result
    assert "Log4Shell campaign" in result
    # Continuation comes from the Link response header (rel=next).
    assert 'continuation="tok456"' in result

    assert "Error" in await handlers["get_feedly_stream_iocs"](stream_id="")


def test_parse_link_continuation_matches_rel_next_only():
    parse = feedly._parse_link_continuation
    # Bare and quoted rel=next, with URL-decoding via proper query parsing.
    assert parse("<https://x/ioc?continuation=abc&count=20>; rel=next") == "abc"
    assert parse('<https://x/ioc?continuation=abc>; rel="next"') == "abc"
    # Multi-segment header: only the rel=next segment wins.
    assert (
        parse(
            "<https://x/ioc?continuation=old>; rel=prev, "
            "<https://x/ioc?continuation=new>; rel=next"
        )
        == "new"
    )
    # A URL merely containing "next" in a non-next segment must not match.
    assert parse("<https://x/next-batch?continuation=bad>; rel=prev") == ""
    # Space-separated relation lists per RFC 8288.
    assert parse('<https://x/ioc?continuation=abc>; rel="last next"') == "abc"
    assert parse("") == ""
    assert parse("<https://x/ioc>; rel=next") == ""


@pytest.mark.asyncio
async def test_trending_threats_routes_by_kind(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "memes": [
                {
                    "rank": 1,
                    "label": "CVE-2021-21551",
                    "summary": ["Dell driver privilege escalation."],
                    "items": [{"id": "e1"}, {"id": "e2"}],
                    "vulnerabilitiesMetadata": {"cvss": 7.5, "vendors": ["dell"]},
                }
            ]
        },
    )
    handlers, _ = _build()
    result = await handlers["get_feedly_trending_threats"](kind="vulnerabilities")
    assert captured["url"].endswith("/v3/memes/vulnerabilities/en")
    assert "CVE-2021-21551" in result
    assert "CVSS 7.5" in result

    captured = _capture(
        monkeypatch,
        {
            "items": [
                {
                    "actor": {"id": "nlp/f/entity/gz:ta:d9e5be22", "label": "LAPSUS"},
                    "entryIds": ["a", "b"],
                }
            ]
        },
    )
    result = await handlers["get_feedly_trending_threats"](kind="threat_actors")
    assert captured["url"].endswith("/v3/trends/threat-actors")
    assert "LAPSUS" in result
    assert "2 related article(s)" in result

    captured = _capture(
        monkeypatch,
        {
            "items": [
                {
                    "new-malware": {
                        "id": "nlp/f/entity/gz:mal:abc123",
                        "label": "DarkGate",
                    },
                    "entryIds": ["x", "y", "z"],
                }
            ]
        },
    )
    result = await handlers["get_feedly_trending_threats"](kind="new_malware")
    assert captured["url"].endswith("/v3/trends/new-malwares")
    assert "DarkGate" in result
    assert "nlp/f/entity/gz:mal:abc123" in result
    assert "3 related article(s)" in result

    assert "Error" in await handlers["get_feedly_trending_threats"](kind="botnets")


@pytest.mark.asyncio
async def test_vulnerability_dashboard_builds_layers(monkeypatch):
    captured = _capture(
        monkeypatch,
        {"total": 1, "vulnerabilities": [_CVE_CARD], "continuation": "vtok"},
    )
    handlers, _ = _build()
    result = await handlers["query_feedly_vulnerability_dashboard"](
        period="Last30Days",
        trending=True,
        exploited=True,
        in_cisa_kev=True,
        cvss_min=9,
        sort_field="cvssScore",
    )
    assert captured["url"].endswith("/v3/trends/vulnerability-dashboard")
    body = captured["json"]
    assert {"filters": [{"field": "period", "value": {"type": "Last30Days"}}]} in (
        body["layers"]
    )
    # Boolean signals are OR'd inside one layer.
    assert {
        "filters": [
            {"field": "exploited", "value": True},
            {"field": "inCisaKev", "value": True},
        ]
    } in body["layers"]
    assert {"filters": [{"field": "trending", "value": True}]} in body["layers"]
    assert {"filters": [{"field": "cvssScore", "value": {"gte": 9.0}}]} in (
        body["layers"]
    )
    assert body["sort"] == {"field": "cvssScore", "order": "desc"}
    assert "CVE-2021-26084" in result
    assert 'continuation="vtok"' in result

    assert "Error" in await handlers["query_feedly_vulnerability_dashboard"](
        period="LastDecade"
    )
    assert "Error" in await handlers["query_feedly_vulnerability_dashboard"](
        sort_field="severity"
    )


@pytest.mark.asyncio
async def test_cyberattacks_builds_filters(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "attacks": [
                {
                    "victim": {"label": "Acme Hospital"},
                    "attackType": "Ransomware",
                    "threatActor": {"label": "LockBit"},
                    "victimCountry": "US",
                }
            ]
        },
    )
    handlers, _ = _build()
    result = await handlers["query_feedly_cyberattacks"](
        period="Last7Days",
        victim_country="us",
        attack_type="Ransomware",
    )
    assert captured["url"].endswith("/v3/ml/relationships/cyber-attacks/dashboard/table")
    body = captured["json"]
    assert body["period"] == {"type": "Last7Days"}
    assert {"filters": [{"field": "victimCountry", "value": "US"}]} in body["layers"]
    assert {"filters": [{"field": "attackType", "value": "Ransomware"}]} in (
        body["layers"]
    )
    assert "Acme Hospital" in result
    assert "LockBit" in result

    assert "Error" in await handlers["query_feedly_cyberattacks"](period="Forever")


@pytest.mark.asyncio
async def test_emerging_trends_validates_and_builds_body(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "trends": [
                {
                    "entity": {
                        "id": "nlp/f/entity/gz:topic:x",
                        "label": "Passkeys",
                        "description": "Passwordless authentication standard.",
                    },
                    "growth": "Exploding",
                    "size": "Known",
                }
            ]
        },
    )
    handlers, _ = _build()
    result = await handlers["get_feedly_emerging_trends"](
        growth_stages=["exploding"], sizes=["known"]
    )
    assert captured["url"].endswith("/v3/ml/trend-discovery/trends")
    assert captured["params"]["sort"] == "-growth"
    body = captured["json"]
    assert {"type": "growth", "values": ["Exploding"]} in body["filters"]
    assert {"type": "size", "values": ["Known"]} in body["filters"]
    assert body["period"] == "Last6Months"
    assert "Passkeys" in result
    assert "Exploding" in result

    assert "Error" in await handlers["get_feedly_emerging_trends"](
        growth_stages=["Skyrocketing"]
    )


@pytest.mark.asyncio
async def test_ask_ai_reports_missing_addon(monkeypatch):
    captured = _capture(
        monkeypatch,
        {
            "answer": "Scattered Spider is targeting aeronautics.",
            "items": [
                {
                    "id": "entry-1",
                    "title": "Aviation sector under attack",
                    "origin": {"title": "Example Blog"},
                }
            ],
        },
    )
    handlers, _ = _build()
    result = await handlers["ask_feedly_ai"](
        question="What threat actors are targeting aeronautics?",
        stream_ids=["enterprise/acme/category/uuid-1"],
    )
    assert captured["url"].endswith("/v3/search/rag")
    body = captured["json"]
    assert body["query"] == "What threat actors are targeting aeronautics?"
    assert body["source"]["items"] == [
        {"type": "stream", "id": "enterprise/acme/category/uuid-1"}
    ]
    assert "Scattered Spider" in result
    assert "Aviation sector under attack" in result

    async def _fake_403(client, method, url, provider, action, **kwargs):
        return _FakeResp(403, {}), "Feedly error 403"

    monkeypatch.setattr(feedly, "safe_request", _fake_403)
    result = await handlers["ask_feedly_ai"](question="anything")
    assert "Ask AI API" in result

    assert "Error" in await handlers["ask_feedly_ai"](question="  ")


@pytest.mark.asyncio
async def test_ask_ai_normalizes_entity_ids(monkeypatch):
    captured = _capture(monkeypatch, {"answer": "Patched in 2.17."})
    handlers, _ = _build()

    # Ask AI requires the full vulnerability path for CVEs - a bare CVE id
    # must be auto-converted (unlike /v3/entities/{id} which accepts both).
    await handlers["ask_feedly_ai"](
        question="Is Log4Shell patched?", entity_id="cve-2021-44228"
    )
    assert captured["json"]["entityIds"] == [
        "vulnerability/m/entity/CVE-2021-44228"
    ]

    # Non-CVE Feedly entity ids pass through untouched.
    await handlers["ask_feedly_ai"](
        question="What is Kinsing?", entity_id="nlp/f/entity/gz:mal:ef0e3a56"
    )
    assert captured["json"]["entityIds"] == ["nlp/f/entity/gz:mal:ef0e3a56"]


# ---------------------------------------------------------------------------
# Team + curation writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_team_users(monkeypatch):
    _capture(
        monkeypatch,
        [
            {
                "name": "Jane Analyst",
                "email": "jane@acme.com",
                "role": "admin",
                "isAdmin": True,
                "lastActivity": 1737658718941,
            }
        ],
    )
    handlers, _ = _build()
    result = await handlers["list_feedly_team_users"]()
    assert "Jane Analyst" in result
    assert "admin" in result


@pytest.mark.asyncio
async def test_board_add_and_remove_encode_paths(monkeypatch):
    captured = _capture(monkeypatch, {})
    handlers, _ = _build()
    board = "enterprise/acme/tag/002820c3-918f-4052-b5c9-595091c2e419"
    entry = "abc+def=_17ee7d2facc:14ddcbb:aa31659c"

    result = await handlers["add_feedly_article_to_board"](
        board_stream_id=board, entry_id=entry
    )
    assert captured["method"] == "put"
    assert "/v3/tags/enterprise%2Facme%2Ftag%2F002820c3" in captured["url"]
    assert captured["json"] == {"entryId": entry}
    assert "saved to board" in result

    captured = _capture(monkeypatch, {})
    result = await handlers["remove_feedly_article_from_board"](
        board_stream_id=board, entry_id=entry
    )
    assert captured["method"] == "delete"
    assert "/v3/tags/" in captured["url"]
    # Both path segments are URL-encoded.
    assert "abc%2Bdef%3D_17ee7d2facc%3A14ddcbb%3Aaa31659c" in captured["url"]
    assert "removed from board" in result

    assert "Error" in await handlers["add_feedly_article_to_board"](
        board_stream_id="", entry_id=entry
    )
    assert "Error" in await handlers["remove_feedly_article_from_board"](
        board_stream_id=board, entry_id=""
    )


@pytest.mark.asyncio
async def test_annotate_article_posts_comment(monkeypatch):
    captured = _capture(monkeypatch, {})
    handlers, _ = _build()
    result = await handlers["annotate_feedly_article"](
        entry_id="entry-1", comment="Confirmed relevant, escalating."
    )
    assert captured["url"].endswith("/v3/annotations")
    assert captured["json"] == {
        "entryId": "entry-1",
        "comment": "Confirmed relevant, escalating.",
    }
    assert "Annotation added" in result

    assert "Error" in await handlers["annotate_feedly_article"](
        entry_id="entry-1", comment=" "
    )


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_reports_identity(monkeypatch):
    _capture(
        monkeypatch,
        {
            "id": "user-1",
            "fullName": "Jane Analyst",
            "email": "jane@acme.com",
            "enterpriseName": "Acme CTI",
        },
    )
    handlers, _ = _build()
    result = await handlers["get_feedly_profile"]()
    assert "Jane Analyst" in result
    assert "jane@acme.com" in result
    assert "Acme CTI" in result

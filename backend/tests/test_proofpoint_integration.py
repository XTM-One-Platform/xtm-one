"""Regression tests for the Proofpoint TAP integration provider.

Pins the compound-token (principal||secret) HTTP Basic auth handling, the SIEM
time-window constraints (since_seconds clamped to the API's 1-hour maximum),
the consolidated event_type routing, the forensics threat/campaign XOR
validation, the People API window enum, the URL-decoder input validation, and
the path-traversal guards on id-interpolating tools.
"""

from __future__ import annotations

import base64

import pytest

from app.integrations.providers import proofpoint
from app.integrations.providers.proofpoint import (
    ProofpointIntegration,
    build_tools,
)
from app.services.integration_tools import _SafeIntegrationExecutor

# principal||secret (HTTP Basic compound token)
_TOKEN = "principal-uuid||secret-value"


class _FakeResp:
    """Minimal JSON response (routes through safe_request's request())."""

    def __init__(
        self,
        payload: object,
        status_code: int = 200,
        *,
        json_error: bool = False,
    ):
        self._payload = payload
        self._json_error = json_error
        self.status_code = status_code
        self.headers: dict = {}
        # Non-empty body marker so _api's empty-body short-circuit is not
        # taken (unless the payload is explicitly empty).
        self.content = b"x" if (payload or json_error) else b""

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> object:
        if self._json_error:
            raise ValueError("not JSON")
        return self._payload


class _FakeClient:
    """httpx.AsyncClient stand-in recording every dispatched request."""

    def __init__(
        self,
        payload: object,
        status_code: int = 200,
        *,
        json_error: bool = False,
    ):
        self._payload = payload
        self._status_code = status_code
        self._json_error = json_error
        self.requests: list[tuple[str, str, dict]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def _resp(self) -> _FakeResp:
        return _FakeResp(
            self._payload, self._status_code, json_error=self._json_error
        )

    async def request(self, method, url, **kwargs) -> _FakeResp:
        self.requests.append((str(method), str(url), kwargs))
        return self._resp()

    async def get(self, url, **kwargs) -> _FakeResp:
        self.requests.append(("GET", str(url), kwargs))
        return self._resp()


def _patch_client(
    monkeypatch,
    payload: object,
    status_code: int = 200,
    *,
    json_error: bool = False,
) -> dict:
    holder: dict = {}

    def _factory(**kw):
        holder["client"] = _FakeClient(
            payload, status_code, json_error=json_error
        )
        return holder["client"]

    monkeypatch.setattr(proofpoint.httpx, "AsyncClient", _factory)
    return holder


def _handlers() -> dict:
    """Build the provider tools and return the registered handler map."""
    executor = _SafeIntegrationExecutor()
    build_tools(_TOKEN, executor)
    return executor._handlers


# ---------------------------------------------------------------------------
# Token parsing + Basic auth header
# ---------------------------------------------------------------------------


def test_basic_auth_header_strips_whitespace():
    header = proofpoint._basic_auth_header(" principal \n", " secret ")
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode()
    assert decoded == "principal:secret"


def test_build_tools_returns_all_definitions():
    executor = _SafeIntegrationExecutor()
    defs = build_tools(_TOKEN, executor)
    names = {d.name for d in defs}
    assert names == {
        "list_proofpoint_siem_events",
        "list_proofpoint_campaigns",
        "get_proofpoint_campaign",
        "get_proofpoint_forensics",
        "get_proofpoint_threat",
        "list_proofpoint_vap",
        "list_proofpoint_top_clickers",
        "decode_proofpoint_urls",
    }
    # Every registered handler has a matching definition and vice versa.
    assert names == set(executor._handlers)
    # The whole surface is read-only.
    assert all(d.priority == 0 for d in defs)
    assert all(d.source == "integration:proofpoint" for d in defs)


# ---------------------------------------------------------------------------
# SIEM events — event_type routing + time-window clamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_siem_rejects_unknown_event_type():
    handler = _handlers()["list_proofpoint_siem_events"]
    result = await handler(event_type="bogus")
    assert "unknown event_type" in result
    # Lists the supported values so the LLM can self-correct.
    assert "clicks_blocked" in result


@pytest.mark.asyncio
async def test_siem_clamps_since_seconds_to_one_hour(monkeypatch):
    """The API allows at most 1 hour per request — larger values are clamped."""
    holder = _patch_client(monkeypatch, {})
    handler = _handlers()["list_proofpoint_siem_events"]
    await handler(event_type="all", since_seconds=86400)
    _method, url, kwargs = holder["client"].requests[0]
    assert url.endswith("/siem/all")
    params = kwargs.get("params") or {}
    assert params.get("sinceSeconds") == 3600
    assert params.get("format") == "json"


@pytest.mark.asyncio
async def test_siem_interval_overrides_since_seconds(monkeypatch):
    holder = _patch_client(monkeypatch, {})
    handler = _handlers()["list_proofpoint_siem_events"]
    interval = "2026-07-10T09:00:00Z/2026-07-10T10:00:00Z"
    await handler(event_type="clicks_blocked", interval=interval)
    _method, url, kwargs = holder["client"].requests[0]
    assert url.endswith("/siem/clicks/blocked")
    params = kwargs.get("params") or {}
    assert params.get("interval") == interval
    assert "sinceSeconds" not in params


@pytest.mark.asyncio
async def test_siem_whitespace_interval_falls_back_to_since_seconds(monkeypatch):
    """A whitespace-only interval must behave like 'not provided'."""
    holder = _patch_client(monkeypatch, {})
    handler = _handlers()["list_proofpoint_siem_events"]
    await handler(event_type="all", interval="   ", since_seconds=600)
    _method, _url, kwargs = holder["client"].requests[0]
    params = kwargs.get("params") or {}
    assert "interval" not in params
    assert params.get("sinceSeconds") == 600


@pytest.mark.asyncio
async def test_siem_400_appends_window_guidance(monkeypatch):
    """A 400 (typical window violation) must return actionable guidance."""
    _patch_client(monkeypatch, {}, status_code=400)
    handler = _handlers()["list_proofpoint_siem_events"]
    result = await handler(
        interval="2026-07-01T00:00:00Z/2026-07-02T00:00:00Z"  # > 1 hour
    )
    assert "bad request" in result
    assert "ONE HOUR" in result
    assert "7 days" in result


@pytest.mark.asyncio
async def test_siem_rejects_invalid_threat_filters():
    handler = _handlers()["list_proofpoint_siem_events"]
    result = await handler(threat_type="bogus")
    assert "threat_type must be" in result
    result = await handler(threat_status="bogus")
    assert "threat_status must be" in result


@pytest.mark.asyncio
async def test_siem_whitespace_filters_treated_as_not_provided(monkeypatch):
    """Whitespace-only threat filters behave like 'not provided'."""
    holder = _patch_client(monkeypatch, {})
    handler = _handlers()["list_proofpoint_siem_events"]
    result = await handler(threat_type="  ", threat_status="  ")
    assert "must be" not in result  # no enum rejection
    params = holder["client"].requests[0][2].get("params") or {}
    assert "threatType" not in params
    assert "threatStatus" not in params


@pytest.mark.asyncio
async def test_siem_renders_message_and_click_events(monkeypatch):
    _patch_client(
        monkeypatch,
        {
            "queryEndTime": "2026-07-10T10:00:00Z",
            "messagesBlocked": [
                {
                    "GUID": "guid-1",
                    "subject": "Totally safe invoice",
                    "fromAddress": "badguy@evil.zz",
                    "recipient": ["clark.kent@pharmtech.zz"],
                    "messageTime": "2026-07-10T09:30:00Z",
                    "malwareScore": 100,
                    "quarantineFolder": "Attachment Defense",
                    "quarantineRule": "module.sandbox.threat",
                    "threatsInfoMap": [
                        {
                            "threatId": "threat-abc",
                            "threatType": "ATTACHMENT",
                            "classification": "MALWARE",
                            "threatStatus": "active",
                            "threat": "2fab740f143f",
                            "campaignId": "campaign-1",
                        }
                    ],
                }
            ],
            "clicksPermitted": [
                {
                    "url": "http://badguy.zz/",
                    "classification": "MALWARE",
                    "clickTime": "2026-07-10T09:17:44Z",
                    "recipient": "bruce.wayne@pharmtech.zz",
                    "clickIP": "192.0.2.1",
                    "threatID": "threat-xyz",
                    "threatStatus": "active",
                }
            ],
        },
    )
    handler = _handlers()["list_proofpoint_siem_events"]
    result = await handler(event_type="all")
    assert "Found 2 TAP threat event(s)" in result
    assert "Totally safe invoice" in result
    assert "threat-abc" in result  # threat id surfaced for follow-up pivots
    assert "campaign-1" in result  # campaign id surfaced for follow-up pivots
    assert "http://badguy.zz/" in result
    assert "threat-xyz" in result
    assert "Attachment Defense" in result


@pytest.mark.asyncio
async def test_siem_empty_window_returns_guidance(monkeypatch):
    _patch_client(monkeypatch, {})
    handler = _handlers()["list_proofpoint_siem_events"]
    result = await handler(event_type="issues", since_seconds=600)
    assert "No TAP threat events found" in result


@pytest.mark.asyncio
async def test_non_json_success_response_surfaces_error(monkeypatch):
    """A 2xx with a garbled body is a real API problem, not an empty result."""
    _patch_client(monkeypatch, {}, json_error=True)
    handler = _handlers()["list_proofpoint_siem_events"]
    result = await handler(event_type="all")
    assert "non-JSON response" in result
    assert "No TAP threat events found" not in result


# ---------------------------------------------------------------------------
# Campaigns — interval requirement + traversal guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_campaigns_requires_interval():
    handler = _handlers()["list_proofpoint_campaigns"]
    result = await handler(interval="")
    assert "interval is required" in result


@pytest.mark.asyncio
async def test_list_campaigns_clamps_size_and_renders(monkeypatch):
    holder = _patch_client(
        monkeypatch,
        {
            "campaigns": [
                {
                    "id": "camp-1",
                    "lastUpdatedAt": "2026-07-10T08:00:00Z",
                    "notable": True,
                    "verticallyTargeted": False,
                }
            ]
        },
    )
    handler = _handlers()["list_proofpoint_campaigns"]
    result = await handler(
        interval="2026-07-09T00:00:00Z/2026-07-10T00:00:00Z", size=9999
    )
    assert "camp-1" in result
    assert "notable" in result
    _method, _url, kwargs = holder["client"].requests[0]
    assert (kwargs.get("params") or {}).get("size") == 200  # API max


@pytest.mark.asyncio
async def test_list_campaigns_rejects_non_integer_size_page():
    handler = _handlers()["list_proofpoint_campaigns"]
    interval = "2026-07-09T00:00:00Z/2026-07-10T00:00:00Z"
    result = await handler(interval=interval, size="lots")
    assert "size and page must be integers" in result
    result = await handler(interval=interval, page="two")
    assert "size and page must be integers" in result


@pytest.mark.asyncio
async def test_list_campaigns_400_appends_window_guidance(monkeypatch):
    _patch_client(monkeypatch, {}, status_code=400)
    handler = _handlers()["list_proofpoint_campaigns"]
    result = await handler(
        interval="2026-07-01T00:00:00Z/2026-07-05T00:00:00Z"  # > 1 day
    )
    assert "bad request" in result
    assert "ONE DAY" in result


@pytest.mark.asyncio
async def test_get_campaign_rejects_traversal_id():
    handler = _handlers()["get_proofpoint_campaign"]
    for bad in ("../secrets", "a/b", "..", "", "%2e%2e%2fsecrets", "a%2Fb"):
        result = await handler(campaign_id=bad)
        assert "campaign_id must be" in result


@pytest.mark.asyncio
async def test_get_campaign_renders_details(monkeypatch):
    _patch_client(
        monkeypatch,
        {
            "id": "camp-1",
            "name": "Ursnif wave",
            "startDate": "2026-07-01T00:00:00Z",
            "description": "A malicious campaign.",
            "actors": [{"id": "a1", "name": "TA505"}],
            "malware": [{"id": "m1", "name": "Ursnif"}],
            "techniques": [{"id": "t1", "name": "XL4 macros"}],
            "campaignMembers": [
                {
                    "id": "threat-1",
                    "threat": "http://evil.zz/x",
                    "type": "url",
                    "subType": "COMPLETE_URL",
                    "threatTime": "2026-07-01T12:00:00Z",
                }
            ],
        },
    )
    handler = _handlers()["get_proofpoint_campaign"]
    result = await handler(campaign_id="camp-1")
    assert "Ursnif wave" in result
    assert "TA505" in result
    assert "Ursnif" in result
    assert "XL4 macros" in result
    assert "threat-1" in result


# ---------------------------------------------------------------------------
# Forensics — threat/campaign XOR
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forensics_requires_exactly_one_id():
    handler = _handlers()["get_proofpoint_forensics"]
    result = await handler()
    assert "exactly ONE" in result
    result = await handler(threat_id="t1", campaign_id="c1")
    assert "exactly ONE" in result


@pytest.mark.asyncio
async def test_forensics_sends_campaign_param_and_renders(monkeypatch):
    holder = _patch_client(
        monkeypatch,
        {
            "generated": "2026-07-10T10:00:00Z",
            "reports": [
                {
                    "name": "http://evil.zz/x",
                    "scope": "THREAT",
                    "type": "url",
                    "id": "threat-1",
                    "forensics": [
                        {
                            "type": "dns",
                            "display": "DNS lookup evil.zz",
                            "malicious": True,
                            "what": {"host": "evil.zz"},
                        },
                        {
                            "type": "url",
                            "display": "Visited landing page",
                            "malicious": False,
                            "what": {"url": "http://evil.zz/x"},
                        },
                    ],
                }
            ],
        },
    )
    handler = _handlers()["get_proofpoint_forensics"]
    result = await handler(campaign_id="camp-1")
    _method, url, kwargs = holder["client"].requests[0]
    assert url.endswith("/forensics")
    assert (kwargs.get("params") or {}).get("campaignId") == "camp-1"
    assert "1 forensic report(s)" in result
    assert "DNS lookup evil.zz" in result
    assert "1 malicious" in result
    assert "non-malicious contextual evidence" in result


@pytest.mark.asyncio
async def test_forensics_include_campaign_flag(monkeypatch):
    holder = _patch_client(monkeypatch, {"reports": []})
    handler = _handlers()["get_proofpoint_forensics"]
    await handler(threat_id="threat-1", include_campaign_forensics=True)
    params = holder["client"].requests[0][2].get("params") or {}
    assert params.get("threatId") == "threat-1"
    assert params.get("includeCampaignForensics") == "true"


# ---------------------------------------------------------------------------
# Threat summary — traversal guard + rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_threat_rejects_traversal_id():
    handler = _handlers()["get_proofpoint_threat"]
    for bad in ("../../etc", "threat%2F..%2Fother", "%2e%2e"):
        result = await handler(threat_id=bad)
        assert "threat_id must be" in result


@pytest.mark.asyncio
async def test_get_threat_renders_summary(monkeypatch):
    _patch_client(
        monkeypatch,
        {
            "id": "threat-1",
            "name": "threat-1",
            "type": "attachment",
            "category": "malware",
            "status": "active",
            "severity": 800,
            "attackSpread": 62,
            "notable": True,
            "identifiedAt": "2026-07-01T15:30:10Z",
            "actors": [{"id": "a1", "name": "TA505"}],
            "families": [{"id": "f1", "name": "Banking"}],
            "brands": [{"id": "b1", "name": "DocuSign"}],
        },
    )
    handler = _handlers()["get_proofpoint_threat"]
    result = await handler(threat_id="threat-1")
    assert "severity: 800/1000" in result
    assert "TA505" in result
    assert "Banking" in result
    assert "DocuSign" in result
    assert "notable" in result


# ---------------------------------------------------------------------------
# People — window enum validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_people_window_must_be_supported():
    for tool in ("list_proofpoint_vap", "list_proofpoint_top_clickers"):
        handler = _handlers()[tool]
        result = await handler(window=7)
        assert "window must be one of 14, 30, 90" in result
        # Unparseable values must return the same self-correcting message
        # instead of raising into the executor's generic fallback.
        result = await handler(window="ninety")
        assert "window must be one of 14, 30, 90" in result
        result = await handler(window=90, size="many")
        assert "size and page must be integers" in result


@pytest.mark.asyncio
async def test_vap_renders_users_and_threshold(monkeypatch):
    _patch_client(
        monkeypatch,
        {
            "users": [
                {
                    "identity": {
                        "guid": "g1",
                        "emails": ["bruce.wayne@waynetech.net"],
                        "name": "Bruce Wayne",
                        "department": "Office of the CEO",
                        "title": "CEO",
                        "vip": True,
                    },
                    "threatStatistics": {
                        "attackIndex": 18558,
                        "families": [{"name": "phishing", "score": 2619}],
                    },
                }
            ],
            "totalVapUsers": 150,
            "averageAttackIndex": 371,
            "vapAttackIndexThreshold": 1520,
        },
    )
    handler = _handlers()["list_proofpoint_vap"]
    result = await handler(window=90)
    assert "Bruce Wayne" in result
    assert "attack index: 18558" in result
    assert "VIP" in result
    assert "phishing=2619" in result
    assert "VAP threshold: 1520" in result


@pytest.mark.asyncio
async def test_top_clickers_renders_click_stats(monkeypatch):
    _patch_client(
        monkeypatch,
        {
            "users": [
                {
                    "identity": {
                        "guid": "g2",
                        "emails": ["clark.kent@dailyplanet.com"],
                        "name": "Clark Kent",
                    },
                    "clickStatistics": {
                        "clickCount": 27,
                        "families": [{"name": "Banking", "clicks": 8}],
                    },
                }
            ],
            "totalTopClickers": 2,
        },
    )
    handler = _handlers()["list_proofpoint_top_clickers"]
    result = await handler(window=30)
    assert "Clark Kent" in result
    assert "clicks: 27" in result
    assert "Banking=8" in result


# ---------------------------------------------------------------------------
# URL decoder — input validation + POST body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decode_urls_requires_input():
    handler = _handlers()["decode_proofpoint_urls"]
    result = await handler(urls=[])
    assert "at least one" in result
    result = await handler(urls=["  ", ""])
    assert "at least one" in result


@pytest.mark.asyncio
async def test_decode_urls_caps_batch_size():
    handler = _handlers()["decode_proofpoint_urls"]
    result = await handler(urls=[f"https://u.example/{i}" for i in range(101)])
    assert "at most 100" in result


@pytest.mark.asyncio
async def test_decode_urls_posts_body_and_renders(monkeypatch):
    holder = _patch_client(
        monkeypatch,
        {
            "urls": [
                {
                    "encodedUrl": "https://urldefense.proofpoint.com/v2/url?u=x",
                    "decodedUrl": "https://media.example.com/jupiter.jpg",
                    "recipientEmail": "ckent@acme.zz",
                    "success": True,
                },
                {
                    "encodedUrl": "https://not-a-rewritten-url.example",
                    "success": False,
                    "error": "Encoded URL is not a Valid V1, V2, or V3 URL",
                },
            ]
        },
    )
    handler = _handlers()["decode_proofpoint_urls"]
    result = await handler(
        urls=["https://urldefense.proofpoint.com/v2/url?u=x", "https://bad"]
    )
    method, url, kwargs = holder["client"].requests[0]
    assert method == "POST"
    assert url.endswith("/url/decode")
    assert kwargs.get("json") == {
        "urls": ["https://urldefense.proofpoint.com/v2/url?u=x", "https://bad"]
    }
    assert "https://media.example.com/jupiter.jpg" in result
    assert "ckent@acme.zz" in result
    assert "DECODE FAILED" in result
    assert "not a Valid V1, V2, or V3 URL" in result


def test_decode_urls_schema_accepts_string_or_array():
    """The schema must mirror the handler's bare-string tolerance (oneOf)."""
    executor = _SafeIntegrationExecutor()
    defs = {d.name: d for d in build_tools(_TOKEN, executor)}
    urls_schema = defs["decode_proofpoint_urls"].input_schema["properties"]["urls"]
    variants = {
        v.get("type") for v in urls_schema["oneOf"]
    }
    assert variants == {"array", "string"}


@pytest.mark.asyncio
async def test_decode_urls_accepts_single_string(monkeypatch):
    """The LLM sometimes passes a bare string instead of a list."""
    holder = _patch_client(
        monkeypatch,
        {"urls": [{"decodedUrl": "https://ok.example", "success": True}]},
    )
    handler = _handlers()["decode_proofpoint_urls"]
    result = await handler(urls="https://urldefense.proofpoint.com/v2/url?u=x")
    assert "https://ok.example" in result
    assert holder["client"].requests[0][2].get("json") == {
        "urls": ["https://urldefense.proofpoint.com/v2/url?u=x"]
    }


# ---------------------------------------------------------------------------
# Connectivity validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_connection_missing_credentials():
    ok, message = await proofpoint.validate_connection("", "secret")
    assert not ok
    assert "Missing" in message
    # Whitespace-only input gets the same friendly message (stripped before
    # validation) instead of a confusing authentication failure.
    ok, message = await proofpoint.validate_connection("   ", "secret")
    assert not ok
    assert "Missing" in message
    ok, message = await proofpoint.validate_connection("principal", "  \n ")
    assert not ok
    assert "Missing" in message


@pytest.mark.asyncio
async def test_validate_connection_success(monkeypatch):
    holder = _patch_client(monkeypatch, {}, status_code=200)
    ok, message = await proofpoint.validate_connection("principal", "secret")
    assert ok
    assert "connected" in message
    _method, url, kwargs = holder["client"].requests[0]
    assert url.endswith("/siem/clicks/blocked")
    params = kwargs.get("params") or {}
    assert params.get("format") == "json"
    # Auth header present and correctly Basic-encoded.
    auth = (kwargs.get("headers") or {}).get("Authorization", "")
    assert auth.startswith("Basic ")
    assert base64.b64decode(auth.removeprefix("Basic ")).decode() == (
        "principal:secret"
    )


@pytest.mark.asyncio
async def test_validate_connection_auth_failure(monkeypatch):
    _patch_client(monkeypatch, {}, status_code=401)
    ok, message = await proofpoint.validate_connection("principal", "bad")
    assert not ok
    assert "Authentication failed" in message


@pytest.mark.asyncio
async def test_validate_connection_throttled(monkeypatch):
    _patch_client(monkeypatch, {}, status_code=429)
    ok, message = await proofpoint.validate_connection("principal", "secret")
    assert not ok
    assert "429" in message


@pytest.mark.asyncio
async def test_integration_class_parses_compound_token(monkeypatch):
    _patch_client(monkeypatch, {}, status_code=200)
    integ = ProofpointIntegration()
    assert await integ.validate_connection(_TOKEN) is True
    info = await integ.get_user_info(_TOKEN)
    assert info["principal"] == "principal-uuid"
    assert "secret" not in str(info.values())

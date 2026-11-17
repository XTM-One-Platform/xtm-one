"""OpenAEV's security posture widgets as validation outcomes: what the adapter reads and builds.

Pure: OpenAEV is an ``httpx.MockTransport`` handler answering with recorded
answers (the shapes of its ``/api/dashboards/adhoc/*``, ``/api/domains``,
``/api/security_platforms`` and ``/api/exercises/{id}``). What each widget
draws is ``dev-docs/dashboard-rendering-reference.md`` (OpenAEV, sections 9-14);
the engine draws the outcomes, the adapter alone knows OpenAEV.
"""

from __future__ import annotations

import base64
import json
import math
import uuid
from urllib.parse import unquote

import httpx
import pytest

from app.services import dashboard_live
from app.services.dashboard_live import (
    MAX_OUTCOME_ACTIONS,
    MAX_OUTCOME_CHAINS,
    MAX_OUTCOME_GROUPS,
    MAX_OUTCOME_ORBIT,
    MAX_OUTCOME_PHASES,
    MAX_OUTCOME_REFERENCE,
    MAX_OUTCOME_ROWS,
    MAX_OUTCOME_SUB_IDS,
    LiveError,
    LiveRequest,
    closed_static_data,
    outcomes_data,
    presentation_of,
)
from app.services.dashboard_live import openaev as live_openaev
from tests.test_dashboard_live import (
    INJECTS,
    INTEGRATION,
    PATTERNS,
    PHASES,
    _attack_path,
    _client,
    _posture,
    _recording,
    _reference_of,
    _technique_series,
    _use_transport,
)

BASE = "https://cti.example"


@pytest.fixture
def no_cache_no_limit(monkeypatch):
    """A cache of this test's own, and no rate limit."""
    store: dict[str, dict] = {}

    async def get(key):
        return store.get(key)

    async def put(key, value, ttl=None):
        store[key] = value

    async def no_limit(_user):
        return None

    monkeypatch.setattr(dashboard_live, "_cache_get", get)
    monkeypatch.setattr(dashboard_live, "_cache_set", put)
    monkeypatch.setattr(dashboard_live, "_enforce_rate", no_limit)
    return store

# ── Recorded answers ─────────────────────────────────────────────────────────

#: ``/adhoc/series`` of a command center: SUCCESS / FAILED / PENDING over the expectation types.
CENTER_SERIES = [
    {"label": "SUCCESS", "data": [
        {"key": "DETECTION", "label": "DETECTION", "value": 44}, {"key": "PREVENTION", "label": "PREVENTION", "value": 58},
        {"key": "CUSTOM_PROBE", "label": "CUSTOM_PROBE", "value": 2}, {"key": "VULNERABILITY", "label": "VULNERABILITY", "value": 23},
    ]},
    {"label": "FAILED", "data": [
        {"key": "PREVENTION", "label": "PREVENTION", "value": 22}, {"key": "DETECTION", "label": "DETECTION", "value": 38},
        {"key": "VULNERABILITY", "label": "VULNERABILITY", "value": 17}, {"key": "", "label": None, "value": 4},
    ]},
    {"label": "PENDING", "data": [{"key": "DETECTION", "label": "DETECTION", "value": 9}]},
]  # fmt: skip

#: ``/security_platforms``: ten platforms, extra fields OpenAEV sends besides those the widget reads.
PLATFORMS = [
    {"asset_id": f"sp-{i}", "asset_name": name, "security_platform_type": kind, "asset_description": "x", "asset_tags": []}
    for i, (name, kind) in enumerate([
        ("CrowdStrike Falcon", "EDR"), ("Splunk", "SIEM"), ("Darktrace", "NDR"), ("Cortex XSOAR", "SOAR"), ("Sentinel", "XDR"),
        ("Wiz", "ISPM"), ("Proofpoint", "EMAIL_SECURITY"), ("Home made", "CUSTOM"), ("Nessus", "VULNERABILITY_SCANNER"), ("Ninth", "EDR"),
    ])
]  # fmt: skip

#: ``/domains``: OpenAEV's domains, in the order it stores them.
DOMAINS = [
    {"domain_id": "d-cloud", "domain_name": "Cloud", "domain_color": "#123456"},
    {"domain_id": "d-tbc", "domain_name": "To classify", "domain_color": "#000000"},
    {"domain_id": "d-quantum", "domain_name": "Quantum", "domain_color": "#654321"},
    {"domain_id": "d-endpoint", "domain_name": "Endpoint", "domain_color": "#abcdef"},
    {"domain_id": "d-web", "domain_name": "Web App", "domain_color": "#abcdef"},
]

#: ``/adhoc/average``: a domain's expectation types, each with its status buckets and its document count.
AVERAGE = {
    "security_domain_average": [
        {"label": "Endpoint", "data": [
            {"label": "PREVENTION", "value": 42, "data": [
                {"key": "SUCCESS", "label": "SUCCESS", "value": 30}, {"key": "FAILED", "label": "FAILED", "value": 10},
                {"key": "PENDING", "label": "PENDING", "value": 2},
            ]},
            {"label": "MANUAL", "value": 3, "data": [{"key": "SUCCESS", "label": "SUCCESS", "value": 3}]},
        ]},
        {"label": "To classify", "data": [{"label": "PREVENTION", "value": 1, "data": [{"key": "SUCCESS", "label": "SUCCESS", "value": 1}]}]},
    ]
}  # fmt: skip

#: ``/exercises/{id}``.
EXERCISE = {"exercise_id": "sim-1", "exercise_name": "Q4 run", "exercise_start_date": "2026-10-20T09:00:00Z", "exercise_end_date": "2026-10-20T11:15:30Z"}


def _openaev(handlers: dict[str, object]):
    """A recording OpenAEV answering each path ending with a key of *handlers* (an answer, or a status code)."""

    def handler(request: httpx.Request) -> httpx.Response:
        for end, answer in handlers.items():
            if request.url.path.endswith(end):
                return httpx.Response(answer) if isinstance(answer, int) else httpx.Response(200, json=answer)
        return httpx.Response(404)

    return _recording(handler)


def _request(native: dict) -> LiveRequest:
    return LiveRequest(source="openaev", integration_id=INTEGRATION, native=native, shape="outcomes")


def _gauge(kind: str) -> dict:
    series = {"name": "", "filter": {"mode": "and", "filters": [{"key": "inject_expectation_type", "operator": "eq", "values": [kind], "mode": "or"}]}}
    return _posture("resilience-gauge", series, field="inject_expectation_status")


# ── Command center ───────────────────────────────────────────────────────────


async def test_the_command_center_is_one_gate_per_expectation_type_in_attack_order_with_its_platforms_and_calls_to_action(
    monkeypatch, no_cache_no_limit
):
    seen, record = _openaev({"/adhoc/series": CENTER_SERIES, "/security_platforms": PLATFORMS})
    _use_transport(monkeypatch, record)
    data = await live_openaev.fetch(_client(provider="openaev", tenant_id="t/1"), _request(_posture("command-center")))
    assert sorted((r.method, r.url.raw_path.decode()) for r in seen) == [
        ("GET", "/api/tenants/t%2F1/security_platforms"),
        ("POST", "/api/tenants/t%2F1/dashboards/adhoc/series"),
    ]
    assert data["items"] == [
        # VULNERABILITY, PREVENTION, DETECTION, then the others by name; a bucket without a label is left out.
        {"label": "Vulnerability", "success": 23.0, "failed": 17.0, "pending": 0.0, "parts": [],
         "icon": "bug", "color": "#0fbcff", "key": "VULNERABILITY", "successLabel": "not vulnerable"},
        {"label": "Prevention", "success": 58.0, "failed": 22.0, "pending": 0.0, "parts": [],
         "icon": "shield-check", "color": "#0fbcff", "key": "PREVENTION", "successLabel": "prevented"},
        {"label": "Detection", "success": 44.0, "failed": 38.0, "pending": 9.0, "parts": [],
         "icon": "radio", "color": "#0fbcff", "key": "DETECTION", "successLabel": "detected"},
        # An unknown type is humanised, gets no identity colour (the engine's cycle colours it) and is "stopped".
        {"label": "Custom probe", "success": 2.0, "failed": 0.0, "pending": 0.0, "parts": [],
         "icon": "circle-question-mark", "key": "CUSTOM_PROBE", "successLabel": "stopped"},
    ]  # fmt: skip
    # The first eight platforms, coloured by type; an unknown type has no colour (the engine's primary).
    assert data["orbit"] == [
        {"label": "CrowdStrike Falcon", "type": "EDR", "color": "#0fbcff"}, {"label": "Splunk", "type": "SIEM", "color": "#ffb300"},
        {"label": "Darktrace", "type": "NDR", "color": "#26a96c"}, {"label": "Cortex XSOAR", "type": "SOAR", "color": "#9575cd"},
        {"label": "Sentinel", "type": "XDR", "color": "#00bcd4"}, {"label": "Wiz", "type": "ISPM", "color": "#ff7043"},
        {"label": "Proofpoint", "type": "EMAIL_SECURITY", "color": "#4dd0e1"}, {"label": "Home made", "type": "CUSTOM"},
    ]  # fmt: skip
    # The calls to action link into the integration, built here.
    assert data["actions"] == [
        {"label": "Design a scenario", "caption": "Emulate real-world adversaries", "icon": "route", "color": "accent", "link": f"{BASE}/admin/scenarios"},
        {"label": "Launch a simulation", "caption": "Validate your defenses now", "icon": "circle-play", "color": "info", "link": f"{BASE}/admin/simulations"},
        {"label": "Run an atomic test", "caption": "Fire a single technique", "icon": "target", "color": "warning", "link": f"{BASE}/admin/atomic_testings"},
        {"label": "Review findings", "caption": "Hunt down validated gaps", "icon": "binoculars", "color": "danger", "link": f"{BASE}/admin/findings"},
    ]  # fmt: skip


@pytest.mark.parametrize("platforms", [[], 500])
async def test_without_its_platforms_the_command_center_orbits_the_sample_set(monkeypatch, no_cache_no_limit, platforms):
    _, record = _openaev({"/adhoc/series": CENTER_SERIES, "/security_platforms": platforms})
    _use_transport(monkeypatch, record)
    data = await live_openaev.fetch(_client(provider="openaev"), _request(_posture("command-center")))
    assert [(m["label"], m["color"]) for m in data["orbit"]] == [
        ("EDR", "#0fbcff"), ("SIEM", "#ffb300"), ("NDR", "#26a96c"), ("SOAR", "#9575cd"), ("XDR", "#00bcd4"), ("ISPM", "#ff7043"),
    ]  # fmt: skip


async def test_the_lists_the_posture_widgets_read_are_kept_an_hour_per_credential(monkeypatch, no_cache_no_limit):
    seen, record = _openaev({"/adhoc/series": CENTER_SERIES, "/security_platforms": PLATFORMS, "/adhoc/average": AVERAGE, "/domains": DOMAINS})
    _use_transport(monkeypatch, record)
    client = _client(provider="openaev")
    for _ in range(2):
        await live_openaev.fetch(client, _request(_posture("command-center")))
        await live_openaev.fetch(client, _request(_posture("average", config_type="average")))
    assert [r.url.path for r in seen if r.method == "GET"] == ["/api/security_platforms", "/api/domains"]
    # Kept under keys of the credential, cut to the fields the widgets read; another credential reads its own.
    kept = {key.split(":")[0]: value for key, value in no_cache_no_limit.items()}
    assert kept["dashboard-aev-platforms"]["rows"][0] == {"asset_id": "sp-0", "asset_name": "CrowdStrike Falcon", "security_platform_type": "EDR"}
    assert kept["dashboard-aev-domains"]["rows"][1] == {"domain_id": "d-tbc", "domain_name": "To classify"}
    other = _client(provider="openaev")
    other.headers["Authorization"] = "Bearer another"
    await live_openaev.fetch(other, _request(_posture("command-center")))
    assert [r.url.path for r in seen if r.method == "GET"] == ["/api/security_platforms", "/api/domains", "/api/security_platforms"]


# ── Exposure score and resilience gauge ──────────────────────────────────────


def test_the_exposure_score_is_one_outcome_per_bucket_as_it_comes():
    data = live_openaev.normalize(_posture("exposure-score"), CENTER_SERIES)
    assert [(i["label"], i["key"], i["success"], i["failed"], "color" in i) for i in data["items"]] == [
        ("Detection", "DETECTION", 44.0, 38.0, False),
        ("Prevention", "PREVENTION", 58.0, 22.0, False),
        ("Custom probe", "CUSTOM_PROBE", 2.0, 0.0, False),
        ("Vulnerability", "VULNERABILITY", 23.0, 17.0, False),
    ]
    # Series named otherwise are read by their place: success, failed.
    unnamed = [{"label": "", "data": [{"key": "P", "label": "PREVENTION", "value": 5}]}, {"label": "", "data": [{"key": "P", "label": "PREVENTION", "value": 1}]}]
    assert [(i["success"], i["failed"]) for i in live_openaev.normalize(_posture("exposure-score"), unnamed)["items"]] == [(5.0, 1.0)]


@pytest.mark.parametrize(
    ("kind", "label", "glyph"),
    [("PREVENTION", "Prevention", "shield-check"), ("DETECTION", "Detection", "eye"), ("VULNERABILITY", "Vulnerability", "bug"),
     ("MANUAL", "Manual", "headset"), ("CHALLENGE", "Challenge", "headset"), ("FIREWALL_RULE", "Firewall rule", "crosshair")],
)  # fmt: skip
def test_a_resilience_gauge_counts_its_type_s_statuses_with_the_type_s_glyph(kind, label, glyph):
    statuses = [{"label": "", "data": [{"key": k, "label": k, "value": v} for k, v in {"SUCCESS": 64, "FAILED": 27, "PENDING": 9, "PARTIAL": 4}.items()]}]
    assert live_openaev.normalize(_gauge(kind), statuses) == {
        "items": [{"label": label, "success": 64.0, "failed": 27.0, "pending": 9.0, "parts": [], "icon": glyph}]
    }


# ── Security domains ─────────────────────────────────────────────────────────


async def test_the_security_domains_are_every_domain_in_order_with_the_measured_ones_types_and_status_rows(monkeypatch, no_cache_no_limit):
    seen, record = _openaev({"/adhoc/average": AVERAGE, "/domains": DOMAINS})
    _use_transport(monkeypatch, record)
    data = await live_openaev.fetch(_client(provider="openaev"), _request(_posture("average", config_type="average")))
    assert sorted(r.url.path for r in seen) == ["/api/dashboards/adhoc/average", "/api/domains"]
    assert data["items"] == [
        {
            "label": "Endpoint", "success": 33.0, "failed": 10.0, "pending": 2.0, "icon": "monitor-smartphone",
            "parts": [
                {"label": "Prevention", "success": 30.0, "failed": 10.0, "pending": 2.0, "icon": "shield-check", "key": "PREVENTION", "total": 42.0,
                 "rows": [{"label": "Prevented", "value": 30.0, "status": "success", "key": "SUCCESS"},
                          {"label": "Not Prevented", "value": 10.0, "status": "failed", "key": "FAILED"},
                          {"label": "Pending", "value": 2.0, "status": "pending", "key": "PENDING"}]},
                # A human-graded type reads its status capitalised.
                {"label": "Manual", "success": 3.0, "failed": 0.0, "pending": 0.0, "icon": "circle-check-big", "key": "MANUAL", "total": 3.0,
                 "rows": [{"label": "Success", "value": 3.0, "status": "success", "key": "SUCCESS"}]},
            ],
        }
    ]  # fmt: skip
    placeholders = [{"label": "Prevention", "icon": "shield-check"}, {"label": "Detection", "icon": "radio"}, {"label": "Vulnerability", "icon": "bug"}]
    # OpenAEV's order (Endpoint, Network, Web App, ..., Cloud, ...), an unknown domain last, the unclassified one left out.
    assert data["reference"] == [
        {"id": "d-endpoint", "label": "Endpoint", "icon": "monitor-smartphone", "parts": placeholders},
        {"id": "d-web", "label": "Web App", "icon": "monitor", "parts": placeholders},
        {"id": "d-cloud", "label": "Cloud", "icon": "cloud", "parts": placeholders},
        {"id": "d-quantum", "label": "Quantum", "icon": "circle-question-mark", "parts": placeholders},
    ]


async def test_without_the_domain_list_the_cards_are_the_measured_domains(monkeypatch, no_cache_no_limit):
    _, record = _openaev({"/adhoc/average": AVERAGE, "/domains": 403})
    _use_transport(monkeypatch, record)
    data = await live_openaev.fetch(_client(provider="openaev"), _request(_posture("average", config_type="average")))
    assert ([i["label"] for i in data["items"]], "reference" in data) == (["Endpoint"], False)


# ── Security coverage ────────────────────────────────────────────────────────


def test_a_security_coverage_lists_every_kill_chain_and_top_level_technique_and_the_tested_ones_rolled_up():
    native = _posture("security-coverage", field="base_attack_patterns_side")
    phases = [*PHASES, {"phase_id": "atlas-recon", "phase_name": "Reconnaissance", "phase_order": 1, "phase_kill_chain_name": "mitre-atlas"}]
    answer = _technique_series({"ap-phish-link": 2, "ap-cmd": 1, "ap-gone": 9}, {"ap-phish": 1, "ap-valid": 3})
    data = live_openaev.technique_matrix(native, answer, _reference_of(phases=phases))
    # Chains by name; ATT&CK opens (the first whose name holds "attack"); phases by order; known chains by their product name.
    assert data["chains"] == [
        {"id": "mitre-atlas", "label": "MITRE ATLAS", "groups": [{"id": "atlas-recon", "label": "Reconnaissance"}]},
        {"id": "mitre-attack", "label": "MITRE ATT&CK", "default": True,
         "groups": [{"id": "ta-init", "label": "Initial Access"}, {"id": "ta-exec", "label": "Execution"}]},
        {"id": "mitre-ics-attack", "label": "mitre-ics-attack", "groups": [{"id": "ics-impact", "label": "Impact"}]},
    ]  # fmt: skip
    # Every top-level technique, with its phases and the sub-techniques a click on it covers.
    assert data["reference"] == [
        {"id": "ap-phish", "label": "Phishing", "code": "T1566", "phases": ["ta-init"], "subIds": ["ap-phish-link"]},
        {"id": "ap-valid", "label": "Valid Accounts", "code": "T1078", "phases": ["ta-init", "ta-exec"]},
        {"id": "ap-cmd", "label": "Command and Scripting Interpreter", "code": "T1059", "phases": ["ta-exec"]},
        {"id": "ap-ics", "label": "Manipulation of Control", "code": "T0831", "phases": ["ics-impact"]},
    ]
    # The tested ones by id, a sub-technique's outcomes in its parent's; one OpenAEV no longer knows left out.
    assert [(i["id"], i["code"], i["label"], i["success"], i["failed"]) for i in data["items"]] == [
        ("ap-phish", "T1566", "Phishing", 2.0, 1.0),
        ("ap-cmd", "T1059", "Command and Scripting Interpreter", 1.0, 0.0),
        ("ap-valid", "T1078", "Valid Accounts", 0.0, 3.0),
    ]


async def test_a_security_coverage_without_its_reference_lists_the_techniques_tested(monkeypatch, no_cache_no_limit):
    _, record = _openaev({"/adhoc/series": _technique_series({"ap-cmd": 1}, {}), "/kill_chain_phases": 403})
    _use_transport(monkeypatch, record)
    data = await live_openaev.fetch(_client(provider="openaev"), _request(_posture("security-coverage", field="base_attack_patterns_side")))
    assert data == {"items": [{"label": "ap-cmd", "success": 1.0, "failed": 0.0, "pending": 0.0, "parts": []}]}


# ── Attack path ──────────────────────────────────────────────────────────────


def test_an_attack_path_node_opens_its_simulation_s_injects_on_its_technique():
    url = live_openaev.injects_link(BASE, "sim/1", "ap-phish")
    assert url is not None
    base, query = url.split("?query=")
    assert base == f"{BASE}/admin/simulations/sim%2F1"
    # As ``qs.stringify`` writes it, then base64 (``AttackPath.tsx``).
    pairs = [tuple(unquote(part).split("=", 1)) for part in base64.b64decode(query).decode().split("&")]
    filter_id = pairs[5][1]
    assert str(uuid.UUID(filter_id)) == filter_id
    assert pairs == [
        ("page", "0"), ("size", "20"), ("sorts[0][property]", "inject_updated_at"), ("sorts[0][direction]", "DESC"),
        ("filterGroup[mode]", "or"), ("filterGroup[filters][0][id]", filter_id), ("filterGroup[filters][0][key]", "inject_attack_patterns"),
        ("filterGroup[filters][0][mode]", "and"), ("filterGroup[filters][0][values][0]", "ap-phish"),
        ("filterGroup[filters][0][operator]", "contains"), ("key", "simulation-injects-results"),
    ]  # fmt: skip
    # Brackets are encoded, as qs encodes keys.
    assert base64.b64decode(query).decode().startswith("page=0&size=20&sorts%5B0%5D%5Bproperty%5D=inject_updated_at&")
    # A base URL that is not http(s) links nowhere.
    assert live_openaev.injects_link("javascript:", "sim-1", "ap-phish") is None


def test_an_attack_path_is_its_simulation_s_techniques_under_each_of_their_phases_linked_to_the_next():
    native = _attack_path()
    path, body = live_openaev.injects_request(native, tenant_id="t/1")
    assert path == "/api/tenants/t%2F1/dashboards/adhoc/entities"
    assert body["widget_config"]["perspective"]["filter"]["filters"][1] == {"key": "base_simulation_side", "operator": "eq", "mode": "or", "values": ["sim-1"]}
    assert (body["widget_config"]["time_range"], body["widget_config"]["limit"]) == ("ALL_TIME", 1000)
    answer = _technique_series({"ap-phish": 2, "ap-cmd": 1}, {"ap-valid": 1, "ap-phish": 1})
    data = live_openaev.attack_path(native, answer, _reference_of(), INJECTS, base_url=BASE, exercise=EXERCISE)
    # The phases its techniques sit under, by order.
    assert data["chains"] == [{"id": "path", "label": "", "groups": [{"id": "ta-init", "label": "Initial Access"}, {"id": "ta-exec", "label": "Execution"}]}]
    # Only the techniques its injects carry (an unknown one left out), each once with every phase it sits under;
    # a link to itself or to a technique no inject carries is left out.
    assert [(i["id"], i["code"], i["label"], i["success"], i["failed"], i["phases"], i["links"]) for i in data["items"]] == [
        ("ap-phish", "T1566", "Phishing", 2.0, 1.0, ["ta-init"], ["ap-cmd", "ap-valid"]),
        ("ap-valid", "T1078", "Valid Accounts", 0.0, 1.0, ["ta-init", "ta-exec"], ["ap-cmd"]),
        ("ap-cmd", "T1059", "Command and Scripting Interpreter", 1.0, 0.0, ["ta-exec"], []),
    ]
    assert data["items"][0]["link"] == live_openaev.injects_link(BASE, "sim-1", "ap-phish")
    assert data["timeline"] == {"start": "2026-10-20T09:00:00Z", "end": "2026-10-20T11:15:30Z"}


async def test_an_attack_path_without_a_simulation_asks_for_one_and_reads_nothing(monkeypatch, no_cache_no_limit):
    seen, record = _openaev({})
    _use_transport(monkeypatch, record)
    assert await live_openaev.fetch(_client(provider="openaev"), _request(_attack_path(None))) == {"items": [], "needsScope": True}
    assert seen == []


async def test_an_attack_path_reads_its_series_reference_injects_and_simulation(monkeypatch, no_cache_no_limit):
    handlers: dict[str, object] = {
        "/adhoc/series": _technique_series({"ap-phish": 2}, {}), "/kill_chain_phases": PHASES,
        "/attack_patterns/search": {"content": PATTERNS, "last": True}, "/adhoc/entities": INJECTS, "/exercises/sim-1": EXERCISE,
    }  # fmt: skip
    seen, record = _openaev(handlers)
    _use_transport(monkeypatch, record)
    request = _request(_attack_path())
    data = await live_openaev.fetch(_client(provider="openaev"), request)
    assert sorted(r.url.path.rsplit("/", 1)[-1] for r in seen) == ["entities", "kill_chain_phases", "search", "series", "sim-1"]
    assert (data["items"][0]["links"], data["timeline"]["end"]) == (["ap-cmd", "ap-valid"], "2026-10-20T11:15:30Z")
    # Without the simulation's dates, the path is drawn without them.
    handlers["/exercises/sim-1"] = 500
    assert "timeline" not in await live_openaev.fetch(_client(provider="openaev"), request)
    # Without its injects or its reference it is a failure, never a coverage drawn in its place.
    for path, status, code in (("/adhoc/entities", 500, "upstream_error"), ("/kill_chain_phases", 403, "access_denied")):
        handlers[path] = status
        no_cache_no_limit.clear()
        with pytest.raises(LiveError) as failed:
            await live_openaev.fetch(_client(provider="openaev"), request)
        assert failed.value.code == code
        handlers["/adhoc/entities"], handlers["/kill_chain_phases"] = INJECTS, PHASES


# ── Presentation ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("widget_type", "view", "options"),
    [
        ("security-coverage", "xtm.outcomematrix", {"expandShown": True}),
        ("attack-path", "xtm.pathgraph", {}),
        ("command-center", "xtm.exposureconsole", {"sampleDisplay": "subtle"}),
        ("exposure-score", "xtm.scoregauge", {"sampleDisplay": "full"}),
        ("resilience-gauge", "xtm.ringgauge", {"sampleDisplay": "full"}),
        ("average", "xtm.domaincards", {}),
    ],
)
def test_each_posture_widget_is_presented_with_its_view_and_validated_options(widget_type, view, options):
    native = _posture(widget_type, config_type="average" if widget_type == "average" else "structural-histogram")
    # Every OpenAEV widget's title in its letter-spaced capitals (WidgetTitle).
    assert presentation_of("openaev", native) == {"view": view, "options": {**options, "titleStyle": "caps", "emptyMessage": "display", "accentColor": "brand"}}


def test_another_openaev_widget_has_no_posture_preset():
    assert live_openaev.outcome_presentation(_posture("donut", field="inject_status")) is None


# ── The shape's bounds ───────────────────────────────────────────────────────


def test_an_outcome_keeps_its_valid_extras_only_and_bounded():
    extras = {
        "code": "T1059" * 20, "icon": "ShieldCheck", "color": "url(#x)", "key": "PREVENTION", "link": "javascript:alert(1)",
        "successLabel": "prevented", "phases": [f"p{i}" for i in range(30)] + ["p0"], "subIds": [f"s{i}" for i in range(70)], "junk": 1,
    }  # fmt: skip
    item = outcomes_data([("T", 1, 0, 0, [], "", "id-1", [], extras)])["items"][0]
    assert item == {
        "label": "T", "success": 1.0, "failed": 0.0, "pending": 0.0, "parts": [], "id": "id-1", "links": [],
        "code": ("T1059" * 20)[:40], "icon": "shield-check", "key": "PREVENTION", "successLabel": "prevented",
        "phases": [f"p{i}" for i in range(MAX_OUTCOME_PHASES)], "subIds": [f"s{i}" for i in range(MAX_OUTCOME_SUB_IDS)],
    }  # fmt: skip
    # An item merges by id within its group.
    twice = outcomes_data([("Same", 1, 0, 0, [], "", "a", []), ("Same", 1, 0, 0, [], "", "b", []), ("Again", 2, 0, 0, [], "", "a", [])])
    assert [(i["id"], i["label"], i["success"]) for i in twice["items"]] == [("a", "Same", 3.0), ("b", "Same", 1.0)]


def test_a_part_keeps_its_status_rows_bounded_and_known_statuses_only():
    rows = [{"label": f"r{i}", "value": i, "status": "success", "key": "SUCCESS"} for i in range(MAX_OUTCOME_ROWS + 4)]
    part = ("Prevention", 1, 2, 3, {"icon": "bug", "key": "PREVENTION", "total": "42", "rows": [{"label": "x", "value": 1, "status": "partial"}, *rows]})
    kept = outcomes_data([("d", 0, 0, 0, [part, ("bad", 1)])])["items"][0]["parts"]
    assert len(kept) == 1
    assert (kept[0]["icon"], kept[0]["key"], kept[0]["total"], len(kept[0]["rows"]), kept[0]["rows"][0]) == (
        "bug", "PREVENTION", 42.0, MAX_OUTCOME_ROWS, {"label": "r0", "value": 0.0, "status": "success", "key": "SUCCESS"},
    )  # fmt: skip
    assert "total" not in outcomes_data([("d", 0, 0, 0, [("p", 0, 0, 0, {"total": -1})])])["items"][0]["parts"][0]


def test_chains_reference_timeline_actions_and_orbit_are_bounded_and_validated():
    chains = [{"id": f"c{i}", "label": "", "default": "yes", "groups": [{"id": f"g{j}", "label": f"G{j}"} for j in range(50)] + [{"id": "g0"}, "junk"]}
              for i in range(MAX_OUTCOME_CHAINS + 2)]  # fmt: skip
    data = outcomes_data(
        [],
        chains=[{"id": ""}, *chains],
        reference=[{"id": f"r{i}", "label": f"R{i}", "code": "X", "color": "red", "parts": [{"label": "P", "icon": "bug", "x": 1}]} for i in range(MAX_OUTCOME_REFERENCE + 5)],
        timeline={"start": "2026-10-20T11:00:00+02:00", "end": "2026-10-20T08:00:00Z"},
        actions=[{"label": "Go", "link": "/dashboards"}, {"label": "No link"}, {"label": "Bad", "link": "javascript:x"}] * 4,
        orbit=[{"label": f"P{i}", "type": "EDR", "color": "#0fbcff"} for i in range(12)] + [{"type": "EDR"}],
    )  # fmt: skip
    assert len(data["chains"]) == MAX_OUTCOME_CHAINS
    assert (data["chains"][0]["id"], data["chains"][0]["label"], "default" in data["chains"][0], len(data["chains"][0]["groups"])) == ("c0", "", False, MAX_OUTCOME_GROUPS)
    assert len(data["reference"]) == MAX_OUTCOME_REFERENCE
    assert data["reference"][0] == {"id": "r0", "label": "R0", "code": "X", "parts": [{"label": "P", "icon": "bug"}]}
    # An end before its start is dropped; a moment is kept in UTC.
    assert data["timeline"] == {"start": "2026-10-20T09:00:00Z", "end": None}
    assert data["actions"] == [{"label": "Go", "link": "/dashboards"}] * 4
    assert len(data["orbit"]) == MAX_OUTCOME_ORBIT
    assert outcomes_data([], actions=[{"label": "Go", "link": "/x"}] * 10)["actions"] == [{"label": "Go", "link": "/x"}] * MAX_OUTCOME_ACTIONS
    assert outcomes_data([], timeline={"start": "soon"}, chains="junk", reference=None, orbit=[{}]) == {"items": []}


def test_the_counts_stay_finite_and_positive():
    data = outcomes_data([("T", "3", -1, math.nan, [("p", math.inf, "x", 2)])])
    assert data["items"][0] == {"label": "T", "success": 3.0, "failed": 0.0, "pending": 0.0, "parts": [{"label": "p", "success": 0.0, "failed": 0.0, "pending": 2.0}]}


def test_static_outcomes_keep_every_extra_and_link_only_inside_the_platform():
    raw = {
        "items": [{"label": "Phishing", "success": 2, "id": "ap-1", "code": "T1566", "phases": ["ta"], "link": "/dashboards/1",
                   "parts": [{"label": "Prevention", "success": 1, "rows": [{"label": "Prevented", "value": 1, "status": "success"}]}]}],
        "chains": [{"id": "c", "label": "Chain", "groups": [{"id": "ta", "label": "Initial Access"}]}],
        "reference": [{"id": "ap-1", "label": "Phishing"}], "timeline": {"start": "2026-10-20"},
        "actions": [{"label": "Open", "link": "/dashboards"}], "orbit": [{"label": "EDR", "type": "EDR"}], "needsScope": True,
    }  # fmt: skip
    data = closed_static_data("outcomes", raw)
    assert data == {
        "items": [{"label": "Phishing", "success": 2.0, "failed": 0.0, "pending": 0.0, "id": "ap-1", "links": [], "code": "T1566",
                   "link": "/dashboards/1", "phases": ["ta"],
                   "parts": [{"label": "Prevention", "success": 1.0, "failed": 0.0, "pending": 0.0,
                              "rows": [{"label": "Prevented", "value": 1.0, "status": "success"}]}]}],
        "chains": raw["chains"], "reference": raw["reference"], "timeline": {"start": "2026-10-20", "end": None},
        "actions": raw["actions"], "orbit": raw["orbit"], "needsScope": True,
    }  # fmt: skip
    for outside in ({"items": [{"label": "x", "link": "https://evil.example"}]}, {"items": [], "actions": [{"label": "x", "link": "https://evil.example"}]}):
        with pytest.raises(ValueError, match="href must be a path of this platform"):
            closed_static_data("outcomes", outside)
    # An adapter's links survive a derived source re-closing them.
    closed = outcomes_data([("x", 1, 0, 0, [], "", "id", [], {"link": f"{BASE}/admin/simulations/s"})], actions=[{"label": "a", "link": f"{BASE}/admin"}])
    assert closed_static_data("outcomes", json.loads(json.dumps(closed)), reclosing=True) == closed


"""The known-exploited count tailored to the organization profile's software
(#11213, slice 2): the Act now KEV row counts only the KEV vulnerabilities that
affect software the organization runs, and names the products it matched.

No DB / network: the profile read, ``_user_creds``, the collectors and
``_graphql`` are monkeypatched.
"""

from __future__ import annotations

from app.services import ctem_metrics_collector as collector

STACK = [
    {"product": "FortiOS", "vendor": "Fortinet", "cpe": "", "label": "Fortinet FortiOS"},
    {"product": "Exchange Server", "vendor": "Microsoft", "cpe": "cpe:2.3:a:microsoft:exchange_server:2019:*:*:*:*:*:*:*", "label": "Microsoft Exchange Server"},
]


def test_stack_matches_by_cpe_then_name_and_vendor():
    cpe = STACK[1]["cpe"]
    assert collector._stack_matches({"name": "whatever", "cpe": cpe.upper()}, STACK) == "Microsoft Exchange Server"
    assert collector._stack_matches({"name": "fortios", "vendor": "Fortinet"}, STACK) == "Fortinet FortiOS"
    # A vendor named on both sides must agree; a missing vendor does not block.
    assert collector._stack_matches({"name": "FortiOS", "vendor": "Other"}, STACK) is None
    assert collector._stack_matches({"name": "FortiOS"}, STACK) == "Fortinet FortiOS"
    assert collector._stack_matches({"name": "Nginx"}, STACK) is None


async def test_kev_tailored_counts_kev_related_to_matched_software(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_graphql(client, base_url, headers, query, variables=None):
        calls.append((query, variables or {}))
        if query is collector._OCTI_STACK_SOFTWARE:
            return {
                "stixCyberObservables": {
                    "edges": [
                        {"node": {"id": "sw-ex", "name": "Exchange Server", "vendor": "Microsoft", "cpe": ""}},
                        {"node": {"id": "sw-fo", "name": "FortiOS", "vendor": "Fortinet", "cpe": ""}},
                        {"node": {"id": "sw-no", "name": "FortiOS", "vendor": "Elsewhere", "cpe": ""}},
                    ]
                }
            }, None
        return {"entities": [{"total": 4}]}, None

    monkeypatch.setattr(collector, "_graphql", fake_graphql)

    out = await collector._octi_kev_tailored(None, "http://x", {}, STACK, "u1")

    # The fake answers one number: the funnel's tailored counts are missing, so None.
    assert out == {
        "kev_tailored_count": 4,
        "kev_tailored_products": ["Fortinet FortiOS", "Microsoft Exchange Server"],
        "funnel_tailored": None,
    }
    software_vars = calls[0][1]
    keys = [f["key"] for f in software_vars["filters"]["filters"]]
    assert keys == [["name"], ["cpe"]] and software_vars["first"] == collector._STACK_SOFTWARE_CAP
    params = calls[1][1]["params"][0]
    regarding = params["filters"]["filters"][1]
    assert regarding["key"] == ["regardingOf"]
    assert {"key": "id", "values": ["sw-ex", "sw-fo"]} in regarding["values"]


async def test_kev_tailored_is_zero_when_no_software_matches(monkeypatch):
    queries: list[str] = []

    async def fake_graphql(client, base_url, headers, query, variables=None):
        queries.append(query)
        return {"stixCyberObservables": {"edges": []}}, None

    monkeypatch.setattr(collector, "_graphql", fake_graphql)

    out = await collector._octi_kev_tailored(None, "http://x", {}, STACK, "u1")

    assert out == {
        "kev_tailored_count": 0,
        "kev_tailored_products": [],
        "funnel_tailored": {"relevant": 0, "exploitable": 0},
    }
    assert queries == [collector._OCTI_STACK_SOFTWARE]


async def test_kev_tailored_is_none_when_opencti_cannot_answer(monkeypatch):
    async def fake_graphql(client, base_url, headers, query, variables=None):
        return None, "forbidden"

    monkeypatch.setattr(collector, "_graphql", fake_graphql)

    assert await collector._octi_kev_tailored(None, "http://x", {}, STACK, "u1") is None


def _wire(monkeypatch, stack):
    async def fake_stack(db):
        return stack

    async def fake_creds(db, user_id, provider):
        return ("http://fake", {"Authorization": "x"})

    async def fake_octi(base_url, headers, user_id, client):
        return {"prioritization": {"kev_count": 1300}}

    monkeypatch.setattr(collector, "_organization_software", fake_stack)
    monkeypatch.setattr(collector, "_user_creds", fake_creds)
    monkeypatch.setattr(collector, "_VERTICAL_COLLECTORS", {"cyber": [("opencti", fake_octi)]})


async def test_build_posture_merges_the_tailored_count(monkeypatch):
    _wire(monkeypatch, STACK)

    async def fake_tailored(client, base_url, headers, stack, user_id):
        assert stack == STACK
        return {"kev_tailored_count": 3, "kev_tailored_products": ["Fortinet FortiOS"]}

    monkeypatch.setattr(collector, "_octi_kev_tailored", fake_tailored)

    stage_facts, _ = await collector.build_posture_for_user(None, "u1", "cyber")

    assert stage_facts["prioritization"] == {
        "kev_count": 1300,
        "kev_tailored_count": 3,
        "kev_tailored_products": ["Fortinet FortiOS"],
    }


async def test_build_posture_without_a_tech_stack_runs_no_tailoring(monkeypatch):
    _wire(monkeypatch, [])

    async def never(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("no software: no tailoring query")

    monkeypatch.setattr(collector, "_octi_kev_tailored", never)

    stage_facts, _ = await collector.build_posture_for_user(None, "u1", "cyber")

    assert stage_facts["prioritization"] == {"kev_count": 1300}


async def test_organization_software_is_empty_without_a_session() -> None:
    assert await collector._organization_software(None) == []

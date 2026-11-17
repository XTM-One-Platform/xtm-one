"""The CTEM deep-link lab (#12058): [service.lab-ctem-opencti] and its seeded OpenCTI under /opencti.

The CTEM figures of [service.lab-ctem-screens] come from a sample whose tailored layers carry no filter, on an
OpenCTI address on ``.invalid``: no link could be followed. This lab reads them live from an OpenCTI of its own,
seeded with known vulnerabilities, served under ``/opencti`` of the preview origin. These pin the declaration,
the seed's counts, the proxy and the address the posture answers. dev-docs/reference/agentic-labs.md.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from app.services.sandbox_workspace import checks
from tests.test_lab_seed_declarations import CHECKS, LAB, _lab_namespace, _lab_source, _services

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not LAB.is_file(), reason="no .xtm-one next to the backend")

OPENCTI_KEYS = {"opencti", "opencti-search", "opencti-broker", "opencti-redis", "opencti-s3"}
VITE = CHECKS.parents[1] / "frontend" / "vite.config.ts"
DOCS = CHECKS.parents[1] / "dev-docs" / "reference" / "agentic-labs.md"


def test_the_lab_is_the_screens_lab_on_an_opencti_of_its_own_under_opencti():
    services = _services()
    lab, opencti, plain = services["lab-ctem-opencti"], services["lab-opencti-ctem"], services["lab-opencti"]
    assert lab["argv"] == services["lab-ctem-screens"]["argv"] and lab["depends_on"] == ["lab-opencti-ctem"]
    assert lab["env"]["LAB_OPENCTI"] == "live" and lab["env"]["LAB_CTEM_OPENCTI"] == "on"
    # The figures are OpenCTI's own: no sample switch beside it.
    assert "LAB_CTEM_SCREENS" not in lab["env"]
    # The same OpenCTI as [service.lab-opencti], served under /opencti.
    assert {k: v for k, v in opencti["env"].items() if k not in ("APP__BASE_PATH", "APP__APP_LOGS__LOGS_LEVEL")} == {
        k: v for k, v in plain["env"].items() if k != "APP__APP_LOGS__LOGS_LEVEL"
    }
    namespace = _lab_namespace("CTEM_OPENCTI_PATH", "CTEM_OPENCTI_TOKEN")
    assert opencti["env"]["APP__BASE_PATH"] == namespace["CTEM_OPENCTI_PATH"] == "/opencti"
    assert opencti["env"]["APP__ADMIN__TOKEN"] == namespace["CTEM_OPENCTI_TOKEN"]
    assert opencti["ready_path"].startswith("/opencti/health")
    specs = checks.load_services(CHECKS.read_text(encoding="utf-8"))
    chain = checks.service_chain(specs, "lab-ctem-opencti", own_images=OPENCTI_KEYS)
    assert chain[-1].name == "lab-ctem-opencti" and chain[-2].name == "lab-opencti-ctem"


def test_the_seeded_vulnerabilities_give_every_figure_a_distinct_nonzero_count():
    ns = _lab_namespace("CTEM_OPENCTI_VULNERABILITIES", "CTEM_OPENCTI_SOFTWARE", "LAB_ORG_TECH_STACK",
                        "ctem_opencti_expected")
    expected = ns["ctem_opencti_expected"]()
    assert expected == {"known": 10, "critical": 4, "kev": 3, "relevant": 5, "exploitable": 3}
    # Each funnel layer narrows: a link that dropped its filter would show a different count.
    assert expected["known"] > expected["relevant"] > expected["exploitable"] > 0
    stack = {(e["vendor"], e["product"]) for e in ns["LAB_ORG_TECH_STACK"]}
    assert set(ns["CTEM_OPENCTI_SOFTWARE"]) <= stack
    names = [row[0] for row in ns["CTEM_OPENCTI_VULNERABILITIES"]]
    assert len(set(names)) == len(names)


def test_the_seed_writes_through_opencti_then_checks_the_live_funnel():
    calls: list[tuple[str, dict]] = []

    def gql(query, variables):
        calls.append((query, variables))
        key = "stixCyberObservableAdd" if "stixCyberObservableAdd" in query else (
            "vulnerabilityAdd" if "vulnerabilityAdd" in query else "stixCoreRelationshipAdd")
        return {key: {"id": f"id-{len(calls)}"}}

    ns = _lab_namespace(
        "CTEM_OPENCTI_PATH",
        "CTEM_OPENCTI_VULNERABILITIES",
        "CTEM_OPENCTI_SOFTWARE",
        "CTEM_OPENCTI_KPI_FACTS",
        "ctem_opencti_expected",
        "seed_ctem_opencti",
    )
    expected = ns["ctem_opencti_expected"]()
    filters = {"mode": "and", "filters": [], "filterGroups": []}
    # #12220: the KEV and critical KPIs are OpenCTI's own counts (3 / 4), never the LARGE_FACTS overlay.
    kpis = [
        {"label": "Known-exploited (KEV)", "value": expected["kev"]},
        {"label": "Critical CVEs", "value": expected["critical"]},
    ]
    posture = {
        "stages": [
            {
                "key": "prioritization",
                "facts": kpis,
                "viz": {
                    "funnel": {
                        "layers": [
                            {"key": "known", "count": expected["known"], "filters": None},
                            {"key": "relevant", "count": expected["relevant"], "filters": filters},
                            {"key": "exploitable", "count": expected["exploitable"], "filters": filters},
                        ]
                    }
                },
            }
        ]
    }
    api_calls: list[tuple] = []

    def api(method, path, token=None, body=None):
        api_calls.append((method, path))
        return posture if method == "GET" else {}

    ns.update(ctem_opencti_graphql=gql, api=api)
    seeded = ns["seed_ctem_opencti"]("t", "http://localhost:3100")
    assert "CTEM funnel live from OpenCTI" in seeded
    assert sum("vulnerabilityAdd" in q for q, _ in calls) == expected["known"]
    assert sum("stixCoreRelationshipAdd" in q for q, _ in calls) == expected["relevant"]
    assert api_calls[0] == ("POST", "/ctem-metrics/sync?vertical=cyber")
    assert (expected["kev"], expected["critical"]) == (3, 4)
    # The overlay's KEV 42 is the defect #12220 fixed: the seed refuses it.
    kpis[0]["value"] = 42
    with pytest.raises(RuntimeError, match=r"Known-exploited \(KEV\) KPI reads 42"):
        ns["seed_ctem_opencti"]("t", "http://localhost:3100")
    kpis[0]["value"] = expected["kev"]
    # A tailored layer without its filter is the defect this lab exists to catch.
    posture["stages"][0]["viz"]["funnel"]["layers"][1]["filters"] = None
    with pytest.raises(RuntimeError, match="relevant layer carries no OpenCTI filter"):
        ns["seed_ctem_opencti"]("t", "http://localhost:3100")


def test_the_seed_runs_after_the_registration_and_saves_the_stack():
    seed = _lab_source("seed_entities")
    assert seed.index("seed_live_opencti(token, base)") < seed.index("seed_ctem_opencti(token, base)")
    assert seed.index("seed_org_ai_draft_stub(token, base)") < seed.index("seed_ctem_opencti(token, base)")
    assert "lab_switch('LAB_CTEM_OPENCTI', LAB_CTEM_OPENCTI_STATES)" in _lab_source("seed_org_ai_draft_stub")
    assert "'LAB_CTEM_OPENCTI'" in _lab_source("seeds_ctem_large_facts")


def test_the_vite_proxy_is_declared_only_with_the_lab_opencti():
    source = VITE.read_text(encoding="utf-8")
    assert "process.env.VITE_OPENCTI_URL" in source and '"/opencti"' in source
    main = _lab_source("main")
    assert "'VITE_OPENCTI_URL': LIVE_OPENCTI_URL" in main and "'VITE_OPENCTI_TOKEN': CTEM_OPENCTI_TOKEN" in main


def _lab_asgi(monkeypatch):
    async def backend(scope, receive, send):
        body = json.dumps({"products": {"opencti": {"present": True, "url": "http://localhost:4000/opencti"}}})
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", b"1")]})
        await send({"type": "http.response.body", "body": body.encode()})

    import importlib.util

    lab_dir = CHECKS.parent / "lab"
    monkeypatch.setitem(sys.modules, "app.main", types.SimpleNamespace(application=backend))
    monkeypatch.syspath_prepend(str(lab_dir))
    spec = importlib.util.spec_from_file_location("lab_asgi_ctem_opencti_under_test", lab_dir / "lab_asgi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_posture_answers_opencti_on_the_preview_origin(monkeypatch):
    module = _lab_asgi(monkeypatch)
    scope = {"type": "http", "method": "GET", "path": "/api/v1/ctem-metrics/posture",
             "headers": [(b"referer", b"https://preview.example/ctem/prioritization")]}
    assert module.asks_ctem_posture_on_preview(scope, {}) is False
    assert module.asks_ctem_posture_on_preview(scope, {"LAB_CTEM_OPENCTI": "on"}) is True
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    asyncio.run(module.answer_ctem_posture_on_preview(scope, receive, send))
    body = json.loads(sent[1]["body"])
    assert body["products"]["opencti"]["url"] == "https://preview.example/opencti"
    assert dict(sent[0]["headers"])[b"content-length"] == str(len(sent[1]["body"])).encode()
    # No Referer: the registered address is kept as is.
    assert module.with_preview_opencti_url({"products": {"opencti": {"url": "x"}}}, None)["products"]["opencti"]["url"] == "x"


def test_the_lab_is_documented():
    docs = DOCS.read_text(encoding="utf-8")
    assert "[service.lab-ctem-opencti]" in docs and "LAB_CTEM_OPENCTI" in docs

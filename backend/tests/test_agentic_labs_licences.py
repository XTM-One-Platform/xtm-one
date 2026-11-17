"""Enterprise Edition licences in the agentic coding labs (Sam's decision D2).

The licensing server is an ``httpx.MockTransport``; the lab is started on the
fake Kubernetes client of ``test_agentic_labs.py``, whose fixtures (``lab_settings``,
``no_quota``, ``redis``) are imported under their own names for pytest to resolve them.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings, settings
from app.services.sandbox_workspace import labs, licences
from tests.test_agentic_labs import (  # noqa: F401
    _Kube,
    _no_sleep,
    _resolved,
    _workspace,
    lab_settings,
    no_quota,
    redis,
)

PEM = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----"
TOKEN = "api-user-token-123"


@pytest.fixture
def licensing(monkeypatch):
    monkeypatch.setattr(settings, "agentic_labs_licensing_url", "https://licences.example/")
    monkeypatch.setattr(settings, "agentic_labs_licensing_token", TOKEN)
    monkeypatch.setattr(settings, "agentic_labs_licences", {"PRODUCT_A_LICENSE": "Product A", "PRODUCT_B_LICENSE": "B"})
    monkeypatch.setattr(settings, "agentic_labs_licence_hours", 24)


def _server(answers: dict[str, httpx.Response], seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        product = json.loads(request.content)["product_name"]
        return answers.get(product) or httpx.Response(400, text="Invalid product")

    return httpx.MockTransport(handler)


def test_the_mapping_is_parsed_from_the_environment_on_the_first_colon():
    parsed = Settings._parse_agentic_labs_licences("OPENCTI_LICENSE:OpenCTI, XTM_LICENSE:Filigran XTM,broken")
    assert parsed == {"OPENCTI_LICENSE": "OpenCTI", "XTM_LICENSE": "Filigran XTM"}


@pytest.mark.parametrize("missing", ["agentic_labs_licensing_url", "agentic_labs_licensing_token", "agentic_labs_licences"])
async def test_nothing_is_asked_until_the_url_the_token_and_the_mapping_are_set(licensing, monkeypatch, missing):
    monkeypatch.setattr(settings, missing, {} if missing == "agentic_labs_licences" else "")

    def handler(_request):
        raise AssertionError("no licensing call expected")

    issued = await licences.issue(transport=httpx.MockTransport(handler))
    assert not licences.configured() and issued.env == {} and issued.missing == {} and issued.summary() == ""


async def test_one_ci_licence_per_product_with_the_token_and_the_validity(licensing):
    seen: list[httpx.Request] = []
    answers = {"Product A": httpx.Response(200, text=PEM + "\n"), "B": httpx.Response(200, text=PEM)}
    issued = await licences.issue(transport=_server(answers, seen))
    assert issued.env == {"PRODUCT_A_LICENSE": PEM + "\n", "PRODUCT_B_LICENSE": PEM + "\n"} and issued.missing == {}
    assert {str(r.url) for r in seen} == {"https://licences.example/api/ci"}
    assert {r.headers["authorization"] for r in seen} == {f"Bearer {TOKEN}"}
    bodies = [json.loads(r.content) for r in seen]
    assert bodies == [{"product_name": "Product A", "validity_hours": 24}, {"product_name": "B", "validity_hours": 24}]
    assert issued.summary() == "Enterprise Edition licences: PRODUCT_A_LICENSE, PRODUCT_B_LICENSE"


async def test_the_validity_is_bounded_by_what_the_server_accepts(licensing, monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setattr(settings, "agentic_labs_licence_hours", 1000)
    await licences.issue(transport=_server({}, seen))
    assert {json.loads(r.content)["validity_hours"] for r in seen} == {licences.MAX_HOURS}


async def test_a_licence_the_server_refuses_is_named_and_the_token_never_appears(licensing, caplog):
    answers = {"Product A": httpx.Response(200, text=PEM), "B": httpx.Response(401, text=f"bad token {TOKEN}")}
    issued = await licences.issue(transport=_server(answers))
    assert list(issued.env) == ["PRODUCT_A_LICENSE"]
    assert issued.missing == {"PRODUCT_B_LICENSE": "the licensing server answered 401"}
    assert "missing licences: PRODUCT_B_LICENSE (the licensing server answered 401)" in issued.summary()
    assert TOKEN not in issued.summary() and TOKEN not in caplog.text and PEM not in caplog.text


async def test_an_unreachable_server_or_an_answer_that_is_no_certificate_never_raises(licensing, monkeypatch):
    monkeypatch.setattr(settings, "agentic_labs_licences", {"A": "Product A", "B": "B", "bad name": "C"})

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["product_name"] == "Product A":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, text="<html>sign in</html>")

    issued = await licences.issue(transport=httpx.MockTransport(handler))
    assert issued.env == {}
    assert issued.missing == {
        "A": "the licensing server did not answer (ConnectError)",
        "B": "the licensing server answered no certificate",
        "bad name": "not an environment variable name",
    }


# -- the lab pod ------------------------------------------------------------------


@pytest.mark.usefixtures("lab_settings")
async def test_every_container_gets_the_licences_and_a_declaration_can_override_one():
    resolved = await _resolved(_workspace())
    manifest = labs.pod_manifest(
        resolved, name="lab-1", image="i", workspace_id="w",
        licence_env={"PRODUCT_A_LICENSE": PEM, "ENABLED_FEATURES": "from-licences"},
    )
    for container in manifest["spec"]["containers"]:
        env = {item["name"]: item["value"] for item in container["env"]}
        assert env["PRODUCT_A_LICENSE"] == PEM
    lab = {item["name"]: item["value"] for item in manifest["spec"]["containers"][2]["env"]}
    assert lab["ENABLED_FEATURES"] == "*"  # the service's own env wins
    bare = labs.pod_manifest(resolved, name="lab-1", image="i", workspace_id="w")
    assert not any(item["name"] == "PRODUCT_A_LICENSE" for c in bare["spec"]["containers"] for item in c["env"])


@pytest.mark.usefixtures("lab_settings", "no_quota", "licensing", "redis")
async def test_a_lab_starts_with_its_licences_and_says_which_one_is_missing(monkeypatch):
    answers = {"Product A": httpx.Response(200, text=PEM), "B": httpx.Response(503, text="down")}
    original = licences.issue

    async def issue():
        return await original(transport=_server(answers))

    monkeypatch.setattr(licences, "issue", issue)
    workspace = _workspace()
    fake = _Kube()
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    assert outcome.ready, outcome.text
    (pod,) = fake.pods.values()
    for container in pod["spec"]["containers"]:
        assert {"name": "PRODUCT_A_LICENSE", "value": PEM + "\n"} in container["env"]
        assert all(TOKEN not in item["value"] for item in container["env"])
    assert "Enterprise Edition licences: PRODUCT_A_LICENSE" in outcome.text
    assert "PRODUCT_B_LICENSE (the licensing server answered 503)" in outcome.text
    assert PEM not in outcome.text and TOKEN not in outcome.text and TOKEN not in json.dumps(pod)
    held = (await workspace.record(create=False)).kernel_state["labs"]["co:lab"]
    assert PEM not in json.dumps(held) and TOKEN not in json.dumps(held)

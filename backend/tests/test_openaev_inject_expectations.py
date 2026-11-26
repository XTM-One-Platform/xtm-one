"""An inject wired into a scenario is scored by intent, not blindly.

``add_openaev_inject_to_scenario`` is generic and also reached over
``/mcp/openaev``. When the ``expectations`` argument is passed it writes those
types into ``inject_content.expectations`` (replacing the injector contract's
predefined ones, which OpenAEV seeds only when the key is absent or empty).
When it is omitted and the caller put no expectations in ``content``, the tool
reads the injector contract's ``availableExpectations``:

* a contract that PREDEFINES a security-control expectation (a payload's own
  types, the HTTP Request contract's DETECTION + PREVENTION) is left alone, so
  OpenAEV seeds those with the platform scope their author chose;
* a contract that only OFFERS prevention / detection is scored on the
  contract's own PREVENTION + DETECTION objects;
* a human-response contract (email, article, challenge) keeps its own.

VULNERABILITY is never auto-added (no collector fills it on a non-scan step).
"""

import json

import pytest

from app.integrations.providers.openaev import _common, build_tools
from app.integrations.providers.openaev.core import (
    _contract_default_expectations,
    _expectations_for_registered_platforms,
)
from app.tools.base import CallableToolExecutor, ToolCall

BASE = "https://demo.openaev.io"


def _expectation(
    expectation_type: str, *, predefined: bool = False, platforms: list[str] | None = None
) -> dict:
    """One ``availableExpectations`` item, in the shape OpenAEV serves it."""
    return {
        "expectation_type": expectation_type,
        "expectation_name": expectation_type.title(),
        "expectation_description": "",
        "expectation_score": 100.0,
        "expectation_expectation_group": False,
        "expectation_is_predefined": predefined,
        "expectation_expected_security_platform_types": platforms or [],
    }


def _contract_content(available: list) -> str:
    """A realistic ``injector_contract_content`` offering ``available``.

    Mirrors the platform shape: a ``fields`` entry with ``key ==
    "expectations"`` whose ``availableExpectations`` array carries one object
    per item. A bare string is an offered, non-predefined expectation of that
    type.
    """
    items = [_expectation(a) if isinstance(a, str) else a for a in available]
    return json.dumps(
        {
            "fields": [
                {"key": "targeted_asset", "type": "asset"},
                {
                    "key": "expectations",
                    "label": "Expectations",
                    "type": "expectation",
                    "availableExpectations": items,
                },
            ]
        }
    )


#: A payload contract as ``PayloadService.createContractExpectationsBasedOnPayload``
#: builds it: every technical type is offered, only the payload's chosen ones
#: (here DETECTION scoped to SIEM) are predefined.
PAYLOAD_CONTRACT_DETECTION_ON_SIEM = [
    _expectation("PREVENTION"),
    _expectation("DETECTION", predefined=True, platforms=["SIEM"]),
    _expectation("LOGGING"),
    _expectation("VULNERABILITY"),
]

#: The same builder for a scan payload predefined on VULNERABILITY only.
PAYLOAD_CONTRACT_VULNERABILITY = [
    _expectation("PREVENTION"),
    _expectation("DETECTION"),
    _expectation("LOGGING"),
    _expectation("VULNERABILITY", predefined=True),
]

#: The HTTP Request contracts of the OpenAEV ``injectors`` repository
#: (``http-query/http_query/contracts_http.py``): DETECTION and PREVENTION are
#: both predefined, each scoped to its security platform types.
HTTP_REQUEST_CONTRACT = [
    _expectation("DETECTION", predefined=True, platforms=["XDR", "SIEM", "NDR"]),
    _expectation("PREVENTION", predefined=True, platforms=["XDR", "NDR"]),
]

#: A contract that offers the security-control pair but predefines only MANUAL:
#: left alone, the inject would measure nothing against the security stack.
MANUAL_PREDEFINED_TECHNICAL_CONTRACT = [
    _expectation("MANUAL", predefined=True),
    _expectation("PREVENTION", platforms=["EDR"]),
    _expectation("DETECTION", platforms=["EDR", "SIEM"]),
]

#: The email contract (``ContractExpectationsDefaultsTest``): MANUAL offered,
#: nothing predefined.
EMAIL_CONTRACT = [_expectation("MANUAL")]

#: The media-pressure contract: ARTICLE predefined, MANUAL offered.
MEDIA_PRESSURE_CONTRACT = [_expectation("ARTICLE", predefined=True), _expectation("MANUAL")]


class _Resp:
    status_code = 200
    content = b"{}"
    text = "{}"
    headers: dict = {}

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Recorder:
    """Records the JSON body of every non-settings request.

    ``available_expectations`` is what the wired injector contract offers in
    its ``injector_contract_content`` (see ``_contract_content``). It starts
    empty, i.e. a contract offering nothing, so the default harness keeps the
    conservative "omitting writes nothing" behaviour. ``contract_error`` makes
    the contract read fail.
    """

    def __init__(self):
        self.bodies: list[dict] = []
        self.available_expectations: list = []
        self.contract_error: str | None = None
        #: The registered security platform types; None makes the read fail.
        self.security_platform_types: list[str] | None = None
        self.platform_reads = 0

    async def __call__(self, client, method, url, provider, action, **kwargs):
        if url == f"{BASE}/api/settings":
            return _Resp({}), None
        if url == f"{BASE}/api/me/tenants":
            # No membership route: an OpenAEV without default-tenant scoping.
            return None, "OpenAEV error (HTTP 404): Not Found"
        if "/security_platforms/search" in url:
            self.platform_reads += 1
            if self.security_platform_types is None:
                return None, "OpenAEV error (HTTP 503): Service Unavailable"
            return (
                _Resp({"content": [{"security_platform_type": t} for t in self.security_platform_types]}),
                None,
            )
        if "/api/injector_contracts/" in url and method == "get":
            # The expectations default reads what the contract offers.
            if self.contract_error:
                return None, self.contract_error
            return (
                _Resp(
                    {"injector_contract_content": _contract_content(self.available_expectations)}
                ),
                None,
            )
        self.bodies.append(kwargs.get("json") or {})
        return _Resp({"scenario_id": "s1", "inject_id": "i1"}), None


def _install(monkeypatch) -> tuple[_Recorder, CallableToolExecutor]:
    recorder = _Recorder()
    monkeypatch.setattr(_common, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(f"{BASE}||test-token", executor)
    return recorder, executor


async def _add(executor, **extra) -> str:
    args = {"scenario_id": "s1", "title": "I", "injector_contract_id": "c1", **extra}
    result = await executor.execute_tool(
        ToolCall(id="c", name="add_openaev_inject_to_scenario", arguments=args)
    )
    return result.content


def _content(recorder: _Recorder) -> dict:
    return recorder.bodies[-1]["inject_content"]


# ── The pure content reader ──────────────────────────────────────────────


def _types(expectations: list[dict]) -> list[str]:
    return [e["expectation_type"] for e in expectations]


def test_reader_leaves_a_payload_contract_with_a_predefined_type_alone():
    # The payload's own DETECTION-on-SIEM is predefined: OpenAEV seeds it, so
    # no generic PREVENTION the payload author excluded is added.
    assert _contract_default_expectations(_contract_content(PAYLOAD_CONTRACT_DETECTION_ON_SIEM)) == []


def test_reader_leaves_a_vulnerability_payload_contract_alone():
    assert _contract_default_expectations(_contract_content(PAYLOAD_CONTRACT_VULNERABILITY)) == []


def test_reader_leaves_the_http_request_contract_alone():
    # Its predefined DETECTION + PREVENTION already score the inject, with
    # their XDR / SIEM / NDR scopes.
    assert _contract_default_expectations(_contract_content(HTTP_REQUEST_CONTRACT)) == []


def test_reader_scores_a_contract_that_only_offers_prevention_detection():
    defaults = _contract_default_expectations(
        _contract_content(MANUAL_PREDEFINED_TECHNICAL_CONTRACT)
    )

    # PREVENTION then DETECTION, as the contract itself defines them: the
    # platform scope its author chose is kept.
    assert _types(defaults) == ["PREVENTION", "DETECTION"]
    assert defaults[0]["expectation_expected_security_platform_types"] == ["EDR"]
    assert defaults[1]["expectation_expected_security_platform_types"] == ["EDR", "SIEM"]
    assert all(e["expectation_score"] == 100.0 for e in defaults)


def test_reader_fills_keys_a_contract_item_leaves_out():
    defaults = _contract_default_expectations(
        _contract_content([{"expectation_type": "DETECTION"}])
    )

    assert defaults == [
        {
            "expectation_type": "DETECTION",
            # OpenAEV's default name for the type (#2480), never a home-made one.
            "expectation_name": "Detection",
            "expectation_description": "",
            "expectation_score": 100,
            "expectation_expectation_group": False,
            "expectation_expected_security_platform_types": [],
        }
    ]


def test_reader_never_defaults_vulnerability():
    # Offered but not predefined: a scan step is scored explicitly.
    assert _contract_default_expectations(_contract_content(["VULNERABILITY"])) == []


def test_reader_leaves_human_response_contracts_alone():
    assert _contract_default_expectations(_contract_content(EMAIL_CONTRACT)) == []
    assert _contract_default_expectations(_contract_content(MEDIA_PRESSURE_CONTRACT)) == []


def test_reader_treats_legacy_predefined_expectations_as_predefined():
    # Pre-migration contracts list only their predefined items, under
    # predefinedExpectations: OpenAEV seeds them, so nothing is defaulted.
    content = json.dumps(
        {
            "fields": [
                {
                    "key": "expectations",
                    "type": "expectation",
                    "predefinedExpectations": [
                        {"expectation_type": "PREVENTION"},
                        {"expectation_type": "DETECTION"},
                    ],
                }
            ]
        }
    )
    assert _contract_default_expectations(content) == []


def test_reader_tolerates_malformed_content():
    assert _contract_default_expectations(None) == []
    assert _contract_default_expectations("") == []
    assert _contract_default_expectations("not json") == []
    assert _contract_default_expectations("[]") == []
    assert _contract_default_expectations('{"fields": "x"}') == []
    assert _contract_default_expectations('{"fields": [{"key": "expectations", "availableExpectations": ["x", 3]}]}') == []


# ── The tool's omitted-argument default ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "available",
    [
        PAYLOAD_CONTRACT_DETECTION_ON_SIEM,
        PAYLOAD_CONTRACT_VULNERABILITY,
        HTTP_REQUEST_CONTRACT,
        EMAIL_CONTRACT,
        MEDIA_PRESSURE_CONTRACT,
        ["VULNERABILITY"],
        [],
    ],
    ids=[
        "payload-detection-on-siem",
        "payload-vulnerability",
        "http-request",
        "email",
        "media-pressure",
        "vulnerability-offered",
        "nothing-offered",
    ],
)
async def test_omitting_keeps_what_the_contract_predefines(monkeypatch, available):
    recorder, executor = _install(monkeypatch)
    recorder.available_expectations = available

    await _add(executor)

    # No key written: OpenAEV seeds the contract's predefined expectations.
    assert "expectations" not in _content(recorder)


@pytest.mark.asyncio
async def test_omitting_scores_a_contract_that_only_offers_prevention_detection(monkeypatch):
    recorder, executor = _install(monkeypatch)
    recorder.available_expectations = MANUAL_PREDEFINED_TECHNICAL_CONTRACT

    await _add(executor)

    written = _content(recorder)["expectations"]
    assert _types(written) == ["PREVENTION", "DETECTION"]
    assert written[1]["expectation_expected_security_platform_types"] == ["EDR", "SIEM"]


@pytest.mark.asyncio
async def test_an_empty_expectations_list_in_content_counts_as_absent(monkeypatch):
    recorder, executor = _install(monkeypatch)
    recorder.available_expectations = MANUAL_PREDEFINED_TECHNICAL_CONTRACT

    # OpenAEV's setExpectations treats [] as absent, and so does the default.
    await _add(executor, content='{"url": "http://honey.scanme.sh", "expectations": []}')

    body = _content(recorder)
    assert body["url"] == "http://honey.scanme.sh"
    assert _types(body["expectations"]) == ["PREVENTION", "DETECTION"]


@pytest.mark.asyncio
async def test_a_failed_contract_read_is_logged(monkeypatch, caplog):
    recorder, executor = _install(monkeypatch)
    recorder.contract_error = "OpenAEV error (HTTP 503): Service Unavailable"

    with caplog.at_level("WARNING", logger="app.integrations.providers.openaev.core"):
        await _add(executor)

    assert "expectations" not in _content(recorder)
    assert any("c1" in r.getMessage() and "HTTP 503" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_technical_default_does_not_override_content_expectations(monkeypatch):
    recorder, executor = _install(monkeypatch)
    recorder.available_expectations = MANUAL_PREDEFINED_TECHNICAL_CONTRACT

    # The caller put its own expectations in content; the default must not win.
    content = '{"url": "http://honey.scanme.sh", "expectations": [{"expectation_type": "MANUAL"}]}'
    await _add(executor, content=content)

    body = _content(recorder)
    assert body["expectations"] == [{"expectation_type": "MANUAL"}]


@pytest.mark.asyncio
async def test_omitting_expectations_keeps_those_in_content(monkeypatch):
    recorder, executor = _install(monkeypatch)

    # A caller (e.g. an MCP client) supplies its own expectations in content.
    content = '{"url": "http://honey.scanme.sh", "expectations": [{"expectation_type": "MANUAL"}]}'
    await _add(executor, content=content)

    body = _content(recorder)
    assert body["url"] == "http://honey.scanme.sh"
    # Preserved exactly, not replaced by a generated default.
    assert body["expectations"] == [{"expectation_type": "MANUAL"}]


@pytest.mark.asyncio
async def test_explicit_prevention_detection_is_written(monkeypatch):
    recorder, executor = _install(monkeypatch)

    await _add(executor, expectations=["PREVENTION", "DETECTION"])

    types = [e["expectation_type"] for e in _content(recorder)["expectations"]]
    assert types == ["PREVENTION", "DETECTION"]


@pytest.mark.asyncio
async def test_explicit_vulnerability_is_written(monkeypatch):
    recorder, executor = _install(monkeypatch)

    await _add(executor, expectations=["VULNERABILITY"])

    types = [e["expectation_type"] for e in _content(recorder)["expectations"]]
    assert types == ["VULNERABILITY"]


@pytest.mark.asyncio
async def test_each_written_expectation_is_a_full_scored_object(monkeypatch):
    recorder, executor = _install(monkeypatch)

    await _add(executor, expectations=["PREVENTION", "DETECTION"])

    for e in _content(recorder)["expectations"]:
        # A score the platform can evaluate, and an empty platform scope
        # (= any security platform, OpenAEV's legacy default).
        assert e["expectation_score"] == 100
        assert e["expectation_name"]
        assert e["expectation_expected_security_platform_types"] == []


@pytest.mark.asyncio
async def test_explicit_expectations_replace_those_in_content(monkeypatch):
    recorder, executor = _install(monkeypatch)

    content = '{"url": "http://honey.scanme.sh", "expectations": [{"expectation_type": "MANUAL"}]}'
    await _add(executor, content=content, expectations=["PREVENTION", "DETECTION"])

    body = _content(recorder)
    # The non-expectation content is preserved,
    assert body["url"] == "http://honey.scanme.sh"
    # but the explicit argument wins over the stray one in content.
    assert [e["expectation_type"] for e in body["expectations"]] == ["PREVENTION", "DETECTION"]


@pytest.mark.asyncio
async def test_explicit_manual_is_written(monkeypatch):
    recorder, executor = _install(monkeypatch)

    await _add(executor, expectations=["MANUAL"])

    types = [e["expectation_type"] for e in _content(recorder)["expectations"]]
    assert types == ["MANUAL"]


@pytest.mark.asyncio
async def test_expectations_are_case_insensitive_and_canonicalised(monkeypatch):
    recorder, executor = _install(monkeypatch)

    await _add(executor, expectations=["detection", "prevention"])

    types = [e["expectation_type"] for e in _content(recorder)["expectations"]]
    assert types == ["DETECTION", "PREVENTION"]


@pytest.mark.asyncio
async def test_unknown_expectation_type_is_rejected(monkeypatch):
    recorder, executor = _install(monkeypatch)

    out = await _add(executor, expectations=["NOPE"])

    assert "Unknown expectation type" in out
    assert recorder.bodies == []  # returned before any request


@pytest.mark.asyncio
async def test_empty_expectations_list_is_rejected(monkeypatch):
    recorder, executor = _install(monkeypatch)

    out = await _add(executor, expectations=[])

    assert "cannot be an empty list" in out
    assert recorder.bodies == []


# ── #2378: expectations no registered security platform can meet ──────────

#: The Nuclei CVE contract of OpenAEV-Platform/injectors
#: (``nuclei/nuclei/nuclei_contracts/nuclei_contracts.py``): DETECTION (XDR,
#: SIEM, NDR), PREVENTION (XDR, NDR) and VULNERABILITY, all predefined.
NUCLEI_CVE_CONTRACT = [
    _expectation("DETECTION", predefined=True, platforms=["XDR", "SIEM", "NDR"]),
    _expectation("PREVENTION", predefined=True, platforms=["XDR", "NDR"]),
    _expectation("VULNERABILITY", predefined=True),
]


def test_helper_keeps_only_vulnerability_on_an_edr_and_scanner_tenant():
    kept, left_out = _expectations_for_registered_platforms(
        _contract_content(NUCLEI_CVE_CONTRACT), {"EDR", "VULNERABILITY_SCANNER"}
    )
    assert _types(kept) == ["VULNERABILITY"]
    assert _types(left_out) == ["DETECTION", "PREVENTION"]


def test_helper_keeps_the_full_set_when_an_expected_type_is_registered():
    kept, left_out = _expectations_for_registered_platforms(_contract_content(NUCLEI_CVE_CONTRACT), {"XDR"})
    assert _types(kept) == ["DETECTION", "PREVENTION", "VULNERABILITY"]
    assert left_out == []


def test_helper_decides_nothing_when_the_platforms_are_unreadable():
    assert _expectations_for_registered_platforms(_contract_content(NUCLEI_CVE_CONTRACT), None) == ([], [])


def test_helper_keeps_an_unscoped_expectation():
    # An empty platform scope means "any security platform": never left out.
    content = _contract_content([_expectation("DETECTION", predefined=True)])
    kept, left_out = _expectations_for_registered_platforms(content, set())
    assert _types(kept) == ["DETECTION"] and left_out == []


@pytest.mark.asyncio
async def test_omitting_drops_expectations_no_registered_platform_can_meet(monkeypatch):
    recorder, executor = _install(monkeypatch)
    recorder.available_expectations = NUCLEI_CVE_CONTRACT
    recorder.security_platform_types = ["EDR", "VULNERABILITY_SCANNER"]

    out = await _add(executor)

    written = _content(recorder)["expectations"]
    assert _types(written) == ["VULNERABILITY"]
    assert written[0]["expectation_score"] == 100.0
    assert "Expectations left out" in out
    assert "DETECTION (expects XDR, SIEM, NDR)" in out and "PREVENTION (expects XDR, NDR)" in out
    assert "EDR, VULNERABILITY_SCANNER" in out


@pytest.mark.asyncio
async def test_omitting_keeps_the_predefined_set_when_a_platform_matches(monkeypatch):
    recorder, executor = _install(monkeypatch)
    recorder.available_expectations = NUCLEI_CVE_CONTRACT
    recorder.security_platform_types = ["XDR"]

    out = await _add(executor)

    assert "expectations" not in _content(recorder)
    assert "left out" not in out


@pytest.mark.asyncio
async def test_omitting_keeps_the_predefined_set_when_the_platforms_are_unreadable(monkeypatch):
    recorder, executor = _install(monkeypatch)
    recorder.available_expectations = NUCLEI_CVE_CONTRACT
    recorder.security_platform_types = None

    out = await _add(executor)

    assert "expectations" not in _content(recorder)
    assert "left out" not in out


@pytest.mark.asyncio
async def test_an_inject_with_nothing_measurable_keeps_its_set_and_says_why(monkeypatch):
    # An empty list would make OpenAEV seed the predefined set again.
    recorder, executor = _install(monkeypatch)
    recorder.available_expectations = HTTP_REQUEST_CONTRACT
    recorder.security_platform_types = ["EDR"]

    out = await _add(executor)

    assert "expectations" not in _content(recorder)
    assert "cannot be met" in out


@pytest.mark.asyncio
async def test_explicit_expectations_never_read_the_platforms(monkeypatch):
    recorder, executor = _install(monkeypatch)
    recorder.available_expectations = NUCLEI_CVE_CONTRACT
    recorder.security_platform_types = ["EDR"]

    await _add(executor, expectations=["PREVENTION", "DETECTION"])

    assert _types(_content(recorder)["expectations"]) == ["PREVENTION", "DETECTION"]
    assert recorder.platform_reads == 0

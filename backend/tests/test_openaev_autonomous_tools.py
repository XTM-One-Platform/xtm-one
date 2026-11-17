"""Unit tests for the OpenAEV autonomous attack-path run-coordination tools.

These cover the pure validation / formatting behaviour of the four tools the
``openaev-attack-path-orchestrator`` agent uses to drive a live run. The HTTP
layer (``_api``) is monkeypatched so no network is touched: we assert that bad
input is rejected before any call, and that a good call formats its body and
response as the OpenAEV ``AutonomousRunApi`` expects.
"""

from __future__ import annotations

import pytest

from app.integrations.providers.openaev import autonomous as auto_tools
from app.tools.base import CallableToolExecutor, ToolCall


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


def _build():
    """Register the tools into a fresh executor and return (executor, calls)."""
    executor = CallableToolExecutor()
    defs = auto_tools.register("https://oaev.example", {"Authorization": "Bearer x"}, executor)
    return executor, {d.name for d in defs}


async def _run(executor: CallableToolExecutor, name: str, **args) -> str:
    result = await executor.execute_tool(ToolCall(id="1", name=name, arguments=args))
    return result.content


def test_register_exposes_the_run_coordination_tools():
    _, names = _build()
    assert names == {
        "record_openaev_event",
        "update_openaev_run_status",
        "consume_openaev_directives",
        "resolve_openaev_capabilities",
        "author_openaev_attack_path_step",
        "update_openaev_attack_path_step",
        "delete_openaev_attack_path_step",
        "evaluate_openaev_attack_path",
        "read_openaev_attack_path_state",
        "promote_openaev_finding_to_asset",
        "ensure_openaev_target_team",
        "get_openaev_run_scope",
        "set_openaev_run_scope",
    }


def test_record_event_caps_question_options_at_three():
    # OpenAEV's reasoning panel slices the proposed choices down to its own
    # MAX_QUESTION_CHOICES = 3 and silently drops the rest, so this description - the
    # surface the LLM actually reads while building the "options" array - must state the
    # same cap. Pinned so the two sides cannot drift apart again: an over-long menu loses
    # its tail with no error anywhere.
    executor = CallableToolExecutor()
    defs = auto_tools.register("https://oaev.example", {}, executor)
    description = next(d.description for d in defs if d.name == "record_openaev_event")
    assert "AT MOST 3 options" in description
    # The superseded, un-renderable cap must never come back.
    assert "5-6" not in description


@pytest.mark.asyncio
async def test_record_event_requires_run_id(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(executor, "record_openaev_event", type="DECISION", title="x")
    assert "run_id is required" in out


@pytest.mark.asyncio
async def test_record_event_rejects_unknown_type(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(executor, "record_openaev_event", run_id="r1", type="BOGUS", title="x")
    assert "Unknown event type" in out
    # The GAP type (not CAPABILITY_GAP) is the accepted vocabulary.
    assert "GAP" in out
    assert "CAPABILITY_GAP" not in out


@pytest.mark.asyncio
async def test_record_event_requires_title_or_content(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(executor, "record_openaev_event", run_id="r1", type="NARRATION")
    assert "title and/or content" in out


@pytest.mark.asyncio
async def test_record_event_happy_path_posts_expected_body(monkeypatch):
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        captured.update(method=method, path=path, body=json)
        return _FakeResponse({"ok": True}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    # A PROOF must be backed by at least one finding (no proof without a finding).
    proof_data = '{"host": "dc01", "findings": [{"finding_id": "f1", "type": "credentials"}]}'
    out = await _run(
        executor,
        "record_openaev_event",
        run_id="r1",
        type="proof",  # lower-case must be normalised
        title="Domain admin",
        content="Proven",
        data=proof_data,
    )
    assert captured["method"] == "post"
    assert captured["path"] == "/api/autonomous-runs/r1/events"
    assert captured["body"] == {
        "type": "PROOF",
        "title": "Domain admin",
        "content": "Proven",
        "data": proof_data,
    }
    assert "Recorded PROOF" in out


@pytest.mark.asyncio
async def test_record_event_strips_null_bytes_from_fields(monkeypatch):
    # OpenAEV stores data in a jsonb column and title/content in text columns; a
    # NUL byte (\u0000) fails the write and loses the event. Strip it from every
    # field before the request leaves.
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        captured.update(body=json)
        return _FakeResponse({"ok": True}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor,
        "record_openaev_event",
        run_id="r1",
        type="NARRATION",
        title="Owned\u0000host",
        content="creds\u0000found",
        data='{"k": "v\u0000x"}',
    )
    assert "\u0000" not in captured["body"]["title"]
    assert "\u0000" not in captured["body"]["content"]
    assert "\u0000" not in captured["body"]["data"]
    assert captured["body"]["title"] == "Ownedhost"
    assert "Recorded NARRATION" in out


@pytest.mark.asyncio
async def test_record_event_rejects_non_json_data(monkeypatch):
    # data lands in a jsonb column, so a non-JSON value must be rejected
    # client-side for EVERY event type (not just PROOF), before the doomed write.
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(
        executor,
        "record_openaev_event",
        run_id="r1",
        type="DECISION",
        title="x",
        data="not valid json",
    )
    assert "valid JSON" in out


@pytest.mark.asyncio
async def test_record_event_rejects_non_object_json_data(monkeypatch):
    # data lands in a jsonb column that accepts any JSON, but the tool contract
    # and the timeline consumers expect key/value metadata. A bare array (valid
    # JSON, not an object) must be rejected client-side for EVERY event type.
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(
        executor,
        "record_openaev_event",
        run_id="r1",
        type="DECISION",
        title="x",
        data='["not", "an", "object"]',
    )
    assert "JSON OBJECT" in out


@pytest.mark.asyncio
async def test_record_event_strips_leaked_tool_markup(monkeypatch):
    """The model sometimes leaks its tool-call framing into the content string; that raw XML must
    never reach the operator's reasoning panel, so title/content are cut at the first such marker."""
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        captured.update(body=json)
        return _FakeResponse({"ok": True}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    leaked = (
        "Built the live attack path and evaluated it.</content>\n"
        '<invoke name="record_openaev_event">\n'
        '<parameter name="run_id">r1</parameter>\n'
        '<parameter name="type">GAP</parameter>'
    )
    out = await _run(
        executor,
        "record_openaev_event",
        run_id="r1",
        type="NARRATION",
        content=leaked,
    )
    assert captured["body"]["content"] == "Built the live attack path and evaluated it."
    assert "<invoke" not in captured["body"]["content"]
    assert "</content>" not in captured["body"]["content"]
    assert "Recorded NARRATION" in out


@pytest.mark.asyncio
async def test_evaluate_attack_path_surfaces_step_error(monkeypatch):
    """A step that ERRORs (e.g. 'Email needs at least one user') must be surfaced by evaluate, with
    a warning, so the orchestrator cannot mistake a delivery failure for an in-flight step."""

    async def fake_api(base_url, headers, method, path, action, **kw):
        if path.endswith("/attack-path/state"):
            return _FakeResponse(
                [
                    {
                        "title": "Spearphishing lure",
                        "status": "ERROR",
                        "inject_id": "i1",
                        "traces": ["Email needs at least one user"],
                    }
                ]
            ), None
        return _FakeResponse({}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(executor, "evaluate_openaev_attack_path", run_id="r1")
    assert "FAILED" in out
    assert "Email needs at least one user" in out


@pytest.mark.asyncio
async def test_update_status_rejects_operator_only_states(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    # CREATED / PAUSED / CANCELED are operator-driven, not orchestrator-driven.
    out = await _run(executor, "update_openaev_run_status", run_id="r1", status="PAUSED")
    assert "Unknown status" in out


@pytest.mark.asyncio
async def test_update_status_happy_path(monkeypatch):
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        captured.update(path=path, body=json)
        return _FakeResponse({}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor,
        "update_openaev_run_status",
        run_id="r1",
        status="waiting_input",
        content="Which web apps are in scope?",
    )
    assert captured["path"] == "/api/autonomous-runs/r1/status"
    assert captured["body"] == {
        "status": "WAITING_INPUT",
        "content": "Which web apps are in scope?",
    }
    assert "WAITING_INPUT" in out


@pytest.mark.asyncio
async def test_update_status_reports_ignored_noop_transition(monkeypatch):
    # OpenAEV answers 200 with the UNCHANGED run on an illegal / stale / terminal
    # transition. The tool must read the returned autonomous_run_status back and
    # report honestly instead of claiming the transition applied.
    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        return _FakeResponse({"autonomous_run_status": "RUNNING"}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor, "update_openaev_run_status", run_id="r1", status="COMPLETED"
    )
    assert "still RUNNING" in out
    assert "did NOT apply" in out
    assert "COMPLETED" in out


@pytest.mark.asyncio
async def test_update_status_benign_noop_already_in_state_is_ok(monkeypatch):
    # Being already in the requested state is a benign no-op, not an error - the
    # returned status matches, so the tool reports success.
    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        return _FakeResponse({"autonomous_run_status": "WAITING_INPUT"}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor, "update_openaev_run_status", run_id="r1", status="WAITING_INPUT"
    )
    assert "status set to WAITING_INPUT" in out
    assert "did NOT apply" not in out


@pytest.mark.asyncio
async def test_update_status_unreadable_body_degrades_to_success(monkeypatch):
    # OpenAEV normally returns the run object as JSON, but a 200 with an empty or
    # non-JSON body must NOT turn a successful transition into a raised exception.
    # The read-back is best-effort: when the body cannot be parsed the tool
    # degrades to reporting success (the transition itself succeeded).
    class _BadJsonResponse:
        def json(self):
            raise ValueError("no JSON body")

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        return _BadJsonResponse(), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor, "update_openaev_run_status", run_id="r1", status="COMPLETED"
    )
    assert "status set to COMPLETED" in out
    assert "did NOT apply" not in out


@pytest.mark.asyncio
async def test_consume_directives_formats_pending(monkeypatch):
    async def fake_api(base_url, headers, method, path, action, **kw):
        return (
            _FakeResponse(
                [
                    {"autonomous_directive_content": "focus on finance subnet"},
                    {"autonomous_directive_content": "avoid host X"},
                ]
            ),
            None,
        )

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(executor, "consume_openaev_directives", run_id="r1")
    assert "2 pending operator directive(s)" in out
    assert "focus on finance subnet" in out
    assert "avoid host X" in out


@pytest.mark.asyncio
async def test_consume_directives_none(monkeypatch):
    async def fake_api(base_url, headers, method, path, action, **kw):
        return _FakeResponse([]), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(executor, "consume_openaev_directives", run_id="r1")
    assert out == "No pending operator directives."


@pytest.mark.asyncio
async def test_resolve_capabilities_requires_a_query(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(executor, "resolve_openaev_capabilities")
    assert "Provide at least one of" in out


@pytest.mark.asyncio
async def test_author_attack_path_step_requires_run_id(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(executor, "author_openaev_attack_path_step", title="Recon", injector_contract_id="c1")
    assert "run_id is required" in out


@pytest.mark.asyncio
async def test_author_attack_path_step_requires_title_and_contract(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(executor, "author_openaev_attack_path_step", run_id="r1", title="Recon")
    assert "injector_contract_id are required" in out


@pytest.mark.asyncio
async def test_author_attack_path_step_root_posts_expected_body(monkeypatch):
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        captured.update(method=method, path=path, body=json)
        return _FakeResponse({"step_template_id": "st-1"}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="Port scan",
        injector_contract_id="c-nmap",
        content='{"args": "-sV"}',
    )
    assert captured["method"] == "post"
    assert captured["path"] == "/api/autonomous-runs/r1/attack-path/steps"
    # Root step: no parent_step_template_id in the body.
    assert "parent_step_template_id" not in captured["body"]
    assert captured["body"]["inject"] == {
        "inject_title": "Port scan",
        "inject_injector_contract": "c-nmap",
        "inject_enabled": True,
        "inject_content": {"args": "-sV"},
        "inject_all_teams": False,
    }
    assert "step_template_id=st-1" in out
    assert "seed step" in out


@pytest.mark.asyncio
async def test_author_attack_path_step_chains_parent(monkeypatch):
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        captured.update(body=json)
        return _FakeResponse({"step_template_id": "st-2"}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="Exploit",
        injector_contract_id="c-http",
        parent_step_template_id="st-1",
    )
    assert captured["body"]["parent_step_template_id"] == "st-1"
    assert "ordered after step st-1" in out


@pytest.mark.asyncio
async def test_author_attack_path_step_finding_driven_trigger(monkeypatch):
    """A finding-driven step normalises its trigger (upper-cased operator/mapping, primitive
    aliases folded) and posts it under 'trigger' - never a parent - so the path draws itself."""
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        captured.update(body=json)
        return _FakeResponse({"step_template_id": "st-9"}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="SMB share enum",
        injector_contract_id="c-nxc",
        trigger={
            "event_name": "SMB service exposed",
            "match": "and",
            "filters": [{"key_type": "port", "operator": "eq", "value": "445"}],
            "mappings": [
                {"input_key": "host", "key_type": "host"},
                {"input_key": "creds", "key_type": "credentials", "mapping_type": "global"},
            ],
        },
    )
    assert "parent_step_template_id" not in captured["body"]
    assert captured["body"]["trigger"] == {
        # event_name titles the event node in the Logic graph (never "Untitled event").
        "event_name": "SMB service exposed",
        "match": "AND",
        "filters": [{"key_type": "port", "operator": "EQ", "value": "445"}],
        "mappings": [
            {"input_key": "host", "key_type": "host"},
            # 'credentials' alias folds onto its primary primitive 'username'.
            {"input_key": "creds", "key_type": "username", "mapping_type": "GLOBAL"},
        ],
    }
    assert "finding-driven" in out


@pytest.mark.asyncio
async def test_author_attack_path_step_rejects_bad_trigger_key_type(monkeypatch):
    """An unknown primitive in a trigger is rejected client-side with a helpful message, before any
    API call, so the agent can correct it instead of getting an opaque 400."""
    called = {"n": 0}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        called["n"] += 1
        return _FakeResponse({"step_template_id": "st-x"}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="Bad",
        injector_contract_id="c1",
        trigger={"filters": [{"key_type": "not_a_primitive"}]},
    )
    assert called["n"] == 0
    assert "unknown key_type" in out


@pytest.mark.asyncio
async def test_author_attack_path_step_accepts_email_trigger_primitive(monkeypatch):
    """The 'email' finding primitive (OpenAEV PrimitiveType.email) is a valid trigger
    key_type, so an email-driven step (a follow-up that fires on a harvested address)
    is accepted client-side instead of being rejected as an unknown primitive."""
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        captured.update(body=json)
        return _FakeResponse({"step_template_id": "st-mail"}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="Follow-up on harvested address",
        injector_contract_id="c-mail",
        trigger={
            "event_name": "Email address harvested",
            "filters": [{"key_type": "email", "operator": "eq", "value": "v@corp.tld"}],
            "mappings": [{"input_key": "target", "key_type": "email"}],
        },
    )
    assert captured["body"]["trigger"]["filters"] == [
        {"key_type": "email", "operator": "EQ", "value": "v@corp.tld"}
    ]
    assert captured["body"]["trigger"]["mappings"] == [
        {"input_key": "target", "key_type": "email"}
    ]
    assert "finding-driven" in out


@pytest.mark.asyncio
async def test_author_attack_path_step_flags_pending_duplicate(monkeypatch):
    """A second author that comes back with the SAME step_template_id is OpenAEV's pending-step
    idempotent de-duplication: the tool must tell the agent nothing new was created and steer it to
    evaluate instead of looping - without ever blocking the call."""

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        return _FakeResponse({"step_template_id": "st-dup"}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()

    first = await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="Recon",
        injector_contract_id="c1",
    )
    assert "step_template_id=st-dup" in first
    assert "no new step created" not in first

    second = await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="Recon",
        injector_contract_id="c1",
    )
    assert "no new step created" in second
    assert "st-dup" in second
    assert "evaluate_openaev_attack_path" in second


@pytest.mark.asyncio
async def test_author_attack_path_step_rejects_bad_content_json(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="x",
        injector_contract_id="c1",
        content="{not json",
    )
    assert "content must be a valid JSON object string" in out


@pytest.mark.asyncio
async def test_evaluate_attack_path_requires_run_id(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(executor, "evaluate_openaev_attack_path")
    assert "run_id is required" in out


@pytest.mark.asyncio
async def test_evaluate_attack_path_happy_path(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake_api(base_url, headers, method, path, action, **kw):
        calls.append((method, path))
        # The evaluate POST returns nothing; the folded-in state GET returns the live steps so the
        # orchestrator sees what actually happened (statuses + traces) in the same tool result.
        if path.endswith("/attack-path/state"):
            return _FakeResponse([{"title": "Recon", "status": "SUCCESS", "inject_id": "i1", "contract": "c1"}]), None
        return _FakeResponse({}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(executor, "evaluate_openaev_attack_path", run_id="r1")
    # Evaluates first, then reads back the live state so success can never be assumed blindly.
    assert calls[0] == ("post", "/api/autonomous-runs/r1/attack-path/evaluate")
    assert calls[1] == ("get", "/api/autonomous-runs/r1/attack-path/state")
    assert "Evaluated the attack path" in out
    assert "Recon" in out


def test_fmt_capability_report_all_satisfied():
    report = {
        "fully_satisfied": True,
        "resolutions": [
            {
                "token": "T1110",
                "label": "Brute Force",
                "satisfied": True,
                "contracts": [
                    {
                        "injector_contract_id": "c1",
                        "label": "Hydra",
                        "injector_type": "openaev_caldera",
                    }
                ],
            }
        ],
        "gaps": [],
    }
    out = auto_tools._fmt_capability_report(report)
    assert "All queried capabilities are satisfied" in out
    assert "[OK] Brute Force (T1110)" in out
    assert "Hydra" in out


def test_fmt_capability_report_with_gap_and_suggestions():
    report = {
        "fully_satisfied": False,
        "resolutions": [
            {
                "token": "T1566",
                "label": "Phishing",
                "satisfied": False,
                "suggested_connectors": [{"title": "Phishing Simulator", "subscription_link": "https://x"}],
            }
        ],
        "gaps": [{"token": "T1566"}],
    }
    out = auto_tools._fmt_capability_report(report)
    assert "1 of 1 queried capabilities are NOT satisfied" in out
    assert "[GAP] Phishing (T1566)" in out
    assert "Phishing Simulator" in out


def test_fmt_capability_report_handles_empty():
    assert "resolved to nothing" in auto_tools._fmt_capability_report({"resolutions": []})
    assert "No capability report" in auto_tools._fmt_capability_report(None)


def _fail_if_called():
    async def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("_api should not be called for invalid input")

    return _boom


@pytest.mark.asyncio
async def test_record_proof_without_findings_is_rejected(monkeypatch):
    # No valid proof without an associated finding: a PROOF must carry a non-empty
    # "findings" array, so it is rejected before any HTTP call when it is missing.
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(executor, "record_openaev_event", run_id="r1", type="PROOF", title="Owned")
    assert "at least one finding" in out

    out_empty = await _run(
        executor,
        "record_openaev_event",
        run_id="r1",
        type="PROOF",
        title="Owned",
        data='{"findings": []}',
    )
    assert "at least one finding" in out_empty


@pytest.mark.asyncio
async def test_record_proof_with_findings_posts(monkeypatch):
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, **kw):
        captured.update(method=method, path=path, json=kw.get("json"))
        return _FakeResponse({}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor,
        "record_openaev_event",
        run_id="r1",
        type="PROOF",
        title="Owned",
        data='{"findings": [{"finding_id": "f1", "type": "credentials", "value": "root"}]}',
    )
    assert captured["method"] == "post"
    assert captured["path"] == "/api/autonomous-runs/r1/events"
    assert "Recorded PROOF" in out


@pytest.mark.asyncio
async def test_read_attack_path_state_requires_run_id(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(executor, "read_openaev_attack_path_state")
    assert "run_id is required" in out


@pytest.mark.asyncio
async def test_read_attack_path_state_formats_steps(monkeypatch):
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, **kw):
        captured.update(method=method, path=path)
        return (
            _FakeResponse(
                [
                    {
                        "inject_id": "i1",
                        "title": "Nmap recon",
                        "status": "SUCCESS",
                        "injector_contract_id": "c1",
                        "traces": ["COMMAND/SUCCESS: 3 hosts up"],
                    }
                ]
            ),
            None,
        )

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(executor, "read_openaev_attack_path_state", run_id="r1")
    assert captured["method"] == "get"
    assert captured["path"] == "/api/autonomous-runs/r1/attack-path/state"
    assert "[SUCCESS] Nmap recon" in out
    assert "trace: COMMAND/SUCCESS: 3 hosts up" in out


@pytest.mark.asyncio
async def test_read_attack_path_state_empty_nudges_small_batch(monkeypatch):
    async def fake_api(base_url, headers, method, path, action, **kw):
        return _FakeResponse([]), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(executor, "read_openaev_attack_path_state", run_id="r1")
    assert "No steps authored yet" in out


@pytest.mark.asyncio
async def test_promote_finding_requires_ids(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(executor, "promote_openaev_finding_to_asset", run_id="r1")
    assert "finding_id are required" in out


@pytest.mark.asyncio
async def test_promote_finding_happy_path(monkeypatch):
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, **kw):
        captured.update(method=method, path=path)
        return _FakeResponse({"asset_id": "a1", "asset_name": "web-01", "finding_id": "f1"}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(executor, "promote_openaev_finding_to_asset", run_id="r1", finding_id="f1")
    assert captured["method"] == "post"
    assert captured["path"] == "/api/autonomous-runs/r1/findings/f1/promote-to-asset"
    assert "asset_id=a1" in out
    assert 'asset_ids=["a1"]' in out


@pytest.mark.asyncio
async def test_ensure_target_team_requires_players(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(executor, "ensure_openaev_target_team", run_id="r1")
    assert "player_ids is required" in out


@pytest.mark.asyncio
async def test_ensure_target_team_happy_path(monkeypatch):
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, **kw):
        captured.update(method=method, path=path, json=kw.get("json"))
        return (
            _FakeResponse({"team_id": "t1", "team_name": "AI target sam@corp", "player_ids": ["u1"]}),
            None,
        )

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(executor, "ensure_openaev_target_team", run_id="r1", player_ids=["u1"])
    assert captured["method"] == "post"
    assert captured["path"] == "/api/autonomous-runs/r1/target-teams"
    assert captured["json"] == {"player_ids": ["u1"]}
    assert "team_id=t1" in out
    assert 'team_ids=["t1"]' in out


@pytest.mark.asyncio
async def test_get_run_scope_requires_run_id(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(executor, "get_openaev_run_scope")
    assert "run_id is required" in out


@pytest.mark.asyncio
async def test_get_run_scope_reads_and_renders_allow_and_deny(monkeypatch):
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, **kw):
        captured.update(method=method, path=path)
        return (
            _FakeResponse(
                {
                    "run_id": "r1",
                    "allowlist": [
                        {"source": "ASSET", "type": "ASSETS", "id": "a1", "name": "DESKTOP-1"},
                        {"source": "TEAM", "type": "TEAMS", "id": "t1", "name": "Finance"},
                    ],
                    "denylist": [
                        {"source": "MANUAL", "type": "MANUAL", "id": "10.0.0.1", "name": "10.0.0.1"},
                    ],
                }
            ),
            None,
        )

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(executor, "get_openaev_run_scope", run_id="r1")
    assert captured["method"] == "get"
    assert captured["path"] == "/api/autonomous-runs/r1/scope"
    assert "ALLOW-LIST" in out
    assert "DENY-LIST" in out
    assert "DESKTOP-1 (a1)" in out
    assert "Finance (t1)" in out
    assert "10.0.0.1" in out


@pytest.mark.asyncio
async def test_get_run_scope_empty_prompts_for_targets(monkeypatch):
    async def fake_api(base_url, headers, method, path, action, **kw):
        return (_FakeResponse({"run_id": "r1", "allowlist": [], "denylist": []}), None)

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(executor, "get_openaev_run_scope", run_id="r1")
    assert "EMPTY" in out
    assert "set_openaev_run_scope" in out


# --------------------------------------------------------------------------- #
# Transient 5xx retry opt-in (the wasted-retry storm fix)                      #
# --------------------------------------------------------------------------- #
#
# OpenAEV occasionally answers a transient 5xx (a 500 on set_openaev_run_scope
# was the production incident) on an IDEMPOTENT call that then succeeds on
# retry. The orchestrator re-issued the failed tool 6x in a row, burning
# iterations and tokens. The fix: the idempotent autonomous callbacks opt into
# the shared HTTP-layer transient retry (retry_transient=True) so a blip
# resolves inside one tool call. NON-idempotent mutations (record event,
# consume directives) must NOT opt in - a double-apply would double-post an
# event or silently drop a directive.


def _capture_api(calls: list[dict], payload: object = None):
    async def _fake(base_url, headers, method, path, action, **kw):
        calls.append(
            {
                "method": method,
                "path": path,
                "retry_transient": kw.get("retry_transient", False),
            }
        )
        return _FakeResponse({} if payload is None else payload), None

    return _fake


@pytest.mark.parametrize(
    "name,args",
    [
        (
            "author_openaev_attack_path_step",
            {"run_id": "r1", "title": "Recon seed", "injector_contract_id": "c1"},
        ),
        (
            "update_openaev_attack_path_step",
            {
                "run_id": "r1",
                "step_template_id": "st-1",
                "title": "Fix payload",
                "injector_contract_id": "c1",
            },
        ),
        ("read_openaev_attack_path_state", {"run_id": "r1"}),
        ("evaluate_openaev_attack_path", {"run_id": "r1"}),
        ("get_openaev_run_scope", {"run_id": "r1"}),
        ("resolve_openaev_capabilities", {"output_types": ["credentials"]}),
    ],
)
@pytest.mark.asyncio
async def test_idempotent_autonomous_calls_opt_into_transient_retry(monkeypatch, name, args):
    # Every _api call these idempotent tools make must set retry_transient=True so a
    # transient upstream 5xx is retried at the HTTP layer instead of surfacing an
    # error the orchestrator re-issues many times. evaluate also reads state back,
    # so BOTH of its calls are checked by the "all calls" assertion.
    calls: list[dict] = []
    monkeypatch.setattr(
        auto_tools, "_api", _capture_api(calls, {"step_template_id": "st-1"})
    )
    executor, _ = _build()
    await _run(executor, name, **args)
    assert calls, f"{name} made no _api call"
    assert all(c["retry_transient"] for c in calls), (
        f"{name} must opt every _api call into transient-5xx retry; got {calls}"
    )


@pytest.mark.asyncio
async def test_set_run_scope_opts_put_and_reread_into_transient_retry(monkeypatch):
    # set_openaev_run_scope does a PUT (the exact scope-500 the operator hit) then a
    # confirming GET re-read; both must be resilient to a transient 5xx.
    calls: list[dict] = []
    monkeypatch.setattr(
        auto_tools, "_api", _capture_api(calls, {"allowlist": [], "denylist": []})
    )
    executor, _ = _build()
    await _run(executor, "set_openaev_run_scope", run_id="r1", asset_ids=["a1"])
    puts = [c for c in calls if c["method"] == "put"]
    gets = [c for c in calls if c["method"] == "get"]
    assert puts and all(c["retry_transient"] for c in puts)
    assert gets and all(c["retry_transient"] for c in gets)


@pytest.mark.parametrize(
    "name,args,payload",
    [
        ("record_openaev_event", {"run_id": "r1", "type": "NARRATION", "title": "hi"}, {}),
        ("consume_openaev_directives", {"run_id": "r1"}, []),
    ],
)
@pytest.mark.asyncio
async def test_non_idempotent_autonomous_calls_do_not_retry_transient(
    monkeypatch, name, args, payload
):
    # A double-applied record/consume would double-post an event or drop a
    # directive, so these NON-idempotent mutations must NOT opt into retry.
    calls: list[dict] = []
    monkeypatch.setattr(auto_tools, "_api", _capture_api(calls, payload))
    executor, _ = _build()
    await _run(executor, name, **args)
    assert calls
    assert not any(c["retry_transient"] for c in calls), (
        f"{name} is non-idempotent and must NOT retry a transient 5xx; got {calls}"
    )


@pytest.mark.asyncio
async def test_author_step_rejects_valueless_comparison_operator(monkeypatch):
    # A comparison operator with no value (e.g. `port EQ` with nothing to match) is
    # malformed - OpenAEV would reject it or the branch would never fire - so it is
    # rejected client-side with an actionable message, before any HTTP call.
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="Bad filter",
        injector_contract_id="c1",
        trigger={"filters": [{"key_type": "port", "operator": "EQ"}]},
    )
    assert "needs a" in out and "value" in out


@pytest.mark.asyncio
async def test_author_step_accepts_is_not_null_without_value(monkeypatch):
    # The value-required check must NOT over-reject: IS_NOT_NULL (and IS_NULL) take
    # no value and stay valid - an operator-less filter defaults to IS_NOT_NULL too.
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        captured.update(body=json)
        return _FakeResponse({"step_template_id": "st-nn"}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="Fire on any port finding",
        injector_contract_id="c1",
        trigger={
            "event_name": "Port discovered",
            "filters": [{"key_type": "port", "operator": "IS_NOT_NULL"}],
        },
    )
    assert captured["body"]["trigger"]["filters"] == [
        {"key_type": "port", "operator": "IS_NOT_NULL"}
    ]
    assert "finding-driven" in out


def test_degrade_after_transient_retry_appends_antispin_hint_to_5xx():
    # A 5xx that survived the HTTP-layer retry gets a one-sentence "don't re-fire"
    # hint (the anti-spin half of the fix); a non-5xx error is returned untouched.
    from app.integrations.providers.openaev import _common

    hinted = _common._degrade_after_transient_retry(
        "Error: OpenAEV server error (500) while trying to set run scope. "
        "The service may be temporarily unavailable."
    )
    assert "already retried automatically" in hinted
    assert "immediately re-issue" in hinted

    unchanged = "Error: OpenAEV rejected the request to author - bad request."
    assert _common._degrade_after_transient_retry(unchanged) == unchanged


# --------------------------------------------------------------------------- #
# State read is finding-driven-first and surfaces each step's WIRING (item 1)   #
# --------------------------------------------------------------------------- #


def _fake_state(steps):
    async def _fake(base_url, headers, method, path, action, **kw):
        return _FakeResponse(steps), None

    return _fake


@pytest.mark.asyncio
async def test_state_render_is_finding_driven_first_not_linear_parent(monkeypatch):
    # The state read is appended to read_/evaluate_ EVERY cycle and is read FIRST,
    # so it must teach finding-driven extension, NOT the linear-parent chaining that
    # caused the "NMAP finds 445, nothing triggers netexec" regression. The old
    # header instruction ("author a new step with parent_step_template_id = an
    # existing step_template_id") must be gone; the finding-driven + consumption
    # guidance must be present.
    steps = [
        {"step_template_id": "st-seed", "title": "Nmap sweep", "status": "SUCCESS"},
        {
            "step_template_id": "st-smb",
            "title": "NetExec over SMB",
            "status": "PENDING",
            "event_name": "SMB service exposed",
            "trigger_filters": [{"key_type": "port", "operator": "EQ", "value": "445"}],
            "trigger_mappings": [{"input_key": "host", "key_type": "host"}],
        },
        {
            "step_template_id": "st-order",
            "title": "Cleanup",
            "status": "PENDING",
            "parent_step_template_id": "st-seed",
        },
    ]
    monkeypatch.setattr(auto_tools, "_api", _fake_state(steps))
    executor, _ = _build()
    out = await _run(executor, "read_openaev_attack_path_state", run_id="r1")
    # The superseded linear-parent instruction must NOT reappear.
    assert "author a new step with parent_step_template_id = an existing" not in out
    assert "To EXTEND the chain, author a new step with parent_step_template_id" not in out
    # Finding-driven-first guidance + the consumption rule the user's regression needs.
    assert "finding-driven step whose trigger={event_name, filters, mappings}" in out
    assert "Every discovery MUST have at least one downstream step" in out
    assert "pure ordering that consumes NO findings" in out
    # Per-step WIRING labels distinguish seed / finding-driven / ordering.
    assert "seed (fires immediately vs the run scope)" in out
    assert 'finding-driven on "SMB service exposed"' in out
    assert "port EQ 445" in out  # compact filter summary
    assert "host<-host" in out  # compact mapping summary
    assert "ordering (after st-seed)" in out
    # A finding-driven step must NEVER render as a bare seed indistinguishable from one.
    assert "[PENDING] NetExec over SMB | finding-driven" in out


@pytest.mark.asyncio
async def test_state_render_inert_safe_without_trigger_dto_fields(monkeypatch):
    # The trigger fields on the state DTO land in OpenAEV PR #7473. Until then a
    # finding-driven step carries none of them here, so the render must NOT break -
    # it degrades to the seed/ordering label (inert-safe) and lights up once the
    # DTO fields arrive.
    steps = [{"step_template_id": "st1", "title": "Recon", "status": "SUCCESS"}]
    monkeypatch.setattr(auto_tools, "_api", _fake_state(steps))
    executor, _ = _build()
    out = await _run(executor, "read_openaev_attack_path_state", run_id="r1")
    assert "[SUCCESS] Recon | seed (fires immediately vs the run scope)" in out


@pytest.mark.asyncio
async def test_state_render_caps_and_truncates_traces(monkeypatch):
    # A long path with verbose traces must not balloon the per-cycle prompt (item 14):
    # cap the traces shown per step and truncate each one.
    long_trace = "X" * 500
    steps = [
        {
            "step_template_id": "st1",
            "title": "Noisy",
            "status": "SUCCESS",
            "traces": [f"t{i}" for i in range(9)] + [long_trace],
        }
    ]
    monkeypatch.setattr(auto_tools, "_api", _fake_state(steps))
    executor, _ = _build()
    out = await _run(executor, "read_openaev_attack_path_state", run_id="r1")
    assert "(+5 more)" in out  # 10 traces, cap 5 -> 5 shown + "(+5 more)"
    assert "... (truncated)" not in out  # the long trace is beyond the cap, never shown
    # Only the first 5 traces are rendered.
    assert "trace: t4" in out
    assert "trace: t5" not in out


def test_state_wiring_helpers_are_pure_and_inert_safe():
    # The compact summaries used by the state render, exercised directly.
    assert (
        auto_tools._summarize_trigger_filters(
            [{"key_type": "port", "operator": "eq", "value": "445"}]
        )
        == "port EQ 445"
    )
    # No operator in the DTO summary -> shown as IS_NOT_NULL (fire-on-exists).
    assert (
        auto_tools._summarize_trigger_filters([{"key_type": "host"}]) == "host IS_NOT_NULL"
    )
    assert (
        auto_tools._summarize_trigger_mappings(
            [{"input_key": "host", "key_type": "host"}, {"input_key": "p", "key_type": "port"}]
        )
        == "host<-host, p<-port"
    )
    # Inert-safe: junk / missing -> empty string, never raises.
    assert auto_tools._summarize_trigger_filters(None) == ""
    assert auto_tools._summarize_trigger_mappings("nope") == ""
    assert auto_tools._step_wiring({}) == "seed (fires immediately vs the run scope)"
    assert auto_tools._step_wiring({"parent_step_template_id": "p1"}) == "ordering (after p1)"
    assert auto_tools._step_wiring({"event_name": "X"}).startswith("finding-driven")


def test_state_wiring_helpers_accept_openaev_string_shape():
    # THE REAL OpenAEV DTO SHAPE (OpenAEV PR #7473): AutonomousAttackPathStepState
    # declares trigger_filters / trigger_mappings as List<String> whose elements are
    # ALREADY pre-rendered ("port EQ 445", "ipv4 -> target_host"). The summarizers
    # must accept those strings verbatim (not only the write-side dict shape), or the
    # whole filter/mapping DETAIL - the causal wiring item 1b exists to surface - is
    # silently dropped against the real response.
    assert (
        auto_tools._summarize_trigger_filters(["port EQ 445", "service IS_NOT_NULL"])
        == "port EQ 445, service IS_NOT_NULL"
    )
    assert (
        auto_tools._summarize_trigger_mappings(["ipv4 -> target_host"])
        == "ipv4 -> target_host"
    )
    # Empty / blank string elements are skipped (never emitted as bare separators).
    assert auto_tools._summarize_trigger_filters(["port EQ 445", "", "  "]) == "port EQ 445"
    assert auto_tools._summarize_trigger_mappings([""]) == ""
    # _step_wiring surfaces the string-shaped detail alongside the event name.
    wiring = auto_tools._step_wiring(
        {
            "event_name": "SMB service exposed",
            "trigger_filters": ["port EQ 445"],
            "trigger_mappings": ["ipv4 -> target_host"],
        }
    )
    assert 'finding-driven on "SMB service exposed"' in wiring
    assert "port EQ 445" in wiring
    assert "ipv4 -> target_host" in wiring


@pytest.mark.asyncio
async def test_state_render_surfaces_string_shaped_trigger_detail(monkeypatch):
    # End-to-end against the REAL OpenAEV response shape: a step whose
    # trigger_filters / trigger_mappings are pre-rendered STRINGS (List<String>)
    # must render the causal predicate detail, not just the bare event label. This is
    # the cross-repo seam that was silently dropping the detail before the string-shape fix.
    steps = [
        {
            "step_template_id": "st-smb",
            "title": "NetExec over SMB",
            "status": "PENDING",
            "event_name": "SMB service exposed",
            "trigger_filters": ["port EQ 445"],
            "trigger_mappings": ["ipv4 -> target_host"],
        }
    ]
    monkeypatch.setattr(auto_tools, "_api", _fake_state(steps))
    executor, _ = _build()
    out = await _run(executor, "read_openaev_attack_path_state", run_id="r1")
    assert 'finding-driven on "SMB service exposed"' in out
    assert "filters: port EQ 445" in out
    assert "maps: ipv4 -> target_host" in out


# --------------------------------------------------------------------------- #
# update tool: finding-driven description + trigger edit + phase-gate relief    #
# --------------------------------------------------------------------------- #


def test_update_tool_description_is_finding_driven_not_linear_parent():
    # The permanently-in-context contradiction (item 2): the update description used
    # to say "to EXTEND the chain use author_openaev_attack_path_step with
    # parent_step_template_id", re-teaching the linear model every cycle. It must now
    # teach finding-driven extension and the in-place trigger edit.
    executor = CallableToolExecutor()
    defs = auto_tools.register("https://oaev.example", {}, executor)
    desc = next(d.description for d in defs if d.name == "update_openaev_attack_path_step")
    assert "to EXTEND the chain use author_openaev_attack_path_step with" not in desc
    assert "parent_step_template_id" in desc  # still named, but only for pure ordering
    assert "pure ordering that consumes no findings" in desc
    assert "finding-driven" in desc
    assert "trigger" in desc  # the in-place trigger edit is taught
    # The update tool schema now accepts a trigger.
    schema = next(
        d.input_schema for d in defs if d.name == "update_openaev_attack_path_step"
    )
    assert "trigger" in schema["properties"]


@pytest.mark.asyncio
async def test_update_step_posts_cleaned_trigger_and_relieves_gate(monkeypatch):
    # update can CORRECT a mis-wired step in place: the trigger routes through the
    # same _clean_trigger as authoring, and a successful update relieves the
    # step-before-more phase gate (item 11) so a fork-then-update loop is not wedged.
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        captured.update(method=method, path=path, body=json)
        return _FakeResponse({}), None

    gate_calls = {"n": 0}

    def fake_record():
        gate_calls["n"] += 1

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    monkeypatch.setattr(
        "app.services.builtin_tools.filtering.record_orchestrator_step_authored",
        fake_record,
    )
    executor, _ = _build()
    out = await _run(
        executor,
        "update_openaev_attack_path_step",
        run_id="r1",
        step_template_id="st-1",
        title="NetExec over SMB",
        injector_contract_id="c-nxc",
        trigger={
            "event_name": "SMB service exposed",
            "filters": [{"key_type": "port", "value": "445"}],  # no operator -> EQ
            "mappings": [{"input_key": "host", "key_type": "host"}],
        },
    )
    assert captured["method"] == "put"
    assert captured["path"] == "/api/autonomous-runs/r1/attack-path/steps/st-1"
    # The trigger is cleaned and posted alongside the inject.
    assert captured["body"]["trigger"]["event_name"] == "SMB service exposed"
    assert captured["body"]["trigger"]["filters"] == [
        {"key_type": "port", "operator": "EQ", "value": "445"}
    ]
    assert gate_calls["n"] == 1  # phase gate relieved on the successful update
    assert "trigger was updated" in out


# --------------------------------------------------------------------------- #
# delete tool (inert-safe; OpenAEV DELETE endpoint lands in PR #7473)           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete_step_requires_ids(monkeypatch):
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(executor, "delete_openaev_attack_path_step", run_id="r1")
    assert "run_id and step_template_id are required" in out


@pytest.mark.asyncio
async def test_delete_step_happy_path(monkeypatch):
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, **kw):
        captured.update(method=method, path=path, retry=kw.get("retry_transient"))
        return _FakeResponse({}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor, "delete_openaev_attack_path_step", run_id="r1", step_template_id="st-9"
    )
    assert captured["method"] == "delete"
    assert captured["path"] == "/api/autonomous-runs/r1/attack-path/steps/st-9"
    assert captured["retry"] is True  # idempotent -> transient retry
    assert "Deleted attack-path step st-9" in out


def test_delete_tool_in_orchestrator_allow_list():
    # A registered tool the orchestrator can never call is dead weight; the delete
    # tool must be in the orchestrator's OpenAEV allow-list (BASE - editing logic is
    # valid in build and live modes, like update).
    from app.services.autonomous_pentest import _ORCHESTRATOR_OPENAEV_TOOLS_BASE

    assert "delete_openaev_attack_path_step" in _ORCHESTRATOR_OPENAEV_TOOLS_BASE


# --------------------------------------------------------------------------- #
# author schema hard rules (item 3) + inject object guard (item 10)            #
# --------------------------------------------------------------------------- #


def test_author_description_carries_consumption_and_precondition_rules():
    executor = CallableToolExecutor()
    defs = auto_tools.register("https://oaev.example", {}, executor)
    desc = next(
        d.description for d in defs if d.name == "author_openaev_attack_path_step"
    )
    assert "CONSUMPTION" in desc
    assert "PRECONDITION IS NOT A SEED" in desc
    assert "seeded beside it" in desc  # the exact anti-pattern


@pytest.mark.asyncio
async def test_author_step_rejects_non_object_content(monkeypatch):
    # inject_content is a server ObjectNode: a bare array is valid JSON but 400s
    # opaquely, so it is rejected client-side with an actionable message (item 10).
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="x",
        injector_contract_id="c1",
        content='["not", "an", "object"]',
    )
    assert "JSON OBJECT string" in out


@pytest.mark.asyncio
async def test_build_inject_strips_null_bytes_from_title_and_description(monkeypatch):
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        captured.update(body=json)
        return _FakeResponse({"step_template_id": "st1"}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="SMB\u0000enum",
        injector_contract_id="c1",
        description="finds\u0000shares",
    )
    assert captured["body"]["inject"]["inject_title"] == "SMBenum"
    assert captured["body"]["inject"]["inject_description"] == "findsshares"


# --------------------------------------------------------------------------- #
# _clean_trigger: default EQ when a value is present + reject an out-of-set match #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_trigger_value_without_operator_defaults_to_eq(monkeypatch):
    # A concrete value with no operator would be sent operator-less, and OpenAEV
    # defaults a leaf to IS_NOT_NULL - which IGNORES the value and fires on ANY
    # finding. Default to EQ so the value the model supplied is actually compared.
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        captured.update(body=json)
        return _FakeResponse({"step_template_id": "st1"}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="SMB",
        injector_contract_id="c1",
        trigger={"filters": [{"key_type": "port", "value": "445"}]},
    )
    assert captured["body"]["trigger"]["filters"] == [
        {"key_type": "port", "operator": "EQ", "value": "445"}
    ]


@pytest.mark.asyncio
async def test_trigger_rejects_out_of_set_match(monkeypatch):
    # An out-of-set match must be REJECTED, not silently dropped: dropping "ALL"
    # would flip a step the model meant one way to the server default AND.
    monkeypatch.setattr(auto_tools, "_api", _fail_if_called())
    executor, _ = _build()
    out = await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="x",
        injector_contract_id="c1",
        trigger={"match": "ALL", "filters": [{"key_type": "port", "value": "445"}]},
    )
    assert "must be AND or OR" in out


def test_orchestrator_persona_is_lean_and_keeps_every_rule():
    # XTM One invariant #15 (no prompt inflation): the orchestrator persona was
    # ~63.4K chars (~21K tokens, OVER the 20K large-prompt warning), a plausible
    # cause of the reported "lost the big picture" regression. Keep it materially
    # smaller than that WITHOUT dropping a behavioral rule - each anchor below pins
    # one rule area so a future trim cannot silently delete it.
    from app.agents.seed import _resolve_seed_content

    persona = _resolve_seed_content("agent_persona", "openaev-attack-path-orchestrator")
    assert persona, "orchestrator persona must resolve from seed_data/agent_persona/"
    assert len(persona) < 48_000, (
        f"orchestrator persona regrew to {len(persona)} chars - trim it (invariant #15)"
    )
    assert len(persona) // 3 < 16_000, "persona est. tokens must stay well under the 20K warning"
    for anchor in (
        "OpenAEV Attack Path Orchestrator",  # identity
        "read_openaev_attack_path_state",  # state-first
        "consume_openaev_directives",  # operator steering
        "resolve_openaev_capabilities",  # capability precedence
        "Delivery substrate",  # ground crafted payloads in a real substrate
        "manage_openaev_threat_arsenal",  # arsenal adapt + JIT gate
        "spawn_background_task",  # delegate originals out-of-band
        "FINDING-DRIVEN",  # core authoring model
        "event_name",  # always name the event
        "Untitled event",
        "parent_step_template_id",  # pure ordering only
        "ignite",  # consumption rule
        "TYPED",  # typed output, never text
        "never re-author",  # author vs update
        "set_shared_state",  # control-plane decision signal
        "never conflate them",  # two distinct signals
        "acting_agent_id",  # discovery modes
        "EXPANSIVE",
        "ensure_openaev_target_team",  # person -> team
        "ONE human step per audience",
        "promote_openaev_finding_to_asset",  # lateral movement
        "No proof without a finding",
        "get_openaev_run_scope",  # scope resolution
        "set_openaev_run_scope",
        "Whole authorized environment",
        "AUTHOR SCENARIO",  # planning mode
        "EXECUTE-FIRST",  # prior-plan / scenario seed
        "tradecraft",  # cross-run learning
        "Never name a target",
    ):
        assert anchor in persona, (
            f"orchestrator persona lost the rule anchored by: {anchor!r}"
        )


# --------------------------------------------------------------------------- #
# Event reuse: attach a step to an EXISTING event by id instead of minting a    #
# duplicate (operator gap: "unable to link a step to an existing event"). The   #
# capability is OPTIONAL - seeds and standalone/objective actions are event-    #
# less by design and must NEVER be forced onto an event.                        #
# --------------------------------------------------------------------------- #


def test_clean_trigger_reuse_event_id_keeps_only_mappings():
    # Reusing an existing event drops the event DESCRIPTION (event_name / match /
    # filters) - the event already exists and is linked by id server-side, so
    # re-describing it is redundant and, if it disagreed, misleading. Only the
    # step's own value bindings (mappings) survive.
    clean, err = auto_tools._clean_trigger(
        {
            "event_id": "evt-smb",
            "event_name": "ignored when reusing",
            "match": "OR",
            "filters": [{"key_type": "port", "operator": "EQ", "value": "445"}],
            "mappings": [{"input_key": "host", "key_type": "host"}],
        }
    )
    assert err is None
    assert clean == {
        "event_id": "evt-smb",
        "mappings": [{"input_key": "host", "key_type": "host"}],
    }
    assert "event_name" not in clean
    assert "match" not in clean
    assert "filters" not in clean


def test_clean_trigger_reuse_event_id_alone_is_a_valid_bare_attach():
    # An event_id with no filters and no mappings is a valid BARE attach: the step
    # simply fires on the shared event with no value binding. The "describe
    # something" rule (at least one filter or mapping) applies only when MINTING.
    clean, err = auto_tools._clean_trigger({"event_id": "evt-smb"})
    assert err is None
    assert clean == {"event_id": "evt-smb"}


def test_clean_trigger_empty_without_event_id_points_at_reuse_option():
    # Without an event_id, a trigger carrying neither a filter nor a mapping is
    # meaningless; the error names event_id reuse as one of the ways to fix it.
    clean, err = auto_tools._clean_trigger({"match": "AND"})
    assert clean is None
    assert err is not None
    assert "event_id to reuse an existing event" in err


@pytest.mark.asyncio
async def test_author_step_reuse_event_id_posts_only_event_id_and_mappings(monkeypatch):
    # End-to-end: authoring a second action on the SAME discovery attaches to the
    # existing event by id and posts ONLY {event_id, mappings} - the event
    # description keys are dropped, so the server links (never duplicates) the event.
    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        captured.update(method=method, path=path, body=json)
        return _FakeResponse({"step_template_id": "st-reuse"}), None

    monkeypatch.setattr(auto_tools, "_api", fake_api)
    executor, _ = _build()
    out = await _run(
        executor,
        "author_openaev_attack_path_step",
        run_id="r1",
        title="SMB share dump on the same discovery",
        injector_contract_id="c-nxc2",
        trigger={
            "event_id": "evt-smb",
            "event_name": "should be ignored",
            "filters": [{"key_type": "port", "operator": "EQ", "value": "445"}],
            "mappings": [{"input_key": "host", "key_type": "host"}],
        },
    )
    assert captured["method"] == "post"
    assert captured["body"]["trigger"] == {
        "event_id": "evt-smb",
        "mappings": [{"input_key": "host", "key_type": "host"}],
    }
    # Placed as a finding-driven step (fires on the shared event), never a seed.
    assert "finding-driven" in out


def test_events_overview_rolls_up_distinct_events_with_counts():
    # The overview lists each distinct event ONCE with how many steps fire on it,
    # so the orchestrator can copy an id straight into a new step's trigger.event_id.
    steps = [
        {"step_template_id": "s1", "event_id": "evt-smb", "event_name": "SMB service exposed"},
        {"step_template_id": "s2", "event_id": "evt-smb", "event_name": "SMB service exposed"},
        {"step_template_id": "s3", "event_id": "evt-web", "event_name": "Open web port discovered"},
        {"step_template_id": "s4"},  # event-less (a seed) - skipped, adds no noise
    ]
    out = auto_tools._events_overview(steps)
    assert "EVENTS in this path" in out
    assert "evt-smb (SMB service exposed) <- 2 step(s)" in out
    assert "evt-web (Open web port discovered) <- 1 step(s)" in out


def test_events_overview_empty_for_event_less_or_junk_input():
    # An all-seed / standalone path has no event to share, so the overview is empty
    # (nothing pushes the model to invent an event). Inert-safe on junk input.
    assert auto_tools._events_overview([{"step_template_id": "s1"}]) == ""
    assert auto_tools._events_overview(None) == ""
    assert auto_tools._events_overview("nope") == ""


def test_step_wiring_surfaces_event_id_so_it_can_be_reused():
    # The per-step wiring surfaces the event_id so the orchestrator can attach
    # ANOTHER action to this SAME event (pass it as the next step's trigger.event_id).
    wiring = auto_tools._step_wiring(
        {
            "event_name": "SMB service exposed",
            "event_id": "evt-smb",
            "trigger_filters": ["port EQ 445"],
        }
    )
    assert wiring.startswith("finding-driven")
    assert 'on "SMB service exposed"' in wiring
    assert "event_id: evt-smb" in wiring


@pytest.mark.asyncio
async def test_state_render_includes_events_overview_and_reuse_guidance(monkeypatch):
    # The state read (appended every cycle, read FIRST) must give the orchestrator
    # the "all events" overview and teach OPTIONAL reuse, so it links a shared
    # discovery to one event instead of duplicating it - and does not force an
    # event onto seeds/standalone actions.
    steps = [
        {"step_template_id": "st-seed", "title": "Nmap sweep", "status": "SUCCESS"},
        {
            "step_template_id": "st-smb",
            "title": "NetExec over SMB",
            "status": "PENDING",
            "event_id": "evt-smb",
            "event_name": "SMB service exposed",
            "trigger_filters": ["port EQ 445"],
            "trigger_mappings": ["ipv4 -> target_host"],
        },
        {
            "step_template_id": "st-smb2",
            "title": "SMB share dump",
            "status": "PENDING",
            "event_id": "evt-smb",
            "event_name": "SMB service exposed",
            "trigger_mappings": ["ipv4 -> target_host"],
        },
    ]
    monkeypatch.setattr(auto_tools, "_api", _fake_state(steps))
    executor, _ = _build()
    out = await _run(executor, "read_openaev_attack_path_state", run_id="r1")
    # The shared event is listed once, with its 2-step count.
    assert "EVENTS in this path" in out
    assert "evt-smb (SMB service exposed) <- 2 step(s)" in out
    # Reuse guidance: attach by trigger.event_id with only mappings, and it is OPTIONAL.
    assert "trigger.event_id" in out
    assert "Reusing an event is OPTIONAL" in out
    assert "seeds and standalone actions have NO event" in out
    # The event_id is copyable from each finding-driven step's wiring.
    assert "event_id: evt-smb" in out


@pytest.mark.asyncio
async def test_state_render_no_events_overview_when_path_is_event_less(monkeypatch):
    # An all-seed path carries no event, so NO EVENTS overview is rendered - nothing
    # nudges the model to invent one (event-less steps are correct and expected).
    steps = [
        {"step_template_id": "st1", "title": "Nmap sweep", "status": "SUCCESS"},
        {"step_template_id": "st2", "title": "AD enum", "status": "SUCCESS"},
    ]
    monkeypatch.setattr(auto_tools, "_api", _fake_state(steps))
    executor, _ = _build()
    out = await _run(executor, "read_openaev_attack_path_state", run_id="r1")
    assert "EVENTS in this path" not in out


def test_trigger_schema_exposes_optional_event_id_on_author_and_update():
    # Both authoring and updating share ONE trigger schema, so event_id can never
    # drift between them; its description must mark it OPTIONAL.
    executor = CallableToolExecutor()
    defs = auto_tools.register("https://oaev.example", {}, executor)
    for tool in ("author_openaev_attack_path_step", "update_openaev_attack_path_step"):
        schema = next(d.input_schema for d in defs if d.name == tool)
        trigger_props = schema["properties"]["trigger"]["properties"]
        assert "event_id" in trigger_props
        assert "OPTIONAL" in trigger_props["event_id"]["description"]
    # event_id is never a required trigger field (reuse is a choice, not an obligation).
    author_schema = next(
        d.input_schema for d in defs if d.name == "author_openaev_attack_path_step"
    )
    assert "event_id" not in (
        author_schema["properties"]["trigger"].get("required") or []
    )


def test_author_and_update_descriptions_teach_optional_event_reuse():
    executor = CallableToolExecutor()
    defs = auto_tools.register("https://oaev.example", {}, executor)
    author_desc = next(
        d.description for d in defs if d.name == "author_openaev_attack_path_step"
    )
    # Author: reuse by trigger.event_id, and it is never required; standalone = no event.
    assert "trigger.event_id" in author_desc
    assert "event_id is never required" in author_desc
    assert "standalone action legitimately has NO event" in author_desc
    update_desc = next(
        d.description for d in defs if d.name == "update_openaev_attack_path_step"
    )
    # Update: re-point onto an existing event, still OPTIONAL.
    assert "To re-point this step onto an event that ALREADY" in update_desc
    assert "trigger.event_id" in update_desc
    assert "event_id stays OPTIONAL" in update_desc


def test_orchestrator_persona_teaches_optional_event_reuse():
    # The persona must teach: reuse ONE event by id (not by re-typing), that
    # event_id is OPTIONAL, and that event-less seeds/standalone actions are valid -
    # so the orchestrator neither duplicates a shared event nor forces an event onto
    # every action.
    from app.agents.seed import _resolve_seed_content

    persona = _resolve_seed_content("agent_persona", "openaev-attack-path-orchestrator")
    assert persona
    assert "ATTACH to the existing event" in persona
    assert 'trigger={"event_id"' in persona
    assert "`event_id` is OPTIONAL" in persona
    assert "event-less steps are correct and expected" in persona
    assert "EVENTS overview" in persona


def test_cycle_prompt_teaches_optional_event_reuse():
    # The per-cycle instruction (rebuilt every cycle) must carry the same event
    # discipline as the persona: share one event by id, event_id is OPTIONAL, and
    # seeds/standalone actions have NO event.
    from app.services.autonomous_pentest import _build_cycle_prompt

    prompt = _build_cycle_prompt(
        {"objective": "own the domain", "openaev_run_id": "r1"}, 1
    )
    assert "To SHARE one event across several reactors" in prompt
    assert 'trigger={"event_id"' in prompt
    assert "event_id is OPTIONAL" in prompt
    assert "standalone/objective actions" in prompt

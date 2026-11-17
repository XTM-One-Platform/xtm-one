"""Tests for the agent export/import HTTP surface and the shared pipeline.

Two layers:

- **Endpoints** — the format query parameter, the portability preview, the
  auto-detecting file upload, and the audit trail (which must record rejected
  uploads too, since the import telemetry counts them).
- **Shared pipeline** — that both import entry points seed the initial version
  history and apply the default built-in tools, which the old
  import path did not.
"""

from __future__ import annotations

import io
import json
import uuid
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import agent_import as agent_import_mod
from app.api.v1 import agents as agents_api
from app.api.v1.schemas.agent import AgentResponse
from app.core.database import get_db
from app.services import persistent_memory
from app.services.agentschema import schema_problems

# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------


class _FakeDb:
    """Records what the endpoint did without touching a database."""

    def __init__(self):
        self.added: list[object] = []
        self.commits = 0
        self.flushes = 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1

    async def execute(self, *_args, **_kwargs):  # pragma: no cover - unused here
        raise AssertionError("Unexpected execute() call")


def _user(*, role: str = "user"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=f"{role}@example.com",
        full_name="Test User",
        role=role,
        is_admin=role == "admin",
    )


def _agent_row(user_id: uuid.UUID):
    now = "2026-07-30T10:00:00+00:00"
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Threat Analyst",
        slug="threat-analyst",
        description="Analyses CTI reports",
        icon="Shield",
        persona="You are a threat analyst.",
        guardrails="Never disclose internal hostnames.",
        output_format="markdown",
        output_schema=None,
        enabled=True,
        is_company_managed=False,
        tags=["cti"],
        llm_provider="anthropic",
        llm_model="claude-opus-5",
        temperature=0.3,
        reasoning_effort="high",
        max_iterations=None,
        config={},
        memory_short_term_enabled=True,
        memory_medium_term_enabled=False,
        memory_persistent_enabled=True,
        memory_user_enabled=True,
        disable_chat=False,
        handover_types={},
        handover_conditions={},
        work_item_owner_agent_ids=[],
        trace_supervision_target_agent_ids=[],
        builtin_tool_ids=["code_interpreter"],
        tool_deny_list=[],
        custom_tool_ids=[],
        mcp_server_ids=[],
        skill_ids=[],
        prompt_ids=[],
        knowledge_base_ids=[],
        integration_bindings={},
        created_at=now,
        updated_at=now,
    )


def _export_bundle(agent) -> dict:
    """What ``build_agent_export_bundle`` would produce for ``agent``."""
    return {
        "version": 3,
        "export_type": "agent",
        "exported_at": "2026-07-30T10:00:00+00:00",
        "agent": {
            "name": agent.name,
            "description": agent.description,
            "icon": agent.icon,
            "persona": agent.persona,
            "output_format": "markdown",
            "output_schema": None,
            "tags": ["cti"],
            "config": {},
            "llm_provider": "anthropic",
            "llm_model": "claude-opus-5",
            "temperature": 0.3,
            "reasoning_effort": "high",
            "max_iterations": None,
            "guardrails": agent.guardrails,
            "memory_short_term_enabled": True,
            "memory_medium_term_enabled": False,
            "memory_persistent_enabled": True,
            "memory_user_enabled": True,
            "disable_chat": False,
            "handover_types": {},
            "handover_conditions": {},
            "work_item_owner_agent_ids": [],
            "trace_supervision_target_agent_ids": [],
            "builtin_tool_ids": ["code_interpreter"],
            "tool_deny_list": [],
        },
        "assignments": [],
        "custom_tools": [],
        "mcp_servers": [],
        "integration_providers": [],
        "skills": [],
        "prompts": [],
        "knowledge_base_notes": [],
        "variables": [],
    }


def _client(monkeypatch, *, user, agent=None, memories=()):
    """A TestClient over the agents router with the DB layer stubbed out."""
    fake_db = _FakeDb()
    audit: list[dict] = []

    async def _get_user_agent(_db, _uid, _agent_id, _group_ids=None, **_kwargs):
        return agent

    async def _get_user_group_ids(_db, _uid):
        return []

    async def _build_bundle(_db, resolved_agent):
        return _export_bundle(resolved_agent)

    async def _log_audit(_db, **kwargs):
        audit.append(kwargs)

    def _to_response(row, assignment_count=0, **_kwargs):
        # The real serializer reads ~40 ORM columns; the tests here are about
        # detection/validation, not agent serialization.
        return AgentResponse(
            id=str(row.id),
            user_id=str(row.user_id),
            name=row.name,
            icon=row.icon,
            enabled=True,
            assignment_count=assignment_count,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    monkeypatch.setattr(agents_api, "get_user_agent", _get_user_agent)
    monkeypatch.setattr(agents_api, "get_user_group_ids", _get_user_group_ids)
    monkeypatch.setattr(agents_api, "build_agent_export_bundle", _build_bundle)
    monkeypatch.setattr(agents_api, "_to_response", _to_response)
    monkeypatch.setattr(agents_api, "log_audit", _log_audit)

    async def _export_memories(_db, *, agent_id):
        return list(memories)

    # The native export carries the agent's memory; none by default.
    monkeypatch.setattr(persistent_memory, "export_memories", _export_memories)

    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api/v1/agents")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None

    async def _get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app), fake_db, audit


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_defaults_to_the_native_json_bundle(monkeypatch):
    user = _user()
    agent = _agent_row(user.id)
    client, _db, audit = _client(monkeypatch, user=user, agent=agent)

    response = client.get(f"/api/v1/agents/{agent.id}/export")

    assert response.status_code == 200
    assert response.json()["export_type"] == "agent"
    assert "agent-threat-analyst.json" in response.headers["content-disposition"]
    assert audit[0]["action"] == "export"
    assert audit[0]["details"]["format"] == "native"
    # No memory: no ``memories`` key.
    assert "memories" not in response.json()
    assert "memory_count" not in audit[0]["details"]


def test_native_export_carries_the_owners_agent_memory(monkeypatch):
    user = _user()
    agent = _agent_row(user.id)
    entry = {"content": "c", "memory_type": "fact", "subject_key": None,
             "source": "chat", "user_id": None, "created_at": "2026-01-01T00:00:00+00:00"}
    client, _db, audit = _client(monkeypatch, user=user, agent=agent, memories=[entry])

    response = client.get(f"/api/v1/agents/{agent.id}/export")

    assert response.status_code == 200
    assert response.json()["memories"] == [entry]
    assert audit[0]["details"]["memory_count"] == 1


def test_export_as_agentschema_returns_valid_yaml(monkeypatch):
    user = _user()
    agent = _agent_row(user.id)
    client, _db, audit = _client(monkeypatch, user=user, agent=agent)

    response = client.get(f"/api/v1/agents/{agent.id}/export?format=agentschema")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/yaml")
    assert "agent-threat-analyst.agent.yaml" in response.headers["content-disposition"]

    document = yaml.safe_load(response.text)
    assert document["kind"] == "prompt"
    assert schema_problems(document) == []

    # The audit entry records the format and which fields were left out, which
    # is what the adoption telemetry reads.
    assert audit[0]["details"]["format"] == "agentschema"
    assert "guardrails" in audit[0]["details"]["omitted_fields"]


def test_export_rejects_an_unknown_format(monkeypatch):
    user = _user()
    agent = _agent_row(user.id)
    client, _db, _audit = _client(monkeypatch, user=user, agent=agent)

    response = client.get(f"/api/v1/agents/{agent.id}/export?format=protobuf")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Portability preview
# ---------------------------------------------------------------------------


def test_portability_preview_lists_the_non_portable_fields(monkeypatch):
    user = _user()
    agent = _agent_row(user.id)
    client, _db, _audit = _client(monkeypatch, user=user, agent=agent)

    response = client.get(
        f"/api/v1/agents/{agent.id}/export/portability?format=agentschema"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "agentschema"
    assert body["preserved_in_metadata"] is True
    keys = {entry["key"] for entry in body["entries"]}
    assert "guardrails" in keys
    assert "memory_tiers" in keys
    assert all(entry["detail"] for entry in body["entries"])


def test_portability_preview_reports_nothing_for_the_native_format(monkeypatch):
    user = _user()
    agent = _agent_row(user.id)
    client, _db, _audit = _client(monkeypatch, user=user, agent=agent)

    response = client.get(f"/api/v1/agents/{agent.id}/export/portability?format=native")

    assert response.status_code == 200
    assert response.json() == {
        "format": "native",
        "format_label": "XTM One native bundle",
        # No external specification governs the native bundle.
        "spec_version": None,
        "entries": [],
        "preserved_in_metadata": False,
    }


def test_portability_preview_does_not_write_an_audit_entry(monkeypatch):
    """It is a read-only preview — only the actual export is an auditable act."""
    user = _user()
    agent = _agent_row(user.id)
    client, _db, audit = _client(monkeypatch, user=user, agent=agent)

    client.get(f"/api/v1/agents/{agent.id}/export/portability?format=agentschema")
    assert audit == []


# ---------------------------------------------------------------------------
# Import with auto-detection
# ---------------------------------------------------------------------------


def _upload(client, content: bytes, filename: str):
    return client.post(
        "/api/v1/agents/import-file",
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
    )


def _stub_import(monkeypatch, *, warnings=None):
    """Replace the creation pipeline; return the calls it received."""
    calls: list[dict] = []

    async def _create(_db, *, user, bundle, request=None, source_format="native", audit_details=None):
        calls.append(
            {
                "bundle": bundle,
                "source_format": source_format,
                "audit_details": audit_details or {},
            }
        )
        return agent_import_mod.ImportedAgent(
            agent=_agent_row(user.id),
            assignment_count=0,
            warnings=list(warnings or []),
        )

    monkeypatch.setattr(agents_api, "create_agent_from_bundle", _create)
    return calls


def test_import_detects_a_native_bundle_without_being_told(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)
    calls = _stub_import(monkeypatch)

    agent = _agent_row(user.id)
    response = _upload(client, json.dumps(_export_bundle(agent)).encode(), "agent.json")

    assert response.status_code == 201
    body = response.json()
    assert body["detected_format"] == "native"
    assert body["detected_format_label"] == "XTM One native bundle"
    assert calls[0]["source_format"] == "native"


def test_import_detects_an_agentschema_yaml_document(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)
    calls = _stub_import(monkeypatch)

    document = (
        "kind: prompt\n"
        "name: External Assistant\n"
        "model:\n  id: gpt-4o\n"
        "instructions: Be helpful.\n"
    )
    response = _upload(client, document.encode(), "external.agent.yaml")

    assert response.status_code == 201
    body = response.json()
    assert body["detected_format"] == "agentschema"
    assert body["detected_format_label"] == "Microsoft AgentSchema"
    # Mapped onto the native bundle shape before it reaches the pipeline.
    assert calls[0]["bundle"]["export_type"] == "agent"
    assert calls[0]["bundle"]["agent"]["persona"] == "Be helpful."
    assert calls[0]["audit_details"]["validation_outcome"] == "passed"


def test_import_surfaces_leniencies_and_unresolvable_references(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)
    _stub_import(monkeypatch)

    # No ``kind``, shorthand ``model``, and a tool this instance cannot honour.
    document = (
        "name: Analyst\n"
        "model: gpt-4o\n"
        "instructions: Analyse.\n"
        "tools:\n"
        "  - name: get_weather\n    kind: function\n    parameters:\n"
        "      properties:\n        city:\n          kind: string\n"
    )
    response = _upload(client, document.encode(), "analyst.yaml")

    assert response.status_code == 201
    body = response.json()
    assert any("no 'kind'" in note for note in body["notes"])
    assert any("get_weather" in warning for warning in body["warnings"])


@pytest.mark.parametrize("kind", ["hosted", "workflow"])
def test_import_rejects_the_unsupported_agent_kinds(monkeypatch, kind):
    user = _user()
    client, _db, audit = _client(monkeypatch, user=user)
    _stub_import(monkeypatch)

    response = _upload(
        client, f"kind: {kind}\nname: n\nmodel: m\n".encode(), "other.yaml"
    )

    assert response.status_code == 400
    assert "only import 'kind: prompt'" in response.json()["detail"]
    # A rejection is still an auditable import attempt (the telemetry counts it).
    assert audit[0]["action"] == "import_rejected"
    assert audit[0]["details"]["validation_outcome"] == "failed"


def test_import_rejects_an_unrecognized_file_with_both_formats_explained(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)
    _stub_import(monkeypatch)

    response = _upload(client, b"hello: world\n", "random.yaml")

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "neither an XTM One agent bundle nor a Microsoft AgentSchema" in detail


def test_import_reports_schema_problems_per_field(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)
    _stub_import(monkeypatch)

    response = _upload(client, b"kind: prompt\nname: n\ninstructions: 12\n", "bad.yaml")

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "'model' is a required property" in detail
    assert "instructions:" in detail


def test_import_rejects_an_oversized_upload(monkeypatch):
    from app.services.agentschema import max_upload_bytes

    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)
    _stub_import(monkeypatch)

    response = _upload(client, b"x" * (max_upload_bytes() + 1), "huge.json")

    assert response.status_code == 400
    assert "maximum size" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Dry-run check + confirm (the import page's two-step flow)
# ---------------------------------------------------------------------------


def _check(client, content: bytes, filename: str):
    return client.post(
        "/api/v1/agents/import-file/check",
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
    )


def test_check_passes_a_native_bundle_and_returns_it(monkeypatch):
    user = _user()
    client, db, audit = _client(monkeypatch, user=user)

    bundle = _export_bundle(_agent_row(user.id))
    response = _check(client, json.dumps(bundle).encode(), "agent.json")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["detected_format"] == "native"
    assert body["agent_name"] == "Threat Analyst"
    assert [stage["status"] for stage in body["stages"]] == ["passed"] * 4
    # The bundle is echoed back so the confirm call imports what was checked.
    assert body["bundle"] == bundle
    # Dry-run: nothing persisted, nothing audited.
    assert db.commits == 0
    assert audit == []


def test_check_reports_the_failing_stage_without_erroring(monkeypatch):
    user = _user()
    client, _db, audit = _client(monkeypatch, user=user)

    bad = {"version": 99, "export_type": "agent", "agent": {"name": "Bad"}}
    response = _check(client, json.dumps(bad).encode(), "bad.json")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    by_key = {stage["key"]: stage for stage in body["stages"]}
    assert by_key["parse"]["status"] == "passed"
    assert by_key["detect"]["status"] == "passed"
    assert by_key["schema"]["status"] == "failed"
    assert any(
        "unsupported export version 99" in problem
        for problem in by_key["schema"]["problems"]
    )
    assert by_key["references"]["status"] == "skipped"
    assert body["bundle"] is None
    assert audit == []


def test_check_surfaces_agentschema_warnings_before_any_import(monkeypatch):
    user = _user()
    client, db, _audit = _client(monkeypatch, user=user)

    document = (
        "kind: prompt\n"
        "name: Analyst\n"
        "model:\n  id: gpt-4o\n"
        "instructions: Analyse.\n"
        "tools:\n"
        "  - name: get_weather\n    kind: function\n    parameters:\n"
        "      properties:\n        city:\n          kind: string\n"
    )
    response = _check(client, document.encode(), "analyst.yaml")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["detected_format"] == "agentschema"
    assert any("get_weather" in warning for warning in body["warnings"])
    # Mapped onto the native bundle shape, ready for the confirm call.
    assert body["bundle"]["export_type"] == "agent"
    assert body["bundle"]["agent"]["persona"] == "Analyse."
    assert db.commits == 0


def test_check_fails_the_detect_stage_for_an_unrecognized_file(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)

    response = _check(client, b"hello: world\n", "random.yaml")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    by_key = {stage["key"]: stage for stage in body["stages"]}
    assert by_key["parse"]["status"] == "passed"
    assert by_key["detect"]["status"] == "failed"
    assert by_key["schema"]["status"] == "skipped"
    assert body["detected_format"] is None


_AGENTSCHEMA_WITH_PLACEHOLDERS = (
    "kind: prompt\n"
    "name: Analyst\n"
    "model:\n  id: gpt-4o\n"
    "instructions: Analyse.\n"
    "tools:\n"
    "  - name: get_weather\n    kind: function\n    parameters:\n"
    "      properties:\n        city:\n          kind: string\n"
    "  - name: tickets\n    kind: mcp\n    serverName: jira\n"
)


def test_check_offers_corrections_for_placeholder_entries(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)

    response = _check(
        client, _AGENTSCHEMA_WITH_PLACEHOLDERS.encode(), "analyst.yaml"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    by_subject = {c["subject"]: c for c in body["corrections"]}
    assert by_subject["get_weather"]["kind"] == "drop_custom_tool"
    assert by_subject["get_weather"]["id"] == "drop_custom_tool:0"
    assert by_subject["jira"]["kind"] == "drop_mcp_server"
    assert by_subject["jira"]["id"] == "drop_mcp_server:0"


def test_corrections_skip_nameless_placeholders():
    from app.services.agentschema import derive_corrections

    bundle = {
        "custom_tools": [
            {"name": "  ", "js_function": "", "tags": ["agentschema-import"]},
            {"name": "real", "js_function": "", "tags": ["agentschema-import"]},
        ],
        "mcp_servers": [{"name": "", "tags": ["agentschema-import"]}],
    }
    corrections = derive_corrections(bundle)
    # The nameless stub and server yield no correction (no readable copy);
    # they stay covered by the warnings instead. The id addresses the entry
    # by its index in the collection, so it survives the skipped sibling.
    assert [(c.id, c.subject) for c in corrections] == [("drop_custom_tool:1", "real")]


def test_corrections_tell_apart_placeholders_sharing_a_name():
    from app.services.agentschema import apply_corrections, derive_corrections

    bundle = {
        "custom_tools": [
            {"name": "dup", "js_function": "", "tags": ["agentschema-import"]},
            {"name": "dup", "js_function": "", "tags": ["agentschema-import"]},
        ],
    }
    corrections = derive_corrections(bundle)
    # Two same-named stubs are two distinct corrections, not one that would
    # silently drop both.
    assert [c.id for c in corrections] == ["drop_custom_tool:0", "drop_custom_tool:1"]

    corrected = apply_corrections(bundle, ["drop_custom_tool:0"])
    assert len(corrected["custom_tools"]) == 1
    # The original bundle is never mutated.
    assert len(bundle["custom_tools"]) == 2


def test_confirm_applies_selected_corrections_server_side(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)
    calls = _stub_import(monkeypatch)

    checked = _check(
        client, _AGENTSCHEMA_WITH_PLACEHOLDERS.encode(), "analyst.yaml"
    ).json()
    response = client.post(
        "/api/v1/agents/import/confirm",
        json={
            "bundle": checked["bundle"],
            "detected_format": "agentschema",
            "corrections": ["drop_custom_tool:0"],
        },
    )

    assert response.status_code == 201
    imported_bundle = calls[0]["bundle"]
    # The stub tool is gone; the (unselected) MCP placeholder is kept.
    assert imported_bundle["custom_tools"] == []
    assert [s["name"] for s in imported_bundle["mcp_servers"]] == ["jira"]
    assert calls[0]["audit_details"]["corrections_applied"] == ["drop_custom_tool:0"]


def test_confirm_rejects_a_correction_the_check_would_not_offer(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)
    _stub_import(monkeypatch)

    # An implemented tool is not a placeholder — dropping it via the
    # correction mechanism must not be possible.
    bundle = _export_bundle(_agent_row(user.id))
    bundle["custom_tools"] = [
        {"name": "real_tool", "js_function": "return 1;", "tags": []}
    ]
    response = client.post(
        "/api/v1/agents/import/confirm",
        json={"bundle": bundle, "corrections": ["drop_custom_tool:real_tool"]},
    )

    assert response.status_code == 400
    assert "Unknown import correction" in response.json()["detail"]


def _stub_advisor(monkeypatch, *, skill=True):
    """Stub the platform-agent + seeded-skill resolution; return run calls."""
    advisor_agent = SimpleNamespace(
        id=uuid.uuid4(), slug="xtm-one-assistant", skill_ids=["existing-skill-id"]
    )
    advisor_skill = (
        SimpleNamespace(id=uuid.uuid4(), name="Agent Import Fix Suggestions")
        if skill
        else None
    )

    async def _resolve(_db):
        return advisor_agent, advisor_skill

    monkeypatch.setattr(agents_api, "_resolve_import_fix_advisor", _resolve)

    calls: list[dict] = []

    async def _run(db, *, user_id, agent, history, user_message, **kwargs):
        calls.append(
            {"agent": agent, "user_message": user_message, "kwargs": kwargs}
        )
        return "- Implement get_weather.\n", None

    monkeypatch.setattr(agents_api, "run_agent_chat", _run)
    return advisor_agent, advisor_skill, calls


def test_suggest_import_fixes_runs_the_platform_agent_with_the_skill(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)
    advisor_agent, advisor_skill, calls = _stub_advisor(monkeypatch)

    response = client.post(
        "/api/v1/agents/import-file/suggest",
        json={
            "agent_name": "Analyst",
            "persona": "Use get_weather for forecasts.",
            "warnings": ["Function tool 'get_weather' was imported without an implementation"],
        },
    )

    assert response.status_code == 200
    assert response.json()["suggestions"] == "- Implement get_weather."
    call = calls[0]
    assert call["agent"] is advisor_agent
    # The seeded skill is injected for the session, on top of the agent's own.
    assert call["kwargs"]["session_skill_ids"] == [
        "existing-skill-id",
        str(advisor_skill.id),
    ]
    # Advisory run: no handover, no tool surface beyond the skill tools.
    assert call["kwargs"]["allow_handover"] is False
    assert call["kwargs"]["session_builtin_tool_ids"] == []
    assert call["kwargs"]["session_custom_tool_ids"] == []
    assert call["kwargs"]["session_mcp_server_ids"] == []
    assert call["kwargs"]["session_integration_ids"] == []
    assert call["kwargs"]["session_knowledge_base_ids"] == []
    # The message tells the agent to load the skill, and carries the data.
    assert "Agent Import Fix Suggestions" in call["user_message"]
    assert "get_weather" in call["user_message"]
    assert "Use get_weather for forecasts." in call["user_message"]


def test_suggest_import_fixes_degrades_without_the_seeded_skill(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)
    _agent, _skill, calls = _stub_advisor(monkeypatch, skill=False)

    response = client.post(
        "/api/v1/agents/import-file/suggest",
        json={"warnings": ["Function tool 'x' has no implementation"]},
    )

    assert response.status_code == 200
    call = calls[0]
    # No skill to inject: the agent keeps its own, the brief is inlined.
    assert call["kwargs"]["session_skill_ids"] is None
    assert "load_skill" not in call["user_message"]
    assert "markdown bullet list" in call["user_message"]


def test_suggest_import_fixes_reports_a_missing_llm_provider_cleanly(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)
    advisor_agent = SimpleNamespace(id=uuid.uuid4(), skill_ids=[])

    async def _resolve(_db):
        return advisor_agent, None

    monkeypatch.setattr(agents_api, "_resolve_import_fix_advisor", _resolve)

    async def _run(db, **_kwargs):
        raise ValueError("No LLM provider configured.")

    monkeypatch.setattr(agents_api, "run_agent_chat", _run)

    response = client.post(
        "/api/v1/agents/import-file/suggest",
        json={"warnings": ["Function tool 'x' has no implementation"]},
    )

    assert response.status_code == 400
    assert "No LLM provider configured" in response.json()["detail"]


def test_suggest_import_fixes_requires_warnings(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)

    response = client.post(
        "/api/v1/agents/import-file/suggest", json={"warnings": []}
    )

    assert response.status_code == 400


def test_confirm_imports_the_checked_bundle_with_its_provenance(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)
    calls = _stub_import(monkeypatch, warnings=["placeholder tool to finish"])

    bundle = _export_bundle(_agent_row(user.id))
    response = client.post(
        "/api/v1/agents/import/confirm",
        json={"bundle": bundle, "detected_format": "agentschema"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["detected_format"] == "agentschema"
    assert body["detected_format_label"] == "Microsoft AgentSchema"
    assert body["warnings"] == ["placeholder tool to finish"]
    # Provenance reaches the shared pipeline for the audit trail.
    assert calls[0]["source_format"] == "agentschema"
    # The outcome is derived (validate_native_bundle just ran), not asserted.
    assert calls[0]["audit_details"]["validation_outcome"] == "passed"


def test_confirm_rejects_a_malformed_bundle_envelope(monkeypatch):
    user = _user()
    client, _db, _audit = _client(monkeypatch, user=user)
    calls = _stub_import(monkeypatch)

    response = client.post(
        "/api/v1/agents/import/confirm",
        json={"bundle": {"version": "not-an-int", "agent": []}},
    )

    assert response.status_code == 400
    assert "version" in response.json()["detail"]
    # Rejected before the creation pipeline ever ran.
    assert calls == []


# ---------------------------------------------------------------------------
# Shared pipeline
# ---------------------------------------------------------------------------


def test_default_builtin_tools_are_applied_when_the_bundle_is_silent():
    """A bundle that names no built-in tools gets the platform default."""
    assert agent_import_mod._resolve_builtin_tool_ids({"name": "n"}) == [
        "inspect_my_traces"
    ]


def test_an_explicit_builtin_tool_list_is_honoured_including_empty():
    """"No built-in tools" must stay a choice the bundle can express."""
    assert agent_import_mod._resolve_builtin_tool_ids(
        {"name": "n", "builtin_tool_ids": []}
    ) == []
    assert agent_import_mod._resolve_builtin_tool_ids(
        {"name": "n", "builtin_tool_ids": ["code_interpreter"]}
    ) == ["code_interpreter"]


def test_create_agent_shares_the_default_builtin_tool_set():
    """The created and imported defaults must come from one constant.

    They drifted apart once already — an imported agent silently lost
    ``inspect_my_traces`` because the import path hard-coded an empty list.
    """
    import inspect

    source = inspect.getsource(agents_api.create_agent)
    assert "DEFAULT_BUILTIN_TOOL_IDS" in source
    assert '"inspect_my_traces"' not in source


@pytest.mark.asyncio
async def test_import_seeds_the_initial_version_history():
    """An imported agent must get the same baseline a created one does."""
    seeded_personas: list[str] = []
    seeded_config: list[tuple[str, object]] = []

    class _Recorder:
        async def create_persona(self, _db, *, agent_id, content, author_id, commit_message):
            seeded_personas.append(commit_message)
            return SimpleNamespace(version_number=1)

        async def create_config(self, _db, *, agent_id, field_name, value_json, author_id, commit_message):
            seeded_config.append((field_name, value_json))
            return SimpleNamespace(version_number=1)

    import app.services.agent_config_versioning as versioning

    recorder = _Recorder()
    original_persona = versioning.create_agent_persona_version
    original_config = versioning.create_agent_config_version
    versioning.create_agent_persona_version = recorder.create_persona
    versioning.create_agent_config_version = recorder.create_config
    try:
        agent = SimpleNamespace(
            id=uuid.uuid4(),
            persona="You are a threat analyst.",
            guardrails="Never disclose internal hostnames.",
            temperature=0.3,
            reasoning_effort="high",
            max_iterations=None,
        )
        await agent_import_mod._seed_initial_versions(
            _FakeDb(), agent=agent, author_id=uuid.uuid4()
        )
    finally:
        versioning.create_agent_persona_version = original_persona
        versioning.create_agent_config_version = original_config

    assert seeded_personas == ["Initial version"]
    seeded_fields = dict(seeded_config)
    assert seeded_fields["guardrails"] == "Never disclose internal hostnames."
    assert seeded_fields["temperature"] == 0.3
    assert seeded_fields["reasoning_effort"] == "high"
    # ``None`` means "inherit" and must not be snapshotted as a real value.
    assert "max_iterations" not in seeded_fields

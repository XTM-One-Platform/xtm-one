"""Tests for the full-CRUD Agent Builder tools.

These cover the tools added to ``build_agent_builder_tool`` for managing
prompts, skills, variables, MCP servers, custom tools, knowledge-base
documents, and integrations — focusing on the contracts that matter for the
agent-builder surface:

* every tool is both registered on the executor AND has a ToolDefinition,
* anonymous callers and disabled feature flags are refused gracefully,
* invalid UUID arguments are rejected before a DB session is opened,
* the shared three-tier permission gate is honoured on write tools,
* secret material is never echoed back, and
* happy-path create/delete round-trips commit through a dedicated session.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

import app.api.v1.helpers as api_helpers
import app.core.config as config_module
import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
from app.models.agent import Agent
from app.models.knowledge_base import KnowledgeBase
from app.services.builtin_tool_aliases import alias_scoped_functions
from app.tools.base import ToolCall

build_agent_builder_tool = builtin_tools_module.build_agent_builder_tool


@pytest.fixture(autouse=True)
def update_audit(monkeypatch):
    """Records the ``update_agent`` audit rows: these fakes have no audit
    table and their agents no slug, so the rows are read back in
    ``tests/test_agent_builder_versions_postgres.py`` and
    ``tests/test_agent_builder_update_audit.py`` (#251)."""
    import app.services.audit as audit_module

    entries: list[dict] = []

    async def _record(_db, **kwargs):
        entries.append(kwargs)

    monkeypatch.setattr(audit_module, "log_audit", _record)
    return entries


@pytest.fixture(autouse=True)
def version_history(monkeypatch):
    """Records how the builder calls the version history: these fakes have no
    version tables, so the rows are read back in
    ``tests/test_agent_builder_versions_postgres.py`` and the calls asserted
    here (``.records`` / ``.seeds``, the keyword arguments of each call)."""
    import app.services.agent_config_versioning as versioning

    spy = SimpleNamespace(records=[], seeds=[])

    async def _record(_db, **kwargs):
        spy.records.append(kwargs)
        return None, {}

    async def _seed(_db, **kwargs):
        spy.seeds.append(kwargs)

    monkeypatch.setattr(versioning, "record_agent_version_changes", _record)
    monkeypatch.setattr(versioning, "seed_initial_agent_versions", _seed)
    monkeypatch.setattr(versioning, "versioned_agent_values", lambda _agent: {})
    return spy


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value

    def all(self):
        # For row-tuple result sets (e.g. ``select(Model.id, Model.name)``)
        # consumed directly via ``result.all()`` (not ``.scalars().all()``).
        if self._value is None:
            return []
        return list(self._value)

    def scalars(self):
        return _ScalarsIterator(self._value)


class _ScalarsIterator:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value

    def all(self):
        if self._value is None:
            return []
        if isinstance(self._value, (list, tuple)):
            return list(self._value)
        return [self._value]


class _FakeSession:
    """Minimal async-session double for the builder CRUD tools.

    ``execute`` pops pre-baked results from ``execute_results`` (default
    ``None`` once exhausted). add/delete/commit/rollback/flush are recorded so
    tests can assert the write actually round-tripped.
    """

    def __init__(self, created, *, execute_results=None, fail_on_commit=False):
        self._created = created
        self._created.append(self)
        self._execute_results = list(execute_results or [])
        self.fail_on_commit = fail_on_commit
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        self.closed = True
        return False

    async def execute(self, _stmt):
        value = self._execute_results.pop(0) if self._execute_results else None
        return _ScalarResult(value)

    def add(self, instance):
        self.added.append(instance)

    async def delete(self, instance):
        self.deleted.append(instance)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        if self.fail_on_commit:
            raise RuntimeError("simulated commit failure")
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _session_factory(created, **kwargs):
    def _make():
        return _FakeSession(created, **kwargs)

    return _make


def _admin_caller(monkeypatch, *, is_admin=True):
    fake = SimpleNamespace(
        id=uuid.uuid4(),
        email="caller@example.com",
        role="admin" if is_admin else "user",
        is_admin=is_admin,
    )

    async def _fake(_db, _uid):
        return fake

    monkeypatch.setattr(builtin_tools_module, "_load_caller_user_for_builder", _fake)
    return fake


def _stub_manageable_gate(monkeypatch, *, entity=None, error=None, package_origin=False):
    async def _fake(_db, _model, _uid, _entity_uuid, *, action="modify", entity_label="entity", lock=False):
        return entity, error

    monkeypatch.setattr(
        builtin_tools_module, "_load_manageable_entity_for_builder", _fake
    )
    _stub_package_origin(monkeypatch, package_origin)


def _stub_package_origin(monkeypatch, package_origin: bool):
    """Answer the package-origin lookup without a query (the fake session
    pops scripted results in order, so a real lookup would eat one).

    Patched where the shared gate (``helpers.package_origin_*_error``) reads
    it, so the builder tools still run the real gate and its refusal."""

    async def _origin(_db, _entity_id):
        return package_origin

    for name in ("is_package_origin_custom_tool", "is_package_origin_knowledge_base"):
        monkeypatch.setattr(api_helpers, name, _origin)


def _stub_writable_agent(monkeypatch, *, agent=None, error=None):
    async def _fake(_db, _uid, _agent_uuid, *, action="modify"):
        return agent, error

    monkeypatch.setattr(
        builtin_tools_module, "_load_writable_agent_for_builder", _fake
    )


def _stub_personal_bindings(monkeypatch, *, recorder=None, error=None):
    """Stub the REST personal-binding visibility validator update_agent reuses.

    ``validate_personal_agent_bindings`` queries the DB to confirm every bound
    resource is visible to the caller; under the fake session that query
    returns nothing, so the real validator would always raise. Tests that are
    not exercising the visibility gate stub it to a no-op; the dedicated
    visibility tests pass ``error`` to force the rejection path and an optional
    ``recorder`` dict to capture the kwargs the builder forwarded.
    """
    from fastapi import HTTPException

    import app.services.agent_bindings as agent_bindings_module

    async def _fake(_db, _uid, _group_ids, **kwargs):
        if recorder is not None:
            recorder.update(kwargs)
        if error is not None:
            raise HTTPException(status_code=400, detail=error)

    monkeypatch.setattr(
        agent_bindings_module, "validate_personal_agent_bindings", _fake
    )


async def _call(executor, tool_name, /, **arguments):
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=tool_name, arguments=arguments)
    )
    return result


# ---------------------------------------------------------------------------
# Registration completeness
# ---------------------------------------------------------------------------

# Every CRUD tool the agent builder is expected to expose.
_EXPECTED_TOOLS = {
    # prompts
    "list_prompts", "get_prompt", "create_prompt", "update_prompt", "delete_prompt",
    # skills
    "list_skills", "get_skill", "create_skill", "update_skill", "delete_skill",
    # variables
    "list_variables", "get_variable", "create_variable", "update_variable",
    "delete_variable",
    # mcp servers
    "list_mcp_servers", "get_mcp_server", "create_mcp_server", "update_mcp_server",
    "delete_mcp_server",
    # custom tools (create/update/delete added)
    "create_custom_tool", "update_custom_tool", "delete_custom_tool",
    # knowledge bases (get/update/delete + docs added)
    "get_knowledge_base", "update_knowledge_base", "delete_knowledge_base",
    "list_kb_documents", "get_kb_document", "delete_kb_document",
    # integrations
    "list_integration_catalog", "get_integration", "create_integration",
    "update_integration", "delete_integration",
}


def test_new_tools_registered_and_defined():
    definitions, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    defined = {d.name for d in definitions}
    registered = set(executor.tool_names)

    missing_def = _EXPECTED_TOOLS - defined
    missing_reg = _EXPECTED_TOOLS - registered
    assert not missing_def, f"missing ToolDefinition for: {sorted(missing_def)}"
    assert not missing_reg, f"missing executor registration for: {sorted(missing_reg)}"


def test_every_definition_has_a_handler():
    """No ToolDefinition may reference a tool name that is not registered."""
    definitions, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    registered = set(executor.tool_names)
    for d in definitions:
        assert d.name in registered, f"{d.name} defined but not registered"


# ---------------------------------------------------------------------------
# Feature-flag gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_variable_refused_when_disabled(monkeypatch):
    monkeypatch.setattr(config_module.settings, "disable_custom_variables", True)
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "create_variable", name="API_BASE", value="x")
    assert not result.is_error
    assert "disabled on this platform" in result.content


@pytest.mark.asyncio
async def test_create_custom_tool_refused_when_disabled(monkeypatch):
    monkeypatch.setattr(config_module.settings, "disable_custom_tools", True)
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "create_custom_tool", name="echo")
    assert not result.is_error
    assert "disabled on this platform" in result.content


# ---------------------------------------------------------------------------
# Anonymous + validation gates (return before opening a DB session)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_prompt_refused_for_anonymous():
    _, executor = build_agent_builder_tool(SimpleNamespace(), "anonymous")
    result = await _call(executor, "create_prompt", name="P", content="c")
    assert "anonymous users" in result.content


@pytest.mark.asyncio
async def test_get_prompt_rejects_invalid_uuid():
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_prompt", prompt_id="not-a-uuid")
    assert "must be a valid UUID" in result.content


@pytest.mark.asyncio
async def test_create_integration_rejects_unknown_provider():
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "create_integration", provider="definitely-not-real", name="X"
    )
    assert "Unknown integration provider" in result.content


@pytest.mark.asyncio
async def test_create_mcp_server_rejects_invalid_transport():
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "create_mcp_server", name="srv", transport="carrier-pigeon"
    )
    assert "Invalid transport" in result.content


@pytest.mark.asyncio
async def test_create_skill_requires_url_or_content():
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "create_skill", name="My Skill")
    assert "url" in result.content and "content" in result.content


@pytest.mark.asyncio
async def test_create_custom_tool_rejects_unknown_language(monkeypatch):
    monkeypatch.setattr(config_module.settings, "disable_custom_tools", False)
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "create_custom_tool", name="t", language="rust"
    )
    assert "Invalid language" in result.content


# ---------------------------------------------------------------------------
# Permission gate propagation (write tools)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_prompt_surfaces_permission_error(monkeypatch):
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _stub_manageable_gate(
        monkeypatch, error="Only admins can edit company-managed prompts."
    )
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_prompt", prompt_id=str(uuid.uuid4()), name="new"
    )
    assert result.content == "Only admins can edit company-managed prompts."
    # No commit should have happened.
    assert created and created[0].commits == 0


@pytest.mark.asyncio
async def test_update_prompt_identical_content_is_noop(monkeypatch):
    """Passing `content` identical to the existing prompt content must NOT report
    it as updated, must NOT create a new version, and must NOT commit — only a
    real change should. Otherwise the tool reports a misleading "updated:
    content" and writes a spurious version + commit.
    """
    import app.services.prompt_versioning as prompt_versioning

    versions: list = []

    async def _track_version(*_a, **_k):
        versions.append(1)
        return SimpleNamespace(version_number=len(versions))

    monkeypatch.setattr(prompt_versioning, "create_prompt_version", _track_version)

    prompt = SimpleNamespace(
        id=uuid.uuid4(),
        name="House Style",
        content="Be concise.",
        description="x",
        tags=[],
        hide_from_chat=False,
    )
    _stub_manageable_gate(monkeypatch, entity=prompt)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_prompt",
        prompt_id=str(prompt.id),
        content="Be concise.",  # identical to existing content
    )

    assert not result.is_error
    # Nothing actually changed → reported as no-op, no version, no commit.
    assert result.content == "No fields to update. Provide at least one field."
    assert versions == []
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_prompt_changed_content_creates_version(monkeypatch):
    """A genuine content change must create a new version and commit."""
    import app.services.prompt_versioning as prompt_versioning

    versions: list = []

    async def _track_version(*_a, **_k):
        versions.append(1)
        return SimpleNamespace(version_number=len(versions))

    monkeypatch.setattr(prompt_versioning, "create_prompt_version", _track_version)

    prompt = SimpleNamespace(
        id=uuid.uuid4(),
        name="House Style",
        content="Be concise.",
        description="x",
        tags=[],
        hide_from_chat=False,
    )
    _stub_manageable_gate(monkeypatch, entity=prompt)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_prompt",
        prompt_id=str(prompt.id),
        content="Be concise and friendly.",  # genuine change
    )

    assert not result.is_error
    assert "updated: content" in result.content
    assert prompt.content == "Be concise and friendly."
    assert versions == [1]
    assert created[0].commits == 1


def _long_prompt_text() -> str:
    """A prompt of about 3,000 characters with two Markdown sections."""
    intro = "# House style\n" + ("Write plainly and keep each line short. " * 30) + "\n"
    rules = "## Rules\n" + ("Never use an em dash in a sentence. " * 40) + "\n"
    return intro + rules


async def _update_prompt_harness(monkeypatch, content: str):
    import app.services.prompt_versioning as prompt_versioning

    versions: list = []

    async def _track_version(*_a, **kwargs):
        versions.append(kwargs)
        return SimpleNamespace(version_number=len(versions))

    monkeypatch.setattr(prompt_versioning, "create_prompt_version", _track_version)
    prompt = SimpleNamespace(
        id=uuid.uuid4(), name="House Style", content=content, description="x", tags=[], hide_from_chat=False
    )
    _stub_manageable_gate(monkeypatch, entity=prompt)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    return prompt, versions, created, executor


@pytest.mark.asyncio
async def test_update_prompt_edit_mode_changes_one_string_and_answers_the_diff(monkeypatch):
    before = _long_prompt_text()
    prompt, versions, created, executor = await _update_prompt_harness(monkeypatch, before)
    result = await _call(
        executor,
        "update_prompt",
        prompt_id=str(prompt.id),
        mode="edit",
        edits=[{"find": "# House style", "replace": "# House style guide"}],
    )
    assert not result.is_error
    assert "updated: content (edit)" in result.content
    assert "```diff" in result.content and "+# House style guide" in result.content
    assert prompt.content == before.replace("# House style", "# House style guide", 1)
    assert len(versions) == 1 and versions[0]["commit_message"] == "update_prompt (edit)"
    assert created[0].commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("find", ["not in the prompt", "Never use an em dash"])
async def test_update_prompt_edit_absent_or_ambiguous_find_saves_nothing(monkeypatch, find):
    before = _long_prompt_text()
    prompt, versions, created, executor = await _update_prompt_harness(monkeypatch, before)
    result = await _call(
        executor, "update_prompt", prompt_id=str(prompt.id), mode="edit", edits=[{"find": find, "replace": "x"}]
    )
    assert result.content.startswith("Error:") and "Nothing was changed." in result.content
    assert prompt.content == before
    assert versions == [] and created[0].commits == 0


@pytest.mark.asyncio
async def test_update_prompt_append_and_replace_section(monkeypatch):
    before = _long_prompt_text()
    prompt, versions, _created, executor = await _update_prompt_harness(monkeypatch, before)
    appended = await _call(
        executor, "update_prompt", prompt_id=str(prompt.id), mode="append", content="## Tone\nCalm."
    )
    assert "updated: content (append)" in appended.content
    assert prompt.content.startswith(before) and prompt.content.endswith("## Tone\nCalm.")
    replaced = await _call(
        executor,
        "update_prompt",
        prompt_id=str(prompt.id),
        mode="replace_section",
        section="Rules",
        content="## Rules\nOne rule only.",
    )
    assert "updated: content (replace_section)" in replaced.content
    assert "Never use an em dash" not in prompt.content
    assert "Write plainly" in prompt.content and prompt.content.endswith("## Tone\nCalm.")
    assert len(versions) == 2


@pytest.mark.asyncio
async def test_update_prompt_shrinking_replace_needs_confirm_shrink(monkeypatch):
    before = _long_prompt_text()
    short = "Write plainly. " * 34
    prompt, versions, created, executor = await _update_prompt_harness(monkeypatch, before)
    refused = await _call(executor, "update_prompt", prompt_id=str(prompt.id), content=short)
    assert refused.content.startswith("Error:") and "confirm_shrink" in refused.content
    assert "shrink the prompt" in refused.content
    assert prompt.content == before and versions == [] and created[0].commits == 0
    confirmed = await _call(
        executor, "update_prompt", prompt_id=str(prompt.id), content=short, confirm_shrink=True
    )
    assert "updated: content" in confirmed.content
    assert prompt.content == short


@pytest.mark.asyncio
async def test_update_prompt_unknown_mode_is_refused(monkeypatch):
    prompt, versions, _created, executor = await _update_prompt_harness(monkeypatch, "Be concise.")
    result = await _call(executor, "update_prompt", prompt_id=str(prompt.id), mode="patch", content="x")
    assert result.content.startswith("Error: unknown mode 'patch'")
    assert prompt.content == "Be concise." and versions == []


@pytest.mark.asyncio
async def test_delete_mcp_server_surfaces_permission_error(monkeypatch):
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _stub_manageable_gate(
        monkeypatch, error="You don't have permission to delete this MCP server."
    )
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "delete_mcp_server", server_id=str(uuid.uuid4())
    )
    assert "don't have permission" in result.content


# ---------------------------------------------------------------------------
# Secret handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_variable_masks_secret_value(monkeypatch):
    monkeypatch.setattr(config_module.settings, "disable_custom_variables", False)

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    var = SimpleNamespace(
        id=uuid.uuid4(),
        name="OPENAI_KEY",
        value="sk-supersecret-value",
        is_secret=True,
        description="prod key",
        tags=[],
        is_company_managed=False,
    )
    created: list = []
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[var])
    )
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_variable", variable_id=str(var.id))
    assert "sk-supersecret-value" not in result.content
    assert "••••••••" in result.content


# ---------------------------------------------------------------------------
# Happy-path create / delete round-trips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_prompt_happy_path_commits(monkeypatch):
    import app.services.prompt_versioning as prompt_versioning

    async def _no_version(*_a, **_k):
        return SimpleNamespace(version_number=1)

    monkeypatch.setattr(prompt_versioning, "create_prompt_version", _no_version)
    _admin_caller(monkeypatch, is_admin=True)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_prompt",
        name="Reply Style",
        content="Be concise.",
        description="house style",
    )
    assert not result.is_error
    assert "created successfully" in result.content
    assert created and created[0].commits == 1
    assert len(created[0].added) == 1
    assert created[0].added[0].name == "Reply Style"


@pytest.mark.asyncio
async def test_delete_variable_happy_path_commits(monkeypatch):
    monkeypatch.setattr(config_module.settings, "disable_custom_variables", False)
    fake_var = SimpleNamespace(id=uuid.uuid4(), name="STALE_VAR")
    _stub_manageable_gate(monkeypatch, entity=fake_var)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "delete_variable", variable_id=str(fake_var.id))
    assert not result.is_error
    assert "has been deleted" in result.content
    assert created and created[0].commits == 1
    assert created[0].deleted == [fake_var]


@pytest.mark.asyncio
async def test_create_mcp_server_rejects_malformed_env_json():
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_mcp_server",
        name="srv",
        transport="stdio",
        command="my-mcp-server",  # satisfy the stdio-requires-command guard
        env="{not json",
    )
    assert "Invalid env" in result.content


@pytest.mark.asyncio
async def test_delete_kb_document_purges_embeddings_and_decrements_chunk_count(
    monkeypatch,
):
    """Deleting a KB document must also delete its embedding chunks (keyed by
    ``source_id == str(doc.id)``, not a FK, so no cascade) and decrement the
    KB's denormalised ``chunk_count`` — otherwise the deleted document's chunks
    keep surfacing in RAG / hybrid search.
    """
    import app.services.kb_helpers as kb_helpers

    registered: list = []
    monkeypatch.setattr(
        kb_helpers, "cancel_document_jobs_after_commit", lambda db, ids: registered.append((db, list(ids)))
    )

    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="Docs KB",
        is_builtin=False,
        document_count=3,
        chunk_count=10,
    )
    _stub_manageable_gate(monkeypatch, entity=kb)
    doc = SimpleNamespace(
        id=uuid.uuid4(),
        filename="report.pdf",
        chunk_count=4,
        knowledge_base_id=kb.id,
    )
    created: list = []
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[doc])
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "delete_kb_document",
        knowledge_base_id=str(kb.id),
        document_id=str(doc.id),
    )

    assert not result.is_error
    assert "deleted" in result.content.lower()
    # The document row was deleted, the KB counters kept in sync, and an
    # embeddings DELETE was issued (second execute) before the commit.
    assert doc in created[0].deleted
    assert kb.document_count == 2
    assert kb.chunk_count == 6  # 10 − doc.chunk_count(4); pre-fix stayed at 10
    assert created[0].commits == 1
    # Its queued jobs are handed to the session, cancelled once it commits.
    assert registered == [(created[0], [doc.id])]


@pytest.mark.asyncio
async def test_delete_kb_document_normalizes_inconsistent_chunk_count(monkeypatch):
    """If the KB's denormalised ``chunk_count`` is somehow falsy (``None``/0)
    while the deleted document reports chunks, the decrement must still apply
    and be floored at 0 — the old ``and kb.chunk_count`` guard skipped it,
    leaving the counter in its inconsistent state (e.g. ``None``).
    """
    import app.services.kb_helpers as kb_helpers

    monkeypatch.setattr(kb_helpers, "cancel_document_jobs_after_commit", lambda db, ids: None)

    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="Docs KB",
        is_builtin=False,
        document_count=1,
        chunk_count=None,  # inconsistent: counter desynced from real chunks
    )
    _stub_manageable_gate(monkeypatch, entity=kb)
    doc = SimpleNamespace(
        id=uuid.uuid4(),
        filename="report.pdf",
        chunk_count=4,
        knowledge_base_id=kb.id,
    )
    created: list = []
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[doc])
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "delete_kb_document",
        knowledge_base_id=str(kb.id),
        document_id=str(doc.id),
    )

    assert not result.is_error
    # Post-fix: normalised to 0 (max(0, 0 − 4)); pre-fix left it as None.
    assert kb.chunk_count == 0
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_get_kb_document_returns_full_dechunked_content_and_status(monkeypatch):
    """get_kb_document must return the document's FULL de-chunked content
    (the stored original, not search fragments) plus its embedding processing
    status — the exact-fetch-by-id contract."""
    from datetime import UTC, datetime

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    kb = SimpleNamespace(id=uuid.uuid4(), name="CSIRT Cases")
    doc = SimpleNamespace(
        id=uuid.uuid4(),
        filename="incident-8948.md",
        source_type="text",
        status="ready",
        chunk_count=3,
        source_url=None,
        content="# Incident 8948\nFull reassembled case-file body.",
        error_message=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        knowledge_base_id=kb.id,
    )
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[kb, doc]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "get_kb_document",
        knowledge_base_id=str(kb.id),
        document_id=str(doc.id),
    )

    assert not result.is_error
    assert "incident-8948.md" in result.content
    # Full de-chunked content is returned verbatim, not ranked fragments.
    assert "Full reassembled case-file body." in result.content
    # Processing status is surfaced so callers can detect not-yet-indexed docs.
    assert "ready" in result.content


@pytest.mark.asyncio
async def test_get_kb_document_returns_empty_string_content_verbatim(monkeypatch):
    """An intentionally-empty document (content == "") is a distinct state from
    "no stored text" (content is None): the exact-get tool must round-trip it as
    empty content (the "Content (full):" header), NOT the NULL fallback message."""

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    kb = SimpleNamespace(id=uuid.uuid4(), name="CSIRT Cases")
    doc = SimpleNamespace(
        id=uuid.uuid4(),
        filename="empty-placeholder.md",
        source_type="text",
        status="ready",
        chunk_count=0,
        source_url=None,
        content="",  # intentionally empty, not NULL
        error_message=None,
        created_at=None,
        updated_at=None,
        knowledge_base_id=kb.id,
    )
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[kb, doc]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "get_kb_document",
        knowledge_base_id=str(kb.id),
        document_id=str(doc.id),
    )

    assert not result.is_error
    assert "empty-placeholder.md" in result.content
    # The empty document is returned as empty content (header present)...
    assert "Content (full):" in result.content
    # ...and is NOT mislabeled as having no stored text.
    assert "no stored text content" not in result.content


@pytest.mark.asyncio
async def test_get_kb_document_flags_not_yet_embedded_document(monkeypatch):
    """A document whose embedding is still pending must still return its full
    content (stored at insert time) with a clear not-yet-searchable note — the
    write-then-read race the tool is meant to serve."""

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    kb = SimpleNamespace(id=uuid.uuid4(), name="CSIRT Cases")
    doc = SimpleNamespace(
        id=uuid.uuid4(),
        filename="incident-9001.md",
        source_type="text",
        status="pending",
        chunk_count=0,
        source_url=None,
        content="Just written, not embedded yet.",
        error_message=None,
        created_at=None,
        updated_at=None,
        knowledge_base_id=kb.id,
    )
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[kb, doc]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "get_kb_document",
        knowledge_base_id=str(kb.id),
        document_id=str(doc.id),
    )

    assert not result.is_error
    assert "Just written, not embedded yet." in result.content
    assert "pending" in result.content
    assert "not yet searchable" in result.content


@pytest.mark.asyncio
async def test_get_kb_document_rejects_invalid_uuid():
    """Invalid UUID arguments are rejected before any DB session is opened."""
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "get_kb_document",
        knowledge_base_id="not-a-uuid",
        document_id=str(uuid.uuid4()),
    )
    assert "must be a valid UUID" in result.content


@pytest.mark.asyncio
async def test_get_kb_document_not_found_when_document_missing(monkeypatch):
    """When the KB is visible but the document id is absent, return a clear
    not-found message rather than crashing."""

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    kb = SimpleNamespace(id=uuid.uuid4(), name="CSIRT Cases")
    created: list = []
    # execute_results: [kb, None] → KB visible, document row missing.
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[kb, None]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "get_kb_document",
        knowledge_base_id=str(kb.id),
        document_id=str(uuid.uuid4()),
    )

    assert not result.is_error
    assert "not found" in result.content.lower()


@pytest.mark.asyncio
async def test_update_variable_to_secret_without_value_encrypts_existing_plaintext(
    monkeypatch,
):
    """Switching a variable to secret without supplying a new value must migrate
    the existing plaintext into the encrypted column, never leave it in the
    plaintext ``value`` field (the "secrets encrypted at rest" contract).
    """
    import app.core.security as security_module

    monkeypatch.setattr(config_module.settings, "disable_custom_variables", False)
    monkeypatch.setattr(security_module, "encrypt_value", lambda v: f"ENC({v})")

    var = SimpleNamespace(
        id=uuid.uuid4(),
        name="API_KEY",
        value="plaintext-secret",
        value_enc=None,
        is_secret=False,
    )
    _stub_manageable_gate(monkeypatch, entity=var)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_variable", variable_id=str(var.id), is_secret="true"
    )

    assert not result.is_error
    assert var.is_secret is True
    assert var.value is None  # plaintext no longer stored
    assert var.value_enc == "ENC(plaintext-secret)"
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_variable_unsecret_without_value_is_rejected(monkeypatch):
    """Turning ``is_secret`` off without a new ``value`` must be rejected: the
    secret lives in ``value_enc`` and isn't decrypted into the plaintext column
    here, so allowing it would make the variable resolve to an empty string at
    runtime. The update must not commit.
    """
    monkeypatch.setattr(config_module.settings, "disable_custom_variables", False)

    var = SimpleNamespace(
        id=uuid.uuid4(),
        name="API_KEY",
        value=None,
        value_enc="ENC(prod-secret)",
        is_secret=True,
    )
    _stub_manageable_gate(monkeypatch, entity=var)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_variable", variable_id=str(var.id), is_secret="false"
    )

    assert not result.is_error
    assert "requires a new 'value'" in result.content
    # Nothing persisted; the encrypted secret is untouched.
    assert created[0].commits == 0
    assert var.value_enc == "ENC(prod-secret)"


@pytest.mark.asyncio
async def test_create_skill_rejects_url_and_content():
    """``url`` and ``content`` are mutually exclusive (same contract as
    ``update_skill``) — providing both must be rejected rather than silently
    preferring the URL."""
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_skill",
        name="My Skill",
        url="https://example.com/SKILL.md",
        content="# inline",
    )
    assert "not both" in result.content


@pytest.mark.asyncio
async def test_create_skill_url_with_whitespace_content_not_rejected(monkeypatch):
    """``url`` + a whitespace-only ``content`` (a padding glitch) must NOT be
    rejected as "both" - whitespace-only content is "not provided", so the URL
    is used (consistent with the emptiness check and the rest of the builder).
    """
    import app.services.skill_source as skill_source_module

    async def _fake_fetch(_url):
        return "# Fetched body", {"name": "Fetched Skill"}

    monkeypatch.setattr(skill_source_module, "fetch_skill_content", _fake_fetch)
    _admin_caller(monkeypatch)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_skill",
        url="https://example.com/SKILL.md",
        content="   ",
    )

    assert not result.is_error
    assert "not both" not in result.content
    assert "created successfully" in result.content
    # Built from the fetched URL body, not the blank content.
    skills = [o for o in created[0].added if type(o).__name__ == "AgentSkill"]
    assert len(skills) == 1
    assert skills[0].content == "# Fetched body"
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_skill_url_with_whitespace_content_refetches(monkeypatch):
    """``url`` + whitespace-only ``content`` must re-fetch the URL and ignore the
    blank content: not rejected as "both", and the blank body must not undo the
    re-fetch (which the old plain-truthiness ``if content:`` storage check did).
    """
    import app.services.skill_source as skill_source_module

    async def _fake_fetch(_url):
        return "# Fresh", {"name": "Fresh Name"}

    monkeypatch.setattr(skill_source_module, "fetch_skill_content", _fake_fetch)

    skill = SimpleNamespace(
        id=uuid.uuid4(),
        name="Old",
        url="https://example.com/old/SKILL.md",
        content="# old",
        description=None,
        compatibility=None,
        fetched_at=None,
        tags=[],
    )
    _stub_manageable_gate(monkeypatch, entity=skill)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_skill",
        skill_id=str(skill.id),
        url="https://example.com/new/SKILL.md",
        content="   ",
    )

    assert not result.is_error
    assert "not both" not in result.content
    # The URL re-fetch took effect and was NOT undone by the blank content.
    assert skill.url == "https://example.com/new/SKILL.md"
    assert skill.content == "# Fresh"
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_create_mcp_server_validates_transport_fields():
    """A transport/field mismatch must fail fast rather than persist a broken
    server the MCP runtime would later reject."""
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    stdio = await _call(executor, "create_mcp_server", name="srv", transport="stdio")
    assert "requires a 'command'" in stdio.content

    sse = await _call(
        executor, "create_mcp_server", name="srv", transport="sse"
    )
    assert "requires a 'url'" in sse.content


@pytest.mark.asyncio
async def test_update_skill_refetches_from_new_url(monkeypatch):
    """Updating a skill with a new ``url`` must re-fetch its content and refresh
    metadata, mirroring the REST ``PUT /agent-skills/{id}`` behaviour."""
    import app.services.skill_source as skill_source_module

    async def _fake_fetch(_url):
        return "# Fresh content", {"name": "Fetched Name"}

    monkeypatch.setattr(skill_source_module, "fetch_skill_content", _fake_fetch)

    skill = SimpleNamespace(
        id=uuid.uuid4(),
        name="Old Name",
        url="https://example.com/old/SKILL.md",
        content="# old",
        description=None,
        compatibility=None,
        fetched_at=None,
        tags=[],
    )
    _stub_manageable_gate(monkeypatch, entity=skill)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_skill",
        skill_id=str(skill.id),
        url="https://example.com/new/SKILL.md",
    )

    assert not result.is_error
    assert skill.url == "https://example.com/new/SKILL.md"
    assert skill.content == "# Fresh content"
    assert skill.name == "Fetched Name"  # backfilled from frontmatter
    assert skill.fetched_at is not None
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_variable_whitespace_only_name_leaves_unchanged(monkeypatch):
    """A whitespace-only ``name`` is "leave unchanged" — consistent with the
    other ``update_*`` builder tools (an LLM/UI padding glitch must not fail the
    call). With no other field it's a clean no-op: nothing commits and the
    existing name stays intact. (Creating a variable still requires a real name.)
    """
    monkeypatch.setattr(config_module.settings, "disable_custom_variables", False)

    var = SimpleNamespace(
        id=uuid.uuid4(),
        name="ORIGINAL_NAME",
        value="v",
        value_enc=None,
        is_secret=False,
    )
    _stub_manageable_gate(monkeypatch, entity=var)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_variable", variable_id=str(var.id), name="   "
    )

    assert not result.is_error
    assert result.content == "No fields to update. Provide at least one field."
    # Nothing persisted; the original name is untouched.
    assert var.name == "ORIGINAL_NAME"
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_delete_prompt_unbinds_from_agents(monkeypatch):
    """Deleting a prompt must purge its id from every agent's ``prompt_ids`` so
    no agent keeps a stale binding (parity with delete_mcp_server /
    delete_custom_tool).
    """
    prompt = SimpleNamespace(id=uuid.uuid4(), name="Reply Style")
    _stub_manageable_gate(monkeypatch, entity=prompt)
    pid = str(prompt.id)
    other_id = str(uuid.uuid4())
    agent = SimpleNamespace(prompt_ids=[pid, other_id])
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[[agent]]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "delete_prompt", prompt_id=pid)

    assert not result.is_error
    assert "has been deleted" in result.content
    assert agent.prompt_ids == [other_id]  # only the deleted id was removed
    assert prompt in created[0].deleted
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_delete_skill_unbinds_from_agents(monkeypatch):
    """Deleting a skill must purge its id from every agent's ``skill_ids``."""
    skill = SimpleNamespace(id=uuid.uuid4(), name="Triage", tags=[])
    _stub_manageable_gate(monkeypatch, entity=skill)
    sid = str(skill.id)
    other_id = str(uuid.uuid4())
    agent = SimpleNamespace(skill_ids=[sid, other_id])
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[[agent]]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "delete_skill", skill_id=sid)

    assert not result.is_error
    assert "has been deleted" in result.content
    assert agent.skill_ids == [other_id]
    assert skill in created[0].deleted
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_delete_integration_purges_integration_bindings(monkeypatch):
    """Deleting an integration must purge its id from every agent's
    ``integration_bindings`` (parity with REST ``DELETE /integrations/{id}``);
    a provider key whose list becomes empty is dropped entirely.
    """
    integ = SimpleNamespace(id=uuid.uuid4(), name="GitHub Prod")
    _stub_manageable_gate(monkeypatch, entity=integ)
    iid = str(integ.id)
    other_id = str(uuid.uuid4())
    agent = SimpleNamespace(
        integration_bindings={"github": [iid], "slack": [other_id]}
    )
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[[agent]]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "delete_integration", integration_id=iid)

    assert not result.is_error
    assert "has been deleted" in result.content
    # The github key (now empty) is dropped; the unrelated slack binding stays.
    assert agent.integration_bindings == {"slack": [other_id]}
    assert integ in created[0].deleted
    assert created[0].commits == 1


# ---------------------------------------------------------------------------
# update_mcp_server — whitespace hardening + full-CRUD-parity fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_mcp_server_whitespace_command_url_leave_unchanged(monkeypatch):
    """Whitespace-only ``command``/``url`` must be treated as "leave unchanged"
    (not persisted as a blank, unusable value) — mirroring the ``llm_model``
    hardening in ``update_agent``. With no other field, it is a no-op: nothing
    changes and nothing commits.
    """
    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="GitHub MCP",
        command="my-mcp-server",
        url="https://mcp.example.com",
    )
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_mcp_server", server_id=str(srv.id), command="   ", url=" "
    )

    assert not result.is_error
    assert result.content == "No fields to update. Provide at least one field."
    assert srv.command == "my-mcp-server"  # untouched
    assert srv.url == "https://mcp.example.com"  # untouched
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_mcp_server_updates_remote_and_oauth_fields(monkeypatch):
    """The builder must reach CRUD parity with the REST API: transport,
    auth_mode, OAuth client fields, scopes, tags, and hide_from_chat are all
    editable. Secrets are encrypted and never echoed back."""
    import socket

    import app.core.security as security_module
    from app.integrations import token_endpoint

    monkeypatch.setattr(security_module, "encrypt_value", lambda v: f"ENC({v})")

    async def _resolve(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

    # The token URL is held to the token endpoint policy: answer for DNS.
    monkeypatch.setattr(token_endpoint, "_resolve", _resolve)

    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="Remote MCP",
        transport="stdio",
        command="old",
        url=None,
        auth_mode="headers",
        client_id_enc=None,
        client_secret_enc=None,
        scopes=None,
        authorize_url=None,
        token_url=None,
        tags=[],
        hide_from_chat=False,
        enabled=True,
    )
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_mcp_server",
        server_id=str(srv.id),
        transport="streamable_http",
        url="  https://mcp.example.com/sse  ",
        auth_mode="oauth",
        client_id="oauth-client-id",
        client_secret="oauth-client-secret",
        scopes="read write",
        authorize_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
        tags="prod, mcp",
        hide_from_chat="true",
    )

    assert not result.is_error
    assert srv.transport == "streamable_http"
    assert srv.url == "https://mcp.example.com/sse"  # canonicalised (stripped)
    assert srv.auth_mode == "oauth"
    assert srv.client_id_enc == "ENC(oauth-client-id)"
    assert srv.client_secret_enc == "ENC(oauth-client-secret)"
    assert srv.scopes == "read write"
    assert srv.authorize_url == "https://auth.example.com/authorize"
    assert srv.token_url == "https://auth.example.com/token"
    assert srv.tags == ["prod", "mcp"]
    assert srv.hide_from_chat is True
    # Secrets are never echoed back in the tool result.
    assert "oauth-client-id" not in result.content
    assert "oauth-client-secret" not in result.content
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_mcp_server_auth_mode_headers_resets_oauth_session(monkeypatch):
    """Switching auth_mode back to 'headers' must invalidate any existing OAuth
    session (status + tokens), mirroring the REST ``update_mcp_server`` reset."""
    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="Remote MCP",
        auth_mode="oauth",
        oauth_status="connected",
        access_token_enc="tok",
        refresh_token_enc="ref",
        token_expires_at=object(),
    )
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_mcp_server", server_id=str(srv.id), auth_mode="headers"
    )

    assert not result.is_error
    assert srv.auth_mode == "headers"
    assert srv.oauth_status == "disconnected"
    assert srv.access_token_enc is None
    assert srv.refresh_token_enc is None
    assert srv.token_expires_at is None
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_mcp_server_rejects_invalid_transport():
    """An unknown transport must be rejected up front (before the DB session)."""
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_mcp_server",
        server_id=str(uuid.uuid4()),
        transport="carrier-pigeon",
    )
    assert "Invalid transport" in result.content


# ---------------------------------------------------------------------------
# update_integration — auth-type switching + OAuth endpoint fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_integration_switches_auth_type_to_platform_managed(monkeypatch):
    """Switching auth_type to 'platform_managed' must clear stored credentials
    and mark the integration connected, mirroring REST ``update_integration``.
    """
    import app.api.v1.integration_catalog as integration_catalog_module

    monkeypatch.setattr(
        integration_catalog_module, "PROVIDER_CATALOG", {"acme": {}}, raising=False
    )

    from app.core.security import encrypt_value

    integ = SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme",
        provider="acme",
        auth_type="api_key",
        status="connected",
        api_key_enc="ENC(key)",
        access_token_enc="ENC(tok)",
        refresh_token_enc="ENC(ref)",
        client_secret_enc="ENC(sec)",
        # The platform's base_url: without one the switch is refused (#8386).
        extra_config_enc=encrypt_value(json.dumps({"base_url": "https://platform.example.com"})),
    )
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_integration",
        integration_id=str(integ.id),
        auth_type="platform_managed",
    )

    assert not result.is_error, result.content
    assert integ.auth_type == "platform_managed"
    assert integ.api_key_enc is None
    assert integ.access_token_enc is None
    assert integ.refresh_token_enc is None
    assert integ.client_secret_enc is None
    assert integ.status == "connected"
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_integration_switching_oauth_to_an_api_key_connects_it(monkeypatch):
    """#2553: an OAuth integration left "configured" and switched to an API key
    with the key is connected, as in the REST update; never Pre-configured."""
    import app.api.v1.integration_catalog as integration_catalog_module

    monkeypatch.setattr(
        integration_catalog_module,
        "PROVIDER_CATALOG",
        {"acme": {"supported_auth_types": ["oauth", "api_key"]}},
        raising=False,
    )
    integ = SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme",
        provider="acme",
        auth_type="oauth",
        status="configured",
        api_key_enc=None,
        client_id_enc="ENC(app)",
        access_token_enc=None,
        refresh_token_enc=None,
        extra_config_enc=None,
    )
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_integration", integration_id=str(integ.id), auth_type="api_key", api_key="key-2553"
    )

    assert not result.is_error, result.content
    assert integ.auth_type == "api_key"
    assert integ.status == "connected"


@pytest.mark.asyncio
async def test_update_integration_refuses_platform_managed_without_a_base_url(monkeypatch):
    """#8386: a platform_managed integration with no base_url has no credential
    at all; the switch is refused before anything is written."""
    import app.api.v1.integration_catalog as integration_catalog_module

    monkeypatch.setattr(
        integration_catalog_module, "PROVIDER_CATALOG", {"acme": {}}, raising=False
    )
    integ = SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme",
        provider="acme",
        auth_type="api_key",
        status="connected",
        api_key_enc="ENC(key)",
        extra_config_enc=None,
    )
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_integration", integration_id=str(integ.id), auth_type="platform_managed"
    )

    assert "needs the platform's base_url" in result.content
    assert integ.auth_type == "api_key"
    assert integ.api_key_enc == "ENC(key)"
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_integration_refuses_a_type_outside_the_default_auth_type(monkeypatch):
    """#8386: a provider that declares only ``auth_type`` (an OAuth-only provider)
    supports exactly that type: platform_managed is refused, naming oauth."""
    import app.api.v1.integration_catalog as integration_catalog_module

    monkeypatch.setattr(
        integration_catalog_module,
        "PROVIDER_CATALOG",
        {"acme": {"auth_type": "oauth"}},
        raising=False,
    )
    integ = SimpleNamespace(
        id=uuid.uuid4(), name="Acme", provider="acme", auth_type="oauth", status="configured"
    )
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_integration", integration_id=str(integ.id), auth_type="platform_managed"
    )

    assert "Auth type 'platform_managed' is not supported for provider 'acme'. Supported: oauth" in result.content
    assert integ.auth_type == "oauth"
    assert integ.status == "configured"
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_create_integration_refuses_a_type_outside_the_default_auth_type(monkeypatch):
    import app.api.v1.integration_catalog as integration_catalog_module

    monkeypatch.setattr(
        integration_catalog_module,
        "PROVIDER_CATALOG",
        {"acme": {"name": "Acme", "auth_type": "oauth"}},
        raising=False,
    )
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "create_integration", provider="acme", name="Acme", auth_type="platform_managed"
    )

    assert "is not supported for provider 'acme'. Supported: oauth" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_update_integration_switching_away_from_oauth_drops_the_personal_token(monkeypatch):
    """Like REST ``update_integration``: the personal-token rule keys on
    ``auth_type``, so a token left behind would become usable by everyone the
    integration is shared with."""
    import app.api.v1.integration_catalog as integration_catalog_module

    monkeypatch.setattr(
        integration_catalog_module, "PROVIDER_CATALOG", {"acme": {}}, raising=False
    )

    integ = SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme",
        provider="acme",
        auth_type="oauth",
        status="connected",
        api_key_enc=None,
        access_token_enc="ENC(tok)",
        refresh_token_enc="ENC(ref)",
        token_expires_at="2026-09-26T00:00:00Z",
        oauth_share_acknowledged=True,
        oauth_token_owner_id=uuid.uuid4(),
        client_secret_enc="ENC(sec)",
    )
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_integration",
        integration_id=str(integ.id),
        auth_type="api_key",
    )

    assert not result.is_error
    assert integ.auth_type == "api_key"
    assert integ.access_token_enc is None
    assert integ.refresh_token_enc is None
    assert integ.token_expires_at is None
    assert integ.oauth_share_acknowledged is False
    # Who connected the removed token goes with it.
    assert integ.oauth_token_owner_id is None
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_integration_rejects_unsupported_auth_type(monkeypatch):
    """An auth_type the provider doesn't support must be rejected before commit,
    leaving the existing auth_type untouched."""
    import app.api.v1.integration_catalog as integration_catalog_module

    monkeypatch.setattr(
        integration_catalog_module,
        "PROVIDER_CATALOG",
        {"acme": {"supported_auth_types": ["api_key"]}},
        raising=False,
    )

    integ = SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme",
        provider="acme",
        auth_type="api_key",
        status="connected",
    )
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_integration",
        integration_id=str(integ.id),
        auth_type="oauth",
    )

    assert not result.is_error
    assert "is not supported for provider" in result.content
    assert integ.auth_type == "api_key"  # untouched
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_integration_updates_oauth_urls_and_scopes(monkeypatch):
    """authorize_url / token_url / scopes are editable; whitespace-only values
    are treated as "leave unchanged" and stored values are stripped."""
    integ = SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme",
        provider="acme",
        auth_type="oauth",
        status="configured",
        authorize_url=None,
        token_url="https://old.example.com/token",
        scopes=None,
    )
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_integration",
        integration_id=str(integ.id),
        authorize_url="  https://auth.example.com/authorize  ",
        token_url="   ",  # whitespace-only → leave unchanged
        scopes="repo,read:org",
    )

    assert not result.is_error
    assert integ.authorize_url == "https://auth.example.com/authorize"  # stripped
    assert integ.token_url == "https://old.example.com/token"  # untouched
    assert integ.scopes == "repo,read:org"
    assert created[0].commits == 1


# ---------------------------------------------------------------------------
# The admin OAuth app fallback on create_integration / update_integration
# ---------------------------------------------------------------------------

_ADMIN_APP_ID = "admin-app-client-id-fallback"
_ADMIN_APP_SECRET = "admin-app-client-secret-fallback"


class _OAuthDefaultSession(_FakeSession):
    """A session answering the OAuth default lookup with *oauth_default*."""

    def __init__(self, created, *, oauth_default=None, **kwargs):
        super().__init__(created, **kwargs)
        self.oauth_default = oauth_default
        self.oauth_default_reads = 0

    async def execute(self, stmt):
        from app.models.oauth_default import OAuthDefault

        descriptions = getattr(stmt, "column_descriptions", None) or [{}]
        if descriptions[0].get("entity") is OAuthDefault:
            self.oauth_default_reads += 1
            return _ScalarResult(self.oauth_default)
        return await super().execute(stmt)


def _oauth_default_session_factory(created, *, oauth_default=None):
    def _make():
        return _OAuthDefaultSession(created, oauth_default=oauth_default)

    return _make


def _microsoft_admin_default():
    from app.core.security import encrypt_value
    from app.models.oauth_default import OAuthDefault

    return OAuthDefault(
        provider="microsoft",
        client_id_enc=encrypt_value(_ADMIN_APP_ID),
        client_secret_enc=encrypt_value(_ADMIN_APP_SECRET),
    )


def _added_integration(created):
    from app.models.integration import Integration

    rows = [obj for session in created for obj in session.added if isinstance(obj, Integration)]
    assert len(rows) == 1
    return rows[0]


@pytest.mark.asyncio
async def test_create_integration_holds_the_admin_oauth_app_without_credentials(monkeypatch, caplog):
    from app.core.security import decrypt_value

    _admin_caller(monkeypatch, is_admin=False)
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _oauth_default_session_factory(created, oauth_default=_microsoft_admin_default()),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    with caplog.at_level("INFO"):
        result = await _call(executor, "create_integration", provider="microsoft", name="MS")

    assert not result.is_error, result.content
    stored = _added_integration(created)
    assert decrypt_value(stored.client_id_enc) == _ADMIN_APP_ID
    assert decrypt_value(stored.client_secret_enc) == _ADMIN_APP_SECRET
    assert stored.uses_admin_oauth_credentials is True
    assert stored.status == "configured"
    assert "Ready to connect with the platform's OAuth app" in result.content
    assert "Auto-filled OAuth credentials from admin defaults" in caplog.text
    # Never served, never logged.
    for text in (result.content, caplog.text):
        assert _ADMIN_APP_ID not in text
        assert _ADMIN_APP_SECRET not in text


@pytest.mark.asyncio
async def test_create_integration_without_an_admin_default_is_unchanged(monkeypatch):
    _admin_caller(monkeypatch, is_admin=False)
    created: list = []
    monkeypatch.setattr(database, "async_session", _oauth_default_session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "create_integration", provider="microsoft", name="MS")

    assert not result.is_error, result.content
    stored = _added_integration(created)
    assert stored.client_id_enc is None
    assert stored.uses_admin_oauth_credentials is not True
    assert stored.status != "configured"
    assert "Ready to connect" not in result.content


@pytest.mark.asyncio
async def test_create_integration_company_managed_never_takes_the_admin_default(monkeypatch):
    _admin_caller(monkeypatch, is_admin=True)
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _oauth_default_session_factory(created, oauth_default=_microsoft_admin_default()),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "create_integration", provider="microsoft", name="MS", is_company_managed=True
    )

    assert not result.is_error, result.content
    stored = _added_integration(created)
    assert stored.client_id_enc is None
    assert stored.uses_admin_oauth_credentials is not True
    assert all(s.oauth_default_reads == 0 for s in created)


def _oauth_row(**overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "MS",
        "provider": "microsoft",
        "auth_type": "oauth",
        "status": "disconnected",
        "user_id": uuid.uuid4(),
        "is_company_managed": False,
        "tool_slug": "ms_team",
        "client_id_enc": None,
        "client_secret_enc": None,
        "extra_config_enc": None,
        "api_key_enc": None,
        "uses_admin_oauth_credentials": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_update_integration_repairs_an_oauth_row_onto_the_admin_app(monkeypatch):
    from app.core.security import decrypt_value

    integ = _oauth_row()
    _stub_manageable_gate(monkeypatch, entity=integ)
    _admin_caller(monkeypatch, is_admin=False)
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _oauth_default_session_factory(created, oauth_default=_microsoft_admin_default()),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "update_integration", integration_id=str(integ.id))

    assert not result.is_error, result.content
    assert decrypt_value(integ.client_id_enc) == _ADMIN_APP_ID
    assert decrypt_value(integ.client_secret_enc) == _ADMIN_APP_SECRET
    assert integ.uses_admin_oauth_credentials is True
    assert integ.status == "configured"
    # Repaired in place: the id and the tool suffix the agents use are kept.
    assert integ.tool_slug == "ms_team"
    assert created[0].commits == 1
    assert "Ready to connect with the platform's OAuth app" in result.content
    assert _ADMIN_APP_ID not in result.content
    assert _ADMIN_APP_SECRET not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"client_id_enc": "ENC(own-app)", "status": "configured"},
        {"status": "connected"},
        {"is_company_managed": True},
    ],
    ids=["own-client-id", "connected", "company-managed"],
)
async def test_update_integration_leaves_other_rows_alone(monkeypatch, overrides):
    integ = _oauth_row(**overrides)
    before = dict(vars(integ))
    _stub_manageable_gate(monkeypatch, entity=integ)
    _admin_caller(monkeypatch, is_admin=True)
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _oauth_default_session_factory(created, oauth_default=_microsoft_admin_default()),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "update_integration", integration_id=str(integ.id))

    assert result.content == "No fields to update. Provide at least one field."
    assert vars(integ) == before
    assert created[0].commits == 0


# ---------------------------------------------------------------------------
# tool_slug on update_integration / create_integration
# ---------------------------------------------------------------------------


class _SlugSession(_FakeSession):
    """A session whose ``scalar`` answers the tool-slug uniqueness count."""

    def __init__(self, created, *, taken=0, **kwargs):
        super().__init__(created, **kwargs)
        self.taken = taken
        self.scalar_calls = 0

    async def scalar(self, _stmt):
        self.scalar_calls += 1
        return self.taken


def _slug_session_factory(created, *, taken=0):
    def _make():
        return _SlugSession(created, taken=taken)

    return _make


def _slug_integration(**overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "Notion Filigran",
        "provider": "notion",
        "auth_type": "api_key",
        "status": "connected",
        "user_id": uuid.uuid4(),
        "is_company_managed": False,
        "tool_slug": "notion_filigran",
        "api_key_enc": "ENC(key)",
        "extra_config_enc": "ENC(extra)",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_update_integration_sets_the_tool_slug(monkeypatch):
    integ = _slug_integration()
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _slug_session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_integration", integration_id=str(integ.id), tool_slug="filigran_page"
    )

    assert not result.is_error
    assert "updated: tool_slug" in result.content
    assert "`__filigran_page`" in result.content
    assert integ.tool_slug == "filigran_page"
    # Credentials and extra config are not touched by a suffix change.
    assert integ.api_key_enc == "ENC(key)"
    assert integ.extra_config_enc == "ENC(extra)"
    assert created[0].scalar_calls == 1
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_integration_surfaces_a_tool_slug_collision(monkeypatch):
    integ = _slug_integration()
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _slug_session_factory(created, taken=1))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_integration", integration_id=str(integ.id), tool_slug="filigran_page"
    )

    assert "another notion integration of the same owner already uses the tool suffix 'filigran_page'" in result.content
    assert integ.tool_slug == "notion_filigran"
    assert created[0].commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["Filigran_Page", "filigran-page", "x" * 22, "_lead"])
async def test_update_integration_refuses_an_invalid_tool_slug_before_any_read(monkeypatch, bad):
    integ = _slug_integration()
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _slug_session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "update_integration", integration_id=str(integ.id), tool_slug=bad)

    assert result.content.startswith("Integration not updated - tool_slug")
    assert created == []  # no session opened, nothing written
    assert integ.tool_slug == "notion_filigran"


@pytest.mark.asyncio
async def test_update_integration_with_the_current_tool_slug_says_so(monkeypatch):
    integ = _slug_integration(tool_slug="filigran_page")
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _slug_session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_integration", integration_id=str(integ.id), tool_slug="filigran_page"
    )

    assert "already uses the tool suffix `__filigran_page`" in result.content
    assert created[0].scalar_calls == 0
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_create_integration_refuses_an_invalid_tool_slug_before_any_read(monkeypatch):
    created: list = []
    monkeypatch.setattr(database, "async_session", _slug_session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "create_integration", provider="github", name="GitHub Sam", tool_slug="Git-Hub"
    )

    assert result.content.startswith("Integration not created - tool_slug")
    assert created == []


@pytest.mark.asyncio
async def test_create_integration_surfaces_a_tool_slug_collision(monkeypatch):
    _admin_caller(monkeypatch, is_admin=False)
    created: list = []
    monkeypatch.setattr(database, "async_session", _slug_session_factory(created, taken=1))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "create_integration", provider="github", name="GitHub Sam", tool_slug="github_sam"
    )

    assert "already uses the tool suffix 'github_sam'" in result.content
    assert created[0].added == []
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_get_integration_shows_the_tool_suffix(monkeypatch):
    integ = _slug_integration(description=None, tags=[], shared_group_ids=[])

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)
    created: list = []
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[integ])
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_integration", integration_id=str(integ.id))

    assert "Tool suffix: `__notion_filigran`" in result.content


def test_integration_tools_declare_the_tool_slug_argument():
    definitions, _ = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    by_name = {d.name: d for d in definitions}
    for name in ("create_integration", "update_integration"):
        assert "tool_slug" in by_name[name].input_schema["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(
    ("tool", "id_arg"),
    [("update_integration", "integration_id"), ("update_mcp_server", "server_id")],
)
async def test_the_builder_saves_a_loopback_token_url_unless_strict(monkeypatch, tool, id_arg, strict):
    """A loopback token URL is saved as it always was; only the opt-in
    OAUTH_TOKEN_URL_STRICT refuses it, and the refusal names the setting."""
    monkeypatch.setattr(config_module.settings, "oauth_token_url_strict", strict)
    monkeypatch.setattr(config_module.settings, "oauth_token_dev_trusted_origins", [])
    entity = SimpleNamespace(
        id=uuid.uuid4(),
        name="IdP",
        provider="acme",
        auth_type="oauth",
        status="configured",
        authorize_url=None,
        token_url=None,
        scopes=None,
    )
    _stub_manageable_gate(monkeypatch, entity=entity)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, tool, **{id_arg: str(entity.id)}, token_url="http://127.0.0.1:8080/token"
    )

    if strict:
        assert "Invalid token_url" in result.content
        assert "OAUTH_TOKEN_URL_STRICT=false" in result.content
        assert entity.token_url is None
        assert created == []
    else:
        assert not result.is_error
        assert entity.token_url == "http://127.0.0.1:8080/token"
        assert created[0].commits == 1


@pytest.mark.asyncio
async def test_create_mcp_server_rejects_whitespace_only_required_field():
    """A whitespace-only command/url must be rejected up front (not persisted as
    a blank, unusable server) — the truthiness validation would otherwise let
    ``url=" "`` through. Mirrors the update_mcp_server whitespace hardening.
    """
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    stdio = await _call(
        executor, "create_mcp_server", name="srv", transport="stdio", command="   "
    )
    assert "requires a 'command'" in stdio.content

    sse = await _call(
        executor, "create_mcp_server", name="srv", transport="sse", url="  "
    )
    assert "requires a 'url'" in sse.content


@pytest.mark.asyncio
async def test_update_mcp_server_rejects_transport_switch_without_required_field(
    monkeypatch,
):
    """Switching transport must re-validate the resulting config: switching a
    url-less server to a remote transport (or a command-less server to stdio)
    must be rejected before commit, so a transport switch can't persist a
    server the MCP runtime would reject.
    """
    # A stdio server (has a command, no url). Switching it to 'sse' without
    # also providing a url must be refused.
    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="Local MCP",
        transport="stdio",
        command="my-mcp-server",
        url=None,
    )
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_mcp_server", server_id=str(srv.id), transport="sse"
    )

    assert not result.is_error
    assert "requires a 'url'" in result.content
    assert created[0].commits == 0  # nothing persisted

    # The converse: a url-only server switched to stdio with no command.
    srv2 = SimpleNamespace(
        id=uuid.uuid4(),
        name="Remote MCP",
        transport="streamable_http",
        command=None,
        url="https://mcp.example.com",
    )
    _stub_manageable_gate(monkeypatch, entity=srv2)
    created2: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created2))

    _, executor2 = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result2 = await _call(
        executor2, "update_mcp_server", server_id=str(srv2.id), transport="stdio"
    )

    assert not result2.is_error
    assert "requires a 'command'" in result2.content
    assert created2[0].commits == 0


@pytest.mark.asyncio
async def test_update_mcp_server_allows_transport_switch_with_required_field(
    monkeypatch,
):
    """A transport switch that also supplies the required field must succeed."""
    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="Local MCP",
        transport="stdio",
        command="my-mcp-server",
        url=None,
    )
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_mcp_server",
        server_id=str(srv.id),
        transport="sse",
        url="https://mcp.example.com/sse",
    )

    assert not result.is_error
    assert srv.transport == "sse"
    assert srv.url == "https://mcp.example.com/sse"
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_create_skill_rejects_oversized_content():
    """Paste-mode content over the REST 1 MB guard must be rejected, not
    silently persisted — `parse_skill_frontmatter`'s size limit is the source of
    truth and the builder must honour it (parity with the REST API / URL path).
    """
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    oversized = "x" * (1024 * 1024 + 1)  # 1 MB + 1 byte
    result = await _call(executor, "create_skill", name="Big", content=oversized)
    assert "exceeds maximum size" in result.content


@pytest.mark.asyncio
async def test_update_skill_rejects_oversized_content(monkeypatch):
    """update_skill must enforce the same 1 MB size guard on inline content,
    rejecting before any mutation/commit (the REST API runs `parse_skill_frontmatter`
    on inline content; the builder must too).
    """
    skill = SimpleNamespace(
        id=uuid.uuid4(), name="Skill", url=None, content="old body", tags=[]
    )
    _stub_manageable_gate(monkeypatch, entity=skill)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    oversized = "x" * (1024 * 1024 + 1)
    result = await _call(
        executor, "update_skill", skill_id=str(skill.id), content=oversized
    )

    assert "exceeds maximum size" in result.content
    assert skill.content == "old body"  # untouched
    assert all(c.commits == 0 for c in created)  # nothing persisted


@pytest.mark.asyncio
async def test_create_agent_rejects_non_uuid_binding_ids(monkeypatch):
    """create_agent must validate id lists as UUIDs (parity with update_agent):
    a non-UUID id is rejected up front, before any DB write, instead of
    persisting a dead binding that later raises ValueError downstream.
    """
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "create_agent", name="My Agent", knowledge_base_ids="not-a-uuid"
    )
    assert "Invalid knowledge_base_ids" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_create_agent_rejects_non_uuid_integration_id(monkeypatch):
    """create_agent must reject a non-UUID integration_id (parity with
    update_agent) rather than silently persisting a broken binding.
    """
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_agent",
        name="My Agent",
        integration_bindings="github:not-a-uuid",
    )
    assert "Invalid integration_bindings" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_create_skill_whitespace_url_treated_as_not_provided():
    """A whitespace-only ``url`` must be treated as "not provided" (clear
    "provide url or content" error), not fetched as a blank URL with an opaque
    fetch/SSRF failure.
    """
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "create_skill", url="   ")
    assert "Provide either" in result.content


@pytest.mark.asyncio
async def test_update_skill_whitespace_url_leaves_unchanged(monkeypatch):
    """A whitespace-only ``url`` must be treated as "leave unchanged" (no
    re-fetch, no mutation), not a change against a blank URL."""
    skill = SimpleNamespace(
        id=uuid.uuid4(),
        name="Skill",
        url="https://orig.example.com/SKILL.md",
        content="body",
        tags=[],
    )
    _stub_manageable_gate(monkeypatch, entity=skill)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "update_skill", skill_id=str(skill.id), url="   ")

    assert "No fields to update" in result.content
    assert skill.url == "https://orig.example.com/SKILL.md"  # untouched
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_update_skill_strips_padded_url(monkeypatch):
    """A padded ``url`` must be re-fetched and stored in canonical stripped
    form, not with the surrounding whitespace."""
    import app.services.skill_source as skill_source_module

    async def _fake_fetch(_url):
        return "# Fresh content", {}

    monkeypatch.setattr(skill_source_module, "fetch_skill_content", _fake_fetch)

    skill = SimpleNamespace(
        id=uuid.uuid4(),
        name="Skill",
        url="https://orig.example.com/SKILL.md",
        content="old",
        description=None,
        compatibility=None,
        fetched_at=None,
        tags=[],
    )
    _stub_manageable_gate(monkeypatch, entity=skill)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_skill",
        skill_id=str(skill.id),
        url="  https://new.example.com/SKILL.md  ",
    )

    assert not result.is_error
    assert skill.url == "https://new.example.com/SKILL.md"  # stored stripped
    assert skill.content == "# Fresh content"
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_create_agent_rejects_malformed_integration_binding(monkeypatch):
    """A malformed integration_bindings pair (missing ':') must be rejected with
    a clear error, not silently dropped (parity with update_agent).
    """
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_agent",
        name="My Agent",
        integration_bindings="githubnocolon",
    )
    assert "Invalid integration_bindings" in result.content
    assert all(c.commits == 0 for c in created)


# ---------------------------------------------------------------------------
# Whitespace-name guards across the remaining create/update tools (sweep)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_prompt_rejects_whitespace_name(monkeypatch):
    """create_prompt must reject a whitespace-only name (parity with
    create_variable) rather than persisting a blank-named prompt."""
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "create_prompt", name="   ", content="Be concise.")
    assert "name is required" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_create_knowledge_base_rejects_whitespace_name(monkeypatch):
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "create_knowledge_base", name="   ")
    assert "name is required" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_create_mcp_server_rejects_whitespace_name(monkeypatch):
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "create_mcp_server", name="   ", transport="stdio", command="x"
    )
    assert "name is required" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_create_integration_rejects_whitespace_name(monkeypatch):
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "create_integration", provider="github", name="   ")
    assert "name is required" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_create_custom_tool_rejects_whitespace_name(monkeypatch):
    monkeypatch.setattr(config_module.settings, "disable_custom_tools", False)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "create_custom_tool", name="   ")
    assert "name is required" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_update_knowledge_base_strips_name(monkeypatch):
    """update_knowledge_base canonicalises a padded name to its stripped form."""
    kb = SimpleNamespace(
        id=uuid.uuid4(), name="Old", description=None, tags=[], is_builtin=False
    )
    _stub_manageable_gate(monkeypatch, entity=kb)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_knowledge_base", knowledge_base_id=str(kb.id), name="  Docs  "
    )
    assert not result.is_error
    assert kb.name == "Docs"  # stored stripped
    assert created[0].commits == 1


# ---------------------------------------------------------------------------
# Null-argument robustness (tool-call args are forwarded without type coercion,
# so a provider can send JSON ``null`` for a string field — the canonicalising
# ``.strip()`` must not raise AttributeError and turn a friendly validation
# message into an opaque tool-execution error).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_prompt_handles_null_name(monkeypatch):
    """A ``null`` required name must yield the friendly "name is required"
    message, not an AttributeError from ``None.strip()``."""
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "create_prompt", name=None, content="Be concise.")
    assert not result.is_error
    assert "name is required" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_create_agent_handles_null_name(monkeypatch):
    """``create_agent`` with a ``null`` name returns the friendly validation
    message rather than raising on ``None.strip()``."""
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "create_agent", name=None)
    assert not result.is_error
    assert "name is required" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_update_knowledge_base_handles_null_name_and_description(monkeypatch):
    """``null`` name/description on update must be treated as "leave unchanged"
    (no AttributeError), so with no other field it's a clean no-op."""
    kb = SimpleNamespace(
        id=uuid.uuid4(), name="Old", description="orig", tags=[], is_builtin=False
    )
    _stub_manageable_gate(monkeypatch, entity=kb)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_knowledge_base",
        knowledge_base_id=str(kb.id),
        name=None,
        description=None,
    )
    assert not result.is_error
    assert "No fields to update" in result.content
    assert kb.name == "Old"  # untouched
    assert kb.description == "orig"  # untouched
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_mcp_server_handles_null_transport_fields(monkeypatch):
    """``null`` transport/command/url/auth_mode/scopes/authorize_url/token_url
    (a provider can forward JSON null) must be treated as "leave unchanged" —
    not raise AttributeError from ``None.strip()``. With no other field it's a
    clean no-op."""
    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="GitHub MCP",
        transport="stdio",
        command="my-mcp-server",
        url="https://mcp.example.com",
        auth_mode="headers",
    )
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_mcp_server",
        server_id=str(srv.id),
        transport=None,
        command=None,
        url=None,
        args=None,
        auth_mode=None,
        scopes=None,
        authorize_url=None,
        token_url=None,
    )

    assert not result.is_error
    assert result.content == "No fields to update. Provide at least one field."
    assert srv.transport == "stdio"  # untouched
    assert srv.command == "my-mcp-server"  # untouched
    assert srv.url == "https://mcp.example.com"  # untouched
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_create_mcp_server_handles_null_command(monkeypatch):
    """``null`` command with the stdio transport must yield the friendly
    "requires a 'command'" validation message, not ``None.strip()`` crash."""
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_mcp_server",
        name="My Server",
        transport="stdio",
        command=None,
        url=None,
    )
    assert not result.is_error
    assert "requires a 'command'" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_update_integration_handles_null_auth_fields(monkeypatch):
    """``null`` auth_type/authorize_url/token_url/scopes must be "leave
    unchanged" (no AttributeError from ``None.strip()``); with no other field
    it's a clean no-op."""
    integ = SimpleNamespace(
        id=uuid.uuid4(),
        name="My Integration",
        provider="github",
        auth_type="api_key",
        authorize_url=None,
        token_url=None,
        scopes=None,
        tags=[],
    )
    _stub_manageable_gate(monkeypatch, entity=integ)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_integration",
        integration_id=str(integ.id),
        auth_type=None,
        authorize_url=None,
        token_url=None,
        scopes=None,
    )

    assert not result.is_error
    assert result.content == "No fields to update. Provide at least one field."
    assert integ.auth_type == "api_key"  # untouched
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_skill_handles_null_url_and_compatibility(monkeypatch):
    """``null`` url/compatibility must be "leave unchanged" (no AttributeError
    from ``None.strip()``); with no other field it's a clean no-op."""
    skill = SimpleNamespace(
        id=uuid.uuid4(),
        name="My Skill",
        url="https://example.com/SKILL.md",
        compatibility="claude",
        tags=[],
    )
    _stub_manageable_gate(monkeypatch, entity=skill)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_skill",
        skill_id=str(skill.id),
        url=None,
        content=None,
        compatibility=None,
    )

    assert not result.is_error
    assert "No fields to update" in result.content
    assert skill.compatibility == "claude"  # untouched
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_create_skill_handles_null_url_and_content(monkeypatch):
    """``null`` url AND content must yield the friendly "provide either url or
    content" message, not an AttributeError from ``None.strip()``."""
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "create_skill", name="My Skill", url=None, content=None
    )
    assert not result.is_error
    assert "Provide either" in result.content
    assert all(c.commits == 0 for c in created)


# ---------------------------------------------------------------------------
# list_integration_catalog — extra_fields is a dict, not a list of dicts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_integration_catalog_lists_dict_extra_fields(monkeypatch):
    """``extra_fields`` is a ``dict[str, str]`` (field key -> label) per
    ``schemas/integration.py``. The catalog tool must list its KEYS alongside
    ``fields`` — pre-fix it iterated the dict (yielding string keys) and called
    ``.get("key")`` on each, raising ``AttributeError: 'str' object has no
    attribute 'get'`` and failing the whole tool call for any provider with a
    populated ``extra_fields``.
    """
    import app.api.v1.integration_catalog as integration_catalog_module

    monkeypatch.setattr(
        integration_catalog_module,
        "PROVIDER_CATALOG",
        {
            "acme": {
                "name": "Acme",
                "auth_type": "api_key",
                "description": "Acme connector",
                "fields": ["api_key"],
                "extra_fields": {"region": "API Region", "tenant_id": "Tenant ID"},
            }
        },
        raising=False,
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_integration_catalog")

    assert not result.is_error
    # Base fields AND the extra_fields keys are surfaced.
    assert "api_key" in result.content
    assert "region" in result.content
    assert "tenant_id" in result.content
    # The dict VALUES (labels) are not what gets listed.
    assert "API Region" not in result.content


@pytest.mark.asyncio
async def test_list_integration_catalog_real_catalog_does_not_crash():
    """Smoke guard against the real ``PROVIDER_CATALOG``: every provider —
    including those with populated ``extra_fields`` dicts (e.g. ``microsoft``)
    — must render without raising, so the tool can never regress to the
    dict-iteration bug.
    """
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_integration_catalog")

    assert not result.is_error
    assert "Available integration providers" in result.content


# ---------------------------------------------------------------------------
# Read-before-write tools must surface content in FULL (never truncated) so the
# agent can faithfully rewrite it. get_agent (persona), get_prompt (content),
# and get_skill (content) previously truncated to 200 / 1000 chars, making
# every edit lossy (the agent reported "the persona was truncated").
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_agent_returns_full_persona(monkeypatch):
    """get_agent must return the FULL persona, not a 200-char preview, or the
    agent cannot faithfully rewrite a persona it just read."""

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    long_persona = "You are a compliance orchestrator. " + ("X" * 5000)
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Compliance Orchestrator",
        slug="compliance-orchestrator",
        description="desc",
        persona=long_persona,
        enabled=True,
        tags=[],
        knowledge_base_ids=[],
        mcp_server_ids=[],
        custom_tool_ids=[],
        integration_bindings={},
        prompt_ids=[],
        skill_ids=[],
        handover_agent_ids=[],
        handover_types={},
        is_company_managed=False,
        user_id=None,
    )
    created: list = []
    # Empty prompt/skill/handover lists -> no id->name lookups execute, so only
    # two executes are needed: the agent row then the assignment count.
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[agent, 0])
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_agent", agent_id=str(agent.id))

    assert not result.is_error
    assert "Persona (full):" in result.content
    assert long_persona in result.content  # full persona, not a 200-char slice
    assert "..." not in result.content  # no truncation marker


@pytest.mark.asyncio
async def test_get_prompt_returns_full_content(monkeypatch):
    """get_prompt must return the FULL content, not a 1000-char preview."""

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    long_content = "Prompt body. " + ("Y" * 5000)
    prompt = SimpleNamespace(
        id=uuid.uuid4(),
        name="House Style",
        description="desc",
        tags=[],
        hide_from_chat=False,
        is_company_managed=False,
        content=long_content,
        user_id=None,
    )
    created: list = []
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[prompt])
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_prompt", prompt_id=str(prompt.id))

    assert not result.is_error
    assert "Content (full):" in result.content
    assert long_content in result.content
    assert "first 1000 chars" not in result.content


# ---------------------------------------------------------------------------
# update_agent / create_agent — is_company_managed (visibility) toggle.
# Flipping platform-wide visibility is admin-only and routes through the shared
# REST toggle helper; the builder previously exposed no way to do it at all.
# ---------------------------------------------------------------------------


def _builder_agent(**fields) -> SimpleNamespace:
    """An agent row as update_agent reads it: ``user_id`` and the share lists
    are what ``agent_audience()`` compares before and after the edit."""
    return SimpleNamespace(**{"user_id": None, "shared_group_ids": None, "shared_user_ids": None, **fields})


@pytest.mark.asyncio
async def test_update_agent_company_managed_requires_admin(monkeypatch):
    """A non-admin caller must NOT be able to flip an agent company-managed via
    the builder — even when they can otherwise edit the agent."""
    agent = _builder_agent(id=uuid.uuid4(), name="Helper", is_company_managed=False)
    _stub_writable_agent(monkeypatch, agent=agent)
    _admin_caller(monkeypatch, is_admin=False)

    async def _toggle_must_not_run(*_a, **_k):
        raise AssertionError("toggle must not run for a non-admin")

    monkeypatch.setattr(
        builtin_tools_module,
        "_apply_agent_company_managed_toggle",
        _toggle_must_not_run,
    )
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_agent", agent_id=str(agent.id), is_company_managed="true"
    )

    assert not result.is_error
    assert "Only admins" in result.content
    assert agent.is_company_managed is False  # untouched
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_update_agent_company_managed_admin_toggles(monkeypatch):
    """An admin flips the agent company-managed; the shared toggle helper is
    invoked with the resolved want-state + owner fallback and the change
    commits."""
    agent = _builder_agent(id=uuid.uuid4(), name="Helper", is_company_managed=False)
    _stub_writable_agent(monkeypatch, agent=agent)
    admin = _admin_caller(monkeypatch, is_admin=True)

    seen: dict = {}

    async def _toggle(_db, _agent, want, owner_id):
        seen["want"] = want
        seen["owner_id"] = owner_id
        _agent.is_company_managed = want
        return None

    monkeypatch.setattr(
        builtin_tools_module, "_apply_agent_company_managed_toggle", _toggle
    )
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_agent", agent_id=str(agent.id), is_company_managed="true"
    )

    assert not result.is_error
    assert "company-managed" in result.content
    assert seen == {"want": True, "owner_id": admin.id}
    assert agent.is_company_managed is True
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_company_managed_noop_when_unchanged(monkeypatch):
    """Re-asserting the current value is a no-op: neither the admin gate nor the
    toggle helper is reached, and nothing commits."""
    agent = _builder_agent(id=uuid.uuid4(), name="Helper", is_company_managed=True)
    _stub_writable_agent(monkeypatch, agent=agent)

    async def _toggle_must_not_run(*_a, **_k):
        raise AssertionError("toggle must not run when the value is unchanged")

    monkeypatch.setattr(
        builtin_tools_module,
        "_apply_agent_company_managed_toggle",
        _toggle_must_not_run,
    )
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_agent", agent_id=str(agent.id), is_company_managed="true"
    )

    assert not result.is_error
    assert "No fields to update" in result.content
    assert created[0].commits == 0


def test_workset_is_a_valid_builtin_tool_id():
    """The one-line allow-list change: `workset` is offered, an unknown id is
    not (so the refusal below is a real gate, not a vacuous one)."""
    from app.services.builtin_tools.agent_builder import _VALID_BUILTIN_TOOL_IDS

    assert "workset" in _VALID_BUILTIN_TOOL_IDS
    assert "not_a_builtin" not in _VALID_BUILTIN_TOOL_IDS


@pytest.mark.asyncio
async def test_update_agent_accepts_workset_and_refuses_unknown_builtin(monkeypatch):
    """Drive the builder end to end: `builtin_tool_ids="workset"` is accepted
    and stored, while an unknown id is refused before any write. Pins the
    allow-list change on the real validation path, not just the tuple."""
    agent = _builder_agent(
        id=uuid.uuid4(),
        name="Helper",
        is_company_managed=False,
        builtin_tool_ids=[],
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    _admin_caller(monkeypatch, is_admin=False)
    _stub_personal_bindings(monkeypatch)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    bad = await _call(
        executor, "update_agent", agent_id=str(agent.id), builtin_tool_ids="not_a_builtin"
    )
    assert "Invalid builtin_tool_ids: not_a_builtin" in bad.content
    assert agent.builtin_tool_ids == []
    assert all(c.commits == 0 for c in created)

    good = await _call(
        executor, "update_agent", agent_id=str(agent.id), builtin_tool_ids="workset"
    )
    assert not good.is_error
    assert "Invalid builtin_tool_ids" not in good.content
    # ``workset`` is a [DEPRECATED] alias of ``coordination``: stored
    # as its group, scoped to the functions it granted.
    assert agent.builtin_tool_ids == ["coordination"]
    assert agent.builtin_allowed_tools == {"coordination": list(alias_scoped_functions("workset"))}
    assert any(c.commits == 1 for c in created)


def test_every_registered_group_and_former_id_is_a_valid_builtin_tool_id():
    """The accepted ids are the registry, not a hand-kept tuple (#8827): every
    group the settings picker sets and every [DEPRECATED] former id."""
    from app.services.builtin_tool_aliases import BUILTIN_GROUP_ALIASES, RENAMED_BUILTIN_GROUPS
    from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS
    from app.services.builtin_tools.agent_builder import (
        _VALID_BUILTIN_TOOL_IDS,
        _advertised_builtin_tool_ids,
    )

    groups = {group.id for group in BUILTIN_TOOL_GROUPS}
    assert set(_VALID_BUILTIN_TOOL_IDS) == groups | set(BUILTIN_GROUP_ALIASES) | set(RENAMED_BUILTIN_GROUPS)
    assert {"files", "web", "cti", "design", "coordination"} <= set(_VALID_BUILTIN_TOOL_IDS)
    # Advertised: groups only, never a former id.
    assert set(_advertised_builtin_tool_ids()) <= groups
    assert {"files", "web", "cti", "design", "coordination"} <= set(_advertised_builtin_tool_ids())


def _every_group() -> list[str]:
    from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS

    return [group.id for group in BUILTIN_TOOL_GROUPS]


def _builder_for(monkeypatch, agent):
    _stub_writable_agent(monkeypatch, agent=agent)
    _admin_caller(monkeypatch, is_admin=False)
    _stub_personal_bindings(monkeypatch)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    return executor, created


@pytest.mark.asyncio
async def test_update_agent_sets_the_consolidated_groups(monkeypatch):
    """The groups the API refused (#8827) are accepted and stored as given."""
    agent = _builder_agent(id=uuid.uuid4(), name="Helper", is_company_managed=False, builtin_tool_ids=[])
    executor, _created = _builder_for(monkeypatch, agent)

    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        builtin_tool_ids="files,web,cti,design,coordination,observability",
    )

    assert not result.is_error, result.content
    assert agent.builtin_tool_ids == ["files", "web", "cti", "design", "coordination", "observability"]
    assert "added files, web, cti, design, coordination, observability" in result.content


@pytest.mark.asyncio
async def test_update_agent_add_mode_keeps_every_group(monkeypatch):
    """The regression: adding one group in 'add' mode keeps all the others."""
    every = [g for g in _every_group() if g != "observability"]
    agent = _builder_agent(id=uuid.uuid4(), name="Helper", is_company_managed=False, builtin_tool_ids=list(every))
    executor, _created = _builder_for(monkeypatch, agent)

    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        builtin_tool_ids="observability",
        binding_mode="add",
    )

    assert not result.is_error, result.content
    assert agent.builtin_tool_ids == [*every, "observability"]
    assert "added observability" in result.content
    assert "removed" not in result.content


@pytest.mark.asyncio
async def test_update_agent_add_mode_does_not_duplicate_a_group_through_its_former_id(monkeypatch):
    agent = _builder_agent(id=uuid.uuid4(), name="Helper", is_company_managed=False, builtin_tool_ids=["coordination"])
    executor, _created = _builder_for(monkeypatch, agent)

    result = await _call(
        executor, "update_agent", agent_id=str(agent.id), builtin_tool_ids="work_items", binding_mode="add"
    )

    assert not result.is_error, result.content
    assert agent.builtin_tool_ids == ["coordination"]
    assert "Built-in tools unchanged." in result.content


@pytest.mark.asyncio
async def test_update_agent_replace_mode_names_the_groups_removed(monkeypatch):
    every = _every_group()
    agent = _builder_agent(id=uuid.uuid4(), name="Helper", is_company_managed=False, builtin_tool_ids=list(every))
    executor, _created = _builder_for(monkeypatch, agent)

    result = await _call(executor, "update_agent", agent_id=str(agent.id), builtin_tool_ids="observability")

    assert not result.is_error, result.content
    assert agent.builtin_tool_ids == ["observability"]
    removed = [g for g in every if g != "observability"]
    assert f"removed {', '.join(removed)}" in result.content


@pytest.mark.asyncio
async def test_update_agent_remove_mode_drops_a_group_and_refuses_clear(monkeypatch):
    agent = _builder_agent(
        id=uuid.uuid4(), name="Helper", is_company_managed=False, builtin_tool_ids=["files", "web", "cti"]
    )
    executor, _created = _builder_for(monkeypatch, agent)

    refused = await _call(
        executor, "update_agent", agent_id=str(agent.id), builtin_tool_ids="none", binding_mode="remove"
    )
    assert "only valid with binding_mode='replace'" in refused.content
    assert agent.builtin_tool_ids == ["files", "web", "cti"]

    # A former id removes its group.
    result = await _call(
        executor, "update_agent", agent_id=str(agent.id), builtin_tool_ids="browser", binding_mode="remove"
    )
    assert not result.is_error, result.content
    assert agent.builtin_tool_ids == ["files", "cti"]
    assert "removed web" in result.content


@pytest.mark.asyncio
async def test_update_agent_company_managed_rejects_personal_binding(monkeypatch):
    """Editing the bindings of an already-company-managed agent re-validates the
    final binding state against the REST contract: a personal (non-company-
    managed) binding is rejected with the REST detail and rolled back, never
    committed. The toggle helper only guards the private -> company-managed flip,
    so this end-of-update check is what mirrors ``PUT /agents/{id}`` for binding
    edits on an already-shared agent."""
    from fastapi import HTTPException

    import app.services.agent_bindings as agent_bindings_module

    # A real Agent model (not a SimpleNamespace) so the update flow's
    # ``prune_orphaned_knowledge_base_permissions`` call — which now runs
    # whenever knowledge_base_ids changes, mirroring the REST endpoint — is
    # exercised exactly as in production.
    agent = Agent(
        id=uuid.uuid4(),
        name="Shared Helper",
        is_company_managed=True,
        knowledge_base_ids=[],
        knowledge_base_permissions=None,
        mcp_server_ids=[],
        integration_bindings={},
        handover_agent_ids=[],
    )
    _stub_writable_agent(monkeypatch, agent=agent)

    seen: dict = {}

    async def _validate(_db, **kwargs):
        seen.update(kwargs)
        raise HTTPException(
            status_code=400,
            detail=(
                "Company-managed agents can only use company-managed knowledge "
                'bases. "Personal KB" is not company-managed.'
            ),
        )

    monkeypatch.setattr(agent_bindings_module, "validate_company_managed_bindings", _validate)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    personal_kb = str(uuid.uuid4())
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        knowledge_base_ids=personal_kb,
    )

    assert not result.is_error
    assert "not company-managed" in result.content
    # The agent's FINAL binding state was validated (the just-set KB id)...
    assert seen["knowledge_base_ids"] == [personal_kb]
    # ...and the rejected change was rolled back, never committed.
    assert created[0].commits == 0
    assert created[0].rollbacks == 1


@pytest.mark.asyncio
async def test_create_agent_company_managed_requires_admin(monkeypatch):
    """Creating a company-managed agent via the builder is admin-only (parity
    with the other create_* tools and the REST create endpoint)."""
    _admin_caller(monkeypatch, is_admin=False)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "create_agent", name="Shared Bot", is_company_managed=True
    )

    assert not result.is_error
    assert "Only admins can create company-managed agents" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_apply_agent_company_managed_toggle_on_and_off(monkeypatch):
    """Exercise the real toggle helper's orchestration (the update_agent tests
    stub it). ON clears group sharing + strips personal bindings + validates;
    OFF backfills a legacy ownerless agent + clears the public-catalog flag +
    reverts. The heavy REST side-effect helpers are spied."""
    import app.services.agent_bindings as agent_bindings_module

    calls = {"strip": 0, "validate": 0, "revert": 0}

    async def _strip(_db, _agent):
        calls["strip"] += 1

    async def _validate(_db, **_k):
        calls["validate"] += 1

    async def _revert(_db, _agent_id, _owner):
        calls["revert"] += 1

    monkeypatch.setattr(agent_bindings_module, "strip_personal_bindings", _strip)
    monkeypatch.setattr(agent_bindings_module, "validate_company_managed_bindings", _validate)
    monkeypatch.setattr(agent_bindings_module, "revert_company_managed_to_private", _revert)

    import app.services.workset as workset_module

    retiered: list[tuple[uuid.UUID, bool]] = []

    async def _retier(_db, agent_id, is_company_managed):
        retiered.append((agent_id, is_company_managed))
        return 0

    monkeypatch.setattr(workset_module, "retier_agent_worksets", _retier)

    # private -> company-managed
    agent_on = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=False,
        shared_group_ids=[str(uuid.uuid4())],
        shared_group_permissions={"x": "config"},
        mcp_server_ids=[],
        knowledge_base_ids=[],
        integration_bindings={},
        handover_agent_ids=[],
    )
    err_on = await builtin_tools_module._apply_agent_company_managed_toggle(
        _FakeSession([]), agent_on, True, uuid.uuid4()
    )
    assert err_on is None
    assert agent_on.is_company_managed is True
    assert agent_on.shared_group_ids is None
    assert agent_on.shared_group_permissions is None
    assert calls["strip"] == 1 and calls["validate"] == 1 and calls["revert"] == 0
    # The agent's worksets follow it to the new tier.
    assert retiered == [(agent_on.id, True)]

    # company-managed -> private, legacy ownerless agent gets the fallback owner
    fallback = uuid.uuid4()
    agent_off = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        is_company_managed=True,
        is_public_catalog=True,
    )
    err_off = await builtin_tools_module._apply_agent_company_managed_toggle(
        _FakeSession([]), agent_off, False, fallback
    )
    assert err_off is None
    assert agent_off.is_company_managed is False
    assert agent_off.user_id == fallback  # backfilled for the legacy ownerless agent
    assert agent_off.is_public_catalog is False
    assert calls["revert"] == 1
    assert retiered == [(agent_on.id, True), (agent_off.id, False)]


# ---------------------------------------------------------------------------
# get_agent — surface attached prompts, skills, and handover routing (by name).
# Previously omitted entirely, so the agent could not read these back.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_agent_surfaces_prompts_skills_and_handovers(monkeypatch):
    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    prompt_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    ho_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Compliance Orchestrator",
        slug="compliance-orchestrator",
        description="desc",
        persona="You orchestrate compliance.",
        enabled=True,
        tags=[],
        knowledge_base_ids=[],
        mcp_server_ids=[],
        custom_tool_ids=[],
        integration_bindings={},
        is_company_managed=False,
        prompt_ids=[str(prompt_id)],
        skill_ids=[str(skill_id)],
        handover_agent_ids=[str(ho_id)],
        handover_types={str(ho_id): "consult"},
        user_id=None,
    )
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[
                agent,  # agent SELECT
                0,  # assignment count
                [(prompt_id, "House Style")],  # prompt id -> name
                [(skill_id, "Triage Skill")],  # skill id -> name
                [(ho_id, "Control Monitor")],  # handover id -> name
            ],
        ),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_agent", agent_id=str(agent.id))

    assert not result.is_error
    # Prompts + skills surfaced by name (and prompt id), not just a count.
    assert "House Style" in result.content
    assert str(prompt_id) in result.content
    assert "Triage Skill" in result.content
    # Handover routing surfaced with the target name AND the handover type.
    assert "Control Monitor" in result.content
    assert "[consult]" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        # An empty map is the legacy mode: every target gets every handover
        # tool, so none of them is reported as delegate.
        (False, ("[all types]", "[all types]")),
        # A map that holds a type: an untyped target runs as delegate.
        (True, ("[consult]", "[delegate]")),
    ],
)
async def test_get_agent_reports_the_handover_type_each_target_runs(monkeypatch, typed, expected):
    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    billing, research = uuid.uuid4(), uuid.uuid4()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Front Desk",
        slug="front-desk",
        description="desc",
        persona="You route requests.",
        enabled=True,
        tags=[],
        knowledge_base_ids=[],
        mcp_server_ids=[],
        custom_tool_ids=[],
        integration_bindings={},
        is_company_managed=False,
        prompt_ids=[],
        skill_ids=[],
        handover_agent_ids=[str(billing), str(research)],
        handover_types={str(billing): "consult"} if typed else {},
        user_id=None,
    )
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            [],
            execute_results=[
                agent,  # agent SELECT
                0,  # assignment count
                [(billing, "Billing"), (research, "Research")],  # handover id -> name
            ],
        ),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_agent", agent_id=str(agent.id))

    assert not result.is_error
    assert f"Billing (`{billing}`) {expected[0]}" in result.content
    assert f"Research (`{research}`) {expected[1]}" in result.content
    if not typed:
        assert "[delegate]" not in result.content


@pytest.mark.asyncio
async def test_get_skill_returns_full_content(monkeypatch):
    """get_skill must return the FULL content, not a 1000-char preview."""

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    long_content = "# Skill\n" + ("Z" * 5000)
    skill = SimpleNamespace(
        id=uuid.uuid4(),
        name="Triage",
        url=None,
        description="desc",
        compatibility="claude",
        enabled=True,
        is_company_managed=False,
        content=long_content,
        user_id=None,
    )
    created: list = []
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[skill])
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_skill", skill_id=str(skill.id))

    assert not result.is_error
    assert "Content (full):" in result.content
    assert long_content in result.content
    assert "first 1000 chars" not in result.content


# ---------------------------------------------------------------------------
# update_agent / create_agent — per-KB write access (knowledge_base_permissions).
# The builder previously bound KBs but exposed NO way to grant the store_knowledge
# (write) capability, so an agent built via chat could only ever read its KBs.
# These mirror the REST ``PUT /agents/{id}`` knowledge_base_permissions contract.
# A real Agent model is used so the model's
# ``prune_orphaned_knowledge_base_permissions`` runs exactly as in production.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_agent_grants_kb_write_permission(monkeypatch):
    """A '<kb_id>:write' pair on a bound KB grants store_knowledge and commits."""
    kb_id = str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="KB Agent",
        knowledge_base_ids=[kb_id],
        knowledge_base_permissions=None,
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        knowledge_base_permissions=f"{kb_id}:write",
    )

    assert not result.is_error
    assert "knowledge_base_permissions" in result.content
    assert agent.knowledge_base_permissions == {kb_id: "write"}
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_kb_write_grant_pruned_when_unbound(monkeypatch):
    """A write grant for a KB that is NOT bound is pruned to read-only (None),
    mirroring the REST endpoint's prune_orphaned_knowledge_base_permissions."""
    kb_bound = str(uuid.uuid4())
    kb_unbound = str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="KB Agent",
        knowledge_base_ids=[kb_bound],
        knowledge_base_permissions=None,
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        knowledge_base_permissions=f"{kb_unbound}:write",
    )

    assert not result.is_error
    assert agent.knowledge_base_permissions is None
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_kb_permissions_clear_sentinel(monkeypatch):
    """'none'/'clear' resets every write grant to read-only."""
    kb_id = str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="KB Agent",
        knowledge_base_ids=[kb_id],
        knowledge_base_permissions={kb_id: "write"},
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        knowledge_base_permissions="none",
    )

    assert not result.is_error
    assert agent.knowledge_base_permissions is None
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_kb_permissions_rejects_malformed(monkeypatch):
    """A non-UUID / bad-shape pair is rejected before any commit."""
    agent = Agent(
        id=uuid.uuid4(),
        name="KB Agent",
        knowledge_base_ids=[str(uuid.uuid4())],
        knowledge_base_permissions=None,
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        knowledge_base_permissions="not-a-uuid:write",
    )

    assert not result.is_error
    assert "Invalid knowledge_base_permissions" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_update_agent_refuses_openapi_per_user_binding(monkeypatch):
    """Every OpenAPI integration is a different API, so it never resolves per
    user: the builder refuses it like the REST validator, before any commit."""
    agent = Agent(id=uuid.uuid4(), name="API Agent", integration_template_bindings=["opencti"])
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        integration_template_bindings="opencti, openapi",
    )

    assert "cannot be resolved per user" in result.content
    assert agent.integration_template_bindings == ["opencti"]
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_update_agent_kb_permissions_separator_only_does_not_clear(monkeypatch):
    """A non-blank value with no parseable pair (e.g. just ',') must be rejected,
    NOT treated as a clear — only 'none'/'clear' may wipe write grants, so a
    stray separator in tool output can never silently drop write access."""
    kb_id = str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="KB Agent",
        knowledge_base_ids=[kb_id],
        knowledge_base_permissions={kb_id: "write"},
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        knowledge_base_permissions=", ,",
    )

    assert not result.is_error
    assert "no valid" in result.content
    # The existing write grant survives and nothing is committed.
    assert agent.knowledge_base_permissions == {kb_id: "write"}
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_create_agent_grants_kb_write_permission(monkeypatch):
    """create_agent accepts knowledge_base_permissions and the new agent carries
    the write grant (parity with update_agent + the REST create endpoint)."""
    kb_id = str(uuid.uuid4())
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    # Binding a KB on a personal agent now runs the REST personal-binding
    # visibility gate; this test exercises the write-grant plumbing, not the
    # gate, so stub it to a no-op (the dedicated tests below cover the gate).
    _stub_personal_bindings(monkeypatch)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_agent",
        name="Writer Bot",
        knowledge_base_ids=kb_id,
        knowledge_base_permissions=f"{kb_id}:write",
        create_default_assignments=False,
    )

    assert not result.is_error
    assert "created successfully" in result.content
    added_agents = [obj for obj in created[0].added if isinstance(obj, Agent)]
    assert len(added_agents) == 1
    assert added_agents[0].knowledge_base_permissions == {kb_id: "write"}


@pytest.mark.asyncio
async def test_create_agent_sets_max_iterations(monkeypatch, version_history):
    """create_agent persists a per-agent max_iterations override; omitting it
    leaves NULL (= platform default)."""
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_agent",
        name="Capped Bot",
        max_iterations=40,
        create_default_assignments=False,
    )

    assert not result.is_error
    added_agents = [obj for obj in created[0].added if isinstance(obj, Agent)]
    assert len(added_agents) == 1
    assert added_agents[0].max_iterations == 40
    # The new agent's history is seeded once; nothing went through the update path.
    (seed,) = version_history.seeds
    assert seed["agent"] is added_agents[0]
    assert version_history.records == []


@pytest.mark.asyncio
async def test_create_agent_defaults_max_iterations_to_inherit(monkeypatch):
    """Omitting max_iterations on create leaves NULL so the agent inherits the
    platform default (it is not forced to a number)."""
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_agent",
        name="Default Bot",
        create_default_assignments=False,
    )

    assert not result.is_error
    added_agents = [obj for obj in created[0].added if isinstance(obj, Agent)]
    assert len(added_agents) == 1
    assert added_agents[0].max_iterations is None


@pytest.mark.asyncio
async def test_create_agent_rejects_out_of_range_max_iterations(monkeypatch):
    """An out-of-range max_iterations is rejected (no agent persisted)."""
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_agent",
        name="Runaway Bot",
        max_iterations=0,
        create_default_assignments=False,
    )

    assert "max_iterations" in result.content
    assert not any(isinstance(o, Agent) for c in created for o in c.added)


@pytest.mark.asyncio
async def test_create_agent_template_assignments_carry_budget_fields(monkeypatch):
    """Template-born assignments created through the agent-builder tool must
    honour per-assignment ``timeout_seconds`` and ``max_iterations`` from the
    template's ``default_assignments`` (parity with the REST create path,
    Dropping them silently reverts the assignment to the 300 s /
    NULL defaults regardless of what the template budgeted)."""
    from app.models.assignment import Assignment

    template = SimpleNamespace(
        name="Mail Desk",
        default_persona="You are Mail Desk.",
        default_skills=[],
        default_assignments=[
            {
                "name": "Learn writing style from sent emails",
                "trigger_type": "schedule",
                "trigger_config": {"cron": "0 2 * * *"},
                "prompt": "Analyze sent emails.",
                "timeout_seconds": 900,
                "max_iterations": 5000,  # over the ceiling — must be clamped
            },
            {
                "name": "Plain assignment",
                "trigger_type": "manual",
                "prompt": "Do the thing.",
            },
            {
                # Free-form admin-saved JSON: malformed values must fall back
                # to the defaults instead of crashing agent creation.
                "name": "Malformed assignment",
                "trigger_type": "manual",
                "prompt": "Do the other thing.",
                "timeout_seconds": None,
                "max_iterations": "twenty",
            },
        ],
    )
    created: list = []
    # execute order: slug-uniqueness probe (no collision), template lookup.
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[None, template]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_agent",
        name="Mail Desk Clone",
        template_slug="email-triage",
    )

    assert not result.is_error
    assert "created successfully" in result.content
    added = [o for c in created for o in c.added if isinstance(o, Assignment)]
    assert len(added) == 3
    by_name = {a.name: a for a in added}
    style = by_name["Learn writing style from sent emails"]
    assert style.timeout_seconds == 900
    assert style.max_iterations == 1000  # clamped to MAX_ITERATIONS_CEILING
    plain = by_name["Plain assignment"]
    assert plain.timeout_seconds == 300
    assert plain.max_iterations is None  # inherit (agent → platform default)
    malformed = by_name["Malformed assignment"]
    assert malformed.timeout_seconds == 300  # explicit null → default
    assert malformed.max_iterations is None  # non-int → inherit


# ---------------------------------------------------------------------------
# update_knowledge_base / create_knowledge_base — company-managed visibility.
# Flipping platform-wide visibility is admin-only and routes through the shared
# REST toggle helper; the builder previously exposed no way to do it at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_knowledge_base_company_managed_requires_admin(monkeypatch):
    """A non-admin who can edit a private KB still cannot publish it company-wide."""
    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="Docs",
        description=None,
        tags=[],
        is_builtin=False,
        is_company_managed=False,
    )
    _stub_manageable_gate(monkeypatch, entity=kb)
    _admin_caller(monkeypatch, is_admin=False)

    async def _toggle_must_not_run(*_a, **_k):
        raise AssertionError("toggle must not run for a non-admin")

    monkeypatch.setattr(
        api_helpers, "handle_company_managed_toggle", _toggle_must_not_run
    )
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_knowledge_base",
        knowledge_base_id=str(kb.id),
        is_company_managed="true",
    )

    assert not result.is_error
    assert "Only admins" in result.content
    assert kb.is_company_managed is False
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_update_knowledge_base_company_managed_admin_toggles(monkeypatch):
    """An admin flips a KB company-managed via the shared toggle helper, which is
    invoked with the resolved want-state + the agent list field, then commits."""
    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="Docs",
        description=None,
        tags=[],
        is_builtin=False,
        is_company_managed=False,
    )
    _stub_manageable_gate(monkeypatch, entity=kb)
    _admin_caller(monkeypatch, is_admin=True)

    seen: dict = {}

    async def _toggle(_db, _user, _entity, _new_value, *, agent_field):
        seen["new_value"] = _new_value
        seen["agent_field"] = agent_field
        _entity.is_company_managed = _new_value

    monkeypatch.setattr(api_helpers, "handle_company_managed_toggle", _toggle)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_knowledge_base",
        knowledge_base_id=str(kb.id),
        is_company_managed="true",
    )

    assert not result.is_error
    assert "company-managed" in result.content
    assert seen == {"new_value": True, "agent_field": "knowledge_base_ids"}
    assert kb.is_company_managed is True
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_knowledge_base_company_managed_noop_when_unchanged(monkeypatch):
    """Re-asserting the current value is a no-op: the toggle helper is never
    reached and nothing commits."""
    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="Docs",
        description=None,
        tags=[],
        is_builtin=False,
        is_company_managed=True,
    )
    _stub_manageable_gate(monkeypatch, entity=kb)

    async def _toggle_must_not_run(*_a, **_k):
        raise AssertionError("toggle must not run when the value is unchanged")

    monkeypatch.setattr(
        api_helpers, "handle_company_managed_toggle", _toggle_must_not_run
    )
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_knowledge_base",
        knowledge_base_id=str(kb.id),
        is_company_managed="true",
    )

    assert not result.is_error
    assert "No fields to update" in result.content
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_create_knowledge_base_company_managed_requires_admin(monkeypatch):
    """Creating a company-managed KB is admin-only; a non-admin is refused before
    any row is added or committed."""
    _admin_caller(monkeypatch, is_admin=False)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "create_knowledge_base", name="Shared KB", is_company_managed=True
    )

    assert not result.is_error
    assert "Only admins" in result.content
    assert all(c.commits == 0 for c in created)
    assert all(not c.added for c in created)


@pytest.mark.asyncio
async def test_create_knowledge_base_company_managed_admin_creates(monkeypatch):
    """An admin creates a company-managed KB; the persisted row carries the flag."""
    _admin_caller(monkeypatch, is_admin=True)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "create_knowledge_base", name="Shared KB", is_company_managed=True
    )

    assert not result.is_error
    assert "created successfully" in result.content
    assert created[0].commits == 1
    added_kbs = [obj for obj in created[0].added if isinstance(obj, KnowledgeBase)]
    assert len(added_kbs) == 1
    assert added_kbs[0].is_company_managed is True


# ---------------------------------------------------------------------------
# get_agent — surface the EXACT bindings (provider:id + names) and every other
# configurable option, so the agent can read-modify-write any field faithfully
# instead of reconstructing bindings from the persona (#bug: only counts shown).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_agent_surfaces_integration_bindings_and_resource_names(monkeypatch):
    """get_agent must list integration bindings as copy-pasteable 'provider:id'
    pairs WITH the integration name, and resolve KB/MCP/tool names — not just a
    count the agent can't act on."""

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    kb_id = uuid.uuid4()
    mcp_id = uuid.uuid4()
    ct_id = uuid.uuid4()
    gh_id = uuid.uuid4()
    slack_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Security Master Control",
        slug="security-master-control",
        description="desc",
        persona="You orchestrate security.",
        enabled=True,
        tags=[],
        knowledge_base_ids=[str(kb_id)],
        knowledge_base_permissions={str(kb_id): "write"},
        mcp_server_ids=[str(mcp_id)],
        custom_tool_ids=[str(ct_id)],
        integration_bindings={"github": [str(gh_id)], "slack": [str(slack_id)]},
        prompt_ids=[],
        skill_ids=[],
        handover_agent_ids=[],
        handover_types={},
        is_company_managed=False,
        user_id=None,
    )
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[
                agent,  # agent SELECT
                0,  # assignment count
                [(kb_id, "Compliance KB")],  # KB id -> name
                [(mcp_id, "GitHub MCP")],  # MCP id -> name
                [(ct_id, "Risk Scorer")],  # custom tool id -> name
                [(gh_id, "GitHub Prod"), (slack_id, "Eng Slack")],  # integ -> name
            ],
        ),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_agent", agent_id=str(agent.id))

    assert not result.is_error
    # Exact integration bindings (provider:id) WITH names — feedable straight
    # back into update_agent.
    assert f"github:{gh_id}" in result.content
    assert f"slack:{slack_id}" in result.content
    assert "GitHub Prod" in result.content
    assert "Eng Slack" in result.content
    # Bound resources resolved by name, not just counts.
    assert "Compliance KB" in result.content
    assert "[writable]" in result.content  # the KB write grant is surfaced
    assert "GitHub MCP" in result.content
    assert "Risk Scorer" in result.content


@pytest.mark.asyncio
async def test_get_agent_surfaces_all_config_options(monkeypatch):
    """get_agent must surface EVERY configurable option (model, behavior,
    memory, channels, sharing, ...) so the agent can read them back."""

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Full Config Agent",
        slug="full-config-agent",
        description="desc",
        icon="ShieldCheck",
        persona="Persona text.",
        guardrails="Never reveal secrets.",
        enabled=True,
        disable_chat=True,
        tags=["ops"],
        knowledge_base_ids=[],
        knowledge_base_permissions=None,
        mcp_server_ids=[],
        custom_tool_ids=[],
        integration_bindings={},
        integration_template_bindings=["opencti"],
        builtin_tool_ids=["code_interpreter"],
        tool_deny_list=["delete_repo"],
        prompt_ids=[],
        skill_ids=[],
        handover_agent_ids=[],
        handover_types={},
        handover_conditions={},
        output_format="json",
        output_schema={"type": "object"},
        skip_llm=False,
        llm_model="claude-sonnet",
        llm_provider="anthropic",
        temperature=0.7,
        reasoning_effort="high",
        max_iterations=25,
        memory_short_term_enabled=False,
        memory_medium_term_enabled=True,
        memory_persistent_enabled=True,
        memory_user_enabled=False,
        human_in_the_loop=True,
        human_in_the_loop_prompt="Ask before sending.",
        available_channel_types=["web", "slack"],
        channel_config=None,
        web_enabled=True,
        web_token="tok",
        web_suggested_prompts=["Hi"],
        is_company_managed=True,
        is_public_catalog=True,
        shared_group_ids=None,
        shared_group_permissions=None,
        config={"k": "v"},
        user_id=None,
    )
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[agent, 0]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_agent", agent_id=str(agent.id))

    assert not result.is_error
    c = result.content
    assert "Icon: ShieldCheck" in c
    assert "Output format: json" in c
    assert "Temperature: 0.7" in c
    assert "Reasoning effort: high" in c
    assert "Max iterations: 25" in c
    assert "Short-term: No" in c
    assert "User memory: No" in c
    assert "Web widget enabled: Yes" in c
    assert "Public catalog: Yes" in c
    assert "Built-in tools: code_interpreter" in c
    assert "opencti" in c  # integration template bindings
    assert "Guardrails (full):" in c and "Never reveal secrets." in c
    assert "Human-in-the-loop prompt (full):" in c


def _web_token_agent():
    """A minimal web-enabled, company-managed agent double carrying a token."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Web Agent",
        slug="web-agent",
        description="desc",
        persona="p",
        enabled=True,
        tags=[],
        knowledge_base_ids=[],
        knowledge_base_permissions=None,
        mcp_server_ids=[],
        custom_tool_ids=[],
        integration_bindings={},
        prompt_ids=[],
        skill_ids=[],
        handover_agent_ids=[],
        handover_types={},
        handover_conditions={},
        web_enabled=True,
        web_token="s3cr3t-web-token",
        is_company_managed=True,
        user_id=None,
    )


@pytest.mark.asyncio
async def test_get_agent_web_token_visible_to_manage_tier(monkeypatch):
    """web_token is a public-chat credential, so get_agent surfaces the RAW
    token to a caller who can MANAGE the agent (here: an admin) — the
    manage-tier read-modify-write loop needs it to build/verify the embed URL."""

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)
    _admin_caller(monkeypatch, is_admin=True)

    agent = _web_token_agent()
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory([], execute_results=[agent, 0]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_agent", agent_id=str(agent.id))

    assert not result.is_error
    assert "web_token: `s3cr3t-web-token`" in result.content
    assert "(token set)" not in result.content


@pytest.mark.asyncio
async def test_get_agent_web_token_redacted_for_non_manage_caller(monkeypatch):
    """A caller who can SEE but not MANAGE the agent (here: a non-admin viewing
    a company-managed agent) gets the redacted "(token set)" indicator, never
    the raw web_token — the builder read tool has wider reach than the
    credential's manage tier, so it must not become a disclosure bypass."""

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)
    _admin_caller(monkeypatch, is_admin=False)

    agent = _web_token_agent()
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory([], execute_results=[agent, 0]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_agent", agent_id=str(agent.id))

    assert not result.is_error
    assert "(token set)" in result.content
    assert "s3cr3t-web-token" not in result.content


@pytest.mark.asyncio
async def test_get_agent_renders_empty_json_objects_distinctly(monkeypatch):
    """An empty output_schema / config ({}) must render as `{}`, not collapse to
    "None" - so a read-modify-write loop can tell an empty object from null."""

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Empty JSON Agent",
        slug="empty-json-agent",
        description="desc",
        persona="Persona text.",
        enabled=True,
        tags=[],
        knowledge_base_ids=[],
        knowledge_base_permissions=None,
        mcp_server_ids=[],
        custom_tool_ids=[],
        integration_bindings={},
        prompt_ids=[],
        skill_ids=[],
        handover_agent_ids=[],
        handover_types={},
        handover_conditions={},
        output_schema={},
        config={},
        is_company_managed=False,
        user_id=None,
    )
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[agent, 0]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_agent", agent_id=str(agent.id))

    assert not result.is_error
    c = result.content
    assert "Output schema: {}" in c
    assert "Config (free-form): {}" in c


# ---------------------------------------------------------------------------
# update_agent — binding_mode add/remove (the reported pain): change a single
# binding without re-sending the whole set.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_agent_add_integration_binding_merges(monkeypatch):
    """binding_mode='add' merges new pairs into the CURRENT bindings (deduped)
    instead of replacing them — so adding one integration can't drop the rest."""
    existing_gh = str(uuid.uuid4())
    new_google = str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="Threat Intel",
        integration_bindings={"github": [existing_gh]},
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    # Binding visibility is validated separately (see the dedicated tests);
    # this test targets the add/merge semantics, so stub it to a no-op.
    _stub_personal_bindings(monkeypatch)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        # also re-add the existing pair to prove dedup
        integration_bindings=f"github:{existing_gh},google_search:{new_google}",
        binding_mode="add",
    )

    assert not result.is_error
    assert agent.integration_bindings == {
        "github": [existing_gh],
        "google_search": [new_google],
    }
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_remove_integration_binding_subtracts(monkeypatch):
    """binding_mode='remove' subtracts the given pairs and drops a provider key
    whose id list becomes empty."""
    gh1, gh2, slack1 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="Threat Intel",
        integration_bindings={"github": [gh1, gh2], "slack": [slack1]},
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        integration_bindings=f"github:{gh1},slack:{slack1}",
        binding_mode="remove",
    )

    assert not result.is_error
    assert agent.integration_bindings == {"github": [gh2]}
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_add_mcp_server_merges_and_dedupes(monkeypatch):
    """binding_mode='add' on an id-list field merges + dedupes."""
    m1, m2 = str(uuid.uuid4()), str(uuid.uuid4())
    agent = Agent(id=uuid.uuid4(), name="Ops", mcp_server_ids=[m1])
    _stub_writable_agent(monkeypatch, agent=agent)
    # Binding visibility is validated separately (see the dedicated tests);
    # this test targets the add/dedupe semantics, so stub it to a no-op.
    _stub_personal_bindings(monkeypatch)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        mcp_server_ids=f"{m1},{m2}",
        binding_mode="add",
    )

    assert not result.is_error
    assert agent.mcp_server_ids == [m1, m2]
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_remove_kb_subtracts_and_prunes_write_grant(monkeypatch):
    """binding_mode='remove' on knowledge_base_ids subtracts the id and the
    orphaned write grant is pruned (parity with REST)."""
    kb1, kb2 = str(uuid.uuid4()), str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="KB Agent",
        knowledge_base_ids=[kb1, kb2],
        knowledge_base_permissions={kb1: "write"},
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        knowledge_base_ids=kb1,
        binding_mode="remove",
    )

    assert not result.is_error
    assert agent.knowledge_base_ids == [kb2]
    assert kb1 not in (agent.knowledge_base_permissions or {})
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_rejects_invalid_binding_mode(monkeypatch):
    """An unknown binding_mode is rejected before any mutation/commit."""
    agent = Agent(id=uuid.uuid4(), name="Ops")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        name="Renamed",
        binding_mode="merge",
    )

    assert not result.is_error  # soft error returned as content
    assert "Invalid binding_mode" in result.content
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_clear_sentinel_rejected_in_add_mode(monkeypatch):
    """'none'/'clear' only clears in replace mode; in add/remove it's rejected
    rather than silently wiping the binding."""
    m1 = str(uuid.uuid4())
    agent = Agent(id=uuid.uuid4(), name="Ops", mcp_server_ids=[m1])
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        mcp_server_ids="none",
        binding_mode="add",
    )

    assert not result.is_error
    assert "binding_mode='replace'" in result.content
    assert agent.mcp_server_ids == [m1]  # untouched
    assert created[0].commits == 0


# ---------------------------------------------------------------------------
# update_agent — full option parity with the REST UpdateAgentRequest.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_agent_sets_scalar_and_memory_options(monkeypatch):
    """temperature, reasoning_effort, guardrails, memory toggles, skip_llm and
    disable_chat are all editable (previously unreachable from the builder)."""
    agent = Agent(id=uuid.uuid4(), name="Tuned")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        temperature="0.7",
        reasoning_effort="high",
        guardrails="Never reveal internal data.",
        memory_short_term_enabled="false",
        skip_llm="true",
        disable_chat="true",
    )

    assert not result.is_error
    assert agent.temperature == 0.7
    assert agent.reasoning_effort == "high"
    assert agent.guardrails == "Never reveal internal data."
    assert agent.memory_short_term_enabled is False
    assert agent.skip_llm is True
    assert agent.disable_chat is True
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_temperature_out_of_range_rejected(monkeypatch):
    """temperature outside 0-2 is rejected (no commit)."""
    agent = Agent(id=uuid.uuid4(), name="Tuned")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_agent", agent_id=str(agent.id), temperature="5"
    )

    assert not result.is_error
    assert "temperature must be at most" in result.content
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_sets_builtin_tools_and_validates(monkeypatch):
    """builtin_tool_ids accepts the supported set and rejects unknown tools."""
    agent = Agent(id=uuid.uuid4(), name="Tools")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    ok = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        builtin_tool_ids="code_interpreter, generate_image",
    )
    assert not ok.is_error
    # ``generate_image`` folds into ``files``, scoped to what it granted.
    assert agent.builtin_tool_ids == ["code_interpreter", "files"]
    assert agent.builtin_allowed_tools == {"files": ["generate_image"]}

    bad = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        builtin_tool_ids="rm_rf",
    )
    assert "Invalid builtin_tool_ids" in bad.content


@pytest.mark.asyncio
async def test_update_agent_accepts_all_platform_builtin_tool_ids(monkeypatch):
    """builtin_tool_ids must accept the full platform set, not just
    code_interpreter / generate_image (parity with the runtime + UI)."""
    agent = Agent(id=uuid.uuid4(), name="Tools")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    extended = "web_fetch, browser, generate_file, stix_bundle_validator, agent_builder"
    ok = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        builtin_tool_ids=extended,
    )
    assert not ok.is_error
    # Former ids are stored as their group: ``web_fetch`` and
    # ``browser`` give the whole ``web`` group, the others keep what they granted;
    # ``agent_builder`` is the platform group's builder sub-area.
    assert agent.builtin_tool_ids == ["web", "files", "cti", "platform"]
    assert agent.builtin_allowed_tools == {
        "files": list(alias_scoped_functions("generate_file")),
        "cti": ["stix_bundle_validator"],
        "platform": list(alias_scoped_functions("agent_builder")),
    }


@pytest.mark.asyncio
async def test_update_agent_sets_output_schema_json(monkeypatch):
    """output_schema accepts a JSON Schema string and rejects invalid JSON."""
    agent = Agent(id=uuid.uuid4(), name="Structured")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    ok = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        output_schema='{"type": "object", "properties": {"response": {"type": "string"}}}',
    )
    assert not ok.is_error
    assert agent.output_schema == {
        "type": "object",
        "properties": {"response": {"type": "string"}},
    }

    bad = await _call(
        executor, "update_agent", agent_id=str(agent.id), output_schema="not json"
    )
    assert "must be a valid JSON object" in bad.content


@pytest.mark.asyncio
async def test_update_agent_sets_handover_types_pairs(monkeypatch):
    """handover_types accepts '<agent_id>:<type>' pairs and keeps only targets
    that are in handover_agent_ids (reusing the REST sanitizer)."""
    a1 = str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="Router",
        handover_agent_ids=[a1],
        handover_types={},
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        handover_types=f"{a1}:consult",
    )

    assert not result.is_error
    assert agent.handover_types == {a1: "consult"}
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_sets_llm_provider_and_provider_id(monkeypatch):
    """llm_provider + llm_provider_id are editable (full parity with REST
    UpdateAgentRequest, which exposes both)."""
    pid = str(uuid.uuid4())
    agent = Agent(id=uuid.uuid4(), name="Pinned")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    # The provider config lock (advisory, then rows), then the config
    # the id names.
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(created, execute_results=[None, None, uuid.UUID(pid)]),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        llm_provider="anthropic",
        llm_provider_id=pid,
    )

    assert not result.is_error
    assert agent.llm_provider == "anthropic"
    assert str(agent.llm_provider_id) == pid
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_refuses_a_provider_id_that_names_no_config(monkeypatch):
    """A pin to a config that does not exist (deleted, or hallucinated)
    is refused and nothing is committed."""
    agent = Agent(id=uuid.uuid4(), name="Pinned")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        llm_provider_id=str(uuid.uuid4()),
    )

    assert "no longer exists" in result.content
    assert agent.llm_provider_id is None
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_waiting_past_the_timeout_answers_the_busy_refusal(monkeypatch):
    """A model choice that waited for the provider lock past the statement
    timeout gets the same sentence as a REST save's 503, and nothing is written."""
    from app.services import llm_provider_deletion

    async def _busy(_db, *, shared=False):
        raise llm_provider_deletion.ProviderLockBusyError()

    monkeypatch.setattr(llm_provider_deletion, "lock_provider_configs", _busy)
    agent = Agent(id=uuid.uuid4(), name="Pinned")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "update_agent", agent_id=str(agent.id), llm_model="gpt-4o")

    assert result.content == llm_provider_deletion.PROVIDER_LOCK_BUSY_DETAIL
    assert agent.llm_model is None
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_clear_llm_provider_also_clears_provider_id(monkeypatch):
    """Clearing llm_provider also clears llm_provider_id (mirrors REST: 'when
    llm_provider is cleared, also clear llm_provider_id')."""
    agent = Agent(
        id=uuid.uuid4(),
        name="Pinned",
        llm_provider="openai",
        llm_provider_id=uuid.uuid4(),
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        llm_provider="none",
    )

    assert not result.is_error
    assert agent.llm_provider is None
    assert agent.llm_provider_id is None
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_clearing_the_provider_never_checks_the_id_sent_with_it(monkeypatch):
    """The call clears the provider, which clears the id it also sends,
    so that id is never stored and is not refused for naming no config."""
    agent = Agent(
        id=uuid.uuid4(), name="Pinned", llm_provider="openai", llm_provider_id=uuid.uuid4()
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        llm_provider="none",
        llm_provider_id=str(uuid.uuid4()),
    )

    assert not result.is_error
    assert (agent.llm_provider, agent.llm_provider_id) == (None, None)
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_rejects_invalid_llm_provider_id(monkeypatch):
    """A non-UUID llm_provider_id is rejected before any commit."""
    agent = Agent(id=uuid.uuid4(), name="Pinned")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        llm_provider_id="not-a-uuid",
    )

    assert not result.is_error
    assert "Invalid llm_provider_id" in result.content
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_rejects_non_string_scalar_arg(monkeypatch):
    """A verbatim native non-string for a string-typed field returns a
    friendly, correctable error instead of crashing on ``.strip()`` (tool
    kwargs are forwarded as-is, so an LLM can emit e.g. ``llm_provider_id=123``;
    mirrors the max_iterations isinstance guard)."""
    agent = Agent(id=uuid.uuid4(), name="Pinned")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        llm_provider_id=123,  # native int, not a string
    )

    assert not result.is_error
    assert "llm_provider_id must be a string" in result.content
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_web_token_not_backfilled_on_unrelated_update(monkeypatch):
    """A legacy ``web_enabled=True, web_token=NULL`` row must NOT get a token
    backfilled when web_enabled is not part of THIS update (mirrors REST, which
    gates the backfill on ``body.web_enabled`` truthiness, not the stored
    flag)."""
    agent = Agent(
        id=uuid.uuid4(), name="Legacy Web", web_enabled=True, web_token=None
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        persona="A brand new persona, unrelated to web exposure.",
    )

    assert not result.is_error
    assert agent.web_token is None  # not backfilled
    assert created[0].commits == 1  # the persona change still committed


@pytest.mark.asyncio
async def test_update_agent_web_token_generated_when_web_enabled_set(monkeypatch):
    """Enabling web exposure in THIS call auto-generates the web_token (parity
    with REST when ``body.web_enabled`` is truthy)."""
    agent = Agent(
        id=uuid.uuid4(), name="Newly Web", web_enabled=False, web_token=None
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        web_enabled="true",
    )

    assert not result.is_error
    assert agent.web_enabled is True
    assert agent.web_token  # generated
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_channel_config_accepts_native_dict(monkeypatch):
    """A native dict channel_config (forwarded JSON, not a JSON string) is
    accepted, not crashed on a ``.strip()`` pre-guard — the very shape
    ``_parse_optional_json_arg`` is designed to accept."""
    import app.services.agent_bindings as agent_bindings_module

    async def _ok_validate(*_a, **_k):
        return None

    monkeypatch.setattr(agent_bindings_module, "validate_channel_config", _ok_validate)

    agent = Agent(id=uuid.uuid4(), name="Chan")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        channel_config={"slack": {"receive_enabled": True}},
    )

    assert not result.is_error
    assert agent.channel_config == {"slack": {"receive_enabled": True}}
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_only_checks_the_channel_bots_it_adds(monkeypatch):
    """A bot the call ADDS must be one the caller can see (the fake session
    finds none); a bot the agent already names is kept on a re-save."""
    import app.services.agent_bindings as agent_bindings_module
    from app.api.v1.binding_access import CHANNEL_BOTS_REFUSED_DETAIL

    async def _ok_validate(*_a, **_k):
        return None

    monkeypatch.setattr(agent_bindings_module, "validate_channel_config", _ok_validate)
    stored_bot, new_bot = str(uuid.uuid4()), str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(), name="Chan", channel_config={"teams": {"channel_bot_id": stored_bot}}
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    _admin_caller(monkeypatch, is_admin=False)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    kept = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        channel_config={
            "teams": {"channel_bot_id": stored_bot},
            "slack": {"receive_enabled": True},
        },
    )
    assert not kept.is_error

    refused = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        channel_config={
            "teams": {"channel_bot_id": stored_bot},
            "discord": {"channel_bot_id": new_bot},
        },
    )
    assert CHANNEL_BOTS_REFUSED_DETAIL in refused.content
    assert new_bot not in refused.content
    assert created[-1].rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict-admin-access"])
async def test_update_agent_checks_an_added_bot_with_the_agent_tools_admin_bypass(
    monkeypatch, strict
):
    """An admin's agent adding a bot: its visibility check takes the agent
    tools' admin bypass, which strict admin access withdraws; the
    admin-only ``bypass_whitelist`` check still reads the admin role."""
    import app.services.agent_bindings as agent_bindings_module
    from app.api.v1 import binding_access
    from app.core.config import settings

    seen: list = []

    async def _validate(*_a, **kwargs):
        seen.append(("whitelist", kwargs.get("is_admin")))

    async def _visibility(*_a, **kwargs):
        seen.append(("visibility", kwargs.get("is_admin")))
        return set()

    monkeypatch.setattr(agent_bindings_module, "validate_channel_config", _validate)
    monkeypatch.setattr(binding_access, "refused_channel_bots", _visibility)
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", strict)
    agent = Agent(id=uuid.uuid4(), name="Chan", channel_config=None)
    _stub_writable_agent(monkeypatch, agent=agent)
    _admin_caller(monkeypatch, is_admin=True)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        channel_config={"slack": {"channel_bot_id": str(uuid.uuid4())}},
    )

    assert not result.is_error
    assert seen == [("visibility", not strict), ("whitelist", True)]


@pytest.mark.asyncio
async def test_get_agent_integration_bindings_sorted(monkeypatch):
    """get_agent renders integration bindings in a deterministic (sorted)
    order so read-modify-write diffs stay stable regardless of dict insertion
    order."""

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    gh1 = "11111111-1111-1111-1111-111111111111"
    gh2 = "22222222-2222-2222-2222-222222222222"
    slack1 = "33333333-3333-3333-3333-333333333333"
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Ordered",
        slug="ordered",
        description="desc",
        persona="p",
        enabled=True,
        tags=[],
        knowledge_base_ids=[],
        knowledge_base_permissions=None,
        mcp_server_ids=[],
        custom_tool_ids=[],
        # Insertion order is intentionally NON-sorted (slack before github;
        # gh2 before gh1) so a passing assertion proves the renderer sorts.
        integration_bindings={"slack": [slack1], "github": [gh2, gh1]},
        prompt_ids=[],
        skill_ids=[],
        handover_agent_ids=[],
        handover_types={},
        is_company_managed=False,
        user_id=None,
    )
    created: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        _session_factory(
            created,
            execute_results=[
                agent,  # agent SELECT
                0,  # assignment count
                [],  # integration id -> name lookup (names omitted)
            ],
        ),
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_agent", agent_id=str(agent.id))

    assert not result.is_error
    content = result.content
    # providers sorted alphabetically: github before slack
    assert content.index(f"github:{gh1}") < content.index(f"slack:{slack1}")
    # ids sorted within a provider: gh1 (1...) before gh2 (2...)
    assert content.index(f"github:{gh1}") < content.index(f"github:{gh2}")


@pytest.mark.asyncio
async def test_update_agent_shared_group_permissions_empty_dict_collapses_to_none(
    monkeypatch,
):
    """An empty shared_group_permissions dict collapses to None (parity with
    REST, both through ``normalize_group_grants``); a non-empty map is kept."""
    gid = str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="Shared",
        shared_group_ids=[gid],
        shared_group_permissions={gid: "config"},
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    # The group lookup finds the granted group.
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[[(gid,)]])
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    # Empty dict → None (never persisted as non-canonical {}).
    empty = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        shared_group_permissions="{}",
    )
    assert not empty.is_error
    assert agent.shared_group_permissions is None

    # Non-empty map → stored verbatim.
    full = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        shared_group_permissions=f'{{"{gid}": "config"}}',
    )
    assert not full.is_error
    assert agent.shared_group_ids == [gid]
    assert agent.shared_group_permissions == {gid: "config"}


@pytest.mark.asyncio
async def test_update_agent_group_grants_go_through_the_rest_writer(monkeypatch):
    """``update_agent`` stores group grants as the REST routes do: an
    added group must exist, an unknown tier is refused, and nothing is written."""
    stored, unknown = str(uuid.uuid4()), str(uuid.uuid4())
    agent = Agent(id=uuid.uuid4(), name="Shared", shared_group_ids=[stored])
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[[(stored,)]])
    )
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    added = await _call(
        executor, "update_agent", agent_id=str(agent.id), shared_group_ids=f"{stored},{unknown}"
    )
    assert added.content == (
        f"Cannot share agent **Shared**: Cannot share with unknown group(s): {unknown}"
    )
    tier = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        shared_group_permissions=f'{{"{stored}": "owner"}}',
    )
    assert tier.content.startswith("Cannot share agent **Shared**: Invalid share permission.")
    assert (agent.shared_group_ids, agent.shared_group_permissions) == ([stored], None)
    assert [session.commits for session in created] == [0, 0]


@pytest.mark.asyncio
async def test_update_agent_allowed_tools_validate_value_shapes(monkeypatch):
    """integration_allowed_tools / mcp_allowed_tools mirror the REST Pydantic
    type ``dict[str, list[str] | None]``: a valid list-of-strings is stored
    (null per-key = "all tools" is stripped), but a bare-string value is
    rejected before persisting so runtime tool filtering can't degrade to
    substring matching."""
    agent = Agent(id=uuid.uuid4(), name="Scoped")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    # Valid: list of tool names, with a null per-key value stripped to None.
    ok = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        integration_allowed_tools='{"github": ["create_github_issue"], "slack": null}',
    )
    assert not ok.is_error
    assert agent.integration_allowed_tools == {"github": ["create_github_issue"]}

    # Invalid: a bare string instead of a list is rejected (no commit).
    bad = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        mcp_allowed_tools='{"server-1": "search"}',
    )
    assert not bad.is_error
    assert "must be a JSON array" in bad.content
    assert created[-1].commits == 0

    # Invalid: a list containing a non-string entry is rejected.
    bad_item = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        integration_allowed_tools='{"github": ["ok", 123]}',
    )
    assert not bad_item.is_error
    assert "must be a list of tool-name strings" in bad_item.content


@pytest.mark.asyncio
async def test_update_agent_refuses_an_unknown_openapi_tool_and_keeps_the_scope(monkeypatch):
    """An ``openapi`` list naming no tool of the bound OpenAPI integration used
    to be dropped by the per-integration split: the binding became unscoped
    (every tool). It is now refused and the stored scope is kept."""
    from app.services.openapi_import import service as openapi_service

    pets = str(uuid.uuid4())

    async def _spec(_db, integration_id, **_kwargs):
        if integration_id != pets:
            return None
        return SimpleNamespace(active_tools=[{"name": "get_pets"}, {"name": "create_pet"}])

    monkeypatch.setattr(openapi_service, "get_spec", _spec)
    agent = Agent(id=uuid.uuid4(), name="Scoped", integration_bindings={"openapi": [pets]})
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    ok = await _call(
        executor, "update_agent", agent_id=str(agent.id), integration_allowed_tools='{"openapi": ["get_pets"]}'
    )
    assert not ok.is_error
    assert agent.integration_allowed_tools == {f"openapi@{pets}": ["get_pets"]}

    for listed in ('["get_petz"]', '["get_pets", "get_petz"]'):
        bad = await _call(
            executor,
            "update_agent",
            agent_id=str(agent.id),
            integration_allowed_tools=f'{{"openapi": {listed}}}',
        )
        assert bad.content.startswith("Error: integration_allowed_tools['openapi']: unknown tool(s) get_petz")
        assert created[-1].commits == 0
        assert agent.integration_allowed_tools == {f"openapi@{pets}": ["get_pets"]}

    cleared = await _call(executor, "update_agent", agent_id=str(agent.id), integration_allowed_tools="none")
    assert not cleared.is_error
    assert agent.integration_allowed_tools is None


@pytest.mark.asyncio
async def test_update_agent_element_bindings_validate_value_shapes(monkeypatch):
    """integration_element_bindings mirrors the REST Pydantic type
    ``dict[str, list[dict]]``: a list of objects is stored, but a list whose
    items are not objects is rejected before persisting."""
    agent = Agent(id=uuid.uuid4(), name="Bound")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    ok = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        integration_element_bindings='{"github:repo": [{"id": "1", "name": "repo"}]}',
    )
    assert not ok.is_error
    assert agent.integration_element_bindings == {
        "github:repo": [{"id": "1", "name": "repo"}]
    }

    bad = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        integration_element_bindings='{"github:repo": ["not-an-object"]}',
    )
    assert not bad.is_error
    assert "must be a list of JSON objects" in bad.content
    assert created[-1].commits == 0


# ---------------------------------------------------------------------------
# update_agent — personal-binding visibility parity with REST
# ---------------------------------------------------------------------------
# The REST ``PUT /agents/{id}`` endpoint calls ``_validate_personal_agent_
# bindings`` for non-company-managed agents so a user cannot bind another
# user's private integration/KB/MCP/etc by guessing a UUID (token hijack /
# cross-user data access). update_agent must mirror that gate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_agent_personal_binding_visibility_enforced(monkeypatch):
    """A binding change on a personal agent runs the REST visibility validator;
    an invisible resource is rejected with the REST detail and rolled back,
    never committed. Only the changed binding field is forwarded (mirrors
    REST's ``model_fields_set`` gating) and the caller's non-admin status is
    propagated so the validator does not bypass the check."""
    kb_id = str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="Personal Helper",
        is_company_managed=False,
        knowledge_base_ids=[],
        knowledge_base_permissions=None,
        mcp_server_ids=[],
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    _admin_caller(monkeypatch, is_admin=False)
    seen: dict = {}
    _stub_personal_bindings(
        monkeypatch,
        recorder=seen,
        error="One or more knowledge bases were not found or are not accessible.",
    )
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        knowledge_base_ids=kb_id,
    )

    assert not result.is_error
    assert "not accessible" in result.content
    # Only the changed binding field is validated; untouched fields are None.
    assert seen["knowledge_base_ids"] == [kb_id]
    assert seen["mcp_server_ids"] is None
    assert seen["is_admin"] is False
    # The rejected change was rolled back, never committed.
    assert created[0].commits == 0
    assert created[0].rollbacks == 1


@pytest.mark.asyncio
async def test_update_agent_personal_binding_admin_bypasses_visibility(monkeypatch):
    """An admin caller forwards ``is_admin=True`` so the validator bypasses the
    visibility check (parity with REST, where admins can configure any
    resource on their own agents)."""
    m_id = str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="Personal Helper",
        is_company_managed=False,
        mcp_server_ids=[],
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    _admin_caller(monkeypatch, is_admin=True)
    seen: dict = {}
    _stub_personal_bindings(monkeypatch, recorder=seen)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        mcp_server_ids=m_id,
    )

    assert not result.is_error
    assert seen["is_admin"] is True
    assert seen["mcp_server_ids"] == [m_id]
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_personal_binding_skipped_on_remove(monkeypatch):
    """Pure removals skip the visibility validator: ``binding_mode='remove'``
    can only subtract, so it can never grant access to an invisible resource.
    The stubbed validator raises if reached, proving it is not called."""
    m1, m2 = str(uuid.uuid4()), str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="Personal Helper",
        is_company_managed=False,
        mcp_server_ids=[m1, m2],
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    _stub_personal_bindings(
        monkeypatch, error="validator must not run for pure removals"
    )
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        mcp_server_ids=m1,
        binding_mode="remove",
    )

    assert not result.is_error
    assert "must not run" not in result.content
    assert agent.mcp_server_ids == [m2]
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_company_managed_skips_personal_binding_check(monkeypatch):
    """Company-managed agents use ``validate_company_managed_bindings`` (which
    is a no-op here because no bound row is non-company-managed), NOT the
    personal-visibility validator — so the stubbed personal validator (set to
    raise) is never reached."""
    kb_id = str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="Shared Helper",
        is_company_managed=True,
        knowledge_base_ids=[],
        knowledge_base_permissions=None,
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    _stub_personal_bindings(
        monkeypatch, error="personal validator must not run for company-managed"
    )
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        knowledge_base_ids=kb_id,
    )

    assert not result.is_error
    assert "must not run" not in result.content
    assert created[0].commits == 1


# ---------------------------------------------------------------------------
# update_agent — handover_conditions value-shape parity with REST
# ---------------------------------------------------------------------------
# REST enforces ``dict[str, str]`` via Pydantic. The builder bypasses Pydantic,
# so a non-string value would crash ``validate_handover_conditions`` with an
# AttributeError instead of returning a friendly, correctable error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_agent_handover_conditions_rejects_non_string_value(monkeypatch):
    """A non-string handover_conditions value is rejected with a friendly error
    (no AttributeError, no commit), mirroring the REST ``dict[str, str]``
    contract."""
    a1 = str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="Router",
        handover_agent_ids=[a1],
        handover_types={a1: "mandatory_transfer"},
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        handover_conditions=f'{{"{a1}": 123}}',
    )

    assert not result.is_error
    assert "values must be strings" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_update_agent_handover_conditions_accepts_string_value(monkeypatch):
    """A string handover_conditions value is accepted and committed."""
    a1 = str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="Router",
        handover_agent_ids=[a1],
        handover_types={a1: "mandatory_transfer"},
        handover_conditions={},
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        handover_conditions=f'{{"{a1}": "when the request is urgent"}}',
    )

    assert not result.is_error
    assert agent.handover_conditions == {a1: "when the request is urgent"}
    assert created[0].commits == 1


# ---------------------------------------------------------------------------
# update_agent — channel_config value-shape parity with REST
# ---------------------------------------------------------------------------
# REST enforces ``dict[str, dict]`` via Pydantic. The builder bypasses Pydantic,
# so a non-object channel entry would crash ``derive_available_channel_types``
# / ``validate_channel_config`` (both call ``ch_cfg.get(...)``) with an
# AttributeError instead of returning a friendly, correctable error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_agent_channel_config_rejects_non_object_value(monkeypatch):
    """A non-object channel_config value (e.g. a bare string) is rejected with a
    friendly error before ``derive_available_channel_types`` /
    ``validate_channel_config`` run, so the tool never crashes with an
    AttributeError. Nothing is committed."""
    agent = Agent(
        id=uuid.uuid4(),
        name="Chan Bot",
        is_company_managed=False,
        available_channel_types=None,
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        channel_config='{"slack": "oops"}',
    )

    assert not result.is_error
    assert "channel_config values must be JSON objects" in result.content
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_update_agent_channel_config_accepts_object_value(monkeypatch):
    """A well-formed channel_config (object values) is accepted: it is stored,
    available_channel_types is derived from it, and the change commits. The REST
    whitelist validator is stubbed (it needs a live channel bot row)."""
    import app.services.agent_bindings as agent_bindings_module

    async def _ok(*_a, **_k):
        return None

    monkeypatch.setattr(agent_bindings_module, "validate_channel_config", _ok)
    # The bot-visibility check needs a live bot row as well.
    import app.api.v1.binding_access as binding_access

    async def _none_refused(*_a, **_k):
        return set()

    monkeypatch.setattr(binding_access, "refused_channel_bots", _none_refused)

    bot_id = str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="Chan Bot",
        is_company_managed=False,
        available_channel_types=["web"],
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        channel_config=(
            '{"slack": {"channel_bot_id": "' + bot_id + '", "receive_enabled": true}}'
        ),
    )

    assert not result.is_error
    assert agent.channel_config == {
        "slack": {"channel_bot_id": bot_id, "receive_enabled": True}
    }
    assert "slack" in (agent.available_channel_types or [])
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_channel_config_merges_into_the_stored_map(monkeypatch):
    """#9898: setting one flag of one channel keeps every other channel and key
    (a bot id included); null drops a key or a channel."""
    import app.api.v1.binding_access as binding_access
    import app.services.agent_bindings as agent_bindings_module

    async def _ok(*_a, **_k):
        return None

    async def _none_refused(*_a, **_k):
        return set()

    monkeypatch.setattr(agent_bindings_module, "validate_channel_config", _ok)
    monkeypatch.setattr(binding_access, "refused_channel_bots", _none_refused)

    slack_bot, teams_bot = str(uuid.uuid4()), str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="Chan Bot",
        is_company_managed=False,
        available_channel_types=["web", "slack", "teams"],
        channel_config={
            "slack": {"channel_bot_id": slack_bot, "receive_enabled": True},
            "teams": {"channel_bot_id": teams_bot, "receive_enabled": True},
        },
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        channel_config='{"slack": {"publish_enabled": true}, "teams": null}',
    )

    assert not result.is_error
    assert agent.channel_config == {
        "slack": {"channel_bot_id": slack_bot, "receive_enabled": True, "publish_enabled": True}
    }


# ---------------------------------------------------------------------------
# get_agent — bound-resource NAME lookups are visibility-filtered
# ---------------------------------------------------------------------------
# A visible agent row can reference a resource the caller cannot see (stale
# group-share, legacy binding, company-managed agent bound to a private
# resource). Resolving that id to a name must not leak the name, so every
# id->name lookup carries the three-tier visibility predicate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_agent_name_lookups_are_visibility_filtered(monkeypatch):
    """The id->name SELECTs in get_agent (the generic ``_id_name_map`` queries
    and the integration-name lookup) must carry the ``is_company_managed``
    visibility predicate, so a bound resource the caller can't see never has its
    name revealed (only the already-visible id is shown)."""

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    kb_id = uuid.uuid4()
    gh_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Visible Agent",
        slug="visible-agent",
        description="desc",
        persona="p",
        enabled=True,
        tags=[],
        knowledge_base_ids=[str(kb_id)],
        knowledge_base_permissions={},
        mcp_server_ids=[],
        custom_tool_ids=[],
        integration_bindings={"github": [str(gh_id)]},
        prompt_ids=[],
        skill_ids=[],
        handover_agent_ids=[],
        handover_types={},
        is_company_managed=True,
        user_id=None,
    )

    class _RecordingSession(_FakeSession):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.statements: list = []

        async def execute(self, stmt):
            self.statements.append(stmt)
            return await super().execute(stmt)

    created: list = []

    def _make():
        return _RecordingSession(
            created,
            execute_results=[
                agent,  # agent SELECT
                0,  # assignment count
                [(kb_id, "Compliance KB")],  # KB id -> name
                [(gh_id, "GitHub Prod")],  # integration id -> name
            ],
        )

    monkeypatch.setattr(database, "async_session", _make)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_agent", agent_id=str(agent.id))

    assert not result.is_error
    sqls = [str(s) for s in created[0].statements]
    integ_sql = next(s for s in sqls if "integrations.name" in s)
    kb_sql = next(s for s in sqls if "knowledge_bases.name" in s)
    assert "is_company_managed" in integ_sql
    assert "is_company_managed" in kb_sql


# ---------------------------------------------------------------------------
# create_agent — personal-binding visibility parity with REST
# ---------------------------------------------------------------------------
# The REST create endpoint runs ``validate_personal_agent_bindings`` for
# personal agents so a user cannot bind another user's private integration / KB
# / MCP server / custom tool by guessing a UUID (token hijack / cross-user data
# access). create_agent must mirror that gate whenever a binding is provided.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_agent_personal_binding_visibility_enforced(monkeypatch):
    """Binding a KB on a new personal agent runs the REST visibility validator;
    an invisible resource is rejected with the REST detail and never persisted.
    The caller's non-admin status is forwarded so the validator does not
    bypass."""
    kb_id = str(uuid.uuid4())
    _admin_caller(monkeypatch, is_admin=False)
    seen: dict = {}
    _stub_personal_bindings(
        monkeypatch,
        recorder=seen,
        error="One or more knowledge bases were not found or are not accessible.",
    )
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_agent",
        name="Writer Bot",
        knowledge_base_ids=kb_id,
        create_default_assignments=False,
    )

    assert not result.is_error
    assert "not accessible" in result.content
    assert seen["knowledge_base_ids"] == [kb_id]
    assert seen["is_admin"] is False
    assert not any(isinstance(o, Agent) for c in created for o in c.added)
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_create_agent_personal_binding_admin_bypasses(monkeypatch):
    """An admin creator forwards ``is_admin=True`` so the validator bypasses the
    visibility check (parity with REST, where admins can configure any
    resource)."""
    mcp_id = str(uuid.uuid4())
    _admin_caller(monkeypatch, is_admin=True)
    seen: dict = {}
    _stub_personal_bindings(monkeypatch, recorder=seen)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_agent",
        name="Admin Bot",
        mcp_server_ids=mcp_id,
        create_default_assignments=False,
    )

    assert not result.is_error
    assert "created successfully" in result.content
    assert seen["is_admin"] is True
    assert seen["mcp_server_ids"] == [mcp_id]
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_create_agent_personal_binding_skipped_without_bindings(monkeypatch):
    """A personal agent created with NO bindings skips the visibility validator
    entirely (nothing to validate). The stubbed validator raises if reached."""
    _stub_personal_bindings(
        monkeypatch, error="validator must not run without bindings"
    )
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_agent",
        name="Bare Bot",
        create_default_assignments=False,
    )

    assert not result.is_error
    assert "must not run" not in result.content
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_create_agent_company_managed_skips_personal_binding_check(monkeypatch):
    """A company-managed agent uses ``validate_company_managed_bindings`` (a
    no-op here because no bound row is non-company-managed under the fake), NOT
    the personal-visibility validator - so the stubbed personal validator (set
    to raise) is never reached."""
    kb_id = str(uuid.uuid4())
    _admin_caller(monkeypatch, is_admin=True)
    _stub_personal_bindings(
        monkeypatch, error="personal validator must not run for company-managed"
    )
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_agent",
        name="Shared Bot",
        knowledge_base_ids=kb_id,
        is_company_managed=True,
        create_default_assignments=False,
    )

    assert not result.is_error
    assert "must not run" not in result.content
    assert created[0].commits == 1


# ---------------------------------------------------------------------------
# Package-installed custom tools and knowledge bases
# ---------------------------------------------------------------------------

_PACKAGE_ORIGIN_WRITES = [
    ("update_custom_tool", {"name": "Renamed"}, "Custom tools imported from skill packages cannot be modified"),
    ("delete_custom_tool", {}, "Custom tools imported from skill packages cannot be deleted"),
    ("update_knowledge_base", {"name": "Renamed"}, "Knowledge bases imported from skill packages cannot be modified"),
    ("delete_knowledge_base", {}, "Knowledge bases imported from skill packages cannot be deleted"),
    ("delete_kb_document", {"document_id": "__doc__"}, "Knowledge bases imported from skill packages cannot be modified"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "extra", "refusal"), _PACKAGE_ORIGIN_WRITES)
async def test_builder_writes_refuse_a_package_installed_entity(
    monkeypatch, tool_name, extra, refusal
):
    """The REST routes refuse these writes on a package-installed entity; the
    builder tools, another write surface, must refuse them too - even for the
    owner or an admin, whom the manage gate lets through."""
    monkeypatch.setattr(config_module.settings, "disable_custom_tools", False, raising=False)
    entity = SimpleNamespace(
        id=uuid.uuid4(),
        name="From a package",
        is_builtin=False,
        is_system_managed=False,
        document_count=1,
        chunk_count=1,
    )
    _stub_manageable_gate(monkeypatch, entity=entity, package_origin=True)
    doc = SimpleNamespace(id=uuid.uuid4(), filename="a.md", chunk_count=1, knowledge_base_id=entity.id)
    created: list = []
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[doc])
    )
    id_arg = "tool_id" if "custom_tool" in tool_name else "knowledge_base_id"
    arguments = {id_arg: str(entity.id)}
    for key, value in extra.items():
        arguments[key] = str(doc.id) if value == "__doc__" else value

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, tool_name, **arguments)

    assert refusal in result.content
    assert entity.name == "From a package"
    assert created[0].commits == 0
    assert created[0].deleted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["update_custom_tool", "update_knowledge_base"])
async def test_builder_writes_still_work_on_an_entity_no_package_installed(
    monkeypatch, tool_name
):
    monkeypatch.setattr(config_module.settings, "disable_custom_tools", False, raising=False)
    entity = SimpleNamespace(id=uuid.uuid4(), name="Mine", is_builtin=False, tags=[])
    _stub_manageable_gate(monkeypatch, entity=entity, package_origin=False)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    id_arg = "tool_id" if "custom_tool" in tool_name else "knowledge_base_id"

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, tool_name, **{id_arg: str(entity.id), "name": "Renamed"})

    assert not result.is_error
    assert entity.name == "Renamed"
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_add_kb_document_refuses_a_package_installed_knowledge_base(monkeypatch):
    caller = _admin_caller(monkeypatch, is_admin=True)
    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="From a package",
        is_builtin=False,
        is_company_managed=False,
        user_id=caller.id,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
        document_count=3,
    )
    _stub_package_origin(monkeypatch, True)
    created: list = []
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[[], kb])
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(caller.id))
    result = await _call(
        executor, "add_kb_document", knowledge_base_id=str(kb.id), title="t", content="body"
    )

    assert "Knowledge bases imported from skill packages cannot be modified" in result.content
    assert created[0].added == []
    assert created[0].commits == 0
    assert kb.document_count == 3


@pytest.mark.asyncio
async def test_add_kb_document_refuses_a_read_only_knowledge_base_for_its_owner(monkeypatch):
    """The REST add-document route refuses a read-only KB; the builder tool
    runs the same gate, with the same sentence, for a non-admin owner."""
    caller = _admin_caller(monkeypatch, is_admin=False)
    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="Ground truth",
        is_builtin=False,
        is_read_only=True,
        is_company_managed=False,
        user_id=caller.id,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
        document_count=3,
    )
    _stub_package_origin(monkeypatch, False)
    created: list = []
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[[], kb])
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(caller.id))
    result = await _call(
        executor, "add_kb_document", knowledge_base_id=str(kb.id), title="t", content="body"
    )

    assert (
        "'Ground truth' is read-only: its content is locked, so you cannot add "
        "documents to it." in result.content
    )
    assert created[0].added == []
    assert created[0].commits == 0
    assert kb.document_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "extra", "refusal"),
    [
        ("delete_kb_document", {"document_id": "__doc__"}, "so you cannot delete documents from it"),
        ("delete_knowledge_base", {}, "so you cannot delete it"),
    ],
)
async def test_builder_deletes_refuse_a_read_only_knowledge_base(
    monkeypatch, tool_name, extra, refusal
):
    """Deleting a document of, or the whole, read-only KB is refused like the
    REST routes refuse it - even for the owner the manage gate lets through."""
    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="Ground truth",
        is_builtin=False,
        is_read_only=True,
        is_system_managed=False,
        document_count=1,
        chunk_count=1,
    )
    _stub_manageable_gate(monkeypatch, entity=kb, package_origin=False)
    doc = SimpleNamespace(id=uuid.uuid4(), filename="a.md", chunk_count=1, knowledge_base_id=kb.id)
    created: list = []
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[doc])
    )
    arguments = {"knowledge_base_id": str(kb.id)}
    for key, value in extra.items():
        arguments[key] = str(doc.id) if value == "__doc__" else value

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, tool_name, **arguments)

    assert refusal in result.content
    assert "is read-only" in result.content
    assert created[0].commits == 0
    assert created[0].deleted == []


# ---------------------------------------------------------------------------
# update_mcp_server — a masked argument or URL keeps its stored value
# ---------------------------------------------------------------------------

_BUILDER_TOKEN = "ghp_" + "a" * 36
_BUILDER_URL = "https://user:pw-s3cret@mcp.example.com/sse?token=qt0k-xyz"
_BUILDER_SHOWN_URL = "https://user:***@mcp.example.com/sse?token=***"


@pytest.mark.asyncio
async def test_update_mcp_server_masked_arg_keeps_the_stored_token(monkeypatch):
    """The arguments the MCP API shows are masked; one pasted back as shown
    (same rule as ``PUT /mcp-servers/{id}``) keeps the value it stands for,
    a bare ``***`` by the secret flag before it, and a changed argument is
    saved as typed."""
    from app.core.utils import mask_secret_arg

    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="GitHub MCP",
        args=["-y", "server", _BUILDER_TOKEN, "--client-secret", "s3cret"],
    )
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_mcp_server",
        server_id=str(srv.id),
        args=(
            f'["-y", "other-server", "--client-secret", "***", '
            f'"{mask_secret_arg(_BUILDER_TOKEN)}"]'
        ),
    )

    assert not result.is_error
    assert srv.args == ["-y", "other-server", "--client-secret", "s3cret", _BUILDER_TOKEN]
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_mcp_server_ambiguous_masked_arg_is_refused(monkeypatch):
    """Two stored tokens sharing a mask, one dropped: which one is left cannot
    be told, so nothing is written."""
    from app.core.utils import mask_secret_arg

    other = "ghp_aa" + "b" * 34
    stored = [_BUILDER_TOKEN, "-e", other]
    srv = SimpleNamespace(id=uuid.uuid4(), name="GitHub MCP", args=list(stored))
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_mcp_server",
        server_id=str(srv.id),
        args=f'["{mask_secret_arg(other)}"]',
    )

    assert result.content == (
        "An argument is still masked and cannot be matched to exactly one "
        "stored value. Ask the user for the full value of each masked argument."
    )
    assert srv.args == stored
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_mcp_server_masked_urls_keep_the_stored_urls(monkeypatch):
    """A URL pasted back as ``get_mcp_server`` shows it keeps the stored one,
    the OAuth endpoints too."""
    token_url = "https://idp.example.com/token?client_secret=cs-s3cret"
    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="Remote MCP",
        transport="sse",
        url=_BUILDER_URL,
        authorize_url=None,
        token_url=token_url,
    )
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    async def _allowed(_url):
        return None

    from app.integrations import token_endpoint

    monkeypatch.setattr(token_endpoint, "assert_token_endpoint_allowed", _allowed)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_mcp_server",
        server_id=str(srv.id),
        url=_BUILDER_SHOWN_URL,
        token_url="https://idp.example.com/token?client_secret=***",
    )

    assert not result.is_error
    assert srv.url == _BUILDER_URL
    assert srv.token_url == token_url
    assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_mcp_server_judges_the_restored_token_url(monkeypatch):
    """The token endpoint policy reads the URL that would be saved: the stored
    one a masked value stands for, and nothing is written when it refuses."""
    stored_token_url = "https://idp.example.com/token?client_secret=cs-s3cret"
    authorize_url = "https://idp.example.com/authorize?client_secret=cs-s3cret"
    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="Remote MCP",
        transport="sse",
        url=None,
        authorize_url=authorize_url,
        token_url=stored_token_url,
    )
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    judged: list[str] = []

    async def _refused(url):
        judged.append(url)
        if "cs-s3cret" in url:
            from app.integrations.token_endpoint import TokenEndpointRefusedError

            raise TokenEndpointRefusedError("refused")

    from app.integrations import token_endpoint

    monkeypatch.setattr(token_endpoint, "assert_token_endpoint_allowed", _refused)

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_mcp_server",
        server_id=str(srv.id),
        authorize_url="https://idp.example.com/authorize?client_secret=***",
        token_url="https://idp.example.com/token?client_secret=***",
    )

    # The value as sent first, then the stored one it stands for.
    assert judged == ["https://idp.example.com/token?client_secret=***", stored_token_url]
    assert "Invalid token_url" in result.content
    assert srv.token_url == stored_token_url
    assert srv.authorize_url == authorize_url
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_mcp_server_masked_authorize_url_edited_around_is_refused(monkeypatch):
    authorize_url = "https://idp.example.com/authorize?client_secret=cs-s3cret"
    srv = SimpleNamespace(id=uuid.uuid4(), name="Remote MCP", authorize_url=authorize_url)
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_mcp_server",
        server_id=str(srv.id),
        authorize_url="https://other.example.com/authorize?client_secret=***",
    )

    assert result.content == (
        "The authorization URL is still masked and does not match the stored "
        "one. Ask the user for the full authorization URL."
    )
    assert srv.authorize_url == authorize_url
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_mcp_server_url_edited_around_its_mask_is_refused(monkeypatch):
    srv = SimpleNamespace(id=uuid.uuid4(), name="Remote MCP", transport="sse", url=_BUILDER_URL)
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_mcp_server",
        server_id=str(srv.id),
        url="https://user:***@other.example.com/sse",
    )

    assert result.content == (
        "The URL is still masked and does not match the stored one. "
        "Ask the user for the full URL."
    )
    assert srv.url == _BUILDER_URL
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_get_mcp_server_shows_the_url_masked(monkeypatch):
    """Anyone who sees the server reads this, and so does the model: its URL
    is masked the way the MCP API shows it."""
    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="Remote MCP",
        transport="sse",
        command=None,
        url=_BUILDER_URL,
        auth_mode="headers",
        enabled=True,
        is_company_managed=True,
        discovered_tools=None,
    )

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)
    created: list = []
    monkeypatch.setattr(
        database, "async_session", _session_factory(created, execute_results=[srv])
    )

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_mcp_server", server_id=str(srv.id))

    assert not result.is_error
    assert f"  • URL: {_BUILDER_SHOWN_URL}" in result.content.splitlines()
    assert "pw-s3cret" not in result.content
    assert "qt0k-xyz" not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("moved", "refused"),
    [(False, False), (True, True)],
    ids=["sent-back-as-read", "token-moved"],
)
async def test_update_mcp_server_masked_args_are_restored_before_the_repoint_check(
    monkeypatch, moved, refused
):
    """The re-point check compares the arguments to the stored ones:
    it sees them restored, so sending them back as read is no re-point of a
    reference the caller could not write, while a token moved elsewhere is."""
    from app.core.utils import redact_secret_args
    from app.services.builtin_tools import agent_builder

    async def _non_admin(_db, _uid):
        return SimpleNamespace(is_admin=False)

    monkeypatch.setattr(agent_builder, "_load_caller_user_for_builder", _non_admin)
    monkeypatch.setattr(
        agent_builder,
        "retained_reference_writer_problem",
        lambda *_args, **_kwargs: "keeps a reference you could not write",
    )
    stored = ["-y", "server", _BUILDER_TOKEN]
    srv = SimpleNamespace(id=uuid.uuid4(), name="GitHub MCP", args=list(stored))
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    shown = redact_secret_args(stored)
    sent = [shown[-1], *shown[:-1]] if moved else shown

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_mcp_server", server_id=str(srv.id), args=json.dumps(sent)
    )

    if refused:
        assert result.content == (
            "MCP server not updated - keeps a reference you could not write."
        )
        assert created[0].commits == 0
    else:
        assert not result.is_error
        assert created[0].commits == 1
    assert srv.args == stored


# ---------------------------------------------------------------------------
# MCP server URL policy (the REST routes' save check, on the builder's path)
# ---------------------------------------------------------------------------


def _mcp_url_policy(policy: str) -> None:
    from app.core import security_options

    security_options.publish_stored({"mcp_server_url_policy": policy, "mcp_server_trusted_origins": ()})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "url", "refused"),
    [
        ("permissive", "http://169.254.169.254/latest/meta-data/", True),
        ("permissive", "http://[fd00:ec2::254]/", True),
        ("permissive", "http://127.0.0.1:8080/mcp", False),
        ("private_networks", "http://127.0.0.1:8080/mcp", True),
        ("private_networks", "http://10.0.0.5:8080/mcp", False),
        ("public_only", "http://10.0.0.5:8080/mcp", True),
    ],
)
async def test_create_mcp_server_holds_the_url_to_the_policy(monkeypatch, policy, url, refused):
    _mcp_url_policy(policy)
    _admin_caller(monkeypatch, is_admin=False)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    result = await _call(executor, "create_mcp_server", name="srv", transport="sse", url=url)

    if refused:
        assert result.content.startswith("Invalid url: This MCP server URL is not allowed")
        assert created == []
    else:
        assert "created successfully" in result.content, result.content
        assert created[0].commits == 1


@pytest.mark.asyncio
async def test_update_mcp_server_judges_the_server_as_it_will_be_saved(monkeypatch):
    _mcp_url_policy("private_networks")
    srv = SimpleNamespace(
        id=uuid.uuid4(), name="Remote", transport="stdio", command="npx", url="http://127.0.0.1:8080/mcp",
        relay_group_id=None,
    )
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    # The stored loopback URL becomes reachable by switching the transport.
    switched = await _call(executor, "update_mcp_server", server_id=str(srv.id), transport="sse")
    assert switched.content.startswith("Invalid url: This MCP server URL is not allowed")
    assert (srv.transport, srv.url) == ("stdio", "http://127.0.0.1:8080/mcp")

    srv.transport, srv.url = "sse", "https://mcp.example.com/sse"
    to_metadata = await _call(executor, "update_mcp_server", server_id=str(srv.id), url="http://169.254.169.254/")
    assert to_metadata.content.startswith("Invalid url: This MCP server URL is not allowed")
    assert srv.url == "https://mcp.example.com/sse"
    assert all(session.commits == 0 for session in created)


@pytest.mark.asyncio
async def test_update_mcp_server_masked_url_pasted_back_is_no_new_destination(monkeypatch):
    """A URL pasted back as ``get_mcp_server`` shows it (password masked) is
    restored before the URL policy decides whether the destination changed."""
    from app.core.utils import redact_secret_text

    _mcp_url_policy("public_only")
    stored = "http://user:pw-s3cret@127.0.0.1:8080/sse"
    srv = SimpleNamespace(
        id=uuid.uuid4(), name="Kept", transport="sse", command=None, url=stored, args=None,
        authorize_url=None, token_url=None, relay_group_id=None,
    )
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    shown = redact_secret_text(stored)
    result = await _call(executor, "update_mcp_server", server_id=str(srv.id), name="Renamed", url=shown)

    assert shown != stored
    assert not result.is_error, result.content
    assert (srv.name, srv.url) == ("Renamed", stored)


@pytest.mark.asyncio
async def test_update_mcp_server_of_a_routed_server_judges_only_what_the_url_spells(monkeypatch):
    _mcp_url_policy("public_only")
    srv = SimpleNamespace(
        id=uuid.uuid4(), name="Routed", transport="sse", command=None, url="https://mcp.example.com/sse",
        relay_group_id=uuid.uuid4(),
    )
    _stub_manageable_gate(monkeypatch, entity=srv)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    result = await _call(executor, "update_mcp_server", server_id=str(srv.id), url="http://10.0.0.5:8080/sse")

    assert not result.is_error, result.content
    assert srv.url == "http://10.0.0.5:8080/sse"

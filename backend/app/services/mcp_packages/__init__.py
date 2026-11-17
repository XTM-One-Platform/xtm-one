# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Downloadable installer packages for the ``/mcp/xtm`` server.

Three shapes of the same thing, because the ecosystem has three answers to
"install this MCP server" and they are not interchangeable:

``claude-code``
    A Claude Code plugin, wrapped in a single-plugin marketplace so the
    documented install path can prompt for the API key.  ``--plugin-dir``
    loads a plugin but does **not** prompt for ``userConfig``, so a plugin
    shipped for that path alone would reach the server with an unresolved
    ``${user_config.api_token}``.

``mcpb``
    A Claude Desktop extension.  The ``.mcpb`` format runs a **local** server
    over stdio, so a remote Streamable HTTP endpoint cannot be declared
    directly; the bundle carries a small dependency-free stdio↔HTTP bridge
    (``data/proxy/index.js``) and Claude Desktop collects the API key through
    the manifest's ``user_config``.

``agent-plugin``
    An Agent Plugins v1.0.0 package (agent-plugins.org) — the vendor-neutral
    shape, for clients that are neither.

All three carry the **same skill**, so whichever a user installs, the model
gets the same guidance on the tool surface.

Credentials are opt-in, and only for the two client-specific formats.  Given
an *api_key*, they embed it so the install asks nothing — the archive is then
a live credential and its README says so.  Without one they fall back to the
client's own secure prompt.  The Agent Plugins package never embeds a key:
the specification states that configured headers are literal, visible package
data that must not carry secrets, and defines no portable credential
reference, so a key there would make the package non-conformant — which is
the one property that format is chosen for.

The skill's prose is authored in ``data/`` and versioned with the code.  Its
tool reference is **generated** from the caller's own ``ToolDefinition`` list
instead, so the document can never drift from what the server actually
exposes — and it describes the surface that caller will really get.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.tools.base import ToolDefinition

DATA_DIR = Path(__file__).parent / "data"

#: Package formats offered for download. Order is the UI's display order.
PACKAGE_FORMATS: tuple[str, ...] = ("claude-code", "mcpb", "agent-plugin")

#: Stable identifiers. Kebab-case in every format's manifest, and used to name
#: the plugin directory, so they must not carry the (configurable) brand.
PLUGIN_NAME = "xtm-one-platform"
MARKETPLACE_NAME = "xtm-one"
SKILL_NAME = "xtm-one-platform-tools"
MCP_SERVER_KEY = "xtm-one-platform"


@dataclass(frozen=True)
class PackageTarget:
    """One MCP server a package can be built for.

    A package targets **one** server. Bundling every server into a single
    package is technically trivial and a trap: the client loads every tool
    definition of every server into its context on each request, and tool
    selection degrades long before the ~213 that "all of them" currently
    means. Installing two packages is the user's explicit choice; a button
    that quietly makes it for them is not.
    """

    #: Wire value of the ``target`` request field.
    key: str
    #: Path segment under ``/mcp/``.
    path: str
    #: Kebab-case plugin identifier — stable, and brand-free because the brand
    #: is deployment-configurable.
    plugin_name: str
    #: Agent Skills name (also the skill's directory).
    skill_name: str
    #: Key of the entry in ``mcpServers``.
    server_key: str
    #: Product name shown to a human. ``None`` means "this platform's brand",
    #: resolved at build time.
    display: str | None
    #: Whether an authored SKILL.md exists in ``data/``. Only the platform's
    #: own server has one; a bridge ships the generated catalogue alone, since
    #: writing guidance for OpenCTI's tools is OpenCTI's job, not this
    #: packager's — and invented prose would rot without anyone noticing.
    authored_skill: bool


PACKAGE_TARGETS: dict[str, PackageTarget] = {
    "xtm": PackageTarget(
        key="xtm",
        path="xtm",
        plugin_name=PLUGIN_NAME,
        skill_name=SKILL_NAME,
        server_key=MCP_SERVER_KEY,
        display=None,
        authored_skill=True,
    ),
    "opencti": PackageTarget(
        key="opencti",
        path="opencti",
        plugin_name="xtm-one-opencti",
        skill_name="opencti-tools",
        server_key="opencti",
        display="OpenCTI",
        authored_skill=False,
    ),
    "openaev": PackageTarget(
        key="openaev",
        path="openaev",
        plugin_name="xtm-one-openaev",
        skill_name="openaev-tools",
        server_key="openaev",
        display="OpenAEV",
        authored_skill=False,
    ),
    "opencrq": PackageTarget(
        key="opencrq",
        path="opencrq",
        plugin_name="xtm-one-opencrq",
        skill_name="opencrq-tools",
        server_key="opencrq",
        display="OpenCRQ",
        authored_skill=False,
    ),
}


class UnknownPackageTargetError(ValueError):
    """Raised for a target outside :data:`PACKAGE_TARGETS`."""


def resolve_target(key: str) -> PackageTarget:
    target = PACKAGE_TARGETS.get(key)
    if target is None:
        raise UnknownPackageTargetError(key)
    return target

#: Canonical schema identifiers. Clients select validation rules from these
#: strings without fetching them, so they are exact constants, not templates.
AGENT_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
AGENT_PLUGIN_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
CLAUDE_PLUGIN_SCHEMA = (
    "https://json.schemastore.org/claude-code-plugin-manifest.json"
)
MCPB_MANIFEST_VERSION = "0.3"

#: Every ZIP entry is stamped with the same fixed timestamp so two downloads
#: of an unchanged package are byte-identical — a moving mtime would give a
#: different checksum every time and make the download look like it changed.
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

#: Tool-name fragment → section heading for the generated reference, in
#: first-match order. ``agent`` sits late deliberately: ``list_agent_templates``
#: and ``inspect_agent_traces`` both contain it, and both belong elsewhere.
_TOOL_GROUPS: tuple[tuple[str, str], ...] = (
    ("trace", "Traces"),
    ("assignment", "Assignments"),
    ("_run", "Runs"),
    ("knowledge", "Knowledge bases"),
    ("_kb_", "Knowledge bases"),
    ("prompt", "Prompts"),
    ("skill", "Skills"),
    ("variable", "Variables"),
    ("custom_tool", "Custom tools"),
    ("integration", "Integrations"),
    ("mcp_server", "MCP servers"),
    ("agent", "Agents"),
)
_TOOL_GROUP_FALLBACK = "Other"

#: Section order in the generated reference. A group with no tools is skipped.
_GROUP_ORDER: tuple[str, ...] = (
    "Traces",
    "Agents",
    "Assignments",
    "Runs",
    "Knowledge bases",
    "Prompts",
    "Skills",
    "Custom tools",
    "Integrations",
    "MCP servers",
    "Variables",
    _TOOL_GROUP_FALLBACK,
)


#: Env var a credential-free snippet points at, so the two clients agree on
#: one name and a user exports it once.
SNIPPET_ENV_VAR = "XTM_ONE_API_KEY"


@dataclass(frozen=True)
class SnippetClient:
    """A client configured by editing a file, not by installing a package.

    These are emitted as **fragments to merge**, never as downloadable files:
    the target holds the user's other MCP servers (and, for Codex, the rest of
    their settings), so handing over a whole file invites someone to overwrite
    a working configuration with ours.
    """

    key: str
    display: str
    #: Where the user pastes it.
    path: str
    #: Syntax highlighting hint for the UI.
    language: str


SNIPPET_CLIENTS: dict[str, SnippetClient] = {
    # One file covers the Codex CLI, the Codex IDE extension and the ChatGPT
    # desktop app - they share this configuration, so this is one entry rather
    # than three.
    "codex": SnippetClient(
        key="codex",
        display="Codex / ChatGPT desktop",
        path="~/.codex/config.toml",
        language="toml",
    ),
    "cursor": SnippetClient(
        key="cursor",
        display="Cursor",
        path="~/.cursor/mcp.json",
        language="json",
    ),
}


class UnknownSnippetClientError(ValueError):
    """Raised for a client outside :data:`SNIPPET_CLIENTS`."""


def build_snippet(
    client_key: str,
    *,
    url: str,
    target: PackageTarget,
    api_key: str | None,
) -> tuple[SnippetClient, str]:
    """Render the config fragment for *client_key*. Returns ``(client, text)``.

    Without an *api_key* the fragment references an environment variable
    instead - the shape each client documents for keeping a token out of a
    file that often ends up in a dotfiles repository.
    """
    client = SNIPPET_CLIENTS.get(client_key)
    if client is None:
        raise UnknownSnippetClientError(client_key)

    if client.key == "codex":
        # The table name must be snake_case: Codex ignores a malformed
        # ``[mcp_servers....]`` section silently, so a kebab-case key here
        # would look installed and simply never load.
        name = target.server_key.replace("-", "_")
        lines = [f"[mcp_servers.{name}]", f'url = "{url}"']
        if api_key:
            lines += [
                "",
                f"[mcp_servers.{name}.http_headers]",
                f'Authorization = "Bearer {api_key}"',
            ]
        else:
            lines.append(f'bearer_token_env_var = "{SNIPPET_ENV_VAR}"')
        return client, "\n".join(lines) + "\n"

    authorization = (
        f"Bearer {api_key}" if api_key else f"Bearer ${{env:{SNIPPET_ENV_VAR}}}"
    )
    body = {
        "mcpServers": {
            target.server_key: {
                "url": url,
                "headers": {"Authorization": authorization},
            }
        }
    }
    return client, json.dumps(body, indent=2, ensure_ascii=False) + "\n"


class UnknownPackageFormatError(ValueError):
    """Raised for a format outside :data:`PACKAGE_FORMATS`."""


def _read_data(*parts: str) -> str:
    return (DATA_DIR.joinpath(*parts)).read_text(encoding="utf-8")


def _render(template: str, platform_name: str, key_name: str = "") -> str:
    return template.replace("{{PLATFORM_NAME}}", platform_name).replace(
        "{{KEY_NAME}}", key_name
    )


def _readme(
    package_format: str,
    *,
    platform_name: str,
    api_key: str | None,
    key_name: str,
) -> str:
    """Render a README, with the install/auth section the mode calls for."""
    body = _read_data("readme", f"{package_format}.md")
    if "{{INSTALL_AUTH}}" in body:
        mode = "embedded" if api_key else "prompted"
        body = body.replace(
            "{{INSTALL_AUTH}}",
            _read_data("readme", "auth", f"{package_format}-{mode}.md").strip(),
        )
    return _render(body, platform_name, key_name)


def _display_title(target: PackageTarget, platform_name: str) -> str:
    """Human-facing package name. A bridge names its product; the platform's
    own server names the brand, which is deployment-configurable."""
    if target.display is None:
        return f"{platform_name} platform tools"
    return f"{target.display} (via {platform_name})"


def _description(target: PackageTarget, platform_name: str) -> str:
    if target.display is None:
        return (
            f"Read {platform_name} agent traces and manage agents, "
            "assignments, knowledge bases, prompts and skills."
        )
    return (
        f"{target.display} business and knowledge tools, exposed through "
        f"{platform_name}. Every call runs with your own identity on "
        f"{target.display}."
    )


def _group_for(tool_name: str) -> str:
    for fragment, heading in _TOOL_GROUPS:
        if fragment in tool_name:
            return heading
    return _TOOL_GROUP_FALLBACK


def _tool_reference(tools: list[ToolDefinition], platform_name: str) -> str:
    """Render the per-caller tool reference shipped inside the skill.

    Generated rather than authored: the surface is resolved per user, so a
    hand-maintained list would be wrong for anyone whose permissions differ
    from the author's — and stale the first time a tool is added.
    """
    grouped: dict[str, list[ToolDefinition]] = {}
    for tool in tools:
        grouped.setdefault(_group_for(tool.name), []).append(tool)

    lines = [
        f"# {platform_name} tools — reference",
        "",
        (
            "Generated from the tool surface this server offered when the "
            "package was downloaded. The surface is resolved per user, so a "
            "colleague with different permissions may see fewer tools than "
            "are listed here."
        ),
        "",
    ]

    for heading in _GROUP_ORDER:
        entries = grouped.get(heading)
        if not entries:
            continue
        lines.append(f"## {heading}")
        lines.append("")
        for tool in sorted(entries, key=lambda t: t.name):
            description = " ".join((tool.description or "").split())
            lines.append(f"- **`{tool.name}`** — {description}")
            required = ((tool.input_schema or {}).get("required")) or []
            if required:
                lines.append(
                    f"  - required: {', '.join(f'`{arg}`' for arg in required)}"
                )
        lines.append("")

    return "\n".join(lines)


def _generated_skill(target: PackageTarget, platform_name: str) -> str:
    """SKILL.md for a bridge: the catalogue, and nothing invented.

    A bridge's tools belong to its own product, which documents them; writing
    workflow guidance here would be this packager speaking for OpenCTI, and it
    would rot silently as that product moves. So the skill states what it is,
    where the authority lies, and points at the generated reference.
    """
    display = target.display or platform_name
    return (
        "---\n"
        f"name: {target.skill_name}\n"
        f"description: Catalogue of the {display} tools exposed through "
        f"{platform_name}'s MCP bridge. Use when working with {display} data "
        f"— entities, searches and mutations — to see which tools exist and "
        f"what each one takes.\n"
        "---\n"
        "\n"
        f"# {display} tools, via {platform_name}\n"
        "\n"
        f"This skill carries the catalogue of the {display} tools this server "
        "exposes. It is generated from the surface the server offered when the "
        "package was downloaded, so it lists what your permissions actually "
        "reach rather than a maintained-by-hand list.\n"
        "\n"
        f"Every call runs as **you** on {display}: {platform_name} signs a "
        "short-lived token for your user, so that product's own permissions, "
        "markings and audit trail apply exactly as if you had acted there "
        "directly. A refusal is that platform's answer, not a bug here.\n"
        "\n"
        f"{display}'s own documentation is the authority on what these tools "
        "mean. See `references/tool-reference.md` for the full list.\n"
    )


def _skill_files(
    tools: list[ToolDefinition],
    platform_name: str,
    *,
    prefix: str,
    target: PackageTarget,
) -> dict[str, str]:
    """The skill, laid out per the Agent Skills spec, under *prefix*."""
    root = f"{prefix}skills/{target.skill_name}"
    body = (
        _render(_read_data("skills", target.skill_name, "SKILL.md"), platform_name)
        if target.authored_skill
        else _generated_skill(target, platform_name)
    )
    return {
        f"{root}/SKILL.md": body,
        f"{root}/references/tool-reference.md": _tool_reference(
            tools, target.display or platform_name
        ),
    }


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            # 0o644, as the ZIP external attribute wants it (high 16 bits).
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[path])
    return buffer.getvalue()


# ── Claude Code plugin ───────────────────────────────────────────────────────


def _claude_code_files(
    *,
    url: str,
    platform_name: str,
    app_version: str,
    tools: list[ToolDefinition],
    api_key: str | None,
    key_name: str,
    target: PackageTarget,
) -> dict[str, str]:
    prefix = f"{target.plugin_name}/"
    manifest = {
        "$schema": CLAUDE_PLUGIN_SCHEMA,
        "name": target.plugin_name,
        "displayName": _display_title(target, platform_name),
        "version": app_version,
        "description": _description(target, platform_name),
        "homepage": url.rsplit("/mcp/", 1)[0],
        "keywords": ["mcp", "observability", "traces", "agents", "xtm-one"],
    }
    # No ``userConfig`` when the key is embedded: an unused prompt would ask
    # for a value nothing reads, and `--plugin-dir` (which never prompts) then
    # works too, which is the whole point of the embedded download.
    if not api_key:
        manifest["userConfig"] = {
            "api_token": {
                "type": "string",
                "title": f"{platform_name} API key",
                "description": (
                    "A personal API key (fcp-...) from My Profile > API Keys. "
                    "Every call runs with your own identity and permissions."
                ),
                "sensitive": True,
                "required": True,
            }
        }
    # The documented remote form is url + headers; ``transport`` is only set
    # for the legacy SSE variant, which this server does not speak.
    mcp = {
        "mcpServers": {
            target.server_key: {
                "url": url,
                "headers": {
                    "Authorization": (
                        f"Bearer {api_key}"
                        if api_key
                        else "Bearer ${user_config.api_token}"
                    )
                },
            }
        }
    }
    # Single-plugin marketplace: the install path that prompts for the token.
    marketplace = {
        "name": MARKETPLACE_NAME,
        "owner": {"name": platform_name},
        "description": f"{platform_name} plugins.",
        "plugins": [
            {
                "name": target.plugin_name,
                "source": "./",
                "description": manifest["description"],
            }
        ],
    }

    files = {
        f"{prefix}.claude-plugin/plugin.json": _json(manifest),
        f"{prefix}.claude-plugin/marketplace.json": _json(marketplace),
        f"{prefix}.mcp.json": _json(mcp),
        f"{prefix}README.md": _readme(
            "claude-code",
            platform_name=platform_name,
            api_key=api_key,
            key_name=key_name,
        ),
    }
    files.update(_skill_files(tools, platform_name, prefix=prefix, target=target))
    return files


# ── Claude Desktop extension (.mcpb) ─────────────────────────────────────────


def _mcpb_files(
    *,
    url: str,
    platform_name: str,
    app_version: str,
    tools: list[ToolDefinition],
    api_key: str | None,
    key_name: str,
    target: PackageTarget,
) -> dict[str, str]:
    # No wrapping directory: Claude Desktop reads ``manifest.json`` at the
    # archive root.
    manifest = {
        "manifest_version": MCPB_MANIFEST_VERSION,
        "name": target.plugin_name,
        "display_name": _display_title(target, platform_name),
        "version": app_version,
        "description": _description(target, platform_name),
        "author": {"name": platform_name},
        "server": {
            "type": "node",
            "entry_point": "server/index.js",
            "mcp_config": {
                "command": "node",
                "args": ["${__dirname}/server/index.js"],
                "env": {
                    "XTM_MCP_URL": url,
                    "XTM_MCP_TOKEN": (
                        api_key if api_key else "${user_config.api_key}"
                    ),
                },
            },
        },
        "compatibility": {
            # Node 18 is the floor for the global ``fetch`` the bridge uses.
            "platforms": ["darwin", "win32", "linux"],
            "runtimes": {"node": ">=18.0.0"},
        },
    }

    # Same reasoning as the Claude Code manifest: with the key embedded there
    # is nothing to ask for, and an empty install prompt reads like a bug.
    if not api_key:
        manifest["user_config"] = {
            "api_key": {
                "type": "string",
                "title": f"{platform_name} API key",
                "description": (
                    "A personal API key (fcp-...) from My Profile > API Keys. "
                    "Every call runs with your own identity and permissions."
                ),
                "required": True,
                "sensitive": True,
            }
        }

    files = {
        "manifest.json": _json(manifest),
        # NOT rendered. ``_render`` is an unescaped replace and
        # ``platform_name`` is admin-configurable branding text; this file is
        # the only one in any package that a client *executes*. A brand
        # containing ``*/`` would close the banner comment and turn the rest
        # into JavaScript running on every workstation that installs the
        # bundle. The JSON manifests are safe because ``json.dumps`` escapes,
        # so removing the brand from here closes the whole surface — and the
        # banner never needed it.
        "server/index.js": _read_data("proxy", "index.js"),
        "README.md": _readme(
            "mcpb",
            platform_name=platform_name,
            api_key=api_key,
            key_name=key_name,
        ),
    }
    # Claude Desktop does not read a plugin ``skills/`` directory, but the
    # skill still ships: it is the guidance for these tools, and a user who
    # wants it can add it as a Desktop skill instead of hunting for it.
    files.update(_skill_files(tools, platform_name, prefix="", target=target))
    return files


# ── Agent Plugins v1.0.0 ─────────────────────────────────────────────────────


def _agent_plugin_files(
    *,
    url: str,
    platform_name: str,
    app_version: str,
    tools: list[ToolDefinition],
    api_key: str | None,
    key_name: str,
    target: PackageTarget,
) -> dict[str, str]:
    # *api_key* is accepted for a uniform builder signature and deliberately
    # unused: see the module docstring — a credential here would make the
    # package non-conformant.
    del api_key, key_name
    prefix = f"{target.plugin_name}/"
    # Closed schema: only the ten permitted top-level fields, or a conformant
    # client rejects the manifest outright.
    manifest = {
        "$schema": AGENT_PLUGIN_SCHEMA,
        "name": target.plugin_name,
        "version": app_version,
        "description": _description(target, platform_name),
        "author": {"name": platform_name},
        "homepage": url.rsplit("/mcp/", 1)[0],
        "keywords": ["mcp", "observability", "traces", "agents", "xtm-one"],
    }
    # No Authorization header: the spec states configured headers are literal,
    # visible package data and must not carry credentials, and defines no
    # portable credential-reference field. Authentication stays client-managed.
    mcp = {
        "$schema": AGENT_PLUGIN_MCP_SCHEMA,
        "mcpServers": {
            target.server_key: {
                "type": "streamable-http",
                "url": url,
            }
        },
    }

    files = {
        f"{prefix}plugin.json": _json(manifest),
        f"{prefix}mcp.json": _json(mcp),
        f"{prefix}README.md": _readme(
            "agent-plugin",
            platform_name=platform_name,
            api_key=None,
            key_name="",
        ),
    }
    files.update(_skill_files(tools, platform_name, prefix=prefix, target=target))
    return files


def _json(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


# ── Entry point ──────────────────────────────────────────────────────────────

_BUILDERS = {
    "claude-code": (_claude_code_files, "{name}-claude-code.zip"),
    "mcpb": (_mcpb_files, "{name}.mcpb"),
    "agent-plugin": (_agent_plugin_files, "{name}-agent-plugin.zip"),
}


def embeds_api_key(package_format: str) -> bool:
    """Whether *package_format* can carry a credential at all.

    The UI asks this before offering a one-click download, so the Agent
    Plugins package is never presented with an option it would silently drop.
    """
    return package_format in ("claude-code", "mcpb")


def build_package(
    package_format: str,
    *,
    url: str,
    platform_name: str,
    app_version: str,
    tools: list[ToolDefinition],
    api_key: str | None = None,
    key_name: str = "",
    target: PackageTarget | None = None,
) -> tuple[str, bytes]:
    """Build one installer package. Returns ``(filename, zip_bytes)``.

    *url* is the absolute MCP endpoint of *target* and *tools* the definitions
    the requesting user would actually be offered by it. *target* defaults to
    this platform's own server.

    *api_key* is the plaintext key to embed for a one-click install, and
    *key_name* its label, quoted in the README so the reader knows which key
    to revoke.  Formats that cannot carry a credential ignore both — call
    :func:`embeds_api_key` rather than assuming one was used.
    """
    entry = _BUILDERS.get(package_format)
    if entry is None:
        raise UnknownPackageFormatError(package_format)
    resolved = target or PACKAGE_TARGETS["xtm"]
    builder, filename_template = entry
    files = builder(
        url=url,
        platform_name=platform_name,
        app_version=app_version,
        tools=tools,
        api_key=api_key,
        key_name=key_name,
        target=resolved,
    )
    return filename_template.format(name=resolved.plugin_name), _zip(files)

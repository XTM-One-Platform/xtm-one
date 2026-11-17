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

"""Turn an MCP configuration document into servers this platform can create.

WHY THIS EXISTS
---------------
Everyone arriving at XTM One already has their MCP servers configured
somewhere else — in Claude Desktop, Cursor, VS Code, or a ``server.json``
published by the vendor. Retyping a command, its arguments and its
environment variable names into a form is busywork that also gets typos
wrong in ways that only surface as a failed connection later.

WHAT "AN MCP CONFIGURATION FILE" ACTUALLY IS
--------------------------------------------
The MCP specification (2026-07-28) defines the *protocol* — JSON-RPC over
two standard transports, ``stdio`` and ``streamable-http`` — and deliberately
says nothing about how a client stores the servers it connects to. There is
therefore no single spec-defined config file to implement against. Two
documents are what people actually have, and both are accepted here:

1. **The client config object** — ``{"mcpServers": {...}}``, as written by
   Claude Desktop, Cursor and Codex; VS Code spells the same thing
   ``{"servers": {...}}`` and tags each entry with ``type``. A de-facto
   convention rather than a standard, but it is the file on everyone's disk.

2. **``server.json``** — the schema behind the official MCP registry
   (``static.modelcontextprotocol.io/schemas/.../server.schema.json``), which
   describes a server as ``packages[]`` (how to run it locally) and
   ``remotes[]`` (where to reach it hosted). This is what a vendor publishes.

Both are normalised to the same candidate shape, so the caller never learns
which one it was handed.

WHAT THIS MODULE WILL NOT DO
----------------------------
It never invents a credential. ``server.json`` describes environment
variables including which are secret, and a client config may contain real
tokens; the first gives key names with no values, the second gives values.
Key names are imported either way, values only when the document actually
carried one, and anything still needed is reported in
``missing_secrets`` so the UI can say so rather than creating a server that
fails on first use. Placeholder values a vendor wrote as documentation
(``<your-token>``, ``YOUR_API_KEY``) are treated as absent — importing them
would produce a server that looks configured and is not.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# The spec's transport identifiers are hyphenated (``streamable-http``); this
# platform's column has always used an underscore. Translated at the boundary
# so the difference never leaks past this module.
_SPEC_TRANSPORT_TO_INTERNAL = {
    "stdio": "stdio",
    "streamable-http": "streamable_http",
    "streamable_http": "streamable_http",
    "sse": "sse",
    # VS Code writes "http" for what the spec calls streamable-http.
    "http": "streamable_http",
}

# How a package registry is normally launched, when the document does not say.
# ``runtimeHint`` is authoritative when present — this is only the fallback.
_REGISTRY_DEFAULT_RUNTIME = {
    "npm": "npx",
    "pypi": "uvx",
    "oci": "docker",
    "nuget": "dnx",
}

# Registries whose artefacts are not a command line at all: an .mcpb bundle is
# a file a client installs, not something that can be spawned with arguments.
_UNSUPPORTED_REGISTRIES = {"mcpb"}

# Strings a vendor writes to mean "put your own value here". Importing one
# produces a server that looks configured, connects, and fails — worse than an
# obviously empty field, so they are dropped and reported as missing instead.
_PLACEHOLDER_VALUE = re.compile(
    r"""^(
        <.*>                      # <your-token>
        | \{\{.*\}\}              # {{TOKEN}}
        | \$\{[A-Z_][A-Z0-9_]*\}  # ${TOKEN}
        | (?:YOUR|MY|INSERT|REPLACE|CHANGE)[-_ ].*
        | (?:xxx+|\.\.\.|TODO|TBD|CHANGEME|REPLACE_ME)
        | .*(?:-here|_here)
    )$""",
    re.VERBOSE | re.IGNORECASE,
)

#: ``server.json`` substitutes ``{name}`` in a value from the input's
#: ``variables`` block, so a single-brace template is a slot for a value the
#: user still has to supply — not the value. Caught anywhere in the string
#: rather than anchored, because the real shape is a fragment:
#: ``"Bearer {ipinfo_token}"`` is how the registry's own entries are written.
_TEMPLATE_VARIABLE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")

MAX_DOCUMENT_BYTES = 1_000_000
MAX_SERVERS_PER_DOCUMENT = 100


@dataclass
class ImportCandidate:
    """One server found in the document, in this platform's own vocabulary."""

    name: str
    transport: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # Env/header keys the document declared as needed but supplied no value
    # for. Surfaced so the UI can tell the user what is still missing.
    missing_secrets: list[str] = field(default_factory=list)
    # Where this entry came from, for a preview that has to explain itself.
    source: str = ""


@dataclass
class ImportPreview:
    """Everything found, plus what could not be used and why."""

    format: str  # "mcp_servers" | "server_json" | ""
    candidates: list[ImportCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ConfigImportError(ValueError):
    """The document cannot be read as an MCP configuration at all."""


def _as_list(value: Any) -> list[Any]:
    """``value`` when it is a list; nothing when the document put a scalar there."""
    return value if isinstance(value, list) else []


def _is_placeholder(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return True
    stripped = value.strip()
    return bool(_PLACEHOLDER_VALUE.match(stripped) or _TEMPLATE_VARIABLE.search(stripped))


def _clean_str_map(raw: Any) -> tuple[dict[str, str], list[str]]:
    """Split a ``{key: value}`` block into usable values and missing keys.

    A key whose value is a placeholder is still imported — as a key with no
    value — because knowing *which* variables a server needs is most of what
    makes the form fillable.
    """
    values: dict[str, str] = {}
    missing: list[str] = []
    if not isinstance(raw, dict):
        return values, missing
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if _is_placeholder(value):
            missing.append(key)
        else:
            values[key] = str(value)
    return values, missing


def _header_list_to_map(raw: Any) -> tuple[dict[str, str], list[str]]:
    """``server.json`` carries headers as a list of KeyValueInput, not a map."""
    values: dict[str, str] = {}
    missing: list[str] = []
    if not isinstance(raw, list):
        return values, missing
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        value = item.get("value", item.get("default"))
        if _is_placeholder(value):
            missing.append(name)
        else:
            values[name] = str(value)
    return values, missing


def _env_list_to_map(raw: Any) -> tuple[dict[str, str], list[str]]:
    """``server.json`` environmentVariables → env map + what has no value.

    Only variables the document marks required, or that carry a real value,
    are reported missing: an optional variable with no default is not
    something the user has to supply.
    """
    values: dict[str, str] = {}
    missing: list[str] = []
    if not isinstance(raw, list):
        return values, missing
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        value = item.get("value", item.get("default"))
        if _is_placeholder(value):
            if item.get("isRequired") or item.get("isSecret"):
                missing.append(name)
        else:
            values[name] = str(value)
    return values, missing


def _arguments_to_argv(raw: Any) -> list[str]:
    """``server.json`` Argument[] → the argv fragment it stands for.

    A positional argument contributes its value; a named one contributes its
    flag, and its value too when the document fixes one. An argument with
    neither is documentation for something the user must supply by hand, so it
    is left out rather than emitted as an empty string that would shift every
    argument after it.
    """
    argv: list[str] = []
    if not isinstance(raw, list):
        return argv
    for item in raw:
        if not isinstance(item, dict):
            continue
        value = item.get("value", item.get("default"))
        name = item.get("name")
        if item.get("type") == "named" or (name and item.get("type") != "positional"):
            if not isinstance(name, str) or not name.strip():
                continue
            argv.append(name)
            if isinstance(value, str) and value.strip() and not _is_placeholder(value):
                argv.append(value)
            continue
        if isinstance(value, str) and value.strip():
            argv.append(value)
    return argv


# ---------------------------------------------------------------------------
# Format 1 — the client config object
# ---------------------------------------------------------------------------


def _parse_client_config(block: dict[str, Any], key: str) -> ImportPreview:
    preview = ImportPreview(format="mcp_servers")
    for name, entry in block.items():
        if len(preview.candidates) >= MAX_SERVERS_PER_DOCUMENT:
            preview.warnings.append(f"Stopped after {MAX_SERVERS_PER_DOCUMENT} servers; the rest were ignored.")
            break
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(entry, dict):
            preview.warnings.append(f"'{name}' is not a server object — ignored.")
            continue
        # A disabled entry is still imported, but as a disabled server would be
        # a surprise in a list the user just asked to fill, it is skipped and
        # said so out loud.
        if entry.get("disabled") is True:
            preview.warnings.append(f"'{name}' is disabled in the file — ignored.")
            continue

        declared = entry.get("type") or entry.get("transport")
        url = entry.get("url")
        command = entry.get("command")

        # `type` wins when present; otherwise the shape decides, which is how
        # every client that omits it reads the same file.
        if isinstance(declared, str) and declared.lower() in _SPEC_TRANSPORT_TO_INTERNAL:
            transport = _SPEC_TRANSPORT_TO_INTERNAL[declared.lower()]
        elif isinstance(url, str) and url.strip():
            transport = "streamable_http"
        elif isinstance(command, str) and command.strip():
            transport = "stdio"
        else:
            preview.warnings.append(f"'{name}' has neither a command nor a url — ignored.")
            continue

        candidate = ImportCandidate(name=name.strip(), transport=transport, source=f"{key}.{name}")
        if transport == "stdio":
            if not isinstance(command, str) or not command.strip():
                preview.warnings.append(f"'{name}' is stdio but has no command — ignored.")
                continue
            candidate.command = command.strip()
            raw_args = entry.get("args")
            if isinstance(raw_args, list):
                candidate.args = [str(a) for a in raw_args if isinstance(a, (str, int, float))]
            env, missing = _clean_str_map(entry.get("env"))
            candidate.env, candidate.missing_secrets = env, missing
        else:
            if not isinstance(url, str) or not url.strip():
                preview.warnings.append(f"'{name}' is a remote server but has no url — ignored.")
                continue
            candidate.url = url.strip()
            headers, missing = _clean_str_map(entry.get("headers"))
            candidate.headers, candidate.missing_secrets = headers, missing

        preview.candidates.append(candidate)

    if not preview.candidates and not preview.warnings:
        preview.warnings.append("The file contains no MCP servers.")
    return preview


# ---------------------------------------------------------------------------
# Format 2 — server.json
# ---------------------------------------------------------------------------


def _display_name(detail: dict[str, Any]) -> str:
    """A name a person would recognise in a list.

    ``server.json`` names are reverse-DNS (``io.github.owner/thing``), which is
    an identifier, not a label. ``title`` is the human one when the document
    has it; otherwise the last path segment is closer than the full string.
    """
    title = detail.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    name = detail.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip().rsplit("/", 1)[-1]
    return "Imported MCP server"


def _package_to_candidate(pkg: dict[str, Any], label: str, warnings: list[str]) -> ImportCandidate | None:
    registry = str(pkg.get("registryType") or "").lower()
    identifier = pkg.get("identifier")
    if not isinstance(identifier, str) or not identifier.strip():
        warnings.append(f"'{label}' has a package with no identifier — ignored.")
        return None
    if registry in _UNSUPPORTED_REGISTRIES:
        warnings.append(
            f"'{label}' ships as a {registry} bundle, which is installed rather than "
            "launched — add it by hand if you need it."
        )
        return None

    raw_transport = pkg.get("transport")
    # The schema writes ``{"type": ...}``; a bare string is read as the type.
    if isinstance(raw_transport, dict):
        raw_transport = raw_transport.get("type")
    transport_type = str(raw_transport or "stdio").lower()
    transport = _SPEC_TRANSPORT_TO_INTERNAL.get(transport_type, "stdio")
    if transport != "stdio":
        # A package entry whose transport is HTTP describes a server the user
        # runs themselves and then connects to over a port this document does
        # not name. There is nothing to put in the URL field.
        warnings.append(
            f"'{label}' describes a package served over {transport_type}, which needs a "
            "URL the file does not provide — add it by hand."
        )
        return None

    runtime = pkg.get("runtimeHint") or _REGISTRY_DEFAULT_RUNTIME.get(registry)
    if not runtime:
        warnings.append(f"'{label}' uses an unknown package registry '{registry}' — ignored.")
        return None

    candidate = ImportCandidate(name=label, transport="stdio", command=str(runtime))
    argv = _arguments_to_argv(pkg.get("runtimeArguments"))
    version = pkg.get("version")
    pinned = (
        f"{identifier}@{version}"
        if isinstance(version, str) and version.strip() and registry in {"npm", "pypi"}
        else identifier
    )

    # Each ecosystem's runner wants the package named differently, and getting
    # this wrong is exactly the typo the import exists to avoid.
    if registry == "npm" and str(runtime).endswith("npx"):
        argv = ["-y", *argv, pinned]
    elif registry == "pypi" and str(runtime).endswith("uvx"):
        argv = [*argv, pinned]
    elif registry == "oci":
        env_flags: list[str] = []
        for item in _as_list(pkg.get("environmentVariables")):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                env_flags += ["-e", item["name"]]
        argv = ["run", "-i", "--rm", *env_flags, *argv, pinned]
    else:
        argv = [*argv, pinned]

    candidate.args = [*argv, *_arguments_to_argv(pkg.get("packageArguments"))]
    candidate.env, candidate.missing_secrets = _env_list_to_map(pkg.get("environmentVariables"))
    candidate.source = f"packages[{registry}:{identifier}]"
    return candidate


def _remote_to_candidate(remote: dict[str, Any], label: str) -> ImportCandidate | None:
    url = remote.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    transport = _SPEC_TRANSPORT_TO_INTERNAL.get(str(remote.get("type") or "streamable-http").lower(), "streamable_http")
    candidate = ImportCandidate(name=label, transport=transport, url=url.strip(), source="remotes[0]")
    candidate.headers, candidate.missing_secrets = _header_list_to_map(remote.get("headers"))
    return candidate


def _parse_server_json(detail: dict[str, Any]) -> ImportPreview:
    preview = ImportPreview(format="server_json")
    label = _display_name(detail)

    # A server.json usually describes the *same* server several ways — hosted
    # and as an npm package, say. Offering all of them as separate rows would
    # invite the user to create duplicates of one thing, so the hosted entry
    # wins when there is one: it needs no local runtime and is what the vendor
    # operates. The rest are reported, not silently dropped.
    remotes = [r for r in _as_list(detail.get("remotes")) if isinstance(r, dict)]
    packages = [p for p in _as_list(detail.get("packages")) if isinstance(p, dict)]

    for remote in remotes:
        candidate = _remote_to_candidate(remote, label)
        if candidate is not None:
            preview.candidates.append(candidate)
            break

    if not preview.candidates:
        for pkg in packages:
            candidate = _package_to_candidate(pkg, label, preview.warnings)
            if candidate is not None:
                preview.candidates.append(candidate)
                break
    elif packages:
        preview.warnings.append(f"'{label}' is also published as a package; the hosted server was chosen.")

    if not preview.candidates and not preview.warnings:
        preview.warnings.append(f"'{label}' lists no package or remote this platform can run.")
    return preview


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_mcp_config(raw: str) -> ImportPreview:
    """Read an MCP configuration document into importable candidates.

    Raises :class:`ConfigImportError` when the document is not JSON or is not
    a shape this understands; everything softer than that — an entry that
    cannot be used, a registry that is not a command line — comes back as a
    warning alongside whatever *could* be read, so one bad entry never costs
    the user the rest of the file.
    """
    if len(raw.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ConfigImportError("That file is too large to be an MCP configuration.")
    if not raw.strip():
        raise ConfigImportError("The file is empty.")

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigImportError(f"Not valid JSON: {exc.msg} (line {exc.lineno}).") from exc
    except RecursionError as exc:
        # Well-formed, but nested deeper than the decoder's stack.
        raise ConfigImportError("Not valid JSON: it is nested too deeply to read.") from exc
    except ValueError as exc:
        # Well-formed, but an integer longer than Python converts to a number
        # (``sys.get_int_max_str_digits()``, 4300 digits by default).
        raise ConfigImportError("Not valid JSON: it holds a number too long to read.") from exc

    if not isinstance(doc, dict):
        raise ConfigImportError("An MCP configuration must be a JSON object.")

    # The client config is checked first and by key, because a file can carry
    # both shapes (an editor config that also has a "name") and the explicit
    # container is the stronger signal of intent.
    for key in ("mcpServers", "servers"):
        block = doc.get(key)
        if isinstance(block, dict):
            return _parse_client_config(block, key)

    if "packages" in doc or "remotes" in doc:
        return _parse_server_json(doc)

    # A bare single entry — what someone gets by copying one server out of a
    # client config. Accepted because refusing it would be pedantry.
    if doc.get("command") or doc.get("url"):
        return _parse_client_config({_display_name(doc): doc}, "server")

    raise ConfigImportError(
        "No MCP servers found. Expected a 'mcpServers' object or a server.json with 'packages' or 'remotes'."
    )

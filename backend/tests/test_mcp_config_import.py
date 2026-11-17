"""Reading MCP configuration documents written by other tools.

The parser's job is to be forgiving about shape and strict about credentials:
a file with one unusable entry must still yield the rest, and a placeholder a
vendor wrote as documentation must never become a stored value.
"""

from __future__ import annotations

import json

import pytest

from app.services.mcp_config_import import (
    ConfigImportError,
    parse_mcp_config,
)


def _only(preview):
    assert len(preview.candidates) == 1, preview.warnings
    return preview.candidates[0]


# ---------------------------------------------------------------------------
# Client config — the mcpServers object
# ---------------------------------------------------------------------------


def test_reads_a_stdio_entry_from_a_claude_desktop_config():
    preview = parse_mcp_config(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-github"],
                        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_real_value"},
                    }
                }
            }
        )
    )
    assert preview.format == "mcp_servers"
    c = _only(preview)
    assert (c.name, c.transport, c.command) == ("github", "stdio", "npx")
    assert c.args == ["-y", "@modelcontextprotocol/server-github"]
    assert c.env == {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_real_value"}
    assert c.missing_secrets == []


def test_vs_code_servers_block_and_http_type_are_understood():
    """VS Code names the block differently and calls the transport "http"."""
    preview = parse_mcp_config(
        json.dumps({"servers": {"remote": {"type": "http", "url": "https://mcp.example.com/mcp"}}})
    )
    c = _only(preview)
    assert (c.transport, c.url) == ("streamable_http", "https://mcp.example.com/mcp")


def test_transport_is_inferred_from_shape_when_untyped():
    preview = parse_mcp_config(
        json.dumps(
            {
                "mcpServers": {
                    "local": {"command": "uvx", "args": ["thing"]},
                    "hosted": {"url": "https://mcp.example.com/mcp"},
                }
            }
        )
    )
    by_name = {c.name: c for c in preview.candidates}
    assert by_name["local"].transport == "stdio"
    assert by_name["hosted"].transport == "streamable_http"


@pytest.mark.parametrize(
    "placeholder",
    ["<your-token>", "YOUR_API_KEY", "${GITHUB_TOKEN}", "changeme", "xxxxx", "token-here", ""],
)
def test_placeholder_credentials_are_reported_not_stored(placeholder):
    """A documentation placeholder must not become a stored value.

    Importing it would produce a server that looks configured and fails on
    first use, which is harder to diagnose than an obviously empty field.
    """
    preview = parse_mcp_config(json.dumps({"mcpServers": {"x": {"command": "npx", "env": {"API_KEY": placeholder}}}}))
    c = _only(preview)
    assert c.env == {}
    assert c.missing_secrets == ["API_KEY"]


def test_a_server_json_template_variable_is_a_placeholder_not_a_value():
    """``{name}`` is a slot the registry fills from the input's variables.

    Caught on a real document: ipinfo's published server.json carries
    ``"Bearer {ipinfo_token}"``, which imported as a literal header would
    produce a server that authenticates with the word "{ipinfo_token}".
    """
    preview = parse_mcp_config(
        json.dumps(
            {
                "name": "io.github.ipinfo/mcp",
                "title": "IPinfo",
                "description": "d",
                "version": "1",
                "remotes": [
                    {
                        "type": "streamable-http",
                        "url": "https://mcp.ipinfo.io/",
                        "headers": [{"name": "Authorization", "value": "Bearer {ipinfo_token}"}],
                    }
                ],
            }
        )
    )
    c = _only(preview)
    assert c.headers == {}
    assert c.missing_secrets == ["Authorization"]


def test_one_broken_entry_does_not_cost_the_others():
    preview = parse_mcp_config(
        json.dumps(
            {
                "mcpServers": {
                    "good": {"command": "npx", "args": ["a"]},
                    "empty": {},
                    "disabled": {"command": "npx", "disabled": True},
                }
            }
        )
    )
    assert [c.name for c in preview.candidates] == ["good"]
    assert any("empty" in w for w in preview.warnings)
    assert any("disabled" in w for w in preview.warnings)


def test_a_bare_single_entry_is_accepted():
    """What someone gets by copying one server out of a larger config."""
    preview = parse_mcp_config(json.dumps({"command": "uvx", "args": ["falcon-mcp"]}))
    assert _only(preview).command == "uvx"


# ---------------------------------------------------------------------------
# server.json — the official registry schema
# ---------------------------------------------------------------------------


def test_npm_package_becomes_an_npx_command_line():
    preview = parse_mcp_config(
        json.dumps(
            {
                "name": "io.github.acme/thing",
                "description": "A thing",
                "version": "1.0.0",
                "packages": [
                    {
                        "registryType": "npm",
                        "identifier": "@acme/thing-mcp",
                        "version": "2.1.0",
                        "runtimeHint": "npx",
                        "transport": {"type": "stdio"},
                        "environmentVariables": [
                            {"name": "ACME_TOKEN", "isRequired": True, "isSecret": True},
                            {"name": "ACME_REGION", "value": "eu"},
                        ],
                    }
                ],
            }
        )
    )
    assert preview.format == "server_json"
    c = _only(preview)
    assert c.command == "npx"
    assert c.args == ["-y", "@acme/thing-mcp@2.1.0"]
    # A declared value is kept; a required secret with none is reported.
    assert c.env == {"ACME_REGION": "eu"}
    assert c.missing_secrets == ["ACME_TOKEN"]


def test_pypi_package_uses_uvx_and_carries_arguments():
    preview = parse_mcp_config(
        json.dumps(
            {
                "name": "io.github.acme/py",
                "description": "d",
                "version": "1",
                "packages": [
                    {
                        "registryType": "pypi",
                        "identifier": "acme-mcp",
                        "transport": {"type": "stdio"},
                        "packageArguments": [
                            {"type": "named", "name": "--mode", "value": "stdio"},
                            {"type": "positional", "value": "run"},
                        ],
                    }
                ],
            }
        )
    )
    c = _only(preview)
    assert c.command == "uvx"
    assert c.args == ["acme-mcp", "--mode", "stdio", "run"]


def test_oci_package_becomes_a_docker_run_with_env_flags():
    preview = parse_mcp_config(
        json.dumps(
            {
                "name": "io.github.acme/img",
                "description": "d",
                "version": "1",
                "packages": [
                    {
                        "registryType": "oci",
                        "identifier": "ghcr.io/acme/mcp:1.2.3",
                        "transport": {"type": "stdio"},
                        "environmentVariables": [{"name": "TOKEN", "isRequired": True}],
                    }
                ],
            }
        )
    )
    c = _only(preview)
    assert c.command == "docker"
    assert c.args == ["run", "-i", "--rm", "-e", "TOKEN", "ghcr.io/acme/mcp:1.2.3"]


def test_a_hosted_remote_is_preferred_over_a_package():
    """Both describe one server, so offering both would invite a duplicate."""
    preview = parse_mcp_config(
        json.dumps(
            {
                "name": "io.github.acme/both",
                "title": "Acme",
                "description": "d",
                "version": "1",
                "remotes": [{"type": "streamable-http", "url": "https://mcp.acme.com/mcp"}],
                "packages": [
                    {
                        "registryType": "npm",
                        "identifier": "@acme/mcp",
                        "transport": {"type": "stdio"},
                    }
                ],
            }
        )
    )
    c = _only(preview)
    assert (c.name, c.transport, c.url) == ("Acme", "streamable_http", "https://mcp.acme.com/mcp")
    assert any("also published as a package" in w for w in preview.warnings)


def test_reverse_dns_name_is_shortened_when_there_is_no_title():
    preview = parse_mcp_config(
        json.dumps(
            {
                "name": "io.github.acme/falcon-mcp",
                "description": "d",
                "version": "1",
                "remotes": [{"type": "streamable-http", "url": "https://x.example/mcp"}],
            }
        )
    )
    assert _only(preview).name == "falcon-mcp"


def test_an_mcpb_bundle_is_refused_with_a_reason():
    """A bundle is installed by a client, not spawned with arguments."""
    preview = parse_mcp_config(
        json.dumps(
            {
                "name": "io.github.acme/bundle",
                "description": "d",
                "version": "1",
                "packages": [
                    {
                        "registryType": "mcpb",
                        "identifier": "https://example.com/thing.mcpb",
                        "transport": {"type": "stdio"},
                    }
                ],
            }
        )
    )
    assert preview.candidates == []
    assert any("bundle" in w for w in preview.warnings)


# ---------------------------------------------------------------------------
# Documents that are not configurations at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ("", "empty"),
        ("not json", "valid JSON"),
        ("[1, 2, 3]", "JSON object"),
        (json.dumps({"unrelated": True}), "No MCP servers found"),
    ],
)
def test_unusable_documents_say_what_was_expected(raw, fragment):
    with pytest.raises(ConfigImportError) as exc:
        parse_mcp_config(raw)
    assert fragment.lower() in str(exc.value).lower()


def test_an_oversized_document_is_refused_before_parsing():
    with pytest.raises(ConfigImportError, match="too large"):
        parse_mcp_config(json.dumps({"mcpServers": {}, "pad": "x" * 1_000_001}))


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ("[" * 200_000, "nested too deeply"),
        ('{"a":' * 200_000, "nested too deeply"),
        ("1" * 5000, "number too long"),
    ],
    ids=["deep array", "deep object", "5000-digit integer"],
)
def test_well_formed_json_this_reader_cannot_hold_is_a_config_error(raw, fragment):
    """Deep nesting and a 5000-digit integer used to escape as a 500."""
    with pytest.raises(ConfigImportError, match=fragment):
        parse_mcp_config(raw)


@pytest.mark.parametrize(
    "doc",
    [
        {"remotes": 1},
        {"packages": True},
        {"remotes": 1.5, "packages": 7},
        {"packages": [{"identifier": "x", "registryType": "npm", "transport": 3}]},
        {"packages": [{"identifier": "x", "registryType": "oci", "environmentVariables": 7}]},
    ],
)
def test_a_server_json_with_scalars_where_lists_belong_is_read_not_crashed_on(doc):
    """A scalar where the schema has a list or an object is ignored, not a crash."""
    preview = parse_mcp_config(json.dumps(doc))
    assert preview.format == "server_json"


def test_a_bare_string_package_transport_is_read_as_its_type():
    """The schema writes ``{"type": ...}``; a bare string means the same thing."""
    stdio = parse_mcp_config(
        json.dumps({"packages": [{"identifier": "@acme/mcp", "registryType": "npm", "transport": "stdio"}]})
    )
    assert _only(stdio).command == "npx"

    served = parse_mcp_config(
        json.dumps(
            {"packages": [{"identifier": "@acme/mcp", "registryType": "npm", "transport": "streamable-http"}]}
        )
    )
    assert served.candidates == []
    assert "served over streamable-http" in served.warnings[0]


def test_an_empty_mcp_servers_block_is_not_an_error():
    """A valid file that happens to hold nothing: report it, do not raise."""
    preview = parse_mcp_config(json.dumps({"mcpServers": {}}))
    assert preview.candidates == []
    assert preview.warnings == ["The file contains no MCP servers."]

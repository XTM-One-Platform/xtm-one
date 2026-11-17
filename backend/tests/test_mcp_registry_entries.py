"""The MCP Registry entries of XTM One's MCP servers.

``mcp-registry/*.server.json`` describe the servers for the official MCP
Registry. These tests hold them to what the registry accepts and to what the
backend serves, so an entry can never advertise a URL nobody answers:

- each entry validates against the vendored registry schema, pinned to the
  version its ``$schema`` names (checksum included);
- the registry's own semantic checks on a remote URL (HTTPS, not loopback,
  every ``{variable}`` declared) hold;
- names sit under the ``io.github.XTM-One-Platform`` namespace the release
  workflow's GitHub OIDC login is granted, one per file;
- the published endpoints are exactly the MCP endpoints the backend serves
  but ``/mcp/global`` (an instance key, not a person's sign-in);
- ``websiteUrl`` and the icon point at pages and files the docs publish;
- the release workflow (its parsed YAML) publishes every entry, stamped with
  the release tag's version, only on a release tag, once the repository opts
  in, behind a protected environment, with actions pinned to a commit.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from jsonschema import Draft7Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_DIR = REPO_ROOT / "mcp-registry"
SCHEMA_PATH = REGISTRY_DIR / "schema" / "server.schema.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "mcp-registry.yml"
DOCS_DIR = REPO_ROOT / "docs" / "docs"

SCHEMA_VERSION = "2025-12-11"
SCHEMA_URL = f"https://static.modelcontextprotocol.io/schemas/{SCHEMA_VERSION}/server.schema.json"
SCHEMA_SHA256 = "3fba09590c99f61735d234822279f4223fab9e300c0a81e81c91ab62a4114de0"

#: The namespace ``mcp-publisher login github-oidc`` is granted from this
#: repository: ``io.github.<repository owner>/*``.
NAMESPACE = "io.github.XTM-One-Platform/"
DOCS_BASE = "https://docs.xtmone.io/latest/"

#: The MCP endpoints a catalog may not list, and why.
UNPUBLISHED = {"/mcp/global": "authenticates with the instance's administrator key"}

_TEMPLATE_VARIABLE = re.compile(r"\{([^{}]+)\}")

ENTRY_PATHS = sorted(REGISTRY_DIR.glob("*.server.json"))


def _entry(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_there_are_entries():
    assert ENTRY_PATHS, "mcp-registry/ holds no *.server.json"


def test_the_vendored_schema_is_the_pinned_one():
    raw = SCHEMA_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SCHEMA_SHA256
    assert json.loads(raw)["$id"] == SCHEMA_URL
    assert SCHEMA_SHA256 in (SCHEMA_PATH.parent / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ENTRY_PATHS, ids=lambda p: p.name)
def test_an_entry_validates_against_the_registry_schema(path):
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(Draft7Validator(schema).iter_errors(_entry(path)), key=str)
    assert not errors, "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)


@pytest.mark.parametrize("path", ENTRY_PATHS, ids=lambda p: p.name)
def test_an_entry_names_the_schema_version_vendored(path):
    assert _entry(path)["$schema"] == SCHEMA_URL


@pytest.mark.parametrize("path", ENTRY_PATHS, ids=lambda p: p.name)
def test_an_entry_is_named_after_its_file_in_the_granted_namespace(path):
    entry = _entry(path)
    assert entry["name"] == NAMESPACE + path.name.removesuffix(".server.json")
    # The release workflow writes the version; the repository never carries one.
    assert entry["version"] == "0.0.0"
    assert entry["title"]


def test_names_are_unique():
    names = [_entry(path)["name"] for path in ENTRY_PATHS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("path", ENTRY_PATHS, ids=lambda p: p.name)
def test_a_remote_passes_the_registry_semantic_checks(path):
    (remote,) = _entry(path)["remotes"]
    assert remote["type"] == "streamable-http"
    url, variables = remote["url"], remote.get("variables", {})
    used = set(_TEMPLATE_VARIABLE.findall(url))
    # IsValidTemplatedURL: every {variable} is declared.
    assert used and used <= set(variables), url
    # IsValidRemoteURL: once filled in, an HTTPS URL that is not loopback.
    filled = _TEMPLATE_VARIABLE.sub("placeholder.example", url)
    parts = urlsplit(filled)
    assert parts.scheme == "https"
    assert parts.hostname not in ("localhost", "127.0.0.1") and not (parts.hostname or "").endswith(".localhost")
    for name in used:
        assert variables[name]["isRequired"] is True
        assert "isSecret" not in variables[name]


#: The one variable every entry asks for. Namespaced: a client merging the
#: inputs of several servers must not mistake it for another server's ``host``.
HOST_VARIABLE = "xtm_one_host"


def test_every_entry_asks_for_the_host_the_same_way():
    """A client merging equal inputs asks for the host once for all five."""
    declared = {json.dumps(_entry(path)["remotes"][0]["variables"], sort_keys=True) for path in ENTRY_PATHS}
    assert len(declared) == 1
    for path in ENTRY_PATHS:
        remote = _entry(path)["remotes"][0]
        assert set(remote["variables"]) == {HOST_VARIABLE}
        assert remote["url"].startswith(f"https://{{{HOST_VARIABLE}}}/mcp/")


def _served_mcp_endpoints() -> set[str]:
    from app.main import application

    paths = set()
    for route in application.routes:
        path = getattr(route, "path", "")
        if (
            path.startswith("/mcp/")
            and "POST" in (getattr(route, "methods", None) or set())
            and path.count("/") == 2
            and not path.startswith("/mcp/oauth")
        ):
            paths.add(path)
    return paths


def test_the_entries_publish_every_endpoint_but_the_instance_key_one():
    published = {urlsplit(_entry(path)["remotes"][0]["url"]).path for path in ENTRY_PATHS}
    served = _served_mcp_endpoints()
    assert "/mcp/xtm" in served and "/mcp/user" in served
    assert published == served - set(UNPUBLISHED), (
        "An MCP endpoint is served without a registry entry (or an entry names one "
        "nobody serves): add mcp-registry/<name>.server.json, or say in UNPUBLISHED "
        "why a catalog must not list it"
    )


@pytest.mark.parametrize("path", ENTRY_PATHS, ids=lambda p: p.name)
def test_the_website_and_the_icon_are_published_by_the_docs(path):
    entry = _entry(path)
    page = entry["websiteUrl"]
    assert page.startswith(DOCS_BASE) and page.endswith("/")
    assert (DOCS_DIR / (page.removeprefix(DOCS_BASE).rstrip("/") + ".md")).is_file(), page
    for icon in entry["icons"]:
        assert icon["src"].startswith(DOCS_BASE)
        assert (DOCS_DIR / icon["src"].removeprefix(DOCS_BASE)).is_file(), icon["src"]


# ── The release workflow ─────────────────────────────────────────────────────
#
# The registry grants io.github.<owner>/* to the OIDC token of any workflow of
# the owner: these hold the job to what keeps that grant to reviewed releases.


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def publish_job(workflow) -> dict:
    return workflow["jobs"]["publish"]


def _run(job: dict, name: str) -> str:
    (step,) = [step for step in job["steps"] if step.get("name") == name]
    return step["run"]


def test_only_the_publish_job_may_mint_an_oidc_token(workflow, publish_job):
    assert workflow["permissions"] == {"contents": "read"}
    assert publish_job["permissions"] == {"contents": "read", "id-token": "write"}
    assert list(workflow["jobs"]) == ["publish"]


def test_the_job_is_opt_in_tag_only_and_behind_a_protected_environment(workflow, publish_job):
    condition = " ".join(publish_job["if"].split())
    assert "vars.MCP_REGISTRY_PUBLISH == 'true'" in condition
    assert "github.ref_type == 'tag'" in condition
    assert "!contains(github.ref_name, '-lts')" in condition
    assert publish_job["environment"] == "mcp-registry"
    # PyYAML reads a bare ``on:`` key as True. A release tag, or a dispatch ON
    # one; no input, so no free-form version.
    triggers = workflow[True]
    assert triggers["push"] == {"tags": ["*.*.*"]}
    assert triggers["workflow_dispatch"] in ({}, None)


def test_every_action_is_pinned_to_a_commit_and_keeps_no_token(publish_job):
    uses = [step for step in publish_job["steps"] if "uses" in step]
    assert uses
    for step in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    (checkout,) = [step for step in uses if step["uses"].startswith("actions/checkout@")]
    assert checkout["with"] == {"sparse-checkout": "mcp-registry", "persist-credentials": False}


def test_no_expression_is_expanded_inside_a_script(publish_job):
    for step in publish_job["steps"]:
        assert "${{" not in step.get("run", ""), step["name"]


def test_a_release_never_drops_a_pending_one(workflow):
    assert workflow["concurrency"] == {"group": "${{ github.workflow }}-${{ github.ref }}", "cancel-in-progress": False}


def test_the_version_is_the_tags_and_stamped_into_every_entry(publish_job):
    assert 'version="$GITHUB_REF_NAME"' in _run(publish_job, "Resolve the version")
    stamp = _run(publish_job, "Stamp the version into every entry")
    assert "mcp-registry/*.server.json" in stamp and ".version = $version" in stamp


def test_publishing_logs_in_per_entry_and_publishes_only_on_a_404(publish_job):
    script = _run(publish_job, "Publish every entry not published at this version yet")
    assert "for entry in mcp-registry/*.server.json" in script
    # The registry token lives 5 minutes: a login per entry.
    loop = script[script.index("for entry") :]
    assert loop.index("./mcp-publisher login github-oidc") < loop.index("./mcp-publisher publish")
    # 200 = already there, 404 = publish, anything else stops the job.
    assert re.search(r"200\)\s.*continue", script, re.S)
    assert re.search(r"404\) ;;", script)
    assert re.search(r"\*\)\s.*exit 1", script, re.S)


def test_the_publisher_is_checked_against_its_pinned_checksum(workflow, publish_job):
    install = _run(publish_job, "Install mcp-publisher")
    assert re.fullmatch(r"[0-9a-f]{64}", workflow["env"]["MCP_PUBLISHER_SHA256"])
    assert "sha256sum --check --strict" in install
    assert "--retry" in install and "--max-time" in install

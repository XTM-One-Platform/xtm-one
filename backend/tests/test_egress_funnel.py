"""Tripwire: outbound HTTP of a routable resource goes through ``egress_client``.

An integration, a remote MCP server or a flow HTTP node may be routed through a
relay group (``app/core/egress.py``). That only holds if every client those
code paths build is an ``egress_client``: one bare ``httpx.AsyncClient(`` in an
integration would send a routed resource's requests straight from the platform
- to an address the platform cannot reach, or worse, one it can.

The scan covers the whole application. In the routed trees no bare client is
allowed; anywhere else a module that builds one must be listed below with the
reason it is platform-internal (never routed), and an entry that no longer
builds one fails too, so the list cannot rot.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

# The code paths a resource's route applies to.
ROUTED_TREES = (
    "integrations",
    "tools/mcp_client.py",
    "services/flow_runner.py",
    "services/flow_triggers.py",
    "services/event_poller.py",
    "services/kb_sync.py",
    "services/mcp_oauth_discovery.py",
    "services/token_refresh.py",
    "services/declarative_http_executor.py",
    "services/integration_tools.py",
    "services/autonomous.py",
    "api/oauth_callbacks.py",
    "api/v1/agent_skills.py",
    "api/v1/integrations.py",
    "api/v1/integration_elements.py",
    "api/v1/integration_testing.py",
    "api/v1/integration_github_webhooks.py",
    "api/v1/integration_notifications.py",
)

# Modules that build their own client on purpose, and why they are never routed.
PLATFORM_INTERNAL = {
    "a2a/client/delegation.py": "A2A peers are reached from the platform (allow-listed, SSRF-checked)",
    "a2a/client/registration.py": "A2A agent-card discovery, from the platform",
    "a2a/client/probe.py": "A2A Test Connection mints the partner's token from the platform, as registration does",
    "a2a/push.py": "A2A push notifications to registered webhooks, SSRF-checked",
    "api/auth_sso.py": "the platform's own SSO (OIDC discovery, token, userinfo)",
    "api/v1/channel_bots.py": "channel bot credentials checked against Slack / Teams / Telegram APIs",
    "api/v1/datasets.py": "dataset import from the public Hugging Face API",
    "api/v1/hooks.py": "platform event hooks the administrator registered",
    "api/v1/llm_proxy.py": "the LLM proxy forwards to the platform's own model providers",
    "api/v1/mcp_servers.py": "MCP configuration import from a public URL, SSRF-checked",
    "api/v1/settings.py": "LLM provider validation, against the platform's model providers",
    "channels/discord.py": "the Discord channel adapter (platform-hosted bot)",
    "channels/google_chat.py": "the Google Chat channel adapter (platform-hosted bot)",
    "channels/router.py": "inbound channel attachments, fetched from the channel's own CDN",
    "channels/slack.py": "the Slack channel adapter (platform-hosted bot)",
    "channels/teams.py": "the Teams channel adapter (platform-hosted bot)",
    "channels/telegram.py": "the Telegram channel adapter (platform-hosted bot)",
    "channels/whatsapp.py": "the WhatsApp channel adapter (platform-hosted bot)",
    "core/pinned_request.py": "pinned_client: web_fetch and pinned_fetch reach the public web, pinned to the checked address",
    "llm/custom_openai_provider.py": "model providers are configured platform-wide",
    "llm/elevenlabs_provider.py": "model providers are configured platform-wide",
    "llm/gateway_provider.py": "model providers are configured platform-wide",
    "llm/gemini_provider.py": "model providers are configured platform-wide (its speech routes)",
    "llm/kling_provider.py": "model providers are configured platform-wide",
    "llm/runway_provider.py": "model providers are configured platform-wide",
    "llm/seedance_provider.py": "model providers are configured platform-wide",
    "llm/tei_embedding_provider.py": "model providers are configured platform-wide",
    "services/autonomous_pentest.py": "registered OpenAEV platforms, reached at their registered API URL",
    "services/builtin_tools/image_assets.py": "fetch_image_asset reaches the public web (SSRF-checked)",
    "services/ctem_cases_source.py": "registered platforms, reached at their registered API URL (CTEM Cases, #11545)",
    "services/ctem_cve_software.py": "registered OpenCTI, reached at its registered API URL with the recorder's access (#2171)",
    "services/ctem_dependency_matches.py": "registered OpenCTI, reached at its registered API URL (#1331)",
    "services/ctem_metrics_collector.py": "registered platforms, reached at their registered API URL",
    "services/ctem_threat_package_live.py": "registered OpenCTI, reached at its registered API URL with the acting user's access (#1547)",
    "services/ctem_threat_software.py": (
        "registered OpenCTI, reached at its registered API URL with the recorder's access (#2523)"
    ),
    "services/github_token_installations.py": "GitHub App installation tokens from api.github.com",
    "services/health/platform_checks.py": "Platform Health version check: the public XTM One release list on api.github.com",
    "services/mcp_oauth_clients.py": (
        "Client ID Metadata Documents of MCP clients signing in to this platform's authorization server, SSRF-checked"
    ),
    "services/mobile_push.py": "Web Push to the browser push services of linked phones, host allow-listed, SSRF-checked",
    "services/notify.py": "notifications through the platform's channel bots",
    "services/nova_rule_sync.py": "Nova rules from their public repository",
    "services/orchestration_adapters/autonomous.py": "registered OpenAEV platforms (run callbacks)",
    "services/relay_transport.py": "the relay diagnostic client, routed by its own transport",
    "services/sandbox_workspace/kube.py": (
        "the Kubernetes API server of the platform's own cluster (agentic labs); a relay would see its service-account token"
    ),
    "services/sandbox_workspace/licences.py": (
        "Filigran's licensing server issuing the agentic labs' ci licences; a relay would see its API-user token"
    ),
    "services/sandbox_workspace/preview.py": (
        "previews of started sandbox services on the operator's own OpenSandbox proxy; a relay would see its API key"
    ),
    "services/google_chat_decision_cards.py": "Google Chat API calls (spaces.messages.create and patch, spaces.findDirectMessage) of the platform-hosted Chat app",
    "services/slack_decision_cards.py": "Slack Web API calls (chat.update, views.open) of the platform-hosted Slack bot",
    "services/slack_events.py": "Slack identity lookups for the platform-hosted Slack bot",
    "services/telemetry.py": "Filigran telemetry",
    "services/tracing/clickhouse_client.py": "the platform's own trace store",
    "services/tracing/price_loader.py": "the public model price list",
    "services/vault/azure.py": "the operator-declared Azure Key Vault and identity endpoints; a relay would see every secret",
    "services/vault/gcp.py": "Google Secret Manager with the platform's workload identity; a relay would see every secret",
    "services/vault/hashicorp.py": "the operator-declared HashiCorp Vault; a relay would see its token and every secret",
    "services/vault/onepassword.py": "the operator-declared 1Password Connect server; a relay would see its token and every secret",
    "services/web_fetch.py": "fetch_url_bytes reaches API documents from the platform, pinned to the checked address (guarded_transport)",
    "services/web_search.py": "web search providers' public APIs",
}

_CLIENT_ATTRS = frozenset(
    {"AsyncClient", "Client", "get", "post", "put", "patch", "delete", "request", "stream", "head", "options"}
)


def _client_calls(path: Path) -> list[int]:
    """Lines building an httpx client or calling a module-level httpx shortcut."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases: set[str] = set()
    direct: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name == "httpx":
                    aliases.add(name.asname or "httpx")
        elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
            for name in node.names:
                if name.name in _CLIENT_ATTRS:
                    direct.add(name.asname or name.name)
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in aliases
            and func.attr in _CLIENT_ATTRS
        ) or (isinstance(func, ast.Name) and func.id in direct):
            lines.append(node.lineno)
    return lines


def _modules() -> dict[str, Path]:
    return {path.relative_to(APP).as_posix(): path for path in sorted(APP.rglob("*.py"))}


def _routed(relative: str) -> bool:
    return any(relative == tree or relative.startswith(f"{tree}/") for tree in ROUTED_TREES)


def test_routed_code_builds_no_bare_http_client():
    offenders = {
        relative: lines
        for relative, path in _modules().items()
        if _routed(relative) and (lines := _client_calls(path))
    }
    assert not offenders, (
        "Outbound HTTP in a routable code path must use app.core.egress.egress_client "
        f"(a drop-in for httpx.AsyncClient), so a relay route applies: {offenders}"
    )


def test_every_other_client_is_platform_internal_and_says_why():
    builders = {
        relative
        for relative, path in _modules().items()
        if not _routed(relative) and relative != "core/egress.py" and _client_calls(path)
    }
    unlisted = sorted(builders - PLATFORM_INTERNAL.keys())
    assert not unlisted, (
        "These modules build an httpx client outside the egress funnel. Use "
        "app.core.egress.egress_client if a resource's relay route should apply, or add "
        f"the module to PLATFORM_INTERNAL with the reason it is never routed: {unlisted}"
    )
    stale = sorted(PLATFORM_INTERNAL.keys() - builders)
    assert not stale, f"These PLATFORM_INTERNAL entries no longer build a client; remove them: {stale}"


def test_every_routed_tree_exists():
    for tree in ROUTED_TREES:
        assert (APP / tree).exists(), f"{tree} is listed as routed but does not exist"


def test_the_scanner_sees_every_spelling_of_a_client(tmp_path: Path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import httpx\n"
        "import httpx as _h\n"
        "from httpx import AsyncClient as AC\n"
        "a = httpx.AsyncClient()\n"
        "b = _h.AsyncClient()\n"
        "c = AC()\n"
        "d = httpx.get('https://x')\n"
        "e = httpx.Timeout(5)\n",
        encoding="utf-8",
    )
    assert _client_calls(sample) == [4, 5, 6, 7]

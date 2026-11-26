# SPDX-License-Identifier: Apache-2.0

"""Integration connectivity testing – provider-specific health checks.

Helper module (not a router) imported by ``integrations.py``.

Exports:
    CONNECTIVITY_TIMEOUT       – default HTTP timeout for connectivity checks
    test_provider_connectivity – verify an integration and report what it found

Two verification contracts live here. The default one is the ``checks`` table
below: one authenticated HTTP call per provider, the status code is the
verdict. Providers whose health is *internal state* rather than a reachable
endpoint (no API to call, often no credential at all) instead register a probe
with :mod:`app.integrations.connectivity`; a registered probe owns the entire
verdict and short-circuits everything in this module.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import quote

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.connectivity import ProbeContext, dispatch_connectivity
from app.integrations.providers.anyrun import ANYRUN_API, build_anyrun_headers
from app.integrations.providers.feedly import FEEDLY_API, build_feedly_headers
from app.integrations.providers.github import GITHUB_API
from app.integrations.providers.google import GMAIL_API
from app.integrations.providers.greynoise import (
    GREYNOISE_API,
    build_greynoise_headers,
)
from app.integrations.providers.microsoft import GRAPH_API
from app.integrations.providers.notion import NOTION_API, NOTION_VERSION
from app.integrations.providers.securityscorecard import (
    SECURITYSCORECARD_API,
    build_securityscorecard_headers,
)
from app.integrations.providers.slack import SLACK_API
from app.integrations.providers.vulncheck import (
    VULNCHECK_API,
    build_vulncheck_headers,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.integration import Integration

logger = logging.getLogger(__name__)

CONNECTIVITY_TIMEOUT = 15  # seconds


def defender_audience_report(name: str, results: list[tuple[str, bool, str]]) -> dict[str, str]:
    """One verdict over the Defender audiences, each reported on its own (#2386).

    *results* holds (audience, ok, detail). All ok: ok. Some ok: error naming
    the failing audience, the working one said to work (its tools are usable).
    """
    lines = [f"{audience}: {'OK' if ok else detail}" for audience, ok, detail in results]
    if all(ok for _, ok, _ in results):
        return {"status": "ok", "message": f"{name} is connected. " + " | ".join(lines)}
    return {"status": "error", "message": f"{name}: " + " | ".join(lines)}


async def _defender_delegated_test(check: tuple[str, dict[str, str]], holder: str, name: str) -> dict[str, str]:
    """Delegated Defender: the Graph token on the incidents endpoint, then a
    fresh token for the Defender for Endpoint audience (#2386).

    The MDE audience has its own admin consent; a grant that covers Graph only
    used to pass this test and fail every MDE tool with "re-authorize".
    """
    import httpx

    from app.integrations.providers.defender import _MDE_SCOPE, token_failure_message
    from app.services.integration_tools import redeem_azure_delegated_audience

    url, headers = check
    try:
        async with egress_client(timeout=CONNECTIVITY_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            graph = ("Microsoft Graph", True, "")
        elif resp.status_code == 401:
            graph = ("Microsoft Graph", False, "authentication failed, the token may be expired or revoked")
        elif resp.status_code == 403:
            graph = (
                "Microsoft Graph",
                False,
                "access denied, SecurityIncident.Read.All (or ReadWrite) is not granted",
            )
        else:
            graph = ("Microsoft Graph", False, f"HTTP {resp.status_code}")
    except httpx.HTTPError as exc:
        graph = ("Microsoft Graph", False, f"unreachable ({format_exception_message(exc)})")
    mde_token, failure = await redeem_azure_delegated_audience(holder, _MDE_SCOPE)
    if mde_token:
        mde = ("Defender for Endpoint", True, "")
    else:
        detail = token_failure_message(_MDE_SCOPE, failure, delegated=True).removeprefix("Error: ")
        mde = ("Defender for Endpoint", False, detail)
    return defender_audience_report(name, [graph, mde])


async def test_provider_connectivity(
    provider: str,
    # None only for a probe-backed provider, which short-circuits every code
    # path below that would dereference it.
    token: str | None,
    extra: dict,
    name: str,
    *,
    db: AsyncSession | None = None,
    integration: Integration | None = None,
    caller_can_manage: bool = False,
    acting_user_id: str | None = None,
    token_holder: str | None = None,
) -> dict:
    """Verify an integration and report what was actually checked.

    Returns ``{"status": "ok"|"info"|"error", "message": "..."}`` — never
    raises. ``info`` means "works, with a caveat" and only ever comes from a
    registered probe.

    ``db`` / ``integration`` carry the integration row for providers whose
    health is internal state; the test endpoint always passes them, callers
    that only have a credential (provider unit tests) can omit them.
    ``caller_can_manage`` is handed to a registered probe (``ProbeContext``).
    """
    import httpx

    # extra_config is stored as encrypted JSON and decoded with a bare
    # json.loads by the caller - a non-object document (list/scalar) must
    # degrade to "no extra config" instead of crashing the provider
    # branches' ``extra.get(...)`` calls and the never-raises contract.
    if not isinstance(extra, dict):
        extra = {}

    # A provider-registered probe (app.integrations.connectivity) owns the
    # whole verdict: it runs before credential resolution, the checks table and
    # the "appears configured" fallback, so an integration with no endpoint and
    # no credential can still be verified for real. ``None`` = no probe
    # registered, carry on with the HTTP contract below.
    probe_result = await dispatch_connectivity(
        ProbeContext(
            provider=provider,
            name=name,
            token=token,
            extra=extra,
            db=db,
            integration=integration,
            caller_can_manage=caller_can_manage,
            acting_user_id=acting_user_id,
        )
    )
    if probe_result is not None:
        return probe_result.as_response()

    checks: dict[str, tuple[str, dict[str, str]]] = {
        # provider → (url, headers)
        "google": (
            f"{GMAIL_API}/users/me/profile",
            {"Authorization": f"Bearer {token}"},
        ),
        "microsoft": (
            f"{GRAPH_API}/me",
            {"Authorization": f"Bearer {token}"},
        ),
        "github": (
            f"{GITHUB_API}/user",
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        ),
        "slack": (
            f"{SLACK_API}/auth.test",
            {"Authorization": f"Bearer {token}"},
        ),
        "slack_bot": (
            f"{SLACK_API}/auth.test",
            {"Authorization": f"Bearer {token}"},
        ),
        "notion": (
            f"{NOTION_API}/users/me",
            {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION},
        ),
        "newsapi": (
            f"https://newsapi.org/v2/top-headlines?country=us&pageSize=1&apiKey={token}",
            {},
        ),
        # GreyNoise authenticates with the API key in the "key" header;
        # GET /ping is the documented lightweight status/access check.
        "greynoise": (
            f"{GREYNOISE_API}/ping",
            build_greynoise_headers(token),
        ),
        # SecurityScorecard: the token in ``Authorization: Token``; listing the
        # portfolios is a cheap authenticated read.
        "securityscorecard": (
            f"{SECURITYSCORECARD_API}/portfolios",
            build_securityscorecard_headers(token or ""),
        ),
        # ANY.RUN authenticates with the API key in the Authorization header
        # (API-KEY prefix); GET /v1/user is a lightweight authenticated check
        # that returns the account API limits.
        "anyrun": (
            f"{ANYRUN_API}/user",
            build_anyrun_headers(token),
        ),
        "figma": (
            "https://api.figma.com/v1/me",
            {"Authorization": f"Bearer {token}"},
        ),
        # Direct LLM-query integrations — validate the API key against the
        # provider's lightweight models-list endpoint. Strip the key so a
        # pasted value with leading/trailing whitespace still validates
        # (matches the provider's _headers()).
        "anthropic": (
            "https://api.anthropic.com/v1/models?limit=1",
            {"x-api-key": token.strip(), "anthropic-version": "2023-06-01"},
        ),
        "openai": (
            "https://api.openai.com/v1/models",
            {"Authorization": f"Bearer {token.strip()}"},
        ),
        "gemini": (
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
            {"x-goog-api-key": token.strip()},
        ),
        "mistral": (
            "https://api.mistral.ai/v1/models",
            {"Authorization": f"Bearer {token.strip()}"},
        ),

        "jira": (
            # The cross-product /me endpoint validates the OAuth access token
            # (the cloud_id in extra_config is not needed just to verify auth).
            "https://api.atlassian.com/me",
            {"Authorization": f"Bearer {token}", "Accept": "application/json"},
        ),
    }

    # LinkedIn: a Page token has no member profile to read, so the
    # provider checks it on the Pages it administers instead.
    if provider == "linkedin":
        from app.integrations.providers.linkedin import check_linkedin_connectivity

        return await check_linkedin_connectivity(token, extra, name)

    # Shodan sends the API key as the "key" query parameter. Pass it via
    # params (not interpolated into the URL string) so the credential never
    # lands in a logged/returned request URL, and never echo raw exception
    # text that could embed it.
    if provider == "shodan":
        api_key = (token or "").strip()
        if not api_key:
            return {"status": "error", "message": "Missing API key"}
        try:
            async with egress_client(timeout=CONNECTIVITY_TIMEOUT) as client:
                resp = await client.get(
                    "https://api.shodan.io/api-info", params={"key": api_key}
                )
            if resp.status_code == 200:
                return {"status": "ok", "message": f"{name} is connected and responding"}
            if resp.status_code in (401, 403):
                return {
                    "status": "error",
                    "message": "Authentication failed - the API key may be invalid or revoked",
                }
            return {
                "status": "error",
                "message": f"{name} returned HTTP {resp.status_code}",
            }
        except httpx.HTTPError:
            return {"status": "error", "message": f"{name} connection failed"}

    # GIPHY: one search of one GIF. The key is the api_key query parameter,
    # passed through params (never in the URL string), and no exception
    # text is echoed, so the credential never reaches the answer.
    if provider == "giphy":
        from app.integrations.providers import giphy

        api_key = (token or "").strip()
        if not api_key:
            return {"status": "error", "message": "Missing API key"}
        try:
            async with egress_client(timeout=CONNECTIVITY_TIMEOUT) as client:
                resp = await client.get(
                    f"{giphy.BASE_URL}/gifs/search",
                    params={"q": "hello", "limit": 1, "rating": giphy.DEFAULT_RATING, giphy.KEY_PARAM: api_key},
                    headers={"Accept": "application/json"},
                )
            if resp.status_code == 200:
                return {"status": "ok", "message": f"{name} is connected and responding"}
            if resp.status_code in (401, 403):
                return {
                    "status": "error",
                    "message": "Authentication failed - the API key may be invalid or revoked",
                }
            if resp.status_code == 429:
                return {
                    "status": "error",
                    "message": "GIPHY rate limit reached - a beta key allows few calls per hour",
                }
            return {"status": "error", "message": f"{name} returned HTTP {resp.status_code}"}
        except httpx.HTTPError:
            return {"status": "error", "message": f"{name} connection failed"}

    # Brave Search: one cheap web query (count=1) with the subscription token
    # header; a 403 means the key is valid but the plan lacks web search.
    if provider == "brave_search":
        from app.integrations.providers import brave_search

        api_key = (token or "").strip()
        if not api_key:
            return {"status": "error", "message": "Missing API key"}
        try:
            async with egress_client(timeout=CONNECTIVITY_TIMEOUT) as client:
                resp = await client.get(
                    f"{brave_search.BASE_URL}/web/search",
                    params={"q": "brave search", "count": 1},
                    headers={"Accept": "application/json", brave_search.TOKEN_HEADER: api_key},
                )
            if resp.status_code == 200:
                return {"status": "ok", "message": f"{name} is connected and responding"}
            return {"status": "error", "message": brave_search.format_brave_error(resp, "run a test query")}
        except httpx.HTTPError:
            return {"status": "error", "message": f"{name} connection failed"}

    # OpenRouter: the API key, and the management key when one is set. Its
    # /models is public and proves nothing, so the API key is checked on /key,
    # which answers for the calling key only, and the management key on
    # /credits, which refuses any other key.
    if provider == "openrouter":
        from app.integrations.providers import openrouter

        api_key = (token or "").strip()
        management_key = str(extra.get(openrouter.MANAGEMENT_KEY_FIELD) or "").strip()
        if not api_key:
            return {"status": "error", "message": "Missing API key"}
        checks_done: list[str] = []
        for label, call in (
            ("API key", lambda: openrouter.get_key_info(api_key)),
            ("management key", lambda: openrouter.get_credits(management_key)),
        ):
            if label == "management key" and not management_key:
                continue
            try:
                await call()
            except openrouter.OpenRouterError as exc:
                return {
                    "status": "error",
                    "message": (
                        f"The {label} was refused - it may be invalid, revoked or expired"
                        if exc.kind in ("auth", "management_key_required")
                        else f"{name}: {exc.public_message}"
                    ),
                }
            except Exception:
                # The endpoint never raises; the reason goes to the log only.
                logger.warning("OpenRouter connectivity check of the %s failed", label, exc_info=True)
                return {"status": "error", "message": f"{name} connection failed"}
            checks_done.append(label)
        return {
            "status": "ok",
            "message": f"{name} is connected ({' + '.join(checks_done)} verified)",
        }

    # CrowdSec uses two independent x-api-key credentials: the CTI API key
    # (api_key_enc -> ``token``) and an optional Service API key
    # (extra_config.service_api_key). Validate whichever is configured - the
    # CTI probe is a freemium single-IP lookup where a 404 (IP unknown to the
    # network) still proves the key authenticated, so it needs a custom branch
    # instead of the 200-only checks table.
    if provider == "crowdsec":
        from app.integrations.providers.crowdsec import (
            validate_cti_key,
            validate_service_key,
        )

        cti_key = (token or "").strip()
        service_key = (extra.get("service_api_key") or "").strip()
        if not cti_key and not service_key:
            return {
                "status": "error",
                "message": "Missing API key - set a CTI API key and/or a Service API key",
            }
        try:
            validated: list[str] = []
            if cti_key:
                if not await validate_cti_key(cti_key, timeout=CONNECTIVITY_TIMEOUT):
                    return {
                        "status": "error",
                        "message": (
                            "CTI API authentication failed - the CTI API key "
                            "may be invalid or revoked"
                        ),
                    }
                validated.append("CTI API")
            if service_key:
                if not await validate_service_key(
                    service_key, timeout=CONNECTIVITY_TIMEOUT
                ):
                    return {
                        "status": "error",
                        "message": (
                            "Service API authentication failed - the Service "
                            "API key may be invalid, revoked, or lacking "
                            "permissions"
                        ),
                    }
                validated.append("Service API")
            return {
                "status": "ok",
                "message": f"{name} is connected ({' + '.join(validated)} verified)",
            }
        except httpx.HTTPError:
            return {"status": "error", "message": f"{name} connection failed"}

    # Providers that need base_url from extra_config
    if provider == "opencti":
        base_url = extra.get("base_url", "").rstrip("/")
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        checks["opencti"] = (
            f"{base_url}/graphql",
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    elif provider == "odoo":
        # The server version, the signed-in user and the companies it sees.
        from app.integrations.providers.odoo import OdooCredential
        from app.integrations.providers.odoo import validate_connection as odoo_validate

        ok, message = await odoo_validate(
            OdooCredential.build(
                str(extra.get("base_url") or ""),
                str(extra.get("database") or ""),
                str(extra.get("login") or ""),
                token or "",
                str(extra.get("company_ids") or ""),
            )
        )
        return {"status": "ok" if ok else "error", "message": message}
    elif provider == "metabase":
        base_url = extra.get("base_url", "").rstrip("/")
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        checks["metabase"] = (
            f"{base_url}/api/session/properties",
            {"X-Api-Key": token},
        )
    elif provider == "grafana":
        base_url = extra.get("base_url", "").rstrip("/")
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        checks["grafana"] = (
            f"{base_url}/api/health",
            {"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    elif provider == "openaev":
        base_url = extra.get("base_url", "").rstrip("/")
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        checks["openaev"] = (
            f"{base_url}/api/settings/version",
            {"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    elif provider == "opencrq":
        # Strip before rstrip so a pasted URL with surrounding whitespace
        # normalises the same way _get_token does — otherwise the Test
        # button would report a false failure for a config that works.
        base_url = extra.get("base_url", "").strip().rstrip("/")
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        # tRPC public health procedure — validates URL/reachability; the
        # bearer token is forwarded so an authenticated deployment can also
        # verify credentials.
        checks["opencrq"] = (
            f"{base_url}/trpc/health",
            {"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    elif provider == "drata":
        # Drata routes by region (us/eu/apac), not a stored base_url. Resolve the
        # v2 host and hit a cheap, always-present endpoint so the Test button
        # validates the API key + region instead of just reporting "configured".
        from app.integrations.providers.drata import resolve_base_url

        base_url = resolve_base_url(extra.get("region"))
        checks["drata"] = (
            f"{base_url}/workspaces",
            {"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    elif provider == "sentinelone":
        base_url = extra.get("base_url", "").rstrip("/")
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        # SentinelOne uses an ``ApiToken`` (not ``Bearer``) auth header. A
        # lightweight scoped GET validates both the token and console URL.
        checks["sentinelone"] = (
            f"{base_url}/web/api/v2.1/sites?limit=1",
            {"Authorization": f"ApiToken {token}"},
        )
    elif provider == "harfanglab":
        base_url = extra.get("base_url", "").strip().rstrip("/")
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        # HarfangLab uses a ``Token`` (not ``Bearer``) auth header. The version
        # endpoint validates both the token and the manager URL.
        checks["harfanglab"] = (
            f"{base_url}/api/version",
            {"Authorization": f"Token {token}", "Accept": "application/json"},
        )
    elif provider == "attackiq":
        # Strip before rstrip so a pasted tenant URL with surrounding
        # whitespace normalises the same way _get_token does — otherwise the
        # Test button would report a false failure for a config that works.
        base_url = extra.get("base_url", "").strip().rstrip("/")
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        # AttackIQ uses a "Token" (not "Bearer") auth header. A lightweight
        # scoped GET validates both the token and the tenant URL.
        checks["attackiq"] = (
            f"{base_url}/v1/assessments?page_size=1",
            {"Authorization": f"Token {token}", "Accept": "application/json"},
        )
    elif provider == "elastic_security":
        base_url = extra.get("base_url", "").rstrip("/")
        if not base_url:
            return {
                "status": "error",
                "message": "Missing base_url (Kibana URL) in extra config",
            }
        # Elastic uses an ``ApiKey`` auth header. The detection-engine
        # privileges endpoint validates both the key and the Kibana URL. Honour
        # the configured Kibana space so a non-default space (``/s/{space}``
        # prefix) is tested against the right path; encode the segment so a
        # reserved character can't alter the request path.
        space = (extra.get("space_id") or "").strip() or "default"
        space_prefix = "" if space == "default" else f"/s/{quote(space, safe='')}"
        checks["elastic_security"] = (
            f"{base_url}{space_prefix}/api/detection_engine/privileges",
            {"Authorization": f"ApiKey {token}", "kbn-xsrf": "true"},
        )
    elif provider == "defender":
        # OAuth (delegated) mode reaches here with a Graph access token —
        # validate it against the Defender XDR incidents endpoint instead of
        # the previous no-op "appears configured" fall-through. (App-only mode
        # is validated separately in the integrations test handler.)
        checks["defender"] = (
            "https://graph.microsoft.com/v1.0/security/incidents?$top=1",
            {"Authorization": f"Bearer {token}"},
        )
    elif provider == "entra":
        # OAuth (delegated) mode reaches here with a Microsoft Graph access
        # token — validate it against the organization (tenant) endpoint, which
        # only needs the broad Directory/Organization read present in every
        # Entra configuration. (App-only mode is validated separately in the
        # integrations test handler via a client-credentials token exchange.)
        checks["entra"] = (
            "https://graph.microsoft.com/v1.0/organization",
            {"Authorization": f"Bearer {token}"},
        )
    elif provider == "purview":
        # OAuth (delegated) mode reaches here with a Microsoft Graph access
        # token - validate it against /me, which works for any signed-in user
        # (User.Read) and does not require a specific Purview compliance role.
        # (App-only mode is validated separately in the integrations test handler
        # via a client-credentials token exchange.)
        checks["purview"] = (
            "https://graph.microsoft.com/v1.0/me",
            {"Authorization": f"Bearer {token}"},
        )
    elif provider == "sentinel":
        # OAuth (delegated) mode reaches here with an Azure Resource Manager
        # access token — validate it against the Sentinel incidents endpoint.
        sub = extra.get("subscription_id", "").strip()
        rg = extra.get("resource_group", "").strip()
        ws = extra.get("workspace_name", "").strip()
        if not all([sub, rg, ws]):
            return {
                "status": "error",
                "message": (
                    "Missing subscription_id, resource_group, or workspace_name "
                    "in extra config"
                ),
            }
        checks["sentinel"] = (
            (
                f"https://management.azure.com/subscriptions/{sub}/resourceGroups/"
                f"{rg}/providers/Microsoft.OperationalInsights/workspaces/{ws}"
                "/providers/Microsoft.SecurityInsights/incidents"
                "?api-version=2024-03-01&$top=1"
            ),
            {"Authorization": f"Bearer {token}"},
        )
    elif provider == "planhat":
        checks["planhat"] = (
            "https://api.planhat.com/companies?limit=1",
            {"Authorization": f"Bearer {token}"},
        )
    elif provider == "google_search":
        cx = extra.get("search_engine_id", "")
        if not cx:
            return {
                "status": "error",
                "message": "Missing search_engine_id in extra config",
            }
        checks["google_search"] = (
            f"https://www.googleapis.com/customsearch/v1?key={token}&cx={cx}&q=test&num=1",
            {},
        )
    elif provider == "perk":
        checks["perk"] = (
            "https://api.travelperk.com/travelsafe/restrictions",
            {"Authorization": f"Bearer {token}", "Api-Version": "1"},
        )
    elif provider == "mitre_atlas":
        # ATLAS has no credentials - the check fetches the (optionally
        # overridden) public dataset and validates the data format, reporting
        # the loaded release version and object counts.
        from app.integrations.providers.mitre_atlas import (
            fetch_atlas_dataset,
            resolve_atlas_source_url,
        )

        source_url = resolve_atlas_source_url((extra.get("source_url") or "").strip())
        index, atlas_err = await fetch_atlas_dataset(source_url)
        if index is None:
            return {"status": "error", "message": atlas_err or "Fetch failed"}
        return {
            "status": "ok",
            "message": (
                f"{name} loaded ATLAS release {index.version}: "
                f"{len(index.tactics)} tactics, {len(index.techniques)} "
                f"techniques, {len(index.mitigations)} mitigations, "
                f"{len(index.case_studies)} case studies"
            ),
        }
    elif provider == "mitre_attack":
        from app.integrations.providers.mitre_attack import (
            DEFAULT_SOURCE_URL,
            index_url,
            sanitize_source_url,
        )

        # ATT&CK is credential-less — verify the STIX data source is reachable
        # by fetching the small collection index (lists domains + versions).
        # Run the configured mirror through the provider's own sanitizer so
        # the test probes exactly the URL the tools will use at runtime
        # (query/fragment stripped), and surface an actionable error when the
        # value would be silently ignored (runtime falls back to the default).
        raw_source = (extra.get("source_url") or "").strip()
        source = sanitize_source_url(raw_source)
        if raw_source and not source:
            return {
                "status": "error",
                "message": (
                    "source_url is not a usable http(s) URL (userinfo and "
                    "non-http(s) schemes are rejected) — fix it, or clear the "
                    "field to use the official MITRE source"
                ),
            }
        checks["mitre_attack"] = (index_url(source or DEFAULT_SOURCE_URL), {})
    elif provider == "rss":
        # RSS feed URLs are stored in extra_config.feed_urls (newline/comma separated)
        if token.strip():
            urls = [
                u.strip()
                for u in token.replace(",", "\n").splitlines()
                if u.strip().startswith(("http://", "https://"))
            ]
            return {
                "status": "ok",
                "message": f"{name} has {len(urls)} feed URL(s) configured",
            }
        return {"status": "error", "message": "No RSS feed URLs configured"}
    elif provider == "teams_bot":
        # Teams bot uses client_id + client_secret — no simple health endpoint
        return {"status": "ok", "message": f"{name} credentials are configured"}
    elif provider == "whatsapp_bot":
        phone_id = extra.get("phone_number_id", "")
        if not phone_id:
            return {
                "status": "error",
                "message": "Missing phone_number_id in extra config",
            }
        checks["whatsapp_bot"] = (
            f"https://graph.facebook.com/v21.0/{phone_id}",
            {"Authorization": f"Bearer {token}"},
        )
    elif provider == "mattermost":
        from app.integrations.providers.mattermost import (
            build_mattermost_headers,
            mattermost_api,
        )

        api = mattermost_api(str(extra.get("base_url") or ""))
        if api is None:
            return {
                "status": "error",
                "message": (
                    "Missing or invalid base_url (the Mattermost URL, e.g. "
                    "https://chat.company.com) in extra config"
                ),
            }
        # build_mattermost_headers raises on a blank token; this function must not.
        if not (token or "").strip():
            return {"status": "error", "message": "Missing access token"}
        checks["mattermost"] = (f"{api}/users/me", build_mattermost_headers(token))
    elif provider == "zendesk":
        from app.integrations.providers.zendesk import (
            build_zendesk_headers,
            zendesk_api,
            zendesk_subdomain,
        )

        subdomain = zendesk_subdomain(extra.get("subdomain"))
        email = str(extra.get("email") or "").strip()
        if not subdomain:
            return {"status": "error", "message": "Missing or invalid subdomain in extra config"}
        if not email:
            return {"status": "error", "message": "Missing the agent email in extra config"}
        if not (token or "").strip():
            return {"status": "error", "message": "Missing API token"}
        # /users/me answers 200 to an anonymous caller; the ticket count
        # needs a valid agent credential.
        checks["zendesk"] = (
            f"{zendesk_api(subdomain)}/tickets/count.json",
            build_zendesk_headers(email, token),
        )
    elif provider == "gitlab":
        from app.integrations.providers.gitlab import build_gitlab_headers, gitlab_api_base

        api = gitlab_api_base(str(extra.get("base_url") or ""))
        if api is None:
            return {
                "status": "error",
                "message": "Invalid base_url: an http(s) URL without credentials, query or fragment",
            }
        if not (token or "").strip():
            return {"status": "error", "message": "Missing access token"}
        # GET /user names the token's user: a cheap authenticated read.
        checks["gitlab"] = (f"{api}/user", build_gitlab_headers(token))
    elif provider == "pagerduty":
        from app.integrations.providers.pagerduty import (
            build_pagerduty_headers,
            pagerduty_api,
        )

        if not (token or "").strip():
            return {"status": "error", "message": "Missing API key"}
        # GET /users?limit=1 is a cheap authenticated read in the chosen region.
        checks["pagerduty"] = (
            f"{pagerduty_api(str(extra.get('region') or ''))}/users?limit=1",
            build_pagerduty_headers(token),
        )
    elif provider == "whatsapp":
        from app.integrations.providers.whatsapp import GRAPH_API as WHATSAPP_GRAPH_API
        from app.integrations.providers.whatsapp import whatsapp_headers
        from app.integrations.providers.whatsapp_gateway import BACKEND_GATEWAY, WHAPI_BASE_URL

        if not (token or "").strip():
            return {"status": "error", "message": "Missing access token"}
        if str(extra.get("backend") or "").strip() == BACKEND_GATEWAY:
            # The linked-device gateway: its channel health answers with
            # the channel token, whatever the pairing state.
            base_url = str(extra.get("base_url") or "").strip().rstrip("/") or WHAPI_BASE_URL
            checks["whatsapp"] = (f"{base_url}/health", whatsapp_headers(token))
        else:
            phone_id = str(extra.get("phone_number_id") or "").strip()
            if not phone_id:
                return {"status": "error", "message": "Missing phone_number_id in extra config"}
            checks["whatsapp"] = (
                f"{WHATSAPP_GRAPH_API}/{phone_id}?fields=display_phone_number,verified_name",
                whatsapp_headers(token),
            )
    elif provider == "telegram_bot":
        checks["telegram_bot"] = (
            f"https://api.telegram.org/bot{token}/getMe",
            {},
        )
    elif provider == "tanium":
        base_url = extra.get("base_url", "").rstrip("/")
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        # Tanium authenticates with the API token in the `session` header
        # (not Authorization/Bearer); /api/v2/session/info validates the token.
        checks["tanium"] = (
            f"{base_url}/api/v2/session/info",
            {"session": token, "Accept": "application/json"},
        )
    elif provider == "qradar":
        from app.integrations.providers.qradar import (
            build_qradar_headers,
            qradar_api_base,
        )

        base_url = extra.get("base_url", "").rstrip("/")
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        # QRadar authenticates with the token in the `SEC` header + a Version
        # header; /api/system/about validates the token.
        checks["qradar"] = (
            f"{qradar_api_base(base_url)}/system/about",
            build_qradar_headers(token, (extra.get("api_version") or "").strip()),
        )
    elif provider == "fortisiem":
        from app.integrations.providers.fortisiem import (
            build_fortisiem_headers,
            fortisiem_api_base,
        )

        # Strip whitespace too (a pasted " https://host " must behave the same
        # here as in the runtime token assembly, which trims it).
        base_url = extra.get("base_url", "").strip().rstrip("/")
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        username = (extra.get("username") or "").strip()
        if not username:
            return {"status": "error", "message": "Missing username in extra config"}
        # FortiSIEM authenticates with HTTP Basic ('org/username' login); the
        # watch list entry count is a cheap, universally-permitted authenticated
        # probe (200 = ok, 401 = auth failed).
        checks["fortisiem"] = (
            f"{fortisiem_api_base(base_url)}/watchlist/cnt",
            build_fortisiem_headers(
                (extra.get("organization") or "").strip(), username, token
            ),
        )
    elif provider == "xsoar":
        # Cortex XSOAR needs version-aware headers (API key + optional
        # x-xdr-auth-id / advanced-key hashing) and a POST body, so handle it
        # inline rather than via the simple GET `checks` table.
        from app.integrations.providers.xsoar import (
            build_xsoar_headers,
            xsoar_api_base,
        )

        base_url = extra.get("base_url", "").rstrip("/")
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        api_key_id = (extra.get("api_key_id") or "").strip()
        auth_mode = (extra.get("auth_mode") or "standard").strip() or "standard"
        api_base = xsoar_api_base(base_url, api_key_id)
        headers = build_xsoar_headers(token, api_key_id, auth_mode)
        try:
            async with egress_client(timeout=CONNECTIVITY_TIMEOUT) as client:
                resp = await client.post(
                    f"{api_base}/incidents/search",
                    headers=headers,
                    json={"filter": {"size": 1}},
                )
            if resp.status_code == 200:
                return {"status": "ok", "message": f"{name} is connected and responding"}
            if resp.status_code in (401, 403):
                return {
                    "status": "error",
                    "message": (
                        f"{name} authentication failed (HTTP {resp.status_code}) — "
                        "check the API key, API Key ID, and auth mode."
                    ),
                }
            return {
                "status": "error",
                "message": f"{name} returned HTTP {resp.status_code}",
            }
        except httpx.HTTPError as exc:
            return {"status": "error", "message": f"{name} connection failed: {exc}"}
    elif provider == "xdr":
        # Cortex XDR uses the Cortex platform auth (API key + x-xdr-auth-id,
        # standard/advanced) and a request_data POST body, so handle it inline.
        from app.integrations.providers.xdr import build_xdr_headers, xdr_api_base

        base_url = extra.get("base_url", "").rstrip("/")
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        api_key_id = (extra.get("api_key_id") or "").strip()
        if not api_key_id:
            return {"status": "error", "message": "Missing api_key_id in extra config"}
        auth_mode = (extra.get("auth_mode") or "standard").strip() or "standard"
        headers = build_xdr_headers(token, api_key_id, auth_mode)
        try:
            async with egress_client(timeout=CONNECTIVITY_TIMEOUT) as client:
                resp = await client.post(
                    f"{xdr_api_base(base_url)}/incidents/get_incidents/",
                    headers=headers,
                    json={"request_data": {"search_from": 0, "search_to": 1}},
                )
            if resp.status_code == 200:
                return {"status": "ok", "message": f"{name} is connected and responding"}
            if resp.status_code in (401, 403):
                return {
                    "status": "error",
                    "message": (
                        f"{name} authentication failed (HTTP {resp.status_code}) — "
                        "check the API key, API Key ID, and auth mode."
                    ),
                }
            return {
                "status": "error",
                "message": f"{name} returned HTTP {resp.status_code}",
            }
        except httpx.HTTPError as exc:
            return {"status": "error", "message": f"{name} connection failed: {exc}"}
    elif provider == "docusign":
        # The credential is the provider's compound token (environment, account,
        # OAuth access token or JWT Grant key material): its validate_connection
        # reads /oauth/userinfo for the account's base URI, then the account.
        from app.integrations.providers.docusign import (
            validate_connection as docusign_validate,
        )

        ok, message = await docusign_validate(token)
        return {"status": "ok" if ok else "error", "message": message}
    elif provider == "pennylane":
        # GET /me names the company and the token's scopes; a firm token names
        # its company in the optional company_id field (X-Company-Id).
        from app.integrations.providers.pennylane import validate_connection as pennylane_validate

        company_id = str(extra.get("company_id") or "").strip()
        credential = f"{company_id}||{token or ''}" if company_id else (token or "")
        ok, message = await pennylane_validate(credential)
        return {"status": "ok" if ok else "error", "message": message}
    elif provider == "netsuite":
        # The credential is the provider's compound token (account, mode and
        # the M2M or TBA material): its validate_connection mints the token
        # (M2M) and runs one SuiteQL query over the subsidiaries.
        from app.integrations.providers.netsuite import (
            validate_connection as netsuite_validate,
        )

        ok, message = await netsuite_validate(token)
        return {"status": "ok" if ok else "error", "message": message}
    elif provider == "ashby":
        # Ashby uses HTTP Basic auth (the key as username) and POST-only RPC
        # endpoints. apiKey.info names the key and its permissions; a key that
        # lacks "API Keys Read" is still told apart from a rejected one.
        from app.integrations.providers.ashby import validate_connection as ashby_validate

        ok, message = await ashby_validate(token)
        return {"status": "ok" if ok else "error", "message": message}
    elif provider == "quotapath":
        # A read of the users list through the provider's own client.
        from app.integrations.providers.quotapath import validate_connection as quotapath_validate

        ok, message = await quotapath_validate(token or "")
        return {"status": "ok" if ok else "error", "message": message}
    elif provider == "wordsmith":
        # GET /me through the provider's own client: the region host, the
        # rate limiter and Wordsmith's error_code reading are the runtime's.
        from app.integrations.providers.wordsmith import resolve_base_url as wordsmith_base_url
        from app.integrations.providers.wordsmith import validate_connection as wordsmith_validate

        region = (extra.get("region") or "").strip()
        credential = token or ""
        compound = f"{wordsmith_base_url(region)}||{credential}" if region else credential
        ok, message = await wordsmith_validate(compound)
        return {"status": "ok" if ok else "error", "message": message}
    elif provider == "hackerone":
        # HackerOne uses HTTP Basic auth (token identifier + token value).
        # Delegate to the provider's own validate_connection so the test shares
        # the runtime credential handling (incl. stripping) and its 401/403
        # guidance, instead of reaching into a private header helper.
        from app.integrations.providers.hackerone import (
            validate_connection as hackerone_validate,
        )

        api_username = (extra.get("api_username") or "").strip()
        ok, message = await hackerone_validate(api_username, token)
        return {"status": "ok" if ok else "error", "message": message}
    elif provider == "domaintools":
        # DomainTools signs requests with HMAC-SHA256 (username + timestamp +
        # URI path, keyed by the API key). Delegate to the provider's own
        # validate_connection so the test shares the runtime signing helper
        # and its 401/403 guidance.
        from app.integrations.providers.domaintools import (
            validate_connection as domaintools_validate,
        )

        api_username = (extra.get("api_username") or "").strip()
        ok, message = await domaintools_validate(api_username, token)
        return {"status": "ok" if ok else "error", "message": message}
    elif provider == "intel471":
        # Intel 471 (Titan) uses HTTP Basic auth (account email + API key).
        # Delegate to the provider's own validate_connection so the test shares
        # the runtime credential handling (stripping, 1-record GIR probe) and
        # its 401/403 guidance.
        from app.integrations.providers.intel471 import (
            validate_connection as intel471_validate,
        )

        api_username = (extra.get("api_username") or "").strip()
        ok, message = await intel471_validate(api_username, token)
        return {"status": "ok" if ok else "error", "message": message}
    elif provider == "proofpoint":
        # Proofpoint TAP uses HTTP Basic auth (service principal + secret).
        # Delegate to the provider's own validate_connection so the test shares
        # the runtime credential handling (stripping, minimal SIEM probe) and
        # its 401/403/429 guidance.
        from app.integrations.providers.proofpoint import (
            validate_connection as proofpoint_validate,
        )

        principal = (extra.get("principal") or "").strip()
        ok, message = await proofpoint_validate(principal, token)
        return {"status": "ok" if ok else "error", "message": message}
    elif provider == "tenable":
        from app.integrations.providers.tenable import (
            build_tenable_headers,
            tenable_api_base,
        )

        access_key = (extra.get("access_key") or "").strip()
        if not access_key:
            return {"status": "error", "message": "Missing access_key in extra config"}
        # Tenable authenticates with the access/secret key pair in the X-ApiKeys
        # header; GET /session validates the keys (and the base URL).
        base_url = (extra.get("base_url") or "https://cloud.tenable.com").strip()
        checks["tenable"] = (
            f"{tenable_api_base(base_url)}/session",
            build_tenable_headers(access_key, token),
        )
    elif provider == "infoblox":
        from app.integrations.providers.infoblox import (
            build_infoblox_headers,
            infoblox_api_base,
        )

        # Infoblox CSP authenticates with the API key in the Authorization header
        # using the "Token" prefix (not Bearer); a cheap GET of the IPAM IP
        # spaces validates the key and the base URL.
        base_url = (extra.get("base_url") or "https://csp.infoblox.com").strip()
        checks["infoblox"] = (
            f"{infoblox_api_base(base_url)}/api/ddi/v1/ipam/ip_space?_limit=1",
            build_infoblox_headers(token),
        )
    elif provider == "misp":
        from app.integrations.providers.misp import (
            build_misp_headers,
            misp_api_base,
        )

        base_url = misp_api_base(extra.get("base_url", ""))
        if not base_url:
            return {
                "status": "error",
                "message": (
                    "Missing base_url (MISP instance URL, e.g. "
                    "https://misp.company.com) in extra config"
                ),
            }
        # Strip + validate the key before building the header so a blank
        # credential surfaces as a clear configuration error (MISP sends the
        # key RAW in Authorization - a blank value would just 403 opaquely).
        misp_key = (token or "").strip()
        if not misp_key:
            return {"status": "error", "message": "Missing API key"}
        # MISP authenticates with the automation key RAW in the Authorization
        # header; GET /users/view/me validates the key and the base URL.
        checks["misp"] = (
            f"{base_url}/users/view/me",
            build_misp_headers(misp_key),
        )
    elif provider == "thehive":
        from app.integrations.providers.thehive import TheHiveIntegration

        base_url = (extra.get("base_url") or "").strip()
        if not base_url:
            return {
                "status": "error",
                "message": (
                    "Missing base_url (TheHive instance URL, e.g. "
                    "https://thehive.company.com) in extra config"
                ),
            }
        thehive_key = (token or "").strip()
        if not thehive_key:
            return {"status": "error", "message": "Missing API key"}
        # TheHive authenticates with the API key as a Bearer token; GET
        # /api/v1/user/current validates the key and the base URL. Reuse the
        # provider's validate_connection (a custom branch, not the 200-only
        # checks table) because it also requires the 2xx body to be a JSON
        # object - a login/SSO/proxy page answering HTTP 200 with HTML must
        # not report "connected".
        try:
            integration = TheHiveIntegration()
            if await integration.validate_connection(f"{base_url}||{thehive_key}"):
                return {"status": "ok", "message": f"{name} is connected"}
            return {
                "status": "error",
                "message": (
                    "TheHive validation failed - check the API key and that "
                    "base_url points at the TheHive instance itself (the API "
                    "lives under /api/v1 on the same host), not at a login "
                    "page or proxy"
                ),
            }
        except httpx.HTTPError:
            return {"status": "error", "message": f"{name} connection failed"}
    elif provider == "databricks":
        from app.integrations.providers.databricks import (
            validate_connection as validate_databricks,
        )

        # Databricks authenticates either with a personal access token (Bearer)
        # or, when a service principal client ID is configured, with an OAuth
        # machine-to-machine token minted at {workspace}/oidc/v1/token. Reuse
        # the provider's validate_connection (a custom branch, not the 200-only
        # checks table) because it performs that exchange first and requires the
        # SCIM probe's 2xx body to be a JSON object - an SSO/proxy page
        # answering HTTP 200 with HTML must not report "connected".
        try:
            ok, message = await validate_databricks(
                extra.get("base_url", ""),
                extra.get("client_id", ""),
                token or "",
            )
        except httpx.HTTPError:
            return {"status": "error", "message": f"{name} connection failed"}
        if ok:
            return {"status": "ok", "message": f"{name} is connected"}
        return {"status": "error", "message": message}
    elif provider == "xtm_hub":
        from app.integrations.providers.xtm_hub import XtmHubIntegration

        # The catalog is public. Asking the Hub for its catalogs proves the
        # URL answers as an XTM Hub (a JSON GraphQL envelope listing them),
        # not merely that something answers 200; with an API token it also
        # says whom the token acts as. describe_connectivity never raises and
        # words every failure itself.
        ok, message = await XtmHubIntegration().describe_connectivity(
            extra.get("base_url"), token or ""
        )
        return {"status": "ok" if ok else "error", "message": message}
    elif provider == "supabase":
        from app.integrations.providers.supabase import (
            validate_connection as validate_supabase,
        )
        from app.integrations.providers.supabase import (
            validate_database as validate_supabase_database,
        )

        # A custom branch, not the 200-only checks table: with a project ref
        # configured the token must reach THAT project (every tool is
        # restricted to it), an invalid ref is refused before any call, and the
        # 2xx body must be the JSON the API documents. Never raises: the
        # provider's bounded fetch turns network errors into its message.
        ok, message = await validate_supabase(token or "", extra.get("project_ref", ""))
        # With a database role, its login is tested too: the data is reached
        # through it, whatever the access token can do.
        db_ok, db_message = await validate_supabase_database(extra)
        if db_message:
            message = f"{message}; {db_message}"
        return {"status": "ok" if ok and db_ok else "error", "message": message}
    elif provider == "onepassword":
        from app.integrations.providers.onepassword import OnePasswordIntegration

        # Three independent bearer-token surfaces (Connect server, Events
        # Reporting API, SCIM bridge). describe_connectivity tests every
        # CONFIGURED one and aggregates a per-surface status line, so the UI
        # "Test" button reports exactly which surface is broken. A custom
        # branch (not the 200-only checks table) because the multi-surface
        # aggregation and the JSON-body validation (a 2xx HTML login/proxy
        # page must not count as connected) don't fit the table.
        connect_url = (extra.get("base_url") or "").strip().rstrip("/")
        connect_token = (token or "").strip()
        events_url = (extra.get("events_base_url") or "").strip().rstrip("/")
        events_token = (extra.get("events_api_key") or "").strip()
        scim_url = (extra.get("scim_base_url") or "").strip().rstrip("/")
        scim_token = (extra.get("scim_api_key") or "").strip()
        if (
            not (connect_url and connect_token)
            and not events_token
            and not (scim_url and scim_token)
        ):
            return {
                "status": "error",
                "message": (
                    "Nothing to test - configure at least the Connect server "
                    "(base_url + the Connect access token in the API Key "
                    "field), an Events Reporting token (events_api_key), or "
                    "the SCIM bridge (scim_base_url + scim_api_key)"
                ),
            }
        compound = "||".join(
            [
                connect_url,
                connect_token,
                events_url,
                events_token,
                scim_url,
                scim_token,
            ]
        )
        try:
            integration = OnePasswordIntegration()
            ok, message = await integration.describe_connectivity(compound)
            return {"status": "ok" if ok else "error", "message": message}
        except httpx.HTTPError:
            return {"status": "error", "message": f"{name} connection failed"}
    elif provider == "rapid7":
        from app.integrations.providers.rapid7 import (
            build_rapid7_headers,
            rapid7_api_base,
        )

        # Rapid7 authenticates with the API key in the X-Api-Key header;
        # GET /validate confirms the key (and the region base URL).
        base_url = (
            extra.get("base_url") or "https://us.api.insight.rapid7.com"
        ).strip()
        checks["rapid7"] = (
            f"{rapid7_api_base(base_url)}/validate",
            build_rapid7_headers(token),
        )
    elif provider == "recorded_future":
        from app.integrations.providers.recorded_future import (
            build_rf_headers,
            rf_api_base,
        )

        # Recorded Future authenticates with the API token in the X-RFToken
        # header; the IP risk-rule catalog is a lightweight authenticated GET
        # that needs only basic Connect API access.
        base_url = (
            extra.get("base_url") or "https://api.recordedfuture.com"
        ).strip()
        checks["recorded_future"] = (
            f"{rf_api_base(base_url)}/v2/ip/riskrules",
            build_rf_headers(token),
        )
    elif provider == "sekoia":
        from app.integrations.providers.sekoia import (
            build_sekoia_headers,
            sekoia_api_base,
        )

        # Sekoia.io authenticates with the API key as a Bearer token;
        # GET /v1/auth/validate is the vendor's cheap authenticated probe
        # (validates the key and the regional base URL). Guard the blank-key
        # case here because build_sekoia_headers (bearer_auth_headers) raises
        # on empty tokens and this function must never raise.
        if not (token or "").strip():
            return {"status": "error", "message": "Missing API key"}
        base_url = (extra.get("base_url") or "https://api.sekoia.io").strip()
        checks["sekoia"] = (
            f"{sekoia_api_base(base_url)}/v1/auth/validate",
            build_sekoia_headers(token),
        )
    elif provider == "vulncheck":
        # VulnCheck authenticates with a single API key as Authorization: Bearer.
        # GET /v3/index is available to every key tier (community + commercial)
        # and lists the licensed indexes, making it the lightweight access check.
        # Guard the blank-key case here because build_vulncheck_headers
        # (bearer_auth_headers) raises on empty tokens and this function must
        # never raise.
        if not (token or "").strip():
            return {"status": "error", "message": "Missing API key"}
        checks["vulncheck"] = (
            f"{VULNCHECK_API}/v3/index",
            build_vulncheck_headers(token),
        )
    elif provider == "sublime":
        from app.integrations.providers.sublime import (
            build_sublime_headers,
            sublime_api_base,
        )

        # Sublime authenticates with the API key as a Bearer token;
        # GET /v0/rules?limit=1 is a cheap authenticated read (validates the
        # key and the regional base URL). Guard the blank-key case here
        # because build_sublime_headers (bearer_auth_headers) raises on empty
        # tokens and this function must never raise.
        if not (token or "").strip():
            return {"status": "error", "message": "Missing API key"}
        base_url = (
            extra.get("base_url") or "https://platform.sublime.security"
        ).strip()
        checks["sublime"] = (
            f"{sublime_api_base(base_url)}/v0/rules?limit=1",
            build_sublime_headers(token),
        )
    elif provider == "feedly":
        # Feedly authenticates with an Enterprise API access token as a
        # Bearer header; GET /v3/profile is the documented lightweight
        # identity probe. Guard the blank-token case here because
        # build_feedly_headers (bearer_auth_headers) raises on empty tokens
        # and this function must never raise.
        if not (token or "").strip():
            return {"status": "error", "message": "Missing API token"}
        checks["feedly"] = (
            f"{FEEDLY_API}/v3/profile",
            build_feedly_headers(token),
        )
    elif provider == "flashpoint":
        from app.integrations.providers.flashpoint import (
            build_flashpoint_headers,
            flashpoint_api_base,
        )

        # Flashpoint authenticates with the API token as a Bearer token; a
        # size-1 indicators query is the vendor's own connectivity probe
        # (used by the official XSOAR integration's test module). Guard the
        # blank-token case here because build_flashpoint_headers
        # (bearer_auth_headers) raises on empty tokens and this function
        # must never raise.
        if not (token or "").strip():
            return {"status": "error", "message": "Missing API token"}
        base_url = (extra.get("base_url") or "https://api.flashpoint.io").strip()
        checks["flashpoint"] = (
            f"{flashpoint_api_base(base_url)}/technical-intelligence/v2/indicators?size=1",
            build_flashpoint_headers(token),
        )
    elif provider == "virustotal":
        from app.integrations.providers.virustotal import VT_API, build_vt_headers

        # VirusTotal authenticates with the API key in the x-apikey header;
        # the popular-threat-categories catalog is a lightweight authenticated
        # GET available to every key tier that never embeds the credential in
        # the URL path (unlike GET /users/{api_key}).
        checks["virustotal"] = (
            f"{VT_API}/popular_threat_categories",
            build_vt_headers(token),
        )
    elif provider == "gti":
        from app.integrations.providers.gti import GTI_API, build_gti_headers

        # Google TI authenticates with the API key in the x-apikey header
        # (plus x-tool for gti_assessment); the popular-threat-categories
        # catalog is a lightweight authenticated GET available to every key
        # tier that never embeds the credential in the URL path.
        checks["gti"] = (
            f"{GTI_API}/popular_threat_categories",
            build_gti_headers(token),
        )
    elif provider == "qualys":
        from app.integrations.providers.qualys import (
            build_qualys_headers,
            qualys_api_base,
        )

        username = (extra.get("username") or "").strip()
        if not username:
            return {"status": "error", "message": "Missing username in extra config"}
        base_url = (extra.get("base_url") or "").strip()
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        # Qualys' classic VM API requires HTTP Basic auth + the X-Requested-With
        # header on every request; a KnowledgeBase lookup for a non-existent QID
        # is a cheap, universally-permitted authenticated probe (200 = ok,
        # 401 = auth failed). build_qualys_headers adds the mandatory header.
        checks["qualys"] = (
            f"{qualys_api_base(base_url)}/api/2.0/fo/knowledge_base/vuln/"
            "?action=list&ids=0&details=None",
            build_qualys_headers(username, token),
        )
    elif provider == "anomali":
        from app.integrations.providers.anomali import (
            anomali_api_base,
            build_anomali_headers,
        )

        username = (extra.get("username") or "").strip()
        if not username:
            return {"status": "error", "message": "Missing username in extra config"}
        # Strip + validate the key before building the header so a blank
        # credential surfaces as a clear configuration error instead of the
        # generic 401 "token may be expired or revoked" message.
        anomali_key = (token or "").strip()
        if not anomali_key:
            return {"status": "error", "message": "Missing API key"}
        # ThreatStream authenticates with the ``apikey user:key`` header; a
        # 1-record intelligence read is a cheap, universally-permitted
        # authenticated probe (200 = ok, 401 = auth failed).
        base_url = (extra.get("base_url") or "https://api.threatstream.com").strip()
        checks["anomali"] = (
            f"{anomali_api_base(base_url)}/api/v2/intelligence/?limit=1",
            build_anomali_headers(username, anomali_key),
        )
    elif provider == "socradar":
        from app.integrations.providers.socradar import (
            build_socradar_headers,
            socradar_api_base,
        )

        socradar_company_id = str(extra.get("company_id") or "").strip()
        if not socradar_company_id:
            return {
                "status": "error",
                "message": "Missing company_id in extra config",
            }
        socradar_key = (token or "").strip()
        if not socradar_key:
            return {"status": "error", "message": "Missing API key"}
        # SOCRadar authenticates with the company API key in the API-Key
        # header; a 1-record incidents v4 read (without total counting) is a
        # cheap probe that validates both the key and the Company ID
        # (200 = ok, 401 = bad key, 404 = wrong company ID). The Company ID
        # is user-controlled extra_config, so it is percent-encoded before
        # being interpolated into the path.
        base_url = (
            extra.get("base_url") or "https://platform.socradar.com/api"
        ).strip()
        checks["socradar"] = (
            f"{socradar_api_base(base_url)}"
            f"/company/{quote(socradar_company_id, safe='')}"
            "/incidents/v4?limit=1&page=1&include_total_records=false",
            build_socradar_headers(socradar_key),
        )
    elif provider == "doppel":
        from app.integrations.providers.doppel import (
            build_doppel_headers,
            doppel_api_base,
            validate_doppel_v2_connection,
        )

        base_url = (extra.get("base_url") or "").strip()
        client_id = (extra.get("client_id") or "").strip()
        client_secret = (extra.get("client_secret") or "").strip()
        if client_id and client_secret:
            # V2 mode (OAuth client credentials): delegate to the provider's
            # own validation so the test shares the runtime token-exchange
            # path (POST /oauth/token + a 1-record /v2/alerts probe).
            ok, message = await validate_doppel_v2_connection(
                base_url, client_id, client_secret
            )
            return {"status": "ok" if ok else "error", "message": message}
        # Strip + validate the key before building the headers so a blank
        # credential surfaces as a clear configuration error instead of the
        # generic 401 "token may be expired or revoked" message.
        doppel_key = (token or "").strip()
        if not doppel_key:
            return {
                "status": "error",
                "message": (
                    "Missing credentials - set either the V1 organization "
                    "API key or a V2 client_id + client_secret"
                ),
            }
        # V1: Doppel authenticates with the x-api-key (+ optional
        # x-user-api-key / x-organization-code) headers; a 1-record alerts
        # read is a cheap, universally-permitted authenticated probe
        # (200 = ok, 401 = auth failed).
        checks["doppel"] = (
            f"{doppel_api_base(base_url)}/v1/alerts?page_size=1",
            build_doppel_headers(
                doppel_key,
                extra.get("user_api_key", ""),
                extra.get("organization_code", ""),
            ),
        )
    elif provider == "sumologic":
        from app.integrations.providers.sumologic import (
            build_sumologic_headers,
            sumologic_api_base,
        )

        access_id = (extra.get("access_id") or "").strip()
        if not access_id:
            return {"status": "error", "message": "Missing access_id in extra config"}
        # Sumo Logic authenticates with the Access ID/Key pair as HTTP Basic;
        # GET /v1/collectors?limit=1 is a cheap call that validates the keys
        # (and the regional base URL).
        base_url = (extra.get("base_url") or "https://api.sumologic.com/api").strip()
        checks["sumologic"] = (
            f"{sumologic_api_base(base_url)}/v1/collectors?limit=1",
            build_sumologic_headers(access_id, token),
        )
    elif provider == "servicenow":
        from app.integrations.providers.servicenow import (
            build_servicenow_headers,
            servicenow_api_base,
        )

        base_url = servicenow_api_base(extra.get("base_url", ""))
        if not base_url:
            return {
                "status": "error",
                "message": (
                    "Missing base_url (instance URL, e.g. "
                    "https://acme.service-now.com) in extra config"
                ),
            }
        username = (extra.get("username") or "").strip()
        if not username:
            return {"status": "error", "message": "Missing username in extra config"}
        # ServiceNow authenticates with HTTP Basic; a 1-row sys_user read is a
        # cheap call that validates the credentials and the instance URL.
        checks["servicenow"] = (
            f"{base_url}/api/now/table/sys_user?sysparm_limit=1&sysparm_fields=sys_id",
            build_servicenow_headers(username, token),
        )
    elif provider == "logrhythm":
        from app.integrations.providers.logrhythm import (
            build_logrhythm_headers,
            logrhythm_api_base,
        )

        base_url = (extra.get("base_url") or "").strip()
        if not base_url:
            return {"status": "error", "message": "Missing base_url in extra config"}
        # Strip + validate the token before building the Bearer header so a
        # blank/whitespace credential surfaces as a clear configuration error
        # instead of an opaque httpx LocalProtocolError (illegal "Bearer ").
        api_token = (token or "").strip()
        if not api_token:
            return {"status": "error", "message": "Missing API token"}
        # LogRhythm authenticates with a Bearer API token; a small entities
        # list on the Administration API gateway validates the token + URL.
        # Cap the response with the count/offset pagination headers the Admin
        # API uses (matches the provider) so Test Connection never pulls every
        # entity.
        logrhythm_headers = build_logrhythm_headers(api_token)
        logrhythm_headers.update({"count": "1", "offset": "0"})
        checks["logrhythm"] = (
            f"{logrhythm_api_base(base_url)}/lr-admin-api/entities",
            logrhythm_headers,
        )
    elif provider == "abuseipdb":
        from app.integrations.providers.abuseipdb import (
            ABUSEIPDB_API,
            build_abuseipdb_headers,
        )

        # AbuseIPDB authenticates with the API key in the `Key` header; a
        # single-IP /check lookup (8.8.8.8) is a cheap authenticated probe
        # (1 call from the daily check quota of 1,000+).
        api_key = (token or "").strip()
        if not api_key:
            return {"status": "error", "message": "Missing API key"}
        checks["abuseipdb"] = (
            f"{ABUSEIPDB_API}/check?ipAddress=8.8.8.8&maxAgeInDays=30",
            build_abuseipdb_headers(api_key),
        )
    elif provider == "urlscan":
        from app.integrations.providers.urlscan import (
            URLSCAN_BASE,
            build_urlscan_headers,
        )

        # urlscan.io authenticates with the API key in the `API-Key` header;
        # GET /user/quotas/ is the documented lightweight probe. It answers
        # 200 even without a key (anonymous IP-scoped quotas) but 400 for an
        # invalid key, so guard the blank-key case before the request.
        api_key = (token or "").strip()
        if not api_key:
            return {"status": "error", "message": "Missing API key"}
        checks["urlscan"] = (
            f"{URLSCAN_BASE}/user/quotas/",
            build_urlscan_headers(api_key),
        )
    elif provider == "abusech":
        from app.integrations.providers.abusech import (
            URLHAUS_API,
            build_abusech_headers,
        )

        # abuse.ch authenticates every API with the Auth-Key header (one key
        # for MalwareBazaar, URLhaus, and ThreatFox); a 1-entry URLhaus
        # recent-URLs read is the cheapest authenticated GET probe (an
        # invalid or missing key returns HTTP 401).
        auth_key = (token or "").strip()
        if not auth_key:
            return {
                "status": "error",
                "message": (
                    "Missing API key - paste your free abuse.ch Auth-Key "
                    "from auth.abuse.ch"
                ),
            }
        checks["abusech"] = (
            f"{URLHAUS_API}/urls/recent/limit/1/",
            build_abusech_headers(auth_key),
        )
    elif provider == "ransomware_live":
        from app.integrations.providers.ransomware_live import (
            build_ransomlive_headers,
            ransomlive_status_url,
        )

        # Ransomware.live is dual-tier: a real key probes the PRO /validate
        # endpoint (X-API-KEY); the free tier probes the unauthenticated v2
        # /info endpoint. Both return HTTP 200 on success. The free tier is
        # represented by the explicit "__no_auth__" sentinel, so a blank
        # token here can only mean an api_key-mode integration missing its
        # key - fail fast instead of silently passing via the free probe.
        if not (token or "").strip():
            return {
                "status": "error",
                "message": (
                    "Missing API key - paste your Ransomware.live PRO key, "
                    "or switch the authentication mode to 'None' to use the "
                    "free v2 tier"
                ),
            }
        checks["ransomware_live"] = (
            ransomlive_status_url(token),
            build_ransomlive_headers(token),
        )
    elif provider == "vuln_intel":
        from app.integrations.providers.vuln_intel import (
            build_vulnintel_headers,
            vulnintel_status_url,
        )

        # Vulnerability Intelligence is dual-tier: a real NVD API key probes
        # the NVD CVE API (apiKey header, 1-result page - validates the key);
        # the keyless tier probes the tiny unauthenticated EPSS endpoint
        # instead of burning the strict public NVD budget (5 req/30s). The
        # keyless tier is represented by the explicit "__no_auth__" sentinel,
        # so a blank token here can only mean an api_key-mode integration
        # missing its key - fail fast instead of silently passing via the
        # EPSS probe.
        if not (token or "").strip():
            return {
                "status": "error",
                "message": (
                    "Missing API key - paste your NVD API key, or switch the "
                    "authentication mode to 'None' to use the keyless public "
                    "tier (KEV and EPSS never need a key)"
                ),
            }
        checks["vuln_intel"] = (
            vulnintel_status_url(token),
            build_vulnintel_headers(token),
        )
    elif provider == "splunk":
        from app.integrations.providers.splunk import (
            build_splunk_headers,
            splunk_api_base,
        )

        base_url = splunk_api_base(extra.get("base_url", ""))
        if not base_url:
            return {
                "status": "error",
                "message": (
                    "Missing base_url (management endpoint, e.g. "
                    "https://splunk.example.com:8089) in extra config"
                ),
            }
        # Strip + validate the secret before building the auth header so a blank
        # credential surfaces as a clear configuration error.
        secret = (token or "").strip()
        if not secret:
            return {"status": "error", "message": "Missing API token (or password)"}
        # Bearer token when no username is set; HTTP Basic (username:secret)
        # otherwise. /services/server/info is a cheap call that validates both
        # the credential and the management URL.
        username = (extra.get("username") or "").strip()
        checks["splunk"] = (
            f"{base_url}/services/server/info?output_mode=json",
            build_splunk_headers(username, secret),
        )

    if provider == "defender" and integration is not None and provider in checks:
        return await _defender_delegated_test(checks[provider], token_holder or str(integration.id), name)

    check = checks.get(provider)
    if not check:
        # No HTTP contract for this provider — fall back to a bare "configured"
        # claim. This verifies nothing, so it is the wrong answer for any
        # provider whose health is checkable at all: give it a probe
        # (app.integrations.connectivity) rather than landing here.
        return {"status": "ok", "message": f"{name} appears configured"}

    url, headers = check

    try:
        async with egress_client(timeout=CONNECTIVITY_TIMEOUT) as client:
            if provider == "opencti":
                # OpenCTI needs a POST with a simple GraphQL query
                resp = await client.post(
                    url,
                    headers=headers,
                    json={"query": "{ about { version } }"},
                )
            elif provider == "slack" or provider == "slack_bot":
                # Slack auth.test is a POST endpoint
                resp = await client.post(url, headers=headers)
            else:
                resp = await client.get(url, headers=headers)

            if resp.status_code == 200:
                # Extra validation for Slack (200 doesn't always mean success)
                if provider in ("slack", "slack_bot"):
                    body = resp.json()
                    if not body.get("ok"):
                        return {
                            "status": "error",
                            "message": f"Slack auth failed: {body.get('error', 'unknown')}",
                        }
                return {
                    "status": "ok",
                    "message": f"{name} is connected and responding",
                }
            elif resp.status_code == 401:
                return {
                    "status": "error",
                    "message": "Authentication failed — token may be expired or revoked",
                }
            elif resp.status_code == 403:
                # Microsoft /me requires User.Read which may not be granted;
                # fall back to endpoints covered by common scopes.
                if provider == "microsoft":
                    fallbacks = [
                        f"{GRAPH_API}/me/mailFolders?$top=1",  # Mail.Read
                        f"{GRAPH_API}/me/chats?$top=1",  # Chat.Read
                        f"{GRAPH_API}/me/drive/root",  # Files.Read
                    ]
                    for fb_url in fallbacks:
                        fb_resp = await client.get(fb_url, headers=headers)
                        if fb_resp.status_code == 200:
                            return {
                                "status": "ok",
                                "message": f"{name} is connected and responding",
                            }
                        if fb_resp.status_code == 401:
                            return {
                                "status": "error",
                                "message": "Authentication failed — token may be expired or revoked",
                            }
                return {
                    "status": "error",
                    "message": "Access denied — check scopes and permissions",
                }
            else:
                return {
                    "status": "error",
                    "message": f"Provider returned HTTP {resp.status_code}",
                }

    except httpx.ConnectError:
        return {
            "status": "error",
            "message": "Could not connect to provider — check the URL",
        }
    except httpx.TimeoutException:
        return {
            "status": "error",
            "message": "Connection timed out — provider may be unreachable",
        }
    except Exception as e:
        logger.warning("Integration test failed for %s (%s): %s", name, provider, format_exception_message(e))
        return {
            "status": "error",
            "message": f"Test failed: {format_exception_message(e)}",
        }

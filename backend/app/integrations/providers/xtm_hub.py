# SPDX-License-Identifier: Apache-2.0

"""XTM Hub integration - the Filigran ecosystem catalog, for agents.

XTM Hub (https://hub.filigran.io) is the entry point of the Filigran
ecosystem. It publishes ready-to-use resources for the XTM Platform modules:
OpenCTI custom dashboards, custom views, playbooks and integrations
(connectors, CSV / TAXII / RSS feeds, live streams, third-party integrations)
and OpenAEV scenarios. An agent bound to this integration finds the resource
that answers a need ("a dashboard for ransomware tracking on OpenCTI 6.8", "a
TAXII feed for phishing indicators", "an OpenAEV scenario for APT29"), reads
it in full and hands the user the Hub page that deploys it.

Tools (reads):
  - list_xtm_hub_catalogs: the resource catalogs the Hub publishes
  - search_xtm_hub_resources: search one catalog, or all of them, with the
    Hub's own filters (use case, compatible version, integration type,
    verified, deployable by the connector manager, license, entity type)
  - get_xtm_hub_resource: one resource in full, by slug or Hub link
  - list_xtm_hub_featured_resources: newest / most deployed resources
  - list_xtm_hub_use_cases: the use cases a search can be narrowed to
With an XTM Hub API token:
  - get_xtm_hub_myself: who the token acts as, in which organization, how
  - list_xtm_hub_libraries: the libraries the user reaches, private included
  - search_xtm_hub_library: the resources of one library, private included
  - list_xtm_hub_registered_platforms: the organization's registered platforms
  - download_xtm_hub_resource: a resource file, stored as a file of the run
With a read and write API token:
  - publish_xtm_hub_resource: a file of the run as a new resource of a
    dashboards, views, playbooks or scenarios library, always inactive: a
    person activates it on the Hub, whether tool approvals are on or not.
    Updating or deleting a resource stays on the Hub: its update replaces the
    resource's images and uploader organization unless every one is resent.

Authentication
--------------
The public catalog needs none: it is served by public GraphQL queries at
``{base_url}/graphql-api`` and ``auth_type: none`` assembles
``base_url||__no_auth__`` (an empty base URL means the default hub,
``XTM_HUB_DEFAULT_URL``). The other tools need a personal XTM Hub API token
(``xhp_...``, created on the Hub under Profile > API tokens, ``auth_type:
api_key``): it acts as its user in ONE organization, read only or read /
write, and is sent as ``Authorization: Bearer`` by those tools only, over
HTTPS (or to a Hub on this machine), once the Hub advertises
``API_TOKENS``. The public tools
never send it. A token belongs to the Hub it was made on: changing the
integration's XTM Hub URL forgets it (``api/v1/integrations.py``).

Hardening
---------
Resources are published by the community, so every Hub answer is third-party
data. A request never follows a redirect nor reads proxy settings from the
environment, its body is read as a stream up to ``_MAX_RESPONSE_BYTES`` raw
bytes (``Accept-Encoding: identity``; a compressed answer is refused) and must
be JSON carrying a GraphQL envelope, and an error handed to the agent is one
of a few fixed sentences - the precise reason is only logged. Hub-authored
text reaches the model inside a ``wrap_untrusted`` block, and the resource
page link is built here from the configured base URL and percent-encoded
slugs, never read from an answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.core.config import settings
from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import (
    deliver_file,
    fetch_file,
    file_content_type,
    read_pipe_file,
    with_extension,
)
from app.integrations.http import PLATFORM_USER_AGENT, parse_compound_token
from app.integrations.providers.xtm_hub_api import build_api_tools
from app.services.content_safety import wrap_untrusted
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "XTM Hub"
_SOURCE = "integration:xtm_hub"

_GRAPHQL_PATH = "/graphql-api"
#: Where the Hub's web front serves one resource page.
_RESOURCE_PAGE_PATH = "/cybersecurity-solutions"

_TIMEOUT_SECONDS = 20
#: A page of 50 resources with their descriptions stays well under this.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_LIMIT = 50
#: ``newestDocuments`` / ``mostDeployedDocuments`` answer at most 20.
_MAX_FEATURED = 20
#: Per catalog when one search covers every catalog.
_MAX_PER_CATALOG = 10
_MAX_SHORT_TEXT = 300
_MAX_DESCRIPTION = 8000

#: The catalogs the Hub publishes, by their slug (the one in their page URL),
#: with what they hold and the module they are for.
CATALOGS: dict[str, tuple[str, str]] = {
    "opencti-custom-dashboards": ("OpenCTI custom dashboards", "opencti"),
    "opencti-custom-views": ("OpenCTI custom views", "opencti"),
    "opencti-playbooks": ("OpenCTI playbooks", "opencti"),
    "opencti-integrations": (
        "OpenCTI integrations: connectors, CSV / TAXII / RSS feeds, live streams "
        "and third-party integrations",
        "opencti",
    ),
    "openaev-scenarios": ("OpenAEV scenarios", "openaev"),
}
_INTEGRATIONS_CATALOG = "opencti-integrations"
_CUSTOM_VIEWS_CATALOG = "opencti-custom-views"

#: GraphQL type -> (label, catalog slug). The catalog of a resource listed
#: across catalogs (newest, most deployed) is read from its type.
_TYPES: dict[str, tuple[str, str]] = {
    "CustomDashboard": ("OpenCTI custom dashboard", "opencti-custom-dashboards"),
    "CustomView": ("OpenCTI custom view", "opencti-custom-views"),
    "OpenCTIPlaybook": ("OpenCTI playbook", "opencti-playbooks"),
    "OpenAEVScenario": ("OpenAEV scenario", "openaev-scenarios"),
    "Connector": ("OpenCTI connector", _INTEGRATIONS_CATALOG),
    "CsvFeed": ("OpenCTI CSV feed", _INTEGRATIONS_CATALOG),
    "TaxiiFeed": ("OpenCTI TAXII feed", _INTEGRATIONS_CATALOG),
    "RssFeed": ("OpenCTI RSS feed", _INTEGRATIONS_CATALOG),
    "Stream": ("OpenCTI live stream", _INTEGRATIONS_CATALOG),
    "ThirdPartyIntegration": ("Third-party integration", _INTEGRATIONS_CATALOG),
}

_INTEGRATION_TYPES = frozenset(
    {"connector", "csv_feed", "taxii_feed", "rss_feed", "stream", "third_party_integration"}
)
_LICENSE_TYPES = {"commercial": "Commercial", "free": "Free"}
#: order_by -> (Hub ordering, direction).
_ORDERINGS = {
    "updated_at": ("updated_at", "desc"),
    "created_at": ("created_at", "desc"),
    "name": ("name", "asc"),
}
_PRODUCTS = frozenset({"opencti", "openaev"})
_VERSION_RE = re.compile(r"^\d{1,4}(?:\.\d{1,4}){1,2}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
#: The Hub's ``DocumentStatus``: only a published resource is the library's.
_PUBLISHED = "published"
_DRAFT = "draft"
#: A Relay cursor is base64; anything else is refused before it is sent.
_CURSOR_RE = re.compile(r"^[A-Za-z0-9+/=_-]{1,200}$")
_ID_RE = re.compile(r"^[A-Za-z0-9+/=_:-]{1,200}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f  ]+")

# The three things that can go wrong, as the agent reads them. The precise
# reason (status, content type, GraphQL message) only goes to the log: a URL
# that does not answer as an XTM Hub must not become a way to read another
# service's answers.
_UNREACHABLE = (
    "Error: XTM Hub could not be reached (network error or timeout). Retry "
    "shortly; if it persists, check the integration's XTM Hub URL."
)
_UNAVAILABLE = "Error: XTM Hub is temporarily unavailable. Retry shortly."
_NOT_A_HUB = (
    "Error: the configured URL did not answer as an XTM Hub. Check the "
    f"integration's XTM Hub URL (default {settings.xtm_hub_default_url})."
)
_REFUSED = (
    "Error: XTM Hub refused the request. Check the arguments (a cursor is only "
    "valid for the search that returned it) and retry."
)
#: A Hub error code (``extensions.code``, or a message that is one): the only
#: part of a refusal the agent reads, so no free text of the answer leaks.
_HUB_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_FORBIDDEN_HINT = (
    " An API token never carries the Hub's BYPASS capability: the token's user "
    "needs the operation's own capability (for example READ_ORGANIZATION_MEMBERS)."
)
_BAD_BASE_URL = (
    "Error: the integration's XTM Hub URL is not an http(s) URL. Set it to the "
    f"XTM Hub address (e.g. {settings.xtm_hub_default_url}) or leave it empty for the "
    "default XTM Hub."
)
_NEEDS_HUB_KEY = (
    "Error: this needs an XTM Hub API token. On the XTM Hub, open Profile > API "
    "tokens and create one for the organization to act in, then set the "
    "integration's authentication to API key and paste it."
)
_BAD_HUB_KEY = (
    "Error: the integration's API key is not an XTM Hub API token (xhp_...). "
    "Create one on the XTM Hub under Profile > API tokens."
)
_HUB_KEY_REFUSED = (
    "Error: XTM Hub refused the integration's API token: it is invalid, revoked "
    "or expired, or its user left the token's organization. Create a new token "
    "on the XTM Hub (Profile > API tokens) and update the integration."
)
_READ_ONLY_HUB_KEY = (
    "Error: the integration's XTM Hub API token is read only. Create a read and "
    "write token on the XTM Hub (Profile > API tokens) to change anything."
)
_INSECURE_HUB_KEY_URL = (
    "Error: an XTM Hub API token is only sent over HTTPS, or to an XTM Hub on "
    "this machine. Set the integration's XTM Hub URL to its https:// address."
)
_NO_API_TOKENS = (
    "Error: this XTM Hub does not accept API tokens yet; only its public "
    "catalog is available."
)

#: A personal API token as the Hub mints it: ``xhp_`` and 32 random bytes.
_API_TOKEN_RE = re.compile(r"^xhp_[A-Za-z0-9_-]{43}$")
#: What the token half of ``base_url||token`` holds when there is no token.
_NO_TOKEN_MARKERS = frozenset({"", "__no_auth__", "__dummy__"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
#: What the Hub's ``settings.capabilities`` says once it accepts API tokens.
_API_TOKENS_CAPABILITY = "API_TOKENS"
#: Hub resources are JSON documents, archives or images.
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
_DOWNLOAD_PATH = "/document/get"

SUMMARY_FIELDS = """
  __typename id type name slug short_description created_at updated_at
  use_cases { id name }
  uploader { first_name last_name }
  uploader_organization { name }
  ... on CustomDashboard { product_version }
  ... on CustomView { product_version entity_types }
  ... on OpenCTIPlaybook { product_version }
  ... on OpenAEVScenario { product_version }
  ... on Integration { integration_type license_type }
  ... on Connector { product_version verified manager_supported }
  ... on ThirdPartyIntegration { product_version }
"""

DETAIL_FIELDS = (
    SUMMARY_FIELDS
    + """
  description download_number share_number
  ... on Integration { datasheet_url blogpost_url demo_url solution_categories { name } }
  ... on Connector {
    container_image source_code playbook_supported minimum_deployable_version
  }
  ... on ThirdPartyIntegration { vendor_url github_url }
  ... on CsvFeed { feed_url }
  ... on TaxiiFeed { feed_url }
  ... on RssFeed { feed_url }
  ... on Stream { feed_url }
"""
)

CATALOGS_QUERY = """
query XtmOneHubCatalogs {
  seoServiceInstances { id name slug }
}
"""

SEARCH_QUERY = (
    """
query XtmOneHubSearch(
  $slug: String!, $sid: ServiceInstanceId!, $first: Int!, $after: ID,
  $orderBy: DocumentOrdering!, $orderMode: OrderingMode!, $search: String,
  $filters: LogicalFilterInput
) {
  publicDocuments(
    slug: $slug, serviceInstanceId: $sid, first: $first, after: $after,
    orderBy: $orderBy, orderMode: $orderMode, searchTerm: $search,
    logicalFilters: $filters
  ) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges { node {"""
    + SUMMARY_FIELDS
    + """} }
  }
}
"""
)

RESOURCE_QUERY = (
    """
query XtmOneHubResource($sid: ServiceInstanceId!, $slug: String!) {
  publicDocumentBySlug(serviceInstanceId: $sid, slug: $slug) {"""
    + DETAIL_FIELDS
    + """}
}
"""
)

_FEATURED_QUERY_TEMPLATE = """
query XtmOneHubFeatured($limit: Int!, $platforms: [PlatformIdentifier!]) {
  %s(limit: $limit, platformIdentifiers: $platforms) {%s}
}
"""
NEWEST_QUERY = _FEATURED_QUERY_TEMPLATE % ("newestDocuments", SUMMARY_FIELDS)
MOST_DEPLOYED_QUERY = _FEATURED_QUERY_TEMPLATE % ("mostDeployedDocuments", SUMMARY_FIELDS)

USE_CASES_QUERY = """
query XtmOneHubUseCases($first: Int!, $after: ID, $search: String, $product: FiligranProduct) {
  useCases(
    first: $first, after: $after, orderBy: name, orderMode: asc,
    searchTerm: $search, product: $product
  ) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges { node { id name product } }
  }
}
"""

#: What the connectivity test asks: the public catalogs.
STATUS_QUERY = CATALOGS_QUERY

#: Public, asked before any token is sent: an older Hub has no such field.
CAPABILITIES_QUERY = """
query XtmOneHubCapabilities { settings { capabilities } }
"""

MYSELF_QUERY = """
query XtmOneHubMyself {
  currentApiToken { name scope organization_name expires_at last_used_at }
  me { email first_name last_name }
}
"""

LIBRARIES_QUERY = """
query XtmOneHubLibraries($first: Int!) {
  serviceInstances(first: $first, orderBy: ordering, orderMode: asc) {
    totalCount
    edges { node {
      id name slug description public organization_subscribed user_joined
      capabilities service_definition { identifier }
    } }
  }
}
"""

LIBRARY_SEARCH_QUERY = (
    """
query XtmOneHubLibrarySearch(
  $sid: ServiceInstanceId!, $first: Int!, $after: ID,
  $orderBy: DocumentOrdering!, $orderMode: OrderingMode!, $search: String
) {
  documents(
    serviceInstanceId: $sid, first: $first, after: $after, orderBy: $orderBy,
    orderMode: $orderMode, searchTerm: $search, parentsOnly: true
  ) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges { node { status file_name"""
    + SUMMARY_FIELDS
    + """} }
  }
}
"""
)

DOCUMENT_QUERY = """
query XtmOneHubDocument($sid: ServiceInstanceId!, $did: DocumentId!) {
  document(serviceInstanceId: $sid, documentId: $did) {
    id name file_name status service_instance_id
  }
}
"""

REGISTERED_PLATFORMS_QUERY = """
query XtmOneHubRegisteredPlatforms($input: RegisteredPlatformsInput!) {
  registeredPlatforms(input: $input) {
    id title url identifier version contract status last_connectivity_check
  }
}
"""

#: The Hub's ``PlatformIdentifier`` values, the filter of the registered platforms.
_PLATFORM_IDENTIFIERS = frozenset({"opencti", "openaev", "xtmone"})

LIBRARY_QUERY = """
query XtmOneHubLibrary($sid: ServiceInstanceId!) {
  serviceInstanceById(service_instance_id: $sid) {
    id name slug capabilities service_definition { identifier }
  }
}
"""

#: Who publishes: the uploader is always the token's own user.
UPLOADER_QUERY = """
query XtmOneHubUploader { me { id } }
"""

PUBLISH_MUTATION = """
mutation XtmOneHubPublish(
  $sid: ServiceInstanceId!, $input: CreateDocumentInput!,
  $metadata: [DocumentMetadata!]!, $sourceDocument: Upload
) {
  createDocument(
    serviceInstanceId: $sid, input: $input, metadata: $metadata,
    sourceDocument: $sourceDocument
  ) { id name slug status }
}
"""

#: The libraries a resource file can be published to, by their service
#: definition: each takes the module version the resource was made for, and a
#: custom view also the entity types it applies to. The integrations library
#: (connectors, feeds) takes per-type metadata this tool does not write.
_PUBLISHABLE_LIBRARIES = {
    "opencti_custom_dashboards": "OpenCTI custom dashboard",
    "opencti_custom_views": "OpenCTI custom view",
    "opencti_playbooks": "OpenCTI playbook",
    "openaev_scenarios": "OpenAEV scenario",
}
_VIEWS_LIBRARY = "opencti_custom_views"
_PUBLISH_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_ENTITY_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
#: A dashboard, view, playbook or scenario export.
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_MAX_NAME = 200
_MAX_PUBLISH_SHORT_DESCRIPTION = 300
#: The Hub publishes "{Product} {Type}: {meta_description}." as the page's meta
#: description and refuses it outside these bounds (META_DESCRIPTION_TOO_SHORT /
#: _TOO_LONG), with an en or em dash, or when it starts with the resource name.
_META_DESCRIPTION_MIN = 120
_META_DESCRIPTION_MAX = 160
#: The prefix the Hub puts before the meta description, per library.
_META_DESCRIPTION_PREFIX = {
    "opencti_custom_dashboards": "OpenCTI Dashboard: ",
    "opencti_custom_views": "OpenCTI Custom View: ",
    "opencti_playbooks": "OpenCTI Playbook: ",
    "openaev_scenarios": "OpenAEV Scenario: ",
}
_MAX_PUBLISH_DESCRIPTION = 20000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def xtm_hub_default_url() -> str:
    """The XTM Hub used when none is named: ``XTM_HUB_DEFAULT_URL``, the public one by default."""
    return settings.xtm_hub_default_url


def xtm_hub_base_url(raw: object) -> str:
    """The XTM Hub the integration talks to, without a trailing slash.

    Empty means the default hub (``xtm_hub_default_url``). ``""`` is returned for a value that is not an
    absolute http(s) URL without credentials, which the callers answer with
    ``_BAD_BASE_URL``.
    """
    if raw is None:
        return xtm_hub_default_url()
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return xtm_hub_default_url()
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    if parts.username or parts.password or parts.query or parts.fragment:
        return ""
    return value.rstrip("/")


def xtm_hub_token_url(raw: object) -> str:
    """The Hub a credential may be sent to, ``""`` when refused.

    ``xtm_hub_base_url``, and HTTPS unless the Hub runs on this machine: a
    platform token or an API token travels to that address.
    """
    base = xtm_hub_base_url(raw)
    if not base:
        return ""
    parts = urlsplit(base)
    if parts.scheme == "https" or (parts.hostname or "").lower() in _LOOPBACK_HOSTS:
        return base
    return ""


def api_token_of(token_part: str) -> tuple[str | None, str | None]:
    """``(api_token, error)`` from the token half of ``base_url||token``.

    ``(None, None)`` when the integration holds no token (``auth_type: none``
    or the catalog's introspection).
    """
    value = token_part.strip()
    if value in _NO_TOKEN_MARKERS:
        return None, None
    if not _API_TOKEN_RE.match(value):
        return None, _BAD_HUB_KEY
    return value, None


def resource_page_url(base_url: str, catalog: str, slug: str) -> str:
    """The Hub page of one resource, each segment percent-encoded."""
    return (
        f"{base_url}{_RESOURCE_PAGE_PATH}/{quote(catalog, safe='')}/{quote(slug, safe='')}"
    )


def _one_line(value: Any, limit: int = _MAX_SHORT_TEXT) -> str:
    """Hub text on one line: control characters collapsed, length-capped."""
    if value is None:
        return ""
    text = _CONTROL_RE.sub(" ", str(value))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def is_transient_error(error: str | None) -> bool:
    """Whether an ``xtm_hub_graphql()`` error is worth retrying (unreachable or unavailable Hub)."""
    return error in (_UNREACHABLE, _UNAVAILABLE)


def _log_failure(action: str, reason: str) -> None:
    logger.warning("XTM Hub %s failed: %r", action, reason[:300])


def _is_compressed(resp: httpx.Response) -> bool:
    encoding = resp.headers.get("content-encoding", "").strip().lower()
    return encoding not in ("", "identity")


async def xtm_hub_graphql(
    base_url: str,
    query: str,
    variables: dict[str, Any],
    action: str,
    *,
    api_token: str | None = None,
    platform_token: tuple[str, str] | None = None,
    upload: tuple[str, str, bytes, str] | None = None,
    uploads: list[tuple[str, str, bytes, str]] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Run one GraphQL query against the Hub; returns ``(data, error)``.

    With *api_token*, the query runs as the token's user (``Authorization:
    Bearer``). With *platform_token* ``(platform_id, token)``, it runs as that
    registered platform (``XTM-Hub-Platform-Id`` and ``XTM-Hub-Platform-Token``,
    what the Hub's ``@platform_token`` operations read). Either credential is
    only sent to a Hub ``xtm_hub_token_url`` accepts. With *upload*
    ``(variable, filename, data, content_type)``, the request is a GraphQL
    multipart request carrying that file as ``$variable``, with the
    ``Apollo-Require-Preflight`` header the Hub's CSRF prevention asks for.
    *uploads* is the same for several files, each
    ``(variable path, filename, data, content_type)`` with a path such as
    ``images.0``, whose variable the caller already set to ``None``.
    """
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": PLATFORM_USER_AGENT,
    }
    if (api_token is not None or platform_token is not None) and xtm_hub_token_url(base_url) != base_url:
        return None, _INSECURE_HUB_KEY_URL
    if api_token is not None:
        headers["Authorization"] = f"Bearer {api_token}"
    if platform_token is not None:
        platform_id, token = platform_token
        headers["XTM-Hub-Platform-Id"] = platform_id
        headers["XTM-Hub-Platform-Token"] = token
    body: dict[str, Any]
    if uploads:
        headers["Apollo-Require-Preflight"] = "true"
        body = {
            "data": {
                "operations": json.dumps({"query": query, "variables": variables}),
                "map": json.dumps({str(i): [f"variables.{path}"] for i, (path, *_rest) in enumerate(uploads)}),
            },
            "files": {
                str(i): (filename, data, content_type)
                for i, (_path, filename, data, content_type) in enumerate(uploads)
            },
        }
    elif upload is None:
        headers["Content-Type"] = "application/json"
        body = {"json": {"query": query, "variables": variables}}
    else:
        variable, filename, data, content_type = upload
        headers["Apollo-Require-Preflight"] = "true"
        operations = {"query": query, "variables": {**variables, variable: None}}
        body = {
            "data": {
                "operations": json.dumps(operations),
                "map": json.dumps({"0": [f"variables.{variable}"]}),
            },
            "files": {"0": (filename, data, content_type)},
        }
    raw = bytearray()
    try:
        async with (
            egress_client(
                timeout=_TIMEOUT_SECONDS, trust_env=False, follow_redirects=False
            ) as client,
            client.stream(
                "POST", f"{base_url}{_GRAPHQL_PATH}", headers=headers, **body
            ) as resp,
        ):
            status = resp.status_code
            if status == 429 or status >= 500:
                _log_failure(action, f"HTTP {status}")
                return None, _UNAVAILABLE
            if status == 401 and api_token is not None:
                # The Hub answers every refused token with the same 401.
                _log_failure(action, "API token refused")
                return None, _HUB_KEY_REFUSED
            # A refused operation (invalid query, a directive's or the API
            # token guard's refusal) comes back as a GraphQL error envelope.
            if not (200 <= status < 300 or status in (400, 403)):
                _log_failure(action, f"HTTP {status}")
                return None, _NOT_A_HUB
            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type not in ("application/json", "application/graphql-response+json"):
                _log_failure(action, f"content type {content_type or 'missing'}")
                return None, _NOT_A_HUB
            if _is_compressed(resp):
                _log_failure(action, "compressed answer to an identity request")
                return None, _NOT_A_HUB
            declared = resp.headers.get("content-length", "")
            # ASCII digits of a bounded length: int() refuses a 5,000-digit string.
            if (
                declared.isascii()
                and declared.isdigit()
                and len(declared) <= 20
                and int(declared) > _MAX_RESPONSE_BYTES
            ):
                _log_failure(action, f"declared {declared} bytes")
                return None, _NOT_A_HUB
            async for chunk in resp.aiter_raw():
                raw.extend(chunk)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    _log_failure(action, "answer over the size limit")
                    return None, _NOT_A_HUB
    except httpx.TimeoutException as exc:
        _log_failure(action, f"timeout: {type(exc).__name__}")
        return None, _UNREACHABLE
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        _log_failure(action, f"network error: {type(exc).__name__}")
        return None, _UNREACHABLE

    try:
        body = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _log_failure(action, "body is not JSON")
        return None, _NOT_A_HUB
    if not isinstance(body, dict):
        _log_failure(action, "body is not a JSON object")
        return None, _NOT_A_HUB
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        messages = [
            str(e.get("message") or "") for e in errors[:3] if isinstance(e, dict)
        ]
        _log_failure(action, "GraphQL errors: " + "; ".join(messages))
        codes = _error_codes(errors)
        if api_token is not None and "READ_ONLY_API_TOKEN" in codes:
            return None, _READ_ONLY_HUB_KEY
        partial = body.get("data")
        # A field the caller may not read answers null with an error beside a
        # root that still answered: the answer is kept, the refusal logged.
        if 200 <= status < 300 and isinstance(partial, dict) and any(value is not None for value in partial.values()):
            return partial, None
        return None, _refusal(codes)
    if not 200 <= status < 300:
        _log_failure(action, f"HTTP {status} without a GraphQL error")
        return None, _NOT_A_HUB
    envelope = body.get("data")
    if not isinstance(envelope, dict):
        _log_failure(action, "no GraphQL data envelope")
        return None, _NOT_A_HUB
    return envelope, None


def _error_codes(errors: list[Any]) -> list[str]:
    """The distinct Hub error codes of a GraphQL error list, at most three."""
    codes: list[str] = []
    for error in errors[:5]:
        if not isinstance(error, dict):
            continue
        extensions = error.get("extensions")
        candidates = [
            extensions.get("code") if isinstance(extensions, dict) else None,
            error.get("message"),
        ]
        for value in candidates:
            if isinstance(value, str) and _HUB_ERROR_CODE_RE.match(value) and value not in codes:
                codes.append(value)
    return codes[:3]


def _refusal(codes: list[str]) -> str:
    """The refusal the agent reads: the fixed sentence, with the Hub's codes."""
    if not codes:
        return _REFUSED
    hint = _FORBIDDEN_HINT if "FORBIDDEN_ACCESS" in codes else ""
    return f"{_REFUSED} Hub error: {', '.join(codes)}.{hint}"


def is_refusal(error: str | None) -> bool:
    """Whether *error* is a GraphQL refusal of the Hub, with or without its codes."""
    return bool(error) and str(error).startswith(_REFUSED)


def _catalog_ids(data: dict[str, Any]) -> dict[str, str]:
    """Catalog slug -> service instance id, for the catalogs this module knows."""
    out: dict[str, str] = {}
    for row in data.get("seoServiceInstances") or []:
        if not isinstance(row, dict):
            continue
        slug, sid = row.get("slug"), row.get("id")
        if isinstance(slug, str) and slug in CATALOGS and isinstance(sid, str) and sid:
            out[slug] = sid
    return out


def _list_arg(value: Any) -> list[str]:
    """A comma-separated string (or a list) as clean, de-duplicated items."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _clamp(value: Any, default: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, number))


def _type_info(node: dict[str, Any]) -> tuple[str, str | None]:
    """(label, catalog slug) of a resource, read from its GraphQL type."""
    typename = node.get("__typename")
    if isinstance(typename, str) and typename in _TYPES:
        return _TYPES[typename]
    return _one_line(node.get("type") or "resource", 60), None


def _uploader(node: dict[str, Any]) -> str:
    org = node.get("uploader_organization") or {}
    user = node.get("uploader") or {}
    org_name = _one_line(org.get("name"), 80) if isinstance(org, dict) else ""
    person = ""
    if isinstance(user, dict):
        person = _one_line(
            " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p), 80
        )
    if org_name and person:
        return f"{person} ({org_name})"
    return org_name or person


def _summary_lines(node: dict[str, Any], base_url: str) -> list[str]:
    label, catalog = _type_info(node)
    name = _one_line(node.get("name"), 160) or "(unnamed)"
    slug = node.get("slug") if isinstance(node.get("slug"), str) else ""
    lines = [f"- {name} [{label}]" + (f" - slug: {_one_line(slug, 200)}" if slug else "")]
    short = _one_line(node.get("short_description"))
    if short:
        lines.append(f"  {short}")
    facts: list[str] = []
    version = _one_line(node.get("product_version"), 40)
    if version:
        facts.append(f"version {version}")
    integration_type = _one_line(node.get("integration_type"), 40)
    if integration_type:
        facts.append(f"type {integration_type}")
    if node.get("verified") is True:
        facts.append("verified")
    if node.get("manager_supported") is True:
        facts.append("deployable by the connector manager")
    license_type = _one_line(node.get("license_type"), 40)
    if license_type:
        facts.append(f"license {license_type}")
    entity_types = node.get("entity_types")
    if isinstance(entity_types, list) and entity_types:
        facts.append("entity types " + ", ".join(_one_line(t, 60) for t in entity_types[:8]))
    use_cases = [
        _one_line(u.get("name"), 60)
        for u in node.get("use_cases") or []
        if isinstance(u, dict) and u.get("name")
    ]
    if use_cases:
        facts.append("use cases " + ", ".join(use_cases[:6]))
    # The day of an ISO timestamp, cut without an ellipsis.
    updated = _one_line(node.get("updated_at") or node.get("created_at"), 40)[:10]
    if updated:
        facts.append(f"updated {updated}")
    by = _uploader(node)
    if by:
        facts.append(f"by {by}")
    if facts:
        lines.append("  " + " | ".join(facts))
    if catalog and slug:
        lines.append(f"  Hub page: {resource_page_url(base_url, catalog, slug)}")
    return lines


def _render_list(nodes: list[dict[str, Any]], base_url: str) -> str:
    return "\n".join(line for node in nodes for line in _summary_lines(node, base_url))


def _quarantine(text: str) -> str:
    return wrap_untrusted(text, source="XTM Hub catalog (community-published resources)")


def _detail(node: dict[str, Any], base_url: str, catalog: str) -> str:
    lines = _summary_lines(node, base_url)
    if not any(line.startswith("  Hub page:") for line in lines):
        slug = node.get("slug")
        if isinstance(slug, str) and slug:
            lines.append(f"  Hub page: {resource_page_url(base_url, catalog, slug)}")
    extra: list[str] = []
    for key, label in (
        ("download_number", "downloads"),
        ("share_number", "shares"),
    ):
        value = node.get(key)
        if isinstance(value, int):
            extra.append(f"{label} {value}")
    if node.get("playbook_supported") is True:
        extra.append("usable in playbooks")
    minimum = _one_line(node.get("minimum_deployable_version"), 40)
    if minimum:
        extra.append(f"minimum deployable version {minimum}")
    categories = [
        _one_line(c.get("name"), 60)
        for c in node.get("solution_categories") or []
        if isinstance(c, dict) and c.get("name")
    ]
    if categories:
        extra.append("solution categories " + ", ".join(categories[:8]))
    if extra:
        lines.append("  " + " | ".join(extra))
    for key, label in (
        ("container_image", "Container image"),
        ("source_code", "Source code"),
        ("feed_url", "Feed URL"),
        ("vendor_url", "Vendor"),
        ("github_url", "GitHub"),
        ("datasheet_url", "Datasheet"),
        ("blogpost_url", "Blog post"),
        ("demo_url", "Demo"),
    ):
        value = _one_line(node.get(key), 500)
        if value:
            lines.append(f"  {label}: {value}")
    description = node.get("description")
    if isinstance(description, str) and description.strip():
        text = _CONTROL_RE.sub(lambda m: "\n" if "\n" in m.group() else " ", description)
        text = text.strip()
        if len(text) > _MAX_DESCRIPTION:
            text = text[:_MAX_DESCRIPTION].rstrip() + "\n[description truncated]"
        lines.append("\nDescription:\n" + text)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


class XtmHubIntegration(BaseIntegration):
    provider_name = "xtm_hub"
    display_name = "XTM Hub"
    icon = "Store"
    description = (
        "Connect the XTM Hub so agents find OpenCTI dashboards, views, playbooks "
        "and integrations and OpenAEV scenarios in the Filigran ecosystem catalog."
    )
    auth_types = ["none", "api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def describe_connectivity(
        self, raw_base_url: str | None, token: str = ""
    ) -> tuple[bool, str]:
        """``(ok, message)`` for the Test button.

        Asks the Hub for its catalogs, and with an API token whom it acts as.
        """
        base_url = xtm_hub_base_url(raw_base_url)
        if not base_url:
            return False, _BAD_BASE_URL.removeprefix("Error: ")
        api_token, err = api_token_of(token)
        if err:
            return False, err.removeprefix("Error: ")
        data, err = await xtm_hub_graphql(base_url, STATUS_QUERY, {}, "connectivity test")
        if err:
            return False, err.removeprefix("Error: ")
        found = _catalog_ids(data or {})
        if not found:
            return False, (
                "XTM Hub answered but publishes none of the catalogs XTM One "
                "searches (OpenCTI dashboards, views, playbooks, integrations, "
                "OpenAEV scenarios)."
            )
        catalogs = f"{len(found)} of {len(CATALOGS)} resource catalogs available"
        if api_token is None:
            return True, f"XTM Hub is connected: {catalogs}."
        identity, err = await _whoami(base_url, api_token)
        if err:
            return False, err.removeprefix("Error: ")
        return True, f"XTM Hub is connected: {catalogs}; {identity}."

    async def validate_connection(self, token: str) -> bool:
        base_raw, token_part = parse_compound_token(token, 2, greedy_last=True)
        ok, _message = await self.describe_connectivity(base_raw, token_part or "")
        return ok

    async def get_user_info(self, token: str) -> dict:
        base_raw, token_part = parse_compound_token(token, 2, greedy_last=True)
        api_token, err = api_token_of(token_part or "")
        base_url = xtm_hub_base_url(base_raw)
        if api_token is None or err or not base_url:
            # The public catalog knows no account.
            return {}
        myself, err = await _myself(base_url, api_token)
        if err or myself is None:
            return {}
        me = myself["me"]
        name = " ".join(
            _one_line(me.get(key), 80) for key in ("first_name", "last_name") if me.get(key)
        )
        return {"email": _one_line(me.get("email"), 200), "name": name}


async def _token_support_error(base_url: str) -> str | None:
    """``None`` when the Hub accepts API tokens, else the error to answer with.

    Asked without the token: a Hub that predates API tokens would only read
    one as an anonymous request and refuse every field.
    """
    data, err = await xtm_hub_graphql(base_url, CAPABILITIES_QUERY, {}, "read capabilities")
    if err:
        # An older Hub has no ``settings.capabilities``: a GraphQL refusal.
        return _NO_API_TOKENS if is_refusal(err) else err
    settings = (data or {}).get("settings")
    capabilities = settings.get("capabilities") if isinstance(settings, dict) else None
    if not isinstance(capabilities, list) or _API_TOKENS_CAPABILITY not in capabilities:
        return _NO_API_TOKENS
    return None


async def _myself(
    base_url: str, api_token: str
) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """``_read_myself`` once the Hub says it accepts API tokens."""
    err = await _token_support_error(base_url)
    if err:
        return None, err
    return await _read_myself(base_url, api_token)


async def _read_myself(
    base_url: str, api_token: str
) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """``({"token": ..., "me": ...}, None)`` for the token's own account."""
    data, err = await xtm_hub_graphql(
        base_url, MYSELF_QUERY, {}, "read the token's user", api_token=api_token
    )
    if err:
        return None, err
    token, me = (data or {}).get("currentApiToken"), (data or {}).get("me")
    if not isinstance(token, dict) or not isinstance(me, dict):
        _log_failure("read the token's user", "no currentApiToken or me")
        return None, _NOT_A_HUB
    return {"token": token, "me": me}, None


def _scope_label(scope: Any) -> str:
    return {
        "read": "read only",
        "read_write": "read and write",
        "admin": "read, write and admin",
    }.get(str(scope), "unknown access")


async def _whoami(base_url: str, api_token: str) -> tuple[str, str | None]:
    """One line naming the token's user, organization and access."""
    myself, err = await _myself(base_url, api_token)
    if err or myself is None:
        return "", err or _NOT_A_HUB
    token, me = myself["token"], myself["me"]
    organization = _one_line(token.get("organization_name"), 120) or "its organization"
    expires = _one_line(token.get("expires_at"), 40)[:10]
    return (
        f"the API token acts as {_one_line(me.get('email'), 200)} in "
        f"{organization}, {_scope_label(token.get('scope'))}"
        + (f", until {expires}" if expires else "")
    ), None


# ---------------------------------------------------------------------------
# Publishing (shared by the tool and the dashboards' "Share on XTM Hub")
# ---------------------------------------------------------------------------


def library_page_url(base_url: str, kind: str, library_id: str, document_id: str | None = None) -> str:
    """The Hub page of a library (and of one of its documents), each segment percent-encoded."""
    url = f"{base_url}/app/service/{quote(kind, safe='')}/{quote(library_id, safe='')}"
    return f"{url}/{quote(document_id, safe='')}" if document_id else url


async def hub_api_token(base_url: str, token_part: str) -> tuple[str, str | None]:
    """``(api_token, None)`` once the token may be sent to *base_url*, else ``("", error)``."""
    if not base_url:
        return "", _BAD_BASE_URL
    api_token, token_error = api_token_of(token_part or "")
    if token_error:
        return "", token_error
    if api_token is None:
        return "", _NEEDS_HUB_KEY
    if xtm_hub_token_url(base_url) != base_url:
        return "", _INSECURE_HUB_KEY_URL
    err = await _token_support_error(base_url)
    return ("", err) if err else (api_token, None)


async def find_publishable_library(base_url: str, api_token: str, kind: str) -> tuple[str | None, str | None]:
    """The id of the first library of *kind* the token's user may publish to (UPLOAD), or an error."""
    data, err = await xtm_hub_graphql(
        base_url, LIBRARIES_QUERY, {"first": 100}, "list libraries", api_token=api_token
    )
    if err:
        return None, err
    edges = ((data or {}).get("serviceInstances") or {}).get("edges") or []
    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            continue
        definition = node.get("service_definition")
        if (
            isinstance(definition, dict)
            and definition.get("identifier") == kind
            and isinstance(node.get("capabilities"), list)
            and "UPLOAD" in node["capabilities"]
            and isinstance(node.get("id"), str)
            and _ID_RE.match(node["id"])
        ):
            return node["id"], None
    return None, (
        f"Error: the token's user may publish to no {_PUBLISHABLE_LIBRARIES.get(kind, kind)} library "
        "on this XTM Hub (a library's UPLOAD right, in the token's organization, is needed)."
    )


def meta_description_published(kind: str, summary: str) -> str:
    """The meta description the Hub publishes for *summary* in a *kind* library."""
    text = summary.strip()
    period = "" if text.endswith(".") else "."
    return f"{_META_DESCRIPTION_PREFIX.get(kind, '')}{text}{period}"


def _meta_description_facts(kind: str, summary: str) -> str:
    """The length the Hub measured and its bounds, appended to its refusal."""
    published = meta_description_published(kind, summary)
    return (
        f" The Hub publishes '{published}' ({len(published)} characters); it takes "
        f"{_META_DESCRIPTION_MIN} to {_META_DESCRIPTION_MAX} characters with its prefix, "
        "no en or em dash, and a text that does not start with the resource name. "
        "Pass meta_description to set it apart from short_description."
    )


async def publish_hub_resource(
    base_url: str,
    api_token: str,
    *,
    library_id: str,
    name: str,
    slug: str,
    short_description: str,
    description: str,
    product_version: str,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    use_cases: list[str] | None = None,
    entity_types: list[str] | None = None,
    meta_description: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    """Publish one file as a new, inactive resource of a library.

    *meta_description* is the summary the Hub turns into the page's meta
    description; empty, the short description is sent in its place.

    ``({"id", "kind", "active"}, None)``, or ``(None, error)`` with a reason:
    the library must take this kind of resource and the token's user must hold
    its UPLOAD right; the uploader is always the token's own user, and the
    resource is never active - a person makes it visible on the XTM Hub.
    """
    types = list(entity_types or [])
    data, err = await xtm_hub_graphql(
        base_url, LIBRARY_QUERY, {"sid": library_id}, "read a library", api_token=api_token
    )
    if err:
        return None, err
    library = (data or {}).get("serviceInstanceById")
    if not isinstance(library, dict):
        return None, "Error: no such library, or the token's user cannot see it."
    definition = library.get("service_definition")
    kind = definition.get("identifier") if isinstance(definition, dict) else None
    if kind not in _PUBLISHABLE_LIBRARIES:
        return None, (
            "Error: resources are published by this tool only to an OpenCTI "
            "dashboards, views or playbooks library or an OpenAEV scenarios "
            "library."
        )
    capabilities = library.get("capabilities")
    if not isinstance(capabilities, list) or "UPLOAD" not in capabilities:
        return None, (
            "Error: the token's user may not publish to this library (the "
            "library's UPLOAD right, in the token's organization, is missing)."
        )
    if kind == _VIEWS_LIBRARY and not types:
        return None, "Error: a custom view needs entity_types, the entity types it applies to."
    if kind != _VIEWS_LIBRARY and types:
        return None, "Error: entity_types applies to custom views only."
    data, err = await xtm_hub_graphql(
        base_url, UPLOADER_QUERY, {}, "read the token's user", api_token=api_token
    )
    if err:
        return None, err
    me = (data or {}).get("me")
    uploader = me.get("id") if isinstance(me, dict) else None
    if not isinstance(uploader, str) or not _ID_RE.match(uploader):
        _log_failure("read the token's user", "no me.id")
        return None, _NOT_A_HUB
    document_input: dict[str, Any] = {
        "name": name,
        "slug": slug,
        "short_description": short_description,
        "description": description,
        # The Hub's meta description is required; the short description is
        # its default when the caller gives none.
        "meta_description": meta_description or short_description,
        # Always a draft: a person publishes it on the Hub. Tool approvals
        # are opt-in, so this is the step that keeps a human between an
        # agent (which reads Hub content) and a public catalog.
        "status": _DRAFT,
        "use_cases": list(use_cases or []),
        "solution_categories": [],
        # Never the caller's choice: the resource is the token's user's.
        "uploader_id": uploader,
    }
    if types:
        document_input["entity_types"] = types
    data, err = await xtm_hub_graphql(
        base_url,
        PUBLISH_MUTATION,
        {"sid": library_id, "input": document_input, "metadata": [{"key": "product_version", "value": product_version}]},
        "publish a resource",
        api_token=api_token,
        upload=("sourceDocument", filename or f"{slug}.json", file_bytes, content_type),
    )
    if err:
        if "META_DESCRIPTION" in err:
            err += _meta_description_facts(kind, meta_description or short_description)
        return None, err
    created = (data or {}).get("createDocument")
    if not isinstance(created, dict) or not isinstance(created.get("id"), str):
        _log_failure("publish a resource", "no createDocument")
        return None, _NOT_A_HUB
    return {"id": created["id"], "kind": kind, "active": created.get("status") == _PUBLISHED}, None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build the XTM Hub tools from ``base_url||api_token`` (or ``||__no_auth__``).

    The tool set is the same either way: a tool that needs the token says so.
    """
    base_raw, token_part = parse_compound_token(token, 2, greedy_last=True)
    base_url = xtm_hub_base_url(base_raw)
    api_token, token_error = api_token_of(token_part or "")
    persist_file_fn = _kwargs.get("persist_file_fn")
    # Catalog slug -> service instance id, read once per tool build (a run).
    catalog_cache: dict[str, str] = {}
    cache_lock = asyncio.Lock()
    # Whether the Hub accepts API tokens, asked once per tool build.
    token_support: list[str | None] = []
    token_lock = asyncio.Lock()

    async def _authed() -> tuple[str, str | None]:
        """``(api_token, None)`` once the token may be sent, else ``("", error)``."""
        if not base_url:
            return "", _BAD_BASE_URL
        if token_error:
            return "", token_error
        if api_token is None:
            return "", _NEEDS_HUB_KEY
        if xtm_hub_token_url(base_url) != base_url:
            return "", _INSECURE_HUB_KEY_URL
        async with token_lock:
            if not token_support:
                err = await _token_support_error(base_url)
                if err is not None and is_transient_error(err):
                    return "", err  # asked again on the next call
                token_support.append(err)
        err = token_support[0]
        return ("", err) if err else (api_token, None)

    async def _catalogs() -> tuple[dict[str, str], str | None]:
        if not base_url:
            return {}, _BAD_BASE_URL
        async with cache_lock:
            if catalog_cache:
                return dict(catalog_cache), None
            data, err = await xtm_hub_graphql(base_url, CATALOGS_QUERY, {}, "list catalogs")
            if err:
                return {}, err
            catalog_cache.update(_catalog_ids(data or {}))
            if not catalog_cache:
                _log_failure("list catalogs", "none of the known catalogs published")
                return {}, _NOT_A_HUB
            return dict(catalog_cache), None

    def _catalog_arg(value: Any, *, allow_all: bool) -> tuple[str, str | None]:
        catalog = str(value or "").strip().lower()
        if allow_all and catalog in ("", "all"):
            return "all", None
        if catalog in CATALOGS:
            return catalog, None
        return "", (
            f"Error: catalog must be one of {', '.join(CATALOGS)}"
            + (" or 'all'" if allow_all else "")
            + "."
        )

    # ── 1. Catalogs ────────────────────────────────────────────────────────

    async def list_xtm_hub_catalogs() -> str:
        """The resource catalogs the Hub publishes."""
        ids, err = await _catalogs()
        if err:
            return err
        lines = [f"XTM Hub ({base_url}) publishes {len(ids)} resource catalogs:"]
        for slug, (label, product) in CATALOGS.items():
            if slug not in ids:
                continue
            lines.append(
                f"- {slug}: {label} (for {'OpenCTI' if product == 'opencti' else 'OpenAEV'}) "
                f"- {base_url}{_RESOURCE_PAGE_PATH}/{slug}"
            )
        missing = [slug for slug in CATALOGS if slug not in ids]
        if missing:
            lines.append(f"Not published by this hub: {', '.join(missing)}.")
        lines.append(
            "Pass a catalog slug to search_xtm_hub_resources, or catalog='all'."
        )
        return "\n".join(lines)

    # ── 2. Search ──────────────────────────────────────────────────────────

    def _filters(
        catalog: str,
        *,
        use_case_ids: list[str],
        compatible_with_version: str,
        integration_type: str,
        verified_only: bool,
        deployable_only: bool,
        license_type: str,
        entity_types: list[str],
    ) -> tuple[dict[str, Any] | None, str | None]:
        integration_only = {
            "integration_type": integration_type,
            "verified_only": verified_only,
            "deployable_only": deployable_only,
            "license_type": license_type,
        }
        used = [name for name, value in integration_only.items() if value]
        if used and catalog != _INTEGRATIONS_CATALOG:
            return None, (
                f"Error: {', '.join(used)} only apply to catalog "
                f"'{_INTEGRATIONS_CATALOG}'."
            )
        if entity_types and catalog != _CUSTOM_VIEWS_CATALOG:
            return None, (
                f"Error: entity_types only applies to catalog '{_CUSTOM_VIEWS_CATALOG}'."
            )
        leaves: list[tuple[str, list[str]]] = []
        if use_case_ids:
            bad = [u for u in use_case_ids if not _ID_RE.match(u)]
            if bad:
                return None, (
                    "Error: use_case_ids takes the ids list_xtm_hub_use_cases returns, "
                    "comma-separated."
                )
            leaves.append(("label", use_case_ids[:20]))
        if compatible_with_version:
            version = compatible_with_version.strip().lstrip("vV")
            if not _VERSION_RE.match(version):
                return None, (
                    "Error: compatible_with_version must be a version like 6.8 or 6.8.3."
                )
            leaves.append(("product_version", [version]))
        if integration_type:
            if integration_type not in _INTEGRATION_TYPES:
                return None, (
                    "Error: integration_type must be one of "
                    f"{', '.join(sorted(_INTEGRATION_TYPES))}."
                )
            leaves.append(("integration_type", [integration_type]))
        if verified_only:
            leaves.append(("verified", ["true"]))
        if deployable_only:
            leaves.append(("manager_supported", ["true"]))
        if license_type:
            license_value = _LICENSE_TYPES.get(license_type.strip().lower())
            if not license_value:
                return None, "Error: license_type must be 'free' or 'commercial'."
            leaves.append(("license_type", [license_value]))
        if entity_types:
            leaves.append(("entity_type", [_one_line(t, 80) for t in entity_types[:20]]))
        if not leaves:
            return None, None
        return {
            "operator": "AND",
            "children": [{"leaf": {"key": key, "value": value}} for key, value in leaves],
        }, None

    async def _search_one(
        catalog: str,
        sid: str,
        *,
        search: str,
        filters: dict[str, Any] | None,
        order: tuple[str, str],
        first: int,
        after: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        variables: dict[str, Any] = {
            "slug": catalog,
            "sid": sid,
            "first": first,
            "after": after or None,
            "orderBy": order[0],
            "orderMode": order[1],
            "search": search or None,
            "filters": filters,
        }
        data, err = await xtm_hub_graphql(
            base_url, SEARCH_QUERY, variables, f"search {catalog}"
        )
        if err:
            return None, err
        connection = (data or {}).get("publicDocuments")
        if not isinstance(connection, dict):
            _log_failure(f"search {catalog}", "no publicDocuments connection")
            return None, _NOT_A_HUB
        return connection, None

    def _nodes(connection: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            edge["node"]
            for edge in connection.get("edges") or []
            if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
        ]

    async def search_xtm_hub_resources(
        catalog: str = "all",
        search: str = "",
        use_case_ids: str = "",
        compatible_with_version: str = "",
        integration_type: str = "",
        verified_only: bool = False,
        deployable_only: bool = False,
        license_type: str = "",
        entity_types: str = "",
        order_by: str = "updated_at",
        limit: int = 20,
        after: str = "",
    ) -> str:
        """Search the XTM Hub catalog."""
        catalog_slug, err = _catalog_arg(catalog, allow_all=True)
        if err:
            return err
        order = _ORDERINGS.get(str(order_by or "").strip().lower())
        if order is None:
            return f"Error: order_by must be one of {', '.join(_ORDERINGS)}."
        after = str(after or "").strip()
        if after and (catalog_slug == "all" or not _CURSOR_RE.match(after)):
            return (
                "Error: 'after' takes the cursor a search of one catalog returned; "
                "a search of every catalog has no next page."
            )
        search_text = _one_line(search, 200)
        filters_by_catalog: dict[str, dict[str, Any] | None] = {}
        targets = list(CATALOGS) if catalog_slug == "all" else [catalog_slug]
        for target in targets:
            filters, ferr = _filters(
                target,
                use_case_ids=_list_arg(use_case_ids),
                compatible_with_version=str(compatible_with_version or ""),
                integration_type=str(integration_type or "").strip().lower(),
                verified_only=bool(verified_only),
                deployable_only=bool(deployable_only),
                license_type=str(license_type or ""),
                entity_types=_list_arg(entity_types),
            )
            if ferr:
                return ferr
            filters_by_catalog[target] = filters

        ids, err = await _catalogs()
        if err:
            return err
        targets = [t for t in targets if t in ids]
        if not targets:
            return f"XTM Hub ({base_url}) does not publish the catalog '{catalog_slug}'."

        if catalog_slug != "all":
            first = _clamp(limit, 20, _MAX_LIMIT)
            connection, err = await _search_one(
                catalog_slug,
                ids[catalog_slug],
                search=search_text,
                filters=filters_by_catalog[catalog_slug],
                order=order,
                first=first,
                after=after,
            )
            if err:
                return err
            nodes = _nodes(connection)
            total = connection.get("totalCount")
            label = CATALOGS[catalog_slug][0]
            if not nodes:
                return f"No {label} matched on XTM Hub ({base_url})."
            header = (
                f"{len(nodes)} of {total if isinstance(total, int) else '?'} {label} "
                f"matched on XTM Hub ({base_url}):"
            )
            page = connection.get("pageInfo") or {}
            cursor = page.get("endCursor") if isinstance(page, dict) else None
            hint = ""
            if (
                isinstance(page, dict)
                and page.get("hasNextPage")
                and isinstance(cursor, str)
                and _CURSOR_RE.match(cursor)
                and isinstance(total, int)
                and total > len(nodes)
            ):
                hint = f'\n[More results may be available - pass after="{cursor}"]'
            return (
                header
                + "\n"
                + _quarantine(_render_list(nodes, base_url))
                + hint
                + "\nUse get_xtm_hub_resource with a slug for the full description."
            )

        first = _clamp(limit, _MAX_PER_CATALOG, _MAX_PER_CATALOG)
        results = await asyncio.gather(
            *(
                _search_one(
                    target,
                    ids[target],
                    search=search_text,
                    filters=filters_by_catalog[target],
                    order=order,
                    first=first,
                    after="",
                )
                for target in targets
            )
        )
        sections: list[str] = []
        failed: list[str] = []
        matched = 0
        for target, (connection, cerr) in zip(targets, results, strict=True):
            if cerr or connection is None:
                failed.append(target)
                continue
            nodes = _nodes(connection)
            if not nodes:
                continue
            matched += len(nodes)
            total = connection.get("totalCount")
            sections.append(
                f"## {CATALOGS[target][0]} ({target}): {len(nodes)} of "
                f"{total if isinstance(total, int) else '?'}\n"
                + _render_list(nodes, base_url)
            )
        if failed and len(failed) == len(targets):
            return results[0][1] or _NOT_A_HUB
        notes = (
            f"\nNot searched (XTM Hub error, retry): {', '.join(failed)}." if failed else ""
        )
        if not sections:
            return f"Nothing matched in the XTM Hub catalogs ({base_url}).{notes}"
        return (
            f"{matched} resources matched across the XTM Hub catalogs ({base_url}), "
            f"at most {first} per catalog:\n"
            + _quarantine("\n\n".join(sections))
            + notes
            + "\nSearch one catalog for more results, or use get_xtm_hub_resource "
            "with a slug for the full description."
        )

    # ── 3. One resource ────────────────────────────────────────────────────

    def _parse_reference(slug: str, catalog: str) -> tuple[str, str, str | None]:
        """``(catalog, slug, error)`` from a slug or a Hub resource link."""
        value = str(slug or "").strip()
        if "/" in value:
            path = urlsplit(value).path if "://" in value else value
            segments = [s for s in path.split("/") if s]
            if _RESOURCE_PAGE_PATH.strip("/") in segments:
                index = segments.index(_RESOURCE_PAGE_PATH.strip("/"))
                tail = segments[index + 1 :]
                if len(tail) == 2:
                    catalog, value = tail
        catalog_slug, err = _catalog_arg(catalog, allow_all=True)
        if err:
            return "", "", err
        if not _SLUG_RE.match(value):
            return "", "", (
                "Error: slug must be a resource slug (as search_xtm_hub_resources "
                "returns it) or an XTM Hub resource link."
            )
        return catalog_slug, value, None

    async def get_xtm_hub_resource(slug: str, catalog: str = "") -> str:
        """One XTM Hub resource in full."""
        catalog_slug, resource_slug, err = _parse_reference(slug, catalog)
        if err:
            return err
        ids, err = await _catalogs()
        if err:
            return err
        targets = list(ids) if catalog_slug == "all" else [catalog_slug]
        targets = [t for t in targets if t in ids]
        if not targets:
            return f"XTM Hub ({base_url}) does not publish the catalog '{catalog_slug}'."
        results = await asyncio.gather(
            *(
                xtm_hub_graphql(
                    base_url,
                    RESOURCE_QUERY,
                    {"sid": ids[target], "slug": resource_slug},
                    f"get resource in {target}",
                )
                for target in targets
            )
        )
        found: list[tuple[str, dict[str, Any]]] = []
        errors: list[str] = []
        for target, (data, rerr) in zip(targets, results, strict=True):
            if rerr:
                errors.append(rerr)
                continue
            node = (data or {}).get("publicDocumentBySlug")
            if isinstance(node, dict):
                found.append((target, node))
        if not found:
            if errors and len(errors) == len(targets):
                return errors[0]
            where = "any catalog" if catalog_slug == "all" else f"catalog '{catalog_slug}'"
            return (
                f"No resource with slug '{resource_slug}' in {where} of XTM Hub "
                f"({base_url}). Use search_xtm_hub_resources to find its slug."
            )
        parts = [_detail(node, base_url, target) for target, node in found]
        header = (
            "XTM Hub resource:"
            if len(found) == 1
            else f"{len(found)} XTM Hub resources share the slug '{resource_slug}':"
        )
        return (
            header
            + "\n"
            + _quarantine("\n\n".join(parts))
            + "\nTo deploy it, open its Hub page: the Hub's Deploy action installs it "
            "into the user's OpenCTI or OpenAEV (it needs the user's own XTM Hub "
            "session)."
        )

    # ── 4. Featured ────────────────────────────────────────────────────────

    async def list_xtm_hub_featured_resources(
        ranking: str = "newest", product: str = "", limit: int = 10
    ) -> str:
        """Newest or most deployed XTM Hub resources."""
        ranking_key = str(ranking or "").strip().lower()
        if ranking_key not in ("newest", "most_deployed"):
            return "Error: ranking must be 'newest' or 'most_deployed'."
        product_key = str(product or "").strip().lower()
        if product_key and product_key not in _PRODUCTS:
            return "Error: product must be 'opencti', 'openaev' or empty for both."
        if not base_url:
            return _BAD_BASE_URL
        query = NEWEST_QUERY if ranking_key == "newest" else MOST_DEPLOYED_QUERY
        field = "newestDocuments" if ranking_key == "newest" else "mostDeployedDocuments"
        data, err = await xtm_hub_graphql(
            base_url,
            query,
            {
                "limit": _clamp(limit, 10, _MAX_FEATURED),
                "platforms": [product_key] if product_key else None,
            },
            f"list {ranking_key} resources",
        )
        if err:
            return err
        rows = (data or {}).get(field)
        if not isinstance(rows, list):
            _log_failure(f"list {ranking_key} resources", f"no {field} list")
            return _NOT_A_HUB
        nodes = [row for row in rows if isinstance(row, dict)]
        what = "newest" if ranking_key == "newest" else "most deployed"
        scope = {"opencti": " for OpenCTI", "openaev": " for OpenAEV"}.get(product_key, "")
        if not nodes:
            return f"XTM Hub ({base_url}) lists no {what} resources{scope}."
        return (
            f"The {len(nodes)} {what} XTM Hub resources{scope} ({base_url}):\n"
            + _quarantine(_render_list(nodes, base_url))
        )

    # ── 5. Use cases ───────────────────────────────────────────────────────

    async def list_xtm_hub_use_cases(
        product: str = "", search: str = "", limit: int = 50, after: str = ""
    ) -> str:
        """The use cases XTM Hub resources are tagged with."""
        product_key = str(product or "").strip().lower()
        if product_key and product_key not in _PRODUCTS:
            return "Error: product must be 'opencti', 'openaev' or empty for both."
        after = str(after or "").strip()
        if after and not _CURSOR_RE.match(after):
            return "Error: 'after' takes the cursor a previous call returned."
        if not base_url:
            return _BAD_BASE_URL
        data, err = await xtm_hub_graphql(
            base_url,
            USE_CASES_QUERY,
            {
                "first": _clamp(limit, 50, 100),
                "after": after or None,
                "search": _one_line(search, 100) or None,
                "product": product_key or None,
            },
            "list use cases",
        )
        if err:
            return err
        connection = (data or {}).get("useCases")
        if not isinstance(connection, dict):
            _log_failure("list use cases", "no useCases connection")
            return _NOT_A_HUB
        rows = _nodes(connection)
        if not rows:
            return f"No XTM Hub use case matched ({base_url})."
        lines = []
        for row in rows:
            products = row.get("product")
            for_products = (
                ", ".join(_one_line(p, 20) for p in products if p)
                if isinstance(products, list)
                else ""
            )
            lines.append(
                f"- {_one_line(row.get('name'), 80)} - id: {_one_line(row.get('id'), 200)}"
                + (f" ({for_products})" if for_products else "")
            )
        total = connection.get("totalCount")
        page = connection.get("pageInfo") or {}
        cursor = page.get("endCursor") if isinstance(page, dict) else None
        hint = ""
        if (
            isinstance(page, dict)
            and page.get("hasNextPage")
            and isinstance(cursor, str)
            and _CURSOR_RE.match(cursor)
            and isinstance(total, int)
            and total > len(rows)
        ):
            hint = f'\n[More use cases may be available - pass after="{cursor}"]'
        return (
            f"{len(rows)} of {total if isinstance(total, int) else '?'} XTM Hub use cases:\n"
            + _quarantine("\n".join(lines))
            + hint
            + "\nPass ids to search_xtm_hub_resources(use_case_ids=...)."
        )

    # ── 6. With an API token ───────────────────────────────────────────────

    async def get_xtm_hub_myself() -> str:
        """Whom the integration's API token acts as on the Hub."""
        token_value, err = await _authed()
        if err:
            return err
        myself, err = await _read_myself(base_url, token_value)
        if err or myself is None:
            return err or _NOT_A_HUB
        token, me = myself["token"], myself["me"]
        name = " ".join(
            _one_line(me.get(key), 80) for key in ("first_name", "last_name") if me.get(key)
        )
        lines = [
            f"User: {_one_line(me.get('email'), 200)}" + (f" ({name})" if name else ""),
            f"Organization: {_one_line(token.get('organization_name'), 120) or '?'}",
            f"Access: {_scope_label(token.get('scope'))}",
            f"Token: {_one_line(token.get('name'), 100)}, expires "
            f"{_one_line(token.get('expires_at'), 40)[:10] or '?'}",
        ]
        return f"XTM Hub API token on {base_url}:\n" + _quarantine("\n".join(lines))

    async def list_xtm_hub_libraries() -> str:
        """The libraries (service instances) the token's user reaches."""
        token_value, err = await _authed()
        if err:
            return err
        data, err = await xtm_hub_graphql(
            base_url, LIBRARIES_QUERY, {"first": 100}, "list libraries", api_token=token_value
        )
        if err:
            return err
        connection = (data or {}).get("serviceInstances")
        if not isinstance(connection, dict):
            _log_failure("list libraries", "no serviceInstances connection")
            return _NOT_A_HUB
        rows = _nodes(connection)
        if not rows:
            return f"The token's user reaches no XTM Hub library ({base_url})."
        lines = []
        count = 0
        for row in rows:
            sid = row.get("id")
            if not isinstance(sid, str) or not _ID_RE.match(sid):
                continue
            count += 1
            definition = row.get("service_definition")
            kind = (
                _one_line(definition.get("identifier"), 60)
                if isinstance(definition, dict)
                else ""
            )
            facts = ["public" if row.get("public") else "private"]
            if row.get("organization_subscribed") is True:
                facts.append("subscribed")
            capabilities = row.get("capabilities")
            if isinstance(capabilities, list) and capabilities:
                facts.append("you may " + ", ".join(_one_line(c, 40) for c in capabilities[:8]))
            lines.append(
                f"- {_one_line(row.get('name'), 120)}"
                + (f" [{kind}]" if kind else "")
                + f" - library id: {sid} ({'; '.join(facts)})"
            )
            description = _one_line(row.get("description"), 200)
            if description:
                lines.append(f"  {description}")
        if not count:
            _log_failure("list libraries", "no library with a usable id")
            return _NOT_A_HUB
        return (
            f"{count} XTM Hub libraries the token's user reaches:\n"
            + _quarantine("\n".join(lines))
            + "\nPass a library id to search_xtm_hub_library."
        )

    async def search_xtm_hub_library(
        library_id: str,
        search: str = "",
        order_by: str = "updated_at",
        limit: int = 20,
        after: str = "",
    ) -> str:
        """The active resources of one library, private libraries included."""
        sid = str(library_id or "").strip()
        if not _ID_RE.match(sid):
            return "Error: library_id takes an id list_xtm_hub_libraries returned."
        if order_by not in _ORDERINGS:
            return f"Error: order_by must be one of {', '.join(_ORDERINGS)}."
        after = str(after or "").strip()
        if after and not _CURSOR_RE.match(after):
            return "Error: 'after' takes the cursor a previous search returned."
        token_value, err = await _authed()
        if err:
            return err
        ordering, direction = _ORDERINGS[order_by]
        data, err = await xtm_hub_graphql(
            base_url,
            LIBRARY_SEARCH_QUERY,
            {
                "sid": sid,
                "first": _clamp(limit, 20, _MAX_LIMIT),
                "after": after or None,
                "orderBy": ordering,
                "orderMode": direction,
                "search": _one_line(search, 200) or None,
            },
            "search a library",
            api_token=token_value,
        )
        if err:
            return err
        connection = (data or {}).get("documents")
        if not isinstance(connection, dict):
            _log_failure("search a library", "no documents connection")
            return _NOT_A_HUB
        rows = _nodes(connection)
        # Only a published resource is offered: a draft or one in validation
        # is not the library's yet.
        active = [row for row in rows if row.get("status") == _PUBLISHED]
        hidden = len(rows) - len(active)
        page = connection.get("pageInfo") or {}
        cursor = page.get("endCursor") if isinstance(page, dict) else None
        hint = ""
        if (
            isinstance(page, dict)
            and page.get("hasNextPage")
            and isinstance(cursor, str)
            and _CURSOR_RE.match(cursor)
        ):
            hint = f'\n[More resources may be available - pass after="{cursor}"]'
        if not active:
            return f"No published resource matched in this library ({base_url}).{hint}"
        lines: list[str] = []
        for row in active:
            # The public catalog page of a type is no page of a private library.
            lines.extend(
                line
                for line in _summary_lines(row, base_url)
                if not line.startswith("  Hub page:")
            )
            did = row.get("id")
            if isinstance(did, str) and _ID_RE.match(did):
                lines.append(f"  resource id: {did}")
        total = connection.get("totalCount")
        return (
            f"{len(active)} of {total if isinstance(total, int) else '?'} resources "
            "of the library"
            + (f" ({hidden} not published not shown)" if hidden else "")
            + ":\n"
            + _quarantine("\n".join(lines))
            + hint
            + "\ndownload_xtm_hub_resource(library_id, resource_id) stores one as a file."
        )

    async def list_xtm_hub_registered_platforms(
        identifier: str = "", only_active: bool = False
    ) -> str:
        """The platforms registered with the Hub in the token's organization."""
        kind = str(identifier or "").strip().lower()
        if kind and kind not in _PLATFORM_IDENTIFIERS:
            return "Error: identifier must be 'opencti', 'openaev', 'xtmone' or empty."
        token_value, err = await _authed()
        if err:
            return err
        data, err = await xtm_hub_graphql(
            base_url,
            REGISTERED_PLATFORMS_QUERY,
            {"input": {"identifier": kind or None, "onlyActive": bool(only_active)}},
            "list registered platforms",
            api_token=token_value,
        )
        if err:
            return err
        rows = (data or {}).get("registeredPlatforms")
        if not isinstance(rows, list):
            _log_failure("list registered platforms", "no registeredPlatforms list")
            return _NOT_A_HUB
        lines = []
        for row in rows[:100]:
            if not isinstance(row, dict):
                continue
            facts = [
                _one_line(row.get(key), 40)
                for key in ("identifier", "version", "contract", "status")
                if row.get(key)
            ]
            checked = _one_line(row.get("last_connectivity_check"), 40)[:10]
            if checked:
                facts.append(f"checked {checked}")
            lines.append(
                f"- {_one_line(row.get('title'), 120)} - {_one_line(row.get('url'), 300)}"
                + (f" ({', '.join(facts)})" if facts else "")
            )
        if not lines:
            return f"No platform is registered in the token's organization ({base_url})."
        return (
            f"{len(lines)} platforms registered with the XTM Hub in the token's "
            "organization:\n" + _quarantine("\n".join(lines))
        )

    async def download_xtm_hub_resource(library_id: str, resource_id: str) -> str | ToolResult:
        """Store a resource's file as a file of the run."""
        sid = str(library_id or "").strip()
        did = str(resource_id or "").strip()
        if not _ID_RE.match(sid) or not _ID_RE.match(did):
            return (
                "Error: pass the library id and the resource id search_xtm_hub_library "
                "returned."
            )
        token_value, err = await _authed()
        if err:
            return err
        # The Hub's own access rules decide first, through the query.
        data, err = await xtm_hub_graphql(
            base_url,
            DOCUMENT_QUERY,
            {"sid": sid, "did": did},
            "read a resource",
            api_token=token_value,
        )
        if err:
            return err
        node = (data or {}).get("document")
        if not isinstance(node, dict):
            return "Error: no such resource in that library, or the token's user cannot see it."
        if node.get("status") != _PUBLISHED:
            return "Error: this resource was withdrawn from the XTM Hub; it is not downloaded."
        name = _one_line(node.get("file_name") or node.get("name"), 200) or "xtm-hub-resource"
        url = f"{base_url}{_DOWNLOAD_PATH}/{quote(sid, safe='')}/{quote(did, safe='')}?attach=1"
        # The Hub streams the file itself: a redirect is an error, never
        # followed with the token on it.
        async with egress_client(
            timeout=60, trust_env=False, follow_redirects=False
        ) as client:
            fetched = await fetch_file(
                client,
                "get",
                url,
                provider=_PROVIDER,
                action="download a resource",
                max_bytes=_MAX_DOWNLOAD_BYTES,
                headers={
                    "Authorization": f"Bearer {token_value}",
                    "Accept-Encoding": "identity",
                    "User-Agent": PLATFORM_USER_AGENT,
                },
            )
        if isinstance(fetched, str):
            # fetch_file's text may quote the Hub's answer: logged only.
            _log_failure("download a resource", str(fetched))
            status = getattr(fetched, "status", None)
            if status == 401:
                return _HUB_KEY_REFUSED
            if getattr(fetched, "too_large", False):
                return (
                    f"Error: the resource is over the {_MAX_DOWNLOAD_BYTES // 1024 // 1024} MB "
                    "download limit."
                )
            if status == 429 or (isinstance(status, int) and status >= 500):
                return _UNAVAILABLE
            if status is None:
                return _UNREACHABLE
            return "Error: XTM Hub did not serve the resource's file."
        filename = fetched.filename or name
        content_type = file_content_type(fetched.content_type, filename)
        return await deliver_file(
            data=fetched.data,
            filename=with_extension(filename, content_type),
            content_type=content_type,
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            details=["Downloaded from the XTM Hub with the integration's API token."],
        )

    # ── 7. Publish (read and write token) ──────────────────────────────────

    async def publish_xtm_hub_resource(
        library_id: str,
        file_id: str,
        name: str,
        slug: str,
        short_description: str,
        description: str,
        product_version: str,
        use_case_ids: str = "",
        entity_types: str = "",
        meta_description: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Publish a file of the run as a new resource of one library."""
        sid = str(library_id or "").strip()
        if not _ID_RE.match(sid):
            return "Error: library_id takes an id list_xtm_hub_libraries returned."
        title = _one_line(name, _MAX_NAME + 1)
        if not title or len(title) > _MAX_NAME:
            return f"Error: name is required, at most {_MAX_NAME} characters."
        slug_value = str(slug or "").strip()
        if not _PUBLISH_SLUG_RE.match(slug_value):
            return (
                "Error: slug must be lowercase letters, digits and dashes, starting "
                "with a letter or a digit, at most 100 characters."
            )
        short = _one_line(short_description, _MAX_PUBLISH_SHORT_DESCRIPTION + 1)
        if not short or len(short) > _MAX_PUBLISH_SHORT_DESCRIPTION:
            return (
                "Error: short_description is required, at most "
                f"{_MAX_PUBLISH_SHORT_DESCRIPTION} characters."
            )
        meta = _one_line(meta_description, _MAX_PUBLISH_SHORT_DESCRIPTION + 1)
        if len(meta) > _MAX_PUBLISH_SHORT_DESCRIPTION:
            return f"Error: meta_description is one line, at most {_MAX_PUBLISH_SHORT_DESCRIPTION} characters."
        long_text = str(description or "").strip()
        if not long_text or len(long_text) > _MAX_PUBLISH_DESCRIPTION:
            return (
                "Error: description is required, at most "
                f"{_MAX_PUBLISH_DESCRIPTION} characters."
            )
        version = str(product_version or "").strip()
        if not _VERSION_RE.match(version):
            return "Error: product_version is the module version it was made for, e.g. 6.8.0."
        use_cases = _list_arg(use_case_ids)
        if any(not _ID_RE.match(u) for u in use_cases):
            return "Error: use_case_ids takes ids list_xtm_hub_use_cases returned."
        types = _list_arg(entity_types)
        if any(not _ENTITY_TYPE_RE.match(t) for t in types):
            return "Error: entity_types takes OpenCTI entity types, e.g. Threat-Actor-Group,Malware."
        token_value, err = await _authed()
        if err:
            return err
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_UPLOAD_BYTES, target="the XTM Hub"
        )
        if isinstance(piped, str):
            return piped
        file_bytes, filename, content_type = piped
        created, err = await publish_hub_resource(
            base_url,
            token_value,
            library_id=sid,
            name=title,
            slug=slug_value,
            short_description=short,
            description=long_text,
            product_version=version,
            file_bytes=file_bytes,
            filename=filename,
            content_type=content_type,
            use_cases=use_cases,
            entity_types=types,
            meta_description=meta,
        )
        if err or created is None:
            return err or _NOT_A_HUB
        state = (
            "active: visible in the library"
            if created["active"]
            else "inactive: only its publishers see it until it is activated on the XTM Hub"
        )
        return (
            f"Published {_PUBLISHABLE_LIBRARIES[created['kind']]} '{title}' on {base_url} "
            f"({state}).\n  resource id: {_one_line(created['id'], 200)}\n"
            f"  library id: {sid}"
        )

    executor.register("list_xtm_hub_catalogs", list_xtm_hub_catalogs)
    executor.register("search_xtm_hub_resources", search_xtm_hub_resources)
    executor.register("get_xtm_hub_resource", get_xtm_hub_resource)
    executor.register("list_xtm_hub_featured_resources", list_xtm_hub_featured_resources)
    executor.register("list_xtm_hub_use_cases", list_xtm_hub_use_cases)
    executor.register("get_xtm_hub_myself", get_xtm_hub_myself)
    executor.register("list_xtm_hub_libraries", list_xtm_hub_libraries)
    executor.register("search_xtm_hub_library", search_xtm_hub_library)
    executor.register("list_xtm_hub_registered_platforms", list_xtm_hub_registered_platforms)
    executor.register("download_xtm_hub_resource", download_xtm_hub_resource)
    executor.register("publish_xtm_hub_resource", publish_xtm_hub_resource)

    # ── 8. The rest of the Hub API, one tool per operation (xtm_hub_api) ────

    async def _run_api(
        query: str,
        variables: dict[str, Any],
        action: str,
        token_value: str,
        uploads: list[tuple[str, str, bytes, str]] | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        return await xtm_hub_graphql(base_url, query, variables, action, api_token=token_value, uploads=uploads)

    async def _read_api_file(file_context: FileContext | None, file_id: str) -> tuple[bytes, str, str] | str:
        return await read_pipe_file(file_context, file_id, max_bytes=_MAX_UPLOAD_BYTES, target="the XTM Hub")

    api_definitions = build_api_tools(base_url, executor, authed=_authed, run=_run_api, read_file=_read_api_file)

    catalog_enum = list(CATALOGS)
    return [
        ToolDefinition(
            name="list_xtm_hub_catalogs",
            description=(
                "List the resource catalogs XTM Hub (the Filigran ecosystem hub) "
                "publishes for the XTM Platform: OpenCTI custom dashboards, custom "
                "views, playbooks and integrations, and OpenAEV scenarios. Returns "
                "each catalog's slug for search_xtm_hub_resources."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="search_xtm_hub_resources",
            description=(
                "Search XTM Hub for ready-to-use XTM Platform resources: OpenCTI "
                "custom dashboards, custom views, playbooks, integrations "
                "(connectors, CSV / TAXII / RSS feeds, live streams, third-party "
                "integrations) and OpenAEV scenarios. Search one catalog (with "
                "paging) or all of them; free text matches name, description and "
                "use cases. Returns slugs and Hub page links."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "catalog": {
                        "type": "string",
                        "enum": ["all", *catalog_enum],
                        "description": "Catalog to search, or 'all' (no paging)",
                        "default": "all",
                    },
                    "search": {
                        "type": "string",
                        "description": "Free text, e.g. 'ransomware' or 'phishing feed'",
                        "default": "",
                    },
                    "use_case_ids": {
                        "type": "string",
                        "description": "Comma-separated use case ids from list_xtm_hub_use_cases",
                        "default": "",
                    },
                    "compatible_with_version": {
                        "type": "string",
                        "description": (
                            "Only resources that run on this OpenCTI / OpenAEV "
                            "version, e.g. 6.8"
                        ),
                        "default": "",
                    },
                    "integration_type": {
                        "type": "string",
                        "enum": ["", *sorted(_INTEGRATION_TYPES)],
                        "description": "opencti-integrations only",
                        "default": "",
                    },
                    "verified_only": {
                        "type": "boolean",
                        "description": "opencti-integrations only: Filigran-verified connectors",
                        "default": False,
                    },
                    "deployable_only": {
                        "type": "boolean",
                        "description": (
                            "opencti-integrations only: connectors the OpenCTI "
                            "connector manager can deploy"
                        ),
                        "default": False,
                    },
                    "license_type": {
                        "type": "string",
                        "enum": ["", "free", "commercial"],
                        "description": "opencti-integrations only",
                        "default": "",
                    },
                    "entity_types": {
                        "type": "string",
                        "description": (
                            "opencti-custom-views only: comma-separated entity "
                            "types, e.g. Threat-Actor-Group,Malware"
                        ),
                        "default": "",
                    },
                    "order_by": {
                        "type": "string",
                        "enum": list(_ORDERINGS),
                        "description": "Newest update first, newest first, or by name",
                        "default": "updated_at",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"Results (max {_MAX_LIMIT}; with catalog 'all', max "
                            f"{_MAX_PER_CATALOG} per catalog)"
                        ),
                        "default": 20,
                    },
                    "after": {
                        "type": "string",
                        "description": "Cursor from a previous search of the same catalog",
                        "default": "",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_xtm_hub_resource",
            description=(
                "Get one XTM Hub resource in full: description, compatible version, "
                "use cases, publisher, download count, and for integrations the "
                "container image, source code, feed URL and documentation links. "
                "Takes the slug search_xtm_hub_resources returns, or an XTM Hub "
                "resource link. Deploying it is done from its Hub page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Resource slug, or its XTM Hub page link",
                    },
                    "catalog": {
                        "type": "string",
                        "enum": ["", *catalog_enum],
                        "description": "Catalog of the slug (empty: look in every catalog)",
                        "default": "",
                    },
                },
                "required": ["slug"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_xtm_hub_featured_resources",
            description=(
                "List the newest or the most deployed XTM Hub resources, for "
                "OpenCTI, OpenAEV or both - what is new or popular in the "
                "Filigran ecosystem."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ranking": {
                        "type": "string",
                        "enum": ["newest", "most_deployed"],
                        "default": "newest",
                    },
                    "product": {
                        "type": "string",
                        "enum": ["", "opencti", "openaev"],
                        "description": "Empty for both",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Results (max {_MAX_FEATURED})",
                        "default": 10,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_xtm_hub_use_cases",
            description=(
                "List the use cases XTM Hub resources are tagged with (e.g. "
                "ransomware, vulnerability management), with the ids "
                "search_xtm_hub_resources filters on."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "product": {
                        "type": "string",
                        "enum": ["", "opencti", "openaev"],
                        "description": "Empty for both",
                        "default": "",
                    },
                    "search": {
                        "type": "string",
                        "description": "Text matched against use case names",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Results (max 100)",
                        "default": 50,
                    },
                    "after": {
                        "type": "string",
                        "description": "Cursor from a previous call",
                        "default": "",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_xtm_hub_myself",
            description=(
                "Say whom the integration's XTM Hub API token acts as: the user, "
                "the organization, read only or read and write, and its expiry. "
                "Needs an XTM Hub API token."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_xtm_hub_libraries",
            description=(
                "List the XTM Hub libraries the API token's user reaches, private "
                "organization libraries included, with their ids and what the user "
                "may do in each. Needs an XTM Hub API token."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="search_xtm_hub_library",
            description=(
                "Search the active resources of one XTM Hub library, private "
                "libraries included, as the API token's user. Returns resource "
                "ids for download_xtm_hub_resource. Needs an XTM Hub API token."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "library_id": {
                        "type": "string",
                        "description": "Library id from list_xtm_hub_libraries",
                    },
                    "search": {
                        "type": "string",
                        "description": "Free text matched against the resources",
                        "default": "",
                    },
                    "order_by": {
                        "type": "string",
                        "enum": list(_ORDERINGS),
                        "default": "updated_at",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Results (max {_MAX_LIMIT})",
                        "default": 20,
                    },
                    "after": {
                        "type": "string",
                        "description": "Cursor from a previous search of the same library",
                        "default": "",
                    },
                },
                "required": ["library_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_xtm_hub_registered_platforms",
            description=(
                "List the OpenCTI, OpenAEV and XTM One platforms registered with "
                "the XTM Hub in the API token's organization: URL, version, "
                "edition, connectivity status. Needs an XTM Hub API token."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "enum": ["", *sorted(_PLATFORM_IDENTIFIERS)],
                        "description": "Empty for every kind of platform",
                        "default": "",
                    },
                    "only_active": {
                        "type": "boolean",
                        "description": "Only the platforms whose last check succeeded",
                        "default": False,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="download_xtm_hub_resource",
            workspace_ingest=True,
            description=(
                "Download the file of an XTM Hub resource (a dashboard, a view, a "
                "scenario, a document) as a file of the run, with its file_id. "
                "Takes the library id and resource id search_xtm_hub_library "
                f"returns; files over {_MAX_DOWNLOAD_BYTES // 1024 // 1024} MB are "
                "refused. Needs an XTM Hub API token."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "library_id": {
                        "type": "string",
                        "description": "Library id from list_xtm_hub_libraries",
                    },
                    "resource_id": {
                        "type": "string",
                        "description": "Resource id from search_xtm_hub_library",
                    },
                },
                "required": ["library_id", "resource_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="publish_xtm_hub_resource",
            description=(
                "Publish a file of the run (an OpenCTI dashboard, view or "
                "playbook export, an OpenAEV scenario) as a new resource of an "
                "XTM Hub library, as the API token's user. Needs a read and "
                "write XTM Hub API token and the library's UPLOAD right. The "
                "resource is always published inactive: a person then reviews "
                "and activates it on the XTM Hub."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "library_id": {
                        "type": "string",
                        "description": "Library id from list_xtm_hub_libraries",
                    },
                    "file_id": {
                        "type": "string",
                        "description": "A file of the run: attached, generated or downloaded",
                    },
                    "name": {"type": "string", "description": f"Max {_MAX_NAME} characters"},
                    "slug": {
                        "type": "string",
                        "description": "Lowercase letters, digits and dashes; unique in the library",
                    },
                    "short_description": {
                        "type": "string",
                        "description": f"One line, max {_MAX_PUBLISH_SHORT_DESCRIPTION} characters",
                    },
                    "description": {"type": "string", "description": "Markdown"},
                    "meta_description": {
                        "type": "string",
                        "description": (
                            "Search summary (default short_description). Published as "
                            "'OpenCTI Dashboard: TEXT.', "
                            f"{_META_DESCRIPTION_MIN}-{_META_DESCRIPTION_MAX} characters with that prefix"
                        ),
                        "default": "",
                    },
                    "product_version": {
                        "type": "string",
                        "description": "The OpenCTI / OpenAEV version it was made for, e.g. 6.8.0",
                    },
                    "use_case_ids": {
                        "type": "string",
                        "description": "Comma-separated use case ids from list_xtm_hub_use_cases",
                        "default": "",
                    },
                    "entity_types": {
                        "type": "string",
                        "description": (
                            "Custom views only, required there: comma-separated "
                            "entity types, e.g. Threat-Actor-Group,Malware"
                        ),
                        "default": "",
                    },
                },
                "required": [
                    "library_id",
                    "file_id",
                    "name",
                    "slug",
                    "short_description",
                    "description",
                    "product_version",
                ],
            },
            source=_SOURCE,
            priority=1,
        ),
        *api_definitions,
    ]

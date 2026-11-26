"""OpenCTI integration – comprehensive STIX entity management.

Provides full CRUD operations on STIX Domain Objects (SDOs),
STIX Cyber Observables (SCOs), and STIX Core Relationships via
the OpenCTI GraphQL API.  GraphQL queries are derived from the
official pycti client library to ensure accuracy and completeness.

Token format passed by the dispatcher: ``base_url||api_token``
(the extra_config base_url is prepended by ``_get_token`` in
integration_tools.py).

Every outgoing call in this provider opens its client through :func:`_client`
and goes out through :func:`_graphql` / :func:`_graphql_upload`, so a
platform-managed integration's short-lived cross-platform JWT is re-minted
whenever it is about to expire (or has already been rejected) instead of pinning
the token minted at tool-build time.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    AutoRefreshClient,
    bearer_seconds_remaining,
    clip_text,
    ensure_fresh_token,
    safe_request,
)

logger = logging.getLogger(__name__)

_PROVIDER = "OpenCTI"

# ---------------------------------------------------------------------------
# Knowledge-mutation tools that require a draft context
# ---------------------------------------------------------------------------
# These tools create, update, or delete STIX knowledge (entities,
# relationships, observables, indicators, etc.).  They are only allowed
# when the ``opencti-draft-id`` scope header is present.  Non-knowledge
# APIs (search, dashboards, stats, …) remain always available.

_DRAFT_REQUIRED_TOOLS: set[str] = {
    "create_opencti_relationship",
    "create_opencti_observable",
    "create_opencti_note",
    "update_opencti_field",
    "update_opencti_relationship_field",
    "delete_opencti_entity",
    "delete_opencti_relationship",
    "add_opencti_label",
    "remove_opencti_label",
    "add_opencti_marking",
    "remove_opencti_marking",
    "create_opencti_indicator",
    "create_opencti_container",
    "create_opencti_entity",
    "add_opencti_to_container",
    "remove_opencti_from_container",
    "promote_opencti_observable",
}

_DRAFT_REQUIRED_MSG = (
    "Knowledge changes are staged in an OpenCTI draft for human review, and no draft "
    "is active yet. Do NOT reuse or pick an existing draft on your own - enriching a "
    "draft the user did not intend must be avoided (creating a new one is fine once no "
    "draft exists, see step 3).\n"
    "1. Discover existing drafts: check the conversation history for IDs from earlier "
    "`create_opencti_draft` calls, and call `list_opencti_drafts`.\n"
    "2. If one or more drafts exist, ASK the user whether to reuse one (present the list "
    "when there are several) or create a new one - never pick one yourself.\n"
    "3. Create a new draft (`create_opencti_draft`) only when the user asks for one or "
    "none exist yet; you may give it a short descriptive name yourself (no need to ask "
    "the user for the name).\n"
    "4. Activate the chosen draft with `use_opencti_draft`, then retry this action."
)

# The gate's answer once the draft the user opened in the OpenCTI UI is gone. Such
# a run offers no ``use_opencti_draft``: the next draft comes from the UI (the next
# message carries it) or is a new one the user asks for.
_UI_DRAFT_UNUSABLE_MSG = (
    "Knowledge changes are staged in an OpenCTI draft, and the draft the user has open "
    "in the OpenCTI UI is no longer usable (validated, deleted or locked) - a change is "
    "never sent without a draft. Tell the user: they can open another "
    "draft in OpenCTI and send the request again, or ask you to create a new draft "
    "(`create_opencti_draft`), then retry this action."
)

# ---------------------------------------------------------------------------
# Observable type mapping for creation
# Maps: display type → (GraphQL var name, GraphQL input type, value field)
# ---------------------------------------------------------------------------

_OBSERVABLE_TYPES: dict[str, tuple[str, str, str]] = {
    "Autonomous-System": ("AutonomousSystem", "AutonomousSystemAddInput", "number"),
    "Directory": ("Directory", "DirectoryAddInput", "path"),
    "Domain-Name": ("DomainName", "DomainNameAddInput", "value"),
    "Email-Addr": ("EmailAddr", "EmailAddrAddInput", "value"),
    "Hostname": ("Hostname", "HostnameAddInput", "value"),
    "IPv4-Addr": ("IPv4Addr", "IPv4AddrAddInput", "value"),
    "IPv6-Addr": ("IPv6Addr", "IPv6AddrAddInput", "value"),
    "Mac-Addr": ("MacAddr", "MacAddrAddInput", "value"),
    "Phone-Number": ("PhoneNumber", "PhoneNumberAddInput", "value"),
    "StixFile": ("StixFile", "StixFileAddInput", "name"),
    "Url": ("Url", "UrlAddInput", "value"),
    "User-Agent": ("UserAgent", "UserAgentAddInput", "value"),
    "Credential": ("Credential", "CredentialAddInput", "value"),
    "Cryptocurrency-Wallet": (
        "CryptocurrencyWallet",
        "CryptocurrencyWalletAddInput",
        "value",
    ),
    "Cryptographic-Key": ("CryptographicKey", "CryptographicKeyAddInput", "value"),
    "Tracking-Number": ("TrackingNumber", "TrackingNumberAddInput", "value"),
    "Text": ("Text", "TextAddInput", "value"),
    "Software": ("Software", "SoftwareAddInput", "name"),
    "Bank-Account": ("BankAccount", "BankAccountAddInput", "iban"),
    "Mutex": ("Mutex", "MutexAddInput", "name"),
}

# ---------------------------------------------------------------------------
# STIX pattern mapping for automatic indicator pattern generation
# Maps: observable type → (STIX SCO type, property name, is_integer_value)
# ---------------------------------------------------------------------------

_STIX_PATTERN_MAP: dict[str, tuple[str, str, bool]] = {
    "IPv4-Addr": ("ipv4-addr", "value", False),
    "IPv6-Addr": ("ipv6-addr", "value", False),
    "Domain-Name": ("domain-name", "value", False),
    "Url": ("url", "value", False),
    "Email-Addr": ("email-addr", "value", False),
    "Mac-Addr": ("mac-addr", "value", False),
    "Hostname": ("hostname", "value", False),
    "User-Agent": ("user-agent", "value", False),
    "Credential": ("credential", "value", False),
    "Phone-Number": ("phone-number", "value", False),
    "Cryptocurrency-Wallet": ("cryptocurrency-wallet", "value", False),
    "Cryptographic-Key": ("cryptographic-key", "value", False),
    "Tracking-Number": ("tracking-number", "value", False),
    "Text": ("text", "value", False),
    "Mutex": ("mutex", "name", False),
    "Directory": ("directory", "path", False),
    "Software": ("software", "name", False),
    "StixFile": ("file", "name", False),
    "Autonomous-System": ("autonomous-system", "number", True),
    "Bank-Account": ("bank-account", "iban", False),
}

_HASH_ALGORITHMS: dict[str, str] = {
    "MD5": "hashes.MD5",
    "SHA-1": "hashes.'SHA-1'",
    "SHA1": "hashes.'SHA-1'",
    "SHA-256": "hashes.'SHA-256'",
    "SHA256": "hashes.'SHA-256'",
    "SHA-512": "hashes.'SHA-512'",
    "SHA512": "hashes.'SHA-512'",
    "SSDEEP": "hashes.SSDEEP",
}


def _build_stix_pattern(
    observable_type: str,
    value: str,
    hash_algorithm: str = "",
) -> str | None:
    """Build a STIX 2.1 pattern string from an observable type and value.

    Returns ``None`` when the observable type is not recognized.
    """
    if hash_algorithm:
        prop = _HASH_ALGORITHMS.get(hash_algorithm.upper())
        if prop:
            return f"[file:{prop} = '{value}']"
        return None
    entry = _STIX_PATTERN_MAP.get(observable_type)
    if not entry:
        return None
    sco_type, prop, is_int = entry
    if is_int:
        return f"[{sco_type}:{prop} = {value}]"
    return f"[{sco_type}:{prop} = '{value}']"


# ---------------------------------------------------------------------------
# Container-type mutation mapping
# Maps: entity_type → (mutation_name, input_type_name)
# ---------------------------------------------------------------------------

_CONTAINER_MUTATIONS: dict[str, tuple[str, str]] = {
    "Report": ("reportAdd", "ReportAddInput"),
    "Case-Incident": ("caseIncidentAdd", "CaseIncidentAddInput"),
    "Case-Rfi": ("caseRfiAdd", "CaseRfiAddInput"),
    "Case-Rft": ("caseRftAdd", "CaseRftAddInput"),
    "Grouping": ("groupingAdd", "GroupingAddInput"),
}

#: The vocabulary field naming a case's own types, per case container type.
_CASE_TYPES_FIELDS: dict[str, str] = {
    "Case-Incident": "response_types",
    "Case-Rfi": "information_types",
    "Case-Rft": "takedown_types",
}

# ---------------------------------------------------------------------------
# SDO-type mutation mapping for generic domain object creation
# Maps: entity_type → (mutation_name, input_type_name)
# ---------------------------------------------------------------------------

_SDO_MUTATIONS: dict[str, tuple[str, str]] = {
    "Threat-Actor-Group": ("threatActorGroupAdd", "ThreatActorGroupAddInput"),
    "Threat-Actor-Individual": (
        "threatActorIndividualAdd",
        "ThreatActorIndividualAddInput",
    ),
    "Intrusion-Set": ("intrusionSetAdd", "IntrusionSetAddInput"),
    "Campaign": ("campaignAdd", "CampaignAddInput"),
    "Malware": ("malwareAdd", "MalwareAddInput"),
    "Tool": ("toolAdd", "ToolAddInput"),
    "Attack-Pattern": ("attackPatternAdd", "AttackPatternAddInput"),
    "Vulnerability": ("vulnerabilityAdd", "VulnerabilityAddInput"),
    "Incident": ("incidentAdd", "IncidentAddInput"),
    "Infrastructure": ("infrastructureAdd", "InfrastructureAddInput"),
    "Course-Of-Action": ("courseOfActionAdd", "CourseOfActionAddInput"),
    "Channel": ("channelAdd", "ChannelAddInput"),
    "Narrative": ("narrativeAdd", "NarrativeAddInput"),
    "Event": ("eventAdd", "EventAddInput"),
    "Individual": ("individualAdd", "IndividualAddInput"),
    "Organization": ("organizationAdd", "OrganizationAddInput"),
    "Sector": ("sectorAdd", "SectorAddInput"),
}

# ---------------------------------------------------------------------------
# GraphQL fragments – derived from pycti's entity property definitions
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GraphQL fragments – split by context to avoid invalid fragment spreading.
#
# OpenCTI type hierarchy:
#   BasicObject → StixObject → StixCoreObject → StixDomainObject
#                                              → StixCyberObservable
#
# StixDomainObject and StixCyberObservable are *siblings* under
# StixCoreObject — no type implements both.  GraphQL validation rejects
# inline fragments that can never match: ``... on StixCyberObservable``
# inside a StixDomainObject context (and vice-versa).
#
# We therefore split into three building blocks:
#   _BASE_FIELDS       – common scalars on BasicObject / StixObject
#   _SDO_INLINE_FIELDS – StixDomainObject interface + all SDO type fragments
#   _SCO_INLINE_FIELDS – StixCyberObservable interface + all SCO type fragments
#
# Composed fragments:
#   _ENTITY_FIELDS     = base + SDO + SCO  (for StixCoreObject contexts)
#   _SDO_ENTITY_FIELDS = base + SDO        (for StixDomainObject contexts)
#   _SCO_ENTITY_FIELDS = base + SCO        (for StixCyberObservable contexts)
# ---------------------------------------------------------------------------

_BASE_FIELDS = """
    id
    entity_type
    standard_id
    parent_types
    created_at
    updated_at
"""

_SDO_INLINE_FIELDS = """
    ... on StixDomainObject {
        created
        modified
        revoked
        confidence
        objectLabel { id value color }
        objectMarking {
            id
            standard_id
            definition_type
            definition
            x_opencti_order
            x_opencti_color
        }
        createdBy {
            ... on Identity { id name entity_type }
        }
        externalReferences {
            edges {
                node { id source_name url description external_id }
            }
        }
    }
    ... on AttackPattern {
        name description aliases x_mitre_id x_mitre_platforms
        killChainPhases { id kill_chain_name phase_name x_opencti_order }
    }
    ... on Campaign { name description aliases first_seen last_seen objective }
    ... on CourseOfAction { name description x_opencti_aliases }
    ... on DataComponent { name description }
    ... on DataSource { name description x_mitre_platforms collection_layers }
    ... on Event { name description aliases event_types }
    ... on Channel { name description aliases channel_types }
    ... on Grouping { name description context }
    ... on Incident {
        name description aliases first_seen last_seen severity objective
    }
    ... on Indicator {
        name description pattern pattern_type pattern_version
        valid_from valid_until x_opencti_score x_opencti_detection
        indicator_types x_opencti_main_observable_type
        killChainPhases { id kill_chain_name phase_name x_opencti_order }
    }
    ... on Infrastructure {
        name description aliases infrastructure_types first_seen last_seen
    }
    ... on IntrusionSet {
        name description aliases first_seen last_seen goals
        resource_level primary_motivation secondary_motivations
    }
    ... on Malware {
        name description aliases malware_types is_family first_seen last_seen
        architecture_execution_envs implementation_languages capabilities
        killChainPhases { id kill_chain_name phase_name x_opencti_order }
    }
    ... on MalwareAnalysis {
        product version result_name result
        analysis_started analysis_ended submitted
    }
    ... on Narrative { name description aliases narrative_types }
    ... on Note { attribute_abstract content authors note_types likelihood }
    ... on ObservedData { first_observed last_observed number_observed }
    ... on Opinion { explanation authors opinion }
    ... on Report { name description report_types published report_content: content }
    ... on ThreatActor {
        name description aliases threat_actor_types first_seen last_seen
        roles goals sophistication resource_level
        primary_motivation secondary_motivations personal_motivations
    }
    ... on Tool {
        name description aliases tool_types tool_version
        killChainPhases { id kill_chain_name phase_name x_opencti_order }
    }
    ... on Vulnerability {
        name description x_opencti_cvss_base_score
        x_opencti_cvss_base_severity x_opencti_cvss_attack_vector
        x_opencti_epss_score x_opencti_epss_percentile
        x_opencti_cisa_kev x_opencti_cwe x_opencti_score
    }
    ... on Individual {
        name description x_opencti_aliases contact_information
        x_opencti_firstname x_opencti_lastname
    }
    ... on Organization {
        name description x_opencti_aliases contact_information
        x_opencti_organization_type x_opencti_reliability
    }
    ... on Sector { name description x_opencti_aliases contact_information }
    ... on System { name description x_opencti_aliases }
    ... on City { name description latitude longitude x_opencti_aliases }
    ... on Country { name description latitude longitude x_opencti_aliases }
    ... on Region { name description latitude longitude x_opencti_aliases }
    ... on Position {
        name description latitude longitude
        street_address postal_code x_opencti_aliases
    }
    ... on Case { name description }
"""

_SCO_INLINE_FIELDS = """
    ... on StixCyberObservable {
        observable_value
        x_opencti_score
        x_opencti_description
        objectLabel { id value color }
        objectMarking {
            id standard_id definition_type definition
            x_opencti_order x_opencti_color
        }
        createdBy {
            ... on Identity { id name entity_type }
        }
    }
    ... on AutonomousSystem { number sco_name: name rir }
    ... on Directory { path path_enc ctime mtime atime }
    ... on DomainName { value }
    ... on EmailAddr { value display_name }
    ... on EmailMessage {
        is_multipart attribute_date content_type message_id subject body
    }
    ... on Artifact {
        mime_type url hashes { algorithm hash }
    }
    ... on StixFile {
        extensions size sco_name: name name_enc magic_number_hex mime_type
        hashes { algorithm hash } x_opencti_additional_names
    }
    ... on IPv4Addr { value }
    ... on IPv6Addr { value }
    ... on MacAddr { value }
    ... on Hostname { value }
    ... on Url { value }
    ... on UserAgent { value }
    ... on PhoneNumber { value }
    ... on Credential { value }
    ... on CryptocurrencyWallet { value }
    ... on CryptographicKey { value }
    ... on TrackingNumber { value }
    ... on Text { value }
    ... on BankAccount { iban bic account_number }
    ... on PaymentCard { card_number expiration_date holder_name }
    ... on Persona { persona_name persona_type }
    ... on MediaContent { title media_content: content url media_category publication_date }
    ... on Mutex { sco_name: name }
    ... on NetworkTraffic {
        start end is_active src_port dst_port protocols
    }
    ... on Process { is_hidden pid created_time cwd command_line }
    ... on Software { sco_name: name cpe vendor version }
    ... on UserAccount {
        user_id account_login account_type display_name
        is_service_account is_privileged
    }
    ... on WindowsRegistryKey { attribute_key modified_time }
    ... on X509Certificate {
        serial_number issuer subject
        validity_not_before validity_not_after
        hashes { algorithm hash }
    }
"""

# Combined: for StixCoreObject contexts (search, get-by-id)
_ENTITY_FIELDS = _BASE_FIELDS + _SDO_INLINE_FIELDS + _SCO_INLINE_FIELDS

# SDO-only: for stixDomainObjects queries
_SDO_ENTITY_FIELDS = _BASE_FIELDS + _SDO_INLINE_FIELDS

# SCO-only: for stixCyberObservables queries
_SCO_ENTITY_FIELDS = _BASE_FIELDS + _SCO_INLINE_FIELDS

# Relationship fragment with from/to entity resolution
_RELATIONSHIP_FIELDS = """
    id
    entity_type
    standard_id
    relationship_type
    description
    start_time
    stop_time
    revoked
    confidence
    created
    modified
    created_at
    updated_at
    objectLabel { id value color }
    objectMarking {
        id standard_id definition_type definition
        x_opencti_order x_opencti_color
    }
    createdBy {
        ... on Identity { id name entity_type }
    }
    from {
        ... on BasicObject { id entity_type parent_types }
        ... on StixObject { standard_id }
        ... on AttackPattern { name x_mitre_id }
        ... on Campaign { name }
        ... on CourseOfAction { name }
        ... on Individual { name }
        ... on Organization { name }
        ... on Sector { name }
        ... on System { name }
        ... on Indicator { name }
        ... on Infrastructure { name }
        ... on IntrusionSet { name }
        ... on Position { name }
        ... on City { name }
        ... on Country { name }
        ... on Region { name }
        ... on Malware { name }
        ... on ThreatActor { name }
        ... on Tool { name }
        ... on Vulnerability { name }
        ... on Incident { name }
        ... on Event { name }
        ... on Channel { name }
        ... on Narrative { name }
        ... on DataComponent { name }
        ... on DataSource { name }
        ... on Case { name }
        ... on StixCyberObservable { observable_value }
    }
    to {
        ... on BasicObject { id entity_type parent_types }
        ... on StixObject { standard_id }
        ... on AttackPattern { name x_mitre_id }
        ... on Campaign { name }
        ... on CourseOfAction { name }
        ... on Individual { name }
        ... on Organization { name }
        ... on Sector { name }
        ... on System { name }
        ... on Indicator { name }
        ... on Infrastructure { name }
        ... on IntrusionSet { name }
        ... on Position { name }
        ... on City { name }
        ... on Country { name }
        ... on Region { name }
        ... on Malware { name }
        ... on ThreatActor { name }
        ... on Tool { name }
        ... on Vulnerability { name }
        ... on Incident { name }
        ... on Event { name }
        ... on Channel { name }
        ... on Narrative { name }
        ... on DataComponent { name }
        ... on DataSource { name }
        ... on Case { name }
        ... on StixCyberObservable { observable_value }
    }
"""

# ---------------------------------------------------------------------------
# CONSTANTS for NLQ filter validation
# ---------------------------------------------------------------------------

RULE_PREFIX = "i_rule_"
ABSTRACT_STIX_CORE_OBJECT = "Stix-Core-Object"
ABSTRACT_STIX_DOMAIN_OBJECT = "Stix-Domain-Object"
ABSTRACT_STIX_CYBER_OBSERVABLE = "Stix-Cyber-Observable"
ABSTRACT_STIX_META_OBJECT = "Stix-Meta-Object"
ENTITY_TYPE_USER = "User"
INSTANCE_REGARDING_OF = "regardingOf"

# ---------------------------------------------------------------------------
# Helpers for NLQ filter validation
# ---------------------------------------------------------------------------

RELATIVE_DATE_RE = re.compile(r"^now([-+]\d+[smhHdwMy](/[smhHdwMy])?)?$")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Substrings that identify a GraphQL-level authentication failure in an OpenCTI
#: ``errors`` payload. OpenCTI does NOT answer an expired bearer token with an
#: HTTP 401: it returns ``200 OK`` carrying ``AUTH_REQUIRED`` /
#: "You must be logged in to do this" in the GraphQL ``errors`` array. Matched
#: against the error ``message`` and its ``extensions.code``, both lower-cased.
#:
#: Deliberately narrow. ``FORBIDDEN_ACCESS`` (a genuine capability denial) is
#: NOT listed: re-minting a token for the same identity cannot grant a missing
#: capability, so retrying would only repeat the call - and repeat any side
#: effect it already had.
_AUTH_ERROR_MARKERS: tuple[str, ...] = (
    "you must be logged in to do this",
    "auth_required",
)


def _client(
    headers: Any,
    timeout: int = 30,
    *,
    follow_redirects: bool = False,
) -> AutoRefreshClient | httpx.AsyncClient:
    """Open an HTTP client for one OpenCTI call, honouring token re-mints.

    THE client factory for this provider - every GraphQL query, mutation, upload
    and file download goes through it. Funnelling them all here is what stops the
    next tool added from quietly regressing to a bare ``httpx.AsyncClient``, i.e.
    to a client pinned to the JWT that existed at tool-build time.

    A :class:`RefreshableHeaders` (built by ``build_tools`` for a
    platform-managed integration) carries the cross-platform JWT re-minter, and
    gets an :class:`AutoRefreshClient`: a 401/403 re-mints and retries once, and
    :func:`_graphql` can force that same single re-mint when OpenCTI reports the
    auth failure inside a ``200 OK`` body instead. A static-API-key integration
    has no callback and keeps the plain short-lived client.
    """
    refresh_fn = getattr(headers, "refresh_fn", None)
    if refresh_fn is None:
        return egress_client(timeout=timeout, follow_redirects=follow_redirects)
    return AutoRefreshClient(
        headers,
        refresh_fn,
        timeout=timeout,
        follow_redirects=follow_redirects,
    )


async def _refresh_if_stale(headers: Any) -> None:
    """Proactively re-mint the JWT in *headers* when it is close to expiring.

    No-op unless *headers* is a :class:`RefreshableHeaders` whose bearer token is
    a JWT inside the refresh skew, so a static-API-key integration is untouched.
    Concurrent callers coalesce onto one re-mint, and the refreshed token is
    written back into the shared dict every later call reads from.
    """
    refresh_fn = getattr(headers, "refresh_fn", None)
    if refresh_fn is not None:
        await ensure_fresh_token(headers, refresh_fn)


async def _force_remint(client: Any) -> bool:
    """Spend *client*'s single token re-mint after a body-level auth failure.

    Returns ``True`` when the shared headers now carry a new token and the caller
    should retry. The re-mint budget lives on the client instance and is shared
    with its 401/403 path, so one OpenCTI call re-mints at most once however the
    auth failure surfaced. Returns ``False`` for a plain ``httpx.AsyncClient``
    (no ``refresh_once``), i.e. for a static-API-key integration.
    """
    refresh_once = getattr(client, "refresh_once", None)
    if refresh_once is None:
        return False
    return bool(await refresh_once())


def _is_auth_error(errors: Any) -> bool:
    """True when a GraphQL ``errors`` payload means "this token is not valid"."""
    if not isinstance(errors, list):
        return False
    for err in errors:
        if not isinstance(err, dict):
            continue
        haystack = str(err.get("message", "")).lower()
        extensions = err.get("extensions")
        if isinstance(extensions, dict):
            haystack = f"{haystack} {str(extensions.get('code', '')).lower()}"
        if any(marker in haystack for marker in _AUTH_ERROR_MARKERS):
            return True
    return False


def _format_graphql_errors(errors: list[Any]) -> str:
    return "; ".join(
        e.get("message", str(e)) if isinstance(e, dict) else str(e) for e in errors
    )


# ---------------------------------------------------------------------------
# GraphQL failure reporting
#
# Every OpenCTI tool funnels through ``_graphql``, so a failure used to reach
# the analyst as a bare ``"GraphQL error: You are not allowed to do this."`` —
# with no indication of WHICH of the (often several) GraphQL calls a handler
# makes was refused, nor which capability the token is missing.  The operation
# name and the capability hint are therefore derived here, once, rather than at
# the ~57 call sites.
#
# ``_format_graphql_errors`` above stays the plain message join used by the
# auth-retry log; ``_describe_graphql_failure`` below is the enriched,
# analyst-facing rendering that becomes the tool result.
# ---------------------------------------------------------------------------

# Named operation: ``mutation AddLabel($id: ID!, …) { … }`` -> ``AddLabel``.
# Every query in this package is named except a handful of trigger mutations
# in ``notifications.py``, which fall through to the root-field regex below.
_GRAPHQL_OP_NAME_RE = re.compile(
    r"\b(?:query|mutation|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)"
)

# First field of the top-level selection set — OpenCTI's OWN API name
# (``stixDomainObjectEdit``, ``labelAdd``, ``userPatch`` …).  Variable
# definitions carry no braces, so the first ``{`` always opens the selection
# set.  This is the reliable signal for the capability mapping: the operation
# names in this package are hand-written abbreviations (``StAdd``,
# ``ExclPatch``) that no mapping could follow.
_GRAPHQL_ROOT_FIELD_RE = re.compile(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)")

_UNNAMED_OPERATION = "unnamed operation"

# A document that may write: ``mutation`` (and ``subscription``, never sent
# here) anywhere in it. Deliberately broad: a read that merely mentions the
# word loses the transient retry, a write is never replayed.
_GRAPHQL_WRITE_RE = re.compile(r"\b(?:mutation|subscription)\b")


def _is_read_document(query: str) -> bool:
    """True when *query* is a read (``query`` or anonymous ``{ ... }``).

    Only a read opts into the 502/503/504 retry of ``safe_request``: a proxy
    error in front of OpenCTI may hide a mutation that was applied, and
    replaying it would apply it twice.
    """
    return _GRAPHQL_WRITE_RE.search(query) is None


# OpenCTI's exact wording / error code for a capability refusal — the opposite
# remediation from ``_AUTH_ERROR_MARKERS`` above (which means "this token is
# not valid", and is what ``_is_auth_error`` re-mints on).  Kept to exact
# phrasings: a loose marker such as "restricted" also matches benign reference
# and validation errors, which must not be reported as permission failures
# (they would trip the agent loop's long auth cooldown).
_FORBIDDEN_MARKERS = (
    "you are not allowed to do this",
    "missing capability",
)
_FORBIDDEN_CODES = frozenset({"FORBIDDEN_ACCESS"})

# Root-field fragment -> OpenCTI capability the token's role needs, ordered
# most-specific-first (the first fragment found in the lowercased root field
# wins).  Only families whose requirement this codebase already asserts in a
# tool description, or that OpenCTI documents unambiguously, are listed —
# anything else falls back to the generic "check the role" hint rather than
# naming a capability that might be wrong.
#
# MAINTENANCE: adding a tool whose root field belongs to a NEW domain means
# adding its capability here, or its permission errors fall back to the
# generic hint.  Referenced from ``dev-docs/reference/backend-integrations-providers.md`` (OpenCTI row).
_CAPABILITY_BY_ROOT_FIELD: tuple[tuple[str, str], ...] = (
    # Access management — see ``access.py`` tool descriptions.
    ("capabilit", "SETTINGS_SETACCESSES"),
    ("user", "SETTINGS_SETACCESSES"),
    ("group", "SETTINGS_SETACCESSES"),
    ("role", "SETTINGS_SETACCESSES"),
    ("session", "SETTINGS_SETACCESSES"),
    # Customization — see ``customization.py`` tool descriptions.
    ("vocabular", "SETTINGS_SETVOCABULARIES"),
    ("entitysetting", "SETTINGS_SETCUSTOMIZATION"),
    ("subtype", "SETTINGS_SETCUSTOMIZATION"),
    ("statustemplate", "SETTINGS_SETCUSTOMIZATION"),
    # Taxonomy objects live under their own settings capabilities.
    ("markingdefinition", "SETTINGS_SETMARKINGS"),
    ("label", "SETTINGS_SETLABELS"),
    # Platform parameters — decay / retention / exclusion rules.
    ("decayrule", "SETTINGS_SETPARAMETERS"),
    ("retentionrule", "SETTINGS_SETPARAMETERS"),
    ("exclusionlist", "SETTINGS_SETPARAMETERS"),
    ("settings", "SETTINGS_SETPARAMETERS"),
    # Activity/audit trail — see ``notifications.py`` / ``audit.py``.  The
    # bare ``logs`` root field is deliberately NOT mapped: it serves both the
    # security-activity trail and plain entity history (KNOWLEDGE), so naming
    # one capability would be wrong half the time.
    ("audit", "SETTINGS_SECURITYACTIVITY"),
    # Dashboards, investigations, public dashboards.
    ("publicdashboard", "EXPLORE_EXUPDATE_PUBLISH"),
    ("workspace", "EXPLORE_EXUPDATE"),
    # Connectors / managed-connector deployment.
    ("connector", "MODULES_MODMANAGE"),
    ("catalog", "MODULES_MODMANAGE"),
    ("synchronizer", "MODULES_MODMANAGE"),
    # Ingestion — see ``ingestion.py`` tool descriptions.
    ("ingestion", "INGESTION_SETINGESTIONS"),
    ("csvmapper", "INGESTION_SETINGESTIONS"),
    ("form", "INGESTION_SETINGESTIONS"),
    # Data sharing out — see ``data_sharing.py`` tool descriptions.
    ("taxii", "TAXIIAPI_SETCOLLECTIONS"),
    ("stream", "TAXIIAPI_SETCOLLECTIONS"),
    ("feed", "TAXIIAPI_SETCSVFEEDS"),
    # PIR — see ``pir.py`` tool descriptions.
    ("pir", "PIRAPI"),
)

# STIX knowledge root fields: the capability depends on what the mutation does
# rather than on the object family, so they are resolved separately.
_KNOWLEDGE_ROOT_FIELD_FRAGMENTS = (
    "stix",
    "indicator",
    "observable",
    "container",
    "note",
    "opinion",
    "report",
    "relationship",
)

_DELETE_FIELD_RE = re.compile(r"\bdelete\b")
_MERGE_FIELD_RE = re.compile(r"\bmerge\b")

_CAPABILITY_CHECK_HINT = (
    "Inspect the token's role with list_opencti_capabilities / "
    "manage_opencti_role."
)


def _operation_names(query: str) -> tuple[str, str]:
    """Return ``(operation_label, root_field)`` for a GraphQL document.

    ``operation_label`` is what the analyst sees in the error ("which of this
    handler's calls failed"); ``root_field`` drives the capability mapping.
    Either falls back to the other when a document is anonymous.
    """
    root_match = _GRAPHQL_ROOT_FIELD_RE.search(query)
    root_field = root_match.group(1) if root_match else ""
    name_match = _GRAPHQL_OP_NAME_RE.search(query)
    label = name_match.group(1) if name_match else (root_field or _UNNAMED_OPERATION)
    return label, root_field


def _required_capability(root_field: str, query: str) -> str | None:
    """Best-effort OpenCTI capability required by a GraphQL root field.

    Returns ``None`` when the family is not one whose requirement is
    established — the caller then emits the generic "check the role" hint
    instead of naming a capability that could be wrong.
    """
    lowered = root_field.lower()
    if not lowered:
        return None
    for fragment, capability in _CAPABILITY_BY_ROOT_FIELD:
        if fragment in lowered:
            return capability
    if not any(f in lowered for f in _KNOWLEDGE_ROOT_FIELD_FRAGMENTS):
        return None
    # Knowledge mutations: OpenCTI grades write access by the operation kind.
    if lowered.endswith(("add", "edit", "patch")) or "add" in lowered:
        if _MERGE_FIELD_RE.search(query):
            return "KNOWLEDGE_KNUPDATE_KNMERGE"
        if _DELETE_FIELD_RE.search(query):
            return "KNOWLEDGE_KNUPDATE_KNDELETE"
        return "KNOWLEDGE_KNUPDATE"
    return "KNOWLEDGE"


def _permission_hint(
    errors: list[Any],
    root_field: str,
    query: str,
    message: str,
    codes: set[str],
    *,
    auth_failure: bool = False,
) -> str:
    """Actionable suffix for an auth/permission GraphQL failure (``""`` if not one).

    Two distinct failures wear almost identical wording in OpenCTI and need
    OPPOSITE remediations: a token that is not valid (re-authorize / re-mint)
    and a role missing a capability (grant the capability).  The first is
    delegated to ``_is_auth_error`` - or to the caller's *auth_failure*, the
    ``_is_request_auth_failure`` verdict ``_graphql`` re-mints on - so this hint
    and the re-mint retry can never disagree about what "not authenticated"
    means.
    """
    if auth_failure or _is_auth_error(errors):
        return (
            " — unauthorized: the OpenCTI API token was rejected (missing, "
            "expired or revoked). The user should re-authorize this "
            "integration."
        )
    lowered = message.lower()
    if not (
        codes & _FORBIDDEN_CODES
        or any(marker in lowered for marker in _FORBIDDEN_MARKERS)
    ):
        return ""
    capability = _required_capability(root_field, query)
    named = f" '{root_field}'" if root_field else ""
    if capability:
        return (
            f" — permission denied: the token's role lacks the capability "
            f"required for{named}, typically {capability}. "
            f"{_CAPABILITY_CHECK_HINT}"
        )
    return (
        f" — permission denied: the token's role lacks the capability required "
        f"for{named}. {_CAPABILITY_CHECK_HINT}"
    )


def _describe_graphql_failure(
    errors: list[Any],
    operation: str,
    root_field: str,
    query: str,
    *,
    kind: str = "error",
    auth_failure: bool = False,
) -> str:
    """Render GraphQL ``errors`` into one error-shaped, self-describing string.

    The ``"GraphQL error"`` / ``"GraphQL upload error"`` lead-in is recognised
    by ``app.tools.base.looks_like_tool_error``, so the resulting tool result
    is flagged ``is_error=True`` on the trace span, on the ``tool_done`` event
    and in the agent loop's per-tool error counters.
    """
    messages: list[str] = []
    codes: set[str] = set()
    for err in errors:
        if not isinstance(err, dict):
            messages.append(str(err))
            continue
        message = err.get("message") or str(err)
        extensions = err.get("extensions")
        if isinstance(extensions, dict):
            code = extensions.get("code")
            if code:
                codes.add(str(code))
                message = f"{message} [{code}]"
            # OpenCTI puts the concrete reason (missing reference, forbidden
            # field, …) in ``extensions.data`` — the only server-provided clue
            # about WHAT was refused, so it must not be dropped.
            data = extensions.get("data")
            if data:
                message = f"{message} {json.dumps(data, default=str)[:300]}"
        messages.append(message)
    combined = "; ".join(messages)
    hint = _permission_hint(
        errors, root_field, query, combined, codes, auth_failure=auth_failure
    )
    return f"GraphQL {kind} in '{operation}': {combined}{hint}"


def format_bulk_outcome(
    verb_past: str,
    verb_infinitive: str,
    target: str,
    succeeded: list[str],
    failures: list[str],
) -> str:
    """Render a loop-over-ids result so a PARTIAL failure reads as a failure.

    ``add_opencti_to_container`` used to concatenate ``"Added 3 object(s)…
    Errors: <id>: GraphQL error …"`` into one string starting with ``"Added"``,
    which the agent loop recorded as an unqualified success.  Leading
    with ``"Failed to …"`` / ``"Partial failure:"`` — both recognised by
    ``app.tools.base.looks_like_tool_error`` — keeps the successful ids fully
    visible while making the outcome honest.

    The two lead-ins are load-bearing for that classifier; see
    ``tests/test_tool_error_shaped_results.py`` for the test that keeps this
    wording and ``base._ERROR_PREFIXES`` in sync.
    """
    total = len(succeeded) + len(failures)
    if not total:
        return f"No objects were {verb_past.lower()}."
    if not failures:
        return f"{verb_past} {total} object(s) {target}: {', '.join(succeeded)}"
    failed_line = f"Failed ({len(failures)}): {'; '.join(failures)}"
    if not succeeded:
        return f"Failed to {verb_infinitive} {total} object(s) {target}.\n{failed_line}"
    return (
        f"Partial failure: {len(succeeded)} of {total} object(s) "
        f"{verb_past.lower()} {target}, {len(failures)} failed.\n"
        f"{verb_past} ({len(succeeded)}): {', '.join(succeeded)}\n{failed_line}"
    )


# ---------------------------------------------------------------------------
# Request-context failures: HTTP 500 with a GraphQL body
#
# OpenCTI answers HTTP 200 for every error raised inside the GraphQL pipeline
# (its ``httpResponsePlugin`` forces it). A 500 means the request failed BEFORE
# the pipeline, in Apollo's context factory, which is where OpenCTI checks the
# request's draft (``checkDraftInContext``). Apollo answers those with its
# default 500 because OpenCTI's errors carry ``extensions.data.http_status``, not
# ``extensions.http`` - but the JSON body still names the cause. With an
# ``opencti-draft-id`` header the context factory fails, on EVERY later call
# carrying that header, when:
#   * the user could not be authenticated (expired / invalid token): the auth
#     failure is swallowed and the draft check throws "User cannot be found" -
#     where a request WITHOUT the header would have reached the resolvers and
#     answered 200 + AUTH_REQUIRED, which the re-mint below already handles;
#   * the draft is missing from the platform cache, not accessible to the user,
#     or no longer open (validated / locked).
# ---------------------------------------------------------------------------

#: Non-2xx statuses whose body is read for GraphQL ``errors`` (see above).
_CONTEXT_FAILURE_STATUSES = frozenset({500})

_DRAFT_HEADER = "opencti-draft-id"

# A dead draft, as worded by OpenCTI's context check (``DRAFT_LOCKED`` code,
# "Could not find draft workspace", "Draft <id> cannot be found ...", "Cannot
# execute request in a draft that is not in an open state").
_DEAD_DRAFT_MARKERS = (
    "could not find draft",
    "cannot be found",
    "not in an open state",
    "locked",
)


def _json_errors(resp: Any) -> list[Any] | None:
    """The GraphQL ``errors`` array of a failed response, if it has one."""
    try:
        payload = resp.json()
    except Exception:
        return None
    errors = payload.get("errors") if isinstance(payload, dict) else None
    return errors if isinstance(errors, list) and errors else None


def _errors_text(errors: list[Any]) -> str:
    """Lowercased messages + codes of a GraphQL ``errors`` array."""
    parts: list[str] = []
    for err in errors:
        if not isinstance(err, dict):
            parts.append(str(err))
            continue
        parts.append(str(err.get("message", "")))
        extensions = err.get("extensions")
        if isinstance(extensions, dict) and extensions.get("code"):
            parts.append(str(extensions["code"]))
    return " ".join(parts).lower()


def _is_request_auth_failure(
    errors: list[Any], status: int | None, draft_id: str | None
) -> bool:
    """True when the request failed because the token was not accepted.

    *draft_id* is the draft the request carried: "User cannot be found" is the
    swallowed auth failure only on a draft-scoped request.
    """
    if _is_auth_error(errors):
        return True
    return (
        status is not None
        and bool(draft_id)
        and "user cannot be found" in _errors_text(errors)
    )


def _is_dead_draft(errors: list[Any]) -> bool:
    text = _errors_text(errors)
    if "draft_locked" in text:
        return True
    return "draft" in text and any(marker in text for marker in _DEAD_DRAFT_MARKERS)


#: Attribute set on the shared :class:`RefreshableHeaders` when a draft was
#: rejected. An attribute, not a key: every key of that dict is sent as an HTTP
#: header.
_REVOKED_DRAFT_ATTR = "revoked_opencti_draft"

_MUTATION_RE = re.compile(r"^\s*mutation\b")

# Mutations that may run while a revoked draft awaits its replacement: creating
# the replacement itself, and validating or deleting a draft (each names its own
# draft by id, none writes knowledge).
_DRAFT_LIFECYCLE_ROOT_FIELDS = frozenset(
    {"draftWorkspaceAdd", "draftWorkspaceValidate", "draftWorkspaceDelete"}
)

# Root fields on draft workspaces themselves, which live in the live graph and
# never in a draft: they run in the live graph even while the run works in a
# draft, where creating one fails ("Cannot index unsupported element in draft
# context"). A document is routed by its FIRST root field
# (_operation_names): keep a draft workspace field out of a document that
# selects other roots, and add any new draft workspace root field here.
_LIVE_GRAPH_ROOT_FIELDS = frozenset(
    {
        "draftWorkspaceAdd",
        "draftWorkspace",
        "draftWorkspaces",
        "draftWorkspaceValidate",
        "draftWorkspaceDelete",
    }
)

# Root fields on the acting account and the platform (the capability probes,
# the account lookups), the same in every draft: read in the live graph, so a
# dead draft is reported by the tool call that meets it, never dropped first
# by a probe whose failure is only logged.
_ACCOUNT_ROOT_FIELDS = frozenset({"me", "about"})


def _drop_dead_draft(headers: Any, draft_id: str, errors: list[Any]) -> str:
    """Stop sending *draft_id* once OpenCTI rejects it, and say what happened.

    The draft id lives in the ``headers`` dict every tool of this build shares,
    so leaving it there failed every later call - reads included - for the rest
    of the run. Changes stay draft-staged: the headers are marked revoked, and
    :func:`_revoked_draft_refusal` refuses every mutation - including the next
    iterations of a multi-step tool already running - until a new draft is
    active (the knowledge draft gate asks for one, or creates one in an
    autonomous run). A draft that replaced *draft_id* while this request was in
    flight is left alone.
    """
    reason = _format_graphql_errors(errors)
    lead = (
        f"Error: the OpenCTI draft {draft_id} is no longer usable ({reason}): it "
        "was validated, deleted or locked, or this account cannot access it. "
        "Entities created in it cannot be read through it any more."
    )
    current = headers.get(_DRAFT_HEADER)
    if current and current != draft_id:
        return f"{lead} This run now uses draft {current}."
    try:
        setattr(headers, _REVOKED_DRAFT_ATTR, draft_id)
    except AttributeError:
        # A plain dict cannot carry the mark: keep the draft (every call keeps
        # failing) rather than let a change reach the live graph.
        return f"{lead} Create or select another draft before retrying."
    headers.pop(_DRAFT_HEADER, None)
    return (
        f"{lead} It is no longer used for this run: later reads leave the draft, "
        "and no change is sent until a new draft is active."
    )


def _revoked_draft_refusal(headers: Any, draft_id: str | None, query: str) -> str | None:
    """Refuse a mutation while a rejected draft awaits its replacement."""
    revoked = getattr(headers, _REVOKED_DRAFT_ATTR, None)
    if draft_id or not revoked or not _MUTATION_RE.match(query):
        return None
    _, root_field = _operation_names(query)
    if root_field in _DRAFT_LIFECYCLE_ROOT_FIELDS:
        return None
    return (
        f"Error: not sent - the OpenCTI draft {revoked} was rejected earlier in "
        "this run, and a change is never sent without a draft. "
        "Create a new draft (create_opencti_draft), then retry."
    )


def _request_headers(
    headers: Any, draft_id: str | None, *, live: bool = False
) -> dict[str, str]:
    """The headers one request sends: the shared ones, with *draft_id* pinned.

    The tools of one batch run concurrently on the shared ``headers``. Copied per
    attempt, they carry a re-minted ``Authorization``, while the draft stays the
    one the call started in: a concurrent call may drop (or replace) the shared
    draft while this one awaits a token refresh, a 429 back-off or its re-mint
    replay, and a mutation sent without its draft would reach the live graph.

    The header is always sent. Without it OpenCTI does not use the live
    graph but the account's own draft, the one last entered in the OpenCTI UI:
    a run with no draft read that draft's unapproved changes, wrote into it
    (the platform MCP servers' direct mutations), and failed to create anything
    a draft cannot hold (a dashboard, a draft workspace). An empty header is the
    live graph: OpenCTI falls back to the account's draft only when the header
    is absent. *live* sends the request there even while the run works in a
    draft (:data:`_LIVE_GRAPH_ROOT_FIELDS`).
    """
    sent = {k: v for k, v in headers.items() if k != _DRAFT_HEADER}
    sent[_DRAFT_HEADER] = "" if live else draft_id or ""
    return sent


def _context_failure_message(
    errors: list[Any],
    status: int | None,
    headers: Any,
    draft_id: str | None,
    operation: str,
    root_field: str,
    query: str,
    kind: str,
) -> str:
    """The tool result for a GraphQL failure, a context-level one included.

    A dead draft is this run's only when the request carried it (*draft_id*): a
    request that named no draft ran in the live graph, and has none to drop.
    """
    if status is not None and draft_id and _is_dead_draft(errors):
        return _drop_dead_draft(headers, draft_id, errors)
    if status is not None:
        kind = f"{kind} (HTTP {status})"
    message = _describe_graphql_failure(
        errors,
        operation,
        root_field,
        query,
        kind=kind,
        auth_failure=_is_request_auth_failure(errors, status, draft_id),
    )
    if draft_id and _is_object_outside_draft(errors):
        message += OBJECT_OUTSIDE_DRAFT_HINT.format(draft_id=draft_id)
    return message


#: Appended to an object OpenCTI could not find inside the run's draft (#2397).
OBJECT_OUTSIDE_DRAFT_HINT = (
    " — the object is not visible in OpenCTI draft {draft_id}, the draft this request ran in: OpenCTI "
    "resolves an object per draft, so an id a read outside this draft returned may live in another draft "
    "(a case package's own draft, for one) or was deleted. Write it in the draft that holds it, or read it "
    "again in this draft and recreate it here; nothing was written."
)


def _is_object_outside_draft(errors: list[Any]) -> bool:
    """An object (not the draft itself) OpenCTI did not find in a draft-scoped request (#2397)."""
    text = _errors_text(errors)
    return "cannot be found" in text and "draft" not in text and "user cannot be found" not in text


def _log_context_failure(
    resp: Any,
    operation: str,
    root_field: str,
    sent_headers: Any,
    errors: list[Any] | None,
) -> None:
    """One diagnostic line per HTTP 500 from OpenCTI.

    Enough to tell the causes above apart after the fact: whether the request
    carried a draft header, OpenCTI's own code and message, and how long the
    bearer JWT it sent had left (never the token itself).
    """
    remaining = bearer_seconds_remaining(sent_headers)
    if errors:
        codes = sorted(
            {
                str(e["extensions"]["code"])
                for e in errors
                if isinstance(e, dict)
                and isinstance(e.get("extensions"), dict)
                and e["extensions"].get("code")
            }
        )
        detail = f"code={','.join(codes) or '-'} message={_format_graphql_errors(errors)[:300]}"
    else:
        detail = f"body={str(getattr(resp, 'text', '') or '')[:300]!r}"
    draft = sent_headers.get(_DRAFT_HEADER)
    logger.warning(
        "OpenCTI HTTP %s on '%s' (root %s) | draft=%s | %s | bearer_expires_in=%s",
        getattr(resp, "status_code", "?"),
        operation,
        root_field or "?",
        draft or ("live" if draft == "" else "none"),
        detail,
        f"{remaining:.0f}s" if remaining is not None else "n/a",
    )


async def _graphql(
    client: httpx.AsyncClient | AutoRefreshClient,
    base_url: str,
    headers: dict[str, str],
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    live: bool = False,
) -> tuple[dict, str | None]:
    """Execute a GraphQL query against the OpenCTI API.

    *live* sends the request to the live graph whatever draft the run works
    in, like the root fields of :data:`_LIVE_GRAPH_ROOT_FIELDS`: the platform
    configuration tools (a label created as configuration) pass it.

    Returns ``(data_dict, None)`` on success, or ``({}, error_string)``
    on failure (HTTP error, network error, or GraphQL-level error).

    A platform-managed integration's cross-platform JWT lives ~10 minutes, far
    less than an import or harvesting run, so the token is kept valid on both
    sides of the request: the JWT is re-minted before sending if it is close to
    expiring, and an ``AUTH_REQUIRED`` answer (which OpenCTI returns as a
    ``200 OK`` GraphQL error, never as an HTTP 401, so ``AutoRefreshClient``
    cannot see it) re-mints once and replays the query. Both are no-ops for a
    static API key.

    The error string names the failing operation and, for permission failures,
    the capability the token's role is missing — a handler such as
    ``add_opencti_label`` makes three GraphQL calls, and a bare ``"GraphQL
    error: You are not allowed to do this."`` left the analyst no way to tell
    which step was refused.
    """
    operation, root_field = _operation_names(query)
    # Unattended runs and background CTEM builds carry a capability probe: a
    # document the acting user's role cannot pass is never sent, or OpenCTI
    # records it as their unauthorized read (capabilities.unsent_reason).
    if getattr(headers, "capability_probe", None) is not None:
        from .capabilities import unsent_reason

        if (reason := await unsent_reason(headers, query)) is not None:
            logger.info("OpenCTI %s not sent: %s", operation, reason)
            return {}, reason
    # A draft workspace operation runs in no draft; any other call runs in the
    # draft it started in, fixed for all its attempts (_request_headers).
    # A configuration write the caller sends live on purpose is no knowledge
    # change: a rejected draft does not hold it back.
    configuration = live
    live = live or root_field in _LIVE_GRAPH_ROOT_FIELDS or root_field in _ACCOUNT_ROOT_FIELDS
    draft_id = None if live else headers.get(_DRAFT_HEADER)
    if not configuration and (refusal := _revoked_draft_refusal(headers, draft_id, query)):
        return {}, refusal
    body: dict[str, Any] = {"query": query}
    if variables:
        body["variables"] = variables
    # A proxy 502/503/504 in front of OpenCTI is usually gone in seconds: a
    # read retries it with backoff, a mutation never does.
    retry_statuses = TRANSIENT_RETRY_STATUSES if _is_read_document(query) else None

    async def _attempt() -> tuple[dict, str | None, list[Any] | None, int | None]:
        """Returns ``(data, error, errors_payload, http_status)``.

        *errors_payload* is set for GraphQL errors - in a ``200`` body, or in the
        body of an HTTP 500 (*http_status* then says so, see
        :data:`_CONTEXT_FAILURE_STATUSES`).
        """
        await _refresh_if_stale(headers)
        sent_headers = _request_headers(headers, draft_id, live=live)
        resp, err = await safe_request(
            client,
            "post",
            f"{base_url}/graphql",
            _PROVIDER,
            f"execute GraphQL operation '{operation}'",
            headers=sent_headers,
            json=body,
            expected_statuses=_CONTEXT_FAILURE_STATUSES,
            retry_statuses=retry_statuses,
        )
        if err:
            # ``expected_statuses`` hands back the response of ANY failed
            # status; only the context-failure 500 is read here.
            if resp is not None and resp.status_code in _CONTEXT_FAILURE_STATUSES:
                # The body is where OpenCTI says WHY; the generic "server
                # error (500) ... temporarily unavailable" string is not.
                errors = _json_errors(resp)
                _log_context_failure(resp, operation, root_field, sent_headers, errors)
                if errors:
                    return {}, None, errors, resp.status_code
            return {}, err, None, None
        if resp is None:
            raise RuntimeError(
                "OpenCTI request unexpectedly returned no response after a successful safe_request call."
            )
        data = resp.json()
        errors = data.get("errors")
        if errors:
            return {}, None, errors, None
        return data.get("data", {}), None, None, None

    data, err, errors, status = await _attempt()
    if errors and _is_request_auth_failure(errors, status, draft_id) and await _force_remint(client):
        # An auth failure means the query never ran, so replaying it - even a
        # mutation - cannot double any side effect.
        logger.info(
            "OpenCTI GraphQL auth error (%s) — re-minted the token, retrying once",
            _format_graphql_errors(errors),
        )
        data, err, errors, status = await _attempt()
    if errors:
        message = _context_failure_message(
            errors, status, headers, draft_id, operation, root_field, query, "error"
        )
        logger.warning(
            "OpenCTI %s | query preview: %.120s", message, query.strip()[:120]
        )
        return {}, message
    return data, err


async def _graphql_upload(
    client: httpx.AsyncClient | AutoRefreshClient,
    base_url: str,
    auth_headers: dict[str, str],
    query: str,
    variables: dict[str, Any],
    file_var_path: str,
    file_name: str,
    file_bytes: bytes,
    mime_type: str,
) -> tuple[dict, str | None]:
    """Execute a GraphQL mutation with a file upload (multipart request spec).

    Follows https://github.com/jaydenseric/graphql-multipart-request-spec.

    Carries the same two-sided JWT handling as :func:`_graphql` - a document
    import is exactly the kind of long run whose token expires mid-way - and
    the same request-context handling for an HTTP 500.
    """
    draft_id = auth_headers.get(_DRAFT_HEADER)
    if refusal := _revoked_draft_refusal(auth_headers, draft_id, query):
        return {}, refusal
    operation, root_field = _operation_names(query)
    variables[file_var_path.split(".")[-1]] = None
    operations = json.dumps({"query": query, "variables": variables})
    mapping = json.dumps({"0": [f"variables.{file_var_path}"]})

    async def _attempt() -> tuple[dict, str | None, list[Any] | None, int | None]:
        await _refresh_if_stale(auth_headers)
        # Rebuilt per attempt so a re-minted Authorization header is picked up.
        upload_headers = {
            k: v
            for k, v in _request_headers(auth_headers, draft_id).items()
            if k.lower() != "content-type"
        }
        try:
            resp = await client.post(
                f"{base_url}/graphql",
                headers=upload_headers,
                data={"operations": operations, "map": mapping},
                files={"0": (file_name, file_bytes, mime_type)},
            )
            if not resp.is_success:
                if resp.status_code in _CONTEXT_FAILURE_STATUSES:
                    errors = _json_errors(resp)
                    _log_context_failure(resp, operation, root_field, upload_headers, errors)
                    if errors:
                        return {}, None, errors, resp.status_code
                return (
                    {},
                    (
                        f"Error: OpenCTI upload failed (HTTP {resp.status_code}): "
                        f"{resp.text[:300]}"
                    ),
                    None,
                    None,
                )
        except httpx.HTTPError as exc:
            return {}, f"Error: OpenCTI upload network error: {exc}", None, None
        data = resp.json()
        errors = data.get("errors")
        if errors:
            return {}, None, errors, None
        return data.get("data", {}), None, None, None

    data, err, errors, status = await _attempt()
    if (
        errors
        and _is_request_auth_failure(errors, status, draft_id)
        and await _force_remint(client)
    ):
        logger.info(
            "OpenCTI GraphQL upload auth error (%s) — re-minted the token, retrying once",
            _format_graphql_errors(errors),
        )
        data, err, errors, status = await _attempt()
    if errors:
        message = _context_failure_message(
            errors, status, auth_headers, draft_id, operation, root_field, query, "upload error"
        )
        logger.warning("OpenCTI %s", message)
        return {}, message
    return data, err


def _edges(data: dict | None, key: str) -> list[dict]:
    """Extract node list from a GraphQL connection (edges/node pattern)."""
    if not data:
        return []
    container = data.get(key, {})
    if isinstance(container, dict) and "edges" in container:
        return [e["node"] for e in container["edges"] if e.get("node")]
    if isinstance(container, list):
        return container
    return []


#: The longest description or content one entity of a listing shows; a
#: longer one is cut and its marker names :func:`_entity_read_call`.
LISTING_ENTITY_TEXT_LIMIT = 500

#: The longest description one relationship of a listing shows.
LISTING_RELATIONSHIP_TEXT_LIMIT = 300

#: The longest description one object of ``get_opencti_container_full``
#: (or a create tool's echo) shows: that read lists up to 1,000 objects.
LISTING_CONTAINER_ITEM_TEXT_LIMIT = 200


def _entity_read_call(entity: dict) -> str:
    """The ``get_opencti_entity`` call that returns *entity*'s texts in full."""
    entity_id = entity.get("id")
    return f"get_opencti_entity(entity_id={entity_id})" if entity_id else ""


def _relationship_read_call(rel: dict) -> str:
    """The ``get_opencti_relationship`` call that returns *rel*'s description in full."""
    rel_id = rel.get("id")
    return f"get_opencti_relationship(relationship_id={rel_id})" if rel_id else ""


def _format_entity(entity: dict, base_url: str = "", *, full_text: bool = False) -> str:
    """Format any OpenCTI STIX entity into a readable string for the agent.

    *base_url* is the OpenCTI instance host (e.g. ``https://demo.opencti.io``)
    used to build absolute "Open in OpenCTI" links.  When empty, the link is
    omitted entirely — never emit a bare path, since the chat UI would
    resolve it against its own origin and produce a wrong cross-host link.

    With *full_text* (one entity read by ``get_opencti_entity``) the
    description and content are shown whole. Otherwise (a listing) each is
    cut at :data:`LISTING_ENTITY_TEXT_LIMIT` with a marker that says how much
    is left and names the read call, never silently.
    """
    if not entity:
        return "No entity data."
    text_limit = None if full_text else LISTING_ENTITY_TEXT_LIMIT
    read_call = _entity_read_call(entity)
    lines: list[str] = []
    entity_type = entity.get("entity_type", "Unknown")
    name = (
        entity.get("name")
        or entity.get(
            "sco_name"
        )  # aliased name for SCO types (avoids String!/String conflict)
        or entity.get("observable_value")
        or entity.get("value")
        or entity.get("persona_name")
        or entity.get("title")
        or entity.get("attribute_abstract")
        or entity.get("id", "Unknown")
    )
    lines.append(f"[{entity_type}] {name}")
    entity_id = entity.get('id', 'N/A')
    lines.append(f"  ID: {entity_id}")
    if entity_id != 'N/A' and base_url:
        lines.append(f"  Link: [Open in OpenCTI]({base_url}/dashboard/id/{entity_id})")
    if entity.get("standard_id"):
        lines.append(f"  STIX ID: {entity['standard_id']}")
    if entity.get("description"):
        lines.append(f"  Description: {clip_text(entity['description'], text_limit, read_call)}")
    # Common SDO fields
    if entity.get("confidence") is not None:
        lines.append(f"  Confidence: {entity['confidence']}")
    for ts_field in ("created", "modified"):
        if entity.get(ts_field):
            lines.append(f"  {ts_field.title()}: {entity[ts_field]}")
    if entity.get("revoked"):
        lines.append("  Revoked: Yes")
    # Indicator
    if entity.get("pattern"):
        lines.append(
            f"  Pattern ({entity.get('pattern_type', '?')}): {entity['pattern']}"
        )
    if entity.get("valid_from"):
        lines.append(
            f"  Valid: {entity['valid_from']} → {entity.get('valid_until', 'ongoing')}"
        )
    if entity.get("x_opencti_score") is not None:
        lines.append(f"  Score: {entity['x_opencti_score']}")
    # Time-range fields
    for field, label in (
        ("first_seen", "First seen"),
        ("last_seen", "Last seen"),
        ("published", "Published"),
        ("first_observed", "First observed"),
        ("last_observed", "Last observed"),
    ):
        if entity.get(field):
            lines.append(f"  {label}: {entity[field]}")
    # Type-specific scalars
    if entity.get("severity"):
        lines.append(f"  Severity: {entity['severity']}")
    if entity.get("objective"):
        lines.append(f"  Objective: {entity['objective']}")
    if entity.get("is_family") is not None:
        lines.append(f"  Is family: {entity['is_family']}")
    if entity.get("x_mitre_id"):
        lines.append(f"  MITRE ID: {entity['x_mitre_id']}")
    if entity.get("x_opencti_organization_type"):
        lines.append(f"  Org type: {entity['x_opencti_organization_type']}")
    if entity.get("x_opencti_reliability"):
        lines.append(f"  Reliability: {entity['x_opencti_reliability']}")
    # CVSS / EPSS
    if entity.get("x_opencti_cvss_base_score") is not None:
        lines.append(
            f"  CVSS: {entity['x_opencti_cvss_base_score']} ({entity.get('x_opencti_cvss_base_severity', 'N/A')})"
        )
    if entity.get("x_opencti_epss_score") is not None:
        lines.append(f"  EPSS: {entity['x_opencti_epss_score']}")
    if entity.get("x_opencti_cisa_kev"):
        lines.append("  CISA KEV: Yes")
    # List fields
    for field in (
        "aliases",
        "x_opencti_aliases",
        "malware_types",
        "threat_actor_types",
        "tool_types",
        "infrastructure_types",
        "indicator_types",
        "report_types",
        "note_types",
        "event_types",
        "channel_types",
        "narrative_types",
    ):
        vals = entity.get(field)
        if vals and isinstance(vals, list):
            label = field.replace("_", " ").replace("x opencti ", "").title()
            lines.append(f"  {label}: {', '.join(str(v) for v in vals)}")
    # Hashes
    if entity.get("hashes"):
        for h in entity["hashes"]:
            if isinstance(h, dict):
                lines.append(
                    f"  Hash ({h.get('algorithm', '?')}): {h.get('hash', 'N/A')}"
                )
    # Note content
    if entity.get("content") and entity_type == "Note":
        lines.append(f"  Content: {clip_text(entity['content'], text_limit, read_call)}")
    # Report and MediaContent content, aliased: their String clashes with
    # Note.content's String! in one selection set.
    for content_field in ("report_content", "media_content"):
        if entity.get(content_field):
            lines.append(f"  Content: {clip_text(entity[content_field], text_limit, read_call)}")
    if entity.get("opinion"):
        lines.append(f"  Opinion: {entity['opinion']}")
    # Observable description
    if entity.get("x_opencti_description"):
        lines.append(
            f"  Description: {clip_text(entity['x_opencti_description'], text_limit, read_call)}"
        )
    # Metadata
    labels = entity.get("objectLabel")
    if isinstance(labels, list) and labels:
        label_names = [
            lb.get("value", str(lb)) if isinstance(lb, dict) else str(lb)
            for lb in labels
        ]
        lines.append(f"  Labels: {', '.join(label_names)}")
    markings = entity.get("objectMarking")
    if isinstance(markings, list) and markings:
        mark_names = [
            m.get("definition", str(m)) if isinstance(m, dict) else str(m)
            for m in markings
        ]
        lines.append(f"  Markings: {', '.join(mark_names)}")
    author = entity.get("createdBy")
    if isinstance(author, dict) and author.get("name"):
        lines.append(f"  Author: {author['name']}")
    # Kill chain phases
    phases = entity.get("killChainPhases")
    if isinstance(phases, list) and phases:
        phase_strs = [
            f"{p.get('kill_chain_name', '?')}:{p.get('phase_name', '?')}"
            for p in phases
            if isinstance(p, dict)
        ]
        lines.append(f"  Kill chain: {', '.join(phase_strs)}")
    # External references (connection style)
    ext_refs = entity.get("externalReferences", {})
    if isinstance(ext_refs, dict) and ext_refs.get("edges"):
        ref_parts: list[str] = []
        for edge in ext_refs["edges"][:5]:
            ref = edge.get("node", {})
            s = ref.get("source_name", "Unknown")
            if ref.get("url"):
                s += f" ({ref['url']})"
            ref_parts.append(s)
        if ref_parts:
            lines.append(f"  External refs: {'; '.join(ref_parts)}")
    return "\n".join(lines)


def _relationship_end_ref(end: dict[str, Any]) -> str:
    """The ids of one end of a relationship: its id, then its STIX id and MITRE ID.

    An agent walking a campaign to its techniques needs the id of each end to
    read or link it, never the name alone (two techniques share a name across
    the Enterprise and Mobile matrices). Empty when the end carries no id.
    """
    end_id = end.get("id")
    if not end_id:
        return ""
    extra = [str(end["standard_id"])] if end.get("standard_id") else []
    if end.get("x_mitre_id"):
        extra.append(f"MITRE {end['x_mitre_id']}")
    return f"{end_id} ({', '.join(extra)})" if extra else str(end_id)


def _format_relationship(rel: dict, base_url: str = "", *, full_text: bool = False) -> str:
    """Format a STIX core relationship into a readable string.

    *base_url* is the OpenCTI instance host used to build absolute "Open in
    OpenCTI" links.  When empty, the link is omitted (see ``_format_entity``).
    With *full_text* (``get_opencti_relationship``) the description is whole;
    a listing cuts it at :data:`LISTING_RELATIONSHIP_TEXT_LIMIT`, marked.
    """
    if not rel:
        return "No relationship data."
    from_obj = rel.get("from") or {}
    to_obj = rel.get("to") or {}
    from_name = (
        from_obj.get("name")
        or from_obj.get("observable_value")
        or from_obj.get("id", "?")
    )
    to_name = (
        to_obj.get("name") or to_obj.get("observable_value") or to_obj.get("id", "?")
    )
    from_type = from_obj.get("entity_type", "?")
    to_type = to_obj.get("entity_type", "?")
    lines: list[str] = []
    lines.append(
        f"[{from_type}] {from_name} --{rel.get('relationship_type', '?')}--> [{to_type}] {to_name}"
    )
    rel_id = rel.get('id', 'N/A')
    lines.append(f"  ID: {rel_id}")
    for label, end in (("From", from_obj), ("To", to_obj)):
        if ref := _relationship_end_ref(end):
            lines.append(f"  {label}: {ref}")
    if rel_id != 'N/A' and base_url:
        lines.append(f"  Link: [Open in OpenCTI]({base_url}/dashboard/id/{rel_id})")
    if rel.get("description"):
        text_limit = None if full_text else LISTING_RELATIONSHIP_TEXT_LIMIT
        lines.append(
            f"  Description: {clip_text(rel['description'], text_limit, _relationship_read_call(rel))}"
        )
    if rel.get("confidence") is not None:
        lines.append(f"  Confidence: {rel['confidence']}")
    if rel.get("start_time"):
        lines.append(f"  Start: {rel['start_time']}")
    if rel.get("stop_time"):
        lines.append(f"  Stop: {rel['stop_time']}")
    if rel.get("created"):
        lines.append(f"  Created: {rel['created']}")
    labels = rel.get("objectLabel")
    if isinstance(labels, list) and labels:
        lines.append(
            f"  Labels: {', '.join(lb.get('value', str(lb)) for lb in labels if isinstance(lb, dict))}"
        )
    markings = rel.get("objectMarking")
    if isinstance(markings, list) and markings:
        lines.append(
            f"  Markings: {', '.join(m.get('definition', str(m)) for m in markings if isinstance(m, dict))}"
        )
    author = rel.get("createdBy")
    if isinstance(author, dict) and author.get("name"):
        lines.append(f"  Author: {author['name']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


__all__ = [
    'ABSTRACT_STIX_CORE_OBJECT',
    'ABSTRACT_STIX_CYBER_OBSERVABLE',
    'ABSTRACT_STIX_DOMAIN_OBJECT',
    'ABSTRACT_STIX_META_OBJECT',
    'ENTITY_TYPE_USER',
    'INSTANCE_REGARDING_OF',
    'RELATIVE_DATE_RE',
    'RULE_PREFIX',
    '_AUTH_ERROR_MARKERS',
    '_BASE_FIELDS',
    '_CONTAINER_MUTATIONS',
    '_DRAFT_REQUIRED_MSG',
    '_DRAFT_REQUIRED_TOOLS',
    '_ENTITY_FIELDS',
    '_HASH_ALGORITHMS',
    '_OBSERVABLE_TYPES',
    '_PROVIDER',
    '_RELATIONSHIP_FIELDS',
    '_SCO_ENTITY_FIELDS',
    '_SCO_INLINE_FIELDS',
    '_SDO_ENTITY_FIELDS',
    '_SDO_INLINE_FIELDS',
    '_SDO_MUTATIONS',
    '_STIX_PATTERN_MAP',
    '_build_stix_pattern',
    '_client',
    '_describe_graphql_failure',
    '_edges',
    '_force_remint',
    '_format_entity',
    '_format_graphql_errors',
    '_format_relationship',
    '_graphql',
    '_graphql_upload',
    '_is_auth_error',
    '_refresh_if_stale',
    'format_bulk_outcome',
    'logger',
]

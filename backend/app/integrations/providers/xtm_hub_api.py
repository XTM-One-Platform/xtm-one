# SPDX-License-Identifier: Apache-2.0

"""The XTM Hub's whole GraphQL API as tools, read off the vendored schema.

``xtm_hub_schema.graphql`` is the Hub's own SDL (XTM-Hub/xtm-hub,
``apps/frontend/schema.graphql``). Every query and mutation of it is, in
:data:`OPERATIONS`, one tool with a name in the ``list_/get_/create_/update_/
delete_xtm_hub_*`` convention, or covered by a curated tool of
``xtm_hub.py`` (:data:`COVERED`), or not exposed with its reason
(:data:`NOT_EXPOSED`); ``tests/test_xtm_hub_api.py`` holds the three in step
with the schema, so a new Hub operation fails the test until it is placed.

A tool's arguments are the operation's arguments as JSON Schema, read from
the SDL input types (a password field is never offered, a deprecated argument
neither); its answer is the operation's return type, selected two object
levels deep (connections and their edges do not count as a level, a field
that needs arguments is skipped, a field named like a credential is never
selected and is dropped again from the answer), as JSON inside a
``wrap_untrusted`` block. An ``Upload`` argument takes file ids of the run,
sent as a GraphQL multipart request. Every generated tool runs as the
integration's personal API token: the token, its Hub and its checks are
``xtm_hub.build_tools``'s, handed in as *authed*.

Read and write follow the name (``services/tool_classification.py``): a
query's tool reads, a mutation's tool writes, so a read-only binding keeps the
reads; the only query that changes something (``updateOpenCTIManifest``) is
named as the write it is.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from app.services.content_safety import wrap_untrusted
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition

SCHEMA_PATH = Path(__file__).with_name("xtm_hub_schema.graphql")

_SOURCE = "integration:xtm_hub"
_SECRET_FIELD_RE = re.compile(r"(?i)(token|password|secret)")
_BUILTIN_SCALARS = {"String": "string", "ID": "string", "Int": "integer", "Float": "number", "Boolean": "boolean"}
_SELECTION_DEPTH = 2
_INPUT_DEPTH = 4
_DEFAULT_FIRST = 25
_MAX_FIRST = 100
_MAX_ANSWER_CHARS = 40_000
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_MAX_UPLOADS = 10

# ---------------------------------------------------------------------------
# The operation table
# ---------------------------------------------------------------------------

#: Hub operation -> (tool name, what it does). The tool name reads or writes as
#: the operation does; a destructive tool says what it deletes.
OPERATIONS: dict[str, tuple[str, str]] = {
    # ── Me and settings ──
    "settings": (
        "get_xtm_hub_settings",
        "Read the Hub's public settings: capabilities, feature flags, login providers, front URL, environment.",
    ),
    "apiTokens": ("list_xtm_hub_my_api_tokens", "List the token user's API tokens, revoked and expired ones included."),
    "organizationApiTokens": (
        "list_xtm_hub_organization_api_tokens",
        "List every API token of the token's organization (its administrators).",
    ),
    "revokeApiToken": ("revoke_xtm_hub_api_token", "Revoke one API token at once; it stops working."),
    "userOrganizations": ("list_xtm_hub_my_organizations", "List the organizations the token's user belongs to."),
    "userServiceCapabilities": (
        "get_xtm_hub_my_service_capabilities",
        "Read the token user's capabilities on one service (library, platform).",
    ),
    "editMeUser": (
        "update_xtm_hub_my_profile",
        "Update the token user's own profile: first and last name, country, language.",
    ),
    "uploadUserPicture": (
        "upload_xtm_hub_my_picture",
        "Replace the token user's profile picture with a file of the run.",
    ),
    # ── Content: documents and libraries ──
    "serviceInstances": (
        "list_xtm_hub_services",
        "List the Hub services (libraries, platforms, roadmap), inaccessible ones too for an administrator (includeInaccessible).",
    ),
    "serviceInstanceById": (
        "get_xtm_hub_service",
        "Read one Hub service by id, with its links, subscriptions and capabilities.",
    ),
    "serviceInstanceLinksByTags": (
        "list_xtm_hub_service_links",
        "List the public service links carrying the given tags.",
    ),
    "seoServiceInstance": ("get_xtm_hub_seo_service", "Read one public service by slug, as the SEO pages show it."),
    "seoServiceInstanceMetadata": (
        "get_xtm_hub_service_seo_metadata",
        "Read a service's SEO metadata (title, description) per language.",
    ),
    "editSeoServiceInstance": (
        "update_xtm_hub_service_seo_metadata",
        "Update a service's SEO metadata in one language.",
    ),
    "addServicePicture": (
        "upload_xtm_hub_service_picture",
        "Set a service's logo or illustration from a file of the run.",
    ),
    "updatePlatformServiceMetadata": (
        "update_xtm_hub_platform_service_metadata",
        "Rename the service of a registered platform.",
    ),
    "document": ("get_xtm_hub_document", "Read one document (resource) of a library by id, whatever its status."),
    "documentFacets": (
        "get_xtm_hub_document_facets",
        "Read the facet counts (use cases, types, versions...) of a library search.",
    ),
    "lastDeployedOverview": (
        "get_xtm_hub_last_deployed_overview",
        "Read the most recently deployed resources of a library.",
    ),
    "publicDocumentsByServiceSlug": (
        "list_xtm_hub_public_documents",
        "List every published document of a public library, by its slug.",
    ),
    "publicDocumentSlugsByServiceSlug": (
        "list_xtm_hub_public_document_slugs",
        "List the slugs and dates of the published documents of a public library.",
    ),
    "createDocument": (
        "create_xtm_hub_document",
        "Create a document in a library with its full metadata (any library, the integrations one included), files taken from the run.",
    ),
    "updateDocument": (
        "update_xtm_hub_document",
        "Update a document: name, descriptions, status (draft, in_validation, published: publish or unpublish), use cases, categories, metadata, files. Images not listed in existingImageIds are dropped.",
    ),
    "deleteDocument": (
        "delete_xtm_hub_document",
        "Delete a document of a library permanently, with its files and its children.",
    ),
    "incrementShareNumberDocument": ("increment_xtm_hub_document_share_count", "Add one to a document's share count."),
    "solutionCategories": (
        "list_xtm_hub_solution_categories",
        "List the solution categories resources are filed under.",
    ),
    "addSolutionCategory": ("create_xtm_hub_solution_category", "Create a solution category."),
    "editSolutionCategory": ("update_xtm_hub_solution_category", "Update a solution category."),
    "deleteSolutionCategory": (
        "delete_xtm_hub_solution_category",
        "Delete a solution category permanently; resources lose it.",
    ),
    "addUseCase": ("create_xtm_hub_use_case", "Create a use case resources can be tagged with."),
    "editUseCase": ("update_xtm_hub_use_case", "Update a use case."),
    "deleteUseCase": ("delete_xtm_hub_use_case", "Delete a use case permanently; resources lose the tag."),
    "contentTranslations": (
        "list_xtm_hub_content_translations",
        "List the published content translations, by locale and keys.",
    ),
    "contentTranslationDrafts": (
        "list_xtm_hub_content_translation_drafts",
        "List the unpublished content translation drafts.",
    ),
    "saveContentTranslationDraft": (
        "save_xtm_hub_content_translation_draft",
        "Save a translation draft for one key, one value per locale.",
    ),
    "publishContentTranslationDrafts": (
        "publish_xtm_hub_content_translation_drafts",
        "Publish every pending content translation draft.",
    ),
    "discardContentTranslationDrafts": (
        "discard_xtm_hub_content_translation_drafts",
        "Discard every pending content translation draft; answers how many were dropped.",
    ),
    "newsFeedItems": ("list_xtm_hub_news_feed", "List the news feed items."),
    "deleteNewsFeedItem": ("delete_xtm_hub_news_feed_item", "Delete one news feed item permanently."),
    "generateManifest": ("generate_xtm_hub_manifest", "Generate the connectors manifest of a product version."),
    "competitors": ("list_xtm_hub_competitors", "List the competitors referenced by the Hub."),
    "createCompetitor": ("create_xtm_hub_competitor", "Create a competitor."),
    "updateCompetitor": ("update_xtm_hub_competitor", "Update a competitor."),
    "deleteCompetitor": ("delete_xtm_hub_competitor", "Delete a competitor permanently."),
    # ── Roadmap ──
    "epics": ("list_xtm_hub_epics", "List the roadmap epics."),
    "countEpicsPerTimeline": ("count_xtm_hub_epics_per_timeline", "Count the roadmap epics per timeline."),
    "createEpic": ("create_xtm_hub_epic", "Create a roadmap epic."),
    "updateEpic": ("update_xtm_hub_epic", "Update a roadmap epic."),
    "deleteEpic": ("delete_xtm_hub_epic", "Delete a roadmap epic permanently."),
    "currentVotingRound": ("get_xtm_hub_current_voting_round", "Read the open voting round of a roadmap service."),
    "votingRounds": ("list_xtm_hub_voting_rounds", "List the voting rounds, of one roadmap service or all."),
    "votingRound": ("get_xtm_hub_voting_round", "Read one voting round with its features."),
    "votingRoundResults": ("get_xtm_hub_voting_round_results", "Read the vote results of a voting round."),
    "createVotingRound": ("create_xtm_hub_voting_round", "Create a voting round."),
    "updateVotingRound": ("update_xtm_hub_voting_round", "Update a voting round."),
    "setVotingRoundStatus": ("set_xtm_hub_voting_round_status", "Open, close or reset a voting round's status."),
    "deleteVotingRound": (
        "delete_xtm_hub_voting_round",
        "Delete a voting round permanently, with its votable features and votes.",
    ),
    "createVotableFeature": (
        "create_xtm_hub_votable_feature",
        "Create a votable feature in a voting round, attachments taken from the run.",
    ),
    "updateVotableFeature": ("update_xtm_hub_votable_feature", "Update a votable feature."),
    "deleteVotableFeature": ("delete_xtm_hub_votable_feature", "Delete a votable feature permanently, with its votes."),
    "voteForFeature": ("vote_xtm_hub_feature", "Vote, as the token's user, for a feature of the open voting round."),
    # ── Administration: organizations and users ──
    "organization": ("get_xtm_hub_organization", "Read one organization."),
    "organizations": ("list_xtm_hub_organizations", "List the organizations (Hub administrators)."),
    "addOrganization": ("create_xtm_hub_organization", "Create an organization."),
    "editOrganization": ("update_xtm_hub_organization", "Update an organization's name and domains."),
    "deleteOrganization": (
        "delete_xtm_hub_organization",
        "Delete an organization permanently, with its memberships and subscriptions.",
    ),
    "users": (
        "list_xtm_hub_users",
        "List the users (an organization administrator: its members; a Hub administrator: everyone).",
    ),
    "pendingUsers": ("list_xtm_hub_pending_users", "List the users waiting to join the organization."),
    "usersWithCapabilitiesInOrganization": (
        "list_xtm_hub_organization_contacts",
        "List the members of an organization holding the given capabilities.",
    ),
    "adminAddUser": ("create_xtm_hub_user", "Create a user with organization capabilities (Hub administrators)."),
    "adminEditUser": (
        "update_xtm_hub_user",
        "Update a user: name, email, disabled, organization capabilities (Hub administrators).",
    ),
    "deleteUser": ("delete_xtm_hub_user", "Delete a user account permanently."),
    "addUser": ("add_xtm_hub_organization_user", "Add a user by email to the token's organization with capabilities."),
    "editUserCapabilities": (
        "update_xtm_hub_user_capabilities",
        "Replace a member's capabilities in the token's organization.",
    ),
    "acceptPendingUserInOrganization": ("accept_xtm_hub_pending_user", "Accept one pending user into an organization."),
    "removePendingUserFromOrganization": ("remove_xtm_hub_pending_user", "Refuse one pending user of an organization."),
    "bulkAcceptPendingUserInOrganization": (
        "accept_xtm_hub_pending_users",
        "Accept the pending users named by their ids (explicit ids only).",
    ),
    "bulkRemovePendingUserFromOrganization": (
        "remove_xtm_hub_pending_users",
        "Refuse the pending users named by their ids (explicit ids only).",
    ),
    "removeUserFromOrganization": (
        "remove_xtm_hub_user_from_organization",
        "Remove a member from an organization; the account stays.",
    ),
    "rolePortals": ("list_xtm_hub_role_portals", "List the portal roles and their capabilities."),
    "addRolePortal": ("create_xtm_hub_role_portal", "Create a portal role."),
    "updateRolePortal": ("update_xtm_hub_role_portal", "Update a portal role's capabilities."),
    "deleteRolePortal": (
        "delete_xtm_hub_role_portal",
        "Delete a portal role permanently; its users lose its capabilities.",
    ),
    "ssoGroupRolePortals": ("list_xtm_hub_sso_group_role_portals", "List the SSO group to portal role mappings."),
    "addSSOGroupRolePortal": ("create_xtm_hub_sso_group_role_portal", "Map an SSO group to a portal role."),
    "updateSSOGroupRolePortal": ("update_xtm_hub_sso_group_role_portal", "Change an SSO group to portal role mapping."),
    "deleteSSOGroupRolePortal": ("delete_xtm_hub_sso_group_role_portal", "Delete an SSO group to portal role mapping."),
    # ── Administration: subscriptions, services, groups ──
    "subscriptions": ("list_xtm_hub_subscriptions", "List the organizations' subscriptions to services."),
    "subscriptionById": ("get_xtm_hub_subscription", "Read one subscription."),
    "createSubscriptions": ("create_xtm_hub_subscriptions", "Subscribe organizations to a service."),
    "updateSubscription": ("update_xtm_hub_subscription", "Update a subscription's dates."),
    "deleteSubscriptions": (
        "delete_xtm_hub_subscriptions",
        "Delete the subscriptions named by their ids permanently; their members lose the service.",
    ),
    "addSubscriptionCapability": ("add_xtm_hub_subscription_capability", "Add capabilities to a subscription."),
    "userServiceFromSubscription": (
        "list_xtm_hub_subscription_user_services",
        "List the users of a subscription and their service capabilities.",
    ),
    "addUserService": ("create_xtm_hub_user_service", "Give users access to a service."),
    "editUserService": ("update_xtm_hub_user_service", "Change a user's capabilities on a service."),
    "deleteUserServices": (
        "delete_xtm_hub_user_services",
        "Delete users' access to a service (their user service rows); the accounts stay.",
    ),
    "addCapabilitiesToUserServices": (
        "add_xtm_hub_user_service_capabilities",
        "Add capabilities to users of a service.",
    ),
    "serviceGroups": ("list_xtm_hub_service_groups", "List the groups of a platform service."),
    "updateServiceGroups": ("update_xtm_hub_service_groups", "Replace the groups of a platform service."),
    "bundleUserServiceGroups": ("list_xtm_hub_bundle_user_groups", "List the users of a bundle and their groups."),
    "addUsersToBundleGroups": ("add_xtm_hub_users_to_bundle_groups", "Add users to groups of a bundle."),
    "removeUsersFromBundleGroups": (
        "remove_xtm_hub_users_from_bundle_groups",
        "Remove the users named by their ids from every group of a bundle.",
    ),
    "updateBundleUserGroups": ("update_xtm_hub_bundle_user_groups", "Replace the groups of users of a bundle."),
    "bundleProducts": ("list_xtm_hub_bundle_products", "List the products of a bundle."),
    # ── Trials and deployments ──
    "platformTrialStatus": (
        "get_xtm_hub_trial_status",
        "Read an organization's trial status: deployment request, blacklist, end date.",
    ),
    "xtmPlatformBundle": (
        "get_xtm_hub_platform_bundle",
        "Read the token organization's XTM Platform bundle deployment.",
    ),
    "deploymentRequestsAvailable": (
        "list_xtm_hub_available_deployments",
        "List which products and regions can be requested now, and the quota left.",
    ),
    "deploymentRequestsList": (
        "list_xtm_hub_deployment_queue",
        "List every deployment request with its queue order (Hub administrators).",
    ),
    "createDeploymentRequest": ("create_xtm_hub_deployment_request", "Request a trial or bundle deployment."),
    "reorderDeploymentRequestInQueue": (
        "reorder_xtm_hub_deployment_request",
        "Move a deployment request in the queue (Hub administrators).",
    ),
    "cancelDeploymentRequest": (
        "cancel_xtm_hub_deployment_request",
        "Cancel one of the organization's deployment requests; a running platform is stopped.",
    ),
    "adminCancelDeploymentRequest": (
        "admin_cancel_xtm_hub_deployment_request",
        "Cancel any deployment request (Hub administrators); a running platform is stopped.",
    ),
    "updateDeploymentQuotaCapacity": (
        "update_xtm_hub_deployment_quota_capacity",
        "Set the deployment quota capacity (Hub administrators).",
    ),
    "giveDeploymentFeedback": ("submit_xtm_hub_deployment_feedback", "Send feedback on a deployment."),
    # ── Platforms ──
    "registeredPlatform": ("get_xtm_hub_registered_platform", "Read one registered platform by id."),
    "registeredPlatformByPlatformId": (
        "get_xtm_hub_registered_platform_by_platform_id",
        "Read one registered platform by its platform id.",
    ),
    "isPlatformRegistered": ("check_xtm_hub_platform_registered", "Tell whether a platform id is registered."),
    "canUnregisterPlatform": (
        "check_xtm_hub_platform_can_unregister",
        "Tell whether a platform can be unregistered, and why not.",
    ),
    "platformAssociatedOrganization": (
        "get_xtm_hub_platform_organization",
        "Read the organization a platform (or one of its tenants) is registered in.",
    ),
    "unregisterPlatform": (
        "unregister_xtm_hub_platform",
        "Unregister a platform (or one tenant) from the Hub; it loses its Hub services.",
    ),
    "registeredProductVersions": ("list_xtm_hub_product_versions", "List the versions of a product the Hub knows."),
    "newProductVersion": ("add_xtm_hub_product_version", "Declare a new released version of a product."),
    "xtmonePlatformIntegrationStatus": (
        "get_xtm_hub_xtmone_integration_status",
        "Read the XTM One integration status of a platform service.",
    ),
    "saasManagerPlatforms": ("list_xtm_hub_saas_platforms", "List the SaaS platforms the SaaS manager runs."),
    "registeredSaasPlatformData": (
        "get_xtm_hub_saas_platform_metrics",
        "Read a SaaS platform's metadata, metrics, storage and SLA over a period (READ_SAAS_METRICS).",
    ),
}

#: Hub operations a curated tool of ``xtm_hub.py`` already answers.
COVERED: dict[str, str] = {
    "me": "get_xtm_hub_myself",
    "currentApiToken": "get_xtm_hub_myself",
    "seoServiceInstances": "list_xtm_hub_catalogs",
    "publicDocuments": "search_xtm_hub_resources",
    "publicDocumentBySlug": "get_xtm_hub_resource",
    "newestDocuments": "list_xtm_hub_featured_resources",
    "mostDeployedDocuments": "list_xtm_hub_featured_resources",
    "useCases": "list_xtm_hub_use_cases",
    "documents": "search_xtm_hub_library",
    "registeredPlatforms": "list_xtm_hub_registered_platforms",
}

_SIGN_IN_RESET_REASON = "a person's own sign-in secret, never an agent's to set"
_SYSTEM_CREDENTIAL_REASON = "the Hub's system credential only (@system_token); a personal API token is refused"

#: Hub operations never offered as a tool, with the reason.
NOT_EXPOSED: dict[str, str] = {
    "login": "a browser session login; the integration authenticates with its API token",
    "logout": "ends a browser session the integration does not hold",
    "resetPassword": _SIGN_IN_RESET_REASON,
    "requestTransferPersonalSpace": "starts an e-mailed confirmation a person completes",
    "transferPersonalSpace": "completes an e-mailed confirmation of a person",
    "changeSelectedOrganization": "a browser session preference; the API token is bound to one organization",
    "frontendErrorLog": "the Hub front end's error reporting",
    "sendTelemetryEvent": "the Hub front end's telemetry",
    "contactUs": "the sales contact form, a person's message",
    "registerPlatform": "answers the platform's own credential; a platform registers itself from its settings",
    "autoRegisterPlatform": "a platform's own registration call, with the platform credential",
    "refreshUserPlatformToken": "rotates a platform credential the platform itself holds",
    "refreshPlatformRegistrationConnectivityStatus": "a platform's heartbeat, sent with its own credential",
    "refreshPlatformRegistrationConnectivityStatusSingleTenant": "a platform tenant's heartbeat, sent with its own credential",
    "refreshPlatformRegistrationConnectivityStatusAllTenants": "a platform's tenants heartbeat, sent with its own credential",
    "openCTIPlatformRegistrationStatus": "deprecated by the Hub; a platform's own check, with its credential",
    # The Hub answers these to its system or platform credential only, never to
    # a personal API token: a tool would always be refused.
    "deploymentRequests": _SYSTEM_CREDENTIAL_REASON,
    "updateDeploymentRequest": _SYSTEM_CREDENTIAL_REASON,
    "ingestManifestFragments": _SYSTEM_CREDENTIAL_REASON,
    "updateOpenCTIManifest": _SYSTEM_CREDENTIAL_REASON,
    "consumeProvisionedNewsFeedItems": "a registered platform's own call, with the platform credential",
    "createApiToken": "the Hub refuses it to an API token (NOT_ALLOWED_WITH_API_TOKEN); a person creates tokens",
}

#: Queries that change something: a tool of one is a write (none is offered today:
#: ``updateOpenCTIManifest`` is the Hub system credential's).
WRITE_QUERIES = frozenset({"updateOpenCTIManifest"})

#: Input fields a tool narrows to: bulk tools act on explicit ids only.
_INPUT_ALLOW: dict[str, dict[str, tuple[str, ...]]] = {
    "bulkAcceptPendingUserInOrganization": {"input": ("ids",)},
    "bulkRemovePendingUserFromOrganization": {"input": ("ids",)},
}


# ---------------------------------------------------------------------------
# The SDL, parsed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TypeRef:
    named: str
    text: str  # as written in the SDL, e.g. ``[UserId!]!``

    @property
    def required(self) -> bool:
        return self.text.endswith("!")

    @property
    def is_list(self) -> bool:
        return self.text.startswith("[")


@dataclass(frozen=True)
class Arg:
    name: str
    type: TypeRef
    has_default: bool
    deprecated: bool


@dataclass
class Field:
    name: str
    type: TypeRef
    args: list[Arg] = field(default_factory=list)
    deprecated: bool = False


@dataclass
class GqlType:
    kind: str  # type | interface | input | enum | scalar
    name: str
    fields: list[Field] = field(default_factory=list)
    values: list[str] = field(default_factory=list)


def _type_ref(text: str) -> TypeRef:
    text = text.strip()
    return TypeRef(named=re.sub(r"[\[\]!\s]", "", text), text=re.sub(r"\s+", "", text))


def _split_top(text: str, sep: str = ",") -> list[str]:
    """Split on *sep* outside brackets and parentheses."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _parse_args(text: str) -> list[Arg]:
    # Arguments are separated by commas or by whitespace before ``name:``.
    text = re.sub(r"\s+(?=\w+\s*:)", ",", text)
    args = []
    for part in _split_top(text):
        name, _, rest = part.partition(":")
        deprecated = "@deprecated" in rest
        rest = re.sub(r"@\w+(\([^)]*\))?", "", rest)
        type_text, eq, _default = rest.partition("=")
        args.append(Arg(name.strip(), _type_ref(type_text), bool(eq), deprecated))
    return args


def _parse_field(line: str) -> Field:
    deprecated = "@deprecated" in line
    args: list[Arg] = []
    match = re.match(r"(\w+)\s*(\((.*)\))?\s*:\s*(.*)$", line, re.S)
    if match is None:
        raise ValueError(f"unreadable field: {line!r}")
    name, _, arg_text, rest = match.groups()
    if arg_text is not None:
        # The return type follows the closing parenthesis of the arguments.
        depth, end = 0, 0
        for index, ch in enumerate(line):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        arg_text = line[line.index("(") + 1 : end]
        rest = line[end + 1 :].split(":", 1)[1]
        args = _parse_args(arg_text)
    rest = re.sub(r"@\w+(\([^)]*\))?", "", rest)
    return Field(name, _type_ref(rest), args, deprecated)


def _block_fields(body: str) -> list[Field]:
    fields, buf, depth = [], "", 0
    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue
        buf = f"{buf} {line}" if buf else line
        depth += line.count("(") - line.count(")")
        if depth == 0:
            fields.append(_parse_field(buf))
            buf = ""
    return fields


def parse_schema(text: str) -> dict[str, GqlType]:
    """The named types of an SDL document (descriptions and comments dropped)."""
    text = re.sub(r'"""[\s\S]*?"""', "", text)
    text = re.sub(r'"[^"\n]*"', "", text)
    text = re.sub(r"#[^\n]*", "", text)
    types: dict[str, GqlType] = {}
    for name in re.findall(r"^scalar\s+(\w+)", text, re.M):
        types[name] = GqlType("scalar", name)
    for kind, name, body in re.findall(r"^(type|interface|input|enum)\s+(\w+)[^{\n]*\{(.*?)^\}", text, re.M | re.S):
        if kind == "enum":
            values = [re.sub(r"@.*", "", v).strip() for v in body.split("\n")]
            types[name] = GqlType(kind, name, values=[v for v in values if v])
        else:
            types[name] = GqlType(kind, name, fields=_block_fields(body))
    return types


@cache
def schema() -> dict[str, GqlType]:
    return parse_schema(SCHEMA_PATH.read_text(encoding="utf-8"))


def root_fields(kind: str) -> dict[str, Field]:
    """The fields of ``Query`` or ``Mutation``, by name."""
    return {f.name: f for f in schema()[kind].fields}


# ---------------------------------------------------------------------------
# Arguments as JSON Schema, answers as a selection set
# ---------------------------------------------------------------------------


def _json_schema(ref_text: str, depth: int) -> dict[str, Any]:
    if ref_text.endswith("!"):
        ref_text = ref_text[:-1]
    if ref_text.startswith("["):
        return {"type": "array", "items": _json_schema(ref_text[1:-1], depth)}
    types = schema()
    if ref_text in _BUILTIN_SCALARS:
        return {"type": _BUILTIN_SCALARS[ref_text]}
    gql = types.get(ref_text)
    if gql is None or gql.kind == "scalar":
        if ref_text == "JSON":
            return {"type": "object", "description": "A JSON object."}
        if ref_text == "Upload":
            return {"type": "string", "description": "file_id of a file of this run."}
        if ref_text == "Date":
            return {"type": "string", "description": "ISO 8601 date."}
        return {"type": "string"}
    if gql.kind == "enum":
        return {"type": "string", "enum": list(gql.values)}
    if depth <= 0:
        return {"type": "object", "description": f"A {ref_text} object."}
    return _input_object(gql, depth - 1, None)


def _input_object(gql: GqlType, depth: int, allow: tuple[str, ...] | None) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for f in gql.fields:
        if f.deprecated or _SECRET_FIELD_RE.search(f.name):
            continue
        if allow is not None and f.name not in allow:
            continue
        properties[f.name] = _json_schema(f.type.text, depth)
        if f.type.required:
            required.append(f.name)
    if allow is not None:
        # Narrowed fields (explicit ids) are always required, never empty.
        required = list(properties)
        for name in properties:
            if properties[name].get("type") == "array":
                properties[name]["minItems"] = 1
    return {"type": "object", "properties": properties, "required": required}


def _defaulted(op: str, arg: Arg) -> Any:
    """The value a tool sends for a required argument the model may omit."""
    gql = schema().get(arg.type.named)
    if arg.name == "first" and arg.type.named == "Int":
        return _DEFAULT_FIRST
    if arg.name == "pageSize":
        return _DEFAULT_FIRST
    if arg.name == "page":
        return 1
    if arg.name in ("orderMode",):
        return "asc"
    if arg.name == "orderBy" and gql is not None and gql.kind == "enum":
        return gql.values[0]
    return None


def input_schema(op: str, root: Field) -> dict[str, Any]:
    """The tool's JSON Schema, from the operation's arguments."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    allow = _INPUT_ALLOW.get(op, {})
    types = schema()
    for arg in root.args:
        if arg.deprecated:
            continue
        gql = types.get(arg.type.named)
        if arg.name in allow and gql is not None and gql.kind == "input":
            prop = _input_object(gql, _INPUT_DEPTH, allow[arg.name])
        else:
            prop = _json_schema(arg.type.text, _INPUT_DEPTH)
        default = _defaulted(op, arg)
        if arg.name == "first" and arg.type.named == "Int":
            prop.update({"minimum": 1, "maximum": _MAX_FIRST, "description": f"Page size (default {_DEFAULT_FIRST})."})
        elif arg.name == "after":
            prop["description"] = "endCursor of the previous page."
        properties[arg.name] = prop
        if arg.type.required and not arg.has_default and default is None:
            required.append(arg.name)
    return {"type": "object", "properties": properties, "required": required}


def selection(type_name: str, depth: int = _SELECTION_DEPTH) -> str:
    """The selection set of *type_name*, *depth* object levels deep."""
    return _selection(type_name, depth, frozenset())


def _transparent(name: str) -> bool:
    return name.endswith("Connection") or name.endswith("Edge") or name == "PageInfo"


def _selection(type_name: str, depth: int, seen: frozenset[str]) -> str:
    types = schema()
    gql = types.get(type_name)
    if gql is None or gql.kind not in ("type", "interface"):
        return ""
    parts = ["__typename"] if gql.kind == "interface" else []
    for f in gql.fields:
        if f.deprecated or _SECRET_FIELD_RE.search(f.name):
            continue
        if any(a.type.required and not a.has_default for a in f.args):
            continue
        sub = types.get(f.type.named)
        if f.type.named in _BUILTIN_SCALARS or sub is None or sub.kind in ("scalar", "enum"):
            parts.append(f.name)
            continue
        transparent = _transparent(f.type.named)
        next_depth = depth if transparent else depth - 1
        if next_depth < 0 or (not transparent and f.type.named in seen):
            continue
        inner = _selection(f.type.named, next_depth, seen | {type_name})
        if inner:
            parts.append(f"{f.name} {{ {inner} }}")
    return " ".join(parts)


def operation_text(kind: str, op: str, root: Field, sent: set[str]) -> str:
    """The GraphQL document of one call, declaring the arguments sent."""
    declared = [a for a in root.args if a.name in sent]
    head = ""
    if declared:
        head = "(" + ", ".join(f"${a.name}: {a.type.text}" for a in declared) + ")"
    call = ""
    if declared:
        call = "(" + ", ".join(f"{a.name}: ${a.name}" for a in declared) + ")"
    body = selection(root.type.named)
    tail = f" {{ {body} }}" if body else ""
    keyword = "query" if kind == "Query" else "mutation"
    return f"{keyword} XtmOneHub_{op}{head} {{ {op}{call}{tail} }}"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items() if not _SECRET_FIELD_RE.search(str(k))}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _upload_paths(root: Field, variables: dict[str, Any]) -> list[tuple[str, str]]:
    """``(variable path, file id)`` of every Upload argument the call sets."""
    out: list[tuple[str, str]] = []
    for arg in root.args:
        if arg.type.named != "Upload" or arg.name not in variables:
            continue
        value = variables[arg.name]
        if arg.type.is_list:
            ids = value if isinstance(value, list) else [value]
            for index, file_id in enumerate(ids):
                out.append((f"{arg.name}.{index}", str(file_id)))
            variables[arg.name] = [None] * len(ids)
        else:
            out.append((arg.name, str(value)))
            variables[arg.name] = None
    return out


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

#: ``(query, variables, action, api_token, uploads) -> (data, error)``.
GraphQLRunner = Callable[
    [str, dict[str, Any], str, str, list[tuple[str, str, bytes, str]] | None],
    Awaitable[tuple[dict[str, Any] | None, str | None]],
]
FileReader = Callable[[FileContext | None, str], Awaitable[tuple[bytes, str, str] | str]]


def operation_kind(op: str) -> str:
    return "Query" if op in root_fields("Query") else "Mutation"


def is_read(op: str) -> bool:
    return operation_kind(op) == "Query" and op not in WRITE_QUERIES


def build_api_tools(
    base_url: str,
    executor: CallableToolExecutor,
    *,
    authed: Callable[[], Awaitable[tuple[str, str | None]]],
    run: GraphQLRunner,
    read_file: FileReader,
) -> list[ToolDefinition]:
    """One tool per operation of :data:`OPERATIONS`, registered on *executor*."""
    queries, mutations = root_fields("Query"), root_fields("Mutation")
    definitions: list[ToolDefinition] = []
    for op, (tool_name, summary) in OPERATIONS.items():
        kind = "Query" if op in queries else "Mutation"
        root = (queries if kind == "Query" else mutations)[op]
        read = is_read(op)
        executor.register(tool_name, _handler(op, kind, root, tool_name, base_url, authed, run, read_file))
        definitions.append(
            ToolDefinition(
                name=tool_name,
                description=(
                    f"{summary} XTM Hub {op} {'query' if kind == 'Query' else 'mutation'}, "
                    f"as the integration's API token ({'read' if read else 'write'}; the token's "
                    "user needs the Hub capability it takes)."
                ),
                input_schema=input_schema(op, root),
                source=_SOURCE,
                # The curated tools come first when a model caps the tool count.
                priority=2,
                read_only=read,
            )
        )
    return definitions


def _handler(
    op: str,
    kind: str,
    root: Field,
    tool_name: str,
    base_url: str,
    authed: Callable[[], Awaitable[tuple[str, str | None]]],
    run: GraphQLRunner,
    read_file: FileReader,
) -> Callable[..., Awaitable[str]]:
    args_by_name = {a.name: a for a in root.args if not a.deprecated}
    allow = _INPUT_ALLOW.get(op, {})

    async def handler(file_context: FileContext | None = None, **kwargs: Any) -> str:
        variables: dict[str, Any] = {}
        for name, arg in args_by_name.items():
            value = kwargs.get(name)
            if value is None or value == "":
                value = _defaulted(op, arg)
            if value is None:
                if arg.type.required and not arg.has_default:
                    return f"Error: {name} is required."
                continue
            if name == "first" and arg.type.named == "Int":
                try:
                    value = max(1, min(_MAX_FIRST, int(value)))
                except (TypeError, ValueError):
                    return "Error: first is a whole number."
            if name in allow and isinstance(value, dict):
                value = {k: v for k, v in value.items() if k in allow[name]}
                if not all(value.get(k) for k in allow[name]):
                    return f"Error: {tool_name} acts on explicit ids only: {', '.join(allow[name])} is required."
            variables[name] = value
        token, err = await authed()
        if err:
            return err
        paths = _upload_paths(root, variables)
        if len(paths) > _MAX_UPLOADS:
            return f"Error: at most {_MAX_UPLOADS} files per call."
        uploads: list[tuple[str, str, bytes, str]] = []
        for path, file_id in paths:
            piped = await read_file(file_context, file_id)
            if isinstance(piped, str):
                return piped
            content, filename, content_type = piped
            uploads.append((path, filename, content, content_type))
        document = operation_text(kind, op, root, set(variables))
        data, err = await run(document, variables, op, token, uploads or None)
        if err:
            return err
        answer = json.dumps(_redact((data or {}).get(op)), ensure_ascii=False, default=str)
        if len(answer) > _MAX_ANSWER_CHARS:
            answer = answer[:_MAX_ANSWER_CHARS] + " …(cut: narrow the call or page with first/after)"
        return wrap_untrusted(f"{tool_name} on {base_url}:\n{answer}", source=f"XTM Hub {op}")

    return handler

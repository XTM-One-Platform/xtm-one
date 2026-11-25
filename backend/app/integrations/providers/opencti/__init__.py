"""OpenCTI integration provider package.

Comprehensive management of an OpenCTI platform via its GraphQL API.

The provider is split by domain:

* ``_common``      - shared GraphQL client, formatting helpers, constants.
* ``knowledge``    - STIX knowledge (SDO/SCO/relationship/container/...),
                     draft management, NLQ-filter validation, dev tools.
* ``playbooks``    - playbook management + graph editing.
* ``workspaces``   - investigations & dashboards.
* ``access``       - users, groups, roles, capabilities, self profile.
* ``notifications``- notifiers, triggers/digests, inbox notifications.
* ``customization``- entity settings, vocabularies, status templates,
                     sub-types, decay rules, retention rules, exclusion lists.
* ``audit``        - platform activity / history audit queries.
* ``pir``          - Priority Intelligence Requirements.
* ``security_coverage`` - security coverages, the test requests OpenAEV builds
                     its scenarios from (CTEM-16).
* ``ingestion``    - RSS/CSV/JSON/TAXII feeds + intake forms.
* ``markings``     - marking definitions, and the marking / author ref
                     resolvers the ingestion and marking tools share.
* ``data_sharing`` - live streams, public CSV feeds, TAXII collections.
* ``connectors``   - EE connector catalog browsing + managed-connector
                     deployment / lifecycle (Composer).
* ``capabilities`` - capability pre-check wrapping the gated tools in
                     unattended runs.
* ``naming``       - how the platform names the objects it holds (the
                     grounded STIX extraction's ``platform_names``).
* ``ids``          - the guard refusing a short id prefix before any request.

Token format passed by the dispatcher: ``base_url||api_token`` (the
``extra_config`` base_url is prepended by ``_get_token`` in
``integration_tools.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.core.module_versions import RegisteredVersionFn
from app.integrations.http import RefreshableHeaders, parse_compound_token
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _format_entity, _format_relationship
from .access import install_ignored_arguments_note
from .access import register as register_access
from .audit import register as register_audit
from .capabilities import install_capability_gate
from .connectors import register as register_connectors
from .customization import register as register_customization
from .data_sharing import register as register_data_sharing
from .health_reads import register as register_health_reads
from .ids import install_short_id_guard
from .ingestion import register as register_ingestion
from .knowledge import register_knowledge
from .markings import register as register_markings
from .naming import platform_names_lookup
from .notifications import register as register_notifications
from .pir import register as register_pir
from .playbooks import register as register_playbooks
from .security_coverage import register as register_security_coverage
from .workspaces import register as register_workspaces

logger = logging.getLogger(__name__)

__all__ = ["_format_entity", "_format_relationship", "build_tools"]


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    *,
    platform_names_sink: Callable[[Any], None] | None = None,
    registered_version_fn: RegisteredVersionFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build OpenCTI integration tools.

    ``token`` is ``base_url||api_token`` (compound format). ``registered_version_fn``
    (injected for an integration a registration owns) reads the version XTM One
    registered, named beside the live answer of ``get_opencti_platform_version``.
    """
    base_url, api_token = parse_compound_token(token, 2)
    # ``parse_compound_token`` does not normalise - strip whitespace and the
    # trailing slash exactly once so all downstream uses (GraphQL endpoint,
    # ``[Open in OpenCTI](...)`` markdown links, draft URLs) compose cleanly.
    # Strip the token too so a ``base_url|| token`` value can't produce a
    # malformed ``Authorization: Bearer  <token>`` header that fails auth.
    base_url = base_url.strip().rstrip("/")
    api_token = api_token.strip()
    # A platform-managed integration authenticates with a cross-platform JWT that
    # lives ~10 minutes - far less than a document-import or harvesting run, which
    # used to die on OpenCTI's "You must be logged in to do this" the moment the
    # build-time token expired, and stay dead for the rest of the run. Carrying
    # the dispatcher's re-minter on the headers themselves is what lets every
    # call recover: ``_client`` upgrades to an ``AutoRefreshClient`` and
    # ``_graphql`` re-mints proactively (before expiry) and reactively (on an
    # auth-shaped GraphQL error). ``refresh_fn`` is None for a static API key, in
    # which case this behaves exactly like the plain dict it replaces.
    headers = RefreshableHeaders(
        {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        refresh_fn=_kwargs.get("refresh_fn"),
    )

    # Forward the opencti-draft-id header when present in scope_headers so that
    # all GraphQL calls are scoped to the correct draft context.
    scope_headers: dict[str, str] = _kwargs.get("scope_headers") or {}
    draft_id: str = scope_headers.get("opencti-draft-id", "")
    if draft_id:
        headers["opencti-draft-id"] = draft_id

    # Autonomous = server-side run with no interactive user (e.g. an assignment).
    # The knowledge draft gate uses this to auto-provision a draft on the first
    # mutation instead of instructing the agent to ask the user.
    autonomous: bool = bool(_kwargs.get("autonomous"))

    # Direct mutations = bypass the draft gate entirely: knowledge mutations
    # write straight to the live graph (no draft staging, no gate messaging).
    # Used by the platform MCP servers (/mcp/opencti), where the caller is an
    # external MCP client operating statelessly with its own review workflow.
    direct_mutations: bool = bool(_kwargs.get("direct_mutations"))
    # The integration's opt-in Live writes setting: the draft gate is skipped
    # as above, and apply_opencti_draft is offered. Off by default.
    live_writes: bool = bool(_kwargs.get("live_writes"))
    # The run's fleet gates apply_opencti_draft (#1480, the CTEM loop's analyst
    # review): the tool is offered with Live writes off, the gate decides it.
    gated_draft_validation: bool = bool(_kwargs.get("gated_draft_validation"))
    # With that gate, the lookup of the draft holding a case's recorded threat
    # package (#2089): the only other draft delete_opencti_draft may discard.
    case_draft_fn = _kwargs.get("case_draft_fn")

    # Set when the agent has human-in-the-loop enabled: a callback that verifies
    # a human actually approved a given irreversible action (see
    # _make_hitl_approval_fn in services/integration_tools.py). Merge uses it to
    # escalate for approval instead of refusing outright in autonomous runs.
    hitl_approval_fn = _kwargs.get("hitl_approval_fn")

    if not base_url:
        logger.debug(
            "OpenCTI base_url is empty - returning no tools. Edit the "
            "integration and ensure the base URL is set."
        )
        return []

    tools: list[ToolDefinition] = []
    # STIX knowledge + draft + NLQ tools. Dev/debug PoC tools live here too but
    # are gated behind the DEBUG setting (off in production) inside register.
    tools += register_knowledge(
        base_url,
        headers,
        executor,
        draft_id,
        autonomous=autonomous,
        direct_mutations=direct_mutations,
        live_writes=live_writes,
        gated_draft_validation=gated_draft_validation,
        case_draft_fn=case_draft_fn,
        hitl_approval_fn=hitl_approval_fn,
        persist_file_fn=_kwargs.get("persist_file_fn"),
    )
    # Platform management domains. These are NOT draft-gated: OpenCTI drafts
    # only scope STIX knowledge changes, not platform configuration.
    tools += register_playbooks(base_url, headers, executor)
    tools += register_workspaces(base_url, headers, executor)
    tools += register_access(base_url, headers, executor, registered_version_fn=registered_version_fn)
    tools += register_notifications(base_url, headers, executor)
    tools += register_customization(base_url, headers, executor)
    tools += register_audit(base_url, headers, executor)
    tools += register_pir(base_url, headers, executor)
    tools += register_security_coverage(base_url, headers, executor)
    tools += register_ingestion(base_url, headers, executor)
    tools += register_markings(base_url, headers, executor)
    tools += register_data_sharing(base_url, headers, executor)
    tools += register_connectors(base_url, headers, executor)
    tools += register_health_reads(base_url, headers, executor)
    # Unattended runs act for a user who did not ask for anything (a per-user
    # fan-out runs as every user in turn): never send a query the user's role
    # cannot pass, or OpenCTI records it as their unauthorized access.
    if autonomous:
        install_capability_gate(executor, base_url, headers)
    # An 8-hex id prefix is answered by OpenCTI as a missing object: refuse it by
    # name before any request, the capability gate's probe included
    # (ids.short_id_refusal).
    install_short_id_guard(executor, [tool.name for tool in tools])
    # An argument the chosen action does not apply never refuses the whole action:
    # the action runs and the answer names what was ignored (access._refuse_unapplied).
    install_ignored_arguments_note(executor, [tool.name for tool in tools])
    # How this platform names the objects it holds, read on the tools' own
    # connection (naming.platform_names_lookup): the grounded STIX extraction
    # names its objects so, and its bundle merges into them.
    if callable(platform_names_sink) and tools:
        platform_names_sink(platform_names_lookup(base_url, headers))
    return tools

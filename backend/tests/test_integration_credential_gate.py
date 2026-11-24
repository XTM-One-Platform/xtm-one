"""Tripwire: every read of a stored integration token is accounted for.

OAuth integration tokens are personal: only the owner may use one unless
they shared it. The rule was once hand-copied into eight readers, and several
other readers never got it - the copy-paste is how they were missed. The rule
now has one definition, ``app.services.integration_credentials``, and this test
fails the build when a function under ``backend/app`` reads
``access_token_enc`` / ``refresh_token_enc`` without being listed below with the
reason it is safe.

Adding a reader: gate it with ``oauth_credentials_usable`` /
``oauth_credentials_usable_clause`` / ``require_oauth_credentials_usable`` and
list it in ``GATED_READS``. A reader that acts for a person resolves the token
holder - the integration, or that person's own credential on an integration
whose people each connect their own account - through
``app.services.integration_user_credentials`` (``usable_credential_holder``,
``reader_clause`` + ``resolve_token_holder``), which applies the same rule.
The scan reads the column names, so a person's own credential
(``IntegrationUserCredential``) is covered too. A credential an entity was bound to (an agent's
bindings, a flow node, a KB sync source, a skill source) is used by that
entity's runs as it always was - the write that bound it checks its writer, and
nothing is withheld at run time (maintainer decision, 2026-09-27) - so its reader goes in ``BOUND_READS``.
"""

from __future__ import annotations

import ast
import functools

from tests import app_sources

TOKEN_ATTRS = frozenset({"access_token_enc", "refresh_token_enc"})
HELPER_MODULE = "app.services.integration_credentials"
#: The rule's module, and the per-person holder resolution built on it.
HELPER_MODULES = frozenset({HELPER_MODULE, "app.services.integration_user_credentials"})

#: Reads made after the rule was applied (``module:function`` -> how).
GATED_READS: dict[str, str] = {
    "services/event_poller.py:_poll_credential": (
        "handed only the integration of poll_integration_events' query gated on the "
        "agent's owner (reader_clause) and the holder resolved for them"
    ),
    "services/flow_triggers.py:poll_flow_integration_events": (
        "query gated on the flow's owner (reader_clause), holder resolved for them"
    ),
    "api/v1/integration_notifications.py:microsoft_graph_notification": (
        "query gated on the agent's owner (reader_clause), holder resolved for them"
    ),
    "services/graph_event_subscriptions.py:graph_token_for": (
        "the poller's rule: query gated on the agent's acting user (connected_for_clause, "
        "reader_clause), holder resolved for them"
    ),
    "api/v1/integration_elements.py:list_integration_elements": (
        "the caller's holder, or 403 (usable_credential_holder)"
    ),
    "api/v1/integration_github_webhooks.py:_resolve_token": (
        "403 for a caller who may not use the token"
    ),
    "api/v1/integrations.py:_test_integration": (
        "403 for a caller who may not use the token (test_integration, before the call)"
    ),
    "services/kb_sync.py:_get_integration_token": (
        "without an explicit binding: the user's own rows or the gated "
        "company-wide clause (the bound integration is used as bound, or the "
        "user's own credential on a per_user one)"
    ),
    "services/agent_accounts/pipeline.py:open_integration_session": (
        "query gated on the account's owner (reader_clause), holder resolved for them"
    ),
}

#: Credentials an entity was bound to, used by its runs as bound. The write that
#: binds one checks its writer (a 403 or a 422 on a token they may not use, or
#: an integration they cannot see); a non-owner's chat turn still has its
#: unshared OAuth bindings shadowed first (``shadow_invisible_bindings``).
BOUND_READS: dict[str, str] = {
    "services/integration_tools.py:_get_token": (
        "the tool-token funnel: resolves the bindings it is handed"
    ),
    "services/integration_tools.py:_refresh_inline": (
        "refreshes a row _get_token or the build resolves (through _try_inline_refresh, "
        "which sets the row's relay route)"
    ),
    "services/integration_tools.py:_do_inline_refresh": "same as _try_inline_refresh",
    "services/integration_tools.py:_refresh_inline_locked": (
        "same as _try_inline_refresh: re-reads that row under the refresh lock"
    ),
    "services/integration_tools.py:_record_inline_refresh_failure": (
        "same as _try_inline_refresh: hands back that row's stored token"
    ),
    "services/integration_tools.py:_inline_refresh_on_cooldown": (
        "same as _try_inline_refresh: the back-off digest of that row's "
        "ciphertext; never decrypted"
    ),
    "services/integration_tools.py:_needs_proactive_refresh": (
        "predicate over the build's rows"
    ),
    "services/integration_tools.py:_acquire_sharepoint_token": (
        "closure the build creates for a binding it resolves"
    ),
    "services/integration_tools.py:_acquire_azure_delegated_token": (
        "closure the build creates for a binding it resolves"
    ),
    "services/skill_source.py:resolve_skill_credential": (
        "the skill's bound source credential; binding one is checked "
        "(agent_skills._resolve_source_credential)"
    ),
    "services/custom_tool_sources.py:resolve_access": (
        "the custom tool source's bound credential, never auto-discovered; "
        "binding one is checked (custom_tool_sources._bind_integration: "
        "require_oauth_credentials_usable, per-user integrations refused)"
    ),
}

#: Reads that are not a use of the token on anyone's behalf.
EXEMPT_READS: dict[str, str] = {
    "api/v1/integrations.py:_to_response": (
        "presence flag (has_access_token) in the integration's own response; "
        "never decrypted"
    ),
    "api/v1/users.py:_user_integration_item": (
        "presence flag (has_access_token) and granted-scope check in the "
        "administrator's list of a person's integrations (#11837); never decrypted"
    ),
    "services/builtin_tools/agent_builder.py:_oauth_scope_lines": (
        "presence check deciding whether get_integration reports the scopes "
        "granted to the stored token; never decrypted, never returned"
    ),
    "services/fleet_about.py:integration_state": (
        "presence check deciding whether an expired token can still be refreshed, "
        "for a fleet's prerequisite line; never decrypted, never returned"
    ),
    "services/integration_credentials.py:apply_oauth_share_consent": (
        "presence check deciding whether an edit records consent for a token "
        "already stored on the row; never decrypted"
    ),
    "services/resource_exposure.py:describe_secret": (
        "presence check naming what a resource holds for the sharing and access "
        "view; never decrypted, never returned"
    ),
    "api/v1/integrations.py:_credential_status": (
        "presence flag (has_access_token) of a person's own credential, served "
        "to that person only; never decrypted"
    ),
    "services/integration_user_credentials.py:credential_connected": (
        "presence check deciding whether a person's own credential holds an "
        "account; never decrypted"
    ),
    "services/integration_user_credentials.py:usable_credential_clause": (
        "SQL form of the same presence check; never decrypted"
    ),
    "integrations/oauth.py:apply_oauth_token_expiry": (
        "presence check deciding whether a token with no stated lifetime can be "
        "refreshed, and so takes the synthetic hour; never decrypted"
    ),
    "services/integration_credentials.py:_holds_oauth_token": (
        "presence check deciding whether a row holds a token whose connector "
        "is worth recording or showing; never decrypted"
    ),
    "services/token_refresh.py:_do_refresh_integration": (
        "background refresh of the row's own token, stored back on the same "
        "row - nothing leaves it"
    ),
    "services/slack_events.py:_receiving_integrations": (
        "presence filter on the Slack integrations whose app signed a delivery; "
        "the runs it reaches are gated by eligible_integration_ids"
    ),
    "services/slack_events.py:_receiving_integrations._resolve": (
        "auth.test with the row's own token: which workspace and Slack user it "
        "is, to route the delivery; only those ids are kept, no run acts "
        "with the token"
    ),
    "api/oauth_callbacks.py:_installer_integrations": (
        "GET /user with the row's own token: which GitHub account it is, compared "
        "with the installer's for a state-less GitHub App install; only "
        "the account id is kept, no run acts with the token"
    ),
    "api/v1/webhooks.py:_token_row_columns": (
        "GET /user/installations with the row's own token, in the background: "
        "which installations of the admin GitHub App it sees, to route a "
        "strict-mode delivery to integrations _admin_app_integrations "
        "already found usable by the agent's owner; only installation ids and a "
        "keyed digest of the ciphertexts are kept, no run acts with the token"
    ),
    "api/oauth_callbacks.py:_record_on_proven": (
        "ciphertext compared with the one the proof was made with; "
        "never decrypted"
    ),
    "api/oauth_callbacks.py:_record_after_token_change": (
        "GET /user with the row's own new token after a refresh during the "
        "proof, like _installer_integrations; only the account id is "
        "kept, no run acts with the token"
    ),
    "services/token_refresh.py:refresh_expiring_tokens": (
        "sweep query selecting the rows to refresh"
    ),
    "services/token_refresh.py:_refresh_one_integration": (
        "the back-off digest of the row's ciphertext; never decrypted"
    ),
    "services/token_refresh.py:_refresh_still_due": (
        "presence check on the stored row the sweep refreshes; never decrypted"
    ),
    "services/token_refresh.py:stored_tokens": (
        "ciphertexts compared to tell a replaced grant; never decrypted"
    ),
    "services/token_refresh.py:_refresh_one_mcp": (
        "MCPServer.refresh_token_enc - same"
    ),
    "services/token_refresh.py:_mcp_refresh_still_due": (
        "MCPServer.refresh_token_enc - same"
    ),
    "services/token_refresh.py:_do_refresh_mcp": (
        "MCPServer.refresh_token_enc - same"
    ),
    "services/token_refresh.py:current_mcp_access_token": (
        "MCPServer.refresh_token_enc - same (a connection's refresh)"
    ),
    "services/token_refresh.py:_sendable_token": (
        "MCPServer.access_token_enc - same, sent to that server only"
    ),
    "services/token_refresh.py:_refresh_mcp_in_use": (
        "MCPServer.refresh_token_enc - same"
    ),
    "api/v1/helpers.py:build_mcp_runtime": (
        "MCPServer.access_token_enc, or on a per_user server the acting person's own "
        "MCPServerUserCredential (never another person's) - an MCP server's own OAuth "
        "token, a different entity with its own consent model"
    ),
}

#: Token resolvers private to the funnel -> modules allowed to import them.
PRIVATE_RESOLVERS: dict[str, set[str]] = {
    # The investigation reads only the registered, platform_managed OpenCTI
    # (only_registered_opencti): no token is stored, one is minted for the caller.
    # A dashboard's live widgets and publishing read one integration the caller
    # chose, after its own gate (visible as a non-admin, provider, the
    # credential usable by the caller - oauth_credentials_usable): a token is
    # minted for the caller on a platform_managed integration.
    # Trigger provisioning (#8991) reads the one OpenCTI integration bound to the
    # assignment or its agent, for the assignment's run-as user: the binding was
    # gated when it was set, and the token only creates or removes that
    # assignment's own notifier and trigger; the OpenAEV provisioner does the
    # same with the OpenAEV integration bound to the assignment or its agent.
    # The OpenCRQ primary-asset sync (#8987) reads the company-managed OpenCRQ
    # visible to the administrator who runs it (integration_visible_filter), with
    # a token minted for that administrator.
    "_get_token": {
        "services/ctem_metrics_collector.py",
        # OpenCRQ job-run events: polled for the assignment's acting user (#8991 S2d).
        "services/event_poller.py",
        "services/investigation_queriers.py",
        # The sender's directory profile: the company directory bound to the
        # agent of the turn, for the turn's acting user; the lookup requests the
        # allow-listed directory fields only (services/sender_directory.py).
        "services/sender_directory.py",
        # Jira dynamic webhooks (#11036 S2): the one Jira OAuth integration bound
        # to the assignment or its agent, for the assignment's run-as user
        # (provisioning_user_id); the token only registers, refreshes or deletes
        # that assignment's own webhook.
        "services/jira_webhooks.py",
        # CTEM ticketing sync (#917): the one Jira or ServiceNow integration an
        # administrator declared as the organization's ticketing target, for its
        # owner (integration.user_id); the token only finds, opens, updates and
        # reads the CTEM case tickets of that target (adapter_for).
        "services/ctem_ticketing.py",
        # CTEM attack surface pass (#1110): the one provider integration an administrator
        # declared in the profile (attack_surface.integration), for its owner
        # (integration.user_id); the token only reads the outside view of the owned
        # domains and address ranges (reader_for).
        "services/ctem_attack_surface.py",
        # CTEM-26 reachability assertions (#1164): the OpenAEV integration a verified
        # delivery came through, for its owner (integration.user_id); the token only
        # reads the delivered scenario's assertion flag (stamp_reachability_assertion).
        "services/ctem_openaev_events.py",
        # CTEM evidence health pass (#1161): the one OpenCRQ integration an administrator
        # declared in the profile (evidence_health.integration), for its owner
        # (integration.user_id); the token only reads the evidence health of the
        # tenant's risk scenarios (reader_for).
        "services/ctem_evidence_health.py",
        "services/openaev_trigger_provisioning.py",
        "services/opencti_trigger_provisioning.py",
        "services/organization_context_opencrq.py",
        # The OpenCTI identity link and PIR sync of tracked organizations (#9157
        # M3): the company-managed OpenCTI visible to the administrator who runs
        # it (integration_visible_filter), a token minted for that administrator.
        "services/organization_opencti.py",
        "services/platform_access.py",
        # Wordsmith webhook endpoints (#160): the same rule as jira_webhooks, the
        # token only creates, checks or deletes the assignment's own endpoint and
        # reads its event log for catch-up.
        "services/wordsmith_webhooks.py",
    },
    "_refresh_by_id": set(),
    "_try_inline_refresh": set(),
    "_refresh_inline": set(),
    "_do_inline_refresh": set(),
    "_make_refresh_fn": set(),
    "_make_sharepoint_token_fn": set(),
    "_make_azure_delegated_token_fn": set(),
    "_parallel_refresh_expiring_tokens": set(),
    "_acquire_sharepoint_token": set(),
    "_acquire_azure_delegated_token": set(),
}

@functools.cache
def _modules() -> tuple[tuple[str, ast.Module], ...]:
    return tuple(app_sources.modules())


def _enclosing(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.AST]:
    chain = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            chain.append(node)
    return chain


def _qualname(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names = [scope.name for scope in reversed(_enclosing(node, parents))]
    return ".".join(names) or "<module>"


def _is_token_read(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr in TOKEN_ATTRS and isinstance(node.ctx, ast.Load)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "attrgetter"}
        and any(isinstance(a, ast.Constant) and a.value in TOKEN_ATTRS for a in node.args)
    )


def _token_reads() -> dict[str, list[int]]:
    reads: dict[str, list[int]] = {}
    for module, tree in _modules():
        parents = app_sources.parents(tree)
        for node in app_sources.walk(tree):
            if _is_token_read(node):
                key = f"{module}:{_qualname(node, parents)}"
                reads.setdefault(key, []).append(node.lineno)
    return reads


def _imports_helper(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and node.module in HELPER_MODULES
        for node in app_sources.walk(tree)
    )


def test_the_scan_sees_reads_and_ignores_writes():
    tree = ast.parse(
        "def f(i):\n"
        "    i.access_token_enc = None\n"
        "    decrypt_value(i.refresh_token_enc)\n"
        "    getattr(i, 'access_token_enc')\n"
    )
    assert sorted(node.lineno for node in ast.walk(tree) if _is_token_read(node)) == [3, 4]


def test_every_stored_token_read_is_accounted_for():
    reads = _token_reads()
    allowed = GATED_READS | BOUND_READS | EXEMPT_READS

    unexpected = {key: lines for key, lines in reads.items() if key not in allowed}
    assert not unexpected, (
        "New read(s) of a stored integration token: "
        + ", ".join(f"{key} (lines {sorted(lines)})" for key, lines in sorted(unexpected.items()))
        + ". Gate the read with app.services.integration_credentials (a personal "
        "OAuth token is only usable by its owner unless they shared it), "
        "then list it in GATED_READS with how it is gated."
    )
    stale = sorted(set(allowed) - set(reads))
    assert not stale, f"No longer reads a stored token - drop the entry: {stale}"


def test_gated_readers_apply_the_shared_helper():
    trees = dict(_modules())
    missing = sorted(
        module
        for module in {key.split(":", 1)[0] for key in GATED_READS}
        if not _imports_helper(trees[module])
    )
    assert not missing, f"Listed as gated but never import {sorted(HELPER_MODULES)}: {missing}"


def test_the_private_token_resolvers_stay_inside_the_funnel():
    leaks = []
    for module, tree in _modules():
        if module == "services/integration_tools.py":
            continue
        for node in app_sources.walk(tree):
            used: set[str] = set()
            if isinstance(node, ast.ImportFrom) and node.module == "app.services.integration_tools":
                used = {alias.name for alias in node.names}
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "integration_tools"
            ):
                used = {node.attr}
            for name in used & PRIVATE_RESOLVERS.keys():
                if module not in PRIVATE_RESOLVERS[name]:
                    leaks.append(f"{module}:{node.lineno} uses {name}")
    assert not leaks, (
        "A private token resolver is used outside the tool funnel, which "
        f"resolves only the bindings it is handed: {leaks}"
    )


def test_both_chat_entry_points_share_the_session_override_resolver():
    trees = dict(_modules())
    for module in ("services/agent_chat.py", "services/general_chat.py"):
        calls = [
            node
            for node in app_sources.walk(trees[module])
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "resolve_session_integration_bindings"
        ]
        assert calls, f"{module} must resolve session_integration_ids through the shared resolver"


def test_both_chat_entry_points_say_when_an_integration_is_withheld():
    """A withheld integration is told to the model, never a silent tool absence."""
    trees = dict(_modules())
    for module in ("services/agent_chat.py", "services/general_chat.py"):
        calls = [node for node in app_sources.walk(trees[module]) if isinstance(node, ast.Call)]
        resolver_calls = [
            node
            for node in calls
            if getattr(node.func, "id", None) == "resolve_session_integration_bindings"
        ]
        assert all(
            any(kw.arg == "refused" for kw in node.keywords) for node in resolver_calls
        ), f"{module} must collect what the session resolver withholds (refused=...)"
        assert any(
            getattr(node.func, "id", None) == "format_unshared_oauth_prompt_note"
            for node in calls
        ), f"{module} must tell the model which integrations were withheld"

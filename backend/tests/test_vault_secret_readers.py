"""Tripwire: every reader of a secret column that accepts a vault reference resolves it.

A secret column listed in ``app.services.vault.REFERENCE_COLUMNS`` holds a
value or a ``vault://`` reference. The code that USES the secret reads it
through ``reveal_field`` / ``reveal_ciphertext`` / ``reveal_json_field``, which
resolve the reference; ``decrypt_value`` refuses one, so a reader that was not
taught references fails closed rather than sending the reference as the
credential - but it then does not work for a referenced secret, and
``decrypt_json`` / ``decrypt_stored_value`` would hand the reference over.

This test fails the build when a function under ``backend/app`` decrypts one
of those columns - through the decrypt helpers or any wrapper of them, found
by following the calls, a helper handed to ``asyncio.to_thread`` /
``functools.partial`` / ``run_in_executor`` included - without being listed
below with the reason it is safe. The Fernet key stays in ``core/security.py``
(a module decrypting with it directly would escape the scan), and a browser
profile's credentials reach a browser only through ``resolved_snapshot``.
"""

from __future__ import annotations

import ast
import functools

from app.services.vault import REFERENCE_COLUMNS
from tests import app_sources

COLUMN_NAMES = frozenset(column for columns in REFERENCE_COLUMNS.values() for column in columns)
BASE_DECRYPTERS = frozenset({"decrypt_value", "decrypt_json", "decrypt_stored_value"})
#: The resolving readers: never counted as decrypting.
REVEALERS = frozenset({"reveal_field", "reveal_ciphertext", "reveal_json_field"})

#: Reads of the stored text that never USE the secret (``module:function`` -> why).
STORED_READS: dict[str, str] = {
    "api/v1/integrations.py:_build_bundle_integration_instance_payload": (
        "the integration export carries the stored secrets as written: a reference stays a reference"
    ),
    "api/v1/settings.py:_gateway_url": (
        "shows the URL half of a gateway credential as written: a reference is not a URL, so it shows nothing"
    ),
    "main.py:_seed_llm_providers_from_env._sync_compound_provider_base_url": (
        "rewrites the base URL half of an env-seeded compound key, keeping the key half as stored"
    ),
    "services/github_token_installations.py:token_candidate": (
        "only checks a token is stored (a reference counts); the lookup reveals it in _reveal_token"
    ),
    "services/openrouter_llm_provider.py:_stored_key": (
        "copies the OpenRouter integration's key into the LLM provider config it feeds: a reference "
        "stays a reference, which the LLM router resolves"
    ),
    "services/openrouter_llm_provider.py:_holds_key": (
        "compares a fed config's stored key with the integration's, as written, never uses it"
    ),
    "services/openrouter_llm_provider.py:_on_integration_update": (
        "compares and copies the stored keys of the integration and the configs it feeds, never uses them"
    ),
    "services/integration_state.py:admin_oauth_app": (
        "copies an OAuth default's client secret onto an integration (REST and agent builder create and "
        "update) as written: a reference stays a reference, which the integration's readers resolve"
    ),
    "api/v1/security_audit.py:_refused_webhook_configs": (
        "only tells a stored LinkedIn client secret is readable or a reference; a reference is then read "
        "through the gate's own reveal_webhook_client_secret (_unreadable_vault_client_secrets)"
    ),
    "api/v1/webhooks.py:receive_provider_webhook": (
        "_verify_provider_signatures decrypts the webhook config (extra_config_enc) only; the OAuth default's "
        "client secret it is handed is read through reveal_webhook_client_secret in the LinkedIn gate"
    ),
    "api/v1/channel_bots.py:update_channel_bot": (
        "merges the posted extra_config keys into the stored document as written: a reference stays a "
        "reference, which the channel adapters resolve through reveal_json_field"
    ),
    "a2a/client/credentials.py:_stored_config": (
        "reads the partner card URLs alone, never a credential: the mint flows read the credentials "
        "through _config, which resolves a reference with reveal_json_field"
    ),
    "services/integration_tools.py:_compose_style_of": (
        "reads the compose_style display setting alone, never a credential: a reference is not a style, so the "
        "default style applies"
    ),
    "services/integration_tools.py:_extract_github_binding_metadata": (
        "reads the GitHub installation ids, logins and account types for the binding log alone, never a credential"
    ),
    "services/integration_tools.py:get_integration_public_url": (
        "shows the base_url host as written: a reference is not an http(s) URL, so it shows nothing; the "
        "credential readers resolve the URL through reveal_json_field"
    ),
    "api/v1/webhooks.py:_shown_platform_url": (
        "shows the base_url of an event's platform as written, for the links of a prompt: a reference is not a "
        "URL, so it shows nothing, never a credential"
    ),
    "services/integration_state.py:api_key_binding_of": (
        "compares the address field an API key is bound to before and after an edit, as stored; never uses a "
        "credential, and a reference compares as the text written"
    ),
    "services/browser_profiles/profiles.py:read_credentials": (
        "the stored credentials, for the write's merge and what a response says (set, read from a vault): "
        "snapshot() leaves a reference out and a browser gets resolved_snapshot(), which reads it through the resolver"
    ),
}

#: Modules allowed to hold the platform's Fernet key, with why (``core/security.py`` owns it).
FERNET_HOLDERS: dict[str, str] = {
    "core/security.py": "the platform's encryption at rest",
    "services/relay_transport.py": "seals short-lived relay stream frames in Redis, never a stored secret",
}

#: Synchronous readers that cannot reach a vault: ``decrypt_value`` refuses a reference
#: there, so they fail closed, and their async callers resolve it first.
FAIL_CLOSED_READS: dict[str, str] = {
    "llm/router.py:build_provider": (
        "builds with the key _cached_provider resolved; decrypts only for a caller that passed none"
    ),
}

#: Why a reader of an integration's or an OAuth default's ``extra_config_enc`` is listed:
#: those columns still refuse references (``PENDING`` in test_vault_reference_coverage.py,
#: issue #6080); the channel bots' column of the same name accepts them.
_PENDING_EXTRA_CONFIG = (
    "reads an integration's or an OAuth default's extra_config_enc, which refuses references until "
    "those columns leave PENDING (#6080): the channel bots' column of the same name is the one accepting them"
)

#: The same column name on a model whose column refuses references: ``decrypt_value``
#: fails closed on one, and the write paths never store one there.
OTHER_COLUMN_READS: dict[str, str] = {
    "api/oauth_callbacks.py:_load_extra": _PENDING_EXTRA_CONFIG,
    "api/v1/integration_github_webhooks.py:_get_extra_config": _PENDING_EXTRA_CONFIG,
    "api/v1/integrations.py:_merge_masked_extra_config": _PENDING_EXTRA_CONFIG,
    "api/v1/integrations.py:_merge_registration_extra_config": _PENDING_EXTRA_CONFIG,
    "api/v1/integrations.py:_test_integration": _PENDING_EXTRA_CONFIG,
    "api/v1/integrations.py:_to_response": _PENDING_EXTRA_CONFIG,
    "api/v1/integrations.py:_update_repoints_integration": _PENDING_EXTRA_CONFIG,
    "api/v1/integrations.py:masked_stored_extra_config": _PENDING_EXTRA_CONFIG,
    "api/v1/integrations.py:required_extra_refusal": _PENDING_EXTRA_CONFIG,
    "api/v1/integrations.py:start_oauth": _PENDING_EXTRA_CONFIG,
    "api/v1/integrations.py:update_integration": _PENDING_EXTRA_CONFIG,
    "api/v1/nova_rules.py:update_nova_models": _PENDING_EXTRA_CONFIG,
    "api/v1/platform.py:register_platform": _PENDING_EXTRA_CONFIG,
    "api/v1/settings.py:_stored_oauth_default_extra": _PENDING_EXTRA_CONFIG,
    "api/v1/users.py:_user_integration_item": _PENDING_EXTRA_CONFIG,
    "api/v1/webhooks.py:_admin_app_integrations": _PENDING_EXTRA_CONFIG,
    "api/v1/webhooks.py:_handle_hubspot_assignment_webhook": _PENDING_EXTRA_CONFIG,
    "api/v1/webhooks.py:_handle_hubspot_webhook": _PENDING_EXTRA_CONFIG,
    "api/v1/webhooks.py:provider_webhook_unverified_reason": _PENDING_EXTRA_CONFIG,
    "api/v1/webhooks.py:receive_assignment_webhook": _PENDING_EXTRA_CONFIG,
    "api/v1/webhooks.py:receive_integration_webhook": _PENDING_EXTRA_CONFIG,
    "integrations/providers/openapi.py:decrypt_extra": _PENDING_EXTRA_CONFIG,
    "services/builtin_tools/agent_builder.py:build_agent_builder_tool._update_integration": _PENDING_EXTRA_CONFIG,
    "services/integration_notification_auth.py:load_slack_signing_secrets": _PENDING_EXTRA_CONFIG,
    "services/integration_user_credentials.py:account_extra": _PENDING_EXTRA_CONFIG,
    "services/integration_user_credentials.py:configuration_extra": _PENDING_EXTRA_CONFIG,
    "services/lmstudio_llm_provider.py:_refusal": _PENDING_EXTRA_CONFIG,
    # Rewrites the stored document with the generated receiver secret added: the secret it
    # uses is read through reveal_json_field first, and a revealed copy must never be stored.
    "services/trigger_provisioning.py:receiver_secret": _PENDING_EXTRA_CONFIG,
}


@functools.cache
def _modules() -> tuple[tuple[str, ast.Module], ...]:
    return tuple(app_sources.modules())


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _decrypt_call_args(
    call: ast.Call, decrypters: frozenset[str] | set[str], *, keywords: bool = True
) -> list[ast.AST] | None:
    """The arguments a decrypt helper receives through *call*, or ``None`` when *call* decrypts nothing.

    A direct call (``decrypt_value(x)``) hands it every argument; a helper passed
    on (``asyncio.to_thread(decrypt_value, x)``, ``partial(decrypt_json, x)``,
    ``loop.run_in_executor(None, decrypt_value, x)``) the arguments after it.
    """
    named = [keyword.value for keyword in call.keywords] if keywords else []
    if _callee(call) in decrypters:
        return [*call.args, *named]
    for index, argument in enumerate(call.args):
        if _name_of(argument) in decrypters:
            return [*call.args[index + 1 :], *named]
    return None


def _passes_parameter(argument: ast.AST, params: set[str]) -> bool:
    """Whether *argument* hands a parameter on as is.

    Deliberately narrow: wrappers are found by name across modules, so a wider
    shape (``enc or ""``, a keyword) makes every same-named function anywhere a
    decrypter (``_to_response`` is defined in fifteen route modules).
    """
    return isinstance(argument, ast.Name) and argument.id in params


def _decrypting_functions() -> frozenset[str]:
    """The decrypt helpers (their import aliases included) and every function decrypting a parameter, to a fixpoint."""
    known = set(BASE_DECRYPTERS)
    for _rel, module in _modules():
        for node in app_sources.walk(module):
            if isinstance(node, ast.ImportFrom):
                known.update(alias.asname for alias in node.names if alias.name in BASE_DECRYPTERS and alias.asname)
    changed = True
    while changed:
        changed = False
        for rel, module in _modules():
            if rel.startswith("services/vault/"):
                continue
            for node in app_sources.walk(module):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or node.name in known:
                    continue
                params = {arg.arg for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]}
                for call in ast.walk(node):
                    if not isinstance(call, ast.Call):
                        continue
                    # Positional only: wrappers are found by name across modules (see _passes_parameter).
                    received = _decrypt_call_args(call, known, keywords=False)
                    if received is not None and any(_passes_parameter(argument, params) for argument in received):
                        known.add(node.name)
                        changed = True
                        break
    return frozenset(known - REVEALERS)


def _qualname(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(node.name)
    return ".".join(reversed(names)) or "<module>"


def _columns_in(node: ast.AST, tainted: dict[str, set[str]], wrapper_params: frozenset[str] = frozenset()) -> set[str]:
    """The reference columns *node* reads, directly or through a local name assigned from one.

    A column is read as an attribute (``row.api_key_enc``), by its name as a
    string (``getattr(row, "api_key_enc")``, ``row["api_key_enc"]``), as a
    local or parameter carrying the column's name (the scalar a projected
    ``select(Model.api_key_enc)`` row unpacks into), or through a name tainted
    by one. A tainted name whose field is selected (``row.access_token_enc``)
    reads that field only. *wrapper_params* are the parameters of a decrypt
    wrapper: its callers are what is checked.
    """
    found: set[str] = set()

    def visit(child: ast.AST, selected: bool = False) -> None:
        if isinstance(child, ast.Attribute):
            if child.attr in COLUMN_NAMES:
                found.add(child.attr)
            visit(child.value, selected=True)
            return
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            # A method of a tainted value (``rows.first()``) hands back what it holds.
            visit(child.func.value)
            for argument in [*child.args, *(keyword.value for keyword in child.keywords)]:
                visit(argument)
            return
        if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value in COLUMN_NAMES:
            found.add(child.value)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            if child.id in COLUMN_NAMES and child.id not in wrapper_params:
                found.add(child.id)
            if not selected:
                found.update(tainted.get(child.id, set()))
        for grandchild in ast.iter_child_nodes(child):
            visit(grandchild)

    visit(node)
    return found


def _target_names(target: ast.AST) -> list[str]:
    return [node.id for node in ast.walk(target) if isinstance(node, ast.Name)]


def _tainted_names(function: ast.AST) -> dict[str, set[str]]:
    """Local names a function assigns from a reference column (``encrypted = row.api_key_enc or ...``).

    A loop or comprehension over a tainted value taints its target, so the rows
    of a projected ``select`` carry the columns it selected.
    """
    tainted: dict[str, set[str]] = {}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(function):
            if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
                pairs = [(target, node.value) for target in (node.targets if isinstance(node, ast.Assign) else [node.target])]
            elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
                pairs = [(node.target, node.iter)]
            else:
                continue
            for target, value in pairs:
                columns = _columns_in(value, tainted)
                for name in _target_names(target) if columns else ():
                    if not columns <= tainted.get(name, set()):
                        tainted.setdefault(name, set()).update(columns)
                        changed = True
    return tainted


def _parameters(function: ast.AST | None) -> frozenset[str]:
    if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
        return frozenset()
    arguments = function.args
    return frozenset(arg.arg for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs])


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return node
    return None


@functools.cache
def _decrypting_readers() -> dict[str, list[str]]:
    decrypters = _decrypting_functions()
    readers: dict[str, list[str]] = {}
    for rel, module in _modules():
        if rel.startswith("services/vault/"):
            continue
        parents = app_sources.parents(module)
        taint_cache: dict[ast.AST | None, dict[str, set[str]]] = {}
        for call in app_sources.walk(module):
            if not isinstance(call, ast.Call):
                continue
            received = _decrypt_call_args(call, decrypters)
            if received is None:
                continue
            function = _enclosing_function(call, parents)
            if function not in taint_cache:
                taint_cache[function] = _tainted_names(function) if function is not None else {}
            wrapper_params = _parameters(function) if getattr(function, "name", None) in decrypters else frozenset()
            columns = sorted(
                set().union(*(_columns_in(arg, taint_cache[function], wrapper_params) for arg in received))
            )
            if columns:
                readers.setdefault(f"{rel}:{_qualname(call, parents)}", []).append(
                    f"line {call.lineno}: {_callee(call)}({', '.join(columns)})"
                )
    return readers


def test_a_wrapper_handing_its_parameter_to_a_thread_is_a_decrypter():
    call = ast.parse("asyncio.to_thread(decrypt_value, enc)").body[0].value
    assert any(_passes_parameter(argument, {"enc"}) for argument in _decrypt_call_args(call, BASE_DECRYPTERS, keywords=False))
    assert not _passes_parameter(ast.parse("row.api_key_enc").body[0].value, {"row"})


def test_a_reference_column_given_by_keyword_is_seen():
    call = ast.parse("decrypt_value(ciphertext=row.api_key_enc)").body[0].value
    assert [ast.unparse(arg) for arg in _decrypt_call_args(call, BASE_DECRYPTERS)] == ["row.api_key_enc"]


def test_a_decrypt_helper_handed_to_another_call_is_seen():
    tree = ast.parse("asyncio.to_thread(decrypt_value, row.api_key_enc)\npartial(decrypt_json, x)\nlen(y)")
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    received = [_decrypt_call_args(call, BASE_DECRYPTERS) for call in calls]
    assert [ast.unparse(arg) for arg in received[0]] == ["row.api_key_enc"]
    assert [ast.unparse(arg) for arg in received[1]] == ["x"]
    assert received[2] is None


def test_the_scan_finds_the_decrypt_wrappers():
    decrypters = _decrypting_functions()
    assert {"decrypt_value", "decrypt_json", "_decrypt_dict"} <= decrypters
    assert not decrypters & REVEALERS


def _allowed() -> set[str]:
    return set(STORED_READS) | set(FAIL_CLOSED_READS) | set(OTHER_COLUMN_READS)


def test_every_reader_of_a_reference_column_resolves_it():
    allowed = _allowed()
    unlisted = {name: lines for name, lines in _decrypting_readers().items() if name not in allowed}
    assert unlisted == {}, "\n".join(
        f"{name} decrypts a column that may hold a vault reference ({'; '.join(lines)}).\n"
        "Read it with app.services.vault.reveal_field / reveal_ciphertext / reveal_json_field, or list it in "
        "STORED_READS / FAIL_CLOSED_READS / OTHER_COLUMN_READS (tests/test_vault_secret_readers.py) with why "
        "it is safe."
        for name, lines in sorted(unlisted.items())
    )


def test_every_listed_reader_still_decrypts():
    readers = _decrypting_readers()
    stale = sorted(name for name in _allowed() if name not in readers)
    assert stale == [], f"listed readers that no longer decrypt a reference column, remove them: {stale}"


def test_the_fernet_key_stays_where_the_scan_sees_it():
    """A module decrypting with the key itself would read a reference column unseen by the scan above."""
    holders: set[str] = set()
    for rel, module in _modules():
        for node in app_sources.walk(module):
            imports_key = isinstance(node, ast.ImportFrom) and any(alias.name == "_fernet" for alias in node.names)
            builds_key = isinstance(node, ast.Call) and _callee(node) in {"Fernet", "MultiFernet"}
            if imports_key or builds_key:
                holders.add(rel)
    assert holders <= set(FERNET_HOLDERS), (
        f"{sorted(holders - set(FERNET_HOLDERS))} use the Fernet key directly: decrypt through app.core.security "
        "(and a reference column through app.services.vault), or list the module in FERNET_HOLDERS with why."
    )
    assert set(FERNET_HOLDERS) <= holders, f"stale FERNET_HOLDERS entries: {sorted(set(FERNET_HOLDERS) - holders)}"


#: The stored-credential readers of a browser profile: a browser task never gets their values.
_PROFILE_STORED_READERS = frozenset({"snapshot", "read_credentials"})


def test_browser_profile_credentials_reach_a_browser_only_through_the_resolving_snapshot():
    """``snapshot()`` / ``read_credentials()`` hold a vault reference as written: outside ``profiles.py``,
    every browser task takes ``resolved_snapshot()``, which reads it through the resolver."""
    offenders: list[str] = []
    for rel, module in _modules():
        if rel == "services/browser_profiles/profiles.py":
            continue
        for node in app_sources.walk(module):
            if isinstance(node, ast.ImportFrom) and node.module == "app.services.browser_profiles.profiles":
                offenders.extend(f"{rel}: imports {alias.name}" for alias in node.names if alias.name in _PROFILE_STORED_READERS)
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in _PROFILE_STORED_READERS
                and isinstance(node.value, ast.Name)
                and node.value.id == "profiles"
            ):
                offenders.append(f"{rel}: profiles.{node.attr}")
    assert offenders == [], (
        "A browser profile's stored credentials are read outside profiles.py: take resolved_snapshot(), which reads "
        f"a credential held as a vault reference through the resolver: {offenders}"
    )

"""With AI disabled, reads stay open and writes refuse identically (US4).

Browsable AI data is the point of the mode: FR-008 requires every AI **read** to
answer 200 with AI disabled, and FR-025 requires that nothing is destroyed and
that re-enabling needs no manual repair. The refusal on the write side has to be
the *same* payload everywhere, so a client branches on ``detail.code`` rather
than parsing prose (FR-009, FR-010).
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from tests import app_sources

BACKEND = Path(__file__).resolve().parent.parent

# The gates that consult the resolved AI state, i.e. the ones that refuse as
# soon as an administrator switches AI off.
AI_STATE_GATES = {"require_agentic_ai", "require_chat_enabled"}

# A2A status reads, gated on purpose — see
# ``test_no_read_sits_behind_a_gate_that_consults_the_ai_state``.
_A2A_READS_FOLLOW_CHAT = {
    "get_exposure_status",
    "get_verification_state",
    "list_delegations",
}


def _router_level_dependencies(tree: ast.Module) -> dict[str, set[str]]:
    """``router = APIRouter(..., dependencies=[Depends(x)])`` → {"router": {"x"}}.

    A gate declared once on the router covers every route on it without
    appearing in any signature, so a detector that reads signatures alone
    would miss it — which is how the A2A routers are gated.
    """
    found: dict[str, set[str]] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        if getattr(node.value.func, "id", "") != "APIRouter":
            continue
        for kw in node.value.keywords:
            if kw.arg != "dependencies" or not isinstance(kw.value, ast.List):
                continue
            names = {
                getattr(arg, "id", None) or getattr(arg, "attr", None)
                for elt in kw.value.elts
                if isinstance(elt, ast.Call) and getattr(elt.func, "id", "") == "Depends"
                for arg in elt.args
            }
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = names
    return found


def _router_of(node: ast.AsyncFunctionDef | ast.FunctionDef) -> str | None:
    for d in node.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
            return getattr(d.func.value, "id", None)
    return None


def _get_routes_behind(gates: set[str]) -> list[str]:
    """Every ``@router.get`` handler behind one of *gates*, by signature or by
    router-level declaration."""
    offenders = []
    for path in app_sources.python_files(BACKEND / "app" / "api"):
        tree = app_sources.tree(path)
        router_deps = _router_level_dependencies(tree)
        for node in app_sources.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            methods = [
                d.func.attr
                for d in node.decorator_list
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            ]
            if "get" not in methods:
                continue
            for name in router_deps.get(_router_of(node) or "", set()) & gates:
                offenders.append(
                    f"{path.relative_to(BACKEND)}:{node.lineno} {node.name} <- {name}"
                )
            for default in node.args.defaults + list(node.args.kw_defaults):
                if not (
                    isinstance(default, ast.Call)
                    and getattr(default.func, "id", "") == "Depends"
                ):
                    continue
                for arg in default.args:
                    name = getattr(arg, "id", None) or getattr(arg, "attr", None)
                    if name in gates:
                        offenders.append(
                            f"{path.relative_to(BACKEND)}:{node.lineno} "
                            f"{node.name} <- {name}"
                        )
    return offenders


def test_no_read_sits_behind_a_gate_that_consults_the_ai_state():
    """T035 as a test rather than a one-off audit.

    Reads are the browsable surface the whole mode is built around, so one read
    gated by mistake defeats it — and the mistake is invisible, because the gate
    looks deliberate. The audit that wrote this test found six: four Nova rule
    reads and the platform chat-session history behind ``require_chat_enabled``,
    which also contradicted that gate's own docstring ("existing chat history
    stays reachable … only chat *write* paths 403").

    A read that genuinely needs the licence takes the ``*_licensed`` variant,
    which asks the licence and not the toggle.

    The one recorded exception is A2A, which is offered exactly while agentic
    chat is: with AI off there is nothing a partner can reach or task, so its
    status reads describe a surface that does not exist rather than browsable
    AI data. Its two routers carry ``require_chat_enabled`` at router level.
    """
    offenders = [
        o for o in _get_routes_behind(AI_STATE_GATES)
        if not any(f" {name} <- " in o for name in _A2A_READS_FOLLOW_CHAT)
    ]
    assert not offenders, (
        "these GET routes refuse as soon as AI is switched off, which FR-008 "
        f"forbids — use the *_licensed variant instead: {offenders}"
    )


def test_the_licensed_variants_do_not_consult_the_ai_state():
    """The whole point of the split — if these grow an AI check, every read they
    guard silently starts refusing again."""
    from app.api.v1.platform import is_agentic_ai_licensed, is_chat_licensed

    for fn in (is_agentic_ai_licensed, is_chat_licensed):
        source = inspect.getsource(fn)
        assert "is_ai_enabled" not in source, fn.__name__


def test_the_refusal_payload_is_one_implementation():
    """FR-009/FR-010: the same payload from every path, interactive or not.

    An API token, an integration and a browser must all get ``detail.code ==
    "ai_disabled"``, so the refusal is built in one place rather than spelled
    out per route.
    """
    from app.services.agentic_gate import refuse_ai_disabled
    from app.services.ai_state import AI_DISABLED_CODE

    source = inspect.getsource(refuse_ai_disabled)
    assert "AI_DISABLED_CODE" in source
    assert AI_DISABLED_CODE == "ai_disabled"

    with pytest.raises(Exception) as exc:
        refuse_ai_disabled()
    detail = exc.value.detail  # type: ignore[attr-defined]
    assert detail["code"] == "ai_disabled"
    assert exc.value.status_code == 403  # type: ignore[attr-defined]


def test_no_route_spells_the_refusal_out_by_hand():
    """A second copy of the payload is a second thing to drift.

    Any module raising ``ai_disabled`` itself, rather than through
    ``refuse_ai_disabled``, is how ``detail`` becomes a bare string on one
    route and a dict on the rest — which happened once already, in
    ``ctem_metrics``.
    """
    offenders = []
    for path in app_sources.python_files(BACKEND / "app"):
        if path.name in {"deps.py", "ai_state.py"}:
            continue
        text = app_sources.source(path)
        if '"ai_disabled"' not in text:
            continue
        # Naming the mode in prose or comparing PLATFORM_MODE is fine; building
        # a refusal detail with it is not.
        for line in text.splitlines():
            stripped = line.strip()
            if '"code": "ai_disabled"' in stripped or "'code': 'ai_disabled'" in stripped:
                offenders.append(f"{path.relative_to(BACKEND)}: {stripped}")
    assert not offenders, (
        f"build the refusal with refuse_ai_disabled() instead: {offenders}"
    )


def test_nothing_in_the_disable_path_deletes_anything():
    """FR-025, as far as a unit test can reach it.

    Disabling AI must destroy nothing and re-enabling must need no manual
    repair. The integration test covers the round trip; this pins the cheaper
    half — that the toggle path issues no delete at all, so a regression has to
    be introduced deliberately rather than by accident.

    Read from the AST, not by substring: the first version of this test matched
    the word "drop" inside ``invalidate_cache``'s docstring, which proves how
    little a text search says about what code does.
    """
    from app.api.v1 import settings as settings_api
    from app.services import ai_state

    def _delete_calls(source: str) -> list[str]:
        found = []
        tree = ast.parse(inspect.cleandoc(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # ``delete(Model)`` — the SQLAlchemy construct.
            if isinstance(node.func, ast.Name) and node.func.id == "delete":
                found.append("delete(...)")
            # ``db.delete(obj)`` / ``session.delete(obj)`` — the ORM call.
            if isinstance(node.func, ast.Attribute) and node.func.attr == "delete":
                found.append(f"{getattr(node.func.value, 'id', '?')}.delete(...)")
        return found

    toggle = _delete_calls(
        inspect.getsource(settings_api.update_platform_settings_endpoint)
    )
    state = _delete_calls(inspect.getsource(ai_state))
    assert not toggle, f"the toggle deletes something: {toggle}"
    assert not state, f"the AI-state module deletes something: {state}"


# --- T038: the MCP surface refuses entirely -------------------------------


def test_every_mcp_entrypoint_refuses_with_the_contract_payload():
    """FR-029: not partially, and not by disappearing.

    The endpoint exists to be consumed by a model, so any part left live keeps
    open the path this mode closes. That includes the *platform* MCP endpoints
    (OpenCTI/OpenAEV/OpenCRQ): the tools they front are product APIs rather
    than model calls, but the surface is still a model-consumption surface.

    Asserted per handler, because the failure mode is one of them being missed —
    ``mcp_endpoint`` refused via ``is_chat_enabled`` but with the *licence*
    message, which told an operator to buy what they already had.
    """
    from app.api.v1 import mcp_endpoint, mcp_platform

    for fn in (
        mcp_endpoint.mcp_user_endpoint,
        mcp_endpoint.mcp_global_endpoint,
        mcp_platform._platform_mcp_endpoint,
    ):
        source = inspect.getsource(fn)
        assert "refuse_ai_disabled" in source, fn.__name__
        assert "is_ai_enabled" in source, fn.__name__


def test_the_mcp_routes_still_exist_when_ai_is_off():
    """"Refuses" and "is not there" are different answers, and FR-029 asks for
    the first: a client that worked previously is told rather than left
    guessing. Routes are registered at import time, so their presence is the
    thing to assert — a future "just don't mount it" shortcut fails here."""
    from app.api.v1 import mcp_endpoint, mcp_platform

    user_paths = {r.path for r in mcp_endpoint.router.routes}  # type: ignore[attr-defined]
    assert {"/user", "/global"} <= user_paths

    platform_paths = {r.path for r in mcp_platform.router.routes}  # type: ignore[attr-defined]
    assert {"/opencti", "/openaev", "/opencrq"} <= platform_paths


def test_mcp_server_configuration_is_not_an_mcp_entrypoint():
    """``mcp_servers.py`` is CRUD for configured servers, not a surface a model
    consumes, so FR-029 does not reach it — its reads stay open (FR-008) and its
    writes are ordinary configuration. Recorded so the distinction is not
    re-litigated as an oversight."""
    from app.api.v1 import mcp_servers

    source = inspect.getsource(mcp_servers)
    assert "refuse_ai_disabled" not in source


# --- FR-031: the mode needs no licence, and is visible in telemetry -------


def test_an_ai_disabled_platform_needs_no_licence_to_run():
    """FR-031, asserted at the two places a licence could block a boot.

    The point of the requirement is shipping the platform before the AI
    capability is bought, so a licence wall on this mode defeats the use case.
    Two gates could impose one: the ASGI middleware, and the client-side wall
    driven by ``license_required``.
    """
    from app.core.config import Settings

    disabled = Settings(platform_mode="ai_disabled")

    # The middleware only gates copilot-like modes.
    source = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    cls = source.index("class LicenseGateMiddleware")
    body = source[cls : source.index("\nclass ", cls + 1)]
    assert "settings.is_copilot_like" in body
    assert disabled.is_copilot_like is False

    # And the payload that draws the client-side wall reports it as XTM One,
    # whose branch never sets license_required=True.
    assert disabled.is_xtm_one is True


def test_the_resolved_ai_state_is_reported_in_telemetry():
    """FR-031: "the number of deployments sitting AI-off is visible".

    The *resolved* state, not ``platform_mode`` — the runtime toggle means a
    ``copilot`` deployment can sit AI-off and an ``ai_disabled`` one AI-on, so
    counting the mode string answers a different question.
    """
    from app.services import telemetry

    assert hasattr(telemetry._state, "ai_enabled")
    registry = inspect.getsource(telemetry)
    assert '_single("ai_enabled"' in registry, "no countable gauge"
    assert '"ai_enabled": "true" if state.ai_enabled else "false"' in registry, (
        "not available as a label to slice other metrics by"
    )

    refresh = inspect.getsource(telemetry.refresh_telemetry_data)
    assert "is_ai_enabled" in refresh, "the snapshot never resolves the state"


# --- Every AI execution path is gated server-side --------------------------


def test_no_ai_execution_endpoint_relies_on_the_ui_alone():
    """A disabled button is an affordance, not a gate (FR-001, SC-005).

    `/code-review` found four families of endpoint that started real model or
    embedding traffic with AI off, because the work had only disabled the
    button: manually triggering an assignment, triggering or force-resyncing a
    knowledge-base source, uploading or reprocessing a document, and running an
    evaluation. A direct API call, an API token or any other client reached all
    of them — and `useAiEnabled()`'s own docstring claimed "the backend gate is
    what actually refuses work", which was not true.

    Listed by name rather than counted: a new execution endpoint should have to
    appear here deliberately.
    """
    import ast

    expected = {
        "app/api/v1/assignments.py": {"trigger_assignment_manual"},
        "app/api/v1/knowledge_bases.py": {
            "trigger_sync_source",
            "force_resync_source",
            "test_search",
            "add_text_document",
            "reprocess_documents",
            "reprocess_document",
        },
        "app/api/v1/evaluations.py": {
            "trigger_evaluation",
            "trigger_evaluation_from_runs",
        },
        # Platform Health's on-demand analysis (#101 S9): one model call.
        "app/api/v1/platform_health_analysis.py": {"run_health_analysis"},
        # "Improve with Platform Health" and the Module Analyst's "Investigate now" (#101 S14c).
        "app/api/v1/platform_health_cycles.py": {"request_cycle", "investigate_module"},
    }

    for rel, funcs in expected.items():
        tree = app_sources.tree(BACKEND / rel)
        gated = set()
        for node in app_sources.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            for default in node.args.defaults + list(node.args.kw_defaults):
                if not (
                    isinstance(default, ast.Call)
                    and getattr(default.func, "id", "") == "Depends"
                ):
                    continue
                for arg in default.args:
                    name = getattr(arg, "id", None) or getattr(arg, "attr", None)
                    # ``require_agentic_ai`` also refuses on the AI state, so it
                    # satisfies this; ``*_licensed`` deliberately does not.
                    if name in {"require_ai_enabled", "require_agentic_ai"}:
                        gated.add(node.name)
        missing = funcs - gated
        assert not missing, (
            f"{rel}: these run models or embeddings with no server-side AI "
            f"gate: {sorted(missing)}"
        )


def test_require_ai_enabled_asks_only_the_ai_question():
    """It exists so an execution endpoint can gain the AI check without also
    gaining an Agentic-AI licence requirement it never had."""
    import ast

    from app.api.deps import require_ai_enabled

    # The body, with the docstring dropped: the docstring *mentions* licensing
    # in order to say it is not consulted, and matching prose is how the first
    # version of this assertion failed on its own subject matter.
    tree = ast.parse(textwrap.dedent(inspect.getsource(require_ai_enabled)))
    fn = tree.body[0]
    body = ast.unparse(fn.body[1:] if ast.get_docstring(fn) else fn.body)

    assert "is_ai_enabled" in body
    for licence_check in ("is_agentic_ai_enabled", "is_agentic_ai_licensed"):
        assert licence_check not in body, licence_check


# Platform administration, which FR-021 says must be unaffected — plus the AI
# toggle itself, which lives in ``update_platform_settings_endpoint``: gating it
# would make the mode unexitable.
_EXEMPT_PLATFORM_ADMINISTRATION = {
    "update_platform_settings_endpoint",
    # Settings > Agents and automations > Platform Health (#8254): enable, scope and budget caps; stored
    # configuration reaching no model, and switching it off must stay possible.
    "put_health_settings",
    # Its "Run checks as" user alone, from the CTEM loop setup (dev-docs/platform-health.md 12.3): the same
    # stored setting, validated against the module integrations, reaching no model.
    "put_module_run_as",
    # Platform Health findings triage (#101, S8): dismissing or restoring a stored
    # finding writes its status and an audit row, runs no check and reaches no
    # model; the Health page works with AI switched off, so triage must too.
    "dismiss_health_finding",
    "dismiss_health_findings",
    "restore_health_finding",
    "restore_health_findings",
    # Platform Health companion's pet library (#8261): adding or removing a pet
    # is a stored image reaching no model, managed by administrators.
    "upload_pet",
    "delete_pet",
    # Settings > Organization context (#8987): an administrator saving the
    # versioned organization profile. Stored company data reaching no model
    # when saved; with AI off it stays editable for when agents run again.
    "save_organization_context",
    # The field-scoped save of that profile (#1738): the same hand save, its fields alone.
    "patch_organization_context",
    # Restoring fields of a past version of that profile (#1473): the same hand
    # save, with the same checks; stored data reaching no model.
    "restore_organization_context_version",
    # Settings > Organization (#11533): the platform default of the organization
    # summary cascade; a stored switch reaching no model, editable with AI off.
    "save_organization_context_injection",
    # The OpenCRQ crown-jewel sync of that profile (#8987): an administrator
    # reconciling stored assets with OpenCRQ, no model involved.
    "sync_organization_context_with_opencrq",
    # Assets and exposure from a CSV (#11543): an administrator previewing or
    # saving stored asset rows of that profile; no model involved.
    "import_organization_assets",
    # The draft of that profile from OpenCTI, OpenAEV and OpenCRQ (#8987): the
    # platforms' data mapped to fields, no model involved, nothing saved.
    "draft_organization_context",
    # The import of one profile section from one platform (#11780): a preview of
    # the platform's rows mapped to fields, then the save; no model involved.
    "preview_organization_import",
    "apply_organization_import",
    # The scheduled syncs of that import (#11913): an administrator saving,
    # changing, deleting or running a stored schedule; no model involved.
    "create_organization_sync",
    "update_organization_sync",
    "delete_organization_sync",
    "run_organization_sync",
    # The rows a source wrote (#1276): an administrator purging them or handing them
    # to people, saved as a new version of the profile; no model involved.
    "apply_organization_source_rows",
    # The asset store's merge and split (#1178, S3): an administrator deciding which
    # source records are one asset; a stored decision and a rebuild, no model involved.
    "record_asset_decision",
    "remove_asset_decision",
    # An SBOM imported on an asset (#1178): an administrator's file parsed into the
    # asset's software, no model involved.
    "import_asset_sbom",
    # Software typed, changed, deleted, attached or detached by an administrator (#1495):
    # a stored row and a rebuild, no model involved.
    "create_organization_software",
    "update_organization_software",
    "delete_organization_software",
    "attach_organization_software",
    "detach_organization_software",
    # The asset records, groups, crown-jewel links and change log (#1543): an administrator
    # or an API client writing stored rows one at a time or in a batch; no model involved.
    "create_asset_record",
    "upsert_asset_records",
    "update_asset_record",
    "delete_asset_record",
    "bulk_delete_asset_records",
    "create_asset_group",
    "update_asset_group",
    "delete_asset_group",
    "set_crown_jewel_asset_links",
    "restore_asset_changes",
    # A change proposed from chat for that profile (#9484 S-a2): an
    # administrator accepting or rejecting a stored proposal. Accepted fields are
    # saved as the next version, accepted organizations created with monitoring
    # off; no model involved, so it stays decidable with AI off.
    "decide_organization_context_proposal",
    # The first-login organization setup proposal (#9173, S3): an administrator
    # answering it once (set up now, later, dismiss). A stored flag reaching no
    # model; with AI off the profile form it opens stays editable.
    "answer_setup_prompt",
    # The guided setup's step progress (#1087, CTEM-33): a done or skipped mark per
    # step, stored on the settings row; it reaches no model and enables nothing (the
    # fleet enable step goes through enable_fleet_automations, which is gated).
    "record_setup_step",
    # Settings > Platform "Reset onboarding": an administrator clearing that stored
    # answer and progress so the setup is proposed again; it reaches no model.
    "reset_setup_prompt",
    # Settings > Organization, tracked organizations (#9157): an administrator
    # adding, editing, archiving an organization or saving its profile. Stored
    # company data reaching no model; monitoring stays off.
    "create_organization",
    "update_organization",
    "archive_organization",
    "save_organization_profile",
    # The OpenCTI identity link and PIR sync of a tracked organization (#9157
    # M3): an administrator reconciling stored organizations with OpenCTI, no
    # model involved.
    "link_opencti_identity",
    "sync_opencti_pirs",
    # Settings > ROI baselines (#8990): an administrator setting or resetting
    # the organization's minutes for a unit of work. Stored figures reaching
    # no model; with AI off they stay editable for the value report.
    "put_roi_baseline",
    "delete_roi_baseline",
    # Settings > ROI unit catalog (#9252): an administrator creating, editing
    # or archiving a custom unit of work (each a new version). Stored figures
    # reaching no model; with AI off the catalog stays editable.
    "create_roi_unit",
    "update_roi_unit",
    "archive_roi_unit",
    # ROI declarations (#9285): whoever manages an agent, an assignment or a
    # fleet setting the unit it declares and how it is counted. Stored
    # configuration reaching no model; with AI off it stays editable.
    "put_roi_declaration",
    "delete_roi_declaration",
    # ROI outcome review (#9310): a person marking a run or a work item
    # accepted, modified or rejected. A stored review reaching no model; with
    # AI off past runs stay reviewable for the value report.
    "put_roi_record_outcome",
    # ROI rates by role (#9321): an administrator setting or removing the
    # organization's hourly rate of a role in a currency. Stored figures
    # reaching no model; with AI off they stay editable for the value report.
    "put_roi_rate",
    "delete_roi_rate",
    # ROI settings (#9836 R1): an administrator deciding whether every user
    # also reads the organization-wide totals. A stored, audited flag reaching
    # no model; with AI off the value report keeps its visibility setting.
    "put_roi_settings",
    # ROI from chat backfill (#10544 S3): an administrator recording past chat
    # deliverables from database rows, deterministic and audited, no model;
    # with AI off the value report can still be completed.
    "run_roi_chat_backfill",
    # ROI from chat correction (#10544 S4): a person raising or lowering one
    # chat line from the ROI page. A stored, audited figure reaching no model;
    # with AI off the value report stays correctable.
    "put_roi_chat_correction",
    "update_sso_config",
    # Test Connection for the OIDC Discovery URL: fetches the provider, no model.
    "test_sso_oidc",
    # Settings > Access and security options: platform security switches, like SSO.
    "update_security_options_endpoint",
    # Settings > Access and security > Secret vaults: connecting, editing, testing
    # and removing the vaults secret fields read from. Platform security
    # configuration reaching no model; with AI off, the integrations and
    # settings reading secrets from them still need them.
    "create_secret_vault",
    "update_secret_vault",
    "delete_secret_vault",
    "test_secret_vault",
    "test_secret_vault_draft",
    "test_environment_secret_vault",
    # Settings > Access and security audit: an administrator accepting or restoring
    # a finding. Security review, reaching no model; a platform with AI off is
    # audited all the same.
    "dismiss_security_audit_finding",
    "restore_security_audit_finding",
    "upsert_oauth_default",
    "delete_oauth_default",
    "update_preferences",
    # A person changing their own email with a code sent to the new address:
    # account identity, reaching no model.
    "request_email_change",
    "confirm_email_change",
    # Redeeming a Filigran support code to recharge the agent failure pools:
    # commercial administration, reaching no provider. Arrived from main while
    # this branch was open, and the sweep demanded a classification rather than
    # letting it through — which is the whole point of the inversion. Blocking
    # it would stop an administrator redeeming a code precisely when the
    # platform is idle.
    "redeem_agent_failure_pool_code",
    # The same redemption for every kind of support code, including a reset of
    # the platform-wide usage counters: counters and a ledger row,
    # reaching no provider. A platform with AI off is exactly where an admin
    # may be resetting the counters before switching it back on.
    "redeem_quota_support_code",
    # Globally disabled *integration* providers, not LLM ones — the integration
    # catalogue, which FR-021 and UPGRADING keep working. Gated by mistake on
    # 2026-08-28 (the word "providers"), caught by the UI guard.
    "update_disabled_providers",
    # The per-provider form of the same write: the list PUT above is a
    # read-modify-write whose concurrent callers lose each other's updates, so
    # the catalogue toggles one entry at a time. Same resource, same reasoning
    # — the integration catalogue, not an LLM provider.
    "disable_provider",
    "enable_provider",
    # ``PUT /settings/general-assistant`` is also how an enterprise licence is
    # installed (``useSaveEnterpriseLicense`` posts ``enterprise_license`` to
    # it), and ``LicenseRequired.tsx`` is the page you land on when one is
    # needed. Gating it made a platform with AI off unable to install the
    # licence that would fix it, against FR-031. The General Assistant half is
    # AI config and belongs read-only in the UI (FR-022); splitting the two
    # payloads is the proper fix and is not this branch's to make.
    "update_general_assistant_config",
    # The HITL approval whitelist and its per-platform bulk actions.
    # A policy overlay under Settings → Platform: admin-only, reaching no
    # provider, and creating no AI resource — the same reading that classifies
    # a Custom tool as deterministic tooling. Two of the four *tighten* the
    # policy, and refusing those would mean AI could be re-enabled with a
    # whitelist the administrator was blocked from restricting.
    "add_to_whitelist",
    "remove_from_whitelist",
    "require_approval_for_platform_reads",
    "restore_platform_reads",
    # The dimensions ratings are scored on (Settings > Agents and automations > Quality): rating
    # configuration, reaching no model. A platform with AI off still reads and
    # keeps the ratings it holds, so its administrator may still tidy them.
    "create_feedback_criterion",
    "update_feedback_criterion",
    "delete_feedback_criterion",
    # This platform's registration with the XTM Hub (Settings > Platform):
    # an administrator (or a trial's deployer, for the automatic registration)
    # storing, checking or forgetting the Hub's platform token. It talks to the
    # XTM Hub, never to a model, and a platform with AI off is registered all
    # the same.
    "register_platform",
    "auto_register_platform",
    "check_registration",
    "forget_registration",
    # The platform's pronunciation lexicon (Video Studio): an administrator
    # writing a new version of the platform-wide reference list, or one holding an
    # older version's entries. Database only - nothing is spoken (hearing a term is
    # `speak_pronunciation`, AI-gated) and nothing is written over.
    "save_pronunciation_lexicon",
    "restore_pronunciation_lexicon_version",
    # The Video Studio render retention: an administrator setting how many
    # days renders are kept. Database only - a stored policy, no model call.
    "save_render_retention",
    # Settings > Storage & retention: how many days each category of stored
    # file is kept, and its dry run (counts what the next sweep would delete,
    # writes nothing). Database only - a stored policy, no model call.
    "save_file_retention",
    "preview_file_retention",
    # The departments LLM analytics roll usage up to (Settings >
    # Organization > Departments): the mapping, the directory departments of an
    # HR import and the request to resolve the stored usage again. Database
    # only, no model call; a platform with AI off keeps its analytics.
    "put_departments",
    "import_directory_departments",
    "apply_departments_to_history",
    # Settings > Brand assets (#11458): an administrator setting the GitHub
    # folder of brand packs and syncing it. Logos downloaded and seeded as
    # stored assets, no model call; editable with AI off.
    "update_brand_asset_source",
    "sync_brand_asset_source",
}

# Trace-backend configuration. Langfuse stores records of past AI activity and
# is not a model or embedding provider, so ``test_langfuse_connection`` calls
# Langfuse rather than a model — the same reading that classifies ``traces.py``
# as non-AI.
_EXEMPT_OBSERVABILITY_CONFIG = {
    "update_observability_config",
    "update_langfuse_config",
    "test_langfuse_connection",
}

# --- FR-007: nothing reaches a provider with AI off -----------------------

#: Symbols that mean "this handler talks to a model or embedding provider".
_PROVIDER_HINTS = (
    "PROVIDER_MAP",
    "provider_cls",
    "get_llm_for_user",
    "get_llm(",
    "embed_texts",
    "create_embedding",
    ".embed(",
    "chat_completion",
)

#: Matches a hint without calling out. Each must say why.
_NO_PROVIDER_CALL = {
    "create_llm_provider": (
        "only `body.provider not in PROVIDER_MAP` — a dict membership test on a "
        "name, no outbound request. It is AI *configuration*, so FR-022 makes "
        "the section read-only; that is not FR-007's question."
    ),
}


def _provider_routes_without_a_gate() -> set[str]:
    """Route handlers that reach a provider and are not gated on the AI state."""
    import ast

    found: set[str] = set()
    # ``app/channels`` as well as ``app/api``: the six channel webhooks are
    # entry points too, and walking only ``app/api`` is exactly how nine
    # ungated files reached ``get_llm_for_user`` unnoticed (call-graph
    # analysis, 2026-08-28).
    roots = (BACKEND / "app" / "api", BACKEND / "app" / "channels")
    for path in sorted(p for root in roots for p in app_sources.python_files(root)):
        for node in app_sources.walk(app_sources.tree(path)):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if not [
                d
                for d in node.decorator_list
                if isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr
                in {"post", "put", "patch", "delete", "get", "websocket"}
            ]:
                continue
            source = ast.unparse(node)
            if not any(hint in source for hint in _PROVIDER_HINTS):
                continue
            deps = set()
            for default in node.args.defaults + list(node.args.kw_defaults):
                if (
                    isinstance(default, ast.Call)
                    and getattr(default.func, "id", "") == "Depends"
                ):
                    for arg in default.args:
                        deps.add(getattr(arg, "id", None) or getattr(arg, "attr", None))
            gates = {
                "require_agentic_ai",
                "require_ai_enabled",
                "require_chat_enabled",
                "require_agentic_ai_licensed",
            }
            inline = ("is_ai_enabled", "refuse_ai_disabled", "is_chat_enabled")
            if (
                (deps & gates)
                or any(k in source for k in inline)
                or any(h in source for h in _GATED_HELPERS)
            ):
                continue
            found.add(node.name)
    return found


def test_no_route_reaches_a_provider_with_ai_disabled():
    """FR-007 and SC-001: **zero** model or embedding provider requests.

    The one requirement breach on this branch that no amount of clicking would
    have found, because it is invisible from the interface:
    `settings.py:test_embedding_model` said in its own docstring "Performs a real
    embedding call", and `resolve_models_with_key`, `validate_provider_with_key`
    and `list_provider_models` each call out to the provider. All four were
    reachable with AI off, because `settings.py` was excluded from the mutating
    sweep wholesale (`/speckit-converge`, 2026-08-28).

    `list_provider_models` is a **GET**, and gating it is deliberate: FR-008
    keeps *our stored data* readable, but fetching a provider's model list is an
    outbound request, and FR-007 admits no exception.

    Counted, not inferred — the plan's Principle VI note requires SC-001 to be
    asserted by counting rather than by the absence of an exception. A new
    provider-touching route fails here until it is gated or classified.

    **Limitation, stated because it has already cost twice:** this inspects a
    handler's *own* body. It cannot see a provider call one service-call away,
    which is how ``GET search_agent_memory`` (embeds the query via
    ``persistent_memory.embed_text``) and every channel webhook (via
    ``channels.router.handle_message``) both passed. Reachability needs a call
    graph, not a body scan — see ``test_no_channel_entrypoint_runs_an_agent``
    for the one choke point that is asserted directly.
    """
    unaccounted = _provider_routes_without_a_gate() - set(_NO_PROVIDER_CALL)
    assert not unaccounted, (
        "these reach a model or embedding provider with no AI gate, which FR-007 "
        f"forbids outright: {sorted(unaccounted)}"
    )


def test_no_channel_entrypoint_runs_an_agent_with_ai_disabled():
    """FR-010: "integration-driven calls... no entrypoint may bypass it".

    Slack, Teams, Telegram, Discord, WhatsApp and Google Chat all funnel through
    ``channels.router.handle_message``, which calls ``get_llm_for_user`` and
    ``run_agent_chat``. The whole package had **no** AI gate and none of the
    sweeps walked it, so an outside message ran a model on a platform chosen
    because model access is prohibited — externally reachable, authenticated only
    by the channel's webhook secret.

    Asserted at the choke point rather than per adapter: gating per adapter is
    how six of them were missed at once.
    """
    import inspect

    from app.channels import router

    source = inspect.getsource(router.handle_message)
    # The background gate: AI switched on AND a licensed edition - in
    # its verdict form, which also says when the licence could not be read.
    assert "agentic_refusal(" in source or "agentic_verdict(" in source, (
        "channels.router.handle_message must refuse when AI is disabled — every "
        "external channel arrives here"
    )


def test_the_no_provider_call_exemptions_still_apply():
    """An exemption for a route that now *does* call out would hide a breach."""
    hinted = _provider_routes_without_a_gate() | set(_NO_PROVIDER_CALL)
    stale = set(_NO_PROVIDER_CALL) - hinted
    assert not stale, f"exemptions covering nothing: {sorted(stale)}"
    for name, reason in _NO_PROVIDER_CALL.items():
        assert len(reason) > 30, f"{name}: give a real reason"


# --- Every mutating AI route is gated, or exempt on the record -------------

# Exempt because an operator must keep control of work already running. The
# Blocking a cancel leaves nobody
# able to stop a run in flight, and FR-013 makes cancelling the escape hatch.
_EXEMPT_OPERATOR_CONTROL = {
    "dismiss_run",
    # Closes a request to act on an existing paused run; runs nothing.
    "dismiss_run_action_request",
    "cleanup_stale_runs",
    # Cancelling runs that never started: it only removes AI work,
    # and a quota-exhausted agent's queue must be clearable with AI off.
    "bulk_cancel_runs",
    "cancel_run",
    "cancel_flow_run",
    "cancel_sub_agent_task_endpoint",
    # Disarming a one-off platform event watch before it wakes a task:
    # it only removes future AI work, so AI off must not block it.
    "cancel_platform_event_watch",
    # Stopping a Video Studio recording or render already in flight.
    "cancel_take",
    "cancel_render",
    # The web chat's Stop and "Continue now": they reach a turn already running.
    "cancel_web_chat_turn",
    "skip_web_chat_wait",
    # A lifecycle transition handing a workset to the retention sweep — no
    # model call, and an operator must be able to conclude one.
    "resolve_workset_endpoint",
    # Anything operating on an **existing** run. Its existence already means the
    # gate passed, and FR-013 says work in flight finishes. Gating these left a
    # deterministic flow parked on ``action.ask_user`` (``requires_ai=False``)
    # unanswerable the moment AI was switched off — stuck, not held back.
    "respond_flow_run",
    "debug_flow_run",
    "upload_flow_run_file",
    "respond_to_human_input",
    # The same act over several runs. Every member is an existing
    # paused run, so the reasoning above applies unchanged — and gating it
    # while leaving the single-run form open would make the two surfaces
    # disagree about whether a pause can be answered.
    "batch_respond_to_approvals",
    # Same again, selected by tool rather than by row. It answers
    # existing paused runs and starts none, so nothing here turns AI back on.
    "approve_by_tool",
    # And again, selected by coverage rather than by tool (T213). It
    # answers existing paused runs against approvals the user already granted
    # and grants nothing itself — the weakest of the three, on the same runs.
    "release_covered_runs",
    # The Approve / Reject button of a phone notification: the same answer as
    # `respond_to_human_input`, through the same function, on an existing run.
    "press_notification_button",
    # Revoking an MCP key and disconnecting a provider only ever *reduce*
    # access. Blocking them has no upside and a real downside: a leaked key
    # would be unrevokable while AI is off. Same reasoning that made gating
    # ``deactivate_flow`` wrong (/code-review, 2026-08-28).
    "revoke_mcp_key",
    # An external callback resuming an episode already dispatched. Refusing
    # it abandons work in flight rather than preventing any, so the episode
    # hangs until the deadline sweep (found by /code-review, 2026-08-28).
    "resume_evaluation_episode",
    # The emergency stop. It is the operator control: it cancels every run in
    # scope and, for the platform scope, is what switches AI OFF — so gating
    # it on AI being on would make the second press fail. Its resume only
    # re-enables assignments the stop paused and clears the AI override back
    # to "follow the deployment mode"; the model call, if any, is made later
    # by routes that are gated themselves.
    "emergency_stop",
    "emergency_stop_resume",
    # The same control scoped to one fleet (#9769 S2a): Pause all switches off
    # the fleet's automations (it only removes future AI work, so AI off must
    # not block it) and Resume switches back on exactly the ones that pause
    # stamped `fleet_pause`, as emergency_stop_resume does for the stop. No
    # model call; each run they later start is gated on its own.
    "pause_fleet_automations",
    "resume_fleet_automations",
    # Back to not set up (#11060): switches the fleet's automations off and
    # re-stamps them as never set up. It only removes future AI work, as Pause
    # all does, so AI off must not block it. No model call.
    "unset_fleet_automations",
    # Reset a built-in fleet to its factory state (#1508): automations off,
    # bindings and customer settings cleared. It only removes future AI work and
    # makes no model call, so AI off must not block it.
    "reset_fleet_to_factory",
    # The same two on several fleets at once, plus the admins' every-fleet Pause
    # switch and a bulk autonomy level (#9769 S3): no model call, a level change
    # only decides which future tool calls wait for a person.
    "bulk_fleet_action",
    # Annotating a stop that already happened with its reason: an audit write,
    # no model call, and it must work while AI is off (the stop switched it off).
    "emergency_stop_reason",
    # The token path and its management: the stop itself without a session
    # (a runbook, a monitoring hook), and minting / revoking the credential
    # that allows it. None calls a model; all must work while AI is off.
    "emergency_stop_by_token",
    "emergency_stop_token_generate",
    "emergency_stop_token_revoke",
    # An agent account's approval queue: the owner posts or drops a
    # reply the agent ALREADY drafted. No model call, and refusing it would
    # strand drafts the owner can neither send nor discard while AI is off.
    "approve_agent_account_reply",
    "reject_agent_account_reply",
}

# The lifecycle of a flow, which may be entirely deterministic. FR-014 promises
# such flows keep running, and `require_agentic_ai_licensed`'s own docstring says
# "the endpoints that create, edit, activate, cancel and run them cannot sit
# behind" the AI gate. Whether a *particular* flow may run is answered per flow,
# by `run_flow`. Both were AI-gated until /code-review caught it on 2026-08-28;
# `deactivate_flow` was the worse of the two, since deactivating only ever
# *reduces* AI work and blocking it left an operator unable to stop an AI-bearing
# flow from writing a skipped run every tick.
_EXEMPT_DETERMINISTIC_FLOW_CONTROL = {
    "activate_flow",
    "deactivate_flow",
}

# POSTs that only read, or grant a read.
_EXEMPT_READ_IN_POST = {
    "export_knowledge_base",
    # The inventory lookup of a platform's pivot (#1178): reads the canonical
    # assets the given identifiers name - no model, nothing written. A POST
    # only because up to 200 identifiers do not fit a query string.
    "lookup_canonical_assets",
    # The secret picker's check of one vault reference: it reads the
    # secret on the server and answers whether it can be read, nothing written.
    "check_reference",
    # The Supabase database access probe: lists the projects, roles, hosts,
    # databases and functions a database role is chosen from, through the
    # integration's own credentials - no model, nothing written. A POST only
    # because a password being tried travels in the body.
    "probe_supabase_database_access",
    # A placebo comparison's estimate: the runs, refusals and token
    # estimate a trigger would make, from the stored configuration and
    # metrics - placebos drawn in characters, no model called, nothing
    # written. A POST only because the model selection travels in the body,
    # as the trigger's does.
    "estimate_placebo_comparison",
    # The bundle review of many answer groups at once: stored answers and
    # their expected bundles, re-read by the deterministic metric suite - no
    # model, nothing written. A POST only because up to 200 groups of result
    # ids do not fit a query string (the single review is a GET).
    "get_bundle_reviews",
    # The Video Studio media token: a short-lived signature over
    # files the caller can already read, so the player can stream them.
    "media_token",
    # A built investigation pack's preview: the plan the caller would
    # run, computed from the pack and the caller's integrations - no model,
    # no source queried, nothing written. A POST only because the pack being
    # edited travels in the body.
    "preview_investigation_pack",
    # Rendering an existing deck to PDF / PPTX is a read of stored AI
    # output through the platform's own headless browser: no model, no
    # embedding. FR-008 wants a deck that was generated before AI went off to
    # stay downloadable.
    "export_presentation_endpoint",
    # A report's PDF is the same read through the same browser: the
    # stored report is printed and streamed back, nothing is written.
    "export_report_endpoint",
    # The layout check is the same read through the same browser: the
    # stored deck is rendered and its slides are measured, and the findings are
    # returned - no model, no embedding, nothing written to the deck. Whoever
    # may edit the deck may measure it, AI on or off; the only thing the
    # findings lead to is a chat turn, which carries its own gate.
    "layout_check_presentation_endpoint",
    # Combined server-side run search: a POST only because the filter set
    # (params, metrics, scores, tags, dates) does not fit a query string. It
    # reads stored runs and reaches no provider, so it answers 200 — gating it
    # would make the run list unsearchable with AI off, which is the one thing
    # the mode exists to preserve.
    "search_runs",
    # The admin "break the glass" grant for another user's conversation files:
    # the audit entry plus the Redis lease that the file listing requires. With
    # it gated, an admin could list conversations (deliberately ungated for
    # exactly this reason) but never obtain the grant to read them — against
    # FR-008's "every AI read answers 200".
    "acknowledge_conversation_file_access",
    # The sharing preview answers "whose stored credentials would
    # this change lend to whom": database reads only, no model, nothing
    # written. The change it previews carries its own gate.
    "post_exposure_preview",
    # The model announcement preview renders the email the users would
    # get for the models the admin chose: database reads only, no model call,
    # nothing written or sent. The test email and the send carry their own gate.
    "preview_model_announcement",
    # An agent account's rule simulator: the deterministic rule engine
    # on a sample message, the sender's XTM One groups read from the database -
    # nothing written or sent, no model call.
    "simulate_agent_account",
    # The agent detail's read-only chat panel: the chat panel restores
    # a conversation with a POST, which here only reads one of the agent's
    # conversations and never starts one. FR-008 keeps reading them open.
    "restore_agent_chat_panel_session",
    # The web chat panel's session open: restoring a conversation only reads
    # it, so FR-008 keeps it open; starting one calls ``require_chat_enabled``
    # in the handler.
    "open_web_chat_session",
    # A custom dashboard's live platform widgets (Dashboard Studio): the
    # stored (or previewed) widget's aggregate query sent to OpenCTI, OpenAEV
    # or OpenCRQ with the viewer's own access - no model, nothing written. A
    # dashboard kept while AI is off still draws its figures.
    "read_platform_widget_data",
    "read_data_source",
    # A source editor's question to OpenCTI / OpenAEV (types, filter keys, values), no model.
    "read_platform_schema",
    "preview_platform_widget",
    # The same reads for the export's static page (render token only), and the
    # export itself: a headless browser drawing the page, no model call.
    "read_widget_data_for_render",
    "read_data_source_for_render",
    "export_dashboard_render",
    # A dashboard's published link: a capability URL and a snapshot of the
    # same reads with the publisher's access - no model, no question asked.
    "publish_dashboard_link",
    "update_dashboard_public_link",
    "refresh_dashboard_public_link",
    "unpublish_dashboard_link",
    # An administrator turning any published link off from the review list: it only stops an address answering.
    "turn_off_dashboard_public_link",
    # Extractor preview is a POST only because the extractor spec and
    # the pasted sample do not fit a query string. It runs the same pure
    # ``apply_extractor`` fold the run path uses — no model, no embedding — and
    # writes nothing: no run, no result, no metric, no audit row, no column.
    # Gating it would stop an author inspecting how a stored result maps onto
    # the shape scoring reads, which is a read of their own data.
    "preview_extractor",
    # The same preview on the creation screen, before the evaluation
    # exists: the family, the extractor and a pasted sample, the same pure fold,
    # no model, no database read, nothing written.
    "preview_extractor_for_family",
    # The CTEM command center's "Force sync": clears the caller's cached
    # posture and rebuilds it from OpenCTI / OpenAEV / OpenCRQ aggregates over
    # HTTP - the same deterministic build GET /ctem-metrics/posture runs on a
    # cache miss. No model, no run, no quota; only the caller's own cache is
    # written. Community Edition with AI off refreshes its posture (#9194).
    "refresh_ctem_posture",
}

# Per-user housekeeping, left open by an explicit product decision
# (2026-08-27). Scenario 4.2 says "every create, change, delete and execution
# returns 403" without qualification, but applied literally a user could not
# archive their own conversation while AI is off, and an operator could not
# clear run history. Deletes of *shared AI content* — agents, prompts, KBs,
# skills, datasets, evaluations, intents, template containers, sync sources —
# ARE refused, which is where FR-025's "no data destroyed" actually bites.
#
# So the boundary is ownership, not the HTTP verb: your own data stays yours to
# remove, the platform's AI content freezes.
_EXEMPT_OWN_DATA_HOUSEKEEPING = {
    # Your own custom dashboards: rearranging or deleting one an agent built,
    # sharing it, or bringing back an earlier version. A validated spec edit
    # or a grant, no model call.
    "update_custom_dashboard",
    "delete_custom_dashboard",
    "share_custom_dashboard",
    "restore_custom_dashboard_version",
    # Making a dashboard by hand: empty, from a file, or a private copy of one you read; and filing yours
    # under tags. Validated definitions, no model call.
    "create_custom_dashboard",
    "import_custom_dashboard",
    "duplicate_custom_dashboard",
    "set_custom_dashboard_tags",
    # Your library of dashboard widget packs (Dashboard Studio): importing a
    # file, saving a dashboard as a pack, sharing, duplicating, deleting one,
    # and inserting its widgets into a dashboard you edit. Validated widget
    # data, no model call; the dashboards they fill work with AI off. Built-in
    # packs are never changed (the routes refuse it).
    "import_widget_pack",
    "save_dashboard_as_pack",
    "duplicate_widget_pack",
    "update_widget_pack",
    "share_widget_pack",
    "delete_widget_pack",
    "insert_widget_pack",
    # Publishing a dashboard you read to OpenCTI or OpenAEV with your own
    # account there: the export file sent through your integration, no model call.
    "publish_custom_dashboard",
    # Sharing it on the XTM Hub with your own Hub account: its OpenCTI export
    # as an inactive library resource, no model call.
    "share_custom_dashboard_on_hub",
    # Writing a dashboard's definition by hand (the source editor), validated
    # like any write, no model call.
    "replace_custom_dashboard_definition",
    # Uploading an image or an SVG a canvas of your dashboard draws: a checked
    # and cleaned file, no model call.
    "upload_custom_dashboard_asset",
    # Your own notification feed's read state.
    "mark_notifications_read",
    "dismiss_notification",
    # Your own linked phones (the mobile companion): link, rename, mute,
    # test, unlink. Unlinking only reduces what reaches a phone.
    "link_phone",
    "update_phone",
    "send_test_notification",
    "unlink_phone",
    # The PIN lock of your own phone: it hides data, never runs anything,
    # and must keep working with AI off (a locked phone stays locked).
    "mark_companion_session",
    "unlock_app",
    "lock_app",
    "set_app_pin",
    "update_app_lock",
    # Platform Health consent (#8254): a user's opt-out and the managers'
    # improvement opt-in of an agent or a flow. Taking consent back never waits
    # for AI to be on, and neither switch reaches a model.
    "set_my_opt_out",
    "set_agent_opt_in",
    "set_flow_opt_in",
    # Platform Health companion (#8261): a user's own switch and pet; it reads
    # what they already see and never reaches a model.
    "put_companion",
    "archive_conversation",
    "archive_all_conversations",
    "delete_conversation_share",
    "delete_message_feedback",
    "bulk_delete_sub_agent_tasks",
    "delete_sub_agent_task_endpoint",
    "purge_all_runs",
    "bulk_delete_runs",
    "delete_run",
    "purge_assignment_runs",
    "delete_all_runs",
    "remove_entry_file",
    # The Files repository delete: the same rows `remove_entry_file`
    # and the conversation / run deletes already cover, reached from one page.
    # `can_delete_file` admits only the file's owner, the owner of the
    # conversation it lives in, or whoever `require_run_access` admits on the
    # run that produced it — your own uploads and the outputs your agent made
    # for you. No model call, and a template asset is refused outright.
    "delete_file",
    # Clearing an agent's own chat history.
    "clear_agent_chats",
    # Own-conversation metadata: renaming it, your own message feedback, and
    # sharing it. Same ownership boundary as the deletes — your data stays
    # yours, the platform's AI content freezes.
    "update_conversation",
    # The embedded chat's rename / file of its own conversation: a thin
    # delegate of `update_conversation`, owner-only, no model call.
    "update_platform_chat_session",
    # The web chat's HTTP transport, thin delegates of `update_conversation`,
    # `archive_conversation` and `archive_all_conversations` (owner-only).
    "update_web_chat_session",
    "archive_web_chat_session",
    "archive_all_web_chat_sessions",
    "upsert_message_feedback",
    # The same rating, of any agent work the caller may read: a chat
    # message (the chat's own write path), a run, a background task, a
    # delegation. It records the user's opinion of work that already ran - no
    # model call - and retracting it is the same kind of edit.
    "upsert_target_annotation",
    "delete_target_annotation",
    "create_conversation_share",
    # A user's own approval posture: a boolean saying whether their
    # tool calls pause. It invokes no model and starts no work — and with AI
    # off there are no runs for it to affect, so blocking it protects nothing
    # while leaving a user unable to tighten their own gate.
    "set_my_approval_mode",
    # Binding an own conversation to a deck the user can already see
    # (Presentation Studio) is the same kind of metadata edit as a
    # rename: no model call. The studio turn it sets up is gated where it runs
    # (`send_message`, the WebSocket), so with AI off the chat card's "Open in
    # studio" still lands on a read-only studio over a deck the user owns,
    # instead of a button that refuses on click (the gate the UI guard in
    # `test_ai_disabled_ui_gates.py` would otherwise demand of `PresentationCard`).
    "bind_conversation_presentation",
    # The Video Studio twin: binding an own conversation to a video
    # the user can open. Same reasoning, the studio turn is gated where it runs.
    "bind_conversation_video",
    # The Dashboard Studio twin: binding an own conversation to a dashboard the
    # user reads as themselves. Same reasoning.
    "bind_conversation_dashboard",
    # The fleet page twin (Modify with AI): binding an own conversation to a
    # fleet the user reads as themselves. Same reasoning, the turn is gated
    # where it runs.
    "bind_conversation_fleet",
    # Stopping the continuation of your own conversation in Slack or Teams:
    # it deletes your own binding row, no model call and no message
    # sent. Starting one stays gated (`require_chat_enabled`); refusing the stop
    # would leave a thread bound to a conversation the user wants out of.
    "delete_channel_binding",
    # Dismissing the interrupted turn shown on your own conversation:
    # owner-only, it forgets a Redis record and calls no model. Resuming the
    # turn stays gated where it runs (`send_message`, the WebSocket); refusing
    # the dismiss would pin a banner the user cannot act on while AI is off.
    "dismiss_interrupted_turn",
    # Your own memory and profile, from Settings. Same ownership boundary:
    # your data stays yours to remove, the platform's AI content freezes.
    "purge_my_memory",
    "purge_my_agent_memory",
    "purge_my_user_profile",
    # Undoing a Memory Steward change to your own memory with an agent (#8258):
    # own lane only, no model call. The agent's own lane stays gated.
    "undo_my_memory_consolidation",
    # Withdrawing one of your own standing "Yes, always" approvals (#173).
    # Own data, and safety-increasing: refusing it would strand an approval the
    # user can no longer take back.
    "revoke_my_tool_approval",
    # Granting one from the tools list. The same row
    # ``revoke_my_tool_approval`` removes and ``set_my_approval_mode`` covers
    # wholesale: own data, no model call, and with AI off there are no runs for
    # it to affect. Refusing it would leave a user able to tighten their own
    # gate but not to loosen it, from the same screen.
    "grant_my_tool_approval",
    # A presentation is a deliverable the user's agent produced FOR the
    # user — the same side of the ownership boundary as a conversation, not
    # platform AI content. The manual controls (`PATCH`: retitle, reorder,
    # notes, theme, sharing), the delete and the copy make no model call;
    # generating or rewriting slides only ever happens through the agent tool,
    # which is gated where the agent runs.
    "update_presentation",
    "delete_presentation",
    "duplicate_presentation_endpoint",
    # Collaboration on that same deliverable: review comments are
    # human-to-human text about the user's own deck, presence is a Redis
    # heartbeat that says "I have this open", and restoring a past revision
    # re-applies a spec that was ALREADY validated when it was saved. None of
    # them reaches a model — nothing here can generate or rewrite a slide.
    "create_presentation_comment_endpoint",
    "update_presentation_comment_endpoint",
    "delete_presentation_comment_endpoint",
    "presentation_presence_heartbeat",
    "presentation_presence_leave",
    # Hand the clicker (#10552 slice B): one person drives the slides another
    # presents, relayed over Redis. A human moving a deck, no model call.
    "hand_presentation_clicker",
    "release_presentation_clicker",
    "presentation_clicker_command",
    "restore_presentation_version_endpoint",
    # The editor's per-slide layout picker: the route rebuilds ONE slide
    # of the user's own deck as another layout, deterministically — a field
    # mapping through the target's Pydantic model, no prompt and no provider —
    # and writes nothing; the save is the ordinary `PATCH` above. It is the same
    # manual control as a retitle or a reorder, reaching the same side of the
    # ownership boundary, so it stays open when AI is off for the same reason.
    "switch_slide_layout_endpoint",
    # The deck's public link is sharing like the `PATCH` above: who
    # may open the deliverable, decided by its managers, with no model call.
    # Taking a link down must work whatever the AI state, and turning one on
    # shows slides that already exist.
    "enable_presentation_public_link",
    "disable_presentation_public_link",
    # What the link offers besides the slides: its PDF, rendered from
    # the public page by the headless browser - no model either.
    "update_presentation_public_link",
    # A deck's live poll (#10157): the presenter's buttons (open, lock,
    # reveal, reset) and the audience's anonymous vote write rows of the
    # poll tables, counted in SQL - no model call, and a talk in progress
    # must keep working whatever the AI state.
    "control_presentation_poll",
    "vote_public_poll",
    # Moving a deck between installs: the import re-creates a deck the
    # user already owns elsewhere from a package this platform's export wrote,
    # through the same validator a manual edit uses — no model, no embedding.
    # Refusing it would strand a deck generated before AI went off on the
    # install it was born on, while `duplicate` (the in-place copy) stays open.
    "import_presentation_endpoint",
    # An agent account is owner-only: an agent operating the caller's
    # OWN messaging account through the caller's own integration. Its CRUD and
    # its subscription sync make no model call; the agent turn is reached only
    # in `agent_accounts/pipeline.py`, which asks the background gate before it
    # starts, so with AI off a message is recorded as `ai_unavailable` and
    # nothing is answered. Refusing the delete or the edit would leave a user
    # unable to stop or narrow what an agent does in their name.
    "create_agent_account",
    "update_agent_account",
    "delete_agent_account",
    "sync_agent_account",
    # A video is a deliverable like a presentation: retitling, sharing
    # and deleting it, and editing its timeline by hand (the spec goes through
    # the same validator the agent tool uses), make no model call. The render
    # stays gated - its voice-over calls a speech model - and so do the agent
    # tools that write a video.
    "update_project",
    "put_project_spec",
    "patch_project_spec",
    "delete_project",
    # Making one by hand in the editor, or copying one, goes through
    # the same validator; the AI shot that starts a video is its own gated route.
    "create_project",
    "duplicate_project",
    # The video's public link, the deck's twin above: who may watch
    # the deliverable, decided by its managers, no model call - taking a link
    # down must work whatever the AI state, and turning one on plays a render
    # that already exists.
    "enable_public_link",
    "update_public_link",
    "disable_public_link",
    # Keeping a render out of the retention, making one the video's current
    # render, deleting one: bookkeeping over MP4s already made, no
    # model. Rendering a new one stays gated.
    "update_render",
    "delete_render",
    # Deleting many of a video's renders at once: the same bookkeeping
    # over MP4s already made, and freeing their storage must work whatever the AI state.
    "cleanup_renders",
    # A picture, a sound or a video file uploaded into the editor: stored as the
    # caller's own file, no model.
    "upload_file",
    # The caller's own pictures drawn inside one of their pictures:
    # Pillow, flat or in perspective, stored as a new file of the caller's - no
    # model, no browser. The AI shot the result may start stays gated.
    "compose_picture",
    # The caller's own recordings and AI clips, removed by their owner, renamed
    # or kept out of the media retention, or removed in bulk from the
    # media library.
    "delete_take",
    "delete_clip",
    "update_take",
    "update_clip",
    "bulk_delete_assets",
    # The caller's own AI music tracks, the same way: renamed or removed
    # by their owner, no model. Composing one stays gated.
    "delete_music",
    "update_music",
    # The caller's browser profiles: a saved sign-in for the backend browser.
    # Writing one encrypts credentials, signing in or out drives the headless
    # browser on a login page, checking the session opens its start URL in it,
    # testing the sign-in dry-runs that same drive without saving,
    # and answering its MFA prompt types a code - no model anywhere.
    # Refusing them with AI off would leave a user unable to remove
    # credentials or end a saved session.
    "create_profile",
    "update_profile",
    "delete_profile",
    "sign_in_profile",
    "test_sign_in_profile",
    "sign_out_profile",
    "check_profile_session",
    "submit_mfa",
}

# Deterministic tooling: a Custom tool is a sandboxed script, not an AI
# resource. Nothing here reaches a model or embedding provider — `lint` is
# `ast.parse` / `node --check` and never executes, and `test` runs the script
# itself. Decided 2026-08-28 after Fred asked why tools were locked when we can
# already run their tests.
#
# Four facts settled it: tools are not among the seven things provisioning seeds
# (agents, assignments, integrations, intent bindings, knowledge bases, skills,
# templates); they are absent from the spec's own built-in-content list in
# US1/AC1; the spec defines AI as "needs a model or embedding provider, directly
# or transitively"; and `action.script` (`requires_ai=False`) runs a Custom tool
# from a *deterministic* flow, so gating authoring left a non-AI flow
# unrepairable with AI off — against FR-014.
#
# `create` and `lint` keep their **licence** gate (`require_agentic_ai_licensed`)
# — dropping only the AI-state refusal, since widening licence behaviour is a
# commercial decision, not this feature's. `delete_custom_tool` deliberately
# stays refused: FR-025 says nothing is destroyed while AI is off and re-enabling
# needs no repair, which is the same boundary that refuses prompt, skill and
# knowledge-base deletes. So: author and edit yes, destroy no.
# Gated inside the handler rather than by a dependency, because the refusal
# must be indistinguishable from every other refusal on that route: a partner
# learning "AI is disabled here" from a 403 learns more than a 404 tells it.
_EXEMPT_GATED_IN_HANDLER = {
    # The A2A callback receiver. Checks ``is_chat_enabled`` and answers with
    # the same uniform not-found it gives an unknown delegation.
    "receive_callback",
}

# Inbound provider deliveries sharing a module with gated routes, so the
# module cannot be listed in ``_NON_AI_MODULES`` the way ``webhooks.py`` is.
# Same reasoning: the dispatch they reach is gated downstream.
_EXEMPT_INBOUND_DELIVERY = {
    # An agent account's push endpoint: every trusted entry reaches
    # `agent_accounts/pipeline.process_event`, which asks the background gate
    # before any agent turn.
    "agent_account_notifications",
}

_EXEMPT_DETERMINISTIC_TOOLING = {
    # Running one.
    "test_custom_tool",
    "upload_tool_test_file",
    "cleanup_tool_test_files",
    # Authoring one. `create` and `lint` are licence-gated, not AI-gated.
    "create_custom_tool",
    # Deleting one. Kept gated at first on FR-025 ("no data destroyed while AI
    # is disabled") — a misreading: FR-025 and US4/AC3 say the *mode* must not
    # destroy anything and that re-enabling needs no repair, not that an admin
    # may not delete. FR-009 covers deleting *AI resources*, and T077 settled
    # that a Custom tool is not one. "Author and edit yes, destroy no" was an
    # invented distinction; corrected 2026-08-28 when Fred asked why a tool
    # could not be deleted. A *template* stays refused, and correctly: it is
    # built-in AI content (`seed_builtin_templates`).
    "delete_custom_tool",
    "update_custom_tool",
    "import_custom_tool",
    "lint_custom_tool_code",
    # Its declared tests: the same sandboxed script as `test_custom_tool`, run
    # against data cases and stdlib test code; no model is called.
    "run_custom_tool_tests",
    # Detaching a synced tool only clears its link to its repository source.
    "detach_custom_tool",
    # Repository sources of custom tools (`custom_tool_sources.py`): reading a
    # repository, testing the tools it holds in their sandbox and writing them
    # as custom tools calls no model; create and sync are licence-gated, as
    # `create_custom_tool` is.
    "create_source",
    "update_source",
    "delete_source",
    "sync_source_now",
    # Workspaces. A workspace is a NAMED CONTAINER, not AI content:
    # creating, renaming, archiving and deleting one invoke no model. They are
    # licence-gated (`require_agentic_ai_licensed`) and deliberately NOT
    # AI-gated, for the same reason deterministic flows are not — FR-014
    # promises the configuration of the agentic surface keeps working with AI
    # switched off, and being unable to tidy your own containers while AI is
    # off would be a worse failure than the one the gate prevents.
    #
    # Note what this does NOT cover: the workspace SCOPE resolving inside a
    # chat turn or a flow run is on the agentic path and inherits its gates.
    # Only the container's own CRUD is exempt.
    "create_workspace_endpoint",
    "update_workspace",
    "delete_workspace",
    "set_default_workspace",
    # Binding a knowledge base to a workspace is the same kind of act: it
    # names a corpus the workspace works with and moves nothing.
    "set_workspace_knowledge_bases",
    # Linking a skill and creating the (empty) Sources knowledge base are the
    # same container acts; adding a source's CONTENT queues embedding
    # work and stays AI-gated (``add_workspace_source_from_attachment`` and the
    # knowledge-base document routes).
    "set_workspace_skills",
    "ensure_workspace_sources",
    # The two platform numbers behind it (the sources' context share, the
    # conversations' idle delay): an administrator's setting that calls no
    # model; the indexing it paces is gated on its own.
    "update_workspace_knowledge_config",
    # The same container CRUD reached from the chat embedded in a connected
    # platform: each delegates to the handler above, under the same licence
    # gate and the same rules.
    "create_platform_chat_workspace",
    "update_platform_chat_workspace",
    "delete_platform_chat_workspace",
    # Fleets: a fleet is the same kind of named container, a roster
    # of agents bound to a workspace. Creating it, changing its members, its
    # hierarchy or its exposure invokes no model and edits no agent; licence-
    # gated like workspaces. Generating a fleet from a prompt (a later phase)
    # calls a model and is AI-gated.
    "create_fleet_endpoint",
    "update_fleet",
    "delete_fleet",
    "add_fleet_member",
    "update_fleet_member",
    "remove_fleet_member",
    "move_fleet_member",
    "set_fleet_exposure",
    # Several members' settings (context inclusion, collaboration
    # instruction) in one transaction: the same edit as update_fleet_member,
    # no model call, licence-gated like it.
    "bulk_update_fleet_members",
    # Company presets of those member settings (#9162 R4): an administrator
    # stores a named set of values; no model call, licence-gated like them.
    "create_fleet_member_preset",
    "update_fleet_member_preset",
    "delete_fleet_member_preset",
    # A built-in fleet's shipped documentation brought back (#9768 S2): a
    # copy of the stored seed, no model call, licence-gated like the fleet.
    # The AI regenerate is AI-gated on its own.
    "restore_shipped_fleet_documentation_endpoint",
    # A kept version of that documentation brought back (#9768 S2, UX B1):
    # a copy of a stored version, no model call, licence-gated like it.
    "restore_fleet_documentation_version_endpoint",
    # A configuration suggestion of that documentation dismissed (#10246): a
    # stored id, no model call, licence-gated like the fleet.
    "dismiss_fleet_documentation_finding_endpoint",
    # Several suggestions dismissed or restored at once (#10536): stored ids,
    # who, when and an optional reason, no model call, licence-gated like the
    # fleet.
    "dismiss_fleet_documentation_findings_endpoint",
    "restore_fleet_documentation_findings_endpoint",
    # A good-practice suggestion applied by a fleet manager: one Consult
    # handover added to a member, versioned and audited, no model call,
    # licence-gated like the fleet.
    "apply_fleet_practice_suggestion_endpoint",
    # A manual prerequisite of that documentation confirmed or undone by a
    # fleet manager (#10469 S2): who and when stored, no model call,
    # licence-gated like the fleet.
    "confirm_fleet_prerequisite_endpoint",
    # A connected integration bound to or unbound from a member's capability
    # slot (#914 slice 2): the member agent's integration bindings written,
    # no model call, licence-gated like the fleet.
    "bind_fleet_member_slot",
    "unbind_fleet_member_slot",
    # The fleet choice of a capability written on every member slot of that
    # type, and one member's exact override or reset (#1201): the same
    # integration bindings as a one-click bind, no model call, licence-gated.
    "set_fleet_capability",
    "set_fleet_member_slot",
    # A custom fleet's approval gate catalog stored (#10014): configuration
    # computed in code, no model call, licence-gated like the fleet. Adding
    # gates only adds pauses, so AI off must not block it either.
    "update_fleet_approval_gates_endpoint",
    # #10153: one click of the fleet autonomy cursor, the level and the
    # per-action pins (the same control as PATCH autonomy_level), no model
    # call. Lowering autonomy is a safety control: AI off must not block it.
    "apply_fleet_autonomy_endpoint",
    # #10168: where the fleet's instructions put it (display and one prompt
    # line, never a gate), no model call; lowering it stays open with AI off.
    "set_fleet_instructions_position_endpoint",
}

# INVERTED on purpose. This used to be a hand-written list of "AI
# modules", and that is precisely why three convergence passes each missed a
# different surface: pass 1 missed 125 routes, pass 2 missed ``a2a_server.py``
# and the worker, pass 3 missed 45 unswept routers including one named ``ai``.
# A list of what to check cannot catch what nobody thought to add to it.
#
# So the default is now "every module is in scope", and a module is only out of
# scope if it is named here with a reason. A new module fails the test until
# someone classifies it — the same posture ``NodeKind.requires_ai`` takes for
# node types, and for the same reason: the two ways of being wrong are not
# symmetric.
_NON_AI_MODULES: dict[str, str] = {
    # FR-021: administration unrelated to AI must be unaffected.
    "auth.py": "authentication (FR-021)",
    "auth_sso.py": "single sign-on (FR-021)",
    # The XTM One Desktop browser sign-in handoff: a runner opens a
    # pending login, the OS browser's SSO session completes it, the app
    # exchanges it for a token. Authentication end to end, no model call.
    "desktop_login.py": "XTM One Desktop browser sign-in handoff (FR-021)",
    "users.py": "users (FR-021)",
    "groups.py": "groups and roles (FR-021)",
    "scim.py": "SCIM provisioning of users and groups, and its tokens (FR-021)",
    "invitations.py": "invitations (FR-021)",
    "api_keys.py": "API tokens (FR-021)",
    "platform.py": "platform registration, branding, health (FR-021)",
    # Infrastructure that reaches no model or embedding provider.
    "runners.py": "sandbox runner transport; no model or embedding call",
    # Relays carry integration / MCP / flow HTTP traffic, which keeps
    # working with AI off like the integrations themselves.
    "relays.py": "on-premises egress relays; no model or embedding call",
    "integrations.py": "integration syncs keep working (FR-021, UPGRADING)",
    # Same promise for OpenAPI integrations: their operations also serve
    # deterministic flows (the integration node), and managing the spec —
    # parse, select, refresh, replace — makes no model call.
    "integration_openapi.py": "OpenAPI integration spec management; no model call (FR-021)",
    "variables.py": "custom variables are not AI",
    "integration_notifications.py": "notification delivery, no model call",
    "integration_github_webhooks.py": "inbound VCS events; dispatch is gated downstream",
    "hooks.py": "outbound webhook delivery, no model call",
    # FR-014: editing deterministic automation must keep working. Whether a
    # given flow may *run* is answered per flow at dispatch, not here.
    "flows.py": "flow editing must keep working (FR-014)",
    "flow_event_types.py": "deterministic flow infrastructure (FR-014)",
    # FR-029 targets the MCP *endpoints*, which are gated. These configure them.
    "mcp_servers.py": "MCP server configuration, not a model-consumption surface",
    "mcp_oauth.py": "MCP OAuth dance, not a model-consumption surface",
    # The human-in-the-loop queue is a work list, not a model surface.
    "work_items.py": "HITL work queue; no model call",
    # Agent coordination store: an API write puts a value in it, which is not a
    # model call. What agents then do with it is gated where they run.
    "shared_state.py": "agent coordination store; no model call",
    # Observability records *of* AI output. Attaching a score or deleting a
    # record makes no model call — and FR-008 wants this surface readable.
    "traces.py": "AI observability records; no model call",
    # Agentic AI adoption analytics: reads the platform's own daily metrics;
    # nothing here reaches a model or embedding provider.
    "ai_adoption.py": "adoption analytics; no model call",
    # The waiting arcade: game sessions, scores and the leaderboard.
    # A game played in the browser; nothing here reaches a model or embedding
    # provider, and it has its own kill switch (ARCADE_ENABLED).
    "arcade.py": "the waiting arcade's game sessions and leaderboard; no model call",
    # Browser-sent Content-Security-Policy violation reports: counted
    # and logged, never stored, no model call; security telemetry keeps
    # working with AI off.
    "csp_reports.py": "CSP violation reports; no model call",
    # A registered platform's access notice: one email from a fixed template,
    # no model call. A platform with AI off still adds people to it.
    "platform_access_notices.py": "access notice emails from a fixed template; no model call",
    # Cross-platform keys, identity and the user-token exchange: signatures
    # over a registered platform's verified user, no model call.
    "xtm_auth.py": "cross-platform keys and user-token exchange; no model call",
    # Inbound delivery only. Every dispatch it can reach is gated downstream:
    # assignments in ``assignment_engine.trigger_assignment`` /
    # ``trigger_or_queue_assignment``, flows in
    # ``flow_triggers.hold_back_if_ai_required``. Gating here as well would
    # answer a partner before the dedup slot is settled.
    "webhooks.py": "inbound delivery; dispatch gated in the services it calls",
    # CTEM Cases (#11545): a person changes the owner, due date or state of a
    # case grouped from OpenCTI / OpenAEV data. Rules and writes, no model
    # call, like the posture the cases come from.
    "ctem_cases.py": "CTEM cases: deterministic grouping and state changes; no model call",
    # CTEM signals (#826): a person records a change in exposure; an idempotent
    # row write, no model call, like the cases it feeds.
    "ctem_signals.py": "CTEM signals: deterministic idempotent record; no model call",
}

# Helpers that enforce the gate themselves, so a route delegating to one is
# gated even though its own signature is bare.
_GATED_HELPERS = {
    "_platform_mcp_endpoint",
    "_require_ctem_agentic_ai",
    # The LLM proxies: AI off answers the contract code, an absent licence the
    # mode-aware chat sentence (same two-reason shape as the MCP endpoints).
    "_refuse_unless_chat_enabled",
}


def _ungated_mutating_ai_routes() -> set[str]:
    import ast

    gates = {"require_agentic_ai", "require_ai_enabled", "require_chat_enabled"}
    inline = ("is_ai_enabled", "refuse_ai_disabled", "is_chat_enabled")
    found: set[str] = set()
    for path in app_sources.python_files(BACKEND / "app" / "api"):
        if path.name in _NON_AI_MODULES:
            continue
        tree = app_sources.tree(path)
        router_deps = _router_level_dependencies(tree)
        for node in app_sources.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            methods = [
                d.func.attr
                for d in node.decorator_list
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            ]
            if not [
                m
                for m in methods
                if m in {"post", "put", "patch", "delete", "websocket"}
            ]:
                continue
            deps = set(router_deps.get(_router_of(node) or "", set()))
            for default in node.args.defaults + list(node.args.kw_defaults):
                if (
                    isinstance(default, ast.Call)
                    and getattr(default.func, "id", "") == "Depends"
                ):
                    for arg in default.args:
                        deps.add(
                            getattr(arg, "id", None) or getattr(arg, "attr", None)
                        )
            source = ast.unparse(node)
            if (
                (deps & gates)
                or any(k in source for k in inline)
                or any(h in source for h in _GATED_HELPERS)
            ):
                continue
            found.add(node.name)
    return found


def test_every_mutating_ai_route_is_gated_or_exempt_on_the_record():
    """FR-009 / SC-004 / Scenario 4.2, as a standing check.

    `/speckit-converge` found 125 mutating AI routes with no AI gate, because
    T035 audited only the opposite direction — reads sitting behind write gates
    — and the endpoints gated after the third review were the executions alone.
    Everything else is now gated, and a route that is *not* has to be named in
    one of the exemption sets above, with the reason.

    The point is that a new route cannot join silently: it fails here until
    someone decides which it is.
    """
    exempt = (
        _EXEMPT_OPERATOR_CONTROL
        | _EXEMPT_READ_IN_POST
        | _EXEMPT_OWN_DATA_HOUSEKEEPING
        | _EXEMPT_DETERMINISTIC_TOOLING
        | _EXEMPT_DETERMINISTIC_FLOW_CONTROL
        | _EXEMPT_GATED_IN_HANDLER
        | _EXEMPT_INBOUND_DELIVERY
        | _EXEMPT_PLATFORM_ADMINISTRATION
        | _EXEMPT_OBSERVABILITY_CONFIG
    )
    unaccounted = _ungated_mutating_ai_routes() - exempt
    assert not unaccounted, (
        "these mutate AI resources with no server-side AI gate and no recorded "
        f"exemption: {sorted(unaccounted)}"
    )


def test_the_exemption_sets_do_not_rot():
    """An exemption for a route that no longer exists hides a real gap.

    If a name here stops matching an ungated route — because it was gated, or
    renamed, or deleted — the set should shrink rather than quietly grant cover
    to nothing.
    """
    ungated = _ungated_mutating_ai_routes()
    for label, names in (
        ("operator control", _EXEMPT_OPERATOR_CONTROL),
        ("read-in-POST", _EXEMPT_READ_IN_POST),
        ("own-data housekeeping", _EXEMPT_OWN_DATA_HOUSEKEEPING),
        ("deterministic tooling", _EXEMPT_DETERMINISTIC_TOOLING),
        ("deterministic flow control", _EXEMPT_DETERMINISTIC_FLOW_CONTROL),
        ("inbound delivery", _EXEMPT_INBOUND_DELIVERY),
        ("platform administration", _EXEMPT_PLATFORM_ADMINISTRATION),
        ("observability config", _EXEMPT_OBSERVABILITY_CONFIG),
    ):
        stale = names - ungated
        assert not stale, f"{label}: exemptions covering nothing: {sorted(stale)}"


# --- The entrypoints that are not HTTP routes in an "AI module" -----------


def test_the_a2a_task_surface_refuses_when_ai_is_disabled():
    """FR-010: "every entrypoint", including integration-driven calls.

    ``message/send`` reaches ``execute_task_run`` and runs an agent, so the A2A
    surface starts AI work — reached by another *agent* rather than a person.
    Converge found it with no gate of any kind, because the earlier sweep
    enumerated "AI modules" by hand and this file was not in the list.

    Refused plainly rather than folded into ``_not_found()``: that envelope
    hides which of several data-revealing conditions failed, while the AI state
    is already public on the unauthenticated ``/platform/config`` — and FR-010
    forbids failing obscurely.
    """
    from app.api.v1 import a2a_server

    source = inspect.getsource(a2a_server.a2a_jsonrpc)
    assert "is_ai_enabled" in source
    assert "refuse_ai_disabled" in source
    # Before the exposure is resolved or a task runs. ``_spawn_task_run`` is
    # the only way a run starts from this surface, on a fresh submission and
    # on a continuation alike.
    guard = source.index("refuse_ai_disabled")
    for later in ("get_exposure_by_token(", "_spawn_task_run"):
        assert guard < source.index(later), later

    # And below the per-IP rate limiter, deliberately: that limiter is this
    # surface's only control on external load, so a gate above it made an
    # unauthenticated flood pay a cache-miss database round-trip before being
    # throttled (/code-review, 2026-08-28).
    assert source.index("_enforce_preauth_rate_limit") < guard

    # The body is still not parsed on the path that reaches the gate. The one
    # ``request.json`` above it lives in the throttle refusal branch, which
    # returns before the gate — so compare against the *last* occurrence, the
    # real parse. Checking the first is what made this assertion fail on a
    # correct reordering.
    assert guard < source.rindex("await request.json")


def test_every_ai_dependent_worker_job_refuses_when_ai_is_disabled():
    """FR-007: no request to any model or embedding provider.

    Gating the API layer never covered a job **already queued** when AI was
    switched off, and ``app/worker.py`` had zero AI-state references while
    ``process_document_job`` is documented "chunk, embed, store in pgvector".
    """
    from app import worker

    for fn in (
        worker.process_document_job,
        worker.workset_auto_pass_job,
        worker.workset_cluster_job,
    ):
        source = inspect.getsource(fn)
        assert "_skip_because_ai_is_disabled" in source, fn.__name__


def test_a_refused_job_leaves_its_subject_retryable():
    """It must not flag the document errored (FR-025, and plain recoverability).

    The helper *returns* rather than raising, so the subject stays pending —
    the same reasoning that makes ``queue_pending`` fire-and-forget. Raising
    would burn SAQ retries and eventually mark the document errored, leaving
    manual repair for the administrator that FR-025 says re-enabling must not
    need.
    """
    import ast
    import textwrap

    from app import worker

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(worker._skip_because_ai_is_disabled))
    )
    fn = tree.body[0]
    raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
    assert not raises, "a refusal here must not raise — the job would be retried"

    returns = {
        ast.unparse(n.value)
        for n in ast.walk(fn)
        if isinstance(n, ast.Return) and n.value is not None
    }
    assert returns == {"False", "True"}, returns


def test_the_worker_gate_is_checked_before_the_work_starts():
    """FR-013 allows work already *running* to finish; this is about work that
    has not begun. The check therefore has to precede the first await that does
    anything — otherwise it either cancels a live job or arrives too late."""
    from app import worker

    source = inspect.getsource(worker.process_document_job)
    guard = source.index("_skip_because_ai_is_disabled")
    assert guard < source.index("process_document(document_id")
    assert guard < source.index("_ensure_document_processing_semaphore")


def test_the_non_ai_module_list_is_justified_and_current():
    """Every exclusion carries a reason, and names a module that exists.

    A stale entry silently removes a whole module from the sweep — the failure
    this inversion exists to prevent, reintroduced by drift.
    """
    api = BACKEND / "app" / "api"
    present = {p.name for p in api.rglob("*.py")}
    missing = {m for m in _NON_AI_MODULES if m not in present}
    assert not missing, f"classified but no longer present: {sorted(missing)}"

    unexplained = [m for m, why in _NON_AI_MODULES.items() if not why.strip()]
    assert not unexplained, unexplained


def test_the_sweep_covers_every_module_with_mutating_routes():
    """The inversion only works if the sweep really walks everything.

    Asserted rather than assumed: if the walk were narrowed again — to a
    directory, a prefix, a hand-written list — this fails while the gate tests
    keep passing, which is exactly how the previous guard went stale.
    """
    import ast

    api = BACKEND / "app" / "api"
    with_mutating_routes = set()
    for path in api.rglob("*.py"):
        if path.name.startswith("__"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            methods = [
                d.func.attr
                for d in node.decorator_list
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            ]
            if [
                m
                for m in methods
                if m in {"post", "put", "patch", "delete", "websocket"}
            ]:
                with_mutating_routes.add(path.name)
                break

    # Every such module is either swept or consciously excluded.
    unclassified = with_mutating_routes - _NON_AI_MODULES.keys()
    assert unclassified, "the sweep found nothing — it has been narrowed"
    assert len(with_mutating_routes) >= 40, len(with_mutating_routes)

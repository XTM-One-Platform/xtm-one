"""CTEM and organization-context module calls act as the person, never as a
service account, and never send an OpenCTI query that person's role cannot pass.

A SaaS customer's OpenCTI activity log showed "<analyst> tries an unauthorized
read" every Monday from several analysts: the weekly CTEM fan-out and the
dashboard's posture build ran as each user, with that user's JWT, and sent
queries (PIRs, playbooks) the user's role cannot pass. OpenCTI records every
FORBIDDEN_ACCESS as an unauthorized read attributed to the caller.

These tests pin:
* the document-level gate in ``_common._graphql`` (every query of the CTEM
  collector, the OpenCTI trigger provisioning on a registered platform and the
  tools of an unattended run, whatever builds it);
* the root-field table read from the OpenCTI schema;
* the CTEM posture build carrying the probe;
* the service-account rule (``module_identity``) on the CTEM and
  organization-context paths only (agents, assignments, custom dashboards,
  pollers and trigger provisioning on a company static key behave as before),
  applied on a registered deployment only, no stored credential ever deleted
  or skipped by the shared resolver, and the guard that XTM One never mints a
  token on a platform.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.integrations.http import RefreshableHeaders
from app.integrations.providers.opencti import _common as opencti_common
from app.integrations.providers.opencti.capabilities import (
    _ROOT_FIELD_TABLE,
    attach_probe,
    query_requirement,
    root_fields,
)
from app.services import ctem_metrics_collector as collector
from app.services import module_identity

BACKEND = Path(__file__).resolve().parents[1]
CURRENT = "7.260917.0"


class _Resp:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _stub(monkeypatch, capabilities: list[str] | None, *, probe_error: str | None = None) -> list[str]:
    """Answer the capability probes; answer anything else with empty data."""
    operations: list[str] = []

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        operation = action.removeprefix("execute GraphQL operation ").strip("'")
        operations.append(operation)
        if operation == "MeCapabilities":
            if probe_error:
                return None, probe_error
            caps = [{"name": c} for c in capabilities or []]
            return _Resp({"data": {"me": {"capabilities": caps}, "about": {"version": CURRENT}}}), None
        if operation == "MeDraftCapabilities":
            return _Resp({"data": {"me": {"capabilitiesInDraft": []}}}), None
        return _Resp({"data": {}}), None

    monkeypatch.setattr(opencti_common, "safe_request", _fake_safe_request)
    return operations


def _probed(base: str = "https://octi.test") -> RefreshableHeaders:
    return attach_probe({"Authorization": "Bearer jwt-of-the-analyst"}, base)


async def _send(headers, query: str) -> tuple[dict, str | None]:
    async with opencti_common._client(headers, timeout=5) as client:
        return await opencti_common._graphql(client, "https://octi.test", headers, query)


# ---------------------------------------------------------------------------
# The root-field table and the document parser
# ---------------------------------------------------------------------------


def test_the_table_carries_the_directives_the_refusals_came_from():
    query = _ROOT_FIELD_TABLE["query"]
    assert query["pirs"] == ["PIRAPI"]
    assert query["playbooks"] == ["AUTOMATION"]
    assert query["connectors"] == ["MODULES"]
    assert "SETTINGS_SETACCESSES" in query["users"]
    assert "me" not in query and "about" not in query and "settings" not in query
    assert set(query["stixCoreObjectsMultiNumber"]) == {"KNOWLEDGE", "EXPLORE"}


@pytest.mark.parametrize(
    ("document", "kind", "fields"),
    [
        ("query CtemPirs { pirs(first: 1) { pageInfo { globalCount } } }", "query", ["pirs"]),
        (
            "query CtemPlaybooks { all: playbooks(first: 1) { pageInfo { globalCount } } "
            'running: playbooks(first: 1, filters: {mode: and, filters: [{key: ["a"], values: ["{x}"]}]}) '
            "{ pageInfo { globalCount } } }",
            "query",
            ["playbooks", "playbooks"],
        ),
        ("{ me { name } about { version } }", "query", ["me", "about"]),
        ("mutation M($i: X!) { notifierAdd(input: $i) { id } }", "mutation", ["notifierAdd"]),
        ("query Q { a: stixCoreObjects @include(if: true) { edges { node { id } } } }", "query", ["stixCoreObjects"]),
    ],
)
def test_root_fields_are_read_from_the_document(document, kind, fields):
    assert root_fields(document) == (kind, fields)


def test_pir_relationships_need_pirapi_whatever_the_directive_admits():
    assert query_requirement("query Q($p: ID!) { pirRelationships(pirId: $p) { edges { node { id } } } }") == (
        ("PIRAPI",),
    )


# ---------------------------------------------------------------------------
# The document gate in _graphql
# ---------------------------------------------------------------------------


_GATED = [
    ("CtemPirs", "query CtemPirs { pirs(first: 1) { pageInfo { globalCount } } }", ["PIRAPI"]),
    ("CtemPirIds", "query CtemPirIds { pirs(first: 10) { edges { node { id } } } }", ["PIRAPI"]),
    (
        "CtemPlaybooks",
        "query CtemPlaybooks { all: playbooks(first: 1) { pageInfo { globalCount } } }",
        ["AUTOMATION"],
    ),
    ("XtmNotifier", "query XtmNotifier($id: String!) { notifier(id: $id) { id } }", ["SETTINGS_SETCUSTOMIZATION"]),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "query", "passes"), _GATED, ids=[g[0] for g in _GATED])
async def test_a_query_the_role_cannot_pass_is_never_sent(monkeypatch, operation, query, passes):
    operations = _stub(monkeypatch, ["KNOWLEDGE", "EXPLORE"])
    data, err = await _send(_probed(), query)
    assert data == {}
    assert err is not None and err.startswith("Not sent:")
    assert operation not in operations, "the refused call must never reach OpenCTI"
    assert operations == ["MeCapabilities"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "query", "passes"), _GATED, ids=[g[0] for g in _GATED])
async def test_a_role_that_passes_is_queried_as_before(monkeypatch, operation, query, passes):
    operations = _stub(monkeypatch, passes)
    _, err = await _send(_probed(), query)
    assert err is None
    assert operations == ["MeCapabilities", operation]


@pytest.mark.asyncio
async def test_knowledge_reads_are_sent_and_the_probe_runs_once(monkeypatch):
    operations = _stub(monkeypatch, ["KNOWLEDGE"])
    headers = _probed()
    for _ in range(3):
        _, err = await _send(headers, "query CtemObs { observables: stixCyberObservablesNumber { total } }")
        assert err is None
    assert operations == ["MeCapabilities", "CtemObs", "CtemObs", "CtemObs"]


@pytest.mark.asyncio
async def test_bypass_sends_everything(monkeypatch):
    operations = _stub(monkeypatch, ["BYPASS"])
    headers = _probed()
    for operation, query, _ in _GATED:
        await _send(headers, query)
        assert operation in operations


@pytest.mark.asyncio
async def test_without_a_probe_nothing_changes(monkeypatch):
    """Interactive chat: the person asked, the call is sent (and audited) as before."""
    operations = _stub(monkeypatch, ["KNOWLEDGE"])
    headers = {"Authorization": "Bearer jwt"}
    await _send(headers, _GATED[0][1])
    assert operations == ["CtemPirs"]


@pytest.mark.asyncio
async def test_an_unreadable_probe_sends_everything(monkeypatch):
    operations = _stub(monkeypatch, None, probe_error="Error: OpenCTI server error (500)")
    await _send(_probed(), _GATED[0][1])
    assert "CtemPirs" in operations


# ---------------------------------------------------------------------------
# The CTEM posture build (dashboard load, background refresh)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_cyber_posture_of_a_plain_analyst_sends_no_gated_query(monkeypatch):
    """The exact Monday pattern: an analyst with KNOWLEDGE and EXPLORE only."""
    operations = _stub(monkeypatch, ["KNOWLEDGE", "KNOWLEDGE_KNUPDATE", "EXPLORE"])

    async def creds(_db, _user_id, provider):
        return ("https://octi.test", {"Authorization": "Bearer jwt"}) if provider == "opencti" else None

    async def no_stack(_db):
        return []

    monkeypatch.setattr(collector, "_user_creds", creds)
    monkeypatch.setattr(collector, "_organization_software", no_stack)
    stage_facts, present = await collector.build_posture_for_user(None, str(uuid.uuid4()), "cyber")
    assert "opencti" in present
    gated = {"CtemPirs", "CtemPirIds", "CtemPlaybooks", "CtemPirRel"}
    assert not gated & set(operations), operations
    assert operations.count("MeCapabilities") == 1
    assert stage_facts["scoping"]["pirs"] is None
    assert stage_facts["remediation"]["playbooks_total"] is None


@pytest.mark.asyncio
async def test_the_posture_of_a_user_with_the_capabilities_reads_them(monkeypatch):
    operations = _stub(monkeypatch, ["KNOWLEDGE", "EXPLORE", "PIRAPI", "AUTOMATION"])

    async def creds(_db, _user_id, provider):
        return ("https://octi.test", {"Authorization": "Bearer jwt"}) if provider == "opencti" else None

    async def no_stack(_db):
        return []

    monkeypatch.setattr(collector, "_user_creds", creds)
    monkeypatch.setattr(collector, "_organization_software", no_stack)
    await collector.build_posture_for_user(None, str(uuid.uuid4()), "cyber")
    assert {"CtemPirs", "CtemPirIds", "CtemPlaybooks"} <= set(operations)


def test_every_collector_query_is_checked_against_the_table():
    """A new collector query on a gated root field cannot slip past the gate:
    every document the collector defines resolves to a requirement the table
    knows (or none), and the gated ones are the ones expected."""
    source = (BACKEND / "app" / "services" / "ctem_metrics_collector.py").read_text(encoding="utf-8")
    documents = re.findall(r'^(_OCTI_[A-Z_]+) = """(.*?)"""', source, re.S | re.M)
    assert documents
    gated = {
        name
        for name, doc in documents
        if any(group != ("KNOWLEDGE", "EXPLORE") and "KNOWLEDGE" not in group for group in query_requirement(doc))
    }
    assert gated == {"_OCTI_PIRS", "_OCTI_PIR_IDS", "_OCTI_PIR_REL", "_OCTI_PLAYBOOKS"}


# ---------------------------------------------------------------------------
# No service account on the module paths
# ---------------------------------------------------------------------------

_ACTING = uuid.uuid4()
_OTHER = uuid.uuid4()


@pytest.mark.parametrize(
    ("holder", "allowed"),
    [
        pytest.param(SimpleNamespace(auth_type="platform_managed", user_id=_OTHER), True, id="registered-jwt"),
        pytest.param(SimpleNamespace(is_user_credential=True, user_id=_ACTING), True, id="own-connection"),
        pytest.param(
            SimpleNamespace(auth_type="api_key", user_id=_ACTING, oauth_token_owner_id=None), True, id="own-key"
        ),
        pytest.param(
            SimpleNamespace(auth_type="api_key", user_id=_OTHER, oauth_token_owner_id=None, is_company_managed=True),
            False,
            id="company-api-key",
        ),
        pytest.param(
            SimpleNamespace(auth_type="api_key", user_id=None, oauth_token_owner_id=None), False, id="ownerless-key"
        ),
        pytest.param(SimpleNamespace(is_user_credential=True, user_id=_OTHER), False, id="someone-elses-connection"),
    ],
)
@pytest.mark.parametrize("provider", sorted(module_identity.MODULE_PROVIDERS))
def test_a_module_is_called_as_the_acting_person_only(provider, holder, allowed):
    assert module_identity.refused_for_unattended(provider, holder, _ACTING) is (not allowed)
    assert (module_identity.service_account_refusal(provider, holder, _ACTING, registered=True) is None) is allowed


@pytest.mark.parametrize("provider", sorted(module_identity.MODULE_PROVIDERS))
def test_an_unregistered_deployment_keeps_its_static_key(provider):
    """Sam's rule 1 (13 Nov 2026): with no registered row for the provider, the
    company static key is the only way in and the syncs use it as before."""
    holder = SimpleNamespace(auth_type="api_key", user_id=_OTHER, oauth_token_owner_id=None, is_company_managed=True)
    assert module_identity.service_account_refusal(provider, holder, _ACTING, registered=False) is None
    assert module_identity.service_account_refusal(provider, holder, _ACTING, registered=True) is not None


class _Rows:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Db:
    def __init__(self, row):
        self.row = row
        self.statements: list[Any] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.row)


@pytest.mark.asyncio
@pytest.mark.parametrize(("row", "registered"), [((uuid.uuid4(),), True), (None, False)])
async def test_registration_is_read_from_a_platform_managed_row(row, registered):
    db = _Db(row)
    assert await module_identity.platform_is_registered(db, "opencti") is registered
    sql = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "platform_managed" in sql and "opencti" in sql


def test_other_providers_keep_their_shared_credentials():
    holder = SimpleNamespace(auth_type="api_key", user_id=_OTHER, oauth_token_owner_id=None)
    assert not module_identity.refused_for_unattended("jira", holder, _ACTING)


def test_nobody_acting_is_never_a_module_identity():
    holder = SimpleNamespace(auth_type="platform_managed", user_id=_OTHER)
    assert module_identity.refused_for_unattended("opencti", holder, None)


def test_registration_never_touches_a_stored_credential():
    """Sam, 13 Nov 2026: a credential stored on purpose is never deleted by a
    registration (the capability probe alone fixes the unauthorized reads)."""
    source = (BACKEND / "app" / "api" / "v1" / "platform.py").read_text(encoding="utf-8")
    start = source.index("async def register_platform(")
    body = source[start : source.index("\nasync def ", start + 1)]
    for column in ("access_token_enc", "refresh_token_enc", "api_key_enc", "token_expires_at"):
        assert column not in body, column
    assert "clear_stored_credentials" not in source
    assert not hasattr(module_identity, "clear_stored_credentials")


def test_the_organization_context_syncs_check_the_identity():
    """Tripwire: the organization-context syncs go through
    ``service_account_refusal`` before they read a token."""
    for relative in ("services/organization_opencti.py", "services/organization_context_opencrq.py"):
        source = (BACKEND / "app" / relative).read_text(encoding="utf-8")
        assert "service_account_refusal(" in source, relative
        assert source.index("await platform_is_registered(db, ") < source.index("service_account_refusal("), relative
        token_read = re.search(r"await _get_token\(\s*db", source)
        assert token_read is not None, relative
        assert source.index("service_account_refusal(") < token_read.start(), relative


def test_the_rule_stays_on_the_ctem_and_organization_context_paths():
    """Scope pin (Sam, 13 Nov 2026): agents, assignments, custom dashboards,
    pollers and trigger provisioning bound to a company static key behave as
    before; the no-service-account rule covers CTEM and organization context."""
    for relative in (
        "services/integration_tools.py",
        "services/event_poller.py",
        "services/platform_access.py",
        "services/opencti_trigger_provisioning.py",
        "services/openaev_trigger_provisioning.py",
    ):
        source = (BACKEND / "app" / relative).read_text(encoding="utf-8")
        assert "refused_for_unattended" not in source, relative
        assert "service_account_refusal" not in source, relative


def test_opencti_trigger_provisioning_probes_a_registered_platform_only():
    source = (BACKEND / "app" / "services" / "opencti_trigger_provisioning.py").read_text(encoding="utf-8")
    guard = source.index('if getattr(integration, "auth_type", None) == "platform_managed":')
    assert guard < source.index("headers = attach_probe(headers, api_base)")


_ACCOUNT_MUTATIONS = re.compile(r"\b(userTokenAdd|userAdminTokenAdd|tokenAdd|apiTokenAdd)\b")


def test_xtm_one_never_mints_a_token_on_a_platform():
    """Guard: no code path of XTM One creates an API token on OpenCTI, OpenAEV
    or OpenCRQ, so there is no XTM One service account to delete there."""
    offenders = [
        str(path.relative_to(BACKEND))
        for path in (BACKEND / "app").rglob("*.py")
        if _ACCOUNT_MUTATIONS.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_only_user_creation_is_the_user_asked_admin_tool():
    """Guard: ``userAdd`` is sent only by the interactive ``manage_opencti_user``
    tool (a person with SETTINGS_SETACCESSES asks for it, as themselves); no
    background path creates a platform user, service account or not."""
    offenders = [
        str(path.relative_to(BACKEND))
        for path in (BACKEND / "app").rglob("*.py")
        if re.search(r"\buserAdd\b", path.read_text(encoding="utf-8"))
    ]
    assert offenders == ["app/integrations/providers/opencti/access.py"]


def test_the_shared_resolver_reads_a_stored_token_as_before():
    """The token resolution of every integration is the one of main before
    #11466: a stored token comes first, the minted JWT is the fallback."""
    source = (BACKEND / "app" / "services" / "integration_tools.py").read_text(encoding="utf-8")
    refreshed = source.index("        token = refreshed_token\n")
    stored = source.index("elif integ.access_token_enc:\n        token = decrypt_value(integ.access_token_enc)")
    assert source[refreshed:stored].count("elif") == 0, "no branch between the refresh and the stored token"
    assert stored < source.index('        if integ.auth_type == "platform_managed":\n            if not user_id')


def test_the_ctem_posture_prefers_a_registered_platform_and_keeps_a_static_key():
    source = (BACKEND / "app" / "services" / "ctem_metrics_collector.py").read_text(encoding="utf-8")
    start = source.index("async def _user_creds(")
    body = source[start : source.index("\nasync def ", start + 1)]
    assert 'Integration.auth_type == "platform_managed",\n' not in body, "no filter on the registration"
    assert 'case((Integration.auth_type == "platform_managed", 0), else_=1)' in body


def test_the_cleanup_migration_is_a_no_op():
    path = next((BACKEND / "alembic" / "versions").glob("svcacct0001_*.py"))
    source = path.read_text(encoding="utf-8")
    assert 'revision: str = "svcacct0001"' in source
    assert "UPDATE" not in source.upper().split('"""', 2)[2], "the revision writes nothing"


def test_the_root_field_table_is_valid_json():
    path = BACKEND / "app" / "integrations" / "providers" / "opencti" / "root_field_capabilities.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"source", "query", "mutation"}
    assert all(isinstance(v, list) and v for v in data["query"].values())

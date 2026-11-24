"""The XTM Hub sign-in entitlement gate.

XTM Hub issues one OIDC identity that reaches several XTM One instances, and
tells each instance whether it may admit a person through a claim named after
that instance: ``{platform_id}_groups``. Its **presence** is the entitlement —
there is nothing to compare a value against — and the roles it lists decide
whether the person is an administrator here.

Two properties are worth pinning hardest, because neither is exercised by a
successful sign-in and both fail silently:

* a refused sign-in must leave **no account behind**, which is a property of
  *where* the guard sits, not of what it decides; and
* administrator must be **removed** when the Hub stops asserting it — the half
  of a role sync that usually goes untested, and the reason this path cannot be
  folded into the promote-only auto-admin helper.

The decision itself is a pure function, so the whole table from
``contracts/oidc-entitlement.md`` is provable here without a browser, an
identity provider, or a database.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi.responses import RedirectResponse

from app.api.auth_sso import (
    HubStatus,
    _apply_hub_role,
    _resolve_hub_entitlement,
    oidc_callback,
)
from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.user import User

PLATFORM = "xtmone-admin"
OTHER_PLATFORM = "xtmone-user"
ADMIN_GROUP = "Admin"


@pytest.fixture
def gate_on(monkeypatch):
    """Enable the gate the way a deployment does: an admin group, and an ID.

    The identifier is this instance's own ``platform_id``. The test override is
    left empty here on purpose — it is a testing aid for the Hub development
    tenant, not part of the design, and the suite must exercise the path
    production takes.
    """
    monkeypatch.setattr(settings, "xtm_hub_admin_group", ADMIN_GROUP)
    monkeypatch.setattr(settings, "platform_id", PLATFORM)
    monkeypatch.setattr(settings, "xtm_hub_platform_id", "")


@pytest.fixture
def gate_off(monkeypatch):
    """The default for every deployment that never heard of XTM Hub.

    ``platform_id`` is deliberately SET here: an instance that forces one for
    clone/restore continuity must not thereby acquire a sign-in gate.
    """
    monkeypatch.setattr(settings, "xtm_hub_admin_group", "")
    monkeypatch.setattr(settings, "platform_id", PLATFORM)
    monkeypatch.setattr(settings, "xtm_hub_platform_id", "")


def _userinfo(**claims) -> dict:
    """Userinfo as the Hub sends it, plus whatever the test overrides."""
    base = {
        "email": "person@example.com",
        "name": "A Person",
        "sub": "auth0|person",
        # The namespaced corporate group claim the Hub also emits. Present in
        # every fixture precisely so a test can prove it is never read.
        "https://xtm-hub.filigran.io/groups": ["Filigran", "Engineering"],
    }
    base.update(claims)
    return base


# ===================================================================
# The decision table (contracts/oidc-entitlement.md)
# ===================================================================


def test_unconfigured_gate_is_disabled(gate_off):
    """No admin group means no gate — the whole feature is inert.

    Note the fixture sets ``platform_id``: forcing a platform identifier is an
    ordinary thing to do for clone/restore identity continuity, and it must not
    switch on a sign-in gate as a side effect. The admin group is the switch
    precisely because nothing else sets it by accident.
    """
    assert _resolve_hub_entitlement(_userinfo()).status is HubStatus.DISABLED


def test_a_platform_id_alone_does_not_enable_the_gate(monkeypatch):
    """The regression this inversion exists to prevent.

    Every instance that forces ``PLATFORM_ID`` — a clone, a restore, an
    OpenCTI-style deployment — would otherwise start refusing everyone whose
    identity provider has never heard of XTM Hub.
    """
    monkeypatch.setattr(settings, "platform_id", PLATFORM)
    monkeypatch.setattr(settings, "xtm_hub_platform_id", "")
    monkeypatch.setattr(settings, "xtm_hub_admin_group", "")
    assert _resolve_hub_entitlement(_userinfo()).status is HubStatus.DISABLED


def test_the_identifier_comes_from_platform_id(gate_on):
    """The claim is named after this instance, and that is ``platform_id``."""
    info = _userinfo(**{f"{PLATFORM}_groups": ["Admin"]})
    decision = _resolve_hub_entitlement(info)
    assert (decision.status, decision.is_admin) == (HubStatus.ENTITLED, True)


def test_the_local_override_wins_when_set(gate_on, monkeypatch):
    """The escape hatch, kept honest.

    A local aid for pointing a developer's instance at a Hub tenant that still
    issues non-UUID names — not a deployment setting, and named in no
    documentation. Pinned only so it cannot break silently; deleting it should
    cost nothing but this test.
    """
    monkeypatch.setattr(settings, "xtm_hub_platform_id", "override-name")
    assert _resolve_hub_entitlement(
        _userinfo(**{f"{PLATFORM}_groups": ["Admin"]})
    ).status is HubStatus.REFUSED
    assert _resolve_hub_entitlement(
        _userinfo(**{"override-name_groups": ["Admin"]})
    ).is_admin is True


def test_an_enabled_gate_with_no_identifier_fails_closed(monkeypatch):
    """Startup refuses this pairing, so reaching it means a live mutation.

    A security gate that cannot name the claim it needs has been satisfied by
    nothing, so it admits nobody. The loud failure belongs at boot, not here.
    """
    monkeypatch.setattr(settings, "xtm_hub_admin_group", ADMIN_GROUP)
    monkeypatch.setattr(settings, "platform_id", "")
    monkeypatch.setattr(settings, "xtm_hub_platform_id", "")
    assert _resolve_hub_entitlement(_userinfo()).status is HubStatus.REFUSED


def test_absent_claim_is_refused(gate_on):
    """Presence *is* the entitlement, so absence is the refusal."""
    decision = _resolve_hub_entitlement(_userinfo())
    assert decision.status is HubStatus.REFUSED
    assert decision.is_admin is False


def test_a_claim_for_another_instance_does_not_entitle_here(gate_on):
    """The realistic refusal: entitled somewhere, just not here.

    One identity reaches several instances, so the failure that matters is not
    a missing claim but the *wrong* one — a person the Hub knows, carrying a
    perfectly valid entitlement to a different XTM One.
    """
    info = _userinfo(**{f"{OTHER_PLATFORM}_groups": ["Admin"]})
    assert _resolve_hub_entitlement(info).status is HubStatus.REFUSED


@pytest.mark.parametrize("malformed", ["Admin", {"role": "Admin"}, None, 42])
def test_malformed_claim_is_refused_not_coerced(gate_on, malformed):
    """A claim we cannot read is a claim we cannot admit on.

    Reading a bare string as a one-element list, or truthiness as entitlement,
    is how a gate quietly stops being one.
    """
    info = _userinfo(**{f"{PLATFORM}_groups": malformed})
    assert _resolve_hub_entitlement(info).status is HubStatus.REFUSED


def test_admin_marker_entitles_as_administrator(gate_on):
    info = _userinfo(**{f"{PLATFORM}_groups": ["Admin"]})
    decision = _resolve_hub_entitlement(info)
    assert (decision.status, decision.is_admin) == (HubStatus.ENTITLED, True)


def test_claim_without_admin_marker_entitles_as_ordinary_user(gate_on):
    info = _userinfo(**{f"{PLATFORM}_groups": ["User"]})
    decision = _resolve_hub_entitlement(info)
    assert (decision.status, decision.is_admin) == (HubStatus.ENTITLED, False)


def test_empty_role_list_entitles_as_ordinary_user(gate_on):
    """The claim is present, so the person is entitled; they just hold no role."""
    info = _userinfo(**{f"{PLATFORM}_groups": []})
    decision = _resolve_hub_entitlement(info)
    assert (decision.status, decision.is_admin) == (HubStatus.ENTITLED, False)


@pytest.mark.parametrize("near_miss", ["admin", "ADMIN", "Admins", " Admin"])
def test_admin_marker_is_matched_exactly_including_case(gate_on, near_miss):
    """The Hub owns these names; a lenient comparison grants admin on a typo."""
    info = _userinfo(**{f"{PLATFORM}_groups": [near_miss]})
    decision = _resolve_hub_entitlement(info)
    assert (decision.status, decision.is_admin) == (HubStatus.ENTITLED, False)


def test_admin_marker_is_read_from_this_instance_claim_only(gate_on):
    """Administrator elsewhere is not administrator here (FR-004)."""
    info = _userinfo(
        **{f"{PLATFORM}_groups": ["User"], f"{OTHER_PLATFORM}_groups": ["Admin"]}
    )
    assert _resolve_hub_entitlement(info).is_admin is False


def test_the_decision_performs_no_io(gate_on):
    """Pure by contract: no database, no network, no clock.

    Asserted structurally — the helper is called with nothing but a dict, so a
    later change that reaches for a session or an HTTP client cannot satisfy
    this signature without changing it.
    """
    info = _userinfo(**{f"{PLATFORM}_groups": ["Admin"]})
    assert _resolve_hub_entitlement(info) == _resolve_hub_entitlement(info)


def test_the_corporate_group_claim_is_never_read(gate_on):
    """FR-004a: broad organisational membership answers a different question.

    Proven by making the corporate claim assert administrator while the
    per-instance claim does not: reading the wrong one flips the outcome.
    """
    info = _userinfo(
        **{
            f"{PLATFORM}_groups": ["User"],
            "https://xtm-hub.filigran.io/groups": ["Admin", "Filigran"],
        }
    )
    decision = _resolve_hub_entitlement(info)
    assert (decision.status, decision.is_admin) == (HubStatus.ENTITLED, False)


# ===================================================================
# Role application — both directions, and the exemption
# ===================================================================


def _user(role: str, email: str = "person@example.com") -> User:
    # ``is_active`` only gets its default at INSERT; an unflushed ``User()``
    # carries ``None``, which the SSO sign-in reads as a deactivated account.
    return User(email=email, full_name="A Person", role=role, is_active=True)


def test_admin_marker_grants_administrator():
    user = _user("user")
    _apply_hub_role(user, is_admin=True)
    assert user.role == "admin"


def test_administrator_is_removed_when_the_marker_disappears():
    """FR-005, and the half of a role sync that usually goes untested.

    Central role management is only authoritative if it can take access away.
    A promote-only implementation passes every other test in this file.
    """
    user = _user("admin")
    _apply_hub_role(user, is_admin=False)
    assert user.role == "user"


def test_the_platform_admin_is_never_demoted(monkeypatch):
    """FR-006: the one failure with no way back.

    The gate stands between the operator and their own instance, so a Hub-side
    group change that stripped this account would be unrecoverable through the
    same door it came in by.
    """
    monkeypatch.setattr(settings, "admin_email", "operator@example.com")
    user = _user("admin", email="operator@example.com")
    _apply_hub_role(user, is_admin=False)
    assert user.role == "admin"


def test_the_platform_admin_is_still_promoted(monkeypatch):
    """The exemption covers demotion only — it is not a blanket opt-out."""
    monkeypatch.setattr(settings, "admin_email", "operator@example.com")
    user = _user("user", email="operator@example.com")
    _apply_hub_role(user, is_admin=True)
    assert user.role == "admin"


def test_role_changes_are_logged(caplog):
    """A privilege change is security-sensitive; leave a breadcrumb either way."""
    user = _user("user")
    with caplog.at_level(logging.INFO, logger="app.api.auth_sso"):
        _apply_hub_role(user, is_admin=True)
    assert any("person@example.com" in r.getMessage() for r in caplog.records)


# ===================================================================
# The callback: where the guard sits
# ===================================================================


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def all(self):
        # No group has auto join on in these fakes (#1901).
        return []


class _FakeDb:
    """Enough session to drive ``oidc_callback`` without Postgres.

    Selects are answered by table: the users table yields ``existing_user`` (the
    tests set up at most one relevant account), anything else yields nothing.
    What matters is ``added`` — the objects the callback tried to persist, which
    is how "a refused sign-in leaves no account behind" is asserted.
    """

    def __init__(self, existing_user: User | None = None):
        self.existing_user = existing_user
        self.added: list = []
        self.committed = False

    async def execute(self, stmt):
        # SQLAlchemy renders the clause on its own line, hence no leading space.
        return _Result(self.existing_user if "FROM users" in str(stmt) else None)

    async def scalar(self, stmt):
        return 1  # not the first account, so ``_determine_role`` returns "user"

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    @property
    def created_users(self) -> list[User]:
        return [o for o in self.added if isinstance(o, User)]

    @property
    def audit_rows(self) -> list[AuditLog]:
        return [o for o in self.added if isinstance(o, AuditLog)]


@pytest.fixture
def oidc_stack(monkeypatch):
    """Patch the callback's provider plumbing, leaving the gate itself real.

    Discovery advertises no userinfo endpoint, so the claims arrive through
    ``token_resp["userinfo"]``. Both provider shapes land in the same variable
    before the gate reads it — ``test_the_gate_reads_the_userinfo_endpoint_too``
    covers the other one.
    """
    import app.core.sso_helpers as sso_helpers
    from app.api import auth_sso

    cfg = SimpleNamespace(
        oidc_enabled=True,
        oidc_auto_admin_new_users=False,
        oidc_auto_create_users=True,
        allowed_domain_list=[],
    )

    async def _cfg(db):
        return cfg

    async def _disco(_cfg):
        return {"token_endpoint": "https://idp.example.com/token"}

    def _client(_cfg):
        class _Client:
            async def fetch_token(self, *a, **kw):
                return {"access_token": "at", "userinfo": _client.userinfo}

        return _Client()

    monkeypatch.setattr(sso_helpers, "get_sso_config", _cfg)
    monkeypatch.setattr(auth_sso, "_oidc_discovery_from_cfg", _disco)
    monkeypatch.setattr(auth_sso, "_oidc_client_from_cfg", _client)
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com")
    return _client


async def test_a_refused_sign_in_creates_no_account(gate_on, oidc_stack):
    """FR-002, the security outcome, and a property of *placement*.

    The guard sits above ``_get_or_create_sso_user`` — the only thing that
    writes a user row — so nothing is created rather than created and removed.
    A create-then-check-then-delete implementation would pass a test that only
    counted rows at the end; this one fails it, because the account was never
    offered to the session.
    """
    oidc_stack.userinfo = _userinfo()  # no claim for this instance
    db = _FakeDb()

    response = await oidc_callback(code="x", db=db)

    assert isinstance(response, RedirectResponse)
    assert db.created_users == []


async def test_a_refusal_redirects_rather_than_raising(gate_on, oidc_stack):
    """FR-007: the person lands on a page, not on raw JSON.

    Every other failure in this callback raises ``HTTPException``, which the
    browser renders as a JSON body. A refusal is an ordinary outcome for an
    ordinary person, so it gets an ordinary page.
    """
    oidc_stack.userinfo = _userinfo()
    response = await oidc_callback(code="x", db=_FakeDb())

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == (
        "https://app.example.com/login?sso_error=not_authorised"
    )


async def test_the_refusal_discloses_no_criteria(gate_on, oidc_stack):
    """FR-008: an unauthenticated visitor learns nothing about the IdP.

    The redirect carries a single opaque marker. The instance name, the claim
    name, and the admin marker are all configuration of the organisation's
    identity provider, and none of them may travel to someone we have decided
    not to admit.
    """
    oidc_stack.userinfo = _userinfo()
    response = await oidc_callback(code="x", db=_FakeDb())

    location = response.headers["location"]
    for secret in (PLATFORM, ADMIN_GROUP, "_groups", "claim"):
        assert secret not in location


async def test_a_refusal_is_recorded_against_an_existing_account(gate_on, oidc_stack):
    """FR-009: the refusals worth investigating are those against real accounts.

    Someone who had access and lost it, or someone probing a real account —
    both leave a durable, queryable row attributed to that account.
    """
    existing = _user("user")
    oidc_stack.userinfo = _userinfo()
    db = _FakeDb(existing_user=existing)

    await oidc_callback(code="x", db=db)

    assert db.created_users == []
    assert [row.action for row in db.audit_rows] == ["login_refused"]
    assert db.audit_rows[0].user_email == existing.email
    assert db.committed


async def test_a_refusal_for_an_unknown_address_is_logged_not_stored(
    gate_on, oidc_stack, caplog
):
    """FR-009a: nothing to attribute a row to, so nothing is written.

    A refusal can only follow a *successful* authentication at the Hub, so an
    unknown address is someone the Hub vouched for but did not entitle here —
    routine. It leaves log evidence rather than a row belonging to nobody.
    """
    oidc_stack.userinfo = _userinfo()
    db = _FakeDb(existing_user=None)

    with caplog.at_level(logging.WARNING, logger="app.api.auth_sso"):
        await oidc_callback(code="x", db=db)

    assert db.added == []
    assert any("person@example.com" in r.getMessage() for r in caplog.records)


async def test_the_recorded_reason_omits_the_corporate_group_claim(
    gate_on, oidc_stack, caplog
):
    """FR-004a extends to what we write down, not only what we read."""
    oidc_stack.userinfo = _userinfo()
    db = _FakeDb(existing_user=_user("user"))

    with caplog.at_level(logging.WARNING, logger="app.api.auth_sso"):
        await oidc_callback(code="x", db=db)

    written = db.audit_rows[0].summary + " ".join(
        r.getMessage() for r in caplog.records
    )
    for leaked in ("Filigran", "Engineering", "xtm-hub.filigran.io"):
        assert leaked not in written


async def test_an_entitled_person_signs_in(gate_on, oidc_stack):
    oidc_stack.userinfo = _userinfo(**{f"{PLATFORM}_groups": ["User"]})
    db = _FakeDb()

    response = await oidc_callback(code="x", db=db)

    assert len(db.created_users) == 1
    assert db.created_users[0].role == "user"
    assert "#token=" in response.headers["location"]


async def test_an_entitled_administrator_signs_in_as_one(gate_on, oidc_stack):
    oidc_stack.userinfo = _userinfo(**{f"{PLATFORM}_groups": ["Admin"]})
    db = _FakeDb()

    await oidc_callback(code="x", db=db)

    assert db.created_users[0].role == "admin"


async def test_an_existing_admin_is_demoted_on_sign_in(gate_on, oidc_stack):
    """The end-to-end half of FR-005: the demotion reaches a real account."""
    existing = _user("admin")
    oidc_stack.userinfo = _userinfo(**{f"{PLATFORM}_groups": ["User"]})

    await oidc_callback(code="x", db=_FakeDb(existing_user=existing))

    assert existing.role == "user"


async def test_the_hub_overrides_the_promote_only_auto_admin_toggle(
    gate_on, oidc_stack, monkeypatch
):
    """Both mechanisms can fire on one sign-in; under the gate the Hub wins.

    ``_apply_auto_admin`` runs inside account resolution and promotes. The Hub
    role is applied afterwards precisely so its "not an admin" is the last word
    — otherwise a deployment with the toggle on could never demote anyone.
    """
    import app.api.auth_sso as auth_sso

    async def _cfg(db):
        return SimpleNamespace(
            oidc_enabled=True,
            oidc_auto_admin_new_users=True,
            oidc_auto_create_users=True,
            allowed_domain_list=[],
        )

    monkeypatch.setattr("app.core.sso_helpers.get_sso_config", _cfg)
    oidc_stack.userinfo = _userinfo(**{f"{PLATFORM}_groups": ["User"]})
    db = _FakeDb()

    await auth_sso.oidc_callback(code="x", db=db)

    assert db.created_users[0].role == "user"


async def test_the_gate_reads_the_userinfo_endpoint_too(gate_on, oidc_stack, monkeypatch):
    """Both provider shapes converge on the same variable before the gate.

    Auth0 does not permit unnamespaced custom claims in an ID token, so in
    production these claims arrive only through the userinfo endpoint — the
    path this test exercises, and the one an implementation that decoded the ID
    token would miss entirely.
    """
    from app.api import auth_sso

    async def _disco(_cfg):
        return {
            "token_endpoint": "https://idp.example.com/token",
            "userinfo_endpoint": "https://idp.example.com/userinfo",
        }

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return _userinfo(**{f"{PLATFORM}_groups": ["Admin"]})

    class _Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(auth_sso, "_oidc_discovery_from_cfg", _disco)
    monkeypatch.setattr(auth_sso.httpx, "AsyncClient", lambda *a, **kw: _Http())
    oidc_stack.userinfo = {}
    db = _FakeDb()

    await auth_sso.oidc_callback(code="x", db=db)

    assert db.created_users[0].role == "admin"


# ===================================================================
# US3 — a deployment that never configured the gate
# ===================================================================


async def test_an_unconfigured_deployment_signs_in_exactly_as_before(
    gate_off, oidc_stack
):
    """SC-004: the feature must be invisible to everyone who did not ask for it.

    An unknown address, carrying no XTM Hub claim of any kind, is still
    provisioned — the behaviour that existed before this feature and that the
    gate must not quietly tighten.
    """
    oidc_stack.userinfo = _userinfo()
    db = _FakeDb()

    response = await oidc_callback(code="x", db=db)

    assert len(db.created_users) == 1
    assert db.created_users[0].email == "person@example.com"
    assert "#token=" in response.headers["location"]
    assert [row.action for row in db.audit_rows] == ["login"]


async def test_an_unconfigured_deployment_does_not_touch_roles(gate_off, oidc_stack):
    """With the gate off, an existing admin keeps admin whatever the Hub says.

    The demotion path is the sharpest edge this feature adds, so it must be
    provably unreachable without configuration — including for an identity that
    happens to carry Hub claims because it also uses another instance.
    """
    existing = _user("admin")
    oidc_stack.userinfo = _userinfo(**{f"{PLATFORM}_groups": ["User"]})

    await oidc_callback(code="x", db=_FakeDb(existing_user=existing))

    assert existing.role == "admin"


# ===================================================================
# Configuration: half-configured must fail at startup, not at sign-in
# ===================================================================


def test_an_enabled_gate_with_no_platform_id_refuses_to_start():
    """FR-011: fail loudly and early, not quietly at every sign-in.

    The gate looks for a claim named after this instance, so enabling it where
    there is no instance name refuses EVERYONE — and every refusal looks like an
    ordinary unentitled person, so nothing in the log says why. Refusing to boot
    costs one restart; the alternative is an outage nobody can diagnose from a
    login page.
    """
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError) as exc:
        Settings(
            xtm_hub_admin_group=ADMIN_GROUP,
            platform_id="",
            xtm_hub_platform_id="",
        )

    assert "PLATFORM_ID" in str(exc.value)


def test_whitespace_only_configuration_is_not_mistaken_for_a_value():
    """A stray space in a compose file is a typo, not a configuration."""
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            xtm_hub_admin_group=ADMIN_GROUP,
            platform_id="",
            xtm_hub_platform_id="   ",
        )


def test_a_platform_id_without_an_admin_group_starts_normally():
    """The overwhelmingly common case: an instance with an ID and no Hub.

    This is the pairing the previous version of this validator rejected, and
    rejecting it would have refused to boot on ordinary clone/restore
    deployments that have never heard of XTM Hub.
    """
    from app.core.config import Settings

    cfg = Settings(
        platform_id="123e4567-e89b-12d3-a456-426614174000",
        xtm_hub_admin_group="",
        xtm_hub_platform_id="",
    )
    assert cfg.xtm_hub_admin_group == ""


def test_the_local_override_satisfies_startup_on_its_own():
    """A developer instance using the override must still boot.

    Otherwise pointing one at the Hub tenant would mean inventing a UUID the Hub
    does not use, which is the situation the override exists to avoid.
    """
    from app.core.config import Settings

    cfg = Settings(
        xtm_hub_admin_group=ADMIN_GROUP,
        platform_id="",
        xtm_hub_platform_id=PLATFORM,
    )
    assert cfg.xtm_hub_effective_platform_id == PLATFORM


def test_the_gate_is_off_by_default():
    """SC-004 at its root: a deployment that says nothing gets nothing.

    Asserted on the field defaults rather than on a constructed ``Settings``,
    which would read this machine's ``.env`` and pass for the wrong reason.
    """
    from app.core.config import Settings

    assert Settings.model_fields["xtm_hub_platform_id"].default == ""
    assert Settings.model_fields["xtm_hub_admin_group"].default == ""


# ===================================================================
# The provider's own failures — every OIDC deployment, gate or no gate
# ===================================================================


async def test_declining_the_consent_screen_lands_on_a_page_not_json(gate_off):
    """The failure a person is most likely to cause themselves.

    Choosing "no" at the identity provider comes back as ``?error=access_denied``
    with no authorization code. While ``code`` was a required query parameter,
    FastAPI answered that with a 422 validation body — so declining a consent
    screen showed the person raw JSON about a missing field.

    Note the fixture: this is asserted with the XTM Hub gate OFF, because it has
    nothing to do with XTM Hub. It is how every OIDC deployment behaves.
    """
    response = await oidc_callback(error="access_denied", db=_FakeDb())

    assert isinstance(response, RedirectResponse)
    assert response.headers["location"].endswith("/login?sso_error=cancelled")


async def test_a_cancelled_sign_in_is_distinguished_from_a_refusal(gate_on, oidc_stack):
    """Two different events, and the person deserves two different sentences.

    A refusal means the organisation has not granted them access here. A
    cancellation means they changed their mind. Collapsing both into one message
    would either alarm someone who simply clicked "no", or tell someone who has
    genuinely lost access to "try again when you are ready".
    """
    oidc_stack.userinfo = _userinfo()
    refused = await oidc_callback(code="x", db=_FakeDb())
    cancelled = await oidc_callback(error="access_denied", db=_FakeDb())

    assert "not_authorised" in refused.headers["location"]
    assert "cancelled" in cancelled.headers["location"]


async def test_an_unexpected_provider_error_is_generic_to_the_person(gate_off, caplog):
    """The operator gets the detail; the visitor gets none of it.

    Provider error text describes how the identity provider is configured, and
    in the general case is attacker-influencable, so it is never echoed into a
    URL that an unauthenticated person is about to load.
    """
    with caplog.at_level(logging.WARNING, logger="app.api.auth_sso"):
        response = await oidc_callback(
            error="invalid_scope",
            error_description="the tenant does not permit <script>",
            db=_FakeDb(),
        )

    location = response.headers["location"]
    assert location.endswith("/login?sso_error=failed")
    assert "invalid_scope" not in location
    assert "script" not in location
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "invalid_scope" in logged and "does not permit" in logged


async def test_a_callback_with_neither_a_code_nor_an_error_is_not_a_crash(gate_off):
    """Someone opening the callback URL directly, or a mangled redirect.

    There is nothing to authenticate, so there is nothing to say beyond sending
    them back to the login page.
    """
    response = await oidc_callback(db=_FakeDb())
    assert response.headers["location"].endswith("/login?sso_error=failed")


async def test_the_provider_error_path_never_touches_the_database(gate_off):
    """It returns before the SSO config is even read.

    That placement is what makes this fix universal rather than a property of
    any particular deployment's configuration — and it means a provider error
    cannot create, modify, or audit anything.
    """
    db = _FakeDb()
    await oidc_callback(error="access_denied", db=db)

    assert db.added == []
    assert db.committed is False


# ===================================================================
# Convergence: what the log may be made to say, and who the gate stops
# ===================================================================


@pytest.mark.parametrize(
    "forged",
    [
        "ok\nWARNING:app.api.auth_sso:Admin granted to attacker@evil.test",
        "ok\r\nERROR: database corrupted",
        "line one\n\nline two",
        "tabs\tand\tspaces   collapsed",
    ],
)
async def test_provider_text_cannot_forge_a_log_line(gate_off, caplog, forged):
    """The callback is unauthenticated, so this text is attacker-controlled.

    Anyone can request `/oidc/callback?error=...&error_description=...` directly.
    Written verbatim, a newline would let a stranger append convincing entries to
    the operator's log — the very record FR-008 relies on, since the person is
    told nothing on the understanding that an operator can read what happened.
    """
    with caplog.at_level(logging.WARNING, logger="app.api.auth_sso"):
        await oidc_callback(error="access_denied", error_description=forged, db=_FakeDb())

    for record in caplog.records:
        assert "\n" not in record.getMessage()
        assert "\r" not in record.getMessage()


async def test_provider_text_is_truncated(gate_off, caplog):
    """One request must not cost an unbounded number of log bytes."""
    with caplog.at_level(logging.WARNING, logger="app.api.auth_sso"):
        await oidc_callback(
            error="x" * 5000, error_description="y" * 5000, db=_FakeDb()
        )

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "(truncated)" in logged
    assert len(logged) < 600


async def test_sanitising_leaves_ordinary_provider_text_readable(gate_off, caplog):
    """Bounding the log must not cost the operator the diagnosis.

    The point of logging this at all is that an unexpected provider error is the
    operator's only clue; a sanitiser that mangled it would trade one problem
    for another.
    """
    with caplog.at_level(logging.WARNING, logger="app.api.auth_sso"):
        await oidc_callback(
            error="invalid_scope",
            error_description="The tenant does not permit the requested scope",
            db=_FakeDb(),
        )

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "invalid_scope" in logged
    assert "does not permit the requested scope" in logged


async def test_the_platform_admin_is_refused_like_anyone_else(gate_on, oidc_stack, monkeypatch):
    """The asymmetry, pinned: exempt from demotion, not from refusal.

    ``_apply_hub_role`` never demotes the platform admin (FR-006), but that
    exemption lives *past* the guard. An unentitled `ADMIN_EMAIL` is turned away
    at the door like anyone else, and reaches the instance only through the local
    sign-in route FR-012 keeps open.

    **Recorded for the owner**: SC-006 says the platform administrator "retains
    access in **every** case", which reads broader than FR-006's "never remove
    administrator access". Only the second is implemented. If SC-006 is meant
    literally, the guard needs an `admin_email` exemption too — a deliberate
    decision, not an oversight, and this test will fail loudly if it is ever
    made without updating the wording.
    """
    monkeypatch.setattr(settings, "admin_email", "operator@example.com")
    existing = _user("admin", email="operator@example.com")
    oidc_stack.userinfo = _userinfo(email="operator@example.com")  # no claim here
    db = _FakeDb(existing_user=existing)

    response = await oidc_callback(code="x", db=db)

    assert "sso_error=not_authorised" in response.headers["location"]
    assert existing.role == "admin"  # refused, but never demoted either


# ===================================================================
# Review follow-up: the failure paths that still reached the browser
# ===================================================================


async def test_an_expired_or_replayed_code_lands_on_a_page(gate_off, oidc_stack, monkeypatch, caplog):
    """The most ordinary failure of all, and it used to be a 500.

    An authorization code is single-use and short-lived, so reloading this URL,
    pressing back onto it, or dawdling on the consent screen all fail the token
    exchange. That is someone mishandling a browser, not an incident, and it
    reached them as ``{"detail":"Internal Server Error"}``.
    """
    from app.api import auth_sso

    def _client(_cfg):
        class _Client:
            async def fetch_token(self, *a, **kw):
                raise ValueError("invalid_grant: authorization code expired")

        return _Client()

    monkeypatch.setattr(auth_sso, "_oidc_client_from_cfg", _client)

    with caplog.at_level(logging.WARNING, logger="app.api.auth_sso"):
        response = await oidc_callback(code="stale", db=_FakeDb())

    assert isinstance(response, RedirectResponse)
    assert response.headers["location"].endswith("/login?sso_error=failed")
    # The operator still gets the diagnosis, with a traceback.
    assert any(r.exc_info for r in caplog.records)


async def test_a_failing_userinfo_request_lands_on_a_page(gate_off, oidc_stack, monkeypatch):
    """The provider's profile endpoint is a second network call, and a second
    thing that can fail after the person has already authenticated."""
    from app.api import auth_sso

    async def _disco(_cfg):
        return {
            "token_endpoint": "https://idp.example.com/token",
            "userinfo_endpoint": "https://idp.example.com/userinfo",
        }

    class _Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(auth_sso, "_oidc_discovery_from_cfg", _disco)
    monkeypatch.setattr(auth_sso.httpx, "AsyncClient", lambda *a, **kw: _Http())
    oidc_stack.userinfo = {}

    response = await oidc_callback(code="x", db=_FakeDb())

    assert response.headers["location"].endswith("/login?sso_error=failed")


async def test_a_provider_that_returns_no_email_lands_on_a_page(gate_off, oidc_stack):
    """A misconfigured scope at the provider, not a mistake the person made.

    They are still the one holding the browser, so they get a page while the
    operator gets a log line naming the likely cause.
    """
    oidc_stack.userinfo = {"sub": "auth0|nobody", "name": "No Email"}
    db = _FakeDb()

    response = await oidc_callback(code="x", db=db)

    assert response.headers["location"].endswith("/login?sso_error=failed")
    assert db.created_users == []


# ===================================================================
# Review follow-up: a role change from the Hub leaves the same trail
# ===================================================================


async def test_a_hub_promotion_is_audited(gate_on, oidc_stack):
    """A privilege grant must be queryable, whoever decided it.

    ``users.py`` writes an audit row for every role change made in the admin UI.
    A Hub-driven promotion that left only an INFO log would mean an operator
    asking "who was made an administrator, and when" gets a complete answer for
    manual changes and nothing at all for central ones.
    """
    existing = _user("user")
    oidc_stack.userinfo = _userinfo(**{f"{PLATFORM}_groups": ["Admin"]})
    db = _FakeDb(existing_user=existing)

    await oidc_callback(code="x", db=db)

    assert existing.role == "admin"
    role_rows = [r for r in db.audit_rows if r.action == "update"]
    assert len(role_rows) == 1
    assert "'user' to 'admin'" in role_rows[0].summary
    assert "XTM Hub" in role_rows[0].summary


async def test_a_hub_demotion_is_audited(gate_on, oidc_stack):
    """The withdrawal needs a trail as much as the grant — arguably more, since
    it is the one nobody is expecting."""
    existing = _user("admin")
    oidc_stack.userinfo = _userinfo(**{f"{PLATFORM}_groups": ["User"]})
    db = _FakeDb(existing_user=existing)

    await oidc_callback(code="x", db=db)

    role_rows = [r for r in db.audit_rows if r.action == "update"]
    assert len(role_rows) == 1
    assert "'admin' to 'user'" in role_rows[0].summary


async def test_an_unchanged_role_is_not_audited(gate_on, oidc_stack):
    """Every entitled sign-in passes through this path. Writing a row each time
    would bury the changes that matter in a log of things that did not happen.
    """
    existing = _user("admin")
    oidc_stack.userinfo = _userinfo(**{f"{PLATFORM}_groups": ["Admin"]})
    db = _FakeDb(existing_user=existing)

    await oidc_callback(code="x", db=db)

    assert [r.action for r in db.audit_rows] == ["login"]


async def test_the_platform_admin_exemption_writes_no_role_row(gate_on, oidc_stack, monkeypatch):
    """Nothing changed, so nothing is recorded as having changed.

    The exemption keeps the role; an audit row saying otherwise would be a lie
    in the one trail an operator has to trust.
    """
    monkeypatch.setattr(settings, "admin_email", "operator@example.com")
    existing = _user("admin", email="operator@example.com")
    oidc_stack.userinfo = _userinfo(
        email="operator@example.com", **{f"{PLATFORM}_groups": ["User"]}
    )
    db = _FakeDb(existing_user=existing)

    await oidc_callback(code="x", db=db)

    assert existing.role == "admin"
    assert [r.action for r in db.audit_rows] == ["login"]

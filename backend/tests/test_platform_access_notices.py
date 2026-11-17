"""``POST /api/v1/platform/access-notices``: who may send one, what it says, where it links."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1 import platform_access_notices as route
from app.api.v1.platform_access_notices import AccessNoticeRequest, send_access_notice
from app.core import rate_limit
from app.core.config import settings
from app.core.platform_identity import RegisteredPlatform
from app.services import email as email_service
from app.services import platform_access_notices as service
from app.services import token_store
from app.services.email import access_granted_email, platform_invitation_email, unlinkable_text
from app.services.platform_access_notices import (
    NoticePlatform,
    acting_platform,
    invitation_destination,
    may_invite,
    registered_destination,
)
from tests.test_api_key_access import _FakeRedis

RECIPIENT_ID = uuid.uuid4()
PLATFORM = NoticePlatform(identifier="opencrq", title="OpenCRQ", public_url="https://opencrq.example.com")
ZWSP = "\u200b"


def _admin(**overrides):
    values = {
        "id": uuid.uuid4(),
        "auth_credential_kind": "platform_jwt",
        "platform_issuer": "https://opencrq.example.com",
        "full_name": "Claire Dubois",
        "email": "claire@acme.com",
        "is_admin": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _DB:
    def __init__(self, recipient=None, scalar=None):
        self.recipient = recipient
        self.scalar = scalar
        self.committed = False

    async def get(self, _model, key):
        if self.recipient is not None and self.recipient.id == key:
            return self.recipient
        return None

    async def execute(self, _stmt):
        scalar = self.scalar
        return SimpleNamespace(scalar_one_or_none=lambda: scalar)

    async def commit(self):
        self.committed = True


@pytest.fixture
def sent(monkeypatch):
    """Every email, rate-limit charge and audit entry the route makes."""
    record = {"emails": [], "limits": [], "audits": [], "email_result": True}

    async def _acting_platform(_db, user):
        return PLATFORM if user.auth_credential_kind == "platform_jwt" else None

    async def _send_email(**kwargs):
        record["emails"].append(kwargs)
        return record["email_result"]

    async def _enforce_rate_limit(_request, name, **kwargs):
        record["limits"].append((name, kwargs["bucket"]))

    async def _log_audit(_db, **kwargs):
        record["audits"].append(kwargs)

    monkeypatch.setattr(route, "acting_platform", _acting_platform)
    monkeypatch.setattr(route, "send_email", _send_email)
    monkeypatch.setattr(route, "enforce_rate_limit", _enforce_rate_limit)
    monkeypatch.setattr(route, "log_audit", _log_audit)
    return record


def _recipient(**overrides):
    values = {"id": RECIPIENT_ID, "email": "jane.martin@acme.com", "is_active": True}
    values.update(overrides)
    return SimpleNamespace(**values)


def _body(**overrides):
    values = {"recipient_user_id": str(RECIPIENT_ID), "scope_name": "Acme Corp", "path": "/t-123"}
    values.update(overrides)
    return AccessNoticeRequest(**values)


async def test_a_caller_that_is_not_a_registered_platform_is_refused(sent):
    db = _DB(recipient=_recipient())
    with pytest.raises(HTTPException) as exc:
        await send_access_notice(body=_body(), request=None, user=_admin(auth_credential_kind="api_key"), db=db)
    assert exc.value.status_code == 403
    assert sent["emails"] == []
    assert not db.committed


@pytest.mark.parametrize(
    ("recipient", "recipient_user_id"),
    [(None, str(RECIPIENT_ID)), (_recipient(is_active=False), str(RECIPIENT_ID)), (_recipient(), "not-an-id")],
    ids=["unknown", "deactivated", "malformed"],
)
async def test_an_unknown_deactivated_or_malformed_recipient_gets_one_answer(sent, recipient, recipient_user_id):
    """The directory (``GET /users/directory?ids=``) already tells any signed-in
    caller whether an id is a user and whether it is active: the route tells less,
    one 404 for all three."""
    body = _body(recipient_user_id=recipient_user_id)
    with pytest.raises(HTTPException) as exc:
        await send_access_notice(body=body, request=None, user=_admin(), db=_DB(recipient=recipient))
    assert (exc.value.status_code, exc.value.detail) == (404, "User not found")
    assert sent["emails"] == []


async def test_the_notice_is_emailed_with_a_link_on_the_platform(sent):
    admin = _admin()
    db = _DB(recipient=_recipient())

    response = await send_access_notice(body=_body(), request=None, user=admin, db=db)

    assert (response.outcome, response.user_id, response.email_sent, response.link) == (
        "notified",
        str(RECIPIENT_ID),
        True,
        None,
    )
    [email] = sent["emails"]
    assert email["to"] == "jane.martin@acme.com"
    assert email["subject"] == "You have access to OpenCRQ"
    assert 'href="https://opencrq.example.com/t-123"' in email["html"]
    assert "https://opencrq.example.com/t-123" in email["text"]
    assert "Claire Dubois gave you access to Acme Corp on OpenCRQ" in email["text"]
    assert sent["limits"] == [
        ("platform-access-notice-sender", str(admin.id)),
        ("platform-access-notice-platform", "opencrq"),
        ("platform-access-notice-recipient", str(RECIPIENT_ID)),
    ]
    [audit] = sent["audits"]
    assert audit["entity_type"] == "access_notice"
    assert audit["details"]["email_sent"] is True
    assert db.committed


async def test_without_an_email_server_the_notice_answers_not_sent(sent, monkeypatch):
    """The real ``send_email``, SMTP left unconfigured as it is by default."""
    monkeypatch.setattr(route, "send_email", email_service.send_email)
    monkeypatch.setattr(settings, "smtp_hostname", "")
    db = _DB(recipient=_recipient())

    response = await send_access_notice(body=_body(), request=None, user=_admin(), db=db)

    assert (response.email_sent, response.link) == (False, None)
    assert sent["audits"][0]["details"]["email_sent"] is False
    assert db.committed


async def test_a_notice_that_could_not_be_emailed_is_reported_not_raised(sent):
    sent["email_result"] = False
    db = _DB(recipient=_recipient())

    response = await send_access_notice(body=_body(), request=None, user=_admin(), db=db)

    assert (response.email_sent, response.link) == (False, None)
    assert sent["audits"][0]["summary"].endswith("(email not sent)")
    assert db.committed


@pytest.mark.parametrize(
    "path",
    ["//evil.example", "https://evil.example", "evil", "/a b", '/"><script>', "/a\\b", ""],
    ids=["protocol-relative", "absolute-url", "relative", "space", "markup", "backslash", "empty"],
)
def test_a_path_off_the_platform_is_refused(path):
    with pytest.raises(ValidationError):
        _body(path=path)


@pytest.mark.parametrize("path", ["/", "/t-123/settings?tab=users#top"], ids=["root", "query-and-fragment"])
def test_a_path_on_the_platform_is_kept(path):
    assert _body(path=path).path == path


@pytest.mark.parametrize("scope_name", ["Acme\nClick here", "   "], ids=["two-lines", "blank"])
def test_a_scope_name_must_be_one_printable_line(scope_name):
    with pytest.raises(ValidationError):
        _body(scope_name=scope_name)


def test_the_email_escapes_what_the_platform_supplies():
    html, _text = access_granted_email(
        link="https://opencrq.example.com/",
        platform_name="Open<i>CRQ</i>",
        scope_name="<b>Acme</b>",
        granted_by="<script>alert(1)</script>",
    )
    assert "<script>" not in html
    assert "<b>Acme</b>" not in html
    assert f"&lt;b&gt;Acme&lt;/{ZWSP}b&gt;" in html


LINKS = ["https://evil.example/login", "evil.example", "www.evil.example", "jane@evil.example", "\\\\evil\\share"]


@pytest.mark.parametrize("supplied", LINKS, ids=["url", "domain", "www", "address", "network-path"])
@pytest.mark.parametrize("field", ["scope_name", "granted_by"])
def test_a_link_in_what_the_platform_supplies_is_shown_as_text_no_client_links(field, supplied):
    values = {"scope_name": "Acme", "granted_by": "Claire Dubois"}
    values[field] = f"Acme {supplied}"
    html, text = access_granted_email(link="https://opencrq.example.com/t-1", platform_name="OpenCRQ", **values)

    for body in (html, text):
        assert supplied not in body
        assert "evil.example" not in body
    assert "evil" not in "".join(re.findall(r'href="([^"]*)"', html))
    # The reader sees the same text: only zero-width spaces were added.
    assert unlinkable_text(f"Acme {supplied}").replace(ZWSP, "") == f"Acme {supplied}"


def test_a_supplied_name_is_cut_to_its_cap():
    shown = unlinkable_text("x" * 500)
    assert len(shown) == 120
    assert shown.endswith("\u2026")
    _html, text = access_granted_email(
        link="https://opencrq.example.com/", platform_name="OpenCRQ", scope_name="Acme", granted_by="y" * 500
    )
    assert "y" * 120 not in text
    assert "y" * 119 + "\u2026" in text


def test_the_fine_print_does_not_repeat_the_first_sentence():
    html, text = access_granted_email(
        link="https://opencrq.example.com/", platform_name="OpenCRQ", scope_name="Acme", granted_by="Claire Dubois"
    )
    # Once in the body, once in the hidden preview: the fine print names nobody.
    assert html.count("Claire Dubois") == 2
    assert "You received this email because" not in html
    assert "If you weren&#x27;t expecting this access, you can ignore this email." in html
    assert text.count("Claire Dubois") == 1
    assert text.endswith("If you weren't expecting this access, you can ignore this email.")


@pytest.fixture
def limits(monkeypatch):
    """The real fixed-window limiter on an in-memory Redis; email and audit recorded."""
    redis = _FakeRedis()

    async def _redis():
        return redis

    async def _acting_platform(_db, _user):
        return PLATFORM

    async def _send_email(**_kwargs):
        return True

    async def _log_audit(_db, **_kwargs):
        return None

    monkeypatch.setattr(rate_limit, "redis_client", _redis)
    monkeypatch.setattr(route, "acting_platform", _acting_platform)
    monkeypatch.setattr(route, "send_email", _send_email)
    monkeypatch.setattr(route, "log_audit", _log_audit)
    return redis


def _recipients(count):
    return [_recipient(id=uuid.uuid4(), email=f"r{n}@acme.com") for n in range(count)]


class _Directory(_DB):
    def __init__(self, recipients):
        super().__init__()
        self.by_id = {r.id: r for r in recipients}

    async def get(self, _model, key):
        return self.by_id.get(key)


async def _send(db, user, recipient):
    return await send_access_notice(body=_body(recipient_user_id=str(recipient.id)), request=None, user=user, db=db)


async def _assert_refused(db, user, recipient):
    with pytest.raises(HTTPException) as exc:
        await _send(db, user, recipient)
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) > 0


def test_the_limits_are_200_a_sender_1000_a_platform_and_5_a_recipient():
    assert (route.SENDER_LIMIT_PER_HOUR, route.PLATFORM_LIMIT_PER_HOUR, route.RECIPIENT_LIMIT_PER_HOUR) == (
        200,
        1000,
        5,
    )


async def test_one_administrator_past_the_sender_limit_is_refused(limits, monkeypatch):
    monkeypatch.setattr(route, "SENDER_LIMIT_PER_HOUR", 2)
    people = _recipients(4)
    db, admin, other_admin = _Directory(people), _admin(), _admin()
    for person in people[:2]:
        assert (await _send(db, admin, person)).email_sent
    await _assert_refused(db, admin, people[2])
    assert (await _send(db, other_admin, people[3])).email_sent


async def test_one_platform_past_its_limit_is_refused_whoever_sends(limits, monkeypatch):
    monkeypatch.setattr(route, "PLATFORM_LIMIT_PER_HOUR", 2)
    people = _recipients(3)
    db = _Directory(people)
    for person in people[:2]:
        assert (await _send(db, _admin(), person)).email_sent
    await _assert_refused(db, _admin(), people[2])
    assert limits.values["rl:platform-access-notice-platform:opencrq"] == 3


async def test_one_person_past_the_recipient_limit_is_refused(limits):
    [person] = _recipients(1)
    db = _Directory([person])
    for _ in range(route.RECIPIENT_LIMIT_PER_HOUR):
        assert (await _send(db, _admin(), person)).email_sent
    await _assert_refused(db, _admin(), person)


@pytest.mark.parametrize("kind", ["session_jwt", "api_key", None], ids=["session", "api-key", "none"])
async def test_only_a_platform_token_names_an_acting_platform(kind):
    assert await acting_platform(_DB(), _admin(auth_credential_kind=kind)) is None


async def test_an_issuer_without_an_active_registration_names_no_platform(monkeypatch):
    async def _no_registration(_db, _issuer):
        return None

    monkeypatch.setattr(service, "platform_for_issuer", _no_registration)
    assert await acting_platform(_DB(), _admin()) is None


async def test_the_acting_platform_is_shown_on_its_public_url(monkeypatch):
    async def _registration(_db, _issuer):
        return RegisteredPlatform(
            platform_identifier="opencrq",
            platform_url="https://OpenCRQ.example.com/",
            api_url="http://opencrq:4000",
        )

    monkeypatch.setattr(service, "platform_for_issuer", _registration)
    platform = await acting_platform(_DB(scalar="OpenCRQ Acme"), _admin())
    assert platform == NoticePlatform(
        identifier="opencrq", title="OpenCRQ Acme", public_url="https://opencrq.example.com"
    )


# -- recipient by email: notify, invite or refuse -------------------------------

INVITEE = "new.person@acme.com"


@pytest.fixture
def invitations(monkeypatch):
    """The pending invitations, created and reissued in memory."""
    record = {"pending": {}, "created": [], "reissued": []}

    async def _find(email):
        return next((item for item in record["pending"].values() if item["email"] == email), None)

    async def _create(*, email, role, created_by_id, ttl_hours, destination=None):
        token = f"raw-{len(record['created'])}"
        record["created"].append(
            {"email": email, "role": role, "created_by_id": created_by_id, "destination": destination}
        )
        record["pending"][f"hash-{token}"] = {
            "id": f"hash-{token}",
            "email": email,
            "role": role,
            "destination": destination,
        }
        return token, f"hash-{token}", datetime.now(UTC) + timedelta(hours=ttl_hours)

    async def _reissue(token_hash, *, ttl_hours, destination=None):
        item = record["pending"].pop(token_hash, None)
        if item is None:
            return None
        token = f"raw-reissued-{len(record['reissued'])}"
        record["reissued"].append(token_hash)
        record["pending"][f"hash-{token}"] = {**item, "id": f"hash-{token}", "destination": destination}
        return token, f"hash-{token}", datetime.now(UTC) + timedelta(hours=ttl_hours)

    monkeypatch.setattr(token_store, "find_invitation_by_email", _find)
    monkeypatch.setattr(token_store, "create_invitation", _create)
    monkeypatch.setattr(token_store, "reissue_invitation", _reissue)
    monkeypatch.setattr(token_store, "invitation_link", lambda raw: f"https://xtm-one.example.com/register?token={raw}")
    return record


def _by_email(email=INVITEE, **overrides):
    values = {"recipient_email": email, "scope_name": "Acme Corp", "path": "/t-123"}
    values.update(overrides)
    return AccessNoticeRequest(**values)


@pytest.mark.parametrize(
    "recipients",
    [{}, {"recipient_user_id": str(RECIPIENT_ID), "recipient_email": INVITEE}],
    ids=["neither", "both"],
)
def test_a_notice_names_exactly_one_recipient(recipients):
    with pytest.raises(ValidationError):
        AccessNoticeRequest(scope_name="Acme", **recipients)


async def test_an_account_holder_named_by_email_is_notified(sent, invitations):
    db = _DB(scalar=_recipient())

    response = await send_access_notice(body=_by_email("Jane.Martin@Acme.com"), request=None, user=_admin(), db=db)

    assert (response.outcome, response.user_id, response.email_sent) == ("notified", str(RECIPIENT_ID), True)
    assert sent["emails"][0]["subject"] == "You have access to OpenCRQ"
    assert invitations["created"] == []


async def test_an_email_no_account_holds_is_invited_by_an_administrator(sent, invitations):
    admin = _admin()
    db = _DB()

    response = await send_access_notice(body=_by_email(), request=None, user=admin, db=db)

    assert (response.outcome, response.user_id, response.email_sent, response.link) == ("invited", None, True, None)
    assert invitations["created"] == [
        {
            "email": INVITEE,
            "role": "user",
            "created_by_id": admin.id,
            "destination": {"platform": "opencrq", "path": "/t-123"},
        }
    ]
    [email] = sent["emails"]
    assert email["to"] == INVITEE
    assert email["subject"] == "You're invited to OpenCRQ"
    assert 'href="https://xtm-one.example.com/register?token=raw-0"' in email["html"]
    assert "Claire Dubois invited you to Acme Corp on OpenCRQ. Create your" in email["text"]
    assert "Create your account" in email["html"]
    assert sent["limits"][-1] == ("platform-access-notice-recipient", INVITEE)
    [audit] = sent["audits"]
    assert (audit["entity_type"], audit["action"], audit["details"]["reused"]) == ("invitation", "create", False)
    assert db.committed


async def test_a_pending_invitation_is_reused_not_stacked(sent, invitations):
    await send_access_notice(body=_by_email(), request=None, user=_admin(), db=_DB())
    response = await send_access_notice(body=_by_email(path="/t-456"), request=None, user=_admin(), db=_DB())

    assert response.outcome == "invited"
    assert len(invitations["created"]) == 1
    assert invitations["reissued"] == ["hash-raw-0"]
    [pending] = invitations["pending"].values()
    assert pending["destination"] == {"platform": "opencrq", "path": "/t-456"}
    assert "register?token=raw-reissued-0" in sent["emails"][1]["text"]
    assert "(link valid for 7 days)" in sent["emails"][1]["text"]
    assert sent["audits"][1]["details"]["reused"] is True


@pytest.mark.parametrize(
    "made",
    [
        {"role": "admin", "destination": None},
        {"role": "user", "destination": None},
        {"role": "admin", "destination": {"platform": "opencrq", "path": "/t-1"}},
    ],
    ids=["admin", "user-from-the-invitations-page", "admin-with-a-destination"],
)
async def test_a_pending_invitation_no_notice_made_is_left_alone(sent, invitations, made):
    """An administrator's own invitation keeps its role and its link: 409, as ``POST /invitations`` answers."""
    pending = {"id": "hash-by-hand", "email": INVITEE, **made}
    invitations["pending"]["hash-by-hand"] = dict(pending)
    db = _DB()

    with pytest.raises(HTTPException) as exc:
        await send_access_notice(body=_by_email(), request=None, user=_admin(), db=db)

    assert exc.value.status_code == 409
    assert invitations["pending"] == {"hash-by-hand": pending}
    assert (invitations["created"], invitations["reissued"], sent["emails"], sent["audits"]) == ([], [], [], [])
    assert not db.committed


async def test_a_user_who_may_not_invite_is_refused_and_nothing_is_created(sent, invitations):
    db = _DB()
    with pytest.raises(HTTPException) as exc:
        await send_access_notice(body=_by_email(), request=None, user=_admin(is_admin=False), db=db)

    assert exc.value.status_code == 403
    assert (invitations["created"], invitations["reissued"], sent["emails"], sent["audits"]) == ([], [], [], [])
    assert not db.committed


@pytest.mark.parametrize(("may", "status"), [(True, 404), (False, 403)], ids=["inviter", "non-inviter"])
async def test_a_deactivated_account_named_by_email_is_never_invited(sent, invitations, may, status):
    """To a user who may not invite, it answers like an unknown email (the
    directory's search shows active accounts only); to one who may, the 404 an
    administrator's ``GET /users`` already implies."""
    with pytest.raises(HTTPException) as exc:
        await send_access_notice(
            body=_by_email(), request=None, user=_admin(is_admin=may), db=_DB(scalar=_recipient(is_active=False))
        )

    assert exc.value.status_code == status
    if not may:
        with pytest.raises(HTTPException) as unknown:
            await send_access_notice(body=_by_email(), request=None, user=_admin(is_admin=False), db=_DB())
        assert (unknown.value.status_code, unknown.value.detail) == (status, exc.value.detail)
    assert (invitations["created"], sent["emails"]) == ([], [])


async def test_without_an_email_server_an_invitation_answers_not_sent(sent, invitations, monkeypatch):
    monkeypatch.setattr(route, "send_email", email_service.send_email)
    monkeypatch.setattr(settings, "smtp_hostname", "")

    response = await send_access_notice(body=_by_email(), request=None, user=_admin(), db=_DB())

    assert (response.outcome, response.email_sent) == ("invited", False)
    assert response.link == "https://xtm-one.example.com/register?token=raw-0"


async def test_an_invitation_that_could_not_be_emailed_answers_its_newest_link(sent, invitations):
    """Only the hash is kept, so the answer is the one place the link survives; a reissue kills the previous one."""
    sent["email_result"] = False

    first = await send_access_notice(body=_by_email(), request=None, user=_admin(), db=_DB())
    again = await send_access_notice(body=_by_email(path="/t-456"), request=None, user=_admin(), db=_DB())

    assert first.link == "https://xtm-one.example.com/register?token=raw-0"
    assert again.link == "https://xtm-one.example.com/register?token=raw-reissued-0"
    assert list(invitations["pending"]) == ["hash-raw-reissued-0"]
    assert sent["audits"][1]["summary"].endswith("(pending invitation reused) (email not sent)")


def test_only_an_administrator_may_invite_today():
    assert may_invite(SimpleNamespace(is_admin=True))
    assert not may_invite(SimpleNamespace(is_admin=False))


@pytest.mark.parametrize("supplied", LINKS, ids=["url", "domain", "www", "address", "network-path"])
@pytest.mark.parametrize("field", ["scope_name", "invited_by"])
def test_the_invitation_shows_a_link_the_platform_supplies_as_text(field, supplied):
    values = {"scope_name": "Acme", "invited_by": "Claire Dubois"}
    values[field] = f"Acme {supplied}"
    html, text = platform_invitation_email(
        link="https://xtm-one.example.com/register?token=t", platform_name="OpenCRQ", expire_hours=168, **values
    )

    for body in (html, text):
        assert supplied not in body
        assert "evil.example" not in body
    assert "evil" not in "".join(re.findall(r'href="([^"]*)"', html))


def test_the_invitation_says_who_invited_whom_where():
    html, text = platform_invitation_email(
        link="https://xtm-one.example.com/register?token=t",
        platform_name="OpenCRQ",
        scope_name="Acme Corp",
        invited_by="Claire Dubois",
        expire_hours=168,
    )
    assert "<title>You&#x27;re invited to OpenCRQ</title>" in html
    assert "Create your account" in html
    assert "This link is valid for 7 days" in html
    assert text.startswith("Claire Dubois invited you to Acme Corp on OpenCRQ. Create your ")
    assert "https://xtm-one.example.com/register?token=t" in text


# -- where a person registered from an invitation goes ---------------------------


class _Registrations:
    def __init__(self, platform_url):
        self.platform_url = platform_url

    async def execute(self, _stmt):
        url = self.platform_url
        return SimpleNamespace(scalar_one_or_none=lambda: url)


async def test_a_registered_invitee_goes_to_the_path_on_the_platforms_public_url():
    destination = invitation_destination(PLATFORM, "/t-123?tab=risks")
    assert await registered_destination(_Registrations("https://OpenCRQ.example.com/"), destination) == (
        "https://opencrq.example.com/t-123?tab=risks"
    )


@pytest.mark.parametrize(
    ("platform_url", "destination"),
    [
        ("https://opencrq.example.com", None),
        (None, {"platform": "opencrq", "path": "/t-123"}),
        ("https://opencrq.example.com", {"platform": "opencrq", "path": "//evil.example"}),
        ("https://opencrq.example.com", {"platform": "opencrq", "path": "https://evil.example"}),
        ("https://opencrq.example.com", {"platform": 1, "path": "/"}),
    ],
    ids=["no-destination", "platform-gone", "protocol-relative", "absolute", "malformed"],
)
async def test_an_invitation_no_platform_made_or_whose_platform_is_gone_stays_in_xtm_one(platform_url, destination):
    assert await registered_destination(_Registrations(platform_url), destination) is None

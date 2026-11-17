"""A presentation's public link: anyone with the URL may view it, without an account.

What these pin, with the no-DB fakes of ``test_presentations.py``:

* the token: 256 bits, looked up by its SHA-256 and stored Fernet-encrypted,
  never in clear; a malformed token and the admin switch off answer without
  a query;
* what a link shows: the slides without their speaker notes, through a
  whitelist that names nobody (no owner, editor, grant or conversation);
* the ``set_presentation_public_link`` tool: it runs as the calling user with
  the Share dialog's rules - only a manager of the deck (an administrator on a
  company-managed one), refused while the switch is off (turning a link off
  never is) - writes one audit row per change and hands back the absolute URL;
* ``get_presentation`` says when a public link is on.

``tests/test_presentation_public_links_postgres.py`` drives the same flows
through PostgreSQL, the REST routes and an anonymous client.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.core.security import decrypt_value, hash_token
from app.services.presentation_public_links import (
    OUTCOME_CREATED,
    OUTCOME_DISABLED,
    OUTCOME_KEPT,
    OUTCOME_REGENERATED,
    PUBLIC_LINK_ROUTE,
    format_public_link_result,
    mint_token,
    normalize_public_link_action,
    public_link_path,
    public_link_status_line,
    public_spec,
    resolve_public_presentation,
    well_formed_token,
)
from tests.test_presentations import (
    _account,
    _audit_rows,
    _build_tools,
    _compiled,
    _deck_lookup,
    _FakeSession,
    _q3_deck,
    _Result,
    _run_tool,
    _session_factory,
)


@pytest.fixture
def links_off(monkeypatch):
    """The admin switch off (Settings > Access and security > Public presentation links)."""
    monkeypatch.setattr(settings, "presentation_public_links_enabled", False)


@pytest.fixture
def fake_session(monkeypatch):
    """Install a fake session for the tool's own ``async_session()``."""
    import app.core.database as database

    def _install(results) -> _FakeSession:
        session = _FakeSession(results=results)
        monkeypatch.setattr(database, "async_session", _session_factory(session))
        return session

    return _install


def _link(executor, deck, **arguments):
    return _run_tool(executor, "set_presentation_public_link", {"presentation_id": str(deck.id), **arguments})


# ── The token ────────────────────────────────────────────────────────────────


def test_a_token_carries_256_bits_and_the_page_address_carries_it():
    tokens = {mint_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(well_formed_token(token) and len(token) == 43 for token in tokens)
    token = tokens.pop()
    assert public_link_path(token) == f"{PUBLIC_LINK_ROUTE}/{token}" == f"/public/presentations/{token}"


@pytest.mark.parametrize(
    "token",
    ["", "short", "x" * 44, "../../etc/passwd" + "a" * 27, "a b" + "c" * 40, None, 42],
)
def test_a_malformed_token_is_refused_without_a_query(token):
    import asyncio

    session = _FakeSession()
    assert not well_formed_token(token)
    assert asyncio.run(resolve_public_presentation(session, token)) is None
    assert session.statements == []


def test_a_public_request_is_looked_up_by_the_token_hash_never_the_token():
    import asyncio

    token = mint_token()
    session = _FakeSession(results=[_Result(one=None)])
    assert asyncio.run(resolve_public_presentation(session, token)) is None
    sql = _compiled(session.statements[0])
    assert hash_token(token) in sql
    assert token not in sql
    assert "presentation_public_links" in sql


def test_the_switch_off_answers_every_link_like_an_unknown_one_without_a_query(links_off):
    import asyncio

    session = _FakeSession()
    assert asyncio.run(resolve_public_presentation(session, mint_token())) is None
    assert session.statements == []


# ── What a link shows ────────────────────────────────────────────────────────


def test_the_public_spec_drops_every_speaker_note_and_keeps_the_slides():
    spec = {
        "title": "Q3",
        "theme": "dark",
        "slides": [
            {"id": "s1", "layout": "title", "title": "Q3 2026", "notes": "Say the number slowly."},
            {"id": "s2", "layout": "content", "title": "Pipeline", "blocks": [{"type": "list", "items": ["x"]}]},
        ],
    }
    shown = public_spec(spec)
    assert [slide.get("notes") for slide in shown["slides"]] == [None, None]
    assert shown["slides"][0] == {"id": "s1", "layout": "title", "title": "Q3 2026"}
    assert shown["slides"][1] == spec["slides"][1]
    # The stored spec is left alone.
    assert spec["slides"][0]["notes"] == "Say the number slowly."


def test_the_public_response_is_a_whitelist_that_names_nobody():
    from app.api.v1.schemas.presentation import PresentationResponse, PublicPresentationResponse

    public = set(PublicPresentationResponse.model_fields)
    for private in (
        "user_id",
        "owner_name",
        "shared_user_ids",
        "shared_user_permissions",
        "shared_group_ids",
        "shared_group_permissions",
        "is_company_managed",
        "access_restricted",
        "permission",
        "can_manage",
        "conversation_id",
        "assignment_run_id",
        "template_container_id",
        "template_name",
        "updated_by_user_id",
        "updated_by_name",
        "open_comment_count",
        "has_public_link",
    ):
        assert private in PresentationResponse.model_fields, private
        assert private not in public, private
    assert {"id", "title", "spec", "theme_config", "asset_meta", "template_chrome"} <= public


def test_the_payload_shows_the_deck_as_its_owner_reads_it_without_notes(monkeypatch):
    import asyncio

    from app.api.v1.schemas.presentation import PublicPresentationResponse
    from app.services import presentation_public_links as links
    from app.services import presentations as decks

    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner, shared_user_ids=[str(uuid.uuid4())], template_container_id=uuid.uuid4())
    deck.spec = {**deck.spec, "slides": [{**slide, "notes": "secret"} for slide in deck.spec["slides"]]}
    seen: dict = {}

    async def _palette(_db, container_id, *, user_id, **_kw):
        seen["palette"] = (container_id, user_id)
        return decks.TemplatePalette(themes={"dark": {"accent": "#00f"}, "light": {}}, name="ACME internal brand")

    async def _no_meta(_db, _spec):
        return {}

    monkeypatch.setattr(decks, "resolve_template_palette", _palette)
    monkeypatch.setattr(decks, "load_logo_asset_meta", _no_meta)

    payload = PublicPresentationResponse(**asyncio.run(links.public_presentation_payload(_FakeSession(), deck)))
    # Resolved as the owner reads it: the template the owner sees dresses it.
    assert seen["palette"] == (deck.template_container_id, owner.id)
    assert payload.theme_config == {"accent": "#00f"}
    assert all("notes" not in slide for slide in payload.spec["slides"])
    dumped = payload.model_dump()
    assert "ACME internal brand" not in str(dumped)
    assert str(owner.id) not in str(dumped)


# ── The agent tool ───────────────────────────────────────────────────────────


def test_the_owner_turns_the_link_on_and_gets_the_absolute_url(fake_session, monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", "https://xtm.example.com")
    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner)
    session = fake_session([*_deck_lookup(deck), _Result(one=owner), _Result(one=uuid.uuid4())])

    _defs, executor = _build_tools(str(owner.id))
    result = _link(executor, deck)

    assert not result.is_error, result.content
    assert session.committed
    insert = _compiled(session.statements[3])
    assert "INSERT INTO presentation_public_links" in insert
    assert "ON CONFLICT (presentation_id) DO NOTHING" in insert
    # The token is in the URL handed back, and nowhere in the database in clear.
    url = result.content.split(": ", 1)[1].splitlines()[0]
    assert url.startswith("https://xtm.example.com/public/presentations/")
    token = url.rsplit("/", 1)[1]
    assert well_formed_token(token)
    assert token not in insert
    assert hash_token(token) in insert
    assert "Anyone with this link can view the presentation without signing in" in result.content
    audits = _audit_rows(session)
    assert len(audits) == 1
    assert audits[0].details == {"public_link": OUTCOME_CREATED, "sharing_changed": True, "via": "agent"}
    assert audits[0].entity_id == deck.id


def test_enabling_a_link_the_deck_has_hands_it_back_unchanged_and_audits_nothing(fake_session):
    from app.core.security import encrypt_value
    from app.models.presentation import PresentationPublicLink

    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner)
    token = mint_token()
    stored = PresentationPublicLink(
        presentation_id=deck.id, token_hash=hash_token(token), token_enc=encrypt_value(token)
    )
    session = fake_session([*_deck_lookup(deck), _Result(one=owner), _Result(one=None), _Result(one=stored)])

    _defs, executor = _build_tools(str(owner.id))
    result = _link(executor, deck, action="enable")

    assert not result.is_error, result.content
    assert "already has a public link, unchanged" in result.content
    assert token in result.content
    assert _audit_rows(session) == []


def test_regenerate_rotates_the_token_in_place(fake_session):
    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner)
    session = fake_session([*_deck_lookup(deck), _Result(one=owner), _Result(one=uuid.uuid4())])

    _defs, executor = _build_tools(str(owner.id))
    result = _link(executor, deck, action="regenerate")

    assert not result.is_error, result.content
    rotate = _compiled(session.statements[3])
    assert rotate.startswith("UPDATE presentation_public_links SET token_hash=")
    assert "token_enc=" in rotate
    assert "The previous link no longer works" in result.content
    assert _audit_rows(session)[0].details["public_link"] == OUTCOME_REGENERATED


def test_disable_turns_the_link_off_and_never_needs_the_switch(fake_session, links_off):
    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner)
    session = fake_session([*_deck_lookup(deck), _Result(one=owner), _Result(one=uuid.uuid4())])

    _defs, executor = _build_tools(str(owner.id))
    result = _link(executor, deck, action="off")

    assert not result.is_error, result.content
    assert _compiled(session.statements[3]).startswith("DELETE FROM presentation_public_links")
    assert "turned off: the link no longer opens it, for anyone" in result.content
    assert _audit_rows(session)[0].details["public_link"] == OUTCOME_DISABLED


def test_disabling_a_deck_with_no_link_writes_nothing(fake_session):
    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner)
    session = fake_session([*_deck_lookup(deck), _Result(one=owner), _Result(one=None)])

    _defs, executor = _build_tools(str(owner.id))
    result = _link(executor, deck, action="disable")

    assert "has no public link: nothing to turn off" in result.content
    assert _audit_rows(session) == []
    assert not session.committed


def test_the_switch_off_refuses_a_new_link_before_any_query(fake_session, links_off):
    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner)
    session = fake_session([])

    _defs, executor = _build_tools(str(owner.id))
    for action in ("enable", "regenerate"):
        result = _link(executor, deck, action=action)
        assert "turned off on this platform by an administrator" in result.content
        assert "Nothing was changed" in result.content
    assert session.statements == []


def test_a_read_only_grantee_and_a_stranger_cannot_publish_the_deck(fake_session):
    owner = _account("samuel@filigran.io")
    reader = _account("ada@filigran.io")
    deck = _q3_deck(owner, shared_user_ids=[str(reader.id)], shared_user_permissions={str(reader.id): "read_only"})

    session = fake_session(_deck_lookup(deck))
    _defs, executor = _build_tools(str(reader.id))
    refused = _link(executor, deck)
    assert "but not share it" in refused.content
    assert len(session.statements) == 2  # groups, deck: nothing written
    assert not session.committed

    session = fake_session(_deck_lookup(None))
    _defs, executor = _build_tools(str(uuid.uuid4()))
    unknown = _link(executor, deck)
    assert f"no presentation `{deck.id}` you can access was found" in unknown.content
    assert not session.committed


def test_an_editor_may_publish_the_deck(fake_session):
    owner = _account("samuel@filigran.io")
    editor = _account("ada@filigran.io")
    deck = _q3_deck(owner, shared_user_ids=[str(editor.id)], shared_user_permissions={str(editor.id): "config"})
    session = fake_session([*_deck_lookup(deck), _Result(one=editor), _Result(one=uuid.uuid4())])

    _defs, executor = _build_tools(str(editor.id))
    result = _link(executor, deck)

    assert not result.is_error, result.content
    assert str(editor.id) in _compiled(session.statements[3])  # created_by_user_id
    assert _audit_rows(session)[0].user_id == editor.id


def _published_deck():
    admin = _account("admin@filigran.io")
    admin.role, admin.is_admin = "admin", True
    deck = _q3_deck(admin, is_company_managed=True)
    deck.user_id = None
    return admin, deck


@pytest.mark.parametrize(
    ("action", "sql_start", "outcome"),
    [
        ("enable", "INSERT INTO presentation_public_links", OUTCOME_CREATED),
        ("regenerate", "UPDATE presentation_public_links SET token_hash=", OUTCOME_REGENERATED),
        ("disable", "DELETE FROM presentation_public_links", OUTCOME_DISABLED),
    ],
)
def test_an_admin_manages_the_link_of_a_company_managed_deck_like_the_share_dialog(
    fake_session, action, sql_start, outcome
):
    admin, deck = _published_deck()
    # The ``User.role`` read ``agent_deck_permission`` makes on a published deck
    # the caller does not own, then the actor, then the link write.
    session = fake_session([*_deck_lookup(deck), _Result(one="admin"), _Result(one=admin), _Result(one=uuid.uuid4())])

    _defs, executor = _build_tools(str(admin.id))
    result = _link(executor, deck, action=action)

    assert not result.is_error, result.content
    assert _compiled(session.statements[4]).startswith(sql_start)
    assert session.committed
    audits = _audit_rows(session)
    assert len(audits) == 1
    assert audits[0].user_id == admin.id
    assert audits[0].details["public_link"] == outcome
    assert audits[0].details["via"] == "agent"


def test_a_member_is_refused_the_link_of_a_company_managed_deck_and_told_who_can(fake_session):
    _admin, deck = _published_deck()
    member = _account("ada@filigran.io")
    session = fake_session([*_deck_lookup(deck), _Result(one="user")])

    _defs, executor = _build_tools(str(member.id))
    result = _link(executor, deck)

    assert "company-managed" in result.content
    assert "only an administrator" in result.content and "Share dialog" in result.content
    assert "Nothing was changed" in result.content
    assert len(session.statements) == 3  # groups, deck, role: nothing written
    assert not session.committed
    assert _audit_rows(session) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "enable"),
        ("", "enable"),
        ("ON", "enable"),
        ("publish", "enable"),
        ("rotate", "regenerate"),
        ("Regenerate", "regenerate"),
        ("revoke", "disable"),
        ("turn-off", None),
        ("delete", "disable"),
        ("make it public", None),
    ],
)
def test_actions_read_as_people_say_them(value, expected):
    assert normalize_public_link_action(value) == expected


def test_an_unknown_action_is_refused_before_any_query(fake_session):
    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner)
    session = fake_session([])
    _defs, executor = _build_tools(str(owner.id))
    result = _link(executor, deck, action="forever")
    assert "action must be one of enable, regenerate, disable" in result.content
    assert session.statements == []


def test_the_tool_is_offered_with_the_deck_suite_and_asks_for_the_user():
    defs, executor = _build_tools()
    tool = next(d for d in defs if d.name == "set_presentation_public_link")
    assert tool.source == "builtin:generate_presentation"
    assert "set_presentation_public_link" in executor.tool_names
    assert "ONLY when the user asks" in tool.description
    assert "say in your reply that anyone with the link can view it" in tool.description
    assert tool.input_schema["properties"]["action"]["enum"] == ["enable", "regenerate", "disable"]
    assert tool.input_schema["required"] == ["presentation_id"]
    share = next(d for d in defs if d.name == "share_presentation")
    assert "set_presentation_public_link" in share.description


def test_a_kept_link_whose_key_changed_asks_for_a_new_one():
    message = format_public_link_result("Q3", OUTCOME_KEPT, None)
    assert "regenerate" in message
    assert "/public/presentations/" not in message


# ── get_presentation ─────────────────────────────────────────────────────────


def test_get_presentation_says_a_public_link_is_on(fake_session):
    from app.models.presentation import PresentationPublicLink

    owner = _account("samuel@filigran.io")
    deck = _q3_deck(owner)
    link = PresentationPublicLink(presentation_id=deck.id, token_hash="h", token_enc="e")
    fake_session([*_deck_lookup(deck), _Result(rows=[]), _Result(one=link)])

    _defs, executor = _build_tools(str(owner.id))
    content = _run_tool(executor, "get_presentation", {"presentation_id": str(deck.id)}).content

    assert "Shared with: nobody - only its owner can open it." in content
    assert "Public link: on - anyone with the link can view it without signing in (no PDF download)." in content


def test_the_status_line_says_a_kept_link_works_no_longer_while_the_switch_is_off(links_off):
    assert public_link_status_line(has_link=False) == ""
    assert "turned off on this platform" in public_link_status_line(has_link=True)


def test_the_stored_token_decrypts_to_the_one_handed_out():
    from app.core.security import encrypt_value
    from app.models.presentation import PresentationPublicLink
    from app.services.presentation_public_links import link_token

    token = mint_token()
    link = PresentationPublicLink(presentation_id=uuid.uuid4(), token_hash=hash_token(token), token_enc=encrypt_value(token))
    assert link.token_enc != token
    assert decrypt_value(link.token_enc) == token == link_token(link)
    # A key that no longer decrypts it: the link is not shown, and nothing raises.
    link.token_enc = "gAAAAA-not-a-fernet-token"
    assert link_token(link) is None

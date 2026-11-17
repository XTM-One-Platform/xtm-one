"""A file the caller may not read is answered exactly like an unknown id.

Before, the file and image routes answered 403 for an id that exists but that
the caller may not read and 404 for an id nobody issued, so comparing the two
told any signed-in user which file ids exist. Every route that serves or acts
on one stored file now loads it through ``load_readable_file()`` /
``load_readable_chat_image()``, whose single refusal is the unknown-id 404.

These tests run as a NON-admin: an admin reads every file, so an admin-only
check would pass whatever the refusal looks like.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import ai as ai_api
from app.api.v1 import chat as chat_api
from app.services import file_access
from app.services.file_access import (
    FILE_NOT_FOUND_DETAIL,
    IMAGE_NOT_FOUND_DETAIL,
    authorize_file_access,
    can_read_chat_image,
    load_readable_file,
)


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _Session:
    """Answers every ``execute`` with the one row it was given (or ``None``)."""

    def __init__(self, row=None):
        self._row = row
        self.executed = 0

    async def execute(self, _statement):
        self.executed += 1
        return _Result(self._row)


def _user(role: str = "user"):
    return SimpleNamespace(id=uuid.uuid4(), role=role, is_admin=role == "admin", email="u@example.test")


def _file(owner_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        conversation_id=None,
        assignment_run_id=None,
        template_container_id=None,
        source="upload",
        filename="report.pdf",
        content_type="application/pdf",
        size=42,
        s3_object_name="obj",
        created_at=datetime.now(UTC),
    )


def _refusal(coro) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro)
    return exc.value


def _shape(exc: HTTPException) -> tuple:
    return (exc.status_code, exc.detail, exc.headers)


@pytest.fixture
def unreadable(monkeypatch):
    """Make the access decision refuse, whoever asks (the rule itself is pinned elsewhere)."""

    async def _no(*_args, **_kwargs):
        return False

    monkeypatch.setattr(file_access, "can_read_file", _no)


@pytest.mark.parametrize("route", ["download_file", "get_file_info"])
def test_id_scoped_file_routes_answer_an_unreadable_file_like_an_unknown_one(unreadable, route):
    handler = getattr(chat_api, route)
    user = _user()
    unknown = _refusal(handler(str(uuid.uuid4()), user=user, db=_Session(None)))
    denied = _refusal(handler(str(uuid.uuid4()), user=user, db=_Session(_file(owner_id=uuid.uuid4()))))
    malformed = _refusal(handler("not-a-uuid", user=user, db=_Session(None)))

    assert _shape(denied) == _shape(unknown) == _shape(malformed)
    assert unknown.status_code == 404 and unknown.detail == FILE_NOT_FOUND_DETAIL


def test_file_info_still_serves_a_file_the_caller_owns():
    user = _user()
    row = _file(owner_id=user.id)
    body = asyncio.run(chat_api.get_file_info(str(row.id), user=user, db=_Session(row)))
    assert body["id"] == str(row.id) and body["filename"] == "report.pdf"


def test_an_admin_still_reads_any_file():
    admin = _user("admin")
    row = _file(owner_id=uuid.uuid4())
    body = asyncio.run(chat_api.get_file_info(str(row.id), user=admin, db=_Session(row)))
    assert body["id"] == str(row.id)


def test_chat_image_route_answers_another_user_s_image_like_an_unknown_one():
    user = _user()
    foreign = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), image_data=b"x", content_type="image/png")
    unknown = _refusal(chat_api.get_chat_image(str(uuid.uuid4()), user=user, db=_Session(None)))
    denied = _refusal(chat_api.get_chat_image(str(foreign.id), user=user, db=_Session(foreign)))
    malformed = _refusal(chat_api.get_chat_image("nope", user=user, db=_Session(None)))

    assert _shape(denied) == _shape(unknown) == _shape(malformed)
    assert unknown.status_code == 404 and unknown.detail == IMAGE_NOT_FOUND_DETAIL


def test_chat_image_route_still_serves_its_owner_and_an_admin():
    owner = _user()
    image = SimpleNamespace(id=uuid.uuid4(), user_id=owner.id, image_data=b"png", content_type="image/png")
    assert asyncio.run(chat_api.get_chat_image(str(image.id), user=owner, db=_Session(image))).body == b"png"
    assert asyncio.run(chat_api.get_chat_image(str(image.id), user=_user("admin"), db=_Session(image))).body == b"png"


def test_chat_image_read_rule_scopes_an_owner_less_image_to_its_conversation(monkeypatch):
    """An owner-less image was readable by any signed-in user. It now
    follows ``can_read_file``'s conversation rule; its conversation's readers
    and admins keep it, a row of no conversation is admins-only."""
    owner, other, admin = _user(), _user(), _user("admin")
    conversation = SimpleNamespace(id=uuid.uuid4())
    readers = {owner.id}

    async def _access(_db, user, conv):
        assert conv is conversation
        return user.id in readers

    monkeypatch.setattr(chat_api, "conversation_access", _access)

    def _can(image, user, row=None):
        return asyncio.run(can_read_chat_image(image, user, _Session(row)))

    assert _can(SimpleNamespace(user_id=owner.id, conversation_id=None), owner)
    assert _can(SimpleNamespace(user_id=owner.id, conversation_id=None), admin)
    assert not _can(SimpleNamespace(user_id=owner.id, conversation_id=None), other)

    in_conversation = SimpleNamespace(user_id=None, conversation_id=conversation.id)
    assert _can(in_conversation, owner, conversation)
    assert not _can(in_conversation, other, conversation)
    assert _can(in_conversation, admin, conversation)

    nowhere = SimpleNamespace(user_id=None, conversation_id=None)
    assert not _can(nowhere, other)
    assert _can(nowhere, admin)


def test_a_file_of_a_conversation_is_read_by_the_conversation_s_readers_whoever_uploaded_it(monkeypatch):
    """The chat panel downloads every attachment through the id-scoped route: a
    reader of a shared conversation downloads the owner's files as the
    message-scoped route lets them, and no one else does."""
    owner, reader, other = _user(), _user(), _user()
    conversation = SimpleNamespace(id=uuid.uuid4())
    readers = {owner.id, reader.id}

    async def _access(_db, user, conv):
        assert conv is conversation
        return user.id in readers

    async def _no_deck(*_args, **_kwargs):
        return False

    async def _no_groups(_db, _user_id):
        return []

    import app.api.v1.helpers as helpers

    monkeypatch.setattr(chat_api, "conversation_access", _access)
    monkeypatch.setattr(file_access, "_readable_presentation_names_file", _no_deck)
    monkeypatch.setattr(helpers, "get_user_group_ids", _no_groups)

    def _can(file_att, user):
        return asyncio.run(file_access.can_read_file(file_att, user, _Session(conversation)))

    uploaded = SimpleNamespace(id=uuid.uuid4(), user_id=owner.id, conversation_id=conversation.id, assignment_run_id=None)
    assert _can(uploaded, owner)
    assert _can(uploaded, reader)
    assert not _can(uploaded, other)
    # A file of no conversation stays its owner's.
    private = SimpleNamespace(id=uuid.uuid4(), user_id=owner.id, conversation_id=None, assignment_run_id=None)
    assert not _can(private, reader)
    # An internal file (a checkpoint spill) stays its owner's: the transcript shows it cut.
    spill = SimpleNamespace(
        id=uuid.uuid4(), user_id=owner.id, conversation_id=conversation.id, assignment_run_id=None, source="agent_checkpoint"
    )
    assert _can(spill, owner)
    assert not _can(spill, reader)


def test_an_image_of_a_conversation_is_read_by_the_conversation_s_readers_whoever_made_it(monkeypatch):
    owner, reader, other = _user(), _user(), _user()
    conversation = SimpleNamespace(id=uuid.uuid4())

    async def _access(_db, user, conv):
        return user.id in {owner.id, reader.id}

    monkeypatch.setattr(chat_api, "conversation_access", _access)
    image = SimpleNamespace(user_id=owner.id, conversation_id=conversation.id)
    assert asyncio.run(can_read_chat_image(image, reader, _Session(conversation)))
    assert not asyncio.run(can_read_chat_image(image, other, _Session(conversation)))


def test_authorize_file_access_refuses_with_the_unknown_file_answer(unreadable):
    user = _user()
    denied = _refusal(authorize_file_access(_file(owner_id=uuid.uuid4()), user, _Session()))
    unknown = _refusal(load_readable_file(_Session(None), uuid.uuid4(), user))
    assert _shape(denied) == _shape(unknown)
    assert denied.status_code == 404


def test_load_readable_file_passes_the_deck_hint_to_the_rule(monkeypatch):
    seen = {}

    async def _record(row, user, db, *, presentation_id=None):
        seen["presentation_id"] = presentation_id
        return True

    monkeypatch.setattr(file_access, "can_read_file", _record)
    deck_id = uuid.uuid4()
    row = _file()
    assert asyncio.run(load_readable_file(_Session(row), row.id, _user(), presentation_id=deck_id)) is row
    assert seen == {"presentation_id": deck_id}


def test_template_brief_logo_that_cannot_be_read_gets_the_not_found_warning(unreadable):
    user = _user()
    unknown_warnings: list[str] = []
    denied_warnings: list[str] = []
    assert asyncio.run(ai_api._logo_palette(str(uuid.uuid4()), user, _Session(None), unknown_warnings)) == []
    denied_row = _file(owner_id=uuid.uuid4())
    assert asyncio.run(ai_api._logo_palette(str(denied_row.id), user, _Session(denied_row), denied_warnings)) == []
    assert denied_warnings == unknown_warnings == ["The logo file was not found; it was ignored."]


def test_no_route_answers_a_file_refusal_itself():
    """Tripwire: a route never calls ``authorize_file_access`` directly (it
    would have to re-spell the unknown-id answer next to it); it loads the file
    through ``load_readable_file`` or asks ``can_read_file``."""
    api_dir = pathlib.Path(__file__).resolve().parents[1] / "app" / "api"
    offenders = []
    for path in api_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name in {"authorize_file_access", "_authorize_file_access"}:
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []

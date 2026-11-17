"""Who may keep a file's bytes, and for how long.

The FileAttachment byte routes answer through ``app.services.file_responses``:
``private`` always, a strong ``ETag`` derived from the row, ``no-cache`` for a
template asset (replaced in place under the same id) and a day otherwise, and a
``304`` on a matching ``If-None-Match`` - after authorisation, before storage.
The branding asset is the one public family, versioned by ``?v=``.
"""

from __future__ import annotations

import ast
import inspect
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core.utils import if_none_match_hits
from app.models.template_container import TEMPLATE_ASSET_SOURCE
from app.services import file_responses
from app.services.file_responses import (
    IMMUTABLE_FILE_MAX_AGE,
    file_cache_control,
    file_etag,
    file_not_modified,
    file_response_headers,
    file_version,
    token_max_age,
)
from tests import app_sources

BACKEND_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = BACKEND_ROOT / "app"


# ── Stubs ─────────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return [] if self._value is None else [self._value]


class _QueueDB:
    """Each ``execute`` answers the next queued row; an empty queue reads as no row."""

    def __init__(self, rows):
        self._rows = list(rows)

    async def execute(self, _statement):
        return _Result(self._rows.pop(0) if self._rows else None)


def _file(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=None,
        message_id=None,
        assignment_run_id=None,
        template_container_id=None,
        source="chat",
        filename="figure.png",
        content_type="image/png",
        size=4,
        s3_object_name=f"generated/u/{uuid.uuid4().hex}.png",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _template_asset(**overrides) -> SimpleNamespace:
    base = dict(
        source=TEMPLATE_ASSET_SOURCE,
        template_container_id=uuid.uuid4(),
        filename="logo.png",
        s3_object_name=f"template-asset/c/{uuid.uuid4().hex}.png",
    )
    base.update(overrides)
    return _file(**base)


@pytest.fixture
def storage(monkeypatch):
    """Object store double: records every read, serves bytes by object key."""
    from app.services import file_storage as file_storage_module

    blobs: dict[str, bytes] = {}
    reads: list[str] = []

    async def _download(key):
        reads.append(key)
        return blobs.get(key, b"bytes")

    monkeypatch.setattr(file_storage_module.file_storage, "download", _download)
    return SimpleNamespace(blobs=blobs, reads=reads)


@pytest.fixture
def signed(monkeypatch):
    """Let every signed-link token through (the token gate has its own tests)."""
    import app.core.security as security_module

    monkeypatch.setattr(security_module, "verify_image_token", lambda *_a, **_k: True)


# ── If-None-Match ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("header", "hits"),
    [
        ('"abc"', True),
        ('W/"abc"', True),
        ('"zzz", "abc"', True),
        ('"zzz",W/"abc"', True),
        ("*", True),
        ('"abcd"', False),
        ('"ab"', False),
        ('"zzz"', False),
        ("abc", False),
        ("", False),
        (None, False),
    ],
)
def test_if_none_match_uses_the_weak_comparison_of_rfc_9110(header, hits):
    assert if_none_match_hits(header, '"abc"') is hits
    assert if_none_match_hits(header, 'W/"abc"') is hits


# ── The validator ─────────────────────────────────────────────────────────────


def test_the_etag_is_strong_stable_and_never_carries_the_object_key():
    row = _file()
    etag = file_etag(row)
    assert etag.startswith('"') and etag.endswith('"') and not etag.startswith("W/")
    assert file_etag(row) == etag
    assert row.s3_object_name not in etag
    assert file_version(row) == etag.strip('"')


def test_the_etag_follows_the_bytes_not_the_id():
    """An in-place replace keeps the id and writes a new object key: new ETag.

    Same id, same name, same type, same size - only the key moved, exactly what
    ``replace_template_asset`` and the seed's refresh do.
    """
    row = _template_asset()
    before = file_etag(row)
    row.s3_object_name = f"template-asset/c/{uuid.uuid4().hex}.png"
    assert file_etag(row) != before


def test_the_asset_version_and_the_logo_rev_move_together_on_a_replace():
    """Two URL versions name a template asset's bytes: ``version`` (the Templates
    page thumbnail) and ``image_meta.rev`` (a logo wall). Both are
    digests of the object key, so the replace that moves the key moves both -
    neither surface can keep painting the logo the replace retired."""
    from app.services.template_containers import serialize_asset

    row = _template_asset(
        image_meta={"v": 1, "w": 40, "h": 20, "trim": [0.0, 0.0, 1.0, 1.0]},
        asset_role="logo",
        description=None,
        created_at=None,
    )
    before = serialize_asset(row)
    row.s3_object_name = f"template-asset/c/{uuid.uuid4().hex}.png"
    after = serialize_asset(row)

    assert before["version"] != after["version"]
    assert before["image_meta"]["rev"] != after["image_meta"]["rev"]
    assert row.s3_object_name not in (after["version"], after["image_meta"]["rev"])


@pytest.mark.parametrize("field", ["content_type", "filename"])
def test_the_etag_follows_the_headers_built_from_the_row(field):
    row = _file()
    before = file_etag(row)
    setattr(row, field, "text/plain" if field == "content_type" else "renamed.png")
    assert file_etag(row) != before


# ── The lifetime ──────────────────────────────────────────────────────────────


def test_a_template_asset_is_revalidated_and_every_other_file_kept_a_day():
    assert file_cache_control(_template_asset()) == "private, no-cache"
    assert file_cache_control(_file()) == f"private, max-age={IMMUTABLE_FILE_MAX_AGE}"
    assert file_cache_control(_file(), max_age=42) == "private, max-age=42"
    # A signed link on a template asset is still revalidated, whatever the token.
    assert file_cache_control(_template_asset(), max_age=42) == "private, no-cache"


def test_a_signed_link_is_never_kept_past_its_token():
    now = 1_000_000
    assert token_max_age(now + 120, now=now) == 120
    assert token_max_age(now + 10 * IMMUTABLE_FILE_MAX_AGE, now=now) == IMMUTABLE_FILE_MAX_AGE
    assert token_max_age(now - 5, now=now) == 0


def test_the_304_repeats_the_validator_and_the_lifetime():
    row = _template_asset()
    response = file_not_modified(row, file_etag(row))
    assert response is not None
    assert response.status_code == 304
    assert response.body == b""
    assert response.headers["etag"] == file_etag(row)
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["vary"] == "Authorization"
    assert file_not_modified(row, '"something-else"') is None
    assert file_not_modified(row, None) is None


def test_an_answer_no_cache_may_keep_carries_no_validator():
    """``store=False`` (the render-token fetch behind the export): ``private,
    no-store`` whatever the row, and no ``ETag`` or ``Vary`` - nothing is stored,
    so nothing is ever revalidated - with the disposition and ``nosniff`` every
    other file answer carries."""
    svg = _file(content_type="image/svg+xml", filename="mark.svg")
    for row in (_file(), _template_asset(), svg):
        headers = file_response_headers(row, store=False)
        assert headers["Cache-Control"] == "private, no-store"
        assert "ETag" not in headers and "Vary" not in headers
        assert headers["X-Content-Type-Options"] == "nosniff"
    assert file_response_headers(_file(), store=False)["Content-Disposition"] == "inline"
    assert file_response_headers(svg, store=False)["Content-Disposition"].startswith("attachment;")


# ── GET /chat/files/{id}/download ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "cache_control"),
    [
        (_file(), f"private, max-age={IMMUTABLE_FILE_MAX_AGE}"),
        (_template_asset(), "private, no-cache"),
    ],
    ids=["written-once", "template-asset"],
)
async def test_the_session_download_is_private_with_a_validator(storage, row, cache_control):
    from app.api.v1 import chat as chat_api

    user = SimpleNamespace(id=row.user_id, role="user")
    response = await chat_api.download_file(file_id=str(row.id), user=user, db=_QueueDB([row]))

    assert response.status_code == 200
    assert response.headers["cache-control"] == cache_control
    assert "public" not in response.headers["cache-control"]
    assert response.headers["etag"] == file_etag(row)
    assert response.headers["x-content-type-options"] == "nosniff"
    # A second person signed in to the same browser profile never gets this copy
    # without the server authorising them first.
    assert response.headers["vary"] == "Authorization"


@pytest.mark.asyncio
async def test_the_session_download_answers_304_without_reading_storage(storage):
    from app.api.v1 import chat as chat_api

    row = _template_asset()
    user = SimpleNamespace(id=row.user_id, role="user")
    response = await chat_api.download_file(
        file_id=str(row.id), if_none_match=file_etag(row), user=user, db=_QueueDB([row])
    )

    assert response.status_code == 304
    assert response.headers["etag"] == file_etag(row)
    assert storage.reads == []


@pytest.mark.asyncio
async def test_the_304_is_never_a_way_around_the_authorisation(storage):
    """A reader who lost access (or never had it) and replays the ETag gets the refusal,
    the unknown-file 404."""
    from app.api.v1 import chat as chat_api

    row = _file()
    stranger = SimpleNamespace(id=uuid.uuid4(), role="user")
    with pytest.raises(HTTPException) as exc_info:
        await chat_api.download_file(
            file_id=str(row.id), if_none_match=file_etag(row), user=stranger, db=_QueueDB([row])
        )

    assert (exc_info.value.status_code, exc_info.value.detail) == (404, "File not found")
    assert storage.reads == []


@pytest.mark.asyncio
async def test_a_template_asset_replaced_in_place_is_served_fresh_on_the_next_request(storage):
    """The id route a deck paints its pictures through: the ETag a browser held
    before the replace is a ``200`` with the new bytes afterwards, never a ``304``."""
    from app.api.v1 import chat as chat_api

    row = _template_asset()
    user = SimpleNamespace(id=row.user_id, role="user")
    storage.blobs[row.s3_object_name] = b"old-logo"
    first = await chat_api.download_file(file_id=str(row.id), user=user, db=_QueueDB([row]))
    held = first.headers["etag"]
    assert first.body == b"old-logo"

    row.s3_object_name = f"template-asset/c/{uuid.uuid4().hex}.png"
    storage.blobs[row.s3_object_name] = b"new-logo"
    after = await chat_api.download_file(
        file_id=str(row.id), if_none_match=held, user=user, db=_QueueDB([row])
    )

    assert after.status_code == 200
    assert after.body == b"new-logo"
    assert after.headers["etag"] != held
    assert after.headers["cache-control"] == "private, no-cache"


# ── GET /chat/conversations/{cid}/messages/{mid}/files/{fid}/download ─────────


@pytest.mark.asyncio
async def test_the_message_scoped_download_follows_the_same_contract(storage):
    from app.api.v1 import chat as chat_api

    owner = uuid.uuid4()
    conv = SimpleNamespace(id=uuid.uuid4(), user_id=owner)
    msg = SimpleNamespace(id=uuid.uuid4(), conversation_id=conv.id)
    row = _file(user_id=owner, conversation_id=conv.id, message_id=msg.id)
    user = SimpleNamespace(id=owner, role="user")

    response = await chat_api.download_message_file(
        conversation_id=str(conv.id),
        message_id=str(msg.id),
        file_id=str(row.id),
        user=user,
        db=_QueueDB([conv, msg, row]),
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == f"private, max-age={IMMUTABLE_FILE_MAX_AGE}"
    assert response.headers["etag"] == file_etag(row)

    not_modified = await chat_api.download_message_file(
        conversation_id=str(conv.id),
        message_id=str(msg.id),
        file_id=str(row.id),
        if_none_match=response.headers["etag"],
        user=user,
        db=_QueueDB([conv, msg, row]),
    )
    assert not_modified.status_code == 304
    assert storage.reads == [row.s3_object_name]


# ── Signed links: GET /chat/files/{id}/public, GET /chat/images/{id}/public ───


@pytest.mark.asyncio
async def test_a_signed_file_link_is_private_and_never_outlives_its_token(storage, signed):
    """The token is the only authorisation: a shared cache honouring ``public``
    kept serving the file for up to a day after the token (24 h) expired."""
    from app.api.v1 import chat as chat_api

    row = _file()
    expires = int(time.time()) + 120
    response = await chat_api.get_file_public(
        file_id=str(row.id), token="tok", expires=expires, db=_QueueDB([row])
    )

    cache_control = response.headers["cache-control"]
    assert "public" not in cache_control
    assert cache_control.startswith("private, max-age=")
    assert 100 <= int(cache_control.rsplit("=", 1)[1]) <= 120
    assert response.headers["etag"] == file_etag(row)


@pytest.mark.asyncio
async def test_a_slow_storage_read_never_keeps_a_signed_link_past_its_token(
    monkeypatch, signed
):
    """Freshness starts when the answer is sent: a lifetime read before a slow
    download would keep the bytes in the browser after the token expired."""
    from app.api.v1 import chat as chat_api
    from app.services import file_storage as file_storage_module

    clock = {"now": 1_000_000}
    monkeypatch.setattr(file_responses, "time", SimpleNamespace(time=lambda: clock["now"]))

    async def _slow_download(_key):
        clock["now"] += 90
        return b"bytes"

    monkeypatch.setattr(file_storage_module.file_storage, "download", _slow_download)
    row = _file()
    response = await chat_api.get_file_public(
        file_id=str(row.id), token="tok", expires=clock["now"] + 120, db=_QueueDB([row])
    )

    assert response.headers["cache-control"] == "private, max-age=30"


@pytest.mark.asyncio
async def test_a_signed_link_to_a_template_asset_is_revalidated(storage, signed):
    from app.api.v1 import chat as chat_api

    row = _template_asset()
    expires = int(time.time()) + 3600
    response = await chat_api.get_file_public(
        file_id=str(row.id), token="tok", expires=expires, db=_QueueDB([row])
    )
    assert response.headers["cache-control"] == "private, no-cache"

    not_modified = await chat_api.get_file_public(
        file_id=str(row.id),
        token="tok",
        expires=expires,
        if_none_match=response.headers["etag"],
        db=_QueueDB([row]),
    )
    assert not_modified.status_code == 304
    assert storage.reads == [row.s3_object_name]


@pytest.mark.asyncio
async def test_a_signed_link_checks_its_token_before_any_304(storage, monkeypatch):
    import app.core.security as security_module
    from app.api.v1 import chat as chat_api

    monkeypatch.setattr(security_module, "verify_image_token", lambda *_a, **_k: False)
    row = _file()
    with pytest.raises(HTTPException) as exc_info:
        await chat_api.get_file_public(
            file_id=str(row.id),
            token="forged",
            expires=int(time.time()) + 60,
            if_none_match=file_etag(row),
            db=_QueueDB([row]),
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_a_signed_image_link_is_private_and_never_outlives_its_token(signed):
    from app.api.v1 import chat as chat_api

    image = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), image_data=b"png", content_type="image/png")
    response = await chat_api.get_chat_image_public(
        image_id=str(image.id), token="tok", expires=int(time.time()) + 90, db=_QueueDB([image])
    )

    cache_control = response.headers["cache-control"]
    assert "public" not in cache_control
    assert cache_control.startswith("private, max-age=")
    assert 70 <= int(cache_control.rsplit("=", 1)[1]) <= 90


@pytest.mark.asyncio
async def test_the_session_image_route_stays_private_for_a_day():
    """A ChatImage is written once and never rewritten: a day is right."""
    from app.api.v1 import chat as chat_api

    owner = uuid.uuid4()
    image = SimpleNamespace(id=uuid.uuid4(), user_id=owner, image_data=b"png", content_type="image/png")
    response = await chat_api.get_chat_image(
        image_id=str(image.id), user=SimpleNamespace(id=owner, role="user"), db=_QueueDB([image])
    )
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert response.headers["vary"] == "Authorization"


# ── GET /platform/branding/asset/{kind} ───────────────────────────────────────


_LOGO = "data:image/png;base64,iVBORw0KGgo="


def _branding_request(query: str = "", headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": query.encode(), "headers": raw})


@pytest.fixture
def branding(monkeypatch):
    from app.api.v1 import platform as platform_api
    from app.services.branding import Branding

    current = Branding(name="Acme", logo=_LOGO, favicon=_LOGO)

    async def _get_branding(_db):
        return current

    monkeypatch.setattr(platform_api, "get_branding", _get_branding)
    return current


def _versions(current, kind: str) -> tuple[str, str]:
    from app.core.config import settings
    from app.services.branding import _branding_head_version, _data_url_hash, resolved_branding_payload

    field = "og_image" if kind == "og-image" else kind
    payload = resolved_branding_payload(current, is_xtm=settings.is_xtm_one)
    head = _branding_head_version(current, payload, is_xtm=settings.is_xtm_one)
    return _data_url_hash(getattr(current, field)), head


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["content-hash", "head-version"])
async def test_a_versioned_branding_url_keeps_its_day_in_any_cache(branding, which):
    from app.api.v1 import platform as platform_api

    content_hash, head = _versions(branding, "favicon")
    version = content_hash if which == "content-hash" else head
    response = await platform_api.get_branding_asset("favicon", _branding_request(f"v={version}"), db=None)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=86400"


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "v=0123456789abcdef", "v="])
async def test_an_unversioned_branding_url_is_revalidated(branding, query):
    """The bare path keeps its URL across an admin upload; a stale ``v`` names
    bytes the endpoint no longer serves - neither may be kept a day."""
    from app.api.v1 import platform as platform_api

    response = await platform_api.get_branding_asset("logo", _branding_request(query), db=None)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, no-cache"
    assert response.headers["etag"]


@pytest.mark.asyncio
async def test_the_branding_304_reads_every_form_of_if_none_match(branding):
    from app.api.v1 import platform as platform_api

    first = await platform_api.get_branding_asset("logo", _branding_request(), db=None)
    etag = first.headers["etag"]
    for header in (etag, f"W/{etag}", f'"other", {etag}'):
        response = await platform_api.get_branding_asset(
            "logo", _branding_request(headers={"If-None-Match": header}), db=None
        )
        assert response.status_code == 304, header
        assert response.headers["etag"] == etag
        assert response.headers["cache-control"] == "public, no-cache"


# ── Tripwires ─────────────────────────────────────────────────────────────────


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """``id(node)`` -> name of the innermost function holding it."""
    owners: dict[int, str] = {}

    def _visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else owner
            owners[id(child)] = name
            _visit(child, name)

    _visit(tree, "<module>")
    return owners


#: Every function allowed to answer ``Cache-Control: public``: each serves data
#: that is public by nature (branding, the widget script and logo, the JWKS
#: public keys, the content-hashed build files) and takes no credential. A new
#: entry is the decision that an answer is public; an authorised answer never
#: belongs here. ``main.py``'s module-level entry is
#: ``_IMMUTABLE_CACHE_CONTROL``, sent only for the SPA's hashed ``assets/``
#: files.
_PUBLIC_CACHE_ALLOWED = {
    ("api/v1/platform.py", "_branding_asset_response"),
    ("api/v1/public_chat.py", "serve_widget"),
    ("api/v1/public_chat.py", "serve_logo"),
    ("api/v1/xtm_auth.py", "jwks"),
    ("api/v1/xtm_auth.py", "metadata"),
    ("main.py", "<module>"),
}


def test_only_public_data_is_ever_marked_public():
    import re

    public_directive = re.compile(r"\s*public\s*,")
    found: set[tuple[str, str]] = set()
    for rel, tree in app_sources.modules(APP_ROOT):
        owners: dict[int, str] | None = None
        for node in app_sources.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and public_directive.match(node.value):
                owners = owners if owners is not None else _enclosing_functions(tree)
                found.add((rel, owners.get(id(node), "<module>")))
    assert found == _PUBLIC_CACHE_ALLOWED


#: The two writers that put new bytes behind an EXISTING row (a template asset
#: replaced in place, a seeded brand asset refreshed): both rows are template
#: assets, which is what ``file_bytes_may_change`` revalidates.
_OBJECT_KEY_REWRITERS = {"api/v1/template_containers.py", "services/template_asset_seed.py"}


def test_only_template_asset_writers_move_a_row_to_new_bytes():
    """A row whose object key is rewritten serves new bytes under a stable id. The
    cache contract revalidates template assets only; a new writer of another kind
    of row must either create a new row or widen ``file_bytes_may_change``."""
    found: set[str] = set()
    for rel, tree in app_sources.modules(APP_ROOT):
        for node in app_sources.walk(tree):
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                targets = [node.target]
            hit = any(isinstance(t, ast.Attribute) and t.attr == "s3_object_name" for t in targets)
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "setattr":
                    hit = len(node.args) > 1 and getattr(node.args[1], "value", None) == "s3_object_name"
                elif isinstance(func, ast.Attribute) and func.attr == "values":
                    hit = any(kw.arg == "s3_object_name" for kw in node.keywords)
            if hit:
                found.add(rel)
    assert found == _OBJECT_KEY_REWRITERS


#: The callers of ``upload_to`` besides ``file_storage`` itself, and why their key names one content.
_EXPLICIT_KEY_WRITERS = {
    # The brand seed and the benchmark document seed: the key is the content digest.
    "services/template_asset_seed.py": "file rows keyed by their content digest",
    "evaluations/seed.py": "file rows keyed by their content digest",
    # A figure's still, its steps and its build: no file row, served by their own
    # routes with no ETag, keyed by the digest of what they are drawn from.
    "services/video_studio/figures.py": "no file row, keyed by the digest of what is drawn",
    # A screen insert's build: no file row, served by its own route with no ETag,
    # keyed by the digest of what it is drawn from.
    "services/video_studio/insert_builds.py": "no file row, keyed by the digest of what is drawn",
    # The provider lexicon id of a rule set: no file row, read by the server alone.
    "services/video_studio/voice.py": "no file row, never served",
    # A figure's still kept from the retention sweep: the same bytes written back under their digest.
    "services/video_studio/figure_stills.py": "no file row, the same bytes rewritten under their digest",
    # What a render keeps for the next renders of a video: no file row, read by renders alone,
    # keyed by the digest of everything it is made from.
    "services/video_studio/render_cache.py": "no file row, never served, keyed by the digest of what it is made from",
    # The bundle a render drawn in scene ranges publishes for its ranges: no file row, read by
    # the range workers alone, keyed by the digest of what it holds.
    "services/video_studio/render_ranges.py": "no file row, never served, keyed by the digest of what it holds",
    # A sandbox workspace's contents: no file row, never served, keyed by their sha256.
    "services/sandbox_workspace/store.py": "no file row, never served, keyed by the digest of what it holds",
    # The PDF a knowledge-base figure pass reads: no file row, never served, keyed by the
    # id the document is given before its row is written, so one key names one upload.
    "services/kb_figure_pass.py": "no file row, never served, keyed by a document id minted once",
}


def test_no_writer_overwrites_the_bytes_behind_an_object_key():
    """The ETag is derived from the object key, so a key must name one content
    forever: ``upload()`` mints a random key, and every other caller of
    ``upload_to`` is named in ``_EXPLICIT_KEY_WRITERS`` with the reason its key
    holds - a file row keyed by its content digest, or an object that is no file
    row at all and so carries no ETag."""
    found: set[str] = set()
    for rel, tree in app_sources.modules(APP_ROOT):
        for node in app_sources.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "upload_to":
                found.add(rel)
    assert found == {"services/file_storage.py", *_EXPLICIT_KEY_WRITERS}


def test_the_benchmark_document_key_is_derived_from_its_bytes():
    from app.evaluations.documents import RenderedDocument
    from app.evaluations.seed import seed_object_name

    first = seed_object_name("ocr-benchmark", RenderedDocument("a.pdf", "application/pdf", b"one"))
    assert first == seed_object_name("ocr-benchmark", RenderedDocument("a.pdf", "application/pdf", b"one"))
    assert first != seed_object_name("ocr-benchmark", RenderedDocument("a.pdf", "application/pdf", b"two"))


def test_the_revalidating_routes_read_if_none_match_from_the_request_headers():
    """FastAPI binds the parameter to the ``If-None-Match`` header, not to a query
    string no browser sends (the route tests above call the functions directly)."""
    from fastapi.dependencies.utils import get_dependant

    from app.api.v1 import chat as chat_api
    from app.api.v1 import template_containers as template_api

    for call in (
        chat_api.get_file_public,
        chat_api.download_message_file,
        chat_api.download_file,
        template_api.download_template_asset,
    ):
        dependant = get_dependant(path="/x", call=call)
        assert [p.alias for p in dependant.header_params] == ["if-none-match"], call.__name__
        assert "if_none_match" not in [p.alias for p in dependant.query_params], call.__name__


def test_every_file_byte_route_revalidates_before_it_reads_storage():
    """Source-level guard: each FileAttachment byte route answers the ``304``
    before the object-store read, and builds its ``200`` through the one helper."""
    from app.api.v1 import chat as chat_api
    from app.api.v1 import template_containers as template_api

    routes = [
        chat_api.get_file_public,
        chat_api.download_message_file,
        chat_api.download_file,
        template_api.download_template_asset,
    ]
    for route in routes:
        source = inspect.getsource(route)
        assert "file_not_modified(" in source, route.__name__
        assert source.index("file_not_modified(") < source.index("file_storage.download("), route.__name__
        assert "file_response_headers(" in source, route.__name__
        assert "Cache-Control" not in source, route.__name__
    assert chat_api._file_response_headers is file_responses.file_response_headers


def test_the_render_asset_route_answers_through_the_helper_without_storing():
    """The export's render-token fetch serves a FileAttachment's bytes too: its
    headers come from the helper with ``store=False``, never a hand-written
    ``Cache-Control`` (the token rides in the query string; nothing may keep it)."""
    from app.api.v1 import presentations as presentations_api

    source = inspect.getsource(presentations_api.render_asset)
    assert "file_response_headers(file_att, store=False)" in source
    assert "Cache-Control" not in source


def test_the_widget_session_download_answers_through_the_helper_without_storing():
    """The public widget's session download serves a FileAttachment's
    bytes too. Its capability (the agent token and the session id) rides in the
    URL, so nothing may keep the answer: the helper with ``store=False``, never a
    hand-written ``Cache-Control``."""
    from app.api.v1 import public_chat

    source = inspect.getsource(public_chat.download_session_file)
    assert "file_response_headers(file_att, store=False)" in source
    assert "Cache-Control" not in source

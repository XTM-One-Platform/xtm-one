"""Which stored files open in the browser and which are saved.

The signed link a channel reply carries (``GET /chat/files/{id}/public``) opens
raster images and what a browser shows or plays itself - PDF, plain text,
Markdown, CSV, WebVTT, JSON and its JSON Lines and ``+json`` variants, audio and
video. The session routes
(``GET /chat/files/{id}/download`` behind Work > Files and a run's outputs, and
the chat's message-scoped download) open raster images and save every document
under its name, as at the baseline. HTML, SVG, XML and any other or unknown type
are downloads everywhere. Every answer is ``nosniff``, the decision reads the
stored type and never the file name, and an inline answer declares the type it
was decided on, never the stored string.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.responses import Response

from app.core.utils import build_content_disposition
from app.services.file_responses import (
    INLINE_DOCUMENT_CONTENT_TYPES,
    INLINE_FILE_CONTENT_TYPES,
    file_etag,
    file_response_headers,
)
from tests import app_sources

APP_ROOT = Path(__file__).resolve().parent.parent / "app"

#: What every stub download returns: a page with a script, so an answer that
#: would render it is visible in the assertions, whatever the stored type says.
_HTML_BYTES = b"<!doctype html><script>alert(document.domain)</script>"

#: What the signed link opens again as it did at the baseline, beside PDF, plain
#: text and JSON: a document a browser shows, or audio and video it plays. The
#: session routes saved them at the baseline and still do.
RESTORED_TYPES = [
    "text/markdown",
    "text/x-markdown",
    "text/csv",
    "text/vtt",
    "application/x-ndjson",
    "application/ld+json",
    "application/problem+json",
    "application/vnd.api+json",
    "audio/mpeg",
    "audio/wav",
    "audio/ogg",
    "audio/webm",
    "audio/mp4",
    "video/mp4",
    "video/webm",
    "video/quicktime",
]

#: (stored type, the signed link opens it, the session routes open it).
TYPE_TABLE = [
    ("image/png", True, True),
    ("image/jpeg", True, True),
    ("image/gif", True, True),
    ("image/webp", True, True),
    ("image/avif", True, True),
    ("image/bmp", True, True),
    ("image/tiff", True, True),
    ("image/x-icon", True, True),
    ("image/vnd.microsoft.icon", True, True),
    ("image/jpg", True, True),
    ("image/pjpeg", True, True),
    ("image/x-png", True, True),
    ("image/apng", True, True),
    ("image/x-ms-bmp", True, True),
    ("image/heic", True, True),
    ("image/heif", True, True),
    ("image/heic-sequence", True, True),
    ("image/heif-sequence", True, True),
    ("image/jxl", True, True),
    ("image/jp2", True, True),
    ("image/jpx", True, True),
    ("image/jpm", True, True),
    ("application/pdf", True, False),
    ("text/plain", True, False),
    ("application/json", True, False),
    *((kind, True, False) for kind in RESTORED_TYPES),
    ("text/html", False, False),
    ("application/xhtml+xml", False, False),
    ("image/svg+xml", False, False),
    ("text/xml", False, False),
    ("application/xml", False, False),
    ("application/rss+xml", False, False),
    ("text/javascript", False, False),
    ("application/javascript", False, False),
    ("text/tab-separated-values", False, False),
    ("application/yaml", False, False),
    ("application/zip", False, False),
    ("application/octet-stream", False, False),
    ("", False, False),
    (None, False, False),
]


# -- Stubs --------------------------------------------------------------------


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

    async def commit(self) -> None:
        """The route commits the file's last use (retention, #396) before answering."""


def _file(content_type, *, filename="deliverable.bin", **overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=None,
        message_id=None,
        assignment_run_id=None,
        template_container_id=None,
        source="chat",
        filename=filename,
        content_type=content_type,
        size=len(_HTML_BYTES),
        s3_object_name=f"generated/u/{uuid.uuid4().hex}",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def storage(monkeypatch):
    from app.services import file_storage as file_storage_module

    async def _download(_key):
        return _HTML_BYTES

    monkeypatch.setattr(file_storage_module.file_storage, "download", _download)


@pytest.fixture
def signed(monkeypatch):
    """Let every signed-link token through (the token gate has its own tests)."""
    import app.core.security as security_module

    monkeypatch.setattr(security_module, "verify_image_token", lambda *_a, **_k: True)


async def _signed_link(row):
    from app.api.v1 import chat as chat_api

    return await chat_api.get_file_public(
        file_id=str(row.id), token="tok", expires=int(time.time()) + 3600, db=_QueueDB([row])
    )


async def _download_by_id(row):
    from app.api.v1 import chat as chat_api

    user = SimpleNamespace(id=row.user_id, role="user")
    return await chat_api.download_file(file_id=str(row.id), user=user, db=_QueueDB([row]))


async def _message_scoped_download(row):
    from app.api.v1 import chat as chat_api

    conv = SimpleNamespace(id=uuid.uuid4(), user_id=row.user_id)
    msg = SimpleNamespace(id=uuid.uuid4(), conversation_id=conv.id)
    row.conversation_id, row.message_id = conv.id, msg.id
    return await chat_api.download_message_file(
        conversation_id=str(conv.id),
        message_id=str(msg.id),
        file_id=str(row.id),
        user=SimpleNamespace(id=row.user_id, role="user"),
        db=_QueueDB([conv, msg, row]),
    )


def _browser_mime_essence(header_value: str) -> str:
    """The MIME type a browser reads off ``Content-Type`` (WHATWG Fetch, "extract a MIME type").

    The header is split on commas outside quoted strings and the LAST value that
    parses as ``type/subtype`` wins - which is why an inline answer may never
    echo a stored type verbatim.
    """
    values, current, quoted = [], "", False
    for char in header_value:
        if char == '"':
            quoted = not quoted
        if char == "," and not quoted:
            values.append(current)
            current = ""
        else:
            current += char
    values.append(current)
    essence = ""
    for value in values:
        candidate = value.split(";", 1)[0].strip().lower()
        if re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", candidate) and candidate != "*/*":
            essence = candidate
    return essence


# -- The type table, route by route -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("content_type", "signed_inline", "session_inline"), TYPE_TABLE)
async def test_each_type_opens_or_downloads_on_each_route(storage, signed, content_type, signed_inline, session_inline):
    routes = (
        (_signed_link, signed_inline),
        (_download_by_id, session_inline),
        (_message_scoped_download, session_inline),
    )
    for route, inline in routes:
        response = await route(_file(content_type))
        where = (route.__name__, content_type)
        assert response.status_code == 200, where
        assert response.headers["x-content-type-options"] == "nosniff", where
        disposition = response.headers["content-disposition"]
        if inline:
            assert disposition == "inline", where
            assert response.headers.getlist("content-type") == [
                (content_type or "") + ("; charset=utf-8" if (content_type or "").startswith("text/") else "")
            ], where
        else:
            assert disposition.startswith("attachment;"), where
            assert 'filename="deliverable.bin"' in disposition, where


def _baseline_content_type(stored: str) -> str:
    """The ``Content-Type`` the baseline's file routes sent: the stored type as the
    route's ``media_type``, spelled by Starlette (a ``text/*`` type gains UTF-8)."""
    return Response(content=b"", media_type=stored).headers["content-type"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", RESTORED_TYPES)
async def test_a_restored_type_answers_each_route_as_the_baseline_did(storage, signed, stored):
    """At the baseline the signed link sent no ``Content-Disposition`` - the browser
    shows the file in place, which ``inline`` says - and the session routes the
    download ``build_content_disposition`` builds, each with the stored type."""
    link = await _signed_link(_file(stored))
    assert link.headers["content-disposition"] == "inline"
    assert link.headers.getlist("content-type") == [_baseline_content_type(stored)]
    assert link.headers["x-content-type-options"] == "nosniff"
    for route in (_download_by_id, _message_scoped_download):
        saved = await route(_file(stored))
        assert saved.headers["content-disposition"] == build_content_disposition("deliverable.bin"), route.__name__
        assert saved.headers.getlist("content-type") == [_baseline_content_type(stored)], route.__name__
        assert saved.headers["x-content-type-options"] == "nosniff", route.__name__


_SCRIPT_CAPABLE_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/xml",
    "application/xml",
    "application/rss+xml",
    "text/javascript",
    "application/javascript",
    "application/octet-stream",
)


def test_the_inline_sets_hold_no_script_capable_type():
    inline = INLINE_FILE_CONTENT_TYPES | INLINE_DOCUMENT_CONTENT_TYPES
    for kind in _SCRIPT_CAPABLE_TYPES:
        assert kind not in inline, kind
        assert file_response_headers(_file(kind), inline_documents=True)["Content-Disposition"].startswith(
            "attachment;"
        ), kind
    assert all(kind.startswith("image/") and "xml" not in kind for kind in INLINE_FILE_CONTENT_TYPES)
    assert sorted(INLINE_DOCUMENT_CONTENT_TYPES) == [
        "application/json",
        "application/pdf",
        "application/x-ndjson",
        "text/csv",
        "text/markdown",
        "text/plain",
        "text/vtt",
        "text/x-markdown",
    ]


@pytest.mark.parametrize(
    "stored",
    [
        "video/x-scene+xml",
        "audio/svg+xml",
        "video/xml",
        "audio/html",
        "video/xhtml+xml",
        "application/ld+json+xml",
        "application/+json",
        "application/ld+jsonx",
        "text/json+xml",
        "audio/",
        "video",
        "application/ld+json, text/html",
        "text/html, application/ld+json",
        "application/x,text/html,application/ld+json",
        "audio/mpeg,text/html",
        "video/mp4 text/html",
        'application/"ld+json"',
        "text/markdown,text/html",
        "text/csv, text/html",
    ],
)
def test_the_channel_link_never_opens_a_page_or_a_second_type(stored):
    """The families are read off the whole ``type/subtype``: an audio or video
    subtype naming XML or HTML (any ``+xml`` type is an XML document when
    navigated to), a list of types or a malformed name is a download, like any
    unknown type."""
    headers = file_response_headers(_file(stored), inline_documents=True)
    assert headers["Content-Disposition"].startswith("attachment;"), stored
    assert "Content-Type" not in headers, stored


@pytest.mark.asyncio
async def test_a_json_type_opens_whatever_its_name_says(storage, signed):
    """GitHub serves rendered bodies as ``application/vnd.github.html+json``: the
    ``+json`` suffix makes it JSON to every browser, so the link shows it as
    text, as at the baseline."""
    stored = "application/vnd.github.html+json"
    response = await _signed_link(_file(stored))
    assert response.headers["content-disposition"] == "inline"
    assert response.headers.getlist("content-type") == [stored]
    assert _browser_mime_essence(response.headers["content-type"]) == stored


# -- What an inline answer declares -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "declared"),
    [
        ("image/png", "image/png"),
        ("IMAGE/PNG", "image/png"),
        ("image/JXL", "image/jxl"),
        ("image/jpeg; charset=binary", "image/jpeg"),
        ("application/pdf", "application/pdf"),
        ("Application/PDF; name=report.pdf", "application/pdf"),
        ("text/plain", "text/plain; charset=utf-8"),
        ("text/plain; charset=ISO-8859-1", "text/plain; charset=ISO-8859-1"),
        ('text/plain; charset="windows-1252"', "text/plain; charset=windows-1252"),
        ("text/plain; charset=utf-8,text/html", "text/plain; charset=utf-8"),
        ("application/json", "application/json"),
        ("application/json; charset=utf-8", "application/json; charset=utf-8"),
        ("image/png;x=,text/html", "image/png"),
        ("application/pdf;x=,text/html", "application/pdf"),
        ("text/plain;x=,text/html;charset=utf-8", "text/plain; charset=utf-8"),
        ("text/markdown", "text/markdown; charset=utf-8"),
        ("Text/Markdown; charset=ISO-8859-1", "text/markdown; charset=ISO-8859-1"),
        ("text/csv; header=present", "text/csv; charset=utf-8"),
        ("TEXT/VTT", "text/vtt; charset=utf-8"),
        ("application/x-ndjson", "application/x-ndjson"),
        ("application/ld+json", "application/ld+json"),
        ("application/ld+json; charset=utf-8", "application/ld+json; charset=utf-8"),
        ("audio/ogg; codecs=opus", "audio/ogg"),
        ("audio/mpeg; charset=utf-8", "audio/mpeg"),
        ("video/mp4;x=,text/html", "video/mp4"),
        ("application/ld+json;x=,text/html", "application/ld+json"),
        ("text/markdown;x=,text/html;charset=utf-8", "text/markdown; charset=utf-8"),
    ],
)
async def test_an_inline_answer_declares_the_type_it_was_decided_on(storage, signed, stored, declared):
    """An upload stores the part type its client sent. Sent back verbatim, a
    ``image/png;x=,text/html`` is read as ``text/html`` by the browser, so an
    inline answer declares the canonical type, keeping only a text charset."""
    response = await _signed_link(_file(stored))
    assert response.headers["content-disposition"] == "inline"
    assert response.headers.getlist("content-type") == [declared]
    assert _browser_mime_essence(response.headers["content-type"]) == declared.split(";", 1)[0]
    if "," in stored:
        assert _browser_mime_essence(stored) == "text/html", "the stored string is the attack"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "declared"),
    [
        ('video/mp4; codecs="avc1.42E01E, mp4a.40.2"', "video/mp4"),
        ("video/webm;codecs=vp8,opus", "video/webm"),
        ("audio/webm;codecs=opus", "audio/webm"),
    ],
)
async def test_media_opens_under_its_bare_type(storage, signed, stored, declared):
    """What a recorder stores (``MediaRecorder`` writes ``video/webm;codecs=vp8,opus``)
    opens under the bare type: the player reads the codecs off the bytes."""
    response = await _signed_link(_file(stored))
    assert response.headers["content-disposition"] == "inline"
    assert response.headers.getlist("content-type") == [declared]


@pytest.mark.asyncio
async def test_a_stored_type_that_is_a_list_is_unknown(storage, signed):
    response = await _signed_link(_file("application/pdf, text/html"))
    assert response.headers["content-disposition"].startswith("attachment;")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        "application/pdf",
        "text/plain",
        "application/json",
        "image/png",
        "text/markdown",
        "text/csv",
        "text/vtt",
        "application/ld+json",
        "application/x-ndjson",
        "audio/mpeg",
        "video/mp4",
    ],
)
async def test_html_stored_under_an_inert_type_can_never_render(storage, signed, stored):
    """HTML bytes labelled PDF (or text, JSON, PNG, Markdown, audio, video...): the
    link opens them as that type, ``nosniff``, so the browser shows a broken
    document or player, never a page."""
    response = await _signed_link(_file(stored, filename="page.html"))
    assert response.body == _HTML_BYTES
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert _browser_mime_essence(response.headers["content-type"]) == stored


@pytest.mark.asyncio
async def test_the_decision_reads_the_stored_type_never_the_filename(storage, signed):
    as_pdf = await _signed_link(_file("text/html", filename="report.pdf"))
    assert as_pdf.headers["content-disposition"].startswith("attachment;")
    as_page = await _signed_link(_file("application/pdf", filename="page.html"))
    assert as_page.headers["content-disposition"] == "inline"
    assert as_page.headers["content-type"] == "application/pdf"
    as_notes = await _signed_link(_file("text/html", filename="notes.md"))
    assert as_notes.headers["content-disposition"].startswith("attachment;")
    as_clip = await _signed_link(_file("image/svg+xml", filename="clip.mp4"))
    assert as_clip.headers["content-disposition"].startswith("attachment;")
    as_markdown = await _signed_link(_file("text/markdown", filename="page.html"))
    assert as_markdown.headers["content-disposition"] == "inline"
    assert as_markdown.headers["content-type"] == "text/markdown; charset=utf-8"


# -- Answers cached under the previous rules ----------------------------------


def _previous_rules_etag(row, version: str | None = None) -> str:
    """The validator an earlier rule set minted: its version, then the row's key,
    type and name - or those three alone before the rules had a version."""
    fields = (row.s3_object_name, row.content_type or "", row.filename or "")
    material = "\0".join(fields if version is None else (version, *fields))
    return f'"{hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]}"'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "version"),
    [
        ("image/tiff", None),
        ("image/png;x=,text/html", None),
        ("image/heic", "2"),
        ("image/jxl", "2"),
    ],
)
async def test_an_answer_cached_under_the_previous_rules_is_served_fresh(storage, stored, version):
    """A ``304`` carries no ``Content-Type`` and no ``Content-Disposition``, so a
    TIFF cached as a download, a picture cached with its stored type echoed, or
    a HEIC / JPEG XL picture cached as a download under rules 2, would keep those
    headers through every revalidation: it gets a ``200``."""
    from app.api.v1 import chat as chat_api

    row = _file(stored)
    user = SimpleNamespace(id=row.user_id, role="user")
    fresh = await chat_api.download_file(
        file_id=str(row.id), if_none_match=_previous_rules_etag(row, version), user=user, db=_QueueDB([row])
    )
    assert fresh.status_code == 200
    assert fresh.headers["content-disposition"] == "inline"
    assert _browser_mime_essence(fresh.headers["content-type"]) == stored.split(";", 1)[0]

    held = await chat_api.download_file(
        file_id=str(row.id), if_none_match=fresh.headers["etag"], user=user, db=_QueueDB([row])
    )
    assert held.status_code == 304


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    ["text/markdown", "text/csv", "text/vtt", "application/ld+json", "application/x-ndjson", "audio/mpeg", "video/mp4"],
)
async def test_a_channel_link_cached_as_a_download_under_rules_3_is_served_fresh(storage, signed, stored):
    """Under rules 3 the signed link saved these types, and a browser keeps that
    answer and revalidates it for as long as the token lives: the rules-3
    validator gets a ``200`` that opens the file, the new one a ``304``."""
    from app.api.v1 import chat as chat_api

    row = _file(stored)
    expires = int(time.time()) + 3600
    fresh = await chat_api.get_file_public(
        file_id=str(row.id),
        token="tok",
        expires=expires,
        if_none_match=_previous_rules_etag(row, "3"),
        db=_QueueDB([row]),
    )
    assert fresh.status_code == 200
    assert fresh.headers["content-disposition"] == "inline"
    assert _browser_mime_essence(fresh.headers["content-type"]) == stored

    held = await chat_api.get_file_public(
        file_id=str(row.id), token="tok", expires=expires, if_none_match=fresh.headers["etag"], db=_QueueDB([row])
    )
    assert held.status_code == 304


@pytest.mark.parametrize("stored", ["image/png", "application/pdf", "text/markdown", "video/mp4", "application/zip"])
def test_no_validator_of_the_previous_rules_still_matches(stored):
    """Every rule set bumps the version, whatever route or type a cached answer
    came from, so no ``304`` ever confirms headers built by earlier rules."""
    row = _file(stored)
    assert file_etag(row) not in {_previous_rules_etag(row, version) for version in (None, "2", "3")}


def test_the_validator_names_the_version_of_the_response_rules(monkeypatch):
    from app.services import file_responses

    row = _file("image/png")
    before = file_responses.file_etag(row)
    monkeypatch.setattr(file_responses, "_RESPONSE_CONTRACT_VERSION", "next")
    assert file_responses.file_etag(row) != before


# -- The header helper's other callers ----------------------------------------


def test_documents_stay_downloads_unless_a_route_opens_them():
    """The session routes, the render-token fetch and the widget session
    download call the helper without ``inline_documents``; the template asset
    download names its saved file from the header and opens nothing."""
    for stored in [*sorted(INLINE_DOCUMENT_CONTENT_TYPES), "application/ld+json", "audio/mpeg", "video/mp4"]:
        row = _file(stored, filename="brief.pdf")
        assert file_response_headers(row)["Content-Disposition"].startswith("attachment;"), stored
        assert file_response_headers(row, store=False)["Content-Disposition"].startswith("attachment;"), stored
        opened = file_response_headers(row, inline_documents=True)
        assert opened["Content-Disposition"] == "inline", stored
        assert opened["Content-Type"].split(";", 1)[0] == stored
    for stored in ("image/png", "application/pdf", "text/plain"):
        headers = file_response_headers(_file(stored, filename="logo.png"), inline_images=False)
        assert headers["Content-Disposition"].startswith("attachment;"), stored
        assert "Content-Type" not in headers


def test_a_download_keeps_the_route_type():
    headers = file_response_headers(_file("text/html", filename="page.html"), inline_documents=True)
    assert headers["Content-Disposition"].startswith("attachment;")
    assert "Content-Type" not in headers


def test_an_inline_answer_never_reads_a_filename():
    """A row with no filename at all (a ``ChatImage``) still gets its inline answer."""
    headers = file_response_headers(SimpleNamespace(content_type="image/png"), store=False)
    assert headers["Content-Disposition"] == "inline"
    assert headers["Content-Type"] == "image/png"


def _calls_passing(node: ast.AST, keyword: str, owner: str = "<module>"):
    """Yield the enclosing function of every call under *node* passing *keyword* anything but ``False``."""
    for child in ast.iter_child_nodes(node):
        name = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else owner
        if isinstance(child, ast.Call):
            for kw in child.keywords:
                if kw.arg == keyword and not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                    yield name
        yield from _calls_passing(child, keyword, name)


def _functions_passing(keyword: str) -> set[tuple[str, str]]:
    """(module, enclosing function) of every call in the app passing *keyword*."""
    found: set[tuple[str, str]] = set()
    for path in app_sources.python_files(APP_ROOT):
        if keyword not in app_sources.source(path):
            continue  # a keyword argument is spelt out where it is passed
        module = app_sources.relative(path, APP_ROOT)
        found.update((module, function) for function in _calls_passing(app_sources.tree(path), keyword))
    return found


def test_only_the_signed_channel_link_opens_documents():
    from app.api.v1 import chat as chat_api

    assert _functions_passing("inline_documents") == {("api/v1/chat.py", "get_file_public")}
    assert "inline_documents=True" in inspect.getsource(chat_api.get_file_public)


# -- The link a Slack or Teams reply carries ----------------------------------


def _chat_client(db) -> TestClient:
    from app.api.v1 import chat as chat_api
    from app.core.database import get_db

    app = FastAPI()
    app.include_router(chat_api.router, prefix="/api/v1/chat")

    async def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


@pytest.mark.parametrize(
    ("filename", "stored", "inline"),
    [
        ("Q3 report.pdf", "application/pdf", True),
        ("notes.txt", "text/plain", True),
        ("iocs.json", "application/json", True),
        ("chart.png", "image/png", True),
        ("scan.tiff", "image/tiff", True),
        ("IMG_0042.heic", "image/heic", True),
        ("release-notes.md", "text/markdown", True),
        ("Q3 figures.csv", "text/csv", True),
        ("captions.vtt", "text/vtt", True),
        ("graph.jsonld", "application/ld+json", True),
        ("events.ndjson", "application/x-ndjson", True),
        ("briefing.mp3", "audio/mpeg", True),
        ("demo.mp4", "video/mp4", True),
        ("briefing.html", "text/html", False),
        ("diagram.svg", "image/svg+xml", False),
        ("feed.xml", "application/xml", False),
    ],
)
def test_the_link_a_slack_or_teams_reply_carries_opens_safe_types(storage, filename, stored, inline):
    """End to end: the ``[[FILE:id]]`` marker becomes the Markdown link both
    adapters send (Slack as ``<url|name>`` mrkdwn), and that exact URL, token
    and all, answers through the signed route."""
    from app.channels.base import resolve_channel_file_markers
    from app.core.utils import markdown_to_slack
    from app.services.teams_format import markdown_to_teams_text

    row = _file(stored, filename=filename)
    reply = asyncio.run(
        resolve_channel_file_markers(
            _QueueDB([row]), f"Here it is: [[FILE:{row.id}]]", "https://xtm.example", user_id=row.user_id
        )
    )
    link = re.fullmatch(r"Here it is: \[([^\]]+)\]\((https://xtm\.example/[^)\s]+)\)", reply)
    assert link, reply
    name, url = link.groups()
    assert name == filename
    assert markdown_to_slack(reply) == f"Here it is: <{url}|{name}>"
    assert markdown_to_teams_text(reply) == reply

    parts = urlsplit(url)
    assert parts.path == f"/api/v1/chat/files/{row.id}/public"
    assert set(parse_qs(parts.query)) == {"token", "expires"}

    response = _chat_client(_QueueDB([row])).get(f"{parts.path}?{parts.query}")
    assert response.status_code == 200, response.text
    assert response.content == _HTML_BYTES
    assert response.headers["x-content-type-options"] == "nosniff"
    assert len(response.headers.get_list("content-type")) == 1
    disposition = response.headers["content-disposition"]
    if inline:
        assert disposition == "inline"
        assert _browser_mime_essence(response.headers["content-type"]) == stored
    else:
        assert disposition.startswith("attachment;")
        assert "filename=" in disposition


@pytest.mark.parametrize("stored", ["text/markdown", "application/ld+json", "video/mp4"])
def test_opening_a_restored_type_changes_nothing_else_in_the_answer(monkeypatch, signed, stored):
    """Status, body, ``Content-Length``, ranges (none: a ``Range`` request gets the
    whole file with a ``200``, as at the baseline), the validator and lifetime
    headers, and the ``304`` answered before any storage read."""
    from app.services import file_storage as file_storage_module

    reads: list[str] = []

    async def _download(key):
        reads.append(key)
        return _HTML_BYTES

    monkeypatch.setattr(file_storage_module.file_storage, "download", _download)
    row = _file(stored)
    url = f"/api/v1/chat/files/{row.id}/public?token=tok&expires={int(time.time()) + 3600}"

    response = _chat_client(_QueueDB([row])).get(url, headers={"Range": "bytes=0-3"})
    assert response.status_code == 200
    assert response.content == _HTML_BYTES
    assert response.headers["content-length"] == str(len(_HTML_BYTES))
    assert "content-range" not in response.headers
    assert "accept-ranges" not in response.headers
    assert response.headers["etag"] == file_etag(row)
    assert response.headers["cache-control"].startswith("private, max-age=")
    assert response.headers["vary"] == "Authorization"
    assert reads == [row.s3_object_name]

    held = _chat_client(_QueueDB([row])).get(url, headers={"If-None-Match": response.headers["etag"]})
    assert held.status_code == 304
    assert "content-type" not in held.headers
    assert "content-disposition" not in held.headers
    assert held.headers["etag"] == file_etag(row)
    assert reads == [row.s3_object_name]


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["text/markdown", "application/ld+json", "video/mp4"])
async def test_a_restored_type_keeps_every_token_and_access_check(storage, monkeypatch, stored):
    """The signed link refuses a missing or forged token before any ``304`` or
    read, and the session route refuses a reader who may not read the file."""
    import app.core.security as security_module
    from app.api.v1 import chat as chat_api

    row = _file(stored)
    with pytest.raises(HTTPException) as missing:
        await chat_api.get_file_public(file_id=str(row.id), token="", expires=0, db=_QueueDB([row]))
    assert missing.value.status_code == 401

    monkeypatch.setattr(security_module, "verify_image_token", lambda *_a, **_k: False)
    with pytest.raises(HTTPException) as forged:
        await chat_api.get_file_public(
            file_id=str(row.id),
            token="forged",
            expires=int(time.time()) + 60,
            if_none_match=file_etag(row),
            db=_QueueDB([row]),
        )
    assert forged.value.status_code == 403

    stranger = SimpleNamespace(id=uuid.uuid4(), role="user")
    with pytest.raises(HTTPException) as refused:
        await chat_api.download_file(file_id=str(row.id), user=stranger, db=_QueueDB([row]))
    assert refused.value.status_code == 404


def test_an_image_a_channel_reply_embeds_answers_through_the_same_signed_route():
    from app.api.v1 import chat as chat_api
    from app.channels.base import resolve_channel_images

    file_id = uuid.uuid4()
    text, images = resolve_channel_images(
        f"![chart](/api/v1/chat/files/{file_id}/download)", "https://xtm.example", allowed_ids={str(file_id)}
    )
    assert [image.public_url for image in images] == [text[len("![chart](") : -1]]
    assert urlsplit(images[0].public_url).path == f"/api/v1/chat/files/{file_id}/public"
    route = next(r for r in chat_api.router.routes if getattr(r, "endpoint", None) is chat_api.get_file_public)
    assert route.path == "/files/{file_id}/public"

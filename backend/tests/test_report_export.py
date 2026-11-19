"""Reports as stored files: the server-side PDF export.

A report was printable only by the reader's browser, so it had no stored file:
an agent could not hand it to a file tool and the user got no download chip.
``export_report`` (tool) and ``POST /reports/{id}/export`` (REST) now render it
through the SAME headless Chromium the decks export with - one browser session
(``presentation_export.open_render_page``), the export slots, the same-origin
policy, the timeouts - loading the report's static render page with a confined
render token. The Chromium render itself runs end to end in the deploy
pipeline; what is pinned here is everything around it that can regress
silently: the token, the routes, the tool contract mirrored on
``export_presentation`` (a revision exported once is that file), the wiring
and the frontend protocol.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import create_access_token, is_confined_token
from app.services import report_export
from app.services.presentation_export import (
    PDF_CONTENT_TYPE,
    RENDER_PAGE_SIGNALS,
    ExportedFile,
    RenderTokenError,
    mint_render_token,
)
from app.services.report_export import (
    REPORT_RENDER_TOKEN_SCOPE,
    ReportRenderError,
    decode_report_render_token,
    mint_report_render_token,
    static_report_url,
)

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


class _Result:
    def __init__(self, one=None):
        self._one = one

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.statements: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()

    def in_transaction(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _export_budget_admits(monkeypatch):
    """The export routes' per-user budget is counted in Redis, which unit tests have none of."""
    from app.api.v1 import reports as routes

    async def _admit(user_id):
        return None

    monkeypatch.setattr(routes, "enforce_export_rate_limit", _admit)


def _report(user_id: uuid.UUID | None, **overrides):
    columns = {
        "id": uuid.uuid4(),
        "user_id": user_id,
        "title": "Threat brief",
        "theme": "light",
        "block_count": 1,
        "revision": 2,
        "conversation_id": None,
        "assignment_run_id": None,
        "template_container_id": None,
        "spec": {"title": "Threat brief", "header": {"title": "Threat brief"}, "blocks": []},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    columns.update(overrides)
    return SimpleNamespace(**columns)


def _user(user_id: uuid.UUID, role: str = "user", active: bool = True):
    return SimpleNamespace(id=user_id, role=role, is_admin=role == "admin", is_active=active)


def _app(session: _FakeSession, user=None) -> TestClient:
    from app.api.deps import get_current_user
    from app.api.v1 import reports as routes
    from app.core.database import get_db

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/reports")

    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    if user is not None:

        async def _user_dep():
            return user

        app.dependency_overrides[get_current_user] = _user_dep
    return TestClient(app)


# ── Render token ──────────────────────────────────────────────────────────────


def test_report_render_token_round_trips_and_is_confined():
    report_id, user_id = uuid.uuid4(), uuid.uuid4()
    token = mint_report_render_token(report_id, user_id)
    claims = decode_report_render_token(token)
    assert (claims.report_id, claims.user_id) == (report_id, user_id)
    payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    assert payload["scope"] == REPORT_RENDER_TOKEN_SCOPE
    assert is_confined_token(payload), "get_current_user must refuse it"
    assert payload["exp"] - payload["iat"] <= 180


def test_a_deck_token_and_a_session_are_not_report_tokens():
    with pytest.raises(RenderTokenError):
        decode_report_render_token(mint_render_token(uuid.uuid4(), uuid.uuid4()))
    with pytest.raises(RenderTokenError):
        decode_report_render_token(create_access_token(str(uuid.uuid4())))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: {k: v for k, v in p.items() if k != "report_id"},
        lambda p: {**p, "report_id": "not-a-uuid"},
        # Fixed and long past, not "a second ago": a runner's wall clock may step between encode and decode.
        lambda p: {**p, "iat": 1_000_000_000, "exp": 1_000_000_180},
    ],
    ids=["unbound", "malformed-binding", "expired"],
)
def test_report_render_token_variants_are_refused(mutate):
    payload = {
        "sub": str(uuid.uuid4()),
        "scope": REPORT_RENDER_TOKEN_SCOPE,
        "report_id": str(uuid.uuid4()),
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": datetime.now(UTC) + timedelta(minutes=1),
    }
    token = jwt.encode(mutate(payload), settings.secret_key, algorithm=settings.jwt_algorithm)
    with pytest.raises(RenderTokenError):
        decode_report_render_token(token)


def test_static_report_url_carries_the_token_in_the_query(monkeypatch):
    monkeypatch.setattr(report_export, "render_base_url", lambda: "http://127.0.0.1:4000/xtm")
    report_id = uuid.uuid4()
    assert static_report_url(report_id, "tok en") == f"http://127.0.0.1:4000/xtm/reports/{report_id}/static?render_token=tok+en"


# ── The export itself ─────────────────────────────────────────────────────────


def test_export_prints_the_static_page_through_the_deck_exports_browser(monkeypatch):
    """One browser session for every export: the report page is opened through
    ``open_render_page`` with its own ready signal, and printed by its own
    ``@page`` rule with background graphics off, as a print dialog does."""
    seen: dict = {}

    class _Page:
        async def pdf(self, **kwargs):
            seen["pdf"] = kwargs
            return b"%PDF-1.7 report"

    @asynccontextmanager
    async def _open(url, **kwargs):
        seen["url"], seen["open"] = url, kwargs
        yield _Page()

    monkeypatch.setattr(report_export, "open_render_page", _open)
    report_id, user_id = uuid.uuid4(), uuid.uuid4()
    exported = asyncio.run(report_export.export_report(report_id=report_id, user_id=user_id, title="Brief: Q3 é"))

    assert exported == ExportedFile("brief-q3-e.pdf", PDF_CONTENT_TYPE, b"%PDF-1.7 report")
    assert seen["open"]["signal"] == "report"
    assert seen["open"]["error"] is ReportRenderError
    assert seen["pdf"] == {"prefer_css_page_size": True}
    token = seen["url"].split("render_token=", 1)[1]
    claims = decode_report_render_token(token)
    assert (claims.report_id, claims.user_id) == (report_id, user_id)

    untitled = asyncio.run(report_export.export_report(report_id=report_id, user_id=user_id, title="!!!"))
    assert untitled.filename == "report.pdf"


def test_the_page_signals_are_the_ones_the_static_pages_set():
    assert set(RENDER_PAGE_SIGNALS) == {"deck", "report", "figure", "dashboard"}
    deck = (FRONTEND_SRC / "pages" / "PresentationStaticRender.tsx").read_text(encoding="utf-8")
    report = (FRONTEND_SRC / "pages" / "ReportStaticRender.tsx").read_text(encoding="utf-8")
    figure = (FRONTEND_SRC / "pages" / "VideoFigureStaticRender.tsx").read_text(encoding="utf-8")
    dashboard = (FRONTEND_SRC / "pages" / "DashboardStaticRender.tsx").read_text(encoding="utf-8")
    assert "dataset.deckReady" in deck and "dataset.deckError" in deck
    assert "dataset.dashboardReady" in dashboard and "dataset.dashboardError" in dashboard
    assert "dataset.figureReady" in figure and "dataset.figureError" in figure
    assert "dataset.reportReady" in report and "dataset.reportError" in report
    assert "render_token" in report and "/render-spec?" in report
    assert 'variant="print"' in report, "the viewer's print variant: every annex open"
    assert "window.print" not in report


def test_the_static_route_sits_outside_the_session_gate():
    app = (FRONTEND_SRC / "App.tsx").read_text(encoding="utf-8")
    route = app.split('path="/reports/:id/static"', 1)[1].split("<Route", 1)[0]
    assert "<ReportStaticRender />" in route
    assert "ProtectedRoute" not in route


# ── REST ──────────────────────────────────────────────────────────────────────


def test_export_endpoint_streams_the_pdf(monkeypatch):
    from app.api.v1 import reports as routes

    user_id = uuid.uuid4()
    report = _report(user_id)
    seen: dict = {}

    async def _fake_export(**kwargs):
        seen.update(kwargs)
        return ExportedFile("threat-brief.pdf", PDF_CONTENT_TYPE, b"%PDF-1.7 fake")

    monkeypatch.setattr(routes, "export_report", _fake_export)
    resp = _app(_FakeSession([_Result(report)]), _user(user_id)).post(f"/api/v1/reports/{report.id}/export")
    assert resp.status_code == 200, resp.text
    assert resp.content == b"%PDF-1.7 fake"
    assert resp.headers["content-type"].startswith("application/pdf")
    assert 'filename="threat-brief.pdf"' in resp.headers["content-disposition"]
    assert resp.headers["cache-control"] == "private, no-store"
    assert seen == {"report_id": report.id, "user_id": user_id, "title": "Threat brief"}


def test_export_endpoint_applies_the_read_rule(monkeypatch):
    from app.api.v1 import reports as routes

    async def _never(**kwargs):
        raise AssertionError("nothing is rendered for a caller who may not read the report")

    monkeypatch.setattr(routes, "export_report", _never)
    report = _report(uuid.uuid4())
    stranger = _user(uuid.uuid4())
    assert _app(_FakeSession([_Result(report)]), stranger).post(f"/api/v1/reports/{report.id}/export").status_code == 403
    assert _app(_FakeSession([_Result(None)]), stranger).post(f"/api/v1/reports/{report.id}/export").status_code == 404
    assert _app(_FakeSession(), stranger).post("/api/v1/reports/nope/export").status_code == 404


def test_export_endpoint_maps_failures(monkeypatch):
    from app.api.v1 import reports as routes

    user_id = uuid.uuid4()
    report = _report(user_id)

    async def _fails(**kwargs):
        raise ReportRenderError("The report did not finish rendering in time.")

    monkeypatch.setattr(routes, "export_report", _fails)
    resp = _app(_FakeSession([_Result(report)]), _user(user_id)).post(f"/api/v1/reports/{report.id}/export")
    assert resp.status_code == 502
    assert "did not finish" in resp.json()["detail"]

    async def _crashes(**kwargs):
        raise RuntimeError("Executable doesn't exist")

    monkeypatch.setattr(routes, "export_report", _crashes)
    resp = _app(_FakeSession([_Result(report)]), _user(user_id)).post(f"/api/v1/reports/{report.id}/export")
    assert resp.status_code == 503
    assert resp.json()["detail"] == routes.EXPORT_UNAVAILABLE_DETAIL


def test_the_export_budget_is_one_per_user_across_decks_and_reports(monkeypatch):
    import app.core.rate_limit as rate_limit
    from app.services import presentation_export

    seen: list = []

    async def _limit(request, name, **kwargs):
        seen.append((request, name, kwargs))

    monkeypatch.setattr(rate_limit, "enforce_rate_limit", _limit)
    user_id = uuid.uuid4()
    asyncio.run(presentation_export.enforce_export_rate_limit(user_id))
    assert seen == [
        (
            None,
            "render-export",
            {"limit": presentation_export.EXPORTS_PER_USER_PER_MINUTE, "window_seconds": 60, "bucket": str(user_id)},
        )
    ]


@pytest.mark.parametrize("subject", ["report", "presentation"])
def test_both_export_routes_spend_the_budget_and_hold_no_connection_while_rendering(monkeypatch, subject):
    """Past the budget nothing is loaded or rendered (the limiter's 429 stands);
    within it, the session's transaction is closed before the render starts, so
    a render waiting for its slot holds no pooled connection."""
    from fastapi import HTTPException

    from app.api.deps import get_current_user
    from app.api.v1 import presentations as deck_routes
    from app.api.v1 import reports as report_routes
    from app.core.database import get_db

    routes = report_routes if subject == "report" else deck_routes
    user_id = uuid.uuid4()
    events: list[str] = []

    class _Session(_FakeSession):
        def in_transaction(self) -> bool:
            return True

        async def commit(self):
            events.append("released")

    async def _refuse(uid):
        assert uid == user_id
        raise HTTPException(status_code=429, detail="Too many requests.")

    async def _render(**kwargs):
        events.append("rendered")
        return ExportedFile("x.pdf", PDF_CONTENT_TYPE, b"%PDF")

    deck = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        title="Deck",
        spec={},
        is_company_managed=False,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=False,
    )
    report = _report(user_id)
    results = [_Result(report)] if subject == "report" else [SimpleNamespace(scalars=lambda: SimpleNamespace(all=list)), _Result(deck)]
    session = _Session(results)
    app = FastAPI()
    app.include_router(routes.router, prefix="/x")

    async def _db():
        yield session

    async def _user_dep():
        return _user(user_id)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user_dep
    monkeypatch.setattr(routes, "export_report" if subject == "report" else "export_presentation", _render)
    target = report.id if subject == "report" else deck.id

    monkeypatch.setattr(routes, "enforce_export_rate_limit", _refuse)
    resp = TestClient(app).post(f"/x/{target}/export", json={"format": "pdf"})
    assert resp.status_code == 429
    assert session.statements == [] and events == []

    async def _admit(uid):
        return None

    monkeypatch.setattr(routes, "enforce_export_rate_limit", _admit)
    resp = TestClient(app).post(f"/x/{target}/export", json={"format": "pdf"})
    assert resp.status_code == 200, resp.text
    assert events == ["released", "rendered"]


def test_render_spec_requires_a_report_render_token():
    client = _app(_FakeSession())
    report_id = uuid.uuid4()
    assert client.get(f"/api/v1/reports/{report_id}/render-spec").status_code == 401
    for token in (create_access_token(str(uuid.uuid4())), mint_render_token(report_id, uuid.uuid4())):
        assert client.get(f"/api/v1/reports/{report_id}/render-spec", params={"render_token": token}).status_code == 401


def test_render_spec_refuses_a_token_for_another_report_before_any_lookup():
    session = _FakeSession()
    token = mint_report_render_token(uuid.uuid4(), uuid.uuid4())
    resp = _app(session).get(f"/api/v1/reports/{uuid.uuid4()}/render-spec", params={"render_token": token})
    assert resp.status_code == 403
    assert session.statements == []


def test_render_spec_serves_the_report_to_its_token_under_the_read_rule(monkeypatch):
    from app.api.v1 import reports as routes

    async def _theme(db, container_id, *, user_id, report_theme=None):
        return {}

    monkeypatch.setattr(routes, "resolve_report_theme", _theme)
    owner = uuid.uuid4()
    report = _report(owner)
    token = mint_report_render_token(report.id, owner)
    resp = _app(_FakeSession([_Result(_user(owner)), _Result(report)])).get(
        f"/api/v1/reports/{report.id}/render-spec", params={"render_token": token}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(report.id)
    assert resp.json()["spec"]["title"] == "Threat brief"

    stranger = uuid.uuid4()
    resp = _app(_FakeSession([_Result(_user(stranger)), _Result(report)])).get(
        f"/api/v1/reports/{report.id}/render-spec",
        params={"render_token": mint_report_render_token(report.id, stranger)},
    )
    assert resp.status_code == 403

    resp = _app(_FakeSession([_Result(_user(owner, active=False))])).get(
        f"/api/v1/reports/{report.id}/render-spec", params={"render_token": token}
    )
    assert resp.status_code == 401


# ── Tool ──────────────────────────────────────────────────────────────────────


def _tools(user_id: str | None = None):
    from app.services.builtin_tools import build_report_tools

    return build_report_tools(
        db=None,  # type: ignore[arg-type]
        user_id=user_id or str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )


def test_the_export_is_offered_on_every_turn():
    """Whether the user wants the file is the model's call."""
    definitions, executor = _tools()
    assert "export_report" in {d.name for d in definitions}
    assert "export_report" in executor._handlers


def _run(executor, arguments):
    from app.tools.base import ToolCall

    return asyncio.run(executor.execute_tool(ToolCall(id="c1", name="export_report", arguments=arguments)))


def test_the_tool_stores_the_pdf_and_names_its_file(monkeypatch):
    import app.core.database as database
    from app.services import reports
    from app.services.builtin_tools import _shared

    user_id = uuid.uuid4()
    report = _report(user_id)
    persisted: dict = {}

    @asynccontextmanager
    async def _session():
        yield _FakeSession()

    async def _load(db, report_id, *, user_id):
        return report if report_id == report.id else None

    async def _fake_export(**kwargs):
        return ExportedFile("threat-brief.pdf", PDF_CONTENT_TYPE, b"%PDF" + b"0" * 4096)

    async def _persist(**kw):
        persisted.update(kw)
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kw["filename"],
            content_type=kw["content_type"],
            size=len(kw["raw_bytes"]),
            s3_object_name=f"chat/conv/{kw['filename']}",
        )

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(reports, "load_owned_report", _load)
    monkeypatch.setattr(report_export, "export_report", _fake_export)
    monkeypatch.setattr(_shared, "_persist_generated_file", _persist)

    _defs, executor = _tools(str(user_id))
    result = _run(executor, {"report_id": str(report.id)})
    assert not result.is_error, result.content
    assert "Exported **Threat brief** as PDF: `threat-brief.pdf` (4 KB)." in result.content
    [ref] = result.files
    assert f"[[FILE:{ref.file_id}]]" in result.content
    assert persisted["content_type"] == PDF_CONTENT_TYPE
    assert persisted["user_id"] == str(user_id)

    missing = _run(executor, {"report_id": str(uuid.uuid4())})
    assert missing.is_error and "of yours was found" in missing.content


def test_exporting_a_report_already_exported_hands_back_its_file(monkeypatch):
    """A report nobody changed since its export is the same PDF; saved again, or
    its template edited (its palette, or only what bumps the container's
    updated_at), it renders anew."""
    import app.core.database as database
    from app.services import reports
    from app.services.builtin_tools import _shared

    user_id = uuid.uuid4()
    report = _report(user_id)
    renders: list[uuid.UUID] = []
    template = {"font_heading": "Geologica"}
    container_updated_at = ["2026-11-01T10:00:00+00:00"]

    @asynccontextmanager
    async def _session():
        yield _FakeSession()

    async def _load(db, report_id, *, user_id):
        return report if report_id == report.id else None

    async def _resolve_stamp(db, container_id, *, user_id, report_theme=None):
        return json.dumps({"palette": dict(template), "updated_at": container_updated_at[0]}, sort_keys=True)

    async def _fake_export(**kwargs):
        renders.append(kwargs["report_id"])
        return ExportedFile("threat-brief.pdf", PDF_CONTENT_TYPE, b"%PDF" + b"0" * 4096)

    async def _persist(**kw):
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kw["filename"],
            content_type=kw["content_type"],
            size=len(kw["raw_bytes"]),
            s3_object_name=f"chat/conv/{kw['filename']}",
        )

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(reports, "load_owned_report", _load)
    monkeypatch.setattr(reports, "resolve_report_export_stamp", _resolve_stamp)
    monkeypatch.setattr(report_export, "export_report", _fake_export)
    monkeypatch.setattr(_shared, "_persist_generated_file", _persist)

    _defs, executor = _tools(str(user_id))
    first = _run(executor, {"report_id": str(report.id)})
    again = _run(executor, {"report_id": str(report.id)})
    assert renders == [report.id]
    assert again.files == first.files
    assert "**Threat brief** has not changed since you exported it as PDF" in again.content
    assert f"[[FILE:{first.files[0].file_id}]]" in again.content

    report.revision += 1
    saved_since = _run(executor, {"report_id": str(report.id)})
    assert renders == [report.id, report.id]
    assert saved_since.files != first.files

    template["font_heading"] = "IBM Plex Sans"
    restyled = _run(executor, {"report_id": str(report.id)})
    assert renders == [report.id, report.id, report.id]
    assert restyled.files != saved_since.files

    container_updated_at[0] = "2026-11-01T10:05:00+00:00"
    rechromed = _run(executor, {"report_id": str(report.id)})
    assert renders == [report.id] * 4
    assert rechromed.files != restyled.files


def test_the_tool_refuses_bad_arguments_and_reports_render_failures(monkeypatch):
    import app.core.database as database
    from app.services import reports

    _defs, executor = _tools()
    assert "valid UUID" in _run(executor, {"report_id": "nope"}).content

    @asynccontextmanager
    async def _session():
        yield _FakeSession()

    async def _load(db, report_id, *, user_id):
        return _report(user_id, id=report_id)

    async def _fails(**kwargs):
        raise ReportRenderError("The report did not finish rendering in time.")

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(reports, "load_owned_report", _load)
    monkeypatch.setattr(report_export, "export_report", _fails)
    result = _run(executor, {"report_id": str(uuid.uuid4())})
    assert result.is_error
    assert result.content == "The report could not be exported: The report did not finish rendering in time."


# ── Frontend ──────────────────────────────────────────────────────────────────


def test_the_card_and_the_viewer_export_through_the_rest_route():
    menu = (FRONTEND_SRC / "components" / "report" / "ReportExportMenu.tsx").read_text(encoding="utf-8")
    hooks = (FRONTEND_SRC / "hooks" / "api" / "useReportApi.ts").read_text(encoding="utf-8")
    card = (FRONTEND_SRC / "components" / "report" / "ReportCard.tsx").read_text(encoding="utf-8")
    viewer = (FRONTEND_SRC / "pages" / "ReportViewer.tsx").read_text(encoding="utf-8")
    assert "api.postDownload(`/reports/${id}/export`)" in hooks
    assert "useExportReport()" in menu and "withBasePath(`/reports/${reportId}?print`)" in menu
    assert "<ReportExportMenu" in card and "<ReportExportMenu" in viewer
    assert "print:hidden" in viewer, "the menu never lands on paper"
    for catalog in ("en", "fr", "es", "ja"):
        strings = (FRONTEND_SRC / "i18n" / "lang" / f"{catalog}.json").read_text(encoding="utf-8")
        assert '"Export report"' in strings, catalog
        assert '"The platform could not render the report. Try again or use Print to PDF."' in strings, catalog

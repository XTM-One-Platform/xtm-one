"""The frontend build a page runs vs the one the server serves.

A tab opened before an upgrade keeps running the previous bundle, which
misreads what the new server sends it (a relayed turn, read as
the tab's own) until it is reloaded. The served ``index.html`` names its
build in a ``<meta name="build-id">`` and ``GET /auth/config`` reports the
build the server serves now; the page compares the two
(``frontend/src/lib/build-version.ts``) and offers a reload.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import branding
from app.services.branding import (
    BUILD_ID_META_NAME,
    Branding,
    frontend_build_id,
    get_frontend_build_id,
    render_branded_index_html,
)

_BUILT = (
    '<!doctype html><html><head><meta charset="UTF-8" />'
    "<title>x</title>"
    '<script type="module" crossorigin src="./assets/index-abc.js"></script>'
    '<link rel="stylesheet" crossorigin href="./assets/index-ghi.css" />'
    '</head><body><div id="root"></div></body></html>'
)


def _render(html: str) -> str:
    return render_branded_index_html(html, branding=Branding(name="Acme"), is_xtm=True, base_path="")


def test_the_served_index_names_the_build_of_its_source():
    rendered = _render(_BUILT)
    tag = f'<meta name="{BUILD_ID_META_NAME}" content="{frontend_build_id(_BUILT)}" />'
    assert tag in rendered
    assert rendered.count(f'name="{BUILD_ID_META_NAME}"') == 1
    # Next to the base-path meta, in the head the SPA reads before it boots.
    assert rendered.index('name="base-path"') < rendered.index(tag) < rendered.index("</head>")


def test_the_build_id_changes_with_the_bundle_and_only_with_it():
    rebuilt = _BUILT.replace("index-abc.js", "index-xyz.js")
    assert frontend_build_id(rebuilt) != frontend_build_id(_BUILT)
    assert frontend_build_id(_BUILT) == frontend_build_id(str(_BUILT))
    # Re-rendering (a branding change) never moves it: it names the source.
    first = _render(_BUILT)
    again = render_branded_index_html(
        _BUILT, branding=Branding(name="Other"), is_xtm=True, base_path="/xtm-one"
    )
    assert f'content="{frontend_build_id(_BUILT)}"' in first
    assert f'content="{frontend_build_id(_BUILT)}"' in again


def test_a_source_already_carrying_a_build_id_ends_up_with_one():
    tagged = _BUILT.replace(
        '<meta charset="UTF-8" />', f'<meta charset="UTF-8" /><meta name="{BUILD_ID_META_NAME}" content="stale" />'
    )
    rendered = _render(tagged)
    assert rendered.count(f'name="{BUILD_ID_META_NAME}"') == 1
    assert 'content="stale"' not in rendered


def test_the_server_reports_the_build_it_serves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(branding, "_index_html_content", "")
    monkeypatch.setattr(branding, "_index_html_frontend_dir", None)
    monkeypatch.setattr(branding, "_index_html_branding", None)
    assert get_frontend_build_id() == "", "no built frontend, no build to compare"
    (tmp_path / "index.html").write_text(_BUILT, encoding="utf-8")
    branding.configure_branded_index_html(tmp_path)
    assert get_frontend_build_id() == frontend_build_id(_BUILT)


def test_auth_config_carries_the_build_id(monkeypatch: pytest.MonkeyPatch):
    from dataclasses import replace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.v1 import auth as auth_api
    from app.core.database import get_db
    from app.core.sso_helpers import ResolvedSSOConfig

    async def _get_db():
        yield object()

    async def _get_sso_config(_db):
        return replace(ResolvedSSOConfig(), local_auth_enabled=True)

    monkeypatch.setattr("app.core.sso_helpers.get_sso_config", _get_sso_config)
    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api/v1/auth")
    app.dependency_overrides[get_db] = _get_db
    client = TestClient(app)

    monkeypatch.setattr(branding, "_index_html_content", _render(_BUILT))
    assert client.get("/api/v1/auth/config").json()["build_id"] == frontend_build_id(_BUILT)
    monkeypatch.setattr(branding, "_index_html_content", "")
    assert client.get("/api/v1/auth/config").json()["build_id"] == ""

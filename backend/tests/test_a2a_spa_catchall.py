"""The SPA catch-all must never answer an A2A path with HTML.

This is the one defect in the A2A card surface that cannot be reproduced
against a test double: it only appears once the SPA catch-all is installed,
which never happens in the normal test app. ``_install_spa_catchall`` is
factored as a standalone function precisely so this test can install it on a
throwaway app.

If ``"a2a"`` is missing from ``_API_NAMESPACES``, every unknown or revoked
card token returns HTTP 200 + index.html — a partner's registration tooling
fails on parse, and the uniform not-found the specification requires becomes
distinguishable from a real 404.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import _API_NAMESPACES, _install_spa_catchall


@pytest.fixture
def spa_app(tmp_path):
    """A throwaway app with the SPA catch-all installed over a temp frontend."""
    (tmp_path / "index.html").write_text("<html><body>spa</body></html>")
    app = FastAPI()
    _install_spa_catchall(app, tmp_path)
    return app


def test_a2a_is_a_machine_facing_namespace():
    assert "a2a" in _API_NAMESPACES


@pytest.mark.parametrize(
    "path",
    [
        "/a2a/unknown-token/.well-known/agent-card.json",
        "/a2a/unknown-token/jsonrpc",
        "/a2a",
        "/a2a/",
    ],
)
def test_unknown_a2a_path_returns_json_404_not_html(spa_app, path):
    resp = TestClient(spa_app).get(path)
    assert resp.status_code == 404, f"{path} fell through to the SPA"
    assert "text/html" not in resp.headers.get("content-type", "")
    assert resp.json() == {"detail": "Not Found"}


def test_non_api_path_still_serves_the_spa(spa_app):
    """Guard against over-broadening: ordinary routes must still reach the SPA."""
    resp = TestClient(spa_app).get("/agents/some-id")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")

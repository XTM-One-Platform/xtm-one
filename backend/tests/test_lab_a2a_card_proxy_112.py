"""The lab preview serves A2A agent cards on its own origin (#112).

``/a2a/<token>/.well-known/agent-card.json`` fell to the SPA on the Vite dev server (200 ``index.html``) and the
backend port is out of the browser's reach, so a reviewer could not read a card in the lab. The dev server now
proxies ``^/a2a/`` to the backend with the browser's Host kept, so ``external_base`` writes the card's URLs on
the preview origin. dev-docs/reference/agentic-labs.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VITE = ROOT / "frontend" / "vite.config.ts"
DOCS = ROOT / "dev-docs" / "reference" / "agentic-labs.md"

pytestmark = pytest.mark.skipif(not VITE.is_file(), reason="no frontend next to the backend")


def _block(config: str, key: str) -> str:
    block = config[config.index(f'"{key}": {{') :]
    return block[: block.index("}")]


def test_the_preview_proxies_a2a_cards_to_the_lab_backend_with_the_browser_host():
    block = _block(VITE.read_text(encoding="utf-8"), "^/a2a/")
    assert "VITE_API_URL" in block
    # The Host kept: the card's tokenUrl names the origin the browser used, not 127.0.0.1:8100.
    assert "changeOrigin" not in block


def test_the_token_url_the_card_names_is_proxied_too():
    config = VITE.read_text(encoding="utf-8")
    assert "VITE_API_URL" in _block(config, "^/mcp(/|$)")


def test_the_lab_docs_name_the_card_on_the_preview_origin():
    if not DOCS.is_file():
        pytest.skip("no dev-docs next to the backend")
    text = DOCS.read_text(encoding="utf-8")
    assert "issue #112" in text and "/a2a/<token>/.well-known/agent-card.json" in text

"""A conversation switched away from stops downloading its pictures.

The authenticated image fetches abort on unmount: the shared hook and the chat
Markdown's own image both tie their request to an ``AbortController``. Like
``test_presentation_studio.py``, this reads the frontend source and pins the
shape of the fix.
"""

from __future__ import annotations

from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"


def _read(rel: str) -> str:
    return (FRONTEND_ROOT / rel).read_text(encoding="utf-8")


def _section(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def test_authenticated_image_fetches_abort_on_unmount():
    hook = _read("src/hooks/useAuthenticatedSrc.ts")
    assert "const controller = new AbortController();" in hook
    assert "signal: controller.signal" in hook
    assert "controller.abort();" in hook

    markdown = _read("src/components/chat/ChatMarkdown.tsx")
    image = _section(markdown, "function AuthenticatedImage(", "if (errored) {")
    assert "const controller = new AbortController();" in image
    assert "signal: controller.signal" in image
    assert "controller.abort();" in image

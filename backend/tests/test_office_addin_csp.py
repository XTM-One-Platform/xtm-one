"""The Office add-in's page policy admits the ``@filigran/chatbot`` panel - ``uv run pytest``.

The task pane renders the shared chat panel: its turns and event stream are
same-origin ``fetch`` streams to ``/api/v1/web/chat``, its stylesheet is
bundled with the add-in and sets inline styles, an answer's images may be
``data:``, read aloud plays a ``blob:`` audio, and the add-in loads its fonts
from Google Fonts. The policy is report-only, so a gap would never
break the pane, only flood the reports; this pins what the panel needs.
"""

from __future__ import annotations

import pytest

from app.core import csp
from app.core.csp import page_policy_for


def _office_directives() -> dict[str, list[str]]:
    import app.main  # noqa: F401 - registers every page policy

    policy = page_policy_for("/office/index.html")
    assert policy is not None
    built = csp.build_policy({"type": "http", "headers": []}, page=policy)
    parsed: dict[str, list[str]] = {}
    for part in built.split(";"):
        tokens = part.split()
        if tokens:
            parsed[tokens[0]] = tokens[1:]
    return parsed


@pytest.mark.parametrize(
    ("directive", "source"),
    [
        ("connect-src", "'self'"),
        ("style-src", "'self'"),
        ("style-src", "'unsafe-inline'"),
        ("img-src", "data:"),
        ("img-src", "blob:"),
        ("media-src", "blob:"),
        ("font-src", "'self'"),
        ("font-src", "data:"),
        ("font-src", "https://fonts.gstatic.com"),
        ("style-src", "https://fonts.googleapis.com"),
    ],
)
def test_the_office_page_policy_admits_the_chat_panel(directive, source):
    assert source in _office_directives()[directive]


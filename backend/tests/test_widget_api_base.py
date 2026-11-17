"""The embeddable chat widget finds the public API from its own script URL.

The embed snippet loads ``<platform>/api/v1/public/widget.js``. Under a URL
path prefix the API is ``<platform>/<prefix>/api/v1/public``. A copy of
the script served from any other path - behind the embedding site's own proxy -
keeps the API at its origin's ``/api/v1/public``, as it always did.

The derivation is the ``api-base`` block of ``widget.js``, run here by Node
exactly as the browser runs it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

WIDGET_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "widget.js"


def _node() -> str:
    node = shutil.which("node")
    if not node:
        if os.environ.get("CI"):
            pytest.fail("node is not installed on the CI runner; the Backend job must set it up")
        pytest.skip("node is not installed")
    return node


def _api_block() -> str:
    source = WIDGET_JS.read_text(encoding="utf-8")
    start = source.index("// api-base:start")
    end = source.index("// api-base:end")
    return source[start:end]


def _api_bases(script_srcs: list[str]) -> list[str]:
    program = (
        "const results = [];\n"
        f"for (const src of {json.dumps(script_srcs)}) {{\n"
        "  const script = { src };\n"
        f"{_api_block()}\n"
        "  results.push(API);\n"
        "}\n"
        "console.log(JSON.stringify(results));\n"
    )
    result = subprocess.run([_node(), "-e", program], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("script_src", "api"),
    [
        ("https://xtm.example.com/api/v1/public/widget.js", "https://xtm.example.com/api/v1/public"),
        ("https://xtm.example.com/api/v1/public/widget.js?v=3", "https://xtm.example.com/api/v1/public"),
        ("http://localhost:4000/api/v1/public/widget.js", "http://localhost:4000/api/v1/public"),
        (
            "https://intranet.example.com/xtm-one/api/v1/public/widget.js",
            "https://intranet.example.com/xtm-one/api/v1/public",
        ),
        (
            "https://intranet.example.com/tools/xtm-one/api/v1/public/widget.js",
            "https://intranet.example.com/tools/xtm-one/api/v1/public",
        ),
    ],
)
def test_the_script_served_by_the_platform_gives_its_api(script_src, api):
    assert _api_bases([script_src]) == [api]


@pytest.mark.parametrize(
    "script_src",
    [
        "https://www.customer.example/chat/widget.js",
        "https://www.customer.example/assets/xtm-widget.js",
        "https://www.customer.example/widget.js",
    ],
)
def test_a_copy_served_from_another_path_keeps_the_origins_api(script_src):
    assert _api_bases([script_src]) == ["https://www.customer.example/api/v1/public"]


def test_the_widget_still_parses():
    result = subprocess.run([_node(), "--check", str(WIDGET_JS)], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stderr


def test_the_widget_panel_carries_the_ai_disclaimer_between_input_and_footer():
    """The notice sits after the input area and before the "Powered by" footer."""
    source = WIDGET_JS.read_text(encoding="utf-8")
    assert source.count("AI can make mistakes. Check important information.") == 1
    input_area = source.index('class="fc-input-area"')
    disclaimer = source.index('class="fc-disclaimer"')
    footer = source.index('class="fc-footer"')
    assert input_area < disclaimer < footer

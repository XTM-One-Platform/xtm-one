"""The API reference page loads one pinned, integrity-checked Scalar bundle and
turns Scalar's telemetry off."""

import asyncio
import json
import re

from app.main import SCALAR_JS_INTEGRITY, SCALAR_JS_URL, scalar_api_reference


def _page() -> str:
    return asyncio.run(scalar_api_reference()).body.decode("utf-8")


def test_the_bundle_is_one_exact_release_file():
    assert re.fullmatch(
        r"https://cdn\.jsdelivr\.net/npm/@scalar/api-reference@\d+\.\d+\.\d+/dist/browser/standalone\.js",
        SCALAR_JS_URL,
    )
    assert re.fullmatch(r"sha384-[A-Za-z0-9+/]{64}", SCALAR_JS_INTEGRITY)


def test_the_page_checks_the_bundle_against_its_hash():
    # If a scalar_fastapi upgrade changes how it writes the tag, the integrity
    # rewrite silently stops applying: this is what notices.
    page = _page()
    scripts = re.findall(r"<script[^>]*\bsrc=[^>]*>", page)
    assert scripts == [
        f'<script src="{SCALAR_JS_URL}" integrity="{SCALAR_JS_INTEGRITY}" crossorigin="anonymous">'
    ]


def test_telemetry_is_off():
    page = _page()
    config = re.search(r"Scalar\.createApiReference\(\"#app\", (\{.*\})\)", page)
    assert config is not None
    assert json.loads(config.group(1))["telemetry"] is False

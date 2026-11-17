"""Every content-rendering headless Chromium closes the channels its route cannot see.

The deck export / layout check / slide capture, ``render_html_to_image`` and
the PDF renderer abort every request that leaves the page; a DNS pre-resolution
and a service worker sit outside that filter, so each launch passes
``RENDER_CHROMIUM_ARGS`` and each context ``RENDER_SERVICE_WORKERS``. The scan
covers every browser launch in every module of ``app/`` that imports
Playwright, found on the syntax tree (so neither a multi-line call, an aliased
browser type nor ``launch_persistent_context()`` escapes it), so a new renderer
cannot skip them. The agent ``browser`` tool and the Video Studio recording
browser, which browse the web on purpose behind the outbound URL
policy and the browser-level redirect guard, are the exemptions; the Video
Studio compositor is a renderer like the others.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.headless_render import RENDER_CHROMIUM_ARGS, RENDER_SERVICE_WORKERS

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

#: Browsers that are not content renderers and keep their own options.
EXEMPT = {"services/browser_tool.py", "services/browser_profiles/browser.py"}

#: The renderers the options exist for: each must still be found by the scan.
RENDERERS = {
    "services/presentation_export.py",
    "services/builtin_tools/html_render.py",
    "services/pdf_render.py",
    "services/video_studio/compositor.py",
    "services/video_studio/quality.py",
}


def _calls(tree: ast.AST, attribute: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attribute
    ]


def _keyword(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return ast.unparse(keyword.value)
    return None


def _passes_render_args(call: ast.Call) -> bool:
    """``args=list(RENDER_CHROMIUM_ARGS)``, or a list that unpacks them first and adds flags of its own after."""
    for keyword in call.keywords:
        if keyword.arg != "args":
            continue
        value = keyword.value
        if ast.unparse(value) == "list(RENDER_CHROMIUM_ARGS)":
            return True
        return (
            isinstance(value, ast.List)
            and bool(value.elts)
            and isinstance(value.elts[0], ast.Starred)
            and ast.unparse(value.elts[0].value) == "RENDER_CHROMIUM_ARGS"
        )
    return False


def _imports_playwright(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "playwright" for alias in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "playwright":
            return True
    return False


def _launch_sites() -> dict[str, ast.Module]:
    """Every module of ``app/`` that imports Playwright, parsed (the import is what no renderer can avoid)."""
    sites: dict[str, ast.Module] = {}
    for path in APP_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "playwright" not in source:
            continue
        tree = ast.parse(source)
        if _imports_playwright(tree):
            sites[path.relative_to(APP_ROOT).as_posix()] = tree
    return sites


def test_the_options_are_the_ones_documented():
    assert RENDER_CHROMIUM_ARGS == ("--dns-prefetch-disable",)
    assert RENDER_SERVICE_WORKERS == "block"


def test_the_scan_finds_launches_however_they_are_written():
    tree = ast.parse(
        "from playwright.async_api import async_playwright\n"
        "async def f(pw):\n"
        "    kind = pw.chromium\n"
        "    browser = await kind.launch(\n"
        "        headless=True,\n"
        "    )\n"
        "    ctx = await pw.chromium.launch_persistent_context('/tmp/x')\n"
    )
    assert _imports_playwright(tree)
    assert len(_calls(tree, "launch")) == 1
    assert len(_calls(tree, "launch_persistent_context")) == 1


def test_the_render_args_must_be_seen_at_the_launch():
    def launch(source: str) -> ast.Call:
        return _calls(ast.parse(source), "launch")[0]

    assert _passes_render_args(launch("pw.chromium.launch(args=list(RENDER_CHROMIUM_ARGS))"))
    assert _passes_render_args(launch("pw.chromium.launch(args=[*RENDER_CHROMIUM_ARGS, *extra])"))
    assert not _passes_render_args(launch("pw.chromium.launch(args=args)"))
    assert not _passes_render_args(launch("pw.chromium.launch(args=[*extra, *RENDER_CHROMIUM_ARGS])"))
    assert not _passes_render_args(launch("pw.chromium.launch(args=[])"))
    assert not _passes_render_args(launch("pw.chromium.launch(headless=True)"))


def test_every_content_rendering_chromium_disables_dns_prefetch_and_service_workers():
    sites = _launch_sites()
    missing = RENDERERS - set(sites)
    assert not missing, f"a renderer no longer imports Playwright: {missing}"
    for name, tree in sites.items():
        if name in EXEMPT:
            continue
        launches = _calls(tree, "launch")
        persistent = _calls(tree, "launch_persistent_context")
        contexts = _calls(tree, "new_context")
        if name in RENDERERS:
            assert launches or persistent, f"{name}: no browser launch found"
        if launches:
            assert contexts, f"{name}: launches a browser but opens no context with the render options"
        for call in [*launches, *persistent]:
            assert _passes_render_args(call), f"{name}: {ast.unparse(call)}"
        for call in [*contexts, *persistent]:
            assert _keyword(call, "service_workers") == "RENDER_SERVICE_WORKERS", f"{name}: {ast.unparse(call)}"


def test_the_agent_browser_tool_keeps_its_own_options():
    tree = _launch_sites()["services/browser_tool.py"]
    for call in _calls(tree, "new_context"):
        assert _keyword(call, "service_workers") is None

# SPDX-License-Identifier: Apache-2.0

"""The Documentation link of every health finding opens a section the public docs have (#1348).

Before #1348 the checks stored ``/docs/administration/health#...``, a path the
browser resolved against the platform host, which has no ``/docs`` route: every
link answered "Page not found". A link is now absolute, on the public
documentation, and names a page of ``docs/docs`` and one of its headings; a
link stored before is resolved when the findings API reads it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.docs_links import DOCS_BASE_URL, absolute_docs_link, docs_url
from app.services.health import agent_checks, openaev_checks, opencrq_checks, opencti_checks, platform_checks

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs" / "docs"

CHECK_MODULES = (agent_checks, openaev_checks, opencrq_checks, opencti_checks, platform_checks)


def _anchors(page: Path) -> set[str]:
    """The MkDocs anchors of the headings of ``page`` (toc slugify: lower case, words joined by ``-``)."""
    anchors: set[str] = set()
    for line in page.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6} (.+?)\s*$", line)
        if match:
            text = re.sub(r"[^\w\s-]", "", match.group(1)).strip().lower()
            anchors.add(re.sub(r"[-\s]+", "-", text))
    return anchors


def _page_and_anchor(url: str) -> tuple[Path, str]:
    assert url.startswith(DOCS_BASE_URL), url
    path, _, anchor = url.removeprefix(DOCS_BASE_URL).partition("#")
    return DOCS_DIR / f"{path.strip('/')}.md", anchor


@pytest.mark.parametrize("module", CHECK_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_every_check_links_a_heading_the_docs_have(module):
    page, anchor = _page_and_anchor(module.DOC_LINK)
    assert page.is_file(), f"{module.__name__}.DOC_LINK names a page docs/docs does not have: {page}"
    assert anchor, f"{module.__name__}.DOC_LINK names no section"
    assert anchor in _anchors(page), f"{module.__name__}.DOC_LINK names #{anchor}, not a heading of {page.name}"


@pytest.mark.parametrize("module", CHECK_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_no_check_links_a_path_of_the_platform_host(module):
    assert module.DOC_LINK.startswith("https://"), module.DOC_LINK


def test_docs_url_is_the_mkdocs_directory_form():
    assert docs_url("administration/health", "openaev-checks") == (
        "https://docs.xtmone.io/latest/administration/health/#openaev-checks"
    )
    assert docs_url("administration/health.md") == "https://docs.xtmone.io/latest/administration/health/"


@pytest.mark.parametrize(
    ("stored", "served"),
    [
        (
            "/docs/administration/health#openaev-checks",
            "https://docs.xtmone.io/latest/administration/health/#openaev-checks",
        ),
        (
            "/docs/administration/platform-health#agent-checks",
            "https://docs.xtmone.io/latest/administration/health/#agent-checks",
        ),
        ("", ""),
        (
            "https://docs.xtmone.io/latest/administration/health/#platform-checks",
            "https://docs.xtmone.io/latest/administration/health/#platform-checks",
        ),
    ],
)
def test_a_link_stored_before_1348_is_served_absolute(stored, served):
    assert absolute_docs_link(stored) == served


def test_every_legacy_link_resolves_to_a_heading_the_docs_have():
    for legacy in (
        "/docs/administration/health#platform-checks",
        "/docs/administration/health#opencti-checks",
        "/docs/administration/health#openaev-checks",
        "/docs/administration/health#opencrq-checks",
        "/docs/administration/platform-health#agent-checks",
    ):
        page, anchor = _page_and_anchor(absolute_docs_link(legacy))
        assert anchor in _anchors(page), legacy

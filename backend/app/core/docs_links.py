# SPDX-License-Identifier: Apache-2.0

"""Links from the product to the public XTM One documentation.

The documentation is served at :data:`DOCS_BASE_URL` (MkDocs, ``docs/``), never
by the platform itself: a link rendered in the UI must be absolute, or the
browser resolves it against the platform host, which has no ``/docs`` route
(#1348). :func:`docs_url` builds one from a page of ``docs/docs`` and a heading
anchor; :func:`absolute_docs_link` resolves a link stored before #1348 as a
path relative to the platform host (``/docs/<page>#<anchor>``).
"""

from __future__ import annotations

#: Where the public documentation of the latest release is served.
DOCS_BASE_URL = "https://docs.xtmone.io/latest/"
#: The prefix of a documentation link stored relative to the platform host before #1348.
_LEGACY_PREFIX = "/docs/"
#: Pages a legacy link named that the documentation has under another path.
_RENAMED_PAGES: dict[str, str] = {"administration/platform-health": "administration/health"}


def docs_url(page: str, anchor: str = "") -> str:
    """The public URL of ``page`` (a path of ``docs/docs`` without ``.md``), at ``anchor`` when given.

    MkDocs serves a page as a directory: ``administration/health`` is
    ``<base>administration/health/``, its heading ``OpenAEV checks`` is
    ``#openaev-checks``.
    """
    path = page.strip("/").removesuffix(".md")
    url = f"{DOCS_BASE_URL}{path}/" if path else DOCS_BASE_URL
    return f"{url}#{anchor}" if anchor else url


def absolute_docs_link(link: str) -> str:
    """``link`` as an absolute documentation URL; a link that is not a legacy relative one is kept."""
    if not link.startswith(_LEGACY_PREFIX):
        return link
    page, _, anchor = link.removeprefix(_LEGACY_PREFIX).partition("#")
    page = page.strip("/")
    return docs_url(_RENAMED_PAGES.get(page, page), anchor)

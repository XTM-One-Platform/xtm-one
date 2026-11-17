# SPDX-License-Identifier: Apache-2.0

"""Platform links an external surface can click (#10115).

An agent often names a page of the platform the way it reads in an address
bar, ``copilot.example.ai/presentations/<id>``: no scheme. The web chat links
it; Teams, a mail or WhatsApp show it as plain text the reader cannot open.
:func:`bare_platform_links_as_urls` gives every bare reference to the
platform's own host (``FRONTEND_URL``, ``BASE_URL``) its configured scheme and
writes it as a Markdown link, which the Teams and mail renderers turn into an
anchor.

WhatsApp draws no Markdown: a ``[title](url)`` link (the shape the artifact
marker resolvers and the step above produce) reaches the reader with its
brackets. :func:`markdown_links_as_plain_urls` writes it ``title: url``, the
full URL WhatsApp links by itself.

Both are pure and deterministic. Fenced code, inline code, a URL that already
carries a scheme, a well-formed Markdown link and an e-mail address are left
exactly as written; a host that only starts like the platform's
(``copilot.example.ai.other.net``) is not the platform's and is left alone.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterable
from urllib.parse import urlparse

#: A fence line (``` or ~~~, up to three spaces of indent).
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
#: An inline code span: a backtick run, the shortest content, the same run.
_INLINE_CODE_RE = re.compile(r"(`+).*?\1")
#: A Markdown link or image, kept whole.
_MD_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)")
#: A URL that already carries a scheme (``https://``, ``mailto:`` excluded
#: since it has no ``//``), an angle-bracket autolink included.
_SCHEME_URL_RE = re.compile(r"<?[a-zA-Z][a-zA-Z0-9+.-]*://[^\s>]*>?")
#: A Markdown link (not an image) whose destination is an absolute http(s) URL.
_HTTP_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]*)\]\((https?://[^)\s]+)\)")
#: Sentence punctuation peeled off the end of a bare path.
_TRAILING_PUNCT = ".,;:!?"
#: A body already written as HTML (an Outlook or Gmail body with tags): a
#: bare link is written as an anchor there, a Markdown link would read raw.
_HTML_BODY_RE = re.compile(r"<(?:p|div|br|a|table|ul|ol|li|span|h[1-6]|html|body)\b", re.IGNORECASE)


def _mask(text: str, patterns: Iterable[re.Pattern[str]]) -> tuple[str, list[str]]:
    """*text* with every match of *patterns* replaced by a placeholder, and the originals."""
    kept: list[str] = []

    def _hold(match: re.Match[str]) -> str:
        kept.append(match.group(0))
        return f"\x00{len(kept) - 1}\x00"

    for pattern in patterns:
        text = pattern.sub(_hold, text)
    return text, kept


def _unmask(text: str, kept: list[str]) -> str:
    """*text* with the placeholders of :func:`_mask` put back, latest first."""
    for idx in range(len(kept) - 1, -1, -1):
        text = text.replace(f"\x00{idx}\x00", kept[idx])
    return text


def _outside_fences(text: str, transform: Callable[[str], str]) -> str:
    """*text* with *transform* applied to each line outside a fenced code block."""
    lines = text.split("\n")
    fence: str | None = None
    for idx, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                continue
            lines[idx] = transform(line)
        elif match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
            fence = None
    return "\n".join(lines)


def platform_origins(*urls: str) -> tuple[tuple[str, str], ...]:
    """The distinct ``(scheme, host)`` of the configured platform *urls*, longest host first."""
    seen: dict[str, str] = {}
    for url in urls:
        try:
            parsed = urlparse((url or "").strip())
        except ValueError:
            continue
        if parsed.scheme in ("http", "https") and parsed.netloc:
            seen.setdefault(parsed.netloc.lower(), parsed.scheme)
    return tuple(sorted(((scheme, host) for host, scheme in seen.items()), key=lambda o: -len(o[1])))


@functools.lru_cache(maxsize=8)
def _bare_host_re(origins: tuple[tuple[str, str], ...]) -> re.Pattern[str]:
    hosts = "|".join(re.escape(host) for _, host in origins)
    # Not preceded by what continues a host, a path, a scheme or an address;
    # not followed by what would make it another host (``.other.net``).
    return re.compile(
        rf"(?<![\w.@/:\\=\"'>-])({hosts})(?![\w-]|\.[\w-])(/[^\s<>\[\]()`'\"]*)?",
        re.IGNORECASE,
    )


def bare_platform_links_as_urls(text: str, origins: tuple[tuple[str, str], ...] | None = None) -> str:
    """*text* with every bare reference to the platform's host written as a clickable link.

    ``copilot.example.ai/presentations/abc`` becomes
    ``[https://copilot.example.ai/presentations/abc](https://copilot.example.ai/presentations/abc)``,
    with the scheme the platform is configured on; in a body already written
    as HTML it becomes ``<a href="url">url</a>``. A host inside an attribute
    or an anchor's text is left alone. *origins* defaults to the configured
    ``FRONTEND_URL`` and ``BASE_URL`` (:func:`platform_origins`).
    """
    if not text:
        return text
    if origins is None:
        from app.core.config import settings

        origins = platform_origins(settings.frontend_url, settings.base_url)
    if not origins or not any(host in text.lower() for _, host in origins):
        return text
    schemes = {host: scheme for scheme, host in origins}
    host_re = _bare_host_re(origins)
    as_html = _HTML_BODY_RE.search(text) is not None

    def _link(match: re.Match[str]) -> str:
        host, path = match.group(1), match.group(2) or ""
        trailing = ""
        while path and path[-1] in _TRAILING_PUNCT:
            trailing = path[-1] + trailing
            path = path[:-1]
        url = f"{schemes[host.lower()]}://{host}{path}"
        if as_html:
            return f'<a href="{url}">{url}</a>{trailing}'
        return f"[{url}]({url}){trailing}"

    def _line(line: str) -> str:
        masked, kept = _mask(line, (_INLINE_CODE_RE, _MD_LINK_RE, _SCHEME_URL_RE))
        return _unmask(host_re.sub(_link, masked), kept)

    return _outside_fences(text, _line)


def markdown_links_as_plain_urls(text: str) -> str:
    """*text* with every ``[label](https://...)`` link written as WhatsApp links it.

    A link whose label is its URL (or empty) becomes the URL alone; any other
    becomes ``label: url``. Images, code and every other character are kept.
    """
    if not text or "](" not in text:
        return text

    def _plain(match: re.Match[str]) -> str:
        label, url = match.group(1).strip(), match.group(2)
        if not label or label == url:
            return url
        return f"{label}: {url}"

    def _line(line: str) -> str:
        masked, kept = _mask(line, (_INLINE_CODE_RE,))
        return _unmask(_HTTP_MD_LINK_RE.sub(_plain, masked), kept)

    return _outside_fences(text, _line)

# SPDX-License-Identifier: Apache-2.0

"""The identity an http(s) URL names, compared the RFC 3986 way.

A product of the XTM Suite is known by a URL: the ``iss`` of the tokens it
signs, the ``aud`` of the tokens addressed to it, the ``platform_url`` it
registers with. One URL has several spellings - ``HTTPS://OpenCTI.example:443/``
and ``https://opencti.example`` name the same resource - so an identity is
compared in its canonical form (RFC 3986 section 6.2.2 case normalization of
the scheme and host, section 6.2.3 removal of the default port, and the
trailing slash of the path), never as a raw string. The path keeps its case.

A URL carrying userinfo names no identity (dropping it would make
``https://alice@host`` and ``https://bob@host`` one), nor does one on port 0,
which would otherwise read as the default port. ``lenient=True`` is the reading
of a URL an operator configured for this platform itself (``BASE_URL``, a run's
callback base URL): it has always dropped userinfo, and still does.

Standard library only: ``app.core.config`` parses ``XTM_AUTH_ACCEPTED_AUDIENCES``
through it while the settings are being built.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}
_HTTP_SCHEME_PREFIX = re.compile(r"^\s*https?:/*", re.IGNORECASE)


class URLSettingError(Exception):
    """A URL setting that cannot be used, worded without the value's credentials.

    Deliberately not a ``ValueError``: pydantic wraps a ``ValueError`` raised by
    a validator in a ``ValidationError`` that quotes the raw input, so a
    password typed into the setting would be printed at startup.
    """


def redact_url(value: object) -> str:
    """*value* as a log line or an error may quote it: no userinfo, query or fragment.

    Written for malformed input as much as for URLs: everything before the
    LAST ``@`` is masked (a password may itself hold ``/``, ``?``, ``#`` or
    ``:``, and a missing or mangled ``//`` must not expose it), keeping only a
    leading ``http:`` / ``https:``.
    """
    text = str(value).strip()
    if "@" in text:
        prefix = _HTTP_SCHEME_PREFIX.match(text)
        head = prefix.group(0).strip() if prefix and prefix.end() <= text.rfind("@") else ""
        text = f"{head}***@{text.rpartition('@')[2]}"
    cut = [index for index in (text.find("?"), text.find("#")) if index >= 0]
    if cut:
        text = f"{text[: min(cut)]}?***"
    return text


def normalize_url_identity(url: str, *, lenient: bool = False) -> str:
    """The canonical ``scheme://host[:port][/path]`` of an http(s) URL.

    Raises ``ValueError`` for anything that is not an http(s) URL with a host,
    and for a URL carrying a query string or a fragment. Unless *lenient*, a
    URL carrying userinfo or naming port 0 is refused too. Messages never quote
    the URL's credentials.
    """
    parsed = urlsplit(url.strip())

    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError(f"Unsupported URL scheme: {scheme!r} (expected http or https)")

    if not parsed.hostname:
        raise ValueError(f"URL missing hostname: {redact_url(url)!r}")

    if parsed.fragment:
        raise ValueError(f"URL must not contain a fragment: {redact_url(url)!r}")

    if parsed.query:
        raise ValueError(f"URL must not contain a query string: {redact_url(url)!r}")

    if not lenient and (parsed.username is not None or parsed.password is not None):
        raise ValueError(f"URL must not contain userinfo: {redact_url(url)!r}")

    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f"URL has an invalid port: {redact_url(url)!r}") from None
    if port == 0 and not lenient:
        raise ValueError(f"URL must not name port 0: {redact_url(url)!r}")

    host = parsed.hostname.lower()
    if ":" in host:
        # An IPv6 literal keeps its brackets, or the port would read as part of it.
        host = f"[{host}]"

    if port and port == _DEFAULT_PORTS[scheme]:
        port = None

    netloc = f"{host}:{port}" if port else host
    path = parsed.path.rstrip("/")

    return f"{scheme}://{netloc}{path}"


def url_identity(url: object) -> str | None:
    """The strict :func:`normalize_url_identity` of *url*, or ``None`` when it names no identity."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        return normalize_url_identity(url)
    except ValueError:
        return None


def same_url_identity(left: object, right: object) -> bool:
    """Whether *left* and *right* are two spellings of one http(s) URL."""
    left_identity = url_identity(left)
    return left_identity is not None and left_identity == url_identity(right)


def parse_url_identity_list(raw: object, *, setting: str) -> list[str]:
    """Comma- or space-separated http(s) URLs, canonical, deduplicated, in order.

    A malformed entry raises :class:`URLSettingError` rather than being
    skipped: every list read through this widens what the platform trusts, and
    an operator must never be told everything is fine while the entry they
    meant is silently absent.
    """
    if raw is None or raw == "":
        return []
    items = [raw] if isinstance(raw, str) else list(raw)  # type: ignore[call-overload]
    tokens = [token for item in items for token in re.split(r"[,\s]+", str(item)) if token]
    parsed: list[str] = []
    for token in tokens:
        try:
            parsed.append(normalize_url_identity(token))
        except ValueError:
            raise URLSettingError(
                f"{setting} entry {redact_url(token)!r} is not an http(s) URL with a host, a "
                "valid port and no userinfo, query string or fragment, e.g. http://xtm-one:4000"
            ) from None
    return list(dict.fromkeys(parsed))

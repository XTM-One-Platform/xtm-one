# SPDX-License-Identifier: Apache-2.0

"""Server-side SVG sanitization for template assets.

An SVG is an XML document that may carry ``<script>``, event handlers,
``<foreignObject>`` (arbitrary HTML), and links to external resources. Template
assets are rendered inside the deck (``<img>`` from a same-origin blob URL) and
exported through a headless browser, and a built-in one is seeded into EVERY
deployment — so the bytes are cleaned once, on the way into storage, rather than
trusted at each of those render points.

The sanitizer is deliberately small: it strips what can execute or reach out,
and keeps everything else verbatim. Parsing goes through ``defusedxml`` — the
input is untrusted, and the stdlib parser would happily expand an entity bomb.

* dropped ELEMENTS: ``script``, ``foreignObject``, ``iframe``, ``embed``,
  ``object``, ``audio``, ``video``, ``animate``/``set`` (they can rewrite
  ``href``), ``use`` with an external ``href``, and any ``image`` whose ``href``
  is not a ``data:image/`` URL;
* dropped ELEMENTS, too: anything in a namespace other than SVG's own (or
  none) - an XHTML ``html:meta http-equiv="refresh"`` or ``html:img`` keeps
  its semantics inside an XML document whatever it sits under, and a root
  spelled ``svg`` in the XHTML namespace is refused outright;
* dropped ATTRIBUTES: every ``on*`` handler, any attribute whose value starts
  with ``javascript:`` / ``data:text/html``, and ``style`` attributes carrying
  ``url(`` / ``expression(`` / ``@import`` - read with their CSS escapes
  decoded, so ``\\75rl(`` is ``url(``;
* dropped: XML processing instructions and a plain DOCTYPE are not re-emitted;
  a DOCTYPE that DECLARES entities is refused outright (``defusedxml``) — an
  entity in a logo is never legitimate, and it is the XXE / billion-laughs vector.

Anything that is not well-formed XML is REFUSED (:class:`SvgSanitizeError`)
instead of half-cleaned — a template asset that cannot be parsed cannot be
rendered either.

:func:`sanitize_template_asset` is the entry point every path that turns bytes
into a template asset goes through (REST upload / replace, package import and
duplicate, the agent's ``attach_template_asset`` copy, the brand seed): it
decides from the content type AND the filename whether the bytes are an SVG, so
a mislabelled upload cannot skip the clean.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import defusedxml
import defusedxml.ElementTree as DefusedET

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

_FORBIDDEN_TAGS = frozenset(
    {
        "script",
        "foreignobject",
        "iframe",
        "embed",
        "object",
        "audio",
        "video",
        "animate",
        "set",
        "handler",
        "listener",
    }
)
_UNSAFE_VALUE_RE = re.compile(r"^\s*(javascript:|vbscript:|data:\s*text/html)", re.IGNORECASE)
_UNSAFE_STYLE_RE = re.compile(r"url\s*\(|expression\s*\(|@import|behavior\s*:", re.IGNORECASE)
#: A CSS escape: ``\75`` (up to six hex digits, one optional trailing space)
#: or ``\x`` for any other character. Decoded BEFORE the style check, so
#: ``\75rl(`` reads as ``url(`` - the escape is how a stylesheet spells a
#: keyword the sanitiser is looking for without spelling it.
_CSS_ESCAPE_RE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})\s?|(.))", re.DOTALL)
_SAFE_DATA_IMAGE_RE = re.compile(r"^\s*data:image/(png|jpeg|jpg|gif|webp|svg\+xml);", re.IGNORECASE)
_HREF_ATTRS = ("href", f"{{{XLINK_NS}}}href")
#: The element namespaces an SVG may contain: its own, or none (a bare
#: ``<svg>`` with no ``xmlns``, which browsers treat as inert markup). An
#: element in ANOTHER namespace - XHTML above all - keeps that namespace's
#: semantics in an XML document: ``html:meta http-equiv="refresh"`` is a
#: redirect and ``html:img src`` a beacon whatever ancestor they sit under,
#: and an XHTML-namespaced ROOT spelled ``svg`` is no SVG at all. Foreign
#: metadata (Inkscape / Sodipodi / RDF) renders nothing and is dropped too.
_ALLOWED_ELEMENT_NAMESPACES = frozenset({"", SVG_NS})


class SvgSanitizeError(ValueError):
    """The document is not well-formed SVG and cannot be sanitized."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _css_unescape(value: str) -> str:
    """*value* with its CSS escapes decoded (an undecodable one is dropped)."""

    def _decode(match: re.Match[str]) -> str:
        hexdigits, char = match.group(1), match.group(2)
        if hexdigits is not None:
            try:
                return chr(int(hexdigits, 16))
            except (ValueError, OverflowError):
                return ""
        return char or ""

    return _CSS_ESCAPE_RE.sub(_decode, value)


def _style_reaches_out(css: str) -> bool:
    return bool(_UNSAFE_STYLE_RE.search(_css_unescape(css)))


def _href_of(element: ET.Element) -> str | None:
    for attr in _HREF_ATTRS:
        value = element.get(attr)
        if value is not None:
            return value
    return None


def _is_local_reference(href: str) -> bool:
    return href.strip().startswith("#")


def _element_is_allowed(element: ET.Element) -> bool:
    if not isinstance(element.tag, str) or _namespace(element.tag) not in _ALLOWED_ELEMENT_NAMESPACES:
        return False
    tag = _local(element.tag)
    if tag in _FORBIDDEN_TAGS:
        return False
    href = _href_of(element)
    if tag == "use":
        # <use> may only point inside the document — an external href fetches
        # another SVG into this one.
        return href is None or _is_local_reference(href)
    if tag == "image":
        # Embedded raster data is fine; a remote URL is a tracking pixel at best.
        return href is None or bool(_SAFE_DATA_IMAGE_RE.match(href))
    return True


def _href_allowed(tag: str, value: str) -> bool:
    """Whether an ``href`` may stay on an element of local name *tag*.

    ``<use>`` and ``<image>`` were already vetted at element level; a link may
    point to the web or a mailbox; anything else (``textPath``, ``mpath``, a
    stray ``xlink:href`` on a path) may only reference the document itself.
    """
    if tag in ("use", "image"):
        return True
    if tag == "a":
        return _is_local_reference(value) or value.strip().lower().startswith(("http://", "https://", "mailto:"))
    return _is_local_reference(value)


def _clean_attributes(element: ET.Element) -> None:
    tag = _local(element.tag)
    for name in list(element.attrib):
        value = element.attrib[name]
        local_name = _local(name)
        if (
            local_name.startswith("on")
            or _UNSAFE_VALUE_RE.match(value)
            or (local_name == "style" and _style_reaches_out(value))
            or (local_name == "href" and not _href_allowed(tag, value))
        ):
            del element.attrib[name]


def _strip_style_elements(root: ET.Element) -> None:
    """Drop ``<style>`` blocks that reach out (``url(`` / ``@import``), CSS escapes decoded."""
    for parent in root.iter():
        for child in list(parent):
            if _local(child.tag) == "style" and _style_reaches_out(child.text or ""):
                parent.remove(child)


def sanitize_svg(data: bytes) -> bytes:
    """Return *data* with executable / external constructs removed.

    Raises :class:`SvgSanitizeError` when the input is not well-formed XML, when
    it declares entities or reaches for an external resource, or when its root
    element is not ``<svg>``.
    """
    try:
        root = DefusedET.fromstring(data)
    except defusedxml.DefusedXmlException as exc:
        raise SvgSanitizeError(f"SVG uses a forbidden XML construct: {exc}") from exc
    except ET.ParseError as exc:
        raise SvgSanitizeError(f"not well-formed SVG: {exc}") from exc
    if (
        root is None
        or not isinstance(root.tag, str)
        or _local(root.tag) != "svg"
        or _namespace(root.tag) not in _ALLOWED_ELEMENT_NAMESPACES
    ):
        raise SvgSanitizeError("root element is not <svg>")

    # Depth-first removal of forbidden subtrees; attributes cleaned on what stays.
    stack = [root]
    while stack:
        element = stack.pop()
        _clean_attributes(element)
        for child in list(element):
            if _element_is_allowed(child):
                stack.append(child)
            else:
                element.remove(child)
    _strip_style_elements(root)

    # Keep the SVG namespace as the default one so the output stays a plain
    # <svg xmlns=...> document rather than <ns0:svg>.
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


def is_svg_content_type(content_type: str | None) -> bool:
    return (content_type or "").split(";", 1)[0].strip().lower() == "image/svg+xml"


def looks_like_svg(content_type: str | None, filename: str | None) -> bool:
    """Whether bytes labelled *content_type* / *filename* are to be treated as SVG.

    Either signal is enough: a browser decides what an ``.svg`` is from the
    extension as readily as from the type, so a clean must not be skippable by
    mislabelling one of the two.
    """
    return is_svg_content_type(content_type) or (filename or "").lower().endswith(".svg")


def sanitize_template_asset(data: bytes, content_type: str | None, filename: str | None = None) -> bytes:
    """The bytes to store for a template asset: SVGs cleaned, anything else verbatim.

    Raises :class:`SvgSanitizeError` for an SVG that cannot be parsed — the
    caller turns that into its own refusal (HTTP 400, a tool error, a skipped
    seed entry). Never raises for a non-SVG.
    """
    if not looks_like_svg(content_type, filename):
        return data
    return sanitize_svg(data)

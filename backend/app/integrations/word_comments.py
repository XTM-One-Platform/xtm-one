# SPDX-License-Identifier: Apache-2.0

"""Native Word comments on a ``.docx``, anchored on passages of its text.

A reviewing agent quotes the passages it wants to comment and the comments
it wants on them; :func:`add_word_comments` writes them into the document as
Word comments - ``word/comments.xml`` plus ``w:commentRangeStart`` /
``w:commentRangeEnd`` / ``w:commentReference`` markers in the body - exactly
as Word writes a comment made by hand, so Word desktop and Word for the web
show each one on its passage, under the author's name.

The document's TEXT is never changed: a passage that starts or ends inside
a run has that run split at the boundary (both halves keep its formatting),
nothing is inserted but the markers, and the text of every paragraph is
compared before and after as a guard. A passage is searched in the document
body (tables, text boxes and tracked insertions included; headers, footers
and deleted text excluded), it must occur exactly once unless an occurrence
is named, and a passage that is not found is REFUSED, never placed anywhere
else. Every request is answered: placed with its context, or refused with the
reason and, when the quote drifted (case, typographic quotes, spacing), the
exact text to quote. Storage-agnostic: the Microsoft tools hand it the bytes
of a OneDrive / SharePoint file and upload what it returns.

The same module reads the comments a document holds (:func:`list_word_comments`:
author, date, text, the anchored passage, a reply's parent and the resolved
mark, read from Word's extension parts) and removes some of them
(:func:`remove_word_comments`): the ``w:comment``, its range markers and
reference run wherever they are, its entries in ``commentsExtended.xml``,
``commentsIds.xml`` and ``commentsExtensible.xml``, and its replies with it.
The runs a comment split are merged back when both halves are formatted
alike, the same text guard runs, and the comments part stays in the package,
empty when the last comment goes, so Word opens the file as before. A
comment with a marker in a part this module does not edit (footnotes,
endnotes) is refused, with its whole thread, rather than left dangling there.
"""

from __future__ import annotations

import copy
import io
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.integrations.passage_match import exact_matches, near_matches, passage_context

logger = logging.getLogger(__name__)

# Word's comment extension parts (Word 2013 and later), related to the main
# document part. ``commentsExtended`` keys a comment by the ``w14:paraId`` of
# its LAST paragraph and carries the reply chain (``paraIdParent``) and the
# resolved mark (``done``); ``commentsIds`` maps that paraId to a durable id,
# which ``commentsExtensible`` keys on.
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
_W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
_W16CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
_RT_COMMENTS_EXTENDED = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
_RT_COMMENTS_IDS = "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds"
_RT_COMMENTS_EXTENSIBLE = "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible"

# The parts a comment can be anchored in that :func:`_stories` does not read,
# as a refusal names them (by content type); any other is ``_OTHER_PART``.
_UNEDITED_PART_NAMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml": "the document's footnotes",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml": "the document's endnotes",
}
_OTHER_PART = "another part of the document"
# The local names of the three markers all start with it: a part whose bytes
# do not hold it has none, and is not parsed.
_MARKER_PREFIX = b"commentR"

#: Comments one call may place; a review past it is several calls.
MAX_COMMENTS = 50
#: A passage longer than this is a section, not an anchor.
MAX_ANCHOR_CHARS = 2_000
MAX_COMMENT_CHARS = 10_000
#: Ambiguous occurrences shown in a refusal.
_MAX_CONTEXTS = 5
_MAX_HINT_CHARS = 300


class WordDocumentError(ValueError):
    """The bytes are not a Word document this module can comment, or the
    document could not be commented without changing its text."""


@dataclass(frozen=True)
class CommentRequest:
    """One comment to place: the passage it is on and its body.

    ``occurrence`` is 1-based and counted over the document; ``None`` means
    the passage must occur once.
    """

    anchor: str
    body: str
    occurrence: int | None = None


@dataclass(frozen=True)
class CommentOutcome:
    """What became of one request: the context of a placed comment, or the
    reason it was refused."""

    request: CommentRequest
    placed: bool
    detail: str
    comment_id: int | None = None


@dataclass(frozen=True)
class CommentedDocument:
    """The document with its new comments (``None`` when none was placed)
    and the outcome of every request, in the order they were given."""

    data: bytes | None
    outcomes: tuple[CommentOutcome, ...]

    @property
    def placed(self) -> list[CommentOutcome]:
        return [outcome for outcome in self.outcomes if outcome.placed]

    @property
    def refused(self) -> list[CommentOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.placed]


@dataclass(frozen=True)
class ExistingComment:
    """One comment of the document as it stands."""

    comment_id: int
    author: str
    initials: str
    #: The ``w:date`` attribute as written (ISO 8601), empty when absent.
    date: str
    text: str
    #: The document text between the comment's range markers; empty for a
    #: point comment (no range, or an empty one).
    anchor_text: str
    #: ``...before >>anchor<< after...`` in the paragraph the comment starts in.
    passage_context: str
    #: The comment this one answers, when it is a reply.
    parent_id: int | None = None
    #: Marked resolved in Word.
    done: bool = False


@dataclass(frozen=True)
class RemovalOutcome:
    """What became of one id: removed (with the replies that went with it),
    or refused with the reason."""

    comment_id: int
    removed: bool
    detail: str
    author: str = ""
    anchor_text: str = ""
    #: The replies removed with it, in the order the document lists them.
    replies: tuple[int, ...] = ()


@dataclass(frozen=True)
class RemovedComments:
    """The document without the comments (``None`` when none was removed)
    and the outcome of every id, in the order they were given."""

    data: bytes | None
    outcomes: tuple[RemovalOutcome, ...]

    @property
    def removed(self) -> list[RemovalOutcome]:
        return [outcome for outcome in self.outcomes if outcome.removed]

    @property
    def refused(self) -> list[RemovalOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.removed]


def initials_of(name: str) -> str:
    """The initials Word shows next to a comment: the first letter of up to
    three words of *name*, upper-cased."""
    return "".join(word[0] for word in name.split()[:3] if word[0].isalnum()).upper()


def add_word_comments(
    data: bytes,
    requests: Sequence[CommentRequest],
    *,
    author: str,
    initials: str | None = None,
) -> CommentedDocument:
    """Place *requests* as Word comments in the ``.docx`` *data*.

    Raises :class:`WordDocumentError` when *data* is not a Word document.
    """
    from docx.oxml.ns import qn

    doc = _open(data)
    paragraphs = list(doc.element.body.iter(qn("w:p")))
    texts = [_paragraph_text(paragraph) for paragraph in paragraphs]

    outcomes: list[CommentOutcome | None] = [None] * len(requests)
    placements: list[tuple[int, CommentRequest, tuple[int, int, int]]] = []
    for index, request in enumerate(requests):
        hits = exact_matches(texts, request.anchor)
        refusal = _refusal(request, texts, hits)
        if refusal:
            outcomes[index] = CommentOutcome(request, False, refusal)
        else:
            placements.append((index, request, hits[(request.occurrence or 1) - 1]))

    if initials is None:
        initials = initials_of(author)
    for index, request, (paragraph_index, start, end) in placements:
        paragraph = paragraphs[paragraph_index]
        # The start boundary first: a split keeps the head in the run it cuts,
        # so the end split - inside ``first`` when both boundaries share a run
        # - only shortens ``first`` in place, and it stays the run at ``start``.
        first = _run_starting_at(paragraph, start)
        last = _run_ending_at(paragraph, end)
        comment = doc.comments.add_comment(
            text=request.body.replace("\r\n", "\n").replace("\r", "\n"),
            author=author,
            initials=initials,
        )
        first.insert_comment_range_start_above(comment.comment_id)
        last.insert_comment_range_end_and_reference_below(comment.comment_id)
        outcomes[index] = CommentOutcome(
            request,
            True,
            passage_context(texts[paragraph_index], start, end),
            comment_id=comment.comment_id,
        )

    if not placements:
        return CommentedDocument(None, tuple(o for o in outcomes if o is not None))

    # The guard: the markers and the split runs changed no text.
    after = [_paragraph_text(paragraph) for paragraph in paragraphs]
    if after != texts:
        logger.error("Word comments would have changed the document text; nothing is written")
        raise WordDocumentError(
            "the comments could not be placed without changing the document's text, "
            "so nothing was written."
        )
    buffer = io.BytesIO()
    doc.save(buffer)
    return CommentedDocument(buffer.getvalue(), tuple(o for o in outcomes if o is not None))


def list_word_comments(data: bytes) -> list[ExistingComment]:
    """The comments of the ``.docx`` *data*, in the order of its comments part.

    Raises :class:`WordDocumentError` when *data* is not a Word document.
    """
    from docx.oxml.ns import qn

    doc = _open(data)
    parts = _CommentParts(doc)
    if not parts.index.comments:
        return []
    _, _, anchors = _read_anchors(_stories(doc))
    listed: list[ExistingComment] = []
    for comment_id, comment in parts.index.comments.items():
        anchor = anchors.get(comment_id)
        listed.append(
            ExistingComment(
                comment_id=comment_id,
                author=comment.get(qn("w:author")) or "",
                initials=comment.get(qn("w:initials")) or "",
                date=comment.get(qn("w:date")) or "",
                text="\n".join(_paragraph_text(p) for p in comment.iter(qn("w:p"))),
                anchor_text=anchor.text if anchor else "",
                passage_context=anchor.context if anchor else "",
                parent_id=parts.index.parent.get(comment_id),
                done=comment_id in parts.index.done,
            )
        )
    return listed


def remove_word_comments(data: bytes, comment_ids: Iterable[int]) -> RemovedComments:
    """Remove the comments *comment_ids* - and their replies - from the
    ``.docx`` *data*.

    Each id is answered: removed, with the replies that went with it, or
    refused because no comment has it, or because it or one of its replies
    has a marker in a part this module does not edit (footnotes, endnotes:
    removing the comment would leave that marker dangling). The document's
    text is checked paragraph by paragraph before and after, as
    :func:`add_word_comments` does; a difference raises
    :class:`WordDocumentError` and nothing is returned. Raises it too when
    *data* is not a Word document.
    """
    from docx.oxml.ns import qn

    doc = _open(data)
    parts = _CommentParts(doc)
    stories = _stories(doc)
    paragraphs, texts, anchors = _read_anchors(stories)
    index = parts.index
    elsewhere = _markers_outside_stories(doc) if index.comments else {}

    children: dict[int, list[int]] = {}
    for child, parent in index.parent.items():
        children.setdefault(parent, []).append(child)

    outcomes: list[RemovalOutcome] = []
    doomed: dict[int, None] = {}
    for comment_id in dict.fromkeys(comment_ids):
        comment = index.comments.get(comment_id)
        if comment is None:
            outcomes.append(
                RemovalOutcome(comment_id, False, f"no comment with id {comment_id} in the document")
            )
            continue
        replies = _descendants(comment_id, children)
        blocked = _unedited_marker_refusal(comment_id, replies, elsewhere)
        if blocked:
            outcomes.append(RemovalOutcome(comment_id, False, blocked, author=comment.get(qn("w:author")) or ""))
            continue
        anchor = anchors.get(comment_id)
        outcomes.append(
            RemovalOutcome(
                comment_id,
                True,
                anchor.context if anchor else "",
                author=comment.get(qn("w:author")) or "",
                anchor_text=anchor.text if anchor else "",
                replies=tuple(replies),
            )
        )
        doomed.update(dict.fromkeys([comment_id, *replies]))

    if not doomed:
        return RemovedComments(None, tuple(outcomes))

    for comment_id in doomed:
        parts.remove(comment_id)
        for story in stories:
            _remove_markers(story, comment_id)
    parts.save()

    # The guard: the markers, the reference runs and the merges changed no text.
    after = [_paragraph_text(paragraph) for paragraph in paragraphs]
    if after != texts:
        logger.error("Removing Word comments would have changed the document text; nothing is written")
        raise WordDocumentError(
            "the comments could not be removed without changing the document's text, "
            "so nothing was written."
        )
    buffer = io.BytesIO()
    doc.save(buffer)
    return RemovedComments(buffer.getvalue(), tuple(outcomes))


def _descendants(comment_id: int, children: dict[int, list[int]]) -> list[int]:
    """The replies of *comment_id*, and theirs, in document order."""
    found: list[int] = []
    pending = list(children.get(comment_id, ()))
    while pending:
        child = pending.pop(0)
        if child == comment_id or child in found:
            continue
        found.append(child)
        pending.extend(children.get(child, ()))
    return found


def _open(data: bytes) -> Any:
    from docx import Document

    try:
        return Document(io.BytesIO(data))
    except Exception as exc:  # python-docx raises several unrelated types on a bad package
        raise WordDocumentError(
            "the file is not a Word document (.docx) that can be commented."
        ) from exc


def _refusal(request: CommentRequest, texts: list[str], hits: list[tuple[int, int, int]]) -> str:
    """Why *request* cannot be placed, empty when it can."""
    anchor = request.anchor
    if not hits:
        hints = near_matches(texts, anchor)
        if hints:
            options = " or ".join(f'"{_short(hint)}"' for hint in hints)
            return (
                "the anchor does not occur in the document as written. The document's "
                f"exact text is {options}: quote it verbatim."
            )
        spans = " It spans several lines: an anchor must stay within one paragraph." if "\n" in anchor else ""
        return f"the anchor does not occur in the document body.{spans}"
    count = len(hits)
    if request.occurrence is None and count > 1:
        contexts = "; ".join(
            f"#{number}: {passage_context(texts[index], start, end)}"
            for number, (index, start, end) in enumerate(hits[:_MAX_CONTEXTS], 1)
        )
        more = f" (and {count - _MAX_CONTEXTS} more)" if count > _MAX_CONTEXTS else ""
        return (
            f"the anchor occurs {count} times: pass occurrence (1 to {count}) to pick one, "
            f"or quote a longer passage that occurs once. Occurrences: {contexts}{more}"
        )
    if request.occurrence is not None and request.occurrence > count:
        return (
            f"the anchor occurs {count} time{'s' if count != 1 else ''}; "
            f"occurrence {request.occurrence} does not exist."
        )
    return ""


def _short(text: str) -> str:
    return text if len(text) <= _MAX_HINT_CHARS else text[: _MAX_HINT_CHARS - 3] + "..."


# ---------------------------------------------------------------------------
# Runs: the paragraph's text and the boundaries a comment range needs
# ---------------------------------------------------------------------------


def _tags() -> dict[str, Any]:
    from docx.oxml.ns import qn

    return {
        "run": qn("w:r"),
        "rpr": qn("w:rPr"),
        "t": qn("w:t"),
        # Elements whose runs are not part of the text a reader sees: nested
        # paragraphs (a text box's, read on their own), the paragraph mark's
        # properties, deleted and moved-away tracked changes.
        "skipped": frozenset({qn("w:p"), qn("w:pPr"), qn("w:del"), qn("w:moveFrom")}),
        # Run children that read as text (python-docx's reading, ``CT_R.text``).
        "text": frozenset(qn(tag) for tag in ("w:t", "w:tab", "w:br", "w:cr", "w:noBreakHyphen", "w:ptab")),
        "space": qn("xml:space"),
    }


def _paragraph_runs(paragraph: Any) -> list[Any]:
    """The paragraph's runs in reading order, through hyperlinks, fields,
    content controls and tracked insertions, never into a nested paragraph."""
    tags = _tags()
    runs: list[Any] = []

    def walk(element: Any) -> None:
        for child in element:
            tag = child.tag
            if not isinstance(tag, str):  # a comment or processing instruction
                continue
            if tag == tags["run"]:
                runs.append(child)
            elif tag not in tags["skipped"]:
                walk(child)

    walk(paragraph)
    return runs


def _paragraph_text(paragraph: Any) -> str:
    return "".join(run.text for run in _paragraph_runs(paragraph))


def _run_starting_at(paragraph: Any, offset: int) -> Any:
    """The run whose text starts at *offset* of the paragraph's text, the run
    covering it split there when needed."""
    position = 0
    for run in _paragraph_runs(paragraph):
        length = len(run.text)
        if not length:
            continue
        if position == offset:
            return run
        if position < offset < position + length:
            return _split_run(run, offset - position)[1]
        position += length
    raise WordDocumentError("internal: no run starts at the anchor")


def _run_ending_at(paragraph: Any, offset: int) -> Any:
    """The run whose text ends at *offset*, the run covering it split there
    when needed."""
    position = 0
    for run in _paragraph_runs(paragraph):
        length = len(run.text)
        if not length:
            continue
        end = position + length
        if end == offset:
            return run
        if position < offset < end:
            return _split_run(run, offset - position)[0]
        position = end
    raise WordDocumentError("internal: no run ends at the anchor")


def _split_run(run: Any, offset: int) -> tuple[Any, Any]:
    """Split *run* at character *offset* of its text into two runs of the
    same formatting; the right one is inserted after it. Only a ``w:t`` can
    hold the boundary (every other text child is one character), and a child
    that reads as no text stays with the left half."""
    from docx.oxml import OxmlElement

    tags = _tags()
    right = OxmlElement("w:r")
    properties = run.find(tags["rpr"])
    if properties is not None:
        right.append(copy.deepcopy(properties))
    position = 0
    past = False
    for child in list(run):
        if child.tag == tags["rpr"]:
            continue
        if past:
            right.append(child)
            continue
        length = len(str(child)) if child.tag in tags["text"] else 0
        if position + length <= offset:
            position += length
            continue
        if position >= offset:
            past = True
            right.append(child)
            continue
        # A w:t holding the boundary.
        text = str(child)
        cut = offset - position
        child.text = text[:cut]
        child.set(tags["space"], "preserve")
        tail = OxmlElement("w:t")
        tail.text = text[cut:]
        tail.set(tags["space"], "preserve")
        right.append(tail)
        past = True
        position = offset
    run.addnext(right)
    return run, right


# ---------------------------------------------------------------------------
# Existing comments: where they are anchored, and the parts that describe them
# ---------------------------------------------------------------------------


def _int_attr(element: Any, name: str) -> int | None:
    from docx.oxml.ns import qn

    try:
        return int(element.get(qn(name)))
    except (TypeError, ValueError):
        return None


def _stories(doc: Any) -> list[Any]:
    """The roots whose text a reader sees and a comment can be anchored in:
    the body, then the headers and footers python-docx parses. Footnotes and
    endnotes are opaque parts and stay as they are: a comment with a marker
    in one is refused (:func:`_markers_outside_stories`)."""
    from docx.parts.hdrftr import FooterPart, HeaderPart

    stories = [doc.element.body]
    for part in doc.part.package.iter_parts():
        if isinstance(part, (HeaderPart, FooterPart)):
            stories.append(part.element)
    return stories


def _markers_outside_stories(doc: Any) -> dict[int | None, str]:
    """The comments with a range marker or a reference in a part
    :func:`_stories` does not read - footnotes, endnotes, any other part but
    the document, its headers and footers and the comments part - mapped to
    what a refusal calls that part. Removing such a comment would leave its
    marker there, pointing at nothing, so it is refused instead. A part that
    holds markers but cannot be parsed puts every comment under the ``None``
    key: which ones it names is unknown."""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml.ns import qn
    from docx.parts.hdrftr import FooterPart, HeaderPart
    from lxml import etree

    tags = (qn("w:commentRangeStart"), qn("w:commentRangeEnd"), qn("w:commentReference"))
    read = {id(doc.part), id(_related_part(doc.part, RT.COMMENTS))}
    found: dict[int | None, str] = {}
    for part in doc.part.package.iter_parts():
        if id(part) in read or isinstance(part, (HeaderPart, FooterPart)):
            continue
        content_type = str(getattr(part, "content_type", "") or "")
        root = getattr(part, "element", None)
        if root is None:
            if not content_type.endswith("xml"):
                continue
            blob = part.blob or b""
            if _MARKER_PREFIX not in blob:
                continue
            parser = etree.XMLParser(resolve_entities=False, no_network=True)
            try:
                root = etree.fromstring(blob, parser)
            except etree.XMLSyntaxError:
                logger.warning("A Word part holding comment markers could not be parsed; no comment is removed")
                found[None] = _OTHER_PART
                continue
        name = _UNEDITED_PART_NAMES.get(content_type, _OTHER_PART)
        for element in root.iter(*tags):
            comment_id = _int_attr(element, "w:id")
            if comment_id is not None:
                found.setdefault(comment_id, name)
    return found


def _unedited_marker_refusal(comment_id: int, replies: list[int], elsewhere: dict[int | None, str]) -> str:
    """Why *comment_id* cannot be removed because it, or one of the *replies*
    that would go with it, has a marker in a part this module does not edit;
    empty when it can be."""
    advice = "which this tool does not edit; remove it in Word"
    if comment_id in elsewhere:
        return f"its markers are in {elsewhere[comment_id]}, {advice}"
    for reply in replies:
        if reply in elsewhere:
            return f"its reply #{reply}, removed with it, has its markers in {elsewhere[reply]}, {advice}"
    if None in elsewhere:
        return f"comment markers are in {elsewhere[None]} that could not be read, so no comment is removed; remove it in Word"
    return ""


@dataclass(frozen=True)
class _Anchor:
    text: str
    context: str


class _Span:
    """A comment range being read: where it opened and the text it covers."""

    def __init__(self, paragraph: int | None, start: int) -> None:
        self.paragraph = paragraph
        self.start = start
        self.last_paragraph = paragraph
        self.parts: list[str] = []

    def add(self, text: str, paragraph: int) -> None:
        if not text:
            return
        if self.parts and paragraph != self.last_paragraph:
            self.parts.append("\n")
        self.parts.append(text)
        self.last_paragraph = paragraph

    def close(self, paragraph: int | None, offset: int, texts: list[str]) -> _Anchor:
        anchor = "".join(self.parts)
        if self.paragraph is None:
            return _Anchor(anchor, anchor)
        end = offset if paragraph == self.paragraph else len(texts[self.paragraph])
        return _Anchor(anchor, passage_context(texts[self.paragraph], self.start, max(self.start, end)))


def _read_anchors(stories: list[Any]) -> tuple[list[Any], list[str], dict[int, _Anchor]]:
    """Every paragraph of *stories* with its text, and each comment's anchor:
    the text of the counted runs between its range markers (the runs
    :func:`_paragraph_runs` reads, so a deletion or a nested paragraph's own
    runs never leak in), wherever the markers sit - a paragraph, a hyperlink,
    a table cell, a text box, a tracked insertion, or between paragraphs."""
    from docx.oxml.ns import qn

    tags = _tags()
    start_tag, end_tag, ref_tag = qn("w:commentRangeStart"), qn("w:commentRangeEnd"), qn("w:commentReference")
    paragraphs = [paragraph for story in stories for paragraph in story.iter(qn("w:p"))]
    texts = [_paragraph_text(paragraph) for paragraph in paragraphs]
    paragraph_index = {paragraph: index for index, paragraph in enumerate(paragraphs)}
    counted: dict[Any, tuple[int, int]] = {}
    for index, paragraph in enumerate(paragraphs):
        position = 0
        for run in _paragraph_runs(paragraph):
            counted[run] = (index, position)
            position += len(run.text)

    def paragraph_of(element: Any) -> int | None:
        for ancestor in element.iterancestors(qn("w:p")):
            return paragraph_index.get(ancestor)
        return None

    seen: dict[int, int] = {}  # paragraph index -> characters passed so far
    open_spans: dict[int, _Span] = {}
    anchors: dict[int, _Anchor] = {}
    for story in stories:
        for element in story.iter():
            tag = element.tag
            if not isinstance(tag, str):
                continue
            if tag in (start_tag, end_tag, ref_tag):
                comment_id = _int_attr(element, "w:id")
                if comment_id is None:
                    continue
                paragraph = paragraph_of(element)
                offset = seen.get(paragraph, 0) if paragraph is not None else 0
                if tag == start_tag:
                    open_spans[comment_id] = _Span(paragraph, offset)
                elif tag == end_tag:
                    span = open_spans.pop(comment_id, None)
                    if span is not None and comment_id not in anchors:
                        anchors[comment_id] = span.close(paragraph, offset, texts)
                elif comment_id not in anchors and comment_id not in open_spans:
                    # A reference whose comment has no range: a point comment.
                    anchors[comment_id] = _Span(paragraph, offset).close(paragraph, offset, texts)
            elif tag == tags["run"]:
                found = counted.get(element)
                if found is None:
                    continue
                paragraph, position = found
                text = element.text
                seen[paragraph] = position + len(text)
                for span in open_spans.values():
                    span.add(text, paragraph)
    for comment_id, span in open_spans.items():  # a range never closed
        anchors.setdefault(comment_id, span.close(None, 0, texts))
    return paragraphs, texts, anchors


def _related_part(part: Any, reltype: str) -> Any | None:
    for rel in part.rels.values():
        if rel.reltype == reltype and not rel.is_external:
            return rel.target_part
    return None


class _ExtensionPart:
    """One of Word's comment extension parts, parsed for reading and
    editing. python-docx keeps it as an opaque part with the bytes it was
    loaded with, which :meth:`save` replaces once the part was edited."""

    def __init__(self, part: Any) -> None:
        from lxml import etree

        self.part = part
        element = getattr(part, "element", None)
        self.parsed = element is None
        if self.parsed:
            parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=True)
            element = etree.fromstring(part.blob, parser)
        self.root = element
        self.dirty = False

    @classmethod
    def of(cls, part: Any | None) -> _ExtensionPart | None:
        return cls(part) if part is not None else None

    def remove_where(self, tag: str, attribute: str, value: str) -> None:
        for entry in list(self.root.iter(tag)):
            if (entry.get(attribute) or "").upper() == value:
                _detach(entry, merge=False)
                self.dirty = True

    def save(self) -> None:
        from lxml import etree

        if self.dirty and self.parsed:
            data = etree.tostring(self.root, xml_declaration=True, encoding="UTF-8", standalone=True)
            # The one way to put edited bytes back into an opaque python-docx
            # part: ``Part`` has no blob setter; ``Part.__init__`` keeps the
            # bytes in the private ``_blob`` that ``Part.blob`` returns. Checked
            # on python-docx 1.2.0, the version pinned in pyproject.toml;
            # ``test_python_docx_opaque_part_serves_its_private_blob`` fails a
            # bump that changes it, and a release that moves the bytes must
            # not leave the old entries in place silently: the write is read
            # back, and a mismatch refuses.
            self.part._blob = data
            if self.part.blob != data:
                logger.error("python-docx no longer serves an opaque part's _blob; Word comments are not removed")
                raise WordDocumentError(
                    "the comments could not be removed from Word's comment extension parts, "
                    "so nothing was written."
                )
        self.dirty = False


@dataclass
class _CommentIndex:
    #: ``w:comment`` elements by id, in the order of the comments part.
    comments: dict[int, Any] = field(default_factory=dict)
    #: The ``w14:paraId`` of each comment's last paragraph, upper-cased.
    para_ids: dict[int, str] = field(default_factory=dict)
    #: The durable id ``commentsIds.xml`` gives each comment, upper-cased.
    durable_ids: dict[int, str] = field(default_factory=dict)
    #: A reply's parent.
    parent: dict[int, int] = field(default_factory=dict)
    #: The comments marked resolved.
    done: set[int] = field(default_factory=set)


class _CommentParts:
    """The comments part and the extension parts of a document, with the
    index that ties a comment to its entries in each."""

    def __init__(self, doc: Any) -> None:
        from docx.opc.constants import RELATIONSHIP_TYPE as RT

        document_part = doc.part
        comments_part = _related_part(document_part, RT.COMMENTS)
        self.comments = getattr(comments_part, "element", None)
        self.extended = _ExtensionPart.of(_related_part(document_part, _RT_COMMENTS_EXTENDED))
        self.ids = _ExtensionPart.of(_related_part(document_part, _RT_COMMENTS_IDS))
        self.extensible = _ExtensionPart.of(_related_part(document_part, _RT_COMMENTS_EXTENSIBLE))
        self.index = self._build_index()

    def _build_index(self) -> _CommentIndex:
        from docx.oxml.ns import qn

        index = _CommentIndex()
        if self.comments is None:
            return index
        for comment in self.comments.findall(qn("w:comment")):
            comment_id = _int_attr(comment, "w:id")
            if comment_id is None or comment_id in index.comments:
                continue
            index.comments[comment_id] = comment
            paragraphs = comment.findall(qn("w:p"))
            para_id = paragraphs[-1].get(f"{{{_W14}}}paraId") if paragraphs else None
            if para_id:
                index.para_ids[comment_id] = para_id.upper()
        by_para_id = {para_id: comment_id for comment_id, para_id in index.para_ids.items()}
        if self.extended is not None:
            for entry in self.extended.root.iter(f"{{{_W15}}}commentEx"):
                comment_id = by_para_id.get((entry.get(f"{{{_W15}}}paraId") or "").upper())
                if comment_id is None:
                    continue
                parent_id = by_para_id.get((entry.get(f"{{{_W15}}}paraIdParent") or "").upper())
                if parent_id is not None and parent_id != comment_id:
                    index.parent[comment_id] = parent_id
                if (entry.get(f"{{{_W15}}}done") or "").lower() in ("1", "true"):
                    index.done.add(comment_id)
        if self.ids is not None:
            for entry in self.ids.root.iter(f"{{{_W16CID}}}commentId"):
                comment_id = by_para_id.get((entry.get(f"{{{_W16CID}}}paraId") or "").upper())
                durable_id = entry.get(f"{{{_W16CID}}}durableId")
                if comment_id is not None and durable_id:
                    index.durable_ids[comment_id] = durable_id.upper()
        return index

    def remove(self, comment_id: int) -> None:
        """Drop *comment_id* from the comments part and from every extension
        part that has an entry for it. The parts themselves stay, empty or
        not, with their relationships and content types."""
        comment = self.index.comments.pop(comment_id, None)
        if comment is not None:
            _detach(comment, merge=False)
        para_id = self.index.para_ids.pop(comment_id, None)
        durable_id = self.index.durable_ids.pop(comment_id, None)
        if para_id is not None:
            if self.extended is not None:
                self.extended.remove_where(f"{{{_W15}}}commentEx", f"{{{_W15}}}paraId", para_id)
            if self.ids is not None:
                self.ids.remove_where(f"{{{_W16CID}}}commentId", f"{{{_W16CID}}}paraId", para_id)
        if durable_id is not None and self.extensible is not None:
            self.extensible.remove_where(
                f"{{{_W16CEX}}}commentExtensible", f"{{{_W16CEX}}}durableId", durable_id
            )

    def save(self) -> None:
        for part in (self.extended, self.ids, self.extensible):
            if part is not None:
                part.save()


def _remove_markers(root: Any, comment_id: int) -> None:
    """Remove the range markers and the reference of *comment_id* under
    *root*: the reference's run when it holds nothing else, the reference
    alone otherwise. The runs a marker split are merged back where it stood."""
    from docx.oxml.ns import qn

    tags = _tags()
    for tag in ("w:commentRangeStart", "w:commentRangeEnd"):
        for marker in list(root.iter(qn(tag))):
            if _int_attr(marker, "w:id") == comment_id:
                _detach(marker)
    for reference in list(root.iter(qn("w:commentReference"))):
        if _int_attr(reference, "w:id") != comment_id:
            continue
        run = reference.getparent()
        if (
            run is not None
            and run.tag == tags["run"]
            and all(
                child is reference or child.tag == tags["rpr"]
                for child in run
                if isinstance(child.tag, str)
            )
        ):
            _detach(run)
        else:
            _detach(reference)


def _detach(element: Any, *, merge: bool = True) -> None:
    """Remove *element* from its parent, keeping any tail text in place, and
    merge the runs it stood between when they are two halves of one."""
    parent = element.getparent()
    if parent is None:
        return
    previous, following = element.getprevious(), element.getnext()
    if element.tail:
        if previous is not None:
            previous.tail = (previous.tail or "") + element.tail
        else:
            parent.text = (parent.text or "") + element.tail
    parent.remove(element)
    if merge:
        _merge_split_runs(previous, following)


def _merge_split_runs(left: Any, right: Any) -> None:
    """Merge the adjacent runs *left* and *right* into *left* when they read
    as one run split in two: the same attributes, the same ``rPr``, and
    nothing but text children. Anything else is left alone - text is what
    matters, and the guard reads it either way."""
    from lxml import etree

    tags = _tags()
    if left is None or right is None or left.tag != tags["run"] or right.tag != tags["run"]:
        return
    if left.getnext() is not right or dict(left.attrib) != dict(right.attrib):
        return
    allowed = tags["text"] | {tags["rpr"]}
    for run in (left, right):
        if any(not isinstance(child.tag, str) or child.tag not in allowed for child in run):
            return

    def properties(run: Any) -> bytes:
        rpr = run.find(tags["rpr"])
        return b"" if rpr is None else etree.tostring(rpr, method="c14n")

    if properties(left) != properties(right):
        return
    for child in list(right):
        if child.tag != tags["rpr"]:
            left.append(child)
    _detach(right, merge=False)

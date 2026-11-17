"""Native Word comments on OneDrive / SharePoint documents.

Two layers. ``word_comments.add_word_comments`` writes the comments into the
``.docx`` as Word does - the comments part plus the range markers around the
anchored runs - without changing a character of the text, refusing an anchor
it cannot place exactly; ``list_word_comments`` reads them back (anchor, reply
chain and resolved mark from Word's extension parts) and
``remove_word_comments`` takes them out of every part, replies included,
under the same text guard. ``add_microsoft_document_comments`` fetches the
document from Graph, hands it to that layer and saves the result as a new
version of the same file with ``If-Match`` on the eTag it read, as the
connected account; a checked-out, locked or read-only file is refused with
nothing written. ``list_microsoft_document_comments`` and
``delete_microsoft_document_comments`` share that path, the delete refusing
other people's comments unless asked. The tools run against the fake Graph of
``test_microsoft_file_capabilities`` (``httpx.MockTransport``, no network).
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Any

import httpx
import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

from app.integrations import word_comments
from app.integrations.providers import microsoft as ms
from app.integrations.word_comments import (
    CommentRequest,
    WordDocumentError,
    add_word_comments,
    initials_of,
    list_word_comments,
    remove_word_comments,
)
from app.services.chunking import extract_text_from_file
from tests.test_microsoft_file_capabilities import FakeGraph, _call, _executor, _json
from tests.test_microsoft_file_capabilities import graph as fake_graph  # noqa: F401 - a fixture

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
AUTHOR = "Ada Lovelace"


@pytest.fixture
def graph(fake_graph: FakeGraph) -> FakeGraph:  # noqa: F811 - the imported fixture, by name
    """The fake Graph of the file-capability tests, under its usual name."""
    return fake_graph


def _hyperlink(text: str) -> Any:
    hyperlink = OxmlElement("w:hyperlink")
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = text
    run.append(t)
    hyperlink.append(run)
    return hyperlink


def _tracked(tag: str, text: str, text_tag: str = "w:t") -> Any:
    change = OxmlElement(tag)
    change.set(qn("w:id"), "901")
    change.set(qn("w:author"), "Bob")
    run = OxmlElement("w:r")
    t = OxmlElement(text_tag)
    t.text = text
    run.append(t)
    change.append(run)
    return change


def review_docx() -> bytes:
    """A document with the shapes a review meets: one-run paragraphs, a bold
    word splitting a sentence over three runs, a phrase repeated three times,
    a hyperlink, a table cell, a tracked insertion and a tracked deletion."""
    doc = Document()
    doc.add_paragraph("The quick brown fox jumps over the lazy dog.")
    split = doc.add_paragraph()
    split.add_run("This sentence ")
    split.add_run("needs a fix").bold = True
    split.add_run(" before publication.")
    doc.add_paragraph("Another line that needs a fix too, and needs a fix again.")
    linked = doc.add_paragraph()
    linked.add_run("See ")
    linked._p.append(_hyperlink("the roadmap page"))
    linked.add_run(" for details.")
    tracked = doc.add_paragraph()
    tracked.add_run("Keep ")
    tracked._p.append(_tracked("w:ins", "the inserted words"))
    tracked._p.append(_tracked("w:del", "the deleted words", "w:delText"))
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Budget 2027"
    table.cell(0, 1).text = "TBD figure"
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _anchored_text(doc: Any, comment_id: int) -> str:
    """The text between a comment's range markers, through hyperlinks."""
    wanted = str(comment_id)
    inside = False
    parts: list[str] = []
    for element in doc.element.body.iter():
        if element.tag == qn("w:commentRangeStart") and element.get(qn("w:id")) == wanted:
            inside = True
        elif element.tag == qn("w:commentRangeEnd") and element.get(qn("w:id")) == wanted:
            return "".join(parts)
        elif inside and element.tag == qn("w:r"):
            parts.append(element.text)
    raise AssertionError(f"comment {comment_id} has no closed range")


FIVE = [
    CommentRequest("quick brown fox", "A cliche; rephrase."),
    CommentRequest("sentence needs a fix before", "Spans three runs."),
    CommentRequest("needs a fix", "The second one.", occurrence=2),
    CommentRequest("roadmap page for", "Through the hyperlink."),
    CommentRequest("nowhere to be found", "Missing on purpose."),
]


# ── A document shaped as Word saves it ────────────────────────────────────

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
W16CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
EXTENSION_PARTS = {
    "commentsExtended": "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
    "commentsIds": "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
    "commentsExtensible": "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible",
}
REVIEWER = "Bob Reviewer"
#: Comment id -> the ``w14:paraId`` of its last paragraph and its durable id.
PARA_IDS = {0: "1A2B3C4D", 1: "2B3C4D5E", 2: "3C4D5E6F", 3: "4D5E6F7A"}
DURABLE_IDS = {0: "0A0B0C0D", 1: "1A1B1C1D", 2: "2A2B2C2D", 3: "3A3B3C3D"}


def _xml(text: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + text).encode()


def _comment_xml(comment_id: int, author: str, initials: str, text: str, para_id: str) -> Any:
    return etree.fromstring(
        f'<w:comment xmlns:w="{W}" xmlns:w14="{W14}" w:id="{comment_id}" w:author="{author}" '
        f'w:initials="{initials}" w:date="2026-10-09T09:0{comment_id}:00Z">'
        f'<w:p w14:paraId="{para_id}"><w:pPr><w:pStyle w:val="CommentText"/></w:pPr>'
        '<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:annotationRef/></w:r>'
        f"<w:r><w:t>{text}</w:t></w:r></w:p></w:comment>"
    )


def _marker(tag: str, comment_id: int) -> Any:
    return etree.fromstring(f'<w:{tag} xmlns:w="{W}" w:id="{comment_id}"/>')


def _reference_run(comment_id: int) -> Any:
    return etree.fromstring(
        f'<w:r xmlns:w="{W}"><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>'
        f'<w:commentReference w:id="{comment_id}"/></w:r>'
    )


def word_authored_docx() -> bytes:
    """The review document with comments as Word writes them: two by the
    reviewer (#0 on "quick brown fox", #1 on "TBD figure", resolved), Ada's
    reply #2 to #0 (its range markers next to the parent's, as Word writes a
    reply's), a point comment #3 by a third person (a reference, no range),
    ``w14:paraId`` on every comment's last paragraph and the three extension
    parts - commentsExtended (reply chain, done), commentsIds (durable ids)
    and commentsExtensible - related to the document part."""
    base = add_word_comments(
        review_docx(),
        [CommentRequest("quick brown fox", "Rephrase this."), CommentRequest("TBD figure", "Fill it in.")],
        author=REVIEWER,
    ).data
    assert base is not None
    with zipfile.ZipFile(io.BytesIO(base)) as package:
        files = {name: package.read(name) for name in package.namelist()}

    comments = etree.fromstring(files["word/comments.xml"])
    for comment in comments.findall(f"{{{W}}}comment"):
        comment.findall(f"{{{W}}}p")[-1].set(f"{{{W14}}}paraId", PARA_IDS[int(comment.get(f"{{{W}}}id"))])
    comments.append(_comment_xml(2, AUTHOR, "AL", "Agreed, rephrase it.", PARA_IDS[2]))
    comments.append(_comment_xml(3, "Carol Point", "CP", "A note on the dog.", PARA_IDS[3]))
    files["word/comments.xml"] = etree.tostring(comments, xml_declaration=True, encoding="UTF-8", standalone=True)

    document = etree.fromstring(files["word/document.xml"])
    document.find(f".//{{{W}}}commentRangeStart[@{{{W}}}id='0']").addnext(_marker("commentRangeStart", 2))
    parent_reference = document.find(f".//{{{W}}}commentReference[@{{{W}}}id='0']").getparent()
    parent_reference.addnext(_reference_run(2))
    parent_reference.addnext(_marker("commentRangeEnd", 2))
    document.find(f".//{{{W}}}body/{{{W}}}p").append(_reference_run(3))
    files["word/document.xml"] = etree.tostring(document, xml_declaration=True, encoding="UTF-8", standalone=True)

    files["word/commentsExtended.xml"] = _xml(
        f'<w15:commentsEx xmlns:w15="{W15}">'
        f'<w15:commentEx w15:paraId="{PARA_IDS[0]}" w15:done="0"/>'
        f'<w15:commentEx w15:paraId="{PARA_IDS[1]}" w15:done="1"/>'
        f'<w15:commentEx w15:paraId="{PARA_IDS[2]}" w15:paraIdParent="{PARA_IDS[0]}" w15:done="0"/>'
        f'<w15:commentEx w15:paraId="{PARA_IDS[3]}" w15:done="0"/>'
        "</w15:commentsEx>"
    )
    files["word/commentsIds.xml"] = _xml(
        f'<w16cid:commentsIds xmlns:w16cid="{W16CID}">'
        + "".join(
            f'<w16cid:commentId w16cid:paraId="{PARA_IDS[i]}" w16cid:durableId="{DURABLE_IDS[i]}"/>' for i in range(4)
        )
        + "</w16cid:commentsIds>"
    )
    files["word/commentsExtensible.xml"] = _xml(
        f'<w16cex:commentsExtensible xmlns:w16cex="{W16CEX}">'
        + "".join(
            f'<w16cex:commentExtensible w16cex:durableId="{DURABLE_IDS[i]}" w16cex:dateUtc="2026-10-09T09:0{i}:00Z"/>'
            for i in range(4)
        )
        + "</w16cex:commentsExtensible>"
    )

    rels = etree.fromstring(files["word/_rels/document.xml.rels"])
    for number, (part, reltype) in enumerate(EXTENSION_PARTS.items(), 901):
        rels.append(etree.Element(f"{{{RELS}}}Relationship", Id=f"rId{number}", Type=reltype, Target=f"{part}.xml"))
    files["word/_rels/document.xml.rels"] = etree.tostring(rels, xml_declaration=True, encoding="UTF-8", standalone=True)
    types = etree.fromstring(files["[Content_Types].xml"])
    for part in EXTENSION_PARTS:
        types.append(
            etree.Element(
                f"{{{CONTENT_TYPES}}}Override",
                PartName=f"/word/{part}.xml",
                ContentType=f"application/vnd.openxmlformats-officedocument.wordprocessingml.{part}+xml",
            )
        )
    files["[Content_Types].xml"] = etree.tostring(types, xml_declaration=True, encoding="UTF-8", standalone=True)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in files.items():
            package.writestr(name, content)
    return out.getvalue()


def _package(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        return {name: package.read(name) for name in package.namelist()}


def _ids_in(xml: bytes, tag: str, attribute: str) -> list[str]:
    return [element.get(attribute) for element in etree.fromstring(xml).iter(tag)]


def _marker_ids(xml: bytes) -> dict[str, list[str]]:
    """The comment ids of every range marker and reference in a story."""
    root = etree.fromstring(xml)
    return {
        tag: [element.get(f"{{{W}}}id") for element in root.iter(f"{{{W}}}{tag}")]
        for tag in ("commentRangeStart", "commentRangeEnd", "commentReference")
    }


# ── The OOXML layer ───────────────────────────────────────────────────────


def test_comments_land_on_their_passages_and_change_no_text():
    data = review_docx()

    result = add_word_comments(data, FIVE, author=AUTHOR)

    assert [outcome.placed for outcome in result.outcomes] == [True, True, True, True, False]
    assert result.outcomes[0].detail == "...The >>quick brown fox<< jumps over the lazy dog...."
    assert result.outcomes[4].detail == "the anchor does not occur in the document body."
    assert extract_text_from_file(result.data, "review.docx") == extract_text_from_file(data, "review.docx")
    doc = Document(io.BytesIO(result.data))
    assert [(c.author, c.initials, c.text) for c in doc.comments] == [
        (AUTHOR, "AL", "A cliche; rephrase."),
        (AUTHOR, "AL", "Spans three runs."),
        (AUTHOR, "AL", "The second one."),
        (AUTHOR, "AL", "Through the hyperlink."),
    ]
    assert [_anchored_text(doc, c.comment_id) for c in doc.comments] == [
        "quick brown fox",
        "sentence needs a fix before",
        "needs a fix",
        "roadmap page for",
    ]
    assert all(c.timestamp is not None for c in doc.comments)


def test_a_passage_split_across_runs_keeps_the_formatting_of_each_run():
    result = add_word_comments(review_docx(), [FIVE[1]], author=AUTHOR)

    doc = Document(io.BytesIO(result.data))
    runs = [(run.text, run.bold) for run in doc.paragraphs[1].runs if run.text]
    assert runs == [
        ("This ", None),
        ("sentence ", None),
        ("needs a fix", True),
        (" before", None),
        (" publication.", None),
    ]
    assert doc.paragraphs[1].text == "This sentence needs a fix before publication."


def test_a_table_cell_and_a_tracked_insertion_are_anchors_a_deletion_is_not():
    result = add_word_comments(
        review_docx(),
        [
            CommentRequest("TBD figure", "Fill it in."),
            CommentRequest("Keep the inserted words", "Across the insertion."),
            CommentRequest("the deleted words", "Gone."),
        ],
        author=AUTHOR,
    )

    assert [outcome.placed for outcome in result.outcomes] == [True, True, False]
    doc = Document(io.BytesIO(result.data))
    assert [_anchored_text(doc, c.comment_id) for c in doc.comments] == ["TBD figure", "Keep the inserted words"]


def test_an_ambiguous_anchor_is_refused_with_its_occurrences():
    result = add_word_comments(review_docx(), [CommentRequest("needs a fix", "Which one?")], author=AUTHOR)

    assert result.data is None
    [outcome] = result.outcomes
    assert outcome.detail.startswith("the anchor occurs 3 times: pass occurrence (1 to 3)")
    assert "#1: ...This sentence >>needs a fix<< before publication...." in outcome.detail
    assert "#3: ...Another line that needs a fix too, and >>needs a fix<< again...." in outcome.detail


def test_an_occurrence_past_the_count_is_refused():
    result = add_word_comments(review_docx(), [CommentRequest("needs a fix", "No.", occurrence=4)], author=AUTHOR)

    assert result.data is None
    assert result.outcomes[0].detail == "the anchor occurs 3 times; occurrence 4 does not exist."


def test_a_quote_that_drifted_is_refused_with_the_exact_text():
    drifted = CommentRequest("the Quick  brown fox \u2013 jumps", "Drifted quote.")
    doc_bytes = review_docx()
    doc = Document(io.BytesIO(doc_bytes))
    doc.add_paragraph("the quick brown fox - jumps high")
    out = io.BytesIO()
    doc.save(out)

    result = add_word_comments(out.getvalue(), [drifted], author=AUTHOR)

    assert result.data is None
    assert result.outcomes[0].detail == (
        'the anchor does not occur in the document as written. The document\'s exact text is '
        '"the quick brown fox - jumps": quote it verbatim.'
    )


def test_an_anchor_over_several_paragraphs_says_so():
    result = add_word_comments(
        review_docx(), [CommentRequest("lazy dog.\nThis sentence", "Two paragraphs.")], author=AUTHOR
    )

    assert result.data is None
    assert "must stay within one paragraph" in result.outcomes[0].detail


def test_overlapping_comments_share_a_paragraph():
    result = add_word_comments(
        review_docx(),
        [
            CommentRequest("quick brown fox jumps", "Outer."),
            CommentRequest("brown fox", "Inner."),
            CommentRequest("fox jumps over", "Crossing."),
        ],
        author=AUTHOR,
    )

    doc = Document(io.BytesIO(result.data))
    assert [_anchored_text(doc, c.comment_id) for c in doc.comments] == [
        "quick brown fox jumps",
        "brown fox",
        "fox jumps over",
    ]
    assert doc.paragraphs[0].text == "The quick brown fox jumps over the lazy dog."


def test_a_multi_line_comment_becomes_paragraphs():
    result = add_word_comments(
        review_docx(), [CommentRequest("TBD figure", "First point.\r\nSecond point.")], author=AUTHOR
    )

    [comment] = Document(io.BytesIO(result.data)).comments
    assert [p.text for p in comment.paragraphs] == ["First point.", "Second point."]


def test_a_file_that_is_not_a_word_document_is_refused():
    with pytest.raises(WordDocumentError, match="not a Word document"):
        add_word_comments(b"%PDF-1.7 not a docx", FIVE, author=AUTHOR)


def test_initials_follow_the_author():
    assert initials_of("Ada Lovelace") == "AL"
    assert initials_of("Jean-Luc de la Fontaine Durand") == "JDL"
    assert initials_of("") == ""


# ── Reading the comments a document holds ─────────────────────────────────


def test_listing_reads_the_comments_the_add_placed_with_their_anchors():
    result = add_word_comments(review_docx(), FIVE, author=AUTHOR)

    listed = list_word_comments(result.data)

    assert [(c.comment_id, c.author, c.initials, c.text) for c in listed] == [
        (0, AUTHOR, "AL", "A cliche; rephrase."),
        (1, AUTHOR, "AL", "Spans three runs."),
        (2, AUTHOR, "AL", "The second one."),
        (3, AUTHOR, "AL", "Through the hyperlink."),
    ]
    # An anchor over three runs reads as one passage; through a hyperlink too.
    assert [c.anchor_text for c in listed] == ["quick brown fox", "sentence needs a fix before", "needs a fix", "roadmap page for"]
    assert listed[1].passage_context == "...This >>sentence needs a fix before<< publication...."
    assert listed[2].passage_context == "...Another line that >>needs a fix<< too, and needs a fix again...."
    assert all(c.parent_id is None and not c.done and c.date.endswith("Z") for c in listed)


def test_listing_reads_a_reply_a_resolved_mark_and_a_point_comment_from_the_extension_parts():
    listed = {c.comment_id: c for c in list_word_comments(word_authored_docx())}

    assert sorted(listed) == [0, 1, 2, 3]
    assert (listed[0].author, listed[0].anchor_text, listed[0].parent_id, listed[0].done) == (REVIEWER, "quick brown fox", None, False)
    assert (listed[1].anchor_text, listed[1].done) == ("TBD figure", True)
    reply = listed[2]
    assert (reply.author, reply.text, reply.parent_id, reply.anchor_text) == (AUTHOR, "Agreed, rephrase it.", 0, "quick brown fox")
    assert reply.date == "2026-10-09T09:02:00Z"
    point = listed[3]
    assert (point.author, point.anchor_text, point.parent_id) == ("Carol Point", "", None)
    assert point.passage_context == "...The quick brown fox jumps over the lazy dog.>><<..."


def test_a_document_without_comments_lists_none():
    assert list_word_comments(review_docx()) == []
    with pytest.raises(WordDocumentError, match="not a Word document"):
        list_word_comments(b"%PDF-1.7 not a docx")


# ── Removing comments ─────────────────────────────────────────────────────


def test_removing_one_comment_takes_it_out_of_every_part_and_leaves_the_others():
    data = word_authored_docx()

    result = remove_word_comments(data, [1])

    [outcome] = result.outcomes
    assert (outcome.removed, outcome.author, outcome.anchor_text, outcome.replies) == (True, REVIEWER, "TBD figure", ())
    assert outcome.detail == "...>>TBD figure<<..."
    before, after = _package(data), _package(result.data)
    assert _marker_ids(before["word/document.xml"]) == {
        "commentRangeStart": ["0", "2", "1"],
        "commentRangeEnd": ["0", "2", "1"],
        "commentReference": ["0", "2", "3", "1"],
    }
    assert _marker_ids(after["word/document.xml"]) == {
        "commentRangeStart": ["0", "2"],
        "commentRangeEnd": ["0", "2"],
        "commentReference": ["0", "2", "3"],
    }
    assert _ids_in(after["word/comments.xml"], f"{{{W}}}comment", f"{{{W}}}id") == ["0", "2", "3"]
    assert _ids_in(after["word/commentsExtended.xml"], f"{{{W15}}}commentEx", f"{{{W15}}}paraId") == [PARA_IDS[0], PARA_IDS[2], PARA_IDS[3]]
    assert _ids_in(after["word/commentsIds.xml"], f"{{{W16CID}}}commentId", f"{{{W16CID}}}paraId") == [PARA_IDS[0], PARA_IDS[2], PARA_IDS[3]]
    assert _ids_in(after["word/commentsExtensible.xml"], f"{{{W16CEX}}}commentExtensible", f"{{{W16CEX}}}durableId") == [
        DURABLE_IDS[0],
        DURABLE_IDS[2],
        DURABLE_IDS[3],
    ]
    # The reply chain and the resolved marks of the others survive.
    assert b'w15:paraIdParent="1A2B3C4D"' in after["word/commentsExtended.xml"]
    assert extract_text_from_file(result.data, "m.docx") == extract_text_from_file(data, "m.docx")
    doc = Document(io.BytesIO(result.data))
    assert [c.comment_id for c in doc.comments] == [0, 2, 3]
    assert [p.text for p in doc.paragraphs] == [p.text for p in Document(io.BytesIO(data)).paragraphs]
    assert doc.tables[0].cell(0, 1).text == "TBD figure"
    listed = list_word_comments(result.data)
    assert [(c.comment_id, c.parent_id, c.done) for c in listed] == [(0, None, False), (2, 0, False), (3, None, False)]


def test_removing_a_parent_removes_its_replies_and_says_so():
    data = word_authored_docx()

    result = remove_word_comments(data, [0])

    [outcome] = result.outcomes
    assert outcome.removed and outcome.replies == (2,)
    assert [c.comment_id for c in list_word_comments(result.data)] == [1, 3]
    markers = _marker_ids(_package(result.data)["word/document.xml"])
    assert markers == {"commentRangeStart": ["1"], "commentRangeEnd": ["1"], "commentReference": ["3", "1"]}
    assert _ids_in(_package(result.data)["word/commentsExtended.xml"], f"{{{W15}}}commentEx", f"{{{W15}}}paraId") == [
        PARA_IDS[1],
        PARA_IDS[3],
    ]
    assert extract_text_from_file(result.data, "m.docx") == extract_text_from_file(data, "m.docx")


def test_an_unknown_id_is_refused_and_alone_writes_nothing():
    data = word_authored_docx()

    result = remove_word_comments(data, [99, 1, 99])

    assert [(o.comment_id, o.removed) for o in result.outcomes] == [(99, False), (1, True)]
    assert result.outcomes[0].detail == "no comment with id 99 in the document"
    assert result.data is not None

    nothing = remove_word_comments(data, [42])
    assert nothing.data is None
    assert [(o.comment_id, o.removed, o.detail) for o in nothing.outcomes] == [(42, False, "no comment with id 42 in the document")]
    assert remove_word_comments(review_docx(), [0]).data is None  # no comments part at all


def test_the_runs_a_comment_split_are_merged_back():
    data = review_docx()
    commented = add_word_comments(data, [FIVE[1]], author=AUTHOR).data
    split = Document(io.BytesIO(commented))
    assert len([run for run in split.paragraphs[1].runs if run.text]) == 5

    result = remove_word_comments(commented, [0])

    doc = Document(io.BytesIO(result.data))
    assert [(run.text, run.bold) for run in doc.paragraphs[1].runs if run.text] == [
        ("This sentence ", None),
        ("needs a fix", True),
        (" before publication.", None),
    ]
    assert doc.paragraphs[1].text == "This sentence needs a fix before publication."
    assert _marker_ids(_package(result.data)["word/document.xml"]) == {"commentRangeStart": [], "commentRangeEnd": [], "commentReference": []}


def test_a_reference_run_holding_other_content_keeps_the_rest():
    data = word_authored_docx()
    package = _package(data)
    document = etree.fromstring(package["word/document.xml"])
    reference = document.find(f".//{{{W}}}commentReference[@{{{W}}}id='3']")
    text = etree.SubElement(reference.getparent(), f"{{{W}}}t")
    text.text = " Tail."
    package["word/document.xml"] = etree.tostring(document, xml_declaration=True, encoding="UTF-8", standalone=True)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, content in package.items():
            zipped.writestr(name, content)

    result = remove_word_comments(out.getvalue(), [3])

    doc = Document(io.BytesIO(result.data))
    assert doc.paragraphs[0].text == "The quick brown fox jumps over the lazy dog. Tail."
    assert _marker_ids(_package(result.data)["word/document.xml"])["commentReference"] == ["0", "2", "1"]


def test_removing_the_last_comment_leaves_an_empty_valid_comments_part():
    one = add_word_comments(review_docx(), [FIVE[0]], author=AUTHOR).data

    result = remove_word_comments(one, [0])

    package = _package(result.data)
    assert "word/comments.xml" in package
    assert etree.fromstring(package["word/comments.xml"]).tag == f"{{{W}}}comments"
    assert len(etree.fromstring(package["word/comments.xml"])) == 0
    assert "relationships/comments" in package["word/_rels/document.xml.rels"].decode()
    assert 'PartName="/word/comments.xml"' in package["[Content_Types].xml"].decode()
    doc = Document(io.BytesIO(result.data))
    assert len(doc.comments) == 0 and list_word_comments(result.data) == []
    assert extract_text_from_file(result.data, "m.docx") == extract_text_from_file(one, "m.docx")

    everything = remove_word_comments(word_authored_docx(), [0, 1, 3])
    package = _package(everything.data)
    for part in EXTENSION_PARTS:
        assert len(etree.fromstring(package[f"word/{part}.xml"])) == 0, part
    assert list_word_comments(everything.data) == []
    assert Document(io.BytesIO(everything.data)).paragraphs[0].text == "The quick brown fox jumps over the lazy dog."


def test_a_removal_that_would_change_the_text_raises_and_writes_nothing(monkeypatch):
    data = word_authored_docx()
    real = word_comments._remove_markers

    def faulty(root: Any, comment_id: int) -> None:
        real(root, comment_id)
        first_text = next(root.iter(qn("w:t")), None)
        if first_text is not None:
            first_text.text = "changed " + (first_text.text or "")

    monkeypatch.setattr(word_comments, "_remove_markers", faulty)

    with pytest.raises(WordDocumentError, match="without changing the document's text"):
        remove_word_comments(data, [1])


# ── Comments in a part the module does not edit ──────────────────

FOOTNOTES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
FOOTNOTES_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
FOOTNOTE_PARA_ID = "5E6F7A8B"
IN_FOOTNOTES = "its markers are in the document's footnotes, which this tool does not edit; remove it in Word"


def footnoted_docx(footnotes: bytes | None = None) -> bytes:
    """The Word-authored document plus a footnote holding Ada's comment #4,
    a reply to Carol's point comment #3: its range markers and reference sit
    in ``word/footnotes.xml``, a part python-docx keeps opaque. *footnotes*
    replaces that part's bytes."""
    files = _package(word_authored_docx())
    comments = etree.fromstring(files["word/comments.xml"])
    comments.append(_comment_xml(4, AUTHOR, "AL", "Cite the audit.", FOOTNOTE_PARA_ID))
    files["word/comments.xml"] = etree.tostring(comments, xml_declaration=True, encoding="UTF-8", standalone=True)
    extended = etree.fromstring(files["word/commentsExtended.xml"])
    extended.append(
        etree.Element(
            f"{{{W15}}}commentEx",
            {f"{{{W15}}}paraId": FOOTNOTE_PARA_ID, f"{{{W15}}}paraIdParent": PARA_IDS[3], f"{{{W15}}}done": "0"},
        )
    )
    files["word/commentsExtended.xml"] = etree.tostring(extended, xml_declaration=True, encoding="UTF-8", standalone=True)
    files["word/footnotes.xml"] = footnotes if footnotes is not None else _xml(
        f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="1"><w:p>'
        '<w:commentRangeStart w:id="4"/><w:r><w:t>Source: the 2026 audit.</w:t></w:r><w:commentRangeEnd w:id="4"/>'
        '<w:r><w:commentReference w:id="4"/></w:r></w:p></w:footnote></w:footnotes>'
    )
    rels = etree.fromstring(files["word/_rels/document.xml.rels"])
    rels.append(etree.Element(f"{{{RELS}}}Relationship", Id="rId950", Type=FOOTNOTES_REL, Target="footnotes.xml"))
    files["word/_rels/document.xml.rels"] = etree.tostring(rels, xml_declaration=True, encoding="UTF-8", standalone=True)
    types = etree.fromstring(files["[Content_Types].xml"])
    types.append(etree.Element(f"{{{CONTENT_TYPES}}}Override", PartName="/word/footnotes.xml", ContentType=FOOTNOTES_TYPE))
    files["[Content_Types].xml"] = etree.tostring(types, xml_declaration=True, encoding="UTF-8", standalone=True)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in files.items():
            package.writestr(name, content)
    return out.getvalue()


def test_a_comment_anchored_in_a_footnote_is_refused_and_nothing_is_written():
    data = footnoted_docx()
    assert [(c.comment_id, c.author, c.parent_id) for c in list_word_comments(data)][-1] == (4, AUTHOR, 3)

    result = remove_word_comments(data, [4])

    assert result.data is None
    assert [(o.comment_id, o.removed, o.detail, o.author) for o in result.outcomes] == [(4, False, IN_FOOTNOTES, AUTHOR)]


def test_a_parent_whose_reply_sits_in_a_footnote_is_refused_and_the_rest_is_removed():
    data = footnoted_docx()

    result = remove_word_comments(data, [3, 1])

    assert [(o.comment_id, o.removed, o.detail if not o.removed else "") for o in result.outcomes] == [
        (
            3,
            False,
            "its reply #4, removed with it, has its markers in the document's footnotes, which this "
            "tool does not edit; remove it in Word",
        ),
        (1, True, ""),
    ]
    before, after = _package(data), _package(result.data)
    assert after["word/footnotes.xml"] == before["word/footnotes.xml"]
    assert _ids_in(after["word/comments.xml"], f"{{{W}}}comment", f"{{{W}}}id") == ["0", "2", "3", "4"]
    assert _marker_ids(after["word/document.xml"])["commentReference"] == ["0", "2", "3"]
    assert [(c.comment_id, c.parent_id) for c in list_word_comments(result.data)] == [(0, None), (2, 0), (3, None), (4, 3)]


def test_a_part_holding_comment_markers_that_cannot_be_parsed_refuses_every_removal():
    truncated = _xml(f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="1"><w:p><w:r><w:commentReference w:id="4"/>')
    data = footnoted_docx(footnotes=truncated)

    result = remove_word_comments(data, [1, 0])

    assert result.data is None
    assert [(o.comment_id, o.removed, o.detail) for o in result.outcomes] == [
        (
            comment_id,
            False,
            "comment markers are in another part of the document that could not be read, so no "
            "comment is removed; remove it in Word",
        )
        for comment_id in (1, 0)
    ]


def test_python_docx_opaque_part_serves_its_private_blob():
    """``_ExtensionPart.save`` writes edited bytes into the private ``_blob``
    of python-docx's opaque ``Part`` (no setter exists). This fails a
    python-docx bump that moves it, instead of the removal leaving stale
    extension entries behind."""
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part

    part = Part(PackURI("/word/commentsIds.xml"), "application/xml", b"<a/>")
    assert part._blob == b"<a/>"
    part._blob = b"<b/>"
    assert part.blob == b"<b/>"
    parts = word_comments._CommentParts(Document(io.BytesIO(word_authored_docx())))
    for extension in (parts.extended, parts.ids, parts.extensible):
        assert extension is not None and extension.parsed and type(extension.part) is Part


def test_an_extension_part_that_does_not_take_the_edited_bytes_refuses_the_removal():
    class _FrozenPart:
        def __init__(self, blob: bytes) -> None:
            self._kept = blob

        @property
        def blob(self) -> bytes:
            return self._kept

    data = _package(word_authored_docx())["word/commentsIds.xml"]
    extension = word_comments._ExtensionPart(_FrozenPart(data))
    extension.remove_where(f"{{{W16CID}}}commentId", f"{{{W16CID}}}paraId", PARA_IDS[1])

    with pytest.raises(WordDocumentError, match="comment extension parts, so nothing was written"):
        extension.save()


# ── The tools against Graph ───────────────────────────────────────────────

URL = "https://contoso-my.sharepoint.com/personal/ceo/Documents/Board%20memo.docx"
ITEM = "/v1.0/drives/D1/items/ITEM1"
ETAG = '"{11111111-2222-3333-4444-555555555555},7"'
WEB_URL = "https://contoso-my.sharepoint.com/personal/ceo/_layouts/15/Doc.aspx?sourcedoc=1"
SITE = "contoso.sharepoint.com,11111111-2222,33333333-4444"

COMMENTS = [
    {"anchor": "quick brown fox", "comment": "A cliche; rephrase."},
    {"anchor": "sentence needs a fix before", "comment": "Spans three runs."},
    {"anchor": "needs a fix", "comment": "The second one.", "occurrence": 2},
    {"anchor": "roadmap page for", "comment": "Through the hyperlink."},
    {"anchor": "nowhere to be found", "comment": "Missing on purpose."},
]


def _meta(**overrides: Any) -> dict[str, Any]:
    meta = {
        "id": "ITEM1",
        "name": "Board memo.docx",
        "size": 4096,
        "file": {"mimeType": DOCX_MIME},
        "eTag": ETAG,
        "webUrl": WEB_URL,
        "parentReference": {"driveId": "D1"},
    }
    meta.update(overrides)
    return meta


def _review_routes(graph: FakeGraph, data: bytes, *, meta: dict | None = None, save: Any = None) -> None:
    meta = meta or _meta()
    graph.on("GET", f"/v1.0/shares/{ms._encode_share_id(URL)}/driveItem", _json(meta))
    graph.on("GET", ITEM, _json(meta))
    graph.on("GET", "/v1.0/me", _json({"displayName": AUTHOR, "mail": "ada@contoso.com"}))
    graph.on("GET", f"{ITEM}/content", httpx.Response(302, headers={"Location": "https://contoso-my.sharepoint.com/dl/memo.docx"}))
    graph.on("GET", "/dl/memo.docx", httpx.Response(200, content=data, headers={"Content-Type": DOCX_MIME}))
    saved = dict(meta, eTag='"{11111111-2222-3333-4444-555555555555},8"', **{"@microsoft.graph.downloadUrl": "https://contoso-my.sharepoint.com/dl/memo.docx?tempauth=1"})
    graph.on("PUT", f"{ITEM}/content", save or _json(saved, status=200))


@pytest.mark.asyncio
async def test_the_comments_are_saved_as_a_new_version_with_if_match(graph):
    data = review_docx()
    _review_routes(graph, data)

    result = await _call(_executor(), "add_microsoft_document_comments", url=URL, comments=COMMENTS)

    assert not result.is_error, result.content
    assert result.content.startswith(
        f"Added 4 comments to 'Board memo.docx' as {AUTHOR}, saved as a new version of the document; "
        "its text is unchanged. 1 comment was refused and left out."
    )
    assert '- 3. "needs a fix": placed on ...Another line that >>needs a fix<< too, and needs a fix again....' in result.content
    assert '- 5. "nowhere to be found": refused, the anchor does not occur in the document body.' in result.content
    assert f"Web URL: {WEB_URL}" in result.content
    assert "Download URL: https://contoso-my.sharepoint.com/dl/memo.docx?tempauth=1" in result.content
    [save] = [r for r in graph.requests if r.method == "PUT"]
    assert save.headers["if-match"] == ETAG
    assert save.headers["content-type"] == DOCX_MIME
    assert save.headers["authorization"] == "Bearer tok"
    saved = Document(io.BytesIO(save.content))
    assert [(c.author, c.text) for c in saved.comments] == [
        (AUTHOR, "A cliche; rephrase."),
        (AUTHOR, "Spans three runs."),
        (AUTHOR, "The second one."),
        (AUTHOR, "Through the hyperlink."),
    ]
    assert extract_text_from_file(save.content, "m.docx") == extract_text_from_file(data, "m.docx")
    # The pre-authenticated download hop carries no bearer token.
    hop = next(r for r in graph.requests if r.url.path == "/dl/memo.docx")
    assert "authorization" not in {k.lower() for k in hop.headers}
    assert [(r.method, r.url.path) for r in graph.requests][-1] == ("PUT", f"{ITEM}/content")


@pytest.mark.asyncio
async def test_a_document_in_a_sharepoint_library_is_addressed_by_path(graph):
    base = f"/v1.0/sites/{SITE}/drive"
    graph.on("GET", f"{base}/root:/Reviews/Board memo.docx", _json(_meta()))
    graph.on("GET", "/v1.0/me", _json({"displayName": AUTHOR}))
    graph.on("GET", f"{base}/items/ITEM1/content", httpx.Response(200, content=review_docx()))
    graph.on("PUT", f"{base}/items/ITEM1/content", _json(_meta(), status=200))

    result = await _call(
        _executor(),
        "add_microsoft_document_comments",
        item_path="/Reviews/Board memo.docx",
        site_id=SITE,
        comments=json.dumps(COMMENTS[:1]),
    )

    assert not result.is_error, result.content
    assert "Added 1 comment to 'Board memo.docx'" in result.content
    located = graph.requests[0]
    assert located.url.params["$select"] == "id,name,size,file,eTag,webUrl,parentReference,publication"
    assert graph.paths("PUT") == [f"{base}/items/ITEM1/content"]


@pytest.mark.asyncio
async def test_a_checked_out_document_is_refused_before_anything_is_downloaded(graph):
    checked_out = _meta(publication={"level": "checkout", "checkedOutBy": {"user": {"displayName": "Bob Reviewer"}}})
    _review_routes(graph, review_docx(), meta=checked_out)

    result = await _call(_executor(), "add_microsoft_document_comments", url=URL, comments=COMMENTS)

    assert result.is_error
    assert result.content == (
        "Error: 'Board memo.docx' is checked out by Bob Reviewer, so it cannot be changed until "
        "it is checked in. Nothing was written."
    )
    assert graph.paths("PUT") == [] and not any(p.endswith("/content") for p in graph.paths())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (423, "is locked - open for editing in Word by someone, or checked out"),
        (412, "changed while the comments were being prepared"),
        (403, "has no write access to 'Board memo.docx' (read access only)"),
    ],
    ids=["locked", "changed-meanwhile", "read-only"],
)
async def test_a_save_graph_refuses_is_a_clean_failure(graph, status, expected):
    _review_routes(
        graph,
        review_docx(),
        save=_json({"error": {"code": "notAllowed", "message": "The resource is locked"}}, status=status),
    )

    result = await _call(_executor(), "add_microsoft_document_comments", url=URL, comments=COMMENTS)

    assert result.is_error
    assert expected in result.content and "nothing was written" in result.content.lower()
    assert len(graph.paths("PUT")) == 1


@pytest.mark.asyncio
async def test_when_no_comment_can_be_placed_nothing_is_written(graph):
    _review_routes(graph, review_docx())

    result = await _call(
        _executor(),
        "add_microsoft_document_comments",
        url=URL,
        comments=[{"anchor": "needs a fix", "comment": "Ambiguous."}, {"anchor": "absent", "comment": "Missing."}],
    )

    assert result.is_error
    assert result.content.startswith("Error: none of the comments could be placed, so nothing was written to 'Board memo.docx':")
    assert '- 1. "needs a fix": refused, the anchor occurs 3 times' in result.content
    assert graph.paths("PUT") == []


@pytest.mark.asyncio
async def test_a_file_that_is_not_a_docx_is_refused_without_a_download(graph):
    _review_routes(graph, b"%PDF", meta=_meta(name="Board memo.pdf", file={"mimeType": "application/pdf"}))

    result = await _call(_executor(), "add_microsoft_document_comments", url=URL, comments=COMMENTS)

    assert result.is_error and "'Board memo.pdf' is not a .docx Word document" in result.content
    assert not any(p.endswith("/content") for p in graph.paths())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"url": URL, "comments": []}, "comments must be a non-empty list"),
        ({"url": URL, "comments": "not json"}, "could not be parsed"),
        ({"url": URL, "comments": [{"comment": "no anchor"}]}, "comment 1 has no 'anchor'"),
        ({"url": URL, "comments": [{"anchor": "x", "comment": ""}]}, "comment 1 has no 'comment' text"),
        ({"url": URL, "comments": [{"anchor": "x", "comment": "y", "occurrence": 0}]}, "occurrence must be a whole number from 1"),
        ({"comments": COMMENTS}, "by url (its SharePoint / OneDrive link) or by item_path"),
        ({"url": URL, "item_path": "a.docx", "comments": COMMENTS}, "not both"),
        ({"url": "ftp://x/a.docx", "comments": COMMENTS}, "full https:// SharePoint/OneDrive URL"),
    ],
    ids=["empty", "not-json", "no-anchor", "no-comment", "occurrence-0", "no-location", "two-locations", "not-https"],
)
async def test_a_malformed_call_makes_no_request(graph, arguments, expected):
    result = await _call(_executor(), "add_microsoft_document_comments", **arguments)

    assert result.is_error and expected in result.content
    assert graph.requests == []


@pytest.mark.asyncio
async def test_a_read_only_binding_withholds_the_write_tools_and_keeps_the_listing():
    from app.services.integration_read_only import withhold_write_tools
    from app.tools.base import CallableToolExecutor

    executor = CallableToolExecutor()
    definitions = withhold_write_tools(ms.build_tools("tok", executor), executor, integration_label="Microsoft")

    names = {definition.name for definition in definitions}
    assert "add_microsoft_document_comments" not in names
    assert "delete_microsoft_document_comments" not in names
    assert "list_microsoft_document_comments" in names
    result = await _call(executor, "add_microsoft_document_comments", url=URL, comments=COMMENTS)
    assert result.is_error and result.content.startswith("Refused: add_microsoft_document_comments")
    result = await _call(executor, "delete_microsoft_document_comments", url=URL, comment_ids="1")
    assert result.is_error and result.content.startswith("Refused: delete_microsoft_document_comments")


# ── Listing and deleting against Graph ────────────────────────────────────


def _delete_lines(content: str) -> list[str]:
    return [line for line in content.splitlines() if line.startswith("- #")]


@pytest.mark.asyncio
async def test_listing_shows_every_comment_with_its_id_and_reads_nothing_else(graph):
    _review_routes(graph, word_authored_docx())

    result = await _call(_executor(), "list_microsoft_document_comments", url=URL)

    assert not result.is_error, result.content
    lines = result.content.splitlines()
    assert lines[0] == "4 comments in 'Board memo.docx':"
    # The reviewer's two were placed by add_word_comments, dated when the fixture was built.
    stamp = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"
    assert re.fullmatch(rf'- #0 by {REVIEWER}, {stamp}, on "quick brown fox": Rephrase this\.', lines[1]), lines[1]
    assert re.fullmatch(rf'- #1 by {REVIEWER}, {stamp}, resolved, on "TBD figure": Fill it in\.', lines[2]), lines[2]
    assert lines[3] == f'- #2 by {AUTHOR}, 2026-10-09T09:02:00Z, reply to #0, on "quick brown fox": Agreed, rephrase it.'
    assert lines[4] == (
        "- #3 by Carol Point, 2026-10-09T09:03:00Z, at ...The quick brown fox jumps over the lazy dog.>><<...: "
        "A note on the dog."
    )
    assert f"Web URL: {WEB_URL}" in result.content
    # A read: the account is not looked up and nothing is saved.
    assert "/v1.0/me" not in graph.paths() and graph.paths("PUT") == []


@pytest.mark.asyncio
async def test_listing_a_document_without_comments_says_so(graph):
    _review_routes(graph, review_docx())

    result = await _call(_executor(), "list_microsoft_document_comments", url=URL)

    assert not result.is_error
    assert result.content.startswith("'Board memo.docx' has no comments.")


@pytest.mark.asyncio
async def test_listing_a_document_in_a_sharepoint_library_by_path(graph):
    base = f"/v1.0/sites/{SITE}/drive"
    graph.on("GET", f"{base}/root:/Reviews/Board memo.docx", _json(_meta()))
    graph.on("GET", f"{base}/items/ITEM1/content", httpx.Response(200, content=word_authored_docx()))

    result = await _call(_executor(), "list_microsoft_document_comments", item_path="Reviews/Board memo.docx", site_id=SITE)

    assert not result.is_error, result.content
    assert result.content.startswith("4 comments in 'Board memo.docx':")


@pytest.mark.asyncio
async def test_deleting_own_comments_saves_a_new_version_with_if_match(graph):
    data = word_authored_docx()
    _review_routes(graph, data)  # /me is Ada, who wrote the reply #2

    result = await _call(_executor(), "delete_microsoft_document_comments", url=URL, comment_ids="2, 99")

    assert not result.is_error, result.content
    lines = result.content.splitlines()
    assert lines[0] == (
        "Removed 1 comment from 'Board memo.docx', saved as a new version of the document. "
        "1 id was refused and left as is."
    )
    assert _delete_lines(result.content) == [
        f'- #2 by {AUTHOR} on "quick brown fox": removed',
        "- #99: refused, no comment with id 99 in the document (list_microsoft_document_comments shows the ids)",
    ]
    assert lines[-1] == "The document's text is unchanged."
    assert f"Web URL: {WEB_URL}" in result.content
    [save] = [r for r in graph.requests if r.method == "PUT"]
    assert save.headers["if-match"] == ETAG
    assert save.headers["content-type"] == DOCX_MIME
    assert [c.comment_id for c in list_word_comments(save.content)] == [0, 1, 3]
    assert extract_text_from_file(save.content, "m.docx") == extract_text_from_file(data, "m.docx")
    assert [(r.method, r.url.path) for r in graph.requests][-1] == ("PUT", f"{ITEM}/content")


@pytest.mark.asyncio
async def test_another_persons_comment_is_refused_unless_include_others(graph):
    data = word_authored_docx()
    _review_routes(graph, data)

    refused = await _call(_executor(), "delete_microsoft_document_comments", url=URL, comment_ids=[0])

    assert refused.is_error
    assert refused.content.splitlines() == [
        "Error: none of the comments could be removed from 'Board memo.docx', so nothing was written:",
        f'- #0 by {REVIEWER} on "quick brown fox": refused, not by the connected account ({AUTHOR}); '
        "pass include_others=true to remove other people's comments",
    ]
    assert graph.paths("PUT") == []

    removed = await _call(_executor(), "delete_microsoft_document_comments", url=URL, comment_ids="0", include_others=True)

    assert not removed.is_error, removed.content
    assert removed.content.splitlines()[0] == (
        "Removed 2 comments (1 asked for, 1 reply with them) from 'Board memo.docx', saved as a new version of the document."
    )
    assert _delete_lines(removed.content) == [f'- #0 by {REVIEWER} on "quick brown fox": removed, with its reply #2 by {AUTHOR}']
    [save] = [r for r in graph.requests if r.method == "PUT"]
    assert [c.comment_id for c in list_word_comments(save.content)] == [1, 3]


@pytest.mark.asyncio
async def test_all_mine_removes_the_connected_accounts_comments_only(graph):
    _review_routes(graph, word_authored_docx())

    result = await _call(_executor(), "delete_microsoft_document_comments", url=URL, comment_ids="all_mine")

    assert not result.is_error, result.content
    assert _delete_lines(result.content) == [f'- #2 by {AUTHOR} on "quick brown fox": removed']
    [save] = [r for r in graph.requests if r.method == "PUT"]
    assert [(c.comment_id, c.author) for c in list_word_comments(save.content)] == [
        (0, REVIEWER),
        (1, REVIEWER),
        (3, "Carol Point"),
    ]


@pytest.mark.asyncio
async def test_all_mine_with_no_comment_of_ones_own_writes_nothing(graph):
    graph.on("GET", "/v1.0/me", _json({"displayName": "Nobody Here"}))  # the first route for a path wins
    _review_routes(graph, word_authored_docx())

    result = await _call(_executor(), "delete_microsoft_document_comments", url=URL, comment_ids="all_mine")

    assert result.is_error
    assert result.content == (
        "Error: 'Board memo.docx' has no comment by the connected account (Nobody Here), so nothing was "
        f"written. Its 4 comments are by {REVIEWER} (2), {AUTHOR} (1), Carol Point (1); pass their ids with "
        "include_others=true to remove other people's comments."
    )
    assert graph.paths("PUT") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (423, "is locked - open for editing in Word by someone, or checked out"),
        (412, "changed while the comments were being removed"),
        (403, "has no write access to 'Board memo.docx' (read access only)"),
    ],
    ids=["locked", "changed-meanwhile", "read-only"],
)
async def test_a_delete_graph_refuses_is_a_clean_failure(graph, status, expected):
    _review_routes(
        graph,
        word_authored_docx(),
        save=_json({"error": {"code": "notAllowed", "message": "The resource is locked"}}, status=status),
    )

    result = await _call(_executor(), "delete_microsoft_document_comments", url=URL, comment_ids="2")

    assert result.is_error
    assert expected in result.content and "nothing was written" in result.content.lower()
    assert len(graph.paths("PUT")) == 1


@pytest.mark.asyncio
async def test_the_delete_tool_refuses_a_comment_in_a_footnote_and_removes_the_others(graph):
    data = footnoted_docx()
    _review_routes(graph, data)  # /me is Ada, who wrote #2 in the body and #4 in the footnote

    refused = await _call(_executor(), "delete_microsoft_document_comments", url=URL, comment_ids="4")

    assert refused.is_error
    assert refused.content.splitlines() == [
        "Error: none of the comments could be removed from 'Board memo.docx', so nothing was written:",
        f"- #4 by {AUTHOR} (no anchored text): refused, {IN_FOOTNOTES}",
    ]
    assert graph.paths("PUT") == []

    mixed = await _call(_executor(), "delete_microsoft_document_comments", url=URL, comment_ids="all_mine")

    assert not mixed.is_error, mixed.content
    assert mixed.content.splitlines()[0] == (
        "Removed 1 comment from 'Board memo.docx', saved as a new version of the document. "
        "1 id was refused and left as is."
    )
    assert _delete_lines(mixed.content) == [
        f'- #2 by {AUTHOR} on "quick brown fox": removed',
        f"- #4 by {AUTHOR} (no anchored text): refused, {IN_FOOTNOTES}",
    ]
    [save] = [r for r in graph.requests if r.method == "PUT"]
    assert [c.comment_id for c in list_word_comments(save.content)] == [0, 1, 3, 4]
    assert _package(save.content)["word/footnotes.xml"] == _package(data)["word/footnotes.xml"]


@pytest.mark.asyncio
async def test_a_checked_out_document_refuses_the_delete_before_anything_is_downloaded(graph):
    checked_out = _meta(publication={"level": "checkout", "checkedOutBy": {"user": {"displayName": "Bob Reviewer"}}})
    _review_routes(graph, word_authored_docx(), meta=checked_out)

    result = await _call(_executor(), "delete_microsoft_document_comments", url=URL, comment_ids="2")

    assert result.is_error
    assert result.content == (
        "Error: 'Board memo.docx' is checked out by Bob Reviewer, so it cannot be changed until "
        "it is checked in. Nothing was written."
    )
    assert graph.paths("PUT") == [] and not any(p.endswith("/content") for p in graph.paths())


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_microsoft_document_comments", "delete_microsoft_document_comments"])
@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (_meta(name="Board memo.doc", file={"mimeType": "application/msword"}), "'Board memo.doc' is not a .docx Word document"),
        (_meta(size=26 * 1024 * 1024), "'Board memo.docx' is 26.0 MB; comments can only be {verb} documents of up to 25 MB."),
    ],
    ids=["old-word-format", "oversize"],
)
async def test_a_doc_file_and_an_oversize_file_are_refused_before_any_download(graph, tool, meta, expected):
    _review_routes(graph, word_authored_docx(), meta=meta)
    arguments = {"comment_ids": "2"} if tool.startswith("delete") else {}

    verb = "removed from" if tool.startswith("delete") else "read from"

    result = await _call(_executor(), tool, url=URL, **arguments)

    assert result.is_error and expected.format(verb=verb) in result.content
    assert ("Nothing was written" in result.content) == tool.startswith("delete")
    assert not any(p.endswith("/content") for p in graph.paths())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"url": URL, "comment_ids": ""}, "comment_ids must be the ids of the comments to remove"),
        ({"url": URL, "comment_ids": []}, "comment_ids must be the ids"),
        ({"url": URL, "comment_ids": "1, two"}, "'two' is not a comment id"),
        ({"url": URL, "comment_ids": "[1,"}, "could not be parsed"),
        ({"comment_ids": "1"}, "by url (its SharePoint / OneDrive link) or by item_path"),
        ({"url": "http://x/a.docx", "comment_ids": "1"}, "full https:// SharePoint/OneDrive URL"),
    ],
    ids=["blank", "empty-list", "not-an-id", "bad-json", "no-location", "not-https"],
)
async def test_a_malformed_delete_makes_no_request(graph, arguments, expected):
    result = await _call(_executor(), "delete_microsoft_document_comments", **arguments)

    assert result.is_error and expected in result.content
    assert graph.requests == []


def test_comment_ids_are_read_however_the_model_spells_them():
    from app.integrations.providers.microsoft.files import _parse_comment_ids as parse

    assert parse("3, 7,12") == ([3, 7, 12], False, None)
    assert parse("#3 #7") == ([3, 7], False, None)
    assert parse("[3, 7, 3]") == ([3, 7], False, None)
    assert parse([3, "7", 7.0]) == ([3, 7], False, None)
    assert parse(5) == ([5], False, None)
    assert parse("all_mine") == ([], True, None)
    assert parse(["ALL_MINE"]) == ([], True, None)
    assert parse('"all_mine"') == ([], True, None)
    assert parse([True])[2] is not None and parse(["x"])[2] is not None


def test_the_tool_definitions_are_classified_as_the_binding_rule_reads_them():
    from app.services.tool_classification import infer_tool_type

    definitions = {definition.name: definition for definition in ms.build_tools("tok", _executor())}
    assert infer_tool_type("list_microsoft_document_comments") == "read"
    assert infer_tool_type("delete_microsoft_document_comments") == "write"
    assert definitions["list_microsoft_document_comments"].priority == 1
    assert definitions["delete_microsoft_document_comments"].priority == 2
    assert definitions["delete_microsoft_document_comments"].input_schema["required"] == ["comment_ids"]
    assert "include_others" in definitions["delete_microsoft_document_comments"].description
    assert "which is free text" in definitions["delete_microsoft_document_comments"].description
    assert "list_microsoft_document_comments" in definitions["add_microsoft_document_comments"].description

"""Identifying and swapping a deck's pictures.

Asked to replace an outdated picture, an agent could not tell which file a
slide showed (a bare UUID in the spec), could not open it (``read_file_content``
only resolves files of the current conversation) and could not swap one inside
a list without re-sending the whole list (``replace_text`` skips file ids,
``update_block`` merges top-level fields only). These tests pin the contract
that replaces that:

1. ``picture_placements`` names every place a slide shows a picture in
   ``edit_presentation``'s own address grammar - the address it prints is the
   one ``resolve_block_slot`` resolves.
2. ``replace_image`` swaps a picture by file id wherever it is shown, nested
   lists included, with ``replace_text``'s safety, and leaves every slide it did
   not write byte-identical.
3. ``get_presentation`` / ``capture_presentation_slides`` list the pictures of
   the slides they serve; ``edit_presentation`` accounts for every swap.
4. The key rule of the file contract is spelled once for every consumer.

No DB: the tools' sessions are faked, as in ``test_presentation_patch.py``.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.presentation_patch import (
    PresentationPatchError,
    PresentationPatchNoChangeError,
    apply_operations,
    canonical,
    resolve_block_slot,
    summarize_patch,
    verify_untouched,
)
from app.services.presentation_pictures import (
    PictureFacts,
    canonical_file_reference,
    describe_location,
    is_file_reference_key,
    normalize_file_reference,
    picture_placements,
    slide_file_references,
)
from app.services.presentation_spec import validate_spec

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _ids() -> dict[str, str]:
    return {name: str(uuid.uuid4()) for name in ("cover", "photo", "shot", "acme", "logo", "art")}


def _picture_deck(ids: dict[str, str]) -> dict:
    """Every place a deck can show a picture, each shape once."""
    return validate_spec(
        {
            "title": "Pictures",
            "slides": [
                {
                    "layout": "title",
                    "title": "Opening",
                    "speakers": [{"name": "Samuel Hassine", "photo_file_id": ids["photo"]}],
                    "background_image_file_id": ids["cover"],
                },
                {
                    "layout": "content",
                    "title": "Dashboard",
                    "blocks": [
                        {"type": "text", "content": "Intro."},
                        {"type": "image", "file_id": ids["shot"], "alt": "OpenCTI **dashboard**"},
                    ],
                },
                {
                    "layout": "content",
                    "title": "Customers",
                    "blocks": [
                        {
                            "type": "logo_grid",
                            "items": [
                                {"file_id": ids["acme"], "label": "Acme"},
                                {"file_id": ids["logo"], "label": "OpenCTI"},
                            ],
                        }
                    ],
                },
                {
                    "layout": "content",
                    "title": "Team",
                    "blocks": [
                        {
                            "type": "profiles",
                            "items": [
                                {
                                    "name": "Samuel Hassine",
                                    "photo_file_id": ids["photo"],
                                    "lines": [{"text": "CEO at Filigran", "logo_file_id": ids["logo"]}],
                                }
                            ],
                        }
                    ],
                },
                {
                    "layout": "content",
                    "title": "Platform",
                    "blocks": [
                        {
                            "type": "architecture",
                            "nodes": [
                                {"id": "hub", "label": "OpenCTI", "row": 1, "col": 1, "logo_file_id": ids["logo"]},
                                {"id": "edge", "label": "Edge", "row": 1, "col": 2},
                            ],
                            "edges": [{"source": "hub", "target": "edge"}],
                        }
                    ],
                },
                {
                    "layout": "content",
                    "title": "Side by side",
                    "columns": [
                        [{"type": "text", "content": "Left."}],
                        [
                            {
                                "type": "callout",
                                "title": "Note",
                                "children": [{"type": "image", "file_id": ids["shot"], "alt": "Nested shot"}],
                            }
                        ],
                    ],
                    "footer_block": {"type": "image", "file_id": ids["art"], "alt": "Footer art"},
                },
                {
                    "layout": "content",
                    "title": "Nested blocks",
                    "blocks": [
                        {
                            "type": "columns",
                            "columns": [
                                [{"type": "text", "content": "A"}],
                                [{"type": "image", "file_id": ids["art"], "caption": "In a columns block"}],
                            ],
                        },
                        {
                            "type": "comparison",
                            "left": {"label": "Before", "blocks": [{"type": "text", "content": "Old."}]},
                            "right": {"label": "After", "blocks": [{"type": "image", "file_id": ids["shot"]}]},
                        },
                    ],
                },
            ],
        }
    )


# ── Where a picture is: the edit grammar ─────────────────────────────────────


def test_every_place_a_deck_shows_a_picture_is_named_in_the_edit_address_grammar():
    ids = _ids()
    spec = _picture_deck(ids)
    labels = [placement.label() for placement in picture_placements(spec)]
    assert labels == [
        "slide 1 background image",
        'slide 1 speaker 1 photo "Samuel Hassine"',
        'slide 2 block 2 (image) "OpenCTI dashboard"',
        'slide 3 block 1 (logo_grid) item 1 "Acme"',
        'slide 3 block 1 (logo_grid) item 2 "OpenCTI"',
        'slide 4 block 1 (profiles) item 1 photo_file_id "Samuel Hassine"',
        'slide 4 block 1 (profiles) item 1 line 1 logo_file_id "CEO at Filigran"',
        'slide 5 block 1 (architecture) node 1 logo_file_id "OpenCTI"',
        'slide 6 block 2.1.1 (image) "Nested shot"',
        'slide 6 block footer (image) "Footer art"',
        'slide 7 block 1.2.1 (image) "In a columns block"',
        "slide 7 block 2.right.1 (image)",
    ], labels
    # Markup is read as text, never quoted raw.
    assert '"OpenCTI dashboard"' in labels[2]


def test_the_address_a_place_names_is_the_block_an_operation_resolves():
    """The point of speaking the edit grammar: 'block 2.1.1' in the list is
    the 'block' an update_block / replace_image takes, and it lands on the
    block that holds the picture."""
    ids = _ids()
    spec = _picture_deck(ids)
    checked = 0
    for placement in picture_placements(spec):
        if not placement.where.startswith("block "):
            continue
        address = placement.where.split(" ")[1]
        slide = spec["slides"][placement.slide_index]
        block = resolve_block_slot(slide, address).get()
        found: list[tuple[str, str]] = []
        from app.services.presentation_pictures import walk_file_references

        walk_file_references(block, "", found)
        assert placement.file_id in {value for _path, value in found}, placement.label()
        checked += 1
    assert checked == 10


def test_placements_narrow_to_the_slides_served_and_key_by_the_canonical_id():
    ids = _ids()
    spec = _picture_deck(ids)
    spec["slides"][1]["blocks"][1]["file_id"] = ids["shot"].upper()
    placements = picture_placements(spec, [1, 2])
    assert {p.slide_index for p in placements} == {1, 2}
    assert placements[0].file_id == ids["shot"].upper()
    assert placements[0].key == ids["shot"], "an uppercase id is the same file"


def test_an_unknown_shape_is_named_by_its_raw_path_never_dropped():
    assert describe_location({"layout": "x"}, ["tiles", "0", "logo_file_id"]) == ("tiles.0.logo_file_id", "")
    assert describe_location({"layout": "x", "blocks": []}, ["blocks", "4", "file_id"]) == ("blocks.4.file_id", "")


def test_file_references_are_read_whatever_spelling_the_model_copied():
    file_id = str(uuid.uuid4())
    for spelling in (
        file_id,
        file_id.upper(),
        f"[[FILE:{file_id}]]",
        f"file:{file_id}",
        f"file-{file_id}",
        f"  {file_id} ",
    ):
        assert normalize_file_reference(spelling) == file_id, spelling
    assert normalize_file_reference("not-a-uuid") == "not-a-uuid"


def test_the_key_rule_is_spelled_once_for_every_consumer():
    """The writer check and the reader grant (slide_file_references), the
    export's render allow-list, replace_text's skip rule and replace_image all
    see the same pictures: a field one of them misses would be served without
    being vouched for, or rewritten as text."""
    from app.services.presentation_export import referenced_file_ids

    assert is_file_reference_key("file_id") and is_file_reference_key("background_image_file_id")
    assert not is_file_reference_key("profile") and not is_file_reference_key("file_ids")
    ids = _ids()
    spec = _picture_deck(ids)
    walked = {canonical_file_reference(value) for _path, _layout, value in slide_file_references(spec)}
    assert walked == set(ids.values())
    assert set(referenced_file_ids(spec)) == walked
    assert {p.key for p in picture_placements(spec)} == walked
    # replace_text finds none of them: a file id is never text.
    for file_id in ids.values():
        with pytest.raises(PresentationPatchNoChangeError, match="was not found"):
            apply_operations(spec, [{"op": "replace_text", "find": file_id, "replace": "x"}])


# ── replace_image ────────────────────────────────────────────────────────────


def test_replace_image_swaps_a_picture_everywhere_on_purpose_and_nothing_else():
    ids = _ids()
    spec = _picture_deck(ids)
    new = str(uuid.uuid4())
    # Shown in 3 places: an unscoped swap without count is refused, and says where.
    with pytest.raises(PresentationPatchError, match="shown in 3 places") as exc:
        apply_operations(spec, [{"op": "replace_image", "find": ids["logo"], "replace": new}])
    message = str(exc.value)
    assert "slide 3 block 1 (logo_grid) item 2" in message and "count=3" in message
    # A wrong count is refused with the real one.
    with pytest.raises(PresentationPatchError, match="expected 2 place"):
        apply_operations(spec, [{"op": "replace_image", "find": ids["logo"], "replace": new, "count": 2}])

    out = apply_operations(spec, [{"op": "replace_image", "find": ids["logo"], "replace": new, "count": "3"}])
    assert [ref.index for ref in out.changed] == [3, 4, 5]
    slides = out.spec["slides"]
    assert slides[2]["blocks"][0]["items"][1]["file_id"] == new
    assert slides[2]["blocks"][0]["items"][0]["file_id"] == ids["acme"], "the other logo of the grid is untouched"
    assert slides[3]["blocks"][0]["items"][0]["lines"][0]["logo_file_id"] == new
    assert slides[4]["blocks"][0]["nodes"][0]["logo_file_id"] == new
    # Every slide the swap did not write is byte-identical, verified like any edit.
    assert verify_untouched(spec, out.spec, out.touched_ids) == []
    for index in (0, 1, 5, 6):
        assert canonical(slides[index]) == canonical(spec["slides"][index])
    # The account names the file ids and every place; the history counts them.
    (swap,) = out.image_swaps
    assert (swap.find, swap.replace, swap.slides) == (ids["logo"], new, [3, 4, 5])
    assert swap.places[0] == 'slide 3 block 1 (logo_grid) item 2 "OpenCTI"'
    assert out.pictures_swapped == 3
    assert summarize_patch(out, out.spec) == "Edited slides 3, 4, 5; 3 pictures swapped"
    # The patched deck is still a valid deck.
    assert canonical(validate_spec(out.spec)) == canonical(out.spec)


def test_replace_image_scoped_to_a_slide_or_a_block_needs_no_count():
    ids = _ids()
    spec = _picture_deck(ids)
    new = str(uuid.uuid4())
    # The photo is shown twice in the deck; scoped to slide 1, only its speaker photo moves.
    out = apply_operations(spec, [{"op": "replace_image", "slide": "1", "find": ids["photo"], "replace": new}])
    assert out.spec["slides"][0]["speakers"][0]["photo_file_id"] == new
    assert out.spec["slides"][3]["blocks"][0]["items"][0]["photo_file_id"] == ids["photo"]
    # A block scope reaches inside nested blocks and nowhere else on the slide.
    out = apply_operations(
        spec, [{"op": "replace_image", "slide": "6", "block": "2.1", "find": ids["shot"], "replace": new}]
    )
    assert out.spec["slides"][5]["columns"][1][0]["children"][0]["file_id"] == new
    out = apply_operations(
        spec, [{"op": "replace_image", "slide": "6", "block": "footer", "find": ids["art"], "replace": new}]
    )
    assert out.spec["slides"][5]["footer_block"]["file_id"] == new
    # The same file elsewhere on a slide, outside the block, is not swapped.
    with pytest.raises(PresentationPatchNoChangeError, match="not shown in block 1 of slide 7"):
        apply_operations(
            spec, [{"op": "replace_image", "slide": "7", "block": "1", "find": ids["shot"], "replace": new}]
        )
    with pytest.raises(PresentationPatchError, match="'block' needs 'slide'"):
        apply_operations(spec, [{"op": "replace_image", "block": "1", "find": ids["shot"], "replace": new}])


def test_replace_image_names_what_is_shown_when_the_file_is_not():
    """A wrong id is a 'No change' (never a failure the brake counts),
    and the answer names the pictures that ARE there, so the next call uses
    the right id instead of guessing again."""
    ids = _ids()
    spec = _picture_deck(ids)
    with pytest.raises(PresentationPatchNoChangeError) as exc:
        apply_operations(
            spec, [{"op": "replace_image", "slide": "3", "find": str(uuid.uuid4()), "replace": ids["shot"]}]
        )
    message = str(exc.value)
    assert "is not shown on slide 3" in message
    assert f'`{ids["acme"]}` (slide 3 block 1 (logo_grid) item 1 "Acme")' in message
    with pytest.raises(PresentationPatchNoChangeError, match="no picture is shown there"):
        apply_operations(
            spec, [{"op": "replace_image", "slide": "6", "block": "1.1", "find": ids["shot"], "replace": ids["acme"]}]
        )


def test_replace_image_reads_copied_spellings_and_refuses_what_cannot_be_a_file():
    ids = _ids()
    spec = _picture_deck(ids)
    new = str(uuid.uuid4())
    out = apply_operations(
        spec, [{"op": "replace_image", "slide": 2, "find": f"[[FILE:{ids['shot'].upper()}]]", "replace": f"file:{new}"}]
    )
    assert out.spec["slides"][1]["blocks"][1]["file_id"] == new
    with pytest.raises(PresentationPatchError, match="must be the file id"):
        apply_operations(spec, [{"op": "replace_image", "slide": 2, "find": ids["shot"], "replace": "new-logo.png"}])
    with pytest.raises(PresentationPatchError, match="the same file"):
        apply_operations(
            spec, [{"op": "replace_image", "slide": 2, "find": ids["shot"], "replace": ids["shot"].upper()}]
        )
    with pytest.raises(PresentationPatchError, match="'find' must be a file id"):
        apply_operations(spec, [{"op": "replace_image", "slide": 2, "find": "", "replace": new}])


def test_replace_image_repairs_a_dead_reference_whatever_it_looks_like():
    """A picture whose file is gone (a removed template asset) is still an id in
    the spec: the swap is structural, it never needs the old file to exist."""
    ids = _ids()
    spec = _picture_deck(ids)
    new = str(uuid.uuid4())
    out = apply_operations(spec, [{"op": "replace_image", "slide": "2", "find": ids["shot"], "replace": new}])
    assert out.spec["slides"][1]["blocks"][1]["file_id"] == new


def test_replace_image_account_follows_the_slide_a_later_operation_moved():
    ids = _ids()
    spec = _picture_deck(ids)
    new = str(uuid.uuid4())
    out = apply_operations(
        spec,
        [
            {"op": "replace_image", "slide": "2", "find": ids["shot"], "replace": new},
            {"op": "move_slide", "slide": "2", "to": 7},
        ],
    )
    (swap,) = out.image_swaps
    assert swap.slides == [7]
    assert swap.places == ['slide 7 block 2 (image) "OpenCTI dashboard"']


@pytest.mark.parametrize(
    "undo",
    [
        {
            "op": "replace_slide",
            "slide": "2",
            "content": {"layout": "content", "title": "Dashboard", "blocks": [{"type": "text", "content": "Gone."}]},
        },
        {"op": "replace_block", "slide": "2", "block": "2", "content": {"type": "text", "content": "Gone."}},
        {"op": "delete_slide", "slide": "2"},
    ],
    ids=["replace_slide", "replace_block", "delete_slide"],
)
def test_a_swap_a_later_operation_undid_is_not_accounted(undo):
    """The account is the SAVED deck's, not the operation log's. A swap a
    later operation of the same call removed is not reported, not counted in
    the history line - and its replacement, which no saved slide names and so
    nobody vouched for, is not a file the account may describe."""
    ids = _ids()
    spec = _picture_deck(ids)
    out = apply_operations(
        spec, [{"op": "replace_image", "slide": "2", "find": ids["shot"], "replace": str(uuid.uuid4())}, undo]
    )
    assert out.image_swaps == [] and out.pictures_swapped == 0
    assert "picture" not in summarize_patch(out, out.spec)


def test_a_chained_swap_is_accounted_as_the_swap_that_stands():
    ids = _ids()
    spec = _picture_deck(ids)
    x, y = str(uuid.uuid4()), str(uuid.uuid4())
    out = apply_operations(
        spec,
        [
            {"op": "replace_image", "slide": "2", "find": ids["shot"], "replace": x},
            {"op": "replace_image", "slide": "2", "find": x, "replace": y},
        ],
    )
    (swap,) = out.image_swaps
    assert (swap.find, swap.replace, swap.slides) == (x, y, [2])
    assert swap.places == ['slide 2 block 2 (image) "OpenCTI dashboard"']


def test_a_swap_is_addressed_where_the_picture_ends_up():
    """A later insert on the same slide moves the picture down: the account
    names the block it is in now, the address a follow-up operation takes."""
    ids = _ids()
    spec = _picture_deck(ids)
    out = apply_operations(
        spec,
        [
            {"op": "replace_image", "slide": "2", "find": ids["shot"], "replace": str(uuid.uuid4())},
            {"op": "insert_block", "slide": "2", "at": "1", "content": {"type": "text", "content": "Above."}},
        ],
    )
    (swap,) = out.image_swaps
    assert swap.places == ['slide 2 block 3 (image) "OpenCTI dashboard"']


def test_a_swap_on_a_slide_the_validator_reshapes_is_addressed_as_saved():
    """An operation may write a RETIRED v1 slide; the validator stores it
    as its v2 upgrade, where the picture sits somewhere else. The account is
    re-addressed against the validated spec - the address the saved deck has."""
    from app.services.presentation_patch import reconcile

    ids = _ids()
    spec = _picture_deck(ids)
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    out = apply_operations(
        spec,
        [
            {
                "op": "insert_slide",
                "at": "2",
                "content": {"layout": "generic", "title": "Legacy", "bullets": ["x"], "image_file_id": old},
            },
            {"op": "replace_image", "slide": "2", "find": old, "replace": new},
        ],
    )
    patched = validate_spec(out.spec)
    (swap,) = reconcile(out, spec, patched).image_swaps
    (place,) = swap.places
    assert place.startswith("slide 2 block ") and place.endswith("(image)"), place
    address = place.split(" ")[3]
    held: list[tuple[str, str]] = []
    from app.services.presentation_pictures import walk_file_references

    walk_file_references(resolve_block_slot(patched["slides"][1], address).get(), "", held)
    assert [value for _path, value in held] == [new]


def test_a_caption_fix_after_a_swap_keeps_both_accounted():
    """``replace_text`` rewrites strings where they are, so a caption fixed in
    the same call leaves the swapped picture's slot - and its account - intact."""
    ids = _ids()
    spec = _picture_deck(ids)
    new = str(uuid.uuid4())
    out = apply_operations(
        spec,
        [
            {"op": "replace_image", "slide": "2", "find": ids["shot"], "replace": new},
            {"op": "replace_text", "slide": "2", "find": "dashboard", "replace": "console"},
        ],
    )
    image = out.spec["slides"][1]["blocks"][1]
    assert (image["file_id"], image["alt"]) == (new, "OpenCTI **console**")
    assert out.replacements == 1
    (swap,) = out.image_swaps
    assert swap.places == ['slide 2 block 2 (image) "OpenCTI console"']
    assert summarize_patch(out, out.spec) == "Edited slide 2; 1 text replacement; 1 picture swapped"


@pytest.mark.parametrize("away", ["replace_image", "update_block"])
def test_a_slot_back_to_what_it_showed_is_no_swap(away):
    """A -> X then X -> A leaves the picture the deck showed: nothing was swapped
    on the page. Saved with a change elsewhere, the account must not claim a
    swap on a slide it lists as unchanged, nor count one in the history line -
    whichever operation moved the slot away first."""
    ids = _ids()
    spec = _picture_deck(ids)
    x = str(uuid.uuid4())
    if away == "replace_image":
        first = {"op": "replace_image", "slide": "2", "find": ids["shot"], "replace": x}
    else:
        first = {"op": "update_block", "slide": "2", "block": "2", "fields": {"file_id": x}}
    out = apply_operations(
        spec,
        [
            first,
            {"op": "replace_image", "slide": "2", "find": x, "replace": ids["shot"]},
            {"op": "update_slide", "slide": "3", "fields": {"title": "Our customers"}},
        ],
    )
    assert [ref.index for ref in out.named_unchanged] == [2]
    assert out.image_swaps == [] and out.pictures_swapped == 0
    assert summarize_patch(out, out.spec) == "Edited slide 3"


@pytest.mark.parametrize(
    "steps",
    [
        [("shot", "x"), ("x", "b"), ("b", "x")],
        [("shot", "x"), ("x", "shot"), ("shot", "x")],
        [("shot", "x"), "set b", ("b", "x")],
    ],
    ids=["x-b-x", "x-back-x", "set-between"],
)
def test_a_slot_several_swaps_wrote_is_accounted_once_by_the_swap_that_stands(steps):
    """One picture changed in one place: the slot is accounted to the LAST swap
    that wrote it, never once per swap whose file it held on the way."""
    ids = _ids()
    spec = _picture_deck(ids)
    names = {**ids, "x": str(uuid.uuid4()), "b": str(uuid.uuid4())}
    ops: list[dict] = []
    for step in steps:
        if step == "set b":
            ops.append({"op": "update_block", "slide": "2", "block": "2", "fields": {"file_id": names["b"]}})
        else:
            ops.append({"op": "replace_image", "slide": "2", "find": names[step[0]], "replace": names[step[1]]})
    out = apply_operations(spec, ops)
    (swap,) = out.image_swaps
    assert (swap.find, swap.replace) == (names[steps[-1][0]], names[steps[-1][1]])
    assert swap.places == ['slide 2 block 2 (image) "OpenCTI dashboard"']
    assert out.pictures_swapped == 1
    assert summarize_patch(out, out.spec) == "Edited slide 2; 1 picture swapped"


# ── The tools ────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, one=None, rows=None):
        self._one = one
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.statements: list = []
        self.added: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()

    async def commit(self):
        self.committed = True

    async def flush(self):
        return None

    def add(self, obj):
        self.added.append(obj)


def _existing_row(user_id: uuid.UUID, spec: dict, **columns):
    from app.models.presentation import Presentation

    return Presentation(
        id=uuid.uuid4(),
        user_id=user_id,
        title=spec["title"],
        theme=spec.get("theme") or "dark",
        slide_count=len(spec["slides"]),
        revision=1,
        spec=spec,
        **columns,
    )


def _deck_lookup(existing) -> list[_Result]:
    return [_Result(rows=[]), _Result(one=existing)]


def _tools(monkeypatch, user_id: uuid.UUID, session: _FakeSession):
    import app.core.database as database
    from app.services.builtin_tools import build_presentation_tools
    from app.services.builtin_tools import presentation as tool_module

    async def _no_layout_check(*_a, **_k):
        return ""

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(tool_module, "layout_check_note", _no_layout_check)
    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    return executor


def _run(executor, name: str, arguments: dict):
    from app.tools.base import ToolCall

    return asyncio.run(executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments)))


def _facts(file_id: str, filename: str, **overrides) -> PictureFacts:
    values = {
        "file_id": file_id,
        "filename": filename,
        "content_type": "image/png",
        "size": 2048,
        "origin": "template asset",
        "role": "product_logo",
        "description": None,
        "template_id": None,
        "template_name": None,
        "added": None,
    }
    values.update(overrides)
    return PictureFacts(**values)


def test_get_presentation_lists_the_pictures_of_the_slides_it_serves(monkeypatch):
    from app.services import presentation_pictures

    ids = _ids()
    user_id = uuid.uuid4()
    existing = _existing_row(user_id, _picture_deck(ids))
    asked: list[set[str]] = []

    async def _load(db, file_ids, *, user_id, group_ids=None):
        asked.append(set(file_ids))
        return {
            ids["acme"]: _facts(
                ids["acme"],
                "acme.svg",
                role="customer_logo",
                description="Acme wordmark, white on dark",
                template_id="tpl-1",
                template_name="XTM One Brand Deck",
                added=datetime(2026, 3, 2, tzinfo=UTC).date(),
            ),
            ids["logo"]: _facts(ids["logo"], "opencti-2024.png", origin="uploaded", role=None, description="Old logo"),
        }

    monkeypatch.setattr(presentation_pictures, "load_picture_facts", _load)
    executor = _tools(monkeypatch, user_id, _FakeSession(_deck_lookup(existing)))
    content = _run(executor, "get_presentation", {"presentation_id": str(existing.id), "slides": "3"}).content

    # One lookup for the deck's pictures; the list covers slide 3 only.
    assert asked == [set(ids.values())]
    assert "Pictures on the slides shown (2 file(s), 2 place(s)):" in content
    assert (
        f'- `{ids["acme"]}`: acme.svg, template asset [customer_logo] of "XTM One Brand Deck" '
        '(template_id `tpl-1`): "Acme wordmark, white on dark" - 2.0 KB, added 2026-03-02'
    ) in content
    assert 'shown at slide 3 block 1 (logo_grid) item 1 "Acme"' in content
    assert f'- `{ids["logo"]}`: opencti-2024.png, uploaded: "Old logo" - 2.0 KB' in content
    assert ids["shot"] not in content.split("Pictures on", 1)[1], "slide 2's screenshot is not listed"
    # Where the files cannot be opened, how to look, and how to swap.
    assert "read_file_content opens only files attached to this conversation" in content
    assert "capture_presentation_slides shows these slides as rendered" in content
    assert '"op": "replace_image"' in content


def test_get_presentation_flags_a_picture_whose_file_no_longer_exists(monkeypatch):
    from app.services import presentation_pictures

    ids = _ids()
    user_id = uuid.uuid4()
    existing = _existing_row(user_id, _picture_deck(ids))

    async def _load(db, file_ids, **_k):
        return {}

    monkeypatch.setattr(presentation_pictures, "load_picture_facts", _load)
    executor = _tools(monkeypatch, user_id, _FakeSession(_deck_lookup(existing)))
    content = _run(executor, "get_presentation", {"presentation_id": str(existing.id), "slides": "2"}).content
    assert f"- `{ids['shot']}`: MISSING - no platform file has this id, so nothing is drawn there" in content


def test_get_presentation_of_a_deck_without_pictures_looks_nothing_up(monkeypatch):
    from app.services import presentation_pictures

    user_id = uuid.uuid4()
    existing = _existing_row(user_id, validate_spec({"title": "Words", "slides": [{"layout": "title", "title": "Hi"}]}))

    async def _load(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("a picture-free deck costs no query")

    monkeypatch.setattr(presentation_pictures, "load_picture_facts", _load)
    executor = _tools(monkeypatch, user_id, _FakeSession(_deck_lookup(existing)))
    content = _run(executor, "get_presentation", {"presentation_id": str(existing.id)}).content
    assert "Pictures on" not in content


def test_get_presentation_tells_a_read_only_reader_what_is_shown_but_not_how_to_swap(monkeypatch):
    from app.services import presentation_pictures

    ids = _ids()
    owner, reader = uuid.uuid4(), uuid.uuid4()
    existing = _existing_row(
        owner, _picture_deck(ids), shared_user_ids=[str(reader)], shared_user_permissions={str(reader): "read_only"}
    )

    async def _load(db, file_ids, **_k):
        return {}

    monkeypatch.setattr(presentation_pictures, "load_picture_facts", _load)
    executor = _tools(monkeypatch, reader, _FakeSession(_deck_lookup(existing)))
    content = _run(executor, "get_presentation", {"presentation_id": str(existing.id), "slides": "2"}).content
    assert "Pictures on the slides shown" in content
    assert "replace_image" not in content


def test_edit_presentation_swaps_a_picture_and_names_the_new_file(monkeypatch):
    from app.services import presentation_pictures
    from app.services import presentations as presentations_module

    ids = _ids()
    new = str(uuid.uuid4())
    user_id = uuid.uuid4()
    existing = _existing_row(user_id, _picture_deck(ids))
    before = copy.deepcopy(existing.spec)
    verified: list[dict] = []

    async def _verify(db, spec, *, user, **_kw):
        verified.append(copy.deepcopy(spec))
        return []

    async def _load(db, file_ids, *, user_id, group_ids=None):
        assert set(file_ids) == {ids["logo"], new}
        # The account names a template as this user may see it: their groups count.
        assert group_ids == []
        return {
            ids["logo"]: _facts(ids["logo"], "opencti-2024.png"),
            new: _facts(new, "opencti-2026.svg", description="OpenCTI logo, 2026 version"),
        }

    monkeypatch.setattr(presentations_module, "verify_file_references", _verify)
    monkeypatch.setattr(presentation_pictures, "load_picture_facts", _load)
    session = _FakeSession(_deck_lookup(existing))
    executor = _tools(monkeypatch, user_id, session)
    result = _run(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "replace_image", "find": ids["logo"], "replace": new, "count": 3}],
            "expected_revision": 1,
        },
    )
    assert not result.is_error, result.content
    content = result.content
    assert "3 slides changed, 4 unchanged - verified byte-identical to revision 1" in content
    assert "Pictures swapped (3 places):" in content
    assert (
        f"`{ids['logo']}` (opencti-2024.png) -> `{new}` (opencti-2026.svg, template asset "
        '[product_logo]: "OpenCTI logo, 2026 version" - 2.0 KB)'
    ) in content
    assert 'slide 5 block 1 (architecture) node 1 logo_file_id "OpenCTI"' in content
    assert 'capture_presentation_slides(slides="3,4,5")' in content
    assert 'History: "Edited slides 3, 4, 5; 3 pictures swapped"' in content
    # The new id was checked against the acting user on the written slides.
    (checked,) = verified
    assert checked["slides"][2]["blocks"][0]["items"][1]["file_id"] == new
    # Saved in place, the rest untouched.
    assert existing.revision == 2 and session.committed
    for index in (0, 1, 5, 6):
        assert canonical(existing.spec["slides"][index]) == canonical(before["slides"][index])


def _edit_recording_lookups(monkeypatch, operations: list[dict]):
    """Run ``edit_presentation`` on the picture deck with every file vouched for,
    returning the result, the ids handed to ``load_picture_facts`` and the ids."""
    from app.services import presentation_pictures
    from app.services import presentations as presentations_module

    ids = _ids()
    user_id = uuid.uuid4()
    existing = _existing_row(user_id, _picture_deck(ids))
    asked: list[set[str]] = []

    async def _verify(db, spec, *, user, **_kw):
        return []

    async def _load(db, file_ids, **_k):
        asked.append(set(file_ids))
        return {file_id: _facts(file_id, f"{file_id[:8]}.png") for file_id in file_ids}

    monkeypatch.setattr(presentations_module, "verify_file_references", _verify)
    monkeypatch.setattr(presentation_pictures, "load_picture_facts", _load)
    executor = _tools(monkeypatch, user_id, _FakeSession(_deck_lookup(existing)))
    ops = [{key: (ids.get(value, value) if isinstance(value, str) else value) for key, value in op.items()} for op in operations]
    result = _run(executor, "edit_presentation", {"presentation_id": str(existing.id), "operations": ops})
    return result, asked, ids


def test_the_account_never_describes_a_picture_a_later_operation_removed(monkeypatch):
    """Swap a file in, remove it again in the same call - the saved deck
    never names it, so the writer never vouched for it, and the account must not
    look it up (that would answer "does this file exist, how big, since when?"
    for any id)."""
    probed = str(uuid.uuid4())
    result, asked, _ = _edit_recording_lookups(
        monkeypatch,
        [
            {"op": "replace_image", "slide": "2", "find": "shot", "replace": probed},
            {"op": "delete_block", "slide": "2", "block": "2"},
        ],
    )
    assert not result.is_error, result.content
    assert asked == [], "nothing the saved deck shows was swapped: nothing is looked up"
    history = result.content.split('History: "', 1)[1].split('"', 1)[0]
    assert "Pictures swapped" not in result.content and history == "Edited slide 2"


def test_a_chained_swap_describes_only_what_the_decks_show(monkeypatch):
    """A -> X then X -> Y: the account is X -> Y, Y is described (the saved deck
    shows it, vouched for), X is named by id alone - no deck, stored or saved,
    ever showed it, so it is neither looked up nor called "missing"."""
    x, y = str(uuid.uuid4()), str(uuid.uuid4())
    result, asked, _ = _edit_recording_lookups(
        monkeypatch,
        [
            {"op": "replace_image", "slide": "2", "find": "shot", "replace": x},
            {"op": "replace_image", "slide": "2", "find": x, "replace": y},
        ],
    )
    assert not result.is_error, result.content
    assert asked == [{y}]
    account = result.content.split("Pictures swapped", 1)[1].split('History: "', 1)[0]
    assert f"  `{x}` -> `{y}` ({y[:8]}.png, template asset [product_logo] - 2.0 KB)" in account
    assert "missing" not in account.lower()


@pytest.mark.parametrize("with_other_edit", [True, False], ids=["saved", "alone"])
def test_a_swap_reverted_in_the_same_call_is_not_in_the_edit_account(monkeypatch, with_other_edit):
    """Through the tool: the slide the reverted swap wrote is saved as it was,
    so the account names no swap, counts none and looks nothing up; alone, the
    call changed nothing and is a "No change" result, never a failure
    the loop's brake counts."""
    x = str(uuid.uuid4())
    operations = [
        {"op": "replace_image", "slide": "2", "find": "shot", "replace": x},
        {"op": "replace_image", "slide": "2", "find": x, "replace": "shot"},
    ]
    if with_other_edit:
        operations.append({"op": "update_slide", "slide": "3", "fields": {"title": "Our customers"}})
    result, asked, _ = _edit_recording_lookups(monkeypatch, operations)
    assert not result.is_error, result.content
    assert asked == [] and "Pictures swapped" not in result.content
    if with_other_edit:
        assert 'History: "Edited slide 3"' in result.content
    else:
        assert result.no_op and result.content.startswith("No change: the presentation already reads that way")


def test_a_failed_picture_lookup_never_fails_the_read_or_the_edit(monkeypatch):
    """What a file is, is a description: a lookup that fails costs the list on
    a read and the file names on an edit - never the read, never the save (the
    edit reads the files after its commit)."""
    from app.services import presentation_pictures
    from app.services import presentations as presentations_module

    ids = _ids()
    new = str(uuid.uuid4())
    user_id = uuid.uuid4()
    existing = _existing_row(user_id, _picture_deck(ids))

    async def _boom(*_a, **_k):
        raise RuntimeError("database went away")

    async def _verify(db, spec, *, user, **_kw):
        return []

    monkeypatch.setattr(presentation_pictures, "load_picture_facts", _boom)
    monkeypatch.setattr(presentations_module, "verify_file_references", _verify)

    executor = _tools(monkeypatch, user_id, _FakeSession(_deck_lookup(existing)))
    read = _run(executor, "get_presentation", {"presentation_id": str(existing.id), "slides": "2"}).content
    assert not read.startswith("Error") and "--- slide 2 [content]" in read
    assert "Pictures on" not in read

    session = _FakeSession(_deck_lookup(existing))
    executor = _tools(monkeypatch, user_id, session)
    result = _run(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "replace_image", "slide": "2", "find": ids["shot"], "replace": new}],
        },
    )
    assert not result.is_error, result.content
    assert session.committed and existing.revision == 2
    assert f"  `{ids['shot']}` -> `{new}`\n    at slide 2 block 2 (image)" in result.content
    assert "MISSING" not in result.content and "missing file" not in result.content


def _verified_session(existing, user_id: uuid.UUID, rows: list) -> _FakeSession:
    """The edit's queries in order, for the REAL ``verify_file_references``: the
    deck lookup, then the file rows the written slides name, then the user."""
    return _FakeSession(
        [*_deck_lookup(existing), _Result(rows=rows), _Result(one=SimpleNamespace(id=user_id, role="user"))]
    )


def test_an_edit_of_a_slide_whose_picture_is_gone_is_saved_and_says_so(monkeypatch):
    """A picture already on the slide whose file no longer exists (a removed
    built-in template's asset) never refuses the edit of that slide: the
    real verifier finds no row, the edit is saved with the picture left as it
    was, and the result and the history line name what is gone."""
    ids = _ids()
    user_id = uuid.uuid4()
    existing = _existing_row(user_id, _picture_deck(ids))
    session = _verified_session(existing, user_id, rows=[])
    executor = _tools(monkeypatch, user_id, session)
    result = _run(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "update_slide", "slide": "2", "fields": {"title": "Dashboards"}}],
        },
    )
    assert not result.is_error, result.content
    assert session.committed and existing.revision == 2
    assert existing.spec["slides"][1]["title"] == "Dashboards"
    assert existing.spec["slides"][1]["blocks"][1]["file_id"] == ids["shot"]
    assert "1 picture is no longer on the platform" in result.content
    assert "slide 2 block 2 (image)" in result.content
    assert "1 picture no longer on the platform left empty" in result.content


def test_the_gone_line_names_every_place_of_the_gone_files():
    from app.services.builtin_tools.presentation import gone_pictures_line

    ids = _ids()
    spec = _picture_deck(ids)
    gone = [("slides.1.blocks.1.file_id", "content", ids["shot"]), ("slides.2.blocks.0.items.0.file_id", "content", ids["acme"])]
    line = gone_pictures_line(spec, gone)
    assert line.startswith("2 pictures are no longer on the platform")
    assert "slide 2 block 2 (image)" in line and "slide 3 block 1 (logo_grid) item 1" in line
    assert gone_pictures_line(spec, []) == ""


@pytest.mark.parametrize("readable", [False, True], ids=["invented", "unreadable"])
def test_a_swap_to_a_file_the_writer_cannot_vouch_for_is_refused_and_saves_nothing(monkeypatch, readable):
    """``replace`` is vouched for against the WRITER like any reference an edit
    writes: a deck's readers may fetch what its slides name, so a swap
    to an invented id or to a file the writer cannot read would otherwise hand
    that file to every reader. Refused by the real verifier, nothing saved, and
    no "swap the picture already there" hint - the refused id is the swap's own."""
    from fastapi import HTTPException

    import app.services.file_access as file_access

    ids = _ids()
    user_id = uuid.uuid4()
    existing = _existing_row(user_id, _picture_deck(ids))
    before = copy.deepcopy(existing.spec)
    foreign = uuid.uuid4()
    checked: list[uuid.UUID] = []

    async def _authorize(row, _user, _db):
        checked.append(row.id)
        raise HTTPException(status_code=403, detail="not yours")

    monkeypatch.setattr(file_access, "authorize_file_access", _authorize)
    session = _verified_session(existing, user_id, rows=[SimpleNamespace(id=foreign)] if readable else [])
    executor = _tools(monkeypatch, user_id, session)
    result = _run(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "replace_image", "slide": "2", "find": ids["shot"], "replace": str(foreign)}],
        },
    )
    assert result.is_error and not result.no_op
    # One wording for both: the refusal never confirms the id exists.
    assert f"`{foreign}` is not a platform file you can read" in result.content
    assert "replace_image" not in result.content and "Nothing was saved. That picture" not in result.content
    assert checked == ([foreign] if readable else [])
    assert existing.revision == 1 and not session.committed
    assert canonical(existing.spec) == canonical(before)


def test_a_swap_of_a_file_not_shown_is_a_no_op_result_not_a_failure(monkeypatch):
    """Through the real handler: nothing to swap is one "No change"
    line flagged ``no_op`` - never an error the loop's failure brake counts -
    naming the pictures the scope does show, and nothing is saved."""
    ids = _ids()
    user_id = uuid.uuid4()
    existing = _existing_row(user_id, _picture_deck(ids))
    session = _FakeSession(_deck_lookup(existing))
    executor = _tools(monkeypatch, user_id, session)
    result = _run(
        executor,
        "edit_presentation",
        {
            "presentation_id": str(existing.id),
            "operations": [{"op": "replace_image", "slide": "3", "find": str(uuid.uuid4()), "replace": ids["shot"]}],
        },
    )
    assert result.no_op and not result.is_error
    assert result.content.startswith("No change: operation 1 (replace_image): file `")
    assert f"`{ids['acme']}`" in result.content and "nothing was saved (revision 1 kept)" in result.content
    assert existing.revision == 1 and not session.committed


def test_capture_lists_the_pictures_of_the_captured_slides(monkeypatch):
    from contextlib import asynccontextmanager

    from app.services import presentation_export, presentation_pictures, presentations
    from app.services.builtin_tools import _shared, build_presentation_tools

    ids = _ids()
    user_id = uuid.uuid4()
    deck = _existing_row(user_id, _picture_deck(ids))

    @asynccontextmanager
    async def _session():
        yield _FakeSession([])

    async def _load_deck(db, deck_id, *, user_id):
        return (deck, [])

    async def _render(presentation_id, user_id, **kwargs):
        return presentation_export.RenderedDeck(slide_count=7, slide_images=[b"png" for _ in kwargs["slide_indices"]])

    async def _persist(**kw):
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kw["filename"],
            content_type=kw["content_type"],
            size=len(kw["raw_bytes"]),
            s3_object_name=f"chat/conv/{kw['filename']}",
        )

    async def _facts_for(db, file_ids, **_k):
        assert set(file_ids) == {ids["acme"], ids["logo"]}
        return {ids["acme"]: _facts(ids["acme"], "acme.svg", role="customer_logo")}

    monkeypatch.setattr(_shared, "_own_session", _session)
    monkeypatch.setattr(presentations, "load_visible_presentation", _load_deck)
    monkeypatch.setattr(presentation_export, "render_deck", _render)
    monkeypatch.setattr(_shared, "_persist_generated_file", _persist)
    monkeypatch.setattr(presentation_pictures, "load_picture_facts", _facts_for)

    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    result = _run(executor, "capture_presentation_slides", {"presentation_id": str(deck.id), "slides": "3"})
    assert not result.is_error, result.content
    assert "Pictures on the captured slides (2 file(s), 2 place(s)):" in result.content
    assert f"- `{ids['acme']}`: acme.svg, template asset [customer_logo]" in result.content
    assert f"- `{ids['logo']}`: MISSING" in result.content
    assert "read_file_content" not in result.content


def _capture_with_a_save_in_between(monkeypatch, *, loads: list, rendered_revision: int | None):
    """``capture_presentation_slides`` of slide 3 where the deck row read BEFORE
    the render and AFTER it are *loads* in order, and the page rendered
    *rendered_revision*. Returns the result and the file ids looked up."""
    from contextlib import asynccontextmanager

    from app.services import presentation_export, presentation_pictures, presentations
    from app.services.builtin_tools import _shared, build_presentation_tools

    user_id = uuid.uuid4()
    pending = list(loads)
    asked: list[set[str]] = []

    @asynccontextmanager
    async def _session():
        yield _FakeSession([])

    async def _load_deck(db, deck_id, *, user_id):
        return (pending.pop(0), [])

    async def _render(presentation_id, user_id, **kwargs):
        return presentation_export.RenderedDeck(
            slide_count=7, slide_images=[b"png" for _ in kwargs["slide_indices"]], revision=rendered_revision
        )

    async def _persist(**kw):
        return SimpleNamespace(
            id=uuid.uuid4(), filename=kw["filename"], content_type=kw["content_type"], size=1, s3_object_name="k"
        )

    async def _facts_for(db, file_ids, **_k):
        asked.append(set(file_ids))
        return {}

    monkeypatch.setattr(_shared, "_own_session", _session)
    monkeypatch.setattr(presentations, "load_visible_presentation", _load_deck)
    monkeypatch.setattr(presentation_export, "render_deck", _render)
    monkeypatch.setattr(_shared, "_persist_generated_file", _persist)
    monkeypatch.setattr(presentation_pictures, "load_picture_facts", _facts_for)
    _defs, executor = build_presentation_tools(db=None, user_id=str(user_id), conversation_id=None)  # type: ignore[arg-type]
    deck_id = loads[0].id
    result = _run(executor, "capture_presentation_slides", {"presentation_id": str(deck_id), "slides": "3"})
    return result, asked


def test_capture_reads_the_picture_ids_off_the_revision_it_rendered(monkeypatch):
    """The page renders the CURRENT row. A save that lands between the load and
    the render - a parallel edit_presentation of the same response - puts other
    pictures in the PNG, so the ids are read off the revision the page rendered,
    never paired from the stale spec."""
    ids = _ids()
    before = _existing_row(uuid.uuid4(), _picture_deck(ids))
    swapped_logo = str(uuid.uuid4())
    after_spec = copy.deepcopy(before.spec)
    after_spec["slides"][2]["blocks"][0]["items"][1]["file_id"] = swapped_logo
    after = SimpleNamespace(id=before.id, title=before.title, spec=after_spec, revision=2)

    result, asked = _capture_with_a_save_in_between(monkeypatch, loads=[before, after], rendered_revision=2)
    assert not result.is_error, result.content
    assert asked == [{ids["acme"], swapped_logo}]
    assert f"`{swapped_logo}`" in result.content and f"`{ids['logo']}`" not in result.content


def test_capture_lists_no_pictures_it_cannot_pair_with_the_render(monkeypatch):
    """Moved again by the time it is re-read: no revision matches the PNG, so no
    list is better than a wrong one - the result says so and where to look."""
    ids = _ids()
    before = _existing_row(uuid.uuid4(), _picture_deck(ids))
    later = SimpleNamespace(id=before.id, title=before.title, spec=before.spec, revision=3)

    result, asked = _capture_with_a_save_in_between(monkeypatch, loads=[before, later], rendered_revision=2)
    assert not result.is_error, result.content
    assert asked == []
    assert "Pictures on" not in result.content
    assert "saved again while it was captured (revision 1 -> 2)" in result.content


def test_capture_trusts_its_spec_when_the_page_publishes_no_revision_or_the_same_one(monkeypatch):
    ids = _ids()
    for rendered_revision in (None, 1):
        deck = _existing_row(uuid.uuid4(), _picture_deck(ids))
        result, asked = _capture_with_a_save_in_between(monkeypatch, loads=[deck], rendered_revision=rendered_revision)
        assert asked == [{ids["acme"], ids["logo"]}], rendered_revision
        assert "Pictures on the captured slides (2 file(s), 2 place(s)):" in result.content


# ── The list stays bounded, and never strands a picture ─────────────────────


def _logo_wall_deck(files_per_slide: list[int]) -> tuple[dict, list[list[str]]]:
    """A deck whose slide N carries ``files_per_slide[N]`` distinct logos (walls of 8)."""
    slides, ids = [], []
    for count in files_per_slide:
        logos = [str(uuid.uuid4()) for _ in range(count)]
        ids.append(logos)
        walls = [logos[start : start + 8] for start in range(0, count, 8)]
        slides.append(
            {
                "layout": "content",
                "title": f"Wall of {count}",
                "blocks": [{"type": "logo_grid", "items": [{"file_id": f} for f in wall]} for wall in walls],
            }
        )
    return validate_spec({"title": "Walls", "slides": slides}), ids


def test_a_list_over_several_slides_names_the_slides_whose_pictures_it_left_out():
    from app.services.builtin_tools.presentation import format_picture_list

    spec, ids = _logo_wall_deck([10, 10, 10])
    listed = format_picture_list(picture_placements(spec), {}, spec=spec, scope="this deck")
    assert listed.count("\n- `") == 16, "the first sixteen files are described"
    assert '- and 14 more file(s), on slides 2, 3: get_presentation(slides="2") lists every picture of one slide.' in listed
    assert ids[2][9] not in listed


def test_a_list_of_one_slide_names_every_picture_on_it():
    """Reading fewer slides cannot help when one slide is served: past the
    description cap, every remaining file is still named, with its place."""
    from app.services.builtin_tools.presentation import format_picture_list

    spec, ids = _logo_wall_deck([40])
    listed = format_picture_list(picture_placements(spec), {}, spec=spec, scope="the slides shown")
    for file_id in ids[0]:
        assert f"`{file_id}`" in listed
    assert "more file(s)" not in listed
    assert f"- `{ids[0][39]}`: shown at slide 1 block 5 (logo_grid) item 8" in listed
    assert listed.count("MISSING") == 32, "32 described, the rest named by id and place only"


def test_a_count_refusal_names_a_bounded_number_of_places_and_the_exact_count():
    shared = str(uuid.uuid4())
    spec = validate_spec(
        {
            "title": "Backgrounds",
            "slides": [
                {"layout": "section", "title": f"Part {n}", "background_image_file_id": shared} for n in range(1, 21)
            ],
        }
    )
    with pytest.raises(PresentationPatchError) as exc:
        apply_operations(spec, [{"op": "replace_image", "find": shared, "replace": str(uuid.uuid4())}])
    message = str(exc.value)
    assert f"file `{shared}` is shown in 20 places" in message and "count=20" in message
    assert "slide 12 background image; and 8 more." in message
    assert "slide 13 " not in message


def test_a_withheld_file_is_told_by_what_it_is_never_by_its_name(monkeypatch):
    """The formatting half of the disclosure rule: a file whose name is not the
    reader's is "a file generated by another user", an asset of a template they
    cannot open keeps its role - and neither line invents a name or notes."""
    from app.services import presentation_pictures
    from app.services.builtin_tools.presentation import picture_facts_line, picture_name

    ids = _ids()
    theirs = _facts(ids["shot"], None, origin="generated", role=None)
    private = _facts(ids["acme"], None, role="customer_logo")
    assert picture_facts_line(theirs) == "a file generated by another user - 2.0 KB"
    assert picture_facts_line(private) == "template asset [customer_logo] of a template you cannot open - 2.0 KB"
    assert picture_name(theirs) == "a file generated by another user"
    assert picture_name(None) == "missing file"
    long_name = _facts(ids["logo"], "x" * 400 + ".png", origin="uploaded", role=None)
    assert len(picture_facts_line(long_name)) < 120, "a filename is clipped, never printed whole"

    owner, reader = uuid.uuid4(), uuid.uuid4()
    existing = _existing_row(
        owner, _picture_deck(ids), shared_user_ids=[str(reader)], shared_user_permissions={str(reader): "read_only"}
    )

    async def _load(db, file_ids, **_k):
        return {ids["shot"]: theirs}

    monkeypatch.setattr(presentation_pictures, "load_picture_facts", _load)
    executor = _tools(monkeypatch, reader, _FakeSession(_deck_lookup(existing)))
    content = _run(executor, "get_presentation", {"presentation_id": str(existing.id), "slides": "2"}).content
    assert f"- `{ids['shot']}`: a file generated by another user - 2.0 KB" in content


# ── The walk: one rule, and nothing past it ──────────────────────────


def _spec_model_fields() -> set[str]:
    """Every field name of every model reachable from the spec models."""
    import typing

    from pydantic import BaseModel

    from app.services import presentation_spec

    seen: set[type] = set()
    names: set[str] = set()

    def _models(annotation):
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            yield annotation
        for arg in typing.get_args(annotation):
            yield from _models(arg)

    def _walk(model: type[BaseModel]) -> None:
        if model in seen:
            return
        seen.add(model)
        for name, info in model.model_fields.items():
            names.add(name)
            for nested in _models(info.annotation):
                _walk(nested)

    for value in vars(presentation_spec).values():
        if isinstance(value, type) and issubclass(value, BaseModel) and value is not BaseModel:
            _walk(value)
    return names


#: The fields a deck shows a picture in, as the key rule sees them. A new one is
#: a conscious addition: it is vouched for, granted, served and swapped only
#: because its name follows the rule.
#: A video block's poster and WebVTT track are files the deck grants like a picture.
_PICTURE_FIELD_NAMES = {
    "file_id",
    "logo_file_id",
    "logo_light_file_id",
    "photo_file_id",
    "background_image_file_id",
    "poster_file_id",
    "captions_file_id",
}
#: Picture-sounding fields that hold no file: switches and sizes (a list row's
#: `logo_border` hairline included), and a logo wall group's `logos` list, whose
#: items name their picture as `file_id`.
_NOT_PICTURES = {"photo_size", "show_logo", "logo_position", "logo_height", "logo_border", "logos"}


def test_every_picture_field_of_the_spec_follows_the_key_rule():
    """The walk keys on the NAME (``file_id`` / ``*_file_id``), because a file
    id is a plain 36-character string in the models. A picture field named
    otherwise (``photo_id``, ``logo_asset``) would be neither vouched for when
    written nor granted to the deck's readers nor on the render allow-list:
    a slide with a hole and no error anywhere. This fails first."""
    needles = ("file", "photo", "logo", "image", "picture", "asset", "avatar", "portrait", "screenshot", "thumbnail")
    fields = _spec_model_fields()
    picture_like = {name for name in fields if any(needle in name for needle in needles)} - _NOT_PICTURES
    assert picture_like == _PICTURE_FIELD_NAMES
    assert all(is_file_reference_key(name) for name in picture_like)
    assert {name for name in fields if is_file_reference_key(name)} == _PICTURE_FIELD_NAMES


@pytest.mark.parametrize(
    ("key", "is_reference"),
    [
        ("file_id", True),
        ("logo_file_id", True),
        ("background_image_file_id", True),
        ("mark_file_id", True),
        ("file_ids", False),
        ("profile_id", False),
        ("xfile_id", False),
        ("file_id_2", False),
        ("File_Id", False),
        ("fileid", False),
        (3, False),
        (None, False),
    ],
)
def test_the_key_rule_neither_narrows_nor_widens(key, is_reference):
    """The exact rule the reader grant uses, spelled once:
    a narrower rule is a broken image on every shared deck, a wider one serves
    a file no writer vouched for."""
    assert is_file_reference_key(key) is is_reference


def test_a_file_id_in_prose_grants_nothing_and_is_not_on_the_allow_list():
    from app.services.presentation_export import referenced_file_ids
    from app.services.presentations import spec_references_file

    target = uuid.uuid4()
    spec = validate_spec(
        {
            "title": f"About {target}",
            "slides": [
                {
                    "layout": "content",
                    "title": str(target),
                    "notes": f"The file is {target}",
                    "blocks": [
                        {"type": "text", "content": f"see {target}"},
                        {"type": "logo_grid", "items": [{"file_id": str(uuid.uuid4()), "label": str(target)}]},
                        {"type": "image", "file_id": str(uuid.uuid4()), "alt": str(target), "caption": str(target)},
                    ],
                }
            ],
        }
    )
    assert not spec_references_file(spec, target)
    assert str(target) not in referenced_file_ids(spec)
    assert str(target) not in {value for _path, _layout, value in slide_file_references(spec)}


def test_every_theme_file_the_template_draws_stays_on_the_render_allow_list():
    """What the export and the layout check may fetch beyond the slides: the
    template's logo, its light variant and the hero watermark. The walk
    moving to its own module must not have cost the render any of them."""
    from app.services.presentation_export import referenced_file_ids
    from app.services.presentations import THEME_FILE_ID_KEYS

    assert set(THEME_FILE_ID_KEYS) == {"logo_file_id", "logo_light_file_id", "mark_file_id"}
    theme = {key: str(uuid.uuid4()) for key in THEME_FILE_ID_KEYS}
    assert set(referenced_file_ids({"slides": []}, theme)) == set(theme.values())


# ── What a file is: the loader ───────────────────────────────────────────────


def test_load_picture_facts_tells_a_reader_only_what_is_theirs_to_read():
    """A deck grants its readers the PIXELS its slides show, not every
    word written about them. A template asset's filename, caption and template
    are told under the template's visibility; a conversation file's name and
    the notes an agent saved on it (``annotate_file``) only to its owner - the
    author's generated screenshot is "a file generated by another user" to
    everyone else, however the deck was shared."""
    from app.models.template_container import TEMPLATE_ASSET_SOURCE
    from app.services.presentation_pictures import load_picture_facts

    visible_tpl, private_tpl = uuid.uuid4(), uuid.uuid4()
    reader, author = uuid.uuid4(), uuid.uuid4()

    def _row(**overrides):
        values = {
            "id": uuid.uuid4(),
            "user_id": author,
            "filename": "f.png",
            "content_type": "image/png",
            "size": 10,
            "s3_object_name": "k",
            "source": "chat",
            "description": None,
            "asset_role": None,
            "template_container_id": None,
            "created_at": datetime(2026, 9, 1, tzinfo=UTC),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    shared_asset = _row(
        source=TEMPLATE_ASSET_SOURCE, template_container_id=visible_tpl, asset_role="screenshot", description=" Shot "
    )
    private_asset = _row(
        source=TEMPLATE_ASSET_SOURCE,
        template_container_id=private_tpl,
        asset_role="logo",
        filename="acme-confidential.png",
        description="ACME, confidential engagement",
    )
    generated = _row(source="agent_generated", filename="q3-pricing.png", description="Draft pricing, not public")
    upload = _row(source="chat", user_id=reader, filename="mine.png", description="My own notes")
    session = _FakeSession(
        [
            _Result(rows=[shared_asset, private_asset, generated, upload]),
            _Result(rows=[(visible_tpl, "Brand Deck")]),
        ]
    )
    missing = str(uuid.uuid4())
    wanted = [str(r.id) for r in (shared_asset, private_asset, generated, upload)] + [missing, "not-a-uuid"]
    facts = asyncio.run(load_picture_facts(session, wanted, user_id=reader, group_ids=[]))

    told = facts[str(shared_asset.id)]
    assert (told.filename, told.description, told.template_name) == ("f.png", "Shot", "Brand Deck")
    assert told.template_id == str(visible_tpl) and not told.withheld
    assert told.added.isoformat() == "2026-09-01"
    # An asset of a template the reader cannot see: its role, never its
    # template, its file name or its caption.
    private = facts[str(private_asset.id)]
    assert (private.origin, private.role) == ("template asset", "logo")
    assert private.withheld and private.description is None
    assert private.template_name is None and private.template_id is None
    # The author's generated file: what it is, never what the author's agent called it or wrote about it.
    theirs = facts[str(generated.id)]
    assert (theirs.origin, theirs.filename, theirs.description, theirs.size) == ("generated", None, None, 10)
    # The reader's own upload: everything.
    mine = facts[str(upload.id)]
    assert (mine.origin, mine.filename, mine.description) == ("uploaded", "mine.png", "My own notes")
    assert missing not in facts
    # Two queries: the rows, then the visible templates - with the visibility
    # predicate in the WHERE clause, not a bare id lookup.
    from sqlalchemy.dialects import postgresql

    assert len(session.statements) == 2
    template_sql = str(session.statements[1].compile(dialect=postgresql.dialect()))
    assert "template_containers" in template_sql and "is_company_managed" in template_sql


def test_load_picture_facts_without_a_uuid_runs_no_query():
    from app.services.presentation_pictures import load_picture_facts

    session = _FakeSession()
    assert asyncio.run(load_picture_facts(session, ["nope", ""], user_id=None)) == {}
    assert session.statements == []


def test_an_admin_is_told_the_pictures_of_a_colleague_s_private_template():
    """On the REST surface an admin reads a colleague's deck dressed in its
    template - palette and name - so the alt text of its pictures follows the
    same bypass instead of falling back to their role."""
    from sqlalchemy.dialects import postgresql

    from app.models.template_container import TEMPLATE_ASSET_SOURCE
    from app.services.presentation_pictures import load_picture_facts

    private_tpl = uuid.uuid4()
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        filename="acme-logo.png",
        content_type="image/png",
        size=10,
        s3_object_name="k",
        source=TEMPLATE_ASSET_SOURCE,
        description="ACME logo",
        asset_role="logo",
        template_container_id=private_tpl,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    session = _FakeSession([_Result(rows=[asset]), _Result(rows=[(private_tpl, "ACME brand")])])
    facts = asyncio.run(load_picture_facts(session, [str(asset.id)], user_id=uuid.uuid4(), is_admin=True))

    told = facts[str(asset.id)]
    assert (told.filename, told.description, told.template_name) == ("acme-logo.png", "ACME logo", "ACME brand")
    template_sql = str(session.statements[1].compile(dialect=postgresql.dialect()))
    assert "is_company_managed" not in template_sql


def test_the_rest_deck_response_takes_the_admin_bypass_for_its_pictures_and_the_tools_never_do():
    """``_response`` hands the admin bypass to the picture labels exactly as to
    the palette; the deck agent tools never pass it (their no-bypass rule)."""
    import ast

    app = Path(__file__).resolve().parents[1] / "app"
    api_tree = ast.parse((app / "api" / "v1" / "presentations.py").read_text(encoding="utf-8"))
    response = next(n for n in ast.walk(api_tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_response")
    flags = {
        node.func.id: next((ast.unparse(kw.value) for kw in node.keywords if kw.arg == "is_admin"), None)
        for node in ast.walk(response)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"resolve_template_palette", "load_quote_picture_labels"}
    }
    assert flags == {"resolve_template_palette": "user.is_admin", "load_quote_picture_labels": "user.is_admin"}

    tools = ast.parse((app / "services" / "builtin_tools" / "presentation.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tools)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) in {"load_picture_facts", "load_quote_picture_labels"}
    ]
    assert calls
    assert all(keyword.arg != "is_admin" for call in calls for keyword in call.keywords)

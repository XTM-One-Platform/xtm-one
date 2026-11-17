"""The cards a template adds, set from the spec. Synthetic templates and specs only.

A video template adds its opening and closing cards to a video that does not
open or close on one (``templates.apply_template``): they are not in the spec
the author edits, so a quality finding on one had no fix. What is proven,
from the spec as written to the planned timeline and the quality check:

* ``template_scenes: {intro: {duration}, outro: {duration}}`` sets the length
  of the cards the template adds, and only of those - a card the author
  writes keeps its own ``duration``;
* the planned timeline gives the card that length;
* a reading-time (or still-coming-in) finding on a template's card names
  ``template_scenes.<card>.duration`` as its fix, one on the author's own
  scene its ``duration``;
* the spec refuses a length out of bounds, and the tools say the field exists.
"""

from __future__ import annotations

import pytest

from app.services.video_studio import quality, templates, timeline
from app.services.video_studio.spec import VideoSpecError, authored_spec, parse_video_spec

#: The intro card of a brand's template: the video's title over a long tagline the author never wrote.
_INTRO = {
    "required": True,
    "scene": {
        "title": "{title}",
        "subtitle": "Every alert enriched, routed and closed before your analysts open it, at any scale",
        "duration": 2.5,
    },
}


def _template(**style) -> templates.VideoTemplate:
    return templates.VideoTemplate(
        id="t", name="Brand", kind="video", style=templates.VideoTemplateStyle.model_validate(style)
    )


def _authored(**top) -> dict:
    return authored_spec(
        {"title": "Threat intelligence in practice", "scenes": [{"id": "s1", "type": "statement", "lines": ["One"]}], **top}
    )


def _planned(applied: templates.AppliedTemplate) -> dict:
    return timeline.estimate_plan(parse_video_spec(applied.spec), {}).document


def _audit_reading(document: dict, index: int) -> dict:
    """What the browser measures of the scene at *index*: its text fully in at once, shown for the whole scene."""
    scene = document["scenes"][index]
    start, end = float(scene["start"]), float(scene["end"])
    return {
        "scenes": [
            {"index": index, "start": start, "end": end, "shown_until": end, "fit": 1.0, "reveal_end": 0.4, "findings": []}
        ]
    }


def test_the_spec_sets_the_length_of_the_card_its_template_adds():
    template = _template(intro=_INTRO, outro={"required": True, "scene": {"title": "Thank you"}})
    plain = templates.apply_template(_authored(), template)
    assert plain.added_scenes == ["intro", "outro"]
    assert plain.spec["scenes"][0]["duration"] == 2.5

    applied = templates.apply_template(_authored(template_scenes={"intro": {"duration": 7}, "outro": {"duration": 3}}), template)
    intro, outro = applied.spec["scenes"][0], applied.spec["scenes"][-1]
    assert (intro["id"], intro["duration"]) == ("intro", 7.0) and (outro["id"], outro["duration"]) == ("outro", 3.0)
    # The planned timeline plays the card that long.
    planned = _planned(applied)["scenes"][0]
    assert planned["id"] == "intro" and float(planned["end"]) - float(planned["start"]) >= 7.0


def test_a_card_the_author_writes_keeps_its_own_duration():
    template = _template(intro=_INTRO)
    own = authored_spec(
        {
            "title": "T",
            "template_scenes": {"intro": {"duration": 9}},
            "scenes": [{"id": "open", "type": "title", "title": "Mine", "duration": 4}, {"type": "statement", "lines": ["x"]}],
        }
    )
    applied = templates.apply_template(own, template)
    assert applied.added_scenes == [] and applied.spec["scenes"][0]["duration"] == 4.0


def test_a_reading_finding_on_a_template_card_names_the_field_that_fixes_it():
    template = _template(intro=_INTRO)
    authored = _authored()
    applied = templates.apply_template(authored, template)
    document = _planned(applied)
    [graded] = quality.findings_from_audit(_audit_reading(document, 0), document, authored)
    assert (graded.kind, graded.scene_id, graded.scene_index) == ("reading", "intro", None)
    assert "s long (template_scenes.intro.duration) or cut words" in graded.message
    assert quality.format_quality_report(quality.QualityReport(scene_count=2, findings=(graded,))).count(
        "the template's title scene"
    ) == 1

    # Set as the finding says: the card is long enough to read, and the check says nothing of it.
    fixed = _authored(template_scenes={"intro": {"duration": 8}})
    document = _planned(templates.apply_template(fixed, template))
    assert quality.findings_from_audit(_audit_reading(document, 0), document, fixed) == []


def test_a_finding_on_the_authors_own_card_names_its_duration():
    own = authored_spec(
        {"title": "T", "scenes": [{"id": "open", "type": "title", "title": "Mine", "subtitle": _INTRO["scene"]["subtitle"], "duration": 2}]}
    )
    document = _planned(templates.apply_template(own, None))
    [graded] = quality.findings_from_audit(_audit_reading(document, 0), document, own)
    assert graded.scene_index == 0 and "s long (duration) or cut words" in graded.message


def test_the_spec_bounds_a_cards_length_and_names_only_the_cards_a_template_adds():
    with pytest.raises(VideoSpecError, match=r"template_scenes\.intro\.duration"):
        _authored(template_scenes={"intro": {"duration": 0.2}})
    with pytest.raises(VideoSpecError, match=r"template_scenes\.middle"):
        _authored(template_scenes={"middle": {"duration": 4}})
    assert templates.card_name("title") == "intro" and templates.card_name("outro") == "outro"


def test_the_tools_say_how_to_set_the_cards_a_template_adds():
    from app.services.builtin_tools import video_studio as tool

    template = _template(intro=_INTRO)
    text = tool._template_text(templates.apply_template(_authored(), template))
    assert "It adds the scenes intro around yours" in text and "template_scenes: {intro: {duration, kicker, align}" in text
    definitions, _executor = tool.build_video_studio_tools(None, "00000000-0000-0000-0000-00000000a064", None)
    described = " ".join(d.description for d in definitions if d.name in ("create_video", "edit_video"))
    assert "template_scenes? {intro?: {duration?, kicker?, align? left|center}, outro?: {duration?, kicker?}}" in described

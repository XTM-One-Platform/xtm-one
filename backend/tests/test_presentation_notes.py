"""Speaker notes as an authoring concern + the native speaker view.

Two halves. The service tests pin how notes are measured (words, speaking
time, which slides owe notes) and flattened for PowerPoint. The frontend
tripwires pin the SHAPE of the speaker view: its own route (never the viewer
re-opened in a popup - that is how the studio rail ended up on the presenter's
second screen), a BroadcastChannel bridge keyed on the deck, and the one
words-per-minute figure shared with the backend so a slide takes the same time
to say in a tool result, the editor panel and the speaker window.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services import presentation_notes as notes
from app.services.builtin_tools.presentation import notes_line, summarize_slides
from app.services.pptx_writer import _set_notes

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"
PRESENTATION_DIR = FRONTEND_ROOT / "src" / "components" / "presentation"
PAGES_DIR = FRONTEND_ROOT / "src" / "pages"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── Measuring ──────────────────────────────────────────────────────────────────


def test_words_are_counted_on_the_spoken_text_not_the_markup():
    assert notes.count_words(None) == 0
    assert notes.count_words("   ") == 0
    assert notes.count_words("Land the **42%** figure, then _pause_.") == 6
    # A marker is not a word; a hyphenated or elided word is one word.
    assert notes.count_words("`code` [[warning:risk]]") == 2
    assert notes.count_words("well-known, don't") == 2
    # An unknown tone renders as its text with the wrapper dropped (that is
    # what the speaker view shows), so the presenter reads - and says - one word.
    assert notes.count_words("[[warn:risk]]") == 1


def test_scripts_without_spaces_are_counted_by_the_character():
    # A Japanese sentence is one `\w+` run; per character at 300 a minute it is
    # 12 chars -> ceil(12 * 130 / 300) = 6 word-equivalents, not one word.
    assert notes.count_words("これは日本語のテストです") == 6
    assert notes.estimate_speaking_seconds("これは日本語のテストです") == 3
    # Mixed script: the Latin word counts as one, the 11 CJK characters as 5.
    assert notes.count_words("OpenCTI は 脅威 インテリジェンス") == 6
    # Half-width katakana and ideographs alike; Korean keeps its spaces and is
    # read by the word rule (two words), not by the character.
    assert notes.count_words("ﾃｽﾄ") == 2
    assert notes.count_words("안녕하세요 세계") == 2
    # A CJK character never breaks the Latin count around it.
    assert notes.count_words("say 例 now") == 3
    # 300 characters take a minute, exactly as 130 words do.
    assert notes.estimate_speaking_seconds("日" * notes.CJK_CHARS_PER_MINUTE) == 60


def test_speaking_time_is_the_shared_pace_and_never_zero_for_spoken_text():
    assert notes.estimate_speaking_seconds(None) == 0
    assert notes.estimate_speaking_seconds("one") == 1
    # 130 words at 130 wpm is one minute exactly.
    assert notes.estimate_speaking_seconds(" ".join(["word"] * notes.WORDS_PER_MINUTE)) == 60
    assert notes.estimate_speaking_seconds(" ".join(["word"] * 65)) == 30


def test_speaking_time_uses_the_platform_duration_form():
    # Same compact form as `format_wait_duration` / `formatDurationShort`.
    assert notes.format_speaking_time(0) == "0s"
    assert notes.format_speaking_time(45) == "45s"
    assert notes.format_speaking_time(120) == "2m"
    assert notes.format_speaking_time(750) == "12m 30s"
    assert notes.format_speaking_time(3900) == "1h 5m"
    assert notes.format_speaking_time(-5) == "0s"


def test_plain_text_strips_markup_and_keeps_the_lines():
    text = notes.notes_plain_text("Open with **the number**.\r\n- first _point_\n- `second`\n\nClose.")
    assert text == "Open with the number.\n- first point\n- second\n\nClose."
    assert notes.notes_plain_text(None) == ""
    assert notes.notes_plain_text("  \n ") == ""


def test_plain_text_keeps_a_line_the_tokenizer_refuses():
    # Notes are not markup-validated at save time; an unbalanced marker is
    # kept as written rather than dropped from the presenter's notes.
    raw = "a stray ** marker and an unclosed [[warn:tone"
    assert raw in notes.notes_plain_text(raw)


def test_plain_text_reads_the_notes_the_way_the_speaker_view_renders_them():
    # The frontend tokenizer never refuses a line: an unknown tone shows its
    # text without the wrapper, a tag-like sequence stays literal and the
    # markers around it are still stripped. The PPTX pane and the word count
    # must see that same text - not the raw line the strict parser falls back
    # to, which is not what the presenter reads on the second screen.
    assert notes.notes_plain_text("[[warn:risk]] ahead") == "risk ahead"
    assert notes.notes_plain_text("keep <b>this</b> and **that**") == "keep <b>this</b> and that"
    # ...and is counted as printed: the two `b` of the literal tag are words to
    # the word rule on both sides, so the estimate agrees with the editor panel.
    assert notes.count_words("keep <b>this</b> and **that**") == 6


# ── Coverage ───────────────────────────────────────────────────────────────────


def _deck(*slides: tuple[str, str | None]) -> list[dict]:
    return [{"layout": layout, "title": f"S{i}", "notes": text} for i, (layout, text) in enumerate(slides)]


def test_coverage_owes_notes_on_content_slides_only():
    cov = notes.notes_coverage(
        _deck(("title", None), ("content", "Say this."), ("content", None), ("agenda", None), ("closing", None))
    )
    assert cov.slide_count == 5
    assert cov.content_count == 2
    assert cov.with_notes == 1
    assert cov.missing == (3,)
    assert not cov.complete
    assert cov.total_seconds == notes.estimate_speaking_seconds("Say this.")


def test_whitespace_only_notes_do_not_count_as_notes():
    cov = notes.notes_coverage(_deck(("content", "   \n"), ("content", "Real.")))
    assert cov.with_notes == 1
    assert cov.missing == (1,)


def test_coverage_reads_spec_models_as_well_as_dicts():
    class Slide:
        def __init__(self, layout: str, text: str | None) -> None:
            self.layout = layout
            self.notes = text

    cov = notes.notes_coverage([Slide("content", "a b c"), Slide("section", None)])
    assert cov.content_count == 1 and cov.with_notes == 1 and cov.complete


def test_coverage_line_for_an_empty_deck_is_empty():
    assert notes.format_notes_coverage(notes.notes_coverage([])) == ""
    assert notes_line({"slides": []}) == ""


def test_coverage_line_names_the_missing_slides_and_the_length():
    line = notes.format_notes_coverage(
        notes.notes_coverage(_deck(("title", None), ("content", " ".join(["w"] * 130)), ("content", None)))
    )
    assert line.startswith("Speaker notes: 1/3 slides, about 1m spoken at 130 wpm.")
    assert line.endswith("Missing on content slide 3.")


def test_coverage_line_for_a_complete_deck_says_so():
    line = notes.format_notes_coverage(notes.notes_coverage(_deck(("title", None), ("content", "Talk."))))
    assert "Every content slide has notes." in line


def test_coverage_line_with_no_notes_at_all_tells_the_agent_what_notes_are():
    line = notes.format_notes_coverage(
        notes.notes_coverage(_deck(("title", None), ("content", None), ("content", None)))
    )
    # Same shape as every other coverage line: count, (zero) estimate, pace.
    assert line.startswith("Speaker notes: none yet (0/3 slides, about 0s spoken at 130 wpm).")
    assert "not the bullets read back" in line
    # The slides are still named: the agent addresses them by number, as it
    # does on every other coverage line.
    assert line.endswith("Missing on content slides 2, 3.")


def test_coverage_line_for_a_deck_of_structural_slides_only_owes_nothing():
    line = notes.format_notes_coverage(notes.notes_coverage(_deck(("title", None), ("section", None))))
    assert line.startswith("Speaker notes: 0/2 slides, about 0s spoken")
    assert line.endswith("Every content slide has notes.")


def test_coverage_line_bounds_the_slide_list():
    cov = notes.notes_coverage(_deck(("content", "x"), *[("content", None)] * 20))
    line = notes.format_notes_coverage(cov)
    assert "and 8 more" in line
    assert line.count(",") <= 12


# ── The outline the agent edits from ───────────────────────────────────────────


def test_outline_flags_content_slides_without_notes_on_a_deck():
    spec = {
        "aspect_ratio": "16:9",
        "slides": [
            {"layout": "title", "title": "Cover"},
            {"layout": "content", "title": "Body", "notes": "Say it."},
            {"layout": "content", "title": "Silent"},
            {"layout": "closing", "title": "Thanks"},
        ],
    }
    outline = summarize_slides(spec).splitlines()
    assert outline[0] == "  1. [title] Cover"
    assert outline[1] == "  2. [content] Body"
    assert outline[2] == "  3. [content] Silent (no notes)"
    assert outline[3] == "  4. [closing] Thanks"


def test_outline_never_flags_a_page():
    # A portrait document is read, not presented: nothing is owed.
    spec = {"aspect_ratio": "A4", "slides": [{"layout": "content", "title": "Brief"}]}
    assert summarize_slides(spec) == "  1. [content] Brief"


def test_tool_results_carry_the_coverage_line_for_decks_only():
    source = _read(BACKEND_ROOT / "app" / "services" / "builtin_tools" / "presentation.py")
    assert source.count('notes_line(spec) if spec_kind(spec) == "deck" else ""') == 4, (
        "generate_presentation, edit_presentation, get_presentation and "
        "duplicate_presentation each append the coverage line, decks only"
    )


def test_guidance_asks_for_notes_as_speech_at_the_shared_pace():
    from app.services import presentation_guidance as guidance

    text = guidance.build_generate_presentation_description()
    assert "speaker notes on every content slide" in text
    assert f"{notes.WORDS_PER_MINUTE} words a minute" in text
    assert "never the bullets read back" in text


# ── PowerPoint ─────────────────────────────────────────────────────────────────


def test_pptx_notes_pane_gets_plain_text():
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_notes(slide, "Land the **42%**.\n- then _pause_")
    assert slide.notes_slide.notes_text_frame.text == "Land the 42%.\n- then pause"


def test_pptx_skips_the_notes_pane_when_there_is_nothing_to_say():
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_notes(slide, "   ")
    assert not slide.has_notes_slide


# ── Frontend tripwires ─────────────────────────────────────────────────────────


def test_frontend_shares_the_pace_and_the_optional_layouts():
    ts = _read(PRESENTATION_DIR / "speaking-time.ts")
    wpm = re.search(r"export const WORDS_PER_MINUTE = (\d+);", ts)
    assert wpm and int(wpm.group(1)) == notes.WORDS_PER_MINUTE
    optional = re.search(r"NOTES_OPTIONAL_LAYOUTS[^\[]*\[([^\]]*)\]", ts, re.S)
    assert optional
    assert set(re.findall(r'"([a-z_]+)"', optional.group(1))) == set(notes.NOTES_OPTIONAL_LAYOUTS)


def test_frontend_counts_cjk_characters_at_the_same_pace_over_the_same_class():
    """A Japanese note must take the same time in the editor panel, the speaker
    view and the tool result: the per-character pace AND the character class
    (which scripts are counted that way) are one figure on both sides."""
    ts = _read(PRESENTATION_DIR / "speaking-time.ts")
    cpm = re.search(r"export const CJK_CHARS_PER_MINUTE = (\d+);", ts)
    assert cpm and int(cpm.group(1)) == notes.CJK_CHARS_PER_MINUTE
    ts_class = re.search(r"const CJK_RE = /\[([^\]]+)\]/gu;", ts)
    assert ts_class, "speaking-time.ts lost its CJK character class"
    py_class = re.search(r"\[([^\]]+)\]", notes._CJK_RE.pattern)
    assert py_class and ts_class.group(1) == py_class.group(1)
    # Counted on the plain text, Latin words around a CJK run kept.
    assert "text.replace(CJK_RE, " in ts and "Math.ceil((cjk * WORDS_PER_MINUTE) / CJK_CHARS_PER_MINUTE)" in ts


def test_the_speaker_view_is_its_own_route_not_the_viewer_in_a_popup():
    """The reveal notes plugin opened the CURRENT page in a popup: with the
    studio open, the presenter's second screen carried the chat rail and the
    toolbar. The speaker view is a dedicated route with nothing but current /
    next / notes / clock on it."""
    deck = _read(PRESENTATION_DIR / "PresentationDeck.tsx")
    assert "RevealNotes" not in deck and "plugin/notes" not in deck, "the reveal notes popup is back"
    app = _read(FRONTEND_ROOT / "src" / "App.tsx")
    assert 'path="/presentations/:id/speaker"' in app
    assert 'path="/presentations/:id/notes"' in app
    speaker = _read(PAGES_DIR / "PresentationSpeaker.tsx")
    for forbidden in ("PresentationStudioRail", "DeckToolbar", "<DeckStage", "<PresentationDeck"):
        assert forbidden not in speaker, f"the speaker window renders {forbidden}"
    # Opened by name: a second `S` focuses the existing window instead of
    # stacking popups on the presenter's screen. The route path goes through
    # `withBasePath` at the `window.open` sink - a browser sink, not
    # router navigation, so it must carry the platform's URL path prefix.
    url = _read(PRESENTATION_DIR / "speaker-url.ts")
    assert "speakerWindowName(" in url
    assert "window.open(withBasePath(speakerViewUrl(" in url


def test_the_stage_and_the_speaker_window_talk_over_a_channel_keyed_on_the_deck():
    sync = _read(PRESENTATION_DIR / "speaker-sync.ts")
    assert "new BroadcastChannel(" in sync
    assert re.search(r"export function speakerChannelName\(presentationId: string\)", sync)
    assert "useSpeakerStage" in sync and "useSpeakerFollower" in sync
    # The stage publishes its revision so the window refetches a deck edited
    # in place before showing stale notes over a new slide.
    assert "revision" in sync
    stage = _read(PRESENTATION_DIR / "DeckStage.tsx")
    assert "useSpeakerStage({" in stage
    speaker = _read(PAGES_DIR / "PresentationSpeaker.tsx")
    assert "useSpeakerFollower(" in speaker
    assert "stageRevision" in speaker and "invalidateQueries" in speaker
    # A closed tab never runs the effect cleanup: the `bye` rides `pagehide` too,
    # or the window follows a dead stage until the watchdog gives up on it.
    assert 'window.addEventListener("pagehide", bye)' in sync
    # Switching stages on a move files the one left behind among the others
    # with its last frame; forgetting it until its next heartbeat left a gap
    # in which the new stage closing dropped the window to rehearsal beside a
    # live deck.
    assert "others.set(previous, { state: followedStateRef.current, heard: lastHeardRef.current })" in sync
    # ...and a stage that is GONE must never be filed that way: the drop clears
    # the refs before it picks the successor.
    assert re.search(
        r"const dropFollowed = \(\) => \{\s*followedRef\.current = null;\s*followedStateRef\.current = null;", sync
    )


def test_the_bridge_is_driven_end_to_end_at_the_stage_heartbeat():
    """The Playwright spec proves a heartbeat does not move the window by
    waiting one out; a heartbeat made longer than that wait would turn the
    proof into a pass that checked nothing."""
    sync = _read(PRESENTATION_DIR / "speaker-sync.ts")
    heartbeat = re.search(r"^export const STAGE_HEARTBEAT_MS = (\d+);", sync, re.M)
    assert heartbeat, "STAGE_HEARTBEAT_MS moved"
    spec = _read(FRONTEND_ROOT / "tests_e2e" / "presentation-speaker.spec.ts")
    mirrored = re.search(r"^const STAGE_HEARTBEAT_MS = (\d+);", spec, re.M)
    assert mirrored and mirrored.group(1) == heartbeat.group(1), "the e2e heartbeat wait drifted from the stage"
    for scenario in (
        "Rehearsal - no presentation window connected",
        "Following the presentation",
        "'b'",
        "closes within a heartbeat",
    ):
        assert scenario in spec, f"the e2e spec no longer covers {scenario}"


def test_the_speaker_routes_send_a_page_back_to_its_reader():
    """A page has no bridge and no `S`; a typed `/speaker` or `/notes` URL for
    one lands on the page's own reader instead of a deck of pages."""
    for name in ("PresentationSpeaker.tsx", "PresentationNotes.tsx"):
        source = _read(PAGES_DIR / name)
        assert re.search(r'if \(data\.kind !== "deck"\) \{\s*return <Navigate to=\{`/presentations/\$\{data\.id\}`\} replace />;', source), name


def test_the_s_key_and_the_menus_open_the_speaker_view_for_decks_only():
    keys = _read(PRESENTATION_DIR / "deck-keys.ts")
    assert re.search(r"^\s+S: 83,", keys, re.M)
    assert '"speaker"' in keys
    deck = _read(PRESENTATION_DIR / "PresentationDeck.tsx")
    assert re.search(r'\[DECK_KEY_CODES\.S\]: \(\) => \w+(?:\.current)?\?\.\("speaker"\)', deck)
    stage = _read(PRESENTATION_DIR / "DeckStage.tsx")
    # A deck, unless its host turns the presenter tools off (a public link).
    assert "const speakerTools = !isDocumentFormat(presentation.spec.aspect_ratio) && presenterTools;" in stage
    assert "onOpenSpeakerView={speakerTools ? openSpeaker : undefined}" in stage
    assert "onOpenNotesHandout={speakerTools ? openHandout : undefined}" in stage
    assert "presenterTools={false}" in _read(PAGES_DIR / "PresentationPublic.tsx")
    toolbar = _read(PRESENTATION_DIR / "DeckToolbar.tsx")
    assert 't_i18n("Speaker view")' in toolbar and 't_i18n("Notes handout")' in toolbar
    library = _read(PAGES_DIR / "Presentations.tsx")
    assert "openSpeakerView(deck.id)" in library and "openNotesHandout(deck.id)" in library


def test_the_speaker_window_has_the_presenter_essentials():
    speaker = _read(PAGES_DIR / "PresentationSpeaker.tsx")
    for essential in (
        "useSpeakerTimer(",  # the clock, persisted across an accidental reload
        "<SpeakerNotes",  # the notes, rendered from the same inline markup
        'fit="contain"',  # current slide letterboxed so the notes keep the floor
        "toggleBlackout",  # black the room's screen from the lectern
        "speakingSecondsBefore",  # pacing against the notes' own estimate
        "Rehearsal",  # works with no stage connected
    ):
        assert essential in speaker, f"speaker view lost {essential}"
    css = _read(PRESENTATION_DIR / "speaker.css")
    assert "--notes-scale" in css, "the presenter can no longer size the notes"
    assert not re.search(r"^\s*@page\b", css, re.M), "an unscoped @page rule leaks into every other print path"


def test_the_handout_prints_the_same_thumbnail_width_it_shows():
    """The thumbnail scales from its measured width; a print pass is not
    guaranteed to run the resize observer again, so the column is the same
    width on screen and on paper."""
    css = _read(PRESENTATION_DIR / "speaker.css")
    screen, printed = css.split("@media print {", 1)
    screen_row = re.search(r"\.handout-row \{([^}]*)\}", screen)
    assert screen_row and re.search(r"grid-template-columns:\s*\d+px ", screen_row.group(1)), (
        "the thumbnail column must be a fixed pixel width"
    )
    print_row = re.search(r"\.handout-row \{([^}]*)\}", printed)
    assert print_row and "grid-template-columns" not in print_row.group(1), "print must not re-size the column"
    handout = _read(PAGES_DIR / "PresentationNotes.tsx")
    assert "<SlidePreview" in handout and "<SpeakerNotes" in handout
    assert "window.print()" in handout


def test_the_narrow_speaker_window_sizes_its_rows_to_their_content():
    """A floating speaker window next to the deck stacks its two columns. The
    grid container has a definite height (the shell's middle row), so plain
    `auto` rows are STRETCHED to equal shares of it and two columns taller than
    their share overflow into each other -- the next slide was drawn over the
    notes. The rows must size to content and stack from the top, and the side
    column's grid rule must outrank `.speaker-column { display: flex }`, which
    sits later in the file and wins a single-class tie."""
    css = _read(PRESENTATION_DIR / "speaker.css")
    narrow = css.split("@media (max-width: 820px) {", 1)[1].split("\n}\n", 1)[0]
    main = re.search(r"\.speaker-main \{([^}]*)\}", narrow)
    assert main, "the narrow layout no longer restyles .speaker-main"
    assert "align-content: start" in main.group(1), "rows must stack from the top, not stretch"
    assert "grid-auto-rows: max-content" in main.group(1), "rows must take their content's height"
    assert re.search(r"\.speaker-column\.speaker-side \{", narrow), (
        "the side column's grid rule must outrank the .speaker-column flex rule"
    )
    speaker = _read(PAGES_DIR / "PresentationSpeaker.tsx")
    assert 'className="speaker-column speaker-side"' in speaker
    assert 'className="speaker-timer ' in speaker, "the clock card lost the hook the narrow layout orders it by"


def test_every_locale_has_the_speaker_strings():
    keys = (
        "Speaker view",
        "Notes handout",
        "Speaker view blocked",
        "Rehearsal - no presentation window connected",
        "No speaker notes for this slide.",
        "Only slides with notes",
        "No notes yet",
        "{count, plural, one {# slide} other {# slides}} without notes",
    )
    for locale in ("en", "fr", "es", "ja"):
        catalog = _read(FRONTEND_ROOT / "src" / "i18n" / "lang" / f"{locale}.json")
        for key in keys:
            assert f'"{key}"' in catalog, (locale, key)


def test_editor_panel_shows_words_and_time_per_slide():
    # The deck total heads the Slides tab of the panel; the per-slide count
    # sits over the notes field of the slide inspector (the Selection tab,
    # where the notes are edited).
    panel = _read(PRESENTATION_DIR / "PresentationEditorPanel.tsx")
    assert "deckSpeakingSeconds(draft.slides)" in panel
    inspector = _read(PRESENTATION_DIR / "SlideInspector.tsx")
    assert "countWords(notes)" in inspector
    assert "estimateSpeakingSeconds(notes)" in inspector
    assert 't_i18n("No notes yet")' in inspector

"""The pronunciation lexicon of the voice-over, derived from the seeded readings only.

* the seed: read from its data file, valid against its schema, exactly the
  readings the brand owner confirmed;
* the merge: a video entry overrides the platform's for the same written form;
* the text rewriter: whole words only, punctuation kept, a compound as one
  entry, letters joined so no voice merges them, syllables as given;
* the SSML builder per dialect;
* the aligner on synthetic transcripts: every accepted and rejected hearing
  of every seeded term, the WER made lexicon-aware, the brand check's message;
* fluency: a term said with a pause inside fails whatever its words,
  measured on the audio's own silences; a number heard in digits is its word;
* numbers on both sides: in the written or the ``say`` form, heard in
  digits or in words; an entry that only changes case is never flagged;
* the strict gate: a phonetic reading at most one phoneme off on its
  key (``Phil Igram`` fails), the written form compared case, punctuation and
  spaces aside (``Open CTI``), a term heard otherwise than written a warning
  of the final render; an IPA told to a model taking a delivery instruction,
  the respelling said and reported otherwise; a seeded term that lost its IPA
  given the data file's entry back.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from app.llm.speech import PronunciationSupport
from app.services.video_studio import lexicon as lex
from app.services.video_studio import phonetics, pronunciation_check
from app.services.video_studio.spec import Voiceover

SEED = lex.load_seed()
LEXICON = lex.merge(SEED)


def _entry(text: str) -> lex.Entry:
    return next(e for e in SEED if e.text == text)


# ── The seed ──────────────────────────────────────────────────────────────


def test_the_seed_is_read_from_its_data_file_and_valid_against_its_schema():
    data = json.loads(lex.SEED_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(data, json.loads(lex.SCHEMA_PATH.read_text(encoding="utf-8")))
    readings = {e.text: (e.say, e.ipa, e.match, e.apply_to) for e in SEED}
    assert readings == {
        "Filigran": ("Fil-i-gran", "ˈfɪlɪɡræn", "word", "voice"),
        "CTEM": ("see-tem", "siːtɛm", "word", "voice"),
        "XTM": ("X-T-M", "ˌɛksˌtiːˈɛm", "word", "voice"),
        "XTM One": ("X-T-M One", "ˌɛksˌtiːˈɛm ˈwʌn", "word", "voice"),
        "XTM Hub": ("X-T-M Hub", "ˌɛksˌtiːˈɛm ˈhʌb", "word", "voice"),
        "OpenCTI": ("Open C-T-I", "ˈoʊpən ˌsiːˌtiːˈaɪ", "word", "voice"),
        "OpenAEV": ("Open A-E-V", "ˈoʊpən ˌeɪˌiːˈviː", "word", "voice"),
        "OpenCRQ": ("Open C-R-Q", "ˈoʊpən ˌsiːˌɑːrˈkjuː", "word", "voice"),
        "THREAD": ("thread", "θrɛd", "case_insensitive", "voice"),
    }


def test_every_seeded_entry_is_a_valid_video_pronunciation():
    Voiceover(pronunciations=[e.as_dict() for e in SEED])


def test_a_seed_that_breaks_its_schema_is_refused(tmp_path):
    broken = tmp_path / "lexicon.json"
    broken.write_text(json.dumps({"entries": [{"text": "X", "say": "x", "match": "anywhere"}]}), encoding="utf-8")
    with pytest.raises(jsonschema.ValidationError):
        lex.load_seed(broken)


# ── The merge ─────────────────────────────────────────────────────────────


def test_a_video_entry_overrides_the_platform_entry_for_the_same_written_form():
    merged = lex.merge(SEED, [{"text": "CTEM", "say": "see-tem-program"}, {"text": "SOAR", "say": "soar"}], version=3)
    ctem = [e for e in merged.entries if e.text == "CTEM"]
    assert [e.say for e in ctem] == ["see-tem-program"]
    assert {e.text for e in merged.entries} == {e.text for e in SEED} | {"SOAR"}
    assert merged.version == 3 and merged.entries[0].text == "CTEM"


def test_a_case_insensitive_entry_overrides_whatever_its_case():
    merged = lex.merge(SEED, [{"text": "thread", "say": "the thread", "match": "case_insensitive"}])
    assert [e.say for e in merged.entries if e.text.casefold() == "thread"] == ["the thread"]
    # Case-sensitive on both sides: two written forms, both kept.
    kept = lex.merge(SEED, [{"text": "Ctem", "say": "c tem"}])
    assert {e.text for e in kept.entries if e.text.casefold() == "ctem"} == {"CTEM", "Ctem"}


# ── Finding and rewriting ─────────────────────────────────────────────────


def test_the_rewriter_never_touches_a_word_that_only_contains_a_term():
    text = "XTMs, OpenCTIX and preCTEM stay; CTEM-driven OpenCTI's XTM."
    assert [o.written for o in LEXICON.find(text)] == ["CTEM", "OpenCTI", "XTM"]
    rewritten = LEXICON.rewrite(text)
    assert rewritten.startswith("XTMs, OpenCTIX and preCTEM stay; see-tem-driven ")
    assert rewritten.endswith("OpenCTI's XTM.")


def test_a_compound_is_rewritten_as_one_entry():
    assert LEXICON.rewrite("Meet XTM One, then XTM Hub and XTM.") == "Meet XTM One, then XTM Hub and XTM."
    # Any whitespace between its words, a line break included.
    assert [o.entry.text for o in LEXICON.find("XTM\nOne")] == ["XTM One"]


def test_letters_are_one_token_and_syllables_go_as_given():
    # Never letters with hyphens, spaces or punctuation between them: a voice pauses after each.
    for text in ("XTM", "OpenAEV", "XTM One"):
        sent = lex.rewrite_form(_entry(text))
        assert not set(sent) & set("-\u2010\u2011.\u202f"), sent
    assert lex.rewrite_form(_entry("CTEM")) == "see-tem"
    assert lex.rewrite_form(_entry("Filigran")) == "Fil-i-gran"
    assert lex.rewrite_form(_entry("THREAD")) == "thread"


def test_a_letters_entry_is_sent_one_phonetic_token():
    # The IPA of its letters run together into one word, the main stress on the last.
    assert lex.letters_ipa("XTM") == "ˌɛksˌtiːˈɛm"
    assert _entry("XTM").speech_ipa == "ˌɛksˌtiːˈɛm"
    assert _entry("XTM One").speech_ipa == "ˌɛksˌtiːˈɛm ˈwʌn"
    assert _entry("OpenCTI").speech_ipa == "ˈoʊpən ˌsiːˌtiːˈaɪ"
    # An IPA written letter by letter (an administrator's, an older seed's) is sent as one token all the same.
    assert lex.Entry("XTM", "X-T-M", ipa="ɛks tiː ɛm").speech_ipa == "ˌɛksˌtiːˈɛm"
    assert lex.Entry("OpenCRQ", "Open C-R-Q", ipa="ˈoʊpən siː ɑːr kjuː").speech_ipa == "ˈoʊpən ˌsiːˌɑːrˈkjuː"
    # A letters entry with no IPA gets one from its letters' names and its words' reading; a word entry keeps its own.
    assert lex.Entry("XTM One", "X-T-M One").speech_ipa == "ˌɛksˌtiːˈɛm wʌn"
    assert lex.Entry("AWS Lambda", "A-W-S Lambda").speech_ipa.startswith("ˌeɪˌdʌbəljuːˈɛs ")
    assert _entry("Filigran").speech_ipa == "ˈfɪlɪɡræn" and lex.Entry("SecOps", "Sec Ops").speech_ipa is None
    # Every model that takes an IPA is sent that token: SSML phonemes and a delivery instruction alike.
    ssml = lex.render("Meet XTM One.", LEXICON, PronunciationSupport(ssml="w3c", phoneme=True, sub=True))
    assert '<phoneme alphabet="ipa" ph="ˌɛksˌtiːˈɛm ˈwʌn">XTM One</phoneme>' in ssml.text
    told = lex.render("Meet XTM One.", LEXICON, PronunciationSupport(instruction=True))
    assert told.ipa == (("XTM One", "ˌɛksˌtiːˈɛm ˈwʌn"),) and told.text == "Meet XTM One."


def test_the_retry_glues_the_letters_into_the_words_of_the_written_form():
    # Never a pause between letters: what a voice says fluently, as one word. A syllabic reading stays as
    # given: its parts are what tell the voice how to say it.
    assert {e.text: lex.rewrite_form(e) for e in SEED} == {
        "Filigran": "Fil-i-gran",
        "CTEM": "see-tem",
        "XTM": "XTM",
        "XTM One": "XTM One",
        "XTM Hub": "XTM Hub",
        "OpenCTI": "OpenCTI",
        "OpenAEV": "OpenAEV",
        "OpenCRQ": "OpenCRQ",
        "THREAD": "thread",
    }
    assert lex.rewrite_form(lex.Entry("AWS Lambda", "A-W-S Lambda")) == "AWS Lambda"
    assert lex.rewrite_form(lex.Entry("SecOps", "Sec Ops")) == "Sec Ops"


def test_each_match_mode_finds_what_it_says():
    exact = lex.Lexicon((lex.Entry("XTM", "X-T-M", match="exact"),))
    assert [o.written for o in exact.find("XTM, XTM-based, the XTM's")] == ["XTM"]
    word = lex.Lexicon((lex.Entry("XTM", "X-T-M", match="word"),))
    assert [o.written for o in word.find("XTM, XTM-based, xtm")] == ["XTM", "XTM"]
    # THREAD: any case; it only changes the reading where it is written in capitals.
    assert [o.written for o in LEXICON.find("THREAD 2026, Thread, a thread")] == ["THREAD", "Thread", "thread"]
    assert LEXICON.rewrite("Join THREAD.") == "Join thread."


def test_captions_keep_the_written_form_unless_the_entry_says_otherwise():
    both = lex.merge(SEED, [{"text": "SecOps", "say": "Sec Ops", "apply_to": "both"}, {"text": "K8s", "say": "Kubernetes", "apply_to": "captions"}])
    text = "SecOps on K8s with Filigran"
    assert both.caption_text(text) == "Sec Ops on Kubernetes with Filigran"
    assert both.rewrite(text) == "Sec Ops on K8s with Fil-i-gran"


# ── The SSML builder ──────────────────────────────────────────────────────


def test_the_ssml_builder_writes_inline_phonemes_for_a_model_that_reads_them_in_plain_text():
    support = PronunciationSupport(ssml="inline", phoneme=True, lexicon=True)
    out = lex.render("Filigran & XTM One: CTEM.", LEXICON, support)
    assert out.ssml and out.text == (
        '<phoneme alphabet="ipa" ph="ˈfɪlɪɡræn">Filigran</phoneme> & '
        '<phoneme alphabet="ipa" ph="ˌɛksˌtiːˈɛm ˈwʌn">XTM One</phoneme>: '
        '<phoneme alphabet="ipa" ph="siːtɛm">CTEM</phoneme>.'
    )
    assert dict(out.terms) == {"Filigran": "ssml_phoneme", "XTM One": "ssml_phoneme", "CTEM": "ssml_phoneme"}
    assert out.mechanism == "ssml_phoneme" and not out.rules
    # An entry without IPA, on a dialect without <sub>: the provider's lexicon says it, the written form in the text.
    plain = lex.Lexicon((lex.Entry("SecOps", "Sec Ops"),))
    said = lex.render("SecOps wins.", plain, support)
    assert said.text == "SecOps wins." and said.mechanism == "provider_lexicon" and said.rules


def test_the_ssml_builder_writes_a_w3c_document_with_sub_for_an_entry_without_ipa():
    support = PronunciationSupport(ssml="w3c", phoneme=True, sub=True)
    words = lex.merge(SEED, [{"text": "SecOps", "say": "Sec Ops"}])
    out = lex.render("Filigran & SecOps <3", words, support)
    assert out.text == (
        '<speak><phoneme alphabet="ipa" ph="ˈfɪlɪɡræn">Filigran</phoneme> &amp; '
        '<sub alias="Sec Ops">SecOps</sub> &lt;3</speak>'
    )
    assert out.mechanism == "ssml_phoneme+ssml_sub" and not out.rules


def test_a_model_without_ssml_or_lexicon_gets_the_text_rewrite_and_the_retry_glues_letters():
    plain = lex.render("OpenCTI and CTEM.", LEXICON, PronunciationSupport())
    assert not plain.ssml and plain.mechanism == "text_rewrite"
    assert plain.text == "OpenCTI and see-tem."
    retry = lex.render("OpenCTI and CTEM.", LEXICON, PronunciationSupport(ssml="inline", phoneme=True), level=lex.FALLBACK)
    assert not retry.ssml and retry.text == "OpenCTI and see-tem." and retry.mechanism == "text_rewrite"
    assert lex.render("Nothing to say.", LEXICON, PronunciationSupport()).terms == ()


# ── Phonemes ──────────────────────────────────────────────────────────────


def test_the_ipa_and_the_say_form_read_as_phonemes():
    assert _entry("Filigran").phonemes == ("f", "ɪ", "l", "ɪ", "g", "r", "æ", "n")
    assert _entry("OpenCRQ").phonemes == ("oʊ", "p", "ə", "n", "s", "i", "ɑ", "r", "k", "j", "u")
    assert _entry("XTM One").phonemes == ("ɛ", "k", "s", "t", "i", "ɛ", "m", "w", "ʌ", "n")
    assert phonetics.word_phonemes("seetem") == ("s", "i", "t", "ɛ", "m")


# ── The aligner ───────────────────────────────────────────────────────────

NARRATIONS = {
    "Filigran": "Filigran builds it.",
    "CTEM": "Run CTEM daily.",
    "XTM": "The XTM platform.",
    "OpenCTI": "Try OpenCTI now.",
    "OpenAEV": "Try OpenAEV now.",
    "OpenCRQ": "Try OpenCRQ now.",
    "THREAD": "Join THREAD soon.",
}
HEARD = {
    "Filigran": "{} builds it",
    "CTEM": "run {} daily",
    "XTM": "the {} platform",
    "OpenCTI": "try {} now",
    "OpenAEV": "try {} now",
    "OpenCRQ": "try {} now",
    "THREAD": "join {} soon",
}
ACCEPTED = [
    ("Filigran", "Filigran"),
    ("Filigran", "Filigrand"),
    ("Filigran", "fill a gran"),
    ("Filigran", "Phil a gran"),
    ("CTEM", "see tem"),
    ("CTEM", "C-tem"),
    ("CTEM", "seetem"),
    ("XTM", "X T M"),
    ("XTM", "ex tee em"),
    ("OpenCTI", "Open CTI"),
    ("OpenCTI", "open C T I"),
    ("OpenCTI", "open see tee eye"),
    ("OpenAEV", "open A E V"),
    ("OpenCRQ", "open C R Q"),
    ("OpenCRQ", "open see are queue"),
    ("THREAD", "thread"),
]
REJECTED = [
    ("Filigran", "Philly Grand"),
    ("Filigran", "Filigree"),
    # Video b487288e: the letter name I read inside it, the last consonant off.
    ("Filigran", "Phil Igram"),
    ("CTEM", "stem"),
    ("CTEM", "C T E M"),
    ("CTEM", "kem"),
    ("XTM", "ex tim"),
    ("OpenCTI", "open city"),
    ("OpenAEV", "open aev"),
    ("OpenCRQ", "open circ"),
    ("THREAD", "threat"),
]


@pytest.mark.parametrize(("term", "heard"), ACCEPTED, ids=[f"{t}:{h}" for t, h in ACCEPTED])
def test_an_accepted_reading_passes_the_brand_check_and_costs_no_word(term, heard):
    hearing = pronunciation_check.hear(NARRATIONS[term], HEARD[term].format(heard), LEXICON)
    assert [(t.term, t.passed) for t in hearing.terms] == [(term, True)]
    assert hearing.wer == 0.0


@pytest.mark.parametrize(("term", "heard"), REJECTED, ids=[f"{t}:{h}" for t, h in REJECTED])
def test_a_rejected_reading_fails_the_brand_check(term, heard):
    hearing = pronunciation_check.hear(NARRATIONS[term], HEARD[term].format(heard), LEXICON)
    assert [(t.term, t.passed) for t in hearing.terms] == [(term, False)]
    assert hearing.terms[0].heard.casefold() == heard.casefold()


def test_the_brand_check_names_the_term_what_was_heard_and_when():
    words = [("run", 2.6, 2.9), ("stem", 3.2, 3.6), ("daily", 3.7, 4.1)]
    hearing = pronunciation_check.hear("Run CTEM daily.", words, LEXICON)
    (term,) = hearing.terms
    assert term.message() == "brand term mispronounced: CTEM heard as 'stem' at 0:03.2"
    missing = pronunciation_check.hear("Run CTEM daily.", [("run", 2.6, 2.9), ("daily", 3.7, 4.1)], LEXICON)
    assert missing.terms[0].message() == "brand term mispronounced: CTEM not heard at 0:02.9"


def test_a_mispronounced_term_fails_even_under_the_wer_threshold():
    from app.services.video_studio import voice_check

    text = "Every alert in the queue is triaged and enriched by agents before CTEM reporting starts today."
    check = voice_check.compare(text, text.replace("CTEM", "stem"), lexicon=LEXICON)
    assert check.wer <= voice_check.MAX_WER and not check.passed
    assert "brand term mispronounced: CTEM heard as 'stem'" in check.reason()
    assert voice_check.compare(text, text.replace("CTEM", "see tem"), lexicon=LEXICON).passed


def test_the_other_hearings_the_brand_owner_rejected_are_rejected_too():
    for heard in ("Filigrain", "Filigron"):
        hearing = pronunciation_check.hear("Filigran builds it.", f"{heard} builds it", LEXICON)
        assert not hearing.terms[0].passed, heard
    # A schwa for the second vowel is the same reading.
    assert pronunciation_check.hear("Filigran builds it.", "Filagran builds it", LEXICON).terms[0].passed


def test_a_say_that_does_not_sound_like_its_ipa_is_not_an_accepted_reading():
    wrong = lex.merge(SEED, [{"text": "CTEM", "say": "stem", "ipa": "siːtɛm"}])
    ctem = next(e for e in wrong.entries if e.text == "CTEM")
    assert not ctem.say_agrees and all(e.say_agrees for e in SEED)
    assert not pronunciation_check.hear("Run CTEM daily.", "run stem daily", wrong).terms[0].passed
    assert pronunciation_check.hear("Run CTEM daily.", "run see tem daily", wrong).terms[0].passed


# ── The strict phonetic gate ──────────────────────────────────────


def test_a_reading_is_held_to_its_consonants_and_the_vowels_its_words_start_with():
    filigran = _entry("Filigran")
    assert phonetics.key(filigran.words) == ("f", "l", "g", "r", "n")
    # "Phil Igram": a word broken before a vowel (the letter name I) and the last consonant off - two phonemes.
    igram = phonetics.word_readings("Phil") + phonetics.word_readings("Igram")
    assert phonetics.key(igram) == ("f", "l", phonetics.WORD_VOWEL, "g", "r", "m")
    assert phonetics.key_edits(phonetics.key(filigran.words), phonetics.key(igram)) == 2
    # The weighted distance alone saw one near swap, inside its bound: what let it pass.
    assert phonetics.distance(filigran.phonemes, tuple(p for w in igram for p in w)) <= phonetics.bound(filigran.phonemes)
    assert phonetics.reading_cost(filigran.words, igram) is None
    assert phonetics.reading_cost(filigran.words, phonetics.word_readings("Philigran")) == 0.0


def test_phil_igram_fails_the_brand_check_and_names_what_was_heard():
    from app.services.video_studio import voice_check

    text = (
        "Filigran brings open source threat intelligence and exposure management to every security team, analyst "
        "and leader in the world, today and tomorrow."
    )
    check = voice_check.compare(text, text.replace("Filigran", "Phil Igram"), lexicon=LEXICON)
    assert check.wer <= voice_check.MAX_WER and not check.passed
    assert "brand term mispronounced: Filigran heard as 'Phil Igram'" in check.reason()


@pytest.mark.parametrize(
    ("text", "heard", "reading", "otherwise"),
    [
        ("Filigran builds it.", "Filigran builds it", "written", False),
        ("Filigran builds it.", "filigran builds it", "written", False),
        ("Try OpenCTI now.", "try Open CTI now", "written", False),
        ("Try OpenCTI now.", "try open-CTI now", "written", False),
        ("Try OpenCTI now.", "try open c t i now", "letters", False),
        # The entry's own respelling, as the lexicon says it: nothing to listen to.
        ("Run CTEM daily.", "run see tem daily", "say", False),
        ("Filigran builds it.", "Philigran builds it", "phonetic", True),
        ("Filigran builds it.", "Filigrand builds it", "phonetic", True),
        ("The XTM platform.", "the ex tee em platform", "letters", True),
    ],
    ids=[
        "as-written",
        "lower-case",
        "spaced",
        "hyphenated",
        "letters-lower-case",
        "respelling",
        "philigran",
        "filigrand",
        "letter-names",
    ],
)
def test_a_term_heard_otherwise_than_written_is_flagged_even_when_it_passes(text, heard, reading, otherwise):
    (term,) = pronunciation_check.hear(text, heard, LEXICON).terms
    assert term.passed and term.reading == reading and term.heard_otherwise is otherwise
    # The narration's record and the render's voice summary carry it too.
    assert term.as_dict().get("heard_otherwise", False) is otherwise
    if otherwise:
        assert term.notice().startswith(f"brand term {term.term} heard as '{term.heard}'")


def test_letters_spelled_one_by_one_are_the_written_form_only_of_a_term_read_letter_by_letter():
    # CTEM is read "see-tem": "C T E M" stays rejected, though its letters glued are its written form.
    assert not pronunciation_check.hear("Run CTEM daily.", "run C T E M daily", LEXICON).terms[0].passed
    assert pronunciation_check.hear("The XTM platform.", "the X T M platform", LEXICON).terms[0].reading == "written"


def test_a_say_that_breaks_its_ipa_the_way_phil_igram_does_never_agrees_with_it():
    broken = lex.merge(SEED, [{"text": "Filigran", "say": "Phil Igram", "ipa": "ˈfɪlɪɡræn"}])
    filigran = next(e for e in broken.entries if e.text == "Filigran")
    assert not filigran.say_agrees
    # Said faithfully, it is still heard wrong: the check holds the voice to the IPA.
    assert not pronunciation_check.hear("Filigran builds it.", "Phil Igram builds it", broken).terms[0].passed


def test_the_final_render_warns_on_a_term_heard_otherwise_and_fails_on_one_heard_wrong():
    from app.services.video_studio import audio_qc, render_checks

    narrations = [audio_qc.Narration("s1", "Filigran builds it. Filigran ships it.", 0.0, 4.0)]
    words = [("Philigran", 0.5, 0.9), ("builds", 1.0, 1.2), ("it", 1.2, 1.3)]
    words += [("Philigran", 2.0, 2.4), ("ships", 2.5, 2.7), ("it", 2.7, 2.8)]
    section, findings = audio_qc.pronunciation_findings(pronunciation_check.hear([n.text for n in narrations], words, LEXICON), narrations)
    assert section["status"] == render_checks.PASSED and section["terms"][0]["passed"] == 2
    (warning,) = findings
    assert warning.severity == render_checks.WARN and warning.kind == render_checks.PRONUNCIATION and warning.at == 0.5
    assert warning.message == (
        "brand term Filigran heard as 'Philigran' at 0:00.5, not as written: accepted as a reading that sounds "
        "like its entry - listen to it (2 times)"
    )
    checks = render_checks.Checks(pronunciation=section, findings=findings)
    assert not checks.failed and checks.status == render_checks.FINDINGS
    assert "[warn] 0:00.50 scene `s1` (pronunciation): brand term Filigran heard as 'Philigran'" in render_checks.format_checks(
        checks.as_dict()
    )
    # A warning holds no render back: only a failing audio finding does.
    assert render_checks.open_findings(checks.as_dict(), same_quality=True) == []
    wrong = [("Phil", 0.5, 0.7), ("Igram", 0.7, 0.9), ("builds", 1.0, 1.2), ("it", 1.2, 1.3), ("Filigran", 2.0, 2.4)]
    wrong += [("ships", 2.5, 2.7), ("it", 2.7, 2.8)]
    section, findings = audio_qc.pronunciation_findings(pronunciation_check.hear([n.text for n in narrations], wrong, LEXICON), narrations)
    assert section["status"] == render_checks.FAILED
    assert [(f.severity, f.message) for f in findings] == [
        (render_checks.FAIL, "brand term mispronounced: Filigran heard as 'Phil Igram' at 0:00.5")
    ]


# ── The IPA end to end ────────────────────────────────────────────


def test_a_model_taking_a_delivery_instruction_is_told_each_ipa_and_keeps_the_written_form():
    support = PronunciationSupport(instruction=True)
    out = lex.render("Filigran ships XTM One and CTEM. Filigran again.", LEXICON, support)
    # Every term keeps its written form, its IPA told once each: XTM One as one phonetic token.
    assert out.text == "Filigran ships XTM One and CTEM. Filigran again."
    assert out.ipa == (("Filigran", "ˈfɪlɪɡræn"), ("XTM One", "ˌɛksˌtiːˈɛm ˈwʌn"), ("CTEM", "siːtɛm"))
    assert out.mechanism == "ipa_instruction" and not out.ssml and out.respelled == ()
    assert out.notes("gpt-4o-mini-tts") == []
    # The IPA decides the audio: another one is another take.
    other = lex.merge(SEED, [{"text": "Filigran", "say": "Fil-i-gran", "ipa": "ˈfɪləɡræn"}])
    assert lex.render("Filigran ships.", other, support).key() != lex.render("Filigran ships.", LEXICON, support).key()
    # The retry rewrites every term, as before.
    assert lex.render("Filigran ships.", LEXICON, support, level=lex.FALLBACK).text == "Fil-i-gran ships."


def test_a_model_taking_no_ipa_says_the_respelling_and_the_result_says_so():
    out = lex.render("Filigran ships XTM One.", LEXICON, PronunciationSupport())
    assert out.text == "Fil-i-gran ships XTM One." and out.mechanism == "text_rewrite" and out.ipa == ()
    # What is said instead of each IPA is named as sent: XTM One's letters glued into one word.
    assert out.notes("tts-1-hd") == [
        '"Filigran" has an IPA (/ˈfɪlɪɡræn/) that tts-1-hd cannot take (no SSML phoneme, no pronunciation '
        'instruction): said by its respelling "Fil-i-gran".',
        '"XTM One" has an IPA (/ˌɛksˌtiːˈɛm ˈwʌn/) that tts-1-hd cannot take (no SSML phoneme, no pronunciation '
        'instruction): said by its respelling "XTM One".',
    ]
    # An SSML phoneme sends it: nothing to say either.
    assert lex.render("Filigran ships.", LEXICON, PronunciationSupport(ssml="inline", phoneme=True)).notes("m") == []


# ── The platform lexicon's seeded readings ────────────────────────


def test_a_seeded_term_held_without_its_ipa_gets_the_data_file_s_entry_back():
    from app.services.video_studio import lexicon_store

    filigran = _entry("Filigran")
    assert filigran.ipa == "ˈfɪlɪɡræn" and filigran.apply_to == "voice"
    others = [e for e in SEED if e.text != "Filigran"]
    for stale in (lex.Entry("Filigran", "Filigran"), lex.Entry("Filigran", "Fil-i-gran")):
        given = lexicon_store.seed_readings_lost([stale, *others], SEED)
        assert given == [filigran, *others], stale
    # An entry with an IPA, or a reading an administrator chose, is theirs.
    assert lexicon_store.seed_readings_lost(SEED, SEED) == []
    assert lexicon_store.seed_readings_lost([lex.Entry("Filigran", "Fili-gran"), *others], SEED) == []
    assert lexicon_store.seed_readings_lost([lex.Entry("Filigran", "Filigran", ipa="fɪlɪɡrən")], SEED) == []
    # A term the administrator removed stays removed.
    assert lexicon_store.seed_readings_lost(others, SEED) == []


# ── Numbers heard in digits ───────────────────────────────────────────────


def test_a_number_in_digits_reads_as_its_words():
    assert phonetics.number_words("1") == ["one"]
    assert phonetics.number_words("0") == ["zero"]
    assert phonetics.number_words("21") == ["twenty", "one"]
    assert phonetics.number_words("105") == ["one", "hundred", "five"]
    assert phonetics.number_words("2026") == ["two", "thousand", "twenty", "six"]
    assert phonetics.number_words("3000000") == ["three", "million"]
    assert phonetics.number_words("v2") == ["v2"] and phonetics.number_words("1234567890") == ["1234567890"]


@pytest.mark.parametrize("heard", ["XTM 1", "XTM1", "X T M 1", "XTM one", "XTM-1", "XTM\u00a0One"])
def test_a_term_whose_number_is_heard_in_digits_is_heard_right(heard):
    hearing = pronunciation_check.hear("Meet XTM One today.", f"meet {heard} today", LEXICON)
    assert [(t.term, t.passed) for t in hearing.terms] == [("XTM One", True)] and hearing.wer == 0.0
    assert not pronunciation_check.hear("Meet XTM One today.", "meet XTM 2 today", LEXICON).terms[0].passed
    # Written with a non-breaking space, the narration still writes the term.
    assert pronunciation_check.hear("Meet XTM\u00a0One today.", f"meet {heard} today", LEXICON).terms[0].passed


#: ``(entry, narration, heard)``: a number written in digits on one side, in words on the other.
NUMBERS = [
    ({"text": "85%", "say": "eighty-five percent"}, "We cut triage by 85%.", "we cut triage by 85%"),
    ({"text": "85%", "say": "eighty-five percent"}, "We cut triage by 85%.", "we cut triage by eighty-five percent"),
    ({"text": "85%", "say": "eighty-five percent"}, "We cut triage by 85%.", "we cut triage by 85 percent"),
    ({"text": "eighty-five percent", "say": "eighty-five percent"}, "We cut triage by eighty-five percent.", "we cut triage by 85%"),
    ({"text": "Win11", "say": "Windows 11"}, "Patch Win11 now.", "patch Windows 11 now"),
    ({"text": "Win11", "say": "Windows 11"}, "Patch Win11 now.", "patch windows eleven now"),
    ({"text": "O365", "say": "Office 365"}, "Move to O365 today.", "move to Office 365 today"),
    ({"text": "O365", "say": "Office 365"}, "Move to O365 today.", "move to office three hundred sixty five today"),
]


@pytest.mark.parametrize(("entry", "text", "heard"), NUMBERS, ids=[f"{e['text']}:{h}" for e, _t, h in NUMBERS])
def test_a_number_in_the_written_or_the_say_form_is_heard_in_digits_or_in_words(entry, text, heard):
    hearing = pronunciation_check.hear(text, heard, lex.merge(SEED, [entry]))
    assert [(t.term, t.passed) for t in hearing.terms] == [(entry["text"], True)]
    # The whole reading is the term: none of its words left over to count against the narration.
    assert hearing.wer == 0.0 and hearing.terms[0].reading in ("written", "say")


def test_a_say_form_reads_its_number_as_words():
    win = lex.Entry("Win11", "Windows 11")
    assert win.phonemes == phonetics.words_phonemes(["Windows", "eleven"])
    # The letters of a word still read together across its separators; a number beside them apart.
    assert lex.Entry("Filigran", "Fil-i-gran").phonemes == phonetics.word_phonemes("filigran")
    assert lex.Entry("A2Z", "a2z").phonemes == phonetics.words_phonemes(["a", "two", "z"])
    assert not pronunciation_check.hear("Patch Win11 now.", "patch Windows 12 now", lex.merge(SEED, [win])).terms[0].passed


# ── Fluency ───────────────────────────────────────────────────────────────

#: Each term said in accepted words in "Try <term> now.", timed as a voice says them, with the silences inside it.
BROKEN = [
    ("Filigran", [("Phil", 0.50, 0.80), ("Egran", 1.00, 1.40)], [(0.80, 1.00)], "Phil Egran"),
    ("OpenCTI", [("open", 0.50, 0.80), ("CTI", 0.96, 1.50)], [(0.80, 0.96)], "open CTI"),
    ("OpenAEV", [("open", 0.5, 0.8), ("A", 0.9, 1.0), ("E", 1.39, 1.5), ("V", 1.6, 1.7)], [(1.0, 1.39)], "open A E V"),
    ("XTM", [("X", 0.50, 0.70), ("T", 0.99, 1.10), ("M", 1.39, 1.50)], [(0.70, 0.99), (1.10, 1.39)], "X T M"),
    ("CTEM", [("see", 0.50, 0.70), ("tem", 0.85, 1.10)], [(0.70, 0.85)], "see tem"),
]


def _timed(term: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    """The transcript of "Try <term> now.": *term*'s words, a word of 0.3 s before and after them."""
    return [("try", 0.1, 0.4), *term, ("now", term[-1][2] + 0.05, term[-1][2] + 0.35)]


@pytest.mark.parametrize(("written", "term", "pauses", "heard"), BROKEN, ids=[b[3] for b in BROKEN])
def test_a_term_said_with_a_pause_inside_fails_whatever_its_words(written, term, pauses, heard):
    text, words = f"Try {written} now.", _timed(term)
    # The same words said fluently pass: the reading is accepted, only the pause breaks it.
    assert all(t.passed for t in pronunciation_check.hear(text, words, LEXICON, pauses=[]).terms)
    (said,) = pronunciation_check.hear(text, words, LEXICON, pauses=pauses).terms
    longest = round(max(end - start for start, end in pauses), 3)
    assert not said.passed and said.pause == longest and said.heard == heard and said.term == written
    assert said.message() == (
        f"brand term mispronounced: {said.term} spelled with pauses, a {longest:.2f} s silence inside ('{heard}') at 0:00.5"
    )
    assert said.as_dict()["pause"] == round(longest, 3)


def test_a_short_silence_inside_a_term_or_a_pause_around_it_never_breaks_it():
    words = _timed([("OpenAEV", 0.50, 1.40)])
    # 0.12-0.13 s inside: what every fluent take held. Around it: the silence before and after a word.
    for pauses in ([(0.80, 0.92)], [(0.80, 0.93)], [(0.38, 0.52)], [(1.38, 1.47)], [(0.40, 0.50), (1.40, 1.45)]):
        assert pronunciation_check.hear("Try OpenAEV now.", words, LEXICON, pauses=pauses).terms[0].passed, pauses
    # From the threshold up, it is broken; without the audio's pauses, fluency is not checked.
    assert not pronunciation_check.hear("Try OpenAEV now.", words, LEXICON, pauses=[(0.80, 0.95)]).terms[0].passed
    assert pronunciation_check.hear("Try OpenAEV now.", words, LEXICON).terms[0].passed


def test_a_broken_term_fails_the_segment_check_under_the_wer_threshold():
    from app.services.video_studio import voice_check

    text = "Filigran builds open source threat management tools for every security team today."
    words = [("Phil", 0.0, 0.3), ("Egran", 0.5, 0.9)] + [(w, 1.0 + 0.4 * i, 1.3 + 0.4 * i) for i, w in enumerate(text.split()[1:])]
    transcript = " ".join(w for w, _s, _e in words)
    check = voice_check.compare(text, transcript, lexicon=LEXICON, words=words, pauses=[(0.3, 0.5)])
    assert check.wer == 0.0 and not check.passed
    assert "brand term mispronounced: Filigran spelled with pauses, a 0.20 s silence inside ('Phil Egran')" in check.reason()
    assert voice_check.compare(text, transcript, lexicon=LEXICON, words=words, pauses=[]).passed


def test_the_pauses_are_read_from_the_take_itself(tmp_path):
    import wave

    import numpy as np

    from app.services.video_studio import voice_check

    rate = 16000

    def voiced(seconds: float) -> np.ndarray:
        t = np.arange(round(seconds * rate)) / rate
        return 0.4 * np.sin(2 * np.pi * 180 * t) * (1 + 0.5 * np.sin(2 * np.pi * 7 * t))

    def silent(seconds: float) -> np.ndarray:
        return np.zeros(round(seconds * rate))

    # "Phil" 0.3 s, a 0.25 s pause, "Egran" 0.4 s, a 0.08 s closure, "builds it" 0.5 s.
    take = np.concatenate([silent(0.2), voiced(0.3), silent(0.25), voiced(0.4), silent(0.08), voiced(0.5), silent(0.2)])
    path = tmp_path / "take.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes((take * 32767).astype("<i2").tobytes())
    pauses = voice_check.pauses_of_wav(str(path))
    assert len(pauses) == 1
    (start, end) = pauses[0]
    assert start == pytest.approx(0.5, abs=0.02) and end == pytest.approx(0.75, abs=0.02)
    words = [("Phil", 0.2, 0.5), ("Egran", 0.75, 1.15), ("builds", 1.23, 1.5), ("it", 1.5, 1.73)]
    (said,) = pronunciation_check.hear("Filigran builds it.", words, LEXICON, pauses=pauses).terms
    assert not said.passed and said.pause == pytest.approx(0.25, abs=0.02)


# ── Authoring ─────────────────────────────────────────────────────────────


def test_the_save_time_check_warns_about_what_the_voice_may_not_say_as_the_lexicon_does():
    warnings = lex.pronunciation_warnings("Opencti ships Ctem with SOAR and SecOps; spell C-T-E-M, AI and XTM One.", LEXICON)
    # Each names the narration: the title or the lines on screen may write the term as its entry does.
    assert warnings == [
        'its narration writes "Ctem" where the pronunciation lexicon has "CTEM": the voice is only told how to say '
        '"CTEM" - write "CTEM" in the narration',
        'its narration spells "C-T-E-M" letter by letter, but the pronunciation lexicon reads CTEM as "see-tem": write '
        '"CTEM" in the narration',
        'its narration writes "Opencti" where the pronunciation lexicon has "OpenCTI": the voice is only told how to say '
        '"OpenCTI" - write "OpenCTI" in the narration',
        "its narration says \"SOAR\", \"SecOps\", which no pronunciation lexicon entry (the platform's or the video's) "
        "covers: the voice guesses how to say it - add a reading to voiceover.pronunciations (preview_voiceover tells "
        "how a voice says it), or ask an administrator to add it to the platform lexicon",
    ]
    # A term written as its entry writes it, and a case-insensitive one in any case: nothing to say.
    assert lex.pronunciation_warnings("Filigran, OpenCTI and CTEM at THREAD, on a thread.", LEXICON) == []
    # A video entry covers it as well as the platform's.
    own = lex.merge(SEED, [{"text": "SOAR", "say": "soar"}, {"text": "SecOps", "say": "sec ops"}])
    assert lex.pronunciation_warnings("SOAR and SecOps.", own) == []


def test_an_entry_that_only_changes_case_is_never_flagged_written_in_another_case():
    # Read "extended": the voice says "extended" or "EXTENDED" as the entry does.
    cased = lex.merge(SEED, [{"text": "eXtended", "say": "extended"}])
    assert lex.pronunciation_warnings("eXtended Threat Management: the extended platform, EXTENDED.", cased) == []
    # A reading that is more than a case - syllables, letters, another word, an IPA of its own - still is.
    for entry in (
        {"text": "eXtended", "say": "ex-tended"},
        {"text": "SecOps", "say": "sec ops"},
        {"text": "Gif", "say": "gif", "ipa": "dʒɪf"},
    ):
        written = entry["text"].swapcase()
        (warning,) = lex.pronunciation_warnings(f"The {written} platform.", lex.merge(SEED, [entry]))
        assert warning.startswith(f'its narration writes "{written}" where the pronunciation lexicon has "{entry["text"]}"')


def test_the_quality_check_reports_the_warnings_on_their_scene():
    from app.services.video_studio import timeline
    from app.services.video_studio.quality import ContentFacts, content_findings
    from app.services.video_studio.spec import authored_spec, parse_video_spec, validate_video_spec

    raw = {
        "title": "Readings",
        "freestyle": True,
        "scenes": [
            {"id": "right", "type": "title", "title": "One", "narration": "Filigran runs CTEM."},
            {"id": "cased", "type": "title", "title": "Two", "narration": "Opencti runs Ctem."},
        ],
    }
    spec = parse_video_spec(validate_video_spec(raw))
    document = timeline.estimate_plan(spec, {}, {}).document
    found = content_findings(spec, document, ContentFacts(lexicon=LEXICON), authored_spec(raw))
    said = [(f.scene_id, f.kind, f.severity) for f in found]
    assert said == [("cased", "pronunciation", "advisory"), ("cased", "pronunciation", "advisory")]
    # Without a voice, nothing is said, so nothing to warn about.
    quiet = parse_video_spec(validate_video_spec({**raw, "voiceover": {"enabled": False}}))
    assert not [f for f in content_findings(quiet, document, ContentFacts(lexicon=LEXICON), None) if f.kind == "pronunciation"]


def test_every_seeded_term_in_one_narration_heard_right():
    text = "Filigran ships XTM One and XTM Hub: OpenCTI, OpenAEV and OpenCRQ for CTEM, then THREAD."
    heard = "filigrand ships X T M one and XTM hub open C T I open A E V and open see are queue for see tem then thread"
    hearing = pronunciation_check.hear(text, heard, LEXICON)
    assert all(t.passed for t in hearing.terms) and hearing.wer == 0.0
    assert [t.term for t in hearing.terms] == ["Filigran", "XTM One", "XTM Hub", "OpenCTI", "OpenAEV", "OpenCRQ", "CTEM", "THREAD"]

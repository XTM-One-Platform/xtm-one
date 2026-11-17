"""HITL consent contract: which human replies approve an irreversible action.

``hitl_history_approves`` is the authorisation boundary behind every escalated
irreversible action (today the OpenCTI entity merge): a reply recorded in
``AssignmentRun.human_input_history`` either approves the one action code it
names or approves nothing, and anything ambiguous approves nothing so the
human is asked again. Every row of ``_REFUSED`` below was a false approval
before this contract (or would be one without it).
"""

import re
import sys
import tracemalloc
import unicodedata

import pytest

from app.services import hitl_consent
from app.services.hitl_consent import hitl_reply_approves
from app.services.integration_tools import hitl_history_approves

A = "M-AAAA0000"
B = "M-BBBB1111"


def _answered(*responses):
    return [{"resolution": "answered", "response": r, "question": "q"} for r in responses]


def _approves(reply, code=A):
    return hitl_history_approves(_answered(reply), code)


def _baseline_approves(reply, code):
    """The earlier parser: the keyword first, the code anywhere."""
    return re.match(r"^\W*APPROVED?\b", reply, re.IGNORECASE) is not None and code.upper() in reply.upper()


_ACCEPTED = [
    f"APPROVE {A}",
    f"APPROVE {A} - go ahead",
    f"Approved {A}",
    f"approve {A.lower()}",
    f"APPROVE: {A}",
    f"APPROVE, {A}",
    f"APPROVE ({A})",
    f"APPROVE [{A}]",
    f"APPROVE - {A}",
    f"APPROVE {A}.",
    f"APPROVE {A}!",
    f"APPROVE {A}, thanks",
    f"APPROVE {A} - ticket 2026-09 closed",
    f"`APPROVE {A}`",
    f'"APPROVE {A}"',
    f"**APPROVE {A}**",
    f"*APPROVE* {A}",
    f"APPROVE **{A}** - go ahead",
    f"APPROVE `{A}` - go ahead",
    f"  \n\n APPROVE {A}  ",
    f"APPROVE\n{A}",
    f"APPROVE {A}\n\nThanks,\nSam",
    f"APPROVE\t{A}\r\n\r\nThanks",
    f"APPROVE\u00a0{A}\u00a0- go ahead",
    f"\ufeffAPPROVE {A}",
    "APPROVE M-AAAA\u200b0000",
    f"APPROVE {A} \U0001f44d",
    f"APPROVE {A} \U0001f44d\U0001f3fd",
    f"APPROVE {A} \u2705",
    f"APPROVE {A} \u2714\ufe0f",
    f"APPROVE {A} \u2014 \u00ab go ahead \u00bb",
    f"APPROVE {A} - vas-y, merci \u00e0 toi",
    f"APPROVE {A} - \u0920\u0940\u0915 \u0939\u0948",
    f"APPROVE {A} \u304a\u9858\u3044\u3057\u307e\u3059\u3002",
    f"APPROVE {A}, it saves \u20ac2k a year",
    f"APPROVE {A}\n\nSam | +33 6 12 34 56 78 | <sam@example.com>",
    # Markdown around it: a list item, a heading, parentheses or brackets.
    f"- APPROVE {A}",
    f"+ APPROVE {A}",
    f"\u2022 APPROVE {A}",
    f"# APPROVE {A}",
    f"### APPROVE {A}",
    f"  - **APPROVE {A}**",
    f"*APPROVE {A}*",
    f"(APPROVE {A})",
    f"[APPROVE {A}]",
    f"\u00a1APPROVE {A}!",
    # A symbol that agrees, before or after it; a sign-off or a signature.
    f"\u2705 APPROVE {A}",
    f"\U0001f44d APPROVE {A}",
    f"APPROVE {A}\U0001f44d",
    f"APPROVE {A} \U0001f389",
    f"APPROVE {A} - thanks \U0001f60a",
    f"APPROVE {A}\n\nThanks! \U0001f642\nSam",
    f"APPROVE {A}\n\n--\nSam | Filigran\u2122 | \U0001f4de +33 6 12 34 56 78",
    # The fullwidth and CJK punctuation an input method types, or a CJK word,
    # right after the code.
    f"APPROVE {A}\u3002",
    f"APPROVE {A}\u3002\u3088\u308d\u3057\u304f\u304a\u9858\u3044\u3057\u307e\u3059\u3002",
    f"APPROVE {A}\u3001\u78ba\u8a8d\u6e08\u307f\u3067\u3059",
    f"APPROVE {A}\uff01",
    f"APPROVE {A}\uff0c\u8c22\u8c22",
    f"APPROVE {A}\uff08\u78ba\u8a8d\u6e08\u307f\uff09",
    f"APPROVE {A}(checked)",
    f"APPROVE {A}\u3092\u304a\u9858\u3044\u3057\u307e\u3059",
    f"APPROVE {A}\ud655\uc778",
    f"APPROVE {A}\U00020b9f\u308b",
    f"APPROVE\uff1a{A}",
    f"APPROVE\uff08{A}\uff09",
    f"\uff08APPROVE {A}\uff09",
    f"\u3010APPROVE {A}\u3011",
    f"\u30fbAPPROVE {A}",
    f"\u300cAPPROVE {A}\u300d\u3002",
]

# Approved on the baseline, then voided, and approving again:
# a question anywhere in the reply, and a hex token that is not an action code.
_QUESTIONS = [
    f"APPROVE {A} - can you post the result when it is done?",
    f"APPROVE {A}\nOr should we wait?",
    f"APPROVE {A}, right?",
    f"APPROVE {A}?",
    f"APPROVE {A}? no",
    f"APPROVE? {A}",
    f"\u00bfAPPROVE {A}?",
    f"APPROVE {A} ?",
    f"APPROVE {A}\uff1f",
    f"APPROVE {A}\u3002\u7d50\u679c\u3092\u6559\u3048\u3066\u304f\u308c\u307e\u3059\u304b\uff1f",
    f'"APPROVE {A}"?',
    f"APPROVE {A} \u2753",
    f"APPROVE {A} \u00bfok",
]
_HEX_TOKENS = [
    f"APPROVE {A} - deployed on 20260927",
    f"APPROVE {A} - ticket 12345678",
    f"APPROVE {A} - see JIRA-12345678",
    f"APPROVE {A} (fixed in a1b2c3d4)",
    f"APPROVE {A} - request 3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b",
    f"APPROVE {A} - it failed with 0x80070005 last time",
    f"APPROVE {A} #12345678",
    # Without its prefix another code is one of these tokens, as on the baseline.
    f"APPROVE {A} - sorry, I meant BBBB1111",
    f"APPROVE {A} - no, I meant -BBBB1111",
    f"APPROVE {A} - no, I meant MM-BBBB1111",
    f"APPROVE {A} - no, I meant BBBB1111\ufe0fx",
]
# Several approvals in one reply, each with its own keyword, approve each code.
_TWO_APPROVALS = [
    f"APPROVE {A}\nAPPROVE {B}",
    f"APPROVE {A}, APPROVE {B}",
    f"APPROVE {A} APPROVE {B}",
    f"APPROVE {A} and APPROVE {B} - thanks",
    f"APPROVE {A}; APPROVE {B}.",
    f"APPROVE {A} \u2014 APPROVE {B}",
    f"- APPROVE {A}\n- APPROVE {B}",
    f"**APPROVE {A}**\n**APPROVE {B}**",
    f"`APPROVE {A}` `APPROVE {B}`",
    f'"APPROVE {A}", "APPROVE {B}"',
    f'APPROVE {A}, "APPROVE {B}"',
    f'APPROVE {A}"APPROVE {B}"',
    f'"APPROVE {A}""APPROVE {B}"',
    f"APPROVE {A}\u300cAPPROVE {B}\u300d",
    f"(APPROVE {A}) (APPROVE {B})",
    f"APPROVE {A}\u3002APPROVE {B}\u3002",
    f"APPROVE {A}? APPROVE {B}?",
    f"APPROVE {B}\nAPPROVE {A}",
    f"APPROVE {A}, APPROVE {B} - ticket 12345678",
]


_REFUSED = [
    # A question widens nothing else: these are refused for what they say.
    f"~~APPROVE {A}~~ can you check?",
    f"APPROVE {A} \u274c can you check?",
    f"> APPROVE {A} to confirm?",
    f'"APPROVE {A}" - really?',
    # Quoted history: the agent's own instruction quoted back is not consent.
    f"> APPROVE {A} to confirm the merge\n\nNo.",
    f">> APPROVE {A}",
    f"On Mon, the agent wrote:\n> APPROVE {A}",
    # Quoted history as a channel relay would pass it on: the quoted prompt names the code.
    f"APPROVE {A}\n\nOn Mon, XTM One wrote:\n> Your reply must START with `APPROVE {A}` to confirm",
    f"&gt; APPROVE {A}\nno",
    f"<blockquote>APPROVE {A}</blockquote>no",
    f"---------- Forwarded message ----------\nAPPROVE {A}",
    # Struck through.
    f"~~APPROVE {A}~~",
    f"APPROVE ~~{A}~~",
    f"~APPROVE {A}~",
    # A quoted phrase is a mention of the approval, not a use of it.
    f'"APPROVE {A}" - absolutely not',
    f"`APPROVE {A}` - not happening",
    f"\u201cAPPROVE {A}\u201d\nno way",
    # A code the reply names is one of its approvals, or the reply approves nothing.
    f"APPROVE {A} and {B}",
    f"APPROVE {A},{B}",
    f"APPROVE {A} - but definitely NOT {B}",
    f"APPROVE {A} - no, sorry, I meant {B}",
    f"APPROVE {A}\nsorry, I meant {B}",
    f"APPROVE {A} - no, not {A}",
    f"APPROVE {A} or m\u2011bbbb1111",
    f"APPROVE {A}\nno, not aaaa0000",
    f"APPROVE {A} - no, I meant MBBBB1111",
    f"APPROVE {A} - no, I meant M-BBBB\ufe0f1111",
    f"APPROVE {A} - no, I meant M-BBBB\u034f1111",
    f"APPROVE {A} or M-BBBB\u31641111",
    f"APPROVE {A}, APPROVE M-BBBB\u034f1111",
    f"REJECT {B}, APPROVE {A}",
    "APPROVE ALL",
    # Only approvals back to back: a word between two can retract the first, an
    # arrow can mean "instead of", and a negated or quoted keyword approves nothing.
    f"APPROVE {A}\nno wait, wrong one:\nAPPROVE {B}",
    f"APPROVE {A}\nDo NOT APPROVE {B}",
    f"APPROVE {A} and not APPROVE {B}",
    f"APPROVE {A} or APPROVE {B}",
    f"APPROVE {A} / APPROVE {B}",
    f"APPROVE {A} \u2192 APPROVE {B}",
    f"APPROVE {A}, DISAPPROVE {B}",
    f"APPROVE {A} !APPROVE {B}",
    f"APPROVE {A} -APPROVE {B}",
    f"APPROVE {A}\n> APPROVE {B}",
    f'APPROVE {A}, "APPROVE {B}" - absolutely not',
    f'"APPROVE {B}" APPROVE {A}',
    f'"APPROVE {A}" "APPROVE {B}" - go ahead',
    f"APPROVE {A}\u3001\u300cAPPROVE {B}\u300d\u306f\u30c0\u30e1",
    # A quote mark glued to the code before it still opens the next approval.
    f'APPROVE {A}"APPROVE {B}" - absolutely not',
    f"APPROVE {A}\u300cAPPROVE {B}\u300d\u306f\u30c0\u30e1",
    f"(APPROVE {A})`APPROVE {B}` - no",
    f'"APPROVE {A}""APPROVE {B}" - go ahead',
    f'APPROVE "{A}""APPROVE {B}" - no',
    f'APPROVE {A}"APPROVE {B}", APPROVE M-CCCC2222',
    # Negation before or around the keyword or the code.
    f"do NOT APPROVE {A}",
    f"don't approve {A}",
    f"I would APPROVE {A} if the aliases match",
    f"!APPROVE {A}",
    f"-APPROVE {A}",
    f"\u274c APPROVE {A}",
    f"APPROVE {A} \u274c",
    f"APPROVE -{A}",
    f"APPROVE !{A}",
    f"@xtm-one APPROVE {A}",
    # Markup never widens what approves, and HTML is not parsed at all.
    f"<p>APPROVE {A}</p>",
    f"APPROVE <strong>{A}</strong>",
    f"APPROVE&nbsp;{A}",
    f"APPROVE {A}</p>",
    f"_APPROVE {A}_",
    f"-- APPROVE {A}",
    # An unticked checkbox, or a face that hesitates.
    f"[ ] APPROVE {A}",
    f"- [ ] APPROVE {A}",
    f"\U0001f914 APPROVE {A}",
    f"APPROVE {A} \U0001f914",
    # A symbol not known to agree: a list of refusal symbols is never complete.
    f"APPROVE {A} \u2612",
    f"APPROVE {A} \u2613",
    f"APPROVE {A} \u22a0",
    f"APPROVE {A} \U0001f5d9",
    f"APPROVE {A} \U0001f6d1",
    f"APPROVE {A} \U0001f645",
    f"APPROVE {A} \u00ac",
    f"APPROVE {A} \u2260",
    f"APPROVE {A} \u2639",
    f"APPROVE {A} \u2610",
    f"APPROVE {A} \U0001f44e\U0001f3fd",
    f"APPROVE {A} \U0001f644",
    # Struck or crossed out by a combining mark.
    f"APPROVE {A} \u2714\u20e0",
    f"APPROVE {A} \u2714\u0338",
    f"APPROVE {A} - g\u0336o\u0336 a\u0336h\u0336e\u0336a\u0336d\u0336",
    # Not text a human types.
    f"APPROVE {A} \ue000",
    f"APPROVE {A} \u0378",
    f"APPROVE {A} \ud800",
    f"APPROVE {A} \x08\x08\x08",
    f"APPROVE {A} - no, I meant M-BBBB\x1c1111",
    f"APPROVE\x1c{A}\x85",
    # Invisible, display-changing or look-alike characters.
    f"APPROVE {A}\u200bB",
    f"\u200fAPPROVE {A}",
    "APPROVE M-\u202e0000AAAA",
    f"APPROVE {A}\u0336",
    f"APPROVE {A}\ufe0f",
    f"APPROVE {A}-B",
    f"APPROVE {A}0",
    f"APPROVE {A}\u00e9",
    f"APPROVE {A}_x",
    "APPROVE M-\u0410AAA0000",
    "APPROVE \uff2d-AAAA0000",
    f"\uff21\uff30\uff30\uff32\uff2f\uff36\uff25 {A}",
    # The CJK forms of the punctuation widen nothing else.
    f"APPROVE {A}\u3002\u3044\u3084\u3001{B}",
    f"APPROVE {A}\u3001{B}",
    f"\u300cAPPROVE {A}\u300d\u306f\u30c0\u30e1",
    f"\u300cAPPROVE {A}\u300d\u3067\u3044\u3044\uff1f",
    f"APPROVE {A}\uff10",
    f"APPROVE {A}\uff22",
    f"APPROVE {A}\U0002fffe",
]


@pytest.mark.parametrize("reply", _ACCEPTED, ids=lambda r: r.encode("unicode_escape").decode())
def test_a_well_formed_approval_still_approves(reply):
    assert _approves(reply)


@pytest.mark.parametrize("reply", _REFUSED, ids=lambda r: r.encode("unicode_escape").decode())
def test_an_ambiguous_reply_approves_nothing(reply):
    assert not _approves(reply, A)
    assert not _approves(reply, B)


@pytest.mark.parametrize("reply", _QUESTIONS + _HEX_TOKENS, ids=lambda r: r.encode("unicode_escape").decode())
def test_a_question_or_a_hex_token_approves_as_on_the_baseline(reply):
    assert _baseline_approves(reply, A)
    assert _approves(reply, A)
    assert not _approves(reply, B)


@pytest.mark.parametrize("reply", _TWO_APPROVALS, ids=lambda r: r.encode("unicode_escape").decode())
def test_approvals_back_to_back_approve_each_code_as_on_the_baseline(reply):
    assert _baseline_approves(reply, A) and _baseline_approves(reply, B)
    assert _approves(reply, A)
    assert _approves(reply, B)
    assert not _approves(reply, "M-CCCC2222")


def test_the_same_approval_twice_approves():
    assert _baseline_approves(f"APPROVE {A}\nAPPROVE {A}", A)
    assert _approves(f"APPROVE {A}\nAPPROVE {A}")
    assert _approves(f"APPROVE {A}, APPROVE {A}, APPROVE {B}", B)


def test_words_between_two_approvals_void_both():
    """Prose between two approvals is not read: it may add to the first or retract it."""
    for between in ("Thanks. Also:", "no wait, wrong one:"):
        reply = f"APPROVE {A}\n{between}\nAPPROVE {B}"
        assert _baseline_approves(reply, A) and _baseline_approves(reply, B)
        assert not _approves(reply, A)
        assert not _approves(reply, B)


def test_an_approval_never_carries_over_to_another_code():
    assert not _approves(f"APPROVE {A}", B)
    assert not _approves(f"APPROVE {A} - go ahead", B)


def test_the_human_s_latest_word_on_a_code_wins():
    retracted = _answered(f"APPROVE {A}", f"Wait - do not merge {A.lower()}, it is the wrong entity.")
    assert not hitl_history_approves(retracted, A)
    assert not hitl_history_approves(_answered(f"APPROVE {A}", "no, leave AAAA0000 alone"), A)
    assert not hitl_history_approves(_answered(f"APPROVE {A}", "do not merge -AAAA0000"), A)
    assert not hitl_history_approves(_answered(f"APPROVE {A}", "do not merge MAAAA0000"), A)
    # An invisible mark or control inside or after the code does not hide the retraction.
    for later in ("M-AAAA\u034f0000", "M-AAAA\ufe0f0000", "M-AAAA\x1c0000", "M-AAAA\x850000", "AAAA0000\ufe0fx"):
        assert not hitl_history_approves(_answered(f"APPROVE {A}", f"do not merge {later}"), A)
    re_approved = _answered(f"APPROVE {A}", f"not {A}", f"APPROVE {A}")
    assert hitl_history_approves(re_approved, A)
    # A reply about another code, or about nothing, does not withdraw it.
    assert hitl_history_approves(_answered(f"APPROVE {A}", f"APPROVE {B}"), A)
    assert hitl_history_approves(_answered(f"APPROVE {A}", "thanks, carry on"), A)
    assert hitl_history_approves(_answered(f"APPROVE {A}", "see ticket 12345678"), A)
    # Withdrawing one of two approvals given in one reply leaves the other.
    both_then_one = _answered(f"APPROVE {A}\nAPPROVE {B}", f"do not merge {B}")
    assert hitl_history_approves(both_then_one, A)
    assert not hitl_history_approves(both_then_one, B)


def test_a_reply_that_is_not_text_approves_nothing():
    assert not hitl_reply_approves(None, A)
    assert not hitl_reply_approves(f"APPROVE {A}", "  ")
    for response in ({"text": f"APPROVE {A}"}, [f"APPROVE {A}"], (f"APPROVE {A}",), [[f"APPROVE {A}"]]):
        assert not hitl_history_approves([{"resolution": "answered", "response": response}], A)


def test_an_approval_recorded_before_this_contract_still_approves():
    entry = {
        "question": f"Merge 'France' into 'France'? Your reply must START with `APPROVE {A}` to confirm.",
        "response": f"APPROVE {A}",
        "resolution": "answered",
        "responded_at": "2026-09-20T10:00:00+00:00",
        "responded_by": "Sam",
        "responded_by_id": "00000000-0000-0000-0000-000000000001",
    }
    assert hitl_history_approves([entry], A)


def test_the_unicode_tables_match_the_unicode_database():
    """A Unicode upgrade adding an invisible character, a question mark or an overlay fails here."""
    invisible_names = ("VARIATION SELECTOR", "GRAPHEME JOINER", "INHERENT", "FILLER")
    format_chars, asks, overlays, invisible = set(), {"?"}, set(), set()
    for ch in map(chr, range(sys.maxunicode + 1)):
        category, name = unicodedata.category(ch), unicodedata.name(ch, "")
        if category == "Cf":
            format_chars.add(ch)
        elif not ch.isascii() and ("QUESTION" in name or "INTERROBANG" in name):
            asks.add(ch)
        elif category in ("Mc", "Mn") and "OVERLAY" in name:
            overlays.add(ch)
        elif category in ("Lo", "Mn") and any(part in name for part in invisible_names):
            invisible.add(ch)
    controls = {ch for ch in map(chr, range(0xA0)) if unicodedata.category(ch) == "Cc"} - set("\t\n\r")
    everything = "".join(map(chr, range(sys.maxunicode + 1)))
    assert set(hitl_consent._CONTROLS_RE.findall(everything)) == controls
    assert set(hitl_consent._FORMAT_RE.findall(everything)) == format_chars
    assert set(hitl_consent._ASKS_RE.findall(everything)) == asks
    assert set(hitl_consent._OVERLAY_RE.findall(everything)) == overlays
    assert set(hitl_consent._INVISIBLE_RE.findall(everything)) == invisible


def test_a_symbol_approves_only_when_it_cannot_say_no():
    """A list of refusal symbols is never complete (it missed U+2612), so every symbol refuses by default.

    A question mark drawn as a symbol asks, which does not void an approval.
    """
    allowed = set(
        "\u2611\u2705\u2713\u2714\U0001f197\U0001f44c\U0001f44d\U0001f5f8\U0001f5f9"
        "\u2728\u2764\u2b50\U0001f31f\U0001f389\U0001f44b\U0001f44f\U0001f499\U0001f49a\U0001f4aa"
        "\U0001f600\U0001f601\U0001f603\U0001f604\U0001f60a\U0001f642\U0001f64f\U0001f680\U0001f91d"
        "\u00a9\u00ae\u00b0\u00b4\u2120\u2122\u2192\u21d2\u260e\u2706\u2709\u27a1"
        "\U0001f310\U0001f3e2\U0001f4bc\U0001f4c5\U0001f4cd\U0001f4de\U0001f4e7\U0001f4f1\U0001f517\U0001f5d3"
    )
    allowed |= set(map(chr, range(0x1F3FB, 0x1F400)))
    for ch in map(chr, range(sys.maxunicode + 1)):
        category = unicodedata.category(ch)
        if category[0] == "S" or category == "Me":
            asks = any(word in unicodedata.name(ch, "") for word in ("QUESTION", "INTERROBANG"))
            expected = ch in allowed or asks or category == "Sc" or (ch.isascii() and ch != "~")
            assert _approves(f"APPROVE {A} {ch}") is expected, f"U+{ord(ch):04X} {unicodedata.name(ch, '')}"


def test_any_hangul_letter_may_follow_the_code():
    """A syllable or a jamo cannot extend an ASCII code, as on the baseline; an invisible filler still refuses."""
    for ch in map(chr, range(sys.maxunicode + 1)):
        name = unicodedata.name(ch, "")
        if unicodedata.category(ch) == "Lo" and "HANGUL" in name:
            assert _approves(f"APPROVE {A}{ch}") is ("FILLER" not in name), f"U+{ord(ch):04X} {name}"


def test_a_long_reply_is_parsed_in_linear_time_and_memory():
    """The respond route accepts a reply of any length, and it is parsed on the worker's event loop."""
    replies = (
        "APPROVE" + " " * 1_000_000 + "X",
        "APPROVE" + " -" * 500_000 + "X",
        f"APPROVE {A} " + "\u2014" * 1_000_000,
    )
    tracemalloc.start()
    try:
        decisions = [hitl_reply_approves(reply, A) for reply in replies]
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert decisions == [False, False, True]
    # A copy of the reply at most, never an engine frame per character.
    assert peak < 16_000_000


def test_the_contract_holds_for_any_code_shape():
    assert _approves("APPROVE AB12", "AB12")
    assert not _approves("APPROVE AB123", "AB12")
    assert not _approves("APPROVE AB12, AB12", "AB12")
    assert not _approves("APPROVE A B12", "AB12")
    assert _approves("APPROVE AB12, APPROVE AB12?", "AB12")
    assert _approves("APPROVE X-1234 - see 5678", "X-1234")
    assert not _approves("APPROVE X-1234 - see X-5678", "X-1234")

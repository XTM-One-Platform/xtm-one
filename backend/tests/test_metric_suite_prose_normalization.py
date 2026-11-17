"""PROSE normalization and tokenization: each policy option, the fixed stage order,
tokenization, raw markup, locale independence, the policy reaching every input site, and the
prose invariants over the complete policy grid.
"""

import ast
import itertools
import json
import random
import re
import time
import unicodedata
from collections.abc import Callable, Sequence

import pytest

from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality import (
    CaseMode,
    ProseNormalizationPolicy,
    PunctuationMode,
    UnicodeForm,
    WhitespaceMode,
    prose_exact_match,
    prose_forbidden_phrase_absence,
    prose_required_phrase_coverage,
    prose_rouge_l,
    prose_token_prf,
    trim_edge_marks,
)
from app.evaluation_metrics.quality.results import ScoreResult
from tests.app_sources import APP_DIR, python_files, relative, tree
from tests.evaluation_helpers import FIXTURES_DIR, detail_list

# ── normalization policy: options and closure ──
# Closure means `normalize` performs exactly four stages and nothing else: no stemming,
# lemmatization, stop-word removal, spelling correction, transliteration, or number/date
# canonicalization.

_CORPUS = FIXTURES_DIR / "prose_normalization_corpus.json"


def test_defaults_are_strict() -> None:
    policy = ProseNormalizationPolicy()
    assert policy.unicode_form is UnicodeForm.NFC
    assert policy.case is CaseMode.preserve
    assert policy.punctuation is PunctuationMode.preserve
    assert policy.whitespace is WhitespaceMode.collapse


def test_policy_is_frozen() -> None:
    policy = ProseNormalizationPolicy()
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
        policy.case = CaseMode.casefold


@pytest.mark.parametrize(
    ("form", "text", "expected"),
    [
        (UnicodeForm.NFC, "e\u0301", "\u00e9"),
        (UnicodeForm.NFD, "\u00e9", "e\u0301"),
        (UnicodeForm.NFKC, "\ufb01le", "file"),
        (UnicodeForm.NFKD, "\ufb01le", "file"),
    ],
)
def test_unicode_form_option(form: UnicodeForm, text: str, expected: str) -> None:
    assert ProseNormalizationPolicy(unicode_form=form).normalize(text) == expected


def test_nfkc_folds_compatibility_forms_that_nfc_keeps_distinct() -> None:
    """Compatibility folding is opt-in: only NFKC/NFKD fold these forms."""
    superscript, fullwidth = "x\u00b2", "\uff21\uff22"
    nfc = ProseNormalizationPolicy()
    nfkc = ProseNormalizationPolicy(unicode_form=UnicodeForm.NFKC)
    assert nfc.normalize(superscript) == superscript
    assert nfkc.normalize(superscript) == "x2"
    assert nfc.normalize(fullwidth) == fullwidth
    assert nfkc.normalize(fullwidth) == "AB"


def test_case_option_uses_full_unicode_folding() -> None:
    policy = ProseNormalizationPolicy(case=CaseMode.casefold)
    assert policy.normalize("STRASSE") == "strasse"
    assert policy.normalize("Stra\u00dfe") == "strasse"
    assert ProseNormalizationPolicy().normalize("Stra\u00dfe") == "Stra\u00dfe"


def test_punctuation_strip_splits_rather_than_fuses() -> None:
    policy = ProseNormalizationPolicy(punctuation=PunctuationMode.strip)
    assert policy.tokenize("state-of-the-art") == ("state", "of", "the", "art")
    assert ProseNormalizationPolicy().tokenize("state-of-the-art") == (
        "state-of-the-art",
    )


def test_punctuation_strip_covers_connector_punctuation() -> None:
    """``Pc`` counts as punctuation, so ``snake_case`` becomes two tokens."""
    assert unicodedata.category("_") == "Pc"
    strip = ProseNormalizationPolicy(punctuation=PunctuationMode.strip)
    assert strip.tokenize("snake_case here") == ("snake", "case", "here")
    assert ProseNormalizationPolicy().tokenize("snake_case here") == (
        "snake_case",
        "here",
    )


@pytest.mark.parametrize("mode", list(PunctuationMode))
def test_symbol_categories_survive_every_punctuation_setting(
    mode: PunctuationMode,
) -> None:
    """Symbols are not punctuation: none of these is ever removed (``edges`` trims the
    backtick, a symbol, only as a pair of markdown markers)."""
    policy = ProseNormalizationPolicy(punctuation=mode)
    for symbol in ("\U0001f600", "\u20ac", "+"):
        assert symbol in policy.normalize(f"a {symbol} b")


def test_whitespace_option() -> None:
    text = "  a\t\tb\u00a0\u3000c  "
    assert ProseNormalizationPolicy().normalize(text) == "a b c"
    preserve = ProseNormalizationPolicy(whitespace=WhitespaceMode.preserve)
    assert preserve.normalize(text) == text


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        ("unicode_form", "NFX", "['NFC', 'NFD', 'NFKC', 'NFKD']"),
        ("case", "lower", "['casefold', 'preserve']"),
        ("punctuation", "remove", "['edges', 'preserve', 'strip']"),
        ("whitespace", "squash", "['collapse', 'preserve']"),
    ],
    ids=["unicode_form", "case", "punctuation", "whitespace"],
)
def test_unknown_option_raises_configuration_error(field: str, value: str, accepted: str) -> None:
    """The message names the field and value and lists the accepted values in sorted order."""
    with pytest.raises(ConfigurationError) as excinfo:
        ProseNormalizationPolicy.model_validate({field: value})
    assert str(excinfo.value) == f"unknown {field} {value!r}; expected one of {accepted}"


def test_as_detail_reports_every_option() -> None:
    detail = ProseNormalizationPolicy(case=CaseMode.casefold).as_detail()
    assert detail == {
        "unicode_form": "NFC",
        "case": "casefold",
        "punctuation": "preserve",
        "whitespace": "collapse",
    }


def _compose(policy: ProseNormalizationPolicy, text: str) -> str:
    """Independently computed four-stage composition, used as an oracle."""
    staged = unicodedata.normalize(policy.unicode_form.value, text)
    if policy.case is CaseMode.casefold:
        staged = staged.casefold()
    if policy.punctuation is PunctuationMode.strip:
        staged = "".join(
            " " if unicodedata.category(ch).startswith("P") else ch for ch in staged
        )
    if policy.punctuation is PunctuationMode.edges:
        # The per-token rule is pinned case by case below; here, only that it applies to
        # each whitespace-delimited token and leaves the whitespace to the next stage.
        staged = re.sub(r"\S+", lambda m: trim_edge_marks(m.group(0)), staged)
    if policy.whitespace is WhitespaceMode.collapse:
        staged = " ".join(staged.split())
    return staged


_POLICIES = [
    ProseNormalizationPolicy(),
    ProseNormalizationPolicy(case=CaseMode.casefold),
    ProseNormalizationPolicy(punctuation=PunctuationMode.strip),
    ProseNormalizationPolicy(case=CaseMode.casefold, punctuation=PunctuationMode.edges),
    ProseNormalizationPolicy(
        punctuation=PunctuationMode.edges, whitespace=WhitespaceMode.preserve
    ),
    ProseNormalizationPolicy(unicode_form=UnicodeForm.NFKC),
    ProseNormalizationPolicy(
        unicode_form=UnicodeForm.NFKD,
        case=CaseMode.casefold,
        punctuation=PunctuationMode.strip,
        whitespace=WhitespaceMode.preserve,
    ),
]


def _corpus_inputs() -> list[dict[str, str]]:
    data = json.loads(_CORPUS.read_text(encoding="utf-8"))
    entries: list[dict[str, str]] = data["entries"]
    return entries


@pytest.mark.parametrize("policy", _POLICIES)
def test_normalize_is_exactly_the_four_stage_composition(
    policy: ProseNormalizationPolicy,
) -> None:
    """Closure stated as a falsifiable equality rather than an unprovable negative."""
    for entry in _corpus_inputs():
        text = entry["text"]
        assert policy.normalize(text) == _compose(policy, text), entry["detects"]


@pytest.mark.parametrize("policy", _POLICIES)
def test_corpus_content_survives_normalization(
    policy: ProseNormalizationPolicy,
) -> None:
    """Each discriminating token must still be present, proving nothing rewrote it."""
    for entry in _corpus_inputs():
        assert entry["must_survive"] in policy.normalize(entry["text"])


@pytest.mark.parametrize("apostrophe", ["'", "\u2019"])
def test_apostrophised_words_split_under_strip(apostrophe: str) -> None:
    """Both apostrophe forms are ``P*``, so they split like a hyphen rather than fuse."""
    policy = ProseNormalizationPolicy(punctuation=PunctuationMode.strip)
    assert unicodedata.category(apostrophe).startswith("P")
    assert policy.tokenize(f"don{apostrophe}t stop") == ("don", "t", "stop")
    assert ProseNormalizationPolicy().tokenize(f"don{apostrophe}t stop") == (
        f"don{apostrophe}t",
        "stop",
    )


# ── tokenization: Unicode whitespace, zero-token input, script tradeoffs ──

@pytest.mark.parametrize(
    "separator",
    ["\u0020", "\t", "\n", "\r\n", "\u00a0", "\u3000", "\u2009", "   \t  "],
)
def test_every_unicode_whitespace_class_separates_tokens(separator: str) -> None:
    policy = ProseNormalizationPolicy()
    assert policy.tokenize(f"alpha{separator}beta") == ("alpha", "beta")


def test_empty_and_whitespace_only_input_yield_no_tokens() -> None:
    policy = ProseNormalizationPolicy()
    assert policy.tokenize("") == ()
    assert policy.tokenize("   \t\n ") == ()


def test_punctuation_only_input_yields_no_tokens_under_strip() -> None:
    policy = ProseNormalizationPolicy(punctuation=PunctuationMode.strip)
    assert policy.tokenize("!!!") == ()
    assert ProseNormalizationPolicy().tokenize("!!!") == ("!!!",)


_EDGES = ProseNormalizationPolicy(punctuation=PunctuationMode.edges)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("The actor used evil.com.", ("The", "actor", "used", "evil.com"), id="trailing-period"),
        pytest.param("**APT29** used T1059.001!", ("APT29", "used", "T1059.001"), id="bold-and-bang"),
        pytest.param("`evil.com` (C2)", ("evil.com", "C2"), id="backticks-and-parentheses"),
        pytest.param('"quoted", \u00abguillemets\u00bb', ("quoted", "guillemets"), id="quotes"),
        pytest.param("- item\n# Title ...", ("item", "Title"), id="punctuation-only-tokens-dropped"),
        pytest.param(
            "snake_case state-of-the-art 10.0.0.1 C:\\Windows\\cmd.exe",
            ("snake_case", "state-of-the-art", "10.0.0.1", "C:\\Windows\\cmd.exe"),
            id="inner-punctuation-kept",
        ),
        pytest.param(
            "C# -5 .NET ::1 50% @admin /etc/ *.evil.com __init__ #1",
            ("C#", "-5", ".NET", "::1", "50%", "@admin", "/etc/", "*.evil.com", "__init__", "#1"),
            id="identifying-marks-kept",
        ),
        pytest.param(
            "fe80:: a:: C: Indicators:",
            ("fe80::", "a::", "C", "Indicators"),
            id="a-trailing-double-colon-is-kept-a-single-one-is-not",
        ),
        pytest.param(
            "(see T1078), x.com/a_(b) (https://x.com/a_(b)). f(x)",
            ("see", "T1078", "x.com/a_(b)", "https://x.com/a_(b)", "f(x)"),
            id="brackets-trimmed-unless-they-enclose-part-of-the-token",
        ),
        pytest.param("(a)(b) f(x)(y)", ("(a)(b)", "f(x)(y)"), id="adjacent-groups-keep-their-brackets"),
        pytest.param(
            "*a* `b` **c d** `.NET` *.evil.com",
            ("a", "b", "c", "d", ".NET", "*.evil.com"),
            id="markers-trimmed-where-they-mark-emphasis",
        ),
    ],
)
def test_punctuation_edges_trims_only_the_ends_of_each_token(
    text: str, expected: tuple[str, ...]
) -> None:
    """``edges`` trims what presents a token, never what identifies it."""
    assert _EDGES.tokenize(text) == expected


def test_punctuation_edges_leaves_whitespace_to_the_whitespace_option() -> None:
    policy = ProseNormalizationPolicy(
        punctuation=PunctuationMode.edges, whitespace=WhitespaceMode.preserve
    )
    assert policy.normalize("a.  b,\n- c") == "a  b\n c"


@pytest.mark.parametrize(
    "token",
    [
        "(" * 10**6,
        "*" * 10**6,
        "a." * 10**5,
        "(x" * 10**5 + ")" * 10**5,
        "*" * 500_000 + "a" + "*" * 500_000,
    ],
    ids=["open-brackets", "markers", "sentence-marks", "nested-brackets", "emphasis"],
)
def test_trim_edge_marks_is_linear_in_the_token(token: str) -> None:
    """A quadratic trim takes hours on these tokens. The bound is loose for slow runners."""
    started = time.perf_counter()
    trim_edge_marks(token)
    assert time.perf_counter() - started < 10.0


def test_trim_edge_marks_is_idempotent_and_only_trims() -> None:
    """Trimming again changes nothing, and the result is a slice of the token."""
    rng = random.Random(5749)  # noqa: S311 - a seeded, reproducible sample
    alphabet = "ab1.,:;!?()[]{}<>*`_'\"\u00ab\u00bb\u201c\u201d\u3002\uff08\uff09-#@/"
    for _ in range(20_000):
        token = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 10)))
        once = trim_edge_marks(token)
        assert trim_edge_marks(once) == once, token
        assert once in token, token


def test_tokenization_splits_on_whitespace_regardless_of_the_whitespace_option() -> None:
    """``whitespace="preserve"`` changes exact match but never tokenization."""
    preserve = ProseNormalizationPolicy(whitespace=WhitespaceMode.preserve)
    collapse = ProseNormalizationPolicy(whitespace=WhitespaceMode.collapse)
    text = "  alpha   beta  "
    assert preserve.tokenize(text) == collapse.tokenize(text) == ("alpha", "beta")
    assert preserve.normalize(text) != collapse.normalize(text)


_HEBREW = ("\u05d4\u05ea\u05d5\u05e7\u05e3", "\u05d4\u05d9\u05d4")
_ARABIC = ("\u0627\u0644\u0645\u0647\u0627\u062c\u0645", "\u0627\u0633\u062a\u062e\u062f\u0645")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A script written without spaces is one token: the accepted tradeoff of
        # whitespace-only, dependency-free tokenization.
        (
            "\u653b\u6483\u8005\u306f\u4fb5\u5165\u3057\u305f",
            ("\u653b\u6483\u8005\u306f\u4fb5\u5165\u3057\u305f",),
        ),
        ("cafe\u0301 \U0001f512", ("caf\u00e9", "\U0001f512")),
        ("a\u200db", ("a\u200db",)),
        (" ".join(_HEBREW), _HEBREW),
        (" ".join(_ARABIC), _ARABIC),
        # U+200F and U+202B are not whitespace, so they never create a token boundary.
        ("\u200fabc\u202bdef", ("\u200fabc\u202bdef",)),
        ("actor \u05d4\u05d9\u05d4 here", ("actor", "\u05d4\u05d9\u05d4", "here")),
    ],
    ids=[
        "no_space_script",
        "combining_mark_and_emoji",
        "zero_width_joiner",
        "right_to_left_hebrew",
        "right_to_left_arabic",
        "bidi_controls",
        "mixed_direction",
    ],
)
def test_only_whitespace_splits_a_token(text: str, expected: tuple[str, ...]) -> None:
    """Scripts, joiners, marks and text direction get no special handling."""
    assert ProseNormalizationPolicy().tokenize(text) == expected


# ── Markdown and HTML are scored as raw prose ──

_POLICY = ProseNormalizationPolicy()
_STRIP = ProseNormalizationPolicy(punctuation=PunctuationMode.strip)


def test_markdown_emphasis_characters_are_part_of_the_token() -> None:
    assert _POLICY.tokenize("**bold**") == ("**bold**",)
    assert prose_exact_match("bold", "**bold**", _POLICY).value == 0.0


def test_markdown_emphasis_is_stripped_only_when_punctuation_is_stripped_or_trimmed() -> None:
    assert _STRIP.tokenize("**bold**") == ("bold",)
    assert _EDGES.tokenize("**bold**") == ("bold",)


def test_html_tags_are_not_removed() -> None:
    assert _POLICY.tokenize("<b>alert</b>") == ("<b>alert</b>",)
    result = prose_token_prf("alert", "<b>alert</b>", _POLICY)
    assert result.detail["tp"] == 0


def test_html_entities_are_not_decoded() -> None:
    assert _POLICY.normalize("&amp;") == "&amp;"
    assert prose_exact_match("&", "&amp;", _POLICY).value == 0.0


def test_code_fences_are_ordinary_text() -> None:
    fenced = "```\ncode here\n```"
    assert _POLICY.tokenize(fenced) == ("```", "code", "here", "```")


def test_link_text_is_not_extracted() -> None:
    assert prose_required_phrase_coverage(
        "see [the report](https://example.test)", ("the report",), _POLICY
    ).value == 0.0


def test_markup_phrase_matches_when_supplied_verbatim() -> None:
    assert prose_required_phrase_coverage(
        "see [the report](https://example.test)", ("[the report](https://example.test)",),
        _POLICY,
    ).value == 100.0


# ── locale independence ──
# The Turkish dotted and dotless `I` is the classic case where locale-aware case handling
# changes a result. `str.lower` and `str.casefold` ignore the locale, so the package stays
# independent as long as it calls nothing that reads it. The scan reads names as written:
# an aliased `re` (`import re as _re`) or a module imported by string
# (`importlib.import_module("locale")`) escapes it.

_PACKAGE = APP_DIR / "evaluation_metrics"
# Functions whose result follows the process locale.
_LOCALE_DEPENDENT_NAMES = {"strxfrm", "strcoll", "getlocale", "setlocale", "localeconv"}
# ``re`` flags that make matching follow the locale.
_LOCALE_REGEX_FLAGS = {"LOCALE", "L"}


def _locale_reads(module: ast.Module) -> list[str]:
    """Every place *module* reads the process locale, in source order."""
    reads = []
    for node in ast.walk(module):
        if isinstance(node, ast.Import) and any(alias.name == "locale" for alias in node.names):
            reads.append((node.lineno, "import locale"))
        elif isinstance(node, ast.ImportFrom) and node.module == "locale":
            reads.append((node.lineno, "from locale import ..."))
        elif isinstance(node, ast.Name) and node.id in _LOCALE_DEPENDENT_NAMES:
            reads.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in _LOCALE_DEPENDENT_NAMES:
            reads.append((node.lineno, f".{node.attr}"))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in _LOCALE_REGEX_FLAGS
            and isinstance(node.value, ast.Name)
            and node.value.id == "re"
        ):
            reads.append((node.lineno, f"re.{node.attr}"))
    return [f"line {line}: {what}" for line, what in sorted(reads)]


def test_the_scan_covers_the_package() -> None:
    """Without this, moving the package turns the tripwire below into a silent skip."""
    assert python_files(_PACKAGE), f"{_PACKAGE} holds no Python files"


@pytest.mark.parametrize("path", python_files(_PACKAGE), ids=lambda path: relative(path, _PACKAGE))
def test_the_metric_package_reads_no_locale(path) -> None:
    """Tripwire: no module of ``app/evaluation_metrics`` reads the process locale."""
    assert _locale_reads(tree(path)) == [], f"{relative(path)} reads the locale"


def test_the_scan_catches_every_banned_spelling() -> None:
    """The scan finds each banned spelling, so an empty result above means something."""
    module = ast.parse(
        "import locale\n"
        "from locale import getlocale\n"
        "import re\n"
        "key = locale.strxfrm('x')\n"
        "pattern = re.compile('a', re.LOCALE)\n"
        "order = strcoll('a', 'b')\n"
    )

    assert _locale_reads(module) == [
        "line 1: import locale",
        "line 2: from locale import ...",
        "line 4: .strxfrm",
        "line 5: re.LOCALE",
        "line 6: strcoll",
    ]


# ── the caller's policy reaches every input site ──
# Without this, an implementation that normalized the prediction with the caller's policy
# but the reference or phrases with a default would pass every other test.

_PRESERVE = ProseNormalizationPolicy()
_CASEFOLD = ProseNormalizationPolicy(case=CaseMode.casefold)

_REFERENCE = "The Threat Actor"
_PREDICTION = "the threat ACTOR"
_PHRASE = "Threat Actor"

# The phrase rows also pin the phrase's normalized form: the casefold run reports the
# casefolded phrase, so the phrase and the prediction were normalized by the same policy.
_SITES: list[tuple[str, Callable[[ProseNormalizationPolicy], ScoreResult], float, float, bool]] = [
    ("exact", lambda p: prose_exact_match(_REFERENCE, _PREDICTION, p), 0.0, 100.0, False),
    ("token", lambda p: prose_token_prf(_REFERENCE, _PREDICTION, p), 0.0, 100.0, False),
    ("rouge", lambda p: prose_rouge_l(_REFERENCE, _PREDICTION, p), 0.0, 100.0, False),
    (
        "required",
        lambda p: prose_required_phrase_coverage(_PREDICTION, (_PHRASE,), p),
        0.0,
        100.0,
        True,
    ),
    # Forbidden-phrase absence is inverted: a match is a violation and scores 0.
    (
        "forbidden",
        lambda p: prose_forbidden_phrase_absence(_PREDICTION, (_PHRASE,), p),
        100.0,
        0.0,
        True,
    ),
]


@pytest.mark.parametrize(
    ("call", "preserve_value", "casefold_value", "is_phrase"),
    [row[1:] for row in _SITES],
    ids=[row[0] for row in _SITES],
)
def test_every_site_uses_the_supplied_policy(
    call: Callable[[ProseNormalizationPolicy], ScoreResult],
    preserve_value: float,
    casefold_value: float,
    is_phrase: bool,
) -> None:
    preserve = call(_PRESERVE)
    casefold = call(_CASEFOLD)
    assert preserve.value == preserve_value
    assert casefold.value == casefold_value
    if is_phrase:
        assert detail_list(preserve, "unmatched") == ["Threat Actor"]
        assert detail_list(casefold, "matched") == ["threat actor"]


# ── prose invariants over the policy grid ──
# The four normalization options are independent, so `POLICIES` is the complete option
# space (4 x 2 x 2 x 2 = 32 policies), not a sample. Each invariant is parametrized by
# policy and walks the corpus internally, which keeps the test count proportional to the
# option space instead of its cross product. `CORPUS` hits the cases that break prose
# scoring: empty and whitespace-only input, punctuation-only input, repeated tokens,
# precomposed vs. decomposed forms, ligatures, and text whose result depends on stage order.

POLICIES: list[ProseNormalizationPolicy] = [
    ProseNormalizationPolicy(
        unicode_form=unicode_form,
        case=case,
        punctuation=punctuation,
        whitespace=whitespace,
    )
    for unicode_form, case, punctuation, whitespace in itertools.product(
        UnicodeForm, CaseMode, PunctuationMode, WhitespaceMode
    )
]

POLICY_IDS: list[str] = [
    f"{p.unicode_form.value}-{p.case.value}-{p.punctuation.value}-{p.whitespace.value}"
    for p in POLICIES
]

CORPUS: list[str] = [
    "",
    " ",
    "\t\n  ",
    ".",
    "...",
    "a",
    "A",
    "the quick brown fox",
    "The Quick  Brown Fox.",
    "the the the",
    "one, two; three.",
    "  leading and trailing  ",
    "caf\u00e9",  # precomposed
    "cafe\u0301",  # decomposed - NFC and NFD disagree
    "\u03d3\u0390\u01f0",  # folding depends on the unicode/case stage order
    "\ufb01ne",  # ligature - NFKC and NFC disagree
    "STRASSE",
    "stra\u00dfe",  # casefolds to "strasse"
]

PAIRS: list[tuple[str, str]] = list(itertools.product(CORPUS, CORPUS))

PHRASES: tuple[str, ...] = ("quick", "brown fox", "missing", "the the")

_policies = pytest.mark.parametrize("policy", POLICIES, ids=POLICY_IDS)


@_policies
def test_normalization_is_idempotent(policy: ProseNormalizationPolicy) -> None:
    for text in CORPUS:
        once = policy.normalize(text)
        assert policy.normalize(once) == once, text


_SEQUENCE_METRICS = pytest.mark.parametrize(
    "metric", [prose_token_prf, prose_rouge_l], ids=["token_prf", "rouge_l"]
)


@_SEQUENCE_METRICS
@_policies
def test_exact_match_implies_a_perfect_sequence_score(
    policy: ProseNormalizationPolicy, metric: Callable[..., ScoreResult]
) -> None:
    """Every pair that matches exactly after normalization scores 100, when it is scored.

    Unqualified the invariant is false: two zero-token sides are equal strings, so exact
    match scores 100 while the sequence metrics skip. An empty reference is never scored.
    """
    for reference, prediction in PAIRS:
        exact = prose_exact_match(reference, prediction, policy)
        if reference == prediction and reference.strip():
            assert exact.value == 100.0, reference
        if exact.value != 100.0:
            continue
        result = metric(reference, prediction, policy)
        if result.value is not None:
            assert result.value == 100.0, (reference, prediction)


@_policies
def test_rouge_l_value_and_lcs_length_are_within_bounds(
    policy: ProseNormalizationPolicy,
) -> None:
    """ROUGE-L stays in [0, 100] and its LCS can never exceed the shorter side."""
    for reference, prediction in PAIRS:
        result = prose_rouge_l(reference, prediction, policy)
        if result.value is None:
            continue
        assert 0.0 <= result.value <= 100.0, (reference, prediction)
        lcs_length = int(str(result.detail["lcs_length"]))
        reference_tokens = int(str(result.detail["reference_tokens"]))
        prediction_tokens = int(str(result.detail["prediction_tokens"]))
        assert lcs_length <= min(reference_tokens, prediction_tokens)


@_policies
def test_rouge_l_never_exceeds_token_prf(policy: ProseNormalizationPolicy) -> None:
    """Whenever both are scored: order-constrained overlap cannot beat unordered."""
    for reference, prediction in PAIRS:
        rouge = prose_rouge_l(reference, prediction, policy)
        token = prose_token_prf(reference, prediction, policy)
        if rouge.value is None or token.value is None:
            continue
        assert rouge.value <= token.value, (reference, prediction)


@_SEQUENCE_METRICS
@_policies
def test_swapping_sides_swaps_precision_and_recall(
    policy: ProseNormalizationPolicy, metric: Callable[..., ScoreResult]
) -> None:
    """Swapping the sides swaps precision and recall; both overlaps are symmetric, so F1 holds."""
    for reference, prediction in PAIRS:
        forward = metric(reference, prediction, policy)
        reverse = metric(prediction, reference, policy)
        if forward.value is None or reverse.value is None:
            continue
        assert forward.detail["precision"] == reverse.detail["recall"]
        assert forward.detail["recall"] == reverse.detail["precision"]
        assert forward.detail["f1"] == reverse.detail["f1"]
        assert forward.value == reverse.value


def test_reported_presence_implies_contiguous_subsequence() -> None:
    policy = ProseNormalizationPolicy()
    for text in CORPUS:
        for phrase in PHRASES:
            result = prose_required_phrase_coverage(text, (phrase,), policy)
            if result.value is None:
                continue
            tokens = policy.tokenize(text)
            phrase_tokens = policy.tokenize(phrase)
            present = detail_list(result, "matched") == [policy.normalize(phrase)]
            windows = [
                tuple(tokens[i : i + len(phrase_tokens)])
                for i in range(len(tokens) - len(phrase_tokens) + 1)
            ]
            assert present == (tuple(phrase_tokens) in windows), (text, phrase)


@_policies
def test_total_equals_matched_plus_unmatched(policy: ProseNormalizationPolicy) -> None:
    for prediction in CORPUS:
        for metric in (prose_required_phrase_coverage, prose_forbidden_phrase_absence):
            result = metric(prediction, PHRASES, policy)
            if result.value is None:
                continue
            matched = detail_list(result, "matched")
            unmatched = detail_list(result, "unmatched")
            assert result.detail["total"] == len(matched) + len(unmatched)


# ── fixed stage order ──
# The four stages run in a fixed documented order. Reordering them preserves every
# invariant above, so the order is pinned here against concrete expected output.

_STAGES: dict[str, Callable[[str], str]] = {
    "unicode_form": lambda text: unicodedata.normalize("NFKC", text),
    "case": str.casefold,
    "punctuation": lambda text: "".join(
        " " if unicodedata.category(ch).startswith("P") else ch for ch in text
    ),
    "whitespace": lambda text: " ".join(text.split()),
}

_DOCUMENTED_ORDER: tuple[str, ...] = ("unicode_form", "case", "punctuation", "whitespace")

_ALL_STAGES_POLICY = ProseNormalizationPolicy(
    unicode_form=UnicodeForm.NFKC,
    case=CaseMode.casefold,
    punctuation=PunctuationMode.strip,
    whitespace=WhitespaceMode.collapse,
)

# Inputs chosen so that every observable reordering changes at least one of them:
#   U+03D3  folds differently before vs. after case folding
#   U+013F  NFKC-expands to "L\u00b7", creating punctuation the strip stage would remove
#   U+00A8  NFKC-expands to " \u0308", creating whitespace the collapse stage would remove
#   U+037A  NFKC-expands to " \u03b9", creating whitespace after a letter
#   "a - b" needs punctuation substituted before whitespace is collapsed
_ORDER_CORPUS: tuple[str, ...] = (
    "\u03d3",
    "\u013f",
    "\u00a8",
    "a - b",
    "STRA\u00dfE,  Ltd.",
    "\ufb01ne.",
    "x\u037ay",
    "the  fox.",
)

# Case folding neither creates nor destroys a punctuation or whitespace character
# anywhere in Unicode, and it is a per-character map, so it commutes with both of those
# stages. Their relative order is unobservable and cannot be guarded by any input.
_COMMUTING_ORDERS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("unicode_form", "punctuation", "case", "whitespace"),
        ("unicode_form", "punctuation", "whitespace", "case"),
    }
)


def _apply_in_order(order: Sequence[str], text: str) -> str:
    for stage in order:
        text = _STAGES[stage](text)
    return text


@pytest.mark.parametrize(
    "order",
    [o for o in itertools.permutations(_DOCUMENTED_ORDER) if o != _DOCUMENTED_ORDER],
    ids=lambda order: "-".join(stage[:4] for stage in order),
)
def test_no_other_stage_order_reproduces_the_documented_one(order: tuple[str, ...]) -> None:
    """Every observable reordering of the four stages changes at least one input.

    The two commuting orders are asserted indistinguishable rather than skipped, so a
    stage that breaks the commutation fails here.
    """
    witnesses = [
        text
        for text in _ORDER_CORPUS
        if _apply_in_order(order, text) != _apply_in_order(_DOCUMENTED_ORDER, text)
    ]

    if order in _COMMUTING_ORDERS:
        assert not witnesses, f"{order} no longer commutes; it now needs its own guard"
        return

    assert witnesses, f"no input in _ORDER_CORPUS discriminates {order}"
    for text in witnesses:
        assert _ALL_STAGES_POLICY.normalize(text) == _apply_in_order(
            _DOCUMENTED_ORDER, text
        )


@pytest.mark.parametrize("text", _ORDER_CORPUS)
def test_normalize_matches_the_documented_stage_order(text: str) -> None:
    assert _ALL_STAGES_POLICY.normalize(text) == _apply_in_order(_DOCUMENTED_ORDER, text)

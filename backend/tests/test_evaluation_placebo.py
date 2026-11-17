"""The placebo of a placebo-controlled evaluation: configuration and construction.

``dev-docs/agent-placebo-evaluation.md`` FR-1 to FR-10: what a configuration
accepts, which targets need the agentic loop, and the placebo itself - neutral
text from the versioned corpus, in the original's language, without a
distinctive word of what it replaces, matched in length and rebuilt identically
from its seed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid

import pytest
from pydantic import ValidationError

from app.services import evaluation_placebo as placebo
from app.services.evaluation_placebo import (
    CHARACTER_MEASURER,
    LENGTH_TOLERANCE,
    PLACEBO_CORPUS_DIR,
    LengthMeasurer,
    PlaceboConfig,
    PlaceboCorpus,
    PlaceboLeakError,
    PlaceboSource,
    build_placebo,
    detect_language,
    distinctive_words,
    imported_placebo_config,
    load_placebo_corpora,
    normalized_words,
    parse_placebo_config,
    placebo_mode_error,
)

SKILL_ID = uuid.uuid4()
FRENCH_PERSONA = (
    "Tu es un analyste du renseignement sur les menaces. Tu lis les rapports, tu identifies les acteurs "
    "et les techniques, puis tu rédiges une synthèse claire avec les sources et les recommandations."
)
ENGLISH_PERSONA = (
    "You are a threat intelligence analyst. You read the reports that arrive, identify the actors and "
    "their techniques, and write a clear synthesis with the sources and your recommendations. " * 4
)


async def _quarter_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


#: A stand-in for the run model's tokenizer: about four characters a token.
TOKENS = LengthMeasurer("tokens", _quarter_tokens)


def _skill(name: str, description: str, content: str, **fields) -> PlaceboSource:
    return PlaceboSource(content=content, name=name, description=description, **fields)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_a_configuration_takes_its_defaults():
    config = PlaceboConfig.model_validate({"targets": [{"kind": "persona"}]})

    assert config.model_dump(mode="json") == {
        "targets": [{"kind": "persona", "skill_id": None}],
        "include_no_target": True,
        "trials_per_entry": 5,
        "custom_text": None,
    }


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"targets": []}, "at least 1 item"),
        ({"targets": [{"kind": "skill"}]}, "needs its skill_id"),
        ({"targets": [{"kind": "persona", "skill_id": str(SKILL_ID)}]}, "takes no skill_id"),
        ({"targets": [{"kind": "persona"}, {"kind": "persona"}]}, "Duplicate placebo targets: persona"),
        (
            {"targets": [{"kind": "skill", "skill_id": str(SKILL_ID)}, {"kind": "skill", "skill_id": str(SKILL_ID)}]},
            f"Duplicate placebo targets: skill:{SKILL_ID}",
        ),
        ({"targets": [{"kind": "skills"}, {"kind": "skill", "skill_id": str(SKILL_ID)}]}, "already replaces every skill"),
        ({"targets": [{"kind": "tools"}]}, "Input should be 'persona', 'prompts', 'skills' or 'skill'"),
        ({"targets": [{"kind": "persona"}], "trials_per_entry": 0}, "greater than or equal to 1"),
        ({"targets": [{"kind": "persona"}], "trials_per_entry": 31}, "less than or equal to 30"),
        ({"targets": [{"kind": "persona"}], "shuffle": True}, "Extra inputs are not permitted"),
        ({"targets": [{"kind": "skill", "skill_id": str(uuid.uuid4())} for _ in range(21)]}, "at most 20 items"),
        ({"targets": [{"kind": "persona"}], "custom_text": "x" * 20_001}, "at most 20000 characters"),
    ],
    ids=[
        "no-target", "skill-without-id", "persona-with-id", "duplicate-persona", "duplicate-skill",
        "skills-and-skill", "unknown-kind", "zero-trials", "too-many-trials", "unknown-field",
        "too-many-targets", "custom-text-too-long",
    ],
)
def test_a_configuration_refuses(raw, message):
    with pytest.raises(ValidationError) as caught:
        PlaceboConfig.model_validate(raw)

    assert message in str(caught.value)


def test_a_blank_custom_text_is_no_custom_text_and_a_custom_text_is_trimmed():
    blank = PlaceboConfig.model_validate({"targets": [{"kind": "persona"}], "custom_text": "  \n "})
    text = PlaceboConfig.model_validate({"targets": [{"kind": "persona"}], "custom_text": "  Some text.  "})

    assert blank.custom_text is None
    assert text.custom_text == "Some text."


@pytest.mark.parametrize(
    ("kinds", "mode", "refused"),
    [
        (["persona"], "llm_only", None),
        (["persona"], "full_loop", None),
        (["prompts"], "llm_only", "prompts"),
        (["skills"], "llm_only", "skills"),
        (["skill"], "llm_only", "skill"),
        (["persona", "prompts", "skills"], "llm_only", "prompts, skills"),
        (["prompts", "skills"], "full_loop", None),
    ],
    ids=["persona-llm", "persona-loop", "prompts-llm", "skills-llm", "skill-llm", "mixed-llm", "loop"],
)
def test_prompts_and_skills_need_the_agentic_loop(kinds, mode, refused):
    config = PlaceboConfig.model_validate(
        {"targets": [{"kind": kind, **({"skill_id": str(SKILL_ID)} if kind == "skill" else {})} for kind in kinds]}
    )

    error = placebo_mode_error(config, mode)

    if refused is None:
        assert error is None
    else:
        assert error == (
            f"Placebo targets {refused} need the 'full_loop' execution mode: "
            f"in '{mode}' the model sees the persona alone"
        )


def test_no_configuration_never_conflicts_with_a_mode():
    assert placebo_mode_error(None, "llm_only") is None
    assert parse_placebo_config(None) is None
    assert parse_placebo_config({}) is None


def test_an_import_drops_the_targets_naming_one_skill_of_the_exporting_instance():
    config = PlaceboConfig.model_validate(
        {"targets": [{"kind": "persona"}, {"kind": "skill", "skill_id": str(SKILL_ID)}], "trials_per_entry": 7}
    )
    only_skill = PlaceboConfig.model_validate({"targets": [{"kind": "skill", "skill_id": str(SKILL_ID)}]})

    kept, dropped = imported_placebo_config(config)

    assert kept is not None
    assert [target.kind for target in kept.targets] == ["persona"]
    assert kept.trials_per_entry == 7
    assert dropped == [f"skill:{SKILL_ID}"]
    assert imported_placebo_config(only_skill) == (None, [f"skill:{SKILL_ID}"])
    assert imported_placebo_config(None) == (None, [])


# ---------------------------------------------------------------------------
# Corpus, language, distinctive words
# ---------------------------------------------------------------------------


def test_every_corpus_file_is_loaded_under_its_language_with_its_content_hash():
    corpora = load_placebo_corpora()
    files = sorted(PLACEBO_CORPUS_DIR.glob("*.json"))

    assert sorted(corpora) == sorted(path.stem for path in files) == ["en", "fr"]
    for path in files:
        corpus = corpora[path.stem]
        assert corpus.version == f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()[:16]}"
        assert len(corpus.passages) >= 30
        assert len(corpus.descriptions) >= 30
        assert len(corpus.names) >= 30
        assert len({name.casefold() for name in corpus.names}) == len(corpus.names)


def test_every_passage_and_description_reads_as_its_corpus_language():
    corpora = load_placebo_corpora()

    for language, corpus in corpora.items():
        for text in (*corpus.passages, *corpus.descriptions):
            assert detect_language(text, corpora) == language, text


def test_words_are_folded_to_letters_without_case_or_accent():
    assert normalized_words("L'Été d'IP-Adresse 42 naïve_x") == ["l", "ete", "d", "ip", "adresse", "naive", "x"]


@pytest.mark.parametrize(
    ("text", "language"),
    [
        (ENGLISH_PERSONA, "en"),
        (FRENCH_PERSONA, "fr"),
        ("Tu es un analyste. You answer the questions of the team.", "en"),
        ("", None),
        ("x = compute(1, 2)", None),
        ("the and le et", None),
    ],
    ids=["english", "french", "mixed-dominant-english", "empty", "code", "tie"],
)
def test_the_language_is_the_corpus_whose_function_words_dominate(text, language):
    assert detect_language(text, load_placebo_corpora()) == language


def test_distinctive_words_leave_out_function_words_and_short_words():
    words = distinctive_words(["The CTI Analyst", "Résumé des IOC et de la menace", ""], load_placebo_corpora())

    assert words == frozenset({"cti", "analyst", "resume", "ioc", "menace"})


# ---------------------------------------------------------------------------
# The placebo
# ---------------------------------------------------------------------------


async def test_a_persona_placebo_is_the_original_length_in_tokens_and_its_language():
    build = await build_placebo(
        "persona", [PlaceboSource(content=FRENCH_PERSONA)], agent_name="Analyste", seed="seed-1", measurer=TOKENS
    )

    (element,) = build.elements
    length = element.lengths["content"]
    assert build.language == "fr"
    assert build.language_detected is True
    assert build.unit == "tokens"
    assert detect_language(element.content, load_placebo_corpora()) == "fr"
    assert length.original == await _quarter_tokens(FRENCH_PERSONA)
    assert length.placebo == await _quarter_tokens(element.content)
    assert abs(length.placebo - length.original) <= LENGTH_TOLERANCE * length.original
    assert length.within_tolerance is True
    assert build.warnings == ()


async def test_the_same_seed_rebuilds_the_same_placebo_and_another_seed_another_one():
    async def _build(seed: str):
        return await build_placebo(
            "persona", [PlaceboSource(content=ENGLISH_PERSONA)], agent_name="Analyst", seed=seed, measurer=TOKENS
        )

    first, again, other = await _build("seed-1"), await _build("seed-1"), await _build("seed-2")

    assert first.snapshot() == again.snapshot()
    assert first.elements[0].content != other.elements[0].content


async def test_the_length_converges_when_the_unit_is_not_proportional_to_characters():
    async def _with_overhead(text: str) -> int:
        # A fixed cost per text plus a rate unlike the first guess's.
        return 7 + len(text) // 3

    build = await build_placebo(
        "persona",
        [PlaceboSource(content=ENGLISH_PERSONA)],
        agent_name="Analyst",
        seed="seed-1",
        measurer=LengthMeasurer("tokens", _with_overhead),
    )

    length = build.elements[0].lengths["content"]
    assert length.original == await _with_overhead(ENGLISH_PERSONA)
    assert length.within_tolerance is True


async def test_an_empty_persona_has_an_empty_placebo():
    build = await build_placebo(
        "persona", [PlaceboSource(content="")], agent_name="Analyst", seed="seed-1", measurer=CHARACTER_MEASURER
    )

    (element,) = build.elements
    assert element.content == ""
    assert element.lengths["content"] == placebo.PartLength(
        original=0, placebo=0, deviation=None, within_tolerance=True, repeated=False
    )


async def test_a_skill_placebo_keeps_the_count_the_pins_and_the_references_under_neutral_names():
    sources = [
        _skill(
            "IP Investigation",
            "Investigate an IP address across intelligence feeds and summarize the findings.",
            "# Investigating an address\n" + "Query every feed for the address, then correlate the answers. " * 20,
            ref_id="skill-a",
            version=3,
        ),
        _skill("Report Writer", "Write the final report.", "Open with an executive summary. " * 8, always_load=True),
    ]

    build = await build_placebo(
        "skills",
        sources,
        agent_name="CTI Analyst",
        seed="seed-1",
        measurer=CHARACTER_MEASURER,
        reserved_names=["Domain Investigation"],
    )

    assert [(e.always_load, e.ref_id, e.version) for e in build.elements] == [
        (False, "skill-a", 3),
        (True, None, None),
    ]
    names = [element.name for element in build.elements]
    english = load_placebo_corpora()["en"]
    assert len(set(names)) == 2
    assert all(name in english.names for name in names)
    assert build.elements[0].content != build.elements[1].content
    for element, source in zip(build.elements, sources, strict=True):
        assert sorted(element.lengths) == ["content", "listing"]
        assert element.lengths["listing"].original == len(f"{source.name}\n{source.description}")
        assert element.lengths["listing"].placebo == len(f"{element.name}\n{element.description}")
        assert element.lengths["content"].original == len(source.content)
        assert element.lengths["content"].within_tolerance is True


async def test_a_placebo_carries_no_distinctive_word_of_the_agent_name_or_of_what_it_replaces():
    # Words the corpus is full of: every passage, name and description using
    # one of them must be passed over.
    sources = [
        _skill("Dates and messages", "Read the files of a session.", "Body. " * 50),
        _skill("Tables", "Lists and pages.", "Body. " * 50),
    ]

    build = await build_placebo(
        "skills", sources, agent_name="Image Archive", seed="seed-1", measurer=CHARACTER_MEASURER
    )

    forbidden = {"dates", "messages", "read", "files", "session", "tables", "lists", "pages", "image", "archive"}
    for element in build.elements:
        placebo_words = set(normalized_words(f"{element.name}\n{element.description}\n{element.content}"))
        assert placebo_words & forbidden == set()
    assert build.snapshot()["leakage_check"] == "passed"


async def test_a_placebo_name_never_takes_a_name_the_agent_already_uses():
    english = load_placebo_corpora()["en"]
    reserved = list(english.names[:-1])

    build = await build_placebo(
        "skill",
        [_skill("Triage", "Sort the alerts.", "Body. " * 20)],
        agent_name="Agent",
        seed="seed-1",
        measurer=CHARACTER_MEASURER,
        reserved_names=[name.upper() for name in reserved],
    )

    assert build.elements[0].name == english.names[-1]


async def test_the_seed_picks_the_name_among_the_five_closest_in_length():
    english = load_placebo_corpora()["en"]
    original = "IP Investigation"
    by_closeness = sorted(english.names, key=lambda name: abs(len(name) - len(original)))
    widest_gap = abs(len(by_closeness[4]) - len(original))

    names = set()
    for seed in range(20):
        build = await build_placebo(
            "skill",
            [_skill(original, "", "Body. " * 5)],
            agent_name="Agent",
            seed=f"seed-{seed}",
            measurer=CHARACTER_MEASURER,
        )
        names.add(build.elements[0].name)

    assert len(names) > 1
    assert all(abs(len(name) - len(original)) <= widest_gap for name in names)


async def test_the_parts_of_every_element_are_measured_at_once():
    in_flight = 0
    peak = 0

    async def _slow_count(text: str) -> int:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return len(text)

    await build_placebo(
        "skills",
        [_skill("Gamma", "First one.", "Body. " * 20), _skill("Delta", "Second one.", "Body. " * 20)],
        agent_name="Agent",
        seed="seed-1",
        measurer=LengthMeasurer("chars", _slow_count),
    )

    # Two contents and two listings, measured side by side.
    assert peak == 4


async def test_a_custom_text_is_the_placebo_body():
    custom = "Neutral sentence one. Neutral sentence two. Neutral sentence three."

    build = await build_placebo(
        "persona",
        [PlaceboSource(content="You are an analyst of alerts. " * 2)],
        agent_name="Agent",
        seed="seed-1",
        measurer=CHARACTER_MEASURER,
        custom_text=custom,
    )

    assert build.custom_text is True
    assert build.elements[0].content.startswith("Neutral sentence one.")
    assert set(normalized_words(build.elements[0].content)) <= set(normalized_words(custom))


async def test_a_custom_text_carrying_a_distinctive_word_is_refused():
    with pytest.raises(PlaceboLeakError) as caught:
        await build_placebo(
            "skill",
            [_skill("Phishing Triage", "Sort the reported emails.", "Body.")],
            agent_name="Agent",
            seed="seed-1",
            measurer=CHARACTER_MEASURER,
            custom_text="Background on phishing and on emails in general.",
        )

    assert caught.value.kind == "skill"
    assert caught.value.words == ["emails", "phishing"]
    assert str(caught.value) == "No neutral placebo for the 'skill' target: its text would carry 'emails', 'phishing'"


def _tiny_corpus(**fields) -> dict[str, PlaceboCorpus]:
    english = load_placebo_corpora()["en"]
    values = {
        "language": "en",
        "version": "sha256:test",
        "function_words": english.function_words,
        "names": ("Alpha notes",),
        "descriptions": ("Notes about alpha things.",),
        "passages": ("The alpha passage is short. It has two sentences.",),
    } | fields
    return {"en": PlaceboCorpus(**values)}


async def test_a_corpus_left_empty_by_the_distinctive_words_refuses_the_target(monkeypatch):
    monkeypatch.setattr(placebo, "load_placebo_corpora", _tiny_corpus)

    with pytest.raises(PlaceboLeakError) as caught:
        await build_placebo(
            "persona",
            [PlaceboSource(content="You are the agent.")],
            agent_name="Alpha",
            seed="seed-1",
            measurer=CHARACTER_MEASURER,
        )

    assert caught.value.words == ["alpha"]


async def test_names_are_suffixed_once_the_corpus_runs_out_of_them(monkeypatch):
    monkeypatch.setattr(placebo, "load_placebo_corpora", lambda: _tiny_corpus(names=("Beta notes",)))

    build = await build_placebo(
        "skills",
        [_skill("Gamma", "First.", "Body."), _skill("Delta", "Second.", "Body.")],
        agent_name="Agent",
        seed="seed-1",
        measurer=CHARACTER_MEASURER,
    )

    assert [element.name for element in build.elements] == ["Beta notes", "Beta notes 2"]


async def test_an_original_longer_than_the_corpus_repeats_it_and_says_so(monkeypatch):
    monkeypatch.setattr(placebo, "load_placebo_corpora", _tiny_corpus)

    build = await build_placebo(
        "persona",
        [PlaceboSource(content="You are the agent of the team. " * 20)],
        agent_name="Agent",
        seed="seed-1",
        measurer=CHARACTER_MEASURER,
    )

    assert build.elements[0].lengths["content"].repeated is True
    assert build.elements[0].lengths["content"].within_tolerance is True
    assert build.warnings == ("corpus_repeated",)


async def test_an_original_of_undetermined_language_gets_the_default_language_and_a_warning():
    build = await build_placebo(
        "persona",
        [PlaceboSource(content="x = compute(1, 2) # 42 " * 3)],
        agent_name="Agent",
        seed="seed-1",
        measurer=CHARACTER_MEASURER,
    )

    assert build.language == "en"
    assert build.language_detected is False
    assert build.warnings == ("language_undetermined",)


async def test_a_name_longer_than_the_whole_listing_leaves_the_description_empty():
    build = await build_placebo(
        "skill",
        [_skill("IP", "", "Body of the skill. " * 10)],
        agent_name="Agent",
        seed="seed-1",
        measurer=CHARACTER_MEASURER,
    )

    (element,) = build.elements
    listing = element.lengths["listing"]
    assert element.description == ""
    assert listing.original == 2
    assert listing.placebo == len(element.name)
    assert listing.within_tolerance is False
    assert "length_out_of_tolerance" in build.warnings


async def test_the_snapshot_is_plain_json_with_every_part_length():
    build = await build_placebo(
        "prompts",
        [_skill("Style guide", "How answers are formatted.", "Use short paragraphs. " * 10, ref_id="p1", version=2)],
        agent_name="Agent",
        seed="seed-1",
        measurer=CHARACTER_MEASURER,
    )

    snapshot = build.snapshot()

    assert json.loads(json.dumps(snapshot)) == snapshot
    assert snapshot["kind"] == "prompts"
    assert snapshot["seed"] == "seed-1"
    assert snapshot["corpus_version"] == load_placebo_corpora()["en"].version
    assert snapshot["elements"][0]["ref_id"] == "p1"
    assert snapshot["elements"][0]["version"] == 2
    assert sorted(snapshot["elements"][0]["lengths"]) == ["content", "listing"]


@pytest.mark.parametrize(
    ("kind", "sources", "message"),
    [
        ("tools", [PlaceboSource(content="x")], "Unknown placebo target 'tools'"),
        ("persona", [], "exactly one persona"),
        ("persona", [PlaceboSource(content="a"), PlaceboSource(content="b")], "exactly one persona"),
    ],
    ids=["unknown-kind", "no-persona", "two-personas"],
)
async def test_build_refuses_a_malformed_request(kind, sources, message):
    with pytest.raises(ValueError, match=message):
        await build_placebo(kind, sources, agent_name="Agent", seed="s", measurer=CHARACTER_MEASURER)

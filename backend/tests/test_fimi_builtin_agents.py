"""Contract tests for the FIMI built-in agents and knowledge base.

Pins the wiring that makes the two FIMI built-ins usable out of the box:

- ``fimi-analyst`` is the only shipped consumer of the ``fimi_analytics``
  built-in tool group, so nothing else in CI would notice if that binding were
  dropped and the tool group went back to being unreachable without hand-built
  agents;
- ``fimi-stix-transformer`` must be able to *emit* OpenCTI FIMI domain objects.
  It uses the same shared STIX bundle schema as the harvester and the generic
  transformer. That schema's custom-object branch used to gate on
  ``oneOf: [core, cyber-observable-core]`` — two overlapping definitions, so
  every custom STIX Domain Object matched both and was rejected. The branch now
  uses ``anyOf``; ``test_cti_stix_bundle_schema.py`` pins that directly. The
  test here keeps the *persona's* required output objects covered, so a future
  narrowing of the schema surfaces as a FIMI failure rather than as a
  production-only mismatch between what the persona demands and what the output
  schema accepts;
- the VIGINUM doctrine knowledge base has to exist on disk and stay bound to
  both agents, otherwise the FIMI methodology answers are ungrounded.
"""

from __future__ import annotations

import jsonschema
import pytest

from app.agents.builtin_agents import (
    BUILTIN_AGENTS,
    BUILTIN_KNOWLEDGE_BASES,
    BUILTIN_SKILLS,
)
from app.agents.seed import SEED_DATA_DIR, _resolve_seed_content

_ANALYST_SLUG = "fimi-analyst"
_TRANSFORMER_SLUG = "fimi-stix-transformer"
_GENERIC_TRANSFORMER_SLUG = "cti-stix-transformer"
_DOCTRINE_KB_SLUG = "viginum-fimi-doctrine"


def _agent(slug: str) -> dict:
    for agent in BUILTIN_AGENTS:
        if agent.get("slug") == slug:
            return agent
    raise AssertionError(f"built-in agent {slug!r} not found in BUILTIN_AGENTS")


def _kb(slug: str) -> dict:
    for kb in BUILTIN_KNOWLEDGE_BASES:
        if kb.get("slug") == slug:
            return kb
    raise AssertionError(
        f"built-in knowledge base {slug!r} not found in BUILTIN_KNOWLEDGE_BASES"
    )


@pytest.fixture(scope="module")
def analyst() -> dict:
    return _agent(_ANALYST_SLUG)


@pytest.fixture(scope="module")
def transformer() -> dict:
    return _agent(_TRANSFORMER_SLUG)


class TestFimiAnalyst:
    def test_persona_resolves_from_seed_data(self, analyst):
        persona = _resolve_seed_content(
            "agent_persona", analyst.get("persona", _ANALYST_SLUG)
        )
        assert persona, (
            f"{_ANALYST_SLUG} persona must resolve to a seed file under "
            "seed_data/agent_persona/"
        )
        assert "VIGINUM" in persona

    def test_all_memory_tiers_disabled(self, analyst):
        """Conclusions come from the sources read in the conversation.

        With memory on, a remembered verdict and a wrong generalisation about
        a source came back in later conversations as if they had been read.
        """
        for field in (
            "memory_short_term_enabled",
            "memory_medium_term_enabled",
            "memory_persistent_enabled",
            "memory_user_enabled",
        ):
            assert analyst.get(field) is False, f"{_ANALYST_SLUG} must disable {field}"

    def test_binds_fimi_analytics_tool_group(self, analyst):
        assert "fimi_analytics" in analyst["builtin_tool_ids"], (
            f"{_ANALYST_SLUG} is the only built-in agent that binds the "
            "fimi_analytics tool group (STIX comparator, timeline diff, FIMI "
            "math, FIMI metrics); dropping it makes the group unreachable "
            "without a hand-built agent"
        )

    def test_is_a_chat_agent_with_opencti_access(self, analyst):
        assert analyst["disable_chat"] is False
        assert analyst["output_format"] == "markdown"
        assert "opencti" in analyst["integration_template_bindings"]

    def test_owns_the_fimi_vertical_general_assistant_intent(self, analyst):
        assert analyst["supported_intents"]["global.assistant"] == {"fimi": 5}, (
            "the FIMI Analyst is the fimi vertical's general assistant, at the "
            "same priority the OpenCTI/OpenAEV/OpenCRQ assistants carry in "
            "theirs"
        )

    def test_reachable_from_the_opencti_assistant(self):
        opencti_assistant = _agent("opencti-assistant")
        assert _ANALYST_SLUG in opencti_assistant["handover_agent_slugs"]
        assert (
            opencti_assistant["handover_type_slugs"][_ANALYST_SLUG] == "transfer"
        )


class TestFimiStixTransformer:
    def test_persona_resolves_from_seed_data(self, transformer):
        persona = _resolve_seed_content(
            "agent_persona", transformer.get("persona", _TRANSFORMER_SLUG)
        )
        assert persona
        # The disinformation specialization is the reason this agent exists
        # next to the generic transformer.
        assert "Disinformation Analysis" in persona

    def test_all_memory_tiers_disabled(self, transformer):
        for field in (
            "memory_short_term_enabled",
            "memory_medium_term_enabled",
            "memory_persistent_enabled",
            "memory_user_enabled",
        ):
            assert transformer.get(field) is False, (
                f"{_TRANSFORMER_SLUG} must disable {field}: it is a stateless "
                "playbook step"
            )

    def test_emits_json_and_validates_bundles(self, transformer):
        assert transformer["output_format"] == "json"
        assert "stix_bundle_validator" in transformer["builtin_tool_ids"]

    def test_output_schema_accepts_a_disinformation_analysis_bundle(
        self, transformer
    ):
        """The persona's required output objects must pass the output schema.

        Mirrors the "Required Output Objects" section of the persona: a
        strict-passthrough media-content observable, the Case-RFI carrying the
        verdict card, a detected individual, both narratives, and the ``uses`` /
        ``related-to`` / ``subnarrative-of`` relationships.
        """
        topic_id = "narrative--44444444-4444-4444-8444-444444444444"
        category_id = "narrative--77777777-7777-4777-8777-777777777777"
        media_id = "media-content--22222222-2222-4222-8222-222222222222"
        individual_id = "identity--66666666-6666-4666-8666-666666666666"
        stamps = {
            "created": "2026-07-30T10:00:00.000Z",
            "modified": "2026-07-30T10:00:00.000Z",
        }
        bundle = {
            "type": "bundle",
            "id": "bundle--11111111-1111-4111-8111-111111111111",
            "objects": [
                {
                    "type": "media-content",
                    "id": media_id,
                    "spec_version": "2.1",
                    "x_opencti_type": "Media-Content",
                    "title": "A claim about the election",
                    "url": "https://example.test/article",
                },
                {
                    "type": "x-opencti-case-rfi",
                    "id": "x-opencti-case-rfi--33333333-3333-4333-8333-333333333333",
                    "spec_version": "2.1",
                    "x_opencti_type": "Case-Rfi",
                    "name": "Disinformation Analysis: A claim about the election",
                    "description": "### 🔍 Narrative Verdict Card",
                    "confidence": 100,
                    "revoked": False,
                    "object_refs": [media_id, individual_id, topic_id, category_id],
                    **stamps,
                },
                {
                    "type": "identity",
                    "id": individual_id,
                    "spec_version": "2.1",
                    "identity_class": "individual",
                    "name": "Judita LAŠŠÁKOVÁ",
                    "confidence": 70,
                    "revoked": False,
                    **stamps,
                },
                {
                    "type": "narrative",
                    "id": topic_id,
                    "spec_version": "2.1",
                    "x_opencti_type": "Narrative",
                    "name": "Election fraud claims",
                    "confidence": 80,
                    "revoked": False,
                    **stamps,
                },
                {
                    "type": "narrative",
                    "id": category_id,
                    "spec_version": "2.1",
                    "x_opencti_type": "Narrative",
                    "name": "Undermining democratic institutions",
                    "confidence": 100,
                    "revoked": False,
                    **stamps,
                },
                {
                    "type": "relationship",
                    "id": "relationship--55555555-5555-4555-8555-555555555555",
                    "spec_version": "2.1",
                    "relationship_type": "uses",
                    "source_ref": media_id,
                    "target_ref": topic_id,
                    **stamps,
                },
                {
                    "type": "relationship",
                    "id": "relationship--88888888-8888-4888-8888-888888888888",
                    "spec_version": "2.1",
                    "relationship_type": "related-to",
                    "source_ref": media_id,
                    "target_ref": individual_id,
                    **stamps,
                },
                {
                    "type": "relationship",
                    "id": "relationship--99999999-9999-4999-8999-999999999999",
                    "spec_version": "2.1",
                    "relationship_type": "subnarrative-of",
                    "source_ref": topic_id,
                    "target_ref": category_id,
                    **stamps,
                },
            ],
        }

        errors = sorted(
            jsonschema.Draft202012Validator(
                transformer["output_schema"]
            ).iter_errors(bundle),
            key=lambda err: list(err.absolute_path),
        )
        assert not errors, (
            "the FIMI transformer's own required output objects must validate "
            "against its output schema; rejected: "
            + "; ".join(
                f"objects[{err.absolute_path[1]}]"
                if len(err.absolute_path) > 1
                else "(root)"
                for err in errors
            )
        )

    def test_output_schema_accepts_an_empty_bundle(self, transformer):
        """No media-content in, empty bundle out — per the hard rules."""
        jsonschema.Draft202012Validator(transformer["output_schema"]).validate(
            {
                "type": "bundle",
                "id": "bundle--11111111-1111-4111-8111-111111111111",
                "spec_version": "2.1",
                "objects": [],
            }
        )

    def test_shares_the_stix_construction_skill(self, transformer):
        skill = next(
            s
            for s in BUILTIN_SKILLS
            if s["slug"] == "opencti-stix-bundle-construction"
        )
        assert _TRANSFORMER_SLUG in skill["bind_to_agent_slugs"], (
            "the FIMI transformer relies on the shared skill's object-level "
            "STIX rules and on its transformation-mode container override to "
            "emit a Case-RFI container instead of a report"
        )

    def test_does_not_compete_with_the_generic_transformer(self, transformer):
        """Same intent, disjoint verticals — no ambiguous resolution."""
        generic = _agent(_GENERIC_TRANSFORMER_SLUG)
        intent = "cti.stix_transformer"
        fimi_verticals = set(transformer["supported_intents"][intent])
        generic_verticals = set(generic["supported_intents"][intent])
        assert fimi_verticals == {"fimi"}
        assert not fimi_verticals & generic_verticals, (
            "both transformers bind cti.stix_transformer; their verticals must "
            "stay disjoint so a playbook resolves exactly one"
        )


class TestViginumDoctrineKnowledgeBase:
    def test_seed_folder_exists_and_has_documents(self):
        kb = _kb(_DOCTRINE_KB_SLUG)
        folder = SEED_DATA_DIR / "knowledge_base" / kb["knowledge_base_seed"]
        assert folder.is_dir(), (
            f"knowledge_base_seed {kb['knowledge_base_seed']!r} must resolve to "
            "a folder under seed_data/knowledge_base/ — the seed pass only logs "
            "a warning when it does not"
        )
        docs = [p for p in folder.iterdir() if p.is_file()]
        assert docs, f"{folder} must contain at least one seed document"
        corpus = "\n".join(p.read_text(encoding="utf-8") for p in docs)
        # The workflow, the relationship graph and the confidence/TLP practice
        # are what the analyst persona and the transformer both defer to.
        for expected in (
            "Narrative",
            "Channel",
            "Event",
            "Intrusion Set",
            "amplifies",
            "attributed-to",
            "confidence",
            "TLP",
        ):
            assert expected.lower() in corpus.lower(), (
                f"the doctrine corpus is missing {expected!r}"
            )

    def test_bound_to_both_fimi_agents(self):
        bound = set(_kb(_DOCTRINE_KB_SLUG)["bind_to_agent_slugs"])
        assert {_ANALYST_SLUG, _TRANSFORMER_SLUG} <= bound

    def test_fimi_agents_are_grounded_in_the_stix_standard(self):
        bound = set(_kb("stix-standard")["bind_to_agent_slugs"])
        assert {_ANALYST_SLUG, _TRANSFORMER_SLUG} <= bound

    def test_analyst_relationship_table_matches_the_doctrine(self):
        """The analyst persona restates the doctrine's relationship graph.

        Two copies of the same table drift: a row added to (or corrected in)
        the doctrine corpus but not the persona leaves the agent working from
        a stale graph while its knowledge base says otherwise — the kind of
        split-brain guidance that is invisible until an analyst gets a wrong
        relationship suggested.
        """
        persona = _resolve_seed_content("agent_persona", _ANALYST_SLUG) or ""
        kb = _kb(_DOCTRINE_KB_SLUG)
        folder = SEED_DATA_DIR / "knowledge_base" / kb["knowledge_base_seed"]
        corpus = "\n".join(
            p.read_text(encoding="utf-8") for p in folder.iterdir() if p.is_file()
        )

        def _relationship_rows(text: str) -> set[tuple[str, str, str]]:
            """Rows of the ``Source | Relationship | Target`` tables only.

            The corpus holds other three-column tables (entity choice, the
            five levels of an operation...), which are not relationships.
            """
            rows: set[tuple[str, str, str]] = set()
            in_table = False
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped.startswith("|"):
                    in_table = False
                    continue
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if cells == ["Source", "Relationship", "Target"]:
                    in_table = True
                    continue
                if not in_table or len(cells) != 3:
                    continue
                source, relationship, target = cells
                # Skip the separator row under the header.
                if set(relationship) <= {"-", ":"}:
                    continue
                rows.add((source, relationship, target))
            return rows

        persona_rows = _relationship_rows(persona)
        doctrine_rows = _relationship_rows(corpus)
        assert persona_rows, "the analyst persona must restate the FIMI relationships"
        assert persona_rows == doctrine_rows, (
            "the analyst persona's relationship table and the doctrine corpus "
            "must stay identical; persona-only rows: "
            f"{sorted(persona_rows - doctrine_rows)}; doctrine-only rows: "
            f"{sorted(doctrine_rows - persona_rows)}"
        )


_PUBLICATIONS_KB_SLUG = "viginum-fimi-publications"


def _seed_docs(kb_slug: str) -> list:
    folder = SEED_DATA_DIR / "knowledge_base" / _kb(kb_slug)["knowledge_base_seed"]
    return sorted(p for p in folder.iterdir() if p.is_file())


class TestViginumPublicationsKnowledgeBase:
    def test_bound_to_the_analyst_and_its_investigator(self):
        assert _kb(_PUBLICATIONS_KB_SLUG)["bind_to_agent_slugs"] == [
            _ANALYST_SLUG,
            "fimi-infrastructure-investigator",
        ], (
            "the case reports ground the analyst and the infrastructure it "
            "pivots on; the transformer is a playbook step that only needs the "
            "method"
        )

    @pytest.mark.parametrize("kb_slug", [_DOCTRINE_KB_SLUG, _PUBLICATIONS_KB_SLUG])
    def test_every_document_names_its_source(self, kb_slug):
        """SGDSN content is reproduced on condition that its source is shown.

        Each document opens with a ``Source :`` line naming SGDSN or VIGINUM
        and a link, so a retrieved chunk can always be traced back.
        """
        docs = _seed_docs(kb_slug)
        assert docs
        unsourced = []
        for doc in docs:
            head = doc.read_text(encoding="utf-8").splitlines()[:8]
            source = [line for line in head if line.startswith("Source :")]
            if len(source) != 1 or not (
                "SGDSN" in source[0] and "https://" in source[0]
            ):
                unsourced.append(doc.name)
        assert unsourced == []

    def test_reports_carry_their_figure_descriptions(self):
        """The reports are only useful with their figures described in text.

        The platform ingests text, so a report whose diagrams and screenshots
        were not described would lose most of its graph knowledge.
        """
        docs = _seed_docs(_PUBLICATIONS_KB_SLUG)
        storm = next(d for d in docs if "storm-1516" in d.name)
        text = storm.read_text(encoding="utf-8")
        assert text.count("> **[Figure ") == 22
        assert text.count("Ce que la figure enseigne") == 22


def test_fimi_agent_references_resolve():
    """Handover and binding slugs must name agents that actually exist.

    The seed pass silently skips unresolvable slugs, so a typo would ship as a
    missing handover edge or an unbound knowledge base rather than an error.
    """
    slugs = {a["slug"] for a in BUILTIN_AGENTS}
    fimi_slugs = {_ANALYST_SLUG, _TRANSFORMER_SLUG}
    dangling: list[str] = []

    for agent in BUILTIN_AGENTS:
        if agent["slug"] not in fimi_slugs:
            continue
        for target in agent.get("handover_agent_slugs", []):
            if target not in slugs:
                dangling.append(f"{agent['slug']}.handover -> {target}")
        for target in agent.get("handover_type_slugs", {}):
            if target not in slugs:
                dangling.append(f"{agent['slug']}.handover_type -> {target}")

    for collection, kind in ((BUILTIN_KNOWLEDGE_BASES, "kb"), (BUILTIN_SKILLS, "skill")):
        for entry in collection:
            for target in entry.get("bind_to_agent_slugs", []):
                if target in fimi_slugs and target not in slugs:
                    dangling.append(f"{kind}:{entry['slug']} -> {target}")

    assert not dangling, f"unresolvable agent references: {dangling}"

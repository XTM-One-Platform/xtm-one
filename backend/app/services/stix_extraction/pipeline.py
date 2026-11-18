# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The grounded extraction pipeline: a report's text in, a STIX 2.1 bundle out.

The method of the research pipeline it reproduces (detect broadly, then disambiguate,
then validate; extract relationships apart from entities so an entity error does not
spread, recovering the entities the relationship step finds), re-shaped around objects
instead of character spans, which fixes the errors that pipeline measured:

1. **Patterns** find identifiers and observables (ATT&CK ids, CVEs, hashes, IPs, domains,
   URLs, e-mail addresses, registry keys) with exact offsets.
2. **Detection** (one call per chunk, in parallel): every entity of the chunk as an object
   with all its surface forms, each a verbatim quote plus the numbered segment it is in,
   marked named or nominal; one or two candidate kinds. Recall first.
3. **Grounding** (code): each quote is found back in its segment. An entity with no
   grounded named mention is dropped (a sector excepted: it is generic by nature).
4. **Consolidation** (code): entities of one family sharing a folded name are one entity,
   across chunks; every occurrence of their names in the document becomes a mention.
5. **Resolution** (one call per family batch, not per entity): keep or drop, the single
   kind, the canonical name trimmed of generic words, aliases, duplicates to merge, and
   for a location its ISO / region code. Mentions narrow to the canonical name
   ("defense sector" becomes "defense").
6. **Relationships** (one call per chunk, in parallel): between *inventory ids*, never
   mentions, so the same name read twice never makes the link ambiguous; a generic
   mention ("the group") is resolved to the entity it refers to while reading; an entity
   the inventory lacks may be added (recovered recall).
7. **Validation** (code): the STIX/OpenCTI relationship matrix keeps, remaps (an
   intrusion set "located at" the country it comes from originates from it) or drops each
   relationship; its evidence must be in the text; duplicates collapse on objects.
8. **Bundle**: standard ids, aliases, location codes, mention offsets, evidence, a report.

Every model call runs on the provider the caller gives (:mod:`.llm`), so the pipeline is
deterministic code around structured calls. With the tool's shipped options a chunk takes
about six to seven of them (detection, relationships, candidate pairs, behaviours and their
technique checks), plus the per-batch steps (resolution, relevance, technique linking): one
report of 67 chunks made 427 calls. ``stats`` counts them per step.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path

from app.services.stix_extraction.bundle import (
    ExtractedObject,
    ExtractedRelation,
    build_bundle,
    default_report_name,
    published_date,
)
from app.services.stix_extraction.grounding import (
    Chunk,
    Segment,
    build_chunks,
    core_key,
    find_occurrences,
    ground,
    split_segments,
    surface_key,
)
from app.services.stix_extraction.iocs import PatternMatch, find_pattern_entities, rendered_link
from app.services.stix_extraction.llm import StructuredCall, Usage
from app.services.stix_extraction.ontology import (
    LLM_KINDS,
    NOMINAL_KINDS,
    RELATED_TO,
    RELATION_TYPES,
    resolve_relation,
    resolve_triple,
)
from app.services.stix_extraction.prompts import (
    BEHAVIOURS_SCHEMA,
    BEHAVIOURS_SYSTEM,
    BEHAVIOURS_USER,
    CONFLICT_NOTE,
    CROSS_CHUNK_SCHEMA,
    CROSS_CHUNK_USER,
    DETECTION_SCHEMA,
    DETECTION_SYSTEM,
    DETECTION_USER,
    GLEANING_USER,
    HUB_SCHEMA,
    HUB_SYSTEM,
    HUB_USER,
    PAIRS_SCHEMA,
    PAIRS_USER,
    RELATION_SCHEMA,
    RELATION_USER,
    RELEVANCE_SCHEMA,
    RELEVANCE_SYSTEM,
    RELEVANCE_USER,
    RESOLUTION_SCHEMA,
    RESOLUTION_USER,
    TECHNIQUE_CHECK_SCHEMA,
    TECHNIQUE_CHECK_SYSTEM,
    TECHNIQUE_CHECK_USER,
    TECHNIQUE_LINK_SCHEMA,
    TECHNIQUE_LINK_SYSTEM,
    TECHNIQUE_LINK_USER,
    TECHNIQUE_REPORT_SCHEMA,
    TECHNIQUE_REPORT_SYSTEM,
    TECHNIQUE_SELECT_SCHEMA,
    TECHNIQUE_SELECT_SYSTEM,
    TECHNIQUE_SELECT_USER,
    TITLE_SCHEMA,
    TITLE_SYSTEM,
    TITLE_USER,
    VERIFICATION_SCHEMA,
    VERIFICATION_USER,
    cross_chunk_system_prompt,
    family_of,
    pairs_system_prompt,
    relation_system_prompt,
    resolution_system_prompt,
    verification_system_prompt,
)

logger = logging.getLogger(__name__)

_RESOLUTION_BATCH = 40
_CONTEXT_CHARS = 120
_MAX_MENTIONS_SHOWN = 6
_LOCATION_KINDS = frozenset({"country", "region", "city"})

#: Name (lowercase) → knowledge-base entries carrying it as name or alias; each entry has
#: ``attack_id``, ``name``, ``stix_type`` and ``aliases``. ``None``: no knowledge base.
KnowledgeLookup = Callable[[list[str]], Awaitable[dict[str, list] | None]]
#: Texts -> their embeddings (the platform's configured embedding model), ``None`` on failure.
Embed = Callable[[list[str]], Awaitable[list[list[float]] | None]]
#: The live techniques behaviours are classified against, each with ``attack_id``,
#: ``name``, ``tactics`` and ``description`` (``mitre_attack.attack_techniques``).
TechniqueCatalogue = Callable[[], Awaitable[list | None]]
#: How the platform the bundle goes to names the objects it already holds: given
#: ``(kind, name)`` pairs (the extraction's kinds), for each name exactly one of its
#: objects of that kind holds as name or alias, that object's ``(name, aliases)``.
#: ``None``: the platform could not be read.
PlatformNames = Callable[[list[tuple[str, str]]], Awaitable[dict[tuple[str, str], tuple[str, list[str]]] | None]]
#: The kinds whose bundle object the platform identifies by its name (a named domain
#: object's standard id is its name): only these can merge into a platform's object
#: by being named as it is. An ATT&CK technique is identified by its id, an observable
#: by its value.
_PLATFORM_NAMED_KINDS = frozenset({
    "intrusion-set", "threat-actor", "campaign", "malware", "tool",
    "sector", "organization", "country", "region", "city",
})
#: A technique verdict as a score (CTI2TTP's rubric: 0.7 is the lower bound of "strong").
_SUPPORT_SCORES = {"explicit": 1.0, "strong": 0.8, "weak": 0.4, "none": 0.0}
#: Technique checks per call.
_TECHNIQUE_CHECK_BATCH = 30

# Which knowledge-base types a candidate of a family may be linked to.
_LINKABLE = {
    "technique": {"attack-pattern"},
    "actor": {"intrusion-set", "campaign"},
    "software": {"malware", "tool"},
}
#: How far (in characters) a technique's name may be from its ATT&CK id to be its name.
_TECHNIQUE_GAP = 40
_VERIFICATION_BATCH = 10
#: Objects per relevance review, segments shown per object, characters of the opening.
_RELEVANCE_BATCH = 40
_RELEVANCE_SEGMENTS = 3
_RELEVANCE_OPENING = 400
#: Characters of the report's opening and objects listed for the activity hub.
_HUB_OPENING = 1500
_HUB_OBJECTS = 150
#: Kinds the activity hub may link to (observables are left to their threats).
_HUB_TARGETS = frozenset({
    "intrusion-set", "threat-actor", "malware", "tool", "attack-pattern", "attack-pattern-id", "infrastructure",
    "channel", "vulnerability", "cve", "sector", "organization", "individual", "country", "region", "city",
})
#: Values a report writes out: facts of the report, never reviewed for relevance.
_WRITTEN_KINDS = frozenset({"cve", "attack-pattern-id", "md5", "sha1", "sha256", "sha512",
                            "file-name", "windows-registry-key"})
#: Addresses a report writes out, reviewed like names: a public resolver, a registrar's abuse
#: address or a vendor's server is written out too, and is no indicator.
_ADDRESS_KINDS = frozenset({"ipv4-addr", "ipv6-addr", "email-addr"})
_WEB_ADDRESS_KINDS = frozenset({"domain-name", "url"})
#: Segments of context a verifier sees per candidate (CTI2TTP's window).
_VERIFICATION_WINDOW = 5
#: A re-typing is taken only from a verdict at least this sure.
_RETYPE_MIN_SCORE = 0.7
#: The kinds the cross-chunk pass reads: who acts, with what, against whom and where. The
#: observables and ATT&CK ids are left to the per-chunk steps and the page's layout.
_CROSS_CHUNK_KINDS = frozenset({
    "intrusion-set", "threat-actor", "campaign", "malware", "tool", "infrastructure", "sector", "organization",
    "individual", "country", "region", "city", "vulnerability", "cve",
})
#: Pairs decided per cross-chunk call, pairs read per report (the first, a threat at one
#: end, when there are more), sentences shown per entity and characters per sentence.
_CROSS_CHUNK_BATCH = 40
_CROSS_CHUNK_MAX_PAIRS = 400
_CROSS_CHUNK_SENTENCES = 2
_CROSS_CHUNK_SENTENCE_CHARS = 300


# A list item: a bullet ("-", "*", "•", "–") or an enumeration ("1.", "2)", "a.") opens it.
_LIST_ITEM = re.compile(r"(?:[-*•–·]|\d{1,3}[.)]|[a-z][.)])\s")
#: Characters that make a name touching them part of a value (a path, a file name, an
#: address): ``~/Library/CoreKit/CoreKitAgent`` names a file, not the malware.
_VALUE_JOINERS = frozenset("/\\._:@=")
#: The threats a file name may indicate by layout: what is malicious in itself, not a tool or a
#: platform a file belongs to.
_MALICIOUS_KINDS = frozenset({"malware", "intrusion-set", "threat-actor", "campaign"})
#: Lines without observables a list's heading carries over before its list.
_HEADING_CARRY = 2


@dataclass(frozen=True)
class ExtractionOptions:
    """How the pipeline reads a document."""

    chunk_chars: int = 2_000
    #: The chunks the ATT&CK behaviours are read from (``behaviour_techniques``); 0 is
    #: ``chunk_chars``. A smaller passage asks the model about fewer behaviours at once.
    behaviour_chunk_chars: int = 0
    #: Relationships are read over wider chunks: a link often spans two sentences.
    relation_chunk_chars: int = 3_000
    #: Detection calls per chunk. A model that cannot read at temperature 0 finds a
    #: different subset of a report's entities each time (10 one run, 46 the next); the
    #: union of several readings, judged by resolution as any detected entity, steadies
    #: recall, each extra reading costing one more detection call per chunk. The first
    #: reading is the call a single sample makes; the others are told apart by their step
    #: name (``detection_2``...) so a recorded answer is not replayed for them.
    detection_samples: int = 1
    include_observables: bool = True
    include_report: bool = True
    include_evidence: bool = True
    #: Relationships the text implies without stating them ("the group targets defense
    #: with X": X targets defense). Kept with a lower STIX confidence by default; the
    #: research ground truth annotates stated ones only.
    include_implied: bool = True
    #: Keep the report's knowledge, not every mention: an object no relationship touches is
    #: left out, except an IOC observable (an address, a hash, a file...), which is knowledge
    #: alone. Fits a gold that records what the report establishes (the XTM One golden
    #: dataset); a gold that annotates every mention (MAR) wants it off. Off by default (§8.8).
    knowledge_focus: bool = False
    #: Keep the report's knowledge by reading, not by structure: one review per batch of
    #: objects, each shown with the sentences naming it and the links found for it, keeps
    #: the objects with a role in the activity and leaves out the names that only frame the
    #: report (publisher, cited sources, products named in passing, page links). The values
    #: a report writes out (hashes, addresses, CVE and ATT&CK ids) are not reviewed. Off here
    #: for the benchmarks' comparability; the extraction tool turns it on (§8.9).
    relevance_review: bool = False
    #: The relevance review also reads the domains and URLs the patterns found, as it
    #: reads addresses: a page's own links, its references and a vendor's site are written
    #: out like an indicator and are none.
    relevance_web_addresses: bool = False
    #: Keep only the attack patterns ATT&CK knows (an id the report writes, or a name
    #: linked to one): a pattern known only by its words ("network reconnaissance"), a
    #: CAPEC entry a CVE page lists, is left out with its relationships. Needs the technique
    #: catalogue; without it nothing is left out.
    attack_techniques_only: bool = False
    #: Name a product as analysts and OpenCTI do: one product per item of an enumeration
    #: ("JNC IAQS and I6"), the vendor the text writes before it in its name and ``vendor``,
    #: and its update, build or release in ``version``, not in the name ("Microsoft Exchange
    #: Server 2016" with version "Cumulative Update 23").
    product_names: bool = False
    #: A ransomware brand the text calls ransomware ("BianLian ransomware") is both the group
    #: that runs it and its malware: an intrusion set that uses a malware of type ransomware.
    ransomware_twins: bool = False
    #: Read names against the MISP galaxy's threat actors, ransomware and tools: two
    #: objects of one kind whose names are one cluster's ("Secret Blizzard" and "Turla") are
    #: one object, the cluster's name kept as an alias OpenCTI can match on.
    galaxy_aliases: bool = False
    #: Name a sector as OpenCTI's sector dataset does ("finance", "Financial Services" →
    #: "Finance"), the text's name kept as an alias: one name for one sector across reports.
    sector_names: bool = False
    #: Techniques from behaviour also take, for each behaviour, the ATT&CK techniques whose
    #: definitions are nearest its quote by embedding (CTI2TTP, MEvince: retrieval widens
    #: what the model proposes from memory); every one is still checked against its
    #: definition. 0: none. Needs an ``embed`` function.
    technique_retrieval_k: int = 0
    #: Which of one behaviour's checked candidates to keep: "each" every one at the threshold
    #: (one action can be several techniques); "family" at most one per ATT&CK technique, its
    #: best supported sub-technique or itself (a sibling or the parent of a kept one is the
    #: same action named twice); "best" only the best supported, then one per family.
    technique_choice: str = "each"
    #: A last, listwise reading of the report's techniques (choosing among candidates beats
    #: judging each alone: Wang et al., COLING 2025; Code Like Humans, 2509.05378): one call
    #: per report compares them, each with its definition and quotes, and keeps those it
    #: selects. "off"; "kept" among those the check kept; "candidates" among every candidate
    #: checked at least weak.
    technique_selection: str = "off"
    #: The technique check's readings, each the length of the candidate's ATT&CK definition it
    #: reads (most run past 700 characters: the procedures that tell a technique, "netstat",
    #: "nltest", come later); a candidate takes its best verdict over the readings. Two
    #: readings of different lengths disagree where one is unsure: kept if either keeps it.
    technique_check_views: tuple[int, ...] = (700,)
    #: Each object's confidence graded by what supports it (``bundle.object_confidence``)
    #: instead of one value for all, so an analyst or an import filters by it.
    graded_confidence: bool = False
    #: A list of indicators under a heading that names no threat ("Indicators of
    #: Compromise") indicates the report's main subject when it has exactly one, as an
    #: event's indicators are its threat's in MISP and OpenCTI. Implied.
    ioc_lists_main_subject: bool = False
    #: A threat that targets a city or a region of a country also targets the country:
    #: victimology is read by country. Implied, with the place's evidence.
    victim_countries: bool = False
    #: Link a technique the report names in words ATT&CK does not use ("DLL side-loading",
    #: "vishing") to the ATT&CK technique it is: one reading per batch, against the
    #: technique catalogue, which answers an id or none (a tactic, a broad goal, a
    #: vulnerability class). Needs the catalogue (``techniques``).
    technique_linking: bool = False
    #: The report's main activity when no object names it: one reading of the report's
    #: opening and the kept objects gives an incident (one victim) or a campaign with a
    #: descriptive name, linked to the actors, means and victims the report states for it
    #: (how an analyst records a report on an unnamed operation in OpenCTI). Off by default
    #: until measured on a held-out gold (§8.8).
    activity_hub: bool = False
    #: Keep what the report states that the ontology has no verb for: a link quoted in the
    #: report between two kinds no verb joins (a persona fronting a group, a unit under a
    #: state organization) as STIX's related-to, and a name the report declares another
    #: name of an entity merged into it. Recovers stated knowledge OpenCTI would lose, at
    #: a measured cost in precision against the human golds, which rarely annotate those
    #: links: off by default (§8.8).
    stated_links: bool = False
    #: Check every relationship against its evidence sentences and the relationship
    #: definitions (one call per batch of 30), dropping or re-typing it and giving it a
    #: confidence: the verification that carried the precision of the research
    #: pipelines that did it against definitions and evidence (CTI2TTP), unlike generic
    #: self-checking. Off until measured on held-out data.
    verify_relations: bool = False
    #: A relationship the verifier judges not supported is dropped only below this score;
    #: an unsure or mid-scored one is kept with its score as STIX confidence.
    verify_drop_below: float = 0.3
    #: Benchmarking: drop nothing, give every verified relationship its score (thresholds
    #: are then swept offline).
    verify_score_only: bool = False
    #: A second relationship call per chunk listing what the first found and asking for
    #: what it missed (GraphRAG's gleaning, on relationships); its additions are verified
    #: when verification is on.
    relation_gleaning: bool = False
    #: A recall pass over the pairs of entities named in the same or neighbouring
    #: segments that no relationship links yet: one decision per pair, kept only with a
    #: quote of the chunk that states it (BEACON's propose-then-confirm). On DEV: T3 +2.4,
    #: T4 +2.6 (CIs above zero), relatedness precision 97.3, ~12% more tokens.
    relation_pairs: bool = True
    #: Pairs decided per call (a longer list is split, never cut), and a bound on the pairs
    #: of one chunk (a 70-item list beside its malware is 70 pairs).
    #: Relatedness-oriented pair pass: every pair of the chunk is a candidate (not only
    #: neighbouring segments) and a link the chunk describes in one activity may be kept as
    #: related-to. Off by default: measured as an operating point (§6.13).
    relatedness: bool = False
    #: The report's main subject (§6.13) is a candidate pair with every entity of a chunk,
    #: wherever it is named; a generic mention of it counts as naming it.
    central_subject: bool = True
    #: Each candidate pair lists the relationships the ontology allows between its two
    #: entities as directed sentences ("A uses B", "B targets A") and "none", the
    #: multiple-choice form of QA4RE (2305.11159), instead of the matrix alone.
    pair_options: bool = False
    #: Relationships between two entities the report names in different relationship
    #: chunks, which the per-chunk steps never see together (12.5% of a human gold's
    #: relationships). Each entity is shown with the sentences naming it, and each pair
    #: of them that no chunk holds and nothing links yet is decided, kept only with a quote
    #: of the report and a triple the ontology allows, implied unless the quote names both.
    #: Off until measured on held-out data.
    cross_chunk_relations: bool = False
    max_pairs_per_call: int = 40
    max_pairs_per_chunk: int = 400
    #: Indicators tied to a threat by the layout of the report, in code: a table row naming
    #: one threat beside its hashes, a list of observables under a heading naming one.
    structural_indicators: bool = True
    #: The ATT&CK techniques a report's layout ties to a group or a software (a mapping
    #: table's row "S0469 ABK  Application Layer Protocol, Command and Scripting...", a
    #: list of techniques under a heading or in a report on one subject), which a model
    #: reading prose leaves unlinked. No model call.
    structural_techniques: bool = False
    #: ATT&CK techniques from described behaviour (CTI2TTP's propose-then-verify): per
    #: chunk, the behaviours with a quote and up to three candidate techniques from the
    #: catalogue; each candidate checked against its definition and kept at or above
    #: ``technique_threshold``. Needs a technique catalogue; off until measured (§8.8).
    behaviour_techniques: bool = False
    technique_threshold: float = 0.7
    #: Which of the stage's techniques to keep (each behaviour-derived one; a technique the
    #: report writes by id is always kept): "recall" every one that passes its check;
    #: "precision" only one a whole-report reading also finds (parent level); "balanced" one
    #: that reading finds, or that has an explicit verdict, or two or more checked quotes.
    #: Chosen on DEV-T, DEV-C and DEV-X (§8.8): both corroborated modes beat the stage and the
    #: whole-report reading alone on every curated gold; "recall" suits a complete gold.
    technique_mode: str = "recall"
    max_chars: int = 400_000



def _one_reading(kept: list[int], proposals: list, scores: dict[int, float], choice: str) -> list[int]:
    """The kept candidates of each behaviour (one quote span) that ``technique_choice`` keeps:
    "family" the best supported of each ATT&CK technique family (a tie goes to the one
    proposed first, the proposer's most likely); "best" those at the behaviour's top score,
    then one per family. "each" keeps them all."""
    if choice not in ("family", "best"):
        return kept
    by_behaviour: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i in kept:
        by_behaviour[proposals[i][1]].append(i)
    result: set[int] = set()
    for indexes in by_behaviour.values():
        if choice == "best":
            top = max(scores.get(i, 0.0) for i in indexes)
            indexes = [i for i in indexes if scores.get(i, 0.0) == top]
        families: dict[str, int] = {}
        for i in sorted(indexes, key=lambda i: (-scores.get(i, 0.0), i)):
            families.setdefault(proposals[i][4].split(".")[0], i)
        result.update(families.values())
    return sorted(result)

@dataclass
class _Candidate:
    ref: str
    kinds: list[str]
    name: str
    family: str
    spans: list[tuple[int, int]] = field(default_factory=list)
    surfaces: list[str] = field(default_factory=list)
    anchored: bool = False
    value: str = ""
    aliases: list[str] = field(default_factory=list)
    code: str = ""
    recovered: bool = False
    external_ids: list[tuple[str, str]] = field(default_factory=list)
    vendor: str = ""
    version: str = ""
    malware_types: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.kinds[0] if self.kinds else "unknown"

    def name_keys(self) -> set[str]:
        """Every folded form of its names, with and without qualifying words."""
        names = (self.name, *self.surfaces, *self.aliases)
        return {key for name in names for key in (surface_key(name), core_key(name)) if key}


@dataclass
class ExtractionResult:
    """The bundle and what it took to build it."""

    bundle: dict
    objects: list[ExtractedObject]
    relations: list[ExtractedRelation]
    stats: dict
    #: What each step left, for scoring the steps one by one as the research board did
    #: (entities after detection, after resolution, final; relationships as the model
    #: gave them, then after validation). Not part of the bundle.
    stages: dict[str, list] = field(default_factory=dict)
    #: The report's name: the document's, else :func:`.bundle.default_report_name`.
    document_name: str = ""


class _Pipeline:
    #: Texts -> their embeddings, for the retrieval options; ``None`` without one.
    embed: Embed | None = None

    def __init__(self, text: str, call: StructuredCall, usage: Usage, options: ExtractionOptions) -> None:
        self.text = text
        self.call = call
        self.usage = usage
        self.options = options
        self.segments: list[Segment] = split_segments(text)
        self.stats: dict[str, int] = defaultdict(int)
        self._next = 0
        self.raw_relations: list[ExtractedRelation] = []
        self.off_ontology: list[tuple[str, str, str]] = []
        #: Links and addresses the report cites (references, contacts), not indicators.
        self.cited: list[str] = []
        self._other_readings: dict[str, set[str]] = {}
        #: Every technique candidate with its verdict score (thresholds swept offline).
        self.technique_scores: list[dict] = []
        #: The relationship chunks that name an entity, and the pairs the pair pass decided.
        self.relation_chunks: list[Chunk] = []
        #: ATT&CK id -> the names a report writes it by (its name, "Parent: Sub"), for
        #: the technique tables (``structural_techniques``); empty without a catalogue.
        self.technique_names: dict[str, tuple[str, ...]] = {}
        self._paired: set[frozenset[str]] = set()

    def _ref(self, prefix: str = "C") -> str:
        self._next += 1
        return f"{prefix}{self._next}"

    def _segment_window(self, index: object, chunk: Chunk) -> tuple[int, int]:
        if isinstance(index, int) and 0 <= index < len(self.segments):
            segment = self.segments[index]
            return segment.start, segment.end
        return chunk.start, chunk.end

    # 1. patterns ---------------------------------------------------------------------

    def pattern_candidates(self) -> list[_Candidate]:
        grouped: dict[tuple[str, str], list[PatternMatch]] = defaultdict(list)
        for match in find_pattern_entities(self.text):
            if match.cited:
                if match.kind == "url" and match.value not in self.cited:
                    self.cited.append(match.value)
                self.stats["pattern_cited"] += 1
                continue
            if not self.options.include_observables and match.kind not in ("attack-pattern-id", "cve"):
                continue
            grouped[(match.kind, match.value)].append(match)
        candidates = []
        for (kind, value), matches in grouped.items():
            candidates.append(
                _Candidate(
                    ref=self._ref("P"),
                    kinds=[kind],
                    name=value,
                    family="pattern",
                    spans=[(m.start, m.end) for m in matches],
                    surfaces=[value],
                    anchored=True,
                    value=value,
                )
            )
        self.stats["pattern_entities"] = len(candidates)
        return candidates

    # 2-3. detection and grounding -------------------------------------------------------

    async def detect(self, chunks: list[Chunk], anchors: list[tuple[int, int]]) -> list[_Candidate]:
        samples = max(1, self.options.detection_samples)
        prompts = [DETECTION_USER.format(chunk=_untrusted(chunk)) for chunk in chunks]
        answers = await asyncio.gather(
            *(
                self.call(step=_sample_step("detection", sample), system=DETECTION_SYSTEM, user=prompt,
                          schema=DETECTION_SCHEMA)
                for sample in range(samples)
                for prompt in prompts
            )
        )
        readings = [answers[sample * len(chunks) : (sample + 1) * len(chunks)] for sample in range(samples)]
        self.stats["detection_chunks"] = len(chunks)
        self.stats["detection_samples"] = samples
        # A chunk is read when one of its samples answered.
        self.stats["failed_detection_chunks"] = sum(
            all(reading[i] is None for reading in readings) for i in range(len(chunks))
        )
        # The first reading first: consolidation keeps its candidate as the head of a merge.
        per_sample: list[list[_Candidate]] = []
        for reading in readings:
            found: list[_Candidate] = []
            for chunk, answer in zip(chunks, reading, strict=True):
                for entity in (answer or {}).get("entities", []):
                    candidate = self._grounded_candidate(entity, chunk, anchors)
                    if candidate is not None:
                        found.append(candidate)
            per_sample.append(found)
        if samples > 1:
            self.stats["failed_detection_samples"] = sum(answer is None for answer in answers)
            self.stats["detection_sample_additions"] = _added_by_others(per_sample)
        candidates = [candidate for found in per_sample for candidate in found]
        self.stats["detected_entities"] = len(candidates)
        return candidates

    def _grounded_candidate(self, entity: dict, chunk: Chunk, anchors: list[tuple[int, int]]) -> _Candidate | None:
        kinds = [k for k in entity.get("kinds") or [] if isinstance(k, str)][:2] or ["unknown"]
        spans: list[tuple[int, int]] = []
        surfaces: list[str] = []
        named = False
        for mention in entity.get("mentions") or []:
            quote = str(mention.get("text") or "").strip()
            if not quote:
                continue
            span = ground(self.text, quote, self._segment_window(mention.get("segment"), chunk))
            if span is None:
                self.stats["ungrounded_mentions"] += 1
                continue
            if any(span[0] < end and start < span[1] for start, end in anchors):
                continue  # an identifier the patterns own
            if mention.get("level") == "nominal" and not set(kinds) & NOMINAL_KINDS:
                continue  # "the group": a coreference clue, not a name
            named = True
            spans.append(span)
            surfaces.append(self.text[span[0] : span[1]])
        if not named:
            self.stats["dropped_unnamed_or_ungrounded"] += 1
            return None
        name = str(entity.get("name") or "").strip()
        if not name or ground(self.text, name, (chunk.start, chunk.end)) is None:
            name = " ".join(surfaces[0].split())
        return _Candidate(self._ref(), kinds, name, family_of(kinds[0]), spans, surfaces)

    def complete_enumerations(self, candidates: list[_Candidate], anchors: list[tuple[int, int]]) -> list[_Candidate]:
        """Every item of an enumeration the model sampled from: a list of 70 process names
        or 12 countries is read whole by a reader, while a model returns a varying subset
        of it (33 of 70 one run, 60 the next). When at least three items of one list were
        detected and two thirds of them share a kind, the list's other items become
        candidates of that kind; resolution still judges each one."""
        added: list[_Candidate] = []
        for items in _enumerations(self.text):
            covered: dict[int, str] = {}
            for index, (start, end) in enumerate(items):
                for candidate in candidates:
                    if any(start <= a and b <= end for a, b in candidate.spans):
                        covered[index] = candidate.kind
                        break
            if len(covered) < 3:
                continue
            kind, count = max(((k, list(covered.values()).count(k)) for k in set(covered.values())), key=lambda x: x[1])
            if kind == "unknown" or count * 3 < len(covered) * 2:
                continue
            for index, (start, end) in enumerate(items):
                if index in covered or any(start < b and a < end for a, b in anchors):
                    continue
                name = self.text[start:end]
                added.append(_Candidate(self._ref(), [kind], name, family_of(kind), [(start, end)], [name]))
        self.stats["enumeration_items_added"] = len(added)
        return [*candidates, *added]

    # 4. consolidation ----------------------------------------------------------------

    @staticmethod
    def consolidate(candidates: list[_Candidate]) -> list[_Candidate]:
        """One candidate per (family, folded name), across chunks (union-find)."""
        parent = list(range(len(candidates)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        owner: dict[tuple[str, str], int] = {}
        for index, candidate in enumerate(candidates):
            for key in candidate.name_keys():
                slot = (candidate.family, key)
                if slot in owner:
                    parent[find(index)] = find(owner[slot])
                else:
                    owner[slot] = index
        groups: dict[int, list[_Candidate]] = defaultdict(list)
        for index, candidate in enumerate(candidates):
            groups[find(index)].append(candidate)
        merged = []
        for members in groups.values():
            head = members[0]
            kinds: list[str] = []
            for member in members:
                kinds.extend(k for k in member.kinds if k not in kinds)
            # The kind most members agree on first.
            kinds.sort(key=lambda k: -sum(k in m.kinds[:1] for m in members))
            head.kinds = kinds[:2]
            head.spans = sorted({span for m in members for span in m.spans})
            head.surfaces = list(dict.fromkeys(s for m in members for s in m.surfaces))
            head.name = max((m.name for m in members), key=lambda n: (sum(n == m.name for m in members), len(n)))
            merged.append(head)
        return merged

    def propagate(self, candidates: list[_Candidate], anchors: list[tuple[int, int]]) -> None:
        """Every occurrence of a candidate's names in the document is one of its mentions."""
        for candidate in candidates:
            if candidate.anchored:
                continue
            spans = set(candidate.spans)
            for surface in {candidate.name, *candidate.surfaces}:
                for start, end in find_occurrences(self.text, surface):
                    if any(start < a_end and a_start < end for a_start, a_end in anchors):
                        continue
                    found = self.text[start:end]
                    # "US" the country is never "us" the pronoun.
                    if candidate.kind in _LOCATION_KINDS and found.split() != surface.split():
                        continue
                    spans.add((start, end))
            candidate.spans = sorted(spans)

    # 5. resolution -------------------------------------------------------------------

    def _card(self, candidate: _Candidate) -> str:
        contexts = []
        for start, end in candidate.spans[:2]:
            left = max(0, start - _CONTEXT_CHARS)
            right = min(len(self.text), end + _CONTEXT_CHARS)
            contexts.append(" ".join(self.text[left:right].split()))
        mentions = "; ".join(candidate.surfaces[:_MAX_MENTIONS_SHOWN])
        card = (
            f"- id: {candidate.ref}\n  kinds: {', '.join(candidate.kinds)}\n  name: {candidate.name}\n"
            f"  mentions: {mentions}\n  contexts: " + " || ".join(contexts)
        )
        other = sorted(self._other_readings.get(candidate.ref, ()))
        return card + ("\n" + CONFLICT_NOTE.format(kinds=", ".join(other)) if other else "")

    def _note_other_readings(self, candidates: list[_Candidate]) -> None:
        """Remember, per candidate, the kinds its name got in other families: "Qilin" read
        as an intrusion set in one chunk and as malware in another is shown to resolution
        as one decision to make, not two silent ones."""
        by_key: dict[str, set[str]] = defaultdict(set)
        for candidate in candidates:
            for key in candidate.name_keys():
                by_key[key].add(candidate.kind)
        self._other_readings = {
            c.ref: {k for key in c.name_keys() for k in by_key[key]} - {c.kind} for c in candidates
        }

    async def resolve(self, candidates: list[_Candidate]) -> list[_Candidate]:
        self._note_other_readings(candidates)
        by_family: dict[str, list[_Candidate]] = defaultdict(list)
        for candidate in candidates:
            by_family[candidate.family].append(candidate)
        batches = [
            (family, members[i : i + _RESOLUTION_BATCH])
            for family, members in sorted(by_family.items())
            for i in range(0, len(members), _RESOLUTION_BATCH)
        ]
        answers = await asyncio.gather(
            *(
                self.call(
                    step="resolution",
                    system=resolution_system_prompt(family, self.options.stated_links),
                    user=RESOLUTION_USER.format(
                        kinds=", ".join(sorted({k for m in batch for k in m.kinds} | _family_kinds(family))),
                        candidates="\n".join(self._card(m) for m in batch),
                    ),
                    schema=RESOLUTION_SCHEMA,
                )
                for family, batch in batches
            )
        )
        by_ref = {c.ref: c for c in candidates}
        merge_into: dict[str, str] = {}
        decided: set[str] = set()
        dropped: set[str] = set()
        self.stats["failed_resolution_batches"] += sum(answer is None for answer in answers)
        for (_family, batch), answer in zip(batches, answers, strict=True):
            refs = {m.ref for m in batch}
            for decision in (answer or {}).get("decisions", []):
                ref = decision.get("id")
                if ref not in refs:
                    continue
                decided.add(ref)
                candidate = by_ref[ref]
                kind = decision.get("kind")
                target = decision.get("merge_into")
                # A duplicate is merged whatever "keep" says: its mentions are evidence.
                if isinstance(target, str) and target in refs and target != ref:
                    merge_into[ref] = target
                    continue
                if not decision.get("keep") or kind == "unknown":
                    dropped.add(ref)
                    continue
                # Any kind may be corrected, across families too (a group detected as
                # malware, an "unknown" that is a sector).
                if isinstance(kind, str) and kind in LLM_KINDS:
                    candidate.kinds = [kind]
                    candidate.family = family_of(kind)
                self._rename(candidate, str(decision.get("name") or ""))
                candidate.aliases = [
                    a for a in (str(x) for x in decision.get("aliases") or [])
                    if a and ground(self.text, a, None) is not None
                ]
                candidate.code = str(decision.get("code") or "")
        # A candidate the model skipped keeps its first kind, as detected, unless that
        # kind is "unknown": nothing says what it is. A name made of qualifying words
        # only ("the group", "the malware") names nothing, whatever the model said.
        kept = [
            c for c in candidates
            if c.ref not in dropped and (c.ref in decided or c.kind != "unknown") and core_key(c.name)
        ]
        self.stats["resolution_dropped"] += len(dropped)
        self.stats["resolution_undecided"] += len([c for c in kept if c.ref not in decided])
        return self._apply_merges(kept, merge_into)

    def _rename(self, candidate: _Candidate, name: str) -> None:
        """Adopt the canonical *name* when it is in the text, narrowing each mention that
        contains it to it ("defense sector" → "defense")."""
        name = " ".join(name.split())
        if not name or name == candidate.name or ground(self.text, name, None) is None:
            return
        narrowed: list[tuple[int, int]] = []
        for start, end in candidate.spans:
            inner = find_occurrences(self.text, name, start, end)
            narrowed.append(inner[0] if inner else (start, end))
        candidate.spans = sorted(set(narrowed))
        # A surface that contains the name as words was the name plus generic words: it goes
        # ("defense sector"); one that only contains its letters is another word, kept
        # ("healthcare" for "health": the report's own word for the sector).
        candidate.surfaces = list(
            dict.fromkeys([name, *(s for s in candidate.surfaces if not find_occurrences(s, name))])
        )
        candidate.name = name

    @staticmethod
    def _apply_merges(candidates: list[_Candidate], merge_into: dict[str, str]) -> list[_Candidate]:
        by_ref = {c.ref: c for c in candidates}

        def root(ref: str) -> str:
            seen = set()
            while ref in merge_into and ref not in seen and merge_into[ref] in by_ref:
                seen.add(ref)
                ref = merge_into[ref]
            return ref

        result = []
        for candidate in candidates:
            target = root(candidate.ref)
            if target == candidate.ref:
                result.append(candidate)
                continue
            head = by_ref[target]
            head.spans = sorted(set(head.spans) | set(candidate.spans))
            head.aliases = list(dict.fromkeys([*head.aliases, candidate.name, *candidate.aliases]))
            head.surfaces = list(dict.fromkeys([*head.surfaces, *candidate.surfaces]))
        return result

    def link_technique_names(self, candidates: list[_Candidate], patterns: list[_Candidate]) -> list[_Candidate]:
        """A technique named in words beside its ATT&CK id ("the application layer
        protocol attack pattern [T1071]") is that id's object: its words become the
        object's name (the id stays its ``x_mitre_id``), not a second attack pattern."""
        return self._link_names_to_ids(
            self._link_names_to_ids(candidates, patterns, "attack-pattern", "attack-pattern-id"),
            patterns, "vulnerability", "cve",
        )

    def _link_names_to_ids(
        self, candidates: list[_Candidate], patterns: list[_Candidate], name_kind: str, id_kind: str
    ) -> list[_Candidate]:
        """Pair names of *name_kind* with the ids of *id_kind* written beside them (a
        technique and its ATT&CK id, "Log4Shell (CVE-2021-44228)")."""
        ids = [p for p in patterns if p.kind == id_kind]
        names = [c for c in candidates if c.kind == name_kind]
        # Every (name, id) pair close in one segment, ranked: an id written after its
        # name first ("scripting attack pattern [T1064]", "Phishing (T1566)"), the
        # closest first; then paired one-to-one, so "obfuscated files [T1027] and the
        # scripting [T1064]" gives each name its own id.
        pairs: list[tuple[int, int, int, int]] = []
        for n, candidate in enumerate(names):
            for i, technique in enumerate(ids):
                for start, end in candidate.spans:
                    for t_start, t_end in technique.spans:
                        if not self._same_segment(start, t_start):
                            continue
                        after = t_start >= end
                        gap = t_start - end if after else start - t_end
                        if 0 <= gap <= _TECHNIQUE_GAP:
                            pairs.append((0 if after else 1, gap, n, i))
        linked_names: set[int] = set()
        linked_ids: set[int] = set()
        for _, _, n, i in sorted(pairs):
            if n in linked_names or i in linked_ids:
                continue
            linked_names.add(n)
            linked_ids.add(i)
            identified = ids[i]
            if id_kind == "attack-pattern-id":
                identified.name = names[n].name  # the id stays its x_mitre_id
            else:
                identified.aliases = list(dict.fromkeys([*identified.aliases, names[n].name]))
            identified.spans = sorted(set(identified.spans) | set(names[n].spans))
            self.stats[f"{name_kind}_names_linked"] += 1
        linked = {names[n].ref for n in linked_names}
        return [c for c in candidates if c.ref not in linked]

    async def link_knowledge(
        self, candidates: list[_Candidate], patterns: list[_Candidate], lookup: KnowledgeLookup
    ) -> list[_Candidate]:
        """Link names to a knowledge base (ATT&CK): a technique written in words gets its
        id, a group or a software its ATT&CK id, canonical name and aliases, and software
        the type ATT&CK gives it (malware or tool). Only an unambiguous match of a type the
        candidate's family allows is taken ("PowerShell" the tool never becomes T1059.001);
        the aliases kept are the text's, never the base's whole list."""
        linkable = [c for c in candidates if c.family in _LINKABLE]
        names = sorted({
            v for c in linkable for n in (c.name, *c.aliases, *c.surfaces) if n.strip() for v in (n, *_bare_forms(n))
        })
        if not names:
            return candidates
        try:
            found = await lookup(names)
        except Exception as exc:  # a knowledge base is an enrichment, never a failure
            logger.warning("knowledge lookup failed: %s", exc)
            found = None
        if not found:
            self.stats["knowledge_unavailable"] = 1 if found is None else 0
            return candidates
        by_id = {p.value: p for p in patterns if p.kind == "attack-pattern-id"}
        matches: dict[int, object] = {}
        for index, candidate in enumerate(candidates):
            written = (candidate.name, *candidate.aliases, *candidate.surfaces)
            entries = {
                (e.attack_id, e.stix_type): e
                for n in written
                for e in found.get(n.strip().lower(), [])
                if e.stix_type in _LINKABLE.get(candidate.family, ())
            }
            if not entries:
                # Only when the base knows no written form: its bare form ("Backdoor.X",
                # "X 3.0" → "X"); the base itself names some families so (LockBit 3.0).
                entries = {
                    (e.attack_id, e.stix_type): e
                    for n in written
                    for bare in _bare_forms(n)
                    for e in found.get(bare.lower(), [])
                    if e.stix_type in _LINKABLE.get(candidate.family, ())
                }
            if len({attack_id for attack_id, _ in entries}) == 1:
                matches[index] = next(iter(entries.values()))
        # Names the text keeps apart that a knowledge base files under one id (FIN12 and
        # Wizard Spider) stay apart: the one the base names so, else the most mentioned,
        # takes the link. Aliases are merge keys in OpenCTI, and catalogues lump groups.
        heads: dict[str, int] = {}
        for index, entry in matches.items():
            candidate, current = candidates[index], heads.get(entry.attack_id)
            if current is None:
                heads[entry.attack_id] = index
                continue
            def rank(i: int) -> tuple[bool, int]:
                names = {n.strip().lower() for n in (candidates[i].name, *candidates[i].surfaces)}
                return (matches[i].name.lower() in names, len(candidates[i].spans))
            if rank(index) > rank(current):
                heads[entry.attack_id] = index
        kept: list[_Candidate] = []
        for index, candidate in enumerate(candidates):
            entry = matches.get(index)
            if entry is None:
                kept.append(candidate)
                continue
            if heads[entry.attack_id] != index and entry.stix_type != "attack-pattern":
                self.stats["knowledge_kept_apart"] += 1
                kept.append(candidate)
                continue
            self.stats["knowledge_linked"] += 1
            if entry.stix_type == "attack-pattern":
                target = by_id.get(entry.attack_id)
                if target is not None:  # the id is written in the text too: one object
                    target.spans = sorted(set(target.spans) | set(candidate.spans))
                    if target.name == target.value:
                        target.name = entry.name
                    continue
                if heads[entry.attack_id] != index:  # two wordings of one technique: one object
                    head = candidates[heads[entry.attack_id]]
                    head.spans = sorted(set(head.spans) | set(candidate.spans))
                    continue
                candidate.kinds, candidate.value = ["attack-pattern-id"], entry.attack_id
            else:
                candidate.kinds = [entry.stix_type]
                candidate.external_ids = [("mitre-attack", entry.attack_id)]
            # The base's name, and the names the text gives as aliases: never the base's
            # whole alias list, each of which OpenCTI would match and merge on.
            text_names = [candidate.name, *candidate.aliases]
            candidate.name = entry.name
            candidate.aliases = [n for n in dict.fromkeys(text_names) if n != entry.name]
            kept.append(candidate)
        return kept

    async def link_techniques(
        self, candidates: list[_Candidate], patterns: list[_Candidate], catalogue: list
    ) -> list[_Candidate]:
        """A technique still unlinked after the exact lookup, named in words ATT&CK does not
        use, gets the ATT&CK technique a reading of its passage answers against the
        catalogue (``technique_linking``); "none" leaves it as written. A linked technique
        takes ATT&CK's id and name and keeps the report's words as aliases; two wordings of
        one technique, or a wording of an id the text writes, become one object."""
        from app.services.content_safety import wrap_untrusted

        unlinked = [c for c in candidates if c.family == "technique" and c.kind == "attack-pattern"]
        if not unlinked:
            return candidates
        by_tid = {t.attack_id.upper(): t for t in catalogue}
        listing = "\n".join(
            f"{'  ' if '.' in t.attack_id else ''}{t.attack_id} {t.name}"
            + ("" if "." in t.attack_id else f" [{', '.join(t.tactics)}]")
            for t in catalogue
        )
        system = TECHNIQUE_LINK_SYSTEM.format(catalogue=listing)

        def card(index: int, candidate: _Candidate) -> str:
            named = sorted({self._segment_of(start) for start, _ in candidate.spans} - {None})[:2]
            passage = " ".join(" ".join(self.segments[n].text.split())[:400] for n in named)
            return f"- id: O{index}\n  technique: {candidate.name}\n  passage: {passage or '(none)'}"

        batches = [unlinked[i : i + _TECHNIQUE_CHECK_BATCH] for i in range(0, len(unlinked), _TECHNIQUE_CHECK_BATCH)]
        answers = await asyncio.gather(
            *(
                self.call(
                    step="technique_link",
                    system=system,
                    user=TECHNIQUE_LINK_USER.format(
                        items=wrap_untrusted("\n".join(card(i, c) for i, c in enumerate(batch)), "report techniques", stable=True)
                    ),
                    schema=TECHNIQUE_LINK_SCHEMA,
                )
                for batch in batches
            )
        )
        chosen: dict[str, str] = {}
        for batch, answer in zip(batches, answers, strict=True):
            if answer is None:
                self.stats["failed_technique_link_batches"] += 1
                continue
            links = {d.get("id"): str(d.get("attack_id") or "").strip().upper() for d in answer.get("links", []) if isinstance(d, dict)}
            for i, candidate in enumerate(batch):
                tid = links.get(f"O{i}", "")
                if tid in by_tid:
                    chosen[candidate.ref] = tid
        if not chosen:
            return candidates
        by_id = {p.value.upper(): p for p in patterns if p.kind == "attack-pattern-id"}
        heads = {c.value.upper(): c for c in candidates if c.kind == "attack-pattern-id"}
        kept: list[_Candidate] = []
        for candidate in candidates:
            tid = chosen.get(candidate.ref)
            if tid is None:
                kept.append(candidate)
                continue
            self.stats["techniques_linked_by_reading"] += 1
            head = by_id.get(tid) or heads.get(tid)
            if head is not None:  # the id is already an object: one object
                head.spans = sorted(set(head.spans) | set(candidate.spans))
                head.aliases = list(dict.fromkeys([*head.aliases, *(n for n in (candidate.name, *candidate.aliases) if n != head.name)]))
                continue
            text_names = [candidate.name, *candidate.aliases]
            candidate.kinds, candidate.value = ["attack-pattern-id"], tid
            candidate.name = by_tid[tid].name
            candidate.aliases = [n for n in dict.fromkeys(text_names) if n != candidate.name]
            heads[tid] = candidate
            kept.append(candidate)
        return kept

    # 5c. techniques from behaviour ------------------------------------------------------

    async def behaviour_techniques(
        self, chunks: list[Chunk], objects: list[_Candidate], catalogue: list
    ) -> list[ExtractedRelation]:
        """The ATT&CK techniques the report shows without naming them (CTI2TTP): each chunk's
        adversary behaviours, each quoted with up to three candidate techniques (proposing
        broadly), then every candidate checked against its official definition on its quote
        (deciding strictly); a technique kept at ``technique_threshold`` becomes an
        attack-pattern grounded on its quotes, used by the actor the quote names. Adds the
        objects to *objects*; returns the ``uses`` relationships."""
        by_id = {t.attack_id.upper(): t for t in catalogue}
        listing = "\n".join(
            f"{'  ' if '.' in t.attack_id else ''}{t.attack_id} {t.name}"
            + ("" if "." in t.attack_id else f" [{', '.join(t.tactics)}]")
            for t in catalogue
        )
        system = BEHAVIOURS_SYSTEM.format(catalogue=listing)
        answers = await asyncio.gather(
            *(
                self.call(step="behaviours", system=system, user=BEHAVIOURS_USER.format(chunk=_untrusted(chunk)),
                          schema=BEHAVIOURS_SCHEMA)
                for chunk in chunks
            )
        )
        # (chunk, quote span, quote, actor, technique id) per proposal; a technique proposed
        # twice in one chunk is checked once.
        proposals: list[tuple[Chunk, tuple[int, int], str, str, str]] = []
        proposed: set[tuple[int, str]] = set()
        grounded_behaviours: list[tuple[Chunk, tuple[int, int], str, str]] = []
        for chunk, answer in zip(chunks, answers, strict=True):
            if answer is None:
                self.stats["failed_behaviour_chunks"] += 1
                continue
            for behaviour in answer.get("behaviours", []):
                evidence = " ".join(str(behaviour.get("evidence") or "").split())
                span = ground(self.text, evidence, (chunk.start, chunk.end)) if evidence else None
                if span is None:
                    self.stats["behaviours_ungrounded"] += 1
                    continue
                self.stats["behaviours"] += 1
                grounded_behaviours.append((chunk, span, evidence, str(behaviour.get("actor") or "").strip()))
                for raw in (behaviour.get("candidates") or [])[:3]:
                    attack_id = str(raw).strip().upper()
                    if attack_id not in by_id:
                        self.stats["technique_candidates_unknown"] += 1
                        continue
                    if (chunk.index, attack_id) in proposed:
                        continue
                    proposed.add((chunk.index, attack_id))
                    proposals.append((chunk, span, evidence, str(behaviour.get("actor") or "").strip(), attack_id))
        if self.options.technique_retrieval_k > 0 and self.embed is not None and grounded_behaviours:
            retrieved = await _nearest_techniques(self.embed, [b[2] for b in grounded_behaviours], catalogue,
                                                  self.options.technique_retrieval_k)
            for (chunk, span, evidence, actor), ids in zip(grounded_behaviours, retrieved or [], strict=False):
                for attack_id in ids:
                    if attack_id in by_id and (chunk.index, attack_id) not in proposed:
                        proposed.add((chunk.index, attack_id))
                        proposals.append((chunk, span, evidence, actor, attack_id))
                        self.stats["technique_candidates_retrieved"] += 1
        self.stats["technique_candidates"] = len(proposals)
        by_chunk: dict[int, list[int]] = defaultdict(list)
        for index, (chunk, *_rest) in enumerate(proposals):
            by_chunk[chunk.index].append(index)
        work = [
            (proposals[indexes[0]][0], indexes[start:start + _TECHNIQUE_CHECK_BATCH])
            for indexes in by_chunk.values()
            for start in range(0, len(indexes), _TECHNIQUE_CHECK_BATCH)
        ]

        def item(i: int, chars: int) -> str:
            _chunk, _span, evidence, _actor, attack_id = proposals[i]
            technique = by_id[attack_id]
            definition = technique.description[:chars]
            return f"V{i}: evidence \"{evidence}\"\n  candidate {attack_id} {technique.name}: {definition}"

        views = tuple(self.options.technique_check_views) or (700,)
        checks = await asyncio.gather(
            *(
                self.call(step="technique_check", system=TECHNIQUE_CHECK_SYSTEM,
                          user=TECHNIQUE_CHECK_USER.format(items="\n".join(item(i, chars) for i in indexes),
                                                           chunk=_untrusted(chunk)),
                          schema=TECHNIQUE_CHECK_SCHEMA)
                for chars in views
                for chunk, indexes in work
            )
        )
        scores: dict[int, float] = {}
        for (_chunk, indexes), answer in zip([w for _ in views for w in work], checks, strict=True):
            if answer is None:
                self.stats["failed_technique_checks"] += 1
                continue
            for verdict in answer.get("verdicts", []):
                index = str(verdict.get("item", "")).lstrip("V")
                if index.isdigit() and int(index) in indexes:
                    score = _SUPPORT_SCORES.get(str(verdict.get("support")), 0.0)
                    scores[int(index)] = max(score, scores.get(int(index), 0.0))
        self.technique_scores = [
            {"id": proposals[i][4], "score": scores.get(i), "evidence": proposals[i][2]} for i in range(len(proposals))
        ]
        kept = [i for i in range(len(proposals)) if scores.get(i, 0.0) >= self.options.technique_threshold]
        kept = _one_reading(kept, proposals, scores, self.options.technique_choice)
        self.stats["techniques_verified"] = len(kept)
        if self.options.technique_mode in ("balanced", "precision") and kept:
            kept = await self._corroborated(kept, proposals, scores, by_id)
        if self.options.technique_selection in ("kept", "candidates"):
            pool = kept if self.options.technique_selection == "kept" else [
                i for i in range(len(proposals)) if scores.get(i, 0.0) >= _SUPPORT_SCORES["weak"]
            ]
            if pool:
                kept = await self._selected(pool, kept, proposals, by_id)
        techniques: dict[str, _Candidate] = {c.value.upper(): c for c in objects if c.kind == "attack-pattern-id"}
        actors = [c for c in objects if c.family in ("actor", "software") and c.kind not in ("infrastructure", "product")]
        subjects = self._main_subjects(objects)
        relations: dict[tuple[str, str], ExtractedRelation] = {}
        for i in kept:
            _chunk, span, evidence, actor_name, attack_id = proposals[i]
            technique = techniques.get(attack_id)
            if technique is None:
                technique = _Candidate(self._ref("T"), ["attack-pattern-id"], by_id[attack_id].name, "technique",
                                       value=attack_id)
                techniques[attack_id] = technique
                objects.append(technique)
                self.stats["techniques_from_behaviour"] += 1
            if span not in technique.spans:
                technique.spans = sorted({*technique.spans, span})
            key = surface_key(actor_name)
            named = [c for c in actors if key and key in c.name_keys()]
            # Who does it: the actor the quote names; a generic or absent one is the
            # report's main subject when there is exactly one (an implied link).
            source = named[0] if len(named) == 1 else None
            implied = source is None
            if source is None and len(subjects) == 1:
                source = next(c for c in objects if c.ref in subjects)
            if source is None or resolve_triple(source.kind, "uses", "attack-pattern") is None:
                continue
            relation = relations.setdefault(
                (source.ref, technique.ref),
                ExtractedRelation(source.ref, "uses", technique.ref, [], implied=implied, origin="techniques"),
            )
            relation.implied = relation.implied and implied
            if evidence not in relation.evidence:
                relation.evidence.append(evidence)
        self.stats["technique_relationships"] = len(relations)
        return list(relations.values())

    async def _selected(self, pool: list[int], kept: list[int], proposals: list, by_id: dict) -> list[int]:
        """The candidates of *pool* a listwise reading selects (``technique_selection``): each
        technique once, with its definition and up to three of its quotes; *kept* stands when
        the call fails."""
        quotes: dict[str, list[str]] = defaultdict(list)
        for i in pool:
            attack_id, evidence = proposals[i][4], proposals[i][2]
            if evidence not in quotes[attack_id] and len(quotes[attack_id]) < 3:
                quotes[attack_id].append(evidence)
        listing = "\n".join(
            f"- {attack_id} {by_id[attack_id].name}: {by_id[attack_id].description[:400]}\n"
            + "\n".join(f'    quote: "{q}"' for q in qs)
            for attack_id, qs in sorted(quotes.items())
        )
        from app.services.content_safety import wrap_untrusted

        answer = await self.call(step="technique_select", system=TECHNIQUE_SELECT_SYSTEM,
                                 user=TECHNIQUE_SELECT_USER.format(
                                     candidates=wrap_untrusted(listing, "candidate techniques", stable=True)),
                                 schema=TECHNIQUE_SELECT_SCHEMA)
        if answer is None:
            self.stats["failed_technique_select"] += 1
            return kept
        chosen = {str(x).strip().upper() for x in answer.get("selected", [])}
        result = [i for i in pool if proposals[i][4] in chosen]
        self.stats["techniques_selected"] = len({proposals[i][4] for i in result})
        self.stats["techniques_not_selected"] = len(quotes) - self.stats["techniques_selected"]
        return result

    async def _corroborated(self, kept: list[int], proposals: list, scores: dict[int, float], by_id: dict) -> list[int]:
        """The kept candidates a second reading supports (``technique_mode``): one whole-report
        call lists the techniques the report shows, each on a verbatim quote; "precision" keeps
        a candidate whose parent technique that reading lists, "balanced" also one with an
        explicit verdict or two or more checked quotes."""
        from app.services.content_safety import wrap_untrusted

        parent = lambda attack_id: attack_id.split(".")[0]  # noqa: E731
        answer = await self.call(step="technique_report", system=TECHNIQUE_REPORT_SYSTEM,
                                 user=wrap_untrusted(self.text, "report text", stable=True), schema=TECHNIQUE_REPORT_SCHEMA)
        if answer is None:  # no second reading: the stage's decision stands
            self.stats["failed_technique_report"] += 1
            return kept
        flat = " ".join(self.text.split()).lower()

        def quoted(evidence: object) -> bool:
            # A technique listed without a quote is not read in the text: "" is in any text.
            quote = " ".join(str(evidence or "").split()).lower()
            return bool(quote) and quote in flat

        read = {
            parent(str(t.get("id", "")).strip().upper()) for t in answer.get("techniques", [])
            if str(t.get("id", "")).strip().upper() in by_id and quoted(t.get("evidence"))
        }
        quotes: dict[str, int] = defaultdict(int)
        best: dict[str, float] = defaultdict(float)
        for i in kept:
            key = parent(proposals[i][4])
            quotes[key] += 1
            best[key] = max(best[key], scores.get(i, 0.0))
        balanced = self.options.technique_mode == "balanced"
        result = [
            i for i in kept
            if parent(proposals[i][4]) in read
            or (balanced and (best[parent(proposals[i][4])] >= _SUPPORT_SCORES["explicit"] or quotes[parent(proposals[i][4])] >= 2))
        ]
        self.stats["techniques_uncorroborated"] = len(kept) - len(result)
        return result

    def _same_segment(self, left: int, right: int) -> bool:
        return any(seg.start <= left < seg.end and seg.start <= right < seg.end for seg in self.segments)

    # 6-7. relationships --------------------------------------------------------------

    @staticmethod
    def _main_subjects(objects: list[_Candidate]) -> set[str]:
        """What the report is mostly about: its most-mentioned actors or campaigns (at least
        half the top count), else its most-mentioned malware (a report on one malware)."""
        for kinds in (("intrusion-set", "threat-actor", "campaign"), ("malware",)):
            subjects = [o for o in objects if o.kind in kinds]
            top = max((len(o.spans) for o in subjects), default=0)
            if top:
                return {o.ref for o in subjects if len(o.spans) * 2 >= top}
        return set()

    def _inventory(self, objects: list[_Candidate]) -> str:
        """One line per entity: id, kind, names, mention count; what the report is mostly
        about is marked, so a generic "the group" or "the malware" far from any name resolves."""
        subjects = self._main_subjects(objects)
        lines = []
        for obj in objects:
            names = list(dict.fromkeys([obj.name, *obj.aliases]))
            main = " (main subject)" if obj.ref in subjects else ""
            lines.append(f"{obj.ref} [{obj.kind}] " + " | ".join(names[:4]) + f" ×{len(obj.spans)}{main}")
        return "\n".join(lines)

    async def relate(self, objects: list[_Candidate]) -> list[ExtractedRelation]:
        # Inventory ids are stable and short: E1.. by first appearance.
        objects.sort(key=lambda o: (o.spans[0][0] if o.spans else len(self.text), o.name))
        for index, obj in enumerate(objects, start=1):
            obj.ref = f"E{index}"
        inventory = self._inventory(objects)
        # A chunk no entity is mentioned in has no relationship to give: no call.
        chunks = [
            chunk for chunk in build_chunks(self.segments, self.options.relation_chunk_chars)
            if any(chunk.start <= start < chunk.end for obj in objects for start, _ in obj.spans)
        ]
        answers = await asyncio.gather(
            *(
                self.call(
                    step="relations",
                    system=relation_system_prompt(),
                    user=RELATION_USER.format(inventory=inventory, chunk=_untrusted(chunk)),
                    schema=RELATION_SCHEMA,
                )
                for chunk in chunks
            )
        )
        by_ref = {obj.ref: obj for obj in objects}
        found: dict[tuple[str, str, str], ExtractedRelation] = {}
        self.relation_chunks = chunks
        self.stats["relation_chunks"] = len(chunks)
        self.stats["failed_relation_chunks"] = sum(answer is None for answer in answers)
        rounds = [(chunk, answer, "extraction") for chunk, answer in zip(chunks, answers, strict=True)]
        if self.options.relation_gleaning:
            gleaned = await asyncio.gather(
                *(
                    self.call(
                        step="gleaning",
                        system=relation_system_prompt(),
                        user=RELATION_USER.format(inventory=inventory, chunk=_untrusted(chunk))
                        + GLEANING_USER.format(found=_found_lines((answer or {}).get("relations", []))),
                        schema=RELATION_SCHEMA,
                    )
                    for chunk, answer in zip(chunks, answers, strict=True)
                )
            )
            rounds += [(chunk, answer, "gleaning") for chunk, answer in zip(chunks, gleaned, strict=True)]
        for chunk, answer, origin in rounds:
            local = self._new_entities((answer or {}).get("new_entities", []), chunk, objects, by_ref)
            for relation in (answer or {}).get("relations", []):
                kept = self._validated(relation, chunk, by_ref, local)
                if kept is None:
                    continue
                kept.origin = origin
                if origin == "gleaning" and (kept.source, kept.type, kept.target) not in found:
                    self.stats["relationships_gleaned"] += 1
                key = (kept.source, kept.type, kept.target)
                if key in found:
                    found[key].evidence.extend(kept.evidence)
                    found[key].segments.extend(s for s in kept.segments if s not in found[key].segments)
                    # Stated once anywhere is stated.
                    found[key].implied = found[key].implied and kept.implied
                else:
                    found[key] = kept
        if self.options.relation_pairs:
            await self._pair_pass(chunks, objects, by_ref, found)
        if self.options.structural_indicators:
            self._structural_indicators(objects, found)
        if self.options.structural_techniques:
            self._structural_techniques(objects, found)
        self._victim_locations(objects, found)
        self._propagate_over_enumerations(objects, found)
        relations = await self._validate_recovered(objects, list(found.values()))
        self.stats["relationships"] = len(relations)
        return relations

    def _propagate_over_enumerations(
        self, objects: list[_Candidate], found: dict[tuple[str, str, str], ExtractedRelation]
    ) -> None:
        """A predicate the text applies to a list applies to each of its items: when two or
        more items of one enumeration share a relationship to one entity with one verb
        ("Graphiron checks for: BurpSuite, Charles, DumpIt, ..."), the list's other items of
        the same kind get it too, implied, with the same evidence. Without it a model
        decides each item separately and returns a varying subset. Only from links the text
        states in the list's own passage (its segments and the line leading into it): links
        read elsewhere, or only implied, say nothing of the list (a PDF runs the rows of a
        technique table into one list, and one software's techniques would pass to the next)."""
        def items_of(obj: _Candidate, items: list[tuple[int, int]]) -> set[int]:
            return {i for i, (start, end) in enumerate(items) if any(start <= a and b <= end for a, b in obj.spans)}

        for items in _enumerations(self.text):
            members = {o.ref: items_of(o, items) for o in objects}
            members = {ref: idx for ref, idx in members.items() if idx}
            if len(members) < 3:
                continue
            kinds = {o.ref: o.kind for o in objects if o.ref in members}
            ends = [self._segment_of(items[0][0]), self._segment_of(items[-1][1] - 1)]
            if None in ends:
                continue
            passage = set(range(max(0, ends[0] - 1), ends[1] + 1))  # type: ignore[operator]
            groups: dict[tuple[str, str, bool, str], list[ExtractedRelation]] = {}
            for (source, verb, target), relation in found.items():
                if relation.implied or not passage & set(relation.segments):
                    continue
                if target in members and source not in members:
                    groups.setdefault((source, verb, True, kinds[target]), []).append(relation)
                elif source in members and target not in members:
                    groups.setdefault((target, verb, False, kinds[source]), []).append(relation)
            for (other, verb, outward, kind), relations in groups.items():
                if len(relations) < 2:
                    continue
                for ref in members:
                    if kinds[ref] != kind:
                        continue
                    key = (other, verb, ref) if outward else (ref, verb, other)
                    if key in found:
                        continue
                    found[key] = ExtractedRelation(
                        key[0], verb, key[2], list(relations[0].evidence), implied=True,
                        segments=list(relations[0].segments), origin="enumeration",
                    )
                    self.stats["relationships_over_enumerations"] += 1

    def _victim_locations(
        self, objects: list[_Candidate], found: dict[tuple[str, str, str], ExtractedRelation]
    ) -> None:
        """A threat that targets victims located somewhere targets that place: OpenCTI
        reads victimology from ``targets`` a location, and "financial institutions in South
        Korea" is also a sector located in a country. Implied, with the location's evidence."""
        kinds = {o.ref: o.kind for o in objects}
        located = {}
        for (source, verb, target), relation in found.items():
            if verb == "located-at" and kinds.get(source) in ("sector", "organization", "individual"):
                located.setdefault(source, []).append((target, relation))
        for source, verb, target in list(found):
            if verb != "targets" or target not in located:
                continue
            for place, where in located[target]:
                resolved = resolve_triple(kinds[source], "targets", kinds[place])
                key = (source, "targets", place)
                if resolved == ("targets", False) and key not in found:
                    found[key] = ExtractedRelation(
                        source, "targets", place, list(where.evidence), implied=True,
                        segments=list(where.segments), origin="victim-location",
                    )
                    self.stats["relationships_victim_location"] += 1
        if self.options.victim_countries:
            self._victim_countries(objects, found)

    def _victim_countries(
        self, objects: list[_Candidate], found: dict[tuple[str, str, str], ExtractedRelation]
    ) -> None:
        """A threat that targets a city or a region of a known country also targets that
        country (``ExtractionOptions.victim_countries``)."""
        from app.services.stix_extraction.bundle import _countries, _country_code

        by_ref = {o.ref: o for o in objects}
        countries = {(o.code.upper() or (_country_code("", o.name) or "")): o for o in objects if o.kind == "country"}
        for source, verb, target in list(found):
            place = by_ref.get(target)
            if verb != "targets" or place is None or place.kind not in ("city", "region"):
                continue
            code = _country_code(place.code, "")
            if code is None:
                continue
            country = countries.get(code)
            if country is None:
                known = _countries().get(code)
                if known is None:
                    continue
                country = _Candidate(self._ref("L"), ["country"], known["name"], family_of("country"), code=code)
                objects.append(country)
                by_ref[country.ref] = country
                countries[code] = country
            key = (source, "targets", country.ref)
            if key not in found and resolve_triple(by_ref[source].kind, "targets", "country") == ("targets", False):
                where = found[(source, verb, target)]
                found[key] = ExtractedRelation(source, "targets", country.ref, list(where.evidence), implied=True,
                                               segments=list(where.segments), origin="victim-country")
                self.stats["relationships_victim_country"] += 1

    def _structural_indicators(
        self, objects: list[_Candidate], found: dict[tuple[str, str, str], ExtractedRelation]
    ) -> None:
        """Observables a report's layout ties to one threat, which a reader takes from the
        page and a model reading prose misses (lists of tens of hashes): a table row that
        names one threat beside its observables (``<sha256> – Backdoor.Tokel``), and lines
        of observables only under a short heading that names one (``V3G4 Sample``)."""
        lines = [(m.start(), m.end()) for m in re.finditer(r"[^\n]+", self.text)]
        # Each object's spans by the lines they overlap, read once: a long report has
        # thousands of lines and hundreds of objects.
        starts = [start for start, _ in lines]
        overlapping: dict[int, list[tuple[_Candidate, int, int]]] = defaultdict(list)
        for obj in objects:
            for a, b in obj.spans:
                first = max(bisect.bisect_right(starts, a) - 1, 0)
                for index in range(first, len(lines)):
                    if lines[index][0] >= b:
                        break
                    if a < lines[index][1]:
                        overlapping[index].append((obj, a, b))

        def named(index: int, kinds: frozenset[str], *, as_word: bool = False) -> list[_Candidate]:
            """The objects of *kinds* named within the line; *as_word*: only where the name
            stands as a word, not inside a value (a path, a file name) that contains it."""
            start, end = lines[index]
            found: dict[str, _Candidate] = {}
            for o, a, b in overlapping.get(index, ()):
                if o.kind in kinds and start <= a and b <= end:
                    if as_word and (
                        (a > start and self.text[a - 1] in _VALUE_JOINERS)
                        or (b < end and self.text[b] in _VALUE_JOINERS and b + 1 < end and not self.text[b + 1].isspace())
                    ):
                        continue
                    found.setdefault(o.ref, o)
            return list(found.values())

        def rest(index: int, observables: list[_Candidate]) -> int:
            """Characters of the line outside its observables."""
            start, end = lines[index]
            refs = {o.ref for o in observables}
            covered = sum(min(b, end) - max(a, start) for o, a, b in overlapping.get(index, ()) if o.ref in refs)
            return len(self.text[start:end].strip()) - covered

        def prose(index: int, named: list[_Candidate]) -> bool:
            """The line reads as a sentence beside its values: a table row has a name or a
            label there ("Magnet Goblin Infra"), not a clause ("where APT41 exploited a
            directory traversal vulnerability to read the web.config")."""
            start, end = lines[index]
            refs = {o.ref for o in named}
            spans = sorted((a, b) for o, a, b in overlapping.get(index, ()) if o.ref in refs)
            text, cursor = [], start
            for a, b in spans:
                text.append(self.text[cursor:max(cursor, a)])
                cursor = max(cursor, b)
            text.append(self.text[cursor:end])
            return len(re.findall(r"(?<![A-Za-z])[a-z]{3,}(?![A-Za-z])", " ".join(text))) > 3

        def link(observable: _Candidate, threat: _Candidate, evidence: str, stated: bool) -> None:
            # A file's name beside a tool or a platform is that program's own file (cmd.exe
            # beside Ping, wininet.dll beside Windows), not a sign of a threat; its hash is.
            if observable.kind == "file-name" and threat.kind not in _MALICIOUS_KINDS:
                return
            resolved = resolve_triple(observable.kind, "indicates", threat.kind)
            if resolved is None or resolved[1]:
                return
            key = (observable.ref, resolved[0], threat.ref)
            if key not in found:
                found[key] = ExtractedRelation(
                    observable.ref, resolved[0], threat.ref, [evidence], implied=not stated, origin="layout"
                )
                self.stats["relationships_from_layout"] += 1

        def other_named(index: int) -> bool:
            """The line names an object that is neither a threat nor an observable (a
            technique, a vulnerability, a software, a place): content, not a heading."""
            start, end = lines[index]
            return any(o.kind not in _THREAT_KINDS and o.kind not in _OBSERVABLE_KINDS and start <= a and b <= end
                       for o, a, b in overlapping.get(index, ()))

        heading: tuple[_Candidate, str] | None = None
        # Lines read since the heading's last listed observable: a heading carries over a
        # line or two ("V3G4 Sample" then "July Campaign"), not a table's header, a page
        # footer and the next table.
        carried = 0
        subjects = [o for o in objects if o.ref in self._main_subjects(objects) and o.kind in _THREAT_KINDS]
        main = subjects[0] if self.options.ioc_lists_main_subject and len(subjects) == 1 else None
        indicated = {source for source, verb, _ in found if verb == "indicates"}
        for index, (start, end) in enumerate(lines):
            observables = named(index, _OBSERVABLE_KINDS)
            threats = named(index, _THREAT_KINDS, as_word=True)
            line = " ".join(self.text[start:end].split())
            if observables and len(threats) == 1 and rest(index, observables) <= 120 and not prose(index, observables + threats):
                for observable in observables:
                    link(observable, threats[0], line, stated=True)
                continue
            if observables and not threats and rest(index, observables) <= 3:
                carried = 0
                if heading is not None:
                    for observable in observables:
                        link(observable, heading[0], heading[1], stated=False)
                elif main is not None:
                    for observable in observables:
                        if observable.ref not in indicated:
                            link(observable, main, line, stated=False)
                continue
            # A short line without observables opens a section; it names its threat, or
            # keeps the one above it ("V3G4 Sample" then "July Campaign"). A list item is
            # an entry of the section, not its heading: "- a.csproj – Weaponized Visual
            # Studio project" names what one file is, and leaves the section's threat.
            if _LIST_ITEM.match(line):
                continue
            if len(line) <= 80:
                if len(threats) == 1:
                    heading, carried = (threats[0], line), 0
                elif threats or other_named(index):
                    heading = None
                else:
                    carried += 1
                    if carried > _HEADING_CARRY:
                        heading = None
            else:
                heading = None

    def _structural_techniques(
        self, objects: list[_Candidate], found: dict[tuple[str, str, str], ExtractedRelation]
    ) -> None:
        """ATT&CK techniques a report's layout ties to a user (a group, a campaign or a
        software): on a line made mostly of techniques, each technique goes to the user
        named last before it on that line (a mapping table's row, often several rows run
        into one line by the PDF); a line of techniques that names no user goes to the user
        of the short heading above it, else to the report's one main subject (implied). A
        line where techniques are a minority of the words is prose, left to the model."""
        techniques = [o for o in objects if o.kind in _TECHNIQUE_KINDS]
        users = [o for o in objects if o.kind in _TECHNIQUE_USERS]
        if not techniques or not users:
            return
        subjects = [o for o in objects if o.ref in self._main_subjects(objects) and o.kind in _TECHNIQUE_USERS]
        main = subjects[0] if len(subjects) == 1 else None

        def link(user: _Candidate, technique: _Candidate, evidence: str, stated: bool) -> None:
            if resolve_triple(user.kind, "uses", technique.kind) != ("uses", False):
                return
            key = (user.ref, "uses", technique.ref)
            if key not in found:
                found[key] = ExtractedRelation(user.ref, "uses", technique.ref, [evidence[:500]], implied=not stated,
                                               origin="layout")
                self.stats["technique_relationships_from_layout"] += 1

        # Where the text writes each technique: its object's spans and every place the text
        # writes its ATT&CK name (a table writes "Application Layer Protocol: Web Protocols"
        # for an object known by its id); read here only, the objects keep their spans.
        written: dict[str, list[tuple[int, int]]] = {o.ref: list(o.spans) for o in techniques}
        for o in techniques:
            for name in self.technique_names.get((o.value or "").upper(), ()):
                written[o.ref] += [(m.start(), m.end()) for m in re.finditer(re.escape(name), self.text)]
        # A user named inside a technique's name ("cmd" for the "Windows Command Shell" of
        # "Command and Scripting Interpreter: Windows Command Shell") is not named there.
        technique_spans = sorted({span for spans in written.values() for span in spans})
        starts = [a for a, _ in technique_spans]

        def inside_technique(a: int, b: int) -> bool:
            index = bisect.bisect_right(starts, a) - 1
            return any(ta <= a and b <= tb for ta, tb in technique_spans[max(0, index - 20):index + 1])

        heading: _Candidate | None = None
        # The user a table row ended with: a cell wrapped over the next lines continues it.
        row_user: _Candidate | None = None
        for match in re.finditer(r"[^\n]+", self.text):
            start, end = match.span()
            line = " ".join(match.group().split())
            on_line = sorted(
                {
                    **{(a, b, o.ref): o for o in techniques for a, b in written[o.ref] if start <= a and b <= end},
                    **{(a, b, o.ref): o for o in users for a, b in o.spans
                       if start <= a and b <= end and not inside_technique(a, b)},
                }.items()
            )
            named_techniques = [(a, b, o) for (a, b, _), o in on_line if o.kind in _TECHNIQUE_KINDS]
            # The longest name wins: "Steganography" inside "Obfuscated Files or Information:
            # Steganography" is not a second technique (Data Obfuscation's has that name too).
            named_techniques = [
                (a, b, o) for a, b, o in named_techniques
                if not any(x <= a and b <= y and (x, y) != (a, b) for x, y, _ in named_techniques)
            ]
            named_users = [(a, b, o) for (a, b, _), o in on_line if o.kind in _TECHNIQUE_USERS]
            tabular = named_techniques and not _prose_words(self.text, start, end, [(a, b) for (a, b, _), _o in on_line])
            if tabular and named_users:
                # A row: each technique to the user named last before it; one wrapping
                # onto the next line keeps that user.
                for a, _b, technique in named_techniques:
                    before = [user for ua, _ub, user in named_users if ua < a]
                    if before:
                        link(before[-1], technique, line, stated=True)
                row_user = named_users[-1][2]
                continue
            if tabular and row_user is not None:
                for _a, _b, technique in named_techniques:
                    link(row_user, technique, line, stated=True)
                continue
            row_user = None
            if tabular and len(named_techniques) >= 2:
                owner = heading or main
                if owner is not None:
                    for _a, _b, technique in named_techniques:
                        link(owner, technique, line, stated=False)
                continue
            # A short line naming one user and no technique opens a section about it; prose
            # (a long line) closes it.
            if len(line) <= 80 and not named_techniques:
                distinct = {o.ref: o for _a, _b, o in named_users}
                if len(distinct) == 1:
                    heading = next(iter(distinct.values()))
                elif distinct:
                    heading = None
            elif len(line) > 80:
                heading = None

    def _candidate_pairs(
        self, chunk: Chunk, objects: list[_Candidate], found: dict[tuple[str, str, str], ExtractedRelation]
    ) -> list[tuple[_Candidate, _Candidate, list[int]]]:
        """Entities named in the same or neighbouring segments of *chunk*, that the
        ontology lets relate and no relationship links yet; same segment first."""
        named: dict[str, set[int]] = {}
        for segment in chunk.segments:
            for obj in objects:
                if any(segment.start <= start and end <= segment.end for start, end in obj.spans):
                    named.setdefault(obj.ref, set()).add(segment.index)
        linked = {frozenset((source, target)) for source, _, target in found}
        by_ref = {obj.ref: obj for obj in objects}
        pairs = []
        refs = sorted(named, key=lambda ref: int(ref[1:]) if ref[1:].isdigit() else 0)
        for i, left in enumerate(refs):
            for right in refs[i + 1:]:
                if frozenset((left, right)) in linked:
                    continue
                reach = 10**6 if self.options.relatedness else 1
                close = [(x, y) for x in named[left] for y in named[right] if abs(x - y) <= reach]
                if not close:
                    continue
                near = sorted({index for pair in close for index in pair})
                a, b = by_ref[left], by_ref[right]
                if not any(resolve_triple(a.kind, verb, b.kind) or resolve_triple(b.kind, verb, a.kind) for verb in RELATION_TYPES):
                    continue
                nearest = min(close, key=lambda xy: abs(xy[0] - xy[1]))
                distance = abs(nearest[0] - nearest[1])
                pairs.append((distance, a, b, near if distance <= 1 else sorted(set(nearest))))
        # The report's main subject is paired with every entity of the chunk, however far:
        # "the actor", "the malware" refer to it across the report (BEACON's central subject).
        if self.options.central_subject:
            listed = {frozenset((a.ref, b.ref)) for _, a, b, _ in pairs}
            subjects = self._main_subjects(objects)
            # In the objects' order, not the set's: the prompt is then the same on every run.
            for subject in (obj.ref for obj in objects if obj.ref in subjects):
                main = by_ref[subject]
                for ref in refs:
                    if ref == subject or frozenset((subject, ref)) in linked | listed:
                        continue
                    other = by_ref[ref]
                    if any(resolve_triple(main.kind, verb, other.kind) or resolve_triple(other.kind, verb, main.kind) for verb in RELATION_TYPES):
                        pairs.append((99, main, other, sorted(named[ref])))
        pairs.sort(key=lambda item: item[0])
        return [(a, b, near) for _, a, b, near in pairs[: self.options.max_pairs_per_chunk]]

    @staticmethod
    def _pair_options(a: _Candidate, b: _Candidate) -> str:
        """The relationships the ontology allows between *a* and *b*, both ways, as sentences."""
        # Only a triple the ontology allows as written: a remapped verb or a swap is a
        # reading of a mistake, not an option to offer.
        sentences = [
            f"{x.name} {verb} {y.name}"
            for verb in RELATION_TYPES
            for x, y in ((a, b), (b, a))
            if resolve_triple(x.kind, verb, y.kind) == (verb, False)
        ]
        return "; ".join([*dict.fromkeys(sentences), "none"])

    async def _pair_pass(
        self,
        chunks: list[Chunk],
        objects: list[_Candidate],
        by_ref: dict[str, _Candidate],
        found: dict[tuple[str, str, str], ExtractedRelation],
    ) -> None:
        subjects = self._main_subjects(objects)
        size = max(1, self.options.max_pairs_per_call)
        work = []
        for chunk in chunks:
            pairs = self._candidate_pairs(chunk, objects, found)
            self.stats["candidate_pairs"] += len(pairs)
            work += [(chunk, pairs[start:start + size]) for start in range(0, len(pairs), size)]
        answers = await asyncio.gather(
            *(
                self.call(
                    step="pairs",
                    system=pairs_system_prompt(relatedness=self.options.relatedness),
                    user=PAIRS_USER.format(
                        pairs="\n".join(
                            f"P{i}: {a.ref} [{a.kind}] {a.name}{' (main subject)' if a.ref in subjects else ''} | "
                            f"{b.ref} [{b.kind}] {b.name}{' (main subject)' if b.ref in subjects else ''} "
                            f"(segments {', '.join(map(str, near))})"
                            + (f"\n    options: {self._pair_options(a, b)}" if self.options.pair_options else "")
                            for i, (a, b, near) in enumerate(pairs)
                        ),
                        chunk=_untrusted(chunk),
                    ),
                    schema=PAIRS_SCHEMA,
                )
                for chunk, pairs in work
            )
        )
        for (chunk, pairs), answer in zip(work, answers, strict=True):
            if answer is None:
                self.stats["failed_pair_chunks"] += 1
                continue
            self._paired.update(frozenset((a.ref, b.ref)) for a, b, _ in pairs)
            for decision in answer.get("decisions", []):
                index = str(decision.get("pair", "")).lstrip("P")
                if not index.isdigit() or int(index) >= len(pairs) or decision.get("type") == "none":
                    continue
                a, b, _ = pairs[int(index)]
                evidence = str(decision.get("evidence") or "").strip()
                # Confirmed only by a quote of the chunk (co-occurrence is not a link).
                if not evidence or ground(self.text, evidence, (chunk.start, chunk.end)) is None:
                    self.stats["pairs_unconfirmed"] += 1
                    continue
                source, target = (b, a) if decision.get("source") == b.ref else (a, b)
                kept = self._validated(
                    {"source": source.ref, "target": target.ref, "type": decision.get("type"),
                     "evidence": evidence, "segment": decision.get("segment"), "basis": decision.get("basis")},
                    chunk, by_ref, {},
                )
                if kept is None:
                    continue
                key = (kept.source, kept.type, kept.target)
                if key in found:
                    continue
                kept.origin = "pairs"
                found[key] = kept
                self.stats["relationships_from_pairs"] += 1

    @staticmethod
    def _relatable(a: _Candidate, b: _Candidate) -> bool:
        """Whether the ontology has a verb between the two kinds, either way."""
        return any(resolve_triple(a.kind, verb, b.kind) or resolve_triple(b.kind, verb, a.kind) for verb in RELATION_TYPES)

    def _cross_chunk_pairs(
        self, objects: list[_Candidate], relations: list[ExtractedRelation]
    ) -> list[tuple[_Candidate, _Candidate]]:
        """Pairs of entities no relationship chunk names together, that the ontology lets
        relate, that nothing links yet and that the pair pass has not decided (the main
        subject's pairs); a threat at one end first, then in the report's order."""
        starts = [chunk.start for chunk in self.relation_chunks]

        def chunks_of(obj: _Candidate) -> set[int]:
            found = set()
            for start, _ in obj.spans:
                index = bisect.bisect_right(starts, start) - 1
                if index >= 0 and start < self.relation_chunks[index].end:
                    found.add(index)
            return found

        listed = [(o, chunks_of(o)) for o in objects if o.kind in _CROSS_CHUNK_KINDS]
        listed = [(o, where) for o, where in listed if where]
        linked = {frozenset((r.source, r.target)) for r in relations} | self._paired
        pairs = []
        for i, (a, where_a) in enumerate(listed):
            for b, where_b in listed[i + 1:]:
                if where_a & where_b or frozenset((a.ref, b.ref)) in linked or not self._relatable(a, b):
                    continue
                pairs.append((a, b))
        # Stable: the threats' pairs first, each group in the report's order.
        pairs.sort(key=lambda pair: not (pair[0].kind in _THREAT_KINDS or pair[1].kind in _THREAT_KINDS))
        self.stats["cross_chunk_pairs"] = len(pairs)
        self.stats["cross_chunk_pairs_left_out"] = max(0, len(pairs) - _CROSS_CHUNK_MAX_PAIRS)
        return pairs[:_CROSS_CHUNK_MAX_PAIRS]

    def _cross_chunk_card(self, obj: _Candidate, subjects: set[str]) -> str:
        """An entity as the cross-chunk pass reads it: id, kind, names, and the first
        sentences naming it, each cut around the name when long."""
        names = list(dict.fromkeys([obj.name, *obj.aliases]))
        main = " (main subject)" if obj.ref in subjects else ""
        lines = [f"{obj.ref} [{obj.kind}] " + " | ".join(names[:4]) + main]
        shown: set[int] = set()
        for start, end in sorted(obj.spans):
            index = self._segment_of(start)
            if index is None or index in shown:
                continue
            shown.add(index)
            segment = self.segments[index]
            half = _CROSS_CHUNK_SENTENCE_CHARS // 2
            if segment.end - segment.start > _CROSS_CHUNK_SENTENCE_CHARS:
                left, right = max(segment.start, start - half), min(segment.end, end + half)
            else:
                left, right = segment.start, segment.end
            lines.append(f"  [S{index}] {' '.join(self.text[left:right].split())}")
            if len(shown) == _CROSS_CHUNK_SENTENCES:
                break
        return "\n".join(lines)

    async def cross_chunk(self, objects: list[_Candidate], relations: list[ExtractedRelation]) -> list[ExtractedRelation]:
        """Relationships between entities the report names in different relationship chunks
        (``cross_chunk_relations``): each pair decided on the sentences naming its two
        entities, kept only with a quote of the report, in a segment naming one of them, and
        a triple the ontology allows. Implied unless the quote names both; always doubtful
        for verification."""
        from app.services.content_safety import wrap_untrusted

        if len(self.relation_chunks) < 2:
            return []
        pairs = self._cross_chunk_pairs(objects, relations)
        if not pairs:
            return []
        subjects = self._main_subjects(objects)
        order = {o.ref: i for i, o in enumerate(objects)}
        batches = [pairs[i : i + _CROSS_CHUNK_BATCH] for i in range(0, len(pairs), _CROSS_CHUNK_BATCH)]

        def entities(batch: list[tuple[_Candidate, _Candidate]]) -> str:
            members = {o.ref: o for pair in batch for o in pair}
            ordered = sorted(members.values(), key=lambda o: order[o.ref])
            return "\n".join(self._cross_chunk_card(o, subjects) for o in ordered)

        answers = await asyncio.gather(
            *(
                self.call(
                    step="cross_chunk",
                    system=cross_chunk_system_prompt(),
                    user=CROSS_CHUNK_USER.format(
                        entities=wrap_untrusted(entities(batch), "report excerpts", stable=True),
                        pairs="\n".join(
                            f"P{i}: {a.ref} [{a.kind}] {a.name} | {b.ref} [{b.kind}] {b.name}"
                            for i, (a, b) in enumerate(batch)
                        ),
                    ),
                    schema=CROSS_CHUNK_SCHEMA,
                )
                for batch in batches
            )
        )
        known = {(r.source, r.type, r.target) for r in relations}
        added: dict[tuple[str, str, str], ExtractedRelation] = {}
        for batch, answer in zip(batches, answers, strict=True):
            if answer is None:
                self.stats["failed_cross_chunk_batches"] += 1
                continue
            for decision in answer.get("decisions", []):
                index = str(decision.get("pair", "")).lstrip("P")
                if not index.isdigit() or int(index) >= len(batch) or decision.get("type") in (None, "none"):
                    continue
                kept = self._cross_chunk_relation(*batch[int(index)], decision)
                if kept is None:
                    continue
                key = (kept.source, kept.type, kept.target)
                if key not in known and key not in added:
                    added[key] = kept
        self.stats["relationships_cross_chunk"] = len(added)
        return list(added.values())

    def _cross_chunk_relation(self, a: _Candidate, b: _Candidate, decision: dict) -> ExtractedRelation | None:
        evidence = " ".join(str(decision.get("evidence") or "").split())
        cited = decision.get("segment")
        window = (
            (self.segments[cited].start, self.segments[cited].end)
            if isinstance(cited, int) and 0 <= cited < len(self.segments) else None
        )
        span = ground(self.text, evidence, window) if evidence else None
        segment = self._segment_of(span[0]) if span is not None else None
        # A quote of the report, in a sentence naming one of the two: a sentence naming
        # neither states nothing about them.
        if span is None or segment is None or not (self._named_in(a, segment) or self._named_in(b, segment)):
            self.stats["cross_chunk_unconfirmed"] += 1
            return None
        source, target = (b, a) if decision.get("source") == b.ref else (a, b)
        proposed = str(decision.get("type") or "")
        self.raw_relations.append(ExtractedRelation(source.ref, proposed, target.ref))
        resolved = resolve_triple(source.kind, proposed, target.kind)
        if resolved is None:
            self.stats["relationships_off_ontology"] += 1
            self.off_ontology.append((source.kind, proposed, target.kind))
            return None
        verb, swapped = resolved
        if swapped:
            source, target = target, source
        both = all(any(span[0] <= start and end <= span[1] for start, end in o.spans) for o in (source, target))
        return ExtractedRelation(
            source.ref, verb, target.ref, [evidence], implied=not both, segments=[segment],
            remapped=verb != proposed or swapped, origin="cross-chunk",
        )

    async def _validate_recovered(
        self, objects: list[_Candidate], relations: list[ExtractedRelation]
    ) -> list[ExtractedRelation]:
        """Entities the relationship step added go through resolution, as detected ones
        did (the research pipeline verified them too): one dropped takes its
        relationships with it, one renamed onto an existing object is merged into it."""
        recovered = [o for o in objects if o.recovered]
        if not recovered:
            return relations
        survivors = await self.resolve(recovered)
        kept = {c.ref for c in survivors}
        remap: dict[str, str] = {}
        for candidate in recovered:
            if candidate.ref not in kept:
                objects.remove(candidate)
                # Merged into another recovered entity, or dropped.
                head = next((s for s in survivors if candidate.name in s.surfaces or candidate.name in s.aliases), None)
                if head is not None:
                    remap[candidate.ref] = head.ref
                else:
                    self.stats["recovered_dropped"] += 1
                continue
            twin = next(
                (o for o in objects if o is not candidate and not o.recovered
                 and o.family == candidate.family and o.name_keys() & candidate.name_keys()),
                None,
            )
            if twin is not None:
                remap[candidate.ref] = twin.ref
                twin.spans = sorted(set(twin.spans) | set(candidate.spans))
                objects.remove(candidate)
        for ref, target in list(remap.items()):
            seen = {ref}
            while target in remap and target not in seen:
                seen.add(target)
                target = remap[target]
            remap[ref] = target
        by_ref = {o.ref: o for o in objects}
        merged: dict[tuple[str, str, str], ExtractedRelation] = {}
        for relation in relations:
            source = remap.get(relation.source, relation.source)
            target = remap.get(relation.target, relation.target)
            if source not in by_ref or target not in by_ref or source == target:
                self.stats["relationships_dropped_with_recovered"] += 1
                continue
            resolved = resolve_triple(by_ref[source].kind, relation.type, by_ref[target].kind)
            if resolved is None:
                continue
            verb, swapped = resolved
            if swapped:
                source, target = target, source
            key = (source, verb, target)
            # The rebuilt relationship keeps how it was read (its origin, a remapped verb,
            # a verifier's confidence): only a stated extraction is taken as read.
            remapped = relation.remapped or verb != relation.type or swapped
            if key in merged:
                kept = merged[key]
                kept.evidence.extend(relation.evidence)
                kept.segments.extend(s for s in relation.segments if s not in kept.segments)
                kept.implied = kept.implied and relation.implied
                kept.remapped = kept.remapped and remapped
                if relation.origin == "extraction":
                    kept.origin = "extraction"
                if relation.verified is not None:
                    kept.verified = max(kept.verified or 0.0, relation.verified)
            else:
                merged[key] = replace(
                    relation, source=source, type=verb, target=target, evidence=list(relation.evidence),
                    segments=list(relation.segments), remapped=remapped,
                )
        return list(merged.values())

    def _named_in(self, obj: _Candidate, segment: int) -> bool:
        seg = self.segments[segment]
        return any(seg.start <= start and end <= seg.end for start, end in obj.spans)

    def _suspicious(self, relation: ExtractedRelation, by_ref: dict[str, _Candidate]) -> bool:
        """Whether a relationship needs checking: the extractor read it only by
        implication, without grounded evidence, through a remapped verb, a recall pass, a
        recovered entity, or a generic mention (an endpoint not named where it was read).
        An explicit relationship between two entities named in its grounded sentence is
        taken as read: a second strict filter there removes good relationships."""
        source, target = by_ref[relation.source], by_ref[relation.target]
        if relation.implied or not relation.evidence or relation.remapped or relation.origin != "extraction":
            return True
        if source.recovered or target.recovered or not relation.segments:
            return True
        return not any(self._named_in(source, s) and self._named_in(target, s) for s in relation.segments)

    def _verification_card(self, index: int, relation: ExtractedRelation, by_ref: dict[str, _Candidate]) -> str:
        """The candidate with its context: the cited segment and its neighbours, and where
        each endpoint is named nearest to it; the generic readings the extractor made."""
        source, target = by_ref[relation.source], by_ref[relation.target]
        cited = relation.segments[0] if relation.segments else None
        window: set[int] = set()
        readings = []
        if cited is not None:
            window |= {s for s in (cited - 1, cited, cited + 1) if 0 <= s < len(self.segments)}
        for obj in (source, target):
            named = sorted({self._segment_of(start) for start, _ in obj.spans} - {None})
            if cited is not None and named:
                nearest = min(named, key=lambda s: abs(s - cited))
                window.add(nearest)
                if not self._named_in(obj, cited):
                    readings.append(f'a generic mention in S{cited} read as "{obj.name}"')
            elif named:
                window.add(named[0])
        ordered = sorted(window, key=lambda s: (abs(s - cited) if cited is not None else 0, s))[:_VERIFICATION_WINDOW]
        excerpt = " ".join(f"[S{s}] {' '.join(self.segments[s].text.split())}" for s in sorted(ordered))
        return (
            f"- id: R{index}\n  source: {source.name} [{source.kind}]\n  type: {relation.type}\n"
            f"  target: {target.name} [{target.kind}]\n"
            + (f"  readings: {'; '.join(readings)}\n" if readings else "")
            + f"  excerpt: {excerpt or ' / '.join(relation.evidence)}"
        )

    def _relevance_card(self, index: int, obj: _Candidate, links: list[str]) -> str:
        named = sorted({self._segment_of(start) for start, _ in obj.spans} - {None})[:_RELEVANCE_SEGMENTS]
        excerpt = " ".join(f"[S{s}] {' '.join(self.segments[s].text.split())[:400]}" for s in named)
        return (
            f"- id: O{index}\n  kind: {obj.kind}\n  name: {obj.name}\n"
            + (f"  links: {'; '.join(links[:6])}\n" if links else "")
            + f"  excerpts: {excerpt or '(none)'}"
        )

    async def review_relevance(self, objects: list[_Candidate], relations: list[ExtractedRelation]) -> set[str]:
        """The refs of the objects a reading against the report judges outside its knowledge
        (``relevance_review``). A failed batch keeps its objects."""
        from app.services.content_safety import wrap_untrusted

        by_ref = {o.ref: o for o in objects}
        links: dict[str, list[str]] = defaultdict(list)
        for r in relations:
            if r.source in by_ref and r.target in by_ref:
                links[r.source].append(f"{r.type} {by_ref[r.target].name}")
                links[r.target].append(f"{by_ref[r.source].name} {r.type} it")
        names = [o for o in objects if o.kind not in _WRITTEN_KINDS and o.kind not in _ADDRESS_KINDS]
        address_kinds = _ADDRESS_KINDS | (_WEB_ADDRESS_KINDS if self.options.relevance_web_addresses else frozenset())
        addresses = [o for o in objects if o.kind in address_kinds]
        self.stats["relevance_reviewed"] = len(names) + len(addresses)
        opening = " ".join(self.text[:_RELEVANCE_OPENING].split())
        # Addresses are read in batches of their own, after the names.
        batches = [group[i : i + _RELEVANCE_BATCH] for group in (names, addresses) for i in range(0, len(group), _RELEVANCE_BATCH)]
        answers = await asyncio.gather(
            *(
                self.call(
                    step="relevance",
                    system=RELEVANCE_SYSTEM,
                    user=RELEVANCE_USER.format(
                        opening=wrap_untrusted(opening, "report opening", stable=True),
                        candidates=wrap_untrusted(
                            "\n".join(self._relevance_card(i, o, links[o.ref]) for i, o in enumerate(batch)),
                            "report objects",
                            stable=True,
                        ),
                    ),
                    schema=RELEVANCE_SCHEMA,
                )
                for batch in batches
            )
        )
        dropped: set[str] = set()
        for batch, answer in zip(batches, answers, strict=True):
            if answer is None:
                self.stats["failed_relevance_batches"] += 1
                continue
            verdicts = {d.get("id"): d.get("keep") for d in answer.get("decisions", []) if isinstance(d, dict)}
            dropped |= {o.ref for i, o in enumerate(batch) if verdicts.get(f"O{i}") is False}
        self.stats["relevance_dropped"] = len(dropped)
        return dropped

    async def activity_hub(
        self, objects: list[_Candidate], relations: list[ExtractedRelation]
    ) -> tuple[_Candidate | None, list[ExtractedRelation]]:
        """The report's unnamed main activity and its links (``activity_hub``), or none when
        a campaign or an incident among the objects already names it, or the reading finds
        none. Each link is kept only where the ontology allows it."""
        from app.services.content_safety import wrap_untrusted

        if any(o.kind in ("campaign", "incident") for o in objects):
            return None, []
        listed = [o for o in objects if o.kind in _HUB_TARGETS][:_HUB_OBJECTS]
        if not listed:
            return None, []
        by_ref = {o.ref: o for o in objects}
        links: dict[str, list[str]] = defaultdict(list)
        for r in relations:
            if r.source in by_ref and r.target in by_ref:
                links[r.source].append(f"{r.type} {by_ref[r.target].name}")
        cards = "\n".join(
            f"- id: O{i}  kind: {o.kind}  name: {o.name}" + (f"  links: {'; '.join(links[o.ref][:4])}" if links[o.ref] else "")
            for i, o in enumerate(listed)
        )
        answer = await self.call(
            step="activity",
            system=HUB_SYSTEM,
            user=HUB_USER.format(
                opening=wrap_untrusted(" ".join(self.text[:_HUB_OPENING].split()), "report opening", stable=True),
                objects=wrap_untrusted(cards, "report objects", stable=True),
            ),
            schema=HUB_SCHEMA,
        )
        if answer is None:
            self.stats["failed_activity_hub"] = 1
            return None, []
        kind, name = answer.get("activity"), " ".join(str(answer.get("name") or "").split())
        if kind not in ("incident", "campaign") or not name or name.lower() in {o.name.lower() for o in objects}:
            return None, []
        hub = _Candidate(self._ref("H"), [kind], name, "actor")
        added: list[ExtractedRelation] = []
        seen: set[tuple[str, str, str]] = set()
        for link in answer.get("links") or []:
            match = re.fullmatch(r"O(\d+)", str(link.get("id", "")))
            if not match or int(match.group(1)) >= len(listed):
                continue
            target = listed[int(match.group(1))]
            resolved = resolve_triple(kind, str(link.get("verb")), target.kind)
            if resolved is None:
                continue
            verb, swapped = resolved
            source, end = (target.ref, hub.ref) if swapped else (hub.ref, target.ref)
            if (source, verb, end) in seen:
                continue
            seen.add((source, verb, end))
            added.append(ExtractedRelation(source, verb, end, implied=True, origin="activity"))
        if not added:
            return None, []
        self.stats["activity_hub"] = 1
        self.stats["activity_hub_links"] = len(added)
        return hub, added

    def _segment_of(self, offset: int) -> int | None:
        for segment in self.segments:
            if segment.start <= offset < segment.end:
                return segment.index
        return None

    async def verify(self, objects: list[_Candidate], relations: list[ExtractedRelation]) -> list[ExtractedRelation]:
        """Check the doubtful relationships against their context (the research pipelines'
        evidence- and definition-grounded verification, recall-preserving): a relationship
        judged not supported is dropped only below ``verify_drop_below``; unsure or
        mid-scored ones stay with their score as STIX confidence; a re-typing needs a sure
        verdict. ``verify_score_only`` drops nothing (offline threshold sweeps)."""
        from app.services.content_safety import wrap_untrusted

        by_ref = {o.ref: o for o in objects}
        doubtful = [r for r in relations if self._suspicious(r, by_ref)]
        self.stats["relationships_verified"] = len(doubtful)
        batches = [doubtful[i : i + _VERIFICATION_BATCH] for i in range(0, len(doubtful), _VERIFICATION_BATCH)]
        answers = await asyncio.gather(
            *(
                self.call(
                    step="verification",
                    system=verification_system_prompt(),
                    user=VERIFICATION_USER.format(
                        candidates=wrap_untrusted(
                            "\n".join(self._verification_card(i, r, by_ref) for i, r in enumerate(batch)),
                            "report relationships",
                            stable=True,
                        )
                    ),
                    schema=VERIFICATION_SCHEMA,
                )
                for batch in batches
            )
        )
        dropped: set[int] = set()
        for batch, answer in zip(batches, answers, strict=True):
            if answer is None:
                self.stats["failed_verification_batches"] += 1
                continue
            verdicts = {v.get("id"): v for v in answer.get("verdicts", []) if isinstance(v, dict)}
            for index, relation in enumerate(batch):
                verdict = verdicts.get(f"R{index}")
                if verdict is None:
                    continue
                raw = verdict.get("score")
                score = min(1.0, max(0.0, float(raw))) if isinstance(raw, (int, float)) else 0.5
                label = verdict.get("verdict")
                relation.verified = score
                self.stats[f"verdict_{label}"] += 1
                if self.options.verify_score_only:
                    continue
                if label == "not_supported" and score < self.options.verify_drop_below:
                    dropped.add(id(relation))
                    continue
                proposed = str(verdict.get("type") or relation.type)
                if label == "supported" and score >= _RETYPE_MIN_SCORE and proposed != relation.type:
                    verb = resolve_relation(by_ref[relation.source].kind, proposed, by_ref[relation.target].kind)
                    if verb is not None:
                        self.stats["relationships_retyped"] += 1
                        relation.type = verb
        self.stats["relationships_not_supported"] = len(dropped)
        unique: dict[tuple[str, str, str], ExtractedRelation] = {}
        for relation in relations:
            if id(relation) not in dropped:
                unique.setdefault((relation.source, relation.type, relation.target), relation)
        return list(unique.values())

    def _new_entities(
        self, entities: Iterable[dict], chunk: Chunk, objects: list[_Candidate], by_ref: dict[str, _Candidate]
    ) -> dict[str, str]:
        """Entities the relationship step needed, grounded and merged into the inventory;
        returns their local ref → inventory ref."""
        local: dict[str, str] = {}
        for entity in entities:
            kind, name, ref = entity.get("kind"), str(entity.get("name") or "").strip(), entity.get("ref")
            if not isinstance(ref, str) or not isinstance(kind, str) or kind == "unknown" or not name:
                continue
            span = ground(self.text, name, self._segment_window(entity.get("segment"), chunk))
            if span is None:
                self.stats["ungrounded_new_entities"] += 1
                continue
            family, key = family_of(kind), surface_key(name)
            existing = next((o for o in objects if o.family == family and key in o.name_keys()), None)
            if existing is None:
                existing = _Candidate(
                    f"E{len(objects) + 1}", [kind], self.text[span[0] : span[1]], family, [span], [name],
                    recovered=True,
                )
                objects.append(existing)
                by_ref[existing.ref] = existing
                self.stats["recovered_entities"] += 1
            local[ref] = existing.ref
        return local

    def _validated(
        self, relation: dict, chunk: Chunk, by_ref: dict[str, _Candidate], local: dict[str, str]
    ) -> ExtractedRelation | None:
        source = local.get(relation.get("source"), relation.get("source"))
        target = local.get(relation.get("target"), relation.get("target"))
        if source not in by_ref or target not in by_ref or source == target:
            self.stats["relationships_unknown_endpoint"] += 1
            return None
        self.raw_relations.append(ExtractedRelation(source, str(relation.get("type") or ""), target))
        evidence = str(relation.get("evidence") or "").strip()
        grounded = bool(evidence) and ground(self.text, evidence, (chunk.start, chunk.end)) is not None
        resolved = resolve_triple(by_ref[source].kind, str(relation.get("type") or ""), by_ref[target].kind)
        if resolved is None:
            self.stats["relationships_off_ontology"] += 1
            self.off_ontology.append((by_ref[source].kind, str(relation.get("type") or ""), by_ref[target].kind))
            # A link the report states, between two kinds no verb of the matrix joins (a
            # persona fronting a group, a unit under a state organization): kept as STIX's
            # fallback, which OpenCTI takes between any two objects, when its quote is in
            # the chunk. Without a quote it may be a misread pair, and it is dropped.
            if not (self.options.stated_links and grounded):
                return None
            self.stats["relationships_kept_as_related"] += 1
            resolved = (RELATED_TO, False)
        verb, swapped = resolved
        if swapped:
            # Only allowed the other way round (a threat "using" a hash is what the hash
            # indicates): the same link, read in the direction the ontology knows.
            self.stats["relationships_swapped"] += 1
            source, target = target, source
        if verb != relation.get("type"):
            self.stats["relationships_remapped"] += 1
        window = self._segment_window(relation.get("segment"), chunk)
        if not grounded and not (self._mentioned_in(by_ref[source], window) or self._mentioned_in(by_ref[target], window)):
            self.stats["relationships_ungrounded"] += 1
            return None
        cited = relation.get("segment")
        return ExtractedRelation(
            source, verb, target, [" ".join(evidence.split())] if grounded else [],
            implied=relation.get("basis") == "implied",
            segments=[cited] if isinstance(cited, int) and 0 <= cited < len(self.segments) else [],
            remapped=verb != relation.get("type") or swapped,
        )

    @staticmethod
    def _mentioned_in(obj: _Candidate, window: tuple[int, int]) -> bool:
        return any(window[0] <= start and end <= window[1] for start, end in obj.spans)


_DETECTION_PREFIX = re.compile(
    r"^(?:Backdoor|Trojan|Infostealer|HackTool|Ransom|Ransomware|Spyware|Worm|Rootkit|Win32|Win64|Exploit|Downloader|Dropper)[./:](?=\w)",
    re.IGNORECASE,
)
_VERSION_SUFFIX = re.compile(r"\s+v?\d+(?:\.\d+)+$", re.IGNORECASE)


def _sample_step(step: str, sample: int) -> str:
    """The step name of a call's *sample* (0-based): the step itself for the first, so a
    single reading is the call it always was, ``<step>_2``... for the others."""
    return step if sample == 0 else f"{step}_{sample + 1}"


def _added_by_others(per_sample: list[list[_Candidate]]) -> int:
    """The entities the samples after the first found that it did not: distinct by
    (family, folded name), as consolidation will merge them."""
    seen = {(c.family, key) for c in per_sample[0] for key in c.name_keys()}
    added = 0
    for found in per_sample[1:]:
        for candidate in found:
            slots = {(candidate.family, key) for key in candidate.name_keys()}
            if not slots & seen:
                added += 1
            seen |= slots
    return added


def _bare_forms(name: str) -> list[str]:
    """A software name without an antivirus detection prefix or a version suffix
    ("Backdoor.PowerExchange", "BunnyLoader 3.0"): looked up only when the written form
    is unknown to the knowledge base."""
    name = name.strip()
    forms = [stripped for stripped in (_DETECTION_PREFIX.sub("", name), _VERSION_SUFFIX.sub("", name)) if stripped != name]
    both = _VERSION_SUFFIX.sub("", _DETECTION_PREFIX.sub("", name))
    return list(dict.fromkeys([*forms, *([both] if both != name else [])]))


_THREAT_KINDS = frozenset({"malware", "tool", "intrusion-set", "threat-actor", "campaign", "infrastructure"})
def _technique_name_forms(catalogue: list) -> dict[str, tuple[str, ...]]:
    """ATT&CK id -> the names a table writes it by: its name, and a sub-technique's
    "Parent: Sub" (ATT&CK's own pages, "Application Layer Protocol: Web Protocols")."""
    by_id = {str(t.attack_id).upper(): t for t in catalogue}
    forms: dict[str, tuple[str, ...]] = {}
    for attack_id, technique in by_id.items():
        names = [technique.name]
        parent = by_id.get(attack_id.split(".")[0])
        if "." in attack_id and parent is not None:
            names.insert(0, f"{parent.name}: {technique.name}")
        forms[attack_id] = tuple(n for n in names if len(n) >= 4)
    return forms


#: Words a table's line may hold besides its names (connectors, ATT&CK's own words).
_TABLE_WORDS = frozenset({"and", "or", "of", "the", "via", "for", "to", "in", "on", "with", "from", "by", "a", "an"})


def _prose_words(text: str, start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    """Whether the line ``text[start:end]``, once its named objects are blanked out, still
    holds prose: a lower-case word that is not a connector. A mapping table writes names in
    title case and ids (``S0469``, ``T1078``, ``TA0001``); a sentence has verbs."""
    chars = list(text[start:end])
    for a, b in spans:
        for i in range(max(a, start) - start, min(b, end) - start):
            chars[i] = " "
    return any(
        word[0].islower() and word.lower() not in _TABLE_WORDS
        for word in re.findall(r"[^\W\d_][\w'-]*", "".join(chars))
    )


#: What a technique table lists, and who uses what it lists.
_TECHNIQUE_KINDS = frozenset({"attack-pattern", "attack-pattern-id"})
_TECHNIQUE_USERS = frozenset({"malware", "tool", "intrusion-set", "threat-actor", "campaign"})
_OBSERVABLE_KINDS = frozenset(
    {"ipv4-addr", "ipv6-addr", "domain-name", "url", "email-addr", "file-name", "md5", "sha1", "sha256", "sha512"}
)
#: Observables that are knowledge without any relationship (``knowledge_focus``): an IOC. A
#: URL or a domain alone is more often the article's own link than an indicator.
_STANDALONE_KINDS = frozenset(
    {"ipv4-addr", "ipv6-addr", "email-addr", "file-name", "md5", "sha1", "sha256", "sha512", "windows-registry-key"}
)


_ITEM = re.compile(r"[A-Za-z0-9][\w.+#&/'-]*(?: [\w.+#&/'()-]+){0,3}")


def _enumerations(text: str) -> list[list[tuple[int, int]]]:
    """Lists of at least four short names: comma- or semicolon-separated on one line
    ("BurpSuite, Charles, DumpIt, ..."), or one per line (bulleted or not). Each item is
    its offsets, trimmed; an item longer than four words ends the list (it is prose)."""
    lists: list[list[tuple[int, int]]] = []

    def item(start: int, end: int) -> tuple[int, int] | None:
        raw = text[start:end]
        lead = len(raw) - len(raw.lstrip(" \t*-•·"))
        body = raw.strip(" \t*-•·").rstrip(".:")
        body = re.sub(r"^(?:and|or) ", "", body)
        body = re.sub(r"^(?:the|The) ", "", body)
        if not body or len(body) > 40 or not _ITEM.fullmatch(body):
            return None
        first = start + lead + raw[lead:].find(body)
        return (first, first + len(body))

    for line in re.finditer(r"[^\n]+", text):
        # "A, B, C and D": the last two are two items.
        parts, run = re.split(r"\s*[,;]\s*|\s+(?:and|or)\s+(?=[A-Z0-9])", line.group(0)), []
        offset = line.start()
        for part in parts:
            at = text.find(part, offset, line.end())
            span = item(at, at + len(part)) if part and at >= 0 else None
            if span is None and at >= 0 and not run:
                # The list opens after a colon or a preposition: "checks for: BurpSuite",
                # "organizations in Germany", "tools such as Mimikatz".
                opener = list(re.finditer(r":\s*|\b(?:in|for|from|including|such as|like|against)\s+", part))
                if opener:
                    span = item(at + opener[-1].end(), at + len(part))
            offset = (at + len(part)) if at >= 0 else offset
            if span is None:
                if len(run) >= 4:
                    lists.append(run)
                run = []
            else:
                run.append(span)
        if len(run) >= 4:
            lists.append(run)
    run = []
    for line in re.finditer(r"([^\n]*)(?:\n|$)", text):
        if line.start() == len(text):
            break
        span = item(line.start(1), line.end(1)) if line.group(1).strip() else None
        if span is None or "," in line.group(1):
            if len(run) >= 4:
                lists.append(run)
            run = []
        else:
            run.append(span)
    if len(run) >= 4:
        lists.append(run)
    return lists


def _found_lines(relations: Iterable[dict]) -> str:
    lines = [f"{r.get('source')} {r.get('type')} {r.get('target')}" for r in relations if isinstance(r, dict)]
    return "\n".join(lines) or "(none)"


def _untrusted(chunk: Chunk) -> str:
    """A chunk as quarantined report content: the report is external data, never
    instructions (``content_safety.wrap_untrusted``); stable, so prompts stay cacheable."""
    from app.services.content_safety import wrap_untrusted

    return wrap_untrusted(chunk.numbered(), "report text", stable=True)


def _family_kinds(family: str) -> set[str]:
    return {
        "actor": {"intrusion-set", "threat-actor", "campaign"},
        "software": {"malware", "tool", "channel", "infrastructure", "product"},
        "location": {"country", "region", "city"},
        "identity": {"sector", "organization", "individual"},
        "technique": {"attack-pattern", "vulnerability"},
    }.get(family, set(LLM_KINDS)) | {"unknown"}


def _is_alias(alias: str, name: str) -> bool:
    """An alias is another name, not the name plus words ("APT-K-47 group" is APT-K-47
    with a generic word, "Fancy Bear" an alias of APT28); an antivirus detection name of
    the family ("Backdoor.Winnkit" for Winnkit) is one."""
    if _DETECTION_PREFIX.match(alias) and surface_key(_DETECTION_PREFIX.sub("", alias)) == surface_key(name):
        return True
    alias_key, name_key = surface_key(alias), surface_key(name)
    return bool(alias_key) and name_key not in alias_key and alias_key not in name_key


def _family_names(candidates: list[_Candidate]) -> None:
    """A malware or tool written under an antivirus detection name ("Backdoor.Winnkit",
    "Infostealer.Graphiron") is named by its family, the detection name kept as an alias:
    the vendor's prefix names its detection, not the family. Not when a knowledge base
    linked the name (ATT&CK names some families with the prefix, "Trojan.Karagany"), so it
    runs after linking and leaves linked candidates as they are."""
    for candidate in candidates:
        if candidate.kind not in ("malware", "tool") or candidate.external_ids:
            continue
        bare = _DETECTION_PREFIX.sub("", candidate.name).strip()
        if bare and bare != candidate.name:
            candidate.aliases = list(dict.fromkeys([candidate.name, *candidate.aliases]))
            candidate.name = bare


#: The opening a report's title is read from, and the longest title kept.
_TITLE_OPENING = 3_000
_TITLE_MAX = 300


async def _read_title(call: StructuredCall, text: str) -> tuple[str, str]:
    """The title the report's opening writes and the organization that published it, each
    as written; ``""`` for one the text does not hold (a guess, a name rewritten) or when
    the call fails."""
    from app.services.content_safety import wrap_untrusted

    opening = text[:_TITLE_OPENING]
    answer = await call(step="title", system=TITLE_SYSTEM,
                        user=TITLE_USER.format(opening=wrap_untrusted(opening, "report opening", stable=True)),
                        schema=TITLE_SCHEMA)
    flat = " ".join(text.split()).lower()

    def written(field: str, limit: int) -> str:
        value = " ".join(str((answer or {}).get(field) or "").split())
        return value if value and len(value) <= limit and value.lower() in flat else ""

    title = written("title", _TITLE_MAX)
    return (title if title.lower() in " ".join(opening.split()).lower() else ""), written("publisher", 100)


# A product's update, build or release: what OpenCTI keeps in ``version``, not in the name.
_PRODUCT_QUALIFIER = re.compile(
    r"\s+(?:(?:cumulative\s+update|service\s+pack|feature\s+pack|fix\s+pack|rtm|sp\d+|cu\d+)\b.*"
    r"|(?:update|hotfix|patch|build|releases?|versions?)\s+v?\d.*|v\d+(?:\.\d+)*\b.*|\d+(?:\.\d+)+\S*.*)$",
    re.IGNORECASE,
)
_ENUMERATION = re.compile(r"\s*(?:,\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+|\s*/\s*)", re.IGNORECASE)


def _product_names(pipeline: _Pipeline, objects: list[_Candidate], relations: list[ExtractedRelation]
                   ) -> tuple[list[_Candidate], list[ExtractedRelation]]:
    """Products named as analysts name them (``ExtractionOptions.product_names``)."""
    vendors = [o for o in objects if o.kind == "organization"]
    out: list[_Candidate] = []
    split: dict[str, list[str]] = {}
    flat = " ".join(pipeline.text.split()).lower()
    for obj in objects:
        if obj.kind != "product":
            out.append(obj)
            continue
        # The vendor resolution read for it, kept only when the report names it.
        stated_vendor = " ".join(obj.code.split())
        if stated_vendor and (stated_vendor.lower() not in flat or len(stated_vendor) > 60):
            stated_vendor = ""
        parts = [p.strip() for p in _ENUMERATION.split(obj.name) if p.strip()]
        items = [obj]
        if len(parts) > 1 and all(len(p.split()) <= 4 for p in parts):
            items = [replace(obj, ref=pipeline._ref("P"), name=part, aliases=[], kinds=list(obj.kinds),
                             spans=list(obj.spans), surfaces=list(obj.surfaces)) for part in parts]
            split[obj.ref] = [item.ref for item in items]
        for item in items:
            # A product is named "Vendor Product" (CPE's vendor:product, as OpenCTI keeps it)
            # whenever its vendor is known: the one resolution read, else the organization
            # written just before a mention ("JNC IAQS", "Microsoft Exchange").
            if stated_vendor:
                item.vendor = stated_vendor
                if not item.name.lower().startswith(stated_vendor.lower() + " ") and item.name.lower() != stated_vendor.lower():
                    item.aliases = list(dict.fromkeys([item.name, *item.aliases]))
                    item.name = f"{stated_vendor} {item.name}"
            for vendor in [] if stated_vendor else vendors:
                before = any(
                    " ".join(pipeline.text[max(0, a - len(vendor.name) - 1):a].split()).lower() == vendor.name.lower()
                    for a, _ in item.spans
                )
                if before or item.name.lower().startswith(vendor.name.lower() + " "):
                    item.vendor = vendor.name
                    if not item.name.lower().startswith(vendor.name.lower() + " "):
                        item.aliases = list(dict.fromkeys([item.name, *item.aliases]))
                        item.name = f"{vendor.name} {item.name}"
                    break
            qualifier = _PRODUCT_QUALIFIER.search(item.name)
            if qualifier and qualifier.start() > 0:
                item.version = " ".join(item.name[qualifier.start():].split())
                item.name = " ".join(item.name[: qualifier.start()].split())
            out.append(item)
    if split:
        rebuilt: list[ExtractedRelation] = []
        for relation in relations:
            for source in split.get(relation.source, [relation.source]):
                for target in split.get(relation.target, [relation.target]):
                    rebuilt.append(replace(relation, source=source, target=target))
        relations = rebuilt
        pipeline.stats["products_split"] = len(split)
    return out, relations


def _ransomware_twins(pipeline: _Pipeline, objects: list[_Candidate], relations: list[ExtractedRelation]
                      ) -> tuple[list[_Candidate], list[ExtractedRelation]]:
    """A brand the text calls ransomware is both its group and its malware
    (``ExtractionOptions.ransomware_twins``)."""
    flat = " ".join(pipeline.text.split()).lower()
    by_kind_name = {(o.kind, surface_key(o.name)): o for o in objects}
    added: list[_Candidate] = []
    links: list[ExtractedRelation] = []
    for obj in list(objects):
        if obj.kind not in ("intrusion-set", "malware"):
            continue
        written = re.search(re.escape(obj.name.lower()) + r"(?:'s)?\s+ransomware\b", flat)
        if not written:
            continue
        evidence = flat[written.start(): written.end()]
        group = obj if obj.kind == "intrusion-set" else by_kind_name.get(("intrusion-set", surface_key(obj.name)))
        malware = obj if obj.kind == "malware" else by_kind_name.get(("malware", surface_key(obj.name)))
        if malware is None:
            malware = replace(obj, ref=pipeline._ref("M"), kinds=["malware"], family=family_of("malware"), aliases=list(obj.aliases),
                              spans=list(obj.spans), surfaces=list(obj.surfaces), external_ids=[])
            added.append(malware)
        if group is None:
            group = replace(obj, ref=pipeline._ref("G"), kinds=["intrusion-set"], family=family_of("intrusion-set"), aliases=list(obj.aliases),
                            spans=list(obj.spans), surfaces=list(obj.surfaces), external_ids=[])
            added.append(group)
        malware.malware_types = ["ransomware"]
        if not any(r.source == group.ref and r.target == malware.ref and r.type == "uses" for r in relations):
            links.append(ExtractedRelation(group.ref, "uses", malware.ref, [evidence], implied=True, origin="ransomware"))
    pipeline.stats["ransomware_twins"] = len(added)
    return [*objects, *added], [*relations, *links]


@cache
def _sector_index() -> dict[str, frozenset[str]]:
    """Folded sector name or alias (generic words dropped) -> the OpenCTI sectors it names."""
    data = json.loads((Path(__file__).with_name("data") / "opencti_sectors.json").read_text(encoding="utf-8"))
    index: dict[str, set[str]] = defaultdict(set)
    for name, aliases in data["sectors"].items():
        for written in (name, *aliases):
            for key in _sector_keys(written):
                index[key].add(name)
    return {key: frozenset(found) for key, found in index.items()}


def _sector_keys(name: str) -> set[str]:
    """A sector name's folded forms: as written, without generic words, and without
    joining words and plurals ("Government and administrations" = "government
    administration")."""
    words = [w[:-1] if len(w) > 3 and w.endswith("s") else w
             for w in re.findall(r"[^\W_]+", name.lower()) if w not in ("and", "of", "the", "sector", "sectors", "industry", "industries")]
    return {k for k in (surface_key(name), core_key(name), "".join(words)) if k}


def _sector_names(candidates: list[_Candidate]) -> int:
    """Sectors renamed to OpenCTI's name when every name of theirs names one sector
    (``ExtractionOptions.sector_names``), the report's own words for it kept as aliases
    (``healthcare`` for ``Health``): OpenCTI matches on them; returns how many renamed."""
    index = _sector_index()
    renamed = 0
    for candidate in candidates:
        if candidate.kind != "sector":
            continue
        # NFKC: a PDF's ligatures ("ﬁnance") are the plain letters, never written as an alias.
        written = list(dict.fromkeys(unicodedata.normalize("NFKC", w)
                                     for w in (candidate.name, *candidate.surfaces, *candidate.aliases)))
        keys = {k for form in written for k in _sector_keys(form)}
        found = {s for key in keys for s in index.get(key, ())}
        if len(found) != 1:
            continue
        name = next(iter(found))
        own = list({surface_key(form): form for form in reversed(written)
                    if surface_key(form) != surface_key(name)
                    and any(name in index.get(k, ()) for k in _sector_keys(form))}.values())[::-1]
        candidate.aliases = list({surface_key(a): a for a in reversed([*own, *candidate.aliases])}.values())[::-1]
        if name != candidate.name:
            candidate.name = name
            renamed += 1
    return renamed


#: The catalogue's definitions embedded once per embedding function and catalogue.
_CATALOGUE_VECTORS: dict[tuple[int, int], tuple[list[str], object]] = {}


async def _nearest_techniques(embed: Embed, quotes: list[str], catalogue: list, k: int) -> list[list[str]] | None:
    """For each quote, the *k* catalogue techniques whose name and definition are nearest
    by cosine similarity; ``None`` when embedding fails (retrieval is an enrichment)."""
    import numpy as np

    key = (id(embed), len(catalogue))
    try:
        if key not in _CATALOGUE_VECTORS:
            ids = [t.attack_id.upper() for t in catalogue]
            texts = [f"{t.name}: {t.description[:600]}" for t in catalogue]
            vectors = await embed(texts)
            if not vectors or len(vectors) != len(texts):
                return None
            matrix = np.asarray(vectors, dtype=np.float32)
            matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
            _CATALOGUE_VECTORS[key] = (ids, matrix)
        ids, matrix = _CATALOGUE_VECTORS[key]
        found = await embed(quotes)
        if not found or len(found) != len(quotes):
            return None
    except Exception as exc:  # an enrichment, never a failure
        logger.warning("technique retrieval failed: %s", exc)
        return None
    q = np.asarray(found, dtype=np.float32)
    q /= np.linalg.norm(q, axis=1, keepdims=True) + 1e-9
    scores = q @ matrix.T  # type: ignore[operator]
    return [[ids[j] for j in np.argsort(-row)[:k]] for row in scores]


#: Which of our kinds a MISP galaxy cluster's kind may name.
_GALAXY_KINDS = {"actor": {"intrusion-set", "threat-actor"}, "ransomware": {"intrusion-set", "malware"},
                 "tool": {"tool", "malware"}}


@cache
def _galaxy_index() -> dict[str, frozenset[tuple[str, str]]]:
    """Folded name or synonym -> the MISP galaxy clusters ``(kind, name)`` it names; a name
    shorter than four characters names none."""
    data = json.loads((Path(__file__).with_name("data") / "misp_galaxy_aliases.json").read_text(encoding="utf-8"))
    index: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for kind, name, synonyms in data["clusters"]:
        for written in (name, *synonyms):
            key = surface_key(written)
            if len(key) >= 4:
                index[key].add((kind, name))
    return {key: frozenset(found) for key, found in index.items()}


def _galaxy_merge(candidates: list[_Candidate]) -> tuple[list[_Candidate], int]:
    """Objects of one kind naming one MISP galaxy cluster, as one object
    (``ExtractionOptions.galaxy_aliases``); the one an ATT&CK id identifies stays the head."""
    index = _galaxy_index()
    groups: dict[tuple[tuple[str, str], str], list[_Candidate]] = defaultdict(list)
    for candidate in candidates:
        # The clusters its names name that may be of its kind: one, or the name is ambiguous.
        clusters = {c for key in candidate.name_keys() for c in index.get(key, ()) if candidate.kind in _GALAXY_KINDS[c[0]]}
        if len(clusters) == 1:
            groups[(next(iter(clusters)), candidate.kind)].append(candidate)
    merged: set[str] = set()
    for (cluster, _), group in groups.items():
        head = next((c for c in group if c.external_ids), group[0])
        for other in group:
            if other is head:
                continue
            head.spans = sorted(set(head.spans) | set(other.spans))
            head.surfaces = list(dict.fromkeys([*head.surfaces, *other.surfaces]))
            head.aliases = list(dict.fromkeys([*head.aliases, other.name, *other.aliases]))
            merged.add(other.ref)
        if surface_key(cluster[1]) != surface_key(head.name):
            head.aliases = list(dict.fromkeys([*head.aliases, cluster[1]]))
    return [c for c in candidates if c.ref not in merged], len(merged)


def _technique_names(candidates: list[_Candidate], catalogue: list) -> None:
    """A technique known by its ATT&CK id takes ATT&CK's name, a sub-technique as ATT&CK
    shows it ("Abuse Elevation Control Mechanism: Bypass User Account Control"): the report
    may write only the id ("T1083") or its own words ("obfuscated files"), which stay as
    aliases. The id, not the name, identifies the object."""
    names = {entry.attack_id.upper(): entry.name for entry in catalogue if entry.name}
    for candidate in candidates:
        if candidate.kind != "attack-pattern-id":
            continue
        attack_id = (candidate.value or candidate.name).strip().upper()
        name = names.get(attack_id)
        if not name:
            continue
        parent = names.get(attack_id.split(".")[0]) if "." in attack_id else None
        canonical = f"{parent}: {name}" if parent else name
        if candidate.name == canonical:
            continue
        written = [] if candidate.name.strip().upper() == attack_id else [candidate.name]
        candidate.aliases = list(dict.fromkeys([*written, *([name] if parent else []), *candidate.aliases]))
        candidate.value = candidate.value or attack_id
        candidate.name = canonical


async def _platform_names(pipeline: _Pipeline, objects: list[_Candidate], lookup: PlatformNames) -> None:
    """Objects named as the platform the bundle goes to names them, the text's names
    kept as aliases: "Russia" is its "Russian Federation", "finance" its "Finance",
    "Fancy Bear" its intrusion set "APT28", so the bundle merges into the platform's
    objects instead of creating near-duplicates. A rename only when every name of the
    object the platform knows points to one object of its kind, whose name or alias is
    that name (case-folded); the platform's other aliases are not taken (each would be a
    merge key). A lookup that fails changes nothing: a platform enriches, never fails."""
    named = [o for o in objects if o.kind in _PLATFORM_NAMED_KINDS and o.name.strip()]
    pairs = sorted({(o.kind, n) for o in named for n in (o.name, *o.aliases) if n.strip()})
    if not pairs:
        return
    from app.integrations.providers.opencti.naming import MAX_NAMES

    if len(pairs) > MAX_NAMES:
        # Asked in turn from each kind, up to the lookup's bound: the rest keep their names.
        pipeline.stats["platform_names_not_asked"] = len(pairs) - MAX_NAMES
    try:
        found = await lookup(pairs)
    except Exception as exc:  # a platform is an enrichment, never a failure
        logger.warning("platform names lookup failed: %s", exc)
        found = None
    if found is None:
        pipeline.stats["platform_names_unavailable"] = 1
        return
    renamed: list[_Candidate] = []
    for obj in named:
        canonical: set[str] = set()
        for name in (obj.name, *obj.aliases):
            answer = found.get((obj.kind, name))
            if not answer:
                continue
            platform_name, platform_aliases = answer
            platform_name = " ".join(str(platform_name or "").split())
            held = {platform_name.casefold(), *(" ".join(str(a).split()).casefold() for a in platform_aliases or [])}
            if platform_name and " ".join(name.split()).casefold() in held:
                canonical.add(platform_name)
        if len(canonical) != 1:
            continue
        name = canonical.pop()
        if name == obj.name:
            continue
        obj.aliases = [n for n in dict.fromkeys([obj.name, *obj.aliases]) if n != name]
        obj.name = name
        renamed.append(obj)
    # Two objects of the text one platform object names alike are one bundle object
    # (one standard id): each carries the other's names, whichever the bundle keeps.
    by_name: dict[tuple[str, str], list[_Candidate]] = defaultdict(list)
    for obj in named:
        by_name[(obj.kind, obj.name.casefold())].append(obj)
    for group in by_name.values():
        if len(group) > 1 and any(obj in renamed for obj in group):
            aliases = list(dict.fromkeys(n for obj in group for n in (obj.name, *obj.aliases)))
            for obj in group:
                obj.aliases = [n for n in aliases if n != obj.name]
    pipeline.stats["platform_renamed"] = len(renamed)


def _sub_technique_name(candidate: _Candidate, alias: str) -> bool:
    """*alias* is ATT&CK's own name of the sub-technique *candidate* is named after
    ("Spearphishing Attachment" for "Phishing: Spearphishing Attachment", ``_technique_names``):
    another name, though the name contains it."""
    if not candidate.kind.startswith("attack-pattern") or ": " not in candidate.name:
        return False
    return surface_key(alias) == surface_key(candidate.name.split(": ", 1)[1])


def _to_object(candidate: _Candidate) -> ExtractedObject:
    return ExtractedObject(
        ref=candidate.ref,
        kind=candidate.kind,
        name=candidate.name,
        # A sector's aliases are the report's words for the sector OpenCTI names otherwise
        # (``_sector_names``: "government" for "Government and administrations"), kept.
        aliases=[a for a in dict.fromkeys(candidate.aliases)
                 if (candidate.kind == "sector" and surface_key(a) != surface_key(candidate.name))
                 or _sub_technique_name(candidate, a)
                 or _is_alias(a, candidate.name)],
        spans=list(candidate.spans),
        code=candidate.code,
        value=candidate.value,
        external_ids=list(candidate.external_ids),
        vendor=candidate.vendor,
        version=candidate.version,
        malware_types=list(candidate.malware_types),
    )


async def _catalogue(techniques: TechniqueCatalogue) -> list | None:
    try:
        return await techniques()
    except Exception as exc:  # a knowledge base is an enrichment, never a failure
        logger.warning("technique catalogue failed: %s", exc)
        return None


async def extract_stix(
    text: str,
    call: StructuredCall,
    *,
    usage: Usage | None = None,
    document_name: str = "",
    options: ExtractionOptions | None = None,
    knowledge: KnowledgeLookup | None = None,
    techniques: TechniqueCatalogue | None = None,
    read_title: bool = False,
    read_publisher: bool = False,
    platform_names: PlatformNames | None = None,
    embed: Embed | None = None,
) -> ExtractionResult:
    """Extract the STIX objects and relationships of *text* (see the module docstring).

    *knowledge* links names to a knowledge base (``mitre_attack.lookup_attack_names``);
    without it, names stay as the text writes them. *techniques* is the technique
    catalogue the ``behaviour_techniques`` option classifies behaviours against.
    *platform_names* names the objects as the platform the bundle goes to names those it
    holds (:func:`_platform_names`); without it, names stay the text's and ATT&CK's.
    *read_title*: the report is named by the title its opening writes, read by one call
    and kept only when the text holds it; *document_name* (else
    :func:`.bundle.default_report_name`) names it otherwise. The same call reads the
    organization that published the report, its ``created_by_ref``: with *read_title*, or
    with *read_publisher* alone when the caller names the report (a name the caller gives
    says nothing of who published it)."""
    options = options or ExtractionOptions()
    usage = usage or Usage()
    truncated = len(text) > options.max_chars
    text = text[: options.max_chars]
    title, publisher = await _read_title(call, text) if read_title or read_publisher else ("", "")
    title = title if read_title else ""
    document_name = title or document_name or default_report_name(text)
    pipeline = _Pipeline(text, call, usage, options)
    pipeline.embed = embed
    pipeline.stats["truncated_at_chars"] = options.max_chars if truncated else 0

    patterns = pipeline.pattern_candidates()
    anchors = [span for candidate in patterns for span in candidate.spans]
    detected = await pipeline.detect(build_chunks(pipeline.segments, options.chunk_chars), anchors)
    detected = pipeline.complete_enumerations(detected, anchors)
    consolidated = pipeline.consolidate(detected)
    stages: dict[str, list] = {"detected": [_to_object(c) for c in consolidated]}
    pipeline.propagate(consolidated, anchors)
    resolved = pipeline.consolidate(await pipeline.resolve(consolidated))
    pipeline.propagate(resolved, anchors)

    stages["resolved"] = [_to_object(c) for c in resolved]
    resolved = pipeline.link_technique_names(resolved, patterns)
    # The ATT&CK catalogue, loaded once: every step that reads it reads this one, and one
    # that cannot be loaded (no network, an air-gapped deployment) is said once.
    catalogue = await _catalogue(techniques) if techniques is not None else None
    if techniques is not None and not catalogue:
        pipeline.stats["technique_catalogue_unavailable"] = 1
    if knowledge is not None:
        resolved = await pipeline.link_knowledge(resolved, patterns, knowledge)
    if options.technique_linking and catalogue:
        resolved = await pipeline.link_techniques(resolved, patterns, catalogue)
    _family_names(resolved)
    if options.galaxy_aliases:
        resolved, merged = _galaxy_merge(resolved)
        pipeline.stats["galaxy_merged"] = merged
    if options.sector_names:
        pipeline.stats["sectors_named"] = _sector_names(resolved)
    objects = [*resolved, *patterns]
    if options.structural_techniques and techniques is not None:
        pipeline.technique_names = _technique_name_forms(catalogue or [])
    relations = await pipeline.relate(objects)
    if options.cross_chunk_relations:
        relations += await pipeline.cross_chunk(objects, relations)
    if options.verify_relations and relations:
        relations = await pipeline.verify(objects, relations)
    if options.behaviour_techniques and catalogue:
        known = {(r.source, r.type, r.target) for r in relations}
        size = options.behaviour_chunk_chars or options.chunk_chars
        added = await pipeline.behaviour_techniques(build_chunks(pipeline.segments, size), objects, catalogue)
        relations += [r for r in added if (r.source, r.type, r.target) not in known]
    # A URL the text writes only as a rendered hyperlink is its anchor's link: the line
    # "BianLian (https://site/tag/bianlian/)" names a threat beside a URL without saying the
    # URL is a sign of it. The URL stays an object; only its indicates links go.
    links = {
        obj.ref for obj in objects
        if obj.kind == "url" and obj.spans and all(rendered_link(text, a, b) for a, b in obj.spans)
    }
    kept = [r for r in relations if not (r.type == "indicates" and r.source in links)]
    pipeline.stats["indicates_from_hyperlinks_dropped"] = len(relations) - len(kept)
    relations = kept
    # A threat's origin is an attribution, recorded only when the text states it: "used by
    # Russia-linked actors" says nothing of where a malware comes from.
    attributed = [r for r in relations if not (r.type == "originates-from" and r.implied)]
    pipeline.stats["implied_origins_dropped"] = len(relations) - len(attributed)
    relations = attributed
    pipeline.stats["implied_relationships"] = sum(r.implied for r in relations)
    if not options.include_implied:
        relations = [r for r in relations if not r.implied]
    if options.relevance_review and objects:
        dropped = await pipeline.review_relevance(objects, relations)
        objects = [obj for obj in objects if obj.ref not in dropped]
        relations = [r for r in relations if r.source not in dropped and r.target not in dropped]
    if options.activity_hub and objects:
        hub, hub_links = await pipeline.activity_hub(objects, relations)
        if hub is not None:
            objects = [*objects, hub]
            relations = [*relations, *hub_links]
    if catalogue and any(obj.kind == "attack-pattern-id" for obj in objects):
        _technique_names(objects, catalogue)
    # Only with a catalogue: without one no technique could be linked to ATT&CK, and the
    # filter would leave out every technique the report names in words.
    if options.attack_techniques_only and catalogue:
        unlinked = {obj.ref for obj in objects if obj.kind == "attack-pattern"}
        objects = [obj for obj in objects if obj.ref not in unlinked]
        relations = [r for r in relations if r.source not in unlinked and r.target not in unlinked]
        pipeline.stats["attack_patterns_without_attack_id"] = len(unlinked)
    if options.product_names:
        objects, relations = _product_names(pipeline, objects, relations)
    if options.ransomware_twins:
        objects, relations = _ransomware_twins(pipeline, objects, relations)
    if platform_names is not None:
        # Last, once every step that reads an object's name in the text has run (a
        # vendor before a product, "X ransomware", the relevance review) and the
        # relationship step's own entities are in: relationships hold refs, not names.
        await _platform_names(pipeline, objects, platform_names)
    extracted = [_to_object(obj) for obj in objects]
    if options.knowledge_focus:
        linked = {r.source for r in relations} | {r.target for r in relations}
        # An attack pattern known only by name (a CAPEC entry a CVE page lists) is not an
        # adversary technique of the report: kept only when linked to ATT&CK.
        # A CVE or an ATT&CK id the report writes out is a fact of the report: kept alone too.
        written = {c.ref for c in patterns if c.kind in ("cve", "attack-pattern-id")}
        focused = [
            obj for obj in extracted
            if (obj.ref in linked or obj.kind in _STANDALONE_KINDS or obj.ref in written)
            and not (obj.kind == "attack-pattern" and not any(src == "mitre-attack" for src, _ in obj.external_ids))
        ]
        kept_refs = {obj.ref for obj in focused}
        relations = [r for r in relations if r.source in kept_refs and r.target in kept_refs]
        pipeline.stats["focus_dropped"] = len(extracted) - len(focused)
        extracted = focused
    if not options.include_observables:
        # Every object the bundle writes as an observable, a detected product (STIX
        # ``software``) as well as a pattern match: none is asked for.
        from app.services.stix_extraction.bundle import is_observable

        observed = {obj.ref for obj in extracted if is_observable(obj)}
        extracted = [obj for obj in extracted if obj.ref not in observed]
        relations = [r for r in relations if r.source not in observed and r.target not in observed]
    stages["patterns"] = [_to_object(c) for c in patterns]
    stages["final"] = extracted
    stages["relations_raw"] = pipeline.raw_relations
    stages["relations_final"] = relations
    stages["relations_off_ontology"] = pipeline.off_ontology
    stages["technique_scores"] = pipeline.technique_scores
    bundle = build_bundle(
        extracted,
        relations,
        document_name=document_name,
        include_report=options.include_report,
        include_evidence=options.include_evidence,
        references=pipeline.cited,
        published=published_date(text),
        author=publisher,
        graded_confidence=options.graded_confidence,
    )
    stats = {
        **dict(sorted(pipeline.stats.items())),
        "segments": len(pipeline.segments),
        "objects": sum(1 for obj in bundle["objects"] if obj["type"] not in ("relationship", "report")),
        "usage": usage.as_dict(),
    }
    return ExtractionResult(bundle, extracted, relations, stats, stages, document_name)


__all__ = [
    "ExtractionOptions", "ExtractionResult", "KnowledgeLookup", "PlatformNames", "TechniqueCatalogue", "extract_stix",
]

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

"""STIX_BUNDLE — entity and relationship scores over an *aligned* pairing of objects.

``stix.entity_prf`` and ``stix.relationship_prf`` compare canonical identifiers: an
object counts only when the prediction names it exactly as the reference does, once
case and whitespace are folded. A reviewer reads a report's objects more leniently than
that, and the extraction literature scores them the same way (document-level, entity
centric, one prediction per reference object):

- **One object, many names.** ``U.S.``, ``USA`` and ``United States`` are one country;
  ``APT-K-47`` and ``APT K 47`` one intrusion set; ``T1566`` the attack pattern whatever
  its name says. An object is the set of its names (``name``, ``aliases``,
  ``x_opencti_aliases``, ``x_mitre_id``, the external ids of its references, an
  observable's ``value`` and digests), each folded by :func:`name_key`.
- **One ontology, coarser than STIX types.** OpenCTI and annotators disagree on whether a
  named group is a ``threat-actor`` or an ``intrusion-set``: both are an *actor*. A
  location is told apart by its ``x_opencti_location_type``, an identity by its
  ``identity_class`` (a sector is an identity of class ``class``). Classes that people
  confuse (malware/tool, sector/organization, country/region) are *compatible*: no credit
  in the aligned metrics, half credit in the soft one.
- **One-to-one.** Reference and predicted objects are paired by optimal assignment
  (Hungarian, :func:`pair_entries`) over the credit each pair earns, so a prediction
  that splits ``U.S. state government`` into ``U.S.`` and ``state government`` earns one
  match at most and the other fragment is spurious, reported under ``fragments``.

Relationships are scored after the pairing: a predicted triple's endpoints are read as
the reference objects they were paired with, so the triple counts whichever mention of
``APT41`` the extractor attached it to; objects the pairing left unpaired are paired by
name alone for this (:func:`endpoint_map`), so a differently typed endpoint costs the
entity score, not the relationship one too.

Containers (``report``, ``grouping``, ``note``, ``opinion``, ``observed-data``) are the
envelope of an analysis, not its knowledge, and are left out of the aligned scores. An
indicator ``based-on`` an observable of the bundle stands for that observable (STIX links
an observable to a threat through it): it is scored once, as the observable, and its
``based-on`` link not at all.

Pure and deterministic: no model, no embedding, no I/O. The thresholds are module
constants, part of the versioned contract.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.families.stix_bundle.canonical import (
    Canonicalizer,
    StixObjectTypeSelection,
)
from app.evaluation_metrics.quality.families.stix_bundle.envelope import (
    BundleStructureError,
    StixObject,
    extract_bundle_objects,
)
from app.evaluation_metrics.quality.families.structured_json.pairing import (
    MAX_ENTRY_PRODUCT,
    entry_product_exceeded,
    pair_entries,
)
from app.evaluation_metrics.quality.primitives.empty_answer import (
    pooled_f1_counts,
    prf_with_empty_answer,
)
from app.evaluation_metrics.quality.results import (
    JSONValue,
    MetricDirection,
    MetricMetadata,
    MetricRollup,
    MetricStatus,
    ScoreResult,
)

ALIGNED_ENTITY_META = MetricMetadata(
    key="stix.entity_prf.aligned",
    label="Entity F1, Aligned",
    kind="prf",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)
SOFT_ENTITY_META = MetricMetadata(
    key="stix.entity_prf.soft",
    label="Entity F1, Soft",
    kind="prf",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)
ALIGNED_RELATIONSHIP_META = MetricMetadata(
    key="stix.relationship_prf.aligned",
    label="Relationship F1, Aligned",
    kind="prf",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)
CLOSED_ENTITY_META = MetricMetadata(
    key="stix.entity_prf.aligned.closed",
    label="Entity F1, Aligned, Dataset Types",
    kind="prf",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)
CLOSED_RELATIONSHIP_META = MetricMetadata(
    key="stix.relationship_prf.aligned.closed",
    label="Relationship F1, Aligned, Dataset Types",
    kind="prf",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)

#: Two names whose folded forms are at least this similar (``difflib`` ratio) name one
#: object: a typo, a missing hyphen, a plural. Below it, no credit.
NAME_SIMILARITY_THRESHOLD = 0.85
#: Credit of a pair whose names match only once generic words are dropped
#: (``Lazarus Group`` / ``Lazarus``, ``defense sector`` / ``defense``).
CORE_NAME_CREDIT = 0.9
#: Type credit of two compatible classes in the soft score (malware/tool, ...).
COMPATIBLE_TYPE_CREDIT = 0.5

_CONTAINER_TYPES = frozenset({"report", "grouping", "note", "opinion", "observed-data"})

# Words that qualify a name without being part of it. Dropped only for the *core* key,
# which earns CORE_NAME_CREDIT, never for the exact key.
_GENERIC_WORDS = frozenset(
    {
        "the", "group", "groups", "gang", "team", "crew", "actor", "actors", "threat",
        "apt", "ransomware", "malware", "backdoor", "trojan", "rat", "loader", "stealer",
        "implant", "family", "tool", "toolkit", "framework", "sector", "sectors",
        "industry", "industries", "vertical", "verticals", "campaign", "operation",
        "botnet", "variant", "strain",
    }
)

# Short forms a reviewer reads as the country's name. Keys and values are folded names.
_COUNTRY_SYNONYMS: Mapping[str, str] = {
    "us": "unitedstates",
    "usa": "unitedstates",
    "unitedstatesofamerica": "unitedstates",
    "america": "unitedstates",
    "uk": "unitedkingdom",
    "greatbritain": "unitedkingdom",
    "britain": "unitedkingdom",
    "russianfederation": "russia",
    "prc": "china",
    "peoplesrepublicofchina": "china",
    "mainlandchina": "china",
    "dprk": "northkorea",
    "democraticpeoplesrepublicofkorea": "northkorea",
    "rok": "southkorea",
    "republicofkorea": "southkorea",
    "korearepublicof": "southkorea",
    "islamicrepublicofiran": "iran",
    "iranislamicrepublicof": "iran",
    "uae": "unitedarabemirates",
    "czechrepublic": "czechia",
    "turkiye": "turkey",
    "holland": "netherlands",
    "thenetherlands": "netherlands",
    "ivorycoast": "cotedivoire",
    "syrianarabrepublic": "syria",
    "republicofmoldova": "moldova",
    "laopeoplesdemocraticrepublic": "laos",
    "taiwanprovinceofchina": "taiwan",
    "republicofchina": "taiwan",
    "ksa": "saudiarabia",
    "kingdomofsaudiarabia": "saudiarabia",
}

_ALIAS_FIELDS = ("aliases", "x_opencti_aliases")
_DASHES = re.compile(r"[‐-―−­]")
_QUOTES = re.compile(r"[‘’‚‛ʼ`´]")
_POSSESSIVE = re.compile(r"'s\b")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_WORD = re.compile(r"[0-9a-z]+")


def _fold(text: str) -> str:
    """Unicode-compatible, case-folded, ASCII-preferring text with one kind of dash."""
    text = unicodedata.normalize("NFKC", text)
    text = _DASHES.sub("-", _QUOTES.sub("'", text))
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def name_key(name: str) -> str:
    """The folded form two names of one object share.

    Case, accents, dashes, punctuation, spacing and a possessive ``'s`` are dropped,
    and a leading ``the``: ``The U.S.``, ``US`` and ``U S`` fold to ``us``.
    """
    folded = _POSSESSIVE.sub("", _fold(name)).strip()
    folded = re.sub(r"^the\s+", "", folded)
    return _NON_ALNUM.sub("", folded)


def _singular(word: str) -> str:
    """A plural folded to its singular, roughly: ``technologies`` → ``technology``,
    ``governments`` → ``government``; short words and ``-ss`` kept."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _core_key(name: str) -> str:
    """:func:`name_key` once generic qualifying words are dropped and plurals folded;
    ``""`` when only generic words remain."""
    words = _WORD.findall(_POSSESSIVE.sub("", _fold(name)))
    return "".join(_singular(word) for word in words if word not in _GENERIC_WORDS)


def type_class(obj: StixObject) -> str:
    """The ontology class an object is aligned under (coarser than its STIX type)."""
    if obj.type in ("intrusion-set", "threat-actor"):
        return "actor"
    if obj.type == "location":
        kind = obj.data.get("x_opencti_location_type")
        if isinstance(kind, str) and kind.strip():
            return f"location:{kind.strip().lower()}"
        # Plain STIX says it with its properties: a city names one, a region without a
        # country is a region, a country code alone a country.
        if obj.data.get("city"):
            return "location:city"
        if obj.data.get("region") and not obj.data.get("country"):
            return "location:region"
        if obj.data.get("country"):
            return "location:country"
        return "location"
    if obj.type == "identity":
        kind = obj.data.get("identity_class")
        return f"identity:{str(kind).strip().lower()}" if isinstance(kind, str) else "identity"
    return obj.type


# Pairs of classes a reviewer confuses, symmetric. Credited only by the soft score.
_COMPATIBLE: frozenset[frozenset[str]] = frozenset(
    frozenset(pair)
    for pair in (
        ("malware", "tool"),
        ("actor", "campaign"),
        ("identity:class", "identity:organization"),
        ("location:country", "location:region"),
        ("location:region", "location:city"),
        ("location:country", "location:administrative-area"),
        ("location:region", "location:administrative-area"),
        ("location:city", "location:administrative-area"),
        ("location", "location:country"),
        ("location", "location:region"),
        ("location", "location:city"),
        ("identity", "identity:organization"),
        ("identity", "identity:class"),
        ("identity", "identity:individual"),
        ("domain-name", "url"),
        ("ipv4-addr", "ipv6-addr"),
    )
)


def type_credit(left: str, right: str) -> float:
    """1 for one class, :data:`COMPATIBLE_TYPE_CREDIT` for compatible ones, else 0."""
    if left == right:
        return 1.0
    return COMPATIBLE_TYPE_CREDIT if frozenset((left, right)) in _COMPATIBLE else 0.0


def _external_ids(obj: StixObject) -> Iterable[str]:
    references = obj.data.get("external_references")
    if not isinstance(references, list):
        return
    for reference in references:
        if isinstance(reference, Mapping):
            external_id = reference.get("external_id")
            if isinstance(external_id, str) and external_id.strip():
                yield external_id


def object_names(obj: StixObject) -> tuple[str, ...]:
    """Every name an object answers to, in a stable order, without duplicates."""
    names: list[str] = []
    for field in ("name", "value", "x_mitre_id"):
        value = obj.data.get(field)
        if isinstance(value, str) and value.strip():
            names.append(value)
    for field in _ALIAS_FIELDS:
        value = obj.data.get(field)
        if isinstance(value, list):
            names.extend(alias for alias in value if isinstance(alias, str) and alias.strip())
    hashes = obj.data.get("hashes")
    if isinstance(hashes, Mapping):
        names.extend(digest for digest in hashes.values() if isinstance(digest, str))
    names.extend(_external_ids(obj))
    return tuple(dict.fromkeys(names))


@dataclass(frozen=True, slots=True)
class AlignedObject:
    """An object as the alignment reads it: its id, class and folded names."""

    id: str
    type: str
    type_class: str
    display: str
    keys: frozenset[str]
    core_keys: frozenset[str]


def _keys(obj: StixObject, names: Sequence[str]) -> tuple[frozenset[str], frozenset[str]]:
    keys = {key for key in (name_key(name) for name in names) if key}
    if type_class(obj).startswith("location"):
        keys |= {_COUNTRY_SYNONYMS[key] for key in keys if key in _COUNTRY_SYNONYMS}
    cores = {core for core in (_core_key(name) for name in names) if core}
    return frozenset(keys), frozenset(cores)


def proxies(objects: Sequence[StixObject]) -> dict[str, str]:
    """Indicator id → the id of the observable of the bundle it is ``based-on``."""
    present = {obj.id for obj in objects}
    mapping: dict[str, str] = {}
    for obj in objects:
        if obj.type != "relationship" or obj.data.get("relationship_type") != "based-on":
            continue
        source, target = obj.data.get("source_ref"), obj.data.get("target_ref")
        if isinstance(source, str) and source.startswith("indicator--") and target in present:
            mapping.setdefault(source, str(target))
    return mapping


def aligned_objects(
    objects: Sequence[StixObject], selection: StixObjectTypeSelection
) -> list[AlignedObject]:
    """The scored objects of a bundle, one per id, containers and the indicators standing
    for one of its observables left out."""
    seen: set[str] = set()
    aligned: list[AlignedObject] = []
    stand_ins = proxies(objects)
    for obj in objects:
        if obj.type in _CONTAINER_TYPES or not selection.includes(obj.type) or obj.id in seen:
            continue
        if obj.id in stand_ins:
            continue
        seen.add(obj.id)
        names = object_names(obj)
        keys, cores = _keys(obj, names)
        aligned.append(
            AlignedObject(
                id=obj.id,
                type=obj.type,
                type_class=type_class(obj),
                display=names[0] if names else obj.id,
                keys=keys or frozenset({obj.id}),
                core_keys=cores,
            )
        )
    return aligned


def name_credit(reference: AlignedObject, prediction: AlignedObject) -> float:
    """How well two objects' names agree, from 0 to 1, whatever their types.

    1 when they share a folded name; :data:`CORE_NAME_CREDIT` when they share one once
    generic words are dropped; otherwise the best ``difflib`` ratio of two folded names
    when it reaches :data:`NAME_SIMILARITY_THRESHOLD`, else 0.
    """
    if reference.keys & prediction.keys:
        return 1.0
    if reference.core_keys & prediction.core_keys:
        return CORE_NAME_CREDIT
    best = 0.0
    for left in reference.keys:
        for right in prediction.keys:
            # Identifiers (hashes, ATT&CK and CVE ids) differ by one character from
            # another object, and so do numbered names (APT28 / APT29, UNC2452 /
            # UNC2465): only names long enough to carry a typo, and naming the same
            # numbers, are compared.
            if min(len(left), len(right)) < 5 or _looks_like_identifier(left, right):
                continue
            if _DIGITS.findall(left) != _DIGITS.findall(right):
                continue
            ratio = difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()
            best = max(best, ratio)
    return best if best >= NAME_SIMILARITY_THRESHOLD else 0.0


_IDENTIFIER = re.compile(r"^(t\d{4}\d{0,3}|cve\d{4}\d{4,}|[0-9a-f]{32,128})$")
_DIGITS = re.compile(r"\d+")
_TECHNIQUE = re.compile(r"^t(\d{4})(\d{3})?$")


def _looks_like_identifier(left: str, right: str) -> bool:
    return bool(_IDENTIFIER.match(left) or _IDENTIFIER.match(right))


def technique_family_credit(reference: AlignedObject, prediction: AlignedObject) -> float:
    """:data:`COMPATIBLE_TYPE_CREDIT` when one names an ATT&CK technique and the other
    one of its sub-techniques (``T1059`` / ``T1059.001``), else 0. Soft score only."""
    def parents(keys: frozenset[str]) -> set[str]:
        return {match.group(1) for key in keys if (match := _TECHNIQUE.match(key))}

    shared = parents(reference.keys) & parents(prediction.keys)
    return COMPATIBLE_TYPE_CREDIT if shared else 0.0


@dataclass(frozen=True, slots=True)
class Alignment:
    """A one-to-one pairing of reference and predicted objects with its credits."""

    reference: tuple[AlignedObject, ...]
    prediction: tuple[AlignedObject, ...]
    pairs: tuple[tuple[int, int, float], ...]

    @property
    def matched_reference(self) -> frozenset[int]:
        return frozenset(i for i, _, _ in self.pairs)

    @property
    def matched_prediction(self) -> frozenset[int]:
        return frozenset(j for _, j, _ in self.pairs)

    def prediction_to_reference_ids(self) -> dict[str, str]:
        """Predicted object id → id of the reference object it was paired with."""
        return {
            self.prediction[j].id: self.reference[i].id for i, j, _ in self.pairs
        }


def align(
    reference: Sequence[AlignedObject],
    prediction: Sequence[AlignedObject],
    *,
    soft: bool,
) -> Alignment:
    """Pair reference and predicted objects one-to-one for the most credit in total.

    A pair's credit is its :func:`name_credit` times its :func:`type_credit`; the aligned
    scores (``soft=False``) credit only one class, any positive name credit counting
    as a full match. A pair earning nothing is no pair.
    """
    found: list[tuple[int, int, float]] = []
    for refs, preds in class_blocks(reference, prediction, soft=soft):
        credit = [
            [_pair_credit(reference[i], prediction[j], soft=soft) for j in preds] for i in refs
        ]
        pairing = pair_entries(credit, len(refs), len(preds))
        found += [
            (refs[a], preds[b], credit[a][b]) for a, b in pairing.pairs if credit[a][b] > 0.0
        ]
    return Alignment(tuple(reference), tuple(prediction), tuple(sorted(found)))


def class_blocks(
    reference: Sequence[AlignedObject], prediction: Sequence[AlignedObject], *, soft: bool
) -> list[tuple[list[int], list[int]]]:
    """The reference and predicted indexes that may pair, block by block: one class each
    for the aligned scores, the classes the soft score lets pair (compatible ones, joined
    transitively) for it. A pair across two blocks earns nothing, so pairing each block
    alone finds the same optimum as pairing the whole entry, and the compute bound is a
    block's, not the entry's: a long report of many kinds of objects is scored."""
    classes = sorted({obj.type_class for obj in (*reference, *prediction)})
    root = {name: name for name in classes}

    def find(name: str) -> str:
        while root[name] != name:
            root[name] = root[root[name]]
            name = root[name]
        return name

    if soft:
        for left in classes:
            for right in classes:
                if left < right and type_credit(left, right) > 0.0:
                    root[find(right)] = find(left)
    blocks: dict[str, tuple[list[int], list[int]]] = {}
    for i, ref in enumerate(reference):
        blocks.setdefault(find(ref.type_class), ([], []))[0].append(i)
    for j, pred in enumerate(prediction):
        blocks.setdefault(find(pred.type_class), ([], []))[1].append(j)
    return [blocks[name] for name in sorted(blocks) if blocks[name][0] and blocks[name][1]]


def _largest_block(reference: Sequence[AlignedObject], prediction: Sequence[AlignedObject]) -> int:
    """The largest reference x predicted product among the blocks either score pairs."""
    return max(
        (len(refs) * len(preds) for soft in (False, True)
         for refs, preds in class_blocks(reference, prediction, soft=soft)),
        default=0,
    )


def _pair_credit(reference: AlignedObject, prediction: AlignedObject, *, soft: bool) -> float:
    kind = type_credit(reference.type_class, prediction.type_class)
    if kind == 0.0 or (not soft and kind < 1.0):
        return 0.0
    names = name_credit(reference, prediction)
    if names == 0.0 and soft:
        names = technique_family_credit(reference, prediction)
    if names == 0.0:
        return 0.0
    # The aligned scores count a pair or not; the credit still steers the pairing
    # towards the closer name when one prediction could pair with two references.
    return names * kind if soft else 1.0 + names


def _fragments(alignment: Alignment) -> list[str]:
    """Unpaired predictions whose every name word belongs to a reference name: the
    pieces of an object the extractor split (``U.S.`` and ``state government`` for
    ``U.S. state government``)."""
    reference_words = [
        set(_WORD.findall(_fold(ref.display))) for ref in alignment.reference
    ]
    found: list[str] = []
    for j, pred in enumerate(alignment.prediction):
        if j in alignment.matched_prediction:
            continue
        words = set(_WORD.findall(_fold(pred.display)))
        if words and any(words < ref_words for ref_words in reference_words):
            found.append(pred.display)
    return sorted(found)


def _prf_detail(tp: float, fp: float, fn: float) -> dict[str, JSONValue]:
    precision, recall, f1 = _prf(tp, fp, fn)
    return {
        "precision": precision * 100.0,
        "recall": recall * 100.0,
        "f1": f1 * 100.0,
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def _prf(tp: float, fp: float, fn: float) -> tuple[float, float, float]:
    if isinstance(tp, int) and isinstance(fp, int) and isinstance(fn, int):
        return prf_with_empty_answer(tp, fp, fn)
    if tp == 0 and fp == 0 and fn == 0:
        return 1.0, 1.0, 1.0
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0
    return precision, recall, f1


def _pooled_counts(tp: float, fp: float, fn: float) -> dict[str, float]:
    if isinstance(tp, int) and isinstance(fp, int) and isinstance(fn, int):
        return dict(pooled_f1_counts(tp, fp, fn))
    from app.evaluation_metrics.core.contracts import POOLED_DENOMINATOR, POOLED_NUMERATOR

    return {POOLED_NUMERATOR: 2 * tp, POOLED_DENOMINATOR: 2 * tp + fp + fn}


def _by_type(counts: Mapping[str, list[float]]) -> dict[str, JSONValue]:
    """Per type, ``[tp, fp, fn]`` as precision, recall and F1 with the counts: the one F1 of
    an entry hides a type the answer misses (many objects, few relationships)."""
    out: dict[str, JSONValue] = {}
    for kind in sorted(counts):
        tp, fp, fn = (round(value, 6) for value in counts[kind])
        out[kind] = _prf_detail(tp, fp, fn)
    return out


def _entity_by_type(alignment: Alignment, *, soft: bool) -> dict[str, JSONValue]:
    """The entity counts per alignment class (a pair across compatible types counts under
    the reference's class)."""
    counts: dict[str, list[float]] = {}
    for i, j, credit in alignment.pairs:
        got = credit if soft else 1.0
        ref_row = counts.setdefault(alignment.reference[i].type_class, [0.0, 0.0, 0.0])
        ref_row[0] += got
        ref_row[2] += 1.0 - got
        counts.setdefault(alignment.prediction[j].type_class, [0.0, 0.0, 0.0])[1] += 1.0 - got
    for i, ref in enumerate(alignment.reference):
        if i not in alignment.matched_reference:
            counts.setdefault(ref.type_class, [0.0, 0.0, 0.0])[2] += 1.0
    for j, pred in enumerate(alignment.prediction):
        if j not in alignment.matched_prediction:
            counts.setdefault(pred.type_class, [0.0, 0.0, 0.0])[1] += 1.0
    return _by_type(counts)


def _entity_result(alignment: Alignment, metadata: MetricMetadata, *, soft: bool) -> ScoreResult:
    if soft:
        tp: float = round(sum(credit for _, _, credit in alignment.pairs), 6)
        fp: float = round(len(alignment.prediction) - tp, 6)
        fn: float = round(len(alignment.reference) - tp, 6)
    else:
        tp = len(alignment.pairs)
        fp = len(alignment.prediction) - tp
        fn = len(alignment.reference) - tp
    detail = _prf_detail(tp, fp, fn)
    detail.update(_pooled_counts(tp, fp, fn))
    detail["missing"] = sorted(
        ref.display for i, ref in enumerate(alignment.reference)
        if i not in alignment.matched_reference
    )
    detail["spurious"] = sorted(
        pred.display for j, pred in enumerate(alignment.prediction)
        if j not in alignment.matched_prediction
    )
    detail["fragments"] = _fragments(alignment)
    detail["by_type"] = _entity_by_type(alignment, soft=soft)
    if soft:
        detail["partial"] = sorted(
            f"{alignment.reference[i].display} ~ {alignment.prediction[j].display} ({credit:.2f})"
            for i, j, credit in alignment.pairs
            if credit < 1.0
        )
    f1 = float(detail["f1"])  # type: ignore[arg-type]
    return ScoreResult(status=MetricStatus.scored, value=f1, detail=detail, metadata=metadata)


_Triple = tuple[str, str, str]


def _triples(objects: Sequence[StixObject], id_map: Mapping[str, str] | None) -> set[_Triple]:
    triples: set[_Triple] = set()
    stand_ins = proxies(objects)
    for obj in objects:
        if obj.type != "relationship":
            continue
        source = obj.data.get("source_ref")
        kind = obj.data.get("relationship_type")
        target = obj.data.get("target_ref")
        if not (isinstance(source, str) and isinstance(kind, str) and isinstance(target, str)):
            continue
        if source in stand_ins:
            if kind.strip().lower() == "based-on":
                continue
            source = stand_ins[source]
        target = stand_ins.get(target, target)
        if id_map is not None:
            source, target = id_map.get(source, source), id_map.get(target, target)
        triples.add((source, kind.strip().lower(), target))
    return triples


def endpoint_map(alignment: Alignment) -> dict[str, str]:
    """Predicted object id → reference object id, for reading relationships: the entity
    pairing, completed by a name-only pairing of the objects it left unpaired. A
    relationship is about which objects it links; an endpoint typed differently (a tool
    the reference calls infrastructure) is already scored once, by the entity metrics, and
    must not also cost the relationship a miss and a spurious one."""
    mapping = alignment.prediction_to_reference_ids()
    reference = [r for i, r in enumerate(alignment.reference) if i not in alignment.matched_reference]
    prediction = [p for j, p in enumerate(alignment.prediction) if j not in alignment.matched_prediction]
    if reference and prediction and not entry_product_exceeded(len(reference), len(prediction)):
        credit = [[name_credit(ref, pred) for pred in prediction] for ref in reference]
        for i, j in pair_entries(credit, len(reference), len(prediction)).pairs:
            if credit[i][j] > 0.0:
                mapping[prediction[j].id] = reference[i].id
    return mapping


def reference_copies(alignment: Alignment) -> dict[str, set[str]]:
    """Predicted object id → the reference objects the pairing left unpaired that are of its
    class and share one of its folded names: a reference that records one entity as
    several objects (a victim, its acronym and a misspelling of it, each annotated alone)
    can pair only one of them with the one object that names them all. Read for the
    relationships only: the entity scores keep the one-to-one pairing."""
    by_key: dict[str, list[AlignedObject]] = {}
    for i, ref in enumerate(alignment.reference):
        if i not in alignment.matched_reference:
            for key in ref.keys:
                by_key.setdefault(key, []).append(ref)
    copies: dict[str, set[str]] = {}
    for pred in alignment.prediction:
        found = {
            ref.id for key in pred.keys for ref in by_key.get(key, ()) if ref.type_class == pred.type_class
        }
        if found:
            copies[pred.id] = found
    return copies


def _relationship_result(
    ref_objects: Sequence[StixObject], pred_objects: Sequence[StixObject], alignment: Alignment
) -> ScoreResult:
    names = {obj.id: obj.display for obj in (*alignment.reference, *alignment.prediction)}
    reference = _triples(ref_objects, None)
    mapping = endpoint_map(alignment)
    copies = reference_copies(alignment)
    # Each predicted triple as the pairing reads it, with the readings its endpoints'
    # other names allow: a reference that keeps one entity as several objects is found
    # once per object the prediction names.
    readings: dict[_Triple, set[_Triple]] = {}
    for source, kind, target in _triples(pred_objects, None):
        sources = {mapping.get(source, source), *copies.get(source, ())}
        targets = {mapping.get(target, target), *copies.get(target, ())}
        readings.setdefault((mapping.get(source, source), kind, mapping.get(target, target)), set()).update(
            (left, kind, right) for left in sources for right in targets
        )
    found = reference & set().union(*readings.values()) if readings else set()
    spurious = [triple for triple, read in readings.items() if not read & reference]
    tp = len(found)
    fp = len(spurious)
    fn = len(reference - found)
    detail = _prf_detail(tp, fp, fn)
    detail.update(_pooled_counts(tp, fp, fn))

    def readable(triple: _Triple) -> str:
        source, kind, target = triple
        return f"{names.get(source, source)} {kind} {names.get(target, target)}"

    detail["missing"] = sorted(readable(triple) for triple in reference - found)
    detail["spurious"] = sorted(readable(triple) for triple in spurious)
    counts: dict[str, list[float]] = {}
    for _, kind, _ in found:
        counts.setdefault(kind, [0.0, 0.0, 0.0])[0] += 1.0
    for _, kind, _ in spurious:
        counts.setdefault(kind, [0.0, 0.0, 0.0])[1] += 1.0
    for _, kind, _ in reference - found:
        counts.setdefault(kind, [0.0, 0.0, 0.0])[2] += 1.0
    detail["by_type"] = _by_type(counts)
    f1 = float(detail["f1"])  # type: ignore[arg-type]
    return ScoreResult(
        status=MetricStatus.scored, value=f1, detail=detail, metadata=ALIGNED_RELATIONSHIP_META
    )


def _skipped(metadata: MetricMetadata, reference: int, prediction: int, largest: int) -> ScoreResult:
    return ScoreResult(
        status=MetricStatus.skipped,
        value=None,
        detail={
            "reason": "too_many_objects",
            "reference_objects": reference,
            "predicted_objects": prediction,
            "largest_block_product": largest,
            "max_block_product": MAX_ENTRY_PRODUCT,
        },
        metadata=metadata,
    )


def _too_many_predicted(metadata: MetricMetadata, reference: int, prediction: int, largest: int) -> ScoreResult:
    """An answer too large to pair against a reference that fits: nothing credited, every
    reference item missed and every predicted one extra (0; 0 of 0, nothing expected and
    nothing given, is the empty answer's score, as everywhere)."""
    detail = _prf_detail(0, prediction, reference)
    detail.update(
        reason="prediction_too_many_objects",
        reference_objects=reference,
        predicted_objects=prediction,
        largest_block_product=largest,
        max_block_product=MAX_ENTRY_PRODUCT,
    )
    detail.update(_pooled_counts(0, prediction, reference))
    return ScoreResult(status=MetricStatus.scored, value=float(detail["f1"]), detail=detail, metadata=metadata)  # type: ignore[arg-type]


def stix_aligned_scores(
    reference: object,
    prediction: object,
    canonicalizer: Canonicalizer | None = None,
    selection: StixObjectTypeSelection | None = None,
    *,
    types: frozenset[str] | None = None,
) -> dict[str, ScoreResult]:
    """Score entities and relationships over an aligned pairing of objects.

    Args:
        reference: The reference STIX bundle.
        prediction: The predicted STIX bundle, already through the envelope gate.
        canonicalizer: Applied identically to both sides, as for the exact scores.
        selection: The object-type selection policy; containers are always left out.
        types: The object types the dataset's references annotate: the closed-world scores
            (:func:`stix_closed_world_scores`) are added, from the same canonical objects.

    Returns:
        ``stix.entity_prf.aligned``, ``stix.entity_prf.soft`` and
        ``stix.relationship_prf.aligned``, each ``scored`` with pooled counts. Past the
        pairing's compute bound, nothing is credited (``prediction_too_many_objects``) when the
        reference aligns with itself within it, else are ``skipped`` (``too_many_objects``); with
        *types*, the two closed-world scores too.

    Raises:
        ConfigurationError: If ``canonicalizer`` is missing or the reference is malformed.
        BundleStructureError: If the prediction is malformed after canonicalization.
    """
    selection = selection if selection is not None else StixObjectTypeSelection()
    ref_objects, pred_objects = _canonical_objects(reference, prediction, canonicalizer)
    results = _aligned_from_objects(ref_objects, pred_objects, selection, with_soft=True)
    if types is not None:
        results.update(_closed_from_objects(ref_objects, pred_objects, types, selection))
    return results


def _canonical_objects(
    reference: object, prediction: object, canonicalizer: Canonicalizer | None
) -> tuple[list[StixObject], list[StixObject]]:
    """Both bundles' objects, canonicalized once for every reading of the entry."""
    if canonicalizer is None:
        raise ConfigurationError("STIX aligned scoring requires a Canonicalizer")
    try:
        ref_objects = extract_bundle_objects(canonicalizer.canonicalize(reference))
    except BundleStructureError as exc:
        raise ConfigurationError(
            f"reference STIX bundle is malformed after canonicalization: {exc.message}"
        ) from None
    return list(ref_objects), list(extract_bundle_objects(canonicalizer.canonicalize(prediction)))


def _aligned_from_objects(
    ref_objects: Sequence[StixObject],
    pred_objects: Sequence[StixObject],
    selection: StixObjectTypeSelection,
    *,
    with_soft: bool,
) -> dict[str, ScoreResult]:
    """The aligned entity, soft entity (*with_soft*) and aligned relationship scores of
    canonical objects."""
    ref_aligned = aligned_objects(ref_objects, selection)
    pred_aligned = aligned_objects(pred_objects, selection)
    metas = (ALIGNED_ENTITY_META, *((SOFT_ENTITY_META,) if with_soft else ()), ALIGNED_RELATIONSHIP_META)
    largest = _largest_block(ref_aligned, pred_aligned)
    if largest > MAX_ENTRY_PRODUCT:
        if _largest_block(ref_aligned, ref_aligned) <= MAX_ENTRY_PRODUCT:
            # The reference aligns with itself: only the answer's size is past the bound. It
            # scores 0, as structured_json's does, so over-producing never escapes the score.
            # Relationships as a scored entry counts them: triples, an indicator read as its
            # observable (no `based-on`), duplicates once.
            relations = (len(_triples(ref_objects, None)), len(_triples(pred_objects, None)))
            return {
                meta.key: _too_many_predicted(
                    meta, *(relations if meta is ALIGNED_RELATIONSHIP_META else (len(ref_aligned), len(pred_aligned))), largest
                )
                for meta in metas
            }
        return {
            meta.key: _skipped(meta, len(ref_aligned), len(pred_aligned), largest) for meta in metas
        }
    strict = align(ref_aligned, pred_aligned, soft=False)
    results = {
        ALIGNED_ENTITY_META.key: _entity_result(strict, ALIGNED_ENTITY_META, soft=False),
        ALIGNED_RELATIONSHIP_META.key: _relationship_result(ref_objects, pred_objects, strict),
    }
    if with_soft:
        soft = align(ref_aligned, pred_aligned, soft=True)
        results[SOFT_ENTITY_META.key] = _entity_result(soft, SOFT_ENTITY_META, soft=True)
    return results


#: Object types a closed-world reading never removes: the relationships it keeps or drops by
#: their endpoints, and the containers and meta objects no score reads as knowledge.
_KEPT_IN_CLOSED_WORLD = frozenset({"relationship", "sighting", "marking-definition", "extension-definition",
                                   "language-content", *_CONTAINER_TYPES})


#: STIX types the pairing aligns under one class (``type_class``): a dataset annotating
#: one annotates the other.
_SAME_CLASS: Mapping[str, frozenset[str]] = {
    "intrusion-set": frozenset({"threat-actor"}),
    "threat-actor": frozenset({"intrusion-set"}),
}


def _closed_objects(objects: Sequence[StixObject], types: frozenset[str]) -> list[StixObject]:
    """*objects* keeping only those of *types* (the object types a dataset's references
    annotate) and the relationships between kept objects, read as the pairing reads them: a
    type of the same class as an annotated one is annotated (``threat-actor`` for
    ``intrusion-set``), and a kept indicator keeps the observable it is ``based-on`` (the
    pairing reads the indicator as it, ``proxies``)."""
    annotated = set(types).union(*(_SAME_CLASS.get(kind, frozenset()) for kind in types))
    dropped = {obj.id for obj in objects if obj.type not in annotated and obj.type not in _KEPT_IN_CLOSED_WORLD}
    if "indicator" in annotated:
        dropped -= {
            obj.data.get("target_ref") for obj in objects
            if obj.type == "relationship" and obj.data.get("relationship_type") == "based-on"
            and str(obj.data.get("source_ref") or "").startswith("indicator--")
        }
    return [
        obj for obj in objects
        if obj.id not in dropped
        and not (obj.type == "relationship"
                 and (obj.data.get("source_ref") in dropped or obj.data.get("target_ref") in dropped))
    ]


def _closed_from_objects(
    ref_objects: Sequence[StixObject],
    pred_objects: Sequence[StixObject],
    types: frozenset[str],
    selection: StixObjectTypeSelection,
) -> dict[str, ScoreResult]:
    """The closed-world scores of canonical objects: the strict pairing only (no soft
    reading is reported for it)."""
    aligned = _aligned_from_objects(ref_objects, _closed_objects(pred_objects, types), selection, with_soft=False)
    out: dict[str, ScoreResult] = {}
    for meta, source in ((CLOSED_ENTITY_META, ALIGNED_ENTITY_META), (CLOSED_RELATIONSHIP_META, ALIGNED_RELATIONSHIP_META)):
        result = aligned[source.key]
        out[meta.key] = result.model_copy(
            update={"metadata": meta, "detail": {**result.detail, "dataset_types": sorted(types)}}
        )
    if "relationship" not in types:
        out[CLOSED_RELATIONSHIP_META.key] = ScoreResult(
            status=MetricStatus.skipped,
            value=None,
            detail={"reason": "not_annotated_by_dataset", "dataset_types": sorted(types)},
            metadata=CLOSED_RELATIONSHIP_META,
        )
    return out


def stix_closed_world_scores(
    reference: object,
    prediction: object,
    types: frozenset[str],
    canonicalizer: Canonicalizer | None = None,
    selection: StixObjectTypeSelection | None = None,
) -> dict[str, ScoreResult]:
    """The aligned entity and relationship scores of the prediction reduced to *types*, the
    object types the dataset's references annotate (a closed world: what the dataset's
    guideline never annotates, a correct answer included, is neither a hit nor an error).
    Reported beside the aligned scores, never instead of them; :func:`stix_aligned_scores`
    with ``types`` computes both from one canonicalization.

    Returns:
        ``stix.entity_prf.aligned.closed`` and ``stix.relationship_prf.aligned.closed``,
        each as :func:`stix_aligned_scores` gives its aligned counterpart; the relationship
        score ``skipped`` (``not_annotated_by_dataset``) when *types* has no
        ``relationship``: a dataset that annotates none (indicator lists) has no relationship
        an answer could find or miss.
    """
    selection = selection if selection is not None else StixObjectTypeSelection()
    ref_objects, pred_objects = _canonical_objects(reference, prediction, canonicalizer)
    return _closed_from_objects(ref_objects, pred_objects, types, selection)


__all__ = [
    "ALIGNED_ENTITY_META",
    "ALIGNED_RELATIONSHIP_META",
    "CLOSED_ENTITY_META",
    "CLOSED_RELATIONSHIP_META",
    "SOFT_ENTITY_META",
    "align",
    "aligned_objects",
    "name_credit",
    "name_key",
    "proxies",
    "stix_aligned_scores",
    "stix_closed_world_scores",
    "type_class",
    "type_credit",
]

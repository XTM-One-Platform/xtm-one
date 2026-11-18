"""STIX_BUNDLE aligned scores: objects paired one-to-one over their names and classes.

The exact scores (``stix.entity_prf``) count an object only when the prediction names it
as the reference does. These pin what the aligned scores add: aliases, spelling and
ontology-class equivalence, one-to-one pairing, triples read through the pairing, and the
soft score's partial credit.
"""

from __future__ import annotations

import pytest

from app.evaluation_metrics.core.contracts import MetricStatus
from app.evaluation_metrics.quality.families.stix_bundle.alignment import (
    name_key,
    stix_aligned_scores,
)
from app.evaluation_metrics.quality.families.stix_bundle.metrics import stix_entity_scores


class _Identity:
    """Keeps ids as written, so a test reads its own ids in the details."""

    def canonicalize(self, bundle: object) -> object:
        return bundle


def _bundle(*objects: dict) -> dict:
    return {"type": "bundle", "id": "bundle--1", "objects": list(objects)}


def _sdo(stix_id: str, name: str, **extra: object) -> dict:
    return {"type": stix_id.split("--", 1)[0], "id": stix_id, "name": name, **extra}


def _country(stix_id: str, name: str) -> dict:
    return _sdo(stix_id, name, x_opencti_location_type="Country")


def _rel(stix_id: str, source: str, kind: str, target: str) -> dict:
    return {
        "type": "relationship",
        "id": stix_id,
        "source_ref": source,
        "relationship_type": kind,
        "target_ref": target,
    }


def _scores(reference: dict, prediction: dict) -> dict:
    return stix_aligned_scores(reference, prediction, _Identity())


def _counts(result) -> tuple[float, float, float]:
    return result.detail["tp"], result.detail["fp"], result.detail["fn"]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("U.S.", "US"),
        ("APT-K-47", "APT K 47"),
        ("The Lazarus’s", "lazarus"),
        ("Côte d’Ivoire", "cote d'ivoire"),
        ("T1566.001", "t1566.001"),
    ],
)
def test_names_of_one_object_fold_to_one_key(left: str, right: str) -> None:
    assert name_key(left) == name_key(right)


def test_an_alias_spelling_matches_where_the_exact_score_misses() -> None:
    reference = _bundle(_country("location--a", "United States"))
    prediction = _bundle(_country("location--b", "U.S."))

    exact = stix_entity_scores(reference, prediction, _Identity())["stix.entity_prf"]
    aligned = _scores(reference, prediction)["stix.entity_prf.aligned"]

    assert (exact.detail["tp"], exact.detail["fp"], exact.detail["fn"]) == (0, 1, 1)
    assert _counts(aligned) == (1, 0, 0)
    assert aligned.value == 100.0


def test_an_intrusion_set_and_a_threat_actor_of_one_name_are_one_actor() -> None:
    reference = _bundle(_sdo("intrusion-set--a", "APT41"))
    prediction = _bundle(_sdo("threat-actor--b", "APT 41", aliases=["Wicked Panda"]))

    assert _counts(_scores(reference, prediction)["stix.entity_prf.aligned"]) == (1, 0, 0)


def test_an_alias_names_the_object() -> None:
    reference = _bundle(_sdo("intrusion-set--a", "APT28", aliases=["Fancy Bear"]))
    prediction = _bundle(_sdo("intrusion-set--b", "Fancy Bear"))

    assert _counts(_scores(reference, prediction)["stix.entity_prf.aligned"]) == (1, 0, 0)


def test_numbered_names_never_match_by_similarity() -> None:
    reference = _bundle(_sdo("intrusion-set--a", "APT28"))
    prediction = _bundle(_sdo("intrusion-set--b", "APT29"))

    result = _scores(reference, prediction)
    assert _counts(result["stix.entity_prf.aligned"]) == (0, 1, 1)
    assert _counts(result["stix.entity_prf.soft"]) == (0.0, 1.0, 1.0)


def test_a_typo_matches_above_the_threshold() -> None:
    reference = _bundle(_sdo("malware--a", "WalkerShell"))
    prediction = _bundle(_sdo("malware--b", "WalkerShel"))

    assert _counts(_scores(reference, prediction)["stix.entity_prf.aligned"]) == (1, 0, 0)


def test_a_generic_word_only_costs_a_little_in_the_soft_score() -> None:
    reference = _bundle(_sdo("intrusion-set--a", "Lazarus Group"))
    prediction = _bundle(_sdo("intrusion-set--b", "Lazarus"))

    result = _scores(reference, prediction)
    assert _counts(result["stix.entity_prf.aligned"]) == (1, 0, 0)
    assert result["stix.entity_prf.soft"].detail["tp"] == pytest.approx(0.9)


def test_pairing_is_one_to_one() -> None:
    reference = _bundle(_sdo("malware--a", "WalkerShell"))
    prediction = _bundle(_sdo("malware--b", "WalkerShell"), _sdo("malware--c", "walkerShell"))

    assert _counts(_scores(reference, prediction)["stix.entity_prf.aligned"]) == (1, 1, 0)


def test_an_over_split_object_reports_its_fragments() -> None:
    reference = _bundle(
        _sdo("identity--a", "U.S. state government", identity_class="organization")
    )
    prediction = _bundle(
        _country("location--b", "U.S."),
        _sdo("identity--c", "state government", identity_class="organization"),
    )

    result = _scores(reference, prediction)["stix.entity_prf.aligned"]
    assert _counts(result) == (1, 1, 0)
    assert list(result.detail["fragments"]) == ["U.S."]


def test_a_confusable_class_earns_half_credit_in_the_soft_score_only() -> None:
    reference = _bundle(_sdo("malware--a", "Cobalt Strike"))
    prediction = _bundle(_sdo("tool--b", "Cobalt Strike"))

    result = _scores(reference, prediction)
    assert _counts(result["stix.entity_prf.aligned"]) == (0, 1, 1)
    assert _counts(result["stix.entity_prf.soft"]) == (0.5, 0.5, 0.5)
    assert result["stix.entity_prf.soft"].value == pytest.approx(50.0)


def test_a_sub_technique_earns_half_credit_against_its_technique() -> None:
    reference = _bundle(_sdo("attack-pattern--a", "Phishing", x_mitre_id="T1566"))
    prediction = _bundle(
        _sdo("attack-pattern--b", "Spearphishing Attachment", x_mitre_id="T1566.001")
    )

    result = _scores(reference, prediction)
    assert _counts(result["stix.entity_prf.aligned"]) == (0, 1, 1)
    assert result["stix.entity_prf.soft"].detail["tp"] == pytest.approx(0.5)


def test_relationships_are_read_through_the_pairing() -> None:
    reference = _bundle(
        _sdo("intrusion-set--a", "APT41"),
        _sdo("vulnerability--v", "CVE-2021-44207"),
        _rel("relationship--r", "intrusion-set--a", "targets", "vulnerability--v"),
    )
    prediction = _bundle(
        _sdo("threat-actor--x", "APT 41"),
        _sdo("vulnerability--y", "cve-2021-44207"),
        _rel("relationship--s", "threat-actor--x", "targets", "vulnerability--y"),
    )

    result = _scores(reference, prediction)["stix.relationship_prf.aligned"]
    assert _counts(result) == (1, 0, 0)
    assert result.value == 100.0


def test_a_relationship_to_an_unpaired_object_is_spurious_and_readable() -> None:
    reference = _bundle(
        _sdo("intrusion-set--a", "APT41"),
        _sdo("identity--g", "government", identity_class="class"),
        _rel("relationship--r", "intrusion-set--a", "targets", "identity--g"),
    )
    prediction = _bundle(
        _sdo("intrusion-set--x", "APT41"),
        _sdo("identity--h", "insurance", identity_class="class"),
        _rel("relationship--s", "intrusion-set--x", "targets", "identity--h"),
    )

    detail = _scores(reference, prediction)["stix.relationship_prf.aligned"].detail
    assert (detail["tp"], detail["fp"], detail["fn"]) == (0, 1, 1)
    assert list(detail["missing"]) == ["APT41 targets government"]
    assert list(detail["spurious"]) == ["APT41 targets insurance"]


def test_containers_are_not_scored() -> None:
    reference = _bundle(_sdo("malware--a", "Nimbo-C2"))
    prediction = _bundle(
        _sdo("malware--b", "Nimbo-C2"),
        _sdo("report--r", "A report", object_refs=["malware--b"]),
    )

    assert _counts(_scores(reference, prediction)["stix.entity_prf.aligned"]) == (1, 0, 0)


def test_two_empty_bundles_score_full_marks() -> None:
    result = _scores(_bundle(), _bundle())
    for key in ("stix.entity_prf.aligned", "stix.entity_prf.soft", "stix.relationship_prf.aligned"):
        assert result[key].status is MetricStatus.scored
        assert result[key].value == 100.0
        assert (result[key].detail["numerator"], result[key].detail["denominator"]) == (0, 0)


def test_pooled_counts_give_the_f1() -> None:
    reference = _bundle(_sdo("malware--a", "Nimbo-C2"), _sdo("malware--b", "DemoTrySpy"))
    prediction = _bundle(_sdo("malware--c", "Nimbo-C2"), _sdo("malware--d", "ORPCBackdoor"))

    result = _scores(reference, prediction)["stix.entity_prf.aligned"]
    assert (result.detail["numerator"], result.detail["denominator"]) == (2, 4)
    assert result.value == pytest.approx(50.0)


def test_a_plain_stix_country_is_a_country() -> None:
    reference = _bundle(_country("location--a", "Bangladesh"))
    prediction = _bundle({"type": "location", "id": "location--b", "name": "Bangladesh", "country": "BD"})

    assert _counts(_scores(reference, prediction)["stix.entity_prf.aligned"]) == (1, 0, 0)


def test_a_plural_and_a_generic_word_still_name_one_sector() -> None:
    reference = _bundle(_sdo("identity--a", "technologies", identity_class="class"))
    prediction = _bundle(_sdo("identity--b", "Technology Sector", identity_class="class"))

    result = _scores(reference, prediction)
    assert _counts(result["stix.entity_prf.aligned"]) == (1, 0, 0)
    assert result["stix.entity_prf.soft"].detail["tp"] == pytest.approx(0.9)


def test_a_differently_typed_endpoint_does_not_cost_the_relationship() -> None:
    reference = _bundle(
        _sdo("intrusion-set--a", "Hive"),
        _sdo("infrastructure--f", "FortiOS"),
        _rel("relationship--r", "intrusion-set--a", "uses", "infrastructure--f"),
    )
    prediction = _bundle(
        _sdo("intrusion-set--x", "Hive"),
        _sdo("tool--y", "FortiOS"),
        _rel("relationship--s", "intrusion-set--x", "uses", "tool--y"),
    )

    result = _scores(reference, prediction)
    assert _counts(result["stix.entity_prf.aligned"]) == (1, 1, 1)  # the typing is scored here
    assert _counts(result["stix.relationship_prf.aligned"]) == (1, 0, 0)  # and only here


def test_an_indicator_is_scored_as_the_observable_it_is_based_on() -> None:
    """A reference saying "this address indicates Emotet" and a prediction writing it
    through an indicator (STIX's way) name one link and one object."""
    reference = _bundle(
        _sdo("malware--1", "Emotet"),
        {"type": "ipv4-addr", "id": "ipv4-addr--1", "value": "45.77.1.2"},
        {"type": "indicator", "id": "indicator--1", "name": "45.77.1.2", "pattern": "[ipv4-addr:value = '45.77.1.2']"},
        _rel("relationship--1", "indicator--1", "based-on", "ipv4-addr--1"),
        _rel("relationship--2", "indicator--1", "indicates", "malware--1"),
    )
    prediction = _bundle(
        _sdo("malware--9", "Emotet"),
        {"type": "ipv4-addr", "id": "ipv4-addr--9", "value": "45.77.1.2"},
        {"type": "indicator", "id": "indicator--9", "name": "45.77.1.2", "pattern": "[ipv4-addr:value = '45.77.1.2']"},
        _rel("relationship--8", "indicator--9", "based-on", "ipv4-addr--9"),
        _rel("relationship--9", "indicator--9", "indicates", "malware--9"),
    )
    scores = _scores(reference, prediction)
    assert _counts(scores["stix.entity_prf.aligned"]) == (2, 0, 0)
    assert _counts(scores["stix.relationship_prf.aligned"]) == (1, 0, 0)


def test_a_long_report_of_many_kinds_is_scored_block_by_block() -> None:
    """Objects pair only within their class (compatible classes for the soft score), so
    the compute bound applies to each class's block, not to the whole entry: 120 x 120
    objects of three kinds (14,400 pairs, 4,800 per block) are scored, as the pairing of
    the whole entry would; one block past the bound still skips, saying which."""
    def objects(prefix: str, count: int) -> list[dict]:
        return [
            _sdo(f"{kind}--{prefix}{i}", f"{kind} {i}", **({"identity_class": "organization"} if kind == "identity" else {}))
            for kind in ("malware", "vulnerability", "identity")
            for i in range(count)
        ]

    scored = _scores(_bundle(*objects("r", 40)), _bundle(*objects("p", 40)))
    assert scored["stix.entity_prf.aligned"].status is MetricStatus.scored
    assert _counts(scored["stix.entity_prf.aligned"]) == (120, 0, 0)

    one_kind = [_sdo(f"malware--{side}{i}", f"malware {i}") for side in ("r",) for i in range(101)]
    big = _scores(_bundle(*one_kind), _bundle(*[_sdo(f"malware--p{i}", f"malware {i}") for i in range(101)]))
    skipped = big["stix.relationship_prf.aligned"]
    assert skipped.status is MetricStatus.skipped
    assert skipped.detail["reason"] == "too_many_objects"
    assert skipped.detail["largest_block_product"] == 101 * 101


def test_an_answer_too_large_to_pair_with_a_reference_that_fits_scores_zero() -> None:
    """Past the compute bound because of the answer alone (a reference of 50 objects, 250
    predicted of one kind), nothing is credited, as structured_json does: the entity scores
    are 0, and an extractor that over-produces is scored, not left out. The relationship
    score counts the relationships (none on either side here: the empty answer's 100)."""
    reference = [_sdo(f"malware--r{i}", f"malware {i}") for i in range(50)]
    flood = [_sdo(f"malware--p{i}", f"malware {i}") for i in range(250)]
    scores = _scores(_bundle(*reference), _bundle(*flood))
    for key in ("stix.entity_prf.aligned", "stix.entity_prf.soft", "stix.relationship_prf.aligned"):
        result = scores[key]
        assert result.status is MetricStatus.scored
        assert result.detail["reason"] == "prediction_too_many_objects"
    assert scores["stix.entity_prf.aligned"].value == 0.0 and scores["stix.entity_prf.soft"].value == 0.0
    assert _counts(scores["stix.entity_prf.aligned"]) == (0, 250, 50)
    assert scores["stix.relationship_prf.aligned"].value == 100.0
    assert _counts(scores["stix.relationship_prf.aligned"]) == (0, 0, 0)


def test_an_oversized_answer_counts_relationships_as_a_scored_entry_does() -> None:
    """The zero credited to an oversized answer counts relationships as triples, as a scored
    entry does: an indicator's `based-on` is no relationship of its own and a duplicate
    triple counts once, so the pooled run takes no phantom misses from this entry."""
    indicator = [
        {"type": "ipv4-addr", "id": "ipv4-addr--1", "value": "45.77.1.2"},
        {"type": "indicator", "id": "indicator--1", "name": "45.77.1.2", "pattern": "[ipv4-addr:value = '45.77.1.2']"},
        _rel("relationship--1", "indicator--1", "based-on", "ipv4-addr--1"),
        _rel("relationship--2", "indicator--1", "indicates", "malware--r0"),
        _rel("relationship--3", "indicator--1", "indicates", "malware--r0"),
    ]
    reference = [_sdo(f"malware--r{i}", f"malware {i}") for i in range(50)] + indicator
    flood = [_sdo(f"malware--p{i}", f"malware {i}") for i in range(250)]
    relationships = _scores(_bundle(*reference), _bundle(*flood))["stix.relationship_prf.aligned"]
    assert relationships.detail["reason"] == "prediction_too_many_objects"
    assert relationships.value == 0.0
    assert _counts(relationships) == (0, 0, 1)


def test_a_reference_that_splits_one_entity_finds_its_relationships_in_the_one_that_names_them() -> None:
    """The reference keeps a victim, its acronym and a misspelling as three objects; the
    prediction is one object naming all three. The entity score stays one-to-one (two
    references missed); each of the reference's three relationships is found."""
    reference = _bundle(
        _sdo("intrusion-set--b", "BianLian"),
        _sdo("identity--1", "Boston Children's Health Physicians", identity_class="organization"),
        _sdo("identity--2", "BCHP", identity_class="organization"),
        _sdo("identity--3", "BHCP", identity_class="organization"),
        _rel("relationship--1", "intrusion-set--b", "targets", "identity--1"),
        _rel("relationship--2", "intrusion-set--b", "targets", "identity--2"),
        _rel("relationship--3", "intrusion-set--b", "targets", "identity--3"),
    )
    prediction = _bundle(
        _sdo("intrusion-set--x", "BianLian"),
        _sdo("identity--y", "Boston Children's Health Physicians", identity_class="organization",
             x_opencti_aliases=["BCHP", "BHCP"]),
        _rel("relationship--9", "intrusion-set--x", "targets", "identity--y"),
    )
    scores = _scores(reference, prediction)
    assert _counts(scores["stix.entity_prf.aligned"]) == (2, 0, 2)
    assert _counts(scores["stix.relationship_prf.aligned"]) == (3, 0, 0)

    # A name of another class, or a reference object already paired, is no copy.
    other = _bundle(
        _sdo("intrusion-set--x", "BianLian"),
        _sdo("identity--y", "Boston Children's Health Physicians", identity_class="organization"),
        _rel("relationship--9", "intrusion-set--x", "targets", "identity--y"),
        _sdo("malware--z", "BCHP"),
    )
    assert _counts(_scores(reference, other)["stix.relationship_prf.aligned"]) == (1, 0, 2)


def test_the_dataset_types_reading_leaves_out_what_the_guideline_never_annotates() -> None:
    """A dataset whose references annotate no attack pattern (AZERG) scores a correct
    technique and its relationship as errors in the aligned scores; read in the dataset's
    types, they are neither hits nor errors. What the dataset does annotate scores as
    before, and the aligned scores themselves are unchanged."""
    from app.evaluation_metrics.quality.families.stix_bundle.alignment import stix_closed_world_scores

    reference = _bundle(_sdo("malware--m", "Emotet"), _country("location--l", "Germany"),
                        _rel("relationship--1", "malware--m", "targets", "location--l"))
    prediction = _bundle(
        _sdo("malware--x", "Emotet"), _country("location--y", "Germany"), _sdo("attack-pattern--t", "Phishing"),
        _rel("relationship--8", "malware--x", "targets", "location--y"),
        _rel("relationship--9", "malware--x", "uses", "attack-pattern--t"),
        _sdo("tool--z", "Cobalt Strike"),
    )
    types = frozenset({"malware", "location", "tool", "relationship"})
    aligned = _scores(reference, prediction)
    closed = stix_closed_world_scores(reference, prediction, types, _Identity())

    assert _counts(aligned["stix.entity_prf.aligned"]) == (2, 2, 0)
    assert _counts(aligned["stix.relationship_prf.aligned"]) == (1, 1, 0)
    assert _counts(closed["stix.entity_prf.aligned.closed"]) == (2, 1, 0)  # the tool stays: a type the dataset annotates
    assert _counts(closed["stix.relationship_prf.aligned.closed"]) == (1, 0, 0)
    assert closed["stix.entity_prf.aligned.closed"].detail["dataset_types"] == ["location", "malware", "relationship", "tool"]


def test_the_dataset_types_reading_reads_types_as_the_pairing_does() -> None:
    """Restricted to the dataset's types, an answer scores as the aligned reading scores it on
    those types: a dataset annotating threat actors annotates their class (a correct intrusion
    set is a hit in both readings), and an indicator kept keeps the observable the pairing
    reads it through."""
    from app.evaluation_metrics.quality.families.stix_bundle.alignment import stix_closed_world_scores

    actor = _bundle(_sdo("threat-actor--1", "Fancy Bear"))
    guessed = _bundle(_sdo("intrusion-set--9", "Fancy Bear"))
    closed = stix_closed_world_scores(actor, guessed, frozenset({"threat-actor"}), _Identity())
    assert _counts(closed["stix.entity_prf.aligned.closed"]) == _counts(_scores(actor, guessed)["stix.entity_prf.aligned"]) == (1, 0, 0)

    indicators = _bundle(
        _sdo("malware--1", "Emotet"),
        {"type": "indicator", "id": "indicator--1", "name": "45.77.1.2", "pattern": "[ipv4-addr:value = '45.77.1.2']"},
        _rel("relationship--2", "indicator--1", "indicates", "malware--1"),
    )
    read_through = _bundle(
        _sdo("malware--9", "Emotet"),
        {"type": "ipv4-addr", "id": "ipv4-addr--9", "value": "45.77.1.2"},
        {"type": "indicator", "id": "indicator--9", "name": "45.77.1.2", "pattern": "[ipv4-addr:value = '45.77.1.2']"},
        _rel("relationship--8", "indicator--9", "based-on", "ipv4-addr--9"),
        _rel("relationship--9", "indicator--9", "indicates", "malware--9"),
    )
    types = frozenset({"malware", "indicator", "relationship"})
    closed = stix_closed_world_scores(indicators, read_through, types, _Identity())
    aligned = _scores(indicators, read_through)
    for key in ("entity", "relationship"):
        assert _counts(closed[f"stix.{key}_prf.aligned.closed"]) == _counts(aligned[f"stix.{key}_prf.aligned"]), key


def test_a_dataset_that_annotates_no_relationship_does_not_score_them_in_its_types() -> None:
    """An indicator list (botvrij) holds no relationship in any reference: every relationship
    an answer gives would be an error against an empty gold. Read in the dataset's types, the
    relationship score is not scored; the entity score is."""
    from app.evaluation_metrics.quality.families.stix_bundle.alignment import stix_closed_world_scores

    reference = _bundle(_sdo("malware--m", "Emotet"))
    prediction = _bundle(_sdo("malware--x", "Emotet"), _sdo("tool--z", "Cobalt Strike"),
                         _rel("relationship--8", "malware--x", "uses", "tool--z"))
    closed = stix_closed_world_scores(reference, prediction, frozenset({"malware", "tool"}), _Identity())

    assert _counts(closed["stix.entity_prf.aligned.closed"]) == (1, 1, 0)
    relationship = closed["stix.relationship_prf.aligned.closed"]
    assert relationship.status == "skipped"
    assert relationship.value is None
    assert relationship.detail["reason"] == "not_annotated_by_dataset"


def test_the_scores_break_down_per_object_and_relationship_type() -> None:
    """The one F1 of an entry hides a type the answer misses: each aligned score's detail
    gives precision, recall and F1 per object class and per relationship type, whose counts
    add up to the entry's."""
    reference = _bundle(
        _sdo("malware--m", "Emotet"), _country("location--l", "Germany"), _sdo("intrusion-set--g", "TA542"),
        _rel("relationship--1", "intrusion-set--g", "uses", "malware--m"),
        _rel("relationship--2", "malware--m", "targets", "location--l"),
    )
    prediction = _bundle(
        _sdo("malware--x", "Emotet"), _country("location--y", "Germany"), _sdo("intrusion-set--z", "TA542"),
        _sdo("tool--t", "Cobalt Strike"),
        _rel("relationship--8", "intrusion-set--z", "uses", "malware--x"),
    )
    scores = _scores(reference, prediction)
    entities = scores["stix.entity_prf.aligned"].detail["by_type"]
    assert entities["malware"]["f1"] == 100.0 and entities["tool"]["fp"] == 1
    assert sum(row["tp"] for row in entities.values()) == scores["stix.entity_prf.aligned"].detail["tp"]
    relationships = scores["stix.relationship_prf.aligned"].detail["by_type"]
    assert relationships["uses"]["f1"] == 100.0
    assert relationships["targets"]["recall"] == 0.0 and relationships["targets"]["fn"] == 1

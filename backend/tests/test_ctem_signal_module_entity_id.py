"""The "Open in <Module>" link of a signal opens the module object, never its dedupe key.

Path 11 (third-party event from the OpenCTI listener) stores ``source_external_id`` as
``<OpenCTI id>:<tracked organization id>`` so each tracked organization keeps its own signal.
The link reads the OpenCTI id: the evidence ``opencti_id`` of a new signal, the part before the
colon of a signal stored before it, and a plain key as it is.
"""

from __future__ import annotations

import uuid

from app.models.ctem_signal import CtemSignal
from app.services import ctem_signals
from app.services.ctem_cases_view import signal_detail
from app.services.ctem_signals import OPENCTI_ID_EVIDENCE, module_entity_id

REPORT = "ddb51808-3b7d-480c-8838-4c707b4c5f40"
TRACKED = "2f1a3ce6-d47d-49d7-add4-758861600e11"
COMPOSITE = f"{REPORT}:{TRACKED}"


def test_a_composite_opencti_key_links_the_opencti_object() -> None:
    assert module_entity_id("opencti", COMPOSITE) == REPORT
    assert module_entity_id("OpenCTI", f" {COMPOSITE} ", {}) == REPORT


def test_the_recorded_opencti_id_wins() -> None:
    assert module_entity_id("opencti", COMPOSITE, {OPENCTI_ID_EVIDENCE: REPORT}) == REPORT
    assert module_entity_id("opencti", "other", {OPENCTI_ID_EVIDENCE: REPORT}) == REPORT
    assert module_entity_id("opencti", COMPOSITE, {OPENCTI_ID_EVIDENCE: "  "}) == REPORT


def test_a_plain_key_is_kept() -> None:
    assert module_entity_id("opencti", "vulnerability--1") == "vulnerability--1"
    assert module_entity_id("opencti", REPORT, None) == REPORT
    assert module_entity_id("opencti", None) is None
    assert module_entity_id("opencti", "  ") is None


def test_another_provider_keeps_its_key_whole() -> None:
    # A GitHub key "<repo id>:<number>" is never split: no module page is built from it.
    assert module_entity_id("github", "123:45") == "123:45"
    assert module_entity_id("openaev", "f-1") == "f-1"
    assert module_entity_id(None, "x:y") == "x:y"


def _signal(external_id: str | None, evidence: dict[str, str] | None = None, provider: str = "opencti") -> CtemSignal:
    return CtemSignal(
        id=uuid.uuid4(),
        trigger_path="third_party_event",
        lens="world",
        source_provider=provider,
        source_external_id=external_id,
        subject_kind="third_party",
        subject_ref="1Password",
        subject_label="1Password",
        evidence=evidence or {},
        dedupe_key="k",
        state="watchlist",
        seen_count=1,
        recorded_via="listener",
    )


def test_the_signal_shape_carries_the_entity_id_and_keeps_the_key() -> None:
    shape = ctem_signals.to_dict(_signal(COMPOSITE))
    assert shape["source"]["external_id"] == COMPOSITE
    assert shape["source"]["entity_id"] == REPORT
    # A plain key is its own entity id: the shape stays as it was.
    plain = ctem_signals.to_dict(_signal("vulnerability--1"))
    assert "entity_id" not in plain["source"]


def test_a_case_row_pivots_to_the_opencti_object() -> None:
    assert signal_detail(_signal(COMPOSITE))["source_external_id"] == REPORT
    assert signal_detail(_signal("vulnerability--1"))["source_external_id"] == "vulnerability--1"
    # A GitHub signal has no module page: no pivot id, whatever its key.
    assert signal_detail(_signal("123:45", provider="github"))["source_external_id"] is None

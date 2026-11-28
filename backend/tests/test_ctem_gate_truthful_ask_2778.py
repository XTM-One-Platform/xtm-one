# SPDX-License-Identifier: Apache-2.0

"""A CTEM gate ask states only real follow-ups and provable absences, and its cuts are said (#2778)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from app.agents import builtin_agents as ba
from app.services import ctem_gate_order as order
from app.services.decision_ask import (
    CUT_MARK,
    MAX_CONTEXT_CHARS,
    MAX_EFFECT_CHARS,
    cut_warnings,
    normalize_ask,
)

_PERSONAS = Path(ba.__file__).parent / "seed_data" / "agent_persona"
_ORCHESTRATORS = ("ctem-threat-response-orchestrator.md", "ctem-vuln-prioritization-orchestrator.md")
_CASE = uuid.UUID("3f0b8c52-8a7e-4c3e-9d0f-0c1d2e3f4a5b")
_OTHER = uuid.UUID("7a1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d")
_KEY = f"ctem:close-without-retest:{_CASE}"
_CLAIM_ASK = {
    "question": "Close the case without the re-test?",
    "context": "The patch is applied on both servers.",
    "options": [
        {"key": "A", "label": "Close", "effect": "Closes the case; the side exposure is still tracked in its own case."},
        {"key": "B", "label": "Keep open", "effect": "Waits for the re-test."},
    ],
    "recommendation": "A",
}
_PENDING_ASK = {
    **_CLAIM_ASK,
    "options": [
        {"key": "A", "label": "Close", "effect": "Closes the case; the side exposure waits on its signal retry."},
        {"key": "B", "label": "Keep open", "effect": "Waits for the re-test."},
    ],
}


class _Result:
    def __init__(self, ids: list[uuid.UUID]) -> None:
        self._ids = ids

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[uuid.UUID]:
        return self._ids


class _Db:
    def __init__(self, ids: list[uuid.UUID]) -> None:
        self.ids = ids
        self.queries = 0

    async def execute(self, _query: Any) -> _Result:
        self.queries += 1
        return _Result(self.ids)


@pytest.mark.parametrize("name", _ORCHESTRATORS)
def test_both_orchestrators_state_only_real_effects_pending_retries_and_provable_absences(name: str) -> None:
    text = (_PERSONAS / name).read_text(encoding="utf-8")
    assert "Each effect states only what really happens and its preconditions" in text
    assert "is written as pending (with its retry item), never as tracked in a case" in text
    assert "An absence is stated only for asset classes that carry software data" in text
    assert "classes without it are listed as not verified" in text
    assert "cites that case's id in `blocked_reason`" in text
    # The refused path of record_ctem_signal is described, not only its success.
    assert "When `record_ctem_signal` is refused" in text
    # The dedupe instruction of the gate item survives the new sentences.
    assert 'filed with `dedupe` true and `dedupe_by` "key" so a resumed run never files it twice' in text


def test_a_context_over_its_cap_answers_a_warning_naming_where_it_was_cut() -> None:
    context = "Hosts in scope lack software rows. " * 40
    assert len(context.strip()) > MAX_CONTEXT_CHARS
    ask = {"question": "Close the case?", "context": context}
    stored = normalize_ask(ask)
    assert stored is not None and stored["context"].endswith(CUT_MARK)
    warnings = cut_warnings(ask)
    assert len(warnings) == 1
    assert "ask.context" in warnings[0] and str(MAX_CONTEXT_CHARS) in warnings[0]
    assert "Rewrite it shorter" in warnings[0]


def test_texts_within_their_caps_answer_no_warning() -> None:
    assert cut_warnings(_PENDING_ASK) == []
    assert cut_warnings(None) == []
    assert cut_warnings("not json") == []
    assert cut_warnings({"question": "Go?", "context": "x" * MAX_CONTEXT_CHARS}) == []


def test_a_long_option_effect_is_warned_by_its_key() -> None:
    ask = {"question": "Go?", "options": [{"key": "A", "label": "Go", "effect": "word " * MAX_EFFECT_CHARS}]}
    warnings = cut_warnings(ask)
    assert len(warnings) == 1 and "ask.options[A].effect" in warnings[0]


def test_a_card_claiming_another_case_is_recognised() -> None:
    assert order.claims_other_case(_CLAIM_ASK)
    assert order.claims_other_case({"context": "The kernel exposure is held in a separate case."})
    assert not order.claims_other_case(_PENDING_ASK)
    assert not order.claims_other_case({"context": "Closes the case."})


async def test_a_claim_with_no_cited_case_is_warned() -> None:
    db = _Db([_CASE])
    warnings = await order.case_reference_warnings(db, _KEY, _CLAIM_ASK, None)
    assert warnings == [order.UNCHECKED_CASE_CLAIM_MESSAGE]
    assert "pending with its retry item" in warnings[0]


async def test_a_claim_citing_an_existing_case_is_not_warned() -> None:
    db = _Db([_CASE, _OTHER])
    assert await order.case_reference_warnings(db, _KEY, _CLAIM_ASK, f"side case {_OTHER}") == []
    assert db.queries == 1


async def test_a_claim_citing_a_case_that_does_not_exist_is_warned() -> None:
    db = _Db([_CASE])
    warnings = await order.case_reference_warnings(db, _KEY, _CLAIM_ASK, f"side case {_OTHER}")
    assert warnings == [order.UNCHECKED_CASE_CLAIM_MESSAGE]


async def test_a_gate_keyed_to_a_missing_case_is_warned() -> None:
    db = _Db([])
    warnings = await order.case_reference_warnings(db, _KEY, _PENDING_ASK, None)
    assert warnings == [order.missing_case_message(str(_CASE))]


async def test_a_pending_retry_card_on_an_existing_case_reads_once_and_is_clean() -> None:
    db = _Db([_CASE])
    assert await order.case_reference_warnings(db, _KEY, _PENDING_ASK, None) == []
    assert db.queries == 1


async def test_a_non_gate_key_reads_nothing() -> None:
    db = _Db([])
    assert await order.case_reference_warnings(db, "dev:x", _CLAIM_ASK, None) == []
    assert db.queries == 0

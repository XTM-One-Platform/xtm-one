"""Findings authoring, strict parser normalization, the step-before-more phase
gate, and the fork/create concurrency cap for the OpenAEV Attack Path
Orchestrator.

These pin the coupled root causes behind the "forks land with no outputs / 400
on finalize / 79 iterations, zero steps, ~20 orphaned (duplicate) copies" runs:

* RC-1 - the first-class flat ``findings`` param expands into OpenAEV's nested
  output-parser DTO with ``contract_output_element_is_finding=true`` ALWAYS
  stamped (a primitive boolean that silently persists ``false`` when omitted -
  the copy-with-no-findings bug).
* RC-2 - ``_normalize_output_parsers`` LIFTS the legacy flat parser shape the
  model kept sending into a real nested element, forces the finding flag, and
  REJECTS an element-less parser with an actionable error instead of silently
  dropping it (which stored an action with no outputs).
* the verify gate ``_action_findings_count`` counts ONLY flagged findings and
  never trusts the server-derived ``action_providing``.
* RC-4 - the phase gate refuses a new fork/create once authored arsenal items
  run ``PHASE_GATE_LEAD`` ahead of authored steps.
* RC-5 - a loop-bound ``Semaphore(3)`` serializes findings-required fork/create
  writes (the parallel-burst races that minted the orphans) while chat / other
  agents stay uncapped.
"""

from __future__ import annotations

import asyncio
import contextlib

from app.integrations.providers.openaev._common import (
    _expand_findings,
    _fmt_group_index,
    _normalize_output_parsers,
)
from app.integrations.providers.openaev.threat_arsenal import _action_findings_count

# ---------------------------------------------------------------------------
# RC-1: findings-param expansion -> nested DTO (isFinding=true)
# ---------------------------------------------------------------------------


def test_expand_findings_builds_nested_dto_with_is_finding_true():
    parsers, err = _expand_findings(
        [
            {
                "type": "credentials",
                "regex": "([^:]+):(.+)",
                "fields": {"username": 1, "password": 2},
            }
        ]
    )
    assert err == ""
    assert len(parsers) == 1
    parser = parsers[0]
    assert parser["output_parser_type"] == "REGEX"
    # No mode supplied -> STDOUT (a command emits its findings to stdout).
    assert parser["output_parser_mode"] == "STDOUT"
    elements = parser["output_parser_contract_output_elements"]
    assert len(elements) == 1
    element = elements[0]
    assert element["contract_output_element_type"] == "credentials"
    assert element["contract_output_element_rule"] == "([^:]+):(.+)"
    # The load-bearing flag: forced true so the copy actually PRODUCES a finding.
    assert element["contract_output_element_is_finding"] is True
    # @NotBlank labels are derived from the type, not demanded from the model.
    assert element["contract_output_element_name"]
    assert element["contract_output_element_key"]
    groups = {
        g["regex_group_field"]: g["regex_group_index_values"]
        for g in element["contract_output_element_regex_groups"]
    }
    assert groups == {"username": "$1$", "password": "$2$"}
    # And the expanded shape is accepted by the strict normalizer unchanged.
    assert _normalize_output_parsers({"action_output_parsers": parsers}) is None


def test_expand_findings_forces_is_finding_even_when_caller_sets_false():
    # isFinding is stamped true unconditionally - a caller cannot author a
    # finding-less "finding" through this param (that is the whole bug).
    parsers, err = _expand_findings(
        [
            {
                "type": "cve",
                "regex": r"(CVE-\d{4}-\d+)",
                "contract_output_element_is_finding": False,
            }
        ]
    )
    assert err == ""
    element = parsers[0]["output_parser_contract_output_elements"][0]
    assert element["contract_output_element_is_finding"] is True


def test_expand_findings_groups_by_mode():
    # Same mode -> one parser with two elements.
    parsers, err = _expand_findings(
        [
            {"type": "username", "regex": r"user=(\w+)"},
            {"type": "port", "regex": r"port (\d+)"},
        ]
    )
    assert err == ""
    assert len(parsers) == 1
    assert len(parsers[0]["output_parser_contract_output_elements"]) == 2
    # Distinct modes -> one parser per mode.
    parsers, err = _expand_findings(
        [
            {"type": "credentials", "regex": "a:(.+)", "mode": "STDOUT"},
            {"type": "cve", "regex": r"(CVE-\d{4}-\d+)", "mode": "STDERR"},
        ]
    )
    assert err == ""
    assert {p["output_parser_mode"] for p in parsers} == {"STDOUT", "STDERR"}


def test_expand_findings_rejects_empty_and_missing_fields():
    parsers, err = _expand_findings([])
    assert parsers == []
    assert "non-empty list" in err

    parsers, err = _expand_findings("not-a-list")
    assert parsers == []
    assert "non-empty list" in err

    parsers, err = _expand_findings([{"regex": "x=(.+)"}])
    assert parsers == []
    assert "missing 'type'" in err

    parsers, err = _expand_findings([{"type": "credentials"}])
    assert parsers == []
    assert "missing 'regex'" in err


def test_fmt_group_index_coerces_to_dollar_form():
    assert _fmt_group_index(1) == "$1$"
    assert _fmt_group_index("2") == "$2$"
    assert _fmt_group_index("$3$") == "$3$"
    assert _fmt_group_index(None) == "$1$"
    # bool is an int subclass - must not render "$True$" nor the invalid "$0$"
    # (capture groups are 1-indexed); either bool falls back to the group-1 default.
    assert _fmt_group_index(True) == "$1$"
    assert _fmt_group_index(False) == "$1$"


# ---------------------------------------------------------------------------
# RC-2: strict normalizer - lift the flat shape, force isFinding, REJECT empties
# ---------------------------------------------------------------------------


def test_normalizer_lifts_legacy_flat_parser_into_nested_element():
    # The exact flat shape the old tool description told the model to send:
    # regex + finding type live at the PARSER level. The normalizer used to
    # silently drop it (stored an action with NO outputs); now it lifts it.
    body = {
        "action_output_parsers": [
            {
                "output_parser_type": "REGEX",
                "output_parser_mode": "STDOUT",
                "output_parser_regex": "([^:]+):(.+)",
                "contract_output_element_type": "credentials",
                "fields": {"username": 1, "password": 2},
            }
        ]
    }
    assert _normalize_output_parsers(body) is None
    elements = body["action_output_parsers"][0][
        "output_parser_contract_output_elements"
    ]
    assert len(elements) == 1
    element = elements[0]
    assert element["contract_output_element_type"] == "credentials"
    assert element["contract_output_element_rule"] == "([^:]+):(.+)"
    assert element["contract_output_element_is_finding"] is True
    groups = {
        g["regex_group_field"]: g["regex_group_index_values"]
        for g in element["contract_output_element_regex_groups"]
    }
    assert groups == {"username": "$1$", "password": "$2$"}
    # The consumed flat regex is dropped so OpenAEV never sees a stale field.
    assert "output_parser_regex" not in body["action_output_parsers"][0]


def test_normalizer_rejects_element_less_parser_with_actionable_error():
    # Neither a nested element nor a flat rule+type to build one -> would store an
    # action with NO findings. Must REJECT (not silently drop) and point at the
    # easy `findings` param.
    body = {
        "action_output_parsers": [
            {"output_parser_type": "REGEX", "output_parser_mode": "STDOUT"}
        ]
    }
    err = _normalize_output_parsers(body)
    assert err is not None
    assert "no finding element" in err.lower()
    assert "findings" in err

    # A type with no rule cannot be lifted either -> same rejection.
    body = {
        "action_output_parsers": [
            {
                "output_parser_type": "REGEX",
                "contract_output_element_type": "credentials",
            }
        ]
    }
    err = _normalize_output_parsers(body)
    assert err is not None
    assert "no finding element" in err.lower()


def test_normalizer_forces_is_finding_and_derives_labels_on_nested_element():
    body = {
        "action_output_parsers": [
            {
                "output_parser_type": "REGEX",
                "output_parser_mode": "STDOUT",
                "output_parser_contract_output_elements": [
                    {
                        "contract_output_element_type": "credentials",
                        "contract_output_element_rule": "([^:]+):(.+)",
                    }
                ],
            }
        ]
    }
    assert _normalize_output_parsers(body) is None
    element = body["action_output_parsers"][0][
        "output_parser_contract_output_elements"
    ][0]
    assert element["contract_output_element_is_finding"] is True
    assert element["contract_output_element_name"]
    assert element["contract_output_element_key"]


def test_normalizer_coerces_none_is_finding_to_true():
    # An explicit null (the model emitting JSON null) is treated as "omitted" and
    # forced true, exactly like a missing key - never passed through, where it
    # would fail OpenAEV's primitive-boolean bean-validation and read as
    # finding-less to the verify gate.
    body = {
        "action_output_parsers": [
            {
                "output_parser_type": "REGEX",
                "output_parser_mode": "STDOUT",
                "output_parser_contract_output_elements": [
                    {
                        "contract_output_element_type": "cve",
                        "contract_output_element_rule": r"(CVE-\d{4}-\d+)",
                        "contract_output_element_is_finding": None,
                    }
                ],
            }
        ]
    }
    assert _normalize_output_parsers(body) is None
    element = body["action_output_parsers"][0][
        "output_parser_contract_output_elements"
    ][0]
    assert element["contract_output_element_is_finding"] is True


def test_normalizer_rejects_non_boolean_is_finding():
    # A truthy-but-not-True value (1, "true", ...) must be REJECTED, not shipped:
    # it would slip past _action_findings_count's strict `is True` check (so the
    # fork verifies finding-less) and OpenAEV's primitive-boolean binding.
    for bad in (1, 0, "true", "false"):
        body = {
            "action_output_parsers": [
                {
                    "output_parser_type": "REGEX",
                    "output_parser_mode": "STDOUT",
                    "output_parser_contract_output_elements": [
                        {
                            "contract_output_element_type": "cve",
                            "contract_output_element_rule": r"(CVE-\d{4}-\d+)",
                            "contract_output_element_is_finding": bad,
                        }
                    ],
                }
            ]
        }
        err = _normalize_output_parsers(body)
        assert err is not None, bad
        assert "boolean" in err


def test_normalizer_preserves_explicit_false_is_finding():
    # A real False is a legitimate non-finding element (the dead-branch guard
    # keys on it); it must be preserved, not overwritten to true.
    body = {
        "action_output_parsers": [
            {
                "output_parser_type": "REGEX",
                "output_parser_mode": "STDOUT",
                "output_parser_contract_output_elements": [
                    {
                        "contract_output_element_type": "cve",
                        "contract_output_element_rule": r"(CVE-\d{4}-\d+)",
                        "contract_output_element_is_finding": False,
                    }
                ],
            }
        ]
    }
    assert _normalize_output_parsers(body) is None
    element = body["action_output_parsers"][0][
        "output_parser_contract_output_elements"
    ][0]
    assert element["contract_output_element_is_finding"] is False


def test_normalizer_defaults_missing_mode_to_stdout():
    body = {
        "action_output_parsers": [
            {
                "output_parser_type": "REGEX",
                "output_parser_contract_output_elements": [
                    {
                        "contract_output_element_type": "cve",
                        "contract_output_element_rule": r"(CVE-\d{4}-\d+)",
                    }
                ],
            }
        ]
    }
    assert _normalize_output_parsers(body) is None
    assert body["action_output_parsers"][0]["output_parser_mode"] == "STDOUT"


def test_normalizer_rejects_finding_type_placed_in_parser_type():
    # The finding type belongs in contract_output_element_type; OpenAEV's
    # ParserType has only REGEX. A model putting "credentials" in
    # output_parser_type gets an actionable error, not an opaque 400.
    body = {
        "action_output_parsers": [
            {
                "output_parser_type": "credentials",
                "output_parser_contract_output_elements": [
                    {
                        "contract_output_element_type": "credentials",
                        "contract_output_element_rule": "a:(.+)",
                    }
                ],
            }
        ]
    }
    err = _normalize_output_parsers(body)
    assert err is not None
    assert "must be REGEX" in err


def test_normalizer_rejects_dead_branch_text_typed_credential():
    # A finding typed 'text' but keyed as a real primitive never chains (text ->
    # text primitive only). Caught here with the fix, not shipped as a path that
    # silently never fires.
    body = {
        "action_output_parsers": [
            {
                "output_parser_type": "REGEX",
                "output_parser_mode": "STDOUT",
                "output_parser_contract_output_elements": [
                    {
                        "contract_output_element_type": "text",
                        "contract_output_element_rule": "pw=(.+)",
                        "contract_output_element_key": "password",
                        "contract_output_element_is_finding": True,
                    }
                ],
            }
        ]
    }
    err = _normalize_output_parsers(body)
    assert err is not None
    assert "credentials" in err


def test_normalizer_rejects_invalid_capture_regex():
    body = {
        "action_output_parsers": [
            {
                "output_parser_type": "REGEX",
                "output_parser_mode": "STDOUT",
                "output_parser_contract_output_elements": [
                    {
                        "contract_output_element_type": "credentials",
                        "contract_output_element_rule": "([a-z",
                    }
                ],
            }
        ]
    }
    err = _normalize_output_parsers(body)
    assert err is not None
    assert "valid regex" in err


# ---------------------------------------------------------------------------
# Verify gate: _action_findings_count trusts isFinding, never action_providing
# ---------------------------------------------------------------------------


def test_action_findings_count_counts_only_flagged_findings():
    action = {
        "action_output_parsers": [
            {
                "output_parser_contract_output_elements": [
                    {"contract_output_element_is_finding": True},
                    {"contract_output_element_is_finding": False},
                    {"contract_output_element_is_finding": True},
                ]
            }
        ]
    }
    assert _action_findings_count(action) == 2


def test_action_findings_count_ignores_action_providing():
    # A server-derived action_providing list is computed from element TYPE
    # regardless of the finding flag: a typed-but-non-finding element reads as
    # "provides credentials" yet emits no finding. The count must be 0.
    action = {
        "action_providing": ["credentials", "username"],
        "action_output_parsers": [
            {
                "output_parser_contract_output_elements": [
                    {"contract_output_element_is_finding": False}
                ]
            }
        ],
    }
    assert _action_findings_count(action) == 0
    # No parsers at all -> 0, even with a populated action_providing.
    assert _action_findings_count({"action_providing": ["credentials"]}) == 0
    assert _action_findings_count({}) == 0


def test_action_findings_count_requires_boolean_true_not_truthy():
    # isFinding must be exactly True (a primitive boolean). A truthy int (1) is
    # NOT a finding - locks the strict identity check the verify relies on.
    action = {
        "action_output_parsers": [
            {"output_parser_contract_output_elements": [
                {"contract_output_element_is_finding": 1}
            ]}
        ]
    }
    assert _action_findings_count(action) == 0


# ---------------------------------------------------------------------------
# RC-4: step-before-more phase gate
# ---------------------------------------------------------------------------


def test_phase_gate_blocks_after_lead_arsenal_without_steps():
    from app.services.builtin_tools.filtering import (
        PHASE_GATE_LEAD,
        orchestrator_phase_gate_blocked,
        record_orchestrator_arsenal_authored,
        record_orchestrator_step_authored,
        reset_orchestrator_arsenal_writes,
        reset_orchestrator_phase_counter,
        set_orchestrator_arsenal_writes,
        set_orchestrator_phase_counter,
    )

    # The user's hard requirement: at most ~3 arsenal items before a step.
    assert PHASE_GATE_LEAD == 3
    marker = set_orchestrator_arsenal_writes(True)
    counter = set_orchestrator_phase_counter(True)
    try:
        assert not orchestrator_phase_gate_blocked()
        for _ in range(PHASE_GATE_LEAD):
            record_orchestrator_arsenal_authored()
        # arsenal now runs PHASE_GATE_LEAD ahead of steps -> blocked.
        assert orchestrator_phase_gate_blocked()
        # Authoring a step relieves the gate so authoring can resume.
        record_orchestrator_step_authored()
        assert not orchestrator_phase_gate_blocked()
    finally:
        reset_orchestrator_phase_counter(counter)
        reset_orchestrator_arsenal_writes(marker)


def test_phase_gate_inert_without_orchestrator_marker():
    # A live counter but no orchestrator-run marker (chat / other agents): the
    # records are ignored and the gate never blocks.
    from app.services.builtin_tools.filtering import (
        PHASE_GATE_LEAD,
        orchestrator_phase_gate_blocked,
        record_orchestrator_arsenal_authored,
        reset_orchestrator_phase_counter,
        set_orchestrator_phase_counter,
    )

    counter = set_orchestrator_phase_counter(True)
    try:
        for _ in range(PHASE_GATE_LEAD + 2):
            record_orchestrator_arsenal_authored()
        assert not orchestrator_phase_gate_blocked()
    finally:
        reset_orchestrator_phase_counter(counter)


def test_phase_gate_inert_when_counter_absent():
    # Orchestrator marker set but the counter is None (a nested Payload Creator
    # run clears it): the gate is a strict no-op.
    from app.services.builtin_tools.filtering import (
        PHASE_GATE_LEAD,
        orchestrator_phase_gate_blocked,
        record_orchestrator_arsenal_authored,
        reset_orchestrator_arsenal_writes,
        reset_orchestrator_phase_counter,
        set_orchestrator_arsenal_writes,
        set_orchestrator_phase_counter,
    )

    marker = set_orchestrator_arsenal_writes(True)
    counter = set_orchestrator_phase_counter(False)
    try:
        for _ in range(PHASE_GATE_LEAD + 2):
            record_orchestrator_arsenal_authored()
        assert not orchestrator_phase_gate_blocked()
    finally:
        reset_orchestrator_phase_counter(counter)
        reset_orchestrator_arsenal_writes(marker)


# ---------------------------------------------------------------------------
# RC-5: fork/create concurrency cap
# ---------------------------------------------------------------------------


async def test_arsenal_write_guard_uncapped_when_findings_not_required():
    from app.integrations.providers.openaev import threat_arsenal as ta

    # Default chat / non-autonomous callers stay uncapped, even for fork.
    assert isinstance(ta._arsenal_write_guard("fork"), contextlib.nullcontext)


async def test_arsenal_write_guard_caps_only_write_actions_under_flag():
    from app.integrations.providers.openaev import threat_arsenal as ta
    from app.services.builtin_tools.filtering import (
        reset_arsenal_findings_required,
        set_arsenal_findings_required,
    )

    token = set_arsenal_findings_required(True)
    try:
        assert isinstance(ta._arsenal_write_guard("fork"), asyncio.Semaphore)
        assert isinstance(ta._arsenal_write_guard("create"), asyncio.Semaphore)
        # duplicate MINTS a copy too, so it is capped exactly like fork /
        # create - otherwise a parallel burst of raw duplicates would reopen the
        # orphan-race the cap exists to close.
        assert isinstance(ta._arsenal_write_guard("duplicate"), asyncio.Semaphore)
        # Reads stay uncapped even under the findings-required flag.
        assert isinstance(ta._arsenal_write_guard("list"), contextlib.nullcontext)
        assert isinstance(ta._arsenal_write_guard("get"), contextlib.nullcontext)
    finally:
        reset_arsenal_findings_required(token)


async def test_arsenal_write_semaphore_is_loop_bound_and_three():
    from app.integrations.providers.openaev import threat_arsenal as ta

    assert ta._ARSENAL_WRITE_CONCURRENCY == 3
    ta._arsenal_write_semaphores.clear()
    first = ta._arsenal_write_semaphore()
    second = ta._arsenal_write_semaphore()
    # One semaphore per loop, reused - so it actually serializes writes rather
    # than handing out a fresh (useless) permit set on every call.
    assert first is second


async def test_arsenal_write_guard_serializes_findings_forks_to_three():
    from app.integrations.providers.openaev import threat_arsenal as ta
    from app.services.builtin_tools.filtering import (
        reset_arsenal_findings_required,
        set_arsenal_findings_required,
    )

    token = set_arsenal_findings_required(True)
    ta._arsenal_write_semaphores.clear()  # fresh cap in this loop
    concurrent = 0
    peak = 0
    release = asyncio.Event()

    async def worker() -> None:
        nonlocal concurrent, peak
        async with ta._arsenal_write_guard("fork"):
            concurrent += 1
            peak = max(peak, concurrent)
            try:
                await release.wait()
            finally:
                concurrent -= 1

    try:
        tasks = [asyncio.create_task(worker()) for _ in range(9)]
        # Let every task attempt to acquire; the cap must hold the holders at 3.
        for _ in range(100):
            await asyncio.sleep(0)
            if concurrent >= ta._ARSENAL_WRITE_CONCURRENCY:
                break
        await asyncio.sleep(0.02)  # give the rest a chance to (fail to) acquire
        assert concurrent == 3, concurrent
        assert peak == 3, peak
        release.set()
        await asyncio.gather(*tasks)
        assert peak == 3
    finally:
        release.set()
        reset_arsenal_findings_required(token)

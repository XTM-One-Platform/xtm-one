"""Continuing an investigation past its budget — the pure parts.

Everything here is decidable without a database: whether a stored source
address round-trips, what the coverage tally says, and what a synthetic
continuation pack asks for. The stateful half — chains, cumulative spend, the
cap, and the end-to-end use case — lives in the ``_postgres`` neighbour,
because those are properties of rows rather than of functions.

The bias in this file is towards the two failure modes that would be invisible
in a screenshot: a remainder that silently shrinks, and a "not worth
continuing" nobody can check.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models.investigation import (
    STEP_STATUSES,
    TERMINAL_STEP_STATUSES,
)
from app.services.investigation_continuation import (
    CHAIN_MAX_ITERATIONS,
    CONTINUATION_PACK_SLUG,
    FINISHED_RUN_STATUSES,
    MAX_CHAIN_DEPTH,
    UNQUERIED_STEP_STATUSES,
    ContinuableStep,
    analyse_coverage,
    pack_from_steps,
)
from app.services.investigation_packs import (
    SUBJECT_TYPES,
    SourceDefinition,
)


def _step(status: str, *, pack: str | None = "reputation", findings: int = 0):
    return SimpleNamespace(
        status=status, source_pack_slug=pack, findings_count=findings
    )


# ── The stored address ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        SourceDefinition(
            name="VirusTotal",
            subject_types=("ip",),
            url_template="https://vt.example/{value}",
            value_transform="host",
        ),
        SourceDefinition(
            name="Threat corpus",
            subject_types=("ip",),
            kind="knowledge_base",
            knowledge_base_ids=("kb-1",),
        ),
        SourceDefinition(
            name="srv · lookup_ip",
            subject_types=("ip",),
            kind="mcp_tool",
            mcp_server_id="srv-1",
            mcp_tool_name="lookup_ip",
            mcp_argument="ip",
        ),
        SourceDefinition(
            name="Web",
            subject_types=("ip",),
            kind="web_search",
            search_prompt="What is {value}?",
        ),
    ],
    ids=["http", "knowledge_base", "mcp_tool", "web_search"],
)
def test_a_source_survives_a_round_trip_through_the_stored_ref(source) -> None:
    """``to_ref`` -> ``from_ref`` must preserve what makes the source itself.

    Identity is the assertion rather than field equality: identity is what the
    orchestrator de-duplicates on and what re-authorisation matches against,
    so a round trip that preserved every field EXCEPT one that identity keys
    on would pass a field-wise check and then silently query a different
    place.
    """
    rebuilt = SourceDefinition.from_ref(
        source.to_ref(), subject_types=source.subject_types
    )
    assert rebuilt.identity() == source.identity()
    assert rebuilt.name == source.name
    assert rebuilt.kind == source.kind


def test_the_stored_ref_carries_no_credential() -> None:
    """A ref is a pointer, never a grant.

    Asserted on the serialized form rather than trusted from the code, because
    this is the property that makes it safe to persist at all: a token that
    leaked in here would be readable by anyone who can read the run, and would
    let a continuation query a source the caller can no longer reach.
    """
    source = SourceDefinition(
        name="srv · lookup",
        subject_types=("ip",),
        kind="mcp_tool",
        mcp_server_id="srv-1",
        mcp_tool_name="lookup",
    )
    ref = source.to_ref()
    flat = " ".join(f"{k}={v}" for k, v in ref.items()).lower()
    for smell in ("token", "secret", "api_key", "apikey", "password", "bearer", "authorization"):
        assert smell not in flat, ref


@pytest.mark.parametrize(
    "ref",
    [
        None,
        "not-a-dict",
        {},
        {"name": "x"},
        {"kind": "telepathy", "name": "x"},
        {"kind": "knowledge_base", "name": "x"},
        {"kind": "mcp_tool", "name": "x"},
        {"kind": "http", "name": "x"},
        {"kind": "web_search", "name": "x", "search_prompt": "no placeholder"},
        {"kind": "http", "url_template": "https://x/{value}"},
    ],
    ids=[
        "none", "string", "empty", "no-kind", "unknown-kind", "kb-without-ids",
        "mcp-without-tool", "http-without-template", "search-without-value",
        "no-name",
    ],
)
def test_an_unusable_ref_is_refused_rather_than_guessed(ref) -> None:
    """Fail closed: a source we cannot address must not become one we can.

    Every case here is something a row could actually hold — written by an
    older version, hand-edited, or truncated. The alternative to raising is
    substituting a *plausible* source, which would query the wrong place and
    cite it as though the plan had named it.
    """
    with pytest.raises(ValueError):
        SourceDefinition.from_ref(ref, subject_types=SUBJECT_TYPES)


@pytest.mark.parametrize(
    "template",
    [
        "file:///etc/passwd",
        "gopher://evil.test/_",
        "ftp://evil.test/{value}",
        "javascript:alert(1)",
        "//evil.test/{value}",
        "",
    ],
)
def test_a_persisted_ref_cannot_carry_a_non_http_address(template) -> None:
    """Until leads became continuable every stored `http` ref came from
    `BUILTIN_PACKS`; a lead's URL is derived from a third-party page, so a
    persisted ref is now attacker-influenced and must not survive a reload
    with a scheme the fetcher never intended to serve."""
    with pytest.raises(ValueError):
        SourceDefinition.from_ref(
            {"kind": "http", "name": "Lead", "url_template": template},
            subject_types=SUBJECT_TYPES,
        )


def test_a_hostile_address_is_refused_at_query_time_not_at_store_time() -> None:
    """Where the SSRF verdict belongs, and why it is not in `from_ref`.

    A metadata-service template is a perfectly well-formed http ref, so it
    reconstructs — and is then refused by the resolving check the fetcher runs
    on every hop. That split is deliberate: a hostname that resolves publicly
    when the ref is stored can resolve to 169.254.169.254 by the time the
    continuation runs, so a store-time verdict is stale by construction and
    the query-time one has to be the authority.
    """
    from app.services.web_fetch import validate_outbound_url

    hostile = "http://169.254.169.254/{value}"
    source = SourceDefinition.from_ref(
        {"kind": "http", "name": "Lead", "url_template": hostile},
        subject_types=SUBJECT_TYPES,
    )
    assert source.url_template == hostile, "well-formed, so it reconstructs"

    with pytest.raises(ValueError, match="private or non-routable"):
        validate_outbound_url(source.build_url("x"))

    for blocked in ("http://127.0.0.1/x", "http://10.0.0.1/x", "http://[::1]/x"):
        with pytest.raises(ValueError):
            validate_outbound_url(blocked)


def test_only_a_discovered_lead_escapes_the_pack_gate() -> None:
    """The lead exemption must not become a hole for planned steps.

    A lead belongs to no pack, so gating it on pack membership withheld every
    one as `no_longer_permitted`. Exempting it is right; exempting anything
    else is not — a planned step in a pack the caller lost access to must
    still be withheld, and a lead that somehow arrived as a privileged ref
    keeps the gate, because a corpus and an MCP server carry per-user access
    a public URL does not.
    """
    import uuid as _uuid

    from app.services.investigation_continuation import _is_followable_lead

    http = SourceDefinition(
        name="Pivot", subject_types=SUBJECT_TYPES, kind="http",
        url_template="https://pivot.test/{value}",
    )
    corpus = SourceDefinition(
        name="Corpus", subject_types=SUBJECT_TYPES, kind="knowledge_base",
        knowledge_base_ids=(_uuid.uuid4(),),
    )

    class _Step:
        def __init__(self, lead_from_step_id):
            self.lead_from_step_id = lead_from_step_id

    discovered, planned = _Step(_uuid.uuid4()), _Step(None)

    assert _is_followable_lead(discovered, http) is True
    assert _is_followable_lead(planned, http) is False, "a planned step keeps the gate"
    assert _is_followable_lead(discovered, corpus) is False, (
        "a privileged ref keeps the gate even when it arrived as a lead"
    )


# ── What counts as "never ran" ──────────────────────────────────────────────


def test_unqueried_statuses_are_exactly_the_ones_with_no_result() -> None:
    """The offer must not include a step that already has an outcome.

    Pinned against the model's own vocabulary so a new status has to be
    classified deliberately. Left unclassified it would default to "has a
    result" and vanish from the remainder — the silent shrink this design
    exists to prevent — or, worse, be offered for a re-run it already had.
    """
    assert set(UNQUERIED_STEP_STATUSES) < set(STEP_STATUSES)
    with_result = set(STEP_STATUSES) - set(UNQUERIED_STEP_STATUSES)
    assert with_result == {"active", "completed", "empty", "degraded", "error"}


def test_a_failed_step_is_not_offered_as_continuable() -> None:
    """Retrying a failure is a different feature from continuing a plan.

    ``error`` is terminal and stays out: deciding what is even retryable needs
    the error taxonomy (a timeout, yes; an auth failure, never), and folding it
    in here would let "continue" re-run a broken credential for ever while
    charging the chain for each attempt.
    """
    assert "error" not in UNQUERIED_STEP_STATUSES
    assert "degraded" not in UNQUERIED_STEP_STATUSES
    # And the one that IS offered is terminal too, which is the whole reason a
    # continuation has to be a new run rather than a resumed one.
    assert "skipped" in TERMINAL_STEP_STATUSES


def test_only_a_finished_run_can_be_continued() -> None:
    """A run still in flight may yet query the steps we would offer.

    This replaced a gate on END REASON CODES (``run.budget_spent`` and
    friends), and the swap matters: the offer now gathers across the whole
    chain, so "did this run stop short?" is already answered by whether any
    step is unqueried. Keeping both gates meant two answers to one question,
    and the drift would have shown up as a remainder present in the rows and
    absent from the block.

    Pinned against the model's vocabulary so a new run state has to be
    classified rather than defaulting to "not finished" and silently
    suppressing every offer.
    """
    from app.models.investigation import INVESTIGATION_STATUSES

    assert set(FINISHED_RUN_STATUSES) < set(INVESTIGATION_STATUSES)
    in_flight = set(INVESTIGATION_STATUSES) - set(FINISHED_RUN_STATUSES)
    assert in_flight == {"planning", "running"}


# ── Coverage, derived rather than judged ────────────────────────────────────


def test_a_pack_the_budget_never_touched_is_named() -> None:
    """The one fact that changes what the existing findings are WORTH.

    Three reputation sources answered and the whole telemetry half was never
    reached. The tally alone ("3 of 7") does not say that; ``packs_untouched``
    does, and it is the difference between "the verdict consulted both kinds
    of source" and "the verdict rests on whichever came first in the plan".
    """
    steps = [
        _step("completed", pack="reputation", findings=2),
        _step("empty", pack="reputation"),
        _step("completed", pack="reputation", findings=1),
        _step("skipped", pack="telemetry"),
        _step("skipped", pack="telemetry"),
        _step("skipped", pack="telemetry"),
        _step("skipped", pack="telemetry"),
    ]
    coverage = analyse_coverage(steps)

    assert coverage.queried == 3
    assert coverage.unqueried == 4
    assert coverage.findings == 3
    assert coverage.empty == 1
    assert coverage.packs_reached == ("reputation",)
    assert coverage.packs_untouched == ("telemetry",)
    assert coverage.anything_found is True


def test_a_pack_partially_reached_is_not_reported_as_untouched() -> None:
    """``untouched`` means NOTHING from it ran — not "some of it is left".

    The looser reading would flag every pack in a truncated plan, which is
    every pack, which says nothing at all.
    """
    steps = [
        _step("completed", pack="reputation", findings=1),
        _step("skipped", pack="reputation"),
        _step("skipped", pack="telemetry"),
    ]
    coverage = analyse_coverage(steps)
    assert coverage.packs_reached == ("reputation",)
    assert coverage.packs_untouched == ("telemetry",)


def test_sources_that_all_found_nothing_are_visible_as_such() -> None:
    """"Everything answered, nothing was found" must be distinguishable.

    It is the state where continuing is most likely worthwhile, and the one a
    bare findings count of zero cannot tell apart from "nothing ran".
    """
    coverage = analyse_coverage([_step("empty"), _step("empty"), _step("skipped")])
    assert (coverage.queried, coverage.empty, coverage.findings) == (2, 2, 0)
    assert coverage.anything_found is False


def test_coverage_says_nothing_a_model_had_to_invent() -> None:
    """Guard on the shape: every field is an int or a tuple of pack slugs.

    The assessment shown to an analyst is composed in the UI from these
    numbers, in the reader's own language. A prose verdict computed here would
    be both untranslatable and unverifiable — and a hallucinated "not worth
    continuing" closes an investigation nobody decided to close, which is the
    same reasoning that made ``classify_subject`` a regex and not a prompt.
    """
    coverage = analyse_coverage([_step("completed", findings=1), _step("skipped")])
    for name in ("queried", "unqueried", "findings", "empty", "failed"):
        assert isinstance(getattr(coverage, name), int), name
    for name in ("packs_reached", "packs_untouched"):
        assert all(isinstance(s, str) for s in getattr(coverage, name)), name


# ── The synthetic pack a continuation runs ──────────────────────────────────


def _continuable(name: str, n: int) -> ContinuableStep:
    return ContinuableStep(
        step_id=uuid.uuid4(),
        source_name=name,
        source_pack_slug="telemetry",
        position=n,
        source=SourceDefinition(
            name=name, subject_types=("ip",), url_template=f"https://{name}/{{value}}"
        ),
    )


def test_the_continuation_pack_asks_for_the_allowance_not_the_selection() -> None:
    """The chain cap, not the tick count, decides what a child run may spend.

    This is the inversion that keeps the feature bounded: ``max_iterations`` is
    the checkpoint interval, and the cumulative cap is the limit. Ask for four
    with two left and the run must plan four and spend two — leaving the rest
    ``skipped`` for a spent budget, which is what makes the child continuable
    in turn through the very same path.
    """
    steps = tuple(_continuable(f"s{i}", i) for i in range(4))
    pack = pack_from_steps(steps, subject_type="ip", allowance=2)

    assert pack.slug == CONTINUATION_PACK_SLUG
    assert len(pack.sources) == 4
    assert pack.max_iterations == 2
    # Never auto-selected: a remainder is spent because somebody chose to, and
    # a recommender picking it would be spending on their behalf.
    assert pack.auto_select is False


def test_the_continuation_pack_keeps_the_analysts_order() -> None:
    """Truncation must follow the order they ticked, not a re-sort.

    A cap that re-prioritised would spend the remaining allowance on a source
    the analyst had deliberately put last.
    """
    steps = tuple(_continuable(n, i) for i, n in enumerate(["zulu", "alpha", "mike"]))
    pack = pack_from_steps(steps, subject_type="ip", allowance=3)
    assert [s.name for s in pack.sources] == ["zulu", "alpha", "mike"]


def test_the_continuation_pack_declares_only_the_subject_it_is_for() -> None:
    """A remainder is for ONE subject — the parent's.

    Declaring every subject type would let this pack be offered for a later,
    unrelated subject, where its sources were never chosen and may not apply.
    """
    pack = pack_from_steps((_continuable("s", 0),), subject_type="ip", allowance=1)
    assert pack.subject_types == ("ip",)
    assert pack.sources_for("ip")
    assert not pack.sources_for("domain")


# ── The caps ────────────────────────────────────────────────────────────────


def test_the_caps_keep_a_shape_that_makes_continuing_possible_and_finite() -> None:
    """The two constants are usable numbers — NOT that anything enforces them.

    The docstring here used to claim it pinned enforcement "so neither can
    quietly become advisory", and it did not: deleting the depth check in
    `build_offer`, the allowance refusal in `resolve_selected`, or the offer's
    own exhaustion flag all leave this file green. Verified by reverting each
    one in turn — three mutations, three survivors here.

    What actually binds them needs rows, so it lives in the `_postgres`
    neighbour, where the same three reverts all fail:
    `test_the_cumulative_cap_closes_the_offer_however_it_was_reached` and
    `test_the_depth_cap_stops_a_chain_of_cheap_continuations`.

    So this test keeps the narrow job it can do without a database — the cap
    has to permit more than a single run, or continuing is never possible, and
    it has to be finite, or it is not a cap — and says so.
    """
    assert CHAIN_MAX_ITERATIONS > 0
    assert MAX_CHAIN_DEPTH > 0
    # The cap has to permit more than a single run, or continuing is never
    # possible; and it has to be finite, or it is not a cap.
    assert CHAIN_MAX_ITERATIONS >= 20
    assert CHAIN_MAX_ITERATIONS < 1000
    assert MAX_CHAIN_DEPTH <= 10

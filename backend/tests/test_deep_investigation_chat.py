"""Running a Deep Investigation as a chat turn.

The property under test is the one that makes this branch safe to route a chat
turn through: the assistant text is DERIVED from the run's own summary, so the
sentence the user reads and the block underneath it cannot disagree. A model
asked to summarise the run could call it a success while two sources failed;
this cannot.
"""

import uuid

import pytest

from app.services import deep_investigation_chat as dic
from app.services.investigation_events import INVESTIGATION_MARKER_RE
from app.services.investigation_orchestrator import SourceOutcome
from app.services.investigation_packs import SourceDefinition, SourcePack


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _StubSession:
    """Same minimal stand-in as the orchestrator suite: ids on flush, and the
    one max-position query ``append_lead_step`` issues."""

    def __init__(self):
        self.objects: list[object] = []
        self.commits = 0

    def add(self, obj):
        self.objects.append(obj)

    async def flush(self):
        for obj in self.objects:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        await self.flush()
        self.commits += 1

    async def execute(self, _stmt):
        positions = [o.position for o in self.objects if hasattr(o, "position")]
        return _Result(max(positions) if positions else None)

    async def get(self, _model, _key):
        # No such row. Pack resolution loads the caller to decide visibility,
        # so a turn carrying a `user_id` reaches this; answering None gives
        # the built-ins-only path, which is what an unknown slug must be
        # judged against here.
        return None


def _source(name: str, types=("unknown",)) -> SourceDefinition:
    return SourceDefinition(
        name=name,
        url_template=f"https://{name.lower()}.test/{{value}}",
        subject_types=types,
    )


def _install(monkeypatch, *sources: SourceDefinition, max_iterations: int = 10):
    from app.services import investigation_packs as packs

    pack = SourcePack(
        slug="web-general",
        label="General web",
        description="",
        subject_types=packs.SUBJECT_TYPES,
        sources=sources,
        max_iterations=max_iterations,
    )
    monkeypatch.setattr(packs, "BUILTIN_PACKS", {pack.slug: pack})
    monkeypatch.setattr(packs, "DEFAULT_PACK_SLUG", pack.slug)
    return pack


def _patch_querier(
    monkeypatch, outcomes: dict[str, SourceOutcome], calls: list[str] | None = None
):
    """Replace the querier the chat path builds.

    `make_dispatching_querier` widens a source beyond HTTP: it
    routes on `SourceDefinition.kind` and takes the session, because a
    knowledge base and an MCP tool both need one. Patched by the name this
    module actually calls, so the stub cannot silently stop being used.

    ``calls``, when given, records every source name queried — which is how a
    test asserts that NOTHING was queried, the only way to tell "refused" from
    "ran and found nothing".
    """

    def _factory(*_args, **_kwargs):
        async def _query(source, _subject, _query):
            if calls is not None:
                calls.append(source.name)
            return outcomes.get(source.name, SourceOutcome(findings_count=1))

        return _query

    assert hasattr(dic, "make_dispatching_querier"), (
        "the chat path no longer builds its querier through this name"
    )
    monkeypatch.setattr(dic, "make_dispatching_querier", _factory)


# ---------------------------------------------------------------------------
# The marker and the derived sentence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_assistant_text_carries_the_run_marker(monkeypatch):
    _install(monkeypatch, _source("NVD"))
    _patch_querier(monkeypatch, {})
    text, result = await dic.run_deep_investigation_chat(
        _StubSession(), subject="CVE-2024-3400"
    )
    match = INVESTIGATION_MARKER_RE.search(text)
    assert match
    assert match.group(1) == str(result.investigation.id)


@pytest.mark.asyncio
async def test_the_sentence_reports_failures_before_successes(monkeypatch):
    """An analyst checks what did NOT work first — same order as the block."""
    _install(
        monkeypatch,
        _source("Good", types=("ip",)),
        _source("Down", types=("ip",)),
        _source("Nothing", types=("ip",)),
        _source("Half", types=("ip",)),
    )
    _patch_querier(
        monkeypatch,
        {
            "Down": SourceOutcome(error_type="timeout"),
            "Nothing": SourceOutcome(findings_count=0),
            "Half": SourceOutcome(findings_count=1, partial=True),
        },
    )
    text, _ = await dic.run_deep_investigation_chat(
        _StubSession(), subject="185.220.101.42"
    )
    body = text.split("\n\n")[0]
    assert body.index("1 failed") < body.index("1 with findings")
    assert "1 partial" in body
    assert "1 with nothing" in body


@pytest.mark.asyncio
async def test_the_sentence_cannot_claim_a_success_the_run_did_not_have(monkeypatch):
    """The whole reason no model writes this line."""
    _install(monkeypatch, _source("A"), _source("B"))
    _patch_querier(
        monkeypatch,
        {name: SourceOutcome(error_type="unreachable") for name in ("A", "B")},
    )
    text, result = await dic.run_deep_investigation_chat(_StubSession(), subject="x")
    assert "2 failed" in text
    assert "with findings" not in text
    assert result.summary.all_succeeded is False


@pytest.mark.asyncio
async def test_an_uncovered_subject_says_so_instead_of_reporting_zero_sources(
    monkeypatch,
):
    _install(monkeypatch, _source("IpOnly", types=("ip",)))
    _patch_querier(monkeypatch, {})
    text, result = await dic.run_deep_investigation_chat(
        _StubSession(), subject="Cozy Bear"
    )
    assert "covers subject type `unknown`" in text
    assert "was not investigated" in text
    assert result.summary.total == 0
    assert INVESTIGATION_MARKER_RE.search(text), "the block must still render"


@pytest.mark.asyncio
async def test_the_subject_type_is_classified_and_reported(monkeypatch):
    _install(monkeypatch, _source("Any", types=("ip",)))
    _patch_querier(monkeypatch, {})
    text, result = await dic.run_deep_investigation_chat(
        _StubSession(), subject="8.8.8.8"
    )
    assert result.investigation.subject_type == "ip"
    assert "`ip`" in text


@pytest.mark.asyncio
async def test_a_single_source_run_is_not_pluralised(monkeypatch):
    _install(monkeypatch, _source("Only"))
    _patch_querier(monkeypatch, {})
    text, _ = await dic.run_deep_investigation_chat(_StubSession(), subject="x")
    assert "1 source " in text
    assert "1 sources" not in text


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
async def test_a_blank_subject_is_refused(monkeypatch, bad):
    """The client got it wrong; starting a run that can query nothing hides that."""
    _install(monkeypatch, _source("A"))
    _patch_querier(monkeypatch, {})
    with pytest.raises(dic.EmptySubjectError):
        await dic.run_deep_investigation_chat(_StubSession(), subject=bad)


@pytest.mark.asyncio
async def test_an_oversized_subject_is_truncated_not_refused(monkeypatch):
    """Every source URL is built from the subject, so it must be bounded."""
    _install(monkeypatch, _source("A"))
    _patch_querier(monkeypatch, {})
    _text, result = await dic.run_deep_investigation_chat(
        _StubSession(), subject="A" * (dic.MAX_SUBJECT_LENGTH + 200)
    )
    assert len(result.investigation.subject_value) == dic.MAX_SUBJECT_LENGTH


@pytest.mark.asyncio
async def test_the_conversation_and_user_are_stamped_on_the_run(monkeypatch):
    _install(monkeypatch, _source("A"))
    _patch_querier(monkeypatch, {})
    user, conv = uuid.uuid4(), uuid.uuid4()
    _text, result = await dic.run_deep_investigation_chat(
        _StubSession(), subject="x", user_id=user, conversation_id=conv
    )
    assert result.investigation.user_id == user
    assert result.investigation.conversation_id == conv


@pytest.mark.asyncio
async def test_the_packs_persisted_are_the_packs_queried(monkeypatch):
    """The record must never name a pack that was not used."""
    _install(monkeypatch, _source("A"))
    _patch_querier(monkeypatch, {})
    text, result = await dic.run_deep_investigation_chat(_StubSession(), subject="x")
    assert result.investigation.source_pack_slugs == ["web-general"]
    assert "`web-general`" in text


# ---------------------------------------------------------------------------
# Frames ride the caller's existing channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_callers_on_event_receives_the_investigation_frames(monkeypatch):
    """No transport of its own: the chat socket already forwards status events."""
    _install(monkeypatch, _source("A"), _source("B"))
    _patch_querier(monkeypatch, {})
    frames: list[dict] = []

    async def _on_event(frame):
        frames.append(frame)

    await dic.run_deep_investigation_chat(
        _StubSession(), subject="x", on_event=_on_event
    )
    statuses = [f["status"] for f in frames]
    assert statuses[0] == "investigation_planned"
    assert statuses[-1] == "investigation_complete"
    assert "investigation_step" in statuses


@pytest.mark.asyncio
async def test_a_run_without_an_event_sink_still_completes(monkeypatch):
    _install(monkeypatch, _source("A"))
    _patch_querier(monkeypatch, {})
    _text, result = await dic.run_deep_investigation_chat(
        _StubSession(), subject="x", on_event=None
    )
    assert result.investigation.status == "completed"
# ---------------------------------------------------------------------------
# The turn wrapper: metering, the quota gate, and a message for every failure
#
# These were previously asserted by matching the WebSocket handler's source
# text, because the turn lived inline there with nothing able to call it —
# which is how four of these came to be missing at once without a single test
# noticing. Extracting the turn into a service made them ordinary tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def metering(monkeypatch):
    """Capture what the turn meters and charges, without touching Redis or the
    quota tables."""
    calls: dict[str, list] = {"recorded": [], "checked": [], "charged": []}

    from app.services import agentic_executions, quota

    async def _record(source):
        calls["recorded"].append(source)

    async def _check(_db, uid):
        calls["checked"].append(uid)

    async def _charge(uid, **_kwargs):
        calls["charged"].append(uid)

    async def _refund(source):
        calls["refunded"].append(source)

    async def _refund_usage(uid, **_kwargs):
        calls["uncharged"].append(uid)

    calls["refunded"] = []
    calls["uncharged"] = []
    monkeypatch.setattr(agentic_executions, "record_agentic_execution", _record)
    monkeypatch.setattr(agentic_executions, "refund_agentic_execution", _refund)
    monkeypatch.setattr(quota, "check_agentic_quota", _check)
    monkeypatch.setattr(quota, "increment_agentic_usage", _charge)
    # Patched too, or a refund path reaches real Redis and the fixture's claim
    # to keep the quota tables untouched is false on exactly the paths that
    # exercise it.
    monkeypatch.setattr(quota, "refund_agentic_usage", _refund_usage)
    return calls


@pytest.mark.asyncio
async def test_the_turn_meters_before_it_charges(monkeypatch, metering):
    """Mirrors `agent_chat`: the execution happened whether or not it turned
    out to be chargeable, so the count must not be behind the quota gate."""
    _install(monkeypatch, _source("A"))
    _patch_querier(monkeypatch, {})
    user = uuid.uuid4()
    text = await dic.run_deep_investigation_turn(
        _StubSession(), subject="x", user_id=user
    )
    assert metering["recorded"] == ["deep_investigation"]
    assert metering["checked"] == [user]
    assert metering["charged"] == [user]
    assert "Investigated" in text


@pytest.mark.asyncio
async def test_an_exceeded_quota_is_surfaced_and_no_run_starts(monkeypatch, metering):
    """Leaving the gate out made this mode a way around the per-user limit."""
    from app.services import quota

    _install(monkeypatch, _source("A"))
    calls: list[str] = []

    def _factory(*_args, **_kwargs):
        async def _query(source, _s, _q):
            calls.append(source.name)
            return SourceOutcome(findings_count=1)

        return _query

    monkeypatch.setattr(dic, "make_dispatching_querier", _factory)

    refusal = quota.QuotaExceededError(
        "agentic", limit=50, current=50, period="daily"
    )

    async def _refuse(_db, _uid):
        raise refusal

    monkeypatch.setattr(quota, "check_agentic_quota", _refuse)

    db = _StubSession()
    text = await dic.run_deep_investigation_turn(
        db, subject="x", user_id=uuid.uuid4()
    )
    # The error's own detail is surfaced verbatim, so the user learns which
    # quota and where they stand rather than a generic refusal.
    assert text == str(refusal)
    assert "50/50" in text
    assert calls == [], "no source may be queried once the quota refused"
    assert db.objects == [], "and no run row may be created"
    # Metered on the way in, because the request happened — then refunded,
    # because it was refused BEFORE any work. That is the refund-safe
    # condition `refund_agentic_execution` documents, and skipping it drifts
    # the execution metric away from the quota on precisely the installs that
    # enforce one, where EVERY refusal would be reported as work performed.
    assert metering["recorded"] == ["deep_investigation"]
    assert metering["refunded"] == ["deep_investigation"]


@pytest.mark.asyncio
async def test_a_failure_before_the_run_exists_gives_the_charge_back(monkeypatch, metering):
    """Nothing was queried when the run's skeleton was never written, so the
    user must not lose an agentic action to it."""

    async def _broken(*_a, **_kw):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(dic, "run_deep_investigation_chat", _broken)
    user = uuid.uuid4()
    text = await dic.run_deep_investigation_turn(_StubSession(), subject="x", user_id=user)

    assert text == "The investigation could not be completed."
    assert metering["charged"] == [user]
    assert metering["uncharged"] == [user]
    assert metering["refunded"] == ["deep_investigation"]


@pytest.mark.asyncio
async def test_a_refund_that_fails_does_not_replace_the_answer(monkeypatch, metering):
    from app.services import quota

    async def _broken(*_a, **_kw):
        raise RuntimeError("database unavailable")

    async def _refund_fails(*_a, **_kw):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(dic, "run_deep_investigation_chat", _broken)
    monkeypatch.setattr(quota, "refund_agentic_usage", _refund_fails)
    text = await dic.run_deep_investigation_turn(
        _StubSession(), subject="x", user_id=uuid.uuid4()
    )
    assert text == "The investigation could not be completed."


@pytest.mark.asyncio
async def test_an_anonymous_turn_skips_the_quota_but_is_still_metered(
    monkeypatch, metering
):
    _install(monkeypatch, _source("A"))
    _patch_querier(monkeypatch, {})
    await dic.run_deep_investigation_turn(_StubSession(), subject="x", user_id=None)
    assert metering["recorded"] == ["deep_investigation"]
    assert metering["checked"] == []
    assert metering["charged"] == []


@pytest.mark.asyncio
async def test_a_blank_subject_returns_its_own_message(monkeypatch, metering):
    _install(monkeypatch, _source("A"))
    _patch_querier(monkeypatch, {})
    text = await dic.run_deep_investigation_turn(_StubSession(), subject="   ")
    assert "needs a subject" in text


@pytest.mark.asyncio
async def test_an_unknown_pack_returns_a_recoverable_message(monkeypatch, metering):
    _install(monkeypatch, _source("A"))
    _patch_querier(monkeypatch, {})
    text = await dic.run_deep_investigation_turn(
        _StubSession(), subject="x", source_pack_slugs=["gone"]
    )
    assert "no longer available" in text
    assert "Pick another" in text


@pytest.mark.asyncio
async def test_an_unexpected_failure_still_answers_the_user(monkeypatch, metering):
    """The turn has already consumed the user's message, so an exception that
    escaped here would surface as a socket error with no trace in the chat."""
    _install(monkeypatch, _source("A"))

    def _factory(*_args, **_kwargs):
        async def _query(_source, _s, _q):
            return SourceOutcome(findings_count=1)

        return _query

    monkeypatch.setattr(dic, "make_dispatching_querier", _factory)

    async def _explode(*_a, **_k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(dic, "run_deep_investigation_chat", _explode)
    text = await dic.run_deep_investigation_turn(_StubSession(), subject="x")
    assert text == "The investigation could not be completed."
    assert INVESTIGATION_MARKER_RE.search(text) is None, (
        "it failed before the skeleton existed, so there is no record to "
        "point at and no marker is the honest answer"
    )


_RUN_ID = "7f3c1a52-9d64-4a1b-9c2e-0b5d6e7f8a90"


def _explode_after(frames: list[dict]):
    """A run that emits *frames* and then fails, like a provider dropping
    mid-run once the skeleton and some steps are already committed."""

    async def _run(_db, *, on_event=None, **_kw):
        for frame in frames:
            if on_event is not None:
                await on_event(frame)
        raise RuntimeError("the provider went away mid-run")

    return _run


@pytest.mark.asyncio
async def test_a_failed_run_still_points_at_its_record(monkeypatch, metering):
    """By the time anything can fail, the orchestrator has committed the
    skeleton and every step it reached. Returning the sentence alone tells the
    user the run "could not be completed" while a complete account of what was
    attempted exists and cannot be opened from the conversation — and the
    failure case is exactly when an analyst needs to see which sources were
    queried."""
    monkeypatch.setattr(
        dic,
        "run_deep_investigation_chat",
        _explode_after([{"type": "investigation_planned", "investigation_id": _RUN_ID}]),
    )
    text = await dic.run_deep_investigation_turn(_StubSession(), subject="x")

    assert text.startswith("The investigation could not be completed.")
    assert INVESTIGATION_MARKER_RE.search(text) is not None, (
        "the partial run must stay reachable from the conversation"
    )
    assert _RUN_ID in text, "the marker must name the run that actually ran"


@pytest.mark.asyncio
async def test_a_later_frame_without_an_id_does_not_clobber_it(
    monkeypatch, metering
):
    """Most frames carry no id. Taking the last one instead of the first would
    drop the marker on exactly the path that needs it."""
    monkeypatch.setattr(
        dic,
        "run_deep_investigation_chat",
        _explode_after(
            [
                {"type": "investigation_thinking"},
                {"type": "investigation_planned", "investigation_id": _RUN_ID},
                {"type": "investigation_step_done"},
            ]
        ),
    )
    text = await dic.run_deep_investigation_turn(_StubSession(), subject="x")
    assert _RUN_ID in text


@pytest.mark.asyncio
async def test_capturing_the_id_does_not_disturb_the_callers_event_stream(
    monkeypatch, metering
):
    """The capture wraps ``on_event``; every frame must still reach the caller,
    in order — and a caller that passed none must not be invented."""
    frames = [
        {"type": "investigation_planned", "investigation_id": _RUN_ID},
        {"type": "investigation_step_done"},
    ]
    seen: list[dict] = []

    monkeypatch.setattr(dic, "run_deep_investigation_chat", _explode_after(frames))
    async def _on_event(frame: dict) -> None:
        seen.append(frame)

    await dic.run_deep_investigation_turn(
        _StubSession(), subject="x", on_event=_on_event
    )
    assert seen == frames

    # ...and with no sink at all the turn still fails cleanly.
    monkeypatch.setattr(dic, "run_deep_investigation_chat", _explode_after(frames))
    text = await dic.run_deep_investigation_turn(_StubSession(), subject="x")
    assert _RUN_ID in text



@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed_into_a_message(monkeypatch, metering):
    """`CancelledError` is a BaseException and must keep propagating so the
    turn unwinds and the orchestrator's finalizer marks the run `aborted`.
    Returning a message for a turn being torn down would hide the abort."""
    import asyncio

    _install(monkeypatch, _source("A"))

    async def _cancelled(*_a, **_k):
        raise asyncio.CancelledError

    monkeypatch.setattr(dic, "run_deep_investigation_chat", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        await dic.run_deep_investigation_turn(_StubSession(), subject="x")


@pytest.mark.asyncio
async def test_the_cancel_event_reaches_the_orchestrator(monkeypatch, metering):
    """Consuming a `cancel` frame is useless if the signal stops at the turn."""
    import asyncio

    _install(monkeypatch, _source("A"), _source("B"))
    _patch_querier(monkeypatch, {})
    cancel = asyncio.Event()
    cancel.set()
    text = await dic.run_deep_investigation_turn(
        _StubSession(), subject="x", cancel_event=cancel
    )
    # Nothing was queried, so the sentence reports no findings at all.
    assert "with findings" not in text


@pytest.mark.asyncio
async def test_a_cancelled_run_writes_no_report(monkeypatch, metering):
    """Stop must stop the costly part too: no synthesis after a cancel."""
    import asyncio

    _install(monkeypatch, _source("A"))
    _patch_querier(monkeypatch, {})
    written: list = []

    async def _write(*_a, **_k):
        written.append(True)
        return "a report"

    monkeypatch.setattr(dic, "_write_report", _write, raising=True)
    cancel = asyncio.Event()
    cancel.set()
    await dic.run_deep_investigation_turn(_StubSession(), subject="x", cancel_event=cancel)
    assert written == []


@pytest.mark.asyncio
async def test_a_list_run_reports_under_its_stored_summary(monkeypatch):
    """The writer is handed the subject as stored, not the pasted list: a
    56-line subject in the prompt and the logs is what the summary avoids."""
    from app.services.investigation_packs import list_summary

    _install(monkeypatch, _source("A", types=("domain_list",)))
    _patch_querier(monkeypatch, {})
    asked: list[dict] = []

    async def _write(_db, _result, **kwargs):
        asked.append(kwargs)
        return None

    monkeypatch.setattr(dic, "_write_report", _write, raising=True)
    seeds = [f"newsroom-number-{i}.com" for i in range(40)]
    _text, result = await dic.run_deep_investigation_chat(_StubSession(), subject="\n".join(seeds))
    assert result.investigation.subject_value == list_summary(seeds)
    assert [(a["subject"], a["subject_type"]) for a in asked] == [(list_summary(seeds), "domain_list")]


# ── Structural guards on the WS route ───────────────────────────────────────
#
# The ROUTING behaviour — which turns reach the orchestrator, and what a failed
# one persists — is covered end to end against the real endpoint in
# `test_deep_investigation_turn_route.py`. The two guards below are deliberately
# NOT behavioural: each pins a property of the handler's *shape* that no input
# can exercise, because the shape is what makes the bad input unreachable.
def _send_handler_source() -> str:
    import inspect

    from app.api.v1 import chat

    return inspect.getsource(chat.run_chat_connection)


def test_the_mode_is_read_before_any_early_continue() -> None:
    """A rejected frame must not leave the previous frame's mode behind.

    Python has no block scope: the name lives on the function frame for the
    life of the socket. Today every early `continue` restarts the loop without
    reaching the read, so a stale value is unreachable rather than merely
    unlikely — but that is a property of the current control flow, and anyone
    adding a branch would have to re-derive it. Assigning first makes it true
    by construction, and this pins the ordering so the guarantee cannot be
    quietly undone.
    """
    src = _send_handler_source()
    assign = src.index('_turn_deep_investigation = data.get("mode")')
    empty_guard = src.index('_refuse_send("Empty message"')
    assert assign < empty_guard, (
        "the mode must be read before the first `continue` of the iteration, "
        "or a rejected frame leaves the previous frame's value in place"
    )


def test_the_investigation_import_is_not_inside_the_receive_loop() -> None:
    """A per-frame import hides a circular-import risk behind a runtime path.

    `sys.modules` makes the repeat cheap, but if `deep_investigation_chat`
    ever reaches back into `chat.py` the failure lands at TURN time in
    production rather than at boot. Module scope moves that to import time,
    where a cycle is a startup error somebody sees immediately.
    """
    src = _send_handler_source()
    assert "from app.services.deep_investigation_chat import" not in src, (
        "import it at module scope, not inside the receive loop"
    )


@pytest.mark.asyncio
async def test_a_confirmed_selection_of_nothing_is_refused_not_widened(
    monkeypatch, metering
):
    """`[]` is an answer — "none of these" — and it is not `None`.

    Widening it to the recommendation queries sources the user explicitly did
    not confirm, which is the one thing this feature exists to prevent, and it
    would be invisible: the run looks like any other. Refused with a
    recoverable message, exactly as an unknown pack is.
    """
    _install(monkeypatch, _source("A"))
    calls: list[str] = []
    _patch_querier(monkeypatch, {}, calls)

    db = _StubSession()
    text = await dic.run_deep_investigation_turn(db, subject="x", source_pack_slugs=[])

    assert "No source packs were selected" in text
    assert calls == [], "nothing may be queried when nothing was confirmed"
    assert db.objects == [], "and no run row may be written"
    assert metering["charged"] == [], (
        "a refusal must not spend the user's quota — it queried nothing"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"subject": "x", "source_pack_slugs": []}, "No source packs were selected"),
        ({"subject": "   ", "source_pack_slugs": None}, "subject"),
        ({"subject": "x", "source_pack_slugs": ["no-such-pack"]}, "no longer available"),
    ],
    ids=["empty selection", "blank subject", "unknown pack"],
)
async def test_a_refusal_never_spends_the_users_quota(
    monkeypatch, metering, kwargs, expected
):
    """Metering before the gate is deliberate — the execution happened whether
    or not it was chargeable — but a request refused BEFORE any work is the
    refund-safe condition `refund_agentic_execution` documents, and the same
    reasoning covers the quota.

    The quota is worse than the metric: the metric drifts, the quota is SPENT.
    A user who mis-clicks the picker a few times can be locked out of a
    feature that queried nothing and wrote no row. These three refusals need
    no database and no model, so they must land before the charge.
    """
    _install(monkeypatch, _source("A"))
    calls: list[str] = []
    _patch_querier(monkeypatch, {}, calls)

    db = _StubSession()
    user_id = uuid.uuid4()
    text = await dic.run_deep_investigation_turn(db, user_id=user_id, **kwargs)

    assert expected.lower() in text.lower()
    assert calls == [], "nothing was queried"
    assert db.objects == [], "and no run row was written"
    # Either never charged, or charged and given back — never spent.
    assert metering["charged"] == metering["uncharged"], (
        "the refusal spent the user's quota and did not give it back"
    )
    assert metering["recorded"] == metering["refunded"], (
        "every metered execution that was refused must be refunded"
    )


@pytest.mark.asyncio
async def test_an_absent_selection_still_uses_the_recommendation(
    monkeypatch, metering
):
    """The other half of the distinction: absent means "you choose"."""
    _install(monkeypatch, _source("A"))
    calls: list[str] = []
    _patch_querier(monkeypatch, {}, calls)
    await dic.run_deep_investigation_turn(_StubSession(), subject="x")
    assert calls == ["A"]


@pytest.mark.asyncio
async def test_the_report_write_does_not_dirty_the_callers_session(monkeypatch):
    """`_persist_report` uses its own session precisely so this optional
    feature stops touching the caller's transaction. Assigning the four
    attributes afterwards marked the caller's object dirty, so the chat turn's
    next commit flushed a SECOND update of values already written — handing
    the coupling straight back.
    """
    from types import SimpleNamespace

    from sqlalchemy import inspect as sa_inspect

    from app.models.investigation import Investigation
    from app.services import investigation_report as report_mod

    run = Investigation(
        subject_type="ip",
        subject_value="8.8.8.8",
        source_pack_slugs=[],
        status="completed",
        revision=1,
        iterations_used=1,
        max_iterations=10,
    )
    draft = report_mod.ReportDraft(
        text="The report [1].",
        sources=(
            report_mod.ReportSource(
                n=1, source_name="Web", label="A page", quote="q",
                href="https://a.example/",
            ),
        ),
        used=(1,),
        claim_sentences=1,
    )

    async def _generate(*_a, **_k):
        return draft

    async def _persisted(_investigation_id, _draft):
        return True

    monkeypatch.setattr(report_mod, "generate_report", _generate, raising=True)
    monkeypatch.setattr(dic, "_persist_report", _persisted, raising=True)

    body = await dic._write_report(
        _StubSession(),
        SimpleNamespace(investigation=run, steps=[]),
        subject="8.8.8.8",
        subject_type="ip",
        user_id=None,
    )

    assert body, "the report still reaches the reader"
    assert run.report == "The report [1].", "the caller still sees the new value"
    state = sa_inspect(run)
    dirty = [
        name
        for name in ("report", "report_sources", "report_claims",
                     "report_uncited_claims")
        if state.attrs[name].history.has_changes()
    ]
    assert not dirty, f"these would be flushed a second time: {dirty}"


@pytest.mark.asyncio
async def test_the_message_keeps_the_report_for_every_reader(monkeypatch, metering):
    """The text is what the `deep_investigation` tool hands its caller, what a
    channel posts once the marker is stripped and what the agent's next turns
    read: the report stays in it. Only the web UI, which draws the same report
    in the block, leaves the text out."""
    _install(monkeypatch, _source("A"))
    _patch_querier(monkeypatch, {})

    async def _write(*_a, **_k):
        return "## Findings\n\nThe site is hosted on 1.2.3.4. [1]"

    monkeypatch.setattr(dic, "_write_report", _write, raising=True)
    text = await dic.run_deep_investigation_turn(_StubSession(), subject="x")
    assert "The site is hosted on 1.2.3.4. [1]" in text
    assert "[[INVESTIGATION:" in text

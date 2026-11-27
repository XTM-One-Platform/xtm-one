"""Suite-wide fixtures.

Cross-test isolation only: nothing here should encode behaviour that a
single test could assert for itself.
"""

from __future__ import annotations

import contextlib
import gc
import hashlib
import logging
import os
import shutil
import sys
from pathlib import Path

import pytest

#: The test files this job collects under ``PYTEST_SHARD_SPLIT=files``, as paths from the backend root (``tests/sharding.py``).
_SHARD_FILES: pytest.StashKey[frozenset[str] | None] = pytest.StashKey()

#: The table the shards and the ``app_sources`` groups deal by (``PYTEST_SHARD_DURATIONS``, ``tests/sharding.py``).
_SHARD_DURATIONS: pytest.StashKey[Path] = pytest.StashKey()

#: The test files the merge queue's change reaches (``PYTEST_SELECTED_FILES``, ``tests/selection.py``); None: all.
_SELECTED_FILES: pytest.StashKey[frozenset[str] | None] = pytest.StashKey()


def pytest_addoption(parser):
    """``--repeat N`` runs each selected test N times (``tests/repeat.py``)."""
    from tests import repeat

    repeat.add_option(parser)


def pytest_configure(config):
    """Read ``PYTEST_MARKED_FILES``, ``PYTEST_SELECTED_FILES``, ``PYTEST_SHARD_SPLIT`` and ``PYTEST_SHARD_DURATIONS`` once: a wrong value stops the run before xdist starts its workers.

    Then register ``tests/repeat.py``, the ``--repeat`` plugin.
    """
    from tests import repeat
    from tests.marked_files import MARKED_FILES, parse_marker
    from tests.selection import read_selected_files
    from tests.sharding import dealt_files, durations_path, load_durations, parse_shard, parse_split, shard_files

    repeat.register(config)

    value = os.environ.get("PYTEST_MARKED_FILES")
    shard = os.environ.get("PYTEST_SHARD")
    try:
        marker = config.stash[MARKED_FILES] = parse_marker(value) if value else None
        selected = config.stash[_SELECTED_FILES] = read_selected_files(os.environ.get("PYTEST_SELECTED_FILES"))
        # Every listed file must exist: a list in the wrong form would otherwise collect nothing, and pass.
        missing = sorted(name for name in selected or () if not (config.rootpath / name).is_file())
        if missing:
            raise ValueError(f"PYTEST_SELECTED_FILES lists files that do not exist: {', '.join(missing[:5])}")
        split = parse_split(os.environ.get("PYTEST_SHARD_SPLIT"))
        # Read here, for every run: the app_sources groups deal by it too, with or without shards.
        durations = config.stash[_SHARD_DURATIONS] = durations_path(os.environ.get("PYTEST_SHARD_DURATIONS"))
        files = None
        if shard and split == "files":
            dealt = dealt_files(config.rootpath, selected, marker)
            files = shard_files(dealt, load_durations(durations), *parse_shard(shard))
        config.stash[_SHARD_FILES] = files
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from None


# Longest parameter value spelled out in a test id. Past it the id names the
# value by its type, length and digest: a 5 MB bytes value spelled out made a
# 20 MB node id, printed twice per test by ``-v``, and the shards' logs grew
# past what the CI runner survives.
PARAM_ID_MAX_CHARS = 256


def pytest_make_parametrize_id(config, val, argname):
    """Name a str or bytes parameter longer than ``PARAM_ID_MAX_CHARS`` by its type, length and digest."""
    if isinstance(val, str | bytes) and len(val) > PARAM_ID_MAX_CHARS:
        raw = val.encode("utf-8", "surrogatepass") if isinstance(val, str) else val
        return f"{argname}-{type(val).__name__}{len(val)}-{hashlib.sha256(raw).hexdigest()[:12]}"
    return None


def pytest_ignore_collect(collection_path, config):
    """Leave out the files of other jobs under ``PYTEST_SHARD_SPLIT=files`` (``tests/sharding.py``), the files the change does not reach under ``PYTEST_SELECTED_FILES`` (``tests/selection.py``) and the files that never use the job's marker when ``PYTEST_MARKED_FILES`` is set (``tests/marked_files.py``)."""
    from tests.marked_files import MARKED_FILES, skips_file

    kept = config.stash.get(_SHARD_FILES, None)
    selected = config.stash.get(_SELECTED_FILES, None)
    if (
        (kept is not None or selected is not None)
        and collection_path.suffix == ".py"
        and collection_path.name.startswith("test_")
    ):
        if not collection_path.is_relative_to(config.rootpath):
            return None
        name = collection_path.relative_to(config.rootpath).as_posix()
        if (kept is not None and name not in kept) or (selected is not None and name not in selected):
            return True
    marker = config.stash.get(MARKED_FILES, None)
    if marker is None:
        return None
    return True if skips_file(collection_path, marker) else None


@pytest.hookimpl(tryfirst=True, specname="pytest_collection_modifyitems")
def pytest_collection_modifyitems_group_source_readers(config, items):
    """Run the tests of the files that read ``tests/app_sources.py`` on ``app_sources.XDIST_GROUPS`` xdist workers.

    Its parse cache holds about 1.5 GB per process: one copy per group, not one
    per worker (#10437). Each file goes to one group, dealt longest first by the
    shard weights (``app_sources.xdist_groups``). First, so xdist's ``--dist
    loadgroup`` sees the group; a test already in a group keeps its own.
    """
    from tests import app_sources
    from tests.sharding import DURATIONS_PATH, load_durations

    readers = [
        item
        for item in items
        if item.get_closest_marker("xdist_group") is None and app_sources.reads_sources(item.path)
    ]
    if not readers:
        return
    root = config.rootpath
    names = {
        item: item.path.relative_to(root).as_posix() if item.path.is_relative_to(root) else str(item.path)
        for item in readers
    }
    durations = load_durations(config.stash.get(_SHARD_DURATIONS, DURATIONS_PATH))
    groups = app_sources.xdist_groups(names.values(), durations)
    for item in readers:
        item.add_marker(pytest.mark.xdist_group(groups[names[item]]))


# Neither first nor last: a deeper conftest's hook has added its markers by
# now, and the mark plugin's ``-m`` / ``-k`` deselection comes after.
@pytest.hookimpl(specname="pytest_collection_modifyitems")
def pytest_collection_modifyitems_note_unscanned_markers(config, items):
    """Note the tests whose marker the ``PYTEST_MARKED_FILES`` scan would miss (``tests/marked_files.py``)."""
    from tests.marked_files import UNSCANNED, unscanned_markers

    config.stash[UNSCANNED] = [f"{item.nodeid} ({marker})" for item in items for marker in unscanned_markers(item)]


# Last, so the share is taken of what ``-m`` / ``-k`` left: every job of a
# suite then runs the same number of that suite's tests.
@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Keep this job's share of the tests when ``PYTEST_SHARD`` splits by test (``tests/sharding.py``); a split by file kept it at collection."""
    shard = os.environ.get("PYTEST_SHARD")
    if not shard or config.stash.get(_SHARD_FILES, None) is not None:
        return
    from tests.sharding import parse_shard, shard_node_ids

    kept = shard_node_ids((item.nodeid for item in items), *parse_shard(shard))
    deselected = [item for item in items if item.nodeid not in kept]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item.nodeid in kept]


def pytest_collection_finish(session):
    """Keep what collection built out of every later full garbage collection.

    Collection leaves millions of objects alive (every test module, the whole
    app). A full collection walks them all, holding the GIL for seconds on a CI
    runner, and every other thread of a test - a render's lease thread, a
    watchdog's sampler - stops meanwhile. Collected once, then frozen: a full
    pass walks only what the tests make.

    Then tell the runner watcher collection is over (``tests/runner_speed.py``):
    on CI it names a runner too slow to collect in time.
    """
    gc.collect()
    gc.freeze()
    from tests import runner_speed

    runner_speed.mark_collected()


#: This worker's stall report, opened at its first test (``tests/stall_guard.py``).
_STALL_REPORT: pytest.StashKey = pytest.StashKey()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Fail a hung test's worker with its stacks once ``PYTEST_STALL_SECONDS`` is set (``tests/stall_guard.py``)."""
    from tests import stall_guard

    seconds = stall_guard.stall_seconds()
    if seconds is None:
        return (yield)
    report = item.config.stash.get(_STALL_REPORT, None)
    if report is None:
        report = stall_guard.open_report(os.environ.get("PYTEST_XDIST_WORKER", "main"))
        item.config.stash[_STALL_REPORT] = report
    stall_guard.arm(seconds, report, item.nodeid)
    try:
        return (yield)
    finally:
        stall_guard.disarm(report)


#: This worker's memory ledger, opened at its first test (``tests/memory_report.py``).
_MEMORY_LEDGER: pytest.StashKey = pytest.StashKey()


@pytest.hookimpl(wrapper=True, specname="pytest_runtest_protocol")
def pytest_runtest_protocol_memory(item, nextitem):
    """Add each test's resident memory growth to its file once ``PYTEST_MEMORY_REPORT`` is set (``tests/memory_report.py``)."""
    from tests import memory_report

    if memory_report.report_dir() is None:
        return (yield)
    ledger = item.config.stash.get(_MEMORY_LEDGER, None)
    if ledger is None:
        ledger = memory_report.MemoryLedger()
        item.config.stash[_MEMORY_LEDGER] = ledger
    before = memory_report.resident_bytes()
    try:
        return (yield)
    finally:
        ledger.record(item.nodeid, before, memory_report.resident_bytes())


def pytest_sessionfinish(session, exitstatus):
    """Drop the template databases ``tests/scratch_database.py`` built, and write this worker's memory report.

    Under ``PYTEST_SELECTED_FILES`` a job may collect nothing: its selected
    files hold only tests another job's marker takes (an integration file in a
    unit shard). Its files exist (``pytest_configure``), so that is a pass.
    """
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and session.config.stash.get(_SELECTED_FILES, None):
        session.exitstatus = pytest.ExitCode.OK
    scratch = sys.modules.get("tests.scratch_database")
    if scratch is not None:
        scratch.drop_templates()
    ledger = session.config.stash.get(_MEMORY_LEDGER, None)
    if ledger is not None:
        from tests import memory_report

        directory = memory_report.report_dir()
        if directory is not None:
            memory_report.write_report(directory, os.environ.get("PYTEST_XDIST_WORKER", "main"), ledger)


def reap_loop_watchdogs() -> list[str]:
    """Stop every registered event-loop watchdog. Returns the names stopped.

    Why this must run after *every* test
    ------------------------------------
    ``EventLoopWatchdog`` runs a **daemon sampler thread** that is
    deliberately immune to the loop being blocked — that is the whole point
    of the design, since a blocked loop cannot log about itself. The thread
    therefore also survives the death of the loop it watches: when a test
    starts a watchdog and never stops it (e.g. by exercising
    ``app.worker.startup()``, which calls ``start_loop_watchdog("worker")``,
    without the matching ``shutdown()``), pytest-asyncio closes that test's
    event loop and the orphaned sampler keeps running for the rest of the
    session.

    An orphaned sampler sees a heartbeat that can never be refreshed again,
    concludes the loop is stalled, and emits ``Event loop '<name>' has been
    BLOCKED for ...`` — with the *current* stack of whatever thread id it
    remembers, which is now some unrelated idle loop sitting in
    ``selectors.select``. Those phantom WARNINGs land in the ``caplog`` of a
    later, entirely unrelated test, roughly every ``redump_interval``
    seconds. Any test that asserts on ``app.loop_watchdog`` records — or on
    "no warnings were logged" — can be broken by it, and the failure lands
    on whichever test happens to be running when the re-dump timer fires,
    which is why it presents as a random-ordering flake in CI rather than a
    reproducible one.

    Reaping the whole registry (rather than only watchdogs a given test
    started) is safe and deliberate: pytest-asyncio gives every test a fresh
    event loop, so a watchdog that outlives its test is *always* watching a
    dead loop and can only ever produce a phantom stall.
    """
    from app.core import loop_watchdog

    stopped: list[str] = []
    for name in list(loop_watchdog._watchdogs):
        stopped.append(name)
        # One uncooperative watchdog must not strand the rest. ``stop()``
        # sets the sampler's stop event before anything that could fail, so
        # even on error the thread exits within one interval.
        with contextlib.suppress(Exception):
            loop_watchdog.stop_loop_watchdog(name)
    return stopped


@pytest.fixture(autouse=True)
def _no_platform_event_dispatch(monkeypatch):
    """Keep platform event delivery out of the unit suite.

    A work item write publishes its platform events on the in-process bus, and
    the dispatcher reads the subscribed assignments from the database. A unit
    test has none: the listener is marked registered so it is never added, the
    bus finds no listener, and nothing is scheduled past the test. The tests of
    the dispatcher call it directly.
    """
    from app.services import platform_events

    monkeypatch.setattr(platform_events, "_registered", True)
    yield


@pytest.fixture(autouse=True)
def _ctem_case_opening_on(request, monkeypatch):
    """CTEM case opening is on in the unit suite (#1951), as on a fresh install.

    Every case-opening path reads the switch from ``platform_settings``; a unit test runs on
    a fake session and a scratch database may hold no such table. A module that tests the
    switch itself sets ``READS_CASE_OPENING_SETTING = True`` and reads the real row.
    """
    if not getattr(request.module, "READS_CASE_OPENING_SETTING", False):
        from app.services import ctem_case_opening

        async def on_everywhere(_db):
            return set()

        monkeypatch.setattr(ctem_case_opening, "read_paused_verticals", on_everywhere)
    # #2570: the per-minute opening budget is off in the suite (a test opens as many cases as it
    # seeds); a module that tests the budget sets ``TESTS_CASE_OPENING_BUDGET = True``.
    if not getattr(request.module, "TESTS_CASE_OPENING_BUDGET", False):
        monkeypatch.setenv("CTEM_CASE_OPEN_PER_MINUTE", "0")
    yield


@pytest.fixture(autouse=True)
def _health_findings_open_on_one_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Platform Health findings open on the first failing read here, with no startup grace.

    The check suites pin what each check reads, one pass at a time. In production a
    finding opens on its second failing read in a row and XTM One's availability
    checks have a grace after the process starts (#958); the tests OF that rule
    (``test_health_restart_grace.py``) put the production values back.
    """
    from datetime import timedelta

    from app.services.health import engine

    monkeypatch.setattr(engine, "OPEN_AFTER_FAILS", 1)
    monkeypatch.setattr(engine, "STARTUP_GRACE", timedelta(0))


@pytest.fixture(autouse=True)
def _ctem_exclusions_read_fresh() -> None:
    """Every test reads the CTEM source exclusions afresh: the recorders' cache (#2583 point 7) never leaks between tests."""
    from app.services.ctem_signal_sources import forget_exclusions

    forget_exclusions()


@pytest.fixture(autouse=True)
def _agents_in_no_fleet(monkeypatch):
    """Keep the coordination scope's fleet read out of the unit suite.

    ``list_work_items``, ``get_work_item`` and ``claim_work_item`` resolve the
    acting agent's home fleet from the database. A unit test has none: every
    agent is in no fleet here, which reads as before the scope existed. The
    tests OF the scope put their own fleet back.
    """
    from app.services import coordination_scope

    async def no_fleet(_agent_id):
        return None

    monkeypatch.setattr(coordination_scope, "read_home_fleet", no_fleet)


@pytest.fixture(autouse=True)
def _no_leaked_loop_watchdogs():
    """Guarantee no watchdog sampler thread outlives the test that started it."""
    yield
    reap_loop_watchdogs()


@pytest.fixture(scope="session", autouse=True)
def _switchable_redis_sockets():
    """Let ``_no_redis_in_the_unit_suite`` refuse every Redis socket."""
    from tests.redis_unreachable import switchable_sockets

    with switchable_sockets():
        yield


@pytest.fixture(autouse=True)
def _no_redis_in_the_unit_suite(request):
    """Give every unit test a Redis that refuses the connection, as CI's does.

    CI's unit job runs with no Redis; a developer's machine usually has one.
    A unit test that reached it wrote keys the next run still finds (an A2A
    messageId claim lives 24 h) and, through a pooled connection bound to an
    earlier test's closed event loop, reached it or not depending on the test
    order. ``tests/redis_unreachable.refusing`` refuses every connection of
    the test, through the shared client or any other, without the test's
    ``monkeypatch``: its ``undo()`` cannot lift the refusal. The tests OF
    Redis-backed code fake ``redis_client`` themselves; tests marked
    ``integration`` keep the real Redis.
    """
    from tests import redis_unreachable

    if request.node.get_closest_marker("integration") is not None:
        redis_unreachable.real_redis()
        yield
        return
    with redis_unreachable.refusing():
        yield


@pytest.fixture(scope="session")
def _insert_frames_root(tmp_path_factory):
    return tmp_path_factory.mktemp("insert-frames")


@pytest.fixture(autouse=True)
def _insert_frames_of_the_test(_insert_frames_root, request, monkeypatch):
    """Every test keeps the screen-insert frames it draws in a folder of its own.

    ``insert_frames`` keeps them on the worker's disk for every render after: a test drawing an insert that another
    test or an earlier run drew - the same files, the same fields - would be served those frames instead of drawing
    its own. The folder is made only by a test that draws one.
    """
    from app.services.video_studio import insert_frames

    folder = _insert_frames_root / hashlib.sha256(request.node.nodeid.encode("utf-8")).hexdigest()[:16]
    monkeypatch.setattr(insert_frames, "ROOT", folder)
    yield
    # No other test reads it: freed as the test ends, so a session drawing at 2160p never holds every test's.
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(autouse=True)
def _fresh_oidc_discovery_cache():
    """A discovery document cached by one test must not answer the next test's fetch."""
    from app.api.auth_sso import clear_oidc_discovery_cache

    clear_oidc_discovery_cache()
    yield


@pytest.fixture(autouse=True)
def _no_headless_layout_check(monkeypatch):
    """Keep the deck layout check out of the unit suite.

    Every ``generate_presentation`` save is followed by a render of the deck in
    headless Chromium. In a unit test there is no frontend to render and no
    browser to render it with, so the check could only ever wait for a
    navigation timeout and report itself skipped - a slow no-op on every deck
    test. Disabled here, exactly as a deployment without a browser disables it;
    the tests OF the check re-enable it on ``settings`` themselves and fake the
    render.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "presentation_layout_check_enabled", False)
    yield


@pytest.fixture(autouse=True)
def _forget_tools_refusals():
    """An endpoint's remembered tools refusal (``openai_compat``) is process-wide: clear it per test."""
    from app.llm.openai_compat import forget_tools_refusals

    forget_tools_refusals()
    yield
    forget_tools_refusals()


@pytest.fixture(autouse=True)
def _reset_ai_state_cache():
    """Drop the cached AI state around every test.

    ``services/ai_state`` caches the resolved answer process-wide for a few
    seconds so the request path does no database work. In a test run that makes
    results order-dependent: whichever test resolves first decides the answer the
    next one sees. Cleared both sides so neither direction can leak.

    ``_last_read_failed`` is process-wide state of the same module and is reset
    with it: ``invalidate_cache`` deliberately leaves it alone, so a test whose
    ``is_ai_enabled`` read failed (a session double, a closed engine) left it
    ``True`` for the next test on the same xdist worker, and the A2A pre-flight
    then reported "could not be read" where the test expected
    "chat unavailable" (merge_group run 44561564902, gw3).
    """
    from app.services import ai_state

    ai_state.invalidate_cache()
    ai_state._last_read_failed = False
    yield
    ai_state.invalidate_cache()
    ai_state._last_read_failed = False


@pytest.fixture(autouse=True)
def _restore_cached_branding():
    """Put the process-wide branding snapshot back after every test.

    ``services/branding`` keeps one synchronous snapshot that tool descriptions
    read at build time (the platform group names the product 18 times). A test
    that swaps it (``refresh_branding_cache`` on a stub row, a direct
    assignment) changed every later measure on the same worker: the schema
    budget read 18 characters more after ``tests/test_branding.py``.
    """
    from app.services import branding

    saved = branding._cached_branding
    yield
    branding._cached_branding = saved


@pytest.fixture(autouse=True)
def _no_group_disables_ai():
    """Answer "no group switches AI off" for every test, without a read.

    ``services/group_ai_access`` holds the set of AI-disabled groups process-wide
    and the LLM router asks it on every model resolution, so a test whose
    session is a stand-in would otherwise meet an unexpected ``execute`` on its
    first resolution. The expiry is infinite on purpose: a slow test must not
    outlive it. The tests OF the feature set the cache themselves.
    """
    from app.services import group_ai_access

    group_ai_access._cache = (frozenset(), float("inf"))
    yield
    group_ai_access.invalidate_cache()


@pytest.fixture(autouse=True)
def _reset_preview_lane_read():
    """Drop the preview lane's workers read and the figure fits around every test: kept process-wide, a
    test that patches what they read would otherwise see the last one's."""
    from app.services.video_studio import figures, preview_routing

    preview_routing.forget_lane_workers()
    figures.forget_figure_fits()
    yield
    preview_routing.forget_lane_workers()
    figures.forget_figure_fits()


@pytest.fixture(autouse=True)
def _health_spend_limits_unlocked(monkeypatch):
    """Apply the Platform Health spend ceilings a test stores (#1202).

    Production locks them at their defaults unless
    ``PLATFORM_HEALTH_SPEND_LIMITS_UNLOCKED`` is true; the budget, analysis and
    Module Analyst tests store small ceilings to reach them, so the suite runs
    unlocked. ``test_health_spend_lock.py`` locks them itself.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "platform_health_spend_limits_unlocked", True)
    yield


@pytest.fixture(autouse=True)
def _reset_security_options_snapshot(monkeypatch):
    """Forget the stored security options, and lock none of them.

    The snapshot is process-wide, like the AI state: a test that publishes a
    stored value would otherwise switch that option for every test after it.
    Which options a variable locks is what ``settings`` read when the test
    process imported it - the developer's own environment and ``.env``
    files - so no test inherits a lock from them; a test that needs one sets
    the variable and reads it again (``test_security_options._Environment``).
    """
    from app.core import security_options
    from app.core.config import settings

    monkeypatch.setattr(settings, "_environment_fields", frozenset())
    security_options.reset_snapshot()
    yield
    security_options.reset_snapshot()


@pytest.fixture(autouse=True)
def _reset_trace_switches_snapshot():
    """Put the trace storage and payload switches back to their defaults.

    Process-wide like the security options snapshot they are read with: a
    test that publishes "storage off" would otherwise drop every trace the
    tests after it record.
    """
    from app.services import trace_switches

    trace_switches.reset_trace_switches()
    yield
    trace_switches.reset_trace_switches()


@pytest.fixture(autouse=True)
def _reset_vault_resolver():
    """No vault provider unless a test declares one.

    The process's resolver is built from the environment (and the ``.env``
    files) on first use and kept: a test must neither read a developer's
    vault declarations nor leave its own to the next test. Nor is the reference
    scan another test's database answered kept for the page counts.
    """
    from app.services import vault
    from app.services.vault.resolver import SecretResolver, set_resolver
    from app.services.vault.usage import clear_reference_scan_cache

    set_resolver(SecretResolver({}, {}, 0))
    vault._cleared_at.clear()
    clear_reference_scan_cache()
    yield
    set_resolver(None)
    vault._cleared_at.clear()
    clear_reference_scan_cache()


@pytest.fixture(autouse=True)
def _reset_agentic_gate_cache():
    """Drop the background gate's cached licence answer around every test.

    ``services/agentic_gate`` caches the licence process-wide and keeps
    the last answer read as the fallback for a failed read, so one test's
    edition would otherwise decide the next one's. Both are cleared, both sides.
    """
    from app.services import agentic_gate

    agentic_gate.invalidate_cache()
    agentic_gate._last_known = None
    agentic_gate._inflight = None
    yield
    agentic_gate.invalidate_cache()
    agentic_gate._last_known = None
    agentic_gate._inflight = None


@pytest.fixture(autouse=True)
def _reset_prompt_guard_connection_cache():
    """Forget the cached "no prompt guard connected" answer around every test.

    ``services/prompt_guards`` keeps it process-wide for a few seconds, so a
    turn with no guard connected opens no session: one test's integration
    state would otherwise decide whether the next test's guard runs at all.
    """
    from app.services import prompt_guards

    prompt_guards.invalidate_connection_cache()
    yield
    prompt_guards.invalidate_connection_cache()


def _forget_tool_result_screenings() -> None:
    module = sys.modules.get("app.services.tool_result_guard")
    if module is not None:
        module.reset_background()


@pytest.fixture(autouse=True)
def _reset_tool_result_screenings():
    """Forget the background tool-result screenings around every test.

    ``services/tool_result_guard`` keeps them process-wide under a bounded
    backlog, and a task left by a test's closed event loop never ends: it
    would count against the next test's backlog. Only a module already
    imported is touched.
    """
    _forget_tool_result_screenings()
    yield
    _forget_tool_result_screenings()


def _forget_compaction_pauses() -> None:
    module = sys.modules.get("app.services.conversation_memory")
    if module is not None:
        module._compaction_cuts.clear()


@pytest.fixture(autouse=True)
def _reset_compaction_pauses():
    """Forget the compactor pauses around every test.

    ``services/conversation_memory`` pauses a conversation's compactor calls
    process-wide after repeated ``max_tokens`` cuts, so one test's cuts would
    otherwise pause the next test's compaction of the same conversation id.
    Only a module already imported is touched.
    """
    _forget_compaction_pauses()
    yield
    _forget_compaction_pauses()


@pytest.fixture(autouse=True)
def _no_cross_process_compaction_claim(monkeypatch):
    """Keep the stored compaction state out of the unit suite.

    A compacting turn with a conversation id claims the conversation in Redis
    and reads its stored compaction state from the database. The claim meets
    the refused Redis of ``_no_redis_in_the_unit_suite``; the read would wait
    on a refused database connection - or, pointed at a developer's services,
    read them. Both fail open, exactly as a deployment without them does; the
    tests OF the claim fake them themselves. Only a module already imported is
    touched.
    """
    module = sys.modules.get("app.services.conversation_memory")
    if module is not None:

        async def _no_stored_state(conversation_id):
            return None

        monkeypatch.setattr(module, "_load_stored_compaction", _no_stored_state)
    yield


@pytest.fixture(autouse=True)
def _notices_decided_per_process(monkeypatch):
    """Keep the platform-wide notice claim out of the unit suite.

    A notice said once for the platform claims its interval in Redis. A unit
    test has none: each claim could only wait on a refused connection. The
    process decides alone here, synchronously, and every test starts with no
    notice said; the tests OF the claim fake Redis themselves. A decision one
    of them left in flight belongs to its own event loop and is dropped with it.
    """
    from app.core import log_once

    monkeypatch.setattr(log_once, "shared_store", None)
    log_once.forget()
    yield
    log_once.forget()
    log_once._tasks.clear()


@pytest.fixture(autouse=True)
def _no_github_token_installation_lookups(monkeypatch):
    """Keep Redis and GitHub out of the token check in the unit suite.

    Without a store no answer is read or kept, so a strict-mode delivery through
    an integration recording no installation is reached as before, and no
    lookup is started. The tests OF the check install a fake store and a fake
    GitHub themselves; a lookup that slips through fails instead of calling
    api.github.com.
    """
    from app.services import github_token_installations

    async def _no_github(_token: str):
        raise github_token_installations.InstallationLookupError("GitHub is not asked in the test suite")

    monkeypatch.setattr(github_token_installations, "redis_store", None)
    monkeypatch.setattr(github_token_installations, "fetch_installations", _no_github)
    github_token_installations.reset_state()
    yield
    github_token_installations.reset_state()


@pytest.fixture(autouse=True)
def _no_durable_quota_baseline(monkeypatch):
    """Keep the platform-wide quota read-back out of the unit suite.

    A platform-wide counter Redis does not hold is read back from the database
    before the gate trusts it or a charge increments it. A unit test has no
    database: each read could only wait on a refused connection - or, pointed
    at a developer's services, read them. It answers "nothing saved yet" here,
    the state of a fresh install; the tests OF the read-back fake it
    themselves, and the PostgreSQL suite puts the real one back.
    """
    from app.services import quota

    async def _nothing_saved(usage_type: str, period_key: str) -> int:
        return 0

    monkeypatch.setattr(quota, "_load_global_baseline", _nothing_saved)
    yield


@pytest.fixture(autouse=True)
def _frozen_quota_clock(monkeypatch):
    """Read every quota period at one frozen instant.

    A test pins a period key (``_current_period_key("monthly")``) and the code
    under test reads it again: a run crossing 00:00 UTC split the two (the test
    held ``2026-10``, the code ``2026-11``) and went red on main. Both read
    ``tests.frozen_clock.FROZEN_NOW`` here, whatever the wall clock; a test of
    the rollover itself passes its own ``now`` or pins its own clock on
    ``quota.datetime``, which this fixture then answers instead.
    """
    from datetime import UTC, datetime

    from app.services import quota
    from tests.frozen_clock import FROZEN_NOW

    def _frozen_utcnow() -> datetime:
        # A test that swapped the module's ``datetime`` for a clock of its own
        # (the last second of a day, say) keeps it: only the wall clock is frozen.
        if quota.datetime is not datetime:
            return quota.datetime.now(UTC)
        return FROZEN_NOW

    monkeypatch.setattr(quota, "_utcnow", _frozen_utcnow)
    yield


@pytest.fixture(autouse=True)
def _quota_store_answers(monkeypatch):
    """Keep the quota store probe out of the unit suite.

    An assignment run's quota check (while quotas fail closed), a delegated
    task's start and a held execution's re-check ask whether the quota store
    accepts a write. A unit test has no Redis: every probe would find the
    store out and hold the work. The store answers here, at the probe's one
    source, as it did for every test before the probe existed; the tests OF
    the hold fake an outage themselves.
    """
    from unittest.mock import AsyncMock

    from app.services import quota

    monkeypatch.setattr(quota, "quota_store_outage", AsyncMock(return_value=None))
    yield


def _forget_attachment_memos() -> None:
    from app.llm import attachment_cache

    attachment_cache.clear_all()
    extract = sys.modules.get("app.services.attachment_extract")
    if extract is not None:
        extract.clear_render_cache()


@pytest.fixture(autouse=True)
def _reset_attachment_cache():
    """Empty the attachment memos around every test.

    ``llm/attachment_cache`` keeps downloaded bytes, digests, page counts and
    prepared images process-wide, keyed by storage key or content, and
    ``services/attachment_extract`` keeps renderings and plain extractions per
    content hash. Tests reuse the same keys and bytes with different fakes (a
    failing rasterizer, a counting storage, a patched extractor), so one test's
    result would otherwise be the next one's.
    """
    _forget_attachment_memos()
    yield
    _forget_attachment_memos()


@pytest.fixture(autouse=True)
def _no_inventory_group_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the inventory read behind an OpenAEV asset-group id out of the unit suite (#2714).

    ``get_openaev_asset_group`` first asks the organization inventory whether the id
    is an inventory row synced from OpenAEV (``group_source_id``), through the database.
    A unit test has none: the read could only wait on a refused connection. It answers
    "not an inventory id" here; the tests OF the mapping call the real one on a scratch
    database or patch their own.
    """
    from app.services import organization_inventory_query

    async def not_an_inventory_id(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(organization_inventory_query, "group_source_id", not_an_inventory_id)


@pytest.fixture(autouse=True)
def _no_prompt_camera_reading(monkeypatch):
    """Keep the prompt reading a take's stabilisation asks for out of the unit suite.

    Before an AI shot is measured, the worker asks the person's chat model
    which camera its prompt wants (``content_drift.prompt_camera``), resolving
    that model through the database. A unit test has none: the read could
    only wait on a refused connection. It answers what the shot's params
    already keep here - a camera a test wrote there, else none, the state of
    a prompt never read; the tests OF the reading put the real one back.
    """
    from app.services.video_studio import content_drift

    async def kept(_user_id, *, prompt, negative_prompt, params):
        return content_drift.kept_camera(params)

    monkeypatch.setattr(content_drift, "prompt_camera", kept)


@pytest.fixture(autouse=True)
def _no_start_picture_reading(monkeypatch):
    """Keep the reading of a shot's start picture before it is paid (dev:invented-text-check) out of the unit suite.

    ``clips.plan_clip`` has the person's chat model read the start picture's
    lettering (``text_drift.read_start_picture``), resolving that model
    through the database. A unit test has none: the read could only wait on a
    refused connection. It answers "not read" here - the shot asked as before,
    no lettering line; the tests OF the reading put the real one, or their
    own, back.
    """
    from app.services.video_studio import text_drift

    async def unread(_user_id, _picture):
        return None

    monkeypatch.setattr(text_drift, "read_start_picture", unread)


@pytest.fixture(autouse=True)
def _no_start_negative_reading(monkeypatch):
    """Keep the reading of a shot's start picture against its negative prompt (issue #10053) out of the unit suite.

    ``clips.plan_clip``, ``clips.plan_restart`` and the worker's content check
    have the person's chat model say which negative terms the start picture
    already shows (``content_drift.read_start_picture``), resolving that model
    through the database. A unit test has none: it answers "not read" here -
    the take judged on every frame as before; the tests OF the reading put the
    real one back.
    """
    from app.services.video_studio import content_drift

    async def unread(_user_id, _picture, _negative_prompt):
        return None

    monkeypatch.setattr(content_drift, "read_start_picture", unread)

    async def unread_content(_user_id, _picture, **_asked):
        return None

    # The reading of the start picture against the prompt's subjects (issue #10895): "not read", never refused.
    monkeypatch.setattr(content_drift, "read_start_content", unread_content)


@pytest.fixture(autouse=True)
def _own_figure_cache(monkeypatch, tmp_path_factory):
    """Give each test a figure cache of its own.

    A worker keeps every figure drawing in one folder per key
    (``video_studio.figure_cache.ROOT``, under the system's temporary
    directory): tests drawing the same synthetic figure would otherwise take
    one another's frames and videos, across tests and across xdist workers.
    """
    from app.services.video_studio import figure_cache, render_cache

    figures, renders = tmp_path_factory.mktemp("figures"), tmp_path_factory.mktemp("render-cache")
    monkeypatch.setattr(figure_cache, "ROOT", figures)
    # The render cache too: one test's composed bed or followed track is never another's.
    monkeypatch.setattr(render_cache, "ROOT", renders)
    yield
    # Never another test's: freed as the test ends.
    for folder in (figures, renders):
        shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(autouse=True)
def _video_ultra_frees_its_disk(request):
    """Free a ``video_ultra`` test's working directory as the test ends.

    The ``Video Studio ultra render`` workflow runs its 2160p renders in one session, and pytest keeps every test's
    ``tmp_path`` - its media, its segments, its encoded video, its evidence frames - until the session ends: the runner's
    disk quota ran out on whichever test came last. What each held is logged, the measure of the next one's room.
    """
    if request.node.get_closest_marker("video_ultra") is None:
        yield
        return
    held = request.getfixturevalue("tmp_path")
    yield
    size = 0
    for root, _dirs, files in os.walk(held):
        for name in files:
            with contextlib.suppress(OSError):
                size += os.path.getsize(os.path.join(root, name))
    shutil.rmtree(held, ignore_errors=True)
    logging.getLogger(__name__).warning(
        "%s held %.2f GiB in its working directory, freed", request.node.nodeid, size / 2**30
    )


@pytest.fixture(autouse=True)
def _same_worker_memory_on_every_machine(monkeypatch):
    """Give the Video Studio worker the same physical memory wherever the suite runs.

    Without a container limit, the room of a render is the host's memory less
    the preview reserve (``render_process.launch_room``), a hard floor of
    8 GiB: on a 7.7 GiB CI runner every render a test starts would be refused
    before it ran, on a larger machine never. The container's own limit is
    hidden the same way: a suite run inside a container (a 4 GiB sandbox) reads
    no cgroup memory limit, so its renders are not refused either. The tests of
    a worker's size set their own; every other one runs on a 32 GiB host with
    no container limit.
    """
    from app.services.video_studio import resources

    missing = "/nonexistent/xtm-one-tests/memory"
    monkeypatch.setattr(resources, "_CGROUP_V2_MEMORY_MAX", missing)
    monkeypatch.setattr(resources, "_CGROUP_V1_MEMORY_LIMIT", missing)
    monkeypatch.setattr(resources, "host_memory", lambda: 32 << 30)


@pytest.fixture
def video_studio_enabled(monkeypatch):
    """A deployment with the Video Studio turned on (``ENABLED_FEATURES=VIDEO_STUDIO``).

    The Studio is off by default, so a test of its behaviour - its routes, its
    seeded templates, its public links - asks for it. Every reader goes through
    ``is_feature_enabled``, which reads the setting on each call.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "enabled_features", "VIDEO_STUDIO")


@pytest.fixture
def dashboard_studio_enabled(monkeypatch):
    """A deployment with the Dashboard Studio turned on (``ENABLED_FEATURES=DASHBOARD_STUDIO``).

    Live platform widgets, widget packs and publishing are off by default.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "enabled_features", "DASHBOARD_STUDIO")


@pytest.fixture
def mobile_companion_enabled(monkeypatch):
    """A deployment with the mobile companion turned on (``ENABLED_FEATURES=MOBILE_COMPANION``).

    The companion is off by default, so a test of its behaviour - its routes,
    the PIN lock, phone pushes, the ``phone`` tool - asks for it.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "enabled_features", "MOBILE_COMPANION")


@pytest.fixture
def no_default_video_template(monkeypatch):
    """No built-in video template seeded: a shot asked for no video takes no art direction.

    For the tests that drive ``clips.start_clip`` / ``restart_clip`` on a fake
    session answering ``get`` alone, where the default template's lookup has
    nothing to query.
    """
    from app.services.video_studio import templates

    async def no_template(_db):
        return None

    monkeypatch.setattr(templates, "default_template", no_template)


@pytest.fixture(autouse=True)
def no_configured_video_models(request, monkeypatch):
    """No default video model configured, for the Video Studio tests: the models a test names are kept.

    Their fake sessions answer ``get`` alone, where the platform settings
    have nothing to query. ``test_video_studio_configured_models`` proves the
    rule with a configured default (#10838).
    """
    if "video" not in request.module.__name__:
        return
    from app.services.video_studio import clips

    async def no_configured_models(_db):
        return None, []

    monkeypatch.setattr(clips, "configured_models", no_configured_models)


@pytest.fixture
def evaluation_enabled(monkeypatch):
    """A deployment with evaluation on: a mode where AI runs and ``ENABLED_FEATURES=EVALUATION``.

    Evaluation is off by default, and never on in ``ai_disabled`` mode, so a test of
    its behaviour asks for it. ``dev`` runs AI without depending on a
    licence check of its own.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "enabled_features", "EVALUATION")
    monkeypatch.setattr(settings, "platform_mode", "dev")

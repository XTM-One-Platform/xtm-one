"""Inbound channel message processing must be bounded + tracked.

Every channel webhook (Slack / Teams / WhatsApp / Telegram / Discord / Google
Chat) acknowledges the platform with 200 immediately and processes the message
in a fire-and-forget background task that opens its OWN DB session (routing +
the agentic loop). With a bare ``asyncio.create_task`` an inbound burst — e.g. a
busy Teams tenant at the start of the workday — spawns an UNBOUNDED number of
concurrent DB-session-holding tasks that each contend for the small web-process
connection pool, exhausting it (the production morning pool-exhaustion incident)
and tripping uvicorn's concurrency backstop.

``spawn_channel_task`` fixes this: a process-wide semaphore caps how many
inbound messages process at once (queueing the rest), a strong reference keeps
the task from being garbage-collected mid-flight, and exceptions are logged
instead of silently swallowed. These tests pin that contract and guard against a
channel regressing back to a bare ``asyncio.create_task`` for message
processing.
"""

import ast
import asyncio
import inspect

import pytest

from app.channels import base as channel_base
from app.channels.base import spawn_channel_task
from app.core.config import settings


@pytest.fixture(autouse=True)
def _reset_channel_task_state():
    # The semaphore is created lazily from settings and cached on the module,
    # so reset it (and the tracked-task set) around every test to isolate the
    # configured limit. Clear on BOTH sides of the yield so a prior test that
    # failed early (leaving a task pending) cannot leak state into the next one.
    channel_base._channel_processing_sem = None
    channel_base._channel_tasks.clear()
    yield
    channel_base._channel_processing_sem = None
    channel_base._channel_tasks.clear()


async def test_spawn_channel_task_caps_concurrency(monkeypatch):
    """At most ``channel_max_concurrent_processing`` tasks run at once; the rest
    queue on the semaphore instead of stampeding the pool."""
    monkeypatch.setattr(settings, "channel_max_concurrent_processing", 3)

    running = 0
    max_running = 0
    gate = asyncio.Event()

    async def worker():
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await gate.wait()
        running -= 1

    tasks = [spawn_channel_task(worker(), name="t") for _ in range(8)]
    # Let the event loop run every task that can acquire the semaphore.
    await asyncio.sleep(0.05)
    assert max_running == 3, (
        f"expected at most 3 concurrent tasks, saw {max_running}"
    )

    gate.set()
    await asyncio.gather(*tasks)
    # Every worker entered and exited cleanly (none wedged mid-flight), so the
    # running counter is back to zero...
    assert running == 0
    # ...and all tasks were untracked on completion (no strong-ref leak).
    assert len(channel_base._channel_tasks) == 0


async def test_spawn_channel_task_unbounded_when_disabled(monkeypatch):
    """A non-positive limit restores the legacy unbounded behaviour (escape
    hatch) — all tasks run concurrently with no semaphore."""
    monkeypatch.setattr(settings, "channel_max_concurrent_processing", 0)

    running = 0
    max_running = 0
    gate = asyncio.Event()

    async def worker():
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await gate.wait()
        running -= 1

    tasks = [spawn_channel_task(worker()) for _ in range(6)]
    await asyncio.sleep(0.05)
    assert max_running == 6

    gate.set()
    await asyncio.gather(*tasks)
    assert channel_base._get_channel_semaphore() is None


async def test_spawn_channel_task_tracks_and_releases(monkeypatch):
    """The task is held in a strong-reference set while running and removed on
    completion, so it can never be garbage-collected mid-flight."""
    monkeypatch.setattr(settings, "channel_max_concurrent_processing", 5)
    gate = asyncio.Event()

    async def worker():
        await gate.wait()

    task = spawn_channel_task(worker(), name="tracked")
    assert task in channel_base._channel_tasks

    gate.set()
    await task
    # The done-callback (which discards from the set) runs on the next loop tick.
    await asyncio.sleep(0)
    assert task not in channel_base._channel_tasks


async def test_spawn_channel_task_logs_exceptions(monkeypatch, caplog):
    """A failing processing task must not crash the loop and must log (not
    swallow) its exception."""
    monkeypatch.setattr(settings, "channel_max_concurrent_processing", 5)

    async def boom():
        raise ValueError("kaboom")

    with caplog.at_level("ERROR"):
        task = spawn_channel_task(boom(), name="boomer")
        # Awaiting the wrapper task must not re-raise into the caller.
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # let the done-callback log + untrack run

    assert "boomer" in caplog.text
    assert "kaboom" in caplog.text
    # The failure must be logged WITH a real traceback. Inside a done-callback
    # there is no active exception, so ``exc_info=True`` would log an empty
    # ``(None, None, None)``; the helper passes the exception instance, which
    # stdlib logging expands to ``(type, exc, exc.__traceback__)``. Assert the
    # record carries that traceback so a regression to ``exc_info=True`` (which
    # would silently drop the stack) fails here.
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert error_records, "expected an ERROR log record for the failing task"
    record = error_records[-1]
    assert record.exc_info is not None
    assert record.exc_info[0] is ValueError
    assert record.exc_info[2] is not None  # traceback object captured
    assert "Traceback (most recent call last)" in caplog.text
    assert task not in channel_base._channel_tasks


async def test_spawn_channel_task_names_an_exception_that_has_no_message(monkeypatch, caplog):
    """The failure line goes through ``format_exception_message``: never ``failed: ``."""
    monkeypatch.setattr(settings, "channel_max_concurrent_processing", 5)

    async def times_out():
        raise TimeoutError

    with caplog.at_level("ERROR"):
        task = spawn_channel_task(times_out(), name="slow")
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    (record,) = [r for r in caplog.records if r.levelname == "ERROR"]
    assert record.getMessage() == "Channel processing task slow failed: TimeoutError"


async def test_spawn_channel_task_releases_slot_on_exception(monkeypatch):
    """A task that raises must still release its semaphore slot.

    ``async with sem`` releases on the exception path, so one failure can never
    permanently consume a slot and starve subsequent inbound processing. With a
    cap of 1, a queued follower must stay blocked while the failing task holds
    the only slot, then run as soon as that task raises (proving the slot was
    freed by the exception path, not leaked)."""
    monkeypatch.setattr(settings, "channel_max_concurrent_processing", 1)

    first_gate = asyncio.Event()
    ran_after = asyncio.Event()

    async def boom():
        await first_gate.wait()
        raise ValueError("kaboom")

    async def succeed():
        ran_after.set()

    failing = spawn_channel_task(boom(), name="boomer")
    following = spawn_channel_task(succeed(), name="after")

    # Cap is 1 and the failing task holds the only slot, so the follower must
    # be queued on the semaphore (not yet run).
    await asyncio.sleep(0.02)
    assert not ran_after.is_set()

    # Let the holder raise; its slot must be released despite the exception.
    first_gate.set()
    await asyncio.wait_for(ran_after.wait(), timeout=1.0)

    await asyncio.gather(failing, following, return_exceptions=True)
    await asyncio.sleep(0)
    assert len(channel_base._channel_tasks) == 0


async def test_spawn_channel_task_closes_coro_on_pre_acquire_cancel(monkeypatch):
    """A task cancelled while parked on the semaphore (e.g. shutdown) must close
    its un-started coroutine, not leak a "coroutine was never awaited" warning.

    The whole point of the bound is that excess inbound work queues on the
    semaphore, so a shutdown can cancel many tasks before they ever `await`
    their coroutine; each must be closed cleanly."""
    monkeypatch.setattr(settings, "channel_max_concurrent_processing", 1)

    hold = asyncio.Event()
    started = False

    async def holder():
        await hold.wait()

    async def queued_worker():
        nonlocal started
        started = True

    # The holder takes the only slot and parks, so the queued task blocks on
    # sem.acquire() without ever awaiting its coroutine.
    blocker = spawn_channel_task(holder(), name="holder")
    await asyncio.sleep(0.05)

    queued_coro = queued_worker()
    queued = spawn_channel_task(queued_coro, name="queued")
    await asyncio.sleep(0.05)

    # Cancel while parked on the semaphore (pre-await).
    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    await asyncio.sleep(0)  # let the done-callback run

    # The queued coroutine never started AND was explicitly closed (so Python
    # emits no un-awaited-coroutine warning). Without the close() in the
    # done-callback this would be CORO_CREATED and warn at GC.
    assert started is False
    assert inspect.getcoroutinestate(queued_coro) == inspect.CORO_CLOSED
    assert queued not in channel_base._channel_tasks

    hold.set()
    await asyncio.gather(blocker, return_exceptions=True)
    await asyncio.sleep(0)
    assert len(channel_base._channel_tasks) == 0


_CHANNEL_MODULES = (
    "app.channels.teams",
    "app.channels.slack",
    "app.channels.whatsapp",
    "app.channels.telegram",
    "app.channels.discord",
    "app.channels.google_chat",
)


def _bare_create_task_process_targets(source: str) -> list[str]:
    """Names of ``_process*`` coroutines dispatched via a bare ``create_task``.

    AST-based (format-insensitive): catches a reformatted call such as::

        asyncio.create_task(
            _process_slack_message(inbound)
        )

    which a substring check on ``"asyncio.create_task(_process"`` would miss
    because of the newline/whitespace between ``(`` and ``_process``.
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        is_create_task = (
            isinstance(func, ast.Attribute) and func.attr == "create_task"
        ) or (isinstance(func, ast.Name) and func.id == "create_task")
        if not is_create_task:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Call):
            continue
        callee = first.func
        target = (
            callee.attr
            if isinstance(callee, ast.Attribute)
            else callee.id
            if isinstance(callee, ast.Name)
            else None
        )
        if target and target.startswith("_process"):
            offenders.append(target)
    return offenders


@pytest.mark.parametrize("module_path", _CHANNEL_MODULES)
def test_channels_use_bounded_spawn_not_bare_create_task(module_path):
    """Regression guard: every channel adapter must dispatch inbound message
    processing through the bounded ``spawn_channel_task`` helper, never a bare
    ``asyncio.create_task(_process_..._message(...))`` / ``_process_graph_...``
    that would re-open the unbounded pool-exhaustion vector."""
    import importlib

    module = importlib.import_module(module_path)
    source = inspect.getsource(module)

    assert "spawn_channel_task(" in source, (
        f"{module_path} must dispatch inbound processing via spawn_channel_task"
    )
    offenders = _bare_create_task_process_targets(source)
    assert not offenders, (
        f"{module_path} dispatches {offenders} via a bare asyncio.create_task; "
        "use the bounded spawn_channel_task instead (concurrency-cap bypass)"
    )


def test_bare_create_task_guard_is_format_insensitive():
    """The AST guard catches a bare ``create_task`` of a ``_process*`` function
    even when reformatted across lines (the old substring check did not), while
    leaving the bounded dispatch and non-message setup tasks untouched."""
    multiline_bypass = (
        "import asyncio\n"
        "def f(inbound):\n"
        "    asyncio.create_task(\n"
        "        _process_slack_message(inbound)\n"
        "    )\n"
    )
    assert _bare_create_task_process_targets(multiline_bypass) == [
        "_process_slack_message"
    ]
    # The old substring check would have missed exactly this reformatting.
    assert "asyncio.create_task(_process" not in multiline_bypass

    legitimate = (
        "import asyncio\n"
        "def f(inbound, x):\n"
        "    spawn_channel_task(_process_slack_message(inbound))\n"
        "    asyncio.create_task(_setup_private_channel_monitoring(x))\n"
    )
    assert _bare_create_task_process_targets(legitimate) == []

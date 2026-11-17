"""A run that went on in another process is seen whatever the two processes' clocks say (#10499).

The browser actions of one run alternate between backend processes on different hosts. Each shared copy of the
session is stamped with its writer's wall clock: compared across two clocks, a process on a host ahead of the other
took the other's newer copy for an older one, kept its session on the previous page, and the navigate made
elsewhere was lost (navigate B answered B, the next evaluate read the page of A). A process now remembers the
stamp of the copy it last wrote or started from: any other copy is newer, whatever its clock.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import browser_tool

KEY = "agent:assignment:run"
LAB = "http://10.42.3.17:3100"
STATE = {"cookies": [], "origins": []}
#: The clock of the host of process A runs 600 s ahead of the one of process B.
AHEAD = 2_000_000_600.0
BEHIND = 2_000_000_000.0


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)
        return 1


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    async def client():
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", client)
    return fake


@pytest.fixture
def process(monkeypatch):
    monkeypatch.setattr(browser_tool, "_sessions", {})
    monkeypatch.setattr(browser_tool, "_kept_states", {})
    monkeypatch.setattr(browser_tool, "_known_stamps", {})
    monkeypatch.setattr(browser_tool, "_kept_pages", {})
    monkeypatch.setattr(browser_tool, "_notices", {})
    monkeypatch.setattr(browser_tool, "_ensure_cleanup_running", AsyncMock())
    monkeypatch.setattr(browser_tool, "_PROCESS_ID", "process-a")
    relaunched = SimpleNamespace(name="relaunched")
    launch = AsyncMock(return_value=relaunched)
    monkeypatch.setattr(browser_tool, "_launch_session", launch)
    monkeypatch.setattr(browser_tool, "_close_session", AsyncMock())
    return SimpleNamespace(launch=launch, relaunched=relaunched)


def _open_session(url: str) -> browser_tool.BrowserSession:
    session = browser_tool.BrowserSession(
        pw=None,
        browser=SimpleNamespace(is_connected=lambda: True),
        context=SimpleNamespace(),
        page=SimpleNamespace(url=url, is_closed=MagicMock(return_value=False)),
        redirects=SimpleNamespace(main_frame_refusals=0, refusal_since=lambda mark: None),
    )
    browser_tool._sessions[KEY] = session
    return session


async def _kept_by(monkeypatch, process_id: str, kept_at: float, url: str) -> None:
    """*process_id* keeps and shares the session on *url* at *kept_at* by its own clock, as a navigate does."""
    monkeypatch.setattr(browser_tool, "_PROCESS_ID", process_id)
    await browser_tool._share_state(KEY, STATE, {"url": url}, kept_at=kept_at)
    monkeypatch.setattr(browser_tool, "_PROCESS_ID", "process-a")


async def test_a_copy_shared_by_a_process_whose_clock_is_behind_still_re_creates_the_session(
    redis, process, monkeypatch
):
    # Process A navigated to /html and kept it at its clock, ahead.
    _open_session(f"{LAB}/html")
    browser_tool._kept_states[KEY] = (STATE, AHEAD)
    await _kept_by(monkeypatch, "process-a", AHEAD, f"{LAB}/html")
    # The run went on in process B, which navigated to /css and kept it at its clock, behind.
    await _kept_by(monkeypatch, "process-b", BEHIND, f"{LAB}/css")

    session = await browser_tool._get_or_create_session(KEY)

    assert session is process.relaunched
    process.launch.assert_awaited_once_with(KEY)


async def test_a_session_whose_shared_copy_is_still_its_own_is_kept(redis, process, monkeypatch):
    open_session = _open_session(f"{LAB}/html")
    browser_tool._kept_states[KEY] = (STATE, AHEAD)
    await _kept_by(monkeypatch, "process-a", AHEAD, f"{LAB}/html")

    assert await browser_tool._get_or_create_session(KEY) is open_session
    process.launch.assert_not_awaited()


async def test_a_session_started_from_a_shared_copy_is_kept_until_another_one_is_written(
    redis, process, monkeypatch
):
    # Process B shared /css; process A started its session from that copy.
    await _kept_by(monkeypatch, "process-b", BEHIND, f"{LAB}/css")
    browser_tool._known_stamps[("process-a", KEY)] = ("process-b", BEHIND)
    open_session = _open_session(f"{LAB}/css")
    browser_tool._kept_states[KEY] = (STATE, AHEAD)

    assert await browser_tool._get_or_create_session(KEY) is open_session

    # Process B went on to /js, its clock still behind A's copy.
    await _kept_by(monkeypatch, "process-b", BEHIND + 1, f"{LAB}/js")

    assert await browser_tool._get_or_create_session(KEY) is process.relaunched


async def test_a_late_keep_never_shares_the_previous_page_over_a_copy_written_since(redis, process, monkeypatch):
    # Process A kept /html at its clock, ahead; process B then kept /css at its clock, behind.
    await _kept_by(monkeypatch, "process-a", AHEAD, f"{LAB}/html")
    await _kept_by(monkeypatch, "process-b", BEHIND, f"{LAB}/css")

    # A keep of process A stamped with its last action (an idle close, the settle keep after a navigate).
    await browser_tool._share_state(KEY, STATE, {"url": f"{LAB}/html"}, kept_at=AHEAD + 1, over_newer=False)

    entry = await browser_tool._shared_entry(KEY)
    assert entry is not None
    assert entry[1]["url"] == f"{LAB}/css"
    assert entry[2]["writer"] == "process-b"

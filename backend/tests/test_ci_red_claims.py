"""One diagnosis run per red head commit of a pull request, per agent (#11809).

A red listener keyed on ``owner/name@sha`` and the CI-completion dispatch of a
work item naming the same pull request used to start two diagnosis runs of
the same agent for one red: the branch lease only holds once the first run
takes it. ``services/ci_red_claims.py`` reads the live runs of the agent; the
second path stands down whichever arrives second.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import ci_red_claims
from app.services import work_item_pull_requests as wipr

REPO = "acme/widgets"
SHA = "a" * 40
OTHER_SHA = "b" * 40
NOW = datetime(2026, 11, 14, 13, 0, tzinfo=UTC)


def _event_ctx(sha: str = SHA, repo: str = "Acme/Widgets") -> dict:
    return {"type": "integration_event", "dedup_key": f"cfg:assignment:x:{repo.lower()}@{sha}"}


def _dispatch_ctx(item_id: str = "item-1", sha: str = SHA, conclusion: str = "failure") -> dict:
    return {
        "type": "work_item",
        "work_item_id": item_id,
        "reason": wipr.CI_COMPLETED_REASON,
        "conclusion": conclusion,
        "pull_request": {"repo": REPO, "number": 12, "head_sha": sha},
    }


# -- the pure matchers -----------------------------------------------------------------


def test_the_reason_is_the_one_the_dispatch_writes():
    assert ci_red_claims._CI_COMPLETED_REASON == wipr.CI_COMPLETED_REASON


def test_a_configured_key_naming_a_head_is_read_and_others_are_not():
    assert ci_red_claims.red_head_of_key(f"cfg:assignment:1:{REPO}@{SHA}") == (REPO, SHA)
    assert ci_red_claims.red_head_of_key(f"cfg:group:red:{REPO}@{SHA}:ci") == (REPO, SHA)
    # A mail key, a short sha or no repository names no head.
    assert ci_red_claims.red_head_of_key(f"mail:{REPO}@{SHA}") is None
    assert ci_red_claims.red_head_of_key(f"cfg:assignment:1:{REPO}@{SHA[:12]}") is None
    assert ci_red_claims.red_head_of_key(f"cfg:assignment:1:{SHA}") is None
    assert ci_red_claims.red_head_of_key(None) is None


def test_an_event_run_and_a_failure_dispatch_carry_the_red_of_their_head():
    assert ci_red_claims.carries_red_head(_event_ctx(), REPO, SHA)
    assert ci_red_claims.carries_red_head(_dispatch_ctx(), "ACME/widgets", SHA.upper())
    assert not ci_red_claims.carries_red_head(_event_ctx(OTHER_SHA), REPO, SHA)
    assert not ci_red_claims.carries_red_head(_dispatch_ctx(sha=OTHER_SHA), REPO, SHA)
    # A green dispatch diagnoses nothing; the item itself is not its own carrier.
    assert not ci_red_claims.carries_red_head(_dispatch_ctx(conclusion="success"), REPO, SHA)
    assert not ci_red_claims.carries_red_head(_dispatch_ctx("item-1"), REPO, SHA, exclude_item_id="item-1")
    # A run refused as a duplicate never ran.
    assert not ci_red_claims.carries_red_head({**_event_ctx(), "dedup_duplicate_of": "r"}, REPO, SHA)
    assert not ci_red_claims.carries_red_head(None, REPO, SHA)


# -- the live read ---------------------------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Db:
    def __init__(self, rows: list[tuple[object, object]]) -> None:
        self.rows = rows
        self.reads = 0

    async def execute(self, *_a, **_k):
        self.reads += 1
        return _Rows(self.rows)


async def test_the_oldest_live_carrier_is_answered():
    first, second = uuid.uuid4(), uuid.uuid4()
    db = _Db([(uuid.uuid4(), {"type": "manual"}), (first, _event_ctx()), (second, _dispatch_ctx())])
    assert await ci_red_claims.live_red_carrier(db, uuid.uuid4(), REPO, SHA) == first
    assert await ci_red_claims.live_red_carrier(db, uuid.uuid4(), REPO, SHA, exclude_run_id=first) == second
    assert await ci_red_claims.live_red_carrier(db, uuid.uuid4(), REPO, OTHER_SHA) is None


async def test_a_failed_read_answers_none():
    class _Boom:
        async def execute(self, *_a, **_k):
            raise RuntimeError("db down")

    assert await ci_red_claims.live_red_carrier(_Boom(), uuid.uuid4(), REPO, SHA) is None


async def test_nothing_is_read_without_an_agent_or_a_head():
    db = _Db([])
    assert await ci_red_claims.live_red_carrier(db, None, REPO, SHA) is None
    assert await ci_red_claims.live_red_carrier(db, uuid.uuid4(), REPO, "") is None
    assert db.reads == 0


# -- acceptance 1 and 3: the event path stands down for a live work-item diagnosis ------


async def test_an_event_run_on_a_head_a_work_item_run_diagnoses_is_a_duplicate():
    from app.services import assignment_engine

    carrier = uuid.uuid4()
    db = _Db([(carrier, _dispatch_ctx())])
    key = f"cfg:assignment:{uuid.uuid4()}:{REPO}@{SHA}"
    assert await assignment_engine._red_head_carried(db, uuid.uuid4(), key) == carrier
    # Another head, or a key naming no head: the event run starts.
    assert await assignment_engine._red_head_carried(db, uuid.uuid4(), key.replace(SHA, OTHER_SHA)) is None
    reads = db.reads
    assert await assignment_engine._red_head_carried(db, uuid.uuid4(), "cfg:assignment:1:mail-123") is None
    assert db.reads == reads


# -- acceptance 2 and 3: the work-item dispatch stands down for a live red diagnosis ----


class _RowResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _Session:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, *_a, **_k):
        return _RowResult(self._row)

    async def commit(self):
        pass


@pytest.fixture
def dispatch(monkeypatch):
    from app.core import database
    from app.services import assignment_engine
    from app.services import shared_state_pull_requests as leases
    from app.services import work_item as wi_service

    state: dict = {"triggered": [], "published": [], "carrier": None}
    agent = SimpleNamespace(id=uuid.uuid4())
    inbox = SimpleNamespace(name="Inbox", source=(SimpleNamespace(id=uuid.uuid4()), agent), user_id=None, writer=None)

    async def owner_inbox(_db, _item):
        return inbox

    async def no_lease(*_a, **_k):
        return None

    async def no_active(*_a, **_k):
        return False

    async def trigger(**kwargs):
        state["triggered"].append(kwargs["payload"]["work_item_id"])
        return "run-1"

    async def publish(item_id, **kwargs):
        state["published"].append((item_id, kwargs))

    async def record(*_a, **_k):
        return None

    async def serialized(work_item_id, **_k):
        return {"id": work_item_id, "correlation_key": None}

    async def carrier(db, agent_id, repo, head_sha, *, exclude_item_id=None, exclude_run_id=None):
        state["asked"] = (agent_id, repo, head_sha, exclude_item_id)
        return state["carrier"]

    monkeypatch.setattr(wipr, "_owner_inbox", owner_inbox)
    monkeypatch.setattr(leases, "live_pull_request_lease", no_lease)
    monkeypatch.setattr(ci_red_claims, "live_red_carrier", carrier)
    monkeypatch.setattr(assignment_engine, "has_active_run", no_active)
    monkeypatch.setattr(assignment_engine, "trigger_assignment", trigger)
    monkeypatch.setattr(wi_service, "publish_transition", publish)
    monkeypatch.setattr(wi_service, "record_claim_run", record)
    monkeypatch.setattr(wi_service, "get_work_item", serialized)
    monkeypatch.setattr(wi_service, "bounded_trigger_item", lambda item: item)
    state["install"] = lambda row: monkeypatch.setattr(database, "async_session", lambda: _Session(row))
    state["agent"] = agent
    return state


def _row(status: str = "open") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        owner_agent_id=None,
        status=status,
        payload={"pull_requests": [{"repo": REPO, "number": 12, "head_branch": "fix/12", "state": "open"}]},
        blocked_on=None,
        claim_notes=[],
        claimed_by_agent_id=None,
        claimed_by_run_id=None,
        claimed_at=None,
        last_activity_at=None,
    )


def _done(conclusion: str = "failure") -> SimpleNamespace:
    return SimpleNamespace(
        conclusion=conclusion, run_url="https://ci/1", run_name="ci", repo=REPO, number=12, head_sha=SHA
    )


async def test_a_red_dispatch_while_a_run_diagnoses_the_same_head_starts_no_run(dispatch):
    dispatch["carrier"] = holder = uuid.uuid4()
    row = _row("blocked")
    dispatch["install"](row)

    what = await wipr._dispatch(row.id, _done(), NOW)

    assert what.startswith(f"not dispatched: run {holder} already diagnoses the red of {REPO}#12")
    assert dispatch["triggered"] == []
    assert dispatch["asked"] == (dispatch["agent"].id, REPO, SHA, row.id)
    # Back in the queue, unclaimed: the inbox takes it once that run ended.
    assert row.status == "open" and row.claimed_by_agent_id is None
    assert dispatch["published"] == [(row.id, {"previous_status": "blocked"})]


async def test_a_red_dispatch_with_no_live_carrier_starts_one_run(dispatch):
    row = _row()
    dispatch["install"](row)
    assert await wipr._dispatch(row.id, _done(), NOW) == "dispatched to 'Inbox': run run-1"
    assert dispatch["triggered"] == [str(row.id)]


async def test_a_green_dispatch_never_asks_for_a_carrier(dispatch):
    dispatch["carrier"] = uuid.uuid4()
    row = _row()
    dispatch["install"](row)
    assert (await wipr._dispatch(row.id, _done("success"), NOW)).startswith("dispatched")
    assert "asked" not in dispatch


# -- acceptance 2: the inbox pass leaves a red item a live run diagnoses ----------------


async def test_the_inbox_pass_skips_a_red_item_a_live_run_diagnoses(monkeypatch):
    import app.services.autonomous as autonomous_mod

    asked: list = []

    async def carrier(db, agent_id, repo, head_sha, *, exclude_item_id=None, exclude_run_id=None):
        asked.append((repo, head_sha, exclude_item_id))
        return uuid.uuid4() if head_sha == SHA else None

    monkeypatch.setattr(ci_red_claims, "live_red_carrier", carrier)
    monkeypatch.setattr(autonomous_mod, "async_session", lambda: _Session(None))

    def _item(sha: str, conclusion: str = "failure") -> dict:
        binding = {
            "repo": REPO,
            "number": 12,
            "state": "open",
            "head_sha": sha,
            "ci": {"head_sha": sha, "runs": {"w": {"status": "completed", "conclusion": conclusion}}},
        }
        return {"id": "item-9", "payload": {"pull_requests": [binding]}}

    agent_id = uuid.uuid4()
    assert await autonomous_mod._red_diagnosed_by_a_live_run(_item(SHA), agent_id)
    assert asked == [(REPO, SHA, "item-9")]
    assert not await autonomous_mod._red_diagnosed_by_a_live_run(_item(OTHER_SHA), agent_id)
    asked.clear()
    # A green head an event run already works on is skipped too (the double dispatch).
    assert await autonomous_mod._red_diagnosed_by_a_live_run(_item(SHA, "success"), agent_id)
    assert asked == [(REPO, SHA, "item-9")]
    asked.clear()
    # No open pull request with a head: nothing is read.
    assert not await autonomous_mod._red_diagnosed_by_a_live_run({"id": "x", "payload": {}}, agent_id)
    merged = _item(SHA, "success")
    merged["payload"]["pull_requests"][0]["state"] = "merged"
    assert not await autonomous_mod._red_diagnosed_by_a_live_run(merged, agent_id)
    assert asked == []


# -- the double dispatch: a green event run owns its head ------------------------------


def _green_event_ctx(sha: str = SHA) -> dict[str, str]:
    return {"type": "integration_event", "dedup_key": f"cfg:group:green:{REPO}@{sha}"}


async def test_a_live_green_event_run_on_the_head_is_its_carrier() -> None:
    # Reproduces the double dispatch: a green-check run woken on the head holds the
    # per-head claim and merges; the inbox pass started a second run on that head.
    green = uuid.uuid4()
    db = _Db([(uuid.uuid4(), {"type": "manual"}), (green, _green_event_ctx())])
    assert await ci_red_claims.live_red_carrier(db, uuid.uuid4(), REPO, SHA) == green
    assert await ci_red_claims.live_red_carrier(db, uuid.uuid4(), REPO, OTHER_SHA) is None
    # A green CI-completion dispatch of another item is no carrier: no event run owns the head.
    db = _Db([(uuid.uuid4(), _dispatch_ctx(conclusion="success"))])
    assert await ci_red_claims.live_red_carrier(db, uuid.uuid4(), REPO, SHA) is None

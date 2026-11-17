"""An unsigned closing delivery releases a blocked item once read back.

An unsigned pull_request closed webhook once released no item: the provider
route accepted the delivery without a webhook secret, and
``_route_pull_request_event`` dropped every unsigned delivery, so only the
polling fallback (minutes later) released it. An unsigned closing is now a
hint: the reference is read back from the code host before any release. The
release note dates the merge (the code host's ``merged_at``), not its reading.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.integrations import reference_state
from app.integrations.providers.github.pr_events import parse_github_pull_request_event
from app.integrations.providers.github.reference_state import closing_event
from app.integrations.pull_request_events import PullRequestEvent
from app.services import work_item_blocked_refs as refs_mod

REPO = "acme/widgets"
NOW = datetime(2026, 11, 4, 1, 30, 39, tzinfo=UTC)


def _ref(number: int, **extra) -> dict:
    return {"repo": REPO, "kind": "pull_request", "number": number, "release_on": "merged_or_closed", **extra}


# -- the closing date --------------------------------------------------------------


def test_the_note_dates_the_merge_not_its_reading():
    event = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(1,), merged=True, closed_at="2026-11-04T01:25:43Z")
    assert refs_mod.closing_stamp(event, NOW) == "2026-11-04T01:25:43Z"


def test_no_or_unreadable_closing_date_falls_back_to_now():
    assert refs_mod.closing_stamp(PullRequestEvent(kind="closed", repo=REPO), NOW) == "2026-11-04T01:30:39Z"
    bad = PullRequestEvent(kind="closed", repo=REPO, closed_at="yesterday")
    assert refs_mod.closing_stamp(bad, NOW) == "2026-11-04T01:30:39Z"


def test_the_github_reader_and_parser_carry_the_closing_date():
    polled = closing_event(
        REPO,
        "pull_request",
        5,
        {"state": "closed", "merged": True, "merged_at": "2026-11-04T01:25:43Z", "closed_at": "2026-11-04T01:25:44Z"},
    )
    assert polled is not None and polled.closed_at == "2026-11-04T01:25:43Z"
    issue = closing_event(REPO, "issue", 6, {"state": "closed", "closed_at": "2026-11-04T01:00:00Z"})
    assert issue is not None and issue.closed_at == "2026-11-04T01:00:00Z"
    delivered = parse_github_pull_request_event(
        "pull_request.closed",
        {
            "repository": {"full_name": "Acme/Widgets"},
            "pull_request": {"number": 5, "merged": True, "merged_at": "2026-11-04T01:25:43Z", "head": {}},
        },
    )
    assert delivered is not None and delivered.closed_at == "2026-11-04T01:25:43Z"


# -- the read-back -----------------------------------------------------------------


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def harness(monkeypatch):
    state: dict = {"candidates": [], "tokens": {}, "answers": {}, "reads": [], "applied": []}

    async def candidates(db, event):
        return list(state["candidates"])

    async def token(db, provider, user_id):
        return state["tokens"].get(str(user_id))

    async def reader(repo, kind, number, tok):
        state["reads"].append((repo, kind, number, tok))
        answer = state["answers"].get((repo, kind, number))
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def apply(item_id, event, now):
        state["applied"].append((item_id, event))
        return "released"

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(refs_mod, "_hint_candidates", candidates)
    monkeypatch.setattr(refs_mod, "reference_token", token)
    monkeypatch.setattr(refs_mod, "_apply_to_item", apply)
    monkeypatch.setattr(reference_state, "_READERS", {"github": reader})
    monkeypatch.setattr(refs_mod, "_hint_reads", {})
    return state


def _hint(number: int) -> PullRequestEvent:
    # What an unsigned delivery claims: never applied as such.
    return PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(number,), merged=True)


async def test_a_confirmed_closing_releases_with_the_read_event_not_the_delivery(harness):
    user, item = uuid.uuid4(), uuid.uuid4()
    confirmed = PullRequestEvent(
        kind="closed", repo=REPO, pr_numbers=(8305,), merged=True, closed_at="2026-11-04T01:25:43Z"
    )
    harness["candidates"] = [(item, user, [_ref(8305), _ref(8273)])]
    harness["tokens"] = {str(user): "tok"}
    harness["answers"] = {(REPO, "pull_request", 8305): confirmed}

    out = await refs_mod.release_from_unverified_delivery(_hint(8305))

    assert out == {"read": 1, "released": 1, "noted": 0}
    assert harness["reads"] == [(REPO, "pull_request", 8305, "tok")], "only the reference the delivery names"
    assert harness["applied"] == [(item, confirmed)]


async def test_a_forged_closing_of_an_open_pull_request_releases_nothing(harness):
    user = uuid.uuid4()
    harness["candidates"] = [(uuid.uuid4(), user, [_ref(10)])]
    harness["tokens"] = {str(user): "tok"}
    # The code host says it is still open (None) or cannot be read.
    out = await refs_mod.release_from_unverified_delivery(_hint(10))
    assert out == {"read": 1, "released": 0, "noted": 0}
    assert harness["applied"] == []

    harness["answers"] = {(REPO, "pull_request", 11): RuntimeError("GitHub down")}
    harness["candidates"] = [(uuid.uuid4(), user, [_ref(11)])]
    assert (await refs_mod.release_from_unverified_delivery(_hint(11)))["released"] == 0


async def test_satisfied_or_unnamed_references_and_owners_without_credential_read_nothing(harness):
    user = uuid.uuid4()
    harness["candidates"] = [
        (uuid.uuid4(), user, [_ref(20, state="merged")]),
        (uuid.uuid4(), user, [_ref(21)]),
        (uuid.uuid4(), uuid.uuid4(), [_ref(22)]),
    ]
    harness["tokens"] = {str(user): "tok"}
    await refs_mod.release_from_unverified_delivery(_hint(20))
    await refs_mod.release_from_unverified_delivery(_hint(22))
    assert harness["reads"] == []


async def test_one_read_serves_every_item_and_a_duplicate_delivery_is_not_read_again(harness):
    user = uuid.uuid4()
    first, second = uuid.uuid4(), uuid.uuid4()
    confirmed = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(30,), merged=True)
    harness["candidates"] = [(first, user, [_ref(30)]), (second, user, [_ref(30)])]
    harness["tokens"] = {str(user): "tok"}
    harness["answers"] = {(REPO, "pull_request", 30): confirmed}

    out = await refs_mod.release_from_unverified_delivery(_hint(30))
    assert out["released"] == 2
    assert len(harness["reads"]) == 1

    await refs_mod.release_from_unverified_delivery(_hint(30))
    assert len(harness["reads"]) == 1, "a redelivery within the quiet window costs no read"


async def test_ci_and_head_events_are_never_read_back(harness):
    harness["candidates"] = [(uuid.uuid4(), uuid.uuid4(), [_ref(40)])]
    ci = PullRequestEvent(kind="ci", repo=REPO, pr_numbers=(40,))
    assert await refs_mod.release_from_unverified_delivery(ci) == {"read": 0, "released": 0, "noted": 0}


# -- the route ---------------------------------------------------------------------


async def test_the_route_reads_back_an_unsigned_closing_and_trusts_a_signed_one(monkeypatch):
    import app.integrations.providers.github  # noqa: F401 - registers the parser
    from app.api.v1 import webhooks
    from app.services import work_item_pull_requests

    calls: list[tuple[str, PullRequestEvent]] = []

    async def unverified(event):
        calls.append(("read_back", event))
        return {}

    async def trusted(event):
        calls.append(("trusted", event))
        return {}

    monkeypatch.setattr(refs_mod, "release_from_unverified_delivery", unverified)
    monkeypatch.setattr(work_item_pull_requests, "handle_pull_request_event", trusted)
    from app.services import shared_state_pull_requests

    async def no_state(event):
        return 0

    monkeypatch.setattr(shared_state_pull_requests, "resolve_from_unverified_delivery", no_state)
    monkeypatch.setattr(shared_state_pull_requests, "resolve_states_of_closed_pull_request", no_state)
    payload = {
        "repository": {"full_name": REPO},
        "pull_request": {"number": 7, "merged": True, "head": {"ref": "b", "sha": "s"}},
    }

    monkeypatch.setattr(webhooks, "_signed_with_webhook_secret", lambda enc: False)
    await webhooks._route_pull_request_event("github", "pull_request.closed", payload, None)
    await webhooks._route_pull_request_event("github", "pull_request.synchronize", payload, None)
    assert [c[0] for c in calls] == ["read_back"], "an unsigned head change is still ignored"

    monkeypatch.setattr(webhooks, "_signed_with_webhook_secret", lambda enc: True)
    await webhooks._route_pull_request_event("github", "pull_request.closed", payload, "enc")
    assert [c[0] for c in calls] == ["read_back", "trusted"]

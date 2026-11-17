"""The agent-account Redis state: a message is named by its conversation and its id."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.services.agent_accounts import pipeline, state
from app.services.agent_accounts.providers import ProviderCapabilities
from app.services.agent_accounts.rules import normalize_rules


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.calls: list = []

    def __getattr__(self, name):
        def _queue(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self

        return _queue

    async def execute(self):
        return [await getattr(self.redis, name)(*args, **kwargs) for name, args, kwargs in self.calls]


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sorted: dict[str, dict[str, float]] = {}

    async def set(self, key, value, *, nx=False, xx=False, ex=None, keepttl=False):
        if (nx and key in self.values) or (xx and key not in self.values):
            return None
        self.values[key] = value
        return True

    async def get(self, key):
        value = self.values.get(key)
        return value.encode() if isinstance(value, str) else value

    async def expire(self, key, seconds):
        return int(key in self.values)

    async def exists(self, key):
        return int(key in self.values)

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)

    async def zadd(self, key, mapping):
        self.sorted.setdefault(key, {}).update(mapping)

    async def zrangebyscore(self, key, low, high, start=0, num=None):
        members = sorted((score, member) for member, score in self.sorted.get(key, {}).items() if low <= score <= high)
        return [member.encode() for _, member in members][start : None if num is None else start + num]

    async def zrem(self, key, member):
        member = member.decode() if isinstance(member, bytes) else member
        return int(self.sorted.get(key, {}).pop(member, None) is not None)

    async def zremrangebyscore(self, key, low, high):
        doomed = [member for member, score in self.sorted.get(key, {}).items() if low <= score <= high]
        for member in doomed:
            del self.sorted[key][member]
        return len(doomed)

    async def zcard(self, key):
        return len(self.sorted.get(key, {}))

    async def zcount(self, key, low, high):
        high = float("inf") if high == "+inf" else high
        return sum(1 for score in self.sorted.get(key, {}).values() if low <= score <= high)

    def pipeline(self):
        return FakePipeline(self)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    async def _redis():
        return fake

    monkeypatch.setattr(state, "_redis", _redis)
    return fake


async def test_the_same_message_id_in_two_conversations_is_two_messages(redis):
    # Teams chat message ids are millisecond timestamps: two chats can share one.
    assert await state.first_delivery("acct", "chat-a", "1700000000000")
    assert await state.first_delivery("acct", "chat-b", "1700000000000")
    assert not await state.first_delivery("acct", "chat-a", "1700000000000")


async def test_a_post_is_recognised_in_its_own_conversation_only(redis):
    await state.mark_sent("acct", "chat-a", "1700000000000")
    assert await state.was_sent_by_agent("acct", "chat-a", "1700000000000")
    assert not await state.was_sent_by_agent("acct", "chat-b", "1700000000000")
    assert not await state.was_sent_by_agent("other", "chat-a", "1700000000000")


async def test_the_send_window_opens_and_closes(redis):
    assert not await state.is_sending("acct", "chat-a")
    await state.mark_sending("acct", "chat-a", True)
    assert await state.is_sending("acct", "chat-a") and not await state.is_sending("acct", "chat-b")
    await state.mark_sending("acct", "chat-a", False)
    assert not await state.is_sending("acct", "chat-a")


# -- what one account spends per hour, every conversation together ---------------


async def test_an_account_takes_its_hourly_turns_and_no_more(redis):
    assert [await state.take_turn("acct", 3) for _ in range(5)] == [True, True, True, False, False]
    # A refused turn is not counted: the limit is what ran.
    assert await state.account_turns("acct") == 3
    assert len(redis.sorted[state._key("turns", "acct")]) == 3
    # Another account has its own hour.
    assert await state.take_turn("other", 3) is True
    assert await state.account_turns("other") == 1


async def test_the_turns_of_the_previous_hour_no_longer_count(redis):
    assert await state.take_turn("acct", 1) is True
    assert await state.take_turn("acct", 1) is False
    key = state._key("turns", "acct")
    for member in redis.sorted[key]:
        redis.sorted[key][member] = time.time() - state.REPLY_WINDOW_SECONDS - 1
    assert await state.account_turns("acct") == 0
    assert await state.take_turn("acct", 1) is True


async def test_no_limit_takes_no_count(redis):
    assert all([await state.take_turn("acct", 0) for _ in range(3)])
    assert await state.account_turns("acct") == 0
    assert state._key("turns", "acct") not in redis.sorted


async def test_without_redis_a_limited_account_runs_no_turn_and_an_unlimited_one_runs(monkeypatch):
    """The sender sets how many turns run: the limit fails closed, the read alone fails open."""

    async def _none():
        return None

    monkeypatch.setattr(state, "_redis", _none)
    assert await state.take_turn("acct", 60) is False
    assert await state.take_turn("acct", 0) is True
    assert await state.account_turns("acct") == 0


async def test_a_redis_failure_runs_no_turn(redis, monkeypatch):
    async def _broken(*args, **kwargs):
        raise ConnectionError("gone")

    monkeypatch.setattr(redis, "zadd", _broken)
    assert await state.take_turn("acct", 60) is False


# -- what tells a refusal from a platform the provider could not reach ------------


async def test_the_handshake_says_whether_the_provider_called_back_since_a_moment(redis):
    before = time.time()
    assert await state.handshake_since("microsoft_teams", before) is False
    await state.note_handshake("microsoft_teams")
    assert await state.handshake_since("microsoft_teams", before) is True
    assert await state.handshake_since("microsoft_teams", time.time() + 60) is False
    assert await state.handshake_since("another_provider", before) is False


async def test_without_redis_nobody_knows_whether_the_provider_called_back(monkeypatch):
    async def _none():
        return None

    monkeypatch.setattr(state, "_redis", _none)
    await state.note_handshake("microsoft_teams")
    assert await state.handshake_since("microsoft_teams", 0) is None


# -- a channel thread the account answered in, read for its follow-ups -----------

ROUTE = {"kind": "channel", "team_id": "t", "channel_id": "c", "root_id": "root"}


def _due_now(redis: FakeRedis) -> None:
    for member in redis.sorted.get(state.FOLLOWED_THREADS_KEY, {}):
        redis.sorted[state.FOLLOWED_THREADS_KEY][member] = 0


async def test_a_followed_thread_is_read_from_its_answer_on_and_its_cursor_never_goes_back(redis):
    await state.follow_thread("acct", "c", "c:root", ROUTE)
    assert await state.due_followed_threads(10) == []  # due in a minute, not now
    _due_now(redis)
    ((account_id, conversation_id, thread_id, route, since),) = await state.due_followed_threads(10)
    assert (account_id, conversation_id, thread_id, route) == ("acct", "c", "c:root", ROUTE)
    assert since == pytest.approx(time.time(), abs=5)
    # Claimed: a second worker gets nothing.
    assert await state.due_followed_threads(10) == []

    await state.followed_thread_read("acct", "c", "c:root", ROUTE, since + 30)
    # A second answer in the thread extends the following, never skips what was not read.
    await state.follow_thread("acct", "c", "c:root", ROUTE)
    _due_now(redis)
    ((*_, cursor),) = await state.due_followed_threads(10)
    assert cursor == since + 30


async def test_a_thread_whose_following_ended_is_dropped(redis):
    await state.follow_thread("acct", "c", "c:root", ROUTE)
    _due_now(redis)
    redis.values.clear()  # the cursor expired with the thread's activity
    assert await state.due_followed_threads(10) == []
    await state.followed_thread_read("acct", "c", "c:root", ROUTE, time.time())
    assert redis.sorted[state.FOLLOWED_THREADS_KEY] == {}


async def test_an_answer_in_a_channel_heard_for_mentions_only_follows_its_thread(monkeypatch):
    followed: list[tuple] = []

    async def _follow(*args):
        followed.append(args)

    monkeypatch.setattr(state, "follow_thread", _follow)
    provider = SimpleNamespace(capabilities=ProviderCapabilities(threads=True))
    mentions = normalize_rules([{"conversation_types": ["channel"], "trigger": "mention"}])
    account = SimpleNamespace(id="acct", agent_id="agent", enabled=True, rules=mentions)

    def _reply(conversation_type: str):
        return SimpleNamespace(conversation_type=conversation_type, conversation_id="c", thread_id="c:root", route=ROUTE)

    await pipeline._follow_thread(provider, account, _reply("channel"))
    assert followed == [("acct", "c", "c:root", ROUTE)]

    followed.clear()
    # A chat is heard in full; a channel a rule answers in full too; a rule that takes no follow-up
    # follows nothing; a provider that cannot read a thread neither.
    await pipeline._follow_thread(provider, account, _reply("group"))
    everything = SimpleNamespace(**{**vars(account), "rules": normalize_rules([{"conversation_types": ["channel"], "trigger": "any"}])})
    await pipeline._follow_thread(provider, everything, _reply("channel"))
    no_threads = SimpleNamespace(
        **{**vars(account), "rules": normalize_rules([{"conversation_types": ["channel"], "continue_threads": False}])}
    )
    await pipeline._follow_thread(provider, no_threads, _reply("channel"))
    await pipeline._follow_thread(SimpleNamespace(capabilities=ProviderCapabilities()), account, _reply("channel"))
    assert followed == []

"""``REDIS_KEY_PREFIX``: every key and channel under the prefix, the code naming them without it.

Unit tests: no Redis. The client classes are exercised through redis-py's
own ``execute_command`` / ``parse_response`` replaced by recorders; the
round trip against a real Redis restricted by an ACL is
``tests/test_redis_key_prefix_redis.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from redis.asyncio import ConnectionPool
from redis.asyncio.client import Pipeline, PubSub, Redis

from app.core import queue as queue_module
from app.core.config import Settings
from app.core.redis_key_prefix import (
    KeyPrefix,
    UnprefixedCommandError,
    prefixed_client_class,
    prefixed_pattern_pubsub_class,
)

P = KeyPrefix("dev-xtmone1:")


# ---------------------------------------------------------------------------
# Setting
# ---------------------------------------------------------------------------


def test_the_prefix_is_empty_by_default():
    assert Settings().redis_key_prefix == ""  # type: ignore[call-arg]


@pytest.mark.parametrize("prefix", ["a*", "a?", "a[b]", "a\\b", "a b", "a\tb"])
def test_a_prefix_with_a_glob_character_or_a_space_is_refused(prefix):
    with pytest.raises(ValueError, match="REDIS_KEY_PREFIX"):
        Settings(redis_key_prefix=prefix)  # type: ignore[call-arg]


def test_an_empty_key_prefix_object_is_refused():
    with pytest.raises(ValueError):
        KeyPrefix("")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("SET", "a", "v", "EX", 5), ("SET", "dev-xtmone1:a", "v", "EX", 5)),
        (("GET", b"a"), ("GET", b"dev-xtmone1:a")),
        (("DEL", "a", "b"), ("DEL", "dev-xtmone1:a", "dev-xtmone1:b")),
        (("MSET", "a", "1", "b", "2"), ("MSET", "dev-xtmone1:a", "1", "dev-xtmone1:b", "2")),
        (("BLPOP", "a", "b", 5), ("BLPOP", "dev-xtmone1:a", "dev-xtmone1:b", 5)),
        (("LMOVE", "a", "b", "LEFT", "RIGHT"), ("LMOVE", "dev-xtmone1:a", "dev-xtmone1:b", "LEFT", "RIGHT")),
        (("BITOP", "AND", "d", "a"), ("BITOP", "AND", "dev-xtmone1:d", "dev-xtmone1:a")),
        (("HSET", "h", "f", "v"), ("HSET", "dev-xtmone1:h", "f", "v")),
        (("HSCAN", "h", 0, "MATCH", "f*"), ("HSCAN", "dev-xtmone1:h", 0, "MATCH", "f*")),
        (("EVAL", "s", 2, "a", "b", "arg"), ("EVAL", "s", 2, "dev-xtmone1:a", "dev-xtmone1:b", "arg")),
        (("EVALSHA", "sha", "0", "arg"), ("EVALSHA", "sha", "0", "arg")),
        (
            ("ZUNIONSTORE", "d", 2, "a", "b", "WEIGHTS", 1, 2),
            ("ZUNIONSTORE", "dev-xtmone1:d", 2, "dev-xtmone1:a", "dev-xtmone1:b", "WEIGHTS", 1, 2),
        ),
        (("ZINTERCARD", 2, "a", "b", "LIMIT", 3), ("ZINTERCARD", 2, "dev-xtmone1:a", "dev-xtmone1:b", "LIMIT", 3)),
        (("BLMPOP", 1, 1, "a", "LEFT"), ("BLMPOP", 1, 1, "dev-xtmone1:a", "LEFT")),
        (
            ("XREADGROUP", "GROUP", "g", "c", "COUNT", 1, "STREAMS", "s", "t", ">", ">"),
            ("XREADGROUP", "GROUP", "g", "c", "COUNT", 1, "STREAMS", "dev-xtmone1:s", "dev-xtmone1:t", ">", ">"),
        ),
        (
            ("SORT", "l", "BY", "w_*", "GET", "#", "GET", "o_*", "STORE", "d"),
            ("SORT", "dev-xtmone1:l", "BY", "dev-xtmone1:w_*", "GET", "#", "GET", "dev-xtmone1:o_*", "STORE", "dev-xtmone1:d"),
        ),
        # A subcommand spelled in the name, as redis-py sends some; or apart.
        (("MEMORY USAGE", "a"), ("MEMORY USAGE", "dev-xtmone1:a")),
        (("OBJECT", "ENCODING", "a"), ("OBJECT", "ENCODING", "dev-xtmone1:a")),
        (("XINFO STREAM", "s"), ("XINFO STREAM", "dev-xtmone1:s")),
        # Patterns and channels.
        (("KEYS", "quota:*"), ("KEYS", "dev-xtmone1:quota:*")),
        (("SCAN", 0, "MATCH", "a:*", "COUNT", 10), ("SCAN", 0, "MATCH", "dev-xtmone1:a:*", "COUNT", 10)),
        (("SCAN", 0, "COUNT", 10), ("SCAN", 0, "COUNT", 10, "MATCH", "dev-xtmone1:*")),
        (("PUBLISH", "activity:u", "payload"), ("PUBLISH", "dev-xtmone1:activity:u", "payload")),
        (("PUBSUB CHANNELS",), ("PUBSUB CHANNELS", "dev-xtmone1:*")),
        (("PUBSUB", "NUMSUB", "a", "b"), ("PUBSUB", "NUMSUB", "dev-xtmone1:a", "dev-xtmone1:b")),
        # No key.
        (("PING",), ("PING",)),
        (("INFO", "server"), ("INFO", "server")),
        (("SCRIPT LOAD", "return 1"), ("SCRIPT LOAD", "return 1")),
        (("CLIENT SETNAME", "x"), ("CLIENT SETNAME", "x")),
    ],
    ids=lambda value: " ".join(map(str, value)) if isinstance(value, tuple) else str(value),
)
def test_a_command_has_its_keys_and_channels_under_the_prefix(args, expected):
    assert P.command(args) == expected


@pytest.mark.parametrize("args", [("FLUSHDB",), ("FLUSHALL", "ASYNC"), ("RANDOMKEY",), ("SWAPDB", 0, 1)])
def test_a_command_on_the_whole_database_is_refused(args):
    with pytest.raises(UnprefixedCommandError):
        P.command(args)


def test_an_unknown_command_is_refused_rather_than_sent_unprefixed():
    with pytest.raises(UnprefixedCommandError, match=r"FT\.SEARCH"):
        P.command(("FT.SEARCH", "idx", "*"))


# ---------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "reply", "expected"),
    [
        (("SCAN", 0), (7, ["dev-xtmone1:a", "dev-xtmone1:b"]), (7, ["a", "b"])),
        (("KEYS", "x"), [b"dev-xtmone1:a"], [b"a"]),
        (("BLPOP", "x", 0), ["dev-xtmone1:l", "v"], ["l", "v"]),
        (("BZPOPMIN", "x", 0), ("dev-xtmone1:z", "m", 1.0), ("z", "m", 1.0)),
        (("BLPOP", "x", 0), None, None),
        (("LMPOP", 1, "x", "LEFT"), ["dev-xtmone1:l", ["v"]], ["l", ["v"]]),
        (("XREAD", "STREAMS", "s", "0"), [["dev-xtmone1:s", []]], [["s", []]]),
        (("XREAD", "STREAMS", "s", "0"), {"dev-xtmone1:s": []}, {"s": []}),
        (("PUBSUB CHANNELS",), ["dev-xtmone1:c"], ["c"]),
        (("PUBSUB NUMSUB", "c"), [("dev-xtmone1:c", 2)], [("c", 2)]),
        (("GET", "x"), "dev-xtmone1:not-a-key-name", "dev-xtmone1:not-a-key-name"),
    ],
    ids=lambda value: str(value),
)
def test_a_reply_names_keys_and_channels_without_the_prefix(args, reply, expected):
    assert P.reply(args, reply) == expected


# ---------------------------------------------------------------------------
# Client classes
# ---------------------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch) -> list[tuple[Any, ...]]:
    """The commands redis-py's ``Redis.execute_command`` is given; replies by command."""
    commands: list[tuple[Any, ...]] = []
    replies = {"SCAN": (0, ["dev-xtmone1:a"]), "GET": "v"}

    async def _execute_command(self, *args, **options):
        commands.append(args)
        return replies.get(args[0])

    monkeypatch.setattr(Redis, "execute_command", _execute_command)
    return commands


def _client() -> Redis:
    # A pool connects on the first command only: these tests send none.
    cls = prefixed_client_class("dev-xtmone1:")
    return cls(connection_pool=ConnectionPool.from_url("redis://localhost:1", decode_responses=True))


async def test_the_client_prefixes_the_command_and_unprefixes_the_reply(sent):
    client = _client()

    assert await client.get("a") == "v"
    assert await client.scan(0, match="a*") == (0, ["a"])
    assert [key async for key in client.scan_iter()] == ["a"]

    # redis-py spells some option names as bytes.
    as_text = [tuple(a.decode() if isinstance(a, bytes) else a for a in args) for args in sent]
    assert as_text[0] == ("GET", "dev-xtmone1:a")
    assert as_text[1][:4] == ("SCAN", 0, "MATCH", "dev-xtmone1:a*")
    assert as_text[2] == ("SCAN", "0", "MATCH", "dev-xtmone1:*")


async def test_a_lua_script_receives_its_keys_under_the_prefix(sent):
    client = _client()

    await client.eval("return 1", 1, "a", "arg")
    script = client.register_script("return 1")
    await script(keys=["b"], args=["x"])

    assert sent[0] == ("EVAL", "return 1", 1, "dev-xtmone1:a", "arg")
    assert sent[1][0] == "EVALSHA"
    assert sent[1][2:] == (1, "dev-xtmone1:b", "x")


async def test_a_pipeline_queues_prefixed_commands_and_unprefixes_its_replies(monkeypatch):
    client = _client()
    pipe = client.pipeline(transaction=True)
    assert isinstance(pipe, Pipeline)

    pipe.set("a", "1").incr("b").scan(0)
    assert [args for args, _ in pipe.command_stack] == [
        ("SET", "dev-xtmone1:a", "1"),
        ("INCRBY", "dev-xtmone1:b", 1),
        ("SCAN", 0, "MATCH", "dev-xtmone1:*"),
    ]

    async def _execute(self, raise_on_error=True):
        return [True, 1, (0, ["dev-xtmone1:a"])]

    monkeypatch.setattr(Pipeline, "execute", _execute)
    assert await pipe.execute() == [True, 1, (0, ["a"])]


async def test_a_watched_pipeline_sends_its_immediate_commands_prefixed(monkeypatch):
    seen: list[tuple[Any, ...]] = []

    async def _immediate(self, *args, **options):
        seen.append(args)
        return "1"

    monkeypatch.setattr(Pipeline, "immediate_execute_command", _immediate)
    pipe = _client().pipeline()

    await pipe.watch("a")
    pipe.watching = True
    assert await pipe.get("a") == "1"

    assert seen == [("WATCH", "dev-xtmone1:a"), ("GET", "dev-xtmone1:a")]


def test_the_client_built_from_a_url_or_a_pool_is_the_prefixed_one():
    cls = prefixed_client_class("dev-xtmone1:")
    assert prefixed_client_class("dev-xtmone1:") is cls
    assert isinstance(cls.from_url("redis://localhost:1"), cls)
    assert isinstance(_client().client(), cls)


# ---------------------------------------------------------------------------
# Pub/sub
# ---------------------------------------------------------------------------


@pytest.fixture
def pubsub_wire(monkeypatch) -> dict[str, list]:
    """What ``PubSub`` sends, and the responses it reads (queued by the test)."""
    wire: dict[str, list] = {"sent": [], "responses": []}

    async def _execute_command(self, *args):
        wire["sent"].append(args)

    async def _parse_response(self, block=True, timeout=0):
        return wire["responses"].pop(0) if wire["responses"] else None

    monkeypatch.setattr(PubSub, "execute_command", _execute_command)
    monkeypatch.setattr(PubSub, "parse_response", _parse_response)
    return wire


def _pubsub() -> PubSub:
    return _client().pubsub()


async def test_a_subscription_names_its_channels_without_the_prefix(pubsub_wire):
    pubsub = _pubsub()

    await pubsub.subscribe("activity:u")
    pubsub_wire["responses"] += [
        ["subscribe", "dev-xtmone1:activity:u", 1],
        ["message", "dev-xtmone1:activity:u", "payload"],
    ]

    assert pubsub_wire["sent"] == [("SUBSCRIBE", "dev-xtmone1:activity:u")]
    assert (await pubsub.get_message())["channel"] == "activity:u"
    message = await pubsub.get_message()
    assert (message["channel"], message["data"]) == ("activity:u", "payload")


async def test_a_pattern_subscription_is_one_subscription_to_the_prefix_filtered_here(pubsub_wire):
    pubsub = _pubsub()

    await pubsub.psubscribe("relay:w:*")
    await pubsub.psubscribe("other:[ab]?")
    pubsub_wire["responses"] += [
        ["pmessage", "dev-xtmone1:*", "dev-xtmone1:chat:1", "not ours"],
        ["pmessage", "dev-xtmone1:*", "dev-xtmone1:relay:w:r:1", "1"],
        ["pmessage", "dev-xtmone1:*", "dev-xtmone1:other:bx", "2"],
    ]

    # Redis allows a PSUBSCRIBE pattern only when the ACL names it: the
    # prefix's own is the one sent, once.
    assert pubsub_wire["sent"] == [("PSUBSCRIBE", "dev-xtmone1:*")]
    assert await pubsub.get_message() is None
    message = await pubsub.get_message()
    assert (message["type"], message["pattern"], message["channel"]) == ("pmessage", "relay:w:*", "relay:w:r:1")
    message = await pubsub.get_message()
    assert (message["pattern"], message["channel"]) == ("other:[ab]?", "other:bx")


async def test_a_pattern_subscription_ends_when_its_last_pattern_does(pubsub_wire):
    pubsub = _pubsub()
    await pubsub.psubscribe("a:*", "b:*")

    await pubsub.punsubscribe("a:*")
    assert pubsub_wire["sent"] == [("PSUBSCRIBE", "dev-xtmone1:*")]
    assert set(pubsub.patterns) == {"b:*"}

    await pubsub.punsubscribe()
    assert pubsub_wire["sent"][-1] == ("PUNSUBSCRIBE", "dev-xtmone1:*")
    assert not pubsub.subscribed


async def test_a_reconnected_subscription_subscribes_again_under_the_prefix(pubsub_wire):
    pubsub = _pubsub()
    await pubsub.subscribe("c")
    await pubsub.psubscribe("p:*")
    pubsub_wire["sent"].clear()

    await pubsub.on_connect(MagicMock())

    assert pubsub_wire["sent"] == [("SUBSCRIBE", "dev-xtmone1:c"), ("PSUBSCRIBE", "dev-xtmone1:*")]


async def test_the_saq_pattern_subscription_keeps_its_prefixed_names(pubsub_wire):
    pool = ConnectionPool.from_url("redis://localhost:1", decode_responses=False)
    pubsub = prefixed_pattern_pubsub_class("dev-xtmone1:")(pool)

    await pubsub.psubscribe("dev-xtmone1:saq:job:default*")
    pubsub_wire["responses"] += [
        [b"pmessage", b"dev-xtmone1:*", b"dev-xtmone1:activity:u", b"x"],
        [b"pmessage", b"dev-xtmone1:*", b"dev-xtmone1:saq:job:default:k", b"complete"],
    ]

    assert pubsub_wire["sent"] == [("PSUBSCRIBE", "dev-xtmone1:*")]
    assert await pubsub.get_message() is None
    message = await pubsub.get_message()
    assert (message["pattern"], message["channel"]) == (
        b"dev-xtmone1:saq:job:default*",
        b"dev-xtmone1:saq:job:default:k",
    )


# ---------------------------------------------------------------------------
# SAQ queue
# ---------------------------------------------------------------------------


@pytest.fixture
def saq_abort_prefix(monkeypatch):
    from saq import job as saq_job

    # Restored after the test, whatever the queue sets.
    monkeypatch.setattr(saq_job, "ABORT_ID_PREFIX", saq_job.ABORT_ID_PREFIX)
    return saq_job


def test_the_saq_queue_keeps_its_names_without_a_prefix(saq_abort_prefix):
    from saq import Job

    queue = queue_module._teardown_safe_queue_cls()(MagicMock())

    assert queue.namespace("queued") == "saq:default:queued"
    assert queue.job_id("k") == "saq:job:default:k"
    assert queue.job_key_from_id("saq:job:default:k") == "k"
    assert queue._pubsub.prefix == "saq:job:default"
    assert Job(function="f", key="k").abort_id == "saq:abort:k"


def test_the_saq_queue_names_its_keys_and_channels_under_the_prefix(saq_abort_prefix):
    from saq import Job

    queue = queue_module._teardown_safe_queue_cls()(MagicMock(), key_prefix="dev-xtmone1:")

    assert queue._queued == "dev-xtmone1:saq:default:queued"
    assert queue.namespace("stats") == "dev-xtmone1:saq:default:stats"
    assert queue.job_id("k") == "dev-xtmone1:saq:job:default:k"
    assert queue.job_key_from_id("dev-xtmone1:saq:job:default:k") == "k"
    assert Job(function="f", key="k", queue=queue).id == "dev-xtmone1:saq:job:default:k"
    # SAQ reads this module constant on every call: an upgrade that stops
    # doing so leaves the abort key outside the prefix, and fails here.
    assert Job(function="f", key="k").abort_id == "dev-xtmone1:saq:abort:k"
    assert queue._pubsub.prefix == "dev-xtmone1:saq:job:default"
    assert isinstance(queue._pubsub.pubsub, prefixed_pattern_pubsub_class("dev-xtmone1:"))


def test_get_queue_passes_the_prefix(monkeypatch, saq_abort_prefix):
    import redis.asyncio as redis_asyncio

    monkeypatch.setattr(queue_module, "_queue", None)
    monkeypatch.setattr(redis_asyncio, "from_url", lambda url, **kwargs: MagicMock())
    monkeypatch.setattr(queue_module.settings, "redis_mode", "standalone")
    monkeypatch.setattr(queue_module.settings, "redis_key_prefix", "dev-xtmone1:")

    queue = queue_module.get_queue()

    assert queue.namespace("queued") == "dev-xtmone1:saq:default:queued"


# ---------------------------------------------------------------------------
# Names Redis hands back as values
# ---------------------------------------------------------------------------


def test_unprefixed_key_takes_the_prefix_off_a_name_a_script_stored(monkeypatch):
    from app.core import redis as core_redis

    monkeypatch.setattr(core_redis.settings, "redis_key_prefix", "dev-xtmone1:")
    assert core_redis.unprefixed_key("dev-xtmone1:quota:usage:u") == "quota:usage:u"
    monkeypatch.setattr(core_redis.settings, "redis_key_prefix", "")
    assert core_redis.unprefixed_key("quota:usage:u") == "quota:usage:u"

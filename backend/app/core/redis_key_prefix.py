# SPDX-License-Identifier: Apache-2.0

"""The Redis key prefix (``REDIS_KEY_PREFIX``): one Redis shared by several platforms.

With a prefix, every key the platform reads or writes and every pub/sub channel
it uses is named ``<prefix><name>`` in Redis, while the code keeps using
``<name>``: the client classes built by :func:`prefixed_client_class` add the
prefix to the arguments of each command and take it off the names Redis
replies with. A Redis user whose ACL allows ``~<prefix>* &<prefix>*`` is then
enough. The SAQ queue names its own keys (``app.core.queue``).

Where the keys of a command are comes from the key positions Redis reports
(``COMMAND``, Redis 8), copied below; the commands whose keys move (a key count
argument, a keyword) have their own rule. A command the tables do not know is
refused rather than sent unprefixed, and so are the commands that act on the
whole database (``FLUSHDB``, ``RANDOMKEY``...).

What changes besides the names:

- ``SCAN`` without ``MATCH`` scans the prefix only; ``KEYS`` and the ``MATCH``
  of ``SCAN`` and ``PUBSUB CHANNELS`` are patterns within the prefix.
- A Lua script sees the prefixed names in ``KEYS``: a script that stores one
  of them as a value stores the prefixed name (:func:`app.core.redis.unprefixed_key`).
- Redis allows a ``PSUBSCRIBE`` pattern only when the ACL names that very
  pattern, so a pattern subscription subscribes to ``<prefix>*`` and keeps the
  messages of its own patterns: it reads every message published under the
  prefix.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterable
from typing import Any, ClassVar

from redis.asyncio.client import Pipeline, PubSub, Redis


class UnprefixedCommandError(ValueError):
    """A command the key prefix cannot apply to: refused rather than sent."""


# Key positions as Redis 8 reports them in ``COMMAND`` (first key, last key,
# step; a negative last key counts from the end). A subcommand is
# ``COMMAND|SUBCOMMAND``, its positions counted from the command name.
_KEY_RANGES: dict[tuple[int, int, int], str] = {
    (1, 1, 1): """
        APPEND ARCOUNT ARDEL ARDELRANGE ARGET ARGETRANGE ARGREP ARINFO ARINSERT ARLASTITEMS ARLEN
        ARMGET ARMSET ARNEXT AROP ARRING ARSCAN ARSEEK ARSET BITCOUNT BITFIELD BITFIELD_RO BITPOS
        DECR DECRBY DELEX DIGEST DUMP EXPIRE EXPIREAT EXPIRETIME GEOADD GEODIST GEOHASH GEOPOS
        GEORADIUSBYMEMBER_RO GEORADIUS_RO GEOSEARCH GET GETBIT GETDEL GETEX GETRANGE GETSET HDEL
        HEXISTS HEXPIRE HEXPIREAT HEXPIRETIME HGET HGETALL HGETDEL HGETEX HINCRBY HINCRBYFLOAT HKEYS
        HLEN HMGET HMSET HPERSIST HPEXPIRE HPEXPIREAT HPEXPIRETIME HPTTL HRANDFIELD HSCAN HSET HSETEX
        HSETNX HSTRLEN HTTL HVALS INCR INCRBY INCRBYFLOAT INCREX LINDEX LINSERT LLEN LPOP LPOS LPUSH
        LPUSHX LRANGE LREM LSET LTRIM MOVE PERSIST PEXPIRE PEXPIREAT PEXPIRETIME PFADD PSETEX PTTL
        RESTORE RESTORE-ASKING RPOP RPUSH RPUSHX SADD SCARD SET SETBIT SETEX SETNX SETRANGE SISMEMBER
        SMEMBERS SMISMEMBER SPOP SPUBLISH SRANDMEMBER SREM SSCAN STRLEN SUBSTR TTL TYPE VADD VCARD
        VDIM VEMB VGETATTR VINFO VISMEMBER VLINKS VRANDMEMBER VRANGE VREM VSETATTR VSIM XACK XACKDEL
        XADD XAUTOCLAIM XCFGSET XCLAIM XDEL XDELEX XIDMPRECORD XLEN XNACK XPENDING XRANGE XREVRANGE
        XSETID XTRIM ZADD ZCARD ZCOUNT ZINCRBY ZLEXCOUNT ZMSCORE ZPOPMAX ZPOPMIN ZRANDMEMBER ZRANGE
        ZRANGEBYLEX ZRANGEBYSCORE ZRANK ZREM ZREMRANGEBYLEX ZREMRANGEBYRANK ZREMRANGEBYSCORE
        ZREVRANGE ZREVRANGEBYLEX ZREVRANGEBYSCORE ZREVRANK ZSCAN ZSCORE
    """,
    (1, 2, 1): """
        BLMOVE BLMOVEM BRPOPLPUSH COPY GEOSEARCHSTORE LCS LMOVE LMOVEM RENAME RENAMENX RPOPLPUSH
        SMOVE ZRANGESTORE
    """,
    (1, -1, 1): """
        DEL EXISTS MGET PFCOUNT PFMERGE SDIFF SDIFFSTORE SINTER SINTERSTORE SSUBSCRIBE SUNION
        SUNIONSTORE SUNSUBSCRIBE TOUCH UNLINK WATCH
    """,
    (1, -2, 1): "BLPOP BRPOP BZPOPMAX BZPOPMIN",
    (1, -1, 2): "MSET MSETNX",
    (2, -1, 1): "BITOP",
    (2, 2, 1): """
        HIMPORT|SET MEMORY|USAGE OBJECT|ENCODING OBJECT|FREQ OBJECT|IDLETIME OBJECT|REFCOUNT PFDEBUG
        XGROUP|CREATE XGROUP|CREATECONSUMER XGROUP|DELCONSUMER XGROUP|DESTROY XGROUP|SETID
        XINFO|CONSUMERS XINFO|GROUPS XINFO|STREAM
    """,
}

# Commands with no key and no channel: sent as they are.
_KEYLESS = """
    ACL ASKING AUTH BACKUP BGREWRITEAOF BGSAVE CLIENT CLUSTER COMMAND CONFIG DBSIZE DISCARD ECHO
    EXEC FAILOVER FUNCTION HELLO HIMPORT|DISCARD HIMPORT|DISCARDALL HIMPORT|PREPARE HOTKEYS INFO
    LASTSAVE LATENCY LOLWUT MEMORY|DOCTOR MEMORY|HELP MEMORY|MALLOC-STATS MEMORY|PURGE MEMORY|STATS
    MODULE MONITOR MULTI OBJECT|HELP PFSELFTEST PING PSYNC PUBSUB|HELP PUBSUB|NUMPAT QUIT READONLY
    READWRITE REPLCONF REPLICAOF RESET ROLE SAVE SCRIPT SELECT SHUTDOWN SLAVEOF SLOWLOG SYNC TIME
    TRIMSLOTS UNWATCH WAIT WAITAOF XGROUP|HELP XINFO|HELP
"""

# Commands on the whole database, or on keys the prefix cannot name.
_REFUSED = "DEBUG FLUSHALL FLUSHDB MIGRATE RANDOMKEY SWAPDB"

# Commands whose subcommand decides where the keys are.
_CONTAINERS = frozenset({"HIMPORT", "MEMORY", "OBJECT", "PUBSUB", "XGROUP", "XINFO"})

_GLOB = frozenset("*?[]\\")

type _Argv = list[Any]
type _Rule = Callable[["KeyPrefix", _Argv], None]


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "surrogateescape")
    return str(value)


def _command_name(argv: _Argv) -> str:
    name = _text(argv[0]).upper()
    if name in _CONTAINERS and len(argv) > 1:
        return f"{name}|{_text(argv[1]).upper()}"
    return name


def _key_range(first: int, last: int, step: int) -> _Rule:
    def rule(prefix: KeyPrefix, argv: _Argv) -> None:
        end = last if last >= 0 else len(argv) + last
        for index in range(first, end + 1, step):
            argv[index] = prefix.key(argv[index])

    return rule


def _counted_keys(count_at: int, step: int = 1) -> _Rule:
    """Keys after a key count (``EVAL script numkeys key...``)."""

    def rule(prefix: KeyPrefix, argv: _Argv) -> None:
        count = int(_text(argv[count_at]))
        for index in range(count_at + 1, count_at + 1 + count * step, step):
            argv[index] = prefix.key(argv[index])

    return rule


def _destination_and_counted_keys(prefix: KeyPrefix, argv: _Argv) -> None:
    """``ZUNIONSTORE destination numkeys key...``."""
    argv[1] = prefix.key(argv[1])
    _counted_keys(2)(prefix, argv)


def _keyword_keys(start: int, keywords: frozenset[str]) -> _Rule:
    """A key at 1, and the key following each of *keywords* from *start* on."""

    def rule(prefix: KeyPrefix, argv: _Argv) -> None:
        argv[1] = prefix.key(argv[1])
        index = start
        while index < len(argv) - 1:
            if _text(argv[index]).upper() in keywords:
                argv[index + 1] = prefix.key(argv[index + 1])
                index += 1
            index += 1

    return rule


def _sort(prefix: KeyPrefix, argv: _Argv) -> None:
    """``SORT key [BY pattern] [LIMIT o c] [GET pattern...] [ASC|DESC] [ALPHA] [STORE dest]``."""
    argv[1] = prefix.key(argv[1])
    index = 2
    while index < len(argv) - 1:
        word = _text(argv[index]).upper()
        if word == "LIMIT":
            index += 3
            continue
        if word in ("BY", "GET", "STORE") and not (word == "GET" and _text(argv[index + 1]) == "#"):
            argv[index + 1] = prefix.key(argv[index + 1])
        index += 2 if word in ("BY", "GET", "STORE") else 1


# Options of XREAD / XREADGROUP before STREAMS, by how many values they take.
_XREAD_OPTIONS = {"GROUP": 2, "COUNT": 1, "BLOCK": 1, "MAXCOUNT": 1, "MAXSIZE": 1, "CLAIM": 1}


def _streams(prefix: KeyPrefix, argv: _Argv) -> None:
    """``XREAD [options] STREAMS key... id...``: the first half after STREAMS."""
    index = 1
    while index < len(argv):
        word = _text(argv[index]).upper()
        if word == "STREAMS":
            names = (len(argv) - index - 1) // 2
            for position in range(index + 1, index + 1 + names):
                argv[position] = prefix.key(argv[position])
            return
        index += 1 + _XREAD_OPTIONS.get(word, 0)


def _keys_pattern(prefix: KeyPrefix, argv: _Argv) -> None:
    argv[1] = prefix.key(argv[1])


def _match_option(start: int) -> _Rule:
    """A ``MATCH pattern`` option from *start* on, within the prefix; added when absent."""

    def rule(prefix: KeyPrefix, argv: _Argv) -> None:
        for index in range(start, len(argv) - 1):
            if _text(argv[index]).upper() == "MATCH":
                argv[index + 1] = prefix.key(argv[index + 1])
                return
        argv.extend(("MATCH", prefix.key("*")))

    return rule


def _optional_pattern(prefix: KeyPrefix, argv: _Argv) -> None:
    """``PUBSUB CHANNELS [pattern]``: the channels under the prefix when none."""
    if len(argv) > 2:
        argv[2] = prefix.key(argv[2])
    else:
        argv.append(prefix.key("*"))


def _refuse(prefix: KeyPrefix, argv: _Argv) -> None:
    raise UnprefixedCommandError(f"{_command_name(argv)} cannot run under a Redis key prefix")


def _no_keys(prefix: KeyPrefix, argv: _Argv) -> None:
    return None


def _rules() -> dict[str, _Rule]:
    rules: dict[str, _Rule] = {}
    for positions, names in _KEY_RANGES.items():
        rule = _key_range(*positions)
        rules.update(dict.fromkeys(names.split(), rule))
    rules.update(dict.fromkeys(_KEYLESS.split(), _no_keys))
    rules.update(dict.fromkeys(_REFUSED.split(), _refuse))
    rules.update(dict.fromkeys(("EVAL", "EVALSHA", "EVAL_RO", "EVALSHA_RO", "FCALL", "FCALL_RO"), _counted_keys(2)))
    rules.update(dict.fromkeys(("ZUNIONSTORE", "ZINTERSTORE", "ZDIFFSTORE"), _destination_and_counted_keys))
    rules.update(
        dict.fromkeys(
            ("ZUNION", "ZINTER", "ZDIFF", "ZINTERCARD", "SINTERCARD", "SDIFFCARD", "SUNIONCARD", "LMPOP", "ZMPOP"),
            _counted_keys(1),
        )
    )
    rules.update(dict.fromkeys(("BLMPOP", "BZMPOP"), _counted_keys(2)))
    rules["MSETEX"] = _counted_keys(1, step=2)
    rules.update(dict.fromkeys(("SORT", "SORT_RO"), _sort))
    rules["GEORADIUS"] = _keyword_keys(6, frozenset({"STORE", "STOREDIST"}))
    rules["GEORADIUSBYMEMBER"] = _keyword_keys(5, frozenset({"STORE", "STOREDIST"}))
    rules.update(dict.fromkeys(("XREAD", "XREADGROUP"), _streams))
    # Channels, which ``COMMAND`` does not count as keys.
    rules["PUBLISH"] = _key_range(1, 1, 1)
    rules.update(dict.fromkeys(("SUBSCRIBE", "UNSUBSCRIBE", "PSUBSCRIBE", "PUNSUBSCRIBE"), _key_range(1, -1, 1)))
    rules.update(dict.fromkeys(("PUBSUB|NUMSUB", "PUBSUB|SHARDNUMSUB"), _key_range(2, -1, 1)))
    rules["KEYS"] = _keys_pattern
    rules["SCAN"] = _match_option(2)
    rules.update(dict.fromkeys(("PUBSUB|CHANNELS", "PUBSUB|SHARDCHANNELS"), _optional_pattern))
    return rules


_RULES = _rules()


def _strip_all(prefix: KeyPrefix, names: Iterable[Any]) -> list[Any]:
    return [prefix.strip(name) for name in names]


def _strip_first(prefix: KeyPrefix, reply: Any) -> Any:
    """``[key, ...]`` (``BLPOP``, ``LMPOP``...), or ``None``."""
    if not isinstance(reply, (list, tuple)) or not reply:
        return reply
    return type(reply)((prefix.strip(reply[0]), *reply[1:]))


def _strip_scan(prefix: KeyPrefix, reply: Any) -> Any:
    cursor, keys = reply
    return type(reply)((cursor, _strip_all(prefix, keys)))


def _strip_streams(prefix: KeyPrefix, reply: Any) -> Any:
    if isinstance(reply, dict):
        return {prefix.strip(name): entries for name, entries in reply.items()}
    if isinstance(reply, list):
        return [_strip_first(prefix, stream) for stream in reply]
    return reply


def _strip_numsub(prefix: KeyPrefix, reply: Any) -> Any:
    if reply and isinstance(reply[0], (list, tuple)):
        return [_strip_first(prefix, pair) for pair in reply]
    return [prefix.strip(item) if index % 2 == 0 else item for index, item in enumerate(reply)]


type _ReplyRule = Callable[["KeyPrefix", Any], Any]

# The replies that carry key or channel names.
_REPLY_RULES: dict[str, _ReplyRule] = {
    "KEYS": _strip_all,
    "SCAN": _strip_scan,
    "PUBSUB|CHANNELS": _strip_all,
    "PUBSUB|SHARDCHANNELS": _strip_all,
    "PUBSUB|NUMSUB": _strip_numsub,
    "PUBSUB|SHARDNUMSUB": _strip_numsub,
    "XREAD": _strip_streams,
    "XREADGROUP": _strip_streams,
    **dict.fromkeys(
        ("BLPOP", "BRPOP", "BZPOPMIN", "BZPOPMAX", "BLMPOP", "BZMPOP", "LMPOP", "ZMPOP"),
        _strip_first,
    ),
}


class KeyPrefix:
    """A non-empty key prefix, and how it applies to commands and replies."""

    def __init__(self, prefix: str) -> None:
        if not prefix or any(c in _GLOB or c.isspace() for c in prefix):
            raise ValueError(f"Invalid Redis key prefix {prefix!r}")
        self.text = prefix
        self.raw = prefix.encode()

    def key(self, name: Any) -> Any:
        """*name* under the prefix, in the type it came in (``bytes`` stay ``bytes``)."""
        if isinstance(name, (bytes, bytearray, memoryview)):
            return self.raw + bytes(name)
        return self.text + str(name)

    def strip(self, name: Any) -> Any:
        """*name* without the prefix; a name outside it is left as it is."""
        if isinstance(name, str) and name.startswith(self.text):
            return name[len(self.text) :]
        if isinstance(name, bytes) and name.startswith(self.raw):
            return name[len(self.raw) :]
        return name

    def command(self, args: tuple[Any, ...]) -> tuple[Any, ...]:
        """The arguments of a command, its keys and channels under the prefix.

        redis-py may spell a subcommand in the command name (``"MEMORY USAGE"``):
        the positions count the words of the name, which is sent as it came.
        """
        words = _text(args[0]).split()
        argv = [*words, *args[1:]]
        rule = _RULES.get(_command_name(argv))
        if rule is None:
            raise UnprefixedCommandError(
                f"Unknown Redis command {_command_name(argv)}: its keys cannot be put under the key prefix"
            )
        rule(self, argv)
        return (args[0], *argv[len(words) :])

    def reply(self, args: tuple[Any, ...], reply: Any) -> Any:
        """*reply* to the command *args*, the key and channel names it holds without the prefix."""
        if isinstance(reply, BaseException) or reply is None:
            return reply
        argv = [*_text(args[0]).split(), *args[1:]]
        rule = _REPLY_RULES.get(_command_name(argv))
        return reply if rule is None else rule(self, reply)


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """A Redis glob pattern (``*``, ``?``, ``[...]``, ``[^...]``, ``\\x``) as a regular expression."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            out.append(".*")
        elif char == "?":
            out.append(".")
        elif char == "\\" and index + 1 < len(pattern):
            index += 1
            out.append(re.escape(pattern[index]))
        elif char == "[" and (end := pattern.find("]", index + 2)) != -1:
            body = pattern[index + 1 : end]
            negate = body.startswith("^")
            members = body[1:] if negate else body
            escaped = "".join(c if c == "-" else re.escape(c) for c in members)
            out.append(f"[{'^' if negate else ''}{escaped}]")
            index = end
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("".join(out), re.DOTALL)


class _PrefixedPubSub(PubSub):
    """A subscription whose channels and patterns are under the prefix.

    With ``_logical_names`` the caller names channels and patterns without the
    prefix and reads them so; without, its names already carry it (the SAQ
    queue's) and only the pattern subscriptions change: Redis allows a
    ``PSUBSCRIBE`` pattern only when the ACL names that very pattern, so every
    pattern is served by one subscription to ``<prefix>*``, whose messages are
    kept for the patterns they match.
    """

    _prefix: ClassVar[KeyPrefix]
    _logical_names: ClassVar[bool] = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Pattern, as Redis would name it, -> its matcher.
        self._pattern_matchers: dict[str, re.Pattern[str]] = {}
        self._all_channels_subscribed = False

    def _full(self, name: Any) -> Any:
        return self._prefix.key(name) if self._logical_names else name

    def _shown(self, name: Any) -> Any:
        return self._prefix.strip(name) if self._logical_names else name

    async def on_connect(self, connection: Any) -> None:
        # A new connection holds no subscription: redis-py makes them again.
        self._all_channels_subscribed = False
        await super().on_connect(connection)

    async def execute_command(self, *args: Any) -> Any:
        name = _text(args[0]).upper()
        if name in ("SUBSCRIBE", "UNSUBSCRIBE", "SSUBSCRIBE", "SUNSUBSCRIBE"):
            return await super().execute_command(args[0], *(self._full(channel) for channel in args[1:]))
        if name == "PSUBSCRIBE":
            for pattern in args[1:]:
                full = _text(self._full(pattern))
                self._pattern_matchers[full] = _glob_regex(full)
            if self._all_channels_subscribed:
                return None
            result = await super().execute_command("PSUBSCRIBE", self._prefix.key("*"))
            self._all_channels_subscribed = True
            return result
        if name == "PUNSUBSCRIBE":
            return await self._unsubscribe_patterns(args[1:])
        return await super().execute_command(*args)

    async def _unsubscribe_patterns(self, patterns: tuple[Any, ...]) -> Any:
        if patterns:
            for pattern in patterns:
                self._pattern_matchers.pop(_text(self._full(pattern)), None)
        else:
            self._pattern_matchers.clear()
        # Redis confirms ``<prefix>*`` only, never these patterns: they end now.
        for pattern in self._normalize_keys(dict.fromkeys(patterns or tuple(self.patterns))):
            self.pending_unsubscribe_patterns.discard(pattern)
            self.patterns.pop(pattern, None)
        if self._pattern_matchers or not self._all_channels_subscribed:
            return None
        self._all_channels_subscribed = False
        return await super().execute_command("PUNSUBSCRIBE", self._prefix.key("*"))

    async def parse_response(self, block: bool = True, timeout: float = 0) -> Any:
        response = await super().parse_response(block=block, timeout=timeout)
        if not isinstance(response, list) or len(response) < 2:
            return response
        kind = _text(response[0]).lower()
        if kind == "pmessage":
            channel = response[2]
            text = _text(channel)
            pattern = next((p for p, matcher in self._pattern_matchers.items() if matcher.fullmatch(text)), None)
            if pattern is None:
                return None
            shown = self.encoder.decode(self.encoder.encode(self._shown(pattern)))
            return [response[0], shown, self._shown(channel), *response[3:]]
        if kind in ("message", "smessage", "subscribe", "unsubscribe", "ssubscribe", "sunsubscribe",
                    "psubscribe", "punsubscribe"):
            return [response[0], self._shown(response[1]), *response[2:]]
        return response


class _PrefixedPipeline(Pipeline):
    _prefix: ClassVar[KeyPrefix]

    def execute_command(self, *args: Any, **kwargs: Any) -> Any:
        return super().execute_command(*self._prefix.command(args), **kwargs)

    async def immediate_execute_command(self, *args: Any, **options: Any) -> Any:
        return self._prefix.reply(args, await super().immediate_execute_command(*args, **options))

    async def execute(self, raise_on_error: bool = True) -> list[Any]:
        commands = [args for args, _options in self.command_stack]
        replies = await super().execute(raise_on_error)
        return [self._prefix.reply(args, reply) for args, reply in zip(commands, replies, strict=False)]


class _PrefixedRedis(Redis):
    _prefix: ClassVar[KeyPrefix]
    _pipeline_class: ClassVar[type[Pipeline]]
    _pubsub_class: ClassVar[type[PubSub]]

    async def execute_command(self, *args: Any, **options: Any) -> Any:
        args = self._prefix.command(args)
        return self._prefix.reply(args, await super().execute_command(*args, **options))

    def pipeline(self, transaction: bool = True, shard_hint: str | None = None) -> Pipeline:
        return self._pipeline_class(self.connection_pool, self.response_callbacks, transaction, shard_hint)

    def pubsub(self, **kwargs: Any) -> PubSub:
        return self._pubsub_class(self.connection_pool, event_dispatcher=self._event_dispatcher, **kwargs)


@functools.cache
def prefixed_client_class(prefix: str) -> type[Redis]:
    """The ``redis.asyncio.Redis`` class whose keys and channels are under *prefix*.

    Its pipelines and pub/sub subscriptions are too. A class rather than an
    instance, so that Sentinel's ``master_for(redis_class=...)`` builds it.
    """
    key_prefix = KeyPrefix(prefix)
    pubsub_class = type("PrefixedPubSub", (_PrefixedPubSub,), {"_prefix": key_prefix})
    pipeline_class = type("PrefixedPipeline", (_PrefixedPipeline,), {"_prefix": key_prefix})
    return type(
        "PrefixedRedis",
        (_PrefixedRedis,),
        {"_prefix": key_prefix, "_pipeline_class": pipeline_class, "_pubsub_class": pubsub_class},
    )


@functools.cache
def prefixed_pattern_pubsub_class(prefix: str) -> type[PubSub]:
    """A ``PubSub`` for names that already carry *prefix*: only its pattern subscriptions change.

    For the SAQ queue, which names its channels itself (``app.core.queue``).
    """
    return type(
        "PrefixedPatternPubSub",
        (_PrefixedPubSub,),
        {"_prefix": KeyPrefix(prefix), "_logical_names": False},
    )

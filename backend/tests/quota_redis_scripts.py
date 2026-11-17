"""Python twins of the quota Lua scripts, for the in-memory Redis doubles.

The doubles in the failure-pool suites hold counters in a ``store`` dict and
cause hashes in a ``hashes`` dict. ``run_quota_script`` runs whichever quota
script a double's ``eval`` receives against those two dicts, with the same
semantics as the Lua. The scripts themselves run against a real Redis in
``test_quota_scripts_redis.py``. Expiries are not modelled.

``RedisHashes`` is the hash and set-with-expiry surface the quota clears use
(the stash index, the stash itself, the flush's yield key), for a double to
inherit next to its own counters.
"""

from __future__ import annotations


def _number(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class RedisHashes:
    """Hash commands over ``self.hashes``, ``EXISTS`` over both dicts."""

    store: dict
    hashes: dict

    async def hset(self, name, key, value):
        self.hashes.setdefault(name, {})[key] = value
        return 1

    async def hget(self, name, key):
        return self.hashes.get(name, {}).get(key)

    async def hdel(self, name, *keys):
        held = self.hashes.get(name, {})
        removed = sum(held.pop(key, None) is not None for key in keys)
        if name in self.hashes and not held:
            del self.hashes[name]
        return removed

    async def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    async def hscan_iter(self, name, match=None, count=None):
        for item in list(self.hashes.get(name, {}).items()):
            yield item

    async def exists(self, *keys):
        return sum(key in self.store or key in self.hashes for key in keys)


def run_quota_script(store: dict, hashes: dict, script: str, keys_and_args: tuple):
    from app.core.redis import DECR_IF_POSITIVE_LUA
    from app.services import quota

    if script == quota._CLAIM_POOL_SLOT_LUA:
        counter, user_epoch, global_epoch, pool, _ttl, baseline = keys_and_args
        if counter not in store:
            if baseline == "":
                return [-1, 0, 0]
            store[counter] = _number(baseline)
        store[counter] = _number(store[counter]) + 1
        claimed = store[counter]
        if claimed > int(pool):
            store[counter] = claimed - 1
        return [claimed, _number(store.get(user_epoch)), _number(store.get(global_epoch))]
    if script == quota._COUNT_FREE_CREDIT_LUA:
        counter, user_epoch, _ttl, baseline = keys_and_args
        if counter not in store:
            if baseline == "":
                return [-1, 0]
            store[counter] = _number(baseline)
        store[counter] = _number(store.get(counter)) + 1
        return [store[counter], _number(store.get(user_epoch))]
    if script == quota._RECORD_REFUND_CAUSE_LUA:
        counter, cause_key, granted_at, cause, _ttl = keys_and_args
        if _number(store.get(counter)) < int(granted_at):
            return 0
        bucket = hashes.setdefault(cause_key, {})
        bucket[cause] = bucket.get(cause, 0) + 1
        return 1
    if script == quota._ADVANCE_REFUND_EPOCH_LUA:
        epoch_key, _ttl = keys_and_args
        store[epoch_key] = _number(store.get(epoch_key)) + 1
        return store[epoch_key]
    if script == quota._SET_IF_GENERATION_LUA:
        cache_key, generation_key, payload, _ttl, generation = keys_and_args
        if str(store.get(generation_key, "")) != str(generation):
            return 0
        store[cache_key] = payload
        return 1
    if script == quota._COUNT_GLOBAL_LUA:
        # KEYS = n counters, ARGV = their TTL + one saved mark each.
        n = (len(keys_and_args) - 1) // 2
        counters, marks = keys_and_args[:n], keys_and_args[n + 1 :]
        pairs = list(zip(counters, marks, strict=True))
        if any(mark == "" and counter not in store for counter, mark in pairs):
            return -1
        for counter, mark in pairs:
            store[counter] = _number(store.get(counter, mark)) + 1
        return n
    if script == quota._WRITE_RECEIPT_LUA:
        key, receipt, _ttl, redeemed = keys_and_args
        if store.get(key) == redeemed:
            return 0
        store[key] = receipt
        return 1
    if script == quota._REDEEM_RECEIPT_LUA:
        key, redeemed, _ttl = keys_and_args
        receipt = store.get(key)
        if receipt == redeemed:
            return [quota.RECEIPT_ALREADY_REDEEMED]
        store[key] = redeemed
        if receipt is None:
            return [quota.RECEIPT_ABSENT]
        return [quota.RECEIPT_TAKEN, receipt]
    if script == quota._CREDIT_CHARGE_ONCE_LUA:
        # KEYS = the receipt + n counters, ARGV = credited + the receipt TTL.
        receipt, counters = keys_and_args[0], keys_and_args[1:-2]
        credited = keys_and_args[-2]
        if store.get(receipt) == credited:
            return quota.CHARGE_CREDIT_ALREADY
        for counter in counters:
            current = _number(store.get(counter))
            if current > 0:
                store[counter] = current - 1
        store[receipt] = credited
        return quota.CHARGE_CREDIT_LANDED
    if script == DECR_IF_POSITIVE_LUA:
        key = keys_and_args[0]
        current = _number(store.get(key))
        if current > 0:
            store[key] = current - 1
            return store[key]
        return current
    if script == quota._STASH_COUNTERS_LUA:
        # KEYS = stash + n counters, ARGV = stash TTL + n seeds.
        n = (len(keys_and_args) - 2) // 2
        stash, counters = keys_and_args[0], keys_and_args[1 : 1 + n]
        seeds = keys_and_args[2 + n :]
        moved = 0
        for counter, seed in zip(counters, seeds, strict=True):
            if counter not in store:
                continue
            raw = store.pop(counter)
            if not str(raw).lstrip("-").isdigit():
                continue
            count, seed = int(raw), int(seed)
            held = hashes.setdefault(stash, {})
            if counter in held:
                prior, _pttl, prior_seed = held[counter].split(":")
                count, seed = int(prior) + count - seed, int(prior_seed)
            held[counter] = f"{count}:-1:{seed}"
            moved += 1
        return moved
    if script == quota._RESTORE_STASHED_LUA:
        stash, counters = keys_and_args[0], keys_and_args[1:]
        held = hashes.get(stash, {})
        restored = 0
        for counter in counters:
            packed = held.pop(counter, None)
            if packed is None:
                continue
            count, _pttl, seed = (int(part) for part in packed.split(":"))
            if counter in store:
                store[counter] = _number(store[counter]) + count - seed
            else:
                store[counter] = count
            restored += 1
        if stash in hashes and not held:
            del hashes[stash]
        return restored
    raise AssertionError(f"no Python twin for this script:\n{script}")

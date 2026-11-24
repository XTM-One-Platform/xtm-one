"""The CTEM posture is stamped with the profile inputs it reads, not the profile version.

Before: any new organization profile version (a save of Owned domains, a
description, a sync writing the same rows) made every cached posture stale,
and the command center showed "Syncing..." for a 20 to 30 s rebuild. The
stamp is now ``ctem_posture_inputs.fingerprint`` of the declared inputs.
"""

from __future__ import annotations

import inspect
import json
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.api.v1 import ctem_metrics
from app.services import asset_reachability, ctem_metrics_collector
from app.services import ctem_posture_inputs as inputs

ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
ASSETS = [
    {
        "name": "web-01",
        "category": "host",
        "internet_facing": "yes",
        "source": "intune",
        "external_id": "d-1",
        "last_seen": "2026-11-23",
        "sync_id": "s1",
        "owner": "ops",
    },
    {"name": "db-01", "category": "host", "internet_facing": "no", "group": "core"},
]
GROUPS = [{"name": "core", "internet_facing": "no", "description": "the core"}]
SOFTWARE = [{"vendor": "acme", "product": "gw", "cpe": "", "versions": ["1.2", "1.1"]}]
TYPED = [{"vendor": "acme", "product": "vpn", "cpe": "", "version": "3", "source": "manual"}]


def _stamp(**over: Any) -> str:
    kwargs: dict[str, Any] = {"typed_software": TYPED, "software": SOFTWARE, "assets": ASSETS, "groups": GROUPS}
    kwargs.update(over)
    return inputs.fingerprint(ORG, **kwargs)


def test_the_stamp_keeps_the_organization_id():
    assert _stamp().startswith(f"{ORG}:")


def test_a_field_the_build_never_reads_leaves_the_stamp_unchanged():
    # Acceptance 1: Owned domains, descriptions, PIR details are not inputs at all;
    # a field of a row the reachability rules never read is not either.
    assets = [{**ASSETS[0], "owner": "secops", "last_seen": "2026-11-24", "sync_id": "s2"}, ASSETS[1]]
    groups = [{**GROUPS[0], "description": "renamed"}]
    assert _stamp(assets=assets, groups=groups) == _stamp()
    assert "owned_domains" not in inputs.PROFILE_FIELDS and "policy" not in inputs.PROFILE_FIELDS


def test_an_input_change_changes_the_stamp():
    # Acceptance 2: software, assets, groups.
    base = _stamp()
    assert _stamp(assets=[{**ASSETS[0], "internet_facing": "no"}, ASSETS[1]]) != base
    assert _stamp(assets=ASSETS[:1]) != base
    assert _stamp(groups=[{"name": "core", "internet_facing": "yes"}]) != base
    assert _stamp(software=[{**SOFTWARE[0], "versions": ["1.3"]}]) != base
    assert _stamp(typed_software=[]) != base


def test_identical_inputs_in_another_order_give_the_same_stamp():
    # Acceptance 3: a sync writing the same rows (another order, other versions order).
    again = _stamp(
        assets=list(reversed(ASSETS)),
        software=[{**SOFTWARE[0], "versions": ["1.1", "1.2"]}],
    )
    assert again == _stamp()


def test_record_value_lists_and_profile_dicts_stamp_alike():
    projected = [[a.get(f) for f in inputs.ASSET_FIELDS] for a in ASSETS]
    assert _stamp(assets=projected) == _stamp()


async def test_no_session_no_stamp():
    assert await inputs.posture_inputs_stamp(None) is None


# ── Drift: the build reads nothing the fingerprint does not declare ──────────


class _Recording(dict):
    """A row that records the keys the reachability rules read."""

    read: set[str] = set()

    def get(self, key, default=None):  # type: ignore[override]
        type(self).read.add(key)
        return super().get(key, default)


def test_the_reachability_rules_read_only_declared_asset_and_group_fields():
    _Recording.read = set()
    assets = [_Recording(a) for a in ASSETS]
    groups = [_Recording(g) for g in GROUPS]
    asset_reachability.reachability_split(assets, groups)
    asset_reachability.unreachable_keys({"CVE-1": [_Recording(ASSETS[0])]}, assets, groups)
    declared = set(inputs.ASSET_FIELDS) | set(inputs.GROUP_FIELDS)
    assert _Recording.read <= declared, (
        f"the posture build reads {sorted(_Recording.read - declared)}: declare them in "
        "ctem_posture_inputs (ASSET_FIELDS or GROUP_FIELDS) so a change rebuilds the posture"
    )
    # And every field the rules name statically is declared too.
    named = set(re.findall(r'_field\(\w+, "(\w+)"\)', inspect.getsource(asset_reachability)))
    assert named <= declared, sorted(named - declared)


def test_the_collector_reads_only_declared_profile_fields():
    source = inspect.getsource(ctem_metrics_collector)
    read = set(re.findall(r"\bprofile\.(\w+)", source))
    assert read <= set(inputs.PROFILE_FIELDS), (
        f"the posture build reads profile.{sorted(read - set(inputs.PROFILE_FIELDS))}: declare it in "
        "ctem_posture_inputs.PROFILE_FIELDS and hash it in the fingerprint"
    )
    typed = set(re.findall(r'"(\w+)": e\.(\w+)', source))
    assert {attr for _key, attr in typed} <= set(inputs.TYPED_SOFTWARE_FIELDS)
    # The profile readers the build calls: a new one is a new input to declare.
    readers = set(re.findall(r"org_context\.(\w+)\(", source))
    assert readers <= {"current_version", "current_profile", "parse_profile"}, sorted(readers)
    stores = set(re.findall(r"software_store\.(\w+)\(", source))
    assert stores <= {"matcher_software", "matcher_entries"}, sorted(stores)


# ── The route: a save outside the inputs serves the cache, an input change rebuilds once ──


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value


async def test_a_new_profile_version_with_the_same_inputs_serves_the_cache(monkeypatch):
    monkeypatch.setattr(ctem_metrics, "_require_xtm_one", lambda: None)
    ctem_metrics._build_locks.clear()
    ctem_metrics._build_lock_refs.clear()
    client = _FakeRedis()

    async def _client():
        return client

    monkeypatch.setattr(ctem_metrics, "_redis_or_none", _client)
    profile: dict[str, Any] = {"version": 48, "owned_domains": ["a.example"], "assets": ASSETS}

    async def _stamp_of(_db):
        # What posture_inputs_stamp computes: the version and the domains are not inputs.
        return inputs.fingerprint(ORG, typed_software=[], software=[], assets=profile["assets"], groups=[])

    monkeypatch.setattr(ctem_metrics, "_profile_stamp", _stamp_of)
    builds = 0

    async def fake_build(db, user, vertical):
        nonlocal builds
        builds += 1
        return {"generated_at": datetime.now(UTC).isoformat()}, {}

    monkeypatch.setattr(ctem_metrics, "_build_posture_payload", fake_build)
    rebuilds: list = []
    monkeypatch.setattr(ctem_metrics, "_schedule_posture_refresh", lambda *a: rebuilds.append(a))

    async def _context(_db, payload):
        return payload

    monkeypatch.setattr(ctem_metrics, "_with_context", _context)
    user = SimpleNamespace(id="u-inputs", ctem_vertical="cyber")

    await ctem_metrics.ctem_posture(vertical=None, db=None, user=user)
    assert builds == 1
    # Two saves of Owned domains: two new versions, same inputs.
    for version, domains in ((49, ["a.example", "b.example"]), (50, ["b.example"])):
        profile.update(version=version, owned_domains=domains)
        served = await ctem_metrics.ctem_posture(vertical=None, db=None, user=user)
        assert served["refreshing"] is False and rebuilds == [] and builds == 1

    # An asset turns internet-facing: one rebuild, then fresh again.
    profile["assets"] = [{**ASSETS[1], "internet_facing": "yes"}]
    served = await ctem_metrics.ctem_posture(vertical=None, db=None, user=user)
    assert served["refreshing"] is True and len(rebuilds) == 1
    await ctem_metrics._build_and_cache(None, user, "cyber", client)
    assert builds == 2
    served = await ctem_metrics.ctem_posture(vertical=None, db=None, user=user)
    assert served["refreshing"] is False and len(rebuilds) == 1
    posture_key, _ = ctem_metrics._posture_cache_keys("u-inputs", "cyber")
    assert json.loads(client.store[posture_key])["profile_version"] == await _stamp_of(None)

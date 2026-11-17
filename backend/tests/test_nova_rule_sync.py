"""Nova rule GitHub sync (Phase C) — splitter (pure) + upsert/prune (Postgres).

GitHub is mocked: ``_list_nov_files`` and ``_fetch_file`` are monkeypatched so no
network is touched and the upsert/prune/on_connect behaviour is what's tested.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.integrations.providers import nova as nova_provider
from app.models.integration import Integration
from app.models.nova_rule import NovaRule
from app.models.nova_rule_source import NovaRuleSource
from app.models.relay import RelayGroup
from app.models.user import User
from app.services import nova_guard, nova_rule_sync
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_TABLES = [
    User.__table__,
    RelayGroup.__table__,
    Integration.__table__,
    NovaRule.__table__,
    NovaRuleSource.__table__,
]


# ── split_rules (pure) ───────────────────────────────────────────────────────


def test_split_rules_separates_multiple_blocks():
    text_ = (
        'rule A { meta: category = "x"\n keywords: $a = "hi"\n condition: keywords.$a }\n'
        'rule B { meta: category = "y"\n keywords: $b = "bye"\n condition: keywords.$b }'
    )
    blocks = nova_guard.split_rules(text_)
    assert len(blocks) == 2
    assert nova_guard.parse_rule(blocks[0]).name == "A"
    assert nova_guard.parse_rule(blocks[1]).name == "B"


def test_split_rules_ignores_braces_in_regex_and_strings():
    # `{3,}` inside a regex must not end the block early.
    text_ = (
        "rule Unicode {\n keywords:\n"
        "  $u = /[\\uFEFF]{3,}/\n"
        '  $s = "curly } inside string"\n'
        " condition: keywords.$u or keywords.$s\n}"
    )
    blocks = nova_guard.split_rules(text_)
    assert len(blocks) == 1
    assert nova_guard.parse_rule(blocks[0]).name == "Unicode"


# ── DB harness ───────────────────────────────────────────────────────────────


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("novasync", tables=_TABLES) as db:
        yield db.sessions


def _rule_block(name: str, keyword: str) -> str:
    return (
        f'rule {name} {{ meta: category = "Jailbreak"\n'
        f'  keywords: $k = "{keyword}"\n'
        f"  condition: keywords.$k }}"
    )


def _patch_github(monkeypatch, files: dict[str, str]):
    async def fake_list(_client, _repo, _branch, _path):
        return list(files.keys())

    async def fake_fetch(_client, _repo, _branch, fpath):
        return files.get(fpath)

    monkeypatch.setattr(nova_rule_sync, "_list_nov_files", fake_list)
    monkeypatch.setattr(nova_rule_sync, "_fetch_file", fake_fetch)


@pytest.mark.asyncio
async def test_sync_upserts_then_prunes(monkeypatch):
    async with _temp_database() as sessions, sessions() as session:
        source = NovaRuleSource(
            id=uuid.uuid4(), repo="acme/rules", branch="main", sync_interval="daily"
        )
        session.add(source)
        await session.commit()
        tag = nova_rule_sync._sync_tag(source.id)

        # First sync: one file with two rules.
        _patch_github(
            monkeypatch,
            {
                "a.nov": _rule_block("Alpha", "danger")
                + "\n"
                + _rule_block("Beta", "attack"),
            },
        )
        count = await nova_rule_sync.sync_nova_rule_source(session, source)
        await session.commit()
        assert count == 2
        rules = (await session.execute(select(NovaRule))).scalars().all()
        assert sorted(r.name for r in rules) == ["Alpha", "Beta"]
        assert all(r.is_company_managed and tag in (r.tags or []) for r in rules)
        assert all(r.action == "log" for r in rules)  # synced rules default to log

        # Second sync: Beta gone from the repo → pruned; Gamma added.
        _patch_github(
            monkeypatch,
            {"a.nov": _rule_block("Alpha", "danger") + "\n" + _rule_block("Gamma", "evil")},
        )
        count2 = await nova_rule_sync.sync_nova_rule_source(session, source)
        await session.commit()
        assert count2 == 2
        names = sorted(
            r.name for r in (await session.execute(select(NovaRule))).scalars().all()
        )
        assert names == ["Alpha", "Gamma"]


@pytest.mark.asyncio
async def test_a_sync_keeps_the_admins_action_and_switch(monkeypatch):
    """A rule promoted to block, or switched off, stays so; its text still updates."""
    async with _temp_database() as sessions, sessions() as session:
        source = NovaRuleSource(id=uuid.uuid4(), repo="acme/rules")
        session.add(source)
        await session.commit()
        _patch_github(monkeypatch, {"a.nov": _rule_block("Alpha", "first") + "\n" + _rule_block("Beta", "second")})
        await nova_rule_sync.sync_nova_rule_source(session, source)
        await session.commit()

        rules = {r.name: r for r in (await session.execute(select(NovaRule))).scalars().all()}
        rules["Alpha"].action = "block"
        rules["Beta"].enabled = False
        await session.commit()

        _patch_github(monkeypatch, {"a.nov": _rule_block("Alpha", "third") + "\n" + _rule_block("Beta", "fourth")})
        await nova_rule_sync.sync_nova_rule_source(session, source)
        await session.commit()

        session.expire_all()
        rules = {r.name: r for r in (await session.execute(select(NovaRule))).scalars().all()}
        assert (rules["Alpha"].action, rules["Alpha"].enabled) == ("block", True)
        assert (rules["Beta"].action, rules["Beta"].enabled) == ("log", False)
        assert "third" in rules["Alpha"].rule_text and "fourth" in rules["Beta"].rule_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update",
    [
        'rule Alpha { keywords: $a = "z" }',
        'rule Alpha { keywords: $a = "z"\n  condition: keywords.$a',
        'rule Open { keywords: $a = "/* z"\nrule Alpha { keywords: $a = "z" }',
    ],
    ids=["no-condition", "never-closes", "after-a-block-that-never-closes"],
)
async def test_a_refused_update_keeps_the_last_version_and_the_admins_action(monkeypatch, update):
    """An owned rule whose upstream update no longer parses is kept, not pruned."""
    async with _temp_database() as sessions, sessions() as session:
        source = NovaRuleSource(id=uuid.uuid4(), repo="acme/rules")
        session.add(source)
        await session.commit()
        _patch_github(monkeypatch, {"a.nov": _rule_block("Alpha", "first")})
        await nova_rule_sync.sync_nova_rule_source(session, source)
        await session.commit()
        (rule,) = (await session.execute(select(NovaRule))).scalars().all()
        rule.action = "block"
        await session.commit()

        _patch_github(monkeypatch, {"a.nov": update})
        count = await nova_rule_sync.sync_nova_rule_source(session, source)
        await session.commit()

        session.expire_all()
        (kept,) = (await session.execute(select(NovaRule))).scalars().all()
        assert count == 1
        assert (kept.name, kept.action, kept.enabled) == ("Alpha", "block", True)
        assert "first" in kept.rule_text
        await session.refresh(source)
        assert "Kept the last accepted version of 1 rule(s)" in (source.last_sync_error or "")
        assert "Alpha" in source.last_sync_error


@pytest.mark.asyncio
async def test_an_unreadable_file_removes_no_rule(monkeypatch):
    """A file that answered non-200 used to prune every rule it held."""
    async with _temp_database() as sessions, sessions() as session:
        source = NovaRuleSource(id=uuid.uuid4(), repo="acme/rules")
        session.add(source)
        await session.commit()
        _patch_github(monkeypatch, {"a.nov": _rule_block("Alpha", "first"), "b.nov": _rule_block("Beta", "b")})
        await nova_rule_sync.sync_nova_rule_source(session, source)
        await session.commit()

        _patch_github(monkeypatch, {"a.nov": None, "b.nov": _rule_block("Beta", "b")})
        await nova_rule_sync.sync_nova_rule_source(session, source)
        await session.commit()

        session.expire_all()
        names = sorted(r.name for r in (await session.execute(select(NovaRule))).scalars().all())
        assert names == ["Alpha", "Beta"]
        await session.refresh(source)
        assert "1 rule file(s) could not be read, so no rule was removed." in (source.last_sync_error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remaining",
    [
        "// rule Alpha {\n" + _rule_block("Gamma", "third"),
        _rule_block("Gamma", "rule Alpha {"),
        _rule_block("Gamma", "third").replace("  condition", "  // } rule Alpha {\n  condition"),
    ],
    ids=["header-in-a-comment", "header-in-a-string", "header-in-a-comment-inside-a-rule"],
)
async def test_a_removed_rule_named_only_in_other_text_is_pruned(monkeypatch, remaining):
    """Only a real declaration keeps a rule whose update is refused."""
    async with _temp_database() as sessions, sessions() as session:
        source = NovaRuleSource(id=uuid.uuid4(), repo="acme/rules")
        session.add(source)
        await session.commit()
        _patch_github(monkeypatch, {"a.nov": _rule_block("Alpha", "first") + "\n" + _rule_block("Gamma", "second")})
        await nova_rule_sync.sync_nova_rule_source(session, source)
        await session.commit()
        rules = {r.name: r for r in (await session.execute(select(NovaRule))).scalars().all()}
        rules["Alpha"].action = "block"
        await session.commit()

        _patch_github(monkeypatch, {"a.nov": remaining})
        await nova_rule_sync.sync_nova_rule_source(session, source)
        await session.commit()

        names = sorted(r.name for r in (await session.execute(select(NovaRule))).scalars().all())
        assert names == ["Gamma"]


@pytest.mark.asyncio
async def test_unparseable_rules_are_skipped(monkeypatch):
    async with _temp_database() as sessions, sessions() as session:
        source = NovaRuleSource(id=uuid.uuid4(), repo="acme/rules")
        session.add(source)
        await session.commit()
        _patch_github(
            monkeypatch,
            {"x.nov": _rule_block("Good", "danger") + "\nrule Bad { keywords: $a = \"z\" }"},
        )
        count = await nova_rule_sync.sync_nova_rule_source(session, source)
        await session.commit()
        # 'Bad' has no condition → skipped; only 'Good' lands.
        assert count == 1
        names = [r.name for r in (await session.execute(select(NovaRule))).scalars().all()]
        assert names == ["Good"]


@pytest.mark.asyncio
async def test_on_connect_seeds_default_source_idempotently():
    async with _temp_database() as sessions, sessions() as session:
        integ = Integration(
            id=uuid.uuid4(), name="Nova", provider="nova",
            auth_type="none", status="connected",
            # Only a company-managed connection owns platform-wide Nova state.
            is_company_managed=True,
        )
        session.add(integ)
        await session.commit()

        await nova_provider.on_connect(session, integ)
        await session.commit()
        await nova_provider.on_connect(session, integ)  # idempotent
        await session.commit()

        sources = (
            (await session.execute(select(NovaRuleSource))).scalars().all()
        )
        assert len(sources) == 1
        assert sources[0].repo == nova_provider.DEFAULT_REPO
        assert sources[0].integration_id == integ.id


@pytest.mark.asyncio
async def test_personal_connection_never_seeds_a_source():
    """Nova's rules are platform-wide, so only a company-managed connection may
    own them — otherwise any non-admin could create (and later delete) company
    rules just by connecting/disconnecting a personal integration."""
    async with _temp_database() as sessions, sessions() as session:
        personal = Integration(
            id=uuid.uuid4(), name="Nova", provider="nova", auth_type="none",
            status="connected", is_company_managed=False,
        )
        session.add(personal)
        await session.commit()

        await nova_provider.on_connect(session, personal)
        await session.commit()
        assert (await session.execute(select(NovaRuleSource))).scalars().all() == []

        # And disconnecting it must not touch company-managed rules.
        keeper = NovaRule(
            id=uuid.uuid4(), name="Keeper", category="T", action="log",
            rule_text=_rule_block("Keeper", "x"), enabled=True,
            is_company_managed=True,
        )
        session.add(keeper)
        await session.commit()
        await nova_provider.on_disconnect(session, personal)
        await session.commit()
        names = [r.name for r in (await session.execute(select(NovaRule))).scalars().all()]
        assert names == ["Keeper"]


@pytest.mark.asyncio
async def test_sync_skips_a_name_owned_outside_this_source(monkeypatch):
    """A repo rule whose name already exists as another company rule must not
    be duplicated (both would evaluate) nor adopted (disconnecting would then
    delete an admin's hand-made rule)."""
    async with _temp_database() as sessions, sessions() as session:
        source = NovaRuleSource(id=uuid.uuid4(), repo="acme/rules")
        handmade = NovaRule(
            id=uuid.uuid4(), name="Shared", category="Hand", action="block",
            rule_text=_rule_block("Shared", "original"), enabled=True,
            is_company_managed=True, tags=["mine"],
        )
        session.add_all([source, handmade])
        await session.commit()

        _patch_github(monkeypatch, {"a.nov": _rule_block("Shared", "fromrepo")})
        await nova_rule_sync.sync_nova_rule_source(session, source)
        await session.commit()

        rows = (await session.execute(select(NovaRule))).scalars().all()
        assert len(rows) == 1, "must not create a same-named duplicate"
        assert rows[0].id == handmade.id
        assert "original" in rows[0].rule_text, "hand-made rule left untouched"
        assert rows[0].tags == ["mine"], "not hijacked by the sync"

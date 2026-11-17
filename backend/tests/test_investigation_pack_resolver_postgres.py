"""Install-specific investigation packs, against real PostgreSQL.

What this file is for is *visibility*. The knowledge-base and MCP packs turn
rows into sources, and the rows belong to people: a corpus somebody else owns
must not become a source an investigation reads on my behalf, and a tool
nobody designated must not become one an investigation calls. Both properties
are enforced by ``user_visible_query`` against real SQL — a stubbed session
would assert only that this module builds the query it builds.

``integration``-marked and skipped when PostgreSQL is unreachable, like its
neighbours. The resolver is called directly rather than over HTTP: what is
under test is which rows come back for whom, and an app fixture would not
cover a single extra branch.
"""

from __future__ import annotations

import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.database import Base
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.user import User
from app.services import investigation_packs
from app.services.investigation_pack_resolver import (
    KB_PACK_SLUG,
    MCP_PACK_SLUG,
    resolve_packs,
    resolve_selected,
)
from app.services.investigation_packs import (
    DISINFO_INFRASTRUCTURE_SLUG,
    UnknownSourcePackError,
    recommend_packs,
)

pytestmark = pytest.mark.integration


def _base_url() -> str:
    # CRLF-safe: a .env dragged through Windows leaves \r on the value.
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    ).strip()


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    """Isolated database with the full schema, dropped on exit."""
    parsed = urllib.parse.urlparse(_base_url())
    admin_url = parsed._replace(path="/postgres").geturl()
    db_name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    test_url = parsed._replace(path=f"/{db_name}").geturl()

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except (DBAPIError, OperationalError, ProgrammingError) as exc:
        code = getattr(getattr(exc, "orig", None), "pgcode", None)
        if code == "42501":
            pytest.skip("DATABASE_URL user requires CREATEDB privilege")
        raise
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    finally:
        await admin_engine.dispose()

    engine = create_async_engine(test_url)
    try:
        async with engine.begin() as conn:
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception as exc:
                pytest.skip(f"pgvector unavailable: {exc}")
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        cleanup = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with cleanup.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        finally:
            await cleanup.dispose()


def _user(email: str) -> User:
    # `role`, not `is_admin`: the latter is a read-only property over it, and
    # assigning it raises.
    return User(
        id=uuid.uuid4(), email=email, hashed_password="x", full_name=email, role="user"
    )


def _server(owner: User, name: str, **kw) -> MCPServer:
    return MCPServer(
        id=uuid.uuid4(),
        user_id=owner.id,
        name=name,
        transport="stdio",
        command="echo",
        enabled=kw.pop("enabled", True),
        **kw,
    )


@pytest.mark.asyncio
async def test_a_corpus_someone_else_owns_is_not_a_source() -> None:
    """The pack is built from what the CALLER can see, nothing wider.

    The failure this guards is quiet and serious: an investigation that
    searched every corpus on the install would read a colleague's private
    documents and cite them by name in a report, with no step in the block
    looking any different from a legitimate one.
    """
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        mine, theirs = _user("mine@x.io"), _user("theirs@x.io")
        db.add_all([mine, theirs])
        await db.flush()
        db.add_all([
            KnowledgeBase(id=uuid.uuid4(), user_id=mine.id, name="Mine"),
            KnowledgeBase(id=uuid.uuid4(), user_id=theirs.id, name="Theirs"),
        ])
        await db.commit()

        packs = await resolve_packs(db, mine)
        names = [s.name for s in packs[KB_PACK_SLUG].sources]
        assert names == ["Mine"]

        packs = await resolve_packs(db, theirs)
        assert [s.name for s in packs[KB_PACK_SLUG].sources] == ["Theirs"]


@pytest.mark.asyncio
async def test_only_a_designated_enabled_tool_becomes_a_source() -> None:
    """Three ways a server is not an investigation source, all silent.

    An MCP tool can have side effects, so the gate is opt-in: without a name
    written by a person, no tool is callable by an investigation. A disabled
    server is not callable either — it is not callable for anything.
    """
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me = _user("me@x.io")
        db.add(me)
        await db.flush()
        db.add_all([
            # Two designated servers, added in reverse name order, so the
            # sort has something to do — with one, any order looks right.
            _server(me, "Zulu", investigation_tool_name="lookup"),
            _server(me, "Alpha", investigation_tool_name="search"),
            _server(me, "Undesignated"),
            _server(me, "Blank", investigation_tool_name=""),
            _server(
                me, "Disabled", enabled=False, investigation_tool_name="lookup"
            ),
        ])
        await db.commit()

        packs = await resolve_packs(db, me)
        assert [s.name for s in packs[MCP_PACK_SLUG].sources] == [
            "Alpha · search",
            "Zulu · lookup",
        ]
        source = packs[MCP_PACK_SLUG].sources[0]
        assert source.kind == "mcp_tool"
        # Unpinned: the argument is read off the tool's schema when called.
        assert source.mcp_argument is None


@pytest.mark.asyncio
async def test_a_designated_tool_on_someone_elses_server_is_not_a_source() -> None:
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        mine, theirs = _user("mine@x.io"), _user("theirs@x.io")
        db.add_all([mine, theirs])
        await db.flush()
        db.add(_server(theirs, "Theirs", investigation_tool_name="lookup"))
        await db.commit()

        packs = await resolve_packs(db, mine)
        assert MCP_PACK_SLUG not in packs


@pytest.mark.asyncio
async def test_an_install_with_nothing_extra_offers_only_the_built_ins() -> None:
    """An empty dynamic pack is absent, not present and empty.

    A pack offering zero sources renders in the picker as an entry that cannot
    be chosen — an offer of coverage that does not exist.
    """
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me = _user("me@x.io")
        db.add(me)
        await db.flush()
        await db.commit()

        packs = await resolve_packs(db, me)
        assert KB_PACK_SLUG not in packs
        assert MCP_PACK_SLUG not in packs
        assert "web-general" in packs


@pytest.mark.asyncio
async def test_a_run_with_no_user_gets_the_built_ins_alone() -> None:
    """There is no "everything" to fall back to.

    The dynamic packs exist because somebody can see a corpus. With no
    caller, resolving them against every row would query private corpora on
    behalf of nobody.
    """
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        owner = _user("owner@x.io")
        db.add(owner)
        await db.flush()
        db.add(KnowledgeBase(id=uuid.uuid4(), user_id=owner.id, name="Private"))
        await db.commit()

        packs = await resolve_packs(db, None)
        assert KB_PACK_SLUG not in packs
        # Compared against the registry rather than a hard-coded slug: what is
        # under test is that NOTHING install-specific was added, not which
        # built-in packs happen to ship.
        assert set(packs) == set(investigation_packs.BUILTIN_PACKS)


@pytest.mark.asyncio
async def test_the_dynamic_packs_are_never_recommended() -> None:
    """A chat-initiated run that skipped the picker must not widen itself.

    `recommend_packs` is what a run without an explicit selection uses. If
    these packs were auto-selected, every such run would search every corpus
    the caller can see and call every designated tool — a large change in what
    an investigation does, decided by nobody.
    """
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me = _user("me@x.io")
        db.add(me)
        await db.flush()
        db.add(KnowledgeBase(id=uuid.uuid4(), user_id=me.id, name="Corpus"))
        db.add(_server(me, "Server", investigation_tool_name="lookup"))
        await db.commit()

        packs = await resolve_packs(db, me)
        assert KB_PACK_SLUG in packs and MCP_PACK_SLUG in packs
        assert packs[KB_PACK_SLUG].auto_select is False
        assert packs[MCP_PACK_SLUG].auto_select is False

        for subject_type in ("cve", "domain", "unknown"):
            chosen = {p.slug for p in recommend_packs(subject_type)}
            assert KB_PACK_SLUG not in chosen
            assert MCP_PACK_SLUG not in chosen


@pytest.mark.asyncio
async def test_a_selection_keeps_the_callers_order() -> None:
    """`plan_for` attributes a shared source to the first pack carrying it."""
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me = _user("me@x.io")
        db.add(me)
        await db.flush()
        db.add(KnowledgeBase(id=uuid.uuid4(), user_id=me.id, name="Corpus"))
        await db.commit()

        chosen = await resolve_selected(db, me, [KB_PACK_SLUG, "web-general"])
        assert [p.slug for p in chosen] == [KB_PACK_SLUG, "web-general"]
        chosen = await resolve_selected(db, me, ["web-general", KB_PACK_SLUG])
        assert [p.slug for p in chosen] == ["web-general", KB_PACK_SLUG]


@pytest.mark.asyncio
async def test_a_pack_that_vanished_between_picker_and_run_refuses() -> None:
    """Dropping it silently would persist coverage the run never had.

    A dynamic pack really can disappear in that window — the last corpus
    deleted, a designation withdrawn — so this is the ordinary case, not a
    corrupted request.
    """
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me = _user("me@x.io")
        db.add(me)
        await db.flush()
        await db.commit()

        with pytest.raises(UnknownSourcePackError):
            await resolve_selected(db, me, ["web-general", KB_PACK_SLUG])


@pytest.mark.asyncio
async def test_each_corpus_is_its_own_source() -> None:
    """Merged into one step, the block could not say which corpus answered.

    A run over five corpora that matched in one would read as "found
    something" with nowhere to look, and a run that matched in none would be
    indistinguishable from a run against a single empty corpus.
    """
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me = _user("me@x.io")
        db.add(me)
        await db.flush()
        for name in ("Charlie", "Alpha", "Bravo"):
            db.add(KnowledgeBase(id=uuid.uuid4(), user_id=me.id, name=name))
        await db.commit()

        pack = (await resolve_packs(db, me))[KB_PACK_SLUG]
        # Name order, so the plan is stable across runs rather than
        # following whatever the database happened to return.
        assert [s.name for s in pack.sources] == ["Alpha", "Bravo", "Charlie"]
        assert all(len(s.knowledge_base_ids) == 1 for s in pack.sources)
        # Distinct identities, or `plan_for` would collapse them into one.
        assert len({s.identity() for s in pack.sources}) == 3


@pytest.mark.asyncio
async def test_a_pack_can_afford_the_sources_it_plans() -> None:
    """A ceiling of ten with twelve corpora would plan work it cannot do."""
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me = _user("me@x.io")
        db.add(me)
        await db.flush()
        for i in range(12):
            db.add(KnowledgeBase(id=uuid.uuid4(), user_id=me.id, name=f"KB{i:02d}"))
        await db.commit()

        pack = (await resolve_packs(db, me))[KB_PACK_SLUG]
        assert len(pack.sources) == 12
        assert pack.max_iterations >= len(pack.sources)


# ── Disinformation infrastructure pack: whose VirusTotal key ────────


def _virustotal(owner: User | None, name: str, **kw) -> Integration:
    from app.core.security import encrypt_value

    return Integration(
        id=uuid.uuid4(),
        user_id=owner.id if owner else None,
        is_company_managed=kw.pop("is_company_managed", False),
        provider="virustotal",
        auth_type="api_key",
        name=name,
        status=kw.pop("status", "connected"),
        api_key_enc=kw.pop("api_key_enc", encrypt_value(f"key-of-{name}")),
        **kw,
    )


def _passive_dns_integrations(pack) -> set[str]:
    return {s.integration_id for s in pack.sources if s.kind == "passive_dns"}


@pytest.mark.asyncio
async def test_passive_dns_uses_the_callers_own_virustotal_key() -> None:
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me = _user("me@x.io")
        db.add(me)
        await db.flush()
        mine = _virustotal(me, "My VT")
        db.add(mine)
        await db.commit()

        pack = (await resolve_packs(db, me))[DISINFO_INFRASTRUCTURE_SLUG]
        assert _passive_dns_integrations(pack) == {str(mine.id)}
        assert pack.auto_select is False


@pytest.mark.asyncio
async def test_someone_elses_virustotal_key_is_never_spent() -> None:
    """An admin included: the pack is three-tier like every picker."""
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me, theirs = _user("me@x.io"), _user("theirs@x.io")
        me.role = "admin"
        db.add_all([me, theirs])
        await db.flush()
        db.add(_virustotal(theirs, "Their VT"))
        await db.commit()

        pack = (await resolve_packs(db, me))[DISINFO_INFRASTRUCTURE_SLUG]
        # Offered all the same — the fingerprint and the lookups need no key.
        assert _passive_dns_integrations(pack) == set()
        assert "Connect a VirusTotal integration" in pack.description


@pytest.mark.asyncio
async def test_a_disconnected_or_keyless_virustotal_is_not_a_source() -> None:
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me = _user("me@x.io")
        db.add(me)
        await db.flush()
        db.add_all([
            _virustotal(me, "Broken", status="error"),
            _virustotal(me, "Keyless", api_key_enc=None),
        ])
        await db.commit()

        pack = (await resolve_packs(db, me))[DISINFO_INFRASTRUCTURE_SLUG]
        assert _passive_dns_integrations(pack) == set()


@pytest.mark.asyncio
async def test_a_company_virustotal_serves_everyone() -> None:
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me = _user("me@x.io")
        db.add(me)
        await db.flush()
        company = _virustotal(None, "Company VT", is_company_managed=True)
        db.add(company)
        await db.commit()

        pack = (await resolve_packs(db, me))[DISINFO_INFRASTRUCTURE_SLUG]
        assert _passive_dns_integrations(pack) == {str(company.id)}


@pytest.mark.asyncio
async def test_the_key_is_re_checked_when_the_source_is_queried(monkeypatch) -> None:
    """A stored ref outlives the picker: what decides is visibility NOW."""
    from app.core import database
    from app.services.investigation_queriers import load_virustotal_api_key

    async with _temp_database("inv_resolver") as sessions:
        async with sessions() as db:
            me, theirs = _user("me@x.io"), _user("theirs@x.io")
            db.add_all([me, theirs])
            await db.flush()
            mine = _virustotal(me, "My VT")
            db.add(mine)
            await db.commit()

        monkeypatch.setattr(database, "async_session", sessions)
        assert await load_virustotal_api_key(str(mine.id), me.id) == "key-of-My VT"
        assert await load_virustotal_api_key(str(mine.id), theirs.id) is None
        assert await load_virustotal_api_key("not-a-uuid", me.id) is None

        async with sessions() as db:
            row = await db.get(Integration, mine.id)
            row.status = "disconnected"
            await db.commit()
        assert await load_virustotal_api_key(str(mine.id), me.id) is None


# ── Knowledge bases a pack declares ─────────────────────────────────


def _doctrine(owner: User | None, name: str = "VIGINUM FIMI Doctrine", **kw) -> KnowledgeBase:
    return KnowledgeBase(
        id=uuid.uuid4(),
        user_id=owner.id if owner else None,
        is_company_managed=kw.pop("is_company_managed", False),
        name=name,
        tags=kw.pop("tags", ["builtin", "fimi", "slug:viginum-fimi-doctrine"]),
        **kw,
    )


def _declared(pack) -> list[str]:
    return [s.name for s in pack.sources if s.kind == "knowledge_base"]


@pytest.mark.asyncio
async def test_a_pack_reads_the_corpus_it_declares_by_slug() -> None:
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me = _user("me@x.io")
        db.add(me)
        await db.flush()
        db.add_all([_doctrine(None, is_company_managed=True), _doctrine(me, "Unrelated", tags=["fimi"])])
        await db.commit()

        pack = (await resolve_packs(db, me))[DISINFO_INFRASTRUCTURE_SLUG]
        assert _declared(pack) == ["VIGINUM FIMI Doctrine"]
        source = next(s for s in pack.sources if s.kind == "knowledge_base")
        assert source.min_similarity is not None and "{value}" in source.query_template
        assert "Not available to you" not in pack.description


@pytest.mark.asyncio
async def test_a_declared_corpus_someone_else_owns_is_never_claimed() -> None:
    """Named by the pack, private to a colleague: absent, and said."""
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me, theirs = _user("me@x.io"), _user("theirs@x.io")
        db.add_all([me, theirs])
        await db.flush()
        db.add(_doctrine(theirs))
        await db.commit()

        pack = (await resolve_packs(db, me))[DISINFO_INFRASTRUCTURE_SLUG]
        assert _declared(pack) == []
        assert "Not available to you" in pack.description

        # Not the generic pack's business either: it lists what I can see.
        assert KB_PACK_SLUG not in await resolve_packs(db, me)

        pack = (await resolve_packs(db, theirs))[DISINFO_INFRASTRUCTURE_SLUG]
        assert _declared(pack) == ["VIGINUM FIMI Doctrine"]


@pytest.mark.asyncio
async def test_a_selection_resolves_the_declared_corpus_for_the_caller() -> None:
    """The picker's offer and the run's plan come from one resolution."""
    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me = _user("me@x.io")
        db.add(me)
        await db.flush()
        db.add(_doctrine(me))
        await db.commit()

        (pack,) = await resolve_selected(db, me, [DISINFO_INFRASTRUCTURE_SLUG])
        names = [s.name for s in pack.sources_for("unknown")]
        assert names == ["VIGINUM FIMI Doctrine"]


# ── OpenCTI: the registered integration only, read as the caller ───


def _opencti(owner: User | None, name: str, **kw) -> Integration:
    return Integration(
        id=uuid.uuid4(),
        user_id=owner.id if owner else None,
        is_company_managed=kw.pop("is_company_managed", owner is None),
        provider="opencti",
        auth_type=kw.pop("auth_type", "platform_managed"),
        name=name,
        status=kw.pop("status", "connected"),
        **kw,
    )


def _opencti_sources(pack) -> list[str]:
    return [s.name for s in pack.sources if s.kind == "opencti"]


@pytest.mark.asyncio
async def test_only_the_registered_opencti_is_read_and_as_the_caller(monkeypatch) -> None:
    """An ``api_key`` OpenCTI would read as its key's owner: never a source."""
    from app.core import database
    from app.services import integration_tools
    from app.services.investigation_pack_resolver import visible_opencti_integration
    from app.services.investigation_queriers import load_opencti_access

    async with _temp_database("inv_resolver") as sessions:
        async with sessions() as db:
            me = _user("me@x.io")
            db.add(me)
            await db.flush()
            db.add_all([
                _opencti(None, "A service token", auth_type="api_key"),
                _opencti(me, "B my own", is_company_managed=False),
                _opencti(None, "C disconnected", status="disconnected"),
            ])
            await db.commit()
            assert await visible_opencti_integration(db, me) is None
            assert _opencti_sources((await resolve_packs(db, me))[DISINFO_INFRASTRUCTURE_SLUG]) == []

            registered = _opencti(None, "D registered")
            db.add(registered)
            await db.commit()
            assert await visible_opencti_integration(db, me) == (str(registered.id), "D registered")
            assert _opencti_sources((await resolve_packs(db, me))[DISINFO_INFRASTRUCTURE_SLUG]) != []

        minted: list[tuple[str, str | None]] = []

        async def _mint(_db, integration_id, _provider="", *, user_id=None, **_kw):
            minted.append((integration_id, user_id))
            return "http://opencti:4000||jwt-for-the-caller"

        monkeypatch.setattr(database, "async_session", sessions)
        monkeypatch.setattr(integration_tools, "_get_token", _mint)
        access = await load_opencti_access(me.id)
        assert access is not None and access.headers["Authorization"] == "Bearer jwt-for-the-caller"
        assert minted == [(str(registered.id), str(me.id))]

        async with sessions() as db:
            row = await db.get(Integration, registered.id)
            row.status = "disconnected"
            await db.commit()
        assert await load_opencti_access(me.id) is None, "re-checked when the step queries it"
        assert minted == [(str(registered.id), str(me.id))]


@pytest.mark.asyncio
async def test_a_restricted_opencti_serves_its_grantees_only() -> None:
    from app.services.investigation_pack_resolver import visible_opencti_integration

    async with _temp_database("inv_resolver") as sessions, sessions() as db:
        me, granted = _user("me@x.io"), _user("granted@x.io")
        db.add_all([me, granted])
        await db.flush()
        restricted = _opencti(
            None,
            "Restricted",
            access_restricted=True,
            shared_user_ids=[str(granted.id)],
            shared_user_permissions={str(granted.id): "read_only"},
        )
        db.add(restricted)
        await db.commit()

        assert await visible_opencti_integration(db, me) is None
        assert await visible_opencti_integration(db, granted) == (str(restricted.id), "Restricted")

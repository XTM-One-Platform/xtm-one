"""Continuing past a spent budget, against real PostgreSQL.

The pure half of this feature is tested next door. What needs real rows is
everything that makes it *safe*: a chain's cumulative spend, the cap that
actually binds it, and re-authorisation of a stored source address against
what the caller may query now rather than what they could when the plan was
written. A stubbed session would assert only that this module builds the
queries it builds.

The querier is injected and offline. That is deliberate and it is the same
reason the orchestrator takes one: the properties under test are about state,
and a live network would make them flaky without covering a single extra
branch.

``integration``-marked and skipped when PostgreSQL is unreachable, like its
neighbours.
"""

from __future__ import annotations

import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.database import Base
from app.models.investigation import Investigation, InvestigationStep
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.services.investigation_continuation import (
    CHAIN_MAX_ITERATIONS,
    MAX_CHAIN_DEPTH,
    build_offer,
    chain_of,
    resolve_selected,
)
from app.services.investigation_orchestrator import (
    DeepInvestigationOrchestrator,
    SourceOutcome,
)
from app.services.investigation_packs import SourceDefinition, SourcePack

pytestmark = pytest.mark.integration

SUBJECT = "185.220.101.47"


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
        if getattr(getattr(exc, "orig", None), "pgcode", None) == "42501":
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


def _user(email: str = "analyst@soc.test") -> User:
    # `role`, not `is_admin`: the latter is a read-only property over it.
    return User(
        id=uuid.uuid4(), email=email, hashed_password="x", full_name=email, role="user"
    )


def _http(name: str) -> SourceDefinition:
    return SourceDefinition(
        name=name,
        subject_types=("ip",),
        url_template=f"https://{name.lower().replace(' ', '-')}.test/{{value}}",
    )


#: The scenario, and it is the one from the brief: an IP that hit the proxy,
#: checked against external reputation AND internal telemetry. Two packs
#: because that split is what makes a truncated plan *misleading* rather than
#: merely incomplete — three reputation hits with no telemetry consulted reads
#: as a verdict when it is half a verdict.
REPUTATION = ("VirusTotal", "AbuseIPDB", "URLScan", "AlienVault OTX")
TELEMETRY = ("Microsoft Sentinel", "Google SecOps", "CrowdStrike", "Elasticsearch")


def _packs() -> list[SourcePack]:
    return [
        SourcePack(
            slug="reputation",
            label="External reputation",
            description="Public reputation and sandbox sources.",
            subject_types=("ip",),
            sources=tuple(_http(n) for n in REPUTATION),
            max_iterations=10,
        ),
        SourcePack(
            slug="telemetry",
            label="Internal telemetry",
            description="What our own estate saw.",
            subject_types=("ip",),
            sources=tuple(_http(n) for n in TELEMETRY),
            max_iterations=10,
        ),
    ]


#: Packs a test has declared queryable, keyed by slug.
#:
#: ``build_offer`` re-authorises every stored source against
#: ``resolve_packs`` — the same function the picker offers from — so a source
#: belonging to no registered pack is withheld. That is the feature working,
#: and it is why the synthetic packs here have to be registered for the tests
#: that are about caps and chains rather than about permission.
#:
#: It also records a real production consequence: a source whose pack has been
#: withdrawn from the registry stops being continuable, by design, and the
#: remainder says so by name instead of querying an address nothing offers.
_REGISTERED: dict[str, SourcePack] = {}


@pytest.fixture(autouse=True)
def _pack_registry():
    """Answer re-authorisation from ``_REGISTERED``, or really resolve.

    Falling back to the real resolver when nothing is registered is what lets
    one test in this file exercise authorisation for real (it runs with
    ``register=False`` and builds an actual knowledge base) while the rest take
    it as given. An unconditional patch would have made the permission test
    silently vacuous — it would have asserted the stub, not the rule.
    """
    from unittest.mock import patch

    from app.services import investigation_pack_resolver as resolver

    real = resolver.resolve_packs

    async def _resolve(db, user):
        if _REGISTERED:
            return dict(_REGISTERED)
        return await real(db, user)

    _REGISTERED.clear()
    with patch.object(resolver, "resolve_packs", new=_resolve):
        yield
    _REGISTERED.clear()


def _querier(findings_by_name: dict[str, int] | None = None):
    """Offline querier: every source answers, some with findings."""
    findings = findings_by_name or {}

    async def query(source, subject_value, query_text):
        return SourceOutcome(findings_count=findings.get(source.name, 1))

    return query


async def _run(db, *, packs, budget, user, parent=None, register=True):
    if register:
        _REGISTERED.update({pack.slug: pack for pack in packs})
    orchestrator = DeepInvestigationOrchestrator(db, _querier())
    return await orchestrator.run(
        subject_value=SUBJECT,
        subject_type="ip",
        packs=packs,
        max_iterations=budget,
        user_id=user.id,
        continues_investigation_id=parent.id if parent else None,
    )


# ── The address is persisted, which is what makes any of this possible ──────


@pytest.mark.asyncio
async def test_every_planned_step_stores_how_to_reach_its_source() -> None:
    """Without this the remainder is names, and a name is not an address.

    Asserted over a truncated plan on purpose: the steps that matter are the
    ones the budget never reached, and they are exactly the ones no querier
    ever touched — so if the address were written at query time rather than at
    plan time, this is where it would be missing.
    """
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()

        result = await _run(db, packs=_packs(), budget=3, user=user)

        assert len(result.steps) == 8
        for step in result.steps:
            assert step.source_ref, step.source_name
            rebuilt = SourceDefinition.from_ref(
                step.source_ref, subject_types=("ip",)
            )
            assert rebuilt.name == step.source_name
        # And the unreached ones are terminal-skipped, not left pending —
        # which is why a continuation has to be a new run.
        unreached = [s for s in result.steps if s.status == "skipped"]
        assert len(unreached) == 5
        assert {s.detail_code for s in unreached} == {"run.budget_spent"}


# ── The offer ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_offer_names_the_half_of_the_plan_nobody_reached() -> None:
    """The enriched block's whole point: what is missing, and why it matters.

    Budget 3 against a plan of 8 whose first four are external reputation. So
    the run ends having consulted reputation only — and ``packs_untouched``
    saying ``telemetry`` is the difference between "3 of 8 sources" (a tally
    an analyst cannot act on) and "your own estate was never asked".
    """
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        result = await _run(db, packs=_packs(), budget=3, user=user)

        offer = await build_offer(db, result.investigation, user)

        assert offer.available is True
        assert offer.spent == 3
        assert offer.chain_depth == 1
        assert offer.remaining_allowance == CHAIN_MAX_ITERATIONS - 3
        assert len(offer.steps) == 5
        assert offer.coverage.packs_reached == ("reputation",)
        assert offer.coverage.packs_untouched == ("telemetry",)
        # Offered in the order the analyst already saw them.
        assert [s.position for s in offer.steps] == sorted(
            s.position for s in offer.steps
        )
        assert offer.withheld == ()


@pytest.mark.asyncio
async def test_a_run_that_queried_everything_offers_nothing() -> None:
    """No remainder, and the block must say so rather than invite a spend."""
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        result = await _run(db, packs=_packs(), budget=20, user=user)

        offer = await build_offer(db, result.investigation, user)

        assert result.investigation.end_reason_code == "run.all_sources_queried"
        assert offer.available is False
        assert offer.blocked_reason == "nothing_left"
        assert offer.coverage.unqueried == 0
        assert offer.coverage.packs_untouched == ()


@pytest.mark.asyncio
async def test_every_pack_in_an_offer_can_be_named() -> None:
    """A slug shown to an analyst must arrive with a label.

    Regression guard on a defect that reached a screenshot. The panel kept its
    own slug -> label table, and it drifted the moment a second built-in pack
    existed: the assessment line read "No source from Knowledge bases,
    web-research was reached" — one pack named, one leaking its raw slug,
    side by side in the same sentence.

    Labels now travel on the offer, from the same resolution that decides
    which packs the caller may query at all, so a pack can never be offered
    under one name and labelled under another. Asserted over every slug the
    payload mentions — the steps AND the coverage — because the leak was in
    the coverage line, not in the list.
    """
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        result = await _run(db, packs=_packs(), budget=3, user=user)

        offer = await build_offer(db, result.investigation, user)

        mentioned = {s.source_pack_slug for s in offer.steps if s.source_pack_slug}
        mentioned |= set(offer.coverage.packs_untouched)
        mentioned |= set(offer.coverage.packs_reached)
        assert mentioned, "the scenario must mention at least one pack"

        unnamed = sorted(slug for slug in mentioned if slug not in offer.pack_labels)
        assert not unnamed, f"packs offered with no label: {unnamed}"
        for slug in mentioned:
            assert offer.pack_labels[slug].strip(), slug


# ── Re-authorisation: the ref is a pointer, never a grant ───────────────────


@pytest.mark.asyncio
async def test_a_corpus_the_caller_lost_access_to_is_withheld_and_named() -> None:
    """The security property, and the honesty one, in the same assertion.

    A knowledge base that was visible when the plan was written and is not
    now must not be queried off the stored reference — and it must not
    silently vanish from the remainder either, because a shrinking remainder
    is the coverage gap this whole design exists to prevent. So: withheld,
    with a reason, by name.
    """
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user, other = _user(), _user("someone.else@soc.test")
        db.add_all([user, other])
        await db.flush()

        kb = KnowledgeBase(id=uuid.uuid4(), user_id=user.id, name="Threat corpus")
        db.add(kb)
        await db.flush()

        corpus_pack = SourcePack(
            slug="knowledge-bases",
            label="Knowledge bases",
            description="",
            subject_types=("ip",),
            sources=(
                _http("VirusTotal"),
                SourceDefinition(
                    name="Threat corpus",
                    subject_types=("ip",),
                    kind="knowledge_base",
                    knowledge_base_ids=(str(kb.id),),
                ),
            ),
            max_iterations=10,
        )
        # `register=False`: this is the one test that must hit the REAL
        # resolver, because what it asserts IS the resolver's answer changing.
        result = await _run(
            db, packs=[corpus_pack], budget=1, user=user, register=False
        )

        # Visible when planned: it is offered.
        offer = await build_offer(db, result.investigation, user)
        assert [s.source_name for s in offer.steps] == ["Threat corpus"]

        # Ownership moves away. The stored ref is unchanged and still names it.
        kb.user_id = other.id
        await db.flush()

        after = await build_offer(db, result.investigation, user)
        assert after.steps == ()
        assert after.withheld == (("Threat corpus", "no_longer_permitted"),)
        assert after.blocked_reason == "nothing_addressable"


@pytest.mark.asyncio
async def test_handing_the_orchestrator_a_corpus_you_cannot_see_is_refused() -> None:
    """`run(packs=...)` takes a fully-formed source list and used to query it
    with no authorisation of its own — the guarantee lived in a comment naming
    its one well-behaved caller. The next caller would not read that comment,
    so the boundary refuses instead.

    An `http` source is deliberately NOT refused here: it carries no per-user
    access to re-check, it is bounded by the SSRF policy every fetch runs, and
    it is what a followed lead is.
    """
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        caller, owner = _user(), _user("someone.else@soc.test")
        db.add_all([caller, owner])
        await db.flush()

        # A corpus the CALLER cannot see.
        kb = KnowledgeBase(id=uuid.uuid4(), user_id=owner.id, name="Someone else's corpus")
        db.add(kb)
        await db.flush()

        smuggled = SourcePack(
            slug="hand-built",
            label="Hand built",
            description="",
            subject_types=("ip",),
            sources=(
                SourceDefinition(
                    name="Someone else's corpus",
                    subject_types=("ip",),
                    kind="knowledge_base",
                    knowledge_base_ids=(str(kb.id),),
                ),
            ),
            max_iterations=10,
        )

        with pytest.raises(ValueError, match="not permitted for this caller"):
            await _run(db, packs=[smuggled], budget=1, user=caller, register=False)

        # The same call from the owner is fine — this is a visibility check,
        # not a ban on hand-supplied packs.
        await db.rollback()
        owner2 = _user("someone.else@soc.test")
        db.add(owner2)
        await db.flush()
        kb2 = KnowledgeBase(id=uuid.uuid4(), user_id=owner2.id, name="Own corpus")
        db.add(kb2)
        await db.flush()
        mine = SourcePack(
            slug="hand-built",
            label="Hand built",
            description="",
            subject_types=("ip",),
            sources=(
                SourceDefinition(
                    name="Own corpus",
                    subject_types=("ip",),
                    kind="knowledge_base",
                    knowledge_base_ids=(str(kb2.id),),
                ),
            ),
            max_iterations=10,
        )
        result = await _run(db, packs=[mine], budget=1, user=owner2, register=False)
        assert result.investigation is not None


@pytest.mark.asyncio
async def test_a_declared_corpus_step_is_offered_and_continued_for_its_caller() -> None:
    """A pack's declared corpus carries its question and floor in its ref.

    The guard compares a stored step's identity with what the caller's packs
    resolve to now (`resolve_packs`), so the identity the doctrine step stores
    - query template and floor included - must be the one the disinfo pack
    expands to for this caller, or every continuation of it is refused.
    """
    from app.services.investigation_pack_resolver import resolve_selected as resolve_packs_selected
    from app.services.investigation_packs import DISINFO_INFRASTRUCTURE_SLUG

    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        db.add(
            KnowledgeBase(
                id=uuid.uuid4(),
                user_id=None,
                is_company_managed=True,
                name="VIGINUM FIMI Doctrine",
                tags=["builtin", "fimi", "slug:viginum-fimi-doctrine"],
            )
        )
        await db.flush()

        (pack,) = await resolve_packs_selected(db, user, [DISINFO_INFRASTRUCTURE_SLUG])
        doctrine = next(s for s in pack.sources if s.kind == "knowledge_base")
        assert doctrine.query_template and doctrine.min_similarity is not None

        orchestrator = DeepInvestigationOrchestrator(db, _querier())
        parent = (
            await orchestrator.run(
                subject_value="yerevandaily.am",
                subject_type="domain",
                packs=[pack],
                max_iterations=1,
                user_id=user.id,
            )
        ).investigation

        offer = await build_offer(db, parent, user)
        offered = [s for s in offer.steps if s.source_name == doctrine.name]
        assert len(offered) == 1
        assert [name for name, _reason in offer.withheld if name == doctrine.name] == []

        continued, _offer, rejected = await resolve_selected(db, parent, user, [offered[0].step_id])
        assert rejected == []
        assert [s.identity() for s in continued.sources] == [doctrine.identity()]

        child = (
            await orchestrator.run(
                subject_value="yerevandaily.am",
                subject_type="domain",
                packs=[continued],
                max_iterations=None,
                user_id=user.id,
                continues_investigation_id=parent.id,
            )
        ).investigation
        assert child.continues_investigation_id == parent.id


@pytest.mark.asyncio
async def test_a_hop_the_budget_never_reached_is_continued_on_its_own_subject() -> None:
    """A hop's ref carries the value it queries (the pivot's IP). The guard
    re-authorises the PLACE (the caller's VirusTotal key) and keeps the value,
    so the continued step asks VirusTotal about that IP, not the run's seed."""
    from app.core.security import encrypt_value
    from app.models.integration import Integration
    from app.services.investigation_pack_resolver import resolve_selected as resolve_packs_selected
    from app.services.investigation_packs import DISINFO_INFRASTRUCTURE_SLUG, pivot_leads

    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        db.add(
            Integration(
                id=uuid.uuid4(),
                user_id=None,
                is_company_managed=True,
                provider="virustotal",
                auth_type="api_key",
                name="Company VT",
                status="connected",
                api_key_enc=encrypt_value("vt-key"),
            )
        )
        await db.flush()
        (pack,) = await resolve_packs_selected(db, user, [DISINFO_INFRASTRUCTURE_SLUG])
        passive = next(s for s in pack.sources if s.kind == "passive_dns" and not s.hostname_keywords)
        hops = pivot_leads(passive, "185.220.101.47")
        asked: list[tuple[str, str]] = []

        async def _query(source, subject_value, _query_text):
            asked.append((source.name, subject_value))
            return SourceOutcome(findings_count=1, leads=hops if len(asked) == 1 else ())

        orchestrator = DeepInvestigationOrchestrator(db, _query)
        parent = (
            await orchestrator.run(
                subject_value="yerevandaily.am", subject_type="domain", packs=[pack], max_iterations=1, user_id=user.id
            )
        ).investigation

        offer = await build_offer(db, parent, user)
        co_hosted = [s for s in offer.steps if s.source_name == "VirusTotal co-hosted news domains"]
        assert len(co_hosted) == 1
        continued, _offer, rejected = await resolve_selected(db, parent, user, [co_hosted[0].step_id])
        assert rejected == []
        (source,) = continued.sources
        assert source.subject == "185.220.101.47"

        asked.clear()
        await orchestrator.run(
            subject_value="yerevandaily.am",
            subject_type="domain",
            packs=[continued],
            max_iterations=None,
            user_id=user.id,
            continues_investigation_id=parent.id,
        )
        assert asked == [("VirusTotal co-hosted news domains", "185.220.101.47")]


@pytest.mark.asyncio
async def test_a_step_written_before_the_column_existed_is_named_not_dropped() -> None:
    """Rows predating ``source_ref`` have none, and that is not an error.

    They cannot be continued — there is no address — but they are the sources
    an analyst is most likely to be looking for, so the block says "these five
    are left and this one cannot be reached" rather than quietly offering four.
    """
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        result = await _run(db, packs=_packs(), budget=3, user=user)

        # Simulate the pre-migration shape on one unreached step.
        orphan = next(s for s in result.steps if s.status == "skipped")
        orphan.source_ref = None
        await db.flush()

        offer = await build_offer(db, result.investigation, user)

        assert len(offer.steps) == 4
        assert offer.withheld == ((orphan.source_name, "no_address"),)


@pytest.mark.asyncio
async def test_a_lead_the_budget_never_reached_can_be_continued() -> None:
    """The canonical remainder, and the one that was permanently unreachable.

    `plan_steps` stored `source_ref`; `append_lead_step` did not, so every
    multi-hop lead came back `no_address` — which the panel renders as
    "recorded before continuations existed", about a row written minutes
    earlier by the build reading it. Leads are what a `budget_spent` run most
    characteristically leaves behind, so the feature failed exactly where it
    was most needed.
    """
    lead = _http("Passive DNS Pivot")

    async def query_with_a_lead(source, subject_value, query_text):
        if source.name == "Seed":
            return SourceOutcome(findings_count=1, leads=(lead,))
        return SourceOutcome(findings_count=1)

    pack = SourcePack(
        slug="reputation",
        label="External reputation",
        description="Public reputation sources.",
        subject_types=("ip",),
        sources=(_http("Seed"),),
        max_iterations=10,
    )
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        _REGISTERED.update({pack.slug: pack})

        orchestrator = DeepInvestigationOrchestrator(db, query_with_a_lead)
        result = await orchestrator.run(
            subject_value=SUBJECT,
            subject_type="ip",
            packs=[pack],
            max_iterations=1,          # the seed runs; the lead never does
            user_id=user.id,
        )

        lead_step = next(s for s in result.steps if s.source_name == lead.name)
        assert lead_step.status in ("pending", "skipped")
        assert lead_step.source_ref is not None, "the lead was stored with no address"

        offer = await build_offer(db, result.investigation, user)
        offered = [s.source_name for s in offer.steps]
        assert lead.name in offered, (
            f"the lead was not offered; withheld={offer.withheld}"
        )


# ── Continuing, and the cap that binds it ───────────────────────────────────


@pytest.mark.asyncio
async def test_the_selection_becomes_a_linked_child_run() -> None:
    """The DoD path, end to end.

    An analyst sees three of eight queried, picks the four telemetry sources,
    and gets a NEW run that carries them — linked to the parent, so the chain
    can be summed and the parent's report stays exactly as they read it.
    """
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        parent = (await _run(db, packs=_packs(), budget=3, user=user)).investigation

        offer = await build_offer(db, parent, user)
        telemetry = [s for s in offer.steps if s.source_pack_slug == "telemetry"]
        assert len(telemetry) == 4

        pack, _offer, rejected = await resolve_selected(
            db, parent, user, [s.step_id for s in telemetry]
        )
        assert rejected == []
        assert [s.name for s in pack.sources] == list(TELEMETRY)

        child = (
            await _run(db, packs=[pack], budget=None, user=user, parent=parent)
        ).investigation

        assert child.id != parent.id
        assert child.continues_investigation_id == parent.id
        assert child.source_pack_slugs == ["continuation"]

        # The parent is untouched: its steps, and therefore its report's source
        # numbering, are exactly what the analyst already read.
        refreshed = list(
            (
                await db.execute(
                    select(InvestigationStep).where(
                        InvestigationStep.investigation_id == parent.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert sum(1 for s in refreshed if s.status == "skipped") == 5

        # The chain now knows what it has spent, across both runs.
        chained = await chain_of(db, child)
        assert [inv.id for inv in chained] == [parent.id, child.id]
        child_offer = await build_offer(db, child, user)
        assert child_offer.spent == 7  # 3 in the parent + 4 here
        assert child_offer.chain_depth == 2


@pytest.mark.asyncio
async def test_a_chain_with_a_run_in_flight_offers_nothing() -> None:
    """A continuation still running has pending steps, which are not a
    remainder: offering them again (from the parent, or a second tab) would
    pay for the same sources twice."""
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        parent = (await _run(db, packs=_packs(), budget=3, user=user)).investigation
        offer = await build_offer(db, parent, user)
        telemetry = [s.step_id for s in offer.steps if s.source_pack_slug == "telemetry"]
        pack, _offer, _rejected = await resolve_selected(db, parent, user, telemetry)
        child = (
            await _run(db, packs=[pack], budget=None, user=user, parent=parent)
        ).investigation
        child.status = "running"
        await db.flush()

        offer = await build_offer(db, parent, user)
        assert offer.in_flight
        assert offer.steps == ()
        assert offer.blocked_reason == "in_flight"
        with pytest.raises(ValueError, match="in_flight"):
            await resolve_selected(db, parent, user, telemetry)


@pytest.mark.asyncio
async def test_continuing_with_some_does_not_retire_the_rest() -> None:
    """The remainder belongs to the CHAIN, not to the latest run.

    This is a regression guard on a real defect, found by the depth-cap test
    rather than by reading: ``build_offer`` originally looked only at the run
    it was given. So an analyst with seven sources left who continued with two
    got a child run whose offer listed the ONE source that child had not
    reached — and the other five were gone from the block while still sitting
    unqueried in the parent's rows.

    A remainder that shrinks when you spend on part of it is precisely the
    coverage gap this feature exists to close, and it would have been
    invisible in a screenshot: the block looked right, it was just quietly
    smaller.
    """
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        parent = (await _run(db, packs=_packs(), budget=3, user=user)).investigation

        offer = await build_offer(db, parent, user)
        assert len(offer.steps) == 5

        # Spend on two of the five.
        picked = [s.step_id for s in offer.steps[:2]]
        pack, _o, _r = await resolve_selected(db, parent, user, picked)
        child = (
            await _run(db, packs=[pack], budget=None, user=user, parent=parent)
        ).investigation

        after = await build_offer(db, child, user)

        # The three nobody has touched are still on offer...
        assert len(after.steps) == 3
        # ...and the two just queried are NOT offered again, from either row.
        names = {s.source_name for s in after.steps}
        assert names.isdisjoint({s.source_name for s in offer.steps[:2]})
        # The chain's spend is what the cap is measured against.
        assert after.spent == 5
        assert after.chain_depth == 2


@pytest.mark.asyncio
async def test_a_parents_offer_accounts_for_what_its_children_spent() -> None:
    """The cap is only a cap if it sees the whole family.

    Regression guard on the defect that made this feature's central promise
    fail in the running app. ``chain_of`` walked PARENTS only, so a parent's
    block reported its own run's spend and kept offering the sources its
    children had already queried. Observed on the dev stack: one parent, four
    continuations, 43 queries spent against a 40-query ceiling — and the
    parent still reading "2 of 40 used" with all thirteen sources on offer.
    Every click paid for the same sources again.

    Two assertions, because the single walk broke two things: the spend the
    cap is measured against, and the remainder that decides what gets paid
    for. A fix that only summed the spend would still have double-charged.
    """
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        parent = (await _run(db, packs=_packs(), budget=2, user=user)).investigation

        offer = await build_offer(db, parent, user)
        assert offer.spent == 2
        first_two = [s.step_id for s in offer.steps[:2]]
        names_taken = {s.source_name for s in offer.steps[:2]}

        pack, _o, _r = await resolve_selected(db, parent, user, first_two)
        await _run(db, packs=[pack], budget=None, user=user, parent=parent)

        # A SECOND continuation off the same parent — which is what an analyst
        # clicking the parent's block twice actually produces.
        again = await build_offer(db, parent, user)

        # The spend the cap sees now includes the child's queries...
        assert again.spent == 4, "a parent must be charged for its children"
        assert again.chain_depth == 2
        # ...and the sources that child queried are gone from the remainder,
        # so a second click cannot pay for them twice.
        assert names_taken.isdisjoint({s.source_name for s in again.steps})
        assert len(again.steps) == len(offer.steps) - 2


@pytest.mark.asyncio
async def test_a_stale_selection_is_reported_not_raised() -> None:
    """"These four ran, that one did not, here is why" beats running nothing.

    A step can stop being offerable between rendering the block and clicking
    it. Refusing the whole request would make the analyst re-pick a selection
    that was almost entirely valid.
    """
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        parent = (await _run(db, packs=_packs(), budget=3, user=user)).investigation

        offer = await build_offer(db, parent, user)
        ghost = uuid.uuid4()
        pack, _o, rejected = await resolve_selected(
            db, parent, user, [offer.steps[0].step_id, ghost]
        )

        assert len(pack.sources) == 1
        assert rejected == [ghost]


@pytest.mark.asyncio
async def test_the_cumulative_cap_closes_the_offer_however_it_was_reached() -> None:
    """A budget you can always click past is a prompt, not a budget.

    The per-run allowance is the checkpoint interval; THIS is the limit. Proven
    by spending the chain's whole ceiling in one run and then asking: the
    remainder is still there, still addressable, and still refused.
    """
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()

        # One pack wide enough to exhaust the ceiling, with sources left over.
        wide = SourcePack(
            slug="reputation",
            label="Wide",
            description="",
            subject_types=("ip",),
            sources=tuple(_http(f"Source {i:02d}") for i in range(CHAIN_MAX_ITERATIONS + 5)),
            max_iterations=CHAIN_MAX_ITERATIONS + 5,
        )
        result = await _run(db, packs=[wide], budget=CHAIN_MAX_ITERATIONS, user=user)

        offer = await build_offer(db, result.investigation, user)

        assert offer.spent == CHAIN_MAX_ITERATIONS
        assert offer.remaining_allowance == 0
        assert len(offer.steps) == 5  # addressable, and still refused
        assert offer.available is False
        assert offer.blocked_reason == "chain_budget"

        with pytest.raises(ValueError, match="chain_budget"):
            await resolve_selected(
                db, result.investigation, user, [offer.steps[0].step_id]
            )


@pytest.mark.asyncio
async def test_the_depth_cap_stops_a_chain_of_cheap_continuations() -> None:
    """The spend cap alone is not enough.

    A chain of one-query continuations stays under the ceiling for ever while
    still turning the block into an endless prompt — and each link also costs a
    report generation the spend cap does not count. So depth is capped too, and
    reported as its own reason rather than as a spent budget, because "you have
    continued this enough times" is a different fact from "you have spent
    enough".
    """
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()

        pack = SourcePack(
            slug="reputation",
            label="Trickle",
            description="",
            subject_types=("ip",),
            sources=tuple(_http(f"Source {i:02d}") for i in range(MAX_CHAIN_DEPTH + 4)),
            max_iterations=MAX_CHAIN_DEPTH + 4,
        )
        current = (await _run(db, packs=[pack], budget=1, user=user)).investigation

        # TWO sources ticked per link, ONE query allowed. That is what keeps
        # each child continuable in turn: a child that queried everything it
        # was given would end `run.all_sources_queried` and the chain would
        # stop on its own, testing nothing about the depth cap. Trickling like
        # this is also the shape an actual runaway takes — each step cheap,
        # the chain unbounded — which is the thing the cap exists to stop.
        for _ in range(MAX_CHAIN_DEPTH + 2):
            offer = await build_offer(db, current, user)
            if offer.blocked_reason is not None:
                break
            selected, _o, _r = await resolve_selected(
                db, current, user, [s.step_id for s in offer.steps[:2]]
            )
            current = (
                await _run(
                    db, packs=[selected], budget=1, user=user, parent=current
                )
            ).investigation

        final = await build_offer(db, current, user)
        assert final.chain_depth == MAX_CHAIN_DEPTH + 1
        assert final.remaining_allowance == 0
        assert final.available is False
        assert final.blocked_reason == "chain_depth"
        # Spend never came close — which is precisely why depth had to exist.
        assert final.spent < CHAIN_MAX_ITERATIONS


@pytest.mark.asyncio
async def test_losing_the_parent_costs_the_child_only_the_link() -> None:
    """``ON DELETE SET NULL``, and what it means for the chain.

    A child run's findings and report stand on their own, so deleting the
    parent must not cascade them away. The chain we can then prove is shorter,
    which makes the spend we charge for smaller — deliberately the
    conservative direction: we under-charge rather than refuse a continuation
    on evidence we no longer hold.
    """
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        parent = (await _run(db, packs=_packs(), budget=3, user=user)).investigation

        offer = await build_offer(db, parent, user)
        pack, _o, _r = await resolve_selected(
            db, parent, user, [offer.steps[0].step_id]
        )
        child = (
            await _run(db, packs=[pack], budget=None, user=user, parent=parent)
        ).investigation
        assert child.continues_investigation_id == parent.id

        # Ids captured as plain values BEFORE the expiry: reading them off an
        # expired instance afterwards would trigger a lazy refresh, and a lazy
        # refresh outside the greenlet context is a MissingGreenlet rather than
        # the assertion this test is making.
        child_id, parent_id = child.id, parent.id
        await db.execute(
            text("DELETE FROM investigations WHERE id = :i"), {"i": parent_id}
        )
        await db.flush()
        db.expire_all()

        surviving = (
            await db.execute(select(Investigation).where(Investigation.id == child_id))
        ).scalar_one()
        assert surviving.continues_investigation_id is None
        steps = list(
            (
                await db.execute(
                    select(InvestigationStep).where(
                        InvestigationStep.investigation_id == child_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert steps, "the child's own steps must survive its parent"


@pytest.mark.asyncio
async def test_a_list_run_is_continued_as_a_list_not_as_its_summary(monkeypatch) -> None:
    """A list run is stored under its summary ("2 domains: a, b"). Continuing
    it names the parent's type: the continued step reads the seeds it carries,
    and the child run is a `domain_list` run, not an `unknown` one that would
    plan none of the list's steps."""
    from app.core.security import encrypt_value
    from app.models.integration import Integration
    from app.services import deep_investigation_chat as dic
    from app.services.investigation_pack_resolver import resolve_selected as resolve_packs_selected
    from app.services.investigation_packs import DISINFO_INFRASTRUCTURE_SLUG, list_summary, pivot_leads

    seeds = ("armdailynews.com", "yerevantimes.am")
    async with _temp_database("inv_cont") as sessions, sessions() as db:
        user = _user()
        db.add(user)
        await db.flush()
        db.add(
            Integration(
                id=uuid.uuid4(),
                user_id=None,
                is_company_managed=True,
                provider="virustotal",
                auth_type="api_key",
                name="Company VT",
                status="connected",
                api_key_enc=encrypt_value("vt-key"),
            )
        )
        await db.flush()
        (pack,) = await resolve_packs_selected(db, user, [DISINFO_INFRASTRUCTURE_SLUG])
        asked: list[tuple[str, tuple]] = []
        in_parent = [True]

        async def _query(source, _subject_value, _query_text):
            asked.append((source.name, source.seed_values))
            # The seeds' shared IP is the hop a list run's budget leaves
            # behind: the step a continuation of a stopped list offers.
            leads = pivot_leads(source, "91.239.146.131") if in_parent[0] and len(asked) == 1 else ()
            return SourceOutcome(findings_count=1, leads=leads)

        parent = (
            await DeepInvestigationOrchestrator(db, _query).run(
                subject_value="\n".join(seeds), packs=[pack], max_iterations=1, user_id=user.id
            )
        ).investigation
        assert (parent.subject_type, parent.subject_value) == ("domain_list", list_summary(seeds))

        offer = await build_offer(db, parent, user)
        assert len(offer.steps) >= 1
        continued, _offer, rejected = await resolve_selected(db, parent, user, [offer.steps[0].step_id])
        assert rejected == []

        async def _write(_db, _result, **_kw):
            return None

        monkeypatch.setattr(dic, "make_dispatching_querier", lambda *_a, **_k: _query)
        monkeypatch.setattr(dic, "_write_report", _write, raising=True)
        asked.clear()
        in_parent[0] = False
        _text, child = await dic.run_deep_investigation_chat(
            db,
            subject=parent.subject_value,
            subject_type=parent.subject_type,
            user_id=user.id,
            packs=[continued],
            continues_investigation_id=parent.id,
        )
        assert child.investigation.subject_type == "domain_list"
        assert child.investigation.continues_investigation_id == parent.id
        assert len(child.steps) == 1
        assert [name for name, _seeds in asked] == [offer.steps[0].source_name]

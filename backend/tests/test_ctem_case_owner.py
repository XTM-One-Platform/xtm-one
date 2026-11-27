"""The owner a CTEM case is routed to (#1736 slice 3, #2452, #2560).

Group owner over device user, the majority group with contributors, the primary
asset owner, the default then the decider, and the sweep's capped re-routing.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.services import ctem_case_owner as owner_of

IT = uuid.uuid4()
CLOUD = uuid.uuid4()
RISK = uuid.uuid4()
DEFAULT = uuid.uuid4()
EMPLOYEE = uuid.uuid4()
USERS = {
    "it@example.com": IT,
    "cloud@example.com": CLOUD,
    "risk@example.com": RISK,
    "default@example.com": DEFAULT,
    "abdulaziz@example.com": EMPLOYEE,
}
Candidate = owner_of.Candidate


def _signal(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        organization_id=uuid.uuid4(),
        subject_kind="asset",
        subject_ref="Web-01",
        decision={
            "scope": {
                "sources": [
                    {"from": "asset", "name": "db-02 ", "criticality": "high"},
                    {"from": "asset_group", "name": "web-01", "criticality": "high"},
                    {"from": "primary_asset", "name": "Customer portal", "criticality": "critical"},
                ]
            }
        },
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_asset_names_reads_the_subject_then_the_asset_sources() -> None:
    assert owner_of.asset_names(_signal()) == ["web-01", "db-02"]
    assert owner_of.asset_names(_signal(subject_kind="software", decision=None)) == []
    assert owner_of.asset_names(_signal(subject_kind="software", decision={"scope": "bad"})) == []


def test_primary_asset_names_read_the_scope() -> None:
    assert owner_of.primary_asset_names(_signal()) == ["customer portal"]
    assert owner_of.primary_asset_names(_signal(decision=None)) == []


def test_the_group_owner_wins_and_the_device_user_is_never_a_candidate() -> None:
    # The device user (a record's owner) is not an input of the routing at all: only groups and primary assets are.
    route = owner_of.pick_route(
        [Candidate("All MacOS x86_64", "IT@example.com", 3)],
        [Candidate("customer portal", "risk@example.com", 3)],
        USERS,
        affected=3,
        default="default@example.com",
    )
    assert route.owner == IT
    assert route.reason == "asset group: All MacOS x86_64 (3 of 3 affected assets)"
    assert EMPLOYEE not in {route.owner, *(c[0] for c in route.contributors)}


def test_a_case_on_several_groups_takes_the_majority_group_and_lists_the_others() -> None:
    route = owner_of.pick_route(
        [
            Candidate("OVHCloud Servers", "cloud@example.com", 20),
            Candidate("All endpoints", "it@example.com", 174),
            Candidate("All Windows x86_64", "it@example.com", 60),
        ],
        [],
        USERS,
        affected=194,
    )
    assert route.owner == IT
    assert route.reason == "asset group: All endpoints (174 of 194 affected assets)"
    assert route.contributors == ((CLOUD, "asset group: OVHCloud Servers"),)
    assert route.fallback is False
    evidence = json.loads(route.evidence())
    assert evidence["route"] == owner_of.ROUTE_VERSION and evidence["fallback"] is False
    assert evidence["contributors"] == [{"user_id": str(CLOUD), "via": "asset group: OVHCloud Servers"}]


def test_a_tie_is_broken_by_the_group_name() -> None:
    route = owner_of.pick_route(
        [Candidate("b group", "cloud@example.com", 5), Candidate("A group", "it@example.com", 5)], [], USERS, affected=5
    )
    assert route.owner == IT and route.contributors == ((CLOUD, "asset group: b group"),)


def test_a_group_whose_owner_is_no_account_gives_way_to_the_next_group() -> None:
    route = owner_of.pick_route(
        [Candidate("All endpoints", "IT team", 100), Candidate("All MacOS x86_64", "it@example.com", 10)],
        [],
        USERS,
        affected=100,
    )
    assert route.owner == IT and route.reason == "asset group: All MacOS x86_64 (10 of 100 affected assets)"


def test_the_primary_asset_owner_when_no_group_has_an_owner() -> None:
    route = owner_of.pick_route(
        [Candidate("All endpoints", "", 12)],
        [Candidate("billing", "", 2), Candidate("customer portal", "risk@example.com", 1)],
        USERS,
        affected=12,
        default="default@example.com",
    )
    assert route == owner_of.Route(RISK, "primary asset: customer portal")
    assert route.fallback is False


def test_the_default_then_nobody() -> None:
    route = owner_of.pick_route([], [], USERS, affected=0, default="default@example.com")
    assert route == owner_of.Route(DEFAULT, owner_of.FROM_DEFAULT) and route.fallback is True
    assert owner_of.pick_route([Candidate("g", "Adam Pagliuca", 1)], [], USERS, affected=1) == owner_of.Route()


def test_the_default_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTEM_CASE_DEFAULT_OWNER", raising=False)
    assert owner_of.default_owner_email() == ""
    monkeypatch.setenv("CTEM_CASE_DEFAULT_OWNER", " SecOps@Example.com ")
    assert owner_of.default_owner_email() == "secops@example.com"


class _Savepoint:
    async def __aenter__(self) -> _Savepoint:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def all(self) -> list[Any]:
        return list(self.rows)

    def scalars(self) -> _Result:
        return self


class Session:
    """Answers the reads in order; collects the rows added."""

    def __init__(self, *reads: list[Any], fail: bool = False) -> None:
        self.reads = list(reads)
        self.fail = fail
        self.added: list[Any] = []

    def begin_nested(self) -> _Savepoint:
        return _Savepoint()

    async def execute(self, _statement: object) -> _Result:
        if self.fail:
            raise RuntimeError("inventory unavailable")
        return _Result(self.reads.pop(0) if self.reads else [])

    def add(self, row: Any) -> None:
        self.added.append(row)


def _no_decider(monkeypatch: pytest.MonkeyPatch, who: tuple[Any, Any] = (None, None)) -> None:
    async def decider(_db: Any) -> tuple[Any, Any]:
        return who

    monkeypatch.setattr(owner_of, "decider_owner", decider)


@pytest.mark.asyncio
async def test_route_case_reads_groups_by_membership_and_never_the_record_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CTEM_CASE_DEFAULT_OWNER", raising=False)
    _no_decider(monkeypatch)
    rec_a, rec_b, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = Session(
        [(rec_a, "abdulaziz's macbook air"), (rec_b, "adams-macbook-pro")],  # records (no owner column read)
        [(group, rec_a, "All MacOS x86_64", "it@example.com"), (group, rec_b, "All MacOS x86_64", "it@example.com")],
        [],  # primary asset links
        [(IT, "it@example.com")],  # users
    )
    route = await owner_of.route_case(
        cast("Any", db), uuid.uuid4(), ["abdulaziz's macbook air", "adams-macbook-pro"], profile_owners={}
    )
    assert route.owner == IT and route.reason == "asset group: All MacOS x86_64 (2 of 2 affected assets)"


@pytest.mark.asyncio
async def test_route_case_reaches_the_primary_asset_through_a_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTEM_CASE_DEFAULT_OWNER", raising=False)
    _no_decider(monkeypatch)
    rec = uuid.uuid4()
    db = Session(
        [(rec, "air-dmz.dev.filigran.io")],
        [],  # no group
        [("Customer Portal", rec, None)],  # a direct link
        [(RISK, "risk@example.com")],
    )
    route = await owner_of.route_case(
        cast("Any", db),
        uuid.uuid4(),
        ["air-dmz.dev.filigran.io"],
        profile_owners={"customer portal": "risk@example.com"},
    )
    assert route == owner_of.Route(RISK, "primary asset: customer portal")


@pytest.mark.asyncio
async def test_a_failed_inventory_read_falls_to_the_decider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CTEM_CASE_DEFAULT_OWNER", "default@example.com")
    _no_decider(monkeypatch, (IT, "fallback: CTEM decider (administrator)"))
    route = await owner_of.route_case(cast("Any", Session(fail=True)), uuid.uuid4(), ["web-01"], profile_owners={})
    assert route == owner_of.Route(IT, "fallback: CTEM decider (administrator)")


@pytest.mark.asyncio
async def test_the_default_wins_over_the_decider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CTEM_CASE_DEFAULT_OWNER", "default@example.com")
    _no_decider(monkeypatch, (IT, "fallback: CTEM decider (administrator)"))
    db = Session([], [(DEFAULT, "default@example.com")])  # no record, then the users
    route = await owner_of.route_case(cast("Any", db), uuid.uuid4(), ["web-01"], profile_owners={})
    assert route == owner_of.Route(DEFAULT, owner_of.FROM_DEFAULT)


@pytest.mark.asyncio
async def test_nobody_decides_answers_no_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTEM_CASE_DEFAULT_OWNER", raising=False)
    _no_decider(monkeypatch)
    assert await owner_of.route_case(cast("Any", Session()), None, [], profile_owners={}) == owner_of.Route()


# --- #2452: the CTEM decider -------------------------------------------------------

SAM = uuid.uuid4()
BOB = uuid.uuid4()
SEED = uuid.uuid4()


def test_the_decider_is_a_decide_group_member_before_an_administrator() -> None:
    assert owner_of.pick_decider([(BOB, "bob@example.com")], [(SAM, "sam@example.com")]) == (
        BOB,
        "fallback: CTEM decider (decide group member)",
    )
    assert owner_of.pick_decider([], [(SAM, "sam@example.com")]) == (SAM, "fallback: CTEM decider (administrator)")
    assert owner_of.pick_decider([], []) == (None, None)


def test_several_deciders_take_the_first_by_creation_and_say_so() -> None:
    owner, how = owner_of.pick_decider([], [(SAM, "sam@example.com"), (BOB, "bob@example.com")])
    assert owner == SAM and how == "fallback: CTEM decider (administrator, first of 2 by account creation)"


def test_the_seed_account_decides_only_when_it_is_alone() -> None:
    people = [(SEED, "admin@filigran.io"), (SAM, "sam@example.com")]
    assert owner_of.pick_decider([], people, "Admin@Filigran.io")[0] == SAM
    assert owner_of.pick_decider([], people[:1], "admin@filigran.io")[0] == SEED


# --- the case read and the sweep ---------------------------------------------------


def test_owner_details_read_the_newest_owner_event() -> None:
    route = owner_of.Route(IT, "asset group: All endpoints (3 of 4 affected assets)", ((CLOUD, "asset group: DNS"),))
    newest = SimpleNamespace(kind="owner", reason=route.reason, evidence=route.evidence())
    older = SimpleNamespace(kind="owner", reason="fallback: CTEM decider (administrator)", evidence=None)
    note = SimpleNamespace(kind="note", reason="x", evidence=None)
    assert owner_of.owner_details([note, newest, older]) == {
        "owner_reason": "asset group: All endpoints (3 of 4 affected assets)",
        "owner_basis": {"kind": "asset_group", "name": "All endpoints", "assets": 3, "affected": 4},
        "owner_contributors": [{"user_id": str(CLOUD), "via": "asset group: DNS", "group": "DNS"}],
    }
    assert owner_of.owner_details([older]) == {
        "owner_reason": "fallback: CTEM decider (administrator)",
        "owner_basis": {"kind": "decider", "source": "administrator", "candidates": 1},
        "owner_contributors": [],
    }
    assert owner_of.owner_details([]) == {"owner_reason": None, "owner_basis": None, "owner_contributors": []}


def test_the_basis_is_stored_as_data_and_read_from_older_reasons() -> None:
    """#2571: the case detail words the basis in the reader's language, never the stored sentence."""
    assert owner_of.basis_of("asset group: DNS: shared (2 affected assets)") == {
        "kind": "asset_group",
        "name": "DNS: shared",
        "assets": 2,
        "affected": None,
    }
    assert owner_of.basis_of("primary asset: customer portal") == {"kind": "primary_asset", "name": "customer portal"}
    assert owner_of.basis_of(owner_of.FROM_DEFAULT) == {"kind": "default"}
    assert owner_of.basis_of("fallback: CTEM decider (decide group member, first of 3 by account creation)") == {
        "kind": "decider",
        "source": "decide_group",
        "candidates": 3,
    }
    assert owner_of.basis_of(owner_of.FROM_KEPT) == {"kind": "kept"}
    assert owner_of.basis_of("Taken over") is None and owner_of.basis_of(None) is None
    route = owner_of.Route(IT, "primary asset: customer portal")
    assert json.loads(route.evidence())["basis"] == {"kind": "primary_asset", "name": "customer portal"}
    # A person's change is answered as such; a system event with an unknown reason stays unexplained.
    by_person = SimpleNamespace(kind="owner", actor=str(SAM), reason=None, evidence=None)
    assert owner_of.owner_details([by_person])["owner_basis"] == {"kind": "person"}
    unknown = SimpleNamespace(kind="owner", actor="system", reason="x", evidence=None)
    assert owner_of.owner_details([unknown])["owner_basis"] is None


def _sweep(monkeypatch: pytest.MonkeyPatch, route: owner_of.Route, calls: list[Any]) -> None:
    async def routed(_db: Any, org: Any, names: Any, primaries: Any, **kw: Any) -> owner_of.Route:
        calls.append((org, list(names), list(primaries), kw.get("decider")))
        return route

    async def no_running(_db: Any, _signal: Any, _case: Any = None) -> list[str]:
        return []

    async def owners(_db: Any, _org: Any) -> dict[str, str]:
        return {}

    async def changed(_db: Any) -> None:
        return None

    monkeypatch.setattr(owner_of, "route_case", routed)
    monkeypatch.setattr(owner_of, "case_asset_names", no_running)
    monkeypatch.setattr(owner_of, "primary_asset_owners", owners)
    monkeypatch.setattr(owner_of, "_owners_changed_at", changed)
    _no_decider(monkeypatch, (SAM, "fallback: CTEM decider (administrator)"))

    from app.services import platform_events

    async def no_publish(*_a: Any, **_k: Any) -> str:
        raise AssertionError("the owner repair publishes no platform event")

    monkeypatch.setattr(platform_events, "publish", no_publish)


@pytest.mark.asyncio
async def test_the_repair_reroutes_a_thousand_open_cases_without_an_event_or_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from app.models.ctem_case import CtemCaseEvent

    calls: list[Any] = []
    route = owner_of.Route(IT, "asset group: All endpoints (1 of 1 affected assets)")
    _sweep(monkeypatch, route, calls)
    org = uuid.uuid4()
    cases = [SimpleNamespace(id=uuid.uuid4(), organization_id=org, owner_user_id=SAM) for _ in range(1000)]
    signal = _signal(organization_id=org)
    db = Session(cases, *([[signal]] * 1000))
    now = datetime(2026, 11, 27, 6, 0, tzinfo=UTC)
    assert await owner_of.route_open_cases(cast("Any", db), now=now, limit=1000) == 1000
    assert all(c.owner_user_id == IT for c in cases)
    events = [r for r in db.added if isinstance(r, CtemCaseEvent)]
    assert len(events) == 1000 and {e.kind for e in events} == {"owner"} and {e.actor for e in events} == {"system"}
    assert events[0].from_value == str(SAM) and events[0].to_value == str(IT) and events[0].reason == route.reason
    assert json.loads(events[0].evidence or "{}")["route"] == owner_of.ROUTE_VERSION
    # One decider read for the whole pass, handed to every routing.
    assert {c[3] for c in calls} == {(SAM, "fallback: CTEM decider (administrator)")}
    assert calls[0][1] == ["web-01", "db-02"] and calls[0][2] == ["customer portal"]


@pytest.mark.asyncio
async def test_a_case_with_nobody_to_route_to_keeps_its_owner_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    _sweep(monkeypatch, owner_of.Route(), [])
    kept = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4(), owner_user_id=SAM)
    unowned = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4(), owner_user_id=None)
    db = Session([kept, unowned], [], [])
    assert await owner_of.route_open_cases(cast("Any", db), now=datetime.now(UTC)) == 1
    assert kept.owner_user_id == SAM and unowned.owner_user_id is None
    assert [(e.to_value, e.reason) for e in db.added] == [(str(SAM), "fallback: kept, no owner found")]


@pytest.mark.asyncio
async def test_the_repair_writes_nothing_when_no_case_is_left() -> None:
    from datetime import UTC, datetime

    db = Session([])
    assert await owner_of.backfill_owners(cast("Any", db), now=datetime.now(UTC)) == 0 and db.added == []


# --- #2577: a platform group as the owning team ---------------------------------------

PAYMENTS = owner_of.Team(uuid.uuid4(), "Payments team", CLOUD)


def test_a_group_owned_by_a_platform_group_routes_to_that_team_and_its_lead() -> None:
    route = owner_of.pick_route(
        [Candidate("Payments servers", "Payments Team", 8), Candidate("All endpoints", "it@example.com", 2)],
        [],
        USERS,
        affected=10,
        teams={"payments team": PAYMENTS},
    )
    assert route.owner == CLOUD and route.team == PAYMENTS and route.team_id == PAYMENTS.id
    assert route.reason == "asset group: Payments servers (8 of 10 affected assets)"
    assert route.contributors == ((IT, "asset group: All endpoints"),)
    basis = json.loads(route.evidence())["basis"]
    assert basis["kind"] == "asset_group" and basis["team"] == {"id": str(PAYMENTS.id), "name": "Payments team"}


def test_a_team_with_no_active_member_is_passed_over_and_an_email_keeps_todays_routing() -> None:
    # No team resolved (no active member, or no such group): the next group's email owner wins, no team.
    route = owner_of.pick_route(
        [Candidate("Payments servers", "Payments team", 8), Candidate("All endpoints", "it@example.com", 2)],
        [],
        USERS,
        affected=10,
        teams={},
    )
    assert route.owner == IT and route.team is None and route.team_id is None
    assert "team" not in json.loads(route.evidence())["basis"]


def test_a_primary_asset_owned_by_a_team_routes_to_it() -> None:
    route = owner_of.pick_route(
        [], [Candidate("billing", "payments team", 1)], USERS, affected=1, teams={"payments team": PAYMENTS}
    )
    assert route == owner_of.Route(CLOUD, "primary asset: billing", team=PAYMENTS)


def test_team_names_are_the_owner_texts_without_an_email() -> None:
    assert owner_of.team_names(
        [Candidate("a", " Payments  Team ", 1), Candidate("b", "it@example.com", 1), Candidate("c", "", 1)]
    ) == ["payments team"]


@pytest.mark.asyncio
async def test_team_owners_take_a_manager_before_a_member_and_read_nothing_for_no_name() -> None:
    gid = uuid.uuid4()
    db = Session([(gid, "Payments team", SAM)], [(gid, "Payments team", BOB)])  # managers, then members
    assert await owner_of.team_owners(cast("Any", db), ["payments team"]) == {
        "payments team": owner_of.Team(gid, "Payments team", SAM)
    }
    members_only = Session([], [(gid, "Payments team", BOB)])
    found = await owner_of.team_owners(cast("Any", members_only), ["payments team"])
    assert found["payments team"].lead == BOB
    none = Session(fail=True)
    assert await owner_of.team_owners(cast("Any", none), []) == {}


@pytest.mark.asyncio
async def test_route_case_reads_the_teams_after_the_users(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTEM_CASE_DEFAULT_OWNER", raising=False)
    _no_decider(monkeypatch)
    rec, group, gid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = Session(
        [(rec, "pay-01")],
        [(group, rec, "Payments servers", "Payments team")],
        [],  # primary asset links (no user read: no email owner)
        [],  # team managers
        [(gid, "Payments team", BOB)],  # team members
    )
    route = await owner_of.route_case(cast("Any", db), uuid.uuid4(), ["pay-01"], profile_owners={})
    assert route.owner == BOB and route.team == owner_of.Team(gid, "Payments team", BOB)


@pytest.mark.asyncio
async def test_the_repair_sets_the_team_and_a_kept_owner_keeps_its_team(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    _sweep(
        monkeypatch, owner_of.Route(CLOUD, "asset group: Payments servers (1 of 1 affected assets)", team=PAYMENTS), []
    )
    case = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4(), owner_user_id=SAM, owner_group_id=None)
    db = Session([case], [])
    assert await owner_of.route_open_cases(cast("Any", db), now=datetime.now(UTC)) == 1
    assert case.owner_user_id == CLOUD and case.owner_group_id == PAYMENTS.id

    _sweep(monkeypatch, owner_of.Route(), [])
    team = uuid.uuid4()
    kept = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4(), owner_user_id=SAM, owner_group_id=team)
    db = Session([kept], [])
    assert await owner_of.route_open_cases(cast("Any", db), now=datetime.now(UTC)) == 1
    assert kept.owner_group_id == team

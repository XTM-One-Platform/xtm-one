"""The ROI ledger invariants on every surface's entry point (issue #12077, slice L7d, design 12.7).

``tests/test_roi_ledger_invariants.py`` proves the invariants on ``roi_figures.compute``.
This suite proves them where a person or an agent reads a figure: the routes of
``GET /roi`` (the Value page, the agent and fleet heroes), ``GET /roi/summary``
(the list column), ``GET /roi/export`` (CSV), the fleet datasheet and the
``get_roi_report`` agent tool. The ledger's facts are the seeded fixture of the
invariant suite (7 people, one administrator, own, shared, company-managed and
deleted agents, two fleets, agentless chat, system costs); only the database
reads around the ledger are replaced, never the ledger itself.

(a) the Mine of every person plus "No person" is the organization total;
(b) the organization total is the same for the administrator and every non-administrator;
(c) every breakdown sums to its hero, folded and residual rows included;
(d) agent X: the agent page, the list column, the administrator's by-agent row, the
    export and the agent tool carry the same figure;
(e) using a shared agent moves only the user's own Mine;
(f) no non-administrator answer names another person;
(g) a group under k is never shown to a non-administrator, folded into its breakdown's row;
(h) one row per fact: no breakdown names a key twice.

Slices L7a (home card, list column) and L7b (datasheet) move their surfaces onto the
same entry points read here (``GET /roi/summary`` ``totals``, ``_datasheet_value``),
so no assertion waits on them.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.v1 import fleets as fleets_api
from app.api.v1 import helpers as api_helpers
from app.api.v1 import platform_settings_helpers
from app.api.v1 import roi as roi_api
from app.core.config import settings
from app.services import roi_access, roi_export, roi_ledger, roi_report
from app.services.builtin_tools import roi as roi_tools
from app.services.roi_figures import (
    ROW_NO_PERSON,
    ROW_OTHER,
    ROW_OTHER_SOURCES,
    ROW_OTHER_UNITS,
    SCOPE_AGENT,
    SCOPE_FLEET,
    Viewer,
    week_of,
)
from tests.test_roi_ledger_invariants import (
    ADMIN,
    AGENTS,
    BIG_FLEET,
    COMPANY,
    PEOPLE,
    SHARED,
    SMALL_FLEET,
    _fixture,
)

WINDOW = "all"
NON_ADMINS = [p for p in PEOPLE if p != ADMIN]
VISIBLE_AGENTS = frozenset({SHARED, COMPANY})
VISIBLE_FLEETS = frozenset({BIG_FLEET})
SECTIONS = ("by_source", "by_unit", "by_agent", "by_fleet", "by_week", "by_person")
#: The row a breakdown folds its groups under k into (12.5).
FOLD_ROW = {
    "by_source": ROW_OTHER_SOURCES,
    "by_unit": ROW_OTHER_UNITS,
    "by_agent": ROW_OTHER,
    "by_fleet": ROW_OTHER,
}
#: The figures of a hero or a row compared across surfaces.
FIGURES = (
    "runs",
    "reviewed",
    "units",
    "deliverables",
    "hours",
    "hours_low",
    "hours_high",
    "value",
    "value_low",
    "value_high",
    "ai_cost",
    "net_value",
)
#: The CSV's name of a figure (``roi_export.CSV_COLUMNS``) where it differs from the JSON's.
CSV_NAMES = {"deliverables": "chat_deliverables"}
SEEDS = [0, 7, 42, 99]


class _Db:
    """The session the routes are handed: the ledger's reads are replaced, nothing else is read."""

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def __aenter__(self) -> _Db:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None


def _db() -> Any:
    return _Db()


class _World:
    """The seeded ledger and who reads it: the facts, k, and whether By person is on."""

    def __init__(self) -> None:
        self.facts: list[Any] = _fixture(0)
        self.k: int | None = None
        self.by_person = False

    def viewer(self, user_id: uuid.UUID | None, is_admin: bool) -> Viewer:
        if is_admin:
            return Viewer(user_id=user_id, organization=True, by_person=self.by_person)
        return Viewer(user_id=user_id, agent_ids=VISIBLE_AGENTS, fleet_ids=VISIBLE_FLEETS, min_group_size=self.k)


def _user(person: uuid.UUID) -> Any:
    return SimpleNamespace(id=person, is_admin=person == ADMIN)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    state = _World()

    async def ledger_facts(_db: Any, **_kw: Any) -> tuple[list[Any], str]:
        return list(state.facts), "EUR"

    async def viewer_for(_db: Any, *, user_id: uuid.UUID | None, is_admin: bool) -> Viewer:
        return state.viewer(user_id, is_admin)

    async def reader_scope(_db: Any, *, user_id: uuid.UUID | None, is_admin: bool) -> roi_access.RoiScope:
        if is_admin:
            return roi_access.RoiScope(organization=True)
        return roi_access.RoiScope(agent_ids=VISIBLE_AGENTS, fleet_ids=VISIBLE_FLEETS)

    async def agent_reader(_db: Any, *, user_id: Any, is_admin: bool, agent_id: uuid.UUID) -> bool:
        return is_admin or agent_id in VISIBLE_AGENTS

    async def fleet_reader(_db: Any, *, user_id: Any, is_admin: bool, fleet_id: uuid.UUID) -> bool:
        return is_admin

    async def fleet_visible(_db: Any, *, user_id: Any, fleet_id: uuid.UUID | None) -> bool:
        return fleet_id in VISIBLE_FLEETS

    async def labels(_db: Any, _figures: Any) -> tuple[dict[str, str], list[Any]]:
        return {}, []

    async def load_report(*_a: Any, **_kw: Any) -> None:
        return None

    async def declared_subjects(_db: Any, _subject_type: str, ids: list[uuid.UUID]) -> set[uuid.UUID]:
        return set(ids)

    async def record_details(*_a: Any, **_kw: Any) -> dict[uuid.UUID, dict[str, Any]]:
        return {}

    async def org_settings(_db: Any) -> SimpleNamespace:
        return SimpleNamespace(roi_org_totals_visible=True)

    async def no_audit(*_a: Any, **_kw: Any) -> None:
        return None

    async def no_supervisors(_db: Any, _fleet_ids: Any) -> dict[uuid.UUID, frozenset[uuid.UUID]]:
        return {}

    monkeypatch.setattr(settings, "observability_cost_data_enabled", True)
    monkeypatch.setattr(roi_ledger, "ledger_facts", ledger_facts)
    monkeypatch.setattr(roi_ledger, "viewer_for", viewer_for)
    monkeypatch.setattr(roi_ledger, "fleet_supervisors", no_supervisors)
    monkeypatch.setattr(roi_access, "reader_scope", reader_scope)
    monkeypatch.setattr(roi_access, "agent_roi_reader", agent_reader)
    monkeypatch.setattr(roi_access, "fleet_roi_reader", fleet_reader)
    monkeypatch.setattr(roi_access, "fleet_visible", fleet_visible)
    monkeypatch.setattr(roi_export, "labels", labels)
    monkeypatch.setattr(roi_report, "load_report", load_report)
    monkeypatch.setattr(roi_report, "declared_subjects", declared_subjects)
    monkeypatch.setattr(roi_report, "record_details", record_details)
    monkeypatch.setattr(roi_report, "report_dict", lambda _r: {})
    monkeypatch.setattr(roi_report, "compact_report_dict", lambda _r, **_kw: {})
    monkeypatch.setattr(roi_api, "get_platform_settings", org_settings)
    monkeypatch.setattr(platform_settings_helpers, "get_platform_settings", org_settings)
    monkeypatch.setattr(roi_api, "log_audit", no_audit)
    monkeypatch.setattr(api_helpers, "agent_tool_admin_bypass", lambda user: bool(user.is_admin))
    yield state


# ── The entry points ─────────────────────────────────────────────────


async def _page(person: uuid.UUID, scope: str, scope_id: uuid.UUID | None = None) -> dict[str, Any]:
    """``GET /roi``'s ``figures``: the Value page (mine, platform), the agent and fleet heroes."""
    answer = await roi_api.get_roi_report(
        window=WINDOW, currency=None, scope=scope, id=scope_id, db=_db(), user=_user(person)
    )
    figures: dict[str, Any] = answer["figures"]
    return figures


async def _column(person: uuid.UUID, subject_type: str, ids: list[uuid.UUID]) -> dict[str, Any]:
    """``GET /roi/summary``'s ``figures.totals`` per id: the list column (ledger rows since L7a)."""
    answer = await roi_api.get_roi_summary(
        subject_type=subject_type, ids=ids, window=WINDOW, currency=None, db=_db(), user=_user(person)
    )
    return {item["id"]: item["figures"]["totals"] for item in answer["items"]}


async def _export(person: uuid.UUID, scope: str, scope_id: uuid.UUID | None = None) -> str:
    """``GET /roi/export?format=csv``."""
    response = await roi_api.export_roi_report(
        format="csv",
        window=WINDOW,
        currency=None,
        scope=scope,
        id=scope_id,
        fleet_id=None,
        db=_db(),
        user=_user(person),
    )
    return bytes(response.body).decode("utf-8")


async def _tool(person: uuid.UUID, scope: str, scope_id: uuid.UUID | None = None) -> str:
    """The ``get_roi_report`` agent tool, acting for *person*."""
    user = _user(person)

    async def load_user(*_a: Any) -> Any:
        return user

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(f"{roi_tools.__name__}.database.async_session", lambda: _Db())
        patch.setattr(f"{roi_tools.__name__}._builder._load_caller_user_for_builder", load_user)
        _defs, executor = roi_tools.build_roi_tools(str(person))
        args = {"scope": scope, "window": WINDOW}
        if scope_id is not None:
            args["id"] = str(scope_id)
        answer = await executor._handlers["get_roi_report"](**args)
    return str(answer if isinstance(answer, str) else getattr(answer, "content", str(answer)))


def _csv_sections(text: str) -> list[dict[str, str]]:
    lines = list(csv.reader(io.StringIO(text)))
    figures = lines[: lines.index([])]
    return [dict(zip(figures[0], line, strict=True)) for line in figures[1:]]


def _figure(answer: dict[str, Any], key: str) -> Any:
    """*key* of a JSON totals or a CSV line (``""`` read as ``None``); ``KeyError`` when it carries neither."""
    for name in (key, CSV_NAMES.get(key, key)):
        if name in answer:
            value = answer[name]
            return None if value == "" else value
    raise KeyError(key)


def _same(a: dict[str, Any], b: dict[str, Any], where: object) -> None:
    """Every figure both answers carry is equal (a CSV line has no ``reviewed``)."""
    compared = 0
    for key in FIGURES:
        try:
            left, right = _figure(a, key), _figure(b, key)
        except KeyError:
            continue
        compared += 1
        if left is None or right is None:
            assert left == right, (where, key)
        else:
            assert float(left) == pytest.approx(float(right), abs=0.011), (where, key, left, right)
    assert compared >= len(FIGURES) - 1, (where, "figures compared", compared)


def _summed(rows: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ("runs", "reviewed", "units", "deliverables", "value", "value_low", "value_high", "ai_cost")
    return {k: sum(float(r[k]) for r in rows if r.get(k) is not None) for k in keys}


def _same_sum(a: dict[str, Any], b: dict[str, Any], where: object) -> None:
    for key in a:
        assert a[key] == pytest.approx(b[key], abs=0.05), (where, key, a[key], b[key])


def _empty(totals: dict[str, Any]) -> bool:
    return not (totals["runs"] or totals["deliverables"] or totals["value"] or totals["ai_cost"])


# ── (a) and (b) ──────────────────────────────────────────────────────


@pytest.mark.parametrize("seed", SEEDS)
async def test_a_every_mine_plus_no_person_is_the_organization_total(world: _World, seed: int) -> None:
    world.facts = _fixture(seed)
    world.by_person = True
    org = await _page(ADMIN, "platform")
    by_person = {row["key"]: row for row in org["by_person"]}
    mines = [await _page(p, "mine") for p in PEOPLE]
    for person, mine in zip(PEOPLE, mines, strict=True):
        row = by_person.get(str(person))
        if row is None:
            assert _empty(mine["totals"]), person
        else:
            _same(mine["totals"], row, person)
    parts = [m["totals"] for m in mines]
    if ROW_NO_PERSON in by_person:
        parts.append(by_person[ROW_NO_PERSON])
    _same_sum(_summed(parts), _summed([org["totals"]]), "sum of Mine")


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("k", [None, 3])
async def test_b_the_organization_total_is_the_same_for_every_reader(world: _World, seed: int, k: int | None) -> None:
    world.facts, world.k = _fixture(seed), k
    hero = (await _page(ADMIN, "platform"))["totals"]
    for person in NON_ADMINS:
        _same((await _page(person, "platform"))["totals"], hero, ("page", person))
        export = _csv_sections(await _export(person, "platform"))
        _same(export[0], hero, ("export", person))
        tool = json.loads(await _tool(person, "platform"))
        _same(tool["figures"]["totals"], hero, ("tool", person))


# ── (c) and (h) ──────────────────────────────────────────────────────


async def _every_answer(world: _World) -> list[tuple[object, dict[str, Any]]]:
    answers: list[tuple[object, dict[str, Any]]] = []
    for person in PEOPLE:
        answers.append(((person, "mine"), await _page(person, "mine")))
        answers.append(((person, "platform"), await _page(person, "platform")))
        agents = AGENTS if person == ADMIN else tuple(VISIBLE_AGENTS)
        for agent in agents:
            answers.append(((person, "agent", agent), await _page(person, SCOPE_AGENT, agent)))
        fleets = (SMALL_FLEET, BIG_FLEET) if person == ADMIN else tuple(VISIBLE_FLEETS)
        for fleet in fleets:
            answers.append(((person, "fleet", fleet), await _page(person, SCOPE_FLEET, fleet)))
    return answers


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("k", [None, 3, 5])
async def test_c_h_every_breakdown_sums_to_its_hero_one_row_per_key(world: _World, seed: int, k: int | None) -> None:
    world.facts, world.k, world.by_person = _fixture(seed), k, True
    for where, figures in await _every_answer(world):
        hero = _summed([figures["totals"]])
        for section in SECTIONS:
            rows = figures.get(section)
            if rows is None:
                continue
            keys = [row["key"] for row in rows]
            assert len(keys) == len(set(keys)), (where, section, keys)
            _same_sum(_summed(rows), hero, (where, section))


@pytest.mark.parametrize("seed", SEEDS)
async def test_c_h_the_export_carries_every_row_once_and_each_section_sums_to_the_total(
    world: _World, seed: int
) -> None:
    world.facts, world.k = _fixture(seed), 3
    for person, scope, scope_id in [
        (ADMIN, "platform", None),
        (PEOPLE[3], "platform", None),
        (PEOPLE[2], "mine", None),
    ]:
        lines = _csv_sections(await _export(person, scope, scope_id))
        total = lines[0]
        assert total["section"] == "total"
        pairs = [(line["section"], line["key"]) for line in lines]
        assert len(pairs) == len(set(pairs)), (person, scope)
        for section in {line["section"] for line in lines[1:]}:
            rows = [line for line in lines[1:] if line["section"] == section]
            assert sum(int(r["runs"]) for r in rows) == int(total["runs"]), (person, section)
            assert sum(float(r["value"]) for r in rows) == pytest.approx(float(total["value"]), abs=0.05)


# ── (d) ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("seed", SEEDS)
async def test_d_an_agent_reads_the_same_on_every_surface_for_every_reader(world: _World, seed: int) -> None:
    world.facts = _fixture(seed)
    org = await _page(ADMIN, "platform")
    by_agent = {row["key"]: row for row in org["by_agent"]}
    by_fleet = {row["key"]: row for row in org["by_fleet"]}
    for person in PEOPLE:
        agents = list(AGENTS if person == ADMIN else VISIBLE_AGENTS)
        column = await _column(person, SCOPE_AGENT, agents)
        for agent in agents:
            page = (await _page(person, SCOPE_AGENT, agent))["totals"]
            where = (person, agent)
            _same(column[str(agent)], page, ("column", *where))
            _same(_csv_sections(await _export(person, SCOPE_AGENT, agent))[0], page, ("export", *where))
            _same(json.loads(await _tool(person, SCOPE_AGENT, agent))["figures"]["totals"], page, ("tool", *where))
            if str(agent) in by_agent:
                _same(by_agent[str(agent)], page, ("by-agent row", *where))
            else:
                assert _empty(page), where
        fleets = [SMALL_FLEET, BIG_FLEET] if person == ADMIN else list(VISIBLE_FLEETS)
        fleet_column = await _column(person, SCOPE_FLEET, fleets)
        for fleet in fleets:
            page = (await _page(person, SCOPE_FLEET, fleet))["totals"]
            _same(fleet_column[str(fleet)], page, ("fleet column", person, fleet))
            if str(fleet) in by_fleet:
                _same(by_fleet[str(fleet)], page, ("by-fleet row", person, fleet))
            sheet = await fleets_api._datasheet_value(_db(), fleet, _user(person))
            assert sheet is not None
            assert (sheet.runs, sheet.units, sheet.deliverables) == (page["runs"], page["units"], page["deliverables"])
            assert sheet.value == pytest.approx(float(page["value"]))
            assert sheet.net_value == pytest.approx(float(page["net_value"]))


@pytest.mark.parametrize("seed", SEEDS)
async def test_d_under_k_an_agent_reads_the_same_on_every_surface_of_one_reader(world: _World, seed: int) -> None:
    """Under the k rule a non-administrator may read his own share of an agent: the same share everywhere."""
    world.facts, world.k = _fixture(seed), 5
    for person in NON_ADMINS:
        column = await _column(person, SCOPE_AGENT, list(VISIBLE_AGENTS))
        for agent in VISIBLE_AGENTS:
            page = (await _page(person, SCOPE_AGENT, agent))["totals"]
            _same(column[str(agent)], page, ("column", person, agent))
            _same(_csv_sections(await _export(person, SCOPE_AGENT, agent))[0], page, ("export", person, agent))
            tool = json.loads(await _tool(person, SCOPE_AGENT, agent))["figures"]["totals"]
            _same(tool, page, ("tool", person, agent))


# ── (e) ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("seed", SEEDS)
async def test_e_using_a_shared_agent_moves_only_the_user_s_own_mine(world: _World, seed: int) -> None:
    world.facts = _fixture(seed)
    before = {p: (await _page(p, "mine"))["totals"] for p in PEOPLE}
    user = PEOPLE[4]
    template = next(f for f in _fixture(0) if f.agent_id == SHARED and f.person_id is not None and f.runs)
    world.facts = [*world.facts, replace(template, person_id=user)]
    after = {p: (await _page(p, "mine"))["totals"] for p in PEOPLE}
    assert after[user]["runs"] == before[user]["runs"] + 1
    for person in PEOPLE:
        if person != user:
            _same(after[person], before[person], ("someone else's Mine moved", person))


# ── (f) ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("k", [None, 5])
async def test_f_no_non_admin_answer_names_another_person(world: _World, seed: int, k: int | None) -> None:
    # By person on: what an administrator could read never reaches anyone else.
    world.facts, world.k, world.by_person = _fixture(seed), k, True
    for person in NON_ADMINS:
        texts = [
            json.dumps(await _page(person, "mine")),
            json.dumps(await _page(person, "platform")),
            await _export(person, "platform"),
            await _export(person, "mine"),
            await _tool(person, "platform"),
            await _tool(person, "mine"),
            json.dumps(await _column(person, SCOPE_AGENT, list(VISIBLE_AGENTS))),
        ]
        for agent in VISIBLE_AGENTS:
            texts.append(json.dumps(await _page(person, SCOPE_AGENT, agent)))
            texts.append(await _export(person, SCOPE_AGENT, agent))
            texts.append(await _tool(person, SCOPE_AGENT, agent))
        texts.append(json.dumps(await _page(person, SCOPE_FLEET, BIG_FLEET)))
        for other in PEOPLE:
            if other == person:
                continue
            for text in texts:
                assert str(other) not in text, (person, other)
        assert (await _page(person, "platform"))["by_person"] is None


# ── (g) ──────────────────────────────────────────────────────────────


def _people(facts: list[Any], keep: Any) -> int:
    return len({f.person_id for f in facts if f.person_id is not None and keep(f)})


def _row_filter(section: str, row: dict[str, Any]) -> Any:
    key = row["key"]
    if section == "by_agent":
        return lambda f: str(f.agent_id) == key
    if section == "by_fleet":
        return lambda f: str(f.fleet_id) == key
    if section == "by_unit":
        return lambda f: f.unit == key
    start = week_of(date.fromisoformat(key))
    end = week_of(date.fromisoformat(row.get("until") or key)) + timedelta(days=6)
    return lambda f: start <= f.day <= end


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("k", [3, 5])
async def test_g_a_group_under_k_is_never_shown_to_a_non_admin(world: _World, seed: int, k: int) -> None:
    world.facts, world.k = _fixture(seed), k
    facts = world.facts
    for person in NON_ADMINS:
        org = await _page(person, "platform")
        for section in ("by_agent", "by_fleet", "by_unit", "by_week"):
            for row in org[section]:
                if row["key"] in (ROW_OTHER, ROW_OTHER_UNITS, ROW_OTHER_SOURCES) or not _named(section, row["key"]):
                    continue
                assert _people(facts, _row_filter(section, row)) >= k, (person, section, row["key"])
        suppressed = org["suppressed"]
        assert suppressed is not None and suppressed["k"] == k
        if suppressed["groups"]:
            assert any(r["key"] == FOLD_ROW[s] for s in FOLD_ROW for r in org[s]), (person, "fold row named")
        # The administrator reads every row, whatever k.
        assert (await _page(ADMIN, "platform"))["suppressed"] is None


def _named(section: str, key: str) -> bool:
    """A row naming an object, a unit or a week (the residual rows hold no group of their own)."""
    if section in ("by_agent", "by_fleet"):
        try:
            uuid.UUID(key)
        except ValueError:
            return False
        return True
    if section == "by_unit":
        return not key.startswith("no_") and not key.startswith("other")
    return True

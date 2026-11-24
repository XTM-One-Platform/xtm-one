# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""#1337: the read-only ``ctem_read`` group, bound on any agent."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.models.ctem_case import CtemCaseEvent
from app.services import ctem_case_versions
from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS
from app.services.builtin_tools import (
    AGENT_KEYED_BUILTIN_TOOL_IDS,
    CTEM_READ_TOOL_NAMES,
    build_ctem_read_tools,
    ctem_read_tools_for,
    offers_ctem_read_tools,
)
from app.services.builtin_tools.ctem_read import asset_page, empty_sections, opened_by
from app.services.builtin_tools.ctem_signals import _since
from app.services.ctem_signals import provider_name, to_dict
from app.tools.base import ToolCall
from tests.test_ctem_case_tools import AGENT, env  # noqa: F401  (the shared fixture)


@pytest.fixture
def xtm_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)


def _custom_agent(**kw: Any) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(), slug=kw.get("slug", "my-reporting-agent"), is_builtin=kw.get("builtin", False)
    )


def test_the_group_is_registered_agent_keyed_and_writes_nothing() -> None:
    group = next(g for g in BUILTIN_TOOL_GROUPS if g.id == "ctem_read")
    assert group.agent_keyed and not group.bind_admin_only
    assert "ctem_read" in AGENT_KEYED_BUILTIN_TOOL_IDS
    defs, _ = build_ctem_read_tools(user_id="", agent_id="")
    assert tuple(d.name for d in defs) == CTEM_READ_TOOL_NAMES
    assert not any(d.name.startswith(("record_", "create_", "update_", "decide_", "sync_")) for d in defs)


def test_offered_when_bound_in_xtm_one_mode_only(monkeypatch: pytest.MonkeyPatch, xtm_one: None) -> None:
    assert offers_ctem_read_tools(["web_fetch", "ctem_read"])
    assert not offers_ctem_read_tools(["web_fetch"])
    assert not offers_ctem_read_tools(None)
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: True), raising=False)
    assert not offers_ctem_read_tools(["ctem_read"])


def test_a_custom_agent_bound_to_the_group_gets_the_three_reads(xtm_one: None) -> None:
    wired = ctem_read_tools_for(_custom_agent(), ["ctem_read"], user_id=str(uuid.uuid4()))
    assert wired is not None
    assert tuple(d.name for d in wired[0]) == CTEM_READ_TOOL_NAMES


def test_not_wired_without_a_person_or_without_the_binding(xtm_one: None) -> None:
    assert ctem_read_tools_for(_custom_agent(), ["ctem_read"], user_id="") is None
    assert ctem_read_tools_for(_custom_agent(), ["web_fetch"], user_id=str(uuid.uuid4())) is None


def test_a_ctem_agent_is_never_offered_a_function_twice(xtm_one: None) -> None:
    # Carries the signal tools, not the case tools: the group adds the case reads only.
    triage = _custom_agent(slug="ctem-vuln-triage-analyst", builtin=True)
    wired = ctem_read_tools_for(triage, ["ctem_read"], user_id=str(uuid.uuid4()))
    assert wired is not None
    defs, executor = wired
    assert [d.name for d in defs] == ["list_ctem_cases", "get_ctem_case", "list_ctem_assets"]
    assert executor.handler("list_ctem_signals") is None
    # Carries both: only the inventory read is added (#1782).
    orchestrator = _custom_agent(slug="ctem-vuln-prioritization-orchestrator", builtin=True)
    wired = ctem_read_tools_for(orchestrator, ["ctem_read"], user_id=str(uuid.uuid4()))
    assert wired is not None
    assert [d.name for d in wired[0]] == ["list_ctem_assets"]


def test_the_asset_page_carries_the_counts_by_source_and_category() -> None:
    """#1782: the inventory read answers the page, its next offset and the counts of the same filters."""
    page = {
        "items": [{"id": "a1", "name": "web-01", "sources": ["openaev"], "records": [{"source": "openaev"}]}],
        "total": 3,
        "offset": 0,
        "limit": 1,
    }
    facets = {
        "source": [{"value": "openaev", "count": 2}, {"value": "intune", "count": 1}],
        "category": [{"value": "endpoint", "count": 3}],
    }
    body = asset_page(page, facets)
    assert body["total"] == 3 and body["next_offset"] == 1
    assert body["by_source"]["openaev"] == {"name": "OpenAEV", "assets": 2}
    assert body["by_source"]["intune"] == {"name": "intune", "assets": 1}
    assert body["by_category"] == {"endpoint": 3}
    assert "records" not in body["assets"][0]
    assert asset_page({**page, "total": 1}, {})["next_offset"] is None


def test_a_signal_names_its_provider_product() -> None:
    """#1782: an agent reads \"Aikido Security\", never the bare key alone."""
    assert provider_name("aikido") == "Aikido Security"
    assert provider_name("OpenAEV") == "OpenAEV"
    assert provider_name("acme") == "acme"
    signal = SimpleNamespace(
        id=uuid.uuid4(),
        trigger_path="code_flaw",
        lens="inside",
        source_provider="aikido",
        source_external_id="42",
        subject_kind="package",
        subject_ref="lodash",
        subject_label=None,
        observed_at=None,
        first_seen_at=None,
        last_seen_at=None,
        seen_count=1,
        evidence={},
        dedupe_key="k",
        state="new",
        decision=None,
        case_id=None,
        recorded_via="tool",
    )
    source = to_dict(signal)["source"]  # type: ignore[arg-type]
    assert source == {"provider": "aikido", "name": "Aikido Security", "external_id": "42"}


def test_since_reads_a_date_or_a_time_as_utc() -> None:
    """#1782: the since filter of list_ctem_signals."""
    assert _since("") is None and _since("not a date") is None
    assert _since("2026-11-20") == datetime(2026, 11, 20, tzinfo=UTC)
    assert _since("2026-11-20T08:00:00Z") == datetime(2026, 11, 20, 8, tzinfo=UTC)


def _event(kind: str, at: datetime, actor: str = "system", **kw: Any) -> CtemCaseEvent:
    return CtemCaseEvent(case_id=uuid.uuid4(), actor=actor, kind=kind, at=at, **kw)


def test_opened_by_is_the_oldest_created_event() -> None:
    now = datetime.now(UTC)
    events = [
        _event("note", now, actor="person"),
        _event("created", now - timedelta(days=2), actor="ctem-signal", to_value="upgrade:x@1"),
        _event("created", now - timedelta(days=1), actor="later", to_value="manual"),
    ]
    assert opened_by(events) == {
        "actor": "ctem-signal",
        "at": (now - timedelta(days=2)).isoformat(),
        "how": "upgrade:x@1",
    }
    assert opened_by([_event("note", now)]) is None


def test_every_empty_section_carries_its_reason() -> None:
    rendered = {"remediation_kind": "manual", "history": [], "tickets": [], "validations": [], "exploitation": []}
    reasons = empty_sections(rendered, None)
    assert set(reasons) >= {
        "exploitation",
        "attack_path",
        "treatment_plan",
        "threat_package",
        "tickets",
        "validations",
        "running_versions",
        "history",
    }
    assert reasons["running_versions"] == "only an upgrade case has running versions"
    full = {
        "remediation_kind": "upgrade",
        "history": [{}],
        "tickets": [{}],
        "validations": [{}],
        "exploitation": [{}],
        "attack_path": {},
        "treatment_plan": {},
        "threat_package": {},
        "details_unavailable": ["openaev"],
    }
    assert empty_sections(full, {"assets": []}) == {"issues": "these source products did not answer for you: openaev"}


@pytest.fixture
def read(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:  # noqa: F811
    async def _versions(_db: Any, _case: Any) -> None:
        return None

    monkeypatch.setattr(ctem_case_versions, "for_case", _versions)

    async def _sources(_db: Any, _org: Any) -> None:
        return None

    monkeypatch.setattr(ctem_case_versions, "inventory_sources", _sources)

    def _for(user_id: str) -> Any:
        _, executor = build_ctem_read_tools(user_id=user_id, agent_id=AGENT)

        async def call(name: str, **args: Any) -> str:
            result = await executor.execute_tool(ToolCall(id="r1", name=name, arguments=args))
            return result.content if isinstance(result.content, str) else str(result.content)

        return call

    env.read_for = _for
    return env


async def test_get_answers_the_case_with_who_opened_it_and_the_empty_reasons(read: SimpleNamespace) -> None:
    read.db.added.append(
        _event("created", datetime.now(UTC), actor="ctem-signal", to_value="upgrade:apache/tomcat@9.0.98")
    )
    body = json.loads(await read.read_for(str(read.user_id))("get_ctem_case", case_id=str(read.case.id)))
    assert body["title"] == "Upgrade Apache Tomcat to 9.0.98"
    assert body["opened_by"]["actor"] == "ctem-signal"
    assert body["history_total"] == 1
    assert body["empty_sections"]["running_versions"] == "no running version read for this upgrade"
    assert "tickets" in body["empty_sections"]
    # #1359: the signal rule that applied is said, never left to a guess.
    assert body["scope"]["applied"] == "own" and body["scope"]["acting_user"] == str(read.user_id)
    assert body["scope"]["reason"] == "not an administrator"


def test_an_upgrade_no_asset_runs_names_the_inventory_sources_checked() -> None:
    """#1736: the loop is told which imports answered nothing before it asks a person."""
    rendered = {"remediation_kind": "upgrade"}
    reason = empty_sections(rendered, None, ["defender", "intune"])["running_versions"]
    assert "inventory sources checked: defender, intune" in reason
    assert "before asking the asset owner" in reason
    assert empty_sections(rendered, None, [])["running_versions"] == "no running version read for this upgrade"


async def test_get_names_the_inventory_sources_when_no_asset_runs_the_product(
    read: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _sources(_db: Any, _org: Any) -> list[str]:
        return ["intune"]

    monkeypatch.setattr(ctem_case_versions, "inventory_sources", _sources)
    body = json.loads(await read.read_for(str(read.user_id))("get_ctem_case", case_id=str(read.case.id)))
    assert "inventory sources checked: intune" in body["empty_sections"]["running_versions"]


async def test_get_carries_the_running_versions_per_platform(
    read: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1572: the read tool answers the platform rows and the per-platform fix as the store gives them."""
    running = ctem_case_versions.running_versions(
        [{"vendor": "Acme", "product": "Browser", "version": "36.0", "asset_ids": ["m1", "w1"], "sources": ["intune"]}],
        product_label="Acme Browser",
        fixed_version="36.0",
        asset_platforms={"m1": "macOS", "w1": "Windows"},
    )

    async def _versions(_db: Any, _case: Any) -> Any:
        return running

    monkeypatch.setattr(ctem_case_versions, "for_case", _versions)
    body = json.loads(await read.read_for(str(read.user_id))("get_ctem_case", case_id=str(read.case.id)))
    rows = {v["platform"]: v for v in body["running_versions"]["versions"]}
    assert set(rows) == {"Windows", "macOS"}
    assert rows["macOS"]["asset_ids"] == ["m1"]
    assert rows["macOS"]["status_reason"] == ctem_case_versions.FIX_NOT_KNOWN_FOR_PLATFORM
    assert body["running_versions"]["platform_unverified"] is True
    assert "running_versions" not in body["empty_sections"]


async def test_the_case_list_counts_the_case_bound_signals_the_scope_hides(
    read: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1359: a case's signal members are the person's own; the rest is a count, never named."""

    async def _execute(_statement: Any) -> Any:
        return SimpleNamespace(scalar_one=lambda: 2)

    monkeypatch.setattr(read.db, "execute", _execute, raising=False)
    listed = json.loads(await read.read_for(str(uuid.uuid4()))("list_ctem_cases"))
    assert listed["scope"]["applied"] == "own"
    assert listed["scope"]["hidden_by_scope"] == 2
    assert listed["scope"]["note"].startswith("2 signals exist")


async def test_the_case_read_counts_only_the_hidden_signals_of_that_case(
    read: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1359 review: a single-case read counts the signals bound to that case alone, never the organization's."""
    seen: list[str] = []
    answers = iter([0, 1])
    original = getattr(read.db, "execute", None)

    async def _execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        text = str(statement)
        if "count(" not in text:
            if original is None:
                return SimpleNamespace(scalars=lambda: [], scalar_one=lambda: 0)
            return await original(statement, *args, **kwargs)
        seen.append(text)
        value = next(answers)
        return SimpleNamespace(scalar_one=lambda: value)

    monkeypatch.setattr(read.db, "execute", _execute, raising=False)
    call = read.read_for(str(read.user_id))
    none_hidden = json.loads(await call("get_ctem_case", case_id=str(read.case.id)))
    assert "hidden_by_scope" not in none_hidden["scope"] and "note" not in none_hidden["scope"]
    one_hidden = json.loads(await call("get_ctem_case", case_id=str(read.case.id)))
    assert one_hidden["scope"]["hidden_by_scope"] == 1
    assert one_hidden["scope"]["note"].startswith("1 signal exists on this case")
    assert seen and all("ctem_signals.case_id = " in s and "recorded_by != " in s for s in seen)


async def test_a_person_who_sees_no_issue_reads_no_case(read: SimpleNamespace) -> None:
    other = read.read_for(str(uuid.uuid4()))
    assert "No case" in await other("get_ctem_case", case_id=str(read.case.id))
    listed = json.loads(await other("list_ctem_cases"))
    assert listed["cases"] == []
    assert listed["scope"]["applied"] == "own" and listed["scope"]["is_admin"] is False
    assert "No case" in await read.read_for("")("get_ctem_case", case_id=str(read.case.id))
    assert "case_id must be" in await read.read_for(str(read.user_id))("get_ctem_case", case_id="nope")
    assert read.db.commits == 0

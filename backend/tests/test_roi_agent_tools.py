"""The ROI agent tools (#10006, slice S2): the REST routes' rules, never a rule of their own."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import roi_subjects
from app.services.builtin_tools import roi as roi_tools


def _tools() -> tuple[dict[str, Any], Any]:
    defs, executor = roi_tools.build_roi_tools(str(uuid.uuid4()))
    return {d.name: d for d in defs}, executor


def test_the_suite_lists_its_functions_stamped_platform() -> None:
    defs, _ = _tools()
    assert set(defs) == set(roi_tools.ROI_FUNCTIONS) and len(defs) == len(roi_tools.ROI_FUNCTIONS)
    assert {d.source for d in defs.values()} == {"builtin:platform"}


def test_the_unit_tools_are_the_only_catalog_writes_and_no_tool_creates_a_rate_role() -> None:
    """Sam, 14 Nov 2026: an administrator's agent may add a unit when no seeded one fits; never a rate role."""
    defs, _ = _tools()
    assert {n for n in defs if n.startswith("create_")} == {"create_roi_unit"}
    assert not any("rate" in n.replace("roi_", "").replace("create", "").replace("update", "") for n in defs)
    for name in ("create_roi_unit", "update_roi_unit", "archive_roi_unit"):
        assert defs[name].description.startswith("Admin:")
        assert len(defs[name].description) < 200


class _Session:
    def __init__(self) -> None:
        self.rolled_back = False

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    async def rollback(self) -> None:
        self.rolled_back = True

    async def commit(self) -> None:
        return None


def _as(monkeypatch: pytest.MonkeyPatch, *, is_admin: bool) -> tuple[Any, _Session]:
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin)
    session = _Session()

    async def load_user(*_a: Any) -> Any:
        return user

    monkeypatch.setattr(f"{roi_tools.__name__}.database.async_session", lambda: session)
    monkeypatch.setattr(f"{roi_tools.__name__}._builder._load_caller_user_for_builder", load_user)
    _defs, executor = roi_tools.build_roi_tools(str(user.id))
    return executor, session


def _text(answer: Any) -> str:
    return str(answer if isinstance(answer, str) else getattr(answer, "content", answer))


@pytest.mark.parametrize("name", ["create_roi_unit", "update_roi_unit", "archive_roi_unit"])
async def test_a_non_admin_cannot_change_the_unit_catalog(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    executor, session = _as(monkeypatch, is_admin=False)
    text = _text(await executor._handlers[name](key="report_categorized"))
    assert text == f"{roi_tools._ADMIN_ONLY} Nothing was saved."
    assert session.rolled_back


async def test_create_needs_a_source_and_three_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, _ = _as(monkeypatch, is_admin=True)
    no_source = _text(await executor._handlers["create_roi_unit"](key="x_unit", name="X", minutes=[3, 8, 20]))
    assert "source is required" in no_source
    bad = _text(
        await executor._handlers["create_roi_unit"](key="x_unit", name="X", minutes=[3, 8], source="desk estimate")
    )
    assert "[low, likely, high]" in bad
    zero = _text(await executor._handlers["create_roi_unit"](key="x_unit", name="X", minutes=[0, 8, 20], source="desk"))
    assert "above 0" in zero


async def test_create_goes_through_the_service_and_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import audit, roi_records

    executor, _ = _as(monkeypatch, is_admin=True)
    seen: dict[str, Any] = {}

    async def create_unit(_db: Any, **fields: Any) -> Any:
        seen.update(fields)
        return SimpleNamespace(
            key=fields["key"],
            name=fields["name"],
            role="",
            manual_min_low=fields["low"],
            manual_min_likely=fields["likely"],
            manual_min_high=fields["high"],
            review_min=fields["review"],
            rate_role=fields["rate_role"],
            source=fields["source"],
            archived_at=None,
            version=1,
        )

    audited: list[dict[str, Any]] = []

    async def log_audit(_db: Any, **kwargs: Any) -> None:
        audited.append(kwargs)

    monkeypatch.setattr(roi_records, "create_unit", create_unit)
    monkeypatch.setattr(audit, "log_audit", log_audit)
    text = _text(
        await executor._handlers["create_roi_unit"](
            key="report_categorized",
            name="Report categorized with feed labels",
            rate_role="security_analyst",
            minutes=[3, 8, 20],
            review_min=1,
            source="desk estimate",
        )
    )
    body = json.loads(text)
    assert body["saved"] is True and body["unit"]["origin"] == "custom"
    assert (seen["low"], seen["likely"], seen["high"], seen["review"]) == (3.0, 8.0, 20.0, 1.0)
    assert audited[0]["entity_type"] == "roi_unit" and audited[0]["details"]["via"] == "agent"


async def test_a_built_in_unit_cannot_be_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import roi

    executor, _ = _as(monkeypatch, is_admin=True)
    builtin = next(iter(roi.BASELINES_BY_UNIT))
    text = _text(await executor._handlers["archive_roi_unit"](key=builtin))
    assert "built-in unit and cannot be archived" in text


def test_an_unknown_unit_is_answered_with_the_closest_ones_and_the_way_to_add_one() -> None:
    known = {"report_written": "Report written", "alert_triaged": "Alert triaged", "ioc_enriched": "IOC enriched"}
    text = roi_tools.unknown_unit_message("report_categorized", known)
    assert text.startswith("Unit 'report_categorized' is not a known unit. Closest units: report_written")
    assert text.endswith("an administrator's agent can add one with create_roi_unit.")
    assert "Closest" not in roi_tools.unknown_unit_message("zz", {"alert_triaged": "Alert triaged"})


def test_a_refusal_from_the_service_ends_as_a_sentence_before_nothing_was_saved() -> None:
    assert roi_tools._sentence("unit 'x' is not a known unit") == "unit 'x' is not a known unit."
    assert roi_tools._sentence("Already a sentence.") == "Already a sentence."


def test_the_alias_registry_grants_exactly_the_suite():
    from app.services.builtin_tool_aliases import alias_group, alias_scoped_functions

    assert alias_group("roi") == "platform"
    assert tuple(alias_scoped_functions("roi")) == roi_tools.ROI_FUNCTIONS


def test_the_catalog_lists_the_roi_functions_in_the_platform_group():
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    names = {d["name"] for d in build_builtin_tool_catalog(include_disabled=True)["platform"]}
    assert set(roi_tools.ROI_FUNCTIONS) <= names


def test_set_declaration_defaults_to_a_backfill_and_offers_a_dry_run():
    defs, _ = _tools()
    props = defs["set_roi_declaration"].input_schema["properties"]
    assert props["backfill"]["default"] is True
    assert props["dry_run"]["default"] is False
    assert set(defs["set_roi_declaration"].input_schema["required"]) == {
        "subject_type",
        "subject_id",
        "unit",
        "count_mode",
    }


async def test_an_anonymous_caller_is_refused_in_words():
    _defs, executor = roi_tools.build_roi_tools("")
    answer = await executor._handlers["get_roi_report"](**{})
    text = answer if isinstance(answer, str) else getattr(answer, "content", str(answer))
    assert "anonymous" in str(text)


def test_a_bad_subject_type_is_refused():
    with pytest.raises(roi_tools._RefusedError):
        roi_tools._subject_type("workspace")
    assert roi_tools._subject_type("fleet") == "fleet"


def test_a_malformed_subject_id_is_a_422():
    with pytest.raises(roi_subjects.RoiSubjectError) as refused:
        roi_subjects.parse_subject_id("not-a-uuid")
    assert refused.value.status == 422


async def test_an_invisible_subject_is_a_404(monkeypatch):
    from app.api.v1 import helpers

    async def groups(*_a: Any) -> list[str]:
        return []

    class _NothingVisible:
        async def execute(self, *_a: Any) -> Any:
            return SimpleNamespace(scalar=lambda: None)

    monkeypatch.setattr(helpers, "get_user_group_ids", groups)
    user = SimpleNamespace(id=uuid.uuid4(), role="user")
    with pytest.raises(roi_subjects.RoiSubjectError) as refused:
        await roi_subjects.readable_subject(_NothingVisible(), user, "agent", str(uuid.uuid4()))  # type: ignore[arg-type]
    assert refused.value.status == 404


async def test_an_unmanageable_subject_is_a_403(monkeypatch):
    from app.api.v1 import helpers

    async def groups(*_a: Any) -> list[str]:
        return []

    class _Db:
        async def get(self, *_a: Any) -> Any:
            return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(helpers, "get_user_group_ids", groups)
    monkeypatch.setattr(helpers, "can_manage", lambda *_a, **_k: False)
    user = SimpleNamespace(id=uuid.uuid4(), role="user")
    with pytest.raises(roi_subjects.RoiSubjectError) as refused:
        await roi_subjects.manageable_subject(_Db(), user, "agent", str(uuid.uuid4()))  # type: ignore[arg-type]
    assert refused.value.status == 403


async def test_the_api_maps_a_subject_refusal_to_its_status(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import roi as roi_api

    async def refuse(*_a: Any) -> uuid.UUID:
        raise roi_subjects.RoiSubjectError(403, "You cannot manage this agent.")

    monkeypatch.setattr(roi_subjects, "manageable_subject", refuse)
    with pytest.raises(HTTPException) as refused:
        await roi_api._manageable_subject(None, SimpleNamespace(), "agent", str(uuid.uuid4()))  # type: ignore[arg-type]
    assert refused.value.status_code == 403


async def test_a_non_admin_platform_report_is_refused_while_totals_are_hidden(monkeypatch):
    """Same refusal as GET /roi?scope=platform: the organization totals stay administrators'."""
    from app.api.v1 import platform_settings_helpers

    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def rollback(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    async def load_user(*_a: Any) -> Any:
        return user

    async def hidden(*_a: Any) -> Any:
        return SimpleNamespace(roi_org_totals_visible=False)

    monkeypatch.setattr(roi_tools.database, "async_session", lambda: _Session())
    monkeypatch.setattr(roi_tools._builder, "_load_caller_user_for_builder", load_user)
    monkeypatch.setattr(platform_settings_helpers, "get_platform_settings", hidden)
    _defs, executor = roi_tools.build_roi_tools(str(user.id))
    answer = await executor._handlers["get_roi_report"](**{"scope": "platform"})
    text = str(answer if isinstance(answer, str) else getattr(answer, "content", answer))
    assert "administrators only" in text


async def test_records_need_an_admin_or_a_fleet_reader(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def rollback(self) -> None:
            return None

    async def load_user(*_a: Any) -> Any:
        return user

    monkeypatch.setattr(roi_tools.database, "async_session", lambda: _Session())
    monkeypatch.setattr(roi_tools._builder, "_load_caller_user_for_builder", load_user)
    _defs, executor = roi_tools.build_roi_tools(str(user.id))
    answer = await executor._handlers["list_roi_records"](**{"outcome": "unreviewed"})
    text = str(answer if isinstance(answer, str) else getattr(answer, "content", answer))
    assert "Only administrators" in text
    assert not text.startswith("{")
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)

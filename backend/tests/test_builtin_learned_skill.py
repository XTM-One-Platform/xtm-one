# SPDX-License-Identifier: Apache-2.0
"""The learned skill of a built-in agent (#914, slice 3): section rules and tool gating."""

from __future__ import annotations

import pytest

from app.services import builtin_learned_skill as learned


def test_append_creates_then_extends_a_section():
    body = learned.apply_lesson(None, "Asset owners", "portal.example: platform team")
    assert "## Asset owners\nportal.example: platform team" in body
    body = learned.apply_lesson(body, "asset owners", "db.example: data team")
    assert body.count("## Asset owners") == 1
    assert "db.example: data team" in body


def test_revise_replaces_and_remove_drops_only_that_section():
    body = learned.apply_lesson(None, "A", "first-rule")
    body = learned.apply_lesson(body, "B", "two")
    body = learned.apply_lesson(body, "A", "second-rule", mode="revise")
    assert "## A\nsecond-rule" in body and "first-rule" not in body and "## B\ntwo" in body
    body = learned.apply_lesson(body, "A", "", mode="remove")
    assert "## A" not in body and "## B\ntwo" in body


@pytest.mark.parametrize(
    ("title", "text", "mode"),
    [("", "x", "append"), ("T", "", "append"), ("T", "x", "rewrite"), ("Missing", "", "remove")],
)
def test_refusals(title, text, mode):
    with pytest.raises(learned.LessonError):
        learned.apply_lesson(None, title, text, mode=mode)


def test_bounds_are_enforced():
    with pytest.raises(learned.LessonError):
        learned.apply_lesson(None, "T", "x" * (learned.SECTION_MAX_CHARS + 1))
    body = None
    with pytest.raises(learned.LessonError):
        for i in range(100):
            body = learned.apply_lesson(body, f"Topic {i}", "y" * 1000)


def _near_full_body(target: int = 11900) -> str:
    """A learned skill body of exactly *target* characters, in sections of different sizes."""
    body = learned.apply_lesson(None, "Big topic", "b" * 1400)
    i = 0
    while len(body) < target - 1100:
        body = learned.apply_lesson(body, f"Topic {i}", "y" * 900)
        i += 1
    filler = target - len(body) - len("\n## Small topic\n\n")
    body = learned.apply_lesson(body, "Small topic", "s" * filler)
    assert len(body) == target
    return body


def test_cap_refusal_names_size_room_needed_and_sections_largest_first():
    """#2697: the refusal gives the agent what it needs to make room in the same turn."""
    body = _near_full_body()
    with pytest.raises(learned.LessonCapError) as caught:
        learned.apply_lesson(body, "New topic", "n" * 300)
    exc = caught.value
    message = str(exc)
    assert exc.size == 11900 and exc.room == 100 and exc.needed > 300
    assert "it holds 11900 of 12000" in message
    assert f"this lesson needs {exc.needed} more, 100 are free" in message
    assert message.index("'Big topic' ") < message.index("'Topic 0' ")
    assert "mode revise" in message and "mode remove" in message and "then retry" in message
    sizes = learned.section_sizes(body)
    assert sizes[0][0] == "Big topic"
    assert [s[1] for s in sizes] == sorted((s[1] for s in sizes), reverse=True)
    assert isinstance(exc, learned.LessonError)


def test_consolidate_then_append_succeeds():
    """#2697: revising the largest section shorter makes room for the refused lesson."""
    body = _near_full_body()
    with pytest.raises(learned.LessonCapError):
        learned.apply_lesson(body, "New topic", "n" * 300)
    largest = learned.section_sizes(body)[0][0]
    body = learned.apply_lesson(body, largest, "short summary of the big topic", mode="revise")
    body = learned.apply_lesson(body, "New topic", "n" * 300)
    assert "## New topic\n" + "n" * 300 in body
    assert len(body) <= learned.LEARNED_SKILL_MAX_CHARS


def test_size_report_flags_near_cap():
    assert learned.size_report(None) == {"size": 0, "max_chars": 12000, "near_cap": False}
    assert learned.size_report("x" * 10799)["near_cap"] is False
    assert learned.size_report("x" * 10800)["near_cap"] is True


async def test_tool_answers_size_on_success_and_records_a_friction_on_the_cap(monkeypatch):
    """#2697: size and max_chars on success; the cap refusal lists sections and leaves a friction."""
    import contextlib
    import json
    import uuid
    from types import SimpleNamespace

    from app.core import database
    from app.services import frictions
    from app.services.builtin_tools import build_learned_lesson_tool

    agent = SimpleNamespace(id=uuid.uuid4(), is_builtin=True)
    skill = SimpleNamespace(id=uuid.uuid4(), name="Triage learned for Acme", content=_near_full_body(), tags=[])

    class _Db:
        async def get(self, _model, _id):
            return agent

        async def commit(self):
            return None

    @contextlib.asynccontextmanager
    async def _session():
        yield _Db()

    async def _ensure(_db, _agent, *, author_id):
        return skill

    async def _version(_db, _skill, *, author_id, commit_message):
        return SimpleNamespace(version_number=2)

    noted: list = []
    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(learned, "ensure_learned_skill", _ensure)
    monkeypatch.setattr("app.services.skill_versioning.create_skill_version", _version)
    monkeypatch.setattr(frictions, "note_friction", lambda *a, **k: noted.append(a))

    _, executor = build_learned_lesson_tool(agent_id=str(agent.id), user_id=None, run_id="r1")
    record = executor._handlers["record_learned_lesson"]

    refused = await record(title="New topic", text="n" * 300)
    assert refused.startswith("Refused: the skill would pass 12000 characters: it holds 11900 of 12000")
    assert "'Big topic' " in refused and refused.endswith("Nothing was written.")
    assert noted and noted[0][0] == "tool_error" and noted[0][1] == "record_learned_lesson"

    written = json.loads(await record(title="Big topic", text="short", mode="revise"))
    assert written["changed"] is True and written["max_chars"] == 12000
    assert written["size"] == len(skill.content) and written["near_cap"] is False
    assert len(noted) == 1


def test_name_tags_and_freeze():
    assert learned.skill_name("Triage", "Acme") == "Triage learned for Acme"
    assert learned.skill_name("Triage", "") == "Triage learned for this organization"
    assert learned.agent_tag("abc") == "learned-for-agent:abc"

    class _Skill:
        tags = [learned.LEARNED_TAG, learned.FROZEN_TAG]

    assert learned.is_frozen(_Skill())


def test_tool_is_offered_to_built_ins_only(monkeypatch):
    from app.core.config import settings
    from app.services.builtin_tools import LEARNED_LESSON_TOOL, build_learned_lesson_tool, offers_learned_lesson_tool

    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda self: False), raising=False)
    assert offers_learned_lesson_tool(is_builtin=True)
    assert not offers_learned_lesson_tool(is_builtin=False)
    definitions, _ = build_learned_lesson_tool(agent_id="00000000-0000-0000-0000-000000000001", user_id=None)
    assert [d.name for d in definitions] == [LEARNED_LESSON_TOOL]
    assert len(definitions[0].description) <= 300


async def test_learned_skill_is_owned_by_the_company_owner_not_the_author(monkeypatch):
    """Deleting the user whose run wrote the first lesson must not cascade to the skill."""
    import uuid
    from types import SimpleNamespace

    from app.api.v1 import helpers
    from app.services import builtin_customer_layer, organizations, skill_versioning

    owner_id, author_id = uuid.uuid4(), uuid.uuid4()
    versions: list = []

    async def _none(*_a, **_k):
        return None

    async def _owner(_db):
        return owner_id

    async def _version(_db, skill, *, author_id, commit_message):
        versions.append(author_id)

    monkeypatch.setattr(learned, "find_learned_skill", _none)
    monkeypatch.setattr(organizations, "get_primary", _none)
    monkeypatch.setattr(helpers, "resolve_company_managed_owner_id", _owner)
    monkeypatch.setattr(skill_versioning, "create_skill_version", _version)
    monkeypatch.setattr(builtin_customer_layer, "record_customer_fields", lambda *_a: None)

    added: list = []

    async def _flush():
        added[-1].id = uuid.uuid4()

    db = SimpleNamespace(add=added.append, flush=_flush)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Triage", is_builtin=True, skill_ids=[], skill_load_modes={})
    skill = await learned.ensure_learned_skill(db, agent, author_id=author_id)
    assert skill.user_id == owner_id
    assert skill.is_company_managed
    assert versions == [author_id]

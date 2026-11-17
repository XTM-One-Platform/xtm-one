"""The publisher decides whether a catalog download carries their personal
custom tools.

A catalog download is the agent's export bundle, handed to any user of the
instance. It used to carry the code of every custom tool bound to the agent,
the publisher's personal (not company-managed) ones included, while the
one-click install skips those. ``Agent.catalog_include_personal_tools`` is the
publisher's switch: off for a new publish, on for the agents already published
when it was added (today's behaviour). When off, the download leaves those
tools out - their code and the variables only they reference - and says which.
Company-managed tools are always carried.

The route runs through a session double answering each ``select(Model)`` with
the rows whose id the statement binds; the reader is a NON-admin user.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.api.v1 import agent_import as agent_import_api
from app.api.v1 import public_catalog
from app.models.agent import Agent
from app.models.custom_tool import CustomTool
from app.models.variable import Variable
from app.services.agentschema import prepare_agent_import


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, *rows):
        self._rows = list(rows)
        self.added: list[object] = []

    async def execute(self, stmt):
        description = stmt.column_descriptions[0]
        entity = description.get("entity")
        if not isinstance(entity, type) or description.get("expr") is not entity:
            return _Result([])
        wanted: set[str] = set()
        for value in stmt.compile().params.values():
            values = value if isinstance(value, (list, tuple)) else [value]
            wanted.update(str(v) for v in values if isinstance(v, uuid.UUID))
        return _Result(
            row for row in self._rows if isinstance(row, entity) and str(row.id) in wanted
        )

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


_READER = SimpleNamespace(
    id=uuid.uuid4(), email="r@example.com", full_name="Reader", is_admin=False, role="user"
)


def _published(include: bool | None):
    """A published agent with one personal and one company-managed tool; the
    personal one alone names a variable."""
    owner = uuid.uuid4()
    variable = Variable(
        id=uuid.uuid4(), user_id=owner, name="PERSONAL_TOKEN_HOST", value="h", is_secret=False
    )
    personal = CustomTool(
        id=uuid.uuid4(),
        user_id=owner,
        name="My scraper",
        js_function=f"return '{{{{var_id.{variable.id}}}}}'",
        is_company_managed=False,
    )
    shared = CustomTool(
        id=uuid.uuid4(), name="Company lookup", js_function="return 1", is_company_managed=True
    )
    agent = Agent(
        id=uuid.uuid4(),
        user_id=owner,
        name="Published",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        persona="p",
        is_public_catalog=True,
        catalog_include_personal_tools=include,
        custom_tool_ids=[str(personal.id), str(shared.id)],
    )
    return agent, _Session(agent, personal, shared, variable), variable


async def _download(agent, session):
    response = await public_catalog.download_catalog_item(
        entity_type="agents", entity_id=str(agent.id), user=_READER, db=session
    )
    return json.loads(response.body)


async def test_a_new_publish_leaves_the_personal_tools_out_and_says_which():
    agent, session, variable = _published(include=False)

    bundle = await _download(agent, session)

    assert [t["name"] for t in bundle["custom_tools"]] == ["Company lookup"]
    assert bundle["omitted_custom_tools"] == [{"name": "My scraper"}]
    [note] = bundle["download_notes"]
    assert "My scraper" in note
    # The variable only the left-out tool names is not disclosed either.
    assert str(variable.id) not in json.dumps(bundle)
    assert "My scraper" not in json.dumps(bundle["custom_tools"])


async def test_a_publisher_who_includes_them_gets_todays_download():
    agent, session, variable = _published(include=True)

    bundle = await _download(agent, session)

    assert sorted(t["name"] for t in bundle["custom_tools"]) == ["Company lookup", "My scraper"]
    assert "omitted_custom_tools" not in bundle
    assert "download_notes" not in bundle
    assert [v["id"] for v in bundle["variables"]] == [str(variable.id)]


async def test_an_agent_the_backfill_never_reached_keeps_todays_download():
    """NULL only exists when the startup safety net created the column bare
    before its migration: that row never went through the backfill, so it
    reads as "include" - nothing changes silently for its publisher."""
    agent, session, _ = _published(include=None)

    bundle = await _download(agent, session)

    assert "omitted_custom_tools" not in bundle
    assert len(bundle["custom_tools"]) == 2


async def test_the_catalog_page_names_what_the_download_leaves_out():
    agent, session, _ = _published(include=False)

    response = await public_catalog.get_public_agent(
        agent_id=str(agent.id), user=_READER, db=session
    )

    assert json.loads(response.body)["download_omitted_custom_tools"] == ["My scraper"]
    agent.catalog_include_personal_tools = True
    response = await public_catalog.get_public_agent(
        agent_id=str(agent.id), user=_READER, db=session
    )
    assert json.loads(response.body)["download_omitted_custom_tools"] == []


async def test_a_download_with_a_note_still_imports(monkeypatch):
    agent, session, _ = _published(include=False)
    bundle = await _download(agent, session)

    prepared = prepare_agent_import(json.dumps(bundle).encode("utf-8"), filename="agent.json")

    async def _unique_slug(_db, name):
        return "imported"

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_import_api, "generate_unique_agent_slug", _unique_slug)
    monkeypatch.setattr(agent_import_api, "seed_initial_agent_versions", _noop)
    monkeypatch.setattr(agent_import_api, "log_audit", _noop)
    target = _Session()
    await agent_import_api.create_agent_from_bundle(target, user=_READER, bundle=prepared.bundle)

    assert [t.name for t in target.added if isinstance(t, CustomTool)] == ["Company lookup"]

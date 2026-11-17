"""The event type filter and the time order of the KB Activity tab (#9378, slice 4a).

The tab's Event type pill sends ``event_type`` repeated (sync, documents,
embeddings); each type is a set of event name prefixes, matched literally (the
``_`` of a prefix is never a LIKE wildcard). The list orders by time in the
``direction`` asked, newest first by default.
"""

from __future__ import annotations

import inspect
import typing

from sqlalchemy import ColumnElement
from sqlalchemy.dialects import postgresql

from app.api.v1 import knowledge_bases as kb_mod


def _sql(clause: ColumnElement[bool]) -> str:
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    return str(clause.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))


def test_every_event_type_of_the_query_literal_has_its_prefixes() -> None:
    assert set(typing.get_args(kb_mod.ActivityEventType)) == set(kb_mod.ACTIVITY_EVENT_TYPES)


def test_one_type_matches_its_prefix_literally() -> None:
    sql = _sql(kb_mod._activity_event_type_filter(["sync"]))
    assert "kb_activity_logs.event LIKE 'sync/_' ||" in sql
    assert "ESCAPE '/'" in sql


def test_several_types_are_or_ed() -> None:
    sql = _sql(kb_mod._activity_event_type_filter(["documents", "embeddings"]))
    assert "'document/_'" in sql
    assert "'embedding/_'" in sql
    assert "'reprocess/_'" in sql
    assert " OR " in sql
    assert "'sync/_'" not in sql


def test_the_list_and_the_count_take_the_same_event_type_parameter() -> None:
    listed = inspect.signature(kb_mod.list_activity_logs).parameters
    counted = inspect.signature(kb_mod.count_activity_logs).parameters
    assert listed["event_type"].default is None
    assert counted["event_type"].default is None
    assert listed["direction"].default == "desc"

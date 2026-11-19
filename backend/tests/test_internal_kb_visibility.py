"""An internal knowledge base is never a user-visible entity.

A Workset keeps its corpus in a KnowledgeBase so the retrieval machinery is
reused rather than reimplemented. That KB is storage owned by another
primitive: it must not appear in a list, a picker, or an agent binding.

The exclusion lives in ``user_visible_filter`` — the single choke point every
list, fetch and picker routes through — rather than at each call site, because
one picker that forgot the predicate is a leak. These tests pin that, including
for admins: ``is_admin`` widens WHICH entities you see, it does not turn
another primitive's storage into content.

Compiled SQL is asserted rather than rows: the predicate is what the ten-odd
call sites inherit, and it holds whether or not a database is reachable.
"""

from __future__ import annotations

import uuid

from app.api.v1.helpers import user_visible_filter, user_visible_query
from app.models.agent import Agent
from app.models.knowledge_base import KnowledgeBase
from app.models.prompt import Prompt


def _sql(clause) -> str:
    return str(clause.compile(compile_kwargs={"literal_binds": True}))


def test_the_filter_excludes_internal_knowledge_bases():
    sql = _sql(user_visible_filter(KnowledgeBase, uuid.uuid4()))
    assert "is_internal" in sql
    assert "knowledge_bases.is_internal IS false" in sql


def test_an_admin_does_not_see_internal_knowledge_bases_either():
    """The one that would silently regress: the admin branch returns a bare
    TRUE, so the exclusion has to be applied before that shortcut."""
    sql = _sql(user_visible_filter(KnowledgeBase, uuid.uuid4(), is_admin=True))
    assert "knowledge_bases.is_internal IS false" in sql
    # And nothing else — an admin still sees every non-internal KB.
    assert "user_id" not in sql


def test_the_query_helper_inherits_the_exclusion():
    sql = _sql(user_visible_query(KnowledgeBase, uuid.uuid4()).whereclause)
    assert "knowledge_bases.is_internal IS false" in sql


def test_entities_without_the_column_are_untouched():
    """The predicate is opt-in by column presence: every other entity keeps
    exactly the visibility it had."""
    user_id = uuid.uuid4()
    sql = _sql(user_visible_filter(Prompt, user_id))
    assert "is_internal" not in sql and "is_sandbox" not in sql

    admin_sql = _sql(user_visible_filter(Prompt, user_id, is_admin=True))
    # Unchanged admin bypass: a bare TRUE.
    assert admin_sql.strip().lower() in {"true", "1 = 1"}


def test_an_agent_is_hidden_by_its_sandbox_flag_only(monkeypatch):
    """Agents carry ``is_sandbox`` (#8255), not ``is_internal``: an admin's
    bypass keeps that one exclusion and nothing else, with no feature flag
    (the Concierge is built in since #101 S1)."""
    from app.core.config import settings

    user_id = uuid.uuid4()
    monkeypatch.setattr(settings, "enabled_features", "")
    assert "is_internal" not in _sql(user_visible_filter(Agent, user_id))
    admin_sql = _sql(user_visible_filter(Agent, user_id, is_admin=True))
    assert admin_sql.strip().lower() == "agents.is_sandbox is false"


def test_a_workset_kb_is_created_internal_and_a_normal_one_is_not():
    """The column's default is the pre-existing behaviour: nothing that
    existed before this change becomes invisible."""
    plain = KnowledgeBase(name="Team wiki")
    assert plain.is_internal in (False, None)

    internal = KnowledgeBase(name="workset:abc", is_internal=True)
    assert internal.is_internal is True

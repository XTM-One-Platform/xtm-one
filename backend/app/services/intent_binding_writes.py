# SPDX-License-Identifier: Apache-2.0

"""The single writer of intent bindings.

``PUT /intents/{id}/bind`` and ``DELETE /intents/{id}/bind/{binding_id}``
(``api/v1/intents.py``) and the agent tools ``bind_agent_to_intent`` /
``unbind_agent_from_intent`` (``builtin_tools/intent_bindings.py``) write
through here, so a gate is decided once:

* an administrator writes the global binding (``user_id`` NULL), anyone else
  a personal override of their own;
* a global binding targets a company-managed or group-shared agent only: a
  private agent bound globally would be offered to the users of a connected
  platform through ``GET /intents/catalog``;
* a personal override of a locked global binding is refused;
* the lock is an administrator's switch.

A refusal is :class:`IntentBindingRefusedError`, carrying the HTTP status and the
message the REST route answers; the caller commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, NamedTuple

from sqlalchemy import ColumnElement, case, func, or_, select, text

from app.models.intent import IntentBinding

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent
    from app.models.intent import Intent
    from app.models.user import User

PRIVATE_AGENT_GLOBAL_BINDING = (
    "Cannot create a global intent binding to a private agent. "
    "The agent must be company-managed or shared with at least one "
    "group before it can be exposed via intents to external platforms."
)
LOCKED_BINDING = "This intent binding is locked by an admin and cannot be overridden."
NOT_YOURS = "Cannot delete this binding"
BUILTIN_BINDING = "This intent binding is a built-in default and cannot be changed."


class IntentBindingRefusedError(Exception):
    """A write the gates refuse: *status* and *detail* as the REST route answers."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def agent_is_shared(agent: Agent) -> bool:
    """Whether *agent* may be bound globally (company-managed or group-shared)."""
    return bool(agent.is_company_managed) or bool(agent.shared_group_ids)


def _vertical_clause(vertical: str | None) -> ColumnElement[bool]:
    return IntentBinding.vertical.is_(None) if vertical is None else IntentBinding.vertical == vertical


async def upsert_intent_binding(
    db: AsyncSession,
    user: User,
    intent: Intent,
    agent: Agent,
    *,
    vertical: str | None = None,
    priority: int = 0,
    is_locked: bool = False,
    priority_override_vertical: str | None = None,
    refuse_builtin: bool = False,
) -> IntentBinding:
    """Create or update the binding of *agent* to *intent* for *vertical*.

    The caller has checked that *user* may see *agent*. *refuse_builtin*
    refuses to change a built-in default binding (the agent tools; the REST
    route keeps its behaviour).
    """
    binding_user_id = None if user.is_admin else user.id

    if binding_user_id is None and not agent_is_shared(agent):
        raise IntentBindingRefusedError(422, PRIVATE_AGENT_GLOBAL_BINDING)

    if binding_user_id is not None:
        locked = await db.execute(
            select(IntentBinding).where(
                IntentBinding.intent_id == intent.id,
                _vertical_clause(vertical),
                IntentBinding.user_id.is_(None),
                IntentBinding.is_locked.is_(True),
            )
        )
        if locked.scalars().first() is not None:
            raise IntentBindingRefusedError(403, LOCKED_BINDING)

    locked_flag = is_locked if user.is_admin else False

    # Serialize concurrent identical upserts with a transaction advisory lock so
    # the check-then-insert below is atomic. Without it two simultaneous upserts
    # for the same (intent, vertical, user, agent) both miss the existence check
    # and insert duplicate bindings (there is no unique constraint to catch it).
    scope = f"intent_bind:{intent.id}:{agent.id}:{vertical}:{binding_user_id}"
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:scope))"), {"scope": scope})

    user_clause = (
        IntentBinding.user_id.is_(None) if binding_user_id is None else IntentBinding.user_id == binding_user_id
    )
    existing = await db.execute(
        select(IntentBinding).where(
            IntentBinding.intent_id == intent.id,
            _vertical_clause(vertical),
            user_clause,
            IntentBinding.agent_id == agent.id,
        )
    )
    binding = existing.scalar_one_or_none()

    if binding is not None:
        if refuse_builtin and binding.is_builtin:
            raise IntentBindingRefusedError(403, BUILTIN_BINDING)
        if priority_override_vertical:
            updated = dict(binding.priority_overrides or {})
            updated[priority_override_vertical] = priority
            binding.priority_overrides = updated
        else:
            binding.priority = priority
        binding.is_locked = locked_flag
        return binding

    overrides: dict[str, int] = {}
    if priority_override_vertical:
        overrides[priority_override_vertical] = priority
    binding = IntentBinding(
        intent_id=intent.id,
        agent_id=agent.id,
        vertical=vertical,
        user_id=binding_user_id,
        priority=priority if not priority_override_vertical else 0,
        priority_overrides=overrides or None,
        is_locked=locked_flag,
    )
    db.add(binding)
    return binding


def check_intent_binding_delete(user: User, binding: IntentBinding, *, refuse_builtin: bool = False) -> None:
    """Refuse the removal of *binding* by *user*: anyone but an administrator
    removes only their own overrides; *refuse_builtin* keeps built-in defaults."""
    if not user.is_admin and binding.user_id != user.id:
        raise IntentBindingRefusedError(403, NOT_YOURS)
    if refuse_builtin and binding.is_builtin:
        raise IntentBindingRefusedError(403, BUILTIN_BINDING)


# ── Which binding wins ──────────────────────────────────────────────────────────

#: The ranking rule of ``GET /intents/catalog``, said the same way everywhere.
WINNER_RULE = (
    "the higher effective priority wins (a vertical's priority override replaces the base "
    "priority in that vertical); on a tie the binding made first wins"
)


def effective_priority(binding: IntentBinding, vertical: str | None) -> int:
    """The priority *binding* ranks with in *vertical*: its override there, else its base."""
    overrides = binding.priority_overrides or {}
    if vertical and vertical in overrides:
        return int(overrides[vertical])
    return int(binding.priority or 0)


def ranking_key(binding: IntentBinding, vertical: str | None) -> tuple[int, bool, float, str]:
    """Sort key, first is the winner: highest effective priority, then oldest binding, then id.

    The tie rule makes the order stable: a later binding at the same priority
    never takes the place of the one already serving.
    """
    created = getattr(binding, "created_at", None)
    return (
        -effective_priority(binding, vertical),
        created is None,
        created.timestamp() if created is not None else 0.0,
        str(binding.id),
    )


def rank_bindings[Row](rows: Iterable[Row], vertical: str | None, *, key: Callable[[Row], IntentBinding]) -> list[Row]:
    """*rows* in catalog order for *vertical*; *key* reads the binding of a row."""
    return sorted(rows, key=lambda row: ranking_key(key(row), vertical))


def agent_shared_scope_filter() -> ColumnElement[bool]:
    """SQL predicate: an agent is company-managed OR shared with >=1 group.

    The intent catalog is consumed by external platforms (OpenCTI, OpenAEV)
    which surface agents to *their* users, so private agents (not
    company-managed and not group-shared) must NEVER appear regardless of who
    queries it — otherwise a private agent bound to an intent leaks to all
    platform users. The catalog route and :func:`intent_winners` share it, so
    the winner is drawn from the rows the catalog serves.

    Robustness against malformed JSON: ``shared_group_ids`` is a JSON column
    whose "not shared" value should be SQL NULL or an empty array ``[]``, but
    historically every ``= None`` write path persisted the JSON ``null``
    *scalar* (the generic ``JSON`` type encodes ``None`` as ``'null'`` unless
    ``none_as_null=True``). ``jsonb_array_length`` raises
    ``cannot get array length of a scalar`` on any JSON scalar/``null``, which
    crashed this endpoint in production. We therefore only take the array
    length when ``json_typeof`` confirms an array; every other shape (NULL,
    JSON ``null``, scalar, object) counts as "not shared". This stays correct
    even if a non-API write path reintroduces a scalar value.

    The ``json`` functions, not the ``jsonb`` ones: they read the stored text
    without de-escaping it, so one agent's ``shared_group_ids`` holding a lone
    surrogate escape cannot abort the catalog for everyone, and they
    answer exactly what the ``jsonb`` pair answers on every other row.
    """
    from app.models.agent import Agent

    shared = Agent.shared_group_ids
    shared_len = case(
        (func.json_typeof(shared) == "array", func.json_array_length(shared)),
        else_=0,
    )
    return or_(
        Agent.is_company_managed.is_(True),
        shared_len > 0,
    )


class IntentWinner(NamedTuple):
    vertical: str | None
    binding: IntentBinding
    agent_id: uuid.UUID
    agent_name: str
    priority: int


async def intent_winners(
    db: AsyncSession,
    user: User,
    intent_id: uuid.UUID,
    verticals: Iterable[str | None],
) -> dict[str | None, IntentWinner | None]:
    """The binding the catalog serves first for *intent_id*, per vertical (``None``: no vertical filter).

    The candidates are the catalog's for *user*: enabled agents, company-managed
    or group-shared, visible to a non-administrator; in a vertical, the
    bindings of that vertical and those of every vertical.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.agent import Agent

    query = (
        select(IntentBinding, Agent.name)
        .join(Agent, IntentBinding.agent_id == Agent.id)
        .where(
            IntentBinding.intent_id == intent_id,
            Agent.enabled.is_(True),
            agent_shared_scope_filter(),
        )
    )
    if not user.is_admin:
        group_ids = await get_user_group_ids(db, user.id)
        query = query.where(user_visible_filter(Agent, user.id, group_ids))
    rows = (await db.execute(query)).all()
    out: dict[str | None, IntentWinner | None] = {}
    for vertical in dict.fromkeys(verticals):
        candidates = [r for r in rows if vertical is None or r[0].vertical in (None, vertical)]
        ranked = rank_bindings(candidates, vertical, key=lambda r: r[0])
        if not ranked:
            out[vertical] = None
            continue
        top, name = ranked[0][0], ranked[0][1]
        out[vertical] = IntentWinner(vertical, top, top.agent_id, str(name), effective_priority(top, vertical))
    return out


def intent_verticals(bindings: Iterable[IntentBinding]) -> list[str | None]:
    """``None`` (no vertical filter) then every vertical the bindings name or override."""
    names: set[str] = set()
    for b in bindings:
        if b.vertical:
            names.add(b.vertical)
        names.update(k for k in (b.priority_overrides or {}) if k)
    return [None, *sorted(names)]


def binding_owner_label(binding: IntentBinding, viewer_id: uuid.UUID | None) -> str:
    """``global`` for an administrator's binding, ``yours`` for the viewer's
    own override, ``personal`` for another person's."""
    if binding.user_id is None:
        return "global"
    return "yours" if viewer_id is not None and binding.user_id == viewer_id else "personal"

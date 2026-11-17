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

"""Agent accounts from chat: the agent-tool half.

A person asks their assistant to "let my Executive Assistant run its mailbox",
"stop answering in #general", "only answer my team in direct messages",
"pause my delegate this weekend": these tools are the platform's own
agent-account surface (Profile > Agent Accounts), offered two ways:

* in the ``agent_builder`` suite (the General Assistant, any agent carrying
  it, ``/mcp/xtm``): every account of the caller, every account for an
  administrator's agent (``agent_tool_admin_bypass``), as the REST routes;
* to an agent that operates agent accounts of the person talking to it
  (*agent_id*): the accounts it operates for that person only, so it can read
  and change the rules it answers under without carrying the whole builder.

None is offered while someone else than its owner writes to an account: that
turn runs as the account's owner, but the person asking is not the owner
(``agent_chat`` withholds :data:`AGENT_ACCOUNT_TOOL_NAMES` by name under
``identity_from_platform``, so ``load_tools`` and a call say why). The owner
writing to their own account, as the provider's directory identifies them
(``agent_accounts.pipeline.sender_is_owner``), gets them as in a chat.

Nothing here decides a rule. Every write goes through
``services/agent_accounts/management.py``, the REST routes' writer, which
refuses what they refuse, and the request bodies are the REST schemas, so a
bound is checked once. Writes are audited like the REST ones, marked
``via: agent``.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from app.core.utils import safe_uuid
from app.services import inbound_claims
from app.tools.base import ToolDefinition

from . import agent_builder as _builder
from ._shared import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api.v1.schemas.agent_account import AgentAccountResponse, AgentAccountUpdate
    from app.models.agent_account import AgentAccount
    from app.models.user import User

LIST_TOOL = "list_agent_accounts"
GET_TOOL = "get_agent_account"
OPTIONS_TOOL = "list_agent_account_options"
CREATE_TOOL = "create_agent_account"
UPDATE_TOOL = "update_agent_account"
RULE_TOOL = "edit_agent_account_rule"
DELETE_TOOL = "delete_agent_account"
PREVIEW_TOOL = "preview_agent_account_decision"
DECISIONS_TOOL = "list_agent_account_decisions"

#: Every tool of the family: none is offered while someone else than its owner writes to an account.
AGENT_ACCOUNT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        LIST_TOOL,
        GET_TOOL,
        OPTIONS_TOOL,
        CREATE_TOOL,
        UPDATE_TOOL,
        RULE_TOOL,
        DELETE_TOOL,
        PREVIEW_TOOL,
        DECISIONS_TOOL,
    }
)
#: What an agent configuring the accounts it operates is offered: a new account
#: needs a sign-in as another platform account, set up with the builder or the page.
OWN_ACCOUNT_TOOL_NAMES: frozenset[str] = AGENT_ACCOUNT_TOOL_NAMES - {CREATE_TOOL, OPTIONS_TOOL}

_ANONYMOUS_REFUSAL = "Agent accounts are not available for anonymous users."
_NOT_FOUND_HINT = f"Call `{LIST_TOOL}` to see the accounts you can manage."
#: Decisions one call lists at most.
MAX_DECISIONS_PER_CALL = 50
#: The longest message preview or reply a decision line quotes.
_QUOTE_CHARS = 300

_MODES = {
    "on_behalf": "delegate (answers for its owner, as them)",
    "ai_user": "AI user (the account is the agent, an AI colleague)",
}
_RESPONSE_MODES = {
    "auto": "sent automatically",
    "approval": "held for the owner's approval",
    "shadow": "shadow: recorded, never sent",
}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_RULE_OPERATIONS = {"add": "rule added", "update": "rule updated", "remove": "rule removed", "move": "rule moved"}


def _shown(text: str | None, limit: int = 120) -> str:
    """A name someone else chose (a channel, a sender), on one bounded line."""
    from app.services.content_safety import single_line

    return single_line(text or "")[:limit]


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def _validation_message(exc: Exception) -> str:
    """A request body the REST schema refuses, one clause per fault."""
    from pydantic import ValidationError

    if not isinstance(exc, ValidationError):
        return f"Invalid arguments: {exc}"
    faults = []
    for error in exc.errors()[:5]:
        where = ".".join(str(part) for part in error.get("loc", ()))
        faults.append(f"{where or 'value'}: {error.get('msg', 'invalid')}")
    return "Invalid arguments - " + "; ".join(faults) + "."


def _json_arg(value: Any, label: str, kind: type) -> tuple[Any, str | None]:
    """An object or list argument, given as such or as its JSON text."""
    if value is None or isinstance(value, kind):
        return value, None
    if isinstance(value, str):
        if not value.strip():
            return None, None
        try:
            value = json.loads(value)
        except ValueError:
            return None, f"{label} is not valid JSON."
        if isinstance(value, kind):
            return value, None
    return None, f"{label} must be a JSON {'list' if kind is list else 'object'}."


def _hours(schedule: dict[str, Any] | None) -> str:
    if not schedule:
        return "always"
    windows = []
    for window in schedule.get("windows") or []:
        days = ",".join(_WEEKDAYS[d - 1] for d in window.get("days") or [] if 1 <= d <= 7)
        windows.append(f"{days} {window.get('start')}-{window.get('end')}")
    where = "only inside" if schedule.get("mode", "inside") == "inside" else "only outside"
    return f"{where} {'; '.join(windows)} ({schedule.get('timezone') or 'UTC'})"


def _summary_line(a: AgentAccountResponse, uid: uuid.UUID) -> str:
    parts = [
        a.provider_label or a.provider,
        _MODES.get(a.mode, a.mode).split(" (")[0],
        f"agent {a.agent_name or 'none'}",
        "enabled" if a.enabled else "disabled",
        f"status {a.status}",
        f"replies {_RESPONSE_MODES.get(a.response_mode, a.response_mode)}",
        f"{len(a.rules)} rule(s)",
    ]
    if a.pending_count:
        parts.append(f"{a.pending_count} reply(ies) waiting for approval")
    if a.user_id != str(uid):
        parts.append(f"owner {a.owner_name or a.user_id}")
    return f"- **{a.name}** (id: `{a.id}`): " + ", ".join(parts)


def _describe(a: AgentAccountResponse, uid: uuid.UUID) -> str:
    """One account as a tool answers it: every setting, and the rules as stored."""
    from app.services.agent_accounts.providers import get_provider

    provider = get_provider(a.provider)
    signed_in = a.account_display_name or "not read yet"
    if a.account_email:
        signed_in += f" <{a.account_email}>"
    label = f'on, "{a.ai_label_preview}" ({a.ai_label_position})' if a.ai_label_enabled else "off"
    lines = [
        f"**{a.name}** (id: `{a.id}`): {'enabled' if a.enabled else 'disabled'}, status {a.status}",
        f"  - Platform: {a.provider_label or a.provider} (`{a.provider}`); mode: {_MODES.get(a.mode, a.mode)}",
        f"  - Signed in as: {signed_in}",
        f"  - Integration: {a.integration_name or 'none'}" + (f" (`{a.integration_id}`)" if a.integration_id else ""),
        f"  - Agent: {a.agent_name or 'none'}" + (f" (`{a.agent_id}`)" if a.agent_id else ""),
    ]
    if a.user_id != str(uid):
        lines.append(f"  - Owner: {a.owner_name or a.user_id}")
    if a.description:
        lines.append(f"  - Description: {a.description}")
    lines += [
        f"  - Replies: {_RESPONSE_MODES.get(a.response_mode, a.response_mode)}; AI label {label}",
        f"  - Grace period {a.grace_period_seconds} s; steps back {a.owner_takeover_minutes} min after the owner "
        f"writes; at most {a.max_replies_per_hour or 'unlimited'} replies per conversation and hour; "
        f"reads the last {a.context_messages} messages",
        f"  - Active hours: {_hours(a.active_hours)}",
        f"  - Owner notified of each reply sent automatically: {'yes' if a.notify_on_send else 'no'}",
        f"  - Reactions as feedback: {_compact(a.reaction_feedback)}",
    ]
    if provider is not None and provider.capabilities.triage:
        lines.append(f"  - Mailbox triage actions: {_compact(a.mailbox_actions)}")
    if provider is not None and provider.capabilities.signature:
        signature = a.signature_source
        if signature == "integration":
            signature += f" ({a.signature_integration_name or a.signature_integration_id})"
        lines.append(f"  - Signature: {signature}")
    lines.append(f"  - Account instructions: {a.instructions or 'none'}")
    lines.append(
        f"  - Listening: {a.transport}, {a.subscription_count} subscription(s); last message heard "
        f"{a.last_event_at or 'never'}; last reply {a.last_reply_at or 'never'}; "
        f"{a.pending_count} reply(ies) waiting for approval"
    )
    if a.subscription_expires_at:
        lines.append(f"  - Push subscription expires {a.subscription_expires_at} (renewed a day before)")
    if a.read_cursor:
        lines.append(f"  - {(a.read_cursor_label or 'read cursor').capitalize()}: {a.read_cursor}")
    report = a.sync_report
    if report is not None:
        lines.append(f"  - Last sync {report.checked_at or 'unknown'}: hears {report.heard} of {report.wanted}")
        for problem in report.problems:
            targets = ", ".join(_shown(t.name or t.covers, 80) for t in problem.targets[:5])
            extra = "".join(
                f"; {key} {value}"
                for key, value in (("permission", problem.permission), ("held by", problem.holder))
                if value
            )
            lines.append(f"    - {problem.kind} x{problem.count}{extra}" + (f": {targets}" if targets else ""))
            # The provider's or the platform's own words: "integration x1" alone says nothing.
            lines.extend(f"      - {_shown(detail, 300)}" for detail in problem.details[:3])
    if a.last_error:
        lines.append(f"  - Last error: {_shown(a.last_error, 300)}")
    lines.append("")
    if a.rules:
        lines.append("Rules, in order (the first enabled rule whose scope and trigger match decides), as stored:")
        lines.extend(f"  {i}. {_compact(rule)}" for i, rule in enumerate(a.rules, start=1))
    else:
        lines.append("No rules: the account answers nothing.")
    return "\n".join(lines)


def _decision_line(entry: dict[str, Any]) -> str:
    d = entry["decision"]
    where = d.conversation_type + (f" '{_shown(d.conversation_name, 80)}'" if d.conversation_name else "")
    if entry.get("kind") == "proactive":
        origin = entry.get("origin") or {}
        produced = ", ".join(
            f"{key} {_shown(str(origin[key]), 80)}"
            for key in ("tool", "source", "assignment_id", "run_id", "conversation_id")
            if origin.get(key)
        )
        to = ", ".join(_shown(str(r), 80) for r in (origin.get("recipients") or [])[:10])
        line = (
            f"- {d.created_at:%Y-%m-%d %H:%M} UTC, {where}, proactive send ({entry['outcome']})"
            + (f" to {to}" if to else "")
            + f": \"{_shown(entry.get('reply_text') or d.preview, _QUOTE_CHARS)}\""
        )
        if produced:
            line += f"; origin: {produced}"
        if entry.get("reply_error"):
            line += f"; error: {_shown(entry['reply_error'], 200)}"
        return line
    line = (
        f"- {d.created_at:%Y-%m-%d %H:%M} UTC, {where}, from {_shown(d.sender_name, 80) or 'someone'}: "
        f"\"{_shown(d.preview, _QUOTE_CHARS)}\" -> {entry['group']} ({entry['outcome']})"
    )
    if d.rule_name or d.rule_id:
        line += f", rule '{_shown(d.rule_name or d.rule_id, 80)}'"
    if getattr(d, "holder", None):
        # Who took the mail, an assignment run or another agent account of the agent.
        line += f"; taken by {inbound_claims.describe_holder(d.holder)}"
    if getattr(d, "trace_id", None):
        # The turn that answered it, its own or the one it was merged into.
        line += f"; turn trace {d.trace_id}"
    if d.note:
        line += f"; the agent's note: {_shown(d.note, 200)}"
    if entry.get("reply_text"):
        line += f"; reply: \"{_shown(entry['reply_text'], _QUOTE_CHARS)}\""
    if entry.get("reply_error"):
        line += f"; error: {_shown(entry['reply_error'], 200)}"
    return line


def _given(**values: Any) -> dict[str, Any]:
    """The arguments the model passed: an omitted one is left as it is."""
    return {key: value for key, value in values.items() if value is not None}


def edited_rules(
    rules: list[dict[str, Any]], operation: str, *, rule_id: str, rule: dict[str, Any] | None, position: int | None
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """*rules* with one rule added, changed, removed or moved; or why not.

    *position* is 1-based. ``update`` merges *rule* over the stored rule, its
    id kept. The result is normalized by the engine when it is saved.
    """
    out = [dict(r) for r in rules]
    ids = [str(r.get("id")) for r in out]
    known = f"Rule ids: {', '.join(ids) or 'none'}."
    if operation == "add":
        if not rule:
            return None, "rule is required to add one: its fields (name, conversation_types, trigger, action, ...)."
        index = len(out) if position is None else min(position, len(out) + 1) - 1
        out.insert(index, dict(rule))
        return out, None
    if not rule_id:
        return None, f"rule_id is required to {operation} a rule. {known}"
    if rule_id not in ids:
        return None, f"No rule `{rule_id[:100]}` on this account. {known}"
    index = ids.index(rule_id)
    current = out.pop(index)
    if operation == "remove":
        return out, None
    if operation == "update":
        if not rule:
            return None, "rule is required to update one: the fields to change."
        current = {**current, **rule, "id": current.get("id")}
    elif position is None:
        return None, "position is required to move a rule (1 is the first rule)."
    out.insert(index if position is None else min(position, len(out) + 1) - 1, current)
    return out, None


def build_agent_account_tools(
    user_id: str, *, agent_id: str | None = None
) -> tuple[list[ToolDefinition], dict[str, object]]:
    """The agent-account tools, as definitions plus the handlers to register.

    *agent_id* scopes them to the accounts that agent operates for *user_id*
    (the agent configuring itself): creating one and its options are left
    out, and the admin bypass never applies.
    """
    own_agent = safe_uuid(agent_id) if agent_id else None
    source = "builtin:agent_accounts" if own_agent else "builtin:platform"
    whose = "the agent accounts you operate for the user" if own_agent else "the user's agent accounts"

    def _caller_id() -> uuid.UUID | None:
        return safe_uuid(user_id) if user_id else None

    async def _caller(_db: AsyncSession) -> tuple[User | None, str | None]:
        uid = _caller_id()
        if uid is None:
            return None, _ANONYMOUS_REFUSAL
        user = await _builder._load_caller_user_for_builder(_db, uid)
        return (user, None) if user is not None else (None, "User not found.")

    def _admin_reach(user: User) -> bool:
        from app.api.v1.helpers import agent_tool_admin_bypass

        return own_agent is None and agent_tool_admin_bypass(user)

    async def _resolve(_db: AsyncSession, user: User, ref: str) -> tuple[AgentAccount | None, str | None]:
        """The account *ref* names - an id, an 8+ hex id prefix, or the exact name of one of the caller's own."""
        from sqlalchemy import func, select

        from app.models.agent_account import AgentAccount
        from app.services.agent_accounts import management

        from .id_prefix import resolve_id_prefix

        ref = (ref or "").strip()
        if not ref:
            return None, "account_id is required (an agent account id, or its exact name)."
        # The prefix a listing prints (``ca8be20b``), looked up among the accounts
        # this caller reaches: the owner's, an administrator's agent's, or the agent's own.
        reach = [] if _admin_reach(user) else [AgentAccount.user_id == user.id]
        if own_agent is not None:
            reach.append(AgentAccount.agent_id == own_agent)
        ref, refused = await resolve_id_prefix(_db, AgentAccount, ref, reach, noun="agent accounts")
        if refused:
            return None, refused
        parsed = safe_uuid(ref)
        if parsed is not None:
            try:
                account = await management.load_account(_db, parsed, user, is_admin=_admin_reach(user))
            except management.AccountRefusedError:
                account = None
            if account is None or (
                own_agent is not None and (account.user_id != user.id or account.agent_id != own_agent)
            ):
                return None, f"Agent account `{ref[:80]}` not found. {_NOT_FOUND_HINT}"
            return account, None
        query = select(AgentAccount).where(
            AgentAccount.user_id == user.id, func.lower(AgentAccount.name) == ref.casefold()
        )
        if own_agent is not None:
            query = query.where(AgentAccount.agent_id == own_agent)
        rows = list((await _db.execute(query)).scalars().all())
        if not rows:
            return None, f"No agent account named '{ref[:200]}'. {_NOT_FOUND_HINT}"
        if len(rows) > 1:
            listed = ", ".join(f"`{a.id}`" for a in rows[:10])
            return None, f"Several agent accounts are named '{ref[:200]}': {listed}. Name the one you mean by its id."
        return rows[0], None

    async def _described(_db: AsyncSession, account: AgentAccount, user: User) -> str:
        from app.services.agent_accounts import management

        return _describe((await management.serialize(_db, [account], user))[0], user.id)

    async def _apply(_db: AsyncSession, user: User, account: AgentAccount, body: AgentAccountUpdate, done: str) -> str:
        from app.services.agent_accounts import management

        try:
            await management.update_account(_db, account, user, body, via="agent")
        except management.AccountRefusedError as exc:
            await _db.rollback()
            return f"Nothing was changed: {exc.detail}"
        except Exception:
            logger.exception("Agent tool update of agent account %s failed", account.id)
            await _db.rollback()
            return "Failed to update the agent account. Please try again."
        return f"Agent account **{account.name}** {done}.\n\n{await _described(_db, account, user)}"

    # -- list / get ---------------------------------------------------------------

    async def _list_agent_accounts(scope: str = "mine") -> str:
        from sqlalchemy import select

        from app.core.database import async_session
        from app.models.agent_account import AgentAccount
        from app.services.agent_accounts import management

        if _caller_id() is None:
            return _ANONYMOUS_REFUSAL
        async with async_session() as db:
            user, err = await _caller(db)
            if user is None:
                return err or _ANONYMOUS_REFUSAL
            everyone = str(scope or "").strip().lower() == "all" and _admin_reach(user)
            query = select(AgentAccount).order_by(AgentAccount.created_at.desc())
            if not everyone:
                query = query.where(AgentAccount.user_id == user.id)
            if own_agent is not None:
                query = query.where(AgentAccount.agent_id == own_agent)
            accounts = list((await db.execute(query)).scalars().all())
            if not accounts:
                if own_agent is not None:
                    return "You operate no agent account for the user."
                return f"No agent account yet. Create one with `{CREATE_TOOL}` (see `{OPTIONS_TOOL}`)."
            served = await management.serialize(db, accounts, user)
        if own_agent is not None:
            head = f"{len(served)} agent account(s) you operate for the user:"
        else:
            head = f"{len(served)} agent account(s){' on the platform' if everyone else ''}:"
        return "\n".join(
            [head, *(_summary_line(a, user.id) for a in served), f"Read one with `{GET_TOOL}`: its settings and rules."]
        )

    async def _get_agent_account(account_id: str) -> str:
        from app.core.database import async_session

        if _caller_id() is None:
            return _ANONYMOUS_REFUSAL
        async with async_session() as db:
            user, err = await _caller(db)
            if user is None:
                return err or _ANONYMOUS_REFUSAL
            account, err = await _resolve(db, user, account_id)
            if account is None:
                return err or _NOT_FOUND_HINT
            return await _described(db, account, user)

    # -- options ------------------------------------------------------------------

    async def _list_agent_account_options(provider: str = "") -> str:
        from app.core.database import async_session
        from app.services.agent_accounts import management
        from app.services.agent_accounts.listening import push_reachable
        from app.services.agent_accounts.providers import providers
        from app.services.agent_accounts.rules import MODES, preset

        if _caller_id() is None:
            return _ANONYMOUS_REFUSAL
        wanted = (provider or "").strip()
        catalog = [p for p in providers().values() if not wanted or p.key == wanted]
        if not catalog:
            return f"Unknown platform '{wanted[:50]}'. Known: {', '.join(providers())}."
        parts = ["Platforms an agent account can operate:"]
        if not push_reachable():
            parts.append("(No public HTTPS address: chats and inboxes are polled every minute, channels are not heard.)")
        async with async_session() as db:
            user, err = await _caller(db)
            if user is None:
                return err or _ANONYMOUS_REFUSAL
            for p in catalog:
                capabilities = ", ".join(name for name, on in vars(p.capabilities).items() if on)
                parts.append(f"\n**{p.label}** (provider `{p.key}`, {p.kind}): {capabilities}")
                for mode in MODES:
                    base = preset(mode, p.kind)
                    parts.append(
                        f"  - mode `{mode}` starts with replies {_RESPONSE_MODES[base['response_mode']]}, AI label "
                        f"{'on' if base['ai_label_enabled'] else 'off'}, rules: "
                        + "; ".join(r["name"] for r in base["rules"])
                    )
                usable = await management.usable_integrations(db, user, p)
                if usable:
                    parts.append(f"  - {p.integration_provider} integrations you can use (sign in as the account):")
                    parts.extend(f"    - {i.name} (id: `{i.id}`, status {i.status})" for i in usable)
                else:
                    parts.append(
                        f"  - No {p.integration_provider} integration you can use: connect one signed in as the "
                        "account (Integrations > New integration) first."
                    )
        parts.append(
            "\nThe agent must be one the user can see (`list_agents`). One enabled agent account per platform "
            "account, and per integration and platform."
        )
        return "\n".join(parts)

    # -- create / update / delete ---------------------------------------------------

    async def _create_agent_account(
        name: str,
        provider: str,
        integration_id: str,
        agent_id: str,
        mode: str = "on_behalf",
        enabled: object = None,
        description: str | None = None,
        response_mode: str | None = None,
        ai_label_enabled: object = None,
        ai_label_text: str | None = None,
        ai_label_position: str | None = None,
        grace_period_seconds: object = None,
        owner_takeover_minutes: object = None,
        max_replies_per_hour: object = None,
        context_messages: object = None,
        instructions: str | None = None,
        notify_on_send: object = None,
        active_hours: object = None,
        rules: object = None,
    ) -> str:
        from app.api.v1.schemas.agent_account import AgentAccountCreate
        from app.core.database import async_session
        from app.services.agent_accounts import management

        if _caller_id() is None:
            return _ANONYMOUS_REFUSAL
        parsed: dict[str, Any] = {}
        for label, raw, kind in (("active_hours", active_hours, dict), ("rules", rules, list)):
            parsed[label], err = _json_arg(raw, label, kind)
            if err:
                return err
        try:
            body = AgentAccountCreate(
                **_given(
                    name=name,
                    provider=provider,
                    integration_id=integration_id,
                    agent_id=agent_id,
                    mode=mode or "on_behalf",
                    enabled=enabled,
                    description=description,
                    response_mode=response_mode,
                    ai_label_enabled=ai_label_enabled,
                    ai_label_text=ai_label_text,
                    ai_label_position=ai_label_position,
                    grace_period_seconds=grace_period_seconds,
                    owner_takeover_minutes=owner_takeover_minutes,
                    max_replies_per_hour=max_replies_per_hour,
                    context_messages=context_messages,
                    instructions=instructions,
                    notify_on_send=notify_on_send,
                    **parsed,
                )
            )
        except Exception as exc:
            return _validation_message(exc)
        async with async_session() as db:
            user, err = await _caller(db)
            if user is None:
                return err or _ANONYMOUS_REFUSAL
            try:
                account = await management.create_account(db, user, body, via="agent")
            except management.AccountRefusedError as exc:
                await db.rollback()
                return f"Nothing was created: {exc.detail}"
            except Exception:
                logger.exception("Agent tool create_agent_account failed for user %s", user.id)
                await db.rollback()
                return "Failed to create the agent account. Please try again."
            state = (
                "It sets itself up in the background: it reads who the integration is signed in as, subscribes "
                f"to the conversations its rules reach, then listens. Check it with `{GET_TOOL}` in a minute."
                if account.enabled
                else f"It is disabled: enable it with `{UPDATE_TOOL}` (enabled=true) when it is ready."
            )
            return f"Agent account **{account.name}** created. {state}\n\n{await _described(db, account, user)}"

    async def _update_agent_account(
        account_id: str,
        name: str | None = None,
        description: str | None = None,
        enabled: object = None,
        agent_id: str | None = None,
        integration_id: str | None = None,
        mode: str | None = None,
        response_mode: str | None = None,
        ai_label_enabled: object = None,
        ai_label_text: str | None = None,
        ai_label_position: str | None = None,
        grace_period_seconds: object = None,
        owner_takeover_minutes: object = None,
        max_replies_per_hour: object = None,
        context_messages: object = None,
        instructions: str | None = None,
        notify_on_send: object = None,
        active_hours: object = None,
        clear_active_hours: object = None,
        rules: object = None,
        mailbox_actions: object = None,
        signature_source: str | None = None,
        signature_integration_id: str | None = None,
        reaction_feedback: object = None,
    ) -> str:
        from app.api.v1.schemas.agent_account import AgentAccountUpdate
        from app.core.database import async_session

        if _caller_id() is None:
            return _ANONYMOUS_REFUSAL
        parsed: dict[str, Any] = {}
        for label, raw, kind in (
            ("active_hours", active_hours, dict),
            ("rules", rules, list),
            ("mailbox_actions", mailbox_actions, dict),
            ("reaction_feedback", reaction_feedback, dict),
        ):
            parsed[label], err = _json_arg(raw, label, kind)
            if err:
                return err
        try:
            body = AgentAccountUpdate(
                **_given(
                    name=name,
                    description=description,
                    enabled=enabled,
                    agent_id=agent_id,
                    integration_id=integration_id,
                    mode=mode,
                    response_mode=response_mode,
                    ai_label_enabled=ai_label_enabled,
                    ai_label_text=ai_label_text,
                    ai_label_position=ai_label_position,
                    grace_period_seconds=grace_period_seconds,
                    owner_takeover_minutes=owner_takeover_minutes,
                    max_replies_per_hour=max_replies_per_hour,
                    context_messages=context_messages,
                    instructions=instructions,
                    notify_on_send=notify_on_send,
                    clear_active_hours=clear_active_hours,
                    signature_source=signature_source,
                    signature_integration_id=signature_integration_id,
                    **parsed,
                )
            )
        except Exception as exc:
            return _validation_message(exc)
        if not body.model_fields_set:
            return "Nothing to change: pass the settings to change."
        async with async_session() as db:
            user, err = await _caller(db)
            if user is None:
                return err or _ANONYMOUS_REFUSAL
            account, err = await _resolve(db, user, account_id)
            if account is None:
                return err or _NOT_FOUND_HINT
            return await _apply(db, user, account, body, "updated")

    async def _edit_agent_account_rule(
        account_id: str,
        operation: str,
        rule_id: str = "",
        rule: object = None,
        position: object = None,
    ) -> str:
        from app.api.v1.schemas.agent_account import AgentAccountUpdate
        from app.core.database import async_session

        if _caller_id() is None:
            return _ANONYMOUS_REFUSAL
        operation = str(operation or "").strip().lower()
        if operation not in _RULE_OPERATIONS:
            return f"operation must be one of {', '.join(_RULE_OPERATIONS)}."
        rule, err = _json_arg(rule, "rule", dict)
        if err:
            return err
        where: int | None = None
        if position not in (None, ""):
            try:
                where = int(position)
            except (TypeError, ValueError):
                return "position must be a whole number (1 is the first rule)."
            if where < 1:
                return "position must be 1 or more (1 is the first rule)."
        async with async_session() as db:
            user, err = await _caller(db)
            if user is None:
                return err or _ANONYMOUS_REFUSAL
            account, err = await _resolve(db, user, account_id)
            if account is None:
                return err or _NOT_FOUND_HINT
            # Edits of one account's rules run one after the other: the row is locked
            # until the save commits and the rules are read again under the lock, so a
            # concurrent edit is kept instead of being overwritten by a stale copy.
            await db.refresh(account, with_for_update=True)
            rules, err = edited_rules(
                list(account.rules or []), operation, rule_id=str(rule_id or "").strip(), rule=rule, position=where
            )
            if rules is None:
                return err or "Nothing was changed."
            return await _apply(
                db, user, account, AgentAccountUpdate(rules=rules), f"saved ({_RULE_OPERATIONS[operation]})"
            )

    async def _delete_agent_account(account_id: str) -> str:
        from app.core.database import async_session
        from app.services.agent_accounts import management

        if _caller_id() is None:
            return _ANONYMOUS_REFUSAL
        async with async_session() as db:
            user, err = await _caller(db)
            if user is None:
                return err or _ANONYMOUS_REFUSAL
            account, err = await _resolve(db, user, account_id)
            if account is None:
                return err or _NOT_FOUND_HINT
            name = account.name
            try:
                await management.delete_account(db, account, user, via="agent")
            except Exception:
                logger.exception("Agent tool delete_agent_account failed for %s", account.id)
                await db.rollback()
                return "Failed to delete the agent account. Please try again."
        return (
            f"Agent account **{name}** deleted: it no longer listens nor answers, and its waiting approvals, "
            "replies and activity log are gone. Its integration and its agent are kept."
        )

    # -- preview / decisions ----------------------------------------------------------

    async def _preview_agent_account_decision(
        account_id: str,
        text: str = "Hello",
        conversation_type: str = "direct",
        conversation_id: str = "",
        sender_email: str | None = None,
        sender_id: str = "",
        mentions_account: object = False,
        from_application: object = False,
        thread_active: object = False,
        owner_active: object = False,
        replies_last_hour: object = 0,
        rules: object = None,
    ) -> str:
        from app.api.v1.schemas.agent_account import SimulatedEvent, SimulatedState, SimulateRequest
        from app.core.database import async_session
        from app.services.agent_accounts import management

        if _caller_id() is None:
            return _ANONYMOUS_REFUSAL
        rules, err = _json_arg(rules, "rules", list)
        if err:
            return err
        try:
            event = SimulatedEvent(
                conversation_type=conversation_type or "direct",
                conversation_id=conversation_id or "",
                sender_id=sender_id or "",
                sender_email=sender_email or None,
                text=text or "Hello",
                mentions_account=mentions_account or False,
                from_application=from_application or False,
            )
            state = SimulatedState(
                thread_active=thread_active or False,
                owner_active=owner_active or False,
                replies_last_hour=replies_last_hour or 0,
            )
        except Exception as exc:
            return _validation_message(exc)
        async with async_session() as db:
            user, err = await _caller(db)
            if user is None:
                return err or _ANONYMOUS_REFUSAL
            account, err = await _resolve(db, user, account_id)
            if account is None:
                return err or _NOT_FOUND_HINT
            draft = management.saved_settings(account)
            if rules is not None:
                draft = draft.model_copy(update={"rules": rules})
            body = SimulateRequest(settings=draft, mode=account.mode, provider=account.provider, event=event, state=state)
            try:
                result = await management.simulate(db, body)
            except management.AccountRefusedError as exc:
                return f"Cannot preview: {exc.detail}"
            name = account.name
        d = result.decision
        tested = "the proposed rules" if rules is not None else "its saved rules"
        rule = f"rule '{d.get('rule_name') or d.get('rule_id')}' (id `{d.get('rule_id')}`)" if d.get("rule_id") else None
        if d.get("respond"):
            lines = [
                f"With {tested}, **{name}** would answer this message, under {rule or 'a rule'}.",
                "  - The agent " + ("must reply." if d.get("must_reply") else "decides whether to reply."),
                f"  - Reply {_RESPONSE_MODES.get(d.get('response_mode'), d.get('response_mode'))}; AI label "
                f"{'on' if d.get('ai_label') else 'off'}; grace period {d.get('grace_period_seconds', 0)} s.",
                "  - Access: " + (_compact(d["access"]) if d.get("access") else "the agent's whole set."),
            ]
            if d.get("thread_continuation"):
                lines.append("  - A follow-up in a thread the account answered in.")
        else:
            lines = [f"With {tested}, **{name}** would leave this message: reason `{d.get('reason')}`"
                     + (f", {rule}" if rule else "") + "."]
        if result.sender is not None:
            lines.append(f"  - Sender: {_compact(result.sender)}")
        lines.append(f"  - Listening: {_compact(result.listening)}")
        lines.append("Nothing was sent or saved.")
        return "\n".join(lines)

    async def _list_agent_account_decisions(
        account_id: str,
        group: str = "",
        conversation_type: str = "",
        sender: str = "",
        search: str = "",
        limit: object = 20,
        offset: object = 0,
        kind: str = "",
    ) -> str:
        from app.core.database import async_session
        from app.services.agent_accounts import activity
        from app.services.content_safety import wrap_untrusted

        if _caller_id() is None:
            return _ANONYMOUS_REFUSAL
        try:
            page = max(1, min(int(limit or 20), MAX_DECISIONS_PER_CALL))
            skip = max(0, int(offset or 0))
        except (TypeError, ValueError):
            return "limit and offset must be whole numbers."
        group = str(group or "").strip()
        if group and group not in activity.GROUPS:
            return f"group must be one of {', '.join(activity.GROUPS)}."
        kind = str(kind or "").strip().lower()
        if kind and kind not in ("reply", "proactive"):
            return "kind must be reply, proactive, or empty for both."
        async with async_session() as db:
            user, err = await _caller(db)
            if user is None:
                return err or _ANONYMOUS_REFUSAL
            account, err = await _resolve(db, user, account_id)
            if account is None:
                return err or _NOT_FOUND_HINT
            filters = activity.DecisionFilters(
                group=group or None,
                conversation_type=str(conversation_type or "").strip() or None,
                sender=str(sender or "").strip() or None,
                text=str(search or "").strip() or None,
                kind=kind or None,
            )
            items, total = await activity.list_decisions(db, account.id, filters, limit=page, offset=skip)
            name, key = account.name, account.id
        if not items:
            return f"No message of **{name}** matches (the log keeps {activity.RETENTION_DAYS} days)."
        head = (
            f"Activity log of **{name}**, newest first: {skip + 1}-{skip + len(items)} of {total} "
            f"(the log keeps {activity.RETENTION_DAYS} days): the messages it heard with its replies, and the "
            "messages the agent sent through it on its own (proactive sends). Quoted as written."
        )
        more = f"\nNext page: offset={skip + len(items)}." if skip + len(items) < total else ""
        listing = "\n".join(_decision_line(entry) for entry in items)
        return f"{head}\n{wrap_untrusted(listing, f'activity log of agent account {key}')}{more}"

    definitions = _definitions(source=source, whose=whose, account_ref="The agent account's id, or its exact name.")
    handlers: dict[str, object] = {
        LIST_TOOL: _list_agent_accounts,
        GET_TOOL: _get_agent_account,
        OPTIONS_TOOL: _list_agent_account_options,
        CREATE_TOOL: _create_agent_account,
        UPDATE_TOOL: _update_agent_account,
        RULE_TOOL: _edit_agent_account_rule,
        DELETE_TOOL: _delete_agent_account,
        PREVIEW_TOOL: _preview_agent_account_decision,
        DECISIONS_TOOL: _list_agent_account_decisions,
    }
    if own_agent is not None:
        definitions = [d for d in definitions if d.name in OWN_ACCOUNT_TOOL_NAMES]
        handlers = {name: handler for name, handler in handlers.items() if name in OWN_ACCOUNT_TOOL_NAMES}
    return definitions, handlers


_PEOPLE: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "A platform id, an email address, or a domain written @example.com."},
            "name": {"type": "string"},
            "email": {"type": "string"},
        },
        "required": ["id"],
    },
}

#: One rule as ``rules.normalize_rule`` reads it; every field is optional.
_RULE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Generated when omitted."},
        "name": {"type": "string"},
        "enabled": {"type": "boolean", "description": "Default true."},
        "action": {"type": "string", "enum": ["respond", "ignore"], "description": "ignore stops there."},
        "conversation_types": {
            "type": "array",
            "items": {"type": "string", "enum": ["direct", "group", "channel"]},
            "description": (
                "Empty = all. Mailbox: direct = to the account alone, group = other recipients too."
            ),
        },
        "conversations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "team_id": {"type": "string"},
                },
                "required": ["id"],
            },
            "description": "Specific chats or channels (empty = any of the types).",
        },
        "senders": {**_PEOPLE, "description": "Only these people (or domains); with groups, either one."},
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
                "required": ["id"],
            },
            "description": "XTM One groups whose members the rule covers (list_groups).",
        },
        "exclude_senders": {**_PEOPLE, "description": "People (or domains) this rule never answers."},
        "trigger": {
            "type": "string",
            "enum": ["mention", "any"],
            "description": "mention: only when addressed (a direct message counts; To for a mailbox); any: every message.",
        },
        "reply": {"type": "string", "enum": ["always", "decide"], "description": "decide: the agent may stay silent."},
        "continue_threads": {
            "type": "boolean",
            "description": "Follow-ups in a thread the account answered in count as addressed.",
        },
        "include_automated": {"type": "boolean"},
        "response_mode": {
            "type": "string",
            "enum": ["auto", "approval", "shadow"],
            "description": "Overrides the account's.",
        },
        "ai_label": {"type": "boolean", "description": "Overrides the account's AI label."},
        "grace_period_seconds": {"type": "integer", "description": "Overrides the account's (0-3600)."},
        "instructions": {"type": "string", "description": "Guardrails and instructions for this audience."},
        "access": {
            "type": "object",
            "description": (
                "What the agent may use for these messages, each a subset of its own: integrations (providers), "
                "knowledge_base_ids, mcp_server_ids, builtin_tool_ids, custom_tool_ids, skill_ids (null = all), "
                "memory on/off."
            ),
        },
    },
}

_SETTINGS_PROPERTIES: dict[str, Any] = {
    "description": {"type": "string"},
    "response_mode": {
        "type": "string",
        "enum": ["auto", "approval", "shadow"],
        "description": "auto: sent at once; approval: the owner approves each reply; shadow: recorded, never sent.",
    },
    "ai_label_enabled": {"type": "boolean", "description": "Add the AI label to every reply."},
    "ai_label_text": {"type": "string", "description": "The label; {name} is the account's name. Empty: the default."},
    "ai_label_position": {"type": "string", "enum": ["prefix", "suffix"]},
    "grace_period_seconds": {"type": "integer", "description": "Wait before answering (0-3600)."},
    "owner_takeover_minutes": {"type": "integer", "description": "Stay out after the owner writes (0-1440)."},
    "max_replies_per_hour": {"type": "integer", "description": "Per conversation (0 = no limit)."},
    "context_messages": {"type": "integer", "description": "Earlier messages read (0-50)."},
    "instructions": {"type": "string", "description": "Added to the agent's persona for every reply of the account."},
    "notify_on_send": {"type": "boolean", "description": "Notify the owner of each reply sent automatically."},
    "active_hours": {
        "type": "object",
        "description": (
            '{"timezone": "Europe/Paris", "mode": "inside" or "outside", "windows": '
            '[{"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}]} (day 1 is Monday).'
        ),
    },
    "rules": {
        "type": "array",
        "items": _RULE_SCHEMA,
        "description": f"The FULL ordered rule list, replacing the current one; for one rule, use `{RULE_TOOL}`.",
    },
}


def _definitions(*, source: str, whose: str, account_ref: str) -> list[ToolDefinition]:
    from app.services.agent_accounts.providers import providers

    registered = providers().values()
    labels = ", ".join(p.label for p in registered)
    keys = ", ".join(p.key for p in registered)
    about = (
        f"An agent account is an agent operating a messaging or mail account ({labels}) through its own "
        "sign-in: as a delegate for its owner, or as an AI user. Its ordered rules decide which messages it "
        "answers and how."
    )
    reference = {"account_id": {"type": "string", "description": account_ref}}
    return [
        ToolDefinition(
            name=LIST_TOOL,
            description=f"List {whose}: platform, mode, agent, status, response mode, rules. {about}",
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["mine", "all"],
                        "description": "all: every user's account (administrators only).",
                        "default": "mine",
                    },
                },
                "required": [],
            },
            source=source,
        ),
        ToolDefinition(
            name=GET_TOOL,
            description=(
                "Get one agent account: whose account it is and as whom it speaks, its reply settings, schedule, "
                "instructions, its rules in order as stored, and whether it hears its conversations (sync problems)."
            ),
            input_schema={"type": "object", "properties": reference, "required": ["account_id"]},
            source=source,
        ),
        ToolDefinition(
            name=OPTIONS_TOOL,
            description=(
                f"What creating an agent account needs: the platforms ({labels}), what each supports, the "
                "starting settings of each mode, and the integrations the user may use for each."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "description": "One platform key; empty for all.", "default": ""},
                },
                "required": [],
            },
            source=source,
        ),
        ToolDefinition(
            name=CREATE_TOOL,
            description=(
                f"Create an agent account owned by the user. {about} It starts from its mode's preset "
                f"(`{OPTIONS_TOOL}`); a setting given overrides it. The integration must be signed in as the "
                "account: the user's own sign-in for a delegate, the service account's for an AI user."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "provider": {"type": "string", "description": f"The platform: {keys}."},
                    "integration_id": {"type": "string"},
                    "agent_id": {"type": "string", "description": "The agent that answers."},
                    "mode": {"type": "string", "enum": ["on_behalf", "ai_user"], "default": "on_behalf"},
                    "enabled": {"type": "boolean", "description": "Default true: it starts listening."},
                    **_SETTINGS_PROPERTIES,
                },
                "required": ["name", "provider", "integration_id", "agent_id"],
            },
            source=source,
        ),
        ToolDefinition(
            name=UPDATE_TOOL,
            description=(
                "Change an agent account: enable or disable it (enabled), its reply settings, label, schedule, "
                "instructions, agent, integration, mode, or its whole rule list. Only the fields given change; "
                "disabling stops it listening at once."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **reference,
                    "name": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "agent_id": {"type": "string"},
                    "integration_id": {"type": "string"},
                    "mode": {"type": "string", "enum": ["on_behalf", "ai_user"]},
                    **_SETTINGS_PROPERTIES,
                    "clear_active_hours": {"type": "boolean", "description": "Answer at any time again."},
                    "mailbox_actions": {
                        "type": "object",
                        "description": (
                            "A mailbox's triage filing: flag_needs_owner, notify_needs_owner, mark_read_replied, "
                            "mark_read_no_reply (booleans), category_replied, category_needs_owner, "
                            "category_no_reply (text, empty = none)."
                        ),
                    },
                    "signature_source": {"type": "string", "enum": ["own", "integration", "none"]},
                    "signature_integration_id": {"type": "string"},
                    "reaction_feedback": {
                        "type": "object",
                        "description": "enabled (boolean), positive and negative (lists of emoji).",
                    },
                },
                "required": ["account_id"],
            },
            source=source,
        ),
        ToolDefinition(
            name=RULE_TOOL,
            description=(
                "Add, change, remove or move ONE rule of an agent account; the others are kept. Rules are read "
                "top to bottom and the first enabled one whose scope and trigger match decides, so order is the "
                "policy (an ignore rule first excludes a conversation or a person). update merges the fields "
                "given over the rule's (a list given replaces the rule's list); enabled=false pauses a rule. "
                f"Check the result with `{PREVIEW_TOOL}`."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **reference,
                    "operation": {"type": "string", "enum": list(_RULE_OPERATIONS)},
                    "rule_id": {"type": "string", "description": "The rule to update, remove or move."},
                    "rule": {**_RULE_SCHEMA, "description": "add: the new rule; update: the fields to change."},
                    "position": {"type": "integer", "description": "1-based place to add or move it to."},
                },
                "required": ["account_id", "operation"],
            },
            source=source,
        ),
        ToolDefinition(
            name=DELETE_TOOL,
            description=(
                "Delete an agent account: it stops listening, and its waiting approvals, replies and activity log "
                "go with it. Cannot be undone: confirm with the user first (disabling keeps everything)."
            ),
            input_schema={"type": "object", "properties": reference, "required": ["account_id"]},
            source=source,
        ),
        ToolDefinition(
            name=PREVIEW_TOOL,
            description=(
                "What an agent account would do with a sample message, with its saved rules or proposed ones: "
                "which rule matches, whether and how the agent answers, or why the message is left. Nothing is "
                "sent or saved."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **reference,
                    "text": {"type": "string", "default": "Hello"},
                    "conversation_type": {"type": "string", "enum": ["direct", "group", "channel"], "default": "direct"},
                    "conversation_id": {"type": "string", "description": "A chat or channel id a rule names."},
                    "sender_email": {"type": "string", "description": "Tests people, domain and group rules."},
                    "sender_id": {"type": "string"},
                    "mentions_account": {"type": "boolean", "default": False},
                    "from_application": {"type": "boolean", "default": False},
                    "thread_active": {"type": "boolean", "description": "The account answered in this thread lately."},
                    "owner_active": {"type": "boolean", "description": "The owner wrote in this conversation lately."},
                    "replies_last_hour": {"type": "integer", "default": 0},
                    "rules": {"type": "array", "items": _RULE_SCHEMA, "description": "Proposed rules to test instead."},
                },
                "required": ["account_id"],
            },
            source=source,
        ),
        ToolDefinition(
            name=DECISIONS_TOOL,
            description=(
                "The activity log of an agent account, newest first: it holds every message sent through the "
                "account, not only the replies. Each message it heard, what it did with it and why (rule, reason, the "
                "agent's note) and the reply it produced (kind reply), and each message the agent sent through the "
                "account on its own from a chat, an assignment or a tool call, with its recipients and origin (kind "
                "proactive), failed sends included: everything the AI sent in the owner's name. How to answer "
                "'why did you not answer X?' and 'what did you send?'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **reference,
                    "group": {
                        "type": "string",
                        "description": (
                            "answered, needs_owner, awaiting_approval, shadow, dropped, silent, waiting, "
                            "already_handled (another pipeline of the agent took the mail first, the line names "
                            "it), left (not for the agent), failed; empty for all."
                        ),
                        "default": "",
                    },
                    "conversation_type": {"type": "string", "default": ""},
                    "sender": {"type": "string", "description": "A sender's platform id or name.", "default": ""},
                    "search": {"type": "string", "description": "Words of the message or a name.", "default": ""},
                    "kind": {
                        "type": "string",
                        "enum": ["", "reply", "proactive"],
                        "description": (
                            "reply: the messages the account heard and their replies; proactive: the messages the "
                            "agent sent on its own; empty for both."
                        ),
                        "default": "",
                    },
                    "limit": {"type": "integer", "default": 20},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["account_id"],
            },
            source=source,
        ),
    ]

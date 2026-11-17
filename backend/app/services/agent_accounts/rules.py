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

"""The agent-account rule engine: pure, provider-agnostic, the ONE place rules mean anything.

An account's ``rules`` are an ordered list evaluated on every message the
provider normalizes into an :class:`~.events.AccountEvent`; the first rule
whose scope (conversation types, conversations, senders) and trigger
(mentioned, or every message) match decides. Its ``action`` either ignores
the message or answers it with the account's defaults, overridden where the
rule says so (response mode, AI label, grace period, instructions).

What the engine cannot know by itself - whether the account answered in this
thread recently, whether its human owner just wrote there, how many replies it
sent this hour - is read by the pipeline and handed in as an
:class:`EventState`, so every decision here is a function of its inputs and
:func:`evaluate` is what the simulator, the pipeline and the tests all call.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import time as dtime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.agent_accounts.events import CONVERSATION_TYPES, AccountEvent

if TYPE_CHECKING:
    from app.services.agent_accounts.providers import ProviderCapabilities

MODES = ("on_behalf", "ai_user")
#: What a provider's conversations are (``AccountProvider.kind``): chats, or the emails of a mailbox.
KINDS = ("chat", "mailbox")
RESPONSE_MODES = ("auto", "approval", "shadow")
TRIGGERS = ("mention", "any")
REPLY_POLICIES = ("always", "decide")
ACTIONS = ("respond", "ignore")
LABEL_POSITIONS = ("prefix", "suffix")
ACTIVE_HOURS_MODES = ("inside", "outside")
#: What a rule's ``access`` may narrow, each a subset of the agent's own.
ACCESS_LISTS = (
    "integrations",
    "knowledge_base_ids",
    "mcp_server_ids",
    "builtin_tool_ids",
    "custom_tool_ids",
    "skill_ids",
)
MEMORY_MODES = ("on", "off")

MAX_RULES = 50
MAX_TARGETS = 200
MAX_INSTRUCTIONS_CHARS = 4000
MAX_LABEL_CHARS = 300
MAX_GRACE_SECONDS = 3600
MAX_TAKEOVER_MINUTES = 24 * 60
MAX_REPLIES_PER_HOUR = 1000
MAX_CONTEXT_MESSAGES = 50
#: The agent turns a mailbox runs per hour, every email together: one
#: sender writing new emails starts a new conversation with each.
MAILBOX_TURNS_PER_HOUR = 60
#: The people a reply may reach besides the sender before it is left to the owner.
DEFAULT_REPLY_RECIPIENTS = 10

#: The label written when the account sets none; ``{name}`` is the account's name.
DEFAULT_LABEL = {
    "on_behalf": "Sent by an AI agent on behalf of {name}",
    "ai_user": "Sent by an AI agent",
}

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class RuleError(ValueError):
    """A rule, a schedule or a setting the engine cannot apply, said in words."""


# ---------------------------------------------------------------------------
# Normalization: what is stored is what the engine reads
# ---------------------------------------------------------------------------


def _choice(value: Any, allowed: tuple[str, ...], what: str, default: str | None = None) -> str:
    if value is None or value == "":
        if default is None:
            raise RuleError(f"{what} is required ({', '.join(allowed)}).")
        return default
    if value not in allowed:
        raise RuleError(f"{what} must be one of {', '.join(allowed)}, not {value!r}.")
    return value


def _optional_choice(value: Any, allowed: tuple[str, ...], what: str) -> str | None:
    if value is None or value == "":
        return None
    return _choice(value, allowed, what)


def _bounded_int(value: Any, what: str, low: int, high: int, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise RuleError(f"{what} is required.")
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuleError(f"{what} must be a whole number.")
    if not low <= value <= high:
        raise RuleError(f"{what} must be between {low} and {high}.")
    return value


def _text(value: Any, what: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RuleError(f"{what} must be text.")
    value = value.strip()
    if len(value) > limit:
        raise RuleError(f"{what} is longer than {limit} characters.")
    return value


def _targets(value: Any, what: str, *, keys: tuple[str, ...]) -> list[dict[str, str]]:
    """A list of ``{id, name, ...}`` picks, de-duplicated by id, each id kept as text."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise RuleError(f"{what} must be a list.")
    if len(value) > MAX_TARGETS:
        raise RuleError(f"{what} holds more than {MAX_TARGETS} entries.")
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict):
            raise RuleError(f"Every entry of {what} must name an id.")
        target_id = item.get("id") or item.get("email")
        if not isinstance(target_id, str) or not target_id.strip():
            raise RuleError(f"Every entry of {what} must name an id.")
        key = target_id.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        entry = {"id": target_id.strip()}
        for extra in keys:
            raw = item.get(extra)
            if isinstance(raw, str) and raw.strip():
                entry[extra] = raw.strip()[:500]
        out.append(entry)
    return out


def _access(raw: Any, where: str) -> dict[str, Any] | None:
    """A rule's ``access`` as stored: ``None`` is the agent's full set, a list a subset of it.

    Ids and providers are not checked against the agent here - the agent may
    change after the rule is saved - but intersected with its own at every
    turn, so access can only narrow what the agent has, never widen it.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RuleError(f"{where}: the access settings must be an object.")
    unknown = sorted(set(raw) - set(ACCESS_LISTS) - {"memory"})
    if unknown:
        raise RuleError(f"{where}: unknown access setting {unknown[0]!r}.")
    out: dict[str, Any] = {}
    for key in ACCESS_LISTS:
        value = raw.get(key)
        if value is None:
            out[key] = None
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise RuleError(f"{where}: {key.replace('_', ' ')} must be a list of ids.")
        if len(value) > MAX_TARGETS:
            raise RuleError(f"{where}: {key.replace('_', ' ')} holds more than {MAX_TARGETS} entries.")
        out[key] = list(dict.fromkeys(v.strip() for v in value))
    out["memory"] = _choice(raw.get("memory"), MEMORY_MODES, f"{where}: the memory setting", "on")
    if out["memory"] == "on" and all(out[key] is None for key in ACCESS_LISTS):
        return None
    return out


def provider_conversation_types(capabilities: ProviderCapabilities | None) -> tuple[str, ...]:
    """The conversation types a provider declares (``capabilities.direct`` / ``group`` / ``channel``), in order."""
    if capabilities is None:
        return CONVERSATION_TYPES
    return tuple(kind for kind in CONVERSATION_TYPES if getattr(capabilities, kind, False))


def _check_capabilities(
    rule: dict[str, Any], capabilities: ProviderCapabilities | None, where: str
) -> None:
    """Refuse what the provider cannot serve: a rule naming it would be stored and never match."""
    if capabilities is None:
        return
    supported = provider_conversation_types(capabilities)
    for kind in rule["conversation_types"]:
        if kind not in supported:
            raise RuleError(f"{where}: this platform has no {kind} conversations.")
    if not rule["conversations"]:
        return
    if not capabilities.conversations:
        raise RuleError(f"{where}: on this platform a rule cannot name specific conversations.")
    for target in rule["conversations"]:
        kind = target.get("type")
        if kind in CONVERSATION_TYPES and kind not in supported:
            raise RuleError(f"{where}: this platform has no {kind} conversations.")


def normalize_rule(
    raw: Any, index: int = 0, *, capabilities: ProviderCapabilities | None = None
) -> dict[str, Any]:
    """One rule as stored, every field present and checked; raises :class:`RuleError`.

    With the *capabilities* of the account's provider, a conversation type or
    a named conversation the provider cannot serve is refused, and a rule
    naming no type covers the provider's own types.
    """
    if not isinstance(raw, dict):
        raise RuleError(f"Rule {index + 1} must be an object.")
    where = f"Rule {index + 1}"
    types = raw.get("conversation_types")
    if types is None or types == []:
        conversation_types: list[str] = list(provider_conversation_types(capabilities))
    elif isinstance(types, list):
        conversation_types = []
        for kind in types:
            conversation_types.append(_choice(kind, CONVERSATION_TYPES, f"{where}: a conversation type"))
        conversation_types = [t for t in CONVERSATION_TYPES if t in conversation_types]
    else:
        raise RuleError(f"{where}: conversation types must be a list.")
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        rule_id = uuid.uuid4().hex[:12]
    grace = raw.get("grace_period_seconds")
    label = raw.get("ai_label")
    if label is not None and not isinstance(label, bool):
        raise RuleError(f"{where}: the AI label override must be true, false or empty.")
    rule = {
        "id": rule_id.strip()[:100],
        "name": _text(raw.get("name"), f"{where}: the name", 200),
        "enabled": raw.get("enabled", True) is not False,
        "action": _choice(raw.get("action"), ACTIONS, f"{where}: the action", "respond"),
        "conversation_types": conversation_types,
        "conversations": _targets(
            raw.get("conversations"), f"{where}: conversations", keys=("name", "type", "team_id")
        ),
        "senders": _targets(raw.get("senders"), f"{where}: people", keys=("name", "email")),
        "groups": _targets(raw.get("groups"), f"{where}: groups", keys=("name",)),
        "exclude_senders": _targets(
            raw.get("exclude_senders"), f"{where}: excluded people", keys=("name", "email")
        ),
        "trigger": _choice(raw.get("trigger"), TRIGGERS, f"{where}: the trigger", "mention"),
        "reply": _choice(raw.get("reply"), REPLY_POLICIES, f"{where}: the reply policy", "always"),
        "continue_threads": raw.get("continue_threads", True) is not False,
        # Opt-in: the rule also hears what an application wrote (a newsletter,
        # an invitation sent through a mailing platform) from the senders it names.
        "include_automated": raw.get("include_automated") is True,
        "response_mode": _optional_choice(raw.get("response_mode"), RESPONSE_MODES, f"{where}: the response mode"),
        "ai_label": label,
        "grace_period_seconds": (
            None if grace is None else _bounded_int(grace, f"{where}: the grace period", 0, MAX_GRACE_SECONDS)
        ),
        "instructions": _text(raw.get("instructions"), f"{where}: the instructions", MAX_INSTRUCTIONS_CHARS),
        "access": _access(raw.get("access"), where),
    }
    _check_capabilities(rule, capabilities, where)
    return rule


def normalize_rules(raw: Any, *, capabilities: ProviderCapabilities | None = None) -> list[dict[str, Any]]:
    """The rules as stored, in order; raises :class:`RuleError` naming the first fault.

    *capabilities* are those of the account's provider: the API passes them, so
    a rule the provider cannot serve is refused rather than never matching.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RuleError("Rules must be a list.")
    if len(raw) > MAX_RULES:
        raise RuleError(f"An account holds at most {MAX_RULES} rules.")
    rules = [normalize_rule(item, i, capabilities=capabilities) for i, item in enumerate(raw)]
    ids = [r["id"] for r in rules]
    if len(set(ids)) != len(ids):
        raise RuleError("Two rules share one id.")
    return rules


def _hhmm(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _HHMM.match(value):
        raise RuleError(f"{what} must be a time written HH:MM.")
    return value


def normalize_active_hours(raw: Any) -> dict[str, Any] | None:
    """The weekly schedule as stored, ``None`` for always; raises :class:`RuleError`."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise RuleError("Active hours must be an object.")
    zone = raw.get("timezone") or "UTC"
    if not isinstance(zone, str):
        raise RuleError("The active hours' timezone must be text.")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise RuleError(f"Unknown timezone {zone!r}.") from None
    windows_raw = raw.get("windows")
    if not isinstance(windows_raw, list) or not windows_raw:
        raise RuleError("Active hours need at least one window.")
    if len(windows_raw) > 28:
        raise RuleError("Active hours hold at most 28 windows.")
    windows = []
    for i, window in enumerate(windows_raw):
        if not isinstance(window, dict):
            raise RuleError(f"Window {i + 1} must be an object.")
        days = window.get("days")
        if not isinstance(days, list) or not days or not all(
            isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 7 for d in days
        ):
            raise RuleError(f"Window {i + 1}: days are ISO weekdays, 1 (Monday) to 7 (Sunday).")
        start = _hhmm(window.get("start"), f"Window {i + 1}: the start")
        end = _hhmm(window.get("end"), f"Window {i + 1}: the end")
        if start == end:
            raise RuleError(f"Window {i + 1} starts and ends at the same time.")
        windows.append({"days": sorted(set(days)), "start": start, "end": end})
    return {
        "timezone": zone,
        "mode": _choice(raw.get("mode"), ACTIVE_HOURS_MODES, "The active hours' mode", "inside"),
        "windows": windows,
    }


def _clock(value: str) -> dtime:
    hours, minutes = value.split(":")
    return dtime(int(hours), int(minutes))


def within_active_hours(schedule: dict[str, Any] | None, now: datetime) -> bool:
    """Whether the account may answer at *now* under *schedule* (``None``: always).

    A window whose end is before its start runs past midnight: its days are
    the days it starts on.
    """
    if not schedule:
        return True
    local = now.astimezone(ZoneInfo(schedule.get("timezone") or "UTC"))
    today = local.isoweekday()
    yesterday = 7 if today == 1 else today - 1
    clock = local.time().replace(second=0, microsecond=0)
    inside = False
    for window in schedule.get("windows") or []:
        start, end = _clock(window["start"]), _clock(window["end"])
        days = window["days"]
        if start < end:
            if today in days and start <= clock < end:
                inside = True
        elif (today in days and clock >= start) or (yesterday in days and clock < end):
            inside = True
        if inside:
            break
    return inside if schedule.get("mode", "inside") == "inside" else not inside


# ---------------------------------------------------------------------------
# Presets: the two modes' starting points
# ---------------------------------------------------------------------------


def preset(mode: str, kind: str = "chat") -> dict[str, Any]:
    """The defaults an account of *mode* on a provider of *kind* starts from; every field stays editable."""
    _choice(mode, MODES, "The mode")
    _choice(kind, KINDS, "The kind of platform")
    if kind == "mailbox":
        return _mailbox_preset(mode)
    if mode == "ai_user":
        return {
            "response_mode": "auto",
            "ai_label_enabled": False,
            "grace_period_seconds": 0,
            "owner_takeover_minutes": 0,
            "max_replies_per_hour": 30,
            "max_turns_per_hour": 0,
            "max_reply_recipients": DEFAULT_REPLY_RECIPIENTS,
            "context_messages": 20,
            "rules": normalize_rules(
                [
                    {
                        "id": "direct",
                        "name": "Direct messages",
                        "conversation_types": ["direct"],
                        "trigger": "any",
                        "reply": "always",
                    },
                    {
                        "id": "mentions",
                        "name": "Mentions in groups and channels",
                        "conversation_types": ["group", "channel"],
                        "trigger": "mention",
                        "reply": "always",
                    },
                ]
            ),
        }
    return {
        "response_mode": "approval",
        "ai_label_enabled": True,
        "grace_period_seconds": 120,
        "owner_takeover_minutes": 60,
        "max_replies_per_hour": 10,
        "max_turns_per_hour": 0,
        "max_reply_recipients": DEFAULT_REPLY_RECIPIENTS,
        "context_messages": 20,
        "rules": normalize_rules(
            [
                {
                    "id": "direct",
                    "name": "Direct messages",
                    "conversation_types": ["direct"],
                    "trigger": "any",
                    "reply": "always",
                },
                {
                    "id": "mentions",
                    "name": "Mentions",
                    "conversation_types": ["group", "channel"],
                    "trigger": "mention",
                    "reply": "always",
                },
            ]
        ),
    }


def _mailbox_preset(mode: str) -> dict[str, Any]:
    """A mailbox answers the emails sent to it, alone or in ``To``; ``Cc`` is for its information.

    The agent decides each one: many emails need no answer, and the ones it
    cannot answer go to the owner (``triage.py``).
    """
    delegate = mode == "on_behalf"
    return {
        "response_mode": "approval" if delegate else "auto",
        "ai_label_enabled": delegate,
        "grace_period_seconds": 120 if delegate else 0,
        "owner_takeover_minutes": 60 if delegate else 0,
        "max_replies_per_hour": 10 if delegate else 30,
        "max_turns_per_hour": MAILBOX_TURNS_PER_HOUR,
        "max_reply_recipients": DEFAULT_REPLY_RECIPIENTS,
        "context_messages": 10,
        "rules": normalize_rules(
            [
                {
                    "id": "direct",
                    "name": "Emails to me alone" if delegate else "Emails to the account alone",
                    "conversation_types": ["direct"],
                    "trigger": "any",
                    "reply": "decide",
                },
                {
                    "id": "to",
                    "name": "Emails where I am in To" if delegate else "Emails where the account is in To",
                    "conversation_types": ["group"],
                    "trigger": "mention",
                    "reply": "decide",
                },
            ]
        ),
    }


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountSettings:
    """What the engine reads of an account, detached from the ORM row."""

    enabled: bool = True
    mode: str = "on_behalf"
    response_mode: str = "approval"
    ai_label_enabled: bool = True
    grace_period_seconds: int = 0
    owner_takeover_minutes: int = 0
    max_replies_per_hour: int = 20
    #: Agent turns per hour, every conversation together; 0 = no limit.
    max_turns_per_hour: int = 0
    active_hours: dict[str, Any] | None = None
    instructions: str = ""
    rules: list[dict[str, Any]] = field(default_factory=list)
    has_agent: bool = True

    @classmethod
    def of(cls, account: Any) -> AccountSettings:
        """The settings of an ``AgentAccount`` row (or anything with its attributes)."""
        return cls(
            enabled=bool(getattr(account, "enabled", True)),
            mode=getattr(account, "mode", None) or "on_behalf",
            response_mode=getattr(account, "response_mode", None) or "approval",
            ai_label_enabled=bool(getattr(account, "ai_label_enabled", True)),
            grace_period_seconds=int(getattr(account, "grace_period_seconds", 0) or 0),
            owner_takeover_minutes=int(getattr(account, "owner_takeover_minutes", 0) or 0),
            max_replies_per_hour=int(getattr(account, "max_replies_per_hour", 0) or 0),
            max_turns_per_hour=int(getattr(account, "max_turns_per_hour", 0) or 0),
            active_hours=getattr(account, "active_hours", None) or None,
            instructions=(getattr(account, "instructions", None) or "").strip(),
            rules=list(getattr(account, "rules", None) or []),
            has_agent=getattr(account, "agent_id", None) is not None,
        )


@dataclass(frozen=True)
class EventState:
    """What the pipeline knows about the conversation when the message arrives."""

    #: The account's agent answered in this thread within the continuation window.
    thread_active: bool = False
    #: The account's human wrote in this conversation within the takeover window.
    owner_active: bool = False
    #: Replies the account posted in this conversation during the last hour.
    replies_last_hour: int = 0
    #: Agent turns the account ran during the last hour, every conversation together.
    turns_last_hour: int = 0
    #: Another agent account posted the message: two agents never answer each other.
    agent_post: bool = False
    #: The XTM One groups of the sender, matched by email to an XTM One user (empty: none).
    sender_group_ids: frozenset[str] = frozenset()
    now: datetime | None = None


@dataclass(frozen=True)
class Decision:
    """What to do with one message, and why."""

    respond: bool
    #: A stable code the UI translates: see :data:`REASONS`.
    reason: str
    rule_id: str | None = None
    rule_name: str | None = None
    #: The agent must answer (``[NO_RESPONSE]`` forbidden) rather than decide.
    must_reply: bool = False
    #: The message is addressed to the account: a direct message or a mention.
    addressed: bool = False
    #: Not addressed, but a follow-up in a thread the account answered in.
    thread_continuation: bool = False
    response_mode: str = "approval"
    ai_label: bool = False
    grace_period_seconds: int = 0
    instructions: str = ""
    #: The rule's ``access`` (``normalize_rule``): what the turn may use, ``None`` = the agent's all.
    access: dict[str, Any] | None = None
    #: Who the sender was found to be, when a rule names groups (the pipeline sets it,
    #: never the engine): ``{"known", "emails", "groups"}``, the groups the rules name only.
    sender: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            **({"sender": self.sender} if self.sender is not None else {}),
            "respond": self.respond,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "must_reply": self.must_reply,
            "addressed": self.addressed,
            "thread_continuation": self.thread_continuation,
            "response_mode": self.response_mode,
            "ai_label": self.ai_label,
            "grace_period_seconds": self.grace_period_seconds,
            "access": self.access,
        }


#: Every reason a decision can carry.
REASONS = (
    "respond",
    "account_disabled",
    "no_agent",
    "own_message",
    "application_author",
    "agent_author",
    "empty_message",
    "outside_active_hours",
    "ignored_by_rule",
    "not_addressed",
    "no_matching_rule",
    "owner_takeover",
    "rate_limited",
    "account_rate_limited",
)


def _matches_target(targets: list[dict[str, str]], *values: str | None) -> bool:
    wanted = {v.strip().lower() for v in values if isinstance(v, str) and v.strip()}
    if not wanted:
        return False
    for target in targets:
        for key in ("id", "email"):
            value = target.get(key)
            if isinstance(value, str) and value.strip().lower() in wanted:
                return True
    return False


def _is_domain(value: Any) -> bool:
    """A sender target naming a whole domain: ``@example.com``."""
    return isinstance(value, str) and value.startswith("@") and "." in value and "@" not in value[1:]


def _matches_sender(targets: list[dict[str, str]], event: AccountEvent) -> bool:
    """Whether *targets* name the sender of *event*: by id, by address, or by the domain of an address."""
    if _matches_target(targets, event.sender_id, *event.sender_addresses):
        return True
    domains = {target["id"].strip().lower() for target in targets if _is_domain(target.get("id"))}
    return bool(domains) and any(
        "@" + address.rsplit("@", 1)[1].strip().lower() in domains
        for address in event.sender_addresses
        if "@" in address
    )


def _conversation_in_scope(rule: dict[str, Any], event: AccountEvent) -> bool:
    """Whether *rule* reaches the conversation of *event*, whoever wrote it."""
    if event.conversation_type not in (rule.get("conversation_types") or CONVERSATION_TYPES):
        return False
    conversations = rule.get("conversations") or []
    return not (conversations and not _matches_target(conversations, event.conversation_id))


def _in_scope(rule: dict[str, Any], event: AccountEvent, group_ids: frozenset[str]) -> bool:
    if not _conversation_in_scope(rule, event):
        return False
    # Who may ask: the people named, or a member of a group named - either one.
    senders = rule.get("senders") or []
    groups = rule.get("groups") or []
    if (senders or groups) and not (
        (senders and _matches_sender(senders, event)) or (groups and _matches_target(groups, *group_ids))
    ):
        return False
    excluded = rule.get("exclude_senders") or []
    return not (excluded and _matches_sender(excluded, event))


def _hears_automated(rule: dict[str, Any]) -> bool:
    """Whether *rule* hears automated messages: opted in, enabled, and naming whom.

    Only a rule that names its senders or groups may: an application writes
    without end, so "every automated message" is never a scope.
    """
    return bool(
        rule.get("enabled", True)
        and rule.get("include_automated") is True
        and (rule.get("senders") or rule.get("groups"))
    )


def evaluate(settings: AccountSettings, event: AccountEvent, state: EventState | None = None) -> Decision:
    """Decide whether the account answers *event*, and how."""
    state = state or EventState()
    now = state.now or datetime.now(UTC)
    if not settings.enabled:
        return Decision(False, "account_disabled")
    if not settings.has_agent:
        return Decision(False, "no_agent")
    if event.from_account:
        return Decision(False, "own_message")
    # A conversation no rule reaches is said so, whoever wrote in it: an app
    # posting in a channel nobody asked to follow is not the reason it is left.
    if not any(rule.get("enabled", True) and _conversation_in_scope(rule, event) for rule in settings.rules):
        return Decision(False, "no_matching_rule")
    if event.from_application and not any(
        _hears_automated(rule) and _in_scope(rule, event, state.sender_group_ids) for rule in settings.rules
    ):
        return Decision(False, "application_author")
    if state.agent_post:
        return Decision(False, "agent_author")
    if not event.text.strip():
        return Decision(False, "empty_message")
    if not within_active_hours(settings.active_hours, now):
        return Decision(False, "outside_active_hours")

    addressed = event.mentions_account or event.conversation_type == "direct"
    unaddressed_in_scope = False
    for rule in settings.rules:
        if not rule.get("enabled", True) or not _in_scope(rule, event, state.sender_group_ids):
            continue
        if event.from_application and not _hears_automated(rule):
            continue
        continuation = (
            not addressed and state.thread_active and bool(rule.get("continue_threads", True))
        )
        if rule.get("trigger", "mention") == "mention" and not (addressed or continuation):
            unaddressed_in_scope = True
            continue
        if rule.get("action", "respond") == "ignore":
            return Decision(
                False,
                "ignored_by_rule",
                rule_id=rule.get("id"),
                rule_name=rule.get("name") or None,
                addressed=addressed,
            )
        decision = _respond(settings, rule, addressed=addressed, continuation=continuation)
        if settings.owner_takeover_minutes > 0 and state.owner_active:
            return _refused(decision, "owner_takeover")
        if settings.max_replies_per_hour > 0 and state.replies_last_hour >= settings.max_replies_per_hour:
            return _refused(decision, "rate_limited")
        if settings.max_turns_per_hour > 0 and state.turns_last_hour >= settings.max_turns_per_hour:
            return _refused(decision, "account_rate_limited")
        return decision
    return Decision(False, "not_addressed" if unaddressed_in_scope else "no_matching_rule", addressed=addressed)


def _respond(settings: AccountSettings, rule: dict[str, Any], *, addressed: bool, continuation: bool) -> Decision:
    label = rule.get("ai_label")
    grace = rule.get("grace_period_seconds")
    instructions = "\n\n".join(
        part for part in (settings.instructions, (rule.get("instructions") or "").strip()) if part
    )
    return Decision(
        True,
        "respond",
        rule_id=rule.get("id"),
        rule_name=rule.get("name") or None,
        # A follow-up nobody addressed to the account is the agent's call.
        must_reply=rule.get("reply", "always") == "always" and not continuation,
        addressed=addressed,
        thread_continuation=continuation,
        response_mode=rule.get("response_mode") or settings.response_mode,
        ai_label=settings.ai_label_enabled if label is None else bool(label),
        grace_period_seconds=settings.grace_period_seconds if grace is None else int(grace),
        instructions=instructions,
        access=rule.get("access"),
    )


def _refused(decision: Decision, reason: str) -> Decision:
    return Decision(
        False,
        reason,
        rule_id=decision.rule_id,
        rule_name=decision.rule_name,
        addressed=decision.addressed,
        thread_continuation=decision.thread_continuation,
        response_mode=decision.response_mode,
        ai_label=decision.ai_label,
        grace_period_seconds=decision.grace_period_seconds,
    )


# ---------------------------------------------------------------------------
# What the account must listen to
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelTarget:
    """A channel the account listens to, and whether mentions alone can decide there."""

    id: str
    team_id: str | None
    mentions_only: bool
    #: The name the rule's picker stored, for what the account says about it.
    name: str | None = None


@dataclass(frozen=True)
class ListeningPlan:
    """The conversations an account's answering rules can reach.

    ``all_channels`` is set when a rule answers in channels without naming
    them: the provider then listens to the account's channels as far as it
    can, with mentions alone when every such rule waits for a mention.
    """

    chats: bool = False
    channels: tuple[ChannelTarget, ...] = ()
    all_channels: bool = False
    all_channels_mentions_only: bool = True

    @property
    def empty(self) -> bool:
        return not (self.chats or self.channels or self.all_channels)


def listening_plan(settings: AccountSettings) -> ListeningPlan:
    """What the provider must subscribe to so every answering rule can fire.

    A rule that only ignores reaches nothing. A channel needs every message
    only when an answering rule reaching it answers without a mention:
    hearing a whole channel for the follow-ups of one thread would bring every
    message of it - alerts and bots included - to the account. The threads the
    account answered in are followed by reading them instead
    (``listening.follow_threads``).
    """
    if not settings.enabled or not settings.has_agent:
        return ListeningPlan()
    chats = False
    named: dict[str, dict[str, Any]] = {}
    all_channels = False
    all_mentions_only = True
    for rule in settings.rules:
        if not rule.get("enabled", True) or rule.get("action", "respond") != "respond":
            continue
        types = rule.get("conversation_types") or CONVERSATION_TYPES
        mentions_only = rule.get("trigger", "mention") == "mention"
        if "direct" in types or "group" in types:
            chats = True
        if "channel" not in types:
            continue
        channels = [c for c in rule.get("conversations") or [] if c.get("type") in (None, "channel")]
        if not channels and rule.get("conversations"):
            continue
        if not channels:
            all_channels = True
            all_mentions_only = all_mentions_only and mentions_only
            continue
        for channel in channels:
            entry = named.setdefault(
                channel["id"], {"team_id": channel.get("team_id"), "mentions_only": True, "name": None}
            )
            entry["team_id"] = entry["team_id"] or channel.get("team_id")
            entry["mentions_only"] = entry["mentions_only"] and mentions_only
            entry["name"] = entry["name"] or channel.get("name") or None
    return ListeningPlan(
        chats=chats,
        channels=tuple(
            ChannelTarget(cid, entry["team_id"], entry["mentions_only"], entry["name"])
            for cid, entry in named.items()
        ),
        all_channels=all_channels,
        all_channels_mentions_only=all_mentions_only,
    )


def plan_hears(plan: ListeningPlan, event: AccountEvent) -> bool:
    """Whether *plan* listens to the conversation of *event*.

    A message from a conversation the plan does not listen to came through a
    subscription the rules no longer want: the account's listening is behind
    its rules.
    """
    if event.conversation_type != "channel":
        return plan.chats
    return plan.all_channels or any(channel.id == event.conversation_id for channel in plan.channels)


def hears_channel_in_full(plan: ListeningPlan, channel_id: str) -> bool:
    """Whether *plan* hears every message of the channel, not its mentions alone."""
    for channel in plan.channels:
        if channel.id == channel_id:
            return not channel.mentions_only
    return plan.all_channels and not plan.all_channels_mentions_only


def follows_channel_threads(settings: AccountSettings, channel_id: str) -> bool:
    """Whether an answering rule reaching the channel takes the follow-ups of a thread the account answered in."""
    for rule in settings.rules:
        if not rule.get("enabled", True) or rule.get("action", "respond") != "respond":
            continue
        if not rule.get("continue_threads", True) or "channel" not in (rule.get("conversation_types") or CONVERSATION_TYPES):
            continue
        conversations = rule.get("conversations") or []
        if not conversations or any(c.get("id") == channel_id for c in conversations if c.get("type") in (None, "channel")):
            return True
    return False


def rule_group_names(settings: AccountSettings) -> dict[str, str]:
    """The groups the enabled rules name, by lowercased id, with the name the owner saw."""
    names: dict[str, str] = {}
    for rule in settings.rules:
        if not rule.get("enabled", True):
            continue
        for group in rule.get("groups") or []:
            group_id = str(group.get("id") or "").lower()
            if group_id:
                names.setdefault(group_id, str(group.get("name") or group_id))
    return names


# ---------------------------------------------------------------------------
# The AI label
# ---------------------------------------------------------------------------


def label_text(mode: str, custom: str | None, account_name: str | None) -> str:
    """The label an account posts: its own text, else the mode's default; ``{name}`` filled in."""
    text = (custom or "").strip() or DEFAULT_LABEL.get(mode, DEFAULT_LABEL["on_behalf"])
    return text.replace("{name}", (account_name or "").strip() or "the account owner")


def apply_ai_label(text: str, label: str, position: str = "suffix") -> str:
    """*text* with *label* on a line of its own, in italics, before or after it."""
    label = label.strip()
    if not label:
        return text
    marked = f"_{label}_"
    if position == "prefix":
        return f"{marked}\n\n{text}"
    return f"{text}\n\n{marked}"

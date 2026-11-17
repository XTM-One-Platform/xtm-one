"""The lab's agent accounts: a Teams one with a week of activity, an empty one and a mailbox.

Started by ``lab.py`` (``seed_agent_account_activity``) with the backend's interpreter,
from ``backend/`` and with the backend's environment, once the backend answers. Without
it ``GET /api/v1/agent-accounts`` answers ``[]`` and ``/profile/agent-accounts/<id>``
(Overview, Decisions, Messages sent) is only reviewed through a ``window.fetch``
override. Creating an account through the API needs a signed-in delegated integration
(a real provider call), and no API writes a decision, so this writes the rows through
the backend's own models (``AgentAccount``, ``AgentAccountDecision``), as
``services/agent_accounts/activity.py`` would record them. Arguments: the administrator's
user id, the JSON file the account ids are written to, then optionally the agent id and the
Microsoft integration id of the saveable account.

* ``Lab sample Teams account`` (``microsoft_teams``, ``on_behalf``, no integration):
  decisions over the last 7 days on 14 rules (one without a name), 14 conversations
  (one with a very long name) and 5 people; the activity endpoint answers the 14 rules
  (``TOP_ROWS * 2``), so the rules table pages past 10 rows, and its top 10
  conversations (``TOP_ROWS``) out of 14; and 30 messages sent over the last 30 days
  (``agent_account_replies``, #9951): every status (sent, pending, shadow, failed with
  its error, rejected, expired), both kinds (reply, proactive with its origin and
  recipients), direct chats without a name, one very long conversation name and one
  very long rule name, and ratings on three sent answers when the administrator owns an
  agent, so the Messages sent tab filters, sorts and pages past 25 rows;
* ``Lab sample empty Teams account``: no decision, the empty sentences of the Overview;
* ``Lab sample Outlook mailbox`` (``microsoft_outlook``, kind ``mailbox``): decisions in every
  outcome group of ``activity.GROUPS`` (the left ones on the reasons a mailbox words in its
  own terms), on mail threads named by their subject, and one proactive send with its reply
  row, so the Decisions, Overview and Messages sent tabs render the mailbox wording;
* ``Lab sample saveable Teams account``, when the agent and integration ids are given: bound to
  the lab's agent and to the lab Microsoft integration whose pickers ``lab_asgi`` answers, so its
  Edit dialog (General, Rules, Replies, Schedule) saves end to end. Disabled too: a save that
  leaves it off claims nothing and schedules no sync.

No provider is called: the accounts carry no integration (the saveable one carries the lab's) and are written disabled
(status ``paused``), so the maintenance pass (``listening.run_maintenance``, enabled
accounts only) never schedules a sync that would fail without a token. Idempotent: a restarted
lab finds the accounts by owner and name and writes no decision or message twice.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ACTIVE_NAME = "Lab sample Teams account"
EMPTY_NAME = "Lab sample empty Teams account"
MAILBOX_NAME = "Lab sample Outlook mailbox"
SAVEABLE_NAME = "Lab sample saveable Teams account"

#: (rule id, rule name or None): 14 rules, the ninth without a name.
RULES: tuple[tuple[str, str | None], ...] = tuple(
    (f"lab-rule-{i:02d}", None if i == 9 else f"Lab rule {i:02d}: answer the {topic} questions")
    for i, topic in enumerate(
        (
            "SOC", "CTI", "on-call", "vendor", "customer", "release", "incident", "audit",
            "unnamed", "hiring", "budget", "roadmap", "training", "travel",
        ),
        start=1,
    )
)

LONG_CONVERSATION = (
    "Lab sample cross-team coordination channel for the quarterly threat landscape review, the detection "
    "engineering backlog, the purple team exercises and every follow-up the regional security operations "
    "centres raise between two steering committees"
)

#: (conversation id, name, type): 14 conversations, one with a very long name.
CONVERSATIONS: tuple[tuple[str, str | None, str], ...] = (
    ("lab-conv-long", LONG_CONVERSATION, "channel"),
    *(
        (f"lab-conv-{i:02d}", f"Lab sample channel {i:02d}", "channel")
        for i in range(1, 9)
    ),
    *((f"lab-chat-{i:02d}", f"Lab sample group chat {i:02d}", "group") for i in range(1, 4)),
    # Direct chats have no name of their own: the table names the person.
    ("lab-dm-01", None, "direct"),
    ("lab-dm-02", None, "direct"),
)

#: (sender id, display name): 5 people.
PEOPLE: tuple[tuple[str, str], ...] = (
    ("lab-user-ada", "Ada Lovelace"),
    ("lab-user-alan", "Alan Turing"),
    ("lab-user-grace", "Grace Hopper"),
    ("lab-user-claude", "Claude Shannon"),
    ("lab-user-radia", "Radia Perlman"),
)

#: Outcomes in rotation: answered, left alone, handed to the owner, awaiting approval, failed.
OUTCOMES: tuple[tuple[str, str | None, bool], ...] = (
    ("sent", None, True),
    ("silent", None, True),
    ("sent", None, True),
    ("not_addressed", "not_addressed", False),
    ("flagged", None, True),
    ("pending", None, True),
    ("sent", None, True),
    ("failed", None, True),
)

#: Decisions written on the active account (about 8 a day over 7 days).
DECISIONS = 56


def decisions(account_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    """The active account's decisions, spread over the last 7 days, deterministic."""
    rows: list[dict[str, object]] = []
    for i in range(DECISIONS):
        conversation_id, name, conversation_type = CONVERSATIONS[i % len(CONVERSATIONS)]
        # Every rule is matched at least once; the first rules more often, so the table sorts.
        rule_id, rule_name = RULES[(i * 3) % len(RULES)] if i >= len(RULES) else RULES[i]
        sender_id, sender_name = PEOPLE[i % len(PEOPLE)]
        outcome, reason, respond = OUTCOMES[i % len(OUTCOMES)]
        at = now - timedelta(hours=(i * 167) // DECISIONS + 1, minutes=i % 50)
        rows.append({
            "account_id": account_id,
            "outcome": outcome,
            "reason": reason,
            "respond": respond,
            "conversation_type": conversation_type,
            "conversation_id": conversation_id,
            "conversation_name": name,
            "message_id": f"lab-msg-{i:03d}",
            "sender_id": sender_id,
            "sender_name": sender_name,
            "preview": f"Lab sample message {i:03d} from {sender_name}: can you look at this?",
            "mentions_account": i % 3 == 0,
            "addressed": respond,
            "rule_id": rule_id,
            "rule_name": rule_name,
            "note": "Seeded by the lab.",
            "message_at": at,
            "created_at": at,
            "updated_at": at,
        })
    return rows


#: (thread id, subject, type): a mailbox's conversations are mail threads; a
#: mail to the account alone is ``direct``, one with other recipients ``group``.
MAIL_THREADS: tuple[tuple[str, str, str], ...] = (
    ("lab-mail-thread-01", "Re: Quarterly threat landscape review", "group"),
    ("lab-mail-thread-02", "Vendor security questionnaire", "direct"),
    ("lab-mail-thread-03", "Incident follow-up: phishing wave", "group"),
    ("lab-mail-thread-04", "Your weekly newsletter", "direct"),
    ("lab-mail-thread-05", "Budget sign-off for the detection backlog", "direct"),
)

#: (outcome, reason, respond, holder): one row per outcome, so every group of
#: ``activity.GROUPS`` holds rows; ``not_addressed`` and ``application_author``
#: are the reasons a mailbox words in its own terms.
MAIL_OUTCOMES: tuple[tuple[str, str | None, bool, str | None], ...] = (
    ("sent", None, True, None),
    ("coalesced", None, True, None),
    ("flagged", None, True, None),
    ("pending", None, True, None),
    ("shadow", None, True, None),
    ("rejected", None, True, None),
    ("expired", None, True, None),
    ("silent", None, True, None),
    ("waiting", None, True, None),
    ("already_handled", None, False, "assignment"),
    ("not_addressed", "not_addressed", False, None),
    ("application_author", "application_author", False, None),
    ("no_matching_rule", "no_matching_rule", False, None),
    ("failed", None, True, None),
    ("ai_unavailable", None, True, None),
)

#: Statuses of the active account's messages sent, in rotation: every status
#: of ``AgentAccountReply`` the Messages sent tab filters on.
REPLY_STATUSES: tuple[str, ...] = ("sent", "pending", "shadow", "failed", "rejected", "expired")

#: Messages written on the active account, spread over the last 30 days.
REPLIES = 30

LONG_RULE = (
    "Lab rule 15: answer the questions on the quarterly threat landscape review, the detection "
    "engineering backlog and the purple team exercises raised by the regional SOCs"
)

#: (rating, reply index): the administrator's ratings of three sent answers.
REPLY_RATINGS: tuple[tuple[str, int], ...] = (("positive", 6), ("negative", 18), ("positive", 24))


def replies(account_id: uuid.UUID, owner: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    """The active account's messages sent, newest first, deterministic.

    Status ``REPLY_STATUSES[i % 6]``; the sent and failed ones of every other
    rotation are proactive (an assignment's ``send_teams_message`` call, its
    recipients named); the others answer a message the account heard.
    """
    rows: list[dict[str, object]] = []
    for i in range(REPLIES):
        status = REPLY_STATUSES[i % len(REPLY_STATUSES)]
        proactive = status in ("sent", "failed") and (i // len(REPLY_STATUSES)) % 2 == 0
        conversation_id, name, conversation_type = CONVERSATIONS[i % len(CONVERSATIONS)]
        if i == 1:
            conversation_id, name, conversation_type = CONVERSATIONS[0]
        sender_id, sender_name = PEOPLE[i % len(PEOPLE)]
        rule_id, rule_name = RULES[i % len(RULES)]
        if i == 2:
            rule_id, rule_name = "lab-rule-15", LONG_RULE
        at = now - timedelta(hours=(i * 719) // REPLIES + 1, minutes=(i * 7) % 60)
        draft = f"Lab sample answer {i:03d}: thanks {sender_name}, I am looking at it and will come back today."
        if proactive:
            draft = f"Lab sample message {i:03d}: the weekly SOC digest is ready, three alerts need a decision."
        decided = status in ("sent", "rejected", "failed")
        rows.append({
            "account_id": account_id,
            "user_id": owner,
            "status": status,
            "kind": "proactive" if proactive else None,
            "origin": {
                "tool": "send_teams_message",
                "source": "assignment",
                "recipients": ["Ada Lovelace", "Grace Hopper"],
            } if proactive else None,
            "conversation_type": conversation_type,
            "conversation_id": conversation_id,
            "conversation_name": name,
            "thread_id": conversation_id,
            "message_id": f"lab-reply-msg-{i:03d}",
            "sender_id": None if proactive else sender_id,
            "sender_name": "Lab Admin" if proactive else sender_name,
            "inbound_text": "" if proactive else f"Lab sample message {i:03d} from {sender_name}: can you look at this?",
            "draft_text": draft,
            "sent_text": f"{draft}\n\n(AI generated)" if status == "sent" else None,
            "sent_message_id": f"lab-reply-sent-{i:03d}" if status == "sent" else None,
            "rule_id": None if proactive else rule_id,
            "rule_name": None if proactive else rule_name,
            "ai_label": True,
            "error": (
                "Microsoft Graph answered 403 Forbidden: the account is no longer a member of the conversation."
                if status == "failed" else None
            ),
            "decided_by_user_id": owner if status == "rejected" else None,
            "decided_at": at + timedelta(minutes=3) if decided else None,
            "created_at": at,
            "updated_at": at,
        })
    return rows


async def seed_replies(db: Any, account: Any, now: datetime) -> None:
    """The active account's messages sent, and the administrator's ratings of three when they own an agent."""
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.agent_account import AgentAccountReply
    from app.models.annotation import Annotation

    written = [AgentAccountReply(id=uuid.uuid4(), **row) for row in replies(account.id, account.user_id, now)]
    db.add_all(written)
    await db.flush()
    agent_id = (
        await db.execute(select(Agent.id).where(Agent.user_id == account.user_id).order_by(Agent.created_at).limit(1))
    ).scalar_one_or_none()
    if agent_id is None:
        return
    db.add_all(
        Annotation(
            target_type="agent_account_reply",
            target_id=written[index].id,
            user_id=account.user_id,
            agent_id=agent_id,
            rating=rating,
            source="web",
        )
        for rating, index in REPLY_RATINGS
    )


#: The mailbox's one proactive send: a mail the agent wrote on its own.
MAIL_PROACTIVE = {
    "conversation_id": "lab-mail-thread-sent-01",
    "conversation_name": "Weekly CTI digest",
    "message_id": "lab-mail-sent-001",
    "text": "Hello team, here is this week's CTI digest: three new intrusion sets and two campaigns to review.",
    "recipients": ["soc@example.com", "cti@example.com"],
}


def mailbox_decisions(account_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    """The mailbox's decisions, two per outcome over the last 7 days, deterministic."""
    rows: list[dict[str, object]] = []
    for i in range(len(MAIL_OUTCOMES) * 2):
        outcome, reason, respond, holder = MAIL_OUTCOMES[i % len(MAIL_OUTCOMES)]
        conversation_id, subject, conversation_type = MAIL_THREADS[i % len(MAIL_THREADS)]
        sender_id, sender_name = PEOPLE[i % len(PEOPLE)]
        rule_id, rule_name = RULES[i % 4]
        at = now - timedelta(hours=(i * 160) // (len(MAIL_OUTCOMES) * 2) + 1, minutes=i % 50)
        rows.append({
            "account_id": account_id,
            "outcome": outcome,
            "reason": reason,
            "respond": respond,
            "conversation_type": conversation_type,
            "conversation_id": conversation_id,
            "conversation_name": subject,
            "message_id": f"lab-mail-{i:03d}",
            "sender_id": sender_id,
            "sender_name": sender_name,
            "preview": f"{subject}: lab sample email {i:03d} from {sender_name}, could you reply by Friday?",
            "mentions_account": False,
            "addressed": reason != "not_addressed",
            "rule_id": None if reason else rule_id,
            "rule_name": None if reason else rule_name,
            "holder": holder,
            "note": "Seeded by the lab.",
            "message_at": at,
            "created_at": at,
            "updated_at": at,
        })
    return rows


async def seed_mailbox_proactive(db: Any, account: Any, now: datetime) -> None:
    """The mailbox's proactive send: its reply row and its log entry, as ``sent_log._write`` writes them."""
    from app.models.agent_account import AgentAccountDecision, AgentAccountReply

    sent = MAIL_PROACTIVE
    at = now - timedelta(hours=5)
    reply = AgentAccountReply(
        id=uuid.uuid4(),
        account_id=account.id,
        user_id=account.user_id,
        status="sent",
        kind="proactive",
        origin={"tool": "send_microsoft_email", "source": "assignment", "recipients": sent["recipients"]},
        conversation_type="group",
        conversation_id=sent["conversation_id"],
        conversation_name=sent["conversation_name"],
        thread_id=sent["conversation_id"],
        message_id=sent["message_id"],
        sender_name=account.account_display_name or account.name,
        inbound_text="",
        draft_text=sent["text"],
        sent_text=sent["text"],
        sent_message_id=sent["message_id"],
        ai_label=False,
        created_at=at,
        updated_at=at,
    )
    db.add(reply)
    await db.flush()
    db.add(
        AgentAccountDecision(
            account_id=account.id,
            outcome="sent",
            respond=True,
            kind="proactive",
            conversation_type="group",
            conversation_id=sent["conversation_id"],
            conversation_name=sent["conversation_name"],
            message_id=sent["message_id"],
            sender_name=account.account_display_name or account.name,
            preview=sent["text"],
            reply_id=reply.id,
            message_at=at,
            created_at=at,
            updated_at=at,
        )
    )


#: (key, name, provider): the accounts written, each once.
ACCOUNTS: tuple[tuple[str, str, str], ...] = (
    ("active", ACTIVE_NAME, "microsoft_teams"),
    ("empty", EMPTY_NAME, "microsoft_teams"),
    ("mailbox", MAILBOX_NAME, "microsoft_outlook"),
    ("saveable", SAVEABLE_NAME, "microsoft_teams"),
)


async def seed(admin_id: str, agent_id: str | None = None, integration_id: str | None = None) -> dict[str, str]:
    """Write the accounts (each once) and the active and mailbox ones' decisions; answer their ids.

    The saveable account is written only with *agent_id* and *integration_id*, bound to both at every start
    (the lab's integration id is chosen anew by each ``lab.py``).
    """
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from sqlalchemy import func, select

    from app.core.database import async_session, engine
    from app.models.agent_account import AgentAccount, AgentAccountDecision, AgentAccountReply

    owner = uuid.UUID(admin_id)
    now = datetime.now(UTC)
    ids: dict[str, str] = {}
    async with async_session() as db:
        for key, name, provider in ACCOUNTS:
            if key == "saveable" and not (agent_id and integration_id):
                continue
            account = (
                await db.execute(select(AgentAccount).where(AgentAccount.user_id == owner, AgentAccount.name == name))
            ).scalar_one_or_none()
            if account is None:
                account = AgentAccount(
                    user_id=owner,
                    name=name,
                    description="Seeded by the lab: no integration, no provider call.",
                    provider=provider,
                    mode="on_behalf",
                    enabled=False,
                    response_mode="approval",
                    transport="none",
                    status="paused",
                    account_display_name="Lab Admin",
                    account_email="lab-mailbox@example.com" if key == "mailbox" else None,
                )
                db.add(account)
                await db.flush()
            ids[key] = str(account.id)
            if key == "saveable":
                account.agent_id = uuid.UUID(agent_id)
                account.integration_id = uuid.UUID(integration_id)
                account.description = (
                    "Seeded by the lab: an agent and a lab Microsoft integration whose pickers lab_asgi answers."
                )
                continue
            if key == "empty":
                continue
            if key == "active":
                sent = (
                    await db.execute(
                        select(func.count()).select_from(AgentAccountReply).where(
                            AgentAccountReply.account_id == account.id
                        )
                    )
                ).scalar_one()
                if not sent:
                    await seed_replies(db, account, now)
            held = (
                await db.execute(
                    select(func.count()).select_from(AgentAccountDecision).where(
                        AgentAccountDecision.account_id == account.id
                    )
                )
            ).scalar_one()
            if held:
                continue
            if key == "active":
                db.add_all(AgentAccountDecision(**row) for row in decisions(account.id, now))
            else:
                db.add_all(AgentAccountDecision(**row) for row in mailbox_decisions(account.id, now))
                await seed_mailbox_proactive(db, account, now)
            await db.flush()
        await db.commit()
    await engine.dispose()
    return ids


def main() -> None:
    admin_id, out, *saveable = sys.argv[1:5]
    agent_id, integration_id = (saveable + [None, None])[:2]
    Path(out).write_text(json.dumps(asyncio.run(seed(admin_id, agent_id, integration_id))))


if __name__ == "__main__":
    main()

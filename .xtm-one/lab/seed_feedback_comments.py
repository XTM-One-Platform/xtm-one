"""The lab's feedback comments: one agent whose Observability table lists commented feedback on real rows.

Started by ``lab.py`` (``seed_feedback_comments``) with the backend's interpreter,
from ``backend/`` and with the backend's environment, once the analytics seed
wrote ``analytics_ids.json``: no API writes another user's ``message_feedback``
row on a conversation that user never had, so this writes the conversations,
the assistant messages and the feedback through the platform's models.
Arguments: the ``analytics_ids.json`` file (``agents`` and ``users`` created
through the API by ``seed_analytics_traces``) and the JSON file the ids are
written to.

* the agent is ``Lab sample analytics agent 12`` (``AGENT_INDEX``);
* ``len(COMMENTS)`` assistant messages over ``len(CHANNELS)`` channels, one
  commented feedback each, positive and negative, one comment over 250
  characters (``LONG_COMMENT``), raters taken from the analytics users plus one
  user with an empty ``full_name`` (``NAMELESS_EMAIL``), so the table falls
  back to the email;
* the ratings spread over the last 30 days (``SPREAD_HOURS`` apart), and
  ``CRITERIA``: a 1-5 scale, a categorical criterion with three options and a
  name over 50 characters, a disabled numeric 0-10, scored on the ratings
  ``score_plan`` picks (every one but each fourth), at the rating's time.

So ``GET /agents/{id}/feedback-comments`` answers more than two pages of ten,
narrows on ``rating`` and finds the long comment by a word of it, with no fetch
override (issue #9494); and Agent > Feedback lists the Feedback criteria table
with scores, average, distribution and last 30 days bars, the disabled
criterion with its badge (issue #9860).

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: ``Lab sample analytics agent 12`` in ``analytics_ids.json`` (agents 01 to 20).
AGENT_INDEX = 11
CHANNELS = ("web", "slack", "teams", "whatsapp")
NAMELESS_EMAIL = "lab-feedback-nameless@filigran.io"
#: Hours between two ratings: 26 ratings over about 28 days, inside Last 30 days.
SPREAD_HOURS = 26
#: The criteria definitions, checked by ``services.feedback_criteria.validate_shape``.
CRITERIA: tuple[dict, ...] = (
    {"name": "Accuracy", "description": "Seeded by the lab: were the facts right?", "type": "scale",
     "min_value": 1, "max_value": 5, "options": None, "enabled": True},
    {"name": "Usefulness of the answer for the analyst's next investigation step",
     "description": "Seeded by the lab: a name over 50 characters.", "type": "categorical",
     "min_value": None, "max_value": None, "enabled": True,
     "options": [{"value": "useful", "label": "Useful", "score": 1.0},
                 {"value": "partly", "label": "Partly useful", "score": 0.5},
                 {"value": "not-useful", "label": "Not useful", "score": 0.0}]},
    {"name": "Response time", "description": "Seeded by the lab: retired, so disabled.", "type": "numeric",
     "min_value": 0, "max_value": 10, "options": None, "enabled": False},
)
#: A word of it the reviewer searches: ``indicators``.
LONG_COMMENT = (
    "Seeded by the lab: the answer listed the right campaign but mixed two intrusion sets, cited a report "
    "that was retracted last month and never linked the indicators it extracted to the observables already "
    "in the platform, so I had to redo the enrichment by hand before sharing it with the team."
)
COMMENTS: tuple[tuple[str, str], ...] = (
    ("positive", "Clear summary of the ransomware campaign, exactly what I needed."),
    ("negative", "The answer ignored the sector filter I asked for."),
    ("positive", "Good mapping to MITRE ATT&CK techniques."),
    ("negative", LONG_COMMENT),
    ("positive", "Fast and accurate triage of the phishing report."),
    ("negative", "Hallucinated a CVE number that does not exist."),
    ("positive", "Helpful list of mitigations, well prioritized."),
    ("negative", "Too long; I wanted three bullet points."),
    ("positive", "The timeline of the intrusion was spot on."),
    ("negative", "Missed the second malware family in the sample."),
    ("positive", "Nice explanation for a non-technical reader."),
    ("positive", "Correctly linked the threat actor to its aliases."),
    ("negative", "The table had the wrong dates."),
    ("positive", "Saved me an hour on the weekly report."),
    ("negative", "It answered in English although I wrote in French."),
    ("positive", "Great use of the knowledge base."),
    ("negative", "Sources were not cited."),
    ("positive", "Exactly the right level of detail."),
    ("negative", "Confused two vulnerabilities with similar names."),
    ("positive", "The detection rule compiled on the first try."),
    ("positive", "Useful comparison of the two campaigns."),
    ("negative", "Stopped halfway through the answer."),
    ("positive", "Good catch on the expired certificate."),
    ("negative", "The recommendation contradicts our policy."),
    ("positive", "Clear and concise, thanks."),
    ("negative", "Slack formatting broke the code block."),
)


def plan(now: datetime, raters: int) -> list[dict]:
    """One row per comment, newest first: channel, rating, comment, rater index (``raters`` is the nameless one)."""
    rows = []
    for n, (rating, comment) in enumerate(COMMENTS):
        rows.append({
            "channel": CHANNELS[n % len(CHANNELS)],
            "rating": rating,
            "comment": comment,
            "rater": raters if n == 1 else n % raters,
            "at": now - timedelta(hours=SPREAD_HOURS * n + 1),
        })
    return rows


def score_plan(rows: list[dict]) -> list[list[tuple[int, float | None, str | None]]]:
    """Per row, the ``(criterion index, value_numeric, value_text)`` scores it carries.

    Each fourth row is left unscored; a positive rating scores high, a negative one
    low; the disabled numeric criterion is scored on every other scored row, so it
    keeps a history and stays listed.
    """
    scales = {"positive": (5.0, 4.0, 4.0), "negative": (1.0, 2.0, 3.0)}
    choices = {"positive": ("useful", "useful", "partly"), "negative": ("not-useful", "partly", "not-useful")}
    plans: list[list[tuple[int, float | None, str | None]]] = []
    for n, row in enumerate(rows):
        if n % 4 == 3:
            plans.append([])
            continue
        scores = [(0, scales[row["rating"]][n % 3], None), (1, None, choices[row["rating"]][n % 3])]
        if n % 2 == 0:
            scores.append((2, float(n % 11), None))
        plans.append(scores)
    return plans


async def seed(agent_id: str, user_ids: list[str]) -> dict[str, object]:
    """Create the nameless rater, the conversations, the messages, the feedback, the criteria
    and their scores; answer the ids."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.annotation import AnnotationScore
    from app.models.chat import Conversation, Message, MessageFeedback
    from app.models.feedback_criterion import FeedbackCriterion
    from app.models.user import User
    from app.services.feedback_criteria import base_key, validate_shape

    agent = uuid.UUID(agent_id)
    async with async_session() as db:
        nameless = User(email=NAMELESS_EMAIL, full_name="", hashed_password=None, role="user", is_active=True)
        db.add(nameless)
        await db.flush()
        raters = [uuid.UUID(u) for u in user_ids] + [nameless.id]
        criteria: list[FeedbackCriterion] = []
        for order, spec in enumerate(CRITERIA):
            options, lo, hi = validate_shape(spec["type"], spec["options"], spec["min_value"], spec["max_value"])
            criteria.append(FeedbackCriterion(
                key=base_key(spec["name"]), name=spec["name"], description=spec["description"], type=spec["type"],
                weight=1.0, options=options, min_value=lo, max_value=hi, sort_order=order, enabled=spec["enabled"],
            ))
            db.add(criteria[-1])
        await db.flush()
        conversations: dict[tuple[uuid.UUID, str], Conversation] = {}
        feedback: list[str] = []
        scored = 0
        rows = plan(datetime.now(UTC), len(user_ids))
        for sequence, (row, scores) in enumerate(zip(rows, score_plan(rows), strict=True)):
            rater = raters[row["rater"]]
            key = (rater, row["channel"])
            if key not in conversations:
                conversations[key] = Conversation(
                    user_id=rater, agent_id=agent, channel=row["channel"],
                    title="Seeded by the lab: feedback comments", created_at=row["at"], updated_at=row["at"],
                )
                db.add(conversations[key])
                await db.flush()
            conversation = conversations[key]
            message = Message(
                conversation_id=conversation.id, role="assistant", channel=row["channel"], sequence=sequence,
                content="Seeded by the lab: an answer the user rated.", created_at=row["at"],
            )
            db.add(message)
            await db.flush()
            entry = MessageFeedback(
                message_id=message.id, user_id=rater, rating=row["rating"], comment=row["comment"],
                created_at=row["at"], updated_at=row["at"],
            )
            db.add(entry)
            await db.flush()
            feedback.append(str(entry.id))
            for index, value_numeric, value_text in scores:
                db.add(AnnotationScore(
                    message_feedback_id=entry.id, criterion_id=criteria[index].id,
                    value_numeric=value_numeric, value_text=value_text, created_at=row["at"],
                ))
            scored += bool(scores)
        await db.commit()
        result = {
            "agent": agent_id, "feedback": feedback, "scored": scored,
            "criteria": [str(c.id) for c in criteria],
            "conversations": [str(c.id) for c in conversations.values()],
        }
    await engine.dispose()
    return result


def main() -> None:
    ids_file, out = sys.argv[1:3]
    ids = json.loads(Path(ids_file).read_text())
    Path(out).write_text(json.dumps(asyncio.run(seed(ids["agents"][AGENT_INDEX], ids["users"]))))


if __name__ == "__main__":
    main()

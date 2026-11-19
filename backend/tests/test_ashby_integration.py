"""Ashby provider: what each tool sends, and what it makes of the answer.

The fake API is routed by endpoint (``/candidate.search`` ...) and shaped like
the documented answers of Ashby's API version 2026-01-01. It records every
request body, so a test pins the parameters Ashby takes (``applicationId``,
``name`` / ``email``, ``interviewPlanId``...) as well as the rendering.
"""

from __future__ import annotations

import contextlib
import json
import time

import httpx
import pytest

from app.api.v1 import integration_testing
from app.integrations.providers import ashby
from app.integrations.providers.ashby import _common
from app.services.integration_tools import _SafeIntegrationExecutor
from tests.integration_file_fakes import call, fake_api


class _Unpaced:
    @contextlib.asynccontextmanager
    async def slot(self, *args, **kwargs):
        yield


@pytest.fixture(autouse=True)
def _unpaced(monkeypatch):
    """The per-key limiter is process-wide; its lock must not outlive a test's event loop."""
    monkeypatch.setattr(_common, "get_shared_rate_limiter", lambda *a, **k: _Unpaced())


def _executor() -> _SafeIntegrationExecutor:
    executor = _SafeIntegrationExecutor()
    ashby.build_tools("ashby-test-key", executor)
    return executor


def ok(results, **extra) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "results": results, **extra})


def refused(code: str, message: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"success": False, "errors": [code], "errorInfo": {"code": code, "message": message}},
    )


class FakeAshby:
    """Answers each endpoint from *routes* (a result, or a function of the body)."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.bodies: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.lstrip("/")
        body = json.loads(request.content or b"{}")
        self.bodies.append((endpoint, body))
        route = self.routes.get(endpoint)
        if route is None:
            return refused("unexpected_endpoint", f"no route for {endpoint}")
        answer = route(body) if callable(route) else route
        return answer if isinstance(answer, httpx.Response) else ok(answer)

    def sent(self, endpoint: str) -> list[dict]:
        return [body for name, body in self.bodies if name == endpoint]


HUGO = {"id": "u-hm", "firstName": "Hugo", "lastName": "Manager", "email": "hugo@example.com"}
RITA = {"id": "u-rec", "firstName": "Rita", "lastName": "Recruiter", "email": "rita@example.com"}
CANDIDATE = {
    "id": "bcb69294",
    "name": "Yann Romestant",
    "primaryEmailAddress": {"value": "yann@example.com", "type": "Work", "isPrimary": True},
    "position": "Staff Engineer",
    "company": "Acme",
    "tags": [{"id": "t1", "title": "Silver medalist", "isArchived": False}],
    "applicationIds": ["app-1"],
    "profileUrl": "https://app.ashbyhq.com/candidates/bcb69294",
}
HISTORY = [
    {
        "id": "h0",
        "stageId": "st-review",
        "title": "Application Review",
        "enteredStageAt": "2026-01-10T09:00:00.000Z",
        "leftStageAt": "2026-01-12T09:00:00.000Z",
        "stageNumber": 0,
        "allowedActions": ["none"],
        "actorId": None,
    },
    {
        "id": "h1",
        "stageId": "st-tech",
        "title": "Technical Interview",
        "enteredStageAt": "2026-01-12T09:00:00.000Z",
        "leftStageAt": "2026-02-20T09:00:00.000Z",
        "stageNumber": 1,
        "allowedActions": ["none"],
        "actorId": "u-rec",
    },
    {
        "id": "h2",
        "stageId": "st-hired",
        "title": "Hired",
        "enteredStageAt": "2026-02-20T09:00:00.000Z",
        "leftStageAt": None,
        "stageNumber": 2,
        "allowedActions": ["none"],
        "actorId": "u-hm",
    },
]
APPLICATION = {
    "id": "app-1",
    "createdAt": "2026-01-10T09:00:00.000Z",
    "updatedAt": "2026-03-02T10:00:00.000Z",
    "status": "Hired",
    "candidate": {
        "id": "bcb69294",
        "name": "Yann Romestant",
        "primaryEmailAddress": {"value": "yann@example.com", "type": "Work", "isPrimary": True},
    },
    "currentInterviewStage": {"id": "st-hired", "title": "Hired", "type": "Hired", "orderInInterviewPlan": 3},
    "source": {"id": "src-1", "title": "Referral", "isArchived": False, "sourceType": {"id": "x", "title": "Inbound"}},
    "archiveReason": None,
    "archivedAt": None,
    "job": {"id": "job-1", "title": "Senior Engineer"},
    "creditedToUser": RITA,
    "hiringTeam": [{"userId": "u-hm", **{k: v for k, v in HUGO.items() if k != "id"}, "role": "Hiring Manager"}],
    "applicationHistory": HISTORY,
}
STAGES = {
    "st-review": {"id": "st-review", "title": "Application Review", "type": "PreInterviewScreen"},
    "st-tech": {"id": "st-tech", "title": "Technical Interview", "type": "Active"},
    "st-hired": {"id": "st-hired", "title": "Hired", "type": "Hired"},
}
USERS = {"u-hm": HUGO, "u-rec": RITA}


def _stage_info(body):
    return STAGES.get(body["interviewStageId"]) or refused("interview_stage_not_found", "Stage not found")


def _user_info(body):
    return USERS.get(body["userId"]) or refused("user_not_found", "User not found")


# ---------------------------------------------------------------------------
# The envelope: success false, permissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_success_false_answer_is_an_error_not_an_empty_record():
    api = FakeAshby({"application.info": refused("application_not_found", "Application not found")})
    with fake_api(api):
        result = await call(_executor(), "get_ashby_application", application_id="nope")
    assert result.is_error
    assert "Application not found (application_not_found)" in result.content
    assert "confidential jobs" in result.content
    assert "ID: ?" not in result.content


@pytest.mark.asyncio
async def test_a_missing_permission_names_the_permission_to_grant():
    def forbidden(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "errors": ["missing_endpoint_permission"]})

    with fake_api(forbidden):
        result = await call(_executor(), "search_ashby_candidates", term="Romestant")
    assert result.is_error
    assert "candidate.search" in result.content
    assert "'Candidates Read'" in result.content


def test_required_permission_reads_the_ashby_permission_table():
    assert _common.required_permission("candidate.search") == "Candidates Read"
    assert _common.required_permission("candidate.createNote") == "Candidates Write"
    assert _common.required_permission("interviewSchedule.list") == "Interviews Read"
    assert _common.required_permission("feedbackFormDefinition.info") == "Hiring Process Read"
    assert _common.required_permission("application.delete") == "Candidates Delete"
    assert _common.required_permission("customField.setValue") == "Candidates Write"
    assert _common.required_permission("report.synchronous") == "Reports Read"


# ---------------------------------------------------------------------------
# 1. get_ashby_application
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_application_sends_application_id_and_maps_every_field():
    api = FakeAshby({"application.info": APPLICATION})
    with fake_api(api):
        result = await call(_executor(), "get_ashby_application", application_id="app-1")
    assert not result.is_error, result.content
    text = result.content
    assert api.sent("application.info") == [{"applicationId": "app-1", "expand": ["openings"]}]
    assert "**Application** (ID: app-1) - Status: Hired" in text
    assert "Candidate: Yann Romestant (ID: bcb69294) <yann@example.com (Work)>" in text
    assert "Job: Senior Engineer (ID: job-1)" in text
    assert "Current stage: Hired [Hired] (ID: st-hired)" in text
    assert "Source: Referral (Inbound) (ID: src-1)" in text
    assert "Credited to: Rita Recruiter <rita@example.com> (ID: u-rec)" in text
    assert "Created: 2026-01-10T09:00:00Z | Updated: 2026-03-02T10:00:00Z | Hired at: 2026-02-20T09:00:00Z" in text
    assert "Hugo Manager <hugo@example.com> (ID: u-hm) - Hiring Manager" in text
    assert "Application Review (2026-01-10) -> Technical Interview (2026-01-12) -> Hired (2026-02-20)" in text


@pytest.mark.asyncio
async def test_an_archived_application_shows_its_reason_and_date():
    archived = {
        **APPLICATION,
        "status": "Archived",
        "archivedAt": "2026-02-01T12:00:00.000Z",
        "archiveReason": {"id": "ar-1", "text": "Went with another candidate", "reasonType": "RejectedByOrg"},
    }
    with fake_api(FakeAshby({"application.info": archived})):
        result = await call(_executor(), "get_ashby_application", application_id="app-1")
    assert "Archived on 2026-02-01T12:00:00Z: Went with another candidate [RejectedByOrg]" in result.content
    assert "Hired at" not in result.content


# ---------------------------------------------------------------------------
# 2. list_ashby_application_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_maps_each_stage_change_with_its_origin_actor_and_time():
    api = FakeAshby(
        {
            "application.listHistory": list(reversed(HISTORY)),
            "user.info": _user_info,
            "interviewStage.info": _stage_info,
        }
    )
    with fake_api(api):
        result = await call(_executor(), "list_ashby_application_history", application_id="app-1")
    assert not result.is_error, result.content
    text = result.content
    assert api.sent("application.listHistory")[0]["applicationId"] == "app-1"
    assert (
        '0. 2026-01-10T09:00:00Z - Application created: entered "Application Review" [PreInterviewScreen] (first stage)'
        in text
    )
    assert (
        '1. 2026-01-12T09:00:00Z - Stage change: entered "Technical Interview" [Active] (from "Application Review")'
        in text
    )
    assert "left 2026-02-20T09:00:00Z after 39.0 days | by Rita Recruiter <rita@example.com> (ID: u-rec)" in text
    assert '2. 2026-02-20T09:00:00Z - Stage change: entered "Hired" [Hired] (from "Technical Interview")' in text
    assert "current stage | by Hugo Manager <hugo@example.com> (ID: u-hm)" in text
    assert "?" not in text.split("\n", 1)[1]


# ---------------------------------------------------------------------------
# 3. list_ashby_application_feedback / submit
# ---------------------------------------------------------------------------

FORM = {
    "sections": [
        {
            "title": "Scorecard",
            "fields": [
                {
                    "isRequired": True,
                    "field": {
                        "id": "f1",
                        "type": "ValueSelect",
                        "path": "overall_recommendation",
                        "title": "Overall Recommendation",
                        "humanReadablePath": "Overall Recommendation",
                        "isNullable": False,
                        "selectableValues": [
                            {"label": "Strong Hire", "value": "strong_hire"},
                            {"label": "Hire", "value": "hire"},
                            {"label": "No Hire", "value": "no_hire"},
                        ],
                    },
                },
                {
                    "isRequired": True,
                    "field": {
                        "id": "f2",
                        "type": "Score",
                        "path": "technical",
                        "title": "Technical skills",
                        "humanReadablePath": "Technical skills",
                        "isNullable": False,
                    },
                },
                {
                    "isRequired": False,
                    "field": {
                        "id": "f3",
                        "type": "RichText",
                        "path": "notes",
                        "title": "Notes",
                        "humanReadablePath": "Notes",
                        "isNullable": True,
                    },
                },
            ],
        }
    ]
}
FEEDBACK = {
    "id": "fb-1",
    "applicationId": "app-1",
    "feedbackFormDefinitionId": "ff-1",
    "interviewId": "i-tech",
    "interviewEventId": "ev-1",
    "applicationHistoryId": "h1",
    "submittedAt": "2026-01-15T14:30:00.000Z",
    "submittedByUser": {"id": "u-sarah", "firstName": "Sarah", "lastName": "Johnson", "email": "sarah@example.com"},
    "creditedToUser": {"id": "u-sarah", "firstName": "Sarah", "lastName": "Johnson", "email": "sarah@example.com"},
    "formDefinition": FORM,
    "submittedValues": {
        "overall_recommendation": "hire",
        "technical": {"score": 3, "comments": {"type": "PlainText", "value": "Solid system design"}},
        "notes": {"type": "PlainText", "value": "Great communicator"},
    },
}


@pytest.mark.asyncio
async def test_feedback_returns_the_whole_scorecard():
    api = FakeAshby(
        {
            "applicationFeedback.list": [FEEDBACK],
            "application.info": APPLICATION,
            "feedbackFormDefinition.info": {"id": "ff-1", "title": "Technical Scorecard", "formDefinition": FORM},
            "interview.info": {"id": "i-tech", "title": "System design", "type": "Interview"},
        }
    )
    with fake_api(api):
        result = await call(_executor(), "list_ashby_application_feedback", application_id="app-1")
    assert not result.is_error, result.content
    text = result.content
    assert "- Yann Romestant for Senior Engineer (1 submission, oldest first)" in text
    assert "1. Submitted 2026-01-15T14:30:00Z by Sarah Johnson <sarah@example.com> (ID: u-sarah)" in text
    assert "Form: Technical Scorecard (ID: ff-1)" in text
    assert "Interview: System design (ID: i-tech)" in text
    assert "Stage: Technical Interview" in text
    assert "Interview event: ev-1" in text
    assert "Overall Recommendation: Hire" in text
    assert "Technical skills: 3/4 - Solid system design" in text
    assert "Notes: Great communicator" in text


@pytest.mark.asyncio
async def test_feedback_submit_writes_the_values_the_way_ashby_takes_them():
    api = FakeAshby(
        {
            "feedbackFormDefinition.info": {"id": "ff-1", "title": "Technical Scorecard", "formDefinition": FORM},
            "applicationFeedback.submit": {"submittedFormInstance": {"id": "inst-1"}},
        }
    )
    values = json.dumps({"Overall Recommendation": "Strong Hire", "technical": 4, "notes": "Hire him"})
    with fake_api(api):
        result = await call(
            _executor(),
            "submit_ashby_application_feedback",
            application_id="app-1",
            feedback_form_definition_id="ff-1",
            submitted_values=values,
            interview_event_id="ev-1",
        )
    assert not result.is_error, result.content
    assert "form instance ID: inst-1" in result.content
    (body,) = api.sent("applicationFeedback.submit")
    assert body == {
        "applicationId": "app-1",
        "formDefinitionId": "ff-1",
        "feedbackForm": {
            "fieldSubmissions": [
                {"path": "overall_recommendation", "value": "strong_hire"},
                {"path": "technical", "value": {"score": 4}},
                {"path": "notes", "value": {"type": "PlainText", "value": "Hire him"}},
            ]
        },
        "interviewEventId": "ev-1",
    }


@pytest.mark.asyncio
async def test_feedback_submit_refuses_an_unknown_option_before_writing():
    api = FakeAshby({"feedbackFormDefinition.info": {"id": "ff-1", "formDefinition": FORM}})
    with fake_api(api):
        result = await call(
            _executor(),
            "submit_ashby_application_feedback",
            application_id="app-1",
            feedback_form_definition_id="ff-1",
            submitted_values='{"overall_recommendation": "Maybe", "technical": 7}',
        )
    assert result.is_error
    assert "'Maybe' is not an option of 'Overall Recommendation'" in result.content
    assert "score from 1 to 4" in result.content
    assert not api.sent("applicationFeedback.submit")


# ---------------------------------------------------------------------------
# 4. list_ashby_interviews / get_ashby_interview / schedule
# ---------------------------------------------------------------------------


def _schedule(schedule_id: str, start: str, end: str, status: str = "Scheduled") -> dict:
    return {
        "id": schedule_id,
        "status": status,
        "applicationId": "app-1",
        "interviewStageId": "st-tech",
        "scheduledBy": RITA,
        "createdAt": "2026-01-12T10:00:00.000Z",
        "interviewEvents": [
            {
                "id": f"ev-{schedule_id}",
                "interviewId": "i-tech",
                "interviewScheduleId": schedule_id,
                "interviewerUserIds": ["u-hm"],
                "interviewers": [{**HUGO, "isFeedbackRequired": True}],
                "startTime": start,
                "endTime": end,
                "meetingLink": "https://meet.example/abc",
                "hasSubmittedFeedback": False,
                "interview": {"id": "i-tech", "title": "System design"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_interviews_are_the_application_schedules_with_their_events():
    schedule = _schedule("sch-1", "2026-10-20T09:00:00.000Z", "2026-10-20T10:00:00.000Z")
    api = FakeAshby(
        {
            "interviewSchedule.list": [schedule],
            "interviewStage.info": _stage_info,
            "application.info": APPLICATION,
        }
    )
    with fake_api(api):
        result = await call(_executor(), "list_ashby_interviews", application_id="app-1")
    assert not result.is_error, result.content
    text = result.content
    assert api.sent("interviewSchedule.list")[0]["applicationId"] == "app-1"
    assert "**Interview schedule** (ID: sch-1) - Status: Scheduled" in text
    assert "Candidate: Yann Romestant (ID: bcb69294) | Application: app-1 | Job: Senior Engineer (ID: job-1)" in text
    assert "Stage: Technical Interview [Active] (ID: st-tech)" in text
    assert "- System design: 2026-10-20T09:00:00Z -> 2026-10-20T10:00:00Z (event ID: ev-sch-1" in text
    assert "Interviewers: Hugo Manager <hugo@example.com> (ID: u-hm) (feedback required)" in text
    assert "Meeting: https://meet.example/abc" in text
    assert "Scheduled by: Rita Recruiter" in text


@pytest.mark.asyncio
async def test_upcoming_only_keeps_the_schedules_still_to_come_without_skipping_any():
    past = _schedule("old", "2020-01-01T09:00:00.000Z", "2020-01-01T10:00:00.000Z", status="Complete")
    soon = _schedule("soon", "2099-01-01T09:00:00.000Z", "2099-01-01T10:00:00.000Z")
    api = FakeAshby(
        {
            "interviewSchedule.list": lambda body: ok([past, soon], moreDataAvailable=False),
            "interviewStage.info": _stage_info,
            "application.info": APPLICATION,
        }
    )
    with fake_api(api):
        result = await call(_executor(), "list_ashby_interviews", upcoming_only=True, limit=5)
    assert "ID: soon" in result.content
    assert "ID: old" not in result.content
    assert api.sent("interviewSchedule.list")[0]["limit"] == 5


@pytest.mark.asyncio
async def test_get_interview_resolves_an_event_id_with_its_schedule():
    schedule = _schedule("sch-1", "2026-10-20T09:00:00.000Z", "2026-10-20T10:00:00.000Z")
    briefing = {
        "id": "ev-sch-1",
        "applicationId": "app-1",
        "interviewId": "i-tech",
        "interviewStageId": "st-tech",
        "interviewers": [{"userId": "u-hm", "isFeedbackRequired": True, "hasSubmittedFeedback": True, "user": HUGO}],
        "feedbackFormDefinitionId": "ff-1",
        "feedbackFormDefinition": {"id": "ff-1", "title": "Technical Scorecard"},
        "candidate": CANDIDATE,
        "job": {"id": "job-1", "title": "Senior Engineer"},
        "interview": {"id": "i-tech", "title": "System design", "instructionsPlain": "Ask about caching."},
    }
    api = FakeAshby(
        {
            "interviewBriefing.info": briefing,
            "interviewSchedule.list": [schedule],
            "interviewStage.info": _stage_info,
        }
    )
    with fake_api(api):
        result = await call(_executor(), "get_ashby_interview", interview_id="ev-sch-1")
    assert not result.is_error, result.content
    text = result.content
    assert "**Interview: System design** (event ID: ev-sch-1, interview ID: i-tech)" in text
    assert "Status: Scheduled (schedule ID: sch-1)" in text
    assert "When: 2026-10-20T09:00:00Z -> 2026-10-20T10:00:00Z" in text
    assert "Candidate: Yann Romestant (ID: bcb69294)" in text
    assert "Hugo Manager <hugo@example.com> (ID: u-hm) (feedback submitted)" in text
    assert "Feedback form: Technical Scorecard (ID: ff-1)" in text
    assert "Ask about caching." in text


@pytest.mark.asyncio
async def test_get_interview_falls_back_to_the_interview_definition():
    api = FakeAshby(
        {
            "interviewBriefing.info": refused("interview_event_not_found", "Interview event not found"),
            "interviewEvent.list": refused("interview_schedule_not_found", "Schedule not found"),
            "interview.info": {
                "id": "i-tech",
                "title": "System design",
                "type": "Interview",
                "isFeedbackRequired": True,
            },
        }
    )
    with fake_api(api):
        result = await call(_executor(), "get_ashby_interview", interview_id="i-tech")
    assert not result.is_error, result.content
    assert "**System design** (interview ID: i-tech) - Type: Interview [feedback required]" in result.content


@pytest.mark.asyncio
async def test_schedule_interview_sends_interview_events_with_interviewer_emails():
    api = FakeAshby(
        {
            "user.info": _user_info,
            "interviewSchedule.create": _schedule("new", "2026-10-21T09:00:00.000Z", "2026-10-21T10:00:00.000Z"),
            "interviewStage.info": _stage_info,
            "application.info": APPLICATION,
        }
    )
    with fake_api(api):
        result = await call(
            _executor(),
            "schedule_ashby_interview",
            application_id="app-1",
            start_time="2026-10-21T09:00:00Z",
            end_time="2026-10-21T10:00:00Z",
            interviewer_emails="rita@example.com",
            interviewer_user_ids="u-hm",
            interview_id="i-tech",
        )
    assert not result.is_error, result.content
    assert api.sent("interviewSchedule.create") == [
        {
            "applicationId": "app-1",
            "interviewEvents": [
                {
                    "startTime": "2026-10-21T09:00:00Z",
                    "endTime": "2026-10-21T10:00:00Z",
                    "interviewers": [
                        {"email": "rita@example.com", "feedbackRequired": True},
                        {"email": "hugo@example.com", "feedbackRequired": True},
                    ],
                    "interviewId": "i-tech",
                }
            ],
        }
    ]


# ---------------------------------------------------------------------------
# 5. list_ashby_interview_config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stages_are_listed_per_interview_plan():
    plan_stages = {
        "plan-eng": [
            {"id": "s2", "title": "Technical Interview", "type": "Active", "orderInInterviewPlan": 2},
            {"id": "s1", "title": "Application Review", "type": "PreInterviewScreen", "orderInInterviewPlan": 1},
        ],
        "plan-sales": [{"id": "s9", "title": "Sales Screen", "type": "Active", "orderInInterviewPlan": 1}],
    }
    api = FakeAshby(
        {
            "interviewPlan.list": [{"id": "plan-eng", "title": "Engineering"}, {"id": "plan-sales", "title": "Sales"}],
            "interviewStage.list": lambda body: plan_stages[body["interviewPlanId"]],
        }
    )
    with fake_api(api):
        result = await call(_executor(), "list_ashby_interview_config", config_type="stages")
    assert not result.is_error, result.content
    sent = sorted(b["interviewPlanId"] for b in api.sent("interviewStage.list"))
    assert sent == ["plan-eng", "plan-sales"]
    text = result.content
    assert "- Plan **Engineering** (plan ID: plan-eng)" in text
    assert text.index("1. Application Review [PreInterviewScreen] (stage ID: s1)") < text.index(
        "2. Technical Interview [Active] (stage ID: s2)"
    )
    assert "1. Sales Screen [Active] (stage ID: s9)" in text


@pytest.mark.asyncio
async def test_stages_of_a_job_come_from_its_interview_plan():
    plan = {
        "jobId": "job-1",
        "interviewPlanId": "plan-eng",
        "stages": [
            {
                "id": "s2",
                "title": "Onsite",
                "type": "Active",
                "orderInInterviewPlan": 2,
                "activities": [
                    {
                        "id": "a1",
                        "title": "Loop",
                        "interviews": [
                            {"id": "x", "title": "Coding", "interviewId": "i-code", "interviewDurationMinutes": 60}
                        ],
                    }
                ],
            }
        ],
    }
    api = FakeAshby({"jobInterviewPlan.info": plan})
    with fake_api(api):
        result = await call(_executor(), "list_ashby_interview_config", config_type="stages", job_id="job-1")
    assert api.sent("jobInterviewPlan.info") == [{"jobId": "job-1"}]
    assert "(plan ID: plan-eng)" in result.content
    assert "2. Onsite [Active] (stage ID: s2)" in result.content
    assert "Coding (interview ID: i-code, 60 min)" in result.content


# ---------------------------------------------------------------------------
# 6. search_ashby_candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"term": "Romestant"}, [{"name": "Romestant", "limit": 25}]),
        ({"term": "Yann"}, [{"name": "Yann", "limit": 25}]),
        ({"term": "yann@example.com"}, [{"email": "yann@example.com", "limit": 25}]),
        (
            {"name": "Yann Romestant", "email": "yann@example.com"},
            [{"name": "Yann Romestant", "limit": 25}, {"email": "yann@example.com", "limit": 25}],
        ),
    ],
    ids=["last-name", "first-name", "email", "name-and-email"],
)
async def test_candidate_search_sends_name_and_email(arguments, expected):
    api = FakeAshby({"candidate.search": [CANDIDATE]})
    with fake_api(api):
        result = await call(_executor(), "search_ashby_candidates", **arguments)
    assert not result.is_error, result.content
    assert api.sent("candidate.search") == expected
    assert result.content.count("**Yann Romestant** (ID: bcb69294)") == 1
    assert "Current role: Staff Engineer at Acme" in result.content


@pytest.mark.asyncio
async def test_an_empty_candidate_search_says_what_was_searched_and_what_to_check():
    with fake_api(FakeAshby({"candidate.search": []})):
        result = await call(_executor(), "search_ashby_candidates", term="Nobody")
    assert not result.is_error
    assert "name 'Nobody': 0" in result.content
    assert "confidential jobs" in result.content


# ---------------------------------------------------------------------------
# 7. get_ashby_job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_job_maps_title_status_department_location_and_hiring_team():
    job = {
        "id": "job-1",
        "title": "Senior Engineer",
        "status": "Open",
        "employmentType": "FullTime",
        "departmentId": "dep-1",
        "locationId": "loc-1",
        "location": {"id": "loc-1", "name": "Paris", "workplaceType": "Hybrid"},
        "hiringTeam": [
            {
                "userId": "u-rec",
                "firstName": "Rita",
                "lastName": "Recruiter",
                "email": "rita@example.com",
                "role": "Recruiter",
            }
        ],
        "defaultInterviewPlanId": "plan-eng",
        "interviewPlanIds": ["plan-eng"],
        "jobPostingIds": ["post-1"],
        "openings": [
            {
                "id": "op-1",
                "openingState": "Open",
                "latestVersion": {"identifier": "ENG-12", "targetHireDate": "2026-12-01"},
            }
        ],
        "compensation": {"compensationTiers": [{"title": "Base", "tierSummary": "EUR 80K - 100K"}]},
        "createdAt": "2026-01-01T00:00:00.000Z",
        "openedAt": "2026-01-05T00:00:00.000Z",
    }
    api = FakeAshby(
        {
            "job.info": job,
            "department.list": [{"id": "dep-1", "name": "Engineering"}],
        }
    )
    with fake_api(api):
        result = await call(_executor(), "get_ashby_job", job_id="job-1")
    assert not result.is_error, result.content
    text = result.content
    assert api.sent("job.info")[0]["id"] == "job-1"
    assert "**Senior Engineer** (ID: job-1) - Status: Open" in text
    assert "Department: Engineering (ID: dep-1) | Location: Paris [Hybrid] (ID: loc-1) | Employment: FullTime" in text
    assert "Rita Recruiter <rita@example.com> (ID: u-rec) - Recruiter" in text
    assert "Interview plan: plan-eng (default)" in text
    assert "ENG-12 [Open, target hire 2026-12-01] (ID: op-1)" in text
    assert "Base: EUR 80K - 100K" in text
    assert "Untitled" not in text and "Unknown" not in text


@pytest.mark.asyncio
async def test_job_writes_send_team_and_job_ids():
    api = FakeAshby(
        {
            "job.create": {"id": "job-2", "title": "SRE", "status": "Draft"},
            "job.update": {"id": "job-2", "title": "SRE II", "status": "Draft"},
            "department.list": [],
        }
    )
    executor = _executor()
    with fake_api(api):
        await call(executor, "create_ashby_job", title="SRE", department_id="dep-1", location_id="loc-1")
        await call(executor, "update_ashby_job", job_id="job-2", title="SRE II", department_id="dep-2")
    assert api.sent("job.create") == [{"title": "SRE", "teamId": "dep-1", "locationId": "loc-1"}]
    assert api.sent("job.update") == [{"jobId": "job-2", "title": "SRE II", "teamId": "dep-2"}]


# ---------------------------------------------------------------------------
# 8. list_ashby_applications filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_applications_filters_hires_server_side_and_pages_with_the_cursor():
    listed = {k: v for k, v in APPLICATION.items() if k != "applicationHistory"}
    api = FakeAshby(
        {
            "application.list": lambda body: ok([listed], moreDataAvailable=True, nextCursor="next-1"),
            "application.info": APPLICATION,
        }
    )
    executor = _executor()
    with fake_api(api):
        first = await call(
            executor,
            "list_ashby_applications",
            status="hired",
            job_id="job-1",
            created_after="2026-01-01",
            include_hire_dates=True,
            limit=1,
        )
        await call(executor, "list_ashby_applications", status="Hired", cursor="next-1", limit=1)
    assert not first.is_error, first.content
    body, follow_up = api.sent("application.list")
    assert body == {"status": "Hired", "jobId": "job-1", "createdAfter": 1767225600000, "limit": 1}
    assert follow_up["cursor"] == "next-1"
    assert "Hired at: 2026-02-20T09:00:00Z" in first.content
    assert 'pass cursor="next-1"' in first.content


@pytest.mark.asyncio
async def test_list_applications_count_only_pages_every_application_and_breaks_it_down():
    def page(body):
        start = int(body.get("cursor") or 0)
        size = body["limit"]
        rows = [
            {
                "id": f"app-{i}",
                "status": "Active" if i % 3 else "Archived",
                "job": {"id": f"job-{i % 2}", "title": "SRE" if i % 2 else "PM"},
            }
            for i in range(start, min(start + size, 230))
        ]
        more = start + size < 230
        return ok(rows, moreDataAvailable=more, nextCursor=str(start + size) if more else None)

    api = FakeAshby({"application.list": page})
    with fake_api(api):
        result = await call(_executor(), "list_ashby_applications", created_after="2026-01-01", count_only=True)
    assert not result.is_error, result.content
    sent = api.sent("application.list")
    assert [b["limit"] for b in sent] == [100, 100, 100]
    assert all(b["createdAfter"] == 1767225600000 for b in sent)
    assert result.content.startswith("230 applications with created_after=2026-01-01")
    assert "Active 153, Archived 77" in result.content
    assert "SRE (ID: job-1): 115" in result.content
    assert "PM (ID: job-0): 115" in result.content
    assert "app-0" not in result.content


@pytest.mark.asyncio
async def test_list_applications_count_only_says_at_least_when_it_stops(monkeypatch):
    from app.integrations.providers.ashby import applications

    monkeypatch.setattr(applications, "COUNT_MAX", 2)
    api = FakeAshby(
        {
            "application.list": lambda body: ok(
                [{"id": "a", "status": "Active"}, {"id": "b", "status": "Active"}][: body["limit"]],
                moreDataAvailable=True,
                nextCursor="c-2",
            )
        }
    )
    with fake_api(api):
        result = await call(_executor(), "list_ashby_applications", status="active", count_only=True)
    assert result.content.startswith("At least 2 applications with status=Active")
    assert 'cursor="c-2"' in result.content


@pytest.mark.asyncio
async def test_list_applications_refuses_an_unknown_status():
    result = await call(_executor(), "list_ashby_applications", status="Rejected")
    assert result.is_error
    assert "Active, Hired, Archived, Lead" in result.content


def test_time_bounds_read_relative_windows_dates_and_timestamps():
    assert _common.to_epoch_ms("2026-01-01", "x") == (1767225600000, None)
    assert _common.to_epoch_ms("1767225600", "x") == (1767225600000, None)
    assert _common.to_epoch_ms("1767225600000", "x") == (1767225600000, None)
    ms, err = _common.to_epoch_ms("30d", "x")
    assert err is None and abs(ms - (time.time() - 30 * 86400) * 1000) < 5000
    assert _common.to_epoch_ms("", "x") == (None, None)
    assert _common.to_epoch_ms("last tuesday", "x")[1].startswith("Error: x 'last tuesday' is not a time")


# ---------------------------------------------------------------------------
# Writes whose payload the old provider got wrong
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidate_writes_send_name_and_candidate_id():
    api = FakeAshby(
        {
            "candidate.create": {**CANDIDATE, "id": "c-new", "name": "Ada Lovelace"},
            "candidate.update": CANDIDATE,
            "candidate.createNote": {"id": "note-1"},
        }
    )
    executor = _executor()
    with fake_api(api):
        await call(executor, "create_ashby_candidate", first_name="Ada", last_name="Lovelace", email="ada@example.com")
        await call(executor, "update_ashby_candidate", candidate_id="bcb69294", phone="+33 1 23 45 67 89")
        note = await call(
            executor, "create_ashby_candidate_note", candidate_id="bcb69294", note="<p>Strong</p>", note_type="html"
        )
    assert api.sent("candidate.create") == [{"name": "Ada Lovelace", "email": "ada@example.com"}]
    assert api.sent("candidate.update") == [
        {"candidateId": "bcb69294", "phoneNumber": "+33 1 23 45 67 89", "sendNotifications": False}
    ]
    assert api.sent("candidate.createNote")[0]["note"] == {"type": "text/html", "value": "<p>Strong</p>"}
    assert "note ID: note-1" in note.content


@pytest.mark.asyncio
async def test_notes_show_their_author_and_plain_text():
    note = {
        "id": "n1",
        "createdAt": "2026-02-01T08:00:00.000Z",
        "isPrivate": True,
        "content": "<p>Debrief: <b>strong</b> yes</p>",
        "author": RITA,
    }
    with fake_api(FakeAshby({"candidate.listNotes": [note]})):
        result = await call(_executor(), "list_ashby_candidate_notes", candidate_id="bcb69294")
    assert "[2026-02-01T08:00:00Z] Rita Recruiter <rita@example.com> [private] (note ID: n1)" in result.content
    assert "Debrief: strong yes" in result.content


@pytest.mark.asyncio
async def test_offers_are_read_by_offer_id_and_created_through_the_offer_form():
    offer_form = {
        "sections": [
            {
                "fields": [
                    {
                        "isRequired": True,
                        "field": {
                            "id": "o1",
                            "type": "Date",
                            "path": "startDate",
                            "title": "Start date",
                            "humanReadablePath": "Start date",
                            "isNullable": False,
                        },
                    },
                    {
                        "isRequired": True,
                        "field": {
                            "id": "o2",
                            "type": "Currency",
                            "path": "salary",
                            "title": "Salary",
                            "humanReadablePath": "Salary",
                            "isNullable": False,
                        },
                    },
                ]
            }
        ]
    }
    offer = {
        "id": "off-1",
        "applicationId": "app-1",
        "acceptanceStatus": "Pending",
        "offerStatus": "WaitingOnCandidateResponse",
        "latestVersion": {
            "id": "ver-1",
            "startDate": "2026-11-02",
            "salary": {"value": 95000, "currencyCode": "EUR"},
            "approvalStatus": "Approved",
            "author": RITA,
        },
        "versions": [{"id": "ver-1"}],
    }
    api = FakeAshby(
        {
            "offer.info": offer,
            "application.info": APPLICATION,
            "offerProcess.start": {"id": "proc-1", "applicationId": "app-1", "status": "WaitingOnOfferCreation"},
            "offer.start": {"id": "form-1", "formDefinition": offer_form},
            "offer.create": offer,
        }
    )
    executor = _executor()
    with fake_api(api):
        read = await call(executor, "get_ashby_offer", offer_id="off-1")
        started = await call(executor, "start_ashby_offer", application_id="app-1")
        created = await call(
            executor,
            "create_ashby_offer",
            offer_process_id="proc-1",
            offer_form_id="form-1",
            field_values='{"Start date": "2026-11-02", "salary": {"value": 95000, "currencyCode": "EUR"}}',
        )
    assert api.sent("offer.info")[0]["offerId"] == "off-1"
    assert "Status: WaitingOnCandidateResponse | Acceptance: Pending" in read.content
    assert "start date 2026-11-02 | salary 95000 EUR | approval Approved" in read.content
    assert "Candidate: Yann Romestant (ID: bcb69294)" in read.content
    assert "offer_process_id: proc-1" in started.content and "offer_form_id: form-1" in started.content
    assert "- startDate: Start date [Date, required]" in started.content
    assert not created.is_error, created.content
    assert api.sent("offer.create")[0]["offerForm"] == {
        "fieldSubmissions": [
            {"path": "startDate", "value": "2026-11-02"},
            {"path": "salary", "value": {"value": 95000, "currencyCode": "EUR"}},
        ]
    }


@pytest.mark.asyncio
async def test_lookups_that_take_their_own_id_name_send_it():
    api = FakeAshby(
        {
            "opening.info": {"id": "op-1", "openingState": "Open", "latestVersion": {"identifier": "ENG-12"}},
            "user.search": [RITA],
            "project.search": [{"id": "p1", "title": "Backend pool"}],
            "job.search": [],
            "department.list": [],
        }
    )
    executor = _executor()
    with fake_api(api):
        opening = await call(executor, "get_ashby_opening", opening_id="op-1")
        await call(executor, "search_ashby_users", term="rita@example.com")
        await call(executor, "search_ashby_projects", title="Backend")
        await call(executor, "search_ashby_jobs", term="Engineer")
    assert api.sent("opening.info") == [{"openingId": "op-1"}]
    assert "**Opening ENG-12** (ID: op-1) - State: Open" in opening.content
    assert api.sent("user.search") == [{"email": "rita@example.com"}]
    assert api.sent("project.search") == [{"title": "Backend"}]
    assert api.sent("job.search") == [{"limit": 25, "title": "Engineer"}]


@pytest.mark.asyncio
async def test_unpaged_lists_are_called_without_paging_fields():
    api = FakeAshby(
        {
            "source.list": [{"id": "src-1", "title": "LinkedIn", "sourceType": {"title": "Sourced"}}],
            "closeReason.list": [{"id": "cr-1", "reasonText": "Filled"}],
            "jobBoard.list": [{"id": "jb-1", "title": "Careers", "isInternal": False}],
            "archiveReason.list": [{"id": "ar-1", "text": "Not a fit", "reasonType": "RejectedByOrg"}],
        }
    )
    executor = _executor()
    with fake_api(api):
        sources = await call(executor, "list_ashby_sources")
        closes = await call(executor, "list_ashby_reference_data", kind="close_reasons")
        boards = await call(executor, "list_ashby_reference_data", kind="job_boards")
        archives = await call(executor, "list_ashby_reference_data", kind="archive_reasons")
    assert api.sent("source.list") == [{"includeArchived": False}]
    assert api.sent("closeReason.list") == [{"includeArchived": False}]
    assert api.sent("jobBoard.list") == [{}]
    assert "LinkedIn (Sourced) (ID: src-1)" in sources.content
    assert "Filled (ID: cr-1)" in closes.content
    assert "Careers (ID: jb-1)" in boards.content
    assert "Not a fit [RejectedByOrg] (ID: ar-1)" in archives.content


@pytest.mark.asyncio
async def test_a_user_name_search_scans_the_user_list():
    api = FakeAshby({"user.list": [RITA, HUGO]})
    with fake_api(api):
        result = await call(_executor(), "search_ashby_users", term="hugo")
    assert "Hugo Manager <hugo@example.com> (ID: u-hm)" in result.content
    assert "Rita" not in result.content


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_connection_names_the_key_and_its_permissions():
    info = {"title": "XTM One", "createdAt": "2026-01-01T00:00:00.000Z", "scopes": ["jobsRead", "candidatesRead"]}
    with fake_api(FakeAshby({"apiKey.info": info})):
        ok_, message = await ashby.validate_connection("key")
        tested = await integration_testing.test_provider_connectivity("ashby", "key", {}, "Ashby")
    assert ok_ is True
    assert message == "Connected to Ashby with API key 'XTM One' (2 permissions: candidatesRead, jobsRead)."
    assert tested == {"status": "ok", "message": message}


@pytest.mark.asyncio
async def test_validate_connection_tells_a_key_without_api_keys_read_from_a_rejected_key():
    def missing(_request):
        return httpx.Response(403, json={"success": False, "errors": ["missing_endpoint_permission"]})

    def rejected(_request):
        return httpx.Response(403, json={"success": False, "errors": ["invalid_api_key"]})

    with fake_api(missing):
        valid, valid_message = await ashby.validate_connection("key")
    with fake_api(rejected):
        bad, bad_message = await ashby.validate_connection("key")
    assert valid is True and "'API Keys Read'" in valid_message
    assert bad is False and "rejected the API key" in bad_message
    assert await ashby.validate_connection("  ") == (False, "Missing Ashby API key.")


def _sample_arguments(schema: dict) -> list[dict]:
    """One call per value of an ``action`` enum (else one), every property filled."""
    arguments: dict = {}
    for name, prop in schema.get("properties", {}).items():
        if prop.get("enum"):
            arguments[name] = prop["enum"][0]
        elif prop.get("type") == "integer":
            arguments[name] = 1
        elif prop.get("type") == "boolean":
            arguments[name] = False
        else:
            arguments[name] = f"{name}-1"
    actions = schema.get("properties", {}).get("action", {}).get("enum")
    if not actions:
        return [arguments]
    return [{**arguments, "action": action} for action in actions]


def _permissive(request: httpx.Request) -> httpx.Response:
    endpoint = request.url.path.lstrip("/")
    method = endpoint.partition(".")[2]
    return ok([] if method.startswith(("list", "search")) else {})


@pytest.mark.asyncio
async def test_every_tool_answers_without_raising_on_a_minimal_ashby_answer():
    from app.tools.base import ToolCall

    executor = _SafeIntegrationExecutor()
    tools = ashby.build_tools("ashby-test-key", executor)
    crashed = []
    with fake_api(_permissive):
        for tool in tools:
            for arguments in _sample_arguments(tool.input_schema):
                result = await executor.execute_tool(ToolCall(id="c1", name=tool.name, arguments=arguments))
                if result.content.startswith("Error executing tool"):
                    crashed.append(f"{tool.name}({arguments.get('action', '')}): {result.content}")
    assert not crashed, "\n".join(crashed)


def test_build_tools_exposes_unique_registered_tools_with_the_ashby_source():
    from app.tools.base import CallableToolExecutor

    executor = CallableToolExecutor()
    tools = ashby.build_tools("dummy", executor)
    names = [t.name for t in tools]
    assert len(names) == len(set(names))
    assert all(t.source == "integration:ashby" for t in tools)
    assert {
        "get_ashby_application",
        "list_ashby_application_history",
        "list_ashby_application_feedback",
        "list_ashby_interviews",
        "get_ashby_interview",
        "list_ashby_interview_config",
        "search_ashby_candidates",
        "get_ashby_job",
        "list_ashby_applications",
    } <= set(names)

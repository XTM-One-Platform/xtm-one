"""The lab's backend application: ``app.main:application`` with one lab-only answer in front of it.

Started by ``lab.py`` as the uvicorn application (``lab_asgi:application``, from ``backend/``) so the
GitHub integration the lab seeds renders its Webhooks card with nothing reaching GitHub. The repository
picker of that card lists ``GET /api/v1/integrations/<id>/elements?element_type=repository``, which the
backend answers from api.github.com with the integration's token: for the lab's own integration only
(``LAB_GITHUB_INTEGRATION_ID``, set by ``lab.py``), this wrapper answers one lab repository itself.
Every other request, the same path of any other integration included, goes to the application as is.
Never part of a deployment: the file lives under ``.xtm-one/lab`` and only a lab starts it.

With ``LAB_FREEZE_TRIGGER_PROVISIONING=1`` (set by ``lab.py``) the scheduler's trigger provisioning pass is a
no-op: nothing in the lab answers OpenCTI or OpenAEV, so a pass would turn every state ``seed_provisioning.py``
writes into an error within a minute. Assignments, events and every other pass are untouched.

With ``LAB_CTEM_SAMPLE=1`` (``[service.lab-ctem]``) the CTEM posture collection answers the deterministic facts of
``ctem_sample`` for the cyber vertical, OpenCTI present and OpenAEV not: nothing answers the Suite platforms here,
so the live collection would leave every stage score null. Every other vertical is collected as is.

With ``LAB_CTEM_SAMPLE_STATE=on`` (``[service.lab-ctem-sample]``) the posture collection answers no product and no
fact for every vertical, without waiting on the ``.invalid`` URLs of the registered platforms: the command center
renders its sample state (``posture_score`` null, no product present, no stage covered) on a real answer.

With ``LAB_CTEM_FINDINGS=on`` (``[service.lab-ctem-findings]``) the cyber posture collection answers
``ctem_sample.findings_facts``, OpenCTI and OpenAEV present: failed OpenAEV expectations on four types, Scoping
connected with no data, KEVs and a detection coverage gap, so the rule-based rows of the Recommended actions card
render on a real answer. Every other vertical is collected as is.

With ``LAB_CTEM_ACTIVITY=on`` (``[service.lab-ctem-activity]``) the cyber collection answers the same findings and
the posture payload carries a ``last_sync`` time for each connected product (``LAB_PRODUCT_SYNC_MINUTES``), so the
deterministic activity feed of the Live activity card lists its rows on a real answer.

With ``LAB_CTEM_STALE=on`` (``[service.lab-ctem-stale]``, #12381) every cyber posture build is cached 25 h old (past
the soft TTL) until a Force sync rebuilds it, so the command center shows its stale posture after every reload.

With ``LAB_CTEM_POSTURE_HISTORY=1`` (set by ``lab.py`` in every lab that seeds the 90 days of CTEM posture history)
the posture route keeps a stored today snapshot of the cyber vertical as it is: the live collection answers no fact
here, so its upsert would replace the seeded today row with an empty one at every read. Before the seed writes
today, the route records the live answer as usual. The same labs lay ``ctem_sample.LARGE_FACTS`` over the live cyber
facts (``large_ctem_facts``), unless a CTEM sample switch is on: every stage shows counts of 1,000 and more beside
small ones, so the compact numbers (621K, exact on hover) render with no fetch override.

With ``LAB_OPENCTI_IDENTITIES=1`` (set by ``lab.py`` unless the declaration's env says ``0``) the OpenCTI
Organization search of ``GET /api/v1/organizations/opencti-identities`` (and the run estimate's
``XtmRunEstimate`` count on a fixture Organization, :data:`LAB_RUN_ESTIMATE_COUNT`) is answered from
:data:`LAB_OPENCTI_ORGANIZATIONS` (name or alias contains the term), so the picker of Settings > Organization
renders its results and no-results states, and the read of one Organization by id (``organization(id:)``) is
answered from the same fixture (an unknown id reads ``null``, the route's 404), so linking a fixture identity and
the Edit drawer's linked name work. The search term ``lab:error``, the id ``identity--lab-error`` and every other
OpenCTI query go to the real caller, which answers the route's 409 here (nothing answers the ``.invalid`` platform
URL).

With ``LAB_OPENCTI_PIRS=1`` (set by ``lab.py`` unless the declaration's env says ``0``) the PIR picker's list
(``GET /api/v1/organization-context/pirs``) is answered from :data:`LAB_OPENCTI_PIRS` (nine named PIRs, one name
over 60 characters) with ``unavailable`` null, so Settings > Organization > Profile renders its answered list, and a
profile saved with an id outside the fixture renders the "PIR no longer in OpenCTI" state. A saved profile whose
PIR ids hold :data:`LAB_OPENCTI_PIRS_ERROR` answers ``unreadable``, and an administrator with no OpenCTI token
answers ``not_connected``, as the route does.

With ``LAB_ORG_AI_DRAFT=1`` (set by ``lab.py`` unless the declaration's env says ``0``) a Draft with AI request
(``POST /api/v1/organization-context/ai-draft``) whose description starts ``lab:quota`` is answered the route's
429 with the platform's quota message, so the dialog's inline quota alert renders on a real answer. Every other
description goes to the application, whose model call the stub model answers with a proposal set.

With ``LAB_OPENCRQ_SYNC=1`` (set by ``lab.py`` unless the declaration's env says ``0``) Check against OpenCRQ
(``POST /api/v1/organization-context/opencrq-sync``) answers a deterministic plan, one row of each kind, from
:data:`LAB_OPENCRQ_JEWELS` and :data:`LAB_OPENCRQ_ASSETS` through the real ``plan_sync``; Apply answers ``applied``
and writes nothing, and the next check answers the plan where everything agrees, once. Nothing answers OpenCRQ
here, so the real ``run_sync`` would answer the route's 409.

With ``LAB_SLACK_BOT_TOKEN`` set (by ``lab.py``, which seeds one Slack channel bot with that lab-only token) the
channel list of that bot (``GET /api/v1/channel-bots/<id>/channels``) is answered from :data:`LAB_SLACK_CHANNELS`
(three channels, one private), so the ChannelPickers of Channels & Routing list them with nothing sent to Slack.
Every other bot goes to the real caller.

With ``LAB_TEAMS_BOT_CLIENT_ID`` set (by ``lab.py``, which seeds one Teams channel bot with that lab-only client id)
the channel list of that bot is answered from :data:`LAB_TEAMS_CHANNELS` (three channels in two teams), so the Teams
ChannelPickers of Channels & Bots list them with nothing sent to Microsoft. Every other Teams bot goes to the real
caller.

With ``LAB_AGENT_ACCOUNT_INTEGRATION_ID`` set (by ``lab.py``, which creates that Microsoft integration and binds it to the
saveable agent account) the agent account pickers of that integration (``GET /api/v1/agent-accounts/pickers/<provider>/
identity``, ``/conversations`` and ``/people``) answer :data:`LAB_AGENT_ACCOUNT_IDENTITY`, its conversations and its
people, so the account's Edit dialog saves with nothing sent to Microsoft. Every other integration goes to the real
session.

With ``LAB_NOVA_ENGINE=1`` (set by ``lab.py`` unless the declaration's env says ``0``) the Nova engine status
(``GET /api/v1/nova-rules/engine-status``) answers ``engine_available`` true and the three tiers over the route's own
answer (models and calibration kept as the backend computes them), so the Nova rule source page renders its Engine
models card and calibration line. The lab has no platform embedding model, so an empty ``configured_model`` names
:data:`LAB_NOVA_SEMANTIC_MODEL` (uncalibrated: the warning line renders); ``LAB_NOVA_OVERRIDE_IGNORED=1`` (the
default) also fills an empty ``override_ignored`` so the second warning line renders. Nothing else changes: the guard
still evaluates prompts on the keyword tier, no model is loaded and ``nova-hunting`` is not installed.

With ``LAB_OFFICE_ADDIN_DIST`` set (by ``lab.py`` when ``LAB_OFFICE_ADDIN`` is on, #11109) ``/office/`` is served from
the task pane the lab builds there, read at each request: the build ends after the backend started, and until it
exists the path goes to the application as before. The preview proxies ``/office`` here.

With ``LAB_STALE_BUILD_SERVER_ID`` set (by ``lab.py`` under ``LAB_STALE_BUILD=on``, #63) ``GET /api/v1/auth/config``
answers that id as ``build_id`` over the route's own answer: the Vite dev server writes another id in the page's
``<meta name="build-id">``, so the stale build notice renders natively. Every other field is the route's.

Every lab answers ``GET /api/v1/lab/whatsapp-decision`` (and ``/index``) with the render of the WhatsApp decision
message in every state, drawn from the real payload builder (``whatsapp_decision_preview``, #842 S12): WhatsApp
offers no other surface a review can open.

Every lab answers ``GET /api/v1/lab/locale/<en|fr|es|ja|default>?next=<path>`` (``lab_locale``, #1653): the
language is stored on every user of the lab and the answer redirects to ``next``, so one browser ``navigate`` reloads
the app in that language. ``GET /api/v1/lab/theme/<light|dark>?next=<path>`` does the same for the theme (#1848).

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import ctem_sample  # beside this file
import lab_locale  # beside this file
import whatsapp_decision_preview  # beside this file

from app.api.v1 import ctem_metrics
from app.main import application as backend
from app.services import organization_context_import, organization_opencti, trigger_provisioning

#: The one repository the lab's GitHub integration lists, in the shape ``integration_elements`` answers.
LAB_REPOSITORY = {
    "id": "lab-sample/lab-repository",
    "name": "lab-repository",
    "group": "lab-sample",
    "metadata": {
        "full_name": "lab-sample/lab-repository",
        "owner": "lab-sample",
        "private": True,
        "default_branch": "main",
        "language": "Python",
    },
}


def lab_answer(scope: dict, integration_id: str) -> dict | None:
    """The JSON the lab answers for *scope* itself, or ``None`` when the application answers it."""
    if not integration_id or scope.get("type") != "http" or scope.get("method") != "GET":
        return None
    if scope.get("path") != f"/api/v1/integrations/{integration_id}/elements":
        return None
    query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    if query.get("element_type") != ["repository"]:
        return None
    return {"elements": [LAB_REPOSITORY], "element_type": "repository"}


async def _frozen_trigger_provisioning(_db) -> None:
    """The lab's provisioning pass: the seeded states stay as written."""


def freeze_trigger_provisioning(environ: dict) -> bool:
    """Replace the scheduler's provisioning pass with a no-op when *environ* asks for it; whether it did."""
    if environ.get("LAB_FREEZE_TRIGGER_PROVISIONING") != "1":
        return False
    # ``autonomous`` imports the pass by name at every tick, so the module attribute is the one it calls.
    trigger_provisioning.maintain_trigger_provisioning = _frozen_trigger_provisioning
    return True


freeze_trigger_provisioning(dict(os.environ))


async def _sample_posture(db, user_id: str, vertical: str = ctem_sample.VERTICAL, *, _live=None):
    """The lab's posture collection: today's sample facts for the cyber vertical, the live one otherwise."""
    if vertical != ctem_sample.VERTICAL:
        return await _live(db, user_id, vertical)
    return ctem_sample.stage_facts(0), set(ctem_sample.PRESENT)


def sample_ctem_posture(environ: dict) -> bool:
    """Answer the posture collection with ``ctem_sample`` when *environ* asks for it; whether it did."""
    if environ.get("LAB_CTEM_SAMPLE") != "1":
        return False
    live = ctem_metrics.build_posture_for_user

    async def collect(db, user_id: str, vertical: str = ctem_sample.VERTICAL):
        return await _sample_posture(db, user_id, vertical, _live=live)

    # The posture route calls the collector by the name it imported, so the route module's attribute is the one used.
    ctem_metrics.build_posture_for_user = collect
    return True


sample_ctem_posture(dict(os.environ))


async def _empty_posture(db, user_id: str, vertical: str = ctem_sample.VERTICAL):
    """The lab's posture collection under ``LAB_CTEM_SAMPLE_STATE=on``: no fact, no product present."""
    return {}, set()


def empty_ctem_posture(environ: dict) -> bool:
    """Answer the posture collection with no product when *environ* asks for it; whether it did."""
    if environ.get("LAB_CTEM_SAMPLE_STATE", "").strip().lower() != "on":
        return False
    # The posture route calls the collector by the name it imported, so the route module's attribute is the one used.
    ctem_metrics.build_posture_for_user = _empty_posture
    return True


empty_ctem_posture(dict(os.environ))


async def _findings_posture(db, user_id: str, vertical: str = ctem_sample.VERTICAL, *, _live=None):
    """The lab's posture collection under ``LAB_CTEM_FINDINGS=on``: the findings for cyber, the live one otherwise."""
    if vertical != ctem_sample.VERTICAL:
        return await _live(db, user_id, vertical)
    return ctem_sample.findings_facts(), set(ctem_sample.FINDINGS_PRESENT)


def findings_ctem_posture(environ: dict) -> bool:
    """Answer the cyber posture collection with ``ctem_sample.findings_facts`` when *environ* asks; whether it did."""
    if environ.get("LAB_CTEM_FINDINGS", "").strip().lower() != "on":
        return False
    live = ctem_metrics.build_posture_for_user

    async def collect(db, user_id: str, vertical: str = ctem_sample.VERTICAL):
        return await _findings_posture(db, user_id, vertical, _live=live)

    # The posture route calls the collector by the name it imported, so the route module's attribute is the one used.
    ctem_metrics.build_posture_for_user = collect
    return True


findings_ctem_posture(dict(os.environ))


def actions_ctem_posture(environ: dict) -> bool:
    """``LAB_CTEM_ACTIONS=on``: the cyber posture answers the findings (KEVs, coverage under 60); whether it did.

    ``[service.lab-ctem-actions]`` keeps AI on: ``seed_ctem_actions.py`` stores a ready assessment per stage and a
    ready briefing, so the Act now queue merges briefing, stage and module rows on real answers.
    """
    if environ.get("LAB_CTEM_ACTIONS", "").strip().lower() != "on":
        return False
    return findings_ctem_posture({"LAB_CTEM_FINDINGS": "on"})


actions_ctem_posture(dict(os.environ))


#: How old ``LAB_CTEM_STALE=on`` stores a posture no Force sync rebuilt: past the 24 h soft TTL (``_SOFT_TTL``).
STALE_POSTURE_AGE = timedelta(hours=25)


def aged_posture(payload: dict, now: datetime) -> dict:
    """*payload* (a built posture) stamped ``STALE_POSTURE_AGE`` before *now*, so the route serves it as stale."""
    payload["generated_at"] = (now - STALE_POSTURE_AGE).isoformat()
    return payload


def stale_ctem_posture(environ: dict) -> bool:
    """``LAB_CTEM_STALE=on`` (#12381): each posture build is cached aged until a Force sync; whether it did.

    The posture route serves the cached copy as stale (its refresh spinner, the Out of date stamp) at every reload,
    since each background rebuild is aged again. ``POST /ctem-metrics/sync`` (``force=True``) stores its build as
    built and marks the (user, vertical) synced, so later builds stay fresh: the Force sync completes in the lab.
    """
    if environ.get("LAB_CTEM_STALE", "").strip().lower() != "on":
        return False
    build = ctem_metrics._build_and_cache
    synced: set[str] = set()

    async def aged(db, user, vertical, client, *, force: bool = False):
        key = f"{user.id}:{vertical}"
        if force:
            synced.add(key)
        payload, stage_facts = await build(db, user, vertical, client, force=force)
        if key in synced:
            return payload, stage_facts
        payload = aged_posture(payload, datetime.now(UTC))
        posture_key, _ = ctem_metrics._posture_cache_keys(user.id, vertical)
        await ctem_metrics._cache_set_json(client, posture_key, payload, ctem_metrics._HARD_TTL)
        return payload, stage_facts

    # The route, ``/sync`` and the background refresh call it by its module attribute, so that attribute is the one used.
    ctem_metrics._build_and_cache = aged
    return True


stale_ctem_posture(dict(os.environ))


def modules_ctem_posture(environ: dict) -> bool:
    """``LAB_CTEM_MODULES=cti|cti-aev|all`` (#11998): the cyber posture of that module state; whether it did.

    ``ctem_sample.modules_facts`` answers the cyber collection (the Discovery, Scoping and Prioritization lists
    included), the live one answers every other vertical. The collector's own steps then run on it (#12220): with
    OpenAEV, ``_outcome_matrix`` turns ``ctem_sample.OUTCOME_ROWS`` into the Validation hero's matrix, and
    ``_attach_funnel`` turns the funnel inputs into the prioritization ``funnel``. An unknown value is ignored, so
    the lab keeps its live answer.
    """
    state = environ.get("LAB_CTEM_MODULES", "").strip().lower()
    if state not in ctem_sample.MODULE_STATES:
        return False
    live = ctem_metrics.build_posture_for_user

    async def collect(db, user_id: str, vertical: str = ctem_sample.VERTICAL):
        if vertical != ctem_sample.VERTICAL:
            return await live(db, user_id, vertical)
        return modules_posture(state, await profile_reachability(db))

    # The posture route calls the collector by the name it imported, so the route module's attribute is the one used.
    ctem_metrics.build_posture_for_user = collect
    return True


modules_ctem_posture(dict(os.environ))


async def profile_reachability(db) -> dict[str, int] | None:
    """The reachability split of the organization profile's assets, as the live collector reads it (#103).

    ``None`` when the profile holds no asset (the Exposed layer stays not configured); with assets, the sample
    funnel's Exposed layer answers not available, reason ``assets_not_linked``, with this split.
    """
    from app.services import ctem_metrics_collector

    return ctem_metrics_collector._profile_reachability(await ctem_metrics_collector._profile_asset_rows(db))


def modules_posture(state: str, reachability: dict[str, int] | None = None) -> tuple[dict, set[str]]:
    """The cyber facts and present products of one ``LAB_CTEM_MODULES`` state, through the collector's steps."""
    from app.services import ctem_metrics_collector

    facts, present = ctem_sample.modules_facts(state)
    if "openaev" in present:
        rows = [dict(row) for row in ctem_sample.OUTCOME_ROWS]
        phases = [dict(phase) for phase in ctem_sample.OUTCOME_PHASES]
        facts["validation"]["outcome_matrix"] = ctem_metrics_collector._outcome_matrix(rows, phases)
    ctem_metrics_collector._attach_funnel(facts, present, reachability)
    return facts, present


def tailor_kev(stage_facts: dict, stack: list[dict]) -> dict:
    """*stage_facts* with the KEV count narrowed to *stack*: ``ctem_sample.tailored_kev`` on Prioritization."""
    if not stack:
        return stage_facts
    facts = {stage: dict(row) for stage, row in stage_facts.items()}
    facts.setdefault("prioritization", {}).update(ctem_sample.tailored_kev([entry["label"] for entry in stack]))
    return facts


def tailored_ctem_posture(environ: dict) -> bool:
    """``LAB_CTEM_KEV_TAILORED=on``: the cyber KEV count is narrowed to the saved tech stack; whether it is.

    Nothing answers OpenCTI here, so the collector's tailoring query (``_octi_kev_tailored``) never runs: the
    saved organization software (``ctem_metrics_collector._organization_software``, the route's own reader,
    #1495) is matched by ``ctem_sample.tailored_kev`` on the posture the other switches answer. No stack, no
    tailored count, as live.
    """
    if environ.get("LAB_CTEM_KEV_TAILORED", "").strip().lower() != "on":
        return False
    from app.services import ctem_metrics_collector

    collector = ctem_metrics.build_posture_for_user

    async def collect(db, user_id: str, vertical: str = ctem_sample.VERTICAL):
        stage_facts, present = await collector(db, user_id, vertical)
        if vertical != ctem_sample.VERTICAL or "opencti" not in present:
            return stage_facts, present
        return tailor_kev(stage_facts, await ctem_metrics_collector._organization_software(db)), present

    # The posture route calls the collector by the name it imported, so the route module's attribute is the one used.
    ctem_metrics.build_posture_for_user = collect
    return True


tailored_ctem_posture(dict(os.environ))


async def _stored_today(db, user_id, vertical: str):
    """The stored today snapshot of *user_id* in *vertical*, or ``None``."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models.ctem_posture_snapshot import CtemPostureSnapshot

    return (
        await db.execute(
            select(CtemPostureSnapshot).where(
                CtemPostureSnapshot.user_id == user_id,
                CtemPostureSnapshot.vertical == vertical,
                CtemPostureSnapshot.snapshot_date == datetime.now(UTC).date(),
            )
        )
    ).scalar_one_or_none()


def stored_posture(snapshot) -> tuple[dict, dict[str, dict]]:
    """The ``(posture, stage_facts)`` the history upsert writes back as *snapshot* already holds it."""
    metrics = snapshot.stage_metrics or {}
    posture = {
        "posture_score": snapshot.posture_score,
        "stages": [{"key": key, "score": (row or {}).get("score")} for key, row in metrics.items()],
    }
    facts = {key: {k: v for k, v in (row or {}).items() if k != "score"} for key, row in metrics.items()}
    return posture, facts


def keep_seeded_posture_today(environ: dict) -> bool:
    """``LAB_CTEM_POSTURE_HISTORY=1``: a stored cyber today snapshot is written back as stored; whether it did."""
    if environ.get("LAB_CTEM_POSTURE_HISTORY") != "1":
        return False
    if environ.get("LAB_CTEM_SCREENS", "").strip().lower() == "on" or environ.get("LAB_CTEM_MODULES"):
        # The screens and module samples are today's live answer: recorded as collected, never replaced by the seeded row.
        return False
    record = ctem_metrics._record_and_load_history

    async def keep(db, user_id, vertical, posture, stage_facts):
        if vertical == ctem_sample.VERTICAL:
            try:
                stored = await _stored_today(db, user_id, vertical)
            except Exception:  # noqa: BLE001 - history is best-effort, as in the route
                stored = None
            if stored is not None:
                posture, stage_facts = stored_posture(stored)
        return await record(db, user_id, vertical, posture, stage_facts)

    # ``_build_posture_payload`` calls the recorder by its module attribute, so that attribute is the one used.
    ctem_metrics._record_and_load_history = keep
    return True


keep_seeded_posture_today(dict(os.environ))


#: The switches whose lab answers the cyber collection with a sample of its own, left as that sample; one list with
#: ``lab.seeds_ctem_large_facts`` (``ctem_sample.CTEM_SAMPLE_SWITCHES``). ``LAB_CTEM_OPENCTI`` is among them (#12220):
#: its lab reads the lab's own OpenCTI, whose KEV and critical counts the overlay would replace.
CTEM_SAMPLE_SWITCHES: tuple[str, ...] = ctem_sample.CTEM_SAMPLE_SWITCHES


def large_ctem_facts(environ: dict) -> bool:
    """``LAB_CTEM_POSTURE_HISTORY=1``: the cyber facts carry ``ctem_sample.LARGE_FACTS``; whether they do.

    The live collection answers every count at 0 here, so a compact-number review of ``/`` and ``/ctem/<stage>``
    had no figure of 1,000 or more to read. The counts feed no score; the labs of a CTEM sample keep theirs.
    """
    if environ.get("LAB_CTEM_POSTURE_HISTORY") != "1" or environ.get("LAB_CTEM_SAMPLE") == "1":
        return False
    if any(environ.get(name, "").strip().lower() == "on" for name in CTEM_SAMPLE_SWITCHES):
        return False
    if environ.get("LAB_CTEM_MODULES", "").strip().lower() in ctem_sample.MODULE_STATES:
        return False
    live = ctem_metrics.build_posture_for_user

    async def collect(db, user_id: str, vertical: str = ctem_sample.VERTICAL):
        facts, present = await live(db, user_id, vertical)
        if vertical != ctem_sample.VERTICAL:
            return facts, present
        return ctem_sample.with_large_facts(facts or {}), present

    # The posture route calls the collector by the name it imported, so the route module's attribute is the one used.
    ctem_metrics.build_posture_for_user = collect
    return True


large_ctem_facts(dict(os.environ))


async def _screens_posture(db, user_id: str, vertical: str = ctem_sample.VERTICAL, *, _live=None):
    """The lab's posture collection under ``LAB_CTEM_SCREENS=on``: the screens sample for cyber, live otherwise."""
    if vertical != ctem_sample.VERTICAL:
        return await _live(db, user_id, vertical)
    from app.services import ctem_metrics_collector

    facts, present = ctem_sample.screens_facts(), set(ctem_sample.SCREENS_PRESENT)
    # The collector's own step: the funnel inputs become the prioritization ``funnel`` (live layers), the Exposed
    # layer read from the profile's assets as the live collector does (#103).
    ctem_metrics_collector._attach_funnel(facts, present, await profile_reachability(db))
    return facts, present


def screens_ctem_posture(environ: dict) -> bool:
    """``LAB_CTEM_SCREENS=on``: five scored cyber stages and a live funnel, OpenCTI and OpenAEV present; whether."""
    if environ.get("LAB_CTEM_SCREENS", "").strip().lower() != "on":
        return False
    live = ctem_metrics.build_posture_for_user

    async def collect(db, user_id: str, vertical: str = ctem_sample.VERTICAL):
        return await _screens_posture(db, user_id, vertical, _live=live)

    # The posture route calls the collector by the name it imported, so the route module's attribute is the one used.
    ctem_metrics.build_posture_for_user = collect
    return True


screens_ctem_posture(dict(os.environ))


def screens_source_issues(now: datetime, hidden: tuple[str, ...] = ()) -> list:
    """``ctem_sample.SCREENS_ISSUES`` as ``ctem_cases.SourceIssue`` rows, first seen ``days_ago`` before *now*.

    *hidden* (``source:external_id``) are left out: the viewer's own read does not return them.
    """
    from app.services.ctem_cases import SourceIssue

    rows = []
    for raw in ctem_sample.SCREENS_ISSUES:
        fields = {k: v for k, v in raw.items() if k != "days_ago"}
        issue = SourceIssue(**fields, first_seen_at=now - timedelta(days=raw["days_ago"]))
        if issue.member_id not in hidden:
            rows.append(issue)
    return rows


async def screens_viewer_email(db, user_id) -> str | None:
    """The email of *user_id*, or ``None`` when it cannot be read (the viewer then reads every issue)."""
    if db is None:
        return None
    try:
        import uuid

        from sqlalchemy import select

        from app.models.user import User

        viewer = uuid.UUID(str(user_id))
        return (await db.execute(select(User.email).where(User.id == viewer))).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - lab only: an unreadable viewer reads the whole sample
        return None


def screens_ctem_cases(environ: dict) -> bool:
    """``LAB_CTEM_SCREENS=on``: the CTEM Cases source read answers ``SCREENS_ISSUES``, every one open; whether.

    ``read_open_issues`` is called by its module attribute (recompute and every viewer's view), so replacing it
    feeds the real grouping, store and view: ``lab.py`` runs the real recompute, which creates the cases once
    and merges into the same ones after a restart (the keys are stable). A viewer named in
    ``ctem_sample.SCREENS_RESTRICTED`` reads without its listed issues, so the real view marks the case
    ``aggregates_restricted`` for that viewer only (merge-only recompute: a narrower read never closes them).

    The stub takes every keyword argument of the real read (the recompute passes ``remap=True``): a missing one
    made the recompute answer 500 and the lab start with no case (#2081). The sample has no OpenAEV member
    gone at source, so *remap* reads the same issues.
    """
    if environ.get("LAB_CTEM_SCREENS", "").strip().lower() != "on":
        return False
    from app.services import ctem_cases_source

    async def read_open_issues(db, user_id, vertical, *, now=None, members=None, remap=False):  # noqa: ARG001
        out = ctem_cases_source.SourceRead()
        if ctem_cases_source.normalize_vertical(vertical) != ctem_sample.VERTICAL:
            return out
        email = await screens_viewer_email(db, user_id)
        hidden = ctem_sample.SCREENS_RESTRICTED.get((email or "").lower(), ())
        out.issues = screens_source_issues(now or datetime.now(UTC), hidden)
        return out

    ctem_cases_source.read_open_issues = read_open_issues
    return True


screens_ctem_cases(dict(os.environ))


#: Minutes before the build at which each connected product last synced under ``LAB_CTEM_ACTIVITY=on``: the
#: ``product_sync`` row of ``ctem_activity_timeline.json``, the one timeline the seed and the feed's test read.
LAB_PRODUCT_SYNC_MINUTES = json.loads(
    (Path(__file__).resolve().parent / "ctem_activity_timeline.json").read_text()
)["product_sync"]


def stamp_product_syncs(payload: dict, now) -> dict:
    """*payload* with ``products.<p>.last_sync`` set for each present product, ``LAB_PRODUCT_SYNC_MINUTES`` before *now*."""
    from datetime import timedelta

    for product, row in (payload.get("products") or {}).items():
        if isinstance(row, dict) and row.get("present") and not row.get("coming_soon"):
            minutes = LAB_PRODUCT_SYNC_MINUTES.get(product, 15)
            row["last_sync"] = (now - timedelta(minutes=minutes)).isoformat()
    return payload


def activity_ctem_posture(environ: dict) -> bool:
    """``LAB_CTEM_ACTIVITY=on``: the findings collection for cyber, and a ``last_sync`` per connected product.

    The live payload answers ``last_sync`` null (no collector reports one yet), so the posture build is wrapped
    and each present product gets a sync time: the deterministic activity feed of ``/dashboard`` then lists its
    "data synced" rows. Whether it did.
    """
    if environ.get("LAB_CTEM_ACTIVITY", "").strip().lower() != "on":
        return False
    from datetime import UTC, datetime

    findings_ctem_posture({"LAB_CTEM_FINDINGS": "on"})
    build = ctem_metrics._build_posture_payload

    async def stamped(db, user, vertical):
        payload, stage_facts = await build(db, user, vertical)
        return stamp_product_syncs(payload, datetime.now(UTC)), stage_facts

    # ``_build_and_cache`` calls the builder by its module attribute, so that attribute is the one used.
    ctem_metrics._build_posture_payload = stamped
    return True


activity_ctem_posture(dict(os.environ))


#: The OpenCTI Organizations the lab's search answers, in the shape ``search_identities`` reads.
LAB_OPENCTI_ORGANIZATIONS: list[dict] = [
    {"id": "identity--0b5c3e2a-lab0-4000-8000-000000000001", "name": "Acme Corporation",
     "x_opencti_aliases": ["ACME", "Acme Corp"], "x_opencti_organization_type": "vendor"},
    {"id": "identity--0b5c3e2a-lab0-4000-8000-000000000002",
     "name": "Acme International Logistics and Maritime Freight Forwarding Holdings Limited (Europe, Middle East and Africa)",
     "x_opencti_aliases": [], "x_opencti_organization_type": None},
    {"id": "identity--0b5c3e2a-lab0-4000-8000-000000000003", "name": "Initech",
     "x_opencti_aliases": ["Acme Labs", "Initech Software"], "x_opencti_organization_type": "partner"},
    {"id": "identity--0b5c3e2a-lab0-4000-8000-000000000004", "name": "Globex",
     "x_opencti_aliases": ["Globex Corporation"], "x_opencti_organization_type": "other"},
]
#: The search term that reaches the real OpenCTI caller, for the picker's error state.
LAB_OPENCTI_ERROR_SEARCH = "lab:error"
#: The Organization id whose read reaches the real OpenCTI caller, for the read's error state (409).
LAB_OPENCTI_ERROR_ID = "identity--lab-error"


def lab_opencti_read(query: str, variables: dict | None) -> dict | None:
    """The GraphQL data the lab answers for the read of one Organization by id, or ``None`` for the real caller.

    A fixture id answers its node, any other id ``organization: null`` (the route's 404), and
    :data:`LAB_OPENCTI_ERROR_ID` the real caller (its 409).
    """
    if "query XtmOrg(" not in query:
        return None
    wanted = str((variables or {}).get("id") or "").strip()
    if wanted == LAB_OPENCTI_ERROR_ID:
        return None
    node = next((n for n in LAB_OPENCTI_ORGANIZATIONS if n["id"] == wanted), None)
    return {"organization": dict(node) if node else None}


def lab_opencti_search(query: str, variables: dict | None) -> dict | None:
    """The GraphQL data the lab answers for an Organization search, or ``None`` when the real caller answers."""
    if "XtmOrgSearch" not in query:
        return None
    search = str((variables or {}).get("search") or "").strip()
    if search.lower() == LAB_OPENCTI_ERROR_SEARCH:
        return None
    wanted = search.lower()
    first = int((variables or {}).get("first") or 20)
    nodes = [
        n for n in LAB_OPENCTI_ORGANIZATIONS
        if any(wanted in str(v).lower() for v in [n["name"], *n["x_opencti_aliases"]])
    ][:first]
    return {"organizations": {"edges": [{"node": dict(n)} for n in nodes]}}


#: The ``globalCount`` the lab answers for the run estimate's count query on a fixture Organization.
LAB_RUN_ESTIMATE_COUNT = 42


def lab_opencti_count(query: str, variables: dict | None) -> dict | None:
    """The GraphQL data the lab answers for the run estimate's count query, or ``None`` for the real caller.

    ``organization_run_cap.estimate_runs`` sends ``XtmRunEstimate`` with the organization's OpenCTI
    identity in its one filter group: a fixture id answers :data:`LAB_RUN_ESTIMATE_COUNT`, any other
    id the real caller (its error, the per-assignment error state).
    """
    if "XtmRunEstimate" not in query:
        return None
    groups = ((variables or {}).get("filters") or {}).get("filterGroups") or []
    named = {str(v) for g in groups for f in g.get("filters") or [] for v in f.get("values") or [] if isinstance(v, str)}
    if not named & {n["id"] for n in LAB_OPENCTI_ORGANIZATIONS}:
        return None
    return {"stixCoreObjects": {"pageInfo": {"globalCount": LAB_RUN_ESTIMATE_COUNT}}}


def stub_opencti_identities(environ: dict) -> bool:
    """Answer the OpenCTI Organization search, read by id and run estimate count from the fixture when *environ* asks."""
    if environ.get("LAB_OPENCTI_IDENTITIES") != "1":
        return False
    live = organization_opencti.opencti_gql

    async def opencti_gql(db, user_id: str):
        real = await live(db, user_id)
        if isinstance(real, str):
            # No registered OpenCTI: the route's own 409 stays as it is.
            return real

        async def gql(query: str, variables: dict | None = None):
            data = lab_opencti_search(query, variables)
            if data is None:
                data = lab_opencti_read(query, variables)
            if data is None:
                data = lab_opencti_count(query, variables)
            if data is not None:
                return data, None
            return await real(query, variables)

        return gql

    # The route imports the caller by name at each request, so the module attribute is the one it calls.
    organization_opencti.opencti_gql = opencti_gql
    return True


stub_opencti_identities(dict(os.environ))


#: What a platform that is not connected answers the organization import with under ``LAB_ORG_IMPORT=1``
#: (#65): raw entities in each platform's own shape, mapped by the import's real mappers.
LAB_ORG_IMPORT_OPENCTI_SYSTEMS = [
    {"id": "system--lab-0001", "name": "Payments API gateway", "x_opencti_system_category": "web_application",
     "x_opencti_criticality": "very_high", "x_opencti_internet_facing": "yes", "x_opencti_environment": "production",
     "x_opencti_platform": "Linux", "objectLabel": [{"value": "pci"}], "createdBy": {"name": "Lab sample SOC"}},
    {"id": "system--lab-0002", "name": "Domain controller DC-01", "x_opencti_system_category": "host",
     "x_opencti_system_subcategory": "server", "x_opencti_criticality": "high", "x_opencti_internet_facing": "no",
     "x_opencti_environment": "production", "objectMarking": [{"definition": "TLP:AMBER"}]},
    {"id": "system--lab-0003", "name": "Build farm runner pool with a name long enough to wrap in the preview sample"},
]
LAB_ORG_IMPORT_OPENAEV_ASSETS = [
    {"asset_id": "lab-aev-0001", "asset_name": "FINANCE-LAPTOP-07", "endpoint_platform": "Windows",
     "asset_criticality": "medium", "asset_internet_facing": False},
    {"asset_id": "lab-aev-0002", "asset_name": "web-frontend-02", "endpoint_platform": "Linux",
     "asset_criticality": "high", "asset_internet_facing": True},
]
LAB_ORG_IMPORT_OPENAEV_GROUPS = [
    {"asset_group_id": "lab-aev-group-0001", "asset_group_name": "Finance endpoints",
     "asset_group_description": "Every laptop of the finance team."},
]
LAB_ORG_IMPORT_OPENAEV_PLATFORMS = [{"security_platform_name": "Microsoft Defender for Endpoint"}, {"asset_name": "CrowdStrike Falcon"}]


def lab_org_import_read(section: str, source: str, batch_type: str = ""):
    """The fixture read of *section* from *source* (a ``ReadResult``), or ``None`` when the lab has none for it."""
    imp = organization_context_import
    rows: list = []
    if (section, source) == ("assets", "opencti"):
        rows = [imp.opencti_system(node, batch_type) for node in LAB_ORG_IMPORT_OPENCTI_SYSTEMS]
    elif (section, source) == ("assets", "openaev"):
        rows = [imp.openaev_asset(asset, batch_type) for asset in LAB_ORG_IMPORT_OPENAEV_ASSETS]
    elif (section, source) == ("asset_groups", "openaev"):
        rows = [imp.openaev_asset_group(group) for group in LAB_ORG_IMPORT_OPENAEV_GROUPS]
    elif (section, source) == ("software", "openaev"):
        names = [str(p.get("asset_name") or p.get("security_platform_name")) for p in LAB_ORG_IMPORT_OPENAEV_PLATFORMS]
        rows = [imp.SoftwareEntry(product=name) for name in names]
    else:
        return None
    rows = [row for row in rows if row]
    return imp.ReadResult(rows=rows, total=len(rows))


def _not_connected(error: str | None, source: str = "", environ: dict | None = None) -> bool:
    """Whether the real read failed because no reachable platform answers it, so the fixture stands in.

    "not connected" (no integration or no token), and "could not connect" too: the lab registers each Suite
    platform at ``https://<provider>.lab.invalid`` (``seed_suite_registrations``), which no worker reaches, so the
    real path answers an outage, not "not connected" (gap lab-org-synced-import-preview-empty). A lab with its own
    OpenCTI (``LAB_OPENCTI = "live"``) keeps the real OpenCTI answer, an outage included.
    """
    text = (error or "").lower()
    if "not connected" in text:
        return True
    if "could not connect" not in text:
        return False
    live = (environ or {}).get("LAB_OPENCTI", "").strip().lower() == "live"
    return not (source == "opencti" and live)


def stub_org_import(environ: dict) -> bool:
    """Answer the organization import preview, apply and syncs from the fixture where the platform is not connected.

    Only a read the real path answers "not connected" is replaced, and only the source status that real path
    reports unavailable: a lab with a live OpenCTI or OpenAEV (``LAB_OPENCTI = "live"``) keeps its real answers.
    """
    if environ.get("LAB_ORG_IMPORT") != "1":
        return False
    imp = organization_context_import
    live_read, live_statuses = imp.read_source, imp.source_statuses

    async def read_source(db, *, user_id, section, source, filters, batch_type=""):
        real = await live_read(
            db, user_id=user_id, section=section, source=source, filters=filters, batch_type=batch_type
        )
        if not _not_connected(real.error, source, environ):
            return real
        return lab_org_import_read(section, source, batch_type) or real

    async def source_statuses(db, *, user_id):
        statuses = await live_statuses(db, user_id=user_id)
        for source in ("opencti", "openaev"):
            status = statuses.get(source)
            if status is not None and not status.available:
                statuses[source] = imp.SourceStatus(source=source, available=True)
        return statuses

    # ``preview_import`` (the preview, the apply and every sync run) and the routes call these by module
    # attribute at each request, so the module attributes are the ones they call.
    imp.read_source = read_source
    imp.source_statuses = source_statuses
    return True


stub_org_import(dict(os.environ))


#: The PIRs the lab's OpenCTI answers the PIR picker with, in the ``{id, name}`` rows ``list_pirs`` answers.
LAB_OPENCTI_PIRS = [
    {"id": "5b1e0c3a-7d2f-4e61-9a10-000000000001", "name": "Ransomware targeting financial services in Europe"},
    {"id": "5b1e0c3a-7d2f-4e61-9a10-000000000002", "name": "State-sponsored espionage against the energy sector"},
    {"id": "5b1e0c3a-7d2f-4e61-9a10-000000000003", "name": "Phishing campaigns impersonating our brand"},
    {"id": "5b1e0c3a-7d2f-4e61-9a10-000000000004", "name": "Exploited vulnerabilities in our edge devices"},
    {"id": "5b1e0c3a-7d2f-4e61-9a10-000000000005", "name": "Supply chain compromise of our software vendors"},
    {"id": "5b1e0c3a-7d2f-4e61-9a10-000000000006", "name": "Leaked credentials of our employees"},
    {
        "id": "5b1e0c3a-7d2f-4e61-9a10-000000000007",
        "name": "Initial access brokers selling footholds in European and Asia-Pacific manufacturing subsidiaries",
    },
    {"id": "5b1e0c3a-7d2f-4e61-9a10-000000000008", "name": "Hacktivist DDoS against public services"},
    {"id": "5b1e0c3a-7d2f-4e61-9a10-000000000009", "name": "Insider threat in cloud administration"},
]
#: A PIR id that, saved in the profile, makes the lab answer the picker ``unreadable`` (the route's error path).
LAB_OPENCTI_PIRS_ERROR = "lab:pirs-error"
#: With ``LAB_OPENCTI_PIRS_SAME_NAME=on``: two more PIRs whose names repeat fixture names, so the picker's
#: disambiguation (name and short id) renders on a real answer (#11713). Off by default: names stay unique.
LAB_OPENCTI_PIRS_SAME_NAME = [
    {"id": "5b1e0c3a-7d2f-4e61-9a10-000000000010", "name": "Ransomware targeting financial services in Europe"},
    {"id": "5b1e0c3a-7d2f-4e61-9a10-000000000011", "name": "Leaked credentials of our employees"},
]


def lab_pirs(environ: dict) -> list[dict[str, str]]:
    """The PIR rows the lab answers: the fixture, plus the same-name pair when *environ* asks."""
    rows = [dict(row) for row in LAB_OPENCTI_PIRS]
    if environ.get("LAB_OPENCTI_PIRS_SAME_NAME", "").strip().lower() == "on":
        rows += [dict(row) for row in LAB_OPENCTI_PIRS_SAME_NAME]
    return rows


def stub_opencti_pirs(environ: dict) -> bool:
    """Answer the PIR picker's list from :data:`LAB_OPENCTI_PIRS` when *environ* asks.

    ``list_pirs`` is imported by name at each request of the route, so the module attribute is the one it calls.
    An administrator with no OpenCTI token keeps ``not_connected``; a saved profile carrying
    :data:`LAB_OPENCTI_PIRS_ERROR` answers ``unreadable``.
    """
    if environ.get("LAB_OPENCTI_PIRS") != "1":
        return False
    from app.services import ctem_metrics_collector, organization_context, organization_context_draft

    answered = lab_pirs(environ)

    async def list_pirs(db, *, user_id):
        # The real signature (#9615): rows, why they cannot be read, OpenCTI's base URL (empty: not connected).
        creds = await ctem_metrics_collector._user_creds(db, str(user_id), "opencti")
        if creds is None:
            return [], "not_connected", ""
        link_base = str(creds[0]).rstrip("/")
        current = await organization_context.current_version(db)
        saved = ((current.profile or {}).get("pir_ids") or []) if current is not None else []
        if LAB_OPENCTI_PIRS_ERROR in saved:
            return [], "unreadable", link_base
        return [dict(row) for row in answered], None, link_base

    organization_context_draft.list_pirs = list_pirs
    return True


stub_opencti_pirs(dict(os.environ))


#: The platform channels the lab's Slack channel bot lists, in the shape ``list_channel_bot_channels`` answers.
LAB_SLACK_CHANNELS = [
    {"id": "C0LAB00001", "name": "security-alerts", "group": "public"},
    {"id": "C0LAB00002", "name": "threat-intel", "group": "public"},
    {"id": "G0LAB00003", "name": "incident-response", "group": "private"},
]


def stub_slack_channels(environ: dict) -> bool:
    """Answer the channel list of the lab's Slack channel bot from :data:`LAB_SLACK_CHANNELS`.

    Only for the bot whose token is ``LAB_SLACK_BOT_TOKEN`` (set by ``lab.py``, which seeds that bot):
    any other Slack bot goes to the real ``conversations.list`` caller. ``_list_platform_channels`` calls
    ``_list_slack_channels`` by name at each request, so the module attribute is the one it calls.
    """
    lab_token = environ.get("LAB_SLACK_BOT_TOKEN", "")
    if not lab_token:
        return False
    from app.api.v1 import channel_bots
    from app.services.vault import reveal_field

    real = channel_bots._list_slack_channels

    async def list_slack_channels(bot):
        if (await reveal_field(bot, "api_key_enc")) == lab_token:
            return [dict(row) for row in LAB_SLACK_CHANNELS]
        return await real(bot)

    channel_bots._list_slack_channels = list_slack_channels
    return True


stub_slack_channels(dict(os.environ))


#: The teams and channels the lab's Teams channel bot lists, in the shape ``_list_teams_channels`` answers
#: (``<team id>:<channel id>``, the team's name as the group).
LAB_TEAMS_CHANNELS = [
    {"id": "lab-team-soc:19:lab-general@thread.tacv2", "name": "General", "group": "Security Operations"},
    {"id": "lab-team-soc:19:lab-alerts@thread.tacv2", "name": "Alerts triage", "group": "Security Operations"},
    {"id": "lab-team-cti:19:lab-reports@thread.tacv2", "name": "Intelligence reports", "group": "Threat Intelligence"},
]


def stub_teams_channels(environ: dict) -> bool:
    """Answer the channel list of the lab's Teams channel bot from :data:`LAB_TEAMS_CHANNELS`.

    Only for the bot whose client id is ``LAB_TEAMS_BOT_CLIENT_ID`` (set by ``lab.py``, which seeds that bot):
    any other Teams bot goes to the real Microsoft Graph caller. ``_list_platform_channels`` calls
    ``_list_teams_channels`` by name at each request, so the module attribute is the one it calls.
    """
    lab_client_id = environ.get("LAB_TEAMS_BOT_CLIENT_ID", "")
    if not lab_client_id:
        return False
    from app.api.v1 import channel_bots

    real = channel_bots._list_teams_channels

    async def list_teams_channels(bot):
        client_id = channel_bots.decrypt_value(bot.client_id_enc) if bot.client_id_enc else None
        if client_id == lab_client_id:
            return [dict(row) for row in LAB_TEAMS_CHANNELS]
        return await real(bot)

    channel_bots._list_teams_channels = list_teams_channels
    return True


stub_teams_channels(dict(os.environ))


#: Who the saveable agent account's Microsoft integration is signed in as, its chats and channels, and its directory:
#: the answers of the agent account pickers for that integration (``LAB_AGENT_ACCOUNT_INTEGRATION_ID``).
LAB_AGENT_ACCOUNT_IDENTITY = {
    "external_id": "lab-agent-account-user",
    "display_name": "Lab Admin",
    "email": "lab-agent-account@example.com",
}
LAB_AGENT_ACCOUNT_CONVERSATIONS = [
    {"id": "19:lab-chat-soc@thread.v2", "name": "SOC handover", "type": "group", "team_id": None},
    {"id": "19:lab-chat-direct@unq.gbl.spaces", "name": "Lab Analyst", "type": "direct", "team_id": None},
    {"id": "19:lab-alerts@thread.tacv2", "name": "Security Operations > Alerts triage", "type": "channel",
     "team_id": "lab-team-soc"},
]
LAB_AGENT_ACCOUNT_PEOPLE = [
    {"id": "lab-person-analyst", "name": "Lab Analyst", "email": "lab-analyst@example.com"},
    {"id": "lab-person-manager", "name": "Lab Manager", "email": "lab-manager@example.com"},
    {"id": "lab-person-ciso", "name": "Lab CISO", "email": "lab-ciso@example.com"},
]


class _LabPickerProvider:
    """The account provider of the saveable agent account's pickers: the real one, its directory read from the fixture."""

    def __init__(self, real) -> None:
        self._real = real

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    async def list_conversations(self, _session, query: str | None):
        from app.services.agent_accounts.events import ConversationOption

        term = (query or "").strip().lower()
        return [ConversationOption(**row) for row in LAB_AGENT_ACCOUNT_CONVERSATIONS if term in row["name"].lower()]

    async def search_people(self, _session, query: str):
        from app.services.agent_accounts.events import PersonOption

        term = query.strip().lower()
        return [
            PersonOption(**row) for row in LAB_AGENT_ACCOUNT_PEOPLE
            if term in row["name"].lower() or term in row["email"].lower()
        ]


def stub_agent_account_pickers(environ: dict) -> bool:
    """Answer the agent account pickers of the lab's Microsoft integration from the fixtures above.

    Only for the integration whose id is ``LAB_AGENT_ACCOUNT_INTEGRATION_ID`` (set by ``lab.py``, which creates it and
    binds it to the saveable agent account): nothing signs it in, so the real session answers 502 "The integration is
    not connected". The integration is still checked as the route checks it (visible to the caller, of the account's
    provider). Any other integration goes to the real session. The routes call ``_picker_session`` by name at each
    request, so the module attribute is the one they call.
    """
    lab_integration_id = environ.get("LAB_AGENT_ACCOUNT_INTEGRATION_ID", "")
    if not lab_integration_id:
        return False
    from app.api.v1 import agent_accounts
    from app.services.agent_accounts import management
    from app.services.agent_accounts.events import AccountIdentity
    from app.services.agent_accounts.providers import ProviderSession

    real = agent_accounts._picker_session

    async def picker_session(db, user, provider_key: str, integration_id: str):
        if str(integration_id) != lab_integration_id:
            return await real(db, user, provider_key, integration_id)
        with agent_accounts._answered():
            provider = management.provider_for(provider_key)
            await management.check_integration(db, user, provider, integration_id)
        identity = AccountIdentity(**LAB_AGENT_ACCOUNT_IDENTITY)
        session = ProviderSession(
            token="lab-only-agent-account-token",  # noqa: S106 - never sent: the fixtures answer
            account_external_id=identity.external_id,
            integration_id=lab_integration_id,
        )
        return _LabPickerProvider(provider), session, identity

    agent_accounts._picker_session = picker_session
    return True


stub_agent_account_pickers(dict(os.environ))


#: The OpenRouter catalog the lab's OpenRouter integration lists, as OpenRouter's ``GET /models`` sends it: three
#: authors, free and paid, tools and reasoning, so the author picker, the sort and the filters have rows to act on.
LAB_OPENROUTER_TEXT_MODELS: list[dict] = [
    {"id": "openai/gpt-lab-mini", "name": "OpenAI: GPT Lab Mini", "created": 1767225600,
     "description": "A small lab model: fast, cheap, tool use.", "context_length": 128000,
     "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
     "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
     "top_provider": {"context_length": 128000, "max_completion_tokens": 16384, "is_moderated": True},
     "supported_parameters": ["tools", "response_format", "temperature"]},
    {"id": "openai/gpt-lab-reasoner", "name": "OpenAI: GPT Lab Reasoner", "created": 1769904000,
     "description": "A lab reasoning model.", "context_length": 200000,
     "pricing": {"prompt": "0.0000011", "completion": "0.0000044"},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
     "top_provider": {"context_length": 200000, "max_completion_tokens": 100000, "is_moderated": True},
     "supported_parameters": ["tools", "reasoning", "include_reasoning"]},
    {"id": "mistralai/mistral-lab-large", "name": "Mistral: Mistral Lab Large", "created": 1764547200,
     "description": "A large lab model from a second author.", "context_length": 131072,
     "pricing": {"prompt": "0.000002", "completion": "0.000006"},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
     "top_provider": {"context_length": 131072, "max_completion_tokens": None, "is_moderated": False},
     "supported_parameters": ["tools", "temperature"]},
    {"id": "meta-llama/llama-lab-8b-instruct:free", "name": "Meta: Llama Lab 8B Instruct (free)", "created": 1759276800,
     "description": "A free lab model.", "context_length": 8192,
     "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
     "top_provider": {"context_length": 8192, "max_completion_tokens": 4096, "is_moderated": False},
     "supported_parameters": ["temperature"]},
]
#: Its embedding catalog (``GET /embeddings/models``): two authors.
LAB_OPENROUTER_EMBEDDING_MODELS: list[dict] = [
    {"id": "openai/text-embedding-lab-small", "name": "OpenAI: Text Embedding Lab Small", "created": 1761955200,
     "description": "A lab embedding model.", "context_length": 8191,
     "pricing": {"prompt": "0.00000002", "completion": "0"},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["embeddings"]},
     "top_provider": {"context_length": 8191}, "supported_parameters": []},
    {"id": "mistralai/mistral-lab-embed", "name": "Mistral: Mistral Lab Embed", "created": 1762041600,
     "description": "A second lab embedding model.", "context_length": 8192,
     "pricing": {"prompt": "0.0000001", "completion": "0"},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["embeddings"]},
     "top_provider": {"context_length": 8192}, "supported_parameters": []},
]
#: The key's own limit and spend (``GET /key``), for the console's overview.
LAB_OPENROUTER_KEY_INFO = {
    "label": "Lab sample key", "limit": 50, "limit_remaining": 37.5, "limit_reset": "monthly",
    "usage": 12.5, "usage_daily": 0.42, "usage_weekly": 3.1, "usage_monthly": 12.5, "is_free_tier": False,
}
#: The providers serving any lab model (``GET /models/<author>/<slug>/endpoints``).
LAB_OPENROUTER_ENDPOINTS: list[dict] = [
    {"provider_name": "Lab Provider A", "tag": "lab-a", "quantization": "fp8", "context_length": 128000,
     "max_completion_tokens": 16384, "status": 0, "uptime_last_30m": 99.9, "uptime_last_1d": 99.7,
     "latency_last_30m": {"p50": 420}, "throughput_last_30m": {"p50": 85}, "supported_parameters": ["tools"],
     "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}},
    {"provider_name": "Lab Provider B", "tag": "lab-b", "quantization": "bf16", "context_length": 64000,
     "max_completion_tokens": 8192, "status": 0, "uptime_last_30m": 98.2, "uptime_last_1d": 99.1,
     "latency_last_30m": {"p50": 910}, "throughput_last_30m": {"p50": 40}, "supported_parameters": [],
     "pricing": {"prompt": "0.0000002", "completion": "0.0000008"}},
]
#: The account's credits (``GET /credits``, management key), for the console's Credits left tile.
LAB_OPENROUTER_CREDITS = {"total_credits": 2000.0, "total_usage": 1475.25}
#: The models the account spent on over the last 30 days (``GET /activity``, management key): model id, the providers
#: serving it, its spend per provider and day in USD, the day period it is used on (1: every day), its requests and
#: tokens per row. One id over 40 characters, one model on 3 providers, one over $1,000 in 30 days (42 x 30), one
#: under $0.01 (3 rows of $0.0009), one free model, so the per-model table, its truncation and the totals have edges.
LAB_OPENROUTER_ACTIVITY_MODELS: list[tuple[str, tuple[str, ...], float, int, int, int]] = [
    ("anthropic/claude-lab-sonnet-4-5-extended-context-preview", ("Lab Provider A",), 42.0, 1, 900, 2_400_000),
    ("openai/gpt-lab-mini", ("Lab Provider A", "Lab Provider B", "Lab Provider C"), 0.6, 1, 1200, 800_000),
    ("openai/gpt-lab-reasoner", ("Lab Provider B",), 3.5, 2, 140, 420_000),
    ("mistralai/mistral-lab-large", ("Lab Provider C",), 1.25, 1, 260, 310_000),
    ("google/gemini-lab-flash", ("Lab Provider A", "Lab Provider D"), 0.35, 1, 640, 520_000),
    ("meta-llama/llama-lab-8b-instruct:free", ("Lab Provider D",), 0.0, 3, 75, 60_000),
    ("openai/text-embedding-lab-small", ("Lab Provider B",), 0.0009, 10, 12, 9_000),
]


def lab_openrouter_activity(today: datetime | None = None) -> list[dict]:
    """The lab account's ``GET /activity`` rows, as OpenRouter sends them: the 30 completed UTC days before ``today``.

    One row per day, model and provider of :data:`LAB_OPENROUTER_ACTIVITY_MODELS`; the spend varies by up to 20 %
    around the declared value and averages it over every 5 consecutive days, so a model used every day totals 30
    times its declared spend per provider.
    """
    day0 = (today or datetime.now(UTC)).date()
    rows: list[dict] = []
    for index, (model, providers, usage, period, requests, tokens) in enumerate(LAB_OPENROUTER_ACTIVITY_MODELS):
        for back in range(1, 31):
            if back % period:
                continue
            swing = 1 + ((back + index) % 5 - 2) / 10
            for provider in providers:
                rows.append({
                    "date": (day0 - timedelta(days=back)).isoformat(), "model": model, "provider_name": provider,
                    "usage": round(usage * swing, 6), "byok_usage_inference": 0.0,
                    "requests": round(requests * swing), "prompt_tokens": round(tokens * swing * 0.8),
                    "completion_tokens": round(tokens * swing * 0.2), "reasoning_tokens": 0,
                    "cached_tokens": round(tokens * swing * 0.1),
                })
    return rows


#: The keys of the lab account (``GET /keys``, management key) besides the integration's own, as OpenRouter lists
#: them: a name over 80 characters, a disabled key with a monthly limit, a $0 key with an expiry and a key over
#: $1,000 spent, so the API Keys tab has its edges. The integration's own key is added by :func:`lab_openrouter_keys`
#: under its real hash, which the console marks in use by the integration and the platform LLM provider.
LAB_OPENROUTER_OTHER_KEYS: list[dict] = [
    {"name": "Threat intelligence enrichment workers, EU region, production ingestion pipeline (rotated quarterly)",
     "label": "sk-or-v1-a41...7c2", "disabled": False, "limit": None, "limit_remaining": None, "limit_reset": None,
     "usage": 86.4, "usage_daily": 2.1, "usage_weekly": 14.7, "usage_monthly": 61.3, "expires_at": None},
    {"name": "Retired staging key", "label": "sk-or-v1-0d9...e15", "disabled": True, "limit": 100.0,
     "limit_remaining": 100.0, "limit_reset": "monthly", "usage": 0.0, "usage_daily": 0.0, "usage_weekly": 0.0,
     "usage_monthly": 0.0, "expires_at": None},
    {"name": "Contractor trial", "label": "sk-or-v1-77b...3aa", "disabled": False, "limit": 25.0,
     "limit_remaining": 25.0, "limit_reset": None, "usage": 0.0, "usage_daily": 0.0, "usage_weekly": 0.0,
     "usage_monthly": 0.0, "expires_in_days": 21},
    {"name": "Research notebooks", "label": "sk-or-v1-5e0...b48", "disabled": False, "limit": 2500.0,
     "limit_remaining": 1179.6, "limit_reset": None, "usage": 1320.4, "usage_daily": 18.2, "usage_weekly": 121.9,
     "usage_monthly": 410.75, "expires_at": None},
]


def lab_openrouter_keys(api_key: str, today: datetime | None = None) -> dict[str, dict]:
    """The lab account's keys as ``GET /keys`` sends them, by hash: ``api_key``'s own row first, then the others.

    ``api_key`` is the integration's key: its row carries the hash the console computes for it
    (``openrouter.key_hash``), so the route marks it in use. Every other hash is the SHA-256 of a lab-only string.
    """
    now = today or datetime.now(UTC)
    created = (now - timedelta(days=90)).isoformat()
    rows: dict[str, dict] = {}
    own = {"name": "Lab sample key", "label": "sk-or-v1-lab...key", "disabled": False, "limit": 50.0,
           "limit_remaining": 37.5, "limit_reset": "monthly", "usage": 12.5, "usage_daily": 0.42,
           "usage_weekly": 3.1, "usage_monthly": 12.5, "expires_at": None}
    entries = [(hashlib.sha256(api_key.strip().encode()).hexdigest(), own)]
    entries += [(hashlib.sha256(f"lab-openrouter-key-{index}".encode()).hexdigest(), row)
                for index, row in enumerate(LAB_OPENROUTER_OTHER_KEYS)]
    for key_hash, row in entries:
        fields = {k: v for k, v in row.items() if k != "expires_in_days"}
        if "expires_in_days" in row:
            fields["expires_at"] = (now + timedelta(days=row["expires_in_days"])).isoformat()
        rows[key_hash] = {"hash": key_hash, **fields, "include_byok_in_limit": False, "created_at": created,
                          "updated_at": created}
    return rows


#: The models the lab's LM Studio server holds, as LM Studio's ``GET /api/v1/models`` sends them: two publishers,
#: chat and embedding models, loaded and not loaded.
LAB_LMSTUDIO_MODELS: list[dict] = [
    {"key": "qwen/qwen3-lab-8b", "type": "llm", "display_name": "Qwen3 Lab 8B", "publisher": "qwen",
     "architecture": "qwen3", "format": "gguf", "quantization": {"name": "Q4_K_M", "bits_per_weight": 4},
     "size_bytes": 5_030_000_000, "params_string": "8B", "max_context_length": 40960,
     "capabilities": {"trained_for_tool_use": True, "reasoning": {"allowed_options": ["off", "on"]}},
     "description": "A lab chat model, loaded.",
     "loaded_instances": [{"id": "qwen/qwen3-lab-8b", "config": {"context_length": 8192, "parallel": 2}}]},
    {"key": "qwen/qwen3-lab-32b", "type": "llm", "display_name": "Qwen3 Lab 32B", "publisher": "qwen",
     "architecture": "qwen3", "format": "gguf", "quantization": {"name": "Q4_K_M", "bits_per_weight": 4},
     "size_bytes": 19_800_000_000, "params_string": "32B", "max_context_length": 40960,
     "capabilities": {"trained_for_tool_use": True}, "description": "A lab chat model, not loaded.",
     "loaded_instances": []},
    {"key": "mistralai/mistral-lab-small", "type": "llm", "display_name": "Mistral Lab Small",
     "publisher": "mistralai", "architecture": "mistral", "format": "mlx",
     "quantization": {"name": "4bit", "bits_per_weight": 4}, "size_bytes": 13_300_000_000, "params_string": "24B",
     "max_context_length": 131072, "capabilities": {"vision": True, "trained_for_tool_use": True},
     "description": "A lab vision model from a second publisher, not loaded.", "loaded_instances": []},
    {"key": "text-embedding-nomic-lab-v1.5", "type": "embedding", "display_name": "Nomic Embed Lab v1.5",
     "publisher": "nomic-ai", "architecture": "nomic-bert", "format": "gguf",
     "quantization": {"name": "Q8_0", "bits_per_weight": 8}, "size_bytes": 146_000_000, "params_string": "137M",
     "max_context_length": 2048, "description": "A lab embedding model, loaded.",
     "loaded_instances": [{"id": "text-embedding-nomic-lab-v1.5", "config": {"context_length": 2048}}]},
]


def stub_model_catalogs(environ: dict) -> dict[str, bool]:
    """Answer the lab's OpenRouter key and LM Studio server from the fixtures above; which of the two it stubbed.

    ``LAB_OPENROUTER_API_KEY`` (set by ``lab.py``, which seeds one OpenRouter integration with that lab-only key):
    the key info, the text and embedding catalogs and the model endpoints of that key are answered from
    :data:`LAB_OPENROUTER_TEXT_MODELS` and its siblings, run through the client's own normalisers.
    ``LAB_OPENROUTER_MANAGEMENT_KEY`` (the lab-only management key of the same integration): its credits and its
    activity are answered from :data:`LAB_OPENROUTER_CREDITS` and :func:`lab_openrouter_activity`, its keys are listed,
    created, edited and deleted in memory from :func:`lab_openrouter_keys` (the integration's own key among them). ``LAB_LMSTUDIO_BASE_URL``
    (the ``.invalid`` server URL of the seeded LM Studio integration): the model listing of that server is answered
    from :data:`LAB_LMSTUDIO_MODELS` through ``parse_models``. Any other key or server goes to the real client. The
    console routes call the client functions by their module attribute, so the attributes are the ones replaced.
    """
    stubbed = {"openrouter": False, "openrouter_management": False, "lmstudio": False}
    lab_key = environ.get("LAB_OPENROUTER_API_KEY", "")
    if lab_key:
        from app.integrations.providers import openrouter

        real_models, real_key_info = openrouter.cached_models, openrouter.get_key_info
        real_endpoints = openrouter.list_model_endpoints

        async def cached_models(api_key, *, kind: str = "chat"):
            if api_key != lab_key:
                return await real_models(api_key, kind=kind)
            rows = LAB_OPENROUTER_EMBEDDING_MODELS if kind == "embeddings" else LAB_OPENROUTER_TEXT_MODELS
            models = [m for m in (openrouter.normalize_model(row) for row in rows) if m is not None]
            return sorted(models, key=lambda m: m["id"])

        async def get_key_info(api_key):
            if api_key != lab_key:
                return await real_key_info(api_key)
            return openrouter.normalize_key_info(dict(LAB_OPENROUTER_KEY_INFO))

        async def list_model_endpoints(api_key, model_id: str):
            if api_key != lab_key:
                return await real_endpoints(api_key, model_id)
            return [e for e in (openrouter.normalize_endpoint(row) for row in LAB_OPENROUTER_ENDPOINTS) if e is not None]

        openrouter.cached_models = cached_models
        openrouter.get_key_info = get_key_info
        openrouter.list_model_endpoints = list_model_endpoints
        stubbed["openrouter"] = True
    lab_management_key = environ.get("LAB_OPENROUTER_MANAGEMENT_KEY", "")
    if lab_management_key:
        from app.integrations.providers import openrouter

        real_credits, real_activity = openrouter.get_credits, openrouter.get_activity
        real_list_keys, real_create_key = openrouter.list_api_keys, openrouter.create_api_key
        real_update_key, real_delete_key = openrouter.update_api_key, openrouter.delete_api_key
        lab_keys = lab_openrouter_keys(lab_key or "sk-or-lab-only-key-not-configured")

        def lab_row(key_hash: str) -> dict:
            if key_hash not in lab_keys:
                raise openrouter.OpenRouterError("not_found", "OpenRouter has no such resource.")
            return lab_keys[key_hash]

        async def get_credits(management_key):
            if management_key != lab_management_key:
                return await real_credits(management_key)
            total, used = LAB_OPENROUTER_CREDITS["total_credits"], LAB_OPENROUTER_CREDITS["total_usage"]
            return {"total_credits": total, "total_usage": used, "remaining": total - used}

        async def get_activity(management_key, *, date=None, api_key_hash=None):
            if management_key != lab_management_key:
                return await real_activity(management_key, date=date, api_key_hash=api_key_hash)
            rows = [openrouter.normalize_activity_row(row) for row in lab_openrouter_activity()]
            return [r for r in rows if r is not None and (date is None or r["date"] == date)]

        async def list_api_keys(management_key, *, include_disabled: bool = True):
            if management_key != lab_management_key:
                return await real_list_keys(management_key, include_disabled=include_disabled)
            rows = [openrouter.normalize_api_key(dict(row)) for row in lab_keys.values()
                    if include_disabled or not row["disabled"]]
            return openrouter.KeyListing(keys=[r for r in rows if r is not None], truncated=False)

        async def create_api_key(management_key, body):
            if management_key != lab_management_key:
                return await real_create_key(management_key, body)
            secret = f"sk-or-lab-only-created-{len(lab_keys)}"
            key_hash = hashlib.sha256(secret.encode()).hexdigest()
            stamp = datetime.now(UTC).isoformat()
            lab_keys[key_hash] = {
                "hash": key_hash, "name": body.get("name") or "", "label": "sk-or-v1-lab...new", "disabled": False,
                "limit": body.get("limit"), "limit_remaining": body.get("limit"), "limit_reset": body.get("limit_reset"),
                "include_byok_in_limit": bool(body.get("include_byok_in_limit")), "usage": 0.0, "usage_daily": 0.0,
                "usage_weekly": 0.0, "usage_monthly": 0.0, "created_at": stamp, "updated_at": stamp,
                "expires_at": body.get("expires_at"),
            }
            return openrouter.normalize_api_key(dict(lab_keys[key_hash])), secret

        async def update_api_key(management_key, hash_, body):
            if management_key != lab_management_key:
                return await real_update_key(management_key, hash_, body)
            row = lab_row(hash_)
            row.update({k: v for k, v in body.items() if k in row}, updated_at=datetime.now(UTC).isoformat())
            return openrouter.normalize_api_key(dict(row))

        async def delete_api_key(management_key, hash_):
            if management_key != lab_management_key:
                return await real_delete_key(management_key, hash_)
            lab_row(hash_)
            del lab_keys[hash_]

        openrouter.get_credits = get_credits
        openrouter.get_activity = get_activity
        openrouter.list_api_keys = list_api_keys
        openrouter.create_api_key = create_api_key
        openrouter.update_api_key = update_api_key
        openrouter.delete_api_key = delete_api_key
        stubbed["openrouter_management"] = True
    lab_server = environ.get("LAB_LMSTUDIO_BASE_URL", "")
    if lab_server:
        from app.integrations.providers import lmstudio

        real_list = lmstudio.list_models
        lab_root = lmstudio.normalize_base_url(lab_server)

        async def list_models(connection):
            if connection.base_url != lab_root:
                return await real_list(connection)
            return lmstudio.parse_models({"models": [dict(row) for row in LAB_LMSTUDIO_MODELS]})

        lmstudio.list_models = list_models
        stubbed["lmstudio"] = True
    return stubbed


stub_model_catalogs(dict(os.environ))


#: The primary assets the lab's OpenCRQ sync plans over, in the shape ``PrimaryAsset`` validates: one row per plan kind
#: against :data:`LAB_OPENCRQ_ASSETS` (linked by name, ambiguous name, a typed row linked to an asset OpenCRQ
#: lacks). Nothing is pushed since #1591: ``pushed_to_opencrq`` stays empty.
LAB_OPENCRQ_JEWELS = [
    {"name": "Customer portal", "criticality": "high"},
    {"name": "Payment gateway", "criticality": "critical", "opencrq_asset_id": "lab-asset-payments"},
    {"name": "Data warehouse", "criticality": "medium"},
    {"name": "Legacy billing", "criticality": "high", "opencrq_asset_id": "lab-asset-retired", "source": "manual"},
]
#: The primary assets the lab's OpenCRQ answers (#1591), one row per asset.
LAB_OPENCRQ_ASSETS = [
    {"id": "lab-asset-portal", "name": "Customer portal", "criticality": "high"},
    {"id": "lab-asset-payments", "name": "Payment gateway", "criticality": "high"},
    {"id": "lab-asset-dwh-eu", "name": "Data warehouse", "criticality": "medium"},
    {"id": "lab-asset-dwh-us", "name": "Data warehouse", "criticality": "medium"},
    {"id": "lab-asset-idp", "name": "Identity provider", "criticality": "very_high"},
]


def stub_opencrq_sync(environ: dict) -> bool:
    """Answer Check against OpenCRQ with a deterministic plan when *environ* asks; Apply writes nothing.

    ``run_sync`` is imported by name at each request of the route, so the module attribute is the one it calls.
    A dry run answers the real ``plan_sync`` over :data:`LAB_OPENCRQ_JEWELS` and :data:`LAB_OPENCRQ_ASSETS`
    (one row of each kind); ``apply`` answers that plan applied and writes neither the profile nor OpenCRQ, and the
    next dry run answers the plan where everything agrees (no row: the panel's "already agree" state), once.
    """
    if environ.get("LAB_OPENCRQ_SYNC") != "1":
        return False
    from app.services import organization_context, organization_context_opencrq

    agreed = [False]

    async def run_sync(db, *, user_id, apply, tenant_id="", min_criticality=""):
        profile = organization_context.parse_profile({"primary_assets": LAB_OPENCRQ_JEWELS})
        if not apply and agreed[0]:
            agreed[0] = False
            return organization_context_opencrq.SyncPlan(profile=profile), []
        agreed[0] = bool(apply)
        threshold = min_criticality or organization_context_opencrq.DEFAULT_MIN_CRITICALITY
        assets = [dict(a) for a in LAB_OPENCRQ_ASSETS]
        return organization_context_opencrq.plan_sync(profile, assets, threshold), []

    organization_context_opencrq.run_sync = run_sync
    return True


stub_opencrq_sync(dict(os.environ))


#: The Draft with AI route, and the description prefix ``lab_asgi`` refuses with the quota's 429.
LAB_ORG_AI_DRAFT_PATH = "/api/v1/organization-context/ai-draft"
LAB_ORG_AI_DRAFT_QUOTA = "lab:quota"
#: ``app.services.agent_chat.QUOTA_EXCEEDED_MESSAGE``, copied: importing it here would load the chat service.
LAB_QUOTA_MESSAGE = (
    "You have reached your agentic execution quota for the current period. "
    "Please contact your administrator or wait for the quota to reset."
)


def asks_lab_quota(body: bytes) -> bool:
    """Whether the multipart *body* of a Draft with AI request carries a description starting ``lab:quota``."""
    marker = b'name="description"'
    at = body.find(marker)
    if at < 0:
        return False
    start = body.find(b"\r\n\r\n", at)
    return start >= 0 and body[start + 4 :].lstrip().startswith(LAB_ORG_AI_DRAFT_QUOTA.encode())


async def _read_body(receive) -> tuple[bytes, list[dict]]:
    """The whole request body and the messages it came in, to replay them to the application."""
    messages: list[dict] = []
    body = b""
    while True:
        message = await receive()
        messages.append(message)
        if message.get("type") != "http.request":
            break
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    return body, messages


#: The Nova engine status route ``lab_asgi`` answers as available under ``LAB_NOVA_ENGINE=1``.
LAB_NOVA_ENGINE_STATUS_PATH = "/api/v1/nova-rules/engine-status"

#: The embedding model the lab's status names when the route has none (the lab has no platform embedding
#: model): not a calibrated model, so the rules tab shows its uncalibrated semantics line.
LAB_NOVA_SEMANTIC_MODEL = "lab-embedding-model"

#: The Nova semantic override the lab's status reports as not used (``LAB_NOVA_OVERRIDE_IGNORED=1``).
LAB_NOVA_OVERRIDE_IGNORED = "lab-nova-override-model"


def nova_engine_available(body: bytes, override_ignored: bool = False) -> bytes:
    """The engine status JSON *body* with ``engine_available`` true and every tier; *body* when it is not that JSON.

    A ``semantic_calibration`` with no ``configured_model`` (no platform embedding model in the lab) names
    :data:`LAB_NOVA_SEMANTIC_MODEL`, uncalibrated; with *override_ignored* an empty ``override_ignored`` names
    :data:`LAB_NOVA_OVERRIDE_IGNORED`. A model the route reports is kept as is.
    """
    try:
        status = json.loads(body)
    except ValueError:
        return body
    if not isinstance(status, dict) or "engine_available" not in status:
        return body
    calibration = status.get("semantic_calibration")
    if isinstance(calibration, dict):
        calibration = dict(calibration)
        if not calibration.get("configured_model"):
            calibration["configured_model"] = LAB_NOVA_SEMANTIC_MODEL
            calibration["calibrated"] = False
        if override_ignored and not calibration.get("override_ignored"):
            calibration["override_ignored"] = LAB_NOVA_OVERRIDE_IGNORED
        status = {**status, "semantic_calibration": calibration}
    return json.dumps({**status, "engine_available": True, "tiers": ["keywords", "semantics", "llm"]}).encode()


def asks_nova_engine_status(scope: dict, environ: dict) -> bool:
    """Whether *scope* is the engine status read the lab answers as available (``LAB_NOVA_ENGINE=1``)."""
    return (
        environ.get("LAB_NOVA_ENGINE") == "1"
        and scope.get("type") == "http"
        and scope.get("method") == "GET"
        and scope.get("path") == LAB_NOVA_ENGINE_STATUS_PATH
    )


async def answer_nova_engine_available(scope, receive, send) -> None:
    """Run the route, then send its 200 JSON answer with the engine available (any other answer as is)."""
    start: dict = {}
    chunks: list[bytes] = []

    async def capture(message):
        if message["type"] == "http.response.start":
            start.update(message)
            return
        if message["type"] != "http.response.body":
            await send(message)
            return
        chunks.append(message.get("body", b""))
        if message.get("more_body"):
            return
        body = b"".join(chunks)
        if start.get("status") == 200:
            body = nova_engine_available(body, os.environ.get("LAB_NOVA_OVERRIDE_IGNORED") == "1")
        headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
        headers.append((b"content-length", str(len(body)).encode()))
        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    await backend(scope, receive, capture)


#: The route whose ``build_id`` ``LAB_STALE_BUILD_SERVER_ID`` replaces (#63).
AUTH_CONFIG_PATH = "/api/v1/auth/config"


def with_build_id(body: bytes, build_id: str) -> bytes:
    """The ``/auth/config`` JSON *body* with ``build_id`` set to *build_id*; *body* when it is not that JSON."""
    try:
        config = json.loads(body)
    except ValueError:
        return body
    if not isinstance(config, dict):
        return body
    return json.dumps({**config, "build_id": build_id}).encode()


def asks_stale_build(scope: dict, environ: dict) -> str:
    """The server build to report when *scope* reads ``/auth/config`` in a ``LAB_STALE_BUILD=on`` lab, else ``""``."""
    build_id = environ.get("LAB_STALE_BUILD_SERVER_ID", "").strip()
    reads_config = scope.get("type") == "http" and scope.get("method") == "GET" and scope.get("path") == AUTH_CONFIG_PATH
    return build_id if build_id and reads_config else ""


async def answer_stale_build(scope, receive, send, build_id: str) -> None:
    """Run the route, then send its 200 JSON answer with ``build_id`` replaced (any other answer as is)."""
    start: dict = {}
    chunks: list[bytes] = []

    async def capture(message):
        if message["type"] == "http.response.start":
            start.update(message)
            return
        if message["type"] != "http.response.body":
            await send(message)
            return
        chunks.append(message.get("body", b""))
        if message.get("more_body"):
            return
        body = b"".join(chunks)
        if start.get("status") == 200:
            body = with_build_id(body, build_id)
        headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
        headers.append((b"content-length", str(len(body)).encode()))
        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    await backend(scope, receive, capture)


#: The prefix the Office add-in task pane is served under (``vite.config.ts`` base of the hosted build).
OFFICE_ADDIN_PREFIX = "/office"
_office_addin_files: dict[str, object] = {}


def office_addin_scope(scope: dict, dist: str) -> dict | None:
    """*scope* with its path under ``/office`` when the lab's built task pane answers it, else ``None``."""
    if not dist or scope.get("type") != "http" or scope.get("method") not in ("GET", "HEAD"):
        return None
    path = scope.get("path") or ""
    if path != OFFICE_ADDIN_PREFIX and not path.startswith(OFFICE_ADDIN_PREFIX + "/"):
        return None
    if not (Path(dist) / "index.html").is_file():
        return None
    return {**scope, "path": path[len(OFFICE_ADDIN_PREFIX):] or "/"}


def office_addin_files(dist: str):
    """The static files of the built task pane (``index.html`` for a directory), created once."""
    if dist not in _office_addin_files:
        from starlette.staticfiles import StaticFiles

        _office_addin_files[dist] = StaticFiles(directory=dist, html=True, check_dir=False)
    return _office_addin_files[dist]


#: The path the lab's Vite server proxies to the lab's own OpenCTI under ``LAB_CTEM_OPENCTI=on`` (#12058).
CTEM_OPENCTI_PATH = "/opencti"
#: The posture route whose ``products.opencti.url`` names that proxy.
CTEM_POSTURE_PATH = "/api/v1/ctem-metrics/posture"


def preview_origin(scope) -> str | None:
    """The origin the browser loaded the lab from, read from the request's ``Referer``; ``None`` without one."""
    for name, value in scope.get("headers") or []:
        if name.lower() == b"referer":
            parts = urlsplit(value.decode("latin-1"))
            if parts.scheme in ("http", "https") and parts.netloc:
                return f"{parts.scheme}://{parts.netloc}"
    return None


def with_preview_opencti_url(answer: dict, origin: str | None) -> dict:
    """*answer* (a posture) with ``products.opencti.url`` on the preview origin's ``/opencti`` (#12058).

    The lab's OpenCTI registers its own address (``http://localhost:4000/opencti``), which only the pod reaches;
    the Vite server proxies ``/opencti`` to it, so the figures' links open in the reviewer's browser. The
    registration and every figure stay the product's own: only the address the browser follows changes.
    """
    opencti = (answer.get("products") or {}).get("opencti")
    if origin and isinstance(opencti, dict) and opencti.get("url"):
        opencti["url"] = f"{origin}{CTEM_OPENCTI_PATH}"
    return answer


async def answer_ctem_posture_on_preview(scope, receive, send) -> None:
    """The backend's posture answer, ``products.opencti.url`` rewritten by ``with_preview_opencti_url``."""
    start: dict = {}
    chunks: list[bytes] = []

    async def capture(message):
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await backend(scope, receive, capture)
    body = b"".join(chunks)
    if start.get("status") == 200:
        try:
            body = json.dumps(with_preview_opencti_url(json.loads(body), preview_origin(scope))).encode()
        except (ValueError, AttributeError):
            pass
    headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
    headers.append((b"content-length", str(len(body)).encode()))
    await send({"type": "http.response.start", "status": start.get("status", 500), "headers": headers})
    await send({"type": "http.response.body", "body": body})


def asks_ctem_posture_on_preview(scope, environ) -> bool:
    """Whether *scope* is a posture read of a ``LAB_CTEM_OPENCTI=on`` lab."""
    return (
        environ.get("LAB_CTEM_OPENCTI", "").strip().lower() == "on"
        and scope.get("type") == "http"
        and scope.get("method") == "GET"
        and scope.get("path") == CTEM_POSTURE_PATH
    )


async def _send_html(send, page: str) -> None:
    body = page.encode()
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


async def application(scope, receive, send):
    locale = lab_locale.asks_locale(scope)
    if locale is not None:
        await lab_locale.answer(scope, send, locale)
        return
    theme = lab_locale.asks_theme(scope)
    if theme is not None:
        await lab_locale.answer_theme(scope, send, theme)
        return
    whatsapp_page = whatsapp_decision_preview.answer(scope)
    if whatsapp_page is not None:
        await _send_html(send, whatsapp_page)
        return
    if asks_ctem_posture_on_preview(scope, os.environ):
        await answer_ctem_posture_on_preview(scope, receive, send)
        return
    dist = os.environ.get("LAB_OFFICE_ADDIN_DIST", "")
    office = office_addin_scope(scope, dist)
    if office is not None:
        await office_addin_files(dist)(office, receive, send)
        return
    if asks_nova_engine_status(scope, os.environ):
        await answer_nova_engine_available(scope, receive, send)
        return
    stale_build = asks_stale_build(scope, os.environ)
    if stale_build:
        await answer_stale_build(scope, receive, send, stale_build)
        return
    answer, status = lab_answer(scope, os.environ.get("LAB_GITHUB_INTEGRATION_ID", "")), 200
    if (
        answer is None
        and os.environ.get("LAB_ORG_AI_DRAFT") == "1"
        and scope.get("type") == "http"
        and scope.get("method") == "POST"
        and scope.get("path") == LAB_ORG_AI_DRAFT_PATH
    ):
        body, messages = await _read_body(receive)
        if asks_lab_quota(body):
            answer, status = {"detail": LAB_QUOTA_MESSAGE}, 429
        else:
            pending = list(messages)

            async def replay():
                return pending.pop(0) if pending else await receive()

            await backend(scope, replay, send)
            return
    if answer is None:
        await backend(scope, receive, send)
        return
    body = json.dumps(answer).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})

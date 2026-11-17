"""Cleaning up a video's renders and keeping them under a retention: the pure pieces.

The video of the issue: a 4K final a 1080p cut superseded, two ready 1080p
renders (revisions 28 and 31), a failed and a cancelled attempt and twenty
scene previews. ``delete_video_renders`` with its default selector and
``keep_quality: high`` keeps the revision 31 render and deletes the rest; a
render still running, the render the public link plays (without ``confirm``)
and someone else's render on a video shared to watch only are refused by id;
a dry run answers the same list. The retention keeps the newest ready render
of each quality, the current one and those kept by hand, and deletes the
others past their days. The deletion, the files and the routes against
PostgreSQL are ``test_video_studio_render_cleanup_postgres.py``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from app.models.video_studio import JOB_CANCELLED, JOB_FAILED, JOB_READY, JOB_RUNNING, VideoProject, VideoRender
from app.services.video_studio import render_cleanup, render_retention
from app.services.video_studio.render_cleanup import Ask, CleanupError

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
OWNER = uuid.uuid4()


def _render(
    days_ago: float,
    *,
    quality: str = "high",
    revision: int = 31,
    status: str = JOB_READY,
    scene_id: str | None = None,
    **columns,
) -> VideoRender:
    created = NOW - timedelta(days=days_ago)
    base = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "user_id": OWNER,
        "revision": revision,
        "quality": quality,
        "status": status,
        "scene_id": scene_id,
        "keep": False,
        "created_at": created,
        "finished_at": created + timedelta(minutes=5) if status != JOB_RUNNING else None,
        "file_id": uuid.uuid4() if status == JOB_READY else None,
        "width": 1920,
        "height": 1080,
    }
    base.update(columns)
    return VideoRender(**base)


class Film:
    """The video of the issue, its renders newest first by role."""

    def __init__(self) -> None:
        self.project = VideoProject(id=uuid.uuid4(), user_id=OWNER, title="Brand Film")
        self.ultra = _render(10, quality="ultra", revision=26, width=3840, height=2160)
        self.rev28 = _render(5, revision=28)
        self.failed = _render(4, revision=29, status=JOB_FAILED)
        self.cancelled = _render(3, revision=30, status=JOB_CANCELLED)
        self.rev31 = _render(2, revision=31)
        self.previews = [
            _render(1 + i * 0.4, quality="standard", revision=31, scene_id=f"s{i:02d}") for i in range(20)
        ]
        self.renders = [self.ultra, self.rev28, self.failed, self.cancelled, self.rev31, *self.previews]
        for render in self.renders:
            render.project_id = self.project.id
        self.project.latest_render_id = self.rev31.id
        self.sizes = {render.id: 1_000 * (index + 1) for index, render in enumerate(self.renders)}

    def standing(self, *, public_link: bool = False, renders: list[VideoRender] | None = None):
        return render_retention.standing_of(
            self.project, renders or self.renders, public_link=public_link, policy=render_retention.Policy(), now=NOW
        )

    def plan(self, ask: Ask, *, public_link: bool = False, can_manage: bool = True, user_id=OWNER, renders=None):
        rows = renders or self.renders
        return render_cleanup.plan(
            rows,
            ask,
            standing=self.standing(public_link=public_link, renders=rows),
            user_id=user_id,
            can_manage=can_manage,
            sizes=self.sizes,
        )


def _actions(planned) -> dict[uuid.UUID, tuple[str, str | None]]:
    return {item.render.id: (item.action, item.reason) for item in planned.items}


# ── What a cleanup deletes ────────────────────────────────────────────────


def test_the_default_selector_keeps_the_newest_ready_render_of_the_quality_asked_and_deletes_the_rest():
    film = Film()
    planned = film.plan(Ask.parse(keep_quality="high"))
    deleted = {item.render.id for item in planned.deleting}
    assert deleted == {film.ultra.id, film.rev28.id, film.failed.id, film.cancelled.id, *(p.id for p in film.previews)}
    assert _actions(planned)[film.rev31.id] == ("keep", "latest_ready")
    assert planned.bytes == sum(film.sizes[render_id] for render_id in deleted)
    served = planned.serves()
    assert served["current"]["render_id"] == str(film.rev31.id) and served["current_changed"] is False
    assert list(served["latest_ready"]) == ["high"] and served["latest_ready"]["high"]["revision"] == 31


def test_without_keep_quality_the_newest_ready_render_of_every_quality_stays():
    film = Film()
    planned = film.plan(Ask.parse())
    assert {item.render.id for item in planned.items if item.action == "keep"} == {film.ultra.id, film.rev31.id}
    assert set(planned.serves()["latest_ready"]) == {"high", "ultra"}


def test_a_running_render_is_refused_by_id_and_the_rest_go():
    film = Film()
    running = _render(0.01, revision=32, status=JOB_RUNNING, project_id=film.project.id)
    planned = film.plan(Ask.parse(keep_quality="high"), renders=[*film.renders, running])
    assert _actions(planned)[running.id] == ("refuse", "in_progress")
    assert len(planned.deleting) == 24
    text = render_cleanup.answer_text(render_cleanup.Outcome(film.project.id, "Brand Film", Ask(keep_quality="high"), planned))
    assert f"Refused render `{running.id}`" in text and f"cancel_video_job(render_ids=['{running.id}'])" in text


def test_the_render_the_public_link_plays_goes_only_with_confirm_and_the_link_then_plays_the_newest_1080p():
    film = Film()
    film.project.latest_render_id = film.ultra.id
    refused = film.plan(Ask.parse(keep_quality="high"), public_link=True)
    assert _actions(refused)[film.ultra.id] == ("refuse", "public_link")
    assert refused.current_after is film.ultra and not refused.current_moves
    text = render_cleanup.answer_text(render_cleanup.Outcome(film.project.id, "Brand Film", Ask(), refused))
    assert f"the link then plays render `{film.rev31.id}` (high rev 31)" in text

    confirmed = film.plan(Ask.parse(keep_quality="high", confirm=True), public_link=True)
    assert _actions(confirmed)[film.ultra.id] == ("delete", None)
    assert confirmed.current_moves and confirmed.current_after is film.rev31
    assert confirmed.confirmed == [render_cleanup.CONFIRMED_PUBLIC_LINK]
    outcome = render_cleanup.Outcome(film.project.id, "Brand Film", Ask(confirm=True), confirmed)
    text = render_cleanup.answer_text(outcome)
    assert f"The video plays render `{film.rev31.id}` (high rev 31) (it was moved from the render deleted) - and so does its public link." in text
    assert render_cleanup.audit_details(outcome)["confirmed_for"] == ["public_link"]


def test_a_dry_run_answers_the_same_list_and_says_nothing_was_deleted():
    film = Film()
    dry = film.plan(Ask.parse(keep_quality="high", dry_run=True))
    real = film.plan(Ask.parse(keep_quality="high"))
    dry_answer = render_cleanup.Outcome(film.project.id, "Brand Film", Ask(keep_quality="high", dry_run=True), dry)
    real_answer = render_cleanup.Outcome(film.project.id, "Brand Film", Ask(keep_quality="high"), real)
    assert dry_answer.as_dict()["renders"] == real_answer.as_dict()["renders"]
    assert dry_answer.as_dict()["would_free"] == {"renders": 24, "bytes": dry.bytes}
    assert render_cleanup.answer_text(dry_answer).startswith("Dry run - nothing was deleted: 24 render(s)")


def test_on_a_video_shared_to_watch_only_only_the_callers_own_renders_go():
    film = Film()
    viewer = uuid.uuid4()
    film.failed.user_id = viewer
    planned = film.plan(Ask.parse(select="failed_and_cancelled"), can_manage=False, user_id=viewer)
    assert _actions(planned) == {film.failed.id: ("delete", None), film.cancelled.id: ("refuse", "not_yours")}


def test_a_render_kept_by_hand_stays_under_a_selector_and_goes_when_named():
    film = Film()
    film.rev28.keep = True
    assert _actions(film.plan(Ask.parse(keep_quality="high")))[film.rev28.id] == ("keep", "kept_by_hand")
    assert _actions(film.plan(Ask.parse(render_ids=[str(film.rev28.id)])))[film.rev28.id] == ("delete", None)


def test_select_all_with_confirm_takes_the_newest_ready_and_kept_renders_too():
    film = Film()
    film.rev28.keep = True
    planned = film.plan(Ask.parse(select="all", confirm=True))
    assert len(planned.deleting) == len(film.renders)
    assert set(planned.confirmed) == {render_cleanup.CONFIRMED_LATEST_READY, render_cleanup.CONFIRMED_KEPT}
    assert planned.current_after is None and planned.current_moves
    text = render_cleanup.answer_text(render_cleanup.Outcome(film.project.id, "Brand Film", Ask(select="all", confirm=True), planned))
    assert "plays nothing now" in text


def test_a_cancelled_render_stays_while_the_render_that_replaced_it_takes_its_frames():
    film = Film()
    running = _render(0.01, revision=32, status=JOB_RUNNING, project_id=film.project.id)
    film.cancelled.superseded_by_id = running.id
    planned = film.plan(Ask.parse(select="failed_and_cancelled"), renders=[*film.renders, running])
    assert _actions(planned)[film.cancelled.id] == ("keep", "handing_over")


def test_before_narrows_the_scope_and_unknown_ids_are_named():
    film = Film()
    planned = film.plan(Ask.parse(select="failed_and_cancelled", before=(NOW - timedelta(days=3, hours=12)).isoformat()))
    assert _actions(planned) == {film.failed.id: ("delete", None)}
    stranger = str(uuid.uuid4())
    named = film.plan(Ask.parse(render_ids=json.dumps([str(film.failed.id), stranger])))
    assert named.unknown == [stranger] and [item.render.id for item in named.items] == [film.failed.id]


def test_a_quality_the_video_has_no_ready_render_at_is_refused_before_anything_goes():
    with pytest.raises(CleanupError, match="no ready render at standard to keep"):
        Film().plan(Ask.parse(keep_quality="standard"))


@pytest.mark.parametrize(
    ("arguments", "said"),
    [
        ({"render_ids": ["a"], "select": "previews"}, "not both"),
        ({"select": "all"}, "only with confirm"),
        ({"select": "everything"}, "select is one of"),
        ({"keep_quality": "8k"}, "keep_quality is one of"),
        ({"select": "previews", "keep_quality": "high"}, "goes with select all_but_latest_ready"),
        ({"before": "last week"}, "ISO 8601"),
        ({"render_ids": []}, "names no render"),
    ],
    ids=["ids-and-select", "all-unconfirmed", "unknown-selector", "unknown-quality", "quality-without-default", "bad-before", "no-ids"],
)
def test_an_ask_that_cannot_be_planned_says_why(arguments, said):
    with pytest.raises(CleanupError, match=said):
        Ask.parse(**arguments)


def test_an_ask_reads_strings_as_the_model_sends_them():
    ask = Ask.parse(render_ids="a, b", dry_run="true", confirm="false", before="2026-10-20")
    assert ask.render_ids == ("a", "b") and ask.dry_run and not ask.confirm and ask.selector is None
    assert ask.before == datetime(2026, 10, 20, tzinfo=UTC)
    assert Ask.parse().selector == "all_but_latest_ready"


# ── The retention ─────────────────────────────────────────────────────────


def test_the_policy_reads_what_an_administrator_stored_bounded():
    assert render_retention.Policy.of(None).as_dict() == {
        "failed_cancelled_days": 1,
        "previews_days": 7,
        "superseded_ready_days": 30,
    }
    stored = {"previews_days": 0, "superseded_ready_days": 99_999, "failed_cancelled_days": True, "extra": 3}
    assert render_retention.Policy.of(stored).as_dict() == {
        "failed_cancelled_days": 1,
        "previews_days": 0,
        "superseded_ready_days": render_retention.MAX_DAYS,
    }
    assert render_retention.Policy.of({"previews_days": -4, "failed_cancelled_days": "2"}).previews_days == 0


def test_renders_dated_2_8_and_31_days_ago_are_kept_exactly_until_their_days():
    project = VideoProject(id=uuid.uuid4(), latest_render_id=None)
    latest = _render(31, quality="ultra")
    newest_high = _render(2)
    renders = {
        "failed_2": _render(2, status=JOB_FAILED),
        "cancelled_8": _render(8, status=JOB_CANCELLED),
        "preview_2": _render(2, scene_id="s1", quality="standard"),
        "preview_8": _render(8, scene_id="s1", quality="standard"),
        "high_8": _render(8),
        "high_31": _render(31),
        "kept_31": _render(31, keep=True),
    }
    rows = [latest, newest_high, *renders.values()]
    held = render_retention.standing_of(project, rows, public_link=False, policy=render_retention.Policy(), now=NOW)
    kept = {name: render_retention.kept_by(render, held) for name, render in renders.items()}
    assert kept == {
        "failed_2": None,
        "cancelled_8": None,
        "preview_2": "retention",
        "preview_8": None,
        "high_8": "retention",
        "high_31": None,
        "kept_31": "author",
    }
    assert render_retention.kept_by(latest, held) == render_retention.KEPT_LATEST_READY
    assert render_retention.expires_at(renders["high_8"], held) == renders["high_8"].finished_at + timedelta(days=30)
    # Every rule at 0 keeps every render for good.
    forever = render_retention.standing_of(
        project, rows, public_link=False, policy=render_retention.Policy(0, 0, 0), now=NOW
    )
    assert all(render_retention.kept_by(render, forever) is not None for render in rows)
    assert render_retention._due_videos(render_retention.Policy(0, 0, 0), NOW) is None


def test_the_sweep_query_leaves_out_the_newest_ready_render_of_each_quality_the_current_and_the_kept_ones():
    sql = str(render_retention._due_videos(render_retention.Policy(), NOW).compile(dialect=postgresql.dialect()))
    assert "PARTITION BY video_renders.project_id, video_renders.quality" in sql
    assert "video_renders.keep IS false" in sql
    assert "video_projects.latest_render_id IS NULL OR video_projects.latest_render_id != video_renders.id" in sql


class _Redis:
    def __init__(self, swept: bool = False, broken: bool = False) -> None:
        self.keys = {"video_studio:render_retention:swept"} if swept else set()
        self.broken = broken

    async def exists(self, key):
        if self.broken:
            raise ConnectionError("no redis")
        return key in self.keys

    async def set(self, key, _value, ex=None):
        assert ex and ex > 3600
        self.keys.add(key)


@pytest.mark.parametrize(
    ("hour", "redis", "complete", "runs", "marked"),
    [
        (14, _Redis(), True, False, False),
        (2, _Redis(swept=True), True, False, True),
        (2, _Redis(), True, True, True),
        (2, _Redis(), False, True, False),
        (2, _Redis(broken=True), True, True, False),
    ],
    ids=["day", "night-already-swept", "night", "night-incomplete", "redis-down"],
)
def test_the_sweep_runs_at_night_until_one_finds_nothing_left(monkeypatch, hour, redis, complete, runs, marked):
    from app.core import redis as redis_module

    ran: list[datetime] = []

    async def sweep(_db, *, now):
        ran.append(now)
        return render_retention.Swept([], complete=complete)

    async def client():
        return redis

    monkeypatch.setattr(render_retention, "sweep", sweep)
    monkeypatch.setattr(redis_module, "redis_client", client)
    swept = asyncio.run(render_retention.sweep_if_due(object(), now=NOW.replace(hour=hour)))
    assert bool(ran) is runs and (swept is not None) is runs
    assert ("video_studio:render_retention:swept" in redis.keys) is marked


# ── The tool and the docs ─────────────────────────────────────────────────


def test_the_tool_is_in_the_video_studio_suite():
    from app.services.builtin_tools.video_studio import build_video_studio_tools

    definitions, executor = build_video_studio_tools(user_id="")
    tool = next(d for d in definitions if d.name == "delete_video_renders")
    properties = tool.input_schema["properties"]
    assert tool.input_schema["required"] == ["video_id"]
    assert properties["select"]["enum"] == list(render_cleanup.SELECTORS)
    assert properties["keep_quality"]["enum"] == ["draft", "standard", "high", "ultra"]
    assert properties["render_ids"]["type"] == "array" and properties["confirm"]["type"] == "boolean"
    assert "delete_video_renders" in executor._handlers


def test_a_bad_ask_is_answered_by_the_tool_without_opening_the_video():
    from app.services.builtin_tools.video_studio import build_video_studio_tools
    from app.tools.base import ToolCall

    _definitions, executor = build_video_studio_tools(user_id=str(uuid.uuid4()))
    result = asyncio.run(
        executor.execute_tool(ToolCall(id="c", name="delete_video_renders", arguments={"video_id": "v", "select": "all"}))
    )
    assert result.is_error and "only with confirm" in result.content and "Nothing was deleted" in result.content


def test_the_render_retention_is_documented_as_a_setting():
    settings_doc = (ROOT / "docs" / "docs" / "administration" / "settings.md").read_text(encoding="utf-8")
    assert "### Render retention" in settings_doc
    configuration = (ROOT / "docs" / "docs" / "deployment" / "configuration.md").read_text(encoding="utf-8")
    assert "| `VIDEO_STUDIO_RENDER_KEEP_LAST` |" not in configuration and "#render-retention" in configuration

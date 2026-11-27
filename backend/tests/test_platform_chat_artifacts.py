"""The decks, reports and videos an answer shows, in the embedded chat.

``services/platform_chat_artifacts`` and its wiring in ``api/v1/platform.py``:

* a client that promises ``supports_artifacts`` keeps the markers and gets one
  descriptor per artifact, in marker order, scoped to the conversation or the
  caller;
* an older client gets the markers as links to the XTM One viewer and never
  sees one streamed;
* the two routes the product proxies forward read an artifact by its own rule
  and download it in the formats its descriptor lists.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api.v1 import platform as platform_api
from app.services import platform_chat_artifacts as artifacts

DECK = uuid.UUID("11111111-1111-4111-8111-111111111111")
REPORT = uuid.UUID("22222222-2222-4222-8222-222222222222")
VIDEO = uuid.UUID("33333333-3333-4333-8333-333333333333")
OTHER = uuid.UUID("44444444-4444-4444-8444-444444444444")


# ── Markers ───────────────────────────────────────────────────────────────


def test_markers_are_read_in_order_each_once_with_the_slide_a_deck_names():
    text = (
        f"Video [[VIDEO:{VIDEO}]] then the deck [[PRESENTATION:{DECK}]], "
        f"its slide [[presentation:{str(DECK).upper()}#/s-4]], the report [[REPORT:{REPORT}]] "
        f"and a file [[FILE:{OTHER}]]"
    )
    refs = artifacts.marker_refs(text)
    assert [(r.kind, r.id, r.slide_ref) for r in refs] == [
        ("video", VIDEO, None),
        ("presentation", DECK, "s-4"),
        ("report", REPORT, None),
    ]
    assert artifacts.marker_refs("no marker here") == []
    assert artifacts.marker_refs(None) == []


def test_the_viewer_url_is_the_web_page_the_channel_links_open(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.frontend_url", "https://xtm.example/")
    assert artifacts.viewer_url("presentation", DECK, "s-2") == f"https://xtm.example/presentations/{DECK}#/s-2"
    assert artifacts.viewer_url("report", REPORT) == f"https://xtm.example/reports/{REPORT}"
    assert artifacts.viewer_url("video", VIDEO) == f"https://xtm.example/videos/{VIDEO}"
    monkeypatch.setattr("app.core.config.settings.frontend_url", "")
    assert artifacts.viewer_url("video", VIDEO) is None


def test_a_deck_downloads_in_every_export_format():
    from app.services.presentation_export import EXPORT_FORMATS

    assert artifacts.download_formats("presentation") == EXPORT_FORMATS
    assert artifacts.download_formats("report") == ("pdf",)
    assert artifacts.download_formats("video") == ("mp4",)
    assert artifacts.download_formats("elsewhere") == ()


# ── The stream an older client receives ──────────────────────────────────


def _stream(chunks: list[str]) -> str:
    stream_filter = artifacts.ArtifactMarkerStreamFilter()
    shown = [stream_filter.feed(chunk) for chunk in chunks]
    shown.append(stream_filter.flush())
    return "".join(shown), shown


def test_a_marker_split_across_chunks_is_never_streamed():
    marker = f"[[PRESENTATION:{DECK}#/s-1]]"
    chunks = ["Here is the deck ", marker[:3], marker[3:20], marker[20:], " enjoy [[FILE:", f"{OTHER}]]"]
    text, shown = _stream(chunks)
    assert text == f"Here is the deck  enjoy [[FILE:{OTHER}]]"
    assert not any("[[PRES" in piece for piece in shown)


def test_brackets_that_are_not_a_marker_are_released():
    text, _ = _stream(["see [the docs](https://x.y) and [[note]]", " [[REP"])
    assert text == "see [the docs](https://x.y) and [[note]] [[REP"
    text, _ = _stream([f"[[VIDEO:{VIDEO}]] done", " [[REPORT:", f"{REPORT}]]"])
    assert text == " done "


# ── Descriptors of an answer ──────────────────────────────────────────────


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _DB:
    """Answers each ``select`` with the rows of the entity it reads."""

    def __init__(self, rows_by_entity: dict):
        self.rows_by_entity = rows_by_entity
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        return _Rows(self.rows_by_entity.get(entity.__name__, []))


@pytest.mark.asyncio
async def test_an_answer_describes_its_artifacts_in_marker_order_within_its_scope(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.frontend_url", "https://xtm.example")
    render = SimpleNamespace(status="ready", progress=100, file_id=uuid.uuid4(), duration_ms=45000)

    async def _renders(_db, projects):
        return {str(p.id): render for p in projects}, {str(p.id): render for p in projects}

    monkeypatch.setattr(artifacts, "_video_renders", _renders)
    db = _DB(
        {
            "Presentation": [SimpleNamespace(id=DECK, title="Weekly threats", slide_count=7)],
            "Report": [SimpleNamespace(id=REPORT, title="Q3 readout", block_count=9)],
            "VideoProject": [
                SimpleNamespace(
                    id=VIDEO, title="Explainer", scene_count=6, estimated_duration_ms=50000, latest_render_id=uuid.uuid4()
                )
            ],
        }
    )
    conversation = uuid.uuid4()
    user_id = uuid.uuid4()
    first = f"[[REPORT:{REPORT}]] and [[PRESENTATION:{DECK}#/s-3]] and one elsewhere [[PRESENTATION:{OTHER}]]"
    second = f"the film [[VIDEO:{VIDEO}]]"

    described = await artifacts.describe_answers(db, [first, second, "nothing"], conversation_id=conversation, user_id=user_id)

    assert described[0] == [
        {
            "kind": "report",
            "id": str(REPORT),
            "title": "Q3 readout",
            "section_count": 9,
            "downloads": ["pdf"],
            "url": f"https://xtm.example/reports/{REPORT}",
        },
        {
            "kind": "presentation",
            "id": str(DECK),
            "title": "Weekly threats",
            "slide_count": 7,
            "downloads": ["pdf", "pptx", "pptx_native"],
            "slide_ref": "s-3",
            "url": f"https://xtm.example/presentations/{DECK}#/s-3",
        },
    ]
    assert described[1] == [
        {
            "kind": "video",
            "id": str(VIDEO),
            "title": "Explainer",
            "scene_count": 6,
            "downloads": ["mp4"],
            "duration_ms": 45000,
            "status": "ready",
            "progress": 100,
            "url": f"https://xtm.example/videos/{VIDEO}",
        }
    ]
    assert described[2] == []
    # One read per kind for every answer, each narrowed to the conversation or the caller.
    assert len(db.statements) == 3
    for statement in db.statements:
        where = str(statement.whereclause)
        assert "conversation_id" in where and "user_id" in where


@pytest.mark.asyncio
async def test_without_a_scope_nothing_is_described():
    db = _DB({"Presentation": [SimpleNamespace(id=DECK, title="x", slide_count=1)]})
    assert await artifacts.describe_answers(db, [f"[[PRESENTATION:{DECK}]]"]) == [[]]
    assert db.statements == []


def test_a_video_still_rendering_says_so_and_offers_no_download():
    project = SimpleNamespace(id=VIDEO, title="Film", scene_count=3, estimated_duration_ms=30000, latest_render_id=None)
    running = SimpleNamespace(status="running", progress=42, file_id=None, duration_ms=None)
    descriptor = artifacts.video_descriptor(project, latest=running, current=None)
    assert descriptor["status"] == "running"
    assert descriptor["progress"] == 42
    assert descriptor["downloads"] == []
    assert descriptor["duration_ms"] == 30000
    never = artifacts.video_descriptor(project, latest=None, current=None)
    assert "status" not in never and never["downloads"] == []


# ── What each client receives ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_older_client_gets_links_through_the_channel_resolvers(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.frontend_url", "https://xtm.example")
    seen = []

    def _resolver(kind):
        async def _resolve(_db, text, frontend_url, *, conversation_id, user_id, language):
            seen.append((kind, frontend_url, conversation_id, user_id, language))
            return text.replace(f"[[{kind}:", f"<{kind}:")

        return _resolve

    for kind in ("PRESENTATION", "REPORT", "VIDEO"):
        monkeypatch.setattr(artifacts, f"resolve_channel_{kind.lower()}_markers", _resolver(kind))
    text = f"[[PRESENTATION:{DECK}]] [[REPORT:{REPORT}]] [[VIDEO:{VIDEO}]]"
    linked = await artifacts.markers_as_links(object(), text, conversation_id="c", user_id="u", language="fr")
    assert "[[" not in linked
    assert [s[0] for s in seen] == ["PRESENTATION", "REPORT", "VIDEO"]
    assert all(s[1:] == ("https://xtm.example", "c", "u", "fr") for s in seen)
    assert await artifacts.markers_as_links(object(), "plain") == "plain"


@pytest.mark.asyncio
async def test_a_restore_describes_or_links_the_answers_by_the_client_promise(monkeypatch):
    async def _describe(_db, texts, *, conversation_id, user_id):
        return [[{"kind": "report", "id": str(REPORT)}] for _ in texts]

    async def _links(_db, text, **_kw):
        return text.replace(f"[[REPORT:{REPORT}]]", "[Q3](https://xtm.example)")

    monkeypatch.setattr(artifacts, "describe_answers", _describe)
    monkeypatch.setattr(artifacts, "markers_as_links", _links)
    user = SimpleNamespace(id=uuid.uuid4(), language="en")

    def _messages():
        return [
            {"role": "user", "content": f"echo [[REPORT:{REPORT}]]"},
            {"role": "assistant", "content": f"Done [[REPORT:{REPORT}]]"},
            {"role": "assistant", "content": "no marker"},
        ]

    supported = _messages()
    await platform_api._present_restored_artifacts(object(), supported, supported=True, conversation_id="c", user=user)
    assert supported[1]["artifacts"] == [{"kind": "report", "id": str(REPORT)}]
    assert supported[1]["content"] == f"Done [[REPORT:{REPORT}]]"
    assert "artifacts" not in supported[0] and "artifacts" not in supported[2]

    older = _messages()
    await platform_api._present_restored_artifacts(object(), older, supported=False, conversation_id="c", user=user)
    assert older[1]["content"] == "Done [Q3](https://xtm.example)"
    assert older[0]["content"] == f"echo [[REPORT:{REPORT}]]", "a user's own words are never rewritten"
    assert "artifacts" not in older[1]


@pytest.mark.asyncio
async def test_the_web_chat_keeps_a_dashboard_s_marker_the_embedded_panels_get_as_a_link(monkeypatch):
    """The XTM One panel draws ``[[DASHBOARD:<id>]]`` as the dashboard; made a link on restore, the card never showed."""
    dashboard = uuid.uuid4()

    async def _describe(_db, texts, *, conversation_id, user_id):
        return [[] for _ in texts]

    async def _dashboard_links(_db, text, **_kw):
        return text.replace(f"[[DASHBOARD:{dashboard}]]", "[Posture](https://xtm.example/dashboards/x)")

    monkeypatch.setattr(artifacts, "describe_answers", _describe)
    monkeypatch.setattr(artifacts, "link_only_markers_as_links", _dashboard_links)
    user = SimpleNamespace(id=uuid.uuid4(), language="en")
    answer = f"Built.\n\n[[DASHBOARD:{dashboard}]]"

    embedded = [{"role": "assistant", "content": answer}]
    await platform_api._present_restored_artifacts(object(), embedded, supported=True, conversation_id="c", user=user)
    assert embedded[0]["content"] == "Built.\n\n[Posture](https://xtm.example/dashboards/x)"

    web = [{"role": "assistant", "content": answer}]
    await platform_api._present_restored_artifacts(
        object(), web, supported=True, conversation_id="c", user=user, keep_dashboards=True
    )
    assert web[0]["content"] == answer


@pytest.mark.asyncio
async def test_a_non_streaming_answer_carries_descriptors_or_links_and_stores_its_markers(monkeypatch):
    answer = f"Here is the readout [[REPORT:{REPORT}]]"
    stored = []

    async def _fake_resolve_platform_agent(_db, _agent_slug):
        return SimpleNamespace(id=uuid.uuid4())

    async def _fake_store_message(_db, _conversation_id, role, content, channel, metadata_json=None):
        stored.append((role, content))
        return SimpleNamespace(id=uuid.uuid4())

    async def _fake_get_history(_db, _conversation_id):
        return []

    async def _fake_check_quota(_db, _user_id):
        return None

    async def _fake_run_agent_chat(_db, _user_id_str, _agent, _history, _content, conversation_id, **kwargs):
        return (answer, None)

    async def _describe(_db, texts, *, conversation_id, user_id):
        return [[{"kind": "report", "id": str(REPORT), "title": "Q3"}] for _ in texts]

    async def _links(_db, text, **_kw):
        return text.replace(f"[[REPORT:{REPORT}]]", "[Q3](https://xtm.example/reports/x)")

    monkeypatch.setattr(platform_api, "_resolve_platform_agent", _fake_resolve_platform_agent)
    monkeypatch.setattr("app.channels.router.store_message", _fake_store_message)
    monkeypatch.setattr("app.channels.router.get_conversation_history", _fake_get_history)
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _fake_check_quota)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _fake_run_agent_chat)
    monkeypatch.setattr(artifacts, "describe_answers", _describe)
    monkeypatch.setattr(artifacts, "markers_as_links", _links)

    async def _no_files(_db, _ctx, _initial, **_kwargs):
        return [], []

    monkeypatch.setattr(platform_api, "_collect_generated_attachments", _no_files)

    class _DB:
        def add(self, obj):
            self.obj = obj

        async def flush(self):
            if getattr(self.obj, "id", None) is None:
                self.obj.id = uuid.uuid4()

        async def commit(self):
            return None

    user = SimpleNamespace(id=uuid.uuid4(), email="user@example.com", language="en")
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})

    async def _send(**flags):
        return await platform_api.send_platform_chat_message(
            platform_api.PlatformChatRequest(content="a readout", agent_slug="agent", stream=False, **flags),
            request=request,
            db=_DB(),  # type: ignore[arg-type]
            user=user,  # type: ignore[arg-type]
        )

    supported = await _send(supports_artifacts=True)
    assert supported["content"] == answer
    assert supported["artifacts"] == [{"kind": "report", "id": str(REPORT), "title": "Q3"}]

    older = await _send()
    assert older["content"] == "Here is the readout [Q3](https://xtm.example/reports/x)"
    assert "artifacts" not in older
    # The stored answer keeps its marker: the web chat renders it.
    assert [content for role, content in stored if role == "assistant"] == [answer, answer]


def test_both_requests_carry_the_promise_off_by_default():
    assert platform_api.PlatformChatRequest(content="x").supports_artifacts is False
    assert platform_api.PlatformChatSessionRequest().supports_artifacts is False


# ── The routes the product proxies forward ────────────────────────────────


def test_the_artifact_routes_are_served():
    routes = {(route.path, tuple(sorted(route.methods))) for route in platform_api.router.routes}
    assert ("/chat/artifacts/{kind}/{artifact_id}", ("GET",)) in {(p.removeprefix("/platform"), m) for p, m in routes}
    assert ("/chat/artifacts/{kind}/{artifact_id}/download", ("POST",)) in {
        (p.removeprefix("/platform"), m) for p, m in routes
    }


@pytest.mark.asyncio
async def test_an_artifact_the_caller_may_not_read_is_a_404(monkeypatch):
    async def _unreadable(_db, _kind, _artifact_id, _user):
        return None

    monkeypatch.setattr(artifacts, "load_readable_artifact", _unreadable)
    with pytest.raises(platform_api.HTTPException) as caught:
        await platform_api.get_platform_chat_artifact("report", str(REPORT), db=object(), user=SimpleNamespace(id=1))
    assert caught.value.status_code == 404
    assert await artifacts.describe_artifact(object(), "report", str(REPORT), SimpleNamespace(id=1)) is None


@pytest.mark.asyncio
async def test_a_download_refuses_a_kind_or_a_format_its_descriptor_does_not_list():
    user = SimpleNamespace(id=uuid.uuid4())
    for kind, fmt, code in (("elsewhere", "pdf", 404), ("report", "pptx", 400), ("video", "pdf", 400)):
        with pytest.raises(artifacts.ArtifactDownloadError) as caught:
            await artifacts.download_response(object(), kind, str(REPORT), fmt, user)
        assert caught.value.status_code == code, (kind, fmt)


@pytest.mark.asyncio
async def test_a_video_downloads_only_its_finished_current_render(monkeypatch):
    project = SimpleNamespace(id=VIDEO, title="Film", latest_render_id=uuid.uuid4())

    async def _readable(_db, _kind, _artifact_id, _user):
        return project

    class _DB:
        def __init__(self, render):
            self.render = render

        async def get(self, _model, _id):
            return self.render

    monkeypatch.setattr(artifacts, "load_readable_artifact", _readable)
    running = SimpleNamespace(status="running", file_id=None, height=None, fps=None)
    with pytest.raises(artifacts.ArtifactDownloadError) as caught:
        await artifacts.download_response(_DB(running), "video", str(VIDEO), "mp4", SimpleNamespace(id=1))
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_a_report_export_counts_against_the_export_budget_and_reads_by_owner(monkeypatch):
    calls = []

    async def _budget(user_id):
        calls.append(("budget", user_id))

    async def _readable(_db, kind, artifact_id, user):
        calls.append(("read", kind, artifact_id))
        return SimpleNamespace(id=REPORT, title="Q3 readout")

    async def _release(_db):
        calls.append(("release",))

    async def _export(*, report_id, user_id, title):
        calls.append(("export", report_id, title))
        return SimpleNamespace(data=b"%PDF", content_type="application/pdf", filename="Q3 readout.pdf")

    monkeypatch.setattr("app.services.presentation_export.enforce_export_rate_limit", _budget)
    monkeypatch.setattr(artifacts, "load_readable_artifact", _readable)
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", _release)
    monkeypatch.setattr("app.services.report_export.export_report", _export)
    user = SimpleNamespace(id=uuid.uuid4())

    response = await artifacts.download_response(object(), "report", str(REPORT), "PDF", user)

    assert response.body == b"%PDF"
    assert response.headers["content-type"].startswith("application/pdf")
    assert "Q3 readout.pdf" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-store"
    assert [c[0] for c in calls] == ["budget", "read", "release", "export"]

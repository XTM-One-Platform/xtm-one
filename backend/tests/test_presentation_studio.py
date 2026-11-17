"""Presentation Studio — a chat bound to the deck it edits.

What these lock in:

1. The binding is resolved PER TURN against the calling user's visibility: an
   unbound conversation, an invisible deck and a deleted deck all yield no
   context — the agent is never told about a deck the user cannot read.
2. The agent's tier is the one the deck TOOL enforces (``agent_deck_permission``,
   no blanket admin bypass), so the prompt's "you may save" never contradicts a
   refusal from ``generate_presentation``.
3. A studio turn always carries the deck tools, on top of whatever the agent or
   the session picker configured — the union never removes a tool.
4. The prompt block names the deck by id, restates the edit-in-place contract,
   uses the kind's own noun (slides vs pages), and points at the slide on the
   user's screen only when the client sent an index that exists.
5. Both chat loops accept the context; the REST + WS turn paths and the
   conversation CRUD carry the binding; the migration is lock-safe and the
   model's FK breaks the presentations <-> conversations cycle.

No DB: the loaders take a capturing fake session, same style as
``test_presentations.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from pathlib import Path

import pytest

from app.services.presentation_studio import (
    STUDIO_TOOL_IDS,
    StudioContext,
    ensure_studio_tools,
    format_studio_prompt,
    load_studio_context,
    parse_focused_index,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"


# ── Fakes ────────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, one=None, rows=None):
        self._one = one
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.statements: list = []

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()


def _slides(count: int) -> list[dict]:
    return [{"layout": "content", "title": f"Slide {i + 1}"} for i in range(count)]


def _deck(owner: uuid.UUID, *, slides: int = 3, aspect_ratio: str = "16:9", **columns):
    from app.models.presentation import Presentation

    return Presentation(
        id=uuid.uuid4(),
        user_id=owner,
        title="Quarterly review",
        theme="filigran",
        slide_count=slides,
        revision=4,
        spec={
            "title": "Quarterly review",
            "theme": "filigran",
            "aspect_ratio": aspect_ratio,
            "slides": _slides(slides),
        },
        **columns,
    )


def _lookup(deck, *, group_ids: list[str] | None = None) -> list[_Result]:
    """Group memberships, then the deck row — what ``load_visible_presentation`` consumes."""
    return [_Result(rows=list(group_ids or [])), _Result(one=deck)]


def _load(session, **kwargs):
    return asyncio.run(load_studio_context(session, **kwargs))


# ── Focused slide ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, count, expected",
    [
        (1, 3, 1),
        (3, 3, 3),
        (0, 3, None),  # 1-based: 0 is not a slide
        (4, 3, None),  # past the end (stale client index after a deletion)
        (-1, 3, None),
        (1, 0, None),  # empty deck has no slide to focus
        (True, 3, None),  # bool is an int in Python — never a slide index
        ("2", 3, None),
        (2.0, 3, None),
        (None, 3, None),
    ],
)
def test_focused_index_is_bounds_checked_against_the_deck(value, count, expected):
    assert parse_focused_index(value, count) == expected


# ── Tool guarantee ───────────────────────────────────────────────────────────


def test_studio_tools_are_added_without_removing_anything():
    resolved = ensure_studio_tools(["web_fetch", "code_interpreter"])
    assert resolved[:2] == ["web_fetch", "code_interpreter"]
    assert set(STUDIO_TOOL_IDS) <= set(resolved)
    assert "generate_presentation" in STUDIO_TOOL_IDS
    assert "template_search" in STUDIO_TOOL_IDS


def test_studio_tools_union_is_idempotent_and_handles_none():
    once = ensure_studio_tools(None)
    assert once == list(STUDIO_TOOL_IDS)
    assert ensure_studio_tools(once) == once
    # An explicit empty picker ("disable every tool") still gets the deck
    # tools: the studio is what makes the agent able to edit the deck.
    assert ensure_studio_tools([]) == list(STUDIO_TOOL_IDS)


# ── Context loading ──────────────────────────────────────────────────────────


def test_no_binding_means_no_context_and_no_query():
    session = _FakeSession()
    assert _load(session, user_id=uuid.uuid4(), presentation_id=None) is None
    assert session.statements == []


def test_an_invisible_deck_yields_no_context():
    # The visibility filter runs in SQL: an invisible deck comes back as no
    # row, exactly like a deleted one.
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=None)])
    assert _load(session, user_id=uuid.uuid4(), presentation_id=uuid.uuid4()) is None


def test_owner_gets_owner_tier_and_the_focused_slide():
    owner = uuid.uuid4()
    deck = _deck(owner)
    session = _FakeSession(results=_lookup(deck))
    ctx = _load(session, user_id=owner, presentation_id=deck.id, focused_index=2)
    assert isinstance(ctx, StudioContext)
    assert ctx.presentation is deck
    assert ctx.permission == "owner"
    assert ctx.focused_index == 2
    # Group memberships + the deck row; no role lookup for one's own deck.
    assert len(session.statements) == 2


def test_stale_focused_slide_is_dropped_not_rejected():
    owner = uuid.uuid4()
    deck = _deck(owner, slides=2)
    session = _FakeSession(results=_lookup(deck))
    ctx = _load(session, user_id=owner, presentation_id=deck.id, focused_index=7)
    assert ctx is not None
    assert ctx.focused_index is None


def test_read_only_grantee_gets_read_only_tier():
    owner, reader = uuid.uuid4(), uuid.uuid4()
    deck = _deck(
        owner,
        shared_user_ids=[str(reader)],
        shared_user_permissions={str(reader): "read_only"},
    )
    session = _FakeSession(results=_lookup(deck))
    ctx = _load(session, user_id=reader, presentation_id=deck.id)
    assert ctx is not None
    assert ctx.permission == "read_only"


def test_config_grant_through_a_group_gets_config_tier():
    owner, editor, group = uuid.uuid4(), uuid.uuid4(), str(uuid.uuid4())
    deck = _deck(
        owner,
        shared_group_ids=[group],
        shared_group_permissions={group: "config"},
    )
    session = _FakeSession(results=_lookup(deck, group_ids=[group]))
    ctx = _load(session, user_id=editor, presentation_id=deck.id)
    assert ctx is not None
    assert ctx.permission == "config"


def test_admin_owns_a_published_deck_but_the_role_is_looked_up_not_assumed():
    owner, admin = uuid.uuid4(), uuid.uuid4()
    deck = _deck(owner, is_company_managed=True)
    # Third result: the ``User.role`` lookup ``agent_deck_permission`` makes
    # for a company-managed deck the caller does not own.
    session = _FakeSession(results=[*_lookup(deck), _Result(one="admin")])
    ctx = _load(session, user_id=admin, presentation_id=deck.id)
    assert ctx is not None
    assert ctx.permission == "owner"
    assert len(session.statements) == 3

    # A plain member of the org: readable, not editable.
    session = _FakeSession(results=[*_lookup(deck), _Result(one="user")])
    ctx = _load(session, user_id=uuid.uuid4(), presentation_id=deck.id)
    assert ctx is not None
    assert ctx.permission == "read_only"


def test_agent_tier_matches_the_tool_loader_on_every_case():
    """The prompt's access note and the tool's refusal share one resolver."""
    import app.services.presentations as presentations

    src = inspect.getsource(presentations.load_writable_presentation)
    assert "agent_deck_permission(" in src
    assert "is_admin=True" not in src
    studio_src = inspect.getsource(load_studio_context)
    assert "agent_deck_permission" in studio_src


# ── Prompt block ─────────────────────────────────────────────────────────────


def _ctx(deck, permission="owner", focused_index=None) -> StudioContext:
    return StudioContext(presentation=deck, permission=permission, focused_index=focused_index)


def test_prompt_names_the_deck_and_restates_edit_in_place():
    deck = _deck(uuid.uuid4(), slides=3)
    prompt = format_studio_prompt(_ctx(deck))
    assert prompt.startswith("\n[Presentation Studio]")
    assert f"presentation_id: {deck.id}" in prompt
    assert f"presentation_id={deck.id}" in prompt
    assert '"Quarterly review"' in prompt
    assert "deck, 3 slides, revision 4" in prompt
    # The fixture stores the retired ``filigran`` theme name: the prompt speaks
    # the scheme it is, never a theme the tool would not accept back.
    assert "theme dark" in prompt
    assert "filigran" not in prompt
    assert "Never create a new presentation" in prompt
    assert "get_presentation" in prompt
    # The outline is the tool's own edit map — one line per slide, 1-based.
    assert "1. [content] Slide 1" in prompt
    assert "3. [content] Slide 3" in prompt
    assert "Current slides:" in prompt


def test_prompt_states_the_access_tier():
    deck = _deck(uuid.uuid4())
    assert "owns this presentation" in format_studio_prompt(_ctx(deck, "owner"))
    assert "can edit this presentation" in format_studio_prompt(_ctx(deck, "config"))
    read_only = format_studio_prompt(_ctx(deck, "read_only"))
    assert "READ-ONLY" in read_only
    assert "refuse to save" in read_only
    assert "duplicate" in read_only


def test_prompt_points_at_the_slide_in_view_only_when_known():
    deck = _deck(uuid.uuid4(), slides=3)
    without = format_studio_prompt(_ctx(deck))
    assert "is looking at" not in without
    with_focus = format_studio_prompt(_ctx(deck, focused_index=2))
    assert 'is looking at slide 2 ("Slide 2")' in with_focus
    assert '"this slide"' in with_focus


def test_prompt_uses_the_page_noun_for_a_portrait_document():
    deck = _deck(uuid.uuid4(), slides=2, aspect_ratio="A4")
    deck.spec["slides"] = [
        {"layout": "content", "title": "Summary"},
        {"layout": "content", "title": "Findings"},
    ]
    prompt = format_studio_prompt(_ctx(deck, focused_index=1))
    assert "page, 2 pages, revision 4" in prompt
    assert "Current pages:" in prompt
    assert 'is looking at page 1 ("Summary")' in prompt
    # The kind's own noun everywhere the block speaks — never "slides" about a page.
    assert "slide" not in prompt.lower()


def test_prompt_handles_an_empty_deck():
    deck = _deck(uuid.uuid4(), slides=0)
    prompt = format_studio_prompt(_ctx(deck))
    assert "0 slides" in prompt
    assert "(empty)" in prompt


# ── Loops + API wiring ───────────────────────────────────────────────────────


def test_both_chat_loops_take_the_studio_context():
    from app.services.agent_chat import run_agent_chat
    from app.services.general_chat import run_general_chat

    for fn in (run_agent_chat, run_general_chat):
        params = inspect.signature(fn).parameters
        assert "presentation_studio" in params, fn.__name__
        assert params["presentation_studio"].default is None
        src = inspect.getsource(fn)
        # Tool guarantee AND prompt block, on both paths - each studio's own
        # (the deck's, or the video's).
        assert "ensure_studio_tools(" in src and "presentation_studio.tool_ids" in src, fn.__name__
        assert "presentation_studio.prompt()" in src, fn.__name__


def test_both_studio_contexts_answer_the_same_two_questions():
    """A deck's and a video's studio context give the loops their tools and their block."""
    from app.services.presentation_studio import STUDIO_TOOL_IDS, StudioContext
    from app.services.video_studio.studio import VideoStudioContext

    deck = _ctx(_deck(uuid.uuid4(), slides=1))
    assert deck.tool_ids == STUDIO_TOOL_IDS
    assert deck.prompt() == format_studio_prompt(deck)
    assert isinstance(deck, StudioContext)
    assert VideoStudioContext.__dataclass_fields__["tool_ids"].default == ("video_studio", "template_search")


def test_studio_prompt_lands_before_the_security_anchor():
    """The block is context, so it sits above the anchor like page_context."""
    from app.services import agent_chat, general_chat

    for module in (agent_chat, general_chat):
        src = inspect.getsource(module)
        assert src.index("presentation_studio.prompt()") < src.index("build_security_policy("), module.__name__


def test_conversation_api_carries_the_binding():
    from app.api.v1 import chat

    assert "presentation_id" in chat.ConversationCreate.model_fields
    assert "presentation_id" in chat.ConversationResponse.model_fields
    assert "presentation_id" in chat.ConversationDetailResponse.model_fields
    assert "studio_slide" in chat.SendMessageRequest.model_fields
    assert "presentation_id" in inspect.signature(chat.list_conversations).parameters

    create_src = inspect.getsource(chat.create_conversation)
    # A deck the caller cannot read is a 404, never a probe.
    assert "load_visible_presentation(" in create_src
    assert "Presentation not found" in create_src

    module_src = inspect.getsource(chat)
    # Both turn paths resolve the binding and hand it to whichever loop runs:
    # the agent, the General Assistant and an @mentioned agent.
    assert module_src.count("load_studio_context(") >= 2
    assert module_src.count("presentation_studio=_studio") == 3
    assert module_src.count("presentation_studio=_turn_studio") == 3
    # The WS payload's slide index feeds the context.
    assert 'data.get("studio_slide")' in module_src
    assert "focused_index=body.studio_slide" in module_src


def test_list_filter_applies_to_both_scopes():
    from app.api.v1 import chat

    src = inspect.getsource(chat.list_conversations)
    assert src.count("*studio_filter,") == 2


# ── Model + migration ────────────────────────────────────────────────────────


def test_conversation_binding_breaks_the_fk_cycle_and_detaches_on_delete():
    from app.models.chat import Conversation

    column = Conversation.__table__.c.presentation_id
    assert column.nullable
    (fk,) = column.foreign_keys
    assert fk.column.table.name == "presentations"
    assert fk.ondelete == "SET NULL"
    # presentations.conversation_id already points the other way: without
    # use_alter, create_all cannot order the two tables.
    assert fk.use_alter is True


def test_migration_is_lock_safe_and_chained_to_the_collaboration_head():
    path = BACKEND_ROOT / "alembic" / "versions" / "k5p3358studio_add_conversation_presentation_id.py"
    src = path.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "j4p3358collab"' in src
    assert "ADD COLUMN IF NOT EXISTS presentation_id UUID" in src
    # Hot table: constraint added NOT VALID, validated separately, index built
    # concurrently — never a full-table lock behind the rolling deploy.
    assert "NOT VALID" in src
    assert "VALIDATE CONSTRAINT" in src
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in src
    assert "autocommit_block()" in src
    assert src.count("terminate_lock_holders(") >= 3
    assert src.count("set_lock_timeout(") >= 3


# ── Frontend tripwires ───────────────────────────────────────────────────────


def _read(rel: str) -> str:
    return (FRONTEND_ROOT / rel).read_text(encoding="utf-8")


def _read_rail() -> str:
    """The Presentation Studio rail: the shared studio rail and the deck's side of it."""
    return _read("src/components/presentation/PresentationStudioRail.tsx") + _read("src/components/studio/StudioRail.tsx")


def test_frontend_sends_the_slide_in_view_and_binds_conversations():
    # Every message says which slide is on the stage, and a conversation the
    # rail starts is bound to the deck by the session POST that creates it.
    rail = _read_rail()
    assert "studio_slide: activeIndex + 1" in rail
    assert "getRequestExtras={getRequestExtras}" in rail
    endpoints = _read("src/components/chat-panel/endpoints.ts")
    assert 'presentation: "presentation_id"' in endpoints
    assert "?${STUDIO_BINDING[studio]}=${encodeURIComponent(subjectId)}" in endpoints
    types = _read("src/types/index.ts")
    assert re.search(r"presentation_id\??: string \| null", types)


def test_frontend_studio_rail_exists_and_the_viewer_mounts_it():
    rail = _read_rail()
    # The rail is a real chat (transcript + composer + attachments) that never
    # steals the deck's keyboard shortcuts.
    for token in ("LazyXtmChatPanel", "useTurnFrames", "DECK_KEYS_OFF_ATTR"):
        assert token in rail, token
    # The deck on the stage refreshes the moment the agent's save is
    # COMMITTED, not when the tool returns and not when the agent finishes
    # talking about it: the deck tools report the save from inside
    # their execution (`tool_progress` / `presentation_saved`, emitted before
    # the post-save layout render) and the shared frame handler invalidates the
    # presentation queries on that frame — top-level and nested under a
    # consult — keyed on the EVENT, not the tool name. The
    # `generate_presentation` / `edit_presentation` / `duplicate_presentation`
    # tool_done invalidation stays as the fallback (`refreshAfterToolDone`,
    # shared by the rail and the chat page), and the comment tools' tool_done frames refresh too, so a
    # note the agent posts or closes shows up in the Comments panel at once
    #, and so does share_presentation, so the Share dialog shows the
    # new audience, and set_presentation_public_link, so it shows the
    # public link the agent turned on or off. A deck delete_presentation
    # removed leaves the library at once.
    chat_api = _read("src/hooks/api/useChatApi.ts")
    assert re.search(
        r'tool === "generate_presentation" \|\|\s*'
        r'tool === "edit_presentation" \|\|\s*'
        r'tool === "duplicate_presentation" \|\|\s*'
        r'tool === "delete_presentation" \|\|\s*'
        r'tool === "add_presentation_comment" \|\|\s*'
        r'tool === "resolve_presentation_comment" \|\|\s*'
        r'tool === "share_presentation" \|\|\s*'
        r'tool === "set_presentation_public_link"\s*\)\s*\{\s*'
        r'qc\.invalidateQueries\(\{ queryKey: \["presentations"\]',
        chat_api,
    )
    frames = _read("src/pages/chat-panel/useTurnFrames.ts")
    assert 'const TOOL_PROGRESS_STATUS = "tool_progress"' in frames
    assert 'const PRESENTATION_SAVED_EVENT = "presentation_saved"' in chat_api
    assert re.search(
        r"frame\.event === PRESENTATION_SAVED_EVENT\)"
        r'\s*\{\s*qc\.invalidateQueries\(\{ queryKey: \["presentations"\]',
        chat_api,
    )
    # Both sites hand the frame on to the consumer's `onToolProgress`:
    # the studio rail follows the agent's pen from the SAME frame that
    # refreshes the deck, so the two can never key on different events.
    assert re.search(
        r"status === TOOL_PROGRESS_STATUS\)\s*\{[^}]*handleToolProgress\(qc, frame, onToolProgress\)",
        frames,
    )
    assert re.search(
        r"inner\?\.status === TOOL_PROGRESS_STATUS\) handleToolProgress\(qc, inner, onToolProgress\)",
        frames,
    )
    assert "hostToolProgressRef.current?.(frame);" in frames
    assert "useTurnFrames(convId, { onToolProgress: subject.onToolProgress })" in rail
    assert "onToolProgress," in rail
    assert "frame.event !== PRESENTATION_SAVED_EVENT" in rail
    assert "focus_slide_id" in rail
    # The two spellings are one contract: the frontend constants mirror the
    # backend's, byte for byte.
    from app.tools import tool_events

    assert tool_events.TOOL_PROGRESS_STATUS == "tool_progress"
    assert tool_events.PRESENTATION_SAVED_EVENT == "presentation_saved"
    viewer = _read("src/pages/PresentationViewer.tsx")
    assert "PresentationStudioRail" in viewer
    assert "PresentationStudioHandle" in viewer
    # The rail can move the stage: a slide link followed in place, the
    # receipt's "Go to slide N", the slide an edit just wrote.
    assert "onSelectSlide={live.goTo}" in viewer
    # `?studio=1` (+ `&c=<conversation>`) is the one URL shape every entry
    # point builds, so a studio link can be pasted and reopened.
    url_helper = _read("src/components/presentation/studio-url.ts")
    assert 'STUDIO_PARAM = "studio"' in url_helper
    assert 'STUDIO_CONVERSATION_PARAM = "c"' in url_helper
    for entry in (
        "src/pages/PresentationViewer.tsx",
        "src/components/presentation/PresentationCard.tsx",
        "src/components/presentation/usePresentationActions.tsx",
    ):
        assert "studio-url" in _read(entry), entry
    # The in-studio chat card collapses to a one-line receipt: the deck on the
    # stage IS the preview, a second copy of it in the rail would be noise.
    card = _read("src/components/presentation/PresentationCard.tsx")
    assert "studioPresentationId" in card
    for bar in ("DeckToolbar.tsx", "PageToolbar.tsx"):
        assert 't_i18n("Open in studio")' in _read(f"src/components/presentation/{bar}"), bar


def test_frontend_studio_is_offered_to_an_editor_only():
    """A read-only reader has no studio: its assistant could save
    nothing, so the viewer draws neither the toolbar verb nor the left-edge
    handle, a ``?studio=1&c=`` link they follow folds away (URL included), and
    the chat card offers ``Open in a new tab`` instead of ``Open in studio``.
    Comments and history stay offered to every reader."""
    viewer = _read("src/pages/PresentationViewer.tsx")
    assert "const studio = !data.can_manage ? null : studioOpen ? (" in viewer
    assert viewer.count('testId="presentation.studio"') == 2
    assert len(re.findall(r"\{data\.can_manage && \(\s*<StudioToggle\s", viewer)) == 2
    assert "const readOnly = !!data && !data.can_manage;" in viewer
    assert re.search(r"if \(!readOnly\) return;\s*setStudioOpen\(false\);\s*setStudioConvId\(null\);", viewer)
    assert 'onToggleComments: () => togglePanel("comments")' in viewer
    assert 'onToggleHistory: () => togglePanel("history")' in viewer
    card = _read("src/components/presentation/PresentationCard.tsx")
    assert "onOpenEditor: data.can_manage ? () => void openStudio() : undefined," in card
    # Only an editor ever sees the rail, so its empty state has one wording.
    rail = _read_rail()
    assert "presentation.can_manage" not in rail


def test_frontend_both_studios_share_one_top_bar():
    """The Presentation Studio and the Video Studio are one product.

    Their bars take the way back, the kind icon, the title type, the studio
    toggle, the share button and the screens shown before the work loads from
    ONE module, so they cannot drift apart again - they had: a library icon
    against a back arrow, a bar darker than the other, an outlined Share."""
    chrome = _read("src/components/studio/StudioChrome.tsx")
    assert "<ArrowLeft" in chrome
    pieces = (
        "<StudioBarLeading",
        "STUDIO_BAR_TITLE",
        "<StudioToggle",
        "<StudioShareButton",
        "<StudioLoading />",
        "<StudioUnavailable",
    )
    for page in ("src/pages/PresentationViewer.tsx", "src/pages/VideoEditor.tsx"):
        src = _read(page)
        for piece in pieces:
            assert piece in src, (page, piece)
        assert "<ArrowLeft" not in src and "<Library" not in src, page
    # The video bar sits on the deck bar's surface and speaks its vocabulary:
    # the primary verb and the studio toggle labelled, every other action an icon.
    # Watching is the primary verb; rendering is the outlined one beside it.
    editor = _read("src/pages/VideoEditor.tsx")
    header = editor[editor.index("<header") : editor.index("</header>")]
    assert "layer-1 bg-elevation-default" in header
    same_verb = r"(?:(?!<ToolbarVerb)[\s\S])*?"
    assert re.search(r'<ToolbarVerb\s+icon=\{Play\}\s+label=\{t_i18n\("Play"\)\}' + same_verb + r"\sprimary\s", header)
    assert re.search(
        r'<ToolbarVerb\s+icon=\{MonitorPlay\}\s+label=\{t_i18n\("Render"\)\}' + same_verb + r"\soutline\s", header
    )
    assert header.count("primary") == 1
    assert "<VideoDownloadMenu video={video} compact />" in header
    assert 'label={t_i18n("More actions")}' in header
    # The rail's header is as tall as the bar beside it, so the borders line up.
    assert '<header className="flex h-12 ' in _read("src/components/studio/StudioRail.tsx")


def test_frontend_studio_picker_offers_only_agents_that_carry_the_deck_tool():
    """The rail lists the agents built for the job, not every chat agent.

    ``ensure_studio_tools`` would hand the deck tool to any agent for the turn,
    but an agent whose persona was written for detection rules or STIX
    harvesting does not become a deck editor by holding it - it ignores the
    tool or declines - so the picker filters on ``generate_presentation`` and
    offers the General Assistant on the same terms (the user's override, else
    the platform's tools). An install with none is told so, not handed a
    conversation that cannot edit.

    The answer lives in ONE hook (``useDeckAgents``) consumed by the rail
    AND the "New presentation" dialog's "Written by" picker, so the two surfaces
    can never disagree on who is offered. That hook is the deck's
    instance of ``useToolAgents``, which the Video Studio shares.
    """
    hook = _read("src/components/presentation/deck-agents.ts")
    assert 'export const DECK_TOOL_ID = "generate_presentation"' in hook
    assert "return useToolAgents(DECK_TOOL_ID);" in hook
    shared = _read("src/hooks/useToolAgents.ts")
    pure = _read("src/lib/tool-agents.ts")
    # The list itself is filtered - not a badge on an unfiltered list. The
    # filter goes through ``carriesJob`` so an agent storing a
    # [DEPRECATED] former id of a merged group still counts.
    assert re.search(
        r"\.filter\(\(a\) => a\.enabled && !a\.disable_chat && "
        r"carriesJob\(a\.builtin_tool_ids, a\.builtin_allowed_tools, job, aliases\)\)",
        pure,
    )
    # The General Assistant follows general_chat's precedence: the user's own
    # override, else the platform configuration - and a General Assistant linked
    # to an agent is that agent (offered on its own merits, not twice).
    assert "carriesJob(user?.ga_builtin_tool_ids ?? gaConfig?.builtin_tool_ids ?? [], null, suite, aliases)" in shared
    assert "!linkedAgentId &&" in shared
    assert "none: toolAgents.length === 0 && !generalAssistantOffered" in shared
    # The rail's picker is the panel's, fed by the web chat's agent list under
    # the same rule (``?studio=``, ``list_web_chat_agents``).
    from app.api.v1 import chat_web_stream

    assert chat_web_stream.STUDIO_PICKER_TOOL["presentation"] == "generate_presentation"
    assert "agents: `${CHAT_PANEL_ENDPOINTS.agents}?studio=${studio}`" in _read("src/components/chat-panel/endpoints.ts")
    # Both surfaces read the hook; neither re-derives the list.
    rail = _read_rail()
    dialog = _read("src/components/presentation/NewPresentationDialog.tsx")
    for surface, source in (("rail", rail), ("dialog", dialog)):
        assert "useDeckAgents()" in source, surface
        assert "builtin_tool_ids" not in source, surface
    # No eligible agent: the row, the picker and the empty state say so, and a
    # new conversation cannot be started (an existing one keeps its agent).
    assert "none: noStudioAgent } = toolAgents" in rail
    assert "toolAgents: deckAgents" in rail
    assert "const startBlocked = !convId && noStudioAgent" in rail
    assert "const readOnly = !chatOn || startBlocked || convUnusable" in rail
    assert 't_i18n("No agent carries the deck tools yet")' in rail
    # The "any agent can edit here" copy is gone with the unfiltered list.
    assert "Any agent can edit the presentation here" not in rail


def test_frontend_studio_rail_is_resizable_up_to_half_the_screen():
    """The rail's width is the reader's to set.

    A ~380px column is right for a short exchange and wrong for what the studio
    produces most of the time - the outline of a deck, a slide-by-slide review.
    The divider on the rail's right edge drags it out to half the screen; the
    choice is remembered in the browser (a reading preference, never part of
    the shared ``?studio=1&c=`` URL); the divider is a WAI-ARIA window
    splitter, so the keyboard drives it too, and it lives INSIDE the rail so
    its arrows never flip slides.
    """
    rail = _read_rail()
    # The width comes from the shared hook, not from a fixed class.
    assert "useResizableWidth" in rail
    assert "w-[380px]" not in rail
    assert "2xl:w-[440px]" not in rail
    assert "style={{ width: rail.width }}" in rail
    # Bounds: usable at the narrowest, half the screen at the widest.
    assert "const STUDIO_RAIL_MIN_WIDTH = 320" in rail
    assert "const STUDIO_RAIL_MAX_FRACTION = 0.5" in rail
    assert "Math.floor(viewport * STUDIO_RAIL_MAX_FRACTION)" in rail
    # Remembered across decks and reloads, in the browser.
    assert 'const STUDIO_RAIL_WIDTH_KEY = "xtm-studio-rail-width"' in rail
    # A docked-right rail (the fleet page) keeps its own width under a
    # suffixed key; the studios share the plain one.
    assert "storageKey: docksRight ? `${STUDIO_RAIL_WIDTH_KEY}-right` : STUDIO_RAIL_WIDTH_KEY" in rail
    # The divider: labelled, testable, inside the `DECK_KEYS_OFF_ATTR` aside,
    # and left out when the viewport leaves no room between the bounds.
    assert "{...rail.handleProps}" in rail
    assert 't_i18n("Resize the studio")' in rail
    assert "data-testid={`${subject.testId}.resize`}" in rail
    assert 'testId: "presentation.studio"' in rail
    assert "railAttrs: DECK_KEYS_OFF_ATTR" in rail
    assert rail.index("{...subject.railAttrs}") < rail.index("{...rail.handleProps}")
    assert "{rail.canResize && (" in rail

    hook = _read("src/hooks/useResizableWidth.ts")
    # The hook says whether there is room to resize; the callers do not re-derive
    # it from the bounds (the rail and the side panel spelled `max > min` twice).
    assert "canResize: max > min" in hook
    # A WAI-ARIA window splitter: role, orientation, value range, focusable,
    # arrows / Home / End on the keyboard, double-click to reset.
    for token in (
        'role: "separator"',
        '"aria-orientation": "vertical"',
        '"aria-valuemin": min',
        '"aria-valuemax": max',
        '"aria-valuenow": width',
        "tabIndex: 0",
        'case "ArrowRight":',
        'case "ArrowLeft":',
        'case "Home":',
        'case "End":',
        "onDoubleClick: reset",
    ):
        assert token in hook, token
    # The same splitter resizes a HEIGHT (the video editor's timeline):
    # a horizontal separator, Up / Down on the keyboard, the row cursor.
    for token in (
        'axis?: "x" | "y";',
        '"aria-orientation": axis === "y" ? "horizontal" : "vertical"',
        'case "ArrowDown":',
        'case "ArrowUp":',
        'axis === "y" ? "row-resize" : "col-resize"',
        "axis === \"y\" ? event.clientY : event.clientX",
    ):
        assert token in hook, token
    # The drag is captured on the handle (it keeps following the pointer over
    # the stage), the press focuses the handle so the arrows can carry on, and
    # it does NOT prevent the pointerdown's default: that would also cancel the
    # tooltip trigger's own close-on-press and leave the tip up for the drag.
    assert "setPointerCapture" in hook
    assert "releasePointerCapture" in hook
    assert "target.focus({ preventScroll: true })" in hook
    assert "event.preventDefault()" not in hook.split("const onPointerDown")[1].split("const onPointerMove")[0]
    # A live drag only updates the state; storage is written ONCE, when the
    # drag ends, and only when the drag moved the width. `Escape` mid-drag
    # restores the preference from before the drag (`null` included).
    assert "writeStored" not in hook.split("const onPointerMove")[1].split("// While a drag is on")[0]
    assert "if (active.moved) writeStored(storageKey, active.width)" in hook
    assert "setPreferred(active.startPreferred)" in hook
    assert 'event.key !== "Escape"' in hook
    # A keyboard command (or `setWidth`) that lands where the rail already is
    # - `End` at the widest, an arrow against a bound - is not a choice and
    # must not pin the clamped width over a wider preference the viewport is
    # hiding (900px stored, 600px rendered: `ArrowRight` keeps the 900px).
    assert "if (next === width) return;" in hook.split("const commit")[1].split("const reset")[0]
    # The release lands on the window when the handle could not capture the
    # pointer, and never comes at all when the window loses focus mid-drag:
    # neither may leave a drag running with a resize cursor on the body.
    assert 'window.addEventListener("pointerup", onPointerEnd)' in hook
    assert 'window.addEventListener("pointercancel", onPointerEnd)' in hook
    assert 'window.addEventListener("blur", endDrag)' in hook
    # The PREFERENCE is stored, the DISPLAYED width is the preference clamped
    # to the viewport: a width chosen on a large screen survives a laptop. With
    # no preference the default is resolved at render, so a default keyed on a
    # breakpoint keeps following the viewport like the class it replaced.
    assert "const width = clamp(preferred ?? resolveDefault())" in hook
    assert "useState<number | null>(() => readStored(storageKey))" in hook
    assert 'window.addEventListener("resize", onResize)' in hook


def test_frontend_side_panel_is_one_resizable_slot_up_to_a_third_of_the_screen():
    """The viewer's right-hand slot - comments, editor, history, layout check -
    resizes too, through the same hook, from its LEFT edge, up to a
    third of the screen: a review note is read next to the slide it is about,
    so the deck keeps the larger share. One wrapper owns the width; the four
    panels fill it instead of each carrying a fixed 400px, and a new panel that
    mounts outside the slot (or brings its own width back) fails here."""
    panel = _read("src/components/presentation/PresentationSidePanel.tsx")
    assert "useResizableWidth" in panel
    assert 'edge: "start"' in panel  # the handle sits on the left edge, so the drag direction is inverted
    assert "const SIDE_PANEL_MIN_WIDTH = 320" in panel
    assert "const SIDE_PANEL_MAX_FRACTION = 1 / 3" in panel
    assert "Math.floor(viewport * SIDE_PANEL_MAX_FRACTION)" in panel
    assert 'const SIDE_PANEL_WIDTH_KEY = "xtm-presentation-panel-width"' in panel
    assert "style={{ width: panel.width }}" in panel
    assert "{...panel.handleProps}" in panel
    assert 't_i18n("Resize the side panel")' in panel
    assert 'data-testid="presentation.panel.resize"' in panel
    # Arrows on the handle resize the panel, never flip slides.
    assert panel.index("{...DECK_KEYS_OFF_ATTR}") < panel.index("{...panel.handleProps}")
    # Left out when the viewport leaves no room between the bounds, as the rail is.
    assert "{panel.canResize && (" in panel

    viewer = _read("src/pages/PresentationViewer.tsx")
    panels = ("PresentationCommentsPanel", "PresentationEditorPanel", "PresentationHistoryPanel", "PresentationLayoutPanel")
    # Every panel the viewer mounts sits INSIDE the slot: nothing between the
    # slot's opening tag and its closing tag is a panel rendered elsewhere.
    slot = viewer[viewer.index("<PresentationSidePanel>") : viewer.index("</PresentationSidePanel>")]
    for name in panels:
        assert f"<{name}" in slot, name
        assert viewer.count(f"<{name}") == 1, name
        source = _read(f"src/components/presentation/{name}.tsx")
        assert "w-[400px]" not in source, name
        assert "w-full min-w-0" in source, name

    hook = _read("src/hooks/useResizableWidth.ts")
    assert 'edge?: "start" | "end"' in hook
    assert 'const direction = edge === "end" ? 1 : -1;' in hook


def test_frontend_studio_rail_transcript_and_composer_are_the_shared_panel():
    """The rail's transcript and composer are the shared chat panel's.

    The rail renders the panel /chat renders (its own chunk, never prefetched
    with the viewer, #3927), without the conversation list.
    """
    rail = _read("src/components/studio/StudioRail.tsx")
    panel = rail[rail.index("<LazyXtmChatPanel") :]
    panel = panel[: panel.index("/>,")]
    for prop in ("hideConversationList", "steerAttachments", "holdSendsDuringTurn", "apiEndpoints={endpoints}"):
        assert prop in panel, prop
    # No second transcript or composer left behind.
    for gone in ("<textarea", "scrollContainerRef", "useChatAttachments", "ChatMessages"):
        assert gone not in rail, gone
    lazy = _read("src/components/chat-panel/LazyXtmChatPanel.tsx")
    assert "{ prefetch: false }" in lazy
    endpoints = _read("src/components/chat-panel/endpoints.ts")
    assert "...CHAT_PANEL_ENDPOINTS," in endpoints
    assert 'upload: "/web/chat/upload",' in endpoints
    # A run's result still draws its stored files and cards outside the panel.
    run_detail = _read("src/pages/RunDetail.tsx")
    assert "<InlineFileContent" in run_detail


def test_frontend_comment_composer_drafts_with_ai_and_the_chat_follows_the_comment_tools():
    """The composer's sparkle is the platform's `PromptGenerator`, fed by a
    deck-scoped draft endpoint: the slide on screen is the anchor, a
    reply drafts against its thread, and the person's own draft is what
    "Improve with AI" improves. The chat surface names the three comment tools
    and refreshes the deck queries when one of them writes."""
    panel = _read("src/components/presentation/PresentationCommentsPanel.tsx")
    assert 'contextType="presentation_comment"' in panel
    assert panel.count("<PromptGenerator") == 2  # the composer AND the reply box
    assert "useDraftPresentationComment" in panel
    assert "parent_id: parentId" in panel
    assert "current_text: currentText.trim() || null" in panel

    generator = _read("src/components/ui/prompt-generator.tsx")
    assert "generate?: (brief: string) => Promise<string>" in generator
    assert '"presentation_comment"' in generator
    # The refine path passes the brief explicitly - no stale closure over the input.
    assert "handleGenerate(brief)" in generator
    assert "setTimeout(handleGenerate" not in generator
    # One generation at a time, decided by a ref and not by `pending` alone:
    # state flips after a render, so Enter and a click in the same tick would
    # both pass a state-only check. Cleared however the request settles.
    assert "if (!text || pending || inFlight.current) return;" in generator
    assert generator.count("inFlight.current = false;") == 2  # the custom path's finally, the mutation's onSettled
    # The custom generator is entered through a settled promise: a callback
    # that throws synchronously must still reach that `finally`, or the latch
    # stays set for the life of the component.
    assert ".then(() => customGenerate(text))" in generator

    hooks = _read("src/hooks/api/usePresentationApi.ts")
    assert "/comments/draft" in hooks

    constants = _read("src/lib/constants.ts")
    for tool in ("list_presentation_comments", "add_presentation_comment", "resolve_presentation_comment"):
        assert f"{tool}:" in constants, tool
    chat_api = _read("src/hooks/api/useChatApi.ts")
    for tool in ("add_presentation_comment", "resolve_presentation_comment"):
        assert tool in chat_api, tool


def test_every_locale_has_the_studio_strings():
    keys = (
        "Edit with my assistant",
        "Studio",
        "Open in studio",
        "Hide the studio",
        "Open the studio - edit with your assistant",
        "No agent carries the deck tools yet",
        "No agent with the deck tools",
        "Resize the studio",
        "Drag to resize the studio - double-click to reset",
        "Resize the side panel",
        "Drag to resize the panel - double-click to reset",
        "Say what the note should convey...",
        "Could not draft the comment",
        "Review notes on slides. Your assistant reads the open ones and closes them when you ask.",
        "Review notes on pages. Your assistant reads the open ones and closes them when you ask.",
    )
    for locale in ("en", "fr", "es", "ja"):
        catalog = _read(f"src/i18n/lang/{locale}.json")
        for key in keys:
            assert f'"{key}"' in catalog, (locale, key)

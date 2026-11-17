"""A repair of a finished take: its source kept while it runs, the frames its page
painted said as such, its MP4 read a frame at a time, its keyframes' off-scale runs dropped, and the tool's answer
(the contact sheet opt-out, the source told). Synthetic rows and frames only."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.core.config import settings
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, VideoTake
from app.services.video_studio import encoder, library, retention, take_repair

SOURCE = "00000000-0000-0000-0000-00000000b001"
OWNER = "00000000-0000-0000-0000-00000000a001"
CONVERSATION = "00000000-0000-0000-0000-0000000000c1"


# ── 1. The take a repair is made from is kept while the repair runs ─────────


class _Db:
    """The captures of the takes still running, then the ready takes past their period; no video uses any."""

    def __init__(self, running: list[dict], due: list[VideoTake]):
        self.running, self.due = running, due

    async def execute(self, statement, *_args, **_kwargs):
        sql = str(statement.compile())
        if "asset_refs IS NULL" in sql:
            return SimpleNamespace(all=lambda: [])
        if "FROM video_projects" in sql:
            return SimpleNamespace(all=lambda: [], scalars=lambda: SimpleNamespace(all=lambda: [], first=lambda: None), scalar_one=lambda: 0)
        if "status NOT IN" in sql:
            rows = list(self.running)
        elif "status IN" in sql:
            rows = []
        else:
            rows = [row for row in self.due if "video_takes" in sql]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows, first=lambda: None))

    async def commit(self):
        return None


def test_the_sweep_keeps_a_take_a_repair_is_made_from(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_media_retention_days", 30)
    monkeypatch.setattr(settings, "video_studio_unused_media_retention_days", 14)
    now = datetime(2026, 12, 1, tzinfo=UTC)
    long_ago = now - timedelta(days=90)
    kept_file, other_file = uuid.uuid4(), uuid.uuid4()
    source = VideoTake(
        id=uuid.UUID(SOURCE), name="Flow", status=JOB_READY, script={}, keep=False,
        created_at=long_ago, last_used_at=long_ago, file_id=kept_file,
    )
    other = VideoTake(
        id=uuid.uuid4(), name="Other", status=JOB_READY, script={}, keep=False,
        created_at=long_ago, last_used_at=long_ago, file_id=other_file,
    )
    removed: list = []

    async def delete_files(files):
        removed.extend(files)

    monkeypatch.setattr("app.services.video_studio.assets.delete_files", delete_files)
    db = _Db([{take_repair.REPAIR_KEY: SOURCE}, {"opened": {}}], [source, other])
    counts = asyncio.run(retention.sweep(db, now=now))
    assert counts["expired"] == 1 and counts["in_use"] == 1
    assert source.file_id == kept_file and source.expired_at is None and source.last_used_at == now
    assert other.file_id is None and kept_file not in removed


def test_deleting_a_take_a_repair_is_made_from_asks_for_force(monkeypatch):
    db = _Db([{take_repair.REPAIR_KEY: SOURCE}], [])
    user = SimpleNamespace(id=uuid.UUID(OWNER))
    refusal = asyncio.run(library.deletion_refusal(db, user, "take", uuid.UUID(SOURCE)))
    assert refusal is not None and refusal["code"] == "media_in_use" and "repair" in refusal["message"]
    assert refusal["force_allowed"] is False  # force=true does not lift it
    # Once the repair ended, nothing holds it.
    assert asyncio.run(library.deletion_refusal(_Db([], []), user, "take", uuid.UUID(SOURCE))) is None


class _BulkDb(_Db):
    """A bulk deletion of named takes: no video uses any, a repair is made from *SOURCE*."""

    def __init__(self, named: list[VideoTake]):
        super().__init__([{take_repair.REPAIR_KEY: SOURCE}], [])
        self.named, self.deleted = named, []

    async def execute(self, statement, *_args, **_kwargs):
        sql = str(statement.compile())
        if sql.split("FROM", 1)[0].strip() == "SELECT video_takes.capture":
            return await super().execute(statement)
        if "count(" in sql:
            return SimpleNamespace(scalar_one=lambda: 0)
        rows = list(self.named) if "FROM video_takes" in sql else []
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows, first=lambda: None))


def test_a_bulk_deletion_keeps_a_take_a_repair_is_made_from(monkeypatch):
    source = VideoTake(id=uuid.UUID(SOURCE), name="Flow", status=JOB_READY, script={}, keep=False)
    other = VideoTake(id=uuid.uuid4(), name="Other", status=JOB_READY, script={}, keep=False)
    deleted: list = []

    async def delete_batch(_db, media, done):
        deleted.extend(row for _kind, row in media)

    monkeypatch.setattr(library, "_delete_batch", delete_batch)
    user = SimpleNamespace(id=uuid.UUID(OWNER))
    done = asyncio.run(
        library.delete_assets(_BulkDb([source, other]), user, items=[("take", source.id), ("take", other.id)])
    )
    assert deleted == [other] and done.kept == 1


def test_a_queued_repair_stamps_its_source_used():
    source = SimpleNamespace(last_used_at=None)
    before = datetime.now(UTC)
    take_repair.hold_source(source)
    assert source.last_used_at >= before


# ── 2. The frames its page painted, not the frames chosen off its MP4 ───────


def test_captured_frames_are_what_the_page_painted():
    # From its MP4: the take repaired's count, never the chosen frames (a hold is one frame there).
    assert take_repair.painted_count(take_repair.FROM_MP4, recorded_frames=5400, kept=12) == 5400
    # A take recorded before it counted any: unknown, as it said, never the chosen frames.
    assert take_repair.painted_count(take_repair.FROM_MP4, recorded_frames=None, kept=12) is None
    assert take_repair.painted_count(take_repair.FROM_MP4, recorded_frames=0, kept=12) is None
    # From its keyframes: what its browser painted, less an off-scale run.
    assert take_repair.painted_count(take_repair.FROM_KEYFRAMES, recorded_frames=5400, kept=300) == 300


# ── 4. Its MP4 read a frame at a time ───────────────────────────────────────


def test_painted_from_mp4_streams_its_thumbnails(monkeypatch, tmp_path: Path):
    still, moved = bytes([100] * 16), bytes([100] * 8 + [180] * 8)
    held: list[int] = []

    async def frames(_path, *, width, height, fps, start=0.0):
        for index, thumb in enumerate([still, still, moved, moved, still]):
            held.append(index)
            yield thumb

    async def extract(_path, picked, *, fps, out_dir):
        return {index: str(Path(out_dir) / f"f{index}.png") for index in picked}

    monkeypatch.setattr(encoder, "iter_gray_frames", frames)
    monkeypatch.setattr(encoder, "extract_frames", extract)
    got = asyncio.run(
        take_repair.painted_from_mp4(tmp_path / "t.mp4", tmp_path, fps=10, size=(160, 90), events=[], capture=None)
    )
    assert held == [0, 1, 2, 3, 4]
    assert [name for _t, name in got] == ["f0.png", "f2.png", "f4.png"]


def test_the_change_detector_holds_one_thumbnail():
    changed = take_repair._Changes()
    assert [changed(t) for t in (bytes([1] * 4), bytes([1] * 4), bytes([200] * 4))] == [True, False, True]
    assert changed.before is not None and changed.before.size == 4


# ── 5. Its keyframes' off-scale runs dropped ────────────────────────────────


def test_keyframes_inside_an_off_scale_run_are_dropped():
    frames = [(0.0, "a"), (0.1, "b"), (0.25, "c"), (0.3, "d"), (0.5, "e")]
    # Half-open [0.2, 0.3): 0.3 is the first valid frame after the run.
    assert take_repair.drop_faults(frames, [(0.2, 0.3)]) == [(0.0, "a"), (0.1, "b"), (0.3, "d"), (0.5, "e")]
    # The first frame always stays, even inside a run.
    assert take_repair.drop_faults(frames, [(0.0, 0.1)])[0] == (0.0, "a")
    assert take_repair.drop_faults(frames, []) == frames


def test_off_scale_bounds_stored_to_the_millisecond_still_catch_their_frames():
    # A run of frames 1 and 2 at 60 fps is [1/60, 3/60): stored as [0.017, 0.05], its end the first valid frame.
    frames = [(round(i / 60, 6), f"f{i}") for i in range(5)]
    run = [(round(1 / 60, 3), round(3 / 60, 3))]
    assert take_repair.drop_faults(frames, run) == [frames[0], frames[3], frames[4]]
    times = [i / 60 for i in range(5)]
    assert take_repair.select_painted(times, [True] * 5, faults=run) == [0, 3, 4]


def test_the_first_valid_frame_after_a_run_ending_on_a_rounded_boundary_is_kept():
    # A run ending at 2/60 = 0.033333 is stored as 0.033: the frame at 0.033333 is valid, in both routes.
    run = [(round(1 / 60, 3), round(2 / 60, 3))]
    frames = [(round(i / 60, 6), f"f{i}") for i in range(4)]
    assert take_repair.drop_faults(frames, run) == [frames[0], frames[2], frames[3]]
    times = [i / 60 for i in range(4)]
    assert take_repair.select_painted(times, [True] * 4, faults=run) == [0, 2, 3]
    assert take_repair.in_fault(1 / 60, run) and not take_repair.in_fault(2 / 60, run)
    # A run ending at 1/30 (0.033) at 30 fps: the frame at 1/30 is kept as well.
    assert not take_repair.in_fault(1 / 30, [(0.0, round(1 / 30, 3))])


# ── 3, 6. The tool: the contact sheet opt-out kept, the source told ─────────


def _tool_world(monkeypatch):
    from app.services.builtin_tools import video_studio as tool
    from app.services.video_studio import jobs, projects

    source = SimpleNamespace(
        id=uuid.UUID(SOURCE), status=JOB_READY, file_id=uuid.uuid4(), profile_id=None, name="Flow", script={},
        last_used_at=None, owed_conversation_id=uuid.UUID(CONVERSATION),
    )
    repaired = SimpleNamespace(
        id=uuid.uuid4(), status=JOB_FAILED, error="synthetic", profile_id=None, events=[], capture={},
        contact_sheet_file_id=uuid.uuid4(),
    )
    seen = SimpleNamespace(told=[], sheets=[], queued=[])

    class _Session:
        async def get(self, _model, _key):
            return source

        def add(self, row):
            seen.queued.append(row)

        async def flush(self):
            seen.queued[-1].id = repaired.id

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Session()

    async def find_user(_db, _user_id):
        return SimpleNamespace(id=uuid.UUID(OWNER), role="user")

    async def can_read(*_args):
        return True

    async def mark_told(kind, rows, conversation_id):
        seen.told.append((kind, [r.id for r in rows], conversation_id))

    async def enqueue(*_args, **_kwargs):
        return None

    async def wake():
        return None

    async def wait_for(*_args, **_kwargs):
        return [repaired]

    async def file_refs(file_ids, **_kwargs):
        seen.sheets.extend(file_ids)
        return tool._Pictures()

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(tool, "_file_refs", file_refs)
    monkeypatch.setattr(projects, "can_read_project_asset", can_read)
    monkeypatch.setattr(jobs, "mark_told", mark_told)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(jobs, "wake", wake)
    monkeypatch.setattr(jobs, "wait_for", wait_for)
    monkeypatch.setattr(jobs, "serialize_take", lambda take, **_k: {"id": str(take.id)})
    return tool, source, repaired, seen


def _repair(tool, **arguments):
    from app.tools.base import ToolCall

    _definitions, executor = tool.build_video_studio_tools(user_id=OWNER, conversation_id=CONVERSATION)
    call = ToolCall(id="r1", name="get_video_recording", arguments={"recording_id": SOURCE, "repair": True, **arguments})
    return asyncio.run(executor.execute_tool(call))


def test_a_repair_tells_its_source_and_keeps_the_sheet_opt_out(monkeypatch):
    tool, source, _repaired, seen = _tool_world(monkeypatch)
    result = _repair(tool, include_contact_sheet=False)
    assert f"Repair of recording `{SOURCE}`" in result.content
    # The source read: no longer owed to the conversation, before the repair is queued; and used now.
    assert seen.told[0] == ("take", [source.id], uuid.UUID(CONVERSATION))
    assert source.last_used_at is not None
    assert seen.queued and seen.queued[0].capture == {take_repair.REPAIR_KEY: SOURCE}
    assert seen.queued[0].status == JOB_PENDING
    # include_contact_sheet=false: no sheet asked for.
    assert seen.sheets == []


def test_a_repair_shows_the_sheet_by_default(monkeypatch):
    tool, _source, repaired, seen = _tool_world(monkeypatch)
    _repair(tool)
    assert seen.sheets == [repaired.contact_sheet_file_id]

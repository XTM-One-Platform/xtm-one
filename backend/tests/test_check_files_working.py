"""Check files: the pictures a tool makes for the agent stay working files.

A QA sweep producing forty contact sheets used to put forty download cards
under the reply, and a background task handed its check pictures back to the
parent's reply as deliverables. A check's picture is now stored as
``agent_check``: shown as a working file (one collapsed line), handed back as a
file to read, deleted after ``AGENT_CHECK_FILE_RETENTION_DAYS``. A frame the
person asked for (``save_video_frame`` with ``deliver: true``) stays a
download file.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.base import AGENT_CHECK_FILE_SOURCE, FileRef, is_working_file_source
from app.tools.base import FileContext


def _ref(name: str, source: str) -> FileRef:
    return FileRef(
        file_id=str(uuid.uuid4()),
        filename=name,
        content_type="image/png",
        size=1_024,
        s3_object_name=f"generated/{name}",
        source=source,
    )


# -- the source ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "working"),
    [
        (AGENT_CHECK_FILE_SOURCE, True),
        ("agent_working", True),
        ("AGENT_CHECK", True),
        ("agent_download", False),
        ("agent_generated", False),
        ("chat", False),
        (None, False),
    ],
)
def test_a_check_file_is_a_working_file_and_a_deliverable_is_not(source, working):
    assert is_working_file_source(source) is working
    assert _ref("a.png", source or "").is_working_file is working


def test_a_check_file_is_still_a_file_a_tool_produced():
    """Read back with a tool like any agent file, never inlined as an upload."""
    assert _ref("sheet.png", AGENT_CHECK_FILE_SOURCE).is_agent_generated


def test_the_chip_of_a_check_file_is_a_working_file_and_an_explicit_download_a_card():
    from app.services.chat_messages import attachment_meta_from_file_attachment

    def meta(source: str) -> dict:
        return attachment_meta_from_file_attachment(
            SimpleNamespace(id=uuid.uuid4(), filename="frame.png", content_type="image/png", size=10, source=source)
        )

    assert meta(AGENT_CHECK_FILE_SOURCE)["file_tag"] == "working_file"
    assert meta("agent_working")["file_tag"] == "working_file"
    assert meta("agent_download")["file_tag"] == "download_file"
    assert meta("chat")["file_tag"] == "download_file"


# -- the hand-back ---------------------------------------------------------------


def test_a_task_hands_its_working_files_back_to_read_and_its_deliverables_to_carry():
    from app.services.sub_agent_files import register_result_refs

    ctx = FileContext()
    sheet = _ref("sweep - scene 1.png", AGENT_CHECK_FILE_SOURCE)
    report = _ref("report.pdf", "agent_download")

    register_result_refs(ctx, [sheet, report])

    # Both readable by id from the parent run.
    assert ctx.get(sheet.file_id) is sheet
    assert ctx.get(report.file_id) is report
    # Only the working file is a file fetched to read: carried only when the reply names it.
    assert ctx.is_fetched(sheet.file_id)
    assert not ctx.is_fetched(report.file_id)


def test_register_result_refs_without_a_context_is_a_no_op():
    from app.services.sub_agent_files import register_result_refs

    register_result_refs(None, [_ref("a.png", AGENT_CHECK_FILE_SOURCE)])


def test_a_completion_notice_shows_the_deliverables_and_only_the_working_files_it_names():
    from app.services.sub_agent_files import deliverable_rows

    sheet = SimpleNamespace(id=uuid.uuid4(), source=AGENT_CHECK_FILE_SOURCE)
    named_sheet = SimpleNamespace(id=uuid.uuid4(), source=AGENT_CHECK_FILE_SOURCE)
    report = SimpleNamespace(id=uuid.uuid4(), source="agent_download")
    text = f"The verdict is in the report; the worst frame is [[FILE:{named_sheet.id}]]."

    assert deliverable_rows([sheet, named_sheet, report], text) == [named_sheet, report]
    assert deliverable_rows([sheet, report], None) == [report]


# -- the retention ---------------------------------------------------------------


def test_the_retention_cutoff_follows_the_setting_and_zero_keeps_check_files():
    from app.services import check_files

    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    with patch("app.core.config.settings.agent_check_file_retention_days", 7):
        assert check_files.cutoff(now=now) == now - timedelta(days=7)
    with patch("app.core.config.settings.agent_check_file_retention_days", 0):
        assert check_files.cutoff(now=now) is None


def test_the_default_retention_is_seven_days():
    from app.core.config import Settings

    assert Settings.model_fields["agent_check_file_retention_days"].default == 7


@pytest.mark.asyncio
async def test_the_sweep_keeps_a_check_file_a_video_uses():
    from app.services import check_files

    old, used = uuid.uuid4(), uuid.uuid4()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [old, used]
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    with (
        patch("app.core.config.settings.agent_check_file_retention_days", 7),
        patch("app.services.video_studio.retention.files_in_use", AsyncMock(return_value={str(used)})),
    ):
        assert await check_files.expired_ids(db) == [str(old)]


@pytest.mark.asyncio
async def test_a_sweep_with_retention_off_reads_nothing():
    from app.services import check_files

    db = SimpleNamespace(execute=AsyncMock())
    with patch("app.core.config.settings.agent_check_file_retention_days", 0):
        assert await check_files.sweep(db) == 0
    db.execute.assert_not_awaited()


# -- the tools -------------------------------------------------------------------


def test_the_video_studio_pictures_are_stored_as_check_files():
    from app.services.builtin_tools import video_studio

    assert video_studio._SHOWN_FILE_SOURCE == AGENT_CHECK_FILE_SOURCE
    # An older conversation copy is still found, not copied again.
    assert "agent_working" in video_studio._SHOWN_FILE_SOURCES


def test_save_video_frame_offers_deliver_for_a_frame_the_person_asked_for():
    from app.services.builtin_tools.video_studio import build_video_studio_tools

    definitions, _executor = build_video_studio_tools(user_id="")
    tool = next(d for d in definitions if d.name == "save_video_frame")
    schema = tool.input_schema["properties"]["deliver"]
    assert schema["type"] == "boolean"
    assert "working file" in tool.description
    assert "deliver" not in tool.input_schema.get("required", [])

from app.channels.base import ChannelType, InboundMessage
from app.channels.router import _extract_channel_id


def _inbound_teams(
    *,
    channel_thread_id: str,
    conversation_type: str | None = None,
    team_id: str | None = None,
    channel_id: str | None = None,
) -> InboundMessage:
    metadata: dict[str, str] = {}
    if conversation_type is not None:
        metadata["conversation_type"] = conversation_type
    if team_id is not None:
        metadata["team_id"] = team_id
    if channel_id is not None:
        metadata["channel_id"] = channel_id
    return InboundMessage(
        channel=ChannelType.TEAMS,
        text="hello",
        channel_thread_id=channel_thread_id,
        metadata=metadata,
    )


def test_extract_channel_id_uses_team_and_channel_metadata_for_teams():
    inbound = _inbound_teams(
        channel_thread_id="19:conversation@thread.tacv2",
        conversation_type="channel",
        team_id="team-123",
        channel_id="channel-456",
    )
    assert _extract_channel_id(inbound) == "team-123:channel-456"


def test_extract_channel_id_keeps_dm_bypass_for_teams_personal():
    inbound = _inbound_teams(
        channel_thread_id="a:personal-conversation",
        conversation_type="personal",
    )
    assert _extract_channel_id(inbound) is None

"""Regression tests for Teams channel file attachment auth handling."""

from unittest.mock import AsyncMock, patch

import pytest

from app.channels.base import ChannelType, InboundFileAttachment, InboundMessage, OutboundMessage
from app.channels.teams import (
    TeamsChannel,
    _apply_teams_attachment_auth_headers,
    _is_botframework_attachment_url,
    _process_teams_message,
)


class TestIsBotframeworkAttachmentUrl:
    def test_accepts_canonical_https_url(self):
        assert _is_botframework_attachment_url(
            "https://smba.trafficmanager.net/v3/attachments/attachment-id/views/original"
        )

    def test_accepts_regional_subdomain(self):
        assert _is_botframework_attachment_url(
            "https://smba.trafficmanager.net/fr-fr/v3/attachments/attachment-id/views/original"
        )
        assert _is_botframework_attachment_url(
            "https://amer.smba.trafficmanager.net/v3/attachments/attachment-id/views/original"
        )

    def test_rejects_http_scheme(self):
        # HTTP must be rejected to avoid leaking the Authorization bearer token
        # over plaintext if an attacker downgrades the URL.
        assert not _is_botframework_attachment_url(
            "http://smba.trafficmanager.net/fr-fr/v3/attachments/attachment-id/views/original"
        )

    def test_rejects_different_host_with_attachment_path(self):
        assert not _is_botframework_attachment_url(
            "https://attacker.example.com/v3/attachments/attachment-id/views/original"
        )

    def test_rejects_trafficmanager_host_without_attachment_path(self):
        assert not _is_botframework_attachment_url(
            "https://smba.trafficmanager.net/fr-fr/v3/conversations/conversation-id"
        )

    def test_rejects_empty_or_invalid_inputs(self):
        assert not _is_botframework_attachment_url("")
        assert not _is_botframework_attachment_url("not a url")
        assert not _is_botframework_attachment_url("ftp://smba.trafficmanager.net/v3/attachments/x")

    def test_rejects_userinfo_injection_targeting_attacker_host(self):
        """Critical security regression: ``smba.trafficmanager.net`` cannot
        be spoofed via a ``userinfo@`` prefix.

        A URL of the form
        ``https://smba.trafficmanager.net:443@attacker.example.com/v3/attachments/x``
        has ``netloc = 'smba.trafficmanager.net:443@attacker.example.com'``
        but the HTTP client connects to ``attacker.example.com`` (the
        real ``hostname``).  The previous ``netloc.split(':', 1)[0]``
        implementation incorrectly classified this as Bot Framework and
        would have leaked the bot bearer token to the attacker.  The
        ``parsed.hostname`` + explicit ``@`` rejection guard closes both
        the standard userinfo trick and any future ``urlparse`` parser
        quirk that would otherwise let credential-leaking URLs through.
        """
        attacks = [
            # Standard userinfo trick with port.
            "https://smba.trafficmanager.net:443@attacker.example.com/v3/attachments/x",
            # No port — `userinfo` still points at trafficmanager.net but
            # the real host is the attacker.
            "https://smba.trafficmanager.net@attacker.example.com/v3/attachments/x",
            # Trailing-dot trick combined with userinfo (some parsers
            # normalise this to the FQDN form).
            "https://amer.smba.trafficmanager.net@attacker.example.com/v3/attachments/x",
            # Bare ``@`` with userinfo collapsed to empty — still
            # untrusted because the actual host is the attacker.
            "https://@attacker.example.com/v3/attachments/x",
            # Sanity check: a URL with userinfo where even the hostname
            # is in the trafficmanager.net suffix must still be rejected,
            # because no legitimate Bot Framework URL ever carries a
            # ``userinfo@`` prefix.
            "https://user:pass@smba.trafficmanager.net/v3/attachments/x",
        ]
        for url in attacks:
            assert not _is_botframework_attachment_url(url), (
                f"userinfo-injection URL must be rejected: {url}"
            )

    def test_rejects_lookalike_domain_with_trafficmanager_substring(self):
        """Hostname-suffix check must require a literal ``.``-separated
        boundary so a lookalike domain like ``not-trafficmanager.net``
        or ``faketrafficmanager.net`` cannot impersonate the real one.
        """
        assert not _is_botframework_attachment_url(
            "https://not-trafficmanager.net/v3/attachments/x"
        )
        assert not _is_botframework_attachment_url(
            "https://faketrafficmanager.net/v3/attachments/x"
        )
        assert not _is_botframework_attachment_url(
            "https://eviltrafficmanager.net/v3/attachments/x"
        )

    def test_rejects_attacker_registered_trafficmanager_profile(self):
        """Critical security regression: ``*.trafficmanager.net`` profiles
        outside the ``smba.`` label are NOT Bot Framework.

        Azure Traffic Manager lets anyone register arbitrary
        ``*.trafficmanager.net`` profiles, so an attacker can stand up
        ``attacker.trafficmanager.net/v3/attachments/x`` (or any
        variant that doesn't sit under the ``smba.`` label) and trick a
        broad-suffix allowlist into attaching the bot bearer token to a
        request they receive — leaking the credential.

        The allowlist is anchored at ``smba.trafficmanager.net`` so:

        * ``smba.trafficmanager.net`` (exact)                   → ACCEPTED elsewhere
        * ``amer.smba.trafficmanager.net`` (regional subdomain) → ACCEPTED elsewhere
        * ``attacker.trafficmanager.net``                       → MUST be rejected here
        * ``attackersmba.trafficmanager.net`` (no dot boundary) → MUST be rejected here
        * ``smba.trafficmanager.net.attacker.com`` (path-style spoof) → MUST be rejected here
        """
        attacks = [
            # Plain attacker-registered Traffic Manager profile.
            "https://attacker.trafficmanager.net/v3/attachments/x",
            # Multi-label attacker profile still under trafficmanager.net.
            "https://attacker.subdomain.trafficmanager.net/v3/attachments/x",
            # Substring spoof with no ``.`` boundary before ``smba``.
            "https://attackersmba.trafficmanager.net/v3/attachments/x",
            # Attacker host that hangs the real Bot Framework label off
            # the front but resolves to a different parent — must NOT
            # match because the ``endswith`` check is anchored at the
            # tail.
            "https://smba.trafficmanager.net.attacker.com/v3/attachments/x",
            # The bare ``trafficmanager.net`` parent itself is not a
            # Bot Framework host either, so it must be rejected.
            "https://trafficmanager.net/v3/attachments/x",
        ]
        for url in attacks:
            assert not _is_botframework_attachment_url(url), (
                f"attacker-controlled trafficmanager.net profile must be rejected: {url}"
            )


class TestApplyTeamsAttachmentAuthHeaders:
    def test_empty_token_returns_zero_and_does_not_mutate(self):
        attachments = [
            InboundFileAttachment(
                url="https://smba.trafficmanager.net/v3/attachments/x/views/original",
                filename="file.png",
            )
        ]
        applied = _apply_teams_attachment_auth_headers(attachments, "")
        assert applied == 0
        assert attachments[0].auth_headers == {}

    def test_empty_attachment_list_returns_zero(self):
        assert _apply_teams_attachment_auth_headers([], "bot-token") == 0

    def test_skips_attachment_with_existing_authorization_header(self):
        attachments = [
            InboundFileAttachment(
                url="https://smba.trafficmanager.net/v3/attachments/x/views/original",
                filename="file.png",
                auth_headers={"Authorization": "Bearer existing-token"},
            )
        ]
        applied = _apply_teams_attachment_auth_headers(attachments, "bot-token")
        assert applied == 0
        assert attachments[0].auth_headers == {"Authorization": "Bearer existing-token"}

    def test_applies_bearer_to_only_botframework_urls(self):
        attachments = [
            InboundFileAttachment(
                url="https://smba.trafficmanager.net/v3/attachments/x/views/original",
                filename="bf-file.png",
            ),
            InboundFileAttachment(
                url="https://contoso.sharepoint.com/shared/doc.docx",
                filename="sp-file.docx",
            ),
        ]
        applied = _apply_teams_attachment_auth_headers(attachments, "bot-token")
        assert applied == 1
        assert attachments[0].auth_headers == {"Authorization": "Bearer bot-token"}
        assert attachments[1].auth_headers == {}

    def test_never_applies_bearer_to_userinfo_spoofed_url(self):
        """End-to-end security regression — the bot bearer token must
        NEVER be attached to a URL that ``netloc``-string-splitting
        would have misclassified as Bot Framework.  Pairs with the
        ``_is_botframework_attachment_url`` userinfo-injection test:
        proves the upstream classifier change actually closes the
        token-leak path, not just the classification function.
        """
        attachments = [
            InboundFileAttachment(
                url="https://smba.trafficmanager.net:443@attacker.example.com/v3/attachments/x",
                filename="evil.png",
            ),
            InboundFileAttachment(
                url="https://user:pass@smba.trafficmanager.net/v3/attachments/x",
                filename="also-evil.png",
            ),
        ]
        applied = _apply_teams_attachment_auth_headers(attachments, "bot-token")
        assert applied == 0
        for att in attachments:
            assert "Authorization" not in att.auth_headers, (
                f"Bot bearer token must not leak to {att.url}"
            )

    def test_never_applies_bearer_to_attacker_registered_trafficmanager_profile(self):
        """End-to-end security regression — the bot bearer token must
        NEVER be attached to an attacker-registered
        ``*.trafficmanager.net`` profile outside the ``smba.`` label.

        Pairs with ``test_rejects_attacker_registered_trafficmanager_profile``
        on the classifier: proves the narrowed allowlist actually keeps
        the bearer token from leaking to attacker-controlled hosts
        through the public ``_apply_teams_attachment_auth_headers``
        entry point.
        """
        attachments = [
            InboundFileAttachment(
                url="https://attacker.trafficmanager.net/v3/attachments/x",
                filename="attacker-profile.png",
            ),
            InboundFileAttachment(
                url="https://attackersmba.trafficmanager.net/v3/attachments/x",
                filename="lookalike-no-dot.png",
            ),
            InboundFileAttachment(
                url="https://smba.trafficmanager.net.attacker.com/v3/attachments/x",
                filename="suffix-spoof.png",
            ),
        ]
        applied = _apply_teams_attachment_auth_headers(attachments, "bot-token")
        assert applied == 0
        for att in attachments:
            assert "Authorization" not in att.auth_headers, (
                f"Bot bearer token must not leak to {att.url}"
            )


@pytest.mark.asyncio
async def test_process_teams_message_adds_bot_auth_to_botframework_attachments():
    inbound = InboundMessage(
        channel=ChannelType.TEAMS,
        text="Please review the files",
        user_email="user@example.com",
        file_attachments=[
            InboundFileAttachment(
                url="https://smba.trafficmanager.net/fr-fr/v3/attachments/attachment-id/views/original",
                filename="botframework-file.png",
            ),
            InboundFileAttachment(
                url="https://contoso.sharepoint.com/sites/copilot/shared/document.docx",
                filename="sharepoint-file.docx",
            ),
        ],
        metadata={
            "conversation_type": "channel",
            "bot_was_mentioned": False,
        },
    )

    with (
        patch(
            "app.channels.teams._get_teams_creds",
            new=AsyncMock(return_value=("bot-token", "bot-app-id")),
        ),
        patch(
            "app.channels.teams._store_conversation_reference",
            new=AsyncMock(),
        ),
        patch(
            "app.channels.teams.handle_message",
            new=AsyncMock(return_value=OutboundMessage(text="Done")),
        ),
        patch(
            "app.channels.teams.TeamsChannel.send_message",
            new=AsyncMock(return_value="reply-id"),
        ),
    ):
        await _process_teams_message(
            inbound,
            "https://smba.trafficmanager.net/fr-fr/",
            "19:conversation-id",
        )

    assert inbound.file_attachments[0].auth_headers == {
        "Authorization": "Bearer bot-token"
    }
    assert inbound.file_attachments[1].auth_headers == {}


_ADAPTIVE_CARD = {
    "type": "AdaptiveCard",
    "version": "1.5",
    "body": [{"type": "TextBlock", "text": "Daily digest"}],
}


def _patch_send(captured: dict):
    """Patch ``httpx.AsyncClient`` so ``send_message`` captures its wire payload.

    Returns a context manager that records the posted ``json`` body into
    ``captured["activity"]`` and returns a 200 response carrying an activity id.
    """

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "act-1"}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["activity"] = json
            return _FakeResp()

    return patch("app.channels.teams.httpx.AsyncClient", lambda *a, **k: _FakeClient())


_INTERACTIVE_CARD = {
    "type": "AdaptiveCard",
    "version": "1.5",
    "body": [{"type": "TextBlock", "text": "Approve?"}],
    "actions": [{"type": "Action.Submit", "title": "Approve", "data": {"ok": True}}],
}


@pytest.mark.asyncio
async def test_send_message_interactive_card_in_channel_uses_summary_not_text():
    """Regression: an *interactive* Adaptive Card reply in a CHANNEL must carry
    its fallback on ``summary``, never ``text``.

    An activity with BOTH ``text`` and ``attachments`` is rejected by the
    Teams/Skype connector with HTTP 400 "Activity resulted into multiple skype
    activities". The agent reply path (``TeamsChannel.send_message``) must use
    ``summary`` for the fallback, mirroring ``_build_card_activity``. Channels
    keep cards (they render full-width there), and interactive cards always stay
    cards (Markdown cannot represent a submit button).
    """
    channel = TeamsChannel(
        "https://smba.trafficmanager.net/amer/",
        "19:conversation-id",
        "bf-token",
        bot_id="bot-app-id",
        conversation_type="channel",
    )
    captured: dict = {}
    msg = OutboundMessage(
        text="Approval needed",
        metadata={"adaptive_card": dict(_INTERACTIVE_CARD)},
    )
    with _patch_send(captured):
        result = await channel.send_message(msg)

    assert result == "act-1"
    activity = captured["activity"]
    assert "text" not in activity, (
        "`text` alongside `attachments` triggers the multi-skype-activity 400"
    )
    assert activity["summary"] == "Approval needed"
    content = activity["attachments"][0]["content"]
    assert content["msteams"]["width"] == "Full"


@pytest.mark.asyncio
async def test_send_message_display_card_in_chat_becomes_full_width_text():
    """A display-only card replied into a CHAT surface (group/1:1) is delivered
    as full-width Markdown text, not a bubble-constrained card attachment.

    Adaptive Cards are capped to the message-bubble width in chats regardless
    of msteams.width=Full (Microsoft platform behaviour), whereas Markdown text
    renders edge-to-edge — so the digest spans the full conversation again.
    """
    channel = TeamsChannel(
        "https://smba.trafficmanager.net/amer/",
        "19:group-chat@thread.v2",
        "bf-token",
        bot_id="bot-app-id",
        conversation_type="groupChat",
    )
    captured: dict = {}
    msg = OutboundMessage(
        text="Daily digest is ready",
        metadata={"adaptive_card": dict(_ADAPTIVE_CARD)},
    )
    with _patch_send(captured):
        await channel.send_message(msg)

    activity = captured["activity"]
    assert "attachments" not in activity, (
        "display cards in chats must downgrade to full-width Markdown text"
    )
    assert "Daily digest" in activity["text"]
    assert "summary" not in activity


@pytest.mark.asyncio
async def test_send_message_display_card_in_channel_without_type_stays_card():
    """When ``conversation_type`` is not provided (e.g. sub_agent.py and the
    monitoring helpers construct TeamsChannel without it), a display card sent
    to a real CHANNEL conversation id must still be kept as a card - inferred
    from the ``@thread.tacv2`` suffix - not silently downgraded to Markdown.
    """
    channel = TeamsChannel(
        "https://smba.trafficmanager.net/amer/",
        "19:abc@thread.tacv2",
        "bf-token",
        bot_id="bot-app-id",
    )
    captured: dict = {}
    msg = OutboundMessage(
        text="Daily digest is ready",
        metadata={"adaptive_card": dict(_ADAPTIVE_CARD)},
    )
    with _patch_send(captured):
        await channel.send_message(msg)

    activity = captured["activity"]
    assert "text" not in activity, (
        "a real channel card (inferred from the id suffix) must stay a card"
    )
    assert (
        activity["attachments"][0]["contentType"]
        == "application/vnd.microsoft.card.adaptive"
    )


@pytest.mark.asyncio
async def test_send_message_display_card_in_chat_without_type_downgrades():
    """Without ``conversation_type``, a display card sent to a CHAT id
    (``@thread.v2`` group chat) is still downgraded to full-width Markdown
    text - the suffix-based inference resolves it as a chat surface.
    """
    channel = TeamsChannel(
        "https://smba.trafficmanager.net/amer/",
        "19:group-chat@thread.v2",
        "bf-token",
        bot_id="bot-app-id",
    )
    captured: dict = {}
    msg = OutboundMessage(
        text="Daily digest is ready",
        metadata={"adaptive_card": dict(_ADAPTIVE_CARD)},
    )
    with _patch_send(captured):
        await channel.send_message(msg)

    activity = captured["activity"]
    assert "attachments" not in activity
    assert "Daily digest" in activity["text"]


@pytest.mark.asyncio
async def test_send_message_unknown_conversation_type_falls_back_to_suffix():
    """An unknown/placeholder ``conversation_type`` (e.g. "?", the default
    _process_teams_message uses when metadata lacks it) must NOT be treated as
    authoritative. A channel conversation id with such a value still keeps the
    card via suffix inference, instead of being wrongly downgraded.
    """
    channel = TeamsChannel(
        "https://smba.trafficmanager.net/amer/",
        "19:abc@thread.tacv2",
        "bf-token",
        bot_id="bot-app-id",
        conversation_type="?",
    )
    captured: dict = {}
    msg = OutboundMessage(
        text="Daily digest is ready",
        metadata={"adaptive_card": dict(_ADAPTIVE_CARD)},
    )
    with _patch_send(captured):
        await channel.send_message(msg)

    activity = captured["activity"]
    assert "text" not in activity, (
        "an unknown conversation_type must not force a chat downgrade on a "
        "channel id"
    )
    assert (
        activity["attachments"][0]["contentType"]
        == "application/vnd.microsoft.card.adaptive"
    )


@pytest.mark.asyncio
async def test_send_message_interactive_card_in_chat_stays_card():
    """An *interactive* card in a chat stays a card (Markdown cannot represent
    a submit button), still using the ``summary`` fallback."""
    channel = TeamsChannel(
        "https://smba.trafficmanager.net/amer/",
        "19:group-chat@thread.v2",
        "bf-token",
        bot_id="bot-app-id",
        conversation_type="groupChat",
    )
    captured: dict = {}
    msg = OutboundMessage(
        text="Approval needed",
        metadata={"adaptive_card": dict(_INTERACTIVE_CARD)},
    )
    with _patch_send(captured):
        await channel.send_message(msg)

    activity = captured["activity"]
    assert "text" not in activity
    assert activity["summary"] == "Approval needed"
    assert (
        activity["attachments"][0]["contentType"]
        == "application/vnd.microsoft.card.adaptive"
    )


@pytest.mark.asyncio
async def test_send_message_plain_text_still_uses_text():
    """A plain (no card) reply keeps using ``text`` — no attachments present."""
    channel = TeamsChannel(
        "https://smba.trafficmanager.net/amer/",
        "19:conversation-id",
        "bf-token",
        bot_id="bot-app-id",
    )
    captured: dict = {}
    msg = OutboundMessage(text="Hello there")
    with _patch_send(captured):
        await channel.send_message(msg)

    activity = captured["activity"]
    assert activity["text"] == "Hello there"
    assert "attachments" not in activity
    assert "summary" not in activity

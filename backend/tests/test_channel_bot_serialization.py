"""Regression test: the channel-bot serializer must round-trip ``only_mention``.

``_to_response`` previously omitted ``only_mention``.  Because
``ChannelBotResponse.only_mention`` defaults to ``False``, every
``GET``/``POST``/``PUT`` ``/channel-bots`` response silently reported
``only_mention=False`` regardless of the stored value.  The Settings ->
Channels & Bots "Only when mentioned" toggle therefore appeared to "not save":
the ``PUT`` persisted the column correctly, but the refetched list always
rendered the switch as off (while ``allow_anonymous`` / ``allow_private``
worked because they *were* serialized).

This pins the serializer so the field can never be dropped again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.v1.channel_bots import _to_response


def _fake_bot(**overrides) -> SimpleNamespace:
    """Build a lightweight stand-in for a ``ChannelBot`` ORM row.

    Only the attributes read by ``_to_response`` are populated, so the test
    needs neither a database session nor the SQLAlchemy default machinery.
    """
    base = {
        "id": uuid.uuid4(),
        "user_id": None,
        "is_company_managed": True,
        "shared_group_ids": None,
        "shared_group_permissions": None,
        "provider": "teams",
        "name": "Test bot",
        "status": "connected",
        "api_key_enc": None,
        "client_id_enc": None,
        "client_secret_enc": None,
        "extra_config_enc": None,
        "allow_anonymous": False,
        "allow_private": True,
        "only_mention": True,
        "requires_approval": True,
        "allowed_receive_channels": None,
        "allowed_publish_channels": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("value", [True, False])
def test_to_response_round_trips_only_mention(value: bool):
    bot = _fake_bot(only_mention=value)

    resp = _to_response(bot)

    # Before the fix this always returned False (schema default) because the
    # serializer never forwarded the column.
    assert resp.only_mention is value


@pytest.mark.parametrize("value", [True, False])
def test_to_response_round_trips_requires_approval(value: bool):
    """The switch decides whether this bot's conversations pause.

    Serialised for the same reason ``only_mention`` is: a column the response
    never forwarded would leave the switch reading its schema default, so the
    UI would show every bot as gating however it was actually set.
    """
    resp = _to_response(_fake_bot(requires_approval=value))

    assert resp.requires_approval is value

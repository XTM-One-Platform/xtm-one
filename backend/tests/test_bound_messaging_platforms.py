"""Which messaging platforms an agent reaches, from the catalogs and its channels."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.agent_chat import bound_messaging_platforms


def _agent(integration_bindings=None, channel_config=None, integration_template_bindings=None):
    return SimpleNamespace(
        integration_bindings=integration_bindings,
        channel_config=channel_config,
        integration_template_bindings=integration_template_bindings,
    )


def test_a_template_binding_is_a_bound_platform():
    """Each user's own integration of the provider fills it, in the same chat path."""
    agent = _agent(integration_template_bindings=["slack"], channel_config={"teams": {"channel_bot_id": "b1"}})

    assert bound_messaging_platforms(agent) == {"slack", "teams"}


def test_two_configured_channel_bots_are_two_platforms():
    agent = _agent(
        channel_config={
            "slack": {"channel_bot_id": "00000000-0000-4000-8000-000000000001"},
            "discord": {"channel_bot_id": "00000000-0000-4000-8000-000000000002"},
        }
    )

    assert bound_messaging_platforms(agent) == {"slack", "discord"}


def test_an_integration_counts_when_its_catalog_types_it_messaging():
    agent = _agent(integration_bindings={"slack": ["i1"], "github": ["i2"]})

    assert bound_messaging_platforms(agent) == {"slack"}


def test_an_integration_and_a_bot_of_one_platform_are_one_platform():
    """The catalog entry names the channel an integration's messaging tools reach."""
    agent = _agent(
        integration_bindings={"slack": ["i1"], "microsoft": ["i2"]},
        channel_config={
            "slack": {"channel_bot_id": "00000000-0000-4000-8000-000000000001"},
            "teams": {"channel_bot_id": "00000000-0000-4000-8000-000000000002"},
        },
    )

    assert bound_messaging_platforms(agent) == {"slack", "teams"}


def test_a_channel_without_a_bot_and_no_bindings_count_for_nothing():
    agent = _agent(channel_config={"teams": {"receive_enabled": True}})

    assert bound_messaging_platforms(agent) == set()
    assert bound_messaging_platforms(_agent()) == set()

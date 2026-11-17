"""Every field the agent response declares is actually populated.

`_to_response` builds `AgentResponse(...)` field by field. Omitting one does not
raise: the response model's default quietly stands in, and the API reports that
default for every agent forever. For `human_in_the_loop` the default was
`False`, so the Configuration tab read "human-in-the-loop: off" on agents that
were gating — telling an operator the opposite of the truth about a safety
control, with nothing to prompt them to look further.

Asserted over the whole model rather than the fields that were wrong, because
the failure is silent by construction and the next omission would be too.
"""

from __future__ import annotations

import inspect
import re

from app.api.v1 import agents as agents_module
from app.api.v1.schemas.agent import AgentResponse
from app.models.agent import Agent


def test_the_serialiser_passes_every_field_the_model_can_supply():
    source = inspect.getsource(agents_module._to_response)
    passed = set(re.findall(r"(\w+)=", source))

    declared = set(AgentResponse.model_fields)
    on_the_model = {c.name for c in Agent.__table__.columns}

    missing = sorted(f for f in declared - passed if f in on_the_model)

    assert not missing, (
        "these response fields exist on the agent but are never passed, so the "
        f"API reports their default instead of the agent's value: {missing}"
    )


def test_the_legacy_hitl_flag_keeps_mains_default():
    """``human_in_the_loop`` is main's prompt-driven switch again."""
    from app.api.v1.schemas.agent import CreateAgentRequest

    assert AgentResponse.model_fields["human_in_the_loop"].default is False
    assert CreateAgentRequest.model_fields["human_in_the_loop"].default is False


def test_a_new_agent_follows_the_install_default():
    """NULL means "follow the install", so nothing here decides the gate."""
    from app.api.v1.schemas.agent import CreateAgentRequest

    assert CreateAgentRequest.model_fields["approve_tool_calls"].default is None
    assert Agent.__table__.c.approve_tool_calls.nullable
    assert Agent.__table__.c.approve_tool_calls.default is None

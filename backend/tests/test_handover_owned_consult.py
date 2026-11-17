"""A builder consults the agents its user owns once, and every consult refusal says why.

An agent holding the agent-builder tools may already create, configure,
trigger and hand a background task to any agent its user owns. It may
consult one too - a desk it built, without a scheduled run - and the consulted
agent answers with all its handover tools, so its handover is tested as a run
exercises it (a configured consult peer keeps its consult handover only,
``test_handover_consult_chain``). A configured peer of another handover type
stays refused, named with the tool that reaches it; an unknown agent gets the
plain refusal.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

from app.services import agent_chat
from app.services.builtin_tools import consult_refusal, owned_consult_target


def _agent(name: str, **extra):
    return SimpleNamespace(id=uuid.uuid4(), name=name, slug=name.lower().replace(" ", "-"), enabled=True, **extra)


def test_an_unknown_agent_gets_the_plain_refusal():
    peer = _agent("Reviewer")
    assert consult_refusal("ghost", {str(peer.id): "consult"}, [peer]) == "Agent ghost is not available for consultation."


def test_a_peer_of_another_type_is_named_with_its_tool():
    worker, closer = _agent("Worker"), _agent("Closer")
    types = {str(worker.id): "delegate", str(closer.id): "transfer"}
    assert consult_refusal("worker", types, [worker, closer]) == (
        "Agent worker is not available for consultation: it is a delegate peer of this agent - "
        "use spawn_background_task, or change its handover type to consult."
    )
    assert "use transfer_to_agent" in consult_refusal(str(closer.id), types, [worker, closer])
    assert "is not offered on this turn" in consult_refusal("worker", types, [worker, closer], offered={"consult"})


def test_a_builder_is_told_an_owned_agent_would_do():
    assert consult_refusal("ghost", {}, [], owned_reachable=True) == (
        "Agent ghost is not available for consultation: it is neither a consult peer of this "
        "agent nor an enabled agent you own."
    )


async def test_an_owned_agent_is_a_consult_target_of_a_builder():
    desk, me, peer = _agent("Mail Desk"), _agent("Chief of Staff"), _agent("Worker")
    owned = {"mail-desk": desk, str(me.id): me, "worker": peer}

    async def _resolve(ref):
        return owned.get(ref)

    async def _target(ref):
        return await owned_consult_target(ref, configured_peers=[peer], resolve_owned_target=_resolve, self_id=str(me.id))

    assert await _target("mail-desk") is desk
    assert await _target(str(me.id)) is None, "never itself"
    assert await _target("worker") is None, "a peer of another type keeps its refusal"
    desk.enabled = False
    assert await _target("mail-desk") is None, "never a disabled agent"
    assert await owned_consult_target("mail-desk", configured_peers=[], resolve_owned_target=None, self_id="x") is None


def test_chat_offers_consult_to_a_builder_and_runs_an_owned_target_with_its_handover():
    source = inspect.getsource(agent_chat.run_agent_chat)
    gate = source.split("_owned_consult = (", 1)[1].split("\n        )", 1)[0]
    assert '"consult" in _offered_types' in gate and "allow_handover" in gate and "not is_anonymous_user" in gate
    assert "delegation_depth == 0" in gate, "a consulted builder never consults its own agents in turn"
    assert "if handover_agents or _owned_consult is not None:" in source
    assert 'needs_consult = "consult" in _h_types or _owned_consult is not None' in source
    # A peer consult keeps its consult handover only; an owned one keeps every
    # handover tool, one level deeper.
    assert "_nested_handover, _nested_types = consulted_peer_handover(delegation_depth + 1)" in source
    assert "_nested_handover, _nested_types = True, None" in source
    assert "allow_handover=_nested_handover," in source
    assert "allowed_handover_types=_nested_types," in source
    assert "delegation_depth=delegation_depth + 1," in source

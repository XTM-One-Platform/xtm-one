"""The A2A liaisons the agent graphs draw (``app.a2a.liaisons``).

Covers the graph data served to the UI: partner nodes, edges and their
direction, the fan-out of one integration to several agents and of one
outbound registration to its cards, and the visibility filter (an integration
the caller cannot see, or with no direction, draws nothing).
"""

from __future__ import annotations

import uuid

from app.a2a.liaisons import (
    LiaisonAgent,
    LiaisonIntegration,
    LiaisonTarget,
    build_liaisons,
    partner_node_id,
)

AGENT_IN = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
AGENT_OUT = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
AGENT_BOTH = uuid.UUID("00000000-0000-0000-0000-0000000000a3")
INBOUND = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
OUTBOUND = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
HIDDEN = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
CARD_1 = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
CARD_2 = uuid.UUID("00000000-0000-0000-0000-0000000000c2")


def _integrations() -> list[LiaisonIntegration]:
    return [
        LiaisonIntegration(id=INBOUND, name="Partner intake", provider="oauth_a2a"),
        LiaisonIntegration(id=OUTBOUND, name="Partner desk", provider="oauth_a2a"),
    ]


def _directions() -> dict[uuid.UUID, str]:
    return {INBOUND: "inbound", OUTBOUND: "outbound", HIDDEN: "inbound"}


def _targets() -> list[LiaisonTarget]:
    return [
        LiaisonTarget(id=CARD_1, integration_id=OUTBOUND, card_url="https://p.example/a/card", declared_name="Triage"),
        LiaisonTarget(id=CARD_2, integration_id=OUTBOUND, card_url="https://p.example/b/card", declared_name=None),
    ]


def test_inbound_partner_tasks_the_bound_agent() -> None:
    agents = [LiaisonAgent(id=AGENT_IN, integration_bindings={"oauth_a2a": [str(INBOUND)]})]
    out = build_liaisons(agents, _integrations(), _directions(), _targets())
    assert [p.id for p in out.partners] == [partner_node_id(INBOUND)]
    node = out.partners[0]
    assert (node.name, node.direction, node.card_urls) == ("Partner intake", "inbound", [])
    assert [(e.source_id, e.target_id, e.direction) for e in out.edges] == [
        (partner_node_id(INBOUND), str(AGENT_IN), "inbound"),
    ]


def test_outbound_agent_delegates_to_each_card() -> None:
    agents = [LiaisonAgent(id=AGENT_OUT, integration_bindings={"oauth_a2a": [str(OUTBOUND)]})]
    out = build_liaisons(agents, _integrations(), _directions(), _targets())
    names = {p.id: p.name for p in out.partners}
    # The card's declared name, else a label from its URL, the distinct part first.
    assert names == {
        partner_node_id(OUTBOUND, CARD_1): "Triage",
        partner_node_id(OUTBOUND, CARD_2): "b · p.example",
    }
    assert {(e.source_id, e.target_id, e.direction) for e in out.edges} == {
        (str(AGENT_OUT), partner_node_id(OUTBOUND, CARD_1), "outbound"),
        (str(AGENT_OUT), partner_node_id(OUTBOUND, CARD_2), "outbound"),
    }
    assert all(p.integration_name == "Partner desk" for p in out.partners)


def test_two_unnamed_cards_of_one_host_are_told_apart_by_their_leading_words() -> None:
    """A graph cuts a long name at its end: the distinct part leads (#12347 UX r1 B1)."""
    lab = "https://partner.lab.invalid"
    targets = [
        LiaisonTarget(id=CARD_1, integration_id=OUTBOUND, card_url=f"{lab}/triage/.well-known/agent-card.json"),
        LiaisonTarget(id=CARD_2, integration_id=OUTBOUND, card_url=f"{lab}/hunt/.well-known/agent-card.json"),
    ]
    agents = [LiaisonAgent(id=AGENT_OUT, integration_bindings={"oauth_a2a": [str(OUTBOUND)]})]
    out = build_liaisons(agents, _integrations(), _directions(), targets)
    names = {p.id: p.name for p in out.partners}
    assert names == {
        partner_node_id(OUTBOUND, CARD_1): "triage · partner.lab.invalid",
        partner_node_id(OUTBOUND, CARD_2): "hunt · partner.lab.invalid",
    }
    # The full URL stays on the node for its tooltip.
    assert sorted(u for p in out.partners for u in p.card_urls) == sorted(t.card_url for t in targets)


def test_an_unnamed_card_at_the_root_of_its_host_is_the_host() -> None:
    card_url = "https://agents.partner.invalid/.well-known/agent.json"
    targets = [LiaisonTarget(id=CARD_1, integration_id=OUTBOUND, card_url=card_url)]
    agents = [LiaisonAgent(id=AGENT_OUT, integration_bindings={"oauth_a2a": [str(OUTBOUND)]})]
    out = build_liaisons(agents, _integrations(), _directions(), targets)
    assert [p.name for p in out.partners] == ["agents.partner.invalid"]


def test_outbound_without_a_card_is_one_node_for_the_integration() -> None:
    agents = [LiaisonAgent(id=AGENT_OUT, integration_bindings={"oauth_a2a": [str(OUTBOUND)]})]
    out = build_liaisons(agents, _integrations(), _directions(), [])
    assert [p.id for p in out.partners] == [partner_node_id(OUTBOUND)]
    assert [(e.source_id, e.target_id) for e in out.edges] == [(str(AGENT_OUT), partner_node_id(OUTBOUND))]


def test_one_integration_fans_out_to_every_bound_agent() -> None:
    agents = [
        LiaisonAgent(id=AGENT_IN, integration_bindings={"oauth_a2a": [str(INBOUND)]}),
        LiaisonAgent(id=AGENT_BOTH, integration_bindings={"oauth_a2a": [str(INBOUND), str(OUTBOUND)]}),
    ]
    out = build_liaisons(agents, _integrations(), _directions(), _targets())
    inbound_edges = [(e.source_id, e.target_id) for e in out.edges if e.direction == "inbound"]
    assert sorted(inbound_edges) == sorted(
        [
            (partner_node_id(INBOUND), str(AGENT_IN)),
            (partner_node_id(INBOUND), str(AGENT_BOTH)),
        ]
    )
    # One partner node per integration or card, never one per binding.
    assert len(out.partners) == 3


def test_an_integration_the_caller_cannot_see_is_not_drawn() -> None:
    agents = [LiaisonAgent(id=AGENT_IN, integration_bindings={"oauth_a2a": [str(HIDDEN)]})]
    out = build_liaisons(agents, _integrations(), _directions(), _targets())
    assert out.partners == [] and out.edges == []


def test_an_integration_without_a_direction_draws_nothing() -> None:
    agents = [LiaisonAgent(id=AGENT_IN, integration_bindings={"oauth_a2a": [str(INBOUND)]})]
    out = build_liaisons(agents, _integrations(), {}, _targets())
    assert out.partners == [] and out.edges == []


def test_no_a2a_binding_draws_nothing_and_malformed_ids_are_skipped() -> None:
    agents = [
        LiaisonAgent(id=AGENT_IN, integration_bindings={"github": [str(INBOUND)]}),
        LiaisonAgent(id=AGENT_OUT, integration_bindings={"oauth_a2a": ["not-a-uuid"]}),
        LiaisonAgent(id=AGENT_BOTH, integration_bindings=None),
    ]
    out = build_liaisons(agents, _integrations(), _directions(), _targets())
    assert out.partners == [] and out.edges == []


def test_the_answer_is_deterministic() -> None:
    agents = [
        LiaisonAgent(id=AGENT_BOTH, integration_bindings={"oauth_a2a": [str(OUTBOUND), str(INBOUND)]}),
        LiaisonAgent(id=AGENT_IN, integration_bindings={"oauth_a2a": [str(INBOUND)]}),
    ]
    first = build_liaisons(agents, _integrations(), _directions(), _targets())
    second = build_liaisons(list(reversed(agents)), list(reversed(_integrations())), _directions(), _targets())
    assert first == second


def test_no_credential_field_is_served() -> None:
    from app.api.v1.a2a_liaisons import PartnerNodeOut

    fields = set(PartnerNodeOut.model_fields)
    assert fields == {"id", "name", "integration_id", "integration_name", "provider", "direction", "card_urls"}
    assert not any("secret" in f or "token" in f or f == "client_id" for f in fields)

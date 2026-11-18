"""How an OpenCTI names the objects it holds (``providers/opencti/naming.py``).

The lookup reads with the provider's own connection, asks exact matches of a name
or an alias per entity type (never a fuzzy search), and answers a name only when
exactly one object of that type holds it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.opencti import build_tools, naming
from app.services.integration_tools import _routed_lookup
from app.tools.base import CallableToolExecutor

_OBJECTS = {
    "Intrusion-Set": [
        {"id": "is-1", "entity_type": "Intrusion-Set", "name": "APT28", "aliases": ["Fancy Bear", "Sofacy"]},
        {"id": "is-2", "entity_type": "Intrusion-Set", "name": "Lazarus Group", "aliases": ["Lazarus"]},
        {"id": "is-3", "entity_type": "Intrusion-Set", "name": "Andariel", "aliases": ["Lazarus"]},
    ],
    "Sector": [{"id": "s-1", "entity_type": "Sector", "name": "Finance", "x_opencti_aliases": []}],
    "Country": [
        {"id": "c-1", "entity_type": "Country", "name": "Russian Federation", "x_opencti_aliases": ["RU", "RUS", "Russia"]}
    ],
}


def _answer(variables: dict) -> dict:
    """The objects of the asked type holding one of the asked values (case-folded, as
    OpenCTI's keyword normalizer compares)."""
    entity_type = variables["types"][0]
    values = {v.casefold() for f in variables["filters"]["filters"] for v in f["values"]}
    keys = {k for f in variables["filters"]["filters"] for k in f["key"]}
    nodes = [
        node for node in _OBJECTS.get(entity_type, [])
        if values & {str(v).casefold() for k in keys for v in ([node.get(k)] if k == "name" else node.get(k) or [])}
    ]
    return {"data": {"stixDomainObjects": {"edges": [{"node": n} for n in nodes],
                                           "pageInfo": {"globalCount": len(nodes)}}}}


def _lookup(answer=_answer, sent: list | None = None):
    found: list = []
    executor = CallableToolExecutor()
    build_tools("https://octi.test||static-token", executor, platform_names_sink=found.append)
    assert len(found) == 1

    async def _fake(self, method, url, **kwargs):
        body = kwargs.get("json") or {}
        if sent is not None:
            sent.append((url, kwargs.get("headers") or {}, body))
        request = httpx.Request("POST", url)
        result = answer(body.get("variables") or {})
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result, request=request)

    async def run(pairs):
        with patch.object(httpx.AsyncClient, "request", _fake):
            return await found[0](pairs)

    return run


def test_the_provider_hands_its_lookup_over_only_with_a_connection() -> None:
    found: list = []
    build_tools("https://octi.test||static-token", CallableToolExecutor(), platform_names_sink=found.append)
    assert len(found) == 1 and callable(found[0])
    found.clear()
    build_tools("||static-token", CallableToolExecutor(), platform_names_sink=found.append)
    assert found == []
    # No sink, no lookup, tools unchanged.
    assert build_tools("https://octi.test||static-token", CallableToolExecutor())


def test_names_come_back_as_the_platform_names_them_from_exact_matches() -> None:
    sent: list = []
    run = _lookup(sent=sent)
    pairs = [("intrusion-set", "Fancy Bear"), ("sector", "finance"), ("country", "Russia"),
             ("intrusion-set", "Lazarus"), ("intrusion-set", "Unknown"), ("attack-pattern-id", "T1059")]
    found = asyncio.run(run(pairs))
    assert found == {
        ("intrusion-set", "Fancy Bear"): ("APT28", ["Fancy Bear", "Sofacy"]),
        ("sector", "finance"): ("Finance", []),
        ("country", "Russia"): ("Russian Federation", ["RU", "RUS", "Russia"]),
    }  # Lazarus: two objects hold it; Unknown: none; T1059: not a name the platform identifies
    types = sorted(body["variables"]["types"][0] for _, _, body in sent)
    assert types == ["Country", "Intrusion-Set", "Sector"]
    for url, headers, body in sent:
        assert url == "https://octi.test/graphql"
        assert headers["Authorization"] == "Bearer static-token"
        assert "opencti-draft-id" in headers  # the live graph named, as every provider call does
        assert "search" not in body["variables"]
        group = body["variables"]["filters"]
        assert group["mode"] == "or"
        assert {f["operator"] for f in group["filters"]} == {"eq"}
        assert group["filters"][0]["key"] == ["name"]
    alias_keys = {body["variables"]["types"][0]: body["variables"]["filters"]["filters"][1]["key"] for _, _, body in sent}
    assert alias_keys == {"Intrusion-Set": ["aliases"], "Sector": ["x_opencti_aliases"], "Country": ["x_opencti_aliases"]}
    sector = next(body for _, _, body in sent if body["variables"]["types"] == ["Sector"])
    # Its usual casings too, whether or not the platform's eq ignores case.
    assert {"finance", "Finance"} <= set(sector["variables"]["filters"]["filters"][0]["values"])


def test_names_are_asked_in_bounded_batches() -> None:
    sent: list = []
    run = _lookup(sent=sent)
    asyncio.run(run([("malware", f"Family{i}") for i in range(naming.BATCH_NAMES + 5)]))
    assert len(sent) == 2


def test_a_long_report_s_names_are_cut_in_turn_from_each_kind() -> None:
    """Past the lookup's bound, each kind keeps a share: a report of 400 malware names
    still has its sectors asked (the pairs arrive sorted by kind, and a plain cut dropped
    every kind after the first)."""
    pairs = [("malware", f"Family{i:03d}") for i in range(400)] + [("sector", "finance"), ("tool", "Mimikatz")]
    kept = naming.fair_share(pairs, naming.MAX_NAMES)
    assert len(kept) == naming.MAX_NAMES
    assert ("sector", "finance") in kept and ("tool", "Mimikatz") in kept


def test_one_lookup_opens_one_client_for_its_batches() -> None:
    """Every batch of one lookup goes through one client (one connection, one TLS
    handshake, through a relay too), not one per batch of names."""
    opened: list[int] = []
    real_init = httpx.AsyncClient.__init__

    def counting_init(self, *args, **kwargs):
        opened.append(1)
        real_init(self, *args, **kwargs)

    sent: list = []
    run = _lookup(sent=sent)
    with patch.object(httpx.AsyncClient, "__init__", counting_init):
        asyncio.run(run([("malware", f"Family{i}") for i in range(naming.BATCH_NAMES * 3)]))
    assert len(sent) == 3
    assert len(opened) == 1


def test_a_page_that_does_not_hold_every_match_answers_nothing() -> None:
    def partial(variables: dict) -> dict:
        answer = _answer(variables)
        answer["data"]["stixDomainObjects"]["pageInfo"]["globalCount"] = 500
        return answer

    assert asyncio.run(_lookup(partial)([("intrusion-set", "Fancy Bear")])) is None


@pytest.mark.parametrize("failure", ["graphql", "http"])
def test_a_platform_that_cannot_be_read_answers_none(failure: str) -> None:
    def broken(variables: dict):
        if failure == "graphql":
            return {"errors": [{"message": "boom", "extensions": {"code": "INTERNAL"}}]}
        return httpx.Response(500, text="down", request=httpx.Request("POST", "https://octi.test/graphql"))

    assert asyncio.run(_lookup(broken)([("sector", "finance")])) is None


def test_a_slow_platform_is_given_up_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(naming, "TIMEOUT_SECONDS", 0.05)
    found: list = []
    build_tools("https://octi.test||static-token", CallableToolExecutor(), platform_names_sink=found.append)

    async def _slow(self, method, url, **kwargs):
        await asyncio.sleep(5)

    async def run():
        with patch.object(httpx.AsyncClient, "request", _slow):
            return await found[0]([("sector", "finance")])

    assert asyncio.run(run()) is None


def test_only_kinds_the_platform_names_are_asked() -> None:
    sent: list = []
    assert asyncio.run(_lookup(sent=sent)([("attack-pattern-id", "T1059"), ("url", "http://x")])) == {}
    assert sent == []


def test_a_routed_lookup_runs_on_the_integration_s_relay() -> None:
    from app.core import egress

    seen: list = []

    async def lookup(pairs):
        seen.append(egress.current_route())
        return {}

    route = object()
    asyncio.run(_routed_lookup(lookup, route)([("sector", "finance")]))  # type: ignore[arg-type]
    assert seen == [route]
    assert _routed_lookup(lookup, None) is lookup


def test_chat_and_assignment_runs_hand_their_platform_names_to_the_extraction() -> None:
    """Source pins (the two call graphs are too large to drive end to end here): the
    list the integration build fills is the one the extraction tool reads."""
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    for relative in ("app/services/agent_chat.py", "app/services/assignment_engine.py"):
        source = (backend / relative).read_text(encoding="utf-8")
        assert "platform_names=_platform_names," in source, relative
        assert "PLATFORM_NAMES_CONTEXT_KEY" in source, relative
        # The run's bound MITRE ATT&CK integration the same way, read through its relay.
        assert "attack_sources=_attack_sources" in source, relative
        assert "ATTACK_SOURCES_CONTEXT_KEY" in source, relative
    # An evaluation builds the integrations itself: the chat takes what that build collected.
    chat = (backend / "app/services/agent_chat.py").read_text(encoding="utf-8")
    assert "_platform_names.extend(_prebuilt_context" in chat
    assert "_attack_sources.extend(_prebuilt_context" in chat

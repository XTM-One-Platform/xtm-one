"""The STIX extraction benchmark's ``coarse`` scoring profile, read by a caller that imports it.

A scorer that imports the benchmark module (a held-out set's own scoring script) passes its
choices as arguments: the relationship types scored come from ``args.relations``, not from
state that only the benchmark's command line sets, and an indicator stands for the observable
it is based on whatever relationship types are scored.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "stix_extraction_benchmark.py"


@pytest.fixture(scope="module")
def benchmark():
    spec = importlib.util.spec_from_file_location("stix_extraction_benchmark_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(relations: str) -> argparse.Namespace:
    return argparse.Namespace(profile="coarse", relations=relations, closed_world=False, no_sectors=False,
                              min_confidence=0, alias_split=False)


def _rel(rid: str, kind: str, source: str, target: str) -> dict:
    return {"type": "relationship", "id": f"relationship--{rid}", "relationship_type": kind,
            "source_ref": source, "target_ref": target}


_MALWARE = {"type": "malware", "id": "malware--1", "name": "PeerStitch"}
_DOMAIN = {"type": "domain-name", "id": "domain-name--1", "value": "evil.example"}
_INDICATOR = {"type": "indicator", "id": "indicator--1", "pattern": "[domain-name:value = 'evil.example']"}

_REFERENCE = {"type": "bundle", "objects": [_MALWARE, _DOMAIN, _rel("r1", "communicates-with", "malware--1", "domain-name--1")]}
_PREDICTION = {"type": "bundle", "objects": [
    _MALWARE, _DOMAIN, _INDICATOR,
    _rel("p1", "communicates-with", "malware--1", "domain-name--1"),
    _rel("p2", "based-on", "indicator--1", "domain-name--1"),
    _rel("p3", "indicates", "indicator--1", "malware--1"),
]}


def _counts(benchmark, relations: str) -> tuple[dict, dict]:
    reference, prediction = benchmark.scored(_REFERENCE, _PREDICTION, _args(relations))
    metrics = benchmark.stix_metrics(reference, prediction)
    entity, relationship = metrics["stix.entity_prf.aligned"], metrics["stix.relationship_prf.aligned"]
    return ({k: entity.get(k) or 0 for k in ("tp", "fp", "fn")},
            {k: relationship.get(k) or 0 for k in ("tp", "fp", "fn")})


def test_an_importing_scorer_gets_every_relationship_type_it_asks_for(benchmark):
    _, relationship = _counts(benchmark, "all")
    # communicates-with is scored, and the indicator's indicates reads as the domain's
    assert relationship == {"tp": 1, "fp": 1, "fn": 0}


@pytest.mark.parametrize("relations", ["core", "all"])
def test_an_indicator_based_on_an_observable_is_that_observable_whatever_is_scored(benchmark, relations):
    entity, _ = _counts(benchmark, relations)
    assert entity == {"tp": 2, "fp": 0, "fn": 0}


def test_a_recorded_call_is_replayed_and_a_failed_one_is_not_recorded(benchmark, tmp_path):
    import asyncio

    calls = []

    async def call(*, step, system, user, schema):
        calls.append(step)
        return None if step == "fails" else {"answer": len(calls)}

    replay = benchmark.replayed(call, tmp_path, "model-a")
    first = asyncio.run(replay(step="entities", system="s", user="u", schema={}))
    again = asyncio.run(replay(step="entities", system="s", user="u", schema={}))
    other = asyncio.run(replay(step="entities", system="s", user="another report", schema={}))
    assert first == again == {"answer": 1} and other == {"answer": 2}
    assert asyncio.run(replay(step="fails", system="s", user="u", schema={})) is None
    assert asyncio.run(replay(step="fails", system="s", user="u", schema={})) is None
    assert calls == ["entities", "entities", "fails", "fails"]


def test_two_runs_asking_one_call_at_once_go_on_with_the_first_recorded_answer(benchmark, tmp_path):
    import asyncio

    async def first(*, step, system, user, schema):
        return {"answer": "first"}

    async def second(*, step, system, user, schema):
        # another run records the same call while this one waits for its answer
        await asyncio.to_thread(lambda: None)
        return {"answer": "second"}

    a, b = benchmark.replayed(first, tmp_path, "m"), benchmark.replayed(second, tmp_path, "m")

    async def both():
        return await asyncio.gather(a(step="s", system="x", user="u", schema={}),
                                    b(step="s", system="x", user="u", schema={}))

    answers = asyncio.run(both())
    assert answers[0] == answers[1] and not list(tmp_path.glob("*.tmp"))


def test_a_passage_with_no_text_gives_the_harvester_an_empty_bundle_without_a_call(benchmark):
    import asyncio

    class NoCall:
        async def chat(self, *args, **kwargs):
            raise AssertionError("a passage with no text is not sent")

    args = argparse.Namespace(provider="anthropic", model="m")
    bundle, usage, _ = asyncio.run(benchmark.run_harvester({"text": "  \n"}, NoCall(), args))
    assert bundle == {"type": "bundle", "objects": []} and usage["calls"] == 0


def test_the_recorded_answers_stay_where_files_cannot_be_hard_linked(
    benchmark, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.link`` raises a plain ``OSError`` on a filesystem without hard links: the answer
    is still recorded, as the rename did before the links."""
    import asyncio
    import os

    def no_links(*_: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)

    async def answer(**_: object) -> dict:
        return {"entities": []}

    cached = benchmark.replayed(answer, tmp_path, "m")
    assert asyncio.run(cached(step="detection", system="s", user="u", schema={})) == {"entities": []}
    assert [p.suffix for p in tmp_path.iterdir() if p.name != "misses.log"] == [".json"]

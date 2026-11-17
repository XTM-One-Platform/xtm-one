"""Choices a pack offers in the picker before a run.

A list run pivots by default on the IPs two seeds or more share. The
disinformation pack offers, for a list, to pivot on every seed's IP instead:
a deeper map that costs more VirusTotal calls. The choice is the pack's own
(`PackOption`, rendered from the server, so the frontend names no pack); it
lands in the seeds' source, so its identity, its stored ref and what the step
says it asked all carry it, and a continuation runs what was chosen. A choice
the pack does not offer is refused, never ignored.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.api.v1.chat import (
    MAX_CONFIRMED_PACKS,
    MAX_PACK_OPTION_TEXT,
    InvalidPackOptionsError,
    pack_options_from_frame,
)
from app.services import deep_investigation_chat as dic
from app.services import investigation_queriers as queriers
from app.services.investigation_orchestrator import DeepInvestigationOrchestrator, SourceOutcome
from app.services.investigation_packs import (
    DISINFO_INFRASTRUCTURE_SLUG,
    MAX_ALL_IP_PIVOTS,
    MAX_SHARED_IP_PIVOTS,
    PIVOT_DEPTH_OPTION,
    PackChoice,
    PackOption,
    SourceDefinition,
    UnknownPackOptionError,
    disinfo_infrastructure_pack,
    list_summary,
    with_seeds,
)

from .test_chat_web_steering import fake_rl, ws_env  # noqa: F401
from .test_deep_investigation_chat import _StubSession as _ChatStubSession
from .test_deep_investigation_chat import metering  # noqa: F401
from .test_investigation_orchestrator import _StubSession

VT = ("11111111-1111-1111-1111-111111111111", "Team VirusTotal")
OCTI = ("22222222-2222-2222-2222-222222222222", "OpenCTI")
LIST_SOURCE = "VirusTotal passive DNS of the seeds"
LANG = Path(__file__).resolve().parents[2] / "frontend" / "src" / "i18n" / "lang"
EVERY_IP = {DISINFO_INFRASTRUCTURE_SLUG: {"ip_pivot": "all"}}


def _seeds_source(pack) -> SourceDefinition:
    return next(s for s in pack.sources if s.name == LIST_SOURCE)


# ── The option ──────────────────────────────────────────────────────────────


def test_the_pivot_depth_is_offered_for_a_list_only_and_only_with_virustotal():
    pack = disinfo_infrastructure_pack(VT, OCTI)
    assert [o.key for o in pack.options_for("domain_list")] == ["ip_pivot"]
    assert pack.options_for("domain") == () and pack.options_for("ip") == ()
    assert disinfo_infrastructure_pack(None, OCTI).options_for("domain_list") == ()
    assert PIVOT_DEPTH_OPTION.default == "shared"
    assert [c.value for c in PIVOT_DEPTH_OPTION.choices] == ["shared", "all"]


@pytest.mark.parametrize(
    "choices, default",
    [
        ((PackChoice("a", "A", ""),), "a"),
        ((PackChoice("a", "A", ""), PackChoice("a", "B", "")), "a"),
        ((PackChoice("a", "A", ""), PackChoice("b", "B", "")), "c"),
    ],
)
def test_an_option_needs_distinct_choices_and_a_default_among_them(choices, default):
    with pytest.raises(ValueError):
        PackOption("k", "K", "", ("domain_list",), choices, default, apply=lambda pack, _v: pack)


def test_every_ip_lands_in_the_seeds_source_and_its_budget():
    seeds = ("armdailynews.com", "yerevantimes.am")
    pack = disinfo_infrastructure_pack(VT, OCTI)
    deep = with_seeds(pack.with_options({"ip_pivot": "all"}, "domain_list"), seeds)
    shallow = with_seeds(pack.with_options({"ip_pivot": "shared"}, "domain_list"), seeds)
    assert _seeds_source(deep).pivot_scope == "all"
    assert _seeds_source(shallow).pivot_scope == "shared"
    assert _seeds_source(shallow) == _seeds_source(with_seeds(pack, seeds)), "the default changes nothing"
    assert (deep.max_iterations, shallow.max_iterations) == (2 + 2 * MAX_ALL_IP_PIVOTS, 2 + 2 * MAX_SHARED_IP_PIVOTS)
    # Only the list's source: the one-site sources keep pivoting as before.
    assert [s.name for s in deep.sources if s.pivot_scope != "shared"] == [LIST_SOURCE]
    assert "every IP pivoted on" in _seeds_source(deep).describe(list_summary(seeds))
    assert "share pivoted on" in _seeds_source(shallow).describe(list_summary(seeds))


@pytest.mark.parametrize(
    ("chosen", "subject_type"),
    [
        ({"ip_pivot": "everything"}, "domain_list"),
        ({"depth": "all"}, "domain_list"),
        ({"ip_pivot": "all"}, "domain"),  # not offered for one site
    ],
)
def test_a_choice_the_pack_does_not_offer_is_refused(chosen, subject_type):
    with pytest.raises(UnknownPackOptionError):
        disinfo_infrastructure_pack(VT, OCTI).with_options(chosen, subject_type)


def test_the_scope_is_part_of_the_source_and_survives_its_stored_address():
    seeds = ("armdailynews.com", "լրատու.հայ")
    pack = disinfo_infrastructure_pack(VT, OCTI)
    deep = _seeds_source(with_seeds(pack.with_options({"ip_pivot": "all"}, "domain_list"), seeds))
    shallow = _seeds_source(with_seeds(pack, seeds))
    assert deep.identity() != shallow.identity()
    assert deep.access_identity() == shallow.access_identity(), "the same VirusTotal is asked"
    assert deep.to_ref()["pivot_scope"] == "all" and "pivot_scope" not in shallow.to_ref()
    rebuilt = SourceDefinition.from_ref(deep.to_ref(), subject_types=deep.subject_types)
    assert rebuilt.identity() == deep.identity() and rebuilt.pivot_scope == "all"
    with pytest.raises(ValueError):
        SourceDefinition.from_ref({**deep.to_ref(), "pivot_scope": "deeper"}, subject_types=deep.subject_types)


def test_a_scope_is_a_passive_dns_setting():
    with pytest.raises(ValueError):
        SourceDefinition(name="X", url_template="https://x.test/{value}", subject_types=("domain",), pivot_scope="all")


# ── The pivot ───────────────────────────────────────────────────────────────


def _resolution(host: str, ip: str) -> dict:
    return {"type": "resolution", "attributes": {"host_name": host, "ip_address": ip, "date": 1780000000}}


async def _key(_integration_id, _user_id):
    return "vt-key"


async def _seeds(answers: dict[str, list[str]], scope: str):
    def _handler(request: httpx.Request) -> httpx.Response:
        domain = request.url.path.split("/")[-2]
        return httpx.Response(200, json={"data": [_resolution(domain, ip) for ip in answers[domain]]})

    querier = queriers.make_passive_dns_querier(
        user_id="u", key_loader=_key, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    )
    pack = disinfo_infrastructure_pack(VT, OCTI).with_options({"ip_pivot": scope}, "domain_list")
    source = _seeds_source(with_seeds(pack, tuple(answers)))
    return await querier(source, list_summary(tuple(answers)), "")


@pytest.mark.asyncio
async def test_every_ip_pivots_on_the_ips_one_seed_resolved_to_too():
    answers = {"a.com": ["1.1.1.1"], "b.com": ["2.2.2.2"], "c.com": ["2.2.2.2"], "d.com": ["3.3.3.3"]}
    shallow = await _seeds(answers, "shared")
    deep = await _seeds(answers, "all")
    assert [lead.subject for lead in shallow.leads] == ["2.2.2.2"]
    # Most-shared first, then the order the seeds were given in.
    assert [lead.subject for lead in deep.leads] == ["2.2.2.2", "1.1.1.1", "3.3.3.3"]
    assert deep.detail_params == {"resolved": 4, "seeds": 4, "ips": 3, "shared": 1, "pivoted": 3}
    assert all(lead.hop_fingerprint is False for lead in deep.leads)


@pytest.mark.asyncio
async def test_every_ip_is_capped_and_says_how_many_it_pivots_on():
    answers = {f"s{i}.com": [f"45.84.205.{i}"] for i in range(MAX_ALL_IP_PIVOTS + 5)}
    deep = await _seeds(answers, "all")
    assert len(deep.leads) == MAX_ALL_IP_PIVOTS
    assert deep.detail_params["ips"] == MAX_ALL_IP_PIVOTS + 5
    assert deep.detail_params["pivoted"] == MAX_ALL_IP_PIVOTS


# ── The run ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_run_with_every_ip_records_the_choice():
    async def _query(_source, _subject_value, _url):
        return SourceOutcome(findings_count=1)

    result = await DeepInvestigationOrchestrator(_StubSession(), _query).run(
        subject_value="armdailynews.com\nyerevantimes.am",
        packs=[disinfo_infrastructure_pack(VT, OCTI)],
        pack_options=EVERY_IP,
    )
    seeds_step = next(s for s in result.steps if s.source_name == LIST_SOURCE)
    assert seeds_step.source_ref["pivot_scope"] == "all"
    assert result.investigation.max_iterations == 2 + 2 * MAX_ALL_IP_PIVOTS


@pytest.mark.asyncio
async def test_a_run_with_a_choice_the_pack_does_not_offer_is_never_written():
    db = _StubSession()

    async def _query(_source, _subject_value, _url):
        raise AssertionError("nothing is queried")

    with pytest.raises(UnknownPackOptionError):
        await DeepInvestigationOrchestrator(db, _query).run(
            subject_value="armdailynews.com\nyerevantimes.am",
            packs=[disinfo_infrastructure_pack(VT, OCTI)],
            pack_options={DISINFO_INFRASTRUCTURE_SLUG: {"ip_pivot": "deeper"}},
        )
    assert db.objects == [] and db.commits == 0


@pytest.mark.asyncio
async def test_the_turn_answers_a_refused_choice_and_gives_the_quota_back(monkeypatch, metering):  # noqa: F811
    from app.services import investigation_packs as packs

    monkeypatch.setattr(packs, "BUILTIN_PACKS", {DISINFO_INFRASTRUCTURE_SLUG: disinfo_infrastructure_pack(VT, OCTI)})
    text = await dic.run_deep_investigation_turn(
        _ChatStubSession(),
        subject="armdailynews.com\nyerevantimes.am",
        user_id="00000000-0000-0000-0000-000000000001",
        source_pack_slugs=[DISINFO_INFRASTRUCTURE_SLUG],
        pack_options={DISINFO_INFRASTRUCTURE_SLUG: {"ip_pivot": "deeper"}},
    )
    assert text == "That source pack option is no longer available. Pick another and try again."
    assert "deeper" not in text, "the reason stays in the logs"
    assert [str(u) for u in metering["uncharged"]] == ["00000000-0000-0000-0000-000000000001"]


# ── The frame ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (EVERY_IP, EVERY_IP),
        ({" disinfo-infrastructure ": {" ip_pivot ": " all "}}, EVERY_IP),
        ({"p": {}}, None),
        ({}, None),
        (None, None),
    ],
)
def test_the_frame_reads_text_choices(raw, expected):
    assert pack_options_from_frame(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # A choice dropped would run at its default: refused whole instead.
        {"p": {"k": 1, "ok": "v"}},
        {"p": {"k": None, "ok": "v"}},
        {"p": {"": "v", "ok": "v"}},
        {"p": {"k": "  ", "ok": "v"}},
        {"p": {"k": "x" * (MAX_PACK_OPTION_TEXT + 1)}},
        {"": {"k": "v"}},
        # Alike once stripped: one of the two choices would be lost.
        {"p": {"k": "off", " k ": "on"}},
        {"p": {"k": "v"}, " p ": {"j": "v"}},
        {"p": "all"},
        {f"p{i}": {"k": "v"} for i in range(MAX_CONFIRMED_PACKS + 1)},
        ["p"],
        "all",
    ],
)
def test_a_frame_that_cannot_be_read_whole_is_refused(raw):
    with pytest.raises(InvalidPackOptionsError):
        pack_options_from_frame(raw)


# ── The strings ─────────────────────────────────────────────────────────────


def _option_strings() -> set[str]:
    strings: set[str] = set()
    for option in disinfo_infrastructure_pack(VT, OCTI).options:
        strings.update((option.label, option.description))
        for choice in option.choices:
            strings.update((choice.label, choice.description))
    return strings


@pytest.mark.parametrize("locale", ["en", "fr", "es", "ja"])
def test_every_string_an_option_sends_is_translated(locale):
    catalog = json.loads((LANG / f"{locale}.json").read_text(encoding="utf-8"))
    assert not sorted(_option_strings() - set(catalog))
    assert not sorted(s for s in _option_strings() if (catalog[s] == s) != (locale == "en"))


# ── The routes ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_picker_is_served_the_option_for_a_list_only(monkeypatch):
    import uuid
    from types import SimpleNamespace

    from app.api.v1.investigations import plan_source_packs
    from app.services import investigation_pack_resolver

    async def _resolve(_db, _user):
        return {DISINFO_INFRASTRUCTURE_SLUG: disinfo_infrastructure_pack(VT, OCTI)}

    monkeypatch.setattr(investigation_pack_resolver, "resolve_packs", _resolve)
    user = SimpleNamespace(id=uuid.uuid4(), role="user")
    listed = await plan_source_packs(subject="armdailynews.com\nyerevantimes.am", user=user, db=None)
    single = await plan_source_packs(subject="armdailynews.com", user=user, db=None)
    (option,) = listed.packs[0].options
    assert (option.key, option.default, [c.value for c in option.choices]) == ("ip_pivot", "shared", ["shared", "all"])
    assert single.packs[0].options == []


@pytest.mark.asyncio
async def test_the_frame_s_choice_reaches_the_turn(monkeypatch, ws_env):  # noqa: F811
    from app.api.v1 import chat as chat_module

    from .test_chat_web_steering import _send

    seen: list = []

    async def _run_turn(_db, *, pack_options=None, **_kw):
        seen.append(pack_options)
        return "done"

    # The mode exists only behind ``DEEP_INVESTIGATION``: pinned on,
    # as the other route tests do, so the result does not depend on the env.
    monkeypatch.setattr(chat_module, "is_feature_enabled", lambda flag: flag == "DEEP_INVESTIGATION")
    monkeypatch.setattr(chat_module, "run_deep_investigation_turn", _run_turn)
    frame = {
        "content": "armdailynews.com\nyerevantimes.am",
        "mode": "deep_investigation",
        "source_pack_slugs": [DISINFO_INFRASTRUCTURE_SLUG],
        "source_pack_options": EVERY_IP,
    }
    await _send(ws_env, frame)
    assert seen == [EVERY_IP]


@pytest.mark.asyncio
async def test_a_frame_whose_choices_cannot_be_read_whole_starts_no_turn(monkeypatch, ws_env):  # noqa: F811
    from app.api.v1 import chat as chat_module

    from .test_chat_web_steering import _send

    seen: list = []

    async def _run_turn(_db, *, pack_options=None, **_kw):
        seen.append(pack_options)
        return "done"

    monkeypatch.setattr(chat_module, "is_feature_enabled", lambda flag: flag == "DEEP_INVESTIGATION")
    monkeypatch.setattr(chat_module, "run_deep_investigation_turn", _run_turn)
    frame = {
        "content": "armdailynews.com",
        "mode": "deep_investigation",
        "source_pack_slugs": [DISINFO_INFRASTRUCTURE_SLUG],
        # One more than any pack offers: dropping the rest would run them at
        # their defaults, a step switched off included.
        "source_pack_options": {
            DISINFO_INFRASTRUCTURE_SLUG: {f"k{i}": "off" for i in range(chat_module.MAX_PACK_OPTIONS + 1)}
        },
    }
    conn = await _send(ws_env, frame)
    assert seen == []
    assert conn.sent == []
    assert (conn.refusal.status_code, conn.refusal.detail) == (
        422,
        "The investigation's choices could not be read. Confirm the sources again.",
    )

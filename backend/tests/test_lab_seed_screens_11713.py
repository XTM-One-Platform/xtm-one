"""The lab seed of the screens a UX round reviews (#11713): CTEM cases, five scored stages and a live funnel
(``[service.lab-ctem-screens]``), one video with a ready render in every XTM One lab, and an opt-in PIR list
with shared names. ``lab_asgi`` is loaded with a stand-in for ``app.main`` (as test_lab_seed_declarations.py
does); ``lab.py`` is read from its syntax tree. dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
import tomllib
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB_DIR = ROOT / ".xtm-one" / "lab"
CHECKS = ROOT / ".xtm-one" / "checks.toml"
LAB = LAB_DIR / "lab.py"
DOCS = ROOT / "dev-docs" / "reference" / "agentic-labs.md"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not LAB.is_file(), reason="no .xtm-one next to the backend")


def _lab_asgi(monkeypatch):
    async def backend(scope, receive, send):
        return None

    monkeypatch.setitem(sys.modules, "app.main", types.SimpleNamespace(application=backend))
    monkeypatch.syspath_prepend(str(LAB_DIR))
    spec = importlib.util.spec_from_file_location("lab_asgi_screens_under_test", LAB_DIR / "lab_asgi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample():
    spec = importlib.util.spec_from_file_location("lab_ctem_sample_screens", LAB_DIR / "ctem_sample.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _functions() -> dict[str, ast.FunctionDef]:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def test_the_screens_posture_scores_every_stage_above_zero_with_a_live_funnel(monkeypatch):
    from app.api.v1 import ctem_metrics
    from app.services.ctem_scoring import compute_posture

    module = _lab_asgi(monkeypatch)
    original = ctem_metrics.build_posture_for_user
    assert module.screens_ctem_posture({}) is False
    assert ctem_metrics.build_posture_for_user is original

    async def live(db, user_id, vertical="cyber"):
        return {"scoping": {}}, set()

    monkeypatch.setattr(ctem_metrics, "build_posture_for_user", live)
    assert module.screens_ctem_posture({"LAB_CTEM_SCREENS": "on"}) is True
    facts, present = asyncio.run(ctem_metrics.build_posture_for_user(None, "u", "cyber"))
    assert present == {"opencti", "openaev"}
    stages = compute_posture(facts, present)["stages"]
    assert [s["key"] for s in stages if s["score"]] == [
        "scoping",
        "discovery",
        "prioritization",
        "validation",
        "remediation",
    ]
    funnel = facts["prioritization"]["funnel"]
    assert "funnel_inputs" not in facts["prioritization"] and "funnel_tailored" not in facts["prioritization"]
    live_layers = [layer for layer in funnel["layers"] if layer["state"] == "live"]
    assert [layer["key"] for layer in live_layers] == ["known", "relevant", "exploitable"]
    counts = [layer["count"] for layer in live_layers]
    assert counts == sorted(counts, reverse=True) and all(counts) and not funnel["capped"]
    assert funnel["needs_action"] == counts[-1]
    # Another vertical keeps the live collection; each read is a fresh copy.
    assert asyncio.run(ctem_metrics.build_posture_for_user(None, "u", "fimi")) == ({"scoping": {}}, set())
    again, _ = asyncio.run(ctem_metrics.build_posture_for_user(None, "u", "cyber"))
    assert again == facts and again is not facts


def test_the_screens_lab_is_a_ctem_sample_lab_left_out_of_the_large_facts_and_the_kept_today(monkeypatch):
    module = _lab_asgi(monkeypatch)
    assert "LAB_CTEM_SCREENS" in module.CTEM_SAMPLE_SWITCHES
    assert module.large_ctem_facts({"LAB_CTEM_POSTURE_HISTORY": "1", "LAB_CTEM_SCREENS": "on"}) is False
    assert module.keep_seeded_posture_today({"LAB_CTEM_POSTURE_HISTORY": "1", "LAB_CTEM_SCREENS": "on"}) is False
    guard = ast.unparse(_functions()["seeds_ctem_large_facts"])
    assert "'LAB_CTEM_SCREENS'" in guard


def test_the_screens_issues_group_into_at_least_three_cases_with_stable_keys(monkeypatch):
    from app.services import ctem_cases

    module = _lab_asgi(monkeypatch)
    now = datetime(2026, 11, 14, tzinfo=UTC)
    issues = module.screens_source_issues(now)
    assert len({i.member_id for i in issues}) == len(issues)
    grouping = ctem_cases.group_issues(issues)
    assert len(grouping.groups) == module.ctem_sample.SCREENS_CASES >= 3
    kinds = {r.kind for r in grouping.remediations.values()}
    assert kinds == {"upgrade", "course_of_action"}
    # R3: the OpenAEV finding joins the FortiOS case by its CVE; the weak cipher has no known fix.
    forti = next(k for k in grouping.groups if k.startswith("upgrade:fortinet/fortios@"))
    assert "openaev:finding-lab-0001" in {i.member_id for i in grouping.groups[forti]}
    assert [i.member_id for i in grouping.no_known_fix] == ["openaev:finding-lab-0002"]
    assert ctem_cases.group_issues(module.screens_source_issues(now)).groups.keys() == grouping.groups.keys()
    assert all(i.first_seen_at and i.first_seen_at < now for i in issues)


def test_the_screens_cases_source_read_answers_the_fixture_only_when_asked(monkeypatch):
    from app.services import ctem_cases_source

    module = _lab_asgi(monkeypatch)
    original = ctem_cases_source.read_open_issues
    assert module.screens_ctem_cases({}) is False
    assert ctem_cases_source.read_open_issues is original
    monkeypatch.setattr(ctem_cases_source, "read_open_issues", original)
    assert module.screens_ctem_cases({"LAB_CTEM_SCREENS": "on"}) is True
    read = asyncio.run(ctem_cases_source.read_open_issues(None, "u", "cyber", members=["opencti:gone"]))
    assert read.missing == [] and read.complete and read.closed == []
    assert len(read.issues) == len(module.ctem_sample.SCREENS_ISSUES)
    assert asyncio.run(ctem_cases_source.read_open_issues(None, "u", "fimi")).issues == []


def test_the_screens_source_stub_takes_every_keyword_of_the_real_read(monkeypatch):
    """#2081: the recompute passes ``remap=True``; a stub without it answered 500 and seeded no case."""
    import inspect

    from app.services import ctem_cases_source

    module = _lab_asgi(monkeypatch)
    original = ctem_cases_source.read_open_issues
    monkeypatch.setattr(ctem_cases_source, "read_open_issues", original)
    assert module.screens_ctem_cases({"LAB_CTEM_SCREENS": "on"}) is True
    stub = ctem_cases_source.read_open_issues

    def keywords(function):
        return {
            name
            for name, p in inspect.signature(function).parameters.items()
            if p.kind is inspect.Parameter.KEYWORD_ONLY
        }

    assert keywords(original) <= keywords(stub), keywords(original) - keywords(stub)
    read = asyncio.run(stub(None, "u", "cyber", members=["opencti:gone"], remap=True))
    assert len(read.issues) == len(module.ctem_sample.SCREENS_ISSUES)


def test_the_peer_reads_the_ivanti_case_without_one_member_so_its_aggregates_are_restricted(monkeypatch):
    from types import SimpleNamespace

    from app.services import ctem_cases_source, ctem_cases_view

    module = _lab_asgi(monkeypatch)
    peer = next(
        node.value.value
        for node in ast.parse(LAB.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "PEER_EMAIL"
    )
    hidden = module.ctem_sample.SCREENS_RESTRICTED[peer]
    monkeypatch.setattr(ctem_cases_source, "read_open_issues", ctem_cases_source.read_open_issues)
    assert module.screens_ctem_cases({"LAB_CTEM_SCREENS": "on"}) is True
    emails = {"admin": "lab@filigran.io", "peer": peer}
    original_email = module.screens_viewer_email

    async def email(db, user_id):
        return emails[user_id]

    monkeypatch.setattr(module, "screens_viewer_email", email)
    admin = asyncio.run(ctem_cases_source.read_open_issues(object(), "admin", "cyber"))
    peer_read = asyncio.run(ctem_cases_source.read_open_issues(object(), "peer", "cyber"))
    assert len(admin.issues) == len(module.ctem_sample.SCREENS_ISSUES)
    assert {i.member_id for i in admin.issues} - {i.member_id for i in peer_read.issues} == set(hidden)
    # The real list row: the peer sees the case through its other member, its detail aggregates are null.
    ivanti = [i for i in admin.issues if i.vendor == "Ivanti"]
    rows = [SimpleNamespace(source=i.source, external_id=i.external_id, open=True) for i in ivanti]
    for reader, restricted in ((admin, False), (peer_read, True)):
        view = ctem_cases_view.ViewerView(issues={i.member_id: i for i in reader.issues})
        visible = ctem_cases_view.visible_members(rows, view)
        without = sum(1 for r in rows if ctem_cases_view._member_id(r) not in view.issues)
        assert bool(visible) and bool(without) is restricted
    # Unreadable viewer (no session): the whole sample.
    assert asyncio.run(original_email(None, "x")) is None


def test_the_lab_recomputes_the_screens_cases_and_reads_them_back():
    functions = _functions()
    helper = ast.unparse(functions["seed_ctem_screens"]) + ast.unparse(functions["_ctem_screens_recompute"])
    for part in (
        "'POST', '/ctem-cases/recompute?vertical=cyber'",
        "/ctem-cases?vertical=cyber",
        "CTEM_SCREENS_RECOMPUTE_BUSY",
        "len(cases) < 3",
        "/ctem-metrics/posture?vertical=cyber",
        "len(scored) != 5",
        "/ctem/cases",
    ):
        assert part in helper, part
    # #1529: the scope is declared before the recompute, else every issue is a watchlisted signal.
    seeded_screens = ast.unparse(functions["seed_ctem_screens"])
    assert seeded_screens.index("seed_ctem_screens_scope(token)") < seeded_screens.index(
        "_ctem_screens_recompute(token)"
    )
    scope = ast.unparse(functions["seed_ctem_screens_scope"])
    for part in ("'PUT', '/organization-context'", "/organization-context/assets/records/batch", "SCREENS_ASSETS"):
        assert part in scope, part
    seed = ast.unparse(functions["seed_entities"])
    assert "lab_switch('LAB_CTEM_SCREENS', LAB_CTEM_SCREENS_STATES) == 'on'" in seed
    assert "seeded.update(seed_ctem_screens(token, base))" in seed
    assert "skipped['CTEM cases and scored stages']" in seed


def _recompute_helper(answers: list) -> tuple:
    """``_ctem_screens_recompute`` alone, its ``api`` answering *answers* in order (an exception is raised)."""
    calls: list[tuple[str, str]] = []
    waits: list[float] = []

    def api(method, path, token=None, body=None):
        calls.append((method, path))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    namespace = {"api": api, "log": lambda message: None, "time": types.SimpleNamespace(sleep=waits.append)}
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    wanted = {
        "CTEM_SCREENS_RECOMPUTE_WAIT",
        "CTEM_SCREENS_RECOMPUTE_ATTEMPTS",
        "CTEM_SCREENS_RECOMPUTE_BUSY",
        "_ctem_screens_recompute",
    }
    nodes = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in wanted)
        or (isinstance(node, ast.Assign) and {getattr(t, "id", "") for t in node.targets} & wanted)
    ]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - lab.py's own code
    return namespace["_ctem_screens_recompute"], calls, waits


def test_the_screens_recompute_waits_out_a_taken_slot_and_recomputes_again():
    """Gap lab-ctem-screens-cases-seed-flaky: a 429 before the scope existed left a twin lab with 0 cases."""
    three = {"cases": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
    recompute, calls, waits = _recompute_helper(
        [RuntimeError("POST /ctem-cases/recompute: 429 One recompute per minute"), {"applied": True}, three]
    )
    assert recompute("t") == three
    assert [m for m, _ in calls] == ["POST", "POST", "GET"]
    assert waits == [62]

    recompute, calls, waits = _recompute_helper([{"applied": True}, {"cases": []}, {"applied": True}, three])
    assert recompute("t") == three
    assert waits == [62]

    recompute, calls, waits = _recompute_helper([{"applied": True}, three])
    assert recompute("t") == three
    assert waits == []

    recompute, _, _ = _recompute_helper([{"applied": False, "missing": ["opencti"]}])
    with pytest.raises(RuntimeError, match="recompute answered"):
        recompute("t")

    recompute, calls, waits = _recompute_helper([RuntimeError("x: 429 ")] * 4 + [{"cases": []}])
    assert recompute("t") == {"cases": []}
    assert waits == [62, 62, 62]
    assert calls[-1] == ("GET", "/ctem-cases?vertical=cyber&limit=50")

    # Any other error is not waited out.
    recompute, _, waits = _recompute_helper([RuntimeError("POST /ctem-cases/recompute: 500 boom")])
    with pytest.raises(RuntimeError, match=": 500 "):
        recompute("t")
    assert waits == []


def test_the_screens_recompute_waits_out_another_running_recompute():
    """#2030: since #1953 a recompute already running answers 409; the seed waits it out like a 429."""
    three = {"cases": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
    recompute, calls, waits = _recompute_helper(
        [
            RuntimeError("POST /ctem-cases/recompute: 409 A recompute of the cases is already running"),
            RuntimeError("POST /ctem-cases/recompute: 429 One recompute per minute"),
            {"applied": True},
            three,
        ]
    )
    assert recompute("t") == three
    assert [m for m, _ in calls] == ["POST", "POST", "POST", "GET"]
    assert waits == [62, 62]


def test_the_screens_scope_puts_every_fixable_issue_on_a_critical_asset(monkeypatch):
    """#1529: each screens issue with a fix resolves to a stack product run by a ``very_high`` asset."""
    from app.services import ctem_cases
    from app.services.ctem_scope_gate import ASSET_TO_SCOPE, CRITICALITY_ORDER
    from app.services.organization_context import AssetEntry

    sample = _sample()
    module = _lab_asgi(monkeypatch)
    stack = {e["product"] for e in sample.SCREENS_TECH_STACK}
    assets = [AssetEntry.model_validate(row) for row in sample.SCREENS_ASSETS]
    threshold = CRITICALITY_ORDER.index("high")
    grouping = ctem_cases.group_issues(module.screens_source_issues(datetime(2026, 11, 14, tzinfo=UTC)))
    products = {i.product for members in grouping.groups.values() for i in members if i.product}
    assert products and products <= stack
    for product in products:
        running = [a for a in assets if product in a.products]
        assert running, product
        assert max(CRITICALITY_ORDER.index(ASSET_TO_SCOPE[a.criticality]) for a in running) >= threshold
    assert len({a.external_id for a in assets}) == len(assets)


def test_the_screens_labs_are_the_default_lab_with_the_switch():
    services = tomllib.loads(CHECKS.read_text(encoding="utf-8"))["service"]
    lab = services["lab"]
    twins = {
        "lab-ctem-screens": {"LAB_CTEM_SCREENS": "on"},
        "lab-ctem-screens-light": {"LAB_CTEM_SCREENS": "on", "LAB_THEME": "light"},
        "lab-pirs-same-name": {"LAB_OPENCTI_PIRS_SAME_NAME": "on"},
    }
    for name, switches in twins.items():
        service = services[name]
        assert service["env"] == {**lab["env"], **switches}, name
        own = ("env", "description")
        assert {k: v for k, v in service.items() if k not in own} == {k: v for k, v in lab.items() if k not in own}, (
            name
        )
    for switch in ("LAB_CTEM_SCREENS", "LAB_OPENCTI_PIRS_SAME_NAME"):
        holders = {n for n, s in services.items() if switch in s.get("env", {})}
        # The organization context labs (#83) are the screens lab with LAB_ORG_CONTEXT: test_lab_seed_org_context_83.
        context = {n for n in holders if n.startswith("lab-ctem-context-")} if switch == "LAB_CTEM_SCREENS" else set()
        assert holders - context == {n for n, s in twins.items() if switch in s}, switch


def test_pir_names_are_unique_unless_the_same_name_switch_is_on(monkeypatch):
    module = _lab_asgi(monkeypatch)
    rows = module.lab_pirs({})
    assert len({r["name"] for r in rows}) == len(rows) == len(module.LAB_OPENCTI_PIRS)
    both = module.lab_pirs({"LAB_OPENCTI_PIRS_SAME_NAME": "on"})
    assert len({r["id"] for r in both}) == len(both) == len(rows) + 2
    names = [r["name"] for r in both]
    shared = {n for n in names if names.count(n) > 1}
    assert len(shared) == 2 and shared <= {r["name"] for r in rows}


def test_the_pir_stub_answers_the_same_name_pair_when_asked(monkeypatch):
    from app.services import ctem_metrics_collector, organization_context, organization_context_draft

    module = _lab_asgi(monkeypatch)
    original = organization_context_draft.list_pirs

    async def user_creds(db, user_id, provider):
        return ("https://opencti.lab.invalid", {})

    async def current_version(db, organization_id=None):
        return None

    monkeypatch.setattr(ctem_metrics_collector, "_user_creds", user_creds)
    monkeypatch.setattr(organization_context, "current_version", current_version)
    monkeypatch.setattr(organization_context_draft, "list_pirs", original)
    assert module.stub_opencti_pirs({"LAB_OPENCTI_PIRS": "1", "LAB_OPENCTI_PIRS_SAME_NAME": "on"}) is True
    rows, unavailable, _ = asyncio.run(organization_context_draft.list_pirs(None, user_id="admin"))
    assert unavailable is None and len(rows) == len(module.LAB_OPENCTI_PIRS) + 2


def _ready_render_seed():
    spec = importlib.util.spec_from_file_location("lab_seed_ready_render", LAB_DIR / "seed_video_ready_render.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_ready_render_seed_stores_the_committed_mp4_and_its_poster_with_no_ffmpeg():
    module = _ready_render_seed()
    mp4, poster = module.fixtures()
    head = mp4.read_bytes()[:64]
    # An ISO MP4 with its moov first (faststart), under 100 KB with its poster: the player plays it at once.
    assert head[4:8] == b"ftyp" and b"moov" in mp4.read_bytes()[:4096]
    assert poster.read_bytes()[:3] == b"\xff\xd8\xff"
    assert mp4.stat().st_size + poster.stat().st_size < 100_000
    from app.services.video_studio.spec import QUALITY_PRESETS, VideoSpec

    assert module.QUALITY in QUALITY_PRESETS
    assert VideoSpec.model_validate(module.spec()).title == module.VIDEO_TITLE
    source = ast.unparse(ast.parse((LAB_DIR / "seed_video_ready_render.py").read_text(encoding="utf-8")))
    # No ffmpeg: the python-dev image of every lab has none (#11713 retest).
    assert "ffmpeg_path" not in source and "subprocess" not in source
    # Found again on a restart: the existing video with a current render is answered, nothing written twice.
    assert source.index("found.latest_render_id is not None") < source.index("mp4, poster = fixtures()")
    assert "status=JOB_READY" in source and "found.latest_render_id = render.id" in source


def test_a_missing_fixture_raises_naming_it(monkeypatch, tmp_path):
    module = _ready_render_seed()
    monkeypatch.setattr(module, "VIDEO_FIXTURE", tmp_path / "gone.mp4")
    with pytest.raises(FileNotFoundError, match=r"gone\.mp4"):
        module.fixtures()


def test_every_lab_seeds_the_ready_render_and_reads_it_back():
    functions = _functions()
    helper = ast.unparse(functions["seed_video_ready_render"])
    for part in (
        "'seed_video_ready_render.py'",
        "sys.executable",
        "env=seed_script_env()",
        "ready_render",
        "stderr_summary(process.stderr)",
        "/videos/",
    ):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seeded.update(seed_video_ready_render(token, base))" in seed
    # A failed seed stops the lab start with its error named; only a Community lab (no Video Studio) skips it.
    call = seed.index("seeded.update(seed_video_ready_render(token, base))")
    assert "raise SystemExit(f'lab: video ready render not seeded: " in seed[call : call + 400]
    assert "if lab_switch('LAB_EDITION', LAB_EDITIONS) != 'community':" in seed[call - 200 : call]
    assert "'LAB_EDITION=community: the Video Studio is Enterprise Edition'" in seed


def test_the_screens_seed_is_documented():
    text = DOCS.read_text(encoding="utf-8")
    for part in (
        "[service.lab-ctem-screens]",
        "LAB_CTEM_SCREENS",
        "LAB_OPENCTI_PIRS_SAME_NAME",
        "seed_video_ready_render.py",
    ):
        assert part in text, part


def test_the_sample_funnels_read_the_profile_assets_reachability(monkeypatch):
    """#103: the screens and module samples pass the profile's reachability split to ``_attach_funnel``."""
    from app.api.v1 import ctem_metrics
    from app.services import ctem_metrics_collector

    module = _lab_asgi(monkeypatch)
    split = {"total": 3, "reachable": 1, "unreachable": 1, "unknown": 1, "endpoints": 0}
    rows = {"value": None}

    async def asset_rows(db):
        return rows["value"]

    monkeypatch.setattr(ctem_metrics_collector, "_profile_asset_rows", asset_rows)
    monkeypatch.setattr(ctem_metrics_collector, "_profile_reachability", lambda r: dict(split) if r else None)

    async def live(db, user_id, vertical="cyber"):
        return {}, set()

    def exposed(facts):
        return next(layer for layer in facts["prioritization"]["funnel"]["layers"] if layer["key"] == "exposed")

    for switch in ({"LAB_CTEM_SCREENS": "on"}, {"LAB_CTEM_MODULES": "cti-aev"}):
        monkeypatch.setattr(ctem_metrics, "build_posture_for_user", live)
        if "LAB_CTEM_SCREENS" in switch:
            assert module.screens_ctem_posture(switch) is True
        else:
            assert module.modules_ctem_posture(switch) is True
        rows["value"] = None
        facts, _ = asyncio.run(ctem_metrics.build_posture_for_user(None, "u", "cyber"))
        assert exposed(facts)["state"] == "not_configured", switch
        rows["value"] = ([object()], [])
        facts, _ = asyncio.run(ctem_metrics.build_posture_for_user(None, "u", "cyber"))
        layer = exposed(facts)
        assert layer["state"] == "not_available" and layer["reason"] == "assets_not_linked", (switch, layer)
        assert layer["reachability"] == split, switch
    assert "lab_asgi.profile_reachability" in DOCS.read_text(encoding="utf-8")

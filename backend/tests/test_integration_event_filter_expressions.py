"""Filter expressions on integration-event triggers.

Operators per key (``eq``, ``ne``, ``in``, ``not_in``, ``starts_with``,
``ends_with``, ``contains``, ``matches``, ``exists``), the ``all`` / ``any`` /
``not`` combinators, the save-time validation, the GitHub head-PR enrichment
and the backward compatibility of every filter shape saved before.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.v1 import webhooks
from app.api.v1.schemas.assignment import CreateAssignmentRequest, UpdateAssignmentRequest
from app.integrations.event_enrichment import event_enricher
from app.integrations.providers.github import event_enrichment as gh_enrichment
from app.services import event_enrichment
from app.services.builtin_tools.agent_builder import platform_event_trigger_error
from app.services.integration_event_filters import (
    event_filter_reason,
    filter_expression_error,
    needs_enrichment,
    payload_filter_reason,
    reason_label,
    trigger_filter_error,
)

ME = "SamuelHassine"
DESK_REPOS = ["XTM-One-Platform/xtm-one", "FiligranHQ/platform"]


def _pr(number: int, login: str, sha: str, *, state: str = "open", draft: bool = False, labels=()) -> dict:
    return {
        "number": number,
        "state": state,
        "draft": draft,
        "user": {"login": login},
        "author_association": "MEMBER",
        "labels": [{"name": n} for n in labels],
        "base": {"ref": "main", "repo": {"full_name": DESK_REPOS[0]}},
        "head": {"ref": f"fix/{number}", "sha": sha, "repo": {"full_name": DESK_REPOS[0]}},
    }


def _workflow_run(*, actor: str, sha: str = "a" * 40, conclusion: str = "failure", pulls=None) -> dict:
    """A recorded-shape GitHub ``workflow_run.completed`` delivery."""
    return {
        "action": "completed",
        "workflow_run": {
            "id": 1,
            "name": "CI",
            "head_branch": "fix/x",
            "head_sha": sha,
            "conclusion": conclusion,
            "actor": {"login": actor},
            "pull_requests": pulls if pulls is not None else [],
        },
        "repository": {"full_name": DESK_REPOS[0]},
        "sender": {"login": actor},
    }


# -- operators ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("operators", "value", "holds"),
    [
        ({"eq": "a"}, "a", True),
        ({"eq": "a"}, "b", False),
        ({"ne": "a"}, "b", True),
        ({"ne": "a"}, "a", False),
        ({"in": ["a", "b"]}, "b", True),
        ({"in": ["a", "b"]}, "c", False),
        ({"not_in": ["a", "b"]}, "c", True),
        ({"not_in": ["a", "b"]}, "a", False),
        ({"starts_with": "feat/"}, "feat/x", True),
        ({"starts_with": ["feat/", "fix/"]}, "fix/y", True),
        ({"starts_with": ["feat/", "fix/"]}, "main", False),
        ({"ends_with": "[bot]"}, "dependabot[bot]", True),
        ({"ends_with": "[bot]"}, "someone", False),
        ({"contains": "deps"}, "chore(deps): bump", True),
        ({"contains": "deps"}, "feat: x", False),
        ({"contains": "ui"}, ["backend", "ui"], True),
        ({"contains": "ui"}, ["backend"], False),
        ({"matches": r"v\d+\.\d+"}, "v1.2", True),
        ({"matches": r"v\d+\.\d+"}, "v1.2-rc", False),
        ({"exists": True}, "x", True),
        ({"exists": True}, None, False),
        ({"exists": False}, None, True),
        ({"ne": "a", "not_in": ["b"]}, "c", True),
        ({"ne": "a", "not_in": ["b"]}, "b", False),
        ({"starts_with": 1}, "1x", False),
        ({"ends_with": "x"}, 5, False),
    ],
    ids=lambda v: repr(v),
)
def test_each_operator(operators, value, holds):
    assert (payload_filter_reason({"f": value}, {"f": operators}) is None) is holds


@pytest.mark.parametrize(
    ("operators", "holds"),
    [
        ({"ne": "a"}, True),
        ({"not_in": ["a"]}, True),
        ({"exists": False}, True),
        ({"eq": "a"}, False),
        ({"in": ["a"]}, False),
        ({"starts_with": "a"}, False),
        ({"contains": "a"}, False),
        ({"matches": "a"}, False),
        ({"exists": True}, False),
    ],
    ids=lambda v: repr(v),
)
def test_an_absent_field(operators, holds):
    assert (payload_filter_reason({"other": 1}, {"f.g": operators}) is None) is holds


def test_a_drop_names_the_key_and_the_operator():
    reason = payload_filter_reason({"sender": {"login": ME}}, {"sender.login": {"ne": ME}})
    assert reason == "sender.login fails ne"
    assert payload_filter_reason({}, {"sender.login": {"eq": ME}}) == "the event has no sender.login"


def test_a_backtracking_pattern_is_bounded_in_time():
    import time

    started = time.monotonic()
    # Refused at save, and bounded anyway when it reaches the evaluator.
    assert payload_filter_reason({"f": "a" * 4000 + "!"}, {"f": {"matches": "^(a|aa)+$"}}) is not None
    assert time.monotonic() - started < 2.0


def test_many_patterns_share_one_time_budget(monkeypatch):
    import time

    from app.services import integration_event_filters as filters

    monkeypatch.setattr(filters, "MATCH_BUDGET_SECONDS", 0.05)
    branches = [{"f": {"matches": f"^(a|aa)+{i}$"}} for i in range(40)]
    started = time.monotonic()
    assert payload_filter_reason({"f": "a" * 4000 + "!"}, {"any": branches}) is not None
    assert time.monotonic() - started < 1.0
    assert "16" in (filter_expression_error({"any": [{"f": {"matches": f"x{i}"}} for i in range(17)]}) or "")
    assert filter_expression_error({"any": [{"f": {"matches": f"x{i}"}} for i in range(16)]}) is None


def test_a_field_named_not_is_filtered_with_an_operator_object():
    assert payload_filter_reason({"not": 1}, {"not": {"eq": 1}}) is None
    assert payload_filter_reason({"not": 2}, {"not": {"eq": 1}}) is not None


@pytest.mark.parametrize(
    ("flt", "payload", "passes"),
    [
        ({"not": {"x": 1}}, {"not": {"x": 1}}, True),
        ({"not": {"x": 1}}, {"not": {"x": 2}}, False),
        ({"all": [{"x": 1}]}, {"all": {"x": 1}}, True),
        ({"all": [{"x": 1}]}, {"all": {"x": 2}}, False),
        ({"any": [{"x": 1}]}, {"any": {"x": 1}}, True),
    ],
    ids=lambda v: str(v)[:30],
)
def test_a_payload_field_named_like_a_combinator_keeps_its_legacy_meaning(flt, payload, passes):
    assert (payload_filter_reason(payload, flt) is None) is passes
    assert (payload_filter_reason(payload, flt) is None) is (_legacy_reason(payload, flt) is None)


def test_a_character_class_is_not_a_nested_alternation():
    assert filter_expression_error({"f": {"matches": "([|])+"}}) is None
    assert filter_expression_error({"f": {"matches": "([+*]x)+"}}) is None
    assert filter_expression_error({"f": {"matches": "(a|b)+"}}) is not None
    # An escaped bracket opens no class: the nested repetition is still seen.
    assert filter_expression_error({"f": {"matches": r"([\|a+)+[x]"}}) is not None
    assert filter_expression_error({"f": {"matches": r"(\[a+)+"}}) is not None
    assert filter_expression_error({"f": {"matches": "([]|])+"}}) is None


def test_a_leading_inline_flag_is_honoured():
    flt = {"f": {"matches": "(?i)^foo$"}}
    assert filter_expression_error(flt) is None
    assert payload_filter_reason({"f": "FOO"}, flt) is None
    assert payload_filter_reason({"f": "bar"}, flt) is not None
    assert payload_filter_reason({"f": "xfoo"}, flt) is not None


def test_a_legacy_field_named_matches_is_not_counted_as_patterns():
    values = [f"v{i}" for i in range(20)]
    assert filter_expression_error({"matches": values}) is None
    assert filter_expression_error({"any": [{"matches": values}]}) is None
    assert filter_expression_error({"x": {"matches": [f"p{i}" for i in range(17)]}}) is not None


@pytest.mark.parametrize(
    "flt",
    [
        {"not": {"enriched.pull_request.user.login": "bot"}},
        {"any": [{"action": "nope"}, {"enriched.pull_request.user.login": {"ne": "x"}}]},
        {"enriched.pull_request": {"exists": False}},
        {"enriched.pull_request.user.login": {"not_in": ["x"]}},
    ],
    ids=lambda f: str(f)[:40],
)
def test_an_unreadable_enrichment_never_widens_a_filter(flt):
    payload = {"action": "completed", "enriched": {"unavailable": "HTTP 502"}}
    reason = payload_filter_reason(payload, flt)
    assert reason is not None and "could not be read" in reason


def test_a_long_text_is_never_matched():
    assert payload_filter_reason({"f": "a" * 5000}, {"f": {"matches": "a*"}}) is not None


def test_list_paths_map_over_their_elements():
    payload = {"pull_request": {"labels": [{"name": "bug"}, {"name": "ui"}]}}
    assert payload_filter_reason(payload, {"pull_request.labels[].name": {"contains": "ui"}}) is None
    assert payload_filter_reason(payload, {"pull_request.labels[].name": {"contains": "docs"}}) is not None


@pytest.mark.parametrize(
    "wanted",
    ["agent-a", ["agent-a"], ["agent-z", "agent-a"]],
    ids=["scalar", "list", "list-any-of"],
)
def test_a_plain_value_matches_an_element_of_a_list_field(wanted):
    """Fleet_impact.targets = "<id>" (or ["<id>"]) matches targets ["<id>", ...]."""
    payload = {"fleet_impact": {"targets": ["agent-a", "agent-b"], "kind": "assignment"}}
    assert payload_filter_reason(payload, {"fleet_impact.targets": wanted}) is None
    other = {"fleet_impact": {"targets": ["agent-c"], "kind": "assignment"}}
    assert payload_filter_reason(other, {"fleet_impact.targets": wanted}) is not None


def test_a_list_field_matches_any_shared_element_and_drops_an_empty_one():
    payload = {"labels": ["bug", "ui"]}
    assert payload_filter_reason(payload, {"labels": ["bug", "ui"]}) is None
    assert payload_filter_reason({"labels": []}, {"labels": "bug"}) is not None
    assert payload_filter_reason({"labels": "bug"}, {"labels": "bug"}) is None


def test_operators_work_inside_a_nested_object():
    payload = {"workflow_run": {"head_branch": "feat/x", "conclusion": "failure"}}
    assert payload_filter_reason(payload, {"workflow_run": {"head_branch": {"starts_with": "feat/"}}}) is None
    assert payload_filter_reason(payload, {"workflow_run": {"head_branch": {"starts_with": "fix/"}}}) is not None


# -- combinators -------------------------------------------------------------


def test_all_any_and_not():
    payload = {"sender": {"login": "renovate[bot]"}, "action": "opened"}
    bot = {"sender.login": {"ends_with": "[bot]"}}
    assert payload_filter_reason(payload, {"not": bot}) == "not: the negated filter holds"
    assert payload_filter_reason({"sender": {"login": "alice"}}, {"not": bot}) is None
    assert payload_filter_reason(payload, {"all": [bot, {"action": "opened"}]}) is None
    assert payload_filter_reason(payload, {"all": [bot, {"action": "closed"}]}).startswith("all[1]:")
    assert payload_filter_reason(payload, {"any": [{"action": "closed"}, bot]}) is None
    assert payload_filter_reason(payload, {"any": [{"action": "closed"}, {"action": "edited"}]}).startswith("any:")


def test_combinators_nest_with_keys_at_the_same_level():
    flt = {
        "action": "completed",
        "any": [{"workflow_run.head_branch": {"starts_with": ["feat/", "fix/"]}}, {"not": {"sender.login": ME}}],
    }
    assert payload_filter_reason(_workflow_run(actor=ME), flt) is None
    other = _workflow_run(actor=ME)
    other["workflow_run"]["head_branch"] = "main"
    assert payload_filter_reason(other, flt) is not None
    other["sender"]["login"] = "alice"
    assert payload_filter_reason(other, flt) is None


# -- backward compatibility (the invariant) ----------------------------------

#: Every filter shape saved before expressions, as the fleet's assignments hold them.
_LEGACY_FILTERS = [
    {
        "action": "completed",
        "workflow_run.conclusion": "failure",
        "workflow_run.actor.login": ME,
        "repository.full_name": DESK_REPOS,
    },
    {"action": ["synchronize", "edited", "closed"], "pull_request.user.login": ME, "repository.full_name": DESK_REPOS},
    {"workflow_run": {"conclusion": ["failure", "timed_out"]}},
    {"a.b": "literal"},
    {"x": [None]},
    {"scope": {"hubspot_object": ["deal"]}, "channels": ["t:c"]},
    {"all": "a plain value under a key named all"},
    {"not": ["a", "list", "under", "not"]},
]

_LEGACY_PAYLOADS = [
    _workflow_run(actor=ME),
    _workflow_run(actor="alice"),
    _workflow_run(actor=ME, conclusion="success"),
    {"action": "edited", "pull_request": {"user": {"login": ME}}, "repository": {"full_name": DESK_REPOS[1]}},
    {"action": "edited", "pull_request": {"user": {"login": "bob"}}, "repository": {"full_name": DESK_REPOS[1]}},
    {"a.b": "literal", "a": {"b": "nested"}},
    {"x": "value"},
    {"x": None},
    {"all": "a plain value under a key named all", "not": ["a", "list", "under", "not"]},
    {},
]


def _legacy_reason(payload, flt):
    """The payload filter as it was before expressions (subset equality, list any of, dotted paths).

    With the one deliberate change: a plain value against a list-valued
    field holds when it is one of the list's elements (it never matched before,
    which is the bug); every non-list field keeps its old result.
    """
    missing = object()

    def plain(actual, expected):
        return actual == expected or (isinstance(actual, list) and expected in actual)

    def value(obj, key):
        if not isinstance(obj, dict):
            return missing
        if key in obj:
            return obj[key]
        if "." not in key:
            return missing
        cur = obj
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return missing
            cur = cur[part]
        return cur

    def matches(actual, expected):
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(matches(value(actual, str(k)), v) for k, v in expected.items())
        if isinstance(expected, list):
            return any(matches(actual, o) if isinstance(o, dict) else plain(actual, o) for o in expected)
        return actual is not missing and plain(actual, expected)

    for key, expected in flt.items():
        if key in ("scope", "channels"):
            continue
        actual = value(payload, key)
        if actual is missing or not matches(actual, expected):
            return "dropped"
    return None


@pytest.mark.parametrize("flt", _LEGACY_FILTERS, ids=lambda f: ",".join(f))
@pytest.mark.parametrize("payload", _LEGACY_PAYLOADS, ids=lambda p: str(sorted(p))[:40])
def test_a_filter_saved_before_keeps_its_result_on_every_event(flt, payload):
    assert (payload_filter_reason(payload, flt) is None) is (_legacy_reason(payload, flt) is None)


@pytest.mark.parametrize("flt", _LEGACY_FILTERS, ids=lambda f: ",".join(f))
def test_a_filter_saved_before_is_still_accepted(flt):
    assert filter_expression_error(flt) is None
    assert not needs_enrichment(flt)


def test_the_provider_family_filters_keep_their_keys():
    mail = {"from": "ceo@example.com", "subject": "Board"}
    assert event_filter_reason("new_email", mail, {"from": {"deny": ["example.com"]}}) == "sender is on the deny list"
    assert event_filter_reason("new_email", mail, {"from": {"allow": ["example.com"]}, "exclude_automated": True}) is None
    for flt in (
        {"exclude_automated": True, "from": {"deny": ["a@example.com"]}},
        {"folder": "sentitems", "from": {"allow": ["a@example.com"]}, "exclude_automated": True},
        {"scope": {"teams_channel": ["t:c"]}, "exclude_authors": ["Someone"]},
        {"change_kinds": ["created", "updated", "cancelled"]},
    ):
        assert filter_expression_error(flt) is None


# -- validation at save time -------------------------------------------------


@pytest.mark.parametrize(
    ("flt", "names"),
    [
        ({"sender.login": {"ne": ME, "nope": 1}}, ["sender.login", "nope"]),
        ({"sender.login": {"notIn": [ME]}}, ["sender.login", "notIn", "not_in"]),
        ({"sender.login": {"starts-with": "x"}}, ["sender.login", "starts_with"]),
        ({"sender.login": {"in": ME}}, ["sender.login", "in", "list"]),
        ({"sender.login": {"exists": "yes"}}, ["sender.login", "exists"]),
        ({"sender.login": {"matches": "("}}, ["sender.login", "matches"]),
        ({"sender.login": {"matches": "(a+)+$"}}, ["sender.login", "matches"]),
        ({"sender.login": {"matches": "^(a|aa)+$"}}, ["sender.login", "matches"]),
        ({"sender.login": {"matches": "a" * 300}}, ["sender.login", "256"]),
        ({"sender.login": {"starts_with": ""}}, ["sender.login", "starts_with"]),
        ({"sender.login": {"eq": {"a": 1}}}, ["sender.login", "eq"]),
        ({"any": []}, ["any"]),
        ({"not": [{"a": 1}]}, ["not"]),
        ({"Any": [{"a": 1}]}, ["Any", "any"]),
        ({"all": [{"x": {"ne": 1, "bad": 2}}]}, ["x", "bad"]),
        ("not an object", ["filter"]),
    ],
    ids=lambda v: repr(v)[:40],
)
def test_an_invalid_expression_is_refused_naming_the_key(flt, names):
    error = filter_expression_error(flt)
    assert error is not None
    for name in names:
        assert name in error


def test_validation_runs_where_assignments_are_saved():
    # An unknown key next to an operator; a plain object alone stays a nested subset.
    bad = {"provider": "github", "event": "workflow_run", "filter": {"sender.login": {"ne": ME, "nope": 1}}}
    assert trigger_filter_error("integration_event", {"filter": {"sender": {"nope": 1}}}) is None
    good = {"provider": "github", "event": "workflow_run", "filter": {"sender.login": {"ne": ME}}}
    assert trigger_filter_error("integration_event", bad) is not None
    assert trigger_filter_error("integration_event", good) is None
    assert trigger_filter_error("schedule", {"cron": "0 * * * *", "filter": {"x": {"ne": 1, "nope": 2}}}) is None
    assert "nope" in (platform_event_trigger_error("integration_event", bad) or "")
    base = {"agent_id": str(uuid.uuid4()), "name": "n", "prompt": "p", "trigger_type": "integration_event"}
    with pytest.raises(ValueError, match="nope"):
        CreateAssignmentRequest(**base, trigger_config=bad)
    CreateAssignmentRequest(**base, trigger_config=good)
    with pytest.raises(ValueError, match="nope"):
        UpdateAssignmentRequest(trigger_config=bad)
    UpdateAssignmentRequest(trigger_config=good)


def test_a_reason_is_one_bounded_line():
    assert reason_label("a\nb   c") == "a b c"
    assert len(reason_label("x" * 500)) == 160


# -- the GitHub enrichment ---------------------------------------------------


def test_the_github_enricher_is_registered_for_ci_and_push_events():
    enricher = event_enricher("github")
    assert enricher is not None
    for event in ("workflow_run.completed", "check_suite.completed", "check_run.completed", "status", "push"):
        assert enricher.applies(event, {})
    assert not enricher.applies("pull_request.synchronize", {})
    assert enricher.cache_key("workflow_run.in_progress", _workflow_run(actor=ME, sha="B" * 40)) == (
        f"{DESK_REPOS[0].lower()}@{'b' * 40}"
    )
    assert enricher.cache_key("push", {"after": "0" * 40, "repository": {"full_name": DESK_REPOS[0]}}) is None


def test_the_open_pull_request_of_the_head_is_picked():
    sha = "c" * 40
    pulls = [
        _pr(7, "alice", sha, state="closed"),
        _pr(9, ME, "d" * 40),
        _pr(12, "bob", sha),
        _pr(11, ME, sha, labels=["ui"]),
    ]
    named = gh_enrichment.facts_from_pulls(
        "workflow_run.completed", _workflow_run(actor="alice", pulls=[{"number": 12}]), pulls, sha
    )
    assert named["pull_request"]["number"] == 12
    assert [p["number"] for p in named["pull_requests"]] == [12, 11]
    unnamed = gh_enrichment.facts_from_pulls("workflow_run.completed", _workflow_run(actor="alice"), pulls, sha)
    assert unnamed["pull_request"]["number"] == 11
    assert unnamed["pull_request"]["user"]["login"] == ME
    assert unnamed["pull_request"]["labels"] == [{"name": "ui"}]
    assert gh_enrichment.facts_from_pulls("push", {}, [], sha) == {"pull_request": None, "pull_requests": []}


def _check(name: str, status: str = "completed", conclusion: str | None = "success") -> dict:
    """One item of GitHub's ``check_runs`` answer."""
    return {"id": 1, "name": name, "status": status, "conclusion": conclusion}


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        (
            [
                _check("CI"),
                _check("E2E", status="in_progress", conclusion=None),
                _check("Lint", status="queued", conclusion=None),
            ],
            {"total": 3, "completed": 1, "pending": 2, "failed": 0, "all_completed": False, "all_green": False},
        ),
        (
            [_check("CI"), _check("E2E", conclusion="failure"), _check("Docs", conclusion="cancelled")],
            {"total": 3, "completed": 3, "pending": 0, "failed": 2, "all_completed": True, "all_green": False},
        ),
        (
            [_check("CI"), _check("Video", conclusion="skipped"), _check("Aikido", conclusion="neutral")],
            {"total": 3, "completed": 3, "pending": 0, "failed": 0, "skipped": 1, "neutral": 1, "all_green": True},
        ),
        (
            [_check("A", conclusion="skipped"), _check("B", conclusion="skipped")],
            {"total": 2, "skipped": 2, "failed": 0, "all_completed": True, "all_green": True},
        ),
        ([], {"total": 0, "all_completed": False, "all_green": False}),
    ],
    ids=["pending", "failed", "green", "skipped-only", "no-check"],
)
def test_the_check_state_of_the_head(runs, expected):
    facts = gh_enrichment.check_facts(runs)
    assert {key: facts[key] for key in expected} == expected
    assert [n["name"] for n in facts["names"]] == [r["name"] for r in runs]


def test_a_completion_is_never_answered_from_an_earlier_read():
    enricher = event_enricher("github")
    sha = "e" * 40
    requested = {**_workflow_run(actor=ME, sha=sha), "action": "requested"}
    first = _workflow_run(actor=ME, sha=sha, conclusion="success")
    second = _workflow_run(actor=ME, sha=sha, conclusion="success")
    second["workflow_run"] = {**second["workflow_run"], "id": 2, "name": "E2E Tests"}
    rerun = _workflow_run(actor=ME, sha=sha, conclusion="success")
    rerun["workflow_run"] = {**rerun["workflow_run"], "run_attempt": 2}
    head = f"{DESK_REPOS[0].lower()}@{sha}"
    assert enricher.cache_key("workflow_run.requested", requested) == head
    keys = {enricher.cache_key("workflow_run.completed", p) for p in (first, second, rerun)}
    assert len(keys) == 3 and all(k.startswith(f"{head}#workflow_run:") for k in keys)
    # A redelivery of one completion reads once.
    assert enricher.cache_key("workflow_run.completed", first) == enricher.cache_key("workflow_run.completed", first)
    check_run = {
        "action": "completed",
        "check_run": {"id": 5, "head_sha": sha, "completed_at": "2026-11-04T22:00:00Z"},
        "repository": {"full_name": DESK_REPOS[0]},
    }
    assert enricher.cache_key("check_run.completed", check_run) != head
    status = {"id": 9, "sha": sha, "state": "pending", "repository": {"full_name": DESK_REPOS[0]}}
    assert enricher.cache_key("status", status) == head
    assert enricher.cache_key("status", {**status, "state": "success"}) != head


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


@pytest.mark.asyncio
async def test_the_fetch_reads_every_page_of_check_runs(monkeypatch):
    sha = "f" * 40
    calls: list[tuple[str, dict]] = []
    pages = {
        "1": [_check(f"job-{i}") for i in range(gh_enrichment.CHECKS_PER_PAGE)],
        "2": [_check("last", status="in_progress", conclusion=None)],
    }

    async def _request(_client, _method, url, *_a, params=None, **_k):
        calls.append((url, params))
        if url.endswith("/pulls"):
            return _Resp([_pr(3, ME, sha)]), None
        return _Resp({"total_count": gh_enrichment.CHECKS_PER_PAGE + 1, "check_runs": pages[params["page"]]}), None

    monkeypatch.setattr(gh_enrichment._common, "safe_request", _request)
    facts = await gh_enrichment.fetch("workflow_run.completed", _workflow_run(actor=ME, sha=sha), "tok")
    assert facts["pull_request"]["number"] == 3
    assert facts["head"]["sha"] == sha
    assert facts["head"]["checks"]["total"] == gh_enrichment.CHECKS_PER_PAGE + 1
    assert facts["head"]["checks"]["pending"] == 1 and facts["head"]["checks"]["all_green"] is False
    assert sorted(p["page"] for u, p in calls if u.endswith("/check-runs")) == ["1", "2"]
    # A push reads no check runs.
    calls.clear()
    push = {"after": sha, "repository": {"full_name": DESK_REPOS[0]}}
    facts = await gh_enrichment.fetch("push", push, "tok")
    assert "checks" not in facts["head"]
    assert [u for u, _ in calls if u.endswith("/check-runs")] == []


def _done(run_id: int, name: str, conclusion: str, completed_at: str | None) -> dict:
    return {"id": run_id, "name": name, "status": "completed", "conclusion": conclusion, "completed_at": completed_at}


def test_the_first_failed_check_wakes_a_red_head_while_a_job_still_runs():
    """One failed job beside a hung one: the failed job's completion is the red wake, once."""
    hung = {"id": 4, "name": "Backend (Python) 4/6", "status": "in_progress", "conclusion": None}
    runs = [
        _done(1, "Lint", "success", "2026-11-06T08:20:00Z"),
        _done(2, "Frontend unit tests 3/3", "failure", "2026-11-06T08:26:00Z"),
        hung,
    ]
    facts = gh_enrichment.check_facts(runs, event_check_run_id=2)
    assert facts["first_failure"] is True and facts["all_completed"] is False
    assert facts["failed_names"] == ["Frontend unit tests 3/3"]
    assert facts["first_failed"] == {"name": "Frontend unit tests 3/3", "id": 2, "completed_at": "2026-11-06T08:26:00Z"}
    # A later failure of the same head is not the first: no second wake.
    later = [*runs[:2], _done(4, "Backend (Python) 4/6", "cancelled", "2026-11-06T09:10:00Z")]
    assert gh_enrichment.check_facts(later, event_check_run_id=4)["first_failure"] is False
    # A success, a workflow_run or a status event never carries it.
    assert gh_enrichment.check_facts(runs, event_check_run_id=1)["first_failure"] is False
    assert gh_enrichment.check_facts(runs)["first_failure"] is False
    green = gh_enrichment.check_facts([runs[0]], event_check_run_id=1)
    assert green["first_failed"] is None and green["failed_names"] == []


def test_the_first_failed_check_ties_on_the_lower_id():
    runs = [_done(9, "B", "failure", "2026-11-06T08:26:00Z"), _done(7, "A", "timed_out", "2026-11-06T08:26:00Z")]
    assert gh_enrichment.check_facts(runs, event_check_run_id=7)["first_failure"] is True
    assert gh_enrichment.check_facts(runs, event_check_run_id=9)["first_failure"] is False


def test_only_the_latest_completion_of_a_green_head_is_the_last_to_complete():
    runs = [_done(1, "Lint", "success", "2026-11-09T09:10:00Z"), _done(2, "Build", "success", "2026-11-09T10:00:00Z")]
    assert gh_enrichment.check_facts(runs, 1, "2026-11-09T09:10:00Z")["last_to_complete"] is False
    assert gh_enrichment.check_facts(runs, 2, "2026-11-09T10:00:00Z")["last_to_complete"] is True
    # Ties are allowed: two checks finishing in the same second both count as last.
    tied = [*runs, _done(3, "Docs", "success", "2026-11-09T10:00:00Z")]
    assert gh_enrichment.check_facts(tied, 3, "2026-11-09T10:00:00Z")["last_to_complete"] is True
    # Not a completion (no time), or an unreadable time: never the last.
    assert gh_enrichment.check_facts(runs)["last_to_complete"] is False
    assert gh_enrichment.check_facts(runs, 2, "not a time")["last_to_complete"] is False


def _green_head(monkeypatch, sha: str, runs: list[dict]) -> None:
    """GitHub answering one pull request, *runs* and one completed workflow run for a head."""

    async def _request(_client, _method, url, *_a, params=None, **_k):
        if url.endswith("/pulls"):
            return _Resp([_pr(3, ME, sha)]), None
        if url.endswith("/actions/runs"):
            return _Resp({"total_count": 1, "workflow_runs": [{"id": 5, "name": "CI", "status": "completed"}]}), None
        return _Resp({"total_count": len(runs), "check_runs": runs}), None

    monkeypatch.setattr(gh_enrichment._common, "safe_request", _request)


@pytest.mark.asyncio
async def test_a_late_or_redelivered_completion_of_a_green_head_does_not_match(monkeypatch):
    """Two completions of one green head: only the latest wakes a filter on last_to_complete."""
    sha = "b" * 40
    runs = [_done(1, "App check", "success", "2026-11-09T09:10:00Z"), _done(2, "CI", "success", "2026-11-09T10:00:00Z")]
    _green_head(monkeypatch, sha, runs)
    green = {"action": "completed", "enriched.head.checks.all_green": True}
    last = {**green, "enriched.head.checks.last_to_complete": True}

    def _delivery(run: dict) -> dict:
        return {
            "action": "completed",
            "check_run": {
                "id": run["id"],
                "head_sha": sha,
                "conclusion": "success",
                "completed_at": run["completed_at"],
            },
            "repository": {"full_name": DESK_REPOS[0]},
        }

    stale, latest = _delivery(runs[0]), _delivery(runs[1])
    stale_facts = await gh_enrichment.fetch("check_run.completed", stale, "tok")
    latest_facts = await gh_enrichment.fetch("check_run.completed", latest, "tok")
    # all_green alone matches both, as before: the filters without the new key are unchanged.
    assert payload_filter_reason({**stale, "enriched": stale_facts}, green) is None
    assert payload_filter_reason({**latest, "enriched": latest_facts}, green) is None
    assert payload_filter_reason({**stale, "enriched": stale_facts}, last) is not None
    assert payload_filter_reason({**latest, "enriched": latest_facts}, last) is None
    # A workflow run's completion is timed by its updated_at.
    done = {
        "action": "completed",
        "workflow_run": {"id": 5, "head_sha": sha, "conclusion": "success", "updated_at": "2026-11-09T10:00:05Z"},
        "repository": {"full_name": DESK_REPOS[0]},
    }
    facts = await gh_enrichment.fetch("workflow_run.completed", done, "tok")
    assert facts["head"]["checks"]["last_to_complete"] is True


@pytest.mark.asyncio
async def test_the_fetch_names_the_delivered_check_run(monkeypatch):
    sha = "a" * 40
    runs = [
        _done(2, "Frontend", "failure", "2026-11-06T08:26:00Z"),
        {**_check("Backend", "in_progress", None), "id": 4},
    ]

    async def _request(_client, _method, url, *_a, params=None, **_k):
        if url.endswith("/pulls"):
            return _Resp([_pr(3, ME, sha)]), None
        return _Resp({"total_count": len(runs), "check_runs": runs}), None

    monkeypatch.setattr(gh_enrichment._common, "safe_request", _request)
    delivery = {
        "action": "completed",
        "check_run": {"id": 2, "head_sha": sha, "conclusion": "failure", "completed_at": "2026-11-06T08:26:00Z"},
        "repository": {"full_name": DESK_REPOS[0]},
    }
    facts = await gh_enrichment.fetch("check_run.completed", delivery, "tok")
    assert facts["head"]["checks"]["first_failure"] is True
    red = {"action": "completed", "enriched.head.checks.first_failure": True}
    assert payload_filter_reason({**delivery, "enriched": facts}, red) is None
    created = {**delivery, "action": "created"}
    facts = await gh_enrichment.fetch("check_run.created", created, "tok")
    assert facts["head"]["checks"]["first_failure"] is False


@pytest.mark.asyncio
async def test_a_github_error_on_the_check_runs_fails_the_enrichment(monkeypatch):
    async def _request(_client, _method, url, *_a, **_k):
        if url.endswith("/pulls"):
            return _Resp([]), None
        return None, "GitHub answered HTTP 502"

    monkeypatch.setattr(gh_enrichment._common, "safe_request", _request)
    with pytest.raises(gh_enrichment.EnrichmentError, match="502"):
        await gh_enrichment.fetch("workflow_run.completed", _workflow_run(actor=ME), "tok")


def _github_head(
    monkeypatch, check_runs: list[dict], workflow_runs: list[dict], *, opened_at: str | None = None
) -> list[str]:
    """GitHub answering one pull request (opened at *opened_at*), *check_runs* and *workflow_runs* for a head."""
    urls: list[str] = []
    pull = _pr(3, ME, "b" * 40)
    if opened_at is not None:
        pull["created_at"] = opened_at

    async def _request(_client, _method, url, *_a, params=None, **_k):
        urls.append(url)
        if url.endswith("/pulls"):
            return _Resp([pull]), None
        if url.endswith("/actions/runs"):
            assert params["head_sha"] == "b" * 40
            return _Resp({"total_count": len(workflow_runs), "workflow_runs": workflow_runs}), None
        return _Resp({"total_count": len(check_runs), "check_runs": check_runs}), None

    monkeypatch.setattr(gh_enrichment._common, "safe_request", _request)
    return urls


@pytest.mark.asyncio
async def test_a_head_whose_failed_job_is_re_run_is_not_green(monkeypatch):
    """A stale completion read while a re-run of the failed job is queued: the head is pending, not green."""
    sha = "b" * 40
    # The failed check run is superseded and the new one not listed yet: every listed run is green.
    checks = [
        _done(1, "Lint", "success", "2026-11-07T14:12:00Z"),
        _done(3, "Aikido", "skipped", "2026-11-07T14:11:53Z"),
    ]
    rerun = [{"id": 42, "name": "CI", "status": "queued", "conclusion": None, "run_attempt": 2}]
    _github_head(monkeypatch, checks, rerun)
    delivery = {
        "action": "completed",
        "check_run": {"id": 3, "head_sha": sha, "conclusion": "skipped", "completed_at": "2026-11-07T14:11:53Z"},
        "repository": {"full_name": DESK_REPOS[0]},
    }
    facts = await gh_enrichment.fetch("check_run.completed", delivery, "tok")
    head = facts["head"]["checks"]
    assert head["all_green"] is False and head["all_completed"] is False
    assert head["pending"] == 1 and head["total"] == 3
    assert head["names"][-1] == {"name": "workflow:CI", "status": "queued", "conclusion": None}
    green = {"action": "completed", "enriched.head.checks.all_green": True}
    assert payload_filter_reason({**delivery, "enriched": facts}, green) is not None
    assert gh_enrichment.head_settled(facts) is False


@pytest.mark.asyncio
async def test_a_green_head_with_every_workflow_run_completed_stays_green(monkeypatch):
    checks = [_done(1, "Lint", "success", "2026-11-07T14:12:00Z")]
    urls = _github_head(monkeypatch, checks, [{"id": 42, "name": "CI", "status": "completed"}])
    facts = await gh_enrichment.fetch("workflow_run.completed", _workflow_run(actor=ME, sha="b" * 40), "tok")
    assert facts["head"]["checks"]["all_green"] is True and facts["head"]["checks"]["total"] == 1
    assert any(u.endswith("/actions/runs") for u in urls)


@pytest.mark.asyncio
async def test_a_red_or_running_head_never_reads_the_workflow_runs(monkeypatch):
    for checks in (
        [_done(1, "Lint", "failure", "2026-11-07T14:12:00Z")],
        [{"id": 2, "name": "Backend", "status": "in_progress", "conclusion": None}],
    ):
        urls = _github_head(monkeypatch, checks, [])
        facts = await gh_enrichment.fetch("workflow_run.completed", _workflow_run(actor=ME, sha="b" * 40), "tok")
        assert facts["head"]["checks"]["all_green"] is False
        assert not any(u.endswith("/actions/runs") for u in urls)


def _seconds_ago(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.asyncio
async def test_a_draft_skip_seconds_after_a_push_is_not_green_before_the_workflow_runs_exist(monkeypatch):
    """Aikido skips a draft 3 s after the push, before GitHub creates the push's workflow runs: pending, not green."""
    sha = "b" * 40
    skip = {**_done(3, "Aikido Security: check code", "skipped", _seconds_ago(3)), "started_at": _seconds_ago(3)}
    _github_head(monkeypatch, [skip], [])
    delivery = {
        "action": "completed",
        "check_run": {"id": 3, "head_sha": sha, "conclusion": "skipped", "completed_at": skip["completed_at"]},
        "repository": {"full_name": DESK_REPOS[0]},
    }
    facts = await gh_enrichment.fetch("check_run.completed", delivery, "tok")
    head = facts["head"]["checks"]
    assert head["all_green"] is False and head["pending"] == 1
    assert head["names"][-1]["name"] == "workflow:not-created"
    green = {"action": "completed", "enriched.head.checks.all_green": True}
    assert payload_filter_reason({**delivery, "enriched": facts}, green) is not None
    assert gh_enrichment.head_settled(facts) is False


@pytest.mark.asyncio
async def test_a_head_without_workflow_runs_past_the_grace_window_is_green(monkeypatch):
    """A repository without Actions: its app checks alone make the head green once the window has passed."""
    old = _seconds_ago(gh_enrichment.WORKFLOWS_GRACE_SECONDS + 60)
    _github_head(monkeypatch, [{**_done(1, "Lint", "success", old), "started_at": old}], [])
    facts = await gh_enrichment.fetch("workflow_run.completed", _workflow_run(actor=ME, sha="b" * 40), "tok")
    assert facts["head"]["checks"]["all_green"] is True and facts["head"]["checks"]["total"] == 1


@pytest.mark.asyncio
async def test_a_last_check_seconds_after_the_push_stays_green_when_its_workflows_completed(monkeypatch):
    recent = _seconds_ago(5)
    _github_head(
        monkeypatch,
        [{**_done(1, "CI verdict", "success", recent), "started_at": recent}],
        [{"id": 42, "name": "CI", "status": "completed"}],
    )
    facts = await gh_enrichment.fetch("workflow_run.completed", _workflow_run(actor=ME, sha="b" * 40), "tok")
    assert facts["head"]["checks"]["all_green"] is True


def test_the_grace_window_reads_the_earliest_check_and_ignores_unparsable_times():
    now = datetime(2026, 11, 9, 7, 8, tzinfo=UTC)
    early = {"started_at": "2026-11-09T07:07:53Z"}
    late = {"completed_at": "2026-11-09T07:07:56Z"}
    assert gh_enrichment.workflows_not_created([late, early], [], now=now) is True
    assert gh_enrichment.workflows_not_created([early], ["push"], now=now) is False
    assert gh_enrichment.workflows_not_created([{"started_at": "not a date"}, {}], [], now=now) is False
    assert gh_enrichment.workflows_not_created([early], [], now=now + timedelta(minutes=5)) is False


def test_the_grace_window_of_a_pull_request_waits_for_its_pull_request_runs():
    """#10860: a completed pull_request_target run is not the pull request's CI; the window opens with the PR."""
    check = {"started_at": "2026-11-10T15:09:23Z"}
    opened = datetime(2026, 11, 10, 15, 9, 53, tzinfo=UTC)
    skip_time = datetime(2026, 11, 10, 15, 10, 59, tzinfo=UTC)
    target_only = ["pull_request_target"]
    assert gh_enrichment.workflows_not_created([check], target_only, opened, now=skip_time) is True
    assert gh_enrichment.workflows_not_created([check], [*target_only, "pull_request"], opened, now=skip_time) is False
    assert gh_enrichment.workflows_not_created([check], target_only, None, now=skip_time) is False
    later = opened + timedelta(seconds=gh_enrichment.PR_WORKFLOWS_GRACE_SECONDS + 1)
    assert gh_enrichment.workflows_not_created([check], target_only, opened, now=later) is False


def test_a_pull_request_whose_runs_lag_past_the_push_grace_is_still_pending():
    """#10879: #10877 opened 16:40:49Z, woken green 16:44:21Z, its pull_request runs created about 16:46Z."""
    check = {"started_at": "2026-11-10T16:40:54Z"}
    opened = datetime(2026, 11, 10, 16, 40, 49, tzinfo=UTC)
    wake = datetime(2026, 11, 10, 16, 44, 21, tzinfo=UTC)
    target_only = ["pull_request_target"]
    assert gh_enrichment.workflows_not_created([check], target_only, opened, now=wake) is True
    assert gh_enrichment.workflows_not_created([check], [], None, now=wake) is False
    assert gh_enrichment.workflows_not_created([check], [*target_only, "pull_request"], opened, now=wake) is False


@pytest.mark.asyncio
async def test_a_third_party_skip_after_a_pull_request_target_run_is_not_green(monkeypatch):
    """#10860: a skipped app check and a completed pull_request_target run, the PR's CI not created yet: pending."""
    sha = "b" * 40
    skip = {**_done(3, "Aikido Security: Deep Review", "skipped", _seconds_ago(2)), "started_at": _seconds_ago(95)}
    deploy = {**_done(4, "Feature branch deploy", "success", _seconds_ago(30)), "started_at": _seconds_ago(60)}
    target = [{"id": 41, "name": "Feature branch deploy", "status": "completed", "event": "pull_request_target"}]
    _github_head(monkeypatch, [skip, deploy], target, opened_at=_seconds_ago(66))
    delivery = {
        "action": "completed",
        "check_run": {"id": 3, "head_sha": sha, "conclusion": "skipped", "completed_at": skip["completed_at"]},
        "repository": {"full_name": DESK_REPOS[0]},
    }
    facts = await gh_enrichment.fetch("check_run.completed", delivery, "tok")
    head = facts["head"]["checks"]
    assert head["all_green"] is False and head["names"][-1]["name"] == "workflow:not-created"
    green = {"action": "completed", "enriched.head.checks.all_green": True}
    assert payload_filter_reason({**delivery, "enriched": facts}, green) is not None


@pytest.mark.asyncio
async def test_a_pull_request_head_whose_runs_are_listed_and_completed_stays_green(monkeypatch):
    recent = _seconds_ago(5)
    runs = [
        {"id": 41, "name": "Feature branch deploy", "status": "completed", "event": "pull_request_target"},
        {"id": 42, "name": "CI", "status": "completed", "event": "pull_request"},
    ]
    _github_head(
        monkeypatch, [{**_done(1, "CI verdict", "success", recent), "started_at": recent}], runs, opened_at=recent
    )
    facts = await gh_enrichment.fetch("workflow_run.completed", _workflow_run(actor=ME, sha="b" * 40), "tok")
    assert facts["head"]["checks"]["all_green"] is True


_DESK_GREEN = {
    "action": "completed",
    "workflow_run.conclusion": "success",
    "enriched.head.checks.all_green": True,
    "enriched.pull_request.user.login": ME,
}


def _green_facts(**checks) -> dict:
    return {"pull_request": {"user": {"login": ME}}, "head": {"sha": "a" * 40, "checks": checks}}


def test_the_desk_green_filter_waits_for_the_last_check():
    payload = _workflow_run(actor=ME, conclusion="success")
    early = _green_facts(all_green=False, pending=2)
    assert payload_filter_reason({**payload, "enriched": early}, _DESK_GREEN) == (
        "enriched.head.checks.all_green does not match the filter"
    )
    assert payload_filter_reason({**payload, "enriched": _green_facts(all_green=True, pending=0)}, _DESK_GREEN) is None
    assert payload_filter_reason(
        {**payload, "enriched": _green_facts(all_green=True)}, {"enriched.head.checks.pending": {"eq": 0}}
    ) == "the event has no enriched.head.checks.pending"
    outage = {**payload, "enriched": {"unavailable": "GitHub answered HTTP 502"}}
    assert "could not be read" in payload_filter_reason(outage, _DESK_GREEN)


@pytest.mark.asyncio
async def test_only_the_last_check_to_finish_green_passes_the_trigger(monkeypatch):
    # Each completion reads the head again: the first finds a check still running.
    states = iter([_green_facts(all_green=False, pending=1), _green_facts(all_green=True, pending=0)])

    async def _token(_db, _provider, _integration_id, _agent):
        return "tok", "integration"

    async def _miss(_key):
        return None

    async def _remember(_key, _facts):
        return None

    async def _fetch(_event_key, _payload, _token):
        return next(states)

    enricher = event_enricher("github")
    monkeypatch.setattr(event_enrichment, "_credential", _token)
    monkeypatch.setattr(event_enrichment, "_cached", _miss)
    monkeypatch.setattr(event_enrichment, "_remember", _remember)
    monkeypatch.setattr(event_enrichment, "event_enricher", lambda _p: SimpleNamespace(
        applies=enricher.applies, cache_key=enricher.cache_key, fetch=_fetch
    ))
    agent = SimpleNamespace(integration_bindings={"github": [str(uuid.uuid4())]}, user_id=None)
    config = {"provider": "github", "event": "workflow_run", "filter": _DESK_GREEN}
    ci = _workflow_run(actor=ME, conclusion="success")
    # A workflow never named in any filter finishes last.
    added = _workflow_run(actor=ME, conclusion="success")
    added["workflow_run"] = {**added["workflow_run"], "id": 2, "name": "A workflow added today"}
    reasons = [
        await webhooks._assignment_filter_reason(
            None, "github", "workflow_run.completed", p, event="workflow_run", trigger_config=config, agent=agent
        )
        for p in (ci, added)
    ]
    assert reasons == ["enriched.head.checks.all_green does not match the filter", None]
    # The snapshot leaves the enriched keys to the match path.
    matcher = webhooks._assignment_matcher(
        "github", uuid.uuid4(), uuid.uuid4(), "integration_event", {**config, "event_matching": "exact"}
    )
    assert matcher is not None and webhooks._filtered_out(matcher, ci) is None


_DESK_CI = {
    "action": "completed",
    "workflow_run.conclusion": "failure",
    "repository.full_name": DESK_REPOS,
    "enriched.pull_request.user.login": ME,
}


@pytest.mark.parametrize(
    ("payload", "facts", "runs"),
    [
        # A team member pushed to a desk pull request: the CI result reaches the desk.
        (_workflow_run(actor="alice"), {"pull_request": {"user": {"login": ME}}}, True),
        # The desk pushed to its own pull request.
        (_workflow_run(actor=ME), {"pull_request": {"user": {"login": ME}}}, True),
        # The desk pushed to a team member's pull request: not a desk pull request.
        (_workflow_run(actor=ME), {"pull_request": {"user": {"login": "alice"}}}, False),
        # A fork pull request (no pull_requests in the body) by an outsider.
        (_workflow_run(actor="outsider"), {"pull_request": {"user": {"login": "outsider"}}}, False),
        # A push to a branch with no open pull request.
        (_workflow_run(actor=ME), {"pull_request": None, "pull_requests": []}, False),
        # GitHub did not answer: never widened.
        (_workflow_run(actor="alice"), {"unavailable": "HTTP 502"}, False),
    ],
    ids=["team-push-on-desk-pr", "desk-push", "desk-push-on-team-pr", "fork-pr", "no-pr", "unavailable"],
)
def test_the_desk_red_check_filter_on_enriched_facts(payload, facts, runs):
    reason = payload_filter_reason({**payload, "enriched": facts}, _DESK_CI)
    assert (reason is None) is runs
    if facts.get("unavailable"):
        assert "could not be read" in reason


def test_no_open_pull_request_satisfies_exists_false():
    flt = {"enriched.pull_request": {"exists": False}}
    assert payload_filter_reason({"enriched": {"pull_request": None}}, flt) is None
    assert payload_filter_reason({"enriched": {"pull_request": {"number": 1}}}, flt) is not None


def test_a_team_review_and_a_bot_review_on_a_desk_pr():
    flt = {
        "action": "submitted",
        "pull_request.user.login": ME,
        "sender.login": {"ne": ME},
        "not": {"sender.login": {"ends_with": "[bot]"}},
    }
    review = {"action": "submitted", "pull_request": {"user": {"login": ME}}}
    assert payload_filter_reason({**review, "sender": {"login": "alice"}}, flt) is None
    assert payload_filter_reason({**review, "sender": {"login": "aikido-pr-checks[bot]"}}, flt) is not None
    assert payload_filter_reason({**review, "sender": {"login": ME}}, flt) is not None
    synchronize = {"action": "synchronize", "pull_request": {"user": {"login": ME}}, "sender": {"login": "alice"}}
    assert payload_filter_reason(synchronize, {**flt, "action": "synchronize"}) is None


# -- the enrichment service and the webhook receivers ------------------------


@pytest.mark.asyncio
async def test_no_enrichment_is_read_for_a_filter_that_names_none(monkeypatch):
    async def _never(*_a, **_k):
        raise AssertionError("no credential is read")

    monkeypatch.setattr(event_enrichment, "_credential", _never)
    config = {"provider": "github", "event": "workflow_run", "filter": {"action": "completed"}}
    facts = await event_enrichment.enrichment_for(
        None, "github", "workflow_run.completed", _workflow_run(actor=ME), trigger_config=config, agent=None
    )
    assert facts is None


@pytest.mark.asyncio
async def test_the_enrichment_is_read_once_per_head_and_never_reaches_the_run(monkeypatch):
    reads: list[str] = []
    cache: dict[str, dict] = {}

    async def _token(_db, _provider, integration_id, _agent):
        return "tok", "integration"

    async def _fetch(event_key, payload, token):
        reads.append(token)
        return {"pull_request": {"user": {"login": ME}}, "pull_requests": []}

    async def _cached(key):
        return cache.get(key)

    async def _remember(key, facts):
        cache[key] = facts

    enricher = event_enricher("github")
    monkeypatch.setattr(event_enrichment, "_credential", _token)
    monkeypatch.setattr(event_enrichment, "_cached", _cached)
    monkeypatch.setattr(event_enrichment, "_remember", _remember)
    monkeypatch.setattr(event_enrichment, "event_enricher", lambda _p: SimpleNamespace(
        applies=enricher.applies, cache_key=enricher.cache_key, fetch=_fetch
    ))
    agent = SimpleNamespace(integration_bindings={"github": [str(uuid.uuid4())]}, user_id=None)
    config = {"provider": "github", "event": "workflow_run", "filter": _DESK_CI}
    payload = _workflow_run(actor="alice")
    memo: dict = {}
    for _ in range(2):
        reason = await webhooks._assignment_filter_reason(
            None,
            "github",
            "workflow_run.completed",
            payload,
            event="workflow_run",
            trigger_config=config,
            agent=agent,
            memo=memo,
        )
        assert reason is None
    # A second delivery on the same head reads the cache, not GitHub.
    await webhooks._assignment_filter_reason(
        None, "github", "workflow_run.completed", payload, event="workflow_run", trigger_config=config, agent=agent
    )
    assert reads == ["tok"]
    assert "enriched" not in payload


@pytest.mark.asyncio
async def test_the_cache_is_keyed_on_whose_credential_answered(monkeypatch):
    keys: list[str] = []
    holders = iter([("tok-a", "user:a"), ("tok-b", "user:b")])

    async def _token(_db, _provider, _integration_id, _agent):
        return next(holders)

    async def _cached(key):
        keys.append(key)
        return None

    async def _remember(_key, _facts):
        return None

    async def _fetch(_event_key, _payload, _token):
        return {"pull_request": None, "pull_requests": []}

    enricher = event_enricher("github")
    monkeypatch.setattr(event_enrichment, "_credential", _token)
    monkeypatch.setattr(event_enrichment, "_cached", _cached)
    monkeypatch.setattr(event_enrichment, "_remember", _remember)
    monkeypatch.setattr(event_enrichment, "event_enricher", lambda _p: SimpleNamespace(
        applies=enricher.applies, cache_key=enricher.cache_key, fetch=_fetch
    ))
    integration = str(uuid.uuid4())
    config = {"provider": "github", "event": "workflow_run", "integration_id": integration, "filter": _DESK_CI}
    for _ in range(2):
        await event_enrichment.enrichment_for(
            None, "github", "workflow_run.completed", _workflow_run(actor=ME), trigger_config=config, agent=None
        )
    assert len(keys) == 2 and keys[0] != keys[1]
    assert ":user:a:" in keys[0] and ":user:b:" in keys[1]


@pytest.mark.asyncio
async def test_the_receiving_integration_reads_and_a_pin_elsewhere_reads_nothing(monkeypatch):
    read_with: list[str] = []

    async def _token(_db, _provider, integration_id, _agent):
        read_with.append(str(integration_id))
        return "tok", "integration"

    async def _miss(_key):
        return None

    async def _remember(_key, _facts):
        return None

    async def _fetch(_event_key, _payload, _token):
        return {"pull_request": None, "pull_requests": []}

    enricher = event_enricher("github")
    monkeypatch.setattr(event_enrichment, "_credential", _token)
    monkeypatch.setattr(event_enrichment, "_cached", _miss)
    monkeypatch.setattr(event_enrichment, "_remember", _remember)
    monkeypatch.setattr(event_enrichment, "event_enricher", lambda _p: SimpleNamespace(
        applies=enricher.applies, cache_key=enricher.cache_key, fetch=_fetch
    ))
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    agent = SimpleNamespace(integration_bindings={"github": [a, b]}, user_id=None)
    unpinned = {"provider": "github", "event": "workflow_run", "filter": _DESK_CI}
    await event_enrichment.enrichment_for(
        None, "github", "workflow_run.completed", _workflow_run(actor=ME),
        trigger_config=unpinned, agent=agent, receiver_integration_id=b,
    )
    assert read_with == [b]
    pinned = {**unpinned, "integration_id": a}
    facts = await event_enrichment.enrichment_for(
        None, "github", "workflow_run.completed", _workflow_run(actor=ME),
        trigger_config=pinned, agent=agent, receiver_integration_id=b,
    )
    assert read_with == [b]
    assert facts is not None and "another integration" in facts["unavailable"]
    # The same integration spelled otherwise (uppercase UUID) is the receiver.
    upper = {**unpinned, "integration_id": b.upper()}
    facts = await event_enrichment.enrichment_for(
        None, "github", "workflow_run.completed", _workflow_run(actor=ME),
        trigger_config=upper, agent=agent, receiver_integration_id=b,
    )
    assert read_with == [b, b]
    assert facts is not None and "unavailable" not in facts


@pytest.mark.asyncio
async def test_no_credential_drops_the_event_naming_why(monkeypatch):
    async def _none(*_a, **_k):
        return None

    async def _miss(_key):
        return None

    monkeypatch.setattr(event_enrichment, "_credential", _none)
    monkeypatch.setattr(event_enrichment, "_cached", _miss)
    agent = SimpleNamespace(integration_bindings={"github": [str(uuid.uuid4())]}, user_id=None)
    config = {"provider": "github", "event": "workflow_run", "filter": _DESK_CI}
    reason = await webhooks._assignment_filter_reason(
        None,
        "github",
        "workflow_run.completed",
        _workflow_run(actor="alice"),
        event="workflow_run",
        trigger_config=config,
        agent=agent,
    )
    assert reason is not None and "no credential" in reason


def test_the_snapshot_leaves_an_enriched_filter_to_the_match_path():
    config = {"provider": "github", "event": "workflow_run", "event_matching": "exact", "filter": _DESK_CI}
    matcher = webhooks._assignment_matcher("github", uuid.uuid4(), uuid.uuid4(), "integration_event", config)
    assert matcher is not None
    assert webhooks._filtered_out(matcher, _workflow_run(actor="alice")) is None


def test_the_snapshot_counts_the_reason_of_a_drop():
    config = {
        "provider": "github",
        "event": "workflow_run",
        "event_matching": "exact",
        "filter": {"sender.login": {"ne": ME}},
    }
    wf_id = str(uuid.uuid4())
    matcher = webhooks._assignment_matcher("github", wf_id, uuid.uuid4(), "integration_event", config)
    routing = webhooks._ProviderRouting(
        oauth_default_exists=False, extra_config_enc=None, client_secret_enc=None, has_agents=True, matchers=(matcher,)
    )
    filtered: dict[str, int] = {}
    reasons: dict[str, dict[str, int]] = {}
    matched = webhooks._match_provider_assignments(
        routing, "github", "workflow_run.completed", payload=_workflow_run(actor=ME), filtered=filtered, reasons=reasons
    )
    assert matched == []
    assert filtered == {wf_id: 1}
    assert reasons == {wf_id: {"sender.login fails ne": 1}}

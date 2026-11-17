"""Which GitHub App installations an integration's own token sees.

The check a strict-mode delivery of the admin GitHub App runs for an
integration recording no installation: it reads the answer cached for the
integration's credentials and never asks GitHub; the lookups it calls for run
in the background. GitHub is faked (``FakeGitHub`` for the check, an
``httpx.MockTransport`` for the listing itself) and so is Redis.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest

from app.core.config import settings
from app.core.security import encrypt_value
from app.services import github_token_installations as gti
from app.services.github_token_installations import fetch_installations as real_fetch_installations

INSTALLATION = "222"


class FakeRedis:
    """``MGET`` and ``SET EX``: ``fail`` refuses both, ``fail_set`` only the write, ``hang`` never answers."""

    def __init__(self, *, fail: bool = False, fail_set: bool = False, hang: bool = False) -> None:
        self.fail = fail
        self.fail_set = fail_set
        self.hang = hang
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.commands: list[str] = []

    async def _answer(self, name: str) -> None:
        if self.hang:
            await asyncio.Event().wait()
        if self.fail or (self.fail_set and name == "set"):
            raise ConnectionError("Redis is unreachable")
        self.commands.append(name)

    async def mget(self, keys):
        await self._answer("mget")
        return [self.values.get(key) for key in keys]

    async def set(self, key, value, *, ex=None):
        await self._answer("set")
        self.values[key] = value
        self.ttls[key] = ex
        return True


class FakeGitHub:
    """``fetch_installations``: an answer (or an exception) per token, every call counted."""

    def __init__(self) -> None:
        self.answers: dict[str, object] = {}
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, token: str) -> gti.TokenListing:
        self.calls.append(token)
        if self.gate is not None:
            await self.gate.wait()
        answer = self.answers.get(token, gti.InstallationLookupError("HTTP 502"))
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    async def _store():
        return fake

    monkeypatch.setattr(gti, "redis_store", _store)
    return fake


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(gti, "fetch_installations", fake)
    return fake


def _candidate(
    access_token: str | None = None,
    *,
    integration_id: str = "integ-1",
    name: str = "Alice's GitHub",
    api_key: str | None = None,
    token_enc: str | None = None,
    no_token: bool = False,
) -> gti.TokenCandidate:
    """One integration row as the webhook reads it. Encrypting again changes the
    fingerprint, so a test keeps the candidate it built for every delivery."""
    if token_enc is not None:
        access = token_enc
    elif no_token:
        access = None
    else:
        access = encrypt_value(access_token or "gho-token")
    return gti.token_candidate(
        integration_id,
        name,
        "oauth",
        encrypt_value("admin-client"),
        access,
        encrypt_value(api_key) if api_key else None,
        None,
    )


def _seed(redis: FakeRedis, candidate, ids, *, at: float | None = None, complete: bool = True, fp=None) -> None:
    redis.values[f"github:token_installations:{candidate.integration_id}"] = json.dumps(
        {
            "fp": fp or candidate.fingerprint,
            "at": time.time() if at is None else at,
            "ids": sorted(ids),
            "complete": complete,
        }
    )


async def _reach(candidate, installation=INSTALLATION) -> bool:
    return (await gti.installation_reach([candidate], installation))[candidate.integration_id]


# ---------------------------------------------------------------------------
# Fingerprint and candidate
# ---------------------------------------------------------------------------


def test_the_fingerprint_moves_with_every_credential_column_and_the_secret_key(monkeypatch):
    base = {
        "auth_type": "oauth",
        "client_id_enc": "c",
        "access_token_enc": "t",
        "api_key_enc": None,
        "extra_config_enc": "x",
    }
    fingerprint = gti.credentials_fingerprint(**base)
    assert fingerprint == gti.credentials_fingerprint(**base)
    assert len(fingerprint) == 32
    for column, value in (
        ("auth_type", "api_key"),
        ("client_id_enc", "c2"),
        ("access_token_enc", "t2"),
        ("api_key_enc", "k"),
        ("extra_config_enc", None),
    ):
        assert gti.credentials_fingerprint(**{**base, column: value}) != fingerprint, column
    monkeypatch.setattr(settings, "secret_key", settings.secret_key + "-rotated")
    assert gti.credentials_fingerprint(**base) != fingerprint


def test_the_candidate_holds_the_token_its_tools_use_and_whether_it_decrypts():
    access = encrypt_value("gho-access")
    key = encrypt_value("ghp-key")
    candidate = gti.token_candidate("i", None, "oauth", None, access, key, None)
    assert candidate.token_enc == access
    assert candidate.usable is True
    assert candidate.name == "i"
    assert gti.token_candidate("i", "n", "api_key", None, None, key, None).token_enc == key
    none_stored = gti.token_candidate("i", "n", "oauth", None, None, None, None)
    assert (none_stored.token_enc, none_stored.usable) == (None, False)
    assert gti.token_candidate("i", "n", "oauth", None, "not-a-fernet-token", None, None).usable is False


@pytest.mark.asyncio
async def test_a_token_stored_as_a_vault_reference_is_read_from_its_vault(monkeypatch):
    from app.services import vault
    from app.services.vault.base import VaultProvider
    from app.services.vault.config import VaultProviderConfig
    from app.services.vault.errors import VaultReadError
    from app.services.vault.resolver import SecretResolver, set_resolver

    secrets: dict[str, object] = {"github/bot": {"token": "gho-from-vault"}}

    class _Vault(VaultProvider):
        async def read(self, path: str) -> object:
            answer = secrets[path]
            if isinstance(answer, Exception):
                raise answer
            return answer

    async def _no_record(*_args) -> None:
        return None

    monkeypatch.setattr(vault, "clear_failure", _no_record)
    monkeypatch.setattr(vault, "record_failure", _no_record)
    set_resolver(SecretResolver({"corp": _Vault(VaultProviderConfig(name="corp", type="hashicorp"))}, {}, 0))
    candidate = gti.token_candidate("i", "n", "oauth", None, encrypt_value("vault://corp/github/bot#token"), None, None)
    assert (candidate.usable, candidate.token_field) == (True, "access_token")
    assert await gti._reveal_token(candidate) == "gho-from-vault"
    secrets["github/bot"] = VaultReadError("access_denied", "HTTP 403")
    with pytest.raises(gti.InstallationLookupError, match="secret vault"):
        await gti._reveal_token(candidate)


def test_the_events_that_change_who_sees_an_installation():
    for event in ("installation.created", "installation_repositories.added", "organization.member_added",
                  "membership.added", "member.added", "team.added_to_repository", "team_add"):
        assert gti.changes_installation_visibility(event), event
    for event in ("issues.opened", "pull_request.closed", "push", "installation_x.created"):
        assert not gti.changes_installation_visibility(event), event


# ---------------------------------------------------------------------------
# The listing (GET /user/installations)
# ---------------------------------------------------------------------------


def _github_answers(monkeypatch, handler):
    requests: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(gti, "transport", httpx.MockTransport(_record))
    return requests


def _page(ids, total=None):
    body = {"installations": [{"id": i, "account": {"login": f"org-{i}"}} for i in ids]}
    if total is not None:
        body["total_count"] = total
    return httpx.Response(200, json=body)


@pytest.mark.asyncio
async def test_the_listing_is_read_page_by_page_to_the_total(monkeypatch):
    pages = {"1": list(range(1, 101)), "2": list(range(101, 151))}
    requests = _github_answers(monkeypatch, lambda r: _page(pages[r.url.params["page"]], total=150))

    listing = await real_fetch_installations("gho-secret")

    assert listing == gti.TokenListing(frozenset(str(i) for i in range(1, 151)))
    assert [r.url.params["page"] for r in requests] == ["1", "2"]
    assert all(r.url.params["per_page"] == "100" for r in requests)
    assert str(requests[0].url).startswith("https://api.github.com/user/installations")
    assert requests[0].headers["Authorization"] == "Bearer gho-secret"


@pytest.mark.asyncio
async def test_a_short_page_ends_the_listing(monkeypatch):
    requests = _github_answers(monkeypatch, lambda _r: _page([7, 8]))
    assert await real_fetch_installations("t") == gti.TokenListing(frozenset({"7", "8"}))
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_the_page_cap_marks_the_listing_incomplete(monkeypatch):
    def _full(request):
        start = (int(request.url.params["page"]) - 1) * 100
        return _page(range(start + 1, start + 101))

    requests = _github_answers(monkeypatch, _full)
    listing = await real_fetch_installations("t")
    assert (len(listing.ids), listing.complete) == (gti.PER_PAGE * gti.MAX_PAGES, False)
    assert len(requests) == gti.MAX_PAGES


@pytest.mark.asyncio
async def test_a_token_github_lists_no_installation_for_sees_none(monkeypatch):
    _github_answers(
        monkeypatch,
        lambda _r: httpx.Response(403, json={"message": "You must authenticate with an access token authorized to a GitHub App"}),
    )
    assert await real_fetch_installations("ghp-pat") == gti.TokenListing(frozenset())


@pytest.mark.asyncio
async def test_ids_that_are_not_installation_ids_are_left_out(monkeypatch):
    _github_answers(
        monkeypatch,
        lambda _r: httpx.Response(
            200, json={"installations": [{"id": 5}, {"id": "6"}, {"id": True}, {"id": "x"}, {}, "7", {"id": None}]}
        ),
    )
    assert (await real_fetch_installations("t")).ids == frozenset({"5", "6"})


def _raise_transport_error(request):
    raise httpx.ConnectError("unreachable", request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        lambda _r: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={"message": "API rate limit exceeded"}),
        lambda _r: httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"}),
        lambda _r: httpx.Response(429, headers={"retry-after": "60"}),
        lambda _r: httpx.Response(401, json={"message": "Bad credentials"}),
        lambda _r: httpx.Response(502),
        lambda _r: httpx.Response(200, text="<html>"),
        lambda _r: httpx.Response(200, json={"installations": "none"}),
        lambda _r: httpx.Response(200, json=[1, 2]),
        _raise_transport_error,
        lambda r: _page(range(1, 101)) if r.url.params["page"] == "1" else httpx.Response(403),
    ],
    ids=["rate-limit", "secondary-rate-limit", "429", "401", "502", "not-json", "no-list", "not-an-object",
         "transport", "403-on-a-later-page"],
)
async def test_every_other_answer_is_a_failed_lookup(monkeypatch, handler):
    _github_answers(monkeypatch, handler)
    with pytest.raises(gti.InstallationLookupError):
        await real_fetch_installations("t")


# ---------------------------------------------------------------------------
# The check a delivery runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_answer_listing_the_installation_reaches_it_and_asks_github_nothing(redis, github):
    candidate = _candidate()
    _seed(redis, candidate, {"111", INSTALLATION})
    assert await _reach(candidate) is True
    await gti.settle()
    assert github.calls == []


@pytest.mark.asyncio
async def test_a_complete_answer_not_listing_the_installation_does_not_reach_it(redis, github):
    candidate = _candidate()
    _seed(redis, candidate, {"111"})
    assert await _reach(candidate) is False
    await gti.settle()
    assert github.calls == []


@pytest.mark.asyncio
async def test_with_no_answer_it_is_reached_as_before_and_learned_in_the_background(redis, github):
    candidate = _candidate("gho-alice")
    github.answers["gho-alice"] = gti.TokenListing(frozenset({"111"}))
    github.gate = asyncio.Event()

    # Answered while GitHub has not answered: the delivery never waits on it.
    assert await _reach(candidate) is True
    await asyncio.sleep(0)
    assert github.calls == ["gho-alice"]
    github.gate.set()
    await gti.settle()

    assert await _reach(candidate) is False
    assert await _reach(candidate, "111") is True
    await gti.settle()
    assert github.calls == ["gho-alice"]


@pytest.mark.asyncio
async def test_what_redis_keeps_is_the_answer_and_nothing_secret(redis, github):
    candidate = _candidate("gho-alice-secret")
    github.answers["gho-alice-secret"] = gti.TokenListing(frozenset({"111"}))
    await _reach(candidate)
    await gti.settle()

    key = "github:token_installations:integ-1"
    stored = json.loads(redis.values[key])
    assert stored["ids"] == ["111"]
    assert stored["complete"] is True
    assert stored["fp"] == candidate.fingerprint
    assert redis.ttls[key] == gti.RETAIN_SECONDS
    assert "gho-alice-secret" not in redis.values[key]
    assert candidate.token_enc not in redis.values[key]


@pytest.mark.asyncio
async def test_an_answer_read_with_other_credentials_is_no_answer(redis, github):
    reconnected = _candidate("gho-new")
    _seed(redis, reconnected, {INSTALLATION}, fp="read-with-the-previous-token")
    github.answers["gho-new"] = gti.TokenListing(frozenset({"111"}))

    assert await _reach(reconnected) is True
    await gti.settle()
    assert github.calls == ["gho-new"]
    assert await _reach(reconnected) is False


@pytest.mark.asyncio
async def test_a_new_secret_key_invalidates_every_answer(redis, github, monkeypatch):
    row = ("integ-1", "GitHub", "oauth", encrypt_value("admin-client"), encrypt_value("gho"), None, None)
    before = gti.token_candidate(*row)
    _seed(redis, before, {"111"})
    assert await _reach(before) is False
    monkeypatch.setattr(settings, "secret_key", settings.secret_key + "-rotated")
    after = gti.token_candidate(*row)
    assert after.fingerprint != before.fingerprint
    assert await _reach(after) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("token_enc", ["", "not-a-fernet-token"], ids=["none-stored", "undecryptable"])
async def test_an_integration_holding_no_usable_token_sees_nothing_and_is_named(
    redis, github, caplog, token_enc
):
    candidate = _candidate(token_enc=token_enc or None, no_token=not token_enc)
    with caplog.at_level(logging.WARNING, logger=gti.__name__):
        assert await _reach(candidate) is False
    await gti.settle()
    assert github.calls == []
    assert redis.commands == []
    assert any(
        "integ-1" in r.getMessage() and "Alice's GitHub" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_an_incomplete_listing_decides_only_for_what_it_lists(redis, github):
    candidate = _candidate()
    _seed(redis, candidate, {"111"}, complete=False)
    assert await _reach(candidate, "111") is True
    assert await _reach(candidate) is True


@pytest.mark.asyncio
async def test_a_stale_answer_keeps_deciding_while_it_is_read_again(redis, github):
    candidate = _candidate("gho-alice")
    _seed(redis, candidate, {"111"}, at=time.time() - gti.FRESH_SECONDS - 1)
    github.answers["gho-alice"] = gti.TokenListing(frozenset({"111", INSTALLATION}))

    assert await _reach(candidate) is False
    await gti.settle()
    assert github.calls == ["gho-alice"]
    assert await _reach(candidate) is True


@pytest.mark.asyncio
async def test_an_installation_changed_after_the_answer_is_reached_until_read_again(redis, github):
    """An organisation that installs the app later: GitHub says so with an
    ``installation`` event first, and its events are not lost meanwhile."""
    candidate = _candidate("gho-alice")
    _seed(redis, candidate, {"111"}, at=time.time() - 5)
    github.answers["gho-alice"] = gti.TokenListing(frozenset({"111", INSTALLATION}))

    await gti.note_installation_change(INSTALLATION)
    assert await _reach(candidate) is True
    assert await _reach(candidate, "111") is True
    await gti.settle()
    assert github.calls == ["gho-alice"]
    # Read again after the mark: the answer decides once more.
    assert await _reach(candidate) is True
    assert await _reach(candidate, "333") is False


@pytest.mark.asyncio
async def test_a_member_removed_is_decided_by_the_answer_read_after_the_change(redis, github):
    candidate = _candidate("gho-alice")
    _seed(redis, candidate, {INSTALLATION}, at=time.time() - 5)
    github.answers["gho-alice"] = gti.TokenListing(frozenset({"111"}))

    await gti.note_installation_change(INSTALLATION)
    assert await _reach(candidate) is True
    await gti.settle()
    assert await _reach(candidate) is False


@pytest.mark.asyncio
async def test_a_change_marked_while_github_is_asked_leaves_the_answer_outdated(redis, github):
    """The answer is stamped with when its lookup STARTED: a change GitHub
    announced while it ran may not be in it."""
    candidate = _candidate("gho-alice")
    github.answers["gho-alice"] = gti.TokenListing(frozenset({"111"}))
    github.gate = asyncio.Event()
    assert await _reach(candidate) is True
    await asyncio.sleep(0.02)
    await gti.note_installation_change(INSTALLATION)
    github.gate.set()
    await gti.settle()

    stored = json.loads(redis.values["github:token_installations:integ-1"])
    assert stored["at"] < float(redis.values[f"github:installation_visibility_changed:{INSTALLATION}"])
    assert await _reach(candidate) is True


@pytest.mark.asyncio
async def test_a_failed_lookup_is_named_and_not_retried_for_a_minute(redis, github, caplog):
    candidate = _candidate("gho-alice")
    with caplog.at_level(logging.WARNING, logger=gti.__name__):
        assert await _reach(candidate) is True
        await gti.settle()
        assert await _reach(candidate) is True
        await gti.settle()
    assert github.calls == ["gho-alice"]
    assert any(
        "integ-1" in r.getMessage() and "HTTP 502" in r.getMessage() and "as before" in r.getMessage()
        for r in caplog.records
    )
    # Once the back-off ends, GitHub is asked again.
    gti._failed_until.clear()
    await _reach(candidate)
    await gti.settle()
    assert github.calls == ["gho-alice", "gho-alice"]


def test_the_failure_back_offs_are_bounded_without_forgetting_the_live_ones(monkeypatch):
    monkeypatch.setattr(gti, "MAX_REMEMBERED_FAILURES", 3)
    gti._failed_until[("expired", "fp")] = 0.0
    for n in range(2):
        gti._remember_failure((f"live-{n}", "fp"))
    gti._remember_failure(("new", "fp"))
    assert list(gti._failed_until) == [("live-0", "fp"), ("live-1", "fp"), ("new", "fp")]
    gti._remember_failure(("newer", "fp"))
    assert list(gti._failed_until) == [("live-1", "fp"), ("new", "fp"), ("newer", "fp")]


@pytest.mark.asyncio
async def test_a_failed_lookup_keeps_the_last_answer_for_the_same_credentials(redis, github):
    candidate = _candidate("gho-alice")
    _seed(redis, candidate, {"111"}, at=time.time() - gti.FRESH_SECONDS - 1)
    assert await _reach(candidate) is False
    await gti.settle()
    assert github.calls == ["gho-alice"]
    assert await _reach(candidate) is False
    assert await _reach(candidate, "111") is True


@pytest.mark.asyncio
async def test_an_answer_that_cannot_be_kept_backs_off_like_a_failure(github, monkeypatch):
    fake = FakeRedis(fail_set=True)

    async def _store():
        return fake

    monkeypatch.setattr(gti, "redis_store", _store)
    candidate = _candidate("gho-alice")
    github.answers["gho-alice"] = gti.TokenListing(frozenset({"111"}))
    assert await _reach(candidate) is True
    await gti.settle()
    assert await _reach(candidate) is True
    await gti.settle()
    assert github.calls == ["gho-alice"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fake", [FakeRedis(fail=True), FakeRedis(hang=True)], ids=["refused", "silent"])
async def test_a_redis_that_does_not_answer_reaches_as_before_and_asks_github_nothing(github, monkeypatch, fake):
    async def _store():
        return fake

    monkeypatch.setattr(gti, "redis_store", _store)
    monkeypatch.setattr(gti, "CACHE_TIMEOUT_SECONDS", 0.05)
    candidate = _candidate()
    assert await _reach(candidate) is True
    await gti.settle()
    assert github.calls == []


@pytest.mark.asyncio
async def test_without_a_store_it_is_reached_as_before_and_github_is_not_asked(github, monkeypatch):
    monkeypatch.setattr(gti, "redis_store", None)
    assert await _reach(_candidate()) is True
    await gti.settle()
    assert github.calls == []


@pytest.mark.asyncio
async def test_one_lookup_at_a_time_for_the_same_credentials(redis, github):
    candidate = _candidate("gho-alice")
    github.answers["gho-alice"] = gti.TokenListing(frozenset())
    github.gate = asyncio.Event()
    await asyncio.gather(_reach(candidate), _reach(candidate), gti.installation_reach([candidate, candidate], "9"))
    github.gate.set()
    await gti.settle()
    assert github.calls == ["gho-alice"]


@pytest.mark.asyncio
async def test_the_lookups_in_flight_are_bounded(redis, github, monkeypatch):
    monkeypatch.setattr(gti, "MAX_PENDING_LOOKUPS", 2)
    github.gate = asyncio.Event()
    candidates = [_candidate(f"gho-{n}", integration_id=f"integ-{n}") for n in range(3)]
    reach = await gti.installation_reach(candidates, INSTALLATION)
    assert set(reach.values()) == {True}
    github.gate.set()
    await gti.settle()
    assert len(github.calls) == 2


@pytest.mark.asyncio
async def test_several_integrations_are_read_in_one_round_trip(redis, github):
    sees, blind = _candidate("a", integration_id="sees"), _candidate("b", integration_id="blind")
    _seed(redis, sees, {INSTALLATION})
    _seed(redis, blind, {"111"})
    assert await gti.installation_reach([sees, blind], INSTALLATION) == {"sees": True, "blind": False}
    assert redis.commands == ["mget"]


@pytest.mark.asyncio
async def test_a_change_mark_is_kept_for_the_retention_and_never_fails_the_caller(monkeypatch):
    fake = FakeRedis()

    async def _store():
        return fake

    monkeypatch.setattr(gti, "redis_store", _store)
    await gti.note_installation_change("444")
    key = "github:installation_visibility_changed:444"
    assert float(fake.values[key]) <= time.time()
    assert fake.ttls[key] == gti.RETAIN_SECONDS

    for crafted in ("x" * 5000, "12a", "1" * 21, ""):
        await gti.note_installation_change(crafted)
    assert list(fake.values) == [key]

    fake.fail = True
    await gti.note_installation_change("444")
    monkeypatch.setattr(gti, "redis_store", None)
    await gti.note_installation_change("444")

"""LLM providers restricted to groups and users.

The rules live in ``app.llm.provider_access``; the router resolves every run
within them. These tests pin the rule itself (who may use a provider, which one
a run picks), the write of an access list, and the router entry points a run,
a model picker and a model write go through.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.llm import provider_access, router
from app.llm.provider_access import (
    NO_ACCOUNT,
    NO_USABLE_PROVIDER_MESSAGE,
    ProviderAccessError,
    ProviderAudience,
    ProviderNotUsableError,
    apply_provider_access,
    provider_usable_by,
    routing_for,
    usable_model_ids,
)

TEAM_A = str(uuid.uuid4())
TEAM_B = str(uuid.uuid4())
ALICE = str(uuid.uuid4())  # in team A
BOB = str(uuid.uuid4())  # in team B
CAROL = str(uuid.uuid4())  # in no team

AUDIENCES = {
    ALICE: ProviderAudience(user_id=ALICE, group_ids=frozenset({TEAM_A})),
    BOB: ProviderAudience(user_id=BOB, group_ids=frozenset({TEAM_B})),
    CAROL: ProviderAudience(user_id=CAROL),
}


def _cfg(provider="anthropic", models=("claude-sonnet-4-6",), *, groups=None, users=None, restricted=None):
    """A provider config; restricted when it names an audience, unless told."""
    if restricted is None:
        restricted = bool(groups or users)
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider=provider,
        available_models=list(models),
        access_restricted=restricted,
        shared_group_ids=list(groups) if groups else None,
        shared_user_ids=list(users) if users else None,
        is_default=False,
        created_at=None,
    )


# ── who may use a provider ───────────────────────────────────────────────


def test_an_unrestricted_provider_serves_everyone_including_no_account():
    cfg = _cfg()
    assert provider_usable_by(cfg, NO_ACCOUNT)
    assert provider_usable_by(cfg, AUDIENCES[CAROL])


def test_a_restricted_provider_serves_its_groups_and_users_only():
    cfg = _cfg(groups=[TEAM_A], users=[CAROL])
    assert provider_usable_by(cfg, AUDIENCES[ALICE])  # through team A
    assert provider_usable_by(cfg, AUDIENCES[CAROL])  # named directly
    assert not provider_usable_by(cfg, AUDIENCES[BOB])
    assert not provider_usable_by(cfg, NO_ACCOUNT)


def test_a_restricted_provider_naming_no_one_serves_no_one():
    cfg = _cfg(restricted=True)
    assert not any(provider_usable_by(cfg, a) for a in [*AUDIENCES.values(), NO_ACCOUNT])


def test_stored_ids_match_whatever_their_spelling():
    cfg = _cfg(groups=[TEAM_A.upper()], users=[uuid.UUID(CAROL)])
    assert provider_usable_by(cfg, AUDIENCES[ALICE])
    assert provider_usable_by(cfg, AUDIENCES[CAROL])


def test_the_restriction_flag_alone_decides_whether_lists_apply():
    # Lists kept while the provider is open restrict nothing.
    cfg = _cfg(groups=[TEAM_B], restricted=False)
    assert provider_usable_by(cfg, AUDIENCES[ALICE])


def test_a_caller_with_no_account_is_no_one():
    assert asyncio.run(provider_access.load_provider_audience(None, "anonymous")) is NO_ACCOUNT
    assert asyncio.run(provider_access.load_provider_audience(None, None)) is NO_ACCOUNT


# ── which provider a run picks ───────────────────────────────────────────


def test_routing_splits_granted_open_and_withheld():
    open_cfg = _cfg()
    team_a = _cfg(groups=[TEAM_A])
    team_b = _cfg(groups=[TEAM_B])
    routing = routing_for(AUDIENCES[ALICE], [open_cfg, team_a, team_b])
    assert routing.usable == [open_cfg, team_a]
    assert routing.granted == [team_a]
    assert routing.open == [open_cfg]
    assert routing.team_first == [team_a, open_cfg]
    assert routing.is_withheld(team_b.id)
    assert routing.is_withheld(str(team_b.id).upper())
    assert not routing.is_withheld(team_a.id)
    assert not routing.is_withheld("not-a-uuid")


def test_a_shared_agent_runs_on_the_callers_team_key():
    open_cfg = _cfg()
    team_a = _cfg(groups=[TEAM_A])
    team_b = _cfg(groups=[TEAM_B])
    configs = [open_cfg, team_a, team_b]
    model = "claude-sonnet-4-6"
    assert routing_for(AUDIENCES[ALICE], configs).owner_for(model) is team_a
    assert routing_for(AUDIENCES[BOB], configs).owner_for(model) is team_b
    assert routing_for(AUDIENCES[CAROL], configs).owner_for(model) is open_cfg
    assert routing_for(NO_ACCOUNT, configs).owner_for(model) is open_cfg


def test_the_team_key_serves_a_model_of_its_vendor_it_does_not_list():
    open_cfg = _cfg(models=("claude-opus-4-6",))
    team_a = _cfg(models=("claude-sonnet-4-6",), groups=[TEAM_A])
    routing = routing_for(AUDIENCES[ALICE], [open_cfg, team_a])
    # The open provider lists it: it serves it.
    assert routing.owner_for("claude-opus-4-6") is open_cfg
    # Nobody lists it: the team's key of that vendor serves it first.
    assert routing.owner_for("claude-haiku-4-6") is team_a


def test_a_model_only_another_team_offers_is_not_the_callers():
    open_cfg = _cfg(provider="openai", models=("gpt-5-mini",))
    team_b = _cfg(models=("claude-sonnet-4-6",), groups=[TEAM_B])
    routing = routing_for(AUDIENCES[ALICE], [open_cfg, team_b])
    assert routing.is_reserved("claude-sonnet-4-6")
    assert routing.owner_for("claude-sonnet-4-6") is None
    assert not routing.is_reserved("gpt-5-mini")
    # A model nobody lists is not reserved: prefix inference still applies.
    assert not routing.is_reserved("gpt-6")


def test_a_model_the_caller_also_has_elsewhere_is_not_reserved():
    open_cfg = _cfg()
    team_b = _cfg(groups=[TEAM_B])
    routing = routing_for(AUDIENCES[ALICE], [open_cfg, team_b])
    assert not routing.is_reserved("claude-sonnet-4-6")
    assert routing.owner_for("claude-sonnet-4-6") is open_cfg


def test_a_stored_reference_to_an_open_provider_yields_to_the_team_key():
    open_cfg = _cfg()
    team_a = _cfg(groups=[TEAM_A])
    other_vendor = _cfg(provider="openai", models=("gpt-5-mini",))
    routing = routing_for(AUDIENCES[ALICE], [open_cfg, team_a, other_vendor])
    assert routing.prefer_team(open_cfg, "claude-sonnet-4-6") is team_a
    assert routing.prefer_team(team_a, "claude-sonnet-4-6") is team_a
    # No model named: the team's key of the same vendor.
    assert routing.prefer_team(open_cfg, None) is team_a
    # The team has no key of that vendor: the reference stands.
    assert routing.prefer_team(other_vendor, "gpt-5-mini") is other_vendor
    # Nothing granted: every reference stands.
    assert routing_for(AUDIENCES[CAROL], [open_cfg]).prefer_team(open_cfg, None) is open_cfg


def test_choosing_a_vendor_takes_the_team_key_first():
    open_cfg = _cfg(models=("claude-sonnet-4-6", "claude-opus-4-6"))
    team_a = _cfg(models=("claude-sonnet-4-6",), groups=[TEAM_A])
    routing = routing_for(AUDIENCES[ALICE], [open_cfg, team_a])
    assert routing.choose("anthropic") is team_a
    assert routing.choose("anthropic", "claude-sonnet-4-6") is team_a
    # Only the open provider lists that model.
    assert routing.choose("anthropic", "claude-opus-4-6") is open_cfg
    assert routing.choose("openai") is None


def test_usable_models_leave_out_the_other_teams():
    routing = routing_for(
        AUDIENCES[ALICE],
        [_cfg(models=("a",)), _cfg(models=("b",), groups=[TEAM_A]), _cfg(models=("c",), groups=[TEAM_B])],
    )
    assert usable_model_ids(routing) == {"a", "b"}


# ── writing an access list ───────────────────────────────────────────────


@pytest.fixture
def known_ids(monkeypatch):
    """The ids that exist; records every id the write looked up."""
    known: set[str] = set()
    looked_up: list[str] = []

    async def _missing(_db, _model, ids):
        looked_up.extend(ids)
        return [i for i in ids if i not in known]

    monkeypatch.setattr(provider_access, "_missing_ids", _missing)
    return SimpleNamespace(known=known, looked_up=looked_up)


def _write(cfg, **kwargs):
    kwargs = {"access_restricted": None, "group_ids": None, "user_ids": None, **kwargs}
    return asyncio.run(apply_provider_access(None, cfg, **kwargs))


def test_restricting_a_provider_writes_its_audience(known_ids):
    known_ids.known.update({TEAM_A, ALICE})
    cfg = _cfg()
    change = _write(cfg, access_restricted=True, group_ids=[TEAM_A.upper(), TEAM_A], user_ids=[ALICE])
    assert cfg.access_restricted is True
    assert cfg.shared_group_ids == [TEAM_A]  # canonical, de-duplicated
    assert cfg.shared_user_ids == [ALICE]
    assert change["before"]["access_restricted"] is False
    assert change["after"] == {
        "access_restricted": True,
        "shared_group_ids": [TEAM_A],
        "shared_user_ids": [ALICE],
        "excluded_group_ids": [],
        "excluded_user_ids": [],
    }


def test_an_unchanged_write_is_no_change(known_ids):
    cfg = _cfg(groups=[TEAM_A])
    assert _write(cfg, access_restricted=True, group_ids=[TEAM_A]) is None
    assert _write(cfg) is None


def test_opening_a_provider_keeps_its_lists_for_the_next_restriction(known_ids):
    cfg = _cfg(groups=[TEAM_A], users=[ALICE])
    _write(cfg, access_restricted=False)
    assert cfg.access_restricted is False
    assert cfg.shared_group_ids == [TEAM_A]
    assert cfg.shared_user_ids == [ALICE]
    _write(cfg, access_restricted=True)
    assert provider_usable_by(cfg, AUDIENCES[ALICE])


def test_only_an_id_the_write_adds_is_checked(known_ids):
    # Bob was granted, then deleted: his id no longer names anyone.
    known_ids.known.add(CAROL)
    cfg = _cfg(users=[BOB])
    _write(cfg, user_ids=[BOB, CAROL])
    assert known_ids.looked_up == [CAROL]
    assert cfg.shared_user_ids == [BOB, CAROL]


def test_an_unknown_added_id_is_refused_and_nothing_is_written(known_ids):
    cfg = _cfg(groups=[TEAM_A])
    with pytest.raises(ProviderAccessError, match="Unknown user"):
        _write(cfg, access_restricted=True, user_ids=[CAROL])
    assert cfg.shared_user_ids is None
    with pytest.raises(ProviderAccessError, match="Unknown group"):
        _write(cfg, group_ids=[TEAM_A, TEAM_B])
    assert cfg.shared_group_ids == [TEAM_A]


def test_a_malformed_id_is_refused(known_ids):
    with pytest.raises(ProviderAccessError, match="Invalid group id"):
        _write(_cfg(), group_ids=["not-a-uuid"])


def test_an_audience_is_bounded(known_ids):
    many = [str(uuid.uuid4()) for _ in range(provider_access.MAX_AUDIENCE_IDS + 1)]
    with pytest.raises(ProviderAccessError, match="at most"):
        _write(_cfg(), user_ids=many)


def test_the_bound_counts_groups_and_users_together(known_ids):
    half = provider_access.MAX_AUDIENCE_IDS // 2 + 1
    groups = [str(uuid.uuid4()) for _ in range(half)]
    users = [str(uuid.uuid4()) for _ in range(half)]
    known_ids.known.update(groups + users)
    with pytest.raises(ProviderAccessError, match="at most"):
        _write(_cfg(), group_ids=groups, user_ids=users)
    # A write naming one list only is bounded by the list already stored.
    cfg = _cfg(users=users)
    with pytest.raises(ProviderAccessError, match="at most"):
        _write(cfg, group_ids=groups)
    assert cfg.shared_group_ids is None


def test_emptied_lists_are_stored_as_none(known_ids):
    cfg = _cfg(groups=[TEAM_A], users=[ALICE])
    _write(cfg, group_ids=[], user_ids=[])
    assert cfg.shared_group_ids is None
    assert cfg.shared_user_ids is None


# ── the router, for a caller ─────────────────────────────────────────────


class _Rows:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def one_or_none(self):
        return None  # no stored model preference


class _Session:
    def __init__(self, *results):
        self._results = list(results)

    async def execute(self, _statement):
        if not self._results:
            raise AssertionError("Unexpected extra execute() call")
        return self._results.pop(0)


@pytest.fixture
def audiences(monkeypatch):
    async def _load(_db, user_id, *, with_groups=True):
        return AUDIENCES.get(provider_access.canonical_id(user_id) or "", NO_ACCOUNT)

    monkeypatch.setattr(provider_access, "load_provider_audience", _load)


@pytest.fixture
def instances(monkeypatch):
    async def _platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _platform_settings)
    monkeypatch.setattr(router, "_get_cached_provider", lambda cfg: SimpleNamespace(config=cfg))


def _run_for(user_id, configs, **kwargs):
    db = _Session(_Rows(configs), *([_Rows()] if user_id else []))
    provider, model = asyncio.run(router.get_llm_for_user(db, user_id, **kwargs))
    return provider, model


def test_one_agent_runs_on_each_callers_team_key(audiences, instances):
    open_cfg = _cfg()
    team_a = _cfg(groups=[TEAM_A])
    team_b = _cfg(groups=[TEAM_B])
    configs = [open_cfg, team_a, team_b]
    for user, expected in ((ALICE, team_a), (BOB, team_b), (CAROL, open_cfg), (None, open_cfg)):
        provider, model = _run_for(user, configs, model_override="claude-sonnet-4-6")
        assert provider.config is expected, user
        assert model == "claude-sonnet-4-6"


def test_an_agent_pinned_to_another_teams_key_runs_on_the_callers(audiences, instances):
    team_a = _cfg(groups=[TEAM_A])
    team_b = _cfg(groups=[TEAM_B])
    provider, model = _run_for(
        ALICE, [team_a, team_b], model_override="claude-sonnet-4-6", provider_id_override=team_b.id
    )
    assert provider.config is team_a
    assert model == "claude-sonnet-4-6"


def test_a_caller_with_no_usable_provider_is_told_so(audiences, instances):
    with pytest.raises(ValueError, match="No LLM provider is available to you"):
        _run_for(CAROL, [_cfg(groups=[TEAM_A])], model_override="claude-sonnet-4-6")
    assert "Settings > AI and models > Models" in NO_USABLE_PROVIDER_MESSAGE


def test_resolving_a_model_stays_within_the_callers_providers(audiences, instances):
    open_cfg = _cfg(provider="openai", models=("gpt-5-mini",))
    team_b = _cfg(groups=[TEAM_B])

    def _resolve(user_id):
        session = _Session(_Rows([open_cfg, team_b]))
        return asyncio.run(router.resolve_provider_for_model(session, "claude-sonnet-4-6", user_id=user_id))

    provider, _ = _resolve(BOB)
    assert provider.config is team_b
    with pytest.raises(ProviderNotUsableError, match="reserved for other groups"):
        _resolve(ALICE)


def test_an_explicit_model_tells_no_access_from_no_such_model(audiences, instances):
    open_cfg = _cfg(provider="openai", models=("gpt-5-mini",))
    team_b = _cfg(groups=[TEAM_B])

    def _resolve(user_id, model, configs):
        session = _Session(_Rows(configs))
        return asyncio.run(router.resolve_provider_for_model(session, model, user_id=user_id))

    # Every provider is another team's: a matter of access, whatever the model.
    with pytest.raises(ProviderNotUsableError) as refused:
        _resolve(ALICE, "gpt-5-mini", [team_b])
    assert str(refused.value) == NO_USABLE_PROVIDER_MESSAGE
    # A model only another team's provider lists: access too, naming the model.
    with pytest.raises(ProviderNotUsableError, match="'claude-sonnet-4-6' is only offered"):
        _resolve(ALICE, "claude-sonnet-4-6", [open_cfg, team_b])
    # A model nobody offers stays the plain "not available".
    with pytest.raises(ValueError, match="not available") as unknown:
        _resolve(ALICE, "mistral-large-3", [open_cfg, team_b])
    assert not isinstance(unknown.value, ProviderNotUsableError)
    assert "Settings > AI and models > Models" in str(unknown.value)


def test_the_anthropic_pass_through_refuses_a_matter_of_access_as_403(audiences):
    from fastapi import HTTPException

    from app.api.v1 import llm_proxy

    open_openai = _cfg(provider="openai", models=("gpt-5-mini",))
    team_b = _cfg(groups=[TEAM_B])

    def _config(user_id, model, configs):
        session = _Session(_Rows(configs))
        return asyncio.run(llm_proxy._get_anthropic_config(session, model, user_id=user_id))

    assert _config(BOB, "claude-sonnet-4-6", [team_b]) is team_b
    with pytest.raises(HTTPException) as reserved:
        _config(ALICE, "claude-sonnet-4-6", [open_openai, team_b])
    assert reserved.value.status_code == 403
    assert "reserved for other groups" in reserved.value.detail
    with pytest.raises(HTTPException) as withheld:
        _config(ALICE, "claude-opus-4-6", [team_b])
    assert withheld.value.status_code == 403
    assert withheld.value.detail == NO_USABLE_PROVIDER_MESSAGE


def test_the_model_list_is_the_callers_unless_every_provider_is_asked(audiences):
    open_cfg = _cfg(provider="openai", models=("gpt-5-mini",))
    team_a = _cfg(models=("claude-sonnet-4-6",), groups=[TEAM_A])
    team_b = _cfg(models=("claude-opus-4-6",), groups=[TEAM_B])
    configs = [open_cfg, team_a, team_b]

    def _models(**kwargs):
        rows = asyncio.run(router.get_available_models(_Session(_Rows(configs)), **kwargs))
        return {(r["model"], r["access_restricted"]) for r in rows}

    assert _models(user_id=ALICE) == {("gpt-5-mini", False), ("claude-sonnet-4-6", True)}
    assert _models() == {("gpt-5-mini", False)}
    assert _models(every_provider=True) == {
        ("gpt-5-mini", False),
        ("claude-sonnet-4-6", True),
        ("claude-opus-4-6", True),
    }


def test_a_scoped_model_write_refuses_another_teams_model(audiences):
    configs = [_cfg(provider="openai", models=("gpt-5-mini",)), _cfg(groups=[TEAM_B])]

    def _validate(model, **kwargs):
        return asyncio.run(router.validate_selectable_chat_model(_Session(_Rows(configs)), model, **kwargs))

    problem = _validate("claude-sonnet-4-6", user_id=ALICE, scoped=True)
    assert problem is not None
    assert "do not have access to" in problem
    assert "gpt-5-mini" in problem
    assert _validate("claude-sonnet-4-6", user_id=BOB, scoped=True) is None
    # An admin, or a platform-wide setting, keeps the whole catalog.
    assert _validate("claude-sonnet-4-6", user_id=ALICE) is None


# ── keys a capability spends directly (speech, web search) ───────────────


def _keyed(provider="openai", *, key, default=False, **kwargs):
    cfg = _cfg(provider=provider, models=(), **kwargs)
    cfg.api_key_enc = key
    cfg.is_default = default
    cfg.default_model = None
    return cfg


def test_a_vendor_key_is_the_callers_team_first_then_the_default(audiences):
    open_cfg = _keyed(key="open")
    open_default = _keyed(key="open-default", default=True)
    team_a = _keyed(key="team-a", groups=[TEAM_A])
    team_b = _keyed(key="team-b", groups=[TEAM_B])
    configs = [open_cfg, open_default, team_a, team_b]

    def _usable(user_id):
        return asyncio.run(provider_access.usable_provider_configs(_Session(_Rows(configs)), user_id, ("openai",)))

    assert _usable(ALICE) == [team_a, open_default, open_cfg]
    assert _usable(BOB) == [team_b, open_default, open_cfg]
    assert _usable(None) == [open_default, open_cfg]


def test_web_search_spends_only_a_key_the_caller_may_use(audiences, monkeypatch):
    from app.services.investigation_queriers import resolve_search_credentials

    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda value: value)
    open_openai = _keyed(key="open-openai")
    team_a = _keyed("anthropic", key="team-a-anthropic", groups=[TEAM_A])
    team_b = _keyed("anthropic", key="team-b-anthropic", groups=[TEAM_B])
    configs = [open_openai, team_a, team_b]

    def _search_key(user_id):
        found = asyncio.run(resolve_search_credentials(_Session(_Rows(configs)), user_id=user_id))
        return found[1] if found else None

    # The vendor preference decides first (Anthropic), then the caller's key.
    assert _search_key(ALICE) == "team-a-anthropic"
    assert _search_key(BOB) == "team-b-anthropic"
    # Neither Anthropic key is theirs: the open OpenAI one, never another team's.
    assert _search_key(CAROL) == "open-openai"
    assert _search_key(None) == "open-openai"


def test_speech_runs_on_the_callers_openai_key(audiences, monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import chat

    def _decrypt(value):
        if value == "rotated":
            raise ValueError("InvalidToken")
        return value

    monkeypatch.setattr("app.core.security.decrypt_stored_value", _decrypt)
    open_cfg = _keyed(key="open")
    team_a = _keyed(key="rotated", groups=[TEAM_A])
    team_b = _keyed(key="team-b", groups=[TEAM_B])
    configs = [open_cfg, team_a, team_b]

    def _key(user_id, rows=configs):
        user = SimpleNamespace(id=uuid.UUID(user_id))
        return asyncio.run(chat._speech_api_key(_Session(_Rows(rows)), user, "text-to-speech"))

    # Two OpenAI providers are no longer ambiguous: each caller gets theirs.
    assert _key(BOB) == "team-b"
    assert _key(CAROL) == "open"
    # A key that cannot be decrypted is skipped, not a 500.
    assert _key(ALICE) == "open"
    with pytest.raises(HTTPException) as refused:
        _key(CAROL, rows=[team_a, team_b])
    assert refused.value.status_code == 400
    assert "available to you" in refused.value.detail


def test_speech_reports_a_vault_it_cannot_read_rather_than_no_provider(audiences, monkeypatch):
    from app.api.v1 import chat
    from app.services import vault
    from app.services.vault.errors import SecretUnavailableError

    async def _resolve(text, owner=None):
        if text.startswith("vault://"):
            raise SecretUnavailableError("unreachable")
        return text

    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda value: value)
    monkeypatch.setattr(vault, "resolve_secret_text", _resolve)
    in_vault = _keyed(key="vault://corp/openai#key")
    user = SimpleNamespace(id=uuid.UUID(CAROL))

    def _key(rows):
        return asyncio.run(chat._speech_api_key(_Session(_Rows(rows)), user, "text-to-speech"))

    with pytest.raises(SecretUnavailableError):
        _key([in_vault])
    assert _key([in_vault, _keyed(key="open")]) == "open"

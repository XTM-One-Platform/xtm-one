"""A live model for a lab (``model_keys``): a short-lived LLM-proxy key minted per lab, never a provider secret."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.sandbox_workspace import labs, model_keys
from tests.test_agentic_labs import (  # noqa: F401
    _Kube,
    _no_sleep,
    _resolved,
    _workspace,
    lab_settings,
    no_quota,
    redis,
)

RAW = "fcp-" + "ab" * 24


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(settings, "agentic_labs_model_key_account", "labs@filigran.io")
    monkeypatch.setattr(settings, "agentic_labs_model_base_url", "https://copilot.example/v1/")
    monkeypatch.setattr(settings, "agentic_labs_model_names", "gpt-lab")
    monkeypatch.setattr(settings, "agentic_labs_model_key_hours", 12)


def _opted(resolved, value="live"):
    chain = tuple(
        SimpleNamespace(**{**spec.__dict__, "env": (*spec.env, ("LAB_MODEL", value))}) for spec in resolved.chain
    )
    return SimpleNamespace(**{**resolved.__dict__, "chain": chain})


class _Session:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def commit(self):
        self.store["commits"] += 1


@pytest.fixture
def minted_rows(monkeypatch):
    store = {"rows": [], "commits": 0}

    async def create(_db, *, account, name, now):
        row = SimpleNamespace(
            id=uuid.uuid4(),
            key_prefix=RAW[:12] + "...",
            expires_at=datetime(2026, 11, 7, 22, tzinfo=UTC),
            account=account,
            name=name,
            surfaces=["llm_proxy"],
        )
        store["rows"].append(row)
        return row, RAW

    revoked: list[str] = []

    async def revoke(key_id, **_):
        if key_id:
            revoked.append(key_id)
        return bool(key_id)

    monkeypatch.setattr(model_keys, "_create", create)
    store["factory"] = lambda: _Session(store)
    store["revoked"] = revoked
    store["revoke"] = revoke
    return store


@pytest.mark.usefixtures("lab_settings")
async def test_a_lab_that_does_not_opt_in_gets_no_key(live, minted_rows):
    resolved = await _resolved(_workspace())
    minted = await model_keys.mint(resolved, pod="lab-1", session_factory=minted_rows["factory"])
    assert minted.env == {} and minted.key_id == "" and minted.summary() == "" and minted_rows["rows"] == []


@pytest.mark.usefixtures("lab_settings")
@pytest.mark.parametrize("missing", ["agentic_labs_model_key_account", "agentic_labs_model_base_url"])
async def test_an_opted_in_lab_on_an_unconfigured_platform_keeps_the_stub_and_says_why(
    live, minted_rows, monkeypatch, missing
):
    monkeypatch.setattr(settings, missing, "")
    minted = await model_keys.mint(
        _opted(await _resolved(_workspace())), pod="lab-1", session_factory=minted_rows["factory"]
    )
    assert minted.env == {} and minted_rows["rows"] == []
    assert "stub model" in minted.summary() and "AGENTIC_LABS_MODEL_KEY_ACCOUNT" in minted.summary()


@pytest.mark.usefixtures("lab_settings")
async def test_an_opted_in_lab_gets_a_proxy_key_its_url_and_its_models_and_the_summary_hides_the_key(live, minted_rows):
    minted = await model_keys.mint(
        _opted(await _resolved(_workspace())), pod="lab-1", session_factory=minted_rows["factory"]
    )
    assert minted.env == {
        "LAB_MODEL_API_KEY": RAW,
        "LAB_MODEL_BASE_URL": "https://copilot.example/v1",
        "LAB_MODEL_NAMES": "gpt-lab",
    }
    (row,) = minted_rows["rows"]
    assert row.account == "labs@filigran.io" and row.name == "agentic lab lab-1" and minted_rows["commits"] == 1
    assert minted.key_id == str(row.id) and RAW not in minted.summary() and "LLM proxy only" in minted.summary()


@pytest.mark.usefixtures("lab_settings")
async def test_a_key_that_cannot_be_created_never_fails_the_lab(live, monkeypatch):
    async def create(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(model_keys, "_create", create)
    minted = await model_keys.mint(
        _opted(await _resolved(_workspace())), pod="lab-1", session_factory=lambda: _Session({"commits": 0})
    )
    assert minted.env == {} and "could not be created (RuntimeError)" in minted.summary()


class _Lookup:
    """A session whose user lookup answers *user* (None: no such email)."""

    def __init__(self, user):
        self.user = user
        self.added: list = []

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.user)

    def add(self, row):
        self.added.append(row)


@pytest.mark.parametrize(
    ("user", "why"),
    [
        (None, "no user of this platform signs in as labs@filigran.io"),
        (SimpleNamespace(id=uuid.uuid4(), is_active=False), "the platform user labs@filigran.io is disabled"),
    ],
)
async def test_an_account_that_cannot_hold_the_key_is_named_for_what_it_is(live, user, why):
    db = _Lookup(user)
    created = await model_keys._create(db, account="labs@filigran.io", name="agentic lab lab-1", now=datetime.now(UTC))
    assert isinstance(created, str) and why in created and db.added == []
    if user is None:
        assert "not a mailbox or an integration account" in created


async def test_revoke_refuses_an_id_that_is_no_uuid():
    assert await model_keys.revoke("") is False
    assert await model_keys.revoke("not-a-uuid") is False


@pytest.mark.usefixtures("lab_settings", "no_quota", "redis")
async def test_a_live_lab_pod_carries_the_key_the_record_does_not_and_the_stop_revokes_it(
    live, minted_rows, monkeypatch
):
    monkeypatch.setattr(model_keys, "revoke", minted_rows["revoke"])
    original = model_keys.mint

    async def mint(resolved, *, pod):
        return await original(_opted(resolved), pod=pod, session_factory=minted_rows["factory"])

    monkeypatch.setattr(model_keys, "mint", mint)
    workspace = _workspace()
    fake = _Kube()
    outcome = await labs.start_lab(await _resolved(workspace), workspace=workspace, client=fake, sleep=_no_sleep)
    assert outcome.ready, outcome.text
    (pod,) = fake.pods.values()
    for container in pod["spec"]["containers"]:
        env = {item["name"]: item["value"] for item in container["env"]}
        assert env["LAB_MODEL_API_KEY"] == RAW and env["LAB_MODEL_BASE_URL"] == "https://copilot.example/v1"
    (row,) = minted_rows["rows"]
    assert pod["metadata"]["annotations"][labs.ANNOTATION_MODEL_KEY] == str(row.id)
    assert "Live model: LAB_MODEL_API_KEY" in outcome.text and RAW not in outcome.text
    held = (await workspace.record(create=False)).kernel_state["labs"]["co:lab"]
    assert RAW not in json.dumps(held) and held["model_key_id"] == str(row.id)
    await labs.stop_lab(workspace, outcome.service_id, client=fake)
    assert minted_rows["revoked"] == [str(row.id)]


@pytest.mark.usefixtures("lab_settings")
async def test_the_reaper_revokes_the_key_of_a_pod_it_deletes(monkeypatch):
    revoked: list[str] = []

    async def revoke(key_id, **_):
        revoked.append(key_id)
        return True

    monkeypatch.setattr(model_keys, "revoke", revoke)
    fake = _Kube()
    resolved = await _resolved(_workspace())
    manifest = labs.pod_manifest(resolved, name="lab-9", image="i", workspace_id="w", model_key_id="k-1")
    manifest["status"] = {"phase": "Failed"}
    fake.pods["lab-9"] = manifest
    assert await labs.reap(client=fake) == ["lab-9"]
    assert revoked == ["k-1"]

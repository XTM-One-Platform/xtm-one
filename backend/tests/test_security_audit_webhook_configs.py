"""The Security Audit names the stored webhook configurations whose deliveries are refused.

Two refusals of the webhook gates show up nowhere an admin looks, only as a
sender's failed deliveries:

- a stored configuration the current ``SECRET_KEY`` cannot read (undecryptable,
  not JSON, not an object) refuses every delivery with a 503, and so
  does the LinkedIn OAuth default's client secret on the LinkedIn provider route;
- a webhook secret, or HubSpot client secret, that is not text refuses every
  delivery with a 401: no sender can sign with it.

The audit lists both, reading each configuration through the gates' own
readers (``decrypt_webhook_config``, ``decrypt_webhook_client_secret``,
``configured_secret``, ``hubspot_webhook_secret``). The agreement tests run the
integration and provider routes on the same configurations, so what the audit
names is exactly what the gates refuse. Decryption runs off the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import uuid
from collections import namedtuple
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from starlette.datastructures import Headers

from app.api.v1 import security_audit, webhooks
from app.core import security
from app.core.utils import format_exception_message

SECRET = "s3cr3t"
_BODY = b'{"action":"opened"}'
_HUBSPOT_BODY = b"[]"


def _stored(value: object) -> str:
    """*value* as the platform stores a configuration: JSON, encrypted with the current key."""
    return security.encrypt_value(json.dumps(value))


def _under_another_key(plaintext: str) -> str:
    """What a changed ``SECRET_KEY`` leaves in the database."""
    return Fernet(Fernet.generate_key()).encrypt(plaintext.encode()).decode()


#: Stored configurations the gates cannot read, by why.
_UNREADABLE = {
    "another-key": lambda: _under_another_key(json.dumps({"webhook_secret": SECRET})),
    "not-a-token": lambda: "not-a-fernet-token",
    "not-json": lambda: security.encrypt_value("not json"),
    "not-an-object": lambda: _stored(["a", "list"]),
    "json-null": lambda: security.encrypt_value("null"),
}

#: Stored configurations the gates read, whatever they hold.
_READABLE = {
    "no-config": lambda: None,
    "empty": lambda: "",
    "a-secret": lambda: _stored({"webhook_secret": SECRET}),
    "no-secret": lambda: _stored({"base_url": "https://example.test"}),
    "a-secret-that-is-not-text": lambda: _stored({"webhook_secret": True}),
}


# -- The database the audit reads ----------------------------------------------


class _AuditDb:
    """``execute`` answering a SELECT with the records of its table, shaped as
    the columns the statement selects (order and filters are the caller's)."""

    def __init__(self, integrations=(), oauth_defaults=()):
        self._records = {"integrations": list(integrations), "oauth_defaults": list(oauth_defaults)}

    async def execute(self, statement, *_args, **_kwargs):
        [table] = {table.name for table in statement.get_final_froms()}
        keys = [column.key for column in statement.selected_columns]
        row = namedtuple("Row", keys)
        rows = [row(*(record.get(key) for key in keys)) for record in self._records[table]]
        return SimpleNamespace(all=lambda: rows)


def _integration(name: str, provider: str, extra_config_enc: str | None) -> dict:
    return {"id": uuid.uuid4(), "name": name, "provider": provider, "extra_config_enc": extra_config_enc}


def _oauth_default(provider: str, extra_config_enc: str | None = None, client_secret_enc: str | None = None) -> dict:
    return {
        "provider": provider,
        "extra_config_enc": extra_config_enc,
        "client_secret_enc": client_secret_enc or security.encrypt_value("client-secret"),
    }


async def _webhook_config_findings(**records) -> list[security_audit.AuditFinding]:
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_webhook_configs(_AuditDb(**records), findings)
    return findings


async def _unreadable_findings(**records) -> list[security_audit.AuditFinding]:
    return [
        f for f in await _webhook_config_findings(**records) if f.category == "encryption" and f.severity != "pass"
    ]


async def _unusable_findings(**records) -> list[security_audit.AuditFinding]:
    return [f for f in await _webhook_config_findings(**records) if f.category == "integrations"]


def _named(finding: security_audit.AuditFinding, record: dict) -> bool:
    """Whether *finding* names the integration *record* (by name and id)."""
    return f"{record['name']} (" in finding.description and str(record["id"]) in finding.description


# -- Unreadable configurations: the 503 -------------------------------


async def test_the_audit_names_the_configurations_the_current_key_cannot_read():
    readable = _integration("Slack alerts", "slack", _stored({"webhook_secret": SECRET}))
    unreadable = [
        _integration("GitHub prod", "github", _UNREADABLE["another-key"]()),
        _integration("Jira", "jira", _UNREADABLE["not-json"]()),
        _integration("HubSpot CRM", "hubspot", _UNREADABLE["not-an-object"]()),
    ]
    defaults = [
        _oauth_default("github", _stored({"webhook_secret": SECRET})),
        _oauth_default("microsoft", _UNREADABLE["another-key"]()),
        # The LinkedIn provider route decrypts the client secret on every delivery...
        _oauth_default("linkedin", _stored({}), client_secret_enc=_under_another_key("client-secret")),
        # ...another provider's route only for a delivery carrying X-LI-Signature, not every one.
        _oauth_default("slack", None, client_secret_enc=_under_another_key("client-secret")),
    ]

    [finding] = await _unreadable_findings(integrations=[readable, *unreadable], oauth_defaults=defaults)

    assert (finding.category, finding.severity) == ("encryption", "warning")
    assert finding.title == "5 stored webhook configuration(s) cannot be read"
    for record in unreadable:
        assert _named(finding, record)
    assert "Microsoft OAuth default" in finding.description
    assert "LinkedIn OAuth default" in finding.description
    assert not _named(finding, readable)
    assert "GitHub OAuth default" not in finding.description
    assert "Slack OAuth default" not in finding.description
    assert "refused" in finding.description
    assert "503" in finding.description
    assert "saved again with the current key" in finding.description
    assert "SECRET_KEY" in (finding.recommendation or "")
    assert "OAuth Defaults" in (finding.recommendation or "")
    # The refusal is the only behaviour: nothing is accepted unverified, nothing to opt into.
    assert "accepted without signature verification" not in finding.description
    assert "WEBHOOK_REFUSE" not in finding.description + (finding.recommendation or "")


@pytest.mark.parametrize("why", sorted(_UNREADABLE))
async def test_every_way_a_configuration_cannot_be_read_is_listed(why):
    record = _integration("Broken", "github", _UNREADABLE[why]())
    [finding] = await _unreadable_findings(integrations=[record])
    assert finding.title.startswith("1 ")
    assert _named(finding, record)


async def test_the_unreadable_finding_is_silent_when_every_configuration_reads():
    integrations = [_integration(f"Integration {why}", "github", make()) for why, make in _READABLE.items()]
    defaults = [
        _oauth_default("github", _stored({"webhook_secret": SECRET})),
        _oauth_default("linkedin", None),
        _oauth_default("hubspot", _stored({"webhook_secret": [SECRET]})),
    ]
    assert await _unreadable_findings(integrations=integrations, oauth_defaults=defaults) == []
    assert await _unreadable_findings() == []


async def test_every_configuration_read_is_a_pass_and_no_configuration_is_nothing():
    """No silent pass: configurations the gates can all read say so."""
    integrations = [_integration("GitHub prod", "github", _stored({"webhook_secret": SECRET}))]
    defaults = [_oauth_default("github", _stored({"webhook_secret": SECRET}))]
    [finding] = await _webhook_config_findings(integrations=integrations, oauth_defaults=defaults)
    assert (finding.id, finding.severity) == ("encryption.webhook_configs_readable", "pass")
    assert finding.title == "All 2 stored webhook configuration(s) can be read"
    # One unreadable configuration takes the pass away; nothing stored reports nothing.
    unreadable = _integration("Broken", "github", _UNREADABLE["another-key"]())
    findings = await _webhook_config_findings(integrations=[*integrations, unreadable])
    assert "encryption.webhook_configs_readable" not in {f.id for f in findings}
    assert await _webhook_config_findings() == []


async def test_the_unreadable_finding_names_five_configurations_at_most():
    records = [_integration(f"Integration {i}", "github", _UNREADABLE["another-key"]()) for i in range(8)]
    [finding] = await _unreadable_findings(integrations=records)
    assert finding.title.startswith("8 ")
    assert [_named(finding, record) for record in records] == [True] * 5 + [False] * 3
    assert "and 3 more" in finding.description


async def test_an_oauth_default_is_named_before_the_integrations():
    records = [_integration(f"Integration {i}", "github", _UNREADABLE["not-json"]()) for i in range(6)]
    [finding] = await _unreadable_findings(
        integrations=records, oauth_defaults=[_oauth_default("github", _UNREADABLE["not-json"]())]
    )
    assert finding.title.startswith("7 ")
    assert finding.description.index("GitHub OAuth default") < finding.description.index("Integration 0 (")
    assert "and 2 more" in finding.description


# -- Secrets that are not text: the 401 --------------------------------


async def test_the_audit_names_the_configurations_whose_secret_is_not_text():
    refused = [
        _integration("OpenCTI feed", "opencti", _stored({"webhook_secret": True})),
        _integration("n8n flows", "n8n", _stored({"webhook_secret": False})),
        _integration("HubSpot CRM", "hubspot", _stored({"client_secret": {}, "webhook_secret": 1.5})),
    ]
    accepted = [
        _integration("GitHub prod", "github", _stored({"webhook_secret": SECRET})),
        _integration("Grafana", "grafana", _stored({"webhook_secret": 12345})),
        _integration("Jira", "jira", _stored({"base_url": "https://jira.example"})),
        _integration("HubSpot app", "hubspot", _stored({"client_secret": ["cs"], "webhook_secret": "ws"})),
        # Unreadable: the other finding's, never counted here.
        _integration("Broken", "github", _UNREADABLE["another-key"]()),
        _integration("Not an object", "github", _UNREADABLE["not-an-object"]()),
    ]
    defaults = [
        _oauth_default("github", _stored({"webhook_secret": [SECRET]})),
        _oauth_default("microsoft", _stored({"tenant_id": "t", "webhook_secret": "ws"})),
        _oauth_default("slack", _UNREADABLE["not-json"]()),
    ]

    [finding] = await _unusable_findings(integrations=[*refused, *accepted], oauth_defaults=defaults)

    assert (finding.category, finding.severity) == ("integrations", "warning")
    assert finding.title == "4 webhook configuration(s) with a secret that is not text"
    for record in refused:
        assert _named(finding, record)
    assert "GitHub OAuth default" in finding.description
    for record in accepted:
        assert not _named(finding, record)
    assert "Microsoft OAuth default" not in finding.description
    assert "Slack OAuth default" not in finding.description
    assert "refused" in finding.description
    assert "401" in finding.description
    assert "replaced by a string" in finding.description
    assert "as text" in (finding.recommendation or "")
    assert "accepted without signature verification" not in finding.description
    assert "WEBHOOK_REFUSE" not in finding.description + (finding.recommendation or "")


async def test_the_non_string_finding_never_counts_a_configuration_it_cannot_read():
    """Every configuration unreadable: the first finding lists them all, this one none."""
    integrations = [_integration(f"Broken {why}", "github", make()) for why, make in _UNREADABLE.items()]
    defaults = [_oauth_default("github", _UNREADABLE["another-key"]())]
    assert await _unusable_findings(integrations=integrations, oauth_defaults=defaults) == []
    [unreadable] = await _unreadable_findings(integrations=integrations, oauth_defaults=defaults)
    assert unreadable.title.startswith(f"{len(integrations) + 1} ")


async def test_a_readable_config_with_an_unreadable_client_secret_is_checked_for_its_secret_too():
    """LinkedIn: the config reads, so its secret is checked; the client secret does not, so it is listed as unreadable."""
    default = _oauth_default(
        "linkedin", _stored({"webhook_secret": True}), client_secret_enc=_under_another_key("client-secret")
    )
    [unreadable, unusable] = await _webhook_config_findings(oauth_defaults=[default])
    assert (unreadable.category, unusable.category) == ("encryption", "integrations")
    assert "LinkedIn OAuth default" in unreadable.description
    assert "LinkedIn OAuth default" in unusable.description


async def test_the_non_string_finding_is_silent_when_every_secret_is_text():
    integrations = [
        _integration("GitHub prod", "github", _stored({"webhook_secret": SECRET})),
        _integration("Grafana", "grafana", _stored({"webhook_secret": 7})),
        _integration("RSS", "rss", None),
        _integration("HubSpot", "hubspot", _stored({"client_secret": "cs", "webhook_secret": True})),
    ]
    defaults = [_oauth_default("github", _stored({"webhook_secret": SECRET})), _oauth_default("linkedin", None)]
    assert await _unusable_findings(integrations=integrations, oauth_defaults=defaults) == []
    assert await _unusable_findings() == []


async def test_the_non_string_finding_names_five_configurations_at_most():
    records = [_integration(f"Integration {i}", "github", _stored({"webhook_secret": [i]})) for i in range(6)]
    [finding] = await _unusable_findings(integrations=records)
    assert finding.title.startswith("6 ")
    assert [_named(finding, record) for record in records] == [True] * 5 + [False]
    assert "and 1 more" in finding.description


# -- Off the event loop, and registered ----------------------------------------


def _on_the_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


async def test_each_stored_value_is_decrypted_once_off_the_event_loop(monkeypatch):
    calls: list[bool] = []

    def _spying(decrypt):
        def _spy(ciphertext: str) -> str:
            calls.append(_on_the_event_loop())
            return decrypt(ciphertext)

        return _spy

    monkeypatch.setattr(webhooks, "decrypt_value", _spying(webhooks.decrypt_value))
    # The LinkedIn client secret is read as stored (a vault reference is then resolved).
    monkeypatch.setattr(security_audit, "decrypt_stored_value", _spying(security_audit.decrypt_stored_value))
    records = {
        "integrations": [
            _integration("GitHub prod", "github", _stored({"webhook_secret": True})),
            _integration("Broken", "github", _UNREADABLE["another-key"]()),
        ],
        "oauth_defaults": [_oauth_default("linkedin", _stored({"webhook_secret": SECRET}))],
    }
    findings = await _webhook_config_findings(**records)
    assert [f.category for f in findings] == ["encryption", "integrations"]
    assert len(calls) == 4  # two integrations, the LinkedIn config and client secret: once each
    assert not any(calls), "a stored configuration was decrypted on the event loop"


async def test_a_linkedin_client_secret_its_vault_does_not_give_is_named_unreadable(monkeypatch):
    """A client secret held as a vault reference is read the gate's way: an unreadable vault is named."""
    from app.services.vault import SecretUnavailableError

    reads: list[str] = []

    async def _vault(ciphertext, owner=None):
        reads.append(security.decrypt_stored_value(ciphertext))
        raise SecretUnavailableError("not_found")

    monkeypatch.setattr(webhooks, "reveal_ciphertext", _vault)
    records = {
        "oauth_defaults": [
            _oauth_default(
                "linkedin",
                _stored({"webhook_secret": SECRET}),
                security.encrypt_value("vault://corp/linkedin#client_secret"),
            ),
            _oauth_default("github", _stored({"webhook_secret": SECRET})),
        ]
    }
    [finding] = await _unreadable_findings(**records)
    assert "LinkedIn OAuth default" in finding.description
    assert "GitHub" not in finding.description
    assert reads == ["vault://corp/linkedin#client_secret"]


async def test_a_linkedin_client_secret_its_vault_gives_is_not_named(monkeypatch):
    async def _vault(ciphertext, owner=None):
        return "from-the-vault"

    monkeypatch.setattr(webhooks, "reveal_ciphertext", _vault)
    records = {
        "oauth_defaults": [
            _oauth_default(
                "linkedin",
                _stored({"webhook_secret": SECRET}),
                security.encrypt_value("vault://corp/linkedin#client_secret"),
            )
        ]
    }
    assert await _unreadable_findings(**records) == []


async def test_the_security_audit_runs_both_checks(monkeypatch):
    ran: list[str] = []
    for name, check in list(vars(security_audit).items()):
        if name.startswith("_check_") and inspect.iscoroutinefunction(check):

            async def _record(*_args, _name=name, **_kwargs):
                ran.append(_name)

            monkeypatch.setattr(security_audit, name, _record)

    await security_audit.run_security_audit(admin=SimpleNamespace(), db=SimpleNamespace())

    assert "_check_webhook_configs" in ran


# -- Agreement with the gates: listed exactly when a route refuses --------------


class _ReachedDedupClaimError(Exception):
    """The delivery got past the gate: the route reached its dedup claim."""


class _Delivery(SimpleNamespace):
    async def body(self) -> bytes:
        return self._body


def _delivery(pairs=(), body: bytes = _BODY) -> _Delivery:
    headers = Headers(raw=[(name.lower().encode(), value) for name, value in pairs])
    return _Delivery(headers=headers, _body=body, method="POST", url="https://xtm.example/api/hooks/x")


class _Session:
    """``async_session()`` answering every query with *row* (and no agents)."""

    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, *_args, **_kwargs):
        return SimpleNamespace(scalar_one_or_none=lambda: self._row, all=lambda: [])


def _accept_at_the_dedup_claim(monkeypatch) -> None:
    async def _claim(*_args, **_kwargs):
        raise _ReachedDedupClaimError

    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", _claim)


async def _integration_route(monkeypatch, provider: str, extra_config_enc: str | None, delivery) -> int:
    """200 when the integration route lets *delivery* past its gate, else the refusal's status."""
    row = SimpleNamespace(id=uuid.uuid4(), provider=provider, extra_config_enc=extra_config_enc)
    monkeypatch.setattr(webhooks, "async_session", lambda: _Session(row))
    _accept_at_the_dedup_claim(monkeypatch)
    try:
        await webhooks.receive_integration_webhook(str(row.id), delivery)
    except _ReachedDedupClaimError:
        return 200
    except HTTPException as exc:
        return exc.status_code
    raise AssertionError("the route answered before its dedup claim")


async def _provider_route(monkeypatch, provider: str, extra_config_enc, client_secret_enc, delivery) -> int:
    """200 when the provider route lets *delivery* past its gate, else the refusal's status."""
    row = SimpleNamespace(id=uuid.uuid4(), extra_config_enc=extra_config_enc, client_secret_enc=client_secret_enc)
    monkeypatch.setattr(webhooks, "async_session", lambda: _Session(row))
    _accept_at_the_dedup_claim(monkeypatch)
    webhooks._invalidate_provider_routing_cache()
    try:
        await webhooks.receive_provider_webhook(provider, delivery)
    except _ReachedDedupClaimError:
        return 200
    except HTTPException as exc:
        return exc.status_code
    finally:
        webhooks._invalidate_provider_routing_cache()
    raise AssertionError("the route answered before its dedup claim")


def _held_secrets(config: dict) -> list[str]:
    """The secrets a sender could have been given for *config*, as text."""
    held = (config.get("client_secret"), config.get("webhook_secret"))
    return [value if isinstance(value, str) else json.dumps(value) for value in held if value is not None]


def _signed_deliveries(config: dict, *, hubspot: bool = False) -> list[_Delivery]:
    """An unsigned delivery, and one signed with each secret *config* holds."""
    body = _HUBSPOT_BODY if hubspot else _BODY
    deliveries = [_delivery(body=body)]
    for secret in _held_secrets(config):
        if hubspot:
            signature = hashlib.sha256((secret + body.decode()).encode()).hexdigest()
            deliveries.append(_delivery([("X-HubSpot-Signature", signature.encode())], body=body))
        else:
            signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            deliveries.append(_delivery([("X-Signature", signature.encode())], body=body))
    return deliveries


def _refused_every_time(answers: list[int]) -> bool:
    """Whether no delivery got through; a refusal of every one is the 401 of a wrong secret."""
    if 200 in answers:
        return False
    assert set(answers) == {401}, answers
    return True


@pytest.mark.parametrize(
    ("provider", "config", "refused"),
    [
        pytest.param("github", {"webhook_secret": SECRET}, False, id="text"),
        pytest.param("github", {"base_url": "https://github.example"}, False, id="none-configured"),
        pytest.param("n8n", {"webhook_secret": ""}, False, id="empty"),
        pytest.param("opencti", {"webhook_secret": 12345}, False, id="a-number-is-its-digits"),
        pytest.param("opencti", {"webhook_secret": True}, True, id="true"),
        pytest.param("n8n", {"webhook_secret": False}, True, id="false"),
        pytest.param("grafana", {"webhook_secret": 1.5}, True, id="float"),
        pytest.param("github", {"webhook_secret": [SECRET]}, True, id="list"),
        pytest.param("github", {"webhook_secret": {"k": SECRET}}, True, id="object"),
        pytest.param(
            "github", {"client_secret": True, "webhook_secret": SECRET}, False, id="only-hubspot-reads-client-secret"
        ),
        pytest.param("hubspot", {"client_secret": "cs"}, False, id="hubspot-client-secret"),
        pytest.param(
            "hubspot",
            {"client_secret": ["cs"], "webhook_secret": "ws"},
            False,
            id="hubspot-falls-back-to-webhook-secret",
        ),
        pytest.param(
            "hubspot", {"client_secret": "cs", "webhook_secret": True}, False, id="hubspot-client-secret-wins"
        ),
        pytest.param("hubspot", {"client_secret": {}, "webhook_secret": 1.5}, True, id="hubspot-neither-usable"),
        pytest.param("hubspot", {"webhook_secret": False}, True, id="hubspot-webhook-secret-unusable"),
        pytest.param("hubspot", {}, False, id="hubspot-none-configured"),
    ],
)
async def test_an_integration_is_listed_exactly_when_its_route_refuses_it_for_its_secret(
    monkeypatch, provider, config, refused
):
    extra_config_enc = _stored(config)
    answers = [
        await _integration_route(monkeypatch, provider, extra_config_enc, delivery)
        for delivery in _signed_deliveries(config, hubspot=provider == "hubspot")
    ]
    assert _refused_every_time(answers) is refused

    findings = await _unusable_findings(integrations=[_integration("Case", provider, extra_config_enc)])
    assert bool(findings) is refused


@pytest.mark.parametrize(
    ("provider", "config", "refused"),
    [
        pytest.param("github", {"webhook_secret": SECRET}, False, id="text"),
        pytest.param("github", {"webhook_secret": [SECRET]}, True, id="list"),
        pytest.param("microsoft", {"tenant_id": "t", "webhook_secret": 0.0}, True, id="float-zero"),
        pytest.param("hubspot", {"client_secret": True}, False, id="the-provider-route-reads-the-webhook-secret-only"),
        pytest.param("slack", {"webhook_secret": 7}, False, id="a-number-is-its-digits"),
        pytest.param("github", {}, False, id="none-configured"),
    ],
)
async def test_an_oauth_default_is_listed_exactly_when_its_route_refuses_it_for_its_secret(
    monkeypatch, provider, config, refused
):
    extra_config_enc = _stored(config)
    client_secret_enc = security.encrypt_value("client-secret")
    answers = [
        await _provider_route(monkeypatch, provider, extra_config_enc, client_secret_enc, delivery)
        for delivery in _signed_deliveries({"webhook_secret": config.get("webhook_secret")})
    ]
    assert _refused_every_time(answers) is refused

    findings = await _unusable_findings(oauth_defaults=[_oauth_default(provider, extra_config_enc, client_secret_enc)])
    assert bool(findings) is refused


@pytest.mark.parametrize("provider", ["github", "hubspot"])
@pytest.mark.parametrize(
    ("make", "unreadable"),
    [pytest.param(make, True, id=why) for why, make in _UNREADABLE.items()]
    + [pytest.param(make, False, id=why) for why, make in _READABLE.items()],
)
async def test_an_integration_is_listed_exactly_when_its_route_cannot_read_it(monkeypatch, provider, make, unreadable):
    extra_config_enc = make()
    body = _HUBSPOT_BODY if provider == "hubspot" else _BODY
    answer = await _integration_route(monkeypatch, provider, extra_config_enc, _delivery(body=body))
    assert (answer == 503) is unreadable

    findings = await _unreadable_findings(integrations=[_integration("Case", provider, extra_config_enc)])
    assert bool(findings) is unreadable


@pytest.mark.parametrize(
    ("provider", "extra_config_enc", "client_secret_enc", "unreadable"),
    [
        pytest.param("github", _UNREADABLE["another-key"], None, True, id="config-under-another-key"),
        pytest.param("microsoft", _UNREADABLE["not-an-object"], None, True, id="config-not-an-object"),
        pytest.param("github", _READABLE["a-secret"], None, False, id="config-readable"),
        pytest.param("github", _READABLE["no-config"], None, False, id="no-config"),
        pytest.param(
            "linkedin",
            _READABLE["no-config"],
            lambda: _under_another_key("client-secret"),
            True,
            id="linkedin-client-secret-under-another-key",
        ),
        pytest.param(
            "linkedin",
            _READABLE["no-secret"],
            lambda: "not-a-fernet-token",
            True,
            id="linkedin-client-secret-not-a-token",
        ),
        pytest.param("linkedin", _READABLE["no-config"], None, False, id="linkedin-client-secret-readable"),
        pytest.param(
            "github",
            _READABLE["no-config"],
            lambda: _under_another_key("client-secret"),
            False,
            id="other-client-secret-not-read-without-a-linkedin-signature",
        ),
    ],
)
async def test_an_oauth_default_is_listed_exactly_when_its_route_cannot_read_it(
    monkeypatch, provider, extra_config_enc, client_secret_enc, unreadable
):
    extra = extra_config_enc()
    client_secret = client_secret_enc() if client_secret_enc else security.encrypt_value("client-secret")
    answer = await _provider_route(monkeypatch, provider, extra, client_secret, _delivery())
    assert (answer == 503) is unreadable

    findings = await _unreadable_findings(oauth_defaults=[_oauth_default(provider, extra, client_secret)])
    assert bool(findings) is unreadable


# -- The gates answer as they did before their readers were split out ----------


def _undecryptable(_ciphertext: str) -> str:
    raise ValueError("InvalidToken")


def _json_error() -> ValueError:
    try:
        json.loads("not json")
    except ValueError as exc:
        return exc
    raise AssertionError("unreachable")


def _logged(caplog, level: int) -> str:
    [message] = [record.getMessage() for record in caplog.records if record.levelno == level]
    return message


def _assert_unverifiable(exc: HTTPException) -> None:
    assert (exc.status_code, exc.detail) == (503, "Webhook signature cannot be verified right now")
    assert exc.headers == {"Retry-After": "300"}


@pytest.mark.parametrize(
    ("decrypt", "tail"),
    [
        pytest.param(_undecryptable, " InvalidToken", id="undecryptable"),
        pytest.param(lambda _enc: "not json", f" {format_exception_message(_json_error())}", id="not-json"),
        pytest.param(lambda _enc: json.dumps(["a", "list"]), "", id="not-an-object"),
    ],
)
def test_an_unreadable_config_logs_the_error_that_made_it_unreadable(monkeypatch, caplog, decrypt, tail):
    """The ERROR line ends with the original error, never the audit reader's own message."""
    monkeypatch.setattr(webhooks, "decrypt_value", decrypt)
    with caplog.at_level(logging.ERROR, logger=webhooks.logger.name), pytest.raises(HTTPException) as exc_info:
        webhooks._read_webhook_config("enc", context="integration=abc-123")

    _assert_unverifiable(exc_info.value)
    message = _logged(caplog, logging.ERROR)
    assert message.startswith("Webhook delivery refused (integration=abc-123): ")
    assert message.endswith(f"encrypted with.{tail}")


async def test_an_unreadable_linkedin_client_secret_logs_the_error_that_made_it_unreadable(monkeypatch, caplog):
    monkeypatch.setattr("app.core.security.decrypt_stored_value", _undecryptable)
    delivery = _delivery([("X-LI-Signature", b"signature")])
    with caplog.at_level(logging.ERROR, logger=webhooks.logger.name), pytest.raises(HTTPException) as exc_info:
        await webhooks._verify_linkedin_provider_signature(_BODY, "linkedin", "enc", delivery)

    _assert_unverifiable(exc_info.value)
    message = _logged(caplog, logging.ERROR)
    assert message.startswith("Webhook delivery refused (provider=linkedin client secret): ")
    assert message.endswith("encrypted with. InvalidToken")


@pytest.mark.parametrize(
    ("config", "context", "logged"),
    [
        pytest.param(
            {"client_secret": True},
            "integration=abc-123",
            "HubSpot delivery refused (integration=abc-123): the configured secret is a JSON bool, not a string",
            id="client-secret",
        ),
        pytest.param(
            {"client_secret": {}, "webhook_secret": 1.5},
            "integration=abc-123",
            "HubSpot delivery refused (integration=abc-123): the configured secret is a JSON dict, not a string",
            id="the-client-secret-is-named-first",
        ),
        pytest.param(
            {"webhook_secret": [SECRET]},
            "",
            "HubSpot delivery refused: the configured secret is a JSON list, not a string",
            id="webhook-secret-without-context",
        ),
    ],
)
def test_an_unusable_hubspot_secret_is_refused_with_the_same_warning(caplog, config, context, logged):
    with caplog.at_level(logging.WARNING, logger=webhooks.logger.name), pytest.raises(HTTPException) as exc_info:
        webhooks._hubspot_secret(_stored(config), context=context)

    assert (exc_info.value.status_code, exc_info.value.detail) == (401, "Invalid HubSpot signature")
    assert exc_info.value.headers is None
    assert _logged(caplog, logging.WARNING) == logged

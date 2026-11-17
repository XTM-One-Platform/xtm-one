"""The registration answer carries the proof of the XTM sub-license.

``POST /platform/register`` tells a product whether it runs Enterprise Edition,
but nothing authenticates that answer: the product picks the host it calls XTM
One, so a bare ``ee_enabled`` is only as trustworthy as that setting. When the
Filigran license is what grants Enterprise Edition, the answer therefore carries the
Filigran-signed license certificate itself (``xtm_license_pem``), which the
product verifies against the XTM trust anchor pinned in its own build.

These tests drive the real route through FastAPI - a fake session, and test
certificate authorities generated here standing in for the Filigran ones - and
pin the contract the products build on: when the certificate travels, that it
is exactly the one XTM One validated, that nothing else ever travels with it,
and that ``ee_enabled`` is unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.utils import CryptographyDeprecationWarning
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.sql.elements import TextClause

from app.api.v1 import helpers as helpers_api
from app.api.v1 import platform as platform_api
from app.core.config import settings
from app.core.registration_token import PUBLISHED_DEFAULT_REGISTRATION_TOKEN
from app.core.security import decrypt_value, encrypt_value
from app.enterprise import platform_license_validator as plv
from app.enterprise.licensing import (
    LICENSE_OID_OPENAEV_IDS,
    LICENSE_OID_OPENCRQ_IDS,
    LICENSE_OID_OPENCTI_IDS,
    LICENSE_OID_PRODUCT,
    LICENSE_OID_TYPE,
    clear_license_cache,
    decode_license_pem,
    decode_xtm_license_pem,
)
from app.enterprise.xtm_ca import XTM_CA_PEM
from app.models.platform_registration import PlatformRegistration
from app.services import xtm_license

_TOKEN = "test-registration-token"
# The URL the ``register`` fixture registers with.
_PLATFORM_URL = "https://platform.example"
# What an answer withholding the proof says in ``xtm_license_withheld_reason``.
_WITHHELD_REASON = "registration_token_published_default"
# The cache key ``resolve_xtm_license`` resolves through. Cleared around every
# registration so a license decoded by one test is never served to the next.
_XTM_CACHE_KEY = "xtm"
# The digests products pin, as written in
# dev-docs/platform-registration-entitlement.md: the certificate's, and the
# SubjectPublicKeyInfo's for a product that pins the key itself.
_DOCUMENTED_XTM_CA_SHA256 = (
    "43:6E:0B:82:14:0F:43:CA:0E:15:3F:1B:27:AC:E3:B3:"
    "88:F1:9F:24:BC:6C:36:6E:C1:24:91:87:45:BC:40:64"
)
_DOCUMENTED_XTM_SPKI_SHA256_HEX = (
    "9ef09cc6ab116344afd1a47ae5b777842b8c44fae78d223440d19bb7bc7d84ec"
)
_DOCUMENTED_XTM_SPKI_SHA256_B64 = "nvCcxqsRY0Sv0aR65bd3hCuMRPrnjSI0QNGbt7x9hOw="
_ENTITLEMENT_DOC = (
    Path(__file__).resolve().parents[2]
    / "dev-docs"
    / "platform-registration-entitlement.md"
)


@dataclass(frozen=True)
class _Authority:
    key: rsa.RSAPrivateKey
    name: x509.Name
    # The trust anchor: the self-signed CA certificate, like ``XTM_CA_PEM``.
    pem: str


def _authority(common_name: str) -> _Authority:
    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    return _Authority(key, name, cert.public_bytes(serialization.Encoding.PEM).decode())


def _intermediate(parent: _Authority, common_name: str) -> _Authority:
    """A CA certificate the parent signed: what a chain would hand a verifier."""
    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(parent.name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(parent.key, hashes.SHA256())
    )
    return _Authority(key, name, cert.public_bytes(serialization.Encoding.PEM).decode())


@pytest.fixture(scope="module")
def pki() -> SimpleNamespace:
    """The XTM authority, an intermediate under it, a foreign one, and an
    OpenCRQ product authority."""
    xtm = _authority("Test XTM CA")
    return SimpleNamespace(
        xtm=xtm,
        intermediate=_intermediate(xtm, "Test XTM Intermediate CA"),
        foreign=_authority("Someone Else CA"),
        product=_authority("Test OpenCRQ CA"),
    )


def _license(
    authority: _Authority,
    *,
    product: str = "filigran xtm",
    # ``None`` leaves the type extension out, which XTM One reads as a trial.
    license_type: str | None = "standard",
    customer: str = "Test Customer",
    # The instance the license is bound to (``OU``), or every instance.
    ou: str = "global",
    not_before: datetime | None = None,
    not_after: datetime | None = None,
    opencti_ids: list[str] | None = None,
    openaev_ids: list[str] | None = None,
    opencrq_ids: list[str] | None = None,
) -> str:
    """A license certificate carrying the Filigran OID extensions."""
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, customer),
                    x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, ou),
                    x509.NameAttribute(NameOID.COMMON_NAME, "test-license"),
                ]
            )
        )
        .issuer_name(authority.name)
        # The subject key plays no part in license validation.
        .public_key(authority.key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - timedelta(minutes=5))
        .not_valid_after(not_after or now + timedelta(days=365))
        .add_extension(
            x509.UnrecognizedExtension(LICENSE_OID_PRODUCT, product.encode()),
            critical=False,
        )
    )
    if license_type is not None:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(LICENSE_OID_TYPE, license_type.encode()),
            critical=False,
        )
    for oid, ids in (
        (LICENSE_OID_OPENCTI_IDS, opencti_ids),
        (LICENSE_OID_OPENAEV_IDS, openaev_ids),
        (LICENSE_OID_OPENCRQ_IDS, opencrq_ids),
    ):
        if ids is not None:
            builder = builder.add_extension(
                x509.UnrecognizedExtension(oid, json.dumps(ids).encode()),
                critical=False,
            )
    cert = builder.sign(authority.key, hashes.SHA256())
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _canonical(pem_text: str) -> str:
    """The first certificate of a PEM text, re-serialised on its own."""
    cert = x509.load_pem_x509_certificate(pem_text.encode())
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _Session:
    """Just enough ``AsyncSession`` for ``register_platform``."""

    def __init__(self, existing: PlatformRegistration | None) -> None:
        self._existing = existing

    async def execute(self, statement: object, *_params: object) -> _Result:
        # The route takes the platform's lock, looks up the registration, then
        # the company-managed integration; only the registration may already
        # exist here.
        if isinstance(statement, TextClause):
            return _Result(None)
        entity = statement.column_descriptions[0]["entity"]
        return _Result(self._existing if entity is PlatformRegistration else None)

    def add(self, _obj: object) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def refresh(self, _obj: object) -> None:
        return None


@pytest.fixture
def register(monkeypatch, pki):
    """``POST /api/v1/platform/register`` through the real route."""

    async def _no_intent_sync(*_args, **_kwargs) -> None:
        return None

    async def _no_owner(_db) -> None:
        return None

    # The limiter in front of the token check is pinned in test_registration_token.py.
    async def _no_rate_limit(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(platform_api, "enforce_rate_limit", _no_rate_limit)
    monkeypatch.setattr(platform_api, "refund_rate_limit", _no_rate_limit)
    monkeypatch.setattr(settings, "platform_registration_token", _TOKEN)
    monkeypatch.setattr(xtm_license, "XTM_CA_PEM", pki.xtm.pem)
    monkeypatch.setitem(
        plv.PRODUCT_CA_CERTS, "opencrq", (pki.product.pem, ("opencrq",))
    )
    monkeypatch.setattr(platform_api, "_sync_platform_intents", _no_intent_sync)
    monkeypatch.setattr(helpers_api, "resolve_company_managed_owner_id", _no_owner)

    def _call(
        *,
        identifier: str = "opencrq",
        platform_id: str | None = "instance-1",
        env_pem: str = "",
        db_pem: str | None = None,
        platform_pem: str | None = None,
        created_at: datetime | None = None,
        existing: PlatformRegistration | None = None,
        # ``None`` sends no Authorization header at all; bytes are sent as is.
        token: str | bytes | None = _TOKEN,
    ):
        monkeypatch.setattr(settings, "enterprise_license", env_pem)
        platform_settings = SimpleNamespace(
            enterprise_license=db_pem,
            created_at=created_at or datetime.now(UTC) - timedelta(days=30),
        )

        async def _load_ps(_db) -> SimpleNamespace:
            return platform_settings

        monkeypatch.setattr(platform_api, "_load_ps", _load_ps)
        clear_license_cache(_XTM_CACHE_KEY)

        app = FastAPI()
        app.include_router(platform_api.router, prefix="/api/v1/platform")
        app.dependency_overrides[platform_api.get_db] = lambda: _Session(existing)
        body: dict[str, object] = {
            "platform_identifier": identifier,
            "platform_url": _PLATFORM_URL,
            "platform_id": platform_id,
        }
        if platform_pem is not None:
            body["enterprise_license_pem"] = platform_pem
        headers: dict[str, str | bytes] = {}
        if isinstance(token, bytes):
            headers["Authorization"] = b"Bearer " + token
        elif token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return TestClient(app).post(
            "/api/v1/platform/register",
            json=body,
            headers=headers,
        )

    yield _call
    clear_license_cache(_XTM_CACHE_KEY)


# -- When the proof travels ----------------------------------------------------


@pytest.mark.parametrize(
    ("identifier", "platform_id", "xtm_ids", "own_license", "expected_sources"),
    [
        # Sub-licensed by platform id, and by the global grant.
        ("opencrq", "instance-1", {"opencrq_ids": ["instance-1"]}, False, ["xtm_sublicense"]),
        ("opencti", "instance-1", {"opencti_ids": ["global"]}, False, ["xtm_sublicense"]),
        ("opencrq", None, {"opencrq_ids": ["global"]}, False, ["xtm_sublicense"]),
        # Not sub-licensed: another instance, another product type.
        ("opencrq", "instance-1", {"opencrq_ids": ["instance-2"]}, False, []),
        ("opencrq", None, {"opencrq_ids": ["instance-1"]}, False, []),
        ("opencrq", "instance-1", {"opencti_ids": ["global"]}, False, []),
        # Legacy identifiers read their product's sub-license.
        ("openbas", "instance-1", {"openaev_ids": ["instance-1"]}, False, ["xtm_sublicense"]),
        ("opengrc", "instance-1", {"opencrq_ids": ["instance-1"]}, False, ["xtm_sublicense"]),
        # The platform's own license: nothing to prove from XTM One.
        ("opencrq", "instance-1", None, True, ["platform_license"]),
        ("opencrq", "instance-1", {"opencrq_ids": ["instance-2"]}, True, ["platform_license"]),
        # Both paths: the proof travels whatever the own license says.
        (
            "opencrq",
            "instance-1",
            {"opencrq_ids": ["global"]},
            True,
            ["platform_license", "xtm_sublicense"],
        ),
        # Neither.
        ("opencrq", "instance-1", None, False, []),
    ],
)
def test_the_proof_travels_exactly_when_the_xtm_sub_license_grants_ee(
    register, pki, identifier, platform_id, xtm_ids, own_license, expected_sources
):
    xtm_pem = _license(pki.xtm, **xtm_ids) if xtm_ids is not None else None
    response = register(
        identifier=identifier,
        platform_id=platform_id,
        db_pem=xtm_pem,
        platform_pem=_license(pki.product, product="opencrq") if own_license else None,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["platform_identifier"] == platform_api._canonical_platform_identifier(
        identifier
    )
    assert body["ee_sources"] == expected_sources
    # Unchanged for older products: the platform's own license OR the XTM
    # sub-license, the rule ``ee_enabled`` followed before the proof existed.
    assert body["ee_enabled"] is bool(expected_sources)
    if "xtm_sublicense" in expected_sources:
        assert body["xtm_license_pem"] == _canonical(xtm_pem)
    else:
        assert body["xtm_license_pem"] is None
    # A token of the operator's own: nothing is ever withheld.
    assert body["xtm_license_withheld_reason"] is None


@pytest.mark.parametrize(
    ("license_kwargs", "created_ago", "granted"),
    [
        ({}, None, True),
        # A standard license keeps granting for 90 days past its end...
        ({"not_before": -400, "not_after": -10}, None, True),
        # ...and nothing after that.
        ({"not_before": -500, "not_after": -100}, None, False),
        # lts and nfr share that grace period.
        ({"license_type": "lts", "not_before": -400, "not_after": -10}, None, True),
        ({"license_type": "nfr", "not_before": -400, "not_after": -10}, None, True),
        # The grace branch also covers a start date still to come: notBefore
        # is not enforced for the types that have a grace period...
        ({"not_before": 10, "not_after": 375}, None, True),
        # ...and it is for the types that have none.
        ({"license_type": "trial", "not_before": 10, "not_after": 40}, None, False),
        # Trial and ci licenses get no grace period.
        ({"license_type": "trial", "not_before": -30, "not_after": -1}, None, False),
        # No type extension reads as a trial: valid within its dates, no grace.
        ({"license_type": None}, None, True),
        ({"license_type": None, "not_before": -30, "not_after": -1}, None, False),
        # A type outside the five is no license.
        ({"license_type": "enterprise"}, None, False),
        # A ci license lives 45 minutes past the XTM One instance creation...
        ({"license_type": "ci"}, timedelta(minutes=10), True),
        ({"license_type": "ci"}, timedelta(hours=2), False),
        # ...and never more than a year past its notBefore, whatever notAfter says.
        (
            {"license_type": "ci", "not_before": -400, "not_after": 365},
            timedelta(minutes=10),
            False,
        ),
        # Wrong signer, wrong product.
        ({"authority": "foreign"}, None, False),
        ({"product": "opencti"}, None, False),
        # Issued through an intermediate the XTM CA signed: the signature is
        # checked against the anchor's own key and no chain is built.
        ({"authority": "intermediate"}, None, False),
    ],
    ids=[
        "valid",
        "inside-grace",
        "past-grace",
        "lts-inside-grace",
        "nfr-inside-grace",
        "standard-not-yet-started",
        "trial-not-yet-started",
        "expired-trial",
        "no-type-is-a-trial",
        "no-type-has-no-grace",
        "unknown-type",
        "ci-inside-window",
        "ci-past-window",
        "ci-past-a-year-from-notbefore",
        "foreign-signer",
        "other-product",
        "intermediate-signer",
    ],
)
def test_no_proof_travels_for_a_license_xtm_one_does_not_accept(
    register, pki, license_kwargs, created_ago, granted
):
    kwargs = dict(license_kwargs)
    authority = getattr(pki, kwargs.pop("authority", "xtm"))
    now = datetime.now(UTC)
    for bound in ("not_before", "not_after"):
        if bound in kwargs:
            kwargs[bound] = now + timedelta(days=kwargs[bound])
    xtm_pem = _license(authority, opencrq_ids=["instance-1"], **kwargs)

    body = register(
        db_pem=xtm_pem,
        created_at=now - created_ago if created_ago else None,
    ).json()

    assert body["ee_enabled"] is granted
    assert body["ee_sources"] == (["xtm_sublicense"] if granted else [])
    assert body["xtm_license_pem"] == (_canonical(xtm_pem) if granted else None)


def _known_registration(platform_url: str = _PLATFORM_URL) -> PlatformRegistration:
    """The row a heartbeat finds: the platform registered before, at the URL it sends."""
    return PlatformRegistration(
        platform_identifier="opencrq",
        platform_url=platform_url,
        platform_title="Old title",
        platform_version="",
        platform_id="instance-1",
        contract="CE",
        license_type=None,
        license_validated=False,
        status="active",
    )


def test_every_heartbeat_carries_the_proof(register, pki):
    """A product re-registers periodically; the known row takes the update path."""
    existing = _known_registration()
    xtm_pem = _license(pki.xtm, opencrq_ids=["instance-1"])

    body = register(db_pem=xtm_pem, existing=existing).json()

    assert body["ee_sources"] == ["xtm_sublicense"]
    assert body["xtm_license_pem"] == _canonical(xtm_pem)
    assert body["xtm_license_withheld_reason"] is None
    assert existing.contract == "EE"


def test_a_token_of_your_own_moves_a_registration_to_its_new_url(register, pki):
    """Only the published default is held to the registered URL (``test_registration_token.py``)."""
    existing = _known_registration(platform_url="https://old.example")

    response = register(db_pem=_license(pki.xtm, opencrq_ids=["instance-1"]), existing=existing)

    assert response.status_code == 200
    assert existing.platform_url == _PLATFORM_URL


@pytest.mark.parametrize(
    ("configured", "withheld"),
    [(_TOKEN, False), (PUBLISHED_DEFAULT_REGISTRATION_TOKEN, True)],
    ids=["configured-token", "published-default"],
)
def test_the_heartbeat_withholds_the_proof_exactly_on_the_published_default(
    register, pki, monkeypatch, configured, withheld
):
    """The periodic re-registration (known row) says what it withholds and why."""
    monkeypatch.setattr(settings, "platform_registration_token", configured)
    xtm_pem = _license(pki.xtm, opencrq_ids=["instance-1"])

    for _ in range(2):
        existing = _known_registration()
        body = register(db_pem=xtm_pem, existing=existing, token=configured).json()

        assert body["ee_enabled"] is True
        assert body["ee_sources"] == ["xtm_sublicense"]
        assert existing.contract == "EE"
        if withheld:
            assert body["xtm_license_pem"] is None
            assert body["xtm_license_withheld_reason"] == _WITHHELD_REASON
        else:
            assert body["xtm_license_pem"] == _canonical(xtm_pem)
            assert body["xtm_license_withheld_reason"] is None


def test_nothing_is_withheld_where_no_proof_would_travel(register, pki, monkeypatch):
    """No ``xtm_sublicense`` grant: no proof to withhold, so no reason either."""
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)

    body = register(
        db_pem=_license(pki.xtm, opencrq_ids=["instance-2"]),
        existing=_known_registration(),
        token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN,
    ).json()

    assert body["ee_sources"] == []
    assert body["xtm_license_pem"] is None
    assert body["xtm_license_withheld_reason"] is None


def test_the_withheld_warning_is_logged_once_and_names_no_token(
    register, pki, monkeypatch, caplog
):
    """One line per process, across heartbeats, and never a token value."""
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    monkeypatch.setattr(platform_api, "_license_withheld_warned", False)
    xtm_pem = _license(pki.xtm, opencrq_ids=["instance-1"])

    with caplog.at_level(logging.WARNING, logger=platform_api.logger.name):
        for existing in (_known_registration(), _known_registration(), _known_registration()):
            body = register(
                db_pem=xtm_pem, existing=existing, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN
            ).json()
            assert body["xtm_license_withheld_reason"] == _WITHHELD_REASON
        # A token of the operator's own afterwards: the proof travels, nothing is logged.
        monkeypatch.setattr(settings, "platform_registration_token", _TOKEN)
        assert register(db_pem=xtm_pem, token=_TOKEN).json()["xtm_license_withheld_reason"] is None

    records = [r for r in caplog.records if r.name == platform_api.logger.name]
    assert [r.levelno for r in records] == [logging.WARNING]
    message = records[0].getMessage()
    assert "PLATFORM_REGISTRATION_TOKEN" in message
    for token in (PUBLISHED_DEFAULT_REGISTRATION_TOKEN, _TOKEN):
        assert token not in message


def test_the_documented_contract_names_the_withheld_reason():
    """Stated where products read the contract: the OpenAPI schema and the recipe."""
    model = platform_api.RegistrationResponse
    assert "xtm_license_withheld_reason" in (model.model_fields["xtm_license_pem"].description or "")
    schema = model.model_json_schema()["properties"]["xtm_license_withheld_reason"]
    assert _WITHHELD_REASON in json.dumps(schema)

    doc = _ENTITLEMENT_DOC.read_text(encoding="utf-8")
    for name in model.model_fields:
        assert f"| `{name}` |" in doc, f"{name} is missing from the answer table"
    assert f"`{_WITHHELD_REASON}`" in doc


def test_the_published_default_token_withholds_the_proof(register, pki, monkeypatch, caplog):
    """The published default authenticates nothing, so the license never travels.

    Heartbeats keep working and ``ee_enabled`` / ``ee_sources`` are unchanged;
    only the certificate stays home, with one warning per process however many
    heartbeats arrive.
    """
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    monkeypatch.setattr(platform_api, "_license_withheld_warned", False)
    xtm_pem = _license(pki.xtm, opencrq_ids=["instance-1"])

    with caplog.at_level(logging.WARNING, logger=platform_api.logger.name):
        bodies = [
            register(
                db_pem=xtm_pem, existing=_known_registration(), token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN
            ).json()
            for _ in range(3)
        ]

    for body in bodies:
        assert body["ee_enabled"] is True
        assert body["ee_sources"] == ["xtm_sublicense"]
        assert body["xtm_license_pem"] is None
        assert body["xtm_license_withheld_reason"] == _WITHHELD_REASON
    withheld = [r for r in caplog.records if "withheld" in r.getMessage()]
    assert len(withheld) == 1
    assert "PLATFORM_REGISTRATION_TOKEN" in withheld[0].getMessage()


def test_a_token_of_your_own_carries_the_proof_as_before(register, pki, monkeypatch, caplog):
    monkeypatch.setattr(platform_api, "_license_withheld_warned", False)
    xtm_pem = _license(pki.xtm, opencrq_ids=["instance-1"])

    with caplog.at_level(logging.WARNING, logger=platform_api.logger.name):
        body = register(db_pem=xtm_pem).json()

    assert body["ee_sources"] == ["xtm_sublicense"]
    assert body["xtm_license_pem"] == _canonical(xtm_pem)
    assert body["xtm_license_withheld_reason"] is None
    assert not [r for r in caplog.records if "withheld" in r.getMessage()]


@pytest.mark.parametrize(
    "token",
    ["wrong", None, "", b"\xe9t\xe9"],
    ids=["wrong-token", "no-header", "empty-token", "non-ascii-token"],
)
def test_no_proof_without_the_registration_token(register, pki, token):
    response = register(db_pem=_license(pki.xtm, opencrq_ids=["global"]), token=token)

    # A clean refusal - never a 500 - whatever the header holds.
    assert response.status_code == 401
    assert "xtm_license_pem" not in response.json()


def test_an_unset_registration_token_matches_nothing(register, pki, monkeypatch):
    """No token configured means registration is off, not open to an empty one.

    ``PLATFORM_REGISTRATION_TOKEN`` defaults to empty, and an ``Authorization:
    Bearer `` header whose trailing space survives the HTTP stack would
    otherwise carry exactly that empty token - and take the Filigran license home.
    """
    monkeypatch.setattr(settings, "platform_registration_token", "")

    for token in ("", None, "anything"):
        response = register(db_pem=_license(pki.xtm, opencrq_ids=["global"]), token=token)

        assert response.status_code == 401, token
        assert "xtm_license_pem" not in response.json()


# -- Which certificate, and nothing else ---------------------------------------


def test_the_proof_is_one_certificate_verifying_against_the_xtm_trust_anchor(
    register, pki
):
    xtm_pem = _license(pki.xtm, opencrq_ids=["instance-1"], customer="Acme")

    proof = register(db_pem=xtm_pem).json()["xtm_license_pem"]

    assert proof.count("-----BEGIN ") == 1
    assert proof.count("-----BEGIN CERTIFICATE-----") == 1
    assert "PRIVATE KEY" not in proof
    assert len(x509.load_pem_x509_certificates(proof.encode())) == 1
    # Verified by the routine the backend runs, against the anchor the route
    # used - exactly what a product repeats with the anchor in its build.
    info = decode_xtm_license_pem(proof, xtm_license.XTM_CA_PEM)
    assert info.license_valid_cert
    assert info.license_validated
    assert info.opencrq_ids == ["instance-1"]
    assert info.license_customer == "Acme"


def test_only_the_certificate_travels_whatever_surrounds_it_in_storage(register, pki):
    """The loader skips everything around the first certificate - so must the answer."""
    xtm_pem = _license(pki.xtm, opencrq_ids=["global"])
    pasted_key = pki.product.key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    stored = (
        "License pasted from the portal:\n"
        f"{pasted_key}{xtm_pem}{pki.foreign.pem}"
        "-- end of paste --\n"
    )

    proof = register(db_pem=stored).json()["xtm_license_pem"]

    assert proof == _canonical(xtm_pem)
    assert "PRIVATE KEY" not in proof
    assert proof.count("-----BEGIN ") == 1


def test_a_full_chain_in_storage_proves_nothing(register, pki):
    """A license issued through an intermediate never grants EE, chain or not.

    The intermediate is a certificate the XTM CA really signed, stored right
    after the license as a "fullchain" paste: XTM One still verifies the
    license against the anchor's own key, builds no path, and returns nothing.
    """
    license_pem = _license(pki.intermediate, opencrq_ids=["global"])

    body = register(db_pem=f"{license_pem}{pki.intermediate.pem}").json()

    assert body["ee_enabled"] is False
    assert body["ee_sources"] == []
    assert body["xtm_license_pem"] is None


def test_the_proof_is_the_license_xtm_one_decided_on(register, pki):
    """ENTERPRISE_LICENSE wins over the installed license, as for every decision."""
    env_pem = _license(pki.xtm, opencrq_ids=["instance-1"], customer="From the env")
    db_pem = _license(pki.xtm, opencrq_ids=["instance-1"], customer="From Settings")

    from_env = register(env_pem=env_pem, db_pem=db_pem).json()["xtm_license_pem"]
    from_db = register(env_pem="", db_pem=db_pem).json()["xtm_license_pem"]

    assert from_env == _canonical(env_pem)
    assert from_db == _canonical(db_pem)


def test_an_installed_license_never_stands_in_for_a_failing_env_one(register, pki):
    """The env license failing grants nothing - and proves nothing either."""
    env_pem = _license(pki.foreign, opencrq_ids=["instance-1"])
    db_pem = _license(pki.xtm, opencrq_ids=["instance-1"])

    body = register(env_pem=env_pem, db_pem=db_pem).json()

    assert body["ee_enabled"] is False
    assert body["ee_sources"] == []
    assert body["xtm_license_pem"] is None


# -- Where the certificate comes from ------------------------------------------


def test_the_decode_keeps_the_certificate_only_once_its_signature_verified(pki):
    pem = _license(pki.xtm, opencrq_ids=["global"])

    verified = decode_license_pem(pem, pki.xtm.pem, "filigran xtm")
    assert verified.license_certificate_pem == _canonical(pem)
    assert decode_license_pem(pem, pki.foreign.pem, "filigran xtm").license_certificate_pem is None
    assert decode_license_pem("not a certificate", pki.xtm.pem, "filigran xtm").license_certificate_pem is None
    # Kept out of reprs, so logging a license info never dumps a certificate.
    assert "BEGIN CERTIFICATE" not in repr(verified)
    assert "BEGIN CERTIFICATE" not in repr(decode_xtm_license_pem(pem, pki.xtm.pem))


def test_the_documented_trust_anchor_fingerprint_is_the_embedded_one():
    """Products pin the digests the doc gives; they must be ``XTM_CA_PEM``'s."""
    doc = _ENTITLEMENT_DOC.read_text(encoding="utf-8")
    # SHA-256 over the DER bytes the PEM armours, as ``openssl x509
    # -fingerprint -sha256`` computes it.
    der = base64.b64decode(
        "".join(line for line in XTM_CA_PEM.splitlines() if not line.startswith("-----"))
    )
    digest = hashlib.sha256(der).hexdigest().upper()

    assert ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)) == (
        _DOCUMENTED_XTM_CA_SHA256
    )
    assert _DOCUMENTED_XTM_CA_SHA256 in doc

    # The key a product may pin instead of the certificate, and its size. The
    # anchor carries serial number 0, as the recipe warns, hence the filter.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CryptographyDeprecationWarning)
        public_key = x509.load_der_x509_certificate(der).public_key()
    assert isinstance(public_key, rsa.RSAPublicKey)
    assert public_key.key_size == 4096
    spki = hashlib.sha256(
        public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).digest()
    assert spki.hex() == _DOCUMENTED_XTM_SPKI_SHA256_HEX
    assert base64.b64encode(spki).decode() == _DOCUMENTED_XTM_SPKI_SHA256_B64
    assert _DOCUMENTED_XTM_SPKI_SHA256_HEX in doc
    assert _DOCUMENTED_XTM_SPKI_SHA256_B64 in doc


# ``license_certificate_pem`` read as an attribute, or by name.
_CERTIFICATE_READ = re.compile(
    r"\.license_certificate_pem\b|[\"']license_certificate_pem[\"']"
)
_BACKEND = Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"
# Well below the real count, so a moved ``app/`` tree fails loudly instead of
# letting the scan pass over nothing.
_MIN_SCANNED_MODULES = 100


def test_the_certificate_leaves_xtm_one_through_the_register_answer_only():
    """Tripwire: the Filigran license certificate is a license, not a status.

    Whoever holds it can install it on an XTM One or present it to a product
    until it expires, so it leaves XTM One through exactly one door: the
    register answer, to a caller holding the registration token. The decode
    writes it; one reader in ``platform.py`` hands it on. A second reader - a
    settings payload, ``/config``, telemetry, a log line - fails here first.
    """
    offenders: list[str] = []
    platform_reads: list[str] = []
    scanned = 0
    for path in _APP.rglob("*.py"):
        scanned += 1
        if path == _APP / "enterprise" / "licensing.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not _CERTIFICATE_READ.search(line):
                continue
            where = f"{path.relative_to(_BACKEND)}:{lineno}: {line.strip()}"
            if path == _APP / "api" / "v1" / "platform.py":
                platform_reads.append(where)
            else:
                offenders.append(where)

    assert scanned >= _MIN_SCANNED_MODULES, f"scanned only {scanned} modules under {_APP}"
    assert not offenders, "\n".join(offenders)
    assert len(platform_reads) == 1, "\n".join(platform_reads)
    # No other payload of the module declares a certificate field.
    for model in (
        platform_api.PlatformConfigResponse,
        platform_api.XTMLicense,
        platform_api.PlatformRegistrationOut,
    ):
        assert not [
            name for name in model.model_fields if "pem" in name or "certificate" in name
        ], model.__name__


def test_the_tripwire_recognises_every_spelling_of_a_certificate_read():
    for line in (
        "pem = xtm_info.license_certificate_pem",
        'getattr(info, "license_certificate_pem")',
        "payload['license_certificate_pem']",
    ):
        assert _CERTIFICATE_READ.search(line), line
    for line in (
        "# ``license_certificate_pem`` is kept out of reprs",
        "license_certificate_pem_hash = None",
    ):
        assert not _CERTIFICATE_READ.search(line), line


# -- The published default never clears, downgrades or swaps a license ---------
#
# On the published default token the caller is anyone who knows the registered
# URL. The license fields decide whether the platform grants Enterprise Edition
# (inheritance, the ee_platform allowance, the quotas, the Filigran AI
# credential), so that heartbeat keeps them unless it brings the stored
# license's renewal (validated, newer, same customer and instance binding), and
# a registration holding no license takes only one bound to its instance. The
# stored certificate's own validity still ends it.


def _product_license(
    pki,
    *,
    issued_days_ago: int = 1,
    valid_days: int = 365,
    customer: str = "Test Customer",
    ou: str = "global",
) -> str:
    now = datetime.now(UTC)
    return _license(
        pki.product,
        product="opencrq",
        customer=customer,
        ou=ou,
        not_before=now - timedelta(days=issued_days_ago),
        not_after=now + timedelta(days=valid_days),
    )


def _licensed_registration(pem: str) -> PlatformRegistration:
    return PlatformRegistration(
        platform_identifier="opencrq",
        platform_url=_PLATFORM_URL,
        platform_title="Old title",
        platform_version="",
        platform_id="instance-1",
        contract="EE",
        license_type="standard",
        license_validated=True,
        enterprise_license_pem_enc=encrypt_value(pem),
        status="active",
    )


def _stored_pem(reg: PlatformRegistration) -> str | None:
    return decrypt_value(reg.enterprise_license_pem_enc) if reg.enterprise_license_pem_enc else None


def test_a_published_default_heartbeat_cannot_clear_a_license(register, pki, monkeypatch):
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    stored = _product_license(pki)
    existing = _licensed_registration(stored)

    body = register(existing=existing, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN).json()

    assert (existing.license_validated, existing.contract, existing.license_type) == (True, "EE", "standard")
    assert _stored_pem(existing) == stored
    assert body["ee_sources"] == ["platform_license"]


@pytest.mark.parametrize(
    "sent",
    ["older", "foreign", "garbage"],
)
def test_a_published_default_heartbeat_cannot_downgrade_a_license(register, pki, monkeypatch, sent):
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    stored = _product_license(pki, valid_days=365)
    sent_pem = {
        # Validates, but a replay of an earlier certificate.
        "older": _product_license(pki, issued_days_ago=300, valid_days=60),
        "foreign": _license(pki.foreign, product="opencrq", not_after=datetime.now(UTC) + timedelta(days=900)),
        "garbage": "not a certificate",
    }[sent]
    existing = _licensed_registration(stored)

    register(existing=existing, platform_pem=sent_pem, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN)

    assert existing.license_validated is True
    assert _stored_pem(existing) == stored


def test_a_published_default_heartbeat_records_a_renewal(register, pki, monkeypatch):
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    existing = _licensed_registration(_product_license(pki, issued_days_ago=300, valid_days=60))
    renewal = _product_license(pki, valid_days=730)

    register(existing=existing, platform_pem=renewal, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN)

    assert existing.license_validated is True
    assert _stored_pem(existing) == renewal


def test_a_published_default_heartbeat_lets_the_stored_license_lapse(register, pki, monkeypatch):
    """Sticky is not forever: past its grace the stored certificate ends itself."""
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    lapsed = _product_license(pki, issued_days_ago=700, valid_days=-200)
    existing = _licensed_registration(lapsed)

    register(existing=existing, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN)

    assert (existing.license_validated, existing.enterprise_license_pem_enc, existing.contract) == (False, None, "CE")


@pytest.mark.parametrize(
    ("customer", "ou"),
    [("Someone Else", "global"), ("Test Customer", "instance-1")],
    ids=["another-customer", "another-instance-binding"],
)
def test_a_published_default_heartbeat_cannot_swap_in_another_license(register, pki, monkeypatch, customer, ou):
    """Later and valid is not enough: a renewal keeps the stored license's holder."""
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    stored = _product_license(pki)
    existing = _licensed_registration(stored)

    register(
        existing=existing,
        platform_pem=_product_license(pki, valid_days=730, customer=customer, ou=ou),
        token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN,
    )

    assert existing.license_validated is True
    assert _stored_pem(existing) == stored


@pytest.mark.parametrize(
    ("ou", "recorded"),
    [("global", True), ("instance-1", True), ("instance-2", False)],
)
def test_a_published_default_heartbeat_takes_a_first_license_bound_to_the_platform(
    register, pki, monkeypatch, ou, recorded
):
    """A license the product itself would refuse (bound to another instance) is never recorded."""
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    existing = _known_registration()
    sent = _product_license(pki, ou=ou)

    body = register(existing=existing, platform_pem=sent, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN).json()

    assert existing.license_validated is recorded
    assert _stored_pem(existing) == (sent if recorded else None)
    assert body["ee_sources"] == (["platform_license"] if recorded else [])


@pytest.mark.parametrize(
    ("ou", "recorded"),
    [("global", True), ("instance-1", False), ("instance-2", False)],
    ids=["global", "the-instance-the-call-names", "another-instance"],
)
def test_a_published_default_heartbeat_takes_only_a_global_first_license_without_a_platform_id(
    register, pki, monkeypatch, ou, recorded
):
    """A registration recorded without a platform id has no instance to bind a license to.

    The published default keeps the stored platform id, so the one the call
    sends proves nothing: only a license valid for every instance may serve it.
    """
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    existing = _known_registration()
    existing.platform_id = None
    sent = _product_license(pki, ou=ou)

    body = register(
        existing=existing, platform_pem=sent, platform_id="instance-1", token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN
    ).json()

    assert existing.platform_id is None
    assert existing.license_validated is recorded
    assert _stored_pem(existing) == (sent if recorded else None)
    assert body["ee_sources"] == (["platform_license"] if recorded else [])


def test_a_published_default_heartbeat_reads_the_stored_license_under_the_registered_identifier(
    register, pki, monkeypatch
):
    """Mid-rename, a platform still sending the old identifier keeps the license an upgraded one recorded.

    The legacy identifier only accepts the legacy product OID, while the one
    the registration is stored under accepts both (``PRODUCT_CA_CERTS``).
    """
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    monkeypatch.setitem(plv.PRODUCT_CA_CERTS, "opencrq", (pki.product.pem, ("opencrq", "opengrc")))
    monkeypatch.setitem(plv.PRODUCT_CA_CERTS, "opengrc", (pki.product.pem, ("opengrc",)))
    stored = _product_license(pki)
    existing = _licensed_registration(stored)

    body = register(existing=existing, identifier="opengrc", token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN).json()

    assert (existing.license_validated, existing.contract) == (True, "EE")
    assert _stored_pem(existing) == stored
    assert body["ee_sources"] == ["platform_license"]


def test_a_published_default_heartbeat_takes_a_new_holders_license_once_the_stored_one_lapsed(
    register, pki, monkeypatch
):
    """A license that changed hands at renewal is recorded as soon as the old one ends: no gap."""
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    existing = _licensed_registration(_product_license(pki, issued_days_ago=700, valid_days=-200))
    successor = _product_license(pki, customer="Test Customer Group")

    register(existing=existing, platform_pem=successor, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN)

    assert existing.license_validated is True
    assert _stored_pem(existing) == successor


@pytest.mark.parametrize(
    "stored_pem_enc",
    [None, "not-a-fernet-token"],
    ids=["no-certificate", "unreadable-certificate"],
)
def test_a_published_default_heartbeat_keeps_no_license_it_cannot_read(
    register, pki, monkeypatch, stored_pem_enc
):
    """A validated flag with no certificate behind it (an older row) has nothing that could lapse."""
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    existing = _licensed_registration(_product_license(pki))
    existing.enterprise_license_pem_enc = stored_pem_enc

    body = register(existing=existing, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN).json()

    assert (existing.license_validated, existing.enterprise_license_pem_enc) == (False, None)
    assert body["ee_sources"] == []


def test_a_token_of_your_own_records_the_license_as_sent(register, pki):
    existing = _licensed_registration(_product_license(pki))

    register(existing=existing, platform_id="instance-9")

    assert (existing.license_validated, existing.enterprise_license_pem_enc) == (False, None)
    assert (existing.contract, existing.platform_id) == ("CE", "instance-9")


def test_a_published_default_heartbeat_keeps_the_platform_id(register, pki, monkeypatch):
    """The XTM sub-license is matched on it, so it is an edition field too."""
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    existing = _known_registration()

    body = register(
        db_pem=_license(pki.xtm, opencrq_ids=["instance-9"]),
        existing=existing,
        platform_id="instance-9",
        token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN,
    ).json()

    assert existing.platform_id == "instance-1"
    assert (existing.contract, body["ee_sources"]) == ("CE", [])


def test_the_edition_cache_is_dropped_only_after_the_license_is_kept(register, pki, monkeypatch):
    """The licence cache is dropped on every registration commit: by then the forged clear is already ignored."""
    from app.services import agentic_gate

    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    existing = _licensed_registration(_product_license(pki))
    seen: list[bool] = []
    monkeypatch.setattr(agentic_gate, "invalidate_cache", lambda: seen.append(existing.license_validated))

    register(existing=existing, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN)

    assert seen == [True]


# -- A valid license that is not recorded says why ----------------------


@pytest.fixture(autouse=True)
def _no_unrecorded_license_logged(monkeypatch):
    """The once-per-change memory is module state: start every test with none."""
    monkeypatch.setattr(platform_api, "_unrecorded_license_logged", {})


def _unrecorded_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "is not recorded" in r.getMessage()]


def test_a_license_bound_to_another_instance_is_logged_once_with_its_reason(register, pki, monkeypatch, caplog):
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    sent = _product_license(pki, ou="instance-2")

    with caplog.at_level(logging.INFO, logger=platform_api.logger.name):
        for _ in range(3):
            register(
                existing=_known_registration(), platform_pem=sent, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN
            )

    (line,) = _unrecorded_lines(caplog)
    assert "opencrq" in line
    assert "not bound to the stored platform_id" in line
    assert "stored platform_id='instance-1'" in line
    assert "ou='instance-2'" in line
    assert "customer='Test Customer'" in line
    assert "expires=" in line
    assert "BEGIN CERTIFICATE" not in line
    assert sent.splitlines()[1] not in line


def test_a_different_unrecorded_license_is_logged_again(register, pki, monkeypatch, caplog):
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)

    # Built once each: the expiry is part of the key and has whole-second precision.
    licenses = {ou: _product_license(pki, ou=ou) for ou in ("instance-2", "instance-3")}

    with caplog.at_level(logging.INFO, logger=platform_api.logger.name):
        for ou in ("instance-2", "instance-2", "instance-3"):
            register(
                existing=_known_registration(),
                platform_pem=licenses[ou],
                token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN,
            )

    assert [("ou='instance-2'" in line, "ou='instance-3'" in line) for line in _unrecorded_lines(caplog)] == [
        (True, False),
        (False, True),
    ]


def test_a_license_that_does_not_renew_the_stored_one_is_logged(register, pki, monkeypatch, caplog):
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    existing = _licensed_registration(_product_license(pki))

    with caplog.at_level(logging.INFO, logger=platform_api.logger.name):
        register(
            existing=existing,
            platform_pem=_product_license(pki, valid_days=730, customer="Someone Else"),
            token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN,
        )

    (line,) = _unrecorded_lines(caplog)
    assert "stored license kept" in line


def test_nothing_is_logged_when_the_license_is_recorded_or_resent(register, pki, monkeypatch, caplog):
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    stored = _product_license(pki, issued_days_ago=300, valid_days=60)
    existing = _licensed_registration(stored)

    with caplog.at_level(logging.INFO, logger=platform_api.logger.name):
        # The stored certificate again, a renewal, and a first license bound to the platform.
        register(existing=existing, platform_pem=stored, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
        register(
            existing=existing,
            platform_pem=_product_license(pki, valid_days=730),
            token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN,
        )
        register(
            existing=_known_registration(),
            platform_pem=_product_license(pki, ou="instance-1"),
            token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN,
        )

    assert _unrecorded_lines(caplog) == []


def test_another_license_with_the_same_ou_is_logged_again(register, pki, monkeypatch, caplog):
    """Two refused licenses can share an OU (``global``) and a reason: the key tells them apart."""
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    existing = _licensed_registration(_product_license(pki))

    # Built once each: the expiry is part of the key and has whole-second precision.
    variants = (("Someone Else", 400), ("Another One", 400), ("Another One", 500))
    pems = {v: _product_license(pki, customer=v[0], valid_days=v[1]) for v in variants}

    with caplog.at_level(logging.INFO, logger=platform_api.logger.name):
        for variant in (variants[0], variants[0], variants[1], variants[2]):
            register(
                existing=existing,
                platform_pem=pems[variant],
                token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN,
            )

    lines = _unrecorded_lines(caplog)
    assert len(lines) == 3
    assert ["Someone Else" in line for line in lines] == [True, False, False]
    assert all("stored license kept" in line for line in lines)


def test_an_older_certificate_of_the_same_holder_is_logged(register, pki, monkeypatch, caplog):
    """Not the stored certificate resent: the holder matches but the stored one is later."""
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    existing = _licensed_registration(_product_license(pki, valid_days=365))

    with caplog.at_level(logging.INFO, logger=platform_api.logger.name):
        register(
            existing=existing,
            platform_pem=_product_license(pki, valid_days=100),
            token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN,
        )

    (line,) = _unrecorded_lines(caplog)
    assert "stored license kept" in line


def test_a_lapsed_stored_license_logs_the_license_that_is_not_bound(register, pki, monkeypatch, caplog):
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    existing = _licensed_registration(_product_license(pki, issued_days_ago=700, valid_days=-200))

    with caplog.at_level(logging.INFO, logger=platform_api.logger.name):
        register(
            existing=existing,
            platform_pem=_product_license(pki, ou="instance-2"),
            token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN,
        )

    (line,) = _unrecorded_lines(caplog)
    assert "not bound to the stored platform_id" in line
    assert "ou='instance-2'" in line


def test_a_recorded_renewal_resets_the_once_per_change_memory(register, pki, monkeypatch, caplog):
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    existing = _licensed_registration(_product_license(pki, valid_days=100))
    refused = _product_license(pki, customer="Someone Else")

    with caplog.at_level(logging.INFO, logger=platform_api.logger.name):
        register(existing=existing, platform_pem=refused, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
        register(existing=existing, platform_pem=refused, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
        assert len(_unrecorded_lines(caplog)) == 1
        register(
            existing=existing,
            platform_pem=_product_license(pki, valid_days=730),
            token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN,
        )
        register(existing=existing, platform_pem=refused, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN)

    assert len(_unrecorded_lines(caplog)) == 2


def test_the_stored_certificate_resent_in_another_format_is_not_logged(register, pki, monkeypatch, caplog):
    monkeypatch.setattr(settings, "platform_registration_token", PUBLISHED_DEFAULT_REGISTRATION_TOKEN)
    stored = _product_license(pki)
    existing = _licensed_registration(stored)
    rewrapped = "\r\n".join(stored.splitlines()) + "\r\n\r\n"

    with caplog.at_level(logging.INFO, logger=platform_api.logger.name):
        register(existing=existing, platform_pem=rewrapped, token=PUBLISHED_DEFAULT_REGISTRATION_TOKEN)

    assert _unrecorded_lines(caplog) == []


def test_the_license_oids_line_names_the_ou(pki, monkeypatch, caplog):
    monkeypatch.setitem(plv.PRODUCT_CA_CERTS, "opencrq", (pki.product.pem, ("opencrq",)))
    pem = _product_license(pki, ou="instance-1")

    with caplog.at_level(logging.INFO, logger="app.enterprise.licensing"):
        assert plv.validate_platform_license("opencrq", pem).license_validated

    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("License OIDs")]
    assert "ou='instance-1'" in line

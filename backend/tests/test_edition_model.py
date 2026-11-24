"""The XTM Platform edition model: a license carries a quota, not a feature switch.

Pins the FY2027 packaging as the product enforces it:

* Two editions, not three tiers. ``enterprise`` is granted by EITHER a valid
  Filigran license OR a connected Enterprise Edition platform; ``community`` is
  neither. There is no intermediate "Starter Pack" state where chat runs but
  creation flows stay gated.
* Every licence gate in the codebase resolves to the ONE predicate
  (``is_agentic_ai_licensed``) - ``is_chat_licensed`` and
  ``ee_features_enabled`` are its aliases, so chat, A2A, creation flows, KB
  sources, MCP agent endpoints and the briefing can never disagree.
* The legacy ``agentic_ai`` / ``copilot`` capability flags on the certificate
  are parsed for information and consulted by NOTHING: not the install-time
  validation, not a gate, not the wire payload. A tripwire fails the build on
  a new reader.
* ``GET /platform/config`` carries ``edition`` and one ``agentic_enabled``;
  the retired ``deployment_tier`` / ``activation_tiers`` / ``copilot_enabled``
  / ``agentic_ai_enabled`` fields are gone.
* A license period whose quota claim is ``0``, or missing, has no ceiling:
  both spellings are on issued certificates that cannot be revoked.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from app.api.v1 import platform as platform_api
from app.api.v1.platform import (
    EDITION_COMMUNITY,
    EDITION_ENTERPRISE,
    PlatformConfigResponse,
    XTMLicense,
    resolve_deployment_edition,
)
from app.enterprise.licensing import (
    DEFAULT_AGENTIC_DAILY_QUOTA,
    DEFAULT_AGENTIC_MONTHLY_QUOTA,
    DEFAULT_AGENTIC_YEARLY_QUOTA,
    LICENSE_OID_AGENTIC_AI,
    LICENSE_OID_COPILOT,
    LICENSE_OID_DAILY_QUOTA,
    LICENSE_OID_LICENSE_MODEL,
    LICENSE_OID_MONTHLY_QUOTA,
    LICENSE_OID_PRODUCT,
    LICENSE_OID_TYPE,
    LICENSE_OID_YEARLY_QUOTA,
    decode_xtm_license_pem,
    validate_enterprise_license_for_install,
)
from tests import app_sources

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"


# ── A real certificate pair, with and without the legacy flags ──────────────


def _make_license_pair(
    *,
    agentic_ai: bool | None = None,
    copilot: bool | None = None,
    daily_quota: int | None = None,
    monthly_quota: int | None = None,
    yearly_quota: int | None = None,
    not_valid_before: datetime | None = None,
    license_model: str | None = None,
) -> tuple[str, str]:
    """``(client_pem, ca_pem)`` for a ``standard`` Filigran license.

    The two capability OIDs are only written when asked for, so a certificate
    with neither is exactly the one the retired install check refused. So are
    the quota claims: ``None`` writes no claim for the period.
    """
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test License CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .sign(ca_key, hashes.SHA256())
    )
    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    builder = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test Customer"),
                    x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "global"),
                    x509.NameAttribute(NameOID.COMMON_NAME, "test-license"),
                ]
            )
        )
        .issuer_name(ca_name)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_before or now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.UnrecognizedExtension(LICENSE_OID_TYPE, b"standard"), critical=False)
        .add_extension(
            x509.UnrecognizedExtension(LICENSE_OID_PRODUCT, b"filigran xtm"), critical=False
        )
    )
    if agentic_ai is not None:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(LICENSE_OID_AGENTIC_AI, str(agentic_ai).lower().encode()),
            critical=False,
        )
    if copilot is not None:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(LICENSE_OID_COPILOT, str(copilot).lower().encode()),
            critical=False,
        )
    if license_model is not None:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(LICENSE_OID_LICENSE_MODEL, license_model.encode()),
            critical=False,
        )
    for oid, quota in (
        (LICENSE_OID_DAILY_QUOTA, daily_quota),
        (LICENSE_OID_MONTHLY_QUOTA, monthly_quota),
        (LICENSE_OID_YEARLY_QUOTA, yearly_quota),
    ):
        if quota is not None:
            builder = builder.add_extension(
                x509.UnrecognizedExtension(oid, str(quota).encode()), critical=False
            )
    client_cert = builder.sign(ca_key, hashes.SHA256())
    return (
        client_cert.public_bytes(Encoding.PEM).decode(),
        ca_cert.public_bytes(Encoding.PEM).decode(),
    )


# ── The license: a quota, not a feature switch ──────────────────────────────


def test_a_valid_license_without_the_capability_flags_installs():
    """The retired model refused this certificate in BOTH modes ("does not
    include the Agentic AI add-on" / "the Filigran Copilot product"). It now
    installs, because it activates exactly the same platform as one that
    carries the flags."""
    pem, ca_pem = _make_license_pair(daily_quota=500)
    info = validate_enterprise_license_for_install(pem, ca_pem=ca_pem)
    assert info.license_validated
    assert info.daily_quota == 500
    # The flags are decoded (absent here), but the outcome above did not
    # depend on them.
    assert info.agentic_ai_enabled is False
    assert info.copilot_enabled is False


def test_a_license_explicitly_denying_the_flags_installs_too():
    pem, ca_pem = _make_license_pair(agentic_ai=False, copilot=False)
    assert validate_enterprise_license_for_install(pem, ca_pem=ca_pem).license_validated


def test_the_flags_are_still_parsed_for_information():
    """Support reads the log line; nothing else reads the fields."""
    pem, ca_pem = _make_license_pair(agentic_ai=True, copilot=True)
    info = decode_xtm_license_pem(pem, ca_pem)
    assert info.license_validated
    assert info.agentic_ai_enabled is True
    assert info.copilot_enabled is True


def test_install_validation_has_no_mode_parameter():
    """``is_xtm_one`` used to pick WHICH capability flag to demand; with no
    flag demanded in either mode the parameter has nothing left to decide."""
    assert "is_xtm_one" not in inspect.signature(validate_enterprise_license_for_install).parameters


# An attribute read (``info.copilot_enabled``) and the dynamic spellings of
# one (``getattr(info, "copilot_enabled")``, ``payload["agentic_ai_enabled"]``,
# ``.get("copilot_enabled")``) - a reader that dodges the dotted form must not
# dodge the tripwire.
_FLAG_READ = re.compile(
    r"\.(agentic_ai_enabled|copilot_enabled)\b"
    r"|[\"'](agentic_ai_enabled|copilot_enabled)[\"']"
)

# Well below the real count (hundreds of modules), so a moved ``app/`` tree
# fails loudly instead of letting the scan pass over nothing.
_MIN_SCANNED_MODULES = 100


def test_nothing_outside_the_parser_reads_the_capability_flags():
    """Tripwire: a license carries a quota, not a feature switch.

    ``licensing.py`` decodes the two legacy OIDs into ``XTMLicenseInfo`` (and
    logs them); every other module is forbidden to branch on them. A new
    reader would quietly reintroduce the three-tier gating this model retired.
    """
    offenders: list[str] = []
    scanned = 0
    for path in app_sources.python_files(APP):
        if path == APP / "enterprise" / "licensing.py":
            continue
        scanned += 1
        text = app_sources.source(path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _FLAG_READ.search(line):
                offenders.append(f"{path.relative_to(BACKEND)}:{lineno}: {line.strip()}")
    # The tripwire must never pass vacuously: it only proves anything when
    # it actually walked the application tree.
    assert scanned >= _MIN_SCANNED_MODULES, f"scanned only {scanned} modules under {APP}"
    assert not offenders, "\n".join(offenders)


def test_the_tripwire_recognises_every_spelling_of_a_flag_read() -> None:
    """The regex is the whole guarantee of the test above, so pin what it
    catches - and that a mere mention in prose is not a read."""
    for line in (
        "if xtm_info.copilot_enabled:",
        "flag = info.agentic_ai_enabled or info.copilot_enabled",
        'getattr(info, "copilot_enabled")',
        "payload['agentic_ai_enabled']",
        'data.get("copilot_enabled", False)',
    ):
        assert _FLAG_READ.search(line), line
    for line in (
        "# the legacy ``copilot_enabled`` flag is parsed for information",
        "copilot_enabled_by_someone_else = 1",
        "agentic_source = 'xtm_license'",
    ):
        assert not _FLAG_READ.search(line), line


def test_the_wire_payload_carries_no_capability_flag_and_no_tier():
    for field in ("copilot_enabled", "agentic_ai_enabled", "deployment_tier", "activation_tiers"):
        assert field not in PlatformConfigResponse.model_fields, field
    for field in ("copilot_enabled", "agentic_ai_enabled"):
        assert field not in XTMLicense.model_fields, field
    assert "edition" in PlatformConfigResponse.model_fields
    assert PlatformConfigResponse.model_fields["edition"].default == ""
    # Fail closed while unresolved: an unlicensed default must read as such.
    assert PlatformConfigResponse.model_fields["agentic_enabled"].default is False


# ── Two editions, one rule ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("xtm_validated", "has_ee", "expected"),
    [
        (True, True, EDITION_ENTERPRISE),
        (True, False, EDITION_ENTERPRISE),
        # The former "Starter Pack": a connected EE platform and no XTM
        # license is Enterprise Edition, full stop.
        (False, True, EDITION_ENTERPRISE),
        (False, False, EDITION_COMMUNITY),
    ],
)
def test_resolve_deployment_edition(xtm_validated, has_ee, expected):
    assert resolve_deployment_edition(True, xtm_validated=xtm_validated, has_ee=has_ee) == expected


def test_copilot_mode_has_no_edition():
    assert resolve_deployment_edition(False, xtm_validated=True, has_ee=True) == ""


def test_there_are_exactly_two_editions():
    """No third constant may creep back in (the retired ``ee_platform`` tier)."""
    editions = {
        name: value
        for name, value in vars(platform_api).items()
        if name.startswith("EDITION_")
    }
    assert editions == {
        "EDITION_ENTERPRISE": "enterprise",
        "EDITION_COMMUNITY": "community",
    }
    for retired in ("DEPLOYMENT_TIER_XTM_LICENSED", "DEPLOYMENT_TIER_EE_PLATFORM",
                    "DEPLOYMENT_TIER_CE_ONLY", "ACTIVATION_TIER_CATALOG", "ActivationTierInfo"):
        assert not hasattr(platform_api, retired), retired


@pytest.mark.asyncio
@pytest.mark.parametrize("edition", [EDITION_ENTERPRISE, EDITION_COMMUNITY])
async def test_every_licence_gate_gives_the_one_answer(monkeypatch, edition):
    """Chat, the briefing/autonomous path and the agentic gate are ONE licence
    question. The retired model let them disagree (chat on, creation off)."""

    async def _edition(_db):
        return edition

    monkeypatch.setattr(platform_api.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(platform_api, "get_deployment_edition", _edition)

    expected = edition == EDITION_ENTERPRISE
    assert await platform_api.is_agentic_ai_licensed(object()) is expected
    assert await platform_api.is_chat_licensed(object()) is expected
    assert await platform_api.ee_features_enabled(object()) is expected


@pytest.mark.asyncio
async def test_copilot_mode_is_licensed_by_a_valid_license_alone(monkeypatch):
    """The ``copilot`` flag used to be demanded on top of validity for chat."""
    monkeypatch.setattr(platform_api.settings, "platform_mode", "copilot")

    async def _ps(_db):
        return None

    monkeypatch.setattr(platform_api, "_load_ps", _ps)
    for validated in (True, False):
        monkeypatch.setattr(
            platform_api,
            "resolve_xtm_license",
            lambda _ps, v=validated: SimpleNamespace(license_validated=v),
        )
        assert await platform_api.is_agentic_ai_licensed(object()) is validated
        assert await platform_api.is_chat_licensed(object()) is validated


# ── Licence model claim (OID 62944.120, 17 Nov 2026) ───────────────────────


def test_a_license_without_the_model_claim_decodes_as_model_1() -> None:
    pem, ca_pem = _make_license_pair(agentic_ai=True)
    info = decode_xtm_license_pem(pem, ca_pem)
    assert info.license_validated
    assert info.license_model == 1
    assert not info.covers_every_module


def test_a_model_2_license_decodes_and_covers_every_module() -> None:
    pem, ca_pem = _make_license_pair(agentic_ai=True, license_model="2")
    info = decode_xtm_license_pem(pem, ca_pem)
    assert info.license_validated
    assert info.license_model == 2
    assert info.covers_every_module
    # No module extension on this certificate, yet every module is covered.
    assert info.opencti_ids == []


def test_a_malformed_model_claim_reads_as_model_1() -> None:
    pem, ca_pem = _make_license_pair(license_model="two")
    assert decode_xtm_license_pem(pem, ca_pem).license_model == 1


# ── GET /platform/config, the three shapes a deployment can take ────────────


def _xtm_info(validated: bool, **quotas) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        license_validated=validated,
        license_by_configuration=False,
        license_customer="Test Customer" if validated else "invalid",
        license_type="standard" if validated else "trial",
        license_expiration_date=now + timedelta(days=200),
        license_expired=False,
        license_expiration_prevention=False,
        license_extra_expiration=False,
        license_extra_expiration_days=0,
        daily_quota=quotas.get("daily", 0),
        monthly_quota=quotas.get("monthly", 0),
        yearly_quota=quotas.get("yearly", 0),
        opencti_ids=[],
        openaev_ids=[],
        opencrq_ids=[],
    )


async def _config(
    monkeypatch, *, validated: bool, has_ee: bool, branding=None, **quotas
) -> PlatformConfigResponse:
    """Run ``get_platform_config`` in XTM One mode against doubles.

    With ``branding``, the real branding payload is built from it instead of
    the stub dict.
    """
    from app.services import ai_state

    monkeypatch.setattr(platform_api.settings, "platform_mode", "xtm_one")

    async def _ps(_db):
        return None

    async def _branding(_db):
        return branding if branding is not None else object()

    async def _ai_on(_db):
        return True

    async def _ee_status(_db, _info=None):
        return (has_ee, 1 if has_ee else 0)

    async def _link_status(_db):
        return platform_api.PlatformIntegrationStatus()

    monkeypatch.setattr(platform_api, "_load_ps", _ps)
    monkeypatch.setattr(platform_api, "get_branding", _branding)
    monkeypatch.setattr(platform_api, "_load_integration_status", _link_status)
    monkeypatch.setattr(platform_api, "ee_platform_status", _ee_status)
    monkeypatch.setattr(platform_api, "resolve_xtm_license", lambda _ps: _xtm_info(validated, **quotas))
    monkeypatch.setattr(ai_state, "ai_enabled_for_display", _ai_on)
    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai_on)
    if branding is not None:
        return await platform_api.get_platform_config(db=object())
    monkeypatch.setattr(
        platform_api,
        "resolved_branding_payload",
        lambda _b, is_xtm: {
            "platform_name": "XTM One",
            "platform_logo": None,
            "platform_logo_hash": None,
            "platform_description": "",
            "platform_theme_color": "#000",
            "platform_favicon": None,
            "platform_favicon_hash": None,
            "platform_accent_color": None,
            "platform_background_color": None,
            "platform_default_accent_color": "#000",
            "platform_default_background_color": "#000",
            "platform_og_image": "",
            "platform_og_image_hash": None,
            "platform_login_message": "",
        },
    )
    return await platform_api.get_platform_config(db=object())


@pytest.mark.asyncio
async def test_config_with_an_xtm_license(monkeypatch):
    cfg = await _config(monkeypatch, validated=True, has_ee=False, daily=500, monthly=5000, yearly=50000)
    assert cfg.edition == EDITION_ENTERPRISE
    assert cfg.agentic_enabled is True
    assert cfg.chat_enabled is True
    assert cfg.agentic_source == "xtm_license"
    assert (cfg.agentic_quotas.daily, cfg.agentic_quotas.monthly, cfg.agentic_quotas.yearly) == (500, 5000, 50000)
    assert cfg.xtm_license is not None and cfg.xtm_license.valid
    assert cfg.features.all_integrations and cfg.features.all_templates and cfg.features.ai_adoption


@pytest.mark.asyncio
async def test_an_uncapped_license_period_is_reported_as_null(monkeypatch):
    """Inside the certificate an uncapped period is 0; on the wire it is null,
    so a consumer never reads a 0 that means the opposite of "no allowance".
    Community Edition keeps its 0/0/0 below: that one IS no allowance."""
    cfg = await _config(monkeypatch, validated=True, has_ee=False, daily=500)
    assert (cfg.agentic_quotas.daily, cfg.agentic_quotas.monthly, cfg.agentic_quotas.yearly) == (500, None, None)
    assert cfg.xtm_license is not None
    assert cfg.xtm_license.agentic_quotas.monthly is None

    uncapped = await _config(monkeypatch, validated=True, has_ee=False)
    assert (uncapped.agentic_quotas.daily, uncapped.agentic_quotas.monthly, uncapped.agentic_quotas.yearly) == (None, None, None)


@pytest.mark.asyncio
async def test_config_serves_the_light_wordmark_until_an_operator_sets_a_logo(monkeypatch):
    """The bundled logo's wordmark is white: a client in the light theme needs
    the dark-text variant, and ``platform_logo`` always carried the white one,
    so the light theme showed an invisible product name."""
    from app.services.branding import Branding

    bundled = await _config(monkeypatch, validated=True, has_ee=False, branding=Branding(name="XTM One"))
    assert (bundled.platform_logo, bundled.platform_logo_light) == ("/xtp-one.svg", "/xtp-one-light.svg")
    assert (bundled.platform_emblem, bundled.platform_emblem_light) == ("/xtm-one-emblem.svg", "/xtm-one-emblem-light.svg")

    own = await _config(
        monkeypatch, validated=True, has_ee=False,
        branding=Branding(name="Acme", logo="data:image/png;base64,QUFB"),
    )
    assert own.platform_logo == "/api/v1/platform/branding/asset/logo"
    assert own.platform_logo_light is None
    assert own.platform_emblem is None


# ── A license period without a ceiling: a 0 claim, or no claim ─────────────
#
# The licensing application wrote an Unlimited period as 0 until it started
# writing no claim at all. An issued
# license cannot be revoked or edited, and customer platforms run on both
# spellings, so both must keep reading as "no ceiling" for as long as such a
# certificate can be installed.

_NO_CEILING_CLAIMS = {
    "zero_claims": {"daily_quota": 0, "monthly_quota": 0, "yearly_quota": 0},
    "no_claims": {},
}


async def _license_ceilings(monkeypatch, pem: str, ca_pem: str) -> tuple:
    """The ceilings the quota gate enforces for this certificate, and the
    license limit the resolver applies - through the real code, with only the
    settings row and the product CA stubbed."""
    from app.api.v1 import platform_settings_helpers
    from app.enterprise import licensing
    from app.services import quota as quota_module

    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(
        platform_settings_helpers,
        "get_platform_settings",
        AsyncMock(return_value=SimpleNamespace(enterprise_license=pem, created_at=None)),
    )
    monkeypatch.setattr(
        licensing, "get_xtm_license_info", lambda **_kwargs: decode_xtm_license_pem(pem, ca_pem)
    )
    return (
        await quota_module.get_xtm_agentic_ceilings(object()),
        await quota_module._resolve_xtm_agentic_config(object()),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", list(_NO_CEILING_CLAIMS.values()), ids=list(_NO_CEILING_CLAIMS))
async def test_a_license_period_of_0_or_without_a_claim_has_no_ceiling(monkeypatch, claims):
    pem, ca_pem = _make_license_pair(**claims)
    info = decode_xtm_license_pem(pem, ca_pem)
    assert info.license_validated
    assert (info.daily_quota, info.monthly_quota, info.yearly_quota) == (0, 0, 0)

    reported = platform_api.AgenticQuotas.from_license(info)
    assert (reported.daily, reported.monthly, reported.yearly) == (None, None, None)
    assert await _license_ceilings(monkeypatch, pem, ca_pem) == (None, None)


@pytest.mark.asyncio
async def test_a_0_claim_has_no_ceiling_whatever_the_license_start_date(monkeypatch):
    """No split on ``not_valid_before``: it is the validity start the issuer
    chose, not the signing date, so a renewal pre-issued with a 0 (Unlimited
    then) may start after the issuer stopped writing 0 - it stays uncapped."""
    issuer_change = datetime(2026, 9, 27, tzinfo=UTC)
    start = max(issuer_change + timedelta(days=1), datetime.now(UTC) + timedelta(days=30))
    pem, ca_pem = _make_license_pair(
        daily_quota=0, monthly_quota=0, yearly_quota=0, not_valid_before=start
    )
    info = decode_xtm_license_pem(pem, ca_pem)
    assert info.license_validated
    assert info.license_start_date > issuer_change
    assert (info.daily_quota, info.monthly_quota, info.yearly_quota) == (0, 0, 0)

    reported = platform_api.AgenticQuotas.from_license(info)
    assert (reported.daily, reported.monthly, reported.yearly) == (None, None, None)
    assert await _license_ceilings(monkeypatch, pem, ca_pem) == (None, None)


@pytest.mark.asyncio
async def test_a_license_period_with_a_positive_claim_is_enforced(monkeypatch):
    """The same path, with a ceiling: what keeps the test above from passing on a gate that enforces nothing."""
    pem, ca_pem = _make_license_pair(daily_quota=500, monthly_quota=5000, yearly_quota=50000)
    ceilings, resolved = await _license_ceilings(monkeypatch, pem, ca_pem)
    assert ceilings == {"daily": 500, "monthly": 5000, "yearly": 50000}
    assert resolved == {"default": 500, "period": "daily", "global": True}


@pytest.mark.asyncio
async def test_config_with_an_ee_platform_and_no_license_is_fully_enterprise(monkeypatch):
    """The former Starter Pack: everything on, under the included allowance.

    The retired payload reported ``agentic_ai_enabled=False`` and kept the
    catalog / templates / AI adoption off here, so an Enterprise customer
    whose XTM One had no license of its own could chat but not build.
    """
    cfg = await _config(monkeypatch, validated=False, has_ee=True)
    assert cfg.edition == EDITION_ENTERPRISE
    assert cfg.agentic_enabled is True
    assert cfg.chat_enabled is True
    assert cfg.agentic_source == "ee_platform"
    assert cfg.agentic_quotas.daily == DEFAULT_AGENTIC_DAILY_QUOTA
    assert cfg.agentic_quotas.monthly == DEFAULT_AGENTIC_MONTHLY_QUOTA
    assert cfg.agentic_quotas.yearly == DEFAULT_AGENTIC_YEARLY_QUOTA
    assert cfg.xtm_license is not None and cfg.xtm_license.valid is False
    assert cfg.features.has_ee_platform is True
    assert cfg.features.all_integrations and cfg.features.all_templates and cfg.features.ai_adoption


@pytest.mark.asyncio
async def test_config_on_community_edition_has_ai_off(monkeypatch):
    cfg = await _config(monkeypatch, validated=False, has_ee=False)
    assert cfg.edition == EDITION_COMMUNITY
    assert cfg.agentic_enabled is False
    assert cfg.chat_enabled is False
    assert cfg.agentic_source == "none"
    assert (cfg.agentic_quotas.daily, cfg.agentic_quotas.monthly, cfg.agentic_quotas.yearly) == (0, 0, 0)
    assert not cfg.features.all_integrations
    # Community Edition is not a licence wall: the app stays navigable.
    assert cfg.license_required is False


def test_the_refusal_names_both_activation_paths():
    """Every 403 the licence gates raise carries the same sentence, and it
    names what actually changes the answer: connect an EE platform, or install
    a license - plus the trial and sales paths."""
    from app.api.deps import (
        CHAT_UNAVAILABLE_ENTERPRISE_DETAIL,
        ENTERPRISE_EDITION_REQUIRED_DETAIL,
    )

    detail = ENTERPRISE_EDITION_REQUIRED_DETAIL
    assert "Enterprise Edition" in detail
    assert "OpenCTI" in detail and "OpenAEV" in detail and "OpenCRQ" in detail
    assert "Filigran license" in detail
    assert "filigran-xtm-one-enterprise-edition-trial" in detail
    assert "filigran.io/contact" in detail
    for word in ("add-on", "Starter Pack", "tier"):
        assert word not in detail, word
    # The chat gate leads with what the person was doing, then the same remedy.
    assert CHAT_UNAVAILABLE_ENTERPRISE_DETAIL.startswith("Chat is unavailable")
    assert CHAT_UNAVAILABLE_ENTERPRISE_DETAIL.endswith(detail)


def test_the_refusal_names_a_remedy_that_exists_in_each_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copilot mode has no edition and no platform to connect: the license is
    the one way in. Sending an unlicensed Copilot caller (the MCP agent
    endpoints and A2A sit outside the ``/api`` licence-wall middleware) to
    "connect an Enterprise Edition OpenCTI" names a path that changes nothing,
    so the gates pick their sentence by mode."""
    from app.api import deps

    monkeypatch.setattr(deps.settings, "platform_mode", "xtm_one")
    assert deps.licence_required_detail() == deps.ENTERPRISE_EDITION_REQUIRED_DETAIL
    assert deps.chat_unavailable_detail() == deps.CHAT_UNAVAILABLE_ENTERPRISE_DETAIL

    monkeypatch.setattr(deps.settings, "platform_mode", "copilot")
    detail = deps.licence_required_detail()
    assert detail == deps.LICENSE_REQUIRED_DETAIL
    assert "Filigran license" in detail
    assert "filigran-xtm-one-enterprise-edition-trial" in detail
    assert "filigran.io/contact" in detail
    for word in ("connect", "OpenCTI", "OpenAEV", "OpenCRQ", "Community Edition"):
        assert word not in detail, word
    assert deps.chat_unavailable_detail() == deps.CHAT_UNAVAILABLE_PREFIX + detail


_XTM_ONE_REFUSAL_CONSTANTS = frozenset(
    {"ENTERPRISE_EDITION_REQUIRED_DETAIL", "CHAT_UNAVAILABLE_ENTERPRISE_DETAIL"}
)


def _names_a_refusal_constant(node: ast.AST) -> bool:
    """Whether an expression is one of the XTM One refusal constants, spelt
    bare (``ENTERPRISE_EDITION_REQUIRED_DETAIL``) or qualified
    (``deps.ENTERPRISE_EDITION_REQUIRED_DETAIL``), anywhere inside it - a
    concatenation or a conditional built on the constant is still the
    constant."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in _XTM_ONE_REFUSAL_CONSTANTS:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in _XTM_ONE_REFUSAL_CONSTANTS:
            return True
    return False


def _hard_coded_refusal_details(tree: ast.AST) -> list[int]:
    """Line numbers of ``HTTPException(...)`` calls whose ``detail`` argument
    names an XTM One refusal constant - read from the AST, so the layout of
    the call (one line, or the argument on the line after ``detail=``) makes
    no difference."""
    hits: list[int] = []
    for node in app_sources.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if callee != "HTTPException":
            continue
        for keyword in node.keywords:
            if keyword.arg == "detail" and _names_a_refusal_constant(keyword.value):
                hits.append(node.lineno)
        # ``HTTPException(403, ENTERPRISE_EDITION_REQUIRED_DETAIL)``: detail is
        # the second positional argument.
        if len(node.args) >= 2 and _names_a_refusal_constant(node.args[1]):
            hits.append(node.lineno)
    return hits


def test_every_licence_gate_raises_the_mode_aware_sentence() -> None:
    """No gate may hard-code the XTM One constant into its refusal again: the
    raise sites go through ``licence_required_detail()`` /
    ``chat_unavailable_detail()`` (tests may still name the constants)."""
    offenders: list[str] = []
    scanned = 0
    for path in app_sources.python_files(APP):
        scanned += 1
        for lineno in _hard_coded_refusal_details(app_sources.tree(path)):
            offenders.append(f"{path.relative_to(BACKEND)}:{lineno}")
    assert scanned >= _MIN_SCANNED_MODULES, f"scanned only {scanned} modules under {APP}"
    assert not offenders, "\n".join(offenders)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_mode", ["xtm_one", "copilot"])
async def test_the_llm_proxies_refuse_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, platform_mode: str
) -> None:
    """The OpenAI-compatible and Anthropic proxies (Cursor, Claude Code) are
    the easiest way around the UI: AI switched off answers the contract code,
    an absent licence the mode-aware chat sentence, and a licensed AI-on
    deployment passes. They used to answer the retired "XTM One trial or
    Enterprise license" sentence for both reasons, in both modes."""
    from fastapi import HTTPException

    from app.api import deps
    from app.api.v1 import llm_proxy
    from app.services import ai_state
    from app.services.ai_state import AI_DISABLED_CODE

    monkeypatch.setattr(deps.settings, "platform_mode", platform_mode)

    async def _off(_db):
        return False

    async def _on(_db):
        return True

    monkeypatch.setattr(ai_state, "is_ai_enabled", _off)
    with pytest.raises(HTTPException) as caught:
        await llm_proxy._refuse_unless_chat_enabled(object())
    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == AI_DISABLED_CODE

    monkeypatch.setattr(ai_state, "is_ai_enabled", _on)
    monkeypatch.setattr(platform_api, "is_chat_licensed", _off)
    with pytest.raises(HTTPException) as caught:
        await llm_proxy._refuse_unless_chat_enabled(object())
    assert caught.value.status_code == 403
    assert caught.value.detail == deps.chat_unavailable_detail()
    assert "trial or Enterprise license is required" not in caught.value.detail

    monkeypatch.setattr(platform_api, "is_chat_licensed", _on)
    assert await llm_proxy._refuse_unless_chat_enabled(object()) is None


def test_the_refusal_scan_reads_the_call_not_the_line() -> None:
    """The detector is the guarantee of the test above: it must see the
    constant when the argument sits on the line after ``detail=``, when it is
    qualified, positional or concatenated - and must not flag the helpers."""
    flagged = textwrap.dedent(
        """
        raise HTTPException(
            status_code=403,
            detail=(
                ENTERPRISE_EDITION_REQUIRED_DETAIL
            ),
        )
        raise HTTPException(status_code=403, detail=deps.CHAT_UNAVAILABLE_ENTERPRISE_DETAIL)
        raise HTTPException(403, ENTERPRISE_EDITION_REQUIRED_DETAIL)
        raise fastapi.HTTPException(403, detail="Chat is unavailable. " + ENTERPRISE_EDITION_REQUIRED_DETAIL)
        """
    )
    assert len(_hard_coded_refusal_details(ast.parse(flagged))) == 4
    clean = textwrap.dedent(
        """
        raise HTTPException(status_code=403, detail=licence_required_detail())
        raise HTTPException(status_code=403, detail=chat_unavailable_detail())
        CHAT_UNAVAILABLE_ENTERPRISE_DETAIL = CHAT_UNAVAILABLE_PREFIX + ENTERPRISE_EDITION_REQUIRED_DETAIL
        """
    )
    assert _hard_coded_refusal_details(ast.parse(clean)) == []

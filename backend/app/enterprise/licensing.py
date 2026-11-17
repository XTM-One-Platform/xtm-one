"""Filigran Copilot license validation.

Validates X.509 PEM license certificates issued by the filigran-licenses
application.  The same OID scheme (IANA enterprise number 62944) is shared
across all Filigran products (OpenCTI, OpenAEV, Copilot).

Mirrors the validation logic from OpenCTI's ``licensing.ts`` and the
certificate generation in ``filigran-licenses/src/lib/generate_opencti_client_cert.ts``.

Every certificate - license or trust anchor - is parsed with
``load_license_certificate``, which keeps working when cryptography refuses the
serial number 0 or the non-ASCII PrintableString name strings filigran-licenses
writes (see ``license_certificate.py``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ObjectIdentifier

from app.enterprise.license_certificate import LicenseCertificate, load_license_certificate

logger = logging.getLogger(__name__)

# ── Filigran licence OIDs (IANA enterprise 62944) ────────────────────────────
LICENSE_OID_TYPE = ObjectIdentifier("1.3.6.1.4.1.62944.10")
LICENSE_OID_PRODUCT = ObjectIdentifier("1.3.6.1.4.1.62944.20")
LICENSE_OID_CREATOR = ObjectIdentifier("1.3.6.1.4.1.62944.30")
LICENSE_OID_YEARLY_QUOTA = ObjectIdentifier("1.3.6.1.4.1.62944.40")

# XTM sub-licensing OIDs
LICENSE_OID_OPENCTI_IDS = ObjectIdentifier("1.3.6.1.4.1.62944.50")
LICENSE_OID_OPENAEV_IDS = ObjectIdentifier("1.3.6.1.4.1.62944.60")
LICENSE_OID_DAILY_QUOTA = ObjectIdentifier("1.3.6.1.4.1.62944.70")
LICENSE_OID_MONTHLY_QUOTA = ObjectIdentifier("1.3.6.1.4.1.62944.80")
LICENSE_OID_OPENCRQ_IDS = ObjectIdentifier("1.3.6.1.4.1.62944.90")
LICENSE_OID_COPILOT = ObjectIdentifier("1.3.6.1.4.1.62944.100")
LICENSE_OID_AGENTIC_AI = ObjectIdentifier("1.3.6.1.4.1.62944.110")
# Licence model: ``2`` on every Filigran license issued from 17 Nov 2026 (one
# licence bound to its subject instance, Enterprise Edition on every module by
# design). Absent on every licence issued before, read as model 1.
LICENSE_OID_LICENSE_MODEL = ObjectIdentifier("1.3.6.1.4.1.62944.120")
# The model from which a Filigran license covers every module whatever its
# module extensions say.
FILIGRAN_LICENSE_MODEL_EE_EVERYWHERE = 2

LICENSE_LEGACY_OID_TYPE = ObjectIdentifier("2.14521.4.4.10")
LICENSE_LEGACY_OID_PRODUCT = ObjectIdentifier("2.14521.4.4.20")
LICENSE_LEGACY_OID_CREATOR = ObjectIdentifier("2.14521.4.4.30")

LICENSE_TYPES = ("standard", "lts", "trial", "nfr", "ci")
GLOBAL_LICENSE = "global"
GRACE_PERIOD_MONTHS = 3
CACHE_TTL_SECONDS = 300  # 5 minutes

# The INCLUDED AGENTIC ALLOWANCE of the XTM Platform model (FY2027).
#
# Every Enterprise deployment carries an acts ceiling. When a Filigran license is
# installed the ceilings are the ones on the certificate; when Enterprise
# Edition is granted by a connected EE platform (OpenCTI / OpenAEV / OpenCRQ
# EE) with no Filigran license, these figures are the allowance — sized to
# discover, test and demonstrate value, not to run production workloads.
# Capacity for production comes from the acts packs, delivered as license
# ceilings. The yearly cap is set so daily * ~10 ≤ monthly * 3 ≤ yearly,
# keeping the three periods consistent for the consumption gauges rendered in
# Settings → Usage and quotas → Quotas.
DEFAULT_AGENTIC_DAILY_QUOTA = 30
DEFAULT_AGENTIC_MONTHLY_QUOTA = 100
DEFAULT_AGENTIC_YEARLY_QUOTA = 300


@dataclass
class LicenseInfo:
    """Result of licence validation."""

    license_enterprise: bool = False
    license_validated: bool = False
    license_by_configuration: bool = False
    license_valid_cert: bool = False
    license_customer: str = "invalid"
    license_type: str = "trial"
    license_creator: str = "Unknown"
    license_platform: str = "invalid"
    license_global: bool = False
    license_platform_match: bool = True
    license_start_date: datetime = field(default_factory=lambda: datetime.now(UTC))
    license_expiration_date: datetime = field(default_factory=lambda: datetime.now(UTC))
    license_expired: bool = True
    license_extra_expiration: bool = False
    license_extra_expiration_days: int = 0
    license_expiration_prevention: bool = False
    # The certificate whose signature verified, re-serialised from the parsed
    # certificate (``None`` until a signature verifies). Never the PEM text as
    # stored: the loader skips whatever surrounds the first CERTIFICATE block,
    # a pasted private key included, and none of that may travel with it. The
    # Filigran license path hands it to a registering platform as the proof of its
    # sub-license.
    license_certificate_pem: str | None = field(default=None, repr=False)
    # Which check refused the certificate, for the log of the caller that owns
    # the license ("" while it is validated). Never the PEM text.
    license_refusal_reason: str = ""


@dataclass
class XTMLicenseInfo(LicenseInfo):
    """Extended license info for Filigran licenses.

    Carries the sub-licensing platform IDs (which connected module is in
    Enterprise Edition) and the agentic execution quotas (the acts ceilings).

    What a license DECIDES is exactly those two things: the edition of each
    module, and the capacity. A valid Filigran license activates the whole XTM One
    core — every agentic capability — whatever else the certificate says.

    ``copilot_enabled`` / ``agentic_ai_enabled`` are the legacy capability
    flags (OIDs ``62944.100`` / ``62944.110``) that the retired three-tier model
    gated creation flows on. They are still PARSED so a certificate is decoded
    in full (support, logs), but nothing branches on them anymore: no gate, no
    install-time refusal, no wire field. Do not reintroduce a reader — a
    license carries a quota, not a feature switch.
    """

    opencti_ids: list[str] = field(default_factory=list)
    openaev_ids: list[str] = field(default_factory=list)
    opencrq_ids: list[str] = field(default_factory=list)
    # Informational only — see the class docstring.
    copilot_enabled: bool = False
    agentic_ai_enabled: bool = False
    daily_quota: int = 0
    monthly_quota: int = 0
    yearly_quota: int = 0
    # Licence model (OID ``62944.120``): 1 when absent (every licence issued
    # before 17 Nov 2026, per-module grants as written), 2 from then on (every
    # module covered by design). See :attr:`covers_every_module`.
    license_model: int = 1

    @property
    def covers_every_module(self) -> bool:
        """Whether the licence grants every module by design (model 2 or later)."""
        return self.license_model >= FILIGRAN_LICENSE_MODEL_EE_EVERYWHERE


def _extract_string_from_der(data: bytes) -> str:
    """Best-effort extraction of a UTF-8 string from DER-encoded extension bytes.

    Node-forge encodes extension values as an ASN.1 UTF8String (tag 0x0C)
    or sometimes as raw bytes.  We handle both cases.
    """
    if not data:
        return ""
    # If the first byte is an ASN.1 tag (e.g. 0x0C=UTF8String, 0x13=PrintableString,
    # 0x16=IA5String, 0x04=OctetString), skip the tag+length header.
    if data[0] in (0x0C, 0x13, 0x16, 0x04) and len(data) > 2:
        length = data[1]
        if length == len(data) - 2:
            return data[2:].decode("utf-8", errors="replace").strip()
    # Fallback: decode the entire blob as UTF-8
    return data.decode("utf-8", errors="replace").strip()


def _get_extension_value(
    cert: LicenseCertificate,
    standard_oid: ObjectIdentifier,
    legacy_oid: ObjectIdentifier | None = None,
) -> str | None:
    """Extract a custom extension value from the certificate."""
    for oid in (standard_oid, legacy_oid):
        if oid is None:
            continue
        for ext in cert.extensions:
            if ext.oid == oid:
                raw = ext.value
                if isinstance(raw, bytes):
                    return _extract_string_from_der(raw)
                if hasattr(raw, "value") and isinstance(raw.value, bytes):
                    return _extract_string_from_der(raw.value)
    return None


def extract_license_product(pem: str) -> str | None:
    """Return the product a licence PEM is issued for (e.g. ``"opencti"``).

    Reads the product OID (standard ``62944.20`` / legacy ``2.14521.4.4.20``)
    without verifying the signature — the caller only needs to know which
    product signed the cert so it can forward it under the matching
    ``X-<Product>-Certificate`` header. Returns ``None`` when the PEM is
    unparseable or carries no product OID.
    """
    try:
        cert = load_license_certificate(pem)
    except Exception:
        return None
    return _get_extension_value(cert, LICENSE_OID_PRODUCT, LICENSE_LEGACY_OID_PRODUCT)


def _parse_json_list(raw: str | None) -> list[str]:
    """Parse a JSON-encoded string list from a certificate extension value."""
    if not raw:
        return []
    try:
        import json

        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
    except (json.JSONDecodeError, TypeError):
        pass  # malformed extension data; return empty list
    return []


def _parse_int(raw: str | None, default: int = 0) -> int:
    """Parse a numeric string from a certificate extension value."""
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def decode_xtm_license_pem(
    pem: str,
    ca_pem: str,
    platform_id: str | None = None,
    platform_created_at: datetime | None = None,
) -> XTMLicenseInfo:
    """Parse and validate a Filigran license PEM certificate.

    Extracts the standard license fields plus XTM-specific extensions:
    sub-licensing platform IDs and agentic quotas (the two things a license
    decides), and the informational legacy capability flags.
    """
    base = decode_license_pem(
        pem,
        ca_pem,
        "filigran xtm",
        platform_id=platform_id,
        platform_created_at=platform_created_at,
    )

    info = XTMLicenseInfo(
        license_enterprise=base.license_enterprise,
        license_validated=base.license_validated,
        license_by_configuration=base.license_by_configuration,
        license_valid_cert=base.license_valid_cert,
        license_customer=base.license_customer,
        license_type=base.license_type,
        license_creator=base.license_creator,
        license_platform=base.license_platform,
        license_global=base.license_global,
        license_platform_match=base.license_platform_match,
        license_start_date=base.license_start_date,
        license_expiration_date=base.license_expiration_date,
        license_expired=base.license_expired,
        license_extra_expiration=base.license_extra_expiration,
        license_extra_expiration_days=base.license_extra_expiration_days,
        license_expiration_prevention=base.license_expiration_prevention,
        license_certificate_pem=base.license_certificate_pem,
        license_refusal_reason=base.license_refusal_reason,
    )

    if info.license_type == "ci" and platform_created_at is None:
        # A ci license is dated from the platform's creation: with no date to
        # count from, the 45-minute cap cannot apply and the certificate would
        # run to its own end. Refused, as OpenCTI's xtm-one-license.ts does.
        info.license_validated = False

    if not base.license_valid_cert:
        return info

    try:
        client_cert = load_license_certificate(pem)

        info.opencti_ids = _parse_json_list(
            _get_extension_value(client_cert, LICENSE_OID_OPENCTI_IDS)
        )
        info.openaev_ids = _parse_json_list(
            _get_extension_value(client_cert, LICENSE_OID_OPENAEV_IDS)
        )
        info.opencrq_ids = _parse_json_list(
            _get_extension_value(client_cert, LICENSE_OID_OPENCRQ_IDS)
        )
        copilot_raw = _get_extension_value(client_cert, LICENSE_OID_COPILOT)
        info.copilot_enabled = copilot_raw == "true"
        agentic_ai_raw = _get_extension_value(client_cert, LICENSE_OID_AGENTIC_AI)
        info.agentic_ai_enabled = agentic_ai_raw == "true"
        # A quota claim of 0 and a missing claim both parse to 0 and both mean
        # no ceiling, on every certificate whatever its start date (see
        # ``ceiling_or_none`` in services/quota.py). Since 27 Sep 2026 the
        # issuer writes Unlimited as no claim, never a 0.
        info.daily_quota = _parse_int(_get_extension_value(client_cert, LICENSE_OID_DAILY_QUOTA))
        info.monthly_quota = _parse_int(_get_extension_value(client_cert, LICENSE_OID_MONTHLY_QUOTA))
        info.yearly_quota = _parse_int(_get_extension_value(client_cert, LICENSE_OID_YEARLY_QUOTA))
        info.license_model = max(1, _parse_int(_get_extension_value(client_cert, LICENSE_OID_LICENSE_MODEL), 1))

        logger.info(
            "Filigran license extensions: copilot=%s, agentic_ai=%s, daily=%d, monthly=%d, yearly=%d, "
            "opencti_ids=%s, openaev_ids=%s, opencrq_ids=%s",
            info.copilot_enabled,
            info.agentic_ai_enabled,
            info.daily_quota,
            info.monthly_quota,
            info.yearly_quota,
            info.opencti_ids,
            info.openaev_ids,
            info.opencrq_ids,
        )
    except Exception:
        logger.debug("Failed to extract Filigran license extensions", exc_info=True)

    return info


def _load_trust_anchor_public_key(ca_pem: str) -> rsa.RSAPublicKey:
    """Resolve the RSA public key of a trust anchor PEM.

    Accepts either an x509 CA certificate PEM (the historical form) or a
    bare ``PUBLIC KEY`` PEM pinning the product signing key directly. The
    filigran-licenses application signs every license with the product
    keypair and its product CA certificate is only a self-signed wrapper
    around that same key, so pinning the public key is an equivalent trust
    anchor - used for products whose CA certificate is not embedded here
    (OpenCRQ, whose signing key was extracted from a genuine product
    license - see opencrq's ``xtm-one-ee.ts``).
    """
    if "-----BEGIN PUBLIC KEY-----" in ca_pem:
        public_key = serialization.load_pem_public_key(ca_pem.encode())
    else:
        public_key = load_license_certificate(ca_pem).public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise TypeError("License trust anchor must carry an RSA public key.")
    return public_key


def decode_license_pem(
    pem: str,
    ca_pem: str,
    expected_product: str,
    platform_id: str | None = None,
    platform_created_at: datetime | None = None,
) -> LicenseInfo:
    """Parse and validate a license PEM certificate.

    Args:
        pem: The license certificate PEM string.
        ca_pem: The trust anchor PEM used to verify the signature - either
            a CA certificate or a bare product signing public key.
        expected_product: Expected product name (e.g. ``"copilot"``).
        platform_id: Current platform instance ID for platform-match check.
            ``None`` skips the platform match (always passes).
        platform_created_at: Timestamp when the platform instance was first
            created (``PlatformSettings.created_at``).  When provided for a
            ``ci``-type license the effective expiration is capped to
            ``min(platform_created_at + 45 min, cert_notBefore + 1 yr)``,
            mirroring OpenCTI's ``licensing.ts`` behaviour.

    Returns:
        Populated ``LicenseInfo`` with validation results.
    """
    now = datetime.now(UTC)
    info = LicenseInfo(license_enterprise=True)

    try:
        client_cert = load_license_certificate(pem)

        # Verify signature chain
        ca_public_key = _load_trust_anchor_public_key(ca_pem)
        ca_public_key.verify(
            client_cert.signature,
            client_cert.tbs_certificate_bytes,
            padding.PKCS1v15(),
            client_cert.signature_hash_algorithm,
        )
        info.license_valid_cert = True
        info.license_certificate_pem = client_cert.public_bytes(
            serialization.Encoding.PEM
        ).decode("ascii")
        logger.info("License cert signature verified against CA")

        # Extract OID extensions
        license_type = _get_extension_value(
            client_cert, LICENSE_OID_TYPE, LICENSE_LEGACY_OID_TYPE
        )
        license_product = _get_extension_value(
            client_cert, LICENSE_OID_PRODUCT, LICENSE_LEGACY_OID_PRODUCT
        )
        license_creator = _get_extension_value(
            client_cert, LICENSE_OID_CREATOR, LICENSE_LEGACY_OID_CREATOR
        )
        # Subject fields: O = customer, OU = platform/instance ID
        subject = client_cert.subject
        org = subject.get_attributes_for_oid(x509.oid.NameOID.ORGANIZATION_NAME)
        ou = subject.get_attributes_for_oid(x509.oid.NameOID.ORGANIZATIONAL_UNIT_NAME)
        logger.info(
            "License OIDs: type=%r, product=%r, creator=%r, ou=%r (expected product=%r)",
            license_type,
            license_product,
            license_creator,
            ou[0].value if ou else GLOBAL_LICENSE,
            expected_product,
        )

        info.license_type = license_type or "trial"
        info.license_creator = license_creator or "Unknown"

        valid_type = info.license_type in LICENSE_TYPES
        valid_product = license_product == expected_product

        info.license_customer = org[0].value if org else "Unknown"
        info.license_platform = ou[0].value if ou else "global"
        info.license_global = info.license_platform == GLOBAL_LICENSE

        info.license_platform_match = (
            valid_product
            and valid_type
            and (
                info.license_global
                or platform_id is None
                or info.license_platform == platform_id
            )
        )

        info.license_start_date = client_cert.not_valid_before_utc
        info.license_expiration_date = client_cert.not_valid_after_utc

        # CI-specific expiration override (mirrors OpenCTI licensing.ts).
        # Effective expiry = min(platform_created_at + 45 min, cert_notBefore + 1 yr).
        # This prevents CI licenses from silently living beyond the pipeline run
        # that issued them, regardless of the certificate's stated validity window.
        if info.license_type == "ci" and platform_created_at is not None:
            ci_platform_end = platform_created_at + timedelta(minutes=45)
            cert_end = info.license_start_date + timedelta(days=365)
            info.license_expiration_date = min(ci_platform_end, cert_end)

        info.license_expired = (
            now > info.license_expiration_date or now < info.license_start_date
        )

        # Expiration prevention warning (< 3 months left)
        if info.license_type not in ("trial", "ci"):
            remaining = info.license_expiration_date - now
            info.license_expiration_prevention = remaining < timedelta(days=90)

        # Determine validated status
        info.license_validated = info.license_valid_cert and info.license_platform_match

        if info.license_validated and info.license_expired:
            if info.license_type in ("trial", "ci"):
                info.license_validated = False
            else:
                grace_end = info.license_expiration_date + timedelta(days=90)
                info.license_extra_expiration = now < grace_end
                info.license_extra_expiration_days = max(
                    0, int((grace_end - now).total_seconds() / 86400)
                )
                info.license_validated = info.license_extra_expiration

        if not info.license_validated:
            info.license_refusal_reason = _refusal_reason(
                info, license_product, expected_product, platform_id, platform_created_at, now
            )

    except InvalidSignature:
        info.license_refusal_reason = (
            f"its signature does not match the {expected_product!r} certificate authority"
        )
    except Exception as exc:
        logger.debug("License PEM validation failed", exc_info=True)
        info.license_refusal_reason = (
            f"the certificate could not be read ({type(exc).__name__}: {exc})"
        )

    return info


def _refusal_reason(
    info: LicenseInfo,
    product: str | None,
    expected_product: str,
    platform_id: str | None,
    platform_created_at: datetime | None,
    now: datetime,
) -> str:
    """Name the check that refused a certificate whose signature verified."""
    if product != expected_product:
        return f"its product is {product!r}, not {expected_product!r}"
    if info.license_type not in LICENSE_TYPES:
        return f"its type {info.license_type!r} is not one of {', '.join(LICENSE_TYPES)}"
    if not info.license_platform_match:
        return f"it is issued for the platform {info.license_platform!r}, not {platform_id!r}"
    if now < info.license_start_date:
        return f"it is not valid before its notBefore, {info.license_start_date.isoformat()}"
    end = info.license_expiration_date.isoformat()
    if info.license_type == "ci" and platform_created_at is not None:
        return (
            f"this ci license ended at {end}: 45 minutes after the platform's creation "
            f"({platform_created_at.isoformat()}) or 365 days after its notBefore "
            f"({info.license_start_date.isoformat()}), whichever comes first"
        )
    if info.license_type in ("trial", "ci"):
        return f"it ended at {end}"
    return f"it ended at {end} and its 90-day grace period is over"


# ── Cached licence resolution ────────────────────────────────────────────────

# cache_key -> (info, active_pem, platform_created_at, cached_at_ts).
# ``platform_created_at`` participates in the validity check for ``ci``-type
# licenses so an entry decoded without the expiration cap can never be served
# to a caller that provides a timestamp (and vice versa) within the TTL.
_cache: dict[str, tuple[LicenseInfo, str | None, datetime | None, float]] = {}


def get_license_info(
    pem_from_env: str,
    pem_from_db: str | None,
    ca_pem: str,
    expected_product: str,
    platform_id: str | None = None,
    cache_key: str = "copilot",
) -> LicenseInfo:
    """Return cached licence info, re-parsing only when the PEM changes.

    Priority: env var PEM > DB PEM (same dual-source pattern as OpenCTI).
    """
    active_pem = pem_from_env if pem_from_env else pem_from_db
    by_config = bool(pem_from_env)

    now_ts = time.time()
    cached = _cache.get(cache_key)
    if cached:
        cached_info, cached_pem, _, cached_at = cached
        if cached_pem == active_pem and (now_ts - cached_at) < CACHE_TTL_SECONDS:
            return cached_info

    if active_pem:
        info = decode_license_pem(active_pem, ca_pem, expected_product, platform_id)
        info.license_by_configuration = by_config
    else:
        info = LicenseInfo()

    _cache[cache_key] = (info, active_pem, None, now_ts)
    return info


def get_xtm_license_info(
    pem_from_env: str,
    pem_from_db: str | None,
    ca_pem: str,
    platform_id: str | None = None,
    platform_created_at: datetime | None = None,
    cache_key: str = "xtm",
) -> XTMLicenseInfo:
    """Return cached Filigran licence info, re-parsing only when the PEM changes."""
    active_pem = pem_from_env if pem_from_env else pem_from_db
    by_config = bool(pem_from_env)

    now_ts = time.time()
    cached = _cache.get(cache_key)
    if cached:
        cached_info, cached_pem, cached_platform_created_at, cached_at = cached
        # Only ``ci`` licenses depend on ``platform_created_at`` (the 45-min
        # expiration cap), so only they need re-decoding when it changes.
        ci_cap_consistent = (
            cached_info.license_type != "ci"
            or cached_platform_created_at == platform_created_at
        )
        if (
            cached_pem == active_pem
            and (now_ts - cached_at) < CACHE_TTL_SECONDS
            and isinstance(cached_info, XTMLicenseInfo)
            and ci_cap_consistent
        ):
            return cached_info

    if active_pem:
        info = decode_xtm_license_pem(
            active_pem,
            ca_pem,
            platform_id=platform_id,
            platform_created_at=platform_created_at,
        )
        info.license_by_configuration = by_config
        if not info.license_validated:
            # Once per decode, so at most once per cache TTL and cache key.
            logger.warning(
                "The Filigran license from %s is refused: %s",
                "ENTERPRISE_LICENSE" if by_config else "the settings",
                info.license_refusal_reason,
            )
    else:
        info = XTMLicenseInfo()

    _cache[cache_key] = (info, active_pem, platform_created_at, now_ts)
    return info


def clear_license_cache(cache_key: str = "copilot") -> None:
    """Force re-validation on next call."""
    _cache.pop(cache_key, None)


# ── Install-time validation ─────────────────────────────────────────────────


class LicenseValidationError(Exception):
    """Raised when an admin tries to install a license PEM that fails validation.

    Carries a single ``message`` attribute suitable for direct display in the UI.
    The API layer converts this into ``HTTPException(400, detail=message)`` so
    the frontend can surface a meaningful error instead of silently succeeding
    on a junk certificate.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def validate_enterprise_license_for_install(
    pem: str,
    *,
    ca_pem: str,
    platform_id: str | None = None,
    platform_created_at: datetime | None = None,
) -> XTMLicenseInfo:
    """Validate a license PEM before persisting it as the platform license.

    Performs the full chain of checks: parse, signature, product, platform
    match, expiration with grace period. Nothing else — a valid Filigran license is
    a valid Filigran license. The retired three-tier model additionally refused a
    certificate lacking the ``agentic_ai`` / ``copilot`` capability flag; under
    the XTM Platform model the license only carries the module editions and
    the acts quota, so a certificate without those flags activates exactly
    the same platform as one with them, and refusing it would send an
    administrator to Sales for a switch that changes nothing.

    When ``platform_created_at`` is provided, ``ci``-type licenses are
    checked against the same capped expiration used at runtime, so a CI
    license that is already past its 45-minute window is rejected up-front.

    Raises
    ------
    LicenseValidationError
        With a user-facing ``message`` describing the failure.
    """
    info = decode_xtm_license_pem(
        pem,
        ca_pem,
        platform_id=platform_id,
        platform_created_at=platform_created_at,
    )

    if not info.license_valid_cert:
        raise LicenseValidationError(
            "License is invalid: certificate could not be parsed or its "
            "signature does not match the Filigran certificate authority.",
        )
    if not info.license_platform_match:
        # Covers wrong product OID, unknown license type, and (when a
        # platform_id is provided) an instance mismatch.
        raise LicenseValidationError(
            "License product, type, or platform does not match this "
            "deployment. Verify the license was issued by Filigran for "
            "this product, or request a global license.",
        )
    if info.license_expired and not info.license_extra_expiration:
        expired_on = info.license_expiration_date.date().isoformat()
        raise LicenseValidationError(
            f"License expired on {expired_on} and is past the grace period. "
            "Request a renewed license from Filigran.",
        )
    if info.license_type == "ci" and platform_created_at is None:
        raise LicenseValidationError(
            "A ci license cannot be dated without the platform's creation date.",
        )
    if not info.license_validated:
        raise LicenseValidationError(
            "License is invalid. Check that it was issued by Filigran and "
            "has not been altered or truncated.",
        )

    return info

"""Tests for the channel webhook authentication helpers.

Covers the pure verification helpers added by the channel-webhook-security
work: Slack signature hardening, the Bot Framework serviceUrl allowlist,
Meta HMAC verification, Discord Ed25519 verification, and the shared
``timing_safe_str_eq`` / ``warn_unauthenticated_once`` /
``DECRYPT_FAILED_CREDENTIAL`` primitives in ``channels/base.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

from app.channels.base import (
    DECRYPT_FAILED_CREDENTIAL,
    timing_safe_str_eq,
    warn_unauthenticated_once,
)
from app.channels.discord import _verify_discord_signature
from app.channels.teams import _is_botframework_service_url
from app.channels.whatsapp import _verify_meta_signature
from app.core.security import verify_slack_signature

# ── Slack signature verification ─────────────────────────────────────────────


def _slack_sign(body: bytes, secret: str, ts: str) -> str:
    base = f"v0:{ts}:{body.decode()}"
    return "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


def test_slack_signature_valid():
    body = b'{"type":"event_callback"}'
    secret = "test-secret"
    ts = str(int(time.time()))
    sig = _slack_sign(body, secret, ts)
    assert verify_slack_signature(body, secret, sig, ts) is True


def test_slack_signature_wrong_secret_rejected():
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    sig = _slack_sign(body, "other-secret", ts)
    assert verify_slack_signature(body, "test-secret", sig, ts) is False


def test_slack_signature_expired_timestamp_rejected():
    body = b"{}"
    secret = "test-secret"
    ts = str(int(time.time()) - 3600)
    sig = _slack_sign(body, secret, ts)
    assert verify_slack_signature(body, secret, sig, ts) is False


def test_slack_signature_non_numeric_timestamp_is_401_not_500():
    # Crafted timestamp header must yield a clean False, never a ValueError.
    assert verify_slack_signature(b"{}", "s", "v0=abc", "not-a-number") is False


def test_slack_signature_nan_inf_timestamps_rejected():
    # float("nan") / float("inf") parse successfully — math.isfinite guards.
    assert verify_slack_signature(b"{}", "s", "v0=abc", "nan") is False
    assert verify_slack_signature(b"{}", "s", "v0=abc", "inf") is False


def test_slack_signature_non_utf8_body_is_401_not_500():
    ts = str(int(time.time()))
    assert verify_slack_signature(b"\xff\xfe", "s", "v0=abc", ts) is False


# ── Bot Framework serviceUrl allowlist ───────────────────────────────────────


def test_botframework_service_url_accepts_real_hosts():
    assert _is_botframework_service_url("https://smba.trafficmanager.net/apis/")
    assert _is_botframework_service_url("https://emea.smba.trafficmanager.net/emea/")
    assert _is_botframework_service_url("https://EMEA.SMBA.TRAFFICMANAGER.NET/x")


def test_botframework_service_url_rejects_bypass_attempts():
    # http scheme
    assert not _is_botframework_service_url("http://smba.trafficmanager.net/apis/")
    # userinfo trick: everything before @ is credentials, host is evil.com
    assert not _is_botframework_service_url(
        "https://smba.trafficmanager.net@evil.com/apis/"
    )
    # suffix-extension trick
    assert not _is_botframework_service_url(
        "https://smba.trafficmanager.net.evil.com/apis/"
    )
    # sibling-subdomain trick
    assert not _is_botframework_service_url("https://attacker.trafficmanager.net/x")
    # empty / garbage
    assert not _is_botframework_service_url("")
    assert not _is_botframework_service_url("not a url")


# ── Meta (WhatsApp) X-Hub-Signature-256 ──────────────────────────────────────


def test_meta_signature_valid():
    body = b'{"entry":[]}'
    secret = "app-secret"
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert _verify_meta_signature(body, secret, sig) is True


def test_meta_signature_wrong_secret_rejected():
    body = b'{"entry":[]}'
    sig = "sha256=" + hmac.new(b"other", body, hashlib.sha256).hexdigest()
    assert _verify_meta_signature(body, "app-secret", sig) is False


def test_meta_signature_missing_prefix_rejected():
    assert _verify_meta_signature(b"{}", "s", "md5=abc") is False


def test_meta_signature_non_ascii_header_is_mismatch_not_500():
    # Starlette decodes headers as latin-1, so crafted bytes >= 0x80 reach the
    # comparison as non-ASCII str — must return False, never raise TypeError.
    assert _verify_meta_signature(b"{}", "s", "sha256=\xe9\xff") is False


# ── Discord Ed25519 ──────────────────────────────────────────────────────────


def test_discord_signature_roundtrip():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    private_key = Ed25519PrivateKey.generate()
    public_hex = private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    ).hex()
    ts = "1719900000"
    body = b'{"type":1}'
    signature_hex = private_key.sign(ts.encode() + body).hex()

    assert _verify_discord_signature(public_hex, signature_hex, ts, body) is True
    # Tampered body must fail.
    assert _verify_discord_signature(public_hex, signature_hex, ts, b"{}") is False


def test_discord_signature_bad_hex_is_false_not_500():
    assert _verify_discord_signature("not-hex", "also-not-hex", "0", b"") is False
    # The decrypt-failure sentinel is not valid hex → always rejects.
    assert (
        _verify_discord_signature(DECRYPT_FAILED_CREDENTIAL, "00" * 64, "0", b"")
        is False
    )


# ── Shared primitives ────────────────────────────────────────────────────────


def test_timing_safe_str_eq_basic():
    assert timing_safe_str_eq("abc", "abc") is True
    assert timing_safe_str_eq("abc", "abd") is False
    assert timing_safe_str_eq("", "") is True


def test_timing_safe_str_eq_non_ascii_never_raises():
    # hmac.compare_digest on str raises TypeError for non-ASCII — the bytes
    # comparison must turn crafted header input into a clean mismatch.
    assert timing_safe_str_eq("caf\xe9", "cafe") is False
    assert timing_safe_str_eq("caf\xe9", "caf\xe9") is True
    # Lone surrogates (surrogateescape round-trip) must not raise either.
    assert timing_safe_str_eq("\udcff", "x") is False


def test_decrypt_failed_sentinel_never_matches_real_credentials():
    assert timing_safe_str_eq(DECRYPT_FAILED_CREDENTIAL, "") is False
    assert timing_safe_str_eq(DECRYPT_FAILED_CREDENTIAL, "real-secret") is False
    # Non-empty so `if credential:` guards treat it as configured (fail closed).
    assert DECRYPT_FAILED_CREDENTIAL


def test_warn_unauthenticated_once_logs_once_per_key(caplog):
    from app.channels import base as channels_base

    channels_base._unauthenticated_warned.discard("test-once-key")
    with caplog.at_level(logging.WARNING, logger="app.channels.base"):
        warn_unauthenticated_once("test-once-key", "endpoint is UNAUTHENTICATED")
        warn_unauthenticated_once("test-once-key", "endpoint is UNAUTHENTICATED")
    matching = [r for r in caplog.records if "UNAUTHENTICATED" in r.message]
    assert len(matching) == 1
    channels_base._unauthenticated_warned.discard("test-once-key")

"""Support codes of every kind, verified offline.

The ``platform_quota_reset`` kind resets the platform-wide usage counters the
license ceilings are enforced against. What is pinned here needs no database:
which kinds a route accepts, how a reset's scope is read off the signed
payload (and refused when it is not sane), what the audit trail says, and that
the minting script and the platform agree. The counters themselves are
exercised against PostgreSQL in ``test_platform_quota_reset_postgres.py``.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import subprocess
import sys
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import app.enterprise.xtm_ca as xtm_ca_mod
from app.services.agent_failure_pool import (
    PLATFORM_QUOTA_RESET_KIND,
    POOL_RECHARGE_KIND,
    SUPPORT_CODE_PREFIX,
    SupportCodeError,
    verify_support_code,
)
from app.services.quota import PLATFORM_RESETTABLE_PERIODS, PLATFORM_RESETTABLE_QUOTA_TYPES
from app.services.support_codes import (
    SUPPORTED_KINDS,
    describe_grant,
    platform_quota_reset_scope,
    verify_redeemable_code,
)

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "mint_support_code.py"


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return key, public_pem


def _mint(key, **overrides) -> str:
    payload = {
        "v": 1,
        "kind": PLATFORM_QUOTA_RESET_KIND,
        "id": str(uuid.uuid4()),
        "platform": "global",
        "exp": "2099-01-01T00:00:00Z",
    }
    payload.update(overrides)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = key.sign(raw, padding.PKCS1v15(), hashes.SHA256())

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    return f"{SUPPORT_CODE_PREFIX}{b64(raw)}.{b64(signature)}"


@pytest.fixture
def signing_key(monkeypatch):
    """Stand in for the Filigran product key: sign here, trust the public half."""
    key, public_pem = _keypair()
    monkeypatch.setattr(xtm_ca_mod, "XTM_CA_PEM", public_pem)
    return key


# ---------------------------------------------------------------------------
# Which kinds a caller accepts
# ---------------------------------------------------------------------------


def test_a_reset_code_verifies_where_resets_are_accepted():
    key, public_pem = _keypair()
    verified = verify_support_code(
        _mint(key, note="POC-118"), public_pem, None, expected_kind=SUPPORTED_KINDS
    )
    assert verified.kind == PLATFORM_QUOTA_RESET_KIND
    assert verified.note == "POC-118"


def test_a_reset_code_is_not_a_pool_recharge():
    """The pool-only route keeps refusing anything but a recharge, and says
    what the code would have granted."""
    key, public_pem = _keypair()
    with pytest.raises(SupportCodeError) as refused:
        verify_support_code(_mint(key), public_pem, None)
    assert str(refused.value) == (
        "This support code is valid but does not grant an agent failure pool "
        "recharge: it grants a reset of the platform-wide usage counters."
    )


def test_a_recharge_code_is_not_a_reset():
    key, public_pem = _keypair()
    with pytest.raises(SupportCodeError) as refused:
        verify_support_code(
            _mint(key, kind=POOL_RECHARGE_KIND),
            public_pem,
            None,
            expected_kind=PLATFORM_QUOTA_RESET_KIND,
        )
    assert str(refused.value) == (
        "This support code is valid but does not grant a reset of the "
        "platform-wide usage counters: it grants an agent failure pool recharge."
    )


def test_a_kind_this_platform_does_not_know_is_refused_as_such():
    key, public_pem = _keypair()
    with pytest.raises(SupportCodeError) as refused:
        verify_support_code(
            _mint(key, kind="future_grant"), public_pem, None, expected_kind=SUPPORTED_KINDS
        )
    assert str(refused.value) == (
        "This support code is valid but does not grant a reset of the "
        "platform-wide usage counters or an agent failure pool recharge: it "
        "grants something this version of the platform cannot apply."
    )


def test_the_signed_payload_travels_with_the_verified_code():
    key, public_pem = _keypair()
    verified = verify_support_code(
        _mint(key, periods=["daily"]), public_pem, None, expected_kind=SUPPORTED_KINDS
    )
    assert verified.claims["periods"] == ["daily"]
    assert verified.claims["kind"] == PLATFORM_QUOTA_RESET_KIND


# ---------------------------------------------------------------------------
# The scope of a reset
# ---------------------------------------------------------------------------


def _scope(key, public_pem, **claims):
    verified = verify_support_code(
        _mint(key, **claims), public_pem, None, expected_kind=SUPPORTED_KINDS
    )
    return platform_quota_reset_scope(verified)


def test_a_reset_without_a_scope_resets_the_agentic_ceiling_in_every_window():
    """Agentic and sandbox are the two counters an agentic ceiling adds up."""
    key, public_pem = _keypair()
    assert _scope(key, public_pem) == (
        ("agentic", "sandbox"),
        ("daily", "monthly", "yearly"),
    )
    assert _scope(key, public_pem, quota_types=None, periods=None) == (
        ("agentic", "sandbox"),
        ("daily", "monthly", "yearly"),
    )


def test_a_narrowed_scope_is_read_in_a_stable_order_once_each():
    key, public_pem = _keypair()
    assert _scope(
        key,
        public_pem,
        quota_types=["llm_proxy", "agentic", "agentic"],
        periods=["yearly", "daily"],
    ) == (("agentic", "llm_proxy"), ("daily", "yearly"))


@pytest.mark.parametrize(
    "claims",
    [
        {"quota_types": []},
        {"quota_types": "agentic"},
        {"quota_types": ["tokens"]},
        {"quota_types": ["agentic", None]},
        {"quota_types": [1]},
        {"quota_types": {"agentic": True}},
        {"periods": []},
        {"periods": "daily"},
        {"periods": ["weekly"]},
        {"periods": ["Daily"]},
        {"periods": [["daily"]]},
    ],
)
def test_a_scope_that_is_not_sane_is_refused(claims):
    """Signed is not sane: a scope the platform would have to guess at resets
    nothing, and says so."""
    key, public_pem = _keypair()
    with pytest.raises(SupportCodeError) as refused:
        _scope(key, public_pem, **claims)
    assert str(refused.value) == (
        "This support code carries a reset scope this version of the platform "
        "cannot apply. Ask Filigran support for a new code."
    )


def test_verify_redeemable_code_refuses_a_bad_scope_before_any_database(signing_key):
    """The route verifies before it writes its audit row: a malformed reset
    is refused there, so it is neither audited nor burned."""
    with pytest.raises(SupportCodeError, match="reset scope"):
        verify_redeemable_code(_mint(signing_key, periods=["weekly"]))


def test_verify_redeemable_code_accepts_both_kinds(signing_key):
    assert verify_redeemable_code(_mint(signing_key)).kind == PLATFORM_QUOTA_RESET_KIND
    assert (
        verify_redeemable_code(_mint(signing_key, kind=POOL_RECHARGE_KIND)).kind
        == POOL_RECHARGE_KIND
    )


def test_the_audit_trail_names_what_a_code_granted(signing_key):
    reset = verify_redeemable_code(
        _mint(signing_key, id="reset-1", quota_types=["agentic"], periods=["monthly"])
    )
    assert describe_grant(reset) == (
        "Reset the platform-wide usage counters (agentic; current monthly "
        "windows) with support code reset-1"
    )
    recharge = verify_redeemable_code(
        _mint(signing_key, id="recharge-1", kind=POOL_RECHARGE_KIND)
    )
    assert describe_grant(recharge) == (
        "Recharged all agent failure pools with support code recharge-1"
    )


# ---------------------------------------------------------------------------
# The minting script and the platform agree
# ---------------------------------------------------------------------------


def _load_script():
    spec = importlib.util.spec_from_file_location("mint_support_code", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_offers_exactly_what_the_platform_resets():
    module = _load_script()
    assert module.PLATFORM_QUOTA_RESET_KIND == PLATFORM_QUOTA_RESET_KIND
    assert set(module.KINDS) == set(SUPPORTED_KINDS)
    assert module.QUOTA_TYPES == PLATFORM_RESETTABLE_QUOTA_TYPES
    assert module.PERIODS == PLATFORM_RESETTABLE_PERIODS


def _run_script(tmp_path, key, *args) -> subprocess.CompletedProcess:
    key_file = tmp_path / "product-key.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return subprocess.run(  # noqa: S603 - this interpreter, the repo's own script
        [sys.executable, str(_SCRIPT), "--key", str(key_file), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_reset_minted_by_the_script_verifies_with_its_scope(tmp_path):
    key, public_pem = _keypair()
    minted = _run_script(
        tmp_path,
        key,
        "--kind",
        PLATFORM_QUOTA_RESET_KIND,
        "--quota-types",
        "agentic",
        "--periods",
        "monthly",
        "daily",
        "--note",
        "POC-118",
    )
    assert minted.returncode == 0, minted.stderr
    verified = verify_support_code(
        minted.stdout.strip(), public_pem, None, expected_kind=SUPPORTED_KINDS
    )
    assert verified.kind == PLATFORM_QUOTA_RESET_KIND
    assert verified.note == "POC-118"
    assert platform_quota_reset_scope(verified) == (("agentic",), ("daily", "monthly"))


def test_the_script_refuses_a_scope_on_a_pool_recharge(tmp_path):
    key, _ = _keypair()
    refused = _run_script(tmp_path, key, "--periods", "daily")
    assert refused.returncode == 2
    assert "apply to --kind platform_quota_reset" in refused.stderr
    assert refused.stdout == ""

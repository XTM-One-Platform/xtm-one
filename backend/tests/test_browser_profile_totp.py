"""The platform's one TOTP implementation (``app.core.totp``), pinned to RFC 6238 appendix B.

Browser profile sign-ins and the 1Password integration's one-time password
tool both compute their codes with it.
"""

import base64

import pytest

from app.core import totp

# RFC 6238 appendix B: the seeds are ASCII digits, 20 / 32 / 64 bytes long.
_SEEDS = {
    "SHA1": b"12345678901234567890",
    "SHA256": b"12345678901234567890123456789012",
    "SHA512": b"1234567890123456789012345678901234567890123456789012345678901234",
}
_VECTORS = [
    (59, "94287082", "46119246", "90693936"),
    (1111111109, "07081804", "68084774", "25091201"),
    (1111111111, "14050471", "67062674", "99943326"),
    (1234567890, "89005924", "91819424", "93441116"),
    (2000000000, "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826"),
]


def _b32(seed: bytes) -> str:
    return base64.b32encode(seed).decode().rstrip("=")


@pytest.mark.parametrize(("timestamp", "sha1", "sha256", "sha512"), _VECTORS)
def test_rfc6238_vectors(timestamp, sha1, sha256, sha512):
    for algorithm, expected in (("SHA1", sha1), ("SHA256", sha256), ("SHA512", sha512)):
        uri = f"otpauth://totp/demo?secret={_b32(_SEEDS[algorithm])}&algorithm={algorithm}&digits=8&period=30"
        assert totp.code_at(totp.parse_secret(uri), timestamp) == expected


def test_a_plain_base32_secret_is_six_digits_of_thirty_seconds():
    secret = _b32(_SEEDS["SHA1"])
    assert totp.current_code(secret, now=59 - 10) == "287082"


def test_the_secret_is_read_the_way_people_paste_it():
    secret = _b32(_SEEDS["SHA1"]).lower()
    spaced = " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))
    assert totp.parse_secret(spaced).key == _SEEDS["SHA1"]
    assert totp.parse_secret(f"{secret}====").key == _SEEDS["SHA1"]


def test_an_otpauth_uri_gives_its_parameters_and_issuer():
    secret = _b32(_SEEDS["SHA1"])
    config = totp.parse_secret(
        f"otpauth://totp/GitHub:alice@example.com?secret={secret}&issuer=GitHub&algorithm=SHA256&digits=8&period=60"
    )
    assert (config.algorithm, config.digits, config.period, config.issuer) == ("SHA256", 8, 60, "GitHub")
    assert totp.parse_secret(f"otpauth://totp/Acme%20Corp:bob?secret={secret}").issuer == "Acme Corp"
    assert totp.parse_secret(secret).issuer == ""


def test_seconds_left_counts_down_the_window():
    config = totp.parse_secret(_b32(_SEEDS["SHA1"]))
    assert totp.seconds_left(config, 30.0) == 30
    assert totp.seconds_left(config, 59.0) == 1


def test_a_code_about_to_expire_is_replaced_by_the_next_window():
    secret = _b32(_SEEDS["SHA1"])
    config = totp.parse_secret(secret)
    # 29 s into the 30 s window: 1 s left, under the 3 s margin.
    assert totp.current_code(secret, now=29.0) == totp.code_at(config, 30.0)
    assert totp.current_code(secret, now=10.0) == totp.code_at(config, 10.0)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not base32 at all!",
        "ABCD",  # too short to be an authenticator key
        f"otpauth://hotp/demo?secret={_b32(_SEEDS['SHA1'])}",
        f"otpauth://totp/demo?secret={_b32(_SEEDS['SHA1'])}&algorithm=MD5",
        f"otpauth://totp/demo?secret={_b32(_SEEDS['SHA1'])}&digits=4",
        f"otpauth://totp/demo?secret={_b32(_SEEDS['SHA1'])}&period=0",
        f"otpauth://totp/demo?secret={_b32(_SEEDS['SHA1'])}&period=abc",
        "otpauth://totp/demo?issuer=NoSecret",
        f"otpauth://motp/demo?secret={_b32(_SEEDS['SHA1'])}",
    ],
)
def test_unreadable_secrets_are_refused_without_echoing_them(value):
    with pytest.raises(totp.TOTPSecretError) as exc:
        totp.validate_secret(value)
    if value:
        assert value not in str(exc.value)

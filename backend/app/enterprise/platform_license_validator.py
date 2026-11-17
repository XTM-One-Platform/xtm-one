"""Server-side license validation for registered XTM platforms.

When OpenCTI, OpenAEV, or OpenCRQ registers with XTM One, it sends its
enterprise license PEM.  XTM One validates it using the product's own trust
anchor — the product CA certificate, or the product signing public key when
the CA certificate is not distributed (both anchor the same keypair, see
``PRODUCT_CA_CERTS``).

This module embeds the trust anchors for all supported products so that
XTM One can independently verify any platform's license without trusting
the platform's self-reported EE status.
"""

from __future__ import annotations

import base64
import logging

from app.enterprise.license_certificate import MAX_LICENSE_PEM_CHARS
from app.enterprise.licensing import LicenseInfo, decode_license_pem

logger = logging.getLogger(__name__)

# The longest license a registering platform may send, checked before decoding:
# the largest PEM the loader parses, base64-encoded the way OpenAEV sends it.
_MAX_REGISTRATION_LICENSE_CHARS = 4 * -(-MAX_LICENSE_PEM_CHARS // 3)

# ── OpenCTI CA certificate ───────────────────────────────────────────────────
# Source: opencti-app/opencti-platform/opencti-graphql/src/enterprise-edition/opencti_ca.js
OPENCTI_CA_PEM = """\
-----BEGIN CERTIFICATE-----
MIIFZjCCA06gAwIBAgIBADANBgkqhkiG9w0BAQsFADBeMRkwFwYDVQQDExBGaWxp
Z3JhbiBDQSBDRVJUMQswCQYDVQQGEwJGUjEOMAwGA1UEBxMFUGFyaXMxETAPBgNV
BAoTCEZpbGlncmFuMREwDwYDVQQLEwhGaWxpZ3JhbjAeFw0yNDEyMTcyMzE2MTRa
Fw0zNDEyMTcyMzE2MTRaMF4xGTAXBgNVBAMTEEZpbGlncmFuIENBIENFUlQxCzAJ
BgNVBAYTAkZSMQ4wDAYDVQQHEwVQYXJpczERMA8GA1UEChMIRmlsaWdyYW4xETAP
BgNVBAsTCEZpbGlncmFuMIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA
l5q+YE+McTeaMRp/PQbeV1uq2dP3GpfkvwrU+p6IIwkbZHlSEnvDuVTzpmID66gF
wp8edZNvr9oY4Y29dz1ii8fyNrDE5SfCdNGp8U7FVaFoZabRdXWSUQlOlIdLxbvi
Ax63Islqk23M8YfPRMBrM/OBk2V70iJIeIkjRPFX3gE8KEHdg5A2JH3tXAt6l5GA
NkNHldNEjAIyiJle56QAPevWF5egt5bfsPUYsSQmu+8l38lEOX/HzzJ5KTmnFski
H7DzvYZ3O2i2ucx/VabCSK9XjRrcWRqdXEQT+2Qn2zji3X181inv9AtfK/kCKj8a
gb33ldm6cc1kCriBqr4sqm2IExC/G72NwI8AWkxIkAU0ug81loULJDNwzvpkcHl7
a5vuG0ojl1eh+Dtf4ZgFiTYtVAOGi5M9cgRUG0+7L9nRmJ11+w1L5V9JjH/3mmZl
UcyWSpAhkEViT75UJ6S8Q2h+XqxqGda6XU1LIRcYHfrtgvv49a102YhdrhDBP3qT
kY9mOQ4XeD2g9X2ZxOH4f3+CsZ2MvfreeyqncBaTp5/YETcTwu5HpvktbgwbB/Ag
OMSzQ4zNK+UTkTuqHHvri2i837/LQkjPb+M6N5ffmomgYcDXbuoJoOMM9XTnhss9
X49otzJHBxUNRJzEkl39LuxD3vGxzU9YxxPgfAZuaqUCAwEAAaMvMC0wDAYDVR0T
BAUwAwEB/zAdBgNVHQ4EFgQUd7Si3rM4fnC7SqwmBthYsR1w70UwDQYJKoZIhvcN
AQELBQADggIBAAwftRf/WPUTNflwSOeyc3+qjmkRdaM91qKBTlNreQcI4VDxiITX
uuQswMuGgUbDopaL9A37nTCgSeZgaeZbtQ6gcXRNjyYj98gBAHWL85I0LATr/KnA
PIVA6Ya2tSxLnEyuGsL2XFgF1NDOcJPnJ8JDE5lXqyDuEVdAIk7LHo9HEU8S8aR0
AQBNWQRu0S3+5R2WI6WpIsoVkW+EfVN06ZYd+j5nP2cORPJ3rXkxh1W5hCJ/9X8z
wvDWSg3tykN6eVo4c4uaxwsnh8lJIJd2di0Z4bhVymXuOwpW0yU0mDAk9ApevIXo
0JTuExYHKYUAA5jy68zFiXO168rVcAxpmemd4mpZ92YrnvTd8ayqzPaTvsLHNvcW
JEG2LcrxnyrCLqQ63QBmFq6XicdlmH9SzbxKPZZD6huZYzGY5wzl89N1Oy04LZ07
biqSr2r4HufyZokJu2H4JZWoYGT8GMlG4eEIglPoNBAnGp5s9Gs4iaciUQX9m5IC
hVYFw9JuxfNd898k+gAV+VYygRf7fHFwjHw7f3zC2XbhLcPvu19pZwjr+jS0p+Ga
25ZshzAeGJcgWdGFuk26hf8xjCVm1h42lbCb4NeVUYTfP23BDuMjF4AYdY2+h3zF
NB0yB3Uu71hyoxLJx3Bxs84tGtu9usF1HVwf4BPU88LerIpLJl24UWYw
-----END CERTIFICATE-----"""

# ── OpenAEV CA certificate ───────────────────────────────────────────────────
# Source: openaev-app/openaev-api/src/main/java/io/openaev/ee/Pem.java
OPENAEV_CA_PEM = """\
-----BEGIN CERTIFICATE-----
MIIFZjCCA06gAwIBAgIBADANBgkqhkiG9w0BAQsFADBeMRkwFwYDVQQDExBGaWxp
Z3JhbiBDQSBDRVJUMQswCQYDVQQGEwJGUjEOMAwGA1UEBxMFUGFyaXMxETAPBgNV
BAoTCEZpbGlncmFuMREwDwYDVQQLEwhGaWxpZ3JhbjAeFw0yNTAxMTMyMDMxMDNa
Fw0zNTAxMTMyMDMxMDNaMF4xGTAXBgNVBAMTEEZpbGlncmFuIENBIENFUlQxCzAJ
BgNVBAYTAkZSMQ4wDAYDVQQHEwVQYXJpczERMA8GA1UEChMIRmlsaWdyYW4xETAP
BgNVBAsTCEZpbGlncmFuMIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA
thsWRiSc9uzqlUteifNB8YQQejpLCGhJdHlEI2BJIG62tJz302zm+fzCA5mmDzKB
vWiJelvBHw1ily9T1cRreGGy48frTWZ+/jNI/MqTCXppIfXGX9lQmUm+gHJsFlqJ
uHZYgMJCSpNlTzxuMyk33EpY/RvIl6X4tVJwkl5Ati3coEOnNZlxqXnjG6DJrM0K
zAgQyjQwJKfpG36kHQVfDZc/ae37PIYQM+GyDJQ8wOQWolYWJzM+FxprDZu2ko/R
7+Qqyl0lUIdNfBQyaiPCIGjguIOAlwkHaDjBlGqjkLwJ6f1i3i4lbMQhMqGxpktC
nYwv+Bc7+d9MtpCb935oR76yqR/JRaazP+Q0NGS00OphZu8Dy6oo+NxzAVLIPPSH
vR2sABIkTCwFvZyiy8O6mG5gCzvfzFsCHXGhrmCt9xbEDQscgJJoh35zVa4A1MGa
JQEN3D03ZPNFScKgcY03z4CSyrRxbENp+0zlgmwDse6OLpL/GCjRu6iDuscYQ3A9
KAKFpMRKfxzT9lmdb9U06/nqzE0TrKVlk/0xCuTBAELA6lGFT3+o2ZyVCNmS41Rd
MwZUzcifYMQxxDKoTzHDX8ZqviZSMkDVz+aVCO31PlbYkye97gHPlaGZ9XRvv4Z5
W+MxLqnHFN695rimam9y7uRq9wZqmSstQde3YvwdxG0CAwEAAaMvMC0wDAYDVR0T
BAUwAwEB/zAdBgNVHQ4EFgQU1vCtWXyHnsGgeIoU8K9Ge4hR9lkwDQYJKoZIhvcN
AQELBQADggIBAByvipR+vPP9i55KDgWPoq5BYrWpqcskOyqqwPQVSOZQG+A/QZtP
qmI/2CvJHjpGOd87jCUdEy0hnHomN7DV1flzToU2n8WWJdGmH417CEmbcf0cNAY/
dha911Yg2MAKIzgB9Sh1NqOFR4gKiUQLGC+XFSM3J+YuJk4dusb6sbjRXX7Ijqop
nwvqMjrGXY8u0Wghjb5/M44SmR/Ca2IgWaTZg14nrwiM+d8SlZr3BVCzRC8ps/3g
GqIQhhA3ESdezp0rQ/kRCg5aRgawUj+GGSoO/Y10GT8R4aj1QVOLVK+uqMXUcbzf
wAJssjSZ44avm8JOid5pcQshj7iWZlVJoci0N8559cG8zJ4T4y4KDkf3jFnhhnv+
hQ1EJsD9eVXAuBBEqA27rPDJ5TfQUW6YAUlyf/WVYf8csJAoATgrZjpLj3lPsUDU
npmD5KmwHYpRhGsJDccm9IQ/y6ObyZijcVQPXaoiVZ+9yIA7za2SesPtAwhQjEkc
SuDGh/vlvR5WSN6mhi1lhP8VGnaNyfyD8hADwHofqn5dqx2K8jro/+OhjYCQjE5J
HEqI3OSIHbc81C7AUD16IOWBgkU1V2cwDc0JSmy1XXd0S/kJdBhHv8OwOuAkjSJ6
2mA7AWnryoOIwEo8Yjyag44PYuI05mGnqle01Isb06IGChLIc1rFtvih
-----END CERTIFICATE-----"""

# ── OpenCRQ product signing key ──────────────────────────────────────────────
# Source: opencrq/opencrq-back/src/features/xtm-one/xtm-one-ee.ts
#
# filigran-licenses generates ONE CA PER PRODUCT, each product row owning its
# own keypair. It signs every license with the product keypair AND embeds the
# same public key in the issued certificate, so the product CA certificate is
# only a self-signed wrapper around this exact key. Pinning the public key is
# therefore the same trust anchor as embedding the CA certificate — used here
# because the OpenCRQ product CA certificate itself is not distributed (the
# key below was extracted from a genuine OpenCRQ product license, mirroring
# what OpenCRQ pins for its own local validation).
OPENCRQ_PRODUCT_PUBLIC_KEY_PEM = """\
-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAvrMWI0MXelDM1Q0bKOSJ
VW+AfB/VAKVlwu1rgYea9SZSqPwJ0fiiWVUdFjc+ICIyg+XzIdeVhRcYTv5ZGwyH
dlWUfMxC/OkV2SjoN8AC/XVxu3cQulR9pKE+ZNsJjh0cohfwExjquxP28rxWl8kr
ylT+JzCO//z9um/e1CM0Xe0vYlSmpgcK1emIzyeNJT7+HbJLaORktTxEt4Is9KvW
DL2PLH/85HDAFqdFnDc3cmkn95cFOX2fl2QxJxhVprt1V/As83FG/TsxFepejl1S
W7ZxrECba8ePRo5zIplZHyHwl0BVBBlGMlrbgaXAoGZLgw+PTHyybXBVqMkqQF10
iLoLDF0yMfdrDSNBqdSBgPar6VG8rz10mxi+u/RPCYc2TxqWueVL/xVCAY4tyZwC
pf+FrbpE6MkICCG9bxF/MdbS0w94JNIJqohvz5xHvOxiz2UGjGTK4vAzfIoL6GiA
xtJ6ZiSZSpAt0Mpf5Q/5iNG/G6+TEMxJ7+0+I8RWJ5Z1isMgleQInRzs0IkZ7Xwk
K5ffpabf+g/KloS+pyEa+2xAJgAYKA/bbBiQxLu2zwYt0DH/ZYVda//M/Ue/9o5m
an9nFIQwtRvJR8xr1X3kDgkraKdh1ZIYvB4lrIPG+WthVgHZrr1fN9RX4Z7tz76P
vrOkSxSKS2UyJWpfmorTMDsCAwEAAQ==
-----END PUBLIC KEY-----"""

# Mapping: platform_identifier → (trust anchor PEM, expected product OID value)
#
# The trust anchor is either the product CA certificate or the bare product
# signing public key (see ``_load_trust_anchor_public_key`` in
# ``licensing.py`` — both anchor the same keypair). Without an entry here, a
# platform's PEM cannot be validated and EE is only granted via the XTM
# license sub-licensing path or the self-reported ``license_type``.
PRODUCT_CA_CERTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "opencti": (OPENCTI_CA_PEM, ("opencti",)),
    "openaev": (OPENAEV_CA_PEM, ("openaev",)),
    # Legacy product name from when OpenAEV was called OpenBAS
    "openbas": (OPENAEV_CA_PEM, ("openbas",)),
    # OpenCRQ accepts its own product OID and the ``opengrc`` one it was
    # issued under before the rename. The signing keypair never changed, so
    # both are the same trust anchor; a platform that has shipped the new
    # identifier while its customer still holds a pre-rename licence would
    # otherwise drop to CE until that licence was reissued.
    "opencrq": (OPENCRQ_PRODUCT_PUBLIC_KEY_PEM, ("opencrq", "opengrc")),
    # A platform that has not yet shipped the rename still registers as
    # ``opengrc``, and its licence can only carry the old OID.
    "opengrc": (OPENCRQ_PRODUCT_PUBLIC_KEY_PEM, ("opengrc",)),
}


def _decode_pem(raw: str) -> str:
    """Return a raw PEM string, base64-decoding if necessary.

    OpenAEV sends the license PEM as a base64-encoded string (via
    ``Base64.getEncoder().encodeToString(pem.getBytes())``).  OpenCTI
    sends it raw.  This helper transparently handles both cases.
    """
    if raw.lstrip().startswith("-----"):
        return raw
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        if decoded.lstrip().startswith("-----"):
            return decoded
    except Exception:
        logger.debug("Base64 decode attempt failed for license PEM", exc_info=True)
    return raw


def validate_platform_license(
    platform_identifier: str,
    license_pem: str | None,
) -> LicenseInfo:
    """Validate a registered platform's enterprise license.

    Uses the product-specific CA certificate to verify the license PEM,
    exactly mirroring how each product validates its own license.

    Returns a ``LicenseInfo`` with ``license_validated=False`` when the
    platform identifier is unknown, the PEM is missing, or validation fails.
    """
    if not license_pem or platform_identifier not in PRODUCT_CA_CERTS:
        return LicenseInfo()
    if len(license_pem) > _MAX_REGISTRATION_LICENSE_CHARS:
        logger.debug(
            "Refusing a %d-character license from a %s platform before decoding it",
            len(license_pem),
            platform_identifier,
        )
        return LicenseInfo()

    ca_pem, expected_products = PRODUCT_CA_CERTS[platform_identifier]
    pem = _decode_pem(license_pem)

    # An identifier may accept more than one product string when the product
    # was renamed: the platform ships its new identifier as soon as it
    # upgrades, but the licence in its hands keeps the product OID it was
    # issued with until the customer is reissued one. Trying each accepted
    # value keeps that window from silently dropping the platform to CE.
    # The trust anchor is the same throughout — only the OID differs — so
    # this widens nothing: an unsigned or wrongly-signed PEM still fails
    # every attempt.
    #
    # The first accepted value is the canonical one. A match on a later
    # (legacy) value is logged so the platforms still holding a pre-rename
    # licence can be counted before the legacy entry is removed, and a total
    # failure reports the canonical attempt: the legacy product name is not
    # one an operator debugging a bad PEM should be shown.
    canonical_product = expected_products[0]
    canonical: LicenseInfo | None = None
    for expected_product in expected_products:
        info = decode_license_pem(
            pem=pem,
            ca_pem=ca_pem,
            expected_product=expected_product,
        )
        if canonical is None:
            canonical = info
        if info.license_validated:
            if expected_product != canonical_product:
                logger.info(
                    "Platform %s validated with legacy product OID %r - its "
                    "licence should be reissued under %r",
                    platform_identifier,
                    expected_product,
                    canonical_product,
                )
            return info
    return canonical or LicenseInfo()

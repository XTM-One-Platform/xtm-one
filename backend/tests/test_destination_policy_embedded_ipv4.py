"""IPv6 addresses that carry an IPv4 address are judged as that address.

``is_global`` calls the whole NAT64 well-known prefix global since Python
3.13, so ``64:ff9b::a9fe:a9fe`` - the metadata endpoint through a NAT64
gateway - passed a check built on it; and on an IPv6-only network with DNS64,
refusing the prefix outright would refuse every IPv4-only public site.
"""

import ipaddress

import pytest

from app.core.destination_policy import forbidden_address_reason, is_metadata_address


@pytest.mark.parametrize(
    ("address", "public_only", "operator_network"),
    [
        ("64:ff9b::808:808", None, None),  # 8.8.8.8
        ("64:ff9b::a9fe:a9fe", "cloud metadata endpoint", "cloud metadata endpoint"),
        ("64:ff9b::a00:1", "private address", None),  # 10.0.0.1
        ("64:ff9b::7f00:1", "loopback address", "loopback address"),
        ("::ffff:169.254.169.254", "cloud metadata endpoint", "cloud metadata endpoint"),
        ("::ffff:8.8.8.8", None, None),
    ],
)
def test_an_embedded_ipv4_address_is_judged_as_itself(address, public_only, operator_network):
    ip = ipaddress.ip_address(address)
    assert forbidden_address_reason(ip) == public_only
    assert forbidden_address_reason(ip, allow_private=True) == operator_network


#: Teredo addresses (``2001::/32``) through the server 65.54.227.120; the last
#: 32 bits are the client's IPv4 address, inverted.
_TEREDO_METADATA = "2001:0:4136:e378:8000:63bf:5601:5601"  # 169.254.169.254
_TEREDO_LOOPBACK = "2001:0:4136:e378:8000:63bf:80ff:fffe"  # 127.0.0.1
_TEREDO_PRIVATE = "2001:0:4136:e378:8000:63bf:f5ff:fffe"  # 10.0.0.1
_TEREDO_PUBLIC = "2001:0:4136:e378:8000:63bf:f7f7:f7f7"  # 8.8.8.8


@pytest.mark.parametrize(
    ("address", "public_only", "operator_network", "metadata"),
    [
        # 6to4 (``2002::/16``): the next 32 bits are the IPv4 address.
        ("2002:a9fe:a9fe::1", "cloud metadata endpoint", "cloud metadata endpoint", True),
        ("2002:7f00:1::1", "loopback address", "loopback address", False),
        ("2002:a9fe:a01::", "link-local address", "link-local address", False),
        # The 6to4 and Teredo prefixes stay refused by the public-only policy.
        ("2002:a00:1::1", "private address", None, False),
        ("2002:808:808::1", "private address", None, False),
        (_TEREDO_METADATA, "cloud metadata endpoint", "cloud metadata endpoint", True),
        (_TEREDO_LOOPBACK, "loopback address", "loopback address", False),
        (_TEREDO_PRIVATE, "private address", None, False),
        (_TEREDO_PUBLIC, "private address", None, False),
    ],
)
def test_a_tunnelled_ipv4_address_is_judged_as_well(address, public_only, operator_network, metadata):
    """6to4 and Teredo carry an IPv4 destination; the operator-network policy
    used to admit them whatever they carried, as private IPv6 prefixes."""
    ip = ipaddress.ip_address(address)
    assert forbidden_address_reason(ip) == public_only
    assert forbidden_address_reason(ip, allow_private=True) == operator_network
    assert is_metadata_address(ip) is metadata


@pytest.mark.parametrize("address", ["2002:7f00:1::1", _TEREDO_LOOPBACK])
def test_an_origin_trusted_for_loopback_admits_a_tunnelled_loopback_too(address):
    """``allow_loopback`` reaches the carried address: the operator named that origin."""
    ip = ipaddress.ip_address(address)
    assert forbidden_address_reason(ip, allow_private=True, allow_loopback=True) is None

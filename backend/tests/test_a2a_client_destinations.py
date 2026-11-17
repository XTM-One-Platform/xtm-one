"""The destination guard: an SSRF probe matrix.

FR-035 / SC-006. This guard exists because a card location is
operator-supplied and fetched by the backend with stored credentials, and no
deployment-wide egress policy applies on this code path. Nothing else in the
repository refuses these destinations, so if this file is wrong nothing
catches it.
"""

from __future__ import annotations

import pytest

from app.a2a.client import destinations as destinations_module
from app.a2a.client.destinations import (
    DestinationRefusedError,
    assert_allowed,
    assert_allowed_async,
    assert_same_origin,
    origin_of,
)
from app.core import config


@pytest.fixture
def trusted_origins(monkeypatch):
    """Set ``a2a_client_dev_trusted_origins`` for one test.

    Patches the settings object ``security_option()`` reads when nothing is
    stored in Settings > Access and security, and undoes it afterwards — the guard is
    process-global, so a leaked value would quietly widen every later test in
    the session.
    """

    def _set(*origins: str) -> None:
        monkeypatch.setattr(config.settings, "a2a_client_dev_trusted_origins", list(origins))

    return _set


class TestRefusedDestinations:
    @pytest.mark.parametrize(
        ("url", "why"),
        [
            ("https://169.254.169.254/card.json", "cloud metadata, IPv4"),
            ("https://[fd00:ec2::254]/card.json", "cloud metadata, IPv6"),
            ("https://127.0.0.1/card.json", "loopback"),
            ("https://[::1]/card.json", "loopback, IPv6"),
            ("https://10.1.2.3/card.json", "RFC1918 10/8"),
            ("https://192.168.1.1/card.json", "RFC1918 192.168/16"),
            ("https://172.16.0.1/card.json", "RFC1918 172.16/12"),
            ("https://169.254.1.1/card.json", "link-local"),
            ("https://0.0.0.0/card.json", "unspecified"),
            ("https://100.64.0.7/card.json", "RFC 6598 shared address space"),
            ("https://100.127.255.254/card.json", "RFC 6598, top of the range"),
        ],
    )
    def test_literal_address_is_refused(self, url, why):
        with pytest.raises(DestinationRefusedError):
            assert_allowed(url, resolve=False)

    def test_shared_address_space_is_refused(self):
        # 100.64.0.0/10 is the one range for which ``is_private`` is False
        # while the address is still not globally routable, so every named
        # check in the guard passes it and only the ``is_global`` backstop
        # catches it. It is the space between a subscriber and their ISP: a
        # deployment behind carrier-grade NAT could otherwise be pointed at
        # its provider's own infrastructure, which is the metadata-endpoint
        # attack with a different address.
        with pytest.raises(DestinationRefusedError) as exc:
            assert_allowed("https://100.64.0.7/card.json", resolve=False)
        assert exc.value.reason == "destination_not_permitted"
        assert exc.value.host == "100.64.0.7"

    def test_plain_http_is_refused_not_upgraded(self):
        # FR-012: refused rather than downgraded to. Silently upgrading would
        # send a partner credential over a connection nobody can vouch for.
        with pytest.raises(DestinationRefusedError, match="https"):
            assert_allowed("http://partner.example/card.json")

    @pytest.mark.parametrize("url", ["ftp://x.example/c", "partner.example/c", ""])
    def test_missing_or_wrong_scheme_is_refused(self, url):
        with pytest.raises(DestinationRefusedError):
            assert_allowed(url)

    def test_literal_address_is_checked_even_when_resolution_is_skipped(self):
        # ``resolve=False`` must not be a way to skip the guard: the literal
        # case is the one an attacker controls most directly.
        with pytest.raises(DestinationRefusedError):
            assert_allowed("https://169.254.169.254/c", resolve=False)


class TestAllowedDestinations:
    def test_public_hostname_is_allowed(self):
        assert assert_allowed("https://partner.example/card.json", resolve=False) == (
            "https://partner.example"
        )

    @pytest.mark.parametrize(
        ("host", "why"),
        [
            ("100.63.255.255", "one below 100.64.0.0/10"),
            ("100.128.0.0", "one above 100.64.0.0/10"),
            ("[2606:4700:4700::1111]", "ordinary global IPv6"),
        ],
    )
    def test_the_backstop_does_not_over_refuse(self, host, why):
        # The ``is_global`` backstop is the one check here broad enough to
        # refuse a real partner by accident, so its edges are pinned. The two
        # IPv4 hosts bracket shared address space exactly; the IPv6 one is
        # there because ``is_global`` is derived differently per family and a
        # backstop that quietly refused every IPv6 partner would look, from
        # the outside, exactly like a partner being unreachable.
        assert assert_allowed(f"https://{host}/c", resolve=False)

    def test_public_literal_address_is_allowed(self):
        assert assert_allowed("https://93.184.216.34/c", resolve=False) == (
            "https://93.184.216.34"
        )


class TestOriginPinning:
    def test_port_is_part_of_the_origin(self):
        # Two services, not one: a partner that moves between them has moved.
        assert origin_of("https://p.example:8443/c") == "https://p.example:8443"
        assert origin_of("https://p.example/c") == "https://p.example"

    def test_path_and_query_are_not_part_of_the_origin(self):
        assert origin_of("https://p.example/a/b?x=1#f") == "https://p.example"

    def test_same_origin_passes(self):
        assert_same_origin("https://p.example/rpc", "https://p.example")

    @pytest.mark.parametrize(
        "elsewhere",
        [
            "https://evil.example/rpc",  # different host
            "https://p.example:8443/rpc",  # different port
            "https://p.example.evil.example/rpc",  # suffix trick
        ],
    )
    def test_off_origin_is_refused(self, elsewhere):
        with pytest.raises(DestinationRefusedError):
            assert_same_origin(elsewhere, "https://p.example")


class TestTheDevTrustedOriginAllowList:
    """``a2a_client_dev_trusted_origins``: the one way past this guard.

    It exists so the outbound half can be exercised against a second local
    deployment, which is otherwise untestable — every address a dev box offers
    is loopback or private and every URL is http. That makes this the highest
    -consequence setting in the feature, so what it does NOT widen matters
    more here than what it does.
    """

    def test_by_default_it_widens_nothing(self):
        # The shipped state. Every other test in this file runs under it, so
        # this asserts the default itself rather than a consequence of it.
        assert config.Settings().a2a_client_dev_trusted_origins == []

    def test_a_listed_origin_is_reachable_over_plain_http(self, trusted_origins):
        trusted_origins("http://localhost:8200")
        assert assert_allowed("http://localhost:8200/a2a/t/card.json") == (
            "http://localhost:8200"
        )

    @pytest.mark.parametrize(
        ("url", "why"),
        [
            ("http://localhost:8201/c", "different port"),
            ("https://localhost:8200/c", "different scheme"),
            ("http://127.0.0.1:8200/c", "different host, same service"),
            ("http://evil.example/c", "not listed at all"),
        ],
    )
    def test_listing_one_origin_widens_only_that_origin(
        self, trusted_origins, url, why
    ):
        # The reason this is an allow-list and not a boolean. `127.0.0.1` is
        # in here on purpose: it reaches the same server as `localhost` on a
        # normal machine, and is still refused, because the setting names an
        # ORIGIN rather than a destination reachable by any spelling.
        trusted_origins("http://localhost:8200")
        with pytest.raises(DestinationRefusedError):
            assert_allowed(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/c",
            "https://169.254.169.254/c",
            "http://[fd00:ec2::254]/c",
            "https://[fd00:ec2::254]/c",
        ],
    )
    def test_cloud_metadata_is_exempt_from_the_exemption(self, trusted_origins, url):
        # The one destination no operator can opt into. Everything else this
        # guard refuses is a judgement about reachability that a local test
        # may legitimately overrule; this is where cloud credentials live, so
        # an environment variable that could reach it would turn a development
        # convenience into the request-forgery the guard exists to stop.
        #
        # Both schemes, because they are refused by different checks once the
        # allow-list declines them — http never gets past the transport check,
        # https reaches the address one — and only "refused, every spelling,
        # however listed" is the property worth having.
        trusted_origins(origin_of(url))
        with pytest.raises(DestinationRefusedError):
            assert_allowed(url)

    def test_listed_metadata_still_reports_the_address_as_the_reason(
        self, trusted_origins
    ):
        # On the https spelling the allow-list declines and the address check
        # is what refuses it, so an operator reads "that destination is not
        # one this platform will send to" rather than a transport complaint
        # about a URL whose transport was fine.
        trusted_origins("https://169.254.169.254")
        with pytest.raises(DestinationRefusedError) as exc:
            assert_allowed("https://169.254.169.254/c")
        assert exc.value.reason == "destination_not_permitted"

    def test_a_trusted_origin_is_still_pinned(self, trusted_origins):
        # Being trusted is a way past the transport and address checks, and
        # nothing else. A partner that moves is refused whether or not the
        # origin it came from was listed.
        trusted_origins("http://localhost:8200")
        assert_same_origin("http://localhost:8200/rpc", "http://localhost:8200")
        with pytest.raises(DestinationRefusedError):
            assert_same_origin("http://localhost:8201/rpc", "http://localhost:8200")


class TestTheAllowListIsParsedSafely:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("http://localhost:8200", ["http://localhost:8200"]),
            # Normalised to exactly what origin_of() emits, so matching is a
            # set lookup rather than a comparison of two spellings.
            ("HTTP://LocalHost:8200/some/path", ["http://localhost:8200"]),
            (
                " http://localhost:8200 , http://localhost:8100 ",
                ["http://localhost:8200", "http://localhost:8100"],
            ),
            ("http://a.test,http://a.test", ["http://a.test"]),
            ("", []),
            ("http://localhost:8200,,", ["http://localhost:8200"]),
        ],
    )
    def test_entries_are_normalised_and_deduplicated(self, raw, expected):
        assert (
            config.Settings(a2a_client_dev_trusted_origins=raw).a2a_client_dev_trusted_origins
            == expected
        )

    @pytest.mark.parametrize("raw", ["localhost:8200", "not a url", "/just/a/path"])
    def test_a_malformed_entry_refuses_to_start(self, raw):
        # Loudly, rather than being skipped. This setting widens a security
        # boundary: an operator who fat-fingers an entry must not be told
        # everything is fine while the origin they meant to trust is silently
        # absent and delegation fails for a reason that looks unrelated.
        with pytest.raises(ValueError, match="A2A_CLIENT_DEV_TRUSTED_ORIGINS"):
            config.Settings(a2a_client_dev_trusted_origins=raw)

    def test_a_configured_origin_says_so_at_boot(self):
        """The footgun reports itself in the logs, not only in this file.

        A stray dev entry in a production config downgrades credential
        transport to plaintext and reaches addresses this guard otherwise
        refuses, and nothing in a running deployment would otherwise show
        it. So the boot log names every configured origin, at WARNING,
        unconditionally — asserted here because a log line nobody checks is
        a log line that can be deleted by accident.
        """
        import ast
        import inspect
        import pathlib as _pathlib

        main = _pathlib.Path(inspect.getfile(config)).parents[1] / "main.py"
        tree = ast.parse(main.read_text(encoding="utf-8"))
        guarded = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and "a2a_client_dev_trusted_origins" in ast.unparse(node.test)
        ]
        assert guarded, "nothing in main.py reacts to the setting being on"
        body = ast.unparse(guarded[0].body)
        assert "logger.warning" in body, "INFO is not loud enough for this one"
        # The origins themselves, not merely a count: an operator has to be
        # able to see WHICH entry they left behind.
        assert "join(settings.a2a_client_dev_trusted_origins)" in body


class TestTheAsyncGuard:
    """The same check, for callers on the event loop: every caller that
    resolves is a coroutine, and a blocking lookup would stall the worker."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "https://169.254.169.254/c",
            "https://10.0.0.1/c",
            "https://127.0.0.1/c",
            "http://partner.example/c",
            "ftp://partner.example/c",
        ],
    )
    async def test_it_refuses_exactly_what_the_sync_guard_refuses(self, url):
        with pytest.raises(DestinationRefusedError) as sync_exc:
            assert_allowed(url, resolve=False)
        with pytest.raises(DestinationRefusedError) as async_exc:
            await assert_allowed_async(url, resolve=False)
        assert async_exc.value.reason == sync_exc.value.reason

    @pytest.mark.asyncio
    async def test_a_name_that_resolves_inward_is_refused(self, monkeypatch):
        async def _mixed(_host, _port):
            # One public, one private: EVERY address must be acceptable.
            return [
                (None, None, None, "", ("93.184.216.34", 443)),
                (None, None, None, "", ("10.0.0.5", 443)),
            ]

        monkeypatch.setattr(destinations_module, "_resolve", _mixed)
        with pytest.raises(DestinationRefusedError) as exc:
            await assert_allowed_async("https://partner.example/c")
        assert exc.value.reason == "destination_not_permitted"

    @pytest.mark.asyncio
    async def test_a_public_name_passes_and_pins_the_origin(self, monkeypatch):
        async def _public(_host, _port):
            return [(None, None, None, "", ("93.184.216.34", 443))]

        monkeypatch.setattr(destinations_module, "_resolve", _public)
        assert await assert_allowed_async("https://partner.example:8443/c.json") == (
            "https://partner.example:8443"
        )

    @pytest.mark.asyncio
    async def test_an_unresolvable_name_is_a_partner_that_is_not_there(self, monkeypatch):
        import socket

        async def _nowhere(_host, _port):
            raise socket.gaierror(8, "nodename nor servname provided")

        monkeypatch.setattr(destinations_module, "_resolve", _nowhere)
        with pytest.raises(DestinationRefusedError) as exc:
            await assert_allowed_async("https://nx.invalid/c")
        assert exc.value.reason == "card_unfetchable"

    def test_resolution_goes_through_the_loop_not_the_thread(self):
        import inspect

        assert "get_running_loop().getaddrinfo" in inspect.getsource(
            destinations_module._resolve
        )
        assert "socket.getaddrinfo" not in inspect.getsource(assert_allowed_async)

    def test_every_coroutine_that_resolves_uses_the_async_entry_point(self):
        import inspect

        from app.a2a.client import credentials, delegation, registration, verification

        for module in (credentials, delegation, registration, verification):
            assert "assert_allowed(" not in inspect.getsource(module), module.__name__

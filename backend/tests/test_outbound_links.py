"""Platform links an external surface can click (#10115).

A bare ``<host>/presentations/<id>`` gets the platform's scheme on every
outbound channel; WhatsApp gets a Markdown link as its full URL. Teams and
mail render the Markdown link as an anchor.
"""

from __future__ import annotations

from unittest.mock import patch

from app.services.outbound_links import (
    bare_platform_links_as_urls,
    markdown_links_as_plain_urls,
    platform_origins,
)
from app.services.outbound_markers import _outbound_arguments

ORIGINS = platform_origins("https://copilot.example.ai", "https://api.copilot.example.ai/")
URL = "https://copilot.example.ai/presentations/abc"


# ── Bare platform host ────────────────────────────────────────────────────────


def test_origins_are_distinct_http_hosts_longest_first():
    assert ORIGINS == (("https", "api.copilot.example.ai"), ("https", "copilot.example.ai"))
    assert platform_origins("", "not a url", "ftp://x.y") == ()


def test_a_bare_host_path_gets_the_scheme_and_becomes_a_link():
    out = bare_platform_links_as_urls("Deck: copilot.example.ai/presentations/abc.", ORIGINS)
    assert out == f"Deck: [{URL}]({URL})."


def test_the_configured_scheme_is_used():
    origins = platform_origins("http://localhost:3000")
    assert bare_platform_links_as_urls("see localhost:3000/reports/x", origins) == (
        "see [http://localhost:3000/reports/x](http://localhost:3000/reports/x)"
    )


def test_well_formed_links_and_urls_with_a_scheme_are_untouched():
    for text in (
        f"[the deck]({URL})",
        f"open {URL} now",
        f"<{URL}>",
        "[copilot.example.ai/presentations/abc](https://copilot.example.ai/presentations/abc)",
    ):
        assert bare_platform_links_as_urls(text, ORIGINS) == text


def test_code_spans_and_fenced_blocks_are_untouched():
    text = "run `curl copilot.example.ai/api` then\n```\ncopilot.example.ai/presentations/abc\n```\n"
    assert bare_platform_links_as_urls(text, ORIGINS) == text


def test_another_host_an_address_and_a_subdomain_suffix_are_untouched():
    for text in (
        "mail admin@copilot.example.ai today",
        "copilot.example.ai.evil.net/x",
        "notcopilot.example.ai/x",
        "unrelated text",
    ):
        assert bare_platform_links_as_urls(text, ORIGINS) == text


def test_the_api_host_is_matched_whole_not_as_its_parent():
    out = bare_platform_links_as_urls("api.copilot.example.ai/files/1", ORIGINS)
    assert out == "[https://api.copilot.example.ai/files/1](https://api.copilot.example.ai/files/1)"


def test_an_html_body_gets_an_anchor_and_attributes_are_untouched():
    text = '<p>Deck copilot.example.ai/presentations/abc and <a href="https://copilot.example.ai/x">x</a></p>'
    out = bare_platform_links_as_urls(text, ORIGINS)
    assert f'<a href="{URL}">{URL}</a>' in out
    assert '<a href="https://copilot.example.ai/x">x</a>' in out


def test_settings_give_the_default_origins():
    with patch("app.core.config.settings") as cfg:
        cfg.frontend_url = "https://copilot.example.ai"
        cfg.base_url = "https://copilot.example.ai"
        assert bare_platform_links_as_urls("copilot.example.ai/presentations/abc") == f"[{URL}]({URL})"


# ── WhatsApp ──────────────────────────────────────────────────────────────────


def test_whatsapp_gets_the_full_url_of_a_markdown_link():
    assert markdown_links_as_plain_urls(f"See [Q3 deck]({URL}).") == f"See Q3 deck: {URL}."
    assert markdown_links_as_plain_urls(f"[{URL}]({URL})") == URL


def test_whatsapp_keeps_code_images_and_plain_text():
    for text in (f"`[a]({URL})`", f"```\n[a]({URL})\n```", f"![chart]({URL})", "no link", "[a](relative/path)"):
        assert markdown_links_as_plain_urls(text) == text


def test_a_bare_link_reaches_whatsapp_as_a_full_url():
    linked = bare_platform_links_as_urls("copilot.example.ai/presentations/abc", ORIGINS)
    assert markdown_links_as_plain_urls(linked) == URL


# ── Send tools: the bare host is linked without a session ─────────────────────


async def test_a_send_argument_with_a_bare_host_is_linked_without_opening_a_session():
    with (
        patch("app.core.config.settings") as cfg,
        patch("app.core.database.async_session", side_effect=AssertionError("no session")),
    ):
        cfg.frontend_url = "https://copilot.example.ai"
        cfg.base_url = "https://copilot.example.ai"
        out = await _outbound_arguments(
            {"to": "copilot.example.ai/x", "message": "copilot.example.ai/presentations/abc"},
            ("message",),
            {},
        )
    assert out == {"to": "copilot.example.ai/x", "message": f"[{URL}]({URL})"}

"""The model catalog announcement emails: what a change says, and how it looks.

The database halves (recording at boot, delivering from the scheduler) are in
``test_model_catalog_announcements_postgres.py``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.services import email as email_module
from app.services import model_catalog_announcements as mca
from app.services.branding import Branding
from app.services.model_catalog_announcements import (
    CatalogModel,
    ModelCatalog,
    diff_catalogs,
    model_display_name,
    render_announcement,
)

SONNET_5 = CatalogModel("claude-sonnet-5", "anthropic")
SONNET_55 = CatalogModel("claude-sonnet-5-5", "anthropic")
HAIKU = CatalogModel("claude-haiku-4-5", "anthropic")
OPUS_55 = CatalogModel("claude-opus-5-5", "anthropic")

# The SaaS catalog today, and after the Sonnet 5.5 rollout.
BEFORE = ModelCatalog((HAIKU, SONNET_5), "claude-sonnet-5", "anthropic")
AFTER = ModelCatalog((HAIKU, SONNET_55), "claude-sonnet-5-5", "anthropic")


@pytest.fixture(autouse=True)
def _platform(monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", "https://acme.one.filigran.cloud")
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(email_module, "get_cached_branding", lambda: Branding(name="XTM One"))


# --- What changed --------------------------------------------------------------


def test_a_new_default_is_announced_with_what_it_replaces_and_what_left():
    change = diff_catalogs(BEFORE, AFTER)
    assert change.new_default == SONNET_55
    assert change.previous_default == "claude-sonnet-5"
    # The new default has its own card, it is not also "newly available".
    assert change.added == ()
    assert change.removed == (SONNET_5,)
    assert change.default_newly_offered
    assert change.new_model_count == 1
    assert change.announces
    assert change.describe() == "default claude-sonnet-5 -> claude-sonnet-5-5; retired: claude-sonnet-5"


def test_models_added_without_a_default_change_are_announced():
    change = diff_catalogs(BEFORE, ModelCatalog((HAIKU, OPUS_55, SONNET_5), "claude-sonnet-5", "anthropic"))
    assert change.new_default is None
    assert change.added == (OPUS_55,)
    assert change.new_model_count == 1
    assert change.announces


def test_a_removal_alone_is_announced():
    change = diff_catalogs(BEFORE, ModelCatalog((SONNET_5,), "claude-sonnet-5", "anthropic"))
    assert change.removed == (HAIKU,)
    assert change.new_model_count == 0
    assert change.announces


def test_the_default_moving_to_a_model_already_offered_is_announced():
    change = diff_catalogs(BEFORE, ModelCatalog((HAIKU, SONNET_5), "claude-haiku-4-5", "anthropic"))
    assert change.new_default == HAIKU
    assert change.added == ()
    assert change.removed == ()
    # Not a new model: users could already pick it.
    assert not change.default_newly_offered
    assert change.new_model_count == 0


def test_an_unpinned_default_announces_nothing():
    change = diff_catalogs(BEFORE, ModelCatalog((HAIKU, SONNET_5), None, None))
    assert not change.announces


def test_catalog_json_round_trips_and_ignores_malformed_entries():
    catalog = ModelCatalog.from_json(
        [{"id": "claude-sonnet-5-5", "provider": "anthropic"}, {"provider": "x"}, "junk", None],
        "claude-sonnet-5-5",
        "anthropic",
    )
    assert catalog.models == (SONNET_55,)
    assert ModelCatalog.from_json(catalog.models_json(), "claude-sonnet-5-5", "anthropic") == catalog


@pytest.mark.parametrize(
    ("model_id", "label"),
    [
        ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
        ("claude-sonnet-5", "Claude Sonnet 5"),
        ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
        ("claude-opus-4-1", "Claude Opus 4.1"),
        ("global.anthropic.claude-sonnet-5-5", "Claude Sonnet 5.5"),
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "Claude Sonnet 4.5"),
        ("claude-3-5-haiku-20241022", "Claude 3.5 Haiku"),
        ("gpt-4o-mini", "GPT-4o mini"),
        ("gpt-5.1", "GPT-5.1"),
        ("gemini-2.5-pro", "Gemini 2.5 Pro"),
        ("my-custom-model", "my-custom-model"),
    ],
)
def test_model_display_names(model_id, label):
    assert model_display_name(model_id) == label


# --- The email -----------------------------------------------------------------


def _images(html: str) -> list[str]:
    return re.findall(r'<img[^>]+src="([^"]+)"', html)


def test_the_default_model_email_says_what_changed_for_the_reader():
    email = render_announcement(diff_catalogs(BEFORE, AFTER), recipient_name="Josh", platform_name="XTM One")
    assert email.subject == "Claude Sonnet 5.5 is now your default model on XTM One"
    for text in (email.html, email.text):
        assert "Hello Josh," in text
        assert "Claude Sonnet 5.5" in text
        assert "claude-sonnet-5-5" in text
        assert "Replaces Claude Sonnet 5" in text
        assert "Retired" in text
        assert "claude-sonnet-5<" in text or "claude-sonnet-5)" in text
        assert "Agents and personal preferences that used it now follow the default model." in text
        assert "https://acme.one.filigran.cloud" in text
    assert "Open XTM One" in email.html


def test_the_new_models_email_names_one_or_several_models():
    one = render_announcement(
        diff_catalogs(BEFORE, ModelCatalog((HAIKU, OPUS_55, SONNET_5), "claude-sonnet-5", "anthropic")),
        recipient_name=None,
        platform_name="XTM One",
    )
    assert one.subject == "Claude Opus 5.5 is now available on XTM One"
    assert "Hello," in one.text
    several = render_announcement(
        diff_catalogs(
            BEFORE,
            ModelCatalog((HAIKU, OPUS_55, SONNET_5, SONNET_55), "claude-sonnet-5", "anthropic"),
        ),
        recipient_name=None,
        platform_name="XTM One",
    )
    assert several.subject == "New models available in your XTM One platform"
    assert "Claude Opus 5.5 and Claude Sonnet 5.5 are now available" in several.text
    assert several.text.count("\nNew: ") == 2


FABLE_51 = CatalogModel("claude-fable-5-1", "anthropic")


def test_several_new_models_and_a_retired_one_are_all_listed():
    change = diff_catalogs(
        ModelCatalog((HAIKU, SONNET_5, SONNET_55), "claude-sonnet-5-5", "anthropic"),
        ModelCatalog((FABLE_51, HAIKU, OPUS_55, SONNET_55), "claude-sonnet-5-5", "anthropic"),
    )
    email = render_announcement(change, recipient_name=None, platform_name="XTM One")
    assert email.subject == "New models available in your XTM One platform"
    assert "New: Claude Opus 5.5 (Anthropic - claude-opus-5-5)" in email.text
    assert "New: Claude Fable 5.1 (Anthropic - claude-fable-5-1)" in email.text
    assert "Retired: Claude Sonnet 5 (Anthropic - claude-sonnet-5)" in email.text
    # The retired models come after the new ones, in a muted panel of their own.
    assert email.text.index("Retired:") > email.text.index("New: Claude Fable 5.1")
    assert email.html.index(">Retired<") > email.html.index(">Claude Fable 5.1<")


def test_a_new_default_among_several_new_models_leads_with_the_new_models():
    change = diff_catalogs(BEFORE, ModelCatalog((FABLE_51, HAIKU, OPUS_55, SONNET_5), "claude-opus-5-5", "anthropic"))
    assert change.new_model_count == 2
    email = render_announcement(change, recipient_name=None, platform_name="XTM One")
    assert email.subject == "New models available in your XTM One platform"
    assert "Claude Opus 5.5 and Claude Fable 5.1 are now available" in email.text
    assert "Claude Opus 5.5 is also the new default model." in email.text
    assert "Default model: Claude Opus 5.5" in email.text
    assert "New: Claude Fable 5.1" in email.text


def test_a_retired_model_alone_is_the_headline():
    change = diff_catalogs(BEFORE, ModelCatalog((SONNET_5,), "claude-sonnet-5", "anthropic"))
    email = render_announcement(change, recipient_name=None, platform_name="XTM One")
    assert email.subject == "Claude Haiku 4.5 is no longer available on XTM One"
    assert (
        "Claude Haiku 4.5 is no longer available on your XTM One platform. "
        "Agents and personal preferences that used it now follow the default model."
    ) in email.text
    # The lead already says it: no second sentence under the list.
    assert email.text.count("now follow the default model") == 1


def test_a_new_model_leads_over_the_retired_ones():
    change = diff_catalogs(BEFORE, ModelCatalog((OPUS_55,), "claude-sonnet-5", "anthropic"))
    email = render_announcement(change, recipient_name=None, platform_name="XTM One")
    assert email.subject == "Claude Opus 5.5 is now available on XTM One"
    assert email.text.count("Retired: ") == 2
    assert "Agents and personal preferences that used them now follow the default model." in email.text


def test_several_retired_models_alone_share_one_headline():
    change = diff_catalogs(
        ModelCatalog((HAIKU, OPUS_55, SONNET_5), "claude-sonnet-5", "anthropic"),
        ModelCatalog((SONNET_5,), "claude-sonnet-5", "anthropic"),
    )
    email = render_announcement(change, recipient_name=None, platform_name="XTM One")
    assert email.subject == "Models retired from your XTM One platform"
    assert "Claude Haiku 4.5 and Claude Opus 5.5 are no longer available" in email.text
    assert "used them now follow the default model" in email.text


def test_the_email_follows_the_filigran_guideline_and_renders_in_gmail():
    html = render_announcement(diff_catalogs(BEFORE, AFTER), recipient_name="Josh", platform_name="XTM One").html
    # Guideline palette: page, text, button.
    for color in ("#F1F5F9", "#00020C", "#001BDB"):
        assert color in html
    # Tables, not ``display: table`` divs (Gmail drops attribute selectors),
    # and raster images only (no SVG renders in Gmail).
    assert "data-layout" not in html
    assert 'role="presentation"' in html
    images = _images(html)
    assert images and all(src.startswith("https://acme.one.filigran.cloud/email-assets/") for src in images)
    assert not any(src.endswith(".svg") for src in images)
    # The Filigran footer: social links, logo, offices, and the Filigran sign-off.
    for name in ("website.png", "slack.png", "linkedin.png", "youtube.png", "x.png", "filigran.png"):
        assert any(src.endswith(name) for src in images)
    assert "New York, Paris, London, Sydney, Singapore, Tokyo" in html
    assert "The Filigran Team" in html
    # A product email, not a marketing one.
    assert "unsubscribe" not in html.lower()
    assert "preferences" not in html.lower().replace("personal preferences", "")


def test_every_email_asset_the_layout_names_ships_with_the_frontend():
    from pathlib import Path

    public = Path(__file__).resolve().parents[2] / "frontend" / "public" / "email-assets"
    html = render_announcement(diff_catalogs(BEFORE, AFTER), recipient_name=None, platform_name="XTM One").html
    for src in _images(html):
        assert (public / src.rsplit("/", 1)[1]).is_file(), src


def test_the_logos_have_no_background_and_outlook_dark_mode_swaps_them():
    from pathlib import Path

    from PIL import Image, ImageChops

    public = Path(__file__).resolve().parents[2] / "frontend" / "public" / "email-assets"
    # Transparent: a darkened card never shows a white box around the logo.
    for name in ("xtm-one.png", "xtm-one-dark.png", "filigran.png", "filigran-dark.png"):
        with Image.open(public / name) as image:
            assert image.mode == "RGBA", name
            assert image.getpixel((0, 0))[3] == 0, name
    # No background at all on the light logos, not even a soft halo.
    for name in ("xtm-one.png", "filigran.png"):
        with Image.open(public / name) as image:
            red, green, blue, alpha = image.convert("RGBA").split()
            darkest = ImageChops.darker(ImageChops.darker(red, green), blue)
            white = ImageChops.multiply(darkest.point(lambda v: 255 * (v > 200)), alpha.point(lambda v: 255 * (v > 0)))
            assert white.getbbox() is None, name
    html = render_announcement(diff_catalogs(BEFORE, AFTER), recipient_name=None, platform_name="XTM One").html
    # Header and footer: each logo is followed by its dark background variant,
    # hidden unless Outlook's dark mode swaps it in.
    pairs = re.findall(
        r'<a class="email-logo"[^>]*><img src="[^"]*/([\w-]+)\.png".*?'
        r'<div class="email-logo-dark" style="display:none;[^"]*"><a [^>]*><img src="[^"]*/([\w-]+)\.png"',
        html,
        re.S,
    )
    assert len(pairs) == 2 and ("filigran", "filigran-dark") in pairs
    assert all(dark == f"{light}-dark" for light, dark in pairs)
    # The swap is a <style> of its own: a client that drops the attribute
    # selector keeps the mobile paddings.
    swaps = [block for block in re.findall(r"<style>(.*?)</style>", html, re.S) if "data-ogsc" in block]
    assert len(swaps) == 1 and "@media" not in swaps[0]


def test_a_configured_logo_has_no_dark_variant_to_swap_in(monkeypatch):
    monkeypatch.setattr(
        email_module, "get_cached_branding", lambda: Branding(name="Acme AI", logo="https://acme.example/logo.png")
    )
    html = render_announcement(diff_catalogs(BEFORE, AFTER), recipient_name=None, platform_name="Acme AI").html
    assert '<img src="https://acme.example/logo.png"' in html
    assert 'class="email-logo-dark"' not in html


def test_model_ids_and_names_are_escaped():
    hostile = CatalogModel('claude-x-1"><script>alert(1)</script>', "anthropic")
    change = diff_catalogs(BEFORE, ModelCatalog((HAIKU, SONNET_5, hostile), "claude-sonnet-5", "anthropic"))
    html = render_announcement(change, recipient_name="<b>Josh</b>", platform_name="A & B").html
    assert "<script>" not in html
    assert "<b>Josh</b>" not in html
    assert "A &amp; B" in html


def test_a_rebranded_platform_signs_with_its_own_name_and_drops_the_filigran_footer(monkeypatch):
    monkeypatch.setattr(email_module, "get_cached_branding", lambda: Branding(name="Acme AI"))
    html = render_announcement(diff_catalogs(BEFORE, AFTER), recipient_name=None, platform_name="Acme AI").html
    assert "The Acme AI team" in html
    assert "The Filigran Team" not in html
    assert "linkedin.png" not in html
    assert "New York" not in html


def test_the_transactional_emails_share_the_layout():
    html, text = email_module.password_reset_email("https://acme.one.filigran.cloud/reset-password?token=t", 24)
    assert "Reset your password" in html and "#001BDB" in html and "data-layout" not in html
    assert "https://acme.one.filigran.cloud/reset-password?token=t" in text
    html, _ = email_module.invitation_email("https://acme.one.filigran.cloud/invite?t=1", "admin", 168)
    assert "You've been invited" in html and "as an administrator" in html and "7 days" in html


# --- Who is told, and when -------------------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "smtp", "expected"),
    [
        (True, "smtp.example.com", ("pending", None)),
        (False, "smtp.example.com", ("skipped", "disabled")),
        (True, "", ("skipped", "smtp_not_configured")),
    ],
)
def test_a_change_is_only_queued_when_the_emails_can_go_out(monkeypatch, enabled, smtp, expected):
    monkeypatch.setattr(settings, "model_catalog_emails_enabled", enabled)
    monkeypatch.setattr(settings, "smtp_hostname", smtp)
    assert mca._initial_status(diff_catalogs(BEFORE, AFTER)) == expected


@pytest.mark.parametrize("enabled", [True, False])
def test_a_change_is_never_queued_while_the_emails_are_turned_off(monkeypatch, enabled):
    monkeypatch.setattr(settings, "model_announcement_emails_disabled", True)
    monkeypatch.setattr(settings, "model_catalog_emails_enabled", enabled)
    monkeypatch.setattr(settings, "smtp_hostname", "smtp.example.com")
    assert mca._initial_status(diff_catalogs(BEFORE, AFTER)) == ("skipped", "turned_off")


@pytest.mark.parametrize(
    ("turned_off", "smtp", "detail"),
    [
        (True, "smtp.example.com", mca.TURNED_OFF_DETAIL),
        (True, "", mca.TURNED_OFF_DETAIL),
        (False, "", mca.SMTP_NOT_CONFIGURED_DETAIL),
        (False, "smtp.example.com", None),
    ],
)
def test_an_admin_announcement_is_refused_while_the_emails_are_turned_off(monkeypatch, turned_off, smtp, detail):
    monkeypatch.setattr(settings, "model_announcement_emails_disabled", turned_off)
    monkeypatch.setattr(settings, "smtp_hostname", smtp)
    if detail is None:
        assert mca.refuse_unless_emails_allowed() is None
        return
    with pytest.raises(mca.AnnouncementRefusedError) as exc:
        mca.refuse_unless_emails_allowed()
    assert str(exc.value) == detail


def test_a_change_skipped_while_turned_off_is_a_configuration_skip():
    assert "turned_off" in mca._CONFIGURATION_SKIPS
    assert mca._SKIP_EXPLANATIONS["turned_off"] == "MODEL_ANNOUNCEMENT_EMAILS_DISABLED is on"


def test_a_change_with_nothing_to_announce_is_only_recorded(monkeypatch):
    monkeypatch.setattr(settings, "model_catalog_emails_enabled", True)
    monkeypatch.setattr(settings, "smtp_hostname", "smtp.example.com")
    unpinned = diff_catalogs(BEFORE, ModelCatalog(BEFORE.models, None, None))
    assert mca._initial_status(unpinned) == ("recorded", None)


def test_backoff_grows_and_is_capped():
    assert mca._backoff(1) < mca._backoff(2) < mca._backoff(5)
    assert mca._backoff(40) == mca._BACKOFF_MAX


def test_the_settle_hold_never_shortens_a_back_off(monkeypatch):
    now = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(settings, "model_catalog_emails_settle_minutes", 30)
    assert mca._settle_hold(now) == now + timedelta(minutes=30)
    assert mca._settle_hold(now, now + timedelta(minutes=5)) == now + timedelta(minutes=30)
    assert mca._settle_hold(now, now + timedelta(hours=1)) == now + timedelta(hours=1)
    monkeypatch.setattr(settings, "model_catalog_emails_settle_minutes", 0)
    assert mca._settle_hold(now) is None
    assert mca._settle_hold(now, now + timedelta(minutes=5)) == now + timedelta(minutes=5)


@pytest.mark.parametrize(
    ("full_name", "greeting"),
    [("Josh Smith", "Josh"), ("Admin", None), ("admin@acme.io", None), ("", None), (None, None)],
)
def test_greeting_names(full_name, greeting):
    assert email_module.email_greeting_name(full_name) == greeting


def test_settings_validate_the_recipient_scope_and_batch_size():
    from pydantic import ValidationError

    from app.core.config import Settings

    assert Settings(model_catalog_emails_recipients=" Admins ").model_catalog_emails_recipients == "admins"
    with pytest.raises(ValidationError):
        Settings(model_catalog_emails_recipients="everyone")
    with pytest.raises(ValidationError):
        Settings(model_catalog_emails_batch_size=0)
    assert Settings(model_catalog_emails_settle_minutes=0).model_catalog_emails_settle_minutes == 0
    for minutes in (-1, 1441):
        with pytest.raises(ValidationError):
            Settings(model_catalog_emails_settle_minutes=minutes)


def test_the_test_email_is_the_users_email_marked_as_a_test():
    change = mca.default_model_change("claude-sonnet-5-5", "anthropic", "claude-sonnet-5")
    real = mca.render_announcement(change, recipient_name="Ann", platform_name="XTM One")
    test = mca.render_test_announcement(change, recipient_name="Ann", platform_name="XTM One")
    assert test.subject == mca.TEST_SUBJECT_PREFIX + real.subject
    assert (test.html, test.text) == (real.html, real.text)
    assert mca.default_model_change("claude-sonnet-5-5", None, "claude-sonnet-5-5").previous_default is None


def test_composed_changes_tell_the_net_news():
    gpt55, mini = CatalogModel("gpt-5.5", "openai"), CatalogModel("gpt-5.5-mini", "openai")
    earlier = mca.CatalogChange(new_default=None, previous_default=None, added=(OPUS_55, gpt55), removed=(HAIKU,))
    later = mca.CatalogChange(new_default=None, previous_default=None, added=(HAIKU, mini), removed=(gpt55,))
    composed = mca._compose_catalog_changes(earlier, later)
    # gpt-5.5 added then retired, Haiku retired then offered again: no news.
    assert composed.added == (OPUS_55, mini)
    assert composed.removed == ()
    assert composed.new_default is None
    assert not mca._compose_catalog_changes(
        composed, mca.CatalogChange(new_default=None, previous_default=None, added=(), removed=(OPUS_55, mini))
    ).announces


# --- The models the admin picks -------------------------------------------


def test_the_providers_an_email_names_are_the_chat_providers_on_both_sides():
    from pathlib import Path

    from app.llm.router import CHATLESS_PROVIDERS, PROVIDER_MAP

    chat_providers = set(PROVIDER_MAP) - CHATLESS_PROVIDERS
    assert chat_providers == mca.ANNOUNCEABLE_PROVIDERS
    source = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "model-announcements.ts").read_text(
        encoding="utf-8"
    )
    block = re.search(r"export const ANNOUNCEABLE_PROVIDERS: readonly string\[\] = \[(.*?)\];", source, re.S)
    assert block is not None
    assert set(re.findall(r'"([a-z_]+)"', block.group(1))) == mca.ANNOUNCEABLE_PROVIDERS


def test_the_model_id_shape_is_the_same_on_both_sides():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "model-announcements.ts").read_text(
        encoding="utf-8"
    )
    pattern = re.search(r"const MODEL_ID_PATTERN = /\^(.*?)\$/;", source)
    assert pattern is not None
    # The frontend writes the hyphen last in its class instead of escaping it.
    assert pattern.group(1) == mca.MODEL_ID_PATTERN.replace("\\-", "-")


ANNOUNCEABLE_IDS = [
    "gpt-5.5",
    "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "openai/gpt-4o:free",
    "llama3.1:8b-instruct-q4_K_M",
    "claude-3-5-sonnet@20240620",
    "models/gemini+exp",
    "x" * mca.MODEL_ID_MAX_CHARS,
]
SENTENCE_IDS = [
    "my model",
    "Your account is locked, sign in at evil.example",
    "gpt\u00a04o",
    "gpt\u200b4o",
    "gpt\t4o",
    "gpt\n4o",
    "<b>gpt</b>",
    "[gpt](https://evil.example)",
    "-gpt-4o",
    ".gpt",
    "\U0001f600",
    "gpt-4o!",
    "x" * (mca.MODEL_ID_MAX_CHARS + 1),
]


@pytest.mark.parametrize("model_id", ANNOUNCEABLE_IDS)
async def test_a_retired_model_named_like_a_model_id_is_announced(model_id):
    change = await mca.resolve_model_selection(
        None, [], [CatalogModel(f" {model_id} ", "openai")], offered=ModelCatalog((HAIKU,), None, None)
    )
    assert change.removed == (CatalogModel(model_id, "openai"),)


@pytest.mark.parametrize("model_id", SENTENCE_IDS)
async def test_a_retired_model_is_a_model_id_never_a_sentence(model_id):
    # The email names a retired model as typed (#5045).
    for removed in ([model_id], [CatalogModel(model_id, "openai")]):
        with pytest.raises(mca.AnnouncementSelectionError, match="must be a model id"):
            await mca.resolve_model_selection(None, [], removed, offered=ModelCatalog((HAIKU,), None, None))


async def test_a_new_model_is_held_to_the_offered_models_not_to_the_model_id_shape():
    # A new model is one the users can pick: whatever its provider calls it.
    offered = ModelCatalog((HAIKU, CatalogModel("My Model", "custom_openai")), None, None)
    change = await mca.resolve_model_selection(None, ["My Model"], [], offered=offered)
    assert change.added == (CatalogModel("My Model", "custom_openai"),)
    with pytest.raises(mca.AnnouncementSelectionError, match="not offered"):
        await mca.resolve_model_selection(None, ["Your account is locked"], [], offered=offered)


async def test_a_retired_model_is_never_announced_under_a_provider_the_email_cannot_name(monkeypatch):
    offered = ModelCatalog((HAIKU, CatalogModel("bge-m3", "tei")), None, None)

    async def _formerly(_db, _offered):
        return (CatalogModel("legacy-embed", "tei"), CatalogModel("gpt-4o", "openai"))

    monkeypatch.setattr(mca, "formerly_offered_models", _formerly)
    change = await mca.resolve_model_selection(
        None,
        [],
        ["bge-m3", "legacy-embed", "gpt-4o", CatalogModel("claude-haiku-4-5", "")],
        offered=offered,
    )
    assert change.removed == (
        CatalogModel("bge-m3", ""),
        CatalogModel("claude-haiku-4-5", "anthropic"),
        CatalogModel("gpt-4o", "openai"),
        CatalogModel("legacy-embed", ""),
    )
    # Naming one outright is still refused: nothing guessed it.
    with pytest.raises(mca.AnnouncementSelectionError, match="Unknown provider"):
        await mca.resolve_model_selection(None, [], [CatalogModel("bge-m3", "tei")], offered=offered)


async def test_the_provider_catalogs_release_the_connection_and_say_when_a_list_is_cut(monkeypatch):
    import uuid

    from app.core.security import encrypt_value
    from app.llm import router as llm_router

    events: list[str] = []

    class _Serves:
        def __init__(self, api_key: str) -> None:
            assert api_key == "sk-test"

        async def list_models(self) -> list[str]:
            events.append("listed")
            # An id no email could name is never offered for retirement (#5045).
            return [
                "gpt-5.5",
                "gpt-4o",
                "gpt-4.1",
                "gpt-4.1-mini",
                "text-embedding-3-small",
                "gpt 4o, visit evil.example",
            ]

    class _Result:
        def all(self):
            return [(uuid.uuid4(), "openai", None, encrypt_value("sk-test"))]

    class _Session:
        async def execute(self, _statement):
            events.append("read")
            return _Result()

        def in_transaction(self) -> bool:
            return True

        async def commit(self) -> None:
            events.append("commit")

    monkeypatch.setitem(llm_router.PROVIDER_MAP, "openai", _Serves)
    monkeypatch.setattr(mca, "PROVIDER_CATALOG_MAX_MODELS", 3)
    (catalog,) = await mca.list_provider_catalogs(_Session())
    # No pooled connection is held while the providers answer.
    assert events == ["read", "commit", "listed"]
    assert catalog.models == ("gpt-4.1", "gpt-4.1-mini", "gpt-4o")
    assert catalog.truncated
    monkeypatch.setattr(mca, "PROVIDER_CATALOG_MAX_MODELS", 4)
    (catalog,) = await mca.list_provider_catalogs(_Session())
    assert (len(catalog.models), catalog.truncated) == (4, False)


async def test_the_provider_catalogs_read_a_key_stored_as_a_vault_reference(monkeypatch):
    import uuid

    from app.core.security import encrypt_value
    from app.llm import router as llm_router
    from app.services import vault
    from app.services.vault.base import VaultProvider
    from app.services.vault.config import VaultProviderConfig
    from app.services.vault.resolver import SecretResolver, set_resolver

    class _Vault(VaultProvider):
        async def read(self, path: str) -> dict[str, str]:
            assert path == "llm/openai"
            return {"key": "sk-from-vault"}

    class _Serves:
        def __init__(self, api_key: str) -> None:
            assert api_key == "sk-from-vault"

        async def list_models(self) -> list[str]:
            return ["gpt-4o"]

    class _Result:
        def all(self):
            return [(uuid.uuid4(), "openai", None, encrypt_value("vault://corp/llm/openai#key"))]

    class _Session:
        async def execute(self, _statement):
            return _Result()

        def in_transaction(self) -> bool:
            return False

    async def _no_record(owner) -> None:
        return None

    monkeypatch.setattr(vault, "clear_failure", _no_record)
    monkeypatch.setitem(llm_router.PROVIDER_MAP, "openai", _Serves)
    set_resolver(SecretResolver({"corp": _Vault(VaultProviderConfig(name="corp", type="hashicorp"))}, {}, 300))
    (catalog,) = await mca.list_provider_catalogs(_Session())
    assert (catalog.models, catalog.error) == (("gpt-4o",), None)


async def test_listing_the_provider_catalogs_is_rate_limited_per_admin(monkeypatch):
    import uuid
    from types import SimpleNamespace

    from app.api.v1 import model_announcements as api

    calls: list[dict[str, object]] = []

    async def _rate_limit(request, key, **kwargs):
        calls.append({"key": key, **kwargs})

    async def _offered(_db):
        return ModelCatalog((HAIKU,), None, None)

    async def _catalogs(_db):
        config_id = uuid.uuid4()
        return (mca.ProviderCatalog(config_id, "openai", "EU", models=("gpt-4o",), truncated=True),)

    monkeypatch.setattr(api, "enforce_rate_limit", _rate_limit)
    monkeypatch.setattr(api.announcements, "read_offered_models", _offered)
    monkeypatch.setattr(api.announcements, "list_provider_catalogs", _catalogs)
    admin = SimpleNamespace(id=uuid.uuid4(), email="ann@acme.test")
    response = await api.list_model_catalogs(request=object(), db=None, user=admin, _ai_gate=None)
    assert calls == [
        {
            "key": "model_announcement_catalogs",
            "limit": api.CATALOGS_LIMIT,
            "window_seconds": api.CATALOGS_WINDOW_SECONDS,
            "bucket": str(admin.id),
        }
    ]
    assert [(m.id, m.provider) for m in response.models] == [("gpt-4o", "openai")]
    assert (response.providers[0].model_count, response.providers[0].truncated) == (1, True)


def test_every_model_announcement_route_is_admin_only_and_gated_by_what_it_does():
    from app.api import deps
    from app.api.v1 import model_announcements as api

    gates = {route.name: {dep.call for dep in route.dependant.dependencies} for route in api.router.routes}
    assert set(gates) == {
        "get_model_announcement_status",
        "list_model_catalogs",
        "preview_model_announcement",
        "send_test_model_announcement",
        "announce_default_model",
        "announce_model_changes",
    }
    assert all(deps.require_admin in route_gates for route_gates in gates.values())
    # Every provider is asked: an outbound call, gated like the provider model listing.
    assert deps.require_ai_enabled in gates["list_model_catalogs"]
    # A render of the email: nothing is called, written or sent.
    assert not {deps.require_ai_enabled, deps.require_agentic_ai} & gates["preview_model_announcement"]
    # A send is delivered only while agentic AI runs.
    for name in ("send_test_model_announcement", "announce_default_model", "announce_model_changes"):
        assert deps.require_agentic_ai in gates[name]


# --- Capabilities ----------------------------------------------------------

VEO = mca.CapabilityModel("video", "veo-3.1-generate-preview", "gemini")
TTS = mca.CapabilityModel("speech", "tts-1-hd", "openai")
LYRIA = mca.CapabilityModel("music", "lyria-3.5", "gemini")
IMAGE = mca.CapabilityModel("image", "gpt-image-2", "openai")


def _with(catalog: ModelCatalog, *capabilities: mca.CapabilityModel) -> ModelCatalog:
    return ModelCatalog(catalog.models, catalog.default_model, catalog.default_provider, tuple(sorted(capabilities)))


def test_a_capability_that_arrives_or_changes_model_is_announced_and_one_dropped_is_not():
    change = diff_catalogs(_with(BEFORE, IMAGE), _with(BEFORE, IMAGE, VEO, mca.CapabilityModel("image", "gpt-image-3", "openai")))
    assert change.announces
    assert change.capabilities == (
        mca.CapabilityNews("image", "gpt-image-3", "openai", previous_model="gpt-image-2"),
        mca.CapabilityNews("video", "veo-3.1-generate-preview", "gemini"),
    )
    assert "video none -> veo-3.1-generate-preview" in change.describe()
    # The environment stops pinning video: another choice takes over, no news.
    assert not diff_catalogs(_with(BEFORE, VEO), BEFORE).announces
    # The same model on another provider is no news for the users either.
    assert not diff_catalogs(_with(BEFORE, VEO), _with(BEFORE, mca.CapabilityModel("video", VEO.model, "litellm"))).announces


def test_capabilities_round_trip_one_per_capability():
    catalog = _with(AFTER, VEO, TTS)
    parsed = ModelCatalog.from_json(
        catalog.models_json(),
        catalog.default_model,
        catalog.default_provider,
        [*catalog.capabilities_json(), {"capability": "video", "model": "other"}, {"model": "no-capability"}, "junk"],
    )
    assert parsed == catalog
    assert not ModelCatalog(capabilities=(VEO,)).is_empty


def test_a_row_recorded_before_capabilities_were_tracked_knows_what_the_boot_left_as_it_was():
    from app.models.model_catalog_announcement import ModelCatalogAnnouncement

    recorded = {"models": AFTER.models_json(), "default_model": AFTER.default_model, "default_provider": "anthropic"}
    old = ModelCatalogAnnouncement(**recorded, capabilities=None)
    # Before the seed: the image model, and the retired Sora 2 the provider
    # fill-in stored. The seed keeps the image model and pins Veo.
    before = _with(AFTER, IMAGE, mca.CapabilityModel("video", "sora-2", "openai"))
    after = _with(AFTER, IMAGE, VEO)
    known = mca._known_catalog(old, before, after)
    assert known == _with(AFTER, IMAGE)
    # So video is new, never "replacing Sora 2", and the image model is no news.
    assert diff_catalogs(known, after).capabilities == (mca.CapabilityNews("video", VEO.model, "gemini"),)
    # A row that tracks them says what it recorded, nothing at all included.
    tracked = ModelCatalogAnnouncement(**recorded, capabilities=[])
    assert mca._known_catalog(tracked, before, after).capabilities == ()


@pytest.mark.parametrize(
    ("model_id", "name"),
    [
        ("veo-3.1-generate-preview", "Veo 3.1"),
        ("veo-3.1-fast-generate-preview", "Veo 3.1 Fast"),
        ("veo-2.0-generate-001", "Veo 2.0"),
        ("lyria-3.5", "Lyria 3.5"),
        ("lyria-3-clip-preview", "Lyria 3 Clip"),
        ("tts-1-hd", "TTS-1 HD"),
        ("tts-1", "TTS-1"),
        ("gpt-4o-mini-tts", "GPT-4o mini TTS"),
        ("gemini-3.8-flash-tts", "Gemini 3.8 Flash TTS"),
        ("eleven_multilingual_v2", "eleven_multilingual_v2"),
    ],
)
def test_media_models_are_named_as_their_vendor_names_them(model_id, name):
    assert model_display_name(model_id) == name


def test_one_new_capability_is_the_headline():
    email = render_announcement(diff_catalogs(BEFORE, _with(BEFORE, VEO)), recipient_name="Josh", platform_name="XTM One")
    assert email.subject == "Video generation is now available on XTM One"
    assert (
        "Video generation is now available on your XTM One platform: "
        "Veo 3.1 generates the AI clips of the Video Studio."
    ) in email.text
    assert "Video generation: Veo 3.1 (Google Gemini - veo-3.1-generate-preview)" in email.text
    assert "<strong>Veo 3.1</strong>" in email.html


def test_a_capability_moving_to_another_model_says_what_it_replaces():
    newer = mca.CapabilityModel("speech", "gpt-4o-mini-tts", "openai")
    email = render_announcement(diff_catalogs(_with(BEFORE, TTS), _with(BEFORE, newer)), recipient_name=None, platform_name="XTM One")
    assert email.subject == "Speech generation now uses GPT-4o mini TTS on XTM One"
    assert "in place of TTS-1 HD." in email.text
    assert "Replaces TTS-1 HD" in email.text


def test_the_speech_email_names_the_video_studio_only_while_it_is_on(request):
    def lead() -> str:
        return render_announcement(
            diff_catalogs(BEFORE, _with(BEFORE, TTS)), recipient_name=None, platform_name="XTM One"
        ).text

    assert "Speech generation is now available on your XTM One platform: TTS-1 HD reads answers aloud." in lead()
    assert "Video Studio" not in lead()

    request.getfixturevalue("video_studio_enabled")
    assert "TTS-1 HD reads answers aloud and narrates Video Studio videos." in lead()


def test_several_new_capabilities_share_one_headline():
    email = render_announcement(
        diff_catalogs(_with(BEFORE, IMAGE), _with(BEFORE, IMAGE, VEO, TTS, LYRIA)), recipient_name=None, platform_name="XTM One"
    )
    assert email.subject == "New AI capabilities in your XTM One platform"
    assert "Music generation, speech generation and video generation are ready on your XTM One platform" in email.text
    for line in (
        "Video generation: Veo 3.1 (Google Gemini - veo-3.1-generate-preview)",
        "Speech generation: TTS-1 HD (OpenAI - tts-1-hd)",
        "Music generation: Lyria 3.5 (Google Gemini - lyria-3.5)",
    ):
        assert line in email.text
    # The image model the platform already had is not news.
    assert "gpt-image-2" not in email.text


def test_capabilities_join_the_model_news_without_taking_its_headline():
    email = render_announcement(diff_catalogs(BEFORE, _with(AFTER, VEO)), recipient_name=None, platform_name="XTM One")
    assert email.subject == "Claude Sonnet 5.5 is now your default model on XTM One"
    assert "Also on your platform: video generation with Veo 3.1." in email.text
    assert "Video generation: Veo 3.1" in email.text


def test_the_status_route_reports_each_capability_an_announcement_carries():
    from app.api.v1.model_announcements import _summary
    from app.models.model_catalog_announcement import ModelCatalogAnnouncement

    newer = mca.CapabilityModel("speech", "gpt-4o-mini-tts", "openai")
    row = ModelCatalogAnnouncement(
        source=mca.SOURCE_ENV,
        status="pending",
        previous_models=BEFORE.models_json(),
        previous_default_model=BEFORE.default_model,
        previous_capabilities=_with(BEFORE, TTS).capabilities_json(),
        models=BEFORE.models_json(),
        default_model=BEFORE.default_model,
        default_provider=BEFORE.default_provider,
        capabilities=_with(BEFORE, newer, VEO).capabilities_json(),
        created_at=datetime(2026, 10, 17, 9, 0, tzinfo=UTC),
    )
    summary = _summary(mca.AnnouncementProgress(row=row, change=mca.announcement_change(row)))
    assert [item.model_dump() for item in summary.capabilities] == [
        {"capability": "speech", "model": "gpt-4o-mini-tts", "provider": "openai", "previous_model": "tts-1-hd"},
        {"capability": "video", "model": VEO.model, "provider": "gemini", "previous_model": None},
    ]
    assert summary.default_model is None and summary.added_models == []


def test_a_retirement_headline_with_capabilities_says_the_follow_up_once():
    email = render_announcement(
        diff_catalogs(BEFORE, _with(ModelCatalog((SONNET_5,), "claude-sonnet-5", "anthropic"), VEO)),
        recipient_name=None,
        platform_name="XTM One",
    )
    assert email.subject == "Claude Haiku 4.5 is no longer available on XTM One"
    assert email.text.count("now follow the default model") == 1
    assert "Video generation: Veo 3.1" in email.text

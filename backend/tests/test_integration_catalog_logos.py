"""Every logo the integration and channel catalogs name is served by the frontend.

A catalog entry carries its logo as a path under the frontend's `public/`; a
path with no file there renders as a broken image on the integration tile, the
detail header and the left-nav shortcut (Nova shipped that way).
"""

from pathlib import Path

import pytest

from app.api.v1.channel_bots import CHANNEL_BOT_CATALOG
from app.api.v1.integration_catalog import PROVIDER_CATALOG

PUBLIC = Path(__file__).resolve().parents[2] / "frontend" / "public"


def _logos() -> list[tuple[str, str]]:
    entries = [*PROVIDER_CATALOG.items(), *CHANNEL_BOT_CATALOG.items()]
    return [(key, entry["logo"]) for key, entry in entries if isinstance(entry.get("logo"), str) and entry["logo"].startswith("/")]


def test_the_catalogs_name_logos():
    # Guards the parametrisation below against silently testing nothing.
    assert len(_logos()) > 100


@pytest.mark.parametrize(("provider", "logo"), _logos())
def test_catalog_logo_file_exists(provider, logo):
    assert (PUBLIC / logo.lstrip("/")).is_file(), f"{provider}: {logo} is not in frontend/public"

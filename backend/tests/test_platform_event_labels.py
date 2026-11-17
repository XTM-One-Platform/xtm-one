"""Names a person reads instead of keys on the fleet Data flow view (UX #9882 round 2 B1, B2).

The view names an integration by ``INTEGRATION_PROVIDER_NAMES``
(frontend/src/lib/constants.ts) and a platform event by ``PLATFORM_EVENT_LABELS``
(frontend/src/lib/platform-events.ts). A provider or an event missing from those
tables would fall back to words built from its key; these tests keep both tables
complete against the backend catalogs, so a new provider or event names itself.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.services.platform_events import PLATFORM_EVENTS

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib"


def _table(path: Path, name: str, key: str) -> set[str]:
    source = path.read_text(encoding="utf-8")
    block = re.search(rf"const {name}\b[^{{]*\{{(.*?)\n\}}", source, re.S)
    if block is None:
        raise AssertionError(f"{name} is no longer a plain object literal in {path.name}")
    return set(re.findall(key, block.group(1), re.M))


def test_every_platform_event_has_a_label():
    labels = _table(_FRONTEND / "platform-events.ts", "PLATFORM_EVENT_LABELS", r'^\s*"([a-z0-9_.]+)":')
    assert sorted(set(PLATFORM_EVENTS) - labels) == []
    assert sorted(labels - set(PLATFORM_EVENTS)) == []


def test_every_catalog_provider_has_a_display_name():
    names = _table(_FRONTEND / "constants.ts", "INTEGRATION_PROVIDER_NAMES", r"^\s*([a-z0-9_]+):")
    assert sorted(set(PROVIDER_CATALOG) - names) == []

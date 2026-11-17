"""Every integration type of the provider catalog has a frontend label.

``frontend/src/lib/constants.ts`` maps each ``integration_types`` value to the
English label the catalog cards, the provider details dialog and the agent
wizards render through ``t_i18n``. A type missing there falls back to its raw
id ("guardrails" in lowercase on the Nova Prompt Safety card), in every
locale, and nothing announces it. The drift is caught here instead, together with the catalog key each label needs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.api.v1.integration_catalog import PROVIDER_CATALOG

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend/src"
LOCALES = ("en", "fr", "es", "ja")


def _frontend_labels() -> dict[str, str]:
    source = (FRONTEND_SRC / "lib/constants.ts").read_text(encoding="utf-8")
    block = source.split("const INTEGRATION_TYPE_LABEL_KEYS = {", 1)[1].split("} as const;", 1)[0]
    return dict(re.findall(r'^\s{2}([a-z0-9_]+):\s*"([^"]+)"', block, re.M))


def _catalog_types() -> set[str]:
    return {
        integration_type
        for entry in PROVIDER_CATALOG.values()
        for integration_type in entry.get("integration_types", [])
    }


def test_every_catalog_integration_type_has_a_frontend_label():
    missing = _catalog_types() - set(_frontend_labels())
    assert not missing, (
        f"Integration types without a label in INTEGRATION_TYPE_LABEL_KEYS: {sorted(missing)}"
    )


def test_every_integration_type_label_is_translated():
    labels = set(_frontend_labels().values())
    for locale in LOCALES:
        catalog = json.loads((FRONTEND_SRC / f"i18n/lang/{locale}.json").read_text(encoding="utf-8"))
        missing = sorted(label for label in labels if label not in catalog)
        assert not missing, f"{locale}.json lacks integration type labels: {missing}"

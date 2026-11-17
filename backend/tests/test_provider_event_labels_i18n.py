"""Every provider catalog event label reads in the viewer's language.

The schedule view and the trigger editor show an event by its catalog label
(``available_events[].label``) through the frontend's ``t_text``, which
translates the text when the catalog holds it and shows it as written
otherwise. A label with no entry in fr, es or ja therefore renders in English
to those viewers ("Sur New Slack message depuis Slack"). This guard fails the
moment a provider adds an event label without its translations.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG

LANG_DIR = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "i18n" / "lang"
LOCALES = ("en", "fr", "es", "ja")
# The two labels a custom integration's catalog entry carries
# (``app/api/v1/integrations.py``, polling and webhook support).
CUSTOM_EVENT_LABELS = {"Polling event", "Webhook event"}


def _event_labels() -> set[str]:
    labels = set(CUSTOM_EVENT_LABELS)
    for spec in PROVIDER_CATALOG.values():
        for event in spec.get("available_events", []):
            if isinstance(event, dict) and event.get("label"):
                labels.add(event["label"])
    return labels


def _event_descriptions() -> set[str]:
    # The event picker, the integration page, the provider dialog and the
    # assignment summary show the description under the label, through the
    # same ``t_text``: a description with no entry renders in English.
    descriptions: set[str] = set()
    for spec in PROVIDER_CATALOG.values():
        for event in spec.get("available_events", []):
            if isinstance(event, dict) and event.get("description"):
                descriptions.add(event["description"])
    return descriptions


def test_the_catalog_lists_event_labels():
    assert len(_event_labels()) > len(CUSTOM_EVENT_LABELS)


@pytest.mark.parametrize("locale", LOCALES)
def test_every_event_label_has_a_catalog_entry(locale):
    catalog = json.loads((LANG_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    missing = sorted(label for label in _event_labels() if label not in catalog)
    assert not missing, f"{locale}.json lacks provider event labels: {missing}"


@pytest.mark.parametrize("locale", LOCALES)
def test_every_event_description_has_a_catalog_entry(locale):
    assert _event_descriptions()
    catalog = json.loads((LANG_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    missing = sorted(text for text in _event_descriptions() if text not in catalog)
    assert not missing, f"{locale}.json lacks provider event descriptions: {missing}"

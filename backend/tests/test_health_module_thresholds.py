# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Platform Health editable thresholds (#101 S10b, dev-docs/platform-health.md sections 4.3, 4.4 and 7):
the OpenCTI and OpenAEV (E) thresholds live in the Platform Health settings, default to the values the
checks shipped with, refuse values out of bounds, and are what the checks compare against."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.services.health import openaev_checks, opencti_checks
from app.services.health.settings import (
    HealthOpenAEVThresholds,
    HealthOpenCTIThresholds,
    HealthSettings,
    _read,
)

NOW = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
REG = SimpleNamespace(id="reg-1")


def test_the_defaults_are_the_thresholds_the_checks_shipped_with() -> None:
    cti = HealthSettings().opencti
    assert (cti.connector_stale_minutes, cti.connector_errors_max, cti.queue_depth_max) == (30, 10, 100_000)
    assert (cti.search_disk_max_percent, cti.ingestion_lag_minutes) == (85, 15)
    aev = HealthSettings().openaev
    assert (aev.integration_silent_minutes, aev.agents_offline_max_percent) == (60, 50)


def test_a_value_stored_before_the_thresholds_existed_reads_their_defaults() -> None:
    settings = _read({"enabled": True})
    assert settings.enabled is True
    assert settings.opencti == HealthOpenCTIThresholds()
    assert settings.openaev == HealthOpenAEVThresholds()


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (HealthOpenCTIThresholds, "connector_stale_minutes", 1),
        (HealthOpenCTIThresholds, "connector_errors_max", -1),
        (HealthOpenCTIThresholds, "search_disk_max_percent", 100),
        (HealthOpenCTIThresholds, "ingestion_lag_minutes", 0),
        (HealthOpenAEVThresholds, "integration_silent_minutes", 4),
        (HealthOpenAEVThresholds, "agents_offline_max_percent", 100),
    ],
)
def test_a_threshold_out_of_bounds_is_refused(model: type, field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        model(**{field: value})


def test_the_opencti_checks_compare_against_the_thresholds_given() -> None:
    snapshot = {
        "connectors": [
            {
                "id": "c1",
                "name": "C1",
                "active": True,
                "updated_at": (NOW - timedelta(minutes=20)).isoformat(),
                "works_in_error": 5,
            }
        ],
        "rabbitmq": {"queue_messages": 5_000, "consumers": 2},
        "search_engine": {"status": "green", "disk_used_percent": 70},
        "ingestion_lag_seconds": 600,
    }
    tight = HealthOpenCTIThresholds(
        connector_stale_minutes=10,
        connector_errors_max=4,
        queue_depth_max=1_000,
        search_disk_max_percent=60,
        ingestion_lag_minutes=5,
    )
    for read in (
        opencti_checks.connector_stale,
        opencti_checks.connector_errors,
        opencti_checks.queue_depth,
        opencti_checks.search_disk,
        opencti_checks.ingestion_lag,
    ):
        assert [r.failing for r in read(REG, snapshot, NOW)] == [False], read.__name__
        assert [r.failing for r in read(REG, snapshot, NOW, tight)] == [True], read.__name__
    stale = opencti_checks.connector_stale(REG, snapshot, NOW, tight)[0]
    assert stale.evidence["threshold"] == "10 minutes"


def test_the_openaev_checks_compare_against_the_thresholds_given() -> None:
    snapshot = {
        "integrations": [
            {"id": "i1", "name": "I1", "kind": "injector", "updated_at": (NOW - timedelta(minutes=30)).isoformat()}
        ],
        "agents": {"total": 10, "offline": 3},
    }
    tight = HealthOpenAEVThresholds(integration_silent_minutes=15, agents_offline_max_percent=20)
    for read in (openaev_checks.integration_silent, openaev_checks.agents_offline):
        assert [r.failing for r in read(REG, snapshot, NOW)] == [False], read.__name__
        assert [r.failing for r in read(REG, snapshot, NOW, tight)] == [True], read.__name__
    offline = openaev_checks.agents_offline(REG, snapshot, NOW, tight)[0]
    assert offline.evidence["threshold"] == "20 % offline"

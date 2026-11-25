# SPDX-License-Identifier: Apache-2.0
"""The stage card of a member without a module account (#2042, slice 4).

A per-user stage run skipped by the module preflight is read back by the
stage assessment endpoint as ``no_module_account`` with the modules to sign
in to, so the card shows the log-in-once hint instead of ``pending`` (which
would start another skipped run on every landing).
"""

from __future__ import annotations

from types import SimpleNamespace

from app.api.v1.ctem_metrics import _artifact_stale, _artifact_status, _no_module_account_modules
from app.services.ctem_module_preflight import NO_MODULE_ACCOUNT


def _run(status: str, error: str | None, states: dict[str, str] | None) -> SimpleNamespace:
    ctx = {"ctem_vertical": "cyber"}
    if states is not None:
        ctx["module_accounts"] = states
    return SimpleNamespace(status=status, error=error, trigger_context=ctx)


def test_skipped_run_names_the_missing_modules() -> None:
    run = _run("skipped", NO_MODULE_ACCOUNT, {"opencti": "missing", "openaev": "missing"})
    assert _no_module_account_modules(run, NO_MODULE_ACCOUNT) == ["openaev", "opencti"]


def test_present_and_unknown_modules_are_not_named() -> None:
    run = _run("skipped", NO_MODULE_ACCOUNT, {"opencti": "present", "openaev": "missing"})
    assert _no_module_account_modules(run, NO_MODULE_ACCOUNT) == ["openaev"]
    run = _run("skipped", NO_MODULE_ACCOUNT, {"opencti": "unknown"})
    assert _no_module_account_modules(run, NO_MODULE_ACCOUNT) == []


def test_any_other_run_is_not_a_module_account_skip() -> None:
    assert _no_module_account_modules(None, NO_MODULE_ACCOUNT) is None
    assert _no_module_account_modules(_run("completed", None, {"opencti": "missing"}), NO_MODULE_ACCOUNT) is None
    assert (
        _no_module_account_modules(_run("skipped", "other_reason", {"opencti": "missing"}), NO_MODULE_ACCOUNT) is None
    )


def test_card_status_answers_no_module_account_only_without_an_artifact() -> None:
    modules = ["openaev"]
    assert (
        _artifact_status(generating=False, artifact=None, not_assessable=None, no_module_account=modules)
        == "no_module_account"
    )
    # A run in flight and a stored assessment both outrank the hint.
    assert (
        _artifact_status(generating=True, artifact=None, not_assessable=None, no_module_account=modules) == "generating"
    )
    assert (
        _artifact_status(generating=False, artifact={"headline": "h"}, not_assessable=None, no_module_account=modules)
        == "ready"
    )
    assert _artifact_status(generating=False, artifact=None, not_assessable=None) is None


def test_no_stale_regeneration_while_the_newest_run_was_skipped() -> None:
    def always_stale(_generated_at: object) -> bool:
        return True

    artifact = {"headline": "h"}
    assert not _artifact_stale(
        artifact=artifact, generated_at=None, not_assessable=None, is_stale=always_stale, no_module_account=["opencti"]
    )
    assert _artifact_stale(artifact=artifact, generated_at=None, not_assessable=None, is_stale=always_stale)

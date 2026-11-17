"""The live LLM smoke suite (``tests/live``) stays off unless explicitly enabled.

It calls the real provider APIs with real keys and real cost, so its gate is
pinned here, in the suite CI runs: without ``LIVE_LLM_SMOKE=1`` AND the
provider's key every live test is skipped at collection, no workflow turns the
flag on, and a live report never carries a key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live.live_llm_support import (
    KEY_ENV,
    LIVE_FLAG,
    REDACTED,
    register_secret,
    scrub,
    skip_reason,
)

_FAKE_KEY = "fake-provider-key-for-the-gate-test"


@pytest.mark.parametrize("provider", sorted(KEY_ENV))
def test_a_key_alone_does_not_turn_the_suite_on(provider: str):
    assert skip_reason(provider, {KEY_ENV[provider]: _FAKE_KEY}) is not None


@pytest.mark.parametrize("provider", sorted(KEY_ENV))
def test_the_flag_alone_does_not_run_a_provider_without_its_key(provider: str):
    assert skip_reason(provider, {LIVE_FLAG: "1", KEY_ENV[provider]: "  "}) is not None


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "2", " 1", "1 "])
def test_only_the_documented_flag_value_turns_it_on(value: str):
    assert skip_reason("anthropic", {LIVE_FLAG: value, KEY_ENV["anthropic"]: _FAKE_KEY}) is not None


def test_the_flag_and_the_key_together_turn_it_on():
    assert skip_reason("anthropic", {LIVE_FLAG: "1", KEY_ENV["anthropic"]: _FAKE_KEY}) is None


def test_no_workflow_turns_the_suite_on():
    workflows = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    if not workflows.is_dir():
        pytest.skip("no .github/workflows next to the backend")
    offenders = [path.name for path in workflows.glob("*.y*ml") if LIVE_FLAG in path.read_text(encoding="utf-8")]
    assert offenders == []


def test_reports_are_scrubbed_of_registered_secrets_and_key_shapes():
    register_secret("registered-secret-value-0001")

    text = scrub(
        "a registered-secret-value-0001 b sk-ant-fake0123456789 c sk-or-fake0123456789 d AIzaFAKE0123456789abcdef e "
        "sk-proj-fake0123456789 f sk_0123456789abcdef0123456789 g"
    )

    assert text == f"a {REDACTED} b {REDACTED} c {REDACTED} d {REDACTED} e {REDACTED} f {REDACTED} g"

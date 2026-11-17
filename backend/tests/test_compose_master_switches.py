"""Every on/off switch in ``.env.sample`` reaches the shipped stack.

A Compose ``.env`` file only feeds interpolation: a variable that
``docker-compose.yml`` does not forward never reaches a container, so a
documented ``RELAYS_ENABLED=false`` or ``DISABLE_CUSTOM_TOOLS=true`` would
change nothing. The platform and the worker both read ``Settings``, so both
forward every documented switch, with the default the platform ships - blank
(unset) for a Settings > Access and security option, which a value would lock, and for a
switch whose default the platform derives. A switch left out is listed in
``_NOT_FORWARDED`` with the reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.core.security_options import SECURITY_OPTIONS

REPO_ROOT = Path(__file__).resolve().parents[2]

# `# RELAYS_ENABLED=true   # master switch for ...`
_MASTER_SWITCH = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=(\S*)\s+#\s*master switch\b", re.MULTILINE)

# `# NAME=value`, commented out or not.
_DOCUMENTED = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=(\S*)", re.MULTILINE)

# Documented switches docker-compose.yml deliberately does not forward: a value
# left in an operator's `.env` would otherwise reach a production container.
_NOT_FORWARDED = {
    "DEBUG": "development only: verbose SQL logging and loopback URLs allowed",
    "A2A_ALLOW_INSECURE_LOOPBACK": "end-to-end stack only: docker-compose.e2e.yml forwards it",
    "TRUST_PROXY_HEADERS": "trusts X-Forwarded-For from any peer: Settings > Access and security, or FORWARDED_ALLOW_IPS",
}

_SERVICES = ["platform", "worker", "worker-preview"]


def _master_switches() -> dict[str, str]:
    sample = (REPO_ROOT / ".env.sample").read_text(encoding="utf-8")
    return dict(_MASTER_SWITCH.findall(sample))


def _compose_environment(service: str) -> dict[str, str]:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return compose["services"][service]["environment"]


def _documented_switches() -> list[str]:
    """Variables of ``.env.sample`` that ``Settings`` reads as an on/off switch.

    A boolean field, or a field documented with ``true`` / ``false`` (the
    ``TOOL_APPROVALS_ENABLED`` seed is text, read as one).
    """
    sample = (REPO_ROOT / ".env.sample").read_text(encoding="utf-8")
    switches: dict[str, None] = {}
    for name, value in _DOCUMENTED.findall(sample):
        field = Settings.model_fields.get(name.lower())
        if field is None:
            continue
        if field.annotation in (bool, bool | None) or value.lower() in {"true", "false"}:
            switches[name] = None
    return list(switches)


def _forwarded_value(name: str) -> str:
    field = Settings.model_fields[name.lower()]
    if name.lower() in SECURITY_OPTIONS or field.default is None:
        return f"${{{name}:-}}"
    return f"${{{name}:-{str(field.default).lower()}}}"


def test_the_switch_reader_finds_the_documented_switches():
    # Guards the reader: a reworded .env.sample must not leave nothing to check.
    switches = set(_documented_switches())
    assert {
        "RELAYS_ENABLED",
        "RUNNERS_ENABLED",
        "DISABLE_CUSTOM_TOOLS",
        "TOOL_APPROVALS_ENABLED",
        "MCP_DCR_ENABLED",
        "ARCADE_ENABLED",
    } <= switches
    assert set(_NOT_FORWARDED) <= switches


@pytest.mark.parametrize("service", _SERVICES)
@pytest.mark.parametrize("name", [n for n in _documented_switches() if n not in _NOT_FORWARDED])
def test_every_documented_switch_is_forwarded(service, name):
    assert _compose_environment(service).get(name) == _forwarded_value(name)


@pytest.mark.parametrize("service", _SERVICES)
@pytest.mark.parametrize("name", sorted(_NOT_FORWARDED))
def test_a_switch_left_out_is_not_forwarded(service, name):
    assert name not in _compose_environment(service)


@pytest.mark.parametrize(
    "name",
    [n for n in _documented_switches() if n.lower() in SECURITY_OPTIONS and n not in _NOT_FORWARDED],
)
def test_a_blank_security_option_is_unset(monkeypatch, name):
    # What `${NAME:-}` puts in the container when .env does not set it: the
    # value of an unset variable, and Settings > Access and security keeps the field.
    field = name.lower()
    monkeypatch.delenv(name, raising=False)
    unset = getattr(Settings(), field)
    monkeypatch.setenv(name, "")
    blank = Settings()
    assert getattr(blank, field) == unset
    assert field not in blank.environment_fields


@pytest.mark.parametrize("raw", ["", "  "])
def test_a_blank_boolean_switch_takes_the_field_default(monkeypatch, raw):
    monkeypatch.setenv("ARCADE_ENABLED", raw)
    monkeypatch.setenv("AI_CATALOG_ENABLED", raw)
    built = Settings()
    assert built.arcade_enabled is True
    assert built.ai_catalog_enabled is False


def test_a_set_boolean_switch_still_wins(monkeypatch):
    monkeypatch.setenv("ARCADE_ENABLED", "false")
    monkeypatch.setenv("MCP_DCR_ENABLED", "false")
    built = Settings()
    assert built.arcade_enabled is False
    assert built.mcp_dcr_enabled is False
    assert "mcp_dcr_enabled" in built.environment_fields


def test_the_env_sample_documents_the_subsystem_master_switches():
    # Guards the reader: a reworded comment must not leave nothing to check.
    assert {"RELAYS_ENABLED", "RUNNERS_ENABLED"} <= set(_master_switches())


@pytest.mark.parametrize("name", sorted(_master_switches()))
def test_the_documented_default_is_the_shipped_one(name):
    shipped = str(Settings.model_fields[name.lower()].default).lower()
    assert _master_switches()[name] == shipped


@pytest.mark.parametrize("service", ["platform", "worker"])
@pytest.mark.parametrize("name", sorted(_master_switches()))
def test_every_master_switch_is_forwarded_with_the_shipped_default(service, name):
    shipped = str(Settings.model_fields[name.lower()].default).lower()
    assert _compose_environment(service).get(name) == f"${{{name}:-{shipped}}}"

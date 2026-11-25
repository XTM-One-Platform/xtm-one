"""The xtm-one labs: dev and copilot declarations beside the xtm_one one, and the entity seed.

No single ``PLATFORM_MODE`` serves every page: ``AGENTIC_CODING`` needs copilot or dev mode
(``FLAG_CONDITIONS``) while ``/ctem/:stage`` needs XTM One mode. These pin the two
declarations and the seed the script runs once the preview answers.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKS = ROOT / ".xtm-one" / "checks.toml"
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not LAB.is_file(), reason="no .xtm-one next to the backend")


def _services() -> dict:
    return tomllib.loads(CHECKS.read_text(encoding="utf-8"))["service"]


def test_the_two_labs_run_the_same_script_in_the_two_modes():
    services = _services()
    lab, dev = services["lab"], services["lab-dev"]
    assert lab["argv"] == dev["argv"] == ["python", ".xtm-one/lab/lab.py"]
    assert lab["env"]["PLATFORM_MODE"] == "xtm_one"
    assert dev["env"]["PLATFORM_MODE"] == "dev"
    assert lab["env"]["ENABLED_FEATURES"] == dev["env"]["ENABLED_FEATURES"] == "*"


def test_the_copilot_lab_is_the_same_lab_in_copilot_mode():
    services = _services()
    lab, copilot = services["lab"], services["lab-copilot"]
    assert copilot["env"]["PLATFORM_MODE"] == "copilot"
    assert {k: v for k, v in copilot["env"].items() if k != "PLATFORM_MODE"} == {
        k: v for k, v in lab["env"].items() if k != "PLATFORM_MODE"
    }
    assert {k: v for k, v in copilot.items() if k not in ("env", "description")} == {
        k: v for k, v in lab.items() if k not in ("env", "description")
    }


def test_the_cost_masked_labs_are_the_lab_and_the_copilot_lab_with_cost_data_hidden():
    """``lab-roi-cost-masked`` (XTM One) and ``lab-copilot-roi-cost-masked`` (+ light) hide cost data, nothing else."""
    services = _services()
    hidden = {"OBSERVABILITY_COST_DATA_ENABLED": "false"}
    expected = {
        "lab-roi-cost-masked": ("lab", hidden),
        "lab-copilot-roi-cost-masked": ("lab-copilot", hidden),
        "lab-copilot-roi-cost-masked-light": ("lab-copilot", {**hidden, "LAB_THEME": "light"}),
    }
    for name, (base_name, switches) in expected.items():
        service, base = services[name], services[base_name]
        assert service["env"] == {**base["env"], **switches}, name
        assert {k: v for k, v in service.items() if k not in ("env", "description")} == {
            k: v for k, v in base.items() if k not in ("env", "description")
        }, name
    assert "OBSERVABILITY_COST_DATA_ENABLED" not in services["lab"]["env"]
    assert "OBSERVABILITY_COST_DATA_ENABLED" not in services["lab-copilot"]["env"]


def test_the_light_lab_is_the_same_lab_with_the_light_theme_and_the_default_sets_none():
    services = _services()
    lab, light = services["lab"], services["lab-light"]
    assert light["env"]["LAB_THEME"] == "light"
    assert "LAB_THEME" not in lab["env"]
    assert {k: v for k, v in light["env"].items() if k != "LAB_THEME"} == lab["env"]
    assert {k: v for k, v in light.items() if k not in ("env", "description")} == {
        k: v for k, v in lab.items() if k not in ("env", "description")
    }


def test_the_home_state_labs_are_the_same_lab_with_one_switch_each():
    """``lab-ce`` and ``lab-model-less`` (and their light twins) differ from the default lab by their switches only."""
    services = _services()
    lab = services["lab"]
    expected = {
        "lab-ce": {"LAB_EDITION": "community"},
        "lab-ce-light": {"LAB_EDITION": "community", "LAB_THEME": "light"},
        "lab-model-less": {"LAB_DEFAULT_MODEL": "none"},
        "lab-model-less-light": {"LAB_DEFAULT_MODEL": "none", "LAB_THEME": "light"},
        "lab-ai-off": {"LAB_AI": "off"},
        "lab-group-ai-off": {"LAB_PEER_GROUP_AI": "off"},
        "lab-org-setup-prompt": {"LAB_ORG_SETUP_PROMPT": "on"},
        "lab-org-setup-prompt-light": {"LAB_ORG_SETUP_PROMPT": "on", "LAB_THEME": "light"},
        # Organization rows written by platform syncs next to typed rows (#1345).
        "lab-org-synced": {"LAB_ORG_SYNCED": "on"},
        "lab-org-synced-light": {"LAB_ORG_SYNCED": "on", "LAB_THEME": "light"},
        "lab-perf-thread": {"LAB_PERF_THREAD": "on"},
        "lab-langfuse-env": {"LAB_LANGFUSE": "env"},
        # SMTP configured on lab-only values (#1542).
        "lab-smtp": {"LAB_SMTP": "on"},
        "lab-db-license": {"LAB_LICENSE_SOURCE": "database"},
        "lab-db-license-light": {"LAB_LICENSE_SOURCE": "database", "LAB_THEME": "light"},
        # The worker runs: a manually triggered assignment run executes on the stub model (#11297).
        "lab-worker": {"LAB_WORKER": "1"},
        "lab-worker-light": {"LAB_WORKER": "1", "LAB_THEME": "light"},
    }
    for name, switches in expected.items():
        service = services[name]
        assert service["env"] == {**lab["env"], **switches}, name
        assert {k: v for k, v in service.items() if k not in ("env", "description")} == {
            k: v for k, v in lab.items() if k not in ("env", "description")
        }, name
    assert not {
        "LAB_EDITION",
        "LAB_DEFAULT_MODEL",
        "LAB_AI",
        "LAB_PEER_GROUP_AI",
        "LAB_LANGFUSE",
        "LAB_SMTP",
        "LAB_LICENSE_SOURCE",
        "LAB_ORG_SETUP_PROMPT",
        "LAB_ORG_SYNCED",
        "LAB_PERF_THREAD",
        "LAB_WORKER",
    } & set(lab["env"])


def test_lab_org_setup_prompt_leaves_the_profile_unsaved_and_reads_propose_true_back():
    """#10247: ``LAB_ORG_SETUP_PROMPT=on`` renders the first-login setup dialog of #9173 S3 on the real API."""
    calls: list[tuple[str, str]] = []
    answers: dict = {
        ("GET", "/organization-context"): {"version": None},
        ("PUT", "/organization-context"): {"version": 1},
        ("GET", "/organization-context/setup-prompt"): {"propose": True, "outcome": None, "answered_at": None},
    }

    def api(method, path, token=None, body=None):
        calls.append((method, path))
        return answers.get((method, path), {})

    names = (
        "LAB_ORG_PROFILE",
        "LAB_ORG_TECH_STACK",
        "LAB_ORG_SETUP_PROMPT_STATES",
        "LAB_ORG_CONTEXT_STATES",
        "LAB_CTEM_KEV_TAILORED_STATES",
        "LAB_CTEM_OPENCTI_STATES",
        "lab_switch",
        "seed_org_ai_draft_stub",
        "seed_org_setup_prompt",
    )
    base = "http://localhost:3100"
    # Switch off (every other lab): version 1 is saved as today.
    assert _lab_namespace(*names, api=api)["seed_org_ai_draft_stub"]("t", base)["id"] == "profile version 1"
    assert ("PUT", "/organization-context") in calls

    calls.clear()
    switched = _lab_namespace(*names, api=api, env={"LAB_ORG_SETUP_PROMPT": "on"})
    assert switched["seed_org_ai_draft_stub"]("t", base)["id"] == "profile version None"
    assert ("PUT", "/organization-context") not in calls
    assert switched["seed_org_setup_prompt"]("t", base) == {"id": "propose true", "url": f"{base}/"}

    answers[("GET", "/organization-context/setup-prompt")] = {"propose": False, "outcome": "dismissed"}
    with pytest.raises(RuntimeError, match="the setup proposal answered"):
        switched["seed_org_setup_prompt"]("t", base)

    seed = _lab_source("seed_entities")
    assert "seeded['org setup prompt'] = seed_org_setup_prompt(token, base)" in seed
    assert seed.index("seed_org_ai_draft_stub(") < seed.index("seed_org_setup_prompt(") < seed.index("seeded.json")
    assert "if lab_switch('LAB_ORG_SETUP_PROMPT', LAB_ORG_SETUP_PROMPT_STATES) == 'on':" in seed


def _lab_namespace(*names: str, env: dict[str, str] | None = None, **extra) -> dict:
    """Some top-level definitions of lab.py run in one namespace, with a recorded ``log`` and a given environment."""
    import types

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    nodes = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in names)
        or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets))
    ]
    logged: list[str] = []
    namespace: dict = {"os": types.SimpleNamespace(environ=env or {}), "log": logged.append, "logged": logged, **extra}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


def _lab_source(name: str) -> str:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    return ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name))


def test_lab_edition_community_blanks_the_licence_and_the_default_keeps_it():
    names = ("LAB_EDITIONS", "LAB_LICENSE_SOURCES", "lab_switch", "edition_env", "stores_database_licence")
    assert _lab_namespace(*names)["edition_env"]() == {}
    assert _lab_namespace(*names, env={"LAB_EDITION": "Community"})["edition_env"]() == {"ENTERPRISE_LICENSE": ""}
    unknown = _lab_namespace(*names, env={"LAB_EDITION": "gold"})
    assert unknown["edition_env"]() == {}
    assert unknown["logged"] == ["LAB_EDITION='gold' ignored: one of enterprise, community"]
    assert "**edition_env()" in _lab_source("backend_env")


_LICENCE_NAMES = ("LAB_EDITIONS", "LAB_LICENSE_SOURCES", "lab_switch", "edition_env", "stores_database_licence",
                  "seed_database_licence")


def test_lab_license_source_database_blanks_the_env_licence_on_an_enterprise_lab_only():
    assert _lab_namespace(*_LICENCE_NAMES)["stores_database_licence"]() is False
    database = _lab_namespace(*_LICENCE_NAMES, env={"LAB_LICENSE_SOURCE": "Database", "ENTERPRISE_LICENSE": "pem"})
    assert database["stores_database_licence"]() is True
    assert database["edition_env"]() == {"ENTERPRISE_LICENSE": ""}
    # A community lab has no licence to store: the switch stays off.
    community = _lab_namespace(*_LICENCE_NAMES, env={"LAB_LICENSE_SOURCE": "database", "LAB_EDITION": "community"})
    assert community["stores_database_licence"]() is False
    unknown = _lab_namespace(*_LICENCE_NAMES, env={"LAB_LICENSE_SOURCE": "vault"})
    assert unknown["stores_database_licence"]() is False
    assert unknown["logged"] == ["LAB_LICENSE_SOURCE='vault' ignored: one of env, database"]


def test_the_database_licence_is_installed_through_the_settings_api_and_read_back():
    calls: list[tuple[str, str, dict | None]] = []
    answers: dict = {
        ("GET", "/platform/config"): {"xtm_license": {"license_source": "database"}},
        ("GET", "/settings/platform"): {"has_db_license": True, "has_env_license": False},
    }

    def api(method, path, token=None, body=None):
        calls.append((method, path, body))
        return answers.get((method, path), {})

    base = "http://localhost:3100"
    seed = _lab_namespace(*_LICENCE_NAMES, api=api, env={"LAB_LICENSE_SOURCE": "database", "ENTERPRISE_LICENSE": " pem "})
    assert seed["seed_database_licence"]("t", base) == {"id": "database", "url": f"{base}/settings/platform"}
    assert calls[0] == ("PUT", "/settings/platform", {"enterprise_license": "pem"})

    answers[("GET", "/platform/config")] = {"xtm_license": {"license_source": "env_var"}}
    with pytest.raises(RuntimeError, match="not 'database'"):
        seed["seed_database_licence"]("t", base)
    answers[("GET", "/platform/config")] = {"xtm_license": {"license_source": "database"}}
    answers[("GET", "/settings/platform")] = {"has_db_license": True, "has_env_license": True}
    with pytest.raises(RuntimeError, match="has_db_license true"):
        seed["seed_database_licence"]("t", base)

    none = _lab_namespace(*_LICENCE_NAMES, api=api, env={"LAB_LICENSE_SOURCE": "database"})
    with pytest.raises(RuntimeError, match="no ENTERPRISE_LICENSE"):
        none["seed_database_licence"]("t", base)
    # First in the seed: every Enterprise Edition row after it needs the licence.
    entities = _lab_source("seed_entities")
    assert entities.index("seed_database_licence(token, base)") < entities.index("seed_theme(token)")


_LANGFUSE_NAMES = ("LAB_LANGFUSE_STATES", "LAB_LANGFUSE_PUBLIC_KEY", "LAB_LANGFUSE_SECRET_KEY", "LAB_LANGFUSE_HOST",
                   "lab_switch", "langfuse_env", "seed_langfuse_states")


def test_lab_langfuse_env_hands_the_lab_keys_to_the_backend_and_the_default_does_not():
    assert _lab_namespace(*_LANGFUSE_NAMES)["langfuse_env"]() == {}
    assert _lab_namespace(*_LANGFUSE_NAMES, env={"LAB_LANGFUSE": "off"})["langfuse_env"]() == {}
    env = _lab_namespace(*_LANGFUSE_NAMES, env={"LAB_LANGFUSE": "env"})
    assert env["langfuse_env"]() == {
        "LANGFUSE_PUBLIC_KEY": env["LAB_LANGFUSE_PUBLIC_KEY"],
        "LANGFUSE_SECRET_KEY": env["LAB_LANGFUSE_SECRET_KEY"],
        "LANGFUSE_HOST": env["LAB_LANGFUSE_HOST"],
    }
    # Lab-only values, and a host on the loopback: no Langfuse call leaves the lab.
    assert "lab-only" in env["LAB_LANGFUSE_PUBLIC_KEY"] and "lab-only" in env["LAB_LANGFUSE_SECRET_KEY"]
    assert env["LAB_LANGFUSE_HOST"].startswith("http://127.0.0.1:")
    assert "**langfuse_env()" in _lab_source("backend_env")


_SMTP_NAMES = ("LAB_SMTP_STATES", "LAB_SMTP_HOSTNAME", "LAB_SMTP_PORT", "LAB_SMTP_FROM", "lab_switch", "smtp_env")


def test_lab_smtp_on_configures_a_loopback_smtp_server_and_the_default_does_not():
    """#1542: ``LAB_SMTP=on`` makes the backend answer SMTP configured, on a host no mail leaves the lab by."""
    assert _lab_namespace(*_SMTP_NAMES)["smtp_env"]() == {}
    assert _lab_namespace(*_SMTP_NAMES, env={"LAB_SMTP": "off"})["smtp_env"]() == {}
    unknown = _lab_namespace(*_SMTP_NAMES, env={"LAB_SMTP": "relay"})
    assert unknown["smtp_env"]() == {}
    assert unknown["logged"] == ["LAB_SMTP='relay' ignored: one of off, on"]
    on = _lab_namespace(*_SMTP_NAMES, env={"LAB_SMTP": "On"})
    env = on["smtp_env"]()
    assert env["SMTP_HOSTNAME"] == "127.0.0.1" and env["SMTP_PORT"] == "9"
    assert env["SMTP_STARTTLS"] == "false" and env["SMTP_USE_TLS"] == "false"
    assert env["SMTP_FROM"].endswith(".invalid")
    assert "**smtp_env()" in _lab_source("backend_env")


def test_the_langfuse_states_are_saved_and_read_back():
    calls: list[tuple[str, str, dict | None]] = []
    answers: dict = {("GET", "/settings/langfuse"): {"has_public_key": True, "has_secret_key": True}}

    def api(method, path, token=None, body=None):
        calls.append((method, path, body))
        return answers.get((method, path), {})

    base = "http://localhost:3100"
    url = f"{base}/settings/usage/observability"
    stored = _lab_namespace(*_LANGFUSE_NAMES, api=api)
    assert stored["seed_langfuse_states"]("t", base) == ({"Langfuse stored keys": {"id": "stored", "url": url}}, {})
    put = next(body for method, path, body in calls if method == "PUT")
    assert put == {"enabled": True, "host": stored["LAB_LANGFUSE_HOST"],
                   "public_key": stored["LAB_LANGFUSE_PUBLIC_KEY"], "secret_key": stored["LAB_LANGFUSE_SECRET_KEY"]}
    # No connection test: nothing is sent to a Langfuse.
    assert all(path != "/settings/langfuse/test" for _, path, _ in calls)

    answers[("GET", "/settings/langfuse")] = {"has_public_key": False, "has_secret_key": True}
    assert "both keys stored" in stored["seed_langfuse_states"]("t", base)[1]["Langfuse stored keys"]

    calls.clear()
    env = _lab_namespace(*_LANGFUSE_NAMES, api=api, env={"LAB_LANGFUSE": "env"})
    answers[("GET", "/settings/langfuse")] = {"is_from_env": True, "has_public_key": True, "has_secret_key": True}
    assert env["seed_langfuse_states"]("t", base) == ({"Langfuse env-managed": {"id": "env", "url": url}}, {})
    # The env-managed configuration is read, never written (the API refuses the edit with a 403).
    assert [method for method, _, _ in calls] == ["GET"]
    answers[("GET", "/settings/langfuse")] = {"is_from_env": False}
    assert "is_from_env true" in env["seed_langfuse_states"]("t", base)[1]["Langfuse env-managed"]

    calls.clear()
    assert _lab_namespace(*_LANGFUSE_NAMES, api=api, env={"LAB_LANGFUSE": "off"})["seed_langfuse_states"]("t", base) == ({}, {})
    assert calls == []
    # Before the AI switches, which would not refuse it but come last by contract.
    seed = _lab_source("seed_entities")
    assert seed.index("seed_langfuse_states(token, base)") < seed.index("seed_ai_lock_states(token, base, peer_id)")


def test_the_home_states_are_read_back_and_logged_only_when_switched_on():
    calls: list[tuple[str, str, dict | None]] = []
    answers = {
        ("GET", "/platform/config"): {"edition": "community"},
        ("GET", "/auth/me"): {"ctem_splash_seen": False},
        ("GET", "/settings/general-assistant"): {"agent_default_model": None},
        ("GET", "/assignments/runs/all?limit=500"): [
            {"id": "r1", "status": "pending", "trigger_context": {"ctem_vertical": "exposure"}},
            {"id": "r2", "status": "completed", "trigger_context": {"ctem_vertical": "exposure"}},
            {"id": "r3", "status": "pending", "trigger_context": {}},
        ],
        ("GET", "/ctem-metrics/briefing"): {"status": "no_model"},
        **{("GET", f"/ctem-metrics/stages/{s}/assessment"): {"status": "no_model"}
           for s in ("scoping", "discovery", "prioritization", "validation")},
    }

    def api(method, path, token=None, body=None):
        calls.append((method, path, body))
        return answers.get((method, path), {})

    names = ("LAB_EDITIONS", "LAB_DEFAULT_MODELS", "CTEM_LOCK_STAGES", "lab_switch", "seed_home_states",
             "seed_ctem_no_model_lock")
    base = "http://localhost:3100"
    assert _lab_namespace(*names, api=api)["seed_home_states"]("t", base) == ({}, {})
    assert calls == []

    switched = _lab_namespace(*names, api=api, env={"LAB_EDITION": "community", "LAB_DEFAULT_MODEL": "none"})
    seeded, skipped = switched["seed_home_states"]("t", base)
    assert skipped == {}
    assert seeded == {
        "CE splash": {"id": "community", "url": f"{base}/"},
        "model-less state": {"id": "no-default-model", "url": f"{base}/"},
        "CTEM no-model lock": {"id": "no_model", "url": f"{base}/"},
    }
    assert ("PUT", "/settings/general-assistant", {"agent_default_model": ""}) in calls
    # Only the active CTEM fan-out runs are deleted, so the GETs answer no_model, not generating.
    deleted = [path for method, path, _ in calls if method == "DELETE"]
    assert deleted == ["/assignments/runs/r1"]

    answers[("GET", "/platform/config")] = {"edition": "enterprise"}
    answers[("GET", "/settings/general-assistant")] = {"agent_default_model": "lab-model"}
    seeded, skipped = switched["seed_home_states"]("t", base)
    assert seeded == {}
    assert "not 'community'" in skipped["CE splash"] and "still set" in skipped["model-less state"]
    seed = _lab_source("seed_entities")
    assert seed.index("seed_home_states(token, base)") < seed.index("seeded.json")


def test_the_ai_lock_states_are_read_back_and_logged_only_when_switched_on():
    """``LAB_AI=off`` and ``LAB_PEER_GROUP_AI=off`` open the two ``AiLockedFooter`` reasons the others do not."""
    calls: list[tuple[str, str, str | None, dict | None]] = []
    answers: dict = {
        ("GET", "/platform/config"): {"ai_enabled": False},
        ("POST", "/groups"): {"id": "g1"},
        ("POST", "/auth/login"): {"access_token": "peer-token"},
        ("GET", "/auth/me"): {"ai_disabled_by_group": True},
    }

    def api(method, path, token=None, body=None):
        calls.append((method, path, token, body))
        return answers.get((method, path), {})

    names = ("LAB_AI_STATES", "LAB_PEER_GROUP_AI_STATES", "LAB_AI_OFF_GROUP", "lab_switch", "seed_ai_lock_states",
             "signed_in_url", "_created", "PEER_EMAIL", "PEER_PASSWORD")
    base = "http://localhost:3100"
    assert _lab_namespace(*names, api=api)["seed_ai_lock_states"]("t", base, "peer") == ({}, {})
    assert calls == []

    switched = _lab_namespace(*names, api=api, env={"LAB_AI": "off", "LAB_PEER_GROUP_AI": "off"})
    seeded, skipped = switched["seed_ai_lock_states"]("t", base, "peer")
    assert skipped == {}
    assert seeded["platform AI off"] == {"id": "ai-disabled", "url": f"{base}/"}
    assert seeded["group with AI off"] == {"id": "g1", "url": f"{base}/users-and-groups/groups/g1"}
    assert seeded["signed-in session (peer, group AI off)"]["url"] == f"{base}/login#token=peer-token"
    assert ("PUT", "/settings/platform", "t", {"ai_enabled": False}) in calls
    assert ("PUT", "/groups/g1", "t", {"ai_enabled": False}) in calls
    assert ("POST", "/groups/g1/members", "t", {"user_ids": ["peer"]}) in calls
    assert ("GET", "/auth/me", "peer-token", None) in calls
    # The group is written while the platform's AI is still on: the platform switch comes last.
    paths = [c[1] for c in calls]
    assert paths.index("/groups") < paths.index("/settings/platform")

    answers[("GET", "/platform/config")] = {"ai_enabled": True}
    answers[("GET", "/auth/me")] = {"ai_disabled_by_group": False}
    seeded, skipped = switched["seed_ai_lock_states"]("t", base, "peer")
    assert seeded == {}
    assert "ai_enabled false" in skipped["platform AI off"]
    assert "ai_disabled_by_group" in skipped["group with AI off"]
    assert "no peer user" in switched["seed_ai_lock_states"]("t", base, None)[1]["group with AI off"]

    seed = _lab_source("seed_entities")
    assert seed.index("seed_ai_lock_states(token, base, peer_id)") > seed.index("seed_fleet_runs(token, base)")
    assert seed.index("seed_ai_lock_states(token, base, peer_id)") < seed.index("seeded.json")


def test_the_copilot_lab_seeds_the_filigran_brand_deck_and_the_others_do_not():
    from app.services.template_container_seed import builtin_containers_for

    services = _services()
    slugs = {
        name: {c["slug"] for c in builtin_containers_for(services[name]["env"]["PLATFORM_MODE"])}
        for name in ("lab", "lab-dev", "lab-copilot")
    }
    assert "filigran-brand" in slugs["lab-copilot"]
    assert "filigran-brand" not in slugs["lab"] and "filigran-brand" not in slugs["lab-dev"]


def test_the_dev_mode_is_one_where_evaluation_holds():
    from app.core.feature_flags import FLAG_CONDITIONS

    assert "EVALUATION" in FLAG_CONDITIONS
    assert _services()["lab-dev"]["env"]["PLATFORM_MODE"] in ("copilot", "dev")


def test_the_lab_seeds_once_the_preview_answers():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "seed_entities" in functions
    main = ast.unparse(functions["main"])
    # seed_lab runs seed_entities on a fresh database, restore_seed on a kept one (#12253).
    assert main.index("wait_http(f'http://127.0.0.1:{WEB_PORT}/'") < main.index("seed_lab(kept, fingerprint)")
    assert "seed_entities()" in ast.unparse(functions["seed_lab"])
    seed = ast.unparse(functions["seed_entities"])
    assert "seeded.json" in seed and "except Exception" in seed


def test_lab_theme_sets_the_administrators_preference_before_the_rows():
    """``LAB_THEME`` goes through the preference the app paints from, never a forced attribute."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    theme = ast.unparse(functions["seed_theme"])
    assert "'LAB_THEME'" in theme and "'/settings/preferences'" in theme and "except Exception" in theme
    seed = ast.unparse(functions["seed_entities"])
    assert seed.index("seed_theme(token)") < seed.index("for entity, route, create in SEEDS")
    assignments = {t.id: node.value for node in tree.body if isinstance(node, ast.Assign) for t in node.targets
                   if isinstance(t, ast.Name)}
    assert ast.literal_eval(assignments["LAB_THEMES"]) == ("light", "dark")


def test_the_lab_seeds_one_fleet_with_an_orchestrator_and_a_handover_back():
    """A fresh lab answers ``GET /fleets`` with one fleet whose Graph tab renders."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assignments = {t.id: node.value for node in tree.body if isinstance(node, ast.Assign) for t in node.targets
                   if isinstance(t, ast.Name)}
    assert len(ast.literal_eval(assignments["FLEET_MEMBERS"])) == 3
    seeds = ast.unparse(next(node for node in tree.body if isinstance(node, ast.AnnAssign)
                             and getattr(node.target, "id", "") == "SEEDS"))
    assert "('fleet', '/fleets/{id}', _seed_fleet)" in seeds
    fleet = ast.unparse(functions["_seed_fleet"])
    for call in ("'/fleets'", "/members'", "'orchestrator_agent_id'", "'parent_member_id'", "'consult'", "'delegate'"):
        assert call in fleet, call


def test_the_lab_seeds_a_large_fleet_with_a_nested_parent_and_a_locked_fleet_for_the_peer():
    """A dense Graph tab (7 nodes, one nested level) and the locked state render on real rows."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    members = ast.literal_eval(next(node.value for node in tree.body if isinstance(node, ast.AnnAssign)
                                    and getattr(node.target, "id", "") == "LARGE_FLEET_MEMBERS"))
    assert len(members) + 1 >= 6
    parents = [parent for parent in members.values() if parent]
    assert parents and all(parent in members and members[parent] is None for parent in parents)
    # Parents before their children: the member rows are created in the dict's order.
    names = list(members)
    assert all(names.index(parent) < names.index(child) for child, parent in members.items() if parent)
    large = ast.unparse(functions["seed_large_fleet"])
    for call in ("'/fleets'", "/members'", "'orchestrator_agent_id'", "'parent_member_id'", "'large fleet'"):
        assert call in large, call
    locked = ast.unparse(functions["seed_locked_fleet"])
    for call in ("'shared_user_ids': [peer]", "'read_only'", "'locked fleet'"):
        assert call in locked, call
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_large_fleet(token, base)" in seed and "skipped['large fleet']" in seed
    assert seed.index("seed_peer(token, base)") < seed.index("seed_locked_fleet(") < seed.index("seeded.json")
    assert "skipped['locked fleet']" in seed


def test_the_lab_seeds_a_fleet_whose_first_documentation_stays_generating():
    """About shows its generating notice on a real row, with no fetch override (#10345)."""
    source = LAB.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    seeder = ast.unparse(functions["seed_doc_generating_fleet"])
    # The stub refuses the creation-time draft, so version 1 is never saved.
    for call in ("'/fleets'", "LAB_DOC_DRAFT_REFUSE", "/documentation'", "'generating'", "_hold_doc_generating"):
        assert call in seeder, call
    hold = ast.unparse(functions["_hold_doc_generating"])
    assert "'SET'" in hold and "'EX'" in hold and "LAB_DOC_GENERATING_REFRESH_SECONDS" in hold
    # Same marker as the backend, refreshed well inside its lifetime.
    from app.services import fleet_doc_on_create

    assert 'LAB_DOC_GENERATING_PREFIX = "fleet_doc:generating:"' in source
    assert fleet_doc_on_create._marker("x") == "fleet_doc:generating:x"
    assert f"LAB_DOC_GENERATING_TTL_SECONDS = {fleet_doc_on_create.GENERATING_TTL_SECONDS}" in source
    assert "LAB_DOC_GENERATING_REFRESH_SECONDS = 60" in source
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_doc_generating_fleet(token, base)" in seed
    assert "skipped['fleet documentation generating']" in seed


def test_the_lab_seeds_an_archived_fork_of_the_sample_fleet_found_again_on_a_restart():
    """The archived badge and chip render on a real row, with no hand PUT by the reviewer."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    archived = ast.unparse(functions["seed_archived_fleet"])
    for call in ("include_archived=true", "_fork(token, fleet, ARCHIVED_FORK)", "{'is_archived': True}",
                 "'archived fleet'", "/fleets/"):
        assert call in archived, call
    # Found by name first, so a restarted lab does not fork a second archived copy.
    assert archived.index("include_archived=true") < archived.index("_fork(")
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_archived_fleet(token, base, ids['fleet'])" in seed
    assert "skipped['archived fleet']" in seed


def test_the_lab_seeds_a_not_set_up_fleet_the_peer_reads_with_its_members_but_cannot_manage():
    """A managers-only action on a Not set up fleet renders for a reader on real rows (#9896)."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_peer_not_set_up_fleet"])
    for part in ("'shared_user_ids': [peer]", "'read_only'", "/agents/", "{member: 'delegate'}",
                 "'orchestrator_agent_id'", "'parent_member_id'", "'GET', '/fleets'", "can_manage",
                 "('administrator', token, True)", "('peer', peer_token, False)",
                 "'peer read-only not set up fleet'"):
        assert part in helper, part
    # No automation is created: the fleet reads Not set up.
    assert "/assignments" not in helper and "/automations" not in helper
    seed = ast.unparse(functions["seed_entities"])
    assert seed.index("seed_peer(token, base)") < seed.index("seed_peer_not_set_up_fleet(") < seed.index("seeded.json")
    assert "skipped['peer read-only not set up fleet']" in seed


def test_the_lab_seeds_a_fleet_whose_prerequisites_the_peer_cannot_read():
    """The About checklist's "Not shared with you" state renders for the peer on real rows (#10258)."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_prerequisites_unreadable_fleet"])
    for part in ("'/integrations'", "'/knowledge-bases'", "'integration_bindings'", "'knowledge_base_ids'",
                 "'shared_user_ids': [peer]", "'read_only'", "/documentation", "'live_prerequisites'",
                 "'unreadable'", "'can_fix'", "'fleet prerequisites unreadable for the peer'"):
        assert part in helper, part
    # Neither the integration nor the knowledge base is shared: only the fleet and its agents are.
    integration_body = helper[helper.index("'/integrations'"):helper.index("'/knowledge-bases'")]
    assert "shared_user_ids" not in integration_body
    seed = ast.unparse(functions["seed_entities"])
    assert seed.index("seed_peer(token, base)") < seed.index("seed_prerequisites_unreadable_fleet(") < seed.index("seeded.json")
    assert "skipped['fleet prerequisites unreadable for the peer']" in seed


def test_the_lab_seeds_manual_prerequisites_in_each_confirmation_state():
    """#10509: the About page's manual lines render confirmed, to confirm, expired and for a reader on real rows."""
    import importlib.util

    from app.services.fleet_about import CONFIRMATION_DAYS, MANUAL_LINES_KEPT, covered_by_configuration
    from app.services.fleet_docs import clean_structured

    spec = importlib.util.spec_from_file_location(
        "lab_seed_fleet_manual_prerequisites", LAB.parent / "seed_fleet_manual_prerequisites.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Each fleet's lines fit what live_prerequisites keeps, pass the documentation's validation and
    # name nothing the fleet's configuration reads (its agents and fleet names are the subjects).
    subjects = {
        f"Lab sample {name}{part}"
        for name in ("manual prerequisites", "manual prerequisites to confirm")
        for part in (" member", " orchestrator", "")
    }
    for index in (0, 1):
        lines = module.manual_lines(index)
        assert len(lines) == MANUAL_LINES_KEPT
        assert clean_structured({"prerequisites": lines})["prerequisites"] == lines
        assert not any(covered_by_configuration(line["label"], subjects) for line in lines)
    assert module.CONFIRMATIONS[0] < CONFIRMATION_DAYS < module.CONFIRMATIONS[1]
    script = (LAB.parent / "seed_fleet_manual_prerequisites.py").read_text(encoding="utf-8")
    # The product's own writers, never a hand-built JSON.
    assert "save_documentation(" in script and "set_prerequisite_confirmation(" in script
    assert "fleet.documentation =" not in script and "prerequisite_confirmations =" not in script

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_fleet_manual_prerequisites"])
    for part in ("'seed_fleet_manual_prerequisites.py'", "sys.executable", "env=seed_script_env()",
                 "stderr_summary(process.stderr)", "/documentation", "'live_prerequisites'", "'can_confirm'",
                 "('peer', peer_token, False)", "'fleet manual prerequisites'",
                 "'fleet manual prerequisites to confirm'"):
        assert part in helper, part
    assert "'shared_user_ids': [peer]" in helper and "'read_only'" in helper
    assert "token, read_only)" in ast.unparse(functions["_manual_fleet"])
    seed = ast.unparse(functions["seed_entities"])
    assert seed.index("seed_peer(token, base)") < seed.index("seed_fleet_manual_prerequisites(") < seed.index("seeded.json")
    assert "skipped['fleet manual prerequisites']" in seed


def test_the_lab_seeds_a_fleet_the_peer_manages_with_two_automations_off_to_enable():
    """The non-admin state of Enable fleet (Run as read-only) renders on real rows, no fetch override."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_peer_managed_fleet_enable"])
    for part in ("PEER_EMAIL", "'POST', '/fleets', peer_token", "'orchestrator_agent_id'", "'parent_member_id'",
                 "'POST', '/assignments', peer_token", "'enabled': False", "'schedule'", "'integration_event'",
                 "'provider': 'opencti'", "/enable", "{'dry_run': True}", "plan.get('changed') != len(automations)",
                 "plan.get('missing')", "'peer-managed fleet to enable'"):
        assert part in helper, part
    # The peer's own session: no administrator token, no other run-as user.
    assert "token," not in helper.replace("peer_token", "") and "run_as_user_id':" not in helper
    seed = ast.unparse(functions["seed_entities"])
    assert seed.index("seed_peer(token, base)") < seed.index("seed_peer_managed_fleet_enable(")
    assert seed.index("seed_peer_managed_fleet_enable(") < seed.index("seed_ai_lock_states(")
    assert "skipped['peer-managed fleet to enable']" in seed


def test_the_lab_seeds_a_fleet_whose_enable_dry_run_misses_a_module_for_a_lab_only_user():
    """The missing-modules state of Enable fleet renders on real rows, no fetch override (#9970)."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_fleet_enable_missing_module"])
    for part in ("'POST', '/users'", "NO_CRQ_EMAIL", "'access_restricted': True", "'shared_group_ids': [group]",
                 "/members", "u.get('role') != 'admin'", "/enable", "'dry_run': True", "'run_as_user_id': user",
                 "plan.get('missing')", "'fleet enable missing module'", "(run as {NO_CRQ_EMAIL})"):
        assert part in helper, part
    # A new lab-only user: the peer's integrations, used by other reviews, are untouched.
    assert "PEER_EMAIL" not in helper
    seed = ast.unparse(functions["seed_entities"])
    # After every seed that creates a user, before the AI switches that refuse Enable fleet.
    assert seed.index("seed_peer(token, base)") < seed.index("seed_fleet_enable_missing_module(")
    assert seed.index("seed_analytics_traces(") < seed.index("seed_fleet_enable_missing_module(")
    assert seed.index("seed_fleet_enable_missing_module(") < seed.index("seed_ai_lock_states(")
    assert "skipped['fleet enable missing module']" in seed


def test_the_lab_connects_four_members_to_the_sample_fleet_one_with_an_override():
    """The Members view lists connected rows, inherited and overridden, and its bulk bar can select them."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assignments = {t.id: node.value for node in tree.body if isinstance(node, ast.Assign) for t in node.targets
                   if isinstance(t, ast.Name)}
    assert len(ast.literal_eval(assignments["CONNECTED_MEMBERS"])) >= 4
    override = ast.literal_eval(assignments["CONNECTED_OVERRIDE"])
    assert override["collaboration_instruction"] and override["context_inclusion"] in ("none", "summary", "full")
    helper = ast.unparse(functions["seed_connected_members"])
    for part in ("/members'", "'role': 'connected'", "CONNECTED_OVERRIDE if index == 0",
                 "'seeded connected members: ", "/fleets/{fleet}/members'"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_connected_members(token, base, ids['fleet'])" in seed
    assert "skipped['connected members'] = 'no fleet row'" in seed
    assert seed.index("for entity, route, create in SEEDS") < seed.index("seed_connected_members(")


def test_the_lab_seeds_two_company_presets_and_a_fleet_the_peer_reads_but_cannot_edit():
    """5 presets in the picker and the Members view's reader state on real rows (#9428)."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    presets = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign)
                   and getattr(node.target, "id", "") == "FLEET_MEMBER_PRESETS")
    names = [ast.unparse(item.elts[0]) for item in presets.elts]
    values = [ast.literal_eval(item.elts[1]) for item in presets.elts]
    assert len(values) == 2
    # One preset sets the three member settings with a list scope.
    assert any(set(v) == {"context_inclusion", "collaboration_instruction", "organization_scope"}
               and v["organization_scope"]["mode"] == "list" for v in values)
    # One name over 100 characters, under the 120 a preset name allows (f-string: SEED_NAME is 10).
    lengths = [len("".join(part.value for part in item.elts[0].values if isinstance(part, ast.Constant))) + 10
               for item in presets.elts if isinstance(item.elts[0], ast.JoinedStr)]
    assert any(100 < length <= 120 for length in lengths), (names, lengths)
    helper = ast.unparse(functions["seed_fleet_member_presets"])
    for part in ("'/fleet-member-presets'", "organizations.json", "'mode': 'list'", "'seeded fleet member presets: "):
        assert part in helper, part
    reader = ast.unparse(functions["seed_reader_fleet"])
    for part in ("'shared_user_ids': [peer]", "'read_only'", "/agents/", "/fleets/", "/fleets/{fleet}/members'",
                 "'fleet read-only for the peer'"):
        assert part in reader, part
    seed = ast.unparse(functions["seed_entities"])
    assert seed.index("seed_organizations(base)") < seed.index("seed_fleet_member_presets(token, base)")
    assert "skipped['fleet member presets']" in seed
    assert seed.index("seed_peer(token, base)") < seed.index("seed_reader_fleet(") < seed.index("seeded.json")
    assert seed.index("seed_connected_members(") < seed.index("seed_reader_fleet(")
    assert "skipped['fleet read-only for the peer']" in seed


def test_the_lab_exports_decks_with_the_images_chromium_and_its_own_frontend():
    """Presentation export in the lab: Chromium baked in python-dev, the render page on the lab's Vite."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    env = ast.unparse(functions["backend_env"])
    assert "'PRESENTATION_RENDER_BASE_URL': f'http://127.0.0.1:{WEB_PORT}'" in env
    assert "PLAYWRIGHT_BROWSERS_PATH" in ast.unparse(functions["has_renderer"])
    assert "has_renderer()" in ast.unparse(functions["main"])
    for name in ("lab", "lab-dev", "lab-copilot"):
        assert _services()[name]["image"] == "python-dev", name
    dockerfile = (ROOT / "backend" / "sandbox" / "Dockerfile.sandbox.dev").read_text(encoding="utf-8")
    python_dev = dockerfile.split(" AS python-dev", 1)[1].split(" AS node-builder", 1)[0]
    assert "ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in python_dev
    assert "playwright install --with-deps --only-shell chromium" in python_dev
    # Installed as root, before the switch to the user that launches it.
    assert python_dev.index("playwright install") < python_dev.index("USER sandboxuser")
    workflow = (ROOT / ".github" / "workflows" / "sandbox-images.yml").read_text(encoding="utf-8")
    step = workflow.split("- name: Assert headless Chromium prints a PDF offline", 1)[1].split("- name:", 1)[0]
    assert "--network none" in step and "page.pdf()" in step


def test_the_lab_seeds_a_non_admin_peer_and_rows_shared_between_the_two_owners():
    """Two accounts, so "Shared by" and Read-only render on real rows with no fetch override."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assignments = {t.id: node.value for node in tree.body if isinstance(node, ast.Assign) for t in node.targets
                   if isinstance(t, ast.Name)}
    assert ast.literal_eval(assignments["PEER_EMAIL"]) == "lab-peer@filigran.io"
    peer = ast.unparse(functions["seed_peer"])
    for call in ("'/users'", "'role': 'user'", "'/auth/login'", "'/auth/me'", "'/work-items'", "'/shared-state'",
                 "except Exception"):
        assert call in peer, call
    # One workspace each way: the administrator reads the peer's, the peer reads the administrator's.
    assert "_shared_workspace(peer_token, admin" in peer and "_shared_workspace(token, peer" in peer
    share = ast.unparse(functions["_share_read_only"])
    assert "'shared_user_ids'" in share and "'read_only'" in share
    seed = ast.unparse(functions["seed_entities"])
    assert seed.index("for entity, route, create in SEEDS") < seed.index("seed_peer(token, base)") < seed.index(
        "seeded.json")


def test_the_lab_totp_code_matches_the_rfc_6238_vectors_and_the_backend():
    """The lab's stdlib TOTP answers the RFC 6238 SHA1 vectors (6 digits) and the backend's own pyotp."""
    ns = _lab_namespace("totp_code")
    ns.update(base64=__import__("base64"), hmac=__import__("hmac"), hashlib=__import__("hashlib"),
              struct=__import__("struct"), time=__import__("time"))
    # The RFC 6238 SHA1 seed, encoded here: a base32 literal reads as a key to the secret scan.
    secret = __import__("base64").b32encode(b"12345678901234567890").decode()
    for at, code in ((59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")):
        assert ns["totp_code"](secret, at) == code, at
    pyotp = pytest.importorskip("pyotp")
    fresh = pyotp.random_base32()
    assert ns["totp_code"](fresh, 1_700_000_000) == pyotp.TOTP(fresh).at(1_700_000_000)


def test_the_lab_seeds_a_non_admin_user_with_two_factor_enrolled_and_never_logs_the_secret():
    """User detail renders the Two-factor card enabled on a real row (#10159), no fetch override."""
    calls: list[tuple[str, str, str | None, dict | None]] = []
    answers: dict = {
        ("POST", "/users"): {"id": "u1"},
        ("POST", "/auth/login"): {"access_token": "mfa-token"},
        ("POST", "/auth/mfa/setup"): {"secret": __import__("base64").b32encode(b"12345678901234567890").decode()},
        ("POST", "/auth/mfa/enable"): {"recovery_codes": ["r1"]},
        ("GET", "/users/u1"): {"id": "u1", "mfa_enabled": True},
    }

    def api(method, path, token=None, body=None):
        calls.append((method, path, token, body))
        return answers.get((method, path), {})

    base = "http://localhost:3100"
    ns = _lab_namespace("seed_mfa_user", "totp_code", "_created", "MFA_EMAIL", "MFA_PASSWORD", "MFA_NAME", api=api)
    ns.update(base64=__import__("base64"), hmac=__import__("hmac"), hashlib=__import__("hashlib"),
              struct=__import__("struct"), time=__import__("time"))
    assert ns["seed_mfa_user"]("admin-token", base) == {"two-factor user": {"id": "u1", "url": f"{base}/users-and-groups/users/u1"}}
    assert [(c[0], c[1], c[2]) for c in calls] == [
        ("POST", "/users", "admin-token"),
        ("POST", "/auth/login", None),
        ("POST", "/auth/mfa/setup", "mfa-token"),
        ("POST", "/auth/mfa/enable", "mfa-token"),
        ("GET", "/users/u1", "admin-token"),
    ]
    assert calls[0][3]["role"] == "user" and calls[0][3]["email"] == "lab-mfa@filigran.io"
    code = calls[3][3]["code"]
    assert len(code) == 6 and code.isdigit()
    assert not ns["logged"]
    # The enrollment not read back as enabled refuses the row: the caller skips it.
    answers[("GET", "/users/u1")] = {"id": "u1", "mfa_enabled": False}
    with pytest.raises(RuntimeError):
        ns["seed_mfa_user"]("admin-token", base)
    source = _lab_source("seed_mfa_user")
    assert "log(" not in source and "print(" not in source
    seed = _lab_source("seed_entities")
    assert seed.index("seed_peer(token, base)") < seed.index("seed_mfa_user(token, base)") < seed.index("seeded.json")
    assert "skipped['two-factor user']" in seed


def test_the_lab_seeds_a_budget_ceiling_the_peer_cannot_exceed_and_logs_its_fleet():
    """The blocked-limit states of the Budget tab render for the peer on real rows, with no query override."""
    calls: list[tuple[str, str, str | None, dict | None]] = []
    answers: dict = {
        ("GET", "/agents/budget-policy"): {"can_exceed_ceiling": False, "ceilings": {"max_cost_usd": 5.0}},
        ("POST", "/fleets"): {"id": "f1"},
    }

    def api(method, path, token=None, body=None):
        calls.append((method, path, token, body))
        return answers.get((method, path), {})

    base = "http://localhost:3100"
    ns = _lab_namespace("seed_budget_ceiling", "_created", "SEED_NAME", "LAB_BUDGET_CEILING_USD", api=api)
    expected = {"budget ceiling": {"id": "f1", "url": f"{base}/fleets/f1"}}
    assert ns["seed_budget_ceiling"]("peer-token", base) == expected
    # Read and created as the peer: the fleet is the peer's to edit, the ceiling the peer's to meet.
    assert [(c[0], c[1], c[2]) for c in calls] == [
        ("GET", "/agents/budget-policy", "peer-token"),
        ("POST", "/fleets", "peer-token"),
    ]
    assert float(ns["LAB_BUDGET_CEILING_USD"]) > 0

    # A peer allowed past the ceiling, or no ceiling at all, refuses the row before any fleet is created.
    for policy in (
        {"can_exceed_ceiling": True, "ceilings": {"max_cost_usd": 5.0}},
        {"can_exceed_ceiling": False, "ceilings": {"max_cost_usd": None}},
    ):
        calls.clear()
        answers[("GET", "/agents/budget-policy")] = policy
        with pytest.raises(RuntimeError):
            ns["seed_budget_ceiling"]("peer-token", base)
        assert ("POST", "/fleets") not in [(c[0], c[1]) for c in calls]

    env = _lab_source("backend_env")
    assert "'AGENT_LOOP_MAX_COST_USD': os.environ.get('AGENT_LOOP_MAX_COST_USD', LAB_BUDGET_CEILING_USD)" in env
    peer = _lab_source("seed_peer")
    assert "seed_budget_ceiling(peer_token, base)" in peer and "skipped['budget ceiling']" in peer


def test_the_lab_turns_the_decisions_read_switch_on_and_seeds_one_open_decision():
    calls: list = []
    answers: dict = {
        ("POST", "/work-items"): {"id": "w1"},
        ("GET", "/decisions/waiting?limit=200"): {"enabled": True, "items": [{"id": "work_item:w1"}]},
    }

    def api(method, path, token=None, body=None):
        calls.append((method, path, token, body))
        return answers.get((method, path), {})

    base = "http://localhost:3100"
    ns = _lab_namespace("seed_open_decision", "_created", "SEED_NAME", api=api)
    assert ns["seed_open_decision"]("t", base) == {
        "open decision": {"id": "work_item:w1", "url": f"{base}/tasks/work-items/w1"}
    }
    body = calls[0][3]
    # A complete ask, as the API requires of needs_human; no owner agent, so no inbox claims it.
    assert body["needs_human"] is True and "owner_agent_id" not in body
    ask = body["ask"]
    assert ask["question"] and len(ask["options"]) >= 2
    assert ask["recommendation"] in {option["key"] for option in ask["options"]}

    # The switch off, or the row missing, raises: the seed never reports a row the inbox does not show.
    for waiting in (
        {"enabled": False, "reason": "DECISIONS_READ_ENABLED is off", "items": []},
        {"enabled": True, "items": []},
    ):
        answers[("GET", "/decisions/waiting?limit=200")] = waiting
        with pytest.raises(RuntimeError):
            ns["seed_open_decision"]("t", base)

    env = _lab_source("backend_env")
    assert "'DECISIONS_READ_ENABLED': os.environ.get('DECISIONS_READ_ENABLED', 'true')" in env
    seed = _lab_source("seed_entities")
    assert "seed_open_decision(token, base)" in seed and "skipped['open decision']" in seed


def test_the_lab_seeds_the_friction_runs_through_the_backends_interpreter_and_logs_their_urls():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_frictions"])
    for part in ("'/work-items'", "'seed_frictions.py'", "sys.executable", "env=seed_script_env()", "/operations/executions/",
                 "'run with frictions'", "'run with a live friction ledger'", "stderr_summary(process.stderr)"):
        assert part in helper, part
    # Started by path, the script has .xtm-one/lab on sys.path, not its cwd: ``app`` comes from PYTHONPATH.
    env = ast.unparse(functions["seed_script_env"])
    assert "backend_env()" in env and "env['PYTHONPATH']" in env and "CHECKOUT / 'backend'" in env
    seed = ast.unparse(functions["seed_entities"])
    assert seed.index("for entity, route, create in SEEDS") < seed.index("seed_frictions(token, ids['agent'], base)")
    assert seed.index("seed_frictions(token") < seed.index("seeded.json")
    # Each seeded row is logged as ``seeded <entity>: <url>``: "seeded run with frictions: .../runs/<id>".
    assert "log_seeded(seeded, skipped)" in seed
    assert "log(f\"seeded {entity}: {row['url']}\")" in LAB.read_text(encoding="utf-8")


def _lab_function(name: str):
    """One standard-library function of lab.py, without running the module (it starts the stores)."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace: dict = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace[name]


def test_the_lab_names_one_signed_in_url_per_seeded_account():
    """One browser ``navigate`` lands signed in: the SSO callback fragment ``Login.tsx`` consumes and scrubs."""
    signed_in = _lab_function("signed_in_url")
    assert signed_in("http://localhost:3100", "a.b.c") == "http://localhost:3100/login#token=a.b.c"
    login = (ROOT / "frontend" / "src" / "pages" / "Login.tsx")
    if login.is_file():
        page = login.read_text(encoding="utf-8")
        assert "function consumeSsoCallback" in page and '"token"' in page
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    seed = ast.unparse(functions["seed_entities"])
    assert "seeded['signed-in session (administrator)']" in seed and "signed_in_url(base, token)" in seed
    assert seed.index("signed_in_url(base, token)") < seed.index("seeded.json")
    peer = ast.unparse(functions["seed_peer"])
    assert "seeded['signed-in session (peer)']" in peer and "signed_in_url(base, peer_token)" in peer


def test_lab_logs_each_signed_in_session_at_sign_in_before_the_long_seed():
    """#89: the session line is in the start log as soon as the sign-in answers, not after the whole seed."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["log_session"])
    # The same "seeded <label>: <url>" form as log_seeded, so the service's session_log prefix matches it.
    assert "log(f\"seeded {label}: {row['url']}\")" in helper
    seed = ast.unparse(functions["seed_entities"])
    logged = seed.index("log_session('signed-in session (administrator)'")
    assert seed.index("signed_in_url(base, token)") < logged < seed.index("for entity, route, create in SEEDS")
    peer = ast.unparse(functions["seed_peer"])
    assert peer.index("signed_in_url(base, peer_token)") < peer.index("log_session('signed-in session (peer)'")
    assert peer.index("log_session('signed-in session (peer)'") < peer.index("rows: list")


def test_lab_theme_covers_the_peer_before_its_signed_in_link():
    """``LAB_THEME`` is a per-user preference: the peer gets it too, or its session opens dark."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    peer = ast.unparse(functions["seed_peer"])
    assert "seed_theme(peer_token, 'peer')" in peer
    assert peer.index("seed_theme(peer_token, 'peer')") < peer.index("signed_in_url(base, peer_token)")
    theme = ast.unparse(functions["seed_theme"])
    assert "who: str='administrator'" in theme and "the {who}'s preference" in theme


def test_a_failed_backend_startup_stops_the_lab_at_once():
    """A 503 readiness answer naming ``startup_error`` is final: the lab stops instead of waiting 600 s."""
    failure = _lab_function("startup_failure")
    namespace = failure.__globals__
    import json as json_module

    namespace["json"] = json_module
    assert failure(b'{"status": "starting", "startup_error": "ProgrammingError: relation x"}') == (
        "ProgrammingError: relation x"
    )
    assert failure(b'{"status": "starting", "startup_error": null}') == ""
    assert failure(b"<html>bad gateway</html>") == ""
    assert failure(b'["not", "an", "object"]') == ""
    wait = ast.unparse(next(
        n for n in ast.parse(LAB.read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef) and n.name == "wait_http"
    ))
    assert "startup_failure(body)" in wait and "startup failed" in wait


def test_a_failed_seed_script_is_logged_by_its_exception_line():
    summary = _lab_function("stderr_summary")
    traceback = "\n".join([
        "Traceback (most recent call last):",
        *[f'  File "/tmp/frame_{i}.py", line {i}, in step_{i}' for i in range(40)],
        '  File "/tmp/workspace/xtm-one/.xtm-one/lab/seed_frictions.py", line 34, in seed',
        "    import app.models",
        "ModuleNotFoundError: No module named 'app'",
    ])
    text = summary(traceback)
    assert text.startswith("ModuleNotFoundError: No module named 'app'")
    assert "seed_frictions.py" in text and len(text) <= 520
    assert summary("") == "no output"
    assert summary("RuntimeError: boom\n") == "RuntimeError: boom"


def _seed_frictions_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_frictions", LAB.parent / "seed_frictions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_friction_seed_writes_a_settled_run_of_four_rows_and_a_live_run(monkeypatch):
    """Four rows on the settled run (one undisposed, so ``frictions_undisposed`` 1), a live ledger beside it."""
    import asyncio
    import uuid

    import app.core.database as database
    from app.services import frictions

    runs: list = []
    rows: dict[str, dict] = {}
    settled_runs: list = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add_all(self, items):
            for item in items:
                item.id = uuid.uuid4()
                runs.append(item)

        async def commit(self):
            return None

    class Engine:
        async def dispose(self):
            return None

    async def record_friction(scope, *, kind, tool="", evidence=""):
        key = f"{scope.run_id}|{kind}|{tool}|{evidence}"
        row = rows.setdefault(key, {"id": str(uuid.uuid4()), "run_id": scope.run_id, "kind": kind, "tool": tool,
                                    "evidence": evidence, "count": 0, "class": "unclassified", "disposition": None})
        row["count"] += 1
        return dict(row), row["count"] == 1

    async def dispose_friction(friction_id, *, agent_id, user_id, friction_class, disposition, ref=""):
        row = next(r for r in rows.values() if r["id"] == friction_id)
        row.update({"class": friction_class, "disposition": {"kind": disposition, "ref": ref or None}})
        return dict(row)

    async def settle_run_frictions(run):
        run.frictions_undisposed = sum(1 for r in rows.values() if r["run_id"] == run.id and not r["disposition"])
        settled_runs.append(run)
        return run.frictions_undisposed

    monkeypatch.setattr(database, "async_session", Session)
    monkeypatch.setattr(database, "engine", Engine())
    monkeypatch.setattr(frictions, "record_friction", record_friction)
    monkeypatch.setattr(frictions, "dispose_friction", dispose_friction)
    monkeypatch.setattr(frictions, "settle_run_frictions", settle_run_frictions)

    agent, user, item = (str(uuid.uuid4()) for _ in range(3))
    ids = asyncio.run(_seed_frictions_module().seed(agent, user, item))

    settled, live = (next(r for r in runs if str(r.id) == ids[key]) for key in ("settled", "live"))
    assert settled.status == "completed" and live.status == "running"
    assert settled_runs == [settled] and settled.frictions_undisposed == 1
    assert getattr(live, "frictions_undisposed", None) is None
    ledger = [r for r in rows.values() if r["run_id"] == settled.id]
    assert len(ledger) == 4
    by_kind = {r["kind"]: r for r in ledger}
    assert by_kind["tool_error"]["count"] == 3 and by_kind["tool_error"]["class"] == "harness"
    assert by_kind["tool_error"]["disposition"] == {"kind": "work_item", "ref": item}
    assert by_kind["sandbox_failure"]["disposition"] is None and by_kind["sandbox_failure"]["class"] == "unclassified"
    assert by_kind["approval_pause"]["disposition"] == {"kind": "accepted", "ref": None}
    assert by_kind["manual"]["tool"] == "" and by_kind["manual"]["evidence"] == ""
    live_ledger = [r for r in rows.values() if r["run_id"] == live.id]
    assert len(live_ledger) == 2 and sum(1 for r in live_ledger if not r["disposition"]) == 1


def _seed_builtin_fleet_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_builtin_fleet", LAB.parent / "seed_builtin_fleet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_lab_seeds_a_builtin_fleet_through_the_platforms_own_seed_and_logs_its_url():
    """The Built-in badge renders on a real ``is_builtin`` fleet, written by ``seed_builtin_fleets``."""
    from app.agents.fleet_seed import fleets_for_mode, resolve_parent

    module = _seed_builtin_fleet_module()
    entry = module.lab_entry(["a-orchestrator", "b-member", "c-member"])
    assert entry["slug"] == module.LAB_FLEET_SLUG and entry["orchestrator"] == "a-orchestrator"
    assert [m["agent"] for m in entry["members"]] == ["a-orchestrator", "b-member", "c-member"]
    assert all(m["parent"] == "a-orchestrator" for m in entry["members"][1:])
    assert [m.get("exposed") for m in entry["members"][1:]] == [True, False]
    # Seeded in the lab's modes by the boot seed's own filter, never in copilot mode.
    assert fleets_for_mode("xtm_one", [entry]) == [entry] == fleets_for_mode("dev", [entry])
    assert fleets_for_mode("copilot", [entry]) == []
    parents = {m["agent"]: m.get("parent") for m in entry["members"]}
    assert resolve_parent("c-member", parents, {"a-orchestrator", "c-member"}) == "a-orchestrator"

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_builtin_fleet"])
    for part in ("'seed_builtin_fleet.py'", "sys.executable", "env=seed_script_env()", "'PLATFORM_MODE'",
                 "'built-in fleet'", "/fleets/", "stderr_summary(process.stderr)"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_builtin_fleet(base)" in seed and "skipped['built-in fleet']" in seed
    assert seed.index("seed_builtin_fleet(base)") < seed.index("seeded.json")
    script = ast.unparse(ast.parse((LAB.parent / "seed_builtin_fleet.py").read_text(encoding="utf-8")))
    assert "seed_builtin_fleets(db, platform_mode=platform_mode, fleets=[*BUILTIN_FLEETS, lab_entry(" in script


def _seed_fork_source_changes_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "lab_seed_fork_source_changes", LAB.parent / "seed_fork_source_changes.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_lab_seeds_three_forks_whose_source_changed_and_logs_only_those_read_back_changed(tmp_path):
    """The fork source-changes notice renders on real rows: built-in, custom and older forks."""
    import types

    module = _seed_fork_source_changes_module()
    assert module.changed_role_label("Member 1") == "Member 1 (changed after the fork)"
    assert len(module.changed_role_label("x" * 100)) == 100
    kept = {"fleet_id": "f", "fingerprint": "abc", "structure": {"members": []}}
    assert module.without_structure(kept) == {"fleet_id": "f", "fingerprint": "abc"}
    assert "structure" in kept

    calls: list[tuple[str, str, dict | None]] = []
    diff = {"custom": True, "built-in": True, "older": False}
    ids = {"Lab sample fork of the fleet": "custom", "Lab sample older fork of the fleet": "older",
           "Lab sample fork of the built-in fleet": "built-in"}

    def api(method, path, token=None, body=None):
        calls.append((method, path, body))
        if path.endswith("/fork"):
            return {"fleet": {"id": ids[body["name"]]}}
        if path == "/fleets/sample/members":
            return [
                {"id": "m0", "role": "member", "is_orchestrator": True},
                {"id": "m1", "role": "member", "is_orchestrator": False},
                {"id": "c1", "role": "connected"}, {"id": "c2", "role": "connected"},
            ]
        if path.endswith("/source-changes"):
            fork = path.split("/")[2]
            return {"changed": True, "diff_available": diff[fork]}
        return {}

    ran: list[list[str]] = []

    def run(argv, **_):
        ran.append(argv)
        return types.SimpleNamespace(returncode=0, stderr="")

    namespace = _lab_namespace(
        "_created", "_fork", "seed_fork_source_changes", "FORK_SOURCE_CHANGES", "SEED_NAME",
        api=api, subprocess=types.SimpleNamespace(run=run), sys=types.SimpleNamespace(executable="python"),
        ROOT=tmp_path, CHECKOUT=tmp_path, seed_script_env=dict, stderr_summary=str,
    )
    seeded, skipped = namespace["seed_fork_source_changes"]("tok", "http://lab", "sample", "builtin")
    assert skipped == {}
    assert {k: v["url"] for k, v in seeded.items()} == {
        f"fork with source changes ({kind})": f"http://lab/fleets/{kind}" for kind in ("built-in", "custom", "older")
    }
    # The custom source moves through the API: a member relabelled, a connected agent changed, one removed.
    assert ("PATCH", "/fleets/sample/members/m1", {"role_label": "Lead analyst"}) in calls
    assert ("PATCH", "/fleets/sample/members/c1", {"collaboration_instruction": "Answer the fleet in three bullets."}) in calls
    assert ("DELETE", "/fleets/sample/members/c2", None) in calls
    assert calls.index(("DELETE", "/fleets/sample/members/c2", None)) > max(
        i for i, c in enumerate(calls) if c[1] == "/fleets/sample/fork"
    )
    # The writes the API refuses run on the lab's database, the built-in source and the older fork named.
    assert len(ran) == 1 and ran[0][1].endswith("seed_fork_source_changes.py") and ran[0][2:4] == ["builtin", "older"]

    # A fork whose read-back does not match its kind is skipped, never logged as seeded.
    diff["older"] = True
    calls.clear()
    seeded, skipped = namespace["seed_fork_source_changes"]("tok", "http://lab", "sample", None)
    assert set(seeded) == {"fork with source changes (custom)"}
    assert skipped["fork with source changes (built-in)"] == "no built-in fleet row"
    assert "source-changes answered" in skipped["fork with source changes (older)"]

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_fork_source_changes(token, base, ids['fleet'], builtin)" in seed
    assert seed.index("seed_builtin_fleet(base)") < seed.index("seed_fork_source_changes(")
    assert seed.index("seed_connected_members(") < seed.index("seed_fork_source_changes(") < seed.index("seeded.json")
    script = ast.unparse(ast.parse((LAB.parent / "seed_fork_source_changes.py").read_text(encoding="utf-8")))
    assert "source.is_builtin" in script and "fork.forked_from = without_structure(fork.forked_from)" in script


def _seed_handovers_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_handovers", LAB.parent / "seed_handovers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_handover_seed_plans_live_recent_and_old_rows_along_configured_edges():
    """Five delegations: running, pending, completed 5 min ago, failed 10 min ago, completed hours ago."""
    from datetime import UTC, datetime, timedelta

    module = _seed_handovers_module()
    names = {name for edge in module.HANDOVERS for name in edge[:2]}
    agents = {name: f"id-{name}" for name in names}
    now = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
    rows = module.plan(agents, now)
    assert [r["status"] for r in rows] == ["running", "pending", "completed", "failed", "completed"]
    edges = {(f"id-{s}", f"id-{t}") for s, t, _ in module.HANDOVERS}
    assert all((r["parent_agent_id"], r["target_agent_id"]) in edges for r in rows)
    window = timedelta(minutes=15)
    live = [r for r in rows if r["status"] in ("running", "pending") or now - r["completed_at"] < window]
    old = [r for r in rows if r not in live]
    assert len(live) == 4 and len(old) == 1 and now - old[0]["completed_at"] >= timedelta(hours=1)
    assert [now - r["completed_at"] for r in rows[2:4]] == [timedelta(minutes=5), timedelta(minutes=10)]
    assert {r["type"] for r in rows} <= {"consult", "transfer", "background_task"}
    # One live row inside the seeded fleet (its orchestrator and members).
    assert rows[0]["parent_agent_id"] == "id-Orchestrator" and rows[0]["status"] == "running"
    assert all(r["description"].startswith("Lab sample: ") for r in rows)

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_handovers"])
    for part in ("'seed_handovers.py'", "sys.executable", "env=seed_script_env()", "/flows/map?vis=all",
                 "'handovers (", "stderr_summary(process.stderr)", "Triage=triage"):
        assert part in helper, part
    assert "LAB_AGENTS.update(" in ast.unparse(functions["_seed_fleet"])
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_handovers(token, base)" in seed and "skipped['handovers']" in seed
    assert seed.index("seed_handovers(token, base)") < seed.index("seeded.json")


def _seed_organizations_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_organizations", LAB.parent / "seed_organizations.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_organizations_seed_plans_a_primary_and_three_tracked_rows():
    """#9389: a long name, an archived one and one with monitoring on, every value the model accepts."""
    from app.models.organization import (
        ORGANIZATION_CRITICALITIES,
        ORGANIZATION_MONITORING,
        ORGANIZATION_RELATIONSHIPS,
    )

    module = _seed_organizations_module()
    assert module.PRIMARY_NAME and module.PRIMARY_NAME != "Primary"
    rows = module.TRACKED
    assert len(rows) == 3 and len({name for name, *_ in rows}) == 3
    assert all(r[1] in ORGANIZATION_RELATIONSHIPS and r[2] in ORGANIZATION_CRITICALITIES for r in rows)
    assert all(r[3] in ORGANIZATION_MONITORING for r in rows)
    assert any(len(r[0]) > 80 for r in rows) and all(len(r[0]) <= 200 for r in rows)
    assert [r[4] for r in rows].count(True) == 1
    assert [r[3] for r in rows].count("on") == 1
    # The archived row is never the monitored one: monitoring is on an organization the fleets cover.
    assert not any(r[3] == "on" and r[4] for r in rows)
    script = ast.unparse(ast.parse((LAB.parent / "seed_organizations.py").read_text(encoding="utf-8")))
    for part in ("orgs.ensure_primary(", "orgs.create_tracked(", "orgs.archive(row)", "row.monitoring = monitoring"):
        assert part in script, part

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_organizations"])
    for part in ("'seed_organizations.py'", "sys.executable", "env=seed_script_env()", "/settings/organization",
                 "organizations (primary and ", "stderr_summary(process.stderr)"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_organizations(base)" in seed and "skipped['organizations']" in seed
    assert seed.index("seed_organizations(base)") < seed.index("seeded.json")


def _seed_tracked_ai_draft_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_tracked_ai_draft", LAB.parent / "seed_tracked_ai_draft.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_tracked_ai_draft_seed_covers_every_review_row_state():
    """#9761: four pending rows (high, medium, low, a long name), one conflict, a failed batch of two."""
    from app.models.organization import ORGANIZATION_CRITICALITIES, ORGANIZATION_RELATIONSHIPS
    from app.services.organization_tracked_ai_draft import CONFIDENCES, MAX_NAMES, NAME_MAX_CHARS, Row

    module = _seed_tracked_ai_draft_module()
    rows = module.rows("Lab sample monitored partner", "00000000-0000-4000-8000-000000000001")
    assert len(rows) == 7 <= MAX_NAMES and len({r["name"] for r in rows}) == 7
    assert set(rows[0]) == set(Row(name="x", relationship="provider").as_dict())
    by_status: dict[str, list[dict]] = {}
    for row in rows:
        by_status.setdefault(row["status"], []).append(row)
    pending, conflict, error = by_status["pending"], by_status["conflict"], by_status["error"]
    assert len(pending) == 4 and len(conflict) == 1 and len(error) == 2
    # Every pending row is one store_tracked_draft accepts.
    assert all(r["relationship"] in ORGANIZATION_RELATIONSHIPS for r in pending)
    assert all(r["criticality"] in ORGANIZATION_CRITICALITIES for r in pending)
    assert {r["confidence"] for r in pending} == set(CONFIDENCES)
    assert any(len(r["name"]) > 80 for r in pending) and all(len(r["name"]) <= NAME_MAX_CHARS for r in rows)
    # Two OpenCTI matches with the exact one preset, and rows with no match.
    two = [r for r in pending if len(r["opencti_matches"]) == 2]
    assert len(two) == 1 and two[0]["opencti_organization_id"] == two[0]["opencti_matches"][0]["id"]
    assert two[0]["opencti_matches"][0]["name"] == two[0]["name"]
    assert sum(1 for r in pending if not r["opencti_matches"] and not r["opencti_organization_id"]) >= 1
    # The matches are fixture identities the lab's OpenCTI stub reads back.
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_asgi_fixture", LAB.parent / "lab_asgi.py")
    source = spec.loader.get_source("lab_asgi_fixture")
    for row in pending:
        for match in row["opencti_matches"]:
            assert match["id"] in source and match["name"] in source, match
    assert conflict[0]["already_tracked_id"] and conflict[0]["name"] == "Lab sample monitored partner"
    assert all(r["error"] == module.BATCH_ERROR for r in error)

    script = ast.unparse(ast.parse((LAB.parent / "seed_tracked_ai_draft.py").read_text(encoding="utf-8")))
    assert "store_tracked_draft(db" in script and "OrganizationContextProposal.status == 'pending'" in script
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_tracked_ai_draft"])
    for part in ("'seed_tracked_ai_draft.py'", "sys.executable", "env=seed_script_env()", "'organizations.json'",
                 "'tracked org ai-draft'", "/settings/organization/tracked/ai-draft/", "stderr_summary(process.stderr)"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    call = "seed_tracked_ai_draft(base)"
    assert call in seed and "skipped['tracked org ai-draft']" in seed
    assert seed.index("seed_organizations(base)") < seed.index(call) < seed.index("seeded.json")


def _seed_fleet_runs_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_fleet_runs", LAB.parent / "seed_fleet_runs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fleet_run_seed_covers_every_trigger_source_and_outcome_on_the_fleets_members():
    """One run per trigger (four integration providers), and every outcome the timeline badges."""
    from datetime import UTC, datetime, timedelta

    from app.services.run_outcome import run_outcome

    module = _seed_fleet_runs_module()
    agents = {name: f"id-{name}" for name in ("Researcher", "Analyst", "Writer", "Orchestrator")}
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    rows = module.plan(agents, now)
    contexts = [r["trigger_context"] for r in rows]
    assert {c["type"] for c in contexts} == {
        "integration_event", "webhook", "work_item", "chat_handover", "chain", "schedule", "manual",
    }
    events = [c for c in contexts if c["type"] == "integration_event"]
    assert {c["provider"] for c in events} == {"opencti", "openaev", "opencrq", "github"}
    assert all(c.get("event") for c in events)
    assert {run_outcome(r["status"], r["error"]) for r in rows} == {"ok", "failed", "interrupted", "other", "active"}
    assert all(timedelta(0) < now - r["created_at"] <= timedelta(hours=1) for r in rows)
    assert all(r["completed_at"] is None for r in rows if r["status"] == "running")
    assert {r["agent_id"] for r in rows} <= set(agents.values())
    assert all(c["seeded_by"] == "lab" for c in contexts)

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_fleet_runs"])
    for part in ("'seed_fleet_runs.py'", "sys.executable", "env=seed_script_env()", "'fleet runs by trigger'",
                 "stderr_summary(process.stderr)", "json.dumps(LAB_AGENTS)"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_fleet_runs(token, base)" in seed and "skipped['fleet runs by trigger']" in seed
    assert seed.index("seed_fleet_runs(token, base)") < seed.index("seeded.json")


def _seed_ctem_fleet_event_runs_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "lab_seed_ctem_fleet_event_runs", LAB.parent / "seed_ctem_fleet_event_runs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_ctem_fleet_event_run_reads_its_trigger_from_the_fleets_own_assignments():
    """The event of the seeded run is the one a seeded assignment subscribes to, never invented (#10598)."""
    from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS
    from app.agents.builtin_fleets import BUILTIN_FLEETS

    module = _seed_ctem_fleet_event_runs_module()
    fleets = {f["slug"]: f for f in BUILTIN_FLEETS}
    assert set(module.FLEETS) <= set(fleets)
    for slug in module.FLEETS:
        members = {fleets[slug]["orchestrator"], *(m["agent"] for m in fleets[slug].get("members", []))}
        rows = [
            {"id": a["name"], "name": a["name"], "agent_id": a["agent_slug"], "trigger_type": a["trigger_type"],
             "trigger_config": a.get("trigger_config")}
            for a in BUILTIN_ASSIGNMENTS if a.get("agent_slug") in members
        ]
        trigger = module.pick_trigger(rows)
        assert trigger is not None, slug
        source = next(r for r in rows if r["id"] == trigger["id"])
        assert trigger["event"] == module.event_name(source["trigger_type"], source["trigger_config"]), slug
    assert module.pick_trigger([{"id": "a", "name": "a", "agent_id": "x", "trigger_type": "schedule",
                                 "trigger_config": {"cron": "0 6 * * *"}}]) is None
    assert module.event_name("integration_event", {"provider": "opencti", "provision": {"event_types": ["create"]}}) == "create"


def test_the_ctem_fleet_event_run_carries_a_trace_and_its_handovers():
    import importlib.util
    from datetime import UTC, datetime

    module = _seed_ctem_fleet_event_runs_module()
    trigger = {"id": "asg", "name": "New relevant threat", "trigger_type": "integration_event",
               "provider": "opencti", "event": "create"}
    rows = module.plan("Fleet", trigger, "orch", [("m1", "One"), ("m2", "Two"), ("m3", "Three")],
                       datetime(2026, 11, 10, tzinfo=UTC))
    run = rows["run"]
    assert run["status"] == "completed" and run["assignment_id"] == "asg" and run["agent_id"] == "orch"
    assert run["trigger_context"] == {"type": "integration_event", "provider": "opencti", "event": "create",
                                      "seeded_by": "lab"}
    roles = [m["role"] for m in run["messages"]]
    assert roles == ["user", "assistant", "tool", "tool", "assistant"]
    assert [c["name"] for c in run["messages"][1]["tool_calls"]] == ["consult_agent", "consult_agent"]
    assert [c["agent_id"] for c in rows["children"]] == ["m1", "m2"]
    assert all(c["trigger_payload"]["trigger_type"] == "delegation" for c in rows["children"])
    assert all(c["completed_at"] <= run["completed_at"] for c in rows["children"])
    # Older than the opt-in seed's failed run, so the failing fleet keeps its health 'failing'.
    optin = LAB.parent / "seed_ctem_fleet_optin.py"
    spec = importlib.util.spec_from_file_location("lab_seed_ctem_fleet_optin_age", optin)
    optin_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(optin_module)
    assert module.STARTED_AGO > optin_module.FAILED_AGE


def test_the_lab_seeds_the_ctem_fleet_event_runs_and_logs_each_fleet():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_ctem_fleet_event_runs"])
    for part in ("'seed_ctem_fleet_event_runs.py'", "sys.executable", "env=seed_script_env()",
                 "CTEM fleet event run: ", "/assignments/runs/", "/activity", "agentic_steps", "/operations/executions/"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_ctem_fleet_event_runs(token, base)" in seed and "skipped['CTEM fleet event run']" in seed
    assert seed.index("seed_fleet_runs(token, base)") < seed.index("seed_ctem_fleet_event_runs(token, base)")
    assert seed.index("seed_ctem_fleet_event_runs(token, base)") < seed.index("seeded.json")


def _seed_awaiting_human_runs_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "lab_seed_awaiting_human_runs", LAB.parent / "seed_awaiting_human_runs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_awaiting_human_seed_pauses_runs_on_approvals_and_a_question_on_two_fleets():
    """#10221: two approvals, a long question, company-managed, one decided, one older than a day."""
    from datetime import UTC, datetime, timedelta

    module = _seed_awaiting_human_runs_module()
    now = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)
    rows = module.plan("f" * 36, ["agent-a", "agent-b"], now)
    cases = {r["trigger_context"]["case"]: r for r in rows}
    assert set(cases) == {"two-approvals", "long-question", "company-managed", "decided", "older"}
    assert all(r["status"] == "awaiting_human" and r["completed_at"] is None for r in rows)
    # Ids set before the flush, so the decisions dual write opens a decision for each paused run
    # (dev:harness-gaps lab-decisions-backfill-misses-seeded-runs).
    import uuid

    assert len({uuid.UUID(r["id"]) for r in rows}) == len(rows)
    source = (LAB.parent / "seed_awaiting_human_runs.py").read_text(encoding="utf-8")
    assert '"id": uuid.UUID(row["id"])' in source
    two = cases["two-approvals"]["pending_approvals"]
    assert len(two) == 2 and len({p["source"] for p in two}) == 2
    assert all(p["source"].startswith("integration:") for p in two)
    assert len(cases["long-question"]["human_input_question"]) > 150
    assert cases["long-question"]["pending_approvals"] is None
    assert cases["company-managed"]["is_company_managed"] is True
    assert len(cases["company-managed"]["pending_approvals"]) == 1
    assert [c for c, r in cases.items() if r["approval_decisions"]] == ["decided"]
    decided = cases["decided"]
    assert set(decided["approval_decisions"]) == {p["tool_call_id"] for p in decided["pending_approvals"]}
    assert now - cases["older"]["created_at"] > timedelta(days=1)
    ids = [p["tool_call_id"] for r in rows for p in (r["pending_approvals"] or [])]
    assert len(ids) == len(set(ids))
    assert {r["agent_id"] for r in rows} == {"agent-a", "agent-b"}
    assert all(r["trigger_context"]["seeded_by"] == "lab" for r in rows)
    assert module.BUILTIN_FLEET_SLUG == "ctem-loop"

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_awaiting_human_runs"])
    for part in ("'seed_awaiting_human_runs.py'", "sys.executable", "env=seed_script_env()", "'awaiting-human runs'",
                 "stderr_summary(process.stderr)", "status=awaiting_human&fleet=", "/approval\"",
                 "approval_answered"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "skipped['awaiting-human runs']" in seed
    assert seed.index("seed_corporate_flow_fleet(token, base)") < seed.index("seed_awaiting_human_runs(")
    assert seed.index("seed_builtin_fleet(base)") < seed.index("seed_awaiting_human_runs(")
    assert seed.index("seed_awaiting_human_runs(") < seed.index("seeded.json")


def _seed_fleet_chat_cards_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_fleet_chat_cards", LAB.parent / "seed_fleet_chat_cards.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fleet_chat_card_seed_stores_the_backend_answers_the_panel_cards_match():
    """#10281: the pause summary and the raise refusal, as the fleet tools answer them, on the last message."""
    import re

    from app.services.builtin_tools.fleets import _AUTONOMY_RAISE_REFUSAL

    module = _seed_fleet_chat_cards_module()
    rows = {r["kind"]: r for r in module.plan("f" * 36, "Lab fleet", 7, _AUTONOMY_RAISE_REFUSAL)}
    assert set(rows) == {"confirm", "raise_refused"}
    confirm, refused = rows["confirm"]["trace"], rows["raise_refused"]["trace"]
    assert confirm["name"] == "set_fleet_automations" and refused["name"] == "update_fleet"
    # The regexes of frontend/src/components/fleets/fleet-chat-cards.ts.
    assert re.match(r"^Not applied\. Pause all would switch off (\d+) automation", confirm["output"]).group(1) == "7"
    assert re.match(r"^An agent can only lower a fleet's autonomy level\.", refused["output"])
    # The summary is the tool's own wording, kept in step with builtin_tools/fleets.py.
    tool = (ROOT / "backend" / "app" / "services" / "builtin_tools" / "fleets.py").read_text(encoding="utf-8")
    for part in (
        "Not applied. Pause all would switch off {count} automation(s) of fleet **{fleet.name}** ",
        "(runs in flight continue; Resume switches back on exactly these). Show this to the user and ",
        "call again with confirm=true only once they agree.",
    ):
        assert part in tool, part
    assert module.pause_summary(7, "Lab fleet") == confirm["output"]
    assert all(r["trace"]["success"] is True for r in rows.values())

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_fleet_chat_cards"])
    for part in (
        "'seed_fleet_chat_cards.py'",
        "sys.executable",
        "env=seed_script_env()",
        "stderr_summary(process.stderr)",
        "tool_call_trace",
        "?ai=1&c=",
    ):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "skipped['fleet chat cards']" in seed and "skipped['fleet AI blocked user']" in seed
    assert seed.index("seed_corporate_flow_fleet(token, base)") < seed.index("seed_fleet_chat_cards(")
    assert seed.index("seed_fleet_chat_cards(") < seed.index("seeded.json")
    assert seed.index("seed_fleet_ai_blocked_user(") < seed.index("seed_ai_lock_states(")


def _seed_perf_thread_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_perf_thread", LAB.parent / "seed_perf_thread.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_perf_thread_seed_plans_200_messages_of_every_markdown_shape_and_a_short_thread():
    """``LAB_PERF_THREAD=on``: a long thread whose answers mix tables, lists, code and prose, and a short one."""
    module = _seed_perf_thread_module()
    rows = {r["kind"]: r for r in module.plan()}
    assert set(rows) == {"long", "short"}
    assert rows["long"]["title"] == "Perf 200-message thread" and 2 * len(rows["long"]["turns"]) == 200
    assert rows["short"]["title"] == "Perf short thread" and 2 * len(rows["short"]["turns"]) == 2
    answers = [reply for _, reply in rows["long"]["turns"]]
    assert any("|---|" in a for a in answers) and any("```python" in a for a in answers)
    assert any("\n- Point" in a for a in answers) and any("> A quote" in a for a in answers)


def test_the_lab_seeds_the_perf_thread_only_when_switched_on_and_logs_its_ids():
    """The switch is off by default; on, the helper starts the script, reads both threads back and logs the ids."""
    namespace = _lab_namespace("lab_switch", "LAB_PERF_THREAD_STATES")
    assert namespace["lab_switch"]("LAB_PERF_THREAD", namespace["LAB_PERF_THREAD_STATES"]) == "off"
    namespace = _lab_namespace("lab_switch", "LAB_PERF_THREAD_STATES", env={"LAB_PERF_THREAD": "on"})
    assert namespace["lab_switch"]("LAB_PERF_THREAD", namespace["LAB_PERF_THREAD_STATES"]) == "on"

    helper = _lab_source("seed_perf_thread")
    for part in (
        "'seed_perf_thread.py'",
        "sys.executable",
        "env=seed_script_env()",
        "stderr_summary(process.stderr)",
        "messages?limit=500",
        "log(f'perf thread: {ids}')",
    ):
        assert part in helper, part
    seed = _lab_source("seed_entities")
    assert "lab_switch('LAB_PERF_THREAD', LAB_PERF_THREAD_STATES) == 'on'" in seed
    assert "skipped['perf thread']" in seed
    assert seed.index("seed_perf_thread(token, base)") < seed.index("seeded.json")


def test_the_fleet_ai_blocked_user_carries_no_fleet_tool_and_refuses_an_agent_that_does():
    """#10281: the blocked state of Modify with AI renders on a real account, never a cache override."""
    calls: list[tuple[str, str, object]] = []
    state = {"agents": [{"name": "Reader", "enabled": True, "disable_chat": False, "builtin_tool_ids": ["web_fetch"]}]}

    def api(method, path, token=None, body=None):
        calls.append((method, path, body))
        if (method, path) == ("POST", "/users"):
            return {"id": "u1"}
        if path == "/auth/login":
            return {"access_token": "t1"}
        if path == "/auth/me":
            return {"id": "u1", "ga_builtin_tool_ids": ["web_fetch"]}
        if path == "/agents":
            return state["agents"]
        if (method, path) == ("POST", "/fleets"):
            return {"id": "f1"}
        return {}

    names = (
        "FLEET_AI_BLOCKED_EMAIL",
        "FLEET_AI_BLOCKED_PASSWORD",
        "FLEET_AI_BLOCKED_NAME",
        "FLEET_TOOL_IDS",
        "SEED_NAME",
        "_created",
        "signed_in_url",
        "seed_fleet_ai_blocked_user",
    )
    seeded = _lab_namespace(*names, api=api)["seed_fleet_ai_blocked_user"]("admin", "http://b")
    assert seeded["fleet AI blocked fleet"] == {"id": "f1", "url": "http://b/fleets/f1"}
    assert seeded["fleet AI blocked user"]["url"] == "http://b/login#token=t1"
    prefs = next(body for method, path, body in calls if path == "/settings/preferences")
    assert not {"platform", "agent_builder", "fleets"} & set(prefs["ga_builtin_tool_ids"])

    state["agents"].append({"name": "Builder", "enabled": True, "disable_chat": False, "builtin_tool_ids": ["fleets"]})
    with pytest.raises(RuntimeError, match="carry the fleet functions: Builder"):
        _lab_namespace(*names, api=api)["seed_fleet_ai_blocked_user"]("admin", "http://b")


def _seed_roi_records_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_roi_records", LAB.parent / "seed_roi_records.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_roi_seed_plans_one_run_per_review_outcome_and_a_work_item_record():
    """Unreviewed, accepted, modified (review minutes measured) and rejected runs, on a built-in unit (#9394)."""
    from datetime import UTC, datetime

    from app.services import roi

    module = _seed_roi_records_module()
    assert module.UNIT in roi.BASELINES_BY_UNIT
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    rows = module.plan(now)
    outcomes = {r["label"]: r["record"]["outcome"] for r in rows}
    assert outcomes == {"unreviewed": None, "accepted": "accepted", "modified": "modified", "rejected": "rejected"}
    assert all(r["record"]["outcome"] in (None, *roi.OUTCOMES) for r in rows)
    modified = next(r for r in rows if r["label"] == "modified")["record"]
    assert modified["review_min"] is not None and modified["review_note"]
    assert all(r["run"]["status"] == "completed" and r["run"]["completed_at"] <= now for r in rows)
    assert all((r["record"]["reviewed_at"] is None) == (r["record"]["outcome"] is None) for r in rows)
    assert all(r["run"]["trigger_context"]["seeded_by"] == "lab" for r in rows)

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_roi_records"])
    for part in ("'seed_roi_records.py'", "sys.executable", "env=seed_script_env()", "'/assignments'",
                 "'/work-items'", "/complete", "stderr_summary(process.stderr)", "ROI review run", "/operations/executions/"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_roi_records(token, base)" in seed and "skipped['ROI review runs']" in seed
    assert seed.index("seed_roi_records(token, base)") < seed.index("seeded.json")


def test_the_roi_seed_back_dates_a_weekly_history_so_the_value_trend_draws():
    """Ten weeks of runs before the seed, accepted and unreviewed mixed, so GET /roi answers 6+ trend points (#10067)."""
    import inspect
    from datetime import UTC, datetime, timedelta

    from app.services import roi
    from app.services.roi_report import week_start

    module = _seed_roi_records_module()
    now = datetime(2026, 11, 8, 13, 0, tzinfo=UTC)  # a Sunday: the history must not fold into the seed's week
    history = module.history_plan(now)
    assert len(history) == module.HISTORY_WEEKS * module.HISTORY_PER_WEEK
    created = [r["record"]["created_at"] for r in history]
    assert created == sorted(created) and all(at < now for at in created)
    weeks = {week_start(r["record"]["created_at"]) for r in [*history, *module.plan(now)]}
    assert len(weeks) >= 7, sorted(weeks)
    # The default 30-day window and the 90-day window both draw a line, not a single point.
    assert len({w for w in weeks if w >= week_start(now - timedelta(days=30))}) >= 5
    assert len({w for w in weeks if w >= week_start(now - timedelta(days=90))}) >= 11
    # The module's own bucket matches the report's (Monday 00:00 UTC).
    assert {module.week_of(r["record"]["created_at"]) for r in history} == {
        week_start(r["record"]["created_at"]).date().isoformat() for r in history
    }
    outcomes = {r["record"]["outcome"] for r in history}
    assert outcomes == {None, "accepted"} and outcomes <= {None, *roi.OUTCOMES}
    assert all((r["record"]["reviewed_at"] is None) == (r["record"]["outcome"] is None) for r in history)
    assert all(r["run"]["trigger_context"]["seeded_by"] == "lab" for r in history)
    # Only the ROI review seed (with its work item) writes it; the CTEM fleet records stay within 7 days.
    source = inspect.getsource(module.seed)
    assert "history_plan(now) if work_item_id else []" in source and '"trend_weeks"' in source
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "seeded ROI trend weeks: " in ast.unparse(functions["seed_roi_records"])


def test_lab_seeds_roi_records_on_the_ctem_fleet_seeded_on():
    """The CTEM fleet seeded On carries ROI records of the last 7 days, so its tile renders figures (#9831)."""
    import inspect
    from datetime import UTC, datetime, timedelta

    module = _seed_roi_records_module()
    signature = inspect.signature(module.seed)
    assert list(signature.parameters)[3:] == ["work_item_id", "fleet_id"]
    assert signature.parameters["fleet_id"].default is None
    main = inspect.getsource(module.main)
    assert "sys.argv[6]" in main and '== "-"' in main
    source = inspect.getsource(module.seed)
    assert "fleet_id=fleet" in source and "if work_item_id:" in source
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    rows = module.plan(now)
    # Within the 7-day window, mixed outcomes: the tile's review rate is neither 0 nor 1.
    assert all(now - r["record"]["created_at"] < timedelta(days=7) for r in rows)
    outcomes = [r["record"]["outcome"] for r in rows]
    assert None in outcomes and "accepted" in outcomes and "rejected" in outcomes
    assert sum(r["record"]["units"] for r in rows) > 0

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_ctem_fleet_roi"])
    for part in (
        "'ctem_fleet_optin.json'",
        "'seed_roi_records.py'",
        "sys.executable",
        "env=seed_script_env()",
        "'-'",
        "str(on['id'])",
        "'/roi?window=7d'",
        "'CTEM fleet ROI records'",
        "f'{base}/'",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_ctem_fleet_roi(token, base)" in seed and "skipped['CTEM fleet ROI records']" in seed
    assert seed.index("seed_ctem_fleet_optin(token, base)") < seed.index("seed_ctem_fleet_roi(token, base)")
    # The start log names it: ``seeded CTEM fleet ROI records: <base>/`` or ``not seeded ...: <why>``.
    text = LAB.read_text(encoding="utf-8")
    assert "log(f\"seeded {entity}: {row['url']}\")" in text and 'log(f"not seeded {entity}: {why}")' in text


def _seed_roi_fleet_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_roi_fleet", LAB.parent / "seed_roi_fleet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_roi_fleet_seed_declares_a_unit_and_records_counted_runs_inside_the_window():
    """"Lab sample fleet" declares a built-in unit and has accepted runs in the 30-day window (#9734)."""
    from datetime import UTC, datetime, timedelta

    from app.services import roi

    module = _seed_roi_fleet_module()
    assert module.UNIT in roi.BASELINES_BY_UNIT
    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    rows = module.plan(now)
    assert rows and all(r["record"]["units"] > 0 and r["record"]["outcome"] in roi.OUTCOMES for r in rows)
    assert all(now - timedelta(days=30) < r["record"]["created_at"] <= now for r in rows)
    assert all(r["run"]["status"] == "completed" and r["run"]["trigger_context"]["seeded_by"] == "lab" for r in rows)
    assert "fleet_id=fleet" in (LAB.parent / "seed_roi_fleet.py").read_text(encoding="utf-8")

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_roi_fleet"])
    for part in ("'seed_roi_fleet.py'", "sys.executable", "env=seed_script_env()", "/roi/declarations?subject_type=fleet",
                 "'per_run'", "'/assignments'", "stderr_summary(process.stderr)", "'ROI fleet'", "/observability/outcomes"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_roi_fleet(token, base, ids['fleet'])" in seed and "skipped['ROI fleet']" in seed
    assert seed.index("seed_roi_fleet(token, base, ids['fleet'])") < seed.index("seeded.json")


def _seed_roi_chat_records_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_roi_chat_records", LAB.parent / "seed_roi_chat_records.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_roi_chat_seed_covers_every_state_of_the_from_chat_card():
    """Every kind, counted and waiting lines, corrections past 1,000 hours, long names, peer lines (#10696)."""
    import uuid
    from datetime import UTC, datetime, timedelta

    from app.services import roi_chat, roi_chat_series

    module = _seed_roi_chat_records_module()
    ids = {role: str(uuid.uuid4()) for role in module.ROLES}
    now = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    rows = [r["record"] for r in module.plan(now, ids)]
    assert 6 <= len(rows) <= 10
    assert {r["kind"] for r in rows} == set(roi_chat.KINDS)
    assert len({r["record_key"] for r in rows}) == len(rows)
    assert all(r["record_key"] == roi_chat.record_key(r["kind"], r["deliverable_id"]) for r in rows)
    assert all(now - r["created_at"] < timedelta(days=30) for r in rows)  # the default window
    counted = [r for r in rows if roi_chat.is_counted(r["signals"], last_edit_at=r["last_edit_at"], now=now)]
    assert 0 < len(counted) < len(rows)
    assert any(not r["signals"] for r in counted)  # one settled with no signal
    assert all(set(r["signals"]) <= set(roi_chat.USE_SIGNALS) for r in rows)
    corrected = [r for r in counted if r["adjusted_min"] is not None]
    assert corrected and all(r["adjusted_by_user_id"] and r["adjusted_at"] for r in corrected)
    assert all(0 <= r["adjusted_min"] <= roi_chat_series.CORRECTION_MAX_MIN for r in corrected)
    minutes = sum(
        roi_chat_series.counted_minutes(roi_chat.estimate(r["kind"], r["size"], user_min=0.0), r["adjusted_min"])
        for r in counted
    )
    assert minutes / 60 > 1000  # compact figures
    assert any(len(r["title"]) > 100 for r in rows) and all(len(r["title"]) <= 300 for r in rows)
    by_user = {r["user_id"] for r in rows}
    by_agent = {r["agent_id"] for r in rows}
    assert {ids["admin"], ids["peer"], ids["long_user"]} <= by_user
    assert {ids["review_agent"], ids["fleet_agent"], ids["long_agent"]} <= by_agent
    assert any(r["user_id"] == ids["peer"] and r in counted for r in rows)

    text = LAB.read_text(encoding="utf-8")
    tree = ast.parse(text)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_roi_chat_records"])
    for part in (
        "'seed_roi_chat_records.py'",
        "sys.executable",
        "env=seed_script_env()",
        "'/roi/chat?window=30d'",
        "stderr_summary(process.stderr)",
        "seeded ROI chat records (",
        "/tasks/value?scope=organization",
        "FLEET_MEMBERS[0]",
    ):
        assert part in helper, part
    assert len(text.split("ROI_CHAT_LONG_AGENT = ")[1].split("\n")[0]) > 40
    _email, person = ast.literal_eval(text.split("ROI_CHAT_LONG_USER = ")[1].split("\n")[0])
    assert len(person) > 40
    seed = ast.unparse(functions["seed_entities"])
    call = "seed_roi_chat_records(token, base, seeded['ROI review agent']['id'], peer_id)"
    assert call in seed and "skipped['ROI chat records']" in seed
    assert seed.index("seed_roi_records(token, base)") < seed.index(call) < seed.index("seeded.json")
    assert seed.index("peer_id = ") < seed.index(call)


def test_the_peer_manages_a_seeded_group():
    """"Lab managed group" has the peer as manager, so the peer's Analytics shows the manager view (#9734)."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_group_manager"])
    for part in ("'/groups'", "/members", "/managers", "'group manager (peer)'", "/groups/"):
        assert part in helper, part
    assert "LAB_MANAGED_GROUP = 'Lab managed group'" in ast.unparse(tree)
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_group_manager(token, base, peer_id)" in seed and "skipped['group manager (peer)']" in seed
    assert seed.index("seed_peer(token, base)") < seed.index("seed_group_manager(token, base, peer_id)")


def _seed_feedback_comments_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "lab_seed_feedback_comments", LAB.parent / "seed_feedback_comments.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_feedback_seed_fills_three_pages_of_commented_feedback_on_one_agent():
    """Over 20 comments, both ratings, every channel, one long comment and one nameless rater (#9494)."""
    from datetime import UTC, datetime

    module = _seed_feedback_comments_module()
    now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
    rows = module.plan(now, 15)
    assert len(rows) > 20
    assert {r["rating"] for r in rows} == {"positive", "negative"}
    assert {r["channel"] for r in rows} == set(module.CHANNELS) and len(module.CHANNELS) >= 3
    assert all(r["comment"].strip() for r in rows)
    long = [r for r in rows if len(r["comment"]) > 250]
    assert len(long) == 1 and "indicators" in long[0]["comment"]
    assert sum(r["rater"] == 15 for r in rows) == 1
    assert all(0 <= r["rater"] <= 15 for r in rows)
    assert all(r["at"] < now for r in rows)
    assert len({r["at"] for r in rows}) == len(rows)
    assert module.AGENT_INDEX == 11

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert functions["seed_analytics_traces"] is not None
    assert "range(1, ANALYTICS_AGENTS + 1)" in ast.unparse(functions["seed_analytics_traces"])
    helper = ast.unparse(functions["seed_feedback_comments"])
    for part in ("'seed_feedback_comments.py'", "sys.executable", "env=seed_script_env()", "analytics_ids.json",
                 "stderr_summary(process.stderr)", "'feedback comments'", "/observability"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_feedback_comments(base)" in seed and "skipped['feedback comments']" in seed
    assert "'feedback criteria'" in helper and '/feedback"' in helper
    assert "skipped['feedback criteria']" in seed
    assert seed.index("seed_analytics_traces(token, base)") < seed.index("seed_feedback_comments(base)")
    assert seed.index("seed_feedback_comments(base)") < seed.index("seeded.json")


def test_the_feedback_seed_scores_three_criteria_over_thirty_days():
    """A 1-5 scale, a categorical with 3 options and a long name, a disabled numeric 0-10; over 10 scored ratings
    spread over the last 30 days, both enabled criteria scored, every value inside its bounds (#9860)."""
    from datetime import UTC, datetime, timedelta

    from app.services.feedback_criteria import base_key, validate_shape

    module = _seed_feedback_comments_module()
    criteria = module.CRITERIA
    assert [c["type"] for c in criteria] == ["scale", "categorical", "numeric"]
    assert [c["enabled"] for c in criteria] == [True, True, False]
    assert (criteria[0]["min_value"], criteria[0]["max_value"]) == (1, 5)
    assert (criteria[2]["min_value"], criteria[2]["max_value"]) == (0, 10)
    assert len(criteria[1]["options"]) == 3
    assert any(len(c["name"]) > 50 and len(c["name"]) <= 100 for c in criteria)
    assert len({base_key(c["name"]) for c in criteria}) == 3
    for c in criteria:
        validate_shape(c["type"], c["options"], c["min_value"], c["max_value"])

    now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
    rows = module.plan(now, 15)
    plans = module.score_plan(rows)
    assert len(plans) == len(rows)
    scored = [(row, scores) for row, scores in zip(rows, plans, strict=True) if scores]
    assert len(scored) >= 10
    assert all(now - timedelta(days=30) < row["at"] < now for row in rows)
    assert len({row["at"].date() for row, _ in scored}) >= 10
    options = {o["value"] for o in criteria[1]["options"]}
    for _, scores in scored:
        assert {index for index, _, _ in scores} >= {0, 1}
        for index, value_numeric, value_text in scores:
            spec = criteria[index]
            if spec["type"] == "categorical":
                assert value_numeric is None and value_text in options
            else:
                assert value_text is None and spec["min_value"] <= value_numeric <= spec["max_value"]
    assert {s[1] for _, scores in scored for s in scores if s[0] == 0} >= {1.0, 5.0}
    assert any(s[0] == 2 for _, scores in scored for s in scores)


def _seed_analytics_traces_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_analytics_traces", LAB.parent / "seed_analytics_traces.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_analytics_seed_fills_every_breakdown_past_one_page_and_aggregates():
    """20 agents, 15 users, 10 models and 12 error messages over 30 past days, then the rollups."""
    from datetime import UTC, datetime, timedelta

    from app.models.llm_trace import LLM_TRACE_SOURCES

    module = _seed_analytics_traces_module()
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.startswith("ANALYTICS_")
    }
    agents = [f"agent-{n}" for n in range(constants["ANALYTICS_AGENTS"])]
    users = [f"user-{n}" for n in range(constants["ANALYTICS_USERS"])]
    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    rows = module.plan(agents, users, now)
    ok = [r for r in rows if r["status"] == "ok"]
    errors = [r for r in rows if r["status"] == "error"]

    # More than one page (10 rows) of every table the Analytics page lists.
    assert len({r["agent_id"] for r in ok}) == 20
    assert len({r["user_id"] for r in rows}) == 15
    assert len({r["model"] for r in ok}) == 10
    assert len(constants["ANALYTICS_GROUPS"]) == 3
    assert len({(r["error_message"], r["agent_id"], r["model"]) for r in errors}) == len(module.ERRORS) > 10
    # Every trace on a past day inside the 30-day window, the errors inside the default 7-day one.
    assert all(now - timedelta(days=module.DAYS + 1) < r["started_at"] < now - timedelta(hours=1) for r in rows)
    assert all(r["started_at"] > now - timedelta(days=7) for r in errors)
    assert len({r["started_at"].date() for r in ok}) == module.DAYS
    # Rows the trace table accepts, attributed to their user, with a cost.
    assert {r["source"] for r in rows} <= set(LLM_TRACE_SOURCES)
    assert all(r["attributed_user_id"] == r["user_id"] and r["attribution"] == "user" for r in rows)
    assert all(r["total_cost"] > 0 and r["total_tokens"] == r["input_tokens"] + r["output_tokens"] for r in ok)
    assert "run_daily_stats_backfill" in (LAB.parent / "seed_analytics_traces.py").read_text(encoding="utf-8")

    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_analytics_traces"])
    for part in ("'seed_analytics_traces.py'", "sys.executable", "env=seed_script_env()", "'/users'", "'/groups'",
                 "/members", "stderr_summary(process.stderr)", "'analytics traces'", "/observability"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_analytics_traces(token, base)" in seed and "skipped['analytics traces']" in seed
    assert seed.index("seed_analytics_traces(token, base)") < seed.index("seeded.json")


def _seed_trace_sessions_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_trace_sessions", LAB.parent / "seed_trace_sessions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_trace_sessions_seed_fills_two_pages_that_sort_on_every_column():
    """Over 10 sessions of 2 to 30 traces, errors, a long title, the peer's, over 60 days (#9742)."""
    from datetime import UTC, datetime, timedelta

    from app.services.tracing.read_store import TRACE_SESSION_SORT_FIELDS

    module = _seed_trace_sessions_module()
    agents = [f"agent-{n}" for n in range(20)]
    users = [f"user-{n}" for n in range(15)]
    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    sessions = module.plan(agents, users, "peer", now)

    assert len(sessions) > 10
    assert all(2 <= len(s["traces"]) <= 30 for s in sessions)
    assert sum(s["owner"] == "peer" for s in sessions) == 1
    assert module.plan(agents, users, None, now)[module.PEER_SESSION]["owner"] in users
    assert sum(len(s["title"]) > 200 for s in sessions) == 1
    assert all(t["user_id"] == s["owner"] for s in sessions for t in s["traces"])
    # Every trace before now, the sessions spread over the 60 days.
    started = [t["started_at"] for s in sessions for t in s["traces"]]
    assert all(now - timedelta(days=module.DAYS + 1) < at < now for at in started)
    assert max(started) - min(started) > timedelta(days=module.DAYS - 5)
    # What each sortable column aggregates differs between sessions, so a sort changes the order.
    columns = {
        "last_activity_at": lambda s: max(t["started_at"] for t in s["traces"]),
        "trace_count": lambda s: len(s["traces"]),
        "error_count": lambda s: sum(t["status"] == "error" for t in s["traces"]),
        "agent_count": lambda s: len({t["agent_id"] for t in s["traces"]}),
        "model_count": lambda s: len({t["model"] for t in s["traces"]}),
        "total_tokens": lambda s: sum(t["total_tokens"] for t in s["traces"]),
        "total_cost": lambda s: round(sum(t["total_cost"] for t in s["traces"]), 8),
    }
    assert set(columns) | {"conversation_id"} == set(TRACE_SESSION_SORT_FIELDS)
    for name, value in columns.items():
        assert len({value(s) for s in sessions}) > 2, name
    for name in ("last_activity_at", "trace_count", "total_tokens", "total_cost"):
        assert len({columns[name](s) for s in sessions}) == len(sessions), name
    assert sum(columns["error_count"](s) > 0 for s in sessions) >= 3

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_trace_sessions"])
    for part in ("'seed_trace_sessions.py'", "sys.executable", "env=seed_script_env()", "analytics_ids.json",
                 "stderr_summary(process.stderr)", "'trace sessions'", "/observability/sessions"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_trace_sessions(base, peer_id)" in seed and "skipped['trace sessions']" in seed
    assert seed.index("seed_analytics_traces(token, base)") < seed.index("seed_trace_sessions(base, peer_id)")
    assert seed.index("seed_peer(token, base)") < seed.index("seed_trace_sessions(base, peer_id)")
    assert seed.index("seed_trace_sessions(base, peer_id)") < seed.index("seeded.json")


def _seed_video_findings_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_video_findings", LAB.parent / "seed_video_findings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_video_seed_carries_every_labelled_finding_kind_in_both_severities():
    """The Renders tab of the seeded video lists every kind ``FINDING_KIND_LABELS`` labels, fail and warn."""
    import re

    source = (ROOT / "frontend" / "src" / "components" / "video-studio" / "video-format.ts").read_text(encoding="utf-8")
    block = re.search(r"const FINDING_KIND_LABELS = \{(.*?)\} as const;", source, re.S)
    assert block, "FINDING_KIND_LABELS is gone from video-format.ts"
    labelled = set(re.findall(r"^\s*(\w+):", block.group(1), re.M))

    module = _seed_video_findings_module()
    kinds = module.finding_kinds()
    assert set(kinds) == labelled
    rows = module.findings(kinds, "open")
    assert {(r["kind"], r["severity"]) for r in rows} == {(k, s) for k in labelled for s in ("fail", "warn")}
    assert all(r["scene_id"] == "open" and r["message"].startswith("Seeded by the lab") for r in rows)
    assert all(r["end"] is None or r["end"] > r["at"] for r in rows)

    # Every kind renders on a real row: the Renders tab shows the first 6 findings of a render, so each seeded
    # whole render carries at most 3 kinds and no kind sits past index 5 of its render.
    panel = (ROOT / "frontend" / "src" / "components" / "video-studio" / "editor" / "RendersPanel.tsx").read_text(
        encoding="utf-8"
    )
    assert f"findings.slice(0, {module.VISIBLE_FINDINGS})" in panel
    groups = module.kind_groups(kinds)
    assert [kind for group in groups for kind in group] == kinds
    for group in groups:
        render_rows = module.findings(group, "open")
        assert len(render_rows) <= module.VISIBLE_FINDINGS
        assert {r["kind"] for r in render_rows[: module.VISIBLE_FINDINGS]} == set(group)

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_video_findings"])
    for part in ("'/video-studio/projects'", "'seed_video_findings.py'", "sys.executable", "env=seed_script_env()",
                 "'video render findings'", "/videos/", "stderr_summary(process.stderr)", "written.get('renders')"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_video_findings(token, base)" in seed and "skipped['video render findings']" in seed
    assert seed.index("seed_video_findings(token, base)") < seed.index("seeded.json")


def _seed_video_media_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_video_media", LAB.parent / "seed_video_media.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_video_media_seed_fills_two_pages_used_and_unused_inside_the_retention():
    """The Media tab lists more than two pages of recordings and AI shots, some used by a video, none expiring (#10330)."""
    from datetime import UTC, datetime

    from app.core.config import settings
    from app.services.video_studio import library, spec

    module = _seed_video_media_module()
    now = datetime.now(UTC)
    rows = module.media_plan(now)
    assert len(rows) == module.MEDIA > library.ASSET_PAGE_SIZE and module.MEDIA >= 60
    assert sum(r["kind"] == "take" for r in rows) == module.TAKES
    assert sum(r["kind"] == "clip" for r in rows) == module.CLIPS
    used = [r for r in rows if r["used"]]
    assert 0 < len(used) < len(rows) and len(used) == -(-module.MEDIA // module.USED_EVERY)
    assert {r["kind"] for r in used} == {"take", "clip"}
    # Both kinds and both usages on the first page, newest first.
    first = rows[: library.ASSET_PAGE_SIZE]
    assert {r["kind"] for r in first} == {"take", "clip"} and {r["used"] for r in first} == {True, False}
    assert [r["created_at"] for r in rows] == sorted((r["created_at"] for r in rows), reverse=True)
    # Spread over several days, never past the unused retention.
    oldest = now - rows[-1]["created_at"]
    assert 1 < oldest.days + oldest.seconds / 86400 <= module.SPREAD_DAYS < settings.video_studio_unused_media_retention_days
    assert len({r["name"] for r in rows}) == len(rows)
    assert all(r["name"].startswith(module.SEED_PREFIX) and len(r["name"]) <= 200 for r in rows)
    assert max(len(r["name"]) for r in rows) >= 100

    # The video names every used medium, as the index the library reads (``asset_refs``) sees it.
    ids = [(r["kind"], f"00000000-0000-4000-8000-{index:012d}") for index, r in enumerate(used)]
    assert spec.asset_refs(module.video_spec(ids)) == sorted(f"{kind}:{asset_id}" for kind, asset_id in ids)
    # A valid video: the editor opens it as any other.
    spec.VideoSpec.model_validate(module.video_spec(ids))

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_video_media"])
    for part in ("'seed_video_media.py'", "sys.executable", "env=seed_script_env()", "'video media library'",
                 "/videos/media", "stderr_summary(process.stderr)"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_video_media(token, base)" in seed and "skipped['video media library']" in seed
    assert seed.index("seed_video_media(token, base)") < seed.index("seeded.json")


def test_the_video_gate_seed_refuses_the_next_render_on_every_picture_kind():
    """The render gate video's only whole render left one open ``fail`` per ``PICTURE_KINDS`` kind (or the kinds
    ``LAB_VIDEO_GATE_KINDS`` names): ``open_findings`` keeps every row, so Render opens the refused-render dialog."""
    import pytest

    from app.services.video_studio import render_checks

    module = _seed_video_findings_module()
    kinds = module.gate_kinds()
    assert set(kinds) == set(render_checks.PICTURE_KINDS)
    rows = module.gate_findings(kinds, "open")
    assert render_checks.open_findings({"findings": rows}, same_quality=False) == rows
    assert all(r["severity"] == "fail" and r["scene_id"] == "open" for r in rows)
    assert all(r["message"].startswith("Seeded by the lab") and r["end"] > r["at"] for r in rows)
    assert module.gate_kinds(" insert_flicker , insert_flicker,voice_segment") == ["insert_flicker", "voice_segment"]
    with pytest.raises(ValueError, match="music_level"):
        module.gate_kinds("music_level")

    panel = (ROOT / "frontend" / "src" / "components" / "video-studio" / "editor" / "RendersPanel.tsx").read_text(
        encoding="utf-8"
    )
    assert 'data-testid="video.editor.render.gate"' in panel

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_video_render_gate"])
    for part in ("'/video-studio/projects'", "'seed_video_findings.py'", "'--gate'", "'LAB_VIDEO_GATE_KINDS'",
                 "env=seed_script_env()", "'video render gate'", "/videos/", "stderr_summary(process.stderr)"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_video_render_gate(token, base)" in seed and "skipped['video render gate']" in seed
    assert seed.index("seed_video_findings(token, base)") < seed.index("seed_video_render_gate(token, base)")
    assert seed.index("seed_video_render_gate(token, base)") < seed.index("seeded.json")


def test_the_lab_seeds_a_render_gate_video_with_exactly_one_open_finding(tmp_path, monkeypatch):
    """A third video's render carries one open ``fail`` alone (``--gate-one``), so the refused-render dialog's
    reason renders its singular form ("1 finding") on a real row, not through a fetch override of the 409."""
    from app.services.video_studio import render_checks

    module = _seed_video_findings_module()
    captured: dict = {}

    async def seed(project_id, user_id, gate=None):
        captured["gate"] = gate
        return {"gate": {"id": "r", "kinds": gate}}

    monkeypatch.setattr(module, "seed", seed)
    out = tmp_path / "gate.json"
    monkeypatch.setattr(module.sys, "argv", ["seed_video_findings.py", "p", "u", str(out), "--gate-one", ""])
    module.main()
    assert captured["gate"] == module.gate_kinds()[:1]
    rows = module.gate_findings(captured["gate"], "open")
    assert len(render_checks.open_findings({"findings": rows}, same_quality=False)) == 1
    assert rows[0]["at"] == 0.0 and rows[0]["severity"] == "fail"
    monkeypatch.setattr(module.sys, "argv", ["seed_video_findings.py", "p", "u", str(out), "--gate", ""])
    module.main()
    assert captured["gate"] == module.gate_kinds()

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_video_render_gate"])
    for part in ("'--gate-one'", "'video render gate (one finding)'", "'video_render_gate_one.json'"):
        assert part in helper, part
    seed_entities = ast.unparse(functions["seed_entities"])
    assert "seed_video_render_gate(token, base, one=True)" in seed_entities
    assert "skipped['video render gate (one finding)']" in seed_entities
    assert seed_entities.index("seed_video_render_gate(token, base, one=True)") < seed_entities.index("seeded.json")


def test_the_video_seed_words_every_coded_finding_in_the_reader_language():
    """Every ``FINDING_MESSAGES`` key is seeded with its ``code`` and a value for every placeholder of its
    template, so the Renders tab words it in fr/es/ja without a fetch override; other kinds carry no code."""
    import re

    source = (ROOT / "frontend" / "src" / "components" / "video-studio" / "video-format.ts").read_text(encoding="utf-8")
    block = re.search(r"const FINDING_MESSAGES = \{(.*?)\} as const;", source, re.S)
    assert block, "FINDING_MESSAGES is gone from video-format.ts"
    messages = dict(re.findall(r'^\s*(\w+): "(.*)",$', block.group(1), re.M))

    module = _seed_video_findings_module()
    assert set(module.CODED_KINDS) == set(messages)
    kinds = module.finding_kinds()
    assert set(messages) <= set(kinds)
    rows = module.findings(kinds, "open")
    for code, template in messages.items():
        coded = [r for r in rows if r.get("code") == code]
        assert {r["severity"] for r in coded} == {"fail", "warn"}, code
        placeholders = set(re.findall(r"\{(\w+)[,}]", template))
        for row in coded:
            assert row["kind"] == code and set(row["params"]) == placeholders, code
            assert row["message"].startswith("Seeded by the lab"), code
    assert all("code" not in r and "params" not in r for r in rows if r["kind"] not in messages)
    # Both plural forms of the double image template render.
    assert {r["params"]["frames"] for r in rows if r.get("code") == "double_image"} == {1, 2}


def _lab_asgi_module(monkeypatch):
    """``lab_asgi`` with a stand-in for ``app.main`` (importing the real one boots nothing here, but is heavy)."""
    import importlib.util
    import sys
    import types

    calls: list[dict] = []

    async def backend(scope, receive, send):
        calls.append(scope)

    monkeypatch.setitem(sys.modules, "app.main", types.SimpleNamespace(application=backend))
    # uvicorn runs it with ``--app-dir .xtm-one/lab``: its sibling modules are importable.
    monkeypatch.syspath_prepend(str(LAB.parent))
    spec = importlib.util.spec_from_file_location("lab_asgi_under_test", LAB.parent / "lab_asgi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls


def test_the_lab_github_integration_lists_one_repository_and_nothing_else_is_answered(monkeypatch):
    """``lab_asgi`` answers the repository elements of the lab's GitHub integration alone; the rest is the app's."""
    import asyncio
    import json as json_module

    module, calls = _lab_asgi_module(monkeypatch)
    lab_id = "11111111-2222-3333-4444-555555555555"
    path = f"/api/v1/integrations/{lab_id}/elements"

    def scope(path: str, query: bytes, method: str = "GET") -> dict:
        return {"type": "http", "method": method, "path": path, "query_string": query}

    answer = module.lab_answer(scope(path, b"element_type=repository"), lab_id)
    assert answer == {"elements": [module.LAB_REPOSITORY], "element_type": "repository"}
    assert module.LAB_REPOSITORY["id"] == module.LAB_REPOSITORY["metadata"]["full_name"] == "lab-sample/lab-repository"
    for other in (
        scope(path, b"element_type=organization"),
        scope(path, b"element_type=repository", "POST"),
        scope("/api/v1/integrations/00000000-0000-0000-0000-000000000000/elements", b"element_type=repository"),
        scope(f"/api/v1/integrations/{lab_id}/github-webhooks", b""),
        {"type": "websocket", "path": path, "query_string": b"element_type=repository"},
    ):
        assert module.lab_answer(other, lab_id) is None, other
    assert module.lab_answer(scope(path, b"element_type=repository"), "") is None

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    monkeypatch.setenv("LAB_GITHUB_INTEGRATION_ID", lab_id)
    asyncio.run(module.application(scope(path, b"element_type=repository"), receive, send))
    assert sent[0]["status"] == 200 and json_module.loads(sent[1]["body"]) == answer and not calls
    asyncio.run(module.application(scope("/api/v1/health", b""), receive, send))
    assert [c["path"] for c in calls] == ["/api/v1/health"]


def _seed_provisioning_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_provisioning", LAB.parent / "seed_provisioning.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_provisioning_seed_writes_one_assignment_per_kind_of_the_trigger_card_line():
    """Each seeded state reads, through the passes' own keys, as the kind it is named after."""
    from datetime import UTC, datetime, timedelta

    from app.services import openaev_trigger_provisioning as openaev
    from app.services import opencti_trigger_provisioning as opencti

    module = _seed_provisioning_module()
    passes = {"opencti": opencti, "openaev": openaev}
    assert {p: m.STATE_KEY for p, m in passes.items()} == module.STATE_KEYS
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    rows = module.plan(now)
    assert set(rows) == set(module.KINDS)

    def kind(provider: str, row: dict) -> str:
        """The kind ``provisioningView`` (provisioning-state.ts) derives from the row."""
        config = module.apply({"provider": provider, "event": "x"}, row["config"])
        state = config.get(passes[provider].STATE_KEY)
        if state:
            return state["status"] if state["status"] in ("provisioned", "orphaned") else "error"
        assert config.get("provision"), row
        return "pending" if row["enabled"] else "disabled"

    for name, provider in module.KINDS.items():
        row = rows[name]
        assert kind(provider, row) == name.split("-")[0], name
        config = module.apply({"provider": provider, "event": "x"}, row["config"])
        if config.get("provision"):
            assert passes[provider].read_provision_spec(config), name
    held_openaev = rows["provisioned-openaev"]["config"]["openaev_provisioning"]
    assert len([v for v in held_openaev["trigger_ids"].values() if v]) == 2
    assert rows["provisioned-opencti"]["config"]["opencti_provisioning"]["trigger_id"]
    assert timedelta(0) < now - rows["provisioned-opencti"]["last_triggered_at"] <= timedelta(hours=1)
    assert module.apply({"provision": {}, "a": 1}, {"provision": None, "b": 2}) == {"a": 1, "b": 2}

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_provisioning_states"])
    for part in ("'seed_provisioning.py'", "sys.executable", "env=seed_script_env()", "'/assignments'",
                 "'integration_event'", "f'provisioning {kind}'", "/assignments/", "stderr_summary(process.stderr)"):
        assert part in helper, part
    assert "'LAB_FREEZE_TRIGGER_PROVISIONING': '1'" in ast.unparse(functions["backend_env"])
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_provisioning_states(token, base, suite)" in seed
    assert seed.index("seed_suite_registrations(token, base)") < seed.index("seed_provisioning_states(")
    assert seed.index("seed_provisioning_states(") < seed.index("seeded.json")


def test_the_lab_freezes_the_trigger_provisioning_pass_only_when_asked(monkeypatch):
    """``lab_asgi`` swaps the scheduler's pass for a no-op under the lab's flag, and leaves it otherwise."""
    import asyncio

    from app.services import trigger_provisioning

    original = trigger_provisioning.maintain_trigger_provisioning
    module, _ = _lab_asgi_module(monkeypatch)
    assert trigger_provisioning.maintain_trigger_provisioning is original
    assert module.freeze_trigger_provisioning({}) is False
    assert trigger_provisioning.maintain_trigger_provisioning is original
    monkeypatch.setattr(trigger_provisioning, "maintain_trigger_provisioning", original)
    assert module.freeze_trigger_provisioning({"LAB_FREEZE_TRIGGER_PROVISIONING": "1"}) is True
    assert trigger_provisioning.maintain_trigger_provisioning is not original
    assert asyncio.run(trigger_provisioning.maintain_trigger_provisioning(object())) is None


def test_the_lab_answers_the_opencti_organization_search_from_its_fixture(monkeypatch):
    """``acme`` lists fixture rows through the real ``search_identities``, ``zzz`` none, ``lab:error`` the real caller."""
    import asyncio

    from app.services import organization_opencti

    original = organization_opencti.opencti_gql
    module, _ = _lab_asgi_module(monkeypatch)
    assert organization_opencti.opencti_gql is original
    assert module.stub_opencti_identities({}) is False
    assert organization_opencti.opencti_gql is original

    fixture = module.LAB_OPENCTI_ORGANIZATIONS
    assert 3 <= len(fixture) <= 5
    assert any(len(n["name"]) > 80 for n in fixture) and any(n["x_opencti_aliases"] for n in fixture)

    real_calls: list[tuple[str, dict | None]] = []

    async def real_gql(query, variables=None):
        real_calls.append((query, variables))
        return {}, "Error: Could not connect to OpenCTI while trying to search"

    async def live(db, user_id):
        return real_gql

    monkeypatch.setattr(organization_opencti, "opencti_gql", live)
    assert module.stub_opencti_identities({"LAB_OPENCTI_IDENTITIES": "1"}) is True
    gql = asyncio.run(organization_opencti.opencti_gql(None, "admin"))

    acme = asyncio.run(organization_opencti.search_identities(gql, "acme"))
    assert acme[0]["name"] == "Acme Corporation"  # the exact alias match first
    assert {n["name"] for n in acme} >= {"Acme Corporation", "Initech"}  # Initech by its alias
    assert all("globex" not in n["name"].lower() for n in acme)
    assert asyncio.run(organization_opencti.search_identities(gql, "zzz")) == []
    assert not real_calls
    try:
        asyncio.run(organization_opencti.search_identities(gql, "lab:error"))
    except RuntimeError as exc:
        assert "Could not connect" in str(exc)
    else:
        raise AssertionError("lab:error must reach the real caller and its error")
    assert len(real_calls) == 1
    acme_id = next(n["id"] for n in fixture if n["name"] == "Acme Corporation")
    read = "query XtmOrg($id: String!) { organization(id: $id) { id name } }"
    data, err = asyncio.run(gql(read, {"id": acme_id}))
    assert err is None and data["organization"]["name"] == "Acme Corporation"
    assert asyncio.run(gql(read, {"id": "identity--unknown"})) == ({"organization": None}, None)
    linked = asyncio.run(organization_opencti.link_identity(gql, name="Acme", relationship="", identity_id=acme_id))
    assert linked == (acme_id, None)
    try:
        asyncio.run(organization_opencti.link_identity(gql, name="x", relationship="", identity_id="identity--unknown"))
    except LookupError:
        pass
    else:
        raise AssertionError("an unknown id must read as no Organization")
    assert len(real_calls) == 1  # the read by id is the fixture's
    try:
        asyncio.run(organization_opencti.link_identity(
            gql, name="x", relationship="", identity_id=module.LAB_OPENCTI_ERROR_ID
        ))
    except RuntimeError as exc:
        assert "Could not connect" in str(exc)
    else:
        raise AssertionError("the reserved id must reach the real caller and its error")
    assert len(real_calls) == 2
    asyncio.run(gql("mutation XtmOrgAdd($input: OrganizationAddInput!) { organizationAdd(input: $input) { id } }", {}))
    assert len(real_calls) == 3  # every other query is the real caller's

    async def unregistered(db, user_id):
        return "no company-managed OpenCTI integration is registered"

    monkeypatch.setattr(organization_opencti, "opencti_gql", unregistered)
    module.stub_opencti_identities({"LAB_OPENCTI_IDENTITIES": "1"})
    assert asyncio.run(organization_opencti.opencti_gql(None, "admin")) == (
        "no company-managed OpenCTI integration is registered"
    )

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "'LAB_OPENCTI_IDENTITIES'" in ast.unparse(functions["backend_env"])
    probe = ast.unparse(functions["seed_opencti_identities_stub"])
    assert "/organizations/opencti-identities?search=" in probe and "/settings/organization" in probe
    seed = ast.unparse(functions["seed_entities"])
    assert "seeded['opencti identities stub'] = seed_opencti_identities_stub(token, base)" in seed
    assert seed.index("seed_suite_registrations(token, base)") < seed.index("seed_opencti_identities_stub(")
    assert seed.index("seed_opencti_identities_stub(") < seed.index("seeded.json")


def test_the_lab_pir_stub_answers_the_shape_the_route_unpacks(monkeypatch):
    """The stub's answer has the arity of the real ``list_pirs`` return and passes the route (#9722).

    A change of the real signature (#9615 added the OpenCTI base URL) fails here instead of
    a 500 on ``GET /organization-context/pirs`` in every lab.
    """
    import asyncio
    import types
    import typing

    from app.api.v1 import organization_context as route
    from app.services import ctem_metrics_collector, organization_context, organization_context_draft

    original = organization_context_draft.list_pirs
    arity = len(typing.get_args(typing.get_type_hints(original)["return"]))
    module, _ = _lab_asgi_module(monkeypatch)

    async def user_creds(db, user_id, provider):
        return ("https://opencti.lab.invalid/", {})

    async def current_version(db, organization_id=None):
        return None

    monkeypatch.setattr(ctem_metrics_collector, "_user_creds", user_creds)
    monkeypatch.setattr(organization_context, "current_version", current_version)
    monkeypatch.setattr(organization_context_draft, "list_pirs", original)
    assert module.stub_opencti_pirs({"LAB_OPENCTI_PIRS": "1"}) is True
    answer = asyncio.run(organization_context_draft.list_pirs(None, user_id="admin"))
    assert len(answer) == arity

    response = asyncio.run(route.list_organization_pirs(db=None, admin=types.SimpleNamespace(id="admin")))
    assert [p.id for p in response.pirs] == [p["id"] for p in module.LAB_OPENCTI_PIRS]
    assert response.unavailable is None and response.opencti_url == "https://opencti.lab.invalid"


def test_the_lab_answers_the_pir_picker_from_its_fixture(monkeypatch):
    """The fixture with ``unavailable`` null, ``lab:pirs-error`` saved answers unreadable, no token not_connected."""
    import asyncio
    import types

    from app.services import ctem_metrics_collector, organization_context, organization_context_draft

    original = organization_context_draft.list_pirs
    module, _ = _lab_asgi_module(monkeypatch)
    assert organization_context_draft.list_pirs is original
    assert module.stub_opencti_pirs({}) is False
    assert organization_context_draft.list_pirs is original

    fixture = module.LAB_OPENCTI_PIRS
    assert 8 <= len(fixture) <= 10
    assert len({p["id"] for p in fixture}) == len(fixture)
    assert any(len(p["name"]) > 60 for p in fixture)

    creds: list = [("https://opencti.lab.invalid", {})]
    saved: list = [None]

    async def user_creds(db, user_id, provider):
        return creds[0]

    async def current_version(db, organization_id=None):
        return saved[0]

    monkeypatch.setattr(ctem_metrics_collector, "_user_creds", user_creds)
    monkeypatch.setattr(organization_context, "current_version", current_version)
    monkeypatch.setattr(organization_context_draft, "list_pirs", original)
    assert module.stub_opencti_pirs({"LAB_OPENCTI_PIRS": "1"}) is True
    stub = organization_context_draft.list_pirs
    assert stub is not original

    rows, unavailable, opencti_url = asyncio.run(stub(None, user_id="admin"))
    assert unavailable is None and rows == fixture and rows is not fixture
    assert opencti_url == "https://opencti.lab.invalid"
    saved[0] = types.SimpleNamespace(profile={"pir_ids": [fixture[0]["id"], "pir--gone"]})
    # the extra id renders as missing
    assert asyncio.run(stub(None, user_id="admin")) == (fixture, None, "https://opencti.lab.invalid")
    saved[0] = types.SimpleNamespace(profile={"pir_ids": [module.LAB_OPENCTI_PIRS_ERROR]})
    assert asyncio.run(stub(None, user_id="admin")) == ([], "unreadable", "https://opencti.lab.invalid")
    creds[0] = None
    assert asyncio.run(stub(None, user_id="admin")) == ([], "not_connected", "")

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "'LAB_OPENCTI_PIRS'" in ast.unparse(functions["backend_env"])
    probe = ast.unparse(functions["seed_opencti_pirs_stub"])
    assert "/organization-context/pirs" in probe and "/settings/organization" in probe
    seed = ast.unparse(functions["seed_entities"])
    assert "seeded['opencti pirs stub'] = seed_opencti_pirs_stub(token, base)" in seed
    assert seed.index("seed_suite_registrations(token, base)") < seed.index("seed_opencti_pirs_stub(")
    assert seed.index("seed_opencti_pirs_stub(") < seed.index("seeded.json")


def test_the_lab_answers_the_opencrq_sync_plan_from_its_fixture(monkeypatch):
    """One row of each plan kind through the route, Apply writes nothing, then the plan where all agrees once."""
    import asyncio
    import types

    from app.api.v1 import organization_context as route
    from app.services import organization_context, organization_context_opencrq

    original = organization_context_opencrq.run_sync
    module, _ = _lab_asgi_module(monkeypatch)
    assert organization_context_opencrq.run_sync is original
    assert module.stub_opencrq_sync({}) is False
    assert organization_context_opencrq.run_sync is original

    saves: list = []

    async def current_version(db, organization_id=None):
        return types.SimpleNamespace(version=3)

    async def save_version(*args, **kwargs):
        saves.append(kwargs)

    class Db:
        async def commit(self):
            return None

    monkeypatch.setattr(organization_context, "current_version", current_version)
    monkeypatch.setattr(organization_context, "save_version", save_version)
    monkeypatch.setattr(organization_context_opencrq, "run_sync", original)
    assert module.stub_opencrq_sync({"LAB_OPENCRQ_SYNC": "1"}) is True
    assert organization_context_opencrq.run_sync is not original
    admin = types.SimpleNamespace(id="admin")

    def sync(apply: bool):
        return asyncio.run(route.sync_organization_context_with_opencrq(
            body=route.OpenCrqSyncRequest(apply=apply), db=Db(), admin=admin,
        ))

    kinds = ("linked", "pulled_from_opencrq", "ambiguous", "not_in_opencrq")
    dry = sync(False)
    assert dry.applied is False and dry.errors == [] and dry.version == 3
    assert {kind: len(dry.plan[kind]) for kind in kinds} == dict.fromkeys(kinds, 1)
    assert dry.plan["pushed_to_opencrq"] == []  # #1591: nothing is written to OpenCRQ
    assert dry.plan["not_in_opencrq"] == [{"primary_asset": "Legacy billing", "opencrq_asset_id": "lab-asset-retired"}]
    assert dry.plan["ambiguous"] == ["Data warehouse"]
    assert sync(False).plan == dry.plan

    applied = sync(True)
    assert applied.applied is True and applied.plan == dry.plan and saves == []
    agreed = sync(False)
    assert all(agreed.plan[kind] == [] for kind in (*kinds, "pushed_to_opencrq"))
    assert sync(False).plan == dry.plan

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "'LAB_OPENCRQ_SYNC'" in ast.unparse(functions["backend_env"])
    probe = ast.unparse(functions["seed_opencrq_sync_stub"])
    assert "/organization-context/opencrq-sync" in probe and "/settings/organization" in probe
    seed = ast.unparse(functions["seed_entities"])
    assert "seeded['OpenCRQ sync stub'] = seed_opencrq_sync_stub(token, base)" in seed
    assert seed.index("seed_org_ai_draft_stub(") < seed.index("seed_opencrq_sync_stub(")
    assert seed.index("seed_opencrq_sync_stub(") < seed.index("seeded.json")


def test_the_lab_seeds_a_fleet_run_estimate_with_a_figure_and_a_per_assignment_error(monkeypatch):
    """#9555: the count query of a fixture identity answers 42; any other id reaches the real caller."""
    import asyncio
    import importlib.util
    from types import SimpleNamespace

    from app.services import organization_opencti
    from app.services.opencti_trigger_provisioning import read_provision_spec
    from app.services.organization_run_cap import estimate_runs, serves

    module, _ = _lab_asgi_module(monkeypatch)
    spec = importlib.util.spec_from_file_location("lab_seed_run_estimate", LAB.parent / "seed_run_estimate.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    assert seed.IDENTITY in {n["id"] for n in module.LAB_OPENCTI_ORGANIZATIONS}
    provision = read_provision_spec({"provider": "opencti", "provision": seed.PROVISION})
    assert provision is not None and provision["organizations"] == "tracked"

    real_calls: list[str] = []

    async def real_gql(query, variables=None):
        real_calls.append(query)
        return {}, "Error: Could not connect to OpenCTI"

    async def live(db, user_id):
        return real_gql

    monkeypatch.setattr(organization_opencti, "opencti_gql", live)
    assert module.stub_opencti_identities({"LAB_OPENCTI_IDENTITIES": "1"}) is True
    gql = asyncio.run(organization_opencti.opencti_gql(None, "admin"))

    assignment = SimpleNamespace(
        id="a1", name="Lab sample run estimate", enabled=True,
        trigger_config={"provider": "opencti", "provision": seed.PROVISION},
    )

    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return [assignment]

    class _Db:
        async def execute(self, _query):
            return _Rows()

    def org(identity):
        return SimpleNamespace(id="o", relationship="partner", monitoring="on", daily_run_cap=5,
                               opencti_organization_id=identity)

    assert serves(provision, org(None))
    linked = asyncio.run(estimate_runs(_Db(), gql, org(seed.IDENTITY), agent_ids=["m"]))
    assert linked["assignments"][0]["events_last_30_days"] == module.LAB_RUN_ESTIMATE_COUNT
    assert linked["expected_runs_per_month"] > 0
    unlinked = asyncio.run(estimate_runs(_Db(), gql, org(None), agent_ids=["m"]))
    assert unlinked["assignments"][0]["error"] == "no OpenCTI identity linked"
    assert unlinked["expected_runs_per_month"] == 0
    assert not real_calls
    other = asyncio.run(estimate_runs(_Db(), gql, org("identity--unknown"), agent_ids=["m"]))
    assert "Could not connect" in other["assignments"][0]["error"] and len(real_calls) == 1

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_fleet_run_estimate"])
    for part in ("'seed_run_estimate.py'", "sys.executable", "env=seed_script_env()", "'/assignments'",
                 "/run-estimate?", "/fleets/{fleet}/members'", "'fleet run estimate'"):
        assert part in helper, part
    seeded = ast.unparse(functions["seed_entities"])
    call = "seed_fleet_run_estimate(token, base, ids['fleet'], suite['opencti'])"
    assert call in seeded
    assert seeded.index("seed_organizations(base)") < seeded.index(call) < seeded.index("seeded.json")
    assert seeded.index("seed_suite_registrations(token, base)") < seeded.index(call)


def test_the_lab_seeds_a_connected_github_integration_served_by_lab_asgi():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_github_integration"])
    for part in ("'/integrations'", "'provider': 'github'", "'auth_type': 'api_key'", "LAB_GITHUB_INTEGRATION_ID",
                 "'connected'", "/github-webhooks", "element_type=repository", "'GitHub integration'", "/config"):
        assert part in helper, part
    assert "'LAB_GITHUB_INTEGRATION_ID': LAB_GITHUB_INTEGRATION_ID" in ast.unparse(functions["backend_env"])
    assert "'lab_asgi:application'" in ast.unparse(functions["backend_argv"])
    assert "backend_argv()" in ast.unparse(functions["main"])
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_github_integration(token, base)" in seed and "skipped['GitHub integration']" in seed
    assert seed.index("seed_github_integration(token, base)") < seed.index("seeded.json")


def test_the_lab_seeds_one_sync_source_per_type_and_a_nova_rule_source_with_a_last_sync():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_kb_sync_sources"])
    for part in ("'/integrations'", "'provider': 'nova'", "'is_company_managed': True", "seed_kb_sync_sources.py",
                 "/sync-sources", "'sync_interval': 'manual'", "/nova-rules/sources", "last_synced_at",
                 "'KB sync sources'", "'Nova rule source'", "/rules"):
        assert part in helper, part
    source = LAB.read_text(encoding="utf-8")
    for source_type in ("website", "microsoft-drive", "google-drive", "github", "drata"):
        assert f'("{source_type}", ' in source, source_type
    for provider in ("drata", "github", "microsoft", "google"):
        assert f'"{provider}": {{"auth_type"' in source, provider
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_kb_sync_sources(token, base)" in seed and "skipped['KB sync sources']" in seed
    assert seed.index("seed_prerequisites_unreadable_fleet(token, base, peer_id)") < seed.index("seed_kb_sync_sources(token, base)")
    script = (LAB.parent / "seed_kb_sync_sources.py").read_text(encoding="utf-8")
    for part in ("access_token_enc", "refresh_token_enc = None", 'status = "connected"',
                 'sync_interval = "manual"', 'last_sync_status = "ok"', "NovaRuleSource"):
        assert part in script, part
    ast.parse(script)


def test_the_lab_answers_the_nova_engine_status_as_available_over_the_route(monkeypatch):
    """``LAB_NOVA_ENGINE=1``: the route's own answer, engine available and every tier; off or not 200: as is."""
    import asyncio
    import json as json_module

    module, _ = _lab_asgi_module(monkeypatch)
    route = {"engine_available": False, "tiers": ["keywords"], "llm_model": "lab/model", "semantic_model": None,
             "semantic_calibration": {"calibrated": True}}
    status_code = [200]

    async def backend(scope, receive, send):
        body = json_module.dumps(route).encode()
        await send({"type": "http.response.start", "status": status_code[0],
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body[:10], "more_body": True})
        await send({"type": "http.response.body", "body": body[10:]})

    monkeypatch.setattr(module, "backend", backend)
    scope = {"type": "http", "method": "GET", "path": module.LAB_NOVA_ENGINE_STATUS_PATH}
    assert module.asks_nova_engine_status(scope, {"LAB_NOVA_ENGINE": "1"}) is True
    assert module.asks_nova_engine_status(scope, {}) is False
    assert module.asks_nova_engine_status({**scope, "method": "POST"}, {"LAB_NOVA_ENGINE": "1"}) is False

    def answer() -> tuple[dict, dict]:
        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        asyncio.run(module.answer_nova_engine_available(scope, None, send))
        return sent[0], json_module.loads(sent[1]["body"])

    monkeypatch.delenv("LAB_NOVA_OVERRIDE_IGNORED", raising=False)
    start, body = answer()
    lab_calibration = {"calibrated": False, "configured_model": module.LAB_NOVA_SEMANTIC_MODEL}
    assert body == {
        **route,
        "engine_available": True,
        "tiers": ["keywords", "semantics", "llm"],
        "semantic_calibration": lab_calibration,
    }
    assert dict(start["headers"])[b"content-length"] == str(len(json_module.dumps(body).encode())).encode()
    monkeypatch.setenv("LAB_NOVA_OVERRIDE_IGNORED", "1")
    assert answer()[1]["semantic_calibration"] == {
        **lab_calibration,
        "override_ignored": module.LAB_NOVA_OVERRIDE_IGNORED,
    }
    status_code[0] = 403
    assert answer()[1] == route
    assert module.nova_engine_available(b"not json") == b"not json"

    # A model the route reports, calibrated or not, and its own override are kept.
    real = {
        "engine_available": False,
        "semantic_calibration": {
            "configured_model": "text-embedding-3-large",
            "calibrated": True,
            "override_ignored": "x",
        },
    }
    kept = json_module.loads(module.nova_engine_available(json_module.dumps(real).encode(), override_ignored=True))
    assert kept["semantic_calibration"] == real["semantic_calibration"]
    # The rules tab shows the uncalibrated line on exactly this answer (NovaRulesTab.tsx semanticUncalibrated).
    assert module.LAB_NOVA_SEMANTIC_MODEL.lower() != "all-minilm-l6-v2"

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    env = ast.unparse(functions["backend_env"])
    assert "'LAB_NOVA_ENGINE'" in env and "'LAB_NOVA_OVERRIDE_IGNORED'" in env
    helper = ast.unparse(functions["seed_kb_sync_sources"])
    assert "/nova-rules/engine-status" in helper and "'Nova engine status'" in helper
    for part in ("semantic_calibration", "configured_model", "'Nova semantic calibration line'", "/rules"):
        assert part in helper, part


def test_the_lab_seeds_a_connected_teams_bot_whose_edit_form_carries_a_client_id_and_an_extra_field():
    """#9822: Channels > Edit credentials renders the keep-current-values notes on a seeded row, lab-only values."""
    from app.api.v1.channel_bots import CHANNEL_BOT_CATALOG

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    declared = next(
        node.value for node in tree.body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "LAB_TEAMS_BOT_BODY"
    )
    body = ast.literal_eval(declared)
    catalog = CHANNEL_BOT_CATALOG[body["provider"]]
    assert body["provider"] == "teams_bot" and "client_id" in catalog["fields"]
    for field in catalog["fields"]:
        assert body[field].startswith("lab-only-"), field
    assert set(body["extra_config"]) == set(catalog["extra_fields"])
    assert all(value.startswith("lab-only-") for value in body["extra_config"].values())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_teams_channel_bot"])
    for part in ("'/channel-bots'", "**LAB_TEAMS_BOT_BODY", "'connected'", "'has_client_id'", "'connected Teams bot'",
                 "/settings/integrations/channels"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    call = "seed_teams_channel_bot(token, base)"
    assert call in seed and "skipped['connected Teams bot']" in seed
    assert seed.index("seed_channel_bot(token, base)") < seed.index(call) < seed.index("seeded.json")


def test_the_lab_teams_bot_answers_its_channel_list_and_every_other_teams_bot_reaches_the_real_caller(monkeypatch):
    """#10084: the seeded Teams bot lists stub channels at once, so its ChannelPickers never stay on Loading."""
    import asyncio
    import types

    from app.api.v1 import channel_bots

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    declared = next(
        node.value for node in tree.body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "LAB_TEAMS_BOT_BODY"
    )
    lab_client_id = ast.literal_eval(declared)["client_id"]
    assert "\"LAB_TEAMS_BOT_CLIENT_ID\": str(LAB_TEAMS_BOT_BODY[\"client_id\"])" in LAB.read_text(encoding="utf-8")

    original = channel_bots._list_teams_channels
    module, _ = _lab_asgi_module(monkeypatch)
    assert module.stub_teams_channels({}) is False
    assert channel_bots._list_teams_channels is original

    fixture = module.LAB_TEAMS_CHANNELS
    assert len(fixture) >= 2 and len({row["id"] for row in fixture}) == len(fixture)
    for row in fixture:
        assert set(row) == {"id", "name", "group"} and ":" in row["id"]

    real_calls: list = []

    async def real(bot):
        real_calls.append(bot)
        return []

    monkeypatch.setattr(channel_bots, "_list_teams_channels", real)
    monkeypatch.setattr(channel_bots, "decrypt_value", lambda value: value)
    assert module.stub_teams_channels({"LAB_TEAMS_BOT_CLIENT_ID": lab_client_id}) is True
    stub = channel_bots._list_teams_channels
    assert stub is not real

    lab_bot = types.SimpleNamespace(provider="teams_bot", client_id_enc=lab_client_id)
    rows = asyncio.run(channel_bots._list_platform_channels(lab_bot))
    assert rows == fixture and rows is not fixture and real_calls == []
    other = types.SimpleNamespace(provider="teams_bot", client_id_enc="a-real-client-id")
    assert asyncio.run(channel_bots._list_platform_channels(other)) == [] and real_calls == [other]


def test_the_lab_seeds_a_saveable_agent_account_bound_to_an_agent_and_the_lab_microsoft_integration():
    """#11332: one seeded agent account carries an agent and an integration, so its Edit dialog can save."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_agent_account_activity"])
    for part in ("'/integrations'", "'provider': 'microsoft'", "LAB_AGENT_ACCOUNT_INTEGRATION_ID", "_seed_agent(",
                 "*saveable", "'agent account (saveable)'"):
        assert part in helper, part
    assert "'LAB_AGENT_ACCOUNT_INTEGRATION_ID': LAB_AGENT_ACCOUNT_INTEGRATION_ID" in ast.unparse(functions["backend_env"])

    script = (LAB.parent / "seed_agent_account_activity.py").read_text(encoding="utf-8")
    seeds = {node.name: node for node in ast.parse(script).body if isinstance(node, ast.AsyncFunctionDef)}
    seed = ast.unparse(seeds["seed"])
    # Bound to both at every start (the integration id is new each time), and written disabled like the others.
    for part in ("account.agent_id = uuid.UUID(agent_id)", "account.integration_id = uuid.UUID(integration_id)",
                 "enabled=False", "if key == 'saveable' and (not (agent_id and integration_id))"):
        assert part in seed, part


def test_the_lab_answers_the_agent_account_pickers_of_its_integration_and_no_other(monkeypatch):
    """#11332: the saveable account's pickers answer from the fixture; any other integration reaches the real session."""
    import asyncio
    import types

    from app.api.v1 import agent_accounts
    from app.services.agent_accounts import management

    original = agent_accounts._picker_session
    module, _ = _lab_asgi_module(monkeypatch)
    assert module.stub_agent_account_pickers({}) is False
    assert agent_accounts._picker_session is original

    lab_id = "22222222-3333-4444-5555-666666666666"
    real_calls: list = []

    async def real(db, user, provider_key, integration_id):
        real_calls.append(integration_id)
        return "real"

    checked: list = []

    async def check_integration(db, user, provider, integration_id):
        checked.append((provider.key, integration_id))

    monkeypatch.setattr(agent_accounts, "_picker_session", real)
    monkeypatch.setattr(management, "check_integration", check_integration)
    assert module.stub_agent_account_pickers({"LAB_AGENT_ACCOUNT_INTEGRATION_ID": lab_id}) is True
    stub = agent_accounts._picker_session
    assert stub is not real

    user = types.SimpleNamespace(id="lab-user")
    provider, session, identity = asyncio.run(stub(None, user, "microsoft_teams", lab_id))
    assert checked == [("microsoft_teams", lab_id)] and real_calls == []
    assert identity.external_id == module.LAB_AGENT_ACCOUNT_IDENTITY["external_id"]
    assert session.account_external_id == identity.external_id and session.integration_id == lab_id
    assert provider.key == "microsoft_teams"
    conversations = asyncio.run(provider.list_conversations(session, None))
    assert [c.id for c in conversations] == [row["id"] for row in module.LAB_AGENT_ACCOUNT_CONVERSATIONS]
    assert [c.name for c in asyncio.run(provider.list_conversations(session, "soc"))] == ["SOC handover"]
    people = asyncio.run(provider.search_people(session, "lab-ciso@"))
    assert [p.name for p in people] == ["Lab CISO"]

    assert asyncio.run(stub(None, user, "microsoft_teams", "another-integration")) == "real"
    assert real_calls == ["another-integration"]


def test_the_lab_seeds_an_openrouter_and_an_lmstudio_integration_whose_models_tab_lists_rows():
    """#10134: both Models tabs render in a review; the start log names each URL or why it was not seeded."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_model_catalog_integrations"])
    for part in ("'provider': 'openrouter'", "LAB_OPENROUTER_API_KEY", "/openrouter/models?kind=",
                 "'provider': 'lmstudio'", "LAB_LMSTUDIO_BASE_URL", "'relay_group_id': relay_group",
                 "/lmstudio/models", "'model catalog integration (openrouter)'",
                 "'model catalog integration (lmstudio)'", "/models'"):
        assert part in helper, part
    env = ast.unparse(functions["backend_env"])
    assert "'LAB_OPENROUTER_API_KEY': LAB_OPENROUTER_API_KEY" in env
    assert "'LAB_LMSTUDIO_BASE_URL': LAB_LMSTUDIO_BASE_URL" in env
    assert "'LAB_OPENROUTER_MANAGEMENT_KEY': LAB_OPENROUTER_MANAGEMENT_KEY" in env
    for part in ("'extra_config': {'management_key': LAB_OPENROUTER_MANAGEMENT_KEY}", "/openrouter/overview",
                 "/openrouter/activity", "'OpenRouter usage activity'", "/usage'",
                 "/openrouter/llm-provider", "/openrouter/keys", "'OpenRouter keys'", "/keys'"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    call = "seed_model_catalog_integrations(token, base, ids.get('relay group'))"
    assert call in seed and seed.index(call) < seed.index("seeded.json")
    text = LAB.read_text(encoding="utf-8")
    assert 'LAB_OPENROUTER_API_KEY = "sk-or-lab-only-' in text
    assert 'LAB_OPENROUTER_MANAGEMENT_KEY = "sk-or-lab-only-' in text
    assert 'LAB_LMSTUDIO_BASE_URL = "http://lmstudio.lab.invalid:' in text


def test_the_lab_model_catalogs_answer_the_lab_key_and_server_and_nothing_else(monkeypatch):
    """#10134: the lab's OpenRouter key and LM Studio server are answered from fixtures; any other reaches the client."""
    import asyncio

    from app.integrations.providers import lmstudio, openrouter

    module, _ = _lab_asgi_module(monkeypatch)
    assert module.stub_model_catalogs({}) == {"openrouter": False, "openrouter_management": False, "lmstudio": False}

    real_calls: list = []

    async def real_models(api_key, *, kind="chat"):
        real_calls.append(("models", api_key, kind))
        return []

    async def real_key_info(api_key):
        real_calls.append(("key", api_key))
        return {}

    async def real_endpoints(api_key, model_id):
        real_calls.append(("endpoints", api_key, model_id))
        return []

    async def real_list(connection):
        real_calls.append(("lmstudio", connection.base_url))
        return []

    monkeypatch.setattr(openrouter, "cached_models", real_models)
    monkeypatch.setattr(openrouter, "get_key_info", real_key_info)
    monkeypatch.setattr(openrouter, "list_model_endpoints", real_endpoints)
    monkeypatch.setattr(lmstudio, "list_models", real_list)
    lab_key, lab_server = "sk-or-lab-only-test", "http://lmstudio.lab.invalid:1234"
    assert module.stub_model_catalogs(
        {"LAB_OPENROUTER_API_KEY": lab_key, "LAB_LMSTUDIO_BASE_URL": lab_server}
    ) == {"openrouter": True, "openrouter_management": False, "lmstudio": True}

    chat = asyncio.run(openrouter.cached_models(lab_key, kind="chat"))
    embeddings = asyncio.run(openrouter.cached_models(lab_key, kind="embeddings"))
    assert len(chat) == len(module.LAB_OPENROUTER_TEXT_MODELS) >= 3
    assert len(embeddings) == len(module.LAB_OPENROUTER_EMBEDDING_MODELS) >= 2
    assert len({m["author"] for m in chat}) >= 2 and len({m["author"] for m in embeddings}) >= 2
    assert [m["id"] for m in chat] == sorted(m["id"] for m in chat)
    assert any(m["is_free"] for m in chat) and any(m["supports_tools"] for m in chat)
    assert asyncio.run(openrouter.get_key_info(lab_key))["label"] == "Lab sample key"
    assert len(asyncio.run(openrouter.list_model_endpoints(lab_key, chat[0]["id"]))) == 2

    served = asyncio.run(lmstudio.list_models(lmstudio.make_connection(lab_server + "/v1", None)))
    assert len(served) == len(module.LAB_LMSTUDIO_MODELS)
    assert {m["type"] for m in served} == {"llm", "embedding"}
    assert len({m["publisher"] for m in served}) >= 2
    assert any(m["loaded_instances"] for m in served) and any(not m["loaded_instances"] for m in served)
    assert real_calls == []

    asyncio.run(openrouter.cached_models("sk-or-a-real-key", kind="chat"))
    asyncio.run(openrouter.get_key_info("sk-or-a-real-key"))
    asyncio.run(openrouter.list_model_endpoints("sk-or-a-real-key", "openai/gpt-lab-mini"))
    asyncio.run(lmstudio.list_models(lmstudio.make_connection("http://gpu-host:1234", None)))
    assert real_calls == [
        ("models", "sk-or-a-real-key", "chat"), ("key", "sk-or-a-real-key"),
        ("endpoints", "sk-or-a-real-key", "openai/gpt-lab-mini"), ("lmstudio", "http://gpu-host:1234"),
    ]


def test_the_lab_openrouter_management_key_answers_credits_and_30_days_of_activity(monkeypatch):
    """#10567: the Usage tab's per-model table and Credits left render on a fresh lab; any other key reaches the client."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    from app.integrations.providers import openrouter

    module, _ = _lab_asgi_module(monkeypatch)
    real_calls: list = []

    async def real_credits(management_key):
        real_calls.append(("credits", management_key))
        return {}

    async def real_activity(management_key, *, date=None, api_key_hash=None):
        real_calls.append(("activity", management_key, date))
        return []

    monkeypatch.setattr(openrouter, "get_credits", real_credits)
    monkeypatch.setattr(openrouter, "get_activity", real_activity)
    for name in ("list_api_keys", "create_api_key", "update_api_key", "delete_api_key"):
        monkeypatch.setattr(openrouter, name, getattr(openrouter, name))
    lab_key = "sk-or-lab-only-management-test"
    assert module.stub_model_catalogs({"LAB_OPENROUTER_MANAGEMENT_KEY": lab_key}) == {
        "openrouter": False, "openrouter_management": True, "lmstudio": False,
    }

    credits = asyncio.run(openrouter.get_credits(lab_key))
    assert credits["remaining"] == credits["total_credits"] - credits["total_usage"] > 0
    rows = asyncio.run(openrouter.get_activity(lab_key))
    assert rows and all(set(row) == set(openrouter.normalize_activity_row(row)) for row in rows)
    today = datetime.now(UTC).date()
    days = {row["date"] for row in rows}
    assert max(days) == (today - timedelta(days=1)).isoformat()
    assert min(days) == (today - timedelta(days=30)).isoformat() and len(days) == 30
    models: dict[str, dict] = {}
    for row in rows:
        entry = models.setdefault(row["model"], {"providers": set(), "usage": 0.0})
        entry["providers"].add(row["provider_name"])
        entry["usage"] += row["usage"]
    assert 6 <= len(models) <= 8
    assert len({p for m in models.values() for p in m["providers"]}) >= 3
    assert any(len(model) > 40 for model in models)
    assert any(len(m["providers"]) == 3 for m in models.values())
    assert any(m["usage"] > 1000 for m in models.values())
    assert any(0 < m["usage"] < 0.01 for m in models.values())
    assert all(openrouter.is_valid_model_id(model) for model in models)
    one_day = (today - timedelta(days=2)).isoformat()
    assert {row["date"] for row in asyncio.run(openrouter.get_activity(lab_key, date=one_day))} == {one_day}
    assert real_calls == []

    asyncio.run(openrouter.get_credits("sk-or-a-real-key"))
    asyncio.run(openrouter.get_activity("sk-or-a-real-key", date=one_day))
    assert real_calls == [("credits", "sk-or-a-real-key"), ("activity", "sk-or-a-real-key", one_day)]


def test_the_lab_openrouter_management_key_lists_five_keys_and_edits_them_in_memory(monkeypatch):
    """The API Keys tab renders on real rows: the integration's own key by its hash, and the edges of the table."""
    import asyncio
    from datetime import UTC, datetime

    from app.integrations.providers import openrouter

    module, _ = _lab_asgi_module(monkeypatch)
    real_calls: list = []

    async def real_list(management_key, *, include_disabled=True):
        real_calls.append(("list", management_key))
        return openrouter.KeyListing(keys=[], truncated=False)

    async def real_delete(management_key, hash_):
        real_calls.append(("delete", management_key, hash_))

    # Every attribute the stub replaces is registered first, so the teardown puts the real client back.
    for name in ("get_credits", "get_activity", "create_api_key", "update_api_key", "cached_models",
                 "get_key_info", "list_model_endpoints"):
        monkeypatch.setattr(openrouter, name, getattr(openrouter, name))
    monkeypatch.setattr(openrouter, "list_api_keys", real_list)
    monkeypatch.setattr(openrouter, "delete_api_key", real_delete)
    api_key, management_key = "sk-or-lab-only-test", "sk-or-lab-only-management-test"
    module.stub_model_catalogs({"LAB_OPENROUTER_API_KEY": api_key, "LAB_OPENROUTER_MANAGEMENT_KEY": management_key})

    listing = asyncio.run(openrouter.list_api_keys(management_key))
    keys = listing.keys
    assert len(keys) >= 5 and not listing.truncated
    assert keys[0]["hash"] == openrouter.key_hash(api_key) and not keys[0]["disabled"]
    assert len({k["hash"] for k in keys}) == len(keys)
    assert any(len(k["name"]) > 80 for k in keys)
    assert any(k["disabled"] and k["limit"] and k["limit_reset"] == "monthly" for k in keys)
    expiring = [k for k in keys if k["expires_at"]]
    assert expiring and all(k["usage"] == 0 for k in expiring)
    assert all(datetime.fromisoformat(k["expires_at"]) > datetime.now(UTC) for k in expiring)
    assert any(k["usage"] >= 1000 for k in keys)
    assert len(asyncio.run(openrouter.list_api_keys(management_key, include_disabled=False)).keys) == len(keys) - 1

    target = keys[1]["hash"]
    assert asyncio.run(openrouter.update_api_key(management_key, target, {"disabled": True}))["disabled"] is True
    created, secret = asyncio.run(openrouter.create_api_key(management_key, {"name": "New", "limit": 5.0}))
    assert secret.startswith("sk-or-lab-only-") and created["name"] == "New"
    asyncio.run(openrouter.delete_api_key(management_key, created["hash"]))
    try:
        asyncio.run(openrouter.delete_api_key(management_key, created["hash"]))
    except openrouter.OpenRouterError as exc:
        assert exc.kind == "not_found"
    else:
        raise AssertionError("a deleted key is deleted twice")
    assert len(asyncio.run(openrouter.list_api_keys(management_key)).keys) == len(keys)
    assert real_calls == []

    asyncio.run(openrouter.list_api_keys("sk-or-a-real-key"))
    asyncio.run(openrouter.delete_api_key("sk-or-a-real-key", target))
    assert real_calls == [("list", "sk-or-a-real-key"), ("delete", "sk-or-a-real-key", target)]


def test_the_lab_seeds_one_assignment_whose_agent_receives_and_publishes_on_slack_and_teams():
    """Assignment > Channels & Bots renders its per-channel cards only from the parent agent's channel_config."""
    from app.channels.base import CHANNEL_TO_BOT_PROVIDER

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    declared = next(
        node.value for node in tree.body
        if isinstance(node, ast.AnnAssign | ast.Assign)
        and getattr(getattr(node, "target", None) or node.targets[0], "id", "") == "LAB_CHANNEL_ASSIGNMENT_BOTS"
    )
    bots = ast.literal_eval(declared)
    assert bots == {"slack": "connected channel bot", "teams": "connected Teams bot"}
    assert set(bots) <= set(CHANNEL_TO_BOT_PROVIDER)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    for entity, helper in (("connected channel bot", "seed_channel_bot"), ("connected Teams bot", "seed_teams_channel_bot")):
        assert f"'{entity}'" in ast.unparse(functions[helper]), helper
    helper = ast.unparse(functions["seed_channel_assignment"])
    for part in ("'receive_enabled': True", "'publish_enabled': True", "'receive_channels': [first]",
                 "'publish_channels': [first]", "'channel_config': config", "'/channel-bots/{bot}/channels'",
                 "f'/agents/{agent}'", "'/assignments'", "/agents/assignments/{assignment}", "receive and publish"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    call = "seed_channel_assignment(token, base, seeded)"
    assert call in seed and "skipped['channel assignment']" in seed
    assert seed.index("seed_teams_channel_bot(token, base)") < seed.index(call) < seed.index("seeded.json")


def test_the_lab_seeds_a_polling_and_a_hybrid_integration_with_one_event_assignment_each():
    """Gmail (polling) and DocuSign (hybrid) render their Configuration tabs side by side, lab-only credentials."""
    from app.api.v1.integration_catalog import PROVIDER_CATALOG, supported_auth_types

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    declared = next(
        node.value for node in tree.body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "LAB_EVENT_INTEGRATIONS"
    )
    integrations = ast.literal_eval(declared)
    mechanisms = {}
    for title, spec in integrations.items():
        body = spec["body"]
        catalog = PROVIDER_CATALOG[body["provider"]]
        assert body["auth_type"] in (supported_auth_types(catalog) or [catalog["auth_type"]]), title
        events = {event["event"]: event for event in catalog["available_events"]}
        mechanisms[title] = events[spec["event"]]["mechanism"]
        for key in ("client_id", "client_secret", "api_key"):
            assert key not in body or body[key].startswith("lab-only-"), (title, key)
    assert mechanisms == {"Gmail": "polling", "DocuSign": "hybrid"}
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_event_integrations"])
    for part in ("'/integrations'", "'/assignments'", "'integration_event'", "'integration_id': integration", "/config"):
        assert part in helper, part
    # #11180: the Trigger card lists only the agent's bound integrations, so the agent is bound
    # to the integrations it holds assignments on, read back, and the Gmail one is logged.
    for part in ("f'/agents/{agent}'", "'integration_bindings': bindings", "bound != bindings",
                 "'new_email'", "seeded['email event trigger']"):
        assert part in helper, part
    assert helper.index("'integration_bindings': bindings") < helper.index("'/assignments'")
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_event_integrations(token, base)" in seed
    assert seed.index("seed_event_integrations(token, base)") < seed.index("seeded.json")


def test_the_lab_seeds_a_fleet_whose_data_flow_holds_every_greyed_and_dense_state():
    """#9899: not connected, reads only, more than 4 events and a disabled trigger on real rows, read back."""
    from app.services.platform_events import PLATFORM_EVENTS

    calls: list[tuple[str, str, dict | None]] = []
    flow = {
        "nodes": [
            {"lane": "source", "kind": "integration", "name": "Gmail", "reason": "not connected", "events": ["new_email"]},
            {"lane": "source", "kind": "platform_event", "events": ["a", "b", "c", "d", "e", "f"]},
            {"lane": "output", "kind": "integration", "name": "GitHub"},
        ],
        "edges": [{"kind": "read"}, {"kind": "trigger", "reason": "disabled"}],
    }
    ids = iter(f"id{n}" for n in range(100))

    def api(method, path, token=None, body=None):
        calls.append((method, path, body))
        return flow if path.endswith("/flow") else {"id": next(ids)}

    names = ("seed_fleet_flow_states", "_seed_agent", "_created", "SEED_NAME", "FLOW_STATES_PLATFORM_EVENTS")
    ns = _lab_namespace(*names, api=api, LAB_AGENTS={}, LAB_EVENT_INTEGRATIONS={
        "Gmail": {"event": "new_email", "body": {"provider": "google", "auth_type": "oauth"}},
    })
    base = "http://localhost:3100"
    seeded = ns["seed_fleet_flow_states"]("token", base, "gh")
    fleet = seeded["fleet data flow states"]["id"]
    assert seeded["fleet data flow states"]["url"] == f"{base}/fleets/{fleet}/data-flow"
    assert len(ns["FLOW_STATES_PLATFORM_EVENTS"]) > 4
    assert set(ns["FLOW_STATES_PLATFORM_EVENTS"]) <= set(PLATFORM_EVENTS)
    assignments = [body for method, path, body in calls if (method, path) == ("POST", "/assignments")]
    kinds = [(a["trigger_type"], a["enabled"]) for a in assignments]
    assert kinds.count(("integration_event", True)) == 2 and ("schedule", False) in kinds
    assert len({a["agent_id"] for a in assignments if a["trigger_type"] == "integration_event"}) == 2
    assert any(
        body and body.get("integration_read_only") == {"github": ["gh"]} and body.get("integration_bindings") == {"github": ["gh"]}
        for _, _, body in calls
    )
    assert calls[-1][:2] == ("GET", f"/fleets/{fleet}/flow")

    # A flow missing a state is refused, naming it.
    flow["edges"] = [{"kind": "trigger", "reason": "disabled"}]
    with pytest.raises(RuntimeError, match="a read-only output"):
        ns["seed_fleet_flow_states"]("token", base, "gh")

    seed = _lab_source("seed_entities")
    assert "seed_fleet_flow_states(token, base, seeded['GitHub integration']['id'])" in seed
    assert "skipped['fleet data flow states']" in seed
    assert seed.index("seed_github_integration(token, base)") < seed.index("seed_fleet_flow_states(") < seed.index("seeded.json")


def test_the_lab_seeds_a_corporate_sized_fleet_data_flow_that_cannot_start_a_run():
    """#10009: 7 agents, 60 enabled triggers over 7 source families, 25+ outputs, read back; nothing can fire."""
    from croniter import croniter

    from app.api.v1.integration_catalog import PROVIDER_CATALOG, supported_auth_types
    from app.services.platform_events import PLATFORM_EVENTS, trigger_config_error

    calls: list[tuple[str, str, dict | None]] = []
    flow = {
        "nodes": [{"lane": "source"}] * 7 + [{"lane": "agent"}] * 7 + [{"lane": "output"}] * 31,
        "edges": [{"kind": "trigger"}] * 60,
    }
    ids = iter(f"id{n}" for n in range(1000))

    def api(method, path, token=None, body=None):
        calls.append((method, path, body))
        return flow if path.endswith("/flow") else {"id": next(ids)}

    names = (
        "seed_corporate_flow_fleet", "_seed_agent", "_created", "SEED_NAME", "CORPORATE_FLEET_AGENTS",
        "CORPORATE_EVENT_SOURCES", "CORPORATE_PLATFORM_TRIGGERS", "CORPORATE_NEVER_KEY", "CORPORATE_SCHEDULES",
        "CORPORATE_INBOXES", "CORPORATE_OUTPUTS",
    )
    ns = _lab_namespace(*names, api=api)
    base = "http://localhost:3100"
    seeded = ns["seed_corporate_flow_fleet"]("token", base)
    fleet = seeded["corporate data flow fleet"]["id"]
    assert seeded["corporate data flow fleet"]["url"] == f"{base}/fleets/{fleet}/data-flow"
    assert calls[-1][:2] == ("GET", f"/fleets/{fleet}/flow")

    agents = [body for method, path, body in calls if (method, path) == ("POST", "/agents")]
    assert len(agents) == 7
    handovers = [body for method, path, body in calls if method == "PUT" and path.startswith("/agents/")]
    assert sum(len(body["handover_agent_ids"]) for body in handovers) == 4

    # Every integration is a catalog provider with a supported auth type and a lab-only credential.
    created = [body for method, path, body in calls if (method, path) == ("POST", "/integrations")]
    for body in created:
        catalog = PROVIDER_CATALOG[body["provider"]]
        assert body["auth_type"] in (supported_auth_types(catalog) or [catalog["auth_type"]]), body["provider"]
        for key in ("client_id", "client_secret", "api_key"):
            assert key not in body or body[key].startswith("lab-only-"), (body["provider"], key)
    assert len({body["provider"] for body in created}) >= 25
    reads = [body for body in handovers if any(body["integration_read_only"].values())]
    assert reads, "some outputs are read-only"

    assignments = [body for method, path, body in calls if (method, path) == ("POST", "/assignments")]
    assert len(assignments) == 60 and all(a["enabled"] for a in assignments)
    families = {
        a["trigger_config"]["provider"] if a["trigger_type"] == "integration_event" else a["trigger_type"]
        for a in assignments
    }
    assert families == {"microsoft", "slack", "github", "hubspot", "platform_event", "schedule", "work_item"}
    for a in assignments:
        config = a["trigger_config"]
        if a["trigger_type"] == "integration_event":
            catalog_events = {e["event"] for e in PROVIDER_CATALOG[config["provider"]]["available_events"]}
            assert config["event"] in catalog_events, config
        elif a["trigger_type"] == "platform_event":
            # Nothing fires: a known event, filtered on a correlation key no item carries.
            assert trigger_config_error("platform_event", config) is None
            assert set(config["event"]) <= set(PLATFORM_EVENTS)
            assert config["filter"] == {"correlation_key": ns["CORPORATE_NEVER_KEY"]}
        elif a["trigger_type"] == "schedule":
            # 29 February only: valid, and never this year.
            assert croniter.is_valid(config["cron"]) and config["cron"].split()[2:4] == ["29", "2"]
        else:
            assert config["correlation_prefix"].startswith("lab-corporate-")

    # A flow short of the acceptance counts is refused, naming what is short.
    flow["edges"] = [{"kind": "trigger"}] * 54
    with pytest.raises(RuntimeError, match="triggers 54 of 55"):
        ns["seed_corporate_flow_fleet"]("token", base)

    seed = _lab_source("seed_entities")
    assert "seed_corporate_flow_fleet(token, base)" in seed and "skipped['corporate data flow fleet']" in seed
    assert seed.index("seed_corporate_flow_fleet(") < seed.index("seeded.json")


def test_the_lab_seeds_an_oauth_integration_whose_token_lacks_one_requested_scope():
    """#9813: the not-granted state renders with no fetch override; lab-only token, no outbound call."""
    import importlib.util

    from app.api.v1.integration_catalog import PROVIDER_CATALOG
    from app.integrations.granted_scopes import missing_scopes, split_scopes

    spec = importlib.util.spec_from_file_location("lab_seed_oauth_missing_scopes", LAB.parent / "seed_oauth_missing_scopes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    requested = split_scopes(PROVIDER_CATALOG["google"]["scopes"])
    granted, dropped = module.granted_without_one(requested)
    assert missing_scopes(requested, " ".join(granted)) == [dropped]
    assert dropped.lower() not in module.IDENTITY_SCOPES and module.LAB_ACCESS_TOKEN.startswith("lab-only-")
    assert module.granted_without_one(["openid", "a", "email"]) == (["openid", "email"], "a")

    script = ast.unparse(ast.parse((LAB.parent / "seed_oauth_missing_scopes.py").read_text(encoding="utf-8")))
    for part in ("encrypt_value(LAB_ACCESS_TOKEN)", "row.granted_scopes", "row.status = 'connected'",
                 "row.refresh_token_enc = None", "missing_scopes(requested"):
        assert part in script, part
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    declared = next(
        node.value for node in tree.body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "LAB_OAUTH_MISSING_SCOPES_BODY"
    )
    body = ast.literal_eval(declared)
    assert body["provider"] == "google" and body["auth_type"] == "oauth"
    assert body["client_id"].startswith("lab-only-") and body["client_secret"].startswith("lab-only-")
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_oauth_missing_scopes"])
    for part in ("'seed_oauth_missing_scopes.py'", "sys.executable", "env=seed_script_env()", "'/integrations'",
                 "'missing_scopes'", "'OAuth missing scopes'", "stderr_summary(process.stderr)"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    call = "seed_oauth_missing_scopes(token, base)"
    assert call in seed and "skipped['OAuth missing scopes']" in seed
    assert seed.index(call) < seed.index("seeded.json")


def test_the_lab_seeds_disabled_mcp_servers_one_of_them_oauth_connected():
    """#9999: stdio, secret headers and OAuth connected, disabled; lab-only values, no network."""
    import importlib.util

    from app.api.v1.schemas.mcp_server import MCPServerCreate

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    declared = next(
        node.value for node in tree.body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "LAB_MCP_SERVER_BODIES"
    )
    bodies = ast.literal_eval(declared)
    assert set(bodies) == {"stdio", "secret headers", "OAuth connected"}
    for label, body in bodies.items():
        created = MCPServerCreate(**body, name=f"Lab sample MCP {label}")
        assert created.enabled is False, label
        if created.url:
            assert created.url.startswith("https://") and ".lab.invalid/" in created.url, label
    stdio = MCPServerCreate(**bodies["stdio"], name="s")
    assert stdio.transport == "stdio" and stdio.command and stdio.args and len(stdio.env or {}) == 1
    headers = MCPServerCreate(**bodies["secret headers"], name="h")
    assert headers.transport == "streamable_http" and headers.auth_mode == "headers" and len(headers.headers or {}) == 1
    oauth = MCPServerCreate(**bodies["OAuth connected"], name="o")
    assert oauth.auth_mode == "oauth" and ".lab.invalid/" in (oauth.token_url or "")
    values = [v for body in bodies.values() for field in ("env", "headers") for v in (body.get(field) or {}).values()]
    values += [bodies["OAuth connected"]["client_id"], bodies["OAuth connected"]["client_secret"]]
    assert values and all("lab-only-" in value for value in values)

    spec = importlib.util.spec_from_file_location("lab_seed_mcp_oauth_connected", LAB.parent / "seed_mcp_oauth_connected.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.LAB_ACCESS_TOKEN.startswith("lab-only-")
    script = ast.unparse(ast.parse((LAB.parent / "seed_mcp_oauth_connected.py").read_text(encoding="utf-8")))
    for part in ("encrypt_value(LAB_ACCESS_TOKEN)", "row.refresh_token_enc = None", "row.oauth_status = 'connected'",
                 "row.auth_mode != 'oauth' or row.enabled", "timedelta(days=365)"):
        assert part in script, part

    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_mcp_servers"])
    for part in ("'seed_mcp_oauth_connected.py'", "sys.executable", "env=seed_script_env()", "'/mcp-servers'",
                 "'oauth_status'", "'connected'", "/mcp-servers/{server}", "stderr_summary(process.stderr)"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    call = "seed_mcp_servers(token, base)"
    assert call in seed and "skipped['MCP servers']" in seed
    assert seed.index(call) < seed.index("seeded.json")


def test_the_lab_seeds_one_stored_credential_secret_vault():
    """#10557: a database HashiCorp AppRole provider the real create route accepts; lab-only values, no network."""
    from app.api.v1.secret_vaults import SecretVaultProviderCreate
    from app.services.vault.config import DATABASE_CREDENTIALS, DATABASE_SETTINGS, database_config
    from app.services.vault.resolver import build_provider

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    declared = next(
        node.value for node in tree.body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "LAB_SECRET_VAULT_BODY"
    )
    body = ast.literal_eval(ast.unparse(declared).replace("f'{SEED_NAME} vault'", "'Lab sample vault'"))
    created = SecretVaultProviderCreate(**body)
    assert created.name == "lab_vault" and created.type == "hashicorp" and created.enabled
    assert set(created.settings) <= {s.lower() for s in DATABASE_SETTINGS["hashicorp"]}
    assert set(created.credentials) == {"secret_id"} <= {s.lower() for s in DATABASE_CREDENTIALS["hashicorp"]}
    assert created.settings["auth_method"] == "approle"
    assert created.settings["address"].startswith("https://") and ".lab.invalid:" in created.settings["address"]
    assert all("lab-only-" in value for value in (created.settings["role_id"], created.credentials["secret_id"]))
    config = database_config(
        name=created.name, type=created.type, settings=created.settings, credentials=created.credentials,
        display_name=created.display_name, enabled=True, grants=[], browsable=False, cache_seconds=None,
    )
    build_provider(config)

    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_secret_vault"])
    for part in ("'/settings/secret-vaults'", "'POST'", "'GET'", "'database'", "'credentials_set'", "'secret_id'",
                 "'lab-only-vault-secret-id' in json.dumps(row)", "/settings/security/vaults"):
        assert part in helper, part
    assert helper.index("if listed() is None") < helper.index("'POST'")
    seed = ast.unparse(functions["seed_entities"])
    call = "seed_secret_vault(token, base)"
    assert call in seed and "skipped['secret vault']" in seed
    assert seed.index(call) < seed.index("seeded.json")


def test_the_lab_serves_an_openapi_spec_listing_every_auth_kind_and_seeds_an_integration_from_it():
    """#9904: the OpenAPI auth form is reviewed on a served spec, no hand-built file; lab-only values."""
    import json

    from app.services.openapi_import.document import parse_spec_document
    from app.services.openapi_import.operations import analyze_spec

    tree = ast.parse(LAB.read_text(encoding="utf-8"))

    def declared(name: str):
        return ast.literal_eval(next(
            node.value for node in tree.body
            if (isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == name)
            or (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets))
        ))

    spec, kinds = declared("LAB_OPENAPI_SPEC"), declared("LAB_OPENAPI_KINDS")
    analysis = analyze_spec(parse_spec_document(json.dumps(spec)))
    listed = {option.kind for option in analysis.auth_options}
    assert set(kinds) <= listed, set(kinds) - listed
    assert {"api_key", "bearer", "oauth2_client_credentials", "oauth2_password", "oauth2_authorization_code",
            "oauth2_implicit", "unsupported"} <= set(kinds)
    assert analysis.auth_option("ApiKeyHeader").kind == "api_key"
    assert analysis.default_server_url == "https://api.lab.invalid/v1"

    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_openapi_spec"])
    for part in ("lab_private_address()", "'/integrations/openapi/preview'", "'/integrations'", "'provider': 'openapi'",
                 "'auth_option': 'ApiKeyHeader'", "'lab-only-openapi-key'", "'OpenAPI spec'", "'OpenAPI integration'",
                 "/config", "LAB_OPENAPI_KINDS"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    call = "seed_openapi_spec(token, base)"
    assert call in seed and "skipped['OpenAPI spec']" in seed
    assert seed.index(call) < seed.index("seeded.json")


def test_the_seeded_routes_are_routes_of_the_frontend():
    app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    for route in ("/operations/executions/:id", "/fleets/:fleetId", "/tools/:id", "/templates/:id", "/investigation-packs/:id",
                  "/connections/relays/:id",
                  "/evaluations/datasets/:id", "/evaluations/:evaluationId/compare", "/evaluations/:id", "/ctem/:stage",
                  "/tasks/workspaces/:workspaceId", "/tasks/work-items/:itemId", "/tasks/value", "/users-and-groups/users/:id",
                  "/operations/shared-state", "/videos/:id",
                  "/integrations/:id/:tab?"):
        assert f'path="{route}' in app, route


def test_the_ctem_lab_is_the_same_lab_with_the_ctem_sample_and_the_default_sets_none():
    services = _services()
    lab, ctem = services["lab"], services["lab-ctem"]
    assert ctem["env"]["LAB_CTEM_SAMPLE"] == "1"
    assert "LAB_CTEM_SAMPLE" not in lab["env"]
    assert {k: v for k, v in ctem["env"].items() if k != "LAB_CTEM_SAMPLE"} == lab["env"]
    assert {k: v for k, v in ctem.items() if k not in ("env", "description")} == {
        k: v for k, v in lab.items() if k not in ("env", "description")
    }


def _ctem_sample_module(monkeypatch):
    import importlib

    monkeypatch.syspath_prepend(str(LAB.parent))
    return importlib.import_module("ctem_sample")


def test_the_ctem_sample_scores_a_blind_spot_and_a_moving_history(monkeypatch):
    """OpenAEV missing on its primary stages, Validation null, one stage up and one down, every day scored."""
    from app.services.ctem_scoring import compute_posture

    sample = _ctem_sample_module(monkeypatch)
    assert sample.VERTICAL == "cyber" and sample.PRESENT == ("opencti",)
    assert sorted(sample.DAYS_AGO) == [0, 1, 2, 3]
    days = {d: compute_posture(sample.stage_facts(d), set(sample.PRESENT), sample.VERTICAL) for d in sample.DAYS_AGO}
    scores = {d: {s["key"]: s["score"] for s in p["stages"]} for d, p in days.items()}
    for day in scores.values():
        assert day["validation"] is None
        assert all(day[k] is not None for k in ("scoping", "discovery", "prioritization", "remediation"))
    assert scores[3]["scoping"] < scores[0]["scoping"]
    assert scores[3]["discovery"] > scores[0]["discovery"]
    assert len({p["posture_score"] for p in days.values()}) > 1
    assert all(p["posture_score"] is not None for p in days.values())
    today = {s["key"]: s for s in days[0]["stages"]}
    assert "openaev" in today["scoping"]["primary_products"] and "openaev" in today["validation"]["primary_products"]
    sample.stage_facts(0)["scoping"]["scope_completeness_pct"] = -1
    assert sample.stage_facts(0)["scoping"]["scope_completeness_pct"] == 72


def test_the_ctem_history_seed_plans_the_recorded_days_before_today(monkeypatch):
    import importlib.util
    from datetime import date

    _ctem_sample_module(monkeypatch)
    spec = importlib.util.spec_from_file_location("lab_seed_ctem_history", LAB.parent / "seed_ctem_history.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    today = date(2026, 11, 6)
    rows = module.plan(today)
    assert [r["snapshot_date"] for r in rows] == [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
    assert all(r["vertical"] == "cyber" and r["posture_score"] is not None for r in rows)
    assert all(set(r["stage_metrics"]) == {"scoping", "discovery", "prioritization", "validation", "remediation"}
               for r in rows)
    assert rows[0]["stage_metrics"]["validation"]["score"] is None
    assert rows[0]["stage_metrics"]["scoping"]["scope_completeness_pct"] == 55


def _posture_history_module(monkeypatch):
    import importlib.util

    monkeypatch.syspath_prepend(str(LAB.parent))
    spec = importlib.util.spec_from_file_location(
        "lab_seed_ctem_posture_history", LAB.parent / "seed_ctem_posture_history.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_ctem_posture_history_seed_plans_90_days_with_one_shape_per_stage(monkeypatch):
    """Discovery up, Prioritization down, Scoping flat, Validation old only, Remediation one day."""
    from datetime import date, timedelta

    module = _posture_history_module(monkeypatch)
    today = date(2026, 11, 7)
    rows = module.plan(today)
    dates = [r["snapshot_date"] for r in rows]
    assert dates == sorted(dates) and len(set(dates)) == len(dates)
    assert dates[0] == today - timedelta(days=90) and dates[-1] == today
    assert {today - timedelta(days=d) for d in (0, 1, 2, 3, 4, 5)} <= set(dates)
    today_row = rows[-1]["stage_metrics"]
    assert today_row["discovery"]["score"] is not None and today_row["scoping"]["score"] is not None
    assert today_row["remediation"]["score"] is None and today_row["validation"]["score"] is None
    assert all(r["vertical"] == "cyber" and r["posture_score"] is not None for r in rows)

    def scores(stage, days):
        since = today - timedelta(days=days)
        return [r["stage_metrics"][stage]["score"] for r in rows if r["snapshot_date"] >= since]

    def change(values):
        known = [v for v in values if v is not None]
        return known[-1] - known[0] if len(known) >= 2 else None

    assert change(scores("discovery", 30)) > 0
    assert change(scores("discovery", 90)) > change(scores("discovery", 30))
    assert change(scores("prioritization", 90)) < 0
    assert change(scores("scoping", 30)) == 0 and change(scores("scoping", 90)) == 0
    assert change(scores("validation", 30)) is None and change(scores("validation", 90)) is not None
    assert change(scores("remediation", 90)) is None
    assert sum(v is not None for v in scores("remediation", 90)) == 1


def test_the_lab_seeds_the_ctem_posture_history_outside_the_ctem_sample_labs():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_ctem_posture_history"])
    for part in ("'seed_ctem_posture_history.py'", "sys.executable", "env=seed_script_env()",
                 "stderr_summary(process.stderr)", "/ctem/discovery"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seeded['CTEM posture history'] = seed_ctem_posture_history(token, base)" in seed
    assert "skipped['CTEM posture history']" in seed
    assert seed.index("seed_ctem_posture_history(token, base)") < seed.index("seeded.json")
    assert "if seeds_ctem_posture_history():" in seed
    guard = ast.unparse(functions["seeds_ctem_posture_history"])
    assert "os.environ.get('LAB_CTEM_SAMPLE') != '1'" in guard
    assert "lab_switch('LAB_CTEM_SAMPLE_STATE', LAB_CTEM_SAMPLE_STATES) == 'off'" in guard
    assert "('xtm_one', 'ai_disabled')" in guard
    env = ast.unparse(functions["backend_env"])
    assert "'LAB_CTEM_POSTURE_HISTORY': '1' if seeds_ctem_posture_history() else '0'" in env


def test_the_ctem_posture_history_seed_upserts_today_and_keeps_earlier_days():
    """Today replaces an empty live row recorded before the seed; an earlier day already recorded is kept."""
    source = ast.unparse(ast.parse((LAB.parent / "seed_ctem_posture_history.py").read_text(encoding="utf-8")))
    assert "if row['snapshot_date'] == today:" in source
    assert source.index("on_conflict_do_update") < source.index("on_conflict_do_nothing")


def test_lab_asgi_keeps_the_seeded_posture_today_only_when_asked(monkeypatch):
    """Under ``LAB_CTEM_POSTURE_HISTORY=1`` a stored cyber today row is written back as stored; else the live one."""
    import asyncio
    import types

    module, _ = _lab_asgi_module(monkeypatch)
    recorded: list[tuple] = []

    async def record(db, user_id, vertical, posture, stage_facts):
        recorded.append((vertical, posture, stage_facts))
        return [], {}

    monkeypatch.setattr(module.ctem_metrics, "_record_and_load_history", record)
    assert module.keep_seeded_posture_today({}) is False
    assert module.keep_seeded_posture_today({"LAB_CTEM_POSTURE_HISTORY": "0"}) is False
    assert module.ctem_metrics._record_and_load_history is record

    stored = types.SimpleNamespace(
        posture_score=66,
        stage_metrics={
            "scoping": {"score": 70, "scope_completeness_pct": 70.0},
            "discovery": {"score": 67, "detection_coverage_pct": 67.0},
            "remediation": {"score": None},
        },
    )
    rows = {"cyber": stored}

    async def stored_today(db, user_id, vertical):
        return rows.get(vertical)

    monkeypatch.setattr(module, "_stored_today", stored_today)
    assert module.keep_seeded_posture_today({"LAB_CTEM_POSTURE_HISTORY": "1"}) is True
    live = ({"posture_score": 0, "stages": [{"key": "scoping", "score": 0}]}, {"scoping": {}})

    asyncio.run(module.ctem_metrics._record_and_load_history(None, "u", "cyber", *live))
    _, posture, facts = recorded[-1]
    assert posture["posture_score"] == 66
    assert {s["key"]: s["score"] for s in posture["stages"]} == {"scoping": 70, "discovery": 67, "remediation": None}
    assert facts["discovery"] == {"detection_coverage_pct": 67.0} and facts["remediation"] == {}

    rows.clear()
    asyncio.run(module.ctem_metrics._record_and_load_history(None, "u", "cyber", *live))
    assert recorded[-1][1:] == live
    asyncio.run(module.ctem_metrics._record_and_load_history(None, "u", "health", *live))
    assert recorded[-1] == ("health", *live)


def test_the_large_ctem_facts_lay_counts_over_the_live_cyber_facts_and_move_no_score(monkeypatch):
    """``LAB_CTEM_POSTURE_HISTORY=1`` without a CTEM sample switch: each stage shows a count of 1,000 and more."""
    import asyncio

    from app.services.ctem_scoring import compute_posture

    module, _ = _lab_asgi_module(monkeypatch)
    original = module.ctem_metrics.build_posture_for_user
    assert module.large_ctem_facts({}) is False
    assert module.large_ctem_facts({"LAB_CTEM_POSTURE_HISTORY": "1", "LAB_CTEM_SAMPLE": "1"}) is False
    for switch in module.CTEM_SAMPLE_SWITCHES:
        assert module.large_ctem_facts({"LAB_CTEM_POSTURE_HISTORY": "1", switch: "on"}) is False
    assert module.ctem_metrics.build_posture_for_user is original

    live_facts = {"discovery": {"detection_coverage_pct": 48.0, "intrusion_sets": 0}}

    async def live(db, user_id, vertical="cyber"):
        return {k: dict(v) for k, v in live_facts.items()}, {"opencti"}

    monkeypatch.setattr(module.ctem_metrics, "build_posture_for_user", live)
    assert module.large_ctem_facts({"LAB_CTEM_POSTURE_HISTORY": "1"}) is True
    facts, present = asyncio.run(module.ctem_metrics.build_posture_for_user(None, "u", "cyber"))
    assert present == {"opencti"}
    assert facts["discovery"]["detection_coverage_pct"] == 48.0 and facts["discovery"]["intrusion_sets"] == 1240
    for stage in ("scoping", "discovery", "prioritization", "validation", "remediation"):
        values = list(module.ctem_sample.LARGE_FACTS[stage].values())
        assert any(v >= 1000 for v in values) and any(v < 1000 for v in values), stage
        assert set(module.ctem_sample.LARGE_FACTS[stage]) <= set(facts[stage])
    scores = {s["key"]: s["score"] for s in compute_posture(facts, present)["stages"]}
    assert scores == {s["key"]: s["score"] for s in compute_posture(live_facts, present)["stages"]}
    assert asyncio.run(module.ctem_metrics.build_posture_for_user(None, "u", "fimi"))[0] == live_facts


def test_the_ctem_module_states_answer_their_products_and_the_discovery_lists(monkeypatch):
    """``LAB_CTEM_MODULES`` (#11998): each state answers its products, the Discovery lists, no OpenAEV fact without it."""
    import asyncio

    module, _ = _lab_asgi_module(monkeypatch)
    original = module.ctem_metrics.build_posture_for_user
    assert module.modules_ctem_posture({}) is False
    assert module.modules_ctem_posture({"LAB_CTEM_MODULES": "bogus"}) is False
    assert module.ctem_metrics.build_posture_for_user is original
    assert module.large_ctem_facts({"LAB_CTEM_POSTURE_HISTORY": "1", "LAB_CTEM_MODULES": "cti"}) is False
    for state, products in {"cti": {"opencti"}, "cti-aev": {"opencti", "openaev"}}.items():
        monkeypatch.setattr(module.ctem_metrics, "build_posture_for_user", original)
        assert module.modules_ctem_posture({"LAB_CTEM_MODULES": state}) is True
        facts, present = asyncio.run(module.ctem_metrics.build_posture_for_user(None, "u", "cyber"))
        assert present == products
        for key in ("techniques_by_tactic", "top_threats", "target_sectors", "target_countries"):
            assert facts["discovery"][key], (state, key)
        # OpenAEV's validation results only with OpenAEV; OpenCTI's Validation lists in every state (#11998).
        assert ("prevention_success_pct" in facts["validation"]) is ("openaev" in products)
        for key in ("rule_coverage_by_tactic", "detection_depth"):
            assert facts["validation"][key], (state, key)
        # OpenCTI's Mobilization lists in every state, OpenAEV's fix backlog only with OpenAEV (#11998).
        for key in ("response_mix", "response_progress"):
            assert facts["remediation"][key], (state, key)
        assert ("fix_backlog" in facts["remediation"]) is ("openaev" in products)
    assert module.ctem_sample.MODULE_STATES["all"] == ("opencti", "openaev", "opencrq")


def test_the_ctem_module_labs_answer_every_stage_card_with_no_fetch_override(monkeypatch):
    """#12220: GET /ctem-metrics/posture of each module lab carries Scoping, Prioritization and (OpenAEV) Validation viz."""
    import asyncio

    from app.api.v1.ctem_metrics import _stage_display
    from app.services.ctem_scoring import compute_posture

    module, _ = _lab_asgi_module(monkeypatch)
    original = module.ctem_metrics.build_posture_for_user
    for state in ("cti", "cti-aev", "all", "cti-unscored"):
        monkeypatch.setattr(module.ctem_metrics, "build_posture_for_user", original)
        assert module.modules_ctem_posture({"LAB_CTEM_MODULES": state}) is True
        facts, present = asyncio.run(module.ctem_metrics.build_posture_for_user(None, "u", "cyber"))
        viz = {stage: _stage_display(stage, facts.get(stage, {}), "cyber")["viz"] for stage in facts}
        scoping = viz["scoping"]
        types = {row["name"] for row in scoping["observables_by_type"]}
        assert len(types) == 8 and {"StixFile", "Autonomous-System"} <= types, state
        assert "UNKNOWN" in {row["label"] for row in scoping["vulnerabilities_by_severity"]}, state
        prio = viz["prioritization"]
        assert prio["top_vulnerabilities"] and prio["severity_distribution"], state
        assert prio["funnel"] and prio["funnel"]["layers"], state
        with_aev = "openaev" in present
        assert (prio["findings"] == {"total": 89, "with_cve": 7}) is with_aev, state
        assert (prio["findings"] is None) is not with_aev, state
        matrix = viz["validation"].get("outcome_matrix")
        assert bool(matrix) is with_aev, state
        if matrix:
            assert matrix["chains"][0]["id"] == "mitre-attack" and matrix["chains"][0]["default"] is True
            assert {item["outcome"] for item in matrix["items"]} == {"prevented", "detected", "missed", "untested"}
        discovery = {s["key"]: s["score"] for s in compute_posture(facts, present)["stages"]}["discovery"]
        # The unscored state is the page's scoreNeedsOpenAev path: OpenCTI alone, Discovery at 0.
        assert (discovery == 0) is (state == "cti-unscored"), (state, discovery)
    assert module.ctem_sample.MODULE_STATES["cti-unscored"] == ("opencti",)

    monkeypatch.setattr(module.ctem_metrics, "build_posture_for_user", original)
    assert module.modules_ctem_posture({"LAB_CTEM_MODULES": "none"}) is True
    assert asyncio.run(module.ctem_metrics.build_posture_for_user(None, "u", "cyber")) == ({}, set())
    assert module.large_ctem_facts({"LAB_CTEM_POSTURE_HISTORY": "1", "LAB_CTEM_MODULES": "none"}) is False


def test_the_ctem_module_labs_have_their_states_and_light_twins():
    """#12220: one service per ``LAB_CTEM_MODULES`` state, each with a light twin identical but ``LAB_THEME``."""
    services = _services()
    for state in ("cti", "cti-aev", "all", "cti-unscored", "none"):
        dark, light = services[f"lab-ctem-modules-{state}"], services[f"lab-ctem-modules-{state}-light"]
        assert dark["env"]["LAB_CTEM_MODULES"] == state and "LAB_THEME" not in dark["env"]
        assert light["env"] == {**dark["env"], "LAB_THEME": "light"}
        assert {k: v for k, v in light.items() if k not in ("env", "description")} == {
            k: v for k, v in dark.items() if k not in ("env", "description")
        }


def test_the_large_facts_switches_are_one_list_and_spare_the_opencti_lab(monkeypatch):
    """#12220: ``lab_asgi`` and ``lab.seeds_ctem_large_facts`` share the switches; ``LAB_CTEM_OPENCTI`` keeps 3 / 4."""
    module, _ = _lab_asgi_module(monkeypatch)
    assert "LAB_CTEM_OPENCTI" in module.CTEM_SAMPLE_SWITCHES
    assert module.large_ctem_facts({"LAB_CTEM_POSTURE_HISTORY": "1", "LAB_CTEM_OPENCTI": "on"}) is False
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    listed = next(
        node.value
        for node in ast.walk(functions["seeds_ctem_large_facts"])
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "switches" for t in node.targets)
    )
    assert ast.literal_eval(listed) == module.CTEM_SAMPLE_SWITCHES
    # The OpenCTI lab reads its KEV and critical KPIs back against the seeded OpenCTI (3 KEV, 4 critical).
    ns = _lab_namespace("CTEM_OPENCTI_VULNERABILITIES", "CTEM_OPENCTI_KPI_FACTS", "ctem_opencti_expected")
    expected = ns["ctem_opencti_expected"]()
    assert (expected["kev"], expected["critical"]) == (3, 4)
    assert set(ns["CTEM_OPENCTI_KPI_FACTS"].values()) == {"kev", "critical"}
    seed = ast.unparse(functions["seed_ctem_opencti"])
    assert "CTEM_OPENCTI_KPI_FACTS" in seed and "prio.get('facts')" in seed


def test_the_large_ctem_facts_name_only_displayed_cyber_count_facts():
    """Every key of ``LARGE_FACTS`` is a count fact the stage card lists, so each shows on ``/ctem/<stage>``."""
    import importlib.util

    from app.services.ctem_verticals import STAGE_FACTS

    spec = importlib.util.spec_from_file_location("lab_ctem_sample_large", LAB.parent / "ctem_sample.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for stage, counts in module.LARGE_FACTS.items():
        listed = {key for _, key, unit, kind in STAGE_FACTS["cyber"][stage] if kind == "fact" and unit == ""}
        assert set(counts) <= listed, (stage, set(counts) - listed)


def test_the_lab_seeds_a_company_dashboard_of_large_figures():
    """The definition validates; the value tile carries its change and previous value, the console outcomes over 1,000."""
    from app.services.dashboard_definition import validate_definition

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    namespace: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            names = [t.id for t in (node.targets if isinstance(node, ast.Assign) else [node.target]) if isinstance(t, ast.Name)]
            if set(names) & {"LAB_DASHBOARD_FIGURES_TITLE", "LAB_DASHBOARD_FIGURES"}:
                exec(compile(ast.Module(body=[node], type_ignores=[]), "lab.py", "exec"), namespace)  # noqa: S102
    definition = validate_definition(namespace["LAB_DASHBOARD_FIGURES"])
    assert definition["title"] == "Lab sample figures"
    tile = definition["dataSources"]["findings"]["options"]["data"]
    assert tile["value"] >= 1000 and tile["previous"] is not None and tile["change"] is not None
    assert tile["period"] == "interval"
    assert definition["dataSources"]["kev"]["options"]["data"]["value"] < 1000
    assert definition["visualizations"]["console"]["type"] == "xtm.exposureconsole"
    items = definition["dataSources"]["outcomes"]["options"]["data"]["items"]
    assert any(i["success"] >= 1000 for i in items) and any(i["success"] < 1000 for i in items)

    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_dashboard_figures"])
    for part in ("'POST', '/custom-dashboards'", "/definition", "/sharing", "'is_company_managed': True", "/dashboards/"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seeded['dashboard figures'] = seed_dashboard_figures(token, base)" in seed
    assert "seeded['CTEM large facts']" in seed
    assert seed.index("seed_dashboard_figures(token, base)") < seed.index("seeded.json")


def test_the_ctem_sample_answers_the_cyber_collection_only_when_asked(monkeypatch):
    import asyncio

    from app.api.v1 import ctem_metrics

    original = ctem_metrics.build_posture_for_user
    module, _ = _lab_asgi_module(monkeypatch)
    assert ctem_metrics.build_posture_for_user is original
    assert module.sample_ctem_posture({}) is False
    assert ctem_metrics.build_posture_for_user is original

    async def live(db, user_id, vertical="cyber"):
        return {"live": {}}, {vertical}

    monkeypatch.setattr(ctem_metrics, "build_posture_for_user", live)
    assert module.sample_ctem_posture({"LAB_CTEM_SAMPLE": "1"}) is True
    facts, present = asyncio.run(ctem_metrics.build_posture_for_user(None, "u", "cyber"))
    assert present == {"opencti"} and facts["scoping"]["scope_completeness_pct"] == 72
    assert asyncio.run(ctem_metrics.build_posture_for_user(None, "u", "fimi")) == ({"live": {}}, {"fimi"})


def test_the_lab_seeds_the_ctem_history_and_leaves_openaev_unregistered_only_under_the_flag():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_ctem_sample"])
    for part in ("'seed_ctem_history.py'", "sys.executable", "env=seed_script_env()", "'CTEM coverage and history'",
                 "stderr_summary(process.stderr)"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_ctem_sample(token, base)" in seed and "skipped['CTEM coverage and history']" in seed
    assert seed.index("seed_ctem_sample(token, base)") < seed.index("seeded.json")
    assert "ctem_sample_unregistered()" in ast.unparse(functions["seed_suite_registrations"])
    unregistered = ast.unparse(functions["ctem_sample_unregistered"])
    assert "('openaev',) if os.environ.get('LAB_CTEM_SAMPLE') == '1' else ()" in unregistered


def test_the_ctem_sample_state_labs_are_the_same_lab_with_the_switch_and_the_default_sets_none():
    services = _services()
    lab = services["lab"]
    for name, switches in {
        "lab-ctem-sample": {"LAB_CTEM_SAMPLE_STATE": "on"},
        "lab-ctem-sample-light": {"LAB_CTEM_SAMPLE_STATE": "on", "LAB_THEME": "light"},
    }.items():
        service = services[name]
        assert service["env"] == {**lab["env"], **switches}, name
        assert {k: v for k, v in service.items() if k not in ("env", "description")} == {
            k: v for k, v in lab.items() if k not in ("env", "description")
        }, name
    assert "LAB_CTEM_SAMPLE_STATE" not in lab["env"] and "LAB_CTEM_SAMPLE_STATE" not in services["lab-ctem"]["env"]


def test_the_ctem_sample_state_answers_no_product_only_when_asked(monkeypatch):
    import asyncio

    from app.api.v1 import ctem_metrics
    from app.services.ctem_scoring import compute_posture

    original = ctem_metrics.build_posture_for_user
    module, _ = _lab_asgi_module(monkeypatch)
    assert ctem_metrics.build_posture_for_user is original
    assert module.empty_ctem_posture({}) is False
    assert module.empty_ctem_posture({"LAB_CTEM_SAMPLE": "1"}) is False
    assert ctem_metrics.build_posture_for_user is original
    monkeypatch.setattr(ctem_metrics, "build_posture_for_user", original)
    assert module.empty_ctem_posture({"LAB_CTEM_SAMPLE_STATE": "on"}) is True
    for vertical in ("cyber", "fimi", "fraud"):
        facts, present = asyncio.run(ctem_metrics.build_posture_for_user(None, "u", vertical))
        assert (facts, present) == ({}, set())
        posture = compute_posture(facts, present, vertical)
        # The command center's sample state: no composite score, no stage covered.
        assert posture["posture_score"] is None, vertical
        assert not any(s["covered"] or s["scored"] for s in posture["stages"]), vertical


def test_the_ctem_sample_state_leaves_opencrq_unregistered_only_under_the_switch():
    names = ("LAB_CTEM_SAMPLE_STATES", "lab_switch", "ctem_sample_unregistered")
    assert _lab_namespace(*names)["ctem_sample_unregistered"]() == ()
    assert _lab_namespace(*names, env={"LAB_CTEM_SAMPLE": "1"})["ctem_sample_unregistered"]() == ("openaev",)
    switched = _lab_namespace(*names, env={"LAB_CTEM_SAMPLE_STATE": "on"})
    assert switched["ctem_sample_unregistered"]() == ("opencrq",)


def test_the_ctem_sample_state_is_read_back_after_the_fan_out_runs_are_deleted():
    calls: list[tuple[str, str]] = []
    sample_posture = {
        "posture_score": None,
        "products": {p: {"present": False} for p in ("opencti", "openaev", "opencrq")},
        "stages": [{"key": k, "covered": False} for k in ("scoping", "discovery")],
    }
    answers: dict = {
        ("GET", "/assignments/runs/all?limit=500"): [
            {"id": "r1", "status": "pending", "trigger_context": {"ctem_vertical": "cyber"}},
            {"id": "r2", "status": "completed", "trigger_context": {"ctem_vertical": "cyber"}},
            {"id": "r3", "status": "running", "trigger_context": {}},
        ],
        ("GET", "/ctem-metrics/posture"): sample_posture,
        ("GET", "/ctem-metrics/briefing"): {"status": "pending", "generating": False},
    }

    def api(method, path, token=None, body=None):
        calls.append((method, path))
        return answers.get((method, path), {})

    import types

    names = ("ctem_sample_state_gaps", "seed_ctem_sample_state")
    sleeps: list[int] = []
    ns = _lab_namespace(*names, api=api, time=types.SimpleNamespace(sleep=sleeps.append))
    base = "http://localhost:3100"
    assert ns["seed_ctem_sample_state"]("t", base) == {"id": "ctem-sample-state", "url": f"{base}/"}
    assert [p for m, p in calls if m == "DELETE"] == ["/assignments/runs/r1"]
    assert sleeps == []

    gaps = ns["ctem_sample_state_gaps"]
    assert gaps(sample_posture, {"status": "pending", "generating": False}) == []
    live = {**sample_posture, "posture_score": 61, "products": {"opencti": {"present": True}},
            "stages": [{"key": "scoping", "covered": True}]}
    assert gaps(live, {"status": "generating", "generating": True}) == [
        "posture_score is 61, not null", "products present: opencti", "stages covered: scoping",
        "the briefing answers status 'generating', generating True",
    ]
    answers[("GET", "/ctem-metrics/briefing")] = {"status": "generating", "generating": True}
    with pytest.raises(RuntimeError, match="not reached"):
        ns["seed_ctem_sample_state"]("t", base)
    assert sleeps == [5, 5, 5, 5]

    seed = _lab_source("seed_entities")
    assert "seed_ctem_sample_state(token, base)" in seed and "skipped['CTEM sample state']" in seed
    assert seed.index("seed_ctem_sample_state(token, base)") < seed.index("seeded.json")
    assert seed.index("seed_fleet_runs(token, base)") < seed.index("seed_ctem_sample_state(token, base)")


def test_the_ctem_findings_labs_are_the_same_lab_with_the_switch_and_ai_off():
    services = _services()
    lab = services["lab"]
    for name, switches in {
        "lab-ctem-findings": {"LAB_CTEM_FINDINGS": "on", "LAB_AI": "off"},
        "lab-ctem-findings-light": {"LAB_CTEM_FINDINGS": "on", "LAB_AI": "off", "LAB_THEME": "light"},
    }.items():
        service = services[name]
        assert service["env"] == {**lab["env"], **switches}, name
        assert {k: v for k, v in service.items() if k not in ("env", "description")} == {
            k: v for k, v in lab.items() if k not in ("env", "description")
        }, name
    assert not any("LAB_CTEM_FINDINGS" in s.get("env", {}) for n, s in services.items() if not n.startswith("lab-ctem-findings"))


def test_the_ctem_fleets_off_lab_is_the_same_lab_with_the_opt_in_switch_off_and_the_default_sets_none():
    """#9610: the default lab seeds the mixed opt-in states; ``lab-ctem-fleets-off`` (and its light twin) keeps the invitation."""
    services = _services()
    lab = services["lab"]
    twins = {
        "lab-ctem-fleets-off": {"LAB_CTEM_FLEET_OPTIN": "off"},
        "lab-ctem-fleets-off-light": {"LAB_CTEM_FLEET_OPTIN": "off", "LAB_THEME": "light"},
    }
    for name, switches in twins.items():
        off = services[name]
        assert off["env"] == {**lab["env"], **switches}, name
        assert {k: v for k, v in off.items() if k not in ("env", "description")} == {
            k: v for k, v in lab.items() if k not in ("env", "description")
        }, name
    assert not any("LAB_CTEM_FLEET_OPTIN" in s.get("env", {}) for n, s in services.items() if n not in twins)
    namespace = _lab_namespace("LAB_CTEM_FLEET_OPTIN_STATES", "lab_switch")
    assert namespace["lab_switch"]("LAB_CTEM_FLEET_OPTIN", namespace["LAB_CTEM_FLEET_OPTIN_STATES"]) == "mixed"


def test_the_ctem_fleets_off_lab_switches_every_ctem_fleet_off_and_reads_it_back():
    """#9703: ``LAB_CTEM_FLEET_OPTIN=off`` writes and verifies every CTEM fleet not set up, never a skip."""
    from app.services.assignment_pause import EXPECTED_REASONS

    module = _seed_ctem_fleet_optin_module()
    # An expected reason: the fleet reads Not set up, never Paused.
    assert module.ALL_OFF_REASON in EXPECTED_REASONS
    script = ast.parse((LAB.parent / "seed_ctem_fleet_optin.py").read_text(encoding="utf-8"))
    script_functions = {
        node.name: node for node in script.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    off = ast.unparse(script_functions["all_off"])
    for part in ("FLEET_OPT_IN_ASSIGNMENTS", "reason=ALL_OFF_REASON", "'ctem-%'", "'enabled'", "db.commit()"):
        assert part in off, part
    assert "'--all-off'" in ast.unparse(script_functions["main"])

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_ctem_fleets_all_off"])
    for part in (
        "'seed_ctem_fleet_optin.py'",
        "'--all-off'",
        "env=seed_script_env()",
        "/health",
        "'automations'",
        "'CTEM fleets all off'",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part
    seeded = ast.unparse(functions["seed_entities"])
    call = "seed_ctem_fleets_all_off(token, base)"
    assert call in seeded and "CTEM fleets all off not seeded" in seeded
    assert seeded.index("seed_ctem_fleet_optin(token, base)") < seeded.index(call) < seeded.index("seeded.json")


def _seed_ctem_fleet_optin_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_ctem_fleet_optin", LAB.parent / "seed_ctem_fleet_optin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_ctem_fleet_optin_seed_plans_one_fleet_per_state_and_leaves_the_others_not_set_up(monkeypatch):
    """#9610: one fleet on (a trackable automation, the preferred slug first), one partial, one paused."""
    from datetime import timedelta

    from app.services import run_outcome
    from app.services.assignment_pause import EXPECTED_REASONS, PAUSE_REASONS
    from app.services.opencti_trigger_provisioning import read_provision_spec

    module = _seed_ctem_fleet_optin_module()
    lab_asgi, _ = _lab_asgi_module(monkeypatch)
    assert module.IDENTITY in {n["id"] for n in lab_asgi.LAB_OPENCTI_ORGANIZATIONS}
    assert module.PAUSE_REASON in PAUSE_REASONS and module.PAUSE_REASON not in EXPECTED_REASONS

    states = module.plan(
        {
            "ctem-ai-security": [("a1", False), ("a2", False)],
            "ctem-posture": [("p1", False), ("p2", False)],
            "ctem-third-party-risk": [("t1", False)],
            "ctem-threat-response": [("r1", False), ("r2", True)],
            "ctem-vuln-prioritization": [("v1", True), ("v2", False)],
        }
    )
    # #9689: the five built-in CTEM fleets read on, partial, paused, stale AND failing; none is left
    # not set up (lab.py forks one for that state).
    assert states == {
        "ctem-ai-security": "partial",
        "ctem-posture": "paused",
        "ctem-third-party-risk": "stale",
        "ctem-threat-response": "failing",
        "ctem-vuln-prioritization": "on",
    }
    # #9670: six fleets read every state, one left not set up.
    six = module.plan(
        {
            "ctem-ai-security": [("a1", False), ("a2", False)],
            "ctem-exposure": [("e1", False)],
            "ctem-posture": [("p1", False), ("p2", False)],
            "ctem-third-party-risk": [("t1", False)],
            "ctem-threat-response": [("r1", False), ("r2", True)],
            "ctem-vuln-prioritization": [("v1", True), ("v2", False)],
        }
    )
    assert sorted(six.values()) == ["failing", "not_set_up", "on", "partial", "paused", "stale"]
    assert six["ctem-third-party-risk"] == "failing" and six["ctem-threat-response"] == "not_set_up"
    assert set(module.ALL_ON) == {"on", "stale", "failing"}
    assert timedelta(hours=24) < module.STALE_AGE
    assert module.FAILED_AGE < run_outcome.HEALTH_WINDOW
    # The failed run is one of the agent's own work, never an interruption the verdict skips.
    source = (LAB.parent / "seed_ctem_fleet_optin.py").read_text(encoding="utf-8")
    error = next(line for line in source.splitlines() if "error=" in line and "Seeded by the lab" in line)
    assert not run_outcome.is_interruption(error)
    # Without the preferred slug, the first fleet with a trackable automation is on.
    assert module.plan({"b": [("1", True)], "a": [("2", False), ("3", False)], "c": [("4", False)]}) == {
        "a": "partial",
        "b": "on",
        "c": "paused",
    }
    with pytest.raises(RuntimeError):
        module.plan({"a": [("1", False)], "b": [("2", False), ("3", False)], "c": [("4", False)]})
    # #1048: the four built-in CTEM fleets read on, partial, paused and stale; failing is carried by
    # the partial fleet, so every required state still renders (lab.py forks one for not set up).
    four = module.plan(
        {
            "ctem-ai-security": [("a1", False), ("a2", False)],
            "ctem-loop": [("l1", False), ("l2", True)],
            "ctem-third-party-risk": [("t1", False)],
            "ctem-vuln-prioritization": [("v1", True), ("v2", False)],
        }
    )
    assert four == {
        "ctem-ai-security": "partial",
        "ctem-loop": "paused",
        "ctem-third-party-risk": "stale",
        "ctem-vuln-prioritization": "on",
    }
    assert module.extras(four) == {"ctem-ai-security": ["failing"]}
    assert module.extras(states) == {} and module.extras(six) == {}

    # A KEV automation can serve the tracked organizations; one reading the primary profile cannot.
    kev = {"provider": "opencti", "provision": {"fleet": "F", "purpose": "P", "entity_types": ["Vulnerability"]}}
    assert module.trackable(kev)
    spec = read_provision_spec({**kev, "provision": {**kev["provision"], "organizations": "tracked"}})
    assert spec is not None and spec["organizations"] == "tracked"
    assert not module.trackable({"provider": "opencti", "provision": {"organization_context": ["sector"]}})
    assert not module.trackable({"cron": "0 6 * * *"})

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_ctem_fleet_optin"])
    # #9689: a missing state raises, the Not set up fork is added when no built-in fleet is left.
    assert "CTEM_FLEET_OPTIN_REQUIRED" in helper and "_ctem_not_set_up_fork(token, source)" in helper
    required = _lab_namespace("CTEM_FLEET_OPTIN_REQUIRED")["CTEM_FLEET_OPTIN_REQUIRED"]
    assert set(required) == {"on", "partial", "paused", "stale", "failing", "not_set_up"}
    fork = ast.unparse(functions["_ctem_not_set_up_fork"])
    assert "'/fleets'" in fork and "_fork(token, source, CTEM_NOT_SET_UP_FORK)" in fork
    for part in (
        "'seed_ctem_fleet_optin.py'",
        "sys.executable",
        "env=seed_script_env()",
        "/run-estimate",
        "'CTEM fleet opt-in states'",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part
    seeded = ast.unparse(functions["seed_entities"])
    call = "seed_ctem_fleet_optin(token, base)"
    assert call in seeded and "'LAB_CTEM_FLEET_OPTIN'" in seeded
    # #9689: the start fails when the opt-in states cannot be seeded, never a skip.
    assert "CTEM fleet opt-in states not seeded" in seeded
    assert "skipped['CTEM fleet opt-in states'] = str(exc)" not in seeded
    # #9716: a community lab skips them (GET /fleets is Enterprise Edition), before any fleet call.
    community = "lab_switch('LAB_EDITION', LAB_EDITIONS) == 'community'"
    assert community in seeded
    assert seeded.index("'LAB_CTEM_FLEET_OPTIN'") < seeded.index(community) < seeded.index(call)
    assert seeded.index("seed_organizations(base)") < seeded.index(call) < seeded.index("seeded.json")
    assert seeded.index("seed_suite_registrations(token, base)") < seeded.index(call)


def test_the_ctem_findings_answer_the_cyber_collection_only_when_asked(monkeypatch):
    import asyncio

    from app.api.v1 import ctem_metrics
    from app.services.ctem_scoring import compute_posture

    original = ctem_metrics.build_posture_for_user
    module, _ = _lab_asgi_module(monkeypatch)
    assert ctem_metrics.build_posture_for_user is original
    assert module.findings_ctem_posture({}) is False
    assert module.findings_ctem_posture({"LAB_CTEM_FINDINGS": "off"}) is False
    assert ctem_metrics.build_posture_for_user is original

    async def live(db, user_id, vertical="cyber"):
        return {"live": {}}, {vertical}

    monkeypatch.setattr(ctem_metrics, "build_posture_for_user", live)
    assert module.findings_ctem_posture({"LAB_CTEM_FINDINGS": "on"}) is True
    assert asyncio.run(ctem_metrics.build_posture_for_user(None, "u", "fimi")) == ({"live": {}}, {"fimi"})
    facts, present = asyncio.run(ctem_metrics.build_posture_for_user(None, "u", "cyber"))
    assert present == {"opencti", "openaev"}
    posture = compute_posture(facts, present, "cyber")
    stages = {s["key"]: s for s in posture["stages"]}
    # A live posture (the command center leaves its sample), Scoping connected with no data, no blind spot.
    assert posture["posture_score"] is not None
    assert not stages["scoping"]["covered"] and stages["scoping"]["missing_products"] == []
    assert all(stages[k]["covered"] for k in ("discovery", "prioritization", "validation", "remediation"))
    buckets = facts["validation"]["expectation_buckets"]
    for kind in ("PREVENTION", "DETECTION", "VULNERABILITY", "HUMAN_RESPONSE"):
        assert buckets[kind]["FAILED"] + buckets[kind]["PARTIAL"] > 0, kind
    assert facts["prioritization"]["kev_count"] > 0 and facts["discovery"]["detection_coverage_pct"] < 60
    # A fresh copy: the route mutates what the collector answers.
    buckets["PREVENTION"]["FAILED"] = 0
    again, _ = asyncio.run(ctem_metrics.build_posture_for_user(None, "u", "cyber"))
    assert again["validation"]["expectation_buckets"]["PREVENTION"]["FAILED"] == 4


def test_the_ctem_findings_are_read_back_with_the_briefing_locked():
    import types

    findings = {
        "products": {"opencti": {"present": True}, "openaev": {"present": True}},
        "stages": [
            {"key": "scoping", "covered": False, "products_present": ["opencti", "openaev"]},
            {"key": "validation", "covered": True, "products_present": ["opencti", "openaev"],
             "viz": {"expectation_buckets": {t: {"FAILED": 1} for t in
                                             ("PREVENTION", "DETECTION", "VULNERABILITY", "HUMAN_RESPONSE")}}},
        ],
        "headline": {"kev_count": 7, "detection_coverage_pct": 42},
    }
    calls: list[tuple[str, str]] = []
    answers: dict = {
        ("GET", "/assignments/runs/all?limit=500"): [
            {"id": "r1", "status": "pending", "trigger_context": {"ctem_vertical": "cyber"}},
            {"id": "r2", "status": "completed", "trigger_context": {"ctem_vertical": "cyber"}},
        ],
        ("GET", "/ctem-metrics/posture"): findings,
        ("GET", "/ctem-metrics/briefing"): {"status": "ai_disabled", "generating": False},
    }

    def api(method, path, token=None, body=None):
        calls.append((method, path))
        return answers.get((method, path), {})

    names = ("LAB_CTEM_FINDINGS_TYPES", "ctem_findings_gaps", "seed_ctem_findings")
    sleeps: list[int] = []
    ns = _lab_namespace(*names, api=api, time=types.SimpleNamespace(sleep=sleeps.append))
    base = "http://localhost:3100"
    assert ns["seed_ctem_findings"]("t", base) == {"id": "ctem-findings", "url": f"{base}/dashboard"}
    assert [p for m, p in calls if m == "DELETE"] == ["/assignments/runs/r1"]
    assert sleeps == []

    gaps = ns["ctem_findings_gaps"]
    sample = {"products": {}, "stages": [{"key": "scoping", "covered": True, "products_present": ["opencti"]}],
              "headline": {"kev_count": 0, "detection_coverage_pct": None}}
    assert gaps(sample, {"status": "ready"}) == [
        "opencti is not present", "openaev is not present",
        "no failed expectation on PREVENTION, DETECTION, VULNERABILITY, HUMAN_RESPONSE",
        "no stage is connected with no data", "headline kev_count is 0",
        "headline detection_coverage_pct is None, not under 60",
        "the briefing answers 'ready': its next steps replace the rule-based rows",
    ]
    answers[("GET", "/ctem-metrics/briefing")] = {"status": "generating", "generating": True}
    with pytest.raises(RuntimeError, match="not reached"):
        ns["seed_ctem_findings"]("t", base)
    assert sleeps == [5, 5, 5, 5]

    seed = _lab_source("seed_entities")
    assert "seed_ctem_findings(token, base)" in seed and "skipped['CTEM findings']" in seed
    # Read back once AI is off (the briefing locked), before the seed is written.
    assert seed.index("seed_ai_lock_states(token, base, peer_id)") < seed.index("seed_ctem_findings(token, base)")
    assert seed.index("seed_ctem_findings(token, base)") < seed.index("seeded.json")


def test_the_ctem_actions_lab_is_the_same_lab_with_the_switch_and_ai_on():
    services = _services()
    lab = services["lab"]
    twins = {
        "lab-ctem-actions": {"LAB_CTEM_ACTIONS": "on"},
        "lab-ctem-actions-light": {"LAB_CTEM_ACTIONS": "on", "LAB_THEME": "light"},
        # #11340: the generic-context states of the command center, each with its light twin.
        "lab-ctem-briefing-generic": {"LAB_CTEM_ACTIONS": "on", "LAB_ORG_SETUP_PROMPT": "on"},
        "lab-ctem-briefing-generic-light": {
            "LAB_CTEM_ACTIONS": "on",
            "LAB_ORG_SETUP_PROMPT": "on",
            "LAB_THEME": "light",
        },
        "lab-ctem-kev-tailored": {"LAB_CTEM_ACTIONS": "on", "LAB_CTEM_KEV_TAILORED": "on"},
        "lab-ctem-kev-tailored-light": {"LAB_CTEM_ACTIONS": "on", "LAB_CTEM_KEV_TAILORED": "on", "LAB_THEME": "light"},
        # #12381: the stale CTEM states, the same lab aged by LAB_CTEM_STALE with the worker on.
        "lab-ctem-stale": {"LAB_CTEM_ACTIONS": "on", "LAB_CTEM_STALE": "on", "LAB_WORKER": "1"},
        "lab-ctem-stale-light": {
            "LAB_CTEM_ACTIONS": "on",
            "LAB_CTEM_STALE": "on",
            "LAB_WORKER": "1",
            "LAB_THEME": "light",
        },
    }
    for name, switches in twins.items():
        actions = services[name]
        assert actions["env"] == {**lab["env"], **switches}, name
        assert "LAB_AI" not in actions["env"], name
        assert {k: v for k, v in actions.items() if k not in ("env", "description")} == {
            k: v for k, v in lab.items() if k not in ("env", "description")
        }, name
    assert not any("LAB_CTEM_ACTIONS" in s.get("env", {}) for n, s in services.items() if n not in twins)
    assert not any("LAB_CTEM_KEV_TAILORED" in s.get("env", {}) for n, s in services.items() if n not in twins)


def test_lab_ctem_actions_answers_the_findings_posture_only_when_on(monkeypatch):
    from app.api.v1 import ctem_metrics

    original = ctem_metrics.build_posture_for_user
    module, _ = _lab_asgi_module(monkeypatch)
    # Restored after the test: the switch replaces the route module's collector.
    monkeypatch.setattr(ctem_metrics, "build_posture_for_user", original)
    assert module.actions_ctem_posture({}) is False
    assert ctem_metrics.build_posture_for_user is original
    assert module.actions_ctem_posture({"LAB_CTEM_ACTIONS": "on"}) is True
    assert ctem_metrics.build_posture_for_user is not original


def _seed_ctem_actions_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_ctem_actions", LAB.parent / "seed_ctem_actions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_ctem_actions_seed_stores_a_ready_artifact_per_stage_and_a_briefing_the_routes_read():
    """Each stored run parses as the route's artifact, authored for cyber; the briefing repeats one stage action."""
    import json
    from datetime import UTC, datetime

    from app.agents.builtin_agents import _EXPOSURE_BRIEFING_SCHEMA, _STAGE_ASSESSMENT_SCHEMA
    from app.api.v1.ctem_metrics import _BRIEFING_AGENT_SLUG, _parse_briefing
    from app.services.ctem_scoring import STAGE_KEYS
    from app.services.ctem_verticals import CTEM_STAGE_AGENT_SLUGS

    module = _seed_ctem_actions_module()
    assert module.BRIEFING_SLUG == _BRIEFING_AGENT_SLUG
    assert set(module.STAGE_ACTIONS) == set(STAGE_KEYS) == set(CTEM_STAGE_AGENT_SLUGS)
    names = _lab_namespace("LAB_CTEM_ACTIONS_STAGES")
    assert set(names["LAB_CTEM_ACTIONS_STAGES"]) == set(module.STAGE_ACTIONS)

    agents = {**{s: f"id-{s}" for s in module.STAGE_ACTIONS}, "briefing": "id-briefing"}
    rows = module.plan(agents, datetime(2026, 11, 9, 12, 0, tzinfo=UTC))
    assert [r["key"] for r in rows] == [*module.STAGE_ACTIONS, "briefing"]
    assert all(r["status"] == "completed" and r["completed_at"] for r in rows)
    assert all(r["trigger_context"]["seeded_by"] == "lab" for r in rows)
    for row in rows:
        parsed = _parse_briefing(row["result_summary"])
        assert parsed is not None and parsed["vertical"] == "cyber", row["key"]
        schema = _EXPOSURE_BRIEFING_SCHEMA if row["key"] == "briefing" else _STAGE_ASSESSMENT_SCHEMA
        assert set(schema["required"]) <= set(parsed), row["key"]
        if row["key"] != "briefing":
            assert parsed["stage"] == row["key"] and parsed["recommended_action"]
    briefing = json.loads(rows[-1]["result_summary"])
    steps = briefing["next_steps"]
    assert [s["priority"] for s in steps] == ["high", "medium", "low"]
    actions = {a for _h, _s, a in module.STAGE_ACTIONS.values()}
    assert sum(s["action"] in actions for s in steps) == 1


def test_the_ctem_actions_are_read_back_ready_after_the_active_runs_are_deleted():
    import types

    ready = {"status": "ready", "assessment": {"recommended_action": "Do it"}}
    calls: list[tuple[str, str]] = []
    answers: dict = {
        ("GET", "/auth/me"): {"id": "admin"},
        ("GET", "/assignments/runs/all?limit=500"): [
            {"id": "r1", "status": "pending", "trigger_context": {"ctem_vertical": "cyber"}},
            {"id": "r2", "status": "completed", "trigger_context": {"ctem_vertical": "cyber"}},
        ],
        ("GET", "/ctem-metrics/briefing"): {"status": "ready", "briefing": {"next_steps": [{}, {}, {}]}},
        ("GET", "/ctem-metrics/posture"): {"headline": {"kev_count": 7, "detection_coverage_pct": 42}},
    }
    for stage in ("scoping", "discovery", "prioritization", "validation", "remediation"):
        answers[("GET", f"/ctem-metrics/stages/{stage}/assessment")] = ready

    def api(method, path, token=None, body=None):
        calls.append((method, path))
        return answers.get((method, path), {})

    class Root:
        def __truediv__(self, _name):
            return types.SimpleNamespace(read_text=lambda: '{"scoping": "a", "briefing": "b"}')

    ran: list[list[str]] = []

    def run(argv, **kwargs):
        ran.append(argv)
        return types.SimpleNamespace(returncode=0, stderr="")

    names = ("LAB_CTEM_ACTIONS_STAGES", "ctem_actions_gaps", "seed_ctem_actions")
    sleeps: list[int] = []
    ns = _lab_namespace(
        *names, api=api, time=types.SimpleNamespace(sleep=sleeps.append), json=__import__("json"),
        _created=lambda row: row["id"], ROOT=Root(), CHECKOUT=LAB.parent.parent.parent,
        subprocess=types.SimpleNamespace(run=run), sys=types.SimpleNamespace(executable="python"),
        seed_script_env=dict, stderr_summary=str,
    )
    base = "http://localhost:3100"
    assert ns["seed_ctem_actions"]("t", base) == {"id": "scoping,briefing", "url": f"{base}/"}
    assert ran and ran[0][1].endswith("seed_ctem_actions.py") and ran[0][2] == "admin"
    assert [p for m, p in calls if m == "DELETE"] == ["/assignments/runs/r1"]
    assert sleeps == []

    gaps = ns["ctem_actions_gaps"]
    assert gaps({}, {"status": "generating"}, {"headline": {"kev_count": 0}}) == [
        *(f"the {s} assessment answers status None, recommended_action None"
          for s in ("scoping", "discovery", "prioritization", "validation", "remediation")),
        "the briefing answers status 'generating' with 0 next steps",
        "headline kev_count is 0",
        "headline detection_coverage_pct is None, not under 60",
    ]
    answers[("GET", "/ctem-metrics/stages/scoping/assessment")] = {"status": "generating", "assessment": None}
    with pytest.raises(RuntimeError, match="not ready"):
        ns["seed_ctem_actions"]("t", base)
    assert sleeps == [5, 5, 5, 5]

    seed = _lab_source("seed_entities")
    assert "seed_ctem_actions(token, base)" in seed and "skipped['CTEM stage assessments']" in seed
    assert seed.index("seed_ai_lock_states(token, base, peer_id)") < seed.index("seed_ctem_actions(token, base)")
    assert seed.index("seed_ctem_actions(token, base)") < seed.index("seeded.json")


def test_the_generic_briefing_and_the_tailored_kev_are_read_back_on_their_labs():
    """#11340: each state of the command center's generic-context notices is read back and logged, or raises."""
    answers: dict = {
        ("GET", "/organization-context"): {"version": None},
        ("GET", "/ctem-metrics/briefing"): {"status": "ready"},
        ("GET", "/ctem-metrics/posture"): {
            "headline": {"kev_tailored_count": 3, "kev_tailored_products": ["Microsoft Exchange Server", "FortiOS"]},
        },
    }

    def api(method, path, token=None, body=None):
        return answers.get((method, path), {})

    ns = _lab_namespace("seed_ctem_generic_briefing", "seed_ctem_kev_tailored", api=api)
    base = "http://localhost:3100"
    assert ns["seed_ctem_generic_briefing"]("t", base) == {"id": "briefing ready, no profile", "url": f"{base}/"}
    assert ns["seed_ctem_kev_tailored"]("t", base) == {
        "id": "3 tailored KEVs on Microsoft Exchange Server, FortiOS",
        "url": f"{base}/",
    }
    answers[("GET", "/organization-context")] = {"version": 1}
    with pytest.raises(RuntimeError, match="version 1"):
        ns["seed_ctem_generic_briefing"]("t", base)
    answers[("GET", "/ctem-metrics/posture")] = {"headline": {"kev_tailored_count": None}}
    with pytest.raises(RuntimeError, match="kev_tailored_count is None"):
        ns["seed_ctem_kev_tailored"]("t", base)

    seed = _lab_source("seed_entities")
    actions = seed.index("seed_ctem_actions(token, base)")
    assert actions < seed.index("seed_ctem_generic_briefing(token, base)") < seed.index("seeded.json")
    assert actions < seed.index("seed_ctem_kev_tailored(token, base)") < seed.index("seeded.json")
    assert "skipped['CTEM generic briefing']" in seed and "skipped['CTEM tailored KEV']" in seed


def test_lab_ctem_kev_tailored_saves_the_tech_stack_with_the_profile_only_when_on():
    from app.services.organization_context import parse_profile

    saved: list[dict] = []

    def api(method, path, token=None, body=None):
        if method == "PUT":
            saved.append(body["profile"])
            return {"version": 1}
        return {"version": None}

    names = (
        "LAB_ORG_PROFILE",
        "LAB_ORG_TECH_STACK",
        "LAB_ORG_SETUP_PROMPT_STATES",
        "LAB_ORG_CONTEXT_STATES",
        "LAB_CTEM_KEV_TAILORED_STATES",
        "LAB_CTEM_OPENCTI_STATES",
        "lab_switch",
        "seed_org_ai_draft_stub",
    )
    _lab_namespace(*names, api=api)["seed_org_ai_draft_stub"]("t", "http://localhost:3100")
    assert "software" not in saved[-1]
    ns = _lab_namespace(*names, api=api, env={"LAB_CTEM_KEV_TAILORED": "on"})
    ns["seed_org_ai_draft_stub"]("t", "http://localhost:3100")
    assert saved[-1]["software"] == [dict(e) for e in ns["LAB_ORG_TECH_STACK"]]
    assert len(parse_profile(saved[-1]).software) == len(ns["LAB_ORG_TECH_STACK"]) >= 3


def test_lab_ctem_kev_tailored_narrows_the_cyber_kev_count_to_the_saved_stack(monkeypatch):
    import asyncio

    from app.api.v1 import ctem_metrics
    from app.services import ctem_metrics_collector

    original = ctem_metrics.build_posture_for_user
    module, _ = _lab_asgi_module(monkeypatch)
    monkeypatch.setattr(ctem_metrics, "build_posture_for_user", original)
    assert module.tailored_ctem_posture({}) is False
    assert ctem_metrics.build_posture_for_user is original

    findings = {"prioritization": {"kev_count": 7}}

    async def collect(db, user_id, vertical="cyber"):
        return findings, {"opencti", "openaev"}

    stack = [{"label": label} for label in ("A", "B", "C", "D")]

    async def read_stack(db):
        return stack

    monkeypatch.setattr(ctem_metrics, "build_posture_for_user", collect)
    monkeypatch.setattr(ctem_metrics_collector, "_organization_software", read_stack)
    assert module.tailored_ctem_posture({"LAB_CTEM_KEV_TAILORED": "on"}) is True
    facts, present = asyncio.run(ctem_metrics.build_posture_for_user(None, "u", "cyber"))
    assert present == {"opencti", "openaev"}
    assert facts["prioritization"] == {
        "kev_count": 7,
        "kev_tailored_count": module.ctem_sample.KEV_TAILORED_COUNT,
        "kev_tailored_products": ["A", "B", "C"],
    }
    assert findings == {"prioritization": {"kev_count": 7}}
    assert module.ctem_sample.KEV_TAILORED_COUNT < 7
    stack.clear()
    assert asyncio.run(ctem_metrics.build_posture_for_user(None, "u", "cyber"))[0] == findings


def test_the_ctem_activity_labs_are_the_ce_lab_with_the_switch_and_ai_off():
    services = _services()
    lab = services["lab"]
    for name, switches in {
        "lab-ctem-activity": {"LAB_EDITION": "community", "LAB_CTEM_ACTIVITY": "on", "LAB_AI": "off"},
        "lab-ctem-activity-light": {
            "LAB_EDITION": "community", "LAB_CTEM_ACTIVITY": "on", "LAB_AI": "off", "LAB_THEME": "light",
        },
    }.items():
        service = services[name]
        assert service["env"] == {**lab["env"], **switches}, name
        assert {k: v for k, v in service.items() if k not in ("env", "description")} == {
            k: v for k, v in lab.items() if k not in ("env", "description")
        }, name
    assert not any("LAB_CTEM_ACTIVITY" in s.get("env", {}) for n, s in services.items() if not n.startswith("lab-ctem-activity"))


def test_the_ctem_activity_stamps_a_sync_time_on_each_connected_product_only_when_asked(monkeypatch):
    import asyncio
    from datetime import UTC, datetime, timedelta

    from app.api.v1 import ctem_metrics

    original_build = ctem_metrics._build_posture_payload
    original_collect = ctem_metrics.build_posture_for_user
    module, _ = _lab_asgi_module(monkeypatch)
    assert module.activity_ctem_posture({}) is False
    assert module.activity_ctem_posture({"LAB_CTEM_ACTIVITY": "off"}) is False
    assert ctem_metrics._build_posture_payload is original_build
    assert ctem_metrics.build_posture_for_user is original_collect

    async def build(db, user, vertical):
        return {"products": {
            "opencti": {"present": True, "last_sync": None},
            "openaev": {"present": True, "last_sync": None},
            "opencrq": {"present": False, "last_sync": None, "coming_soon": True},
        }}, {"facts": vertical}

    monkeypatch.setattr(ctem_metrics, "_build_posture_payload", build)
    monkeypatch.setattr(ctem_metrics, "build_posture_for_user", original_collect)
    assert module.activity_ctem_posture({"LAB_CTEM_ACTIVITY": "on"}) is True
    # The findings collection answers the cyber vertical (validation buckets with every result).
    facts, present = asyncio.run(ctem_metrics.build_posture_for_user(None, "u", "cyber"))
    assert present == {"opencti", "openaev"}
    totals = {r: sum(b.get(r, 0) for b in facts["validation"]["expectation_buckets"].values())
              for r in ("SUCCESS", "FAILED", "PARTIAL")}
    assert all(totals.values()), totals
    payload, stage_facts = asyncio.run(ctem_metrics._build_posture_payload(None, None, "cyber"))
    assert stage_facts == {"facts": "cyber"}
    products = payload["products"]
    assert products["opencrq"]["last_sync"] is None
    now = datetime.now(UTC)
    for product, minutes in (("opencti", 3), ("openaev", 7)):
        synced = datetime.fromisoformat(products[product]["last_sync"])
        assert abs((now - synced) - timedelta(minutes=minutes)) < timedelta(seconds=30), product


def test_the_silent_platform_is_past_the_heartbeat_window():
    import importlib.util
    from datetime import UTC, datetime, timedelta

    from app.models.platform_registration import HEARTBEAT_HEALTHY_WINDOW

    spec = importlib.util.spec_from_file_location("seed_ctem_activity_under_test", LAB.parent / "seed_ctem_activity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = datetime.now(UTC)
    assert now - module.heartbeat(now) > HEARTBEAT_HEALTHY_WINDOW
    # #9627: every registration is backdated past the silent heartbeat and the two newest syncs, so the
    # "registered" rows no longer fill the newest rows of the Live activity card.
    sync = module.TIMELINE["product_sync"]
    newest_sync = now - timedelta(minutes=max(sync["opencti"], sync["openaev"]))
    for identifier in ("opencti", "openaev", "opencrq"):
        at = module.registered(now, identifier)
        assert at is not None and at < module.heartbeat(now) < newest_sync, identifier
    assert module.registered(now, "unknown") is None


def test_the_ctem_activity_is_read_back_with_no_run_left():
    import types

    feed = {
        "generated_at": "2026-11-07T09:00:00+00:00",
        "products": {"opencti": {"present": True, "last_sync": "2026-11-07T08:57:00+00:00"},
                     "openaev": {"present": True, "last_sync": "2026-11-07T08:53:00+00:00"}},
        "stages": [{"key": "validation", "viz": {"expectation_buckets": {
            "PREVENTION": {"SUCCESS": 3, "FAILED": 1, "PARTIAL": 1}}}}],
    }
    registrations = [
        {"registered_at": "2026-11-07T08:00:00+00:00", "healthy": True, "last_heartbeat_at": "2026-11-07T09:00:00+00:00"},
        {"registered_at": "2026-11-07T08:00:00+00:00", "healthy": False, "last_heartbeat_at": "2026-11-07T07:00:00+00:00"},
    ]
    calls: list[tuple[str, str]] = []
    listed = [[{"id": "r1"}, {"id": "r2"}], []]

    def api(method, path, token=None, body=None):
        calls.append((method, path))
        if path == "/assignments/runs/all?limit=500":
            return listed.pop(0) if listed else []
        return {"/ctem-metrics/posture": feed, "/platform/registrations": registrations}.get(path, {})

    ran: list[list[str]] = []

    def run(argv, **kwargs):
        ran.append(argv)
        return types.SimpleNamespace(returncode=0, stderr="")

    names = ("LAB_CTEM_ACTIVITY_SILENT", "_utc", "ctem_activity_gaps", "seed_ctem_activity")
    sleeps: list[int] = []
    from pathlib import Path

    ns = _lab_namespace(
        *names, api=api, time=types.SimpleNamespace(sleep=sleeps.append),
        subprocess=types.SimpleNamespace(run=run), sys=types.SimpleNamespace(executable="python"),
        ROOT=Path("lab-root"), CHECKOUT=Path("/checkout"), seed_script_env=dict, stderr_summary=str,
    )
    base = "http://localhost:3100"
    assert ns["seed_ctem_activity"]("t", base) == {"id": "ctem-activity-feed", "url": f"{base}/dashboard"}
    assert ran[0][-2] == "opencrq" and ran[0][1].endswith("seed_ctem_activity.py")
    assert [p for m, p in calls if m == "DELETE"] == ["/assignments/runs/r1", "/assignments/runs/r2"]
    assert sleeps == []

    gaps = ns["ctem_activity_gaps"]
    assert gaps({"stages": [], "products": {}}, [{"healthy": True}], [{"id": "r"}]) == [
        "the posture answers no generated_at", "opencti has no last_sync", "openaev has no last_sync",
        "no SUCCESS expectation in the validation buckets", "no FAILED expectation in the validation buckets",
        "no PARTIAL expectation in the validation buckets", "no registration with registered_at",
        "no unhealthy registration with a last_heartbeat_at", "1 assignment run(s) listed",
    ]
    # #9677: a "registered" row at or after the validation row (OpenAEV's last_sync) is a gap, so a lab whose
    # registrations are stamped at its start never comes up with the validation row out of the 4-row card.
    fresh = [*registrations, {"platform_identifier": "opencti", "registered_at": "2026-11-07T08:53:00Z",
                              "healthy": True, "last_heartbeat_at": "2026-11-07T09:00:00+00:00"}]
    assert gaps(feed, fresh, []) == ["opencti registered_at is not older than the validation row"]
    assert gaps(feed, registrations, []) == []
    # No OpenAEV sync: the validation row is dated by the posture's generated_at, as in deterministicActivity.
    unsynced = {**feed, "products": {**feed["products"], "openaev": {"present": True, "last_sync": None}}}
    late = [{**registrations[0], "registered_at": "2026-11-07T09:30:00+00:00"}, registrations[1]]
    assert "registered_at is not older than the validation row" in gaps(unsynced, late, [])[-1]

    seed = _lab_source("seed_entities")
    assert "seed_ctem_activity(token, base)" in seed and "skipped['CTEM activity feed']" in seed
    # Last: after every seed that creates runs, before the seed is written.
    assert seed.index("seed_ctem_findings(token, base)") < seed.index("seed_ctem_activity(token, base)")
    assert seed.index("seed_ctem_activity(token, base)") < seed.index("seeded.json")


def test_the_lab_suite_registrations_declare_platform_intents_with_one_unbound():
    """#9826: the Suite registrations declare intents so /intents shows platforms and an Unbound row."""
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    declared = next(
        n for n in tree.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "SUITE_INTENTS"
    )
    unbound_name = next(
        n for n in tree.body
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "LAB_UNBOUND_INTENT" for t in n.targets)
    ).value.value
    intents = ast.literal_eval(ast.unparse(declared.value).replace("LAB_UNBOUND_INTENT", repr(unbound_name)))
    assert set(intents) == {"opencti", "openaev", "opencrq"}
    names = [{i["name"] for i in rows} for rows in intents.values()]
    assert set.intersection(*names), "one intent is declared by all three platforms"
    assert unbound_name in names[0] and all(i["description"] for rows in intents.values() for i in rows)
    assert "'intents': SUITE_INTENTS[provider]" in _lab_source("seed_suite_registrations")

    rows = [
        {"name": "global.summarize", "platforms": ["opencti", "openaev", "opencrq"], "bindings": [{"id": "b"}]},
        {"name": unbound_name, "platforms": ["opencti"], "bindings": []},
        {"name": "global.explain", "platforms": [], "bindings": []},
    ]
    ns = _lab_namespace("seed_platform_intents_check", "LAB_UNBOUND_INTENT", api=lambda *a, **k: rows)
    assert ns["seed_platform_intents_check"]("t", "http://localhost:3100") == {
        "id": "openaev,opencrq,opencti", "url": "http://localhost:3100/intents",
    }
    bound = [{**r, "bindings": [{"id": "b"}]} if r["platforms"] else r for r in rows]
    ns = _lab_namespace("seed_platform_intents_check", "LAB_UNBOUND_INTENT", api=lambda *a, **k: bound)
    with pytest.raises(RuntimeError, match="is not Unbound"):
        ns["seed_platform_intents_check"]("t", "http://localhost:3100")
    ns = _lab_namespace("seed_platform_intents_check", "LAB_UNBOUND_INTENT", api=lambda *a, **k: rows[2:])
    with pytest.raises(RuntimeError, match="no intent declared by a platform"):
        ns["seed_platform_intents_check"]("t", "http://localhost:3100")

    seed = _lab_source("seed_entities")
    assert "seeded['platform intents'] = seed_platform_intents_check(token, base)" in seed
    assert seed.index("seed_suite_registrations(token, base)") < seed.index("seed_platform_intents_check(")
    assert seed.index("seed_platform_intents_check(") < seed.index("seeded.json")


def test_the_lab_turns_the_platform_tools_mcp_server_on_unless_lab_xtm_mcp_is_0():
    """#10210: /profile/mcp renders the Platform Tools MCP Server card with no override; LAB_XTM_MCP=0 keeps it off."""
    calls: list = []
    config = {"xtm_mcp_enabled": True}

    def api(method, path, token=None, body=None):
        calls.append((method, path, token, body))
        return config if method == "GET" else {}

    base = "http://localhost:3100"
    ns = _lab_namespace("LAB_XTM_MCP_STATES", "lab_switch", "seed_xtm_mcp", api=api)
    assert ns["seed_xtm_mcp"]("admin-token", base) == {
        "Platform Tools MCP Server": {"id": "xtm_mcp_enabled", "url": f"{base}/profile/mcp"},
    }
    assert calls == [
        ("PUT", "/settings/general-assistant", "admin-token", {"xtm_mcp_enabled": True}),
        ("GET", "/settings/general-assistant", "admin-token", None),
    ]
    config["xtm_mcp_enabled"] = False
    with pytest.raises(RuntimeError, match="xtm_mcp_enabled off"):
        ns["seed_xtm_mcp"]("admin-token", base)
    calls.clear()
    ns = _lab_namespace("LAB_XTM_MCP_STATES", "lab_switch", "seed_xtm_mcp", env={"LAB_XTM_MCP": "0"}, api=api)
    assert ns["seed_xtm_mcp"]("admin-token", base) == {} and calls == []
    seed = _lab_source("seed_entities")
    assert "seed_xtm_mcp(token, base)" in seed and "skipped['Platform Tools MCP Server']" in seed
    assert seed.index("seed_xtm_mcp(token, base)") < seed.index("seeded.json")


def test_the_module_health_lab_turns_platform_health_on_through_the_real_settings_route():
    """#101 S15d-3: ``LAB_PLATFORM_HEALTH=on`` puts Platform Health on, every other setting kept; off leaves it."""
    calls: list = []
    stored = {"enabled": False, "module_analyst": {"enabled": True}}

    def api(method, path, token=None, body=None):
        calls.append((method, path, token, body))
        if method == "PUT":
            stored.update(body)
        return dict(stored)

    base = "http://localhost:3100"
    names = ("LAB_PLATFORM_HEALTH_STATES", "lab_switch", "seed_platform_health")
    ns = _lab_namespace(*names, api=api)
    assert ns["seed_platform_health"]("admin-token", base) == {} and calls == []
    ns = _lab_namespace(*names, env={"LAB_PLATFORM_HEALTH": "on"}, api=api)
    assert ns["seed_platform_health"]("admin-token", base) == {
        "Platform Health": {"id": "enabled", "url": f"{base}/health"},
    }
    assert calls[1] == (
        "PUT",
        "/health/settings",
        "admin-token",
        {"enabled": True, "module_analyst": {"enabled": True}},
    )
    seed = _lab_source("seed_entities")
    assert "seed_platform_health(token, base)" in seed and "skipped['Platform Health']" in seed
    assert seed.index("seed_suite_registrations(token, base)") > seed.index("seed_platform_health(token, base)")
    assert seed.index("seed_platform_health(token, base)") < seed.index("seeded.json")
    services = _services()
    lab = services["lab-module-health"]
    assert "LAB_PLATFORM_HEALTH" not in services["lab"]["env"]
    assert lab["env"] == {**services["lab-live"]["env"], "LAB_PLATFORM_HEALTH": "on", "LAB_WORKER": "1"}


def _seed_health_findings_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_health_findings", LAB.parent / "seed_health_findings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_health_findings_seed_covers_every_severity_module_and_status():
    """#1303: the seeded findings carry every severity, the XTM One module and the three analyst modules (agentic)."""
    from app.api.v1.platform_health_findings import ANALYST_MODULES, is_agentic
    from app.models.health_check import CATEGORIES, MODULES, SEVERITIES

    module = _seed_health_findings_module()
    rows = module.results()
    assert all(r["failing"] for r in rows) and not any(r["failing"] for r in module.results(failing=False))
    assert {r["severity"] for r in rows} == set(SEVERITIES)
    assert {r["module"] for r in rows} <= set(MODULES) and {r["category"] for r in rows} <= set(CATEGORIES)
    assert {r["module"] for r in rows if is_agentic(r["check_id"])} == set(ANALYST_MODULES)
    assert all(r["check_id"].startswith(f"{r['module']}.") for r in rows)
    checks = [r["check_id"] for r in rows]
    assert len(set(checks)) == len(checks) and {module.DISMISSED, module.RESOLVED} <= set(checks)
    assert module.DISMISSED != module.RESOLVED

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_health_findings"])
    for part in ("'seed_health_findings.py'", "sys.executable", "env=seed_script_env()", "'/health/overview'",
                 "'/health/findings?limit=200'", "stderr_summary(process.stderr)", "'xtm_one'"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "seed_health_findings(token, base)" in seed and "skipped['health findings']" in seed
    assert seed.index("seed_platform_health(token, base)") < seed.index("seed_health_findings(token, base)")
    assert seed.index("seed_health_findings(token, base)") < seed.index("seeded.json")


def test_the_lab_peer_clears_its_default_agent_so_profile_renders_the_general_assistant_tools():
    """#10210: as lab-peer, /profile shows "Reset to defaults"; the clear is read back on /auth/me."""
    calls: list = []
    me = {"default_agent_id": None}

    def api(method, path, token=None, body=None):
        calls.append((method, path, token, body))
        return me if path == "/auth/me" else {}

    base = "http://localhost:3100"
    ns = _lab_namespace("PEER_EMAIL", "seed_peer_general_assistant", api=api)
    assert ns["seed_peer_general_assistant"]("peer-token", base) == {
        "peer General Assistant tools": {"id": "lab-peer@filigran.io", "url": f"{base}/profile"},
    }
    assert calls == [
        ("PUT", "/settings/preferences", "peer-token", {"default_agent_id": ""}),
        ("GET", "/auth/me", "peer-token", None),
    ]
    me["default_agent_id"] = "ctem"
    with pytest.raises(RuntimeError, match="still set"):
        ns["seed_peer_general_assistant"]("peer-token", base)
    peer = _lab_source("seed_peer")
    assert "seed_peer_general_assistant(peer_token, base)" in peer
    assert "skipped['peer General Assistant tools']" in peer


def test_the_lab_seeds_the_administrators_profile_and_agent_memory_and_reads_them_back():
    """#10210: /profile/memory renders Purge All and two Clear buttons on real rows; lab-only texts, no model."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab_seed_profile_memory", LAB.parent / "seed_profile_memory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.LAB_PROFILE_STATEMENT.startswith("Lab sample") and module.LAB_AGENT_MEMORY.startswith("Lab sample")
    script = ast.unparse(ast.parse((LAB.parent / "seed_profile_memory.py").read_text(encoding="utf-8")))
    for part in ("update_entries(redis, user_id, add_statement)", "new_entry(LAB_PROFILE_STATEMENT",
                 "store_short_term_memory(agent_id", "scope_id=user_id", "load_user_memory(user_id)"):
        assert part in script, part
    assert "chat(" not in script and "consolidate" not in script

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_profile_memory"])
    for part in ("'seed_profile_memory.py'", "sys.executable", "env=seed_script_env()", "'/settings/me/memory'",
                 "'medium_term'", "'agent_id'", "stderr_summary(process.stderr)", "/profile/memory"):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    call = "seed_profile_memory(token, ids['agent'], base)"
    assert call in seed and "skipped['profile memory']" in seed
    assert seed.index(call) < seed.index("seeded.json")


def test_the_profile_memory_seed_adds_its_statement_once_and_its_agent_memory_once():
    """Run twice, the seed writes one profile statement and one agent memory (a lab restart is idempotent)."""
    import asyncio
    import importlib.util
    import sys
    import types

    from app.services.user_memory_entries import MemoryEntry

    stored: dict = {"entries": None, "agent": []}

    async def update_entries(redis, identity, change):
        written = change(stored["entries"])
        if written is not None:
            stored["entries"] = written
        return written

    async def load_memory(agent_id, *, scope_id=None):
        return {"short_term": list(stored["agent"]), "medium_term": ""}

    async def store_short_term_memory(agent_id, *, source, summary, source_type, scope_id):
        stored["agent"].append({"source": source, "summary": summary, "scope": scope_id})
        return len(stored["agent"])

    async def load_user_memory(identity):
        return {"medium_term": "\n".join(e.text for e in stored["entries"] or [])}

    async def redis_client():
        return object()

    def new_entry(text, *, now):
        return MemoryEntry(text=text)

    fakes = {
        "app.core.redis": types.SimpleNamespace(redis_client=redis_client),
        "app.services.agent_memory": types.SimpleNamespace(load_memory=load_memory,
                                                           store_short_term_memory=store_short_term_memory),
        "app.services.user_memory": types.SimpleNamespace(load_user_memory=load_user_memory,
                                                          update_entries=update_entries),
        "app.services.user_memory_entries": types.SimpleNamespace(new_entry=new_entry),
    }
    spec = importlib.util.spec_from_file_location("lab_seed_profile_memory_run", LAB.parent / "seed_profile_memory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    saved = {name: sys.modules.get(name) for name in fakes}
    sys.modules.update(fakes)
    try:
        first = asyncio.run(module.seed("user-1", "agent-1"))
        second = asyncio.run(module.seed("user-1", "agent-1"))
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    assert first == second == {"user": "user-1", "agent": "agent-1", "profile_medium_term": True, "agent_short_term": 1}
    assert [e.text for e in stored["entries"]] == [module.LAB_PROFILE_STATEMENT]
    assert stored["agent"] == [{"source": "Lab sample chat", "summary": module.LAB_AGENT_MEMORY, "scope": "user-1"}]


def _seed_fleet_doc_findings_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "lab_seed_fleet_doc_findings", LAB.parent / "seed_fleet_doc_findings.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fleet_doc_findings_seed_stores_four_kinds_and_flags_the_current_version():
    """#10335: the findings survive clean_findings, the notes use real block keys, only the current version is flagged."""
    from app.services.fleet_doc_findings import FINDING_TEXT_MAX, clean_findings, finding_id
    from app.services.fleet_docs import BLOCK_NAMES, REGENERATE_NOTES, doc_note

    module = _seed_fleet_doc_findings_module()
    raw = module.findings("Lab sample corporate mail desk", "Lab sample corporate developer")
    stored = clean_findings(raw)
    assert len(stored) == 4
    assert {f["kind"] for f in stored} == {
        "missing_automation",
        "no_gate_on_sensitive_action",
        "description_mismatch",
        "member_without_role",
    }
    assert all(f["id"] == finding_id(f) for f in stored)
    by_kind = {f["kind"]: f for f in stored}
    assert by_kind["description_mismatch"]["member"] == ""
    assert by_kind["member_without_role"]["member"] == module.ABSENT_MEMBER
    assert by_kind["member_without_role"]["suggested_fix"] == ""
    assert 200 < len(by_kind["no_gate_on_sensitive_action"]["finding"]) <= FINDING_TEXT_MAX

    derived, lost = module.flagged_keys({"steps": [1], "gates": [1]}, BLOCK_NAMES)
    assert (derived, lost) == ("steps", "gates")
    assert module.flagged_keys(None, BLOCK_NAMES)[0] in BLOCK_NAMES
    assert doc_note("block_lost", block=lost)["code"] in REGENERATE_NOTES
    assert doc_note("part_derived", block=derived)["code"] not in REGENERATE_NOTES

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_fleet_doc_findings"])
    for part in (
        "'seed_fleet_doc_findings.py'",
        "sys.executable",
        "env=seed_script_env()",
        "stderr_summary(process.stderr)",
        "/documentation/history",
        "needs_regenerate",
        "'fleet doc findings'",
        "'fleet doc flagged version'",
    ):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert "skipped['fleet doc findings']" in seed
    assert seed.index("seed_fleet_documentation(") < seed.index("seed_fleet_doc_findings(") < seed.index("seeded.json")
    script = (LAB.parent / "seed_fleet_doc_findings.py").read_text(encoding="utf-8")
    # The product's own writer: history, fingerprint and the findings' ids, never a hand-built JSON.
    assert "save_documentation(" in script and "fleet.documentation =" not in script

"""An install check that rewrites a tracked manifest is named, so from_checkout does not commit it unseen (#474)."""

from types import SimpleNamespace

from app.services.sandbox_workspace.commands import rewritten_manifests


def _check(name: str) -> SimpleNamespace:
    return SimpleNamespace(check=name)


def test_an_install_that_rewrote_package_json_names_it():
    changed = ["openaev-front/package.json", "openaev-front/src/app.ts", "openaev-front/yarn.lock"]
    assert rewritten_manifests(_check("openaev-front:node-install"), changed) == [
        "openaev-front/package.json",
        "openaev-front/yarn.lock",
    ]


def test_a_check_that_is_not_an_install_names_nothing():
    assert rewritten_manifests(_check("frontend-unit"), ["frontend/package.json"]) == []


def test_no_command_and_no_manifest_name_nothing():
    assert rewritten_manifests(None, ["frontend/package.json"]) == []
    assert rewritten_manifests(_check("frontend-install"), ["frontend/src/index.ts"]) == []

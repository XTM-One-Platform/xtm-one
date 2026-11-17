"""A detected check carries its CI job's env, never a secret, and a call's env overrides it (#11503)."""

from __future__ import annotations

import pytest

from app.services.sandbox_workspace import checks, commands, detect

DBT_WORKFLOW = r"""
env:
  PYTHON_VERSION: "3.12"
jobs:
  dbt:
    defaults:
      run:
        working-directory: kube-deployment/dbt
    env:
      PG_HOST: "localhost"
      PG_PORT: "5432"
      PG_USER: "ci"
      PG_PASSWORD: ${{ secrets.PG_PASSWORD }}
      DBT_PYTHON: ${{ env.PYTHON_VERSION }}
      BUILD_DIR: ${{ github.workspace }}/build
    steps:
      - run: dbt deps --profiles-dir profiles
      - run: dbt parse --profiles-dir profiles
"""


def _detect(files: dict[str, str]) -> detect.Detection:
    listing = list(files)
    picked = detect.candidates(listing)
    return detect.detect(listing, {p: files[p].encode() for p in picked if p in files}, images={"python", "python-dev"})


def test_the_dbt_parse_check_runs_with_its_job_env_and_never_a_secret_value():
    parse = _detect({".github/workflows/ci.yml": DBT_WORKFLOW}).checks["kube-deployment/dbt:dbt-parse"]
    env = dict(parse.env)
    assert env["PG_HOST"] == "localhost" and env["PG_PORT"] == "5432" and env["PG_USER"] == "ci"
    # ${{ env.X }} reads the outer literal.
    assert env["DBT_PYTHON"] == "3.12"
    # A secret or a runner expression is never copied; its name is kept for the answer.
    assert "PG_PASSWORD" not in env and "BUILD_DIR" not in env
    assert "${{" not in "".join(env.values())
    assert parse.env_unset == ("BUILD_DIR", "PG_PASSWORD")


def test_a_literal_set_again_after_a_secret_travels_and_leaves_the_unset_list():
    workflow = DBT_WORKFLOW + "        env:\n          PG_PASSWORD: ci\n"
    parse = _detect({".github/workflows/ci.yml": workflow}).checks["kube-deployment/dbt:dbt-parse"]
    assert dict(parse.env)["PG_PASSWORD"] == "ci" and parse.env_unset == ("BUILD_DIR",)


def test_the_call_env_overrides_and_validates():
    assert commands.env_override(None) == {} and commands.env_override({}) == {}
    assert commands.env_override({"PG_PASSWORD": "ci", "N": 3, "FLAG": True}) == {
        "PG_PASSWORD": "ci",
        "N": "3",
        "FLAG": "true",
    }
    for bad in ({"PATH": "/x"}, {"GITHUB_TOKEN": "t"}, {"1X": "a"}, {"A": None}, {"A": ["x"]}, ["A"]):
        with pytest.raises(checks.ChecksError, match="env"):
            commands.env_override(bad)


def test_the_unset_note_names_the_variables_and_the_way_out():
    note = commands.unset_env_note(("PG_PASSWORD",))
    assert "PG_PASSWORD" in note and "env=" in note
    many = commands.unset_env_note(tuple(f"V{i}" for i in range(15)))
    assert "and 3 more" in many

"""Platform Health's skills after #1943: one module checks skill, specialist skills internal."""

from __future__ import annotations

from app.agents.builtin_agents import BUILTIN_SKILLS, RENAMED_BUILTIN_SKILL_SLUGS
from app.models.agent_skill import INTERNAL_SKILL_TAG, is_internal_skill
from app.services.health import check_reports
from app.services.health.check_reports import MODULE_CHECKS_SKILL_SLUG, module_playbook

_BY_SLUG = {s["slug"]: s for s in BUILTIN_SKILLS}
_HEALTH = {s["slug"] for s in BUILTIN_SKILLS if "health" in s.get("tags", [])}
_INTERNAL = {"memory-steward", "health-quality-analyst", "health-experimenter", "health-flow-lab"}


def test_health_ships_one_module_checks_skill_and_hides_the_specialists_skills() -> None:
    assert {*_INTERNAL, MODULE_CHECKS_SKILL_SLUG} == _HEALTH
    assert {s for s in _HEALTH if is_internal_skill(_BY_SLUG[s]["tags"])} == _INTERNAL
    # The fleet orchestrators' skills stay in the catalogue.
    for slug in ("fleet-health-advisory", "fleet-design-practices", MODULE_CHECKS_SKILL_SLUG):
        assert INTERNAL_SKILL_TAG not in _BY_SLUG[slug]["tags"], slug


def test_the_opencti_playbook_row_becomes_the_module_checks_skill() -> None:
    assert RENAMED_BUILTIN_SKILL_SLUGS["health-opencti-checks"] == MODULE_CHECKS_SKILL_SLUG
    assert "health-openaev-checks" not in RENAMED_BUILTIN_SKILL_SLUGS
    assert "health-opencrq-checks" not in RENAMED_BUILTIN_SKILL_SLUGS


def test_each_module_playbook_is_the_shared_rules_and_its_own_section() -> None:
    cti, aev, crq = (module_playbook(m) for m in ("opencti", "openaev", "opencrq"))
    for text in (cti, aev, crq):
        assert "## Rules" in text and "## Report" in text and "record_module_check_report" in text
    assert "## OpenCTI" in cti and "## OpenAEV" not in cti and "## OpenCRQ" not in cti
    assert "## OpenAEV" in aev and "## OpenCTI" not in aev and "get_opencti_managers" not in aev
    assert "## OpenCRQ" in crq and "## OpenAEV" not in crq
    # #1642: the managers_running semantics stay in the OpenCTI section.
    assert "`managers_running`" in cti and "A manager's `running` flag is not liveness" in cti
    assert module_playbook("unknown") == ""


def test_each_module_tracks_its_own_section_hash() -> None:
    """#1742 stale-finding tags keep working: one hash per module, distinct and stable."""
    check_reports._skill_hash.cache_clear()
    hashes = {m: check_reports.check_logic_version(m)["skill_hash"] for m in ("opencti", "openaev", "opencrq")}
    assert len(set(hashes.values())) == 3
    assert all(len(h) == 16 for h in hashes.values())
    check_reports._skill_hash.cache_clear()
    assert hashes["opencti"] == check_reports.check_logic_version("opencti")["skill_hash"]


def test_an_edit_of_one_module_section_leaves_the_other_modules_current(monkeypatch, tmp_path) -> None:
    from app.agents import seed

    source = (seed.SEED_DATA_DIR / "skill_content" / f"{MODULE_CHECKS_SKILL_SLUG}.md").read_text(encoding="utf-8")
    (tmp_path / "skill_content").mkdir()
    target = tmp_path / "skill_content" / f"{MODULE_CHECKS_SKILL_SLUG}.md"
    target.write_text(source, encoding="utf-8")
    monkeypatch.setattr(seed, "SEED_DATA_DIR", tmp_path)
    check_reports._skill_hash.cache_clear()
    before = {m: check_reports._skill_hash(m) for m in ("opencti", "openaev", "opencrq")}
    target.write_text(source.replace("| `jobs_state` |", "| `jobs_state` | edited"), encoding="utf-8")
    check_reports._skill_hash.cache_clear()
    after = {m: check_reports._skill_hash(m) for m in ("opencti", "openaev", "opencrq")}
    check_reports._skill_hash.cache_clear()
    assert after["opencrq"] != before["opencrq"]
    assert after["opencti"] == before["opencti"] and after["openaev"] == before["openaev"]


def test_is_internal_skill_reads_the_tag_only() -> None:
    assert is_internal_skill(["builtin", INTERNAL_SKILL_TAG])
    assert not is_internal_skill(["builtin"])
    assert not is_internal_skill(None)

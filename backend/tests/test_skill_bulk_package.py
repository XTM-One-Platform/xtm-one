"""Tests for multi-skill (bulk) archive parsing."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.services.skill_package_manager import (
    BULK_IGNORE_REASONS,
    parse_skill_bulk_package_zip,
)


def _skill_md(name: str, description: str, body: str = "Do work") -> bytes:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"{body}\n"
    ).encode()


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, payload in entries.items():
            zf.writestr(path, payload)
    return buf.getvalue()


def _by_name(parsed):
    return {entry.name: entry for entry in parsed.skills}


def _ignored(parsed):
    return {entry.path: entry.reason for entry in parsed.ignored}


def test_archive_with_several_skill_folders_yields_one_entry_each():
    raw = _zip(
        {
            "alpha-skill/SKILL.md": _skill_md("alpha-skill", "Alpha"),
            "beta-skill/SKILL.md": _skill_md("beta-skill", "Beta"),
            "gamma-skill/SKILL.md": _skill_md("gamma-skill", "Gamma"),
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    assert sorted(entry.name for entry in parsed.skills) == [
        "alpha-skill",
        "beta-skill",
        "gamma-skill",
    ]
    assert all(entry.error is None and entry.package is not None for entry in parsed.skills)
    assert parsed.ignored == []


def test_skill_file_named_after_the_skill_is_detected():
    """A folder that names the file ``<skill-name>.md`` instead of SKILL.md."""
    raw = _zip(
        {
            "phishing-triage/phishing-triage.md": _skill_md("phishing-triage", "Triage"),
            "malware-report.md": _skill_md("malware-report", "Report"),
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    entries = _by_name(parsed)
    assert set(entries) == {"phishing-triage", "malware-report"}
    assert entries["phishing-triage"].path == "phishing-triage/phishing-triage.md"
    assert entries["malware-report"].path == "malware-report.md"


def test_skill_md_wins_over_a_sibling_markdown_copy():
    raw = _zip(
        {
            "alpha-skill/SKILL.md": _skill_md("alpha-skill", "Alpha"),
            "alpha-skill/alpha-skill.md": _skill_md("alpha-skill", "Alpha copy"),
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    assert [entry.path for entry in parsed.skills] == ["alpha-skill/SKILL.md"]
    assert _ignored(parsed) == {"alpha-skill/alpha-skill.md": "not_a_skill_root"}


def test_non_skill_files_are_reported_as_ignored_with_a_reason():
    raw = _zip(
        {
            "alpha-skill/SKILL.md": _skill_md("alpha-skill", "Alpha"),
            "alpha-skill/scripts/run.py": b"print('ok')\n",
            "README.md": b"# Just a readme\n",
            "docs/architecture.png": b"\x89PNG\r\n",
            "__MACOSX/._SKILL.md": b"junk",
            ".DS_Store": b"junk",
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    assert [entry.name for entry in parsed.skills] == ["alpha-skill"]
    # The skill's own resource stays attached, it is not reported as ignored.
    assert parsed.skills[0].resource_count == 1
    ignored = _ignored(parsed)
    assert ignored == {
        "README.md": "no_frontmatter",
        "docs/architecture.png": "unsupported_file_type",
        "__MACOSX/._SKILL.md": "archive_metadata",
        ".DS_Store": "archive_metadata",
    }
    assert set(ignored.values()) <= set(BULK_IGNORE_REASONS)


def test_resources_and_versionning_follow_their_own_skill():
    manifest = json.dumps(
        {
            "schema_version": 1,
            "active_version": 2,
            "versions": [
                {"number": 1, "path": "0001/SKILL.md"},
                {"number": 2, "path": "0002/SKILL.md"},
            ],
        }
    ).encode("utf-8")
    head = _skill_md("alpha-skill", "Alpha", "v2 body")
    raw = _zip(
        {
            "alpha-skill/SKILL.md": head,
            "alpha-skill/scripts/run.py": b"print('ok')\n",
            "alpha-skill/references/REFERENCE.md": b"# Ref\n",
            "alpha-skill/versionning/manifest.json": manifest,
            "alpha-skill/versionning/0001/SKILL.md": _skill_md("alpha-skill", "Alpha", "v1 body"),
            "alpha-skill/versionning/0002/SKILL.md": head,
            "beta-skill/SKILL.md": _skill_md("beta-skill", "Beta"),
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    entries = _by_name(parsed)
    assert entries["alpha-skill"].resource_count == 2
    assert entries["alpha-skill"].version_count == 2
    # Beta owns nothing of alpha's payload.
    assert entries["beta-skill"].resource_count == 0
    assert entries["beta-skill"].version_count == 1


def test_folder_name_mismatch_is_a_warning_not_an_error():
    """The spec's parent-directory rule must not sink a whole archive."""
    raw = _zip(
        {
            "delta/SKILL.md": _skill_md("delta-renamed", "Delta"),
            "beta-skill/SKILL.md": _skill_md("beta-skill", "Beta"),
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    entries = _by_name(parsed)
    assert entries["delta-renamed"].error is None
    assert entries["delta-renamed"].package is not None
    assert any("does not match parent directory" in w for w in entries["delta-renamed"].warnings)
    assert entries["beta-skill"].error is None


def test_a_malformed_skill_does_not_sink_the_rest_of_the_archive():
    raw = _zip(
        {
            "bad/SKILL.md": _skill_md("Bad Name!", "Bad"),
            "good-skill/SKILL.md": _skill_md("good-skill", "Good"),
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    entries = {entry.path: entry for entry in parsed.skills}
    assert entries["bad/SKILL.md"].error is not None
    assert entries["bad/SKILL.md"].package is None
    assert entries["good-skill/SKILL.md"].error is None


def test_duplicate_names_inside_the_archive_are_surfaced():
    raw = _zip(
        {
            "a/SKILL.md": _skill_md("same-skill", "First"),
            "b/SKILL.md": _skill_md("same-skill", "Second"),
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    entries = {entry.path: entry for entry in parsed.skills}
    assert entries["a/SKILL.md"].error is None
    assert "Duplicate skill name" in (entries["b/SKILL.md"].error or "")
    assert entries["b/SKILL.md"].package is None


def test_several_skills_in_one_folder_leave_resources_unattached():
    raw = _zip(
        {
            "pack/first-skill.md": _skill_md("first-skill", "First"),
            "pack/second-skill.md": _skill_md("second-skill", "Second"),
            "pack/scripts/run.py": b"print('ok')\n",
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    assert sorted(entry.name for entry in parsed.skills) == ["first-skill", "second-skill"]
    assert all(entry.resource_count == 0 for entry in parsed.skills)
    assert _ignored(parsed) == {"pack/scripts/run.py": "ambiguous_owner"}
    assert all(
        any("share this folder" in warning for warning in entry.warnings)
        for entry in parsed.skills
    )


def test_markdown_without_frontmatter_is_ignored_not_failed():
    raw = _zip(
        {
            "alpha-skill/SKILL.md": _skill_md("alpha-skill", "Alpha"),
            "notes/todo.md": b"# just notes\n",
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    assert [entry.name for entry in parsed.skills] == ["alpha-skill"]
    assert _ignored(parsed) == {"notes/todo.md": "no_frontmatter"}


def test_non_utf8_skill_document_is_ignored_as_invalid_encoding():
    raw = _zip(
        {
            "alpha-skill/SKILL.md": _skill_md("alpha-skill", "Alpha"),
            "broken/SKILL.md": b"---\nname: broken\n\xff\xfe---\n",
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    assert [entry.name for entry in parsed.skills] == ["alpha-skill"]
    assert _ignored(parsed) == {"broken/SKILL.md": "invalid_encoding"}


def test_archive_without_any_skill_raises():
    raw = _zip({"README.md": b"# nothing here\n"})
    with pytest.raises(ValueError, match="No skill was found"):
        parse_skill_bulk_package_zip(raw)


def test_single_skill_package_still_parses_through_the_bulk_path():
    """The review flow runs every ZIP through the bulk parser, including the
    spec's single-skill package."""
    raw = _zip(
        {
            "my-skill/SKILL.md": _skill_md("my-skill", "Mine"),
            "my-skill/scripts/run.py": b"print('ok')\n",
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    assert len(parsed.skills) == 1
    entry = parsed.skills[0]
    assert entry.name == "my-skill"
    assert entry.resource_count == 1
    assert entry.package is not None


def test_unsafe_paths_are_still_rejected_at_archive_level():
    raw = _zip({"../evil/SKILL.md": _skill_md("evil", "Evil")})
    with pytest.raises(ValueError, match="Unsafe ZIP path"):
        parse_skill_bulk_package_zip(raw)


def test_too_many_skills_is_an_archive_level_error():
    entries = {
        f"skill-{index:03d}/SKILL.md": _skill_md(f"skill-{index:03d}", "Many")
        for index in range(101)
    }
    with pytest.raises(ValueError, match="too many skills"):
        parse_skill_bulk_package_zip(_zip(entries))


def test_wrapper_folder_from_zipping_a_skills_directory_is_peeled_off():
    """Zipping a `skills/` folder produces `skills/<skill>/SKILL.md`."""
    raw = _zip(
        {
            "skills/alpha-skill/SKILL.md": _skill_md("alpha-skill", "Alpha"),
            "skills/alpha-skill/scripts/run.py": b"print('ok')\n",
            "skills/beta-skill/beta-skill.md": _skill_md("beta-skill", "Beta"),
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    entries = _by_name(parsed)
    assert set(entries) == {"alpha-skill", "beta-skill"}
    assert entries["alpha-skill"].resource_count == 1
    assert parsed.ignored == []


def test_a_single_skill_folder_is_never_peeled_as_a_wrapper():
    raw = _zip(
        {
            "my-skill/SKILL.md": _skill_md("my-skill", "Mine"),
            "my-skill/scripts/run.py": b"print('ok')\n",
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    assert [entry.path for entry in parsed.skills] == ["my-skill/SKILL.md"]
    assert parsed.skills[0].resource_count == 1


def test_spec_skill_directories_with_full_resource_layout():
    """The agentskills.io layout — SKILL.md in a named directory with its
    scripts/, references/, assets/ and examples/ folders — repeated N times."""
    raw = _zip(
        {
            "alpha-skill/SKILL.md": _skill_md("alpha-skill", "Alpha"),
            "alpha-skill/scripts/run.py": b"print('ok')\n",
            "alpha-skill/scripts/lib/helper.py": b"# nested\n",
            "alpha-skill/references/GUIDE.md": b"---\nname: guide\ndescription: A guide\n---\n\nRef\n",
            "alpha-skill/assets/logo.png": b"\x89PNG\r\n",
            "alpha-skill/examples/sample.md": b"# sample\n",
            "beta-skill/SKILL.md": _skill_md("beta-skill", "Beta"),
            "beta-skill/references/NOTES.md": b"# notes\n",
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    entries = _by_name(parsed)
    assert set(entries) == {"alpha-skill", "beta-skill"}
    assert entries["alpha-skill"].resource_count == 5
    assert entries["beta-skill"].resource_count == 1
    # A reference document carrying frontmatter belongs to its skill; it must
    # never be promoted to a skill of its own.
    assert parsed.ignored == []


def test_version_snapshots_are_never_detected_as_skills():
    manifest = json.dumps(
        {
            "schema_version": 1,
            "active_version": 1,
            "versions": [{"number": 1, "path": "0001/SKILL.md"}],
        }
    ).encode()
    head = _skill_md("alpha-skill", "Alpha")
    raw = _zip(
        {
            "alpha-skill/SKILL.md": head,
            "alpha-skill/versionning/manifest.json": manifest,
            "alpha-skill/versionning/0001/SKILL.md": head,
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    assert [entry.path for entry in parsed.skills] == ["alpha-skill/SKILL.md"]
    assert parsed.skills[0].version_count == 1
    assert parsed.ignored == []


def test_readme_next_to_the_skill_folders_does_not_hide_them():
    """A wrapper folder usually ships a README beside the skill directories."""
    raw = _zip(
        {
            "skills-pack/README.md": b"# The pack\n",
            "skills-pack/alpha-skill/SKILL.md": _skill_md("alpha-skill", "Alpha"),
            "skills-pack/beta-skill/SKILL.md": _skill_md("beta-skill", "Beta"),
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    assert sorted(entry.name for entry in parsed.skills) == ["alpha-skill", "beta-skill"]
    assert _ignored(parsed) == {"skills-pack/README.md": "no_frontmatter"}


def test_skill_directories_are_found_at_any_depth():
    raw = _zip(
        {
            "export/2026/skills/alpha-skill/SKILL.md": _skill_md("alpha-skill", "Alpha"),
            "export/2026/skills/alpha-skill/scripts/run.py": b"print('ok')\n",
            "export/2026/skills/beta-skill/beta-skill.md": _skill_md("beta-skill", "Beta"),
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    entries = _by_name(parsed)
    assert set(entries) == {"alpha-skill", "beta-skill"}
    assert entries["alpha-skill"].resource_count == 1
    assert parsed.ignored == []


def test_unmapped_folder_inside_a_skill_is_reported_not_attached():
    raw = _zip(
        {
            "alpha-skill/SKILL.md": _skill_md("alpha-skill", "Alpha"),
            "alpha-skill/templates/report.docx": b"PK\x03\x04",
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)

    assert [entry.name for entry in parsed.skills] == ["alpha-skill"]
    assert parsed.skills[0].resource_count == 0
    assert _ignored(parsed) == {"alpha-skill/templates/report.docx": "unsupported_file_type"}

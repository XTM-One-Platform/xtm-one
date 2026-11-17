"""Tests for skill package ZIP parsing/building."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.services.skill_package_manager import (
    SkillPackageResource,
    SkillPackageVersion,
    build_skill_package_zip,
    complete_package_frontmatter,
    derive_package_root_name,
    package_skill_name,
    parse_skill_bulk_package_zip,
    parse_skill_import_upload,
    parse_skill_markdown,
    parse_skill_package_zip,
    rewrite_frontmatter_name,
    validate_frontmatter_warnings,
)


def _skill_md(name: str, description: str, body: str = "Do work") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"{body}\n"
    )


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, payload in entries.items():
            zf.writestr(path, payload)
    return buf.getvalue()


def test_parse_package_with_resources_and_versionning_manifest():
    root = _skill_md("my-skill", "Useful skill", "active body")
    v1 = _skill_md("my-skill", "Useful skill", "v1 body")
    v2 = root
    manifest = (
        "{"
        '"schema_version":1,'
        '"active_version":2,'
        '"versions":['
        '{"number":1,"path":"0001/SKILL.md"},'
        '{"number":2,"path":"0002/SKILL.md"}'
        "]}"
    )
    raw = _zip(
        {
            "my-skill/SKILL.md": root.encode("utf-8"),
            "my-skill/scripts/run.py": b"print('ok')\n",
            "my-skill/references/REFERENCE.md": b"# Ref\n",
            "my-skill/versionning/manifest.json": manifest.encode("utf-8"),
            "my-skill/versionning/0001/SKILL.md": v1.encode("utf-8"),
            "my-skill/versionning/0002/SKILL.md": v2.encode("utf-8"),
        }
    )

    parsed = parse_skill_package_zip(raw)
    assert parsed.name == "my-skill"
    assert parsed.active_version == 2
    assert len(parsed.resources) == 2
    assert [v.number for v in parsed.versions] == [1, 2]
    assert parsed.root_content == root


def test_parse_package_without_manifest_initializes_default_history():
    root = _skill_md("my-skill", "Useful skill")
    raw = _zip({"my-skill/SKILL.md": root.encode("utf-8")})

    parsed = parse_skill_package_zip(raw)
    assert parsed.active_version == 1
    assert len(parsed.versions) == 1
    assert "No versionning manifest found" in parsed.warnings[0]


def test_parse_skill_markdown_initializes_single_version():
    root = _skill_md("my-skill", "Useful skill", "plain markdown import")
    parsed = parse_skill_markdown(root.encode("utf-8"))
    assert parsed.name == "my-skill"
    assert parsed.active_version == 1
    assert parsed.resources == []
    assert len(parsed.versions) == 1
    assert "No versionning manifest found" in parsed.warnings[0]


def test_parse_skill_import_upload_selects_parser_from_filename():
    root = _skill_md("my-skill", "Useful skill", "plain markdown import")
    parsed = parse_skill_import_upload(root.encode("utf-8"), filename="SKILL.md")
    assert parsed.name == "my-skill"
    assert parsed.active_version == 1


def test_parse_skill_import_upload_uses_zip_parser_for_zip_filename():
    root = _skill_md("my-skill", "Useful skill")
    raw = _zip({"my-skill/SKILL.md": root.encode("utf-8")})
    parsed = parse_skill_import_upload(raw, filename="package.zip")
    assert parsed.name == "my-skill"
    assert parsed.active_version == 1


def test_parse_package_retains_examples_directory_as_is():
    """``examples/`` content is part of the package format and must
    round-trip through import/export untouched (never mapped)."""
    root = _skill_md("my-skill", "Useful skill")
    raw = _zip(
        {
            "my-skill/SKILL.md": root.encode("utf-8"),
            "my-skill/examples/demo.md": b"# Demo\n",
        }
    )
    parsed = parse_skill_package_zip(raw)
    assert [r.path for r in parsed.resources] == ["examples/demo.md"]


def test_parse_package_retains_singular_reference_directory():
    """Anthropic-style packages use ``reference/`` (singular) — must be kept."""
    root = _skill_md("my-skill", "Useful skill")
    raw = _zip(
        {
            "my-skill/SKILL.md": root.encode("utf-8"),
            "my-skill/reference/NOTES.md": b"# Notes\n",
            "my-skill/references/REFERENCE.md": b"# Ref\n",
        }
    )
    parsed = parse_skill_package_zip(raw)
    assert sorted(r.path for r in parsed.resources) == [
        "reference/NOTES.md",
        "references/REFERENCE.md",
    ]


def test_parse_package_rejects_path_traversal():
    raw = _zip({"../evil/SKILL.md": b"---\nname: evil\ndescription: x\n---\n"})
    with pytest.raises(ValueError, match="Unsafe ZIP path"):
        parse_skill_package_zip(raw)


def test_parse_package_rejects_backslash_path_traversal():
    """Windows-style backslash segments must be rejected — PurePosixPath
    treats "..\\..\\evil.py" as a single literal part, so it would slip past
    the ".." check and could zip-slip when the embedded workspace ZIP is
    extracted on Windows."""
    root = _skill_md("my-skill", "Useful skill")
    raw = _zip(
        {
            "my-skill/SKILL.md": root.encode("utf-8"),
            "my-skill/scripts/..\\..\\evil.py": b"print('x')\n",
        }
    )
    with pytest.raises(ValueError, match="Unsafe ZIP path"):
        parse_skill_package_zip(raw)


def test_parse_package_rejects_oversized_resource_before_decompression():
    root = _skill_md("my-skill", "Useful skill")
    # 6 MB of zeros compresses to a few KB — the declared decompressed size
    # must be rejected before the payload is ever inflated.
    raw = _zip(
        {
            "my-skill/SKILL.md": root.encode("utf-8"),
            "my-skill/scripts/big.py": b"0" * (6 * 1024 * 1024),
        }
    )
    with pytest.raises(ValueError, match="exceeds maximum file size"):
        parse_skill_package_zip(raw)


def test_parse_package_rejects_total_decompressed_size_above_cap():
    entries = {"my-skill/SKILL.md": _skill_md("my-skill", "Useful skill").encode("utf-8")}
    for idx in range(25):
        entries[f"my-skill/assets/blob-{idx}.bin"] = b"0" * (5 * 1024 * 1024)
    raw = _zip(entries)
    with pytest.raises(ValueError, match="decompressed size exceeds"):
        parse_skill_package_zip(raw)


def test_parse_package_accepts_renamed_skill_version_history():
    """Version snapshots may predate a rename — only root must match the dir."""
    root = _skill_md("new-name", "Useful skill", "active body")
    v1 = _skill_md("old-name", "Useful skill", "v1 body")
    manifest = (
        "{"
        '"schema_version":1,'
        '"active_version":2,'
        '"versions":['
        '{"number":1,"path":"0001/SKILL.md"},'
        '{"number":2,"path":"0002/SKILL.md"}'
        "]}"
    )
    raw = _zip(
        {
            "new-name/SKILL.md": root.encode("utf-8"),
            "new-name/versionning/manifest.json": manifest.encode("utf-8"),
            "new-name/versionning/0001/SKILL.md": v1.encode("utf-8"),
            "new-name/versionning/0002/SKILL.md": root.encode("utf-8"),
        }
    )
    parsed = parse_skill_package_zip(raw)
    assert parsed.name == "new-name"
    assert [v.number for v in parsed.versions] == [1, 2]


def test_rewrite_frontmatter_name_replaces_only_the_name_field():
    content = _skill_md("old-name", "Useful skill", "body line")
    rewritten = rewrite_frontmatter_name(content, "new-name")
    assert "name: new-name" in rewritten
    assert "old-name" not in rewritten
    assert "description: Useful skill" in rewritten
    assert "body line" in rewritten
    # Round-trip: the rewritten document re-derives the new name.
    assert derive_package_root_name(rewritten, fallback="x") == "new-name"


def test_rewrite_frontmatter_name_leaves_unrewritable_content_unchanged():
    assert rewrite_frontmatter_name("# no frontmatter", "x") == "# no frontmatter"
    block_scalar = "---\nname: >\n  my-skill\ndescription: d\n---\n\nBody\n"
    assert rewrite_frontmatter_name(block_scalar, "x") == block_scalar


def test_derive_package_root_name_prefers_frontmatter_name():
    content = _skill_md("frontmatter-name", "Useful skill")
    assert derive_package_root_name(content, fallback="Display Name") == "frontmatter-name"
    assert derive_package_root_name("no frontmatter", fallback="fallback-name") == "fallback-name"
    assert derive_package_root_name(None, fallback="fallback-name") == "fallback-name"


def test_parse_package_rejects_active_version_that_is_not_latest():
    """The platform treats max(version_number) as active - a manifest whose
    active_version points to an older version would import inconsistently."""
    v1 = _skill_md("my-skill", "Useful skill", "v1 body")
    v2 = _skill_md("my-skill", "Useful skill", "v2 body")
    manifest = (
        "{"
        '"schema_version":1,'
        '"active_version":1,'
        '"versions":['
        '{"number":1,"path":"0001/SKILL.md"},'
        '{"number":2,"path":"0002/SKILL.md"}'
        "]}"
    )
    raw = _zip(
        {
            "my-skill/SKILL.md": v1.encode("utf-8"),
            "my-skill/versionning/manifest.json": manifest.encode("utf-8"),
            "my-skill/versionning/0001/SKILL.md": v1.encode("utf-8"),
            "my-skill/versionning/0002/SKILL.md": v2.encode("utf-8"),
        }
    )
    with pytest.raises(ValueError, match="must be the latest version number"):
        parse_skill_package_zip(raw)


def test_parse_package_rejects_manifest_active_mismatch():
    root = _skill_md("my-skill", "Useful skill", "root body")
    v1 = _skill_md("my-skill", "Useful skill", "v1 body")
    manifest = (
        "{"
        '"schema_version":1,'
        '"active_version":1,'
        '"versions":[{"number":1,"path":"0001/SKILL.md"}]'
        "}"
    )
    raw = _zip(
        {
            "my-skill/SKILL.md": root.encode("utf-8"),
            "my-skill/versionning/manifest.json": manifest.encode("utf-8"),
            "my-skill/versionning/0001/SKILL.md": v1.encode("utf-8"),
        }
    )
    with pytest.raises(ValueError, match="must match content"):
        parse_skill_package_zip(raw)


def test_build_package_zip_roundtrip():
    root = _skill_md("my-skill", "Useful skill", "active body")
    raw = build_skill_package_zip(
        root_dir_name="my-skill",
        root_skill_content=root,
        resources=[SkillPackageResource(path="scripts/run.py", content=b"print('ok')\n")],
        versions=[
            SkillPackageVersion(
                number=1,
                content=_skill_md("my-skill", "Useful skill", "v1 body"),
                description="Useful skill",
                compatibility=None,
            ),
            SkillPackageVersion(
                number=2,
                content=root,
                description="Useful skill",
                compatibility=None,
            ),
        ],
        include_versionning=True,
    )
    parsed = parse_skill_package_zip(raw)
    assert parsed.name == "my-skill"
    assert parsed.active_version == 2
    assert len(parsed.resources) == 1


# ---------------------------------------------------------------------------
# validate_frontmatter_warnings
# ---------------------------------------------------------------------------


def test_validate_frontmatter_warnings_valid():
    content = _skill_md("my-skill", "A good description")
    assert validate_frontmatter_warnings(content) == []


def test_validate_frontmatter_warnings_missing_name():
    content = "---\ndescription: hello\n---\n\nbody"
    warnings = validate_frontmatter_warnings(content)
    assert len(warnings) == 1
    assert "`name`" in warnings[0]


def test_validate_frontmatter_warnings_missing_description():
    content = "---\nname: my-skill\n---\n\nbody"
    warnings = validate_frontmatter_warnings(content)
    assert len(warnings) == 1
    assert "`description`" in warnings[0]


def test_validate_frontmatter_warnings_missing_both():
    content = "---\n---\n\nbody"
    warnings = validate_frontmatter_warnings(content)
    assert len(warnings) == 2
    assert any("`name`" in w for w in warnings)
    assert any("`description`" in w for w in warnings)


def test_validate_frontmatter_warnings_no_frontmatter():
    content = "Just a body, no frontmatter"
    warnings = validate_frontmatter_warnings(content)
    assert len(warnings) == 2


def test_validate_frontmatter_warnings_none_content():
    warnings = validate_frontmatter_warnings(None)
    assert len(warnings) == 1
    assert "no content" in warnings[0].lower()


def test_validate_frontmatter_warnings_invalid_name_format():
    content = "---\nname: My Skill\ndescription: hello\n---\n\nbody"
    warnings = validate_frontmatter_warnings(content)
    assert len(warnings) == 1
    assert "lowercase" in warnings[0]


# ---------------------------------------------------------------------------
# complete_package_frontmatter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [
        ("Code Review", "code-review"),
        ("  Revue de code — équipe  ", "revue-de-code-equipe"),
        ("snake_case__name", "snake-case-name"),
        ("a" * 80, "a" * 64),
        ("日本語", None),
        ("", None),
        (None, None),
    ],
)
def test_package_skill_name(display_name, expected):
    assert package_skill_name(display_name) == expected


def test_complete_package_frontmatter_adds_both_to_a_bare_body():
    content = "Review the diff.\n\n## Steps\n"
    completed = complete_package_frontmatter(
        content, name="Code Review", description="Reviews a diff"
    )
    assert completed == (
        "---\nname: code-review\ndescription: Reviews a diff\n---\n\n" + content
    )
    assert validate_frontmatter_warnings(completed) == []


def test_complete_package_frontmatter_fills_only_what_is_missing():
    content = "---\ncompatibility: any\n---\n\nBody\n"
    completed = complete_package_frontmatter(
        content, name="Code Review", description="Reviews a diff"
    )
    assert completed == (
        "---\nname: code-review\ndescription: Reviews a diff\ncompatibility: any\n---\n\nBody\n"
    )
    only_name_missing = _skill_md("kept-name", "kept description").replace(
        "name: kept-name\n", ""
    )
    meta = parse_skill_markdown(
        complete_package_frontmatter(
            only_name_missing, name="Other Name", description="Ignored"
        ).encode()
    )
    assert (meta.name, meta.description) == ("other-name", "kept description")


def test_complete_package_frontmatter_never_overrides_the_frontmatter():
    content = _skill_md("kept-name", "kept description")
    assert (
        complete_package_frontmatter(content, name="Display", description="Column")
        == content
    )


def test_complete_package_frontmatter_replaces_empty_entries():
    content = "---\nname:\ndescription: \n---\n\nBody\n"
    completed = complete_package_frontmatter(
        content, name="Code Review", description="Reviews a diff"
    )
    assert completed.count("name:") == 1
    assert completed.count("description:") == 1
    assert validate_frontmatter_warnings(completed) == []


def test_complete_package_frontmatter_keeps_a_risky_description_parseable():
    description = 'Reviews: diffs # fast\nand "safe"'
    completed = complete_package_frontmatter(
        "Body\n", name="Code Review", description=description
    )
    parsed = parse_skill_markdown(completed.encode())
    assert parsed.description == 'Reviews: diffs # fast and "safe"'


def test_complete_package_frontmatter_keeps_crlf_line_endings():
    content = "---\r\ncompatibility: any\r\n---\r\n\r\nBody\r\n"
    completed = complete_package_frontmatter(
        content, name="Code Review", description="Reviews a diff"
    )
    assert "\n" not in completed.replace("\r\n", "")
    assert validate_frontmatter_warnings(completed) == []


def test_complete_package_frontmatter_leaves_the_gap_without_a_usable_name():
    completed = complete_package_frontmatter("Body\n", name="日本語", description="d")
    assert validate_frontmatter_warnings(completed) == [
        "SKILL.md frontmatter is missing required `name`."
    ]


def test_completed_export_round_trips_through_the_package_import():
    completed = complete_package_frontmatter(
        "Review the diff.\n", name="Code Review", description="Reviews a diff"
    )
    root = derive_package_root_name(completed, fallback="Code Review")
    assert root == "code-review"
    archive = build_skill_package_zip(
        root_dir_name=root,
        root_skill_content=completed,
        resources=[],
        versions=[
            SkillPackageVersion(
                number=1, content=completed, description="Reviews a diff", compatibility=None
            )
        ],
    )
    parsed = parse_skill_package_zip(archive)
    assert (parsed.name, parsed.description) == ("code-review", "Reviews a diff")


@pytest.mark.parametrize("description", ["Yes", "true", "Null", "2024", "1.5", "1_000", "off"])
def test_complete_package_frontmatter_quotes_yaml_ambiguous_descriptions(description):
    completed = complete_package_frontmatter(
        "Body\n", name="Code Review", description=description
    )
    # A folded block always reads back as a string in a YAML reader.
    assert f"description: >-\n  {description}\n" in completed
    assert parse_skill_markdown(completed.encode()).description == description


def test_complete_package_frontmatter_keeps_indented_lines_of_another_key():
    content = "---\ncompatibility: |\n  name:\n  description:\n---\n\nBody\n"
    completed = complete_package_frontmatter(
        content, name="Code Review", description="Reviews a diff"
    )
    assert "compatibility: |\n  name:\n  description:\n---" in completed
    assert completed.startswith("---\nname: code-review\ndescription: Reviews a diff\n")


def test_validate_frontmatter_warnings_judges_the_fallbacks_like_the_export():
    assert validate_frontmatter_warnings("Body\n") != []
    assert (
        validate_frontmatter_warnings(
            "Body\n", fallback_name="Code Review", fallback_description="Reviews a diff"
        )
        == []
    )
    assert validate_frontmatter_warnings(
        "Body\n", fallback_name="日本語", fallback_description="Reviews a diff"
    ) == ["SKILL.md frontmatter is missing required `name`."]
    assert validate_frontmatter_warnings(
        "Body\n", fallback_name="Code Review", fallback_description="x" * 1025
    ) == ["`description` exceeds max length (1024)."]
# ── Members read within a bound, not as they declare ─────────────────


def test_a_member_declaring_less_than_it_holds_is_refused_having_decompressed_what_it_declared():
    """``ZipFile.read`` holds such a member twice over (128 MiB here)."""
    from tests.archive_helpers import declare_size, peak_mib

    body = _skill_md("my-skill", "Useful skill", " " * (64 * 2**20))
    raw = declare_size(_zip({"my-skill/SKILL.md": body.encode()}), "my-skill/SKILL.md", 10)
    peak, raised = peak_mib(lambda: parse_skill_package_zip(raw))
    assert type(raised) is ValueError
    assert str(raised) == "Invalid ZIP file."
    assert peak <= 16


@pytest.mark.parametrize("how", ["damaged stream", "unknown compression", "encrypted"])
def test_an_unreadable_member_is_an_invalid_zip_not_a_500(how):
    from tests.archive_helpers import corrupt_member

    raw = _zip(
        {
            "my-skill/SKILL.md": _skill_md("my-skill", "Useful skill").encode(),
            "my-skill/references/notes.md": bytes(range(256)) * 64,
        }
    )
    with pytest.raises(ValueError, match=r"^Invalid ZIP file\.$"):
        parse_skill_package_zip(corrupt_member(raw, "my-skill/references/notes.md", how))


def test_a_bulk_skill_document_over_the_file_cap_is_still_ignored_as_too_large(monkeypatch):
    from app.services import skill_package_manager

    small = _skill_md("small-skill", "Fits")
    monkeypatch.setattr(skill_package_manager, "_MAX_FILE_BYTES", len(small) + 10)
    raw = _zip(
        {
            "small-skill/SKILL.md": small.encode(),
            "big-skill/SKILL.md": _skill_md("big-skill", "Too big", "x" * 500).encode(),
        }
    )
    parsed = parse_skill_bulk_package_zip(raw)
    assert [entry.path for entry in parsed.skills] == ["small-skill/SKILL.md"]
    assert [(entry.path, entry.reason) for entry in parsed.ignored] == [("big-skill/SKILL.md", "too_large")]

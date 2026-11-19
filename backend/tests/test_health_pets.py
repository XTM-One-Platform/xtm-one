"""Platform Health companion's pet packages (#8261): the format, the built-in Alfred, hostile uploads.

Every refusal is asserted by its code, one case per family: size, container,
manifest, path, image type, image size, image content.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from app.services.health import pets
from app.services.health.pets import ATLAS_SIZE, PetPackageError, parse_package

V1 = ATLAS_SIZE[1]
V2 = ATLAS_SIZE[2]
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_butler_pet.py"


def _image(size=V1, fmt="PNG", **save) -> bytes:
    out = io.BytesIO()
    Image.new("RGB" if fmt == "JPEG" else "RGBA", size, (10, 20, 30, 255)[: 3 if fmt == "JPEG" else 4]).save(
        out, format=fmt, **save
    )
    return out.getvalue()


def _manifest(**fields) -> bytes:
    data = {"id": "owl", "displayName": "Owl", "description": "A night owl.", "spritesheetPath": "spritesheet.png"}
    data.update(fields)
    return json.dumps({k: v for k, v in data.items() if v is not ...}).encode()


def _zip(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return out.getvalue()


def _refused(files) -> str:
    with pytest.raises(PetPackageError) as exc:
        parse_package(files)
    return exc.value.code


# ── The format ─────────────────────────────────────────────────────────────


def test_the_format_is_the_codex_pet_atlas() -> None:
    assert (V1, V2) == ((1536, 1872), (1536, 2288))
    assert [(state, row, len(durations)) for state, row, durations in pets.STATES] == [
        ("idle", 0, 6),
        ("running-right", 1, 8),
        ("running-left", 2, 8),
        ("waving", 3, 4),
        ("jumping", 4, 5),
        ("failed", 5, 8),
        ("waiting", 6, 6),
        ("running", 7, 6),
        ("review", 8, 6),
    ]
    assert pets.STATES[0][2] == (280, 110, 110, 140, 140, 320)


def test_a_package_reads_as_a_zip_or_as_two_files() -> None:
    sheet = _image()
    from_zip = parse_package([("owl.zip", _zip({"owl/pet.json": _manifest(), "owl/spritesheet.png": sheet}))])
    from_files = parse_package([("pet.json", _manifest()), ("spritesheet.png", sheet)])
    assert from_zip.manifest.id == from_files.manifest.id == "owl"
    assert from_zip.spritesheet == from_files.spritesheet
    assert from_zip.spritesheet[:4] == b"RIFF" and from_zip.spritesheet[8:12] == b"WEBP"


def test_the_stored_spritesheet_is_the_platforms_own_encoding() -> None:
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "<script>alert(1)</script>")
    sheet = _image(pnginfo=info)
    parsed = parse_package([("pet.json", _manifest()), ("spritesheet.png", sheet)])
    assert parsed.spritesheet != sheet
    assert b"<script>" not in parsed.spritesheet
    decoded = Image.open(io.BytesIO(parsed.spritesheet))
    assert (decoded.format, decoded.size) == ("WEBP", V1)


def test_a_version_2_package_takes_the_taller_atlas() -> None:
    parsed = parse_package([("pet.json", _manifest(spriteVersionNumber=2)), ("s.png", _image(V2))])
    assert parsed.manifest.sprite_version == 2
    assert _refused([("pet.json", _manifest(spriteVersionNumber=2)), ("s.png", _image(V1))]) == "image_size"


def test_unknown_manifest_keys_are_ignored_and_strings_trimmed() -> None:
    parsed = parse_package([("pet.json", _manifest(displayName="  Owl  ", extra={"x": 1})), ("s.png", _image())])
    assert (parsed.manifest.display_name, parsed.manifest.description) == ("Owl", "A night owl.")


# ── Hostile uploads ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ([("pet.zip", b"PK\x03\x04" + b"\0" * (pets.MAX_PACKAGE_BYTES + 1))], "too_large"),
        ([("pet.zip", b"not a zip at all")], "not_a_package"),
        ([("pet.zip", b"PK\x03\x04garbage")], "not_a_package"),
        ([("a", b"1"), ("b", b"2"), ("c", b"3")], "not_a_package"),
        ([("a.png", _image()), ("b.png", _image())], "not_a_package"),
        ([], "not_a_package"),
    ],
    ids=["too-large", "not-zip", "bad-zip", "three-files", "no-manifest", "no-file"],
)
def test_a_package_that_is_not_one_is_refused(files, code) -> None:
    assert _refused(files) == code


def test_a_zip_with_too_many_entries_or_two_manifests_is_refused() -> None:
    many = {f"f{i}.txt": b"x" for i in range(pets.MAX_ZIP_ENTRIES + 1)}
    assert _refused([("p.zip", _zip({"pet.json": _manifest(), **many}))]) == "not_a_package"
    two = _zip({"pet.json": _manifest(), "other/pet.json": _manifest(), "spritesheet.png": _image()})
    assert _refused([("p.zip", two)]) == "manifest"
    deep = _zip({"a/b/pet.json": _manifest(), "a/b/spritesheet.png": _image()})
    assert _refused([("p.zip", deep)]) == "manifest"


def test_an_entry_larger_than_its_cap_is_refused_whatever_its_header_says() -> None:
    bomb = b"\0" * (pets.MAX_PACKAGE_BYTES + 10)
    archive = _zip({"pet.json": _manifest(), "spritesheet.png": bomb})
    assert len(archive) < pets.MAX_PACKAGE_BYTES
    assert _refused([("p.zip", archive)]) == "too_large"
    big_manifest = _zip({"pet.json": b"{" + b" " * pets.MAX_MANIFEST_BYTES + b"}", "spritesheet.png": _image()})
    assert _refused([("p.zip", big_manifest)]) == "manifest"


@pytest.mark.parametrize(
    "manifest",
    [
        b"not json",
        b"[1, 2]",
        b"\xff\xfe{}",
        _manifest(id="Owl"),
        _manifest(id="../owl"),
        _manifest(id="o" * 65),
        _manifest(id=12),
        _manifest(id=...),
        _manifest(displayName=""),
        _manifest(displayName="   "),
        _manifest(displayName="Owl\nPwned"),
        _manifest(displayName="Owl‮"),
        _manifest(displayName="O" * 65),
        _manifest(description="d" * 281),
        _manifest(description="line break"),
        _manifest(spriteVersionNumber=3),
        _manifest(spriteVersionNumber="2"),
        _manifest(spritesheetPath="../spritesheet.png"),
        _manifest(spritesheetPath="/etc/spritesheet.png"),
        _manifest(spritesheetPath="a\\spritesheet.png"),
        _manifest(spritesheetPath="a/../../spritesheet.png"),
        _manifest(spritesheetPath="spritesheet.svg"),
        _manifest(spritesheetPath="spritesheet.gif"),
        _manifest(spritesheetPath="p" * 125 + ".png"),
    ],
)
def test_a_hostile_manifest_is_refused(manifest: bytes) -> None:
    assert _refused([("pet.json", manifest), ("spritesheet.png", _image())]) == "manifest"


def test_a_spritesheet_the_manifest_names_must_be_in_the_zip() -> None:
    archive = _zip({"pet.json": _manifest(spritesheetPath="missing.png"), "spritesheet.png": _image()})
    assert _refused([("p.zip", archive)]) == "spritesheet_missing"


def _gif() -> bytes:
    out = io.BytesIO()
    Image.new("P", V1).save(out, format="GIF")
    return out.getvalue()


def _animated_webp() -> bytes:
    out = io.BytesIO()
    frames = [Image.new("RGBA", V1, (i, 0, 0, 255)) for i in (0, 255)]
    frames[0].save(out, format="WEBP", save_all=True, append_images=frames[1:], lossless=True)
    return out.getvalue()


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1536" height="1872"><script>alert(1)</script></svg>'


@pytest.mark.parametrize(
    ("sheet", "code"),
    [
        (SVG, "image_type"),
        (b"RIFF\0\0\0\0WEBP" + SVG, "image_unreadable"),
        (_gif(), "image_type"),
        (_image(fmt="JPEG"), "image_type"),
        (_animated_webp(), "image_type"),
        (_image((1536, 1871)), "image_size"),
        (_image((1537, 1872)), "image_size"),
        (_image((192, 208)), "image_size"),
        (_image()[:200], "image_unreadable"),
    ],
    ids=["svg", "svg-behind-webp-magic", "gif", "jpeg", "animated-webp", "short", "wide", "one-cell", "truncated"],
)
def test_a_hostile_spritesheet_is_refused(sheet: bytes, code: str) -> None:
    assert _refused([("pet.json", _manifest()), ("spritesheet.png", sheet)]) == code


def test_a_webp_spritesheet_is_taken() -> None:
    parsed = parse_package([("pet.json", _manifest(spritesheetPath="s.webp")), ("s.webp", _image(fmt="WEBP"))])
    assert parsed.manifest.spritesheet_path == "s.webp"


def test_every_refusal_tells_the_rule_not_the_input() -> None:
    assert set(pets.ERROR_MESSAGES) == {
        "too_large",
        "not_a_package",
        "manifest",
        "spritesheet_missing",
        "image_type",
        "image_size",
        "image_unreadable",
        "slug_taken",
    }
    with pytest.raises(PetPackageError) as exc:
        parse_package([("pet.json", _manifest(displayName="<img src=x onerror=alert(1)>\nx")), ("s.png", _image())])
    assert exc.value.message == pets.ERROR_MESSAGES["manifest"]
    assert "<img" not in exc.value.message


# ── The Butler, the built-in pet ───────────────────────────────────────────


def test_butler_is_a_valid_version_1_package() -> None:
    folder = pets.BUILTIN_DIR / "butler"
    manifest = (folder / "pet.json").read_bytes()
    sheet = (folder / "spritesheet.webp").read_bytes()
    parsed = parse_package([("pet.json", manifest), ("spritesheet.webp", sheet)])
    assert (parsed.manifest.id, parsed.manifest.display_name, parsed.manifest.sprite_version) == ("butler", "Butler", 1)
    assert Image.open(io.BytesIO(sheet)).size == V1
    assert pets.BUILTIN_SLUGS == ("butler",)
    assert pets.DEFAULT_PET == "butler"


def test_the_former_alfred_slug_reads_as_the_butler() -> None:
    assert pets.RENAMED_BUILTIN_PETS == {"alfred": "butler"}
    assert pets.canonical_slug("alfred") == "butler"
    assert pets.canonical_slug("owl") == "owl"
    assert not set(pets.RENAMED_BUILTIN_PETS) & set(pets.BUILTIN_SLUGS)


def test_the_pet_rename_migration_rewrites_only_the_former_slug() -> None:
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    spec = importlib.util.spec_from_file_location(
        "h121healthpet", path / "h121healthpet_rename_builtin_pet_alfred_to_butler.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    renamed = module._RENAMED
    assert renamed == pets.RENAMED_BUILTIN_PETS
    assert module.rewrite_preference({"enabled": True, "pet": "alfred"}, renamed) == {"enabled": True, "pet": "butler"}
    assert module.rewrite_preference({"enabled": True, "pet": "owl"}, renamed) is None
    assert module.rewrite_preference({"enabled": False}, renamed) is None
    assert module.rewrite_preference("junk", renamed) is None
    back = {new: old for old, new in renamed.items()}
    assert module.rewrite_preference({"enabled": True, "pet": "butler"}, back) == {"enabled": True, "pet": "alfred"}


def test_butler_draws_every_frame_of_every_state_and_nothing_else() -> None:
    atlas = Image.open(pets.BUILTIN_DIR / "butler" / "spritesheet.webp").convert("RGBA")
    for _state, row, durations in pets.STATES:
        for column in range(pets.COLUMNS):
            box = (
                column * pets.CELL_WIDTH,
                row * pets.CELL_HEIGHT,
                (column + 1) * pets.CELL_WIDTH,
                (row + 1) * pets.CELL_HEIGHT,
            )
            drawn = atlas.crop(box).getbbox() is not None
            assert drawn == (column < len(durations)), (row, column)


def test_the_committed_butler_is_the_generators_output() -> None:
    spec = importlib.util.spec_from_file_location("generate_butler_pet", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    committed = Image.open(pets.BUILTIN_DIR / "butler" / "spritesheet.webp").convert("RGBA")
    generated = module.build_atlas()
    assert generated.size == committed.size
    assert generated.tobytes() == committed.tobytes()
    assert json.loads((pets.BUILTIN_DIR / "butler" / "pet.json").read_text()) == module.MANIFEST

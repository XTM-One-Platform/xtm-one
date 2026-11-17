"""Regression tests for :class:`app.api.v1.platform.InlineFile` filename validation.

A plain ``str`` for ``InlineFile.filename``, with no
validation, would let a platform caller submit:

* an empty string,
* a path-shaped value (``"../etc/passwd"``, ``"C:\\Windows\\…"``),
* or a multi-MB string,

and the unsanitised value would propagate into
:class:`FileRef.filename` and reach downstream consumers that key off
the filename (``extract_text_from_file`` MIME sniffing, structured
logging, the sandbox's ``entry["filename"]`` surface).

The fix adds Pydantic-level length bounds and a ``field_validator``
that strips directory components and rejects empty / ``"."`` /
``".."`` / NUL-containing values.  These tests pin the contract so
a future refactor of the model can't accidentally weaken it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.platform import InlineFile


class TestInlineFileFilenameValidation:
    def test_basic_filename_is_accepted_unchanged(self):
        """A normal basename round-trips unchanged."""
        f = InlineFile(filename="report.pdf", data="")
        assert f.filename == "report.pdf"

    def test_empty_filename_rejected(self):
        """An empty filename is rejected at the model boundary so
        downstream consumers never see ``""`` as a file identifier.
        """
        with pytest.raises(ValidationError) as excinfo:
            InlineFile(filename="", data="")
        assert "filename" in str(excinfo.value).lower()

    def test_oversized_filename_rejected(self):
        """A filename over the 255-char cap is rejected before it can
        balloon log volume or stress downstream metadata stores.
        """
        with pytest.raises(ValidationError) as excinfo:
            InlineFile(filename="a" * 256, data="")
        assert "filename" in str(excinfo.value).lower()

    def test_filename_at_cap_accepted(self):
        """Exactly 255 chars is the boundary, not the rejection."""
        f = InlineFile(filename="a" * 255, data="")
        assert f.filename == "a" * 255

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("../etc/passwd", "passwd"),
            ("../../etc/passwd", "passwd"),
            ("/etc/passwd", "passwd"),
            ("C:\\Windows\\System32\\notepad.exe", "notepad.exe"),
            ("uploads/report.pdf", "report.pdf"),
            ("a/b/c/d.txt", "d.txt"),
        ],
    )
    def test_path_components_stripped(self, raw, expected):
        """Path-shaped values are stripped to their basename so a
        traversal-like string can never reach ``FileRef.filename``.

        The validator must handle both POSIX (``/``) and Windows
        (``\\``) separators because the platform caller may be a
        Windows agent forwarding native paths.
        """
        f = InlineFile(filename=raw, data="")
        assert f.filename == expected, (
            f"path-shaped {raw!r} must be stripped to {expected!r}; "
            f"observed {f.filename!r}"
        )

    @pytest.mark.parametrize("raw", [".", "..", "./", "../"])
    def test_pure_path_component_rejected(self, raw):
        """A filename that resolves to ``"."`` or ``".."`` (with or
        without a trailing separator) is rejected because it is not a
        meaningful file identifier — accepting it would let a caller
        write ``filename="."`` and confuse downstream code that
        expects a real basename.
        """
        with pytest.raises(ValidationError):
            InlineFile(filename=raw, data="")

    def test_nul_byte_rejected(self):
        """A NUL byte in the filename is rejected — it would otherwise
        truncate filenames in C-string-backed downstream layers
        (log formatters, some FS APIs) and bypass extension-based
        type sniffing.
        """
        with pytest.raises(ValidationError) as excinfo:
            InlineFile(filename="report\x00.pdf", data="")
        assert "nul" in str(excinfo.value).lower() or "filename" in str(excinfo.value).lower()

    def test_content_type_defaults_to_octet_stream(self):
        """The default ``content_type`` is preserved — the filename
        validator must not touch sibling fields.
        """
        f = InlineFile(filename="report.pdf", data="")
        assert f.content_type == "application/octet-stream"

    def test_filename_with_unicode_preserved(self):
        """Unicode filenames (CJK, accented Latin, emoji) survive the
        validator unchanged — the contract is "strip directories and
        reject pathological inputs", not "ASCII-only".
        """
        for unicode_name in (
            "rapport-juillet.pdf",
            "déjà-vu.txt",
            "报告.docx",
            "tactics-emoji.pdf",
        ):
            f = InlineFile(filename=unicode_name, data="")
            assert f.filename == unicode_name

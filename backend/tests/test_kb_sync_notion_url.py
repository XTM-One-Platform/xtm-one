"""Unit tests for ``_extract_notion_id`` — Notion URL / ID normalisation.

The helper handles both Notion page ids and database ids (Notion uses the
same URL / hex shape for both), so the tests pin both shapes through the
same set of input cases.
"""

from app.services.kb_sync import _extract_notion_id

# A well-known fixed UUID used across all test cases.
_UUID_WITH_DASHES = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
_UUID_WITHOUT_DASHES = "a1b2c3d4e5f67890abcdef1234567890"


class TestExtractNotionId:
    # ── Already-valid UUIDs ────────────────────────────────────────────────

    def test_dashed_uuid_returned_lowercase(self):
        """A dashed UUID is returned unchanged (lowercased)."""
        assert _extract_notion_id(_UUID_WITH_DASHES) == _UUID_WITH_DASHES

    def test_dashed_uuid_uppercase_normalised(self):
        assert _extract_notion_id(_UUID_WITH_DASHES.upper()) == _UUID_WITH_DASHES

    def test_dashed_uuid_with_surrounding_whitespace(self):
        assert _extract_notion_id(f"  {_UUID_WITH_DASHES}  ") == _UUID_WITH_DASHES

    def test_bare_hex_uuid_gets_dashes_inserted(self):
        """A 32-char hex string without dashes is reformatted with dashes."""
        assert _extract_notion_id(_UUID_WITHOUT_DASHES) == _UUID_WITH_DASHES

    # ── Notion sharing URLs ───────────────────────────────────────────────

    def test_full_sharing_url_with_workspace_and_query(self):
        """The canonical sharing URL format produced by 'Copy link'."""
        url = (
            f"https://www.notion.so/myworkspace/My-Page-Title-{_UUID_WITHOUT_DASHES}"
            "?source=copy_link"
        )
        assert _extract_notion_id(url) == _UUID_WITH_DASHES

    def test_url_without_query_string(self):
        url = f"https://www.notion.so/myworkspace/My-Page-Title-{_UUID_WITHOUT_DASHES}"
        assert _extract_notion_id(url) == _UUID_WITH_DASHES

    def test_url_without_workspace_prefix(self):
        url = f"https://www.notion.so/{_UUID_WITHOUT_DASHES}"
        assert _extract_notion_id(url) == _UUID_WITH_DASHES

    def test_url_without_www(self):
        url = f"https://notion.so/workspace/Some-Title-{_UUID_WITHOUT_DASHES}"
        assert _extract_notion_id(url) == _UUID_WITH_DASHES

    def test_url_with_fragment(self):
        url = (
            f"https://www.notion.so/workspace/Page-{_UUID_WITHOUT_DASHES}"
            "#some-heading"
        )
        assert _extract_notion_id(url) == _UUID_WITH_DASHES

    def test_url_with_query_and_fragment(self):
        url = (
            f"https://www.notion.so/workspace/Page-{_UUID_WITHOUT_DASHES}"
            "?source=copy_link#heading"
        )
        assert _extract_notion_id(url) == _UUID_WITH_DASHES

    def test_url_with_hyphenated_title_many_words(self):
        url = (
            f"https://www.notion.so/acme-corp/Engineering-Handbook-"
            f"Onboarding-Guide-{_UUID_WITHOUT_DASHES}?source=copy_link"
        )
        assert _extract_notion_id(url) == _UUID_WITH_DASHES

    # ── Edge-cases / unrecognised inputs ─────────────────────────────────

    def test_unrecognised_string_returned_unchanged(self):
        """Non-UUID, non-URL input passes through so the API can error clearly."""
        assert _extract_notion_id("not-a-valid-id") == "not-a-valid-id"

    def test_empty_string_returned_unchanged(self):
        assert _extract_notion_id("") == ""

    def test_too_short_hex_string_returned_unchanged(self):
        assert _extract_notion_id("abcdef1234") == "abcdef1234"

    def test_non_hex_prefix_does_not_block_32_char_match(self):
        """A non-hex prefix character (e.g. 'x') does NOT block the lookbehind,
        so the trailing 32-char hex run is still extracted as the page UUID.

        This is the long-form Notion slug pattern (`Page-Title-<uuid>`) — the
        slug ends with a non-hex separator (a hyphen, here simulated as 'x')
        followed by the 32-char id.
        """
        result = _extract_notion_id("x" + _UUID_WITHOUT_DASHES)
        assert result == _UUID_WITH_DASHES

    def test_pure_64_char_hex_run_does_not_false_match(self):
        """A 64-char run of hex characters (e.g. a SHA-256) must NOT be
        misidentified as a Notion page id — every 32-char window inside it
        is bordered by another hex digit, so the lookbehind/lookahead
        correctly refuse to match. Pinning this so a future relaxation of
        the regex doesn't accidentally swallow a hash as a page id and
        cause confusing Notion 404s."""
        sha256_like = "a" * 64
        assert _extract_notion_id(sha256_like) == sha256_like

    def test_uuid_in_query_string_is_ignored(self):
        """UUIDs that appear only in the query string should not be extracted."""
        other_hex = "b" * 32
        url = (
            f"https://www.notion.so/workspace/plain-page"
            f"?token={other_hex}"
        )
        # No 32-char hex in the path → returns unchanged
        result = _extract_notion_id(url)
        assert result == url

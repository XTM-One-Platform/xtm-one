"""Regression tests for the pre-decode base64 size guard on inline files.

Calling ``base64.b64decode`` BEFORE the size
check would let a malicious caller trigger a large memory allocation
by sending an oversized base64 string that decodes to >>25 MiB.  The
guard translates the decoded-byte caps to base64-string-length caps via
``_max_b64_length_for`` and rejects the request on the cheap length
side.

These tests pin both the helper's arithmetic and the cap-derivation
sanity invariants so a future contributor adjusting the caps (or
swapping in a different base64 variant) cannot inadvertently weaken
the DoS protection.
"""

import base64

from app.api.v1.platform import (
    _MAX_INLINE_FILE_B64_LENGTH,
    _MAX_INLINE_FILE_BYTES,
    _MAX_INLINE_FILES_TOTAL_B64_LENGTH,
    _MAX_INLINE_FILES_TOTAL_BYTES,
    _max_b64_length_for,
)


class TestMaxB64LengthFor:
    def test_small_payload_round_trip(self):
        # 3-byte input encodes to exactly 4 base64 chars (no padding).
        payload = b"abc"
        encoded_len = len(base64.b64encode(payload))
        # Helper returns a small upper bound, not an exact value.
        assert encoded_len <= _max_b64_length_for(len(payload))

    def test_one_byte_payload_with_padding(self):
        # 1-byte input encodes to 4 base64 chars ("Yg==").
        payload = b"a"
        encoded_len = len(base64.b64encode(payload))
        assert encoded_len <= _max_b64_length_for(len(payload))

    def test_real_max_inline_file_payload_passes_cap(self):
        # A payload of exactly the per-file cap (decoded) must produce
        # a base64 string that fits inside the derived b64 cap.  The
        # helper has a small slack (+4) for padding so this is robust
        # across encoding variants (with/without trailing newlines).
        payload_size = _MAX_INLINE_FILE_BYTES
        # We don't actually need to allocate 25 MiB of bytes to test
        # this — the arithmetic is closed-form.
        encoded_len = ((payload_size + 2) // 3) * 4  # standard base64 formula
        assert encoded_len <= _MAX_INLINE_FILE_B64_LENGTH

    def test_payload_significantly_over_cap_exceeds_derived_b64_cap(self):
        # The b64-length formula rounds up to multiples of 4 (4 base64
        # chars per 3 decoded bytes), so a single-byte excess can land
        # in the same b64-length bucket as the cap.  An excess of 3+
        # decoded bytes ALWAYS bumps the b64-length to the next 4-char
        # bucket — that's the threshold at which the cheap pre-decode
        # guard starts rejecting on the cheap side.  Anything smaller
        # than that falls through to the post-decode guard, which is
        # the documented defence-in-depth layer.
        oversized = _MAX_INLINE_FILE_BYTES + 3
        oversized_b64 = _max_b64_length_for(oversized)
        assert oversized_b64 > _MAX_INLINE_FILE_B64_LENGTH

    def test_attacker_payload_orders_of_magnitude_over_cap_rejected(self):
        # The realistic DoS scenario is an attacker sending a base64
        # string that decodes to many MB over the cap.  Verify that
        # the cheap b64 guard catches such payloads without ever
        # decoding.
        attacker_payload_size = _MAX_INLINE_FILE_BYTES * 10  # 250 MiB
        attacker_b64 = _max_b64_length_for(attacker_payload_size)
        assert attacker_b64 > _MAX_INLINE_FILE_B64_LENGTH

    def test_helper_returns_minimum_for_zero_payload(self):
        # ``_max_b64_length_for(0)`` should be tiny (just the slack
        # term) so a zero-byte file is not blocked by the b64 cap.
        assert _max_b64_length_for(0) <= 8


class TestDerivedCaps:
    def test_b64_cap_is_strictly_greater_than_decoded_cap(self):
        # Base64 is roughly 4/3 the size of decoded bytes, so the b64
        # cap must be strictly larger than the byte cap — otherwise
        # the pre-decode guard would falsely reject legitimate
        # payloads at or near the byte cap.
        assert _MAX_INLINE_FILE_B64_LENGTH > _MAX_INLINE_FILE_BYTES
        assert _MAX_INLINE_FILES_TOTAL_B64_LENGTH > _MAX_INLINE_FILES_TOTAL_BYTES

    def test_b64_cap_ratio_is_at_most_4_3_plus_slack(self):
        # Sanity: the b64 cap should not be wildly larger than 4/3 of
        # the byte cap (otherwise we'd be accepting payloads orders
        # of magnitude over budget on the cheap side).  Tolerance
        # accounts for the +4-byte padding slack.
        ratio = _MAX_INLINE_FILE_B64_LENGTH / _MAX_INLINE_FILE_BYTES
        assert ratio < 1.4, (
            f"b64 cap ratio {ratio} is too generous — the pre-decode "
            "guard would allow payloads far over budget on the cheap "
            "side."
        )

    def test_total_b64_cap_consistency(self):
        # Same ratio sanity on the cumulative cap.
        ratio = _MAX_INLINE_FILES_TOTAL_B64_LENGTH / _MAX_INLINE_FILES_TOTAL_BYTES
        assert ratio < 1.4

    def test_decoded_cap_is_25_mib(self):
        # Lock the documented per-file cap so a future change has to
        # consciously update the cap value AND this assertion.
        assert _MAX_INLINE_FILE_BYTES == 25 * 1024 * 1024

    def test_total_cap_is_100_mib(self):
        # Same for the cumulative cap.
        assert _MAX_INLINE_FILES_TOTAL_BYTES == 100 * 1024 * 1024

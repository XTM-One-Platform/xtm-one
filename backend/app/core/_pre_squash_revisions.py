# SPDX-License-Identifier: Apache-2.0

"""Frozen migration-squash compatibility metadata.

The entire Alembic history was collapsed into a single baseline
(``d046bcdf345f``).

The squash assumes the **fully-upgraded contract**: every deployment is on the
last pre-squash release before it installs the squashed version.  That last
release was stamped at exactly one Alembic head — the single revision that was
``head`` immediately before the squash (``PRE_SQUASH_FINAL_HEAD``).  On first
boot after the squash, a database stamped at that revision is schema-identical
to the baseline, so ``app.core.migrations._reconcile_pre_squash_revision`` re-stamps it
to ``BASELINE_REVISION`` — a pure metadata pointer move, zero DDL, zero data
risk.

This is a deliberate **fail-closed** guard: we self-heal ONLY the one revision
we know is schema-equivalent to the baseline.  Any *other* value in
``alembic_version`` (an older/intermediate pre-squash revision, a corrupted
marker, a database from a different product, a hand-edited/foreign marker) is
treated as a genuine error — the app refuses to boot rather than silently
stamping a possibly-incomplete schema as "up to date".

A deployment on an OLDER pre-squash revision than ``PRE_SQUASH_FINAL_HEAD`` must
first upgrade to the last pre-squash release on the old code, then install the
squashed version.  Stamping an intermediate revision to the baseline would
paper over an incomplete schema (``create_all`` cannot replay dropped columns,
type changes, or data backfills), so we refuse it on purpose.  See
``UPGRADING.md`` and ``dev-docs/migration-squash-verification.md``.
"""

from __future__ import annotations

# The squashed baseline revision in the current Alembic script directory.
BASELINE_REVISION = "d046bcdf345f"

# The single Alembic head that existed immediately before the squash — i.e. the
# revision every fully-upgraded deployment is stamped at.  This is the ONLY
# revision that ``_reconcile_pre_squash_revision`` will auto-stamp to the baseline.
#
# NOTE: every time main merges a migration while this squash is in flight, that
# migration is folded INTO the baseline and this constant is re-frozen to
# main's new head (see dev-docs/migration-squash-verification.md §5).  History
# of the folds so far:
#
# - ``p1m2c3s4v5r6`` → ``q1t2r3a4c5e6``: four migrations added by main after
#   the original freeze were folded into the baseline.
# - ``q1t2r3a4c5e6`` → ``c0nvcmpst001``: ``c0nvcmpst001``
#   (``conversations.compaction_state_json``) merged to main and was folded
#   into the baseline.
# - ``c0nvcmpst001`` → ``h1t2l3h4s5t6``: ``h1t2l3h4s5t6``
#   (``assignment_runs.human_input_history``) merged to main and was folded
#   into the baseline.
# - ``h1t2l3h4s5t6`` → ``ci1m2p3o4r5t``: ``ci1m2p3o4r5t``
#   (``custom_integration_definitions`` table for importable provider
#   bundles) was folded into the baseline.  NOTE: ``ci1m2p3o4r5t`` was main's
#   head at that point and every fully-upgraded deployment is stamped at it.
# - ``ci1m2p3o4r5t`` → ``n2s3g4l5e6t7``: ``n2s3g4l5e6t7``
#   (``platform_settings`` singleton unique index) was folded into
#   the baseline (index only — the standalone migration's duplicate-row
#   healing is unnecessary on a fresh baseline table).  NOTE: ``n2s3g4l5e6t7`` was main's
#   head at that point and every fully-upgraded deployment is stamped at it.
# - ``n2s3g4l5e6t7`` → ``zk9o0p1q2r3s4``: ``zk9o0p1q2r3s4``
#   (``agent_skills.refresh_failure_count`` for skill auto-refresh
#   quarantine) merged to main and was folded into the
#   baseline.
# - ``zk9o0p1q2r3s4`` → ``zn3s4t5u6v7w``: five migrations merged to main
#   after the freeze were folded into the baseline in one batch —
#   ``zl0p1q2r3s4t`` (``evaluation_result_traces`` link table),
#   ``zl0p1q2r3s4t5`` (``kb_sync_sources.sync_partial_fail_count``),
#   ``zm1q2r3s4t5u`` (merge, no schema), ``qt4g3n7a1d01`` (incident
#   2026-07-14, ``quota_usage.agent_id`` + FK/index + NULLS-NOT-DISTINCT
#   unique constraint), and ``zn3s4t5u6v7w`` (one-time Notion
#   quarantine purge, data-only — no schema to fold). ``zn3s4t5u6v7w`` is
#   main's new head.
#
# Superseded heads (``p1m2c3s4v5r6``, ``q1t2r3a4c5e6``, ``c0nvcmpst001``,
# ``h1t2l3h4s5t6``, ``ci1m2p3o4r5t``, ``n2s3g4l5e6t7``, ``zk9o0p1q2r3s4``)
# are intentionally NOT healed: a database still at one of them is missing
# the folded columns/tables/constraints, and the heal is a pure stamp (no
# DDL runs on existing installs), so it fails closed.
PRE_SQUASH_FINAL_HEAD = "zn3s4t5u6v7w"

# Every revision id of the Alembic history as it stood immediately before the
# squash (the tree of the squash commit's parent, ``PRE_SQUASH_FINAL_HEAD``
# included).  Frozen: no revision is ever added here.  It lets the startup guard
# tell a genuine old pre-squash database (fail closed, upgrade through the last
# pre-squash release) from a database that is AHEAD of the running image (a
# rollback to an older image: fail closed, never re-stamp).
PRE_SQUASH_REVISIONS: frozenset[str] = frozenset(
    {
        "01c03a51b25c",
        "11ebea2b0f83",
        "31057ba36a7b",
        "35f093137324",
        "3815952fa962",
        "3a9f7c2e8b4d",
        "4b2742ba258a",
        "4b8e6d1f9c3a",
        "5536a5a676e7",
        "57b058bd1e68",
        "5c7d4e2f8b1a",
        "5ha9d57a7e18",
        "6d5c3a9f7e2b",
        "7551cabef0a5",
        "7e4f2a8b3c1d",
        "8c1e2f9a4b7d",
        "90f532aacd1d",
        "98f05660cbf0",
        "999e036a3f62",
        "9d2f3e8b5c6a",
        "9db007ae2dc5",
        "9f1e7c2a4b6d",
        "a0b1c2d3e4f5",
        "a1b2c3d4e5f6",
        "a1b2c3d4e5f7",
        "a1g2r3e4f5c6",
        "a2b3c4d5e6f7",
        "a3c7f2e91d04",
        "a4b5c6d7e8f9",
        "a4c7b9d3e1f2",
        "a7b8c9d0e1f2",
        "a7c4e2f10b93",
        "a7l8m9n0p1q2",
        "a8b9c0d1e2f3",
        "af52b7dd8dde",
        "b1c2d3e4f5g6",
        "b1r2a3n4d5g6",
        "b2c3d4e5f6a7",
        "b3c4d5e6f7g8",
        "b3e9c1a7d2f4",
        "b3f1a2c4d5e6",
        "b3u9s4a8g7e2",
        "b4c5d6e7f8g9",
        "b5d1e3f04a12",
        "b5d8e1f4a2c3",
        "b721cec75c7a",
        "b8d5f3e1a9c2",
        "b8m3e4r5e6m7",
        "c0a1p2v3e4r5",
        "c0nvcmpst001",
        "c1627gachatm0",
        "c1d2e3f4g5h6",
        "c1ea4p1nam30",
        "c3d4e5f6a7b8",
        "c3f8a1b9d4e2",
        "c4d5e6f7g8h9",
        "c4o0r1i2g3a4",
        "c5d6e7f8g9h0",
        "c6ff9bf5c91a",
        "c7em01userv1",
        "c8d9e0f1g2h3",
        "c8e4a1b39f56",
        "c9a1d4e7b2f3",
        "c9d3e7f1a5b2",
        "ci1m2p3o4r5t",
        "d2e3f4g5h6i7",
        "d4e5f6a7b8c9",
        "d5e6f7g8h9i0",
        "d6e7f8g9h0i1",
        "d7e1a4b8c9f3",
        "d8ctem9stg01",
        "d9e0f1g2h3i4",
        "d9f2a7c84e31",
        "dlystatsnnd01",
        "e0f1g2h3i4j5",
        "e1a2b3c4d5f6",
        "e2f3g4h5i6j7",
        "e37dd98fbed4",
        "e4f5a8b92c17",
        "e5f6a7b8c9d0",
        "e6f7g8h9i0j1",
        "e7f8g9h0i1j2",
        "e9b1d2c3f4a5",
        "e9ctemsnap01",
        "f0ctembrfg01",
        "f1263d68e314",
        "f1e2a3b4c5d6",
        "f1g2h3i4j5k6",
        "f2g3h4i5j6k7",
        "f3g4h5i6j7k8",
        "f4b1c2d3e5a6",
        "f6a7b8c9d0e1",
        "f7a3b5d12e89",
        "f7g8h9i0j1k2",
        "fb72fd90e71b",
        "g2h3i4j5k6l7",
        "g4h5i6j7k8l9",
        "g7b8c9d0e1f2",
        "g8h9i0j1k2l3",
        "h1d2r3u4n5s6",
        "h1i2t3l4a5g6",
        "h1t2l3h4s5t6",
        "h3i4j5k6l7m8",
        "h5i6j7k8l9m0",
        "h8c9d0e1f2g3",
        "i2n3t4g5r6s7",
        "i6j7k8l9m0n1",
        "i8dx9c0t1h2r",
        "i9d0e1f2g3h4",
        "j0e1f2g3h4i5",
        "j1w2t3f4w5d6",
        "j7k8l9m0n1o2",
        "k1b2p3e4r5m6",
        "k1b2u3i4l5t6",
        "k1f2g3h4i5j6",
        "k2l3m4n5o6p7",
        "k9l0m1n2o3p4",
        "kb1e2x3p4a5",
        "l2g3h4i5j6k7",
        "l3m4n5o6p7q8",
        "llm0bsbr1dge",
        "m1a2x3o4u5t6",
        "m1g8r9a0t1e2",
        "m1n2o3p4q5r6",
        "m1u2l3t4i5b6",
        "m3h4i5j6k7l8",
        "m3m0ry_us3r01",
        "m3n4o5p6q7r8",
        "m4n5o6p7q8r9",
        "m9f0e1d2b3c4",
        "n0rmsh4r3gid",
        "n2s3g4l5e6t7",
        "n4i5j6k7l8m9",
        "n5o6p7q8r9s0",
        "o1w2n3e4r5s6",
        "o5p6q7r8s9t0",
        "o6p7q8r9s0t1",
        "o9r8c7h6e5s4",
        "p1m2c3s4v5r6",
        "p1q2r3s4t5u6",
        "p1r2o3v4e5r6",
        "p1u2r3g4i5d6",
        "p2u3r4g5e6l7",
        "p6q7r8s9t0u1",
        "p9v8e7r6s5n4",
        "q0l1i2n3k4m5",
        "q1t2r3a4c5e6",
        "q2r3s4t5u6v7",
        "q7r8s9t0u1v2",
        "qt4g3n7a1d01",
        "r1e2a3s4o5n6",
        "r3h4e5a6l7k8",
        "r3s4t5u6v7w8",
        "r7s8t9u0v1w2",
        "r8s9u0m1e2v3",
        "r9t0u1v2w3x4",
        "s1a2m3l4g5r6",
        "s1c2o3p4e5a6",
        "s1d2m3o4d5e6",
        "s1e2l3f4e5c6",
        "s1k2v3e4r5s6",
        "s4t5u6v7w8x9",
        "s8t9u0v1w2x3",
        "s9k8i7l6p5m4",
        "sc0p3m0v3t01",
        "t1h2e3m4e5u6",
        "t1m2e3o4u5t6",
        "t1o2o3l4t5i6",
        "t1u2v3w4x5y6",
        "t5u6v7w8x9y0",
        "t9u0v1w2x3y4",
        "u0v1w2x3y4z5",
        "u1r2l3t4e5x6",
        "u1v2w3x4y5z6",
        "u2v3w4x5y6z7",
        "u6v7w8x9y0z1",
        "v1w2x3y4z5a6",
        "v2w3x4y5z6a7",
        "v4c3f2g1h0i9",
        "v7w8x9y0z1a2",
        "w0rkc4se1713",
        "w1d2e3n4f5g6",
        "w1d3n_sso_prov",
        "w1i2t3m4o5w6",
        "w2x3y4z5a6b7",
        "w4x5y6z7a8b9",
        "x1t2m3o4n5e6",
        "x3y4z5a6b7c8",
        "y2p3l4a5t6s7",
        "y4z5a6b7c8d9",
        "z5a6b7c8d9e0",
        "z6a7b8c9d0e1",
        "z7b8c9d0e1f2",
        "z7y6x5w4v3u2",
        "z8c9d0e1f2g3",
        "z8d9e0f1g2h3",
        "z9d0e1f2g3h4",
        "z9e0f1g2h3i4",
        "za0e1f2g3h4i5",
        "za1f2g3h4i5j6",
        "zb1f2g3h4i5j6",
        "zb2g3h4i5j6k7",
        "zc3h4i5j6k7l8",
        "zd4i5j6k7l8m9",
        "ze5j6k7l8m9n0",
        "zf4i5j6k7l8m9",
        "zg5j6k7l8m9n0",
        "zg5k6l7m8n9o0",
        "zh6l7m8n9o0p1",
        "zi7m8n9o0p1q2",
        "zj8n9o0p1q2r3",
        "zk9o0p1q2r3s4",
        "zl0p1q2r3s4t",
        "zl0p1q2r3s4t5",
        "zm1q2r3s4t5u",
        "zn3s4t5u6v7w",
    }
)

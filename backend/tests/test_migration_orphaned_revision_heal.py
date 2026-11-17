"""Startup migration runner: auto-heal a DB stamped at the last pre-squash head.

After the Alembic history squash collapsed all migrations into a
single baseline, an *existing* database is still stamped at the last pre-squash
revision that no longer exists in the script directory.  A plain
``alembic upgrade head`` fails hard with "Can't locate revision", and the app
refuses to boot — which would force every existing operator (including
**air-gapped on-prem installs**) to run a manual ``alembic stamp --purge``.

The squash assumes the **fully-upgraded contract**: every deployment is on the
last pre-squash release (stamped at the single ``PRE_SQUASH_FINAL_HEAD``) before
it installs the squashed version.  ``_reconcile_pre_squash_revision`` must therefore be
**fail-closed**:
- leave a KNOWN revision untouched (normal pending-migration path),
- re-stamp a DB parked at ``PRE_SQUASH_FINAL_HEAD`` to ``BASELINE_REVISION``
  with ``purge=True`` (metadata-only, no DDL) so startup proceeds
  automatically.  The target is the BASELINE, never ``head``: once post-squash
  migrations exist, ``head`` is baseline+N, and stamping it would silently
  mark those N migrations as applied without running their bodies.  Stamping
  the baseline leaves them pending for the ``upgrade head`` that follows, so a
  deployment can jump from the pre-squash head straight to squash+N,
- REFUSE (raise) for any other unrecognised revision — an older/intermediate
  pre-squash revision, a post-squash revision newer than the image, a corrupted
  marker, or a foreign database — rather than blindly stamping a schema as up
  to date, with a message naming which case it is (only a pre-squash revision
  is told about the manual re-stamp).

No database required — ``command.stamp`` and the revision lookup are
monkeypatched.
"""

from unittest.mock import patch

import pytest

from alembic import command as alembic_command
from app.core import migrations as mig
from app.core._pre_squash_revisions import (
    BASELINE_REVISION,
    PRE_SQUASH_FINAL_HEAD,
    PRE_SQUASH_REVISIONS,
)

IMAGE_HEAD = "imagehead001"


@pytest.mark.asyncio
async def test_known_revision_is_left_untouched():
    """A genuine pending revision must NOT be re-stamped."""
    stamp_calls: list = []

    def _stamp(config, revision, purge=False):
        stamp_calls.append((revision, purge))

    with (
        patch.object(mig, "_known_revisions", return_value={BASELINE_REVISION, "abc123"}),
        patch.object(mig.command, "stamp", side_effect=_stamp),
    ):
        await mig._reconcile_pre_squash_revision("abc123")

    assert stamp_calls == []  # nothing healed — normal upgrade path handles it


@pytest.mark.asyncio
async def test_pre_squash_final_head_is_purge_stamped_to_baseline():
    """A DB stamped at the last pre-squash head is auto-healed to the baseline."""
    stamp_calls: list = []

    def _stamp(config, revision, purge=False):
        stamp_calls.append((revision, purge))

    with (
        patch.object(mig, "_known_revisions", return_value={BASELINE_REVISION}),
        patch.object(mig.command, "stamp", side_effect=_stamp),
    ):
        await mig._reconcile_pre_squash_revision(PRE_SQUASH_FINAL_HEAD)

    # Re-stamped to the BASELINE (with purge) — never to "head".
    assert stamp_calls == [(BASELINE_REVISION, True)]


@pytest.mark.asyncio
async def test_heal_targets_baseline_even_with_post_squash_migrations():
    """With post-squash migrations in the tree, the heal must NOT stamp head.

    The DB at ``PRE_SQUASH_FINAL_HEAD`` is schema-equivalent to the BASELINE
    only.  Stamping ``head`` (= baseline+N) would mark the N post-squash
    migrations as applied without running their bodies — silently skipping
    their DDL and data backfills.  Stamping the baseline leaves them pending,
    and the ``upgrade head`` that follows the reconcile applies them normally.
    """
    stamp_calls: list = []

    def _stamp(config, revision, purge=False):
        stamp_calls.append((revision, purge))

    post_squash_history = {BASELINE_REVISION, "post1aaa", "post2bbb", "post3ccc"}
    with (
        patch.object(mig, "_known_revisions", return_value=post_squash_history),
        patch.object(mig.command, "stamp", side_effect=_stamp),
    ):
        await mig._reconcile_pre_squash_revision(PRE_SQUASH_FINAL_HEAD)

    assert stamp_calls == [(BASELINE_REVISION, True)]
    assert stamp_calls[0][0] != "head"


@pytest.mark.asyncio
async def test_post_squash_revision_unknown_to_the_image_says_the_database_is_ahead() -> None:
    """A database migrated by a newer build, booted by an older image (a rollback).

    It must refuse to boot, say the database is ahead of the image with the
    revision and the image's head, point at a newer image, and never advise
    the re-stamp: stamping the baseline would hide the newer schema and replay
    every post-squash migration on the next upgrade.
    """
    stamp_calls: list[tuple[str, bool]] = []

    def _stamp(config: object, revision: str, purge: bool = False) -> None:
        stamp_calls.append((revision, purge))

    newer = "l12077ledger1"
    assert newer not in PRE_SQUASH_REVISIONS

    with (
        patch.object(mig, "_known_revisions", return_value={BASELINE_REVISION, IMAGE_HEAD}),
        patch.object(mig, "_head_revisions", return_value=[IMAGE_HEAD]),
        patch.object(alembic_command, "stamp", side_effect=_stamp),
        pytest.raises(RuntimeError) as raised,
    ):
        await mig._reconcile_pre_squash_revision(newer)

    message = str(raised.value)
    assert "database is ahead of this image" in message
    assert f"'{newer}'" in message
    assert f"head {IMAGE_HEAD}" in message
    assert "Deploy an image whose migration history contains" in message
    assert "do NOT re-stamp" in message
    assert "stamp --purge" not in message
    assert "pre-squash" not in message
    assert stamp_calls == []


@pytest.mark.asyncio
async def test_foreign_value_asks_to_check_the_database_without_restamp_advice() -> None:
    """A value no Alembic id looks like is a foreign or corrupted marker."""
    stamp_calls: list[tuple[str, bool]] = []

    def _stamp(config: object, revision: str, purge: bool = False) -> None:
        stamp_calls.append((revision, purge))

    foreign = "Not-A-Revision!"

    with (
        patch.object(mig, "_known_revisions", return_value={BASELINE_REVISION}),
        patch.object(mig, "_head_revisions", return_value=[IMAGE_HEAD]),
        patch.object(alembic_command, "stamp", side_effect=_stamp),
        pytest.raises(RuntimeError) as raised,
    ):
        await mig._reconcile_pre_squash_revision(foreign)

    message = str(raised.value)
    assert "unrecognised value" in message
    assert "Verify you are pointing at the correct XTM One database" in message
    assert "stamp --purge" not in message
    assert "ahead of this image" not in message
    assert stamp_calls == []


@pytest.mark.asyncio
async def test_intermediate_pre_squash_revision_fails_closed():
    """An older/intermediate pre-squash revision must NOT be auto-stamped.

    Only the single fully-upgraded head is schema-equivalent to the baseline;
    an intermediate revision has an incomplete schema that ``create_all`` cannot
    reconstruct, so healing it would silently mark a wrong schema as current.
    """
    stamp_calls: list = []

    def _stamp(config, revision, purge=False):
        stamp_calls.append((revision, purge))

    # A real pre-squash revision that is NOT the final head.
    intermediate = "b721cec75c7a"
    assert intermediate != PRE_SQUASH_FINAL_HEAD

    assert intermediate in PRE_SQUASH_REVISIONS

    with (
        patch.object(mig, "_known_revisions", return_value={BASELINE_REVISION}),
        patch.object(mig, "_head_revisions", return_value=[IMAGE_HEAD]),
        patch.object(mig.command, "stamp", side_effect=_stamp),
        pytest.raises(RuntimeError) as raised,
    ):
        await mig._reconcile_pre_squash_revision(intermediate)

    message = str(raised.value)
    assert f"pre-squash revision '{intermediate}'" in message
    assert "last pre-squash release" in message
    assert f"stamp --purge {BASELINE_REVISION}" in message
    assert "ahead of this image" not in message
    assert stamp_calls == []


@pytest.mark.parametrize(
    "prior_head",
    [
        "p1m2c3s4v5r6",
        "q1t2r3a4c5e6",
        "c0nvcmpst001",
        "h1t2l3h4s5t6",
        "ci1m2p3o4r5t",
        "n2s3g4l5e6t7",
        "zk9o0p1q2r3s4",
    ],
)
async def test_prior_pre_squash_head_fails_closed(prior_head):
    """Heads that preceded a rebase fold must NOT auto-heal.

    Each time main merged a migration while the squash was in flight, that
    migration was folded into the baseline and ``PRE_SQUASH_FINAL_HEAD`` was
    re-frozen to main's new head (``p1m2c3s4v5r6`` → ``q1t2r3a4c5e6`` →
    ``c0nvcmpst001`` → ``h1t2l3h4s5t6`` → ``ci1m2p3o4r5t`` →
    ``n2s3g4l5e6t7`` → ``zk9o0p1q2r3s4`` → ``zn3s4t5u6v7w``).  The baseline
    now INCLUDES the columns/tables those folded migrations added, so a DB
    still stamped at a superseded head is MISSING them, and the heal is a
    pure stamp (no DDL runs on existing installs).  Healing it would falsely
    mark an incomplete schema as current — so it must fail closed exactly
    like any other pre-``PRE_SQUASH_FINAL_HEAD`` revision.
    """
    stamp_calls: list = []

    def _stamp(config, revision, purge=False):
        stamp_calls.append((revision, purge))

    assert prior_head != PRE_SQUASH_FINAL_HEAD

    with (
        patch.object(mig, "_known_revisions", return_value={BASELINE_REVISION}),
        patch.object(mig, "_head_revisions", return_value=[IMAGE_HEAD]),
        patch.object(mig.command, "stamp", side_effect=_stamp),
        pytest.raises(RuntimeError, match="pre-squash revision"),
    ):
        await mig._reconcile_pre_squash_revision(prior_head)

    assert stamp_calls == []


def test_compatibility_metadata_is_well_formed():
    """Guard the two constants the heal path depends on."""
    assert isinstance(BASELINE_REVISION, str) and BASELINE_REVISION
    assert isinstance(PRE_SQUASH_FINAL_HEAD, str) and PRE_SQUASH_FINAL_HEAD
    # The baseline and the pre-squash head must be distinct revisions.
    assert BASELINE_REVISION != PRE_SQUASH_FINAL_HEAD
    # The frozen pre-squash history holds its final head, never the baseline.
    assert PRE_SQUASH_FINAL_HEAD in PRE_SQUASH_REVISIONS
    assert BASELINE_REVISION not in PRE_SQUASH_REVISIONS


def test_no_current_revision_is_a_pre_squash_revision() -> None:
    """A post-squash id reused from the pre-squash history would be misdiagnosed."""
    assert not (mig._known_revisions() & PRE_SQUASH_REVISIONS)

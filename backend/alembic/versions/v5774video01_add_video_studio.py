"""add the Video Studio tables

Five new tables, nothing changed on an existing one:

* ``browser_profiles`` - a signed-in browser identity for a demo environment;
  three-tier visibility; Fernet-encrypted credentials and saved session.
* ``video_takes``      - one scripted recording of the backend browser.
* ``video_clips``      - one AI-generated clip.
* ``video_projects``   - the edit (timeline spec); three-tier visibility.
* ``video_renders``    - one production of a project.

Purely additive: older replicas during a rolling deploy never read them.
Every table carries a foreign key to ``users``, which installs its triggers
there under SHARE ROW EXCLUSIVE: each ``CREATE TABLE`` clears the lock
holders of ``users`` first and runs under the ``lock_timeout`` belt, so a busy
``users`` fails fast (55P03) and the startup runner retries. File references
are plain ids (no key on the write-hot ``file_attachments``). Indexes are
built in place on the new, empty tables.

Revision ID: v5774video01
Revises: u5545ucred02
Create Date: 2026-10-14 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5774video01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "u5545ucred02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES_IN_DROP_ORDER: tuple[str, ...] = (
    "video_renders",
    "video_projects",
    "video_clips",
    "video_takes",
    "browser_profiles",
)


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _uuid(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), nullable=nullable)


def _timestamp(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _user_fk(name: str = "user_id") -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


def _visibility_columns() -> list[sa.Column]:
    return [
        sa.Column("is_company_managed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("shared_group_ids", postgresql.JSON(), nullable=True),
        sa.Column("shared_group_permissions", postgresql.JSON(), nullable=True),
        sa.Column("shared_user_ids", postgresql.JSON(), nullable=True),
        sa.Column("shared_user_permissions", postgresql.JSON(), nullable=True),
        sa.Column("access_restricted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    ]


def _job_columns() -> list[sa.Column]:
    return [
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("progress", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        _uuid("execution_id"),
    ]


def _guard(conn) -> None:
    """Clear the lock holders of ``users`` and set the belt before a keyed CREATE TABLE."""
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)


def _create_browser_profiles(conn) -> None:
    if _has_table(conn, "browser_profiles"):
        return
    _guard(conn)
    op.create_table(
        "browser_profiles",
        _uuid("id", nullable=False),
        _user_fk(),
        *_visibility_columns(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("start_url", sa.Text(), nullable=False),
        sa.Column("login_url", sa.Text(), nullable=True),
        sa.Column("allowed_hosts", postgresql.JSON(), nullable=True),
        sa.Column("login_config", postgresql.JSON(), nullable=True),
        sa.Column("browser_options", postgresql.JSON(), nullable=True),
        sa.Column("credentials_enc", sa.Text(), nullable=True),
        sa.Column("storage_state_enc", sa.Text(), nullable=True),
        sa.Column("session_status", sa.String(32), nullable=False, server_default=sa.text("'none'")),
        _timestamp("session_saved_at"),
        _timestamp("session_checked_at"),
        sa.Column("last_sign_in_message", sa.Text(), nullable=True),
        sa.Column("mfa_challenge_id", sa.String(64), nullable=True),
        sa.Column("mfa_challenge_kind", sa.String(20), nullable=True),
        _timestamp("mfa_expires_at"),
        _uuid("mfa_screenshot_file_id"),
        sa.Column("keepalive_minutes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index("ix_browser_profiles_user_id", "browser_profiles", ["user_id"], if_not_exists=True)
    op.create_index(
        "ix_browser_profiles_is_company_managed", "browser_profiles", ["is_company_managed"], if_not_exists=True
    )
    op.create_index("ix_browser_profiles_created_at", "browser_profiles", ["created_at"], if_not_exists=True)


def _create_video_takes(conn) -> None:
    if _has_table(conn, "video_takes"):
        return
    _guard(conn)
    op.create_table(
        "video_takes",
        _uuid("id", nullable=False),
        _user_fk(),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("browser_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _uuid("conversation_id"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("script", postgresql.JSON(), nullable=False),
        sa.Column("events", postgresql.JSON(), nullable=True),
        sa.Column("cursor", postgresql.JSON(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("viewport_width", sa.Integer(), nullable=True),
        sa.Column("viewport_height", sa.Integer(), nullable=True),
        sa.Column("pixel_ratio", sa.Float(), nullable=True),
        sa.Column("fps", sa.Integer(), nullable=True),
        sa.Column("captured_frames", sa.Integer(), nullable=True),
        _uuid("file_id"),
        _uuid("poster_file_id"),
        _uuid("contact_sheet_file_id"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("stage", sa.String(200), nullable=True),
        *_job_columns(),
        _timestamp("started_at"),
        _timestamp("finished_at"),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index("ix_video_takes_user_id", "video_takes", ["user_id"], if_not_exists=True)
    op.create_index("ix_video_takes_profile_id", "video_takes", ["profile_id"], if_not_exists=True)
    op.create_index("ix_video_takes_user_created", "video_takes", ["user_id", "created_at"], if_not_exists=True)


def _create_video_clips(conn) -> None:
    if _has_table(conn, "video_clips"):
        return
    _guard(conn)
    op.create_table(
        "video_clips",
        _uuid("id", nullable=False),
        _user_fk(),
        _uuid("conversation_id"),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSON(), nullable=False),
        sa.Column("provider_job_id", sa.String(255), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("has_audio", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        _uuid("file_id"),
        _uuid("poster_file_id"),
        *_job_columns(),
        _timestamp("finished_at"),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index("ix_video_clips_user_id", "video_clips", ["user_id"], if_not_exists=True)
    op.create_index("ix_video_clips_user_created", "video_clips", ["user_id", "created_at"], if_not_exists=True)


def _create_video_projects(conn) -> None:
    if _has_table(conn, "video_projects"):
        return
    # Keyed to template_containers too (the video template it wears).
    terminate_lock_holders(conn, "template_containers")
    _guard(conn)
    op.create_table(
        "video_projects",
        _uuid("id", nullable=False),
        _user_fk(),
        *_visibility_columns(),
        _uuid("conversation_id"),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("spec", postgresql.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("scene_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("estimated_duration_ms", sa.Integer(), nullable=True),
        _uuid("latest_render_id"),
        sa.Column(
            "template_container_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("template_containers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _user_fk("updated_by_user_id"),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index("ix_video_projects_user_id", "video_projects", ["user_id"], if_not_exists=True)
    op.create_index(
        "ix_video_projects_template_container_id", "video_projects", ["template_container_id"], if_not_exists=True
    )
    op.create_index(
        "ix_video_projects_is_company_managed", "video_projects", ["is_company_managed"], if_not_exists=True
    )
    op.create_index("ix_video_projects_created_at", "video_projects", ["created_at"], if_not_exists=True)


def _create_video_renders(conn) -> None:
    if _has_table(conn, "video_renders"):
        return
    _guard(conn)
    op.create_table(
        "video_renders",
        _uuid("id", nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("video_projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _user_fk(),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("quality", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(200), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("fps", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        _uuid("file_id"),
        _uuid("poster_file_id"),
        _uuid("captions_file_id"),
        sa.Column("notes", postgresql.JSON(), nullable=True),
        *_job_columns(),
        _timestamp("started_at"),
        _timestamp("finished_at"),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index("ix_video_renders_user_id", "video_renders", ["user_id"], if_not_exists=True)
    op.create_index(
        "ix_video_renders_project_created", "video_renders", ["project_id", "created_at"], if_not_exists=True
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "users"):
        return
    _create_browser_profiles(conn)
    _create_video_takes(conn)
    _create_video_clips(conn)
    _create_video_projects(conn)
    _create_video_renders(conn)


def downgrade() -> None:
    conn = op.get_bind()
    for name in _TABLES_IN_DROP_ORDER:
        if _has_table(conn, name):
            # Dropping a table removes its FK triggers from `users` under an
            # ACCESS EXCLUSIVE lock there: the belt makes a busy `users` fail
            # fast instead of queueing behind it.
            set_lock_timeout(conn)
            op.drop_table(name, if_exists=True)

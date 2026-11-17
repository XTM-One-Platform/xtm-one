# SPDX-License-Identifier: Apache-2.0

"""The three-tier visibility column set of a shareable entity, as a mixin."""

from __future__ import annotations

from sqlalchemy import Boolean
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column


class VisibilityColumns:
    """The three-tier visibility column set every shareable entity carries, in full.

    ``user_visible_filter`` reads ``shared_user_ids`` and ``access_restricted``
    unconditionally on its non-admin branch, so a partial set is a 500 for the
    first ordinary user (``tests/test_visibility_helpers.py``).
    """

    is_company_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", default=False)
    shared_group_ids: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    shared_group_permissions: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    shared_user_ids: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    shared_user_permissions: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    access_restricted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", default=False)

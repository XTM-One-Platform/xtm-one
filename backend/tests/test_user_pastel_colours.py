# SPDX-License-Identifier: Apache-2.0

"""The "Pastel colours" profile preference (#1305).

Standard colours are the default of every product: ``/auth/me`` reads
``pastel_colours`` false for an account that never opted in (the column's
server default), true once ``PUT /settings/preferences`` saved the opt-in, and
a preferences save that does not name the field leaves it as it is.
"""

from app.api.v1.schemas.settings import UserPreferencesRequest
from app.models.user import User


def test_the_column_defaults_to_standard_colours() -> None:
    column = User.__table__.c.pastel_colours
    assert column.nullable is False
    assert column.server_default is not None
    assert str(column.server_default.arg) == "false"


def test_a_preferences_save_without_the_field_leaves_it_alone() -> None:
    assert UserPreferencesRequest(theme="light").pastel_colours is None


def test_a_preferences_save_carries_the_opt_in() -> None:
    assert UserPreferencesRequest(pastel_colours=True).pastel_colours is True
    assert UserPreferencesRequest(pastel_colours=False).pastel_colours is False


def test_the_me_response_reads_false_unless_opted_in() -> None:
    from app.api.v1.auth import UserResponse

    base = {"id": "u", "email": "u@example.com", "full_name": "U", "role": "user"}
    assert UserResponse.model_validate(base).pastel_colours is False
    assert UserResponse.model_validate({**base, "pastel_colours": True}).pastel_colours is True

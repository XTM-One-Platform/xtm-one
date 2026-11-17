# SPDX-License-Identifier: Apache-2.0

"""Request fields refused past the column they are written to.

A create or update route writes most text fields as sent, and PostgreSQL
refusing a value longer than its column made the write a 500 after the request
had validated. A schema declares the columns its fields land in, and a value
that does not fit is refused before the route runs, naming the field and the
limit (``text_column_error``, the rule the agent import applies). The limit is
read off the model, so no schema holds a copy of it.

A lone UTF-16 surrogate fits no column as sent: a text column refuses it at
the write, and the engine stores one in a JSON column as U+FFFD, not what was
sent (``core/lone_surrogates.py``). ``lone_surrogate_validator`` refuses one in
every field written to a text or JSON column of the model.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationInfo, field_validator
from sqlalchemy import JSON, String
from sqlalchemy.types import TypeDecorator

from app.core.lone_surrogates import lone_surrogate_error
from app.core.utils import text_column_error


def column_length_validator(model: Any, *fields: str, stripped: bool = False) -> Any:
    """A field validator refusing a string its column of *model* cannot hold.

    Each field is written to the column of its own name. *stripped* is for a
    route that strips the value before writing it: the stripped value is what
    must fit, so padding around a name that fits keeps working. A value that
    is not a string is left to the field's own type.
    """
    columns = {field: model.__table__.c[field] for field in fields}

    def _fit_columns(cls: type, value: object, info: ValidationInfo) -> object:
        written = value.strip() if stripped and isinstance(value, str) else value
        if isinstance(written, str):
            error = text_column_error(columns[info.field_name], written, field=info.field_name)
            if error is not None:
                raise ValueError(error)
        return value

    return field_validator(*fields)(_fit_columns)


def text_or_json_columns(model: Any) -> tuple[str, ...]:
    """The columns of *model* a string reaches as sent: text (``String``, ``Text``) and JSON.

    By column key, which is the attribute name - and the request field name -
    of every model a schema here checks.
    """
    names = []
    for column in model.__table__.columns:
        column_type = column.type.impl_instance if isinstance(column.type, TypeDecorator) else column.type
        if isinstance(column_type, String | JSON):
            names.append(column.key)
    return tuple(names)


def lone_surrogate_validator(model: Any) -> Any:
    """A field validator refusing a lone surrogate in every field written to a text or JSON column of *model*.

    A field is written to the column of its own name; a column the schema has
    no field for is ignored. Runs before the field's own validation, on the
    value as sent, and names where the surrogate is (``trigger_config.path``).
    """

    def _refuse_lone_surrogates(cls: type, value: object, info: ValidationInfo) -> object:
        error = lone_surrogate_error(info.field_name, value)
        if error is not None:
            raise ValueError(error)
        return value

    return field_validator(*text_or_json_columns(model), mode="before", check_fields=False)(_refuse_lone_surrogates)

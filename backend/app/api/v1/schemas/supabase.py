# SPDX-License-Identifier: Apache-2.0

"""Schemas of the Supabase database access probe (``/integrations/{id}/supabase``)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SupabaseProbeRequest(BaseModel):
    """The settings to probe with; a field left out is read from the stored config.

    ``db_password`` left out, or sent back as the response mask, is the stored
    password: the probe of a saved role needs no re-typing.
    """

    model_config = ConfigDict(extra="forbid")

    project_ref: str | None = Field(default=None, max_length=200)
    db_host: str | None = Field(default=None, max_length=255)
    db_port: str | None = Field(default=None, max_length=5)
    db_name: str | None = Field(default=None, max_length=63)
    db_role: str | None = Field(default=None, max_length=63)
    db_password: str | None = Field(default=None, max_length=1024)
    db_ca_cert: str | None = Field(default=None, max_length=20_000)


class SupabaseProbeProject(BaseModel):
    ref: str
    name: str
    region: str
    status: str


class SupabaseProbeRole(BaseModel):
    name: str
    #: The role bypasses row level security: choosing it defeats the purpose.
    bypass_rls: bool


class SupabaseProbeConnection(BaseModel):
    host: str
    port: int
    #: ``session`` / ``transaction`` (the pooler) or ``direct`` (IPv6 only
    #: unless the project has the IPv4 add-on).
    mode: str


class SupabaseProbeFunction(BaseModel):
    schema_name: str = Field(serialization_alias="schema")
    name: str
    signature: str
    returns: str
    returns_set: bool
    security_definer: bool
    volatility: str
    comment: str | None = None


class SupabaseProbeRelation(BaseModel):
    schema_name: str = Field(serialization_alias="schema")
    name: str
    kind: str
    rls_enabled: bool


class SupabaseProbeResponse(BaseModel):
    """What can be chosen; a part the probe could not reach is ``null``, its reason in ``errors``."""

    projects: list[SupabaseProbeProject] | None = None
    roles: list[SupabaseProbeRole] | None = None
    connections: list[SupabaseProbeConnection] | None = None
    #: The databases the role may connect to.
    databases: list[str] | None = None
    #: Whether the logged-in role bypasses row level security.
    role_bypasses_rls: bool | None = None
    #: The functions of the project's own schemas the role may execute.
    functions: list[SupabaseProbeFunction] | None = None
    #: The tables and views the role can still read directly (SELECT).
    readable_relations: list[SupabaseProbeRelation] | None = None
    errors: dict[str, str] = Field(default_factory=dict)

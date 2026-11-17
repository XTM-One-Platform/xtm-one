"""A document-wide ``security: []`` beside declared schemes still sends the credential.

Apache Airflow's stable REST API declares ``Basic`` (HTTP basic) in
``components.securitySchemes`` and ships ``security: []`` at the top level,
while every call needs the Basic credential. Read literally, every operation
was public and the integration's ``user:password`` was never sent (issue #10187).
"""

from __future__ import annotations

import base64

from app.services.declarative_http_executor import _AuthParts, _resolve_auth, _with_auth_scheme
from app.services.openapi_import.document import parse_spec_document
from app.services.openapi_import.operations import analyze_spec
from app.services.openapi_import.service import effective_tools

AIRFLOW_LIKE = """
openapi: 3.0.3
info: {title: Airflow API (Stable), version: 2.10.5}
servers:
  - url: https://airflow.example.test/api/v1
security: []
paths:
  /dags:
    get:
      operationId: get_dags
      parameters:
        - {name: limit, in: query, schema: {type: integer}}
      responses: {'200': {description: ok}}
  /health:
    get:
      operationId: get_health
      security: []
      responses: {'200': {description: ok}}
components:
  securitySchemes:
    Basic: {type: http, scheme: basic}
    Kerberos: {type: http, scheme: negotiate}
"""

PUBLIC_ONLY = """
openapi: 3.0.3
info: {title: Open data, version: '1'}
servers:
  - url: https://data.example.test
security: []
paths:
  /items:
    get:
      operationId: list_items
      responses: {'200': {description: ok}}
"""


def _ops(text: str):
    analysis = analyze_spec(parse_spec_document(text))
    return analysis, {op.key: op for op in analysis.operations}


def test_document_wide_empty_requirement_beside_schemes_sends_the_credential():
    analysis, ops = _ops(AIRFLOW_LIKE)
    assert ops["GET /dags"].sends_credential is True
    # An operation's own ``security: []`` still marks it public.
    assert ops["GET /health"].sends_credential is False
    basic = analysis.auth_option("Basic")
    assert basic is not None and basic.kind == "basic"


def test_document_without_schemes_keeps_its_public_reading():
    _, ops = _ops(PUBLIC_ONLY)
    assert ops["GET /items"].sends_credential is False


def test_http_basic_option_builds_the_basic_header_from_user_password():
    analysis, ops = _ops(AIRFLOW_LIKE)
    basic = analysis.auth_option("Basic")
    tools = effective_tools([ops["GET /dags"].tool], basic.to_dict())
    auth = tools[0]["execution"]["auth"]
    assert auth == {"type": "basic"}
    parts = _resolve_auth(auth, "svc-reader:s3cr3t-pass")
    expected = base64.b64encode(b"svc-reader:s3cr3t-pass").decode("ascii")
    assert parts.headers == {"Authorization": f"Basic {expected}"}


def test_a_spec_imported_with_every_tool_public_sends_the_credential_without_reimport():
    # Stored before the fix: every tool carries sends_credential False and no public_by.
    analysis, ops = _ops(AIRFLOW_LIKE)
    stored = []
    for key in ("GET /dags", "GET /health"):
        tool = dict(ops[key].tool)
        tool["openapi"] = {"operation": key, "sends_credential": False}
        stored.append(tool)
    tools = effective_tools(stored, analysis.auth_option("Basic").to_dict())
    assert [t["execution"]["auth"] for t in tools] == [{"type": "basic"}, {"type": "basic"}]


def test_a_spec_with_some_public_tools_keeps_them_public():
    analysis, ops = _ops(AIRFLOW_LIKE)
    tools = effective_tools([ops["GET /dags"].tool, ops["GET /health"].tool], analysis.auth_option("Basic").to_dict())
    assert [t["execution"]["auth"] for t in tools] == [{"type": "basic"}, {"type": "none"}]


def test_a_selection_of_operations_public_on_their_own_stays_public():
    # Only operations whose own ``security: []`` marks them public: imported since
    # the fix, they record it, so the credential is still withheld.
    analysis, ops = _ops(AIRFLOW_LIKE)
    assert ops["GET /health"].tool["openapi"]["public_by"] == "operation"
    tools = effective_tools([ops["GET /health"].tool], analysis.auth_option("Basic").to_dict())
    assert tools[0]["execution"]["auth"] == {"type": "none"}


def test_a_401_names_the_auth_scheme_sent():
    err = "Error: API authentication failed."
    assert _with_auth_scheme(err, {"type": "none"}, _AuthParts()).endswith(
        "The call carried NO credential (auth scheme sent: none)."
    )
    sent = _resolve_auth({"type": "basic"}, "u:p")
    assert _with_auth_scheme(err, {"type": "basic"}, sent).endswith("Auth scheme sent: basic.")

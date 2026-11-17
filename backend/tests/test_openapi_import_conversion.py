"""Converting Swagger 2.0 / OpenAPI 3.x operations into declarative HTTP tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.declarative_http_executor import validate_execution_spec
from app.services.openapi_import import operations as operations_mod
from app.services.openapi_import.document import parse_spec_document
from app.services.openapi_import.operations import (
    MAX_TOOL_NAME_CHARS,
    SpecAnalysis,
    analyze_spec,
    default_tool_prefix,
    safe_argument_name,
    snake_case,
)
from app.services.openapi_import.schema import RefResolver, SchemaConverter

FIXTURES = Path(__file__).parent / "openapi_fixtures"


def _analyze_file(name: str, source_url: str | None = None, **kwargs: Any) -> SpecAnalysis:
    return analyze_spec(parse_spec_document((FIXTURES / name).read_bytes(), source_url), **kwargs)


def _analyze(spec: dict[str, Any], source_url: str | None = None, **kwargs: Any) -> SpecAnalysis:
    return analyze_spec(parse_spec_document(json.dumps(spec), source_url), **kwargs)


def _op(analysis: SpecAnalysis, key: str):
    return next(op for op in analysis.operations if op.key == key)


def _v3(paths: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Test API", "version": "1"},
        "servers": [{"url": "https://api.example.test/v1"}],
        "paths": paths,
        **extra,
    }


# ── Whole-document behaviour ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "source_url"),
    [
        ("petstore_v2.json", None),
        ("petstore_v3.yaml", "https://petstore3.swagger.io/api/v3/openapi.yaml"),
        ("inventory_v31.yaml", None),
    ],
)
def test_every_generated_execution_spec_is_valid(name, source_url):
    analysis = _analyze_file(name, source_url)
    tools = analysis.tool_definitions()
    assert tools
    for key, tool in tools.items():
        assert validate_execution_spec(tool["execution"], tool_index=0) == [], key
        assert len(tool["name"]) <= MAX_TOOL_NAME_CHARS
        assert tool["input_schema"]["type"] == "object"
        assert tool["openapi"]["operation"] == key
    names = [t["name"] for t in tools.values()]
    assert len(names) == len(set(names))


def test_swagger2_servers_prefer_https_and_keep_the_base_path():
    analysis = _analyze_file("petstore_v2.json")
    assert [s["url"] for s in analysis.servers] == [
        "https://petstore.swagger.io/v2",
        "http://petstore.swagger.io/v2",
    ]
    assert analysis.default_server_url == "https://petstore.swagger.io/v2"


def test_relative_v3_server_resolves_against_the_document_url():
    analysis = _analyze_file("petstore_v3.yaml", "https://petstore3.swagger.io/api/v3/openapi.yaml")
    assert analysis.default_server_url == "https://petstore3.swagger.io/api/v3"


def test_relative_v3_server_from_a_file_asks_for_a_base_url():
    analysis = _analyze_file("petstore_v3.yaml")
    assert analysis.default_server_url is None
    assert any("/api/v3" in w and "relative" in w for w in analysis.warnings)


def test_server_variables_take_their_defaults():
    analysis = _analyze_file("inventory_v31.yaml")
    assert analysis.default_server_url == "https://eu.inventory.acme.test/v2"


def test_v3_document_without_servers_defaults_to_the_document_host():
    spec = _v3({"/ping": {"get": {"responses": {"200": {"description": "ok"}}}}})
    del spec["servers"]
    analysis = _analyze(spec, "https://docs.example.test/specs/openapi.json")
    assert analysis.default_server_url == "https://docs.example.test"


def test_titles_become_short_prefixes():
    assert default_tool_prefix("Swagger Petstore - OpenAPI 3.0") == "petstore"
    assert default_tool_prefix("GitHub v3 REST API") == "github"
    assert default_tool_prefix("Acme Inventory API") == "acme_inventory"
    assert default_tool_prefix("The API") == ""


def test_explicit_prefix_is_normalized_and_can_be_empty():
    assert _analyze_file("petstore_v2.json", tool_prefix="Pet Store!").tool_prefix == "pet_store"
    analysis = _analyze_file("petstore_v2.json", tool_prefix="")
    assert _op(analysis, "GET /pet/{petId}").tool_name == "get_pet_by_id"


def test_default_selection_skips_deprecated_and_unsupported():
    analysis = _analyze_file("petstore_v2.json")
    assert "GET /pet/findByTags" not in analysis.default_selection  # deprecated
    assert "GET /pet/findByTags" in analysis.supported_keys
    inventory = _analyze_file("inventory_v31.yaml")
    assert "HEAD /items/{item-id}" in inventory.supported_keys
    trace = next(op for op in inventory.operations if op.key == "TRACE /items/{item-id}")
    assert not trace.supported and "credentials included" in trace.skip_reason


# ── Naming ───────────────────────────────────────────────────────────────────


def test_snake_case_handles_camel_and_separators():
    assert snake_case("findPetsByStatus") == "find_pets_by_status"
    assert snake_case("repos/list-for-org") == "repos_list_for_org"
    assert snake_case("getHTTPResponse") == "get_http_response"


def test_names_without_operation_id_come_from_method_and_path():
    analysis = _analyze(
        _v3(
            {
                "/users": {
                    "get": {"responses": {"200": {"description": "ok"}}},
                    "post": {"responses": {"201": {"description": "ok"}}},
                },
                "/users/{userId}": {
                    "get": {
                        "parameters": [{"name": "userId", "in": "path", "required": True, "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "ok"}},
                    }
                },
            }
        ),
        tool_prefix="",
    )
    assert _op(analysis, "GET /users").tool_name == "list_users"
    assert _op(analysis, "POST /users").tool_name == "create_users"
    assert _op(analysis, "GET /users/{userId}").tool_name == "get_users_by_user_id"


def test_duplicate_names_are_disambiguated_and_capped():
    long_id = "a" * 80
    ops = {
        f"/p{i}": {"get": {"operationId": long_id, "responses": {"200": {"description": "ok"}}}}
        for i in range(3)
    }
    analysis = _analyze(_v3(ops), tool_prefix="")
    names = [op.tool_name for op in analysis.operations]
    assert len(set(names)) == 3
    assert all(len(n) <= MAX_TOOL_NAME_CHARS for n in names)


def test_many_duplicate_names_stay_unique():
    ops = {
        f"/p{i}": {"get": {"operationId": "list", "responses": {"200": {"description": "ok"}}}}
        for i in range(300)
    }
    names = [op.tool_name for op in _analyze(_v3(ops), tool_prefix="").operations]
    assert len(set(names)) == 300
    assert names[:3] == ["list", "list_2", "list_3"] and names[-1] == "list_300"


def test_the_name_budget_leaves_room_for_the_instance_suffix():
    """Two OpenAPI integrations on one agent: ``<tool>__<slug>`` within 64 characters."""
    from app.services import integration_tools

    room = (
        integration_tools._MAX_TOOL_NAME_LEN
        - MAX_TOOL_NAME_CHARS
        - len(integration_tools._INSTANCE_SUFFIX_SEP)
    )
    assert room >= integration_tools._MIN_INSTANCE_SLUG_LEN
    slug = integration_tools._instance_tool_slug(
        "openapi", "A rather long integration display name", "0" * 32, {}, max_len=room
    )
    name = "x" * MAX_TOOL_NAME_CHARS + integration_tools._INSTANCE_SUFFIX_SEP + slug
    assert len(name) <= integration_tools._MAX_TOOL_NAME_LEN


def test_a_name_shadowing_a_builtin_tool_is_renamed():
    analysis = _analyze(
        _v3({"/fetch": {"get": {"operationId": "web_fetch", "responses": {"200": {"description": "ok"}}}}}),
        tool_prefix="",
    )
    assert _op(analysis, "GET /fetch").tool_name == "web_fetch_api"


def test_argument_names_are_provider_safe():
    assert safe_argument_name("X-Request-ID") == "X_Request_ID"
    assert safe_argument_name("filter[status]") == "filter_status"
    assert safe_argument_name("$select") == "select"
    assert safe_argument_name("2fa") == "p_2fa"


# ── Parameters ───────────────────────────────────────────────────────────────


def test_swagger2_collection_formats_map_to_query_styles():
    analysis = _analyze_file("petstore_v2.json")
    by_status = _op(analysis, "GET /pet/findByStatus").tool["execution"]["query_mapping"]["status"]
    by_tags = _op(analysis, "GET /pet/findByTags").tool["execution"]["query_mapping"]["tags"]
    assert "explode" not in by_status  # multi == form + explode, the default
    assert by_tags["explode"] is False  # csv


def test_swagger2_enum_on_array_items_and_path_types():
    tool = _op(_analyze_file("petstore_v2.json"), "GET /pet/findByStatus").tool
    status = tool["input_schema"]["properties"]["status"]
    assert status["type"] == "array"
    assert status["items"]["enum"] == ["available", "pending", "sold"]
    get_pet = _op(_analyze_file("petstore_v2.json"), "GET /pet/{petId}").tool
    assert get_pet["input_schema"]["properties"]["petId"]["type"] == "integer"
    assert get_pet["input_schema"]["required"] == ["petId"]


def test_v31_parameter_locations_and_styles():
    tool = _op(_analyze_file("inventory_v31.yaml"), "GET /items").tool
    execution = tool["execution"]
    assert execution["query_mapping"]["filter"] == {"from": "filter", "style": "deepObject", "explode": True}
    assert execution["query_mapping"]["sku[]"] == {"from": "sku", "explode": False}
    assert execution["header_mapping"]["X-Tenant-Id"] == {"from": "X_Tenant_Id", "required": True}
    assert execution["cookie_mapping"]["locale"] == {"from": "locale"}
    limit = tool["input_schema"]["properties"]["limit"]
    assert limit == {"type": "integer", "maximum": 500, "exclusiveMinimum": 0}
    assert tool["input_schema"]["required"] == ["X_Tenant_Id"]


def test_ignored_header_parameters_are_left_out():
    analysis = _analyze(
        _v3(
            {
                "/x": {
                    "get": {
                        "parameters": [
                            {"name": "Accept", "in": "header", "schema": {"type": "string"}},
                            {"name": "Authorization", "in": "header", "required": True, "schema": {"type": "string"}},
                            {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
    )
    execution = _op(analysis, "GET /x").tool["execution"]
    assert list(execution["header_mapping"]) == ["X-Trace"]


def test_content_parameters_are_sent_as_json():
    analysis = _analyze(
        _v3(
            {
                "/search": {
                    "get": {
                        "parameters": [
                            {
                                "name": "q",
                                "in": "query",
                                "content": {"application/json": {"schema": {"type": "object"}}},
                            }
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
    )
    assert _op(analysis, "GET /search").tool["execution"]["query_mapping"]["q"]["style"] == "json"


def test_undeclared_path_placeholders_become_string_arguments():
    analysis = _analyze(_v3({"/things/{id}": {"get": {"responses": {"200": {"description": "ok"}}}}}))
    op = _op(analysis, "GET /things/{id}")
    assert op.supported
    assert op.tool["execution"]["path_params"] == ["id"]
    assert any("not declared" in w for w in op.warnings)


def test_path_level_parameters_are_inherited_and_overridden():
    analysis = _analyze(
        _v3(
            {
                "/a/{id}": {
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "v", "in": "query", "schema": {"type": "string"}},
                    ],
                    "get": {
                        "parameters": [{"name": "v", "in": "query", "schema": {"type": "integer"}}],
                        "responses": {"200": {"description": "ok"}},
                    },
                }
            }
        )
    )
    props = _op(analysis, "GET /a/{id}").tool["input_schema"]["properties"]
    assert props["id"]["type"] == "string"
    assert props["v"]["type"] == "integer"


def test_argument_name_collisions_between_locations():
    analysis = _analyze(
        _v3(
            {
                "/r/{id}": {
                    "put": {
                        "parameters": [
                            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                            {"name": "id", "in": "query", "schema": {"type": "string"}},
                        ],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}}
                                }
                            }
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
    )
    tool = _op(analysis, "PUT /r/{id}").tool
    assert set(tool["input_schema"]["properties"]) == {"id", "query_id", "body_id", "name"}
    assert tool["execution"]["path_mapping"]["id"]["from"] == "id"
    assert tool["execution"]["query_mapping"]["id"]["from"] == "query_id"
    assert tool["execution"]["body_mapping"]["id"]["from"] == "body_id"


# ── Request bodies ───────────────────────────────────────────────────────────


def test_json_object_bodies_are_flattened_without_read_only_fields():
    tool = _op(
        _analyze_file("petstore_v3.yaml", "https://petstore3.swagger.io/api/v3/openapi.yaml"),
        "PUT /pet",
    ).tool
    props = tool["input_schema"]["properties"]
    assert "id" not in props  # readOnly
    assert {"name", "photoUrls", "category", "tags", "status"} <= set(props)
    assert tool["input_schema"]["required"] == ["name", "photoUrls"]
    assert tool["execution"]["body_mapping"]["photoUrls"] == {"from": "photoUrls", "required": True}
    assert tool["execution"]["content_type"] == "application/json"
    # nullable enum survives as a plain enum
    assert props["status"]["enum"] == ["available", "pending", "sold"]


def test_request_body_references_are_resolved():
    tool = _op(
        _analyze_file("petstore_v3.yaml", "https://petstore3.swagger.io/api/v3/openapi.yaml"),
        "POST /pet",
    ).tool
    assert tool["input_schema"]["required"] == ["name", "photoUrls"]


def test_one_of_bodies_become_one_argument_with_any_of():
    tool = _op(_analyze_file("inventory_v31.yaml"), "POST /items").tool
    body = tool["input_schema"]["properties"]["body"]
    assert len(body["anyOf"]) == 2
    physical = body["anyOf"][0]
    assert {"weight_kg", "sku", "quantity"} <= set(physical["properties"])  # allOf merged
    assert physical["required"] == ["sku"]
    assert "@odata.type" in physical["description"]  # unsafe key described, not dropped silently
    assert tool["execution"]["body_from"] == "body"
    assert tool["execution"]["body_required"] is True


def test_recursive_models_are_cut_into_typed_stubs():
    tool = _op(_analyze_file("inventory_v31.yaml"), "POST /items").tool
    children = tool["input_schema"]["properties"]["body"]["anyOf"][0]["properties"]["category"]["properties"]["children"]
    assert children["items"]["type"] == "object"
    assert "recursive" in children["items"]["description"]


def test_ref_siblings_refine_the_target():
    tool = _op(_analyze_file("inventory_v31.yaml"), "PATCH /items/{item-id}").tool
    note = tool["input_schema"]["properties"]["note"]
    assert note == {"type": "string", "maxLength": 500, "description": "A free-text note on the item."}
    assert tool["execution"]["content_type"] == "application/merge-patch+json"
    assert tool["execution"]["path_mapping"]["item-id"]["from"] == "item_id"


def test_form_bodies_swagger2():
    analysis = _analyze_file("petstore_v2.json")
    form = _op(analysis, "POST /pet/{petId}").tool["execution"]
    assert form["body_encoding"] == "form"
    assert form["content_type"] == "application/x-www-form-urlencoded"
    assert set(form["body_mapping"]) == {"name", "status"}
    upload = _op(analysis, "POST /pet/{petId}/uploadImage")
    execution = upload.tool["execution"]
    assert upload.supported and upload.accepts_files
    assert execution["body_encoding"] == "multipart"
    assert set(execution["body_mapping"]) == {"additionalMetadata", "file"}
    assert execution["body_mapping"]["file"] == {"from": "file", "file": True}
    assert set(upload.tool["input_schema"]["properties"]["file"]["properties"]) >= {"file_id", "content"}


def test_required_multipart_file_becomes_a_file_argument():
    op = _op(_analyze_file("inventory_v31.yaml"), "POST /reports")
    assert op.supported and op.accepts_files
    execution = op.tool["execution"]
    assert execution["body_mapping"]["file"] == {"from": "file", "file": True, "required": True}
    assert execution["body_mapping"]["note"] == {"from": "note"}
    assert "file" in op.tool["input_schema"]["required"]
    file_schema = op.tool["input_schema"]["properties"]["file"]
    assert file_schema["type"] == "object"
    assert "file_id" in file_schema["description"]


def test_binary_body_becomes_a_file_argument():
    op = _op(
        _analyze_file("petstore_v3.yaml", "https://petstore3.swagger.io/api/v3/openapi.yaml"),
        "POST /pet/{petId}/uploadImage",
    )
    assert op.supported and op.accepts_files
    execution = op.tool["execution"]
    assert (execution["body_encoding"], execution["body_from"]) == ("binary", "file")
    assert execution["content_type"] == "application/octet-stream"
    assert "file" not in op.tool["input_schema"].get("required", [])


def test_xml_body_is_sent_as_a_file():
    analysis = _analyze(
        _v3(
            {
                "/x": {
                    "post": {
                        "requestBody": {"required": True, "content": {"application/xml": {"schema": {"type": "object"}}}},
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
    )
    op = _op(analysis, "POST /x")
    assert op.supported and op.accepts_files
    execution = op.tool["execution"]
    assert (execution["body_encoding"], execution["content_type"]) == ("binary", "application/xml")
    assert execution["body_required"] is True


def _body_op(content: dict[str, Any], *, required: bool = True) -> Any:
    analysis = _analyze(
        _v3(
            {
                "/upload": {
                    "post": {
                        "requestBody": {"required": required, "content": content},
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
    )
    return _op(analysis, "POST /upload")


def test_text_media_declaring_file_content_takes_a_file():
    op = _body_op({"text/csv": {"schema": {"type": "string", "format": "binary"}}})
    assert (op.tool["execution"]["body_encoding"], op.tool["execution"]["content_type"]) == ("binary", "text/csv")
    text = _body_op({"text/csv": {"schema": {"type": "string"}}})
    assert text.tool["execution"]["body_encoding"] == "text"


def test_multipart_part_media_type_and_file_lists():
    op = _body_op(
        {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "data": {"type": "string", "format": "binary"},
                        "attachments": {"type": "array", "items": {"type": "string", "format": "binary"}},
                        "columns": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["data"],
                },
                "encoding": {"data": {"contentType": "text/csv"}, "attachments": {"contentType": "image/png, image/jpeg"}},
            }
        }
    )
    mapping = op.tool["execution"]["body_mapping"]
    assert mapping["data"] == {"from": "data", "file": True, "content_type": "text/csv", "required": True}
    assert mapping["attachments"] == {"from": "attachments", "file": True}
    assert mapping["columns"] == {"from": "columns"}
    attachments = op.tool["input_schema"]["properties"]["attachments"]
    assert attachments["type"] == "array" and attachments["items"]["type"] == "object"


def test_file_field_in_a_urlencoded_form_is_unsupported():
    op = _body_op(
        {
            "application/x-www-form-urlencoded": {
                "schema": {
                    "type": "object",
                    "properties": {"data": {"type": "string", "format": "binary"}},
                    "required": ["data"],
                }
            }
        }
    )
    assert not op.supported
    assert "form-urlencoded" in op.skip_reason


def test_swagger2_binary_body_parameter():
    analysis = _analyze(
        {
            "swagger": "2.0",
            "info": {"title": "Files", "version": "1"},
            "host": "files.example.test",
            "paths": {
                "/blobs": {
                    "put": {
                        "consumes": ["application/octet-stream"],
                        "parameters": [
                            {"name": "blob", "in": "body", "required": True, "schema": {"type": "string", "format": "binary"}}
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
    )
    execution = _op(analysis, "PUT /blobs").tool["execution"]
    assert (execution["body_encoding"], execution["content_type"]) == ("binary", "application/octet-stream")
    assert execution["body_required"] is True


def test_file_context_is_never_an_argument_name():
    analysis = _analyze(
        _v3(
            {
                "/x": {
                    "get": {
                        "parameters": [{"name": "file_context", "in": "query", "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
    )
    tool = _op(analysis, "GET /x").tool
    assert "file_context" not in tool["input_schema"]["properties"]
    assert tool["execution"]["query_mapping"]["file_context"]["from"] == "query_file_context"


def test_accented_titles_and_names_are_transliterated():
    assert default_tool_prefix("API Géoplateforme - Géocodage") == "geoplateforme"
    assert default_tool_prefix("Acme Inventory API") == "acme_inventory"
    assert snake_case("géocoderAdresse") == "geocoder_adresse"
    assert safe_argument_name("rayon_éloigné") == "rayon_eloigne"


def test_text_body_and_operation_server():
    entry = _op(_analyze_file("inventory_v31.yaml"), "POST /export")
    execution = entry.tool["execution"]
    assert execution["body_encoding"] == "text"
    assert execution["content_type"] == "text/csv"
    assert execution["server_url"] == "https://exports.acme.test"
    assert any("without the integration's credentials" in w for w in entry.warnings)


@pytest.mark.parametrize(
    ("server", "security", "warned"),
    [
        ("https://uploads.example.org", [{"key": []}], True),
        ("https://api.example.test/v2", [{"key": []}], False),
        ("https://uploads.example.org", [], False),
    ],
)
def test_an_operation_server_elsewhere_is_flagged(server, security, warned):
    """Only when the call would need the credential it will not get."""
    spec = _v3(
        {"/up": {"post": {"servers": [{"url": server}], "security": security, "responses": {"200": {"description": "ok"}}}}},
        components={"securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Key"}}},
    )
    entry = _op(_analyze(spec), "POST /up")
    assert entry.tool["execution"]["server_url"] == server
    assert any("another server than the API's base URL" in w for w in entry.warnings) is warned


def test_array_bodies_use_the_body_argument():
    analysis = _analyze(
        _v3(
            {
                "/bulk": {
                    "post": {
                        "requestBody": {
                            "required": True,
                            "content": {"application/json": {"schema": {"type": "array", "items": {"type": "string"}}}},
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
    )
    tool = _op(analysis, "POST /bulk").tool
    assert tool["input_schema"]["properties"]["body"]["type"] == "array"
    assert tool["input_schema"]["required"] == ["body"]


def test_accept_prefers_the_json_response():
    execution = _op(_analyze_file("petstore_v2.json"), "GET /pet/{petId}").tool["execution"]
    assert execution["accept"] == "application/json"


# ── Security ─────────────────────────────────────────────────────────────────


def test_auth_options_v2():
    analysis = _analyze_file("petstore_v2.json")
    options = {o.id: o for o in analysis.auth_options}
    assert options["api_key"].kind == "api_key"
    assert (options["api_key"].location, options["api_key"].param_name) == ("header", "api_key")
    assert options["api_key"].runtime_auth() == {"type": "header", "header": "api_key", "prefix": ""}
    implicit = options["petstore_auth:implicit"]
    assert (implicit.supported, implicit.kind, implicit.integration_auth_type) == (True, "oauth2_implicit", "oauth")
    assert options["manual_bearer"].integration_auth_type == "api_key"
    assert analysis.default_auth_option == "api_key"


def test_auth_options_v31():
    analysis = _analyze_file("inventory_v31.yaml")
    options = {o.id: o for o in analysis.auth_options}
    code = options["oauth:authorization_code"]
    assert code.integration_auth_type == "oauth"
    assert code.authorize_url == "https://auth.acme.test/authorize"
    # A relative token URL resolves against the server URL.
    assert code.token_url == "https://eu.inventory.acme.test/oauth/token"
    assert set(code.scopes) == {"items:read", "items:write"}
    cc = options["oauth:client_credentials"]
    assert cc.integration_auth_type == "client_credentials"
    assert options["bearer"].label == "Bearer token (JWT)"
    assert options["session"].runtime_auth() == {"type": "cookie", "cookie": "SESSION"}
    assert not options["oidc"].supported
    assert "manual_bearer" not in options  # a real bearer scheme exists
    assert analysis.default_auth_option == "oauth:authorization_code"


def test_security_requirements_decide_who_carries_the_credential():
    analysis = _analyze_file("petstore_v3.yaml", "https://petstore3.swagger.io/api/v3/openapi.yaml")
    assert _op(analysis, "GET /user/logout").sends_credential is False  # security: []
    assert _op(analysis, "GET /user/logout").tool["openapi"]["sends_credential"] is False
    assert _op(analysis, "GET /pet/{petId}").sends_credential is True
    # Undeclared requirement: the configured credential is still sent.
    assert _op(_analyze_file("petstore_v2.json"), "POST /store/order").sends_credential is True


def test_scheme_named_like_a_synthetic_option_keeps_its_own_id():
    analysis = _analyze(
        _v3(
            {"/x": {"get": {"responses": {"200": {"description": "ok"}}}}},
            components={"securitySchemes": {"none": {"type": "apiKey", "in": "header", "name": "X-Key"}}},
            security=[{"none": []}],
        )
    )
    ids = [o.id for o in analysis.auth_options]
    assert ids.count("none") == 1
    assert analysis.auth_option("scheme:none").param_name == "X-Key"
    assert analysis.default_auth_option == "scheme:none"


# ── Schema conversion details ────────────────────────────────────────────────


def _convert(schema: dict[str, Any], *, family: str = "3.0", doc: dict | None = None, **kwargs: Any):
    return SchemaConverter(RefResolver(doc or {}), family=family).convert(schema, **kwargs)


def test_openapi30_exclusive_bounds_become_numbers():
    assert _convert({"type": "integer", "minimum": 1, "exclusiveMinimum": True}) == {
        "type": "integer",
        "exclusiveMinimum": 1,
    }


def test_patterns_are_described_not_enforced():
    """Validated by a backtracking engine, ``^(a+)+$`` would hang on ``aaa…!``."""
    out = _convert({"type": "string", "pattern": "^(a+)+$", "description": "A code."})
    assert "pattern" not in out
    assert out["description"] == "A code. Pattern: ^(a+)+$"
    assert "pattern" not in _convert({"type": "string", "pattern": "x" * 201})


def test_an_imported_pattern_never_reaches_argument_validation():
    from app.tools.agent_loop import validate_tool_arguments

    document = {
        "openapi": "3.0.0",
        "info": {"title": "Codes", "version": "1"},
        "servers": [{"url": "https://api.example.test"}],
        "paths": {
            "/codes/{code}": {
                "get": {
                    "operationId": "getCode",
                    "parameters": [
                        {
                            "name": "code",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "pattern": "^(a+)+$"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    schema = _analyze(document).operations[0].tool["input_schema"]
    assert '"pattern"' not in json.dumps(schema)
    # Instant; with the pattern enforced this input backtracks 2**40 times.
    assert validate_tool_arguments("codes_get_code", {"code": "a" * 40 + "!"}, schema) is None


def test_string_enums_are_coerced_to_strings():
    assert _convert({"type": "string", "enum": [1, 2, True]})["enum"] == ["1", "2", "true"]


def test_huge_enums_become_a_description_sample():
    out = _convert({"type": "string", "enum": [f"v{i}" for i in range(500)]})
    assert "enum" not in out
    assert "500 values" in out["description"]


def test_depth_limit_stubs_nested_objects():
    deep = {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "object", "properties": {"c": {"type": "string"}}}}}}}
    out = _convert(deep, max_depth=1)
    assert out["properties"]["a"]["type"] == "object"
    assert "properties" not in out["properties"]["a"]
    assert "omitted" in out["properties"]["a"]["description"]


def test_unresolvable_refs_are_reported_and_permissive():
    resolver = RefResolver({})
    out = SchemaConverter(resolver, family="3.0").convert({"$ref": "other.yaml#/Pet"})
    assert "could not be resolved" in out["description"]
    assert "other.yaml#/Pet" in resolver.unresolved


def test_oversized_schemas_are_reduced_by_depth(monkeypatch):
    monkeypatch.setattr(operations_mod, "MAX_TOOL_SCHEMA_CHARS", 400)
    nested = {"type": "object", "properties": {f"f{i}": {"type": "object", "properties": {f"g{j}": {"type": "string", "description": "x" * 40} for j in range(5)}} for i in range(4)}}
    analysis = _analyze(
        _v3({"/big": {"post": {"requestBody": {"content": {"application/json": {"schema": nested}}}, "responses": {"200": {"description": "ok"}}}}})
    )
    tool = _op(analysis, "POST /big").tool
    assert tool is not None
    assert all("properties" not in p for p in tool["input_schema"]["properties"].values())


def test_operation_cap(monkeypatch):
    monkeypatch.setattr(operations_mod, "MAX_OPERATIONS", 2)
    ops = {f"/p{i}": {"get": {"responses": {"200": {"description": "ok"}}}} for i in range(4)}
    analysis = _analyze(_v3(ops))
    assert len(analysis.operations) == 2
    assert any("more than 2 operations" in w for w in analysis.warnings)

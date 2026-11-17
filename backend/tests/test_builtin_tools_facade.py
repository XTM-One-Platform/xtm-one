"""Regression tests for the ``builtin_tools`` package compatibility facade.

``app/services/builtin_tools.py`` was mechanically split into the
``app/services/builtin_tools/`` package.  The package ``__init__`` is a
compatibility facade that must keep re-exporting every name that was
importable from the original module, so all existing imports
(``from app.services.builtin_tools import X``) keep working unchanged.
"""

import importlib

import pytest

#: Public surface documented in dev-docs/reference/backend-services-tools.md
#: ("Shared Builtin Tools") plus every public name the
#: backend imports from ``app.services.builtin_tools``.
PUBLIC_SURFACE = [
    "build_code_interpreter_tool",
    "build_image_generation_tool",
    "build_generate_file_tool",
    "PDF_RENDER_FORMAT",
    "PDF_RENDER_MARGIN",
    "DOCX_CONTENT_TYPE",
    "build_kb_search_tool",
    "build_kb_store_tool",
    "build_kb_document_read_tools",
    "build_kb_document_update_tool",
    "load_kb_names",
    "format_knowledge_base_prompt",
    "build_memory_store_tool",
    "build_memory_search_tool",
    "build_web_fetch_tool",
    "build_browser_tool",
    "build_stix_bundle_validator_tool",
    "build_fimi_analytics_composite",
    "build_work_item_tools",
    "build_shared_state_tools",
    "build_sub_agent_tools",
    "resolve_handover_agent",
    "normalize_visited_agents",
    "extract_visited_agents",
    "handover_cycle_error",
    "build_agent_builder_tool",
    "filter_integration_tools",
    "merge_tool_allow_lists",
    "apply_tool_deny_list",
    "serialize_messages",
    "format_datetime_prompt",
    "format_user_identity",
    "build_identity_context",
    "build_agent_context",
    "ACTING_FOR_VERB",
    "PLATFORM_IDENTITY_KEYS",
    "format_element_bindings_prompt",
    "load_agent_prompts",
    "load_mcp_tools",
    "load_custom_tools",
    "cleanup_mcp_manager",
    "logger",
]

#: Private names referenced from outside the package (tests and
#: ``api/v1/settings.py``) that the facade must keep exposing.
PRIVATE_COMPAT_SURFACE = [
    "_platform_label",
    "_deduplicate_namespaced_tools",
    "_persist_generated_file",
    "_render_text_to_pdf",
    "_render_text_to_docx",
    "_load_caller_user_for_builder",
    "_load_writable_agent_for_builder",
    "_load_writable_assignment_for_builder",
    "_load_manageable_entity_for_builder",
    "_validate_id_arg",
    "_invalid_task_id_message",
    "_is_uuid_like",
]


@pytest.mark.parametrize("name", PUBLIC_SURFACE + PRIVATE_COMPAT_SURFACE)
def test_facade_exports_name(name: str) -> None:
    module = importlib.import_module("app.services.builtin_tools")
    assert getattr(module, name, None) is not None, (
        f"app.services.builtin_tools no longer exposes {name!r}"
    )


def test_logger_keeps_historical_name() -> None:
    """Log records must keep the pre-split logger name (used by caplog
    filters and log-based monitoring)."""
    from app.services import builtin_tools

    assert builtin_tools.logger.name == "app.services.builtin_tools"


def test_facade_setattr_mirrors_to_submodules() -> None:
    """Monkeypatching an attribute on the facade must rebind the module
    global the tool closures actually read (pre-split semantics)."""
    from app.services import builtin_tools
    from app.services.builtin_tools import loaders, media

    original = builtin_tools._persist_generated_file
    sentinel = object()
    try:
        builtin_tools._persist_generated_file = sentinel
        assert media._persist_generated_file is sentinel
        assert loaders._persist_generated_file is sentinel
    finally:
        builtin_tools._persist_generated_file = original
    assert media._persist_generated_file is original
    assert loaders._persist_generated_file is original

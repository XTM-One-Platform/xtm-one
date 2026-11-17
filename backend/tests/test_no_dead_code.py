"""Every function, class or method of ``app/`` that nothing names fails the build (``backend/AGENTS.md``, "Dead code").

Vulture reads names, not strings: check for a computed ``getattr``, a string registry or ``importlib`` before
deleting. A name a test uses keeps a definition alive, so dead code with a unit test is not reported.
:data:`KNOWN_DEAD` only shrinks.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from vulture import Vulture

from tests import app_sources

BACKEND = app_sources.BACKEND_DIR

#: What vulture reads: a name used anywhere here keeps a definition alive.
SCANNED = ("app", "tests", "scripts", "alembic")

#: Decorators that register what they decorate, so the definition is reached
#: without its name (vulture ``fnmatch`` patterns).
REGISTERING_DECORATORS = (
    "@*router.*",  # FastAPI routes, every APIRouter of the API
    "@application.*",  # the FastAPI app's own routes and exception handlers
    "@*event.listens_for",  # SQLAlchemy event listeners (``event`` or ``sa.event``)
    "@client.event",  # discord.py event handlers
    "@_tool",  # intune_management: registers the tool in the ToolExecutor under fn.__name__
    "@field_validator",
    "@model_validator",
    "@field_serializer",
    "@model_serializer",
)

#: Methods a library calls by name, with the library.
LIBRARY_HOOKS: dict[str, str] = {
    "handle_starttag": "html.parser.HTMLParser callback",
    "handle_startendtag": "html.parser.HTMLParser callback",
    "handle_endtag": "html.parser.HTMLParser callback",
    "handle_data": "html.parser.HTMLParser callback",
    "async_auth_flow": "httpx.Auth flow",
    "sync_auth_flow": "httpx.Auth flow",
    "connect_unix_socket": "httpcore network backend interface",
    "get_field_value": "pydantic-settings PydanticBaseSettingsSource abstract method",
    "settings_customise_sources": "pydantic-settings BaseSettings hook",
    "_validate_tool_result": "mcp.ClientSession hook the platform turns off",
    "__getattr__": "module-level lazy attribute (PEP 562)",
    "stream_response": "starlette StreamingResponse hook (chat_web_transport.EventStreamResponse)",
}

_BASE_INTEGRATION = "a BaseIntegration subclass no code instantiates: the module-level validate_connection() is used"

#: Dead code whose removal is more than a deletion, as ``(path, name)``.
KNOWN_DEAD: dict[tuple[str, str], str] = {
    ("app/core/config.py", "code_interpreter_allowed_modules"): (
        "an environment-only property no code may read: test_security_options.py refuses a read"
    ),
    ("app/core/config.py", "custom_tool_allowed_modules"): (
        "an environment-only property no code may read: test_security_options.py refuses a read"
    ),
    ("app/models/ctem_metric.py", "CtemMetric"): "an ORM model nothing reads: dropping its table needs a migration",
    ("app/tools/tool_events.py", "current_tool_iteration"): (
        "the only reader of ToolEventBinding.iteration: removing it reaches the agent loop"
    ),
    ("app/integrations/providers/cybersixgill.py", "CybersixgillIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/domaintools.py", "DomainToolsIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/facebook.py", "FacebookIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/figma.py", "FigmaIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/flashpoint.py", "FlashpointIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/hackerone.py", "HackerOneIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/hibob.py", "HiBobIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/hubspot.py", "HubSpotIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/linkedin.py", "LinkedInIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/n8n.py", "N8nIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/sekoia.py", "SekoiaIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/slack.py", "SlackIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/socradar.py", "SOCRadarIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/supabase.py", "SupabaseIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/threatq.py", "ThreatQIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/wiz.py", "WizIntegration"): _BASE_INTEGRATION,
    ("app/integrations/providers/youtrack.py", "YouTrackIntegration"): _BASE_INTEGRATION,
}


def _dead_definitions(root: Path, scanned: tuple[str, ...], checked: str) -> tuple[set[tuple[str, str]], str]:
    """``(path, name)`` of every dead definition under ``checked``, and vulture's error output."""
    vulture = Vulture(ignore_names=list(LIBRARY_HOOKS), ignore_decorators=list(REGISTERING_DECORATORS))
    errors = io.StringIO()
    with contextlib.redirect_stderr(errors):
        vulture.scavenge([str(root / path) for path in scanned])
    dead = {
        (Path(item.filename).resolve().relative_to(root).as_posix(), item.name)
        for item in vulture.get_unused_code(min_confidence=60)
        if item.typ in ("function", "class", "method", "property")
    }
    return {entry for entry in dead if entry[0].startswith(f"{checked}/")}, errors.getvalue()


def test_no_definition_is_dead() -> None:
    """One test, so one scan: CI splits the suite by test id (``tests/sharding.py``)."""
    dead, errors = _dead_definitions(BACKEND.resolve(), SCANNED, "app")
    # A module vulture cannot parse is a module it cannot check (a comment
    # starting ``# type:`` is read as a type annotation).
    assert errors == ""
    new = sorted(f"{path}: {name}" for path, name in dead - KNOWN_DEAD.keys())
    assert not new, (
        f"Dead code: {new}. Before deleting, check for a reader vulture cannot see "
        "(a computed getattr name, a string registry, importlib); then delete it and the docs that describe it."
    )
    # The list only shrinks: an entry removed from the code leaves it too.
    stale = sorted(f"{path}: {name}" for path, name in KNOWN_DEAD.keys() - dead)
    assert not stale, f"No longer dead (remove from KNOWN_DEAD): {stale}"


def test_the_check_catches_what_it_describes(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "module.py").write_text(
        "from html.parser import HTMLParser\n"
        "\n"
        "def used(): ...\n"
        "def dead(): ...\n"
        "def tested(): ...\n"
        "@router.get('/x')\n"
        "def route(): ...\n"
        "@functools.cache\n"
        "def cached(): ...\n"
        "class Parser(HTMLParser):\n"
        "    def handle_data(self, data): ...\n"
        "    def unread(self): ...\n"
        "    @property\n"
        "    def unread_property(self): ...\n"
        "used()\n"
        "Parser()\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_module.py").write_text("from app.module import tested\n\ntested()\n")
    dead, errors = _dead_definitions(tmp_path, ("app", "tests"), "app")
    assert dead == {
        ("app/module.py", "dead"),
        ("app/module.py", "cached"),
        ("app/module.py", "unread"),
        ("app/module.py", "unread_property"),
    }
    assert errors == ""

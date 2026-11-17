"""Copy names and imported text fields fit their columns.

The unit half of ``test_name_length_guards_postgres.py``. A copy's name is the
original's shortened to fit its ``String(n)`` column with the ``Copy of`` /
``(copy)`` affix intact, an agent slug keeps its counter inside ``Agent.slug``,
and agent import refuses a text value its column cannot take before the
database is touched. Everything that fits is unchanged.
"""

from __future__ import annotations

import pathlib
import re
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.custom_tool import CustomTool
from app.models.mcp_server import MCPServer
from app.models.variable import Variable

AGENT_NAME = Agent.__table__.c.name
LIMIT = AGENT_NAME.type.length


# ---------------------------------------------------------------------------
# Copy names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("prefix", "suffix"), [("Copy of ", ""), ("", " (copy)")])
def test_a_copy_name_that_fits_is_unchanged(prefix, suffix):
    from app.core.utils import fit_copy_name

    assert fit_copy_name("Digest", AGENT_NAME, prefix=prefix, suffix=suffix) == (
        f"{prefix}Digest{suffix}"
    )
    at_limit = "n" * (LIMIT - len(prefix) - len(suffix))
    assert fit_copy_name(at_limit, AGENT_NAME, prefix=prefix, suffix=suffix) == (
        f"{prefix}{at_limit}{suffix}"
    )


@pytest.mark.parametrize(("prefix", "suffix"), [("Copy of ", ""), ("", " (copy)")])
def test_a_long_original_is_shortened_and_the_affix_kept(prefix, suffix):
    from app.core.utils import fit_copy_name

    name = fit_copy_name("n" * LIMIT, AGENT_NAME, prefix=prefix, suffix=suffix)

    assert len(name) == LIMIT
    assert name == f"{prefix}{'n' * (LIMIT - len(prefix) - len(suffix))}{suffix}"


def test_an_unbounded_column_takes_the_whole_name():
    from app.core.utils import fit_copy_name

    unbounded = SimpleNamespace(type=SimpleNamespace(length=None))
    assert fit_copy_name("n" * 5000, unbounded, suffix=" (copy)") == "n" * 5000 + " (copy)"


def _column(length: int) -> SimpleNamespace:
    return SimpleNamespace(type=SimpleNamespace(length=length))


@pytest.mark.parametrize(
    ("original", "kept"),
    [
        ("abce\u0301f", "abc"),  # e + combining acute accent
        ("abc\U0001F44D\U0001F3FD", "abc"),  # thumbs up + skin tone
        ("a\U0001F468\u200d\U0001F469\u200d\U0001F467b", "a"),  # ZWJ family
        ("abc\U0001F1EB\U0001F1F7", "abc"),  # a flag
        ("x\U0001F1EB\U0001F1F7\U0001F1E9\U0001F1EA", "x\U0001F1EB\U0001F1F7"),  # two flags
        ("ab1\ufe0f\u20e3", "ab"),  # keycap
        ("abc\r\nd", "abc"),
        ("ab\u1100\u1161\u11a8", "ab"),  # a decomposed Hangul syllable
        ("ab\u0915\u094d\u0937", "ab"),  # Devanagari ksha: ka + virama + ssa
        ("ab\u0915\u094d x", "ab\u0915\u094d"),  # a virama ending a word stays with it
        ("ab\u0e19\u0e49\u0e33", "ab"),  # Thai "water": no + mai tho + sara am
        ("abc\u0600\u0661", "abc"),  # an Arabic number sign prepended to its digit
        ("abc\U000113D1\U00011380", "abc"),  # a Tulu-Tigalari repha before its letter
        ("abcdef", "abcd"),
        ("abc de", "abc"),  # no space left before the suffix
    ],
)
def test_a_shortened_original_is_cut_between_characters(original, kept):
    """PostgreSQL counts code points like ``len``, so a cut never breaks the
    encoding; it is moved back so it never separates a letter from its accent,
    an emoji from its modifier or joiner, or the two halves of a flag either."""
    from app.core.utils import fit_copy_name

    assert fit_copy_name(original, _column(4)) == kept
    assert fit_copy_name(original, _column(4 + len(" (copy)")), suffix=" (copy)") == f"{kept} (copy)"


def test_a_cut_with_no_boundary_before_it_falls_back_to_the_room():
    from app.core.utils import fit_copy_name

    assert fit_copy_name("e" + "\u0301" * 10, _column(4)) == "e\u0301\u0301\u0301"


def test_a_name_that_fits_is_never_cut_whatever_it_ends_with():
    from app.core.utils import fit_copy_name

    for original in ("abe\u0301", "a\U0001F44D\U0001F3FD", "\U0001F1EB\U0001F1F7\U0001F1E9"):
        assert fit_copy_name(original, _column(len(original))) == original


# ---------------------------------------------------------------------------
# Agent slugs
# ---------------------------------------------------------------------------

SLUG_LIMIT = Agent.__table__.c.slug.type.length


class _Slugs:
    """A session answering the slug lookups of ``generate_unique_agent_slug``."""

    def __init__(self, *taken: str):
        self.taken = set(taken)
        self.asked: list[str] = []

    async def execute(self, statement):
        slug = next(value for key, value in statement.compile().params.items() if key.startswith("slug"))
        self.asked.append(slug)
        return SimpleNamespace(scalar_one_or_none=lambda: uuid.uuid4() if slug in self.taken else None)


@pytest.mark.asyncio
async def test_a_slug_that_fits_is_generated_as_before():
    from app.api.v1.helpers import generate_unique_agent_slug

    db = _Slugs("weekly-digest", "weekly-digest-2")
    assert await generate_unique_agent_slug(db, "Weekly digest") == "weekly-digest-3"
    assert db.asked == ["weekly-digest", "weekly-digest-2", "weekly-digest-3"]


@pytest.mark.asyncio
async def test_a_slug_at_the_limit_keeps_its_counter_inside_the_column():
    """A second copy of an at-limit name, or its second import, was a 500."""
    from app.api.v1.helpers import generate_unique_agent_slug

    base = "n" * SLUG_LIMIT
    db = _Slugs(base)
    slug = await generate_unique_agent_slug(db, base)

    assert slug == "n" * (SLUG_LIMIT - 2) + "-2"
    assert db.asked == [base, slug]


@pytest.mark.asyncio
async def test_a_slug_longer_than_its_name_is_fitted_too():
    """``slugify`` decomposes one character into several (U+33AF is ``rads2``)."""
    from app.api.v1.helpers import generate_unique_agent_slug

    slug = await generate_unique_agent_slug(_Slugs(), "\u33af" * 41)

    assert (len(slug), slug[:10]) == (SLUG_LIMIT, "rads2rads2")


def test_a_fitted_slug_never_ends_on_the_hyphen_of_a_cut_word():
    from app.api.v1.helpers import fit_agent_slug

    assert fit_agent_slug("digest") == "digest"
    assert fit_agent_slug("digest", "-2") == "digest-2"
    # The room before "-2" ends right after a hyphen of the base.
    base = "copy-of-" + "word-" * 50 + "end"
    assert base[SLUG_LIMIT - 3] == "-"
    fitted = fit_agent_slug(base, "-2")
    assert (len(fitted), fitted[-6:]) == (SLUG_LIMIT - 1, "word-2") and "--" not in fitted


def test_no_copy_path_builds_its_name_by_hand():
    """A new ``Copy of`` / ``(copy)`` name goes through ``fit_copy_name``."""
    app_dir = pathlib.Path(__file__).parent.parent / "app"
    by_hand = re.compile(r"""f["']Copy of \{|\} \(copy\)["']""")
    offenders = [
        f"{path.relative_to(app_dir)}:{number}"
        for folder in ("api", "services", "models")
        for path in sorted((app_dir / folder).rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if by_hand.search(line)
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# The text rule
# ---------------------------------------------------------------------------


def test_the_text_rule_reads_the_column():
    from app.core.utils import text_column_error

    command = MCPServer.__table__.c.command
    limit = command.type.length
    assert text_column_error(command, None) is None
    assert text_column_error(command, "") is None
    assert text_column_error(command, "c" * limit) is None
    assert text_column_error(command, "c" * (limit + 1)) == (
        f"command must be at most {limit} characters (got {limit + 1})."
    )
    assert text_column_error(command, 5) == "command must be a string, got int."
    # A Text column has no length: only the type is checked.
    assert text_column_error(Variable.__table__.c.value, "v" * 100_000) is None
    assert text_column_error(Variable.__table__.c.name, True, field="key") == (
        "key must be a string, got bool."
    )


# ---------------------------------------------------------------------------
# Agent import
# ---------------------------------------------------------------------------


def _limit(model, field: str) -> int:
    return model.__table__.c[field].type.length


def _bundle(**sections) -> dict:
    return {"version": 3, "agent": {"name": "Imported agent"}, **sections}


#: (bundle, expected 400 detail) - one per kind of field the import writes.
_REFUSED = [
    (
        _bundle(custom_tools=[{"name": "Lookup", "icon_url": "u" * (_limit(CustomTool, "icon_url") + 1)}]),
        f"Imported custom tool 'Lookup': icon_url must be at most "
        f"{_limit(CustomTool, 'icon_url')} characters (got {_limit(CustomTool, 'icon_url') + 1}).",
    ),
    (
        _bundle(mcp_servers=[{"name": "Docs", "transport": "t" * (_limit(MCPServer, "transport") + 1)}]),
        f"Imported MCP server 'Docs': transport must be at most {_limit(MCPServer, 'transport')} "
        f"characters (got {_limit(MCPServer, 'transport') + 1}).",
    ),
    (
        _bundle(mcp_servers=[{"name": "Docs", "command": 5}]),
        "Imported MCP server 'Docs': command must be a string, got int.",
    ),
    (
        _bundle(variables=[{"id": "v1", "name": "n" * (_limit(Variable, "name") + 1)}]),
        f"Imported variable #1: name must be at most {_limit(Variable, 'name')} characters "
        f"(got {_limit(Variable, 'name') + 1}).",
    ),
    (
        _bundle(variables=[{"id": "v1", "name": "REGION", "value": 5}]),
        "Imported variable 'REGION': value must be a string, got int.",
    ),
    (
        _bundle(prompts=[{"name": "Weekly", "content": ["x"]}]),
        "Imported prompt 'Weekly': content must be a string, got list.",
    ),
    (
        {"version": 3, "agent": {"name": "Imported agent", "icon": "i" * (_limit(Agent, "icon") + 1)}},
        f"Imported agent: icon must be at most {_limit(Agent, 'icon')} characters "
        f"(got {_limit(Agent, 'icon') + 1}).",
    ),
    (
        {"version": 3, "agent": {"name": 5}},
        "Imported agent: name must be a string, got int.",
    ),
    (
        _bundle(assignments=[{"name": "Digest", "prompt": "Go.", "description": 5}]),
        "Imported assignment 'Digest': description must be a string, got int.",
    ),
    (
        _bundle(assignments=[{"name": "Digest", "prompt": "Go.", "human_in_the_loop_prompt": {}}]),
        "Imported assignment 'Digest': human_in_the_loop_prompt must be a string, got dict.",
    ),
    (
        _bundle(variables=[{"id": "v1", "key": "k" * (_limit(Variable, "name") + 1)}]),
        f"Imported variable #1: key must be at most {_limit(Variable, 'name')} characters "
        f"(got {_limit(Variable, 'name') + 1}).",
    ),
    (
        _bundle(variables=[{"id": "v1", "key": "REGION", "value": 5}]),
        "Imported variable 'REGION': value must be a string, got int.",
    ),
    (
        _bundle(assignments=[{"name": "Digest", "prompt": "Go.", "delay": 10}]),
        "Imported assignment 'Digest': delay must be a string, got int.",
    ),
    (
        _bundle(custom_tools=[{"name": 5}]),
        "Imported custom tool entry 'name' must be a string, got int.",
    ),
]


class _Untouchable:
    """A session that fails the test on any use."""

    def __getattr__(self, name: str):
        raise AssertionError(f"the import touched the database ({name})")


@pytest.mark.asyncio
@pytest.mark.parametrize(("bundle", "detail"), _REFUSED)
async def test_import_refuses_before_the_database_is_touched(bundle, detail):
    from app.api.v1.agent_import import create_agent_from_bundle

    with pytest.raises(HTTPException) as exc_info:
        await create_agent_from_bundle(
            _Untouchable(), user=SimpleNamespace(id=uuid.uuid4()), bundle=bundle
        )

    assert (exc_info.value.status_code, exc_info.value.detail) == (400, detail)


def test_import_keeps_every_value_its_column_takes():
    """At the limit, absent or null, a secret's value (not imported) and an
    unlisted model (dropped by the import) pass the check."""
    from app.api.v1.agent_import import _require_importable

    bundle = {
        "version": 3,
        "agent": {
            "name": "n" * _limit(Agent, "name"),
            "icon": "i" * _limit(Agent, "icon"),
            "output_format": None,
            "llm_model": "m" * 500,
            "llm_provider": "p" * 500,
        },
        "variables": [
            {"id": "v1", "name": "TOKEN", "value": 12345, "is_secret": True},
            {"id": "v2", "key": "k" * _limit(Variable, "name")},
        ],
        "custom_tools": [{"name": None}, {"name": "Lookup", "icon_url": "u" * _limit(CustomTool, "icon_url")}],
        "mcp_servers": [{"name": "Docs", "transport": "stdio", "command": None}],
        "assignments": [{"name": "Digest", "prompt": "Go.", "description": "", "delay": None}],
    }

    assert _require_importable(bundle) is bundle["agent"]


def test_import_checks_only_what_it_writes():
    """A variable without an ``id`` is skipped by the import, and the legacy
    ``key`` is not read when ``name`` is set: neither is a reason to refuse a
    bundle that imports today."""
    from app.api.v1.agent_import import _require_importable

    too_long = "x" * (_limit(Variable, "name") + 1)
    bundle = _bundle(
        variables=[
            {"name": too_long, "value": 5, "description": ["not written"]},
            {"id": "", "key": too_long},
            {"id": "v1", "name": "REGION", "key": too_long},
            {"id": "v2", "name": "", "key": "LEGACY", "value": "eu"},
        ]
    )

    assert _require_importable(bundle) is bundle["agent"]


def test_the_assignment_length_rule_is_still_checked_first():
    """The length refusal keeps its place before every other check: a bundle with
    several problems is still refused for its assignment's length first."""
    from app.api.v1.agent_import import _require_importable

    bundle = _bundle(
        custom_tools=[{"name": "Lookup", "icon_url": "u" * (_limit(CustomTool, "icon_url") + 1)}],
        assignments=[{"name": "n" * (_limit(Assignment, "name") + 1), "prompt": "Go."}],
    )

    with pytest.raises(HTTPException) as exc_info:
        _require_importable(bundle)
    assert exc_info.value.detail == (
        f"Imported assignment #1: name must be at most {_limit(Assignment, 'name')} "
        f"characters (got {_limit(Assignment, 'name') + 1})."
    )


def test_the_first_refusal_is_the_one_the_import_stops_at():
    """The sections are checked in the order the import writes them, with the
    entry reader's own refusal for a name, so the problem named is the one the
    import itself would stop at."""
    from app.api.v1.agent_import import _require_importable

    bundle = _bundle(
        custom_tools=[{"name": 5}],
        skills=[{"name": "Summaries", "description": 7}],
        agent={"name": "Imported agent", "icon": 3},
    )

    with pytest.raises(HTTPException) as exc_info:
        _require_importable(bundle)
    assert exc_info.value.detail == "Imported custom tool entry 'name' must be a string, got int."


def test_every_text_column_an_import_writes_is_checked():
    """A ``String`` / ``Text`` column added to a model the import writes must be
    checked here, or named as one the import never writes as sent."""
    from sqlalchemy import String

    from app.api.v1 import agent_import
    from app.models.agent_copy import AGENT_BUNDLE_PORTABLE_FIELDS
    from app.models.agent_skill import AgentSkill
    from app.models.assignment import ASSIGNMENT_BOUNDED_TEXT_FIELDS
    from app.models.prompt import Prompt

    def text_columns(model) -> set[str]:
        return {c.name for c in model.__table__.c if isinstance(c.type, String)}

    agent_text = text_columns(Agent) & set(AGENT_BUNDLE_PORTABLE_FIELDS)
    # The model fields are never written as sent: the model is validated, and
    # the source normalised to a known value or none.
    assert agent_text == set(agent_import._AGENT_TEXT_FIELDS) | {
        "llm_provider",
        "llm_model",
        "llm_model_source",
    }

    sections = {model: set(keys) for _section, _label, model, keys in agent_import._BUNDLE_TEXT_FIELDS}
    unchecked = {model: set(reasons) for model, reasons in agent_import._UNCHECKED_TEXT_COLUMNS.items()}
    for model in (Variable, CustomTool, MCPServer, AgentSkill, Prompt):
        assert text_columns(model) == sections[model] | unchecked.get(model, set()), model.__name__
    assignment_checked = set(agent_import._ASSIGNMENT_TEXT_FIELDS) | set(ASSIGNMENT_BOUNDED_TEXT_FIELDS)
    assert text_columns(Assignment) == assignment_checked | unchecked[Assignment]

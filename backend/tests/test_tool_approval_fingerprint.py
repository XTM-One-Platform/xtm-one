"""Unit tests for the standing-approval fingerprint.

The fingerprint draws the line between a cosmetic tool edit and a material one.
Get it wrong in one direction and users re-approve constantly for no reason; get
it wrong in the other and a tool can be rewritten under consent granted against
an earlier version, which is the privilege-escalation path this feature exists
to close.
"""

from app.tools.approval import fingerprint

SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "What to delete"},
        "cascade": {"type": "boolean"},
    },
    "required": ["target"],
}


def test_identical_schema_yields_identical_digest():
    assert fingerprint(SCHEMA) == fingerprint(dict(SCHEMA))


def test_key_order_does_not_change_the_digest():
    """Dicts built in a different order are the same schema."""
    reordered = {
        "required": ["target"],
        "properties": {
            "cascade": {"type": "boolean"},
            "target": {"description": "What to delete", "type": "string"},
        },
        "type": "object",
    }
    assert fingerprint(reordered) == fingerprint(SCHEMA)


def test_adding_a_parameter_changes_the_digest():
    """The material case: a new argument means a different call shape."""
    widened = {
        **SCHEMA,
        "properties": {**SCHEMA["properties"], "force": {"type": "boolean"}},
    }
    assert fingerprint(widened) != fingerprint(SCHEMA)


def test_changing_a_parameter_type_changes_the_digest():
    narrowed = {
        **SCHEMA,
        "properties": {**SCHEMA["properties"], "cascade": {"type": "string"}},
    }
    assert fingerprint(narrowed) != fingerprint(SCHEMA)


def test_changing_required_fields_changes_the_digest():
    more_required = {**SCHEMA, "required": ["target", "cascade"]}
    assert fingerprint(more_required) != fingerprint(SCHEMA)


def test_empty_and_missing_schema_are_stable():
    """A tool with no parameters must still fingerprint deterministically."""
    assert fingerprint(None) == fingerprint({})
    assert fingerprint(None) == fingerprint(None)


def test_digest_is_hex_sha256():
    digest = fingerprint(SCHEMA)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_argument_descriptions_are_part_of_the_shape():
    """A description *inside* the schema is shown to the approver, so it counts.

    This is distinct from the tool's own top-level description, which is not
    hashed: an argument's description is what the reviewer reads to decide what
    the value means, so silently rewriting it changes what they consented to.
    """
    reworded = {
        **SCHEMA,
        "properties": {
            **SCHEMA["properties"],
            "target": {"type": "string", "description": "Entity id to permanently erase"},
        },
    }
    assert fingerprint(reworded) != fingerprint(SCHEMA)

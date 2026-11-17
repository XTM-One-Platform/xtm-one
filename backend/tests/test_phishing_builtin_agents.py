"""Contract tests for the OpenAEV phishing content-generation built-ins.

Pins the wiring that makes the two phishing generators usable out of the box:

- ``aev-phishing-email-generator`` and ``aev-phishing-landing-page-generator``
  are stateless, chat-disabled JSON producers reached through their own AEV
  intents. Nothing else in CI would notice if their persona, output schema, or
  intent binding were dropped, so these tests pin the persona resolution, the
  schema shape (including the sample payloads the personas promise to emit),
  the intent-to-vertical mapping, and the memory-off / disable-chat contract.
- Both intents must also surface in ``BUILTIN_INTENT_BINDINGS`` on the ``aev``
  vertical and carry a description in ``_INTENT_DESCRIPTIONS`` so the Intents
  admin page shows a meaningful capability rather than a bare identifier.
"""

from __future__ import annotations

import jsonschema
import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.seed import (
    _INTENT_DESCRIPTIONS,
    BUILTIN_INTENT_BINDINGS,
    _resolve_seed_content,
    _resolve_seed_schema,
)

_EMAIL_SLUG = "aev-phishing-email-generator"
_LANDING_SLUG = "aev-phishing-landing-page-generator"
_EMAIL_INTENT = "aev.phishing_email_html_generator"
_LANDING_INTENT = "aev.phishing_landing_page_html_generator"


def _agent(slug: str) -> dict:
    for agent in BUILTIN_AGENTS:
        if agent.get("slug") == slug:
            return agent
    raise AssertionError(f"built-in agent {slug!r} not found in BUILTIN_AGENTS")


@pytest.fixture(scope="module")
def email_agent() -> dict:
    return _agent(_EMAIL_SLUG)


@pytest.fixture(scope="module")
def landing_agent() -> dict:
    return _agent(_LANDING_SLUG)


class TestPhishingEmailGenerator:
    def test_persona_resolves_from_seed_data(self, email_agent):
        persona = _resolve_seed_content(
            "agent_persona", email_agent.get("persona", _EMAIL_SLUG)
        )
        assert persona, (
            f"{_EMAIL_SLUG} persona must resolve to a seed file under "
            "seed_data/agent_persona/"
        )
        # The per-recipient tracking placeholder is the reason this agent is
        # useful inside an OpenAEV campaign.
        assert "{{phishing_url}}" in persona

    def test_is_a_stateless_json_producer(self, email_agent):
        assert email_agent["disable_chat"] is True
        assert email_agent["output_format"] == "json"
        assert email_agent["max_iterations"] == 5
        assert email_agent["builtin_tool_ids"] == []
        assert email_agent["integration_template_bindings"] == []
        assert email_agent["tags"] == ["builtin", "openaev", "phishing"]

    def test_all_memory_tiers_disabled(self, email_agent):
        for field in (
            "memory_short_term_enabled",
            "memory_medium_term_enabled",
            "memory_persistent_enabled",
            "memory_user_enabled",
        ):
            assert email_agent.get(field) is False, (
                f"{_EMAIL_SLUG} must disable {field}: it is a stateless "
                "generation step"
            )

    def test_supported_intent_maps_to_aev_vertical(self, email_agent):
        assert email_agent["supported_intents"] == {_EMAIL_INTENT: {"aev": 0}}

    def test_output_schema_accepts_a_sample_payload(self, email_agent):
        schema = _resolve_seed_schema(email_agent["output_schema"])
        assert schema is not None
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {"subject", "html_body"}
        payload = {
            "subject": "Action required: verify your account",
            "html_body": (
                "<p>Please verify your account.</p>"
                '<p><a href="{{phishing_url}}">Verify</a></p>'
            ),
            "text_body": "Please verify your account: {{phishing_url}}",
        }
        jsonschema.Draft202012Validator(schema).validate(payload)

    def test_output_schema_requires_html_body(self, email_agent):
        schema = _resolve_seed_schema(email_agent["output_schema"])
        errors = list(
            jsonschema.Draft202012Validator(schema).iter_errors(
                {"subject": "Only a subject"}
            )
        )
        assert errors, "html_body must be required by the email output schema"

    def test_output_schema_enforces_tracking_placeholder(self, email_agent):
        # Without {{phishing_url}} OpenAEV cannot inject the per-recipient
        # tracking link, so the schema must reject bodies that omit it.
        schema = _resolve_seed_schema(email_agent["output_schema"])
        validator = jsonschema.Draft202012Validator(schema)
        assert list(
            validator.iter_errors(
                {"subject": "Hi", "html_body": "<p>No link here</p>"}
            )
        ), "html_body without {{phishing_url}} must fail validation"
        # A present text_body must also carry the placeholder.
        assert list(
            validator.iter_errors(
                {
                    "subject": "Hi",
                    "html_body": '<a href="{{phishing_url}}">Verify</a>',
                    "text_body": "No link here",
                }
            )
        ), "text_body without {{phishing_url}} must fail validation when present"


class TestPhishingLandingPageGenerator:
    def test_persona_resolves_from_seed_data(self, landing_agent):
        persona = _resolve_seed_content(
            "agent_persona", landing_agent.get("persona", _LANDING_SLUG)
        )
        assert persona
        # A credential-capture form is the point of a landing page simulation.
        assert 'method="post"' in persona

    def test_is_a_stateless_json_producer(self, landing_agent):
        assert landing_agent["disable_chat"] is True
        assert landing_agent["output_format"] == "json"
        assert landing_agent["max_iterations"] == 5
        assert landing_agent["builtin_tool_ids"] == []
        assert landing_agent["integration_template_bindings"] == []
        assert landing_agent["tags"] == ["builtin", "openaev", "phishing"]

    def test_all_memory_tiers_disabled(self, landing_agent):
        for field in (
            "memory_short_term_enabled",
            "memory_medium_term_enabled",
            "memory_persistent_enabled",
            "memory_user_enabled",
        ):
            assert landing_agent.get(field) is False, (
                f"{_LANDING_SLUG} must disable {field}: it is a stateless "
                "generation step"
            )

    def test_supported_intent_maps_to_aev_vertical(self, landing_agent):
        assert landing_agent["supported_intents"] == {
            _LANDING_INTENT: {"aev": 0}
        }

    def test_output_schema_accepts_a_sample_payload(self, landing_agent):
        schema = _resolve_seed_schema(landing_agent["output_schema"])
        assert schema is not None
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {"html"}
        payload = {
            "html": (
                '<form method="post" action="#">'
                '<input name="username" type="text" />'
                '<input name="password" type="password" />'
                "<button type=\"submit\">Sign in</button></form>"
            ),
            "css": ".login-card{max-width:360px}",
        }
        jsonschema.Draft202012Validator(schema).validate(payload)

    def test_output_schema_requires_html(self, landing_agent):
        schema = _resolve_seed_schema(landing_agent["output_schema"])
        errors = list(
            jsonschema.Draft202012Validator(schema).iter_errors(
                {"css": ".x{}"}
            )
        )
        assert errors, "html must be required by the landing-page output schema"


class TestPhishingIntentWiring:
    @pytest.mark.parametrize("intent", [_EMAIL_INTENT, _LANDING_INTENT])
    def test_intent_has_a_description(self, intent):
        assert _INTENT_DESCRIPTIONS.get(intent), (
            f"{intent} must have a description in _INTENT_DESCRIPTIONS so the "
            "Intents admin page shows a capability, not a bare identifier"
        )

    @pytest.mark.parametrize(
        "intent,slug",
        [(_EMAIL_INTENT, _EMAIL_SLUG), (_LANDING_INTENT, _LANDING_SLUG)],
    )
    def test_intent_binding_present_on_aev_vertical(self, intent, slug):
        rows = [
            row
            for row in BUILTIN_INTENT_BINDINGS
            if row["intent_name"] == intent
        ]
        assert rows, f"{intent} must appear in BUILTIN_INTENT_BINDINGS"
        assert all(row["agent_slug"] == slug for row in rows)
        assert all(row["vertical"] == "aev" for row in rows)
        assert all(row["priority"] == 0 for row in rows)
        assert all(row["intent_description"] for row in rows)

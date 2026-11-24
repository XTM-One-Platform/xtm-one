"""Every model refusing the user's input is an input rejection, not an outage (#1900).

An image over 8,000 px was refused by Claude through the gateway and direct, by
GPT-6 Astra and by Gemini; the chat said "All configured AI providers failed
... Please try again", and the retry failed the same way. The errors below are
the shapes logged on Copilot prod.
"""

from __future__ import annotations

import anthropic
import httpx
import openai
from google.genai import errors as genai_errors

from app.api.v1.chat import _friendly_error_message
from app.llm.router import AllProvidersFailedError, input_rejection
from app.services.failure_causes import FailureCause, classify_run_failure

_IMAGE_TOO_LARGE = "At least one of the image dimensions exceed max allowed size: 8000 pixels"


def _gateway_refusal(upstream: str = f"messages.0.content.1.image.source.base64.data: {_IMAGE_TOO_LARGE}"):
    body = {
        "code": "provider_error",
        "message": f"provider error: upstream returned status 400 Bad Request: {upstream}",
        "retryable": False,
    }
    request = httpx.Request("POST", "https://gateway.example/v1/chat/completions")
    response = httpx.Response(502, json={"error": body}, request=request)
    return openai.APIStatusError(f"Error code: 502 - {{'error': {body}}}", response=response, body=body)


def _anthropic_refusal(message: str = f"messages.0.content.0.image.source.base64.data: {_IMAGE_TOO_LARGE}"):
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(400, json=body, request=request)
    return anthropic.BadRequestError(f"Error code: 400 - {body}", response=response, body=body)


def _openai_stream_refusal():
    message = "input_image image_url could not parse image: Memory limit exceeded"
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    # A stream's error event: no status, and the prod log did not show its type.
    return openai.APIError(message, request, body={"message": message})


def _gemini_refusal():
    inner = (
        '{\n  "error": {\n    "code": 400,\n    "message": "Unable to process input image. Please retry or '
        'report in https://developers.generativeai.google/guide/troubleshooting",\n'
        '    "status": "INVALID_ARGUMENT"\n  }\n}\n'
    )
    return genai_errors.ClientError(400, {"message": inner, "status": "Bad Request"})


def _prod_chain() -> AllProvidersFailedError:
    return AllProvidersFailedError(
        [
            ("filigran_gateway", _gateway_refusal()),
            ("openai", _openai_stream_refusal()),
            ("anthropic", _anthropic_refusal()),
            ("gemini", _gemini_refusal()),
        ],
        tried=[(None, "claude-opus-5-5"), (None, "gpt-6-astra"), (None, "claude-opus-4-6"), (None, "gemini-4-argon")],
    )


def test_every_model_refusing_the_input_is_an_input_rejection_naming_the_limit():
    error = _prod_chain()

    assert input_rejection(error) == _IMAGE_TOO_LARGE
    classification = classify_run_failure(error)
    assert classification.cause is FailureCause.MODEL_REJECTED_REQUEST
    assert len(classification.legs) == 4
    assert "Please try again" not in str(error)
    assert "8000 pixels" in str(error)

    message = _friendly_error_message(error)
    assert "8000 pixels" in message
    assert "try again" not in message.lower()
    assert "retry" not in message.lower()


def test_the_provider_wording_is_quoted_without_its_retry_advice():
    error = AllProvidersFailedError([("gemini", _gemini_refusal()), ("openai", _openai_stream_refusal())])

    reason = input_rejection(error)

    assert reason == "Unable to process input image"


def test_one_leg_failing_otherwise_keeps_the_provider_failure():
    overloaded = anthropic.InternalServerError(
        "Error code: 500 - overloaded",
        response=httpx.Response(500, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")),
        body=None,
    )
    error = AllProvidersFailedError([("filigran_gateway", _gateway_refusal()), ("anthropic", overloaded)])

    assert input_rejection(error) is None
    assert classify_run_failure(error).cause is FailureCause.ALL_PROVIDERS_FAILED
    assert "Please try again" in _friendly_error_message(error)


def test_a_refused_parameter_is_not_an_input_rejection():
    error = AllProvidersFailedError(
        [
            ("anthropic", _anthropic_refusal("temperature: Unsupported parameter for this model")),
            ("filigran_gateway", _gateway_refusal("tools.0.input_schema: JSON schema is invalid")),
        ]
    )

    assert input_rejection(error) is None
    assert classify_run_failure(error).cause is FailureCause.ALL_PROVIDERS_FAILED


def test_a_single_model_refusing_the_input_is_worded_the_same_direct_and_through_the_gateway():
    for exc in (_anthropic_refusal(), _gateway_refusal()):
        assert input_rejection(exc) == _IMAGE_TOO_LARGE
        assert classify_run_failure(exc).cause is FailureCause.MODEL_REJECTED_REQUEST
        message = _friendly_error_message(exc)
        assert "8000 pixels" in message
        assert "try again" not in message.lower()


def test_a_long_gemini_refusal_is_unwrapped_whole():
    padding = ", ".join(f'{{"field": "contents[{i}].parts"}}' for i in range(20))
    inner = (
        f'{{"error": {{"code": 400, "message": "Unable to process input image", "status": "INVALID_ARGUMENT", '
        f'"details": [{padding}]}}}}'
    )
    assert len(inner) > 400

    assert input_rejection(genai_errors.ClientError(400, {"message": inner, "status": "Bad Request"})) == (
        "Unable to process input image"
    )


def test_a_tool_schema_path_naming_a_medium_is_not_the_input():
    exc = _anthropic_refusal("tools.3.custom.input_schema.properties.document: JSON schema is invalid")

    assert input_rejection(exc) is None


def test_a_bedrock_refusal_of_the_image_is_an_input_rejection():
    from botocore.exceptions import ClientError

    exc = ClientError(
        {
            "Error": {"Code": "ValidationException", "Message": f"messages.0.content.1.image: {_IMAGE_TOO_LARGE}"},
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "ConverseStream",
    )

    assert input_rejection(exc) == _IMAGE_TOO_LARGE


def test_a_gateway_input_refusal_is_not_an_outage_for_billing_or_the_platform_api():
    from app.api.v1.platform import classify_platform_turn_failure
    from app.llm.router import is_provider_unavailable_error

    for exc in (_gateway_refusal(), _prod_chain()):
        assert not is_provider_unavailable_error(exc)
        assert classify_platform_turn_failure(exc).retryable is False
    # Answered as a direct refusal is: 500 alone, 502 at the end of a chain.
    direct = classify_platform_turn_failure(_anthropic_refusal())
    assert classify_platform_turn_failure(_gateway_refusal()).status_code == direct.status_code
    assert classify_platform_turn_failure(_prod_chain()).status_code == 502

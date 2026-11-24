"""Every model refusing a turn's files is an input rejection, not an outage (#1900).

An image over 8,000 px was refused by Claude through the gateway and direct, by
GPT-6 Astra and by Gemini; the chat said "All configured AI providers failed
... Please try again", and the retry failed the same way. The errors below are
the shapes logged on Copilot prod.
"""

from __future__ import annotations

import anthropic
import httpx
import openai
import pytest
from google.genai import errors as genai_errors

from app.api.v1.chat import _friendly_error_message
from app.llm.base import BaseLLMProvider, ChatMessage, FileRef
from app.llm.router import AllProvidersFailedError, FailoverLLMProvider, input_rejection
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


def _openai_refusal(message: str, param: str | None) -> openai.BadRequestError:
    body = {"message": message, "type": "invalid_request_error", "param": param, "code": None}
    response = httpx.Response(400, json={"error": body}, request=httpx.Request("POST", "https://api.openai.com/v1/x"))
    return openai.BadRequestError(f"Error code: 400 - {body}", response=response, body=body)


def _openai_stream_refusal():
    message = "input_image image_url could not parse image: Memory limit exceeded"
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    # A stream's error event: no status, and the prod log did not show its type.
    return openai.APIError(message, request, body={"message": message})


def _gemini_refusal(details: str = ""):
    inner = (
        '{\n  "error": {\n    "code": 400,\n    "message": "Unable to process input image. Please retry or '
        'report in https://developers.generativeai.google/guide/troubleshooting",\n'
        f'    "status": "INVALID_ARGUMENT"{details}\n  }}\n}}\n'
    )
    return genai_errors.ClientError(400, {"message": inner, "status": "Bad Request"})


def _chain(*failures: BaseException, carried_files: bool = True) -> AllProvidersFailedError:
    return AllProvidersFailedError([(type(exc).__module__, exc) for exc in failures], carried_files=carried_files)


def _prod_chain(carried_files: bool = True) -> AllProvidersFailedError:
    return AllProvidersFailedError(
        [
            ("filigran_gateway", _gateway_refusal()),
            ("openai", _openai_stream_refusal()),
            ("anthropic", _anthropic_refusal()),
            ("gemini", _gemini_refusal()),
        ],
        tried=[(None, "claude-opus-5-5"), (None, "gpt-6-astra"), (None, "claude-opus-4-6"), (None, "gemini-4-argon")],
        carried_files=carried_files,
    )


def test_every_model_refusing_a_turn_with_files_is_an_input_rejection_naming_the_limit():
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


def test_the_same_refusals_on_a_turn_without_files_stay_a_provider_failure():
    error = _prod_chain(carried_files=False)

    assert input_rejection(error) is None
    assert classify_run_failure(error).cause is FailureCause.ALL_PROVIDERS_FAILED
    assert "Please try again" in _friendly_error_message(error)


# A refusal naming the image's place in the conversation, with no number in its wording.
_NAMED_NO_LIMIT = "messages.0.content.0.image.source.base64.data: Could not process image"


def test_the_provider_wording_is_quoted_without_its_retry_advice():
    error = _chain(_gemini_refusal(), _openai_stream_refusal(), _anthropic_refusal(_NAMED_NO_LIMIT))

    assert input_rejection(error) == "Unable to process input image"


@pytest.mark.parametrize(
    "refusal",
    [
        _anthropic_refusal("Your credit balance is too low to access the Anthropic API."),
        _anthropic_refusal("Invalid request"),
        _gateway_refusal("Unsupported parameter: 'temperature' is not supported with this model."),
    ],
    ids=["credit-balance", "bare-invalid", "gateway-param-unnamed"],
)
def test_refusals_that_name_no_field_of_the_conversation_are_not_an_input_rejection(refusal):
    error = _chain(refusal, _gemini_refusal(), _openai_stream_refusal())

    assert input_rejection(error) is None
    assert "Please try again" in _friendly_error_message(error)


def test_a_long_gemini_refusal_is_unwrapped_whole():
    padding = ", ".join(f'{{"field": "contents[{i}].parts"}}' for i in range(20))
    exc = _gemini_refusal(f',\n    "details": [{padding}]')
    assert len(exc.message) > 400

    assert input_rejection(_chain(exc, _anthropic_refusal(_NAMED_NO_LIMIT))) == "Unable to process input image"


def test_one_leg_failing_otherwise_keeps_the_provider_failure():
    overloaded = anthropic.InternalServerError(
        "Error code: 500 - overloaded",
        response=httpx.Response(500, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")),
        body=None,
    )
    error = _chain(_gateway_refusal(), overloaded)

    assert input_rejection(error) is None
    assert classify_run_failure(error).cause is FailureCause.ALL_PROVIDERS_FAILED
    assert "Please try again" in _friendly_error_message(error)


@pytest.mark.parametrize(
    "refusal",
    [
        _anthropic_refusal("tools.3.custom.input_schema.properties.document: JSON schema is invalid"),
        _anthropic_refusal("temperature: range: 0..1"),
        _gateway_refusal("tools.0.input_schema: JSON schema is invalid"),
        _openai_refusal(
            "Invalid schema for function 'search': In context=('properties', 'file'), 'type' is required.",
            "tools[0].function.parameters",
        ),
        _openai_refusal("Invalid 'tools[0].function.name': document is not a valid name.", "tools[0].function.name"),
        _openai_refusal("Unsupported value: 'temperature' does not support 0.2 with this model.", "temperature"),
    ],
    ids=["anthropic-tool", "anthropic-param", "gateway-tool", "openai-schema", "openai-tool-name", "openai-param"],
)
def test_a_refused_tool_or_parameter_is_not_the_input(refusal):
    error = _chain(refusal, refusal)

    assert input_rejection(error) is None
    assert classify_run_failure(error).cause is FailureCause.ALL_PROVIDERS_FAILED
    assert "attachment" not in _friendly_error_message(error)


def test_a_bedrock_refusal_of_the_image_is_an_input_rejection():
    from botocore.exceptions import ClientError

    exc = ClientError(
        {
            "Error": {"Code": "ValidationException", "Message": f"messages.0.content.1.image: {_IMAGE_TOO_LARGE}"},
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "ConverseStream",
    )

    assert input_rejection(_chain(exc)) == _IMAGE_TOO_LARGE


def test_a_gateway_input_refusal_is_not_an_outage_for_billing_or_the_platform_api():
    from app.api.v1.platform import classify_platform_turn_failure
    from app.llm.router import is_provider_unavailable_error

    error = _chain(_anthropic_refusal(), _gateway_refusal())

    assert not is_provider_unavailable_error(error)
    failure = classify_platform_turn_failure(error)
    assert failure.status_code == 502
    assert failure.retryable is False


class _Refusing(BaseLLMProvider):
    context_window = 100_000

    def __init__(self, name: str, exc: BaseException) -> None:
        self.provider_name = name
        self.default_model = name
        self.exc = exc

    async def chat(self, messages, model=None, **kwargs):
        raise self.exc

    async def chat_stream(self, messages, model=None, **kwargs):
        raise self.exc
        yield ""

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
        raise self.exc

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def _instant() -> None:
        return None

    monkeypatch.setattr("app.llm.router._same_provider_backoff", _instant)


@pytest.mark.parametrize("streamed", [False, True], ids=["chat", "chat_stream"])
@pytest.mark.parametrize("with_file", [True, False], ids=["file", "no-file"])
async def test_the_failover_chain_records_whether_the_turn_carried_files(streamed, with_file):
    image = FileRef(file_id="f1", filename="huge.png", content_type="image/png", size=1, s3_object_name="k")
    messages = [ChatMessage(role="user", content="what is this?", files=[image] if with_file else None)]
    wrapper = FailoverLLMProvider(
        primary=_Refusing("anthropic", _anthropic_refusal()),
        fallbacks=[_Refusing("filigran_gateway", _gateway_refusal())],
    )

    with pytest.raises(AllProvidersFailedError) as caught:
        if streamed:
            async for _ in wrapper.chat_stream(messages):
                pass
        else:
            await wrapper.chat(messages)

    assert input_rejection(caught.value) == (_IMAGE_TOO_LARGE if with_file else None)

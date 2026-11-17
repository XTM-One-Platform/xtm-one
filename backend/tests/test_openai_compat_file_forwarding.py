import anyio

from app.llm.base import ChatMessage, FileRef, build_openai_messages_async
from app.llm.file_resolver import file_label


def test_openai_compat_flattens_text_file_fallback_into_user_content():
    # OpenRouter models can ignore text-only OpenAI content-part arrays.
    # Non-image files must therefore be forwarded as plain user text.
    file_ref = FileRef.from_bytes(
        b"Threat actor uses phishing emails and command-and-control infrastructure.",
        filename="report.txt",
        content_type="text/plain",
    )

    messages = [
        ChatMessage(
            role="user",
            content="Extract a STIX bundle from the attached report.",
            files=[file_ref],
        )
    ]

    converted = anyio.run(build_openai_messages_async, messages, "openai_compat")

    assert converted == [
        {
            "role": "user",
            "content": (
                "Extract a STIX bundle from the attached report.\n\n"
                f"{file_label(file_ref)}\n\n"
                "Threat actor uses phishing emails and command-and-control infrastructure."
            ),
        }
    ]


def test_openai_compat_flattens_file_only_message_without_leading_blank():
    # A file-only message (no accompanying user text, content="" or None)
    # must NOT prepend a blank line — or the literal "None" — ahead of the
    # file content when text-only parts are flattened.
    file_ref = FileRef.from_bytes(
        b"Threat actor uses phishing emails and command-and-control infrastructure.",
        filename="report.txt",
        content_type="text/plain",
    )
    expected = (
        f"{file_label(file_ref)}\n\n"
        "Threat actor uses phishing emails and command-and-control infrastructure."
    )

    for empty in ("", None):
        messages = [
            ChatMessage(role="user", content=empty, files=[file_ref])
        ]
        converted = anyio.run(
            build_openai_messages_async, messages, "openai_compat"
        )
        assert converted[0]["content"] == expected, empty
        assert not converted[0]["content"].startswith("\n"), empty


def test_openai_compat_keeps_multimodal_content_when_file_is_image():
    # Image-capable OpenAI-compatible providers still need the native
    # multimodal payload shape so vision models receive the image bytes.
    file_ref = FileRef.from_bytes(
        (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00"
            b"\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
        ),
        filename="indicator.png",
        content_type="image/png",
    )

    messages = [
        ChatMessage(
            role="user",
            content="Describe the attached image.",
            files=[file_ref],
        )
    ]

    converted = anyio.run(build_openai_messages_async, messages, "openai_compat")

    assert converted[0]["role"] == "user"
    assert isinstance(converted[0]["content"], list)
    assert converted[0]["content"][0] == {
        "type": "text",
        "text": "Describe the attached image.",
    }
    assert converted[0]["content"][1]["type"] == "image_url"
    assert converted[0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )

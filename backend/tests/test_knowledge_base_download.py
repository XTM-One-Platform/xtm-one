from app.core.utils import build_content_disposition


def test_build_content_disposition_supports_unicode_filename() -> None:
    header = build_content_disposition('Résumé — "final".md')

    # Response headers must be latin-1 encodable in Starlette/ASGI.
    header.encode("latin-1")

    assert 'filename="R?sum? ? \\"final\\".md"' in header
    assert "filename*=UTF-8''R%C3%A9sum%C3%A9%20%E2%80%94%20%22final%22.md" in header

"""The AI Gateway's error bodies, as the gateway sends them."""


def gateway_refusal_body(provider_message: str | None = None, *, status: str = "400 Bad Request") -> dict:
    """The gateway's 502 body for an upstream 4xx, with the provider's message (ai-gateway#348) when given."""
    message = f"provider error: upstream returned status {status}"
    if provider_message is not None:
        message += f": {provider_message}"
    return {"error": {"message": message, "type": "provider_error", "code": "provider_error", "retryable": False}}

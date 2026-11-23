"""The AI Gateway's error bodies, as the gateway sends them."""


def gateway_refusal_body(provider_message: str | None = None, *, status: str = "400 Bad Request") -> dict:
    """The gateway's 502 body for an upstream 4xx, with the provider's message (ai-gateway#348) when given."""
    message = f"provider error: upstream returned status {status}"
    if provider_message is not None:
        message += f": {provider_message}"
    return {"error": {"message": message, "type": "provider_error", "code": "provider_error", "retryable": False}}


def gateway_throttle_body(*, circuit_open: bool = False, unwrapped: bool = False) -> dict:
    """The gateway's 502 for an upstream throttle, or for the circuit it opened (``CircuitOpen``)."""
    message = "provider anthropic unavailable (circuit open; retry in ~2s)" if circuit_open else "provider throttled"
    error = {
        "message": message, "type": "provider_error", "code": "provider_error",
        "retry_after_seconds": 2, "retryable": True,
    }
    # The OpenAI SDK's ``body`` is the envelope's ``error``.
    return error if unwrapped else {"error": error}

"""Check provider-published capabilities for selected models.

Examples:
    python scripts/check_model_capabilities.py --model openrouter:openai/gpt-5-mini
    ANTHROPIC_API_KEY=... python scripts/check_model_capabilities.py --model anthropic:claude-sonnet-5
    GEMINI_API_KEY=... python scripts/check_model_capabilities.py --model gemini:gemini-3.8-flash
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# Support ``python scripts/check_model_capabilities.py`` from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class CapabilityCheckError(RuntimeError):
    pass


def _get_json(url: str, *, headers: Mapping[str, str] | None = None) -> object:
    if not url.startswith(("http://", "https://")):
        raise CapabilityCheckError(f"refusing non-http(s) URL: {url}")
    request = Request(url, headers=dict(headers or {}))  # noqa: S310 (scheme validated above)
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 (scheme validated above)
            return json.load(response)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise CapabilityCheckError(f"could not fetch {url}: {exc}") from exc


def _load_catalog_model(
    url: str,
    model: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    base_url = url
    page_url = url
    seen_last_ids: set[str] = set()
    while True:
        payload = _get_json(page_url, headers=headers)
        models = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(models, list):
            raise CapabilityCheckError(f"invalid model catalog response from {page_url}")
        metadata = next(
            (
                item
                for item in models
                if isinstance(item, Mapping) and item.get("id") == model
            ),
            None,
        )
        if metadata is not None:
            return metadata
        if not isinstance(payload, Mapping) or not payload.get("has_more"):
            raise CapabilityCheckError(f"model not found in provider catalog: {model}")
        last_id = payload.get("last_id")
        if not last_id:
            raise CapabilityCheckError(
                f"provider catalog has_more without last_id: {page_url}"
            )
        last_id_str = str(last_id)
        if last_id_str in seen_last_ids:
            raise CapabilityCheckError(
                "provider catalog pagination did not advance; repeated after_id cursor"
            )
        seen_last_ids.add(last_id_str)
        separator = "&" if "?" in base_url else "?"
        page_url = f"{base_url}{separator}after_id={quote(last_id_str, safe='')}"


def _load_parameters(provider: str, model: str) -> list[str]:
    if provider == "openrouter":
        metadata = _load_catalog_model(
            "https://openrouter.ai/api/v1/models", model
        )
        return sorted(metadata.get("supported_parameters") or ())

    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise CapabilityCheckError("ANTHROPIC_API_KEY is required")
        metadata = _load_catalog_model(
            "https://api.anthropic.com/v1/models",
            model,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        capabilities = metadata.get("capabilities")
        if not isinstance(capabilities, Mapping):
            raise CapabilityCheckError(
                f"provider did not publish capabilities for model: {model}"
            )
        return sorted(name for name, value in capabilities.items() if value)

    if provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise CapabilityCheckError("GEMINI_API_KEY or GOOGLE_API_KEY is required")
        metadata = _get_json(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{quote(model, safe='')}",
            headers={"x-goog-api-key": api_key},
        )
        if not isinstance(metadata, Mapping):
            raise CapabilityCheckError("invalid Gemini model response")
        parameters = (
            ["temperature"]
            if "temperature" in metadata or "maxTemperature" in metadata
            else []
        )
        if metadata.get("outputTokenLimit"):
            parameters.append("max_output_tokens")
        return parameters

    raise CapabilityCheckError(f"unsupported provider: {provider}")


def _validate_parameters(
    provider: str, model: str, parameters: list[str]
) -> list[str]:
    from app.llm.model_capabilities import (
        OUTPUT_TOKEN_PARAMETERS,
        build_openai_generation_params,
        omit_temperature,
    )

    errors: list[str] = []
    if provider == "openrouter":
        expected = build_openai_generation_params(model, temperature=0.2, max_tokens=1)
        output_parameter = next(
            (parameter for parameter in expected if parameter in OUTPUT_TOKEN_PARAMETERS),
            None,
        )
        if output_parameter is None:
            raise CapabilityCheckError(
                f"{model}: no output-token parameter produced by "
                "build_openai_generation_params; "
                f"got keys {sorted(expected)}"
            )
        if output_parameter not in parameters:
            errors.append(f"static rules require unsupported parameter: {output_parameter}")
    temperature_supported = "temperature" in parameters
    if omit_temperature(model) and temperature_supported:
        errors.append("static rules omit temperature but provider advertises it")
    elif not omit_temperature(model) and not temperature_supported:
        errors.append("static rules send temperature but provider does not advertise it")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        action="append",
        required=True,
        metavar="PROVIDER:MODEL",
        help="Model to check; repeat for multiple models.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when published parameters contradict static model rules.",
    )
    args = parser.parse_args()

    results: list[dict[str, object]] = []
    exit_code = 0
    for target in args.model:
        provider, separator, model = target.partition(":")
        if not separator or not provider or not model:
            results.append({"model": target, "error": "expected PROVIDER:MODEL"})
            exit_code = 1
            continue
        try:
            parameters = _load_parameters(provider, model)
            result: dict[str, object] = {
                "provider": provider,
                "model": model,
                "supported_parameters": parameters,
            }
            if args.check:
                errors = _validate_parameters(provider, model, parameters)
                if errors:
                    result["error"] = "; ".join(errors)
                    exit_code = 1
            results.append(result)
        except CapabilityCheckError as exc:
            results.append({"provider": provider, "model": model, "error": str(exc)})
            exit_code = 1

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for result in results:
            if "error" in result:
                print(
                    f"{result.get('provider', result['model'])}: "
                    f"ERROR: {result['error']}"
                )
                continue
            print(f"{result['provider']} / {result['model']}")
            print(f"  supported_parameters: {', '.join(result['supported_parameters'])}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

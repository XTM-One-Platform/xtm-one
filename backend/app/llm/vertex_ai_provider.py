# SPDX-License-Identifier: Apache-2.0

"""Google Vertex AI (Gemini Enterprise Agent Platform): Veo video generation, never a chat model.

Veo is served by Vertex AI alone: the Gemini API shut its Veo models down. The
google-genai SDK speaks Vertex AI with the calls it speaks the Gemini API with,
so the Veo implementation is the Gemini provider's (``generate_videos``, the
operation polled, its settings relaxed while the model refuses one), on a
client built for Vertex AI. What differs is said here: Vertex AI takes
``generate_audio`` (a shot asked silent is made without its audio track), it
answers with the video's bytes (no Files API), and its refusals name the IAM
permission or the API they miss.

Credential: a service account key, the JSON Google Cloud downloads (IAM >
Service accounts > Keys) or that JSON base64-encoded (``LLM_PROVIDER_VERTEX_AI_API_KEY``
from a secret store), Fernet-encrypted like every key. Its ``project_id``
is the project Veo runs and is billed in; an optional ``location`` member names
the Vertex AI region, ``us-central1`` (the one serving Veo 3.1) otherwise. The
service account needs the Vertex AI User role (``roles/aiplatform.user``) on
that project. The key's token endpoint is pinned to Google's, so a key edited to
name another never sends its signed assertion elsewhere.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import errors as genai_errors

from app.core.utils import format_exception_message
from app.llm.base import BaseLLMProvider
from app.llm.gemini_provider import _VIDEO_TRANSPORT_ERRORS, GeminiProvider
from app.llm.video import VideoGenerationError

#: The region a key names none for: the one Vertex AI serves Veo 3.1 from.
DEFAULT_LOCATION = "us-central1"
#: The token endpoint every key is exchanged at, whatever the key says.
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret
#: A Vertex AI region (``us-central1``, ``europe-west4``), a multi-region (``us``, ``eu``) or ``global``:
#: the location is part of the API host, so nothing else is taken.
_LOCATION = re.compile(r"global|us|eu|[a-z]+-[a-z]+[0-9]+")
_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
_REQUIRED_FIELDS = ("project_id", "private_key", "client_email")


@dataclass(frozen=True)
class VertexCredential:
    """A service account key and the Vertex AI region it is used in."""

    info: dict[str, Any]
    location: str

    @property
    def project(self) -> str:
        return str(self.info["project_id"])

    @property
    def client_email(self) -> str:
        return str(self.info["client_email"])


def _key_text(text: str) -> str:
    """The key's JSON: as pasted, or decoded from the base64 a secret store or a CI variable holds it as."""
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped
    try:
        return base64.b64decode(stripped, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return stripped


def parse_vertex_credential(text: str) -> VertexCredential:
    """The service account key of a Vertex AI provider, or a :class:`VideoGenerationError` saying what is wrong.

    The JSON may hold raw line breaks inside the private key (an ``.env``
    loader expands its ``\\n``). Never names a value of the key: only which
    fields are missing or wrong.
    """
    try:
        info = json.loads(_key_text(text), strict=False)
    except ValueError:
        raise VideoGenerationError(
            "The Vertex AI credential is not a service account key: paste the JSON key Google Cloud "
            "downloads (IAM > Service accounts > Keys > Add key > JSON)."
        ) from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise VideoGenerationError('The Vertex AI credential is not a service account key (its "type" is not "service_account").')
    missing = [field for field in _REQUIRED_FIELDS if not str(info.get(field) or "").strip()]
    if missing:
        raise VideoGenerationError(f"The service account key has no {', '.join(missing)}.")
    if (info.get("universe_domain") or "googleapis.com") != "googleapis.com":
        raise VideoGenerationError("The service account key is not for Google Cloud (its universe_domain is not googleapis.com).")
    location = str(info.pop("location", None) or DEFAULT_LOCATION).strip().lower()
    if not _LOCATION.fullmatch(location):
        raise VideoGenerationError(f'"{location[:40]}" is not a Vertex AI region (for example {DEFAULT_LOCATION}).')
    info["token_uri"] = GOOGLE_TOKEN_URI
    return VertexCredential(info=info, location=location)


def _reasons(exc: genai_errors.APIError) -> set[str]:
    """The ``ErrorInfo`` reasons of a Google API refusal (``IAM_PERMISSION_DENIED``, ``SERVICE_DISABLED``)."""
    body = exc.details if isinstance(exc.details, dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    return {
        str(item.get("reason"))
        for item in error.get("details") or []
        if isinstance(item, dict) and item.get("reason")
    }


class VertexAIProvider(GeminiProvider):
    """Veo on Vertex AI, authenticated with a service account key."""

    provider_name = "vertex_ai"
    only_capability = "video"
    default_model = None
    default_speech_model = None
    default_music_model = None
    default_video_model = "veo-3.1-generate-001"
    _VIDEO_AUDIO_FIELD = True

    def __init__(self, api_key: str, **kwargs):
        del kwargs
        from google.oauth2 import service_account

        credential = parse_vertex_credential(api_key)
        try:
            credentials = service_account.Credentials.from_service_account_info(credential.info, scopes=_SCOPES)
        except (ValueError, KeyError):
            raise VideoGenerationError("The service account key's private key could not be read.") from None
        self.project = credential.project
        self.location = credential.location
        self.service_account = credential.client_email
        # No Gemini API key: the Interactions API routes (speech, music, Gemini Omni) are not this provider's.
        self._api_key = ""
        self.client = genai.Client(
            vertexai=True,
            project=self.project,
            location=self.location,
            credentials=credentials,
            http_options={"timeout": 120_000},
        )

    @staticmethod
    def _interactions_video(model: str) -> bool:
        """Veo alone: Gemini Omni's interactions are the Gemini API's."""
        del model
        return False

    def _refusal(self, exc: genai_errors.APIError, asked: str) -> VideoGenerationError:
        """A Vertex AI refusal (HTTP 401 / 403) saying what the project or the service account misses."""
        reasons = _reasons(exc)
        if "SERVICE_DISABLED" in reasons:
            return VideoGenerationError(
                f"The Vertex AI API is not enabled on project {self.project}: enable aiplatform.googleapis.com, then try again."
            )
        if "IAM_PERMISSION_DENIED" in reasons:
            return VideoGenerationError(
                f"{self.service_account} may not {asked} on Vertex AI: grant it the Vertex AI User role "
                f"(roles/aiplatform.user) on project {self.project}."
            )
        if exc.code == 401:
            return VideoGenerationError(
                f"Google refused the service account key of {self.service_account} (deleted or disabled?)."
            )
        return VideoGenerationError(f"Vertex AI refused to {asked} (HTTP {exc.code}): {str(exc.message or exc.status)[:300]}")

    def _video_refusal(self, exc: genai_errors.ClientError, model: str) -> RuntimeError:
        if exc.code in (401, 403):
            return self._refusal(exc, f"generate with {model}")
        return super()._video_refusal(exc, model)

    async def _download_video(self, video, model: str) -> bytes:
        raise VideoGenerationError(f"Vertex AI answered {model}'s video with no bytes to read.")

    async def list_video_models(self) -> list[str]:
        """The Veo models Vertex AI lists in the key's region; a listing that fails says why, never a list of its own."""
        from google.auth.exceptions import GoogleAuthError

        try:
            ids = await self._list_all_model_ids()
        except genai_errors.ClientError as exc:
            if exc.code in (401, 403):
                raise self._refusal(exc, "list the Veo models") from None
            raise VideoGenerationError(
                f"Vertex AI refused to list the models of {self.location} (HTTP {exc.code}): {str(exc.message)[:300]}"
            ) from None
        except GoogleAuthError as exc:
            raise VideoGenerationError(
                f"Google refused the service account key of {self.service_account}: {format_exception_message(exc)[:300]}"
            ) from None
        except genai_errors.ServerError as exc:
            raise VideoGenerationError(f"Vertex AI is unavailable (HTTP {exc.code}): try again.") from None
        except _VIDEO_TRANSPORT_ERRORS as exc:
            raise VideoGenerationError(f"Vertex AI could not be reached: {format_exception_message(exc)[:300]}") from None
        return sorted({model for model in ids if model.startswith("veo-")}, reverse=True)

    async def list_models(self) -> list[str]:
        """No chat model: the video models are :meth:`list_video_models`."""
        return []

    async def list_model_context_windows(self) -> dict[str, int] | None:
        return None

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        del text, model
        raise NotImplementedError("Vertex AI is configured here for video generation only.")

    # The Gemini provider's other capabilities are not this provider's: its speech, music and
    # Interactions routes take a Gemini API key. Declared as the base provider's, so whoever
    # asks what a provider type implements (``env_capability_defaults``, the deletion
    # preview) is answered that it does not.
    list_embedding_models = BaseLLMProvider.list_embedding_models
    list_image_models = BaseLLMProvider.list_image_models
    generate_image = BaseLLMProvider.generate_image
    edit_image = BaseLLMProvider.edit_image
    image_reference_limit = BaseLLMProvider.image_reference_limit
    list_speech_models = BaseLLMProvider.list_speech_models
    list_voices = BaseLLMProvider.list_voices
    synthesize_speech = BaseLLMProvider.synthesize_speech
    list_music_models = BaseLLMProvider.list_music_models
    generate_music = BaseLLMProvider.generate_music

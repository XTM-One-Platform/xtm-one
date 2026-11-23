# LLM providers: layout, adding one, retries and decision models

What each module in `backend/app/llm/` is, the supported providers and their credentials, how to add a provider, the shared retry policy and the decision-model (CLM) endpoint; the rules of this area live in `backend/app/llm/AGENTS.md`.

## Architecture

```
llm/
├── AGENTS.md                  ← Rules of this area
├── MODEL_CAPABILITIES.md      ← How the model-parameter rules work and how to check them against a provider
├── __init__.py
├── agent_model.py             ← The provider / model an agent runs on: its own or the platform routing model (`agent_model_selection()`)
├── anthropic_provider.py      ← Anthropic Claude (raw event streams, prompt caching, thinking off, declines)
├── anthropic_thinking_replay.py ← Guarded replay of Anthropic thinking blocks within a tool round
├── attachment_cache.py        ← Process-wide LRU memos of per-request attachment work (downloads, digests, PDF pages, prepared images)
├── attribution.py             ← Which provider config served a call, as recorded on traces (failover, downgrade)
├── azure_openai_provider.py   ← Azure OpenAI Service (OpenAI-compatible, `AsyncAzureOpenAI`)
├── base.py                    ← `BaseLLMProvider`, `ChatMessage` / `ChatResponse`, `retry_llm_call`, OpenAI-format message builders, image / SVG preparation, `StreamUsageSink`, provider-extra counters
├── bedrock_provider.py        ← AWS Bedrock (boto3, Converse / ConverseStream)
├── context_window_scan.py     ← Settings > AI Models scan: provider listing, then catalog, then family
├── custom_openai_provider.py  ← Custom OpenAI-compatible endpoint (vLLM, TGI, LocalAI, LM Studio, ...)
├── cut_answers.py             ← The one check and the one retry for a utility answer cut at `max_tokens`
├── decisions.py               ← Decision models (CLMs): typed choice questions on a provider's decision endpoint
├── elevenlabs_provider.py     ← ElevenLabs (provider type `elevenlabs`): voices, music (Eleven Music), video and images over its Image & Video API, never chat
├── env_capability_defaults.py ← The video, speech, music and decision models pinned from the environment (`DEFAULT_<ROLE>_*`, one `CapabilityRole` each)
├── env_models.py              ← Model catalogs of env-seeded providers (`LLM_PROVIDER_{PROVIDER}_MODELS`)
├── file_resolver.py           ← Attachments: native or extracted text, one budget per request (`resolve_payload_files`)
├── gateway_provider.py        ← Filigran AI Gateway (OpenAI-compatible, XTM Suite JWT auth per request)
├── gemini_provider.py         ← Google Gemini (`google-genai`)
├── identity.py                ← Caller identity (`ContextVar`) for the gateway's per-request JWT: a job binds its person (`bind_llm_caller`, an active user), else the service identity (the admin), looked up when a process the bootstrap did not run starts (`load_service_identity`) and kept current by the worker (`keep_service_identity_current`)
├── images.py                  ← Image helpers shared by the providers (`nearest_aspect_ratio`, `accepted_image`, `ImageGenerationError`), a drawing asked again through a rate limit and its failure said (`draw_retrying`, `failure_message`)
├── kling_provider.py          ← Kling AI (provider type `kling`): video only, its own job API
├── litellm_provider.py        ← LiteLLM proxy
├── lmstudio_provider.py       ← LM Studio on the customer's network, fed only by its integration and reached through its relay group
├── mistral_provider.py        ← Mistral AI
├── model_capabilities.py      ← Per-model request-parameter rules (`MODEL_PARAMETER_RULES`)
├── model_limits.py            ← Context window / max output resolution; `parse_token_count()` (128K, 1.05M)
├── music.py                   ← Music generation: request / result types, `MusicProviderBusyError` / `MusicGenerationError`
├── ollama_provider.py         ← Ollama, self-hosted models
├── openai_compat.py           ← `OpenAICompatibleProvider` base class, `is_retryable_openai`, reasoning-effort normalization
├── openai_provider.py         ← OpenAI (chat, embeddings, images, Decisions API)
├── openrouter_provider.py     ← OpenRouter, one key for many vendors (System One decision endpoint)
├── provider_access.py         ← Who may use a provider: grants, exclusions, `provider_routing()`, `apply_provider_access()`
├── provider_errors.py         ← What a media provider's failed call says: HTTP status, its own words, the wait it asked for (Google `RetryInfo` included)
├── router.py                  ← Provider + model resolution, `FailoverLLMProvider`, embedding / image / speech / video / music provider, shared constants
├── runway_provider.py         ← Runway (provider type `runway`): video only, its own job API
├── seedance_provider.py       ← Seedance on BytePlus ModelArk (provider type `seedance`): video only, its own job API
├── speech.py                  ← Speech generation: request / result types and the OpenAI speech API mixin (`OpenAISpeechMixin`)
├── tei_embedding_provider.py  ← Embedding-only server (`POST /embed`, provider type `tei`)
├── transcription.py           ← Speech-to-text: the platform's one transcription (chat dictation, the Video Studio's voice checks)
├── video.py                   ← Video generation: request / result types, the OpenAI Videos API mixin, `await_job` / `download_video` / `fetch_signed_media` for the job APIs
├── video_served.py            ← Which video models a provider still serves, as the provider itself answered (Redis, per configuration)
├── vertex_ai_provider.py      ← Google Vertex AI (provider type `vertex_ai`): Veo video only, the Gemini provider's Veo code on a Vertex AI client, a service account key
└── xai_provider.py            ← SpaceXAI Grok
```

## Supported Providers

`PROVIDER_MAP` (`router.py`; `api/v1/settings.py` holds a copy of it) lists 19 provider types. Embeddings and images name the provider's built-in default model (`EMBEDDING_DEFAULTS` / `IMAGE_DEFAULTS`); "admin-selected" means the provider serves them only once an admin picks it and a model in Settings.

| Provider (`PROVIDER_MAP` key) | SDK | Chat, tools, streaming | Embeddings | Images | Default chat model |
|----------|-----|------|------------|--------|---------------|
| **Anthropic** (`anthropic`) | `anthropic` | Yes | No | No | `claude-sonnet-5` |
| **OpenAI** (`openai`) | `openai` | Yes | Yes (`text-embedding-3-large`) | Yes (`gpt-image-1`) | `gpt-4o` |
| **Gemini** (`gemini`) | `google-genai` | Yes | Yes (`text-embedding-004`) | Yes (`imagen-3.0-generate-002`) | `gemini-3.8-flash` |
| **OpenRouter** (`openrouter`) | `openai` | Yes | Admin-selected (`/embeddings`, models from `/embeddings/models`) | No | `anthropic/claude-sonnet-5` |
| **Ollama** (`ollama`) | `openai` | Yes | Yes (`nomic-embed-text`) | No | `llama3.2` |
| **Mistral AI** (`mistral`) | `openai` | Yes | Yes (`mistral-embed`) | No | `mistral-large-latest` |
| **SpaceXAI** (`xai`) | `openai` | Yes | No | No | `grok-4.6` |
| **Azure OpenAI** (`azure_openai`) | `openai` (`AsyncAzureOpenAI`) | Yes | Yes (`text-embedding-3-large`; the model is the deployment name) | Admin-selected (no default: the model is the image deployment's name) | `gpt-4o` |
| **AWS Bedrock** (`bedrock`) | `boto3` (Converse) | Yes | Yes (`amazon.titan-embed-text-v2:0`) | No | `anthropic.claude-sonnet-5` |
| **LiteLLM** (`litellm`) | `openai` | Yes | Admin-selected (`/embeddings`, aliases its `/model/info` declares in mode `embedding`) | No | `gpt-4o` |
| **Custom OpenAI-Compatible** (`custom_openai`) | `openai` (`httpx` for `/models`) | Yes | Admin-selected, explicit model required | No | `default` |
| **LM Studio** (`lmstudio`) | `openai` (routed `httpx` client) | Yes | Admin-selected (models its native `/api/v1/models` types `embedding`) | No | the integration's choice |
| **Embedding server** (`tei`) | `httpx` | No (embedding-only, `EMBEDDING_ONLY_PROVIDERS`) | Admin-selected (no model needed) | No | `tei-embedding` |
| **Filigran AI Gateway** (`filigran_gateway`) | `openai` + `httpx` auth | Yes | Admin-selected (`/v1/embeddings`) | Admin-selected (`/v1/images/generations`), else the listing's `default_for: ["image"]` model (last fallback) | The listing's `default_for: ["chat"]` model, else the `default` alias |
| **ElevenLabs** (`elevenlabs`) | `httpx` | No (`only_capability` `speech`) | No | Admin-selected (Image & Video API) | speech: `eleven_multilingual_v2`, music: `music_v2_5` (video models offered, none built in) |
| **Kling AI** (`kling`) | `httpx` | No (`only_capability` `video`) | No | No | video: `kling-v3` |
| **Runway** (`runway`) | `httpx` | No (`only_capability` `video`) | No | No | video: `gen4.5` |
| **Seedance (ModelArk)** (`seedance`) | `httpx` | No (`only_capability` `video`) | No | No | video: `dreamina-seedance-2-0-260128` |
| **Google Vertex AI** (`vertex_ai`) | `google-genai` (`vertexai=True`) + `google-auth` | No (`only_capability` `video`) | No | No | video: `veo-3.1-generate-001` |

**One-capability providers.** A provider class that serves no chat declares the
capability its credentials are validated for (`only_capability`: `"embedding"`
for TEI, `"speech"` for ElevenLabs, `"video"` for Kling AI, Runway, Seedance and Vertex AI);
it may serve other media too (ElevenLabs composes, films and draws).
`router.CHATLESS_PROVIDERS` is derived from it and keeps them out
of chat selection, failover and the provider-deletion chat routes;
`EMBEDDING_ONLY_PROVIDERS` is its embedding subset. Settings validates such a
provider for its capability whatever flow added it. Never list one by name.
`router.provider_capabilities()` serves it to Settings as a provider's
`capabilities` (`chat` or its one capability, plus `decision` for a
`decisions_api`): a provider that does not chat gets no chat model list.

**Integration-fed only** (`integration_fed_only`, LM Studio): a provider that
runs on what an integration holds. Settings never creates one by hand
(`settings._ensure_new_provider_allowed`, 400; the add-provider picker hides it,
`addableByHand`); the integration's console does, through
`services/integration_llm_provider.py`. Every instance is built with its
config's id (`router.provider_instance`, used wherever a saved config is
instantiated) to read the integration through it.

**Chat catalogues hold chat models.** A provider's chat listing (the Models
panel, `GET /settings/llm-providers/{provider_id}/models`, and add-provider
discovery, `settings._list_chat_models`) keeps `is_chat_capable_model` ids only;
image, video, speech, music and embedding models are offered by
`list_<capability>_models` in their own section. A gateway model that lists
several types is offered by each (`gateway_provider._types_of`), the chat
listing included when it chats. `router.is_image_model`
(catalog `image_generation` only; an uncatalogued id is not one) is how a
provider tells its drawing models apart (Gemini's `gemini-*-image` beside
Imagen, drawn through `generate_content`).

**A capability listing that answers is the truth**: what
`list_<capability>_models` reads from the provider is what it serves the key,
even with none of that capability's models in it. A built-in list stands in
only for a listing that cannot be read (a timeout, a 429, a 5xx); a rejected
key is raised. Gemini's five capability listings go through
`GeminiProvider._capability_listing`. Settings asks the providers again each
time a capability's picker opens (`Configure`), and says so while it reads
(`ModelListRefreshing`).

**Speech** (`speech.py`): `list_speech_models` / `list_voices(model)` /
`synthesize_speech(SpeechRequest)` with a class-level `default_speech_model`
(`router.SPEECH_DEFAULTS`). OpenAI-shaped providers share `OpenAISpeechMixin`
(OpenAI and Azure speak OpenAI's voices; a gateway passes its upstream's voice
through, and the Filigran AI Gateway lists Gemini's prebuilt voices for a Gemini
model and the account's voices for an ElevenLabs one, from `GET /v1/audio/voices`;
no style, no pronunciation dictionary, `load_speech_capabilities` reads an alias's
upstream before the sync checks), Gemini speaks over REST (Interactions API, voice library), ElevenLabs
is its own provider. `router.get_speech_provider()` resolves the caller's
choice, then `PlatformSettings.speech_*`, then a built-in model, over the
caller's `provider_routing` - the chat's read-aloud and the Video Studio
voice-over both speak through it. `speech_takes_style(model)` says whether a
model follows `SpeechRequest.style` (`None` when unknown): the Video Studio
reads it to speak a styled narration with a model that takes it.

**Music** (`music.py`): `list_music_models` / `generate_music(MusicRequest)`
with a class-level `default_music_model` (`router.MUSIC_DEFAULTS`): Eleven Music
on ElevenLabs (`POST /v1/music`, exact length, `force_instrumental`) and Lyria on
Gemini (Interactions API; the length and "no vocals" are written into its
prompt, Lyria 3 Clip always makes 30 s). One request answered with the audio:
no answer, a 429 or a 5xx is `MusicProviderBusyError` (the job asks again),
any other refusal `MusicGenerationError`. The Filigran AI Gateway lists
the `/v1/models` entries typed `music` and composes through
`POST /v1/audio/music`, sent once (SDK retries off, no `continue_from`, an
`X-Request-Id` to trace it): each request is billed, so only a 429, a
`503 gateway_overloaded`, a `502 provider_error` with `error.retryable: true`
and a connect failure are `MusicProviderBusyError`; every other 5xx (a proxy's
504, a non-JSON 502) and a read timeout are permanent. A busy refusal carries
the envelope's `retry_after_seconds` (else `Retry-After`) as
`MusicProviderBusyError.retry_after`, and the AI track holds for it, no attempt
spent; a `429 quota_exceeded` is a permanent `MusicGenerationError`
saying when the quota resets. `router.is_music_model()`
reads the family prefixes (the catalog lists Lyria as `chat` for its text output), and
`is_chat_capable_model()` refuses them. `router.get_music_provider()` resolves
like speech, over `PlatformSettings.music_*` - the Video Studio's AI tracks
(`services/video_studio/music.py`) compose through it. A request may continue a
clip (`MusicRequest.continue_from`; `MusicResult.continues_at` says where the new
music starts): a provider says per model whether it takes one
(`continues_music`, `False` by default; ElevenLabs `music_v2*` through an
uploaded clip and a composition plan), served as `continues` by
`list_music_models_for` - never a model-name check outside the provider.

**Video** (`video.py`): `list_video_models` / `generate_video(VideoRequest,
progress=, on_job=, resume_job=, on_state=)` with a class-level
`default_video_model` (`router.VIDEO_DEFAULTS`). OpenAI-shaped providers share
`OpenAIVideosMixin`, Gemini runs Veo itself (`generate_videos`) and Gemini Omni
(its built-in model since Veo is served by Vertex AI alone) as a
background interaction: `POST /interactions` with `background: true`, the
interaction id as the shot's job, `GET /interactions/{id}` polled through
`video.await_finished_job` until its `model_output` step carries the video
inline (`await_job` keeps answering a URL for the providers that download
one). What Omni takes is the provider's to say: a start frame and up to three
reference images together, each declared in the prompt
(`[# Sources <FIRST_FRAME>@Image1] [# References <IMAGE_REF_0>@Image2]`), no
negative prompt field, and no length field - the length, the single shot, the
sound and what to avoid are written into its prompt, as for Lyria. Omni also
answers in text: the catalog lists it as chat, so it stays in Gemini's chat
listing as well. Google Vertex AI (`vertex_ai`, `VertexAIProvider`) serves Veo
GA: it subclasses the Gemini provider for its Veo operation, on a
`genai.Client(vertexai=True, project=..., location=..., credentials=...)` built
from a service account key (`parse_vertex_credential`: the JSON or it
base64-encoded, an optional `location` taken only as a region name since it
builds the API host, the token endpoint pinned to Google's). It sends
`generate_audio` (`_VIDEO_AUDIO_FIELD`: a silent shot is made without audio),
reads the video from the answer's bytes (`_download_video` refuses an answer
without), says what a 401 / 403 misses (`_refusal`: `IAM_PERMISSION_DENIED` -
`roles/aiplatform.user`, `SERVICE_DISABLED` - the API), lists the Veo models of
its region (`publishers/google/models/` ids; a listing that fails raises, never
a list of its own), and declares the Gemini provider's speech, music, image and
embedding methods as the base provider's: it serves video alone.
`video_model_unavailable(model)`
says why a model its provider retired can no longer make videos (`None`: usable
or not known), checked by `router.get_video_provider()` before it uses one.

**A model the provider no longer serves** (`video_served.py`): a listing
is not always what a provider serves (a catalog of its own, a fallback list).
A provider whose API reads a model without making a video implements
`probe_video_model(model)`, raising its refusal with the HTTP status. Gemini's
reads its listing, else `models.get` when the listing cannot be read: a model
Gemini shut down (the Veo 3.1 previews, 2026-10-22) keeps a record `models.get`
answers while it is no longer listed and its generations answer 404. The
request that creates a video maps its failure through
`video.status_error`, whose 404 is `VideoModelNotServedError` (a job polled or
downloaded passes `about_job=True`: its 404 is the job's). Either 404 records
the model as not served on that configuration (Redis, shared), which the
router's resolution refuses (`VideoModelUnusableError`) and
`video_model_listing` leaves out - from `video_studio_capabilities` and from
`GET /settings/available-video-models` alike (`list_video_models_for`). Never
filter a listing by hand.

**Video job APIs** (`video.py`): ElevenLabs (Image & Video API), Kling AI,
Runway and Seedance on ModelArk each create a job over their own HTTP API, hand
its id to `on_job` at once (Kling's names its endpoint, `image2video:<id>`), and
share `await_job()` (polling, `TransientFailures`, the job's state told to
`on_state` at every poll - `queued` while it waits upstream, the provider's
own word while it runs, `completed`, or the busy state - and a resumed job the
provider no longer knows) and `download_video()` (`fetch_signed_media`: the
signed URL through `web_fetch.fetch_url_bytes` - the outbound policy on every
redirect, the connection pinned, a size cap - and no API key).
Each declares its models' limits as data (lengths, resolutions, negative prompt,
sound, start-frame types) and says what it changed in `VideoResult.notes`; a
start image of a type the provider refuses is re-encoded as PNG
(`images.accepted_image`). `VideoRequest.sound = False` asks a model that
makes its own sound for a silent clip where its API has a switch. A video-only provider validates its key with a free
authenticated read in `list_video_models` (Runway's lists only the models its
tier may run). They declare `default_video_model` and `_VIDEO_PROVIDERS` puts a
chatless one first; ElevenLabs declares none, so its voices account never
becomes the platform's video generator on its own. ElevenLabs draws images
through the same API (`generate_image`, the ratio of the size its model offers
nearest, `images.nearest_aspect_ratio`, shared with Gemini).

### OpenAI-Compatible Base (`openai_compat.py`)

OpenRouter, Ollama, Mistral, SpaceXAI, Azure OpenAI, LiteLLM, Custom OpenAI-Compatible and the Filigran AI Gateway all subclass `OpenAICompatibleProvider`.
It implements `chat()`, `chat_stream()`, `chat_stream_with_tools()` and `list_models()` with the OpenAI SDK configured with
a custom `base_url`; its `embed()` raises `NotImplementedError`. Subclasses set class attributes, parse their credential
and override model listing, and `embed()` / `generate_image()` where their API serves them.

### Credential Formats

| Provider | API Key Format |
|----------|---------------|
| Anthropic | `sk-ant-...` |
| OpenAI | `sk-...` |
| Gemini | `AIza...` |
| OpenRouter | `sk-or-...` |
| Ollama | Server URL (e.g. `http://localhost:11434`) |
| Mistral | API key from console.mistral.ai |
| SpaceXAI | `xai-...` (from console.x.ai) |
| Azure OpenAI | `endpoint\|\|api_key\|\|api_version` |
| AWS Bedrock | `aws_access_key\|\|aws_secret_key\|\|region` |
| LiteLLM | `api_key\|\|base_url` |
| Custom OpenAI-Compatible | `api_key\|\|base_url` |
| LM Studio | none: the config stores `integration`, the URL, token and relay group are the integration's |
| Embedding server (`tei`) | `token\|\|base_url` (`none` / `dummy` for no token) |
| Filigran AI Gateway | `base_url[\|\|target_provider[\|\|token]]` (no token: a per-request XTM Suite JWT) |
| ElevenLabs | `sk_...`, or `sk_...\|\|base_url` for a regional endpoint |
| Kling AI | `api-key-kling-...`, or `key\|\|base_url` (`https://api.klingai.com`) |
| Runway | `key_...`, or `key\|\|base_url` (sent with `X-Runway-Version: 2024-11-06`) |
| Seedance (ModelArk) | ModelArk API key, or `key\|\|base_url` (Volcengine Ark: `https://ark.cn-beijing.volces.com/api/v3`) |
| Google Vertex AI | A service account key, the JSON Google Cloud downloads (or it base64-encoded), with an optional `location` member (`us-central1` otherwise); the service account needs `roles/aiplatform.user` |

## How to Add a New LLM Provider

### Step 1: Create the provider

Create a new file `backend/app/llm/{name}_provider.py` implementing `BaseLLMProvider`:

```python
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse, retry_llm_call
from app.tools.base import ToolCall, ToolDefinition

class MyProvider(BaseLLMProvider):
    provider_name = "my_provider"

    def __init__(self, api_key: str):
        self.client = SomeSDK(api_key=api_key)

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs) -> ChatResponse:
        # Must handle tool definitions and return tool_calls in response
        # Wrap the API call with retry_llm_call() for transient error handling:
        #   response = await retry_llm_call(
        #       lambda: self.client.chat(...),
        #       is_retryable=_is_retryable_my_provider,
        #   )
        # Map the API's stop signal onto ChatResponse.stop_reason on every path
        # (chat_stream sets last_stop_reason): an answer cut at the output budget
        # is "max_tokens", which the agent loop's continuation and every caller
        # refusing a cut answer key on. Compare an SDK enum by value, never
        # through str() - tests/test_llm_stop_reason_max_tokens.py.
        ...

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs):
        # Yield text chunks
        ...

    async def embed(self, text, model=None) -> list[float]:
        # Return embedding vector, or raise NotImplementedError
        ...

    async def list_models(self) -> list[str]:
        # Return available model IDs
        ...
```

### Step 2: Register in router.py

Add to `PROVIDER_MAP` in `backend/app/llm/router.py`; `backend/app/api/v1/settings.py` copies it (`dict(ROUTER_PROVIDER_MAP)`).
Validation in settings.py is dynamic (`if body.provider not in PROVIDER_MAP`) — no hardcoded list.
A provider that serves embeddings implements `embed()` and, to be an implicit fallback, joins `_EMBEDDING_PROVIDERS` and `EMBEDDING_DEFAULTS` in `router.py`; images likewise `generate_image()`, `_IMAGE_PROVIDERS` and `IMAGE_DEFAULTS`. Speech, video and music defaults are class attributes (`default_speech_model` / `default_video_model` / `default_music_model`), read into `SPEECH_DEFAULTS` / `VIDEO_DEFAULTS` / `MUSIC_DEFAULTS`. A provider that serves no chat declares `only_capability`; `CHATLESS_PROVIDERS` and `EMBEDDING_ONLY_PROVIDERS` derive from it.

### Step 3: Update frontend

In `frontend/src/lib/constants.ts`, add to `PROVIDER_DISPLAY_NAMES`: the add-provider picker lists its keys. A provider that serves no chat also joins `SINGLE_CAPABILITY_PROVIDERS` with its `only_capability`.
In `frontend/src/pages/Settings.tsx`, add an embedding- or image-capable provider to `allowedProvidersByMode` (a provider in `SINGLE_CAPABILITY_PROVIDERS` is kept out of `reasoning` by it, and a video one is offered only with the Video Studio on).

### Step 4: Add dependency

Add the SDK with `cd backend && uv add <sdk>==<version>` (writes `pyproject.toml` and `uv.lock`).

## Retry with Exponential Back-off

Every chat provider uses the shared `retry_llm_call()` helper from `base.py` to transparently retry transient API errors without failing the entire assignment run.

| Constant | Default | Description |
|----------|---------|-------------|
| `LLM_MAX_RETRIES` | 5 | Maximum retry attempts |
| `LLM_RETRY_BASE_DELAY` | 2.0 s | Initial delay before first retry |
| `LLM_RETRY_MAX_DELAY` | 60.0 s | Upper-bound delay cap |

Strategy: **full jitter** — `sleep = random(0, min(cap, base * 2^attempt))`. This de-correlates concurrent callers and avoids thundering-herd problems.

Each provider implements a retryable-error predicate that returns `True` for transient errors:
- **Anthropic**: `_is_retryable_anthropic()` — 429 rate limit, 529 overloaded, 500/502/503 server errors, connection failures. A 529 is the SDK's own `OverloadedError` (not an `InternalServerError`), and an `overloaded_error` event mid-stream arrives with the stream's 200, so it is matched on its `error.type`; `chat_stream_with_tools` retries it only while nothing reached its consumer (no answer text, no forwarded thinking - tool calls are only handed over in the final response), `chat_stream` only while opening the stream
- **OpenAI + OpenAI-Compatible** (OpenAI, OpenRouter, Ollama, Mistral, SpaceXAI, Azure OpenAI, LiteLLM, Custom OpenAI, Filigran AI Gateway): shared `is_retryable_openai()` in `openai_compat.py` — 429 rate limit, 500 internal, connection errors, 502/503/529, except an error whose body says `retryable: false` (the gateway's 502 for an upstream 4xx); the gateway client also sends that one to the SDK as `x-should-retry: false`, so the SDK does not re-send it either
- **Gemini**: `_is_retryable_gemini()` — 429/500/502/503/529, keyword matching for "overloaded"/"rate limit"/"quota", network errors
- **Bedrock**: `_is_retryable_bedrock()` — the botocore error code when there is one (throttling, too many requests, service unavailable, internal server, model timeout / not ready, request timeout), else keywords

Non-retryable errors (e.g. 400 bad request, 401 auth) are raised immediately. The one 400 the Anthropic provider does act on - a model refusing `thinking.type.disabled` - is handled by a deterministic branch of its own around `retry_llm_call()`, never by widening the retry policy (see below).

## Decision models (CLMs, `decisions.py`)

A classifier language model (TypeSafe's Jev, Cloudflare's Clef, OpenAI's decision model) answers typed questions about a piece of content with a probability per allowed answer instead of writing text. Providers serve them on an endpoint of their own, so they never go through chat routing or failover:

- `BaseLLMProvider.decisions_api` names the wire format a provider class speaks, `None` (the default) when it serves none: `OpenRouterProvider` and `OllamaProvider` speak TypeSafe's System One (`POST {base}/systemone`, also Clef's), `OpenAIProvider`, `FiligranGatewayProvider` and `CustomOpenAIProvider` the Decisions API (`POST {base}/decisions`). A new provider class that serves one sets the attribute; nothing else changes. `tests/test_llm_decisions.py` pins the list.
- `decide_choice(model=, state=, instructions=, choices=)` asks one *choice* question on the provider's own SDK client (`client.post`, so base URL, key, timeout and SDK retries are the chat calls'), and raises `DecisionsNotSupportedError` without a `decisions_api`.
- The answer is external input: `parse_choice_response` returns a `ChoiceDecision` only for a label the caller offered, keeps the probabilities of offered labels in [0, 1], and raises `DecisionResponseError` for anything else (refusal, missing answer, unknown label). A Workers AI `result` envelope is unwrapped.
- `list_decision_models()` (default `[]`) lists what a provider can report for a picker: Ollama keeps its `/api/tags` models whose `capabilities` contain `decision`; OpenAI, which reports nothing, probes each model of its listing on `/decisions` with a question it refuses before billing (a made-up model id first, as a control) and remembers the answers per config, key and endpoint, one round at a time (`recall_decision_support` / `remember_decision_support` / `claim_decision_round` in `decisions.py`, `specs/028-openai-decision-models`); the Filigran AI Gateway and custom OpenAI-compatible providers, both on OpenAI's `/decisions`, keep the `/v1/models` entries whose `types` (else `type`) include `decision`, so a model that is also chat is listed (`declared_model_types`). A provider may serve decisions without listing them, and a listing may be partial (OpenAI's round): `decision_listing()` returns `(models, whole)` for the flow Classify node, which takes a provider whose listing reports the model while every listing answered whole, or with none the only provider that cannot list (`lists_decision_models` false: OpenRouter), a whole listing without the model ruling its provider out (after the card listing and the platform Decision Model); `decisions_only_when_listed` (the gateway, custom endpoints) keeps a lone such provider from being taken before its listing declares a decision model; Ollama's and the gateway's listings raise when the provider does not answer; `_decision_headers(model)` adds a decision's own headers (the gateway's `X-Target-Provider`).
- `decision_context_window(model)` (default: the admin's `context_windows[model]`, else `None`) is the context a decision model is served with; Ollama falls back to the `num_ctx` of `/api/show`. A decision runner refuses a question over it instead of cutting, so `act_on_page` sizes each question's page to fit (`flow_classifier.decision_context_window()` resolves the provider).
- Two consumers, both through `services/flow_classifier.classify()`: the flow `Classify (CLM)` node, and `act_on_page` (`services/builtin_tools/page_actions.py`) on the platform decision model (`PlatformSettings.decision_model`). `classify` resolves the config itself (a pinned one is refused, never swapped, when it is gone, withheld from the caller by `provider_access` or serves no decision model) and reaches the instance through `router.provider_for_config`. A served model may cap a choice below the APIs' 255 options without saying so (Ollama: 26 for every model, `tev1:4b`: 24), which is why `act_on_page` keeps every question to 16.

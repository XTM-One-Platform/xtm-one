# Live LLM smoke suite (opt-in)

The unit tests of the LLM providers run against mocked SDK clients: they pin what the platform **sends**, never whether the provider **accepts** it. A request shape the real API refuses (an HTTP 400 on a `thinking` form, a dropped thought signature, a parameter a model does not take) passes every mocked test and fails in production. This suite sends the platform's own requests to the real APIs, with tiny prompts and small `max_tokens`: a handful of calls per provider, cents per run.

It drives the platform's own provider classes, built by `app.llm.router.build_provider()` from a config row shaped like the one the settings API stores (the key Fernet-encrypted, so the decrypt path runs too), and builds its tool-loop transcripts the way `app.tools.agent_loop` does (`live_llm_support.append_assistant_turn`).

## It never runs in CI

- Every test carries the `live_llm` marker and is **skipped** unless `LIVE_LLM_SMOKE=1` is set **and** the provider's key is present. The gate is evaluated when the modules are collected, so CI and every normal run collect these tests and skip them without a network call.
- `tests/test_live_llm_gate.py` (which CI does run) pins that gate and fails if any workflow under `.github/workflows/` mentions `LIVE_LLM_SMOKE`.
- Only the exact value `1` turns the suite on, and a provider whose key is not set stays skipped even then.

## Running it

It spends real money (cents per run). Put the key in the environment of the shell that runs the suite - read it without echoing it, never write it to a committed file - and run it from `backend/`. The suite touches no database, Redis or object storage, but point them at unreachable addresses anyway so nothing else can reach your dev data:

```bash
cd backend
export DATABASE_URL=postgresql+asyncpg://nobody:nobody@127.0.0.1:1/none REDIS_URL=redis://127.0.0.1:1 S3_ENDPOINT=127.0.0.1:1 SECRET_KEY=live-smoke-local
read -rs LLM_PROVIDER_ANTHROPIC_API_KEY && export LLM_PROVIDER_ANTHROPIC_API_KEY   # and/or the Gemini / OpenRouter key
LIVE_LLM_SMOKE=1 uv run python -m pytest tests/live -v -rfEs -p no:randomly
unset LLM_PROVIDER_ANTHROPIC_API_KEY
```

PowerShell 7:

```powershell
cd backend
$env:DATABASE_URL = 'postgresql+asyncpg://nobody:nobody@127.0.0.1:1/none'; $env:REDIS_URL = 'redis://127.0.0.1:1'; $env:S3_ENDPOINT = '127.0.0.1:1'; $env:SECRET_KEY = 'live-smoke-local'
$env:LLM_PROVIDER_ANTHROPIC_API_KEY = Read-Host -MaskInput 'Anthropic key'; $env:LIVE_LLM_SMOKE = '1'
try { uv run python -m pytest tests/live -v -rfEs -p no:randomly } finally { Remove-Item Env:LLM_PROVIDER_ANTHROPIC_API_KEY, Env:LIVE_LLM_SMOKE }
```

Do not run it with `-s`: output pytest does not capture bypasses the report scrubber below.

| Variable | Purpose |
|---|---|
| `LIVE_LLM_SMOKE` | `1` turns the suite on. Anything else, or unset, skips every test. |
| `LLM_PROVIDER_ANTHROPIC_API_KEY` | Runs the Anthropic cases (the name the backend's env seed reads). |
| `LLM_PROVIDER_GEMINI_API_KEY` | Runs the Gemini cases. |
| `LLM_PROVIDER_OPENROUTER_API_KEY` | Runs the OpenRouter cases. |
| `LLM_PROVIDER_OPENAI_API_KEY` | Runs the OpenAI speech cases, and hears every speech case back (the platform's speech-to-text): the speech cases need it. |
| `LLM_PROVIDER_ELEVENLABS_API_KEY` | Runs the ElevenLabs speech cases. A run registers a pronunciation dictionary in the account. |
| `LIVE_SPEECH_OPENAI_MODEL` / `LIVE_SPEECH_GEMINI_MODEL` | Speech models (defaults `gpt-4o-mini-tts` / `gemini-3.8-flash-tts`). |
| `LIVE_SPEECH_ELEVENLABS_MODEL` / `LIVE_SPEECH_ELEVENLABS_PHONEME_MODEL` | ElevenLabs models: one read through its pronunciation dictionary, one through IPA phoneme tags (defaults `eleven_multilingual_v2` / `eleven_flash_v2`). |
| `LIVE_LLM_ANTHROPIC_MODEL` | Claude 5 model (default `claude-sonnet-5`). |
| `LIVE_LLM_ANTHROPIC_MYTHOS_MODEL` | Mythos model (default `claude-mythos-5`). |
| `LIVE_LLM_ANTHROPIC_SONNET_5_5_MODEL` | Sonnet 5.5 model (default `claude-sonnet-5-5`). |
| `LIVE_LLM_GEMINI_25_MODEL` / `LIVE_LLM_GEMINI_3_MODEL` | Gemini models (defaults `gemini-2.5-flash` / `gemini-3.8-flash`). |
| `LIVE_LLM_GEMINI_VIDEO_MODEL` | Gemini video model of the one-clip test (default `gemini-omni-1.1-flash`; one 4 s 720p clip per run). |
| `LLM_PROVIDER_VERTEX_AI_API_KEY` | A Google Cloud service account key (its JSON, or that JSON base64-encoded) with the Vertex AI User role, for `test_live_vertex_ai.py`. |
| `LIVE_LLM_VERTEX_VIDEO_MODEL` | Veo model of the Vertex AI one-clip test (default `veo-3.1-fast-generate-001`; one silent 4 s 720p clip per run). |
| `LIVE_LLM_OPENROUTER_MODEL` | OpenRouter reasoning model (default `deepseek/deepseek-r1`). |

Model ids rot: when a default is retired, override it and update the default.

## The cases

| Module | Case | What it asserts |
|---|---|---|
| `test_live_anthropic.py` | Claude 5 streamed turn with an `on_thinking` consumer | The request carries `thinking: {"type": "adaptive", "display": "summarized"}` and no `temperature`; the reasoning text reaches the consumer, no passage of it reaches the answer, and the answer is the right number. |
| | Claude 5 tool loop through `chat_stream_with_tools`, with and without a reasoning consumer | No 400; the first round looks up `alpha` alone, a later round looks up the key only that result named, and the answer is their sum (703) as a number of its own - so every tool result reached the model. |
| | The same loop with `ANTHROPIC_THINKING_REPLAY_ENABLED` on | Skipped until that setting exists; then, when the first round thought before its tool call, the next request replays a thinking block first. |
| | Mythos utility request | One `chat(disable_thinking=True)` request in the lowest-effort form (`thinking: {"type": "adaptive"}` plus the lowest `output_config.effort`): accepted, returns text. When the code under test does not send Mythos that form by itself yet, the test puts the provider in the state it reaches once it has learned the model refuses `thinking.type.disabled`, so `chat()` builds the form. A key without Mythos access (403 / 404) skips. |
| | Sonnet 5.5 utility request | One `chat(disable_thinking=True, reasoning_effort="xhigh")` request: `thinking: {"type": "between_tools"}` alone, `output_config.effort` lowered to `high` (the form is refused above it), no `temperature`; accepted on the first request, returns text. A key without Sonnet 5.5 access (403 / 404) skips. |
| `test_live_gemini.py` | Gemini 2.5 and Gemini 3 streamed turn | The request sets `include_thoughts`; the thought summaries reach `on_thinking` only, and the answer is the right number. |
| | Gemini 2.5 and Gemini 3 tool loop | The same tool-loop checks as Claude 5, and every thought signature returned with a tool call is sent back. |
| `test_live_openrouter.py` | Streamed turn, and a turn with a tool offered | The `reasoning` field reaches `on_thinking`, never `content` (no `<think>` either), and the answer is the right number. |
| `test_live_speech_pronunciation.py` | Every seeded term of the Video Studio pronunciation lexicon spoken by each configured speech model (OpenAI, Gemini, ElevenLabs through its dictionary and through phoneme tags), heard back by OpenAI's speech-to-text | The segment check passes: every term heard as an accepted reading, through the model's strongest mechanism or its retry. A deliberately wrong `say` fails after the same voice, the text rewrite and the platform's voice, naming the term and what was heard; a model honouring the IPA says it right (an SSML phoneme always; `gpt-4o-mini-tts`, sent the IPA in its delivery instruction, when it follows it). Needs ffmpeg. |

## Keys never reach a report

- A provider error fails the test with its class, status, API error type and message only (`live_llm_support.call_api`), never the request, whose headers carry the key.
- `conftest.py` scrubs every live report (the failure text and the captured output) and every log record as it is created, before any handler formats it: the key, its Fernet token, and anything shaped like an Anthropic, OpenRouter or Google key become `[REDACTED]`.
- The request spies record the call arguments, which is the body the platform built: the SDK client holds the key, not the arguments.

## Keeping it honest

`append_assistant_turn` mirrors how `app.tools.agent_loop` appends an assistant turn after a tool call (content, tool calls, `_provider_extra`). When the loop starts carrying another field onto that message, carry it here too, or the suite stops sending what production sends.

## Google Docs review check (opt-in)

`test_live_google_docs.py` runs the Google provider's Docs review tools against the real Drive and Docs APIs, on a scratch Doc kept for the purpose. It appends a uniquely marked sentence, reads it back, comments on it (`add_google_doc_comment` with `quoted_text`), replies, resolves, lists the thread, rehearses an edit (`dry_run`), edits directly, then edits as a suggestion. Its cleanup deletes the comment, rejects the suggestion it made and removes the sentence. It costs nothing, but it writes to that Doc as the token's account.

It is gated like the LLM suite and never runs in CI: every test is skipped unless all three variables are set, and `tests/test_google_doc_review.py` pins that gate and fails if a workflow mentions `LIVE_GOOGLE_DOCS`. The token is registered with the report scrubber, so it never reaches a report.

| Variable | Purpose |
|---|---|
| `LIVE_GOOGLE_DOCS` | `1` turns the check on. |
| `GOOGLE_DOCS_LIVE_ACCESS_TOKEN` | An OAuth access token of the test account granting `https://www.googleapis.com/auth/drive` and `https://www.googleapis.com/auth/documents` (for example from the OAuth 2.0 Playground, with your own OAuth client). It expires after an hour. |
| `GOOGLE_DOCS_LIVE_DOC_ID` | The scratch Doc's file ID. The account must be able to edit it, and its Cloud project must have the Google Docs API enabled. |

```powershell
cd backend
$env:GOOGLE_DOCS_LIVE_ACCESS_TOKEN = Read-Host -MaskInput 'Google access token'; $env:GOOGLE_DOCS_LIVE_DOC_ID = '<scratch doc id>'; $env:LIVE_GOOGLE_DOCS = '1'
try { uv run python -m pytest tests/live/test_live_google_docs.py -v -rfEs -p no:randomly } finally { Remove-Item Env:GOOGLE_DOCS_LIVE_ACCESS_TOKEN, Env:LIVE_GOOGLE_DOCS }
```

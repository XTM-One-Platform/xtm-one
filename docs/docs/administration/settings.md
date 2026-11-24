# Settings

`Settings` is where shared platform behavior is configured. Unlike `My Profile`, this page affects the whole environment rather than just one user.

Some tabs are visible to all users, while others are admin-only.

## The main Settings tabs

The current Settings page can include:

- `Parameters`
- `Organization`
- `AI Models`
- `General Assistant`
- `Channels & Bots`
- `Integrations`
- `Quotas`
- `MCP Endpoint`
- `Security`
- `Observability`
- `Quality`

Non-admin users usually see fewer tabs. That is expected.

## Parameters

`Parameters` is an admin area for platform identity and licensing. From the top, it holds:

- `Enterprise License`: install, update or remove the license. The card shows the organization, type and expiry of the license, whether it comes from the `ENTERPRISE_LICENSE` environment variable or from the database, its `Agentic capacity` (per day, month or year, or `Unlimited`), and its `Sub-Licenses`: which OpenCTI, OpenAEV and OpenCRQ instances it covers (`Global` for all of them).
- `Artificial Intelligence`: whether the platform runs AI at all. `Enabled` and `Disabled` override the deployment default; `Deployment default` follows it. Turning AI off asks for confirmation; runs in progress finish under their own time limit and nothing is deleted. See [Running without AI](running-without-ai.md).
- `Platform Language`: the default interface language of every user. `Automatic` follows each user's browser; a user can still choose another language in their profile.
- `Default Presentation Template` (below).
- `Brand asset source` (below).
- `Platform Branding`, edited with `Edit`:
    - `Platform Name`, `Logo` (up to 512 KB) and `Description` (used for the browser tab description and social share previews);
    - `Login Message`: the text under the logo on the sign-in page, in Markdown, so it can link to a policy or a help page;
    - `In-App Colors`: the `Accent Color` (buttons, links, highlights) and the `Background Color` (card and panel shades follow it);
    - `Theme Color`: the color of a mobile browser's address bar and of the installed app's splash screen;
    - `Favicon` (the browser tab icon, up to 64 KB) and `Social Preview Image (OG Image)` (shown when the platform's address is shared, up to 512 KB).
- the XTM Hub registration (XTM One, below).
- `Instance ID`, with a button to copy it.

When the left menu is collapsed, it shows the product's mark instead of its logo. With a logo of your own, the mark is the favicon you upload next to it: upload one so the collapsed menu carries your brand. Without one, the collapsed menu shows the left end of your logo.

The instance ID is especially important during licensing and support work. The page can also show whether the active license comes from the environment or from a value stored in the platform, and which edition the deployment runs. A license carries your agentic capacity and the edition of each connected module; it never switches individual features on or off. See [Editions and licensing](editions-and-licensing.md).

### XTM Hub registration

Like OpenCTI and OpenAEV, XTM One can register itself with the [XTM Hub](https://hub.filigran.io): it then appears among your organization's platforms on the XTM Hub. `Register on the XTM Hub` opens the XTM Hub in a new window, where you sign in and choose the organization; the XTM Hub then hands the platform's registration token back to XTM One, which stores it encrypted and never shows it again. Allow pop-ups for the platform if the browser blocks the window. When the XTM Hub cannot hand the token back to the page (it shows the token instead), paste it under `The XTM Hub showed a platform token instead`. Set the XTM Hub URL before registering only for another XTM Hub than the default one, `https://hub.filigran.io` unless [`XTM_HUB_DEFAULT_URL`](../deployment/configuration.md#xtm-hub-registration) names another; it must be an `https` address. Register only with an XTM Hub you trust: the XTM Hub window can send the registration back to this page only because it stays linked to it, which also lets that XTM Hub navigate this page.

XTM One checks the registration with the XTM Hub every hour, and `Check now` asks at once:

- `Registered`: the XTM Hub confirms it.
- `Connectivity lost`: the XTM Hub did not confirm it, or could not be reached. The registration is kept and checked again every hour.
- `Register again`: the platform's instance ID changed since the registration (`PLATFORM_ID` set later, a restored or copied database). Nothing is sent to the XTM Hub while the instance ID differs. If the instance ID is restored, the next check asks the XTM Hub again.

When the XTM Hub no longer knows the registration (it was removed on the XTM Hub), XTM One forgets it and records it in the audit log. `Unregister` removes the registration on the XTM Hub, from an XTM Hub window, and then here; `Forget locally` only removes it here, for an XTM Hub that cannot be reached. The home dashboard shows the registration next to OpenCTI, OpenAEV and OpenCRQ.

An XTM One deployed in an XTM Platform trial is registered automatically by its deployer, like the trial's OpenCTI and OpenAEV (see [XTM Hub registration](../deployment/configuration.md#xtm-hub-registration)). With a trial licence, like OpenCTI, the registration cannot be removed from here: `Unregister` and `Forget locally` are not offered, and the XTM Hub URL is the default one, shown read-only.

`Default Presentation Template` is the brand template applied to every deck an agent generates when the request names no template. Only company-managed presentation templates qualify — the default is applied for every user, so a private template would leave everyone else's decks unstyled. When none is chosen, the platform's seeded brand deck is used — the `XTM One Brand Deck` on an XTM One platform, the `Filigran Brand Deck` on Filigran Copilot; clearing the field returns to it, and so does a default template that has been removed. See [Templates](../usage/resources/templates.md).

### Brand asset source

`Brand asset source` names a GitHub folder of brand asset packs that the built-in templates read, in addition to the packs the platform ships: logos, badges or pictures a deployment keeps in a private repository. Enter the folder's address, `https://github.com/<owner>/<repository>/tree/<branch>/<folder>` (without `/tree/...`, the root of the default branch), and choose a connected GitHub integration that can read the repository (a token with `Contents: Read`) and shares its account. Each sub-folder holding a `manifest.json` is a pack, in the format of the platform's own packs.

Saving downloads the folder and adds its pictures to the templates at once, and the card says how many packs it read or why it could not. The platform reads the folder again on every restart, and `Sync now` reads it on demand; `Clear` stops reading it. When the folder cannot be read (GitHub unreachable, a token revoked, a path that no longer exists), the templates keep the pictures they already hold until it can be read again. A pack of the folder named like one the platform ships is not used: the platform's own is, and the card says which. `Clear` asks for confirmation: once the folder is no longer read, a picture only the folder gave a template directly is removed from it. The platform's object store keeps the pictures: nothing else is stored from the folder.

## Organization

`Organization` holds the organization profile: what your organization does, what it must protect, the technologies it runs, and where findings become tickets. Agents read it to put security data into your organization's context. See [Organization profile](organization.md).

## AI Models

`AI Models` controls the model providers used by the platform.

This tab can include:

- chat and reasoning providers
- default reasoning effort
- embedding model configuration
- image model configuration
- video model configuration
- speech model and voice configuration
- music model configuration
- decision model configuration
- provider-specific model lists

If your environment manages providers outside the UI, XTM One shows that clearly and treats those values as locked.

### Reply suggestions

Under `Routing Model`, the `Reply suggestions` switch (on by default) lets the routing model propose each user's next message once an agent has answered in a chat: the message box shows it greyed out, `Tab` puts it in the box, and nothing is sent until the user presses Enter. It costs one short routing-model call per answer, made after the answer is shown. Nothing is generated while no routing model is set: unlike routing conditions, suggestions never fall back to the platform default model. Each user can turn suggestions off in their profile.

### Where each model is chosen

A provider's `Models` list holds the models that chat and reason: it is what agents, the General Assistant and the platform default pick from. The image, video, speech, embedding and decision models a provider serves are not in it unless they also chat: they are chosen in the section of their capability below (`Image Generation`, `Video Generation`, `Speech Generation`, `Embedding Provider`, `Decision Model`), each listing what the configured providers serve for it, and its `Configure` button shows once a provider serves one or a choice is saved. Opening `Configure` asks the providers for their models again, so a model a provider added or dropped since the page loaded is listed as it is now. A Filigran AI Gateway model its operator declares with chat and another type (embedding, say) is offered in that type's section as well as in the `Models` list, unless the model catalog classes its id as a model that does not chat. Video and speech models are also recognised by their id. A provider that serves no chat - ElevenLabs (voices, music, video and images), Kling AI, Runway, Seedance and Google Vertex AI (video), an embedding server - is added the same way but has no `Models` list: its card says which sections its models are chosen in.

A deployment can set these choices from its environment, the way it sets the default model: `DEFAULT_EMBEDDING_*`, `DEFAULT_IMAGE_*`, `DEFAULT_VIDEO_*`, `DEFAULT_SPEECH_*`, `DEFAULT_MUSIC_*` and `DEFAULT_DECISION_*` (see [Configuration](../deployment/configuration.md#ai-providers)). The section then shows an **Env** badge instead of `Configure`: the environment applies its choice again at every restart, so a change made here would not last. A variable naming a provider type that cannot serve the capability (or, for the decision model, given without its model) is ignored and logged at startup, and the section stays editable. One naming a provider type that serves it but is not configured keeps the section locked: add that provider, then restart the platform.

### Image generation

`Image Generation` lists the image models of the configured providers: OpenAI's (`gpt-image-*`), Gemini's Imagen models and its Gemini image models ("Nano Banana", `gemini-2.5-flash-image`; "Nano Banana Pro", `gemini-3-pro-image-preview`), Azure OpenAI's image models (the model chosen is the name of your image deployment), ElevenLabs' (GPT Image, Nano Banana and Seedream through its Image & Video API), and those a gateway serves. A Gemini or ElevenLabs image model draws at the aspect ratio it offers nearest the size asked for. ElevenLabs images need a Pro plan, and its key's "Image & Video Generation" permission; choose one of its models here, it is never picked on its own. With no image model chosen, the platform draws with OpenAI's or Gemini's built-in image model, else with the image model the Filigran AI Gateway marks as its default.

### Video generation

`Video Generation` and `Music Generation` are shown only while the Video Studio is turned on (`ENABLED_FEATURES=VIDEO_STUDIO`, see [Configuration](../deployment/configuration.md#video-studio)).

`Video Generation` chooses the model the [Video Studio](../usage/video-studio.md) generates AI clips with. The list is read from the configured providers:

- Sora on OpenAI and on an Azure OpenAI Sora deployment;
- **Google Gemini**, with the same Gemini API key as its chat models: Gemini Omni Flash (`gemini-omni-1.1-flash`), its built-in video model, from a prompt, a start image and up to three reference images together, at 720p or 1080p. Gemini Omni has no length setting: the length the clip asks for is written into its prompt, with "one continuous shot" and what to avoid, and the clip keeps the length the model made. Google shut the Veo 3.1 previews down on the Gemini API on 2026-10-22 and serves Veo 3.1 (`veo-3.1-generate-001`) on Vertex AI (Gemini Enterprise Agent Platform) only: to use Veo 3.1, add a **Google Vertex AI** provider (below);
- **Google Vertex AI** (`Add Provider`, then `Google Vertex AI`): Veo 3.1 (`veo-3.1-generate-001`), Veo 3.1 Fast and Veo 3.1 Lite, as Vertex AI lists them in the provider's region, 4, 6 or 8 seconds at 720p or 1080p, with their own sound - or without it when a shot asks for silence - from a prompt, a start image or up to three reference images (Veo 3.1 and Fast). See [Google Vertex AI (Veo)](#google-vertex-ai-veo) to set it up;
- **Kling AI**, added as a provider of its own (`Add Provider`, then `Kling AI`, with a key of the Kling AI API platform, `api-key-kling-...`): `kling-v3`, `kling-v2-6` and `kling-v2-5-turbo`, 3 to 15 seconds, 720p (`std` mode) or 1080p (`pro`), with its own sound from Kling 2.6;
- **Runway** (`Add Provider`, then `Runway`, with a `key_...` API key): Gen-4.5 (`gen4.5`, its flagship, from a prompt or a start image), Gen-4 Turbo (`gen4_turbo`, from a start image only), and Veo 3.1 / Veo 3.1 Fast through Runway. Only the models the account's Runway tier may run are listed, and a generation needs Runway credits;
- **Seedance** (`Add Provider`, then `Seedance (BytePlus ModelArk)`, with a ModelArk API key): Dreamina Seedance 2.0, 2.0 Fast, 2.0 Mini and 2.5, Seedance 1.5 Pro and 1.0 Pro, each activated in the ModelArk console first. For Volcengine Ark, give `https://ark.cn-beijing.volces.com/api/v3` as the base URL;
- **ElevenLabs** (the provider of its voices): Veo 3.1 and Veo 3.1 Fast, and the Seedance 2.x models once ElevenLabs has approved the account for them, through its Image & Video API (a Pro plan and the key's "Image & Video Generation" permission);
- the video models an OpenAI-compatible gateway serves behind the OpenAI Videos API - a LiteLLM proxy in front of Veo on Vertex AI or Runway, a custom OpenAI-compatible endpoint, the Filigran AI Gateway. A LiteLLM model is listed when its proxy declares it a video model, whatever its alias.

Kling AI, Runway, Seedance and Google Vertex AI serve video only: they are offered under `Add Provider` while the Video Studio is on, never for chat, and their key is checked against the provider when it is validated. Each model's own limits apply, and the clip says what changed: the length snapped to what the model makes, a resolution it does not make, a negative prompt it does not take, the start image deciding the format. `Auto` leaves the choice to the platform: a provider added for video (Kling AI, Runway, Seedance, Google Vertex AI) first, then the first provider with a built-in video model it still serves (Gemini Omni Flash on Gemini; Sora 2 on OpenAI until OpenAI retired it), then the first provider listing a video model. Adding a provider never chooses its video model for you: a provider retires its built-in model (OpenAI's Sora 2), and only `Auto` checks at each generation which one is still served, so the section stays on `Auto` until you, or `DEFAULT_VIDEO_PROVIDER`, choose one. The assistant can still pick another listed model for one clip. Like image generation, the setting is platform-wide: a person the chosen provider excludes cannot generate clips with it, and deleting the provider clears the choice, so clips go back to `Auto`.

A model its provider has retired is never listed nor used, read from the provider's own model records where it publishes them: OpenAI retired Sora 2 and Sora 2 Pro on 2026-09-24, and its Videos API no longer answers. Nor is a model its provider says it no longer serves - one Gemini stopped listing, or one a provider answered "not found" for - in this list as in the Video Studio. A retired model chosen here gives way to the next provider whose video model is live; with none left, the Video Studio and the assistants say why before anything is generated. The section shows such a choice as `Unavailable`, with the reason, instead of `Active`, and `Configure` stays available to replace it or go back to `Auto`. To generate AI shots, add a video provider (Google Vertex AI for Veo, Kling AI, Runway, Seedance, Google Gemini for Gemini Omni Flash, ElevenLabs) or a gateway serving a live video model, and choose it here.

### Google Vertex AI (Veo)

Veo 3.1 is served by Google Cloud's Vertex AI (Gemini Enterprise Agent Platform), billed per second of video to your Google Cloud project, within the project's default quota. The platform reaches it with a service account key:

1. In the Google Cloud project that pays for the videos, enable the Vertex AI API (`aiplatform.googleapis.com`), with billing on.
2. Create a service account for the platform (**IAM & Admin → Service accounts**) and grant it the **Vertex AI User** role (`roles/aiplatform.user`) on the project - for example `gcloud projects add-iam-policy-binding PROJECT --member="serviceAccount:NAME@PROJECT.iam.gserviceaccount.com" --role="roles/aiplatform.user"`. No other role is needed.
3. Create a JSON key for it (**Keys → Add key → Create new key → JSON**).
4. In **Settings → AI Models**, click `Add Provider`, choose `Google Vertex AI`, click `Load JSON key file` (or paste the key), and leave `Location` empty for `us-central1`, the region serving Veo 3.1. The form says which service account and project the key belongs to; the key itself stays masked, and is stored encrypted like every key.
5. Click `Validate Provider`: it lists the Veo models of the region. Save.
6. In `Video Generation`, click `Configure` and choose `veo-3.1-generate-001` under `Google Vertex AI`.

On a deployment configured from the environment, set `LLM_PROVIDER_VERTEX_AI_API_KEY` to the key (its JSON, or that JSON base64-encoded), and `DEFAULT_VIDEO_PROVIDER=vertex_ai` to make Veo the video model (`veo-3.1-generate-001` unless `DEFAULT_VIDEO_MODEL` names another); see [Configuration](../deployment/configuration.md).

When Vertex AI refuses, the clip says what is missing: `may not generate with ... grant it the Vertex AI User role` (step 2), or `The Vertex AI API is not enabled on project ...` (step 1).

!!! note "The service account key"
    The key lets whoever holds it use the project's Vertex AI: give it a service account of its own with the Vertex AI User role only, never one with broader rights, and delete the key in Google Cloud when the provider is removed. The platform only ever exchanges the key at Google's own token endpoint, whatever the key file names.

### Speech generation

`Speech Generation` chooses the model and the voice the chat speaks with (read aloud and voice conversations) and the [Video Studio](../usage/video-studio.md) narrates with. The models are read from the configured providers:

- OpenAI (`gpt-4o-mini-tts`, which also takes a delivery style, and `tts-1` / `tts-1-hd`) and an Azure OpenAI speech deployment, with OpenAI's voices;
- Gemini's text-to-speech models, with its studio voices, its voice library and the voices your Google project designed or replicated;
- **ElevenLabs**, added as a provider of its own (`Add Provider`, then `ElevenLabs`, with its API key). It is never offered for chat; its key is validated against its voices, and the same provider also serves music, video and images. Its voices are the account's voice library: the premade voices and the account's generated, cloned and professional voices, listed first, so the team's brand voice is one choice away. For data residency, give its regional endpoint as the base URL (for example `https://api.eu.residency.elevenlabs.io`);
- the speech models an OpenAI-compatible gateway serves behind the OpenAI speech API: a LiteLLM proxy in front of Amazon Polly, Azure AI Speech, Vertex AI Chirp, ElevenLabs or MiniMax, a custom OpenAI-compatible endpoint, the Filigran AI Gateway. A LiteLLM model is listed when its proxy declares it a speech model, whatever its alias. Such a model's voices are its upstream's: type the voice it expects. A Gemini model behind the Filigran AI Gateway offers Gemini's studio voices, Kore first, but not its voice library nor your Google project's own voices. An ElevenLabs model behind the Filigran AI Gateway offers the ElevenLabs account's voices, its own first, as ElevenLabs added directly does; it takes no delivery style and no pronunciation dictionary, and reads inline phonemes on `eleven_flash_v2`, `eleven_turbo_v2` and `eleven_monolingual_v1` only.

Pick the voice once the model is chosen. `Auto` leaves both to the platform: a provider added for speech alone (ElevenLabs) first, else the first provider with a built-in speech model (OpenAI, then Gemini), on its first voice, else the speech model the Filigran AI Gateway marks as its default, with the voice chosen when that model offers it, else its first voice. A video can still name another listed model and voice for its narration.

Speech only runs on a provider the person may use: when the chosen provider is reserved for other groups, or excludes the person, their speech falls back to `Auto` among the providers left to them, and when none is left the chat reads aloud with the browser's own voice and a video shows its narration as captions. Deleting the provider clears the choice, its voice included.

### Pronunciation lexicon

With the Video Studio on, `Pronunciation lexicon` (below `Speech Generation`) says how every video's voice-over reads the brand names and acronyms a speech model would guess: each entry has the term as written, how it is said (letters read one by one as capitals joined by hyphens, `X-T-M`), and optionally its IPA, how it is matched in a narration and whether it applies to the voice, the captions or both. It starts with the readings the platform ships, and administrators edit it with `Edit`: each save is a new version with a note, `Versions` lists them and `Restore` makes an older one the newest (nothing is written over); every change is in the audit log. The play button beside a term speaks it with the platform's speech model. A video's own pronunciations replace an entry with the same written form; every narration and every render is heard back and checked against the lexicon (see [Video Studio](../usage/video-studio.md)). ElevenLabs keeps the lexicon as a pronunciation dictionary in its account, created the first time a voice-over uses it.

### Render retention

With the Video Studio on, `Render retention` (below `Pronunciation lexicon`) says how many days the [Video Studio](../usage/video-studio.md#render-retention) keeps a render after it ended, before it is deleted with its files (its MP4, poster, captions, frames and insert crops):

- `Failed and cancelled renders`: 1 day by default;
- `Scene previews`: 7 days by default;
- `Older ready renders`, those a newer ready render of the same quality replaced: 30 days by default.

Whatever the days, the newest ready render of each quality of a video, the render the video and its public link play and the renders kept by hand are never deleted. `0` keeps a kind of render for good. A job deletes what is past its days once a night. Administrators change the days with `Edit`; everyone sees them, and the assistant knows them. Every change is in the audit log.

### Music generation

`Music Generation` chooses the model the [Video Studio](../usage/video-studio.md) composes AI music tracks with. The models are read from the configured providers:

- **ElevenLabs** (Eleven Music: `music_v2_5`, `music_v2`, `music_v1`), the same provider as its voices. A track is composed at the exact length asked, instrumental unless the brief asks for vocals. `music_v2_5` and `music_v2` can also continue a track the author approved, so a video that grew keeps its take and only its ending is composed again: that uses ElevenLabs' music inpainting, offered on its enterprise plans. Eleven Music needs a paid ElevenLabs plan;
- **Gemini** (Lyria): `lyria-3.5` composes a whole piece whose length follows the brief, `lyria-3-clip-preview` always makes 30 seconds (the video loops it, crossfaded).

`Auto` leaves the choice to the platform: a provider added for its media (ElevenLabs) first, else Gemini. The assistant can still pick another listed model for one track. Like speech, music only runs on a provider the person may use, and deleting the provider clears the choice.

No setting is needed for the **built-in music library**: its tracks are composed by the platform itself for each video, with no provider and nothing to license. Lyria models are never offered as chat models.

### Decision model

The **Decision Model** section of **Settings → AI Models**, below Speech Generation, picks the decision model agents use to act on the page of an embedded chat (Ask Ariane in OpenCTI): open a report, switch a tab, type a search term. A decision model chooses among options instead of writing text; at each step it picks the next element to click or field to type into from the page's current state. Clef Flash served by a local Ollama is one; OpenRouter, OpenAI and the Filigran AI Gateway serve others.

Only the decision models your providers report are offered in the list, each with its provider; nothing is enabled for them on the provider's card, which lists chat models only:

- **Ollama** offers the models it reports as decision models.
- **OpenAI** reports no such list, so XTM One asks its Decisions API about each model of the provider's model listing, with a request OpenAI refuses before running the model, so the check costs nothing. A model found to serve decisions is remembered for a day, one that does not for an hour; changing the provider's key checks again. The first load may take a few seconds and offer only part of the list, which the next loads complete. OpenAI has no default decision model: pick one it reports (`gpt-6-luna` today).
- **The Filigran AI Gateway** offers the models its operator declared as decision models, one that is also a chat model included, whether you add it as a **Filigran AI Gateway** provider or as a **custom OpenAI-compatible** provider pointed at it. Both ask its OpenAI-standard `/v1/decisions` route. A decision model's id rarely names its upstream (`tev1:0.8b` runs on Ollama), so either the gateway finds the upstream from its own declaration, or you pin it in a Filigran AI Gateway provider's credential (`<gateway URL>||ollama`). A pin applies to every call of that provider, chat included: put it on a second gateway provider used only for decisions. The gateway must serve `/v1/decisions` for either way to work.
- **A custom OpenAI-compatible endpoint** other than the gateway offers nothing in the list: its listing marks no model as a decision model. A custom endpoint reads declared types for this section only: its `Models` list and its embedding section still go by model id.

A provider that serves decision models without listing any (OpenRouter, a custom OpenAI-compatible endpoint, a gateway that declares none, or OpenAI while it cannot be reached or its account cannot use decision models) gets a `Model id` field instead: type the decision model id the provider gives you. A decision model already set stays shown, and can be changed or cleared, whatever the list holds.

A flow's **Classify** node whose provider is left unset runs this model on the provider set here when it asks the same model, unless a provider lists that model on its card: with several decision providers, it is how a model a provider cannot list (an OpenRouter id) finds its provider without picking it on every node. A custom OpenAI-compatible endpoint or gateway whose listing declares no decision model is reached by a Classify node only this way, or when picked on the node.

While the section says **Not configured**, no agent is offered the page actions. A decision model served with a small context reads less of each page: each step sends as much of the page as fits the context the provider runs it with (for Ollama, the model's `num_ctx`), or the context window set for that model on its provider's card, which only a model also enabled there as a chat model has; otherwise a fixed page size is used. Each step is recorded in the run's trace as `page_decision`, and an agent with tool approvals on asks before acting on the page.

The decision model reads the page the user is on: the names of its links, buttons and fields, and the values typed in its fields (never a password), along with the agent's goal and any text it types. All of it goes to the decision model's provider, so pick one you trust with what your users see; a local provider such as Ollama keeps it on your network. Each `page_decision` in a trace keeps that page text unless **Store full payloads** is off (Settings → Observability).

### Context windows

When you edit a provider's models (`Models` on its card), each selected model has a context window field: the number of tokens the model can read, which the platform uses to decide when to summarize a long conversation. Leave it empty to use what the platform knows of the model.

- **Type it in tokens or in shorthand.** `128000`, `128,000`, `128K` and `1.05M` are all accepted; `K` is a thousand and `M` a million, so `1.05M` is saved as 1,050,000 tokens. A saved window is shown in its shortest form, and its tooltip gives the exact number of tokens. A value that is not a whole number of tokens up to 100M is marked in red and the models cannot be saved until it is fixed.
- **Or let the platform find it.** Enabling a model looks up its window, and `Scan context windows` looks up every selected model: first in the provider's own model list (OpenRouter, Anthropic, Gemini, Mistral and OpenAI-compatible servers such as vLLM report it), then in the model catalog bundled with the platform, then in the platform's estimate for the model family. The tooltip of a value found this way says where it came from. A field that already holds a value is never changed, and a model none of these sources knows stays empty. Nothing is saved until you select `Save Models`.

When the provider's model list cannot be read, the scan says so and the values come from the catalog alone.

### When a model declines a request

Claude models can decline a request their safety classifiers flag, for example offensive-security work such as attack chaining. When the decline is in the cyber or AI-development category, XTM One asks an older model of the same provider before the other providers and the fallback model:

- **Claude Sonnet 5.5**: Claude Sonnet 5, then Claude Opus 4.8.
- **Claude Sonnet 5, Claude Opus 5, Claude Opus 5.5 and Claude Fable 5.x**: Claude Opus 4.8.

The provider's API key must give access to these models. The provider can bill a declined request, so each model that declines can add to its cost. A decline in another category (biology, for example) is never handed to another model. When every model declines, the agent says that the model declined the request.

An answer blocked by a provider's content filter (OpenAI, Azure OpenAI, Mistral and others) is a decline too, and so is a refusal served through the Filigran AI Gateway. A request that Azure's prompt filter rejects is not: it fails as a provider error. Only a category the provider names can be handed to another model: a filter that names none fails the request. Text a streamed answer had already shown is not kept.

Once a model has been replaced during a run, XTM One keeps that run's later steps on the model that answered instead of asking the first model again each step. A new run starts again from the model you chose.

### Reserving a provider for teams

By default every provider serves everyone. To split costs by team, configure the same vendor more than once (for example two Anthropic providers with two API keys), open `Access` on a provider card and restrict it to the groups and people who should run on it. A restricted provider shows a `Restricted` badge.

When a run picks a model, it only considers the providers its user may use: the unrestricted ones, plus those granted to them directly or through a group. A provider granted to the user comes first, so an agent shared between two teams runs on each team's own key. When the user may not use the provider an agent is pinned to, the agent runs the same model on another provider they may use, or falls back to their next model choice.

- Callers without an account (anonymous channel users, public widget visitors, the global MCP key) only use unrestricted providers, and so does platform maintenance that runs for no user, such as the consolidation of an agent's agent-level memory. Keep at least one provider unrestricted unless you want that work to stop. Work done for a user without naming one, such as compacting their conversation or consolidating their memories, runs as that user.
- Voice input and read-aloud in the chat use an OpenAI provider the user may use, and investigation web search an Anthropic or OpenAI provider the investigating user may use, their team's first.
- Administrators route like everyone else; restrict a provider to yourself as well if you want to run on it.
- A provider can name at most 500 groups and people in all.
- A model that only restricted providers offer cannot be picked, in the model pickers or on an agent, by someone outside their audience.
- Embeddings and image generation stay platform-wide.
- Opening a provider again keeps its access list, so restricting it later restores the same audience.
- Access stays editable on providers managed by the environment: the environment manages their key and models, not who may use them.

### Keeping people off a provider

Some deployments must keep part of the staff off a vendor, for example where a regulation forbids its models for employees of one jurisdiction, while everyone else keeps using it. Open `Access` on the provider card and turn on `Never use for people and groups`, then list those groups and people. The provider shows an `Exclusions` badge.

An exclusion is the opposite of a restriction, and it wins over it: a person in an excluded group never runs on the provider, even when another of their groups, or they themselves, are granted it. An exclusion applies whether or not the provider is restricted.

For an excluded person:

- Every chat, agent, assignment, flow, delegated task and LLM proxy call resolves among the providers left to them. An agent pinned to an excluded provider runs on another provider they may use, typically a locally hosted model, or falls back to their next model choice. When no provider is left, the request is refused with a message telling them so. Failover never lands on an excluded provider.
- The work done for them without naming them follows them too: compacting their conversation, consolidating and re-embedding their memories, titling their chats.
- Embeddings and image generation are platform-wide, so they cannot move to another provider: if the provider configured for them excludes the person, the person's searches of knowledge bases and memories, the documents they add to a knowledge base and their image requests are refused rather than sent to it. A document refused this way is marked `Embedding model not available to its author` and is not retried automatically. An agent searching their memories is told the search is refused, never answered with "no memories found"; in an agent's `Memory` tab, a search returns the keyword matches only. Re-embedding existing documents (a reprocess, a change of embedding model) is done for the person who last added or changed each document and for its knowledge base's owner, not for the administrator who started it: a document either of them is excluded from is not re-embedded, in a company-managed knowledge base too. A document added before this check existed carries no such person until it is changed again; in a company-managed knowledge base, which has no owner, its re-embedding checks no one. To give excluded people these capabilities, configure an embedding and image provider that excludes no one, for example a locally hosted one.
- A group listed in an exclusion cannot be deleted: remove it from the exclusion first. Deleting the group would otherwise silently give its members the provider back. When SAML group sync merges a group into another one, its exclusions move with its members. A deleted user is removed from exclusions automatically.
- Who is excluded is decided by group membership at the time of each request, so adding someone to an excluded group takes effect at once.

The exclusion only covers what XTM One sends to the provider. Pair it with your identity provider's group mapping (SAML group sync) so membership of the excluded group follows your directory.

### Deleting a provider

Deleting a provider asks for confirmation first, and the confirmation says what happens to everything that used it:

- **The platform default model and the General Assistant chat model** move to a replacement provider and stay pinned to it. Each always lands on a model the replacement lists: it keeps its model when the replacement's model list contains it; otherwise it takes the replacement's own default model if that is in the list, else the first model of the list, and the dialog names the exact model and provider. A provider that lists no model is not offered for a default that runs on a model (the dialog says which providers were left out, and why). The oldest remaining provider that qualifies is proposed; you can pick another one in the dialog.
- **Agents and users** that picked a model on the deleted provider are counted and named. Their choice is cleared, so they follow the platform default model from then on.
- **Image generation** keeps its image model on the replacement when the replacement can serve it, otherwise on the first remaining provider that can. When none serves that model, it moves to the built-in image model of the replacement or, failing that, of the first remaining provider that has one. Only providers that generate images are considered (a gateway listing an image model does not). When none can, the image default is cleared and image generation stops until one is configured. When the image model is set by `DEFAULT_IMAGE_MODEL` alone, it is only kept, never switched to another model, since the environment restores that model at the next restart.
- **The embedding default is never switched automatically.** Knowledge base search only matches content embedded with the same model, so a different embedding model would make every indexed document unsearchable. It is cleared instead. Until you set a new one, embeddings fall back to a remaining embedding-capable provider (OpenAI first, then Gemini, Azure OpenAI, Mistral, Bedrock, Ollama) on its built-in embedding model, which cannot find content indexed with a different model; with no such provider left, knowledge base search and document ingestion stop. The dialog names the fallback and how many knowledge bases hold indexed content. Setting a new embedding default re-embeds every knowledge base.
- **The routing and fallback models** are cleared when they used the provider: routing then uses the platform default model, and failover uses the other providers' default models.
- **The decision model** is cleared when it used the provider: agents can no longer act on the pages of embedded chats until another one is set (see [Decision model](#decision-model)).

When no remaining provider offers a chat model, the dialog says so: the platform default and General Assistant models are cleared instead of moved. If a remaining provider is not embedding-only (for example one listing only image models), chat still goes to it on its provider's built-in chat model, and the dialog names that provider and model. Only when no such provider remains do chat, agents and assignments stop working until a provider is configured. The image default still moves if a remaining provider can serve images. The deletion itself still goes through.

When no default chat model is set (neither the platform default nor the General Assistant model), chat that no agent or user choice decides goes to the oldest provider that lists a chat model. If that is the provider being deleted, the dialog names the provider and model chat goes to next.

When no embedding default is set and embeddings currently fall back to the provider being deleted, the dialog says where they fall back to next, and whether that model can still find the content already indexed.

A provider provisioned from environment variables is deleted when its variable is removed (at the next restart, or when AI is switched back on): the same rules apply, and the audit log records the deletion under a system actor.

The deletion applies what the dialog showed, or nothing. If another change is saved while the dialog is open (another administrator deletes a provider or edits a model list, for example), the provider is not deleted: the dialog reads the impact again and asks you to review it before you confirm. Deletions run one at a time, so two administrators deleting providers at once can never move a default onto a provider the other one is removing.

What was applied is recorded on the audit log entry of the deletion: the replacement provider and model, what each default became, and how many agents and user preferences were reset.

When no default is set at all, requests go to the first remaining provider that offers a chat model, in the order the provider list shows them (oldest first).

### Emailing users about model changes

When email is configured (`SMTP_HOSTNAME`), an administrator can tell the users about the models they work with. The email follows the Filigran layout, is in English, and goes to every active user except the bootstrap administrator account.

- **Changing the default agent model** asks whether to email the users that it is now their default model: `Save without email`, or `Save and email N users`.
- **Adding or retiring models** (saving a provider's model list, adding a provider, deleting one) saves the change first, then offers to email the users the models they can now use and the ones that were retired: `Don't email`, or review and email them. Only models users pick from count; routing, fallback, embedding and image models are never announced.
- Both dialogs offer `Send me a test email` first: the same email, marked `[Test]`, sent to you alone.

Every model list save you have not emailed is kept. The changes offered are those saves combined, per provider, over the last `MODEL_CATALOG_CHANGES_WINDOW_HOURS` (24 by default): a model added then retired again (or the reverse) cancels out, whatever the number of back and forths, and a change the live catalog no longer holds is left out. Before sending you can edit what the email announces. The dialog puts every model in one searchable list: the models users can pick now, the ones they could pick before, the models your providers say they serve (asked when the dialog opens; `Ask the providers again` refreshes them, at most 10 times in 10 minutes; a provider serving more than 2,000 chat models has its first 2,000 listed, and the dialog says so), and any id you type.

- **New models**: any model users can pick right now, even one that was not part of the saved changes. Only these can be announced as new; a new model users can no longer pick has to be removed before emailing.
- **Retired models**: any model at all, listed or typed, for instance one retired in an earlier change that was never announced. The email names a retired model by its id, so the id must look like one: at most 200 letters, digits and `.` `_` `:` `/` `@` `+` `-`, starting with a letter or a digit (`anthropic.claude-3-5-sonnet-20240620-v1:0`, `openai/gpt-4o`, `llama3.1:8b-q4_K_M`); an id with a space or another character is refused, and the models your providers say they serve are listed only when their id has this form. A typed id is announced under the provider you choose next to it; a model recorded under a provider an email cannot name (the embedding server) is announced under none. A model users can still pick can be announced as retired too: the dialog says it is still offered.
- Search by model id, name or provider, narrow the list with `Users can pick`, `Not offered`, `In the email` and the provider filter, and pick with the mouse or the keyboard: the Up and Down arrows move through the list, `Enter` adds or removes the highlighted model (as new when users can pick it, as retired otherwise) and `Shift+Enter` retires it. A typed id no list holds is offered above the list: `Enter` adds it when nothing else matches, and when the search also finds longer ids (`gpt-4` finds `gpt-4o`) the Up arrow reaches it first. `Esc` clears the search before it closes the dialog.
- Pasting a list of ids, one per line or separated by commas, adds them in one go: `Add all` announces the ones users can pick as new and the others as retired, `Retire all` retires them all.
- `Preview` shows the email as the users will receive it, updated as you edit.
- `Reset to the saved changes` goes back to what the saves add up to.

Saves made while you review are kept for the next email: sending covers the changes you reviewed, and the panel then offers the newer ones.

The `User announcements` panel under the active default agent model shows the latest announcement and how it went (emailed to how many users, refused addresses, or why it was not sent). Besides the models, an announcement of a deployment's change can carry the image, video, speech, music and decision models its environment pins (video and music only while the Video Studio is turned on): the panel lists each one with its model, and the model it replaces. The users are emailed `MODEL_CATALOG_EMAILS_BATCH_SIZE` at a time, one batch a minute, so an announcement to many users takes a while: while it goes out the panel shows how many users it reached out of how many, and about how long the rest takes. When the email server refuses a batch for a while (a hosted mailbox's hourly sending limit, for instance), the next batch waits, 1 minute after the first failure and up to an hour after several: the panel says when it runs next and shows the server's last error. An address the server refuses for now (an unknown domain, a full mailbox, greylisting) does not hold the others: it is set aside, tried once more 10 minutes after everyone else was emailed, and counted as refused if it fails again, a sending limit still in force at that point included. While it waits for that retry, the panel says how many addresses refused the announcement for now, when they are tried again (or that they are being retried), and the last address refused, which is not an email server error. Four addresses refused for now in a row read as the server refusing the platform, and the batch stops and waits as after a server error, longer each time a batch gets no email out - except, once the server accepted an email of the announcement, a refusal that names the address itself (no such domain or mailbox, a full or disabled mailbox): a list holding many dead addresses goes on past them. More than 25 addresses refused for now in a row since the server last accepted an email of the announcement, whatever the batch size, read as the server failing again (a relay that lost its DNS answers "domain not found" to every address), and the batch stops as above. The retries are not held to that bound: they are the refused addresses alone, one after the other. An announcement that waits for an earlier one says so, and when the earlier one is tried next; so does a change a deployment made that waits for the deployed models to stop changing (`MODEL_CATALOG_EMAILS_SETTLE_MINUTES`, 30 by default): the panel says when it goes out, and a model change deployed before then is added to it. From there you can email the users about the current default model at any time, including one set by `DEFAULT_MODEL` or a change the platform recorded while its emails were off, review and email the model changes you saved without emailing them (the panel says since when and how many saves they combine), and announce models of your choice with `Announce model changes` when nothing is waiting.

An email never describes a catalog that no longer holds: a queued change that has not reached anyone yet goes back to the saved changes when you save again, and an announcement whose news was undone before it went out is dropped. Announcements sent from Settings do not depend on `MODEL_CATALOG_EMAILS_ENABLED` and ignore `MODEL_CATALOG_EMAILS_RECIPIENTS`, which govern the emails sent when a deployment changes its environment-managed catalog (see [Configuration](../deployment/configuration.md)). When the deployment sets `MODEL_ANNOUNCEMENT_EMAILS_DISABLED=true`, no announcement is emailed at all: the panel says so and offers no way to email the users. Every announcement sent is recorded in the audit log.

## General Assistant

`General Assistant` is an admin tab for the shared assistant used across the platform.

This is where admins can define:

- the default prompt
- the default model
- the maximum number of reasoning steps
- how long an agent may pause when it is waiting for work it delegated
- which tools and resources the assistant can use
- which agents it can hand work to
- how it behaves across channels

Two of these deserve a word, because they bound different things:

- `Max Iterations` (300 by default, up to 1000) is how many *tool-calling rounds* an agent takes before it is asked to wrap up. It is about work done, not time spent. A run still making progress at that point continues in stretches of the same size, up to 1000 rounds, unless the deployment turns this off (see [Long runs](../deployment/configuration.md#long-runs)). Type the number or drag the slider; an agent's own `Max Iterations` (agent `Configuration`) overrides it. Every agent that does not set its own value, a new agent included, runs on this one. The built-in CTEM, XTM-One, OpenCTI, OpenAEV and OpenCRQ assistants run on 300 rounds whatever this setting says, and the other built-in agents that use tools on at least 100, except the Deep Investigation Agent, the CTI STIX Harvester and the CTI STIX Extractor (Grounded), which are bounded on purpose. The images an agent may look at in one message grow with its rounds - 10 per 25 rounds by default (`AGENT_LOOP_VISION_VIEWS_PER_TURN`) - so a long visual job such as a video or a deck keeps its sight: give such an agent more rounds rather than fewer.
- `Waiting` sets two time limits for the `wait` tool — the longest single pause, and the total an agent may spend waiting in one run. An agent uses it when it has delegated a task and wants to re-check in a few minutes rather than poll for it continuously. A pause deliberately does **not** count against `Max Iterations`, which is why it needs its own limits; the run's own time budget still applies, and users can skip or cancel a wait at any time from the chat.

`Built-in Tools` lists every built-in suite the General Assistant can run. Work items, shared state and trace inspection are keyed to an agent, so they are only offered on agents.

A new General Assistant starts with file generation, report generation, presentation generation, template search, the Video Studio, web fetch and the platform builder, the suite named after the platform (`XTM One` or `Filigran Copilot`). The platform builder is what lets a person who has just signed in ask the assistant to create an agent, an assignment or a knowledge base. It acts as that person, with the same permissions as the rest of the platform: a user creates their own agents, and only an admin changes company-managed ones. Upgrading adds a tool a release makes a default (web fetch and the platform builder, the Video Studio, report generation) once to a General Assistant that existed before. If you remove it afterwards, it stays removed. A user who picked their own General Assistant tools in `My Profile` keeps that list. On XTM One, the General Assistant is linked to the `CTEM Assistant`, which carries these tools itself, the platform builder included. Once you save `Link to Agent`, whether with another agent or with no agent, your choice is kept across restarts, upgrades and turning AI back on. If the linked agent is deleted, the link is cleared and the next restart links the `CTEM Assistant` again.

`Link to Agent` makes an agent answer in the General Assistant's place, with its own model, persona, tools and configuration. Only an enabled company-managed agent that every user can chat with can be linked: not one restricted to the people granted access, and not one with chat disabled. Once linked:

- a new conversation started without an agent opens the linked agent, and the chat page and the standalone home select it by default unless a user chose their own default agent;
- a message the General Assistant would answer on a channel (Slack, Teams, ...) is answered by the linked agent, on the channels the General Assistant's `Channels & Routing` settings allow. The reply is the linked agent's: named as its own when other agents are @mentioned in the same message (it does not answer its own mention a second time), and listed under its name in Activity;
- the `General Assistant` entry of the chat agent list is replaced by the linked agent.

A web conversation opened while the link is in effect belongs to the linked agent, like a conversation a user starts by picking that agent: removing the link, or changing the agent later, does not hand it back to the General Assistant, and if the agent is disabled the conversation says so. A channel conversation is not tied to the agent: each message follows the link as it is when the message arrives. Conversations started with the General Assistant before the link keep the General Assistant.

Who answers is the conversation's, wherever it is continued. A Slack or Teams conversation the General Assistant would answer is answered by the linked agent in Slack or Teams, in the web app when the user continues it there (the chat shows the linked agent's name and tools), and when a background task it started finishes. A web conversation started with the General Assistant before the link keeps the General Assistant in Slack or Teams too.

If the linked agent is later disabled, made private or restricted, or has chat turned off, the link is not followed: new conversations and channel messages go to the General Assistant with its own configuration. `Link to Agent` then says why under the selector and shows the General Assistant's configuration again, so you can relink, fix the agent or edit the General Assistant.

`Agent Handover` only offers enabled company-managed agents that every user can reach: the General Assistant serves every user, and a private agent, or a company-managed one restricted to the people granted access, would only be reached by the people who can see it. Each target is used as a `Delegate`, `Consult` or `Transfer` handover (`Mandatory Transfer` is only available on agents). Targets saved without a type are shown as `All types`: the General Assistant can delegate to, consult or transfer to each of them, and an agent you add next to them joins them as `All types`. Types are set per target, so picking a type for one of them gives every target a type; before it does, the screen names the targets that will become `Delegate` and asks you to confirm. A first agent, or an agent added once the targets have types, starts as `Delegate`. A target saved earlier that is no longer company-managed, is restricted, is disabled or was deleted stays listed with the reason until you remove it.

### General Assistant skills

`Skills` lists the company-managed skills the General Assistant can load: instructions it pulls in when a task matches them, the way an agent loads its own skills - only their one-line descriptions sit in its prompt, the full instructions are read when needed. Click a skill to attach or detach it; each click saves.

Until you choose, the General Assistant carries the built-in skills the platform declares for it - `Video Production` (how to make a brand film, an event loop, an explainer or a demo with the Video Studio, while it is turned on), `Presentation Design` (how to build a deck) and `Report Design` (how to write a report or a brief from your data) - marked `Built-in`, and a later release that ships a new one adds it. Once you attach or detach a skill, your list is kept as it is; `Use the built-in skills` goes back to the platform's. A General Assistant linked to an agent answers with that agent's skills, so the card is hidden while the link is in effect. On XTM One the General Assistant is linked to the `CTEM Assistant`, which carries the same three skills, as do the `XTM-One Assistant` and the assistants of each product (`OpenCTI Assistant`, `OpenAEV Assistant`, `OpenCRQ Assistant`, `FIMI Analyst`).

### Workspace knowledge

What every [workspace](../usage/workspaces.md#give-a-workspace-its-knowledge) gives the agents working in its conversations, at the bottom of the tab (Enterprise Edition):

- `Sources given whole (% of the context window)` — while a workspace's sources fit in this share of the model's context window, agents receive them whole; beyond it, they search them. 5 to 40, 15 by default.
- `Conversation indexed after (minutes of quiet)` — a conversation filed in a workspace becomes searchable by its agents once it has been quiet this long. 5 to 1,440, 30 by default.

Changes are recorded in the audit log.

This tab affects the assistant experience seen by the whole organization unless a user overrides part of it in `My Profile`.

## Channels & Bots

`Channels & Bots` is the shared routing and channel administration area.

It currently covers:

- always-on web chat
- external channel bot setup
- receive and publish whitelists
- bot-specific tools
- anonymous access
- private conversation handling
- mention-only behavior in group channels
- per bot, `Ask before running tools`: whether tools that need approval stay unavailable in its conversations, on by default (see [Tool approvals through a bot](channels/index.md#tool-approvals-through-a-bot))

This is also where admins can review or edit the connection details for Slack, Teams, WhatsApp, Telegram, and other supported messaging platforms when those channels are enabled in the deployment.

For the setup of each platform, step by step, see [Channels and bots](channels/index.md).

## Integrations

`Integrations` is an admin tab with its own sub-navigation.

It currently includes:

- `Catalog`
- `OAuth Defaults`

Use this tab when you want to manage which integration types are offered in the platform and what default OAuth behavior should apply when users configure them.

The webhook URL shown for an OAuth default belongs to that admin app. By default, a verified event it receives reaches every assignment it reached before: those whose agent is bound to an integration of that provider. With [Require verified integration notifications](#security-options) on (or `INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true`) it reaches only the integrations running on that app (created with its OAuth defaults; for an integration created before XTM One recorded that, the one whose client ID is the OAuth default's) whose agent owner may use them, and for a GitHub App event, only integrations that record its installation or, recording none, whose own GitHub token sees it (read in the background: a delivery never waits on GitHub, and one reaching an integration whose installations are not known yet is handed over as before; an integration holding no usable token, or a token GitHub lists no installation for, receives none); the Security Audit reports the default, and each delivery that reaches an assignment the strict rule would leave out is logged. An integration on a user's own app receives its events through its own webhook URL, shown on the integration page. The webhook verifies an event with the webhook secret set on the OAuth default (for LinkedIn, its client secret). For a provider with no OAuth default, or with one that holds no webhook secret, an event is accepted without a check, as before: the delivery is logged, and the Security Audit names the provider and the assignments it can trigger. LinkedIn notifications sent to `/api/v1/integrations/notifications/linkedin` follow the same rule: that URL belongs to the same admin app, whose client secret verifies each notification. With no LinkedIn client secret configured, a notification is accepted unverified as before (logged and reported by the Security Audit), and dropped in strict mode.

## Quotas

`Quotas` is the platform-wide usage control area.

In XTM One, it is mainly about:

- agentic execution quotas
- license-driven limits
- per-user or per-group quota overrides

Some other deployments also show extra quota types such as LLM proxy usage.

Use this tab when you need to understand whether the environment is operating under a shared license limit or a manually configured quota policy.

### How the limits read

- **A limit on a user, a group, or the per-user default** is what one user may spend in the period. `0` is a hard stop: the user cannot run any agentic action. This holds under a Filigran license too - a user blocked by a `0`, their own, a group's or the per-user default, stays blocked even while the license pool still has room.
- **A platform-wide cap** (the `Platform-wide limit` field, or a period of your Filigran license) caps every user combined. Empty means no cap, and a license period issued without a quota has none either. The field no longer accepts `0`; a `0` saved by an earlier version is read as no cap, as it always was. What a platform-wide cap has counted is saved to the database with the rest of the quota usage, every few minutes, so a Redis restart or eviction does not start the period over: at most the last few minutes of usage before it are lost.
- **An empty field on a user or a group inherits**: the user falls back to their groups, then to the per-user default. To lift the limit for a user or a group instead, turn on its `Unlimited` switch.
- **When a user belongs to several groups**, the most permissive group limit applies, and an `Unlimited` group is the most permissive of all. A group whose field is empty sets nothing and does not take part.

The platform configuration API reports a license period without a cap as `null`, and a deployment with no allowance at all (Community Edition) as `0`.

These rules apply to every value saved from this release on. A value saved by an earlier version keeps the effect it had until you save it again: a `0` under the license pool gives way to the pool, a group without a limit yields to a member's other group that has a number, and a user or group quota type holding no limit counts as Unlimited. Where saving such a value would change what it does, its quota card says so.

### Requests with no account

Some agentic requests have no XTM One account behind them: a message from someone without an account on a channel that allows anonymous users, a visitor of an agent's public chat widget, and a call to the global MCP endpoint (`/mcp/global`) made with the platform MCP key. They count against the platform-wide limits exactly like everyone else's requests - the periods of your Filigran license, or the `Platform-wide limit` - and against nothing else: no per-user default, user or group limit applies to them, since there is no user.

- **At the limit**, the person gets `This service is at capacity right now. Please try again later.` rather than a message about quotas or licenses they have no way to act on. On a channel, it goes wherever the bot would otherwise have answered, like the message a Community Edition deployment answers with.
- **While the quota store is unreachable**, they are refused like every agentic request in xtm_one mode, with `This service is temporarily unavailable. Please try again later.`; in copilot / dev mode they follow `Quotas when their store is unreachable`.
- **A request that never reached a model** (no provider configured, a provider that could not be reached) gets its action back. Any other failure keeps it: the `Agent Failure Pool` below is an allowance per user, and there is no user to draw it from.
- **Where they show**: the platform-wide usage of this page, and the platform-wide gauge of a user whose limit is the platform-wide pool, include them. They are never attributed to anybody - not in a user's own usage, the top consumers or the `Agent Failure Pool` card.

An assignment run that no user can be attributed to (a company-managed assignment saved before every assignment kept its owner, with no `Run as` user) counts the same way.

### Resetting the platform-wide usage

The license ceilings (or, in copilot / dev mode, the platform-wide limit) are compared with the platform-wide usage counters: every action on the platform, whoever ran it, in the current day, month and year. No administrator action resets those counters - in XTM One mode they are what the license sells, so `Reset usage` on a user's page only ever resets that user's own counters.

When a deployment needs its allowance back before the window turns over - typically a proof of concept that used it before the evaluation ended - Filigran support can issue a **reset code**. On the license banner at the top of the page, `Enter a support code` (administrators only) takes it, and the same dialog takes an `Agent Failure Pool` recharge code as well: the code says what it grants.

- A reset code starts the platform-wide counters of the **current** windows again from zero: by default the agentic and sandbox counters (the two an agentic ceiling adds up) in the current day, month and year; Filigran can narrow it to some of those windows, or include the LLM proxy counter. Past windows are history and stay as they are.
- The ceilings themselves do not change, and neither does any user's own usage or limit. A user whose own agentic limit is tighter than the license ceiling stays blocked by it until you reset that user with `Reset usage` on their page.
- The confirmation names every counter that was reset, with what it held before.
- A run already in progress when the code is redeemed and refunded afterwards gives its action back to the fresh counter (at most one per run).
- Like every support code, it is verified on the spot with no outbound call, single-use, bound to this platform unless issued as `global`, and expires on its own. It is recorded in the audit log (`Reset the platform-wide usage counters (...) with support code ...`) and, when the `Agent Failure Pool` card is shown, in its list of redeemed codes, marked `Usage counters reset`.

### Agent Failure Pool

An agentic action is deducted from the user's action quota before the agent starts, so a run that fails has already spent an action on work nobody received. The `Agent Failure Pool` card is where that is accounted for: each agent's first few failures are credited back instead of deducted, and the card shows how much of that allowance each agent has spent, how many pools are exhausted (and therefore spending actions on failures again), and the date the allowance renews.

The allowance renews once a year, on your Filigran license anniversary. Its size is set by the operator in the environment and is deliberately not editable here — it decides what is billable.

The pool only concerns users who have a limit to give back to. When a user's agentic quota is unlimited at every level — no license ceiling or platform limit, no default per user, no limit on the user or their groups — a failed run costs them nothing, so their agents' pools are left untouched. The card says so: a notice appears when the platform-wide quota is unlimited (the pool then only applies to users or groups carrying a limit of their own), and a row belonging to such a user reads `Not applicable` instead of `Spent`. It is never counted under `Pools exhausted`.

The credited-back totals count every credit granted in the window, including those of a user and an agent that have both been deleted since: those have no row left in the list, and the card says how many of the total they account for (`including N for deleted users and agents`), so the per-cause breakdown under each total always adds up to it.

Resetting a user's quota usage (`Reset usage` on the user's page) gives their agents a full allowance again: their credits leave the totals, and their causes leave the breakdown with them. Causes recorded by earlier versions cannot be traced back to a user, so a reset that removes credits from the current window files those older causes under `No cause recorded` rather than leave a breakdown larger than its total.

A reset, a support-code recharge or a support-code reset of the platform-wide usage never runs while quota usage is being saved to the database: the save in progress stands aside, usually within a fraction of a second. On the rare occasion it cannot, the action is refused with `Quota usage is being saved right now, so nothing was changed. Try again in a few seconds.`, and a support code refused that way is not used up. A reset or a recharge that fails part-way leaves every counter as it was; the causes it had already removed from the breakdown then read `No cause recorded`.

The card separates the two reasons an action comes back, because only one of them is rationed:

- **Agent failure** — the run failed on its own merits. This spends one slot of that agent's allowance, and the per-agent table shows how many slots are left.
- **No LLM call** — the run never reached a model: the provider was unreachable, down or not configured, or the platform abandoned the work itself (worker lost, deadline blown) — assignment runs and background sub-agent tasks alike. Credited back for free, outside the allowance, however often it happens: no LLM was called, and an incident on the platform's side must not ration what a user gets. The `Provider` column counts those per agent.

Some agents carry a `Platform` badge. Those are the seeded platform automations, whose *scheduled* runs never consume the action quota at all — so they can never appear here for that work. A row exists for one only because someone triggered it manually, which is charged like any other run.

One action is available: `Enter a support code`. Filigran support can issue a single-use code that gives every agent on the platform a full allowance again without waiting for the renewal date — after an incident on their side, for instance. Paste the code (`XTMP-…`) and it is verified on the spot, with no outbound call; a refusal explains itself (not issued by Filigran, expired, issued for another platform, already used). Codes already redeemed are listed underneath with the date and the administrator who used them.

## MCP Endpoint

`MCP Endpoint` is the admin version of MCP exposure.

It lets administrators:

- generate or revoke the global MCP key
- copy the platform MCP endpoint
- choose which agents are exposed globally
- review the connection snippet for external clients

This is the shared endpoint, separate from the personal MCP exposure available in `My Profile`. A call made with the global MCP key has no account behind it: it counts against the platform-wide agentic limits (see [Requests with no account](#requests-with-no-account)). A call made with an OAuth token counts against its user's limits, like any other request of that user.

The page also contains the **Platform MCP Servers** card, which controls the native OpenCTI, OpenAEV, and OpenCRQ MCP servers (`/mcp/opencti`, `/mcp/openaev`, `/mcp/opencrq`):

- a global enable/disable toggle (on by default)
- the endpoint URL and live connection status for each platform

These servers activate automatically when a platform is registered with XTM One and expose business and knowledge tools only. See [`Platform MCP servers`](../usage/resources/platform-mcp-servers.md) for details.

A separate **Platform Tools MCP Server** card controls `/mcp/xtm`, which exposes XTM One's *own* tools — agent traces, agents, assignments, runs, knowledge bases, prompts, skills, custom tools, integrations, and variables — to external MCP clients:

- a global enable/disable toggle, **off by default**
- the endpoint URL, with a copy button

It is off by default because it exposes this instance's configuration surface rather than a connected platform's content. Each caller only reaches what they can already see and manage in the UI, and every write goes through the same permission gates as the REST API.

Once enabled, the card also offers downloadable installer packages (Claude Code, Claude Desktop, Agent Plugins) — the same ones users get in their profile, so you can review exactly what is handed out. With **One-click install** on, a download creates a new personal API key for the downloading user and writes it into the package; those key creations appear in the audit log like any other, and revoking a key cuts off only the client it was made for. See [`Platform tools MCP server`](../usage/resources/platform-tools-mcp-server.md) for details.

## Security

`Security` is where administrators check the platform's security posture, set its security options and tool approvals, configure sign-in, connect secret vaults, review personal API keys and hold the emergency stop token. It has eight sections, each with its own address:

- `Security audit` (`/settings/security`, the section the tab opens on)
- `Security options` (`/settings/security/options`)
- `Tool approvals` (`/settings/security/approvals`)
- `Authentication` (`/settings/security/authentication`)
- `Secret vaults` (`/settings/security/vaults`)
- `API keys` (`/settings/security/api-keys`)
- `Emergency stop` (`/settings/security/emergency`)
- `Memory purge` (`/settings/security/memory`)

### Security audit

The security audit reviews the configuration the platform runs with: encryption, sign-in (SSO, self-registration, local sign-in, two-factor coverage, password, session and link lifetimes, the automations of accounts nobody signs in to), the platform registration token and the registered platforms (any the integration catalog does not list), integrations, agents and the public chat widget, channel bots, runners, relays, webhook assignments, custom tools and their sandboxes, the code interpreter, MCP and A2A, LLM providers and traces, and the infrastructure (Redis and the fail modes, debug mode, CORS, the reverse proxy). Each check reports a finding with a severity:

- `Critical` - exploitable now: a secret everyone knows, a dependency failure that switches protections off.
- `Warning` - a protection is off or misconfigured while what it protects is in use, or a setting silently weakens one.
- `Info` - a protection is off with nothing it protects in use, a protection only monitors, or context.
- `Passed` - enforced, or nothing to fix. A check that can warn also reports its pass whenever what it protects is in use, so the report shows what is enforced, not only what is not.

"In use" means the audit sees something the protection would refuse or change right now: an embedded widget, a webhook assignment with no secret, a stored OAuth token URL on loopback, runner or MCP credentials issued while the platform is served over plain HTTP, the answers or tool results a connected prompt guard does not evaluate. While it is in use, a protection that is off is a `Warning`, one that only monitors is `Info`, one that is enforced is `Passed`. An opt-in protection off with nothing in use is `Info`: a deployment that changed nothing is not warned about it.

The page shows:

- the overall posture: `At risk` while a critical finding remains, `Needs attention` while a warning remains, `Healthy` otherwise, and how many security controls are enforced (and how many only monitor);
- the pass rate, the share of the checks that passed. Informational findings neither pass nor fail, so they are not counted;
- how many findings there are per severity, and per category with the category's own pass rate. Select a category to show only its findings;
- two views, `Findings` and `Controls`. The address keeps the one you opened (`/settings/security/audit?view=controls`), so you can link to it;
- the findings, grouped by severity or by category, which you can filter by severity, category and text. Each one says what was found and, when there is something to do, how to fix it;
- when the audit last ran.

The audit reads the live configuration every time it runs. After changing a setting or an environment variable (and restarting the platform when the variable needs it), select `Re-run` to check the result.

The platform also runs the audit on its own once an hour. A `Critical` or `Warning` finding it has never reported notifies every active administrator in the notification bell: one notification per run, naming the new findings, which opens the audit on them. Each finding is reported once, for good: it is not reported again when it escalates, disappears and comes back, or when Redis restarts, since what was reported is kept in the database. A run with no active administrator records nothing, so the first administrator still hears about every finding. The only finding reported a second time is a dismissed one that reopens (see below). The notification names the findings only; what they list (accounts, agents) stays on this page.

#### Dismissing a finding

Some findings describe a choice you made on purpose: a protection you leave off, a setting the deployment needs. Select `Dismiss finding` on a `Critical`, `Warning` or `Info` finding to accept it, with an optional reason (up to 1,000 characters). A dismissed finding:

- leaves the counts, the posture, the pass rate and the category breakdown, and no longer raises its control in the `Controls` view;
- is never notified;
- is listed under `Dismissed findings` at the bottom of the `Findings` view, collapsed, with who dismissed it, when, and why. `Restore` counts it again; a restored finding that was never notified is notified once, like a new one. The platform keeps who last dismissed a restored finding, at which severity and why, along with who restored it and when.

A dismissal holds for the severity it was made at. When a later run reports the finding at a higher severity (an `Info` that became a `Warning`, a `Warning` that became a `Critical`), it reopens: it counts again at once, and the finding says it was reopened, from which severity, and shows the dismissal it replaced. The next hourly run records the reopening and notifies every administrator once (dismissing or restoring any finding in the meantime records it too). Dismiss it again if the new severity is acceptable too.

Dismissals are stored in the database, apply to every administrator, and are recorded in the audit log (`dismiss` and `restore` by the administrator who made them, `reopen` by the platform). Scripts use `PUT /api/v1/security-audit/findings/{id}/dismissal` with an optional `reason` and `DELETE` on the same address to restore; both return the report. In the report, a dismissed finding carries `dismissed` (who, when, why, and the severity it was dismissed at), a reopened one carries `reopened`, and `dismissed_count` says how many were left out of `summary` and `score`. A finding the audit does not report now cannot be dismissed, nor can a `Passed` one.

#### Accounts nobody signs in to

Deactivating an account in `Administration > Users and groups > Users` pauses the assignments and flows it owns, except company-managed ones, and reactivating it resumes them unchanged. Removing someone from the identity provider only stops their sign-in: their account stays active, and so do their automations, with their identity and their integrations. `authentication.dormant_account_automations` is a `Warning` that lists the active accounts that own an enabled assignment (on an enabled agent) or an active flow that is not company-managed, when nobody has signed in to them for 30 days, or ever for an account created more than 30 days ago: each with its last sign-in and what it owns. Deactivate the account of anyone who left, or transfer the agent (its assignments follow) or the flow to an active owner, or make it company-managed. While every such owner signed in within 30 days, the audit reports `authentication.automation_owners_signed_in` instead.

A sign-in is one the audit log records: a password (with its second factor when there is one), OIDC or SAML, an MCP OAuth authorization, or an XTM One Desktop sign-in confirmed in the browser (declining one is not a sign-in). A session lasts the absolute session lifetime (`JWT_EXPIRE_MINUTES`, 7 days by default) and is not extended, so anyone using the web app signs in within the 30 days unless that lifetime is set longer. API keys and the chat embedded in OpenCTI or OpenAEV do not sign in: an account used only through them is listed too.

#### Personal API keys

Personal API keys (`fcp-...`) authenticate their owner on the REST API, the LLM proxy and the MCP endpoints, so the audit reports the ones that authenticate today (active, unexpired, of an active account):

- `authentication.api_keys_without_expiry` (`Info`): keys that never expire, while no maximum lifetime is set. Set `API key maximum lifetime (days)` in [Security options](#security-options) (`API_KEY_MAX_LIFETIME_DAYS`).
- `authentication.api_keys_beyond_max_lifetime` (`Warning`): once a maximum lifetime is set, the keys that never expire or expire more than that many days from now, later than a key created or re-dated today may. Setting the maximum never cuts an existing key off: a key created before it keeps its expiry, or none, until its owner gives it one in `My Profile` > `API Keys` or an administrator revokes it.
- `authentication.api_keys_expire` (`Passed`): every active key expires, within the maximum lifetime when one is set.
- `authentication.api_keys_full_scope` (`Info`): keys that reach every surface, read-write, for every agent - all their owner can do. Every key created before scopes existed is one, until it is changed. `authentication.api_keys_scoped` is the pass.

Each lists the keys by name, owner and last use, and `Review these API keys` opens [API keys](#api-keys) filtered on them.

#### Nova semantic thresholds

The `semantics:` thresholds of the Nova community rules were written for `all-MiniLM-L6-v2`. The platform scores them with its own embedding model and maps each score onto that model's scale when the model has a measured calibration (see [Semantic-threshold calibration](../deployment/configuration.md#semantic-threshold-calibration)). While the Nova integration runs its full engine and an enabled platform rule has a `semantics:` pattern, the audit reports `agents.nova_semantics_calibrated` (`Passed`) on a calibrated model. On a model with no calibration it reports `agents.nova_semantics_uncalibrated_block_rules`, a `Warning` listing the `Block` rules among them, or `agents.nova_semantics_uncalibrated`, an `Info`, when they all only log. Switch those rules to `Log` on the Nova rules tab, or pick a calibrated embedding model for the semantics tier there. When the semantic model picked there is not used (chosen for another embedding provider, or returning no embedding), it also reports `agents.nova_semantic_override_ignored`, a `Warning`, and the findings above describe the platform embedding model the tier scores on instead.

#### Reads with no account

An agent that browses the platform (the agent builder's read tools, template search) for a caller with no account - an anonymous channel sender, the public chat widget - sees only company-managed resources without `Restrict access`; what the agent itself is configured with stays its reach (see [Channels](channels/index.md#direct-messages-mentions-and-anonymous-users)). Three kinds of work with no account keep every company-managed resource, restricted ones included, and three `Info` findings list them while a company-managed resource has `Restrict access` on:

- `access.ownerless_assignments_restricted_reach`: company-managed assignments that run with no user (an automatic trigger and no "run as" user) - set a "run as" user in each resource's audience;
- `access.global_mcp_key_restricted_reach`: the agents the global MCP endpoint's platform key runs - expose there only agents meant for every key holder;
- `access.ownerless_evaluations_restricted_reach`: evaluations with no owning user - give the evaluation an owner in each resource's audience.

#### Personal credentials used by others

An integration connected with a person's own credential - a connected account, a personal access token, an API key - lends it to everyone who can act with it: the people and groups the integration is shared with (a connected account only once its holder shared it), and the audience of every agent, assignment, flow, knowledge base, skill or General Assistant list holding it, whose runs use it as bound (of an agent bound to it, the people who can start one of its runs and its callers without an account: the people who only chat with it and cannot see the integration use their own). `access.personal_credentials_lent` is an `Info` finding listing each such integration, whose credential it is, through what it is lent and how many people (or a public widget link, people with no account on a channel, the holders of the platform MCP key) act with it; it also lists an integration whose owner acts with an account the previous owner connected. A company-managed integration's credential is the company's and is not listed. Review each in the integration's `Sharing and access` section, and where others should not act as its holder, turn on `Don't share my connected account`, switch the agent to per-user integrations, or give the work a company-managed integration.

#### Tool approvals

The two switches of [Tool approvals](#tool-approvals) are controls, `agents.tool_approvals` and `agents.tool_approvals_default`, and the audit reports which tool calls ask first:

- `agents.tool_approvals_off`: a `Warning` while approvals are off for the whole platform and an agent, or the General Assistant, would otherwise ask; `agents.tool_approvals_off_unused` is the `Info` when none would. `agents.tool_approvals_on` is the pass.
- `agents.tool_approvals_default_off`: an `Info` listing the agents that follow a platform default that asks for nothing (approvals are opt-in, so a new install starts that way on purpose and it never raises a security notification); `agents.tool_approvals_default_on` is the pass. `agents.tool_approvals_agents_opted_out` (`Info`) lists the agents set to `Don't ask`.
- While something asks, the tools that run without asking anyway: `agents.tool_approvals_bots_trusted` (`Warning`, the channel bots set to run their tools without approval), `agents.tool_approvals_platforms_skip` (`Warning`, the connected platforms whose tools run while no prompt can be shown), `agents.tool_approvals_users_allow_all` (`Info`, how many users turned on `Allow every tool for me`) and `agents.tool_approvals_whitelist_additions` (`Info`, the tools that may change data an administrator let run without asking under `Tool rules`).

#### Controls

A security control is one switch that makes the platform safer when set: an environment variable, or a setting stored in `Settings`. The `Controls` view lists every control that applies to this platform (the SSO controls, for example, only once SSO is on) with its state:

- `Enforced` - set to the value that protects.
- `Monitoring` - it records what it would block without blocking it. The tool-call budget is one: without the strict cut, a batch that crosses the budget still runs, and the overshoot is logged and recorded on the run's trace. The content security policy is another in its default `report-only` mode: browsers block nothing and report what it would block (see [Security headers](../deployment/security-headers.md)).
- `Off` - neither.

Each control shows its setting, its current value next to the value that enforces it, `Opt-in` when the platform default leaves it off, and the most severe finding about it. A control that is off while a warning or a critical finding is about it comes first, then the other controls that are off, then those that monitor and those that are enforced. Open a control to read what enforcing it changes for users, where what it would block is recorded (read it before enforcing), how to change it, the platform default and the findings about it. Filter the controls by state, category and text.

A control set by an environment variable is changed by setting the variable and restarting the platform; a stored control is changed on the Settings page it names, and a per-agent control (visitor uploads on the public chat widgets) on each agent it counts. A control that is also one of the [security options](#security-options) can be changed there too, with no restart: its details say where its value comes from (its environment variable, a value saved in `Security options`, or the default) and link to the field (`Configure <option>`), unless its variable is set - the control then shows the `Env` badge, since the variable wins. A control XTM One fixes, the quota fail mode, is always enforced in xtm_one mode: its details say it cannot be changed.

| Control | Setting | Enforced when | What enforcing it changes |
| --- | --- | --- | --- |
| `authentication.api_key_max_lifetime` | `API_KEY_MAX_LIFETIME_DAYS` | `1 to 365` | A personal API key created or given a new expiry from then on must expire within that many days, and one created without an expiry gets the longest allowed. Existing keys keep the expiry they have: the Security audit lists those that outlive the limit, for their owner to re-date or an administrator to revoke. |
| `authentication.invitation_expiry` | `INVITATION_EXPIRE_HOURS` | `<= 168` | An invitation link lasts a week at most unless the administrator who creates it picks another lifetime: an invitee who waits longer needs a new one. |
| `authentication.local_sign_in` | `local_auth_enabled` in Settings > Access and security > Authentication (Local Authentication) | `false` | Every sign-in goes through SSO, administrators included: password sign-in and self-registration are refused (an invitation still registers its invitee). Keep an administrator who signs in through SSO before turning it off, since an identity provider outage then locks everyone out. |
| `authentication.min_password_length` | `MIN_PASSWORD_LENGTH` | `>= 12` | A password set from then on (registration, invitation, password change, a local account an administrator creates) needs at least that many characters, with a letter and a digit as today. Existing passwords keep working until they change. |
| `authentication.password_reset_expiry` | `PASSWORD_RESET_EXPIRE_HOURS` | `1` | A password reset link stops working an hour after it is sent: someone who opens the email later asks for a new one. |
| `authentication.platform_token_audiences` | `XTM_AUTH_ACCEPTED_AUDIENCES` | `empty` | A token a registered OpenCTI, OpenAEV or OpenCRQ signs a user in with is accepted only when it is addressed to BASE_URL, XTM One's identity (published at /xtm/auth/metadata): a platform version that addresses its tokens to the internal URL it reaches XTM One on is refused until it reads that document. |
| `authentication.self_registration` | `registration_enabled` in Settings > Access and security > Authentication (Allow local registration) | `false` | Nobody can create an account from the sign-in page: accounts come from an administrator, an invitation or SSO. |
| `authentication.session_idle_timeout` | `SESSION_IDLE_TIMEOUT_MINUTES` | `5 to 30` | A session nobody uses for that long ends: the web app signs the person out and they sign in again. Clicking, typing, scrolling or coming back to the tab counts as use; what the web app does on its own (refreshing a page, the activity stream, an answer streaming in the chat) does not. The web app warns a minute before. While Redis cannot be reached, sessions are not ended for inactivity. |
| `authentication.session_lifetime` | `JWT_EXPIRE_MINUTES` | `<= 1440` | People sign in again at least every day, even while they use the platform (through their identity provider with SSO), and a stolen session token stops working within a day. Lowering the lifetime ends the sessions already older on their next request; raising it applies from the next sign-in. API keys and platform tokens are not sessions and are not affected. |
| `authentication.sso_account_creation` | `oidc_auto_create_users`, `saml_auto_create_users`, `allowed_domains` in Settings > Access and security > Authentication (On-the-fly user creation, Allowed email domains) | `false, or allowed email domains set` | SSO no longer creates an account for everyone the identity provider authenticates. With On-the-fly user creation off, only people who already have an account, matched by SSO subject or email, can sign in through SSO; anyone else is refused until an administrator or an invitation creates the account. With allowed email domains set, SSO creates an account only for an email address, as the identity provider asserts it, at one of those domains. Existing accounts keep signing in whatever their domain. With XTM_HUB_ADMIN_GROUP set, the XTM Hub decides who may sign in through OIDC. |
| `authentication.sso_auto_admin` | `oidc_auto_admin_new_users`, `saml_auto_admin_new_users` in Settings > Access and security > Authentication (Automatically admin users) | `false` | People who sign in through SSO keep the role an administrator gave them. While it is on, everyone who signs in through that method is made an administrator, existing accounts included and at every sign-in; turning it off demotes nobody. With XTM_HUB_ADMIN_GROUP set, the XTM Hub decides the role of an OIDC sign-in instead. |
| `agents.tool_result_guard_scope` | `PROMPT_GUARD_SCANNED_TOOLS` | `the tools that return outside content` | While a prompt guard (Nova) is connected, it evaluates what the listed tools return to an agent, the text of the files they attach included, and records each rule match in the audit log. The evaluation runs beside the agent, so it adds no wait unless tool results a block rule matches are also withheld. |
| `agents.tool_result_guard_blocking` | `PROMPT_GUARD_BLOCK_TOOL_RESULTS` | `true` | A screened tool result that a block rule matches is withheld: the agent reads a short refusal naming the tool instead, without the files the result carried. The agent then waits for the guard's verdict on each screened result before it continues. Monitoring while tools are screened and it is off: each match is recorded, the result reaches the agent. |
| `access.agent_tools_strict_admin_access` | `AGENT_TOOLS_STRICT_ADMIN_ACCESS` | `true` | An admin's agent tools (the agent builder in chat and on the XTM One MCP server, the work-items delegation) read, trigger, edit, delete or add assignments only where the admin's own, group-shared and company-managed objects reach; admins keep full access through the UI and the REST API. |
| `access.presentation_public_links` | `PRESENTATION_PUBLIC_LINKS_ENABLED` | `false` | Nobody can open a presentation without an account: every [public link](../usage/presentations.md#public-link) answers as if it did not exist, at once, and neither the Share dialog nor an agent can create one. The links are kept, so turning the option back on brings back those that were not turned off. |
| `access.video_public_links` | `VIDEO_PUBLIC_LINKS_ENABLED` | `false` | Nobody can watch or download a video without an account: every [public video link](../usage/video-studio.md#public-link) answers as if it did not exist, at once, and neither the Share dialog nor an agent can create one. The links are kept, so turning the option back on brings them back. |
| `access.dashboard_public_links` | `DASHBOARD_PUBLIC_LINKS_ENABLED` | `false` | Nobody can open a custom dashboard without an account: every [published link](../usage/custom-dashboards.md#published-link) answers as if it did not exist, at once, and none can be published. The links are kept, so turning the option back on brings them back. Off by default. |
| `agents.scheduled_work_items` | `SCHEDULED_WORK_ITEMS_ENABLED` | `false` | No agent can [schedule a one-off action](../usage/scheduled-work-items.md) from a chat and nothing already scheduled is run: every agent's scheduling tools answer that scheduling is disabled by the administrator. Items that come due meanwhile are marked missed and their creators notified; turning the option back on lets them reschedule. Recurring assignments are not affected. |
| `agents.prompt_guard_blocks_general_assistant` | `PROMPT_GUARD_BLOCK_GENERAL_ASSISTANT` | `true` | While a prompt guard (Nova) is connected, a General Assistant message one of its block rules matches is refused with the guard's message before the model is called, instead of being answered. A false positive refuses a colleague's question. |
| `agents.prompt_guard_blocks_public_widget` | `PROMPT_GUARD_BLOCK_PUBLIC_WIDGET` | `true` | While a prompt guard (Nova) is connected, a public chat widget message one of its block rules matches is refused with the guard's message before the model is called, instead of being answered. A false positive refuses a visitor, who has nobody to ask. |
| `agents.prompt_guard_model_output` | `PROMPT_GUARD_MODEL_OUTPUT` | `block` | While a prompt guard (Nova) is connected, every model answer is evaluated before it is delivered, and one of its block rules matches is replaced by a withheld notice: answers take the evaluation's time longer. A streamed answer (web chat, the streaming chat embedded in OpenCTI / OpenAEV, the widget's stream) has already been shown, so it is only audited. `log` is the monitoring state. |
| `agents.tool_approvals` | `tool_approvals_enabled` in Settings > Access and security > Tool approvals | `true` | A tool call that asks first waits until a person approves it, in chat and in assignment runs, for every agent that asks before running tools. Suspended, no tool call waits for anyone and tools that change data run without asking; the tool rules, the connected platforms' policies and every personal approval are kept for when approvals resume. |
| `agents.tool_approvals_default` | `approve_tool_calls_default` in Settings > Access and security > Tool approvals (Every agent asks by default) | `true` | Every agent that follows the platform default, and the General Assistant where chat is available, wait for a person to approve each tool call that asks first; an agent set to Ask or Don't ask keeps its own choice. Tool approvals are opt-in: a new install starts with it off, and an upgrade keeps the value already stored (on where an administrator, or an earlier release creating the settings, turned it on), so that no running agent starts asking on its own. Listed only while tool approvals are not suspended. |
| `network.trust_proxy_headers` | `TRUST_PROXY_HEADERS` | `false` | X-Forwarded-For and X-Forwarded-Proto count only from a proxy FORWARDED_ALLOW_IPS names, never from any peer. Declare the proxy there first: without it, rate limits bucket every caller under the proxy's address and HTTPS behind it is unconfirmed. |
| `network.client_ip_strict` | `CLIENT_IP_STRICT` | `true` | Every client address is keyed, logged and stored in canonical form - port, brackets and IPv6 zone index dropped, an IPv4-mapped address read as IPv4, IPv6 lowercase - so one client is one rate-limit key whatever the proxy wrote. A client whose address arrived in another spelling has its current rate-limit window restart once. |
| `network.rate_limit_ipv6_prefix` | `RATE_LIMIT_IPV6_PREFIX` | `<= 64` | The per-IP rate limits key a native IPv6 client on its network (its /64 at 64) instead of its address, so rotating addresses within it draws no fresh bucket. Every device of one /64, a corporate IPv6 LAN included, then shares one budget. |
| `network.credential_transport_strict` | `CREDENTIAL_TRANSPORT_STRICT` | `true` | Runner enrollment (POST /runners/enroll), MCP OAuth client registration (POST /mcp/oauth/register) and the MCP OAuth token endpoint (POST /mcp/oauth/token) refuse a request the platform cannot confirm arrived over HTTPS - 403 https required, the OAuth invalid_client_metadata error and the OAuth invalid_request error - instead of serving it. Behind a TLS-terminating proxy, FORWARDED_ALLOW_IPS must name it first, or every runner enrollment, MCP client registration and MCP token request through it is refused; a deployment served over plain HTTP can no longer enroll runners, register MCP clients or issue MCP tokens. |
| `network.relay_version_floor` | `RELAY_MIN_SUPPORTED_VERSION` | `a relay version (e.g. 1.261002.1)` | A relay older than the floor is refused when it connects and asked to update: one that updates itself does so at its next check, one in a container needs a newer image. Until then its group carries no request through it. |
| `network.secret_vault_trusted_origins` | `SECRET_VAULT_TRUSTED_ORIGINS` | `empty` | A vault connected in Settings > Access and security > Secret vaults is never reached on a loopback address; only a vault on the platform's own host (a Vault Agent, a 1Password Connect server beside it) needs its origin listed. Link-local and cloud metadata addresses stay refused whatever the list holds, and vaults declared in the environment are not affected. See [Secret vaults](../deployment/secret-vaults.md#network-and-tls). |
| `web.content_security_policy` | `CONTENT_SECURITY_POLICY_MODE` | `enforce` | Browsers refuse to run a script or load a resource the platform's pages do not need, a script injected past the output checks or added by a proxy or CDN included, and to show the pages in the iframe of a site the policy does not allow. The API reference, the Teams tab and the Office add-in stay report-only. |
| `web.cors_origins` | `CORS_ORIGINS` | `a list without *` | Only the listed origins may call the API from a browser page; a page on any other origin is refused by the browser. |
| `web.csp_frame_ancestors` | `CONTENT_SECURITY_POLICY_FRAME_ANCESTORS` | `a list without *, a bare scheme or a wildcard host` | Once CONTENT_SECURITY_POLICY_MODE=enforce, only the platform itself, the sites listed and, behind a registration token of the operator's own, the registered platforms may show its pages in an iframe: list a portal that embeds the platform before enforcing, or it is refused. |
| `web.public_widget_strict_rendering` | `PUBLIC_WIDGET_STRICT_RENDERING` | `true` | Public chat widget answers show only the images the platform serves (any other image becomes a link), open every link in a new tab, and drop inline styles, SVG, MathML and links other than http(s), mailto and tel. |
| `web.public_widget_uploads` | `web_uploads_enabled` on each agent, in Agents > the agent > Configuration > Web Exposure (Allow visitor uploads) | `false on every widget` | Visitors of the public chat widgets, who have no account, can only type: a widget that let them attach a document or an image to a message no longer offers it. The files already attached stay readable by the agent until the retention deletes them. Its value is how many live widgets allow uploads; see [Visitor uploads on the public chat widget](../deployment/configuration.md#visitor-uploads-on-the-public-chat-widget). |
| `integrations.oauth_token_url_strict` | `OAUTH_TOKEN_URL_STRICT` | `true` | Every OAuth token request (code exchange and refresh, for integrations and MCP servers) and every save of a token URL also refuses a token URL whose host is, or resolves to, a loopback, link-local, unspecified, reserved or multicast address, and any scheme but http(s); private networks stay allowed. An integration or MCP server whose token URL is on loopback stops exchanging and refreshing tokens until the URL points at another address of its identity provider or, on a development machine, its origin is listed in the loopback token URL origins. |
| `integrations.oauth_token_dev_trusted_origins` | `OAUTH_TOKEN_DEV_TRUSTED_ORIGINS` | `empty` | Under the strict OAuth token URL checks, every OAuth token request (code exchange and refresh, for integrations and MCP servers) refuses a token URL that resolves to a loopback address; only local development against an identity provider on the same machine needs the list. Without the strict checks the list has no effect. |
| `mcp.server_url_policy` | `MCP_SERVER_URL_POLICY` | `private_networks or public_only` | Saving a remote MCP server, and every connection the platform opens to one (redirects and OAuth discovery included), also refuses a URL whose host is, or resolves to, a loopback, link-local, unspecified, reserved or multicast address; public_only refuses private networks too. Cloud metadata addresses are refused in every mode. A server already saved at a refused address stops connecting until its URL changes, its origin is listed in the trusted MCP server origins, or it is reached through a relay group. |
| `mcp.server_trusted_origins` | `MCP_SERVER_TRUSTED_ORIGINS` | `empty` | Every remote MCP server is held to the MCP server URL policy; a listed origin is otherwise reached at any address but a cloud metadata one. |
| `agents.nova_llm_every_condition` | `NOVA_LLM_MAX_CONDITIONS` | `0` | While the Nova integration runs its full engine, every distinct llm: condition of the enabled rules is put to the model on each message it screens, one call per condition, as it always was. With a ceiling set, the conditions past it are not asked (those of block rules are kept first), and a rule resting on one cannot fire through that tier. |
| `assignments.webhook_require_secret` | `WEBHOOK_REQUIRE_SECRET` | `true` | A delivery to an assignment webhook with no secret configured is refused (401) instead of run: such an assignment stops triggering until it has a secret and the sender signs with it. |
| `tools.code_interpreter_disabled` | `DISABLE_CODE_INTERPRETER` | `true` | Agents and the General Assistant can no longer run Python in the code interpreter. |
| `tools.code_interpreter_extra_modules` | `CODE_INTERPRETER_EXTRA_ALLOWED_MODULES` | `empty` | The code interpreter keeps its whole module blocklist: code it runs cannot open network connections (it shares the host's network). |
| `tools.sandbox_command_declared_only` | `CODE_INTERPRETER_COMMAND_ALLOW_ARGV` | `false` | `sandbox_run_command` runs the checks a checked-out repository declares in `.xtm-one/checks.toml` and nothing else: an agent cannot run a command of its own in the sandbox container. The container has no network and no credential either way. |
| `tools.custom_tool_extra_modules` | `CUSTOM_TOOL_EXTRA_ALLOWED_MODULES` | `empty` | Python custom tools keep their whole module blocklist: in the subprocess fallback they cannot open network connections. |
| `tools.custom_tool_node_permission` | `CUSTOM_TOOL_NODE_PERMISSION` | `true` | A JavaScript custom tool run in the subprocess fallback gets no file system, child process, worker thread or native addon access (Node.js 24 or later); the network stays available. |
| `tools.custom_tool_sandbox` | `CUSTOM_TOOL_SANDBOX` | `docker or opensandbox` | Custom tools run in the hardened Docker container or an OpenSandbox container only: when that is unreachable they fail instead of falling back to a plain subprocess. |
| `tools.custom_tools_disabled` | `DISABLE_CUSTOM_TOOLS` | `true` | No custom tool (JavaScript or Python written by a user) runs anywhere: chat, assignments, flows and tests. |
| `tools.custom_variables_disabled` | `DISABLE_CUSTOM_VARIABLES` | `true` | The Variables feature is refused: its API and the agent builder's variable tools. |
| `tools.runner_result_bound` | `RUNNER_MAX_RESULT_BYTES` | `> 0` | A runner file read never asks for more than the bound or 256 KiB, whichever is larger (text up to 256 KiB comes back inline, which the bound does not cap), and a larger file result is refused, so an agent cannot make a runner read a large file into memory. |
| `tools.runner_strict_path_confinement` | `RUNNER_STRICT_PATH_CONFINEMENT` | `true` | A restricted runner refuses a file command whose path leads outside its allowed folders through a symbolic link, junction or mount point, instead of running it with a warning (runners from this release or later). |
| `tools.svg_strict_rendering` | `SVG_STRICT_RENDERING` | `true` | An SVG a model is shown that declares an XML entity or references anything outside itself (a file path, a URL, another document) is not rendered at all, instead of being rendered with that reference dropped. Nothing is fetched or read from disk in either mode. |
| `tools.tool_call_budget_strict` | `AGENT_LOOP_MAX_TOOL_CALLS_STRICT` | `true` | A parallel batch of tool calls that crosses a run's budget runs only the calls that still fit; the others are answered "Not executed" instead of running. |
| `mcp.a2a_dev_trusted_origins` | `A2A_CLIENT_DEV_TRUSTED_ORIGINS` | `empty` | The A2A client reaches every partner over HTTPS at an address its destination guard accepts; only two deployments tested on one machine need the list. |
| `mcp.a2a_insecure_loopback` | `A2A_ALLOW_INSECURE_LOOPBACK` | `false` | A plain HTTP request from 127.0.0.1 or ::1 no longer counts as encrypted on the A2A surface. Behind a reverse proxy on the same host every partner request arrives from loopback, so the setting would waive HTTPS for all of them. |
| `mcp.client_id_metadata_documents` | `MCP_CIMD_ENABLED` | `false` | A client naming its own metadata document as its client_id (claude.ai, Claude Code, VS Code) is refused at /mcp/oauth/authorize and its tokens no longer refresh: it then needs dynamic registration or an API key. The platform no longer fetches client documents. |
| `mcp.dynamic_client_registration` | `MCP_DCR_ENABLED` | `false` | MCP clients can no longer register themselves (POST /mcp/oauth/register answers 403): an MCP client such as Claude Desktop or Cursor then needs an API key. Clients registered before keep working. |
| `mcp.outbound_dynamic_client_registration` | `MCP_OUTBOUND_DCR_ENABLED` | `false` | The platform no longer registers itself with a remote MCP server's authorization server: connecting an OAuth MCP server needs a client ID provisioned by hand. |
| `mcp.test_only_insecure_transport_peers` | `TEST_ONLY_INSECURE_TRANSPORT_PEERS` | `empty` | No network is exempted from the A2A HTTPS requirement: the list exists for the end-to-end test suite only. |
| `mcp.xtm_mcp_server` | `xtm_mcp_enabled` in Settings > MCP Endpoint | `false` | External MCP clients no longer reach this instance's own tools at /mcp/xtm: traces, the agent builder, knowledge search and skills, each within the caller's own rights. |
| `llm.trace_payloads` | `trace_store_payloads`, `trace_storage_enabled` in Settings > Observability (Store full payloads, Enable local trace storage) | `false, or trace storage off` | LLM traces no longer keep what was said: each call's prompts, answers, tool arguments and tool results are dropped, and an error message keeps its first 200 characters. Traces still show the timings, token counts, cost, model, tool names and status; with trace storage off, no trace is stored at all. It applies to the traces written from then on, on every server within seconds: traces stored before keep what they hold until the retention deletes them, and the Langfuse export, while it is on, still receives each call's payloads. |
| `infrastructure.debug_mode` | `DEBUG` | `false` | SQL statements are no longer echoed to the log with the tokens and encrypted values they carry, loopback no longer counts as HTTPS, password reset links are no longer written to the log when no SMTP server is set, and development-only tools are gone. |
| `infrastructure.quota_fail_mode` | `QUOTA_FAIL_MODE` | `closed` | While the quota store cannot be reached, agentic executions are refused instead of running unmetered; a due assignment run or background task is held until the store answers again. Always enforced in xtm_one mode, where quotas are part of the licence and it cannot be changed; configurable in copilot / dev mode. |
| `infrastructure.rate_limit_fail_mode` | `RATE_LIMIT_FAIL_MODE` | `closed` | While Redis cannot be reached, rate-limited requests (sign-in, password reset, webhooks, chat messages) are refused with a 503 instead of passing unthrottled. |

The `Enable local trace storage` and `Store full payloads` switches of `Observability` are one control, `llm.trace_payloads`: it is enforced while either is off. By default traces keep their payloads, which the audit reports as information with the retention period; with payloads or storage off, it reports the pass.

The same report is available to scripts at `GET /api/v1/security-audit`, for administrators only. Every finding carries an `id` (for example `infrastructure.debug_mode`) that stays the same from one run to the next, so a script can follow a finding without relying on its title, which may include a count, and a finding about a control names it in `control`. `controls` lists every control that applies with its `state`, its `value`, `default` and `hardened` value and whether it is an `opt_in`, and `controls_summary` counts the controls per state.

### Security options

`Security options` holds the platform-wide security switches: trusted proxy headers (prefer `FORWARDED_ALLOW_IPS`), canonical client addresses, HTTPS required for runner enrollment and MCP tokens (off by default: served and logged as before) and the IPv6 rate-limit prefix length, what rate limits and quotas do when their store cannot be read, the password length and the reset / invitation link lifetimes, the absolute session lifetime and the idle session timeout (see [Session lifetime and idle timeout](../deployment/authentication.md#session-lifetime-and-idle-timeout)), MCP client registration, the strict OAuth token URL checks and the loopback token URL origins used in development under them, the A2A partner origins trusted in development and the loopback exemption used in tests, required webhook secrets, required verification of integration notifications (Slack, Microsoft Graph and LinkedIn deliveries the platform cannot verify - the Microsoft Graph notifications of the Teams bot's private and shared channels included, see [Microsoft Teams](channels/microsoft-teams.md#graph-notifications-for-private-channels) - and the admin OAuth app's deliveries to other integrations: accepted as before and reported while it is off, refused when it is on), strict widget rendering, strict runner folder confinement, strict admin access for agent tools, the prompt guard switches (whether it refuses the General Assistant and widget messages its block rules match, whether it evaluates model answers, the tools whose results it screens and whether it withholds what a block rule matches), the code interpreter / custom tools / custom variables switches and the sandbox module and Node.js settings. Select `Configure`, change what you need and `Save`; the reset button next to a saved value returns it to its default. Every change is recorded in the audit log and applies on every server within a few seconds.

`Tools screened by the prompt guard` is empty by default: no tool result is screened. While a company-managed Nova integration is connected, the guard evaluates what a listed tool returns to an agent, the text of the files the result attaches included (images excluded), and records each rule match in the audit log as a `Security alert` (surface `tool_result`). The result reaches the agent as it is, and the evaluation runs beside the agent, which does not wait for it. With `Withhold tool results a guard rule blocks` on, a screened result that a block rule matches is withheld instead - the agent reads a short refusal naming the tool, without the result's files - and the agent waits for the guard's verdict on each screened result before it continues. In `Configure`, pick the tools from the list: built-in tools by name, integration and MCP tools by the name the platform gives them, custom tools by their id, shown under their current name - a custom tool stays screened when its owner renames it, with nothing to update here. A listed entry that no tool carries anymore (a deleted custom tool, a server that no longer offers the tool) is flagged so it can be removed.

Each option can also be set by an environment variable, and the variable wins: an option set that way shows an `Env` badge and "Set by environment variable X", and cannot be changed here until the variable is removed from the deployment. The full list, with the variables and defaults, is in [Configuration](../deployment/configuration.md#settings-security-options).

In XTM One, `Quotas when their store is unreachable` is always closed: quotas are part of the licence, so agentic work is refused while their store cannot be reached. A due automation is held instead - shown as **Held**, charged once it runs, and run by itself once the store answers again (bounded by `AI_UNAVAILABLE_HOLD_MAX_SECONDS`). A store that refuses the platform's credentials (a wrong password, an ACL denial) is a configuration error, not an outage: that work is refused at once. The option shows a `Read-only` badge with that reason and cannot be changed, and its variable, `QUOTA_FAIL_MODE`, has no effect (set to `open`, the platform ignores it and logs a warning at startup).

The options are grouped under the categories of the security audit (`Authentication`, `Sharing and access`, `Network`, `Browser security`, `Integrations`, `Agents`, `Assignments`, `Tools`, `MCP and A2A`, `Infrastructure`): each under the category of the control it is.

Every option is also a security control, reported in the audit's `Controls` view with the value it resolves to (variable, saved value, then default), where that value comes from, and the `Env` or `Read-only` badge of a locked one. An option the security audit checks links to its finding: `Security audit` next to it opens the audit on that finding. The other way round, a finding whose recommendation names one of these options names the field and links to it: `Configure <option>` opens `Security options` on that field, ready to change. A finding whose option is set by its variable names only the variable and has no link, since the variable is what to change.

### Tool approvals

`Tool approvals` is the one place for the platform's side of tool approvals, which are opt-in. From the top:

- **Tool approvals**: whether approvals are `In use` (an enabled agent or the General Assistant asks before running tools), `Not in use` or `Suspended`, with the agents that ask, each linking to its setting, and the `Every agent asks by default` switch that every agent left on `Follow the platform default` follows (the General Assistant included, where chat is available). A new install starts with it off; an upgrade keeps the value already stored, so an install that turned it on, or that an earlier release created with it on, keeps it on.
- **Tool rules**: every tool this platform can name and whether it asks first, with `Let it run without asking`, `Make it ask first` and `Back to the default` per tool, `Add a tool by name` for one not connected yet, and `Make all its reads ask first` per connected platform. The rules can be prepared before anything asks; the card says they change nothing until then.
- **Approval for connected platforms**: per connected platform, whether its tools ask while it cannot show a prompt.
- **Suspend all approvals**: the emergency switch that lets every tool call run without asking, for everyone, until approvals are resumed.

Each change asks for confirmation and is recorded in the audit log. Each agent's own setting is under the agent's `Configuration` > `Tooling`, and each person's own approvals under `Profile` > `Tool approvals`. See [Human approval of tool calls](ai-governance.md#human-approval-of-tool-calls) for what asks first and why.

### Authentication

`Authentication` controls sign-in and identity behavior:

- local authentication rules
- registration policy
- OpenID Connect
- SAML 2.0
- SAML group mapping
- allowed email domains

See [Authentication](../deployment/authentication.md) for every setting.

### Secret vaults

`Secret vaults` connects your enterprise secret managers, so that a secret field (of an integration, an MCP server, a variable, an LLM provider...) stores a reference to a secret kept in your vault instead of its value. See [Secret vaults](../deployment/secret-vaults.md).

### API keys

`API keys` lists every personal API key of the platform, whoever owns it: its owner (and whether that account is deactivated), its prefix, what it reaches (`Full access`, or its surfaces, `Read-only` and its agents), its expiry, its own limits, its request count, its last use and when it was created. The key itself is never shown: it is stored hashed and only its owner saw it, once, when it was created. See [API Keys tab](../usage/my-profile.md#api-keys-tab) for what each setting means.

Filter by status (`All`, `Active`, `Expired`, `Revoked`), by exposure (`No expiry`, `Full access`, `Beyond the maximum lifetime`, the keys of active accounts the [security audit](#personal-api-keys) reports), by owner (one or several users) and by key name, prefix or owner name. The status, exposure and owner filters are kept in the address (`/settings/security/api-keys?status=active&issue=no_expiry&owner=<user id>`), so you can link to them.

`Revoke` stops a key at once, on every surface, and notifies its owner in the notification bell. A revoked key stays listed as revoked; its owner can delete it but not reactivate it. The key provisioned by `ADMIN_TOKEN` cannot be revoked here: unset the variable to remove it. Revoking is recorded in the audit log, like creating, changing and deleting a key.

A user's own keys are also on the `API Keys` tab of their page in `Users`, with the same `Revoke`.

Scripts use `GET /api/v1/admin/api-keys` (with `status`, `issue`, `search`, `owner_id` - repeat it for several owners -, `limit` and `offset`; `with_summary=true` adds totals over every matching key, not only the page) and `POST /api/v1/admin/api-keys/{id}/revoke`.

### Emergency stop

`Emergency stop` holds the emergency stop API token: generate, rotate or revoke
the token that presses the [emergency stop](emergency-stop.md) from outside a
session, and print the emergency procedure to keep on paper. The token is shown
once; the card then shows only its prefix, who generated it, and when it was last
used.

### Memory purge

`Memory purge` permanently deletes every agent memory on the platform, for example after discovering a flaw in the memory system. The purge cannot be undone and is recorded in the audit log.

## Storage & retention

`Settings > Usage and quotas > Storage & retention` (administrators) lists every category of stored file with how long it is kept, from when the days count, how many files and how much space it holds today, how many were deleted, and what the last hourly sweep deleted.

| Category | Holds | Default |
|---|---|---|
| Agent working files | scratch files assistants write while they work | 60 days after the last use |
| Agent deliverables | files assistants hand to people to download | 60 days after the last use |
| Other agent files | files tools and MCP servers return | Never |
| Chat uploads | files people attach in a conversation | Never |
| Channel files | files received from Teams, Outlook and the other channels | Never |
| Check pictures | frames and captures a tool makes for the assistant to look at | 7 days after creation |

A use is a download, a preview, a read by an assistant or an attach; it starts the days again. A file an open work item, a workset, an active shared state, a presentation, a report or a video still uses is never deleted. A deleted file keeps its row: the chat, the files panel, run attachments and the Files page show a muted card with when and why, its download answers `410 Gone`, and an assistant reading it is told the file was deleted.

`Edit` turns each value into a field (days; `0` = never). A value shorter than the current one deletes files at the next sweep, so `Save` waits for `Preview`, which counts per category the files and the space the next sweep would delete and how many it keeps because they are in use. Every change is recorded in the audit log with its old and new values. A category whose environment variable is set (`FILE_RETENTION_*_DAYS`, `AGENT_CHECK_FILE_RETENTION_DAYS`, see [configuration](../deployment/configuration.md)) is shown locked with the variable's name.

The same values are read and written through `GET` / `PUT /api/v1/settings/file-retention`, `POST /api/v1/settings/file-retention/preview`, and read by any signed-in caller through `GET /api/v1/settings/file-retention/rules`.

## Observability

`Observability` covers tracing, model costs and usage reporting. It holds three cards.

The observability setup, edited with `Configure`:

- `Enable local trace storage`: whether LLM traces are stored at all. Traces are kept in PostgreSQL, or in ClickHouse when the deployment configures one (see [Configuration](../deployment/configuration.md#observability)); the card says which, and whether the store answers.
- `Store full payloads`: whether a trace keeps what was said (prompts, answers, tool arguments and tool results) or only its timings, token counts, cost, model, tool names and status
- `Trace retention (days)`: how long stored traces are kept
- `Audit log retention (days)`: how long audit log entries are kept, 90 days by default, from 30 to 3650 days. An hourly job deletes the older entries in batches, and keeps each user's most recent sign-in so their last login stays known. See [Logs](introduction.md#logs).
- Langfuse configuration, the optional export to Langfuse, which the two switches above do not govern

A change to the two switches applies within a few seconds on every server, to the traces written from then on; traces already stored keep what they hold until the retention deletes them. See [What a trace keeps](../deployment/monitoring.md#what-a-trace-keeps). The card also warns when the daily cost aggregation has not run for more than 48 hours, when prices have not been updated for a while, and lists the models seen in traces that have no price.

`Model Pricing` holds the price of each model, which gives traces their cost:

- `Sync detected seed` loads the prices from the seed file the platform ships with; `Import JSON file` loads them from a file of yours. A price you edited by hand is kept: the result counts it as `Custom skipped`.
- `Manage model prices` opens the price table, where you search, add and edit rows by provider and model (a model can be matched by a pattern).
- `Reprice $0 traces` gives a cost to the traces recorded at $0 because their model had no price at the time, and recomputes the daily totals. With `Only from the price's effective date`, only traces recorded after the price took effect are repriced.

`Departments` maps people to your organization's departments, so that usage and cost can be reported per department. See [Departments](../deployment/monitoring.md#departments).

`Model Pricing` and `Departments` stay editable while AI is turned off.

## Quality

`Quality` defines the feedback criteria: what people score when they rate an agent's work, on top of the thumbs up or down. Once a criterion exists, the rating dialog asks for it after either thumb, and each score can be skipped.

A criterion has a kind, fixed once it is created:

- `Scale`: a whole step between two bounds, such as 1 to 5
- `Categorical`: one of a few options, each worth a score from 0 to 1 (a `warm` tone can be worth 1 and a `cold` one 0)
- `Numeric`: any number between a minimum and a maximum

Each criterion also has a weight. A rating's weighted score is the weighted mean of its scores, each read on a common 0 to 1 scale; a rating with no score is worth its thumb (1 for up, 0 for down), and so is every rating while no criterion exists. A weight of 0 keeps a criterion visible without counting it, but the enabled criteria cannot all weigh 0.

Switch a criterion off to stop asking for it while keeping the scores already given. Deleting it also deletes every score given on it. The agent's `Feedback` tab shows, per criterion, how many scores it received, their average, their distribution, and the daily average over the last 30 days.

## What regular users should know

If you are not an administrator, you may only see `AI Models` and `Channels & Bots`, or even fewer options depending on deployment. `AI Models` and the model pickers list only the providers and models you may use.

That does not mean your account is broken. It only means shared configuration is intentionally restricted.

## Next step

The [`AI Catalog`](../usage/ai-catalog.md) page explains where you can browse published resources and add them to your account.

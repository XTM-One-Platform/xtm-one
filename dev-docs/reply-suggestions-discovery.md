# Reply suggestions in the chat composer: discovery

Status: v1 built (backend: `backend/app/services/chat_reply_suggestions.py`, contract in `dev-docs/reference/backend-services-chat.md`; composer: `@filigran/chatbot`). This note keeps the discovery and the decisions; section 5 says what v1 does differently from the proposal. Feature asked for: once an agent has answered in a conversation, the platform **Routing Model** proposes the user's next message as a suggestion in the composer; **Tab** puts it in the input. The request also asks whether the agentic loop should end with a 2-3 sentence recap of what was done and what could come next.

This note gathers the state of the art (sources checked 2026-11-19), what the codebase already offers, and a proposed definition with the decisions still open.

## 1. State of the art

### 1.1 Products

| Product | Form | Acceptance | How it is generated | Source |
|---|---|---|---|---|
| Claude Code (CLI) | ONE grey ghost-text prompt in the input, after each answer | `Tab` or `→` fills the input, `Enter` sends; typing dismisses | "a short background request to the same model", reusing the conversation's prompt cache. Skipped when the cache is cold, after an error, in plan mode, near the usage limit, on very short conversations. Shown less often after many unused suggestions in a row; using one brings them back. Can be turned off by the user and by managed settings | https://code.claude.com/docs/en/interactive-mode#prompt-suggestions |
| Qwen Code (open source, same pattern) | ONE ghost text, ~300 ms after the answer ends, never while streaming | `Tab` / `→` fills, `Enter` sends | optional small `fastModel` with thinking off; needs ≥ 2 model turns, no error, no pending dialog. Output filter: 2-12 words, < 100 chars, no evaluation ("looks good"), no AI voice ("Let me…"), no markdown, no meta text, one sentence | https://cdn.jsdelivr.net/npm/@qwen-code/qwen-code@0.17.0/bundled/qc-helper/docs/features/followup-suggestions.md |
| Open WebUI | chips under the answer; the first one also as ghost text in the empty input | `Tab` inserts the ghost text; a chip click sends (or prefills, by setting) | a separate "task model" | https://docs.openwebui.com/features/chat-conversations/chat-features/follow-up-prompts.md |
| ChatGPT | follow-up suggestions (since Aug 2023), a user toggle | click | no public write-up | https://community.openai.com/t/disable-or-customize-the-follow-up-suggestions/1254246 |
| Perplexity | "Related" questions under each answer | click sends | no public write-up; ~40% of queries reportedly come from them (Adweek, one quote, metric unclear) | https://www.adweek.com/media/gen-ai-search-engine-perplexity-has-a-plan-to-sell-ads/ |
| Microsoft Copilot (Teams meetings, Dynamics 365) | up to 3 suggested questions above the chat box, on by default | click | - | https://learn.microsoft.com/en-us/dynamics365/release-plan/2024wave2/finance-supply-chain/dynamics365-supply-chain-management/keep-conversations-going-ai-generated-follow-ups |
| VS Code chat API (Copilot) | `ChatParticipant.followupProvider` → `ChatFollowup { prompt, label }` | click sends | provided by the participant after its result | https://raw.githubusercontent.com/microsoft/vscode/main/src/vscode-dts/vscode.d.ts |
| Gmail Smart Compose | inline ghost text while typing | `Tab` on desktop, swipe on mobile | - | https://support.google.com/mail/answer/9116836 |

Frameworks: CopilotKit `useCopilotChatSuggestions` (experimental), assistant-ui `adapters.suggestion.generate` run after each assistant turn and cleared on a new run, Vercel AI Elements `Suggestion` chips (UI only), Bot Framework `suggestedActions` (they vanish once one is picked, so a stale one cannot be clicked). OpenAI ChatKit has starter prompts only.

Two families: **chips under the answer** (several, clicked to send: ChatGPT, Perplexity, Copilot) and **one ghost text in the input** (Tab to fill, Enter to send: Claude Code, Qwen Code, Open WebUI). The request is the second one.

### 1.2 What the ghost-text products agree on

- One suggestion, made AFTER the turn by a cheap side call, never during streaming, never blocking the turn's end.
- `Tab` (or `→`) only fills the input; `Enter` sends. Claude Code first had `Enter` accept-and-send in one key; users asked for the two to be separate (accepting must never execute).
- Typing, a new turn or `Esc` dismisses it.
- **Silence is a valid answer**: no suggestion after an error, a cancellation, a pending dialog, or when the next step is not obvious.
- A hard output filter: short, in the USER's voice ("I was just about to type that"), not the assistant's.
- Fewer suggestions when the user keeps ignoring them; an off switch for the user and for the organisation.

### 1.3 Research and lessons

- **Smart Reply** (Kannan et al., KDD 2016, https://arxiv.org/abs/1606.04870): a separate triggering model decides whether to suggest at all (fired on ~11% of emails, 10% of mobile replies assisted); never triggered on open-ended questions or sensitive topics; the most probable reply is often low quality, so suggestions came from a vetted response space.
- **Smart Compose** (Chen et al., KDD 2019, https://arxiv.org/abs/1906.00080): top-1 only, shown only above a confidence threshold tuned for coverage; suggestions with gendered pronouns removed after a bias finding; memorisation tested for privacy.
- **Bing "People also ask"** (Rosset et al., WWW 2020, https://www.microsoft.com/en-us/research/?p=630582): judge a suggestion by its usefulness for the NEXT step, not by relevance; training on real next turns gave +8% clicks.
- **Follow-ups as a satisfaction signal** (Kim et al., 2024, https://arxiv.org/abs/2407.13166): unsatisfying conversations carry more clarifying queries and reactions to the answer, so what the user sends next is itself a quality signal.
- **NN/g, "Prompt suggestions"** (2025, https://www.nngroup.com/articles/prompt-suggestions/): follow-ups are the most useful type of suggestion; badly placed or generic ones are ignored.
- **Copilot completions** (Ziegler et al., 2022, https://arxiv.org/abs/2205.06537): the acceptance rate of shown suggestions is the best predictor of perceived usefulness.
- No public A/B test of LLM next-message ghost text was found.

### 1.4 Recap step: what exists

- Nothing found shows that a recap step improves the suggestion. Claude Code makes its suggestion directly from the conversation; its recaps (an "away summary" under 40 words, a few-word activity label) are separate side calls, never a step of the agent loop.
- The visible "recap + next step" products have it emitted as a structured end-of-turn output and shown to the user, not as an input of the suggester.
- Rewriting the agent's history to insert a summary breaks prompt caching and thinking blocks (Anthropic recommends append-only history): a recap must stay a side call, out of the transcript.
- The "Would you like me to…" endings that an agent adds by instruction are widely criticised (ChatGPT dropped them in 2026, https://pcworld.com/article/3085980/chatgpt-quietly-fixed-its-most-annoying-habit.html): a separate generator is the better place for "what next".

### 1.5 Risks

- **One key away from a dangerous action.** User-reported Claude Code issue: in a repository where a push to main deploys, the suggestion kept offering "push it" (https://github.com/anthropics/claude-code/issues/92348). The most predictable next message is not the right one.
- **Prompt injection.** A suggester that reads tool results can be steered by injected content into proposing, in the user's own voice, "approve", "send it", "delete them" (indirect injection, Greshake et al. 2023, https://arxiv.org/abs/2302.12173; applying it to suggestions is our inference).
- **Lost provenance.** Once accepted, a suggestion is indistinguishable from typed text (https://github.com/anthropics/claude-code/issues/60087).
- **Influence and over-reliance.** Suggestions shift what people write and think (Jakesch et al., CHI 2023, https://arxiv.org/abs/2302.00560); predictive text makes writing shorter and more predictable (Arnold et al., IUI 2020).
- **Engagement bait.** Follow-ups that "juice engagement" rather than help (https://techcrunch.com/2025/05/02/ai-chatbots-are-juicing-engagement-instead-of-being-useful-instagram-co-founder-warns).
- **Hallucinated capabilities.** A suggestion that asks for something the agent has no tool for. Not measured anywhere; ground the suggester in the agent's real tools.
- **Accessibility.** Taking over `Tab` in a field can trap keyboard users (WCAG 2.1.1 / 2.1.2, https://www.w3.org/WAI/WCAG21/Understanding/no-keyboard-trap.html); inline ghost text is easy to miss with a screen reader (https://github.com/w3c/aria/issues/494).

## 2. What the codebase already has

- **Routing Model**: `PlatformSettings.routing_model` / `routing_provider_id`, read by `routing_model(db)` (`backend/app/services/flow_llm.py`) and used by the handover classifier (`backend/app/services/routing_evaluator.py`, `traced_generation`, `disable_thinking=True`, usage merged into the turn). It may be unset; `DEFAULT_ROUTING_MODEL` can lock it.
- **The exact precedent: the AI title.** `_generate_title_after_done` (`backend/app/api/v1/chat.py`) runs as a task AFTER the `done` frame, on its own sessions, and reaches every stream of the owner through a live-only frame (`conversation_title`, `LIVE_ONLY_FRAME_TYPES` in `backend/app/services/chat_turn_stream.py`). A suggestion would follow the same path with a new live-only frame, without delaying the end of the turn.
- **Welcome suggestions only.** `backend/app/services/chat_suggestions.py` serves the agent's `web_suggested_prompts` for the empty conversation (`GET /chat/suggestions`); nothing exists after a turn.
- **The composer is not in this repository.** It is `@filigran/chatbot` (`XTM-Foundation/filigran-design-system`, `packages/filigran-chatbot`), shared with the panel embedded in OpenCTI / OpenAEV. `Tab` is already taken by the `@` mention menu (`useMentionMenu.ts`) while it is open. The ghost text, its keys and its accessibility are a chatbot-package change, released and then pinned in `frontend/package.json`.
- **Invariants that constrain the design**:
  - 16, no system prompt inflation: the recap cannot be a new instruction in the agent's prompt; it must be a post-processing call.
  - 18, no workarounds: no keyword list ("delete", "approve") to block risky suggestions in a generic layer; the guard must be structural (turn state, approvals, tool metadata).
  - 17, a license is a quota: decide whether the call counts.

## 3. Proposed definition

1. **Trigger.** After a web chat turn ends with `done` (not `cancelled`, `error`, an interrupted turn, or an `approval_required` pause), a background task asks the Routing Model for ONE suggestion. It runs after `done`, like the title, with a time budget (around 5 s); a new user turn cancels it, and a late answer is dropped.
2. **Input of the suggester: a compact, deterministic digest, not the raw transcript.** The last user message, the final answer (truncated), the names and outcome (ok / failed) of the tools called during the turn, the agent's name, description and tool list, and the conversation's language. Raw tool output never reaches it, so injected content cannot write the suggestion. This also keeps the cost low: the Routing Model cannot reuse the main model's prompt cache.
3. **No extra step in the agentic loop.** The "what was done / what next" reasoning lives in the suggester's prompt, which asks for structured output `{ "suggestion": string | null }`. If the recap is also to be SHOWN to the user, the same call returns `{ "recap": ..., "suggestion": ... }`: one call, no change to the agent's prompt, nothing written back into the history.
4. **Output rules.** In the user's voice and language, short (a few words to one sentence), an action the agent can actually do with its tools, `null` when the next step is not obvious. Server-side validation of shape and length; nothing appears when the answer fails it.
5. **Structural guards (invariant 18).** No suggestion when the turn ends on a pending approval, a question asked to the user (a form, a choice), or an error. A suggestion is never an answer to a HITL prompt. Accepting only fills the input: `Enter` stays a deliberate send.
6. **Delivery.** A new live-only frame `reply_suggestion { conversation_id, turn_id, text }`, sent to the owner's streams only (not to shared viewers), kept in memory with the conversation's last turn so a reload or a second tab can show it, and dropped once any new message exists. It is never part of the history sent to the model.
7. **Composer (`@filigran/chatbot`).**
   - Grey ghost text in the EMPTY composer, with a visible "Tab" hint.
   - `Tab` and `→` take the suggestion ONLY while it is shown, the `@` menu is closed and no IME composition is running (`isComposing`); otherwise `Tab` moves focus as usual.
   - `Esc` or typing dismisses it.
   - On touch screens, a tappable chip that fills the input.
   - For screen readers, `aria-describedby` reads "Suggestion: …, press Tab to accept".
8. **Controls.**
   - An admin switch in Settings > AI Models next to the Routing Model, off when no routing model is set.
   - A user preference to turn it off.
   - Adaptive throttling: after N suggestions in a row go unused, suggest less often.
9. **Observability.**
   - The call is an LLM trace (`traced_generation`, name `reply_suggestion`), and its usage is merged like the routing classifier's.
   - Metrics: shown, accepted (Tab), sent unedited, edited before sending, dismissed, and the share of silences.
   - The message the user sends carries `from_suggestion: true` in its metadata, for metrics only.

## 4. Decisions

Taken (2026-11-19):

- **Form: the Claude Code pattern.** One grey ghost-text suggestion in the composer: `Tab` / `→` fills the input, `Enter` sends, typing or `Esc` dismisses, and the suggester may stay silent. No chips under the answer.
- **No visible recap in v1.** The "what was done / what could come next" reasoning stays inside the suggester's call and is never shown. The answer already says what was done, a recap would duplicate it, and nothing shows it improves the suggestion. A visible recap is a v2 candidate for long agentic turns: the same call would return `{ "recap", "suggestion" }`, possibly only past a number of tool calls in the turn.

Taken when v1 was built (2026-11-19):

- **No routing model: off.** Suggestions never fall back to the platform default model.
- **Surfaces: the web chat and the embedded chat.** Both backend paths send the frame (`api/v1/chat.py`, `api/v1/platform.py`); every surface rendering `@filigran/chatbot` shows it once its pin moves to a release that reads the frame. Channels (Teams `suggestedActions`, Slack) are out of scope.
- **On by default** once a routing model is set, with the administrator's switch (`PlatformSettings.reply_suggestions_enabled`, Settings > AI Models) and the user's preference (`User.reply_suggestions_enabled`, Profile).
- **Quota: platform overhead, like the AI title.** The call is traced (`reply_suggestion`) but charges no agentic action.
- **No per-agent opt-out in v1.**

## 5. What v1 does differently from the proposal

- The digest has the agent's name and description, not its tool list: a large tool list would cost more than the suggestion is worth.
- The answer is `{ "suggestion": string }`, an empty string being the silence (`null` is not portable across providers' native structured output).
- The suggestion is not kept for a reload: it lives in the composer until it is used or dismissed, and a reload or another message drops it.
- A send opts in with `supports_reply_suggestions` (the chat panel sets it, like `supports_artifacts`): the same streams serve scripts and one-shot generation calls ("Generate with AI" in OpenCTI / OpenAEV), which read to the end of the stream and must neither wait on a suggestion nor pay for one.
- No `from_suggestion` flag on the sent message yet: acceptance metrics need the composer to report it.

## 6. Sources not verified

- ChatGPT's and Perplexity's internal generation method.
- The 40% Perplexity figure: one quote, and the metric is unclear.
- A Gemini carousel was tested in 2025; availability is unconfirmed.
- Whether a provider's prompt cache is shared across models.

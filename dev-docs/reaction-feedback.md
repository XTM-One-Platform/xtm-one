# Reactions as feedback

An emoji reaction a person puts on an agent's answer in Teams or Slack becomes a positive or negative rating of that answer, and joins the feedback the web chat thumbs already feed (the agent's Feedback tab and summary, Persona Improvement evidence, `get_agent_feedback`).

Status: **Agent Accounts on Teams implemented** (stack level 1). Channel Bot Teams (level 2) and Channel Bot Slack (level 3) follow the design below.

## Decisions

| Topic | Decision |
|---|---|
| Sources | Agent Accounts on Microsoft Teams (Graph); then Channel Bot Teams (Bot Framework), Channel Bot Slack (Events API) |
| Who may rate | Everyone who can see the answer and react: members, guests, federated users, Slack Connect. No XTM One account needed |
| Emoji -> rating | Configurable per Agent Account (and per Channel Bot); default thumbs up positive, thumbs down negative; any other emoji ignored |
| Rater identity | Only a keyed HMAC of the platform user id (`rater_ref`) - one rating per rater and retraction, no personal data in clear |
| Window | A reaction counts for **14 days** after the answer was posted (approved, or sent at once) |
| Default | Enabled, on existing and new accounts alike; switchable off |
| Teams detection | Graph `updated` notifications, filtered on the answer before any Graph call; periodic checks where no subscription hears changes |
| Raters per answer | At most 500 (`MAX_RATERS_PER_TARGET`) who give a rating, the smallest `rater_ref` first (stable whatever order the platform lists reactions in), no weighting between internal and external raters |
| Custom emojis | Not in v1 (Teams `custom`, Slack workspace emojis are ignored) |

## Behaviour

1. **One rating per rater per answer.** A mapped reaction creates the rating; changing it replaces it; removing it retracts it. A rater holding a positive and a negative emoji at once gives no rating.
2. **Full state, never deltas.** The ingestion always receives every current reaction of the answer (read back from the platform) and brings the stored ratings in line: a repeated or late notification rewrites the same state.
3. **Ignored:** reactions on messages that are not an agent answer; after the window; the agent's own identity (an `ai_user` account, later the bot itself); unmapped emojis; while the setting (or the account) is off.
4. **Switching off keeps, switching on reconciles.** Switching the setting or the account off stops reading reactions; the ratings already stored are kept (they still feed the Feedback tab and Persona Improvement). Switching either back on, or changing which emojis count, reads every answer still in its window again at once (`reactions.resume_checks`): a reaction put while it was off counts, and a rating given by an emoji the setting no longer maps is retracted. An answer past its window keeps the ratings it had.
5. **`on_behalf` mode:** the owner's reaction on their delegate's answer counts like anyone's.
6. **Skin tones and variants:** each provider turns its reaction into Unicode; `normalize_emoji` drops the Fitzpatrick modifiers and the presentation selectors, so a thumbs up of any skin tone is the thumbs up. What is left must be exactly one emoji: a ZWJ sequence (`👨‍💻`), a flag (two regional indicators) or a subdivision flag is one, several in a row (`👍👎`, `🔁🔁🔁`) are refused - by the setting and by the Teams reaction tools before any Graph call. Teams legacy names: `like`, `heart`, `laugh`, `surprised`, `sad`, `angry`.
7. **No LLM involved:** ingesting a reaction runs no agentic work, so neither the agentic gate nor the quota applies.

## Configuration

`{"enabled": true, "positive": [...], "negative": [...]}` - `AgentAccount.reaction_feedback` (NULL = the defaults). `ReactionFeedbackConfig` validates it in the request body (422): disjoint signs, at most 10 emojis each, every entry a single emoji (text, digits, markup refused), normalized and deduplicated. A stored value that no longer validates reads as **off**, never guessed at.

UI: `frontend/src/components/feedback/ReactionFeedbackSettings.tsx` (a `Switch` and an emoji palette cycling positive / negative / ignored), in the *Replies* section of the Agent Account editor; the logic mirrored in `frontend/src/lib/reaction-feedback.ts`.

## Data model

Ratings stay in the existing tables, so every current consumer reads them unchanged:

| Source | Target | Table |
|---|---|---|
| Agent Account | the `AgentAccountReply` (annotation target type `agent_account_reply`) | `annotations` |
| Channel Bot (level 2+) | the assistant `Message` | `message_feedback` |

`annotations` (level 1):

- `user_id` nullable; `rater_ref` (`String(64)`, HMAC-SHA256 of `"<provider>:<platform user id>"` under a key derived from `SECRET_KEY`, domain `xtm-one reaction rater v1`); `ck_annotations_one_rater` = exactly one of the two;
- partial unique index `uq_annotations_target_rater` on `(target_type, target_id, rater_ref) WHERE rater_ref IS NOT NULL` (the upsert's conflict target);
- `source` (`web` default, `teams_agent_account`), `emoji` (the normalized emoji that gave the rating).

A web rating and a reaction from the same person are two ratings: a reaction is never resolved to an XTM One user. `SECRET_KEY` rotation makes a rater who reacts again a new one; bounded by the window.

`agent_account_replies`: `agent_id` (the agent that wrote the answer, no foreign key - it would lock `agents`) and `ix_agent_account_replies_sent_message (account_id, sent_message_id)`. An agent account posts one message per answer, so the reply row itself is the lookup.

`agent_account_subscriptions.change_type`: what a Graph subscription hears (`created`, `created,updated`); Graph cannot change it in place, so a different value is a new subscription.

Channel bots (level 2) need a mapping from each platform post to the answer - a long answer may be split into several posts - planned as a `reaction_targets` table (`source`, `provider_scope`, `conversation_ref`, `platform_message_id`, `message_id`, `agent_id`, `expires_at`), and the same `rater_ref` / `source` / `emoji` columns on `message_feedback`.

## Ingestion

`reaction_feedback.ratings_by_rater()` turns the current reactions into `{rater_ref: (rating, emoji)}` (own identity excluded, capped), and `apply_reactions()` writes them under a per-target advisory lock: upsert the changed, delete the gone.

### Agent Account - Teams (Graph)

- With the setting on, every subscription WITHOUT a mention filter asks `created,updated`. A channel heard for mentions only keeps its filtered `created` subscription: Graph grants one subscription per application and channel, and the mention filter also filters the changes, which the agent's own answers never match.
- `updated` entries are parsed from the notification alone (`updated_message`) and looked up among the account's sent answers before any Graph call: an edit of anyone else's message costs one indexed query. The notification endpoint does that lookup itself, one query per delivery, so a change to another message spawns no task. The notifications of one answer are gathered into one read: the first claims it (`state.claim_reaction_refresh`), waits 3 seconds (`reactions.COALESCE_WAIT_SECONDS`) and releases the claim right before the read, so nothing arriving during it is lost; the claim's 30-second TTL (`state.REACTION_COALESCE_SECONDS`) only bounds a holder that died.
- Periodic checks (`state.REACTION_CHECKS_KEY`, a Redis sorted set): every posted answer is scheduled five minutes on; a check reads it and reschedules it - every 5 minutes the first hour, 30 the first day, 6 hours after - until the window closes. An answer a subscription hears (`reactions_pushed`) is still read every 6 hours at most (`PUSHED_CHECK_INTERVAL`): push may stop hearing it before the window closes, a subscription removed or recreated for mentions only. This covers mention-only channels and accounts that poll. Losing Redis loses the schedule (the answers stop being checked); push is unaffected.
- Switching the setting recreates the subscriptions concerned (delete, then create: a message arriving in between is missed). When Graph refuses the `created,updated` one (a refusal or an error answer), the subscription is created with `created` alone, so the conversation stays heard; its answers are then read by the periodic checks, the account's sync report shows a `reactions_refused` problem, and the syncs of the next day keep `created` rather than recreating it (`state.note_reactions_refused`).

### Channel Bot - Teams (level 2)

- `messageReaction` activities (`reactionsAdded` / `reactionsRemoved`, `replyToId` = the bot's activity id) stop being ignored. Bot Framework sends deltas: the handler rebuilds the rater's current set from what is stored plus the delta.
- Prerequisite: `send_message` returns the `ResourceResponse.id` (today `""` on a 202) and every post writes its `reaction_targets` row.
- To check first: whether Bot Framework delivers reactions from the extended emoji set or only the six legacy types (if only those, a thumbs down cannot be collected through a bot, and the UI says so).

### Channel Bot - Slack (level 3)

- `reaction_added` / `reaction_removed` events and the `reactions:read` scope; existing apps must be reinstalled (the settings say so until then). Deltas, like Bot Framework. `chat.postMessage` returns each post's `ts` for `reaction_targets`.

## Access

- Ingestion trusts nothing in the notification but the subscription's `clientState` (existing check); the rated answer is always the stored one, never an id taken from the notification.
- `agent_account_reply` resolves for the account's owner and administrators (`annotations.resolve_target`), a sent reply only. The agent's managers read its ratings through the existing feedback routes. No response carries `rater_ref`; a platform rating shows its source and emoji, never a name.

## Downstream

- Agent feedback list and summary, Persona Improvement `gather_evidence`, `get_agent_feedback`: include the reactions (new target type, `source` / `emoji` on list items). `gather_evidence` reports them apart from the users' ratings (`reaction_ratings` beside `ratings`), so the rewrite does not weigh a crowd's emojis as its users' verdicts.
- Langfuse and the local trace store: **not fed** for agent account answers. The agent turn of an agent account runs without a conversation, so no trace id reaches the reply - the same as background tasks and delegations today. A follow-up can capture the trace id on the reply.
- Agent Accounts page: positive / negative counts on each reply of `Recent decisions`.

## Behaviour changes

- Existing Agent Accounts start collecting reactions as feedback (default enabled), and their Graph subscriptions without a mention filter are recreated with `created,updated`: more notifications reach the endpoint.
- Feedback counts, averages and Persona Improvement evidence include ratings from people who are not XTM One users.
- `annotations.user_id` becomes nullable; the downgrade deletes the platform ratings.

## Still open

1. Approval mode: the reaction rates `sent_text`, which the owner may have rewritten. Flag such ratings and keep them out of Persona Improvement evidence?
2. Other channel bots with reaction events (Telegram `message_reaction`, WhatsApp `reaction`, Discord gateway): a follow-up issue.
3. Capturing the trace id of an agent account's turn, so its ratings reach Langfuse.

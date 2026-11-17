# XTM One on the shared chat panel

Every XTM One surface that shows a conversation (`/chat`, the deck and video
studio rails, an agent's conversation view, voice calls, the Office add-in)
renders `@filigran/chatbot`, the panel OpenCTI, OpenAEV and OpenCRQ embed
(FiligranHQ/filigran-ui, `packages/filigran-chatbot`). This page says where
each piece of chat code lives, what XTM One holds that the package also has
and why, and which contracts the two repositories change together.

## The rule

The package owns the chat itself: the transcript, the composer, the turn
protocol (frames, Stop, relayed and folded turns, how a turn ended), the
markdown of an answer, the marker grammar, voice calls, read-aloud,
dictation in the composer, the conversation list. XTM One owns what is its
own and hands it to the panel through its props: the routes and the token,
the cards of its markers, the extras under a reply, the composer toolbar, the
banners, the waiting game (`frontend/src/components/chat-panel/`, the one host
module; `apps/microsoft-office/src/components/OfficeChatPanel.tsx` for the
add-in).

A host never re-derives what the panel knows. The panel tells it:

| What | Package API | Read by |
|---|---|---|
| A turn runs, and how it ended | `onTurnRunningChange(running, { ended })` | `useTurnFrames`, Office `lib/turns.ts` |
| A frame ends a turn provisionally or with its stored reply; a turn is replayed | `onStreamEvent(frame, { relayed, end, replayed })` | `useTurnFrames`: a stored end after the turn left the screen refetches the conversation; a replay's charges replace the ones counted |
| The session ended | `onSessionEnded(reason)` | `XtmChatPanel` (`endSession`, `"lifetime"` when no reason is given), Office (signs the pane out) |
| A message is the answer still being written | `renderMessageExtras` / `messageFooterActions` context `{ streaming }` | Office: no Replace / Insert under it |
| Which agent wrote an answer, or failed to | `ChatMessage.answeredBy`, `ChatMessage.mentionFailed` | `MessageExtras` |
| The rating already given to a restored answer | `ChatMessage.feedback` | `MessageFooterActions`, for a reply the page holds no copy of |
| Send a text while the bubble shows another | `onBeforeSend` → `{ text, displayText }`; `handle.send(text, { displayText })` | Office |
| A frame the panel does not render | `onStreamEvent` | `useTurnFrames` |
| Answer markers outside the panel | `@filigran/chatbot/markdown`: `splitAnswerMarkers`, `stripAnswerMarkers`, `markerKinds` | `pages/chat/answerMarkers.ts` |
| Markdown normalisation | `@filigran/chatbot/markdown`: `prepareMarkdown`, `markdownUrlTransform` | `ChatMarkdown.tsx` |
| The conversation list grouped by workspace | `@filigran/chatbot/workspaces`: `parseWorkspaces`, `groupConversations`, `groupIsOpen`, `fileableWorkspaces` | the standalone rail (`useSidebarWorkspaces.ts`, `ConversationSidebar.tsx`) |
| A fenced block XTM One draws (charts) | `codeBlockRenderers` | `XtmChatPanel` |

`ChatMessage.raw` (the backend's restored entry or `done` frame) is not read:
see the known gaps.

Only lazily loaded, never-prefetched pages import the package root (its
`ChatPanel` and styles); every other page may use the `/markdown` and
`/workspaces` entries, which hold no React.

## What XTM One keeps that the package also has

Each is kept for the reason given; change it with its package counterpart.

| XTM One | Package | Why it stays |
|---|---|---|
| `frontend/src/components/chat/ChatMarkdown.tsx` (renderer, code block, lightbox, authenticated images) | `MarkdownMessage.tsx`, `ChatImage.tsx` | Used on eager pages (dashboard, run details, prompts, CTEM, settings) that may not load the package root; draws XTM One's own chart blocks and Desktop local paths with the app's tokens, and fetches a chat file's picture with the session (its `/api/v1/chat/{files,images}` path, whatever host the model wrote before it). The text goes through the package's `prepareMarkdown`. |
| `frontend/src/pages/chat/ConversationSidebar.tsx`, `WorkspaceGroups.tsx`, `useSidebarWorkspaces.ts` | `ConversationSidebar.tsx`, `useWorkspaces.ts` | The standalone home's rail (`StandaloneConversationRail`), on a prefetched page: only the UI is XTM One's. Which groups exist, their order and which are open are the package's `/workspaces` functions. The rail lets a group held open (by a search or the conversation on screen) be folded, which the panel's list does not (`groupCanToggle`). |
| `frontend/src/components/chat/FileConversationDialog.tsx` | the sidebar row's "move" | The header menu's entry point on `/chat`, beside the row menu. |
| `frontend/src/pages/chat/MessageFeedbackButtons.tsx` | `useMessageFeedback.ts` (off: no `feedback` endpoint) | Feedback criteria the package does not have. |
| `frontend/src/hooks/useSpeechToText.ts` | `dictationController.ts` | Dictation on run cards and run details, outside the panel. |
| `frontend/src/components/presentation/PresentationCard`, `report/ReportCard`, `video-studio/VideoCard` | `ArtifactCard.tsx` | They replace the package's cards through `markerRenderers`: live deck, slide stage, studio binding. |
| `frontend/src/components/ui/action-proposal.tsx` | `ChatApprovalPrompt.tsx` | Approvals on run pages, and an expired approval under a reply; live approvals on `/chat` are the package's. |
| `frontend/src/pages/chat/attachments.ts` `fileTypeLabel` | `fileExtensionLabel` (internal) | Follows the backend's attachment type label. |
| `frontend/src/pages/chat/fileVisuals.ts` `formatFileSize` | `formatFileSize` (internal) | Also the Files page's and the conversation files panel's. Kilobytes show one decimal here and none in the package, so a marker's file card and the panel's attachment chip can differ by that decimal. |
| `frontend/src/hooks/api/useDatasetApi.ts` `iterateSSEDataLines` | `utils/sse.ts` | A dataset import stream, with a buffer cap; not chat. |
| `frontend/src/lib/utils.ts` `timeAgo` | `utils/index.ts` `timeAgo` | App-wide helper. |
| `apps/microsoft-office/src/lib/panelConfig.ts` | — | The add-in cannot import the frontend's `components/chat-panel/endpoints.ts`; `frontend/tests_unit/office-addin-panel-config.test.ts` holds the two route maps equal. |
| `apps/microsoft-office/src/lib/sends.ts` | — | The add-in cannot import the frontend's `components/chat-panel/host-send.ts`: the same rule, the composer keeping the text a taken-over send stands for until it lands. |
| `apps/microsoft-office/src/lib/turns.ts` | `onTurnRunningChange` | Sequences the pane's own sends (a document review: one section per turn) on the panel's word that a turn ended, and how. |
| `apps/microsoft-office/src/lib/prompts.ts` `shownRequestOf` | `displayText` | The backend stores the prompt the pane built, not the words its bubble showed: a reopened conversation reads them back from the prompt. |
| `apps/microsoft-office/src/components/OfficeChatPanel.tsx` `sessionEndedMessage` | — | The add-in's sign-in screen says why the session ended, in the web app's words. |

`frontend/src/pages/chat/secretMarkers.ts`, the arcade waiting game and the
file revisions have no package counterpart.

## Contracts the two repositories change together

| Contract | XTM One | Package (and its playground) |
|---|---|---|
| Stream frames: every type the web chat streams, sampled in `frontend/src/pages/chat/stream-frames.contract.json` and checked against the backend's names by `backend/tests/test_chat_panel_contracts.py`. An `error` or `cancelled` marked `provisional: true` is provisional (`is_provisional_end`): the turn's final end follows, its stored end (a `done` or `cancelled` carrying `message_id`) or an `error` when the reply could not be stored. Any other end is final. Every frame but `user_message`, `conversation_title` and `session_expired` names its turn (`turn_id`), the send's acknowledgement included. | `backend/app/services/chat_turn_stream.py`, `backend/app/api/v1/chat.py`, `chat_web_stream.py` | `src/hooks/protocols/parseRestEvent.ts`, `src/hooks/useChat.ts`, `src/hooks/voiceTurn.ts`; playground `mock-api.ts` |
| Restored message shape (feedback, `answered_by`, `mention_failed`, context figures, artifacts), restore window and `has_more` | `platform.py` `restored_message_entries`, `read_message_page`, `ChatPanelSessionResponse`; `chat_web_stream.py` `POST /chat/sessions`, `/chat/sessions/{id}/messages` | `parseRestoredMessages`; playground `restoredEntry` |
| Routes of the web chat and their answers | `backend/app/api/v1/chat_web_stream.py`; `docs/docs/usage/chat-api.md` | README "API Contract" and "Turns that outlive their stream"; copies in `frontend/src/components/chat-panel/endpoints.ts` (each path checked against the served routes by `test_chat_panel_contracts.py`), `apps/microsoft-office/src/lib/panelConfig.ts`, playground `App.tsx` |
| Refusal body: `{"detail": "<sentence>"}`, with a machine `code` beside it where clients branch on one (`CodedHTTPException`); an ended session's 401 is `{"detail": {"code": "session_expired", "reason", "message"}}` | `api/deps.py`, `services/chat_web_transport.py` | `utils/refusal.ts`, `utils/sessionEnd.ts` |
| Client id (`X-Chat-Client-Id`, `?client_id=`): at most 128 printable characters; any other value is replaced by a random one for that request | `chat_web_stream.py` `_client_id` | `useChat.ts` |
| Stop: `POST /chat/messages/cancel` answers 202 with `turn` `local`, `remote` or `none`, and 503 when the turn runs on another replica and Redis cannot carry the Stop. A Stop finding no turn is kept for the caller's next send only when its body says `awaiting_send` (that send has not started yet). The route records the Stop and stops the background work itself (`chat.stop_conversation_work`), so a Stop after the turn's last pass, while its answer is stored, keeps that answer and still stops the work. | `chat_web_stream.py`, `services/chat_turn_guard.py` | `useChat.ts` |
| Wind-down: after a turn's end, sends are held until its stored end or the stream's close, at most 5 s (the panel's rule) | — | `useChat.ts` `WIND_DOWN_MS` |
| Event stream lifetime: the backend ends `GET .../events` when it chooses (`EVENTS_MAX_LIFETIME_SECONDS`); the panel reopens it | `chat_web_stream.py` | `useChat.ts` |
| Wait statuses: `wait_start` (what the pause watches, the seconds granted) and `wait_status` (one of them changed state), sampled in the frames contract | `app/tools/agent_loop.py` | `ChatThinking.tsx` (countdown, "Continue now"); XTM One's `WaitItemList` |
| Approval presence: while a recovered approval prompt is on screen the panel reads the conversation every 10 min, inside the backend's `ABANDONED_APPROVAL_SECONDS` (1800 s), past which a prompt nobody reads is abandoned | `services/chat_turn_guard.py` | `useChat.ts` `APPROVAL_PRESENCE_INTERVAL_MS` |
| Feedback comment: at most 2000 characters | `chat.py` feedback body | `utils/feedback.ts` `FEEDBACK_COMMENT_MAX_LENGTH` |
| Waiting game link: `{agentDashboardUrl}/chat?arcade=play`, or the host's `waitingGameUrl`. XTM One's surfaces draw the game in place (`renderWaitingPanel`), which takes the link's place, so the link serves the embedding platforms. | `frontend/src/components/arcade/arcade-link.ts` (`?arcade=play` on any route) | `ChatWaitingPanel.tsx` |
| Answer markers `[[KIND:args]]`: every kind (file, deck, report, video, investigation, work item, chart) sampled in `frontend/src/pages/chat/answer-markers.contract.json` | `app/channels/base.py` and `services/chat_charts.py` regexes, tested on the samples (`test_presentations.py`, `test_reports.py`, `test_investigation_events.py`, `test_chat_panel_contracts.py`) | `utils/index.ts` marker grammar (via `/markdown`), tested on the samples by `answerMarkers.test.ts` |
| Upload limit: 40 MB per file on the backend. The panel's `maxTotalSize` bounds the files of one message together, and the web app and the add-in pass the per-file limit: the composer refuses files adding up to more than 40 MB even when each would pass. | `services/file_storage.py` `MAX_UPLOAD_SIZE_BYTES`, `frontend/src/lib/constants.ts` (held equal by `test_chat_panel_contracts.py`), `panelConfig.ts` (by `office-addin-panel-config.test.ts`) | `maxTotalSize` prop |

The playground's mock (`projects/filigran-chat-playground/mock-api.ts`)
reproduces XTM One's routes for the package's own tests and demos: relayed
turns (the `resumed` replay, `user_message`, `conversation_title`,
`folded_into_active_turn`, `turn_started`), provisional ends followed by
their stored end (a failed loop, a Stop), `wait_start`, a cap of 100 on a
page of older messages, and the rating route of the embedded mode
(`/chat/conversations/{id}/messages/{id}/feedback`), which the web chat does
not use. Its event stream ends every 2 minutes, so the reopen is exercised.
It does not produce `approval_required`, `session_expired`, `agentic_action`
or `answered_by` / `mention_failed`.

## Known gaps

- The backend's restored and paged entries carry none of the fields XTM One
  shows beside a reply (`runner_activity`, `expired_approvals`,
  `unavailable_integrations`, `llm_trace_id`, a user message's `channel`, a
  reply's `content_format`), so `ChatMessage.raw` cannot stand in for the
  page's own copy of the conversation: those come from `useConversation`, and
  a message on an older page the panel loaded shows none of them (its rating
  already given comes from the panel's `ChatMessage.feedback`).
- `onSessionEnded` fires on a `session_expired` frame and on a 401 whose
  `detail.code` is `session_expired`, which every ended session answers
  (`session_ended_error`). A 401 with a string `detail` (a password changed
  since the sign-in, an invalid token) does not reach it: `/chat` signs out
  on the app's next request, the add-in on its own next request.

# WhatsApp: two backends

This page is about the **WhatsApp integration** that gives agents' tools a number to work with (see [WhatsApp](../../usage/resources/integrations.md#whatsapp) in Integrations), not the [WhatsApp channel bot](whatsapp.md) that lets people talk to an agent. Messages received on a number that a connected WhatsApp integration operates are stored for that integration's tools and agent accounts, and the channel bot does not answer them.

The **WhatsApp** integration (Integrations > WhatsApp) runs on one of two backends, chosen per integration with the **backend** field. The tools keep the same names on both; each backend adds what only it can do.

| | Meta Cloud API (`meta_cloud_api`) | Linked-device gateway (`linked_device_gateway`) |
|---|---|---|
| What it is | The official WhatsApp Business Platform from Meta | A WhatsApp number paired as a linked device to a hosted gateway (Whapi.Cloud first) |
| Number | A business number registered in WhatsApp Manager | Any WhatsApp number, paired by scanning a QR code |
| Credentials | API Key = system user access token; `phone_number_id` (required), `waba_id`, `app_secret`, `verify_token` | API Key = the gateway channel token; `gateway_vendor` (default `whapi`), `base_url` (default `https://gate.whapi.cloud`), `webhook_secret` (inbound) |
| Inbound webhook | `/api/channels/whatsapp/webhook`, shared, routed by `phone_number_id` | `/api/channels/whatsapp/gateway/<integration id>?token=<webhook_secret>`, one per integration |
| Send text, media, reactions | Yes (free-form text needs the 24-hour window, templates outside it) | Yes |
| Templates and interactive buttons | Yes | No |
| Polls | No: the Cloud API cannot create a poll, and a received poll arrives as "unsupported" | Yes: `send_whatsapp_poll` (2 to 12 options, single or multiple choice) and `get_whatsapp_poll_results` (votes per option and who voted, from the votes the webhook stored) |
| Received messages | `list_whatsapp_conversations`, `read_whatsapp_messages` | The same tools: messages, polls and group events (member added, removed, promoted) |
| Sent messages | Not readable: the Cloud API does not list what the number sent | `read_whatsapp_messages` also lists the messages the number sent in the conversation, flagged outgoing, with their message id (read live from the gateway) |
| Edit or delete a sent message | No | Yes: `edit_whatsapp_message` replaces the text of a text message the number sent, within WhatsApp's 15-minute edit window (the recipient sees it marked Edited; past the window, on a message the number did not send or on a media message it is refused with the reason, nothing changed); `delete_whatsapp_message` deletes a message for everyone. `send_whatsapp_message` and `send_whatsapp_media` answer the message id to pass |
| Groups | Only groups the business creates; members join by invite link | Any group the paired number is in, including existing ones (`list_whatsapp_groups`, `get_whatsapp_group`, send to `...@g.us`) |
| Pairing | None | `get_whatsapp_pairing_status` (connected, qr, banned, sync_error, starting) and `get_whatsapp_pairing_qr` (the QR code as an image) |

Sending tools, edit, delete and poll creation are write tools and follow the approval rules of every write; reads (groups, poll results, pairing status) are read-only. Message bodies are never logged above debug level.

## Terms of service: read this before choosing the gateway

A linked-device gateway is **not** the official WhatsApp Business Platform. It drives a normal WhatsApp account the way WhatsApp Web does. WhatsApp's terms do not allow automated or bulk use of a consumer account, and WhatsApp can **ban the paired number**, temporarily or for good, without notice.

- Use a **dedicated number** for the gateway, never a person's own number.
- Keep the volume human: no bulk messages, no messages to people who did not expect them.
- The platform paces every send on the gateway: at most 20 messages per minute per integration and 6 per minute per chat (texts, media and polls together), a random pause of 0.4 to 1.6 seconds before each send, and the typing indicator before a text for about one second per 12 characters (at most 8 seconds). A send over a limit is refused with the seconds to wait; nothing is sent.
- If the pairing status turns `banned`, pair another dedicated number; the ban is WhatsApp's decision and the platform cannot lift it.

Choose the Meta Cloud API when you do not need polls or existing groups.

## Set up the linked-device gateway (Whapi.Cloud)

1. Create a Whapi.Cloud account and a channel; copy its **channel token**.
2. In XTM One, Integrations > WhatsApp, set **backend** to `linked_device_gateway`, paste the channel token into **API Key**, leave `base_url` empty for `https://gate.whapi.cloud`.
3. **Test Connection** reads the channel's health.
4. Pair the dedicated number: open the integration, tab **Configuration**, card **WhatsApp pairing**. While the number is not paired the card shows the gateway's login QR and follows it as it rotates; on the phone open WhatsApp > Settings > Linked devices > Link a device and scan it. The badge turns **Paired** once linked. The card reads the same state as the agent tools `get_whatsapp_pairing_status` and `get_whatsapp_pairing_qr` (connected, qr, starting, sync_error, banned, unknown), which an agent bound to the integration can still run. The QR is shown only to a user who can manage the integration.
5. Add the number to the groups it must work in, from the phone.
6. Inbound: set **webhook_secret** on the integration to a long random value, then in the Whapi.Cloud channel settings > Webhooks add the URL the **WhatsApp pairing** card shows under **Gateway webhook URL** (copy button; visible only to a user who can manage the integration, since it carries the secret), that is `https://<your XTM One>/api/channels/whatsapp/gateway/<integration id>?token=<webhook_secret>` with the events **messages** (POST and PATCH) and **groups**. A delivery without the secret, or with a wrong one, is refused (401); an integration without a secret refuses every delivery. The URL answers 404 only when the integration id is unknown, is not a `whatsapp` integration or is not on `backend = linked_device_gateway`; the server log then names which ("WhatsApp gateway webhook: no gateway integration <id>: <reason>"). Opening the URL in a browser (GET) answers 405 "POST only": that is expected.

## What the gateway webhook stores

- Received messages (text, link previews, media captions) in the same store as the Meta backend, kept `inbound_retention_days` (30 by default). What the number sends to others is not stored; what it writes in its own "message yourself" chat is (the self-chat).
- The self-chat as an instruction channel (off by default): set the extra field `self_chat_agent_id` to an agent that has an enabled WhatsApp agent account on this integration. Each message the owner writes in the self-chat then starts one turn of that agent, as the integration's owner (the XTM One user who owns the integration, never read from the message), and the reply is posted in the same self-chat. No other chat ever reaches that route: contacts and groups stay data for `list_whatsapp_conversations` / `read_whatsapp_messages`, answered only by the account's own rules. The agent's replies echoed back in the self-chat are recognised by message id and never start a turn. An integration without an active owner does not route its self-chat.
- Voice notes and audio messages (any chat) are downloaded from the gateway and transcribed with the speech-to-text provider of the integration's owner (language detected), then stored as `[voice note, 0:42, transcribed, heard in french] ...`, the language the model heard named so a switch is never silent. The transcript is judged before it is trusted (both backends): a note whose audio was cut short, that holds far too few words for its length (under 60 a minute past 30 s), or whose speech was mostly decoded unsure (Whisper's own confidence thresholds per segment) is stored as `[voice note, 0:56, heard in french: transcription low confidence: <reason>]`, never as plausible but doubtful text; segments the model writes over silence are dropped. A failed download or transcription is stored as `[voice note, 0:42: transcription failed: <reason>]`, never as an empty message. The audio is held in memory for the transcription only, never kept by the platform; the stored line is followed by `[audio of this voice note: media id <id>, download_whatsapp_media fetches it as a file]`, so an agent can fetch the raw audio from the gateway (as long as the gateway keeps it) and hear it again, for example after a low-confidence transcript. In the self-chat the transcript is the turn's input; elsewhere it is data read by `read_whatsapp_messages`.
- A poll, in words (question and options), and its tally: per option, its id and its voters. A new vote replaces the voter's previous choice; `get_whatsapp_poll_results` answers from the store and asks the gateway only for a poll the webhook never saw.
- Group events, in words: members added, removed, joined, left, promoted or demoted.

The WhatsApp agent accounts on the integration hear every newly stored message, on both backends.

## Voice notes on the Meta Cloud API backend

A voice note or audio message received on the Meta Cloud API number is transcribed the same way before it is stored: downloaded with the integration's access token (sent only to a Meta host), transcribed with the speech-to-text provider of the integration's owner (language detected), and stored as `[voice note, 0:20, transcribed] ...` after its caption, if any. A failed download or transcription, or no speech-to-text provider for the owner, is stored as `[voice note: transcription failed: <reason>]`. `read_whatsapp_messages` and the WhatsApp agent accounts read the transcript (as data); the audio is not kept by the platform and the transcript is not logged. The stored line names the note's media id, which `download_whatsapp_media` fetches from Meta as a file of the conversation (as long as Meta keeps the media) for a re-transcription on demand. Voice notes sent to the `whatsapp_bot` channel bot (a number no integration owns) follow the channel bot's file handling instead (see the WhatsApp channel page).

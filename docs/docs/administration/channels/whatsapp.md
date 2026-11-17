# WhatsApp

Connecting WhatsApp lets people reach an agent from a normal WhatsApp conversation, and lets agents send messages, templates, and reactions back. It runs on the WhatsApp Business Cloud API from Meta.

Read [Channels and bots](index.md) first for the concepts shared with the other platforms.

## Before you start

- You need a Meta developer account and a WhatsApp Business account with a phone number registered for the Cloud API.
- You need administrator rights in XTM One.
- The platform must be reachable from the internet over HTTPS, because Meta posts messages to it.

## Step 1: Create the Meta app

1. Go to [developers.facebook.com](https://developers.facebook.com/apps/) > `My Apps` > `Create App` and choose the business type.
2. Add the `WhatsApp` product to the app.
3. Open `WhatsApp` > `API Setup` and copy the `Phone Number ID`.
4. Generate a permanent access token. The temporary token shown on that page expires after 24 hours and is only useful for a first test.

The app needs the `whatsapp_business_messaging` permission to send and receive messages, and `whatsapp_business_management` to manage numbers and templates.

## Step 2: Connect the bot in XTM One

1. Go to `Settings` > `Channels & Bots` and click `Configure` on the WhatsApp card.
2. Paste the access token into `Bot Token / Access Token`.
3. Paste the phone number ID into `WhatsApp Business phone number ID`.
4. Choose any string as the verify token and paste it into `Webhook verification token (you define this)`. You will enter the same value in Meta.
5. Optionally paste your Meta app secret into `Meta app secret`. It is worth doing: it enables signature verification, so the platform can confirm every incoming request really comes from Meta.
6. Click `Save Channel`, and copy the webhook URL shown on the card.

## Step 3: Configure the webhook in Meta

1. In the Meta app, go to `WhatsApp` > `Configuration` > `Webhook`.
2. Set the callback URL to the webhook URL from the WhatsApp card — it ends with `/api/channels/whatsapp/webhook`.
3. Enter exactly the same verify token you saved in XTM One.
4. Subscribe to the `messages` webhook field.

Meta verifies the URL immediately, so save the credentials in XTM One before this step.

## Step 4: Enable WhatsApp on an agent

Open the agent, go to `Configuration` > `Channels & Bots`, and enable WhatsApp:

- Turn on `Receive` so the agent answers incoming messages.
- Turn on `Publish` if the agent should send messages on its own.
- Decide whether direct conversations are allowed with `Allow private`, and whether people without an account may use the agent with `Allow anonymous`.

Whitelist entries are phone numbers for direct conversations and group identifiers for groups.

## What to expect on WhatsApp

WhatsApp behaves differently from the other channels in three ways.

- **No identity.** WhatsApp does not expose an email address, so senders cannot be matched to an account automatically. Either turn on `Allow anonymous users`, which passes the contact name and number to the agent as context, or expect unknown senders to be asked to register.
- **No mentions.** The Cloud API does not report structured @mentions, so `Only when mentioned` cannot work here. Leave it off for WhatsApp, or the bot stays silent in groups. Direct conversations are unaffected. For the same reason, `/stop` mutes a group for the full seven days — the mute is normally lifted by mentioning the bot again, which WhatsApp cannot signal.
- **The 24-hour window.** Meta only allows free-form messages within 24 hours of the person's last message. Outside that window, the agent must use a pre-approved template, configured in Meta Business Manager.

There is also no typing indicator on WhatsApp, so a long answer arrives with no visible activity beforehand.

Photos, documents, audio, voice notes, and videos sent to the bot reach the agent as files of the conversation, with their caption as the message; a file sent without a caption is still answered. A file larger than the platform's 40 MB upload limit is not passed on, but its caption is. Stickers are ignored.

## What the agent can do in WhatsApp

When `Publish` is on, the agent can send a text message, send an approved template, mark a message as read, and react to a message with an emoji.

## Best practices

- Use a dedicated agent for WhatsApp with a narrow tool set, since senders are usually anonymous.
- Always set the app secret. Without it, the webhook accepts any request that reaches the URL.
- Prepare and approve templates in Meta Business Manager before relying on the agent to start conversations.
- In groups, use the group whitelist rather than mention filtering, which WhatsApp cannot support.

## Troubleshooting

**Meta rejects the callback URL.** The verify token does not match, or the credentials were not saved in XTM One first. The two values must be identical.

**Messages arrive but nothing happens.** Check that the `messages` field is subscribed, that an agent has WhatsApp `Receive` enabled, and that the group or number is in the receive whitelist if one is set.

**The bot ignores group messages.** Either `Only when mentioned` is on somewhere — it can never be satisfied on WhatsApp — or the group is not whitelisted.

**Sending fails outside a conversation.** The 24-hour window has closed. Use a template message instead.

**Users are told to register.** Anonymous access is off and WhatsApp cannot identify the sender. Turn on `Allow anonymous users` on the bot and on the agent if that traffic should be served.

## Next step

Connect another platform, such as [Telegram](telegram.md), or review how agents use channels in [Agents](../../usage/agents.md).

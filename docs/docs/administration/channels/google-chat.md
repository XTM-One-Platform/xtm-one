# Google Chat

Connecting Google Chat lets people talk to an agent in a space or a direct message from Google Workspace, and lets agents post messages back into spaces.

Read [Channels and bots](index.md) first for the concepts shared with the other platforms.

## Before you start

- You need a Google Cloud project and the rights to enable APIs and create service accounts in it.
- You need a Google Workspace domain where the Chat app can be published to your users.
- You need administrator rights in XTM One.
- The platform must be reachable from the internet over HTTPS, because Google Chat posts messages to it.

## Step 1: Configure the Chat app in Google Cloud

1. In the Google Cloud Console, enable the `Google Chat API` for your project.
2. Open the Chat API configuration page and set the app name, description, and avatar. This is what your users see in Google Chat.
3. Enable the interactive features so the app can receive messages, and choose to receive them in spaces and direct messages.
4. Under the connection settings, select `HTTP endpoint URL` and paste the webhook URL shown on the Google Chat card in XTM One — it ends with `/api/channels/googlechat/webhook`.
5. Set the visibility so the people who should use the app can install it.

## Step 2: Create a service account

1. Go to `IAM & Admin` > `Service Accounts` and create a service account for the Chat app.
2. Create a JSON key for it and download the file. This is how the platform sends messages back to Google Chat.
3. Note the project number from the project overview page — it is the numeric identifier, not the project ID.

## Step 3: Connect the bot in XTM One

1. Go to `Settings` > `Channels & Bots` and click `Configure` on the Google Chat card.
2. Paste the entire contents of the service account JSON key file into `Bot Token / Access Token`.
3. Paste the project number into `Google Cloud project number`. This lets the platform verify that incoming requests really come from Google Chat.
4. Click `Save Channel`.

!!! warning "Set the project number"
    Without it, the webhook accepts any request that reaches the URL. With it, every request must carry a valid Google-signed token issued for your project.

## Step 4: Enable Google Chat on an agent

Open the agent, go to `Configuration` > `Channels & Bots`, and enable Google Chat:

- Turn on `Receive` so the agent answers incoming messages.
- Turn on `Publish` if the agent should post on its own.
- Adjust `Allow anonymous`, `Allow private`, and `Only mention` within what the bot allows.

Whitelist entries are space names, in the `spaces/AAAAAAA` form used by Google Chat.

## Step 5: Add the app to spaces

In Google Chat, open the space and add the app from the space menu, or start a direct message with it. Like every other channel, the bot only sees the spaces it belongs to.

## What to expect on Google Chat

- Google Chat provides the sender's email address, so users are matched to their XTM One account automatically and get their own permissions, preferences, and memory.
- Mentions are detected, so `Only when mentioned` works in spaces. Google Chat has real threads, so once the bot has answered in a thread it keeps following that thread without a new mention, and `/stop` mutes a specific thread.
- There is no typing indicator in the Google Chat API, so a long answer arrives with no visible activity beforehand.
- Files uploaded with a message reach the agent as files of the conversation, and a file sent without text is still answered. A file larger than the platform's 40 MB upload limit is not passed on, but the message is. A Google Drive file shared in a message is named to the agent with its link, but the Chat app cannot download it: Google Chat only serves uploaded content to an app, and Drive needs the user's own authorization, so an agent reads it through a Google integration.

## What the agent can do in Google Chat

When `Publish` is on, the agent can send a message to a space, list the spaces it belongs to, read basic information about a space, and delete one of its own messages. It sends text only: Google Chat lets an app upload a file only on behalf of a signed-in user ([media.upload](https://developers.google.com/workspace/chat/api/reference/rest/v1/media/upload) requires user authentication), which the Chat app's service account is not.

## Best practices

- Restrict app visibility in Google Workspace to the groups that should use it, then use the receive whitelist for finer control per space.
- Use `Only when mentioned` in general-purpose spaces and leave it off in a space dedicated to the agent.
- Keep the service account key in a secrets manager. Rotating it is a matter of creating a new key and pasting it with `Edit credentials`.

## Troubleshooting

**Google rejects the endpoint or the app never delivers messages.** Confirm the Chat API is enabled, the app is configured for an HTTP endpoint, and the URL matches the one shown on the card.

**Every request is rejected.** The project number is wrong. It must be the numeric project number, not the project ID.

**The app answers in direct messages but not in a space.** It has not been added to that space, the space is not in the receive whitelist, or a mention is required.

**The agent cannot post.** The service account key is missing or invalid — the key is what authenticates outgoing messages, separately from the incoming webhook.

## Next step

Connect another platform, such as [Slack](slack.md), or review how agents use channels in [Agents](../../usage/agents.md).

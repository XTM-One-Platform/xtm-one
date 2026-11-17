# Mobile companion

!!! note "Off by default"
    The mobile companion is turned on by your administrator with the `MOBILE_COMPANION` feature flag (`ENABLED_FEATURES`, see [Configuration](../deployment/configuration.md)). Until then, a phone opens the web app, and the Phones tab of your profile is not shown.

On a phone, XTM One is a companion to the platform rather than the full platform on a small screen. It shows what you need away from your desk, and the platform reaches you there with notifications.

The device decides, not the size of the screen. A phone gets the companion even held sideways, and a tablet or a computer gets the full platform.

## The three tabs

- **Today**: the platform's home dashboard (XTM One's command center, or the Copilot dashboard), the one the web app opens on, under a short greeting.
- **Inbox**: your notifications, the same ones as the bell of the web app. What waits on you comes first and is answered right there: approve or reject the tool calls a run proposes, or answer its question.
- **Ask**: the assistant.

Any other page of the platform, for example a link to an agent's settings, says it is made for a computer, with a button back to Today.

What you read on the phone is read on the computer too, and the other way around: read state is kept by the platform, not by each browser.

## Install it as an app

You can install the companion on your home screen. It then opens full screen, like any other app.

- **Android (Chrome, Edge, Samsung Internet):** tap `Install` on the banner at the top of the companion, or `Install the app` in the menu next to the greeting on Today.
- **iPhone (Safari):** the banner reminds you how: tap `Share`, then `Add to Home Screen`.

`Not now` hides the banner on that phone for a month. A long press on the installed app's icon offers its tabs as shortcuts.

The installed app keeps no offline copy of your data or conversations: it needs a connection, and shows a short "You are offline" page when there is none.

## Notifications on your phone

Tap `Turn on notifications` on Today or in the Inbox, then allow notifications when your browser asks. That links the phone to your account. On iPhone, install the app first (iOS 16.4 or later): Safari gives notifications only to installed apps.

Your phone then receives what your notification bell shows:

- runs waiting for your approval or your answer;
- the results of runs set to notify you;
- reminders an agent scheduled for you;
- messages an agent sends you (see below);
- integration problems, security audit findings, API keys about to expire, and the other notices of the bell.

A tap opens the notification's page in the companion.

### Approve or reject from the notification

A run waiting on tool approvals arrives with `Approve` and `Reject` buttons (on Android; Safari on iPhone shows no buttons, the notification opens the Inbox instead; with a [PIN lock](#lock-the-companion-with-a-pin) on, there are no buttons either). A button decides every tool call the run waits on, exactly as the web app would, and it works:

- once: the other button, and any copy of the notification, stop working as soon as the run is decided, from anywhere;
- for 15 minutes;
- only if the run still waits on the same tool calls;
- only while the phone it was sent to is still linked to your account.

To look at the details first, tap the notification instead: the Inbox shows each tool call and its arguments.

### Manage your phones

`Profile > Phones` on the computer lists your linked phones. For each one you can:

- send a test notification;
- turn notifications off or on;
- choose what it notifies about (runs, reminders, messages from agents, analytics, flows, security audit findings). Approvals and questions waiting on you always notify;
- set quiet hours (22:00 to 07:00 in your time zone): nothing is pushed in between, and what arrives waits in the Inbox;
- unlink it: it stops receiving notifications, and the buttons of the notifications already on it stop working.

The QR code on that page opens the platform on your phone. It holds only the address: you sign in on the phone as usual.

An administrator can see and unlink a person's phones (for example when someone leaves), and cannot send them anything. The `Phone notification content` option (`Settings > Access and security`, or `PHONE_NOTIFICATION_CONTENT`) set to `minimal` makes every phone notification say only "New notification", with no buttons, for deployments that want nothing on a lock screen; so does a [PIN lock](#lock-the-companion-with-a-pin), for the person who turned it on. The content of a notification is encrypted for the phone: the browser's push service (Google, Apple, Mozilla or Microsoft) carries it without being able to read it.

## Lock the companion with a PIN

A PIN of your own keeps your data out of reach of anyone who picks up your phone. While the PIN lock is on, the companion shows nothing and approves nothing until the PIN is entered: not your home dashboard, not your name, not a notification's content.

- Turn it on from the companion (`Today` > menu > `Protect with a PIN`) or from `Profile > Phones` on the computer. A PIN has 6 to 8 digits, and cannot be one digit repeated or a run like `123456`.
- The companion asks for it each time it opens, after half a minute in the background, and after 10 minutes without use. `Lock now` in the `Today` menu locks it at once.
- Five wrong PINs in a row sign the phone out. Sign in again on the phone to continue.
- While the PIN lock is on, a phone notification says only "New notification", with no `Approve` or `Reject` buttons: a lock screen shows nothing either. Tap it, enter the PIN, and the Inbox has the details.
- To change the PIN or turn the lock off, open `Profile > Phones` on a computer. Forgot your PIN? Sign out on the phone, choose a new one there (the computer never asks for the current one), then sign in again on the phone.

The PIN applies only to the companion on a phone. On a computer, nothing changes.

An administrator can require a PIN for everyone with the `Require a PIN on the mobile companion` option (`Settings > Access and security`, or `COMPANION_PIN_REQUIRED`). Each person then chooses a PIN the first time the companion opens on their phone, and cannot turn it off.

## What agents can do with your phone

- **Notify you.** An agent with the `Phone Notifications` built-in tool can send a short notification to your linked phones, for something that deserves your attention while you are away: a critical finding, a run that needs you, a result you asked to be told about. It reaches only the person the agent acts for, follows your quiet hours and choices, links only to pages of the platform, and is capped (3 per run, 20 per hour across all agents). It also lands in your bell.
- **Remind you.** Ask any agent to remind you of something ("remind me tomorrow at 9 to check the patch window"). The reminder is a [scheduled work item](scheduled-work-items.md), and it reaches your phone when it is due.
- **Ask you.** When a run needs your approval or your answer, the notification brings it to your phone, with buttons for approvals.

## Share to XTM One

On Android, once the app is installed, `XTM One` appears in the share sheet of other apps. Share a link, some text, a screenshot or a document: the companion opens a new conversation with it in the message box and the files attached. Nothing is sent until you add your question and send it.

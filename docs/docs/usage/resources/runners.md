# XTM One Desktop and runners

**XTM One Desktop** is the native XTM One app for Windows, macOS, and Linux. It
gives you two things in one install:

- **A real window for your agents.** Your chat, your conversation history and
  your agent selection - everything the web app does - in a native window that
  stays in your system tray and comes back with one shortcut
  (`Ctrl+Alt+Space`).
- **An assistant that can act on the machine you are sitting at.** From that
  chat, agents can run commands, read and edit files, capture a genuine
  full-desktop screenshot, and drive a browser you are already logged into - on
  *this* machine, within its permission model, with your approval inline in the
  chat, and with every command audited.

The second half is a **runner**: the small pull-based daemon the Desktop app
embeds, which connects outbound to XTM One and pulls the work agents send to
this machine. A runner also exists on its own, without the window: the
**headless runner** you install on servers, VMs, and MDM-managed fleets where
nobody sits at a desktop (see [Install headless](#install-headless-command-line)).
Both appear as *machines* on the XTM One Desktop page, both obey the same
permission model, and both are what the `computer_use` agent tools target.

Runners exist for the cases a headless browser cannot cover. When an auditor
wants a screenshot with the real desktop, the OS clock, and the signed-in
identity - not an AI-generated or headless render - a runner captures the true
screen of a real session. The same channel lets an agent operate an
authenticated browser or run a check directly on a workstation or server.

Runners are **pull-based**: the app connects outbound to XTM One and pulls work.
The platform never opens a port on your machine, and there is nothing inbound to
firewall.

## Install XTM One Desktop

Everything starts from the **XTM One Desktop** page: open your user menu, at
the top right, and choose `Get XTM One Desktop` (`My machines` inside the
Desktop app), or search for *Get XTM One Desktop* in the command palette. On a personal machine the graphical installer needs no
command line; for servers and fleets there is a fully headless command-line
path (see the next section).

1. Open the **XTM One Desktop** page and download the installer for your
   operating system - the page detects it and offers it first; the other
   platforms are listed below. The main button is the x86_64 (Intel/AMD) build;
   ARM machines take the `arm64` link next to it. Macs need no choice at all -
   one universal bundle covers both Intel and Apple Silicon.
2. Run the installer (`next, next, finish` on Windows; drag to `Applications` on
   macOS; an AppImage or a `.deb` / `.rpm` on Linux). On Linux it needs glibc
   2.35 or newer: Ubuntu 22.04, Debian 12 or a later distribution.
3. Launch **XTM One Desktop**. On the `Connect this machine` screen, paste this
   instance's URL and an **enrollment key**, then connect. The chat window opens
   on your instance: sign in as you would in a browser. (Single sign-on opens
   your system browser - see [Signing in](#signing-in) below.)

!!! note "If the OS blocks the first launch"
    A build that is not code-signed for your operating system trips the
    first-open gate: Windows SmartScreen shows *"unrecognized app"* (click
    **More info > Run anyway**), and macOS reports it *"could not verify ...
    is free of malware"* - click **Done** (not Move to Trash), then open
    **System Settings > Privacy & Security**, click **Open Anyway**, and
    launch again. This only ever concerns the first manual install:
    self-updates are signature-verified and never re-prompt, and the headless
    install path is not affected.

Create an enrollment key from the same page, under **Connect a machine**. The
key carries the permissions the enrolled machine starts with, as one of three
presets:

- **Desktop (recommended)** - every capability enabled. This is the preset for
  the machine you sit at: usable from the very first message. Shell commands
  run as the agent sends them; turn on **Shell commands need approval** on the
  key if you want each one held for your approval in the chat (off unless you
  turn it on).
- **Locked down** - screenshots and browser control only, no approval gate;
  enable more later from the machine's card. The historical default.
- **Custom** - choose the mode, the capabilities and the approval gate yourself
  (this is where fleet keys for servers are configured).

A key is single-use for the first machine but can be configured for reuse
across a fleet, and it can carry labels so an unattended roll-out lands already
configured. After enrollment the app stores a durable credential in the
machine's OS keychain and stays connected across reboots - you never paste the
key again.

!!! info "One record per machine"
    Each enrolled machine is one runner. Installing on ten machines gives you ten
    runners you can name, label, and target individually - not ten integrations.
    Re-enrolling a machine that is already in the list (a reinstall, a re-run of
    the install script) updates its existing record - new credential, refreshed
    host facts, your name/permissions/labels kept - instead of creating a
    duplicate. Revoked runners stay revoked: enrolling that machine again
    deliberately creates a fresh record.

## Install headless (command line)

Servers, VMs, and MDM-managed fleets rarely have someone sitting at a desktop,
and have no use for a chat window. For those machines, the XTM One Desktop page
also shows a one-line install command per operating system under `Servers and
headless machines (command line)`: copy it, replace the key placeholder with an
enrollment key, and run it on the target machine - no graphical session is ever
needed. What it installs is the **headless runner**: the same binary as the
Desktop app without the window and the tray, serving shell, file and browser
commands to your agents exactly as before.

The command downloads the runner build matched to your instance's version,
verifies its integrity, installs the binary, enrolls the machine, and registers
it to start automatically (a LaunchAgent on macOS, a systemd user service on
Linux, a logon task on Windows). The same binary is also a CLI afterwards:
`xtm-one-runner status`, `xtm-one-runner run`, and `xtm-one-runner reset` manage
it without any UI.

If the integrity check cannot run - the host has no `sha256sum` / `shasum`, or
no `.sha256` is available next to the archive - the installer still installs
but warns `INTEGRITY NOT VERIFIED` and says why. For fleet and MDM rollouts,
add `--require-checksum` (`-RequireChecksum` on Windows) or set
`XTM_RUNNER_REQUIRE_CHECKSUM=1` so an unverified binary is refused instead. A
checksum that does not match, or a `.sha256` that holds no checksum, always
stops the install. The checksum is served by your instance next to the
archive, so it proves the download arrived intact rather than who built it:
use an `https://` address for your instance, since over plain HTTP both could
be swapped in transit and the installer warns about it. If the installer
cannot get the archive from your instance, it reports the HTTP status it
received, so a proxy or firewall problem - or a sign-in page from an
authentication proxy answering in its place - is not mistaken for a build
without embedded downloads.

The installer also checks the **release signature**: every release signs its
runner archives, and the installer verifies the archive's `.minisig` against
the release public keys it carries (the signature names which key made it, so
a new release key can be trusted before releases are signed with it), so when
that signature is there to check, a binary that was not built by the release -
swapped on a mirror or behind a proxy, together with its checksum - is
refused. The signature must also have been made for the archive this machine
needs, so a signed build of another platform or flavor served in its place is
refused too. Release signatures also name the runner version they were made
for: when the archive comes from your instance, a signature for another version
- an older signed build served in place of the current one - is refused, and
the installer prints the version it verified. Archives signed before versions
were signed install as before, and say so. The same checks guard the runner's
own self-updates, which never install a build older than the running one. On
macOS and Linux it uses `minisign` when installed, otherwise OpenSSL 3,
otherwise (when upgrading) the runner already installed; on Windows it needs
nothing extra. A
signature that does not verify always stops the install. When the signature
cannot be checked - no `.minisig` next to the archive (a build made without
the release signing key), one that is not a signature at all (an error page
from a mirror or a proxy, for example), a signature whose trusted comment
names no file, or no verifier on the host (for example a first install on a
macOS machine with neither `minisign` nor OpenSSL 3) - the installer warns
`SIGNATURE NOT VERIFIED`; add `--require-signature` (`-RequireSignature` on
Windows) or set `XTM_RUNNER_REQUIRE_SIGNATURE=1` to refuse instead. Without
it, whoever can swap the archive can also withhold its `.minisig` and leave
only that warning, so always add it for a mirror (`--download-base`) and for
MDM or other unattended rollouts, where nobody reads the warning. If you build
and sign the runner with your own key, an instance built from your fork serves
installers that already trust the key of its `tauri.conf.json`; an installer
taken from anywhere else needs `XTM_RUNNER_MINISIGN_PUBKEY` set to your public
key: the key line of your `.pub` file, or the base64 value `tauri.conf.json`
carries. The installer then says which key ID it checks against - the ID your
`.pub` file prints - so you can confirm it is yours.

### The headless build (Linux servers)

On Linux the installer picks between two builds of the same runner:

| Build | Ships | Capabilities |
| --- | --- | --- |
| Desktop (default) | Everywhere else, and any Linux with a graphical session | Screenshots, mouse/keyboard, shell, files, browser |
| Headless | Linux hosts with no graphical session | Shell, files, browser |

This is not a matter of taste. The desktop build links the desktop libraries
(GTK, X11) at load time, so on a server without them it fails before it runs -
no flag or setting can work around that. The headless build links none of them.

The installer decides from the environment: no `DISPLAY` and no
`WAYLAND_DISPLAY`, and no graphical session on the machine, means headless — so
SSH-ing into your own Linux desktop to run the installer still gets you the
desktop build. Force either one with `--headless` or `--desktop` (or
`XTM_RUNNER_FLAVOR=headless` for cloud-init and MDM). The choice sticks: a
headless runner is only ever offered headless updates.

Two things a server does differently:

- **Credential storage.** Servers usually run no keyring daemon. When there is
  no OS keychain, the runner stores its credential in a file readable only by
  its own user, next to its config, and says so in the log.
- **Staying up after logout.** The service runs under your user account, so
  enable lingering or it stops when your SSH session ends. The installer does it
  for you when it can: `sudo loginctl enable-linger <user>`.

!!! note "Desktop capabilities need a desktop"
    Screenshots and mouse/keyboard control require a logged-in graphical
    session. Shell commands, file operations, and browser control work on fully
    headless servers. A headless runner asked for a screenshot answers with a
    clear error rather than a black frame - assign that work to a runner on a
    real desktop.

!!! note "`libxdo.so.3: cannot open shared object file`"
    A runner installed **before** this error was fixed needs one library that a
    stock Ubuntu desktop does not ship (it lives in `universe`). Install it once
    with `sudo apt install libxdo3`; the runner then starts and updates itself
    to a build that no longer needs it. Newer builds synthesize input over the
    X11 protocol directly and require nothing extra to install. That is a
    packaging fix, not new Wayland support: synthetic input reaches X11 and
    XWayland windows, so a Wayland-native application still does not receive
    `computer_action` keystrokes or clicks.

## The XTM One Desktop app

Once enrolled, the app opens your instance in its own window and lives in the
system tray (or menu bar) behind the XTM One icon. From there you can:

- open the chat window - or press `Ctrl+Alt+Space` from anywhere
- see live status - `Online`, `Connecting`, `Reconnecting`, or
  `Update required` when your instance no longer accepts this version of the
  app (update it from the connection panel)
- open the **connection panel**: the machine's name, the enrolled instance URL,
  the app version, and the host facts reported to the platform (OS build,
  available shells, installed browsers, monitors, timezone and locale)
- change a few settings - automatic updates, launch-at-login, and the browser
  CDP URL
- unenroll the machine locally

The same actions are in the app's own menu, so you never have to find the tray
icon: the **File** and **Help** menus of the chat window on Windows and Linux,
the menu bar on macOS (where `Cmd+,` opens the settings). **Check for
updates...**, in the Help menu (the app menu on macOS), asks your instance for
a newer build and, when there is one, installs it and restarts the app.

Closing a window hides the app to the tray; the runner keeps serving. To stop
the app, use **Quit** in the tray or in the app's menu; while an update is
being installed, Quit waits up to a minute for you to answer or cancel its
password prompt, and the connection panel says so. The window stays on your
instance: links that would open a new tab, and any link to another site, open
in your default browser, and downloads land in your Downloads folder. The app
updates itself from signed bundles matched to your instance, so a fleet stays
current without anyone touching each machine. When an update restarts the
app, the windows you had open come back, with the chat on the page you were
on.

On Linux, only the AppImage updates unattended. A `.deb` / `.rpm` install
updates through your package manager, which asks for an administrator
password, so the app does not install those updates on its own: it opens the
connection panel to announce the new version, and **Install version ...**
installs it when you choose to. For a Linux desktop that should stay current
with nobody at the keyboard, install the AppImage (or the headless build on a
server). A `.deb` / `.rpm` install from an earlier version gets this behaviour
through one last update applied the old way: until you accept it, that version
asks for the password on its own each time it checks for updates.

### Upgrading from XTM One Runner

An installed runner updates itself into XTM One Desktop; the enrollment, the
credential and the launch-at-login choice are kept. A few things change name
along the way:

- **Windows**: XTM One Desktop installs into a new `XTM One Desktop` folder and
  removes the former install. The Start menu and desktop shortcuts and a taskbar
  pin keep their `XTM One Runner` name but start XTM One Desktop; `XTM One
  Runner` leaves Installed apps. A machine updated before this was fixed may
  still list `XTM One Runner` in Installed apps: do not uninstall it, as that
  removes the machine's enrollment and sign-in. The next update removes it for
  you.
- **macOS**: the app bundle is swapped in place. A Mac upgraded from the runner
  keeps its old folder name until you reinstall from a fresh download.
- **Linux**: the AppImage updates in place. The `.deb` / `.rpm` package is now
  named `xtm-one-desktop`; it declares that it replaces `xtm-one-runner`, so a
  package-manager upgrade removes the old package for you.

Headless runners on servers are not affected: they keep their own update
channel and the same command line.

### Signing in

Email and password work inside the window like anywhere else. **Single sign-on
runs in your system browser**: identity providers refuse or degrade embedded
browsers (conditional access, passkeys, Windows Hello, "use the browser you are
already signed in to"), and the app's window shares no session with the
browser you use all day. So a `Continue with ...` button in the app opens your
browser on your instance's sign-in; you finish there as usual, the browser then
shows **which machine** is asking (the name of your XTM One Desktop machine and
who enrolled it) and asks you to confirm, and the app signs itself in a moment
later. The browser tab keeps no session - you asked to sign in on the desktop.

The confirmation matters: a sign-in request can only be started by an enrolled
XTM One Desktop, and can only land on a machine that is **yours** (you enrolled
it) or **company-managed** (a shared machine your administrator provisioned). A
machine someone else enrolled is refused, even one shared with your group: that
share lets the machine run your commands, it does not let whoever holds the
machine act as you. If the machine named is not yours, choose **Not my
machine**: the request is dropped so nobody else can complete it, the refusal
is recorded in the audit log, and you should tell your administrator.

Once signed in, **you stay signed in** when the app restarts - after an
update, a reboot or a crash - under exactly the rules of the browser: the
session ends when its lifetime or its idle timeout set by your administrator
ends it (see [Session lifetime and idle timeout](../../deployment/authentication.md#session-lifetime-and-idle-timeout)),
or when you sign out. The app keeps your session in the machine's OS keychain
(Windows Credential Manager, macOS Keychain, Secret Service on Linux) next to
its own credential, so it no longer depends on the window's browser storage,
which an unclean shutdown can damage.

### Acting on this machine

Every message you send from the app carries the machine it was typed on, so
the agent's `computer_use` tools default to it: "run the tests", "what is in
`~/Downloads`", "take a screenshot" act *here*, without naming a runner. Fleet
targeting still works - name another machine and the agent reaches it, within
the usual rules. Above the composer, a small bar shows the machine, its
connection state and a switch to turn local execution off for a conversation.

When the machine's permission model holds a shell command for approval (the
**Shell commands need approval** switch, on the enrollment key or on the
machine's card), the request appears **in the chat**, above the
composer: what the agent wants to run and on which machine, with **Approve**,
**Always allow**, **Reject** and **Always block**. The agent waits for your
decision and the command's output lands in the same reply. A decision you do
not take in time leaves the command queued on the XTM One Desktop page, where
you can still approve it.

### Seeing what the agent does on a machine

Every command an agent sends to a machine shows up in the conversation as a
card, above the reply it leads to - in the Desktop app and in the browser
alike:

- a **shell command** is a small terminal window: the prompt and the command,
  a live timer while it runs, then its exit code, how long it took and its
  output (errors in their own colour), with buttons to copy the command or the
  output and a larger view;
- a **file read or written** shows its path and a preview of the content;
- a **folder listing** shows the folder's entries, folders first;
- a **screenshot** shows a thumbnail you can enlarge;
- a **mouse, keyboard or browser action** says what was done (text typed
  through the agent is shown as a character count, never as text).

A command held for your approval, blocked by the machine's policy, refused
because the machine was busy or offline, or cut short when the reply ended
says so on its card. The cards are saved with the conversation, so they are
still there after a reload and for the people you share it with; a very long
output is shortened on the card, and the reply's reasoning details (the
**i** icon) keep more of it.

While a shell command runs, its card has a `Stop` button (only the square icon
on a narrow card). Stopping ends the command and every process it started; what
it printed until then stays on the card, and the agent is told that the command
was stopped and when. You can stop a command that runs for you, and so can the
people who manage the machine. A stopped card says who stopped it:
`Stopped by you`, or `Stopped by` followed by the person's name for everyone
else who reads the conversation.

Inside **XTM One Desktop**, the paths on a card whose command ran on *this*
machine get **Open** (with its default application), **Show in folder** and
**Copy path**, and so do absolute paths the agent quotes in its reply, such as
`C:\Users\me\Desktop\disks.txt` or a `file:///` link: a click opens the item,
`Alt` (`Option` on macOS) + click shows it in its folder. Only documents,
images, media and plain-text files are opened that way - an executable, a
script, a shortcut, an installer, a web page or a document able to carry
macros is only ever shown in its folder - and a network address (a
`\\server\share` path, a link that leads onto one) is never opened from the
chat; a drive letter you mapped to a share yourself works like any other drive.

When a long reply finishes while the app is behind another window, the
taskbar entry flashes (the Dock icon bounces on macOS) so you know it is
ready.

### Uninstall and re-enroll

Uninstalling the app removes the local enrollment with it - the stored
credential, your sign-in, the configuration, and the launch-at-login entry -
so the next install always starts fresh at the Connect form. The runner's record on the
platform is deliberately left alone (an uninstall can happen offline): revoke
it on the XTM One Desktop page when you retire a machine for good.

If a running runner's record is revoked on the platform, the app shows a fatal
error with a **Re-enroll this machine** button: one click drops the dead local
credential and reopens the Connect form, prefilled with your instance URL and
ready for a fresh enrollment key.

Revoking takes effect on every server at once: from that moment no new command
is queued for the machine or handed to it and nothing more is accepted from it,
its queued and running commands end, and its live connection closes right away,
or at its next heartbeat (every 30 seconds by default) at the latest. A command
handed to it a moment before the revocation may still reach it; it is recorded
as cancelled and its result is refused.

### Permissions the first time

- **macOS** asks for **Screen Recording** and **Accessibility** permission the
  first time an agent captures the screen or controls input. Grant them in
  `System Settings > Privacy & Security`; the app links you straight there and
  shows whether each one is granted.
- **Linux** capture and input need a graphical session. On Wayland, capture goes
  through the desktop portal; the app warns when the current session cannot be
  captured.

## What an agent can do on a runner

When an agent has the `computer_use` built-in tool and at least one of your
runners is online, it can:

- take a **full-desktop screenshot** (the audit-evidence case)
- perform **mouse and keyboard** actions
- **run a shell command**, and keep a long one running in the background (see [Long commands in the background](#long-commands-in-the-background))
- **read, write, and list files**
- **control a browser** you already authenticated

Browser control attaches to a browser you started with remote debugging, at
the browser CDP URL of the app's settings (`http://127.0.0.1:9222` by
default): start Chrome or Edge with `--remote-debugging-port=9222` and a
profile of its own (`--user-data-dir=...`; recent versions ignore the port on
your everyday profile), and sign in there once. The agent acts on one tab and
keeps it from one command to the next: `list_tabs` shows the open tabs, the one
the agent drives and the one you used last, and `select_tab` switches to
another tab (the one you used last when no tab is named), for instance after
you signed in in a new tab. Opening tabs or signing in in that browser does not
break the connection; if it drops anyway, the next command reconnects to the
same tab. When the browser cannot be reached because an administrator policy
turns its remote debugging off (`RemoteDebuggingAllowed`), the error says so.

Typing into a page field brings the driven tab to the front and focuses the
field first, then reads it back: when the field still holds nothing (it could
not take the focus), the action fails and names the field, never the text.

Typing and scrolling are bounded by the command's time budget, never by a
length: text of any size is typed in small chunks, and when the budget runs
out the action stops between two chunks and says how far it got, so nothing
is typed after the command is reported as timed out. A shell command's output
and its errors are each kept up to 1 MiB; past that the output is cut and the
result says so. The data a command returns - that output, a folder listing, a
page's content - may take up to 16 MiB once written as JSON, where escaping
counts (a control character takes six bytes), and a file it returns up to
`RUNNER_MAX_RESULT_BYTES`. A larger result is not kept: the command fails at
once and its error says why, so the agent can ask for less.

Flows can drive runners too: the `Runner command` node dispatches the same
command kinds through the same transport — the runner's permission mode,
allow-lists, and approval gating apply unchanged — and can wait for the
result. A flow reads at most 1 MiB of that result, written as JSON: a larger
one reaches the node's outputs cut to fit, each cut text ending with a
`[TRUNCATED ...]` marker and `flow_truncated` giving its full size, while the
machine's command history keeps it whole. Flows can also react to runner lifecycle through the built-in
`Runner online`, `Runner offline`, and `Runner command completed` event
types with the `On event` and `Wait for event` nodes. `Runner offline` fires
when a machine is revoked while online, or when it has been silent for
`RUNNER_OFFLINE_AFTER_SECONDS` (75 seconds by default): the scheduler's
presence sweep, which runs about once a minute and handles up to 500 machines
per pass, then declares it offline. `Runner online` fires once the machine is
heard from again, so the two always alternate. A platform restart or upgrade
drops every runner connection for a few seconds; a machine back within that
window fires neither. The machine rides the restart out: it retries its polls,
heartbeats and result uploads with back-off, so a command that finishes while
the platform restarts still reports its result once the platform is back
within about 50 seconds.

The agent waits for a command's result as long as the machine may take to
report it: from the moment the machine takes the command, its execution limit
and then those 50 seconds of upload retries (205 seconds in all by default,
`RUNNER_RESULT_WAIT_TIMEOUT_SECONDS` sets a fixed wait instead), never longer
than the agent run allows one tool call. When that limit ends the wait first,
the agent is told the command may still finish.

A command is never sent later to nobody: when the agent stops waiting for it
(its wait ran out or you stopped the turn) or the flow run waiting for it ends,
a command the machine had not picked up yet is cancelled, including one still
waiting for your approval in a flow, and one the machine already took is not
sent again. If that one still finishes, its output is in the machine's command
history.

Screenshots and file reads come back into the conversation as inline images or
downloadable files, exactly like other tool results, so they can flow straight
into an evidence workflow. The PNG is also forwarded to the model as a native
vision block (high-detail, PNG preserved) so the agent can read the URL bar,
labels, errors, and OS clock itself - it should never ask you to describe or
verify the capture.

### Long commands in the background

A command that takes long - a large download, a build, a server that keeps
running - can run as a **background job**. The agent gets an answer at once
with the job's id and its first lines of output, and the command keeps running
on the machine, its output kept in a log file there. The agent then follows the
job with its `shell_jobs` tool: it lists the machine's jobs, reads a job's
output from where it left off, and stops a job with every process it started.

In the conversation, the card of a background job reads `Background` while the
job runs, and keeps its `Stop` button after the reply ends. Stopping a job asks
you to confirm first. Once the job is over, the card shows its exit code
(`Ended` when there is none), or `Stopped by you` if you stopped it.

On the **XTM One Desktop** page, the card of a machine you manage lists its
`Background jobs`, running ones first, each with a `Stop` button. The list is
read from the machine when you open its card, after a stop, and when you click
`Refresh`. Every read is a command in the machine's history, so the list is not
refreshed on its own. When the machine is offline, busy or does not answer, the
list says so.

A machine whose app is too old for background jobs refuses them before
anything is sent, and the agent is told which version it needs: update XTM One
Desktop or the runner, or run the command without the background option.

## Control what a runner allows

Each runner carries its own permission model, set on the XTM One Desktop page
(the machine's card):

- **Restricted** (default) - least privilege. Only the capabilities you enable
  are allowed (screenshot, mouse/keyboard, shell, file operations, browser
  control). Enabling a capability is the consent: once it is on, it runs
  without further prompts. **Shell commands** are the exception and get finer
  control:
    - **Allow-list** (standing approvals) - a matching command runs
      immediately and never asks for approval. With approval off, only
      allow-listed commands can run at all.
    - **Block-list** (standing refusals) - a command containing a blocked word
      or phrase is always refused. It never runs and never reaches the
      approval queue, and the block-list wins over the allow-list.
    - **Require approval** - any shell command not covered by the lists waits
      for a human decision: inline in the chat when the command was asked for
      from XTM One Desktop, otherwise on the XTM One Desktop page. When
      approving or rejecting, **Approve & always allow** / **Reject & always
      block** saves the exact command, whatever its length, into the
      corresponding list, so the same command never asks again. Approval
      applies to shell commands only -
      screenshots, input, file operations and browser control never queue.
    - File operations can additionally be narrowed with a **path allow-list**.
      The platform checks the path as written against it, and the runner
      resolves links to see where the path really leads. A path inside the
      allowed folders that leads outside them through a link (a symbolic
      link, a junction, a mount point) still runs by default, as it always
      has, with a warning in its result and in the runner log; with
      `RUNNER_STRICT_PATH_CONFINEMENT=true` (see
      [Configuration](../../deployment/configuration.md)) it is refused (to
      reach what such a link leads to, add that folder to the allowed paths).
      A link that stays inside the allowed folders keeps working either way, an
      allowed folder that does not exist yet can be written into (the runner
      creates it), and a path written as a network share or automount path
      (`\\server\share`, `/net/host`) is only looked up inside an allowed
      folder that is itself on it. After opening a file or folder the runner
      re-checks the handle it actually got: its type in every mode, and in
      strict mode its real path too, so a link swapped in between the check
      and the open is caught and nothing is truncated or written until that
      check passes (only a folder the write has to create is made before it,
      so a link swapped in at that moment can leave an empty folder where it
      leads; on a volume the OS cannot resolve paths on, some RAM disks
      and VirtualBox shared folders, the handle cannot be named and the
      pre-open check stands). What no mode prevents: a link in an allowed
      folder that leads onto a share is followed to see where it leads, which
      can reach that host, and a hard link is the file itself under a second
      name, so one inside an allowed folder is served like any file there.
- **Full control** - a deliberate opt-in that allows every capability and
  every command with no lists and no approval. It is clearly flagged in the
  UI, and every command is still audited. An agent listing its runners
  (`list_runners`) sees every capability on a full-control runner, whatever
  capability toggles were saved before the switch.

Whatever the mode, every command is recorded server-side with its result, and
**revoking** a runner invalidates its credential immediately - the next command
fails and the app stops. A command still queued when a runner's permissions are
tightened is checked again when it is delivered, and cancelled if they no
longer allow it.

File operations read and write regular files as they always have. A path goes to the operating system as written and is never judged by its name: what the OS opens for it decides, and when the OS refuses it, its error is the result. A file that is not a regular file - a character or block device (a serial port, `/dev/urandom`, `/dev/zero`, `NUL` on Windows, a volume or disk opened raw such as `\\.\C:`), a named pipe (FIFO) or a socket, as the OS reports the object opened - is also read and written as before, on every runner, with a warning in the result and in the runner log saying what it is. With `RUNNER_STRICT_PATH_CONFINEMENT=true` (see [Configuration](../../deployment/configuration.md)) it is refused, on every runner, full control included; on Windows a device is refused once it is opened, before anything is read or written, since its handle is what tells. A name Windows opens as a file is a file, whatever it looks like - `con.log`, `aux.c` or `nul.txt` on Windows 11, a folder named `aux`, a drive, share or volume location behind a `\\.\`, `\??\` or `\\?\` prefix, a file in a volume shadow copy (`\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\...`) - and is served as one, with no warning, strict or not. Reading a Windows device such as `NUL`, or a raw volume, fails as it always did: Windows cannot describe it for the size check before the read. Such a file is opened without waiting and then used as a plain open would, so a named pipe with no program at its other end returns at once (empty for a read, an error for a write) instead of holding a runner worker for good; a socket cannot be opened as a file at all. A restricted runner with allowed folders only reaches one inside those folders; a device path never lies under a drive-letter folder.

A read never asks for more than a positive result size limit (`RUNNER_MAX_RESULT_BYTES`, the largest file a runner may upload) or 256 KiB, whichever is larger. What comes back as a file - binary content, or text past 256 KiB - is cut to the limit and flagged as incomplete; the 256 KiB of text that comes back inline has never met that limit, so under a smaller limit a runner from this release still returns such text whole (an older runner cuts it to the limit, as before). A limit of 0 or less keeps its meaning - every non-empty file result is refused at upload and reads are not bounded - and is reported at startup and by the [security audit](../../administration/settings.md#security-audit) (`Settings` → `Security`).

A read that stops at its size before the end of the file says so in its
result: how many bytes were read, and how to read the rest - a larger
`max_bytes`, or, at the result size limit, another way such as a shell command
that splits or compresses the file. A runner from before this release cannot
tell, so a read it returns at exactly the size it was asked for is flagged as
possibly incomplete.

## Choose which machines an agent can reach

Runners are targeted, not global:

- Give runners **labels** (for example `audit`, `eu-west`, `workstation`).
- On an agent, set a **runner selector** - specific runners or a set of labels -
  so the agent can only reach the machines you intend.
- An **assignment** can refine that selection further, never widen it, so an
  agent-driven automation can never reach a machine its agent could not. A
  flow's `Runner command` node targets one explicit runner instead — gated by
  the runner's own permission mode, allow-lists, and visibility (a flow can
  only use runners visible to the person who runs it: their own,
  group-shared, or company-managed).

When more than one eligible machine is online, mutating actions require the agent
to name the target runner explicitly, so a command can never land on the wrong
machine by accident. The `list_runners` tool lets an agent see which machines are
online before acting.

## Availability and limits

The whole subsystem is governed by platform configuration: an administrator
enables it, sets where installers are downloaded from, and can cap how many
runners a user may enroll and how many commands may run on one machine at once.
If downloads are not configured on your deployment, the page explains it and you
can still enroll a runner you obtained separately. See the runner variables in
[Configuration](../../deployment/configuration.md) for the full list.

## Fleet oversight for administrators

The XTM One Desktop page lists **your** machines: the ones you enrolled, the
ones shared with you, and the company-managed fleet - for administrators too. To
see every machine enrolled with the platform, whoever enrolled it, an
administrator opens `Administration > Objects` and filters by `Machines`: each
row shows the machine's owner, operating system, live connection state
(`Online`, `Offline` with the time it was last seen, or `Revoked`) and runner
version, and opens the machine's own page with the same permission editor and
revoke / delete actions as here. Reading the command history of a machine you
neither own nor hold a configure-level share on (a company-managed machine is
yours to manage and is not counted) is recorded in the audit log. See
[Objects](../../administration/introduction.md#objects).

## Good habits

- Name each machine so you can tell an agent exactly where to work.
- Start in `Restricted` mode and enable only the capabilities you need; reserve
  `Full control` for machines you fully trust.
- Use labels for fleets and set agent selectors so each agent reaches only its
  own machines.
- Turn on shell-command approval when a person should stay in the loop, and
  build up the allow/block lists from real decisions with "Approve & always
  allow" / "Reject & always block".
- Revoke a runner as soon as a machine is decommissioned.

## Related

- The **XTM One Desktop** page (`Get XTM One Desktop`, or `My machines` inside
  the app, in the profile menu) - where you download the app, create enrollment
  keys and manage your machines.
- [Agents](../agents.md) - bind the `computer_use` tool and set the runner
  selector.
- [Configuration](../../deployment/configuration.md) - the `RUNNERS_ENABLED` and
  `RUNNER_*` deployment variables.

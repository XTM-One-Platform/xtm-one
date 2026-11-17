# XTM One Desktop (and the runner inside it)

The native XTM One client for Windows, macOS and Linux - and, in the
same binary, the **pull-based OS-control runner** it grew out of.

You install it with an ordinary installer (no command line), enroll it once
from a small panel, and it opens your instance in its own window: chat, agents,
conversation history, everything the web app does, staying in your system tray
and coming back with `Ctrl+Alt+Space`. Every message typed in that window names
the machine it was typed on, so agents can act **on this machine** through the
`computer_use` built-in tools - within the machine's permission model, with
shell commands approved inline in the chat, every command audited:

- run **shell commands** and **read / write / list files** (the coding /
  operations use case),
- capture **genuine full-desktop screenshots** (compliance audit evidence: the
  real OS clock, the signed-in account, the whole screen - not a headless
  render),
- control a **real, already-authenticated browser** over the Chrome DevTools
  Protocol,
- synthesize **mouse / keyboard input**.

The app connects **outbound** to XTM One (WebSocket fast path, HTTP long-poll
fallback) and **pulls** commands - the platform never opens an inbound port on
your machine. Fleet targeting is unchanged: any enrolled machine an agent may
see can still be named explicitly.

**Naming.** The *product* is XTM One Desktop. The *runner* remains the technical
name of the enrolled execution endpoint an agent targets - the Desktop app
embeds one, a server runs the **headless runner** (below) - and the crate, the
binary (`xtm-one-runner`), the bundle identifier, the config directory, the
keychain entry, the dist artifact names and the `/api/v1/runners` routes all
keep it: that is the identity the installed fleet self-updates under.

## Why a native app (and not a browser tab / extension / headless Chrome)

People stop using an assistant that lives in a tab: it cannot act on their
machine, it is not one keystroke away, and it disappears behind forty other
tabs. A native window with the runner inside is what Claude Desktop, ChatGPT
Desktop and Cursor set as the bar.

Headless automation and MV3 extensions also cannot produce a screenshot an
auditor will accept: no OS chrome, no taskbar clock, no genuine signed-in
identity, and (for MV3) `desktopCapture` needs a per-capture user gesture. A
native process in the interactive session captures the true desktop and can
attach to a real browser you logged into yourself.

## Install (no command line)

1. In XTM One, open the **XTM One Desktop** page (bottom of the left
   navigation, or your user menu) and download the installer for your OS.
2. Run it - it is a normal graphical installer:
   - **Windows:** an NSIS `.exe` (`next, next, finish`; installs for the current
     user, no admin prompt).
   - **macOS:** a `.dmg` - drag **XTM One Desktop** into `Applications`.
   - **Linux:** an `.AppImage` (make it executable and run it), or a `.deb` /
     `.rpm` if you prefer your package manager.
3. Launch the app. On the **Connect this machine** screen paste this instance's
   URL and a single-use **enrollment key** (create one on the XTM One Desktop
   page - the *Desktop* preset gives the machine every capability; turn on
   *Shell commands need approval* if you want to confirm each shell command in
   the chat), then connect.
4. The chat window opens on your instance. Sign in with email + password in the
   window, or with **single sign-on in your system browser** (see below).

That is it. The app stores a durable credential in your OS keychain, drops into
the system tray, and stays enrolled across reboots - you never paste the key
again.

### Signing in

Email and password work inside the window. **Single sign-on runs in the system
browser**: identity providers refuse or degrade embedded webviews (conditional
access, passkeys, Windows Hello, "use the browser you are signed in to"), and
the app's webview shares no session with the browser you use all day. A
`Continue with ...` button in the app therefore opens your browser on the
instance's own SSO route carrying a *pending login* the app opened with its
runner credential (`POST /api/v1/desktop-login/start`); you finish signing in
there, the browser shows which machine is asking (its enrolled name and who
enrolled it, read from the platform) and asks you to confirm, hands the session
over (`POST /api/v1/desktop-login/{id}/complete`), and the app - polling
`POST /api/v1/desktop-login/exchange` - signs its window in and comes to the
front. The browser tab keeps no session. The record lives 5 minutes in Redis
and holds WHO confirmed, never a token: the session is minted at the exchange,
at the one moment it is handed out. Both state changes are single atomic Redis
steps (a compare-and-set on completion, a `GETDEL` on exchange), so two
confirmations or two polls racing each other resolve to exactly one winner.
Only a first-hand browser session may confirm - not an API key, not a
cross-platform token, not an impersonation - and `Not my machine` drops the
request (`POST /api/v1/desktop-login/{id}/decline`, audited) so nobody else can
complete it in its window.

**The session survives restarts.** The web app keeps its session in
`localStorage`, which inside the app is the webview's profile - and that
storage can roll back: on Windows, one write torn by an unclean shutdown makes
WebView2's LevelDB drop every later write at each launch, so the page would
boot with a token it signed out of, or one long expired, and sign the person
out at every update. The shell therefore keeps the session itself, in the OS
keychain next to the machine's credential (`web_session.rs`): the page hands
it every change (`desktop_store_session`: its token, and the administrator's
during an impersonation; both empty once signed out) and takes it back at
launch (`desktop_load_session`), where the kept copy wins over the webview's
storage (`restoreDesktopSession()` in `frontend/src/lib/api.ts`). Both
commands answer the chat window on the enrolled origin only, accept session
tokens only, and forget the kept copy when a write fails, so a stale copy never
wins. Nothing is minted or extended: the platform still ends the session after
its absolute lifetime or idle timeout, as in a browser.

Who may sign in on a machine is a rule of **ownership, not visibility**: the
machine must be active and either enrolled by you or company-managed (a shared
machine an administrator provisioned). A machine someone else enrolled is
refused even when it is shared with your group - a group share lets the machine
run your commands, it does not let whoever holds the machine's credential act
as you on the platform, which is what a handed-over session amounts to. A
pasted link cannot start this either: only an enrolled Desktop can.

### The windows and the tray

- **Chat window:** your instance's web app, in a native, resizable window. The
  window stays on your instance: new tabs and any link to another site open in
  your default browser (the window itself never renders another origin),
  downloads land in your Downloads folder under a sanitised, never-overwriting
  name, `Ctrl` / `Cmd` `+` / `-` zoom. Closing it hides to the tray; the runner
  keeps serving. `Ctrl+Alt+Space` summons or dismisses it from anywhere.
- **Tray icon + menu:** the genuine XTM One icon sits in the tray/menu bar. Its
  menu shows live status (Online / Connecting / Reconnecting / Update required)
  and lets you open the chat, open the instance in your browser, open the
  connection panel, Settings or About, and Quit.
- **Application menu:** the same actions without hunting for the tray icon.
  On Windows and Linux the chat window has a menu bar with **File**
  (Connection panel, Settings..., Open in browser, Quit) and **Help** (Check
  for updates..., About); the connection panel keeps its tabs and no menu bar.
  On macOS the menu bar has the usual Mac layout: the app menu (About, Check
  for updates..., Settings... `Cmd+,`, Connection panel, Quit `Cmd+Q`), File
  (Open in browser, Close window `Cmd+W`), Edit, View, Window (which brings the
  chat window back once it was closed to the tray) and Help. Keyboard
  shortcuts are macOS-only: on Windows the web view never passes a key typed
  in the page to the window's menu, so a shortcut would be shown and never
  fire.
- **Connection panel:** the machine's name, connection status, the enrolled
  instance URL, the app version, and the host facts reported to the platform
  (OS build, shells, browsers, monitors, timezone/locale).
- **Settings:** toggle automatic updates and launch-at-login, set the browser
  CDP URL, and unenroll (local reset).
- **About:** version, instance URL, and links back to XTM One.

### How the page knows it is in the app

The shell injects a read-only object into every page of the instance's own
origin before any script runs:

```js
window.__XTM_ONE_DESKTOP__ = {
  version, platform, arch, runner_id, runner_name, server_url,
  features,         // ["local_paths", "attention", "session"] - what this shell can do
  open_extensions,  // what desktop_open_local_path hands to an application
}
```

The web app reads `runner_id` and sends it as `desktop_runner_id` on every chat
turn; the backend pins that machine as the default target of the agent's
`computer_use` tools and offers held shell commands inline (a
`runner_approval_required` progress frame the chat renders as an approval
card). In the other direction the page is granted, at runtime and for the
enrolled origin only, a deliberately tiny IPC surface: the runner status events,
the bridge facts, "open the connection panel", the browser sign-in handoff, the
opener plugin's URL command (so `target=_blank` links reach the OS browser),
keeping the web session (below), and the local side of the chat below.
Enrolling, unenrolling, settings and
quitting stay reachable from the local panel alone - web content can never do
them (`instance_bridge_capability` in `src/gui.rs`).

**The local side of the chat.** Every `computer_use` command renders as
a card in the transcript (the terminal window of a shell command, the file,
the folder, the capture - `runner_command_started` / `runner_command_finished`
progress frames, persisted with the message as `runner_activity`). Inside the
app, the paths of a card whose command ran on this machine, and absolute local
paths quoted in a reply, get `Open` / `Show in folder` through two commands of
the shell's own - never the opener plugin's unscoped `open_path` /
`reveal_item_in_dir`, which act on any path a page names:

- `desktop_open_local_path` / `desktop_reveal_local_path` accept an absolute
  LOCAL path only (`~` expanded, a host-less `file:` URL read): UNC shares,
  `\\?\` / `\\.\` device namespaces, NTFS alternate streams, reserved device
  names, `.` / `..` segments and the `/net` / `/Network` automount roots (any
  case) are refused before the file system is touched - a network path would
  make Windows authenticate to whatever server it names;
- the item must exist, and every decision is taken on the canonical target
  (links resolved), which must be local too: a link on a local disk that
  leads onto a share or an automount root is refused, a drive letter you
  mapped to a share yourself is not;
- `Open` hands only passive content to its default application - the
  `OPENABLE_EXTENSIONS` allow-list (`txt md markdown log csv tsv json yaml yml
  toml`, `pdf docx xlsx pptx`, `png jpg jpeg gif webp bmp tif tiff heic`, `mp3
  wav m4a flac ogg mp4 mov webm mkv`), checked on both the name asked for and
  the canonical target; never an executable, script, shortcut, installer, disk
  image, archive, web page, SVG, XML or document able to carry macros (the
  Office `m` variants, OpenDocument, RTF); a folder opens in the file manager
  unless it is a bundle (its name carries an extension - a macOS `.app`, a
  Windows `{CLSID}` junction - or it holds `Contents/Info.plist`). Anything
  `Open` will not hand over is shown in its folder instead, in the same call:
  `Show in folder` selects the item in the file manager and never executes
  anything;
- both answer only the chat window, while it is focused, at most once every
  400 ms; refusals come back as stable codes (`not_found`, `remote_path`,
  `invalid_path`, ...) the page words itself.

The validation lives in `src/local_paths.rs`. `desktop_request_attention`
flashes the taskbar entry (bounces the Dock icon) when a turn ends while the
window is in the background, and does nothing while it has the focus. The page
feature-detects all of it through `features`: a web app newer than the
installed shell keeps today's UI on an older one.
The grant is only ever reachable by the enrolled origin because the window is
pinned to it: `navigation_decision` admits that exact origin (scheme, host and
port) and `about:`, sends any other http(s) URL to the OS browser, and refuses
every other scheme - so neither an identity provider nor a previously enrolled
instance ever renders inside the webview that holds the capability.

### OS permissions

- **macOS** asks for **Screen Recording** and **Accessibility** permission the
  first time an agent captures the screen or synthesizes input. Grant them in
  `System Settings > Privacy & Security`; the panel links you straight there and
  shows whether each is granted.
- **Linux** desktop capture and input synthesis need a graphical session. On
  **Wayland**, screen capture goes through the desktop portal; the panel detects
  X11 vs Wayland and warns when the session cannot be captured. Input synthesis
  speaks the X11 XTEST protocol from pure Rust (`enigo`'s `x11rb` backend), so
  there is nothing extra to install: the default `xdo` backend would need
  `libxdo.so.3`, which lives in Ubuntu *universe* and is absent from a stock
  desktop — the loader then kills the runner before `main()` with *"error while
  loading shared libraries: libxdo.so.3"*. A runner installed before this change
  starts again after `sudo apt install libxdo3`, and self-updates from there.
  That removes a packaging problem, not a protocol one: XTEST reaches X11 and
  XWayland clients, so under a **native Wayland** session a Wayland-native
  window still receives no synthetic input, whichever backend is compiled in.

### Uninstall

- **Windows:** `Settings > Apps > Installed apps > XTM One Desktop > Uninstall`
  (or the Start-menu uninstaller shortcut).
- **macOS:** quit the app, then drag `XTM One Desktop.app` to the Trash.
- **Linux:** remove the package (`apt remove` / `dnf remove`) or delete the
  `.AppImage`.

Uninstalling removes the app. To also invalidate the credential server-side,
revoke the machine on the **XTM One Desktop** page (or use **Settings >
Unenroll** in the app first).

### Upgrading from "XTM One Runner"

The product name changed; the technical identity did not, so an installed
runner self-updates into XTM One Desktop like any other release:

- **Windows:** the NSIS installer keys an install by its product name, so the
  update installs into `%LOCALAPPDATA%\XTM One Desktop`, next to the old folder,
  and then retires the `XTM One Runner` install (`windows/installer-hooks.nsh`):
  its Start-menu and desktop shortcuts and its taskbar pin are rewritten in
  place to start the new build (they keep the old name until you reinstall), its
  autostart entry follows, and its program files and its Installed apps entry
  are removed. Its uninstaller is never run: it would wipe the enrollment, the
  config and the sign-in the two installs share. A machine updated before this
  fix may still list `XTM One Runner` in Installed apps - do not uninstall it
  (that unenrolls the machine and signs it out); the next update removes it.
  `windows/test-legacy-upgrade.ps1` (runner CI) runs the installer over such an
  install.
- **macOS:** the updater swaps the bundle contents in place, so a Mac upgraded
  this way keeps its old `XTM One Runner.app` folder name until reinstalled.
- **Linux:** the AppImage updates in place. `.deb` / `.rpm` installs follow the
  package name (`xtm-one-desktop`); the package declares `conflicts` /
  `replaces` (`obsoletes` on rpm) against `xtm-one-runner`, so installing it
  removes the old package for you.
- **Launch at login** is a stored preference (`launch_at_login` in
  `config.json`) re-applied on every start, so the entry always points at the
  executable the current build runs from (an upgrade moves it on Windows). The
  OS entry itself stays registered under the technical name `XTM One Runner`,
  like the crate and the bundle id: an upgraded install has no stored
  preference yet, so its first start reads the choice off that same entry (on
  or off, as you left it) and stores it - off stays off, and the app is never
  started twice at login.

## Self-update

The app keeps itself current with **signed** update bundles embedded in your
own instance's release and served by its update manifest, so a runner always
converges on the build that shipped with its server. Automatic updates are on
by default (a fleet of thousands stays current with zero operator touch) and
can be turned off in Settings, in which case the app checks nothing on its own:
it looks for a newer build only when you click **Check for updates** (connection
panel or app menu), and installs it if there is one. Signature verification is
always enforced; the setting only governs whether the app checks and installs
by itself.

Each Linux install kind is served its own format (the updater manifest's
`linux-<arch>-deb` / `-rpm` entries point at the packages, `linux-<arch>` at
the AppImage), because the updater refuses any other. A `.deb` / `.rpm` update
runs `dpkg -i` / `rpm -U` through `pkexec` (an administrator password prompt),
so on those installs the automatic path only announces the new version - the
connection panel opens and its update button installs it on a click - rather
than raising a password prompt every six hours. The AppImage, Windows and
macOS keep updating unattended; recommend the AppImage for an unattended Linux
desktop (the headless flavor for a server). An update is only announced, or
installed on a click, when the manifest serves the install's own package. An
instance that publishes none (built before this change, or missing that
package's signature) leaves the updater on the AppImage entry, which the
package updater refuses; the app logs that, and the update button says so
before any download. An existing package install is moved to this behaviour
by its previous build, which raises the password prompt on its own at each
check until the update is accepted once.

An update restarts the app, and the windows that were open come back: the
chat window on the page it showed (only if that page is on the enrolled
instance), maximized if it was, and the connection panel on its overview,
which shows the new version. Before installing, the app writes what is on
screen to `open-windows.json` in its config directory, and the next launch
consumes it once. A record older than ten minutes is ignored, which covers an
installer that failed after the app quit. The record is needed because the
restart reuses the launch arguments of the process it replaces: an app
started at login keeps `--autostarted` and would otherwise stay in the tray,
and an app launched by hand would pop its chat window up although it was
closed. The update that installs this behaviour is still applied by the
previous build, so that one restart happens the old way.

The platform can also refuse a runner whose version is below
`runner_min_supported_version` and tell it to update. The desktop app
installs nothing on that signal: the tray and the connection panel read
**Update required** until it is updated - by the panel's update button, or,
where it updates unattended (automatic updates on, and not a `.deb` / `.rpm`
install), by its next automatic check. A headless runner updates itself.

An update that asks for a password - a `.deb` / `.rpm` install through
`pkexec`, or the administrator prompt of a macOS app in a folder you cannot
write to - waits for it on a thread of its own, so the machine stays online
and keeps running commands while the prompt is open (on macOS the app's own
windows wait for that prompt, which the updater shows from the main thread;
the connection panel says so before the prompt opens). Quitting while an
update installs waits for it, a minute at most: the panel says why, and the
app quits once the prompt is answered or cancelled, so no prompt is left open
behind it - unless nobody answers it within that minute, when the app quits
anyway and the prompt stays open. A macOS app that needs an administrator in
a folder whose path holds a quote or a backslash is refused before its
download, with the reason in the panel: the updater cannot ask for one safely
there.

## Real-browser control

The `browser_control` capability drives a real browser over the Chrome DevTools
Protocol, so agents act inside a session **you** already logged into. Start (or
already have running) a browser with a CDP endpoint, e.g.:

```bash
chrome --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\xtm-runner-profile"
```

Log in to your portals **once** in that profile, then set the CDP URL in the
app's Settings (defaults to `http://127.0.0.1:9222`). The runner attaches to the
live, authenticated session instead of spawning a fresh, logged-out one. Use a
profile of its own (`--user-data-dir`): recent Chrome and Edge ignore the port
on the default profile, and a launch while the browser already runs hands off
to that instance and exits.

`capabilities/browser.rs` drives one tab, kept across commands:

- It attaches to the browser's most recently active tab (the `/json/list`
  order), never through chromiumoxide's `fetch_targets`, which re-creates and
  re-attaches every target and leaves a tab with several sessions (navigation
  then never completes).
- `list_tabs` / `select_tab` (by `tab_id`, or the active tab) choose it; a
  screenshot brings it to the front first.
- The CDP handler skips a message chromiumoxide cannot parse (a newer
  browser's event, which a sign-in or a new tab sends) instead of ending, which
  used to fail every later command with `send failed because receiver is gone`.
  A connection that is gone anyway is rebuilt on the next command and
  re-attached to the same tab; the command is run again when it never reached
  the browser, or when running it twice changes nothing (navigate, screenshot,
  get_content, select_tab).
- When the endpoint cannot be reached, the error names a browser whose
  `RemoteDebuggingAllowed` policy is off (Windows registry, Linux managed
  policy files).
- `evaluate` runs a JavaScript expression in the driven tab (`expression`,
  `timeout` in seconds, 30 by default, 120 at most; promises awaited) and
  answers the result as text, cut at 20,000 characters, as the platform's
  headless browser tool does. It is not run again after a connection drop.
- Portals that render their panes inside iframes (the Entra admin center
  blades): `click`, `type` and `get_content` take an optional `frame`, a CSS
  selector of an iframe of the top document. Without one, a `click` or `type`
  whose selector the top document does not match walks its same-origin
  iframes, breadth first, and acts at the element's centre. A cross-origin
  iframe cannot be reached from the top document: the error names those it
  skipped.
- DevTools does not go through the OS input desktop, so `browser_control`
  keeps working on a locked Windows session, where `computer_action` answers
  `input blocked: session locked` (the lock screen or a UAC prompt holds the
  input desktop and Windows drops synthetic input there).

An end-to-end test runs against a real browser:
`XTM_RUNNER_E2E_CDP_URL=http://127.0.0.1:9222 cargo test --bins a_tab_opened_by_hand -- --ignored`;
the iframe and `evaluate` path:
`XTM_RUNNER_E2E_CDP_URL=http://127.0.0.1:9222 cargo test --bins a_button_inside_a_same_origin_iframe -- --ignored`.

## Credentials & storage

- The durable credential (`run-...`) is stored in the **OS keychain** (Windows
  Credential Manager / macOS Keychain / Linux Secret Service via `keyring`),
  never in a config file or a log line.
- XTM One Desktop keeps the chat window's web session in the same keychain
  service, under the account `web-session-<runner id>` (see "The session
  survives restarts" above). Without a usable keychain both fall back to an
  owner-only file in the config directory.
- Non-secret config (server URL, runner id, name, platform, intervals, CDP URL,
  auto-update flag) lives as JSON in the OS config directory.
- The enrollment key (`renr-...`) is exchanged for the durable credential during
  enrollment and is not persisted.

## How it talks to the platform

1. **WebSocket (fast path):** an outbound, authenticated WebSocket. The platform
   pushes commands the instant an agent issues them; the runner ACKs, executes,
   and uploads results. Heartbeats keep the connection warm and drive the
   online/offline signal.
2. **HTTP long-poll (fallback):** if the WebSocket cannot be established, the
   runner long-polls a blocking endpoint that returns as soon as a command is
   queued - still effectively real-time.
3. **Result upload:** large binary results (screenshots, file reads) are uploaded
   over HTTP multipart so they never congest the control channel. The result's
   JSON (`meta`) may be up to 16 MiB (`MAX_RESULT_META_BYTES`, the platform's
   `RUNNER_MAX_RESULT_META_BYTES`), which every capability's own output caps
   fit; a larger one is not sent, and the command is reported as failed with
   its size.

Reconnects use exponential backoff; a fatal auth error (revoked / again-invalid
credential) stops the runner instead of hammering the server.

A platform restart or upgrade is ridden out, not reported as an error. No
answer, a timeout, a `429`, or a `5xx` (the platform answers `503` with
`Retry-After` while it starts or drains; a proxy answers `502` / `504`) is
retried with jittered back-off that honours `Retry-After`: a long-poll until its
stint ends, a heartbeat within its interval, and a result upload for up to 50
seconds, inside the minute the platform's lease keeps for it - so a command that
finishes during a rollout still reports its result, and the agent's wait for it
covers those 50 seconds too. A heartbeat or a result
upload that is refused (`401`, `403`, `404`, any other `4xx`) is not retried;
the platform records a result it refuses as the command's failure, with the
reason, so the agent is told at once instead of waiting for it.
A long-poll refused with `401` stops the runner (its credential was revoked);
any other refused poll is logged as a warning and polled again after the same
back-off. While its polls fail, the fallback shows the machine as
reconnecting.

## Security posture

- **Pull-only.** No inbound listener on the machine.
- **Permission modes.** `restricted` (default: per-capability allow-list +
  optional shell-command / path scoping + optional human-in-the-loop approval)
  or `full_control` (deliberate opt-in - every capability, no per-op approval,
  still fully audited).
- **Capabilities.** `screenshot`, `computer_action` (mouse/keyboard),
  `run_shell`, `file_ops`, `browser_control` - granted individually per runner.
- **Shell commands end with their tree.** `run_shell` starts the shell as the
  root of its own process tree (a new process group on macOS / Linux; on
  Windows `taskkill /T /F` on the shell's pid). A command past its timeout is
  ended with every process it started, and is reported as `timeout` with
  `exit_code: null`, `stopped: "timeout"` and the stdout / stderr it printed
  until then, which the agent reads.
- **Stop from the chat card.** A running `run_shell` card has a Stop button:
  `POST /api/v1/runners/commands/{id}/stop` (the command's owner, or whoever
  manages the runner) publishes `stop:<command id>` on the runner's
  `runner:control:{runner id}` channel; the replica holding the socket sends
  the runner a `{"type": "stop", "command_id": ...}` frame (any other control
  signal is a revoke and closes the socket). The runner ends that command's
  tree like a timeout and reports `failed` with `stopped: "user"` and the
  partial output; the card shows "Stopped by you" and the agent is told the
  user stopped it, and when. A stop for a command that already exited is a
  no-op (`stopping: false`).
- **Background jobs.** `run_shell` with `background: true` starts the command
  as its own process tree like any other, its stdout and stderr written to a
  log file under the temp directory (`xtm-one-runner-jobs/<job id>.log`), and
  answers within about a second with `job_id` (the command id), `pid`,
  `status: running` and the first 4 KB printed; the command keeps running
  after the chat turn. The `shell_job` command kind (tool `shell_jobs`, gated
  by the `run_shell` capability) answers `list` (every job: command, pid,
  status `running` / `exited` / `stopped`, exit code, output size), `tail` (a
  window of the log from `offset`, or its end, with `next_offset` and `more`;
  64 KB at most per read) and `stop` (ends the tree, answers the tail). The
  card's Stop reaches a running job by its command id through the same `stop`
  frame, and stays on the card after the turn while the job runs. At most 8
  jobs run at once per machine (the 9th is refused with what to do); 32 are
  remembered, the oldest ended one forgotten with its log. Jobs end on exit,
  on stop, or with the runner. A runner older than the first build
  carrying them (`SHELL_JOBS_MIN_RUNNER_VERSION` in
  `backend/app/services/runners.py`, 1.261103.10467) would run a background
  command in the foreground and reject `shell_job`: the server refuses both
  before dispatch, naming the runner's version, the version needed and how to
  update. A runner reporting no parseable version is not refused.
- **Background jobs on the Machines page.** `GET /api/v1/runners/{id}/jobs`
  (whoever manages the runner) sends the machine a `shell_job` `list` and
  answers `{available, reason, jobs}`: the runner card lists the jobs, running
  ones first, each with a confirmed Stop that calls
  `POST /api/v1/runners/commands/{job_id}/stop`. A machine that cannot answer
  is `available: false` with `reason` `offline`, `not_allowed` (no shell
  capability), `busy` or `no_answer` (nothing within 10 s; the list command is
  given up), never an error.
- **Auditable.** Every command is recorded server-side with its result status.
- **Local paths from the chat.** The chat window can open or show a path on
  this machine only through `desktop_open_local_path` /
  `desktop_reveal_local_path` (local, existing, canonical, passive content
  only for Open, focused window, rate-limited - see "The local side of the
  chat" above); the opener plugin's path commands are never granted to it.
- **Revocable.** Revoking the runner on the XTM One Desktop page invalidates the
  credential immediately; the next call fails and the runner stops.
- **Certificate trust.** HTTP calls trust the Mozilla roots built into the
  runner; the WebSocket also trusts the operating system's certificate store.
  Keep the machine's CA certificates up to date: the runner cannot drop a root
  the OS still trusts.

---

## Build from source (developers)

The app is a [Tauri v2](https://v2.tauri.app) project (Rust core + a static
webview panel). You need a recent stable Rust toolchain, plus Tauri's per-OS
system dependencies (WebView2 on Windows, `webkit2gtk` + GTK + appindicator on
Linux; nothing extra on macOS).

```bash
# Compile XTM One Desktop (default features: chat window + tray + panel + browser control)
cargo build --release

# Compile the headless runner only (no window, no tray) - for servers / MDM;
# this is the flavor CI checks and the install scripts ship
cargo build --release --no-default-features --features browser,self-update
```

To produce the native installers/bundles locally, use the Tauri CLI at the
version the workflows pin (`release.yml` in XTM-One-Platform/release,
`runner-ci.yml`): the CLI and the Tauri crates in `Cargo.lock` move together.

```bash
npm install -g @tauri-apps/cli@2.12.0
tauri build                  # NSIS / dmg+app / deb+rpm+AppImage for the host OS
```

On Windows the Visual C++ runtime is linked into the binary (`build.rs`), so it
starts on a machine without the Visual C++ Redistributable. CI refuses a
Windows binary that imports it; check a local build the same way:

```powershell
./windows/assert-static-vcruntime.ps1 -Binary target/release/xtm-one-runner.exe
```

There is no separate runner release: the release pipeline
(`release.yml` in XTM-One-Platform/release, `runner-build` job, with
`scripts/release-compile.sh` and `scripts/release-stage.sh`) builds and signs
the installers, the Tauri updater artifacts, and the bare-binary archives for
all three OSes on every platform release, and embeds them into the platform
image.
Each instance serves its own version-matched build from
`/api/v1/runners/dist/...` (the XTM One Desktop page renders the download buttons).
PRs are gated by `.github/workflows/runner-ci.yml` (fmt + clippy + build, GUI
and headless).

Flavors: the release ships two builds of the same source. The **desktop**
flavor (default features) is the product — installers, tray app, screenshots,
synthetic input. The **headless** flavor
(`--no-default-features --features browser,self-update`) drops the `desktop`
feature, and with it `xcap` and `enigo`: no libxcb, no libxkbcommon, nothing
X11- or Wayland-shaped to resolve at load time. That is the whole point — the
desktop build dies in the dynamic loader on a server, before `main()`, so no
runtime flag could ever have made it headless. It is built on the Linux
legs only, into its own `--target-dir` so it cannot clobber the bundled GUI
binary, and staged as `xtm-one-runner-linux-<arch>-headless.tar.gz` (bare
archive only — a server has no use for an AppImage or a tray icon).

A headless runner reports `flavor=headless` on every update check, and the
platform answers with headless artifacts or nothing at all: updating a server
into the desktop build would leave it unable to start. Commands it cannot serve
(`screenshot`, `computer_action`) fail with a message naming the flavor rather
than a black frame.

Architectures: macOS ships **one universal bundle** (Intel + Apple Silicon),
while Windows and Linux ship **one native build per architecture** — `x64` and
`arm64`, each built natively on a GitHub-hosted ARM runner (`windows-11-arm` /
`ubuntu-22.04-arm`) rather than cross-compiled. The artifact names carry the
architecture (`xtm-one-runner-linux-arm64.AppImage`,
`xtm-one-runner-windows-arm64.zip`, …) and are matched byte-for-byte by the
tables in `backend/app/api/v1/runners.py` — renaming one there or in the
workflow 404s the downloads. The install scripts and the daemon self-updater
pick the architecture themselves (`detect_arch()` in `src/enroll.rs`), so an
ARM machine never lands on an x86_64 build.

The Linux builds run on `ubuntu-22.04` / `ubuntu-22.04-arm` on purpose, while
the rest of CI runs on `ubuntu-26.04`: a Linux binary needs at least the glibc
of the machine that built it, so the 22.04 image (glibc 2.35) is the oldest
distribution the AppImage, `.deb`, `.rpm` and headless build start on. Moving
these legs to a newer image drops every user below it.

### Release signing (the update trust root)

Self-update is only offered for **signed** artifacts. The trust root is a
minisign keypair:

- The **public keys** the release trusts live in `release-signing.json`
  (committed): `keys`, one to four minisign key lines, and `timestamp_floor`.
  CI bakes them into the bare binary (`XTM_RUNNER_MINISIGN_PUBKEY`, the keys
  comma-separated, and `XTM_RUNNER_SIGNATURE_FLOOR`), and the image build
  writes them into the install scripts it serves, so the headless self-updater,
  `verify-signature` and the scripts trust the same list. The key id a
  signature names picks the key that checks it.
- The **GUI bundle updater** (the Tauri updater plugin) takes exactly one key,
  `plugins.updater.pubkey` in `tauri.conf.json`; it must be one of the release
  keys (`backend/tests/test_runner_install_scripts.py` fails otherwise).
- The **private key** exists only in the secrets
  `RUNNER_TAURI_SIGNING_PRIVATE_KEY` and
  `RUNNER_TAURI_SIGNING_PRIVATE_KEY_PASSWORD` of the `release` environment of
  XTM-One-Platform/release (keep the originals in a password manager; this
  repository never holds them).
- The two signature files are **not interchangeable**. `.sig` (next to the
  GUI updater artifacts) is what `tauri signer sign` writes: base64 of the
  whole minisign signature text, which the Tauri bundle updater decodes
  itself. `.minisig` (next to the bare archives) is minisign's own four-line
  text, which `update.rs` hands to `minisign_verify` verbatim — the stage
  step decodes the signer's output into it and refuses to stage anything
  else. Serving the base64 form under the `.minisig` name is what kept every
  headless / service runner on its first-installed build; the runner
  has accepted both forms since, but only the served bytes reach a fleet
  that predates that.

Generate a keypair once with the Tauri CLI, then wire both halves:

```bash
tauri signer generate -w xtm-one-runner.key
# xtm-one-runner.key      -> private key: password manager + the two GitHub secrets
# xtm-one-runner.key.pub  -> public key: paste into tauri.conf.json plugins.updater.pubkey
```

When the private-key secret is absent, CI still builds working installers but
strips the updater artifacts and stages no `.minisig`; the platform then
advertises `update_available: false` because runners refuse unsigned code by
design. A missing key therefore shows up as "You're on the latest version" on
every runner, forever.

**What a release signature says.** The bare archives are signed by
`scripts/sign-release-archive.mjs` (the release key, read the way `tauri signer`
reads it) because `tauri signer sign` cannot set the trusted comment: it reads
`timestamp:<t>`, `file:<archive>` and `version:<runner version>`. Every
verifier - the self-updater, `verify-signature`, both install scripts - requires
the `file:` field to name the archive this machine takes, the `version:` field
to be the version the platform offered (the self-updater also never installs a
build that is not newer than itself), and the `timestamp:` not to be older than
`timestamp_floor`. A signature that names no version is not held to one, so
archives signed before release signatures named their version keep verifying;
one that names no file is refused, since nothing ties it to its archive, and one
that names no time is refused while a floor is set. Each release leg checks the
signatures it stages with the runner it just built.

**Rotating the key is staged.** Add the new key to `release-signing.json`
and ship a release (still signed with the old key): its runners and scripts
trust both. Then switch the signing secrets, and retire the old key from the
list in a later release. The bundle updater trusts one key only: switch
`tauri.conf.json` to the new key in the release that is still signed with the
old one (the builds that install it check it with their own, old key), so the
next release, signed with the new key, finds GUI installs that trust it. A
runner whose platform skipped the release that trusts both keys never trusts
the new one: it refuses the releases signed with it until it is reinstalled.

**The signature floor.** `timestamp_floor` (Unix seconds, `0` = none) refuses
every release signature made before it - the way to retire an older build that
is still validly signed, and, once every supported release line is signed with
a version, the archives that name none. It applies to the release keys only: a
key given by hand (`--pubkey`, `XTM_RUNNER_MINISIGN_PUBKEY`) has no floor.

### Code signing (SmartScreen / Gatekeeper)

Two independent signature layers protect the runner:

| Layer | Signs what | Verified by | CI secrets |
| --- | --- | --- | --- |
| Self-update (minisign) | updater artifacts + bare archives | the public key compiled into every runner | `RUNNER_TAURI_SIGNING_PRIVATE_KEY` (+ `_PASSWORD`) — required |
| OS-native (Authenticode / Apple) | the installer + binaries a human downloads | Windows SmartScreen / macOS Gatekeeper | optional, below |

The minisign layer authenticates every **self-update** and is always enforced.
The OS-native layer is what the operating system evaluates on the **first
manual install**. Without it, Windows shows *"Windows protected your PC —
Microsoft Defender SmartScreen prevented an unrecognized app"* on the
downloaded `setup.exe` (browser downloads carry the Mark-of-the-Web), and
macOS shows *"Apple could not verify ... is free of malware"*. Admin rights
change nothing — SmartScreen gates on publisher reputation, not privileges.
Per-machine unblock for unsigned builds: click **More info → Run anyway** in
the SmartScreen dialog, or clear the Mark-of-the-Web first (file
**Properties → Unblock**, or `Unblock-File
xtm-one-runner-windows-x64-setup.exe`); on macOS click **Done** (not Move to
Trash), then **System Settings → Privacy & Security → Open Anyway**, and
launch again — since macOS Sequoia the old right-click → Open bypass no
longer exists. (`xattr -d com.apple.quarantine "/Applications/XTM One
Runner.app"` achieves the same from a terminal, and the headless install
script never triggers Gatekeeper at all — quarantine only taints browser
downloads.)

#### Windows (Authenticode)

Provision two secrets in the `release` environment of XTM-One-Platform/release
(never in this repository) and its release workflow signs the runner
binary, the NSIS installer and the uninstaller during the Tauri build
(sha256 digest, RFC 3161 timestamp over HTTPS from Sectigo's TSA —
DigiCert's endpoint is HTTP-only):

- `RUNNER_WINDOWS_SIGNING_CERT` — base64 of a PFX holding a code-signing
  certificate and its private key
- `RUNNER_WINDOWS_SIGNING_CERT_PASSWORD` — the PFX password (required)

Any PFX works, including a **self-signed** certificate:

```powershell
$cert = New-SelfSignedCertificate -Type CodeSigningCert `
  -Subject "CN=Filigran XTM One Runner" `
  -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddYears(5)
$pfxPassword = ConvertTo-SecureString "<strong password>" -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath runner-signing.pfx -Password $pfxPassword
Export-Certificate -Cert $cert -FilePath runner-signing.cer   # public half, for fleet trust
[Convert]::ToBase64String([IO.File]::ReadAllBytes("runner-signing.pfx")) |
  Set-Clipboard   # → RUNNER_WINDOWS_SIGNING_CERT secret value
```

Be clear-eyed about what each certificate type buys:

- **Self-signed** — on machines where you distribute `runner-signing.cer`
  (Intune / GPO → *Trusted Root Certification Authorities* **and** *Trusted
  Publishers*), the binary has a verified publisher, WDAC / AppLocker
  publisher rules work, and tampering is detectable. SmartScreen's
  "unrecognized app" screen is **reputation-based** (a Microsoft cloud
  service): a chain that does not end at a Microsoft-trusted CA can never
  accrue reputation, so public browser downloads keep the warning even where
  the certificate is locally trusted. Fleet installs pushed by Intune / SCCM
  skip the dialog entirely — MotW only taints browser downloads.
- **CA-issued** — the warning disappears for everyone once reputation
  accrues. [Azure Trusted Signing](https://learn.microsoft.com/azure/trusted-signing/)
  (Microsoft-validated identity, near-immediate reputation, ~$10/month) is
  the pragmatic choice; classic OV certificates build reputation over weeks,
  EV almost immediately. Cloud-HSM signing services (Trusted Signing,
  DigiCert KeyLocker, SSL.com eSigner) plug into the same job via a custom
  Tauri `signCommand` instead of the PFX secrets.

#### macOS (Developer ID + notarization)

Provision the standard Tauri signing secrets, in the same environment, and the
macOS leg signs (and, with the last three, notarizes) the bundle:

- `RUNNER_APPLE_CERTIFICATE` — base64 of the *Developer ID Application* `.p12`
- `RUNNER_APPLE_CERTIFICATE_PASSWORD` — its password
- `RUNNER_APPLE_SIGNING_IDENTITY` — e.g. `Developer ID Application: Filigran (TEAMID)`
  (required whenever the certificate is set)
- `RUNNER_APPLE_ID` / `RUNNER_APPLE_PASSWORD` (app-specific password) /
  `RUNNER_APPLE_TEAM_ID` — optional, enables notarization (required for a
  warning-free install on current macOS); all three must be provided together

Partial configuration (certificate without identity, or an incomplete
notarization trio) fails the build up front with the missing secret named.

### Feature flags

| Feature       | Default | What it adds                                          |
| ------------- | ------- | ----------------------------------------------------- |
| `gui`         | on      | XTM One Desktop: chat window, tray, connection panel, global shortcut, native installers + bundle updater |
| `browser`     | on      | Real-browser control via CDP (`chromiumoxide`)        |
| `self-update` | on      | Signature-verified self-update for headless installs  |

## Headless / service mode (advanced)

The same binary is a headless CLI when given a subcommand, kept for unattended
service / MDM installs where there is no desktop session for the GUI. With no
subcommand it opens the GUI; with one it runs headless.

The fastest path is the install script every instance serves (also shown with
copy-paste commands on the XTM One Desktop page under "Servers and headless machines"). One line
downloads the version-matched archive, verifies its SHA-256, installs the
binary, enrolls, and sets up autostart (LaunchAgent / systemd --user / logon
task):

```bash
# macOS / Linux
curl -fsSL https://<instance>/api/v1/runners/dist/xtm-one-runner-installer.sh \
  | bash -s -- --server https://<instance> --key renr-XXXXXXXX
```

```powershell
# Windows (PowerShell)
iwr https://<instance>/api/v1/runners/dist/xtm-one-runner-installer.ps1 -OutFile xtm-one-runner-installer.ps1
powershell -ExecutionPolicy Bypass -File .\xtm-one-runner-installer.ps1 -Server https://<instance> -Key renr-XXXXXXXX
```

Both scripts take `--no-autostart` / `-NoAutostart` to skip service setup, or
run without `--server` / `--key` to install only. The sources live in
`apps/runner/dist/` (`install.sh`, `install.ps1`).

When the SHA-256 cannot be verified - no `sha256sum` / `shasum` on the host, or
no (or an empty) `.sha256` next to the archive - the scripts install with a
loud `INTEGRITY NOT VERIFIED` warning naming the cause, with the HTTP status
when the `.sha256` could not be fetched. Pass `--require-checksum` /
`-RequireChecksum` (or set `XTM_RUNNER_REQUIRE_CHECKSUM` to `1`, `true` or
`yes`) to refuse instead; use it for MDM and other unattended rollouts:

```bash
curl -fsSL https://<instance>/api/v1/runners/dist/xtm-one-runner-installer.sh \
  | bash -s -- --require-checksum --server https://<instance> --key renr-XXXXXXXX
```

A mismatch, or a `.sha256` that holds no digest, is refused with or without the
flag. A mirror (`--download-base`) may serve the `.sha256` as `sha256sum`
writes it or as the bare digest, in any case, with LF or CRLF line endings -
the forms the self-updater reads. The `.sha256` comes from the same server as
the archive: it proves the download arrived intact, not who built it, and over
plain `http://` anyone on the network path can replace both, so the scripts
warn about a plain-HTTP download. When the platform cannot hand over the
archive, the scripts print the HTTP status it answered (a 5xx behind a proxy, a
403 from a WAF, a 404 for a build without embedded runners, runners disabled,
or a build the release does not ship) rather than guessing at the cause, and
they flag a sign-in page an auth proxy serves in the archive's place.

What proves who built it is the release signature. Both scripts carry
the release public keys and the signature floor of `release-signing.json`, what
the self-updater is compiled with;
`backend/tests/test_runner_install_scripts.py` fails when they drift, so a
key change edits `release-signing.json` and both scripts in the same commit -
and verify the archive's `.minisig` (either published form, like `update.rs`)
against the key whose id it names the way the self-updater does: the prehashed `ED` algorithm only
(the legacy `Ed` one is refused, the release never makes it), signed trusted
comment included. That comment also binds the signature to its archive:
`tauri signer sign` writes `timestamp:<t>` and `file:<name>` into it,
tab-separated, and the `file:` field must name the archive the script picked
for this machine, so a validly signed build of another platform or flavor
served in its place is refused. A comment that names no file (one given to
`minisign -t`) binds the signature to no archive: that is a signature that
cannot be checked, as below. When the archive comes from the platform, the
scripts read its manifest's `runner_version`, and a signature naming another
`version:` is refused: an older signed build served in place of the current one
(a stale cache, a tampered path) passes every other check, since the archive
names are the same in every release. A signature older than the floor is
refused too. On success the scripts print the key ID that verified it and the
version it names, or that it names none (signed before release signatures
carried one).

- `install.sh` uses `minisign` when it is on the host, otherwise OpenSSL 3
  (`openssl dgst -blake2b512` + `openssl pkeyutl -verify -rawin` for Ed25519).
  It refuses a key of small order, and an R of small order in either
  signature, itself, as libsodium does: OpenSSL and the runner's
  `minisign-verify` accept them, and a key of small order accepts forgeries.
  Signature and key lines, and the base64 that wraps the older forms, must be
  base64 every decoder reads the same way: its alphabet, whole groups of four,
  padding only at the end (OpenSSL 3.0 decodes past a stray character, 3.5
  drops an incomplete group). minisign and the runner are handed the lines as
  the script read them, so the verdict does not depend on which verifier the
  host has. On an upgrade of a host with neither (a stock Mac:
  LibreSSL, no minisign), the runner already installed checks it
  (`xtm-one-runner verify-signature`, the self-updater's own code); the script
  still runs all of the checks above, the key id and the archive the trusted
  comment names around it.
- `install.ps1` compiles a small verifier with `Add-Type` (BLAKE2b-512 per
  RFC 7693 and Ed25519 verification per RFC 8032 over `System.Numerics`:
  canonical encodings, `S < L`, the cofactorless equation libsodium and
  `minisign-verify` check, a key or an R of small order refused), because
  Windows PowerShell 5.1 has no Ed25519 and .NET no BLAKE2b. The type is
  named after a hash of its source, so a session that already loaded another
  revision of the verifier compiles this one instead of reusing the old one.
- Each verifier is trusted only after it accepts a known-good signature and
  rejects the same signature over other bytes (the `update.rs` test fixture),
  so a host whose tools cannot do the maths (OpenSSL 1.1, LibreSSL, Constrained
  Language Mode) reads as "cannot check", never as a bad signature.

A signature that does not verify, is signed by another key, uses the legacy
algorithm, was made for another archive, or whose trusted comment cannot be
read is refused whatever the flags. A signature that cannot be checked - no
`.minisig` (a release built without `RUNNER_TAURI_SIGNING_PRIVATE_KEY` ships
none), a body that is not a minisign signature at all (a mirror's error page,
an SPA catch-all, a fifth line, a line that is not base64) or is larger than
one can be, a trusted comment that names no file, or no working verifier - is
a loud `SIGNATURE NOT VERIFIED`
warning; `--require-signature` /
`-RequireSignature` (or `XTM_RUNNER_REQUIRE_SIGNATURE` set to `1`, `true` or
`yes`) refuses it. The default does not fail closed because unsigned builds are
a supported configuration; the cost is that withholding the `.minisig` also
turns a tampered download into a warning, which is why mirrors and unattended
rollouts should always pass the opt-in. `XTM_RUNNER_MINISIGN_PUBKEY` replaces the release
keys, and their floor, for a fork that signs its runners with its own key: the
key line, or the base64 of the whole `.pub` file as `tauri.conf.json` has it. A value that is
neither is refused before anything is downloaded, on a host with no verifier
too, and a valid one is announced with its key ID (as the `.pub` file prints
it), so a key slipped into the environment does not pass silently as the
release key. A `.minisig` larger than 64 KB counts as a missing signature, and
neither script parses more than that: `install.ps1` stops the download there,
`install.sh` has curl stop it (`--max-filesize`: up front when the server
announces the size, and mid-transfer from curl 8.4.0) and checks the size of
what it got.

The image build writes the keys and the floor of its own `release-signing.json`
into the scripts it serves (`dist/embed_release_key.py`, run by the
`Dockerfile`), so an instance built and signed with other keys hands out
scripts that trust them without `XTM_RUNNER_MINISIGN_PUBKEY`. It never fails a
build over the trust file itself (none usable: the scripts keep what they
carry), but a script that lost its key or floor line fails it.

Everything the scripts do can also be done by hand with the binary alone:

```bash
# 1. Enroll once with a single-use key from the XTM One Desktop page
xtm-one-runner enroll --server https://one.filigran.io --key renr-XXXXXXXX

# 2. Connect and serve commands
xtm-one-runner run

# Inspect config + test connectivity + check for updates
xtm-one-runner status

# Remove the stored credential + config (local unenroll)
xtm-one-runner reset

# Check an archive against its release signature, the self-updater's way
# (exit 0 verified, 1 does not verify or names another archive / version,
# 3 could not check)
xtm-one-runner verify-signature --signature xtm-one-runner-linux-x64.tar.gz.minisig \
  --expect-file xtm-one-runner-linux-x64.tar.gz --expect-version 1.260928.57 \
  xtm-one-runner-linux-x64.tar.gz
```

`enroll` also reads `--server` from `XTM_ONE_SERVER` and `--key` from
`XTM_ONE_ENROLL_KEY`. Increase log verbosity with `RUST_LOG`, e.g.
`RUST_LOG=xtm_one_runner=debug`.

## Uninstall (what is removed)

Uninstall means uninstall: no local trace survives, so the next install always
starts at the Connect form. The runner's local state is exactly four things —
`config.json` under the OS config dir (`%APPDATA%\Filigran\xtm-one-runner\` on
Windows, `~/Library/Application Support/one.Filigran.xtm-one-runner/` on macOS,
`~/.config/xtm-one-runner/` on Linux), the enrollment credential in the OS
keychain, the web session kept next to it (XTM One Desktop), and the autostart
entry. `xtm-one-runner reset` removes the credential and the web session
together.

- **Windows**: the NSIS uninstaller wipes all three on every real uninstall
  via `windows/installer-hooks.nsh` (it runs `xtm-one-runner reset` while the
  binary still exists — the Credential Manager entry can only be addressed
  through the app's own keyring entry — then sweeps the state directories,
  the autostart Run key, and the installer's registry crumbs). The stock
  "Delete the application data" checkbox only ever covered the webview
  profile; the hooks make it irrelevant. **Upgrades never wipe**: when a newer
  installer or the auto-updater replaces an install, it runs the old
  uninstaller in place (`uninstall.exe _?=<dir>`, plus `/UPDATE` from the
  auto-updater), while a real uninstall runs a copy of the uninstaller from
  `%TEMP%`. NSIS strips `_?=` from the command line the script sees, so the
  hooks tell the two apart by `/UPDATE` and by where the uninstaller runs,
  and preserve the enrollment on the upgrade path.
- **macOS / Linux (GUI)**: run `xtm-one-runner reset` before deleting the app
  bundle / package, or the keychain entry and config linger.
- **Headless**: `xtm-one-runner reset`, then remove the binary and the
  LaunchAgent / systemd unit / logon task the install script created.

The platform-side runner record is deliberately never touched (an uninstall
may run offline): revoke it on the XTM One Desktop page. A live runner whose record
was revoked shows a fatal error with a one-click "Re-enroll this machine"
button that drops the dead credential and reopens the Connect form.

## Layout

| File                          | Responsibility                                              |
| ----------------------------- | ----------------------------------------------------------- |
| `main.rs`                     | Dispatch: GUI (no subcommand) vs headless CLI, signals      |
| `gui.rs`                      | Tauri app: tray, menu, panel IPC, lifecycle, auto-update    |
| `ui/`                         | Static webview panel (onboarding / overview / settings)     |
| `daemon.rs`                   | Shared serve loop, callable from both GUI and CLI           |
| `status.rs`                   | Live status reporting from the daemon to tray + panel       |
| `enroll.rs`                   | One-time enrollment -> durable credential                   |
| `transport.rs`                | WebSocket + long-poll loop, reconnect, heartbeats           |
| `api.rs`                      | HTTP client (enroll, heartbeat, poll, result upload, update)|
| `executor.rs`                 | Dispatch a command to the right capability, enforce timeout |
| `protocol.rs`                 | Wire types shared with the backend                          |
| `config.rs` / `credential.rs` | Non-secret config / OS-keychain secret (and the kept web session) |
| `web_session.rs`              | The chat window's web session: shape and validation |
| `host_facts.rs`               | OS, shells, browsers, monitors, tz/locale reported on connect |
| `version.rs`                  | `MAJOR.YYMMDD.SEQ` version string (protocol gate)           |
| `update.rs`                   | Self-update check (headless path), `verify-signature`       |
| `capabilities/`               | `screenshot`, `input`, `shell`, `files`, `browser`, `permissions` |

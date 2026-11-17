/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

// XTM One Desktop - connection panel logic.
//
// Talks to the Rust/Tauri host over IPC (`window.__TAURI__.core.invoke`) and
// listens for pushed events:
//   - "status"            : live RunnerStatus from the daemon transport loop
//   - "navigate"          : a menu (tray or application) asked the panel to
//                           show a given section
//   - "check-for-updates" : the application menu's "Check for updates..."
//   - "update-available"  : a newer version to install on a click (.deb / .rpm)
//   - "quit-held"         : a quit waits for an update install (payload: the
//                           most seconds it waits)
//   - "update-needs-approval" : the macOS administrator dialog is about to open
//
// The chat itself lives in the separate chat window (the instance's web app);
// this panel only enrolls, shows the connection, and holds the settings.
//
// The host is the source of truth. This script only renders a snapshot and
// forwards user intent back through IPC commands.
//
// The visible section is a STRICT state machine derived from the enrollment
// state, never trusted from a caller: not enrolled -> the Connect form is the
// only screen; enrolled -> the Connect form is unreachable and the tabs
// (overview / settings / about) are the only screens. Anything that arrives
// before the first state snapshot is deferred, not guessed - acting on a
// `navigate` event with no snapshot used to flash the Connect form (with an
// enabled submit) over an enrolled, online runner.

const invoke = window.__TAURI__.core.invoke;
const listen = window.__TAURI__.event.listen;

const $ = (id) => document.getElementById(id);

const SECTIONS = ["onboarding", "overview", "settings", "about"];

let snapshot = null;
// The section currently shown, and a navigation that arrived before the
// first snapshot (applied as soon as the state is known).
let currentSection = null;
let pendingSection = null;

// ---- Rendering -----------------------------------------------------------

function showSection(name) {
  // No snapshot yet: defer instead of guessing the enrollment state.
  if (!snapshot) {
    pendingSection = name;
    return;
  }
  const enrolled = snapshot.enrolled;
  if (!enrolled) {
    // Until enrolled, the only meaningful screen is onboarding.
    name = "onboarding";
  } else if (name === "onboarding" || !SECTIONS.includes(name)) {
    // Enrolled: the Connect form is unreachable; stale requests land on the
    // overview.
    name = "overview";
  }
  currentSection = name;
  for (const s of SECTIONS) {
    const el = $(`section-${s}`);
    if (el) el.hidden = s !== name;
  }
  // One status indicator per screen: the overview's status card owns it
  // there; the header pill covers every other screen.
  $("status-pill").hidden = enrolled && name === "overview";
  // Tab bar is only meaningful once enrolled.
  $("tabbar").hidden = !enrolled;
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.dataset.section === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  }
}

// Status is only meaningful while enrolled: a daemon being torn down
// (unenroll) or spun up (enroll still in flight) must never leak an "online"
// indicator over the Connect form.
function effectiveStatus() {
  if (!snapshot || !snapshot.enrolled) return { state: "not_enrolled" };
  return snapshot.status || { state: "connecting" };
}

// Turn a raw snake_case state into a human-friendly label. Mirrors the Rust
// `RunnerStatus::label()` (tray tooltip) so the pill never shows "not_enrolled".
// Any unmapped/future state degrades to a prettified form ("some_state" ->
// "Some state") instead of the raw enum.
function friendlyStatusLabel(status) {
  if (!status) return "Starting...";
  switch (status.state) {
    case "not_enrolled":
      return "Not enrolled";
    case "connecting":
      return "Connecting...";
    case "online":
      return status.transport === "long_poll" ? "Online (fallback)" : "Online";
    case "reconnecting":
      return "Reconnecting...";
    case "update_required":
      return "Update required";
    case "stopped":
      return "Stopped";
    case "error":
      return "Error";
    default: {
      const raw = status.label || status.state || "";
      return raw
        ? raw.charAt(0).toUpperCase() + raw.slice(1).replace(/_/g, " ")
        : "Starting...";
    }
  }
}

function statusPill(status) {
  const pill = $("status-pill");
  const label = $("status-label");
  pill.className = "pill";
  const state = status ? status.state : "connecting";
  let cls = "pill-muted";
  let text = friendlyStatusLabel(status);
  switch (state) {
    case "online":
      cls = "pill-online";
      break;
    case "connecting":
    case "reconnecting":
    case "update_required":
      cls = "pill-warn";
      break;
    case "error":
      cls = "pill-error";
      break;
    case "not_enrolled":
    case "stopped":
      cls = "pill-muted";
      break;
  }
  pill.classList.add(cls);
  label.textContent = text;
}

function renderStatusCard(status) {
  const icon = $("status-icon");
  const headline = $("status-headline");
  const detail = $("status-detail");
  if (!status) return;
  icon.className = "status-icon";
  switch (status.state) {
    case "online":
      headline.textContent = "Online";
      detail.textContent =
        status.transport === "long_poll"
          ? "Connected (HTTP fallback)"
          : "Connected, serving commands";
      break;
    case "connecting":
      icon.classList.add("is-warn");
      headline.textContent = "Connecting";
      detail.textContent = "Establishing outbound connection...";
      break;
    case "reconnecting":
      icon.classList.add("is-warn");
      headline.textContent = "Reconnecting";
      detail.textContent = "Lost connection; retrying...";
      break;
    case "update_required":
      icon.classList.add("is-warn");
      // Nothing installs on this signal: the version is refused until the
      // person updates with the button below the card.
      headline.textContent = "Update required";
      detail.textContent =
        "Your instance no longer accepts this version. Update the app below to reconnect.";
      break;
    case "stopped":
      icon.classList.add("is-idle");
      headline.textContent = "Stopped";
      detail.textContent = "Not serving commands on this machine.";
      break;
    case "error":
      icon.classList.add("is-error");
      headline.textContent = "Error";
      detail.textContent = status.message || "Something went wrong.";
      break;
    default:
      icon.classList.add("is-idle");
      headline.textContent = friendlyStatusLabel(status);
      detail.textContent = "";
  }
  // A fatal error means the daemon has stopped for good (revoked credential,
  // runners disabled): surface the one-click way back to the Connect form
  // instead of dead-ending until someone finds Settings > Unenroll.
  $("reenroll-row").hidden = status.state !== "error";
}

function render() {
  if (!snapshot) return;
  const { enrolled, config, version } = snapshot;
  const status = effectiveStatus();

  statusPill(status);

  // Hard guard on top of the section state machine: the submit must never be
  // actionable for a runner that is already enrolled.
  $("enroll-submit").disabled = !!enrolled;

  if (enrolled) {
    // Overview details
    $("d-name").textContent = (config && config.name) || "This machine";
    $("d-server").textContent = (config && config.server_url) || "-";
    $("d-server").title = (config && config.server_url) || "";
    $("d-platform").textContent = (config && config.platform) || "-";
    $("d-version").textContent = version ? `v${version}` : "-";

    renderStatusCard(status);

    // Settings
    setSwitch($("toggle-autostart"), snapshot.autostart);
    setSwitch($("toggle-autoupdate"), config && config.auto_update);
    $("cdp-url").value = (config && config.cdp_url) || "";

    // About
    $("about-version").textContent = version ? `v${version}` : "";
    const inst = (config && config.server_url) || "";
    const link = $("about-instance");
    link.textContent = inst || "-";
    link.href = inst || "#";
  }

  // Apply any navigation that was deferred until the state was known, else
  // keep the current section; showSection derives what is actually allowed.
  showSection(pendingSection || currentSection || "overview");
  pendingSection = null;
}

function setSwitch(el, on) {
  if (el) el.setAttribute("aria-checked", on ? "true" : "false");
}

// ---- State sync ----------------------------------------------------------

async function refresh() {
  snapshot = await invoke("get_state");
  render();
}

// ---- Event wiring --------------------------------------------------------

function wireEvents() {
  // Tab navigation
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => showSection(tab.dataset.section));
  }

  // The enrollment key is a credential: masked by default, with an explicit
  // reveal to verify what was pasted. The value itself is kept around so an
  // unenroll / re-enroll can reuse it (keys can be multi-use) - only the
  // display is protected.
  function setEnrollKeyVisible(show) {
    $("enroll-key").type = show ? "text" : "password";
    const toggle = $("enroll-key-toggle");
    toggle.textContent = show ? "Hide" : "Show";
    toggle.setAttribute(
      "aria-label",
      show ? "Hide enrollment key" : "Show enrollment key"
    );
  }
  $("enroll-key-toggle").addEventListener("click", () => {
    setEnrollKeyVisible($("enroll-key").type === "password");
  });

  // Enrollment
  $("enroll-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("enroll-submit");
    const err = $("enroll-error");
    err.hidden = true;
    btn.disabled = true;
    btn.textContent = "Connecting...";
    try {
      snapshot = await invoke("enroll", {
        serverUrl: $("enroll-server").value.trim(),
        enrollKey: $("enroll-key").value.trim(),
        name: $("enroll-name").value.trim() || null,
      });
      // Re-mask so onboarding never re-opens (after a later unenroll) with
      // the key revealed.
      setEnrollKeyVisible(false);
      render();
      showSection("overview");
    } catch (e2) {
      err.textContent = String(e2);
      err.hidden = false;
    } finally {
      // Never re-arm the submit for an enrolled machine (success path); only
      // a failed attempt gets the button back.
      btn.disabled = !!(snapshot && snapshot.enrolled);
      btn.textContent = "Connect this machine";
    }
  });

  // Open the chat window (the instance's web app in its own native window).
  $("btn-open-chat").addEventListener("click", () =>
    invoke("open_chat").catch(() => {})
  );

  // Open the instance in the system browser instead.
  $("btn-open-instance").addEventListener("click", () =>
    invoke("open_instance").catch(() => {})
  );

  // Check for updates
  $("btn-check-update").addEventListener("click", async () => {
    const note = $("update-note");
    const btn = $("btn-check-update");
    btn.disabled = true;
    note.hidden = false;
    note.className = "update-note";
    note.textContent = "Checking...";
    // Past the check, a failure is the install's: a download cut short, or a
    // .deb / .rpm install whose password prompt was dismissed.
    let installing = false;
    try {
      const meta = await invoke("check_for_update");
      if (meta && meta.version) {
        note.classList.add("is-available");
        note.textContent = `Version ${meta.version} available - installing...`;
        installing = true;
        await invoke("install_update");
        note.textContent = "Update downloaded. Restarting...";
      } else {
        note.textContent = "You're on the latest version.";
        btn.textContent = "Check for updates";
      }
    } catch (e) {
      note.className = "update-note";
      note.textContent = installing
        ? `Update failed: ${e}`
        : `Update check failed: ${e}`;
    } finally {
      btn.disabled = false;
    }
  });

  // Settings toggles
  $("toggle-autostart").addEventListener("click", async () => {
    const next = $("toggle-autostart").getAttribute("aria-checked") !== "true";
    setSwitch($("toggle-autostart"), next);
    try {
      await invoke("set_autostart", { enabled: next });
    } catch (e) {
      setSwitch($("toggle-autostart"), !next);
    }
  });

  $("toggle-autoupdate").addEventListener("click", async () => {
    const next = $("toggle-autoupdate").getAttribute("aria-checked") !== "true";
    setSwitch($("toggle-autoupdate"), next);
    try {
      await invoke("set_auto_update", { enabled: next });
    } catch (e) {
      setSwitch($("toggle-autoupdate"), !next);
    }
  });

  $("btn-save-cdp").addEventListener("click", async () => {
    const btn = $("btn-save-cdp");
    btn.disabled = true;
    btn.textContent = "Saved";
    try {
      await invoke("set_cdp_url", { url: $("cdp-url").value.trim() });
    } catch (e) {
      /* ignore */
    }
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = "Save";
    }, 1200);
  });

  // Unenroll
  $("btn-unenroll").addEventListener("click", async () => {
    const btn = $("btn-unenroll");
    btn.disabled = true;
    try {
      snapshot = await invoke("unenroll");
      render();
      showSection("onboarding");
    } finally {
      btn.disabled = false;
    }
  });

  // Re-enroll from a fatal error: drop the dead local credential and land on
  // the Connect form with the instance URL prefilled, ready for a fresh key.
  $("btn-reenroll").addEventListener("click", async () => {
    const btn = $("btn-reenroll");
    btn.disabled = true;
    const server =
      (snapshot && snapshot.config && snapshot.config.server_url) || "";
    try {
      snapshot = await invoke("unenroll");
      if (server && !$("enroll-server").value) {
        $("enroll-server").value = server;
      }
      render();
      showSection("onboarding");
      $("enroll-key").focus();
    } finally {
      btn.disabled = false;
    }
  });
}

// ---- Pushed events -------------------------------------------------------

// A message about an update, under the overview's update button.
function showUpdateNote(text) {
  const note = $("update-note");
  note.hidden = false;
  note.className = "update-note is-available";
  note.textContent = text;
}

async function wireHostEvents() {
  await listen("status", (event) => {
    // Pre-snapshot events are dropped: the initial refresh() carries the
    // current status, and rendering now would guess the enrollment state.
    if (!snapshot) return;
    snapshot.status = event.payload;
    const status = effectiveStatus();
    statusPill(status);
    if (snapshot.enrolled) renderStatusCard(status);
  });

  // Quit waits for an update install, which may be waiting on a system
  // password prompt that would otherwise outlive the app - for at most the
  // number of seconds the host sends.
  await listen("quit-held", (event) => {
    showUpdateNote(
      `An update is being installed. Answer or cancel the system password prompt: the app quits once the install ends, or in ${event.payload} seconds at the latest.`
    );
  });

  // macOS asks for an administrator from the app's main thread: say so
  // before its windows stop responding.
  await listen("update-needs-approval", () => {
    showUpdateNote(
      "Approve the update in the macOS administrator dialog. The app's windows wait until you answer it."
    );
  });

  await listen("navigate", (event) => {
    const section = event.payload;
    if (SECTIONS.includes(section)) showSection(section);
  });

  // The host has already navigated to the overview; this runs the check of
  // its button, which stays disabled (click() is then a no-op) while one runs.
  await listen("check-for-updates", () => {
    if (snapshot && snapshot.enrolled) $("btn-check-update").click();
  });

  // A .deb / .rpm install is not updated unattended - its package manager asks
  // for an administrator password - so the host announces the new version here
  // and the update button installs it on the user's click.
  await listen("update-available", (event) => {
    const meta = event.payload;
    if (!meta || !meta.version || !(snapshot && snapshot.enrolled)) return;
    showUpdateNote(
      `Version ${meta.version} is available. Your system asks for your password to install it.`
    );
    $("btn-check-update").textContent = `Install version ${meta.version}`;
  });

  await listen("refresh", () => {
    refresh();
  });
}

// ---- Boot ----------------------------------------------------------------

window.addEventListener("DOMContentLoaded", async () => {
  wireEvents();
  await wireHostEvents();
  await refresh();
});

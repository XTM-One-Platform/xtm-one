/**
 * The Office add-in's chat panel routes (`apps/microsoft-office/src/lib/panelConfig.ts`)
 * are the chat page's (`src/components/chat-panel/endpoints.ts`): the pane has
 * Stop, "Continue now", approvals, steering and the event stream like every
 * panel host, under the platform's `/api/v1`.
 *
 * Run with `yarn test:unit` (Node's built-in runner).
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  OFFICE_PANEL_ENDPOINTS,
  OFFICE_PANEL_FILE_LIMITS,
  OFFICE_PANEL_MAX_FILES_PER_MESSAGE,
  OFFICE_PANEL_MAX_UPLOAD_BYTES,
  OFFICE_PANEL_STRINGS,
  OFFICE_PANEL_THEME,
  officePanelTranslate,
  panelApiBaseUrl,
} from "../../apps/microsoft-office/src/lib/panelConfig.ts";

/** `CHAT_PANEL_ENDPOINTS` as written: `key: "path"` / `key: CONSTANT` / `key: null`. */
function chatPageEndpoints(): Map<string, string> {
  const source = readFileSync(new URL("../src/components/chat-panel/endpoints.ts", import.meta.url), "utf8");
  const body = source.match(/export const CHAT_PANEL_ENDPOINTS: ApiEndpoints = \{([\s\S]*?)\n\};/);
  assert.ok(body, "endpoints.ts declares CHAT_PANEL_ENDPOINTS");
  const constants = new Map(
    [...source.matchAll(/^const (\w+) = "([^"]*)";/gm)].map(([, name, value]) => [name, value] as const),
  );
  const entries = new Map<string, string>();
  for (const [, key, value] of body[1].matchAll(/^\s*(\w+):\s*([^,]+),/gm)) {
    entries.set(key, value.startsWith('"') ? JSON.parse(value) : (constants.get(value) ?? value));
  }
  return entries;
}

test("the pane names every route the chat page names, at the same path", () => {
  const page = chatPageEndpoints();
  assert.ok(page.size >= 15, "the chat page's routes were read");
  for (const [key, path] of page) {
    const pane = (OFFICE_PANEL_ENDPOINTS as Record<string, unknown>)[key];
    assert.equal(pane === null ? "null" : pane, path, key);
  }
});

test("the routes sit under the platform's /api/v1", () => {
  assert.equal(panelApiBaseUrl("https://xtm.example.com/"), "https://xtm.example.com/api/v1");
  assert.equal(panelApiBaseUrl("https://xtm.example.com"), "https://xtm.example.com/api/v1");
});

test("the pane's upload limit is the platform's, as the chat page's is", () => {
  const constants = readFileSync(new URL("../src/lib/constants.ts", import.meta.url), "utf8");
  const mb = Number(constants.match(/export const MAX_CHAT_UPLOAD_SIZE_MB = (\d+);/)?.[1]);
  assert.equal(OFFICE_PANEL_MAX_UPLOAD_BYTES, mb * 1024 * 1024);
});

test("a user's message is the pane's text on its dimmed accent, as before the panel, not white on the accent", () => {
  const css = readFileSync(new URL("../../apps/microsoft-office/src/index.css", import.meta.url), "utf8");
  assert.equal(OFFICE_PANEL_THEME.userBackground, "var(--cyan-dim)");
  assert.equal(OFFICE_PANEL_THEME.userText, "var(--text-primary)");
  // Both tokens are defined by the pane's own stylesheet, in each of its themes.
  for (const token of ["--cyan-dim", "--text-primary"]) assert.equal(css.split(`${token}:`).length - 1, 2, token);
});

test("the panel's secondary ink is the pane's secondary text, defined in each of its themes", () => {
  const css = readFileSync(new URL("../../apps/microsoft-office/src/index.css", import.meta.url), "utf8");
  // Unset, the panel falls back to a gray under the 4.5:1 floor on the light pane (UX round 1 of xtm-one #11418, B1).
  assert.equal(OFFICE_PANEL_THEME.textMuted, "var(--text-secondary)");
  assert.equal(css.split("--text-secondary:").length - 1, 2);
});

test("the pane's composer box has the web chat's corner: the design system's 4px small radius", () => {
  const host = readFileSync(new URL("../src/components/chat-panel/panel-host.ts", import.meta.url), "utf8");
  // The web chat's composer takes the design system's small radius (4px); the pane, which does not load
  // the design system's tokens (its own --radius-sm is 6px), writes the same 4px out.
  assert.match(host, /^\s*inputRadius: "var\(--radius-sm\)",$/m, "the web chat's composer radius changed: update the pane's");
  assert.equal(OFFICE_PANEL_THEME.inputRadius, "4px");
});

test("the pane's panel strings are the web chat's English wording, the AI notice under the composer first", () => {
  const en = JSON.parse(readFileSync(new URL("../src/i18n/lang/en.json", import.meta.url), "utf8")) as Record<string, string>;
  assert.ok("Uses AI. Verify results." in OFFICE_PANEL_STRINGS, "the AI notice key is mapped");
  for (const [key, text] of Object.entries(OFFICE_PANEL_STRINGS)) assert.equal(text, en[key], key);
  assert.equal(officePanelTranslate("Uses AI. Verify results."), "AI can make mistakes. Check important information.");
  // Any other key goes back to the panel, which falls back to its own catalog.
  assert.equal(officePanelTranslate("Send"), "Send");
});

test("the pane limits each file and the files of a message, not their total, as the chat page does", () => {
  assert.deepEqual(OFFICE_PANEL_FILE_LIMITS, {
    maxFileSize: OFFICE_PANEL_MAX_UPLOAD_BYTES,
    maxTotalSize: Number.POSITIVE_INFINITY,
    maxFileCount: OFFICE_PANEL_MAX_FILES_PER_MESSAGE,
  });
  const constants = readFileSync(new URL("../src/lib/constants.ts", import.meta.url), "utf8");
  const web = /export const MAX_CHAT_ATTACHMENTS_PER_MESSAGE = (\d+);/.exec(constants);
  assert.equal(Number(web?.[1]), OFFICE_PANEL_MAX_FILES_PER_MESSAGE);
});

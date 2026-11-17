/**
 * Chat-unavailable copy - `yarn test`.
 *
 * `chat_enabled` is false both without a licence and when an administrator
 * switched AI off. The chat page explains the admin-off case with the shared
 * AI-disabled notice (`AiDisabledNotice`), never with the licence wording,
 * and the licence wording carries no em dash (house rule) in any language.
 * The sidebar, the suggestion chips and the composer icon buttons give the
 * same reason, and the AI-off strings carry no em dash.
 */
import { expect, test } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as shadow-overlay-floating.test.ts.
const SRC = join(process.cwd(), "src");
const read = (path: string) => readFileSync(join(SRC, path), "utf8");

const LOCALES = ["en", "fr", "es", "ja"];
const EM_DASH = "\u2014";

test("no chat-unavailable string carries an em dash, in any catalog", () => {
  for (const locale of LOCALES) {
    const catalog = JSON.parse(read(`i18n/lang/${locale}.json`)) as Record<string, string>;
    const offending = Object.entries(catalog).filter(
      ([key, value]) => key.startsWith("Chat is unavailable") && (key.includes(EM_DASH) || value.includes(EM_DASH)),
    );
    expect(offending, `${locale}.json`).toEqual([]);
  }
});

// The chat empty state and the dashboard banner: no em dash in the
// key (the English copy) nor in any translation.
const EMPTY_STATE_KEYS = [
  "New conversations cannot be started while AI is off. Past conversations stay readable: open one from the history.",
  "Threat intelligence, adversary emulation, detection engineering, and exposure management, powered by your connected OpenCTI and OpenAEV platforms.",
  "Ask me anything: triage emails, review PRs, draft messages, query data, or just chat. I can use your connected integrations and tools.",
];

test("the chat empty state and dashboard banner copy carries no em dash, in any catalog", () => {
  for (const locale of LOCALES) {
    const catalog = JSON.parse(read(`i18n/lang/${locale}.json`)) as Record<string, string>;
    for (const key of EMPTY_STATE_KEYS) {
      expect(key.includes(EM_DASH), key).toBe(false);
      expect(catalog[key], `${locale}.json: ${key}`).toBeTruthy();
      expect(catalog[key].includes(EM_DASH), `${locale}.json: ${key}`).toBe(false);
    }
  }
});

test("the licence strings the chat surfaces use exist in every catalog", () => {
  const keys = [
    "Chat is unavailable. Start a free XTM One trial or contact sales to enable agentic chat\u2026",
    "Chat is unavailable. Start a trial, contact sales, or ask an admin to activate a license",
  ];
  for (const locale of LOCALES) {
    const catalog = JSON.parse(read(`i18n/lang/${locale}.json`)) as Record<string, string>;
    for (const key of keys) expect(catalog[key], `${locale}.json: ${key}`).toBeTruthy();
  }
});

test("the chat page shows the AI-disabled notice before the licence wording", () => {
  const page = read("pages/ChatPanelPage.tsx");
  expect(page).toContain("useAiActionDisabled()");
  expect(page).toMatch(
    /aiOff \? \(\s*<AiDisabledNotice \/>\s*\) : !chatSendEnabled \? \(\s*t_i18n\("Chat is unavailable\. Start a free XTM One trial or contact sales to enable agentic chat\u2026"\)/,
  );
});

// One reason on every chat surface in admin-off. Each surface derives
// its off label as `useAiDisabledReason() ?? <licence wording>` once, and no
// tip or aria-label names the licence wording directly any more. The chat page's
// composer shows the AI-disabled notice instead (test above).
test.each(["pages/chat/ConversationSidebar.tsx"])(
  "%s reads the AI-disabled reason before the licence wording",
  (path) => {
    const source = read(path);
    const licence = 't_i18n("Chat is unavailable. Start a trial, contact sales, or ask an admin to activate a license")';
    expect(source).toContain(`useAiDisabledReason() ?? ${licence}`);
    expect(source.split(licence).length - 1).toBe(1);
  },
);

// The keyboard reaches the off reason. A disabled icon button can't take
// focus, so its Tip wraps it (`wrap="auto"`, a tab stop with a focus ring)
// instead of a bare, unfocusable `<span className="inline-flex">`, and the
// shared Tip draws the ring (tip-wrap-focus-ring.test.ts). The panel's own send
// button and its name come from @filigran/chatbot.
test.each(["pages/chat-panel/ComposerParts.tsx", "pages/chat/ConversationSidebar.tsx"])(
  "%s lets the keyboard reach the reason of a disabled chat button",
  (path) => {
    const source = read(path);
    expect(source).not.toMatch(/<span className="inline-flex">\s*<IconButton/);
    expect(source).toContain('wrap="auto"');
  },
);

test("the AI-off strings carry no em dash and the fr path names no page twice", () => {
  const aiOff = (key: string) => key.includes("AI is disabled on this platform") || key.includes("enable AI in Settings");
  for (const locale of LOCALES) {
    const catalog = JSON.parse(read(`i18n/lang/${locale}.json`)) as Record<string, string>;
    const entries = Object.entries(catalog).filter(([key]) => aiOff(key));
    expect(entries.length, `${locale}.json`).toBeGreaterThan(0);
    for (const [key, value] of entries) {
      expect(key.includes(EM_DASH) || value.includes(EM_DASH), `${locale}.json: ${key}`).toBe(false);
      expect(value, `${locale}.json: ${key}`).not.toMatch(/(\S+) \u2192 \1(?!\S)/u);
    }
  }
  expect(read("components/ui/empty-state.tsx")).not.toContain("AI is disabled on this platform \u2014");
});

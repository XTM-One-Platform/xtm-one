/**
 * The Key exposure picker of the API keys list shows its value whole in every
 * locale (#9375, UX round 1 of #10876: "Beyond the maximum lifetime" was cut
 * at 14 px in a 220 px trigger) - `yarn test`.
 *
 * Source guard: the trigger width written in ApiKeysAdminSection.tsx, minus
 * the padding and the chevron, must hold the widest label of the picker in
 * en, fr, es and ja, estimated at the 14 px body size with a conservative
 * average glyph width (Latin 6.9 px, measured 6.6 to 7.0 px in the lab; CJK
 * full width 15 px).
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const SOURCE = readFileSync(join(process.cwd(), "src", "pages", "settings", "ApiKeysAdminSection.tsx"), "utf8");
const LOCALES = ["en", "fr", "es", "ja"] as const;
/** Trigger padding (left and right) plus the chevron and its gap, measured 50 px in the lab. */
const CHROME_PX = 50;

function catalog(locale: string): Record<string, string> {
  return JSON.parse(readFileSync(join(process.cwd(), "src", "i18n", "lang", `${locale}.json`), "utf8"));
}

function estimatedWidth(text: string): number {
  let width = 0;
  for (const char of text) width += (char.codePointAt(0) ?? 0) >= 0x2e80 ? 15 : 6.9;
  return width;
}

function pickerLabels(): string[] {
  const block = /const ISSUE_LABELS = \{([\s\S]*?)\}/.exec(SOURCE);
  expect(block, "ISSUE_LABELS not found in ApiKeysAdminSection.tsx").not.toBeNull();
  const labels = [...(block?.[1] ?? "").matchAll(/:\s*"([^"]+)"/g)].map((match) => match[1]);
  return [...labels, "Any scope or expiry"];
}

function triggerWidth(): number {
  const trigger = /<SelectTrigger[^>]*w-\[(\d+)px\][^>]*aria-label=\{t_i18n\("Key exposure"\)\}/.exec(SOURCE);
  expect(trigger, "the Key exposure trigger has no fixed w-[Npx] width").not.toBeNull();
  return Number(trigger?.[1]);
}

describe("API keys Key exposure picker", () => {
  it("holds its widest label in every locale", () => {
    const room = triggerWidth() - CHROME_PX;
    const labels = pickerLabels();
    expect(labels.length).toBeGreaterThan(1);
    const tooWide: string[] = [];
    for (const locale of LOCALES) {
      const strings = catalog(locale);
      for (const key of labels) {
        const text = strings[key] ?? key;
        const width = estimatedWidth(text);
        if (width > room) tooWide.push(`${locale}: "${text}" ~${Math.round(width)} px > ${room} px`);
      }
    }
    expect(tooWide).toEqual([]);
  });
});

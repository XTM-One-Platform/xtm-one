// Gate: every row of the home dashboard Last Conversations widget keeps one height with nothing
// clipped (Design standards s.4; the content was padded from the top of the 56 px row and measured
// 59 px, so the row cut 3 px of it at 1728 and 1080, light and dark).

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const SOURCE = readFileSync(resolve(__dirname, "Dashboard.tsx"), "utf8");
const START = SOURCE.indexOf("function ConversationCard(");
const CONVERSATION_CARD = SOURCE.slice(START, SOURCE.indexOf("\n}\n", START));

describe("Home dashboard Last Conversations widget rows", () => {
  it("reads the ConversationCard source", () => {
    expect(START).toBeGreaterThan(-1);
    expect(CONVERSATION_CARD.length).toBeGreaterThan(0);
  });

  it("centres the content in the 56 px row instead of padding it from the top", () => {
    expect(CONVERSATION_CARD).toMatch(/overflow-hidden hover:bg-elevation-hover h-14/);
    expect(CONVERSATION_CARD).toMatch(/className="relative flex h-full items-center px-3"/);
    expect(CONVERSATION_CARD).not.toMatch(/className="relative p-3"/);
  });

  it("keeps the time and the arrow on one line, never stacked", () => {
    expect(CONVERSATION_CARD).not.toMatch(/flex-col/);
    expect(CONVERSATION_CARD).toMatch(/whitespace-nowrap">\{formatTimeAgo\(intl, conversation\.updated_at\)\}/);
  });

  it("names a cut title with the design-system tooltip", () => {
    expect(CONVERSATION_CARD).toMatch(/<TruncatedText text=\{conversation\.title\}/);
  });
});

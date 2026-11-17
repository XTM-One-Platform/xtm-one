// Gate: the built-in tool card
// copy carries no em dash (U+2014). The backend guard covers the English source
// in builtin_tool_groups.py; this one covers every translated catalog value of
// the same strings, so a French, Spanish or Japanese user never sees one either.

import { describe, expect, it } from "vitest";

import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";

const CARD_COPY_STARTS = [
  "Manage the platform: agents, assignments",
  "Manage your {name} platform: create and configure agents",
  // The Platform card.
  "Manage the platform from chat: agents, assignments",
  "Manage your {name} platform through conversation, acting as the user",
  "A durable, claimable work queue that survives runs.",
  "Control a headless web browser to navigate pages",
  "Fetch and read content from public web pages.",
  "Build an interactive slide deck from structured slides",
  "Find a reusable design template (presentation deck",
  "Author a reusable design template from a conversation",
  "Look at a source document instead of only reading",
];

const keys = Object.keys(en).filter((key) => CARD_COPY_STARTS.some((start) => key.startsWith(start)));
const catalogs: Record<string, Record<string, string>> = { en, es, fr, ja };

describe("built-in tool card copy", () => {
  it("finds every card string in the English catalog", () => {
    expect(keys).toHaveLength(CARD_COPY_STARTS.length);
  });

  it.each(Object.keys(catalogs))("%s has no em dash in the card copy", (lang) => {
    for (const key of keys) {
      const value = catalogs[lang][key] ?? "";
      expect(value.includes("\u2014"), `${lang}: ${key.slice(0, 48)}`).toBe(false);
    }
  });
});

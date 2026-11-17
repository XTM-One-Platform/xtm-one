/**
 * The studio rail greets a new conversation with the chat panel's own empty
 * state (the agent's mark, one greeting line, its suggestions as design-system
 * chips), as /chat and an agent's conversation do: UX retest 09a3805e, F1. It
 * draws its own screen only when no conversation can start (no agent, AI off).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const source = readFileSync(resolve(__dirname, "StudioRail.tsx"), "utf8");

describe("StudioRail welcome", () => {
  it("leaves a startable conversation to the panel's empty state", () => {
    expect(source).toMatch(/const welcome = startBlocked \|\| aiOff \? \(/);
    expect(source).toMatch(/\) : undefined;/);
  });

  it("hands the studio's prompts to the panel instead of drawing pills", () => {
    expect(source).toContain("promptSuggestions={subject.suggestions}");
    expect(source).not.toMatch(/rounded-full border/);
  });
});

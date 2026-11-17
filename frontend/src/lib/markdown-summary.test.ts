// The one-line summary a list row shows of Markdown an agent wrote.

import { describe, expect, it } from "vitest";

import { markdownSummary } from "./markdown-summary";

describe("markdownSummary", () => {
  it("strips emphasis and code and joins list items", () => {
    expect(markdownSummary("**Deployed:** production runs `cf04d0a`\n- a\n- b")).toBe(
      "Deployed: production runs cf04d0a · a · b",
    );
  });

  it("keeps plain text as it is, on one line", () => {
    expect(markdownSummary("Read the 120 pages\nand store each one.")).toBe("Read the 120 pages and store each one.");
  });

  it("gives a heading, a link and a quote their text and leaves a code block out", () => {
    expect(
      markdownSummary("## Status\n> **Done** at [the PR](https://example.com/1)\n```ts\nconst x = 1;\n```\nNext _step_"),
    ).toBe("Status · Done at the PR · Next step");
  });

  it("reads a table row by row and drops its rule", () => {
    expect(markdownSummary("| PR | State |\n|---|---|\n| #1 | **merged** |")).toBe("PR · State · #1 · merged");
  });

  it("never leaves a stray marker and keeps snake_case and arithmetic", () => {
    expect(markdownSummary("** ✅ Assignment re-enabled.** Read `live")).toBe("✅ Assignment re-enabled. Read live");
    expect(markdownSummary("call work_item_id with 2 * 3 * 4")).toBe("call work_item_id with 2 * 3 * 4");
  });

  it("keeps an autolink's address and drops a real tag", () => {
    expect(markdownSummary("<https://example.test>")).toBe("https://example.test");
    expect(markdownSummary("Mail <ops@example.test> about <b>it</b>")).toBe("Mail ops@example.test about it");
  });

  it("unwraps only a CommonMark email autolink and keeps other angle-bracketed text", () => {
    expect(markdownSummary("<a@b.c>")).toBe("a@b.c");
    expect(markdownSummary("<a@b/c>")).toBe("<a@b/c>");
    for (const malformed of ["<a..b@c.d>", "<.a@c.d>", "<a.@c.d>"]) {
      expect(markdownSummary(malformed)).toBe(malformed);
    }
    expect(markdownSummary('Ping <br/> and <span class="x">ok</span>')).toBe("Ping and ok");
  });

  it("answers an empty string for no text", () => {
    expect(markdownSummary(null)).toBe("");
    expect(markdownSummary("")).toBe("");
    expect(markdownSummary("---\n\n")).toBe("");
  });
});

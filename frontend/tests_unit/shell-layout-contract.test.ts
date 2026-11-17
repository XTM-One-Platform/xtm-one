import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The app shell against the design system's `rules/Layout.md` (Refs #10533).
 *
 * The contract fixes three values a product must not restate its own way:
 * the Content padding (`p-6`), the shared shell surface (`bg-gradient-default`)
 * and the Content minimum width before a horizontal scroll (880 px, so 1080 px
 * with the expanded navbar). The documented departures (a sticky rather than
 * fixed Header, F14; the navbar widths mirrored for the content offset, guarded
 * by header-height.test.ts; no 880 px Content minimum, because the product
 * supports 1024 px without a page scroll) live in fds-migration/LIBRARY-FEEDBACK.md.
 */
const FRONTEND = path.resolve(__dirname, "..");
const read = (rel: string) => readFile(path.join(FRONTEND, rel), "utf8");

describe("shell layout contract", () => {
  it("pads the Content with p-6 at every width", async () => {
    const source = await read("src/components/layout/AppLayout.tsx");
    const main = source.match(/<main id="app-content" className="([^"]+)"/);
    expect(main?.[1].split(/\s+/)).toContain("p-6");
    expect(main?.[1]).not.toMatch(/(^|\s)(sm|md|lg|xl):p-/);
  });

  it("paints the shell on the shared gradient surface", async () => {
    const source = await read("src/components/layout/AppLayout.tsx");
    const shell = source.slice(source.indexOf("function AppShell"), source.indexOf("export function AppLayout"));
    expect(shell).toContain('className="flex min-h-screen bg-gradient-default"');
  });

  it("sets no Content minimum width, so 1024 px never scrolls the page sideways", async () => {
    const source = await read("src/components/layout/AppLayout.tsx");
    const main = source.match(/<main id="app-content" className="([^"]+)"/);
    expect(main?.[1]).not.toMatch(/(^|\s)(sm:|md:|lg:|xl:)?min-w-(?!0\b)/);
  });

  it.each([
    "src/pages/AgentChatConversation.tsx",
    "src/components/chat-panel/ChatFrame.tsx",
  ])("%s bleeds by exactly the Content padding", async (rel) => {
    const source = await read(rel);
    expect(source).toContain("-m-6");
    expect(source).not.toMatch(/(sm|lg):-m-\d/);
  });
});

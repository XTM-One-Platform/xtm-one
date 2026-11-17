/**
 * The /chat page's first paint (#10910): one skeleton, the panel's own, in its
 * reading column, from a hard reload to the conversation. No placeholder of
 * the host's paints before it: the route chunk and the agent resolution show
 * the chat's empty frame, and the resume waits behind the panel's
 * `historyPending` instead of a full-width host copy as its welcome slot.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import { ChatFrame } from "@/components/chat-panel/ChatFrame";

const SRC = join(process.cwd(), "src");
const read = (path: string) => readFileSync(join(SRC, path), "utf8");

describe("the chat's first paint", () => {
  it("the frame alone paints no placeholder", () => {
    const markup = renderToStaticMarkup(<ChatFrame />);
    expect(markup).toContain('data-testid="chat-frame"');
    expect(markup).not.toContain("aria-busy");
    expect(markup).not.toContain('role="status"');
  });

  it("the page waits in the frame, and the resume behind the panel's own history skeleton", () => {
    const page = read("pages/ChatPanelPage.tsx");
    expect(page).not.toMatch(/PageSkeleton|ResumingConversation/);
    expect(page).toContain("if (initialAgent === undefined) return <ChatFrame />;");
    expect(page).toContain("historyPending={resumePending && !conversationId}");
    expect(page).not.toMatch(/welcome=\{resumePending/);
  });

  it("the route chunk loads in the same frame, not a page skeleton", () => {
    const app = read("App.tsx");
    expect(app).toContain('<Route path="/chat" element={<ChatGate><Page fallback={<ChatFrame />}><ChatPanelPage /></Page></ChatGate>} />');
  });
});

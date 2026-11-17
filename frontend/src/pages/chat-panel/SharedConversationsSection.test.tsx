// The conversations shared with the user, under the panel's list: they follow
// its search by title, owner and last message, whatever the case, and the
// section is left out when none matches.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { SharedConversationsSection } from "@/pages/chat-panel/ConversationBanner";
import { renderWithProviders } from "@/testing/providers";
import type { ConversationListItem } from "@/types";

const shared = vi.hoisted(() => [
  { id: "c-1", title: "Ransomware triage", owner_name: "Grace Hopper", last_message_preview: "Check the hashes" },
  { id: "c-2", title: "Quarterly report", owner_name: "Alan Turing", last_message_preview: "Draft the CTEM summary" },
]);

vi.mock("@/hooks/api/useChatApi", () => ({ useSharedConversations: () => ({ data: shared }) }));
vi.mock("@/hooks/api/useAgentApi", () => ({ useAgents: () => ({ data: [] }) }));
vi.mock("@/pages/chat/ConversationSidebar", () => ({
  ConversationRow: ({ conv }: { conv: ConversationListItem }) => <div role="listitem">{conv.title}</div>,
}));

function rows(query: string) {
  const view = renderWithProviders(
    <SharedConversationsSection query={query} activeConversationId={null} onSelect={() => {}} />,
  );
  const titles = screen.queryAllByRole("listitem").map((row) => row.textContent);
  view.unmount();
  return titles;
}

describe("SharedConversationsSection", () => {
  it("lists every shared conversation while the search is empty", () => {
    expect(rows("")).toEqual(["Ransomware triage", "Quarterly report"]);
  });

  it("follows the search by title, owner and last message, whatever the case", () => {
    expect(rows("RANSOM")).toEqual(["Ransomware triage"]);
    expect(rows("turing")).toEqual(["Quarterly report"]);
    expect(rows("hashes")).toEqual(["Ransomware triage"]);
  });

  it("leaves the section out when no shared conversation matches", () => {
    renderWithProviders(<SharedConversationsSection query="nothing like it" activeConversationId={null} onSelect={() => {}} />);
    expect(screen.queryByText("Shared with me")).toBeNull();
  });
});

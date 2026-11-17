// A Slack or Teams direct-message thread in the web's conversation list.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { useIntl } from "react-intl";

import { renderWithProviders } from "@/testing/providers";
import type { Agent, ConversationListItem } from "@/types";
import { ConversationRow, ConversationSidebar } from "./ConversationSidebar";

function conversation(channel: string, overrides: Partial<ConversationListItem> = {}): ConversationListItem {
  return {
    id: `conv-${channel}`,
    title: "Incident review",
    channel,
    agent_id: null,
    updated_at: new Date().toISOString(),
    ...overrides,
  } as unknown as ConversationListItem;
}

const LINKED = {
  id: "ctem",
  name: "CTEM Assistant",
  enabled: true,
  disable_chat: false,
  is_company_managed: true,
  access_restricted: false,
} as unknown as Agent;

/** The row as the sidebar renders it, with the page's translator. */
function Row({ conv, onSelect }: { conv: ConversationListItem; onSelect: (id: string) => void }) {
  const intl = useIntl();
  return (
    <ConversationRow
      conv={conv}
      agentName={null}
      isActive={false}
      onSelect={onSelect}
      t_i18n={(id, values) => intl.formatMessage({ id, defaultMessage: id }, values)}
    />
  );
}

describe("ConversationRow", () => {
  it.each([
    ["slack", "Slack"],
    ["teams", "Teams"],
  ])("marks a conversation that started in %s, named on hover", async (channel, label) => {
    renderWithProviders(<Row conv={conversation(channel)} onSelect={() => {}} />);

    const icon = screen.getByAltText(channel);
    fireEvent.focus(icon.parentElement as HTMLElement);
    expect((await screen.findAllByText(`Started in ${label}`)).length).toBeGreaterThan(0);
  });

  it("marks nothing on a web conversation", () => {
    renderWithProviders(<Row conv={conversation("web")} onSelect={() => {}} />);

    expect(screen.getByText("Incident review")).toBeTruthy();
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.queryByText(/Started in/)).toBeNull();
  });

  it("opens the conversation it shows", () => {
    const onSelect = vi.fn();
    renderWithProviders(<Row conv={conversation("slack")} onSelect={onSelect} />);

    fireEvent.click(screen.getByText("Incident review"));
    expect(onSelect).toHaveBeenCalledWith("conv-slack");
  });

  it("shows an agent's mark on a conversation the linked agent answers, none on the General Assistant's", () => {
    const { container, unmount } = renderWithProviders(
      <Row conv={conversation("teams", { answering_agent_id: "ctem" })} onSelect={() => {}} />,
    );
    expect(container.querySelector(".lucide-bot")).not.toBeNull();
    unmount();

    const { container: general } = renderWithProviders(<Row conv={conversation("teams")} onSelect={() => {}} />);
    expect(general.querySelector(".lucide-bot")).toBeNull();
    expect(general.querySelector(".lucide-message-square")).not.toBeNull();
  });
});

describe("ConversationSidebar", () => {
  const noop = () => {};

  function sidebar(props: {
    conversations: ConversationListItem[] | undefined;
    conversationsLoading?: boolean;
    conversationsFailed?: boolean;
  }) {
    return (
      <ConversationSidebar
        {...props}
        activeConvId={null}
        selectedAgent={null}
        agents={[]}
        showSidebar
        onSelectConversation={noop}
        onNewConversation={noop}
        onArchive={noop}
        onRename={noop}
        onClearAll={async () => {}}
        onSelectAgent={noop}
      />
    );
  }

  // A list on its way is not an empty list.
  it("shows a list skeleton, not the empty state, while the first read is in flight", () => {
    renderWithProviders(sidebar({ conversations: undefined, conversationsLoading: true }));

    const loading = screen.getByTestId("conversation-list-loading");
    expect(loading.getAttribute("role")).toBe("status");
    expect(loading.getAttribute("aria-busy")).toBe("true");
    expect(screen.queryByText("No conversations yet")).toBeNull();
  });

  // A failed read is not an empty list either, and never the raw detail.
  it("shows one inline error line, not the empty state, when the list read failed", () => {
    renderWithProviders(sidebar({ conversations: undefined, conversationsFailed: true }));

    const error = screen.getByTestId("conversation-list-error");
    expect(error.getAttribute("role")).toBe("alert");
    expect(error.textContent).toBe("Could not load your conversations.");
    expect(screen.queryByText("No conversations yet")).toBeNull();
    expect(screen.getByLabelText("Search conversations...")).toBeTruthy();
  });

  it("keeps the list it already read when a later refetch fails", () => {
    renderWithProviders(
      sidebar({ conversations: [conversation("web", { title: "Kept thread" })], conversationsFailed: true }),
    );

    expect(screen.getByText("Kept thread")).toBeTruthy();
    expect(screen.queryByTestId("conversation-list-error")).toBeNull();
  });

  it("shows the empty state once the list is read and empty", () => {
    renderWithProviders(sidebar({ conversations: [], conversationsLoading: false }));

    expect(screen.getByText("No conversations yet")).toBeTruthy();
    expect(screen.queryByTestId("conversation-list-loading")).toBeNull();
  });

  it("names the agent that answers each conversation, the one standing in for the General Assistant included", () => {
    renderWithProviders(
      <ConversationSidebar
        conversations={[
          conversation("teams", { id: "teams-dm", title: "Teams thread", answering_agent_id: "ctem" }),
          conversation("web", { id: "general", title: "Ask anything" }),
        ]}
        activeConvId={null}
        selectedAgent={null}
        agents={[LINKED]}
        showSidebar
        onSelectConversation={noop}
        onNewConversation={noop}
        onArchive={noop}
        onRename={noop}
        onClearAll={async () => {}}
        onSelectAgent={noop}
      />,
    );

    const teamsRow = screen.getByText("Teams thread").closest("[role=button]") as HTMLElement;
    const generalRow = screen.getByText("Ask anything").closest("[role=button]") as HTMLElement;
    expect(teamsRow.textContent).toContain("CTEM Assistant");
    expect(generalRow.textContent).not.toContain("CTEM Assistant");
  });
});

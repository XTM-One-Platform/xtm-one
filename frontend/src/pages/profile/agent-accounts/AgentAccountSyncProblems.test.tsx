// What the last sync of an agent account found: readable causes, never Graph's raw words up front.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { AgentAccount, AgentAccountSyncReport } from "@/types";
import { AgentAccountSyncProblems } from "./AgentAccountSyncProblems";

const GRAPH = "Microsoft Graph answered 403: Operation: Create; Exception: [Status Code: Forbidden; Reason: ...]";

function account(report: AgentAccountSyncReport | null, overrides: Partial<AgentAccount> = {}): AgentAccount {
  return {
    id: "acct",
    provider: "microsoft_teams",
    provider_label: "Microsoft Teams",
    integration_id: "integration-1",
    status: "active",
    can_manage: true,
    sync_report: report,
    ...overrides,
  } as AgentAccount;
}

const REPORT: AgentAccountSyncReport = {
  checked_at: new Date().toISOString(),
  wanted: 14,
  heard: 2,
  problems: [
    {
      kind: "refused",
      count: 8,
      permission: "ChannelMessage.Read.All",
      holder: null,
      targets: Array.from({ length: 8 }, (_, i) => ({ covers: "channel" as const, name: `Engineering > Channel ${i}` })),
      details: [GRAPH],
    },
    {
      kind: "taken_by_account",
      count: 1,
      permission: null,
      holder: "Executive Assistant",
      targets: [{ covers: "chats", name: null }],
      details: [GRAPH],
    },
  ],
};

function renderProblems(report: AgentAccountSyncReport | null, overrides: Partial<AgentAccount> = {}) {
  const onEdit = vi.fn();
  const onSync = vi.fn();
  const view = renderWithProviders(
    <MemoryRouter>
      <AgentAccountSyncProblems account={account(report, overrides)} onEdit={onEdit} onSync={onSync} syncing={false} />
    </MemoryRouter>,
  );
  return { ...view, onEdit, onSync };
}

describe("AgentAccountSyncProblems", () => {
  it("says nothing when the last sync found nothing, or never ran", () => {
    expect(renderProblems(null).container.textContent).toBe("");
    expect(renderProblems({ ...REPORT, problems: [], heard: 14 }).container.textContent).toBe("");
  });

  it("says how much is not heard, why, where and what to do - the provider's words folded", () => {
    renderProblems(REPORT);
    expect(screen.getByText("Not listening to 12 of 14 conversations")).toBeTruthy();
    expect(screen.getByText("Microsoft Teams refused to notify the account")).toBeTruthy();
    expect(screen.getByText(/needs the ChannelMessage.Read.All permission/)).toBeTruthy();
    expect(screen.getByText("Your agent account Executive Assistant already follows it")).toBeTruthy();
    expect(screen.getByText("All chats")).toBeTruthy();
    // A few conversations, then the rest on demand.
    expect(screen.getByText("Engineering > Channel 5")).toBeTruthy();
    expect(screen.queryByText("Engineering > Channel 7")).toBeNull();
    fireEvent.click(screen.getByText("+2 more"));
    expect(screen.getByText("Engineering > Channel 7")).toBeTruthy();
    // Graph's own words only behind the toggle.
    expect(screen.queryByText(GRAPH)).toBeNull();
    fireEvent.click(screen.getAllByText("Technical details")[0]);
    expect(screen.getByText(GRAPH)).toBeTruthy();
  });

  it("words the platform's own reasons in the viewer's language and names a nameless team", () => {
    renderProblems({
      ...REPORT,
      problems: [
        {
          kind: "not_subscribed",
          count: 2,
          permission: null,
          holder: null,
          targets: [{ covers: "team", name: null }, { covers: "channel", name: "Ops > 51" }],
          details: [],
          notes: [{ code: "past_channel_cap", values: { limit: 50 } }],
        },
      ],
    });
    expect(screen.getByText("Every channel of a team")).toBeTruthy();
    fireEvent.click(screen.getByText("Technical details"));
    expect(screen.getByText(/follows the first 50 channels of the account/)).toBeTruthy();
  });

  it("offers the action that fixes each cause", () => {
    const { onEdit, onSync } = renderProblems(REPORT);
    const link = screen.getByText("Open the integration").closest("a");
    expect(link?.getAttribute("href")).toBe("/integrations/integration-1");
    fireEvent.click(screen.getByText("Edit the rules"));
    expect(onEdit).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByText("Synchronize now"));
    expect(onSync).toHaveBeenCalledOnce();
  });

  it("says the problems are the previous check's while a new one runs", () => {
    renderProblems(REPORT, { status: "setup" });
    expect(screen.getByText(/A new check is running\./)).toBeTruthy();
  });

  it("says when the last check ran in the viewer's language", () => {
    const checkedAt = new Date(Date.now() - 5 * 60 * 1000).toISOString();
    renderWithProviders(
      <MemoryRouter>
        <AgentAccountSyncProblems account={account({ ...REPORT, checked_at: checkedAt })} onEdit={vi.fn()} onSync={vi.fn()} syncing={false} />
      </MemoryRouter>,
      { locale: "fr" },
    );
    expect(screen.getByText("Vérifié il y a 5 minutes")).toBeTruthy();
    expect(screen.queryByText(/ago/)).toBeNull();
  });

  it("says the check just ran rather than zero minutes ago", () => {
    renderProblems(REPORT);
    expect(screen.getByText("Checked just now")).toBeTruthy();
  });

  it("does not offer to edit the rules of an account its viewer cannot manage", () => {
    renderProblems(REPORT, { can_manage: false });
    expect(screen.queryByText("Edit the rules")).toBeNull();
  });
});

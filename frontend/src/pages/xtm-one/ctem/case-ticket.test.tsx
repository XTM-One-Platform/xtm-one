// The ticket chip of a case (CTEM-19-UI, #1032): the managed ticket, its sync
// state and sentence, and "No ticketing connected" when no target is declared.
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseTicket } from "@/hooks/api/useCtemCasesApi";
import { CaseTicketChip, caseTicket, ticketSyncSentence, ticketSyncState } from "./case-ticket";

const t = (id: string, values?: Record<string, string | number>) =>
  Object.entries(values ?? {}).reduce((s, [k, v]) => s.replace(`{${k}}`, String(v)), id);
const date = (iso: string) => iso.slice(0, 10);

const synced: CtemCaseTicket = {
  integration: "i-1",
  key: "SEC-12",
  url: "https://jira.example/browse/SEC-12",
  status: "in_progress",
  at: "2026-11-21T10:00:00+00:00",
  sync: { state: "in_sync", since: "2026-11-21T10:00:00+00:00", provider: "jira", external_status: "In Review" },
};

describe("caseTicket", () => {
  it("prefers the newest ticket the sync manages over a later hand-written reference", () => {
    const manual: CtemCaseTicket = { integration: "i-2", key: "OPS-1", url: null, status: "open", at: "2026-11-21T11:00:00+00:00" };
    expect(caseTicket([synced, manual])?.key).toBe("SEC-12");
    expect(caseTicket([manual])?.key).toBe("OPS-1");
    expect(caseTicket([])).toBeNull();
    expect(caseTicket(undefined)).toBeNull();
  });

  it("reads a reference without a sync record as linked", () => {
    expect(ticketSyncState({ ...synced, sync: undefined })).toBe("linked");
    expect(ticketSyncState(synced)).toBe("in_sync");
  });
});

describe("ticketSyncSentence", () => {
  it("says since when a ticket is out of date, with its fix", () => {
    const late = { ...synced, sync: { state: "out_of_date" as const, since: "2026-11-20T08:00:00+00:00", fix: "Renew the token." } };
    expect(ticketSyncSentence(late, t, date)).toBe("The ticket has not been read since 2026-11-20. Renew the token.");
  });

  it("says what failed on an error", () => {
    const failed = { ...synced, sync: { state: "error" as const, error: "403 Forbidden" } };
    expect(ticketSyncSentence(failed, t, date)).toBe("The last sync failed: 403 Forbidden");
  });

  it("names the external status when in sync", () => {
    expect(ticketSyncSentence(synced, t, date)).toBe("In sync since 2026-11-21, status in the ticketing system: In Review.");
  });
});

describe("CaseTicketChip", () => {
  it("draws the system, key and sync state of the managed ticket", () => {
    renderWithProviders(<CaseTicketChip tickets={[synced]} />);
    expect(screen.getByTestId("ctem-case-ticket")).toHaveAttribute("data-sync-state", "in_sync");
    expect(screen.getByTestId("ctem-case-ticket-link")).toHaveTextContent("Jira SEC-12");
  });

  it("draws no anchor in compact mode (inside a card link)", () => {
    renderWithProviders(<CaseTicketChip tickets={[synced]} compact />);
    expect(screen.queryByTestId("ctem-case-ticket-link")).toBeNull();
    expect(screen.getByTestId("ctem-case-ticket")).toHaveTextContent("Jira SEC-12");
  });

  it("says no ticketing is connected, with the settings link, only when asked", () => {
    const ticketing = { connected: false, reason: "No ticketing connected", settings: "/settings/organization?card=ticketing" };
    const { unmount } = renderWithProviders(<CaseTicketChip tickets={[]} ticketing={ticketing} showNotConnected />);
    expect(screen.getByTestId("ctem-case-ticket-none")).toHaveTextContent("No ticketing connected");
    unmount();
    renderWithProviders(<CaseTicketChip tickets={[]} ticketing={ticketing} />);
    expect(screen.queryByTestId("ctem-case-ticket-none")).toBeNull();
  });

  it("draws nothing for a case with no ticket when ticketing is connected", () => {
    renderWithProviders(<CaseTicketChip tickets={[]} ticketing={{ connected: true, settings: "/s" }} showNotConnected />);
    expect(screen.queryByTestId("ctem-case-ticket")).toBeNull();
    expect(screen.queryByTestId("ctem-case-ticket-none")).toBeNull();
  });
});

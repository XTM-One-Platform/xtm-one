// The fleet timeline of the CTEM command center: a row opens its run with
// "Back to Command Center", the badge and the source read as one run's, in
// every language.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { IntlProvider } from "react-intl";

import en from "@/i18n/lang/en.json";
import fr from "@/i18n/lang/fr.json";
import es from "@/i18n/lang/es.json";

import { LocationBackButton } from "@/components/ui/back-button";
import { testQueryClient } from "@/testing/providers";
import type { TimelineEntry } from "@/lib/ctem-fleet-timeline";
import { FleetTimelineCard } from "./FleetTimelineCard";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";

const viewer = vi.hoisted(() => ({ isAdmin: true }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: viewer.isAdmin }) }));

const entry = (over: Partial<TimelineEntry> = {}): TimelineEntry => ({
  runId: "r1",
  fleetId: "f1",
  fleetName: "Exposure",
  agentId: "a1",
  agentName: "Experimenter",
  status: "completed",
  outcome: "succeeded",
  at: new Date().toISOString(),
  source: "chat",
  event: null,
  caseId: null,
  ...over,
});

const CASE = {
  id: "c-1",
  title: "Patch nginx",
  state: "remediating",
} as unknown as CtemCaseSummary;

// The real catalogs, English included: the run-outcome and run-source keys
// are ids whose English text differs ("Run source: chat" reads "Chat").
const CATALOGS = { en, fr, es } as const;

function page(
  timeline: TimelineEntry[],
  locale: keyof typeof CATALOGS = "en",
  lock: { licensed: boolean; lockReason: AiLockReason | null } = { licensed: true, lockReason: null },
  casesById: Record<string, CtemCaseSummary> = {},
) {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <IntlProvider locale={locale} messages={CATALOGS[locale]} onError={() => {}}>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route
              path="/"
              element={
                <FleetTimelineCard
                  timeline={timeline}
                  casesById={casesById}
                  isLoading={false}
                  licensed={lock.licensed}
                  lockReason={lock.lockReason}
                />
              }
            />
            <Route path="/operations/executions/:id" element={<LocationBackButton to={-1} label="Back" />} />
            <Route path="/fleets" element={<p>Fleets tab</p>} />
            <Route path="/ctem/cases/:id" element={<p>Case page</p>} />
          </Routes>
        </MemoryRouter>
      </IntlProvider>
    </QueryClientProvider>,
  );
}

describe("FleetTimelineCard", () => {
  it("opens the run from a row, whose page goes back to the command center", () => {
    page([entry()]);
    fireEvent.click(screen.getByRole("button", { name: "Open run of Experimenter" }));
    expect(screen.getByRole("button", { name: "Back to Command Center" })).toBeTruthy();
  });

  it("opens the Fleets tab from the empty state", () => {
    // Flows reads its tab from the path (/flows/:tab), not from ?tab=.
    page([]);
    fireEvent.click(screen.getByRole("button", { name: "Open fleets" }));
    expect(screen.getByText("Fleets tab")).toBeTruthy();
  });

  it("names the case of a case run and opens it (#915)", () => {
    page([entry({ caseId: "c-1" })], "en", undefined, { "c-1": CASE });
    expect(screen.getByText("Remediating", { exact: false })).toBeTruthy();
    fireEvent.click(screen.getByRole("link", { name: "Open case Patch nginx" }));
    expect(screen.getByText("Case page")).toBeTruthy();
  });

  it("never names a case the reader cannot read (#915)", () => {
    page([entry({ caseId: "c-hidden" })], "en", undefined, { "c-1": CASE });
    expect(screen.queryByRole("link", { name: /Open case/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Open run of Experimenter" })).toBeTruthy();
  });

  it("shows a badge on a completed run", () => {
    page([entry()]);
    expect(screen.getByText("Succeeded")).toBeTruthy();
    expect(screen.getByText(/^Chat · Exposure$/)).toBeTruthy();
  });

  it.each([
    ["fr", "Réussie", "Échouée", "Conversation"],
    ["es", "Correcta", "Fallida", "Conversación"],
  ] as const)("says the badge and the source of one run in %s", (locale, succeeded, failed, chat) => {
    page([entry(), entry({ runId: "r2", outcome: "failed", source: "schedule" })], locale);
    expect(screen.getByText(succeeded)).toBeTruthy();
    expect(screen.getByText(failed)).toBeTruthy();
    expect(screen.getByText(new RegExp(`^${chat}`))).toBeTruthy();
  });
});

// B3 of the #9413 UX review: the same Community Edition state as the Fleet
// activity card, no "All" and never "Open fleets" (a locked page).
describe("FleetTimelineCard in Community Edition", () => {
  it("gives an administrator the Enterprise Edition state and the trial, no dead end", () => {
    viewer.isAdmin = true;
    page([], "en", { licensed: false, lockReason: "unlicensed" });
    expect(screen.getByText("Fleets run with Enterprise Edition")).toBeTruthy();
    expect(screen.getByText("Start free trial")).toBeTruthy();
    expect(screen.queryByText("No fleet run yet")).toBeNull();
    expect(screen.queryByRole("button", { name: /Open fleets/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^All$/ })).toBeNull();
  });

  it("gives a non-administrator the sentence alone, no button", () => {
    viewer.isAdmin = false;
    page([], "en", { licensed: false, lockReason: "unlicensed" });
    expect(screen.getByText("Ask your administrator to activate a license.")).toBeTruthy();
    expect(screen.queryByText("Start free trial")).toBeNull();
    viewer.isAdmin = true;
  });
});

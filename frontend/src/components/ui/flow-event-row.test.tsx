// The rows of the agent flow drawer and the /flows/map activity feed read in
// the viewer's language: the status badge and the relative time.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { FlowEventRow, type FlowEvent } from "./flow-event-row";

const NOW = Date.parse("2026-11-03T10:00:00Z");

function event(status: string, minutesAgo: number): FlowEvent {
  return {
    id: `e-${status}`,
    kind: "run",
    status,
    title: "Nightly sweep",
    timestamp: new Date(NOW - minutesAgo * 60_000).toISOString(),
  };
}

describe("FlowEventRow", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("reads the status and the relative time in French under the fr catalog", () => {
    renderWithProviders(
      <>
        <FlowEventRow event={event("failed", 6)} />
        <FlowEventRow event={event("pending", 11)} />
        <FlowEventRow event={event("in_progress", 120)} />
      </>,
      { locale: "fr-fr" },
    );
    expect(screen.getByText("Échoué")).toBeTruthy();
    expect(screen.getByText("En attente")).toBeTruthy();
    expect(screen.getByText("En cours")).toBeTruthy();
    expect(screen.getByText("il y a 6 min")).toBeTruthy();
    expect(screen.getByText("il y a 11 min")).toBeTruthy();
    expect(screen.queryByText(/^(Failed|Pending|In progress)$/)).toBeNull();
    expect(screen.queryByText(/m ago$/)).toBeNull();
  });

  it("reads a work item's open status as the adjective, never the verb", () => {
    renderWithProviders(
      <FlowEventRow event={{ ...event("open", 3), kind: "work_item" }} />,
      { locale: "fr-fr" },
    );
    expect(screen.getByText("Ouvert")).toBeTruthy();
    expect(screen.queryByText("Ouvrir")).toBeNull();
  });

  it("writes the awaiting status in sentence case", () => {
    renderWithProviders(<FlowEventRow event={event("awaiting_human", 2)} />);
    expect(screen.getByText("Awaiting input")).toBeTruthy();
  });

  it("keeps the English labels and compact time in English", () => {
    renderWithProviders(<FlowEventRow event={event("failed", 6)} />);
    expect(screen.getByText("Failed")).toBeTruthy();
    expect(screen.getByText("6m ago")).toBeTruthy();
  });
});

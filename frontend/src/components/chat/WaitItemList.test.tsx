// What a pause watches: each item by its name (or its kind), its short
// id and its state, how long a running one has run, and a count of the rest.

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";

import { WaitItemList } from "@/components/chat/WaitItemList";
import { renderWithProviders } from "@/testing/providers";
import type { WaitItem } from "@/types";

const item = (id: string, overrides: Partial<WaitItem> = {}): WaitItem => ({
  id,
  kind: "task",
  label: null,
  status: "queued",
  started_at: null,
  ...overrides,
});

describe("WaitItemList", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("names each item, says its state and how long a running one has run", () => {
    vi.useFakeTimers({ now: Date.parse("2026-10-22T10:01:05Z") });
    renderWithProviders(
      <WaitItemList
        items={[
          item("aaaaaaaa-1111", { label: "Enrich IOCs", status: "running", started_at: "2026-10-22T10:00:00Z" }),
          item("bbbbbbbb-2222", { kind: "run", status: "awaiting_human" }),
        ]}
      />,
    );
    const [first, second] = within(screen.getByTestId("wait-items")).getAllByRole("listitem");
    expect(first).toHaveTextContent("Enrich IOCs");
    expect(first).toHaveTextContent("aaaaaaaa");
    expect(first).toHaveTextContent("Running");
    expect(first).toHaveTextContent("1m 5s");
    expect(second).toHaveTextContent("Assignment run");
    expect(second).toHaveTextContent("Waiting for an answer");
  });

  it("lists four and counts the rest", () => {
    renderWithProviders(<WaitItemList items={["1", "2", "3", "4", "5", "6"].map((id) => item(id))} />);
    const rows = within(screen.getByTestId("wait-items")).getAllByRole("listitem");
    expect(rows).toHaveLength(5);
    expect(rows[4]).toHaveTextContent("+2 more");
  });
});

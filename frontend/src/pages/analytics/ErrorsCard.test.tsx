// Top errors at 1024: the card is 696 px wide. The Error column must take the width the
// other four columns leave and cut its text there (no fixed width), so the table never
// scrolls sideways inside its card; the full message stays in the tooltip.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { ErrorsCard } from "@/pages/analytics/ErrorsCard";

const LONG = "upstream request timed out after 120 s while calling the enrichment connector ".repeat(3).trim();

describe("ErrorsCard", () => {
  it("lets the Error column shrink to the card width (no fixed width, one line, keyboard tooltip)", () => {
    renderWithProviders(
      <MemoryRouter>
        <ErrorsCard
          items={[{ error_message: LONG, count: 3, last_seen: "2026-11-07T09:00:00Z", agent_name: "Lab agent", model: "gpt-4.1" }]}
          isLoading={false}
          isError={false}
          isFetching={false}
          errorRate={0.1}
        />
      </MemoryRouter>,
    );
    const message = screen.getByTestId("error-message");
    expect(message.textContent).toBe(LONG);
    expect(message.getAttribute("tabindex")).toBe("0");
    expect(message.closest("td")).not.toBeNull();
  });

  // "Last seen" reads in the app language, not the browser's: no English month or AM/PM in ja or fr.
  it.each([
    ["ja", "11月"],
    ["fr", "nov."],
  ])("renders Last seen in the app language (%s)", (locale, month) => {
    const { container } = renderWithProviders(
      <MemoryRouter>
        <ErrorsCard
          items={[{ error_message: "boom", count: 1, last_seen: "2026-11-07T09:00:00Z", agent_name: "Lab agent", model: null }]}
          isLoading={false}
          isError={false}
          isFetching={false}
          errorRate={0.1}
        />
      </MemoryRouter>,
      { locale },
    );
    const rows = container.querySelector("tbody")?.textContent ?? "";
    expect(rows).toContain(month);
    expect(rows).not.toMatch(/\b(AM|PM|Nov)\b/);
  });

  // Dates are relative plus absolute: the absolute date on the row (one line), the relative time
  // ("2 時間前") in the tooltip, reachable by keyboard, in the app language.
  it("shows the relative Last seen in the tooltip, in the app language (ja)", async () => {
    const lastSeen = new Date(Date.now() - 2 * 3600 * 1000).toISOString();
    renderWithProviders(
      <MemoryRouter>
        <ErrorsCard
          items={[{ error_message: "boom", count: 1, last_seen: lastSeen, agent_name: "Lab agent", model: null }]}
          isLoading={false}
          isError={false}
          isFetching={false}
          errorRate={0.1}
        />
      </MemoryRouter>,
      { locale: "ja" },
    );
    const cell = screen.getByTestId("error-last-seen");
    expect(cell.textContent).toMatch(/\d+月\d+日/);
    expect(cell.getAttribute("tabindex")).toBe("0");
    const user = userEvent.setup();
    for (let presses = 0; presses < 30 && document.activeElement !== cell; presses++) await user.tab();
    expect(document.activeElement).toBe(cell);
    const tip = (await screen.findByRole("tooltip")).textContent ?? "";
    expect(tip).toContain("2 時間前");
    expect(tip).not.toMatch(/ago/);
  });
});

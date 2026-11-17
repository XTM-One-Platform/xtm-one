// Every ranked "name, value, bar" row of the AI adoption tabs keeps a long
// name inside its card (#10396): the name is cut on one line, the full name is
// in the design-system tooltip, and the value stays visible beside it. One
// shared RankedRow draws them all; no tab keeps an inline copy.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";

import { RankedRow } from "./shared";

const LONG_EMAIL = "lab.contractor.with.a.very.long.cursor.email.address.for.truncation@example-consulting.com";
const LONG_MODEL = "claude-4.5-sonnet-thinking-max-extended-context-preview-2026-10";

describe("RankedRow", () => {
  // jsdom lays nothing out: every name reads as wider than its box, as a long
  // name does in a narrow card (the DS TruncatedText names a cut text only).
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(240);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(120);
  });
  afterEach(() => vi.restoreAllMocks());

  it("makes a cut name a tab stop that opens the tooltip on keyboard focus (#11470 UX r1 A2)", async () => {
    const user = userEvent.setup();
    renderWithProviders(<RankedRow name={LONG_EMAIL} value={42} max={100} />);

    await user.tab();
    expect(document.activeElement?.textContent).toBe(LONG_EMAIL);
    expect((await screen.findByRole("tooltip")).textContent).toContain(LONG_EMAIL);
  });

  it("cuts a 90-character email on one line and shows it whole in the tooltip", async () => {
    expect(LONG_EMAIL.length).toBe(90);
    const user = userEvent.setup();
    renderWithProviders(<RankedRow name={LONG_EMAIL} value={42} max={100} />);

    const name = screen.getByTestId("ranked-row-name");
    expect(name.textContent).toBe(LONG_EMAIL);
    expect(name.getAttribute("data-truncate")).toBe("one-line");
    expect(screen.getByText("42")).toBeTruthy();

    await user.hover(screen.getByText(LONG_EMAIL));
    expect((await screen.findByRole("tooltip")).textContent).toContain(LONG_EMAIL);
  });

  it("shows a long model name whole in the tooltip and keeps a custom value label", async () => {
    const user = userEvent.setup();
    renderWithProviders(<RankedRow name={LONG_MODEL} value={3} max={10} pct={80} valueLabel="3" />);

    expect(screen.getByTestId("ranked-row-value").textContent).toBe("3");
    await user.hover(screen.getByText(LONG_MODEL));
    expect((await screen.findByRole("tooltip")).textContent).toContain(LONG_MODEL);
  });
});

describe("AI adoption ranked cards", () => {
  // vitest runs from frontend/ (CI and `yarn test`).
  const DIR = join(process.cwd(), "src/pages/ai-adoption");
  const INLINE_ROW = /truncate max-w-\[70%\]|h-1\.5 w-full rounded-full bg-elevation-hover/;

  it("draw every ranked row with the shared RankedRow, never an inline copy", () => {
    const offenders = readdirSync(DIR)
      .filter((file) => file.endsWith(".tsx") && !file.endsWith(".test.tsx") && file !== "shared.tsx")
      .filter((file) => INLINE_ROW.test(readFileSync(join(DIR, file), "utf8")));
    expect(offenders).toEqual([]);
    const shared = readFileSync(join(DIR, "shared.tsx"), "utf8");
    expect(shared.match(/h-1\.5 w-full rounded-full bg-elevation-hover/g)?.length).toBe(1);
  });
});

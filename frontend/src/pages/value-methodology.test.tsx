// The methodology page (#9836 R7b, design dev-docs/roi-outcomes.md 10.6):
// every user reads it, both modes; an administrator gets the links to the
// settings that change a default, a user is told who changes them; the
// "Read the methodology" link of every "How this is computed" opens it.
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { RoiFigure } from "@/components/roi/RoiFigure";
import type { RoiFigures } from "@/lib/roi-figure";
import { renderWithProviders } from "@/testing/providers";

const auth = vi.hoisted(() => ({ isAdmin: false }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "u-1" }, isAdmin: auth.isAdmin }) }));

import ValueMethodology from "./ValueMethodology";

afterEach(() => {
  auth.isAdmin = false;
});

const renderPage = (locale = "en") =>
  renderWithProviders(
    <MemoryRouter initialEntries={["/tasks/value/methodology"]}>
      <ValueMethodology />
    </MemoryRouter>,
    { locale },
  );

describe("the methodology page", () => {
  it("writes the four formulas with the popover's words and the five labels of every input", () => {
    renderPage();
    const formulas = screen.getByTestId("methodology-formulas").textContent ?? "";
    expect(formulas).toContain("Value = units x (manual minutes - review minutes) x hourly rate");
    expect(formulas).toContain("Net value = value - AI cost");
    expect(formulas).toContain("ROI = net value / AI cost");
    expect(formulas).toContain("Payback = setup cost / net value per month");
    const labels = screen.getByTestId("methodology-labels");
    for (const label of ["Default", "Published estimate", "Assumption", "Measured", "Customized"]) {
      expect(within(labels).getByText(label)).toBeTruthy();
    }
    expect(labels.textContent).not.toMatch(/override/i);
  });

  it("gives every formula a worked example in numbers and indexes the six sections (UX r1 B2)", () => {
    renderPage();
    const examples = screen.getAllByTestId("methodology-example").map((e) => e.textContent ?? "");
    expect(examples).toEqual([
      "Example: 10 units x (30 - 5) min x 60 per hour = 250",
      "Example: 250 - 40 = 210",
      "Example: 210 / 40 = 5.25",
      "Example: 420 / 210 per month = 2 months",
    ]);
    // The arithmetic of the example holds: (30 - 5) min x 10 units = 250 min = 250 at 60 per hour.
    expect((10 * (30 - 5) * 60) / 60).toBe(250);
    const index = screen.getByTestId("section-index");
    expect(within(index).getAllByRole("link")).toHaveLength(6);
    for (const link of within(index).getAllByRole("link")) {
      expect(document.getElementById((link.getAttribute("href") ?? "").slice(1))).not.toBeNull();
    }
  });

  it("names review minutes once in French (UX r1 B1)", () => {
    renderPage("fr");
    expect(screen.getByTestId("methodology-model").textContent).not.toMatch(/relecture/);
    expect(screen.getByText("Minutes de revue")).toBeTruthy();
  });

  it("links an administrator to the settings that change a default", () => {
    auth.isAdmin = true;
    renderPage();
    const rows = screen.getByTestId("methodology-default-rows");
    expect(within(rows).getByRole("link", { name: "Units of work" }).getAttribute("href")).toBe("/settings/organization/units");
    expect(within(rows).getByRole("link", { name: "Rates by role" }).getAttribute("href")).toBe("/settings/organization/rates");
    expect(screen.queryByText("Your administrator changes these defaults.")).toBeNull();
  });

  it("tells a user who changes the defaults, with no link to a settings page he cannot open", () => {
    renderPage();
    expect(within(screen.getByTestId("methodology-default-rows")).queryAllByRole("link")).toHaveLength(0);
    expect(screen.getByText("Your administrator changes these defaults.")).toBeTruthy();
  });

  it("states the ledger's rules: Mine is what you did, one org total, the k rule, By person for admins (#12077 L7e)", () => {
    renderPage();
    const access = screen.getByTestId("methodology-access-rules").textContent ?? "";
    expect(access).toContain("Mine is what you did");
    expect(access).toContain("same figure for an administrator and for every user");
    expect(access).toContain("fewer than k people");
    expect(access).toContain("By person is for administrators only");
    expect(access).not.toMatch(/agents and fleets you can open/);
    const counted = screen.getByTestId("methodology-counted-rules").textContent ?? "";
    expect(counted).toContain("Automations (runs and work items), Chat (estimated)");
    expect(counted).toContain("Other AI usage, cost only");
    expect(counted).toContain("only the total when one source would have to be hidden");
    expect(counted).toContain("counts for one person");
    expect(counted).toContain('read "since" a date');
    const model = screen.getByTestId("methodology-model").textContent ?? "";
    expect(model).toContain("the measured cost of each model call");
    expect(model).not.toMatch(/model usage of the counted runs/);
  });

  it("reads in French with the same formulas as the popover", () => {
    renderPage("fr");
    const formulas = screen.getByTestId("methodology-formulas").textContent ?? "";
    expect(formulas).toContain("Valeur = unités x (minutes manuelles - minutes de revue) x taux horaire");
    expect(formulas).toContain("Amortissement = coût de mise en place / valeur nette par mois");
  });
});

const figures = (): RoiFigures =>
  ({
    runs: 17,
    units: 42,
    hours_saved: 21,
    value: 1995,
    cost: 38,
    net_value: 1957,
    roi: 51.5,
    reviewed: 8,
    rejected: 3,
    review_rate: 0.5,
  }) as unknown as RoiFigures;

describe("Read the methodology", () => {
  it("opens the methodology page from the popover of a figure", () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/tasks/value"]}>
        <Routes>
          <Route path="/tasks/value" element={<RoiFigure figures={figures()} metric="net" currency="USD" window="30d" layout="card" />} />
          <Route path="/tasks/value/methodology" element={<p>methodology page</p>} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByTestId("roi-how-net"));
    const link = within(screen.getByTestId("roi-how-popover")).getByTestId("roi-how-methodology");
    expect(link.textContent).toContain("Read the methodology");
    fireEvent.click(link);
    expect(screen.getByText("methodology page")).toBeTruthy();
  });

  it("is a plain link where no router is mounted", () => {
    renderWithProviders(<RoiFigure figures={figures()} metric="net" currency="USD" window="30d" layout="card" />);
    fireEvent.click(screen.getByTestId("roi-how-net"));
    expect(screen.getByTestId("roi-how-methodology").getAttribute("href")).toMatch(/\/tasks\/value\/methodology$/);
  });
});

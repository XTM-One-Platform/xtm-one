// The Fleets toolbar keeps one row: the secondary filters sit behind one pill.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { NO_FLEET_FILTERS } from "./fleet-list-filter";
import { isInsideFloatingList } from "@/components/ui/filter-pill";
import { FleetMoreFilters, fleetFilterCount } from "./FleetMoreFilters";

const options = { workspaceOptions: [{ value: "w1", label: "Research" }], orchestratorOptions: [] };

describe("fleetFilterCount", () => {
  it("counts each fleet filter that narrows the list", () => {
    expect(fleetFilterCount(NO_FLEET_FILTERS)).toBe(0);
    expect(fleetFilterCount({ workspaceId: "w1", orchestratorId: null, includeArchived: true })).toBe(2);
    expect(fleetFilterCount({ workspaceId: "w1", orchestratorId: "a1", includeArchived: true })).toBe(3);
  });
});

describe("isInsideFloatingList", () => {
  it("knows a press in a portaled option from one elsewhere", () => {
    const option = document.createElement("div");
    option.setAttribute("role", "option");
    document.body.appendChild(option);
    expect(isInsideFloatingList(option)).toBe(true);
    expect(isInsideFloatingList(document.body)).toBe(false);
    expect(isInsideFloatingList(null)).toBe(false);
    option.remove();
  });
});

describe("FleetMoreFilters", () => {
  it("keeps the pickers and the archived switch closed behind the pill until opened", () => {
    renderWithProviders(<FleetMoreFilters filters={NO_FLEET_FILTERS} onChange={vi.fn()} {...options} />);
    const pill = screen.getByRole("button", { name: /Filters/ });
    expect(pill.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByLabelText("Show archived")).toBeNull();

    fireEvent.click(pill);
    expect(pill.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByLabelText("Show archived")).toBeTruthy();
  });

  it("shows how many filters are set and reports the archived switch", () => {
    const onChange = vi.fn();
    renderWithProviders(
      <FleetMoreFilters filters={{ ...NO_FLEET_FILTERS, workspaceId: "w1" }} onChange={onChange} {...options} />,
    );
    const pill = screen.getByRole("button", { name: /Filters/ });
    expect(pill.textContent).toContain("1");

    fireEvent.click(pill);
    fireEvent.click(screen.getByLabelText("Show archived"));
    expect(onChange).toHaveBeenCalledWith({ includeArchived: true });
  });

  it("closes on a press outside, not on a press inside the panel", () => {
    renderWithProviders(<FleetMoreFilters filters={NO_FLEET_FILTERS} onChange={vi.fn()} {...options} />);
    const pill = screen.getByRole("button", { name: /Filters/ });
    fireEvent.click(pill);
    fireEvent.mouseDown(screen.getByLabelText("Show archived"));
    expect(pill.getAttribute("aria-expanded")).toBe("true");
    fireEvent.mouseDown(document.body);
    expect(pill.getAttribute("aria-expanded")).toBe("false");
  });

  it("stays open on a press inside a portaled list, closes on Escape", () => {
    renderWithProviders(<FleetMoreFilters filters={NO_FLEET_FILTERS} onChange={vi.fn()} {...options} />);
    const pill = screen.getByRole("button", { name: /Filters/ });
    fireEvent.click(pill);
    const option = document.createElement("div");
    option.setAttribute("role", "option");
    document.body.appendChild(option);
    fireEvent.mouseDown(option);
    expect(pill.getAttribute("aria-expanded")).toBe("true");
    option.remove();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(pill.getAttribute("aria-expanded")).toBe("false");
  });
});

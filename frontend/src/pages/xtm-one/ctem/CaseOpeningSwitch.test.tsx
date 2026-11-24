// SPDX-License-Identifier: Apache-2.0

import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CtemCaseOpening } from "@/hooks/api/useCtemCasesApi";
import { renderWithProviders } from "@/testing/providers";
import { CaseOpeningOffEmptyState, CaseOpeningSwitch } from "./CaseOpeningSwitch";

const state = vi.hoisted(() => ({
  isAdmin: false,
  opening: undefined as CtemCaseOpening | undefined,
  calls: [] as unknown[],
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: state.isAdmin }) }));
vi.mock("@/hooks/api/useCtemCasesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemCasesApi")>()),
  useCtemCaseOpening: () => ({ data: state.opening }),
  useSetCtemCaseOpening: () => ({ isPending: false, mutate: (body: unknown) => state.calls.push(body) }),
}));

beforeEach(() => {
  state.isAdmin = false;
  state.opening = undefined;
  state.calls = [];
});

describe("CaseOpeningSwitch (#1951)", () => {
  it("gives an administrator the switch, and flipping it sends the new value", () => {
    state.isAdmin = true;
    state.opening = { vertical: "cyber", on: false, waiting: 12 };
    renderWithProviders(<CaseOpeningSwitch />);
    const toggle = screen.getByRole("switch", { name: "Case opening" });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    expect(state.calls).toEqual([{ on: true }]);
  });

  it("is drawn disabled before the setting is read, so the toolbar does not shift", () => {
    state.isAdmin = true;
    renderWithProviders(<CaseOpeningSwitch />);
    expect(screen.getByRole("switch", { name: "Case opening" })).toBeDisabled();
  });

  it("draws nothing for anyone but an administrator, on or off", () => {
    state.opening = { vertical: "cyber", on: false, waiting: 3 };
    const { container, unmount } = renderWithProviders(<CaseOpeningSwitch />);
    expect(container).toBeEmptyDOMElement();
    unmount();
    state.opening = { vertical: "cyber", on: true, waiting: 0 };
    const second = renderWithProviders(<CaseOpeningSwitch />);
    expect(second.container).toBeEmptyDOMElement();
  });
});

describe("CaseOpeningOffEmptyState (#1951)", () => {
  it("keeps the standard empty sentence and adds one muted line", () => {
    renderWithProviders(<CaseOpeningOffEmptyState label="No case is open." />);
    const box = screen.getByTestId("ctem-case-opening-off-state");
    expect(box).toHaveTextContent("No case is open.");
    expect(box).toHaveTextContent("Case opening is off");
    expect(screen.queryByRole("button")).toBeNull();
  });
});

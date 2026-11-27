// SPDX-License-Identifier: Apache-2.0

import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CtemCaseOpening } from "@/hooks/api/useCtemCasesApi";
import { ToolbarCompactContext } from "@/components/ui/toolbar-compact-context";
import { renderWithProviders } from "@/testing/providers";
import { CaseOpeningOffEmptyState, CaseOpeningSwitch } from "./CaseOpeningSwitch";

const state = vi.hoisted(() => ({
  isAdmin: false,
  opening: undefined as CtemCaseOpening | undefined,
  calls: [] as unknown[],
  pending: false,
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: state.isAdmin }) }));
vi.mock("@/hooks/api/useCtemCasesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemCasesApi")>()),
  useCtemCaseOpening: () => ({ data: state.opening }),
  useSetCtemCaseOpening: () => ({ isPending: state.pending, mutate: (body: unknown) => state.calls.push(body) }),
}));

beforeEach(() => {
  state.isAdmin = false;
  state.opening = undefined;
  state.calls = [];
  state.pending = false;
});

describe("CaseOpeningSwitch (#1951)", () => {
  it("gives an administrator the switch, and turning it on with nothing waiting sends at once", () => {
    state.isAdmin = true;
    state.opening = { vertical: "cyber", on: false, waiting: 0 };
    renderWithProviders(<CaseOpeningSwitch />);
    const toggle = screen.getByRole("switch", { name: "Case opening" });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(state.calls).toEqual([{ on: true }]);
  });

  it("asks before turning on while signals wait, naming the count, and sends only on confirm (#2536)", () => {
    state.isAdmin = true;
    state.opening = { vertical: "cyber", on: false, waiting: 12 };
    renderWithProviders(<CaseOpeningSwitch />);
    fireEvent.click(screen.getByRole("switch", { name: "Case opening" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Turn case opening on?");
    expect(dialog).toHaveTextContent("12 confirmed signals are waiting");
    expect(state.calls).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
    expect(state.calls).toEqual([{ on: true }]);
  });

  it("sends nothing when the confirmation is cancelled (#2536)", () => {
    state.isAdmin = true;
    state.opening = { vertical: "cyber", on: false, waiting: 1 };
    renderWithProviders(<CaseOpeningSwitch />);
    fireEvent.click(screen.getByRole("switch", { name: "Case opening" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("1 confirmed signal is waiting");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(state.calls).toEqual([]);
  });

  it("turns off without asking", () => {
    state.isAdmin = true;
    state.opening = { vertical: "cyber", on: true, waiting: 0 };
    renderWithProviders(<CaseOpeningSwitch />);
    fireEvent.click(screen.getByRole("switch", { name: "Case opening" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(state.calls).toEqual([{ on: false }]);
  });

  it("is disabled while its change is in flight, so a second flip cannot race it (#2536)", () => {
    state.isAdmin = true;
    state.pending = true;
    state.opening = { vertical: "cyber", on: true, waiting: 0 };
    renderWithProviders(<CaseOpeningSwitch />);
    const toggle = screen.getByRole("switch", { name: "Case opening" });
    expect(toggle).toBeDisabled();
    fireEvent.click(toggle);
    expect(state.calls).toEqual([]);
  });

  it("is drawn disabled before the setting is read, so the toolbar does not shift", () => {
    state.isAdmin = true;
    renderWithProviders(<CaseOpeningSwitch />);
    expect(screen.getByRole("switch", { name: "Case opening" })).toBeDisabled();
  });

  it("keeps its visible label in a full toolbar", () => {
    state.isAdmin = true;
    state.opening = { vertical: "cyber", on: true, waiting: 0 };
    renderWithProviders(<CaseOpeningSwitch />);
    const pill = screen.getByTestId("ctem-case-opening");
    expect(pill).toHaveTextContent("Case opening");
    expect(pill.getAttribute("data-compact")).toBeNull();
  });

  it("drops its visible label in a compact toolbar and stays named, so the row fits 1080 (UX r2 B1 on #1963)", () => {
    state.isAdmin = true;
    state.opening = { vertical: "cyber", on: false, waiting: 0 };
    renderWithProviders(
      <ToolbarCompactContext.Provider value={true}>
        <CaseOpeningSwitch />
      </ToolbarCompactContext.Provider>,
    );
    const pill = screen.getByTestId("ctem-case-opening");
    expect(pill.getAttribute("data-compact")).toBe("true");
    expect(pill).not.toHaveTextContent("Case opening");
    const toggle = screen.getByRole("switch", { name: "Case opening" });
    fireEvent.click(toggle);
    expect(state.calls).toEqual([{ on: true }]);
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

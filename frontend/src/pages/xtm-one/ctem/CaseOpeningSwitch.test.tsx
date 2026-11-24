// SPDX-License-Identifier: Apache-2.0

import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CtemCaseOpening } from "@/hooks/api/useCtemCasesApi";
import { renderWithProviders } from "@/testing/providers";
import { CaseOpeningPausedState, CaseOpeningSwitch } from "./CaseOpeningSwitch";

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

  it("shows a reader a read-only paused badge, and nothing while case opening is on", () => {
    state.opening = { vertical: "cyber", on: false, waiting: 3 };
    const { unmount } = renderWithProviders(<CaseOpeningSwitch />);
    expect(screen.getByTestId("ctem-case-opening-paused")).toHaveTextContent("Case opening paused");
    expect(screen.queryByRole("switch")).toBeNull();
    unmount();
    state.opening = { vertical: "cyber", on: true, waiting: 0 };
    renderWithProviders(<CaseOpeningSwitch />);
    expect(screen.queryByTestId("ctem-case-opening-paused")).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
  });
});

describe("CaseOpeningPausedState (#1951)", () => {
  it("says how many confirmed signals wait, with Turn on for an administrator", () => {
    state.isAdmin = true;
    state.opening = { vertical: "cyber", on: false, waiting: 1200 };
    renderWithProviders(<CaseOpeningPausedState />);
    expect(screen.getByText("Case opening is paused.")).toBeInTheDocument();
    expect(screen.getByText(/1,200 confirmed signals are waiting\./)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("ctem-case-opening-turn-on"));
    expect(state.calls).toEqual([{ on: true }]);
  });

  it("reads one signal in the singular and none at zero, with no Turn on for a reader", () => {
    state.opening = { vertical: "cyber", on: false, waiting: 1 };
    const { unmount } = renderWithProviders(<CaseOpeningPausedState />);
    expect(screen.getByText("1 confirmed signal is waiting.")).toBeInTheDocument();
    expect(screen.queryByTestId("ctem-case-opening-turn-on")).toBeNull();
    unmount();
    state.opening = { vertical: "cyber", on: false, waiting: 0 };
    renderWithProviders(<CaseOpeningPausedState />);
    expect(screen.getByText("No confirmed signal is waiting.")).toBeInTheDocument();
  });

  it("draws nothing while case opening is on", () => {
    state.isAdmin = true;
    state.opening = { vertical: "cyber", on: true, waiting: 0 };
    renderWithProviders(<CaseOpeningPausedState />);
    expect(screen.queryByTestId("ctem-case-opening-paused-state")).toBeNull();
  });
});

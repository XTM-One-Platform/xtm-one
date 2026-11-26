// #2528: the purge of every signal is an administrators-only icon button; its dialog shows the live
// counts, keeps the resync off by default, and starts only once the shown code is typed back.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { SignalsPurgeAction } from "./SignalsPurgeAction";

const state = vi.hoisted(() => ({
  isAdmin: true,
  started: [] as unknown[],
  info: { counts: { signals: 4400, cases: 37 }, confirm_word: "PURGE", job: null } as Record<string, unknown>,
}));

vi.mock("@/hooks/useAuth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAuth")>()),
  useAuth: () => ({ isAdmin: state.isAdmin }),
}));

vi.mock("@/hooks/api/useCtemSignalsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemSignalsApi")>()),
  useCtemSignalPurge: () => ({ data: state.info, isError: false, error: null, refetch: () => Promise.resolve() }),
  useStartCtemSignalPurge: () => ({
    isPending: false,
    mutate: (body: unknown, opts?: { onSuccess?: () => void }) => {
      state.started.push(body);
      opts?.onSuccess?.();
    },
  }),
}));

describe("SignalsPurgeAction", () => {
  beforeEach(() => {
    state.isAdmin = true;
    state.started = [];
  });

  it("is not drawn for a non-administrator", () => {
    state.isAdmin = false;
    renderWithProviders(<SignalsPurgeAction />);
    expect(screen.queryByTestId("ctem-signals-purge")).toBeNull();
  });

  it("shows the live counts and keeps the resync unchecked by default", () => {
    renderWithProviders(<SignalsPurgeAction />);
    const trigger = screen.getByTestId("ctem-signals-purge");
    expect(trigger.getAttribute("aria-label")).toBe("Purge all signals");
    fireEvent.click(trigger);
    const preview = screen.getByTestId("ctem-signals-purge-preview");
    expect(preview.textContent).toContain("4,400 signals");
    expect(preview.textContent).toContain("37 cases");
    expect(screen.getByRole("checkbox", { name: "Resync signals from sources after the purge" }).getAttribute("aria-checked")).toBe("false");
    expect(screen.getByRole("button", { name: "Purge" })).toHaveProperty("disabled", true);
  });

  it("starts the purge with the resync choice once the code is typed back", () => {
    renderWithProviders(<SignalsPurgeAction />);
    fireEvent.click(screen.getByTestId("ctem-signals-purge"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Resync signals from sources after the purge" }));
    const code = screen.getByTestId("confirm-challenge-code").textContent ?? "";
    fireEvent.change(screen.getByRole("textbox"), { target: { value: code } });
    fireEvent.click(screen.getByRole("button", { name: "Purge" }));
    expect(state.started).toEqual([{ confirm: "PURGE", resync: true }]);
  });
});

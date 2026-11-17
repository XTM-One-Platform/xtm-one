// Profile > Phones > PIN lock: on, off, chosen or changed from a computer;
// kept on by the organization's policy.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { AppLockStatus } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn() }));
vi.mock("@/lib/api", () => ({ api }));
vi.mock("@/hooks/useCompanionDevice", () => ({ useCompanionDevice: () => false }));

import { PinLockCard } from "./PinLockCard";

function status(overrides: Partial<AppLockStatus> = {}): AppLockStatus {
  return {
    applies: false,
    required: false,
    has_pin: false,
    enabled: false,
    companion: false,
    locked: false,
    setup_required: false,
    attempts_left: 5,
    min_digits: 6,
    max_digits: 8,
    ...overrides,
  };
}

describe("PinLockCard", () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset();
  });

  it("turns the lock on by choosing a PIN, never asking a computer for the current one", async () => {
    api.get.mockResolvedValue(status());
    api.put.mockResolvedValue(status({ applies: true, has_pin: true, enabled: true }));
    renderWithProviders(<PinLockCard />);
    fireEvent.click(await screen.findByRole("switch", { name: "Ask for a PIN on my phones" }));
    expect(screen.queryByLabelText("Current PIN")).toBeNull();
    fireEvent.change(screen.getByLabelText("New PIN"), { target: { value: "4829a15" } });
    fireEvent.change(screen.getByLabelText("Confirm the PIN"), { target: { value: "482915" } });
    expect((screen.getByLabelText("New PIN") as HTMLInputElement).value).toBe("482915");
    fireEvent.click(screen.getByRole("button", { name: "Save PIN" }));
    await waitFor(() => expect(api.put.mock.calls).toEqual([["/app-lock/pin", { pin: "482915" }]]));
    expect(api.patch).not.toHaveBeenCalled();
  });

  it("refuses a guessable PIN before sending it", async () => {
    api.get.mockResolvedValue(status());
    renderWithProviders(<PinLockCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Choose a PIN" }));
    fireEvent.change(screen.getByLabelText("New PIN"), { target: { value: "111111" } });
    fireEvent.change(screen.getByLabelText("Confirm the PIN"), { target: { value: "111111" } });
    fireEvent.click(screen.getByRole("button", { name: "Save PIN" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Choose a PIN that is not one digit repeated.");
    expect(api.put).not.toHaveBeenCalled();
  });

  it("stays on while the organization requires it", async () => {
    api.get.mockResolvedValue(status({ applies: true, required: true, has_pin: true, enabled: true }));
    renderWithProviders(<PinLockCard />);
    const toggle = await screen.findByRole("switch", { name: "Ask for a PIN on my phones" });
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    expect((toggle as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Required by your organization")).toBeTruthy();
  });

  it("turns off with the switch", async () => {
    api.get.mockResolvedValue(status({ applies: true, has_pin: true, enabled: true }));
    api.patch.mockResolvedValue(status({ has_pin: true }));
    renderWithProviders(<PinLockCard />);
    fireEvent.click(await screen.findByRole("switch", { name: "Ask for a PIN on my phones" }));
    await waitFor(() => expect(api.patch.mock.calls).toEqual([["/app-lock", { enabled: false }]]));
  });
});

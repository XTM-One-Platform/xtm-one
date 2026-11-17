// "Tell the members" on the instructions position (#10255): checked by default,
// so a move from the page tells the members as before; unchecked, the position
// is sent with announce: false and the toast says the members' instructions are
// unchanged. Read-only readers get no switch.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

import { FleetInstructionsPositionControl } from "./FleetInstructionsPosition";

const api = vi.hoisted(() => ({ mutateAsync: vi.fn(async (_body: { position: string; announce: boolean }) => undefined) }));
vi.mock("@/hooks/api/useFleetApi", () => ({
  useSetFleetInstructionsPosition: () => ({ mutateAsync: api.mutateAsync, isPending: false }),
}));

const toasts = vi.hoisted(() => ({ calls: [] as { title?: string; description?: string }[] }));
vi.mock("@/components/ui/toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/ui/toast")>()),
  useToast: () => ({ toast: (t: (typeof toasts.calls)[number]) => toasts.calls.push(t) }),
}));

// A native select stands for the Radix one, so a test picks a position in one event.
vi.mock("@/components/ui/select", () => ({
  Select: ({ onValueChange, children }: { onValueChange: (v: string) => void; children: React.ReactNode }) => (
    <select aria-label="position" onChange={(e) => onValueChange(e.target.value)}>
      <option value="" />
      {children}
    </select>
  ),
  SelectTrigger: () => null,
  SelectValue: () => null,
  SelectContent: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  SelectItem: ({ value, children }: { value: string; children: React.ReactNode }) => <option value={value}>{children}</option>,
}));

afterEach(() => {
  cleanup();
  api.mutateAsync.mockClear();
  toasts.calls = [];
});

describe("FleetInstructionsPositionControl", () => {
  it("tells the members by default", async () => {
    renderWithProviders(<FleetInstructionsPositionControl fleetId="f1" position={null} canManage isAdmin />);
    expect(screen.getByRole("switch", { name: "Tell the members" })).toHaveAttribute("aria-checked", "true");
    fireEvent.change(screen.getByLabelText("position"), { target: { value: "medium_low" } });
    await waitFor(() => expect(api.mutateAsync).toHaveBeenCalledWith({ position: "medium_low", announce: true }));
    expect(toasts.calls[0]?.description).toContain("The members are told in their next run.");
  });

  it("sends announce false when turned off and says the members' instructions are unchanged", async () => {
    renderWithProviders(<FleetInstructionsPositionControl fleetId="f1" position={null} canManage isAdmin />);
    const toggle = screen.getByRole("switch", { name: "Tell the members" });
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "false");
    fireEvent.change(screen.getByLabelText("position"), { target: { value: "low" } });
    await waitFor(() => expect(api.mutateAsync).toHaveBeenCalledWith({ position: "low", announce: false }));
    expect(toasts.calls[0]?.description).toContain("the members' instructions are unchanged");
  });

  it("starts the switch off for a position saved without telling the members", () => {
    renderWithProviders(<FleetInstructionsPositionControl fleetId="f1" position="medium_low" announced={false} canManage isAdmin />);
    expect(screen.getByRole("switch", { name: "Tell the members" })).toHaveAttribute("aria-checked", "false");
  });

  it("starts the switch on for a position saved and told to the members", () => {
    renderWithProviders(<FleetInstructionsPositionControl fleetId="f1" position="medium_low" announced canManage isAdmin />);
    expect(screen.getByRole("switch", { name: "Tell the members" })).toHaveAttribute("aria-checked", "true");
  });

  it("shows no switch to a reader who cannot move the position", () => {
    renderWithProviders(<FleetInstructionsPositionControl fleetId="f1" position={null} canManage={false} isAdmin={false} />);
    expect(screen.queryByRole("switch", { name: "Tell the members" })).toBeNull();
    expect(screen.getByTestId("fleet-instructions-position-value")).toHaveTextContent("Not set");
  });
});

// The "Run checks as" pick of the guided CTEM loop setup (dev-docs/platform-health.md 12.3): saved on its own,
// the server's refusal shown and the field marked invalid.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api", () => ({ api }));

import { CtemSetupRunAs } from "./CtemSetupRunAs";

const users = [
  { id: "u-1", email: "ana@example.com", full_name: "Ana Analyst", role: "user", is_active: true },
  { id: "u-2", email: "old@example.com", full_name: "Old Account", role: "user", is_active: false },
];

function answer(settings: Record<string, unknown>) {
  api.get.mockImplementation((path: string) => Promise.resolve(path === "/users" ? users : path === "/health/settings" ? settings : {}));
}

async function pick(name: string) {
  await userEvent.click(await screen.findByRole("combobox", { name: "Run checks as" }));
  await userEvent.click(await screen.findByRole("option", { name: new RegExp(name) }));
}

describe("CtemSetupRunAs", () => {
  beforeEach(() => vi.clearAllMocks());

  it("lists active users only and saves the pick on its own", async () => {
    answer({ module_run_as_user_id: null });
    api.put.mockResolvedValue({ module_run_as_user_id: "u-1" });
    renderWithProviders(<CtemSetupRunAs />);
    await userEvent.click(await screen.findByRole("combobox", { name: "Run checks as" }));
    expect(screen.queryByRole("option", { name: /Old Account/ })).toBeNull();
    await userEvent.click(await screen.findByRole("option", { name: /Ana Analyst/ }));
    await userEvent.click(screen.getByTestId("ctem-loop-setup-run-as-save"));
    expect(api.put).toHaveBeenCalledWith("/health/settings/run-as", { user_id: "u-1" });
  });

  it("shows the refusal naming the modules and marks the field invalid", async () => {
    answer({ module_run_as_user_id: null });
    // The 400's code and module names, worded from the catalogs (UX r1 B3), never the server's English line.
    api.put.mockRejectedValue(
      Object.assign(new Error("server English line"), {
        status: 400,
        detail: { code: "run_as_missing_module_access", modules: ["OpenCTI"], message: "server English line" },
      }),
    );
    renderWithProviders(<CtemSetupRunAs />);
    await pick("Ana Analyst");
    await userEvent.click(screen.getByTestId("ctem-loop-setup-run-as-save"));
    expect(await screen.findByTestId("ctem-loop-setup-run-as-error")).toHaveTextContent("This user has no access to OpenCTI");
    expect(screen.getByTestId("ctem-loop-setup-run-as-error")).not.toHaveTextContent("server English line");
    expect(screen.getByRole("combobox", { name: "Run checks as" }).getAttribute("aria-invalid")).toBe("true");
  });

  it("keeps Save off until a new user is picked", async () => {
    answer({ module_run_as_user_id: "u-1" });
    renderWithProviders(<CtemSetupRunAs />);
    expect(await screen.findByTestId("ctem-loop-setup-run-as-saved")).toBeTruthy();
    expect((screen.getByTestId("ctem-loop-setup-run-as-save") as HTMLButtonElement).disabled).toBe(true);
  });
});

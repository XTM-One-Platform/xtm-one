// Settings > Platform Health > "Run checks as" (dev-docs/platform-health.md 12.3).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { HealthRunAsBlock } from "@/pages/settings/HealthRunAsBlock";
import { renderWithProviders } from "@/testing/providers";
import type { HealthSettings } from "@/types";

const mode = vi.hoisted(() => ({ xtmOne: true }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useIsXtmOne: () => mode.xtmOne,
}));

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<object>()), api: { get, put: vi.fn(), post: vi.fn() } }));

const users = [
  { id: "u-1", email: "ana@example.com", full_name: "Ana Analyst", role: "user", is_active: true },
  { id: "u-2", email: "bob@example.com", full_name: "", role: "admin", is_active: true },
];
const draft = (runAs: string | null) => ({ module_run_as_user_id: runAs }) as unknown as HealthSettings;

beforeEach(() => {
  get.mockReset().mockImplementation((url: string) => Promise.resolve(url === "/users" ? users : []));
});

afterEach(() => {
  cleanup();
  mode.xtmOne = true;
});

describe("HealthRunAsBlock", () => {
  it("reads Not set at rest when no user is configured", () => {
    renderWithProviders(<HealthRunAsBlock draft={draft(null)} editing={false} onChange={() => {}} />);
    expect(screen.getByTestId("health.run-as")).toHaveTextContent("Run checks as");
    expect(screen.getByTestId("health.run-as")).toHaveTextContent("Not set");
  });

  it("reads the configured user's name and email at rest", async () => {
    renderWithProviders(<HealthRunAsBlock draft={draft("u-1")} editing={false} onChange={() => {}} />);
    expect(await screen.findByText("Ana Analyst (ana@example.com)")).toBeTruthy();
  });

  it("writes the picked user into the draft", async () => {
    const onChange = vi.fn();
    renderWithProviders(<HealthRunAsBlock draft={draft(null)} editing onChange={onChange} />);
    await userEvent.click(await screen.findByRole("combobox", { name: "Run checks as" }));
    await userEvent.click(await screen.findByRole("option", { name: /Ana Analyst/ }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ module_run_as_user_id: "u-1" }));
  });

  it("is absent in Copilot mode", () => {
    mode.xtmOne = false;
    renderWithProviders(<HealthRunAsBlock draft={draft(null)} editing={false} onChange={() => {}} />);
    expect(screen.queryByTestId("health.run-as")).toBeNull();
  });
});

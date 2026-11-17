// The vault and variable badges of VaultSecretsNotice explain themselves to a keyboard
// user too: each is a tab stop in document order, its tooltip opens on focus
// with the hover copy, and Escape closes it.

import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import { VaultSecretsNotice } from "./vault-secrets";

vi.mock("@/hooks/usePlatformConfig", () => ({ useCustomVariablesEnabled: () => true }));
vi.mock("@/hooks/api/useVariableApi", () => ({
  useVariables: () => ({ data: [{ id: "v-1", name: "LINEAR_TOKEN" }, { id: "v-2", name: "ZENDESK_TOKEN" }] }),
}));

const status = {
  references: { api_key: ["vault-a"], client_secret: ["vault-b"] },
  variables: { headers: ["v-1"], env: ["v-2"] },
  invalid_references: [],
  failures: [],
};

describe("VaultSecretsNotice reference badges", () => {
  it("reaches every vault and variable badge with Tab, in document order", async () => {
    const user = userEvent.setup();
    renderWithProviders(<VaultSecretsNotice status={status} />);
    const badges = screen.getAllByTestId("secret-reference-badge");
    expect(badges.map((badge) => badge.textContent)).toEqual([
      "API key · vault-a",
      "Client secret · vault-b",
      "Headers · LINEAR_TOKEN",
      "Environment variables · ZENDESK_TOKEN",
    ]);
    for (const badge of badges) {
      await user.tab();
      expect(document.activeElement).toBe(badge);
    }
  });

  it("opens the vault tooltip on focus and closes it with Escape", async () => {
    const user = userEvent.setup();
    renderWithProviders(<VaultSecretsNotice status={status} />);
    await user.tab();
    expect((await screen.findByRole("tooltip")).textContent).toContain('Read from the vault provider "vault-a" each time it is used.');
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
  });

  it("opens the variable tooltip on focus with the hover copy", async () => {
    const user = userEvent.setup();
    renderWithProviders(<VaultSecretsNotice status={status} />);
    for (let presses = 0; presses < 3; presses++) await user.tab();
    expect((await screen.findByRole("tooltip")).textContent).toContain("Read from the secret variable each time it is used");
  });
});
